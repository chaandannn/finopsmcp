"""
The lesson ledger: every learned adjustment as a durable, reversible object.

customer_signal() recomputes verdicts live from the recommendations ledger, so
on its own the learning is invisible and silent: a source crosses WARM_FLOOR,
gets suppressed, and nobody can point at when that happened, on what evidence,
or turn it off. This module records each verdict transition (and the approval
floor) as a lesson: the human sentence, the evidence snapshot at the moment it
was learned, and a customer-controlled status.

Three guarantees, in order of importance:

  1. Nothing here mutates detectors or policy. A lesson describes an adjustment
     the signal already computed; rolling it back pins that key to NEUTRAL
     (standard ranking), never to some third behaviour.
  2. A rollback is standing. sync_lessons() never re-creates a lesson for a
     rolled-back key, however loudly the signal argues, until the customer
     restores it. A learning system that re-learns what you told it to forget
     is not under your control.
  3. Every lesson carries its evidence. A row with an empty evidence snapshot
     is a bug, and the tests treat it as one.

Deterministic, single-tenant, propose-only: this reads and writes only this
install's own DB and changes only what the customer sees and in what order.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select

from ..savings_tracker import get_engine
from ...storage.db import learning_lessons

# The floor must move by this fraction before a new floor lesson supersedes the
# old one; re-recording every few-cent drift would bury the real lessons.
_FLOOR_CHANGE_MIN = 0.2

_EVIDENCE_FIELDS = ("acted", "resolved", "dismissed", "business_dismissed",
                    "act_rate", "act_rate_raw", "accuracy", "coverage",
                    "realized_monthly_usd")


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _key_for(entry: dict) -> str:
    if entry.get("bucket"):
        return f"bucket:{entry['source']}|{entry['bucket']}"
    return f"source:{entry['source']}"


def _evidence(entry: dict) -> str:
    return json.dumps({k: entry.get(k) for k in _EVIDENCE_FIELDS})


def _rows(conn, *statuses: str) -> list:
    ll = learning_lessons
    q = select(ll)
    if statuses:
        q = q.where(ll.c.status.in_(statuses))
    return list(conn.execute(q).fetchall())


def rolled_back_keys() -> frozenset[str]:
    """Keys the customer has overruled. The signal pins these to neutral."""
    with get_engine().connect() as conn:
        return frozenset(r.key for r in _rows(conn, "rolled_back"))


def sync_lessons(signal: dict | None = None) -> dict[str, int]:
    """Diff the freshly computed signal against the recorded lessons.

    Records new verdict transitions, confirms unchanged ones (last_confirmed),
    supersedes lessons the signal no longer supports, and never touches a
    rolled-back key. Returns counts for the caller's log line.

    `signal` must be the RAW signal (apply_learned_overrides=False): diffing
    the overridden signal would read every rollback as "the signal changed"
    and supersede the very lesson the customer pinned.
    """
    if signal is None:
        from .signal import customer_signal
        signal = customer_signal(apply_learned_overrides=False)

    now = _now()
    counts = {"new": 0, "confirmed": 0, "superseded": 0, "skipped_rolled_back": 0}
    ll = learning_lessons

    with get_engine().begin() as conn:
        active = {r.key: r for r in _rows(conn, "active")}
        pinned = {r.key for r in _rows(conn, "rolled_back")}

        # Current learned state: every non-neutral verdict entry, keyed. A
        # bucket-level entry only becomes its own lesson when it DISAGREES with
        # its source: "spot is fine in nonprod but not prod" is a lesson,
        # "commitment is suppressed, and also suppressed in its only bucket" is
        # the same lesson twice.
        current: dict[str, dict] = {}
        source_verdicts = {e["source"]: e.get("verdict")
                           for e in signal.get("by_source", [])}
        for entry in signal.get("by_source", []):
            if entry.get("verdict") in ("boost", "suppress"):
                current[_key_for(entry)] = {
                    "kind": "verdict",
                    "to": entry["verdict"],
                    "lesson": entry.get("why", ""),
                    "evidence": _evidence(entry),
                }
        for entry in signal.get("by_bucket", []):
            if (entry.get("verdict") in ("boost", "suppress")
                    and entry["verdict"] != source_verdicts.get(entry["source"])):
                current[_key_for(entry)] = {
                    "kind": "verdict",
                    "to": entry["verdict"],
                    "lesson": entry.get("why", ""),
                    "evidence": _evidence(entry),
                }
        floor = (signal.get("approval_profile") or {}).get("approval_floor_usd")
        if floor is not None:
            profile = signal["approval_profile"]
            current["approval_floor"] = {
                "kind": "approval_floor",
                "to": "floor",
                "lesson": (f"Recommendations under ~${floor:,.0f}/mo rank lower for you: "
                           f"that is the 25th percentile of what you have acted on."),
                "evidence": json.dumps({
                    "approval_floor_usd": floor,
                    "acted_count": profile.get("acted_count"),
                    "acted_median_usd": profile.get("acted_median_usd"),
                    "dismissed_median_usd": profile.get("dismissed_median_usd"),
                }),
            }

        for key, want in current.items():
            if key in pinned:
                counts["skipped_rolled_back"] += 1
                continue
            have = active.get(key)
            same = False
            if have is not None:
                if want["kind"] == "approval_floor":
                    try:
                        old = json.loads(have.evidence).get("approval_floor_usd") or 0.0
                        new = json.loads(want["evidence"]).get("approval_floor_usd") or 0.0
                        same = old > 0 and abs(new - old) / old < _FLOOR_CHANGE_MIN
                    except Exception:
                        same = False
                else:
                    same = have.to_verdict == want["to"]
            if same:
                conn.execute(ll.update().where(ll.c.id == have.id).values(
                    last_confirmed=now, lesson=want["lesson"], evidence=want["evidence"]))
                counts["confirmed"] += 1
                continue
            if have is not None:
                conn.execute(ll.update().where(ll.c.id == have.id).values(
                    status="superseded", superseded_at=now))
                counts["superseded"] += 1
            conn.execute(ll.insert().values(
                key=key, kind=want["kind"],
                from_verdict=have.to_verdict if have is not None else None,
                to_verdict=want["to"], lesson=want["lesson"],
                evidence=want["evidence"], first_seen=now, last_confirmed=now,
                status="active",
            ))
            counts["new"] += 1

        # Lessons the signal no longer supports (verdict fell back to neutral,
        # floor unset): nable unlearned it. Supersede so history says so.
        for key, have in active.items():
            if key not in current:
                conn.execute(ll.update().where(ll.c.id == have.id).values(
                    status="superseded", superseded_at=now))
                counts["superseded"] += 1

    return counts


def lessons(include_history: bool = False) -> list[dict[str, Any]]:
    """The recorded lessons, newest first. Active and rolled-back always;
    superseded only when history is asked for."""
    statuses = ("active", "rolled_back") if not include_history else ()
    with get_engine().connect() as conn:
        rows = _rows(conn, *statuses)
    out = []
    for r in rows:
        try:
            evidence = json.loads(r.evidence)
        except Exception:
            evidence = {}
        out.append({
            "id": r.id, "key": r.key, "kind": r.kind,
            "from_verdict": r.from_verdict, "to_verdict": r.to_verdict,
            "lesson": r.lesson, "evidence": evidence,
            "first_seen": r.first_seen.isoformat() if r.first_seen else None,
            "last_confirmed": r.last_confirmed.isoformat() if r.last_confirmed else None,
            "status": r.status,
            "rollback_note": r.rollback_note or "",
        })
    out.sort(key=lambda x: x["first_seen"] or "", reverse=True)
    return out


def rollback(lesson_id: int, note: str = "") -> dict[str, Any]:
    """Overrule a lesson: its key is pinned to standard ranking until restored."""
    ll = learning_lessons
    with get_engine().begin() as conn:
        row = conn.execute(select(ll).where(ll.c.id == lesson_id)).fetchone()
        if row is None:
            return {"error": f"no lesson with id {lesson_id}"}
        if row.status == "rolled_back":
            return {"ok": True, "already": True, "key": row.key}
        conn.execute(ll.update().where(ll.c.id == lesson_id).values(
            status="rolled_back", rolled_back_at=_now(), rollback_note=note or ""))
    return {"ok": True, "key": row.key,
            "effect": "standard ranking applies for this key until you restore the lesson"}


def restore(lesson_id: int) -> dict[str, Any]:
    """Lift a rollback. The next sync re-evaluates the key from live evidence."""
    ll = learning_lessons
    with get_engine().begin() as conn:
        row = conn.execute(select(ll).where(ll.c.id == lesson_id)).fetchone()
        if row is None:
            return {"error": f"no lesson with id {lesson_id}"}
        if row.status != "rolled_back":
            return {"ok": True, "already": True, "key": row.key}
        # Back to superseded, not active: the signal decides again on live
        # evidence at the next sync, rather than us asserting the old verdict
        # still holds.
        conn.execute(ll.update().where(ll.c.id == lesson_id).values(
            status="superseded", superseded_at=_now()))
    return {"ok": True, "key": row.key,
            "effect": "the next sync re-evaluates this key from current evidence"}


def apply_overrides(signal: dict) -> dict:
    """Pin every rolled-back key to neutral inside a computed signal.

    Applied by customer_signal() itself, so every consumer (the rescorer, the
    watch loop, the MCP tool) honors rollbacks with no wiring of its own.
    """
    try:
        pinned = rolled_back_keys()
    except Exception:
        return signal
    if not pinned:
        return signal

    from .signal import PRIOR_ACT_RATE, _confidence_multiplier
    neutral_mult = _confidence_multiplier(PRIOR_ACT_RATE, None)
    applied: list[str] = []

    def _neutralise(entry: dict) -> dict:
        e = dict(entry)
        e["verdict"] = "neutral"
        e["confidence_multiplier"] = neutral_mult
        e["why"] = (e.get("why", "") +
                    " You rolled this learned adjustment back; standard ranking applies.")
        e["overridden"] = True
        return e

    # A pinned source covers its buckets too: a bucket is a subdivision of the
    # source, and signal_for prefers bucket entries, so leaving them un-pinned
    # would let the rollback be quietly out-argued by its own breakdown.
    def _bucket_pinned(e: dict) -> bool:
        return (f"bucket:{e['source']}|{e.get('bucket')}" in pinned
                or f"source:{e['source']}" in pinned)

    out = dict(signal)
    out["by_source"] = [
        _neutralise(e) if f"source:{e['source']}" in pinned else e
        for e in signal.get("by_source", [])
    ]
    out["by_bucket"] = [
        _neutralise(e) if _bucket_pinned(e) else e
        for e in signal.get("by_bucket", [])
    ]
    applied = [f"source:{e['source']}" for e in signal.get("by_source", [])
               if f"source:{e['source']}" in pinned]
    applied += [f"bucket:{e['source']}|{e.get('bucket')}" for e in signal.get("by_bucket", [])
                if f"bucket:{e['source']}|{e.get('bucket')}" in pinned]
    if "approval_floor" in pinned:
        profile = dict(out.get("approval_profile") or {})
        profile["approval_floor_usd"] = None
        profile["note"] = (profile.get("note", "") +
                           " The learned dollar floor was rolled back by you; no floor applies.")
        out["approval_profile"] = profile
        applied.append("approval_floor")
    out["overrides_applied"] = sorted(set(applied))
    return out
