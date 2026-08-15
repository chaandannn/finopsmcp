# SPDX-License-Identifier: Apache-2.0
"""The free "what has today cost so far" read, and the four ways it lies.

AWS/Billing EstimatedCharges is the only billing figure AWS gives away, which
makes it the only way to close the CUR's 24-hour lag without putting a meter on
asking. It is also a cumulative, coarse, six-hourly estimate published in one
region, and every one of those properties is a trap:

  1. Cumulative month-to-date, so Average over a window reports roughly half the
     month and looks entirely plausible.
  2. Latest is the NEWEST datapoint, not the LARGEST. Taking the max means a
     downward restatement never lands.
  3. Published in us-east-1 only. Reading the caller's default region is the
     commonest reason a hand-rolled version of this finds nothing.
  4. Absent when billing alerts were never switched on, which must read as a
     missing setting and never as $0.00.

Plus the structural one: this is an ESTIMATE and the CUR is a MEASUREMENT. They
must never end up in the same table, because the moment they do somebody sums
them.
"""
from __future__ import annotations

import ast
import inspect
from datetime import datetime, timedelta, timezone

import pytest

from finops.connectors import aws_billing_metrics as bm


class FakeCloudWatch:
    """Records what it was asked for. The asking is half of what is under test."""

    def __init__(self, *, services=(), total=None, publish=True, pages=1):
        self.services = list(services)
        self.total = total
        self.publish = publish
        self.pages = pages
        self.region = None
        self.queries: list[dict] = []
        self.list_calls = 0

    # boto3 paginator shape
    def get_paginator(self, op):
        assert op == "list_metrics"
        outer = self

        class P:
            def paginate(self, **kw):
                per = max(1, len(outer.services) // outer.pages or 1)
                for i in range(0, len(outer.services), per):
                    outer.list_calls += 1
                    yield {"Metrics": [
                        {"Dimensions": [{"Name": "Currency", "Value": "USD"},
                                        {"Name": "ServiceName", "Value": s}]}
                        for s in outer.services[i:i + per]
                    ]}
        return P()

    def get_metric_data(self, **kw):
        self.queries = kw["MetricDataQueries"]
        if not self.publish:
            return {"MetricDataResults": []}
        now = datetime.now(timezone.utc)
        out = []
        for q in self.queries:
            dims = {d["Name"]: d["Value"]
                    for d in q["MetricStat"]["Metric"]["Dimensions"]}
            svc = dims.get("ServiceName")
            if svc is None:
                values = self.total if self.total is not None else [100.0, 80.0]
            else:
                values = [40.0, 30.0]
            # ScanBy=TimestampDescending: newest first, matching the real API.
            stamps = [now - timedelta(hours=6 * i) for i in range(len(values))]
            out.append({"Id": q["Id"], "Values": list(values), "Timestamps": stamps})
        return {"MetricDataResults": out}


class FakeSession:
    def __init__(self, cw):
        self.cw = cw

    def client(self, name, region_name=None):
        assert name == "cloudwatch"
        self.cw.region = region_name
        return self.cw


# ── absence is not zero ──────────────────────────────────────────────────────

def test_billing_alerts_switched_off_reads_as_unavailable_not_zero():
    """A checkbox nobody ticked must not look like a bill that stopped.

    Same defect shape this repo has now fixed eight times: a failed read
    becoming a number, and always the one that reads as good news.
    """
    cw = FakeCloudWatch(publish=False)
    out = bm.latest_estimated_charges(FakeSession(cw))

    assert out["total_usd"] is None, "reported a number for an account with no metrics"
    assert out["basis"] == "unavailable"
    assert out["error"] == "billing_metrics_unavailable"
    assert any("billing alert" in s.lower() for s in out["setup"]), (
        "did not tell the operator which setting to switch on")


def test_a_cloudwatch_error_is_absence_with_instructions(monkeypatch):
    class Boom(FakeCloudWatch):
        def get_metric_data(self, **kw):
            raise RuntimeError("AccessDenied")

    out = bm.latest_estimated_charges(FakeSession(Boom()))
    assert out["total_usd"] is None and out["basis"] == "unavailable"
    assert "billing alerts" in out["message"].lower()


# ── the cumulative-metric traps ──────────────────────────────────────────────

def test_the_statistic_is_maximum_because_the_metric_is_cumulative():
    """Average over a month-to-date counter reports about half the month.

    It would look completely plausible on a dashboard, which is what makes it
    worth pinning rather than trusting.
    """
    cw = FakeCloudWatch(services=["AmazonEC2"])
    bm.latest_estimated_charges(FakeSession(cw))

    stats = {q["MetricStat"]["Stat"] for q in cw.queries}
    assert stats == {"Maximum"}, (
        f"queried with {stats}; a cumulative metric averaged over a window "
        f"reports roughly half the month's spend")


def test_the_latest_value_is_the_newest_not_the_largest():
    """AWS restates downward when credits land.

    Taking max(values) would pin the figure at its high-water mark and never
    let a correction through, which is a bill that only ever goes up.
    """
    # Newest first: AWS restated 100.0 down to 82.5.
    cw = FakeCloudWatch(total=[82.5, 100.0], services=[])
    out = bm.latest_estimated_charges(FakeSession(cw))

    assert out["total_usd"] == pytest.approx(82.5), (
        f"reported ${out['total_usd']}, the high-water mark, instead of the "
        f"restated $82.50")


def test_it_reads_us_east_1_whatever_region_the_caller_is_in():
    """AWS publishes billing metrics to us-east-1 only.

    Reading the caller's default region is the single commonest reason this
    read comes back empty, and empty here is indistinguishable from no spend.
    """
    cw = FakeCloudWatch(services=["AmazonEC2"])
    bm.latest_estimated_charges(FakeSession(cw))
    assert cw.region == "us-east-1", (
        f"asked region {cw.region!r}; AWS/Billing does not exist outside us-east-1")


# ── shape and honesty ────────────────────────────────────────────────────────

def test_services_are_returned_ranked_and_the_total_is_not_among_them():
    cw = FakeCloudWatch(services=["AmazonEC2", "AmazonRDS", "AWSLambda"])
    out = bm.latest_estimated_charges(FakeSession(cw))

    assert out["total_usd"] is not None
    names = [s["service"] for s in out["by_service"]]
    assert names and "Currency" not in names
    amounts = [s["amount_usd"] for s in out["by_service"]]
    assert amounts == sorted(amounts, reverse=True), "not ranked biggest first"


def test_the_payload_says_it_is_an_estimate_in_the_data_not_just_the_docs():
    """This number travels into dashboards and LLM answers that never read a
    docstring. An estimate that loses its label becomes an actual."""
    cw = FakeCloudWatch(services=["AmazonEC2"])
    out = bm.latest_estimated_charges(FakeSession(cw))

    assert out["basis"] == "estimate"
    assert out["source"] == "cloudwatch_billing_metrics"
    assert out["as_of"] is not None, "no timestamp, so nothing can say how old it is"
    assert out["stale_hours"] is not None
    assert "estimate" in out["note"].lower()


def test_reading_it_is_free_and_says_so_with_a_number():
    """Two API calls, whatever the account size. The claim is checkable."""
    cw = FakeCloudWatch(services=[f"Service{i}" for i in range(40)])
    out = bm.latest_estimated_charges(FakeSession(cw))

    from finops.aws_prices import COST_EXPLORER_PER_REQUEST
    assert out["cost_to_read_usd"] < COST_EXPLORER_PER_REQUEST / 10, (
        f"cost ${out['cost_to_read_usd']}, not meaningfully cheaper than a "
        f"Cost Explorer request")


def test_list_metrics_is_paginated_so_the_tail_of_the_bill_survives():
    """A truncated service list is a smaller bill, reported confidently."""
    cw = FakeCloudWatch(services=[f"Service{i}" for i in range(25)], pages=5)
    out = bm.latest_estimated_charges(FakeSession(cw))

    assert cw.list_calls > 1, "did not paginate"
    assert len(out["by_service"]) == 25, (
        f"only {len(out['by_service'])} of 25 services survived pagination")


def test_the_disable_switch_works():
    cw = FakeCloudWatch(services=["AmazonEC2"])
    import os
    os.environ["NABLE_NO_CLOUDWATCH_BILLING"] = "1"
    try:
        out = bm.latest_estimated_charges(FakeSession(cw))
    finally:
        del os.environ["NABLE_NO_CLOUDWATCH_BILLING"]
    assert out["basis"] == "unavailable"
    assert cw.queries == [], "made an API call despite being switched off"


# ── the structural guard ─────────────────────────────────────────────────────

def test_the_estimate_never_writes_to_the_measured_history():
    """cost_snapshots holds what the bill WAS. This module holds what AWS
    currently THINKS it will be.

    Those are different kinds of fact, and the moment they share a table
    somebody sums them: a month would carry both the CUR's measured total and
    CloudWatch's running estimate of the same spend. Checked structurally
    because a comment saying "do not do this" is not a mechanism.
    """
    tree = ast.parse(inspect.getsource(bm))
    writers = {"store_snapshot", "replace_provider_day", "insert", "execute"}
    called = {
        getattr(n.func, "id", None) or getattr(n.func, "attr", None)
        for n in ast.walk(tree) if isinstance(n, ast.Call)
    }
    leaked = writers & called
    assert not leaked, (
        f"the estimate path calls {sorted(leaked)}, so a provisional figure can "
        f"reach the measured history and be summed with it")
