# SPDX-License-Identifier: Apache-2.0
"""aws_waste MCP tools (extracted from server.py; see finops/tools/__init__.py).

Server-local helpers, globals, and the mcp instance are reached through the live
server module (_srv.NAME) so monkeypatching finops.server.* still works and no
import-order coupling exists."""
from __future__ import annotations

from .. import server as _srv


async def _price_on_customer_rates(findings: list[dict], resource_type: str) -> dict:
    """Re-price list-price findings on the customer's measured rate, and say so.

    get_rightsizing_recommendations (EC2) has always routed savings through
    detect_savings_context + adjust_savings and returned a `pricing_basis` block
    saying how the figure was priced. get_rds_rightsizing_recommendations and
    get_ecs_rightsizing_recommendations did neither: they summed the list-price
    hourly tables and returned the raw figure with no label.

    So for a customer with a measured 35% EDP, an RDS downsize really worth
    ~$325/mo was quoted at $499, while its EC2 sibling in the same session was
    quoted correctly, and nothing in either response marked them as different
    bases. Two tools in one family answering "how much will this save" on
    incompatible arithmetic is worse than either being wrong alone, because a
    reader comparing them has no way to know.

    This exists as one shared helper rather than a third copy of the logic. The
    audit that found this also found the EC2/RDS ALB price drifting apart and a
    dedup map keyed on strings nothing emitted: every one of those was two copies
    of a rule that were only equal on the day they were written.

    Adds to each finding, leaving the original figure intact so nothing that
    reads estimated_monthly_savings changes meaning:
      adjusted_monthly_savings  the figure on the customer's real rates
      savings_basis             effective_rate | commitment_coverage | list_price

    Returns the pricing_basis block for the response. Degrades to list price with
    a low-confidence label when no rate data is reachable, which is the honest
    answer for a customer with no CUR and no Cost Explorer access.
    """
    from ..recommendations.effective_savings import adjust_savings, detect_savings_context

    try:
        ctx = await _srv.asyncio.to_thread(detect_savings_context)
    except Exception as exc:
        _srv.log.debug("savings context unavailable, pricing at list: %s", exc)
        return {"basis": {"list_price": len(findings)}, "confidence": {"low": len(findings)},
                "note": ("Savings are shown at list price because no rate data was "
                         "reachable. Connect your Cost and Usage Report (CUR) to price "
                         "them on your real rates.")}

    bases: dict[str, int] = {}
    confidences: dict[str, int] = {}
    adjusted_total = 0.0

    for f in findings:
        listed = f.get("estimated_monthly_savings")
        if listed is None:
            continue
        a = adjust_savings(float(listed), resource_type=resource_type, ctx=ctx)
        f["adjusted_monthly_savings"] = round(a.effective_savings, 2)
        f["savings_basis"] = a.basis
        adjusted_total += a.effective_savings
        bases[a.basis] = bases.get(a.basis, 0) + 1
        confidences[a.confidence] = confidences.get(a.confidence, 0) + 1

    pricing: dict = {"basis": bases, "confidence": confidences,
                     "adjusted_total_monthly_savings": round(adjusted_total, 2)}

    rate = getattr(ctx, "rate", None)
    if rate is not None and getattr(rate, "confidence", "low") in ("high", "medium"):
        pricing["effective_discount_pct"] = round(
            float(getattr(rate, "overall_discount_pct", 0.0)) * 100, 1)
        pricing["rate_source"] = getattr(rate, "source", "measured")
    cc = getattr(ctx, "commitment", None)
    if cc is not None and getattr(cc, "available", False):
        pricing["commitment_coverage_pct"] = round(cc.combined_pct, 1)
    if "list_price" in bases:
        pricing["note"] = (
            "Some savings are shown at list price because no rate data was found. "
            "Connect your Cost and Usage Report (CUR) to price them on your real rates."
        )
    return pricing


@_srv.mcp.tool()
async def take_snapshot_now() -> dict:
    """
    Manually trigger a cost snapshot right now (fetches yesterday's costs from all providers).
    Normally this runs automatically at 01:00 UTC daily.

    Examples:
        - "Take a cost snapshot now"
        - "Update the cost history with today's data"
    """
    from ..scheduler.jobs import run_snapshot_now
    results = await run_snapshot_now()
    # Explicit refresh: bust the read-through cache so the next query reflects
    # the freshly taken snapshot rather than a pre-snapshot cached copy.
    from .. import cache as _cache
    _cache.clear()
    return {"status": "complete", "results": results}


@_srv.mcp.tool()
async def get_rightsizing_recommendations(
    avg_cpu_threshold: float = 20.0,
    max_cpu_threshold: float = 50.0,
) -> dict:
    """
    Analyze EC2 instances with low CPU utilization over the past 14 days and
    return rightsizing recommendations with projected monthly savings.

    Args:
        avg_cpu_threshold: Flag instances with average CPU below this % (default 20%)
        max_cpu_threshold: Flag instances whose peak CPU never exceeded this % (default 50%)

    Examples:
        - "Which EC2 instances are over-provisioned?"
        - "How much could we save by rightsizing?"
        - "Find underutilized instances we should downsize"
    """
    from ..demo_data import is_demo, get_demo_response
    if is_demo():
        return get_demo_response("get_rightsizing_recommendations") or {}

    try:
        from ..recommendations.rightsizing import analyze_rightsizing, rightsizing_summary
        from ..recommendations.effective_savings import detect_savings_context
        # Offload the blocking CloudWatch/EC2 scan so it does not freeze the MCP
        # event loop (and the editor) for the tens of seconds it can take.
        recs = await _srv.asyncio.to_thread(
            analyze_rightsizing,
            avg_cpu_threshold=avg_cpu_threshold,
            max_cpu_threshold=max_cpu_threshold,
        )
        # Price the savings on the customer's real environment: measured effective
        # rate (EDP/private + commitment, from their CUR/Cost Explorer) with
        # commitment coverage as a fallback. Cached (~15 min) and off-thread;
        # degrades to list price with a low-confidence label if no data is reachable.
        savings_ctx = await _srv.asyncio.to_thread(detect_savings_context)
        result = rightsizing_summary(recs, savings_ctx=savings_ctx)

        # Persist recommendations for savings tracking (fire-and-forget)
        try:
            from ..recommendations.savings_tracker import record_recommendation
            for rec in recs:
                if rec.monthly_savings > 0:
                    record_recommendation(
                        source="rightsizing",
                        provider="aws",
                        resource_id=rec.instance_id,
                        resource_type=rec.resource_type,
                        resource_name=rec.name,
                        account_id=rec.account_id,
                        region=rec.region,
                        current_config={
                            "instance_type": rec.instance_type,
                            "monthly_cost_usd": rec.current_monthly_cost,
                        },
                        recommended_config={
                            "instance_type": rec.recommended_type,
                            "monthly_cost_usd": rec.recommended_monthly_cost,
                            "from_instance_type": rec.instance_type,
                        },
                        description=rec.title,
                        estimated_monthly_savings_usd=rec.monthly_savings,
                    )
        except Exception:
            pass  # never block the main response

        # Nudge free users toward ticket creation when there are real savings on the table
        if isinstance(result, dict) and result.get("genuine_monthly_savings", 0) > 0:
            savings = result["genuine_monthly_savings"]
            count = result.get("verdicts", {}).get("genuine_savings", 0)
            nudge = _srv._team_nudge(
                f"You have {count} genuine rightsizing opportunit{'ies' if count != 1 else 'y'} "
                f"worth ${savings:,.0f}/mo after commitment coverage. To auto-create Jira, "
                f"Linear, or GitHub tickets so these actually get fixed, upgrade to Pro:"
            , context="rightsizing_recommendations")
            if nudge:
                result["_upgrade"] = nudge

        return result
    except Exception as e:
        return {"error": str(e)}


@_srv.mcp.tool()
async def audit_aws_waste(
    regions: list[str] | None = None,
    checks: list[str] | None = None,
    account_id: str | None = None,
) -> dict:
    """
    Deep AWS waste audit: scans EC2, EBS, RDS, Lambda, NAT Gateways, CloudWatch
    Logs, S3, and CloudTrail for waste. Returns findings sorted by monthly savings.

    Args:
        regions: AWS regions to scan. Defaults to all opted-in regions.
        checks: Subset to run: ebs, snapshots, eips, nat, rds, cloudtrail,
                cloudwatch, s3, lambda, ec2. Defaults to all.
        account_id: AWS account ID (auto-discovered from STS if not provided).

    Examples:
        - "Run a full AWS waste audit"
        - "Find all idle NAT gateways and unattached EBS volumes"
        - "Audit CloudWatch log groups for missing retention policies"
    """
    try:
        from ..analyzers.optimizer import run_deep_audit
        # Offload the blocking multi-region waste scan off the event loop; this is
        # the heaviest scan and would otherwise freeze the server for minutes.
        report = await _srv.asyncio.to_thread(
            run_deep_audit,
            account_id=account_id,
            regions=regions,
            checks=checks,
        )
        # Cap the detail findings list to a token budget. The list is already sorted
        # by estimated_monthly_savings desc, so fit_to_budget keeps the highest-value
        # findings. All totals/aggregates (total_findings, total_estimated_monthly_savings,
        # by_category/by_severity/by_region) are computed over the WHOLE list upstream and
        # are left untouched, so the model can still state the full picture.
        all_findings = report.get("findings") or []
        if all_findings:
            kept, omitted = _srv.fit_to_budget(all_findings, max_tokens=6000)
            report["findings"] = kept
            if omitted > 0:
                report["findings_truncated"] = (
                    f"Showing top {len(kept)} of {len(all_findings)} findings by monthly "
                    f"savings. {omitted} lower-value findings omitted. Use by_category, "
                    f"by_region, and by_severity for the full breakdown, or pass checks/"
                    f"regions to narrow the scan for full detail."
                )
        # Add a human-readable summary at the top
        monthly = report.get("total_estimated_monthly_savings", 0)
        findings = report.get("total_findings", 0)
        report["summary"] = (
            f"Found {findings} waste findings across "
            f"{len(report.get('regions_scanned', []))} region(s). "
            f"Estimated savings: ${monthly:,.2f}/mo "
            f"(${report.get('total_estimated_annual_savings', 0):,.2f}/yr)."
        )

        # Nudge free users toward ticket creation when there is real waste on the table
        if monthly > 0 and findings > 0:
            nudge = _srv._team_nudge(
                f"To auto-create Jira, Linear, or GitHub tickets for these {findings} "
                f"findings so your team actually acts on them, upgrade to Pro:"
            , context="aws_waste")
            if nudge:
                report["_upgrade"] = nudge

        return report
    except Exception as e:
        _srv.log.error("audit_aws_waste failed: %s", e, exc_info=True)
        return {"error": str(e)}


@_srv.mcp.tool()
async def audit_public_ipv4_addresses(
    regions: list[str] | None = None,
) -> str:
    """
    Audits public IPv4 addresses across AWS. Since Feb 2024, AWS charges
    $3.60/month per IP including stopped instances. Finds unattached Elastic IPs
    and IPs on stopped instances with release recommendations.

    Args:
        regions: AWS regions to scan. Defaults to all opted-in regions.

    Examples:
        - "Find unattached Elastic IPs we can release"
        - "How much are we spending on public IPv4?"
        - "Show Elastic IPs on stopped instances"
    """
    try:
        from ..recommendations.public_ipv4 import audit_public_ipv4
        aws = _srv.CLOUD_CONNECTORS.get("aws")
        if aws is None:
            return "AWS is not connected. Call connect_aws right here in the chat (it detects credentials already on this machine), or run 'uvx nable' in a terminal."

        result = await audit_public_ipv4(aws, regions=regions)

        unattached = result["unattached_eips"]
        stopped = result["stopped_instance_eips"]
        waste = result["total_monthly_waste"]
        total_ips = result["total_ips_found"]

        lines: list[str] = ["## Public IPv4 Audit", ""]

        _TABLE_CAP = 30

        if unattached:
            lines.append(f"**Unattached Elastic IPs** (release immediately) -- {len(unattached)} found")
            lines.append("")
            lines.append("| IP | Allocation ID | Region | Monthly Cost |")
            lines.append("|---|---|---|---|")
            unattached_sorted = sorted(unattached, key=lambda x: x["monthly_cost"], reverse=True)
            for eip in unattached_sorted[:_TABLE_CAP]:
                lines.append(
                    f"| {eip['public_ip']} | {eip['allocation_id']} "
                    f"| {eip['region']} | ${eip['monthly_cost']:.2f} |"
                )
            if len(unattached_sorted) > _TABLE_CAP:
                rest = unattached_sorted[_TABLE_CAP:]
                rest_cost = sum(e["monthly_cost"] for e in rest)
                lines.append(
                    f"| ... and {len(rest)} more | | | ${rest_cost:.2f} total |"
                )
                lines.append("")
                lines.append(f"_Showing top {_TABLE_CAP} of {len(unattached_sorted)} unattached IPs by cost. Scan a single region for the full list._")
            lines.append("")
        else:
            lines.append("**Unattached Elastic IPs:** None found.")
            lines.append("")

        if stopped:
            lines.append(f"**IPs on stopped instances** -- {len(stopped)} found")
            lines.append("")
            lines.append("| IP | Instance ID | Region | Monthly Cost |")
            lines.append("|---|---|---|---|")
            stopped_sorted = sorted(stopped, key=lambda x: x["monthly_cost"], reverse=True)
            for eip in stopped_sorted[:_TABLE_CAP]:
                lines.append(
                    f"| {eip['public_ip']} | {eip['instance_id']} "
                    f"| {eip['region']} | ${eip['monthly_cost']:.2f} |"
                )
            if len(stopped_sorted) > _TABLE_CAP:
                rest = stopped_sorted[_TABLE_CAP:]
                rest_cost = sum(e["monthly_cost"] for e in rest)
                lines.append(
                    f"| ... and {len(rest)} more | | | ${rest_cost:.2f} total |"
                )
                lines.append("")
                lines.append(f"_Showing top {_TABLE_CAP} of {len(stopped_sorted)} stopped-instance IPs by cost. Scan a single region for the full list._")
            lines.append("")
        else:
            lines.append("**IPs on stopped instances:** None found.")
            lines.append("")

        waste_count = len(unattached) + len(stopped)
        lines.append(f"Total monthly waste: ${waste:.2f} across {waste_count} address{'es' if waste_count != 1 else ''}")
        lines.append(f"Total public IPs found: {total_ips} across all scanned regions")
        lines.append("")

        if unattached:
            lines.append("To release unattached IPs:")
            lines.append("```")
            for eip in unattached:
                lines.append(f"aws ec2 release-address --allocation-id {eip['allocation_id']} --region {eip['region']}")
            lines.append("```")

        return "\n".join(lines)

    except Exception as e:
        _srv.log.error("audit_public_ipv4_addresses failed: %s", e, exc_info=True)
        return f"Error running IPv4 audit: {e}"


@_srv.mcp.tool()
async def get_instance_deep_analysis(
    instance_id: str,
    region: str = "us-east-1",
    lookback_days: int = 14,
) -> dict:
    """
    Deep CloudWatch analysis for a specific EC2 instance. Returns CPU, network,
    and disk utilization percentiles, a rightsizing recommendation, and the
    Compute Optimizer recommendation if available.

    Args:
        instance_id: EC2 instance ID (e.g. "i-0abc1234567890def")
        region: AWS region (default: us-east-1)
        lookback_days: Days of metrics to analyze (default: 14, max: 63)

    Examples:
        - "Is i-0abc1234 over-provisioned?"
        - "Show CPU trends for i-0abc1234 over the last 30 days"
    """
    try:
        from ..analyzers.optimizer import get_instance_deep_analysis as _analyze
        return _analyze(
            instance_id=instance_id,
            region=region,
            lookback_days=lookback_days,
        )
    except Exception as e:
        _srv.log.error("get_instance_deep_analysis failed: %s", e, exc_info=True)
        return {"error": str(e)}


@_srv.mcp.tool()
async def scan_cloudwatch_waste(
    regions: list[str] | None = None,
) -> dict:
    """
    Finds CloudWatch Log Groups with no retention policy (infinite retention
    costs $0.03/GB-month). Returns groups, estimated monthly cost, recommended
    retention periods by log type, and CLI commands to fix top offenders.

    Args:
        regions: Regions to scan. Defaults to all opted-in regions.

    Examples:
        - "Which CloudWatch log groups have no retention policy?"
        - "Scan for infinite log retention across all regions"
    """
    try:
        from ..analyzers.optimizer import scan_cloudwatch_log_waste
        result = scan_cloudwatch_log_waste(regions=regions)
        if isinstance(result, dict) and "error" not in result:
            findings = result.get("findings", [])
            if isinstance(findings, list) and findings:
                # findings is pre-sorted desc by estimated_monthly_savings in the connector.
                kept, omitted = _srv.fit_to_budget(findings)
                result["findings"] = kept
                if omitted:
                    result["findings_truncated"] = True
                    result["hint"] = (
                        f"Showing top {len(kept)} of {len(findings)} log groups by estimated "
                        "monthly cost. Totals and per-region counts above cover all of them; "
                        "see by_region for the full breakdown or scan a single region for detail."
                    )
        return result
    except Exception as e:
        _srv.log.error("scan_cloudwatch_waste failed: %s", e, exc_info=True)
        return {"error": str(e)}


@_srv.mcp.tool()
async def get_rds_rightsizing_recommendations(
    cpu_threshold: float = 20.0,
    regions: list[str] | None = None,
) -> dict:
    """
    Detect over-provisioned RDS instances with low CPU utilization.

    Uses CloudWatch CPUUtilization over 14 days. Excludes Aurora Serverless
    and read replicas. Returns downsizing recommendations with estimated savings.

    Args:
        cpu_threshold: Flag instances with average CPU below this % (default 20%).
        regions: AWS regions to scan. Defaults to all opted-in regions.

    Examples:
        - "Which RDS instances are over-provisioned?"
        - "Find oversized databases we can downsize"
        - "How much could we save by rightsizing RDS?"
    """
    try:
        import boto3
        from ..analyzers.waste import check_rds_rightsizing

        loop = _srv.asyncio.get_event_loop()

        if regions is None:
            try:
                regions = await loop.run_in_executor(
                    None,
                    lambda: [
                        r["RegionName"]
                        for r in boto3.client("ec2", region_name="us-east-1").describe_regions(
                            Filters=[{"Name": "opt-in-status", "Values": ["opt-in-not-required", "opted-in"]}]
                        ).get("Regions", [])
                    ],
                )
            except Exception:
                regions = ["us-east-1", "us-west-2", "eu-west-1"]

        denied_actions: set = set()

        async def _scan_region_rds_rs(region: str) -> list[dict]:
            try:
                return await loop.run_in_executor(
                    None,
                    lambda: check_rds_rightsizing(
                        boto3.client("rds", region_name=region),
                        boto3.client("cloudwatch", region_name=region),
                        region,
                        cpu_threshold_pct=cpu_threshold,
                    ),
                )
            except Exception as exc:
                action = _srv._denied_action(str(exc))
                if action:
                    denied_actions.add(action)
                else:
                    _srv.log.warning("RDS rightsizing scan failed for region %s: %s", region, exc)
                return []

        region_results = await _srv.asyncio.gather(*[_scan_region_rds_rs(r) for r in regions])
        all_findings: list[dict] = [f for findings in region_results for f in findings]

        # A permission gap is a precise, fixable cause, not "no data". Surface the
        # exact missing IAM action so the model leads with the real fix instead of
        # guessing about CloudWatch or regions.
        if not all_findings and denied_actions:
            actions = sorted(denied_actions)
            return {
                "count": 0,
                "permission_error": True,
                "missing_permissions": actions,
                "error": (
                    "Could not read your RDS/DocumentDB instances: the IAM identity nable uses "
                    f"is missing {', '.join(actions)}. This is a permissions gap, not missing "
                    "utilization data."
                ),
                "fix": (
                    "Add these read-only actions to nable's IAM policy, or run "
                    "'finops setup aws --iam-template' for the full least-privilege policy, then re-run."
                ),
            }

        all_findings.sort(key=lambda x: x.get("estimated_monthly_savings", 0), reverse=True)
        total_savings = sum(f.get("estimated_monthly_savings", 0) for f in all_findings)

        # Persist for savings tracking so the acted-on -> verified loop covers
        # RDS downsizes the same way it covers EC2 (verify_rds_change confirms
        # the DBInstanceClass switch, measure_realized_savings prices it).
        try:
            from ..recommendations.savings_tracker import record_recommendation
            for f in all_findings:
                if f.get("estimated_monthly_savings", 0) > 0 and f.get("recommended_class"):
                    record_recommendation(
                        source="rightsizing",
                        provider="aws",
                        resource_id=f["resource_id"],
                        resource_type="rds",
                        resource_name=f["resource_id"],
                        current_config={"instance_class": f.get("current_class", "")},
                        recommended_config={
                            "instance_class": f["recommended_class"],
                            "from_instance_class": f.get("current_class", ""),
                        },
                        description=(f.get("detail", "") or "")[:500],
                        estimated_monthly_savings_usd=f["estimated_monthly_savings"],
                        region=f.get("region", "") or "",
                    )
        except Exception as exc:
            _srv.log.debug("RDS rec tracking skipped: %s", exc)

        # Same basis as the EC2 sibling. Without this the RDS tool quotes list
        # price with no label while get_rightsizing_recommendations quotes the
        # customer's measured rate, and nothing marks them as different answers.
        pricing = await _price_on_customer_rates(all_findings, resource_type="rds")

        kept, omitted = _srv.fit_to_budget(all_findings)
        return {
            "count": len(all_findings),
            "total_monthly_savings": round(total_savings, 2),
            "total_annual_savings": round(total_savings * 12, 2),
            "adjusted_monthly_savings": pricing.get("adjusted_total_monthly_savings"),
            "pricing_basis": pricing,
            "regions_scanned": regions,
            "findings": kept,
            **({"findings_truncated": True, "hint": f"Showing {len(kept)} of {len(all_findings)} findings (highest savings first) to stay within token budget."} if omitted else {}),
            "tip": (
                "Verify FreeStorageSpace and DatabaseConnections before resizing. "
                "Take a snapshot before any instance class change."
            ),
        }
    except Exception as e:
        return {"error": str(e)}


@_srv.mcp.tool()
async def get_idle_rds_instances(
    regions: list[str] | None = None,
) -> dict:
    """
    Find RDS instances with near-zero database connections over the past 14 days.

    Zero-connection instances are likely decommissioned and can be stopped
    (free, preserves data) or deleted (saves full cost after final snapshot).

    Args:
        regions: AWS regions to scan. Defaults to all opted-in regions.

    Examples:
        - "Which RDS databases have no active connections?"
        - "Find idle databases we can stop to save money"
        - "Are there any unused RDS instances running?"
    """
    try:
        import boto3
        from ..analyzers.waste import check_rds_idle

        loop = _srv.asyncio.get_event_loop()

        if regions is None:
            try:
                regions = await loop.run_in_executor(
                    None,
                    lambda: [
                        r["RegionName"]
                        for r in boto3.client("ec2", region_name="us-east-1").describe_regions(
                            Filters=[{"Name": "opt-in-status", "Values": ["opt-in-not-required", "opted-in"]}]
                        ).get("Regions", [])
                    ],
                )
            except Exception:
                regions = ["us-east-1", "us-west-2", "eu-west-1"]

        async def _scan_region_rds_idle(region: str) -> list[dict]:
            try:
                return await loop.run_in_executor(
                    None,
                    lambda: check_rds_idle(
                        boto3.client("rds", region_name=region),
                        boto3.client("cloudwatch", region_name=region),
                        region,
                    ),
                )
            except Exception as exc:
                _srv.log.warning("RDS idle scan failed for region %s: %s", region, exc)
                return []

        region_results = await _srv.asyncio.gather(*[_scan_region_rds_idle(r) for r in regions])
        all_findings: list[dict] = [f for findings in region_results for f in findings]
        # A finding with an unknown instance class carries estimated_monthly_savings=None
        # (we refuse to fabricate a price). Treat None as 0 for sorting and the total,
        # so the headline never counts a made-up number, and surface how many were
        # unpriced so the total reads as a floor, not the whole truth.
        all_findings.sort(key=lambda x: x.get("estimated_monthly_savings") or 0, reverse=True)
        total_savings = sum((f.get("estimated_monthly_savings") or 0) for f in all_findings)
        unpriced = sum(1 for f in all_findings if f.get("unpriced"))

        kept, omitted = _srv.fit_to_budget(all_findings)
        out = {
            "count": len(all_findings),
            "total_monthly_savings": round(total_savings, 2),
            "total_annual_savings": round(total_savings * 12, 2),
            "findings": kept,
            **({"findings_truncated": True, "hint": f"Showing {len(kept)} of {len(all_findings)} findings (highest savings first) to stay within token budget."} if omitted else {}),
            "tip": (
                "Stopping an RDS instance pauses billing for compute (storage still billed). "
                "AWS auto-starts stopped instances after 7 days unless stopped again."
            ),
        }
        if unpriced:
            out["unpriced_count"] = unpriced
            out["unpriced_note"] = (
                f"{unpriced} idle instance(s) use a class not in nable's price table and "
                f"are excluded from the total. They are still idle; verify their real rate "
                f"in Cost Explorer. total_monthly_savings is a floor."
            )
        return out
    except Exception as e:
        return {"error": str(e)}


@_srv.mcp.tool()
async def get_idle_load_balancers(
    regions: list[str] | None = None,
    request_threshold: float = 100.0,
) -> dict:
    """
    Detect ALBs, NLBs, and Classic ELBs with near-zero traffic over the past 14 days.

    Idle load balancers still incur hourly LCU base charges. ALB/NLB cost ~$5.84/mo
    minimum; Classic ELBs cost ~$18.25/mo minimum.

    Args:
        regions: AWS regions to scan. Defaults to all opted-in regions.
        request_threshold: Max requests in 14 days to flag as idle (default 100).

    Examples:
        - "Find idle load balancers we can delete"
        - "Which ALBs have no traffic?"
        - "Are there any unused load balancers costing us money?"
    """
    try:
        import boto3
        from ..analyzers.waste import check_idle_load_balancers

        loop = _srv.asyncio.get_event_loop()

        if regions is None:
            try:
                regions = await loop.run_in_executor(
                    None,
                    lambda: [
                        r["RegionName"]
                        for r in boto3.client("ec2", region_name="us-east-1").describe_regions(
                            Filters=[{"Name": "opt-in-status", "Values": ["opt-in-not-required", "opted-in"]}]
                        ).get("Regions", [])
                    ],
                )
            except Exception:
                regions = ["us-east-1", "us-west-2", "eu-west-1"]

        async def _scan_region_elb(region: str) -> list[dict]:
            try:
                return await loop.run_in_executor(
                    None,
                    lambda: check_idle_load_balancers(
                        boto3.client("elbv2", region_name=region),
                        boto3.client("elb", region_name=region),
                        boto3.client("cloudwatch", region_name=region),
                        region,
                        request_threshold=request_threshold,
                    ),
                )
            except Exception as exc:
                _srv.log.warning("Load balancer idle scan failed for region %s: %s", region, exc)
                return []

        region_results = await _srv.asyncio.gather(*[_scan_region_elb(r) for r in regions])
        all_findings: list[dict] = [f for findings in region_results for f in findings]
        all_findings.sort(key=lambda x: x.get("estimated_monthly_savings", 0), reverse=True)
        total_savings = sum(f.get("estimated_monthly_savings", 0) for f in all_findings)

        kept, omitted = _srv.fit_to_budget(all_findings)
        return {
            "count": len(all_findings),
            "total_monthly_savings": round(total_savings, 2),
            "total_annual_savings": round(total_savings * 12, 2),
            "regions_scanned": regions,
            "findings": kept,
            **({"findings_truncated": True, "hint": f"Showing {len(kept)} of {len(all_findings)} findings (highest savings first) to stay within token budget."} if omitted else {}),
            "tip": "Verify target groups and DNS before deleting a load balancer.",
        }
    except Exception as e:
        return {"error": str(e)}


@_srv.mcp.tool()
async def get_s3_incomplete_multipart_uploads(
    older_than_days: int = 7,
) -> dict:
    """
    Find S3 buckets with incomplete multipart uploads older than the threshold.

    Incomplete uploads accumulate silently at STANDARD storage rates ($0.023/GB-month).
    The fix is a single S3 lifecycle rule per bucket. This tool shows which buckets
    need it and how much wasted storage they hold.

    Args:
        older_than_days: Flag uploads older than this many days (default 7).

    Examples:
        - "Which S3 buckets have incomplete multipart uploads?"
        - "Find wasted S3 storage from incomplete uploads"
        - "How much are we paying for failed S3 uploads?"
    """
    try:
        import boto3
        from ..analyzers.waste import check_s3_incomplete_multipart

        s3 = boto3.client("s3", region_name="us-east-1")
        findings = check_s3_incomplete_multipart(s3, older_than_days=older_than_days)
        findings.sort(key=lambda x: x.get("estimated_monthly_savings", 0), reverse=True)
        total_savings = sum(f.get("estimated_monthly_savings", 0) for f in findings)

        kept, omitted = _srv.fit_to_budget(findings)
        return {
            "count": len(findings),
            "total_monthly_savings": round(total_savings, 2),
            "total_annual_savings": round(total_savings * 12, 2),
            "findings": kept,
            **({"findings_truncated": True, "hint": f"Showing {len(kept)} of {len(findings)} findings (highest savings first) to stay within token budget."} if omitted else {}),
            "tip": (
                "Fix: add an S3 lifecycle rule with "
                "AbortIncompleteMultipartUpload DaysAfterInitiation=7 to each flagged bucket."
            ),
        }
    except Exception as e:
        return {"error": str(e)}


@_srv.mcp.tool()
async def get_ecr_cleanup_recommendations(
    older_than_days: int = 90,
    regions: list[str] | None = None,
) -> dict:
    """
    Find ECR repositories with old untagged container images consuming storage.

    ECR charges $0.10/GB-month for images beyond the free 500MB per repo.
    Untagged images from old CI builds accumulate quickly. The fix is an ECR
    lifecycle policy that auto-expires untagged images.

    Args:
        older_than_days: Flag untagged images older than this many days (default 90).
        regions: AWS regions to scan. Defaults to all opted-in regions.

    Examples:
        - "Which ECR repos have old images wasting storage?"
        - "Find container image cleanup opportunities"
        - "How much are old ECR images costing us?"
    """
    try:
        import boto3
        from ..analyzers.waste import check_ecr_old_images

        if regions is None:
            try:
                ec2g = boto3.client("ec2", region_name="us-east-1")
                resp = ec2g.describe_regions(
                    Filters=[{"Name": "opt-in-status", "Values": ["opt-in-not-required", "opted-in"]}]
                )
                regions = [r["RegionName"] for r in resp.get("Regions", [])]
            except Exception:
                regions = ["us-east-1", "us-west-2", "eu-west-1"]

        all_findings: list[dict] = []
        for region in regions:
            try:
                ecr = boto3.client("ecr", region_name=region)
                findings = check_ecr_old_images(ecr, region, older_than_days=older_than_days)
                all_findings.extend(findings)
            except Exception as exc:
                _srv.log.warning("ECR scan failed for region %s: %s", region, exc)

        all_findings.sort(key=lambda x: x.get("estimated_monthly_savings", 0), reverse=True)
        total_savings = sum(f.get("estimated_monthly_savings", 0) for f in all_findings)

        kept, omitted = _srv.fit_to_budget(all_findings, max_tokens=6000)
        result = {
            "count": len(all_findings),
            "total_monthly_savings": round(total_savings, 2),
            "total_annual_savings": round(total_savings * 12, 2),
            "regions_scanned": regions,
            "findings": kept,
            "tip": (
                "Fix: add an ECR lifecycle policy with rule "
                "tagStatus=untagged, countType=sinceImagePushed, countNumber=14 to each repo."
            ),
        }
        if omitted:
            result["findings_truncated"] = omitted
            result["hint"] = f"{omitted} smaller findings omitted to save tokens; total reflects all."
        return result
    except Exception as e:
        return {"error": str(e)}


@_srv.mcp.tool()
async def get_ecs_rightsizing_recommendations(
    cpu_threshold: float = 20.0,
    regions: list[str] | None = None,
) -> dict:
    """
    Find ECS Fargate services with over-provisioned CPU allocations.

    Uses Container Insights CpuUtilized metric. Services using less than
    cpu_threshold% of their allocated vCPUs are candidates for downsizing.
    Fargate billing is per vCPU-hour, so reducing allocation directly cuts cost.

    Requires Container Insights to be enabled on the ECS cluster.

    Args:
        cpu_threshold: Flag services with average CPU below this % (default 20%).
        regions: AWS regions to scan. Defaults to all opted-in regions.

    Examples:
        - "Which ECS Fargate services are over-provisioned?"
        - "Find oversized ECS tasks we can right-size"
        - "How much could we save by reducing Fargate CPU allocations?"
    """
    try:
        import boto3
        from ..analyzers.waste import check_ecs_task_rightsizing

        if regions is None:
            try:
                ec2g = boto3.client("ec2", region_name="us-east-1")
                resp = ec2g.describe_regions(
                    Filters=[{"Name": "opt-in-status", "Values": ["opt-in-not-required", "opted-in"]}]
                )
                regions = [r["RegionName"] for r in resp.get("Regions", [])]
            except Exception:
                regions = ["us-east-1", "us-west-2", "eu-west-1"]

        all_findings: list[dict] = []
        for region in regions:
            try:
                ecs = boto3.client("ecs", region_name=region)
                cw = boto3.client("cloudwatch", region_name=region)
                findings = check_ecs_task_rightsizing(ecs, cw, region, cpu_threshold_pct=cpu_threshold)
                all_findings.extend(findings)
            except Exception as exc:
                _srv.log.warning("ECS rightsizing scan failed for region %s: %s", region, exc)

        all_findings.sort(key=lambda x: x.get("estimated_monthly_savings", 0), reverse=True)
        total_savings = sum(f.get("estimated_monthly_savings", 0) for f in all_findings)

        pricing = await _price_on_customer_rates(all_findings, resource_type="ecs")

        kept, omitted = _srv.fit_to_budget(all_findings, max_tokens=6000)
        result = {
            "count": len(all_findings),
            "total_monthly_savings": round(total_savings, 2),
            "total_annual_savings": round(total_savings * 12, 2),
            "adjusted_monthly_savings": pricing.get("adjusted_total_monthly_savings"),
            "pricing_basis": pricing,
            "regions_scanned": regions,
            "findings": kept,
            "tip": (
                "Enable Container Insights on your ECS clusters for CPU data: "
                "aws ecs update-cluster-settings --cluster <name> "
                "--settings name=containerInsights,value=enabled"
            ),
        }
        if omitted:
            result["findings_truncated"] = omitted
            result["hint"] = f"{omitted} smaller findings omitted to save tokens; total reflects all."
        return result
    except Exception as e:
        return {"error": str(e)}


@_srv.mcp.tool()
async def list_idle_resources(
    resource_types: list[str] | None = None,
    regions: list[str] | None = None,
    min_idle_days: int = 7,
) -> dict:
    """
    Scan for idle/wasted AWS resources that are costing money but doing nothing.

    Finds: unattached EBS volumes, unused Elastic IPs, old snapshots with no AMI
    dependency, stopped EC2 instances (still paying for EBS), load balancers
    with no healthy targets. That is the full scope: this tool does not look at
    RDS, DocumentDB, Kendra, or Textract, so a small or zero total here is not a
    clean bill of health for the account, only for these five resource types.
    Use get_idle_rds_instances, get_documentdb_costs, audit_textract_environment_waste,
    or run_full_cost_audit for those.

    Results are sorted by monthly waste descending. Protected resources
    (tagged env=prod, protected=true, etc.) are flagged but never acted on.

    Examples:
        - "Find idle resources wasting money in AWS"
        - "List any unattached EBS volumes older than 90 days"
        - "What stopped EC2 instances are we still paying for?"
    Args:
        resource_types: Subset to scan, e.g. ["ebs", "eip", "nat"]. All types when omitted.
        regions: AWS regions to scan. Defaults to all enabled regions.
        min_idle_days: Only report resources idle at least this many days.

    """
    try:
        from ..cleanup.idle import scan_idle_resources, idle_resources_summary
        resources = await _srv.asyncio.to_thread(
            scan_idle_resources,
            resource_types=resource_types,
            regions=regions,
            min_idle_days=min_idle_days,
        )

        # Persist for savings tracking
        try:
            from ..recommendations.savings_tracker import record_recommendation
            for r in resources:
                if r.monthly_cost_usd > 0:
                    record_recommendation(
                        source="idle",
                        provider="aws",
                        resource_id=r.resource_id,
                        resource_type=r.resource_type,
                        resource_name=r.name,
                        account_id=r.account_id,
                        region=r.region,
                        current_config={"resource_type": r.resource_type, "idle_days": r.idle_days},
                        recommended_config={"action": "delete_or_release"},
                        description=r.reason,
                        estimated_monthly_savings_usd=r.monthly_cost_usd,
                    )
        except Exception:
            pass

        return idle_resources_summary(resources)
    except Exception as e:
        return {"error": str(e)}


@_srv.mcp.tool()
async def cleanup_idle_resources(
    resource_ids: list[str] | None = None,
    resource_types: list[str] | None = None,
    regions: list[str] | None = None,
    min_idle_days: int = 7,
    dry_run: bool = True,
) -> dict:
    """
    Propose cleanup for idle AWS resources. nable never deletes anything: this
    returns the resources, the dollars, and the exact command for each, and the
    operator runs them. There is no mode in which this tool executes.

    Examples:
        - "What can I safely delete to save money?"
        - "Show me idle Elastic IPs in us-east-1 and how to release them"
        - "Which stopped instances have been idle for two weeks?"
    Args:
        resource_ids: Explicit resource ids to include. Omit to include everything found.
        resource_types: Idle resource types to include, e.g. ["ebs", "eip"].
        regions: AWS regions to scan. Defaults to all enabled regions.
        min_idle_days: Only include resources idle at least this many days.
        dry_run: Ignored. Kept so existing callers do not break; nothing executes.

    """
    if err := _srv.require_role("admin"):
        return err
    try:
        from ..cleanup.actions import cleanup_resources
        return cleanup_resources(
            resource_ids=resource_ids or [],
            dry_run=dry_run,
            resource_types=resource_types,
            regions=regions,
            min_idle_days=min_idle_days,
        )
    except Exception as e:
        return {"error": str(e)}


@_srv.mcp.tool()
async def open_rightsizing_pr(
    tf_dir: str,
    github_repo: str | None = None,
    recommendation_ids: list[int] | None = None,
    resource_overrides: list[dict] | None = None,
    branch: str = "fix/rightsizing",
    base_branch: str = "main",
    pr_title: str | None = None,
    dry_run: bool = False,
    patch_only: bool = False,
) -> dict:
    """
    Apply rightsizing recommendations to Terraform source, optionally opening a GitHub PR.

    nable reads your Terraform state (terraform.tfstate or `terraform show -json`) to
    automatically resolve AWS instance IDs to their Terraform resource addresses. No
    manual mapping needed as long as your tf_dir has state available.

    Resolution order:
      1. Terraform state (automatic, reads instance IDs from state)
      2. resource_overrides (manual fallback if state is unavailable)
      3. recommended_config stored in DB

    Modes:
      dry_run=True    Show diffs only. Nothing written to disk.
      patch_only=True Write .tf files locally. No git, no PR. Use your own workflow.
      default         Write files, commit to a branch, push, open GitHub PR.

    After merging and running `terraform apply`, nable auto-verifies savings by
    checking AWS and updates the recommendation to "verified".

    Args:
        tf_dir:              Path to the Terraform working directory.
        github_repo:         "owner/repo" for GitHub PR. Not needed for dry_run or patch_only.
        recommendation_ids:  Specific rec IDs to act on. Omit to process all open rightsizing recs.
        resource_overrides:  Manual fallback if state resolution fails.
                             Format: [{"recommendation_id": 42, "tf_resource_type": "aws_instance",
                                       "tf_resource_name": "api_server"}, ...]
        branch:              Branch to create. Defaults to "fix/rightsizing".
        base_branch:         PR target branch. Defaults to "main".
        pr_title:            PR title. Auto-generated from saving amount if omitted.
        dry_run:             Show diffs without writing files or creating the PR.
        patch_only:          Patch files locally, skip git and GitHub.

    Examples:
        - "Show me what the rightsizing changes would look like"
        - "Apply the rightsizing fixes to my Terraform repo"
        - "Open a rightsizing PR against acme/infra"
        - "Patch the Terraform files but don't create a PR, I'll handle the git flow"
    """
    if (err := _srv.require_pro("remediation")):
        return err
    if err := _srv.require_role("analyst"):
        return err

    # Opening a PR pushes a branch to the customer's repo: gate it behind the
    # opt-in switch. dry_run (diff only) and patch_only (local files, no git)
    # touch nothing outside the box, so they stay available while disabled.
    if not dry_run and not patch_only:
        from ..remediation.gate import remediation_pr_enabled, disabled_response
        if not remediation_pr_enabled():
            return disabled_response(dry_run_hint=True)

    safe_dir = _srv._resolve_safe_path(tf_dir, must_exist=True)
    if isinstance(safe_dir, dict):
        return safe_dir
    tf_dir = safe_dir

    from ..remediation.rightsizing_pr import open_rightsizing_pr as _open_pr
    return _open_pr(
        tf_dir=tf_dir,
        github_repo=github_repo,
        recommendation_ids=recommendation_ids,
        resource_overrides=resource_overrides,
        branch=branch,
        base_branch=base_branch,
        pr_title=pr_title,
        dry_run=dry_run,
        patch_only=patch_only,
    )


@_srv.mcp.tool()
async def scan_waste_patterns(
    account_id: str | None = None,
    min_monthly_waste: float = 20.0,
    categories: str | None = None,
) -> dict:
    """
    Scan for cloud cost waste patterns using nable's proprietary pattern library.

    Runs 13 waste fingerprints across compute, storage, database, network, AI,
    and governance categories. Each finding includes confidence score, monthly
    waste estimate, and specific remediation steps.

    Args:
        account_id:        AWS account ID to scan. Auto-discovered from the
                           connected AWS account when omitted.
        min_monthly_waste: only return findings above this monthly USD threshold
        categories:        comma-separated filter e.g. "compute,storage" (omit for all)

    Returns structured findings sorted by monthly waste descending, with
    total_monthly_waste and total_annual_waste summary.
    Examples:
        - "Scan for waste patterns"
        - "Any recurring waste in this account?"

    """
    account_id = await _srv._resolve_account_id(account_id)
    if not account_id:
        return {"error": "No account_id provided and none could be auto-discovered.",
                "hint": "Connect AWS with `finops setup aws`, or pass account_id explicitly."}
    try:
        from ..ml.patterns import PatternContext, scan_dict
        from ..storage.db import get_engine
        from sqlalchemy import text as sql_text

        engine = get_engine()
        cat_list = [c.strip() for c in categories.split(",")] if categories else None

        # Pull daily cost series per service (last 90 days). Compute the cutoff in
        # Python and bind it: date('now', ...) is SQLite-only and raises on Postgres,
        # which is the shared-team mode the Startups tier sells.
        from datetime import date as _date_cls, timedelta as _td
        _cutoff = (_date_cls.today() - _td(days=90)).isoformat()
        with engine.connect() as conn:
            rows = conn.execute(sql_text("""
                SELECT service, snapshot_date, SUM(amount_usd) as total
                FROM cost_snapshots
                WHERE account_id = :aid
                  AND snapshot_date >= :cutoff
                GROUP BY service, snapshot_date
                ORDER BY service, snapshot_date
            """), {"aid": account_id, "cutoff": _cutoff}).fetchall()

        daily_costs: dict[str, list[float]] = {}
        for service, _date, total in rows:
            daily_costs.setdefault(service, []).append(float(total))

        ctx = PatternContext(
            daily_costs=daily_costs,
            by_resource=[],
            snapshots=[],
            account_id=account_id,
        )

        result = scan_dict(ctx, min_monthly_waste=min_monthly_waste, categories=cat_list)
        result["account_id"] = account_id
        result["note"] = (
            "Findings based on cost time-series only. "
            "Connect EC2/RDS/Lambda metadata for higher-confidence results."
        )
        return result
    except Exception as e:
        return {"error": str(e)}


@_srv.mcp.tool()
async def get_documentdb_costs(days: int = 30, account: str = "") -> str:
    """
    Analyze Amazon DocumentDB costs by cluster, with rightsizing recommendations.

    Pulls Cost Explorer spend, breaks down compute vs storage, and checks
    CloudWatch CPU utilization to flag clusters that can be downsized.

    Args:
        days:    Number of days to analyze (default 30).
        account: Reserved for future multi-account support.
    Examples:
        - "DocumentDB costs for the last 30 days"

    """
    try:
        from ..connectors.aws_services.documentdb import DocumentDBAnalyzer
        region = _srv.os.getenv("AWS_DEFAULT_REGION", "us-east-1")
        analyzer = DocumentDBAnalyzer(region=region)
        return analyzer.get_costs(days=days)
    except Exception as e:
        return f"DocumentDB cost analysis unavailable: {e}"


@_srv.mcp.tool()
async def get_kendra_costs(account: str = "") -> str:
    """
    Analyze Amazon Kendra costs by index, with edition and usage flags.

    Lists all Kendra indexes, their edition (DEVELOPER vs ENTERPRISE),
    monthly cost, query volume, and cost per query. Flags indexes that are
    oversized for their query volume or appear unused.

    Args:
        account: Reserved for future multi-account support.
    Examples:
        - "What is Amazon Kendra costing us?"

    """
    try:
        from ..connectors.aws_services.kendra import KendraAnalyzer
        region = _srv.os.getenv("AWS_DEFAULT_REGION", "us-east-1")
        analyzer = KendraAnalyzer(region=region)
        return analyzer.get_costs()
    except Exception as e:
        return f"Kendra cost analysis unavailable: {e}"


@_srv.mcp.tool()
async def get_textract_costs(days: int = 30, account: str = "") -> str:
    """
    Analyze AWS Textract costs by API type (sync vs async).

    Breaks down Textract spend by usage type and flags high-cost sync API
    usage where async alternatives would reduce cost by up to 96%.

    Args:
        days:    Number of days to analyze (default 30).
        account: Reserved for future multi-account support.
    Examples:
        - "Textract spend this month"
        - "What are we paying for OCR?"

    """
    try:
        from ..connectors.aws_services.textract import TextractAnalyzer
        analyzer = TextractAnalyzer()
        return analyzer.get_costs(days=days)
    except Exception as e:
        return f"Textract cost analysis unavailable: {e}"


@_srv.mcp.tool()
async def audit_textract_environment_waste(days: int = 30) -> dict:
    """
    Analyzes Textract spend by environment to find non-production API calls.
    Textract charges per page, QA and staging environments often call it
    unnecessarily. Identifies which Lambda functions or services are calling
    Textract in non-prod and estimates the monthly waste.

    Use this when:
        - Textract is a top cost driver
        - User asks about AI/ML service costs
        - User asks why their Textract bill is high
        - User wants to reduce document processing costs

    Args:
        days: Number of days to analyze (default 30).
    Examples:
        - "Is non-prod Textract usage wasting money?"

    """
    try:
        from ..recommendations.textract_env import scan_textract_environment_waste
        region = _srv.os.getenv("AWS_DEFAULT_REGION", "us-east-1")
        return scan_textract_environment_waste(days=days, region=region)
    except Exception as e:
        return {"error": f"Textract environment audit unavailable: {e}"}


@_srv.mcp.tool()
async def audit_duplicate_spend(days: int = 30) -> dict:
    """
    Find places where you're paying for the same capability through two
    different services or providers at once, the kind of waste a plain cost
    breakdown never surfaces because every line item looks legitimate on its
    own. Covers three patterns: multiple LLM inference paths active at the
    same time (e.g. AWS Bedrock Claude AND a direct Anthropic API key),
    multiple managed search/retrieval services (Kendra + OpenSearch), and
    two data platforms at once (Databricks + Snowflake).

    This is a "worth a look" flag, not a claim that anything is wasted:
    running two providers on purpose (failover, per-team routing) is a real
    pattern too. Every finding tells you exactly how to confirm it yourself.

    Use this when:
        - User asks what redundant or duplicate spend they have
        - User connects multiple LLM providers and wants a sanity check
        - User wants a second pass beyond idle-resource / rightsizing checks

    Args:
        days: Lookback window in days (default 30).

    Examples:
        - "Are we paying for the same thing twice?"
        - "Do I have duplicate or redundant cloud spend?"
        - "Am I running two LLM providers by accident?"
        - "Do I need both Databricks and Snowflake?"
    """
    from ..demo_data import is_demo, get_demo_response
    if is_demo():
        return get_demo_response("audit_duplicate_spend") or {}
    try:
        from ..recommendations.duplicate_capability import scan_duplicate_capabilities
        from ..connectors.llm_costs import get_all_llm_costs

        llm_data = await _srv.asyncio.to_thread(get_all_llm_costs, None, None, days)
        llm_by_provider = llm_data.get("by_provider") or {}

        aws_by_service: dict = {}
        aws = _srv.CLOUD_CONNECTORS.get("aws")
        if aws is not None and await aws.is_configured():
            ed = _srv.date.today()
            sd = ed - _srv.timedelta(days=days)
            summary = await aws.get_costs(sd, ed, granularity="MONTHLY")
            aws_by_service = summary.by_service or {}

        saas_summary = await _srv.get_cost_summary(category="saas")
        saas_by_provider = {
            k: v.get("total_usd", 0.0)
            for k, v in (saas_summary.get("by_provider") or {}).items()
        }

        findings = scan_duplicate_capabilities(
            llm_by_provider=llm_by_provider,
            aws_by_service=aws_by_service,
            saas_by_provider=saas_by_provider,
        )
        return {
            "window_days": days,
            "checked": {
                "llm_providers": sorted(k for k, v in llm_by_provider.items() if v),
                "aws_connected": bool(aws_by_service),
                "saas_providers": sorted(k for k, v in saas_by_provider.items() if v),
            },
            "finding_count": len(findings),
            "findings": [f.to_dict() for f in findings],
        }
    except Exception as e:
        return {"error": f"Duplicate-spend audit unavailable: {e}"}


@_srv.mcp.tool()
async def identify_nonprod_scheduling_opportunities(
    regions: list[str] | None = None,
    max_results: int = 50,
) -> str:
    """
    Finds non-production EC2 instances (dev/staging/test) running 24/7.
    Scheduling to business hours only saves 60-70% on compute costs.

    Args:
        regions: AWS regions to scan. Defaults to all opted-in regions.
        max_results: Max instances to return (default 50).

    Examples:
        - "Find non-prod instances we could schedule to save money"
        - "How much could we save by scheduling non-production environments?"
    """
    try:
        from ..recommendations.nonprod_scheduler import identify_nonprod_resources
        aws = _srv.CLOUD_CONNECTORS.get("aws")
        if aws is None or not await aws.is_configured():
            return "AWS is not connected. Call connect_aws right here in the chat (it detects credentials already on this machine), or run 'uvx nable' in a terminal."

        result = await identify_nonprod_resources(aws_client=aws, regions=regions)

        if "error" in result:
            return f"Error: {result['error']}"

        instances = result.get("schedulable_instances", [])
        total_waste = result.get("total_monthly_waste", 0.0)
        total = result.get("total_instances", 0)

        if total == 0:
            return (
                "No schedulable non-production instances found.\n"
                "Either there are no instances tagged dev/staging/test/qa/sandbox, "
                "or they are not significantly idle during off-hours."
            )

        shown = instances[:max_results]
        omitted = len(instances) - len(shown)

        lines = ["## Non-production Scheduling Opportunities", ""]
        lines.append(
            "| Instance | Type | Environment | Region | Idle hrs/wk | Monthly Cost | Monthly Saving |"
        )
        lines.append(
            "|----------|------|-------------|--------|-------------|--------------|----------------|"
        )
        for inst in shown:
            name_label = inst["name"] or inst["instance_id"]
            lines.append(
                f"| {name_label} ({inst['instance_id']}) "
                f"| {inst['instance_type']} "
                f"| {inst['environment']} "
                f"| {inst['region']} "
                f"| {inst['idle_hours_per_week']:.0f} "
                f"| ${inst['monthly_cost_estimate']:,.2f} "
                f"| ${inst['potential_monthly_savings']:,.2f} |"
            )

        if omitted > 0:
            lines.append(f"_Showing top {max_results} by savings. {omitted} more findings omitted._")

        lines.append("")
        lines.append(f"Estimated total monthly saving: ${total_waste:,.2f}")
        lines.append(
            "These instances are running 24/7 but appear idle nights and weekends."
        )
        lines.append(
            "Recommended schedule: Monday-Friday 08:00-18:00 UTC "
            "(50 hrs/wk vs 168 hrs/wk currently)."
        )
        lines.append("")
        lines.append("Next step: Use EventBridge Scheduler or AWS Instance Scheduler.")
        lines.append(
            "Each instance record includes an aws_scheduler_command with the CLI command to set this up."
        )

        nudge = _srv._team_nudge(
            f"To auto-create Jira, Linear, or GitHub tickets for these {total} "
            f"scheduling opportunities, upgrade to Pro:"
        , context="identify_nonprod_scheduling_opportunities")
        if nudge:
            lines.append("")
            lines.append(nudge)

        return "\n".join(lines)

    except Exception as e:
        _srv.log.error("identify_nonprod_scheduling_opportunities failed: %s", e, exc_info=True)
        return f"Error: {e}"


@_srv.mcp.tool()
async def audit_rds_manual_snapshots(
    regions: list[str] | None = None,
    age_threshold_days: int = 30,
) -> str:
    """
    Audits RDS manual snapshots for waste. Manual snapshots never auto-expire
    and cost $0.095/GB-month. Finds orphaned snapshots (source DB deleted)
    and old snapshots past the retention threshold.

    Args:
        regions: AWS regions to scan. Defaults to all opted-in regions.
        age_threshold_days: Flag snapshots older than this. Default: 30 days.

    Examples:
        - "Find orphaned RDS snapshots from deleted databases"
        - "How much are we paying for old RDS manual snapshots?"
    """
    try:
        from ..recommendations.rds_snapshots import audit_rds_manual_snapshots as _audit

        aws = _srv.CLOUD_CONNECTORS.get("aws")
        if aws is None or not await aws.is_configured():
            return "AWS is not connected. Call connect_aws right here in the chat (it detects credentials already on this machine), or run 'uvx nable' in a terminal."

        result = await _audit(
            aws_client=aws,
            regions=regions,
            age_threshold_days=age_threshold_days,
        )

        if "error" in result:
            return f"Error: {result['error']}"

        orphaned = result.get("orphaned_snapshots", [])
        old = result.get("old_snapshots", [])
        total_monthly = result.get("total_monthly_cost", 0.0)
        potential_savings = result.get("potential_monthly_savings", 0.0)
        total_snapshots = result.get("total_snapshots", 0)
        total_size_gb = result.get("total_size_gb", 0.0)

        if total_snapshots == 0:
            return "No manual RDS snapshots found across the scanned regions."

        lines = ["## RDS Manual Snapshot Audit", ""]
        lines.append(
            f"Found {total_snapshots} manual snapshots totalling {total_size_gb:.1f} GB "
            f"(${total_monthly:,.2f}/mo)."
        )
        lines.append(
            f"Potential saving if flagged snapshots are deleted: ${potential_savings:,.2f}/mo."
        )
        lines.append("")

        _SNAP_TABLE_CAP = 30

        if orphaned:
            orphaned = sorted(orphaned, key=lambda s: s.get("monthly_cost", 0.0), reverse=True)
            lines.append(f"### Orphaned Snapshots ({len(orphaned)}) - Source DB no longer exists")
            lines.append("")
            lines.append("| Snapshot ID | DB Identifier | Size (GB) | Age (days) | Monthly Cost |")
            lines.append("|-------------|---------------|-----------|------------|--------------|")
            for snap in orphaned[:_SNAP_TABLE_CAP]:
                lines.append(
                    f"| {snap['snapshot_id']} "
                    f"| {snap['db_identifier']} "
                    f"| {snap['size_gb']:.1f} "
                    f"| {snap['age_days']} "
                    f"| ${snap['monthly_cost']:,.4f} |"
                )
            if len(orphaned) > _SNAP_TABLE_CAP:
                _rest = orphaned[_SNAP_TABLE_CAP:]
                _rest_cost = sum(s.get("monthly_cost", 0.0) for s in _rest)
                lines.append(
                    f"_... and {len(_rest)} more orphaned snapshots, worth ${_rest_cost:,.2f}/mo total. "
                    f"Showing top {_SNAP_TABLE_CAP} by monthly cost. Scan a single region for full detail._"
                )
            lines.append("")

        if old:
            old = sorted(old, key=lambda s: s.get("monthly_cost", 0.0), reverse=True)
            lines.append(
                f"### Old Snapshots ({len(old)}) - Older than {age_threshold_days} days, source DB exists"
            )
            lines.append("")
            lines.append("| Snapshot ID | DB Identifier | Size (GB) | Age (days) | Monthly Cost |")
            lines.append("|-------------|---------------|-----------|------------|--------------|")
            for snap in old[:_SNAP_TABLE_CAP]:
                lines.append(
                    f"| {snap['snapshot_id']} "
                    f"| {snap['db_identifier']} "
                    f"| {snap['size_gb']:.1f} "
                    f"| {snap['age_days']} "
                    f"| ${snap['monthly_cost']:,.4f} |"
                )
            if len(old) > _SNAP_TABLE_CAP:
                _rest = old[_SNAP_TABLE_CAP:]
                _rest_cost = sum(s.get("monthly_cost", 0.0) for s in _rest)
                lines.append(
                    f"_... and {len(_rest)} more old snapshots, worth ${_rest_cost:,.2f}/mo total. "
                    f"Showing top {_SNAP_TABLE_CAP} by monthly cost. Scan a single region for full detail._"
                )
            lines.append("")

        if not orphaned and not old:
            lines.append(
                f"All {total_snapshots} snapshots are recent (under {age_threshold_days} days) "
                f"and their source DBs still exist. No immediate action needed."
            )

        lines.append(
            "To delete a snapshot: "
            "`aws rds delete-db-snapshot --db-snapshot-identifier <snapshot-id> --region <region>`"
        )

        nudge = _srv._team_nudge(
            "To auto-create Jira, Linear, or GitHub tickets for these snapshot findings, "
            "upgrade to Pro:"
        , context="rds_manual_snapshots")
        if nudge:
            lines.append("")
            lines.append(nudge)

        return "\n".join(lines)

    except Exception as e:
        _srv.log.error("audit_rds_manual_snapshots failed: %s", e, exc_info=True)
        return f"Error: {e}"


@_srv.mcp.tool()
async def scan_lambda_concurrency_waste(
    regions: list[str] | None = None,
) -> dict:
    """
    Scans Lambda functions with provisioned concurrency for waste. Provisioned
    concurrency costs money even when idle. Returns functions below 50% avg
    utilization with savings estimates.

    Args:
        regions: AWS regions to scan. Defaults to all common regions.

    Examples:
        - "Find Lambda functions with over-provisioned concurrency"
        - "How much are we wasting on idle Lambda concurrency?"
    """
    try:
        from ..recommendations.lambda_concurrency import scan_lambda_concurrency_waste as _scan

        aws = _srv.CLOUD_CONNECTORS.get("aws")
        if aws is None or not await aws.is_configured():
            return {"error": "AWS connector is not configured."}

        findings = await _scan(aws_client=aws, regions=regions)

        total_wasted = sum(f["wasted_monthly_cost"] for f in findings)
        findings.sort(key=lambda f: f.get("wasted_monthly_cost", 0) or 0, reverse=True)
        kept, omitted = _srv.fit_to_budget(findings)
        return {
            "findings": kept,
            "total_findings": len(findings),
            **({"findings_truncated": True, "hint": f"Showing top {len(kept)} of {len(findings)} by wasted cost to stay within token budget."} if omitted else {}),
            "total_wasted_monthly_cost": round(total_wasted, 4),
            "total_wasted_annual_cost": round(total_wasted * 12, 2),
            "note": (
                "Utilization data covers the last 14 days. "
                "Functions with no CloudWatch data are treated as fully idle."
            ),
        }
    except Exception as exc:
        _srv.log.error("scan_lambda_concurrency_waste failed: %s", exc, exc_info=True)
        return {"error": str(exc)}


@_srv.mcp.tool()
async def scan_s3_bucket_key_opportunities() -> dict:
    """
    Finds S3 buckets using KMS encryption without Bucket Keys enabled.
    Bucket Keys reduce KMS API calls by up to 99%. Returns affected buckets
    with the CLI command to fix each one.

    Examples:
        - "Find S3 buckets missing bucket keys"
        - "How much are we wasting on KMS calls from S3?"
    """
    try:
        from ..recommendations.s3_bucket_keys import scan_s3_bucket_key_opportunities as _scan

        aws = _srv.CLOUD_CONNECTORS.get("aws")
        if aws is None or not await aws.is_configured():
            return {"error": "AWS connector is not configured."}

        findings = await _scan(aws_client=aws)

        total_savings = sum(f["estimated_savings"] for f in findings)
        findings.sort(key=lambda f: f.get("estimated_savings", 0) or 0, reverse=True)
        kept, omitted = _srv.fit_to_budget(findings)
        return {
            "findings": kept,
            "total_findings": len(findings),
            **({"findings_truncated": True, "hint": f"Showing top {len(kept)} of {len(findings)} by estimated savings to stay within token budget."} if omitted else {}),
            "total_estimated_monthly_savings": round(total_savings, 4),
            "total_estimated_annual_savings": round(total_savings * 12, 2),
            "note": (
                "KMS call estimates use CloudWatch AllRequests metrics when available. "
                "When request metrics are absent the bucket is still listed but its "
                "savings are reported as unquantified (0) rather than an invented number. "
                "Enable S3 request metrics in CloudWatch for accurate estimates."
            ),
        }
    except Exception as exc:
        _srv.log.error("scan_s3_bucket_key_opportunities failed: %s", exc, exc_info=True)
        return {"error": str(exc)}


@_srv.mcp.tool()
async def audit_efs_cross_az_mounts(
    regions: list[str] | None = None,
) -> dict:
    """
    Finds EC2 instances mounting EFS from a different AZ. Cross-AZ mounts cost
    $0.02/GB in hidden transfer charges. Fix by adding a mount target per AZ.

    Args:
        regions: AWS regions to scan. Defaults to all common regions.

    Examples:
        - "Find EFS mounts crossing availability zones"
        - "Which EFS file systems are generating cross-AZ transfer costs?"
    """
    try:
        from ..recommendations.efs_cross_az import audit_efs_cross_az_mounts as _scan

        aws = _srv.CLOUD_CONNECTORS.get("aws")
        if aws is None or not await aws.is_configured():
            return {"error": "AWS connector is not configured."}

        findings = await _scan(aws_client=aws, regions=regions)

        total_cost = sum(f["estimated_monthly_cost"] for f in findings)
        findings.sort(key=lambda f: f.get("estimated_monthly_cost", 0) or 0, reverse=True)
        kept, omitted = _srv.fit_to_budget(findings)
        return {
            "findings": kept,
            "total_findings": len(findings),
            **({"findings_truncated": True, "hint": f"Showing top {len(kept)} of {len(findings)} by estimated cost to stay within token budget."} if omitted else {}),
            "total_estimated_monthly_cost": round(total_cost, 4),
            "total_estimated_annual_cost": round(total_cost * 12, 2),
            "note": (
                "Cross-AZ detection uses security group membership as a proxy for "
                "EFS connectivity. Transfer cost is estimated from CloudWatch I/O metrics."
            ),
        }
    except Exception as exc:
        _srv.log.error("audit_efs_cross_az_mounts failed: %s", exc, exc_info=True)
        return {"error": str(exc)}


@_srv.mcp.tool()
async def audit_nlb_cross_zone_costs(
    regions: list[str] | None = None,
) -> dict:
    """
    Finds NLBs with cross-zone load balancing enabled. Cross-zone LB charges
    $0.01/GB for cross-AZ traffic. Safe to disable when AZs have equal capacity.

    Args:
        regions: AWS regions to scan. Defaults to all common regions.

    Examples:
        - "Find NLBs generating cross-zone load balancing charges"
        - "How much are we spending on NLB cross-zone traffic?"
    """
    try:
        from ..recommendations.nlb_cross_zone import audit_nlb_cross_zone_costs as _scan

        aws = _srv.CLOUD_CONNECTORS.get("aws")
        if aws is None or not await aws.is_configured():
            return {"error": "AWS connector is not configured."}

        findings = await _scan(aws_client=aws, regions=regions)

        actionable = [
            f for f in findings
            if f["recommendation"] != "monitor_no_action_needed"
        ]
        total_cost = sum(f["estimated_cross_az_cost"] for f in findings)

        findings.sort(key=lambda f: f.get("estimated_cross_az_cost", 0) or 0, reverse=True)
        kept, omitted = _srv.fit_to_budget(findings)
        return {
            "findings": kept,
            **({"findings_truncated": True, "hint": f"Showing top {len(kept)} of {len(findings)} by cross-AZ cost to stay within token budget."} if omitted else {}),
            "total_findings": len(findings),
            "actionable_findings": len(actionable),
            "total_estimated_monthly_cross_az_cost": round(total_cost, 4),
            "total_estimated_annual_cross_az_cost": round(total_cost * 12, 2),
            "note": (
                "Cost estimate assumes 50% of NLB traffic crosses AZ boundaries. "
                "Disable cross-zone LB only when target groups have balanced capacity per AZ."
            ),
        }
    except Exception as exc:
        _srv.log.error("audit_nlb_cross_zone_costs failed: %s", exc, exc_info=True)
        return {"error": str(exc)}


@_srv.mcp.tool()
async def audit_s3_intelligent_tiering(
    regions: list[str] | None = None,
    max_results: int = 50,
) -> dict:
    """
    Finds S3 buckets using Intelligent-Tiering where the monitoring fee exceeds
    savings. IT costs $0.0025/1,000 objects, making it more expensive than
    S3 Standard for objects smaller than 128KB.

    Args:
        regions: Unused (S3 is global). Present for API consistency.
        max_results: Max buckets to return in findings (default 50).

    Examples:
        - "Find S3 buckets where Intelligent-Tiering costs more than it saves"
        - "Are we wasting money on S3 IT for small files?"
    """
    try:
        from ..recommendations.s3_intelligent_tiering import audit_s3_intelligent_tiering as _scan

        aws = _srv.CLOUD_CONNECTORS.get("aws")
        if aws is None or not await aws.is_configured():
            return {"error": "AWS connector is not configured."}

        findings = await _scan(aws_client=aws, regions=regions)

        waste_findings = [
            f for f in findings
            if f["recommendation"].startswith("LIKELY_WASTE")
        ]
        total_monitoring_cost = sum(
            f["monthly_monitoring_cost"] for f in findings
            if f["monthly_monitoring_cost"] is not None
        )

        shown = findings[:max_results]
        omitted = len(findings) - len(shown)
        note = (
            "Objects below 128KB cost more in IT monitoring fees than they save "
            "in storage tiering. Enable S3 bucket metrics for accurate object size data."
        )
        if omitted > 0:
            note += f" Showing top {max_results} by impact. {omitted} more findings omitted."

        return {
            "findings": shown,
            "total_it_buckets": len(findings),
            "likely_waste_buckets": len(waste_findings),
            "total_monthly_monitoring_cost": round(total_monitoring_cost, 4),
            "note": note,
        }
    except Exception as exc:
        _srv.log.error("audit_s3_intelligent_tiering failed: %s", exc, exc_info=True)
        return {"error": str(exc)}


@_srv.mcp.tool()
async def audit_spot_diversification(
    regions: list[str] | None = None,
) -> str:
    """
    Audits ASGs using spot for instance type diversification. ASGs with fewer
    than 3 types are HIGH_RISK. Best practice: 5+ types with capacity-optimized
    allocation to avoid correlated interruptions.

    Args:
        regions: AWS regions to scan. Defaults to all opted-in regions.

    Examples:
        - "Are our ASGs diversified enough for spot?"
        - "Which ASGs are at risk from spot interruptions?"
    """
    if err := _srv.require_role("analyst"):
        return str(err)

    try:
        from ..recommendations.spot_diversification import audit_spot_diversification as _audit

        results = _audit(regions=regions)

        if not results:
            scanned = ", ".join(regions) if regions else "all regions"
            return (
                f"No spot-using ASGs found in: {scanned}.\n"
                "Either no ASGs use spot instances, or no ASGs exist in the scanned regions."
            )

        high   = [r for r in results if r["risk_level"] == "HIGH_RISK"]
        medium = [r for r in results if r["risk_level"] == "MEDIUM_RISK"]
        ok     = [r for r in results if r["risk_level"] == "OK"]

        lines: list[str] = [
            f"**{len(results)} spot ASG(s) audited: "
            f"{len(high)} HIGH_RISK, {len(medium)} MEDIUM_RISK, {len(ok)} OK**",
            "",
            "| ASG Name | Region | Types | Instance Types | Strategy | Spot % | Risk |",
            "| --- | --- | --- | --- | --- | --- | --- |",
        ]

        # Sort detail most-important-first (riskiest ASGs at top), then cap rows.
        # Header counts above already reflect the FULL result set, so totals hold.
        ordered = high + medium + ok
        DETAIL_CAP = 30
        shown = ordered[:DETAIL_CAP]
        omitted = len(ordered) - len(shown)

        for r in shown:
            types_str = ", ".join(r["instance_types"]) if r["instance_types"] else "-"
            lines.append(
                f"| {r['asg_name']} "
                f"| {r['region']} "
                f"| {r['instance_types_count']} "
                f"| {types_str} "
                f"| {r['allocation_strategy']} "
                f"| {r['spot_pct']:.1f}% "
                f"| {r['risk_level']} |"
            )

        if omitted > 0:
            lines.append(
                f"| _... and {omitted} more lower-risk ASG(s)_ | | | | | | "
                f"_filter by region for full detail_ |"
            )

        if high or medium:
            lines += [
                "",
                "**How to fix:** Add instance types via MixedInstancesPolicy overrides. "
                "Use capacity-optimized allocation strategy. "
                "Target 5+ types across multiple families (m5, m6i, c5, r5, etc.) "
                "to avoid correlated interruptions.",
            ]

        return "\n".join(lines)

    except Exception as e:
        return f"Error auditing spot diversification: {e}"


@_srv.mcp.tool()
async def audit_cloudwatch_metric_cardinality(
    regions: list[str] | None = None,
) -> str:
    """
    Audits CloudWatch custom metric cardinality. Custom metrics above the 10,000
    free-tier threshold cost $0.30/metric/month. High-cardinality dimensions like
    pod_id or request_id can cause thousands of metrics per microservice.

    Args:
        regions: AWS regions to scan. Defaults to all opted-in regions.

    Examples:
        - "Which namespaces have too many custom metrics?"
        - "Find CloudWatch metrics costing us money"
    """
    if err := _srv.require_role("analyst"):
        return err
    try:
        from ..recommendations.cloudwatch_cardinality import audit_cloudwatch_metric_cardinality as _audit
        aws = _srv.CLOUD_CONNECTORS.get("aws")
        if aws is None:
            return "AWS is not connected. Call connect_aws right here in the chat (it detects credentials already on this machine), or run 'uvx nable' in a terminal."

        result = await _audit(aws, regions=regions)

        total = result["total_custom_metrics"]
        cost = result["estimated_monthly_cost"]
        findings = result["high_cardinality_namespaces"]

        lines: list[str] = ["## CloudWatch Custom Metric Cardinality Audit", ""]
        lines.append(f"Total custom metrics found: **{total:,}**")
        lines.append(f"Estimated monthly cost (above 10k free tier): **${cost:,.2f}**")
        lines.append("")

        if not findings:
            lines.append("No high-cardinality namespaces found (all under 100 metrics).")
            return "\n".join(lines)

        findings = sorted(findings, key=lambda f: f.get("estimated_monthly_cost", 0), reverse=True)
        TOP_N = 30
        shown = findings[:TOP_N]
        omitted = findings[len(shown):]

        lines.append(f"**High-cardinality namespaces** ({len(findings)} found):")
        lines.append("")
        lines.append("| Namespace | Metrics | Est. Monthly Cost | Problem Dimensions |")
        lines.append("|---|---|---|---|")
        for f in shown:
            dims = ", ".join(f["high_cardinality_dimensions"]) if f["high_cardinality_dimensions"] else "unknown"
            lines.append(
                f"| {f['namespace']} | {f['metric_count']:,} "
                f"| ${f['estimated_monthly_cost']:,.2f} | {dims} |"
            )
        if omitted:
            omitted_cost = sum(f.get("estimated_monthly_cost", 0) for f in omitted)
            lines.append(
                f"| ... and {len(omitted)} more namespaces "
                f"| | ${omitted_cost:,.2f} total | (sorted by cost; scan a single region for full detail) |"
            )
        lines.append("")
        lines.append("**Recommendations:**")
        lines.append("")
        for f in shown:
            lines.append(f"- {f['recommendation']}")
        if omitted:
            lines.append(f"- ... {len(omitted)} more namespace(s) omitted; showing top {TOP_N} by cost.")

        return "\n".join(lines)

    except Exception as e:
        _srv.log.error("audit_cloudwatch_metric_cardinality failed: %s", e, exc_info=True)
        return f"Error running CloudWatch cardinality audit: {e}"


@_srv.mcp.tool()
async def audit_cloudwatch_orphaned_alarms(
    regions: list[str] | None = None,
    max_results: int = 50,
) -> str:
    """
    Finds CloudWatch alarms on deleted resources. Standard alarms cost
    $0.10/month, composite $0.30/month. Terminated instances and deleted
    queues leave alarms stuck in INSUFFICIENT_DATA indefinitely.

    Args:
        regions: AWS regions to scan. Defaults to all opted-in regions.
        max_results: Max orphaned alarms to return (default 50).

    Examples:
        - "Find orphaned CloudWatch alarms"
        - "How much are we wasting on CloudWatch alarms?"
    """
    if err := _srv.require_role("analyst"):
        return err
    try:
        from ..recommendations.cloudwatch_alarms import audit_cloudwatch_orphaned_alarms as _audit
        aws = _srv.CLOUD_CONNECTORS.get("aws")
        if aws is None:
            return "AWS is not connected. Call connect_aws right here in the chat (it detects credentials already on this machine), or run 'uvx nable' in a terminal."

        result = await _audit(aws, regions=regions)

        total = result["total_alarms"]
        orphaned = result["orphaned_alarms"]
        waste = result["total_monthly_waste"]

        lines: list[str] = ["## CloudWatch Orphaned Alarm Audit", ""]
        lines.append(f"Total alarms scanned: **{total}**")
        lines.append(f"Likely orphaned alarms: **{len(orphaned)}**")
        lines.append(f"Monthly waste: **${waste:.2f}**")
        lines.append("")

        if not orphaned:
            lines.append("No orphaned alarms found.")
            return "\n".join(lines)

        shown = orphaned[:max_results]
        omitted = len(orphaned) - len(shown)

        lines.append("| Alarm | Namespace | Metric | State | Days in INSUFFICIENT_DATA | Monthly Cost | Resource Exists |")
        lines.append("|---|---|---|---|---|---|---|")
        for alarm in shown:
            resource_col = (
                "No" if alarm["resource_exists"] is False
                else "Yes" if alarm["resource_exists"] is True
                else "Unknown"
            )
            lines.append(
                f"| {alarm['alarm_name']} | {alarm['namespace']} "
                f"| {alarm['metric_name']} | {alarm['state']} "
                f"| {alarm['days_insufficient_data'] or 'N/A'} "
                f"| ${alarm['monthly_cost']:.2f} | {resource_col} |"
            )

        if omitted > 0:
            lines.append(f"_Showing top {max_results} by cost. {omitted} more findings omitted._")

        lines.append("")
        lines.append("To delete orphaned alarms (verify before running):")
        lines.append("```")
        by_region: dict[str, list[str]] = {}
        for alarm in shown:
            by_region.setdefault(alarm["region"], []).append(alarm["alarm_name"])
        for region, names in by_region.items():
            quoted = " ".join(f'"{n}"' for n in names)
            lines.append(f"aws cloudwatch delete-alarms --alarm-names {quoted} --region {region}")
        lines.append("```")

        return "\n".join(lines)

    except Exception as e:
        _srv.log.error("audit_cloudwatch_orphaned_alarms failed: %s", e, exc_info=True)
        return f"Error running CloudWatch alarm audit: {e}"


@_srv.mcp.tool()
async def audit_cloudwatch_logs_ia_opportunities(
    regions: list[str] | None = None,
) -> str:
    """
    Finds CloudWatch Log groups to migrate to Infrequent Access class. IA cuts
    ingestion cost 50% ($0.075 to $0.0375/GB). Candidates: groups older than
    30 days with >1 GB/month still on STANDARD. Note: IA does not support
    metric filters or subscription filters.

    Args:
        regions: AWS regions to scan. Defaults to all opted-in regions.

    Examples:
        - "Find CloudWatch log groups to migrate to Infrequent Access"
        - "How much can we save on CloudWatch log ingestion?"
    """
    if err := _srv.require_role("analyst"):
        return err
    try:
        from ..recommendations.cloudwatch_logs_ia import audit_cloudwatch_logs_ia_opportunities as _audit
        aws = _srv.CLOUD_CONNECTORS.get("aws")
        if aws is None:
            return "AWS is not connected. Call connect_aws right here in the chat (it detects credentials already on this machine), or run 'uvx nable' in a terminal."

        result = await _audit(aws, regions=regions)

        total_scanned = result["total_groups_scanned"]
        candidates = result["candidates"]
        total_savings = result["total_monthly_savings"]

        lines: list[str] = ["## CloudWatch Logs Infrequent Access Migration Audit", ""]
        lines.append(f"Log groups scanned: **{total_scanned}**")
        lines.append(f"IA migration candidates: **{len(candidates)}**")
        lines.append(f"Potential monthly savings: **${total_savings:,.2f}**")
        lines.append("")

        if not candidates:
            lines.append(
                "No candidates found. Either all log groups are already on IA class, "
                "ingesting less than 1 GB/month, or younger than 30 days."
            )
            return "\n".join(lines)

        lines.append("| Log Group | Ingestion (GB/mo) | Standard Cost | IA Cost | Savings | Retention |")
        lines.append("|---|---|---|---|---|---|")
        for c in candidates[:25]:  # cap table at 25 rows
            retention = f"{c['retention_days']}d" if c["retention_days"] else "infinite"
            lines.append(
                f"| {c['log_group_name']} "
                f"| {c['monthly_ingestion_gb']:.2f} "
                f"| ${c['monthly_cost_standard']:.4f} "
                f"| ${c['monthly_cost_ia']:.4f} "
                f"| ${c['monthly_savings']:.4f} "
                f"| {retention} |"
            )

        if len(candidates) > 25:
            lines.append(f"_...and {len(candidates) - 25} more_")

        lines.append("")
        lines.append(
            "**Before migrating:** confirm no metric filters or subscription filters "
            "exist on the log group. Check with: "
            "`aws logs describe-metric-filters --log-group-name <name>`"
        )

        return "\n".join(lines)

    except Exception as e:
        _srv.log.error("audit_cloudwatch_logs_ia_opportunities failed: %s", e, exc_info=True)
        return f"Error running CloudWatch Logs IA audit: {e}"


@_srv.mcp.tool()
async def audit_ebs_snapshot_replication(
    regions: list[str] | None = None,
    max_results: int = 50,
) -> dict:
    """
    Audits cross-region EBS snapshot replication for waste. Replicated snapshots
    cost $0.05/GB-month in each region. Finds orphaned copies (source volume
    deleted), excessive copies (more than 3 regions), and old copies where a
    newer copy exists.

    Args:
        regions: AWS regions to scan. Defaults to all opted-in regions.
        max_results: Max findings to return (default 50).

    Examples:
        - "Find orphaned cross-region EBS snapshots"
        - "How much are we spending on cross-region snapshot storage?"
    """
    try:
        from ..recommendations.ebs_snapshot_replication import (
            audit_ebs_snapshot_replication as _audit,
        )

        aws = _srv.CLOUD_CONNECTORS.get("aws")
        if aws is None or not await aws.is_configured():
            return {"error": "AWS is not connected. Call connect_aws right here in the chat (it detects credentials already on this machine), or run 'uvx nable' in a terminal."}

        result = await _audit(aws_client=aws, regions=regions)
        if "error" in result:
            return result

        findings = result.get("cross_region_findings", [])
        if len(findings) > max_results:
            omitted = len(findings) - max_results
            result["cross_region_findings"] = findings[:max_results]
            result["truncated"] = f"Showing top {max_results} by impact. {omitted} more findings omitted."

        return result

    except Exception as e:
        _srv.log.error("audit_ebs_snapshot_replication failed: %s", e, exc_info=True)
        return {"error": str(e)}


@_srv.mcp.tool()
async def audit_s3_transfer_acceleration() -> dict:
    """
    Finds S3 buckets with Transfer Acceleration enabled that won't benefit.
    TA adds $0.04-0.08/GB and is often forgotten. Flags buckets as waste if
    volume is under 1 GB/month, bucket is in us-east-1, or it is behind
    CloudFront. Returns a CLI disable command for each flagged bucket.

    Examples:
        - "Find S3 TA enabled buckets that don't need it"
        - "How much are we wasting on S3 Transfer Acceleration?"
    """
    try:
        from ..recommendations.s3_transfer_acceleration import (
            audit_s3_transfer_acceleration as _audit,
        )

        aws = _srv.CLOUD_CONNECTORS.get("aws")
        if aws is None or not await aws.is_configured():
            return {"error": "AWS is not connected. Call connect_aws right here in the chat (it detects credentials already on this machine), or run 'uvx nable' in a terminal."}

        result = await _audit(aws_client=aws)

        # Cap detail rows to bound token cost. Findings are pre-sorted
        # (likely_waste first, then monthly_ta_cost desc). Totals/counts are
        # separate top-level fields and are never trimmed.
        findings = result.get("findings")
        if isinstance(findings, list) and findings:
            kept, omitted = _srv.fit_to_budget(findings, max_tokens=6000)
            if omitted > 0:
                result["findings"] = kept
                result["findings_truncated"] = (
                    f"showing top {len(kept)} of {len(findings)} TA-enabled buckets "
                    f"by likely waste then monthly cost; totals above reflect all "
                    f"{len(findings)} buckets"
                )

        return result

    except Exception as e:
        _srv.log.error("audit_s3_transfer_acceleration failed: %s", e, exc_info=True)
        return {"error": str(e)}
