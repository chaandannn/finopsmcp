# SPDX-License-Identifier: Apache-2.0
"""llm MCP tools (extracted from server.py; see finops/tools/__init__.py).

Server-local helpers, globals, and the mcp instance are reached through the live
server module (_srv.NAME) so monkeypatching finops.server.* still works and no
import-order coupling exists."""
from __future__ import annotations

from .. import server as _srv


@_srv.mcp.tool()
async def get_ai_engineering_report(days: int = 30, repos: list[str] | None = None,
                                    unit: str = "auto") -> dict:
    """What your AI coding tools actually shipped, by model, and what it cost.

    Attributes each unit of work to the AI model or agent that wrote it (Claude
    Code names the exact model in its commit trailer, so Claude work resolves to
    the model; Copilot, Codex, Cursor, and Devin resolve to the tool), sizes each
    high/medium/low by diff, and joins LLM spend by model. The line it produces:
    "Opus 4.8 was 49% of AI spend and shipped 10 PRs: 3 high, 5 medium, 2 low,
    $X per PR."

    unit picks the unit of work: "pr" (merged pull requests), "commit" (commits on
    the default branch, for teams that push straight to main with no PRs), or
    "auto" (default: PRs if the repo has any in the window, else commits). The unit
    actually used comes back in the "unit" field of the result.

    Needs GITHUB_TOKEN and GITHUB_ORGS connected, or pass explicit repos like
    ["owner/name"]. Read-only.

    Good triggers: "what has AI shipped", "AI engineering output", "which model
    wrote the most code", "cost per PR by model", "cost per commit", "is our AI
    spend producing work".
    Args:
        days: Look-back window in days (default 30).
        repos: Git repos to include (owner/name). All configured repos when omitted.
        unit: Business unit for cost-per-unit math (e.g. "pr", "commit").

    Examples:
        - "What has AI coding shipped this month and what did it cost?"
        - "AI engineering report for the last 14 days"

    """
    if (err := _srv.require_pro("ai_unit_economics")):
        return err
    from ..demo_data import is_demo, get_demo_response
    if is_demo():
        return get_demo_response("get_ai_engineering_report") or {"configured": False}
    from ..connectors.github_contributions import build_report
    return await build_report(days=days, repos=repos, unit=unit)


@_srv.mcp.tool()
async def get_llm_costs(
    days: int = 30,
    start_date: str | None = None,
    end_date: str | None = None,
) -> dict:
    """
    Aggregate AI/LLM spend across all configured providers, OpenAI, Anthropic,
    AWS Bedrock, Azure OpenAI, and Vertex AI.

    Shows total spend, breakdown by provider, breakdown by model, daily trend,
    and model-switching recommendations to reduce costs.

    Args:
        days: Lookback window in days (default 30). Ignored if start_date set.
        start_date: ISO date string (YYYY-MM-DD). Optional.
        end_date: ISO date string (YYYY-MM-DD). Defaults to today.

    Examples:
        - "How much have we spent on AI APIs this month?"
        - "What's our total LLM spend across OpenAI and Bedrock?"
        - "Show AI cost breakdown by model for the last 7 days"
        - "Which AI models are we spending the most on?"
    """
    from ..demo_data import is_demo, get_demo_response
    if is_demo():
        return get_demo_response("get_llm_costs") or {}
    try:
        from datetime import date as _date
        sd = _date.fromisoformat(start_date) if start_date else None
        ed = _date.fromisoformat(end_date) if end_date else _date.today()
        from ..connectors.llm_costs import get_all_llm_costs
        result = await _srv.asyncio.to_thread(get_all_llm_costs, start_date=sd, end_date=ed, days=days)

        # Bound token cost: by_model can be unbounded (many models), daily can be
        # a long window. Trim DETAIL only; keep every total and count intact.
        by_model_full = result.get("by_model", {}) or {}
        result["model_count"] = len(by_model_full)
        if len(by_model_full) > 50:
            # by_model is already sorted desc by cost in the connector
            top_items = list(by_model_full.items())[:50]
            kept_total = round(sum(v for _, v in top_items), 4)
            result["by_model"] = dict(top_items)
            result["by_model_truncated"] = (
                f"showing top 50 of {len(by_model_full)} models by cost "
                f"(${kept_total:,.2f} of ${result.get('total_usd', 0.0):,.2f} total); "
                f"use get_llm_cost_by_model with a provider filter for the full list"
            )

        daily = result.get("daily", []) or []
        result["daily_point_count"] = len(daily)
        if len(daily) > 45:
            period_total = round(sum(d.get("total_usd", 0.0) for d in daily), 4)
            vals = [d.get("total_usd", 0.0) for d in daily]
            # Weekly buckets preserve trend without one row per day.
            weekly = []
            for i in range(0, len(daily), 7):
                chunk = daily[i:i + 7]
                weekly.append({
                    "week_start": chunk[0].get("date", ""),
                    "week_end":   chunk[-1].get("date", ""),
                    "total_usd":  round(sum(c.get("total_usd", 0.0) for c in chunk), 4),
                })
            result["daily"] = daily[-14:]            # most recent 14 days verbatim
            result["weekly"] = weekly                # full window, bucketed
            result["daily_summary"] = {
                "period_total_usd": period_total,
                "min_usd":          round(min(vals), 4),
                "max_usd":          round(max(vals), 4),
                "avg_usd":          round(period_total / len(vals), 4),
            }
            result["daily_truncated"] = (
                f"{len(daily)} days bucketed to weekly; showing last 14 days verbatim. "
                f"Use a shorter days window for full daily detail"
            )

        return result
    except Exception as e:
        return {"error": str(e)}


@_srv.mcp.tool()
async def get_gpu_infra_costs(
    days: int = 30,
    start_date: str | None = None,
    end_date: str | None = None,
) -> dict:
    """
    Report spend status across serverless-GPU / inference-infra providers,    Modal, Together, and Replicate. For the model-builder slice of AI startups
    this is the single largest variable cost, billed per GPU-second inside each
    vendor's own dashboard and invisible to any cloud bill.

    Honest note: these vendors gate per-range cost behind paid plans or omit it
    from their public API. nable confirms each credential and reports what's
    reachable; until a usable usage endpoint exists, track these bills via the
    invoice email parser.

    Args:
        days: Lookback window in days (default 30). Ignored if start_date set.
        start_date: ISO date string (YYYY-MM-DD). Optional.
        end_date: ISO date string (YYYY-MM-DD). Defaults to today.

    Examples:
        - "How much are we spending on Modal / Replicate / Together?"
        - "Show my GPU inference infra costs"
        - "Is my Modal account connected?"
    """
    from ..demo_data import is_demo, get_demo_response
    if is_demo():
        return get_demo_response("get_gpu_infra_costs") or {}
    try:
        from datetime import date as _date, timedelta as _td
        ed = _date.fromisoformat(end_date) if end_date else _date.today()
        sd = _date.fromisoformat(start_date) if start_date else ed - _td(days=days)
        from ..connectors.saas.gpu_infra import get_all_gpu_infra_costs
        return await _srv.asyncio.to_thread(get_all_gpu_infra_costs, sd, ed)
    except Exception as e:
        return {"error": str(e)}


@_srv.mcp.tool()
async def get_llm_commitment_analysis(days: int = 30) -> dict:
    """
    Optimize token spend against committed AI contracts: prepaid credits, Azure
    OpenAI PTUs, AWS Bedrock Provisioned Throughput, and enterprise rate cards.
    This is Reserved-Instance analysis for tokens. nable prices you against your
    ACTUAL negotiated terms, not list, which a provider dashboard cannot do.

    For each contract it reports coverage, utilization, your effective $/Mtok
    versus on-demand, break-even utilization, a right-size recommendation, and
    runway. With no contract configured it tells you whether your on-demand spend
    is high and stable enough to justify buying one.

    Configure contracts via the FINOPS_AI_CONTRACTS env var (a JSON array) or
    ~/.finops-mcp/ai_contracts.json. Terms stay on your machine.

    Args:
        days: Lookback window for observed usage (default 30).

    Examples:
        - "Are we utilizing our Azure OpenAI PTUs?"
        - "What's our effective token rate versus on-demand?"
        - "Should we buy provisioned throughput for our token spend?"
        - "Are we clearing our Anthropic enterprise minimum?"
    """
    from ..demo_data import is_demo, get_demo_response
    if is_demo():
        return get_demo_response("get_llm_commitment_analysis") or {}
    try:
        from ..connectors.llm_costs import get_all_llm_costs
        from ..analytics.llm_commitments import (
            load_contracts, analyze_portfolio, recommend_commitment,
            total_tokens, EXAMPLE_CONTRACTS)
        data = await _srv.asyncio.to_thread(get_all_llm_costs, None, None, days)
        contracts = load_contracts()

        if not contracts:
            daily = [d.get("total_usd", 0.0) for d in (data.get("daily") or [])]
            monthly = float(data.get("total_usd", 0.0)) * (30.0 / max(1, days))
            return {
                "configured_contracts": 0,
                "recommendation": recommend_commitment(daily, monthly),
                "how_to_add_contracts": (
                    "Set FINOPS_AI_CONTRACTS to a JSON array, or write "
                    "~/.finops-mcp/ai_contracts.json. nable then prices you against "
                    "your real terms, not list."),
                "example_contracts": EXAMPLE_CONTRACTS,
            }

        credit_analysis = None
        if any((c.get("type") or "").lower() == "credits" for c in contracts):
            try:
                from ..connectors.credit_tracking import get_credit_status as _gcs
                credit_analysis = await _srv.asyncio.to_thread(_gcs, 6)
            except Exception:
                credit_analysis = None

        usage = {
            "tokens": total_tokens(data.get("by_model_tokens")),
            "spend_usd": float(data.get("total_usd", 0.0)),
            "days": days,
            "credit_analysis": credit_analysis,
        }
        result = analyze_portfolio(contracts, usage)
        result["configured_contracts"] = len(contracts)
        result["window_days"] = days
        return result
    except Exception as e:
        return {"error": str(e)}


@_srv.mcp.tool()
async def forecast_llm_costs(horizon_days: int = 90, balance_usd: float | None = None) -> dict:
    """
    Forecast AI/LLM token spend and, if you give a balance, the date your credits
    or commitment run out. Uses nable's per-account forecaster (Holt-Winters with
    linear and naive fallbacks by history length) on your daily token-cost series.

    Headline outputs: projected next-30-day spend, implied month-over-month
    growth, and the runway-to-exhaustion date. That exhaustion date is what
    finance wants and what no provider dashboard gives.

    Args:
        horizon_days: How far forward to project (default 90).
        balance_usd: Remaining credit/commitment balance to burn down (optional).

    Examples:
        - "Forecast our AI spend for the next quarter"
        - "When will our $100k in credits run out at this rate?"
        - "Is our token bill accelerating?"
    """
    if (err := _srv.require_pro("forecasting")):
        return err
    from ..demo_data import is_demo, get_demo_response
    if is_demo():
        return get_demo_response("forecast_llm_costs") or {}
    try:
        from ..connectors.llm_costs import get_all_llm_costs
        from ..analytics.token_forecast import forecast_token_spend
        data = await _srv.asyncio.to_thread(get_all_llm_costs, None, None, 90)
        daily = data.get("daily") or []
        return await _srv.asyncio.to_thread(forecast_token_spend, daily, horizon_days, balance_usd)
    except Exception as e:
        return {"error": str(e)}


@_srv.mcp.tool()
async def get_ai_spend_monitor(days: int = 30) -> dict:
    """
    On-demand view of what nable's daily AI-spend monitor watches: a spike or drop
    on your token-spend series, plus commitment contracts that need attention
    (capacity under-utilized, enterprise minimum shortfall, commitment expiring).
    The scheduler runs this daily and alerts via Slack; this returns the same view
    on demand.

    Args:
        days: Lookback window in days (default 30).

    Examples:
        - "Did our token spend spike?"
        - "Is any AI commitment being wasted right now?"
    """
    if (err := _srv.require_pro("ai_unit_economics")):
        return err
    from ..demo_data import is_demo, get_demo_response
    if is_demo():
        return get_demo_response("get_ai_spend_monitor") or {}
    try:
        from datetime import date as _date
        from ..connectors.llm_costs import get_all_llm_costs
        from ..analytics.llm_commitments import load_contracts, analyze_portfolio, total_tokens
        from ..anomaly.detector import detect_for_series
        data = await _srv.asyncio.to_thread(get_all_llm_costs, None, None, days)
        series = [float(d.get("total_usd", 0.0)) for d in (data.get("daily") or [])
                  if isinstance(d, dict)]

        anomaly = None
        if len(series) >= 2:
            res = detect_for_series("ai", "LLM tokens", "llm", _date.today(), series[-1], series[:-1])
            if res:
                anomaly = {"direction": res.direction, "severity": res.severity,
                           "pct_change": res.pct_change, "summary": res.summary()}

        contracts = [c for c in load_contracts() if (c.get("type") or "").lower() != "credits"]
        attention: list = []
        if contracts:
            usage = {"tokens": total_tokens(data.get("by_model_tokens")),
                     "spend_usd": float(data.get("total_usd", 0.0)), "days": days,
                     "credit_analysis": None}
            attention = analyze_portfolio(contracts, usage).get("needs_attention", [])

        return {
            "window_days": days,
            "total_usd": round(float(data.get("total_usd", 0.0)), 2),
            "spend_anomaly": anomaly,
            "contracts_needing_attention": attention,
            "note": "Daily token-spend anomaly plus commitment contracts needing attention. "
                    "The scheduler alerts on these via Slack; this is the on-demand view.",
        }
    except Exception as e:
        return {"error": str(e)}


@_srv.mcp.tool()
async def get_llm_cost_by_model(
    days: int = 30,
    provider: str | None = None,
) -> dict:
    """
    Break down AI/LLM costs by individual model with efficiency metrics.

    Shows cost per model, estimated tokens consumed, cost per 1M tokens,
    and which models have cheaper alternatives for the same task class.

    Args:
        days: Lookback window in days (default 30).
        provider: Filter to a specific provider, "openai", "anthropic", "bedrock".
                  Leave blank to see all providers.

    Examples:
        - "Which of our AI models costs the most?"
        - "Show me OpenAI model cost breakdown"
        - "How much are we spending on GPT-4o vs GPT-4o-mini?"
        - "What would we save switching from Claude Opus to Sonnet?"
    """
    from ..demo_data import is_demo, get_demo_response
    if is_demo():
        return get_demo_response("get_llm_cost_by_model") or {}
    try:
        from datetime import date as _date, timedelta
        ed = _date.today()
        sd = ed - timedelta(days=days)
        from ..connectors.llm_costs import get_all_llm_costs
        result = await _srv.asyncio.to_thread(get_all_llm_costs, start_date=sd, end_date=ed)

        if provider:
            # Filter to specific provider
            prov_cost = result["by_provider"].get(provider, 0.0)
            return {
                "provider":    provider,
                "total_usd":   prov_cost,
                "by_model":    dict(sorted(result["by_model"].items(), key=lambda kv: kv[1], reverse=True)[:50]),
                "period":      result["period"],
                "recommendations": result.get("recommendations", []),
            }

        return {
            "period":          result["period"],
            "total_usd":       result["total_usd"],
            "by_provider":     result["by_provider"],
            "by_model":        result["by_model"],
            "top_spenders":    result["top_spenders"],
            "recommendations": result.get("recommendations", []),
        }
    except Exception as e:
        return {"error": str(e)}


@_srv.mcp.tool()
async def get_langfuse_model_costs(
    days: int = 30,
    start_date: str | None = None,
    end_date: str | None = None,
) -> dict:
    """
    Break down LLM spend and token usage by model from Langfuse observability data.

    Shows cost and token consumption for every model tracked in Langfuse, useful
    for understanding which models are driving spend and optimizing model selection.

    Requires:
        LANGFUSE_PUBLIC_KEY and LANGFUSE_SECRET_KEY in environment.
        Optional: LANGFUSE_HOST (defaults to https://cloud.langfuse.com)

    Args:
        days:       lookback window in days (default 30, ignored if start/end provided)
        start_date: ISO date string YYYY-MM-DD
        end_date:   ISO date string YYYY-MM-DD

    Returns cost per model, tokens per model, and cost-per-1k-token efficiency.

    Examples:
        - "Show me our LLM costs by model in Langfuse"
        - "Which model is costing us the most in Langfuse?"
        - "What's our cost per 1k tokens for GPT-4 vs Claude?"
    """
    try:
        connector: _srv.LangfuseConnector = _srv.SAAS_CONNECTORS["langfuse"]  # type: ignore
        if not await connector.is_configured():
            return {
                "error": "Langfuse not configured",
                "help": "Set LANGFUSE_PUBLIC_KEY and LANGFUSE_SECRET_KEY in your environment.",
            }

        if start_date and end_date:
            sd = _srv.date.fromisoformat(start_date)
            ed = _srv.date.fromisoformat(end_date)
        else:
            ed = _srv.date.today()
            sd = ed - _srv.timedelta(days=days)

        result = await connector.get_usage_by_model(start_date=sd, end_date=ed)

        models = result.get("models", [])
        # models is pre-sorted by total_cost_usd desc in the connector.
        result["total_models"] = len(models)
        kept, omitted = _srv.fit_to_budget(models, max_tokens=6000)
        result["models"] = kept
        if omitted > 0:
            shown_cost = round(sum(m.get("total_cost_usd", 0) for m in kept), 4)
            result["models_truncated"] = (
                f"showing top {len(kept)} of {result['total_models']} models by cost "
                f"(${shown_cost:,.2f} of ${result.get('total_cost_usd', 0):,.2f} total); "
                f"{omitted} smaller-spend models omitted. Narrow the date window to see the tail."
            )
        return result
    except Exception as e:
        return {"error": str(e)}


@_srv.mcp.tool()
async def get_langfuse_trace_volume(
    days: int = 30,
    start_date: str | None = None,
    end_date: str | None = None,
) -> dict:
    """
    Daily trace and observation counts from Langfuse, usage volume over time.

    Use this to identify request spikes, growth trends, or unexpected volume surges
    that may be driving LLM cost increases.

    Requires LANGFUSE_PUBLIC_KEY and LANGFUSE_SECRET_KEY.

    Args:
        days:       lookback window in days (default 30)
        start_date: ISO date string YYYY-MM-DD
        end_date:   ISO date string YYYY-MM-DD

    Examples:
        - "How many LLM traces did we run this month in Langfuse?"
        - "Show me daily AI request volume for the last 30 days"
        - "Was there a spike in Langfuse traces last week?"
    """
    try:
        connector: _srv.LangfuseConnector = _srv.SAAS_CONNECTORS["langfuse"]  # type: ignore
        if not await connector.is_configured():
            return {
                "error": "Langfuse not configured",
                "help": "Set LANGFUSE_PUBLIC_KEY and LANGFUSE_SECRET_KEY in your environment.",
            }

        if start_date and end_date:
            sd = _srv.date.fromisoformat(start_date)
            ed = _srv.date.fromisoformat(end_date)
        else:
            ed = _srv.date.today()
            sd = ed - _srv.timedelta(days=days)

        return await connector.get_trace_volume(start_date=sd, end_date=ed)
    except Exception as e:
        return {"error": str(e)}


@_srv.mcp.tool()
async def get_ai_kpis(
    days: int = 30,
    infra_total_usd: float | None = None,
) -> dict:
    """
    Full AI cost health dashboard with actionable KPIs.

    Runs all AI cost health metrics in one call:
      - Prompt cache hit rate and estimated savings (Anthropic)
      - Context window utilisation per model (are you paying for 200K context but using 2K?)
      - Model sprawl score (Herfindahl index of model concentration)
      - Peak usage day-of-week and weekend vs weekday patterns
      - Prompt efficiency (output/input token ratio, flags verbose or wrong-model usage)
      - Error spend estimate (tokens wasted on failed requests)
      - AI vs infrastructure spend ratio (benchmark: healthy SaaS = 5–15%)

    Each finding includes an estimated monthly savings amount and specific
    remediation advice.

    Args:
        days:            Lookback window in days (default 30).
        infra_total_usd: Your total cloud infrastructure spend for the same period.
                         Pass this to get AI-vs-infra ratio benchmarking.

    Examples:
        - "Show me our AI cost health dashboard"
        - "What's our prompt cache hit rate?"
        - "Are we using the right AI models?"
        - "How efficient are our AI prompts?"
        - "What AI cost optimisations should we prioritise?"
    """
    if (err := _srv.require_pro("ai_unit_economics")):
        return err
    try:
        from datetime import date as _date, timedelta
        ed = _date.today()
        sd = ed - timedelta(days=days)

        from ..connectors.llm_costs import get_all_llm_costs
        from ..connectors.saas.anthropic_usage import get_costs as anthropic_costs, is_configured as anth_configured
        from ..analytics.ai_kpis import full_kpi_report

        llm_result = await _srv.asyncio.to_thread(get_all_llm_costs, start_date=sd, end_date=ed)

        # Fetch Anthropic data separately for cache analysis
        anthropic_data = None
        if await anth_configured():
            try:
                anthropic_data = await _srv.asyncio.to_thread(anthropic_costs, sd, ed)
            except Exception as e:
                _srv.log.debug("Anthropic data fetch for KPI: %s", e)

        return full_kpi_report(
            llm_costs_result=llm_result,
            anthropic_data=anthropic_data,
            infra_total_usd=infra_total_usd,
        )
    except Exception as e:
        return {"error": str(e)}


@_srv.mcp.tool()
async def optimize_ai_spend(days: int = 30) -> dict:
    """
    Ranked, dollar-quantified plan to cut your AI/LLM bill, across OpenAI,
    Anthropic, AWS Bedrock, Azure OpenAI, and Vertex.

    This is the OpenRouter question answered as analysis, not a proxy: the
    cheapest way to get the same output. It decomposes spend into its real
    driver (model choice vs token size vs request volume) and returns the
    levers ranked by monthly dollars saved:

      - Model routing: move lower-complexity calls to a cheaper sibling model
        (priced from real input/output ratios, not a guessed percentage)
      - Prompt caching: raise your Anthropic cache hit rate so repeated input
        bills at ~10% of price
      - Output reduction: trim verbose responses (output is the pricier side)
      - Error reduction: stop paying for failed requests
      - Model consolidation: collapse model sprawl into clear tiers

    Only levers with a grounded basis carry a savings number; governance levers
    are listed without inflating the headline. Output-trim savings are skipped
    for any model that already has a routing recommendation, so nothing is
    counted twice. nable never sits in your request path; it reads, ranks, and
    can open the PR.

    Args:
        days: Lookback window in days (default 30). Savings are normalized to a
              30-day month.

    Examples:
        - "How do I cut our AI bill?"
        - "Where is the waste in our LLM spend?"
        - "What's the cheapest way to run the same workloads?"
        - "Optimize our token and model costs."
    """
    from ..demo_data import is_demo
    if is_demo():
        # Run the real planner over demo LLM data so the wedge actually
        # demonstrates (routing + caching levers, dollar savings), no creds.
        from ..demo_data import llm_costs as _demo_llm, bedrock_split as _demo_split
        from ..analytics.ai_optimizer import build_optimization_plan
        plan = build_optimization_plan(_demo_llm(), days=days, bedrock_split=_demo_split())
        plan["_demo_mode"] = True
        return plan
    try:
        from datetime import date as _date, timedelta
        ed = _date.today()
        sd = ed - timedelta(days=days)

        from ..connectors.llm_costs import get_all_llm_costs, bedrock_token_cost_split
        from ..connectors.saas.anthropic_usage import get_costs as anthropic_costs, is_configured as anth_configured
        from ..analytics.ai_kpis import full_kpi_report
        from ..analytics.ai_optimizer import build_optimization_plan

        llm_result = await _srv.asyncio.to_thread(get_all_llm_costs, start_date=sd, end_date=ed)

        anthropic_data = None
        if await anth_configured():
            try:
                anthropic_data = await _srv.asyncio.to_thread(anthropic_costs, sd, ed)
            except Exception as e:
                _srv.log.debug("Anthropic data fetch for optimizer: %s", e)

        # Bedrock input/output/cache cost split from Cost Explorer (best effort).
        try:
            bedrock_split = bedrock_token_cost_split(sd, ed)
        except Exception as e:
            _srv.log.debug("Bedrock token split for optimizer: %s", e)
            bedrock_split = None

        kpi = full_kpi_report(llm_costs_result=llm_result, anthropic_data=anthropic_data)
        return build_optimization_plan(llm_result, kpi, days=days, bedrock_split=bedrock_split)
    except Exception as e:
        return {"error": str(e)}


@_srv.mcp.tool()
async def recommend_bedrock_model_routing(days: int = 30) -> dict:
    """
    Analyzes Bedrock model usage to find invocations that could route to
    cheaper models without quality loss. Sonnet costs 20x more than Haiku.
    Classification, extraction, and short-context tasks rarely need Sonnet.

    Identifies which Lambda functions are using Sonnet for tasks that Haiku
    handles equally well, and estimates monthly savings from routing.

    Use this when:
        - Bedrock is a top cost driver
        - User asks about LLM costs or AI spend
        - User asks how to reduce Bedrock costs
        - User wants to optimize model usage
        - "Why is my Bedrock bill so high?"
        - "Can I use a cheaper model?"

    Args:
        days: Number of days to analyze (default 30).
    Examples:
        - "Could cheaper Bedrock models handle some of our load?"

    """
    try:
        from ..recommendations.bedrock_routing import recommend_bedrock_model_routing as _recommend
        region = _srv.os.getenv("AWS_DEFAULT_REGION", "us-east-1")
        return _recommend(days=days, region=region)
    except Exception as e:
        return {"error": f"Bedrock routing analysis unavailable: {e}"}
