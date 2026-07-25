"""A fix that came undone must be reported as a regression, not as a new finding.

"Nothing stays optimized" is the drift complaint every practitioner has: you
rightsize, workloads move, and six months later the same resource is oversized
again. A point-in-time scanner can only say the resource is wrong right now.
Because the recommendation row survives across scans, nable can also say you
already fixed this, when, and how many times it has come back.

Before this, record_recommendation only re-opened dismissed/expired rows. An
acted_on or verified row that got flagged again hit `return existing.id` and the
regression was invisible.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from finops.recommendations import savings_tracker as st


@pytest.fixture(autouse=True)
def _isolated_db(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("FINOPS_DB_PATH", str(tmp_path / "test.db"))
    from finops.storage import db as _db
    monkeypatch.setattr(_db, "_ENGINE", None, raising=False)
    yield


def _record(savings: float = 100.0, description: str = "downsize m5.xlarge -> m5.large"):
    return st.record_recommendation(
        source="rightsizing",
        provider="aws",
        resource_id="i-drifty",
        resource_type="ec2",
        resource_name="api-server-3",
        current_config={"instance_type": "m5.xlarge"},
        recommended_config={"instance_type": "m5.large"},
        description=description,
        estimated_monthly_savings_usd=savings,
    )


def _backdate(rec_id: int, *, field: str, days: int):
    """Move a timestamp into the past so the grace period is cleared."""
    from sqlalchemy import update

    from finops.storage.db import get_engine, savings_recommendations
    when = datetime.now(timezone.utc) - timedelta(days=days)
    with get_engine().begin() as conn:
        conn.execute(
            update(savings_recommendations)
            .where(savings_recommendations.c.id == rec_id)
            .values(**{field: when})
        )


def test_reflagging_an_acted_on_fix_records_a_regression():
    rec_id = _record()
    assert st.mark_acted_on(rec_id)
    _backdate(rec_id, field="acted_on_at", days=30)

    # Six months later the detector sees the same oversized instance again.
    assert _record() == rec_id, "should update the existing row, not create a new one"

    rec = st.get_recommendation(rec_id)
    assert rec["status"] == "open", "a regressed fix must be actionable again"
    assert rec["regression_count"] == 1
    assert rec["regressed_at"] is not None


def test_regression_count_accumulates():
    rec_id = _record()
    for expected in (1, 2, 3):
        st.mark_acted_on(rec_id)
        _backdate(rec_id, field="acted_on_at", days=30)
        _record()
        assert st.get_recommendation(rec_id)["regression_count"] == expected


def test_a_pending_change_inside_the_grace_period_is_not_a_regression():
    """You marked it acted-on yesterday; the resize lands in the next maintenance
    window and CloudWatch still shows the old shape. Accusing the user of a
    regression here would make the signal worthless."""
    rec_id = _record()
    st.mark_acted_on(rec_id)

    _record()  # detector still sees the old config

    rec = st.get_recommendation(rec_id)
    assert rec["regression_count"] == 0
    assert rec["status"] == "acted_on", "must not be re-opened while the change settles"


def test_a_verified_saving_that_regresses_loses_its_verification():
    """The verified figure described a state that no longer holds. Leaving it in
    place would keep counting a saving that has stopped happening."""
    rec_id = _record()
    st.mark_acted_on(rec_id)
    st.mark_verified(rec_id, 92.5, basis="bill_measured")
    _backdate(rec_id, field="verified_at", days=45)
    _backdate(rec_id, field="acted_on_at", days=50)

    _record()

    rec = st.get_recommendation(rec_id)
    assert rec["status"] == "open"
    assert rec["regression_count"] == 1
    assert rec["verified_monthly_savings_usd"] is None, (
        "a regressed fix must not keep claiming a verified saving"
    )
    assert rec["verified_at"] is None
    assert rec.get("verified_basis") is None


def test_dismissed_rows_still_reopen_without_being_called_regressions():
    """Pre-existing behaviour, preserved: you said no, it came back, so surface it
    again. But you never fixed it, so it is not a regression."""
    rec_id = _record()
    st.mark_dismissed(rec_id, reason="reserved for peak")

    _record()

    rec = st.get_recommendation(rec_id)
    assert rec["status"] == "open"
    assert rec["regression_count"] == 0


def test_list_regressions_surfaces_them_with_a_headline():
    rec_id = _record()
    st.mark_acted_on(rec_id)
    _backdate(rec_id, field="acted_on_at", days=30)
    _record()

    out = st.list_regressions()
    assert len(out) == 1
    assert out[0]["id"] == rec_id
    assert "Fixed before, back again (1x)" in out[0]["headline"]
    assert "api-server-3" in out[0]["headline"]


def test_list_regressions_ignores_untouched_recommendations():
    _record()
    assert st.list_regressions() == []
