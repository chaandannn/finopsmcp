"""Reservations locked to one subscription while a sibling pays on demand.

An Azure reservation scoped to a single subscription only discounts resources in
that subscription. Matching resources anywhere else in the same billing account
pay full price, and the reservation's unused hours are simply lost. Changing the
scope to Shared is free and instant: Settings, Configuration, change the scope.
No exchange, no refund, no new commercial transaction.

WHY THIS NEEDS THREE CONDITIONS, NOT ONE.

Single scope on its own is not waste, and a detector that says otherwise produces
confident false positives on deliberate architecture. Microsoft documents two
legitimate reasons to choose it:

  - Capacity priority. Shared scope forces instance-size flexibility on, so
    single scope is how you reserve actual datacenter capacity for a specific VM
    size. That is an availability decision, not a mistake.
  - Chargeback. Keeping the discount inside one business unit's subscription is a
    real accounting requirement.

So a finding requires all three:

  1. the reservation is scoped to a single subscription (or resource group)
  2. it is measurably underutilised over the window
  3. a subscription the reservation CANNOT reach spent money on the same meter,
     in the same region, over the same window

Only (3) turns a config observation into money, and it is the part no native tool
computes: Azure Advisor reports utilisation, never the counterfactual.

EVIDENCE GRADING. The wasted hours and the sibling spend are both measured from
the API. What is inferred is that the discount would actually have applied, which
depends on instance-size-flexibility rules we cannot fully evaluate from billing
data. So a finding is MEASURED only when the sibling usage matches on meter AND
region; anything looser is an INFERRED investigation carrying a size band rather
than a precise figure. The trust envelope enforces the rest.

Read-only throughout. This module proposes a portal setting change and never
makes one.
"""
from __future__ import annotations

import logging
from typing import Any

from .envelope import INFERRED, MEASURED, Finding

log = logging.getLogger(__name__)

# Below this, the reservation is doing its job and the scope is not the problem.
DEFAULT_UTILIZATION_FLOOR_PCT = 90.0

# Sibling spend under this is noise: a few dollars of drift is not worth a
# recommendation, and acting on it costs more attention than it returns.
DEFAULT_MIN_SIBLING_SPEND_USD = 25.0

# Scope values Azure reports. "Shared" is the one that needs no attention.
_SINGLE_SCOPES = ("single", "resourcegroup", "singleresourcegroup", "subscription")


def _norm(value: Any) -> str:
    return str(value or "").strip().lower()


def _scope_is_narrow(applied_scope_type: Any) -> bool:
    """True when the reservation can only reach part of the billing account.

    ManagementGroup is deliberately NOT narrow: it already spans subscriptions,
    and Azure auto-converts it to Shared when the last subscription leaves it.
    """
    return _norm(applied_scope_type) in _SINGLE_SCOPES


def _reachable_subscriptions(reservation: dict) -> set[str]:
    """The subscription ids this reservation's discount can actually apply to.

    Azure returns applied scopes as full resource paths
    (/subscriptions/<id>/resourceGroups/<rg>); we compare on the subscription id
    alone, because a reservation scoped to a resource group still cannot reach a
    *different* subscription, which is the question this module asks.
    """
    out: set[str] = set()
    for scope in reservation.get("applied_scopes") or []:
        text = _norm(scope)
        if "/subscriptions/" in text:
            tail = text.split("/subscriptions/", 1)[1]
            out.add(tail.split("/", 1)[0])
        elif text:
            out.add(text)
    return {s for s in out if s}


def _hourly_value(reservation: dict) -> float | None:
    """What one reserved hour is worth, so wasted hours become dollars.

    Returns None rather than guessing: a fabricated rate would produce a
    confident wrong figure, which is worse than an honest band.
    """
    for key in ("hourly_rate_usd", "reserved_hour_rate_usd"):
        rate = reservation.get(key)
        if isinstance(rate, (int, float)) and rate > 0:
            return float(rate)
    total = reservation.get("reserved_cost_usd")
    hours = reservation.get("reserved_hours")
    if (isinstance(total, (int, float)) and total > 0
            and isinstance(hours, (int, float)) and hours > 0):
        return float(total) / float(hours)
    return None


def _matches(reservation: dict, usage: dict, *, strict: bool) -> bool:
    """Could this sibling usage have absorbed the reservation's spare capacity?

    strict=True demands meter AND region agreement, which is what earns a
    measured figure. strict=False allows a region-blind meter match, which still
    signals something worth investigating but not a precise claim.
    """
    if _norm(reservation.get("meter_id")) and _norm(usage.get("meter_id")):
        if _norm(reservation.get("meter_id")) != _norm(usage.get("meter_id")):
            return False
    elif _norm(reservation.get("sku_name")) != _norm(usage.get("sku_name")):
        return False

    if strict:
        r_region, u_region = _norm(reservation.get("region")), _norm(usage.get("region"))
        # An unknown region on either side cannot be called a strict match.
        if not r_region or not u_region or r_region != u_region:
            return False
    return True


def find_scope_limited_reservations(
    reservations: list[dict],
    sibling_usage: list[dict],
    *,
    utilization_floor_pct: float = DEFAULT_UTILIZATION_FLOOR_PCT,
    min_sibling_spend_usd: float = DEFAULT_MIN_SIBLING_SPEND_USD,
) -> list[Finding]:
    """All three conditions, or no finding.

    reservations:   [{reservation_id, sku_name, meter_id, region, applied_scope_type,
                      applied_scopes, avg_utilization_pct, wasted_hours,
                      reserved_hours, reserved_cost_usd | hourly_rate_usd}]
    sibling_usage:  [{subscription_id, meter_id, sku_name, region, on_demand_cost_usd}]
                    on-demand (undiscounted) spend per subscription per meter over
                    the same window.
    """
    findings: list[Finding] = []

    for res in reservations or []:
        # ── condition 1: the scope is narrow ──────────────────────────────────
        if not _scope_is_narrow(res.get("applied_scope_type")):
            continue

        # ── condition 2: it is actually being wasted ──────────────────────────
        util = res.get("avg_utilization_pct")
        wasted_hours = float(res.get("wasted_hours") or 0.0)
        if not isinstance(util, (int, float)) or util >= utilization_floor_pct:
            continue
        if wasted_hours <= 0:
            continue

        # ── condition 3: someone it cannot reach paid full price ──────────────
        reachable = _reachable_subscriptions(res)
        strict_hits, loose_hits = [], []
        for usage in sibling_usage or []:
            sub = _norm(usage.get("subscription_id"))
            if not sub or sub in reachable:
                continue                       # the reservation already covers it
            spend = float(usage.get("on_demand_cost_usd") or 0.0)
            if spend < min_sibling_spend_usd:
                continue
            if _matches(res, usage, strict=True):
                strict_hits.append(usage)
            elif _matches(res, usage, strict=False):
                loose_hits.append(usage)

        if not strict_hits and not loose_hits:
            continue

        hits = strict_hits or loose_hits
        evidence = MEASURED if strict_hits else INFERRED
        sibling_spend = sum(float(h.get("on_demand_cost_usd") or 0.0) for h in hits)
        subs = sorted({_norm(h.get("subscription_id")) for h in hits})

        # The recoverable amount is bounded by BOTH sides: you cannot save more
        # than the reservation wasted, and you cannot save more than the sibling
        # actually spent. Taking either number alone overstates it.
        rate = _hourly_value(res)
        wasted_value = wasted_hours * rate if rate else None
        recoverable = (min(wasted_value, sibling_spend)
                       if wasted_value is not None else None)

        sku = res.get("sku_name") or res.get("meter_id") or "reservation"
        others = f"{len(subs)} other subscription(s)" if len(subs) > 1 else "another subscription"

        findings.append(Finding(
            source="azure_reservation_scope",
            title=f"Reservation for {sku} is locked to one subscription while {others} pays full price",
            why=(
                f"This reservation is scoped to a single subscription, so its discount "
                f"cannot reach anything else. It ran at {util:.0f}% utilisation and left "
                f"{wasted_hours:,.0f} reserved hours unused, while {others} it cannot reach "
                f"spent ${sibling_spend:,.0f} on the same capacity at full price. "
                f"Changing the scope to Shared is free and takes effect immediately."
            ),
            evidence=evidence,
            confidence="high" if strict_hits else "medium",
            why_unsure=("" if strict_hits else
                        "The sibling usage matches on meter but the region could not be "
                        "confirmed on both sides, so the discount may not have applied to "
                        "all of it."),
            assumptions=[
                "Sibling spend is on-demand (undiscounted) usage over the same window.",
                "Shared scope would apply the reservation to that usage under Azure's "
                "instance-size-flexibility rules.",
            ],
            remediation=[
                "Azure portal, Reservations, select this reservation, Settings, "
                "Configuration, change scope to Shared.",
                "This is free: no exchange, no refund, no new commercial transaction.",
                "If the single scope was deliberate for capacity priority, leave it: "
                "Shared scope turns instance-size flexibility on and gives up reserved "
                "datacenter capacity for a specific size.",
            ],
            confirm_steps=[
                f"Check this reservation's utilisation trend in the portal; it should "
                f"read about {util:.0f}%.",
                f"Confirm subscription(s) {', '.join(subs[:3])} run matching workloads "
                f"in the same region.",
            ],
            est_monthly_savings=recoverable if evidence == MEASURED else None,
            rough_monthly=recoverable if evidence != MEASURED else None,
            resource_id=str(res.get("reservation_id") or ""),
            metadata={
                "applied_scope_type": res.get("applied_scope_type"),
                "reachable_subscriptions": sorted(reachable),
                "uncovered_subscriptions": subs,
                "avg_utilization_pct": round(float(util), 2),
                "wasted_hours": round(wasted_hours, 2),
                "wasted_value_usd": round(wasted_value, 2) if wasted_value is not None else None,
                "sibling_on_demand_usd": round(sibling_spend, 2),
                "match": "meter+region" if strict_hits else "meter only",
                "region": res.get("region"),
                "sku_name": res.get("sku_name"),
                # The fix is a portal setting, not a resource change, so there is
                # nothing for the resource map to find. Say so explicitly rather
                # than letting an empty map read as "nothing depends on this".
                "remediation_kind": "billing_configuration",
            },
        ))

    return findings
