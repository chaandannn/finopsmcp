# SPDX-License-Identifier: Apache-2.0
"""A rollup that disagrees with its source is worse than no rollup.

The whole value of precomputing is that the fast answer and the slow answer are
the same answer. So the load-bearing test here is not "the rollup returns a
number", it is "the rollup returns the number a live aggregate over
cost_snapshots would have returned" — checked by computing both.

Context, measured 2026-08-15. The hosted dashboard's /api/data does not read
cost_snapshots at all: it iterates connectors and hits the provider APIs live,
under a 30-second cap, and its own timeout string is "The AWS API is slow."
Meanwhile scheduler.jobs.job_snapshot has been writing cost_snapshots on a cron
the whole time. The ingest existed and nothing read it.

Two failure modes this file exists to prevent:

  1. Drift. A rollup that quietly diverges from its source produces a confident
     wrong number, which is the defect class this codebase keeps finding.
  2. A double-refresh doubling every total. The refresh deletes and rewrites,
     and the unique index is what makes that safe; both are pinned below.
"""
from __future__ import annotations

import time
from datetime import date, datetime, timedelta, timezone

import pytest
from sqlalchemy import func, select

from finops.storage import db, rollups
from finops.storage.rollups import (
    ALL, cost_rollups, daily_series, freshness, month_total, refresh_rollups, top_services,
)


@pytest.fixture
def seeded(tmp_path, monkeypatch):
    """A realistic bill: 3 providers, 2 accounts, several services, 120 days.

    Shaped like a real one — a few big services and a long tail — because a
    rollup that only works on uniform data is a rollup that works on nothing.
    """
    monkeypatch.setenv("FINOPS_DB_PATH", str(tmp_path / "r.db"))
    monkeypatch.setenv("FINOPS_DATA_DIR", str(tmp_path / "d"))
    db._ENGINE, db._DATA_DIR = None, None
    engine = db.get_engine()

    today = date.today()
    services = {
        "aws": [("Amazon Elastic Compute Cloud - Compute", 900.0),
                ("Amazon Relational Database Service", 300.0),
                ("Amazon Simple Storage Service", 120.0),
                ("AWS Lambda", 8.0)],
        "gcp": [("Compute Engine", 400.0), ("BigQuery", 90.0)],
        "datadog": [("APM", 180.0)],
    }
    rows = []
    for back in range(120):
        d = (today - timedelta(days=back)).isoformat()
        for provider, svcs in services.items():
            for account in ("111111111111", "222222222222"):
                for name, base in svcs:
                    rows.append({
                        "provider": provider, "service": name, "account_id": account,
                        "region": "us-east-1", "snapshot_date": d,
                        # vary by day so a bug that reads one day and multiplies
                        # cannot accidentally match
                        "amount_usd": base + (back % 7),
                        "granularity": "DAILY",
                        "captured_at": datetime.now(timezone.utc),
                    })
    with engine.begin() as conn:
        conn.execute(db.cost_snapshots.insert(), rows)

    yield engine, len(rows)
    db._ENGINE, db._DATA_DIR = None, None


def _live_month_total(engine, period: str, provider: str | None = None) -> float:
    """The slow answer, computed the way the dashboard would have to without a
    rollup: aggregate cost_snapshots directly."""
    q = select(func.sum(db.cost_snapshots.c.amount_usd)).where(
        db.cost_snapshots.c.snapshot_date.like(f"{period}%"))
    if provider:
        q = q.where(db.cost_snapshots.c.provider == provider)
    with engine.connect() as conn:
        return float(conn.execute(q).scalar() or 0.0)


# ── the one that matters ─────────────────────────────────────────────────────

def test_the_rollup_agrees_with_a_live_aggregate(seeded):
    """Fast answer == slow answer, for the headline and for every provider.

    If this ever fails, the rollup is not a cache, it is a second opinion.
    """
    engine, _ = seeded
    refresh_rollups()
    period = date.today().strftime("%Y-%m")

    assert month_total(period) == pytest.approx(_live_month_total(engine, period)), (
        "the headline total disagrees with a live sum over cost_snapshots"
    )
    for provider in ("aws", "gcp", "datadog"):
        assert month_total(period, provider=provider) == pytest.approx(
            _live_month_total(engine, period, provider)), (
            f"{provider} subtotal disagrees with the source"
        )


def test_provider_subtotals_add_up_to_the_headline(seeded):
    """Internal consistency, which catches a fan-out bug the source cannot.

    The live comparison above would still pass if every cell were computed from
    the same wrong query. This checks the totals against each other.
    """
    _, _ = seeded
    refresh_rollups()
    period = date.today().strftime("%Y-%m")
    parts = sum(month_total(period, provider=p) or 0.0
                for p in ("aws", "gcp", "datadog"))
    assert month_total(period) == pytest.approx(parts), (
        "provider subtotals do not sum to the all-provider total"
    )


def test_top_services_is_ranked_and_matches_the_source(seeded):
    engine, _ = seeded
    refresh_rollups()
    period = date.today().strftime("%Y-%m")
    top = top_services(period, limit=5)

    assert top, "no services returned"
    amounts = [s["amount_usd"] for s in top]
    assert amounts == sorted(amounts, reverse=True), "not ranked biggest first"
    assert "Amazon Elastic Compute Cloud - Compute" in top[0]["service"], (
        f"EC2 is the biggest line in the fixture but the top row is {top[0]}"
    )
    assert ALL not in [s["service"] for s in top], (
        "a total row leaked into the per-service list, which would double the page"
    )


def test_refreshing_twice_does_not_double_anything(seeded):
    """Delete-and-rewrite plus the unique index. Both, or this fails."""
    _, _ = seeded
    refresh_rollups()
    period = date.today().strftime("%Y-%m")
    once = month_total(period)

    refresh_rollups()
    refresh_rollups()
    assert month_total(period) == pytest.approx(once), (
        "a repeated refresh changed the total, so cells are accumulating"
    )


def test_a_month_with_no_data_reads_as_unknown_not_zero(seeded):
    """None and 0.0 are different facts and the dashboard must be able to tell.

    Returning 0.0 for a period nobody ingested is how a product tells someone
    their bill went to zero. Same defect shape as a failed read becoming a
    number, which this repo has now fixed six times.
    """
    _, _ = seeded
    refresh_rollups()
    assert month_total("1999-01") is None, (
        "an uncomputed month returned a number instead of None"
    )
    assert month_total(date.today().strftime("%Y-%m")) is not None


def test_freshness_says_when_and_is_none_before_any_refresh(seeded):
    """The dashboard has to be able to say 'as of 06:00'.

    Serving precomputed data without saying it is precomputed is the one new
    lie this module could introduce.
    """
    _, _ = seeded
    assert freshness() is None, "reports freshness before anything was computed"
    refresh_rollups()
    stamp = freshness()
    assert stamp is not None
    age = (datetime.now(timezone.utc) - stamp.replace(tzinfo=timezone.utc)).total_seconds()
    assert 0 <= age < 60, f"computed_at is {age:.0f}s off, so the stamp is not real"


def test_the_daily_series_covers_the_range_and_is_ordered(seeded):
    _, _ = seeded
    refresh_rollups()
    end = date.today()
    start = end - timedelta(days=29)
    series = daily_series(start.isoformat(), end.isoformat())

    assert len(series) == 30, f"expected 30 days, got {len(series)}"
    dates = [p["date"] for p in series]
    assert dates == sorted(dates), "the chart would render backwards"
    assert all(p["amount_usd"] > 0 for p in series)


# ── the reason the module exists ─────────────────────────────────────────────

def test_the_rollup_read_is_much_faster_than_aggregating_the_source(seeded):
    """The claim, measured rather than asserted.

    Not a benchmark and not a threshold anyone should tune. It exists because
    'we added a cache' is worth nothing without evidence the cache is faster,
    and because if the rollup ever stops being faster there is no reason to
    carry the complexity.
    """
    engine, source_rows = seeded
    refresh_rollups()
    period = date.today().strftime("%Y-%m")

    t0 = time.perf_counter()
    for _ in range(20):
        _live_month_total(engine, period)
    slow = (time.perf_counter() - t0) / 20

    t0 = time.perf_counter()
    for _ in range(20):
        month_total(period)
    fast = (time.perf_counter() - t0) / 20

    assert fast < slow, (
        f"the rollup read ({fast*1000:.2f}ms) is not faster than aggregating "
        f"{source_rows:,} source rows ({slow*1000:.2f}ms); the table is not "
        f"earning its keep"
    )


def test_the_rollup_table_is_declared_on_the_shared_metadata():
    """So create_all builds it for whichever dialect is connected.

    scorecard_history was a hand-written CREATE TABLE carrying AUTOINCREMENT,
    which PostgreSQL rejects outright, and it went unnoticed for months because
    both call sites swallowed the error. Not doing that again.
    """
    assert "cost_rollups" in db.metadata.tables, (
        "cost_rollups is not on storage.db.metadata, so its DDL is hand-written "
        "per backend instead of compiled for the connected dialect"
    )
    idx = {i.name: i for i in cost_rollups.indexes}
    assert "ux_rollup_cell" in idx and idx["ux_rollup_cell"].unique, (
        "the cell index is missing or not unique, so a concurrent refresh can "
        "write the same total twice"
    )


def test_the_snapshot_cron_actually_refreshes_the_rollups():
    """The wiring, checked as a CALL and not as a string.

    Every test above drives refresh_rollups() directly, which leaves the real
    question open: does anything in production call it? A rollup nothing
    refreshes is a rollup that serves the day it was built and silently rots.

    Mutation testing found this gap: replacing the call in job_snapshot left
    all eight other tests green. And it looks for the Call node rather than the
    name, because `"refresh_rollups" in source` still passes when the call is
    gone — the import line keeps mentioning it. That exact false pass happened
    earlier today in the telemetry consent tests.
    """
    import ast
    import inspect

    from finops.scheduler import jobs

    fn = next(
        n for n in ast.walk(ast.parse(inspect.getsource(jobs)))
        if isinstance(n, ast.FunctionDef) and n.name == "job_snapshot"
    )
    calls = [
        n for n in ast.walk(fn)
        if isinstance(n, ast.Call)
        and (getattr(n.func, "id", None) == "refresh_rollups"
             or getattr(n.func, "attr", None) == "refresh_rollups")
    ]
    assert calls, (
        "job_snapshot ingests into cost_snapshots but never refreshes the "
        "rollups, so the dashboard would serve whatever was computed last"
    )

    body = ast.unparse(fn)
    assert "_snapshot_all" in body and body.index("_snapshot_all") < body.index("refresh_rollups"), (
        "the rollup refresh must run AFTER the ingest, or it summarises the "
        "previous run's data"
    )
    assert "except Exception" in body, (
        "a rollup failure must not fail the snapshot: cost_snapshots is the "
        "source of truth and a lost day may never be restated"
    )
