"""
Immutable audit log for GovCloud compliance readiness.

Writes append-only JSONL to:
  ~/.finops/audit.log                         (solo mode)
  ~/.finops/profiles/{profile}/audit.log      (if FINOPS_PROFILE is set)

Disable with: FINOPS_NO_AUDIT=1
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

_lock = threading.Lock()
_logger_instance: "AuditLogger | None" = None


def _audit_log_path() -> Path:
    profile = os.environ.get("FINOPS_PROFILE", "").strip()
    base = Path.home() / ".finops"
    if profile:
        return base / "profiles" / profile / "audit.log"
    return base / "audit.log"


def _hash_key(raw_key: str) -> str:
    """Return a short SHA-256 prefix to identify the key without exposing it."""
    return hashlib.sha256(raw_key.encode()).hexdigest()[:16]


class AuditLogger:
    """
    Lightweight append-only JSONL audit logger.

    Each record:
      {
        "ts": "2026-05-30T21:00:00Z",
        "tool": "get_cost_summary",
        "account": "123456789012" | null,
        "duration_ms": 234,
        "outcome": "success" | "error" | "denied",
        "user_identity": "local" | "<sha256-prefix>",
        "error": "..."    // only on outcome=error
      }
    """

    def __init__(self) -> None:
        self._disabled = bool(os.environ.get("FINOPS_NO_AUDIT", "").strip())
        self._path = _audit_log_path()

    def _resolve_identity(self) -> str:
        """
        Return "local" in solo mode.
        In team mode (FINOPS_REQUIRE_AUTH=1), hash the API key from env.
        Never log the raw key.
        """
        raw = os.environ.get("FINOPS_API_KEY", "").strip()
        if raw:
            return _hash_key(raw)
        return "local"

    def log_tool_call(
        self,
        tool: str,
        duration_ms: int,
        outcome: str,
        account: str | None = None,
        error: str | None = None,
        user_identity: str | None = None,
    ) -> None:
        """
        Append one audit record. Never raises: log failures are warnings only.

        Args:
            tool: MCP tool name
            duration_ms: elapsed time in milliseconds
            outcome: "success" | "error" | "denied"
            account: cloud account ID accessed, or None
            error: error message when outcome="error"
            user_identity: override identity string (defaults to auto-resolved)
        """
        if self._disabled:
            return

        identity = user_identity or self._resolve_identity()
        record: dict[str, Any] = {
            "ts": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "tool": tool,
            "account": account,
            "duration_ms": duration_ms,
            "outcome": outcome,
            "user_identity": identity,
        }
        if error is not None:
            record["error"] = error

        self._write(record)

    def _write(self, record: dict) -> None:
        try:
            with _lock:
                self._path.parent.mkdir(parents=True, exist_ok=True)
                with open(self._path, "a", encoding="utf-8") as f:
                    f.write(json.dumps(record, ensure_ascii=False) + "\n")
        except Exception as exc:
            log.warning("audit log write failed: %s", exc)


def get_audit_logger() -> AuditLogger:
    """Return the process-level singleton AuditLogger."""
    global _logger_instance
    if _logger_instance is None:
        with _lock:
            if _logger_instance is None:
                _logger_instance = AuditLogger()
    return _logger_instance
