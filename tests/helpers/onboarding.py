"""Apply and load EU-layout BYOC onboarding files like the playbook does (issue #21).

The contract tests copy the repository's onboarding files, insert the lines an onboarding
pull request adds (taken verbatim from usegalaxy-eu/infrastructure-playbook #872 and #1384)
at the place usegalaxy.eu's PRs put them, and load the result like
tasks/byoc_onboarding.yml does.
"""

import re
import shutil
import subprocess
import sys
from pathlib import Path

import yaml
from ansible.parsing.dataloader import DataLoader
from ansible.parsing.vault import VaultLib, VaultSecret

REPO_ROOT = Path(__file__).resolve().parents[2]

# include_vars target variable -> file, as in tasks/byoc_onboarding.yml.
FILES = {
    "_galaxy_onboarding_mq": "group_vars/mq.yml",
    "_galaxy_onboarding_jobconf": "templates/galaxy/config/job_conf.yml",
    "_galaxy_onboarding_preferences": "files/galaxy/config/user_preferences_extra_conf.yml",
}
TPV_DESTINATIONS = "files/galaxy/tpv/destinations.yml.j2"
SECRETS_DIR = "secret_group_vars"
SECRETS_FILE = f"{SECRETS_DIR}/pulsar.yml"
VAULT_HEADER = b"$ANSIBLE_VAULT;"

# Onboarding PRs insert their lines after the last entry above these lines (usegalaxy.eu:
# `ega_account:`, `# LOCAL CONDOR DESTINATIONS #` banner, `rabbitmq_plugins:`,
# `default_destination:`).
ANCHORS = {
    FILES["_galaxy_onboarding_preferences"]: r"    # --- end of remote_resources options",
    TPV_DESTINATIONS: r"  #+\n  # LOCAL CONDOR DESTINATIONS #",
    FILES["_galaxy_onboarding_mq"]: r"# --- end of rabbitmq_users",
    FILES["_galaxy_onboarding_jobconf"]: r"  default_destination:",
}
# Snippet file name (in a fixture directory) -> repository file.
SNIPPETS = {Path(path).name: path for path in ANCHORS}


def copy_repository_files(dest: Path) -> Path:
    """Copy the repository's onboarding files (and the rest of files/galaxy/tpv/) to ``dest``."""
    for path in [*FILES.values(), SECRETS_DIR]:
        source = REPO_ROOT / path
        target = dest / path
        target.parent.mkdir(parents=True, exist_ok=True)
        if source.is_dir():
            shutil.copytree(source, target)
        else:
            shutil.copy(source, target)
    shutil.copytree(REPO_ROOT / "files/galaxy/tpv", dest / "files/galaxy/tpv")
    return dest


def insert_before_anchor(text: str, anchor: str, lines: str) -> str:
    """Insert ``lines`` right after the last non-blank line before ``anchor``."""
    match = re.search(f"^{anchor}", text, flags=re.MULTILINE)
    if match is None:
        raise ValueError(f"anchor {anchor!r} not found")
    head = text[: match.start()]
    body = head.rstrip("\n")
    return body + "\n" + lines + head[len(body) + 1 :] + text[match.start() :]


def onboard(root: Path, snippets: Path, secrets: dict, password_file: Path) -> None:
    """Add one endpoint to the files under ``root``, as an onboarding PR does.

    ``snippets`` holds the lines the PR adds, one file per target file; ``secrets`` are the
    variables it adds to the Vault file (created if missing, as by the first onboarding).
    """
    for snippet in sorted(snippets.iterdir()):
        path = SNIPPETS[snippet.name]
        target = root / path
        target.write_text(
            insert_before_anchor(target.read_text(), ANCHORS[path], snippet.read_text())
        )
    add_secrets(root / SECRETS_FILE, secrets, password_file)


def _vault(password_file: Path) -> VaultLib:
    secret = VaultSecret(password_file.read_bytes().strip())
    return VaultLib([("default", secret)])


def add_secrets(path: Path, secrets: dict, password_file: Path) -> None:
    """Add ``secrets`` to the Vault file ``path`` (like ``ansible-vault create``/``edit``)."""
    vault = _vault(password_file)
    current = yaml.safe_load(vault.decrypt(path.read_bytes())) if path.exists() else {}
    content = yaml.safe_dump({**current, **secrets}, default_flow_style=False)
    path.write_bytes(vault.encrypt(content))


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


class _DuplicateKeyLoader(yaml.SafeLoader):
    """A SafeLoader that records mapping keys defined twice (PyYAML keeps the last one)."""

    duplicates: list


def _construct_mapping(loader, node, deep=False):
    keys = [loader.construct_object(key, deep=deep) for key, _ in node.value]
    loader.duplicates.extend(key for i, key in enumerate(keys) if key in keys[:i])
    return yaml.SafeLoader.construct_mapping(loader, node, deep=deep)


_DuplicateKeyLoader.add_constructor(
    yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _construct_mapping
)


def duplicate_keys(text: str) -> list:
    """Mapping keys that appear twice in the same mapping of the YAML ``text``."""
    loader = _DuplicateKeyLoader(text)
    loader.duplicates = []
    try:
        loader.get_single_data()
    finally:
        loader.dispose()
    return loader.duplicates
