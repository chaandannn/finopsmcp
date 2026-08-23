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


def backfill_from_cost_explorer(days: int = _TARGET_DAYS, *,
                                explicit: bool = False) -> dict:
    """Fill in missing daily history, from the free source unless told otherwise.

    Two sources cover the same days and they are not equivalent to the customer.
    The CUR is an export that already exists in their bucket, so reading it costs
    them nothing. Cost Explorer bills per request, and this function paginates,
    so a ninety-day backfill is a charge on their account that nobody asked for.

    So: CUR whenever it is configured, and Cost Explorer only when a person has
    explicitly asked for it. ``explicit=False`` reports that the option exists
    rather than taking it, which is what lets the caller offer it as a choice.

    Returns {source, backfilled_days, rows}, or {skipped: reason} with
    ``available`` set when the only remaining route is one that costs money.
    """
    if not needs_backfill():
        return {"skipped": "history already sufficient"}

    # The free source first, always. Reached through the seam, so it is simply
    # absent on an open install and this falls through.
    try:
        from ..connectors import cur_s3

        if cur_s3.is_configured():
            end = date.today()
            out = cur_s3.ingest_range(end - timedelta(days=days), end)
            rows = out.get("rows", 0) if isinstance(out, dict) else 0
            log.info("backfill: %s rows from the CUR, no Cost Explorer requests", rows)
            return {"source": "cur", "rows": rows, "backfilled_days": days}
    except ImportError:
        pass
    except Exception as exc:  # noqa: BLE001 - fall through to the decision below
        log.warning("backfill: the CUR read failed (%s); not falling back to a "
                    "billed source on its own", exc)

    if not explicit:
        # Deliberately does nothing. Cost Explorer is the customer's money, and
        # spending it silently on history they have not asked to see is the kind
        # of charge that turns up on a bill with no explanation attached.
        return {"skipped": "backfilling from Cost Explorer bills the account, "
                           "so it waits to be asked",
                "available": True, "source": "cost_explorer", "days": days}

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
