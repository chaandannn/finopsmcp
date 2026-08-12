# SPDX-License-Identifier: Apache-2.0
"""The trust envelope must not vouch for a number nobody measured.

Why this file exists, stated plainly: confidence was asserted from a static table
keyed on finding TYPE, not derived from whether the underlying measurement
actually succeeded. WASTE_EVIDENCE mapped idle_nat_gateway to (MEASURED, high),
and it said that whether or not the CloudWatch call threw.

Two detectors then manufactured the number the table vouched for. The NAT
gateway detector caught a metric exception, set datapoints to empty, and read
that as 0.000 GB/day, so every NAT gateway in the region was reported idle the
moment CloudWatch was unreachable or ce/cloudwatch permissions were missing, each
one stamped MEASURED and high. The EC2 idle detector was worse in kind: a failed
NetworkOut read set avg_net_per_hr to 0.0, and the very next line is the guard
that spares a network-active instance from being called idle, so the failure
DISABLED the check that would have protected it.

That combination is the product's differentiator running backwards. The envelope
is what makes these numbers safer than a competitor's, and it was laundering
failures into "measured, high". A system that is most confident exactly when it
knows least is worse than one with no confidence labels at all, because the label
is what earns the trust.

The rule these tests pin: provenance beats the table. The table says what a
finding type is worth WHEN ITS MEASUREMENT SUCCEEDED. It cannot know whether this
particular read did, so a finding that reports its metric was unavailable is an
inference no matter what type it is.

Nothing here calls AWS. The detectors are driven with a CloudWatch client that
raises, which is the condition that produced the bug.
"""
from __future__ import annotations

import pytest

from finops.analyzers.waste_evidence import INFERRED, MEASURED, annotate, spec_for


def test_a_successful_measurement_keeps_its_high_confidence():
    """The envelope must still vouch for real readings, or it is worthless."""
    out = annotate([{"waste_type": "idle_nat_gateway", "estimated_monthly_savings": 32.4}])[0]
    assert out["evidence"] == MEASURED
    assert out["confidence"] == "high"
    assert out["kind"] == "recommendation"


def test_a_failed_measurement_cannot_be_stamped_measured():
    """The bug, at its narrowest.

    Same finding type, same table entry, but this one says the metric could not
    be read. Before the fix it came out MEASURED/high anyway.
    """
    out = annotate([{
        "waste_type": "idle_nat_gateway",
        "estimated_monthly_savings": 32.4,
        "metrics_unavailable": True,
        "metrics_unavailable_reason": "GetMetricStatistics AccessDenied",
    }])[0]
    assert out["evidence"] == INFERRED, (
        "a finding whose metric could not be read was stamped as measured; the "
        "envelope is vouching for a number nobody observed"
    )
    assert out["confidence"] == "low"
    assert out["kind"] != "recommendation", (
        "an unmeasured finding must be an investigation, not a firm recommendation"
    )
    assert "AccessDenied" in out["why_unsure"], "say why we are unsure"


def test_the_table_still_says_high_for_that_type():
    """Proves the override is provenance, not a downgrade of the type itself.

    If the fix had simply demoted idle_nat_gateway in the table, every correct
    reading would have lost its confidence too.
    """
    assert spec_for("idle_nat_gateway").confidence == "high"


def test_a_nat_gateway_we_could_not_measure_is_not_reported_idle(monkeypatch):
    """BytesOutToDestination is this detector's only evidence.

    Sibling detectors already `continue` on this exact failure. This one turned
    it into zero traffic, which is the most alarming possible reading of "the
    API call failed".
    """
    from finops.analyzers import waste

    class _CWRaises:
        def get_metric_statistics(self, **kw):
            raise RuntimeError("AccessDenied: cloudwatch:GetMetricStatistics")

    class _Paginator:
        """The detector paginates. An earlier version of this fake had only
        describe_nat_gateways, so get_paginator raised AttributeError, the
        detector's own try/except swallowed it and returned [], and this test
        passed while never running a single line of the code it claims to cover.
        Which is the same defect class the file is about."""

        def paginate(self, **kw):
            return [{"NatGateways": [{
                "NatGatewayId": "nat-0123456789abcdef0",
                "State": "available",
                "VpcId": "vpc-1",
                "SubnetId": "subnet-1",
                "NatGatewayAddresses": [{}],
                "Tags": [],
            }]}]

    class _EC2:
        def get_paginator(self, name):
            assert name == "describe_nat_gateways"
            return _Paginator()

    findings = waste.check_nat_gateways(
        ec2_client=_EC2(), cw_client=_CWRaises(), region="us-east-1")
    assert findings == [], (
        f"a NAT gateway whose traffic metric could not be read was reported as "
        f"idle: {findings}. A failed read is not zero bytes."
    )


def test_the_fake_actually_drives_the_detector():
    """Guards this file against the failure it exists to describe.

    If the EC2 fake drifts out of shape again, the detector returns [] for a
    reason that has nothing to do with metric provenance, and the test above goes
    green while covering nothing. So: same fake, working metrics, and the finding
    must appear. A test whose negative case cannot be distinguished from a broken
    harness is not evidence.
    """
    from finops.analyzers import waste

    class _CWQuiet:
        def get_metric_statistics(self, **kw):
            # a real read that genuinely observed near-zero traffic
            return {"Datapoints": [{"Sum": 1.0}]}

    class _Paginator:
        def paginate(self, **kw):
            return [{"NatGateways": [{
                "NatGatewayId": "nat-0123456789abcdef0", "State": "available",
                "VpcId": "vpc-1", "SubnetId": "subnet-1",
                "NatGatewayAddresses": [{}], "Tags": [],
            }]}]

    class _EC2:
        def get_paginator(self, name):
            return _Paginator()

    findings = waste.check_nat_gateways(
        ec2_client=_EC2(), cw_client=_CWQuiet(), region="us-east-1")
    assert findings, (
        "the fake no longer drives the detector, so the metrics-unavailable test "
        "above is passing without executing the code it claims to cover"
    )
