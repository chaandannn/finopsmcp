# SPDX-License-Identifier: Apache-2.0
"""cost_queries MCP tools (extracted from server.py; see finops/tools/__init__.py).

Server-local helpers, globals, and the mcp instance are reached through the live
server module (_srv.NAME) so monkeypatching finops.server.* still works and no
import-order coupling exists."""
from __future__ import annotations

from .. import server as _srv


@_srv.mcp.tool()
async def get_cost_summary(
    provider: str | None = None,
    category: str | None = None,
    start_date: str | None = None,
    end_date: str | None = None,
    granularity: str = "MONTHLY",
    account: str | None = None,
) -> dict:
    """
    Get total spend summarized by service, account, and region.

    Args:
        provider: Provider name (e.g. "aws", "datadog"). None = all.
        category: "cloud" or "saas". None = all.
        start_date: ISO date (YYYY-MM-DD). Defaults to 30 days ago.
        end_date: ISO date. Defaults to today.
        granularity: "DAILY" or "MONTHLY".
        account: Named AWS account from accounts.yaml. Uses default when omitted.

    Examples:
        - "How much did we spend last month?"
        - "Give me an AWS cost summary for January"
        - "What did the production account spend this month?"
    """
    from ..demo_data import is_demo, get_demo_response
    if is_demo():
        return get_demo_response("get_cost_summary") or {}

    sd, ed = _srv._default_dates()
    if start_date:
        sd = _srv.date.fromisoformat(start_date)
    if end_date:
        ed = _srv.date.fromisoformat(end_date)

    # Multi-account: swap in an account-specific AWS connector when requested
    if account:
        from ..accounts import get_account, get_default_account, get_boto3_session
        from ..connectors.aws import AWSConnector as _AWSConnector
        acct_cfg = get_account(account) or get_default_account()
        if not acct_cfg:
            return {"error": f"Account '{account}' not found. Run list_aws_accounts() to see configured accounts."}
        session = get_boto3_session(acct_cfg)
        acct_connector = _AWSConnector(session=session)
        pool = {"aws": acct_connector}
        targets = {"aws": acct_connector} if await acct_connector.is_configured() else {}
        if not targets:
            return {"error": f"Could not connect to account '{acct_cfg.name}'. Check credentials."}
    elif provider:
        pool = {provider: _srv._ALL_CONNECTORS[provider]} if provider in _srv._ALL_CONNECTORS else {}
        targets = await _srv._active(pool)
    elif category == "cloud":
        pool = _srv.CLOUD_CONNECTORS
        targets = await _srv._active(pool)
    elif category == "saas":
        pool = _srv.SAAS_CONNECTORS
        targets = await _srv._active(pool)
    else:
        pool = _srv._ALL_CONNECTORS
        targets = await _srv._active(pool)
    if not targets:
        return {"error": "No cloud accounts connected yet. Connect one right here in the chat: call connect_aws or connect_gcp (they detect credentials already on this machine) or connect_azure. No terminal, no restart. Prefer a guided terminal setup? Run 'uvx nable' instead."}

    grand_total, by_provider, grand_by_service = await _srv._gather_costs(targets, sd, ed, granularity)
    # With several providers the per-provider service detail is the token-bloat driver.
    # Keep full detail for a single-provider query (that's where the detail is wanted);
    # cap it once the answer spans multiple providers. grand_by_service keeps the full
    # cross-provider ranking regardless.
    if len(by_provider) > 1:
        by_provider = _srv._cap_provider_service_detail(by_provider)

    # A total is only a total if something was actually read. Providers that
    # errored contribute 0 to grand_total, so a run where every provider failed
    # used to return "$0.00" with the reason buried in by_provider, and a model
    # reading that tells the user they spent nothing this month. A confident
    # wrong number is worse than a refusal.
    _failed = {name: p.get("error") for name, p in by_provider.items()
               if isinstance(p, dict) and p.get("error")}
    _ok = [name for name in by_provider if name not in _failed]

    if _failed and not _ok:
        # Every provider failed. Refuse, and lead with the first reason, which
        # carries the setup instructions when it is a missing billing export.
        first = next(iter(_failed.values()))
        return {
            "error": "no_cost_data",
            "message": str(first),
            "failed_providers": _failed,
            "note": ("No provider returned cost data, so nable has no total to "
                     "report. This is not a finding of zero spend."),
        }

    _ranked_services = sorted(grand_by_service.items(), key=lambda x: -x[1])
    result = {
        "period": {"start": sd.isoformat(), "end": ed.isoformat()},
        "grand_total_usd": round(grand_total, 4),
        "grand_total_formatted": _srv._fmt_usd(grand_total),
        "by_provider": by_provider,
        "grand_by_service": {k: round(v, 4) for k, v in _ranked_services[:50]},
    }
    # This is the front door of the funnel, so it carries the map to the next
    # room. nable advertises only entry points; the drill-down tools are callable
    # but unlisted, and naming the ones that fit THIS answer is what keeps them
    # reachable without paying for 130 tool definitions on every message.
    from ..tool_surface import drilldown_for
    result["next_tools"] = drilldown_for(k for k, _ in _ranked_services[:8])
    if _failed:
        # Some read, some did not. The total is real but incomplete, and nothing
        # downstream may present it as the whole bill.
        result["partial"] = True
        result["failed_providers"] = _failed
        result["partial_warning"] = (
            f"This total covers {', '.join(sorted(_ok))} only. "
            f"{', '.join(sorted(_failed))} could not be read, so real spend is "
            f"higher than the figure above."
        )
    # If any provider reports a non-USD currency, the grand total mixes currencies
    # and must not be presented as USD. nable does not convert, surface it loudly.
    _currencies = {
        p.get("currency", "USD") for p in by_provider.values()
        if isinstance(p, dict) and "error" not in p
    }
    _non_usd = {c for c in _currencies if c and c != "USD"}
    if _non_usd:
        result["currency_warning"] = (
            "Cost data spans more than one currency "
            f"({', '.join(sorted(_currencies))}). nable does not convert currencies, so "
            "grand_total_usd sums raw amounts across currencies and is NOT a true USD total. "
            "Read each provider's own currency under by_provider.<provider>.currency."
        )
    if len(_ranked_services) > 50:
        # grand_total_usd covers ALL services; the dict shows only the top 50.
        # Flag it so the model doesn't read the parts as not summing to the whole.
        result["grand_by_service_truncated"] = (
            f"Showing top 50 of {len(_ranked_services)} services by cost. "
            "grand_total_usd reflects all services."
        )

    # Subtle nudge after the first real cost query -- mention anomaly alerts + ticket creation
    # Only fires for free users with real spend data (not $0 accounts)
    if grand_total > 10:
        nudge = _srv._team_nudge(
            "To get automatic Slack alerts when spend spikes and auto-create tickets "
            "for waste findings, upgrade to Pro:"
        , context="cost_summary")
        if nudge:
            result["_tip"] = nudge

    # Credits: if this AWS account is running on Activate credits, volunteer the
    # true burn so a near-$0 cash bill does not read as "free". Best-effort and
    # cached; nothing shows for a normal cash-paying account.
    aws_conn = targets.get("aws")
    if aws_conn is not None:
        ctx = await _srv._credit_context(aws_conn, account or "default")
        if ctx:
            result["_credit_context"] = ctx

    return result


@_srv.mcp.tool()
async def get_costs_by_service(
    service_filter: str | None = None,
    provider: str | None = None,
    category: str | None = None,
    start_date: str | None = None,
    end_date: str | None = None,
    account: str | None = None,
) -> dict:
    """
    Cost breakdown by service, optionally filtered to a keyword.

    Args:
        service_filter: Case-insensitive substring (e.g. "compute", "storage").
        provider: Specific provider. None = all.
        category: "cloud" or "saas". None = all.
        start_date: ISO date. Defaults to 30 days ago.
        end_date: ISO date. Defaults to today.
        account: Named AWS account from accounts.yaml.

    Examples:
        - "How much did compute cost us?"
        - "Show me all Datadog product costs"
        - "What did the staging account spend on EC2?"
    """
    sd, ed = _srv._default_dates()
    if start_date:
        sd = _srv.date.fromisoformat(start_date)
    if end_date:
        ed = _srv.date.fromisoformat(end_date)

    if account:
        from ..accounts import get_account, get_default_account, get_boto3_session
        from ..connectors.aws import AWSConnector as _AWSConnector
        acct_cfg = get_account(account) or get_default_account()
        if not acct_cfg:
            return {"error": f"Account '{account}' not found. Run list_aws_accounts() to see configured accounts."}
        session = get_boto3_session(acct_cfg)
        acct_connector = _AWSConnector(session=session)
        targets = {"aws": acct_connector} if await acct_connector.is_configured() else {}
    elif provider:
        pool = {provider: _srv._ALL_CONNECTORS[provider]} if provider in _srv._ALL_CONNECTORS else {}
        targets = await _srv._active(pool)
    elif category == "cloud":
        targets = await _srv._active(_srv.CLOUD_CONNECTORS)
    elif category == "saas":
        targets = await _srv._active(_srv.SAAS_CONNECTORS)
    else:
        targets = await _srv._active(_srv._ALL_CONNECTORS)
    if not targets:
        return {"error": "No cloud accounts connected yet. Connect one right here in the chat: call connect_aws or connect_gcp (they detect credentials already on this machine) or connect_azure. No terminal, no restart. Prefer a guided terminal setup? Run 'uvx nable' instead."}

    combined: dict[str, dict[str, float]] = {}
    errors: dict[str, str] = {}

    async def _one_svc(name: str, connector: _srv.Any):
        try:
            return name, await _srv._fetch_costs_cached(name, connector, sd, ed), None
        except Exception as exc:
            return name, None, str(exc)

    for name, summary, err in await _srv.asyncio.gather(*[_one_svc(n, c) for n, c in targets.items()]):
        if err is not None:
            errors[name] = err
            continue
        for svc, amt in summary.by_service.items():
            if service_filter and service_filter.lower() not in svc.lower():
                continue
            if svc not in combined:
                combined[svc] = {}
            combined[svc][name] = combined[svc].get(name, 0.0) + amt

    ranked = sorted(
        [
            {
                "service": svc,
                "total_usd": round(sum(by_prov.values()), 4),
                "total_formatted": _srv._fmt_usd(sum(by_prov.values())),
                "by_provider": {k: round(v, 4) for k, v in by_prov.items()},
            }
            for svc, by_prov in combined.items()
        ],
        key=lambda x: -x["total_usd"],
    )

    total_usd = round(sum(s["total_usd"] for s in ranked), 4)
    kept, omitted = _srv.fit_to_budget(ranked)
    result: dict[str, _srv.Any] = {
        "period": {"start": sd.isoformat(), "end": ed.isoformat()},
        "filter": service_filter,
        "services": kept,
        "total_usd": total_usd,
    }
    if omitted:
        result["services_truncated"] = True
        result["hint"] = f"Showing top {len(kept)} of {len(ranked)} services by cost to stay within token budget. total_usd reflects all services."
    if errors:
        result["errors"] = errors
    return result


@_srv.mcp.tool()
async def get_top_cost_drivers(
    limit: int = 10,
    provider: str | None = None,
    category: str | None = None,
    start_date: str | None = None,
    end_date: str | None = None,
    account: str | None = None,
) -> dict:
    """
    Return the top N most expensive services across all configured providers.

    Args:
        limit: Number of top services to return (default 10).
        provider: Specific provider. None = all.
        category: "cloud" or "saas". None = all.
        start_date: ISO date. Defaults to 30 days ago.
        end_date: ISO date. Defaults to today.
        account: Named AWS account from accounts.yaml.

    Examples:
        - "What are our biggest cost drivers this month?"
        - "Top 5 most expensive things in AWS"
        - "Top cost drivers in the staging account"
    """
    result = await _srv.get_costs_by_service(
        service_filter=None,
        provider=provider,
        category=category,
        start_date=start_date,
        end_date=end_date,
        account=account,
    )
    if "error" in result:
        return result

    grand = result.get("total_usd", 0.0)
    top = result["services"][:limit]
    for svc in top:
        svc["pct_of_total"] = round(svc["total_usd"] / grand * 100, 1) if grand else 0

    return {
        "period": result["period"],
        "top_services": top,
        "grand_total_usd": grand,
        "grand_total_formatted": _srv._fmt_usd(grand),
    }


@_srv.mcp.tool()
async def get_cost_trends(
    provider: str | None = None,
    category: str | None = None,
    days: int = 30,
    granularity: str = "DAILY",
) -> dict:
    """
    Cost trends over time broken down by day or month.

    Args:
        provider: Specific provider. None = all.
        category: "cloud" or "saas". None = all.
        days: Look-back window in days (default 30).
        granularity: "DAILY" or "MONTHLY".

    Examples:
        - "Is our AWS spend trending up or down?"
        - "Show daily cloud costs for the last 2 weeks"
        - "What did we spend each month this quarter?"
    """
    end = _srv.date.today()
    start = end - _srv.timedelta(days=days)

    pool = _srv.CLOUD_CONNECTORS if category == "cloud" else _srv.SAAS_CONNECTORS if category == "saas" else _srv._ALL_CONNECTORS
    if provider and provider in pool:
        pool = {provider: pool[provider]}

    targets = await _srv._active(pool)
    if not targets:
        return {"error": "No cloud accounts connected yet. Connect one right here in the chat: call connect_aws or connect_gcp (they detect credentials already on this machine) or connect_azure. No terminal, no restart. Prefer a guided terminal setup? Run 'uvx nable' instead."}

    grand_total, by_provider, _ = await _srv._gather_costs(targets, start, end, granularity)

    return {
        "period": {"start": start.isoformat(), "end": end.isoformat(), "granularity": granularity},
        "grand_total_usd": round(grand_total, 4),
        "grand_total_formatted": _srv._fmt_usd(grand_total),
        "by_provider": by_provider,
        "note": "For full time-series granularity, configure BigQuery exports (GCP) or Cost and Usage Reports (AWS).",
    }


@_srv.mcp.tool()
async def get_cost_summary_all_accounts(
    start_date: str | None = None,
    end_date: str | None = None,
    granularity: str = "MONTHLY",
) -> dict:
    """
    Fan out cost queries across ALL configured AWS accounts and return a combined
    view sorted by total spend. Shows each account's total and top services.

    Args:
        start_date: ISO date (YYYY-MM-DD). Defaults to 30 days ago.
        end_date: ISO date. Defaults to today.
        granularity: "DAILY" or "MONTHLY".

    Examples:
        - "Show costs across all my AWS accounts"
        - "What is each client account spending this month?"
        - "Compare spend across production and staging accounts"
    """
    from datetime import date, timedelta
    from ..accounts import list_accounts as _list_accounts, get_boto3_session
    from ..connectors.aws import AWSConnector

    sd = date.fromisoformat(start_date) if start_date else date.today() - timedelta(days=30)
    ed = date.fromisoformat(end_date) if end_date else date.today()

    accounts = _list_accounts()
    if not accounts:
        return {
            "error": (
                "No accounts configured. Run 'finops setup aws' to add one, "
                "or 'finops setup aws --org' to auto-discover from AWS Organizations."
            )
        }

    results = []
    grand_total = 0.0
    errors: dict[str, str] = {}

    for acct in accounts:
        try:
            session = get_boto3_session(acct)
            # Identity is what keeps this loop from serving account #1's spend
            # for every account: one connector per account, one cache entry each.
            connector = AWSConnector(
                session=session, identity=acct.account_id or acct.name)
            summary = await connector.get_costs(sd, ed, granularity=granularity)
            top_services = sorted(summary.by_service.items(), key=lambda x: -x[1])[:5]
            results.append({
                "account": acct.name,
                "account_id": acct.account_id or summary.by_account and list(summary.by_account.keys())[0] or "",
                "total_usd": round(summary.total_usd, 4),
                "total_formatted": _srv._fmt_usd(summary.total_usd),
                "top_services": [
                    {"service": s, "amount_usd": round(a, 4)} for s, a in top_services
                ],
            })
            grand_total += summary.total_usd
        except Exception as exc:
            errors[acct.name] = str(exc)

    results.sort(key=lambda x: -x["total_usd"])
    for r in results:
        r["pct_of_total"] = round(r["total_usd"] / grand_total * 100, 1) if grand_total else 0

    out: dict = {
        "period": {"start": sd.isoformat(), "end": ed.isoformat()},
        "grand_total_usd": round(grand_total, 4),
        "grand_total_formatted": _srv._fmt_usd(grand_total),
        "account_count": len(results),
        "accounts": results,
    }
    if errors:
        out["errors"] = errors
    return out


@_srv.mcp.tool()
async def get_saas_spend_summary(
    start_date: str | None = None,
    end_date: str | None = None,
) -> dict:
    """
    Dedicated summary of all SaaS tool spending (Datadog, Snowflake, GitHub, etc.).
    Useful for understanding your software vendor bill separate from cloud infrastructure.

    Examples:
        - "How much are we spending on SaaS tools?"
        - "What's our total software vendor spend?"
        - "Break down our SaaS costs by tool"
    Args:
        start_date: ISO date (YYYY-MM-DD). Defaults to 30 days ago.
        end_date: ISO date (YYYY-MM-DD). Defaults to today.

    """
    return await _srv.get_cost_summary(category="saas", start_date=start_date, end_date=end_date)


@_srv.mcp.tool()
async def get_total_spend_all_sources(
    start_date: str | None = None,
    end_date: str | None = None,
) -> dict:
    """
    Grand total across ALL connected sources, cloud infrastructure + SaaS tools combined.
    The true "total technology spend" number.

    Examples:
        - "What is our total tech spend this month?"
        - "How much are we spending on everything combined?"
        - "Give me our full cloud + software cost picture"
    Args:
        start_date: ISO date (YYYY-MM-DD). Defaults to 30 days ago.
        end_date: ISO date (YYYY-MM-DD). Defaults to today.

    """
    sd, ed = _srv._default_dates()
    if start_date:
        sd = _srv.date.fromisoformat(start_date)
    if end_date:
        ed = _srv.date.fromisoformat(end_date)

    targets = await _srv._active(_srv._ALL_CONNECTORS)
    if not targets:
        return {"error": "No cloud accounts connected yet. Connect one right here in the chat: call connect_aws or connect_gcp (they detect credentials already on this machine) or connect_azure. No terminal, no restart. Prefer a guided terminal setup? Run 'uvx nable' instead."}

    grand_total, by_provider, grand_by_service = await _srv._gather_costs(targets, sd, ed)
    # Cross-provider tool: top_services already carries the ranked drivers, so cap the
    # per-provider service detail to keep the payload flat as providers scale.
    by_provider = _srv._cap_provider_service_detail(by_provider)

    cloud_total = sum(
        by_provider[p]["total_usd"]
        for p in _srv.CLOUD_CONNECTORS
        if p in by_provider and "total_usd" in by_provider[p]
    )
    saas_total = sum(
        by_provider[p]["total_usd"]
        for p in _srv.SAAS_CONNECTORS
        if p in by_provider and "total_usd" in by_provider[p]
    )

    return {
        "period": {"start": sd.isoformat(), "end": ed.isoformat()},
        "grand_total_usd": round(grand_total, 4),
        "grand_total_formatted": _srv._fmt_usd(grand_total),
        "cloud_total_usd": round(cloud_total, 4),
        "cloud_total_formatted": _srv._fmt_usd(cloud_total),
        "saas_total_usd": round(saas_total, 4),
        "saas_total_formatted": _srv._fmt_usd(saas_total),
        "cloud_pct": round(cloud_total / grand_total * 100, 1) if grand_total else 0,
        "saas_pct": round(saas_total / grand_total * 100, 1) if grand_total else 0,
        "by_provider": by_provider,
        "top_services": [
            {"service": k, "amount_usd": round(v, 4), "formatted": _srv._fmt_usd(v)}
            for k, v in sorted(grand_by_service.items(), key=lambda x: -x[1])[:10]
        ],
    }


@_srv.mcp.tool()
async def get_cost_history(
    provider: str,
    service: str,
    account_id: str,
    days: int = 30,
) -> dict:
    """
    Return historical daily cost data for a specific provider + service.
    Used for trend analysis and understanding anomaly context.

    Args:
        provider: e.g. "aws"
        service: e.g. "Amazon EC2"
        account_id: The account/subscription ID
        days: Look-back window in days (default 30)

    Examples:
        - "Show me 30 days of history for AWS EC2"
        - "What did Datadog cost each day this month?"
    """
    from ..storage.snapshots import get_history

    rows = get_history(provider, service, account_id, days=days)
    if not rows:
        return {
            "data": [],
            "message": "No history found. Ensure daily snapshots are running.",
        }

    amounts = [r["amount_usd"] for r in rows]
    import statistics
    return {
        "provider": provider,
        "service": service,
        "account_id": account_id,
        "days_of_data": len(rows),
        "mean_usd": round(statistics.mean(amounts), 4) if amounts else 0,
        "max_usd": round(max(amounts), 4) if amounts else 0,
        "min_usd": round(min(amounts), 4) if amounts else 0,
        "data": [
            {"date": r["snapshot_date"], "amount_usd": round(r["amount_usd"], 4)}
            for r in rows
        ],
    }


@_srv.mcp.tool()
async def get_effective_rate_profile() -> dict:
    """
    Auto-detect the account's effective private rates by comparing actual
    billed amounts against public on-demand prices.

    Captures EDP discounts, MOSA/negotiated rates, and private pricing
    automatically from Cost Explorer or CUR, no manual input needed.

    Used internally by the commitment optimizer and PR cost estimator.
    Useful for understanding how large your negotiated discount actually is.

    Examples:
        - "What's our effective AWS discount?"
        - "Do we have private pricing on AWS?"
        - "How does our actual rate compare to on-demand list prices?"
    """
    try:
        from ..recommendations.rate_detector import detect_effective_rates
        profile = detect_effective_rates()
        result: dict = {
            "source": profile.source,
            "confidence": profile.confidence,
            "has_private_pricing": profile.has_private_pricing,
            "overall_discount_pct": round(profile.overall_discount_pct * 100, 1),
            "note": (
                f"Your effective rate is {profile.overall_discount_pct*100:.1f}% below public "
                f"on-demand prices (detected from {profile.source}, confidence: {profile.confidence})."
            ) if profile.has_private_pricing else (
                "No significant private pricing detected. Public on-demand rates apply."
            ),
        }
        if profile.per_service_discount:
            top = sorted(profile.per_service_discount.items(), key=lambda x: x[1], reverse=True)[:8]
            result["top_service_discounts"] = [
                {"service": k, "discount_pct": round(v * 100, 1)} for k, v in top
            ]
        result["metadata"] = profile.metadata
        return result
    except Exception as e:
        return {"error": str(e)}


@_srv.mcp.tool()
async def get_workload_costs(
    namespace: str | None = None,
    kind: str | None = None,
    sort_by: str = "cost",
    context: str | None = None,
    limit: int = 50,
) -> dict:
    """
    Detailed Kubernetes workload cost breakdown with efficiency grades.
    Supports filtering by namespace and workload kind, sorting by cost or waste.

    Args:
        namespace: Filter to a specific namespace (e.g. "production")
        kind:      Filter by workload type: Deployment, StatefulSet, DaemonSet, Job
        sort_by:   "cost" (default) | "waste" | "efficiency" (worst first)
        context:   Kubeconfig context (default: current context)
        limit:     Max workloads returned (default 50)

    Each workload includes: cost, waste, CPU/memory requests vs actual usage,
    efficiency grade (A-F), and pod labels for attribution.

    Examples:
        - "Show me all workload costs in the production namespace"
        - "Which Deployments are wasting the most money?"
        - "Show me StatefulSet costs sorted by waste"
        - "What are the least efficient workloads in the cluster?"
        - "List all DaemonSet costs"
        - "Show me every workload cost sorted by efficiency"
    """
    try:
        from ..connectors.kubernetes import KubernetesConnector
    except ImportError:
        return {"error": "kubernetes package not installed. Run: pip install finops-mcp[kubernetes]"}

    if sort_by not in ("cost", "waste", "efficiency"):
        return {"error": "sort_by must be 'cost', 'waste', or 'efficiency'"}

    try:
        connector = KubernetesConnector()
        if not await connector.is_configured():
            return {"error": "No kubeconfig found. Set KUBECONFIG or ensure ~/.kube/config exists."}

        report = connector.analyze_cluster(context)
        result = connector.get_workload_breakdown(
            report,
            namespace=namespace,
            kind=kind,
            sort_by=sort_by,
            limit=limit,
        )

        total_waste = sum(w.get("wasted_usd", 0) for w in result.get("workloads", []))
        result["summary"] = (
            f"{result['filtered_workloads']} workload(s) in cluster '{report.cluster}' "
            f"(filtered from {result['total_workloads']} total), "
            f"sorted by {sort_by}. "
            f"Estimated waste in view: ${total_waste:,.0f}/mo."
        )

        return result
    except Exception as e:
        _srv.log.exception("Workload cost breakdown failed")
        return {"error": str(e)}


@_srv.mcp.tool()
async def get_top_spending_accounts(limit: int = 10, days_back: int = 30) -> dict:
    """
    Show the highest-spending AWS accounts in the organization.
    Requires a Pro plan (org_reports).

    Args:
        limit: Number of top accounts to return (default 10)
        days_back: Look-back period in days (default 30)

    Examples:
        - "Which 5 accounts are spending the most?"
        - "Show top spending accounts this month"
        - "Which teams are the biggest AWS spenders?"
    """
    if err := _srv.require_pro("org_reports"):
        return err
    try:
        from ..connectors.aws_org import top_spending_accounts
        accounts = top_spending_accounts(limit=limit, days_back=days_back)
        return {"top_accounts": accounts, "days_back": days_back}
    except Exception as e:
        return {"error": str(e)}


@_srv.mcp.tool()
async def get_storage_info() -> dict:
    """
    Show the current storage backend (SQLite local or Postgres shared).
    Helps teams understand whether they're in single-engineer or shared mode.

    Examples:
        - "What database is nable using?"
        - "Are we in shared mode?"
        - "Show storage configuration"
    """
    try:
        from ..storage.db import storage_mode
        info = storage_mode()
        if info["mode"] == "sqlite":
            info["upgrade_note"] = (
                "To share data across your team, set DATABASE_URL=postgresql://user:pass@host/finops "
                "in your environment. All engineers with this URL will share one database."
            )
        else:
            info["note"] = "Running in shared Postgres mode. All team members with DATABASE_URL access the same data."
        return info
    except Exception as e:
        return {"error": str(e)}


@_srv.mcp.tool()
async def get_credit_status(months: int = 6) -> dict:
    """
    Track AWS promotional-credit (Activate) burn-down and detect the moment
    billing flips from credits to cash, the cliff where an early startup first
    feels cost pain. AWS sends no native alert for this.

    Reads Cost Explorer's RECORD_TYPE (Charge type) to separate gross usage,
    credits applied, and net cash per month. No CUR/Athena setup needed. AWS has
    no API for the remaining Activate balance, so runway is inferred from the
    observed monthly credit-consumption trend, not a stated balance.

    Args:
        months: Months of history to analyze (default 6).

    Examples:
        - "Are my AWS credits about to run out?"
        - "When do my credits flip to cash?"
        - "How much of my bill is still covered by credits?"
    """
    from ..demo_data import is_demo, get_demo_response
    if is_demo():
        return get_demo_response("get_credit_status") or {}
    try:
        from ..connectors.credit_tracking import get_credit_status as _gcs
        return await _srv.asyncio.to_thread(_gcs, months)
    except Exception as e:
        return {"error": str(e)}


@_srv.mcp.tool()
async def benchmark_costs(
    account_id: str | None = None,
    vertical: str = "default",
    days: int = 30,
) -> dict:
    """
    Compare this account's spend profile against anonymised peer group medians.

    Shows where you're above or below the median for companies in your industry
    vertical across metrics like: EC2%, RDS%, savings plan coverage, idle
    resource %, LLM spend %, data transfer %, and rightsizing opportunity %.

    Args:
        account_id: AWS account ID to analyse. Auto-discovered from the connected
                    AWS account when omitted.
        vertical:   industry peer group, saas, ecommerce, fintech, media, ai_ml, default
        days:       lookback period for metric calculation

    Returns per-metric comparisons with assessments (better/similar/worse) and insights.
    Examples:
        - "How does our cloud spend compare to similar companies?"
        - "Benchmark our costs"

    """
    account_id = await _srv._resolve_account_id(account_id)
    if not account_id:
        return {"error": "No account_id provided and none could be auto-discovered.",
                "hint": "Connect AWS with `finops setup aws`, or pass account_id explicitly."}
    try:
        from ..analytics.benchmarks import compare
        return compare(account_id=account_id, vertical=vertical, days=days)
    except Exception as e:
        return {"error": str(e)}


@_srv.mcp.tool()
async def estimate_terraform_cost(
    plan_json: str | None = None,
    plan_file: str | None = None,
    tf_dir: str | None = None,
) -> dict:
    """
    Estimate the monthly AWS cost change from a Terraform plan BEFORE applying it.

    Provide one of:
      - plan_json: raw JSON string from `terraform show -json plan.tfplan`
      - plan_file: path to a saved plan JSON file
      - tf_dir:    directory to run `terraform plan` in automatically

    Returns a cost delta breakdown per resource with adds, changes, and removes.
    Prices: AWS on-demand us-east-1. Supports EC2, RDS, Aurora, ElastiCache,
    EKS, NAT Gateways, ALB/NLB, ECS Fargate, Lambda, EBS, OpenSearch, MSK, Redshift.
    Args:
        plan_json: Terraform plan JSON string (`terraform show -json`).
        plan_file: Path to a terraform plan JSON file.
        tf_dir: Terraform directory to plan and price.

    Examples:
        - "What will this terraform plan cost?"
        - "Price the plan in ./infra"

    """
    try:
        from ..connectors.terraform_estimate import estimate_plan, estimate_from_file, estimate_from_dir
        import json as _json

        if plan_json:
            data = _json.loads(plan_json)
            result = estimate_plan(data)
        elif plan_file:
            safe_file = _srv._resolve_safe_path(plan_file, must_exist=True)
            if isinstance(safe_file, dict):
                return safe_file
            result = estimate_from_file(safe_file)
        elif tf_dir:
            safe_dir = _srv._resolve_safe_path(tf_dir, must_exist=True)
            if isinstance(safe_dir, dict):
                return safe_dir
            result = estimate_from_dir(safe_dir)
        else:
            return {
                "error": "Provide plan_json, plan_file, or tf_dir.",
                "usage": (
                    "Run: terraform plan -out=plan.tfplan && "
                    "terraform show -json plan.tfplan > plan.json, "
                    "then pass the file path as plan_file."
                ),
            }

        return result
    except Exception as e:
        return {"error": str(e)}


@_srv.mcp.tool()
async def estimate_change_cost(
    terraform_plan_json: str | None = None,
    terraform_plan_file: str | None = None,
    tf_dir: str | None = None,
    helm_diff: str | None = None,
    monthly_delta_usd: float | None = None,
    budget_name: str = "",
) -> dict:
    """Cost preflight for a proposed change: what it costs and whether it fits budget.

    Agent-native. Call this BEFORE applying an infrastructure change to get a machine
    verdict (ok / warn / over_budget / no_budget) plus the monthly and annual cost
    delta and the budget headroom. Read-only: it estimates and checks, it never applies
    anything.

    Describe the change one of these ways:
      - terraform_plan_json / terraform_plan_file / tf_dir : a Terraform plan
      - helm_diff : output of `helm diff upgrade` or a values.yaml diff
      - monthly_delta_usd : a known monthly cost delta (escape hatch for any change the
        estimators don't parse, e.g. "launch a db.r6g.4xlarge")

    budget_name selects which budget to check against; default is the first active
    budget. With no budget configured the verdict is "no_budget" and the cost delta is
    still returned.

    Good triggers: "will this fit my budget", "what will this terraform/helm change cost
    before I apply it", "cost preflight", "can the agent afford this change".
    Args:
        terraform_plan_json: Terraform plan JSON string to price.
        terraform_plan_file: Path to a terraform plan JSON file.
        tf_dir: Terraform directory to plan and price.
        helm_diff: Helm diff text to price instead of terraform.
        monthly_delta_usd: Known monthly delta, when you already have the number.
        budget_name: Budget to check the delta against.

    Examples:
        - "What would this change cost per month?"
        - "Preflight the cost of this terraform plan"

    """
    from ..preflight import evaluate_preflight

    # 1. Resolve the monthly cost delta + a short breakdown from whichever input is given.
    change_kind: str | None = None
    breakdown: list = []
    delta: float | None = None
    try:
        if terraform_plan_json or terraform_plan_file or tf_dir:
            from ..connectors.terraform_estimate import (
                estimate_plan, estimate_from_file, estimate_from_dir)
            import json as _json
            if terraform_plan_json:
                est = estimate_plan(_json.loads(terraform_plan_json))
            elif terraform_plan_file:
                safe = _srv._resolve_safe_path(terraform_plan_file, must_exist=True)
                if isinstance(safe, dict):
                    return safe
                est = estimate_from_file(safe)
            else:
                safe = _srv._resolve_safe_path(tf_dir, must_exist=True)
                if isinstance(safe, dict):
                    return safe
                est = estimate_from_dir(safe)
            if isinstance(est, dict) and est.get("error"):
                return est
            delta = float(est.get("monthly_delta_usd", 0.0) or 0.0)
            breakdown = (est.get("lines") or [])[:20]
            change_kind = "terraform"
        elif helm_diff:
            from ..connectors.helm import estimate_helm_diff
            d = estimate_helm_diff(diff_text=helm_diff)
            delta = float(d.delta_monthly_usd)
            breakdown = list(d.changes)
            change_kind = "helm"
        elif monthly_delta_usd is not None:
            delta = float(monthly_delta_usd)
            change_kind = "manual"
        else:
            return {"error": "Describe the change: pass terraform_plan_json / "
                             "terraform_plan_file / tf_dir, helm_diff, or monthly_delta_usd."}
    except Exception as e:
        return {"error": f"Could not estimate the change cost: {e}",
                "hint": ("terraform_plan_json must be the JSON from `terraform show -json "
                         "<planfile>` (run `terraform plan -out=tf.plan` first). Or pass "
                         "tf_dir and nable runs the plan itself.")}

    # 2. Budget to check against (first active, or by name). Best-effort: no DB / no
    #    budgets configured falls through to a "no_budget" verdict, never an error.
    budget_for_eval = None
    alert_pct = 80.0
    try:
        from ..budget.enforcer import list_budgets, check_budget
        budgets = list_budgets(active_only=True) or []
        chosen = None
        if budget_name:
            chosen = next((b for b in budgets if b.get("name") == budget_name), None)
        chosen = chosen or (budgets[0] if budgets else None)
        if chosen:
            alert_pct = float(chosen.get("alert_at_pct", 80.0) or 80.0)
            status = check_budget(chosen)
            budget_for_eval = {
                "name": status.get("name", chosen.get("name", "")),
                "limit_usd": status.get("limit", chosen.get("limit_usd", 0)),
                "run_rate_usd": status.get("run_rate_monthly", 0.0),
            }
    except Exception:
        budget_for_eval = None

    # 3. Verdict.
    result = evaluate_preflight(delta, budget=budget_for_eval, alert_pct=alert_pct)
    result["change_kind"] = change_kind
    if breakdown:
        result["breakdown"] = breakdown
    result["summary"] = result["reason"]
    return result


@_srv.mcp.tool()
async def set_business_metrics(
    arr_usd: float | None = None,
    mrr_usd: float | None = None,
    mau: int | None = None,
    dau: int | None = None,
    paying_customers: int | None = None,
    api_calls_monthly: int | None = None,
    employees: int | None = None,
    custom_metrics: dict | None = None,
    notes: str | None = None,
    metric_date: str | None = None,
    cash_on_hand_usd: float | None = None,
    last_raise_amount_usd: float | None = None,
    last_raise_date: str | None = None,
    monthly_opex_usd: float | None = None,
) -> dict:
    """
    Store your business metrics so nable can connect cloud costs to business outcomes.

    Call this once a month (or whenever metrics change) and nable will track trends
    over time and answer "so what?" when your cloud spend changes.

    Args:
        arr_usd:            Annual Recurring Revenue in USD (e.g. 1_200_000 for $1.2M ARR)
        mrr_usd:            Monthly Recurring Revenue in USD. Use this OR arr_usd, not both.
        mau:                Monthly Active Users
        dau:                Daily Active Users
        paying_customers:   Number of paying customers / accounts
        api_calls_monthly:  Your product's API calls per month (not cloud API calls)
        employees:          Total headcount
        custom_metrics:     Any other metric as a dict, e.g. {"free_signups": 4200, "nps": 42}
        notes:              Free-text context, e.g. "Post Series A, hired 8 engineers"
        metric_date:        Date these metrics apply to (YYYY-MM-DD). Defaults to today.
        cash_on_hand_usd:   Cash in the bank, in USD. Powers runway in get_unit_economics().
        last_raise_amount_usd: Size of your last round, in USD.
        last_raise_date:    Date of your last round (YYYY-MM-DD).
        monthly_opex_usd:   Total monthly burn including payroll, in USD. Without this,
                            runway is reported as "infra runway" (excludes payroll);
                            with it, nable reports true company runway.

    Calling this repeatedly for the same date MERGES: fields you omit keep their
    prior value, so you can set revenue one call and cash the next.

    Examples:
        - "Set our MRR to $45,000 and MAU to 1,200"
        - "Update business metrics: ARR $2.4M, 340 paying customers, 8,200 MAU"
        - "Set cash on hand to $2.4M and monthly opex to $210k"
    """

    # Validation: reject nonsensical values loudly instead of storing them.
    for name, val in (
        ("cash_on_hand_usd", cash_on_hand_usd),
        ("last_raise_amount_usd", last_raise_amount_usd),
        ("monthly_opex_usd", monthly_opex_usd),
    ):
        if val is not None and val < 0:
            return {"error": f"{name} cannot be negative (got {val})."}
    for name, val in (("metric_date", metric_date), ("last_raise_date", last_raise_date)):
        if val is not None:
            try:
                _srv.date.fromisoformat(val)
            except ValueError:
                return {"error": f"{name} must be YYYY-MM-DD (got {val!r})."}

    from ..connectors.business_metrics import save_metrics

    date_str = metric_date or _srv.date.today().isoformat()
    result = save_metrics(
        metric_date=date_str,
        arr_usd=arr_usd,
        mrr_usd=mrr_usd,
        mau=mau,
        dau=dau,
        paying_customers=paying_customers,
        api_calls_monthly=api_calls_monthly,
        employees=employees,
        custom_metrics=custom_metrics,
        notes=notes,
        cash_on_hand_usd=cash_on_hand_usd,
        last_raise_amount_usd=last_raise_amount_usd,
        last_raise_date=last_raise_date,
        monthly_opex_usd=monthly_opex_usd,
    )

    stored = {k: v for k, v in {
        "arr_usd": arr_usd, "mrr_usd": mrr_usd, "mau": mau, "dau": dau,
        "paying_customers": paying_customers, "api_calls_monthly": api_calls_monthly,
        "employees": employees, "custom_metrics": custom_metrics,
        "cash_on_hand_usd": cash_on_hand_usd, "last_raise_amount_usd": last_raise_amount_usd,
        "last_raise_date": last_raise_date, "monthly_opex_usd": monthly_opex_usd,
    }.items() if v is not None}

    return {
        **result,
        "stored": stored,
        "tip": (
            "Call get_unit_economics() to see hosting cost as % of MRR, cost per user, "
            "cost per customer, and more. Call explain_cost_change() to understand what "
            "recent cost movements mean for the business."
        ),
    }


@_srv.mcp.tool()
async def get_business_metrics(history_days: int = 90) -> dict:
    """
    Return stored business metrics and trend over time.

    Args:
        history_days: How many days of history to return (default 90).

    Examples:
        - "Show our business metrics"
        - "What business metrics do we have on file?"
        - "Show MRR and MAU history for the last 6 months"
    """

    from ..connectors.business_metrics import resolve_business_metrics, get_metrics_history

    latest = await resolve_business_metrics()
    history = get_metrics_history(days=history_days)

    if not latest or latest.get("_source") == "none":
        return {
            "metrics": None,
            "message": (
                "No business metrics stored yet. "
                "Use set_business_metrics() to record MRR, MAU, paying customers, etc., "
                "or connect Stripe (STRIPE_SECRET_KEY) and nable pulls MRR and paying "
                "customers automatically. Once set, nable connects your cloud costs to "
                "business outcomes."
            ),
        }

    out = {
        "latest": latest,
        "history": history,
        "history_days": history_days,
        "tip": "Call get_unit_economics() to see cost per customer, hosting as % of MRR, and more.",
    }
    if latest.get("_source") in ("stripe", "stored+stripe"):
        out["metrics_source"] = (
            f"MRR and paying customers pulled live from Stripe "
            f"(as of {latest.get('_stripe_as_of')})."
        )
    return out


@_srv.mcp.tool()
async def get_unit_economics(period_days: int = 30) -> dict:
    """
    Connect your total cloud and SaaS spend to business metrics.

    Shows hosting cost as % of MRR/ARR, cost per customer, cost per MAU,
    cost per API call, and other ratios your finance team and investors care about.

    Requires business metrics to be set with set_business_metrics() first.

    Args:
        period_days: Cost window to use for the calculation (default 30 days).

    Examples:
        - "What are our unit economics?"
        - "What's our hosting cost as a percentage of MRR?"
        - "How much does it cost us per customer per month?"
        - "What's our cost per API call?"
        - "Show me our infrastructure unit economics"
    """

    from ..connectors.business_metrics import (
        resolve_business_metrics, compute_unit_economics, compute_runway,
    )

    metrics = await resolve_business_metrics()
    if not (
        metrics.get("mrr_usd") or metrics.get("arr_usd")
        or metrics.get("paying_customers") or metrics.get("mau")
        or metrics.get("employees")
    ):
        return {
            "error": "No business metrics on file.",
            "fix": (
                "Run set_business_metrics(mrr_usd=..., mau=..., paying_customers=...), "
                "or connect Stripe (STRIPE_SECRET_KEY) and nable will pull MRR and "
                "paying customers automatically. Then it connects cost to business outcomes."
            ),
        }

    start = _srv.date.today() - _srv.timedelta(days=period_days)
    end = _srv.date.today()

    active = await _srv._active()
    total_cost, by_provider, by_service = await _srv._gather_costs(active, start, end)

    econ = compute_unit_economics(total_cost, metrics)

    # Normalize the period cost to a monthly burn for the runway calc.
    infra_monthly_burn = total_cost * (30.0 / period_days) if period_days else total_cost
    runway = compute_runway(
        cash_on_hand_usd=metrics.get("cash_on_hand_usd"),
        infra_monthly_burn_usd=infra_monthly_burn,
        monthly_opex_usd=metrics.get("monthly_opex_usd"),
        mrr_usd=metrics.get("mrr_usd") or (metrics.get("arr_usd", 0) / 12 if metrics.get("arr_usd") else None),
    )

    top_services = sorted(by_service.items(), key=lambda x: -x[1])[:5]

    out = {
        "period": f"{start} to {end} ({period_days} days)",
        "total_infrastructure_cost": _srv._fmt_usd(total_cost),
        "unit_economics": econ,
        "runway": runway,
        "by_provider": {k: _srv._fmt_usd(v.get("total_usd", 0)) for k, v in by_provider.items()},
        "top_cost_drivers": [{"service": s, "cost": _srv._fmt_usd(a)} for s, a in top_services],
        "metrics_as_of": metrics.get("metric_date"),
        "tip": (
            "Call explain_cost_change() to understand what recent cost movements "
            "mean for the business in plain English."
        ),
    }
    if metrics.get("_source") in ("stripe", "stored+stripe"):
        out["metrics_source"] = (
            f"MRR and paying customers pulled live from Stripe "
            f"(as of {metrics.get('_stripe_as_of')}). "
            f"Override anytime with set_business_metrics()."
        )
        if metrics.get("_stripe_caveats"):
            out["metrics_caveats"] = metrics["_stripe_caveats"]
    return out


@_srv.mcp.tool()
async def explain_cost_change(
    compare_days: int = 30,
) -> dict:
    """
    Explain what recent cost changes actually mean for the business.

    Compares this period to the previous period across all providers, then
    connects the change to your business metrics to answer: is this spend
    increase growth-driven and healthy, or is it pure cost inflation?

    Requires business metrics set with set_business_metrics().

    Args:
        compare_days: Length of each comparison window in days (default 30).
                      Uses this period vs the same-length period immediately before.

    Examples:
        - "Explain our cost changes this month"
        - "Is our infrastructure spend healthy given our growth?"
        - "Why did our bill go up and does it matter?"
        - "What do the cost changes mean for our gross margin?"
        - "Are we scaling efficiently?"
    """

    from ..connectors.business_metrics import (
        get_latest_metrics, get_metrics_history,
        compute_unit_economics, explain_cost_change as _explain,
    )

    history = get_metrics_history(days=compare_days * 3)
    latest = get_latest_metrics(n=1)

    if not latest:
        return {
            "error": "No business metrics on file.",
            "fix": "Use set_business_metrics() to record MRR, MAU, paying customers, etc.",
        }

    today = _srv.date.today()
    period_end = today
    period_start = today - _srv.timedelta(days=compare_days)
    prev_end = period_start - _srv.timedelta(days=1)
    prev_start = prev_end - _srv.timedelta(days=compare_days)

    active = await _srv._active()
    cost_now, _, by_service_now = await _srv._gather_costs(active, period_start, period_end)
    cost_before, _, by_service_before = await _srv._gather_costs(active, prev_start, prev_end)

    # Use latest metrics for "now" and the oldest available for "before"
    metrics_now = latest[0]
    metrics_before = history[0] if len(history) > 1 else latest[0]
    enough_history = len(history) > 1

    explanation = _explain(
        cost_now=cost_now,
        cost_before=cost_before,
        metrics_now=metrics_now,
        metrics_before=metrics_before,
        period_label=f"{period_start} to {period_end} vs {prev_start} to {prev_end}",
    )

    # Cost-driver attribution: which services moved the bill, ranked by absolute
    # change. This is the "what specifically changed" the unit-economics engine
    # does not compute. Pure data, no LLM call.
    services = set(by_service_now) | set(by_service_before)
    deltas = []
    for svc in services:
        now_v = by_service_now.get(svc, 0.0)
        prev_v = by_service_before.get(svc, 0.0)
        change = now_v - prev_v
        if abs(change) >= 0.01:
            deltas.append({
                "service": svc,
                "before": round(prev_v, 2),
                "now": round(now_v, 2),
                "change_usd": round(change, 2),
                "direction": "up" if change > 0 else "down",
            })
    deltas.sort(key=lambda d: -abs(d["change_usd"]))
    top_drivers = deltas[:5]
    explanation["cost_drivers"] = top_drivers

    if not enough_history:
        explanation["history_note"] = (
            "Only one month of business metrics on file, so the business-metric "
            "comparison reuses the latest values. Record another month with "
            "set_business_metrics() for a true period-over-period read."
        )

    # context_blob: a compact, prompt-ready object the host model turns into prose.
    # nable ships this structured context; it never calls an LLM itself.
    explanation["context_blob"] = {
        "cost_change": explanation.get("cost_change"),
        "verdict": explanation.get("verdict"),
        "signals": explanation.get("signals"),
        "top_cost_drivers": top_drivers,
        "cost_per_customer_now": explanation.get("unit_economics_now", {}).get("cost_per_customer_label"),
        "cost_per_customer_before": explanation.get("unit_economics_before", {}).get("cost_per_customer_label"),
        "instruction": (
            "Write a 2-3 sentence plain-English summary for a founder. State the "
            "cost change, the single biggest driver, and what it means for unit "
            "economics. Do not invent causes beyond top_cost_drivers and signals."
        ),
    }

    return explanation


@_srv.mcp.tool()
async def get_focus_costs(
    start_date: str | None = None,
    end_date: str | None = None,
    provider: str | None = None,
    group_by: str | None = None,
) -> dict:
    """
    Return unified cost data in FOCUS 1.2 format across all connected providers,
    clouds plus supported usage-based SaaS (e.g. Snowflake).

    FOCUS (FinOps Open Cost and Usage Specification) is an open standard for
    normalizing cost data into one vendor-neutral schema. nable extends it past the
    clouds to the usage-based long tail, so you can query total spend across AWS,
    Azure, GCP, and SaaS providers in a single shape.

    Args:
        start_date: ISO date string (YYYY-MM-DD). Defaults to 30 days ago.
        end_date:   ISO date string (YYYY-MM-DD). Defaults to today.
        provider:   Optional filter, e.g. "aws", "azure", "gcp", "snowflake". Omit for all.
        group_by:   Optional grouping. One of "ServiceName", "ServiceCategory",
                    "RegionId", "SubAccountId". Returns aggregated totals when set.

    Returns:
        FOCUS 1.2 normalized cost records with fields: BilledCost, EffectiveCost,
        ServiceName, ServiceCategory, ProviderName, RegionId, SubAccountId, Tags, etc.
    Examples:
        - "Show costs in FOCUS format grouped by service category"

    """
    if err := _srv.require_role("viewer"):
        return err

    from ..focus import normalize as _focus_normalize
    from dataclasses import asdict

    sd, ed = _srv._default_dates()
    if start_date:
        try:
            sd = _srv.date.fromisoformat(start_date)
        except ValueError:
            return {"error": f"Invalid start_date: {start_date!r}. Use YYYY-MM-DD."}
    sd = _srv._clamp_start_date(sd)
    if end_date:
        try:
            ed = _srv.date.fromisoformat(end_date)
        except ValueError:
            return {"error": f"Invalid end_date: {end_date!r}. Use YYYY-MM-DD."}

    # Fan out across every FOCUS-capable source: clouds, usage-based SaaS, and the
    # aggregated LLM/AI providers. Shared with slice_costs so coverage stays in sync.
    all_records, errors, providers = await _srv._fetch_focus_records(sd, ed, provider)

    if provider and not all_records:
        from ..connectors.llm_costs import _LLM_FOCUS_NAMES
        p = provider.lower()
        perr = errors.get(p)
        _llm_names = set(_LLM_FOCUS_NAMES) | {v.lower() for v in _LLM_FOCUS_NAMES.values()}
        if perr == "unknown provider" and p not in _llm_names and p not in ("llm", "ai"):
            _capable = sorted(n for n, c in {**_srv.CLOUD_CONNECTORS, **_srv.SAAS_CONNECTORS}.items()
                              if hasattr(c, "get_costs_as_focus"))
            return {"error": f"Provider {provider!r} does not emit FOCUS yet. FOCUS-capable: "
                             f"{', '.join(_capable) or 'none'}, plus AI providers "
                             f"(openai, anthropic, openrouter, litellm)."}
        if perr == "not configured":
            return {"error": f"Provider {provider!r} is not configured. Run 'finops-mcp setup' to connect it."}
        if p in _llm_names or p in ("llm", "ai"):
            return {"error": f"Provider {provider!r} is not configured or has no spend in the selected range."}

    if not all_records and errors:
        return {"error": "All providers failed", "details": errors}

    if not all_records:
        return {"error": "No FOCUS-capable providers are configured. Connect AWS, Azure, GCP, a "
                         "supported usage-based provider like Snowflake, or an AI provider like OpenAI."}

    # Serialize records to dicts, converting datetime fields to ISO strings
    def _serialize(rec) -> dict:
        d = asdict(rec)
        for key in ("BillingPeriodStart", "BillingPeriodEnd", "ChargePeriodStart", "ChargePeriodEnd"):
            if d.get(key):
                d[key] = d[key].isoformat()
        return d

    serialized = [_serialize(r) for r in all_records]

    # Apply grouping if requested
    valid_group_by = {"ServiceName", "ServiceCategory", "RegionId", "SubAccountId"}
    if group_by and group_by in valid_group_by:
        grouped: dict[str, dict] = {}
        for rec in all_records:
            key_val = getattr(rec, group_by, None) or "__none__"
            if key_val not in grouped:
                grouped[key_val] = {
                    "key": key_val,
                    "group_by": group_by,
                    "BilledCost": 0.0,
                    "EffectiveCost": 0.0,
                    "ListCost": 0.0,
                    "record_count": 0,
                    "providers": set(),
                }
            g = grouped[key_val]
            g["BilledCost"] = round(g["BilledCost"] + rec.BilledCost, 4)
            g["EffectiveCost"] = round(g["EffectiveCost"] + rec.EffectiveCost, 4)
            g["ListCost"] = round(g["ListCost"] + rec.ListCost, 4)
            g["record_count"] += 1
            g["providers"].add(rec.ProviderName)

        # Convert sets to sorted lists for JSON serialization
        grouped_list = []
        for g in sorted(grouped.values(), key=lambda x: -x["BilledCost"]):
            g["providers"] = sorted(g["providers"])
            grouped_list.append(g)

        return {
            "focus_version": "1.2",
            "period": {"start": sd.isoformat(), "end": ed.isoformat()},
            "providers_queried": providers,
            "group_by": group_by,
            "total_billed_cost": round(sum(r.BilledCost for r in all_records), 4),
            "total_effective_cost": round(sum(r.EffectiveCost for r in all_records), 4),
            "record_count": len(all_records),
            "grouped": grouped_list,
            **({"errors": errors} if errors else {}),
        }

    # Token-aware cap: keep records until we hit the response budget rather than a
    # flat row count, so the model never receives a ledger that costs more to read
    # than the answer is worth. Records carry no inherent priority, so this is a
    # last resort; the cheap path is group_by, which aggregates server-side.
    kept, omitted = _srv.fit_to_budget(serialized)
    return {
        "focus_version": "1.2",
        "period": {"start": sd.isoformat(), "end": ed.isoformat()},
        "providers_queried": providers,
        "total_billed_cost": round(sum(r.BilledCost for r in all_records), 4),
        "total_effective_cost": round(sum(r.EffectiveCost for r in all_records), 4),
        "record_count": len(serialized),
        **({"records_truncated": True, "hint": f"Showing {len(kept)} of {len(serialized)} records to stay within token budget. Use group_by=ServiceName for a complete aggregated view at a fraction of the tokens."} if omitted else {}),
        "records": kept,
        **({"errors": errors} if errors else {}),
    }


@_srv.mcp.tool()
async def slice_costs(
    dimensions: list[str] | None = None,
    filters: list[dict] | None = None,
    exclusions: list[dict] | None = None,
    metric: str = "EffectiveCost",
    granularity: str = "TOTAL",
    order_by: str = "metric",
    limit: int = 50,
    start_date: str | None = None,
    end_date: str | None = None,
    provider: str | None = None,
    title: str | None = None,
    via: str = "auto",
) -> dict:
    """
    Slice cloud cost any way you want. This is the flexible, moldable cost query:
    group and filter by ANY combination of dimensions, over any date range, instead
    of a fixed set of canned reports. Returns both the numbers and a `card` describing
    how to chart them (which the UI can render and pin to the dashboard).

    Dimensions (group by, up to 3): ServiceName, ServiceCategory, ProviderName,
    RegionId, RegionName, SubAccountId, SubAccountName, ResourceId, ResourceName,
    ResourceType, ChargeCategory, ChargeDescription, CommitmentDiscountId,
    CommitmentDiscountType, plus "date" (a time series, use granularity) and
    "Tags[<key>]" for any tag (e.g. "Tags[team]"). For line-item detail (AWS only,
    needs CUR + Athena set up): "usage_type", "instance_type", "resource_id" — using
    any of these auto-routes the query to the CUR pushdown.

    filters / exclusions: each is {dimension, op, values}. op is one of eq, in, neq,
    not_in, contains, regex. filters keep matching rows; exclusions drop matching rows.
    Example "EC2 by region last 90 days, excluding Savings Plan credits":
      dimensions=["RegionId"], filters=[{"dimension":"ServiceName","op":"eq","values":["Amazon EC2"]}],
      exclusions=[{"dimension":"ChargeCategory","op":"in","values":["Credit"]}], metric="EffectiveCost"

    metric: BilledCost | EffectiveCost (amortized, default) | ListCost.
    granularity: TOTAL | DAILY | MONTHLY (only matters when "date" is a dimension).
    order_by: "metric" (default, descending) or a dimension name.
    start_date / end_date: YYYY-MM-DD (default last 30 days). provider: aws|azure|gcp (default all).
    via: "auto" (default; CUR only when a line-item dimension is requested), "focus", or "cur".

    This is read-only: it slices and charts cost data. It never changes anything.
    Args:
        dimensions: Fields to group by (e.g. ["service", "region"]).
        filters: Include-filters, {field: [values]}.
        exclusions: Exclude-filters, {field: [values]}.
        metric: "cost" (default) or another supported metric.
        granularity: "DAILY" or "MONTHLY".
        order_by: Sort field, defaults to the metric descending.
        limit: Max rows to return.
        start_date: ISO date (YYYY-MM-DD). Defaults to 30 days ago.
        end_date: ISO date (YYYY-MM-DD). Defaults to today.
        provider: Limit to one provider (e.g. "aws"). None = all.
        title: Optional title for the resulting card.
        via: Internal: how the slice was invoked.

    """
    if err := _srv.require_role("viewer"):
        return err
    from ..slice import parse_spec, run_slice
    from ..slice.engine import derive_card
    from ..slice.spec import SliceSpecError

    try:
        spec = parse_spec({
            "dimensions": dimensions or [],
            "filters": filters or [],
            "exclusions": exclusions or [],
            "metric": metric,
            "granularity": granularity,
            "order_by": order_by,
            "limit": limit,
        })
    except SliceSpecError as exc:
        return {"error": str(exc)}

    sd, ed = _srv._default_dates()
    if start_date:
        try:
            sd = _srv.date.fromisoformat(start_date)
        except ValueError:
            return {"error": f"Invalid start_date: {start_date!r}. Use YYYY-MM-DD."}
    sd = _srv._clamp_start_date(sd)
    if end_date:
        try:
            ed = _srv.date.fromisoformat(end_date)
        except ValueError:
            return {"error": f"Invalid end_date: {end_date!r}. Use YYYY-MM-DD."}

    # Route to the CUR/Athena pushdown for line-item dimensions (usage_type etc.).
    from ..slice.spec import needs_cur
    via = (via or "auto").lower()
    if via not in ("auto", "focus", "cur"):
        via = "auto"
    if via == "focus" and needs_cur(spec):
        return {"error": "usage_type / instance_type / resource_id need the CUR path; drop via='focus'."}
    if via == "cur" or (via == "auto" and needs_cur(spec)):
        from ..connectors.cur import is_configured as _cur_ok
        if not _cur_ok():
            return {"error": ("Slicing by usage_type / instance_type / resource_id needs the CUR + "
                              "Athena integration (AWS). Set CUR_S3_BUCKET, CUR_ATHENA_DATABASE, "
                              "CUR_ATHENA_TABLE, CUR_ATHENA_RESULTS_BUCKET.")}
        from ..slice import cur_engine
        try:
            result = await _srv.asyncio.to_thread(cur_engine.run_slice_cur, spec, sd, ed)
        except Exception as exc:
            _srv.log.error("CUR slice failed: %s", exc)
            return {"error": f"CUR slice failed: {exc}"}
        card = derive_card(spec, result, title=title)
        period = {"start": sd.isoformat(), "end": ed.isoformat()}
        return {
            "result": {**result.to_dict(), "period": period, "providers": ["aws"], "via": "cur"},
            "card": {**card.to_dict(), "period": period, "days": (ed - sd).days, "via": "cur"},
            "metric_note": cur_engine.METRIC_NOTE,
        }

    records, errors, providers = await _srv._fetch_focus_records(sd, ed, provider)
    if not records:
        if errors:
            return {"error": "Could not fetch cost data", "details": errors}
        return {"error": "No cost data for that range. Connect a provider with 'finops setup', or widen the dates."}

    result = run_slice(spec, records)
    card = derive_card(spec, result, title=title)
    period = {"start": sd.isoformat(), "end": ed.isoformat()}
    return {
        "result": {**result.to_dict(), "period": period, "providers": providers},
        "card": {**card.to_dict(), "period": period, "days": (ed - sd).days},
        **({"partial_errors": errors} if errors else {}),
    }


@_srv.mcp.tool()
async def list_active_services(
    provider: str = "",
    start_date: str = "",
    end_date: str = "",
) -> dict:
    """
    List every cloud service that has spend in the period, across AWS, Azure, and GCP.

    Use this to discover what services are running before querying a specific one.
    Returns services sorted by cost so you can see the top drivers at a glance.

    Works for any service, EC2, RDS, ElastiCache, AppSync, Kendra, IoT Core,
    WorkSpaces, Pinpoint, or anything else in your account.

    Args:
        provider:   "aws", "azure", "gcp", or blank for all connected providers.
        start_date: ISO date (YYYY-MM-DD). Defaults to 30 days ago.
        end_date:   ISO date. Defaults to today.

    Examples:
        - "What services are we running on AWS?"
        - "Show me all GCP services with spend this month"
        - "What cloud services do we use?"
    """
    from ..connectors.universal import list_all_services
    from datetime import date, timedelta

    end = date.fromisoformat(end_date) if end_date else date.today()
    start = date.fromisoformat(start_date) if start_date else end - timedelta(days=30)

    result = list_all_services(
        provider=provider.lower() if provider else None,
        start_date=start,
        end_date=end,
    )

    # Bound token cost: a noisy org can have 200+ services per cloud. Cap each
    # provider's detail list to the top 50 by cost, but always keep the full
    # service count and full spend total so totals are never lost.
    TOP_N = 50
    for prov in ("aws", "azure", "gcp"):
        services = result.get(prov)
        if not isinstance(services, list):
            continue
        services.sort(key=lambda r: r.get("cost_usd", 0) or 0, reverse=True)
        total_count = len(services)
        total_usd = round(sum((r.get("cost_usd", 0) or 0) for r in services), 2)
        result[f"{prov}_service_count"] = total_count
        result[f"{prov}_total_usd"] = total_usd
        if total_count > TOP_N:
            kept = services[:TOP_N]
            omitted = total_count - TOP_N
            shown_usd = round(sum((r.get("cost_usd", 0) or 0) for r in kept), 2)
            result[prov] = kept
            result[f"{prov}_truncated"] = (
                f"showing top {TOP_N} of {total_count} {prov} services by cost "
                f"(${shown_usd:,.2f} of ${total_usd:,.2f} shown); "
                f"{omitted} smaller services omitted. Use get_service_cost for a specific one."
            )

    return result


@_srv.mcp.tool()
async def get_service_cost(
    service_name: str,
    provider: str = "",
    start_date: str = "",
    end_date: str = "",
    granularity: str = "DAILY",
) -> dict:
    """
    Get cost breakdown for any named cloud service on AWS, Azure, or GCP.

    Handles any service, common ones like EC2 and RDS, or less common ones
    like AppSync, Kendra, MSK, WorkSpaces, IoT Core, Pinpoint, Forecast,
    MemoryDB, Clean Rooms, Lake Formation, and 200+ others.

    Short names and abbreviations are resolved automatically:
      "ElastiCache" → "Amazon ElastiCache"
      "MSK" or "Kafka" → "Amazon Managed Streaming for Apache Kafka"
      "Step Functions" → "AWS Step Functions"

    If the service name is ambiguous, returns a list of close matches.

    Args:
        service_name: Name of the service (short name or full name both work).
        provider:     "aws", "azure", "gcp", or blank to auto-detect.
        start_date:   ISO date. Defaults to 30 days ago.
        end_date:     ISO date. Defaults to today.
        granularity:  "DAILY" or "MONTHLY".

    Examples:
        - "How much did we spend on ElastiCache this month?"
        - "Show me AppSync costs for the last 7 days"
        - "What's our MSK spend?"
        - "How much are we spending on Azure Cognitive Services?"
        - "Show me GCP BigQuery costs"
    """
    from ..connectors.universal import get_any_service_cost
    from datetime import date, timedelta

    if not service_name:
        return {"error": "service_name is required."}

    end = date.fromisoformat(end_date) if end_date else date.today()
    start = date.fromisoformat(start_date) if start_date else end - timedelta(days=30)

    return get_any_service_cost(
        service_name=service_name,
        provider=provider.lower() if provider else None,
        start_date=start,
        end_date=end,
        granularity=granularity,
    )


# The collapse rule lives with the sweep that produces the findings it folds.
# Re-exported under its original private name because it is the audit's own
# vocabulary at the call site below, and the tests that pin the 141-156%
# overstatement import it from here.
from ..recommendations.sweep import collapse_per_resource as _collapse_per_resource


@_srv.mcp.tool()
async def run_full_cost_audit(
    regions: list[str] | None = None,
    top_n: int = 10,
) -> str:
    """
    Run a full cost optimization audit across all connected AWS resources.
    Use this when the user explicitly asks for a full audit, cost scan, or
    optimization sweep. For simple cost questions ("what did I spend last month?")
    prefer get_cost_summary or get_costs_by_service, they are faster and cheaper.

    Good triggers: "run a cost audit", "scan for savings", "find waste",
    "full optimization report", "what should I optimize?".
    Not needed for: point-in-time cost queries, single-service questions, forecasts.

    Covers: Graviton, public IPv4, Lambda concurrency, S3 Bucket Keys,
    non-prod scheduling, RDS snapshots, spot adoption, CloudWatch cardinality,
    CloudWatch orphaned alarms, Logs IA migration, Lambda SnapStart, EFS cross-AZ,
    NLB cross-zone, S3 IT, S3 Transfer Acceleration, EBS replication, Database SPs,
    idle/orphaned resources (unattached EBS, unused EIPs, old snapshots, stopped
    EC2, idle load balancers), and idle RDS instances (no connections in 14d).

    Each scanner runs independently. After showing results, ask the user which
    opportunity to investigate first.

    After showing results, offer to export with: 'Want me to export these to CSV?'
    Args:
        regions: AWS regions to scan. Defaults to all enabled regions.
        top_n: How many top results to return.

    Examples:
        - "Run a full cost audit"
        - "Find everything we could save"

    """
    if err := _srv.require_role("analyst"):
        return err

    aws = _srv.CLOUD_CONNECTORS.get("aws")
    if aws is None or not await aws.is_configured():
        return "AWS is not connected. Call connect_aws right here in the chat (it detects credentials already on this machine), or run 'uvx nable' in a terminal."

    from ..recommendations.sweep import sweep

    result = await sweep(aws, regions)
    if result.timed_out:
        return ("The audit took unusually long (a region or API may be slow). Try a single "
                "scan such as get_rightsizing_recommendations, or pass a specific region.")

    findings = result.findings
    errors = result.errors
    learned_note = result.learned_note

    top = findings[:top_n]

    if not top:
        return "No savings opportunities found. Your infrastructure looks well-optimized, or no AWS account is connected."

    # A finding the critique retracted carries None, not a number, and it must not
    # be counted toward a total we are asking someone to believe. `or 0` keeps it
    # out of the headline while the row below still shows its magnitude band.
    #
    # Collapse first. Twenty-one scanners each look at the same account from a
    # different angle, and several of them land on the same resource with answers
    # that are alternatives, not additions: an instance can be migrated to
    # Graviton, or converted to Spot, or scheduled off out of hours, or shut down
    # as idle. You can bank one of those, not all four. Summed raw, the audit
    # claimed 141-156% of an instance's own cost as savings on it, which is not a
    # number anyone can act on and not a number that survives a customer checking
    # it. Counting only the best per resource is conservative and defensible; the
    # alternatives stay attached to the winner so nothing is hidden.
    top, collapsed = _collapse_per_resource(top)

    total_monthly = sum(f.get("monthly_savings") or 0 for f in top)
    total_annual = total_monthly * 12

    def _savings_cell(f: dict) -> str:
        """The dollar figure, or the band when the claim did not survive review."""
        v = f.get("monthly_savings")
        if v is None:
            return f"{f.get('magnitude', 'unconfirmed')} (needs confirming)"
        return f"${v:,.2f}"

    # Show a Confidence column only when at least one shown finding carries real
    # learned signal, so a cold ledger keeps the original clean 4-column table.
    def _confidence_label(f: dict) -> str:
        learned = f.get("learned") or {}
        verdict = learned.get("source_verdict")
        coverage = learned.get("coverage")
        if not verdict or coverage in (None, "COLD"):
            return ""
        if verdict == "boost":
            return "you act on these"
        if verdict == "suppress":
            return "rarely acted on"
        return "neutral"

    show_confidence = any(_confidence_label(f) for f in top)

    lines = [
        f"## Cost Audit, Top {len(top)} Opportunities",
        f"**Estimated monthly saving: ${total_monthly:,.2f} | Annual: ${total_annual:,.2f}**"
        + (f"  \n*{collapsed} alternative fix(es) on the same resources are not counted in this total: you can bank one fix per resource, not all of them. They are listed against the winning row.*" if collapsed else ""),
        "",
    ]
    if show_confidence:
        lines.append("| # | Opportunity | Category | Monthly Saving | Confidence (your ledger) |")
        lines.append("|---|-------------|----------|---------------|--------------------------|")
        for i, f in enumerate(top, 1):
            conf = _confidence_label(f) or "-"
            lines.append(f"| {i} | {f['title']} | {f['category']} | {_savings_cell(f)} | {conf} |")
    else:
        lines.append("| # | Opportunity | Category | Monthly Saving |")
        lines.append("|---|-------------|----------|---------------|")
        for i, f in enumerate(top, 1):
            lines.append(f"| {i} | {f['title']} | {f['category']} | {_savings_cell(f)} |")

    if learned_note:
        lines.append("")
        lines.append(f"*{learned_note}*")

    lines.append("")
    lines.append("*Run any individual tool for full details and remediation commands.*")
    lines.append("")
    lines.append("**What do you want to do with these results?**")
    lines.append("- `export to CSV`, save to ~/Downloads for Excel or Sheets")
    lines.append("- `publish to Notion`, share with your team (requires NOTION_API_KEY)")
    lines.append("- `push to n8n`, trigger your automation workflow")
    lines.append("- `tell me more about #1`, deep dive on the top opportunity")

    if errors:
        lines.append(f"\n*Scanners skipped (no data or not configured): {', '.join(errors)}*")

    body = "\n".join(lines)
    # Make the token cost visible against the savings found. Under our pricing,
    # the customer pays for the tool, so this is the ROI made explicit: pennies
    # of context against dollars of monthly waste.
    lines.append(f"\n*{_srv.cost_note(body, savings_found_usd=total_monthly)}*")
    return "\n".join(lines)


@_srv.mcp.tool()
async def explain_recent_cost_drivers(
    days: int = 30,
    top_n: int = 10,
) -> dict:
    """
    Explain what drove cost changes across all connected providers in the last N days.

    Compares this period to the same-length period before it, finds the top drivers
    of increase and decrease, and summarizes the net change. Works on the free tier
    without requiring business metrics.

    Use when:
        - "Why did my bill go up?"
        - "What changed in our costs this month?"
        - "Show me the top cost drivers vs last month"
        - "Which services had the biggest cost changes?"
        - "What's driving our AWS spend increase?"

    Args:
        days:  Comparison window length in days (default 30)
        top_n: Number of top drivers to return (default 10)
    Examples:
        - "Why did costs go up this week?"
        - "What drove spend recently?"

    """
    from ..demo_data import is_demo, get_demo_response
    if is_demo():
        return get_demo_response("explain_recent_cost_drivers") or {}
    try:
        today = _srv.date.today()
        period_end = today
        period_start = today - _srv.timedelta(days=days)
        prev_end = period_start
        prev_start = period_start - _srv.timedelta(days=days)

        active = await _srv._active()
        if not active:
            return {
                "error": "No providers connected.",
                "fix": "Run 'finops setup aws' (or azure/gcp/datadog) to connect a provider.",
            }

        # _gather_costs returns (grand_total, by_provider, grand_by_service).
        # We diff the per-service breakdown, so take the third element, not the
        # float total (taking [0] caused "'float' object has no attribute 'keys'").
        _, _, cost_now = await _srv._gather_costs(active, period_start, period_end)
        _, _, cost_prev = await _srv._gather_costs(active, prev_start, prev_end)

        # Build per-provider + per-service breakdown
        drivers: list[dict] = []
        all_keys: set = set(cost_now.keys()) | set(cost_prev.keys())
        for key in all_keys:
            now_val = cost_now.get(key, 0.0)
            prev_val = cost_prev.get(key, 0.0)
            delta = now_val - prev_val
            if abs(delta) < 1.0:
                continue
            pct = (delta / prev_val * 100) if prev_val > 0 else None
            drivers.append({
                "key": key,
                "current": round(now_val, 2),
                "previous": round(prev_val, 2),
                "delta": round(delta, 2),
                "delta_pct": round(pct, 1) if pct is not None else None,
                "direction": "increase" if delta > 0 else "decrease",
            })

        drivers.sort(key=lambda x: abs(x["delta"]), reverse=True)
        top = drivers[:top_n]

        increases = [d for d in drivers if d["direction"] == "increase"]
        decreases = [d for d in drivers if d["direction"] == "decrease"]
        total_increase = sum(d["delta"] for d in increases)
        total_decrease = sum(abs(d["delta"]) for d in decreases)
        net_change = total_increase - total_decrease

        total_now = sum(cost_now.values())
        total_prev = sum(cost_prev.values())
        net_pct = ((total_now - total_prev) / total_prev * 100) if total_prev > 0 else None

        return {
            "period": f"{period_start} to {period_end}",
            "comparison_period": f"{prev_start} to {prev_end}",
            "total_current_usd": round(total_now, 2),
            "total_previous_usd": round(total_prev, 2),
            "net_change_usd": round(net_change, 2),
            "net_change_pct": round(net_pct, 1) if net_pct is not None else None,
            "top_increases": [d for d in top if d["direction"] == "increase"][:5],
            "top_decreases": [d for d in top if d["direction"] == "decrease"][:5],
            "all_drivers": top,
            "summary": (
                f"Costs {'increased' if net_change >= 0 else 'decreased'} by "
                f"${abs(net_change):,.0f} "
                f"({'+' if net_change >= 0 else ''}{round(net_pct, 1) if net_pct is not None else 'N/A'}%) "
                f"vs the prior {days}-day period. "
                f"{len(increases)} services had cost increases, {len(decreases)} had decreases."
            ),
        }
    except Exception as exc:
        _srv.log.error("explain_recent_cost_drivers failed: %s", exc)
        return {"error": str(exc)}


@_srv.mcp.tool()
async def get_nable_roi(
    period_days: int = 90,
) -> dict:
    """
    Shows the return on investment from using nable: savings found, acted on, and verified
    versus the cost of the tool itself.

    This report is unique to nable, no other FinOps tool can show this calculation
    because they cost more per month than many teams' actual savings.

    Use when:
        - "Is nable worth it?"
        - "How much has nable saved us?"
        - "Show me the ROI on using nable"
        - "What's the payback period on the Pro plan?"
        - "How do savings compare to the subscription cost?"

    Args:
        period_days: Lookback window for savings (default 90 days)
    Examples:
        - "What has nable saved us versus what it costs?"
        - "Show nable ROI"

    """
    try:
        from ..storage.db import get_engine, savings_recommendations
        from sqlalchemy import select
        from datetime import datetime, timedelta, timezone

        _SOLO_MONTHLY_USD = 0.0

        lic = _srv.get_status()
        plan = lic.plan
        monthly_cost = _srv._PRO_MONTHLY_USD if plan in ("pro", "enterprise") else _SOLO_MONTHLY_USD
        period_cost = monthly_cost * (period_days / 30)

        cutoff = datetime.now(timezone.utc) - timedelta(days=period_days)
        sr = savings_recommendations
        engine = get_engine()

        with engine.connect() as conn:
            rows = conn.execute(select(sr).where(sr.c.generated_at >= cutoff)).fetchall()

        found_total = sum(r.estimated_monthly_savings_usd or 0 for r in rows if r.status not in ("dismissed", "expired"))
        acted_total = sum(r.estimated_monthly_savings_usd or 0 for r in rows if r.status in ("acted_on", "verified"))
        # Verified banked savings = money that actually left the bill. Use ONLY the
        # measured verified amount, never the predicted estimate. A verified row is
        # money nable confirmed against live cloud state; conflating it with the
        # estimate would overstate what was actually banked.
        verified_total = sum(
            r.verified_monthly_savings_usd or 0
            for r in rows if r.status == "verified"
        )
        verified_count = sum(1 for r in rows if r.status == "verified")

        found_annualized = found_total * 12
        acted_annualized = acted_total * 12
        verified_annualized = verified_total * 12
        annual_tool_cost = monthly_cost * 12

        roi_on_verified = ((verified_total - monthly_cost) / monthly_cost * 100) if monthly_cost > 0 else None
        payback_months = (monthly_cost / verified_total) if verified_total > 0 else None

        # Hero line: lead with verified banked savings, the number that actually
        # left the bill, kept clearly distinct from predicted/found opportunity.
        if verified_total > 0:
            hero = (f"**Verified banked savings: ${verified_total:,.0f}/mo "
                    f"(${verified_annualized:,.0f}/yr), confirmed against live cloud state "
                    f"across {verified_count} change{'s' if verified_count != 1 else ''}.**")
        else:
            hero = ("**Verified banked savings: $0/mo so far.** This tracks money that "
                    "actually left your bill. Mark recommendations acted on, then run "
                    "verify_savings() to bank the first confirmed savings.")

        lines = [
            f"## nable ROI Report ({period_days}-day window)",
            "",
            hero,
            "",
            f"**Tool cost:** ${period_cost:,.0f} over {period_days} days "
            f"(${monthly_cost:.0f}/mo · {plan} plan)",
            "",
            "### Savings pipeline",
            f"- Found (predicted): ${found_total:,.0f}/mo in opportunities ({len(rows)} recommendations)",
            f"- Acted on (predicted, pending verification): ${acted_total:,.0f}/mo",
            f"- Verified (banked, actually left the bill): ${verified_total:,.0f}/mo",
            "",
        ]

        if monthly_cost == 0:
            lines += [
                "### ROI",
                f"**Solo plan is free.** You're getting ${found_total:,.0f}/mo in recommendations at zero cost.",
                f"Annualized opportunity: ${found_annualized:,.0f}.",
                "",
                "Upgrade to Pro ($25/mo) to unlock auto-remediation and verified savings tracking.",
                f"At ${verified_total:,.0f}/mo verified savings, payback is "
                f"{'less than 1 week' if verified_total > 0 else 'immediate once first savings are verified'}.",
            ]
        else:
            roi_str = f"{roi_on_verified:.0f}%" if roi_on_verified is not None else "N/A"
            payback_str = f"{payback_months:.1f} months" if payback_months and payback_months > 0 else "immediate"
            lines += [
                "### ROI",
                f"- Monthly net savings (after tool cost): ${max(0, verified_total - monthly_cost):,.0f}",
                f"- Annualized verified savings: ${verified_annualized:,.0f}",
                f"- Annualized tool cost: ${annual_tool_cost:,.0f}",
                f"- ROI on verified savings: {roi_str}",
                f"- Payback period: {payback_str}",
            ]
            if verified_total > monthly_cost * 5:
                lines.append(f"\n**nable is paying for itself {verified_total / monthly_cost:.0f}x over.**")
            elif verified_total > 0:
                lines.append("\n**Verified savings cover tool cost.** Act on remaining recommendations to grow ROI.")
            else:
                lines.append("\n**No verified savings yet.** Run verify_savings() after acting on recommendations.")

        lines += [
            "",
            "### Competitor comparison",
            "| Tool | Cost at your savings level | What you get |",
            "|------|---------------------------|-------------|",
            f"| nable (this) | ${annual_tool_cost:,.0f}/yr | Multi-cloud + SaaS + AI, local-first |",
            f"| CloudHealth | ~${int(verified_annualized * 0.025):,}/yr (2.5% of managed spend) | Dashboard, enterprise only |",
            f"| Cloudability | ~${int(verified_annualized * 0.015):,}/yr (1.5% of spend) | Dashboard, no AI |",
            "| ProsperOps | 30-35% of RI savings | RI management only |",
        ]

        return {
            "summary": "\n".join(lines),
            "period_days": period_days,
            "plan": plan,
            "monthly_cost_usd": monthly_cost,
            "found_monthly_usd": round(found_total, 2),
            "acted_monthly_usd": round(acted_total, 2),
            "verified_monthly_usd": round(verified_total, 2),
            # Explicit banked figure: money confirmed to have left the bill. Same
            # value as verified_monthly_usd, named so a caller can't confuse it
            # with predicted/found savings.
            "verified_banked_monthly_usd": round(verified_total, 2),
            "verified_banked_annual_usd": round(verified_annualized, 2),
            "verified_count": verified_count,
            "found_annualized_usd": round(found_annualized, 2),
            "verified_annualized_usd": round(verified_annualized, 2),
            "annual_tool_cost_usd": round(annual_tool_cost, 2),
            "roi_pct": round(roi_on_verified, 1) if roi_on_verified is not None else None,
            "payback_months": round(payback_months, 1) if payback_months else None,
        }
    except Exception as exc:
        _srv.log.error("get_nable_roi failed: %s", exc)
        return {"error": str(exc)}
