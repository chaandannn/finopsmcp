"""
Encrypted credential vault using Fernet symmetric encryption.

- Master key is stored in the OS keyring (preferred) or in a 0600 key file
- All credentials are encrypted at rest in a SQLite database
- Every read/write is appended to an audit log (key names only, never values)
- The vault DB and key file are chmod 600 on creation
"""
from __future__ import annotations

import base64
import getpass
import logging
import os
import stat
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

log = logging.getLogger("finops.vault")

_KEYRING_SERVICE_DEFAULT = "finops-mcp"
_KEYRING_USER = "master-key"


def _active_profile() -> str:
    """Return the active profile name from FINOPS_PROFILE env var, or empty string."""
    return os.environ.get("FINOPS_PROFILE", "").strip()


def _keyring_service() -> str:
    """Return the keyring service name, scoped to the active profile if set."""
    profile = _active_profile()
    if profile:
        return f"nable-{profile}-mcp"
    return _KEYRING_SERVICE_DEFAULT


def _macos_keychain_missing() -> bool:
    """True on macOS when there is no login keychain to open.

    Calling keyring against a missing login keychain pops a BLOCKING 'a keychain
    cannot be found' modal from the OS before the Python exception is raised, so
    we must avoid the call entirely, not just catch the error. Fall back to the
    file-based key store, which is the default posture anyway. license.py carries
    the identical guard for the trial store."""
    if sys.platform != "darwin":
        return False
    try:
        kc = Path.home() / "Library" / "Keychains"
        return not any(kc.glob("login.keychain*"))
    except Exception:
        return True


def _keyring_disabled() -> bool:
    """FINOPS_NO_KEYRING=1 (or FINOPS_AIRGAP=1) forbids any OS keychain access.

    The keychain here belongs to the macOS user, not to $HOME or
    FINOPS_DATA_DIR, so ephemeral runs (tests, demo scripts, CI on a laptop,
    cold-run checks with a scratch HOME) that miss the key file would otherwise
    fall through to the developer's real keychain: a password prompt per run,
    and on first-run generation a write that clobbers the real recovery key.
    Also disabled when no macOS login keychain exists, to avoid the blocking
    'cannot be found' modal. license.py carries the same gate for the trial
    store."""
    return (
        os.environ.get("FINOPS_NO_KEYRING", "") == "1"
        or os.environ.get("FINOPS_AIRGAP", "") == "1"
        or _macos_keychain_missing()
    )


def _vault_dir() -> Path:
    """Where this vault's db + key live. Profile-aware, so a rotation under
    FINOPS_PROFILE persists beside the db it just re-encrypted."""
    profile = _active_profile()
    if not profile:
        return _data_dir()
    import stat as _stat
    data = Path.home() / ".finops" / "profiles" / profile
    data.mkdir(parents=True, exist_ok=True)
    data.chmod(_stat.S_IRWXU)
    return data


class VaultError(Exception):
    pass


def _data_dir() -> Path:
    raw = os.environ.get("FINOPS_DATA_DIR", "")
    d = Path(raw).expanduser() if raw else Path.home() / ".finops"
    d.mkdir(parents=True, exist_ok=True)
    d.chmod(stat.S_IRWXU)
    return d


def _derive_key(passphrase: str, salt: bytes) -> bytes:
    from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC
    from cryptography.hazmat.primitives import hashes
    kdf = PBKDF2HMAC(algorithm=hashes.SHA256(), length=32, salt=salt, iterations=480_000)
    return base64.urlsafe_b64encode(kdf.derive(passphrase.encode()))


def _machine_id() -> str:
    for path in ["/etc/machine-id", "/var/lib/dbus/machine-id"]:
        try:
            return Path(path).read_text().strip()
        except OSError:
            pass
    import uuid
    return str(uuid.getnode())


class Vault:
    def __init__(self, db_path: Path, key: bytes) -> None:
        self._db_path = db_path
        self._key = key
        self._fernet = self._make_fernet(key)
        self._init_db()
        self._secure_file(db_path)

    @staticmethod
    def _make_fernet(key: bytes):
        from cryptography.fernet import Fernet
        return Fernet(key)

    def _init_db(self) -> None:
        import sqlite3
        con = sqlite3.connect(str(self._db_path))
        try:
            con.execute("""
                CREATE TABLE IF NOT EXISTS credentials (
                    key_name TEXT PRIMARY KEY,
                    encrypted_value BLOB NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
            """)
            con.execute("""
                CREATE TABLE IF NOT EXISTS audit_log (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    ts TEXT NOT NULL,
                    operation TEXT NOT NULL,
                    key_name TEXT NOT NULL,
                    client_pid INTEGER,
                    client_user TEXT
                )
            """)
            con.commit()
        finally:
            con.close()

    @staticmethod
    def _secure_file(path: Path) -> None:
        if path.exists():
            path.chmod(stat.S_IRUSR | stat.S_IWUSR)

    def _audit(self, operation: str, key_name: str) -> None:
        import sqlite3
        try:
            con = sqlite3.connect(str(self._db_path))
            try:
                con.execute(
                    "INSERT INTO audit_log (ts, operation, key_name, client_pid, client_user) VALUES (?,?,?,?,?)",
                    (datetime.now(timezone.utc).isoformat(), operation, key_name, os.getpid(), getpass.getuser()),
                )
                con.commit()
            finally:
                con.close()
        except Exception:
            pass  # audit failure must never block operations

    # ── Public API ────────────────────────────────────────────────────────────

    def store(self, key_name: str, value: str) -> None:
        if not key_name or not value:
            raise VaultError("key_name and value must be non-empty")
        encrypted = self._fernet.encrypt(value.encode())
        now = datetime.now(timezone.utc).isoformat()
        import sqlite3
        con = sqlite3.connect(str(self._db_path))
        try:
            con.execute(
                "INSERT INTO credentials (key_name, encrypted_value, created_at, updated_at) "
                "VALUES (?,?,?,?) ON CONFLICT(key_name) DO UPDATE SET encrypted_value=excluded.encrypted_value, updated_at=excluded.updated_at",
                (key_name, encrypted, now, now),
            )
            con.commit()
        finally:
            con.close()
        self._audit("WRITE", key_name)
        log.debug("Vault: stored %s", key_name)

    def get(self, key_name: str) -> str | None:
        import sqlite3
        con = sqlite3.connect(str(self._db_path))
        try:
            row = con.execute(
                "SELECT encrypted_value FROM credentials WHERE key_name = ?", (key_name,)
            ).fetchone()
        finally:
            con.close()
        if row is None:
            return None
        self._audit("READ", key_name)
        try:
            return self._fernet.decrypt(row[0]).decode()
        except Exception as e:
            # Do not include key_name or the underlying exception in the public
            # message to avoid leaking internal credential names or crypto details.
            log.debug("Vault decrypt failed for key %r: %s", key_name, e)
            raise VaultError("Failed to decrypt credential - check vault key") from e

    def delete(self, key_name: str) -> bool:
        import sqlite3
        con = sqlite3.connect(str(self._db_path))
        try:
            cur = con.execute("DELETE FROM credentials WHERE key_name = ?", (key_name,))
            con.commit()
        finally:
            con.close()
        deleted = cur.rowcount > 0
        if deleted:
            self._audit("DELETE", key_name)
        return deleted

    def list_keys(self) -> list[str]:
        import sqlite3
        con = sqlite3.connect(str(self._db_path))
        try:
            rows = con.execute("SELECT key_name FROM credentials ORDER BY key_name").fetchall()
        finally:
            con.close()
        return [r[0] for r in rows]

    def load_to_env(self) -> int:
        """Decrypt all credentials and set them in os.environ. Returns count loaded."""
        import sqlite3
        con = sqlite3.connect(str(self._db_path))
        try:
            rows = con.execute("SELECT key_name, encrypted_value FROM credentials").fetchall()
        finally:
            con.close()
        count = 0
        for key_name, encrypted_value in rows:
            try:
                value = self._fernet.decrypt(encrypted_value).decode()
                os.environ[key_name] = value
                count += 1
            except Exception:
                log.warning("Vault: could not decrypt %s, skipping", key_name)
        log.debug("Vault: loaded %d credentials into environment", count)
        return count

    def rotate_key(self, new_key: bytes) -> None:
        """
        Re-encrypt all credentials with a new key.
        Uses a single transaction: either all rows are rotated or none are.
        Skips corrupt entries with a warning rather than aborting the whole rotation.
        """
        import sqlite3
        new_fernet = self._make_fernet(new_key)
        con = sqlite3.connect(str(self._db_path))
        try:
            rows = con.execute("SELECT key_name, encrypted_value FROM credentials").fetchall()
            now = datetime.now(timezone.utc).isoformat()
            skipped = 0
            for key_name, enc_val in rows:
                try:
                    plaintext = self._fernet.decrypt(enc_val)
                except Exception:
                    log.warning("Vault: could not decrypt %s during rotation, skipping", key_name)
                    skipped += 1
                    continue
                re_encrypted = new_fernet.encrypt(plaintext)
                con.execute(
                    "UPDATE credentials SET encrypted_value=?, updated_at=? WHERE key_name=?",
                    (re_encrypted, now, key_name),
                )
            con.commit()
        finally:
            con.close()
        self._key = new_key
        self._fernet = new_fernet
        # The cached master key is now stale; force a fresh resolution next time.
        Vault._key_cache.clear()
        rotated = len(rows) - skipped
        log.info("Vault: key rotation complete (%d rotated, %d skipped)", rotated, skipped)

    # ── Factory methods ───────────────────────────────────────────────────────

    # Master key cached per keyring service so the OS keychain is read once per
    # process instead of on every Vault.default() call. Without this, a long-running
    # server re-read the keychain repeatedly and macOS re-prompted the user every
    # few minutes. Only successful reads are cached (a missing item never prompts).
    _key_cache: dict[str, bytes] = {}

    @classmethod
    def _read_key_file(cls, key_path: Path) -> bytes | None:
        """Read the local key file; None if missing or not a plausible Fernet key
        (44 urlsafe-base64 bytes), so a corrupt file falls through to recovery."""
        try:
            if key_path.exists():
                raw = key_path.read_bytes().strip()
                if len(raw) == 44:
                    return raw
                log.warning("Vault: ignoring malformed key file at %s", key_path)
        except OSError:
            pass
        return None

    @classmethod
    def _write_key_file(cls, key_path: Path, key: bytes) -> None:
        """Cache the master key beside the vault DB (chmod 600). Reading a file
        is silent where a keychain read can prompt; the keyring stays the
        durable recovery copy."""
        try:
            key_path.write_bytes(key)
            key_path.chmod(stat.S_IRUSR | stat.S_IWUSR)
        except OSError as e:
            log.debug("Vault: could not write key file: %s", e)

    @classmethod
    def _try_keyring(cls) -> bytes | None:
        if _keyring_disabled():
            return None
        svc = _keyring_service()
        cached = cls._key_cache.get(svc)
        if cached is not None:
            return cached
        try:
            import keyring  # type: ignore[import]
            val = keyring.get_password(svc, _KEYRING_USER)
            if val:
                key = base64.urlsafe_b64decode(val.encode())
                cls._key_cache[svc] = key
                return key
        except Exception:
            pass
        return None

    @classmethod
    def _save_keyring(cls, key: bytes) -> bool:
        if _keyring_disabled():
            return False
        try:
            import keyring  # type: ignore[import]
            svc = _keyring_service()
            keyring.set_password(svc, _KEYRING_USER, base64.urlsafe_b64encode(key).decode())
            cls._key_cache[svc] = key  # warm the cache so the next read skips the keychain
            return True
        except Exception:
            return False

    @classmethod
    def default(cls) -> "Vault":
        """
        Open (or create) the default vault. Key priority:
        1. FINOPS_VAULT_KEY env var (explicit operator config: CI, containers, fleet)
        2. ~/.finops/vault.key file (chmod 600), or profile dir equivalent
        3. OS keyring, re-caching the key to the file for next time
        4. Generate a new key, stored in both the keyring and the file

        The file outranks the keyring deliberately. On macOS, every keychain
        access from an unsigned interpreter can pop a permission dialog, and
        uvx creates a new interpreter per release, so a keychain-first vault
        prompted users on every upgrade. Reading a 0600 file is silent; the
        keyring remains the durable recovery copy (the earlier design already
        fell back to this same file whenever no keyring was available). Set
        FINOPS_VAULT_KEYCHAIN_ONLY=1 to keep the key exclusively in the OS
        keyring and accept the prompts.

        When FINOPS_PROFILE is set, the vault DB lives in
        ~/.finops/profiles/{profile}/vault.db and the keyring service is
        prefixed with "nable-{profile}-".
        """
        data = _vault_dir()
        db_path = data / "vault.db"
        key_path = data / "vault.key"
        keychain_only = os.environ.get("FINOPS_VAULT_KEYCHAIN_ONLY", "") == "1"

        # 1. Env var (base64-encoded Fernet key, for CI/container/fleet use)
        key = None
        raw_env = os.environ.get("FINOPS_VAULT_KEY", "")
        if raw_env:
            key = base64.urlsafe_b64decode(raw_env.encode())

        # 2. Key file (the silent fast path)
        if key is None and not keychain_only:
            key = cls._read_key_file(key_path)

        # 3. OS keyring (recovery store; one prompt at most, then re-cached)
        if key is None:
            key = cls._try_keyring()
            if key is not None and not keychain_only:
                cls._write_key_file(key_path, key)
                log.info("Vault: cached master key to %s", key_path)

        # 4. First run: generate and store in both places
        if key is None:
            from cryptography.fernet import Fernet
            key = Fernet.generate_key()
            cls.persist_master_key(key)

        return cls(db_path, key)

    @classmethod
    def persist_master_key(cls, key: bytes) -> None:
        """Write a master key to exactly the stores default() will read back.

        Shared by first-run generation and by key rotation. Rotation used to
        have its own copy of this rule and had it inverted — it wrote the file
        only when the keyring write FAILED, so on a machine with a working
        keyring the stale old key stayed in vault.key, which default() reads
        first, and every stored credential became undecryptable. One rule, one
        implementation, so the two cannot drift again.
        """
        data = _vault_dir()
        keychain_only = os.environ.get("FINOPS_VAULT_KEYCHAIN_ONLY", "") == "1"
        saved = cls._save_keyring(key)
        # The file is the primary read path. Skip it only when the user asked
        # for keychain-only AND the keychain actually took the key.
        if not saved or not keychain_only:
            cls._write_key_file(data / "vault.key", key)
        log.info("Vault: persisted master key (keyring=%s, file=%s)",
                 saved, not keychain_only or not saved)
