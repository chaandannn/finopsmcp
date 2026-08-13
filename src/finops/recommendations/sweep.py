# SPDX-License-Identifier: Apache-2.0
"""One cost sweep, shared by every tool that reports savings.

Why this module exists, stated plainly: the sweep that runs 21 scanners and
normalises their output into findings existed three times. run_full_cost_audit
had the maintained copy. export_cost_report_csv and publish_cost_report_to_notion
had forks of an older version, and the forks had drifted:

  - Both imported `scan_spot_adoption_opportunities`, a name that has never
    existed in recommendations.spot_adoption. The import sits above the scanner
    dispatch, so BOTH TOOLS RAISED ImportError on every single invocation. Not
    degraded, not partial: dead, for as long as the fork has existed.
  - Both called the spot scanner as `f(aws_client=aws, regions=regions)`, but
    the real function takes only `regions`. Fixing the name alone would have
    swapped ImportError for TypeError.
  - Both gathered the scanners as bare coroutines on the running event loop, so
    they ran back to back and blocked it. The audit moved to threads with a
    deadline; the forks never got that.
  - Neither attached `resource_id`, so neither could collapse mutually exclusive
    fixes on one resource. The 141-156% overstatement that was fixed in the audit
    was still live in both exports.

Four defects, one cause: the same logic written down three times. So it is
written down once here, and the three tools call it. A fourth caller cannot
drift because there is nothing left to drift from.

Nothing in this module talks to finops.server, so it is importable from any tool
module without the import cycle that shape usually brings.
"""
from __future__ import annotations

import asyncio
import logging
import os
from dataclasses import dataclass, field
from typing import Any

log = logging.getLogger(__name__)

# How long the whole sweep may take before we return what we have. One stuck
# region or throttled API must not hang a user's request for minutes.
DEFAULT_DEADLINE_S = 90


@dataclass
class SweepResult:
    """What every caller gets: findings, and an honest account of what failed."""

    findings: list[dict] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)      # scanners that returned nothing
    learned_note: str | None = None
    timed_out: bool = False


def _call(name, fn, **kwargs):
    try:
        res = asyncio.run(fn(**kwargs)) if asyncio.iscoroutinefunction(fn) else fn(**kwargs)
        return name, res
    except Exception as exc:
        log.warning("audit scanner %s failed: %s", name, exc)
        return name, None


def build_specs(aws: Any, regions: list[str] | None) -> list[tuple[str, Any, dict]]:
    """The scanner table: (name, callable, kwargs). Imports are local so a tool
    that never sweeps does not pay for 21 modules at import time."""
    from .graviton import scan_graviton_opportunities
    from .public_ipv4 import audit_public_ipv4
    from .lambda_concurrency import scan_lambda_concurrency_waste as _lc
    from .s3_bucket_keys import scan_s3_bucket_key_opportunities as _s3bk
    from .nonprod_scheduler import identify_nonprod_resources
    from .rds_snapshots import audit_rds_manual_snapshots as _rds_snap
    from .spot_adoption import recommend_spot_adoption as _spot
    from .cloudwatch_cardinality import audit_cloudwatch_metric_cardinality as _cw_card
    from .cloudwatch_alarms import audit_cloudwatch_orphaned_alarms as _cw_alarms
    from .cloudwatch_logs_ia import audit_cloudwatch_logs_ia_opportunities as _cw_logs
    from .lambda_snapstart import recommend_lambda_snapstart as _snapstart
    from .nlb_cross_zone import audit_nlb_cross_zone_costs as _nlb
    from .s3_intelligent_tiering import audit_s3_intelligent_tiering as _s3it
    from .s3_transfer_acceleration import audit_s3_transfer_acceleration as _s3ta
    from .ebs_snapshot_replication import audit_ebs_snapshot_replication as _ebs_rep
    from .database_savings_plans import recommend_database_savings_plans as _dbsp
    from .textract_env import scan_textract_environment_waste as _textract
    from .bedrock_routing import recommend_bedrock_model_routing as _bedrock
    from .commitments import analyze_commitments as _commitments
    from ..cleanup.idle import scan_idle_resources as _idle_resources
    from ..analyzers.waste import scan_all_regions_rds_idle as _scan_all_regions_rds_idle

    return [
        ("graviton",       scan_graviton_opportunities, dict(aws_client=aws, regions=regions)),
        ("ipv4",           audit_public_ipv4,           dict(aws_client=aws, regions=regions)),
        ("lambda_pc",      _lc,                         dict(aws_client=aws, regions=regions)),
        ("s3_bucket_keys", _s3bk,                       dict(aws_client=aws)),
        ("nonprod",        identify_nonprod_resources,  dict(aws_client=aws, regions=regions)),
        ("rds_snapshots",  _rds_snap,                   dict(aws_client=aws, regions=regions)),
        ("spot",           _spot,                       dict(regions=regions)),
        ("cw_cardinality", _cw_card,                    dict(aws_client=aws, regions=regions)),
        ("cw_alarms",      _cw_alarms,                  dict(aws_client=aws, regions=regions)),
        ("cw_logs_ia",     _cw_logs,                    dict(aws_client=aws, regions=regions)),
        ("snapstart",      _snapstart,                  dict(aws_client=aws, regions=regions)),
        ("nlb",            _nlb,                         dict(aws_client=aws, regions=regions)),
        ("s3_it",          _s3it,                       dict(aws_client=aws)),
        ("s3_ta",          _s3ta,                       dict(aws_client=aws)),
        ("ebs_rep",        _ebs_rep,                    dict(aws_client=aws, regions=regions)),
        ("db_sp",          _dbsp,                       dict()),
        ("textract",       _textract,                   dict()),
        ("bedrock",        _bedrock,                    dict()),
        ("commitments",    _commitments,                dict()),
        ("idle_resources", _idle_resources,             dict(regions=regions)),
        ("idle_rds",       _scan_all_regions_rds_idle,  dict(regions=regions)),
    ]


def normalise(name, data) -> list[dict]:
    if data is None:
        return []
    out = []
    try:
        if name == "graviton" and isinstance(data, list):
            for r in data:
                s = r.get("savings_estimate", 0) or 0
                if s > 0:
                    # current cost rides along so the critique can hold the
                    # claim against it (a saving cannot exceed the resource).
                    out.append({"title": f"Migrate {r.get('instance_id','?')} ({r.get('instance_type','?')} → {r.get('graviton_equivalent','?')})", "monthly_savings": s, "category": "Compute", "detail": f"{r.get('savings_pct',0)*100:.0f}% saving, {r.get('region','')}", "resource_id": r.get("instance_id", ""), "region": r.get("region", ""), "current_monthly_cost_usd": r.get("current_monthly_cost_estimate")})
        elif name == "ipv4":
            waste = data.get("total_monthly_waste", 0) or 0
            if waste > 0:
                n_unattached = len(data.get("unattached_eips", []))
                out.append({"title": f"Release {n_unattached} unattached Elastic IP(s)", "monthly_savings": waste, "category": "Network", "detail": f"${waste:.2f}/mo, $3.60 per IP"})
        elif name == "lambda_pc" and isinstance(data, list):
            for r in data:
                s = r.get("wasted_monthly_cost", 0) or 0
                if s > 0:
                    out.append({"title": f"Reduce provisioned concurrency on {r.get('function_name','?')}", "monthly_savings": s, "category": "Compute", "resource_id": r.get("function_name", ""), "detail": f"{r.get('avg_utilization_pct',0)*100:.0f}% utilization"})
        elif name == "s3_bucket_keys" and isinstance(data, list):
            for r in data:
                s = r.get("estimated_savings", 0) or 0
                if s > 0:
                    out.append({"title": f"Enable S3 Bucket Key on {r.get('bucket_name','?')}", "monthly_savings": s, "category": "Storage", "resource_id": r.get("bucket_name", ""), "detail": "Up to 99% KMS cost reduction"})
        elif name == "nonprod":
            items = data.get("schedulable_instances", []) if isinstance(data, dict) else []
            for r in items:
                s = r.get("potential_monthly_savings", 0) or 0
                if s > 0:
                    out.append({"title": f"Schedule non-prod instance {r.get('name', r.get('instance_id','?'))}", "monthly_savings": s, "category": "Compute", "resource_id": r.get("instance_id", ""), "detail": f"env={r.get('environment','?')}, {r.get('idle_hours_per_week',0):.0f} idle hrs/wk"})
        elif name == "rds_snapshots":
            items = data.get("orphaned_snapshots", []) + data.get("old_snapshots", []) if isinstance(data, dict) else []
            total = data.get("potential_monthly_savings", 0) if isinstance(data, dict) else 0
            if total > 0:
                out.append({"title": f"Delete {len(items)} old/orphaned RDS manual snapshots", "monthly_savings": total, "category": "Storage", "detail": f"${total:.2f}/mo at $0.095/GB-month"})
        elif name == "spot" and isinstance(data, list):
            for r in data:
                s = r.get("monthly_savings", 0) or 0
                if s > 0 and r.get("recommendation") == "RECOMMENDED":
                    out.append({"title": f"Convert {r.get('instance_id','?')} ({r.get('instance_type','?')}) to Spot", "monthly_savings": s, "category": "Compute", "resource_id": r.get("instance_id", ""), "detail": f"{r.get('savings_pct',0)*100:.0f}% saving"})
        elif name == "cw_cardinality" and isinstance(data, list):
            for r in data:
                s = r.get("estimated_monthly_cost", 0) or 0
                if s > 0:
                    out.append({"title": f"Reduce CloudWatch metric cardinality in {r.get('namespace','?')}", "monthly_savings": s, "category": "Observability", "detail": f"{r.get('metric_count',0)} metrics"})
        elif name == "cw_alarms":
            items = data.get("orphaned_alarms", []) if isinstance(data, dict) else (data if isinstance(data, list) else [])
            total = sum(r.get("monthly_cost", 0) for r in items)
            if total > 0:
                out.append({"title": f"Delete {len(items)} orphaned CloudWatch alarm(s)", "monthly_savings": total, "category": "Observability", "detail": f"${total:.2f}/mo"})
        elif name == "cw_logs_ia" and isinstance(data, list):
            total = sum(r.get("monthly_savings", 0) for r in data)
            if total > 0:
                out.append({"title": f"Move {len(data)} log group(s) to Infrequent Access", "monthly_savings": total, "category": "Observability", "detail": "50% ingestion cost reduction"})
        elif name == "snapstart" and isinstance(data, list):
            total = sum(r.get("monthly_pc_cost", 0) for r in data if r.get("recommendation") == "ENABLE_SNAPSTART_REPLACE_PC")
            if total > 0:
                out.append({"title": f"Enable Lambda SnapStart on {len([r for r in data if r.get('recommendation')=='ENABLE_SNAPSTART_REPLACE_PC'])} Java function(s)", "monthly_savings": total, "category": "Compute", "detail": "Replaces provisioned concurrency for free"})
        elif name == "nlb" and isinstance(data, list):
            for r in data:
                s = r.get("estimated_cross_az_cost", 0) or 0
                if s > 10:
                    out.append({"title": f"Disable cross-zone on NLB {r.get('nlb_name','?')}", "monthly_savings": s, "category": "Network", "resource_id": r.get("nlb_name", ""), "detail": f"${s:.2f}/mo cross-AZ charges"})
        elif name == "s3_it" and isinstance(data, list):
            waste = [r for r in data if isinstance(r.get("recommendation"), str) and r["recommendation"].startswith("LIKELY_WASTE")]
            total = sum((r.get("net_monthly_cost") or 0) for r in waste)
            if total > 0:
                out.append({"title": f"Disable S3 Intelligent-Tiering on {len(waste)} bucket(s) with small objects", "monthly_savings": total, "category": "Storage", "detail": "Monitoring fee exceeds tiering savings"})
        elif name == "s3_ta":
            items = data.get("findings", data) if isinstance(data, dict) else (data if isinstance(data, list) else [])
            waste = [r for r in items if r.get("likely_waste")]
            total = sum(r.get("monthly_ta_cost", 0) for r in waste)
            if total > 0:
                out.append({"title": f"Disable S3 Transfer Acceleration on {len(waste)} bucket(s)", "monthly_savings": total, "category": "Storage", "detail": f"${total:.2f}/mo surcharge"})
        elif name == "ebs_rep":
            total = data.get("potential_monthly_savings", 0) if isinstance(data, dict) else 0
            n = len(data.get("excess_copies", [])) if isinstance(data, dict) else 0
            if total > 0:
                out.append({"title": f"Clean up {n} excess EBS cross-region snapshot copies", "monthly_savings": total, "category": "Storage", "detail": f"${total:.2f}/mo"})
        elif name == "db_sp":
            s = data.get("estimated_monthly_savings", 0) if isinstance(data, dict) else 0
            if s > 0:
                out.append({"title": "Purchase Database Savings Plan for RDS/Aurora", "monthly_savings": s, "category": "Commitments", "detail": f"Up to 35% off, ${s:.2f}/mo saving"})
        elif name == "textract":
            waste = data.get("estimated_monthly_waste", 0) if isinstance(data, dict) else 0
            callers = data.get("non_prod_callers", []) if isinstance(data, dict) else []
            if waste > 0:
                out.append({"title": f"Disable Textract in non-prod ({len(callers)} caller(s))", "monthly_savings": waste, "category": "AI/ML", "detail": f"${waste:.2f}/mo from QA/staging environments"})
        elif name == "bedrock":
            opps = data.get("routing_opportunities", []) if isinstance(data, dict) else []
            total = data.get("total_monthly_savings", 0) if isinstance(data, dict) else 0
            if total > 0:
                models = [o.get("current_model", "?") for o in opps[:2]]
                out.append({"title": f"Route Bedrock tasks to cheaper models ({', '.join(models)})", "monthly_savings": total, "category": "AI/ML", "detail": f"Short tasks to Haiku, ${total:.2f}/mo saving"})
        elif name == "commitments":
            s = data.get("estimated_monthly_savings", 0) if isinstance(data, dict) else 0
            coverage = data.get("current_coverage_pct", 0) if isinstance(data, dict) else 0
            if s > 0 and coverage < 80:
                out.append({"title": f"Purchase Savings Plans / Reserved Instances ({coverage:.0f}% covered)", "monthly_savings": s, "category": "Commitments", "detail": f"${s:.2f}/mo saving at current spend"})
        elif name == "idle_resources" and isinstance(data, list):
            for r in data:
                if getattr(r, "protected", False) or r.monthly_cost_usd <= 0:
                    continue
                # savings == the resource's own cost by construction (delete
                # it, stop paying it); carrying the cost makes that invariant
                # checkable by the critique instead of assumed.
                out.append({"title": f"{r.resource_type.replace('_', ' ').title()}: {r.name or r.resource_id}", "monthly_savings": r.monthly_cost_usd, "category": "Idle/Orphaned", "detail": f"{r.reason}, idle {r.idle_days}d, {r.region}", "resource_id": r.resource_id, "region": r.region, "current_monthly_cost_usd": r.monthly_cost_usd})
        elif name == "idle_rds" and isinstance(data, list):
            for r in data:
                s = r.get("estimated_monthly_savings", 0) or 0
                if s > 0:
                    # lookback_days matches check_rds_idle's connection window;
                    # cost equals the saving by construction (stopped == unpaid).
                    out.append({"title": f"Stop or delete idle RDS instance {r.get('resource_id','?')}", "monthly_savings": s, "category": "Database", "detail": f"{r.get('engine','?')} {r.get('current_class','?')}, {r.get('region','?')}, no connections in 14d", "resource_id": r.get("resource_id", ""), "region": r.get("region", ""), "current_monthly_cost_usd": s, "lookback_days": 14})
    except Exception as exc:
        log.warning("audit norm failed for %s: %s", name, exc)
    return out


# Map each scanner to the ledger `source` the learning loop keys on, so the
# sweep can rank by what THIS account actually acts on, not just raw dollars.
# A scanner with no ledger source (or a cold one) simply keeps dollar order.
SOURCE_MAP = {
    "graviton": "graviton", "idle_resources": "idle", "commitments": "commitment",
    "spot": "spot", "db_sp": "commitment",
}


def collapse_per_resource(findings: list[dict]) -> tuple[list[dict], int]:
    """Keep one finding per resource: the largest saving, alternatives attached.

    Findings with no resource_id are aggregates ("delete 12 orphaned alarms") and
    cannot double count a single resource, so they pass through untouched.

    Returns (kept, n_collapsed). n_collapsed is how many claims were folded away,
    which the caller reports rather than silently dropping: a total that quietly
    shrank is as hard to trust as one that was quietly inflated.
    """
    best: dict[str, dict] = {}
    passthrough: list[dict] = []
    order: list[str] = []

    for f in findings:
        rid = (f.get("resource_id") or "").strip()
        if not rid:
            passthrough.append(f)
            continue
        cur = best.get(rid)
        if cur is None:
            best[rid] = dict(f)
            order.append(rid)
            continue
        loser, winner = (cur, f) if (f.get("monthly_savings") or 0) > (cur.get("monthly_savings") or 0) else (f, cur)
        merged = dict(winner)
        alts = list(merged.get("alternatives") or []) + list(loser.get("alternatives") or [])
        alts.append({"title": loser.get("title", ""),
                     "monthly_savings": loser.get("monthly_savings")})
        merged["alternatives"] = alts
        best[rid] = merged

    collapsed = sum(len(b.get("alternatives") or []) for b in best.values())

    # Order is not ours to choose. By the time findings reach here the learning
    # rescorer has already ranked them by what this customer's ledger says gets
    # acted on and turns out accurate, which is worth more than sorting by the
    # biggest claim. Re-sorting on monthly_savings threw that away and put the
    # suppressed source back on top. So: rebuild in the incoming order, with each
    # resource represented once, at the position its best-ranked finding held.
    seen: set[str] = set()
    kept: list[dict] = []
    for f in findings:
        rid = (f.get("resource_id") or "").strip()
        if not rid:
            kept.append(f)
            continue
        if rid in seen:
            continue
        seen.add(rid)
        kept.append(best[rid])
    return kept, collapsed


async def sweep(
    aws: Any,
    regions: list[str] | None = None,
    *,
    deadline_s: int | None = None,
) -> SweepResult:
    """Run every scanner, normalise, critique, and rank. Never raises.

    Each scanner makes blocking boto3 calls. Gathered as bare coroutines they
    share one event loop and run back-to-back, so the sweep would take the SUM of
    every scanner's time. Each runs in its own thread instead, so the sweep is
    bounded by the SLOWEST scanner, not their sum (measured ~5x on a real
    account). fn may be sync or async; an async one runs on a fresh loop in its
    thread, which is safe because no scanner shares a main-loop asyncio primitive
    (the cost cache uses a threading.Lock).

    The result is NOT collapsed. Callers slice to their own top-N first, then
    call collapse_per_resource, because collapsing before slicing would let a
    resource's alternatives push real findings off the end of the list.
    """
    specs = build_specs(aws, regions)
    if deadline_s is None:
        deadline_s = int(os.getenv("FINOPS_AUDIT_TIMEOUT", str(DEFAULT_DEADLINE_S)))

    try:
        results = await asyncio.wait_for(
            asyncio.gather(*[asyncio.to_thread(_call, n, fn, **kw) for n, fn, kw in specs]),
            timeout=deadline_s,
        )
    except asyncio.TimeoutError:
        log.warning("cost sweep hit the %ss deadline; returning early", deadline_s)
        return SweepResult(timed_out=True)

    out = SweepResult()
    for name, data in results:
        if data is None:
            out.errors.append(name)
            continue
        for f in normalise(name, data):
            f["source"] = SOURCE_MAP.get(name, name)
            out.findings.append(f)

    # Sort by monthly savings descending first (the stable base order).
    out.findings.sort(key=lambda x: x.get("monthly_savings", 0), reverse=True)

    # Critique first, then rank. A finding whose claim was just retracted must not
    # be ranked on the dollar figure it lost, so this has to run BEFORE rescore.
    # Deterministic falsifiers only here (no network, no LLM unless opted in), so
    # a free sweep stays free. Never drops a finding: the worst case is a
    # downgrade to an investigation with a magnitude band instead of a figure.
    try:
        from .critique import critique
        out.findings = critique(out.findings, savings_key="monthly_savings")
    except Exception as exc:
        log.debug("critique skipped in sweep: %s", exc)

    # Learning loop: reorder by a learned score (savings x this account's
    # confidence in the source). Propose-only: nothing is hidden and spend numbers
    # are untouched; a cold ledger leaves the dollar order intact. Suppressed
    # sources sink to the bottom rather than being removed.
    try:
        from .learning import customer_signal, rescore
        sig = customer_signal()
        rs = rescore(out.findings, sig, savings_key="monthly_savings", source_key="source")
        out.findings = rs["ranked"] + rs["suppressed_for_you"]
        if any(s.get("coverage") != "COLD" for s in sig.get("by_source", [])):
            out.learned_note = (
                "Ranked using your ledger (act-rate and accuracy per source), "
                "propose-only. Call get_recommendation_learning() for the why."
            )
    except Exception as exc:
        log.debug("learning rescore skipped in sweep: %s", exc)

    return out
