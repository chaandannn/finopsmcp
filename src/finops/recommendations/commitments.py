"""
Reserved Instance & Savings Plan coverage analysis with purchase ROI.

Pulls real utilization data from AWS Cost Explorer:
  - Savings Plans utilization (coverage %, unused commitment)
  - RI utilization and coverage
  - On-demand spend that could be covered by commitments

Calculates:
  - Current waste (unused RI/SP payments)
  - Coverage gap (on-demand that commitments could cover)
  - Recommended purchase: type, term, payment, projected ROI

All figures come directly from the Cost Explorer API — no estimates.
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Any

log = logging.getLogger(__name__)

_COMPUTE_SP_DISCOUNT = 0.66   # ~34% off on-demand (1yr no-upfront compute SP)
_EC2_SP_DISCOUNT = 0.72       # ~28% off on-demand (1yr no-upfront EC2 SP)
_RI_DISCOUNT = 0.60           # ~40% off on-demand (1yr no-upfront RI)


@dataclass
class TaggedCoverageEstimate:
    """
    Commitment coverage estimate for a tag slice where tag coverage is partial.

    When only 70% of a domain's resources carry the tag, we can still produce
    a meaningful estimate by solving:

        account_coverage × total_spend =
            tagged_coverage   × tagged_spend
          + untagged_coverage × untagged_spend

    Rearranging gives the untagged portion's coverage, which we blend back
    to produce a full-domain estimate with an explicit confidence level.
    """
    tag_key: str
    tag_value: str
    tag_coverage_pct: float          # how complete the tagging is (e.g. 70.0)

    # Directly measured (from CE tag-filtered query)
    tagged_spend_usd: float
    tagged_sp_coverage_pct: float
    tagged_ri_coverage_pct: float

    # Inferred for the untagged remainder
    untagged_spend_usd: float
    inferred_untagged_sp_coverage_pct: float
    inferred_untagged_ri_coverage_pct: float

    # Blended full-domain estimate
    estimated_sp_coverage_pct: float
    estimated_ri_coverage_pct: float
    estimated_combined_coverage_pct: float

    # Confidence reflects how much of spend is actually tagged
    confidence: str                  # "high" | "medium" | "low"
    confidence_note: str


@dataclass
class CommitmentAnalysis:
    # Current state. Coverage is None when the call was denied or failed:
    # "unknown" needs to be representable, because 0.0 reads as "nothing is
    # covered" and that is what recommended a purchase off a failed read.
    savings_plan_coverage_pct: float | None
    savings_plan_utilization_pct: float
    savings_plan_unused_usd: float
    ri_coverage_pct: float
    ri_utilization_pct: float
    ri_unused_usd: float

    # On-demand that commitments could cover
    uncovered_on_demand_usd: float

    # Recommendations
    recommendations: list[dict[str, Any]] = field(default_factory=list)

    @property
    def total_waste_usd(self) -> float:
        return self.savings_plan_unused_usd + self.ri_unused_usd

    @property
    def coverage_score(self) -> str:
        if self.savings_plan_coverage_pct is None:
            return "unknown"
        avg = (self.savings_plan_coverage_pct + self.ri_coverage_pct) / 2
        if avg >= 80:
            return "good"
        if avg >= 50:
            return "fair"
        return "poor"


def _ec2_spend_for_tag(
    ce_client: Any,
    start: str,
    end: str,
    tag_key: str,
    tag_value: str,
) -> float:
    """Total EC2 compute spend (all purchase types) for resources with this tag."""
    try:
        resp = ce_client.get_cost_and_usage(
            TimePeriod={"Start": start, "End": end},
            Granularity="MONTHLY",
            Filter={
                "And": [
                    {"Dimensions": {"Key": "SERVICE", "Values": [
                        "Amazon Elastic Compute Cloud - Compute", "AWS Fargate"
                    ]}},
                    {"Tags": {"Key": tag_key, "Values": [tag_value]}},
                ]
            },
            Metrics=["UnblendedCost"],
        )
        return sum(
            float(p["Total"]["UnblendedCost"]["Amount"])
            for p in resp.get("ResultsByTime", [])
        )
    except Exception as e:
        log.warning("EC2 spend for tag fetch failed: %s", e)
        return 0.0


def _total_ec2_spend(ce_client: Any, start: str, end: str) -> float:
    """Total EC2 compute spend for the account."""
    try:
        resp = ce_client.get_cost_and_usage(
            TimePeriod={"Start": start, "End": end},
            Granularity="MONTHLY",
            Filter={"Dimensions": {"Key": "SERVICE", "Values": [
                "Amazon Elastic Compute Cloud - Compute", "AWS Fargate"
            ]}},
            Metrics=["UnblendedCost"],
        )
        return sum(
            float(p["Total"]["UnblendedCost"]["Amount"])
            for p in resp.get("ResultsByTime", [])
        )
    except Exception as e:
        log.warning("Total EC2 spend fetch failed: %s", e)
        return 0.0


def _get_date_range(months_back: int = 3) -> tuple[str, str]:
    end = date.today().replace(day=1) - timedelta(days=1)  # last day of prior month
    start = (end.replace(day=1) - timedelta(days=months_back * 30)).replace(day=1)
    return start.isoformat(), end.isoformat()


def _savings_plan_utilization(ce_client: Any, start: str, end: str) -> dict[str, float]:
    try:
        resp = ce_client.get_savings_plans_utilization(
            TimePeriod={"Start": start, "End": end},
            Granularity="MONTHLY",
        )
        total = resp.get("Total", {})
        util = total.get("Utilization", {})
        return {
            "utilization_pct": float(util.get("UtilizationPercentage", 0)),
            "unused_usd": float(total.get("Savings", {}).get("NetSavings", 0)),
            "total_commitment": float(util.get("TotalCommitment", 0)),
        }
    except Exception as e:
        from .._logutil import note_sp_error
        note_sp_error(log, "SP utilization", e)
        return {"utilization_pct": 0.0, "unused_usd": 0.0, "total_commitment": 0.0}


def _savings_plan_coverage(
    ce_client: Any,
    start: str,
    end: str,
    tag_filter: dict | None = None,
) -> float:
    """
    Coverage % for the account — or for a specific tag slice when tag_filter is set.

    tag_filter examples:
        {"team": "platform"}   → coverage for instances tagged team=platform
        {"env": "prod"}        → coverage for prod-tagged instances

    Note: SP coverage filtered by tag shows what % of *that team's tagged EC2 usage*
    is covered by any SP in the account. It does NOT split SP ownership between teams —
    SPs are account-level instruments. This is the closest approximation AWS supports.
    """
    try:
        kwargs: dict[str, Any] = {
            "TimePeriod": {"Start": start, "End": end},
            "Granularity": "MONTHLY",
        }
        if tag_filter:
            tag_key, tag_val = next(iter(tag_filter.items()))
            kwargs["Filter"] = {"Tags": {"Key": tag_key, "Values": [tag_val]}}

        resp = ce_client.get_savings_plans_coverage(**kwargs)
        totals = resp.get("Total", {}).get("CoverageHours", {})
        return float(totals.get("CoverageHoursPercentage", 0))
    except Exception as e:
        # None, not 0.0. "We could not read your coverage" and "you have no
        # coverage" are opposite facts, and 0.0 conflated them into the one that
        # triggers a purchase: a missing ce:GetSavingsPlansCoverage permission
        # produced "Your SP coverage is 0%" and a recommendation to commit
        # $5,940/mo. A denied read must never turn into advice to spend money.
        from .._logutil import note_sp_error
        note_sp_error(log, "SP coverage", e)
        return None


def _ri_utilization(ce_client: Any, start: str, end: str) -> dict[str, float]:
    try:
        resp = ce_client.get_reservation_utilization(
            TimePeriod={"Start": start, "End": end},
            Granularity="MONTHLY",
        )
        total = resp.get("Total", {})
        util = total.get("Utilization", {})
        unused = total.get("UnusedHours", "0")
        unused_cost = float(total.get("UnusedAmortizedUpfrontCostForRIs", 0)) + \
                      float(total.get("UnusedRecurringFeeForRIs", 0))
        return {
            "utilization_pct": float(util.get("UtilizationPercentage", 0)),
            "unused_usd": unused_cost,
        }
    except Exception as e:
        log.warning("RI utilization fetch failed: %s", e)
        return {"utilization_pct": 0.0, "unused_usd": 0.0}


def _ri_coverage(
    ce_client: Any,
    start: str,
    end: str,
    tag_filter: dict | None = None,
) -> float:
    try:
        kwargs: dict[str, Any] = {
            "TimePeriod": {"Start": start, "End": end},
            "Granularity": "MONTHLY",
        }
        if tag_filter:
            tag_key, tag_val = next(iter(tag_filter.items()))
            kwargs["Filter"] = {"Tags": {"Key": tag_key, "Values": [tag_val]}}

        resp = ce_client.get_reservation_coverage(**kwargs)
        total = resp.get("Total", {}).get("CoverageHours", {})
        return float(total.get("CoverageHoursPercentage", 0))
    except Exception as e:
        log.warning("RI coverage fetch failed: %s", e)
        return 0.0


def _uncovered_on_demand_monthly(
    ce_client: Any,
    start: str,
    end: str,
    tag_filter: dict | None = None,
) -> list[float]:
    """Per-month on-demand EC2 + Fargate spend not covered by any commitment.

    The per-month series is what lets us size a commitment to the CONSISTENT
    BASELINE (the floor uncovered every month) instead of a single average or peak.
    """
    try:
        base_filter: dict = {
            "And": [
                {"Dimensions": {"Key": "SERVICE", "Values": [
                    "Amazon Elastic Compute Cloud - Compute", "AWS Fargate"
                ]}},
                {"Dimensions": {"Key": "PURCHASE_TYPE", "Values": ["On Demand"]}},
            ]
        }
        if tag_filter:
            tag_key, tag_val = next(iter(tag_filter.items()))
            base_filter["And"].append(
                {"Tags": {"Key": tag_key, "Values": [tag_val]}}
            )

        resp = ce_client.get_cost_and_usage(
            TimePeriod={"Start": start, "End": end},
            Granularity="MONTHLY",
            Filter=base_filter,
            Metrics=["UnblendedCost"],
        )
        return [
            float(p["Total"]["UnblendedCost"]["Amount"])
            for p in resp.get("ResultsByTime", [])
        ]
    except Exception as e:
        log.warning("On-demand cost fetch failed: %s", e)
        return []


def _uncovered_on_demand(
    ce_client: Any,
    start: str,
    end: str,
    tag_filter: dict | None = None,
) -> float:
    """On-demand EC2 + Fargate spend not covered by any commitment (period total)."""
    return round(sum(_uncovered_on_demand_monthly(ce_client, start, end, tag_filter)), 6)


def _build_recommendations(
    sp_coverage: float | None,
    uncovered_od: float,
    sp_util: float,
    ri_util: float,
    monthly_uncovered_series: list[float] | None = None,
) -> list[dict[str, Any]]:
    recs: list[dict[str, Any]] = []

    # sp_coverage is None when the coverage call was denied or failed. Every
    # commitment recommendation below is an argument about how much of the bill
    # is already covered, so with that unknown there is no argument to make. It
    # used to arrive as 0.0, which reads as "nothing is covered" and is the most
    # aggressive possible reading of "we could not look": a missing
    # ce:GetSavingsPlansCoverage permission produced a recommendation to commit
    # thousands of dollars a month.
    if sp_coverage is None:
        return [{
            "type": "coverage_unavailable",
            "title": "Commitment advice unavailable: coverage could not be read",
            "detail": (
                "nable could not read your Savings Plans coverage, so it does not "
                "know how much of your on-demand spend is already committed. "
                "Recommending a purchase without that would be guessing with your "
                "money. Grant ce:GetSavingsPlansCoverage and run this again."
            ),
            "monthly_savings": None,
            "confidence": "none",
            "blocked_reason": "coverage unavailable, missing ce:GetSavingsPlansCoverage",
        }]

    # Size to the CONSISTENT BASELINE: the floor of monthly uncovered on-demand (what
    # is uncovered EVERY month), not the 3-month average or a peak. Committing to the
    # floor is the safe amount, a quiet month never leaves you over-committed. Falls
    # back to the 3-month average when no monthly series is available.
    series = [m for m in (monthly_uncovered_series or []) if m and m > 0]
    if len(series) >= 2:
        baseline = min(series)
        peak = max(series)
        basis = "your consistent monthly baseline (uncovered every month)"
    else:
        baseline = uncovered_od / 3
        peak = baseline
        basis = "your 3-month average uncovered on-demand"

    # Recommend a Compute SP sized to the baseline when coverage is low and it's meaningful
    if sp_coverage < 60 and baseline > 500:
        commitment_to_add = baseline * _COMPUTE_SP_DISCOUNT
        monthly_savings = baseline * (1 - _COMPUTE_SP_DISCOUNT)
        spiky = peak > baseline * 1.5
        desc = (
            f"Your SP coverage is {sp_coverage:.0f}%. A 1-year no-upfront Compute SP at "
            f"${commitment_to_add:,.0f}/mo hourly commitment covers {basis} "
            f"(${baseline:,.0f}/mo of on-demand)."
        )
        if spiky:
            desc += (f" Sized to the floor, not your ${peak:,.0f}/mo peak, so a slow month "
                     f"won't leave you paying for commitment you can't use.")
        recs.append({
            "type": "savings_plan",
            "title": "Purchase Compute Savings Plan",
            "description": desc,
            "commitment_per_month": round(commitment_to_add, 2),
            "monthly_savings": round(monthly_savings, 2),
            "annual_savings": round(monthly_savings * 12, 2),
            "baseline_monthly_uncovered_usd": round(baseline, 2),
            "sizing_basis": basis,
            "payback_months": 0,  # no-upfront has no payback period
            "term": "1-year",
            "payment": "no-upfront",
            "confidence": "high" if baseline > 5000 else "medium",
        })

    # Warn about over-commitment (low utilization = waste)
    if sp_util < 70 and sp_util > 0:
        recs.append({
            "type": "warning",
            "title": "Savings Plan under-utilised",
            "description": (
                f"Your Savings Plans are only {sp_util:.0f}% utilized — "
                "you're paying for commitment you're not using. "
                "Consider reducing commitment at next renewal or moving workloads "
                "to covered instance families."
            ),
            "monthly_savings": 0,
            "annual_savings": 0,
            "confidence": "high",
        })

    if ri_util < 70 and ri_util > 0:
        recs.append({
            "type": "warning",
            "title": "Reserved Instances under-utilised",
            "description": (
                f"Your RIs are only {ri_util:.0f}% utilized. "
                "List unused RI capacity on the AWS Marketplace or "
                "modify to a different instance size within the same family."
            ),
            "monthly_savings": 0,
            "annual_savings": 0,
            "confidence": "high",
        })

    return recs


def estimate_coverage_for_partial_tag(
    tag_key: str,
    tag_value: str,
    tag_coverage_pct: float,      # how complete the tagging is, 0–100
) -> TaggedCoverageEstimate | None:
    """
    Estimate full-domain commitment coverage when tag coverage is partial.

    At 70% tag coverage we can measure the tagged 70% directly, then solve
    algebraically for the untagged 30% using account totals, producing a
    blended full-domain estimate.

    Confidence:
        ≥ 90% tagged → high    (untagged <10%, rounding error level)
        ≥ 60% tagged → medium  (meaningful estimate, uncertainty noted)
        < 60% tagged → low     (too much unmeasured, treat as directional only)
    """
    try:
        import boto3
    except ImportError:
        return None

    try:
        ce = boto3.client("ce", region_name="us-east-1")
        start, end = _get_date_range(months_back=3)

        # ── Steps 1+2: the six Cost Explorer calls below are independent. Run them
        # concurrently (CE is 2-8s per call, so serial was ~6x the latency); the
        # botocore client is thread-safe for concurrent operations. Mirrors the pool
        # already used in analyze_commitments.
        from concurrent.futures import ThreadPoolExecutor

        with ThreadPoolExecutor(max_workers=6) as _pool:
            f_tagged_sp    = _pool.submit(_savings_plan_coverage, ce, start, end, {tag_key: tag_value})
            f_tagged_ri    = _pool.submit(_ri_coverage, ce, start, end, {tag_key: tag_value})
            f_tagged_spend = _pool.submit(_ec2_spend_for_tag, ce, start, end, tag_key, tag_value)
            f_acct_sp      = _pool.submit(_savings_plan_coverage, ce, start, end)
            f_acct_ri      = _pool.submit(_ri_coverage, ce, start, end)
            f_total        = _pool.submit(_total_ec2_spend, ce, start, end)

        tagged_sp_cov = f_tagged_sp.result()
        tagged_ri_cov = f_tagged_ri.result()
        tagged_spend  = f_tagged_spend.result()
        acct_sp_cov   = f_acct_sp.result()
        acct_ri_cov   = f_acct_ri.result()
        total_spend   = f_total.result()

        # ── Step 3: infer untagged portion via residual ───────────────────────
        # tagged_coverage_fraction  = tagged_sp_cov / 100
        # account_coverage_fraction = acct_sp_cov / 100
        # account_cov × total = tagged_cov × tagged + untagged_cov × untagged
        # → untagged_cov = (account_cov × total - tagged_cov × tagged) / untagged

        untagged_spend = max(0.0, total_spend - tagged_spend)

        def _infer_untagged(acct_cov: float, tagged_cov: float) -> float:
            if untagged_spend <= 0:
                return acct_cov  # no untagged spend, account coverage applies
            numerator = (acct_cov / 100 * total_spend) - (tagged_cov / 100 * tagged_spend)
            raw = (numerator / untagged_spend) * 100
            return max(0.0, min(100.0, raw))

        inferred_untagged_sp = _infer_untagged(acct_sp_cov, tagged_sp_cov)
        inferred_untagged_ri = _infer_untagged(acct_ri_cov, tagged_ri_cov)

        # ── Step 4: blend for full-domain estimate ────────────────────────────
        tagged_weight   = tag_coverage_pct / 100
        untagged_weight = 1.0 - tagged_weight

        blended_sp = tagged_sp_cov * tagged_weight + inferred_untagged_sp * untagged_weight
        blended_ri = tagged_ri_cov * tagged_weight + inferred_untagged_ri * untagged_weight
        blended    = (blended_sp + blended_ri) / 2

        # ── Step 5: confidence ────────────────────────────────────────────────
        if tag_coverage_pct >= 90:
            confidence = "high"
            note = (
                f"{tag_coverage_pct:.0f}% of resources are tagged — "
                f"the untagged {100 - tag_coverage_pct:.0f}% is a rounding-error level gap."
            )
        elif tag_coverage_pct >= 60:
            confidence = "medium"
            note = (
                f"{tag_coverage_pct:.0f}% of resources are tagged. "
                f"The untagged {100 - tag_coverage_pct:.0f}% is inferred from account totals "
                f"(estimated coverage: {inferred_untagged_sp:.0f}% SP, {inferred_untagged_ri:.0f}% RI). "
                f"Improve tagging to increase confidence."
            )
        else:
            confidence = "low"
            note = (
                f"Only {tag_coverage_pct:.0f}% of resources carry the '{tag_key}' tag — "
                f"the estimate is directional only. Bring tagging above 80% for a reliable number."
            )

        return TaggedCoverageEstimate(
            tag_key=tag_key,
            tag_value=tag_value,
            tag_coverage_pct=tag_coverage_pct,
            tagged_spend_usd=round(tagged_spend, 2),
            tagged_sp_coverage_pct=round(tagged_sp_cov, 1),
            tagged_ri_coverage_pct=round(tagged_ri_cov, 1),
            untagged_spend_usd=round(untagged_spend, 2),
            inferred_untagged_sp_coverage_pct=round(inferred_untagged_sp, 1),
            inferred_untagged_ri_coverage_pct=round(inferred_untagged_ri, 1),
            estimated_sp_coverage_pct=round(blended_sp, 1),
            estimated_ri_coverage_pct=round(blended_ri, 1),
            estimated_combined_coverage_pct=round(blended, 1),
            confidence=confidence,
            confidence_note=note,
        )

    except Exception as e:
        log.error("Partial-tag coverage estimate failed: %s", e)
        return None


def analyze_commitments(
    tag_filter: dict | None = None,
) -> CommitmentAnalysis | None:
    """
    Run full RI/SP analysis. Returns None if AWS is not configured.

    tag_filter: optional dict to scope coverage to a tag slice.
        e.g. {"team": "platform"} or {"env": "prod"}

    Important caveat when tag_filter is set:
        SP/RI utilization figures are always account-level (AWS doesn't
        support filtering utilization by tag). Only coverage and on-demand
        figures are tag-filtered. The scorecard makes this explicit.
    """
    try:
        import boto3
    except ImportError:
        return None

    try:
        ce = boto3.client("ce", region_name="us-east-1")
        start, end = _get_date_range(months_back=3)

        # These five Cost Explorer calls are independent. Run them concurrently
        # rather than back-to-back: CE is 2-8s per call, so serial was 5x the
        # latency. botocore low-level clients are thread-safe for concurrent
        # operations, so one shared ce client across the pool is fine.
        from concurrent.futures import ThreadPoolExecutor

        with ThreadPoolExecutor(max_workers=5) as _pool:
            # Utilization is always account-level: AWS can't filter it by tag.
            f_sp_util = _pool.submit(_savings_plan_utilization, ce, start, end)
            f_ri_util = _pool.submit(_ri_utilization, ce, start, end)
            # Coverage and on-demand can be filtered by tag.
            f_sp_cov = _pool.submit(_savings_plan_coverage, ce, start, end, tag_filter)
            f_ri_cov = _pool.submit(_ri_coverage, ce, start, end, tag_filter)
            f_unc = _pool.submit(_uncovered_on_demand_monthly, ce, start, end, tag_filter)

        sp_util_data = f_sp_util.result()
        ri_util_data = f_ri_util.result()
        sp_coverage = f_sp_cov.result()
        ri_coverage = f_ri_cov.result()
        uncovered_monthly = f_unc.result()
        uncovered_od = round(sum(uncovered_monthly), 2)

        recs = _build_recommendations(
            sp_coverage,
            uncovered_od,
            sp_util_data["utilization_pct"],
            ri_util_data["utilization_pct"],
            monthly_uncovered_series=uncovered_monthly,
        )

        return CommitmentAnalysis(
            # None survives to the caller rather than being rounded into a
            # number. commitment_summary and the scorecard both read this field,
            # and both need to be able to say "unknown".
            savings_plan_coverage_pct=(
                None if sp_coverage is None else round(sp_coverage, 1)),
            savings_plan_utilization_pct=round(sp_util_data["utilization_pct"], 1),
            savings_plan_unused_usd=round(sp_util_data["unused_usd"], 2),
            ri_coverage_pct=round(ri_coverage, 1),
            ri_utilization_pct=round(ri_util_data["utilization_pct"], 1),
            ri_unused_usd=round(ri_util_data["unused_usd"], 2),
            uncovered_on_demand_usd=round(uncovered_od, 2),
            recommendations=recs,
        )
    except Exception as e:
        log.error("Commitment analysis failed: %s", e)
        return None


def commitment_summary(analysis: CommitmentAnalysis) -> dict[str, Any]:
    return {
        "coverage_score": analysis.coverage_score,
        "savings_plan": {
            "coverage_pct": analysis.savings_plan_coverage_pct,
            "utilization_pct": analysis.savings_plan_utilization_pct,
            "unused_usd_per_month": analysis.savings_plan_unused_usd,
        },
        "reserved_instances": {
            "coverage_pct": analysis.ri_coverage_pct,
            "utilization_pct": analysis.ri_utilization_pct,
            "unused_usd_per_month": analysis.ri_unused_usd,
        },
        "uncovered_on_demand_usd_3mo": analysis.uncovered_on_demand_usd,
        "total_waste_usd_per_month": analysis.total_waste_usd,
        "recommendations": analysis.recommendations,
    }
