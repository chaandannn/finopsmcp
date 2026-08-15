"""Day-one anomaly baselines.

The detector needs 7 days of snapshot history, so a fresh install's flagship
spike detection is empty for a week, exactly while the user decides whether
nable is worth keeping. Cost Explorer already holds months of daily per-service
history, so we backfill the baseline from CE in one call and anomalies work on
day one.

Idempotent (store_snapshot upserts per provider/service/account/date) and
self-limiting: it runs only when the existing history is thinner than the
detector's minimum, so an instance with a real snapshot habit never re-pulls.
One CE call per run (about $0.01 of AWS API cost, roughly the same as the daily
snapshot job itself).
"""
from __future__ import annotations

import logging
from datetime import date, timedelta

log = logging.getLogger(__name__)

# The detector wants 7 points; pull a little more so day-one detection has slack.
_TARGET_DAYS = 14
# Skip sub-cent rows so a sprawling account doesn't backfill thousands of $0 lines.
_MIN_AMOUNT = 0.01


def _distinct_snapshot_days(provider: str = "aws") -> int:
    from sqlalchemy import distinct, func, select

    from ..storage.db import cost_snapshots, get_engine

    with get_engine().connect() as conn:
        return conn.execute(
            select(func.count(distinct(cost_snapshots.c.snapshot_date))).where(
                cost_snapshots.c.provider == provider
            )
        ).scalar() or 0


def needs_backfill() -> bool:
    from .detector import _MIN_HISTORY_DAYS

    try:
        return _distinct_snapshot_days("aws") < _MIN_HISTORY_DAYS
    except Exception:
        return False


def backfill_from_cost_explorer(days: int = _TARGET_DAYS) -> dict:
    """Pull daily per-service AWS spend for the last ``days`` and store it as
    snapshots. Returns {backfilled_days, rows} or {skipped: reason}."""
    if not needs_backfill():
        return {"skipped": "history already sufficient"}

    try:
        import boto3

        from ..storage.snapshots import store_snapshot

        # Through the gate, not around it. This runs from _snapshot_all on every
        # scheduled snapshot and paginates, so it is not one Cost Explorer
        # request on a timer, it is several. Routed here it refuses in an
        # unattended context and the CUR covers the same history for free.
        from ..billing_access import ce_client

        ce = ce_client(region="us-east-1", reason="anomaly backfill")
        sts_account = boto3.client("sts").get_caller_identity()["Account"]
        end = date.today()
        start = end - timedelta(days=days)

        rows = 0
        seen_days: set[str] = set()
        token: str | None = None
        while True:
            kwargs = dict(
                TimePeriod={"Start": start.isoformat(), "End": end.isoformat()},
                Granularity="DAILY",
                GroupBy=[{"Type": "DIMENSION", "Key": "SERVICE"}],
                Metrics=["UnblendedCost"],
            )
            if token:
                kwargs["NextPageToken"] = token
            resp = ce.get_cost_and_usage(**kwargs)
            for period in resp.get("ResultsByTime", []):
                day = period.get("TimePeriod", {}).get("Start", "")
                if not day:
                    continue
                for group in period.get("Groups", []):
                    amount = float(group["Metrics"]["UnblendedCost"]["Amount"])
                    if amount < _MIN_AMOUNT:
                        continue
                    store_snapshot(
                        provider="aws",
                        service=group["Keys"][0],
                        account_id=sts_account,
                        region="",
                        snapshot_date=date.fromisoformat(day),
                        amount_usd=round(amount, 4),
                    )
                    rows += 1
                    seen_days.add(day)
            token = resp.get("NextPageToken")
            if not token:
                break

        log.info("anomaly baseline backfilled: %d rows across %d days", rows, len(seen_days))
        return {"backfilled_days": len(seen_days), "rows": rows}
    except Exception as exc:
        # Best-effort by design: a missing permission or throttle must never
        # break the flow that triggered the backfill.
        log.debug("baseline backfill skipped: %s", exc)
        return {"skipped": str(exc)}
