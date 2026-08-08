"""
SliceSpec: the one query DSL the agent compiles natural language into.

A SliceSpec groups, filters, and excludes over any FOCUS dimension. The dimension
set is a CLOSED registry so the agent cannot invent columns, and parse_spec()
validates every agent-supplied field before it reaches the engine. The result of a
run carries a CardSpec, the portable contract every surface (web, Slack, Claude)
renders and can pin.
"""
from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field

# ── Closed registries (the agent's allowlist) ────────────────────────────────
# Categorical FOCUS dimensions a slice may group or filter by. "date" (time) and
# "Tags[<key>]" (an arbitrary tag) are handled specially in the engine.
FOCUS_DIMENSIONS: set[str] = {
    "ServiceName", "ServiceCategory", "ProviderName",
    "RegionId", "RegionName", "SubAccountId", "SubAccountName",
    "ResourceId", "ResourceName", "ResourceType",
    "ChargeCategory", "ChargeDescription",
    "CommitmentDiscountId", "CommitmentDiscountType",
}
# The time pseudo-dimension and tag pseudo-dimensions are also groupable/filterable.
TIME_DIMENSION = "date"

# CUR-only dimensions: line-item granularity the normalized FOCUS feed can't reach.
# Using any of these routes the slice to the CUR/Athena pushdown (requires CUR set up).
CUR_DIMENSIONS: set[str] = {"usage_type", "instance_type", "resource_id"}

METRICS: set[str] = {"BilledCost", "EffectiveCost", "ListCost"}
GRANULARITIES: set[str] = {"TOTAL", "DAILY", "MONTHLY"}
FILTER_OPS: set[str] = {"eq", "in", "neq", "not_in", "contains", "regex"}
CARD_TEMPLATES: set[str] = {"line", "bar", "stacked_bar", "table", "kpi", "heatmap"}

DEFAULT_METRIC = "EffectiveCost"
MAX_LIMIT = 500


class SliceSpecError(ValueError):
    """Raised when an agent-supplied slice is malformed or uses unknown dimensions."""


def is_tag_dim(dim: str) -> bool:
    return dim.startswith("Tags[") and dim.endswith("]") and len(dim) > 6


def is_valid_dimension(dim: str) -> bool:
    return (dim == TIME_DIMENSION or dim in FOCUS_DIMENSIONS
            or dim in CUR_DIMENSIONS or is_tag_dim(dim))


def needs_cur(spec) -> bool:
    """True if a slice uses a CUR-only dimension anywhere (group, filter, exclude)."""
    dims = set(spec.dimensions)
    for c in list(spec.filters) + list(spec.exclusions):
        dims.add(c.dimension)
    return bool(dims & CUR_DIMENSIONS)


@dataclass
class FilterClause:
    dimension: str
    op: str               # one of FILTER_OPS
    values: list[str]

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class DateRange:
    """An explicit reporting window.

    Two forms, and only one is set at a time. `days` is a rolling lookback ("the
    last 30 days"), which is what a pinned card wants: it should keep meaning the
    last 30 days a month from now. `start`/`end` is a fixed window ("2026-06-07
    to 2026-09-02"), which is what a human comparing two quarters wants, and it
    must NOT drift when they reopen it tomorrow.

    Before this, a slice carried no window at all: granularity only. Every caller
    applied its own lookback outside the spec, so a saved card could not express
    a fixed window and a dashboard could not impose one on its cards.

    Dates are ISO YYYY-MM-DD and `end` is INCLUSIVE, because a human picking
    Sep 2 means through Sep 2. Callers converting to a half-open API window add
    a day on the way out; `end_exclusive()` does it for them so the off-by-one
    lives in one place.
    """
    days: int | None = None
    start: str | None = None        # ISO date, inclusive
    end: str | None = None          # ISO date, inclusive

    @property
    def is_fixed(self) -> bool:
        return bool(self.start and self.end)

    def end_exclusive(self) -> str | None:
        """`end` + 1 day, for APIs whose upper bound is exclusive (CE, BigQuery)."""
        if not self.end:
            return None
        from datetime import date, timedelta
        y, m, d = (int(p) for p in self.end.split("-"))
        return (date(y, m, d) + timedelta(days=1)).isoformat()

    def to_dict(self) -> dict:
        return {k: v for k, v in asdict(self).items() if v is not None}


@dataclass
class SliceSpec:
    """One cost query. The pure engine runs this over a list of FocusRecords."""
    dimensions: list[str] = field(default_factory=list)   # group-by, e.g. ["RegionId"] or ["date","ServiceName"]
    filters: list[FilterClause] = field(default_factory=list)
    exclusions: list[FilterClause] = field(default_factory=list)
    metric: str = DEFAULT_METRIC
    granularity: str = "TOTAL"      # only meaningful when "date" is in dimensions
    order_by: str = "metric"        # "metric" (desc) or a dimension name (asc)
    limit: int = 50
    date_range: DateRange | None = None   # None = the caller's default lookback

    def to_dict(self) -> dict:
        d = asdict(self)
        if self.date_range is None:
            d.pop("date_range", None)
        else:
            d["date_range"] = self.date_range.to_dict()
        return d

    def with_scope(self, filters: list[FilterClause] | None = None,
                   date_range: "DateRange | None" = None) -> "SliceSpec":
        """This slice narrowed by a dashboard's scope.

        Dashboard filters are ANDed on top of the card's own, never replacing
        them: a card that already says "prod only" must not widen because the
        dashboard filters by team. A dashboard date range overrides the card's,
        because the window is the one thing a viewer is explicitly choosing.

        Returns a new spec; the stored card is never mutated.
        """
        merged = list(self.filters) + [c for c in (filters or [])]
        return SliceSpec(
            dimensions=list(self.dimensions),
            filters=merged,
            exclusions=list(self.exclusions),
            metric=self.metric,
            granularity=self.granularity,
            order_by=self.order_by,
            limit=self.limit,
            date_range=date_range or self.date_range,
        )


@dataclass
class SliceResult:
    rows: list[dict]                # [{<dim>: value, ..., "metric": float}]
    total: float
    metric: str
    dimensions: list[str]
    record_count: int = 0
    currency: str = "USD"
    truncated: bool = False         # True if more groups existed than the limit

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class CardSpec:
    """How to render a slice result, and the slice that regenerates it (for pinning)."""
    title: str
    template: str                   # one of CARD_TEMPLATES
    metric: str
    dimensions: list[str]
    slice: dict                     # the SliceSpec.to_dict() that produced this, so it can be re-run
    refresh_secs: int = 43200       # 12h, matches the cost cache TTL

    def to_dict(self) -> dict:
        return asdict(self)


# ── Validation: agent dict -> SliceSpec ──────────────────────────────────────

def _parse_clause(raw: dict, kind: str) -> FilterClause:
    if not isinstance(raw, dict):
        raise SliceSpecError(f"each {kind} must be an object with dimension/op/values")
    dim = raw.get("dimension")
    op = (raw.get("op") or "in").lower()
    values = raw.get("values")
    if not isinstance(dim, str) or not is_valid_dimension(dim):
        raise SliceSpecError(f"unknown {kind} dimension {dim!r}; allowed: {sorted(FOCUS_DIMENSIONS)} or 'date' or 'Tags[key]'")
    if op not in FILTER_OPS:
        raise SliceSpecError(f"unknown {kind} op {op!r}; allowed: {sorted(FILTER_OPS)}")
    if isinstance(values, (str, int, float)):
        values = [values]
    if not isinstance(values, list) or not values:
        raise SliceSpecError(f"{kind} on {dim!r} needs a non-empty 'values' list")
    return FilterClause(dimension=dim, op=op, values=[str(v) for v in values])


def parse_spec(raw: dict) -> SliceSpec:
    """Validate an agent-supplied slice dict into a SliceSpec, or raise SliceSpecError.

    This is the agent's guardrail: unknown dimensions, bad ops, bad metrics, and
    out-of-range limits are rejected with a message the agent can self-correct on.
    """
    if not isinstance(raw, dict):
        raise SliceSpecError("slice must be an object")

    dims = raw.get("dimensions") or []
    if isinstance(dims, str):
        dims = [dims]
    if not isinstance(dims, list):
        raise SliceSpecError("dimensions must be a list of dimension names")
    for d in dims:
        if not isinstance(d, str) or not is_valid_dimension(d):
            raise SliceSpecError(f"unknown dimension {d!r}; allowed: {sorted(FOCUS_DIMENSIONS)} or 'date' or 'Tags[key]'")
    if len(dims) > 3:
        raise SliceSpecError("at most 3 dimensions per slice (keeps cards legible)")

    metric = raw.get("metric") or DEFAULT_METRIC
    if metric not in METRICS:
        raise SliceSpecError(f"unknown metric {metric!r}; allowed: {sorted(METRICS)}")

    gran = (raw.get("granularity") or "TOTAL").upper()
    if gran not in GRANULARITIES:
        raise SliceSpecError(f"unknown granularity {gran!r}; allowed: {sorted(GRANULARITIES)}")

    order_by = raw.get("order_by") or "metric"
    if order_by != "metric" and order_by not in dims:
        raise SliceSpecError(f"order_by must be 'metric' or one of the dimensions {dims}")

    try:
        limit = int(raw.get("limit") or 50)
    except (TypeError, ValueError):
        raise SliceSpecError("limit must be an integer")
    limit = max(1, min(limit, MAX_LIMIT))

    filters = [_parse_clause(c, "filter") for c in (raw.get("filters") or [])]
    exclusions = [_parse_clause(c, "exclusion") for c in (raw.get("exclusions") or [])]

    return SliceSpec(
        dimensions=dims, filters=filters, exclusions=exclusions,
        metric=metric, granularity=gran, order_by=order_by, limit=limit,
        date_range=parse_date_range(raw.get("date_range")),
    )


_ISO_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
MAX_RANGE_DAYS = 400        # two full year-over-year comparisons, and a ceiling
                            # so one pasted range cannot scan an entire CUR.


def parse_date_range(raw) -> DateRange | None:
    """Validate a date window, or raise SliceSpecError. None means "caller default".

    Rejects a reversed range outright rather than silently swapping the ends: a
    viewer who typed the dates backwards wants to be told, not handed a chart
    that quietly disagrees with what they asked for.
    """
    if raw is None or raw == {}:
        return None
    if not isinstance(raw, dict):
        raise SliceSpecError("date_range must be an object with 'days' or 'start'/'end'")

    days, start, end = raw.get("days"), raw.get("start"), raw.get("end")
    if days is not None and (start or end):
        raise SliceSpecError("date_range takes either 'days' or 'start'/'end', not both")

    if days is not None:
        try:
            days = int(days)
        except (TypeError, ValueError):
            raise SliceSpecError("date_range.days must be an integer")
        if not 1 <= days <= MAX_RANGE_DAYS:
            raise SliceSpecError(f"date_range.days must be 1..{MAX_RANGE_DAYS}")
        return DateRange(days=days)

    if not (start and end):
        raise SliceSpecError("a fixed date_range needs both 'start' and 'end' (YYYY-MM-DD)")
    for label, v in (("start", start), ("end", end)):
        if not isinstance(v, str) or not _ISO_DATE.match(v):
            raise SliceSpecError(f"date_range.{label} must be YYYY-MM-DD, got {v!r}")
    from datetime import date
    try:
        s = date(*(int(p) for p in start.split("-")))
        e = date(*(int(p) for p in end.split("-")))
    except ValueError as exc:
        raise SliceSpecError(f"date_range is not a real calendar date: {exc}")
    if e < s:
        raise SliceSpecError(f"date_range.end ({end}) is before start ({start})")
    if (e - s).days + 1 > MAX_RANGE_DAYS:
        raise SliceSpecError(f"date_range spans more than {MAX_RANGE_DAYS} days")
    return DateRange(start=start, end=end)
