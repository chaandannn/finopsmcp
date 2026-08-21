# SPDX-License-Identifier: Apache-2.0
"""
Role-Based Access Control for nable (finops-mcp).

Three roles (hierarchical — each includes all permissions below it):
  viewer    Read-only cost queries. Optionally scoped to one team or provider.
  analyst   viewer + run attribution, trigger snapshots, create/view budgets.
  admin     Full access — manage API keys, connectors, org sync, all writes.

────────────────────────────────────────────────────────────────────────────────
Single-user / SQLite mode (default)
  No key required. All requests run as an implicit admin.
  RBAC is fully opt-in — existing solo setups are unaffected.

Shared / Postgres team mode
  Set FINOPS_REQUIRE_AUTH=1 to enforce key checks.
  Each engineer gets a named key:
    finops key create --name "Alice" --role analyst --team platform

Key format:   nbl_{32 random hex chars}
Stored as:    SHA-256(raw_key) in the api_keys table — raw key shown once at creation.
────────────────────────────────────────────────────────────────────────────────

Usage in an MCP tool handler:

    from finops.auth.rbac import require_role, current_identity

    def my_tool(args):
        ident = current_identity()           # None in permissive (single-user) mode
        if err := require_role("analyst", ident):
            return err
        team_filter = ident.scope_team if ident else None
        # pass team_filter into queries to enforce scope
        ...
"""
from __future__ import annotations

import hashlib
import logging
import os
import secrets
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Optional

log = logging.getLogger("finops.auth.rbac")

# ── Role hierarchy ─────────────────────────────────────────────────────────────
# Indexed so we can compare: ROLE_LEVEL["analyst"] >= ROLE_LEVEL["viewer"]

ROLES = ("viewer", "analyst", "admin")
ROLE_LEVEL: dict[str, int] = {r: i for i, r in enumerate(ROLES)}

# What each role can do — used for descriptive error messages only.
ROLE_CAPS: dict[str, list[str]] = {
    "viewer": [
        "read cost queries", "view anomalies", "view budgets",
        "view rightsizing recommendations", "view K8s costs",
    ],
    "analyst": [
        "everything in viewer",
        "run attribution", "trigger snapshots", "create/update budgets",
        "acknowledge anomalies",
    ],
    "admin": [
        "everything in analyst",
        "manage API keys", "manage connectors", "org sync",
        "delete budgets", "send digests",
    ],
}


# ── Identity ───────────────────────────────────────────────────────────────────

@dataclass
class Identity:
    key_id: int
    name: str
    email: str
    role: str                    # "viewer" | "analyst" | "admin"
    scope_team: str | None       # None = unrestricted
    scope_provider: str | None   # None = unrestricted

    @property
    def level(self) -> int:
        return ROLE_LEVEL.get(self.role, 0)

    def has_role(self, min_role: str) -> bool:
        return self.level >= ROLE_LEVEL.get(min_role, 999)

    def as_dict(self) -> dict[str, Any]:
        return {
            "key_id": self.key_id,
            "name": self.name,
            "email": self.email,
            "role": self.role,
            "scope_team": self.scope_team,
            "scope_provider": self.scope_provider,
        }


# ── Async-safe identity context ────────────────────────────────────────────────
# ContextVar is the correct primitive for asyncio: each task gets its own copy,
# so concurrent coroutines sharing a thread cannot bleed identity into each other.

_identity: ContextVar[Optional[Identity]] = ContextVar("_identity", default=None)


def set_current_identity(ident: Identity | None) -> None:
    """Attach an identity to the current async task (called by auth middleware)."""
    _identity.set(ident)


def current_identity() -> Identity | None:
    """Return the identity for the current async task, or None if permissive."""
    return _identity.get()


# ── Auth enforcement ───────────────────────────────────────────────────────────

def _auth_required() -> bool:
    """True when shared mode + FINOPS_REQUIRE_AUTH=1."""
    db_url = os.environ.get("DATABASE_URL", "")
    shared = db_url.startswith(("postgresql://", "postgres://", "postgresql+", "postgres+"))
    want_auth = os.environ.get("FINOPS_REQUIRE_AUTH", "0") == "1"
    if want_auth and not shared:
        log.warning(
            "FINOPS_REQUIRE_AUTH=1 is set but DATABASE_URL is not a Postgres URL. "
            "Auth enforcement requires shared Postgres mode. Running in permissive mode."
        )
    return shared and want_auth


def deny_text(err: dict) -> str:
    """Render a guard dict as the sentence a `-> str` tool should return.

    Guards return dicts, and a tool annotated `-> str` cannot emit one: FastMCP
    validates the return against the tool's own output schema and raises
    ToolError. So on seven tools the denial path was DEAD. A viewer in
    shared-Postgres mode who asked for a cost audit got a pydantic traceback
    instead of "this needs the analyst role, ask an admin", and a free user who
    tripped require_pro got a crash where the upgrade nudge belonged.

    Nothing caught it because the guard is only reachable with an identity
    attached, which single-user mode never does, and because the failure is a
    raised exception in a path no test drove.

    The `message` key is already written for a human, so the sentence is the
    dict's own message. The error code follows it because
    tests and log greps key on it and a caller cannot see it otherwise.
    """
    msg = str(err.get("message") or "").strip()
    code = str(err.get("error") or "").strip()
    if msg and code:
        return f"{msg} [{code}]"
    return msg or code or str(err)


def require_role(min_role: str, ident: Identity | None = None) -> dict | None:
    """
    Gate a tool call by minimum role. Returns an error dict if denied, None if allowed.

    In permissive (single-user) mode this always returns None.
    Pass `ident` explicitly or let it fall back to current_identity().

    Usage, and the annotation decides which form:
        async def t(...) -> dict:
            if err := require_role("analyst"):
                return err
        async def t(...) -> str:
            if err := require_role("analyst"):
                return deny_text(err)

    `return err` from a `-> str` tool raises ToolError at dispatch instead of
    answering the user. This docstring used to show only the dict form, which is
    how seven `-> str` tools came to have a dead denial path.
    """
    ident = ident or current_identity()

    if not _auth_required():
        # Permissive (single-user / SQLite) mode. With no identity attached the box
        # owner runs wide open, exactly as before: the local CLI and password/SSO
        # dashboard sessions never attach one. But when a caller HAS deliberately
        # attached an identity for this call (e.g. a control-plane analyst session
        # driving the dashboard agent), honor its role ceiling so a non-admin cannot
        # run admin-only tools even on a single-tenant box.
        if ident is None:
            return None
        # fall through to the role check below
    elif ident is None:
        return {
            "error": "auth_required",
            "message": (
                "This finops server requires authentication. "
                "Set FINOPS_API_KEY=nbl_... or ask your admin to create a key: "
                "finops key create --name you@company.com --role viewer"
            ),
        }

    if not ident.has_role(min_role):
        caps = ROLE_CAPS.get(min_role, [])
        return {
            "error": "insufficient_role",
            "message": (
                f"This action requires the '{min_role}' role. "
                f"You are '{ident.role}' ({ident.name}). "
                f"Ask an admin to upgrade your key."
            ),
            "required_role": min_role,
            "your_role": ident.role,
            "required_capabilities": caps,
        }

    return None


def enforce_team_scope(team: str | None, ident: Identity | None = None) -> str | None:
    """
    Return the effective team filter to use in a query.

    If the caller has a scope_team restriction, that wins regardless of what
    `team` argument the tool received. Admins and unscoped identities pass
    through the requested `team` unchanged.
    """
    ident = ident or current_identity()
    if ident and ident.scope_team:
        return ident.scope_team      # enforced: can only see their own team
    return team                       # unrestricted: use whatever was requested


def enforce_provider_scope(provider: str | None, ident: Identity | None = None) -> str | None:
    """Return the effective provider filter, enforcing any scope restriction."""
    ident = ident or current_identity()
    if ident and ident.scope_provider:
        return ident.scope_provider
    return provider


# ── Key crypto ─────────────────────────────────────────────────────────────────

def _raw_to_hash(raw_key: str) -> str:
    return hashlib.sha256(raw_key.encode()).hexdigest()


def _new_raw_key() -> str:
    return "nbl_" + secrets.token_hex(32)


# ── Key management ─────────────────────────────────────────────────────────────

def create_key(
    name: str,
    role: str = "viewer",
    email: str = "",
    scope_team: str | None = None,
    scope_provider: str | None = None,
    created_by: str = "admin",
) -> dict[str, Any]:
    """
    Create a new API key. Returns the raw key — shown ONCE, not stored.

    Example:
        result = create_key("Alice", role="analyst", scope_team="platform")
        print(result["key"])   # nbl_a3f1...  — save this, it won't appear again
    """
    if role not in ROLE_LEVEL:
        return {"error": f"Unknown role '{role}'. Must be one of: {', '.join(ROLES)}"}

    from ..storage.db import api_keys, get_engine
    from sqlalchemy import insert

    raw = _new_raw_key()
    key_hash = _raw_to_hash(raw)
    now = datetime.now(timezone.utc)

    with get_engine().begin() as conn:
        result = conn.execute(insert(api_keys).values(
            key_hash=key_hash,
            name=name,
            email=email,
            role=role,
            scope_team=scope_team,
            scope_provider=scope_provider,
            created_at=now,
            last_used_at=None,
            created_by=created_by,
            is_active=True,
        ))
        key_id = result.inserted_primary_key[0]

    log.info("API key created: id=%d name=%r role=%s scope_team=%s", key_id, name, role, scope_team)

    return {
        "id": key_id,
        "key": raw,                  # raw key — shown once
        "name": name,
        "email": email,
        "role": role,
        "scope_team": scope_team,
        "scope_provider": scope_provider,
        "warning": "Save this key now — it will not be shown again.",
    }


def validate_key(raw_key: str) -> Identity | None:
    """
    Look up a raw key. Returns an Identity if found and active, None otherwise.
    Also stamps last_used_at.
    """
    from ..storage.db import api_keys, get_engine
    from sqlalchemy import select, update

    key_hash = _raw_to_hash(raw_key)

    with get_engine().begin() as conn:
        row = conn.execute(
            select(api_keys).where(
                api_keys.c.key_hash == key_hash,
                api_keys.c.is_active == True,
            )
        ).fetchone()

        if row is None:
            return None

        # Stamp last_used_at in the same transaction
        conn.execute(
            update(api_keys)
            .where(api_keys.c.id == row.id)
            .values(last_used_at=datetime.now(timezone.utc))
        )

    return Identity(
        key_id=row.id,
        name=row.name,
        email=row.email,
        role=row.role,
        scope_team=row.scope_team,
        scope_provider=row.scope_provider,
    )


def resolve_identity_from_env() -> Identity | None:
    """
    Read FINOPS_API_KEY from the environment and resolve it to an Identity.
    Called once per MCP server startup (or per-request in strict mode).
    Returns None in permissive single-user mode.
    """
    raw = os.environ.get("FINOPS_API_KEY", "").strip()
    if not raw:
        if _auth_required():
            log.warning("FINOPS_REQUIRE_AUTH=1 but no FINOPS_API_KEY set — all requests will be rejected")
        return None

    ident = validate_key(raw)
    if ident is None:
        log.error("FINOPS_API_KEY is set but did not match any active key — check the key or re-create it")
        return None

    log.info("Authenticated as %r (role=%s scope_team=%s)", ident.name, ident.role, ident.scope_team)
    return ident


def list_keys(include_inactive: bool = False) -> list[dict[str, Any]]:
    """List all API keys (without the raw key — it's never stored)."""
    from ..storage.db import api_keys, get_engine
    from sqlalchemy import select

    q = select(
        api_keys.c.id, api_keys.c.name, api_keys.c.email,
        api_keys.c.role, api_keys.c.scope_team, api_keys.c.scope_provider,
        api_keys.c.created_at, api_keys.c.last_used_at,
        api_keys.c.created_by, api_keys.c.is_active,
    )
    if not include_inactive:
        q = q.where(api_keys.c.is_active == True)
    q = q.order_by(api_keys.c.created_at)

    with get_engine().connect() as conn:
        rows = conn.execute(q).fetchall()

    return [dict(r._mapping) for r in rows]


def revoke_key(key_id: int) -> bool:
    """Soft-delete an API key by ID. Returns True if found and deactivated."""
    from ..storage.db import api_keys, get_engine
    from sqlalchemy import update

    with get_engine().begin() as conn:
        result = conn.execute(
            update(api_keys)
            .where(api_keys.c.id == key_id)
            .values(is_active=False)
        )
    return result.rowcount > 0


# ── Audit log ──────────────────────────────────────────────────────────────────

def audit(operation: str, key_name: str, detail: str | None = None) -> None:
    """
    Write an entry to the audit_log table. Non-fatal — errors are logged, not raised.
    Call this from any tool that modifies state (budget writes, key management, etc.).
    """
    import os
    from ..storage.db import audit_log, get_engine
    from sqlalchemy import insert

    try:
        now = datetime.now(timezone.utc)
        with get_engine().begin() as conn:
            conn.execute(insert(audit_log).values(
                ts=now,
                operation=operation,
                key_name=key_name,
                client_pid=os.getpid(),
                client_user=os.environ.get("USER", ""),
                detail=detail,
            ))
    except Exception as e:
        log.warning("audit log write failed: %s", e)
