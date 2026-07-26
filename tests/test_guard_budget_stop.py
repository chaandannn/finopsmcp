"""The guard stops an agent that is burning through its own AI budget.

Two properties this file exists to hold.

1. The stop is REAL but opt-in. Denying every tool call is correct for someone
   who set a budget and meant it, and wrong as a default: a tool that silently
   bricks your agent gets uninstalled before anyone finds the setting that did
   it. Default is a confirmation; FINOPS_GUARD_STOP_ON_BUDGET=1 makes it a deny.

2. The guard is FREE, permanently. It used to sit in PRO_FEATURES and be free
   only via the reversible `_HOLD_AI_UNGATE` flag, which meant the day pricing
   shipped, `gate_command` would return None for every free user. The front door
   would have gone silent, failing open with no message, at exactly the moment
   it mattered most. Free-by-hold is not free.

The budget read is local (Claude Code session logs), so none of this needs a
cloud account, an API key, or a network.
"""
from __future__ import annotations

import importlib

import pytest

import finops.ai_budget as ai_budget
import finops.guard as guard


def _over(basis="tokens"):
    return {
        "verdict": ai_budget.BUDGET_OVER,
        "verdict_basis": basis,
        "pct_of_budget": 1.42,
        "billable_tokens_mtd": 14_200_000,
        "est_usd_mtd_list_price": 213.0,
        "budget": {"monthly_tokens": 10_000_000, "spend_cap": 150.0},
    }


@pytest.fixture(autouse=True)
def _no_ambient_budget(monkeypatch):
    """Default every test to a clean under-budget state and no hard stop, so a
    developer's own real budget cannot change the result."""
    monkeypatch.delenv("FINOPS_GUARD_STOP_ON_BUDGET", raising=False)
    monkeypatch.setattr(ai_budget, "status", lambda: {"verdict": ai_budget.BUDGET_OK})


# ── the stop ────────────────────────────────────────────────────────────────


def test_over_budget_asks_by_default(monkeypatch):
    monkeypatch.setattr(ai_budget, "status", _over)
    r = guard.gate_command("ls -la")
    assert r["decision"] == "ask"
    assert r["action_type"] == "ai_budget"


def test_over_budget_denies_when_the_hard_stop_is_opted_into(monkeypatch):
    monkeypatch.setattr(ai_budget, "status", _over)
    monkeypatch.setenv("FINOPS_GUARD_STOP_ON_BUDGET", "1")
    assert guard.gate_command("ls -la")["decision"] == "deny"


def test_the_stop_applies_to_every_command_not_just_infra(monkeypatch):
    """"Stop the agent because it is spending too much" means stop it, not stop
    it from touching Terraform. A harmless command is still a turn that costs
    tokens."""
    monkeypatch.setattr(ai_budget, "status", _over)
    for cmd in ("ls -la", "cat README.md", "git status", "echo hi"):
        assert guard.gate_command(cmd) is not None, f"{cmd!r} slipped past the budget stop"


def test_the_reason_carries_the_actual_numbers(monkeypatch):
    """A stop with no figure is just an obstruction. The user has to be able to
    decide whether to raise the budget without leaving the terminal."""
    monkeypatch.setattr(ai_budget, "status", _over)
    reason = guard.gate_command("ls -la")["reason"]
    assert "14,200,000" in reason and "10,000,000" in reason
    assert "ai-budget set" in reason

    monkeypatch.setattr(ai_budget, "status", lambda: _over("spend"))
    reason = guard.gate_command("ls -la")["reason"]
    assert "$213" in reason and "$150" in reason


def test_default_mode_tells_you_how_to_make_it_a_hard_stop(monkeypatch):
    monkeypatch.setattr(ai_budget, "status", _over)
    assert "FINOPS_GUARD_STOP_ON_BUDGET" in guard.gate_command("ls -la")["reason"]


# ── it must not fire when it should not ─────────────────────────────────────


def test_under_budget_stays_silent_on_harmless_commands():
    assert guard.gate_command("ls -la") is None


def test_under_budget_still_guards_one_way_doors():
    """The budget stop is additive. Removing it must not disarm door blocking."""
    assert (guard.gate_command("terraform destroy -auto-approve") or {}).get("decision") == "ask"


def test_no_budget_configured_is_not_a_reason_to_block(monkeypatch):
    monkeypatch.setattr(ai_budget, "status", lambda: {"verdict": ai_budget.BUDGET_OK})
    assert guard.gate_command("ls -la") is None


def test_warn_state_does_not_stop_anything(monkeypatch):
    monkeypatch.setattr(ai_budget, "status", lambda: {"verdict": ai_budget.BUDGET_WARN})
    assert guard.gate_command("ls -la") is None


def test_an_unreadable_budget_never_blocks(monkeypatch):
    """A guard that cannot read its own budget must not take a position. This is
    the one place fail-open is unconditionally right: the alternative is bricking
    someone's agent because of a bug in our log parser."""
    def boom():
        raise RuntimeError("session logs unreadable")

    monkeypatch.setattr(ai_budget, "status", boom)
    monkeypatch.setenv("FINOPS_GUARD_STOP_ON_BUDGET", "1")
    assert guard.gate_command("ls -la") is None
    assert guard.check_budget_gate() is None


# ── free forever, not free-by-hold ──────────────────────────────────────────


def test_guard_is_not_a_pro_feature():
    from finops.license import PRO_FEATURES
    assert "agent_gate" not in PRO_FEATURES, (
        "the guard is the front door; gating it means the one surface that works "
        "without a cloud account goes dark for exactly the people it should reach"
    )


def test_guard_still_works_after_pricing_ships(monkeypatch):
    """Simulates the future: the temporary AI ungate is lifted and the user has no
    license. Before this change that combination made gate_command return None for
    every command, silently."""
    import finops.license as lic

    class _NoLicense:
        is_pro = False

    monkeypatch.setattr(lic, "_HOLD_AI_UNGATE", False)
    monkeypatch.setattr(lic, "get_status", lambda *a, **k: _NoLicense())
    importlib.reload(guard)
    try:
        assert (guard.gate_command("terraform destroy") or {}).get("decision") == "ask"
        assert (guard.gate_command("aws s3 rb s3://x --force") or {}).get("decision") == "ask"
    finally:
        importlib.reload(guard)


# ── the choice is the user's, made once, and remembered ────────────────────


def test_stop_preference_is_read_from_the_saved_budget(monkeypatch, tmp_path):
    """Asked at `nable guard install`, stored on the budget, honoured thereafter.
    An env var is fine for CI but it is not something you ask a human to set."""
    import finops.ai_budget as ab

    monkeypatch.setenv("FINOPS_DATA_DIR", str(tmp_path))
    monkeypatch.delenv("FINOPS_GUARD_STOP_ON_BUDGET", raising=False)
    monkeypatch.setattr(ai_budget, "status", _over)

    assert ab.get_budget()["on_breach"] == "notify", "default must not be a hard stop"
    assert guard.gate_command("ls -la")["decision"] == "ask"

    ab.set_budget(on_breach="stop")
    assert guard.gate_command("ls -la")["decision"] == "deny"

    ab.set_budget(on_breach="notify")
    assert guard.gate_command("ls -la")["decision"] == "ask"


def test_env_var_overrides_the_saved_preference(monkeypatch, tmp_path):
    """One-off override for a CI run, in both directions."""
    import finops.ai_budget as ab

    monkeypatch.setenv("FINOPS_DATA_DIR", str(tmp_path))
    monkeypatch.setattr(ai_budget, "status", _over)
    ab.set_budget(on_breach="stop")

    monkeypatch.setenv("FINOPS_GUARD_STOP_ON_BUDGET", "0")
    assert guard.gate_command("ls -la")["decision"] == "ask"

    monkeypatch.setenv("FINOPS_GUARD_STOP_ON_BUDGET", "1")
    assert guard.gate_command("ls -la")["decision"] == "deny"


def test_an_unwritable_budget_does_not_become_a_hard_stop(monkeypatch):
    """Fail toward notify, never toward deny."""
    import finops.ai_budget as ab

    monkeypatch.setattr(ai_budget, "status", _over)
    monkeypatch.setattr(ab, "get_budget", lambda: (_ for _ in ()).throw(OSError("boom")))
    assert guard.gate_command("ls -la")["decision"] == "ask"
