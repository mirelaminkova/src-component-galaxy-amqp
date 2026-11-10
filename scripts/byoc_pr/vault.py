"""Ansible Vault encryption in memory (no plaintext temp files, nothing on a command line)."""

from __future__ import annotations

from pathlib import Path

from ansible.errors import AnsibleError
from ansible.parsing.vault import VaultLib, VaultSecret, is_encrypted


class VaultError(RuntimeError):
    """The vault password is missing or does not open the file."""


class Vault:
    def __init__(self, password: bytes) -> None:
        if not password:
            raise VaultError("the vault password is empty")
        self._lib = VaultLib([("default", VaultSecret(password))])

    @classmethod
    def from_file(cls, path: Path) -> Vault:
        """Read the password like ``ansible-vault --vault-password-file`` (whitespace stripped)."""
        try:
            data = Path(path).read_bytes()
        except OSError as exc:
            raise VaultError(f"cannot read vault password file {path}: {exc.strerror}") from None
        return cls(data.strip())

    @staticmethod
    def is_encrypted(text: str) -> bool:
        return is_encrypted(text)

    def encrypt(self, plaintext: str) -> str:
        return self._lib.encrypt(plaintext.encode()).decode()

    def decrypt(self, vaulttext: str) -> str:
        try:
            return self._lib.decrypt(vaulttext.encode()).decode()
        except AnsibleError:
            raise VaultError("cannot decrypt the vault file: wrong vault password?") from None
