"""
run_slice: the one slicing primitive.

Pure and in-memory: it takes a validated SliceSpec and a list of FocusRecords and
returns a SliceResult. No I/O, no cloud calls, so it is fully unit-testable. The
caller (the slice_costs MCP tool) handles fetching the FOCUS records (reusing the
existing connector fan-out) and deriving a CardSpec. Generalizes the single-key
grouping in get_focus_costs to N dimensions + filters + exclusions over any
FOCUS dimension, which is what closes "no arbitrary filter combinations" and
"no commitment/discount filtering".
"""
from __future__ import annotations

import re

from .spec import TIME_DIMENSION, CardSpec, SliceResult, SliceSpec, is_tag_dim

_NONE = "(none)"


def _dim_value(rec, dim: str, granularity: str) -> str:
    """The value of `dim` for one record, as a string group key."""
    if dim == TIME_DIMENSION:
        dt = getattr(rec, "ChargePeriodStart", None)
        if dt is None:
            return _NONE
        return dt.strftime("%Y-%m") if granularity == "MONTHLY" else dt.strftime("%Y-%m-%d")
    if is_tag_dim(dim):
        key = dim[5:-1]
        tags = getattr(rec, "Tags", None) or {}
        return tags.get(key) or "(untagged)"
    val = getattr(rec, dim, None)
    if val is None or val == "":
        return _NONE
    return str(val)


def _clause_matches(rec, clause, granularity: str) -> bool:
    """Whether `rec` satisfies a filter/exclusion clause."""
    actual = _dim_value(rec, clause.dimension, granularity)
    vals = clause.values
    op = clause.op
    if op == "eq":
        return actual == vals[0]
    if op == "neq":
        return actual != vals[0]
    if op == "in":
        return actual in vals
    if op == "not_in":
        return actual not in vals
    if op == "contains":
        return any(v.lower() in actual.lower() for v in vals)
    if op == "regex":
        try:
            return any(re.search(v, actual) for v in vals)
        except re.error:
            return False
    return False


def run_slice(spec: SliceSpec, records: list) -> SliceResult:
    """Apply filters + exclusions, group by spec.dimensions, sum spec.metric, order/limit."""
    metric = spec.metric
    gran = spec.granularity

    # 1. filter + exclude
    kept = []
    for rec in records:
        if spec.filters and not all(_clause_matches(rec, c, gran) for c in spec.filters):
            continue
        if spec.exclusions and any(_clause_matches(rec, c, gran) for c in spec.exclusions):
            continue
        kept.append(rec)

    # Grand total over the kept set (independent of grouping/limit).
    total = round(sum(float(getattr(r, metric, 0.0) or 0.0) for r in kept), 4)

    # 2. group by the requested dimension tuple (no dims => one KPI row).
    groups: dict[tuple, dict] = {}
    for rec in kept:
        key = tuple(_dim_value(rec, d, gran) for d in spec.dimensions)
        g = groups.get(key)
        if g is None:
            g = {d: key[i] for i, d in enumerate(spec.dimensions)}
            g["metric"] = 0.0
            g["record_count"] = 0
            groups[key] = g
        g["metric"] = round(g["metric"] + float(getattr(rec, metric, 0.0) or 0.0), 4)
        g["record_count"] += 1

    rows = list(groups.values())

    # 3. order
    if spec.order_by == "metric":
        rows.sort(key=lambda r: r["metric"], reverse=True)
    else:
        rows.sort(key=lambda r: str(r.get(spec.order_by, "")))

    # 4. limit (a bare KPI with no dimensions is never truncated)
    truncated = len(rows) > spec.limit and bool(spec.dimensions)
    if spec.dimensions:
        rows = rows[: spec.limit]

    return SliceResult(
        rows=rows,
        total=total,
        metric=metric,
        dimensions=list(spec.dimensions),
        record_count=len(kept),
        truncated=truncated,
        # Both engines answer the same question, so both must report the same two
        # figures. `total` is already the grand total over the kept set here,
        # independent of grouping and limit; this is what the shown rows add to.
        total_shown=round(sum(float(r.get(metric, 0.0) or 0.0) for r in rows), 4),
    )


def derive_card(spec: SliceSpec, result: SliceResult, title: str | None = None) -> CardSpec:
    """Pick a sensible chart template for a slice result.

    - no dimensions            -> kpi (one number)
    - a time dimension present -> line (time series), stacked_bar if a 2nd dim splits it
    - exactly one categorical  -> bar
    - two or more categoricals  -> table
    """
    dims = spec.dimensions
    has_time = TIME_DIMENSION in dims
    if not dims:
        template = "kpi"
    elif has_time:
        template = "stacked_bar" if len(dims) >= 2 else "line"
    elif len(dims) == 1:
        template = "bar"
    else:
        template = "table"

    if title is None:
        if not dims:
            title = f"Total {spec.metric}"
        else:
            pretty = " by ".join(d for d in dims)
            title = f"{spec.metric} by {pretty}" if False else f"{spec.metric}: {pretty}"

    return CardSpec(
        title=title,
        template=template,
        metric=spec.metric,
        dimensions=list(dims),
        slice=spec.to_dict(),
    )
