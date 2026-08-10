"""Three credibility fixes from the deferred cynical-audit judgment calls.

1. Textract: the SAVING headline is the conservative floor (half of measured
   non-prod spend), never the best case, and the full range rides in metadata.
2. Drop alerts: routine cost drops are recorded but not pushed to Slack/Teams;
   only high-severity drops page (something may have stopped running). Spikes
   always page. FINOPS_ALERT_DROPS tunes it.
3. account_id auto-discovery: natural questions ("scan for waste", "benchmark
   us") must not require knowing a 12-digit account id.
"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from finops.scheduler.jobs import should_alert


# ── 1. Textract conservative headline ───────────────────────────────────────────


def test_textract_headline_is_the_floor_with_range_in_metadata(monkeypatch):
    from finops.recommendations import textract_env as t
    monkeypatch.setattr(t, "_make_ce", lambda role_arn=None: object())
    monkeypatch.setattr(t, "_make_lambda", lambda region, role_arn=None: object())
    monkeypatch.setattr(t, "_make_cloudtrail", lambda region="us-east-1", role_arn=None: object())
    monkeypatch.setattr(t, "_get_cloudtrail_callers", lambda ct, s, e: {})
    monkeypatch.setattr(t, "_get_total_textract_spend", lambda ce, s, e: 5000.0)
    monkeypatch.setattr(t, "_get_tagged_env_breakdown",
                        lambda ce, s, e: {"prod": 3000.0, "staging": 1000.0,
                                          "qa": 1000.0, "unknown": 0.0})
    out = t.scan_textract_environment_waste(days=30)
    f = out["finding"]
    waste = out["estimated_monthly_waste"]
    assert f["kind"] == "recommendation"
    # Headline = half the measured non-prod spend, range = [floor, full].
    assert f["est_monthly_savings"] == pytest.approx(waste * 0.5, abs=0.01)
    assert f["metadata"]["savings_range_monthly"] == [
        pytest.approx(waste * 0.5, abs=0.01), waste]
    assert f["metadata"]["non_prod_monthly_spend"] == waste
    # The assumption behind the floor is stated, not hidden.
    assert any("half" in a for a in f["assumptions"])


# ── 2. Drop-alert gating ─────────────────────────────────────────────────────────


def test_spikes_always_alert(monkeypatch):
    for policy in ("high", "all", "never", ""):
        monkeypatch.setenv("FINOPS_ALERT_DROPS", policy)
        for sev in ("low", "medium", "high"):
            assert should_alert("spike", sev) is True


def test_default_policy_only_high_drops_alert(monkeypatch):
    monkeypatch.delenv("FINOPS_ALERT_DROPS", raising=False)
    assert should_alert("drop", "low") is False
    assert should_alert("drop", "medium") is False
    assert should_alert("drop", "high") is True


def test_drop_policy_all_and_never(monkeypatch):
    monkeypatch.setenv("FINOPS_ALERT_DROPS", "all")
    assert should_alert("drop", "low") is True
    monkeypatch.setenv("FINOPS_ALERT_DROPS", "never")
    assert should_alert("drop", "high") is False


# ── 3. account_id auto-discovery ────────────────────────────────────────────────


def _aws_stub(acct="123456789012", configured=True):
    async def _is_configured():
        return configured
    return SimpleNamespace(is_configured=_is_configured, _account_id=lambda: acct)


def test_resolver_prefers_explicit_id():
    from finops import server as srv
    out = asyncio.run(srv._resolve_account_id("999888777666"))
    assert out == "999888777666"


def test_resolver_auto_discovers_from_sts():
    from finops import server as srv
    with patch.dict(srv.CLOUD_CONNECTORS, {"aws": _aws_stub()}):
        assert asyncio.run(srv._resolve_account_id(None)) == "123456789012"


def test_resolver_empty_when_nothing_connected():
    from finops import server as srv
    with patch.dict(srv.CLOUD_CONNECTORS, {"aws": _aws_stub(configured=False)}):
        assert asyncio.run(srv._resolve_account_id(None)) == ""


# ── get_anomalies framing (spikes vs drops split) ───────────────────────────────


def test_anomaly_summary_splits_spikes_and_drops(monkeypatch):
    from finops import server as srv
    rows = [
        {"id": 1, "provider": "aws", "service": "S", "account_id": "a", "severity": "high",
         "direction": "spike", "pct_change": 120.0, "current_amount": 22.0,
         "baseline_mean": 10.0, "z_score": 3.0, "detected_at": "d", "snapshot_date": "s"},
        {"id": 2, "provider": "aws", "service": "R", "account_id": "a", "severity": "low",
         "direction": "drop", "pct_change": -40.0, "current_amount": 6.0,
         "baseline_mean": 10.0, "z_score": -2.5, "detected_at": "d", "snapshot_date": "s"},
    ]
    monkeypatch.setattr("finops.anomaly.detector.get_active_anomalies",
                        lambda **kw: rows)
    monkeypatch.setattr(srv, "_load_alert_policies", lambda: [])
    out = asyncio.run(srv.get_anomalies())
    assert out["count"] == 2
    assert out["spikes"] == 1
    assert out["drops"] == 1
    assert "drops_note" in out
