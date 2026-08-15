"""
nable Cost Pattern Library — proprietary waste fingerprints.

Each pattern is a named heuristic that detects a specific category of cloud
spend waste. Patterns are scored 0.0–1.0 (confidence that waste exists) and
include estimated monthly savings.

Pattern anatomy:
  - id:           unique slug
  - name:         human label
  - category:     compute | storage | network | database | ai | saas | governance
  - severity:     low | medium | high | critical
  - check(ctx):   function(PatternContext) → PatternMatch | None
  - remediation:  what to do
  - tags:         searchable labels

This library is the core of nable's intelligence layer.  The patterns
themselves represent years of finops practitioner knowledge encoded as
executable heuristics.  New patterns are added as we observe real waste
across the user base.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Any, Callable

log = logging.getLogger(__name__)


# Short names patterns may ask for that the alias map does not key, because the
# map is keyed on what a HUMAN types ("ec2", "s3") and patterns were written with
# API-ish spellings. Kept deliberately small: anything resolvable through the
# alias map is not repeated here, so there is one place to change a CE name.
_SERVICE_SPELLINGS: dict[str, str] = {
    "amazonec2": "ec2",
    "amazon ec2": "ec2",
    "amazons3": "s3",
    "amazon s3": "s3",
    "amazoncloudwatch": "cloudwatch",
    "awsdatatransfer": "data transfer",
    "aws data transfer": "data transfer",
    "amazonrds": "rds",
    "amazon rds": "rds",
}


def _ce_service_name(service: str) -> str | None:
    """The Cost Explorer SERVICE value for a service a pattern names, or None.

    Single source of truth is connectors/universal.py's alias map, which already
    records what CE returns and carries the comment explaining that CE uses long
    names. This resolves through it rather than restating any CE name here: a
    second copy of "Amazon Elastic Compute Cloud" is how the last set of these
    literals drifted out of date without anyone noticing.
    """
    try:
        from ..connectors.universal import _AWS_ALIASES
    except Exception:  # pragma: no cover - defensive; patterns must never crash
        return None

    key = service.strip().lower()
    key = _SERVICE_SPELLINGS.get(key, key)
    if key in _AWS_ALIASES:
        return _AWS_ALIASES[key]
    # Already a CE name (or a suffixed one): hand it back unchanged.
    if service in set(_AWS_ALIASES.values()):
        return service
    return None


@dataclass
class PatternContext:
    """Input snapshot passed to every pattern checker."""
    # Cost data
    daily_costs: dict[str, list[float]]       # service → daily totals (90 days)
    by_resource: list[dict]                    # [{id, type, service, cost, tags, metadata}]
    snapshots:   list[dict]                    # raw cost_snapshots rows

    # Infrastructure metadata (optional, enriched from connectors)
    ec2_instances: list[dict] = field(default_factory=list)
    rds_instances: list[dict] = field(default_factory=list)
    k8s_nodes:     list[dict] = field(default_factory=list)
    s3_buckets:    list[dict] = field(default_factory=list)
    lambda_funcs:  list[dict] = field(default_factory=list)
    elbs:          list[dict] = field(default_factory=list)

    # Account metadata
    account_id:  str = ""
    region:      str = "us-east-1"
    today:       date = field(default_factory=date.today)

    # Computed helpers
    @property
    def total_monthly_spend(self) -> float:
        return sum(
            sum(v[-30:]) if len(v) >= 30 else (sum(v) / max(len(v), 1) * 30)
            for v in self.daily_costs.values()
        )

    def service_series(self, service: str) -> list[float]:
        """Daily cost series for a service, matched the way Cost Explorer names it.

        Every pattern in this file used to index daily_costs directly with a
        short, readable name: "Amazon EC2", "Amazon S3", "AmazonCloudWatch",
        "AWSDataTransfer". daily_costs is built straight from
        cost_snapshots.service, which holds the raw Cost Explorer SERVICE
        dimension, and Cost Explorer emits none of those. It says "Amazon Elastic
        Compute Cloud - Compute", "Amazon Simple Storage Service", "Amazon
        CloudWatch", "AWS Data Transfer".

        So every one of those lookups returned an empty list, and every pattern
        that gated on the resulting $0 bailed before emitting: the gp2 fallback,
        S3 intelligent tiering, savings-plans coverage, the NAT spike, weekend
        waste. scan_waste_patterns then returned total_monthly_waste: 0.0 with no
        error, which a customer reads as "your account is clean". On a $27,000/mo
        account at 0% Savings Plans coverage, the largest finding the product can
        make was silently unreachable.

        Nothing could catch it. A dict miss returns the default, a $0 service is
        a legitimate state, and a pattern that declines to fire looks identical
        to one that had nothing to say.

        Resolution order, cheapest first:
          1. exact key, so a caller that already passes the CE name is untouched
          2. the CE name from the repo's own alias map, plus its usage-type
             suffixes: CE splits EC2 into "- Compute", "- Data Transfer", "-
             Other", and the compute line is not the whole service
          3. nothing, and $0 is then a real answer rather than a missed lookup
        """
        if service in self.daily_costs:
            return self.daily_costs[service]

        canonical = _ce_service_name(service)
        if canonical is None:
            return []

        # Sum every CE bucket that belongs to this service. "Amazon Elastic
        # Compute Cloud - Compute" and "... - Data Transfer" are both EC2, and a
        # pattern asking what EC2 costs means all of it.
        matched = [
            v for k, v in self.daily_costs.items()
            if k == canonical or k.startswith(canonical + " ")
        ]
        if not matched:
            return []
        width = max(len(v) for v in matched)
        return [sum(v[i] for v in matched if i < len(v)) for i in range(width)]

    def service_monthly(self, service: str) -> float:
        vals = self.service_series(service)
        if not vals:
            return 0.0
        tail = vals[-30:] if len(vals) >= 30 else vals
        return sum(tail) / len(tail) * 30

    def resources_of_type(self, rtype: str) -> list[dict]:
        return [r for r in self.by_resource if r.get("type") == rtype]


@dataclass
class PatternMatch:
    pattern_id:      str
    pattern_name:    str
    category:        str
    severity:        str
    confidence:      float          # 0.0–1.0
    monthly_waste:   float          # USD
    annual_waste:    float          # USD
    evidence:        list[str]      # specific facts supporting the finding
    remediation:     str
    resources:       list[str]      # affected resource IDs / addresses
    tags:            list[str]

    def to_dict(self) -> dict[str, Any]:
        return {
            "pattern_id":    self.pattern_id,
            "pattern_name":  self.pattern_name,
            "category":      self.category,
            "severity":      self.severity,
            "confidence":    round(self.confidence, 2),
            "monthly_waste": round(self.monthly_waste, 2),
            "annual_waste":  round(self.annual_waste, 2),
            "evidence":      self.evidence,
            "remediation":   self.remediation,
            "resources":     self.resources,
            "tags":          self.tags,
        }


def _match(
    pattern_id: str,
    pattern_name: str,
    category: str,
    severity: str,
    confidence: float,
    monthly_waste: float,
    evidence: list[str],
    remediation: str,
    resources: list[str] | None = None,
    tags: list[str] | None = None,
) -> PatternMatch:
    return PatternMatch(
        pattern_id=pattern_id,
        pattern_name=pattern_name,
        category=category,
        severity=severity,
        confidence=confidence,
        monthly_waste=monthly_waste,
        annual_waste=round(monthly_waste * 12, 2),
        evidence=evidence,
        remediation=remediation,
        resources=resources or [],
        tags=tags or [],
    )


# ── Pattern checkers ──────────────────────────────────────────────────────────

def _check_idle_ec2(ctx: PatternContext) -> PatternMatch | None:
    """EC2 instances with low CPU utilisation (< 5% avg) for 14+ days."""
    idle = [
        i for i in ctx.ec2_instances
        if i.get("avg_cpu_pct", 100) < 5.0
        and i.get("days_monitored", 0) >= 14
        and i.get("state") == "running"
    ]
    if not idle:
        return None
    monthly_waste = sum(i.get("monthly_cost", 0) for i in idle)
    if monthly_waste < 20:
        return None
    ids = [i.get("instance_id", "") for i in idle]
    return _match(
        "idle-ec2", "Idle EC2 Instances", "compute", "high",
        confidence=min(0.95, 0.7 + len(idle) * 0.05),
        monthly_waste=monthly_waste,
        evidence=[
            f"{len(idle)} instance(s) with <5% avg CPU for 14+ days",
            f"Examples: {', '.join(ids[:3])}",
        ],
        remediation=(
            "Stop or terminate idle instances. Use AWS Compute Optimizer or "
            "`finops rightsizing` for per-instance recommendations. "
            "Consider Savings Plans for instances that run intermittently."
        ),
        resources=ids,
        tags=["ec2", "idle", "rightsizing"],
    )


def _check_oversized_rds(ctx: PatternContext) -> PatternMatch | None:
    """RDS instances consistently using < 20% of provisioned capacity."""
    oversized = [
        r for r in ctx.rds_instances
        if r.get("avg_cpu_pct", 100) < 20.0
        and r.get("avg_connections", 1000) < 10
        and r.get("days_monitored", 0) >= 14
    ]
    if not oversized:
        return None
    monthly_waste = sum(r.get("monthly_cost", 0) * 0.4 for r in oversized)  # ~40% savings by downsizing
    if monthly_waste < 30:
        return None
    ids = [r.get("db_instance_id", "") for r in oversized]
    return _match(
        "oversized-rds", "Oversized RDS Instances", "database", "high",
        confidence=0.80,
        monthly_waste=monthly_waste,
        evidence=[
            f"{len(oversized)} RDS instance(s) with <20% CPU and <10 connections",
            "Estimated 40% savings by downsizing one instance class",
        ],
        remediation=(
            "Downsize to the next smaller instance class. "
            "Enable Performance Insights to validate before downsizing. "
            "Use Aurora Serverless v2 for variable workloads."
        ),
        resources=ids,
        tags=["rds", "database", "rightsizing"],
    )


def _check_nat_gateway_spike(ctx: PatternContext) -> PatternMatch | None:
    """NAT Gateway data transfer costs > $100/month — often fixable with VPC endpoints."""
    nat_monthly = ctx.service_monthly("AmazonEC2") * 0.0  # placeholder; check tagged NAT costs
    # Look for NAT-tagged costs in by_resource
    nat_costs = [
        r.get("cost", 0) for r in ctx.by_resource
        if "NatGateway" in r.get("type", "") or "nat" in r.get("id", "").lower()
    ]
    monthly = sum(nat_costs) * 30 / max(len(ctx.snapshots) or 1, 1) * 30 if nat_costs else 0.0

    # Also check raw service cost labelled DataTransfer
    dt_monthly = ctx.service_monthly("AWSDataTransfer")
    if dt_monthly < 100 and monthly < 100:
        return None

    total_monthly = max(monthly, dt_monthly)
    confidence = 0.65 if not nat_costs else 0.85
    return _match(
        "nat-gateway-data", "High NAT Gateway / Data Transfer Costs", "network", "medium",
        confidence=confidence,
        monthly_waste=total_monthly * 0.5,   # 50% typically redirectable via VPC endpoints
        evidence=[
            f"Data transfer / NAT costs ~${total_monthly:.0f}/mo",
            "S3, DynamoDB, and many AWS services are free via VPC endpoints",
        ],
        remediation=(
            "Create VPC endpoints for S3, DynamoDB, ECR, and CloudWatch. "
            "Each endpoint eliminates NAT Gateway charges for that service. "
            "Typical saving: 40–70% of NAT Gateway data processing costs."
        ),
        tags=["network", "nat-gateway", "vpc-endpoints", "data-transfer"],
    )


def _check_gp2_volumes(ctx: PatternContext) -> PatternMatch | None:
    """EBS gp2 volumes — gp3 is 20% cheaper with higher baseline performance."""
    gp2 = [
        r for r in ctx.by_resource
        if r.get("type") == "aws_ebs_volume" and r.get("metadata", {}).get("volume_type") == "gp2"
    ]
    if not gp2:
        # Heuristic: if EC2 cost is significant and we have no metadata, flag it
        ec2_monthly = ctx.service_monthly("Amazon EC2")
        if ec2_monthly > 500:
            return _match(
                "gp2-volumes", "gp2 EBS Volumes (migrate to gp3)", "storage", "low",
                confidence=0.40,
                monthly_waste=ec2_monthly * 0.04,   # ~4% of EC2 is often EBS gp2
                evidence=["Estimated based on EC2 spend — verify with `aws ec2 describe-volumes`"],
                remediation="Migrate gp2 volumes to gp3: 20% cost reduction, same or better IOPS.",
                tags=["ebs", "storage", "gp2", "gp3"],
            )
        return None

    total_gb = sum(r.get("metadata", {}).get("size_gb", 0) for r in gp2)
    monthly_waste = total_gb * (0.10 - 0.08)   # $0.10 gp2 vs $0.08 gp3
    if monthly_waste < 5:
        return None
    return _match(
        "gp2-volumes", "gp2 EBS Volumes (migrate to gp3)", "storage", "medium",
        confidence=0.95,
        monthly_waste=monthly_waste,
        evidence=[
            f"{len(gp2)} gp2 volumes totalling {total_gb:.0f} GB",
            "gp3 is 20% cheaper with higher baseline IOPS and throughput",
        ],
        remediation=(
            "Run: aws ec2 modify-volume --volume-type gp3 --volume-id <id>  "
            "No downtime required. gp3 baseline: 3,000 IOPS, 125 MB/s (vs gp2's 3 IOPS/GB)."
        ),
        resources=[r.get("id", "") for r in gp2],
        tags=["ebs", "storage", "gp2", "gp3"],
    )


def _check_unused_load_balancers(ctx: PatternContext) -> PatternMatch | None:
    """Load balancers with zero healthy targets for 7+ days."""
    empty = [
        lb for lb in ctx.elbs
        if lb.get("healthy_host_count", 1) == 0
        and lb.get("days_empty", 0) >= 7
    ]
    if not empty:
        return None
    monthly = len(empty) * 0.008 * 730   # ALB base rate
    if monthly < 10:
        return None
    names = [lb.get("name", "") for lb in empty]
    return _match(
        "empty-load-balancers", "Load Balancers with No Healthy Targets", "compute", "high",
        confidence=0.92,
        monthly_waste=monthly,
        evidence=[
            f"{len(empty)} load balancer(s) with 0 healthy targets for 7+ days",
            f"Names: {', '.join(names[:5])}",
        ],
        remediation=(
            "Delete unused load balancers. Check if they're attached to stopped "
            "Auto Scaling groups or orphaned after a service shutdown."
        ),
        resources=names,
        tags=["ec2", "load-balancer", "idle", "orphaned"],
    )


def _check_weekend_waste(ctx: PatternContext) -> PatternMatch | None:
    """
    Compute spend on weekends similar to weekdays — dev/staging should be stopped.
    Weekend cost should ideally be <20% of weekday cost for non-prod workloads.
    """
    # service_series, not daily_costs.get: the raw dict is keyed on the CE
    # SERVICE value, and "Amazon EC2" is not one. This read returned [] on
    # every real account.
    ec2_series = ctx.service_series("Amazon EC2")
    if len(ec2_series) < 14:
        return None

    # We don't have actual day-of-week data here — approximate from index
    # Assumes series starts on a Monday (common for Cost Explorer exports)
    weekday_costs = [ec2_series[i] for i in range(len(ec2_series)) if i % 7 not in (5, 6)]
    weekend_costs = [ec2_series[i] for i in range(len(ec2_series)) if i % 7 in (5, 6)]

    if not weekday_costs or not weekend_costs:
        return None

    import statistics as _stats
    wd_avg = _stats.mean(weekday_costs)
    we_avg = _stats.mean(weekend_costs)

    if wd_avg == 0:
        return None
    ratio = we_avg / wd_avg

    if ratio < 0.7:   # weekends already lower
        return None

    monthly_weekend = we_avg * 8   # ~8 weekend days/month
    saveable = monthly_weekend * (1 - 0.2)   # assume 80% could be stopped
    if saveable < 50:
        return None

    return _match(
        "weekend-waste", "Dev/Staging Compute Running on Weekends", "compute", "medium",
        confidence=0.65,
        monthly_waste=saveable,
        evidence=[
            f"Weekend EC2 spend is {ratio*100:.0f}% of weekday spend",
            f"Weekend daily avg: ${we_avg:.0f} vs weekday avg: ${wd_avg:.0f}",
            "Dev/staging environments typically don't need 24/7 uptime",
        ],
        remediation=(
            "Use AWS Instance Scheduler or tag dev instances and apply "
            "an auto-stop/start schedule (Mon–Fri 8am–8pm). "
            "Typical saving: 65% of weekend compute cost."
        ),
        tags=["ec2", "scheduling", "dev", "staging", "weekend"],
    )


def _check_single_az_multi_az_opportunity(ctx: PatternContext) -> PatternMatch | None:
    """Multi-AZ RDS in dev/staging environments — waste on non-prod."""
    multi_az_dev = [
        r for r in ctx.rds_instances
        if r.get("multi_az") is True
        and any(
            tag in str(r.get("tags", {})).lower()
            for tag in ("dev", "staging", "test", "qa", "sandbox")
        )
    ]
    if not multi_az_dev:
        return None
    monthly_waste = sum(r.get("monthly_cost", 0) * 0.5 for r in multi_az_dev)
    if monthly_waste < 30:
        return None
    return _match(
        "multi-az-dev", "Multi-AZ RDS in Non-Production Environments", "database", "medium",
        confidence=0.88,
        monthly_waste=monthly_waste,
        evidence=[
            f"{len(multi_az_dev)} RDS instance(s) with Multi-AZ in dev/staging",
            "Multi-AZ doubles the instance cost — unnecessary for non-prod",
        ],
        remediation=(
            "Disable Multi-AZ on dev/staging RDS instances. "
            "Single-AZ is sufficient for non-production workloads."
        ),
        resources=[r.get("db_instance_id", "") for r in multi_az_dev],
        tags=["rds", "database", "multi-az", "dev", "staging"],
    )


def _check_overprovisioned_lambda(ctx: PatternContext) -> PatternMatch | None:
    """Lambda functions with <30% max memory utilisation."""
    overprovisioned = [
        f for f in ctx.lambda_funcs
        if f.get("max_memory_pct", 100) < 30.0
        and f.get("invocations_monthly", 0) > 10_000
    ]
    if not overprovisioned:
        return None
    # Each halving of memory halves Lambda compute cost
    monthly_waste = sum(
        f.get("monthly_cost", 0) * (1 - f.get("max_memory_pct", 30) / 100 * 1.5)
        for f in overprovisioned
    )
    if monthly_waste < 10:
        return None
    names = [f.get("function_name", "") for f in overprovisioned]
    return _match(
        "oversized-lambda", "Over-Provisioned Lambda Memory", "compute", "low",
        confidence=0.80,
        monthly_waste=monthly_waste,
        evidence=[
            f"{len(overprovisioned)} Lambda function(s) using <30% of provisioned memory",
            "Lambda compute cost is proportional to memory × duration",
        ],
        remediation=(
            "Use AWS Lambda Power Tuning (open-source Step Functions state machine) "
            "to find the optimal memory setting. Usually 256–512 MB is sufficient "
            "for most functions currently set to 1024 MB+."
        ),
        resources=names,
        tags=["lambda", "serverless", "rightsizing", "memory"],
    )


def _check_s3_intelligent_tiering(ctx: PatternContext) -> PatternMatch | None:
    """S3 buckets with Standard storage but infrequent access patterns."""
    s3_monthly = ctx.service_monthly("Amazon S3")
    if s3_monthly < 50:
        return None

    old_buckets = [
        b for b in ctx.s3_buckets
        if b.get("storage_class", "STANDARD") == "STANDARD"
        and b.get("avg_get_requests_daily", 1000) < 100
        and b.get("size_gb", 0) > 100
    ]
    if not old_buckets:
        # Heuristic: assume 40% of S3 spend is standard-storage that could move
        monthly_waste = s3_monthly * 0.30
        return _match(
            "s3-tiering", "S3 Standard Storage — Consider Intelligent Tiering", "storage", "low",
            confidence=0.45,
            monthly_waste=monthly_waste,
            evidence=[
                f"S3 spend is ${s3_monthly:.0f}/mo",
                "Objects not accessed in 30 days auto-tier to Infrequent Access",
                "S3 Intelligent-Tiering has no retrieval fees and no min duration",
            ],
            remediation=(
                "Enable S3 Intelligent-Tiering on buckets with mixed access patterns. "
                "Typical saving: 20–40% on storage costs for buckets > 6 months old."
            ),
            tags=["s3", "storage", "tiering", "intelligent-tiering"],
        )

    total_gb = sum(b.get("size_gb", 0) for b in old_buckets)
    monthly_waste = total_gb * (0.023 - 0.0125)   # STANDARD vs IA
    return _match(
        "s3-tiering", "S3 Standard Storage — Switch to Intelligent Tiering", "storage", "medium",
        confidence=0.75,
        monthly_waste=monthly_waste,
        evidence=[
            f"{len(old_buckets)} bucket(s) with {total_gb:.0f} GB STANDARD storage, <100 daily GETs",
        ],
        remediation="Enable S3 Intelligent-Tiering lifecycle policy on identified buckets.",
        resources=[b.get("name", "") for b in old_buckets],
        tags=["s3", "storage", "tiering"],
    )


def _check_savings_plans_coverage(ctx: PatternContext) -> PatternMatch | None:
    """
    High EC2/Fargate spend with no Savings Plans — usually > 30% saving available.
    Requires commitment metadata from the connectors.
    """
    ec2_monthly = ctx.service_monthly("Amazon EC2")
    fargate_monthly = ctx.service_monthly("AWS Fargate")
    total = ec2_monthly + fargate_monthly

    if total < 200:
        return None

    # Check if any savings plans coverage exists in the context
    sp_coverage = 0.0
    for snap in ctx.snapshots:
        if snap.get("savings_plan_coverage_pct"):
            sp_coverage = snap.get("savings_plan_coverage_pct", 0.0)
            break

    if sp_coverage > 70:
        return None

    uncovered = total * (1 - sp_coverage / 100)
    potential_saving = uncovered * 0.32   # avg Compute SP saving

    return _match(
        "no-savings-plans", "Compute Spend Without Savings Plans Coverage", "compute", "high",
        confidence=0.85,
        monthly_waste=potential_saving,
        evidence=[
            f"${total:.0f}/mo EC2+Fargate spend with {sp_coverage:.0f}% Savings Plans coverage",
            f"${uncovered:.0f}/mo on-demand — Compute Savings Plans typically save 32%",
        ],
        remediation=(
            "Purchase 1-year Compute Savings Plans for your baseline compute spend. "
            "Start conservative (60% of current spend) — Savings Plans are flexible "
            "across instance families, regions, and OS. No-upfront option available."
        ),
        tags=["savings-plans", "commitment", "ec2", "fargate", "cost-optimisation"],
    )


def _check_llm_cost_concentration(ctx: PatternContext) -> PatternMatch | None:
    """
    AI/LLM spend concentrated on expensive frontier models where cheaper alternatives exist.
    """
    openai_monthly  = ctx.service_monthly("OpenAI")
    anthropic_monthly = ctx.service_monthly("Anthropic")
    bedrock_monthly = ctx.service_monthly("Amazon Bedrock")
    total_ai = openai_monthly + anthropic_monthly + bedrock_monthly

    if total_ai < 100:
        return None

    # Look for GPT-4/Claude Opus usage in by_resource
    expensive_models = [
        r for r in ctx.by_resource
        if any(m in r.get("id", "").lower()
               for m in ("gpt-4o", "gpt-4-turbo", "claude-3-opus", "o1"))
        and r.get("cost", 0) > 50
    ]

    if not expensive_models and total_ai < 500:
        return None

    monthly_waste = (
        sum(r.get("cost", 0) * 0.80 for r in expensive_models)
        if expensive_models else total_ai * 0.50
    )

    evidence = [f"Total AI/LLM spend: ${total_ai:.0f}/mo"]
    if expensive_models:
        evidence.append(
            f"{len(expensive_models)} expensive model(s) with high spend: "
            + ", ".join(r.get("id", "") for r in expensive_models[:3])
        )
    else:
        evidence.append("High AI spend — model breakdown not available, run `get_llm_cost_by_model`")

    return _match(
        "llm-model-selection", "AI/LLM Spend Concentrated on Expensive Models", "ai", "high",
        confidence=0.70 if expensive_models else 0.45,
        monthly_waste=monthly_waste,
        evidence=evidence,
        remediation=(
            "Route lower-complexity tasks to cheaper models: "
            "GPT-4o → GPT-4o-mini (90% cheaper), "
            "Claude Opus → Claude Haiku (97% cheaper). "
            "Use `get_llm_unit_economics` to measure cost per request."
        ),
        tags=["ai", "llm", "openai", "anthropic", "model-selection"],
    )


def _check_untagged_resources(ctx: PatternContext) -> PatternMatch | None:
    """Resources without required tags — prevents accurate cost attribution."""
    required = ["team", "environment", "service"]
    untagged = [
        r for r in ctx.by_resource
        if r.get("cost", 0) > 5
        and any(t not in (r.get("tags") or {}) for t in required)
    ]
    if not untagged:
        return None
    monthly_unattributed = sum(r.get("cost", 0) for r in untagged)
    if monthly_unattributed < 50:
        return None
    pct = round(monthly_unattributed / max(ctx.total_monthly_spend, 1) * 100, 1)
    return _match(
        "untagged-resources", "Untagged Resources — Cost Attribution Gap", "governance", "medium",
        confidence=0.95,
        monthly_waste=0,   # governance issue, not direct waste
        evidence=[
            f"{len(untagged)} resource(s) missing required tags (team/environment/service)",
            f"${monthly_unattributed:.0f}/mo ({pct}% of total spend) is unattributed",
        ],
        remediation=(
            "Apply required tags via `finops terraform audit` for IaC-managed resources. "
            "Set up AWS Config rule `required-tags` for ongoing enforcement. "
            "Use tag policies via AWS Organizations."
        ),
        resources=[r.get("id", "") for r in untagged[:20]],
        tags=["tagging", "governance", "attribution", "compliance"],
    )


def _check_cloudwatch_log_retention(ctx: PatternContext) -> PatternMatch | None:
    """CloudWatch Log Groups with no retention policy — logs accumulate forever at $0.03/GB."""
    cw_monthly = ctx.service_monthly("AmazonCloudWatch")
    if cw_monthly < 30:
        return None
    # Heuristic: 40% of CW cost is often storage from unbounded log groups
    return _match(
        "log-retention", "CloudWatch Log Groups Without Retention Policy", "storage", "low",
        confidence=0.60,
        monthly_waste=cw_monthly * 0.40,
        evidence=[
            f"CloudWatch spend: ${cw_monthly:.0f}/mo",
            "Log groups without retention policies accumulate indefinitely at $0.03/GB/mo",
        ],
        remediation=(
            "Set retention policies on all CloudWatch Log Groups: "
            "30 days for dev, 90 days for prod (or match your compliance requirement). "
            "Use: aws logs put-retention-policy --retention-in-days 30"
        ),
        tags=["cloudwatch", "logs", "storage", "retention"],
    )


# ── Pattern registry ──────────────────────────────────────────────────────────

_PATTERNS: list[Callable[[PatternContext], PatternMatch | None]] = [
    _check_idle_ec2,
    _check_oversized_rds,
    _check_nat_gateway_spike,
    _check_gp2_volumes,
    _check_unused_load_balancers,
    _check_weekend_waste,
    _check_single_az_multi_az_opportunity,
    _check_overprovisioned_lambda,
    _check_s3_intelligent_tiering,
    _check_savings_plans_coverage,
    _check_llm_cost_concentration,
    _check_untagged_resources,
    _check_cloudwatch_log_retention,
]


# ── Public API ─────────────────────────────────────────────────────────────────

def scan(
    ctx: PatternContext,
    min_monthly_waste: float = 0.0,
    categories: list[str] | None = None,
) -> list[PatternMatch]:
    """
    Run all patterns against the provided context.

    Args:
        ctx:               PatternContext built from cost snapshots + infra metadata
        min_monthly_waste: filter out findings below this threshold
        categories:        optional filter (e.g. ["compute", "storage"])

    Returns:
        List of PatternMatch, sorted by monthly_waste descending.
    """
    matches: list[PatternMatch] = []
    for checker in _PATTERNS:
        try:
            match = checker(ctx)
            if match is None:
                continue
            if match.monthly_waste < min_monthly_waste:
                continue
            if categories and match.category not in categories:
                continue
            matches.append(match)
        except Exception as exc:
            log.debug("pattern %s failed: %s", checker.__name__, exc)

    return sorted(matches, key=lambda m: m.monthly_waste, reverse=True)


def scan_dict(
    ctx: PatternContext,
    min_monthly_waste: float = 0.0,
    categories: list[str] | None = None,
) -> dict[str, Any]:
    """
    Run all patterns and return a structured summary dict.

    Returns:
        {
          "total_monthly_waste": float,
          "total_annual_waste":  float,
          "pattern_count":       int,
          "findings": [PatternMatch.to_dict(), ...],
          "by_category": {"compute": [...], "storage": [...], ...},
          "by_severity": {"critical": [...], "high": [...], ...},
        }
    """
    matches = scan(ctx, min_monthly_waste, categories)
    total_monthly = sum(m.monthly_waste for m in matches)

    by_category: dict[str, list] = {}
    by_severity: dict[str, list] = {}
    for m in matches:
        by_category.setdefault(m.category, []).append(m.to_dict())
        by_severity.setdefault(m.severity, []).append(m.to_dict())

    return {
        "total_monthly_waste": round(total_monthly, 2),
        "total_annual_waste":  round(total_monthly * 12, 2),
        "pattern_count":       len(matches),
        "findings":            [m.to_dict() for m in matches],
        "by_category":         by_category,
        "by_severity":         by_severity,
    }
