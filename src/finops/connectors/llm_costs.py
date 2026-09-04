"""
LLM cost aggregator — normalizes spend across all AI providers.

Sources:
  - OpenAI (GPT-4o, o3, embeddings, DALL-E, Whisper)
  - Anthropic (Claude 3.x / 3.5 / 3.7)
  - AWS Bedrock (Claude, Llama, Titan, Mistral, Nova via Cost Explorer)
  - Azure OpenAI (via Azure Cost Management)
  - Google Vertex AI (via Cloud Billing)
  - Together AI, Cohere, Mistral AI (via API usage endpoints)

All costs normalised to USD. Token counts normalised to per-1M-token rates
for cross-model efficiency comparison.
"""
from __future__ import annotations

import logging
from datetime import date, timedelta
from typing import Any

log = logging.getLogger(__name__)

# Bedrock model pricing per 1M tokens (on-demand, us-east-1, May 2026)
_BEDROCK_PRICING: dict[str, dict[str, float]] = {
    # Anthropic on Bedrock
    "anthropic.claude-3-5-sonnet-20241022-v2:0": {"input": 3.00,  "output": 15.00},
    "anthropic.claude-3-5-haiku-20241022-v1:0":  {"input": 0.80,  "output": 4.00},
    "anthropic.claude-3-opus-20240229-v1:0":     {"input": 15.00, "output": 75.00},
    "anthropic.claude-3-sonnet-20240229-v1:0":   {"input": 3.00,  "output": 15.00},
    "anthropic.claude-3-haiku-20240307-v1:0":    {"input": 0.25,  "output": 1.25},
    # Meta Llama
    "meta.llama3-70b-instruct-v1:0":             {"input": 0.99,  "output": 0.99},
    "meta.llama3-8b-instruct-v1:0":              {"input": 0.22,  "output": 0.22},
    "meta.llama3-1-405b-instruct-v1:0":          {"input": 5.32,  "output": 16.00},
    # Amazon Nova
    "amazon.nova-pro-v1:0":                      {"input": 0.80,  "output": 3.20},
    "amazon.nova-lite-v1:0":                     {"input": 0.06,  "output": 0.24},
    "amazon.nova-micro-v1:0":                    {"input": 0.035, "output": 0.14},
    # Mistral
    "mistral.mistral-large-2402-v1:0":           {"input": 4.00,  "output": 12.00},
    "mistral.mistral-7b-instruct-v0:2":          {"input": 0.15,  "output": 0.20},
    # Amazon Titan
    "amazon.titan-text-premier-v1:0":            {"input": 0.50,  "output": 1.50},
    "amazon.titan-text-lite-v1":                 {"input": 0.15,  "output": 0.20},
    "amazon.titan-embed-text-v2:0":              {"input": 0.02,  "output": 0.00},
    # Canonical ids for Bedrock Claude SKU display names. Cost Explorer reports
    # Bedrock spend as per-model SKUs ("Claude Sonnet 4.5") with no model-id
    # string, so _normalize_model_id maps them to these keys. Prices mirror
    # recommendations.bedrock_routing.MODEL_PRICING.
    "claude-sonnet-4-5":                         {"input": 3.00,  "output": 15.00},
    "claude-sonnet-4-6":                         {"input": 3.00,  "output": 15.00},
    "claude-haiku-3-5":                          {"input": 0.80,  "output": 4.00},
    "claude-haiku-3":                            {"input": 0.25,  "output": 1.25},
    "claude-opus-4":                             {"input": 15.00, "output": 75.00},
}


def get_bedrock_costs(start_date: date, end_date: date) -> dict[str, Any]:
    """Fetch Bedrock costs from Cost Explorer, broken down by model."""
    # Cost Explorer bills PER REQUEST against the customer's own account, and
    # this function makes two. job_ai_monitor runs it nightly at 05:00 with
    # nobody watching, so an unguarded path here charges every customer every
    # night for a report they did not ask for. The guard is the same one the
    # rest of the codebase uses; this module simply never consulted it.
    #
    # Returning empty rather than raising: the AI monitor also covers OpenAI and
    # Anthropic, which cost nothing to read, and a Bedrock gap should not take
    # the whole job down. The reason string says which gap it is.
    from ..billing_access import cost_explorer_allowed

    if not cost_explorer_allowed():
        return _empty("cost_explorer_not_allowed_unattended")

    try:
        import boto3
    except ImportError:
        return _empty("boto3_missing")

    try:
        ce = boto3.client("ce", region_name="us-east-1")
        period = {"Start": start_date.isoformat(), "End": end_date.isoformat()}
        # AWS labels Bedrock spend under service names that vary and change over
        # time: plain "Amazon Bedrock" plus per-model SKUs like
        # "Claude Sonnet 4.5 (Amazon Bedrock Edition)". Cost Explorer's SERVICE
        # filter is exact-match only (no contains), and ce:GetDimensionValues is
        # not in nable's minimum read-only policy. So discover the names with a
        # SERVICE-grouped GetCostAndUsage (the same permission cost queries use),
        # then pull daily model detail filtered to just those services.
        discover = ce.get_cost_and_usage(
            TimePeriod=period,
            Granularity="MONTHLY",
            GroupBy=[{"Type": "DIMENSION", "Key": "SERVICE"}],
            Metrics=["UnblendedCost"],
        )
        bedrock_services = sorted({
            g["Keys"][0]
            for r in discover.get("ResultsByTime", [])
            for g in r.get("Groups", [])
            if "bedrock" in g["Keys"][0].lower()
        })
        if not bedrock_services:
            return _empty("no_bedrock_services")
        resp = ce.get_cost_and_usage(
            TimePeriod=period,
            Granularity="DAILY",
            Filter={"Dimensions": {"Key": "SERVICE", "Values": bedrock_services}},
            GroupBy=[
                {"Type": "DIMENSION", "Key": "SERVICE"},
                {"Type": "DIMENSION", "Key": "USAGE_TYPE"},
            ],
            Metrics=["UnblendedCost"],
        )
    except Exception as e:
        log.warning("Bedrock cost fetch failed: %s", e)
        return _empty("ce_error")

    total = 0.0
    by_model: dict[str, float] = {}
    daily: list[dict] = []

    for result in resp.get("ResultsByTime", []):
        day = result["TimePeriod"]["Start"]
        day_total = 0.0
        day_by_model: dict[str, float] = {}

        for group in result.get("Groups", []):
            service, usage_type = group["Keys"][0], group["Keys"][1]
            if "bedrock" not in service.lower():
                continue  # belt-and-suspenders; filter should already exclude these
            amount = float(group["Metrics"]["UnblendedCost"]["Amount"])
            day_total += amount
            # Prefer the model id encoded in the usage type
            # (e.g. "USE1-anthropic.claude-3-5-sonnet..."). Real Bedrock model
            # ids always contain a vendor dot ("anthropic.", "meta.", "amazon.").
            # When AWS bills the model as its own service SKU, the usage type is
            # generic, so fall back to the service name (suffix stripped).
            model_id = _extract_model_from_usage_type(usage_type)
            if "." not in model_id:
                model_id = service.replace(" (Amazon Bedrock Edition)", "").strip() or service
            day_by_model[model_id] = day_by_model.get(model_id, 0.0) + amount
            by_model[model_id] = by_model.get(model_id, 0.0) + amount

        total += day_total
        daily.append({"date": day, "total_usd": round(day_total, 4),
                      "by_model": {k: round(v, 4) for k, v in day_by_model.items()}})

    return {
        "total_usd": round(total, 4),
        "by_model":  {k: round(v, 4) for k, v in
                      sorted(by_model.items(), key=lambda x: x[1], reverse=True)},
        "daily":     daily,
        "source":    "cost_explorer",
    }


def bedrock_token_cost_split(start_date: date, end_date: date) -> dict[str, Any]:
    """
    Split Bedrock spend into input, output, and cache cost from Cost Explorer.

    Bedrock bills each token kind as its own usage type (InputTokenCount,
    OutputTokenCount, CacheReadInputTokenCount, ...). That split is enough to
    spot the most common AI waste without any CloudWatch data: an input-heavy
    bill running with no prompt caching. Cost-only on purpose; per-model token
    quantities from Cost Explorer are not reliable, but the dollar split is.

    Returns {} when Bedrock is not in use or the query is denied.
    """
    try:
        import boto3
    except ImportError:
        return {}
    try:
        ce = boto3.client("ce", region_name="us-east-1")
        period = {"Start": start_date.isoformat(), "End": end_date.isoformat()}
        discover = ce.get_cost_and_usage(
            TimePeriod=period, Granularity="MONTHLY",
            GroupBy=[{"Type": "DIMENSION", "Key": "SERVICE"}], Metrics=["UnblendedCost"],
        )
        services = sorted({
            g["Keys"][0]
            for r in discover.get("ResultsByTime", [])
            for g in r.get("Groups", [])
            if "bedrock" in g["Keys"][0].lower()
        })
        if not services:
            return {}
        resp = ce.get_cost_and_usage(
            TimePeriod=period, Granularity="MONTHLY",
            Filter={"Dimensions": {"Key": "SERVICE", "Values": services}},
            GroupBy=[
                {"Type": "DIMENSION", "Key": "SERVICE"},
                {"Type": "DIMENSION", "Key": "USAGE_TYPE"},
            ],
            Metrics=["UnblendedCost"],
        )
    except Exception as e:
        log.warning("Bedrock token split fetch failed: %s", e)
        return {}

    buckets = {"input": 0.0, "output": 0.0, "cache_read": 0.0, "cache_write": 0.0, "other": 0.0}
    by_model: dict[str, dict[str, float]] = {}
    for r in resp.get("ResultsByTime", []):
        for g in r.get("Groups", []):
            service, ut = g["Keys"][0], g["Keys"][1]
            amt = float(g["Metrics"]["UnblendedCost"]["Amount"])
            if amt == 0:
                continue
            u = ut.lower()
            if "cache" in u and "read" in u:
                kind = "cache_read"
            elif "cache" in u and ("write" in u or "creat" in u):
                kind = "cache_write"
            elif "input" in u:
                kind = "input"
            elif "output" in u:
                kind = "output"
            else:
                kind = "other"
            buckets[kind] += amt
            model = service.replace(" (Amazon Bedrock Edition)", "").strip() or service
            m = by_model.setdefault(model, {"input": 0.0, "output": 0.0,
                                            "cache_read": 0.0, "cache_write": 0.0, "other": 0.0})
            m[kind] += amt

    total = sum(buckets.values())
    if total <= 0:
        return {}
    cache_cost = buckets["cache_read"] + buckets["cache_write"]
    return {
        "input_cost": round(buckets["input"], 2),
        "output_cost": round(buckets["output"], 2),
        "cache_read_cost": round(buckets["cache_read"], 2),
        "cache_write_cost": round(buckets["cache_write"], 2),
        "other_cost": round(buckets["other"], 2),
        "total": round(total, 2),
        "input_share_pct": round(buckets["input"] / total * 100, 1),
        "output_share_pct": round(buckets["output"] / total * 100, 1),
        "caching_active": cache_cost > 0,
        "by_model": {k: {kk: round(vv, 2) for kk, vv in v.items()} for k, v in by_model.items()},
    }


def _extract_model_from_usage_type(usage_type: str) -> str:
    """
    Bedrock usage types look like:
      USE1-anthropic.claude-3-5-sonnet-20241022-v2:0-input-tokens
    Extract the model ID portion.
    """
    # Strip region prefix (USE1-, EUW1-, etc.)
    parts = usage_type.split("-", 1)
    if len(parts) == 2 and len(parts[0]) <= 5:
        usage_type = parts[1]
    # Strip suffix (-input-tokens, -output-tokens, etc.)
    for suffix in ["-input-tokens", "-output-tokens", "-invocations"]:
        if usage_type.endswith(suffix):
            usage_type = usage_type[:-len(suffix)]
    return usage_type


def get_all_llm_costs(
    start_date: date | None = None,
    end_date: date | None = None,
    days: int = 30,
    include_provider_results: bool = False,
    exclude_cloud_native: bool = False,
) -> dict[str, Any]:
    """
    Aggregate LLM costs across all configured providers.

    exclude_cloud_native: skip the Bedrock and Vertex fetchers. Both are metered
    (Bedrock fires Cost Explorer calls, Vertex queries the BigQuery billing
    export), so the free-by-default `nable scan` path sets this and leaves the
    cloud-native AI spend to the --spend path.

    Returns:
      {
        "total_usd": float,
        "by_provider": {"openai": float, "anthropic": float, "bedrock": float, ...},
        "by_model": {"gpt-4o": float, "claude-3-5-sonnet...": float, ...},
        "daily": [...],
        "model_efficiency": [  # sorted by cost per 1M tokens
            {"model": str, "provider": str, "cost_usd": float,
             "input_price_per_1m": float, "output_price_per_1m": float},
            ...
        ],
        "top_spenders": [...],   # models ranked by total spend
        "recommendations": [...],
      }
    """
    if end_date is None:
        end_date = date.today()
    if start_date is None:
        start_date = end_date - timedelta(days=days)

    from .saas.openai_usage import get_costs as openai_costs, is_configured as openai_configured
    from .saas.anthropic_usage import get_costs as anthropic_costs, is_configured as anthropic_configured
    from .saas.vertex_costs import get_vertex_costs, is_configured as vertex_configured
    from .saas.openrouter import get_costs as openrouter_costs, is_configured as openrouter_configured
    from .saas.litellm import get_costs as litellm_costs, is_configured as litellm_configured

    # Read-through cache: these four fetches are the slowest path in the
    # product and agentic sessions re-ask constantly.
    import copy as _copy
    from .. import cache as _cache
    _ck = _cache.make_key("llm.get_all", start_date.isoformat(), end_date.isoformat(), f"xcn={exclude_cloud_native}")
    _hit = _cache.get(_ck)
    if _hit is not None:
        out = _copy.deepcopy(_hit)
        if not include_provider_results:
            out.pop("provider_results", None)
        return out

    results: dict[str, dict] = {}

    # Use asyncio.run() safely — avoid deprecated get_event_loop on Python 3.10+
    import asyncio

    def _run(coro):  # type: ignore[return]
        try:
            loop = asyncio.get_event_loop()
            if loop.is_running():
                # Already inside an async context (e.g. called from an MCP tool handler)
                import concurrent.futures
                with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
                    return pool.submit(asyncio.run, coro).result()
            return loop.run_until_complete(coro)
        except RuntimeError:
            return asyncio.run(coro)

    # The four provider fetches are independent network calls that used to run
    # serially (the single biggest chunk of query latency). Run them in a
    # thread pool so the slowest provider sets the wall clock, not the sum.
    def _fetch_openai():
        if _run(openai_configured()):
            return openai_costs(start_date, end_date)
        return None

    def _fetch_anthropic():
        if _run(anthropic_configured()):
            return anthropic_costs(start_date, end_date)
        return None

    def _fetch_bedrock():
        import boto3
        from botocore.config import Config
        # Bound the STS auth probe. Without a timeout a hung IMDS/STS endpoint
        # blocks a pool thread for botocore's default (~60s x retries); on any
        # failure the outer loop skips Bedrock cleanly.
        _cfg = Config(connect_timeout=5, read_timeout=10, retries={"max_attempts": 1})
        boto3.client("sts", config=_cfg).get_caller_identity()  # quick auth check
        return get_bedrock_costs(start_date, end_date)

    def _fetch_vertex():
        if _run(vertex_configured()):
            v = get_vertex_costs(start_date, end_date)
            if v.get("source") != "none":
                return v
        return None

    def _fetch_openrouter():
        if _run(openrouter_configured()):
            v = openrouter_costs(start_date, end_date)
            if v.get("source") != "none":
                return v
        return None

    def _fetch_litellm():
        if _run(litellm_configured()):
            v = litellm_costs(start_date, end_date)
            if v.get("source") != "none":
                return v
        return None

    import concurrent.futures
    _fetchers = {
        "openai": _fetch_openai,
        "anthropic": _fetch_anthropic,
        "bedrock": _fetch_bedrock,
        "vertex": _fetch_vertex,
        "openrouter": _fetch_openrouter,
        "litellm": _fetch_litellm,
    }
    if exclude_cloud_native:
        # The free scan path must not touch the cloud-native AI legs: bedrock
        # fires Cost Explorer calls and vertex queries the BigQuery billing
        # export, both metered. Leave them to the --spend path.
        for _cn in _CLOUD_NATIVE_LLM:
            _fetchers.pop(_cn, None)
    with concurrent.futures.ThreadPoolExecutor(max_workers=len(_fetchers)) as _pool:
        _futs = {name: _pool.submit(fn) for name, fn in _fetchers.items()}
        for name, fut in _futs.items():
            try:
                data = fut.result()
                if data is not None:
                    results[name] = data
            except Exception as e:
                log.debug("%s cost fetch skipped: %s", name, e)

    # Aggregate
    total = 0.0
    by_provider: dict[str, float] = {}
    by_model: dict[str, float] = {}
    by_model_tokens: dict[str, dict[str, int]] = {}
    daily_map: dict[str, float] = {}

    for provider, data in results.items():
        amt = data.get("total_usd", 0.0)
        by_provider[provider] = round(amt, 4)
        total += amt

        for model, cost in data.get("by_model", {}).items():
            full_key = f"{model}"  # keep model names as-is; provider is implicit
            by_model[full_key] = by_model.get(full_key, 0.0) + cost

        # Merge per-model token counts from every provider that reports them
        # (OpenAI, Anthropic, gateways). Bedrock/Vertex report cost-only and
        # contribute nothing here, which is correct. This is what lets the KPI
        # engine cover OpenAI accounts, not just Anthropic.
        for model, tok in (data.get("by_model_tokens") or {}).items():
            bucket = by_model_tokens.setdefault(model, {})
            for k, v in tok.items():
                try:
                    bucket[k] = bucket.get(k, 0) + int(v)
                except (TypeError, ValueError):
                    continue

        for day_entry in data.get("daily", []):
            d = day_entry.get("date", "")
            daily_map[d] = daily_map.get(d, 0.0) + day_entry.get("total_usd", 0.0)

    daily = [{"date": d, "total_usd": round(v, 4)}
             for d, v in sorted(daily_map.items())]

    top_spenders = sorted(
        [{"model": k, "cost_usd": round(v, 4)} for k, v in by_model.items()],
        key=lambda x: x["cost_usd"], reverse=True
    )[:10]

    recommendations = _generate_recommendations(by_model, results)

    _out = {
        "period":       f"{start_date} → {end_date}",
        "total_usd":    round(total, 4),
        "by_provider":  by_provider,
        "by_model":     {k: round(v, 4) for k, v in
                         sorted(by_model.items(), key=lambda x: x[1], reverse=True)},
        "by_model_tokens": by_model_tokens,
        "daily":        daily,
        "top_spenders": top_spenders,
        "recommendations": recommendations,
        "sources":      {k: v.get("source", "none") for k, v in results.items()},
        # Per-provider normalized dicts (by_model, by_model_tokens, ...), kept so
        # FOCUS normalization can attribute each model to its provider. The flat
        # by_model above loses that; provider_results does not. Internal only:
        # stripped from the public return unless include_provider_results is set,
        # so MCP tool responses do not carry the duplicated per-provider payloads.
        "provider_results": results,
    }
    _cache.set(_ck, _copy.deepcopy(_out), _cache.COST_TTL)
    if not include_provider_results:
        _out = dict(_out)
        _out.pop("provider_results", None)
    return _out


# FOCUS ProviderName for each internal LLM provider key.
_LLM_FOCUS_NAMES = {
    "openai": "OpenAI",
    "anthropic": "Anthropic",
    "bedrock": "AWS Bedrock",
    "vertex": "Google Vertex AI",
    "openrouter": "OpenRouter",
    "litellm": "LiteLLM",
}


# Bedrock and Vertex spend already appears in the AWS/GCP FOCUS exports (Cost
# Explorer / BigQuery billing). Exclude them when merging into the unified FOCUS
# dataset so cloud-native AI is not double-counted.
_CLOUD_NATIVE_LLM = {"bedrock", "vertex"}


def get_all_llm_costs_as_focus(
    start_date: date | None = None,
    end_date: date | None = None,
    days: int = 30,
    exclude_cloud_native: bool = False,
) -> list:
    """Return all configured LLM/AI spend as FOCUS 1.2 records, one per model per
    provider (ServiceCategory "AI and Machine Learning"). Token counts and request
    volume ride along in each record's Tags.

    exclude_cloud_native: drop Bedrock and Vertex (already in the AWS/GCP FOCUS
    exports) so the unified FOCUS dataset does not double-count them.
    """
    from ..focus.translators.llm import llm_result_to_focus

    if end_date is None:
        end_date = date.today()
    if start_date is None:
        start_date = end_date - timedelta(days=days)

    agg = get_all_llm_costs(start_date, end_date, days=days, include_provider_results=True)
    records: list = []
    for key, result in (agg.get("provider_results") or {}).items():
        if exclude_cloud_native and key in _CLOUD_NATIVE_LLM:
            continue
        name = _LLM_FOCUS_NAMES.get(key, key.title())
        records.extend(llm_result_to_focus(
            result, provider=name, start_date=start_date, end_date=end_date,
        ))
    return records


def _generate_recommendations(
    by_model: dict[str, float],
    results: dict[str, dict],
) -> list[dict[str, Any]]:
    """Surface cost-saving opportunities across model choices."""
    recs: list[dict[str, Any]] = []

    from .saas.openai_usage import _MODEL_PRICING as OAI_PRICING
    from .saas.anthropic_usage import _MODEL_PRICING as ANT_PRICING
    from ..recommendations.bedrock_routing import _normalize_model_id

    all_pricing = {**OAI_PRICING, **ANT_PRICING, **_BEDROCK_PRICING}

    # Expensive model -> cheaper sibling for lower-complexity tasks. The savings
    # percentage is NOT hardcoded; it's computed from the real pricing tables
    # (blended input+output), so it tracks price changes and never asserts a
    # made-up number. It's an estimate that assumes a balanced input/output token
    # mix; the true figure depends on the workload's ratio.
    downgrades = {
        "gpt-4o":                   "gpt-4o-mini",
        "gpt-4-turbo":              "gpt-4o",
        "claude-3-opus-20240229":   "claude-3-5-sonnet-20241022",
        "claude-opus-4-20250514":   "claude-sonnet-4-5-20250929",
        "claude-opus-4-1-20250805": "claude-sonnet-4-5-20250929",
        # Canonical Bedrock ids (from _normalize_model_id) so Sonnet SKU display
        # names route to Haiku for lower-complexity tasks.
        "claude-sonnet-4-5":        "claude-haiku-3-5",
        "claude-sonnet-4-6":        "claude-haiku-3-5",
        "o1":                       "o3-mini",
    }

    def _blended(price: dict) -> float:
        return float(price.get("input", 0.0)) + float(price.get("output", 0.0))

    for model, spend in by_model.items():
        if spend < 5.0:  # ignore noise
            continue
        # Normalize bedrock/vendor-prefixed ids ("bedrock/anthropic.claude-...")
        # so the match works regardless of provider routing prefix.
        clean = model.split("/")[-1].replace("anthropic.", "").replace("meta.", "").replace("amazon.", "")
        # Bedrock Cost Explorer reports per-model SKU display names like
        # "Claude Sonnet 4.5" with no model-id string, so the cleaned id never
        # matches. Map those to a canonical id ("claude-sonnet-4-5") and match
        # against both, so model-switch recs fire for Bedrock spend too.
        canonical = _normalize_model_id(model)
        for expensive, cheaper in downgrades.items():
            if expensive not in clean and expensive not in canonical:
                continue
            cur_price, new_price = all_pricing.get(expensive), all_pricing.get(cheaper)
            if not cur_price or not new_price:
                continue
            cur_blended, new_blended = _blended(cur_price), _blended(new_price)
            if cur_blended <= 0:
                continue
            savings_frac = max(0.0, 1.0 - new_blended / cur_blended)
            recs.append({
                "model":        model,
                "current_spend": round(spend, 2),
                "recommendation": f"Consider {cheaper} for lower-complexity tasks",
                "estimated_savings_pct": f"{savings_frac * 100:.0f}%",
                "estimated_savings_usd": round(spend * savings_frac, 2),
                "basis": "price-ratio estimate assuming a balanced input/output token mix; actual savings depend on your ratio",
            })
            break

    return sorted(recs, key=lambda r: r["estimated_savings_usd"], reverse=True)


def _empty(reason: str) -> dict[str, Any]:
    return {"total_usd": 0.0, "by_model": {}, "daily": [],
            "source": "none", "reason": reason}
