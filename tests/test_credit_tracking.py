"""Tests for AWS credit-runway tracking, cash-flip detection, and AI-billing
blind spots."""
from datetime import date

import pytest

from finops.connectors.credit_tracking import (
    analyze_credits,
    detect_billing_blind_spots,
    fetch_record_type_monthly,
)


# ── analyze_credits ──────────────────────────────────────────────────────────

def _month(month, gross, credits, net):
    return {"month": month, "gross": gross, "credits": credits,
            "refunds": 0.0, "net_cash": net, "by_type": {}}


def test_cash_flip_detected_when_credits_stop_covering():
    series = [
        _month("2026-04-01", gross=1000.0, credits=950.0, net=50.0),   # cov .95
        _month("2026-05-01", gross=1100.0, credits=900.0, net=200.0),  # cov .82
        _month("2026-06-01", gross=1200.0, credits=10.0, net=1190.0),  # cov ~0 — flip
    ]
    res = analyze_credits(series)
    assert res["cash_flip_detected"] is True
    assert res["status"] == "critical"
    assert res["latest_net_cash_usd"] == 1190.0
    assert res["credits_active"] is True


def test_healthy_credit_coverage_is_ok_no_flip():
    series = [
        _month("2026-05-01", gross=1000.0, credits=900.0, net=100.0),  # cov .90
        _month("2026-06-01", gross=1000.0, credits=880.0, net=120.0),  # cov .88
    ]
    res = analyze_credits(series)
    assert res["cash_flip_detected"] is False
    assert res["status"] == "ok"
    assert res["latest_credit_coverage_pct"] == 88.0


def test_dropping_coverage_warns_before_flip():
    series = [
        _month("2026-05-01", gross=1000.0, credits=900.0, net=100.0),  # cov .90
        _month("2026-06-01", gross=1000.0, credits=300.0, net=700.0),  # cov .30
    ]
    res = analyze_credits(series)
    assert res["status"] == "warning"
    assert res["cash_flip_detected"] is False


def test_no_data_is_handled():
    assert analyze_credits([])["status"] == "no_data"


def test_no_credits_just_cash_is_ok():
    series = [
        _month("2026-05-01", gross=500.0, credits=0.0, net=500.0),
        _month("2026-06-01", gross=600.0, credits=0.0, net=600.0),
    ]
    res = analyze_credits(series)
    assert res["status"] == "ok"
    assert res["credits_active"] is False
    assert res["cash_flip_detected"] is False


# ── fetch_record_type_monthly (Cost Explorer RECORD_TYPE) ────────────────────

class _FakeCE:
    def __init__(self, resp):
        self._resp = resp
        self.calls = []

    def get_cost_and_usage(self, **kwargs):
        self.calls.append(kwargs)
        return self._resp


def test_fetch_record_type_monthly_parses_credits_and_net():
    resp = {"ResultsByTime": [
        {"TimePeriod": {"Start": "2026-06-01"}, "Groups": [
            {"Keys": ["Usage"],  "Metrics": {"UnblendedCost": {"Amount": "1200.0"}}},
            {"Keys": ["Credit"], "Metrics": {"UnblendedCost": {"Amount": "-1150.0"}}},
            {"Keys": ["Tax"],    "Metrics": {"UnblendedCost": {"Amount": "30.0"}}},
        ]},
    ]}
    ce = _FakeCE(resp)
    rows = fetch_record_type_monthly(months=1, today=date(2026, 6, 15), ce=ce)
    assert len(rows) == 1
    row = rows[0]
    assert row["credits"] == 1150.0          # magnitude of the negative credit row
    assert row["gross"] == 1230.0            # positive rows: usage + tax
    assert row["net_cash"] == 80.0           # 1200 - 1150 + 30
    # The query must group by RECORD_TYPE, monthly.
    assert ce.calls[0]["GroupBy"] == [{"Type": "DIMENSION", "Key": "RECORD_TYPE"}]
    assert ce.calls[0]["Granularity"] == "MONTHLY"


# ── AI-billing blind spots ───────────────────────────────────────────────────

def test_blind_spots_flag_bedrock_and_marketplace():
    by_service = {
        "Amazon Bedrock": 500.0,
        "AWS Marketplace": 120.0,
        "Amazon EC2": 4000.0,        # not a blind spot
        "Amazon SageMaker": 0.0,     # zero spend, skipped
    }
    res = detect_billing_blind_spots(by_service)
    assert res["blind_spot_count"] == 2
    assert res["total_blind_spot_usd"] == 620.0
    assert res["findings"][0]["service"] == "Amazon Bedrock"   # sorted desc


def test_blind_spots_empty_when_no_ai_marketplace_spend():
    res = detect_billing_blind_spots({"Amazon EC2": 1000.0, "Amazon S3": 50.0})
    assert res["blind_spot_count"] == 0
    assert res["total_blind_spot_usd"] == 0.0


# ── scheduler registration ───────────────────────────────────────────────────



def test_partial_current_month_does_not_false_flip():
    """The credit alarm must not cry wolf at month start. Prior months ~100%
    covered, current month has only started accruing (gross tiny, credits not yet
    posted by AWS) -> must NOT be critical OR warning, since the scheduler alerts
    on both. Assesses the latest settled month instead."""
    series = [_month(f"2026-0{i}-01", 3000.0, 3000.0, 0.0) for i in range(1, 6)]
    series.append(_month("2026-06-01", gross=80.0, credits=0.0, net=80.0))
    res = analyze_credits(series)
    assert res["cash_flip_detected"] is False
    assert res["status"] not in ("critical", "warning")
    assert res["latest_credit_coverage_pct"] == 100.0  # the settled month, not the partial one


def test_single_month_full_credit_coverage_is_active():
    """A fully credit-covered single month (months=1) must register as credits
    active and report 100% coverage, not 'no spend or credits detected'."""
    res = analyze_credits([_month("2026-06-01", gross=5000.0, credits=5000.0, net=0.0)])
    assert res["credits_active"] is True
    assert res["latest_credit_coverage_pct"] == 100.0
    assert res["cash_flip_detected"] is False


def test_mature_month_still_flips():
    """The guard must not suppress a REAL flip: a fully-accrued current month whose
    credits stopped covering still trips critical."""
    series = [
        _month("2026-04-01", 1000.0, 950.0, 50.0),
        _month("2026-05-01", 1100.0, 900.0, 200.0),
        _month("2026-06-01", 1200.0, 10.0, 1190.0),   # mature gross, credits gone
    ]
    res = analyze_credits(series)
    assert res["cash_flip_detected"] is True
    assert res["status"] == "critical"
