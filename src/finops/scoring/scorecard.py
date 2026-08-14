"""
FinOps Efficiency Scorecard

Produces a 0–100 score (+ letter grade) for any slice of your infrastructure:
  - Overall (all providers combined)
  - Per team (via cost tags: team=platform, team=data, etc.)
  - Per environment (prod, staging, dev)
  - Per provider (aws, gcp, azure, kubernetes)
  - Per cloud account
  - Kubernetes cluster / namespace

Five scored dimensions
──────────────────────
  compute_efficiency   25 pts   CPU & memory utilization vs requests
  waste_reduction      25 pts   idle resources, over-provisioned pods, orphaned releases
  commitment_coverage  20 pts   % of compute spend under RIs / SPs
  tag_hygiene          15 pts   % of resources with required tags
  anomaly_response     15 pts   % of cost anomalies acknowledged within 48h

Score → Grade
─────────────
  90–100  A   World-class FinOps practice
  75–89   B   Good, a few gaps to close
  60–74   C   Significant improvement possible
  40–59   D   High waste, low visibility
   0–39   F   FinOps not yet practiced

Each dimension includes:
  - Raw score (0–100)
  - Weighted contribution to total
  - Key findings that drove the score
  - Top 3 specific actions to improve it

Scorecard is stored in the DB for trend tracking — you can see if your
score is improving week-over-week.
"""
from __future__ import annotations

import json
import logging
import statistics
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from typing import Any

log = logging.getLogger("finops.scoring")

# ── Dimension weights (must sum to 100) ───────────────────────────────────────
WEIGHTS = {
    "compute_efficiency":  25,
    "waste_reduction":     25,
    "commitment_coverage": 20,
    "tag_hygiene":         15,
    "anomaly_response":    15,
}

_GRADE_MAP = [
    (90, "A"), (75, "B"), (60, "C"), (40, "D"), (0, "F"),
]


def _grade(score: float) -> str:
    for threshold, letter in _GRADE_MAP:
        if score >= threshold:
            return letter
    return "F"


def _clamp(v: float, lo: float = 0.0, hi: float = 100.0) -> float:
    return max(lo, min(hi, v))


# Metadata keys one dimension publishes and build_scorecard reads back out. A
# dimension's metadata is otherwise free-form and local to that dimension; these
# five are the only cross-boundary contract, and they are the only ones where a
# producer that forgets to publish costs the customer a number.
#
# The cost of not naming them: _score_commitment_coverage computed
# potential_savings_usd into a local, never wrote it to meta, and build_scorecard
# read it back with .get(key, 0). A dict default cannot tell "never published"
# from "genuinely zero", so an account at 10% coverage with a $16,000/mo Savings
# Plan opportunity was graded worst on commitments and told it could recover $0,
# for as long as that code has existed, with nothing red anywhere.
_CROSS_DIMENSION_KEYS: dict[str, str] = {
    "total_waste_usd": "waste_reduction",
    "potential_savings_usd": "commitment_coverage",
}


def _require_published(dim: "DimensionScore", key: str) -> float:
    """Read a cross-dimension metadata key, refusing to invent a zero for it.

    A dimension that could not be computed says so with data_available=False and
    contributes nothing, which is a real answer. A dimension that ran and then
    failed to publish its key is a wiring bug, and silently scoring it as $0 is
    how this one survived. Absent-but-available is loud; everything else keeps
    the scorecard building, because a partial score beats no score.
    """
    if key in dim.metadata:
        return float(dim.metadata[key] or 0)
    if not dim.data_available:
        return 0.0
    raise KeyError(
        f"dimension {dim.name!r} ran with data available but never published "
        f"{key!r}, which build_scorecard needs for the recoverable total. "
        f"It published: {sorted(dim.metadata)}"
    )


# ── Score result dataclasses ──────────────────────────────────────────────────

@dataclass
class DimensionScore:
    name: str
    display_name: str
    raw_score: float          # 0–100
    weight: int               # pts this dimension is worth
    weighted_score: float     # raw_score * weight / 100
    grade: str
    findings: list[str]       # what drove the score
    actions: list[str]        # top 3 specific things to improve
    data_available: bool = True
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class Scorecard:
    # Identity
    scope: str                # "overall" | "team:platform" | "env:prod" | "provider:aws" | "k8s:cluster-name"
    label: str                # human name e.g. "Platform team" / "Production" / "AWS"
    generated_at: str

    # Overall
    total_score: float        # 0–100 weighted sum
    grade: str
    trend: str                # "improving" | "declining" | "stable" | "no_history"
    trend_delta: float        # pts change vs 7 days ago

    # Dimension breakdown
    dimensions: list[DimensionScore]

    # Financial context
    monthly_spend_usd: float
    potential_savings_usd: float  # realistically recoverable

    # Summary
    summary: str
    top_wins: list[str]       # the 3 highest-impact things to do now

    def as_dict(self) -> dict[str, Any]:
        return {
            "scope": self.scope,
            "label": self.label,
            "generated_at": self.generated_at,
            "total_score": round(self.total_score, 1),
            "grade": self.grade,
            "trend": self.trend,
            "trend_delta": round(self.trend_delta, 1),
            "monthly_spend_usd": self.monthly_spend_usd,
            "potential_savings_usd": self.potential_savings_usd,
            "summary": self.summary,
            "top_wins": self.top_wins,
            "dimensions": [
                {
                    "name": d.name,
                    "display_name": d.display_name,
                    "score": round(d.raw_score, 1),
                    "grade": d.grade,
                    "weight": d.weight,
                    "weighted_contribution": round(d.weighted_score, 1),
                    "data_available": d.data_available,
                    "findings": d.findings,
                    "actions": d.actions,
                    "metadata": d.metadata,
                }
                for d in self.dimensions
            ],
        }


# ── Individual dimension scorers ──────────────────────────────────────────────

def _score_compute_efficiency(
    k8s_reports: list | None = None,           # list[ClusterReport]
    ec2_rightsizing: list | None = None,       # list[RightsizingRec]
) -> DimensionScore:
    """
    Score based on CPU/memory utilization across Kubernetes and EC2.
    High utilization = high score. Over-provisioned = penalty.
    """
    findings: list[str] = []
    actions:  list[str] = []
    scores:   list[float] = []
    meta:     dict[str, Any] = {}

    # ── Kubernetes ────────────────────────────────────────────────────────────
    if k8s_reports:
        cluster_effs = []
        for r in k8s_reports:
            if r.overall_cpu_efficiency is not None:
                cluster_effs.append(r.overall_cpu_efficiency)
                if r.overall_cpu_efficiency < 40:
                    findings.append(
                        f"Cluster '{r.cluster}': {r.overall_cpu_efficiency:.0f}% CPU efficiency "
                        f"(${r.wasted_monthly_cost:,.0f}/mo wasted)"
                    )

        if cluster_effs:
            avg_eff = statistics.mean(cluster_effs)
            # 70% utilisation = 100 score; below 20% = 0
            k8s_score = _clamp((avg_eff - 20) / 50 * 100)
            scores.append(k8s_score)
            meta["k8s_avg_cpu_efficiency_pct"] = round(avg_eff, 1)

            if avg_eff < 50:
                actions.append(
                    f"Reduce pod CPU/memory requests to match actual usage — "
                    f"average cluster efficiency is {avg_eff:.0f}%"
                )

    # ── EC2 rightsizing ───────────────────────────────────────────────────────
    if ec2_rightsizing:
        total_instances = max(len(ec2_rightsizing), 1)
        oversized = [r for r in ec2_rightsizing if hasattr(r, "avg_cpu_pct") and r.avg_cpu_pct < 20]
        oversized_pct = len(oversized) / total_instances * 100
        ec2_score = _clamp(100 - oversized_pct * 1.5)
        scores.append(ec2_score)
        meta["ec2_oversized_instances"] = len(oversized)
        meta["ec2_total_analysed"] = total_instances

        if oversized:
            total_savings = sum(getattr(r, "monthly_savings_usd", 0) for r in oversized)
            findings.append(
                f"{len(oversized)} EC2 instance(s) running below 20% CPU — "
                f"${total_savings:,.0f}/month rightsizing opportunity"
            )
            actions.append(
                f"Rightsize {len(oversized)} over-provisioned EC2 instance(s) "
                f"to save ~${total_savings:,.0f}/month"
            )

    if not scores:
        return DimensionScore(
            name="compute_efficiency", display_name="Compute Efficiency",
            raw_score=50, weight=WEIGHTS["compute_efficiency"],
            weighted_score=50 * WEIGHTS["compute_efficiency"] / 100,
            grade="C", findings=["No compute utilisation data available yet — "
                                  "enable metrics-server for Kubernetes clusters"],
            actions=["Enable metrics-server to get real utilisation data"],
            data_available=False, metadata=meta,
        )

    raw = statistics.mean(scores)
    if not actions:
        actions.append("Maintain current compute efficiency — continue monitoring utilisation trends")

    return DimensionScore(
        name="compute_efficiency", display_name="Compute Efficiency",
        raw_score=raw, weight=WEIGHTS["compute_efficiency"],
        weighted_score=raw * WEIGHTS["compute_efficiency"] / 100,
        grade=_grade(raw), findings=findings, actions=actions, metadata=meta,
    )


def _score_waste_reduction(
    idle_resources: list[dict] | None = None,
    k8s_reports: list | None = None,
    orphaned_helm_releases: list | None = None,
    total_spend: float = 0.0,
) -> DimensionScore:
    """
    Score based on how much spend is demonstrably wasted:
    idle EC2/RDS, over-provisioned pods, orphaned Helm releases, stopped instances.
    """
    findings: list[str] = []
    actions:  list[str] = []
    meta:     dict[str, Any] = {}

    total_waste = 0.0

    # ── Idle cloud resources ───────────────────────────────────────────────────
    if idle_resources:
        idle_cost = sum(r.get("monthly_cost_usd", 0) for r in idle_resources)
        total_waste += idle_cost
        meta["idle_resource_count"] = len(idle_resources)
        meta["idle_resource_cost_usd"] = round(idle_cost, 2)

        if idle_cost > 50:
            findings.append(
                f"{len(idle_resources)} idle resource(s) costing ${idle_cost:,.0f}/month "
                f"(stopped EC2, unused EIPs, detached EBS, idle RDS)"
            )
            actions.append(
                f"Terminate or snapshot {len(idle_resources)} idle resource(s) "
                f"to save ${idle_cost:,.0f}/month"
            )

    # ── Kubernetes waste ───────────────────────────────────────────────────────
    if k8s_reports:
        k8s_waste = sum(r.wasted_monthly_cost for r in k8s_reports)
        k8s_total = sum(r.total_monthly_cost for r in k8s_reports)
        total_waste += k8s_waste
        meta["k8s_wasted_usd"] = round(k8s_waste, 2)

        idle_nodes = sum(len(r.idle_nodes) for r in k8s_reports)
        if idle_nodes:
            findings.append(f"{idle_nodes} idle Kubernetes node(s) detected")
            actions.append(f"Drain and terminate {idle_nodes} idle node(s)")

        if k8s_waste > 50:
            findings.append(
                f"${k8s_waste:,.0f}/month wasted on over-provisioned pod requests "
                f"({k8s_waste / k8s_total * 100:.0f}% of cluster cost)" if k8s_total else
                f"${k8s_waste:,.0f}/month wasted on over-provisioned pod requests"
            )

    # ── Orphaned Helm releases ────────────────────────────────────────────────
    if orphaned_helm_releases:
        orphan_cost = sum(r.monthly_cost if hasattr(r, "monthly_cost") else r.get("monthly_cost_usd", 0)
                         for r in orphaned_helm_releases)
        total_waste += orphan_cost
        meta["orphaned_helm_releases"] = len(orphaned_helm_releases)
        findings.append(
            f"{len(orphaned_helm_releases)} orphaned Helm release(s) — "
            f"deployed but no running pods"
        )
        actions.append(
            f"Run `helm uninstall` on {len(orphaned_helm_releases)} orphaned release(s)"
        )

    # ── Score: waste as % of total spend ─────────────────────────────────────
    meta["total_waste_usd"] = round(total_waste, 2)
    if total_spend > 0:
        waste_pct = total_waste / total_spend * 100
        meta["waste_pct_of_spend"] = round(waste_pct, 1)
        # 0% waste = 100, 30%+ waste = 0
        raw = _clamp(100 - waste_pct * 3.3)
        if not findings:
            findings.append(
                f"Waste is {waste_pct:.1f}% of total spend (${total_waste:,.0f}/month)"
                if waste_pct > 2 else "Minimal detected waste — good discipline"
            )
    elif total_waste > 0:
        raw = 40.0  # waste detected but no total spend for context
        if not findings:
            findings.append(f"${total_waste:,.0f}/month in detected waste")
    else:
        raw = 75.0  # no waste detected (could be no data)
        findings.append("No significant waste detected in available data")

    if not actions:
        actions.append("Continue monitoring for idle and over-provisioned resources")

    return DimensionScore(
        name="waste_reduction", display_name="Waste Reduction",
        raw_score=raw, weight=WEIGHTS["waste_reduction"],
        weighted_score=raw * WEIGHTS["waste_reduction"] / 100,
        grade=_grade(raw), findings=findings, actions=actions, metadata=meta,
    )


def _score_commitment_coverage(
    commitment_data: dict | None = None,
    provider: str = "aws",
    tag_filter: dict | None = None,   # {"team": "platform"} etc.
) -> DimensionScore:
    """
    Score based on % of eligible compute spend covered by RIs / Savings Plans.
    Target: 70%+ coverage = A grade.
    """
    findings: list[str] = []
    actions:  list[str] = []
    meta:     dict[str, Any] = {}

    # Surface the measurement caveat when scoped by tag
    if tag_filter:
        tag_key, tag_val = next(iter(tag_filter.items()))
        meta["scope_note"] = (
            f"Coverage measured for EC2 usage tagged {tag_key}={tag_val}. "
            f"SP/RI utilization figures are account-level (AWS limitation — "
            f"utilization cannot be filtered by tag)."
        )

    if not commitment_data:
        return DimensionScore(
            name="commitment_coverage", display_name="Commitment Coverage",
            raw_score=50, weight=WEIGHTS["commitment_coverage"],
            weighted_score=50 * WEIGHTS["commitment_coverage"] / 100,
            grade="C",
            findings=["No commitment data available — connect AWS Cost Explorer to analyse"],
            actions=["Run `get_commitment_analysis` to see RI/SP purchase opportunities"],
            data_available=False, metadata=meta,
        )

    coverage_pct = commitment_data.get("coverage_pct", 0)
    on_demand_spend = commitment_data.get("on_demand_usd", 0)
    potential_savings = commitment_data.get("potential_savings_usd", 0)
    meta["coverage_pct"] = coverage_pct
    meta["on_demand_spend_usd"] = on_demand_spend
    # build_scorecard reads this key back out to compute the headline
    # "Estimated $X/month recoverable". It was computed into the local above and
    # never published, so the read always found the .get() default and the
    # commitment component of the recoverable total was silently zero: an account
    # at 10% coverage with a $16,000/mo Savings Plan opportunity was graded worst
    # on commitments and then told it could recover $0. The dollar figure did
    # survive verbatim in the actions text below, which is why the report named
    # the problem, priced it, and still totalled it at nothing.
    #
    # This is why _CROSS_DIMENSION_KEYS exists below: .get(key, 0) cannot tell
    # "the producer never wrote it" from "the producer wrote zero", so nothing
    # about this failure was observable from either side.
    meta["potential_savings_usd"] = potential_savings

    # 70% coverage = 100 score; 0% = 0; penalise for high on-demand waste
    raw = _clamp(coverage_pct / 70 * 100)

    if coverage_pct < 30:
        findings.append(
            f"Only {coverage_pct:.0f}% of compute is under commitments — "
            f"${on_demand_spend:,.0f}/month at full on-demand rates"
        )
        if potential_savings > 100:
            actions.append(
                f"Purchase Savings Plans or Reserved Instances to cover "
                f"${on_demand_spend:,.0f}/month of on-demand spend — "
                f"estimated savings: ${potential_savings:,.0f}/month"
            )
    elif coverage_pct < 60:
        findings.append(f"{coverage_pct:.0f}% commitment coverage — room to improve")
        if potential_savings > 50:
            actions.append(f"Increase commitment coverage to 70%+ — ${potential_savings:,.0f}/month opportunity")
    else:
        findings.append(f"Strong commitment coverage at {coverage_pct:.0f}%")
        actions.append("Review commitments annually to ensure they match current usage patterns")

    return DimensionScore(
        name="commitment_coverage", display_name="Commitment Coverage",
        raw_score=raw, weight=WEIGHTS["commitment_coverage"],
        weighted_score=raw * WEIGHTS["commitment_coverage"] / 100,
        grade=_grade(raw), findings=findings, actions=actions, metadata=meta,
    )


def _score_tag_hygiene(
    tag_coverage: dict[str, float] | None = None,   # {tag_key: coverage_pct}
    required_tags: list[str] | None = None,
    untagged_spend_usd: float = 0.0,
    total_spend: float = 0.0,
) -> DimensionScore:
    """
    Score based on what % of resources have required cost allocation tags.
    Default required tags: team, env, service (configurable).
    """
    findings: list[str] = []
    actions:  list[str] = []
    meta:     dict[str, Any] = {}

    required = required_tags or ["team", "env", "service"]
    meta["required_tags"] = required

    if not tag_coverage:
        # Estimate from untagged spend
        if total_spend > 0 and untagged_spend_usd > 0:
            untagged_pct = untagged_spend_usd / total_spend * 100
            raw = _clamp(100 - untagged_pct)
            meta["untagged_spend_usd"] = untagged_spend_usd
            meta["untagged_pct"] = round(untagged_pct, 1)
            findings.append(
                f"${untagged_spend_usd:,.0f}/month ({untagged_pct:.0f}% of spend) "
                f"cannot be attributed to a team or service"
            )
            actions.append(
                f"Tag resources with 'team', 'env', 'service' — "
                f"${untagged_spend_usd:,.0f}/month is currently unattributed"
            )
        else:
            return DimensionScore(
                name="tag_hygiene", display_name="Tag Hygiene",
                raw_score=50, weight=WEIGHTS["tag_hygiene"],
                weighted_score=50 * WEIGHTS["tag_hygiene"] / 100,
                grade="C",
                findings=["Tag coverage data not yet available"],
                actions=[f"Ensure all resources have tags: {', '.join(required)}"],
                data_available=False, metadata=meta,
            )
    else:
        # Score each required tag, average them
        per_tag_scores = []
        for tag in required:
            cov = tag_coverage.get(tag, 0.0)
            per_tag_scores.append(cov)
            meta[f"coverage_{tag}_pct"] = round(cov, 1)
            if cov < 80:
                findings.append(f"'{tag}' tag missing on {100 - cov:.0f}% of resources")
                actions.append(f"Enforce '{tag}' tag — currently only {cov:.0f}% coverage")

        raw = _clamp(statistics.mean(per_tag_scores))
        if not findings:
            findings.append(
                f"Good tag coverage — avg {raw:.0f}% across required tags "
                f"({', '.join(required)})"
            )

    if not actions:
        actions.append("Maintain tag coverage with AWS Config / GCP Policy enforcement")

    return DimensionScore(
        name="tag_hygiene", display_name="Tag Hygiene",
        raw_score=raw, weight=WEIGHTS["tag_hygiene"],
        weighted_score=raw * WEIGHTS["tag_hygiene"] / 100,
        grade=_grade(raw), findings=findings, actions=actions, metadata=meta,
    )


def _score_anomaly_response(
    lookback_days: int = 30,
    response_window_hours: int = 48,
) -> DimensionScore:
    """
    Score based on how quickly anomalies are acknowledged.
    100% ack within 48h = 100. Large unacknowledged backlog = low score.
    """
    findings: list[str] = []
    actions:  list[str] = []
    meta:     dict[str, Any] = {}

    try:
        from ..storage.db import anomalies, get_engine
        from sqlalchemy import select, and_, func

        engine = get_engine()
        cutoff = (datetime.now(timezone.utc) - timedelta(days=lookback_days)).isoformat()
        ack_cutoff = (datetime.now(timezone.utc) - timedelta(hours=response_window_hours))

        with engine.connect() as conn:
            total_q = conn.execute(
                select(func.count()).where(anomalies.c.detected_at >= cutoff)
            ).scalar() or 0

            unacked_old_q = conn.execute(
                select(func.count()).where(
                    and_(
                        anomalies.c.detected_at >= cutoff,
                        anomalies.c.detected_at <= ack_cutoff.isoformat(),
                        anomalies.c.acknowledged == False,  # noqa: E712
                    )
                )
            ).scalar() or 0

            high_sev_unacked = conn.execute(
                select(func.count()).where(
                    and_(
                        anomalies.c.acknowledged == False,  # noqa: E712
                        anomalies.c.severity == "high",
                    )
                )
            ).scalar() or 0

        meta["total_anomalies_30d"] = total_q
        meta["unacknowledged_past_48h"] = unacked_old_q
        meta["high_severity_unacknowledged"] = high_sev_unacked

        if total_q == 0:
            raw = 80.0
            findings.append("No anomalies detected in the last 30 days")
            actions.append("Configure anomaly detection thresholds if no alerts are firing")
        else:
            overdue_rate = unacked_old_q / total_q * 100
            raw = _clamp(100 - overdue_rate * 1.5)

            if high_sev_unacked > 0:
                raw = max(0, raw - high_sev_unacked * 10)  # extra penalty for high severity
                findings.append(
                    f"{high_sev_unacked} HIGH severity anomaly/anomalies "
                    f"unacknowledged — review immediately"
                )
                actions.append(
                    f"Acknowledge and investigate {high_sev_unacked} high severity anomaly/anomalies"
                )

            if unacked_old_q > 0:
                findings.append(
                    f"{unacked_old_q} anomaly/anomalies not acknowledged within {response_window_hours}h"
                )
                actions.append(
                    f"Acknowledge {unacked_old_q} overdue anomaly/anomalies — "
                    f"use `get_anomalies` then `acknowledge_anomaly`"
                )
            else:
                findings.append(
                    f"Good response rate — all {total_q} anomalies acknowledged within {response_window_hours}h"
                )

    except Exception as e:
        log.debug("Could not query anomaly response data: %s", e)
        raw = 50.0
        findings.append("Anomaly response data unavailable")
        actions.append("Ensure nable has run at least one snapshot to track anomalies")
        meta["error"] = str(e)

    if not actions:
        actions.append("Set up Slack/Teams alerts so anomalies are caught immediately")

    return DimensionScore(
        name="anomaly_response", display_name="Anomaly Response",
        raw_score=raw, weight=WEIGHTS["anomaly_response"],
        weighted_score=raw * WEIGHTS["anomaly_response"] / 100,
        grade=_grade(raw), findings=findings, actions=actions, metadata=meta,
    )


# ── Trend tracking ────────────────────────────────────────────────────────────

def _get_score_trend(scope: str, current_score: float) -> tuple[str, float]:
    """
    Compare current score against the score from 7 days ago.
    Returns (trend_label, delta_pts).
    """
    try:
        from ..storage.db import get_engine
        from sqlalchemy import text

        engine = get_engine()
        week_ago = (date.today() - timedelta(days=7)).isoformat()

        with engine.connect() as conn:
            # Create table if it doesn't exist
            conn.execute(text("""
                CREATE TABLE IF NOT EXISTS scorecard_history (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    scope TEXT NOT NULL,
                    score_date TEXT NOT NULL,
                    total_score REAL NOT NULL,
                    grade TEXT NOT NULL,
                    details TEXT,
                    captured_at TEXT NOT NULL
                )
            """))
            conn.commit()

            row = conn.execute(text("""
                SELECT total_score FROM scorecard_history
                WHERE scope = :scope AND score_date <= :cutoff
                ORDER BY score_date DESC LIMIT 1
            """), {"scope": scope, "cutoff": week_ago}).fetchone()

        if not row:
            return "no_history", 0.0

        prev_score = row[0]
        delta = current_score - prev_score

        if abs(delta) < 2:
            return "stable", round(delta, 1)
        return ("improving" if delta > 0 else "declining"), round(delta, 1)

    except Exception:
        return "no_history", 0.0


def _persist_score(scope: str, score: float, grade: str, details: dict) -> None:
    """Save today's score for trend tracking."""
    try:
        from ..storage.db import get_engine
        from sqlalchemy import text

        engine = get_engine()
        today = date.today().isoformat()
        now   = datetime.now(timezone.utc).isoformat()

        with engine.connect() as conn:
            conn.execute(text("""
                CREATE TABLE IF NOT EXISTS scorecard_history (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    scope TEXT NOT NULL,
                    score_date TEXT NOT NULL,
                    total_score REAL NOT NULL,
                    grade TEXT NOT NULL,
                    details TEXT,
                    captured_at TEXT NOT NULL
                )
            """))
            # Upsert today's score
            conn.execute(text("""
                DELETE FROM scorecard_history
                WHERE scope = :scope AND score_date = :today
            """), {"scope": scope, "today": today})
            conn.execute(text("""
                INSERT INTO scorecard_history (scope, score_date, total_score, grade, details, captured_at)
                VALUES (:scope, :today, :score, :grade, :details, :now)
            """), {
                "scope": scope, "today": today,
                "score": round(score, 1), "grade": grade,
                "details": json.dumps(details), "now": now,
            })
            conn.commit()
    except Exception as e:
        log.debug("Could not persist scorecard: %s", e)


# ── Main builder ──────────────────────────────────────────────────────────────

def build_scorecard(
    scope: str = "overall",
    label: str = "Overall",

    # Compute efficiency inputs
    k8s_reports: list | None = None,
    ec2_rightsizing: list | None = None,

    # Waste inputs
    idle_resources: list[dict] | None = None,
    orphaned_helm_releases: list | None = None,

    # Commitment inputs — pass pre-fetched data OR let build_scorecard fetch it
    commitment_data: dict | None = None,

    # Tag hygiene inputs
    tag_coverage: dict[str, float] | None = None,
    required_tags: list[str] | None = None,
    untagged_spend_usd: float = 0.0,

    # Financial context
    total_monthly_spend: float = 0.0,

    # Anomaly response (always read from DB)
    anomaly_lookback_days: int = 30,

    # Tag scope — filters commitment coverage and on-demand queries by this tag
    # e.g. {"team": "platform"} or {"env": "prod"}
    tag_filter: dict | None = None,
) -> Scorecard:

    # Score each dimension
    compute  = _score_compute_efficiency(k8s_reports, ec2_rightsizing)
    waste    = _score_waste_reduction(idle_resources, k8s_reports, orphaned_helm_releases, total_monthly_spend)
    commits  = _score_commitment_coverage(commitment_data, tag_filter=tag_filter)
    tags     = _score_tag_hygiene(tag_coverage, required_tags, untagged_spend_usd, total_monthly_spend)
    anomaly  = _score_anomaly_response(anomaly_lookback_days)

    dimensions = [compute, waste, commits, tags, anomaly]
    total = sum(d.weighted_score for d in dimensions)
    grade = _grade(total)

    # Trend
    trend, delta = _get_score_trend(scope, total)

    # Potential savings. Both keys are read across a dimension boundary, so both
    # go through _require_published: a .get() default here quietly turns a
    # producer that forgot to publish into a $0 component, which is exactly how
    # the commitment opportunity vanished from this total. A missing key now says
    # so instead of costing the customer the sentence.
    potential = (
        _require_published(waste, "total_waste_usd")
        + _require_published(commits, "potential_savings_usd") * 0.7
    )

    # Top wins — pick the 3 lowest-scoring dimensions' top action
    sorted_dims = sorted(dimensions, key=lambda d: d.raw_score)
    top_wins = []
    for d in sorted_dims:
        if d.actions:
            top_wins.append(d.actions[0])
        if len(top_wins) == 3:
            break

    # Summary sentence
    trend_str = {
        "improving": f"↑ {abs(delta):.0f}pts vs last week",
        "declining": f"↓ {abs(delta):.0f}pts vs last week",
        "stable":    "→ stable vs last week",
        "no_history": "first score recorded",
    }[trend]

    lowest_dim = sorted_dims[0]
    summary = (
        f"{label}: {grade} ({total:.0f}/100)  {trend_str}. "
        f"Biggest gap: {lowest_dim.display_name} ({lowest_dim.raw_score:.0f}/100). "
        f"Estimated ${potential:,.0f}/month recoverable."
    )

    scorecard = Scorecard(
        scope=scope,
        label=label,
        generated_at=datetime.now(timezone.utc).isoformat(),
        total_score=round(total, 1),
        grade=grade,
        trend=trend,
        trend_delta=delta,
        dimensions=dimensions,
        monthly_spend_usd=total_monthly_spend,
        potential_savings_usd=round(potential, 2),
        summary=summary,
        top_wins=top_wins,
    )

    # Persist for future trend tracking
    _persist_score(scope, total, grade, {
        d.name: round(d.raw_score, 1) for d in dimensions
    })

    return scorecard
