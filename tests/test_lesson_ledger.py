"""The lesson ledger: learned adjustments as durable, reversible objects.

The signal recomputes verdicts live, so without the ledger they change silently.
These tests pin the three guarantees:
  1. every non-neutral verdict becomes a recorded lesson carrying its evidence
  2. a rollback pins the key to neutral EVERYWHERE (signal, rescorer), and sync
     never re-learns a rolled-back key until the customer restores it
  3. sync is idempotent: an unchanged signal confirms lessons, never duplicates
"""
from __future__ import annotations

import tempfile
from datetime import datetime, timezone
from pathlib import Path

import pytest

from finops.recommendations.learning.rescorer import rescore


@pytest.fixture
def ledger_db(monkeypatch):
    td = tempfile.TemporaryDirectory()
    monkeypatch.setenv("FINOPS_DB_PATH", str(Path(td.name) / "t.db"))
    import finops.storage.db as db_mod
    db_mod._ENGINE = None
    yield db_mod
    db_mod._ENGINE = None
    td.cleanup()


_seq = [0]


def _seed(source, status, est=100.0, ver=None, n=1, bucket=None, reason_category=None):
    from finops.storage.db import get_engine, savings_recommendations
    now = datetime.now(timezone.utc)
    with get_engine().begin() as conn:
        for _ in range(n):
            _seq[0] += 1
            conn.execute(savings_recommendations.insert().values(
                source=source, provider="aws", status=status,
                estimated_monthly_savings_usd=est, verified_monthly_savings_usd=ver,
                generated_at=now, dedup_key=f"k{_seq[0]}", resource_id=f"r{_seq[0]}",
                environment_bucket=bucket, dismiss_reason_category=reason_category,
            ))


def _suppressed_source(source="commitment"):
    """10 resolved, 0 acted: WARM, and the Bayesian-shrunk act-rate
    (0 + 0.4*5)/(10+5) = 0.133 sits under the 0.15 suppress floor. (8 would
    give 0.154, just above it; the shrinkage is doing its job.)"""
    _seed(source, "dismissed", n=10)


# ── guarantee 1: transitions become evidenced lessons ─────────────────────────

def test_a_suppression_becomes_a_recorded_lesson_with_evidence(ledger_db):
    from finops.recommendations.learning.ledger import lessons, sync_lessons
    _suppressed_source()
    counts = sync_lessons()
    assert counts["new"] == 1
    ls = lessons()
    assert len(ls) == 1
    lesson = ls[0]
    assert lesson["key"] == "source:commitment"
    assert lesson["to_verdict"] == "suppress"
    assert lesson["status"] == "active"
    assert "acted on 0/10" in lesson["lesson"]
    # The evidence snapshot is the moment of learning, not an empty stub.
    assert lesson["evidence"]["resolved"] == 10
    assert lesson["evidence"]["coverage"] == "WARM"


def test_a_lesson_with_no_evidence_cannot_exist(ledger_db):
    """Every recorded lesson carries the counts that caused it."""
    from finops.recommendations.learning.ledger import lessons, sync_lessons
    _suppressed_source()
    sync_lessons()
    for lesson in lessons():
        assert lesson["evidence"], f"lesson {lesson['key']} recorded without evidence"
        assert lesson["evidence"].get("resolved") is not None


def test_neutral_sources_produce_no_lessons(ledger_db):
    from finops.recommendations.learning.ledger import lessons, sync_lessons
    _seed("idle", "open", n=5)          # COLD: no decisions at all
    _seed("waste", "acted_on", n=2)     # WARMING: below the floor
    sync_lessons()
    assert lessons() == []


def test_the_approval_floor_becomes_a_lesson(ledger_db):
    from finops.recommendations.learning.ledger import lessons, sync_lessons
    _seed("idle", "acted_on", est=500.0, n=4)
    _seed("idle", "dismissed", est=20.0, n=4)
    sync_lessons()
    floor = [l for l in lessons() if l["key"] == "approval_floor"]
    assert len(floor) == 1
    assert floor[0]["evidence"]["approval_floor_usd"] == 500.0
    assert "rank lower" in floor[0]["lesson"]


# ── guarantee 3: idempotent sync ──────────────────────────────────────────────

def test_resync_confirms_rather_than_duplicates(ledger_db):
    from finops.recommendations.learning.ledger import lessons, sync_lessons
    _suppressed_source()
    sync_lessons()
    counts = sync_lessons()
    assert counts["new"] == 0
    assert counts["confirmed"] == 1
    assert len(lessons()) == 1


def test_a_verdict_flip_supersedes_and_records_the_transition(ledger_db):
    from finops.recommendations.learning.ledger import lessons, sync_lessons
    _suppressed_source()
    sync_lessons()
    # The org starts acting on commitment recs: 12 acted on top of 8 dismissed
    # takes the shrunk act-rate past the boost floor.
    _seed("commitment", "acted_on", n=12)
    sync_lessons()
    active = [l for l in lessons()
              if l["status"] == "active" and l["kind"] == "verdict"]
    assert len(active) == 1
    assert active[0]["to_verdict"] == "boost"
    assert active[0]["from_verdict"] == "suppress", "the transition must be recorded"
    history = lessons(include_history=True)
    assert any(l["status"] == "superseded" and l["to_verdict"] == "suppress"
               for l in history)


def test_an_unlearned_lesson_is_superseded_not_deleted(ledger_db):
    from finops.recommendations.learning.ledger import lessons, sync_lessons
    from finops.storage.db import get_engine, savings_recommendations
    _suppressed_source()
    sync_lessons()
    with get_engine().begin() as conn:
        conn.execute(savings_recommendations.delete())
    sync_lessons()
    assert lessons() == []
    history = lessons(include_history=True)
    assert len(history) == 1
    assert history[0]["status"] == "superseded"


# ── guarantee 2: rollback pins the key, everywhere, until restored ────────────

def test_rollback_neutralises_the_signal_and_the_rescorer(ledger_db):
    """The wiring test: after a rollback, the rescorer must stop suppressing.
    If the override were applied anywhere but inside customer_signal, this is
    the test that fails."""
    from finops.recommendations.learning.ledger import lessons, rollback, sync_lessons
    from finops.recommendations.learning.signal import customer_signal
    _suppressed_source()
    sync_lessons()

    rec = {"source": "commitment", "estimated_monthly_savings_usd": 900.0}
    out = rescore([rec], customer_signal(), use_context=False)
    assert out["suppressed_count"] == 1, "precondition: the source is suppressed"

    result = rollback(lessons()[0]["id"], note="we are ramping commitments now")
    assert result["ok"]

    sig = customer_signal()
    assert sig["overrides_applied"] == ["source:commitment"]
    out = rescore([rec], sig, use_context=False)
    assert out["suppressed_count"] == 0
    assert out["ranked"][0]["learned"]["source_verdict"] == "neutral"
    assert "rolled" in out["ranked"][0]["learned"]["why_ranked"]


def test_sync_never_relearns_a_rolled_back_key(ledger_db):
    from finops.recommendations.learning.ledger import lessons, rollback, sync_lessons
    _suppressed_source()
    sync_lessons()
    rollback(lessons()[0]["id"])
    # More rejections make the raw signal argue even harder for suppression.
    _seed("commitment", "dismissed", n=6)
    counts = sync_lessons()
    assert counts["new"] == 0
    assert counts["skipped_rolled_back"] == 1
    statuses = {l["status"] for l in lessons(include_history=True)}
    assert statuses == {"rolled_back"}


def test_restore_lets_the_signal_decide_again(ledger_db):
    from finops.recommendations.learning.ledger import (lessons, restore,
                                                        rollback, sync_lessons)
    from finops.recommendations.learning.signal import customer_signal
    _suppressed_source()
    sync_lessons()
    lid = lessons()[0]["id"]
    rollback(lid)
    restore(lid)
    counts = sync_lessons()
    assert counts["new"] == 1, "after restore, live evidence re-creates the lesson"
    sig = customer_signal()
    assert sig.get("overrides_applied", []) == []
    assert [s for s in sig["by_source"] if s["source"] == "commitment"][0]["verdict"] == "suppress"


def test_rolled_back_floor_removes_the_dollar_floor(ledger_db):
    from finops.recommendations.learning.ledger import lessons, rollback, sync_lessons
    from finops.recommendations.learning.signal import customer_signal
    _seed("idle", "acted_on", est=500.0, n=4)
    _seed("idle", "dismissed", est=20.0, n=4)
    sync_lessons()
    floor_lesson = [l for l in lessons() if l["key"] == "approval_floor"][0]
    rollback(floor_lesson["id"])
    sig = customer_signal()
    assert sig["approval_profile"]["approval_floor_usd"] is None
    assert "approval_floor" in sig["overrides_applied"]


def test_rollback_of_a_missing_lesson_is_an_error_not_a_crash(ledger_db):
    from finops.recommendations.learning.ledger import restore, rollback
    assert "error" in rollback(99999)
    assert "error" in restore(99999)


def test_the_raw_signal_flag_bypasses_overrides(ledger_db):
    """sync must diff reality. If it saw the overridden signal it would read
    every rollback as 'the signal changed' and supersede the pinned lesson."""
    from finops.recommendations.learning.ledger import lessons, rollback, sync_lessons
    from finops.recommendations.learning.signal import customer_signal
    _suppressed_source()
    sync_lessons()
    rollback(lessons()[0]["id"])
    raw = customer_signal(apply_learned_overrides=False)
    entry = [s for s in raw["by_source"] if s["source"] == "commitment"][0]
    assert entry["verdict"] == "suppress", "raw signal must show the true verdict"
