"""Tests for the local AI-agent budget meter (finops.ai_budget)."""
from __future__ import annotations

import json
import time
from datetime import datetime, timezone

import pytest

from finops import ai_budget as ab


def _write_session(claude_dir, records):
    proj = claude_dir / "projects" / "-Users-x-proj"
    proj.mkdir(parents=True, exist_ok=True)
    f = proj / "sess.jsonl"
    f.write_text("\n".join(json.dumps(r) for r in records) + "\n")
    return f


def _assistant(ts_epoch, tin=0, tout=0, cwrite=0, cread=0, model="claude-sonnet-5"):
    return {
        "timestamp": datetime.fromtimestamp(ts_epoch, tz=timezone.utc)
        .isoformat().replace("+00:00", "Z"),
        "message": {"model": model, "usage": {
            "input_tokens": tin, "output_tokens": tout,
            "cache_creation_input_tokens": cwrite, "cache_read_input_tokens": cread,
        }},
    }


@pytest.fixture(autouse=True)
def _isolate(tmp_path, monkeypatch):
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "claude"))
    monkeypatch.setenv("FINOPS_DATA_DIR", str(tmp_path / "data"))
    yield


def test_billable_excludes_cache_read(tmp_path):
    now = time.time()
    _write_session(tmp_path / "claude", [
        _assistant(now - 60, tin=1000, tout=500, cwrite=2000, cread=9_000_000),
    ])
    u = ab.read_agent_usage(now - 3600)
    assert u["billable_tokens"] == 3500          # 1000 + 500 + 2000, NOT the 9M cache read
    assert u["cache_read_tokens"] == 9_000_000
    assert u["messages"] == 1


def test_window_filters_old_records(tmp_path):
    now = time.time()
    _write_session(tmp_path / "claude", [
        _assistant(now - 100, tin=10, tout=10),          # inside 1h window
        _assistant(now - 10 * 24 * 3600, tin=999, tout=999),  # 10 days ago, excluded
    ])
    u = ab.read_agent_usage(now - 3600)
    assert u["billable_tokens"] == 20                # only the recent record counted
    assert u["messages"] == 1


def test_budget_roundtrip_and_verdicts(tmp_path, monkeypatch):
    # 90 tokens billable this month, budget of 100 tokens -> WARN (>=80%)
    now = time.time()
    _write_session(tmp_path / "claude", [_assistant(now - 60, tin=60, tout=30)])
    ab.set_budget(monthly_tokens=100)
    assert ab.get_budget()["monthly_tokens"] == 100
    st = ab.status()
    assert st["billable_tokens_mtd"] == 90
    assert st["verdict"] == ab.BUDGET_WARN

    ab.set_budget(monthly_tokens=50)                 # now 90/50 -> OVER
    assert ab.status()["verdict"] == ab.BUDGET_OVER

    ab.set_budget(monthly_tokens=100000)             # plenty -> OK
    assert ab.status()["verdict"] == ab.BUDGET_OK


def test_gate_is_advice_only_and_projects_next_task(tmp_path):
    now = time.time()
    _write_session(tmp_path / "claude", [_assistant(now - 60, tin=40, tout=40)])
    ab.set_budget(monthly_tokens=100)                # 80/100 already -> WARN
    g = ab.check()
    assert g["advice_only"] is True
    assert g["verdict"] == ab.BUDGET_WARN
    # a next task that blows the token budget escalates the verdict + reason
    g2 = ab.check(estimated_next_tokens=1000)
    assert g2["verdict"] in (ab.BUDGET_WARN, ab.BUDGET_OVER)
    assert "token budget" in g2["reason"]


def test_subscription_never_over_on_dollar_estimate(tmp_path):
    # A Max user pulls far more compute than their flat fee. That must NOT read as
    # "over budget" — the flat fee is what they actually pay. This is the bug fix.
    now = time.time()
    _write_session(tmp_path / "claude", [
        _assistant(now - 60, tin=1_000_000, tout=500_000, cwrite=100_000_000),
    ])
    ab.set_budget(mode="flat", plan_cost=200)         # arbitrary plan cost, any number
    st = ab.status()
    assert st["mode"] == "flat"
    assert st["verdict"] == ab.BUDGET_OK              # never OVER off a list-price estimate
    assert st["subsidy"] is not None
    assert st["subsidy"]["plan_cost_usd"] == 200.0
    assert st["subsidy"]["multiple"] and st["subsidy"]["multiple"] > 1
    assert st["cost_per_1m_effective"] is not None    # $200 spread over the tokens pulled
    # the gate also must not cry "over budget" on a subscription
    g = ab.check()
    assert g["verdict"] == ab.BUDGET_OK
    assert g["mode"] == "flat"


def test_plan_cost_infers_flat_mode(tmp_path):
    # Passing a plan cost alone is enough; mode falls out of it (matches the wizard).
    ab.set_budget(plan_cost=100)
    assert ab.get_budget()["mode"] == "flat"
    ab.reset_budget()
    ab.set_budget(spend_cap=2500)
    assert ab.get_budget()["mode"] == "metered"


def test_metered_gates_on_spend_cap(tmp_path):
    # Metered API lens: an estimated-dollar spend cap does drive the verdict, and we
    # surface a cost-per-1M rate. (For metered, the list-price estimate IS the basis
    # until an Admin key is wired for exact spend.)
    now = time.time()
    _write_session(tmp_path / "claude", [
        _assistant(now - 60, tin=2_000_000, tout=1_000_000),   # ~$ of list-price compute
    ])
    ab.set_budget(mode="metered", spend_cap=1)        # tiny cap so any usage trips it
    st = ab.status()
    assert st["mode"] == "metered"
    assert st["verdict_basis"] == "spend"
    assert st["verdict"] == ab.BUDGET_OVER
    assert st["cost_per_1m_list"] is not None
    assert st["subsidy"] is None                      # no subsidy story on metered


def test_reset_forgets_budget(tmp_path):
    ab.set_budget(mode="flat", plan_cost=100)
    assert ab.get_budget()["mode"] == "flat"
    ab.reset_budget()
    assert ab.get_budget()["mode"] == ""


def test_welcome_teaser_is_the_front_door(tmp_path):
    # The first-run banner leads with the agent's own usage when Claude Code logs
    # exist, and shows nothing (no empty block) when they don't.
    from finops import welcome
    assert welcome._agent_usage_teaser() is None       # no logs -> no teaser
    now = time.time()
    _write_session(tmp_path / "claude", [_assistant(now - 60, tin=500_000, tout=250_000)])
    line = welcome._agent_usage_teaser()
    assert line and "last 5h" in line[0] and "/hour" in line[0]


def test_no_logs_is_graceful(tmp_path):
    # No claude dir at all: empty, source_present False, no crash.
    u = ab.read_agent_usage(time.time() - 3600)
    assert u["billable_tokens"] == 0
    assert u["source_present"] is False
    assert ab.status()["verdict"] == ab.BUDGET_OK


def test_configured_budget_confirms_even_with_zero_usage(tmp_path):
    # DevEx regression: after setting a budget with no agent usage yet, the status
    # must confirm the budget, never nag you to set the budget you just set.
    # Asserted on the wording-independent core ("to set a budget") so rebranding
    # the command in the copy cannot turn this into a vacuous pass.
    ab.set_budget(mode="flat", plan_cost=100)
    st = ab.status()
    assert st["billable_tokens_mtd"] == 0
    assert "to set a budget" not in st["summary"]
    assert "$100/mo plan is set" in st["summary"]

    ab.reset_budget()
    ab.set_budget(mode="metered", spend_cap=2500)
    # metered already confirms via the spend-cap branch (never said "set a budget")
    smy = ab.status()["summary"]
    assert "$2,500 spend cap" in smy and "to set a budget" not in smy
