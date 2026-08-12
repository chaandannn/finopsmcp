"""
Read-through TTL cache for cost data.

In the MCP model, an agentic session asks the same cost question several times
while cross-referencing. Without caching, every call re-hits the cloud billing
API, and AWS Cost Explorer bills $0.01 per request, so one conversation can
quietly run up real API cost. This cache makes the first call fetch and every
repeat within the TTL free.

It is passive by design. Entries are populated on read and served until they
expire. There is no timer, no polling, no background refresh. It can only ever
call the underlying API when a caller asks and the stored copy is missing or
stale, so it cannot "call the API every few seconds" on its own.

Cost data (long TTL) is additionally persisted to a small on-disk store, so a
restart, a redeploy, or simply the next day's first query serves the prior fetch
instead of going back to Cost Explorer. Short-lived entries (Kubernetes state)
stay in memory only. Persistence is strictly best-effort: any disk problem
degrades to memory-only, never to wrong data.

TTLs are tuned to how fast the source actually changes:
  - AWS Cost Explorer / CUR refresh about 3x/day at daily granularity, so 12h
    is safe and costs nothing in accuracy
  - Kubernetes cluster state moves faster, so a few minutes
"""
from __future__ import annotations

import hashlib
import hmac
import os
import pickle
import secrets
import sqlite3
import threading
import time
from typing import Any, Awaitable, Callable

# TTLs in seconds, env-overridable.
COST_TTL = int(os.getenv("FINOPS_COST_CACHE_TTL", str(12 * 3600)))  # 12 hours
K8S_TTL = int(os.getenv("FINOPS_K8S_CACHE_TTL", "300"))             # 5 minutes
DEFAULT_TTL = int(os.getenv("FINOPS_CACHE_TTL", "3600"))            # 1 hour

# Bound the store so a long-lived server cannot grow without limit.
_MAX_ENTRIES = int(os.getenv("FINOPS_CACHE_MAX_ENTRIES", "512"))

# Global kill switch to force fresh data (e.g. for debugging or a --no-cache run).
_DISABLED = os.getenv("FINOPS_CACHE_DISABLED", "").lower() in ("1", "true", "yes")

# Entries with a TTL at least this long (cost data, 12h) are also written to disk
# so a restart does not go cold and re-hit the billing API. Short-TTL entries
# (k8s, 5min) are not worth the disk write. Disk persistence can be turned off on
# its own (e.g. a read-only filesystem) without disabling the in-memory cache.
_PERSIST_MIN_TTL = int(os.getenv("FINOPS_CACHE_PERSIST_MIN_TTL", "3600"))  # 1h
_DISK_DISABLED = os.getenv("FINOPS_CACHE_DISK_DISABLED", "").lower() in ("1", "true", "yes")
_disk_ready = False

_lock = threading.Lock()
_store: dict[str, tuple[float, Any]] = {}  # key -> (expires_at, value)


def make_key(*parts: Any) -> str:
    """Build a stable cache key from arbitrary parts."""
    return "|".join(str(p) for p in parts)


def get(key: str) -> Any | None:
    """Return the cached value if present and unexpired, else None.

    Checks the in-memory store first, then the on-disk store, so a freshly
    restarted process serves the prior run's cost data instead of re-hitting the
    billing API. A disk hit repopulates the in-memory store.
    """
    if _DISABLED:
        return None
    now = time.time()
    with _lock:
        item = _store.get(key)
        if item is not None:
            expires_at, value = item
            if now < expires_at:
                return value
            _store.pop(key, None)
    disk = _disk_get(key, now)
    if disk is not None:
        expires_at, value = disk
        with _lock:
            _store[key] = (expires_at, value)
        return value
    return None


def set(key: str, value: Any, ttl: int = DEFAULT_TTL) -> None:
    """Store a value under key for ttl seconds.

    Entries whose TTL is at least ``_PERSIST_MIN_TTL`` (cost data) are also
    written to the on-disk store so they survive a restart; shorter-lived entries
    stay in memory only.
    """
    if _DISABLED:
        return
    now = time.time()
    expires_at = now + ttl
    with _lock:
        if len(_store) >= _MAX_ENTRIES:
            # Drop expired entries first; if still full, drop the soonest-to-expire.
            for k in [k for k, (exp, _) in _store.items() if now >= exp]:
                _store.pop(k, None)
            if len(_store) >= _MAX_ENTRIES:
                _store.pop(min(_store, key=lambda k: _store[k][0]), None)
        _store[key] = (expires_at, value)
    if ttl >= _PERSIST_MIN_TTL:
        _disk_set(key, expires_at, value, now)


def clear() -> None:
    """Drop everything, memory and disk. Used after a fresh snapshot or on refresh."""
    with _lock:
        _store.clear()
    conn = _disk_conn()
    if conn is not None:
        try:
            with conn:
                conn.execute("DELETE FROM kv")
        except Exception:
            pass
        finally:
            try:
                conn.close()
            except Exception:
                pass


async def aget_or_set(key: str, ttl: int, producer: Callable[[], Awaitable[Any]]) -> Any:
    """Async read-through: return the cached value, or run producer() once and
    cache it. On a rare concurrent miss two producers may run, which costs one
    extra API call and is not worth locking the event loop to prevent.
    """
    hit = get(key)
    if hit is not None:
        return hit
    value = await producer()
    set(key, value, ttl)
    return value


# --- On-disk persistence (best-effort) -------------------------------------
#
# A tiny key/value table in the data dir. Cost data is picklable (dataclasses,
# dicts); anything that is not simply fails to persist and stays memory-only. A
# read that cannot be unpickled is treated as a miss, so a stale-format or corrupt
# entry causes a re-fetch, never a wrong answer.


_hmac_key: bytes | None = None
_hmac_key_loaded = False


def _cache_hmac_key() -> bytes | None:
    """Per-install key that authenticates the on-disk pickle blobs. Read from a
    0600 file in the data dir, created once. Returns None if it cannot be
    established, in which case disk persistence is skipped entirely rather than
    writing an unauthenticated pickle a tampered file could weaponize."""
    global _hmac_key, _hmac_key_loaded
    if _hmac_key_loaded:
        return _hmac_key
    _hmac_key_loaded = True
    data_dir = os.getenv("FINOPS_DATA_DIR") or os.path.join(os.path.expanduser("~"), ".nable")
    path = os.path.join(data_dir, "cache.key")
    try:
        os.makedirs(data_dir, exist_ok=True)
        if os.path.exists(path):
            with open(path, "rb") as fh:
                key = fh.read()
            if len(key) >= 32:
                _hmac_key = key
                return _hmac_key
        # Create a fresh key, readable only by the owner.
        key = secrets.token_bytes(32)
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        try:
            os.write(fd, key)
        finally:
            os.close(fd)
        _hmac_key = key
    except Exception:
        _hmac_key = None
    return _hmac_key


def _disk_conn() -> "sqlite3.Connection | None":
    """Open the on-disk cache DB, or None if persistence is off or unavailable."""
    global _disk_ready
    if _DISABLED or _DISK_DISABLED:
        return None
    data_dir = os.getenv("FINOPS_DATA_DIR") or os.path.join(os.path.expanduser("~"), ".nable")
    try:
        os.makedirs(data_dir, exist_ok=True)
        # storage/db.py gives each FINOPS_PROFILE its own database and vault, but
        # this file was keyed only on FINOPS_DATA_DIR, so two profiles pointed at
        # two different customers shared one cache. The profile belongs in the
        # filename for the same reason it belongs in the DB name.
        _profile = (os.getenv("FINOPS_PROFILE") or "").strip()
        _cache_name = f"cache-{_profile}.db" if _profile else "cache.db"
        conn = sqlite3.connect(os.path.join(data_dir, _cache_name), timeout=2.0)
        if not _disk_ready:
            conn.execute(
                "CREATE TABLE IF NOT EXISTS kv "
                "(key TEXT PRIMARY KEY, expires_at REAL, value BLOB)"
            )
            conn.commit()
            _disk_ready = True
        return conn
    except Exception:
        return None


def _disk_get(key: str, now: float) -> "tuple[float, Any] | None":
    """Read an unexpired entry from disk, or None. Corrupt or unreadable entries
    return None (a miss), so the caller re-fetches rather than seeing bad data."""
    conn = _disk_conn()
    if conn is None:
        return None
    try:
        row = conn.execute(
            "SELECT expires_at, value FROM kv WHERE key = ?", (key,)
        ).fetchone()
        if row is None:
            return None
        expires_at, blob = row
        if now >= expires_at:
            with conn:
                conn.execute("DELETE FROM kv WHERE key = ?", (key,))
            return None
        # Authenticate the blob before unpickling. A row is `tag(32) || pickle`.
        # If the tag is missing/wrong (tampered file, or a pre-HMAC legacy entry),
        # treat it as a miss and re-fetch — never feed an unverified pickle to
        # pickle.loads, which would be code execution from a writable cache file.
        hkey = _cache_hmac_key()
        if hkey is None or not isinstance(blob, (bytes, bytearray)) or len(blob) < 32:
            return None
        tag, payload = bytes(blob[:32]), bytes(blob[32:])
        if not hmac.compare_digest(tag, hmac.new(hkey, payload, hashlib.sha256).digest()):
            return None
        return (float(expires_at), pickle.loads(payload))
    except Exception:
        return None
    finally:
        try:
            conn.close()
        except Exception:
            pass


def _disk_set(key: str, expires_at: float, value: Any, now: float) -> None:
    """Persist an entry, pruning anything already expired. Best-effort: any error
    (unpicklable value, locked DB, read-only disk) is swallowed and leaves the
    in-memory cache intact."""
    conn = _disk_conn()
    if conn is None:
        return
    # No key means we cannot authenticate what we write, so we do not persist an
    # unsigned pickle (it would be trusted on read). Degrade to memory-only.
    hkey = _cache_hmac_key()
    if hkey is None:
        return
    try:
        payload = pickle.dumps(value, protocol=pickle.HIGHEST_PROTOCOL)
    except Exception:
        return
    blob = hmac.new(hkey, payload, hashlib.sha256).digest() + payload
    try:
        with conn:
            conn.execute("DELETE FROM kv WHERE expires_at <= ?", (now,))
            conn.execute(
                "INSERT OR REPLACE INTO kv (key, expires_at, value) VALUES (?, ?, ?)",
                (key, expires_at, sqlite3.Binary(blob)),
            )
    except Exception:
        pass
    finally:
        try:
            conn.close()
        except Exception:
            pass
