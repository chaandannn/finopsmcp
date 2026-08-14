# SPDX-License-Identifier: Apache-2.0
"""A denied Cost Explorer permission must not crash the savings arithmetic.

Found 2026-08-14 while wiring the RDS rightsizing tool onto the same pricing path
the EC2 tool uses. It was already live on the EC2 path; sharing the code is what
made it visible.

_savings_plan_coverage and _ri_coverage return None when Cost Explorer denies the
call, which is the common real permission gap: ce:GetSavingsPlansCoverage and
ce:GetReservationCoverage are separate IAM actions from the Cost Explorer reads
most people grant, and plenty of accounts have one without the others.
fetch_commitment_context then built CommitmentContext(available=True, ...) with
both fields None, and combined_pct was `max(sp, ri)`, so:

    TypeError: '>' not supported between instances of 'NoneType' and 'NoneType'

Three call sites read combined_pct behind an `available` check that was, in this
state, a lie: effective_savings.adjust_savings, rightsizing_summary's
pricing_basis block, and now the shared block the RDS and ECS tools use. It
stayed hidden because the measured-rate tier usually answers first and returns
before the commitment fallback is consulted, so the crash needed a customer who
had a readable bill but unreadable commitment coverage.

The fix keeps three states apart, which is the whole point: 0.0 is "measured, no
commitments", None is "could not read", and available now means "at least one
instrument answered" rather than "we tried".
"""
from __future__ import annotations

import pytest

from finops.recommendations.genuine_savings import CommitmentContext


def test_an_unreadable_instrument_does_not_crash_the_read():
    """The exact shape CE builds when both coverage permissions are denied."""
    ctx = CommitmentContext(available=True, sp_coverage_pct=None, ri_coverage_pct=None)
    assert ctx.combined_pct == 0.0, (
        "combined_pct must survive an unreadable instrument. Every consumer "
        "reads it behind an `available` check and none of them expect a raise")


def test_one_readable_instrument_is_still_used():
    """A missing RI permission must not throw away the SP number that did arrive."""
    ctx = CommitmentContext(available=True, sp_coverage_pct=62.5, ri_coverage_pct=None)
    assert ctx.combined_pct == 62.5
    ctx = CommitmentContext(available=True, sp_coverage_pct=None, ri_coverage_pct=41.0)
    assert ctx.combined_pct == 41.0


def test_unreadable_is_not_the_same_as_zero():
    """The distinction the whole fix rests on.

    Collapsing None into 0.0 at the field would stop the crash and start a
    quieter error: an account whose coverage nobody could read would be reported
    as an account with no commitments, and every saving on it quoted as if the
    customer pays full on-demand.
    """
    unreadable = CommitmentContext(available=True, sp_coverage_pct=None, ri_coverage_pct=None)
    measured_zero = CommitmentContext(available=True, sp_coverage_pct=0.0, ri_coverage_pct=0.0)

    assert unreadable.combined_pct == measured_zero.combined_pct == 0.0
    assert not unreadable.has_coverage_data, (
        "a context with nothing readable must say so; consumers need to tell "
        "'no commitments' from 'no permission' to label their confidence")
    assert measured_zero.has_coverage_data


def test_available_means_data_not_merely_attempted(monkeypatch):
    """fetch_commitment_context must not advertise data it did not get.

    Drives the real function with a Cost Explorer that denies both coverage
    calls, rather than constructing the dataclass by hand, because the defect was
    in how the function SET available, not in the dataclass.
    """
    import finops.recommendations.genuine_savings as gs

    class _DeniedCE:
        def get_savings_plans_coverage(self, **kwargs):
            raise RuntimeError("AccessDeniedException: ce:GetSavingsPlansCoverage")

        def get_reservation_coverage(self, **kwargs):
            raise RuntimeError("AccessDeniedException: ce:GetReservationCoverage")

    gs._reset_cache_for_tests()
    try:
        ctx = gs.fetch_commitment_context(ce_client=_DeniedCE())
    finally:
        gs._reset_cache_for_tests()

    assert ctx.combined_pct == 0.0, "reading it must not raise"
    assert not ctx.available, (
        "both coverage calls were denied and the context still reports "
        "available=True, so every consumer will trust and then use a number "
        "that was never read"
    )


@pytest.mark.parametrize("sp,ri,expect", [
    (None, 12.0, "SP coverage unknown"),
    (64.0, None, "RI coverage unknown"),
    (None, None, "SP coverage unknown"),
    (64.0, 12.0, "SP coverage 64%"),
])
def test_the_weekly_digest_section_never_silently_vanishes(sp, ri, expect):
    """A swallowed error is worse here than a crash, which is why this exists.

    _section_commitments is wrapped in a try/except that logs and returns empty.
    Its plain-text fallback formatted coverage as f"{sp_cov:.0f}%", so a None
    raised, was caught, and the ENTIRE commitments section disappeared from the
    weekly digest. Not an error the customer sees. Not a wrong number. Just a
    section that is not there, every week, for anyone missing
    ce:GetSavingsPlansCoverage.

    A missing section reads as "nothing to report", which for an account with
    unreadable coverage and $50,000/mo of uncovered on-demand is the opposite of
    true. Mutation testing caught this: reverting the fallback to the raw format
    string left every other test in this file green.
    """
    import asyncio
    from unittest.mock import patch

    from finops.notifications import reports
    from finops.recommendations.commitments import CommitmentAnalysis

    analysis = CommitmentAnalysis(
        savings_plan_coverage_pct=sp, savings_plan_utilization_pct=0.0,
        savings_plan_unused_usd=1200.0, ri_coverage_pct=ri, ri_utilization_pct=0.0,
        ri_unused_usd=0.0, uncovered_on_demand_usd=50000.0, recommendations=[],
    )
    with patch("finops.recommendations.commitments.analyze_commitments",
               return_value=analysis):
        blocks, text = asyncio.run(reports._section_commitments({}))

    assert blocks, (
        f"the commitments section returned nothing for sp={sp!r} ri={ri!r}. It is "
        f"wrapped in a try/except, so a formatting error on None does not raise, "
        f"it just deletes the section from the digest"
    )
    assert expect in blocks[0]["text"]["text"], blocks[0]["text"]["text"]
    assert text, "the plain-text fallback is empty, which is where the None raised"


def test_every_consumer_of_combined_pct_survives_the_gap():
    """The wiring. Three call sites read this and all three must hold.

    Asserting on the dataclass alone would leave a consumer free to do its own
    max() over the raw fields, which is exactly what the buggy version was.
    """
    import ast
    import inspect

    import finops.server  # noqa: F401 - must import first; tools.* import back into it
    import finops.recommendations.effective_savings as es
    import finops.recommendations.rightsizing as rs
    import finops.tools.aws_waste as aw

    for module in (es, rs, aw):
        tree = ast.parse(inspect.getsource(module))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Name):
                continue
            if node.func.id != "max":
                continue
            args = ast.unparse(node)
            assert "coverage_pct" not in args, (
                f"{module.__name__} takes its own max() over coverage fields "
                f"({args}); that bypasses combined_pct and re-creates the crash"
            )
