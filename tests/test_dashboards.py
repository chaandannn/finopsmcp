"""Dashboards as containers, and the scope they impose on their cards.

The load-bearing behaviours:
  - a fixed date range means the same window tomorrow (a saved quarter must not
    drift), while a rolling one keeps meaning "the last N days"
  - dashboard scope NARROWS its cards and never widens them: a card's own
    filters survive, the dashboard's are ANDed on top
  - a slug survives a rename, so a shared link keeps resolving
  - deleting a dashboard detaches its cards instead of destroying them
"""
from __future__ import annotations

import tempfile
from pathlib import Path

import pytest

from finops.slice.spec import (
    DateRange, FilterClause, SliceSpec, SliceSpecError, parse_date_range, parse_spec,
)


@pytest.fixture
def db(monkeypatch):
    td = tempfile.TemporaryDirectory()
    monkeypatch.setenv("FINOPS_DB_PATH", str(Path(td.name) / "t.db"))
    import finops.storage.db as db_mod
    db_mod._ENGINE = None
    yield db_mod
    db_mod._ENGINE = None
    td.cleanup()


# ── date ranges: the thing that did not exist before ─────────────────────────

def test_a_fixed_window_round_trips_exactly():
    dr = parse_date_range({"start": "2026-06-07", "end": "2026-09-02"})
    assert dr.is_fixed
    assert dr.to_dict() == {"start": "2026-06-07", "end": "2026-09-02"}


def test_end_is_inclusive_and_the_off_by_one_lives_in_one_place():
    """A human picking Sep 2 means through Sep 2. APIs with an exclusive upper
    bound get Sep 3, computed here rather than in every caller."""
    dr = parse_date_range({"start": "2026-06-07", "end": "2026-09-02"})
    assert dr.end_exclusive() == "2026-09-03"
    assert parse_date_range({"start": "2026-02-28", "end": "2026-02-28"}).end_exclusive() == "2026-03-01"


def test_a_rolling_window_is_not_a_fixed_one():
    dr = parse_date_range({"days": 30})
    assert dr.days == 30 and not dr.is_fixed and dr.end_exclusive() is None


def test_a_reversed_range_is_refused_not_swapped():
    """Silently swapping hands back a chart that disagrees with what was asked."""
    with pytest.raises(SliceSpecError) as e:
        parse_date_range({"start": "2026-09-02", "end": "2026-06-07"})
    assert "before start" in str(e.value)


@pytest.mark.parametrize("bad", [
    {"start": "2026-06-07"},                      # half a window
    {"end": "2026-09-02"},
    {"start": "06/07/2026", "end": "09/02/2026"},  # not ISO
    {"start": "2026-02-30", "end": "2026-03-01"},  # not a real date
    {"days": 0}, {"days": -5}, {"days": 4000},
    {"days": 30, "start": "2026-06-07", "end": "2026-09-02"},  # both forms
])
def test_malformed_ranges_are_rejected(bad):
    with pytest.raises(SliceSpecError):
        parse_date_range(bad)


def test_no_range_stays_none_so_callers_keep_their_default():
    assert parse_date_range(None) is None and parse_date_range({}) is None
    assert parse_spec({"dimensions": ["ServiceName"]}).date_range is None


def test_a_spec_carries_its_range_through_to_dict():
    s = parse_spec({"dimensions": ["ServiceName"],
                    "date_range": {"start": "2026-06-07", "end": "2026-09-02"}})
    assert s.date_range.is_fixed
    assert s.to_dict()["date_range"] == {"start": "2026-06-07", "end": "2026-09-02"}


# ── scope: narrows, never widens ─────────────────────────────────────────────

def _card():
    return parse_spec({"dimensions": ["ServiceName"],
                       "filters": [{"dimension": "ProviderName", "op": "eq", "values": ["AWS"]}]})


def test_dashboard_filters_are_added_to_the_cards_own():
    scoped = _card().with_scope(
        filters=[FilterClause(dimension="Tags[team]", op="eq", values=["platform"])])
    dims = [(c.dimension, tuple(c.values)) for c in scoped.filters]
    assert ("ProviderName", ("AWS",)) in dims, "the card's own filter was dropped"
    assert ("Tags[team]", ("platform",)) in dims


def test_scope_never_mutates_the_saved_card():
    card = _card()
    card.with_scope(filters=[FilterClause("Tags[team]", "eq", ["platform"])],
                    date_range=DateRange(days=7))
    assert len(card.filters) == 1, "the stored card was mutated"
    assert card.date_range is None


def test_a_dashboard_range_overrides_the_cards_range():
    """The window is what a viewer explicitly picks, so it wins."""
    card = parse_spec({"dimensions": ["ServiceName"], "date_range": {"days": 7}})
    scoped = card.with_scope(date_range=DateRange(start="2026-06-07", end="2026-09-02"))
    assert scoped.date_range.is_fixed and scoped.date_range.start == "2026-06-07"


def test_no_dashboard_range_leaves_the_cards_own_alone():
    card = parse_spec({"dimensions": ["ServiceName"], "date_range": {"days": 7}})
    assert card.with_scope(filters=[]).date_range.days == 7


def test_usage_type_and_tags_are_scopeable():
    """Both were already valid dimensions; this pins that a dashboard can filter
    on them, which is the breakdown the UI needs to expose."""
    scoped = _card().with_scope(filters=[
        FilterClause("usage_type", "contains", ["DataTransfer"]),
        FilterClause("Tags[env]", "eq", ["prod"]),
    ])
    assert {c.dimension for c in scoped.filters} == {"ProviderName", "usage_type", "Tags[env]"}


# ── the container ────────────────────────────────────────────────────────────

def test_create_list_and_open_by_slug(db):
    from finops.slice.dashboards import create_dashboard, get_dashboard, list_dashboards
    d = create_dashboard("CFO monthly review", description="What finance asks for")
    assert d["slug"] == "cfo-monthly-review"
    assert get_dashboard("cfo-monthly-review")["title"] == "CFO monthly review"
    listed = list_dashboards()
    assert len(listed) == 1 and listed[0]["card_count"] == 0


def test_slugs_do_not_collide(db):
    from finops.slice.dashboards import create_dashboard
    a = create_dashboard("Platform weekly")
    b = create_dashboard("Platform weekly")
    assert a["slug"] == "platform-weekly" and b["slug"] == "platform-weekly-2"


def test_a_rename_keeps_the_slug_so_shared_links_survive(db):
    from finops.slice.dashboards import create_dashboard, get_dashboard, update_dashboard
    create_dashboard("Platform weekly")
    out = update_dashboard("platform-weekly", title="Platform review")
    assert out["slug"] == "platform-weekly", "renaming broke every saved link"
    assert get_dashboard("platform-weekly")["title"] == "Platform review"


def test_a_title_with_no_latin_characters_still_gets_a_usable_slug(db):
    """An empty slug would collide with every other empty one and make two
    dashboards unreachable instead of one."""
    from finops.slice.dashboards import create_dashboard
    assert create_dashboard("###")["slug"] == "dashboard"
    assert create_dashboard("成本看板")["slug"] == "dashboard-2"


def test_scope_is_validated_on_the_way_in(db):
    from finops.slice.dashboards import create_dashboard
    with pytest.raises(SliceSpecError):
        create_dashboard("Bad", filters=[{"dimension": "NotADimension", "op": "eq", "values": ["x"]}])
    with pytest.raises(SliceSpecError):
        create_dashboard("Bad", date_range={"start": "2026-09-02", "end": "2026-06-07"})


def test_stored_scope_comes_back_as_usable_objects(db):
    from finops.slice.dashboards import create_dashboard, dashboard_scope, get_dashboard
    create_dashboard("Platform", filters=[{"dimension": "Tags[team]", "op": "eq", "values": ["platform"]}],
                     date_range={"start": "2026-06-07", "end": "2026-09-02"})
    clauses, dr = dashboard_scope(get_dashboard("platform"))
    assert clauses[0].dimension == "Tags[team]" and dr.end == "2026-09-02"
    scoped = _card().with_scope(clauses, dr)
    assert scoped.date_range.is_fixed
    assert {c.dimension for c in scoped.filters} == {"ProviderName", "Tags[team]"}


# ── cards belong to a dashboard, and survive its deletion ────────────────────

def _pin(title="Card", dashboard_id=None, pos=0):
    from datetime import datetime, timezone
    from finops.storage.db import dashboard_views, get_engine
    with get_engine().begin() as conn:
        r = conn.execute(dashboard_views.insert().values(
            dashboard_id=dashboard_id, owner="instance", scope="instance",
            title=title, template="bar", slice_spec="{}", card_spec="{}",
            position=pos, created_at=datetime.now(timezone.utc),
            updated_at=datetime.now(timezone.utc)))
    return r.inserted_primary_key[0]


def test_cards_are_listed_per_dashboard_and_unfiled_stay_unfiled(db):
    from finops.slice.dashboards import assign_card, cards_for, create_dashboard
    d = create_dashboard("Platform weekly")
    legacy = _pin("pinned before dashboards existed")
    mine = _pin("on the dashboard")
    assign_card(mine, d["id"])
    assert [c["title"] for c in cards_for(d["id"])] == ["on the dashboard"]
    assert [c["title"] for c in cards_for(None)] == ["pinned before dashboards existed"]
    assert legacy  # the pre-existing card is still reachable


def test_deleting_a_dashboard_detaches_its_cards_rather_than_deleting_them(db):
    from finops.slice.dashboards import (assign_card, cards_for, create_dashboard,
                                         delete_dashboard, get_dashboard)
    d = create_dashboard("Temp")
    cid = _pin("expensive to rebuild")
    assign_card(cid, d["id"])
    assert delete_dashboard("temp") is True
    assert get_dashboard("temp") is None
    assert [c["title"] for c in cards_for(None)] == ["expensive to rebuild"]


def test_deleting_a_missing_dashboard_is_false_not_a_crash(db):
    from finops.slice.dashboards import delete_dashboard, update_dashboard
    assert delete_dashboard("nope") is False
    assert update_dashboard("nope", title="x") is None
