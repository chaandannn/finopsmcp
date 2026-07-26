"""Free tier = read-only, talk to your bill. The agent team is Pro.

These tests pin the tier boundary for the three agents:
  - Budget Guard: check_action_policy + the guard hook
  - Savings Analyst actions: generate_terraform_tag_fixes (PR tools already gated)
  - The Ledger: mark_recommendation_acted_on / verify_savings / get_recommendation_learning

A free user gets one compact upgrade payload (error=pro_required with the activate
path), never a crash and never the feature. A pro user passes straight through.
"""
from __future__ import annotations

import asyncio

import pytest

from finops import server
from finops.license import LicenseStatus


def _run(coro):
    return asyncio.run(coro)


@pytest.fixture
def free(monkeypatch):
    monkeypatch.delenv("FINOPS_DEMO_MODE", raising=False)
    monkeypatch.setattr(
        "finops.license.get_status",
        lambda: LicenseStatus(mode="free", email="", issued="", message=""),
    )


@pytest.fixture
def pro(monkeypatch):
    monkeypatch.delenv("FINOPS_DEMO_MODE", raising=False)
    monkeypatch.setattr(
        "finops.license.get_status",
        lambda: LicenseStatus(mode="pro", email="dev@acme.com", issued="2026-07-01", message=""),
    )


@pytest.fixture
def regated(monkeypatch):
    # Free tier with the temporary AI ungate turned OFF: the agent features gate
    # again. Proves the upgrade payload and the re-gate path survive the hold, so
    # re-gating is one flag flip when the paid model ships.
    monkeypatch.delenv("FINOPS_DEMO_MODE", raising=False)
    monkeypatch.setattr("finops.license._HOLD_AI_UNGATE", False)
    monkeypatch.setattr(
        "finops.license.get_status",
        lambda: LicenseStatus(mode="free", email="", issued="", message=""),
    )


def _assert_upgrade_payload(r, feature):
    assert r["error"] == "pro_required"
    assert r["feature"] == feature
    assert r["upgrade_url"]
    assert r["activate_command"]
    # The pitch names concrete capabilities, not a generic "upgrade". It used to
    # assert "Budget Guard"; the guard left the Pro list (free forever, it is the
    # front door), so the upsell must sell what is actually behind the paywall.
    assert "Budget Guard" not in r["message"], (
        "the upsell is offering the guard, which is free; that is a broken promise"
    )
    assert any(k in r["message"] for k in ("pull request", "Ledger", "verified")), (
        f"upgrade copy names no concrete Pro capability: {r['message']!r}"
    )


# ── Under the temporary AI ungate (2026-07-10): the agent team runs for free ────
# The features stay wired to require_pro; the hold just makes the gate pass. The
# `regated` tests below flip the hold off and confirm the upgrade payload returns.

def test_free_check_action_policy_runs_under_hold(free, monkeypatch):
    monkeypatch.setattr("finops.budget.enforcer.list_budgets", lambda **k: [])
    r = _run(server.check_action_policy(action_type="rightsizing", monthly_delta_usd=-100.0))
    assert "gate" in r and r.get("error") is None


def test_pro_check_action_policy_passes(pro, monkeypatch):
    monkeypatch.setattr("finops.budget.enforcer.list_budgets", lambda **k: [])
    r = _run(server.check_action_policy(action_type="rightsizing", monthly_delta_usd=-100.0))
    assert "gate" in r and r.get("error") is None


def test_free_ledger_and_remediation_run_under_hold(free):
    # These just must not be gated anymore; they may still return other errors
    # (a missing recommendation, an empty tf dir), so we only assert not-gated.
    for r in (
        _run(server.mark_recommendation_acted_on(1)),
        _run(server.verify_savings()),
        _run(server.get_recommendation_learning()),
        _run(server.generate_terraform_tag_fixes(tf_dir="/tmp")),
    ):
        assert r.get("error") != "pro_required"


# ── Re-gate proof: flip the hold off and the upgrade payload returns ───────────

def test_check_action_policy_stays_free_even_after_pricing_ships(regated):
    """Inverted deliberately. The policy gate is the same capability as the
    `nable guard` hook reached through MCP, and the hook is free forever, so
    gating this surface would be incoherent. `regated` lifts the temporary AI
    ungate, i.e. it simulates the day the paid model launches."""
    r = _run(server.check_action_policy(action_type="rightsizing", monthly_delta_usd=-100.0))
    assert r.get("error") != "pro_required"
    assert "gate" in r


def test_regated_ledger_and_remediation_gate(regated):
    # agent_gate is intentionally absent here: the guard left PRO_FEATURES.
    _assert_upgrade_payload(_run(server.mark_recommendation_acted_on(1)), "agent_learning")
    _assert_upgrade_payload(_run(server.verify_savings()), "agent_learning")
    _assert_upgrade_payload(_run(server.get_recommendation_learning()), "agent_learning")
    _assert_upgrade_payload(_run(server.generate_terraform_tag_fixes(tf_dir="/tmp")), "remediation")


# ── Free stays useful: read-only talk-to-your-bill is NOT gated ────────────────

def test_free_can_still_read_the_ledger(free):
    # get_savings_summary is read-only: free users can see what nable found.
    r = _run(server.get_savings_summary())
    assert r.get("error") != "pro_required"


def test_free_estimate_change_cost_stays_free(free, monkeypatch):
    # The preflight estimate is the on-ramp: "what would this change cost" is
    # talking to your bill, so it stays free.
    monkeypatch.setattr("finops.budget.enforcer.list_budgets", lambda **k: [])
    r = _run(server.estimate_change_cost(monthly_delta_usd=42.0))
    assert r.get("error") != "pro_required"


# ── the agent-team surface ─────────────────────────────────────────────────────

def test_agent_team_free_under_hold_is_available(free, monkeypatch):
    # Under the AI ungate, a free user's agents are available (needs_setup /
    # active), not paywalled.
    monkeypatch.setattr("finops.budget.enforcer.list_budgets", lambda **k: [])
    r = _run(server.get_agent_team())
    assert len(r["agents"]) == 3
    for a in r["agents"]:
        assert a["status"] != "pro_required"
    assert "Propose-only" in r["note"]


def test_agent_team_regated_shows_unlock_path(regated):
    """After pricing ships, the two Pro agents ask for an upgrade and Budget
    Guard does NOT: it is free forever, so its status reflects setup state, not
    licensing."""
    r = _run(server.get_agent_team())
    assert r["plan"] == "free"
    assert len(r["agents"]) == 3
    by_name = {a["agent"]: a for a in r["agents"]}
    guard = by_name["Budget Guard"]
    assert guard["status"] != "pro_required", (
        "the guard is the front door; it must never ask for a license"
    )
    for name in ("Savings Analyst", "the Ledger"):
        a = by_name[name]
        assert a["status"] == "pro_required"
        assert any("activate_pro" in s or "login" in s for s in a["setup"])
    assert "Propose-only" in r["note"]


def test_agent_team_pro_reports_setup_state(pro, monkeypatch):
    monkeypatch.setattr("finops.budget.enforcer.list_budgets", lambda **k: [])
    r = _run(server.get_agent_team())
    names = [a["agent"] for a in r["agents"]]
    assert names == ["Budget Guard", "Savings Analyst", "the Ledger"]
    guard = r["agents"][0]
    # No hook + no budget on this box -> needs_setup with concrete steps.
    assert guard["status"] in ("needs_setup", "active")
    if guard["status"] == "needs_setup":
        assert any("guard install" in s or "budget" in s for s in guard["setup"])
    ledger = r["agents"][2]
    assert ledger["learning"] is not None and "state" in ledger["learning"]
