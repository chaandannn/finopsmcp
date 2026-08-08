"""Dashboards: the named, addressable container a saved card lives in.

Saved cards used to pin into one flat list per owner, which gives an install
exactly one wall of charts. There was no way to keep "Platform weekly" apart
from "CFO monthly review", no way to name a set, no way to point someone at one,
and no way to say "this whole set, but only team=platform, for June 7 to
September 2".

A dashboard is that container:

  - `slug` is its identity in a URL and survives renames, so a link a colleague
    saved in January still resolves after the title changes in March.
  - `filters` + `date_range` are its SCOPE, applied on top of every card it
    holds via SliceSpec.with_scope. Scope lives here rather than being edited
    into each card, so narrowing twenty charts is one change and removing it
    restores exactly what the cards said before. A card's own filters are never
    dropped, only ANDed with.

Propose-only and read-only like everything else: this stores and composes cost
QUERIES. It never touches a cloud.
"""
from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select

from ..storage.db import dashboard_views, dashboards, get_engine
from .spec import DateRange, FilterClause, SliceSpecError, parse_date_range

_SLUG_RE = re.compile(r"[^a-z0-9]+")
MAX_DASHBOARDS = 500        # a ceiling, not a product limit: keeps a runaway
                            # creation loop from filling the box's disk.


def _now() -> datetime:
    return datetime.now(timezone.utc)


def slugify(title: str, taken: set[str] | None = None) -> str:
    """A URL-safe identity for a title, unique against `taken`.

    Falls back to "dashboard" for a title that is entirely punctuation or
    non-Latin, because an empty slug would collide with every other empty one
    and make two dashboards unreachable rather than one.
    """
    base = _SLUG_RE.sub("-", (title or "").strip().lower()).strip("-")[:64] or "dashboard"
    if taken is None or base not in taken:
        return base
    n = 2
    while f"{base}-{n}" in taken:
        n += 1
    return f"{base}-{n}"


def _row_to_dict(r) -> dict[str, Any]:
    try:
        filters = json.loads(r.filters or "[]")
    except Exception:
        filters = []
    try:
        dr = json.loads(r.date_range or "{}")
    except Exception:
        dr = {}
    return {
        "id": r.id, "slug": r.slug, "title": r.title,
        "description": r.description or "",
        "owner": r.owner, "scope": r.scope,
        "filters": filters, "date_range": dr,
        "position": r.position,
        "created_at": r.created_at.isoformat() if r.created_at else None,
        "updated_at": r.updated_at.isoformat() if r.updated_at else None,
        "created_by": r.created_by or "",
    }


def list_dashboards(owner: str = "instance") -> list[dict[str, Any]]:
    """Every dashboard this owner can open, with its card count.

    The count is what makes an index page useful at fifty dashboards: an empty
    one is visibly empty instead of looking identical to a full one.
    """
    with get_engine().connect() as conn:
        rows = conn.execute(
            select(dashboards).where(dashboards.c.owner == owner)
            .order_by(dashboards.c.position, dashboards.c.id)
        ).fetchall()
        counts: dict[int, int] = {}
        for did, in conn.execute(
            select(dashboard_views.c.dashboard_id)
            .where(dashboard_views.c.dashboard_id.isnot(None))
        ).fetchall():
            counts[did] = counts.get(did, 0) + 1
    out = []
    for r in rows:
        d = _row_to_dict(r)
        d["card_count"] = counts.get(r.id, 0)
        out.append(d)
    return out


def get_dashboard(slug: str, owner: str = "instance") -> dict[str, Any] | None:
    with get_engine().connect() as conn:
        r = conn.execute(
            select(dashboards).where(
                dashboards.c.slug == slug, dashboards.c.owner == owner)
        ).fetchone()
    return _row_to_dict(r) if r else None


def create_dashboard(title: str, description: str = "", owner: str = "instance",
                     scope: str = "instance", filters: list[dict] | None = None,
                     date_range: dict | None = None,
                     created_by: str = "") -> dict[str, Any]:
    """Create a dashboard. Scope is validated here, at the boundary.

    An invalid filter or a reversed date range is rejected on the way in rather
    than stored and blown up later on every card render.
    """
    if not (title or "").strip():
        raise SliceSpecError("a dashboard needs a title")
    clauses = validate_filters(filters)
    dr = parse_date_range(date_range)

    now = _now()
    with get_engine().begin() as conn:
        existing = {s for (s,) in conn.execute(select(dashboards.c.slug)).fetchall()}
        if len(existing) >= MAX_DASHBOARDS:
            raise SliceSpecError(f"this install already has {MAX_DASHBOARDS} dashboards")
        slug = slugify(title, existing)
        pos = conn.execute(select(dashboards.c.id)).fetchall()
        conn.execute(dashboards.insert().values(
            slug=slug, title=title.strip()[:256], description=(description or "")[:2000],
            owner=owner, scope=scope if scope in ("me", "instance") else "instance",
            filters=json.dumps([c.to_dict() for c in clauses]),
            date_range=json.dumps(dr.to_dict() if dr else {}),
            position=len(pos), created_at=now, updated_at=now,
            created_by=created_by or "",
        ))
    return get_dashboard(slug, owner)


def update_dashboard(slug: str, owner: str = "instance", *, title: str | None = None,
                     description: str | None = None, filters: list[dict] | None = None,
                     date_range: dict | None = None) -> dict[str, Any] | None:
    """Rename or re-scope a dashboard. The slug never changes.

    Renaming deliberately does not re-slug: a link someone saved has to keep
    resolving, and silently breaking shared URLs on a rename is the kind of thing
    nobody notices until a board meeting.
    """
    values: dict[str, Any] = {"updated_at": _now()}
    if title is not None:
        if not title.strip():
            raise SliceSpecError("a dashboard needs a title")
        values["title"] = title.strip()[:256]
    if description is not None:
        values["description"] = description[:2000]
    if filters is not None:
        values["filters"] = json.dumps([c.to_dict() for c in validate_filters(filters)])
    if date_range is not None:
        dr = parse_date_range(date_range)
        values["date_range"] = json.dumps(dr.to_dict() if dr else {})

    with get_engine().begin() as conn:
        res = conn.execute(dashboards.update().where(
            dashboards.c.slug == slug, dashboards.c.owner == owner).values(**values))
        if not res.rowcount:
            return None
    return get_dashboard(slug, owner)


def delete_dashboard(slug: str, owner: str = "instance") -> bool:
    """Delete a dashboard. Its cards are DETACHED, never deleted.

    Someone clearing out a dashboard is tidying a container; the charts inside it
    were separate work. Orphaning them back to the overview is recoverable, and
    deleting them is not.
    """
    d = get_dashboard(slug, owner)
    if d is None:
        return False
    with get_engine().begin() as conn:
        conn.execute(dashboard_views.update()
                     .where(dashboard_views.c.dashboard_id == d["id"])
                     .values(dashboard_id=None))
        conn.execute(dashboards.delete().where(dashboards.c.id == d["id"]))
    return True


def validate_filters(raw: list[dict] | None) -> list[FilterClause]:
    """Validate dashboard-level filters through the same parser cards use."""
    from .spec import _parse_clause
    if not raw:
        return []
    if not isinstance(raw, list):
        raise SliceSpecError("filters must be a list of {dimension, op, values}")
    return [_parse_clause(c, "filter") for c in raw]


def dashboard_scope(d: dict) -> tuple[list[FilterClause], DateRange | None]:
    """A stored dashboard's scope, as the objects SliceSpec.with_scope wants."""
    if not d:
        return [], None
    clauses = validate_filters(d.get("filters") or [])
    dr = parse_date_range(d.get("date_range") or None)
    return clauses, dr


def cards_for(dashboard_id: int | None, owner: str = "instance") -> list[dict[str, Any]]:
    """The saved cards on a dashboard, in position order.

    `None` returns the unfiled cards (pinned before dashboards existed, or since
    detached), which is what the overview shows.
    """
    with get_engine().connect() as conn:
        q = select(dashboard_views).where(dashboard_views.c.owner == owner)
        q = q.where(dashboard_views.c.dashboard_id == dashboard_id
                    if dashboard_id is not None
                    else dashboard_views.c.dashboard_id.is_(None))
        rows = conn.execute(q.order_by(dashboard_views.c.position,
                                       dashboard_views.c.id)).fetchall()
    out = []
    for r in rows:
        try:
            spec = json.loads(r.slice_spec or "{}")
        except Exception:
            spec = {}
        try:
            card = json.loads(r.card_spec or "{}")
        except Exception:
            card = {}
        out.append({"id": r.id, "title": r.title, "template": r.template,
                    "slice": spec, "card": card, "position": r.position})
    return out


def assign_card(view_id: int, dashboard_id: int | None, owner: str = "instance") -> bool:
    """Move a saved card onto a dashboard, or off every dashboard (None)."""
    with get_engine().begin() as conn:
        res = conn.execute(dashboard_views.update().where(
            dashboard_views.c.id == view_id,
            dashboard_views.c.owner == owner,
        ).values(dashboard_id=dashboard_id))
    return bool(res.rowcount)
