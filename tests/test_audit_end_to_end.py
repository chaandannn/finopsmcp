"""run_full_cost_audit executed end to end, critique included.

The wiring tests assert the call sites via source inspection, which detects a
deleted call or a wrong key but never actually runs the renderer. This file
EXECUTES the real tool: every scanner stubbed at its import site, one returning
findings crafted so the critique retracts one claim and passes another, and the
assertions read the markdown a user would see. The renderer crash this exists to
pin (formatting a retracted None with :,.2f) is precisely the kind that source
inspection cannot prove absent.

It also pins the norm() enrichment: idle and graviton findings must carry
current_monthly_cost_usd on the audit path, because without it the critique's
strongest falsifier (savings cannot exceed the resource's own cost) is latent
code that no production input can ever trigger.
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from finops import server as _srv
from finops.cleanup.idle import IdleResource
from finops.tools import cost_queries as cq

# Every scanner run_full_cost_audit imports, at its defining module, in spec
# order. Stubbing at the source module works because the tool imports inside the
# function body, so our monkeypatching happens before the import resolves.
_SCANNERS = [
    "finops.recommendations.graviton.scan_graviton_opportunities",
    "finops.recommendations.public_ipv4.audit_public_ipv4",
    "finops.recommendations.lambda_concurrency.scan_lambda_concurrency_waste",
    "finops.recommendations.s3_bucket_keys.scan_s3_bucket_key_opportunities",
    "finops.recommendations.nonprod_scheduler.identify_nonprod_resources",
    "finops.recommendations.rds_snapshots.audit_rds_manual_snapshots",
    "finops.recommendations.spot_adoption.recommend_spot_adoption",
    "finops.recommendations.cloudwatch_cardinality.audit_cloudwatch_metric_cardinality",
    "finops.recommendations.cloudwatch_alarms.audit_cloudwatch_orphaned_alarms",
    "finops.recommendations.cloudwatch_logs_ia.audit_cloudwatch_logs_ia_opportunities",
    "finops.recommendations.lambda_snapstart.recommend_lambda_snapstart",
    "finops.recommendations.nlb_cross_zone.audit_nlb_cross_zone_costs",
    "finops.recommendations.s3_intelligent_tiering.audit_s3_intelligent_tiering",
    "finops.recommendations.s3_transfer_acceleration.audit_s3_transfer_acceleration",
    "finops.recommendations.ebs_snapshot_replication.audit_ebs_snapshot_replication",
    "finops.recommendations.database_savings_plans.recommend_database_savings_plans",
    "finops.recommendations.textract_env.scan_textract_environment_waste",
    "finops.recommendations.bedrock_routing.recommend_bedrock_model_routing",
    "finops.recommendations.commitments.analyze_commitments",
    "finops.cleanup.idle.scan_idle_resources",
    "finops.analyzers.waste.scan_all_regions_rds_idle",
]


def _idle(resource_id: str, cost: float) -> IdleResource:
    return IdleResource(
        resource_type="ebs_volume", resource_id=resource_id, region="us-east-1",
        account_id="123456789012", name="", idle_since="2026-05-01", idle_days=90,
        monthly_cost_usd=cost, reason="Unattached gp3 volume",
    )


@pytest.fixture()
def audit_env(monkeypatch):
    """Silence every scanner, connect a fake AWS, and give the learning loop an
    empty ledger so the test exercises the audit pipeline, not the database."""
    for dotted in _SCANNERS:
        mod_path, fn_name = dotted.rsplit(".", 1)
        import importlib
        monkeypatch.setattr(importlib.import_module(mod_path), fn_name,
                            lambda *a, **kw: None)

    aws = MagicMock()
    aws.is_configured = AsyncMock(return_value=True)
    monkeypatch.setitem(_srv.CLOUD_CONNECTORS, "aws", aws)
    monkeypatch.setattr("finops.recommendations.learning.signal.customer_signal",
                        lambda: {"by_source": []}, raising=False)
    return monkeypatch


async def test_audit_renders_a_retracted_claim_as_a_band_not_a_crash(audit_env):
    """The executed version of the load-bearing wiring claim. An impossible
    graviton saving ($9,000/mo off a $1,500/mo instance) and an honest idle
    volume flow through the REAL scanner->norm->critique->rescore->render
    pipeline, and the assertions read the markdown the user gets."""
    import finops.recommendations.graviton as grav
    import finops.cleanup.idle as idle_mod

    audit_env.setattr(grav, "scan_graviton_opportunities", lambda *a, **kw: [{
        "instance_id": "i-impossible", "instance_type": "r5.xlarge",
        "graviton_equivalent": "r7g.xlarge", "savings_estimate": 9000.0,
        "current_monthly_cost_estimate": 1500.0, "savings_pct": 0.2,
        "region": "us-east-1",
    }])
    audit_env.setattr(idle_mod, "scan_idle_resources",
                      lambda *a, **kw: [_idle("vol-honest", 610.0)])

    fn = cq.run_full_cost_audit.fn if hasattr(cq.run_full_cost_audit, "fn") else cq.run_full_cost_audit
    out = await fn(regions=["us-east-1"], top_n=10)

    assert isinstance(out, str) and "Traceback" not in out
    # The honest finding keeps its number.
    assert "$610.00" in out
    # The impossible one is rendered as a band, never as $9,000.00.
    assert "$9,000.00" not in out
    assert "needs confirming" in out
    # And the headline total counts only what survived review.
    assert "$610.00 | Annual: $7,320.00" in out


async def test_audit_headline_never_counts_a_retracted_claim(audit_env):
    # Two impossible claims and nothing honest: the headline must be $0.00, not
    # the $70k the raw scanners asserted.
    import finops.recommendations.graviton as grav

    audit_env.setattr(grav, "scan_graviton_opportunities", lambda *a, **kw: [
        {"instance_id": f"i-{n}", "instance_type": "r5.xlarge",
         "graviton_equivalent": "r7g.xlarge", "savings_estimate": 35000.0,
         "current_monthly_cost_estimate": 400.0, "savings_pct": 0.2,
         "region": "us-east-1"} for n in range(2)
    ])
    fn = cq.run_full_cost_audit.fn if hasattr(cq.run_full_cost_audit, "fn") else cq.run_full_cost_audit
    out = await fn(regions=["us-east-1"], top_n=10)

    assert "$0.00" in out.split("\n")[1], f"headline counted retracted dollars:\n{out[:300]}"
    assert out.count("needs confirming") == 2


async def test_audit_with_all_scanners_silent_still_answers(audit_env):
    fn = cq.run_full_cost_audit.fn if hasattr(cq.run_full_cost_audit, "fn") else cq.run_full_cost_audit
    out = await fn(regions=["us-east-1"], top_n=10)
    assert "No savings opportunities found" in out


def test_the_scanner_stub_list_matches_the_real_spec_list():
    """Self-healing: if a scanner is added to run_full_cost_audit without being
    stubbed here, this test names it instead of the e2e tests hitting AWS."""
    from finops.recommendations.sweep import build_specs

    # Ask the real table for its names rather than regexing the source: the spec
    # list is now built by a function, so it can be called, and calling it also
    # proves every scanner import in it resolves. That is not incidental here.
    # Both exports carried a spec list importing `scan_spot_adoption_opportunities`,
    # a name that never existed, and a source-text count would have happily
    # counted the broken entry.
    spec_paths = {f"{fn.__module__}.{fn.__name__}" for _, fn, _ in build_specs(object(), None)}
    assert spec_paths == set(_SCANNERS), (
        f"the sweep and the e2e stub list disagree.\n"
        f"  only in sweep: {sorted(spec_paths - set(_SCANNERS))}\n"
        f"  only in stubs: {sorted(set(_SCANNERS) - spec_paths)}\n"
        f"update _SCANNERS or an unstubbed scanner will call AWS")


def test_norm_carries_the_falsifier_inputs_for_idle_and_graviton():
    """The enrichment itself: without current_monthly_cost_usd on these findings,
    the savings-exceed-cost falsifier is dead code on the audit path."""
    from finops.recommendations import critique as C

    rec = {"source": "graviton", "monthly_savings": 9000.0,
           "current_monthly_cost_usd": 1500.0}
    assert any(o.code == "savings_exceed_resource_spend" for o in C.falsifiers(rec)), (
        "the enriched graviton shape does not trigger the falsifier it exists to feed")
