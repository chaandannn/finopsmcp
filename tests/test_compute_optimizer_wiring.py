# SPDX-License-Identifier: Apache-2.0
"""Compute Optimizer has never returned a single recommendation.

Why this file exists, stated plainly: two separate parsers read Compute
Optimizer, and both were dead, in different ways, and neither said so.

  rightsizing.py  called co.get_paginator("get_ec2_instance_recommendations").
                  Only get_lambda_function_recommendations has a botocore
                  paginator; EC2 and RDS do not, so that raises
                  OperationNotPageableError. It also filtered on the values
                  "OVER_PROVISIONED" and "VERY_OVER_PROVISIONED", and the API
                  enum is Underprovisioned | Overprovisioned | Optimized |
                  NotOptimized, so neither string exists. The confidence line
                  keyed on VERY_OVER_PROVISIONED could never fire either.

  optimizer.py    same missing paginator inside a broad except logging at DEBUG,
                  so the EC2 block was structurally unreachable. Its RDS block
                  had four faults in six lines: no paginator, the response key is
                  rdsDBRecommendations rather than recommendations, the field is
                  instanceFinding rather than finding, and the savings live on a
                  recommendation OPTION under savingsOpportunity rather than on
                  the recommendation. Lambda survived the paginator, since that
                  one is real, then filtered on a finding value Lambda never
                  returns, and read savings off the option directly so every
                  figure would have been $0.00 regardless.

The cost of this is not just a missing feature. waste_evidence rates every
compute_optimizer_* finding MEASURED and high, the strongest label the trust
envelope can give, on the grounds that it is AWS's own multi-signal
recommendation with AWS's own number. That label has been describing an empty
list.

These tests build fakes from the REAL botocore service model, so a wrong response
key, a wrong field name or an invented enum value cannot pass. That matters more
than usual here: every one of these bugs is a plausible-looking string, and a
hand-written fake would have been written with the same wrong strings as the code
and agreed with it perfectly.
"""
from __future__ import annotations

import pytest

import finops.analyzers.optimizer as opt
from finops.recommendations.rightsizing import (
    CO_EC2_OVERPROVISIONED,
    CO_LAMBDA_NOT_OPTIMIZED,
    CO_RDS_OVERPROVISIONED,
    _co_monthly_savings,
    _co_pages,
    _fetch_ec2_from_co,
    _fetch_lambda_from_co,
)

boto3 = pytest.importorskip("boto3")


# ── the API contract these parsers are written against ────────────────────────

@pytest.fixture(scope="module")
def service_model():
    c = boto3.client("compute-optimizer", region_name="us-east-1",
                     aws_access_key_id="x", aws_secret_access_key="y")
    return c.meta.service_model, c


def test_the_enums_we_filter_on_are_the_enums_aws_returns(service_model):
    """Guards against the original bug class: an invented constant.

    "OVER_PROVISIONED" reads perfectly and does not exist. Asserting our
    constants against the shipped service model is the only check that would
    have caught it without a live AWS account.
    """
    sm, _ = service_model
    ec2 = sm.operation_model("GetEC2InstanceRecommendations").output_shape
    ec2_enum = ec2.members["instanceRecommendations"].member.members["finding"].enum
    assert CO_EC2_OVERPROVISIONED in ec2_enum, (
        f"we filter EC2 on {CO_EC2_OVERPROVISIONED!r}, AWS returns {ec2_enum}"
    )

    rds = sm.operation_model("GetRDSDatabaseRecommendations").output_shape
    rds_enum = rds.members["rdsDBRecommendations"].member.members["instanceFinding"].enum
    assert CO_RDS_OVERPROVISIONED in rds_enum

    lam = sm.operation_model("GetLambdaFunctionRecommendations").output_shape
    lam_enum = lam.members["lambdaFunctionRecommendations"].member.members["finding"].enum
    assert CO_LAMBDA_NOT_OPTIMIZED in lam_enum, (
        f"we filter Lambda on {CO_LAMBDA_NOT_OPTIMIZED!r}, AWS returns {lam_enum}"
    )


def test_no_paginator_exists_for_ec2_or_rds(service_model):
    """The premise of the fix, asserted rather than assumed.

    If a future botocore adds these paginators, this test fails and someone gets
    to delete the manual paging rather than carrying it forever by accident.
    """
    _, client = service_model
    assert not client.can_paginate("get_ec2_instance_recommendations")
    assert not client.can_paginate("get_rds_database_recommendations")
    assert client.can_paginate("get_lambda_function_recommendations"), (
        "Lambda's paginator is real and the code still uses it"
    )


def test_the_response_keys_we_read_are_the_keys_aws_sends(service_model):
    """optimizer.py read page["recommendations"] for RDS. The key is
    rdsDBRecommendations, so the loop body never executed."""
    sm, _ = service_model
    assert "instanceRecommendations" in sm.operation_model(
        "GetEC2InstanceRecommendations").output_shape.members
    assert "rdsDBRecommendations" in sm.operation_model(
        "GetRDSDatabaseRecommendations").output_shape.members
    assert "recommendations" not in sm.operation_model(
        "GetRDSDatabaseRecommendations").output_shape.members


def test_savings_are_nested_under_savings_opportunity(service_model):
    """Both files read estimatedMonthlySavings off the option itself, which is
    absent there, so every figure resolved to 0.0."""
    sm, _ = service_model
    opt_members = (sm.operation_model("GetEC2InstanceRecommendations")
                   .output_shape.members["instanceRecommendations"]
                   .member.members["recommendationOptions"].member.members)
    assert "savingsOpportunity" in opt_members
    assert "estimatedMonthlySavings" not in opt_members
    assert "estimatedMonthlySavings" in opt_members["savingsOpportunity"].members


# ── the paging helper ─────────────────────────────────────────────────────────

class _Recorder:
    """A Compute Optimizer client that pages via nextToken, like the real one."""

    def __init__(self, pages):
        self._pages = pages
        self.calls = []

    def _serve(self, **kw):
        self.calls.append(kw)
        idx = 0 if "nextToken" not in kw else int(kw["nextToken"])
        return self._pages[idx]

    get_ec2_instance_recommendations = _serve
    get_rds_database_recommendations = _serve


def test_paging_follows_next_token_to_the_end():
    c = _Recorder([
        {"instanceRecommendations": [{"instanceArn": "a"}], "nextToken": "1"},
        {"instanceRecommendations": [{"instanceArn": "b"}]},
    ])
    pages = list(_co_pages(c, "get_ec2_instance_recommendations"))
    assert len(pages) == 2
    assert c.calls[0] == {}, "the first call must not send a nextToken"
    assert c.calls[1] == {"nextToken": "1"}


def test_paging_stops_on_a_single_page():
    c = _Recorder([{"instanceRecommendations": []}])
    assert len(list(_co_pages(c, "get_ec2_instance_recommendations"))) == 1
    assert len(c.calls) == 1


def test_paging_passes_filters_through_on_every_page():
    c = _Recorder([
        {"instanceRecommendations": [], "nextToken": "1"},
        {"instanceRecommendations": []},
    ])
    f = [{"name": "Finding", "values": [CO_EC2_OVERPROVISIONED]}]
    list(_co_pages(c, "get_ec2_instance_recommendations", filters=f))
    assert all(call.get("filters") == f for call in c.calls), (
        "dropping the filter on page two would silently widen the query"
    )


# ── the savings reader ────────────────────────────────────────────────────────

def test_savings_are_read_from_the_nested_path():
    v, cur = _co_monthly_savings(
        {"savingsOpportunity": {"estimatedMonthlySavings": {"value": 41.5, "currency": "USD"}}})
    assert (v, cur) == (41.5, "USD")


def test_savings_on_the_option_itself_are_not_read():
    """The shape the old code expected. It must not be honoured, because reading
    it would mean the wrong path still works and the bug could come back."""
    v, _ = _co_monthly_savings({"estimatedMonthlySavings": {"value": 99.0}})
    assert v == 0.0


def test_missing_or_junk_savings_degrade_to_zero():
    assert _co_monthly_savings({})[0] == 0.0
    assert _co_monthly_savings({"savingsOpportunity": {}})[0] == 0.0
    assert _co_monthly_savings(
        {"savingsOpportunity": {"estimatedMonthlySavings": {"value": "x"}}})[0] == 0.0


# ── EC2, end to end through the real parser ───────────────────────────────────

def _ec2_client(recs):
    class _C:
        def __init__(self):
            self.filters = None

        def get_ec2_instance_recommendations(self, **kw):
            self.filters = kw.get("filters")
            return {"instanceRecommendations": recs}

    return _C()


EC2_REC = {
    "instanceArn": "arn:aws:ec2:us-east-1:111122223333:instance/i-0abc",
    "currentInstanceType": "m5.4xlarge",
    "instanceName": "api-prod",
    "finding": "Overprovisioned",
    "utilizationMetrics": [{"name": "Cpu", "statistic": "Average", "value": 4.2}],
    "recommendationOptions": [
        {"instanceType": "m5.large", "rank": 1, "performanceRisk": 1.0,
         "savingsOpportunity": {"estimatedMonthlySavings": {"value": 310.0, "currency": "USD"}}},
        {"instanceType": "m5.xlarge", "rank": 2,
         "savingsOpportunity": {"estimatedMonthlySavings": {"value": 120.0, "currency": "USD"}}},
    ],
}


def test_ec2_returns_a_recommendation_at_all():
    """The headline. Before the fix this raised OperationNotPageableError."""
    out = _fetch_ec2_from_co(_ec2_client([EC2_REC]), "111122223333")
    assert len(out) == 1, "Compute Optimizer produced no EC2 recommendation"


def test_ec2_reads_the_real_numbers():
    r = _fetch_ec2_from_co(_ec2_client([EC2_REC]), "111122223333")[0]
    assert r.instance_id == "i-0abc"
    assert r.instance_type == "m5.4xlarge"
    assert r.recommended_type == "m5.large", "must take rank 1, not list order"
    assert r.monthly_savings == 310.0, "savings must come from savingsOpportunity"
    assert r.region == "us-east-1"
    assert r.source == "compute_optimizer"


def test_ec2_filters_on_a_finding_value_aws_actually_returns():
    c = _ec2_client([EC2_REC])
    _fetch_ec2_from_co(c, "111122223333")
    values = c.filters[0]["values"]
    assert values == [CO_EC2_OVERPROVISIONED]
    assert "OVER_PROVISIONED" not in values and "VERY_OVER_PROVISIONED" not in values


def test_ec2_ignores_a_non_usd_figure():
    """Unchanged behaviour, re-pinned: we do not invent an exchange rate."""
    rec = {**EC2_REC, "recommendationOptions": [
        {"instanceType": "m5.large", "rank": 1,
         "savingsOpportunity": {"estimatedMonthlySavings": {"value": 310.0, "currency": "EUR"}}}]}
    assert _fetch_ec2_from_co(_ec2_client([rec]), "1")[0].monthly_savings == 0.0


def test_ec2_skips_a_recommendation_with_no_options():
    rec = {**EC2_REC, "recommendationOptions": []}
    assert _fetch_ec2_from_co(_ec2_client([rec]), "1") == []


def test_ec2_pages_through_more_than_one_response():
    class _C:
        def __init__(self):
            self.n = 0

        def get_ec2_instance_recommendations(self, **kw):
            self.n += 1
            if "nextToken" not in kw:
                return {"instanceRecommendations": [EC2_REC], "nextToken": "t"}
            return {"instanceRecommendations": [
                {**EC2_REC, "instanceArn": EC2_REC["instanceArn"].replace("i-0abc", "i-0def")}]}

    out = _fetch_ec2_from_co(_C(), "1")
    assert {r.instance_id for r in out} == {"i-0abc", "i-0def"}, (
        "the second page was dropped, so large accounts lose recommendations"
    )


# ── the optimizer.py parser, end to end ───────────────────────────────────────

class _Session:
    def __init__(self, ec2=(), rds=(), lam=()):
        self._ec2, self._rds, self._lam = list(ec2), list(rds), list(lam)

    def client(self, name, **kw):
        outer = self

        class _CO:
            def get_ec2_instance_recommendations(self, **k):
                return {"instanceRecommendations": outer._ec2}

            def get_rds_database_recommendations(self, **k):
                return {"rdsDBRecommendations": outer._rds}

            def get_paginator(self, op):
                assert op == "get_lambda_function_recommendations", (
                    f"get_paginator({op!r}) does not exist in botocore and raises"
                )

                class _P:
                    def paginate(self, **k):
                        return [{"lambdaFunctionRecommendations": outer._lam}]

                return _P()

        return _CO()


RDS_REC = {
    "resourceArn": "arn:aws:rds:us-east-1:111122223333:db:orders",
    "currentDBInstanceClass": "db.r5.4xlarge",
    "instanceFinding": "Overprovisioned",
    "accountId": "111122223333",
    "instanceRecommendationOptions": [
        {"dbInstanceClass": "db.r5.xlarge", "rank": 1,
         "savingsOpportunity": {"estimatedMonthlySavings": {"value": 880.0, "currency": "USD"}}},
    ],
}


def test_optimizer_ec2_block_is_reachable_now():
    out = opt._fetch_compute_optimizer_recommendations(_Session(ec2=[EC2_REC]))
    ec2 = [f for f in out if f["waste_type"] == "compute_optimizer_overprovisioned_ec2"]
    assert len(ec2) == 1, "the EC2 block produced nothing, as it always has"
    assert ec2[0]["estimated_monthly_savings"] == 310.0


def test_optimizer_rds_block_reads_the_right_key_field_and_savings():
    """Four faults in six lines, all of which had to be fixed together."""
    out = opt._fetch_compute_optimizer_recommendations(_Session(rds=[RDS_REC]))
    rds = [f for f in out if f["waste_type"] == "compute_optimizer_overprovisioned_rds"]
    assert len(rds) == 1, (
        "no RDS finding: the response key, the field name, the paginator or the "
        "savings path is still wrong"
    )
    assert rds[0]["estimated_monthly_savings"] == 880.0
    assert rds[0]["resource_id"] == "orders"


def test_optimizer_lambda_savings_are_no_longer_always_zero():
    lam = {
        "functionArn": "arn:aws:lambda:us-east-1:111122223333:function:resize",
        "currentMemorySize": 3008,
        "finding": "NotOptimized",
        "memorySizeRecommendationOptions": [
            {"memorySize": 512, "rank": 1,
             "savingsOpportunity": {"estimatedMonthlySavings": {"value": 63.25, "currency": "USD"}}},
        ],
    }
    out = opt._fetch_compute_optimizer_recommendations(_Session(lam=[lam]))
    fn = [f for f in out if f["waste_type"] == "compute_optimizer_overprovisioned_lambda"]
    assert len(fn) == 1, "Lambda filtered on a finding value Lambda never returns"
    assert fn[0]["estimated_monthly_savings"] == 63.25, (
        "savings read off the option instead of its savingsOpportunity, which is "
        "how every Lambda recommendation came to be worth $0.00"
    )


def test_an_account_not_opted_in_still_returns_cleanly():
    """Compute Optimizer is opt-in. Not having it is not an error."""
    class _Dead:
        def client(self, *a, **k):
            raise RuntimeError("OptInRequiredException")

    assert opt._fetch_compute_optimizer_recommendations(_Dead()) == []


def test_one_dead_service_does_not_take_the_others_down():
    """Per-service try blocks, verified. RDS raising must not lose the EC2 rows."""
    class _Partial(_Session):
        def client(self, name, **kw):
            co = super().client(name, **kw)

            def _boom(**k):
                raise RuntimeError("AccessDeniedException on RDS recommendations")

            co.get_rds_database_recommendations = _boom
            return co

    out = opt._fetch_compute_optimizer_recommendations(_Partial(ec2=[EC2_REC]))
    assert any(f["waste_type"] == "compute_optimizer_overprovisioned_ec2" for f in out), (
        "an RDS failure swallowed the EC2 findings too, so one missing IAM "
        "permission would silently empty the whole Compute Optimizer result"
    )
