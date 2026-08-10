"""Learning drives ranking + annotation in the surfacing tools (Deliverable 1).

Covers get_savings_summary annotation and run_full_cost_audit reordering. The
learning loop is propose-only: it reorders and annotates, never hides spend
numbers and never mutates cloud. On a cold ledger it degrades to a no-op.
"""
from __future__ import annotations

import asyncio
import tempfile
from datetime import datetime, timezone
from pathlib import Path

import pytest

import finops.server as server


# ── isolated ledger fixture (mirrors test_learning.py) ────────────────────────

@pytest.fixture
def ledger(monkeypatch):
    td = tempfile.TemporaryDirectory()
    monkeypatch.setenv("FINOPS_DB_PATH", str(Path(td.name) / "t.db"))
    import finops.storage.db as db_mod
    db_mod._ENGINE = None
    yield db_mod
    db_mod._ENGINE = None
    td.cleanup()


_seq = [0]


def _seed(source, status, est=100.0, ver=None, n=1, reason_category=None):
    from finops.storage.db import get_engine, savings_recommendations
    now = datetime.now(timezone.utc)
    with get_engine().begin() as conn:
        for _ in range(n):
            _seq[0] += 1
            conn.execute(savings_recommendations.insert().values(
                source=source, provider="aws", status=status,
                estimated_monthly_savings_usd=est, verified_monthly_savings_usd=ver,
                generated_at=now, dedup_key=f"lr{_seq[0]}", resource_id=f"lr{_seq[0]}",
                dismiss_reason_category=reason_category,
            ))


# ── get_savings_summary annotation ────────────────────────────────────────────

def test_summary_cold_ledger_has_no_learning_note(ledger):
    _seed("spot", "open", n=3)  # only open -> COLD, no real signal
    out = asyncio.run(server.get_savings_summary())
    assert "learning_note" not in out
    # by_source still carries a learned block, but the verdict is neutral/COLD.
    spot = out["by_source"]["spot"]
    assert spot["learned"]["verdict"] == "neutral"
    assert spot["learned"]["coverage"] == "COLD"


def test_summary_annotates_suppressed_source(ledger):
    _seed("spot", "dismissed", n=12)  # WARM, never acted -> suppress
    out = asyncio.run(server.get_savings_summary())
    spot = out["by_source"]["spot"]
    assert spot["learned"]["verdict"] == "suppress"
    assert spot["learned"]["coverage"] == "WARM"
    assert "learning_note" in out
    assert "spot" in out["learning_note"]


def test_summary_annotation_never_changes_spend_numbers(ledger):
    _seed("idle", "open", est=200.0, n=1)
    _seed("spot", "dismissed", n=12)
    out = asyncio.run(server.get_savings_summary())
    # open potential is unchanged by learning (idle open = 200).
    assert out["by_source"]["idle"]["potential_usd"] == 200.0
    assert out["potential_monthly_usd"] == 200.0


# ── run_full_cost_audit reordering ────────────────────────────────────────────

@pytest.fixture
def audit_env(monkeypatch):
    monkeypatch.setattr(server, "require_role", lambda *a, **k: None)
    aws = server.CLOUD_CONNECTORS.get("aws")

    async def _configured():
        return True

    monkeypatch.setattr(aws, "is_configured", _configured)
    return monkeypatch


def _patch_scanners_spot_and_idle(monkeypatch):
    """Only the spot and idle_resources scanners return findings; the rest empty."""
    from finops.cleanup.idle import IdleResource

    # spot: one big RECOMMENDED conversion (large raw savings)
    def _spot(**kw):
        return [{
            "instance_id": "i-spot1", "instance_type": "m5.large",
            "monthly_savings": 900.0, "recommendation": "RECOMMENDED", "savings_pct": 0.7,
        }]

    # idle: one smaller idle EBS volume
    def _idle(**kw):
        return [IdleResource(
            resource_type="ebs_volume", resource_id="vol-1", region="us-east-1",
            account_id="123456789012", name="orphan", idle_since="2026-06-01",
            idle_days=40, monthly_cost_usd=100.0, reason="Unattached volume",
        )]

    import finops.recommendations.spot_adoption as spot_mod
    import finops.cleanup.idle as idle_mod
    monkeypatch.setattr(spot_mod, "recommend_spot_adoption", _spot)
    monkeypatch.setattr(idle_mod, "scan_idle_resources", _idle)

    # every other scanner returns empty
    empty_specs = [
        ("recommendations.graviton", "scan_graviton_opportunities"),
        ("recommendations.public_ipv4", "audit_public_ipv4"),
        ("recommendations.lambda_concurrency", "scan_lambda_concurrency_waste"),
        ("recommendations.s3_bucket_keys", "scan_s3_bucket_key_opportunities"),
        ("recommendations.nonprod_scheduler", "identify_nonprod_resources"),
        ("recommendations.rds_snapshots", "audit_rds_manual_snapshots"),
        ("recommendations.cloudwatch_cardinality", "audit_cloudwatch_metric_cardinality"),
        ("recommendations.cloudwatch_alarms", "audit_cloudwatch_orphaned_alarms"),
        ("recommendations.cloudwatch_logs_ia", "audit_cloudwatch_logs_ia_opportunities"),
        ("recommendations.lambda_snapstart", "recommend_lambda_snapstart"),
        ("recommendations.nlb_cross_zone", "audit_nlb_cross_zone_costs"),
        ("recommendations.s3_intelligent_tiering", "audit_s3_intelligent_tiering"),
        ("recommendations.s3_transfer_acceleration", "audit_s3_transfer_acceleration"),
        ("recommendations.ebs_snapshot_replication", "audit_ebs_snapshot_replication"),
        ("recommendations.database_savings_plans", "recommend_database_savings_plans"),
        ("recommendations.textract_env", "scan_textract_environment_waste"),
        ("recommendations.bedrock_routing", "recommend_bedrock_model_routing"),
        ("recommendations.commitments", "analyze_commitments"),
        ("analyzers.waste", "scan_all_regions_rds_idle"),
    ]
    for mod, name in empty_specs:
        m = __import__(f"finops.{mod}", fromlist=[name])
        monkeypatch.setattr(m, name, lambda **kw: [])


def test_audit_cold_ledger_orders_by_dollars(ledger, audit_env):
    _patch_scanners_spot_and_idle(audit_env)
    out = asyncio.run(server.run_full_cost_audit())
    # cold ledger: no confidence column, spot (bigger $) ranked above idle.
    assert "Confidence (your ledger)" not in out
    assert out.index("i-spot1") < out.index("orphan")


def test_audit_demotes_suppressed_source(ledger, audit_env):
    # spot is WARM + never acted -> suppress; idle is WARM + always acted -> boost.
    _seed("spot", "dismissed", n=12)
    _seed("idle", "verified", est=100.0, ver=100.0, n=9)
    _seed("idle", "dismissed", n=1)
    _patch_scanners_spot_and_idle(audit_env)
    out = asyncio.run(server.run_full_cost_audit())
    # despite spot's larger raw savings, the boosted+acted idle finding now leads
    # and the suppressed spot finding sinks below it. Nothing is hidden: both show.
    assert "Confidence (your ledger)" in out
    assert "i-spot1" in out and "orphan" in out
    assert out.index("orphan") < out.index("i-spot1")
    assert "rarely acted on" in out
