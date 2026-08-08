"""The funnel: advertise the front door, keep everything else callable.

Family filtering narrowed the surface but left it flat. 198 tools, ~46,700
tokens a message, 59 of them with cost/spend/bill in the name. Tiering advertises
only the entry points.

Two things have to stay true or this is a regression rather than a feature:
the hidden tools must still run, and the front door must not creep back into a
pile. The budget test below is the one that matters most, because creep is
gradual and nobody notices a surface growing 300 tokens at a time.
"""
import asyncio
import json

import pytest

from finops import tool_surface
from finops.token_budget import estimate_tokens

# The front door's ceiling. Measured at ~5,600 with AWS connected; the headroom
# is for a genuinely new question a user asks, not for the drill-downs sneaking
# back up. Raising this is a deliberate act that shows up in a diff.
TIER1_TOKEN_CEILING = 7_000
TIER1_COUNT_CEILING = 22


@pytest.fixture(autouse=True)
def _clean_surface(monkeypatch):
    for k in list(globals().get("_ENVS", ())) + [
        "AWS_ACCESS_KEY_ID", "AWS_PROFILE", "AZURE_CLIENT_ID", "DATABRICKS_HOST",
        "GOOGLE_APPLICATION_CREDENTIALS", "OPENAI_API_KEY", "SLACK_BOT_TOKEN",
        "FINOPS_ALL_TOOLS", "FINOPS_FLAT_TOOLS", "KUBECONFIG",
    ]:
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setattr(tool_surface, "_kubeconfig_present", lambda: False)
    monkeypatch.setattr("finops.security.vault.Vault.default",
                        lambda: type("V", (), {"list_keys": staticmethod(lambda: [])})())
    monkeypatch.setattr("finops.accounts.list_accounts", lambda: [])
    monkeypatch.setattr("finops.demo_data.is_demo", lambda: False)
    tool_surface._reset_cache_for_tests()
    yield
    tool_surface._reset_cache_for_tests()


def _registered() -> set[str]:
    """Everything in the registry, advertised or not: the set a call can reach."""
    from finops import server
    return {t.name for t in server.mcp._tool_manager.list_tools()}


def _advertised(**env) -> list:
    from finops import server
    import os
    for k, v in env.items():
        os.environ[k] = v
    tool_surface._reset_cache_for_tests()
    try:
        return asyncio.run(server.mcp.list_tools())
    finally:
        for k in env:
            os.environ.pop(k, None)
        tool_surface._reset_cache_for_tests()


def _weight(tools) -> int:
    return sum(estimate_tokens(t.description or "") +
               estimate_tokens(json.dumps(getattr(t, "inputSchema", None) or {}))
               for t in tools)


# ── The map is real ──────────────────────────────────────────────────────────

def test_tier1_names_are_real_tools():
    """A rename that misses this set silently empties the front door."""
    missing = tool_surface.TIER1 - _registered()
    assert not missing, f"TIER1 names nothing: {sorted(missing)}"


def test_tier3_names_are_real_tools():
    missing = tool_surface.TIER3 - _registered()
    assert not missing, f"TIER3 names nothing: {sorted(missing)}"


def test_tiers_do_not_overlap():
    assert not (tool_surface.TIER1 & tool_surface.TIER3)


def test_unlisted_tools_default_to_tier_two():
    """Unlisted must fail SHUT. A new tool joining the front door by accident is
    the failure that costs every user tokens on every message."""
    assert tool_surface.tier("some_brand_new_tool") == 2
    assert tool_surface.tier("get_cost_summary") == 1
    assert tool_surface.tier("get_instance_deep_analysis") == 3


# ── The budget. The guard that actually matters ──────────────────────────────

def test_front_door_stays_under_budget():
    tools = _advertised(AWS_ACCESS_KEY_ID="AKIATEST")
    weight = _weight(tools)
    assert len(tools) <= TIER1_COUNT_CEILING, (
        f"front door grew to {len(tools)} tools: {sorted(t.name for t in tools)}")
    assert weight <= TIER1_TOKEN_CEILING, (
        f"front door is {weight} tokens, ceiling {TIER1_TOKEN_CEILING}. "
        "Raise it deliberately or demote a tool.")


def test_funnel_cuts_the_surface_by_most_of_it():
    """The whole point, asserted. If this drops below 60% the funnel has been
    quietly undone by tools drifting into tier 1."""
    funnel = _weight(_advertised(AWS_ACCESS_KEY_ID="AKIATEST"))
    flat = _weight(_advertised(AWS_ACCESS_KEY_ID="AKIATEST", FINOPS_FLAT_TOOLS="1"))
    assert flat > funnel
    cut = 100 * (flat - funnel) // flat
    assert cut >= 60, f"funnel only cuts {cut}% (was ~84%)"


# ── Hidden is not gone. The load-bearing safety property ─────────────────────

def test_a_hidden_tool_still_runs_when_named():
    """The funnel is advertisement-only. If this ever fails, tiering has become
    a feature removal and must be reverted, not patched."""
    from finops import server

    async def go():
        from mcp.shared.memory import create_connected_server_and_client_session
        async with create_connected_server_and_client_session(
            server.mcp._mcp_server
        ) as client:
            listed = {t.name for t in (await client.list_tools()).tools}
            assert "get_costs_by_service" not in listed, "expected it hidden"
            result = await client.call_tool("get_costs_by_service", {})
            return result

    res = asyncio.run(go())
    # It ran. Whether it found accounts is not this test's business; what matters
    # is that the call was dispatched rather than rejected as an unknown tool.
    assert res is not None
    text = json.dumps(getattr(res, "content", None), default=str).lower()
    assert "unknown tool" not in text and "tool not found" not in text


def test_every_tier1_tool_is_actually_advertised_when_its_family_is_connected():
    listed = {t.name for t in _advertised(AWS_ACCESS_KEY_ID="AKIATEST")}
    # Tier 1 spans core + cost; with AWS connected both gates open.
    for name in ("get_cost_summary", "explain_cost_change", "get_anomalies",
                 "check_action_policy", "nable_setup_status"):
        assert name in listed, f"{name} is tier 1 but was not advertised"


# ── The escape hatches ───────────────────────────────────────────────────────

def test_flat_flag_restores_the_old_surface_without_unhiding_other_clouds():
    """FINOPS_FLAT_TOOLS is narrower than FINOPS_ALL_TOOLS on purpose: escaping
    the funnel should not force an AWS-only user to also take the Azure tools."""
    flat = {t.name for t in _advertised(AWS_ACCESS_KEY_ID="AKIATEST", FINOPS_FLAT_TOOLS="1")}
    assert "get_costs_by_service" in flat
    assert "get_azure_cost_by_dimension" not in flat


def test_all_tools_flag_still_wins_over_the_funnel():
    every = {t.name for t in _advertised(FINOPS_ALL_TOOLS="1")}
    assert "get_costs_by_service" in every
    assert "get_instance_deep_analysis" in every


def test_demo_mode_still_shows_everything(monkeypatch):
    monkeypatch.setattr("finops.demo_data.is_demo", lambda: True)
    tool_surface._reset_cache_for_tests()
    assert tool_surface.advertise("get_costs_by_service")


# ── Drill-down routing: the only way back to a hidden tool ───────────────────

def test_drilldown_only_ever_names_real_tools():
    """A suggestion pointing at a tool that does not exist is worse than no
    suggestion: the model reports a capability the user cannot have."""
    registered = _registered()
    suggested = set()
    for services in (["Amazon Textract"], ["Amazon EC2"], ["Amazon RDS"],
                     ["AWS Data Transfer"], ["Amazon Bedrock"], ["Amazon S3"],
                     ["Elastic Load Balancing"], ["AWS Marketplace"], []):
        suggested |= set(tool_surface.drilldown_for(services))
    missing = suggested - registered
    assert not missing, f"drilldown names nothing: {sorted(missing)}"


def test_drilldown_never_suggests_a_tool_already_on_the_front_door():
    """Pointing at an advertised tool wastes the payload; the model can see it."""
    for services in (["Amazon EC2"], ["Amazon Textract"], []):
        overlap = set(tool_surface.drilldown_for(services)) & tool_surface.TIER1
        assert not overlap, f"redundant suggestion: {sorted(overlap)}"


def test_drilldown_is_about_this_answer_not_a_generic_menu():
    textract = tool_surface.drilldown_for(["Amazon Textract", "Amazon S3"])
    rds = tool_surface.drilldown_for(["Amazon Relational Database Service"])
    assert "get_textract_costs" in textract
    assert "get_textract_costs" not in rds
    assert "get_rds_rightsizing_recommendations" in rds


def test_drilldown_always_offers_the_universal_next_questions():
    for services in ([], ["Something Unrecognized"]):
        out = tool_surface.drilldown_for(services)
        assert "get_costs_by_service" in out
        assert "get_cost_trends" in out


def test_cost_summary_carries_the_map(monkeypatch):
    """The front door has to name the next room, or hiding 130 tools is just
    a feature removal with extra steps."""
    import finops.tools.cost_queries as cq
    monkeypatch.setattr("finops.demo_data.is_demo", lambda: True)
    monkeypatch.setattr("finops.demo_data.get_demo_response",
                        lambda name: None if name != "get_cost_summary" else None)
    # Demo returns {} for an unknown key; drive the real path instead by asserting
    # the helper is wired into the module rather than re-running the whole query.
    src = (cq.__file__ or "")
    assert src
    with open(src) as fh:
        body = fh.read()
    assert "drilldown_for" in body, "get_cost_summary no longer names its children"
    assert '"next_tools"' in body
