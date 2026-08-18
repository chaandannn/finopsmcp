"""
Rightsizing recommendations via AWS Compute Optimizer (primary) with
CloudWatch CPU fallback for accounts that haven't opted in.

Compute Optimizer considers CPU, memory, network, and disk — not just CPU.
It covers EC2, Lambda, and ECS services. We surface its findings directly
rather than rebuilding the same logic ourselves.

Fallback (CloudWatch only):
  Used when Compute Optimizer returns no data or opt-in is required.
  CPU-only, EC2 only, less accurate — clearly labelled in output.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

log = logging.getLogger(__name__)

_LOOKBACK_DAYS     = 14
_AVG_CPU_THRESHOLD = 20.0
_MAX_CPU_THRESHOLD = 50.0
_HOURS_PER_MONTH   = 730.0

# Fallback on-demand hourly prices (us-east-1) used only when Compute
# Optimizer savings estimates are unavailable.
_HOURLY_PRICE: dict[str, float] = {
    "t3.nano": 0.0052,    "t3.micro": 0.0104,   "t3.small": 0.0208,
    "t3.medium": 0.0416,  "t3.large": 0.0832,   "t3.xlarge": 0.1664,
    "t3.2xlarge": 0.3328,
    "t3a.nano": 0.0047,   "t3a.micro": 0.0094,  "t3a.small": 0.0188,
    "t3a.medium": 0.0376, "t3a.large": 0.0752,  "t3a.xlarge": 0.1504,
    "t3a.2xlarge": 0.3008,
    "m5.large": 0.096,    "m5.xlarge": 0.192,   "m5.2xlarge": 0.384,
    "m5.4xlarge": 0.768,  "m5.8xlarge": 1.536,
    "m6i.large": 0.096,   "m6i.xlarge": 0.192,  "m6i.2xlarge": 0.384,
    "m6i.4xlarge": 0.768, "m6i.8xlarge": 1.536,
    "c5.large": 0.085,    "c5.xlarge": 0.17,    "c5.2xlarge": 0.34,
    "c5.4xlarge": 0.68,   "c5.9xlarge": 1.53,
    "r5.large": 0.126,    "r5.xlarge": 0.252,   "r5.2xlarge": 0.504,
    "r5.4xlarge": 1.008,  "r5.8xlarge": 2.016,
}

_DOWNSIZE_MAP: dict[str, str] = {
    "t3.medium": "t3.small",    "t3.large": "t3.medium",
    "t3.xlarge": "t3.large",    "t3.2xlarge": "t3.xlarge",
    "t3a.medium": "t3a.small",  "t3a.large": "t3a.medium",
    "m5.xlarge": "m5.large",    "m5.2xlarge": "m5.xlarge",
    "m5.4xlarge": "m5.2xlarge",
    "m6i.xlarge": "m6i.large",  "m6i.2xlarge": "m6i.xlarge",
    "m6i.4xlarge": "m6i.2xlarge",
    "c5.xlarge": "c5.large",    "c5.2xlarge": "c5.xlarge",
    "c5.4xlarge": "c5.2xlarge",
    "r5.xlarge": "r5.large",    "r5.2xlarge": "r5.xlarge",
    "r5.4xlarge": "r5.2xlarge",
}


@dataclass
class RightsizingRecommendation:
    instance_id: str
    instance_type: str          # current type (or "Lambda" / "ECS")
    name: str
    region: str
    account_id: str
    resource_type: str          # "ec2" | "lambda" | "ecs"
    source: str                 # "compute_optimizer" | "cloudwatch_fallback"

    # Utilisation metrics — populated from Compute Optimizer when available
    avg_cpu_pct: float          = 0.0
    max_cpu_pct: float          = 0.0
    avg_mem_pct: float | None   = None   # None = not available
    avg_net_mbps: float | None  = None

    # Carried so the workload classifier can tell production from a sandbox
    # before anything proposes a change to it. Lives here with the other
    # defaulted fields because a dataclass will not take a default before a
    # required one.
    tags: dict                  = field(default_factory=dict)

    recommended_type: str       = ""
    current_monthly_cost: float = 0.0
    recommended_monthly_cost: float = 0.0
    monthly_savings: float      = 0.0
    confidence: str             = "medium"   # "high" | "medium" | "low"
    finding: str                = ""         # Compute Optimizer finding label
    metadata: dict[str, Any]    = field(default_factory=dict)

    @property
    def title(self) -> str:
        label = self.name or self.instance_id
        if self.recommended_type:
            return f"Downsize {label} ({self.instance_type} → {self.recommended_type})"
        return f"Right-size {label} ({self.instance_type})"

    @property
    def description(self) -> str:
        parts = [f"Avg CPU {self.avg_cpu_pct:.1f}%"]
        if self.avg_mem_pct is not None:
            parts.append(f"mem {self.avg_mem_pct:.1f}%")
        parts.append(f"over {_LOOKBACK_DAYS}d.")
        if self.monthly_savings:
            parts.append(f"Saving ~${self.monthly_savings:,.0f}/mo.")
        if self.source == "cloudwatch_fallback":
            parts.append("(CPU-only estimate — enable Compute Optimizer for full analysis)")
        return " ".join(parts)


# ── Compute Optimizer (primary) ───────────────────────────────────────────────

def _co_utilization(metrics: list[dict]) -> dict[str, float]:
    """Extract named utilization metrics from a Compute Optimizer metrics list."""
    out: dict[str, float] = {}
    for m in metrics:
        name = m.get("name", "")
        value = m.get("value", 0.0)
        if name == "Cpu":
            out["cpu"] = round(float(value), 1)
        elif name == "Memory":
            out["mem"] = round(float(value), 1)
        elif name in ("NetworkInBytesPerSecond", "NetworkOutBytesPerSecond"):
            out.setdefault("net_mbps", 0.0)
            out["net_mbps"] = round(out["net_mbps"] + float(value) / 1_000_000, 3)
    return out


# ── Compute Optimizer paging ──────────────────────────────────────────────────
# Only get_lambda_function_recommendations has a botocore paginator. EC2 and RDS
# do not, so get_paginator() on them raises OperationNotPageableError, and both
# call sites had that inside a broad except. Compute Optimizer has therefore
# never returned a single EC2 recommendation in this product, while the trust
# envelope rates compute_optimizer_* findings MEASURED/high.
#
# One helper so there is one paging implementation to get right. It works for
# every operation, paginated or not, because nextToken is in the API contract.
def _co_pages(co_client, op_name: str, **kwargs):
    """Yield every page of a Compute Optimizer operation via its nextToken."""
    token = None
    fn = getattr(co_client, op_name)
    while True:
        page = fn(**kwargs, **({"nextToken": token} if token else {}))
        yield page
        token = page.get("nextToken")
        if not token:
            return


# The finding values AWS actually returns. The code compared against
# "OVER_PROVISIONED" and "VERY_OVER_PROVISIONED", neither of which exists in the
# API: the enum is Underprovisioned | Overprovisioned | Optimized | NotOptimized.
# So even once paging worked, the filter would have matched nothing, and the
# confidence line keyed on VERY_OVER_PROVISIONED could never fire.
CO_EC2_OVERPROVISIONED = "Overprovisioned"
CO_RDS_OVERPROVISIONED = "Overprovisioned"
CO_LAMBDA_NOT_OPTIMIZED = "NotOptimized"


def _co_monthly_savings(option: dict) -> tuple[float, str]:
    """Savings from a recommendation option.

    The figure is nested under savingsOpportunity, not on the option itself.
    Reading option["estimatedMonthlySavings"] returns {} and then 0.0, which is
    why every Lambda recommendation reported $0.00 rather than being dropped.
    """
    info = (option.get("savingsOpportunity") or {}).get("estimatedMonthlySavings") or {}
    try:
        value = float(info.get("value", 0.0) or 0.0)
    except (TypeError, ValueError):
        value = 0.0
    return value, info.get("currency", "USD")


def _fetch_ec2_from_co(co_client: Any, account_id: str) -> list[RightsizingRecommendation]:
    results = []
    pages = _co_pages(
        co_client, "get_ec2_instance_recommendations",
        filters=[{"name": "Finding", "values": [CO_EC2_OVERPROVISIONED]}],
    )
    for page in pages:
        for rec in page.get("instanceRecommendations", []):
            arn        = rec.get("instanceArn", "")
            iid        = arn.split("/")[-1] if "/" in arn else arn
            itype      = rec.get("currentInstanceType", "")
            name       = rec.get("instanceName", "")
            region     = arn.split(":")[3] if ":" in arn else ""
            finding    = rec.get("finding", "")
            util       = _co_utilization(rec.get("utilizationMetrics", []))

            # Best recommendation = rank 1
            options = sorted(
                rec.get("recommendationOptions", []),
                key=lambda o: o.get("rank", 99)
            )
            if not options:
                continue
            best   = options[0]
            rtype  = best.get("instanceType", "")
            monthly_savings, currency = _co_monthly_savings(best)
            if currency != "USD":
                monthly_savings = 0.0  # don't guess FX

            rec_util  = _co_utilization(best.get("projectedUtilizationMetrics", []))
            # AWS has no "very over-provisioned" finding. Confidence comes from
            # the option's own performance risk instead, which is a real field.
            risk = str(best.get("performanceRisk", "")).lower()
            confidence = "high" if risk in ("very_low", "low", "0", "1") else "medium"

            results.append(RightsizingRecommendation(
                instance_id=iid,
                instance_type=itype,
                name=name,
                region=region,
                account_id=account_id,
                resource_type="ec2",
                source="compute_optimizer",
                avg_cpu_pct=util.get("cpu", 0.0),
                avg_mem_pct=util.get("mem"),
                avg_net_mbps=util.get("net_mbps"),
                recommended_type=rtype,
                monthly_savings=round(monthly_savings, 2),
                confidence=confidence,
                finding=finding,
                metadata={"options_count": len(options)},
            ))
    return results


def _fetch_lambda_from_co(co_client: Any, account_id: str) -> list[RightsizingRecommendation]:
    results = []
    try:
        # This one genuinely has a paginator, so it keeps using it.
        paginator = co_client.get_paginator("get_lambda_function_recommendations")
        # Lambda's finding enum is Optimized | NotOptimized | Unavailable. There
        # is no OVER_PROVISIONED, so this filter matched nothing and the block
        # returned an empty list on every run.
        pages = paginator.paginate(
            filters=[{"name": "Finding", "values": [CO_LAMBDA_NOT_OPTIMIZED]}]
        )
        for page in pages:
            for rec in page.get("lambdaFunctionRecommendations", []):
                arn      = rec.get("functionArn", "")
                fname    = arn.split(":")[-1] if ":" in arn else arn
                region   = arn.split(":")[3] if ":" in arn else ""
                current_mb = rec.get("currentMemorySize", 0)
                finding    = rec.get("finding", "")
                util       = _co_utilization(rec.get("utilizationMetrics", []))

                options = rec.get("memorySizeRecommendationOptions", [])
                options_sorted = sorted(options, key=lambda o: o.get("rank", 99))
                if not options_sorted:
                    continue
                best        = options_sorted[0]
                rec_mb      = best.get("memorySize", current_mb)
                monthly_savings, currency = _co_monthly_savings(best)
                if currency != "USD":
                    monthly_savings = 0.0  # don't guess FX

                results.append(RightsizingRecommendation(
                    instance_id=arn,
                    instance_type=f"Lambda {current_mb}MB",
                    name=fname,
                    region=region,
                    account_id=account_id,
                    resource_type="lambda",
                    source="compute_optimizer",
                    avg_cpu_pct=util.get("cpu", 0.0),
                    avg_mem_pct=util.get("mem"),
                    recommended_type=f"Lambda {rec_mb}MB",
                    monthly_savings=round(monthly_savings, 2),
                    confidence="medium",
                    finding=finding,
                ))
    except Exception as e:
        log.debug("Lambda Compute Optimizer recommendations unavailable: %s", e)
    return results


def _analyze_compute_optimizer(account_id: str) -> list[RightsizingRecommendation]:
    """Fetch EC2 + Lambda rightsizing from Compute Optimizer."""
    try:
        import boto3
        co = boto3.client("compute-optimizer", region_name="us-east-1")
        # Verify opt-in status first — avoids a confusing AccessDeniedException
        status = co.get_enrollment_status()
        if status.get("status") not in ("Active", "active"):
            log.info(
                "Compute Optimizer not opted in (status=%s). "
                "Enable it at: https://console.aws.amazon.com/compute-optimizer/",
                status.get("status"),
            )
            return []

        ec2_recs    = _fetch_ec2_from_co(co, account_id)
        lambda_recs = _fetch_lambda_from_co(co, account_id)
        return ec2_recs + lambda_recs

    except Exception as e:
        log.warning("Compute Optimizer unavailable: %s", e)
        return []


# ── CloudWatch fallback (CPU-only, EC2 only) ──────────────────────────────────

def _get_cloudwatch_cpu(cw_client: Any, instance_id: str, days: int) -> tuple[float, float]:
    end   = datetime.now(timezone.utc)
    start = end - timedelta(days=days)
    resp  = cw_client.get_metric_statistics(
        Namespace="AWS/EC2",
        MetricName="CPUUtilization",
        Dimensions=[{"Name": "InstanceId", "Value": instance_id}],
        StartTime=start,
        EndTime=end,
        Period=3600,
        Statistics=["Average", "Maximum"],
    )
    dps = resp.get("Datapoints", [])
    if not dps:
        return 0.0, 0.0
    avgs = [d["Average"] for d in dps]
    maxs = [d["Maximum"] for d in dps]
    return sum(avgs) / len(avgs), max(maxs)


def monthly_cost(instance_type: str) -> float:
    return _HOURLY_PRICE.get(instance_type, 0.0) * _HOURS_PER_MONTH


def _list_region_instances(region: str) -> list[dict]:
    """All running EC2 instances in one region (one paginated describe call)."""
    import boto3
    ec2 = boto3.client("ec2", region_name=region)
    out: list[dict] = []
    for page in ec2.get_paginator("describe_instances").paginate(
        Filters=[{"Name": "instance-state-name", "Values": ["running"]}]
    ):
        for reservation in page["Reservations"]:
            for inst in reservation["Instances"]:
                out.append({
                    "region": region,
                    "iid": inst["InstanceId"],
                    "itype": inst["InstanceType"],
                    "name": next(
                        (t["Value"] for t in inst.get("Tags", []) if t["Key"] == "Name"), ""
                    ),
                    # describe_instances already returned every tag and this kept
                    # only Name. Environment was on the wire and discarded one
                    # line after it arrived, which left the workload classifier's
                    # strongest signal permanently empty: it asks for tags, the
                    # recommendation never carried any, and the guard that is
                    # supposed to keep pull requests off somebody's sandbox ran
                    # on the two weakest signals it has.
                    "tags": {t["Key"]: t["Value"] for t in inst.get("Tags", [])},
                })
    return out


def _analyze_cloudwatch_fallback(
    regions: list[str],
    account_id: str,
    avg_cpu_threshold: float,
    max_cpu_threshold: float,
) -> list[RightsizingRecommendation]:
    """CPU-only EC2 scan when Compute Optimizer is not available.

    Two parallel phases instead of the old regions x instances serial walk (a
    100-instance fleet was 100+ sequential CloudWatch round-trips): first list
    instances across regions concurrently, then fetch each instance's CPU metrics
    concurrently. Bounded workers keep well under CloudWatch API limits.
    """
    import boto3
    from concurrent.futures import ThreadPoolExecutor

    # Phase 1: discover running instances, regions in parallel.
    instances: list[dict] = []
    with ThreadPoolExecutor(max_workers=min(8, max(1, len(regions)))) as pool:
        for result in pool.map(_safe_list_instances, regions):
            instances.extend(result)

    if not instances:
        return []

    # Phase 2: per-instance CPU metrics, in parallel (one CloudWatch client per
    # region, shared across that region's lookups; boto3 clients are thread-safe).
    cw_by_region = {r: boto3.client("cloudwatch", region_name=r)
                    for r in {i["region"] for i in instances}}

    def _cpu(inst: dict) -> tuple[dict, float, float]:
        try:
            avg_cpu, max_cpu = _get_cloudwatch_cpu(
                cw_by_region[inst["region"]], inst["iid"], _LOOKBACK_DAYS)
        except Exception as e:
            log.debug("CPU fetch failed for %s: %s", inst["iid"], e)
            # Unknown utilization must never be recommended for downsizing.
            avg_cpu, max_cpu = 100.0, 100.0
        return inst, avg_cpu, max_cpu

    results: list[RightsizingRecommendation] = []
    with ThreadPoolExecutor(max_workers=10) as pool:
        for inst, avg_cpu, max_cpu in pool.map(_cpu, instances):
            if avg_cpu >= avg_cpu_threshold or max_cpu >= max_cpu_threshold:
                continue
            itype = inst["itype"]
            recommended = _DOWNSIZE_MAP.get(itype)
            if not recommended or recommended == itype:
                continue
            savings = monthly_cost(itype) - monthly_cost(recommended)
            if savings <= 0:
                continue
            results.append(RightsizingRecommendation(
                instance_id=inst["iid"],
                instance_type=itype,
                name=inst["name"],
                region=inst["region"],
                account_id=account_id,
                tags=inst.get("tags") or {},
                resource_type="ec2",
                source="cloudwatch_fallback",
                avg_cpu_pct=round(avg_cpu, 1),
                max_cpu_pct=round(max_cpu, 1),
                recommended_type=recommended,
                current_monthly_cost=round(monthly_cost(itype), 2),
                recommended_monthly_cost=round(monthly_cost(recommended), 2),
                monthly_savings=round(savings, 2),
                confidence="high" if avg_cpu < 10 and max_cpu < 30 else "medium",
            ))
    return results


def _safe_list_instances(region: str) -> list[dict]:
    try:
        return _list_region_instances(region)
    except Exception as e:
        log.warning("CloudWatch fallback failed for region %s: %s", region, e)
        return []


# ── Public API ────────────────────────────────────────────────────────────────

def analyze_rightsizing(
    regions: list[str] | None = None,
    avg_cpu_threshold: float = _AVG_CPU_THRESHOLD,
    max_cpu_threshold: float = _MAX_CPU_THRESHOLD,
    min_monthly_savings: float = 10.0,
) -> list[RightsizingRecommendation]:
    """
    Return rightsizing recommendations sorted by monthly savings (descending).

    Uses AWS Compute Optimizer as the primary source (CPU + memory + network +
    disk, covers EC2 and Lambda). Falls back to a CloudWatch CPU-only scan
    for accounts that haven't opted into Compute Optimizer.
    """
    try:
        import boto3
    except ImportError:
        log.error("boto3 not installed")
        return []

    sts = boto3.client("sts")
    try:
        account_id = sts.get_caller_identity()["Account"]
    except Exception:
        account_id = "unknown"

    # Try Compute Optimizer first
    recommendations = _analyze_compute_optimizer(account_id)

    if not recommendations:
        # Fall back to CloudWatch CPU scan
        if regions is None:
            try:
                ec2g = boto3.client("ec2", region_name="us-east-1")
                resp = ec2g.describe_regions(
                    Filters=[{"Name": "opt-in-status",
                              "Values": ["opt-in-not-required", "opted-in"]}]
                )
                regions = [r["RegionName"] for r in resp.get("Regions", [])]
            except Exception:
                regions = ["us-east-1", "us-west-2", "eu-west-1"]

        recommendations = _analyze_cloudwatch_fallback(
            regions, account_id, avg_cpu_threshold, max_cpu_threshold
        )

    # Filter out noise and sort
    recommendations = [r for r in recommendations if r.monthly_savings >= min_monthly_savings]
    recommendations.sort(key=lambda r: r.monthly_savings, reverse=True)
    return recommendations


def rightsizing_summary(
    recommendations: list[RightsizingRecommendation],
    savings_ctx: Any = None,
    commitment_ctx: Any = None,
) -> dict[str, Any]:
    """
    Summarize rightsizing recommendations with a genuine-savings judgment on each.

    Beyond raw "underutilized" totals, every recommendation is scored against the
    reasons a rightsizing call is usually wrong (burst/peak, memory-bound, trivial
    magnitude) and priced on the customer's real environment via `savings_ctx`
    (effective_savings.SavingsContext: measured effective rate + commitment
    coverage). `commitment_ctx` is accepted for backward compatibility and wrapped.
    When both are absent, savings stay at list price with a low-confidence label.
    """
    from .genuine_savings import assess
    from .effective_savings import SavingsContext

    if savings_ctx is None and commitment_ctx is not None:
        savings_ctx = SavingsContext(rate=None, commitment=commitment_ctx)

    total_savings = sum(r.monthly_savings for r in recommendations)
    co_count  = sum(1 for r in recommendations if r.source == "compute_optimizer")
    cw_count  = sum(1 for r in recommendations if r.source == "cloudwatch_fallback")

    by_type: dict[str, float] = {}
    for r in recommendations:
        by_type[r.resource_type] = by_type.get(r.resource_type, 0) + r.monthly_savings

    # Judge each recommendation, then sort genuine-first and by adjusted savings so
    # the rows most worth acting on survive the token cap.
    assessed = [(r, assess(r, savings_ctx)) for r in recommendations]
    _rank = {"genuine_savings": 0, "review": 1, "likely_false_positive": 2}
    assessed.sort(key=lambda ra: (_rank.get(ra[1].verdict, 3), -ra[1].adjusted_monthly_savings))

    genuine_savings_total = sum(
        a.adjusted_monthly_savings for _, a in assessed if a.verdict == "genuine_savings"
    )
    verdict_counts: dict[str, int] = {}
    for _, a in assessed:
        verdict_counts[a.verdict] = verdict_counts.get(a.verdict, 0) + 1

    # Compact rows: dropped the verbose title/description/net fields in favour of a
    # one-line `why` plus the verdict/score/action, so the judgment is richer AND
    # the per-row token cost is no higher than before.
    rows = [
        {
            "instance_id":    r.instance_id,
            "name":           r.name,
            "region":         r.region,
            "resource_type":  r.resource_type,
            "source":         r.source,
            "current_type":   r.instance_type,
            "recommended_type": r.recommended_type,
            "avg_cpu_pct":    r.avg_cpu_pct,
            "max_cpu_pct":    r.max_cpu_pct or None,
            "avg_mem_pct":    r.avg_mem_pct,
            "monthly_savings": r.monthly_savings,
            "adjusted_monthly_savings": a.adjusted_monthly_savings,
            "verdict":        a.verdict,
            "score":          a.score,
            "why":            a.why,
            "action":         a.action,
        }
        for r, a in assessed
    ]
    from ..token_budget import fit_to_budget
    kept, omitted = fit_to_budget(rows)

    out: dict[str, Any] = {
        "total_instances_flagged": len(recommendations),
        "total_monthly_savings":   round(total_savings, 2),
        "total_annual_savings":    round(total_savings * 12, 2),
        # The number that actually matters: savings that survived the judgment.
        "genuine_monthly_savings": round(genuine_savings_total, 2),
        "genuine_annual_savings":  round(genuine_savings_total * 12, 2),
        "verdicts": verdict_counts,
        "source": {
            "compute_optimizer": co_count,
            "cloudwatch_fallback": cw_count,
            "note": (
                "Compute Optimizer recommendations include CPU, memory, network, and disk. "
                "CloudWatch fallback is CPU-only."
                if cw_count > 0 else
                "All recommendations sourced from AWS Compute Optimizer."
            ),
        },
        "savings_by_resource_type": {k: round(v, 2) for k, v in by_type.items()},
        "recommendations": kept,
    }

    # How the savings were priced: measured effective rate (best), commitment
    # coverage (fallback), or list price (no discount data). Bases present tell the
    # customer how much to trust genuine_monthly_savings.
    bases: dict[str, int] = {}
    confidences: dict[str, int] = {}
    for _, a in assessed:
        bases[a.basis] = bases.get(a.basis, 0) + 1
        confidences[a.confidence] = confidences.get(a.confidence, 0) + 1
    pricing: dict[str, Any] = {"basis": bases, "confidence": confidences}
    if savings_ctx is not None:
        rate = getattr(savings_ctx, "rate", None)
        if rate is not None and getattr(rate, "confidence", "low") in ("high", "medium"):
            pricing["effective_discount_pct"] = round(
                float(getattr(rate, "overall_discount_pct", 0.0)) * 100, 1
            )
            pricing["rate_source"] = getattr(rate, "source", "measured")
        cc = getattr(savings_ctx, "commitment", None)
        if cc is not None and getattr(cc, "available", False):
            pricing["commitment_coverage_pct"] = round(cc.combined_pct, 1)
    if "list_price" in bases:
        pricing["note"] = (
            "Some savings are shown at list price because no rate data was found. "
            "Connect your Cost and Usage Report (CUR) to price them on your real rates."
        )
    out["pricing_basis"] = pricing

    if omitted:
        out["recommendations_truncated"] = True
        out["recommendations_omitted"] = omitted
        out["hint"] = (
            f"Showing the {len(kept)} highest-value of {len(recommendations)} "
            f"recommendations (genuine-savings first) to bound token cost. Raise "
            f"avg_cpu_threshold or scope to fewer accounts to see the rest."
        )
    return out

# ── Deprecated aliases ────────────────────────────────────────────────────────
# These names were private (leading underscore) while the enterprise provider
# imported them anyway, so a legitimate rename here broke a repo this CI cannot
# see. The names above are the promoted, supported ones. These aliases exist for
# one release so a provider pinned to an older core keeps working, and are
# covered by tests/test_extension_surface.py::DEPRECATED_ALIASES. Delete them
# once no released provider imports the underscore form.
_monthly_cost = monthly_cost
