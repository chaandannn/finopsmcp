"""
CUR pushdown for the slice engine.

The default slice path filters normalized FOCUS records in memory. That can't reach
line-item granularity (usage type, instance type, resource id) because the FOCUS feed
is pre-aggregated. When a SliceSpec uses one of those CUR-only dimensions, we compile
the SAME spec into Athena SQL against the customer's CUR table instead.

Security: every interpolated value goes through _safe_literal (single-quote escaping,
control-char + length rejection), tag columns through cur._safe_tag_column (identifier
allowlist), and dimensions resolve through a closed column map. No agent-supplied string
reaches the SQL unescaped. AWS-only (CUR is an AWS report); requires the CUR_* env vars.
"""
from __future__ import annotations

import re
from datetime import date, timedelta

from ..connectors.cur import (
    _athena_query,
    _db,
    _partition_filter,
    _safe_tag_column,
    _table,
    is_configured,
)
from .spec import TIME_DIMENSION, SliceResult, SliceSpec, is_tag_dim

# Closed dimension -> CUR column map (the agent cannot inject a column name).
_CUR_COL = {
    "ServiceName": "line_item_product_code",
    "ServiceCategory": "line_item_product_code",
    "RegionId": "product_region",
    "RegionName": "product_region",
    "SubAccountId": "line_item_usage_account_id",
    "ResourceId": "line_item_resource_id",
    "resource_id": "line_item_resource_id",
    "usage_type": "line_item_usage_type",
    "instance_type": "product_instance_type",
    "ChargeCategory": "line_item_line_item_type",
}
# v1 metric basis: unblended cost for every metric. True amortized/list cost needs the
# SP/RI effective-cost + public-on-demand columns; deferred. Surfaced as metric_note.
_METRIC_COL = "line_item_unblended_cost"
METRIC_NOTE = "CUR path uses unblended cost for all metrics (amortized/list-cost basis is a follow-up)."


class CURNotConfigured(RuntimeError):
    pass


def _safe_literal(v) -> str:
    """A safe single-quoted Athena string literal from an agent-supplied value."""
    s = str(v)
    if len(s) > 256 or any(ord(c) < 32 for c in s):
        raise ValueError(f"unsafe filter value: {v!r}")
    return "'" + s.replace("'", "''") + "'"


def _dim_expr(dim: str, granularity: str) -> str:
    if dim == TIME_DIMENSION:
        fmt = "%Y-%m" if granularity == "MONTHLY" else "%Y-%m-%d"
        return f"date_format(line_item_usage_start_date, '{fmt}')"
    if is_tag_dim(dim):
        return _safe_tag_column(dim[5:-1])   # identifier allowlist; raises on bad keys
    col = _CUR_COL.get(dim)
    if not col:
        raise ValueError(f"dimension {dim!r} is not supported on the CUR path")
    return col


def _alias(dim: str) -> str:
    if dim == TIME_DIMENSION:
        return "d_date"
    if is_tag_dim(dim):
        return "d_tag_" + re.sub(r"[^a-z0-9_]", "_", dim[5:-1].lower())
    return "d_" + re.sub(r"[^a-zA-Z0-9_]", "_", dim)


def _clause_sql(clause, granularity: str, negate: bool) -> str:
    col = _dim_expr(clause.dimension, granularity)
    op, vals = clause.op, clause.values
    if op == "eq":
        pred = f"{col} = {_safe_literal(vals[0])}"
    elif op == "neq":
        pred = f"{col} != {_safe_literal(vals[0])}"
    elif op in ("in", "not_in"):
        lits = ", ".join(_safe_literal(v) for v in vals)
        pred = f"{col} {'NOT IN' if op == 'not_in' else 'IN'} ({lits})"
    elif op == "contains":
        likes = " OR ".join(f"LOWER({col}) LIKE {_safe_literal('%' + str(v).lower() + '%')}" for v in vals)
        pred = f"({likes})"
    elif op == "regex":
        regs = " OR ".join(f"regexp_like({col}, {_safe_literal(v)})" for v in vals)
        pred = f"({regs})"
    else:
        raise ValueError(f"unsupported op {op!r}")
    return f"NOT ({pred})" if negate else pred


def build_cur_sql(spec: SliceSpec, sd: date, ed: date) -> tuple[str, list[tuple[str, str]]]:
    """Compile a SliceSpec into Athena SQL. Returns (sql, [(dimension, column_alias)])."""
    aliases: list[tuple[str, str]] = []
    select_cols: list[str] = []
    group_cols: list[str] = []
    for d in spec.dimensions:
        expr = _dim_expr(d, spec.granularity)
        al = _alias(d)
        aliases.append((d, al))
        select_cols.append(f"{expr} AS {al}")
        group_cols.append(expr)
    select_cols.append(f"SUM({_METRIC_COL}) AS metric")
    # The grand total, computed over the grouped set BEFORE the LIMIT applies.
    # Without this, `total` was the sum of the top-N rows we happened to show, so
    # a slice over ten thousand usage types reported the biggest fifty as the
    # whole bill. A second query would answer it too, but Athena bills per byte
    # scanned and this rides along on the scan already being paid for.
    if group_cols:
        select_cols.append(f"SUM(SUM({_METRIC_COL})) OVER () AS grand_total")

    where = [
        f"({_partition_filter(sd, ed)})",
        f"line_item_usage_start_date >= DATE {_safe_literal(sd.isoformat())}",
        f"line_item_usage_start_date < DATE {_safe_literal((ed + timedelta(days=1)).isoformat())}",
    ]
    for c in spec.filters:
        where.append(_clause_sql(c, spec.granularity, negate=False))
    for c in spec.exclusions:
        where.append(_clause_sql(c, spec.granularity, negate=True))

    sql = (f"SELECT {', '.join(select_cols)}\n"
           f"FROM {_db()}.{_table()}\n"
           f"WHERE {' AND '.join(where)}")
    if group_cols:
        sql += f"\nGROUP BY {', '.join(group_cols)}"
    if spec.order_by != "metric" and spec.order_by in spec.dimensions:
        sql += f"\nORDER BY {_alias(spec.order_by)}"
    else:
        sql += "\nORDER BY metric DESC"
    sql += f"\nLIMIT {int(spec.limit)}"
    return sql, aliases


def run_slice_cur(spec: SliceSpec, sd: date, ed: date) -> SliceResult:
    """Build + run the CUR SQL for a slice and return a SliceResult. Raises
    CURNotConfigured if the CUR_* env vars are not set."""
    if not is_configured():
        raise CURNotConfigured(
            "CUR is not configured. Set CUR_S3_BUCKET, CUR_ATHENA_DATABASE, "
            "CUR_ATHENA_TABLE, CUR_ATHENA_RESULTS_BUCKET to slice by usage_type / "
            "instance_type / resource_id."
        )
    sql, aliases = build_cur_sql(spec, sd, ed)
    rows = _athena_query(sql)
    out_rows: list[dict] = []
    total = 0.0
    grand: float | None = None
    for r in rows:
        try:
            m = float(r.get("metric") or 0.0)
        except (TypeError, ValueError):
            m = 0.0
        total += m
        if grand is None and r.get("grand_total") is not None:
            try:
                grand = float(r["grand_total"])
            except (TypeError, ValueError):
                grand = None
        row = {dim: (r.get(al) or "") for dim, al in aliases}
        row["metric"] = round(m, 4)
        out_rows.append(row)
    truncated = bool(spec.dimensions) and len(out_rows) >= spec.limit
    # `total` is the whole slice; `total_shown` is what these rows add up to. An
    # ungrouped query returns one row that already IS the total, so they agree.
    shown = round(total, 4)
    return SliceResult(
        rows=out_rows, total=round(grand if grand is not None else total, 4),
        metric=spec.metric, dimensions=list(spec.dimensions),
        record_count=len(out_rows), truncated=truncated, total_shown=shown,
    )
