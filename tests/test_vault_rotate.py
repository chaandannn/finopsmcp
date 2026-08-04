"""`finops setup vault rotate` must not destroy every stored credential.

The bug: rotate_key() re-encrypted every row with a new key, then persisted the
new key with

    if not Vault._save_keyring(new_key):
        key_path.write_bytes(new_key)

so the key FILE was written only when the keyring write FAILED. But
Vault.default() reads the file *before* the keyring (priority 2 vs 3 — the file
outranks the keyring deliberately, so macOS users are not prompted on every
uvx-created interpreter). On any machine with a working keyring, which is the
normal case, rotation left the stale old key in vault.key, and the next
Vault.default() picked it up and could no longer decrypt anything.

Vault.default() step 4 already had the correct rule for a freshly generated key:
write the file unless the keyring saved AND keychain-only mode is on. Rotation
reimplemented persistence instead of sharing it, and got it backwards. The fix
gives both one implementation.
"""
from __future__ import annotations

import base64

import pytest

from finops.security.vault import Vault, VaultError


@pytest.fixture
def isolated_vault(tmp_path, monkeypatch):
    """A vault rooted in tmp_path with the OS keyring fully simulated."""
    monkeypatch.setenv("FINOPS_DATA_DIR", str(tmp_path))
    monkeypatch.delenv("FINOPS_VAULT_KEY", raising=False)
    monkeypatch.delenv("FINOPS_VAULT_KEYCHAIN_ONLY", raising=False)
    monkeypatch.delenv("FINOPS_PROFILE", raising=False)
    Vault._key_cache.clear()

    keyring_store: dict = {}

    def fake_save(key: bytes) -> bool:
        keyring_store["key"] = key
        return True                      # a working keyring: the common case

    monkeypatch.setattr(Vault, "_save_keyring", staticmethod(fake_save))
    monkeypatch.setattr(Vault, "_try_keyring", staticmethod(lambda: keyring_store.get("key")))
    yield tmp_path, keyring_store
    Vault._key_cache.clear()


def _rotate(monkeypatch) -> None:
    """Drive the real CLI entry point, not a reimplementation of it."""
    from finops import setup_wizard

    monkeypatch.setattr(setup_wizard, "_prompt", lambda *a, **k: "rotate")
    setup_wizard.vault_rotate()


def test_rotation_leaves_every_credential_readable(isolated_vault, monkeypatch):
    """The regression, end to end: store, rotate, read it back in a fresh Vault.

    This is the whole bug. It fails on the old code with VaultError because
    vault.key still holds the pre-rotation key."""
    tmp_path, _ = isolated_vault

    Vault.default().store("AWS_SECRET_ACCESS_KEY", "s3cret-value")

    _rotate(monkeypatch)

    Vault._key_cache.clear()             # force a real re-resolve, as a new process would
    assert Vault.default().get("AWS_SECRET_ACCESS_KEY") == "s3cret-value"


def test_the_key_file_holds_the_post_rotation_key(isolated_vault, monkeypatch):
    """The precise mechanism, so a future refactor that reintroduces the
    inversion fails here with a clear reason rather than a decrypt error."""
    tmp_path, keyring_store = isolated_vault

    Vault.default().store("K", "v")
    before = (tmp_path / "vault.key").read_bytes()

    _rotate(monkeypatch)

    after = (tmp_path / "vault.key").read_bytes()
    assert after != before, "vault.key still holds the pre-rotation key"
    assert after == keyring_store["key"], "file and keyring disagree after rotation"


def test_keychain_only_mode_does_not_write_the_key_to_disk(isolated_vault, monkeypatch):
    """The one case where skipping the file is correct: the user opted out of
    on-disk keys, and default() reads the keyring first in that mode."""
    tmp_path, keyring_store = isolated_vault
    monkeypatch.setenv("FINOPS_VAULT_KEYCHAIN_ONLY", "1")

    Vault.default().store("K", "v")
    _rotate(monkeypatch)

    assert not (tmp_path / "vault.key").exists(), "keychain-only must not persist to disk"
    Vault._key_cache.clear()
    assert Vault.default().get("K") == "v"


def test_a_failed_keyring_still_persists_the_key_to_disk(isolated_vault, monkeypatch):
    """The case the original code handled. Keep it working: with no keyring,
    the file is the only place the new key can live."""
    tmp_path, _ = isolated_vault
    Vault.default().store("K", "v")

    monkeypatch.setattr(Vault, "_save_keyring", staticmethod(lambda key: False))
    monkeypatch.setattr(Vault, "_try_keyring", staticmethod(lambda: None))
    _rotate(monkeypatch)

    Vault._key_cache.clear()
    assert Vault.default().get("K") == "v"


def test_a_failed_rotation_leaves_the_old_key_in_place(isolated_vault, monkeypatch):
    """Rotation is transactional. If re-encryption fails, the key must NOT be
    replaced, or the failure itself becomes the data loss."""
    tmp_path, _ = isolated_vault
    Vault.default().store("K", "v")
    before = (tmp_path / "vault.key").read_bytes()

    def boom(self, new_key):
        raise RuntimeError("database is locked")

    monkeypatch.setattr(Vault, "rotate_key", boom)
    _rotate(monkeypatch)

    assert (tmp_path / "vault.key").read_bytes() == before
    Vault._key_cache.clear()
    assert Vault.default().get("K") == "v"


def test_rotation_actually_changed_the_encryption(isolated_vault, monkeypatch):
    """Guard against the lazy fix: persisting the OLD key everywhere would make
    every test above pass while rotating nothing."""
    tmp_path, _ = isolated_vault
    v = Vault.default()
    v.store("K", "v")
    before_key = (tmp_path / "vault.key").read_bytes()

    import sqlite3
    con = sqlite3.connect(str(tmp_path / "vault.db"))
    before_blob = con.execute(
        "SELECT encrypted_value FROM credentials WHERE key_name='K'").fetchone()[0]
    con.close()

    _rotate(monkeypatch)

    con = sqlite3.connect(str(tmp_path / "vault.db"))
    after_blob = con.execute(
        "SELECT encrypted_value FROM credentials WHERE key_name='K'").fetchone()[0]
    con.close()
    assert after_blob != before_blob, "ciphertext unchanged: nothing was re-encrypted"
    assert (tmp_path / "vault.key").read_bytes() != before_key
    # And the new key is a valid Fernet key, not a truncated write.
    assert len(base64.urlsafe_b64decode((tmp_path / "vault.key").read_bytes())) == 32
