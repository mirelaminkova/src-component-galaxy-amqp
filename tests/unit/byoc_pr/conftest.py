"""Fixtures for the byoc_pr tests: fake Galaxy API, fixture target repo, mocked ``gh``.

- ``fixtures/galaxy/``: Galaxy 26.0 responses of ``GET /api/users``,
  ``GET /api/users/{id}/information/inputs`` and ``GET /api/users/{id}`` (admin view).
- ``fixtures/infrastructure-playbook/``: copies of the four plain target files from
  usegalaxy-eu/infrastructure-playbook (master, 3e40ac0); the vault file is built from
  ``fixtures/secret_pulsar_plain.yml`` with a throwaway password at test time.
- ``origin``: a local bare git repository standing in for the GitHub fork; ``gh`` is faked.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path
from urllib.parse import urlparse

import pytest

from scripts.byoc_pr.editors import SECRETS
from scripts.byoc_pr.planner import read_repo
from scripts.byoc_pr.validation import ByocRequest
from scripts.byoc_pr.vault import Vault

FIXTURES = Path(__file__).parent / "fixtures"
GALAXY = FIXTURES / "galaxy"
TARGET = FIXTURES / "infrastructure-playbook"
THROWAWAY_VAULT_PASSWORD = b"throwaway-vault-password-for-tests"
GALAXY_URL = "https://galaxy.test"
REPO = "example/infrastructure-playbook"


def make_request(**overrides) -> ByocRequest:
    values = {
        "galaxy_user_id": "f2db41e1fa331b3e",
        "galaxy_username": "alice",
        "username": "alice-hpc",
        "password": "AlicePuls4rPassw0rd",
        "max_accepted_cores": 16,
        "max_accepted_mem": 64,
        "min_accepted_gpus": 0,
        "max_accepted_gpus": 1,
    }
    values.update(overrides)
    return ByocRequest(**values)


@pytest.fixture
def vault() -> Vault:
    return Vault(THROWAWAY_VAULT_PASSWORD)


@pytest.fixture
def vault_file(tmp_path) -> Path:
    path = tmp_path / "vault-password"
    path.write_bytes(THROWAWAY_VAULT_PASSWORD + b"\n")
    return path


@pytest.fixture
def repo_dir(tmp_path, vault) -> Path:
    """A plain directory with the five target files."""
    root = tmp_path / "target"
    shutil.copytree(TARGET, root)
    secret = root / SECRETS
    secret.parent.mkdir(parents=True, exist_ok=True)
    secret.write_text(vault.encrypt((FIXTURES / "secret_pulsar_plain.yml").read_text()))
    return root


@pytest.fixture
def texts(repo_dir) -> dict[str, str]:
    return read_repo(repo_dir)


class FakeResponse:
    def __init__(self, status_code: int, payload=None) -> None:
        self.status_code = status_code
        self._payload = payload

    def json(self):
        return self._payload


class FakeSession:
    """Serves the recorded Galaxy responses and records every request."""

    def __init__(self, overrides: dict[str, FakeResponse] | None = None) -> None:
        self.headers: dict[str, str] = {}
        self.calls: list[tuple[str, dict | None]] = []
        self.overrides = overrides or {}

    def get(self, url, params=None, timeout=None):
        assert timeout
        path = urlparse(url).path
        self.calls.append((path, params))
        if path in self.overrides:
            return self.overrides[path]
        parts = path.strip("/").split("/")
        if parts == ["api", "users"]:
            file = GALAXY / "users.json"
        elif len(parts) == 5 and parts[3:] == ["information", "inputs"]:
            file = GALAXY / "information_inputs" / f"{parts[2]}.json"
        elif len(parts) == 3:
            file = GALAXY / "users" / f"{parts[2]}.json"
        else:  # pragma: no cover - a test asked for an unknown endpoint
            raise AssertionError(path)
        if not file.is_file():
            return FakeResponse(404, {"err_msg": "not found"})
        return FakeResponse(200, json.loads(file.read_text()))


@pytest.fixture
def session() -> FakeSession:
    return FakeSession()


def git(*args: str, cwd: Path | None = None) -> str:
    return subprocess.run(
        ["git", *args], cwd=cwd, check=True, capture_output=True, text=True
    ).stdout


@pytest.fixture
def git_env(monkeypatch):
    """Isolate git from the developer's config (hooks, signing) and give it an identity.

    git hooks (pytest runs as a pre-commit hook) export GIT_DIR, GIT_INDEX_FILE, ...; inherited,
    they would make the test repositories' git commands act on this repository instead.
    """
    for name in [name for name in os.environ if name.startswith("GIT_")]:
        monkeypatch.delenv(name)
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", "/dev/null")
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    for kind in ("AUTHOR", "COMMITTER"):
        monkeypatch.setenv(f"GIT_{kind}_NAME", "BYOC Test")
        monkeypatch.setenv(f"GIT_{kind}_EMAIL", "byoc-test@example.invalid")


@pytest.fixture
def origin(tmp_path, repo_dir, git_env) -> Path:
    """Bare repository with the fixture files on ``master``."""
    bare = tmp_path / "origin.git"
    git("init", "--quiet", "--bare", "-b", "master", str(bare))
    git("init", "--quiet", "-b", "master", cwd=repo_dir)
    git("add", ".", cwd=repo_dir)
    git("commit", "--quiet", "-m", "fixture", cwd=repo_dir)
    git("push", "--quiet", str(bare), "master", cwd=repo_dir)
    return bare


class FakeGh:
    """Runner for :class:`scripts.byoc_pr.github.GitHub`: real git, fake ``gh``."""

    def __init__(self, origin: Path) -> None:
        self.origin = origin
        self.calls: list[tuple[list[str], str | None]] = []
        self.open_prs: dict[str, str] = {}
        self.created: list[dict] = []

    def __call__(self, args, *, cwd=None, input=None):
        from scripts.byoc_pr.github import run

        self.calls.append((list(args), input))
        if args[0] != "gh":
            return run(args, cwd=cwd, input=input)
        command = args[1:3]
        if command == ["repo", "clone"]:
            return run(["git", "clone", "--quiet", str(self.origin), args[4]])
        branch = args[args.index("--head") + 1]
        if command == ["pr", "list"]:
            url = self.open_prs.get(branch)
            return json.dumps([{"url": url}] if url else [])
        assert command == ["pr", "create"]
        url = f"https://github.com/{args[args.index('--repo') + 1]}/pull/{len(self.created) + 1}"
        self.open_prs[branch] = url
        self.created.append({"args": list(args), "body": input, "branch": branch})
        return url + "\n"


@pytest.fixture
def fake_gh(origin) -> FakeGh:
    return FakeGh(origin)
