"""Regression tests for the cost-math defects confirmed in the 2026-08-11 audit.

Why this file exists, stated plainly: nable's promise is that the dollar figure
it prints is the dollar figure the customer would actually get. Nine confirmed
findings in that audit were arithmetic, not plumbing. A total summed over the
top 50 rows and labelled the account total. A saving banked at list price
because a tier read a field name that does not exist. Three mutually exclusive
proposals on one instance adding to 156% of what the instance costs. One
unpriced Aurora Serverless v2 instance taking the whole `nable scan` down with a
TypeError. None of these break CI. They print a number, and the number is wrong,
which is the only failure mode that costs trust rather than uptime.

Where the seams are. Nothing below replaces the function under test. The fakes
sit at the provider boundary and use the shapes the APIs document: a boto3
Session and boto3.client whose rds/cloudwatch/elbv2/ce clients answer with real
response shapes, the row list Athena hands back for a query, cost_snapshots rows
written into a real SQLite database, and the scanner functions run_full_cost_audit
fans out to. Everything between that boundary and the printed dollar figure is
our code: the detectors, the dedup, the sort, the aggregation, the critique, the
rate arithmetic, the evidence split, the scorecard.

Two kinds of test live here, and each docstring says which it is:
  - "Fails today" reproduces a live defect. Red is the correct result until the
    fix lands. Do not weaken one to go green.
  - "Invariant" would pass today and goes red only if someone breaks the rule.

No network, no credentials, no Cost Explorer, no Athena, no LLM.
"""
from __future__ import annotations

import ast
import asyncio
import pathlib
import re
from datetime import date, datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

NOW = datetime.now(timezone.utc)
SRC = pathlib.Path(__file__).resolve().parents[1] / "src" / "finops"


# ── shared fixtures ───────────────────────────────────────────────────────────

@pytest.fixture(autouse=True)
def _isolated(monkeypatch, tmp_path):
    """Keep every test off the developer's own database, vault and CUR config.

    The savings tracker, the scorecard and the audit's learning loop all read and
    write ~/.finops, so an unisolated run would both pollute it and be steered by
    whatever is already there. The engine and data dir are module globals that
    ignore the env vars once populated, and the savings-context caches are
    15-minute module globals monkeypatch cannot see, so all four are reset here
    and restored afterwards.
    """
    import finops.recommendations.effective_savings as es
    import finops.recommendations.genuine_savings as gs
    from finops.storage import db

    saved_engine, saved_dir = db._ENGINE, db._DATA_DIR
    monkeypatch.setenv("FINOPS_DB_PATH", str(tmp_path / "finops.db"))
    monkeypatch.setenv("FINOPS_DATA_DIR", str(tmp_path / "data"))
    for var in ("CUR_S3_BUCKET", "CUR_ATHENA_DATABASE", "CUR_ATHENA_TABLE",
                "CUR_ATHENA_RESULTS_BUCKET", "AWS_ROLE_ARNS"):
        monkeypatch.delenv(var, raising=False)
    db._ENGINE, db._DATA_DIR = None, None
    es._reset_cache_for_tests()
    gs._reset_cache_for_tests()
    yield
    db._ENGINE, db._DATA_DIR = saved_engine, saved_dir
    es._reset_cache_for_tests()
    gs._reset_cache_for_tests()


class _Paginator:
    """boto3's paginator surface, narrowed to what the detectors call."""

    def __init__(self, pages):
        self._pages = pages

    def paginate(self, **kwargs):
        return list(self._pages)


class _FakeCostExplorer:
    """Cost Explorer's GetCostAndUsage response for an account on a 35% discount.

    AmortizedCost / OnDemandCostEquivalent is the ratio rate_detector derives the
    effective rate from, so faking the response rather than the detector keeps
    every line of that arithmetic real. The Savings Plans calls raise AccessDenied,
    which is the common real permission gap and pushes the commitment fallback
    out of the way so the measured rate is the tier under test.
    """

    def __init__(self, amortized=65000.0, on_demand_equivalent=100000.0):
        self._amortized = amortized
        self._od = on_demand_equivalent

    def get_cost_and_usage(self, **kwargs):
        if "GroupBy" in kwargs:
            return {"ResultsByTime": [{"Groups": []}]}
        return {"ResultsByTime": [{"Total": {
            "AmortizedCost": {"Amount": str(self._amortized)},
            "OnDemandCostEquivalent": {"Amount": str(self._od)},
            "NetAmortizedCost": {"Amount": str(self._amortized)},
        }}]}

    def get_savings_plans_coverage(self, **kwargs):
        raise RuntimeError("AccessDeniedException: ce:GetSavingsPlansCoverage")

    def get_reservation_coverage(self, **kwargs):
        raise RuntimeError("AccessDeniedException: ce:GetReservationCoverage")


def _fake_boto3_clients(monkeypatch, **by_service):
    """Route boto3.client to the given fakes. Any other service is an error: a
    cost-math test that reaches a real AWS endpoint has stopped being a unit."""
    import boto3

    def _client(service, *args, **kwargs):
        fake = by_service.get(service)
        if fake is None:
            raise AssertionError(f"test tried to build a real boto3 {service} client")
        return fake

    monkeypatch.setattr(boto3, "client", _client)


# ── 1. An unpriced RDS instance must not take the whole audit down ────────────
#
# check_rds_idle deliberately emits estimated_monthly_savings=None with
# "unpriced": True when DBInstanceClass is missing from the 17-entry _RDS_HOURLY
# table, rather than inventing a rate. Aurora Serverless v2 always lands there:
# it reports Engine="aurora-postgresql" with DBInstanceClass="db.serverless", so
# the `if "aurora-serverless" in engine` guard never fires. The optimizer's
# aggregation then adds and sorts that None.

def _rds_session(instances):
    """A boto3 Session whose rds/cloudwatch/sts clients answer with documented
    shapes. Every instance reports zero connections across 14 daily datapoints,
    so check_rds_idle's own measured signal fires and only the pricing differs."""
    class _RDS:
        def get_paginator(self, name):
            return _Paginator([{"DBInstances": list(instances)}])

    class _CW:
        def get_metric_statistics(self, **kwargs):
            return {"Datapoints": [
                {"Timestamp": NOW - timedelta(days=i), "Maximum": 0.0}
                for i in range(14)
            ]}

    class _STS:
        def get_caller_identity(self):
            return {"Account": "111122223333"}

    class _Session:
        def client(self, service, region_name=None, **kwargs):
            return {"rds": _RDS(), "cloudwatch": _CW(), "sts": _STS()}[service]

    return _Session()


_AURORA_SERVERLESS_V2 = {
    "DBInstanceIdentifier": "analytics-aurora-v2",
    "DBInstanceClass": "db.serverless",       # absent from _RDS_HOURLY
    "Engine": "aurora-postgresql",            # NOT "aurora-serverless"
    "DBInstanceStatus": "available",
    "MultiAZ": False,
}
_PRICED_IDLE = {
    "DBInstanceIdentifier": "legacy-reporting",
    "DBInstanceClass": "db.m5.xlarge",        # $0.342/hr * 730 = $249.66/mo
    "Engine": "postgres",
    "DBInstanceStatus": "available",
    "MultiAZ": False,
}


@pytest.mark.parametrize("instances,priced_total", [
    ([_AURORA_SERVERLESS_V2], 0.0),                    # the sum raises first
    ([_PRICED_IDLE, _AURORA_SERVERLESS_V2], 249.66),   # the sort raises first
])
@pytest.mark.xfail(strict=True, reason="audit finding, not yet fixed. strict=True so that fixing it FAILS here until this marker is removed: the marker count is the work list.")
def test_one_unpriced_finding_does_not_kill_the_deep_audit(monkeypatch, instances, priced_total):
    """Fails today. One Aurora Serverless v2 database with no connections makes
    run_deep_audit raise instead of return, and cli_scan calls it with no
    try/except, so the flagship first-run command dies with a raw traceback and
    the customer loses every waste finding in every region, not just the RDS one.
    With a single unpriced finding the aggregation raises TypeError on int + None;
    with two or more the descending sort raises on the None comparison first, and
    the by_category/by_severity/by_region accumulators raise after that, so fixing
    only the sort key still crashes twice more. The audit has to survive, keep the
    unpriced finding visible because the idle signal really was measured, and
    count only priced dollars toward money it asks anyone to believe."""
    import boto3

    from finops.analyzers import optimizer

    monkeypatch.setattr(boto3, "Session", lambda *a, **k: _rds_session(instances))

    report = optimizer.run_deep_audit(regions=["us-east-1"], checks=["rds_idle"])

    assert "error" not in report, report
    assert report["total_findings"] == len(instances)

    ids = {f["resource_id"] for f in report["findings"]}
    assert "analytics-aurora-v2" in ids, (
        "the unpriced finding was dropped; the idle signal is measured and real, "
        "only its price is unknown")

    total = report["total_estimated_monthly_savings"]
    assert isinstance(total, (int, float)), f"total is not a number: {total!r}"
    assert total == pytest.approx(priced_total), (
        "unpriced findings must stay out of the money total, neither crashing it "
        "nor being silently counted as $0")
    assert report["total_estimated_annual_savings"] == pytest.approx(priced_total * 12)

    by_cat = report["by_category"]["rds_idle_no_connections"]
    assert by_cat["count"] == len(instances)
    assert by_cat["total_estimated_monthly_savings"] == pytest.approx(priced_total)


# ── 2. The audit total must not exceed what the resource costs ────────────────

# Every scanner run_full_cost_audit fans out to, at the module it imports it from.
# The imports are function-local, so replacing the module attribute is enough and
# the tool's own body, normalization, critique, ranking and totalling stay real.
_AUDIT_SCANNERS = {
    "recommendations.graviton": "scan_graviton_opportunities",
    "recommendations.public_ipv4": "audit_public_ipv4",
    "recommendations.lambda_concurrency": "scan_lambda_concurrency_waste",
    "recommendations.s3_bucket_keys": "scan_s3_bucket_key_opportunities",
    "recommendations.nonprod_scheduler": "identify_nonprod_resources",
    "recommendations.rds_snapshots": "audit_rds_manual_snapshots",
    "recommendations.spot_adoption": "recommend_spot_adoption",
    "recommendations.cloudwatch_cardinality": "audit_cloudwatch_metric_cardinality",
    "recommendations.cloudwatch_alarms": "audit_cloudwatch_orphaned_alarms",
    "recommendations.cloudwatch_logs_ia": "audit_cloudwatch_logs_ia_opportunities",
    "recommendations.lambda_snapstart": "recommend_lambda_snapstart",
    "recommendations.nlb_cross_zone": "audit_nlb_cross_zone_costs",
    "recommendations.s3_intelligent_tiering": "audit_s3_intelligent_tiering",
    "recommendations.s3_transfer_acceleration": "audit_s3_transfer_acceleration",
    "recommendations.ebs_snapshot_replication": "audit_ebs_snapshot_replication",
    "recommendations.database_savings_plans": "recommend_database_savings_plans",
    "recommendations.textract_env": "scan_textract_environment_waste",
    "recommendations.bedrock_routing": "recommend_bedrock_model_routing",
    "recommendations.commitments": "analyze_commitments",
    "cleanup.idle": "scan_idle_resources",
    "analyzers.waste": "scan_all_regions_rds_idle",
}

# One Environment=dev m5.xlarge in an ASG, $140.16/mo at list. Three scanners
# each propose a different and mutually exclusive fate for that one box.
_INSTANCE_ID = "i-0dev1"
_INSTANCE_MONTHLY = 140.16
_AUDIT_OVERRIDES = {
    "scan_graviton_opportunities": lambda **kw: [{
        "instance_id": _INSTANCE_ID, "instance_type": "m5.xlarge",
        "graviton_equivalent": "m6g.xlarge", "savings_estimate": 21.02,
        "savings_pct": 0.15, "region": "us-east-1",
        "current_monthly_cost_estimate": _INSTANCE_MONTHLY,
    }],
    "identify_nonprod_resources": lambda **kw: {"schedulable_instances": [{
        "instance_id": _INSTANCE_ID, "name": _INSTANCE_ID,
        "potential_monthly_savings": 98.45,
        "environment": "dev", "idle_hours_per_week": 128,
    }]},
    "recommend_spot_adoption": lambda **kw: [{
        "instance_id": _INSTANCE_ID, "instance_type": "m5.xlarge",
        "monthly_savings": 99.51, "recommendation": "RECOMMENDED",
        "savings_pct": 0.71,
    }],
}


@pytest.fixture
def audit_env(monkeypatch):
    """AWS reads as connected (the credential probe is the boundary, not the tool)
    and every scanner answers from the fixtures above."""
    import finops.server as server

    aws = server.CLOUD_CONNECTORS.get("aws")
    assert aws is not None, "the AWS connector is not registered"

    async def _configured():
        return True

    monkeypatch.setattr(aws, "is_configured", _configured)

    for mod, name in _AUDIT_SCANNERS.items():
        module = __import__(f"finops.{mod}", fromlist=[name])
        monkeypatch.setattr(module, name, _AUDIT_OVERRIDES.get(name, lambda **kw: None))
    return server


def test_audit_total_cannot_exceed_the_resource_it_is_about(audit_env):
    """Fails today. Spot adoption ($99.51), non-prod scheduling ($98.45) and
    Graviton migration ($21.02) are three mutually exclusive fates for ONE
    m5.xlarge that costs $140.16/mo. run_full_cost_audit sums them with no
    resource-level dedup and prints "Estimated monthly saving: $218.98", which is
    156% of the instance. You cannot run a box on Spot, shut it down nights and
    weekends, and migrate it to Graviton and collect the full delta of each. The
    per-finding critique cannot catch this: every claim is individually plausible
    and nothing checks the sum. A customer who acts on the headline finds a third
    of it was never there, on the first number the product ever showed them."""
    out = asyncio.run(audit_env.run_full_cost_audit())

    assert isinstance(out, str)
    m = re.search(r"Estimated monthly saving: \$([\d,]+\.\d\d)", out)
    assert m, f"could not find the headline total in:\n{out}"
    headline = float(m.group(1).replace(",", ""))

    assert headline <= _INSTANCE_MONTHLY + 0.01, (
        f"the audit claims ${headline:,.2f}/mo of savings on one instance that "
        f"costs ${_INSTANCE_MONTHLY:,.2f}/mo "
        f"({headline / _INSTANCE_MONTHLY * 100:.0f}% of the resource)")


def test_every_audit_finding_carries_the_resource_it_is_about(audit_env, monkeypatch):
    """Fails today, and pins the input the fix above needs. Per-resource dedup is
    impossible while findings are anonymous, and the nonprod and spot handlers in
    run_full_cost_audit's normalizer emit no resource_id at all, so nothing
    downstream can tell that all three proposals are about the same box. The
    normalizer is a closure inside the tool, so the critique call is the first
    point outside it where the normalized findings can be observed; this spy
    reads them on the way past and calls straight through to the real critique."""
    from finops.recommendations import critique as critique_mod

    real_critique = critique_mod.critique
    captured: list[dict] = []

    def _spy(findings, **kwargs):
        captured.extend(findings)
        return real_critique(findings, **kwargs)

    monkeypatch.setattr(critique_mod, "critique", _spy)
    asyncio.run(audit_env.run_full_cost_audit())

    assert captured, "no findings reached the critique step"
    anonymous = sorted(f["title"] for f in captured if not f.get("resource_id"))
    assert not anonymous, (
        f"these findings carry no resource_id, so no downstream step can collapse "
        f"them onto the resource they are about: {anonymous}")


# ── 3. The CUR slice total must be the account total, not the page total ──────

class _Rec:
    """One normalized FOCUS record, the shape the in-memory slice engine reads."""

    def __init__(self, resource_id, cost):
        self.ResourceId = resource_id
        self.BilledCost = cost


# A $400,000/mo account with the long tail a real bill has: three named
# resources at the top, then hundreds of small ones. `ORDER BY metric DESC
# LIMIT 3` returns exactly _TOP, worth $30,000, which is 7.5% of the account.
_TOP = [("i-0aaa", 18000.0), ("i-0bbb", 9000.0), ("i-0ccc", 3000.0)]
_TAIL = [(f"i-tail{i:04d}", 1000.0) for i in range(370)]
_ACCOUNT = _TOP + _TAIL
_GRAND_TOTAL = 400000.0


@pytest.mark.xfail(strict=True, reason="audit finding, not yet fixed. strict=True so that fixing it FAILS here until this marker is removed: the marker count is the work list.")
def test_cur_slice_total_is_the_grand_total_not_the_visible_page(monkeypatch):
    """Fails today. build_cur_sql appends LIMIT n (default 50) and run_slice_cur
    then accumulates `total` over only the rows that came back, so slicing a
    $400,000/mo account by resource_id reports the visible page's $30,000 as the
    account total. The in-memory FOCUS path does the opposite and says so in a
    comment: "Grand total over the kept set (independent of grouping/limit)".
    Same field, same tool, two meanings, and swapping the group-by from
    resource_id to ResourceId routes the identical question to the other engine.
    The server instructions mandate leading with "the headline number first ($X
    total)", so the model states $30,000 as what the account spent."""
    from finops.slice import cur_engine, parse_spec

    for var, val in (("CUR_S3_BUCKET", "cur-bucket"),
                     ("CUR_ATHENA_DATABASE", "curdb"),
                     ("CUR_ATHENA_TABLE", "curtbl"),
                     ("CUR_ATHENA_RESULTS_BUCKET", "res-bucket")):
        monkeypatch.setenv(var, val)

    top_three = [{"d_resource_id": rid, "metric": str(cost)} for rid, cost in _TOP]

    def _fake_athena(sql, timeout_secs=30):
        """The Athena boundary. A grouped, limited query returns the page exactly
        as Athena would. An ungrouped SUM, which a correct implementation has to
        issue to know the account total, returns the real total."""
        if "GROUP BY" in sql:
            return list(top_three)
        return [{"metric": str(_GRAND_TOTAL)}]

    monkeypatch.setattr(cur_engine, "_athena_query", _fake_athena)

    spec = parse_spec({"dimensions": ["resource_id"], "metric": "BilledCost", "limit": 3})
    result = cur_engine.run_slice_cur(spec, date(2026, 5, 1), date(2026, 5, 31))

    assert len(result.rows) == 3, "the page itself must still respect the limit"
    assert result.truncated is True
    assert result.total == pytest.approx(_GRAND_TOTAL), (
        f"total is ${result.total:,.2f}, the sum of the {len(result.rows)} rows "
        f"shown, not the ${_GRAND_TOTAL:,.2f} the account actually spent")


@pytest.mark.xfail(strict=True, reason="audit finding, not yet fixed. strict=True so that fixing it FAILS here until this marker is removed: the marker count is the work list.")
def test_the_two_slice_engines_agree_on_what_total_means(monkeypatch):
    """Fails today, and is the same defect stated as the contract it breaks: one
    question, one answer. `slice_costs` picks the CUR engine or the in-memory
    FOCUS engine purely from the dimension spelling (resource_id vs ResourceId),
    and both hand back a SliceResult whose `total` the tool returns as the
    headline. Over identical spend and an identical limit they must not differ."""
    from finops.slice import cur_engine, parse_spec
    from finops.slice.engine import run_slice

    for var, val in (("CUR_S3_BUCKET", "cur-bucket"),
                     ("CUR_ATHENA_DATABASE", "curdb"),
                     ("CUR_ATHENA_TABLE", "curtbl"),
                     ("CUR_ATHENA_RESULTS_BUCKET", "res-bucket")):
        monkeypatch.setenv(var, val)

    monkeypatch.setattr(
        cur_engine, "_athena_query",
        lambda sql, timeout_secs=30: (
            [{"d_resource_id": rid, "metric": str(c)} for rid, c in _TOP]
            if "GROUP BY" in sql else [{"metric": str(_GRAND_TOTAL)}]),
    )

    cur_result = cur_engine.run_slice_cur(
        parse_spec({"dimensions": ["resource_id"], "metric": "BilledCost", "limit": 3}),
        date(2026, 5, 1), date(2026, 5, 31))
    focus_result = run_slice(
        parse_spec({"dimensions": ["ResourceId"], "metric": "BilledCost", "limit": 3}),
        [_Rec(rid, cost) for rid, cost in _ACCOUNT])

    assert focus_result.total == pytest.approx(_GRAND_TOTAL)
    assert cur_result.total == pytest.approx(focus_result.total), (
        f"the same spend sliced by resource_id totals ${cur_result.total:,.2f} on "
        f"the CUR path and ${focus_result.total:,.2f} on the FOCUS path")


# ── 4. Realized savings must reach the effective-rate tier ────────────────────

def test_realized_savings_uses_the_customers_effective_rate(monkeypatch):
    """Fails today. measure._effective_rate returns `adjusted.effective`, and
    AdjustedSavings has no such field; it is `effective_savings`. The
    AttributeError is caught by a bare `except Exception` and logged at DEBUG, so
    tier 2 of the documented three-tier ladder is dead code on every install and
    a confirmed downsize is banked at full list price. For a customer on a 35%
    EDP whose CUR is not wired up, a confirmed m5.4xlarge to m5.2xlarge downsize
    banks $280.32 instead of ~$182. The honesty label survives, because only
    bill_measured counts toward verified_bill_measured_monthly_usd, but the
    dollars in verified_monthly_usd and everything downstream of it (get_nable_roi's
    banked_monthly, the dashboard, the weekly digest, the exporter) are inflated
    by exactly the customer's discount."""
    from finops.recommendations.effective_savings import adjust_savings, detect_savings_context
    from finops.recommendations.measure import measure_realized_savings

    _fake_boto3_clients(monkeypatch, ce=_FakeCostExplorer())

    # The rate really is detected off the faked bill: this is the figure tier 2
    # is supposed to bank, computed by the code under test's own collaborator.
    adjusted = adjust_savings(280.32, resource_type="ec2", ctx=detect_savings_context())
    assert adjusted.basis == "effective_rate" and adjusted.effective_savings == pytest.approx(182.21)

    # acted_on_at is None, so tier 1 (bill measurement off the CUR) declines
    # immediately and nothing reaches Athena.
    row = SimpleNamespace(
        resource_id="i-0abc123",
        resource_type="ec2",
        acted_on_at=None,
        estimated_monthly_savings_usd=280.32,   # m5.4xlarge -> m5.2xlarge at list
    )

    usd, basis = measure_realized_savings(row, 280.32)

    assert basis == "effective_rate", (
        f"basis is {basis!r}: the effective-rate tier never runs, so list price "
        f"is banked as though it had been measured")
    assert usd == pytest.approx(adjusted.effective_savings)


# ── 5. Waste patterns must key on the service names Cost Explorer emits ───────

# connectors/aws.py writes the raw Cost Explorer SERVICE dimension value into
# cost_snapshots.service, and universal.py's own map records the long form
# ("CE uses long names like 'Amazon Elastic Compute Cloud - Compute'").
CE_EC2 = "Amazon Elastic Compute Cloud - Compute"
ACCOUNT_ID = "111122223333"
EC2_PER_DAY = 900.0          # ~$27,000/mo of uncovered compute


def _seed_ce_shaped_snapshots():
    """Write 90 days of EC2 spend into the real cost_snapshots table under the
    service name Cost Explorer actually returns. This is the DB boundary: the
    rows are exactly what a successful backfill leaves behind."""
    from finops.storage.db import cost_snapshots, get_engine

    today = date.today()
    rows = [{
        "provider": "aws", "service": CE_EC2, "account_id": ACCOUNT_ID,
        "region": "us-east-1",
        "snapshot_date": (today - timedelta(days=i)).isoformat(),
        "amount_usd": EC2_PER_DAY, "granularity": "DAILY",
        "captured_at": datetime.now(timezone.utc),
    } for i in range(90)]
    engine = get_engine()
    with engine.begin() as conn:
        conn.execute(cost_snapshots.insert(), rows)


def test_waste_patterns_fire_on_real_cost_explorer_service_names():
    """Fails today. scan_waste_patterns builds PatternContext.daily_costs straight
    from cost_snapshots.service, which holds the raw CE SERVICE value, but
    patterns.py looks up "Amazon EC2", "Amazon S3" and "AWSDataTransfer", none of
    which Cost Explorer ever emits. Every lookup returns $0, so the gp2 fallback,
    S3 intelligent tiering, savings-plans coverage, the NAT spike and weekend
    waste all bail before emitting, and the tool returns total_monthly_waste: 0.0
    with no error, which reads to a customer as "your account is clean". On this
    $27,000/mo account at 0% Savings Plans coverage, the single largest finding
    the product can make never fires."""
    import finops.server as server

    _seed_ce_shaped_snapshots()
    result = asyncio.run(server.scan_waste_patterns(account_id=ACCOUNT_ID))

    assert "error" not in result, result
    fired = {f["pattern_id"] for f in result["findings"]}
    assert "no-savings-plans" in fired, (
        f"${EC2_PER_DAY * 30:,.0f}/mo of uncovered EC2 and the savings-plans "
        f"pattern did not fire; patterns that did: {sorted(fired) or 'none at all'}")
    assert result["total_monthly_waste"] > 0


def _service_literals_in_patterns() -> set[str]:
    """Every string literal patterns.py passes to a service lookup, by AST so a
    rename cannot hide from it."""
    tree = ast.parse((SRC / "ml" / "patterns.py").read_text())
    found: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
            continue
        target = node.func.value
        is_lookup = node.func.attr in ("service_monthly", "service_series")
        is_daily_get = (node.func.attr == "get" and isinstance(target, ast.Attribute)
                        and target.attr == "daily_costs")
        if not (is_lookup or is_daily_get):
            continue
        if node.args and isinstance(node.args[0], ast.Constant) and isinstance(node.args[0].value, str):
            found.add(node.args[0].value)
    return found


# Service names in patterns.py that belong to a SaaS/LLM connector rather than to
# AWS, so Cost Explorer is not the source and the CE map does not apply.
_NON_AWS_SERVICES = {"OpenAI", "Anthropic"}


def test_patterns_never_look_up_a_service_name_cost_explorer_cannot_emit():
    """The invariant half: no pattern may look up a name that silently reads $0.

    This test's MECHANISM changed when the finding was fixed, and the reason is
    worth recording, because "the test told me to do X" is a bad reason to do X
    when X provably does not work.

    As written, it required each literal to BE a Cost Explorer name. Measured
    against the CE shapes this file already seeds, that prescription fails twice:

        daily_costs keys, as CE returns them:
          "Amazon Elastic Compute Cloud - Compute"        $27,000/mo
          "Amazon Elastic Compute Cloud - Data Transfer"   $1,200/mo

        literal "Amazon Elastic Compute Cloud"          -> [] , a total miss
        literal "Amazon Elastic Compute Cloud - Compute" -> $27,000 of $28,200

    CE splits a service across usage-type suffixes, so an exact-match literal is
    either wrong or incomplete, and a pattern asking what EC2 costs means all of
    it. Writing the long name into six call sites would also have restored the
    duplication that let these drift out of date unnoticed in the first place.

    So the lookup resolves instead: patterns keep asking for a short readable
    name and PatternContext.service_series maps it through the repo's existing
    alias map and sums every matching CE bucket. The invariant is unchanged and
    slightly stronger: every literal must RESOLVE, and a name that cannot be
    resolved is a bug rather than a silent $0.

    The vacuity guard below is the load-bearing part. An invariant expressed as
    "the resolver accepts it" is worthless if the resolver accepts anything.
    """
    from finops.connectors.universal import _AWS_ALIASES
    from finops.ml.patterns import _ce_service_name

    ce_names = set(_AWS_ALIASES.values())

    unresolvable = sorted(
        lit for lit in _service_literals_in_patterns()
        if lit not in _NON_AWS_SERVICES and _ce_service_name(lit) is None
    )
    assert not unresolvable, (
        f"patterns.py looks up {unresolvable}, which does not resolve to any Cost "
        f"Explorer service name, so every one of those reads $0 with no error; "
        f"add the spelling to _SERVICE_SPELLINGS or the service to the alias map")

    # Whatever it resolves to must be a name CE can actually emit, not just any
    # string. Without this, a resolver returning its input unchanged would pass.
    for lit in _service_literals_in_patterns() - _NON_AWS_SERVICES:
        assert _ce_service_name(lit) in ce_names, (
            f"{lit!r} resolves to {_ce_service_name(lit)!r}, which is not in the "
            f"CE name map; the resolver is inventing names")

    # Anti-vacuity: the resolver must still REFUSE a name Cost Explorer cannot
    # emit. If it says yes to everything, both assertions above are theatre and
    # the original defect walks straight back in under a new spelling.
    for invented in ("AmazonNotAService", "AWSTotallyMadeUp", "Amazon EC3", ""):
        assert _ce_service_name(invented) is None, (
            f"the resolver accepted {invented!r}, so it cannot distinguish a real "
            f"service from a typo and this test proves nothing")


# ── 6. The rightsizing dedup family must cover what the detectors emit ────────

def _emitted_waste_types() -> set[str]:
    """Every waste_type literal any detector in the tree actually emits."""
    emitted: set[str] = set()
    for path in SRC.rglob("*.py"):
        try:
            tree = ast.parse(path.read_text())
        except SyntaxError:  # pragma: no cover - defensive
            continue
        for node in ast.walk(tree):
            if not isinstance(node, ast.Dict):
                continue
            for k, v in zip(node.keys, node.values):
                if (isinstance(k, ast.Constant) and k.value == "waste_type"
                        and isinstance(v, ast.Constant) and isinstance(v.value, str)):
                    emitted.add(v.value)
    return emitted


def test_rightsizing_dedup_map_keys_types_the_detectors_actually_emit():
    """Fails today. _RIGHTSIZING_FAMILY exists solely so the audit total is not
    "inflated by double-counting the same instance", and half its keys are
    spellings nothing produces. It maps lambda_over_provisioned_memory while the
    detector emits lambda_memory_overprovisioned (transposed), and it maps
    idle_rds, which only the CLI scan emits, while the deep audit's own detectors
    emit rds_idle_no_connections and rds_overprovisioned. Level 2 of the dedup
    therefore never fires for Lambda or RDS on the audit path, and no test in the
    suite references the map at all."""
    from finops.analyzers.optimizer import _RIGHTSIZING_FAMILY

    dead_keys = sorted(set(_RIGHTSIZING_FAMILY) - _emitted_waste_types())
    assert not dead_keys, (
        f"the dedup map keys waste types no detector emits: {dead_keys}")

    for waste_type, intent in (("lambda_memory_overprovisioned", "lambda"),
                               ("rds_idle_no_connections", "rds"),
                               ("rds_overprovisioned", "rds")):
        assert waste_type in _RIGHTSIZING_FAMILY, (
            f"{waste_type} is emitted on the deep-audit path but is absent from "
            f"the dedup map, so its {intent} findings are never collapsed")

    assert (_RIGHTSIZING_FAMILY.get("rds_idle_no_connections")
            == _RIGHTSIZING_FAMILY.get("rds_overprovisioned")), (
        "both RDS detectors describe the same action on the same instance, so "
        "they have to share a family key to be collapsed")


def test_one_rds_instance_flagged_twice_is_counted_once():
    """Fails today, and needs no credentials. check_rds_rightsizing (low CPU) and
    check_rds_idle (no connections) have no mutual exclusion, so a quiet,
    oversized database emits BOTH findings on any default scan: stop it, and also
    downsize it. Those are not additive, and the audit total gains the instance
    roughly one and a half times over."""
    from finops.analyzers.optimizer import _dedup_findings

    findings = [
        {"resource_id": "reporting-db", "waste_type": "rds_overprovisioned",
         "estimated_monthly_savings": 499.32, "region": "us-east-1"},
        {"resource_id": "reporting-db", "waste_type": "rds_idle_no_connections",
         "estimated_monthly_savings": 998.64, "region": "us-east-1"},
    ]
    out = _dedup_findings(findings)
    claimed = sum(f["estimated_monthly_savings"] for f in out)

    assert len(out) == 1, (
        f"one instance, two proposals, {len(out)} findings survive dedup and "
        f"${claimed:,.2f}/mo is claimed on a database that costs $998.64/mo")
    assert out[0]["estimated_monthly_savings"] == pytest.approx(998.64)


# ── 7. The scorecard must report the commitment savings it was handed ─────────

_COMMITMENT_DATA = {
    "coverage_pct": 10.0,
    "on_demand_usd": 50000.0,
    "potential_savings_usd": 16000.0,
}


def test_commitment_dimension_publishes_the_savings_build_scorecard_reads():
    """Fails today. _score_commitment_coverage reads potential_savings_usd into a
    local and writes only coverage_pct and on_demand_spend_usd into its metadata,
    while build_scorecard reads commits.metadata["potential_savings_usd"] * 0.7
    back out of it. The read is always 0. This is the lower half of the defect:
    the producer and the consumer of one metadata key disagree, silently, because
    a dict .get() with a default cannot tell "absent" from "zero"."""
    from finops.scoring.scorecard import _score_commitment_coverage

    dim = _score_commitment_coverage(dict(_COMMITMENT_DATA))

    assert dim.metadata.get("potential_savings_usd") == pytest.approx(16000.0), (
        f"metadata carries {sorted(dim.metadata)}; build_scorecard reads "
        f"potential_savings_usd from here and gets 0")


def test_scorecard_recoverable_total_includes_the_commitment_opportunity():
    """Fails today, one level up, and this is the sentence the customer reads. An
    account at 10% coverage with a $16,000/mo Savings Plan opportunity should be
    told about 70% of it, $11,200, in "Estimated $X/month recoverable". It is
    told $0 while the same report grades commitments the biggest gap, so the
    report names the problem and prices it at nothing. The dollar figure does
    survive verbatim in the actions string, so the error is one-directional and
    conservative, which is why nobody has noticed."""
    from finops.scoring.scorecard import build_scorecard

    card = build_scorecard(
        scope="overall", label="Overall",
        commitment_data=dict(_COMMITMENT_DATA),
        total_monthly_spend=60000.0,
    )

    assert card.potential_savings_usd >= 16000.0 * 0.7 - 0.01, (
        f"recoverable is ${card.potential_savings_usd:,.2f}/mo and drops the "
        f"$11,200 commitment component entirely; the summary reads "
        f"{card.summary!r}")


# ── 8. One savings basis across the rightsizing family ───────────────────────

@pytest.mark.xfail(strict=True, reason="audit finding, not yet fixed. strict=True so that fixing it FAILS here until this marker is removed: the marker count is the work list.")
def test_rds_rightsizing_prices_on_the_same_basis_as_its_ec2_sibling(monkeypatch):
    """Fails today. get_rightsizing_recommendations routes savings through
    detect_savings_context + adjust_savings and returns genuine_monthly_savings
    with a pricing_basis block that says how the figure was priced.
    get_rds_rightsizing_recommendations mentions none of those and just sums the
    _RDS_HOURLY list-price table, and get_ecs_rightsizing_recommendations does the
    same. For a customer with a measured 35% EDP, an RDS downsize really worth
    ~$325/mo is quoted at $499 with no confidence label, while its EC2 sibling in
    the same session is quoted correctly. Two tools in one family answer "how
    much will this save" on different bases with no marker telling them apart."""
    import finops.server as server

    class _RDS:
        def get_paginator(self, name):
            return _Paginator([{"DBInstances": [{
                "DBInstanceIdentifier": "reporting-db",
                "DBInstanceClass": "db.m5.4xlarge",   # downsizes to db.m5.2xlarge
                "Engine": "postgres",
                "DBInstanceStatus": "available",
                "MultiAZ": False,
            }]}])

    class _CW:
        def get_metric_statistics(self, **kwargs):
            return {"Datapoints": [
                {"Timestamp": NOW - timedelta(hours=i), "Average": 4.0}
                for i in range(48)
            ]}

    _fake_boto3_clients(monkeypatch, rds=_RDS(), cloudwatch=_CW(), ce=_FakeCostExplorer())

    result = asyncio.run(server.get_rds_rightsizing_recommendations(regions=["us-east-1"]))

    assert result.get("count") == 1, result
    list_price = 499.32                       # (1.368 - 0.684) * 730
    effective = round(list_price * 0.65, 2)   # 324.56 on a measured 35% discount

    assert "pricing_basis" in result, (
        "the RDS tool ships a dollar figure with no statement of how it was "
        "priced, unlike get_rightsizing_recommendations, so nothing tells a "
        "reader it is list price")

    quoted = {result.get("total_monthly_savings")}
    quoted |= {f.get("estimated_monthly_savings") for f in result.get("findings", [])}
    quoted |= {f.get("adjusted_monthly_savings") for f in result.get("findings", [])}
    assert any(v is not None and abs(v - effective) < 0.5 for v in quoted), (
        f"nothing in the response reflects the customer's measured 35% rate "
        f"(${effective:,.2f}); the figures offered are {sorted(v for v in quoted if v is not None)}, "
        f"i.e. the ${list_price:,.2f} list-price sum")


# ── 9. One price for one load balancer ───────────────────────────────────────

def test_idle_load_balancer_has_a_single_price(monkeypatch):
    """Fails today. analyzers/waste.py prices an idle ALB at _ALB_HOURLY (0.008)
    * 730 = $5.84/mo; cleanup/idle.py prices the same resource at $16.20/mo and
    names the real rate in its comment. idle.py is right: $0.0225 per ALB-hour is
    the hourly base charge and $0.008 is the LCU-hour price, so waste.py
    understates the same load balancer by about 64%. get_idle_load_balancers,
    audit_aws_waste and `nable scan` take the waste.py path while
    list_idle_resources and run_full_cost_audit take idle.py's, so two tools give
    answers 2.8x apart for the same load balancer in the same session, and
    waste_evidence labels the wrong one MEASURED at high confidence."""
    from finops.analyzers import waste as waste_mod
    from finops.analyzers.waste import check_idle_load_balancers
    from finops.cleanup.idle import _ALB_PER_MONTH

    class _ELBv2:
        def get_paginator(self, name):
            return _Paginator([{"LoadBalancers": [{
                "LoadBalancerName": "legacy-api",
                "LoadBalancerArn":
                    "arn:aws:elasticloadbalancing:us-east-1:111122223333:"
                    "loadbalancer/app/legacy-api/abc123",
                "Type": "application",
                "State": {"Code": "active"},
            }]}])

    class _ELB:
        def get_paginator(self, name):
            return _Paginator([{"LoadBalancerDescriptions": []}])

    class _CW:
        def get_metric_statistics(self, **kwargs):
            return {"Datapoints": [{"Timestamp": NOW, "Sum": 0.0}]}

    findings = check_idle_load_balancers(_ELBv2(), _ELB(), _CW(), "us-east-1")

    assert len(findings) == 1 and findings[0]["waste_type"] == "idle_load_balancer"
    assert findings[0]["estimated_monthly_savings"] == pytest.approx(_ALB_PER_MONTH), (
        f"the deep-audit path prices this ALB at "
        f"${findings[0]['estimated_monthly_savings']:,.2f}/mo while cleanup/idle.py "
        f"prices the identical resource at ${_ALB_PER_MONTH:,.2f}/mo")
    # The constants themselves, while a second copy of the price still exists.
    # Single-sourcing them (deleting these names) is the fix, so their absence is
    # a pass here and the behavioural assertion above stays the load-bearing one.
    for name in ("_ALB_HOURLY", "_NLB_HOURLY"):
        hourly = getattr(waste_mod, name, None)
        if hourly is not None:
            assert hourly * 730 == pytest.approx(_ALB_PER_MONTH), (
                f"waste.{name} * 730 is ${hourly * 730:,.2f}/mo against "
                f"idle.py's ${_ALB_PER_MONTH:,.2f}/mo; one load balancer, one price")
