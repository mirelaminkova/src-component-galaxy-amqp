"""Load the EU-layout BYOC onboarding files like tasks/byoc_onboarding.yml does (issue #21).

The contract tests copy a set of onboarding files, apply onboarding diffs to them (as an
onboarding pull request would), and load the result with these helpers.
"""

import shutil
import subprocess
import sys
from pathlib import Path

from ansible.parsing.dataloader import DataLoader

REPO_ROOT = Path(__file__).resolve().parents[2]

# include_vars target variable -> file, as in tasks/byoc_onboarding.yml.
FILES = {
    "_galaxy_onboarding_mq": "group_vars/mq.yml",
    "_galaxy_onboarding_jobconf": "templates/galaxy/config/job_conf.yml",
    "_galaxy_onboarding_preferences": "files/galaxy/config/user_preferences_extra_conf.yml",
}
SECRETS_DIR = "secret_group_vars"
VAULT_HEADER = b"$ANSIBLE_VAULT;"


TPV_DIR = "files/galaxy/tpv"
TPV_DESTINATIONS = f"{TPV_DIR}/destinations.yml.j2"


def apply_diffs(source: Path, diffs: list[Path], dest: Path, onboarded_destinations: Path) -> Path:
    """Copy ``source`` to ``dest`` and apply each unified diff with ``git apply``.

    The repository's TPV files are copied too, with ``onboarded_destinations`` (the
    destinations ``source`` has already onboarded) appended to destinations.yml.j2, so the
    diffs' TPV hunks apply at the end of the onboarding section whatever the repository holds.
    """
    shutil.copytree(source, dest)
    shutil.copytree(REPO_ROOT / TPV_DIR, dest / TPV_DIR)
    with (dest / TPV_DESTINATIONS).open("a") as destinations:
        destinations.write(onboarded_destinations.read_text())
    for diff in diffs:
        subprocess.run(  # noqa: S603 - fixed command, paths from the test suite
            ["git", "apply", "--whitespace=error-all", str(diff)],  # noqa: S607
            cwd=dest,
            check=True,
            capture_output=True,
        )
    return dest


def load_files(root: Path) -> dict:
    """Return the include_vars variables of tasks/byoc_onboarding.yml for files under ``root``."""
    loader = DataLoader()
    return {name: loader.load_from_file(str(root / path)) for name, path in FILES.items()}


def secret_files(root: Path) -> list[Path]:
    """``secret_group_vars/*.yml`` under ``root``, sorted like the playbook's fileglob."""
    return sorted((root / SECRETS_DIR).glob("*.yml"))


def decrypt_command(password_file: Path, path: Path) -> list[str]:
    """The command tasks/byoc_onboarding.yml runs, with this interpreter."""
    return [
        sys.executable,
        "-m",
        "ansible.cli.vault",
        "view",
        "--vault-password-file",
        str(password_file),
        str(path),
    ]


def decrypt_secrets(root: Path, password_file: Path) -> dict:
    """Decrypt and merge ``secret_group_vars/*.yml`` under ``root`` like the playbook."""
    secrets = {}
    for path in secret_files(root):
        result = subprocess.run(  # noqa: S603 - fixed command, paths from the test suite
            decrypt_command(password_file, path), check=True, capture_output=True, text=True
        )
        secrets.update(DataLoader().load(result.stdout) or {})
    return secrets


def plaintext_secret_files(root: Path) -> list[str]:
    """Files under ``secret_group_vars/`` that are not Ansible Vault files."""
    directory = root / SECRETS_DIR
    paths = sorted(p for p in directory.rglob("*") if p.is_file()) if directory.is_dir() else []
    return [
        str(path.relative_to(root))
        for path in paths
        if path.name != ".gitkeep" and not path.read_bytes().startswith(VAULT_HEADER)
    ]
