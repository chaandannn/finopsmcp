# SPDX-License-Identifier: Apache-2.0
"""No query may compare an ISO string against a DateTime column.

A ratchet, not a unit test. It walks every comparison in src/finops, resolves
which ones touch a DateTime column, and fails on any that bind a string.

The defect it locks out, measured 2026-08-14 in scoring/scorecard.py:

    stored by SQLAlchemy's DateTime processor : '2026-08-06 14:30:00.000000'
    bound from .isoformat()                   : '2026-08-06T12:00:00+00:00'

Binding a string makes SQLAlchemy type the parameter as String and skip the
DateTime bind processor entirely, so SQLite compares those two as text. A space
sorts before a T, so every row on the same UTC date as the cutoff reads as older
than it regardless of the clock. Both directions were wrong at once:

  detected_at <= ack_cutoff   an anomaly 45.5h old, inside a 48h response
                              window, counted as OVERDUE
  detected_at >= cutoff       an anomaly 29d18h old, inside a 30 day lookback,
                              VANISHED from total_anomalies_30d, and the
                              dimension reported "No anomalies detected in the
                              last 30 days" and handed out a flat 80

Both fed the customer-facing scorecard and the tickets create_scorecard_tickets
opens from it.

Why a ratchet rather than two regression tests. PostgreSQL casts the literal and
gets this right, so the defect exists ONLY on SQLite, which is the default local
install and therefore almost every user. Anyone developing against the shared
Postgres deployment cannot reproduce it, and the code reads fine: comparing a
timestamp column to an ISO 8601 string looks correct to anyone who has not been
bitten. That combination, invisible on one backend and plausible on sight, is how
it lasted, and it is why a rule beats a memory here.
"""
from __future__ import annotations

import ast
import pathlib

import pytest
from sqlalchemy import DateTime

from finops.storage import db

SRC = pathlib.Path(__file__).resolve().parents[1] / "src" / "finops"


def _datetime_column_names() -> set[str]:
    """Read the column types off the real metadata, not a hardcoded list.

    A column added as DateTime tomorrow is covered without touching this file,
    which is the difference between a ratchet and a snapshot.
    """
    return {
        c.name
        for table in db.metadata.tables.values()
        for c in table.columns
        if isinstance(c.type, DateTime)
    }


def _string_bound_comparisons() -> list[tuple[str, int, str]]:
    """Every comparison that puts an ISO string next to a DateTime column."""
    dt_cols = _datetime_column_names()
    offenders: list[tuple[str, int, str]] = []

    for path in sorted(SRC.rglob("*.py")):
        try:
            tree = ast.parse(path.read_text())
        except SyntaxError:            # pragma: no cover - defensive
            continue
        for node in ast.walk(tree):
            if not isinstance(node, ast.Compare):
                continue
            rendered = ast.unparse(node)
            if "isoformat()" not in rendered:
                continue
            # `.c.<name>` is how a Core column is referenced. Requiring the
            # column spelling keeps this off comparisons between two plain
            # datetimes, which are fine and common.
            if not any(f".c.{name}" in rendered for name in dt_cols):
                continue
            offenders.append((
                str(path.relative_to(SRC)), node.lineno, rendered[:120],
            ))
    return offenders


def test_no_query_binds_a_string_to_a_datetime_column():
    offenders = _string_bound_comparisons()
    assert not offenders, (
        "these compare an ISO string against a DateTime column, which SQLAlchemy "
        "binds as String and SQLite then compares lexicographically against its "
        "own 'YYYY-MM-DD HH:MM:SS.ffffff' format. Pass the datetime object:\n  "
        + "\n  ".join(f"{f}:{ln}  {s}" for f, ln, s in offenders)
    )


def test_the_detector_can_actually_find_this():
    """The ratchet must not be silently finding nothing.

    Without this, deleting the DateTime lookup, or the .c. requirement, or
    pointing SRC at an empty directory, all leave the test above green forever
    while asserting nothing. A detector that cannot fail is not a guard, it is a
    comment that runs.
    """
    dt_cols = _datetime_column_names()
    assert "detected_at" in dt_cols and len(dt_cols) >= 5, (
        f"only found {sorted(dt_cols)}; the metadata scan is not resolving "
        f"DateTime columns, so the check above matches nothing"
    )

    planted = ast.parse(
        "select(func.count()).where(anomalies.c.detected_at >= cutoff.isoformat())")
    found = [
        n for n in ast.walk(planted)
        if isinstance(n, ast.Compare)
        and "isoformat()" in ast.unparse(n)
        and any(f".c.{name}" in ast.unparse(n) for name in dt_cols)
    ]
    assert found, (
        "the AST pattern does not match the exact expression this ratchet exists "
        "to ban, so it would not have caught the original bug either"
    )


@pytest.mark.parametrize("op", ["<=", ">="])
def test_the_bug_this_bans_is_real_on_sqlite(tmp_path, monkeypatch, op):
    """Proof, on a real engine, that the two bind styles disagree.

    A rule with no demonstration behind it gets relaxed by the next person who
    finds it inconvenient. This runs both comparisons against the same row in a
    real SQLite database and shows they return different answers, so the cost of
    breaking the rule is visible rather than asserted.
    """
    from datetime import datetime, timedelta, timezone

    from sqlalchemy import func, select

    monkeypatch.setenv("FINOPS_DB_PATH", str(tmp_path / "t.db"))
    monkeypatch.setenv("FINOPS_DATA_DIR", str(tmp_path / "d"))
    db._ENGINE, db._DATA_DIR = None, None
    try:
        engine = db.get_engine()
        now = datetime(2026, 8, 8, 12, 0, 0, tzinfo=timezone.utc)
        detected = now - timedelta(hours=45, minutes=30)   # inside a 48h window
        with engine.begin() as conn:
            conn.execute(db.anomalies.insert().values(
                provider="aws", service="EC2", account_id="1",
                detected_at=detected, snapshot_date=detected.date().isoformat(),
                severity="medium", direction="spike", pct_change=1.0, z_score=1.0,
                baseline_mean=1.0, current_amount=1.0,
                acknowledged=False, notified=False))

        cutoff = now - timedelta(hours=48)
        col = db.anomalies.c.detected_at
        clause_dt = col <= cutoff if op == "<=" else col >= cutoff
        clause_str = col <= cutoff.isoformat() if op == "<=" else col >= cutoff.isoformat()

        with engine.connect() as conn:
            as_datetime = conn.execute(select(func.count()).where(clause_dt)).scalar()
            as_string = conn.execute(select(func.count()).where(clause_str)).scalar()

        assert as_datetime != as_string, (
            f"binding a datetime and binding its isoformat() agreed for {op!r}, "
            f"so this test no longer demonstrates anything. Either SQLAlchemy "
            f"changed its bind handling or the row no longer straddles the cutoff"
        )
    finally:
        db._ENGINE, db._DATA_DIR = None, None
