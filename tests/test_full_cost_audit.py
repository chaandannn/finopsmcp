"""run_full_cost_audit must actually run.

Two guardrails that regressed in the field:
1. Its 21 scanner imports and call signatures have to resolve. A wrong import
   name plus two wrong call signatures once aborted the entire audit before any
   scan ran, and no test caught it because none exercised the real import path.
2. The scanners must run in parallel. As bare gathered coroutines they shared one
   event loop and ran back-to-back (the sum of their times); each now runs in its
   own thread so the sweep is bounded by the slowest scanner.
"""
from __future__ import annotations

import asyncio
import time

import pytest

import finops.server as server

# (source module, function name) for every scanner run_full_cost_audit imports.
_SCANNERS = {
    "graviton": "scan_graviton_opportunities",
    "public_ipv4": "audit_public_ipv4",
    "lambda_concurrency": "scan_lambda_concurrency_waste",
    "s3_bucket_keys": "scan_s3_bucket_key_opportunities",
    "nonprod_scheduler": "identify_nonprod_resources",
    "rds_snapshots": "audit_rds_manual_snapshots",
    "spot_adoption": "recommend_spot_adoption",
    "cloudwatch_cardinality": "audit_cloudwatch_metric_cardinality",
    "cloudwatch_alarms": "audit_cloudwatch_orphaned_alarms",
    "cloudwatch_logs_ia": "audit_cloudwatch_logs_ia_opportunities",
    "lambda_snapstart": "recommend_lambda_snapstart",
    "nlb_cross_zone": "audit_nlb_cross_zone_costs",
    "s3_intelligent_tiering": "audit_s3_intelligent_tiering",
    "s3_transfer_acceleration": "audit_s3_transfer_acceleration",
    "ebs_snapshot_replication": "audit_ebs_snapshot_replication",
    "database_savings_plans": "recommend_database_savings_plans",
    "textract_env": "scan_textract_environment_waste",
    "bedrock_routing": "recommend_bedrock_model_routing",
    "commitments": "analyze_commitments",
}

# These two live outside finops.recommendations.*, so they carry their own
# full module path instead of being prefixed with "recommendations." below.
_SCANNERS_FULL_PATH = {
    "cleanup.idle": "scan_idle_resources",
    "analyzers.waste": "scan_all_regions_rds_idle",
}


@pytest.fixture
def audit_env(monkeypatch):
    """AWS appears configured and auth is bypassed, so the audit body runs without
    touching the network."""
    monkeypatch.setattr(server, "require_role", lambda *a, **k: None)
    aws = server.CLOUD_CONNECTORS.get("aws")

    async def _configured():
        return True

    monkeypatch.setattr(aws, "is_configured", _configured)
    return monkeypatch


def _patch_all(monkeypatch, fn):
    # Patch the name on each source module. run_full_cost_audit's function-local
    # `from .recommendations.X import Y` re-reads the module attribute at call
    # time, so a wrong import name in server.py would still raise here.
    for mod, name in _SCANNERS.items():
        m = __import__(f"finops.recommendations.{mod}", fromlist=[name])
        monkeypatch.setattr(m, name, fn)
    for mod, name in _SCANNERS_FULL_PATH.items():
        m = __import__(f"finops.{mod}", fromlist=[name])
        monkeypatch.setattr(m, name, fn)


def test_audit_imports_and_signatures_resolve(audit_env):
    _patch_all(audit_env, lambda **kw: [])
    out = asyncio.run(server.run_full_cost_audit())
    assert isinstance(out, str)


def test_audit_runs_scanners_in_parallel(audit_env):
    # 21 scanners each sleeping 0.2s: serial would be 4.2s. Parallel stays well
    # under that even on a low-core CI box.
    def slow(**kw):
        time.sleep(0.2)
        return []

    _patch_all(audit_env, slow)
    start = time.perf_counter()
    asyncio.run(server.run_full_cost_audit())
    elapsed = time.perf_counter() - start
    assert elapsed < 2.0, f"audit took {elapsed:.2f}s; scanners are running serially"


def test_audit_surfaces_idle_ec2_and_idle_rds_on_a_bare_account(audit_env):
    # The account a plain EC2+RDS shop actually has: every fancier scanner
    # (Graviton, Lambda, S3, Textract, Bedrock, ...) legitimately finds nothing,
    # but there is a stopped EC2 instance and an idle RDS instance sitting there.
    # Before wiring scan_idle_resources/scan_all_regions_rds_idle into this
    # audit, a bare account like this got "no waste found" and had no way to
    # know the scanner never looked at either of those two categories.
    from finops.cleanup.idle import IdleResource

    _patch_all(audit_env, lambda **kw: [])

    stopped_instance = IdleResource(
        resource_type="stopped_ec2", resource_id="i-0abc123", region="us-east-1",
        account_id="123456789012", name="worker-1", idle_since="2026-06-01",
        idle_days=35, monthly_cost_usd=40.0, reason="Stopped instance still billing for EBS",
    )

    def _idle_resources(**kw):
        return [stopped_instance]

    def _idle_rds(**kw):
        return [{
            "resource_id": "db-prod-replica", "estimated_monthly_savings": 120.0,
            "engine": "postgres", "current_class": "db.t3.medium", "region": "us-east-1",
        }]

    audit_env.setattr(server, "require_role", lambda *a, **k: None)
    import finops.cleanup.idle as idle_mod
    import finops.analyzers.waste as waste_mod
    audit_env.setattr(idle_mod, "scan_idle_resources", _idle_resources)
    audit_env.setattr(waste_mod, "scan_all_regions_rds_idle", _idle_rds)

    out = asyncio.run(server.run_full_cost_audit())
    assert "i-0abc123" in out or "worker-1" in out
    assert "db-prod-replica" in out
