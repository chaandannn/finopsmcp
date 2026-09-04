"""Read-side aggregation over cost_snapshots.category.

Every function here reads the stored snapshots and nothing else. No Cost
Explorer, no billed API, no clock beyond the window the caller passes. The
category was decided once at ingest by finops.categories.classify_category; this
module only sums it.

A row whose category is NULL (written before categorization shipped) folds into
"other" so window_category_totals still reconciles to the window total. Whether
any real categories exist at all is a separate question, answered honestly by
categories_available.
"""
from __future__ import annotations

from datetime import date

from sqlalchemy import and_, func, select

from ..categories import CATEGORY_KEYS, ai_kind, ai_label
from .db import cost_snapshots, get_engine


def _iso(d: date | str) -> str:
    return d.isoformat() if hasattr(d, "isoformat") else str(d)


def _pct(amount: float | None, total: float) -> float:
    if not total:
        return 0.0
    return round(100.0 * float(amount or 0.0) / total, 1)


def _sum_by(columns, start, end, provider, category=None):
    """GROUP BY `columns`, SUM(amount_usd), over the window and optional filters."""
    clauses = [
        cost_snapshots.c.snapshot_date >= _iso(start),
        cost_snapshots.c.snapshot_date <= _iso(end),
    ]
    if provider and provider != "all":
        clauses.append(cost_snapshots.c.provider == provider)
    if category is not None:
        clauses.append(cost_snapshots.c.category == category)
    q = (select(*columns, func.sum(cost_snapshots.c.amount_usd))
         .where(and_(*clauses))
         .group_by(*columns))
    with get_engine().connect() as conn:
        return conn.execute(q).fetchall()


def window_category_totals(start, end, provider="all") -> dict[str, float]:
    """Window dollars per category. Sums to the window total (NULL -> other)."""
    totals = {k: 0.0 for k in CATEGORY_KEYS}
    for cat, amount in _sum_by([cost_snapshots.c.category], start, end, provider):
        key = cat if cat in totals else "other"
        totals[key] += float(amount or 0.0)
    return {k: round(v, 2) for k, v in totals.items()}


def daily_category_series(start, end, provider="all") -> dict[str, dict[str, float]]:
    """{date_iso: {category: dollars}} for the window. Per-day sums to that day."""
    out: dict[str, dict[str, float]] = {}
    rows = _sum_by(
        [cost_snapshots.c.snapshot_date, cost_snapshots.c.category],
        start, end, provider,
    )
    for day, cat, amount in rows:
        bucket = out.setdefault(day, {k: 0.0 for k in CATEGORY_KEYS})
        key = cat if cat in bucket else "other"
        bucket[key] = round(bucket[key] + float(amount or 0.0), 2)
    return out


def ai_breakdown(start, end, provider="all") -> list[dict]:
    """AI-and-GPU spend split into plain-English lines, largest first."""
    rows = _sum_by(
        [cost_snapshots.c.provider, cost_snapshots.c.service],
        start, end, provider, category="ai",
    )
    total = sum(float(a or 0.0) for _, _, a in rows)
    items = [
        {
            "key": f"{prov}:{svc}",
            "label": ai_label(prov, svc),
            "amount": round(float(amount or 0.0), 2),
            "pct": _pct(amount, total),
            "kind": ai_kind(prov, svc),
        }
        for prov, svc, amount in rows
    ]
    items.sort(key=lambda r: r["amount"], reverse=True)
    return items


def _ai_total(start, end, provider) -> float:
    rows = _sum_by([cost_snapshots.c.category], start, end, provider, category="ai")
    return round(sum(float(a or 0.0) for _, a in rows), 2)


def ai_window_and_prior(start, end, prior_start, prior_end, provider="all"):
    """(this-window AI dollars, prior-window AI dollars)."""
    return _ai_total(start, end, provider), _ai_total(prior_start, prior_end, provider)


def categories_available(start, end, provider="all") -> bool:
    """True if any snapshot in the window carries a non-null category."""
    clauses = [
        cost_snapshots.c.snapshot_date >= _iso(start),
        cost_snapshots.c.snapshot_date <= _iso(end),
        cost_snapshots.c.category.isnot(None),
    ]
    if provider and provider != "all":
        clauses.append(cost_snapshots.c.provider == provider)
    q = select(cost_snapshots.c.id).where(and_(*clauses)).limit(1)
    with get_engine().connect() as conn:
        return conn.execute(q).first() is not None


def _has_row(clauses) -> bool:
    q = select(cost_snapshots.c.id).where(and_(*clauses)).limit(1)
    with get_engine().connect() as conn:
        return conn.execute(q).first() is not None


def window_fully_categorized(start, end, provider="all") -> bool:
    """True only when the window has spend AND every row in it carries a category.

    categorization runs at AWS-CUR ingest, so a GCP or Azure or SaaS snapshot row
    lands with no category. On a mixed account those rows fold into "other" and an
    AI figure summed over only the categorized rows but divided by the whole
    account total is an undercount presented as measured. This gate is the honest
    signal: when any row in the scope is uncategorized it returns False, and the
    caller shows the "not categorized yet" state instead of a wrong number. A
    single-cloud account whose rows are all categorized returns True and reads
    exactly as before.
    """
    base = [
        cost_snapshots.c.snapshot_date >= _iso(start),
        cost_snapshots.c.snapshot_date <= _iso(end),
    ]
    if provider and provider != "all":
        base.append(cost_snapshots.c.provider == provider)
    if not _has_row(base):
        return False
    return not _has_row(base + [cost_snapshots.c.category.is_(None)])
