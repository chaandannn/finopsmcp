"""What nable is allowed to say when a read fails.

Why this file exists, stated plainly: every dollar figure nable prints comes
from a call that can fail. CloudWatch throttles on a wide scan. An IAM policy
grants ce:GetCostAndUsage but not the Savings Plans read actions. An SSO token
expires mid-query. The local SQLite file cannot be opened. In five places that
failure is caught and turned into a zero, and a zero is not a missing value
here: it is the input to "idle", to "0% coverage", to "costs decreased 92%", to
"your week was $0". The tool then states it with no marker and full confidence.
Silence would be safe. A confident wrong number is what gets a live NAT gateway
deleted, a twelve month Savings Plan bought, and a $0 week posted to a team
channel.

The seam is the provider boundary and the storage boundary, never the function
under test. The fakes below are boto3 shaped clients raising the errors AWS
actually raises, a connector that fails the way an expired token fails, a real
SQLite file that genuinely cannot be opened, and Slack faked at the HTTP POST.
Everything between those boundaries is nable's real code: the detector loops,
the trust envelope classification, the recommendation builder, the period diff,
the Block Kit renderer that produces the literal sentence a customer reads.

Each test says in its docstring whether it fails today because the bug is real,
or passes today as a pin on behaviour that must not regress. The pins are here
on purpose. Three of these detectors have a correct sibling a few hundred lines
away in the same file, and the pin is what turns that asymmetry into a test
failure instead of a code reading exercise, and what stops a fix from being
"make the detector never fire".
"""
from __future__ import annotations

import asyncio
import json
import re
from datetime import date, datetime, timedelta, timezone
from typing import Any

import pytest
from botocore.exceptions import ClientError

from finops.analyzers import waste, waste_evidence


# ── boto3 shaped fakes ───────────────────────────────────────────────────────

def _client_error(code: str, op: str, msg: str = "") -> ClientError:
    """The exception botocore raises, with the response envelope intact, so the
    code under test sees exactly what it would see against a real account."""
    return ClientError({"Error": {"Code": code, "Message": msg or code}}, op)


_THROTTLED = "ThrottlingException"


class _Paginator:
    def __init__(self, pages: list[dict]):
        self._pages = pages

    def paginate(self, **_kw):
        return list(self._pages)


class _PagedClient:
    """A describe_* source. One page set, any operation name."""

    def __init__(self, pages: list[dict]):
        self._pages = pages

    def get_paginator(self, _name: str) -> _Paginator:
        return _Paginator(self._pages)


class _CloudWatch:
    """get_metric_statistics driven by one rule per metric name.

    A rule is either the datapoint list to return or the exception to raise,
    which is the only two things CloudWatch does.
    """

    def __init__(self, rules: dict[str, Any]):
        self._rules = rules
        self.asked: list[str] = []

    def get_metric_statistics(self, **kw):
        metric = kw["MetricName"]
        self.asked.append(metric)
        rule = self._rules[metric]
        if isinstance(rule, Exception):
            raise rule
        return {"Datapoints": rule}


_NAT_PAGES = [{
    "NatGateways": [{
        "NatGatewayId": "nat-0prodegress",
        "VpcId": "vpc-prod",
        "SubnetId": "subnet-prod-1a",
        "Tags": [{"Key": "Name", "Value": "prod-egress-1a"}],
    }]
}]


def _ec2_pages(instance_type: str = "m5.4xlarge") -> list[dict]:
    """One long lived running instance, past the lookback window so the
    "too new to judge" skip does not fire."""
    return [{
        "Reservations": [{
            "Instances": [{
                "InstanceId": "i-0kafkabroker",
                "InstanceType": instance_type,
                "LaunchTime": datetime.now(timezone.utc) - timedelta(days=120),
                "State": {"Name": "running"},
                "Tags": [{"Key": "Name", "Value": "kafka-broker-1"}],
            }]
        }]
    }]


# ═════════════════════════════════════════════════════════════════════════════
# 1. A CloudWatch read that threw is not a measurement of zero
#    finops/analyzers/waste.py:322 (NAT) and :1052 (EC2 network guard)
# ═════════════════════════════════════════════════════════════════════════════

def test_a_throttled_nat_metric_read_is_not_evidence_the_gateway_is_idle():
    """FAILS TODAY: the bug is real.

    check_nat_gateways catches the metric exception, sets datapoints = [], and
    the next branch turns an empty list into avg_bytes_per_day = 0.0. A wide
    scan asks CloudWatch about hundreds of resources per region, so throttling
    is routine, and when it hits, every NAT gateway in the region is emitted as
    idle_nat_gateway with the sentence "averaged 0.000 GB/day over 7 days" and a
    precise $32.85/mo saving. The gateway carrying all production egress reads
    exactly like a forgotten one. The proposed action is deleting it.
    """
    cw = _CloudWatch({
        "BytesOutToDestination": _client_error(
            _THROTTLED, "GetMetricStatistics", "Rate exceeded"),
    })

    findings = waste.check_nat_gateways(
        _PagedClient(_NAT_PAGES), cw, region="us-east-1")

    assert cw.asked == ["BytesOutToDestination"], (
        "test wiring: the detector never reached the metric call")

    # A fix may either skip the resource or emit it with an explicit
    # metrics-unavailable marker. What it may not do is claim idle.
    claimed_idle = [
        f for f in findings
        if f.get("waste_type") == "idle_nat_gateway"
        and not f.get("metrics_unavailable")
    ]
    assert not claimed_idle, (
        "a NAT gateway whose CloudWatch read THREW was reported as idle: "
        f"{claimed_idle[0]['detail']!r}. Skip it the way check_rds_idle and "
        "check_idle_load_balancers do, or flag it metrics_unavailable. An "
        "unread metric is not a reading of zero."
    )


def test_an_unread_nat_gateway_never_reaches_the_measured_savings_headline():
    """FAILS TODAY: the bug is real.

    waste_evidence classifies idle_nat_gateway as MEASURED at high confidence,
    the top of the trust envelope, on the stated grounds that it is a real
    metric read straight off the resource. When the read threw there was no
    metric, so split_totals banks a number nable never measured into
    measured_monthly_savings, which is the half of the headline the product
    presents as a commitment rather than as a work queue.
    """
    cw = _CloudWatch({
        "BytesOutToDestination": _client_error(
            _THROTTLED, "GetMetricStatistics", "Rate exceeded"),
    })
    findings = waste.check_nat_gateways(
        _PagedClient(_NAT_PAGES), cw, region="us-east-1")

    totals = waste_evidence.split_totals(waste_evidence.annotate(findings))

    assert totals["measured_monthly_savings"] == 0.0, (
        "nothing was measured, the metric call raised, yet "
        f"${totals['measured_monthly_savings']} landed in the MEASURED bucket "
        "that the audit headline presents as savings nable can stand behind"
    )


def test_a_nat_gateway_that_really_reads_near_zero_is_still_flagged():
    """PASSES TODAY: pin, and the control for the two tests above.

    If a fix makes the detector skip on a failed read, it must not also stop
    flagging the genuinely idle gateway the detector exists to find. This goes
    red if the fix over-corrects into "never emit".
    """
    cw = _CloudWatch({"BytesOutToDestination": [{"Sum": 1024.0}] * 7})
    findings = waste.check_nat_gateways(
        _PagedClient(_NAT_PAGES), cw, region="us-east-1")

    assert [f["waste_type"] for f in findings] == ["idle_nat_gateway"]
    assert findings[0]["estimated_monthly_savings"] == 32.85


def test_a_busy_nat_gateway_is_not_flagged():
    """PASSES TODAY: pin. 40 GB/day of measured egress is not idle, and a fix
    to the error branch must not disturb the measured branch."""
    forty_gb = 40 * 1024 ** 3
    cw = _CloudWatch({"BytesOutToDestination": [{"Sum": forty_gb}] * 7})
    assert waste.check_nat_gateways(
        _PagedClient(_NAT_PAGES), cw, region="us-east-1") == []


def test_the_sibling_detectors_skip_a_resource_whose_metric_read_failed():
    """PASSES TODAY: pin on the direction the NAT detector should have taken.

    check_rds_idle and check_idle_load_balancers both `continue` when
    get_metric_statistics raises. Pinning that keeps the asymmetry with
    check_nat_gateways a test result rather than something a reader has to
    notice, and stops anyone harmonising the three by copying the wrong one. If
    either of these starts emitting on a failed read, an unread database and a
    live load balancer become delete candidates on no evidence at all.
    """
    rds_pages = [{
        "DBInstances": [{
            "DBInstanceIdentifier": "prod-orders",
            "DBInstanceClass": "db.m5.4xlarge",
            "Engine": "postgres",
            "DBInstanceStatus": "available",
        }]
    }]
    rds_cw = _CloudWatch({
        "DatabaseConnections": _client_error(
            _THROTTLED, "GetMetricStatistics", "Rate exceeded"),
    })
    assert waste.check_rds_idle(
        _PagedClient(rds_pages), rds_cw, region="us-east-1") == [], (
        "an RDS instance whose connection metric could not be read was called idle")

    elb_pages = [{
        "LoadBalancers": [{
            "LoadBalancerName": "prod-api",
            "LoadBalancerArn":
                "arn:aws:elasticloadbalancing:us-east-1:1:loadbalancer/app/prod-api/abc",
            "Type": "application",
            "State": {"Code": "active"},
        }]
    }]
    lb_cw = _CloudWatch({
        "RequestCount": _client_error(
            _THROTTLED, "GetMetricStatistics", "Rate exceeded"),
    })
    assert waste.check_idle_load_balancers(
        _PagedClient(elb_pages),
        _PagedClient([{"LoadBalancerDescriptions": []}]),
        lb_cw, region="us-east-1") == [], (
        "a load balancer whose request metric could not be read was called idle")


def test_a_failed_network_read_does_not_disable_the_ec2_false_positive_guard():
    """FAILS TODAY: the bug is real.

    check_idle_ec2 reads CPU, then reads NetworkOut specifically so a network
    bound or disk bound box is not called idle on low CPU alone. When the
    NetworkOut call raises, the handler sets avg_net_per_hr = 0.0, which is
    below the threshold, so the guard's `continue` never fires: the guard
    disables itself in exactly the case it exists for. The CPU fetch two dozen
    lines earlier gets this right and skips the instance, and
    recommendations/rightsizing.py sets an unknown metric to 100.0 under the
    comment "Unknown utilization must never be recommended for downsizing." One
    throttled second call turns a healthy Kafka broker into "Consider stopping,
    downsizing, or terminating".
    """
    cw = _CloudWatch({
        "CPUUtilization": [{"Average": 2.0}] * 300,
        "NetworkOut": _client_error(
            _THROTTLED, "GetMetricStatistics", "Rate exceeded"),
    })

    findings = waste.check_idle_ec2(
        _PagedClient(_ec2_pages()), cw, region="us-east-1")

    assert set(cw.asked) == {"CPUUtilization", "NetworkOut"}, (
        "test wiring: the detector never reached the network guard")
    assert findings == [], (
        "an instance was called idle_ec2_low_cpu after its NetworkOut read "
        f"THREW: {findings[0]['detail']!r} at "
        f"${findings[0]['estimated_monthly_savings']}/mo. Unknown traffic must "
        "fail towards 'in use', not towards 'idle'."
    )


def test_a_low_cpu_instance_with_real_traffic_is_not_called_idle():
    """PASSES TODAY: pin. The guard does work when the metric actually reads,
    which is what makes the test above a failure of the error branch alone."""
    busy = 500 * 1024 ** 2  # 500 MB/hr, well over the 100 MB/hr threshold
    cw = _CloudWatch({
        "CPUUtilization": [{"Average": 2.0}] * 300,
        "NetworkOut": [{"Sum": busy}] * 300,
    })
    assert waste.check_idle_ec2(
        _PagedClient(_ec2_pages()), cw, region="us-east-1") == []


def test_a_genuinely_quiet_instance_is_still_flagged():
    """PASSES TODAY: pin, and the control for the guard fix. Low CPU plus a
    measured trickle of traffic must still surface, or the detector is dead."""
    cw = _CloudWatch({
        "CPUUtilization": [{"Average": 0.4}] * 300,
        "NetworkOut": [{"Sum": 1024.0}] * 300,
    })
    findings = waste.check_idle_ec2(
        _PagedClient(_ec2_pages()), cw, region="us-east-1")
    assert [f["waste_type"] for f in findings] == ["idle_ec2_low_cpu"]


# ═════════════════════════════════════════════════════════════════════════════
# 2. A denied Cost Explorer permission is not a coverage measurement
#    finops/recommendations/commitments.py:205
# ═════════════════════════════════════════════════════════════════════════════

@pytest.fixture
def _fresh_log_once(monkeypatch):
    """_logutil.log_once dedupes on a process global set. Give each test its own
    copy, so a warning already suppressed by an earlier test is not mistaken for
    a code path that never ran, and so this file leaves the global as it found
    it."""
    from finops import _logutil
    monkeypatch.setattr(_logutil, "_seen", set())


class _CostExplorer:
    """A healthy account: 99% SP utilization, 72% RI coverage and a steady
    ~$9k/mo of uncovered on demand. Only the operations named in `denied` fail,
    which is what a policy granting ce:GetCostAndUsage but not the Savings Plans
    read actions looks like from inside the process.
    """

    def __init__(self, denied: tuple[str, ...] = (), sp_coverage_pct: str = "81.0"):
        self._denied = denied
        self._sp_coverage_pct = sp_coverage_pct

    def _guard(self, op: str) -> None:
        if op in self._denied:
            raise _client_error(
                "AccessDeniedException", op,
                f"User is not authorized to perform: ce:{op}")

    def get_savings_plans_coverage(self, **_kw):
        self._guard("GetSavingsPlansCoverage")
        return {"Total": {"CoverageHours": {
            "CoverageHoursPercentage": self._sp_coverage_pct}}}

    def get_savings_plans_utilization(self, **_kw):
        self._guard("GetSavingsPlansUtilization")
        return {"Total": {
            "Utilization": {"UtilizationPercentage": "99.4",
                            "TotalCommitment": "60000"},
            "Savings": {"NetSavings": "0"},
        }}

    def get_reservation_utilization(self, **_kw):
        return {"Total": {
            "Utilization": {"UtilizationPercentage": "98.0"},
            "UnusedHours": "0",
            "UnusedAmortizedUpfrontCostForRIs": "0",
            "UnusedRecurringFeeForRIs": "0",
        }}

    def get_reservation_coverage(self, **_kw):
        return {"Total": {"CoverageHours": {"CoverageHoursPercentage": "72.0"}}}

    def get_cost_and_usage(self, **_kw):
        return {"ResultsByTime": [
            {"Total": {"UnblendedCost": {"Amount": "9000.00"}}},
            {"Total": {"UnblendedCost": {"Amount": "9500.00"}}},
            {"Total": {"UnblendedCost": {"Amount": "9200.00"}}},
        ]}


def _run_commitments(monkeypatch, ce: _CostExplorer):
    """Drive the real analyze_commitments with boto3.client faked at the SDK
    boundary. Nothing inside finops is replaced, and no billed call is made."""
    import boto3
    from finops.recommendations import commitments
    monkeypatch.setattr(boto3, "client", lambda *_a, **_k: ce)
    return commitments.analyze_commitments()


def test_a_denied_coverage_call_is_not_reported_as_zero_percent_coverage(
        monkeypatch, _fresh_log_once):
    """FAILS TODAY: the bug is real.

    _savings_plan_coverage catches the exception and returns 0.0. No caller can
    tell that apart from a measured 0%, so every downstream surface prints it as
    fact. _logutil.note_sp_error has already classified this precisely as
    AccessDenied and knows the exact IAM action to add, and the classification
    is thrown away one line later. A customer sitting at 81% coverage is told
    they are at 0%.
    """
    analysis = _run_commitments(
        monkeypatch, _CostExplorer(denied=("GetSavingsPlansCoverage",)))

    assert analysis is not None, "test wiring: the analysis should not be None here"
    assert analysis.savings_plan_coverage_pct != 0.0, (
        "a Savings Plans coverage call that was DENIED came back as a measured "
        "0.0%. Unknown needs to be its own value, None plus an explicit "
        "'coverage unavailable, missing ce:GetSavingsPlansCoverage' field, not "
        "a number that reads as a finding."
    )


def test_a_denied_coverage_call_never_recommends_buying_a_savings_plan(
        monkeypatch, _fresh_log_once):
    """FAILS TODAY: the bug is real.

    This is the money consequence of the test above. _uncovered_on_demand still
    succeeds, so _build_recommendations sees sp_coverage 0.0 against a $9k/mo
    baseline, clears `if sp_coverage < 60 and baseline > 500`, and emits a one
    year Compute Savings Plan purchase at high confidence carrying the sentence
    "Your SP coverage is 0%." A customer already at 81% coverage is told to sign
    a twelve month commitment because one IAM action is missing.
    """
    analysis = _run_commitments(
        monkeypatch, _CostExplorer(denied=("GetSavingsPlansCoverage",)))

    purchases = [r for r in analysis.recommendations if r["type"] == "savings_plan"]
    assert not purchases, (
        "a commitment PURCHASE was recommended off a coverage figure that was "
        f"never read: {purchases[0]['description']!r}. Every commitment "
        "recommendation must be suppressed while coverage is unknown."
    )


def test_a_real_low_coverage_account_still_gets_the_purchase_recommendation(
        monkeypatch, _fresh_log_once):
    """PASSES TODAY: pin, and the control for the two tests above.

    Same fixture, nothing denied, coverage genuinely reads 12%. The
    recommendation must survive a fix, or the fix has deleted the commitment
    engine instead of teaching it what unknown means.
    """
    analysis = _run_commitments(monkeypatch, _CostExplorer(sp_coverage_pct="12.0"))

    assert analysis.savings_plan_coverage_pct == 12.0
    purchases = [r for r in analysis.recommendations if r["type"] == "savings_plan"]
    assert len(purchases) == 1
    assert purchases[0]["monthly_savings"] > 0


# ═════════════════════════════════════════════════════════════════════════════
# 3. A provider that failed is not a cost decrease
#    finops/tools/cost_queries.py:2121
# ═════════════════════════════════════════════════════════════════════════════

class _Provider:
    """A connector at the provider boundary. Any window ending on or after
    `fails_from` raises the way an expired SSO token raises. Earlier windows,
    which is the prior period comparison, still answer, exactly as they do in
    production where the 12h cost cache serves them outright."""

    def __init__(self, name: str, total: float, by_service: dict[str, float],
                 fails_from: date | None = None):
        self.provider = name
        self._total = total
        self._by_service = by_service
        self._fails_from = fails_from

    async def is_configured(self) -> bool:
        return True

    async def get_costs(self, start: date, end: date, granularity: str = "MONTHLY"):
        if self._fails_from is not None and end >= self._fails_from:
            raise RuntimeError(
                "ExpiredTokenException: The security token included in the "
                "request is expired")
        from finops.connectors.base import CostSummary
        return CostSummary(
            provider=self.provider, start_date=start, end_date=end,
            total_usd=self._total, by_service=dict(self._by_service),
            by_account={}, by_region={}, entries=[],
        )


@pytest.fixture
def _two_providers_one_expired(monkeypatch):
    """A large AWS account whose token expired today, plus a healthy Datadog.

    The cost cache is switched off and the _active() cache cleared so nothing
    from another test, or from the developer's own machine, leaks in. Every
    registry mutated here is restored on teardown.
    """
    import finops.server as srv
    from finops import cache as cache_mod
    from finops import demo_data

    monkeypatch.setattr(demo_data, "DEMO_MODE", False)
    monkeypatch.delenv("FINOPS_DEMO_FORCE", raising=False)
    monkeypatch.setattr(cache_mod, "_DISABLED", True)

    pool = {
        "aws": _Provider("aws", 451_000.0,
                         {"Amazon EC2": 300_000.0, "Amazon S3": 151_000.0},
                         fails_from=date.today()),
        "datadog": _Provider("datadog", 39_000.0, {"Datadog": 39_000.0}),
    }
    saved = (dict(srv._ALL_CONNECTORS), dict(srv.CLOUD_CONNECTORS),
             dict(srv.SAAS_CONNECTORS))
    srv._ALL_CONNECTORS.clear()
    srv._ALL_CONNECTORS.update(pool)
    srv.CLOUD_CONNECTORS.clear()
    srv.CLOUD_CONNECTORS["aws"] = pool["aws"]
    srv.SAAS_CONNECTORS.clear()
    srv.SAAS_CONNECTORS["datadog"] = pool["datadog"]
    srv._ACTIVE_CACHE.clear()
    try:
        yield pool
    finally:
        srv._ACTIVE_CACHE.clear()
        for live, original in zip(
            (srv._ALL_CONNECTORS, srv.CLOUD_CONNECTORS, srv.SAAS_CONNECTORS), saved
        ):
            live.clear()
            live.update(original)


def test_explain_recent_cost_drivers_refuses_to_call_a_failed_provider_a_saving(
        _two_providers_one_expired):
    """FAILS TODAY: the bug is real.

    _gather_costs records the failure in by_provider["aws"]["error"], and this
    tool unpacks `_, _, cost_now = await _gather_costs(...)`, discarding exactly
    that. The current window loses AWS entirely while the prior window still has
    it, so the diff reads as a $451,000 collapse and the tool returns the
    sentence "Costs decreased by $451,000 (-92.0%) vs the prior 30-day period"
    with every AWS service listed under top_decreases. The user is told their
    bill vanished on the day their credentials expired.
    """
    from finops.tools import cost_queries

    out = asyncio.run(cost_queries.explain_recent_cost_drivers(days=30))

    flagged = (
        out.get("partial")
        or out.get("failed_providers")
        or out.get("error")
        or "provider" in str(out.get("note", "")).lower()
    )
    assert flagged, (
        "a provider that ERRORED in the current window was silently counted as "
        f"$0 of spend. The tool returned: {out.get('summary')!r} with "
        f"top_decreases={[d['key'] for d in out.get('top_decreases', [])]}. "
        "get_cost_summary refuses on this same data. This tool must not "
        "manufacture a drop out of a failed read."
    )


def test_get_cost_summary_still_marks_the_same_failure_partial(
        _two_providers_one_expired):
    """PASSES TODAY: pin on the honest neighbour.

    Same connectors, same failure, one tool over. get_cost_summary sets partial,
    lists failed_providers and warns that real spend is higher. This pin is what
    makes the test above a comparison rather than an opinion, and it stops a
    future refactor removing the good half along with the bad.
    """
    from finops.tools import cost_queries

    out = asyncio.run(cost_queries.get_cost_summary())

    assert out.get("partial") is True, out
    assert "aws" in out.get("failed_providers", {}), out
    assert "higher" in out.get("partial_warning", "")


# ═════════════════════════════════════════════════════════════════════════════
# 4. A snapshot read that failed is not a $0 week, and a half covered day is
#    not a 94% drop
#    finops/tools/notifications.py:305, finops/scheduler/jobs.py:842 and :283
# ═════════════════════════════════════════════════════════════════════════════

@pytest.fixture
def _slack_wire(monkeypatch):
    """Fake Slack at the HTTP POST and nowhere else.

    send_weekly_insight, send_daily_digest, the Block Kit builders and the
    summary line all run for real, so what this captures is the literal payload
    that would land in the customer's channel.
    """
    import httpx

    posted: list[dict] = []

    class _OK:
        status_code = 200

        @staticmethod
        def json():
            return {"ok": True}

    async def _post(_self, url, **kw):
        posted.append({"url": url, "json": kw.get("json") or {}})
        return _OK()

    monkeypatch.setattr(httpx.AsyncClient, "post", _post)
    monkeypatch.setenv("SLACK_WEBHOOK_URL", "https://hooks.slack.invalid/T0/B0/x")
    monkeypatch.delenv("SLACK_BOT_TOKEN", raising=False)
    monkeypatch.delenv("TEAMS_WEBHOOK_URL", raising=False)
    return posted


@pytest.fixture
def _sqlite_at(monkeypatch):
    """Point the storage layer at a caller chosen SQLite path.

    Returns a setter so one test can pick a working file and another an
    unopenable one. The engine singleton and both env vars are restored on
    teardown, so the developer's real ~/.finops/finops.db is never touched.
    """
    from finops.storage import db as db_mod

    def _use(path) -> None:
        monkeypatch.setattr(db_mod, "_ENGINE", None)
        monkeypatch.setenv("FINOPS_DB_PATH", str(path))
        monkeypatch.delenv("DATABASE_URL", raising=False)

    return _use


@pytest.fixture
def _pro(monkeypatch):
    """A valid Pro licence, so the real require_pro gate passes and the test is
    exercising the digest rather than the paywall. The gate itself stays real."""
    from finops.license import LicenseStatus
    monkeypatch.setattr(
        "finops.license.get_status",
        lambda: LicenseStatus(mode="pro", email="dev@acme.com",
                              issued="2026-07-01", message=""),
    )


def _seed(engine, rows: list[tuple[str, str, str, float]]) -> None:
    """Insert cost snapshots as (provider, service, YYYY-MM-DD, amount)."""
    from finops.storage.db import cost_snapshots
    with engine.begin() as conn:
        conn.execute(cost_snapshots.insert(), [
            {"provider": p, "service": s, "account_id": "111122223333",
             "region": "us-east-1", "snapshot_date": d, "amount_usd": amt,
             "granularity": "DAILY", "captured_at": datetime.now(timezone.utc)}
            for p, s, d, amt in rows
        ])


def _wire_text(posted: list[dict]) -> str:
    return posted[0]["json"].get("text", "") if posted else ""


def test_a_failed_snapshot_read_is_not_posted_to_slack_as_a_zero_dollar_week(
        tmp_path, _sqlite_at, _slack_wire, _pro):
    """FAILS TODAY: the bug is real.

    push_weekly_insight wraps the whole snapshot query in
    `except Exception as e: grand_total = 0.0; prev_total = 0.0; movers = []`.
    The bound `e` is never logged, execution runs straight on into
    slack.send_weekly_insight, and the real renderer produces "Weekly cost: $0
    (+0.0% vs last week)" in the team channel while the tool returns
    {"sent": True, "grand_total_usd": 0.0} as success. The failure injected here
    is an ordinary one: the SQLite file cannot be opened.
    """
    from finops.tools import notifications as notif

    unopenable = tmp_path / "finops.db"
    unopenable.mkdir()          # a directory where SQLite expects a file
    _sqlite_at(unopenable)

    out = asyncio.run(notif.push_weekly_insight())

    assert "Weekly cost: $0 " not in _wire_text(_slack_wire), (
        "a fabricated $0 week was posted to Slack after the snapshot query "
        f"FAILED: {_wire_text(_slack_wire)!r}. The tool reported {out!r} as "
        "success. Refuse, or say the numbers are unavailable, but do not print "
        "a total nable never read."
    )
    assert out.get("sent") is False or "error" in out, (
        f"the weekly digest reported success on a query that failed: {out!r}")


def test_the_cron_copy_of_the_weekly_digest_has_the_same_refusal(
        tmp_path, _sqlite_at, _slack_wire):
    """FAILS TODAY: the bug is real, and it is duplicated.

    scheduler/jobs.py:run_weekly_insight_now carries its own copy of the same
    swallow: `except Exception: grand_total, prev_total, this_week, last_week =
    0.0, 0.0, {}, {}`. This is the path APScheduler runs unattended, so the
    fabricated $0 posts to the channel every week for as long as the fault
    lasts, with nobody in the loop to notice. Fixing only the MCP tool leaves
    the scheduled surface, the one nobody is watching, still lying.
    """
    from finops.scheduler import jobs

    unopenable = tmp_path / "finops.db"
    unopenable.mkdir()
    _sqlite_at(unopenable)

    sent = asyncio.run(jobs.run_weekly_insight_now())

    assert "Weekly cost: $0 " not in _wire_text(_slack_wire), (
        "the scheduled weekly insight posted a fabricated $0 week after the "
        f"snapshot query FAILED: {_wire_text(_slack_wire)!r} (returned {sent!r})"
    )


def test_a_working_snapshot_read_still_posts_the_real_number(
        tmp_path, _sqlite_at, _slack_wire, _pro):
    """PASSES TODAY: pin, and the control for the two tests above.

    Proves the failure injected above is a genuine storage failure and not a
    mis-wired test: with a working DB holding real rows, the same call reports
    $7,000 and posts it. Goes red if a fix makes the digest refuse
    unconditionally, which would be a different way of shipping nothing.
    """
    from finops.storage.db import get_engine
    from finops.tools import notifications as notif

    _sqlite_at(tmp_path / "finops.db")
    today = date.today()
    _seed(get_engine(), [
        ("aws", "Amazon EC2", (today - timedelta(days=d)).isoformat(), 1000.0)
        for d in range(7)
    ])

    out = asyncio.run(notif.push_weekly_insight())

    assert out.get("sent") is True, out
    assert out["grand_total_usd"] == pytest.approx(7000.0), out
    assert "$7,000" in _wire_text(_slack_wire), _wire_text(_slack_wire)


def _vs_yesterday_pct(posted: list[dict]) -> float | None:
    """Pull the percentage out of the real Block Kit 'vs yesterday' field."""
    for block in posted[0]["json"].get("blocks", []):
        for field in block.get("fields", []) or []:
            text = field.get("text", "")
            if "vs yesterday" in text:
                m = re.search(r"(-?\d+(?:\.\d+)?)%", text)
                return float(m.group(1)) if m else None
    return None


def test_the_daily_digest_does_not_report_a_drop_a_missing_provider_caused(
        tmp_path, _sqlite_at, _slack_wire):
    """FAILS TODAY: the bug is real.

    _snapshot_all records `results[name] = f"error: {exc}"` for a provider whose
    fetch failed, and job_snapshot throws that dict away. _send_daily_digest
    then sums whatever rows exist for yesterday against a complete two days ago
    total, with nothing anywhere comparing the provider sets. Here AWS is
    present two days ago at $90,000 and absent yesterday because its snapshot
    failed, while Datadog is flat at $5,600 across both days. The real renderer
    produces "vs yesterday: -94.1% (-$90,000.00)". Nothing dropped. The same
    missing rows also mean _detect_and_alert never looks at AWS, so a genuine
    spike during the outage raises nothing either.
    """
    from finops.scheduler import jobs
    from finops.storage.db import get_engine

    _sqlite_at(tmp_path / "finops.db")
    today = date.today()
    d1 = (today - timedelta(days=1)).isoformat()
    d2 = (today - timedelta(days=2)).isoformat()
    _seed(get_engine(), [
        ("aws", "Amazon EC2", d2, 90_000.0),
        ("datadog", "APM", d2, 5_600.0),
        # yesterday: the AWS snapshot failed, so only Datadog landed
        ("datadog", "APM", d1, 5_600.0),
    ])

    asyncio.run(jobs._send_daily_digest())

    assert _slack_wire, "test wiring: nothing was posted to Slack"
    pct = _vs_yesterday_pct(_slack_wire)
    blob = json.dumps(_slack_wire[0]["json"]).lower()
    caveat = any(w in blob for w in
                 ("incomplete", "partial", "missing", "coverage", "not comparable"))

    assert pct is None or pct > -50.0 or caveat, (
        f"the digest reported a {pct}% drop that nothing spent. The only "
        "provider present in BOTH windows was flat; the fall is entirely the "
        "AWS snapshot that never landed. Record per-provider snapshot status "
        "and either refuse the delta or annotate it when the two windows do "
        "not cover the same providers."
    )


def test_the_daily_digest_still_reports_a_real_drop(
        tmp_path, _sqlite_at, _slack_wire):
    """PASSES TODAY: pin, and the control for the test above.

    Both windows cover both providers, and AWS genuinely fell from $90,000 to
    $9,000. That is a real 88% drop and the digest must keep saying so. Goes red
    if a fix suppresses every delta instead of only the incomparable ones.
    """
    from finops.scheduler import jobs
    from finops.storage.db import get_engine

    _sqlite_at(tmp_path / "finops.db")
    today = date.today()
    d1 = (today - timedelta(days=1)).isoformat()
    d2 = (today - timedelta(days=2)).isoformat()
    _seed(get_engine(), [
        ("aws", "Amazon EC2", d2, 90_000.0),
        ("datadog", "APM", d2, 5_600.0),
        ("aws", "Amazon EC2", d1, 9_000.0),
        ("datadog", "APM", d1, 5_600.0),
    ])

    asyncio.run(jobs._send_daily_digest())

    assert _slack_wire, "test wiring: nothing was posted to Slack"
    pct = _vs_yesterday_pct(_slack_wire)
    assert pct is not None and pct < -80.0, (
        f"a real 88% drop was not reported: vs-yesterday read {pct}")


# ── both halves of the digest fix, pinned separately ─────────────────────────

def test_the_digest_delta_is_computed_like_for_like(
        tmp_path, _sqlite_at, _slack_wire):
    """The NUMBER, not just the caveat.

    The audit test above accepts "either refuse the delta or annotate it", which
    is the right contract to hold a fix to but leaves neither half pinned once a
    fix does both. Mutation testing showed exactly that: reverting the
    like-for-like basis still passed, because the caveat alone satisfied the OR.

    A caveat is not a substitute for a correct number. A reader who skims sees
    "-94.1%" and stops; the sentence explaining it is the thing they skip.
    Datadog is flat across both days, so the change must be 0%.
    """
    from finops.scheduler import jobs
    from finops.storage.db import get_engine

    _sqlite_at(tmp_path / "finops.db")
    today = date.today()
    d1 = (today - timedelta(days=1)).isoformat()
    d2 = (today - timedelta(days=2)).isoformat()
    _seed(get_engine(), [
        ("aws", "Amazon EC2", d2, 90_000.0),
        ("datadog", "APM", d2, 5_600.0),
        ("datadog", "APM", d1, 5_600.0),      # AWS snapshot failed; Datadog flat
    ])

    asyncio.run(jobs._send_daily_digest())
    pct = _vs_yesterday_pct(_slack_wire)

    assert pct is not None and abs(pct) < 0.5, (
        f"the digest reported {pct}%. The only provider present in BOTH days "
        f"was flat, so the like-for-like change is 0%. Anything else is the "
        f"missing AWS snapshot wearing a spend label"
    )


def test_the_digest_names_the_provider_that_went_missing(
        tmp_path, _sqlite_at, _slack_wire):
    """The CAVEAT, not just the number.

    The other half of the same OR, and the half that tells the reader why
    yesterday's headline total is smaller than usual. Correcting the percentage
    without saying anything would leave a total that silently excludes AWS and
    a reader with no way to know.
    """
    from finops.scheduler import jobs
    from finops.storage.db import get_engine

    _sqlite_at(tmp_path / "finops.db")
    today = date.today()
    d1 = (today - timedelta(days=1)).isoformat()
    d2 = (today - timedelta(days=2)).isoformat()
    _seed(get_engine(), [
        ("aws", "Amazon EC2", d2, 90_000.0),
        ("datadog", "APM", d2, 5_600.0),
        ("datadog", "APM", d1, 5_600.0),
    ])

    asyncio.run(jobs._send_daily_digest())
    blob = json.dumps(_slack_wire[0]["json"])

    assert "AWS" in blob, "the digest never names the provider that is missing"
    assert any(w in blob.lower() for w in ("incomplete", "no snapshot")), (
        f"nothing in the payload says the comparison is incomplete: {blob[:400]}"
    )


def test_explain_recent_cost_drivers_marks_the_result_partial(
        _two_providers_one_expired):
    """`partial` specifically, not any-of-four.

    The audit test accepts partial OR failed_providers OR error OR a note
    mentioning a provider. Reverting `partial` alone still passed it. That key
    is the one get_cost_summary sets and the one a caller checks, so the two
    tools have to agree on it rather than each satisfying the OR differently.
    """
    from finops.tools import cost_queries

    out = asyncio.run(cost_queries.explain_recent_cost_drivers(days=30))

    assert out.get("partial") is True, out
    assert "aws" in (out.get("failed_providers") or {}), out
    assert out["summary"].startswith("PARTIAL:"), (
        f"the caveat is not in the summary, which is the sentence a model "
        f"quotes on its own: {out['summary']!r}"
    )


def test_a_newly_connected_provider_is_not_a_spend_spike(
        tmp_path, _sqlite_at, _slack_wire):
    """The mirror case, and the one delta_basis actually exists for.

    Mutation testing found the gap: reverting slack's like-for-like basis left
    every other test green, because in the vanishing-provider direction the
    caller has already narrowed prev_total and yesterday's total is naturally
    the common-provider total, so the two agree by accident.

    The other direction does not agree by accident. Connect GCP today and
    yesterday holds AWS+GCP while the day before holds AWS alone. Without the
    narrowed basis the digest reads GCP's entire first-day spend as a spend
    increase and posts a spike nobody caused, which is the same defect as the
    -94% drop with the sign flipped, and the one more likely to page someone.

    AWS is flat at $90,000 across both days. GCP appears yesterday at $40,000.
    The like-for-like change is 0%.
    """
    from finops.scheduler import jobs
    from finops.storage.db import get_engine

    _sqlite_at(tmp_path / "finops.db")
    today = date.today()
    d1 = (today - timedelta(days=1)).isoformat()
    d2 = (today - timedelta(days=2)).isoformat()
    _seed(get_engine(), [
        ("aws", "Amazon EC2", d2, 90_000.0),
        ("aws", "Amazon EC2", d1, 90_000.0),   # flat
        ("gcp", "Compute Engine", d1, 40_000.0),  # connected today
    ])

    asyncio.run(jobs._send_daily_digest())
    pct = _vs_yesterday_pct(_slack_wire)
    blob = json.dumps(_slack_wire[0]["json"])

    assert pct is not None and abs(pct) < 0.5, (
        f"the digest reported {pct}%. AWS was flat across both days and GCP is "
        f"new, so nothing rose. Its first day of spend is not an increase"
    )
    assert "GCP" in blob and "new since" in blob, (
        f"the digest does not say GCP is newly connected, so a reader cannot "
        f"tell why the total jumped: {blob[:400]}"
    )
