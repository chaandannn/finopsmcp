"""Connection-aware tool surface: advertise only what this machine can use.

Locks in the load-bearing properties:
  - every registered tool is mapped to exactly one family (a new tool that
    nobody classifies fails here, same self-healing pattern as the CLI help
    groups);
  - a clean machine advertises core only (provider families hidden), while
    connectors and cost tools stay discoverable;
  - detecting a provider reveals its family;
  - FINOPS_ALL_TOOLS=1 and demo mode advertise everything registered;
  - THE SAFETY PROPERTY: an unadvertised tool called by name still runs, so the
    in-chat connect flow can never be broken by filtering;
  - the filter actually pays: advertised-list token weight drops >20% on a
    clean machine vs the full surface.
"""
from __future__ import annotations

import asyncio

import pytest

from finops import server, tool_surface
from finops.tool_surface import FAMILY_TOOLS, _FAMILY_OF, advertise, connected_families


@pytest.fixture(autouse=True)
def _clean_surface(monkeypatch):
    """Scrub every detection signal so each test starts 'nothing connected'."""
    for keys in tool_surface._ENV_KEYS.values():
        for k in keys:
            monkeypatch.delenv(k, raising=False)
    monkeypatch.delenv("FINOPS_ALL_TOOLS", raising=False)
    monkeypatch.delenv("KUBECONFIG", raising=False)
    monkeypatch.setattr(tool_surface, "_kubeconfig_present", lambda: False)
    monkeypatch.setattr("finops.security.vault.Vault.default",
                        classmethod(lambda cls: (_ for _ in ()).throw(RuntimeError("no vault"))))
    monkeypatch.setattr("finops.accounts.list_accounts", lambda: [])
    monkeypatch.setattr("finops.demo_data.is_demo", lambda: False)
    tool_surface._reset_cache_for_tests()
    yield
    tool_surface._reset_cache_for_tests()


# ── completeness: the enforcement for the fail-open runtime ───────────────────

def test_every_registered_tool_is_mapped():
    registered = {t.name for t in server.mcp._tool_manager.list_tools()}
    unmapped = registered - set(_FAMILY_OF)
    assert not unmapped, f"add these to a family in tool_surface.py: {sorted(unmapped)}"


def test_extras_are_mapped_too():
    # The 26 registration-gated extras only exist under FINOPS_ALL_TOOLS=1, so
    # the registry check above can miss them in a normal test run. Pin them here.
    unmapped = set(server._EXTRA_TOOLS) - set(_FAMILY_OF)
    assert not unmapped, f"extras missing a family: {sorted(unmapped)}"


def test_no_tool_in_two_families():
    total = sum(len(v) for v in FAMILY_TOOLS.values())
    assert total == len(_FAMILY_OF), "a tool appears in more than one family"


# ── filtering behavior ─────────────────────────────────────────────────────────

def _advertised_names():
    return {t.name for t in asyncio.run(server.mcp.list_tools())}


def test_clean_machine_advertises_core_only():
    """A machine with nothing connected sees ONLY what is useful with nothing
    connected. core was 115 tools and ~31k tokens riding every message, ~90 of
    which could answer nothing but "no accounts connected", charged to precisely
    the user who had not connected yet. It is now ~21 tools and ~4k tokens."""
    names = _advertised_names()
    # The connect path, diagnosis, and the two surfaces that need no cloud at all.
    for must in ("connect_aws", "connect_gcp", "connect_azure",
                 "what_can_nable_do", "get_agent_team", "nable_setup_status",
                 "check_ai_budget", "check_action_policy",
                 # the single cost tool kept visible on purpose: it is what the
                 # model reaches for when a fresh user asks "what am I spending?",
                 # and its no-accounts response is what starts an in-chat connect
                 "get_cost_summary"):
        assert must in names, must
    # The cross-provider cost surface is NOT advertised until a data source exists.
    for hidden in ("get_cost_trends", "forecast_costs", "run_full_cost_audit",
                   "slice_costs", "get_savings_summary", "list_savings_recommendations"):
        assert hidden not in names, hidden
    assert len(names) <= 30, f"clean-machine surface crept back up to {len(names)}"
    # provider families hidden
    for hidden in ("get_azure_budgets", "audit_gcp_waste", "get_kubernetes_costs",
                   "get_databricks_costs", "get_llm_costs", "create_ticket",
                   "send_weekly_digest_now"):
        assert hidden not in names, hidden
    # and nothing outside core is advertised at all
    assert names <= set(FAMILY_TOOLS["core"])


def test_aws_env_reveals_aws_and_llm(monkeypatch):
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "AKIATEST")
    tool_surface._reset_cache_for_tests()
    fams = connected_families()
    assert "aws" in fams and "llm" in fams  # Bedrock rides the AWS account
    names = _advertised_names()
    assert "audit_aws_waste" in names
    assert "get_llm_costs" in names
    assert "get_azure_budgets" not in names


def test_databricks_env_reveals_family(monkeypatch):
    monkeypatch.setenv("DATABRICKS_HOST", "https://x.cloud.databricks.com")
    tool_surface._reset_cache_for_tests()
    assert "get_databricks_costs" in _advertised_names()


def test_all_tools_flag_advertises_everything(monkeypatch):
    monkeypatch.setenv("FINOPS_ALL_TOOLS", "1")
    registered = {t.name for t in server.mcp._tool_manager.list_tools()}
    assert _advertised_names() == registered


def test_demo_mode_advertises_everything(monkeypatch):
    monkeypatch.setattr("finops.demo_data.is_demo", lambda: True)
    registered = {t.name for t in server.mcp._tool_manager.list_tools()}
    assert _advertised_names() == registered


def test_unmapped_tool_fails_open():
    assert advertise("some_future_tool_nobody_classified") is True


# ── THE SAFETY PROPERTY: hidden tools stay callable ────────────────────────────

def test_unadvertised_tool_is_still_callable():
    async def main():
        from mcp.shared.memory import create_connected_server_and_client_session
        async with create_connected_server_and_client_session(
            server.mcp._mcp_server
        ) as client:
            listed = await client.list_tools()
            names = {t.name for t in listed.tools}
            assert "get_databricks_costs" not in names  # hidden on a clean box
            result = await client.call_tool("get_databricks_costs", {})
            # It ran: it returns a not-configured answer, not "unknown tool".
            text = "".join(getattr(c, "text", "") for c in result.content).lower()
            assert "unknown tool" not in text
            assert text  # produced a real response
    asyncio.run(main())


# ── the payoff, measured ───────────────────────────────────────────────────────

def test_token_weight_drops_meaningfully(monkeypatch):
    from finops.token_budget import estimate_tokens

    def weight(tools):
        return estimate_tokens([
            {"name": t.name, "description": t.description, "schema": t.parameters}
            for t in tools
        ])

    every = server.mcp._tool_manager.list_tools()
    clean = [t for t in every if advertise(t.name)]
    full, filtered = weight(every), weight(clean)
    assert filtered < full * 0.8, f"expected >20% cut, got {full} -> {filtered}"


# ── the cost family gate ──────────────────────────────────────────────────────

def test_cost_tools_appear_once_a_data_source_connects(monkeypatch):
    """The whole point of splitting core: the cross-provider cost surface is dead
    weight until there is something to report on, then it must appear."""
    tool_surface._reset_cache_for_tests()
    assert "get_cost_trends" not in _advertised_names()

    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "AKIATEST")
    tool_surface._reset_cache_for_tests()
    names = _advertised_names()
    for must in ("get_cost_trends", "forecast_costs", "run_full_cost_audit", "slice_costs"):
        assert must in names, must


def test_notifications_alone_do_not_unlock_cost_tools(monkeypatch):
    """A Slack token is a place to SEND findings, not a source to read them from.
    Gating on 'any family connected' rather than 'any DATA SOURCE connected' would
    hand a user 94 cost tools that still have nothing to talk about."""
    monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-test")  # pragma: allowlist secret
    tool_surface._reset_cache_for_tests()
    fams = connected_families()
    assert "notifications" in fams, "precondition: the Slack token registers a family"
    assert not (fams & tool_surface._DATA_SOURCE_FAMILIES), "precondition: no data source"
    assert "get_cost_trends" not in _advertised_names()


def test_a_hidden_cost_tool_is_still_callable(monkeypatch):
    """Hiding is advertisement-only. If a model names a hidden tool it must still
    run, or the in-chat connect flow breaks the moment a tool is gated."""
    import asyncio

    from mcp.shared.memory import create_connected_server_and_client_session as connect

    from finops import server

    tool_surface._reset_cache_for_tests()
    assert "get_cost_trends" not in _advertised_names()

    async def go():
        async with connect(server.mcp._mcp_server) as s:
            return await s.call_tool("get_cost_trends", {})

    r = asyncio.run(go())
    text = "".join(getattr(c, "text", "") for c in r.content)
    assert text, "a hidden tool returned nothing; it was not resolvable"
    assert "Unknown tool" not in text and "not found" not in text.lower()


# ── advertised descriptions: trim what the model already has ─────────────────
#
# Measured on a connected install before this: 147 advertised tools carried
# ~25,300 tokens of description on EVERY message, of which ~24% was a prose
# Args: block duplicating the inputSchema and ~23% was five-deep example lists.

from finops.tool_surface import compact_description  # noqa: E402

_DOC = """
    Get cost breakdown for any named cloud service on AWS, Azure, or GCP.

    Short names are resolved automatically:
      "MSK" -> "Amazon Managed Streaming for Apache Kafka"

    Args:
        service_name: Name of the service (short or full).
        provider:     "aws", "azure", "gcp", or blank to auto-detect.
        start_date:   ISO date. Defaults to 30 days ago.

    Examples:
        - "How much did we spend on ElastiCache this month?"
        - "Show me AppSync costs for the last 7 days"
        - "What's our MSK spend?"
        - "How much are we spending on Azure Cognitive Services?"
        - "Show me GCP BigQuery costs"
"""


def test_the_args_block_is_dropped_because_the_schema_carries_it():
    out = compact_description(_DOC)
    assert "Args:" not in out
    assert "service_name:" not in out
    assert "auto-detect" not in out


def test_the_summary_and_disambiguation_survive():
    """What the model actually needs to pick this tool over a neighbour."""
    out = compact_description(_DOC)
    assert "Get cost breakdown for any named cloud service" in out
    assert "MSK" in out and "Kafka" in out


def test_examples_are_capped_not_deleted():
    out = compact_description(_DOC)
    assert "Examples:" in out
    assert "ElastiCache" in out and "AppSync" in out       # first two kept
    assert "BigQuery" not in out and "Cognitive" not in out  # tail dropped
    assert out.count("- \"") == 2


def test_use_when_is_kept_in_full():
    """It is 1% of the weight and it is the single best selection signal."""
    doc = 'Do a thing.\n\n    Use when:\n        - "the user asks X"\n        - "or Y"\n        - "or Z"\n'
    out = compact_description(doc)
    assert out.count("- \"") == 3


def test_a_description_with_no_sections_is_untouched():
    plain = "Return the current spend total."
    assert compact_description(plain) == plain


@pytest.mark.parametrize("bad", ["", None])
def test_empty_input_is_safe(bad):
    assert compact_description(bad) == bad


def test_the_advertised_surface_actually_shrank():
    """The wiring test: trimming the helper but never calling it would leave
    every user paying the same per-message tax."""
    import asyncio, json
    from finops.server import mcp
    from finops.tool_surface import advertise
    try:
        from finops.token_budget import estimate_tokens
    except Exception:
        estimate_tokens = lambda s: len(s) // 4  # noqa: E731

    raw = [t for t in mcp._tool_manager.list_tools() if advertise(t.name)]
    adv = asyncio.run(mcp.list_tools())
    before = sum(estimate_tokens(t.description or "") for t in raw)
    after = sum(estimate_tokens(t.description or "") for t in adv)
    assert after < before * 0.85, f"expected a real cut, got {before} -> {after}"
    # and the tools themselves are all still advertised
    assert len(adv) == len(raw)


def test_trimming_never_empties_a_description():
    """A tool advertised with no description is worse than a fat one."""
    import asyncio
    from finops.server import mcp
    for t in asyncio.run(mcp.list_tools()):
        assert (t.description or "").strip(), f"{t.name} lost its description"
