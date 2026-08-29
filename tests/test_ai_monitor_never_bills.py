"""The nightly AI monitor must not charge the customer to run.

get_bedrock_costs makes two ce:GetCostAndUsage calls. Cost Explorer bills per
request against the customer's own account, and job_ai_monitor runs at 05:00
every night with nobody watching. llm_costs.py contained no reference to the
unattended guard at all, so every customer was charged nightly for a report
they never asked for.

Driven through the guard the scheduler actually applies, not by calling the
helper with a flag, because the bug was that the module never consulted it.
"""
from __future__ import annotations

import pytest

from finops import billing_access
from finops.connectors import llm_costs


def test_bedrock_costs_makes_no_cost_explorer_call_when_unattended(monkeypatch):
    import datetime as dt

    def explode(*a, **k):  # pragma: no cover
        raise AssertionError("built a Cost Explorer client on the unattended path")

    monkeypatch.setattr("boto3.client", explode, raising=False)

    with billing_access.unattended_context():
        out = llm_costs.get_bedrock_costs(dt.date(2026, 8, 1), dt.date(2026, 8, 2))

    assert out["total_usd"] == 0.0
    assert "cost_explorer" in out["reason"], out


def test_the_attended_path_is_still_allowed_to_ask(monkeypatch):
    """The guard must not simply disable Bedrock. A person who opens the
    dashboard and asks is exactly who Cost Explorer is for."""
    import datetime as dt

    reached = {}

    def fake_client(name, **k):
        reached["ce"] = True
        raise RuntimeError("stop here, we only needed to prove we got this far")

    monkeypatch.setattr("boto3.client", fake_client, raising=False)

    with billing_access.attended_context():
        llm_costs.get_bedrock_costs(dt.date(2026, 8, 1), dt.date(2026, 8, 2))

    assert reached.get("ce"), "the attended path was blocked too"
