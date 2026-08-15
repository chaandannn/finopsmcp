# SPDX-License-Identifier: Apache-2.0
"""Precomputed cost aggregates, so a page load is a row read and not a scan.

Why this exists, measured 2026-08-15. The hosted dashboard's main endpoint,
/api/data, does not read the local snapshot store at all: it iterates every
connector, probes credentials, and fetches costs from the provider APIs live,
under a 30-second cap with a 12-hour cache. Its own timeout message is "The AWS
API is slow." First load after a cache expiry is a page that spins, and the
number the customer sees is gated on Cost Explorer's mood.

That is the wrong shape and it is not what fast products do. Vantage's service
and resource pages load in under three seconds because the data was ingested
once and is served from their own store; the dashboards that compute live are
the ones the user described as lagging. The seam is visible from outside.

nable already ingests. scheduler.jobs.job_snapshot writes cost_snapshots on a
cron. Nothing reads it for the dashboard. So this module is not new data, it is
the missing half of a pipeline that already runs.

WHAT IS ROLLED UP, AND WHY THESE SHAPES

cost_snapshots is already daily and granular: (provider, service, account_id,
region, snapshot_date). Ninety days for one tenant is tens of thousands of rows,
which is not slow to scan so much as slow to AGGREGATE, in Python, on every
request, for every widget on the page.

The rollup precomputes the aggregates the dashboard actually renders, including
the total rows. A "*" in provider, account_id or service means "all of them",
so the headline number every page opens with is one row by primary key rather
than a sum over a fan-out.

    provider  account_id  service   grain  meaning
    aws       *           *         month  what AWS cost this month
    *         *           *         month  the headline number
    aws       *           EC2       month  a row in the top-services list
    *         *           *         day    a point on the spend-over-time chart

REFRESH, NOT INCREMENTAL

refresh_rollups() recomputes whole periods rather than adjusting them. Cost
Explorer restates recent days for a week or more after the fact, so an
incremental counter drifts away from the source and there is no way to notice.
Recomputing a period is cheap, is idempotent, and can never disagree with
cost_snapshots. When it becomes too slow to recompute everything, bound it by
period, not by adding deltas.
"""
from __future__ import annotations

import logging
from datetime import date, datetime, timedelta, timezone

from sqlalchemy import (
    Column, DateTime, Float, Index, Integer, String, Table, delete, func, select,
)

from .db import cost_snapshots, get_engine, metadata

log = logging.getLogger(__name__)

# The wildcard that marks a total row. Chosen over NULL because it participates
# in a unique index on every backend: in SQLite and PostgreSQL two NULLs are not
# equal, so a NULL-bearing unique index would happily store the same total twice.
ALL = "*"

cost_rollups = Table(
    "cost_rollups", metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("provider", String(64), nullable=False),      # or ALL
    Column("account_id", String(128), nullable=False),   # or ALL
    Column("service", String(256), nullable=False),      # or ALL
    Column("grain", String(8), nullable=False),          # "day" | "month"
    Column("period", String(10), nullable=False),        # YYYY-MM-DD | YYYY-MM
    Column("amount_usd", Float, nullable=False, default=0.0),
    Column("computed_at", DateTime, nullable=False),
    # One value per cell. The refresh deletes and rewrites a period, so this is
    # what makes a double-refresh a no-op instead of a doubling.
    Index("ux_rollup_cell", "provider", "account_id", "service", "grain", "period",
          unique=True),
)


def _month_of(day: str) -> str:
    """'2026-08-14' -> '2026-08'. Snapshot dates are stored as ISO strings."""
    return day[:7]


def refresh_rollups(months_back: int = 13) -> dict[str, int]:
    """Recompute every rollup cell from cost_snapshots. Returns a row count.

    13 months by default so a year-over-year comparison always has its other
    end, and one spare month for the partial one at each edge.

    Idempotent: the whole window is deleted and rewritten inside one
    transaction, so a refresh that runs twice, or races another, leaves exactly
    one row per cell rather than two.
    """
    engine = get_engine()
    cutoff = (date.today().replace(day=1) - timedelta(days=31 * months_back)).isoformat()
    now = datetime.now(timezone.utc)
    written = 0

    with engine.begin() as conn:
        rows = conn.execute(
            select(
                cost_snapshots.c.provider,
                cost_snapshots.c.account_id,
                cost_snapshots.c.service,
                cost_snapshots.c.snapshot_date,
                func.sum(cost_snapshots.c.amount_usd).label("amount"),
            )
            .where(cost_snapshots.c.snapshot_date >= cutoff)
            .group_by(
                cost_snapshots.c.provider,
                cost_snapshots.c.account_id,
                cost_snapshots.c.service,
                cost_snapshots.c.snapshot_date,
            )
        ).fetchall()

        # Accumulate every cell in memory first. The source is already grouped
        # to its finest grain, so this is a fan-out over a few thousand rows,
        # not a second pass over the raw table.
        cells: dict[tuple[str, str, str, str, str], float] = {}

        def add(provider: str, account: str, service: str, grain: str,
                period: str, amount: float) -> None:
            key = (provider, account, service, grain, period)
            cells[key] = cells.get(key, 0.0) + amount

        for r in rows:
            amount = float(r.amount or 0.0)
            day, month = r.snapshot_date, _month_of(r.snapshot_date)
            prov, acct, svc = r.provider, r.account_id, r.service

            # MONTH grain gets the full fan-out, because that is what the
            # service list, the account breakdown and the headline all read.
            add(prov, acct, svc, "month", month, amount)
            add(prov, acct, ALL, "month", month, amount)
            add(prov, ALL, svc, "month", month, amount)
            add(prov, ALL, ALL, "month", month, amount)
            add(ALL, ALL, svc, "month", month, amount)
            add(ALL, ALL, ALL, "month", month, amount)

            # DAY grain gets totals only. The single consumer, daily_series,
            # reads (provider, ALL, ALL) for the spend chart and nothing else.
            # Writing per-service and per-account day cells made the rollup 1.7x
            # LARGER than the table it summarises, to answer questions nothing
            # asks. Precomputing everything is not the same as precomputing what
            # is read, and on the biggest table in the product the difference is
            # the whole point.
            #
            # If a per-service daily chart ever ships, add its cells here
            # deliberately, with the query that needs them.
            add(prov, ALL, ALL, "day", day, amount)
            add(ALL, ALL, ALL, "day", day, amount)

        conn.execute(delete(cost_rollups).where(cost_rollups.c.period >= cutoff[:7]))
        if cells:
            conn.execute(cost_rollups.insert(), [
                {
                    "provider": p, "account_id": a, "service": s,
                    "grain": g, "period": per,
                    "amount_usd": round(amt, 6),
                    "computed_at": now,
                }
                for (p, a, s, g, per), amt in cells.items()
            ])
            written = len(cells)

    log.info("rollups refreshed: %s cells from %s source rows", written, len(rows))
    return {"cells": written, "source_rows": len(rows)}


# ── reads: each one is what a widget needs, and nothing more ─────────────────

def month_total(period: str, provider: str = ALL, account_id: str = ALL) -> float | None:
    """Total for a month, e.g. '2026-08'. None when the cell was never computed.

    None and 0.0 are different answers and both are real: a month with no data
    is not a month with no spend. Returning 0.0 for an absent cell is how a
    dashboard tells someone their bill went to zero.
    """
    engine = get_engine()
    with engine.connect() as conn:
        row = conn.execute(
            select(cost_rollups.c.amount_usd).where(
                (cost_rollups.c.provider == provider)
                & (cost_rollups.c.account_id == account_id)
                & (cost_rollups.c.service == ALL)
                & (cost_rollups.c.grain == "month")
                & (cost_rollups.c.period == period)
            )
        ).first()
    return float(row[0]) if row is not None else None


def top_services(period: str, limit: int = 10, provider: str = ALL) -> list[dict]:
    """Largest services in a month, biggest first."""
    engine = get_engine()
    with engine.connect() as conn:
        rows = conn.execute(
            select(cost_rollups.c.service, cost_rollups.c.amount_usd)
            .where(
                (cost_rollups.c.provider == provider)
                & (cost_rollups.c.account_id == ALL)
                & (cost_rollups.c.service != ALL)
                & (cost_rollups.c.grain == "month")
                & (cost_rollups.c.period == period)
            )
            .order_by(cost_rollups.c.amount_usd.desc())
            .limit(limit)
        ).fetchall()
    return [{"service": r.service, "amount_usd": round(float(r.amount_usd), 2)}
            for r in rows]


def daily_series(start: str, end: str, provider: str = ALL) -> list[dict]:
    """Daily totals between two ISO dates, oldest first. The spend chart."""
    engine = get_engine()
    with engine.connect() as conn:
        rows = conn.execute(
            select(cost_rollups.c.period, cost_rollups.c.amount_usd)
            .where(
                (cost_rollups.c.provider == provider)
                & (cost_rollups.c.account_id == ALL)
                & (cost_rollups.c.service == ALL)
                & (cost_rollups.c.grain == "day")
                & (cost_rollups.c.period >= start)
                & (cost_rollups.c.period <= end)
            )
            .order_by(cost_rollups.c.period)
        ).fetchall()
    return [{"date": r.period, "amount_usd": round(float(r.amount_usd), 4)}
            for r in rows]


def freshness() -> datetime | None:
    """When the rollups were last computed, or None if they never were.

    The dashboard has to be able to say "as of 06:00" rather than implying the
    number is live. Serving stale data silently is the failure this whole
    module could otherwise introduce.
    """
    engine = get_engine()
    with engine.connect() as conn:
        row = conn.execute(select(func.max(cost_rollups.c.computed_at))).first()
    return row[0] if row and row[0] else None
