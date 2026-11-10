"""The RabbitMQ passwords must only ever reach the target repo inside the vault file."""

import logging
import subprocess

from scripts.byoc_pr import cli
from scripts.byoc_pr.editors import SECRETS
from scripts.byoc_pr.vault import Vault
from tests.unit.byoc_pr.conftest import GALAXY_URL, REPO, THROWAWAY_VAULT_PASSWORD

PASSWORDS = ["AlicePuls4rPassw0rd", "ErinPuls4rPassw0rd", "DavePuls4rPassw0rd", "Carol-pw!"]
API_KEY = "admin-api-key-0123456789"
EMAILS = ["alice@example.org", "erin@example.org"]


def test_no_password_in_output_argv_pr_body_or_plain_files(
    capsys, caplog, tmp_path, session, fake_gh, vault_file, origin
):
    caplog.set_level(logging.DEBUG)
    workdir = tmp_path / "clone"
    argv = [
        "--galaxy-url", GALAXY_URL, "--open-pr", "--target-repo", REPO, "--workdir", str(workdir),
        "--apply", "--vault-password-file", str(vault_file), "--verbose",
        "--summary-json", str(tmp_path / "pr-summary.json"),
    ]  # fmt: skip
    assert cli.main(argv, environ={"GALAXY_API_KEY": API_KEY}, runner=fake_gh, session=session) == 0
    captured = capsys.readouterr()
    assert len(fake_gh.created) == 2

    texts = {
        "stdout": captured.out,
        "stderr": captured.err,
        "logs": caplog.text,
        "argv": "\n".join(" ".join(args) for args, _ in fake_gh.calls),
        "stdin": "\n".join(stdin or "" for _, stdin in fake_gh.calls),
        "pr bodies": "\n".join(pr["body"] for pr in fake_gh.created),
        "summary": (tmp_path / "pr-summary.json").read_text(),
    }
    git_history = subprocess.run(
        ["git", "log", "-p", "--all"], cwd=origin, capture_output=True, text=True, check=True
    ).stdout
    texts["git history"] = git_history
    for path in workdir.rglob("*"):
        if path.is_file() and ".git" not in path.parts:
            texts[str(path.relative_to(workdir))] = path.read_text(errors="replace")

    secrets = [*PASSWORDS, API_KEY, THROWAWAY_VAULT_PASSWORD.decode()]
    for where, text in texts.items():
        for secret in secrets:
            assert secret not in text, f"{secret!r} leaked into {where}"
    for body in texts["pr bodies"], texts["git history"], texts["summary"]:
        for email in EMAILS:
            assert email not in body

    # ...and the passwords are in the vault file of their own branch
    vault = Vault(THROWAWAY_VAULT_PASSWORD)
    for branch, password in (("byoc/alice_hpc", PASSWORDS[0]), ("byoc/erin_lab", PASSWORDS[1])):
        vaulttext = subprocess.run(
            ["git", "show", f"{branch}:{SECRETS}"],
            cwd=origin,
            capture_output=True,
            text=True,
            check=True,
        ).stdout
        assert password in vault.decrypt(vaulttext)


def test_files_mode_keeps_passwords_out_of_plain_files_and_summary(
    capsys, caplog, tmp_path, session, repo_dir, vault_file
):
    caplog.set_level(logging.DEBUG)
    summary = tmp_path / "summary.json"

    def no_git(args, **kwargs):  # pragma: no cover - must never be called
        raise AssertionError(args)

    argv = [
        "--galaxy-url", GALAXY_URL, "--repo-path", str(repo_dir), "--verbose",
        "--summary-json", str(summary),
    ]  # fmt: skip
    env = {"GALAXY_API_KEY": API_KEY, "ANSIBLE_VAULT_PASSWORD_FILE": str(vault_file)}
    assert cli.main(argv, environ=env, runner=no_git, session=session) == 0
    captured = capsys.readouterr()
    texts = {"stdout": captured.out, "stderr": captured.err, "logs": caplog.text}
    texts["summary"] = summary.read_text()
    for path in repo_dir.rglob("*"):
        if path.is_file():
            texts[str(path)] = path.read_text(errors="replace")
    for where, text in texts.items():
        for secret in [*PASSWORDS, API_KEY, THROWAWAY_VAULT_PASSWORD.decode(), *EMAILS]:
            assert secret not in text, f"{secret!r} leaked into {where}"
