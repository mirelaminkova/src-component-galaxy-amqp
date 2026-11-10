"""Default mode for the CI pipeline: edit an existing checkout, no git, no push, no PR."""

import json

import pytest

from scripts.byoc_pr import cli
from scripts.byoc_pr.editors import JOB_CONF, MQ, PATHS, SECRETS
from scripts.byoc_pr.planner import read_repo
from tests.unit.byoc_pr.conftest import GALAXY_URL, FakeResponse

ENV = {"GALAXY_API_KEY": "admin-api-key"}


def no_git(args, **kwargs):  # pragma: no cover - must never be called
    raise AssertionError(f"files-only mode ran {args}")


def main(*argv, environ=ENV, session):
    return cli.main(
        ["--galaxy-url", GALAXY_URL, *argv], environ=environ, runner=no_git, session=session
    )


def snapshot(root):
    return {path: (root / path).read_bytes() for path in PATHS}


def test_writes_all_new_endpoints_into_the_checkout(capsys, session, repo_dir, vault_file, vault):
    before = read_repo(repo_dir)
    code = main(
        "--repo-path", str(repo_dir), "--vault-password-file", str(vault_file), session=session
    )
    out = capsys.readouterr().out
    assert code == cli.EXIT_CHANGES == 0
    assert "written: alice_hpc (Galaxy user 'alice')" in out
    assert "written: erin_lab (Galaxy user 'erin')" in out
    after = read_repo(repo_dir)
    assert set(path for path in PATHS if after[path] != before[path]) == set(PATHS)
    mq = after[MQ]
    assert mq.index("galaxy_alice_hpc") < mq.index("galaxy_erin_lab")
    assert "pulsar_eu_erin_lab" in after[JOB_CONF]
    secrets = vault.decrypt(after[SECRETS])
    assert "rabbitmq_password_galaxy_alice_hpc" in secrets
    assert "rabbitmq_password_galaxy_erin_lab" in secrets


def test_second_run_finds_nothing_new(capsys, session, repo_dir, vault_file):
    argv = ("--repo-path", str(repo_dir), "--vault-password-file", str(vault_file))
    assert main(*argv, session=session) == cli.EXIT_CHANGES
    files = snapshot(repo_dir)
    capsys.readouterr()
    assert main(*argv, session=session) == cli.EXIT_NOTHING_NEW == 3
    out = capsys.readouterr().out
    assert "present: alice_hpc is already in" in out
    assert snapshot(repo_dir) == files


def test_vault_password_file_from_environment(session, repo_dir, vault_file):
    env = {**ENV, "ANSIBLE_VAULT_PASSWORD_FILE": str(vault_file)}
    assert main("--repo-path", str(repo_dir), environ=env, session=session) == cli.EXIT_CHANGES


def test_writing_needs_a_vault_password(capsys, session, repo_dir):
    files = snapshot(repo_dir)
    assert main("--repo-path", str(repo_dir), session=session) == cli.EXIT_ERROR
    assert "error: writing the files needs a vault password file" in capsys.readouterr().err
    assert snapshot(repo_dir) == files
    assert session.calls == []


def test_dry_run_writes_nothing(capsys, session, repo_dir):
    files = snapshot(repo_dir)
    assert main("--repo-path", str(repo_dir), "--dry-run", session=session) == cli.EXIT_CHANGES
    out = capsys.readouterr().out
    assert "new: alice_hpc (Galaxy user 'alice') would be written" in out
    assert "+  - user: galaxy_erin_lab\n" in out
    assert snapshot(repo_dir) == files


def test_mode_is_required_and_apply_needs_open_pr(capsys):
    with pytest.raises(SystemExit) as excinfo:
        cli.main(["--galaxy-url", GALAXY_URL], environ=ENV)
    assert excinfo.value.code == 2
    assert "--repo-path is required unless --open-pr" in capsys.readouterr().err
    with pytest.raises(SystemExit) as excinfo:
        cli.main(["--galaxy-url", GALAXY_URL, "--repo-path", ".", "--apply"], environ=ENV)
    assert excinfo.value.code == 2
    with pytest.raises(SystemExit):
        cli.main(["--galaxy-url", GALAXY_URL, "--repo-path", ".", "--open-pr"], environ=ENV)


def test_summary_json(tmp_path, session, repo_dir, vault_file):
    summary_path = tmp_path / "out" / "summary.json"
    code = main(
        "--repo-path", str(repo_dir), "--vault-password-file", str(vault_file),
        "--summary-json", str(summary_path), session=session,
    )  # fmt: skip
    assert code == 0
    summary = json.loads(summary_path.read_text())
    assert summary["mode"] == "files"
    assert summary["dry_run"] is False
    assert summary["exit_code"] == 0
    assert summary["error"] is None
    assert [(e["id"], e["galaxy_username"], e["status"]) for e in summary["new"]] == [
        ("alice_hpc", "alice", "written"),
        ("erin_lab", "erin", "written"),
    ]
    assert summary["new"][0]["files"] == list(PATHS)
    assert summary["new"][0]["rabbitmq_user"] == "galaxy_alice_hpc"
    assert summary["invalid"] == [
        {
            "galaxy_username": "carol",
            "errors": [
                "username: must match ^[a-z0-9_-]{3,32}$",
                "password: must be alphanumeric and 10 to 128 characters long",
                "max_accepted_cores: must be a whole number from 1 to 1024",
                "max_accepted_mem: must be a whole number from 1 to 16384",
                "min_accepted_gpus: must not be larger than max_accepted_gpus",
            ],
        }
    ]
    assert summary["skipped"][0]["galaxy_username"] == "dave"
    assert summary["skipped"][0]["reason"].startswith("endpoint 'egi01' partly present")
    assert summary["present"] == []
    assert summary["pr"]["title"] == "Add BYOC pulsar endpoints alice_hpc, erin_lab"
    assert "Requested by Galaxy user: `alice`" in summary["pr"]["body"]
    assert "Requested by Galaxy user: `erin`" in summary["pr"]["body"]


def test_summary_on_nothing_new_and_on_error(tmp_path, session, repo_dir, vault_file):
    summary_path = tmp_path / "summary.json"
    argv = ("--repo-path", str(repo_dir), "--vault-password-file", str(vault_file))
    assert main(*argv, session=session) == 0
    assert main(*argv, "--summary-json", str(summary_path), session=session) == 3
    summary = json.loads(summary_path.read_text())
    assert summary["new"] == []
    assert [e["id"] for e in summary["present"]] == ["alice_hpc", "erin_lab"]
    assert summary["pr"] is None

    session.overrides["/api/users"] = FakeResponse(500, {})
    assert main(*argv, "--summary-json", str(summary_path), session=session) == 1
    summary = json.loads(summary_path.read_text())
    assert summary["error"] == "GET /api/users failed: HTTP 500"
    assert summary["exit_code"] == 1


def test_edit_failure_writes_the_others_and_exits_1(
    capsys, session, repo_dir, vault_file, monkeypatch
):
    def broken(texts, endpoint, *rest):
        if endpoint.id == "alice_hpc":
            raise cli.EditError("layout changed")
        return original(texts, endpoint, *rest)

    original = cli.build_changes
    monkeypatch.setattr(cli, "build_changes", broken)
    summary_path = repo_dir / "summary.json"
    argv = ("--repo-path", str(repo_dir), "--vault-password-file", str(vault_file))
    assert main(*argv, "--summary-json", str(summary_path), session=session) == 1
    assert "failed: alice_hpc: layout changed" in capsys.readouterr().err
    assert "galaxy_erin_lab" in (repo_dir / MQ).read_text()
    summary = json.loads(summary_path.read_text())
    assert [(e["id"], e["status"], e.get("error")) for e in summary["new"]] == [
        ("alice_hpc", "failed", "layout changed"),
        ("erin_lab", "written", None),
    ]
    assert "alice" not in summary["pr"]["body"]


def test_missing_target_file_is_an_error(capsys, session, repo_dir, vault_file):
    (repo_dir / MQ).unlink()
    argv = ("--repo-path", str(repo_dir), "--vault-password-file", str(vault_file))
    assert main(*argv, session=session) == 1
    assert "group_vars/mq.yml: not found" in capsys.readouterr().err
