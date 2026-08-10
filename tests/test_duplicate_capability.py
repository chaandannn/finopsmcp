"""Tests for the duplicate-capability scanner.

The scanner flags spend on two services/providers doing the same job at once,
the pattern a plain cost breakdown can never surface since each line item
looks legitimate alone. Covers LLM inference paths (Bedrock + direct API) and
AWS managed search/retrieval (Kendra + OpenSearch). Every finding must be an
INFERRED investigation (never a precise dollar "recommendation"): this is a
judgment call, not a measured waste fact.
"""
import asyncio
from types import SimpleNamespace
from unittest.mock import patch

from finops.recommendations import duplicate_capability as D
from finops.recommendations.envelope import INVESTIGATION


# ── find_duplicate_llm_paths ─────────────────────────────────────────────────

def test_no_finding_with_a_single_active_provider():
    assert D.find_duplicate_llm_paths({"bedrock": 3500.0, "openai": 0.0}) is None


def test_no_finding_when_second_provider_is_noise():
    # A stray $0.02 test call should never trigger the flag.
    assert D.find_duplicate_llm_paths({"bedrock": 3500.0, "anthropic": 0.02}) is None


def test_flags_two_real_llm_paths():
    f = D.find_duplicate_llm_paths({"bedrock": 3568.13, "anthropic": 42.10, "openai": 0.0})
    assert f is not None
    assert f.evidence == "inferred"
    assert f.kind == INVESTIGATION
    assert "AWS Bedrock" in f.title or "AWS Bedrock" in f.why
    d = f.to_dict()
    # An investigation must never carry a precise dollar figure.
    assert d["est_monthly_savings"] is None
    assert "magnitude" in d
    assert d["metadata"]["active_paths"] == {"AWS Bedrock": 3568.13, "Anthropic (direct API)": 42.1}


def test_flags_three_active_llm_paths():
    f = D.find_duplicate_llm_paths({"bedrock": 100.0, "openai": 50.0, "vertex": 25.0})
    assert f is not None
    assert f.metadata["total_monthly_usd"] == 175.0


def test_empty_input_returns_none():
    assert D.find_duplicate_llm_paths({}) is None
    assert D.find_duplicate_llm_paths(None) is None  # type: ignore[arg-type]


# ── find_duplicate_search_services ───────────────────────────────────────────

def test_no_finding_with_only_kendra():
    by_service = {"Amazon Kendra": 260.40, "Amazon Elastic Compute Cloud - Compute": 200.0}
    assert D.find_duplicate_search_services(by_service) is None


def test_flags_kendra_and_opensearch():
    by_service = {"Amazon Kendra": 260.40, "Amazon OpenSearch Service": 172.80}
    f = D.find_duplicate_search_services(by_service)
    assert f is not None
    assert f.evidence == "inferred"
    assert f.confidence == "low"  # legitimate segmentation is quite plausible here
    assert f.metadata["active_services"] == {"Kendra": 260.4, "OpenSearch": 172.8}


def test_search_noise_floor_respected():
    by_service = {"Amazon Kendra": 260.40, "Amazon OpenSearch Service": 0.5}
    assert D.find_duplicate_search_services(by_service) is None


# ── find_duplicate_data_platforms ────────────────────────────────────────────

def test_no_finding_with_only_snowflake():
    assert D.find_duplicate_data_platforms({"snowflake": 4200.0, "datadog": 300.0}) is None


def test_flags_databricks_and_snowflake():
    f = D.find_duplicate_data_platforms({"databricks": 5100.0, "snowflake": 4200.0})
    assert f is not None
    assert f.evidence == "inferred"
    assert f.confidence == "medium"
    assert f.metadata["active_platforms"] == {"Databricks": 5100.0, "Snowflake": 4200.0}
    assert f.metadata["total_monthly_usd"] == 9300.0


def test_data_platform_ignores_unrelated_providers():
    assert D.find_duplicate_data_platforms({"databricks": 5100.0, "datadog": 300.0}) is None


def test_data_platform_noise_floor_respected():
    assert D.find_duplicate_data_platforms({"databricks": 5100.0, "snowflake": 0.1}) is None


# ── scan_duplicate_capabilities ──────────────────────────────────────────────

def test_scan_combines_all_checks():
    findings = D.scan_duplicate_capabilities(
        llm_by_provider={"bedrock": 3568.13, "anthropic": 42.10},
        aws_by_service={"Amazon Kendra": 260.40, "Amazon OpenSearch Service": 172.80},
        saas_by_provider={"databricks": 5100.0, "snowflake": 4200.0},
    )
    assert len(findings) == 3
    sources = {f.source for f in findings}
    assert sources == {"duplicate_capability"}


def test_scan_with_nothing_active_returns_empty():
    assert D.scan_duplicate_capabilities(llm_by_provider={}, aws_by_service={}, saas_by_provider={}) == []
    assert D.scan_duplicate_capabilities() == []


# ── server tool wiring ────────────────────────────────────────────────────────

def _aws_stub(by_service, configured=True):
    async def _is_configured():
        return configured

    async def _get_costs(start, end, granularity="MONTHLY"):
        return SimpleNamespace(
            provider="aws", start_date=start, end_date=end,
            total_usd=sum(by_service.values()), by_service=by_service,
            by_account={}, by_region={}, currency="USD",
        )

    return SimpleNamespace(is_configured=_is_configured, get_costs=_get_costs)


def _saas_stub(name: str, total_usd: float, configured=True):
    async def _is_configured():
        return configured

    async def _get_costs(start, end, granularity="MONTHLY"):
        return SimpleNamespace(
            provider=name, start_date=start, end_date=end,
            total_usd=total_usd, by_service={name: total_usd},
            by_account={}, by_region={}, currency="USD",
        )

    return SimpleNamespace(is_configured=_is_configured, get_costs=_get_costs)


def test_audit_duplicate_spend_wires_all_three_clusters_together():
    import finops.server as srv
    from finops import cache as _cache
    _cache.clear()

    def _fake_llm_costs(*a, **k):
        # get_all_llm_costs is a plain sync function (run via asyncio.to_thread
        # in the tool), not a coroutine.
        return {"by_provider": {"bedrock": 3568.13, "anthropic": 42.10}}

    with patch("finops.connectors.llm_costs.get_all_llm_costs", _fake_llm_costs), \
         patch.dict(srv.CLOUD_CONNECTORS, {
             "aws": _aws_stub({"Amazon Kendra": 260.40, "Amazon OpenSearch Service": 172.80})
         }), \
         patch.dict(srv.SAAS_CONNECTORS, {
             "databricks": _saas_stub("databricks", 5100.0),
             "snowflake": _saas_stub("snowflake", 4200.0),
         }, clear=True):
        result = asyncio.run(srv.audit_duplicate_spend(days=30))

    assert result["finding_count"] == 3
    assert result["checked"]["aws_connected"] is True
    assert "bedrock" in result["checked"]["llm_providers"]
    assert set(result["checked"]["saas_providers"]) == {"databricks", "snowflake"}


def test_audit_duplicate_spend_handles_no_aws_or_saas_connected():
    import finops.server as srv
    from finops import cache as _cache
    _cache.clear()

    def _fake_llm_costs(*a, **k):
        # get_all_llm_costs is a plain sync function (run via asyncio.to_thread
        # in the tool), not a coroutine.
        return {"by_provider": {"bedrock": 3568.13, "anthropic": 42.10}}

    with patch("finops.connectors.llm_costs.get_all_llm_costs", _fake_llm_costs), \
         patch.dict(srv.CLOUD_CONNECTORS, {"aws": _aws_stub({}, configured=False)}), \
         patch.dict(srv.SAAS_CONNECTORS, {}, clear=True):
        result = asyncio.run(srv.audit_duplicate_spend(days=30))

    assert result["checked"]["aws_connected"] is False
    assert result["checked"]["saas_providers"] == []
    assert result["finding_count"] == 1  # only the LLM-path finding fires
    assert result["finding_count"] == 1  # only the LLM-path finding fires
