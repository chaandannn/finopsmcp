"""
Savings recommendation lifecycle tracker.

Every recommendation nable surfaces (rightsizing, idle resources, K8s waste,
commitment gaps, waste patterns) is persisted here so we can answer:

  "How much have we actually saved from recommendations we acted on?"

Lifecycle:
  open → acted_on → verified   (ideal path)
  open → dismissed              (won't fix)
  open → expired                (30+ days, never actioned)

Verification:
  For EC2/RDS: re-query the instance type after acted_on and compare cost.
  For idle resources: check the resource still exists.
  For K8s: re-run namespace cost and compare.
  For commitments: compare coverage % before/after.
"""
from __future__ import annotations

import hashlib
import json
import logging
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import case, func, select, update

from ..storage.db import get_engine, savings_recommendations

log = logging.getLogger(__name__)

# Recommendations expire (auto-closed as stale) after this many days unactioned
_EXPIRY_DAYS = 45


# ── Dedup key ─────────────────────────────────────────────────────────────────

def _dedup(source: str, resource_id: str, recommended_config: dict) -> str:
    # Hash only the STABLE parts of the config. Numeric estimate ranges
    # (monthly_saving_min/max) moved in and out of recommended_config across
    # releases; including them split the key and wrote the same recommendation
    # as two rows, double-counting the potential savings. Drop them so one
    # recommendation always maps to one key.
    stable = {
        k: v for k, v in (recommended_config or {}).items()
        if k not in ("monthly_saving_min", "monthly_saving_max")
    }
    raw = f"{source}:{resource_id}:{json.dumps(stable, sort_keys=True)}"
    return hashlib.sha256(raw.encode()).hexdigest()[:32]


# Identity for collapsing duplicate rows at READ time. The write-side dedup key
# above prevents new duplicates, but rows written by older releases (before the
# key was stabilized) persist with different keys. The dashboard and the MCP
# tools both dedup on this identity so the "potential savings" number matches
# everywhere and never double-counts a legacy duplicate.
def _identity(source: str, resource_id: str, description: str) -> tuple:
    return (source or "", resource_id or "", description or "")


_STATUS_PRIORITY = {"verified": 4, "acted_on": 3, "open": 2, "expired": 1, "dismissed": 0}


def _dedup_rows(rows: list[dict]) -> list[dict]:
    """Collapse duplicate recommendations by identity, keeping the row with the
    most advanced status (a verified/acted row beats an open duplicate), then the
    highest estimate. Order-preserving for the survivors."""
    best: dict[tuple, dict] = {}
    order: list[tuple] = []
    for r in rows:
        ident = _identity(r.get("source"), r.get("resource_id"), r.get("description"))
        cur = best.get(ident)
        if cur is None:
            best[ident] = r
            order.append(ident)
            continue
        r_rank = (_STATUS_PRIORITY.get(r.get("status"), 0), r.get("estimated_monthly_savings_usd") or 0)
        c_rank = (_STATUS_PRIORITY.get(cur.get("status"), 0), cur.get("estimated_monthly_savings_usd") or 0)
        if r_rank > c_rank:
            best[ident] = r
    return [best[i] for i in order]


def _bucket(resource_type: str | None, environment: str | None) -> str | None:
    """Coarse env/workload bucket for the learning signal. Lazy import keeps the
    learning package (which imports this module) out of import-time cycles."""
    try:
        from .learning.bucket import bucket_for
        return bucket_for(resource_type, environment)
    except Exception:
        return None


# A change takes time to land: you mark a rightsizing acted-on, the resize happens
# in the next maintenance window, and CloudWatch needs a few days of post-change
# data before the detector stops seeing the old shape. Re-flagging inside that
# window is the pipeline catching up, not the fix coming undone. Only past it do we
# call it a regression, so "you already fixed this once" stays a claim worth
# trusting.
_REGRESSION_GRACE_DAYS = 7


def _is_regression(conn, rec_id: int) -> bool:
    """True when an acted-on/verified recommendation is old enough that the
    detector seeing it again means the change was reverted."""
    row = conn.execute(
        select(
            savings_recommendations.c.acted_on_at,
            savings_recommendations.c.verified_at,
        ).where(savings_recommendations.c.id == rec_id)
    ).first()
    if row is None:
        return False
    marker = row.verified_at or row.acted_on_at
    if marker is None:
        # Status says acted_on but no timestamp: cannot date it, so do not accuse.
        return False
    if marker.tzinfo is None:
        marker = marker.replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - marker) > timedelta(days=_REGRESSION_GRACE_DAYS)


def list_regressions(limit: int = 50) -> list[dict[str, Any]]:
    """Fixes that came undone, newest first.

    This is the answer to "nothing stays optimized": a point-in-time scanner can
    only tell you the resource is wrong right now. Because the recommendation
    survives across scans, we can also say you already fixed it, when, and how
    many times it has come back. A resource with a rising regression_count is not
    a savings opportunity, it is a process problem, and reporting it as a fresh
    finding every quarter is how a FinOps tool trains people to ignore it.
    """
    engine = get_engine()
    with engine.begin() as conn:
        rows = conn.execute(
            select(savings_recommendations)
            .where(savings_recommendations.c.regression_count > 0)
            .order_by(savings_recommendations.c.regressed_at.desc())
            .limit(limit)
        ).mappings().all()
    out: list[dict[str, Any]] = []
    for r in rows:
        d = dict(r)
        d["headline"] = (
            f"Fixed before, back again ({d['regression_count']}x): "
            f"{d.get('resource_name') or d.get('resource_id')}"
        )
        out.append(d)
    return out


# ── Upsert a recommendation ───────────────────────────────────────────────────

def record_recommendation(
    source: str,
    provider: str,
    resource_id: str,
    resource_type: str,
    resource_name: str,
    current_config: dict,
    recommended_config: dict,
    description: str,
    estimated_monthly_savings_usd: float,
    account_id: str = "",
    region: str = "",
    environment: str | None = None,
) -> int | None:
    """
    Persist a recommendation. Returns the row ID, or None if it already exists.
    Existing open/acted_on records are NOT overwritten — we only insert new ones.
    """
    key = _dedup(source, resource_id, recommended_config)
    engine = get_engine()
    with engine.begin() as conn:
        # Check if already exists
        existing = conn.execute(
            select(savings_recommendations.c.id, savings_recommendations.c.status)
            .where(savings_recommendations.c.dedup_key == key)
        ).first()

        if existing:
            # The fix was applied (or measured) and the detector is flagging the
            # same resource again: it came undone. This is the drift case, and it
            # is the one thing a point-in-time scanner cannot tell you. Every other
            # tool re-reports it as a brand new finding.
            if existing.status in ("acted_on", "verified"):
                if _is_regression(conn, existing.id):
                    conn.execute(
                        update(savings_recommendations)
                        .where(savings_recommendations.c.id == existing.id)
                        .values(
                            status="open",
                            regressed_at=datetime.now(timezone.utc),
                            regression_count=(
                                savings_recommendations.c.regression_count + 1
                            ),
                            estimated_monthly_savings_usd=estimated_monthly_savings_usd,
                            description=description,
                            current_config=json.dumps(current_config),
                            generated_at=datetime.now(timezone.utc),
                            # The previous verification described a state that no
                            # longer holds, so it cannot stand as a current saving.
                            verified_at=None,
                            verified_monthly_savings_usd=None,
                            verified_basis=None,
                        )
                    )
                return existing.id

            # If previously dismissed/expired, re-open it (resource regressed)
            if existing.status in ("dismissed", "expired"):
                conn.execute(
                    update(savings_recommendations)
                    .where(savings_recommendations.c.id == existing.id)
                    .values(
                        status="open",
                        estimated_monthly_savings_usd=estimated_monthly_savings_usd,
                        description=description,
                        current_config=json.dumps(current_config),
                        generated_at=datetime.now(timezone.utc),
                        acted_on_at=None,
                        verified_at=None,
                        dismissed_at=None,
                        dismiss_reason=None,
                        dismiss_reason_category=None,
                        verified_monthly_savings_usd=None,
                    )
                )
            return existing.id

        result = conn.execute(
            savings_recommendations.insert().values(
                source=source,
                provider=provider,
                account_id=account_id,
                region=region,
                resource_id=resource_id,
                resource_type=resource_type,
                resource_name=resource_name,
                current_config=json.dumps(current_config),
                recommended_config=json.dumps(recommended_config),
                description=description,
                estimated_monthly_savings_usd=estimated_monthly_savings_usd,
                status="open",
                generated_at=datetime.now(timezone.utc),
                dedup_key=key,
                environment_bucket=_bucket(resource_type, environment),
            )
        )
        return result.inserted_primary_key[0]


# ── Status transitions ────────────────────────────────────────────────────────

def mark_acted_on(rec_id: int) -> bool:
    engine = get_engine()
    with engine.begin() as conn:
        r = conn.execute(
            update(savings_recommendations)
            .where(
                savings_recommendations.c.id == rec_id,
                savings_recommendations.c.status == "open",
            )
            .values(status="acted_on", acted_on_at=datetime.now(timezone.utc))
        )
        return r.rowcount > 0


def mark_verified(rec_id: int, actual_monthly_savings_usd: float, basis: str = "list_price") -> bool:
    engine = get_engine()
    with engine.begin() as conn:
        r = conn.execute(
            update(savings_recommendations)
            .where(savings_recommendations.c.id == rec_id)
            .values(
                status="verified",
                verified_at=datetime.now(timezone.utc),
                verified_monthly_savings_usd=actual_monthly_savings_usd,
                verified_basis=basis,
            )
        )
        return r.rowcount > 0


def mark_dismissed(rec_id: int, reason: str = "") -> bool:
    # Canonicalize the free-text reason once, at dismiss time, so the learning signal
    # can separate a quality miss ("estimate is wrong" -> counts against the source)
    # from a business reason ("reserved for peak" -> a choice, not a quality signal).
    # The raw text is always kept; the category is only a hint.
    from .learning.reasons import classify_dismiss_reason
    category = classify_dismiss_reason(reason)
    engine = get_engine()
    with engine.begin() as conn:
        r = conn.execute(
            update(savings_recommendations)
            .where(
                savings_recommendations.c.id == rec_id,
                savings_recommendations.c.status.in_(["open", "acted_on"]),
            )
            .values(
                status="dismissed",
                dismissed_at=datetime.now(timezone.utc),
                dismiss_reason=reason or None,
                dismiss_reason_category=category,
            )
        )
        return r.rowcount > 0


def expire_stale(days: int = _EXPIRY_DAYS) -> int:
    """Mark open recommendations older than `days` as expired."""
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    engine = get_engine()
    with engine.begin() as conn:
        r = conn.execute(
            update(savings_recommendations)
            .where(
                savings_recommendations.c.status == "open",
                savings_recommendations.c.generated_at < cutoff,
            )
            .values(status="expired")
        )
        return r.rowcount


# ── Summary queries ───────────────────────────────────────────────────────────

def get_summary() -> dict[str, Any]:
    """
    Return the realized-savings dashboard:
      - potential_monthly_usd: sum of open recommendations
      - acted_on_monthly_usd:  estimated savings from acted-on recs
      - verified_monthly_usd:  actual measured savings (verified recs)
      - counts by status and source

    Deduplicates legacy duplicate rows by identity before summing, so the
    potential-savings number never double-counts a recommendation written twice
    by an older release. The table is per-account and small, a full scan is
    cheap and correctness beats the GROUP BY it replaced.
    """
    sr = savings_recommendations
    engine = get_engine()

    with engine.connect() as conn:
        raw = conn.execute(
            select(
                sr.c.status, sr.c.source, sr.c.resource_id, sr.c.description,
                sr.c.estimated_monthly_savings_usd, sr.c.verified_monthly_savings_usd,
                sr.c.verified_basis,
            )
        ).fetchall()

    deduped = _dedup_rows([
        {
            "status": r.status, "source": r.source, "resource_id": r.resource_id,
            "description": r.description,
            "estimated_monthly_savings_usd": r.estimated_monthly_savings_usd,
            "verified_monthly_savings_usd": r.verified_monthly_savings_usd,
            "verified_basis": r.verified_basis,
        }
        for r in raw
    ])
    total_count = len(deduped)

    _STATUS_KEYS = ("open", "acted_on", "verified", "dismissed", "expired")

    potential = 0.0
    acted_estimated = 0.0
    verified_actual = 0.0
    verified_bill_measured = 0.0
    by_status: dict[str, int] = {}
    by_source: dict[str, dict] = {}

    for row in deduped:
        s = row["status"]
        src = row["source"]
        est = float(row.get("estimated_monthly_savings_usd") or 0.0)
        ver = float(row.get("verified_monthly_savings_usd") or 0.0)

        by_status[s] = by_status.get(s, 0) + 1

        if src not in by_source:
            by_source[src] = {k: 0 for k in _STATUS_KEYS}
            by_source[src]["potential_usd"] = 0.0
            by_source[src]["verified_usd"] = 0.0

        bucket = s if s in _STATUS_KEYS else "dismissed"
        by_source[src][bucket] = by_source[src].get(bucket, 0) + 1
        by_source[src]["potential_usd"] += est if s == "open" else 0.0
        by_source[src]["verified_usd"] += ver if s == "verified" else 0.0

        if s == "open":
            potential += est
        elif s == "acted_on":
            acted_estimated += est
        elif s == "verified":
            verified_actual += ver
            if row.get("verified_basis") == "bill_measured":
                verified_bill_measured += ver

    return {
        "potential_monthly_usd": round(potential, 2),
        "acted_on_monthly_usd": round(acted_estimated, 2),   # estimated, not yet verified
        "verified_monthly_usd": round(verified_actual, 2),   # confirmed actual savings
        "verified_annual_usd": round(verified_actual * 12, 2),
        # The subset of verified savings measured directly off the bill (CUR
        # before/after), as opposed to rate-based estimates of a confirmed change.
        "verified_bill_measured_monthly_usd": round(verified_bill_measured, 2),
        "total_recommendations": total_count,
        "by_status": by_status,
        "by_source": by_source,
    }


def quality_signal() -> dict[str, Any]:
    """
    The recommendation-quality flywheel: per recommendation type (source), how
    often recs actually get acted on and how close the PREDICTED savings were to
    the MEASURED realized savings.

    This is the moat's training signal (rank and suppress low-yield rec types) and
    the verified-savings proof, not just a log of what we suggested. Per source:
    total, acted (acted_on + verified), verified, predicted_monthly_usd,
    realized_monthly_usd, act_rate (acted / total), and accuracy (realized vs
    predicted among VERIFIED recs only: 1.0 = predictions landed, <1 = we
    over-predicted, >1 = under-predicted). Plus the headline verified monthly and
    annual run-rate. Sorted so the rec types that actually pay off lead.
    """
    sr = savings_recommendations
    engine = get_engine()
    with engine.connect() as conn:
        rows = conn.execute(
            select(
                sr.c.source,
                sr.c.status,
                func.count().label("cnt"),
                func.sum(sr.c.estimated_monthly_savings_usd).label("sum_est"),
                func.sum(sr.c.verified_monthly_savings_usd).label("sum_ver"),
            ).group_by(sr.c.source, sr.c.status)
        ).fetchall()

    agg: dict[str, dict] = {}
    for r in rows:
        d = agg.setdefault(r.source or "unknown", {
            "total": 0, "acted": 0, "verified": 0,
            "predicted_monthly_usd": 0.0,
            "realized_monthly_usd": 0.0,
            "predicted_of_verified_usd": 0.0,
        })
        cnt = r.cnt or 0
        d["total"] += cnt
        d["predicted_monthly_usd"] += float(r.sum_est or 0.0)
        if r.status in ("acted_on", "verified"):
            d["acted"] += cnt
        if r.status == "verified":
            d["verified"] += cnt
            d["realized_monthly_usd"] += float(r.sum_ver or 0.0)
            d["predicted_of_verified_usd"] += float(r.sum_est or 0.0)

    by_source = []
    total_realized = 0.0
    for src, d in agg.items():
        total = d["total"] or 1
        pov = d["predicted_of_verified_usd"]
        realized = d["realized_monthly_usd"]
        total_realized += realized
        by_source.append({
            "source": src,
            "total": d["total"],
            "acted": d["acted"],
            "verified": d["verified"],
            "act_rate": round(d["acted"] / total, 3),
            "predicted_monthly_usd": round(d["predicted_monthly_usd"], 2),
            "realized_monthly_usd": round(realized, 2),
            # accuracy is only meaningful once a source has verified recs to measure
            "accuracy": round(realized / pov, 3) if pov > 0 else None,
        })
    by_source.sort(key=lambda s: s["realized_monthly_usd"], reverse=True)

    return {
        "verified_monthly_usd": round(total_realized, 2),
        "verified_annual_run_rate_usd": round(total_realized * 12, 2),
        # Explicit banked alias: money confirmed to have left the bill, distinct
        # from predicted. Same value, unambiguous name for hero surfaces.
        "verified_banked_monthly_usd": round(total_realized, 2),
        "verified_banked_annual_usd": round(total_realized * 12, 2),
        "by_source": by_source,
        "note": ("accuracy compares predicted vs measured realized savings among "
                 "verified recommendations; act_rate is acted-or-verified over total. "
                 "Sources with a low act_rate or low accuracy are candidates to suppress."),
    }


def list_recommendations(
    status: str | None = None,
    source: str | None = None,
    limit: int = 50,
) -> list[dict[str, Any]]:
    engine = get_engine()
    q = select(savings_recommendations).order_by(
        savings_recommendations.c.estimated_monthly_savings_usd.desc()
    )
    if status:
        q = q.where(savings_recommendations.c.status == status)
    if source:
        q = q.where(savings_recommendations.c.source == source)
    q = q.limit(limit)

    # Fetch a wider window than `limit`, dedup legacy duplicates, then trim.
    with engine.connect() as conn:
        rows = conn.execute(q.limit(None)).fetchall()

    result = []
    for r in rows:
        result.append({
            "id": r.id,
            "source": r.source,
            "provider": r.provider,
            "account_id": r.account_id,
            "resource_id": r.resource_id,
            "resource_type": r.resource_type,
            "resource_name": r.resource_name,
            "description": r.description,
            "estimated_monthly_savings_usd": r.estimated_monthly_savings_usd,
            "verified_monthly_savings_usd": r.verified_monthly_savings_usd,
            "recommended_config": r.recommended_config,
            "current_config": r.current_config,
            "status": r.status,
            "generated_at": r.generated_at.isoformat() if r.generated_at else None,
            "acted_on_at": r.acted_on_at.isoformat() if r.acted_on_at else None,
            "verified_at": r.verified_at.isoformat() if r.verified_at else None,
            "dismiss_reason": r.dismiss_reason,
            "dismiss_reason_category": getattr(r, "dismiss_reason_category", None),
            "environment_bucket": getattr(r, "environment_bucket", None),
        })
    return _dedup_rows(result)[:limit]


def get_recommendation(rec_id: int) -> dict[str, Any] | None:
    """Fetch one recommendation by id, or None. Same dict shape as list_recommendations."""
    engine = get_engine()
    with engine.connect() as conn:
        r = conn.execute(
            select(savings_recommendations).where(savings_recommendations.c.id == rec_id)
        ).fetchone()
    if r is None:
        return None
    return {
        "id": r.id, "source": r.source, "provider": r.provider,
        "account_id": r.account_id, "resource_id": r.resource_id,
        "resource_type": r.resource_type, "resource_name": r.resource_name,
        "description": r.description,
        "estimated_monthly_savings_usd": r.estimated_monthly_savings_usd,
        "status": r.status,
        "environment_bucket": getattr(r, "environment_bucket", None),
        # Verification state, so a caller can tell a measured saving from an
        # estimate, and can see when a measured one was invalidated by a regression.
        "verified_monthly_savings_usd": getattr(r, "verified_monthly_savings_usd", None),
        "verified_at": getattr(r, "verified_at", None),
        "verified_basis": getattr(r, "verified_basis", None),
        # Drift: this fix was applied before and came undone.
        "regressed_at": getattr(r, "regressed_at", None),
        "regression_count": getattr(r, "regression_count", 0) or 0,
    }


# ── Verification: check if changes were actually made ────────────────────────
# The actual verifiers live in verifiers.py, keyed by (source, resource_type) in
# a small registry, so auto_verify_acted_on dispatches instead of hard-coding
# EC2. verify_ec2_change is re-exported here for backward compatibility with any
# caller that imported it from this module before the registry existed.
from .verifiers import get_verifier, verify_ec2_change  # noqa: E402,F401


def auto_verify_acted_on() -> list[dict]:
    """
    For every acted_on recommendation, dispatch to the registered verifier for
    its (source, resource_type) and confirm the change actually landed. A source
    with no registered verifier is a no-op: the row stays acted_on, nothing
    crashes, and it gets another chance once a verifier ships.

    Returns the list of newly-verified records with their measured savings.
    """
    engine = get_engine()
    with engine.connect() as conn:
        rows = conn.execute(
            select(savings_recommendations)
            .where(savings_recommendations.c.status == "acted_on")
        ).fetchall()

    newly_verified = []
    for r in rows:
        try:
            verifier = get_verifier(r.source, r.resource_type)
            if verifier is None:
                # No verifier for this source yet. Leave it acted_on so it can
                # be picked up later without losing the realized saving.
                continue

            rec_config = json.loads(r.recommended_config or "{}")
            actual_savings = verifier(r.resource_id, rec_config, r)

            if actual_savings is not None:
                # The verifier confirmed the change happened. Now price it on
                # the best available data: the bill itself, then the customer's
                # effective rate, then list price. The basis rides the row.
                from .measure import measure_realized_savings
                usd, basis = measure_realized_savings(r, actual_savings)
                mark_verified(r.id, usd, basis)
                newly_verified.append({
                    "id": r.id,
                    "resource_id": r.resource_id,
                    "description": r.description,
                    "verified_monthly_savings_usd": usd,
                    "verified_basis": basis,
                })
        except Exception as e:
            log.debug("auto_verify row %s: %s", r.id, e)

    return newly_verified
