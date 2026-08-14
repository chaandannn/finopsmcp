"""
AWS Deep Audit Orchestrator.

run_deep_audit() is the main entry point. It:
1. Gets a boto3 session using the same credential chain as AWSConnector
2. Discovers regions (or uses the provided list)
3. Runs all waste checks in parallel across regions
4. Optionally fetches AWS Compute Optimizer recommendations
5. Merges, deduplicates, and sorts findings by estimated_monthly_savings
6. Returns a structured report

Usage:
    from finops.analyzers.optimizer import run_deep_audit
    report = run_deep_audit(regions=["us-east-1"], checks=["ebs", "lambda"])
"""
from __future__ import annotations

import hashlib
import logging
import os
import time
from collections.abc import Callable
from concurrent.futures import TimeoutError as _FuturesTimeout
from concurrent.futures import ThreadPoolExecutor, as_completed

log = logging.getLogger(__name__)

# ── All known check keys ──────────────────────────────────────────────────────

_ALL_CHECKS = frozenset([
    "ebs",           # unattached volumes + gp2 to gp3
    "snapshots",     # old EBS snapshots
    "eips",          # unassociated Elastic IPs
    "nat",           # idle NAT Gateways
    "rds",           # excessive RDS backup retention
    "rds_rightsizing",  # oversized RDS instances (low CPU)
    "rds_idle",      # RDS with zero connections
    "cloudtrail",    # CloudTrail waste (data events, duplicate trails)
    "cloudwatch",    # CloudWatch Log Groups without retention
    "s3",            # S3 suboptimal storage class
    "s3_multipart",  # incomplete S3 multipart uploads
    "lambda",        # Lambda memory over-provisioning
    "ec2",           # idle EC2 instances
    "load_balancer", # idle ALBs, NLBs, Classic ELBs
    "ecr",           # old untagged ECR images
    "ecs",           # ECS Fargate over-provisioned CPU
])

_DEFAULT_REGIONS = ["us-east-1"]


# ── Boto3 session factory ─────────────────────────────────────────────────────

def _get_boto3_session(role_arn: str | None = None):
    """
    Return a boto3 Session using the same credential chain as AWSConnector:
    - If role_arn is provided, assume that role
    - Otherwise fall through boto3's default chain
      (env vars → ~/.aws/credentials → instance profile)
    """
    import boto3

    if role_arn:
        sts = boto3.client("sts")
        assumed = sts.assume_role(
            RoleArn=role_arn,
            RoleSessionName="finops-mcp-deep-audit",
        )
        creds = assumed["Credentials"]
        return boto3.Session(
            aws_access_key_id=creds["AccessKeyId"],
            aws_secret_access_key=creds["SecretAccessKey"],
            aws_session_token=creds["SessionToken"],
        )
    return boto3.Session()


def _discover_regions(session) -> list[str]:
    """List all opted-in EC2 regions for this account."""
    try:
        ec2 = session.client("ec2", region_name="us-east-1")
        resp = ec2.describe_regions(Filters=[{"Name": "opt-in-status", "Values": ["opt-in-not-required", "opted-in"]}])
        return [r["RegionName"] for r in resp.get("Regions", [])]
    except Exception as exc:
        log.warning("Could not discover regions: %s — defaulting to us-east-1", exc)
        return _DEFAULT_REGIONS


# ── Per-region audit runner ───────────────────────────────────────────────────

def _audit_region(
    session,
    region: str,
    checks: frozenset[str],
) -> list[dict]:
    """Run all requested checks in a single region. Returns findings list."""
    from .waste import (
        check_ebs_volumes,
        check_ebs_snapshots,
        check_elastic_ips,
        check_nat_gateways,
        check_rds_backups,
        check_rds_rightsizing,
        check_rds_idle,
        check_cloudtrail_waste,
        check_cloudwatch_logs,
        check_s3_storage_class,
        check_s3_incomplete_multipart,
        check_lambda_memory,
        check_idle_ec2,
        check_idle_load_balancers,
        check_ecr_old_images,
        check_ecs_task_rightsizing,
    )

    findings: list[dict] = []

    # Build clients lazily (some regions may not have all services)
    def _client(service: str):
        return session.client(service, region_name=region)

    ec2_client = _client("ec2") if checks & {"ebs", "snapshots", "eips", "nat", "ec2"} else None
    cw_client = _client("cloudwatch") if checks & {
        "nat", "s3", "lambda", "ec2", "cloudwatch",
        "rds_rightsizing", "rds_idle", "load_balancer", "ecs",
    } else None
    rds_client = _client("rds") if checks & {"rds", "rds_rightsizing", "rds_idle"} else None
    lambda_client = _client("lambda") if "lambda" in checks else None
    logs_client = _client("logs") if "cloudwatch" in checks else None
    s3_client = _client("s3") if checks & {"s3", "s3_multipart"} else None
    ct_client = _client("cloudtrail") if "cloudtrail" in checks else None
    elbv2_client = _client("elbv2") if "load_balancer" in checks else None
    elb_client = _client("elb") if "load_balancer" in checks else None
    ecr_client = _client("ecr") if "ecr" in checks else None
    ecs_client = _client("ecs") if "ecs" in checks else None

    def _run(name: str, fn, *args):
        try:
            result = fn(*args)
            for finding in result:
                finding.setdefault("region", region)
            findings.extend(result)
        except Exception as exc:
            log.warning("Check '%s' failed in %s: %s", name, region, exc)

    if "ebs" in checks and ec2_client:
        _run("ebs", check_ebs_volumes, ec2_client, region)

    if "snapshots" in checks and ec2_client:
        _run("snapshots", check_ebs_snapshots, ec2_client, region)

    if "eips" in checks and ec2_client:
        _run("eips", check_elastic_ips, ec2_client, region)

    if "nat" in checks and ec2_client and cw_client:
        _run("nat", check_nat_gateways, ec2_client, cw_client, region)

    if "rds" in checks and rds_client:
        _run("rds", check_rds_backups, rds_client, region)

    if "rds_rightsizing" in checks and rds_client and cw_client:
        _run("rds_rightsizing", check_rds_rightsizing, rds_client, cw_client, region)

    if "rds_idle" in checks and rds_client and cw_client:
        _run("rds_idle", check_rds_idle, rds_client, cw_client, region)

    if "cloudtrail" in checks and ct_client:
        _run("cloudtrail", check_cloudtrail_waste, ct_client, region)

    if "cloudwatch" in checks and logs_client:
        _run("cloudwatch", check_cloudwatch_logs, logs_client, region)

    if "s3" in checks and s3_client and cw_client:
        # S3 is global — only run from us-east-1 to avoid duplicate findings
        if region == "us-east-1":
            _run("s3", check_s3_storage_class, s3_client, cw_client, region)

    if "s3_multipart" in checks and s3_client:
        if region == "us-east-1":
            _run("s3_multipart", check_s3_incomplete_multipart, s3_client, region)

    if "load_balancer" in checks and elbv2_client and elb_client and cw_client:
        _run("load_balancer", check_idle_load_balancers, elbv2_client, elb_client, cw_client, region)

    if "ecr" in checks and ecr_client:
        _run("ecr", check_ecr_old_images, ecr_client, region)

    if "ecs" in checks and ecs_client and cw_client:
        _run("ecs", check_ecs_task_rightsizing, ecs_client, cw_client, region)

    if "lambda" in checks and lambda_client and cw_client:
        _run("lambda", check_lambda_memory, lambda_client, cw_client, region)

    if "ec2" in checks and ec2_client and cw_client:
        _run("ec2", check_idle_ec2, ec2_client, cw_client, region)

    return findings


# ── Compute Optimizer integration ─────────────────────────────────────────────

def _fetch_compute_optimizer_recommendations(session) -> list[dict]:
    """
    Pull AWS Compute Optimizer recommendations and convert to our finding format.
    Wrapped in try/except — Compute Optimizer is opt-in and may not be enabled.
    """
    findings: list[dict] = []

    # Hoisted above the per-service try blocks on purpose. Inside one of them, a
    # failure here would surface as a NameError in the NEXT block and be swallowed
    # by that block's own except, which is precisely the silent-death pattern this
    # commit removes.
    from ..recommendations.rightsizing import (
        CO_EC2_OVERPROVISIONED, CO_RDS_OVERPROVISIONED,
        _co_monthly_savings, _co_pages,
    )

    try:
        co = session.client("compute-optimizer", region_name="us-east-1")

        # EC2 instance recommendations
        try:
            # get_ec2_instance_recommendations has no botocore paginator, so
            # get_paginator raised OperationNotPageableError straight into the
            # except below, at DEBUG. This whole block has never produced a
            # finding, while waste_evidence rates compute_optimizer_* MEASURED
            # and high.
            for page in _co_pages(co, "get_ec2_instance_recommendations"):
                for rec in page.get("instanceRecommendations", []):
                    # The API enum is Underprovisioned | Overprovisioned |
                    # Optimized | NotOptimized. "OVER_PROVISIONED" is not a value
                    # AWS returns, so this never matched even on a live account.
                    if rec.get("finding") == CO_EC2_OVERPROVISIONED:
                        instance_id = rec.get("instanceArn", "").split("/")[-1]
                        current_type = rec.get("currentInstanceType", "unknown")
                        options = rec.get("recommendationOptions", [])
                        if options:
                            best = options[0]
                            recommended_type = best.get("instanceType", "unknown")
                            # estimatedMonthlySavings is nested under
                            # savingsOpportunity, not on the option itself.
                            savings_pct, savings_currency = _co_monthly_savings(best)
                            findings.append({
                                "resource_id": instance_id,
                                "resource_type": "EC2 Instance",
                                "waste_type": "compute_optimizer_overprovisioned_ec2",
                                "estimated_monthly_savings": round(float(savings_pct), 2),
                                "detail": (
                                    f"AWS Compute Optimizer: {instance_id} is over-provisioned. "
                                    f"Current: {current_type} → Recommended: {recommended_type}. "
                                    f"Estimated savings: ${savings_pct:.2f}/mo ({savings_currency}). "
                                    f"Performance risk: {best.get('performanceRisk', 'unknown')}."
                                ),
                                "severity": "high" if savings_pct >= 30 else "medium",
                                "region": rec.get("instanceArn", "").split(":")[3] or "unknown",
                                "account_id": rec.get("accountId"),
                                "source": "compute_optimizer",
                                "current_instance_type": current_type,
                                "recommended_instance_type": recommended_type,
                            })
        except Exception as exc:
            log.debug("Compute Optimizer EC2 recommendations unavailable: %s", exc)

        # Lambda function recommendations
        try:
            paginator = co.get_paginator("get_lambda_function_recommendations")
            for page in paginator.paginate():
                for rec in page.get("lambdaFunctionRecommendations", []):
                    # Lambda's enum is Optimized | NotOptimized | Unavailable.
                    if rec.get("finding") == "NotOptimized":
                        fn_arn = rec.get("functionArn", "")
                        fn_name = fn_arn.split(":")[-1] if ":" in fn_arn else fn_arn
                        options = rec.get("memorySizeRecommendationOptions", [])
                        if options:
                            best = options[0]
                            savings, _cur = _co_monthly_savings(best)
                            findings.append({
                                "resource_id": fn_name,
                                "resource_type": "Lambda Function",
                                "waste_type": "compute_optimizer_overprovisioned_lambda",
                                "estimated_monthly_savings": round(float(savings), 2),
                                "detail": (
                                    f"AWS Compute Optimizer: Lambda '{fn_name}' is not optimized. "
                                    f"Current memory: {rec.get('currentMemorySize', '?')} MB → "
                                    f"Recommended: {best.get('memorySize', '?')} MB. "
                                    f"Estimated savings: ${savings:.2f}/mo."
                                ),
                                "severity": _severity_from_savings_co(savings),
                                "region": fn_arn.split(":")[3] if ":" in fn_arn else "unknown",
                                "account_id": rec.get("accountId"),
                                "source": "compute_optimizer",
                            })
        except Exception as exc:
            log.debug("Compute Optimizer Lambda recommendations unavailable: %s", exc)

        # RDS recommendations (newer API — may not be available in all accounts)
        try:
            # Four separate faults in six lines, each of which alone made this
            # block return nothing: there is no paginator for this operation; the
            # response key is rdsDBRecommendations, not recommendations; the field
            # is instanceFinding, not finding; and the savings live on a
            # recommendation OPTION under savingsOpportunity, not on the
            # recommendation itself.
            for page in _co_pages(co, "get_rds_database_recommendations"):
                for rec in page.get("rdsDBRecommendations", []):
                    if rec.get("instanceFinding") == CO_RDS_OVERPROVISIONED:
                        resource_arn = rec.get("resourceArn", "")
                        db_id = resource_arn.split(":")[-1] if ":" in resource_arn else resource_arn
                        _opts = sorted(
                            rec.get("instanceRecommendationOptions", []),
                            key=lambda opt: opt.get("rank", 99),
                        )
                        if not _opts:
                            continue
                        savings, _cur = _co_monthly_savings(_opts[0])
                        findings.append({
                            "resource_id": db_id,
                            "resource_type": "RDS Instance",
                            "waste_type": "compute_optimizer_overprovisioned_rds",
                            "estimated_monthly_savings": round(float(savings), 2),
                            "detail": (
                                f"AWS Compute Optimizer: RDS '{db_id}' is over-provisioned. "
                                f"Estimated savings: ${savings:.2f}/mo. "
                                f"Check Compute Optimizer console for instance class recommendation."
                            ),
                            "severity": _severity_from_savings_co(savings),
                            "region": resource_arn.split(":")[3] if ":" in resource_arn else "unknown",
                            "account_id": rec.get("accountId"),
                            "source": "compute_optimizer",
                        })
        except Exception as exc:
            log.debug("Compute Optimizer RDS recommendations unavailable: %s", exc)

    except Exception as exc:
        log.info("Compute Optimizer not available or not opted-in: %s", exc)

    return findings


def _severity_from_savings_co(monthly_savings: float) -> str:
    if monthly_savings >= 100:
        return "critical"
    if monthly_savings >= 30:
        return "high"
    if monthly_savings >= 10:
        return "medium"
    return "low"


# ── Deduplication ─────────────────────────────────────────────────────────────

# Waste types that all describe the SAME underlying action on one resource:
# "this compute is over-provisioned / idle, right-size or stop it". A heuristic
# CPU check and Compute Optimizer can both flag the same instance with different
# waste_type strings; collapsing them to one resource-level key stops the same
# instance's savings being counted twice in the audit total.
# Waste types that describe the SAME action on the same resource, grouped by the
# family they collapse into. This map is the only thing standing between the
# audit total and double-counting one instance, and for most of its life half of
# it addressed nobody.
#
# Measured 2026-08-14 by walking every `"waste_type": "..."` literal in the tree
# and diffing it against these keys:
#
#   lambda_over_provisioned_memory   no detector has ever emitted this. The one
#                                    in waste.py emits lambda_memory_over-
#                                    provisioned. The words are transposed.
#   idle_rds                         emitted only by cli_scan._demo_payload, the
#                                    StreamCo sample data, which returns before
#                                    the audit runs and never reaches dedup. A
#                                    demo string was load-bearing in production
#                                    dedup, or would have been if it had matched
#                                    anything real.
#
# Meanwhile the two waste types the deep audit's own RDS detectors actually emit
# were both absent, so a quiet oversized database collected "stop it" AND
# "downsize it", each priced in full, and the audit claimed about 1.5x the
# instance's entire cost as recoverable from it.
#
# A key here that matches nothing is invisible: the map is consulted with .get()
# and a miss just means "not a rightsizing finding", which is the normal case for
# most findings. Nothing can go red. tests/test_audit_cost_math.py now walks the
# same literals and fails on any key no detector emits, which is the only way
# this stays true.
_RIGHTSIZING_FAMILY: dict[str, str] = {
    "idle_ec2_low_cpu": "ec2-rightsize",
    "compute_optimizer_overprovisioned_ec2": "ec2-rightsize",
    # Both RDS detectors describe one action on one instance: this database is
    # too big for its load. Stop-it and shrink-it are alternatives, never a sum.
    "rds_idle_no_connections": "rds-rightsize",
    "rds_overprovisioned": "rds-rightsize",
    "compute_optimizer_overprovisioned_rds": "rds-rightsize",
    "compute_optimizer_overprovisioned_lambda": "lambda-rightsize",
    "lambda_memory_overprovisioned": "lambda-rightsize",
}


def _annotate_evidence(findings: list[dict]) -> list[dict]:
    """Tag each finding measured/inferred. Imported lazily to keep analyzers free of
    a module-level dependency on recommendations (which imports back this way).
    Best-effort: a classification failure must never lose a finding."""
    try:
        from .waste_evidence import annotate
        return annotate(findings)
    except Exception as exc:  # pragma: no cover - defensive
        log.warning("evidence classification skipped: %s", exc)
        return findings


def _split_evidence_totals(findings: list[dict]) -> dict:
    try:
        from .waste_evidence import split_totals
        return split_totals(findings)
    except Exception as exc:  # pragma: no cover - defensive
        log.warning("evidence totals skipped: %s", exc)
        return {}


def _dedup_findings(findings: list[dict]) -> list[dict]:
    """
    Deduplicate findings so a resource is never counted twice.

    Two levels:
      1. Exact dup: same (resource_id, waste_type) -> keep higher savings.
      2. Same-resource cross-source dup: when one resource is flagged by both a
         heuristic and Compute Optimizer for the same intent (e.g. idle_ec2_low_cpu
         AND compute_optimizer_overprovisioned_ec2), collapse to ONE finding,
         preferring the Compute Optimizer source (it weighs CPU + memory + network
         + disk), else the higher-savings one. Without this the audit total was
         inflated by double-counting the same instance.
    """
    # Level 1: exact (resource_id, waste_type)
    by_exact: dict[str, dict] = {}
    for finding in findings:
        key = hashlib.sha256(
            f"{finding.get('resource_id','')}{finding.get('waste_type','')}".encode()
        ).hexdigest()[:16]
        existing = by_exact.get(key)
        if existing is None or finding.get("estimated_monthly_savings", 0) > existing.get("estimated_monthly_savings", 0):
            by_exact[key] = finding

    # Level 2: collapse rightsizing-family findings per resource
    family_winner: dict[str, dict] = {}
    result: list[dict] = []
    for finding in by_exact.values():
        family = _RIGHTSIZING_FAMILY.get(finding.get("waste_type", ""))
        if family is None:
            result.append(finding)
            continue
        fam_key = f"{finding.get('resource_id','')}|{family}"
        current = family_winner.get(fam_key)
        if current is None:
            family_winner[fam_key] = finding
        else:
            # Prefer Compute Optimizer; otherwise the higher-savings finding.
            cur_co = current.get("source") == "compute_optimizer"
            new_co = finding.get("source") == "compute_optimizer"
            if (new_co and not cur_co) or (
                new_co == cur_co
                and finding.get("estimated_monthly_savings", 0) > current.get("estimated_monthly_savings", 0)
            ):
                family_winner[fam_key] = finding

    result.extend(family_winner.values())
    return result


# ── Main orchestrator ─────────────────────────────────────────────────────────

def run_deep_audit(
    account_id: str | None = None,
    regions: list[str] | None = None,
    checks: list[str] | None = None,
    role_arn: str | None = None,
    max_workers: int = 8,
    progress_callback: Callable[[str, int, int, int], None] | None = None,
    deadline_seconds: float | None = None,
) -> dict:
    """
    Run a full deep AWS waste audit and return a structured report.

    Args:
        account_id: Optional — used for labelling; auto-discovered if not provided.
        regions: List of region strings. None = discover all opted-in regions.
        checks: List of check keys from: ebs, snapshots, eips, nat, rds, cloudtrail,
                cloudwatch, s3, lambda, ec2. None = run all.
        role_arn: Optional IAM role ARN to assume (for cross-account audits).
                  Defaults to AWS_ROLE_ARNS env var first role if not provided.
        max_workers: Thread pool size for parallel region scanning.
        progress_callback: Optional hook fired after each region completes:
                callback(region, findings_in_region, regions_done, regions_total).
                Exceptions from the callback are swallowed; progress reporting
                must never kill a scan. None (the default, and the MCP tool
                path) keeps behavior byte-identical to before this parameter.
        deadline_seconds: Optional wall-clock budget for the region fan-out.
                When it expires, pending regions are cancelled, completed
                regions are returned as partial results, and the timed-out
                region names land in "regions_timed_out". In-flight boto3
                calls cannot be interrupted; their threads finish in the
                background and their results are discarded. None (default)
                means no deadline, exactly the prior behavior.

    Returns:
        {
            "account_id": str,
            "regions_scanned": list[str],
            "checks_run": list[str],
            "total_findings": int,
            "total_estimated_monthly_savings": float,
            "total_estimated_annual_savings": float,
            "findings": list[dict],   # sorted by estimated_monthly_savings desc
            "by_category": dict,      # waste_type → {count, total_savings}
            "by_severity": dict,      # severity → {count, total_savings}
            "by_region": dict,        # region → {count, total_savings}
            "compute_optimizer_findings": int,
            "errors": list[str],
        }
    """
    # Resolve role ARN from env if not explicitly passed
    if role_arn is None:
        role_arns_env = os.getenv("AWS_ROLE_ARNS", "")
        if role_arns_env:
            role_arn = role_arns_env.split(",")[0].strip() or None

    try:
        session = _get_boto3_session(role_arn)
    except Exception as exc:
        return {"error": f"Could not create AWS session: {exc}"}

    # Discover account ID
    if account_id is None:
        try:
            sts = session.client("sts")
            account_id = sts.get_caller_identity()["Account"]
        except Exception:
            account_id = "unknown"

    # Resolve regions
    if regions is None:
        regions = _discover_regions(session)
    elif isinstance(regions, str):
        regions = [regions]

    # Resolve checks
    if checks is None:
        active_checks = _ALL_CHECKS
    else:
        unknown = set(checks) - _ALL_CHECKS
        if unknown:
            log.warning("Unknown check keys ignored: %s", unknown)
        active_checks = frozenset(c for c in checks if c in _ALL_CHECKS)

    log.info(
        "Starting deep audit: account=%s regions=%s checks=%s",
        account_id, regions, sorted(active_checks),
    )

    all_findings: list[dict] = []
    errors: list[str] = []
    regions_timed_out: list[str] = []
    threads_abandoned = False  # True when the deadline left live boto3 threads running

    # ── Parallel region scans ────────────────────────────────────────────────
    #
    #   regions ──▶ ThreadPoolExecutor(≤8) ──▶ as_completed ──▶ findings
    #                                              │
    #                    deadline_seconds ─────────┤ (remaining budget per wait)
    #                    progress_callback ────────┘ (per completed region)
    #
    # With a deadline, each as_completed wait gets only the remaining budget;
    # on expiry we cancel pending futures and keep what finished (partials).
    _deadline_at = (
        time.monotonic() + deadline_seconds if deadline_seconds else None
    )
    pool = ThreadPoolExecutor(max_workers=min(max_workers, len(regions)))
    try:
        future_to_region = {
            pool.submit(_audit_region, session, region, active_checks): region
            for region in regions
        }
        pending = set(future_to_region)
        done_count = 0
        while pending:
            timeout = None
            if _deadline_at is not None:
                timeout = _deadline_at - time.monotonic()
                if timeout <= 0:
                    break
            try:
                for future in as_completed(pending, timeout=timeout):
                    pending.discard(future)
                    region = future_to_region[future]
                    done_count += 1
                    try:
                        region_findings = future.result()
                        # Stamp account_id onto all findings from this region
                        for f in region_findings:
                            if f.get("account_id") is None:
                                f["account_id"] = account_id
                        all_findings.extend(region_findings)
                        log.info(
                            "Region %s: %d findings", region, len(region_findings)
                        )
                        if progress_callback is not None:
                            try:
                                progress_callback(
                                    region, len(region_findings),
                                    done_count, len(regions),
                                )
                            except Exception:  # progress must never kill a scan
                                pass
                    except Exception as exc:
                        msg = f"Region {region} failed: {exc}"
                        log.warning(msg)
                        errors.append(msg)
            except (_FuturesTimeout, TimeoutError):
                # concurrent.futures.TimeoutError is the builtin only on 3.11+;
                # catch both so a 3.10 interpreter (tests, old shims) behaves.
                break
        if pending:
            regions_timed_out = sorted(
                future_to_region[f] for f in pending
            )
            # Cancel every queued future (best-effort); the ones whose thread is
            # already blocked in a boto3 call can't be cancelled and outlive this
            # function. List-comp, not any(), so short-circuiting never skips a
            # cancel. The caller (CLI) uses the flag to hard-exit instead of
            # letting interpreter shutdown join those threads, which would hang.
            uncancellable = [f for f in pending if not f.cancel()]
            threads_abandoned = bool(uncancellable)
            log.warning(
                "Deadline of %ss hit; %d region(s) not scanned: %s",
                deadline_seconds, len(regions_timed_out), regions_timed_out,
            )
    finally:
        # cancel_futures drops queued work; threads mid-boto3-call finish in
        # the background (wait=False) and their results are discarded.
        pool.shutdown(wait=False, cancel_futures=True)

    # ── Compute Optimizer (global, not per-region) ───────────────────────────
    # Skipped when the deadline already expired: CO is a bonus enrichment, and
    # a partial-results return should not blow further past the budget.
    _deadline_expired = (
        _deadline_at is not None and time.monotonic() >= _deadline_at
    )
    co_count = 0
    if active_checks & {"ec2", "lambda", "rds"} and not _deadline_expired:
        co_findings = _fetch_compute_optimizer_recommendations(session)
        for f in co_findings:
            if f.get("account_id") is None:
                f["account_id"] = account_id
        all_findings.extend(co_findings)
        co_count = len(co_findings)
        log.info("Compute Optimizer: %d findings", co_count)

    # ── Deduplicate & sort ────────────────────────────────────────────────────
    all_findings = _dedup_findings(all_findings)
    all_findings.sort(key=lambda f: f.get("estimated_monthly_savings", 0), reverse=True)

    # ── Evidence classification ───────────────────────────────────────────────
    # Runs AFTER dedup so it annotates only the findings that survive, and after
    # the sort so ranking still uses the raw figure. Additive: the per-finding
    # savings number is untouched, only the claim we make about it changes.
    all_findings = _annotate_evidence(all_findings)
    evidence_totals = _split_evidence_totals(all_findings)

    # ── Aggregations ──────────────────────────────────────────────────────────
    total_savings = sum(f.get("estimated_monthly_savings", 0) for f in all_findings)

    by_category: dict[str, dict] = {}
    by_severity: dict[str, dict] = {}
    by_region: dict[str, dict] = {}

    for f in all_findings:
        cat = f.get("waste_type", "unknown")
        sev = f.get("severity", "low")
        reg = f.get("region", "unknown")
        sav = f.get("estimated_monthly_savings", 0)

        if cat not in by_category:
            by_category[cat] = {"count": 0, "total_estimated_monthly_savings": 0.0}
        by_category[cat]["count"] += 1
        by_category[cat]["total_estimated_monthly_savings"] += sav

        if sev not in by_severity:
            by_severity[sev] = {"count": 0, "total_estimated_monthly_savings": 0.0}
        by_severity[sev]["count"] += 1
        by_severity[sev]["total_estimated_monthly_savings"] += sav

        if reg not in by_region:
            by_region[reg] = {"count": 0, "total_estimated_monthly_savings": 0.0}
        by_region[reg]["count"] += 1
        by_region[reg]["total_estimated_monthly_savings"] += sav

    # Round aggregated values
    for d in list(by_category.values()) + list(by_severity.values()) + list(by_region.values()):
        d["total_estimated_monthly_savings"] = round(d["total_estimated_monthly_savings"], 2)

    return {
        "account_id": account_id,
        "regions_scanned": sorted(set(regions) - set(regions_timed_out)),
        "regions_timed_out": regions_timed_out,
        "checks_run": sorted(active_checks),
        "total_findings": len(all_findings),
        "total_estimated_monthly_savings": round(total_savings, 2),
        "total_estimated_annual_savings": round(total_savings * 12, 2),
        # The honest split of that same total. `measured_monthly_savings` is what we
        # observed and will stand behind; `unconfirmed_monthly_opportunity` is a work
        # queue, and every finding in it carries confirm_steps. Report the two
        # separately rather than adding them into one headline nobody believes.
        **evidence_totals,
        "findings": all_findings,
        "by_category": by_category,
        "by_severity": by_severity,
        "by_region": by_region,
        "compute_optimizer_findings": co_count,
        "errors": errors,
        "_threads_abandoned": threads_abandoned,
    }


# ── Instance deep analysis (single instance) ──────────────────────────────────

def get_instance_deep_analysis(
    instance_id: str,
    region: str = "us-east-1",
    lookback_days: int = 14,
    role_arn: str | None = None,
) -> dict:
    """
    Full CloudWatch analysis for a single EC2 instance, layered with
    Compute Optimizer recommendations if available.

    Returns utilization profile + rightsizing recommendation.
    """
    if role_arn is None:
        role_arns_env = os.getenv("AWS_ROLE_ARNS", "")
        if role_arns_env:
            role_arn = role_arns_env.split(",")[0].strip() or None

    try:
        session = _get_boto3_session(role_arn)
    except Exception as exc:
        return {"error": f"Could not create AWS session: {exc}"}

    from .cloudwatch import get_ec2_utilization

    ec2_client = session.client("ec2", region_name=region)
    cw_client = session.client("cloudwatch", region_name=region)

    utilization = get_ec2_utilization(ec2_client, cw_client, instance_id, period_days=lookback_days)

    # Rightsizing recommendation based on our own thresholds
    cpu_avg = utilization.get("cpu", {}).get("average") or 0.0
    cpu_p99 = utilization.get("cpu", {}).get("p99") or 0.0

    # Network and disk are already fetched by get_ec2_utilization and used to be
    # thrown away, leaving a CPU-only verdict. They cannot prove an instance is
    # right-sized, but they can prove it is NOT idle: a box moving a gigabyte a day
    # or doing sustained disk IO is working, whatever its CPU says. Cheapest
    # available correction to the classic "low CPU means unused" false positive,
    # and it costs no extra API calls.
    activity = _non_cpu_activity(utilization)

    our_recommendation = _cpu_verdict(cpu_avg, cpu_p99, lookback_days, activity)

    # Fetch Compute Optimizer recommendation for this specific instance
    co_recommendation = None
    try:
        co = session.client("compute-optimizer", region_name="us-east-1")
        resp = co.get_ec2_instance_recommendations(
            instanceArns=[
                f"arn:aws:ec2:{region}:{session.client('sts').get_caller_identity()['Account']}:instance/{instance_id}"
            ]
        )
        recs = resp.get("instanceRecommendations", [])
        if recs:
            rec = recs[0]
            options = rec.get("recommendationOptions", [])
            co_recommendation = {
                "finding": rec.get("finding"),
                "finding_reason_codes": rec.get("findingReasonCodes", []),
                "options": [
                    {
                        "instance_type": opt.get("instanceType"),
                        "performance_risk": opt.get("performanceRisk"),
                        "estimated_monthly_savings": opt.get("estimatedMonthlySavings", {}).get("value"),
                        "savings_currency": opt.get("estimatedMonthlySavings", {}).get("currency"),
                        "rank": opt.get("rank"),
                    }
                    for opt in options[:3]  # top 3 options
                ],
            }
    except Exception as exc:
        log.debug("Compute Optimizer unavailable for %s: %s", instance_id, exc)

    return {
        "instance_id": instance_id,
        "region": region,
        "lookback_days": lookback_days,
        "utilization": utilization,
        "nable_recommendation": our_recommendation,
        "compute_optimizer_recommendation": co_recommendation,
        # ONE answer, so the caller never has to reconcile two recommendations that
        # disagree. Compute Optimizer wins when it has an opinion: it weighs CPU,
        # memory, network and disk and supplies its own dollar figure, which is
        # measured evidence. Our CPU heuristic is the fallback and says so.
        "verdict": _deep_analysis_verdict(our_recommendation, co_recommendation),
    }


# Thresholds for "this instance is doing something that is not CPU". Deliberately
# conservative: they exist to STOP a confident idle call, never to make one.
#   1 GB/day combined network is ~12 KB/s sustained. An idle box does far less.
#   100k disk ops/day is ~1.2 ops/s sustained. Also well clear of noise.
_ACTIVE_NET_BYTES_PER_DAY = 1_000_000_000
_ACTIVE_DISK_OPS_PER_DAY = 100_000


def _non_cpu_activity(utilization: dict) -> list[str]:
    """Evidence the instance is working even if its CPU looks idle."""
    def _avg(key: str) -> float:
        try:
            return float(utilization.get(key, {}).get("average_per_day") or 0.0)
        except (TypeError, ValueError):
            return 0.0

    signals: list[str] = []
    net = _avg("network_in_bytes") + _avg("network_out_bytes")
    if net > _ACTIVE_NET_BYTES_PER_DAY:
        signals.append(f"moving ~{net / 1_000_000_000:.1f} GB/day of network traffic")
    disk = _avg("disk_read_ops") + _avg("disk_write_ops")
    if disk > _ACTIVE_DISK_OPS_PER_DAY:
        signals.append(f"doing ~{disk:,.0f} disk ops/day")
    return signals


# What this analysis cannot see, stated once so every verdict can carry it.
_NO_MEMORY_DATA = (
    "This verdict is CPU plus network and disk. Memory is not here: EC2 does not "
    "publish it without the CloudWatch agent, and memory is the single most common "
    "reason a low-CPU instance cannot actually be downsized."
)
_CONFIRM_STEPS = (
    "Check memory utilization (CloudWatch agent, or the host's own metrics).",
    "Compare average against p99 CPU. A wide gap means bursty, not idle.",
    "Ask the workload owner what runs here before resizing or stopping it.",
)


def _cpu_verdict(cpu_avg: float, cpu_p99: float, lookback_days: int,
                 activity: list[str]) -> dict:
    """Our own read on the instance. Always an INVESTIGATION: it is a heuristic on
    partial signals, so it never carries a dollar figure. The previous version put
    the strings "high — check instance type for exact figure" and "medium — run
    Compute Optimizer" in an estimated_monthly_savings field, which is neither a
    number nor an honest absence of one."""
    base = {
        "evidence": "inferred",
        "kind": "investigation",
        "why_unsure": _NO_MEMORY_DATA,
        "confirm_steps": list(_CONFIRM_STEPS),
        # No estimated_monthly_savings key at all. We did not measure one.
    }
    if activity:
        joined = " and ".join(activity)
        if cpu_avg < 20.0:
            return {**base, "action": "none", "confidence": "low",
                    "reason": (
                        f"CPU is low (avg {cpu_avg:.1f}%, p99 {cpu_p99:.1f}% over "
                        f"{lookback_days}d) but this instance is {joined}. Low CPU on a "
                        f"box that is moving data is not an idle box; it is a workload "
                        f"that is not CPU-bound."
                    )}
    if cpu_avg < 5.0:
        return {**base, "action": "investigate_for_stop", "confidence": "medium",
                "reason": (
                    f"Average CPU {cpu_avg:.1f}% over {lookback_days}d (< 5%), with no "
                    f"significant network or disk activity. Looks unused."
                )}
    if cpu_avg < 20.0 and cpu_p99 < 50.0:
        return {**base, "action": "investigate_for_downsize", "confidence": "medium",
                "reason": (
                    f"Average CPU {cpu_avg:.1f}%, p99 CPU {cpu_p99:.1f}% over "
                    f"{lookback_days}d. Headroom at both average and peak."
                )}
    return {"action": "none", "evidence": "inferred", "kind": "investigation",
            "confidence": "medium",
            "reason": (
                f"CPU utilization looks reasonable (avg {cpu_avg:.1f}%, "
                f"p99 {cpu_p99:.1f}%)."
            )}


def _deep_analysis_verdict(ours: dict, co: dict | None) -> dict:
    """Collapse the two opinions into the single answer a caller should act on."""
    options = (co or {}).get("options") or []
    best = options[0] if options else None
    savings = (best or {}).get("estimated_monthly_savings")
    if best and savings is not None:
        return {
            "source": "aws_compute_optimizer",
            "evidence": "measured",
            "kind": "recommendation",
            "action": "downsize",
            "recommended_instance_type": best.get("instance_type"),
            "estimated_monthly_savings": savings,
            "performance_risk": best.get("performance_risk"),
            "reason": (
                f"AWS Compute Optimizer rates this instance "
                f"{(co or {}).get('finding')} using CPU, memory, network and disk, "
                f"and sizes the saving itself."
            ),
        }
    return {"source": "nable_cpu_heuristic", **ours}


# ── CloudWatch Log Group scan (standalone for MCP tool) ──────────────────────

def scan_cloudwatch_log_waste(
    regions: list[str] | None = None,
    role_arn: str | None = None,
) -> dict:
    """
    Scan all CloudWatch Log Groups across regions for missing retention policies.

    Returns:
        {
            "total_log_groups_scanned": int,
            "log_groups_without_retention": int,
            "total_estimated_monthly_cost": float,
            "findings": list[dict],
            "by_region": dict,
            "recommended_actions": list[str],
        }
    """
    if role_arn is None:
        role_arns_env = os.getenv("AWS_ROLE_ARNS", "")
        if role_arns_env:
            role_arn = role_arns_env.split(",")[0].strip() or None

    try:
        session = _get_boto3_session(role_arn)
    except Exception as exc:
        return {"error": f"Could not create AWS session: {exc}"}

    if regions is None:
        regions = _discover_regions(session)

    from .waste import check_cloudwatch_logs

    all_findings: list[dict] = []
    total_scanned = 0
    by_region: dict[str, dict] = {}

    for region in regions:
        try:
            logs_client = session.client("logs", region_name=region)

            # Count total log groups for this region
            region_total = 0
            try:
                paginator = logs_client.get_paginator("describe_log_groups")
                for page in paginator.paginate():
                    region_total += len(page.get("logGroups", []))
            except Exception:
                pass

            total_scanned += region_total

            findings = check_cloudwatch_logs(logs_client, region=region)
            all_findings.extend(findings)

            region_savings = sum(f.get("estimated_monthly_savings", 0) for f in findings)
            by_region[region] = {
                "total_log_groups": region_total,
                "without_retention": len(findings),
                "estimated_monthly_cost": round(region_savings, 2),
            }
        except Exception as exc:
            log.warning("CloudWatch logs scan failed in %s: %s", region, exc)

    all_findings.sort(key=lambda f: f.get("estimated_monthly_savings", 0), reverse=True)
    total_cost = sum(f.get("estimated_monthly_savings", 0) for f in all_findings)

    # Generate actionable CLI commands for top offenders
    top_5 = all_findings[:5]
    recommended_actions = []
    for f in top_5:
        lg = f.get("resource_id", "")
        days = f.get("recommended_retention_days", 30)
        reg = f.get("region", "us-east-1")
        recommended_actions.append(
            f"aws logs put-retention-policy --region {reg} "
            f"--log-group-name '{lg}' --retention-in-days {days}"
        )

    return {
        "total_log_groups_scanned": total_scanned,
        "log_groups_without_retention": len(all_findings),
        "total_estimated_monthly_cost": round(total_cost, 2),
        "findings": all_findings,
        "by_region": by_region,
        "recommended_actions": recommended_actions,
        "note": (
            "Estimated cost is based on current stored bytes at $0.03/GB-month. "
            "Setting a retention policy will not immediately delete existing data — "
            "CW will expire logs older than the retention period on its next cycle."
        ),
    }
