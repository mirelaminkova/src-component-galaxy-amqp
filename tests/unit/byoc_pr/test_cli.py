"""``--open-pr`` end to end: recorded Galaxy responses -> local bare "fork", faked ``gh``."""

import runpy

import pytest

from scripts.byoc_pr import cli
from scripts.byoc_pr.github import CommandError
from tests.unit.byoc_pr.conftest import GALAXY_URL, REPO, FakeResponse, git

ENV = {"GALAXY_API_KEY": "admin-api-key"}


def args(*extra):
    return ["--galaxy-url", GALAXY_URL, "--open-pr", "--target-repo", REPO, *extra]


def test_secrets_are_never_arguments():
    options = {opt for action in cli.build_parser()._actions for opt in action.option_strings}
    assert {"--api-key", "--vault-password", "--password"}.isdisjoint(options)
    assert {"--api-key-file", "--vault-password-file"} <= options
    assert cli.build_parser().parse_args(["--galaxy-url", "x"]).target_repo == (
        "mirelaminkova/infrastructure-playbook"
    )


def test_read_api_key(tmp_path):
    key_file = tmp_path / "key"
    key_file.write_text("from-file\n")
    assert cli.read_api_key(key_file, ENV) == "from-file"
    assert cli.read_api_key(None, ENV) == "admin-api-key"
    with pytest.raises(cli.UsageError, match="GALAXY_API_KEY"):
        cli.read_api_key(None, {})
    with pytest.raises(cli.UsageError, match="cannot read"):
        cli.read_api_key(tmp_path / "missing", ENV)


def test_open_pr_is_a_dry_run_by_default_and_pushes_nothing(
    capsys, session, fake_gh, vault_file, origin
):
    code = cli.main(
        args("--vault-password-file", str(vault_file)), environ=ENV, runner=fake_gh, session=session
    )
    out = capsys.readouterr().out
    assert code == 0
    assert "skipped: Galaxy user 'carol': username: must match" in out
    assert "BYOC forms: 4 filled in, 3 valid" in out
    assert "skipped: Galaxy user 'dave': endpoint 'egi01' partly present" in out
    assert "new: alice_hpc (Galaxy user 'alice') would be pushed to byoc/alice_hpc" in out
    assert "new: erin_lab (Galaxy user 'erin') would be pushed to byoc/erin_lab" in out
    assert "+  - user: galaxy_erin_lab\n" in out
    assert fake_gh.created == []
    assert not any(a[:2] == ["git", "push"] for a, _ in fake_gh.calls)
    assert git("branch", "--list", cwd=origin).split() == ["*", "master"]


def test_dry_run_without_vault_password(capsys, session, fake_gh, tmp_path):
    workdir = tmp_path / "clone"
    code = cli.main(args("--workdir", str(workdir)), environ=ENV, runner=fake_gh, session=session)
    out = capsys.readouterr().out
    assert code == 0
    assert "(needs --vault-password-file): rabbitmq_password_galaxy_alice_hpc" in out
    assert (workdir / ".git").is_dir()


def test_apply_opens_one_pr_per_endpoint_and_is_idempotent(
    capsys, session, fake_gh, vault_file, origin
):
    argv = args("--apply", "--vault-password-file", str(vault_file))
    assert cli.main(argv, environ=ENV, runner=fake_gh, session=session) == 0
    out = capsys.readouterr().out
    assert f"opened: alice_hpc (Galaxy user 'alice'): https://github.com/{REPO}/pull/1" in out
    assert f"opened: erin_lab (Galaxy user 'erin'): https://github.com/{REPO}/pull/2" in out
    assert [pr["branch"] for pr in fake_gh.created] == ["byoc/alice_hpc", "byoc/erin_lab"]
    # each branch only carries its own endpoint
    mq = git("show", "byoc/erin_lab:group_vars/mq.yml", cwd=origin)
    assert "galaxy_erin_lab" in mq
    assert "galaxy_alice_hpc" not in mq
    heads = git("for-each-ref", "--format=%(objectname)", "refs/heads", cwd=origin)

    assert cli.main(argv, environ=ENV, runner=fake_gh, session=session) == cli.EXIT_NOTHING_NEW
    out = capsys.readouterr().out
    pr1 = f"https://github.com/{REPO}/pull/1"
    assert f"open PR: alice_hpc (Galaxy user 'alice') already has {pr1}, reused" in out
    assert len(fake_gh.created) == 2
    assert git("for-each-ref", "--format=%(objectname)", "refs/heads", cwd=origin) == heads


def test_merged_endpoint_is_reported_present(capsys, session, fake_gh, vault_file, origin):
    argv = args("--apply", "--vault-password-file", str(vault_file))
    assert cli.main(argv, environ=ENV, runner=fake_gh, session=session) == 0
    git("update-ref", "refs/heads/master", "refs/heads/byoc/alice_hpc", cwd=origin)
    fake_gh.open_prs.clear()
    capsys.readouterr()
    assert cli.main(argv, environ=ENV, runner=fake_gh, session=session) == cli.EXIT_NOTHING_NEW
    out = capsys.readouterr().out
    assert f"present: alice_hpc is already in {REPO}@master" in out
    assert "skipped: erin_lab: branch byoc/erin_lab exists without an open PR" in out


def test_apply_needs_vault_password(capsys, session, fake_gh):
    assert cli.main(args("--apply"), environ=ENV, runner=fake_gh, session=session) == 1
    assert "error: --open-pr --apply needs a vault password file" in capsys.readouterr().err
    assert session.calls == []


def test_dry_run_and_apply_are_exclusive(capsys):
    with pytest.raises(SystemExit):
        cli.main(args("--apply", "--dry-run"), environ=ENV)


def test_galaxy_error_exits_1(capsys, session, fake_gh):
    session.overrides["/api/users"] = FakeResponse(403, {})
    assert cli.main(args(), environ=ENV, runner=fake_gh, session=session) == 1
    assert "error: GET /api/users failed: HTTP 403" in capsys.readouterr().err


def test_edit_failure_continues_and_exits_1(capsys, session, fake_gh, vault_file, monkeypatch):
    def broken(texts, endpoint, *rest):
        if endpoint.id == "alice_hpc":
            raise cli.EditError("layout changed")
        return original(texts, endpoint, *rest)

    original = cli.build_changes
    monkeypatch.setattr(cli, "build_changes", broken)
    code = cli.main(
        args("--vault-password-file", str(vault_file)), environ=ENV, runner=fake_gh, session=session
    )
    captured = capsys.readouterr()
    assert code == 1
    assert "failed: alice_hpc: layout changed" in captured.err
    assert "new: erin_lab" in captured.out


def test_publish_failure_exits_1(capsys, session, fake_gh, vault_file, monkeypatch):
    def push_rejected(self, endpoint, changes):
        raise CommandError("git push failed (exit 1): rejected")

    monkeypatch.setattr(cli.GitHub, "publish", push_rejected)
    argv = args("--apply", "--vault-password-file", str(vault_file))
    assert cli.main(argv, environ=ENV, runner=fake_gh, session=session) == 1
    assert "failed: alice_hpc: git push failed (exit 1): rejected" in capsys.readouterr().err


def test_module_entry_point(monkeypatch, capsys):
    monkeypatch.setattr("sys.argv", ["byoc_pr", "--help"])
    with pytest.raises(SystemExit) as excinfo:
        runpy.run_module("scripts.byoc_pr", run_name="__main__")
    assert excinfo.value.code == 0
    assert "--api-key-file" in capsys.readouterr().out
