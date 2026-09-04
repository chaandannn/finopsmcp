"""The two ways this product's own metrics lied to it.

Why this file exists, stated plainly. On 2026-08-29 the funnel read 642 installs
and 641 "connected an account", which should have been impossible: connecting a
cloud account is the harder act, and a separate measurement in July had put it
at 5. Both numbers were wrong in ways nothing would have caught.

1. `provider_count` never meant what the module docstring promises.
   server.py passed `len(_ALL_CONNECTORS)` — the number of connectors compiled
   into the build. So it reported 15, or 12, or 14, clustering by RELEASE rather
   than by user, and `WHERE provider_count >= 1` matched everybody. The metric
   that was supposed to show whether the install-to-connect cliff was moving had
   never been able to show it.

2. The event fields took whatever they were handed.
   Between 08-03 and 08-17, 21 install ids reported `plan="/tmp/pp-fuzz"` and 21
   more `plan="test"`, with matching junk in `tool`. Something walked the
   properties with adversarial values, 21 of those ids also emitted heartbeats,
   and they are inside the 642. Roughly 3% of the top of the funnel was noise
   that looked exactly like signal.

These tests pin both, plus the constraint that makes the first one hard: the
connected count may only be set from a path that has ALREADY resolved
credentials. Resolving them to populate telemetry would put a botocore metadata
lookup, which can hang for a minute without local credentials, in front of
exactly the installs that have none.
"""
from __future__ import annotations

import ast
import inspect
from pathlib import Path

import pytest

from finops import telemetry

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(autouse=True)
def _clean_session():
    """A session dict of our own, restored afterwards.

    `_session` is module-global and every test in the suite that records a tool
    call writes into it. An earlier version of this fixture reset three keys and
    left `tools_used` alone, so these tests passed run on their own and failed
    seven ways in the full suite, against tool names other tests had left behind.
    Copy the whole thing rather than naming keys, or the next key added here has
    the same problem.
    """
    before = {k: (v.copy() if hasattr(v, "copy") else v) for k, v in telemetry._session.items()}
    telemetry._session.update({
        "tools_used": set(), "tool_counts": {},
        "provider_count": 0, "connectors_available": 0, "plan": "free",
    })
    yield
    telemetry._session.clear()
    telemetry._session.update(before)


# ── the field cannot take junk ───────────────────────────────────────────────

@pytest.mark.parametrize("junk", ["/tmp/pp-fuzz", "test", "0", "", None, "../../etc", "TRIAL; DROP"])
def test_a_fuzzed_plan_becomes_unknown_rather_than_itself(junk):
    """The exact values that reached production, and the shapes near them."""
    telemetry.set_plan(junk)
    assert telemetry._session["plan"] == "unknown", (
        f"{junk!r} was stored as a plan tier; every count grouped by plan is now wrong"
    )


@pytest.mark.parametrize("real", ["free", "trial", "pro", "team", "enterprise"])
def test_real_plans_still_pass_through(real):
    telemetry.set_plan(real)
    assert telemetry._session["plan"] == real


@pytest.mark.parametrize("junk", ["/tmp/pp-fuzz", "test-tool", "0", "", "Tool", "a" * 80, None])
def test_a_fuzzed_tool_name_is_dropped_not_recorded(junk, monkeypatch):
    """Dropped rather than stored as "invalid": a per-tool table is read as a
    list of real tools, and one junk row with 21 installs behind it reads like a
    feature nobody has heard of."""
    monkeypatch.setattr(telemetry, "_is_opted_out", lambda: False)
    sent = []
    monkeypatch.setattr(telemetry, "_send_event", lambda *a, **k: sent.append(a))
    # The delta, not the absolute set: this assertion has to hold whatever else
    # ran first, and asserting emptiness is what tied it to test order.
    before = set(telemetry._session["tools_used"])
    telemetry.record_tool_call(junk)
    assert telemetry._session["tools_used"] == before, f"{junk!r} entered the tool set"
    assert sent == [], f"{junk!r} was sent as a tool_called event"


def test_a_real_tool_name_is_still_recorded(monkeypatch):
    monkeypatch.setattr(telemetry, "_is_opted_out", lambda: False)
    monkeypatch.setattr(telemetry, "_send_event", lambda *a, **k: None)
    telemetry.record_tool_call("get_cost_summary")
    assert "get_cost_summary" in telemetry._session["tools_used"]


# ── the connected count means connected ──────────────────────────────────────

def test_startup_does_not_report_the_connector_registry_as_connections():
    """The bug, in one assertion.

    ping_startup receives len(_ALL_CONNECTORS). If that lands in provider_count,
    every install on a build shipping 15 connectors reports 15 connected
    providers and the connected-account query matches everyone.
    """
    telemetry.ping_startup(provider_count=15, plan="trial")
    assert telemetry._session["provider_count"] == 0, (
        "the size of the connector registry is being reported as the number of "
        "providers the user connected"
    )
    assert telemetry._session["connectors_available"] == 15, (
        "the build's connector count should still be recorded, under its own name"
    )


def test_the_connected_count_is_set_from_resolved_credentials():
    telemetry.set_provider_count(2)
    assert telemetry._session["provider_count"] == 2


@pytest.mark.parametrize("bad", ["many", None, -3])
def test_a_nonsense_connected_count_is_refused_or_clamped(bad):
    telemetry.set_provider_count(7)
    telemetry.set_provider_count(bad)
    assert telemetry._session["provider_count"] in (0, 7), (
        "a bad count either leaves the last good value or clamps, never stores junk"
    )


def test_the_heartbeat_carries_both_numbers_separately(monkeypatch):
    """So the old data is still readable and the new data is unambiguous."""
    monkeypatch.setattr(telemetry, "_is_opted_out", lambda: False)
    captured = {}
    monkeypatch.setattr(telemetry, "_send", lambda _id, props: captured.update(props))
    monkeypatch.setattr(telemetry.threading, "Thread",
                        lambda target, args, daemon: type("T", (), {"start": lambda s: target(*args)})())
    telemetry.set_connectors_available(15)
    telemetry.set_provider_count(1)
    telemetry.ping()
    assert captured["provider_count"] == 1
    assert captured["connectors_available"] == 15


# ── the wiring, and the constraint on it ─────────────────────────────────────

def _function_ast(path: Path, name: str) -> ast.AST:
    """Find a function in a FILE by name.

    Not inspect.getsource(fn): these are MCP tools, so the attribute on the
    module is the decorator's wrapper and getsource returns the wrapper, or the
    whole module, depending on how it was registered.
    """
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
            return node
    raise AssertionError(f"{name} is not defined in {path.name} any more")


def test_list_connected_providers_reports_the_count_it_already_computed():
    """The wiring, checked as a CALL rather than as a string.

    `"set_provider_count" in source` still passes when the call is deleted,
    because the import line keeps mentioning it. That exact false pass has
    happened in this repo before.
    """
    fn = _function_ast(ROOT / "src" / "finops" / "tools" / "meta.py",
                       "list_connected_providers")
    calls = [
        n for n in ast.walk(fn)
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
        and n.func.attr == "set_provider_count"
    ]
    assert calls, (
        "nothing reports the connected count from the one place that has already "
        "resolved credentials, so provider_count stays 0 forever"
    )


def test_no_startup_path_resolves_credentials_just_to_fill_in_telemetry():
    """The reason the count is not computed at startup.

    AWS.is_configured() goes through botocore and can sit on the EC2 metadata
    endpoint for about a minute when there are no local credentials, which is
    the exact profile of the install that would answer "nothing connected". A
    telemetry field is never worth a minute of someone's first run.

    Checked against the AST rather than the raw text, because the docstrings in
    that module explain this rule and therefore contain the very names being
    forbidden. Grepping the source fails on its own explanation.
    """
    tree = ast.parse((ROOT / "src" / "finops" / "telemetry.py").read_text(encoding="utf-8"))
    referenced = {
        n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)
    } | {
        n.id for n in ast.walk(tree) if isinstance(n, ast.Name)
    }
    for forbidden in ("is_configured", "CLOUD_CONNECTORS", "SAAS_CONNECTORS"):
        assert forbidden not in referenced, (
            f"telemetry.py calls {forbidden}: it is resolving credentials to "
            f"populate a metric, which puts a network probe on the startup path"
        )
