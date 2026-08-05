# SPDX-License-Identifier: Apache-2.0
"""azure MCP tools (extracted from server.py; see finops/tools/__init__.py).

Server-local helpers, globals, and the mcp instance are reached through the live
server module (_srv.NAME) so monkeypatching finops.server.* still works and no
import-order coupling exists."""
from __future__ import annotations

from .. import server as _srv


@_srv.mcp.tool()
async def connect_azure() -> dict:
    """
    Guide connecting Azure while keeping the service-principal secret off the model.

    Azure has no local credentials nable can safely auto-detect, so connecting
    needs a client secret. Unlike connect_aws and connect_gcp (which read
    credentials already on the machine, so nothing sensitive passes through this
    conversation), an Azure secret would have to be pasted into the chat to reach
    a tool argument, which routes it through the model provider. nable does not do
    that. This tool returns the Cloud Shell script and has you finish the connect
    in your OWN terminal with `finops setup azure`, which encrypts the secret into
    your local vault. The model never sees the secret.

    Examples:
        - "Connect Azure"
        - "How do I connect my Azure subscription?"
    """
    from ..security.azure_cloudshell import CLOUDSHELL_URL, generate_cloudshell_script
    from ..security.vault import Vault

    try:
        already = "AZURE_TENANT_ID" in set(Vault.default().list_keys())
    except Exception:
        already = False
    if already:
        from .. import demo_data as _dd
        _dd._real_provider_cache = None
        _srv._tool_surface_changed()
        return {
            "connected": True,
            "provider": "azure",
            "message": "Azure is already connected. Ask me for your Azure cost summary or top cost drivers.",
        }

    return {
        "connected": False,
        "cloudshell_url": CLOUDSHELL_URL,
        "steps": [
            f"1. Open Azure Cloud Shell (already signed in as you): {CLOUDSHELL_URL}",
            "2. Paste the script below and wait ~30 seconds for it to finish.",
            "3. In YOUR OWN terminal (not this chat) run:  finops setup azure",
            "   choose the Cloud Shell option, and paste the line the script printed there.",
        ],
        "script": generate_cloudshell_script(),
        "why_not_paste_here": (
            "That line contains an Azure client secret. Pasting it into this chat would send it "
            "to the model provider. nable keeps it local: you paste it into the finops CLI, which "
            "encrypts it into your vault, and the model never sees it. AWS and GCP connect in-chat "
            "because they read credentials already on your machine; Azure needs a secret, so it "
            "stays in your terminal."
        ),
    }


@_srv.mcp.tool()
async def get_resource_cost_breakdown_azure(
    start_date: str | None = None,
    end_date: str | None = None,
    subscription_id: str | None = None,
    resource_group: str | None = None,
    min_cost_usd: float = 1.0,
    limit: int = 200,
) -> dict:
    """
    Return per-resource Azure cost detail via the Cost Management Query API.

    No storage account or export job required -- data is queried live.
    Supports multi-subscription environments. Pro plan feature.

    Args:
        start_date: ISO date (YYYY-MM-DD). Defaults to 30 days ago.
        end_date: ISO date. Defaults to today.
        subscription_id: Single Azure subscription ID. None = all configured subs.
        resource_group: Filter to a specific resource group. None = all groups.
        min_cost_usd: Exclude resources below this cost threshold (default $1).
        limit: Maximum resources to return ordered by cost descending (default 200).

    Examples:
        - "Show me per-resource Azure costs this month"
        - "Which Azure resources are most expensive in the production resource group?"
        - "Break down costs by resource across all subscriptions"
    """

    sd, ed = _srv._default_dates()
    if start_date:
        sd = _srv.date.fromisoformat(start_date)
    if end_date:
        ed = _srv.date.fromisoformat(end_date)

    try:
        from ..connectors.azure_detail import get_resource_costs
        result = get_resource_costs(
            start_date=sd,
            end_date=ed,
            subscription_id=subscription_id,
            resource_group=resource_group,
            min_cost_usd=min_cost_usd,
            limit=limit,
        )
        resources = result.get("resources")
        if isinstance(resources, list) and resources:
            # Connector returns resources pre-sorted by cost desc. Bound the
            # token cost of the detail rows without dropping any totals.
            returned_count = len(resources)
            kept, omitted = _srv.fit_to_budget(resources, max_tokens=6000)
            if omitted > 0:
                result["resources"] = kept
                result["resources_truncated"] = omitted
                result["resources_hint"] = (
                    f"showing top {len(kept)} of {returned_count} resources by cost; "
                    f"total_cost covers all {returned_count}. Filter by "
                    f"resource_group or subscription_id, or raise min_cost_usd "
                    f"for fewer, larger resources."
                )
        return result
    except Exception as exc:
        _srv.log.error("get_resource_cost_breakdown_azure failed: %s", exc)
        return {"error": str(exc)}


@_srv.mcp.tool()
async def get_azure_reservation_utilization(
    start_date: str | None = None,
    end_date: str | None = None,
) -> dict:
    """
    Fetch Azure reservation utilization summaries from the Capacity API.

    Shows monthly utilization rates, used vs reserved hours, and wasted
    capacity for all reservations visible to the configured service principal.
    Pro plan feature.

    Args:
        start_date: ISO date (YYYY-MM-DD). Defaults to 30 days ago.
        end_date: ISO date. Defaults to today.

    Examples:
        - "How well are we utilizing our Azure reservations?"
        - "Which Azure reservations are underutilized?"
        - "Show wasted Azure reserved capacity this quarter"
    """

    sd, ed = _srv._default_dates()
    if start_date:
        sd = _srv.date.fromisoformat(start_date)
    if end_date:
        ed = _srv.date.fromisoformat(end_date)

    try:
        from ..connectors.azure_detail import get_reservation_utilization
        return get_reservation_utilization(start_date=sd, end_date=ed)
    except Exception as exc:
        _srv.log.error("get_azure_reservation_utilization failed: %s", exc)
        return {"error": str(exc)}


@_srv.mcp.tool()
async def get_azure_advisor_recommendations(
    subscription_id: str | None = None,
    limit: int = 100,
) -> dict:
    """
    Azure Advisor cost recommendations, with Microsoft-computed annual savings.

    Advisor is Azure's native optimization engine: VM rightsizing, idle resource
    cleanup, and reservation / savings-plan purchase recommendations, each with a
    savings figure Microsoft already calculated. This is the Azure parallel of AWS
    Compute Optimizer.

    Args:
        subscription_id: A single Azure subscription. None = all configured subs.
        limit: Max recommendations to return, highest savings first (default 100).

    Examples:
        - "What does Azure Advisor recommend to cut our costs?"
        - "Show Azure cost recommendations with the biggest savings"
        - "Any idle or oversized Azure resources Advisor flagged?"
    """
    try:
        from ..connectors.azure_optimize import get_advisor_cost_recommendations
        result = await _srv.asyncio.to_thread(get_advisor_cost_recommendations, subscription_id=subscription_id, limit=limit)
        recs = result.get("recommendations")
        if isinstance(recs, list) and recs:
            kept, omitted = _srv.fit_to_budget(recs, max_tokens=6000)
            if omitted:
                result["recommendations"] = kept
                result["recommendations_truncated"] = omitted
                result["recommendations_hint"] = (
                    f"showing top {len(kept)} of {len(recs)} by savings; totals cover all."
                )
        return result
    except Exception as exc:
        _srv.log.error("get_azure_advisor_recommendations failed: %s", exc)
        return {"error": str(exc)}


@_srv.mcp.tool()
async def get_azure_vm_rightsizing(
    subscription_id: str | None = None,
    lookback_days: int = 14,
    limit: int = 100,
    max_vms_scanned: int = 200,
) -> dict:
    """
    Find idle and oversized Azure VMs from Azure Monitor CPU, with real dollar cost.

    Idle VMs (very low average CPU and a low peak) are deallocate/delete candidates.
    Underutilized VMs (low average but some real peak) are downsize candidates.
    Bursty VMs (high peak) are left alone. Per-VM monthly cost is joined from Cost
    Management so the savings are real, not a guess. This is the Azure parallel of
    nable's idle-EC2 and rightsizing engines.

    Args:
        subscription_id: A single Azure subscription. None = all configured subs.
        lookback_days: CPU history window for the analysis (default 14).
        limit: Max VMs to return, highest savings first (default 100).
        max_vms_scanned: Cap on how many VMs (costliest first) get a CPU-metrics
            call, so a large estate does not hang on hundreds of serial requests
            (default 200).

    Examples:
        - "vm rightsizing"
        - "Show me oversized Azure VMs we can downsize"
        - "Which Azure VMs are idle and wasting money?"
        - "Azure rightsizing opportunities for the last two weeks"
    """
    try:
        from ..connectors.azure_optimize import get_vm_rightsizing
        # Offload the blocking Azure REST calls so they do not freeze the asyncio
        # event loop (and the in-process Slack bot / scheduler) for the whole query.
        result = await _srv.asyncio.to_thread(
            get_vm_rightsizing,
            subscription_id=subscription_id, lookback_days=lookback_days,
            limit=limit, max_vms_scanned=max_vms_scanned,
        )
        vms = result.get("vms")
        if isinstance(vms, list) and vms:
            kept, omitted = _srv.fit_to_budget(vms, max_tokens=6000)
            if omitted:
                result["vms"] = kept
                result["vms_truncated"] = omitted
                result["vms_hint"] = (
                    f"showing top {len(kept)} of {len(vms)} by savings; totals cover all."
                )
        return result
    except Exception as exc:
        _srv.log.error("get_azure_vm_rightsizing failed: %s", exc)
        return {"error": str(exc)}


@_srv.mcp.tool()
async def get_azure_budgets(subscription_id: str | None = None) -> dict:
    """
    Read the budgets you already set in Azure and report consumption against each.

    Pulls native Azure Consumption Budgets (the ones configured in the Azure
    Portal), with amount, current spend, percent consumed, and a warning/exceeded
    status. Use this to see budget health without leaving Claude.

    Args:
        subscription_id: A single Azure subscription. None = all configured subs.

    Examples:
        - "Are we over any Azure budgets?"
        - "Show our Azure budget status"
        - "Which Azure budgets are close to their limit?"
    """
    try:
        from ..connectors.azure_optimize import get_native_budgets
        return await _srv.asyncio.to_thread(get_native_budgets, subscription_id=subscription_id)
    except Exception as exc:
        _srv.log.error("get_azure_budgets failed: %s", exc)
        return {"error": str(exc)}


@_srv.mcp.tool()
async def forecast_azure_costs(
    subscription_id: str | None = None,
    end_date: str | None = None,
) -> dict:
    """
    Forecast Azure spend using Azure Cost Management's own forecast model.

    Calls Microsoft's forecast endpoint, which blends actual billed days with a
    forecast for the rest of the window. Defaults to projecting the current month
    to month-end. More accurate for Azure than a generic statistical forecast.

    Args:
        subscription_id: A single Azure subscription. None = all configured subs.
        end_date: ISO date to forecast to (YYYY-MM-DD). Defaults to end of month.

    Examples:
        - "What will our Azure bill be at the end of the month?"
        - "Forecast Azure spend to month-end"
        - "Projected Azure costs for this subscription"
    """
    if (err := _srv.require_pro("forecasting")):
        return err
    try:
        from ..connectors.azure_optimize import forecast_costs
        ed = _srv.date.fromisoformat(end_date) if end_date else None
        return await _srv.asyncio.to_thread(forecast_costs, subscription_id=subscription_id, end_date=ed)
    except ValueError:
        return {"error": "end_date must be ISO format YYYY-MM-DD."}
    except Exception as exc:
        _srv.log.error("forecast_azure_costs failed: %s", exc)
        return {"error": str(exc)}


@_srv.mcp.tool()
async def get_azure_cost_by_dimension(
    dimension: str,
    start_date: str | None = None,
    end_date: str | None = None,
    subscription_id: str | None = None,
    limit: int = 50,
) -> dict:
    """
    Break Azure spend down by any dimension: service, resource group, location, or meter.

    Args:
        dimension: One of service, resource_group, location, meter, meter_subcategory.
        start_date: ISO date (YYYY-MM-DD). Defaults to 30 days ago.
        end_date: ISO date. Defaults to today.
        subscription_id: A single Azure subscription. None = all configured subs.
        limit: Max values to return, highest cost first (default 50).

    Examples:
        - "Break down Azure costs by resource group"
        - "Azure spend by location this month"
        - "Which Azure meters cost the most?"
    """
    sd, ed = _srv._default_dates()
    if start_date:
        try:
            sd = _srv.date.fromisoformat(start_date)
        except ValueError:
            return {"error": "start_date must be ISO format YYYY-MM-DD."}
    if end_date:
        try:
            ed = _srv.date.fromisoformat(end_date)
        except ValueError:
            return {"error": "end_date must be ISO format YYYY-MM-DD."}
    try:
        from ..connectors.azure_optimize import get_cost_by_dimension
        return await _srv.asyncio.to_thread(
            get_cost_by_dimension,
            dimension=dimension, start_date=sd, end_date=ed,
            subscription_id=subscription_id, limit=limit,
        )
    except Exception as exc:
        _srv.log.error("get_azure_cost_by_dimension failed: %s", exc)
        return {"error": str(exc)}


@_srv.mcp.tool()
async def audit_azure_reservation_scope(
    start_date: str | None = None,
    end_date: str | None = None,
) -> dict:
    """
    Find Azure reservations locked to one subscription while a sibling
    subscription pays full price for the same capacity.

    A reservation scoped to a single subscription only discounts resources in
    that subscription; matching workloads anywhere else in the billing account
    pay on-demand rates while the reservation's hours go unused. The fix is a
    free portal setting (scope -> Shared): no exchange, no refund, no new
    commercial transaction.

    A finding requires ALL THREE, so deliberate single-scoping (capacity
    priority, chargeback) is never flagged on its own:
      1. the reservation is scoped to a single subscription or resource group
      2. it is measurably underutilised over the window
      3. a subscription it cannot reach spent real money on the same meter

    Args:
        start_date: ISO date (YYYY-MM-DD). Defaults to 30 days ago.
        end_date: ISO date. Defaults to today.

    Examples:
        - "Are any Azure reservations scoped too narrowly?"
        - "Is reserved capacity going unused while another subscription pays full price?"
        - "Audit our Azure reservation scopes"
    """
    sd, ed = _srv._default_dates()
    if start_date:
        try:
            sd = _srv.date.fromisoformat(start_date)
        except ValueError:
            return {"error": "start_date must be ISO format YYYY-MM-DD."}
    if end_date:
        try:
            ed = _srv.date.fromisoformat(end_date)
        except ValueError:
            return {"error": "end_date must be ISO format YYYY-MM-DD."}

    try:
        from ..connectors.azure_detail import (
            get_on_demand_usage_by_subscription,
            get_reservation_utilization,
        )
        from ..recommendations.azure_reservation_scope import (
            find_scope_limited_reservations,
        )
        from ..recommendations.critique import critique

        util = await _srv.asyncio.to_thread(get_reservation_utilization,
                                            start_date=sd, end_date=ed)
        if util.get("error"):
            return util
        usage = await _srv.asyncio.to_thread(get_on_demand_usage_by_subscription,
                                             start_date=sd, end_date=ed)
        if usage.get("error"):
            return usage

        findings = find_scope_limited_reservations(
            util.get("reservations") or [], usage.get("usage") or [])
        reviewed = critique([f.to_dict() for f in findings], use_llm=False)

        confirmed = [f for f in reviewed
                     if (f.get("critique") or {}).get("survived", True)]
        total = sum(f.get("est_monthly_savings") or 0.0 for f in confirmed)
        return {
            "findings": reviewed,
            "reservations_checked": len(util.get("reservations") or []),
            "subscriptions_checked": len({u.get("subscription_id")
                                          for u in usage.get("usage") or []}),
            "est_monthly_savings_usd": round(total, 2),
            "period": f"{sd} to {ed}",
            "note": ("The fix is a free portal setting change (scope -> Shared). "
                     "nable proposes it and changes nothing."),
        }
    except Exception as exc:
        _srv.log.error("audit_azure_reservation_scope failed: %s", exc)
        return {"error": str(exc)}
