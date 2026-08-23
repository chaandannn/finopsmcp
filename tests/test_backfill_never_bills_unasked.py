# SPDX-License-Identifier: LicenseRef-Proprietary
"""Backfill reads the free source, and never spends the customer's money unasked.

Two sources cover the same days and they are not equivalent to the person paying.
The CUR is an export already sitting in their bucket. Cost Explorer bills per
request and this path paginates, so a ninety-day backfill is a real charge on
their account. It used to fire automatically, in a daemon thread, seconds after
someone connected, before they had seen a single number.
"""
from __future__ import annotations

import sys
import types

import pytest

from finops.anomaly import backfill


@pytest.fixture(autouse=True)
def needs(monkeypatch):
    monkeypatch.setattr(backfill, "needs_backfill", lambda: True)


def _cur(configured: bool, rows: int = 120):
    m = types.ModuleType("finops.connectors.cur_s3")
    m.is_configured = lambda: configured
    m.ingest_range = lambda s, e: {"rows": rows}
    return m


def test_the_cur_is_used_when_it_exists_and_costs_nothing(monkeypatch):
    monkeypatch.setitem(sys.modules, "finops.connectors.cur_s3", _cur(True))
    monkeypatch.setattr(backfill, "boto3", None, raising=False)
    out = backfill.backfill_from_cost_explorer()
    assert out["source"] == "cur" and out["rows"] == 120


def test_without_a_cur_it_declines_rather_than_billing(monkeypatch):
    """The defect. This used to page through Cost Explorer on its own."""
    monkeypatch.setitem(sys.modules, "finops.connectors.cur_s3", _cur(False))
    called = []
    monkeypatch.setattr(backfill, "_TARGET_DAYS", 90, raising=False)
    import finops.billing_access as ba
    monkeypatch.setattr(ba, "ce_client",
                        lambda **k: called.append(k) or pytest.fail("billed the customer"))
    out = backfill.backfill_from_cost_explorer()
    assert out["skipped"] and out["available"] is True
    assert not called, "Cost Explorer was called without being asked"


def test_asked_explicitly_it_will_use_cost_explorer(monkeypatch):
    """Declining by default must not mean the option is gone."""
    monkeypatch.setitem(sys.modules, "finops.connectors.cur_s3", _cur(False))
    reached = []
    import finops.billing_access as ba
    monkeypatch.setattr(ba, "ce_client",
                        lambda **k: reached.append(k) or (_ for _ in ()).throw(
                            RuntimeError("stop here, the gate is what is under test")))
    backfill.backfill_from_cost_explorer(explicit=True)
    assert reached, "an explicit request still refused to run"


def test_a_broken_cur_does_not_silently_fall_back_to_a_billed_source(monkeypatch):
    """A read that failed is not permission to spend money instead."""
    m = types.ModuleType("finops.connectors.cur_s3")
    m.is_configured = lambda: True
    m.ingest_range = lambda s, e: (_ for _ in ()).throw(OSError("s3 unreachable"))
    monkeypatch.setitem(sys.modules, "finops.connectors.cur_s3", m)
    import finops.billing_access as ba
    monkeypatch.setattr(ba, "ce_client",
                        lambda **k: pytest.fail("billed the customer after a CUR failure"))
    out = backfill.backfill_from_cost_explorer()
    assert out["skipped"] and out["available"] is True


def test_enough_history_still_short_circuits(monkeypatch):
    monkeypatch.setattr(backfill, "needs_backfill", lambda: False)
    assert backfill.backfill_from_cost_explorer()["skipped"] == "history already sufficient"
