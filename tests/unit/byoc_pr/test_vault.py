import pytest

from scripts.byoc_pr.vault import Vault, VaultError


def test_round_trip(vault):
    vaulttext = vault.encrypt("a: 1\n")
    assert vaulttext.startswith("$ANSIBLE_VAULT;1.1;AES256\n")
    assert vaulttext.endswith("\n")
    assert Vault.is_encrypted(vaulttext)
    assert not Vault.is_encrypted("a: 1\n")
    assert vault.decrypt(vaulttext) == "a: 1\n"


def test_from_file_strips_whitespace(vault_file, vault):
    assert Vault.from_file(vault_file).decrypt(vault.encrypt("x\n")) == "x\n"


def test_wrong_password(vault):
    with pytest.raises(VaultError, match="wrong vault password"):
        Vault(b"another-password").decrypt(vault.encrypt("x\n"))


def test_missing_or_empty_password_file(tmp_path):
    with pytest.raises(VaultError, match="cannot read vault password file"):
        Vault.from_file(tmp_path / "missing")
    (tmp_path / "empty").write_text("\n")
    with pytest.raises(VaultError, match="empty"):
        Vault.from_file(tmp_path / "empty")
