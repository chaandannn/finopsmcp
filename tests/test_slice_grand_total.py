# SPDX-License-Identifier: Apache-2.0
"""A top-N subtotal must never be presented as the bill.

Why this file exists, stated plainly: the CUR slice engine builds SQL that ends
`ORDER BY metric DESC LIMIT n`, then summed the rows Athena handed back and
returned that as `total`. So a slice over ten thousand usage types with a limit
of fifty reported the biggest fifty as the whole spend. The `truncated` flag was
set, but the number itself was already wrong and nothing downstream could tell.

The FOCUS engine, answering the same question, totals over the kept set BEFORE
grouping and limiting, with a comment saying so. Two engines, one question, two
different meanings of the word total. A customer who ran the same slice on a CUR
box and a FOCUS box got different bills and neither result said why.

The fix computes the grand total in the same Athena scan with a window function,
`SUM(SUM(cost)) OVER ()`, which is evaluated over the grouped set before LIMIT
applies. A second query would answer it too, but Athena bills per byte scanned
and a FinOps tool charging you twice to tell you what you spent is a poor look.
`total_shown` is now a separate field, so the subtotal on screen and the real
figure can never be confused for one another again.

No Athena here. `_athena_query` is replaced at the module boundary, which is
where our code ends and AWS begins; everything above it is the real code path.
"""
from __future__ import annotations

from datetime import date

import pytest

from finops.slice.cur_engine import build_cur_sql, run_slice_cur
from finops.slice.spec import SliceSpec

SD, ED = date(2026, 7, 1), date(2026, 7, 31)


def _spec(**kw):
    kw.setdefault("dimensions", ["usage_type"])
    kw.setdefault("metric", "cost")
    kw.setdefault("limit", 3)
    return SliceSpec(**kw)


@pytest.fixture
def athena(monkeypatch):
    """Swap the Athena call for a canned result set, and record the SQL."""
    import finops.slice.cur_engine as ce

    monkeypatch.setattr(ce, "is_configured", lambda: True)
    state = {"sql": None, "rows": []}

    def _fake(sql, *a, **k):
        state["sql"] = sql
        return state["rows"]

    monkeypatch.setattr(ce, "_athena_query", _fake)
    return state


# ── the SQL itself ────────────────────────────────────────────────────────────

def test_grouped_sql_asks_for_the_grand_total():
    sql, _ = build_cur_sql(_spec(), SD, ED)
    assert "OVER ()" in sql, "no window total, so the LIMIT truncates the answer"
    assert "AS grand_total" in sql


def test_the_window_total_survives_the_limit():
    """Ordering matters: the window is computed over the grouped set, then LIMIT
    trims rows. If the LIMIT applied first the window would be a subtotal too."""
    sql, _ = build_cur_sql(_spec(limit=3), SD, ED)
    assert sql.index("OVER ()") < sql.index("LIMIT"), (
        "the grand total must be computed before the LIMIT clause"
    )


def test_ungrouped_sql_does_not_add_a_window():
    """One row, no grouping: that row already IS the total. A window here would
    be noise on the scan and a needless Presto construct."""
    sql, _ = build_cur_sql(_spec(dimensions=[]), SD, ED)
    assert "OVER ()" not in sql


def test_the_limit_is_still_applied():
    """Fixing the total by removing the limit would be a different bug: an
    unbounded slice can return a million rows into the model's context."""
    sql, _ = build_cur_sql(_spec(limit=7), SD, ED)
    assert "LIMIT 7" in sql


# ── what the engine returns ───────────────────────────────────────────────────

def test_total_is_the_grand_total_not_the_visible_sum(athena):
    """The bug, at its narrowest. Three rows shown, a far larger real bill."""
    athena["rows"] = [
        {"d_usage_type": "BoxUsage", "metric": "100.0", "grand_total": "1000.0"},
        {"d_usage_type": "DataTransfer", "metric": "80.0", "grand_total": "1000.0"},
        {"d_usage_type": "EBS", "metric": "60.0", "grand_total": "1000.0"},
    ]
    res = run_slice_cur(_spec(limit=3), SD, ED)
    assert res.total == 1000.0, (
        f"reported ${res.total} as the total when the visible rows are only "
        f"$240 of a $1000 bill"
    )
    assert res.total_shown == 240.0
    assert res.truncated is True


def test_an_untruncated_slice_agrees_with_itself(athena):
    """When nothing was cut, the two figures must match, or the split is noise."""
    athena["rows"] = [
        {"d_usage_type": "BoxUsage", "metric": "100.0", "grand_total": "180.0"},
        {"d_usage_type": "EBS", "metric": "80.0", "grand_total": "180.0"},
    ]
    res = run_slice_cur(_spec(limit=10), SD, ED)
    assert res.total == 180.0
    assert res.total_shown == 180.0
    assert res.truncated is False


def test_an_ungrouped_slice_has_no_grand_total_column(athena):
    """No window was requested, so the engine falls back to the row sum, which
    for a single ungrouped row is the correct total."""
    athena["rows"] = [{"metric": "4321.0"}]
    res = run_slice_cur(_spec(dimensions=[], limit=1), SD, ED)
    assert res.total == 4321.0
    assert res.total_shown == 4321.0


def test_a_missing_grand_total_falls_back_rather_than_crashing(athena):
    """An older Athena workgroup, or a view that drops the column, must degrade
    to the previous behaviour instead of raising into a user's query."""
    athena["rows"] = [{"d_usage_type": "BoxUsage", "metric": "100.0"}]
    res = run_slice_cur(_spec(), SD, ED)
    assert res.total == 100.0
    assert res.total_shown == 100.0


def test_a_junk_grand_total_falls_back(athena):
    """Athena returns strings. A non-numeric one must not become the bill."""
    athena["rows"] = [
        {"d_usage_type": "BoxUsage", "metric": "100.0", "grand_total": "not-a-number"},
    ]
    res = run_slice_cur(_spec(), SD, ED)
    assert res.total == 100.0


def test_no_rows_is_zero_not_an_error(athena):
    athena["rows"] = []
    res = run_slice_cur(_spec(), SD, ED)
    assert res.total == 0.0
    assert res.total_shown == 0.0
    assert res.record_count == 0


def test_a_null_metric_does_not_poison_the_sum(athena):
    athena["rows"] = [
        {"d_usage_type": "A", "metric": None, "grand_total": "50.0"},
        {"d_usage_type": "B", "metric": "10.0", "grand_total": "50.0"},
    ]
    res = run_slice_cur(_spec(), SD, ED)
    assert res.total == 50.0
    assert res.total_shown == 10.0


def test_the_rows_themselves_are_unchanged(athena):
    """The fix is about the headline. Per-row figures must not move."""
    athena["rows"] = [
        {"d_usage_type": "BoxUsage", "metric": "100.5", "grand_total": "999.0"},
    ]
    res = run_slice_cur(_spec(), SD, ED)
    assert res.rows == [{"usage_type": "BoxUsage", "metric": 100.5}]


def test_total_is_never_smaller_than_what_is_shown(athena):
    """A property, not an example: the visible rows are a subset of the slice, so
    their sum cannot exceed the whole. If it does, the two are measuring
    different things and the pairing is meaningless."""
    athena["rows"] = [
        {"d_usage_type": "A", "metric": "10.0", "grand_total": "1000.0"},
        {"d_usage_type": "B", "metric": "5.0", "grand_total": "1000.0"},
    ]
    res = run_slice_cur(_spec(), SD, ED)
    assert res.total >= (res.total_shown or 0)


# ── parity between the two engines ────────────────────────────────────────────

def test_both_engines_expose_the_same_two_fields():
    """The asymmetry is what let this hide: one engine's `total` meant the bill
    and the other's meant the page. Whatever they compute, they must at least
    answer with the same shape."""
    from dataclasses import fields

    from finops.slice.spec import SliceResult

    names = {f.name for f in fields(SliceResult)}
    assert {"total", "total_shown", "truncated"} <= names


def test_the_focus_engine_totals_over_the_unlimited_set():
    """Guards the engine that was already right.

    An earlier version of this test compared line numbers, asserting only that
    `total` was assigned before `groups`. That is a proxy, not the property: a
    mutation that moved the total to the line directly above the grouping still
    satisfied it while computing the total from the wrong set. So this asserts
    what actually matters, which is WHAT the total is summed over. `kept` is
    every record that passed the filters; `rows` and `groups` are what survived
    grouping and the limit, and totalling either of those is precisely how the
    CUR engine came to report a top-N subtotal as the whole bill.
    """
    import ast
    import inspect
    import textwrap

    from finops.slice import engine

    tree = ast.parse(textwrap.dedent(inspect.getsource(engine.run_slice)))
    assign = next(
        (n for n in ast.walk(tree)
         if isinstance(n, ast.Assign)
         and any(isinstance(t, ast.Name) and t.id == "total" for t in n.targets)),
        None,
    )
    assert assign is not None, "no `total = ...` assignment in run_slice"

    names = {n.id for n in ast.walk(assign.value) if isinstance(n, ast.Name)}
    assert "kept" in names, (
        f"the FOCUS total is computed over {sorted(names)} rather than `kept`. "
        f"Totalling the grouped or limited rows is the bug just removed from the "
        f"CUR engine, arriving in the engine that was correct."
    )
    assert not ({"rows", "groups"} & names), (
        f"the FOCUS total references {sorted({'rows','groups'} & names)}, which "
        f"are post-grouping and post-limit, so the headline would be a subtotal"
    )
