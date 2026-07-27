"""Regression test for the zero-spend false positive.

A grouped Cost Explorer query (the default, grouped by SERVICE) returns each
period's costs under Groups[]; the per-period Total field comes back empty. The
old detector read that empty Total and concluded "all costs are zero", so it
flagged real-spend accounts as free/new-account zero-spend. A $14k account got a
"this account has $0.00 in spend" note attached.

The flag must come from the real merged total (which sums Groups), and only when
Cost Explorer actually returned rows.
"""
from __future__ import annotations

import asyncio
from datetime import date
from unittest.mock import MagicMock

import pytest

from finops import cache
from finops.connectors.aws import AWSConnector

START = date(2026, 5, 21)
END = date(2026, 6, 20)


def _grouped_response(*services: tuple[str, float]) -> dict:
    """Mirror real CE grouped output: empty Total, costs live under Groups[]."""
    return {
        "ResultsByTime": [
            {
                "TimePeriod": {"Start": START.isoformat(), "End": END.isoformat()},
                "Total": {},  # AWS leaves this empty when a GroupBy is set
                "Groups": [
                    {
                        "Keys": [name],
                        "Metrics": {"UnblendedCost": {"Amount": str(amount), "Unit": "USD"}},
                    }
                    for name, amount in services
                ],
                "Estimated": False,
            }
        ]
    }


def _connector_with_ce(response: dict) -> AWSConnector:
    cache.clear()  # read-through cache would otherwise serve a prior test's result
    conn = AWSConnector()
    fake_ce = MagicMock()
    fake_ce.get_cost_and_usage.return_value = response
    conn._make_client = lambda role_arn=None: fake_ce
    conn._account_id = lambda role_arn=None: "123456789012"
    return conn


def test_grouped_spend_is_not_flagged_zero():
    conn = _connector_with_ce(
        _grouped_response(("Amazon Textract", 5558.16), ("Amazon DocumentDB", 2033.13))
    )
    summary = asyncio.run(conn.get_costs(START, END))

    assert summary.total_usd == pytest.approx(7591.29, abs=0.01)
    assert summary.by_service["Amazon Textract"] == pytest.approx(5558.16, abs=0.01)
    assert getattr(summary, "_zero_spend_account", False) is False


def test_genuine_zero_spend_is_flagged():
    # CE connected and returned a row, but the account truly has no spend.
    conn = _connector_with_ce(
        {
            "ResultsByTime": [
                {
                    "TimePeriod": {"Start": START.isoformat(), "End": END.isoformat()},
                    "Total": {},
                    "Groups": [],
                    "Estimated": False,
                }
            ]
        }
    )
    summary = asyncio.run(conn.get_costs(START, END))

    assert summary.total_usd == 0.0
    assert summary._zero_spend_account is True


def test_no_rows_is_not_flagged_zero():
    # An empty result set (e.g. an error path) must not look like a $0 account.
    conn = _connector_with_ce({"ResultsByTime": []})
    summary = asyncio.run(conn.get_costs(START, END))

    assert summary.total_usd == 0.0
    assert getattr(summary, "_zero_spend_account", False) is False
