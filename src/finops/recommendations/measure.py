"""
Realized-savings measurement for verified recommendations.

A verifier (verifiers.py) answers "did the change actually happen?". This
module answers the harder question: "what is the change actually worth?"
It prices a confirmed change on the best data available, in strict order:

  1. bill_measured   CUR before/after: the resource's real unblended cost in
                     the window before the change vs after it settled. This is
                     money measured off the bill, not an estimate.
  2. effective_rate  The type-delta estimate adjusted to the customer's
                     measured effective rate (EDP/private pricing + commitment
                     coverage), the same basis the recommendation side uses.
  3. list_price      The public on-demand delta. Last resort, clearly labeled.

The returned basis is persisted on the row (verified_basis) so the ledger and
the quality signal can distinguish "measured off your bill" from "estimated".
"""
from __future__ import annotations

import logging
from datetime import date, datetime, timedelta, timezone
from typing import Any

log = logging.getLogger(__name__)

# The bill windows. Before: the two weeks ending the day before action. After:
# from the settling buffer to now. Short windows track the change, long enough
# to smooth daily jitter.
_WINDOW_DAYS = 14
# Skip the first day after acted_on: partial-day billing and in-flight
# restarts make it noise, not signal.
_SETTLE_DAYS = 1
# Require at least this many settled days of post-change data before trusting
# a bill measurement; otherwise fall back to the rate-based estimate.
_MIN_AFTER_DAYS = 7

DAYS_PER_MONTH = 30.4


def _utc(dt: datetime | None) -> datetime | None:
    if dt is None:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt


def _resource_daily_cost(resource_id: str, start: date, end: date) -> float | None:
    """Mean daily unblended cost for one resource over [start, end], from CUR.
    None when CUR is not configured, errors, or has no rows for the resource."""
    try:
        from ..connectors import cur
        if not cur.is_configured():
            return None
        result = cur.get_resource_costs(
            start_date=start, end_date=end,
            min_cost_usd=0.0, limit=5, resource_id=resource_id,
        )
        if result.get("error"):
            return None
        rows = result.get("resources") or []
        if not rows:
            return None
        total = sum(float(r.get("unblended_cost") or 0.0) for r in rows)
        days = max(1, (end - start).days + 1)
        return total / days
    except Exception as exc:
        log.debug("bill measurement unavailable for %s: %s", resource_id, exc)
        return None


def _bill_measured(row: Any) -> float | None:
    """CUR before/after delta for the row's resource, or None when the data
    can't support a measurement yet (too fresh, no CUR, resource missing)."""
    acted_at = _utc(getattr(row, "acted_on_at", None))
    if acted_at is None:
        return None
    now = datetime.now(timezone.utc)
    after_start = (acted_at + timedelta(days=_SETTLE_DAYS)).date()
    after_end = now.date()
    if (after_end - after_start).days + 1 < _MIN_AFTER_DAYS:
        return None  # not enough settled data yet; next run may have it

    before_end = (acted_at - timedelta(days=1)).date()
    before_start = before_end - timedelta(days=_WINDOW_DAYS - 1)

    before_daily = _resource_daily_cost(row.resource_id, before_start, before_end)
    after_daily = _resource_daily_cost(row.resource_id, after_start, after_end)
    if before_daily is None or after_daily is None:
        return None

    monthly_delta = (before_daily - after_daily) * DAYS_PER_MONTH
    # A negative delta means the bill went UP after the change: record 0 rather
    # than a negative "saving", and leave the trail in the log.
    if monthly_delta < 0:
        log.info(
            "bill measurement for %s shows cost increased after change "
            "(%.2f/day -> %.2f/day); recording 0", row.resource_id, before_daily, after_daily,
        )
        return 0.0
    return round(monthly_delta, 2)


def _effective_rate(list_estimate: float, row: Any) -> tuple[float, str] | None:
    """Adjust the list-price estimate to the customer's measured effective rate.
    Returns (usd, basis) or None when no rate data is available."""
    if list_estimate <= 0:
        return None
    from .effective_savings import adjust_savings, detect_savings_context

    try:
        ctx = detect_savings_context()
        adjusted = adjust_savings(list_estimate, resource_type=getattr(row, "resource_type", None), ctx=ctx)
    except Exception as exc:
        # Genuinely absent rate data: no CUR, no snapshots, a provider that will
        # not answer. Tier 3 (list price) is the correct fallback and the caller
        # labels it honestly. This except covers the LOOKUP only.
        log.debug("effective-rate adjustment unavailable: %s", exc)
        return None

    # Deliberately outside the try. This read used to say `adjusted.effective`,
    # a field AdjustedSavings has never had, and the resulting AttributeError was
    # caught above and logged at DEBUG. Tier 2 of a documented three-tier ladder
    # was therefore dead on every install since it shipped, and every confirmed
    # downsize banked at full list price: for a customer on a 35% EDP, $280.32
    # where the truth was $182.21. Nothing failed, no test went red, and the
    # number was simply wrong by exactly the customer's discount.
    #
    # A typo in a field name is a coding error, not a missing-data condition, and
    # it must not be able to hide in the same handler as "this customer has no
    # CUR". Attribute access on the result stays out here where it can raise.
    if adjusted.basis in ("effective_rate", "commitment_coverage"):
        return round(adjusted.effective_savings, 2), "effective_rate"
    return None


def measure_realized_savings(row: Any, confirmed_estimate: float | None) -> tuple[float, str]:
    """
    Price a change that a verifier has already confirmed happened.

    `confirmed_estimate` is the verifier's own figure (usually a list-price
    type delta; may be 0.0 when the type is not in the static price table).
    Returns (monthly_usd, basis).
    """
    # Tier 1: the bill itself.
    measured = _bill_measured(row)
    if measured is not None:
        return measured, "bill_measured"

    # A dead verifier estimate (unknown type priced the delta at 0) must not
    # bank $0: fall back to the recommendation's original estimate.
    list_estimate = confirmed_estimate if (confirmed_estimate or 0) > 0 else float(
        getattr(row, "estimated_monthly_savings_usd", 0.0) or 0.0
    )

    # Tier 2: the customer's real rates.
    adjusted = _effective_rate(list_estimate, row)
    if adjusted is not None:
        return adjusted

    # Tier 3: list price, labeled as such.
    return round(list_estimate, 2), "list_price"
