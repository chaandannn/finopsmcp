"""The month-over-month views on the 1st of the month.

Month-to-date on the 1st is an empty date range. Cost providers reject it, the
fetch errors, and the caller's `.get("total_usd", 0.0)` turns that error into a
clean-looking $0.00. So one day in thirty, `get_view("mom")` told people every
provider's spend had dropped to zero, a -100% change, and `get_view("by_service")`
returned an empty service list. Both look like answers, which is what makes them
worse than an error.

These tests drive the real views with the clock pinned, so the boundary is
covered on any day of the year, and they assert against the windows the views
actually ASK FOR, not just against the helper.
"""
from __future__ import annotations

from datetime import date
from types import SimpleNamespace

import pytest

from finops import server as _srv
from finops.tools import meta


def _pin_today(monkeypatch, day: date):
    monkeypatch.setattr(_srv, "date", SimpleNamespace(today=lambda: day))


def _stub_costs(monkeypatch, per_window: dict[tuple[str, str], float]):
    """Answer only for the exact windows given; anything else is an empty range
    the provider would have rejected, and surfaces here as a $0 with an error,
    exactly like the real failure did."""
    asked: list[tuple[str, str]] = []

    async def _gather(active, start, end, granularity="MONTHLY", service_filter=None):
        key = (start.isoformat(), end.isoformat())
        asked.append(key)
        if key not in per_window:
            return 0.0, {"aws": {"error": "start date must be before end date"}}, {}
        total = per_window[key]
        return total, {"aws": {"total_usd": total}}, {"Amazon EC2": total}

    async def _active(subset=None):
        return {"aws": object()}

    monkeypatch.setattr(_srv, "_gather_costs", _gather)
    monkeypatch.setattr(_srv, "_active", _active)
    return asked


# ── the window builder ────────────────────────────────────────────────────────

def test_month_pair_never_builds_an_empty_window():
    from datetime import timedelta

    day = date(2026, 1, 1)
    for _ in range(400):  # a full year plus change, across both year boundaries
        cur_start, cur_end, prev_start, prev_end, on_first = meta._month_pair(day)
        assert cur_start < cur_end, f"empty current window on {day}"
        assert prev_start < prev_end, f"empty previous window on {day}"
        assert prev_end < cur_start, f"windows overlap on {day}"
        assert on_first is (day.day == 1)
        day += timedelta(days=1)


def test_month_pair_on_the_first_compares_the_two_closed_months():
    assert meta._month_pair(date(2026, 8, 1)) == (
        date(2026, 7, 1), date(2026, 7, 31), date(2026, 6, 1), date(2026, 6, 30), True
    )
    # Year rollover.
    assert meta._month_pair(date(2026, 1, 1)) == (
        date(2025, 12, 1), date(2025, 12, 31), date(2025, 11, 1), date(2025, 11, 30), True
    )


def test_month_pair_mid_month_is_month_to_date_vs_last_month():
    assert meta._month_pair(date(2026, 8, 14)) == (
        date(2026, 8, 1), date(2026, 8, 14), date(2026, 7, 1), date(2026, 7, 31), False
    )


# ── the views themselves ──────────────────────────────────────────────────────

async def test_mom_on_the_first_reports_real_dollars_not_a_100pct_collapse(monkeypatch):
    _pin_today(monkeypatch, date(2026, 8, 1))
    _stub_costs(monkeypatch, {
        ("2026-07-01", "2026-07-31"): 48210.0,
        ("2026-06-01", "2026-06-30"): 44000.0,
    })
    out = await meta.get_view("mom")

    assert out["this_month"]["total"] == _srv._fmt_usd(48210.0)
    assert out["total_change"] != "-100.0%"
    assert out["by_provider"][0]["this_month"] != _srv._fmt_usd(0.0)
    assert "1st" in out["note"]           # says which months these actually are
    assert out["this_month"]["period"] == "2026-07-01 to 2026-07-31"


async def test_mom_mid_month_is_unchanged_and_carries_no_note(monkeypatch):
    _pin_today(monkeypatch, date(2026, 8, 14))
    _stub_costs(monkeypatch, {
        ("2026-08-01", "2026-08-14"): 20000.0,
        ("2026-07-01", "2026-07-31"): 48210.0,
    })
    out = await meta.get_view("mom")

    assert out["this_month"]["period"] == "2026-08-01 to 2026-08-14"
    assert out["last_month"]["period"] == "2026-07-01 to 2026-07-31"
    assert "note" not in out


async def test_by_service_on_the_first_still_names_services(monkeypatch):
    _pin_today(monkeypatch, date(2026, 8, 1))
    _stub_costs(monkeypatch, {
        ("2026-07-01", "2026-07-31"): 48210.0,
        ("2026-06-01", "2026-06-30"): 44000.0,
    })
    out = await meta.get_view("by_service")

    assert out["services"], "by_service returned nothing on the 1st"
    assert out["services"][0]["service"] == "Amazon EC2"
    assert out["period"] == "2026-07-01 to 2026-07-31"
    assert "1st" in out["note"]


@pytest.mark.parametrize("view", ["mom", "by_service"])
async def test_views_never_query_an_empty_range_on_the_first(monkeypatch, view):
    # The load-bearing property: whatever the labelling, the view must never ask
    # a cost provider for a range that starts and ends on the same day.
    _pin_today(monkeypatch, date(2026, 8, 1))
    asked = _stub_costs(monkeypatch, {
        ("2026-07-01", "2026-07-31"): 48210.0,
        ("2026-06-01", "2026-06-30"): 44000.0,
    })
    await meta.get_view(view)

    assert asked, "the view made no cost query at all"
    assert all(start != end for start, end in asked), asked
