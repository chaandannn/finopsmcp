# SPDX-License-Identifier: Apache-2.0
"""commitments MCP tools (extracted from server.py; see finops/tools/__init__.py).

Server-local helpers, globals, and the mcp instance are reached through the live
server module (_srv.NAME) so monkeypatching finops.server.* still works and no
import-order coupling exists."""
from __future__ import annotations

from .. import server as _srv


@_srv.mcp.tool()
async def get_commitment_analysis() -> dict:
    """
    Analyze Reserved Instance and Savings Plan coverage, utilization, and waste.
    Coverage %, utilization, and waste figures are free.
    Purchase recommendations with $ amounts require a Pro plan (commitment_recommendations).

    Examples:
        - "How well are we using our Reserved Instances?"
        - "Should we buy more Savings Plans?"
        - "How much are we wasting on unused RIs?"
        - "What's our RI/SP coverage?"
    """
    try:
        from ..recommendations.commitments import analyze_commitments, commitment_summary
        analysis = analyze_commitments()
        if analysis is None:
            return {"error": "AWS is not connected. Call connect_aws right here in the chat (it detects credentials already on this machine), or run 'uvx nable' in a terminal."}
        result = commitment_summary(analysis)

        # Add actionable coverage gap analysis
        sp_cov = analysis.savings_plan_coverage_pct
        ri_cov = analysis.ri_coverage_pct
        # combined_coverage_pct, not (sp + ri) / 2: either half is None when Cost
        # Explorer denied that coverage call, and the arithmetic raised.
        combined_coverage = analysis.combined_coverage_pct or 0.0
        coverage_target = 80.0
        coverage_gap_pct = max(0.0, coverage_target - combined_coverage)

        # Monthly uncovered on-demand (3-month average)
        monthly_uncovered = analysis.uncovered_on_demand_usd / 3 if analysis.uncovered_on_demand_usd > 0 else 0.0

        actionable = {
            "combined_coverage_pct": round(combined_coverage, 1),
            "coverage_target_pct": coverage_target,
            "coverage_gap_pct": round(coverage_gap_pct, 1),
            "monthly_uncovered_on_demand_usd": round(monthly_uncovered, 2),
        }

        # "If you bought $X more in commitments" projection
        if monthly_uncovered > 100:
            # Compute SP covers eligible spend at ~34% discount (1yr no-upfront)
            _COMPUTE_SP_DISCOUNT_RATE = 0.34
            # Covering the full gap would require this hourly commitment
            additional_commitment = monthly_uncovered * 0.5  # cover 50% of gap as a sensible step
            projected_savings = additional_commitment * _COMPUTE_SP_DISCOUNT_RATE
            actionable["if_you_bought_more"] = {
                "description": (
                    f"Buying a 1-year no-upfront Compute Savings Plan at "
                    f"${additional_commitment:,.0f}/mo hourly commitment would cover "
                    f"~50% of your uncovered on-demand spend and save "
                    f"~${projected_savings:,.0f}/mo (${projected_savings * 12:,.0f}/yr)."
                ),
                "additional_monthly_commitment_usd": round(additional_commitment, 2),
                "projected_monthly_savings_usd": round(projected_savings, 2),
                "projected_annual_savings_usd": round(projected_savings * 12, 2),
            }

        # RI conversion opportunities (under-utilized RIs in wrong family)
        if analysis.ri_utilization_pct < 75 and analysis.ri_unused_usd > 50:
            actionable["ri_conversion_opportunity"] = (
                f"Your RIs are {analysis.ri_utilization_pct:.0f}% utilized, wasting "
                f"${analysis.ri_unused_usd:,.0f}/mo. Convert unused RI capacity to a "
                f"different instance size within the same family (e.g. 2x m5.large -> 1x m5.xlarge) "
                f"via the AWS console, or list on the RI Marketplace."
            )

        result["actionable_analysis"] = actionable

        # Strip purchase recommendations on free tier -- coverage/utilization/waste stays free
        if _srv.require_pro("commitment_recommendations") is not None:
            result["recommendations"] = [
                r for r in result.get("recommendations", []) if r.get("type") == "warning"
            ]
            result["recommendations_note"] = (
                f"This is a Team feature ($25/mo). Upgrade at {_srv._UPGRADE_URL} to unlock purchase recommendations with ROI projections."
            )
        return result
    except Exception as e:
        return {"error": str(e)}


@_srv.mcp.tool()
async def get_commitment_coverage_by_tag(
    tag_key: str,
    tag_value: str,
    tag_coverage_pct: float = 100.0,
) -> dict:
    """
    Estimate RI/SP commitment coverage for a specific tag slice,
    even when tagging is incomplete.

    At 70% tag coverage we measure the tagged resources directly via
    Cost Explorer, then solve algebraically for the untagged 30% using
    account totals, producing a full-domain estimate with confidence rating.

    Args:
        tag_key:          Tag key to filter on (e.g. "domain", "team", "service")
        tag_value:        Tag value (e.g. "payments", "platform", "checkout-api")
        tag_coverage_pct: How complete the tagging is for this domain (0–100).
                          If unknown, leave at 100 and interpret results as
                          lower bounds only.

    Examples:
        - "What's the RI coverage for the payments domain? Tags are about 70% complete"
        - "How covered is team=platform under Savings Plans?"
        - "Estimate commitment coverage for env=prod with 85% tag coverage"
    """
    try:
        from ..recommendations.commitments import estimate_coverage_for_partial_tag

        result = estimate_coverage_for_partial_tag(
            tag_key=tag_key,
            tag_value=tag_value,
            tag_coverage_pct=tag_coverage_pct,
        )

        if not result:
            return {"error": "Could not fetch coverage data. Ensure AWS Cost Explorer is enabled."}

        is_partial = tag_coverage_pct < 95

        out: dict = {
            "tag": f"{tag_key}={tag_value}",
            "tag_coverage_pct": tag_coverage_pct,
            "confidence": result.confidence,
            "confidence_note": result.confidence_note,

            # What we can measure directly
            "directly_measured": {
                "tagged_spend_usd": result.tagged_spend_usd,
                "sp_coverage_pct": result.tagged_sp_coverage_pct,
                "ri_coverage_pct": result.tagged_ri_coverage_pct,
                "note": f"Covers {tag_coverage_pct:.0f}% of resources with {tag_key}={tag_value}",
            },
        }

        if is_partial:
            # Surface the residual inference
            out["inferred_untagged"] = {
                "untagged_spend_usd": result.untagged_spend_usd,
                "inferred_sp_coverage_pct": result.inferred_untagged_sp_coverage_pct,
                "inferred_ri_coverage_pct": result.inferred_untagged_ri_coverage_pct,
                "note": (
                    f"Inferred from account totals for the {100 - tag_coverage_pct:.0f}% "
                    f"of resources without the {tag_key} tag"
                ),
            }
            out["full_domain_estimate"] = {
                "sp_coverage_pct": result.estimated_sp_coverage_pct,
                "ri_coverage_pct": result.estimated_ri_coverage_pct,
                "combined_coverage_pct": result.estimated_combined_coverage_pct,
                "note": "Weighted blend of measured + inferred",
            }

        coverage = result.estimated_combined_coverage_pct if is_partial else (
            (result.tagged_sp_coverage_pct + result.tagged_ri_coverage_pct) / 2
        )

        if coverage < 30:
            assessment = f"Low coverage: ${result.tagged_spend_usd:,.0f}/month largely at on-demand rates"
        elif coverage < 60:
            assessment = "Moderate coverage: meaningful SP/RI opportunity remains"
        else:
            assessment = "Good coverage"

        out["summary"] = (
            f"{tag_key}={tag_value}: ~{coverage:.0f}% commitment coverage "
            f"({result.confidence} confidence). {assessment}. "
            + (f"Tagging is {tag_coverage_pct:.0f}% complete. "
               f"Improving to 90%+ will give a high-confidence number."
               if tag_coverage_pct < 90 else "")
        )

        return out

    except Exception as e:
        _srv.log.exception("Commitment coverage by tag failed")
        return {"error": str(e)}


@_srv.mcp.tool()
async def get_ri_waste_detail(
    start_date: str | None = None,
    end_date: str | None = None,
    min_waste_usd: float = 10.0,
) -> dict:
    """
    Identify wasted Reserved Instance spend from CUR RIFee line items.

    Shows which reservations have low utilization and how much money is being
    wasted on unused reserved capacity. Requires CUR via Athena. Pro plan feature.

    Args:
        start_date: ISO date (YYYY-MM-DD). Defaults to 30 days ago.
        end_date: ISO date. Defaults to today.
        min_waste_usd: Minimum wasted dollars to include a reservation (default $10).

    Examples:
        - "Which Reserved Instances are underutilized?"
        - "How much are we wasting on unused RIs?"
        - "Show RI waste for this quarter"
    """

    sd, ed = _srv._default_dates()
    if start_date:
        sd = _srv.date.fromisoformat(start_date)
    if end_date:
        ed = _srv.date.fromisoformat(end_date)

    try:
        from ..connectors.cur import get_ri_waste
        result = get_ri_waste(start_date=sd, end_date=ed, min_waste_usd=min_waste_usd)
        if isinstance(result, dict) and isinstance(result.get("reservations"), list):
            reservations = result["reservations"]
            # Connector already sorts by wasted_usd desc; sort defensively.
            reservations.sort(key=lambda r: r.get("wasted_usd", 0), reverse=True)
            total_count = len(reservations)
            kept, omitted = _srv.fit_to_budget(reservations, max_tokens=6000)
            result["reservations"] = kept
            result["total_reservations"] = total_count
            if omitted > 0:
                result["reservations_truncated"] = omitted
                result["hint"] = (
                    f"showing top {len(kept)} of {total_count} underutilized reservations "
                    f"by wasted spend; total_wasted_usd covers all {total_count}. "
                    "Raise min_waste_usd or narrow the date range for fewer rows."
                )
        return result
    except Exception as exc:
        _srv.log.error("get_ri_waste_detail failed: %s", exc)
        return {"error": str(exc)}


@_srv.mcp.tool()
async def get_savings_plan_showback(
    tag_key: str = "team",
    start_date: str | None = None,
    end_date: str | None = None,
    include_ri: bool = True,
) -> dict:
    """
    Show exactly how much each team saved from Savings Plans and Reserved Instances.

    This is the showback problem no other tool solves at line-item granularity.
    Instead of blending SP/RI discounts across the account, nable attributes the
    real dollar benefit back to the team or service that consumed the covered usage,    using CUR fields that Cost Explorer doesn't expose.

    For each team (or tag value):
      • effective_cost    , what they actually paid under SP/RI rates
      • on_demand_equiv   , what they would have paid without commitments
      • savings_captured  , real dollar benefit from Savings Plans + RIs
      • discount_rate_pct , their effective discount rate
      • sp_savings / ri_savings, broken out by commitment type

    Requires CUR delivery to S3 and Athena. Pro plan feature.

    Args:
        tag_key:    Resource tag to group by, "team", "project", "env" (default "team")
        start_date: ISO date YYYY-MM-DD (default: start of current month)
        end_date:   ISO date YYYY-MM-DD (default: today)
        include_ri: Include Reserved Instance savings alongside SP savings (default True)

    Examples:
        - "Show me savings plan showback by team this month"
        - "How much did the payments team save from our savings plans?"
        - "What's the effective discount rate per team from our commitments?"
        - "Which team is getting the most benefit from our reserved instances?"
    """

    sd, ed = _srv._default_dates()
    if start_date:
        sd = _srv.date.fromisoformat(start_date)
    if end_date:
        ed = _srv.date.fromisoformat(end_date)

    try:
        from ..connectors.cur import get_savings_plan_showback as _showback
        return _showback(
            start_date=sd,
            end_date=ed,
            tag_key=tag_key,
            include_ri=include_ri,
        )
    except Exception as exc:
        _srv.log.error("get_savings_plan_showback failed: %s", exc)
        return {"error": str(exc)}


@_srv.mcp.tool()
async def recommend_lambda_snapstart(
    regions: list[str] | None = None,
) -> dict:
    """
    Finds Java Lambda functions that should use SnapStart. SnapStart eliminates
    cold starts for free, replacing expensive provisioned concurrency.

    Args:
        regions: AWS regions to scan. Defaults to all common regions.

    Examples:
        - "Which Java Lambda functions should use SnapStart?"
        - "Find Lambda functions wasting money on provisioned concurrency"
    """
    try:
        from ..recommendations.lambda_snapstart import recommend_lambda_snapstart as _scan

        aws = _srv.CLOUD_CONNECTORS.get("aws")
        if aws is None or not await aws.is_configured():
            return {"error": "AWS connector is not configured."}

        findings = await _scan(aws_client=aws, regions=regions)

        replaceable = [f for f in findings if f["has_provisioned_concurrency"]]
        total_replaceable_cost = sum(f["monthly_pc_cost"] for f in replaceable)

        findings.sort(key=lambda f: (bool(f.get("has_provisioned_concurrency")), f.get("monthly_pc_cost", 0) or 0), reverse=True)
        kept, omitted = _srv.fit_to_budget(findings)
        return {
            "findings": kept,
            **({"findings_truncated": True, "hint": f"Showing top {len(kept)} of {len(findings)} Java functions to stay within token budget."} if omitted else {}),
            "total_java_functions": len(findings),
            "functions_with_replaceable_pc": len(replaceable),
            "total_monthly_pc_cost_replaceable": round(total_replaceable_cost, 4),
            "total_annual_pc_cost_replaceable": round(total_replaceable_cost * 12, 2),
            "note": (
                "SnapStart is free. It caches a post-init snapshot and restores it "
                "on cold start, eliminating init latency without provisioned concurrency."
            ),
        }
    except Exception as exc:
        _srv.log.error("recommend_lambda_snapstart failed: %s", exc, exc_info=True)
        return {"error": str(exc)}


@_srv.mcp.tool()
async def scan_graviton_migration_opportunities(
    regions: list[str] | None = None,
) -> str:
    """
    Finds EC2 instances that can migrate to Graviton (arm64) for 20-40% savings.
    Returns ranked candidates with estimated monthly savings per instance.

    Args:
        regions: AWS regions to scan. Defaults to us-east-1.

    Examples:
        - "Which EC2 instances can we move to Graviton?"
        - "How much can we save by switching to arm64 instances?"
    """
    if err := _srv.require_role("analyst"):
        return str(err)

    try:
        from ..recommendations.graviton import scan_graviton_opportunities

        aws = _srv.CLOUD_CONNECTORS.get("aws")
        if aws is None or not await aws.is_configured():
            return "AWS is not connected. Call connect_aws right here in the chat (it detects credentials already on this machine), or run 'uvx nable' in a terminal."

        candidates = await scan_graviton_opportunities(aws, regions=regions)

        if not candidates:
            scanned = ", ".join(regions) if regions else "us-east-1"
            return (
                f"No Graviton migration candidates found in: {scanned}.\n"
                "All running x86_64 instances either already use Graviton-equivalent "
                "types or their instance type is not in the migration map."
            )

        total_savings = sum(r["savings_estimate"] for r in candidates)

        # Persist for savings tracking so a later migration can be verified read-only.
        try:
            from ..recommendations.savings_tracker import record_recommendation
            for r in candidates:
                if r.get("savings_estimate", 0) > 0:
                    record_recommendation(
                        source="graviton",
                        provider="aws",
                        resource_id=r["instance_id"],
                        resource_type="ec2",
                        resource_name=r.get("name_tag", "") or r["instance_id"],
                        account_id=r.get("account_id", ""),
                        region=r.get("region", ""),
                        current_config={
                            "instance_type": r["instance_type"],
                            "monthly_cost_usd": r.get("current_monthly_cost_estimate", 0.0),
                        },
                        recommended_config={
                            "graviton_equivalent": r["graviton_equivalent"],
                            "from_instance_type": r["instance_type"],
                            "estimated_monthly_savings_usd": r["savings_estimate"],
                        },
                        description=f"Migrate {r['instance_type']} to Graviton {r['graviton_equivalent']}",
                        estimated_monthly_savings_usd=r["savings_estimate"],
                    )
        except Exception:
            pass  # never block the main response

        lines: list[str] = [
            f"**{len(candidates)} instance{'s' if len(candidates) != 1 else ''} identified. "
            f"Estimated total monthly saving: ${total_savings:,.2f}**",
            "",
            "| Instance | Name | Type | Graviton Equivalent | Monthly Cost | Monthly Saving | Saving % |",
            "| --- | --- | --- | --- | --- | --- | --- |",
        ]

        for r in candidates:
            name = r["name_tag"] or r["instance_id"]
            lines.append(
                f"| {r['instance_id']} "
                f"| {name} "
                f"| {r['instance_type']} "
                f"| {r['graviton_equivalent']} "
                f"| ${r['current_monthly_cost_estimate']:,.2f} "
                f"| ${r['savings_estimate']:,.2f} "
                f"| {r['savings_pct']:.1f}% |"
            )

        lines += [
            "",
            "**How to migrate:** Most workloads (web servers, APIs, background workers) "
            "require only an instance type change and a reboot. Verify your AMI supports "
            "arm64 (Amazon Linux 2/2023 and Ubuntu 20.04+ are multi-arch). "
            "Test in staging before switching production.",
            "",
            _srv.cost_note("\n".join(lines), savings_found_usd=total_savings),
        ]

        nudge = _srv._team_nudge(
            f"You have {len(candidates)} Graviton migration "
            f"opportunit{'ies' if len(candidates) != 1 else 'y'} "
            f"worth ${total_savings:,.0f}/mo. To auto-create Jira, Linear, or GitHub "
            f"tickets so these actually get fixed, upgrade to Pro:"
        , context="scan_graviton_migration_opportunities")
        if nudge:
            lines += ["", nudge]

        return "\n".join(lines)

    except Exception as e:
        return f"Error scanning for Graviton opportunities: {e}"


@_srv.mcp.tool()
async def recommend_spot_adoption(
    regions: list[str] | None = None,
) -> str:
    """
    Finds on-demand EC2 instances to migrate to spot for 60-80% savings. Uses
    env tags, ASG membership, CPU variance, and Spot Advisor interruption data.
    Returns RECOMMENDED, POSSIBLE, or NOT_RECOMMENDED per instance.

    Args:
        regions: AWS regions to scan. Defaults to all opted-in regions.

    Examples:
        - "Which EC2 instances should we move to spot?"
        - "How much can we save by switching to spot instances?"
    """
    if err := _srv.require_role("analyst"):
        return str(err)

    try:
        from ..recommendations.spot_adoption import recommend_spot_adoption as _scan

        candidates = _scan(regions=regions)

        if not candidates:
            scanned = ", ".join(regions) if regions else "all regions"
            return (
                f"No spot adoption candidates found in: {scanned}.\n"
                "All running instances are already on spot, or no on-demand "
                "instances were found."
            )

        recommended = [c for c in candidates if c["recommendation"] == "RECOMMENDED"]
        possible     = [c for c in candidates if c["recommendation"] == "POSSIBLE"]
        total_savings = sum(c["monthly_savings"] for c in recommended + possible)

        lines: list[str] = [
            f"**{len(candidates)} on-demand instance(s) analyzed. "
            f"Potential spot savings: ${total_savings:,.2f}/mo "
            f"({len(recommended)} RECOMMENDED, {len(possible)} POSSIBLE)**",
            "",
            "| Instance | Name | Type | Region | Env | In ASG | Interruption % | Recommendation | Monthly Saving | Saving % |",
            "| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |",
        ]

        for c in candidates:
            name = c["name"] or c["instance_id"]
            lines.append(
                f"| {c['instance_id']} "
                f"| {name} "
                f"| {c['instance_type']} "
                f"| {c['region']} "
                f"| {c['environment'] or '-'} "
                f"| {'yes' if c['in_asg'] else 'no'} "
                f"| {c['interruption_freq_pct']:.1f}% "
                f"| {c['recommendation']} "
                f"| ${c['monthly_savings']:,.2f} "
                f"| {c['savings_pct']:.1f}% |"
            )

        lines += [
            "",
            "**How to migrate:** Use a Launch Template with a mixed instances policy. "
            "Set OnDemandPercentageAboveBaseCapacity=0 to run fully on spot. "
            "Add capacity-optimized allocation strategy and 5+ instance types "
            "for interruption resilience. Always test in staging first.",
            "",
            _srv.cost_note("\n".join(lines), savings_found_usd=total_savings),
        ]

        nudge = _srv._team_nudge(
            f"You have {len(recommended)} RECOMMENDED spot migration "
            f"opportunit{'ies' if len(recommended) != 1 else 'y'} "
            f"worth ${total_savings:,.0f}/mo. To auto-create Jira, Linear, or GitHub "
            f"tickets so these actually get fixed, upgrade to Pro:"
        , context="recommend_spot_adoption")
        if nudge:
            lines += ["", nudge]

        return "\n".join(lines)

    except Exception as e:
        return f"Error scanning for spot adoption opportunities: {e}"


@_srv.mcp.tool()
async def recommend_database_savings_plans() -> dict:
    """
    Recommends AWS Database Savings Plans for RDS and Aurora spend. Database
    SPs (re:Invent 2025) offer up to 45% savings, separate from Compute SPs.
    Sizes a 1-year no-upfront plan to uncovered baseline spend.

    Examples:
        - "Should we buy Database Savings Plans?"
        - "How much could we save on RDS with a Database SP?"
        - "What is our RDS/Aurora Savings Plan coverage?"
    """
    try:
        from ..recommendations.database_savings_plans import (
            recommend_database_savings_plans as _recommend,
        )

        aws = _srv.CLOUD_CONNECTORS.get("aws")
        if aws is None or not await aws.is_configured():
            return {"error": "AWS is not connected. Call connect_aws right here in the chat (it detects credentials already on this machine), or run 'uvx nable' in a terminal."}

        result = _recommend()
        if result is None:
            return {"error": "Could not retrieve RDS spend data. Check AWS credentials."}
        return result

    except Exception as e:
        _srv.log.error("recommend_database_savings_plans failed: %s", e, exc_info=True)
        return {"error": str(e)}
