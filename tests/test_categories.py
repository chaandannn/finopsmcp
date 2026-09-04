"""AI and GPU as a real number: the classifier, the rollup, and honest zero.

These are the open-core halves of the aicost workstream. The cross-fork test
(duckdb == pyarrow classify identically) lives in the enterprise suite next to
cur_s3.py, because the two aggregators only resolve once the enterprise seam is
installed.
"""
from __future__ import annotations

from datetime import date, timedelta

import pytest

from finops import categories as cat
from finops.storage import category_rollup as roll
from finops.storage import db as db_mod
from finops.storage.snapshots import store_snapshot


@pytest.fixture
def fresh_db(tmp_path, monkeypatch):
    prev_engine, prev_dir = db_mod._ENGINE, db_mod._DATA_DIR
    monkeypatch.setenv("FINOPS_DB_PATH", str(tmp_path / "finops.db"))
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.delenv("FINOPS_PROFILE", raising=False)
    db_mod._ENGINE = None
    db_mod._DATA_DIR = None
    yield db_mod
    if db_mod._ENGINE is not None and db_mod._ENGINE is not prev_engine:
        try:
            db_mod._ENGINE.dispose()
        except Exception:
            pass
    db_mod._ENGINE = prev_engine
    db_mod._DATA_DIR = prev_dir


# ── classifier ───────────────────────────────────────────────────────────────

def test_gpu_instance_under_ec2_is_ai():
    # A p4d/g5 running under plain EC2 is AI, read off the usage type.
    assert cat.classify_category("aws", "AmazonEC2", "BoxUsage:p4d.24xlarge") == "ai"
    assert cat.classify_category("aws", "Amazon Elastic Compute Cloud",
                                 "BoxUsage:g5.2xlarge") == "ai"
    # instance_type carries the same signal.
    assert cat.classify_category("aws", "AmazonEC2", None, "p4d.24xlarge") == "ai"


def test_plain_ec2_is_compute_not_ai():
    assert cat.classify_category("aws", "AmazonEC2", "BoxUsage:m5.large") == "compute"
    assert cat.classify_category("aws", "AmazonEC2", "BoxUsage:c5.2xlarge",
                                 "c5.2xlarge") == "compute"


def test_sp_recurring_fee_is_compute_never_gpu():
    # An SP/RI recurring fee line must be compute even when a GPU token is present.
    # "never guess GPU" on a fee line: it carries no usage to read.
    assert cat.classify_category("aws", "AmazonEC2",
                                 "SavingsPlanRecurringFee", "p4d.24xlarge") == "compute"
    assert cat.classify_category("aws", "AmazonEC2",
                                 "RIFee:p4d.24xlarge") == "compute"


def test_managed_ai_services_are_ai_by_name():
    for svc in ("AmazonSageMaker", "Amazon SageMaker", "AmazonBedrock",
                "AmazonComprehend", "Vertex AI"):
        assert cat.classify_category("aws", svc) == "ai", svc


def test_model_provider_bill_is_ai():
    assert cat.classify_category("openai", "GPT-4o") == "ai"
    assert cat.classify_category("anthropic", "Claude Sonnet") == "ai"


def test_service_code_and_human_name_agree():
    assert (cat.classify_category("aws", "AmazonS3")
            == cat.classify_category("aws", "Amazon Simple Storage Service")
            == "storage")
    assert cat.classify_category("aws", "Amazon RDS") == "data"
    assert cat.classify_category("aws", "Amazon CloudFront") == "network"


def test_unknown_service_falls_to_other():
    assert cat.classify_category("aws", "SomeBrandNewService") == "other"
    assert cat.classify_category("datadog", "Log Management") == "other"


def test_classify_returns_only_known_keys():
    for svc in ("AmazonEC2", "AmazonS3", "AmazonRDS", "AmazonSageMaker",
                "Amazon CloudFront", "Weird"):
        assert cat.classify_category("aws", svc) in cat.CATEGORY_KEYS


def test_is_ai_and_ai_kind():
    assert cat.is_ai("ai") is True
    assert cat.is_ai("compute") is False
    assert cat.ai_kind("openai", "GPT-4o") == "model_provider"
    assert cat.ai_kind("aws", "AmazonSageMaker") == "managed_ai"
    assert cat.ai_kind("aws", "AmazonEC2") == "accelerator_compute"


def test_labels_are_plain_english():
    assert cat.CATEGORY_LABELS["ai"] == "AI and GPU"
    # No raw service codes leak through a label.
    assert cat.ai_label("openai", "GPT-4o") == "OpenAI"
    assert cat.ai_label("aws", "AmazonSageMaker") == "SageMaker"
    for label in cat.CATEGORY_LABELS.values():
        assert "Amazon" not in label and "AWS" not in label


# ── persistence + rollup ─────────────────────────────────────────────────────

def _seed(day: date):
    """A small connected account: GPU EC2 (ai), plain EC2 (compute), S3 (storage),
    and an OpenAI line (ai)."""
    store_snapshot("aws", "AmazonEC2", "111", "us-east-1", day, 300.0,
                   category="ai")
    store_snapshot("aws", "AmazonEC2", "111", "us-west-2", day, 100.0,
                   category="compute")
    store_snapshot("aws", "AmazonS3", "111", "us-east-1", day, 50.0,
                   category="storage")
    store_snapshot("openai", "GPT-4o", "acct", "", day, 200.0, category="ai")


def test_category_persists_and_reads_back(fresh_db):
    day = date(2026, 9, 1)
    _seed(day)
    totals = roll.window_category_totals(day, day)
    assert totals["ai"] == 500.0        # 300 GPU EC2 + 200 OpenAI
    assert totals["compute"] == 100.0
    assert totals["storage"] == 50.0
    assert totals["network"] == 0.0


def test_window_totals_sum_to_total(fresh_db):
    day = date(2026, 9, 1)
    _seed(day)
    totals = roll.window_category_totals(day, day)
    assert round(sum(totals.values()), 2) == 650.0   # 300+100+50+200


def test_daily_series_per_day_sums_to_day_total(fresh_db):
    d1, d2 = date(2026, 9, 1), date(2026, 9, 2)
    _seed(d1)
    store_snapshot("aws", "AmazonEC2", "111", "us-east-1", d2, 400.0, category="ai")
    series = roll.daily_category_series(d1, d2)
    assert round(sum(series[d1.isoformat()].values()), 2) == 650.0
    assert round(sum(series[d2.isoformat()].values()), 2) == 400.0


def test_ai_breakdown_shape_and_kinds(fresh_db):
    day = date(2026, 9, 1)
    _seed(day)
    rows = roll.ai_breakdown(day, day)
    keys = {r["key"] for r in rows}
    assert keys == {"aws:AmazonEC2", "openai:GPT-4o"}
    # Sorted largest first, pct sums to ~100, kinds and plain labels present.
    assert rows[0]["amount"] >= rows[-1]["amount"]
    assert round(sum(r["pct"] for r in rows), 0) == 100.0
    by_key = {r["key"]: r for r in rows}
    assert by_key["openai:GPT-4o"]["kind"] == "model_provider"
    assert by_key["openai:GPT-4o"]["label"] == "OpenAI"
    assert by_key["aws:AmazonEC2"]["kind"] == "accelerator_compute"


def test_ai_window_and_prior(fresh_db):
    cur_day, prior_day = date(2026, 9, 8), date(2026, 9, 1)
    _seed(cur_day)
    store_snapshot("openai", "GPT-4o", "acct", "", prior_day, 120.0, category="ai")
    cur, prior = roll.ai_window_and_prior(cur_day, cur_day, prior_day, prior_day)
    assert cur == 500.0
    assert prior == 120.0


def test_null_category_folds_to_other_and_still_reconciles(fresh_db):
    # A pre-migration row (category is NULL) counts as "other", so the totals
    # still sum to the window total, and categories_available reflects reality.
    day = date(2026, 9, 1)
    store_snapshot("aws", "AmazonEC2", "111", "us-east-1", day, 90.0)  # no category
    assert roll.categories_available(day, day) is False
    totals = roll.window_category_totals(day, day)
    assert totals["other"] == 90.0
    assert round(sum(totals.values()), 2) == 90.0


def test_categories_available_true_after_categorized_ingest(fresh_db):
    day = date(2026, 9, 1)
    _seed(day)
    assert roll.categories_available(day, day) is True


def test_all_cpu_account_reports_ai_zero_honestly(fresh_db):
    # An account with no GPU and no AI service: AI is $0, not fabricated.
    day = date(2026, 9, 1)
    store_snapshot("aws", "AmazonEC2", "111", "us-east-1", day, 500.0,
                   category=cat.classify_category("aws", "AmazonEC2", "BoxUsage:m5.large"))
    store_snapshot("aws", "AmazonS3", "111", "us-east-1", day, 120.0,
                   category=cat.classify_category("aws", "AmazonS3"))
    totals = roll.window_category_totals(day, day)
    assert totals["ai"] == 0.0
    assert roll.ai_breakdown(day, day) == []
    cur, _ = roll.ai_window_and_prior(day, day, day - timedelta(days=1),
                                      day - timedelta(days=1))
    assert cur == 0.0
