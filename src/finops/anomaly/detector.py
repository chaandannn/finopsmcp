from __future__ import annotations

import logging
import statistics
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from typing import Any

from sqlalchemy import and_, distinct, func, select
from sqlalchemy.exc import IntegrityError

from ..storage.db import anomalies, cost_snapshots, get_engine
from ..storage.snapshots import get_history

log = logging.getLogger(__name__)

_MIN_HISTORY_DAYS    = 7     # need at least 7 data points
_MIN_SPEND_THRESHOLD = 5.0   # ignore noise below $5
_Z_SCORE_THRESHOLD   = 2.0   # flag if |z| > 2.0
_PCT_THRESHOLD       = 20.0  # AND |pct_change| > 20%

# Tag keys checked for cost attribution when an AWS anomaly is detected.
# Ordered by how commonly they identify the responsible team / workload.
_DEFAULT_TAG_KEYS = [
    "team", "Team",
    "env", "environment", "Environment",
    "service", "Service",
    "owner", "Owner",
    "project", "Project",
    "cost-center", "CostCenter",
]


@dataclass
class AnomalyResult:
    provider: str
    service: str
    account_id: str
    snapshot_date: date
    severity: str           # "high" | "medium" | "low"
    direction: str          # "spike" | "drop"
    pct_change: float
    z_score: float
    baseline_mean: float
    current_amount: float
    is_new: bool = True
    # Which tag key=value pairs drove the change, ranked by delta_usd
    tag_drivers: list[dict[str, Any]] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)

    def summary(self) -> str:
        arrow = "↑" if self.direction == "spike" else "↓"
        base = (
            f"{self.provider.upper()} / {self.service}: "
            f"{arrow} {abs(self.pct_change):.0f}% vs 28-day baseline "
            f"(${self.current_amount:,.2f} vs avg ${self.baseline_mean:,.2f}) "
            f"[{self.severity.upper()}]"
        )
        if self.tag_drivers:
            top = self.tag_drivers[0]
            base += (
                f" — driven by {top['tag_key']}={top['tag_value']} "
                f"(+${top['delta_usd']:,.0f}, {top['pct_of_anomaly']:.0f}% of spike)"
            )
        return base


def _severity(z: float, pct: float) -> str:
    az, ap = abs(z), abs(pct)
    if az >= 3.5 or ap >= 100:
        return "high"
    if az >= 2.5 or ap >= 50:
        return "medium"
    return "low"


def detect_for_series(
    provider: str,
    service: str,
    account_id: str,
    snapshot_date: date,
    current_amount: float,
    history_amounts: list[float],
) -> AnomalyResult | None:
    if len(history_amounts) < _MIN_HISTORY_DAYS:
        return None
    if current_amount < _MIN_SPEND_THRESHOLD and max(history_amounts, default=0) < _MIN_SPEND_THRESHOLD:
        return None

    mean = statistics.mean(history_amounts)
    if mean < _MIN_SPEND_THRESHOLD:
        return None

    stdev = statistics.stdev(history_amounts) if len(history_amounts) > 1 else 0.0
    z_score = (current_amount - mean) / stdev if stdev > 0 else 0.0
    pct_change = (current_amount - mean) / mean * 100

    if abs(z_score) < _Z_SCORE_THRESHOLD or abs(pct_change) < _PCT_THRESHOLD:
        return None

    return AnomalyResult(
        provider=provider,
        service=service,
        account_id=account_id,
        snapshot_date=snapshot_date,
        severity=_severity(z_score, pct_change),
        direction="spike" if pct_change > 0 else "drop",
        pct_change=round(pct_change, 2),
        z_score=round(z_score, 3),
        baseline_mean=round(mean, 4),
        current_amount=round(current_amount, 4),
    )


def get_tag_drivers(
    service: str,
    snapshot_date: date,
    delta_usd: float,
    tag_keys: list[str] | None = None,
) -> list[dict[str, Any]]:
    """
    Query Cost Explorer to find which tag key=value pairs drove a cost spike.

    Returns a list of drivers sorted by delta_usd descending, e.g.:
        [
            {"tag_key": "team", "tag_value": "platform",
             "current_usd": 4100, "baseline_usd": 1810,
             "delta_usd": 2290, "pct_of_anomaly": 78.4},
            ...
        ]

    Only called for AWS (Cost Explorer is AWS-specific). Non-fatal — returns []
    if CE is unavailable or the service name doesn't match CE's naming.
    """
    if delta_usd <= 0:
        return []

    try:
        import boto3
    except ImportError:
        return []

    keys_to_check = tag_keys or _DEFAULT_TAG_KEYS
    # Deduplicate while preserving order (handles "team"/"Team" variants)
    seen: set[str] = set()
    unique_keys: list[str] = []
    for k in keys_to_check:
        lk = k.lower()
        if lk not in seen:
            seen.add(lk)
            unique_keys.append(k)

    # Current window: 7 days ending on snapshot_date
    end_dt      = snapshot_date + timedelta(days=1)   # CE end is exclusive
    start_dt    = snapshot_date - timedelta(days=6)
    # Baseline window: same weekday, 4 weeks prior
    base_end_dt = start_dt - timedelta(days=21)
    base_start_dt = base_end_dt - timedelta(days=7)

    end_str       = end_dt.isoformat()
    start_str     = start_dt.isoformat()
    base_end_str  = base_end_dt.isoformat()
    base_start_str = base_start_dt.isoformat()

    try:
        ce = boto3.client("ce", region_name="us-east-1")
    except Exception as e:
        log.debug("Cost Explorer client creation failed: %s", e)
        return []

    drivers: list[dict[str, Any]] = []

    for tag_key in unique_keys:
        try:
            def _query(start: str, end: str) -> dict[str, float]:
                resp = ce.get_cost_and_usage(
                    TimePeriod={"Start": start, "End": end},
                    Granularity="MONTHLY",
                    Filter={
                        "Dimensions": {
                            "Key": "SERVICE",
                            "Values": [service],
                        }
                    },
                    GroupBy=[{"Type": "TAG", "Key": tag_key}],
                    Metrics=["UnblendedCost"],
                )
                totals: dict[str, float] = {}
                for result in resp.get("ResultsByTime", []):
                    for group in result.get("Groups", []):
                        # Keys come back as "TagKey$TagValue"
                        raw_key = group["Keys"][0]
                        tag_val = raw_key.split("$", 1)[-1] if "$" in raw_key else raw_key
                        tag_val = tag_val or "(untagged)"
                        amount  = float(group["Metrics"]["UnblendedCost"]["Amount"])
                        totals[tag_val] = totals.get(tag_val, 0.0) + amount
                return totals

            current_map  = _query(start_str,      end_str)
            baseline_map = _query(base_start_str, base_end_str)

            for tag_val, current_amt in current_map.items():
                baseline_amt = baseline_map.get(tag_val, 0.0)
                delta = current_amt - baseline_amt
                if delta < 1.0:
                    continue
                drivers.append({
                    "tag_key":        tag_key,
                    "tag_value":      tag_val,
                    "current_usd":    round(current_amt, 2),
                    "baseline_usd":   round(baseline_amt, 2),
                    "delta_usd":      round(delta, 2),
                    "pct_of_anomaly": round(delta / delta_usd * 100, 1),
                })

        except Exception as e:
            log.debug("Tag attribution failed for key %r: %s", tag_key, e)
            continue

    # Sort by contribution and drop duplicates across tag key variants (team/Team)
    drivers.sort(key=lambda d: d["delta_usd"], reverse=True)
    return drivers[:10]


def detect_from_snapshot(
    provider: str,
    service: str,
    account_id: str,
    snapshot_date: date,
    current_amount: float,
    lookback_days: int = 28,
    enrich_tags: bool = True,
) -> AnomalyResult | None:
    history   = get_history(provider, service, account_id, days=lookback_days)
    today_iso = snapshot_date.isoformat()
    amounts   = [
        row["amount_usd"]
        for row in history
        if row["snapshot_date"] != today_iso and row["amount_usd"] > 0
    ]
    result = detect_for_series(provider, service, account_id, snapshot_date, current_amount, amounts)

    # Enrich AWS anomalies with tag-level attribution
    if result is not None and provider == "aws" and enrich_tags and result.direction == "spike":
        delta = current_amount - result.baseline_mean
        result.tag_drivers = get_tag_drivers(service, snapshot_date, delta)

    return result


def persist_anomaly(result: AnomalyResult) -> tuple[int, bool]:
    """Persist an anomaly idempotently. Returns (id, is_new).

    Dedups on (provider, service, account_id, snapshot_date, direction) so a cron
    retry, the run_anomaly_check_now tool, or a fail-open second scheduler process
    cannot re-insert the same spend event or re-fire its alerts and tickets. Uses
    inserted_primary_key (not the SQLite-only lastrowid, which returns None on
    Postgres, the shared-team mode the Startups tier sells).
    """
    engine = get_engine()
    dedup = and_(
        anomalies.c.provider == result.provider,
        anomalies.c.service == result.service,
        anomalies.c.account_id == result.account_id,
        anomalies.c.snapshot_date == result.snapshot_date.isoformat(),
        anomalies.c.direction == result.direction,
    )

    def _lookup(conn) -> int | None:
        row = conn.execute(select(anomalies.c.id).where(dedup).limit(1)).first()
        return int(row[0]) if row is not None else None

    with engine.begin() as conn:
        existing = _lookup(conn)
        if existing is not None:
            return existing, False

        try:
            r = conn.execute(
                anomalies.insert().values(
                    provider=result.provider,
                    service=result.service,
                    account_id=result.account_id,
                    detected_at=datetime.now(timezone.utc),
                    snapshot_date=result.snapshot_date.isoformat(),
                    severity=result.severity,
                    direction=result.direction,
                    pct_change=result.pct_change,
                    z_score=result.z_score,
                    baseline_mean=result.baseline_mean,
                    current_amount=result.current_amount,
                    acknowledged=False,
                    notified=False,
                )
            )
            return int(r.inserted_primary_key[0]), True
        except IntegrityError:
            # Somebody else inserted the same spend event between our SELECT and
            # our INSERT. The ux_anom_dedup unique index is what turns that into
            # a catchable error instead of a second row, and this is the half
            # that decides what the caller is told.
            #
            # `False` matters more than the id. scheduler/jobs.py keys every
            # downstream action on is_new: Slack, Teams, the n8n push, and the
            # ticket. Reporting True here would fire all four a second time for
            # one spend event, which is the exact outcome the docstring above
            # promises cannot happen. The row exists and the alerts already
            # fired; our job is to say so, not to say we created it.
            #
            # Re-read on a FRESH connection. This transaction is poisoned by the
            # failed statement, and on PostgreSQL every subsequent statement in
            # it raises InFailedSqlTransaction, so the lookup has to happen
            # outside it.
            pass

    with engine.connect() as conn:
        winner = _lookup(conn)
    if winner is not None:
        return winner, False
    # The unique index rejected the insert and the row is not there to be found,
    # which means the collision was on something other than the dedup tuple.
    # Raising beats returning a fabricated id: a caller that believes it has an
    # anomaly id will write it into a ticket that points at nothing.
    raise RuntimeError(
        "anomaly insert was rejected as a duplicate but no matching row exists "
        f"for {result.provider}/{result.service}/{result.account_id} "
        f"{result.snapshot_date.isoformat()} {result.direction}"
    )


def snapshot_history_days(provider: str | None = None) -> int:
    """Count how many distinct days of cost snapshots we have on hand.

    This is the honest measure of whether we can detect anomalies at all. The
    detector needs a baseline, so a fresh account with a day or two of data
    cannot produce a real all-clear, only "not enough history yet".
    """
    engine = get_engine()
    query = select(func.count(distinct(cost_snapshots.c.snapshot_date)))
    if provider:
        query = query.where(cost_snapshots.c.provider == provider)
    with engine.connect() as conn:
        return int(conn.execute(query).scalar() or 0)


def has_enough_history(provider: str | None = None) -> bool:
    """True once there are enough days of snapshots to trust a detection result."""
    return snapshot_history_days(provider) >= _MIN_HISTORY_DAYS


def get_active_anomalies(
    provider: str | None = None,
    severity: str | None = None,
    limit: int = 50,
) -> list[dict[str, Any]]:
    engine = get_engine()
    query = (
        select(anomalies)
        .where(anomalies.c.acknowledged == False)  # noqa: E712
        .order_by(anomalies.c.detected_at.desc())
        .limit(limit)
    )
    if provider:
        query = query.where(anomalies.c.provider == provider)
    if severity:
        query = query.where(anomalies.c.severity == severity)
    with engine.connect() as conn:
        return [dict(r._mapping) for r in conn.execute(query).fetchall()]


def acknowledge_anomaly(anomaly_id: int) -> bool:
    engine = get_engine()
    with engine.begin() as conn:
        result = conn.execute(
            anomalies.update()
            .where(anomalies.c.id == anomaly_id)
            .values(acknowledged=True)
        )
        return result.rowcount > 0


def mark_notified(anomaly_id: int) -> None:
    engine = get_engine()
    with engine.begin() as conn:
        conn.execute(
            anomalies.update()
            .where(anomalies.c.id == anomaly_id)
            .values(notified=True)
        )
