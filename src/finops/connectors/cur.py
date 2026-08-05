"""
AWS Cost and Usage Report (CUR) connector via Athena.

Reads line-item granularity data from a CUR v2 table in Athena.
This is a Pro plan feature -- requires CUR delivery to S3 and an
Athena database created via the CUR console or AWS Glue Crawler.

Required env vars:
  CUR_S3_BUCKET              -- S3 bucket where CUR data lands
  CUR_ATHENA_DATABASE        -- Athena database name (e.g. "cur_db")
  CUR_ATHENA_TABLE           -- Athena table name (e.g. "cur_report")
  CUR_ATHENA_RESULTS_BUCKET  -- S3 bucket for Athena query results

Optional:
  CUR_ATHENA_WORKGROUP       -- Athena workgroup (default: "primary")
"""
from __future__ import annotations

import logging
import re
import time
from datetime import date
from typing import Any

from ..security.env import get_env

log = logging.getLogger(__name__)


class CURQueryError(Exception):
    """Raised when an Athena query fails or times out."""


# ── configuration ─────────────────────────────────────────────────────────────

def is_configured() -> bool:
    """Return True when all four required CUR env vars hold a real value.

    Whitespace is not configuration. A key set to "   " (a trailing space in a
    .env file, a blank line in a Docker secret, a copy-paste from a wiki) used to
    read as configured, and the failure then surfaced as an Athena syntax error
    against a table named " " instead of the setup instructions the operator
    actually needed.
    """
    required = [
        "CUR_S3_BUCKET",
        "CUR_ATHENA_DATABASE",
        "CUR_ATHENA_TABLE",
        "CUR_ATHENA_RESULTS_BUCKET",
    ]
    return all((get_env(v) or "").strip() for v in required)


def _db() -> str:
    return get_env("CUR_ATHENA_DATABASE")


def _table() -> str:
    return get_env("CUR_ATHENA_TABLE")


def _results_bucket() -> str:
    return get_env("CUR_ATHENA_RESULTS_BUCKET")


def _workgroup() -> str:
    return get_env("CUR_ATHENA_WORKGROUP") or "primary"


# ── SQL helpers ───────────────────────────────────────────────────────────────

# Athena CUR tag column names are derived from user-supplied tag keys.
# Only allow characters that are valid in AWS tag keys and safe as SQL identifiers.
# This prevents SQL injection via tag_key interpolated into column names.
_TAG_KEY_RE = re.compile(r"^[a-zA-Z0-9_:@./\-]{1,128}$")


def _safe_tag_column(tag_key: str) -> str:
    """
    Convert a tag key to the Athena CUR column name.

    Raises ValueError if the tag key contains characters that could be used
    for SQL injection. Valid AWS tag keys are alphanumeric plus _:@./-
    CUR normalises them to lowercase with spaces replaced by underscores.
    """
    if not _TAG_KEY_RE.match(tag_key):
        raise ValueError(
            f"Invalid tag key {tag_key!r}: must match [a-zA-Z0-9_:@./\\-]{{1,128}}"
        )
    # CUR normalises tag key to lowercase, replaces spaces/hyphens with underscores
    normalised = tag_key.lower().replace("-", "_").replace(" ", "_").replace(".", "_")
    return f"resource_tags_user_{normalised}"


def _partition_filter(start_date: date, end_date: date) -> str:
    """
    Build a SQL WHERE fragment covering all year/month partitions in the range.

    CUR tables are partitioned by year (string) and month (zero-padded string).
    Example for Jan-Feb 2026:
      "(year='2026' AND month='01') OR (year='2026' AND month='02')"

    Handles year boundaries correctly.
    """
    clauses: list[str] = []
    y, m = start_date.year, start_date.month
    end_y, end_m = end_date.year, end_date.month

    while (y, m) <= (end_y, end_m):
        clauses.append(f"(year='{y}' AND month='{m:02d}')")
        m += 1
        if m > 12:
            m = 1
            y += 1

    if not clauses:
        return "1=0"
    return " OR ".join(clauses)


# ── Athena query engine ───────────────────────────────────────────────────────

def _athena_query(sql: str, timeout_secs: int = 30) -> list[dict]:
    """
    Submit a query to Athena, poll with exponential backoff, return rows.

    Backoff sequence: 100ms, 200ms, 400ms, 800ms, 1600ms, then 1600ms repeating
    until timeout_secs is reached.

    Result reuse is enabled with a 1-hour window to reduce cost on repeated
    identical queries (e.g. same date range fetched by multiple tools).

    Raises:
        CURQueryError: on Athena failure or timeout.
    """
    try:
        import boto3
    except ImportError:
        raise CURQueryError("boto3 is not installed. Run: pip install boto3")

    athena = boto3.client("athena")

    output_location = f"s3://{_results_bucket()}/athena-results/"

    try:
        start_resp = athena.start_query_execution(
            QueryString=sql,
            QueryExecutionContext={"Database": _db()},
            ResultConfiguration={"OutputLocation": output_location},
            WorkGroup=_workgroup(),
            ResultReuseConfiguration={
                "ResultReuseByAgeConfiguration": {
                    "Enabled": True,
                    "MaxAgeInMinutes": 60,
                }
            },
        )
    except Exception as exc:
        raise CURQueryError(f"Failed to start Athena query: {exc}") from exc

    execution_id = start_resp["QueryExecutionId"]
    log.debug("Athena query submitted: %s", execution_id)

    # Poll with exponential backoff
    delay = 0.1
    max_delay = 1.6
    elapsed = 0.0

    while elapsed < timeout_secs:
        time.sleep(delay)
        elapsed += delay

        try:
            status_resp = athena.get_query_execution(QueryExecutionId=execution_id)
        except Exception as exc:
            raise CURQueryError(f"Failed to poll Athena query: {exc}") from exc

        state = status_resp["QueryExecution"]["Status"]["State"]

        if state == "SUCCEEDED":
            break
        if state in ("FAILED", "CANCELLED"):
            reason = (
                status_resp["QueryExecution"]["Status"]
                .get("StateChangeReason", "unknown reason")
            )
            raise CURQueryError(
                f"Athena query {execution_id} {state.lower()}: {reason}"
            )

        delay = min(delay * 2, max_delay)
    else:
        raise CURQueryError(
            f"Athena query {execution_id} timed out after {timeout_secs}s"
        )

    # Paginate through results
    rows: list[dict] = []
    paginator = athena.get_paginator("get_query_results")
    columns: list[str] = []
    first_page = True

    try:
        for page in paginator.paginate(QueryExecutionId=execution_id):
            result_rows = page["ResultSet"]["Rows"]
            if first_page:
                # First row is the header
                columns = [c["VarCharValue"] for c in result_rows[0]["Data"]]
                result_rows = result_rows[1:]
                first_page = False
            for row in result_rows:
                values = [cell.get("VarCharValue", "") for cell in row["Data"]]
                rows.append(dict(zip(columns, values)))
    except Exception as exc:
        raise CURQueryError(
            f"Failed to read Athena results for {execution_id}: {exc}"
        ) from exc

    log.debug("Athena query %s returned %d rows", execution_id, len(rows))
    return rows


# ── public API ────────────────────────────────────────────────────────────────

def get_resource_costs(
    start_date: date,
    end_date: date,
    service: str | None = None,
    account_id: str | None = None,
    min_cost_usd: float = 1.0,
    limit: int = 100,
    resource_id: str | None = None,
) -> dict[str, Any]:
    """
    Return per-resource cost data from the CUR Athena table.

    Filters to Usage, DiscountedUsage, and SavingsPlanCoveredUsage line items,
    excludes blank resource IDs, and applies a minimum cost threshold.
    Effective savings is derived as on_demand_equivalent minus unblended_cost.

    Args:
        start_date: Inclusive start of the billing period.
        end_date: Inclusive end of the billing period.
        service: Optional AWS service code filter (e.g. "Amazon EC2").
        account_id: Optional 12-digit account ID filter.
        min_cost_usd: Exclude resources whose total cost is below this.
        limit: Maximum number of resources to return, ordered by cost desc.

    Returns:
        {
            "resources": [...],
            "total_cost": float,
            "total_resources": int,
            "period": str,
            "source": "cur_athena",
        }
    """
    if not is_configured():
        return _error("CUR not configured. Set CUR_S3_BUCKET, CUR_ATHENA_DATABASE, "
                      "CUR_ATHENA_TABLE, CUR_ATHENA_RESULTS_BUCKET.")

    partition = _partition_filter(start_date, end_date)
    table = f"{_db()}.{_table()}"

    extra_filters = ""
    if service:
        safe_service = service.replace("'", "''")
        extra_filters += f"\n         AND line_item_product_code = '{safe_service}'"
    if account_id:
        safe_account = account_id.replace("'", "''")
        extra_filters += f"\n         AND line_item_usage_account_id = '{safe_account}'"
    if resource_id:
        # CUR stores some resource ids as full ARNs and others bare; match the
        # tail so callers can pass either form (e.g. "i-0abc..." matches
        # "arn:aws:ec2:...:instance/i-0abc...").
        safe_rid = resource_id.replace("'", "''")
        extra_filters += (
            f"\n         AND (line_item_resource_id = '{safe_rid}'"
            f" OR line_item_resource_id LIKE '%{safe_rid}')"
        )

    sql = f"""
SELECT
    line_item_resource_id,
    line_item_product_code,
    line_item_usage_account_id,
    product_region,
    product_instance_type,
    SUM(line_item_unblended_cost)        AS unblended_cost,
    SUM(pricing_public_on_demand_cost)   AS on_demand_equivalent
FROM {table}
WHERE ({partition})
  AND line_item_line_item_type IN ('Usage', 'DiscountedUsage', 'SavingsPlanCoveredUsage')
  AND line_item_resource_id IS NOT NULL
  AND line_item_resource_id != ''
  AND line_item_unblended_cost > 0{extra_filters}
GROUP BY
    line_item_resource_id,
    line_item_product_code,
    line_item_usage_account_id,
    product_region,
    product_instance_type
HAVING SUM(line_item_unblended_cost) > {min_cost_usd}
ORDER BY unblended_cost DESC
LIMIT {limit}
""".strip()

    try:
        rows = _athena_query(sql)
    except CURQueryError as exc:
        log.warning("CUR get_resource_costs failed: %s", exc)
        return {"error": str(exc), "source": "cur_athena"}

    resources: list[dict] = []
    total_cost = 0.0

    for row in rows:
        unblended = _float(row.get("unblended_cost", "0"))
        on_demand  = _float(row.get("on_demand_equivalent", "0"))
        savings    = max(0.0, on_demand - unblended)
        total_cost += unblended
        resources.append({
            "resource_id":        row.get("line_item_resource_id", ""),
            "service":            row.get("line_item_product_code", ""),
            "account_id":         row.get("line_item_usage_account_id", ""),
            "region":             row.get("product_region", ""),
            "instance_type":      row.get("product_instance_type", ""),
            "unblended_cost":     round(unblended, 4),
            "on_demand_equivalent": round(on_demand, 4),
            "effective_savings":  round(savings, 4),
        })

    return {
        "resources":        resources,
        "total_cost":       round(total_cost, 4),
        "total_resources":  len(resources),
        "period":           f"{start_date} to {end_date}",
        "source":           "cur_athena",
    }


def get_ri_waste(
    start_date: date,
    end_date: date,
    min_waste_usd: float = 10.0,
) -> dict[str, Any]:
    """
    Identify wasted Reserved Instance spend from CUR RIFee line items.

    Unused RI hours are hours where capacity was reserved but not consumed.
    Wasted USD is the unused recurring fee for those hours.

    Args:
        start_date: Inclusive start of the billing period.
        end_date: Inclusive end of the billing period.
        min_waste_usd: Exclude reservations wasting less than this amount.

    Returns:
        {
            "reservations": [...],
            "total_wasted_usd": float,
            "source": "cur_athena",
        }
    """
    if not is_configured():
        return _error("CUR not configured.")

    partition = _partition_filter(start_date, end_date)
    table = f"{_db()}.{_table()}"

    sql = f"""
SELECT
    reservation_reservation_a_r_n          AS reservation_arn,
    product_instance_type                  AS instance_type,
    product_region                         AS region,
    SUM(reservation_unused_quantity)       AS unused_hours,
    SUM(reservation_unused_recurring_fee)  AS wasted_usd,
    SUM(reservation_unused_quantity)
        / NULLIF(SUM(reservation_unused_quantity)
                 + SUM(line_item_usage_amount), 0) * 100  AS waste_pct
FROM {table}
WHERE ({partition})
  AND line_item_line_item_type = 'RIFee'
  AND reservation_reservation_a_r_n IS NOT NULL
  AND reservation_reservation_a_r_n != ''
GROUP BY
    reservation_reservation_a_r_n,
    product_instance_type,
    product_region
HAVING SUM(reservation_unused_recurring_fee) > {min_waste_usd}
ORDER BY wasted_usd DESC
""".strip()

    try:
        rows = _athena_query(sql)
    except CURQueryError as exc:
        log.warning("CUR get_ri_waste failed: %s", exc)
        return {"error": str(exc), "source": "cur_athena"}

    reservations: list[dict] = []
    total_wasted = 0.0

    for row in rows:
        wasted      = _float(row.get("wasted_usd", "0"))
        unused_hrs  = _float(row.get("unused_hours", "0"))
        waste_pct   = _float(row.get("waste_pct", "0"))
        utilization = max(0.0, round(100.0 - waste_pct, 2))
        total_wasted += wasted
        reservations.append({
            "reservation_arn":   row.get("reservation_arn", ""),
            "instance_type":     row.get("instance_type", ""),
            "region":            row.get("region", ""),
            "unused_hours":      round(unused_hrs, 2),
            "wasted_usd":        round(wasted, 4),
            "utilization_pct":   utilization,
        })

    return {
        "reservations":      reservations,
        "total_wasted_usd":  round(total_wasted, 4),
        "source":            "cur_athena",
    }


def get_tag_cost_breakdown(
    tag_key: str,
    start_date: date,
    end_date: date,
    cost_type: str = "unblended",
) -> dict[str, Any]:
    """
    Break costs down by a CUR resource tag.

    CUR stores user tags as columns named resource_tags_user_{tag_key}.
    Resources missing the tag are grouped under "__untagged__".

    Args:
        tag_key: The tag key to group by (e.g. "team", "env", "project").
        start_date: Inclusive start of the billing period.
        end_date: Inclusive end of the billing period.
        cost_type: "unblended" (default) or "amortized". Amortized uses
                   effective cost for Savings Plan and RI lines.

    Returns:
        {
            "by_tag": {"payments": 1234.56, "__untagged__": 890.12, ...},
            "tag_key": str,
            "source": "cur_athena",
        }
    """
    if not is_configured():
        return _error("CUR not configured.")

    partition = _partition_filter(start_date, end_date)
    table = f"{_db()}.{_table()}"
    try:
        tag_col = _safe_tag_column(tag_key)
    except ValueError as exc:
        return {"error": str(exc), "source": "cur_athena"}

    if cost_type == "amortized":
        cost_expr = (
            "SUM(CASE "
            "  WHEN line_item_line_item_type = 'SavingsPlanCoveredUsage' "
            "    THEN savingsplan_savings_plan_effective_cost "
            "  WHEN line_item_line_item_type = 'DiscountedUsage' "
            "    THEN reservation_effective_cost "
            "  ELSE line_item_unblended_cost "
            "END)"
        )
    else:
        cost_expr = "SUM(line_item_unblended_cost)"

    sql = f"""
SELECT
    COALESCE(NULLIF({tag_col}, ''), '__untagged__') AS tag_value,
    {cost_expr} AS cost_usd
FROM {table}
WHERE ({partition})
  AND line_item_line_item_type IN ('Usage', 'DiscountedUsage', 'SavingsPlanCoveredUsage')
GROUP BY COALESCE(NULLIF({tag_col}, ''), '__untagged__')
ORDER BY cost_usd DESC
""".strip()

    try:
        rows = _athena_query(sql)
    except CURQueryError as exc:
        log.warning("CUR get_tag_cost_breakdown failed: %s", exc)
        return {"error": str(exc), "source": "cur_athena"}

    by_tag: dict[str, float] = {}
    for row in rows:
        tag_val = row.get("tag_value") or "__untagged__"
        cost    = _float(row.get("cost_usd", "0"))
        by_tag[tag_val] = round(cost, 4)

    return {
        "by_tag":    by_tag,
        "tag_key":   tag_key,
        "cost_type": cost_type,
        "period":    f"{start_date} to {end_date}",
        "source":    "cur_athena",
    }


def get_untagged_resource_cost(
    start_date: date,
    end_date: date,
    tag_key: str = "team",
) -> dict[str, Any]:
    """
    Quantify spend on resources that are missing a specified tag.

    Useful for driving tagging compliance: surfaces exactly how much
    unattributed cost exists and which services are the biggest offenders.

    Args:
        start_date: Inclusive start of the billing period.
        end_date: Inclusive end of the billing period.
        tag_key: Tag to check for absence (default "team").

    Returns:
        {
            "untagged_cost_usd": float,
            "untagged_resource_count": int,
            "by_service": {"Amazon EC2": 1234.56, ...},
            "source": "cur_athena",
        }
    """
    if not is_configured():
        return _error("CUR not configured.")

    partition = _partition_filter(start_date, end_date)
    table = f"{_db()}.{_table()}"
    try:
        tag_col = _safe_tag_column(tag_key)
    except ValueError as exc:
        return {"error": str(exc), "source": "cur_athena"}

    sql = f"""
SELECT
    line_item_product_code              AS service,
    SUM(line_item_unblended_cost)       AS cost_usd,
    COUNT(DISTINCT line_item_resource_id) AS resource_count
FROM {table}
WHERE ({partition})
  AND line_item_line_item_type IN ('Usage', 'DiscountedUsage', 'SavingsPlanCoveredUsage')
  AND line_item_resource_id IS NOT NULL
  AND line_item_resource_id != ''
  AND ({tag_col} IS NULL OR {tag_col} = '')
GROUP BY line_item_product_code
ORDER BY cost_usd DESC
""".strip()

    try:
        rows = _athena_query(sql)
    except CURQueryError as exc:
        log.warning("CUR get_untagged_resource_cost failed: %s", exc)
        return {"error": str(exc), "source": "cur_athena"}

    by_service: dict[str, float] = {}
    total_cost = 0.0
    total_resources = 0

    for row in rows:
        service   = row.get("service") or "unknown"
        cost      = _float(row.get("cost_usd", "0"))
        res_count = int(_float(row.get("resource_count", "0")))
        by_service[service] = round(cost, 4)
        total_cost      += cost
        total_resources += res_count

    return {
        "untagged_cost_usd":       round(total_cost, 4),
        "untagged_resource_count": total_resources,
        "by_service":              by_service,
        "tag_key":                 tag_key,
        "period":                  f"{start_date} to {end_date}",
        "source":                  "cur_athena",
    }


def get_savings_plan_showback(
    start_date: date,
    end_date: date,
    tag_key: str = "team",
    include_ri: bool = True,
) -> dict[str, Any]:
    """
    Attribute Savings Plan (and optionally RI) benefits back to teams/services
    by tag — the showback problem no other tool solves at line-item granularity.

    How it works
    ────────────
    AWS Savings Plans apply a blended discount across the payer account. The CUR
    exposes two fields per covered resource:

      savings_plan_effective_cost  — what you actually paid under SP rates
      pricing_public_on_demand_cost — what you'd have paid on-demand

    The difference is the real dollar benefit that resource captured from the SP.
    By grouping these fields by a resource tag (e.g. "team"), each team sees:
      • how much they consumed under SP coverage (effective cost)
      • how much they would have paid without it (on-demand equivalent)
      • how much they saved in real dollars (savings captured)
      • their effective discount rate

    For Reserved Instances, the same logic applies using reservation_effective_cost
    vs pricing_public_on_demand_cost on DiscountedUsage lines.

    Args:
        start_date: Inclusive start of the billing period.
        end_date:   Inclusive end of the billing period.
        tag_key:    Resource tag to group by (default "team").
        include_ri: Also include RI discounts in the showback (default True).

    Returns:
        {
            "by_tag": {
                "payments": {
                    "effective_cost":     float,   # what they actually paid
                    "on_demand_equiv":    float,   # what they'd have paid
                    "savings_captured":   float,   # dollar benefit from SP/RI
                    "discount_rate_pct":  float,   # effective discount %
                    "sp_savings":         float,   # savings from SPs specifically
                    "ri_savings":         float,   # savings from RIs specifically
                },
                ...
                "__untagged__": { ... },           # unattributed resources
            },
            "summary": {
                "total_effective_cost":   float,
                "total_on_demand_equiv":  float,
                "total_savings_captured": float,
                "overall_discount_pct":   float,
                "sp_coverage_pct":        float,   # % of usage covered by SP
                "ri_coverage_pct":        float,   # % of usage covered by RI
            },
            "tag_key": str,
            "period":  str,
            "source":  "cur_athena",
        }
    """
    if not is_configured():
        return _error("CUR not configured. Set CUR_S3_BUCKET, CUR_ATHENA_DATABASE, "
                      "CUR_ATHENA_TABLE, CUR_ATHENA_RESULTS_BUCKET.")

    partition = _partition_filter(start_date, end_date)
    table     = f"{_db()}.{_table()}"
    try:
        tag_col = _safe_tag_column(tag_key)
    except ValueError as exc:
        return {"error": str(exc), "source": "cur_athena"}

    # line_item_types to include
    li_types = "'SavingsPlanCoveredUsage', 'Usage', 'DiscountedUsage'"

    sql = f"""
SELECT
    COALESCE(NULLIF({tag_col}, ''), '__untagged__')            AS tag_value,

    -- Savings Plan lines
    SUM(CASE WHEN line_item_line_item_type = 'SavingsPlanCoveredUsage'
             THEN savingsplan_savings_plan_effective_cost ELSE 0 END)  AS sp_effective_cost,
    SUM(CASE WHEN line_item_line_item_type = 'SavingsPlanCoveredUsage'
             THEN pricing_public_on_demand_cost ELSE 0 END)            AS sp_on_demand_equiv,

    -- Reserved Instance lines
    SUM(CASE WHEN line_item_line_item_type = 'DiscountedUsage'
             THEN reservation_effective_cost ELSE 0 END)               AS ri_effective_cost,
    SUM(CASE WHEN line_item_line_item_type = 'DiscountedUsage'
             THEN pricing_public_on_demand_cost ELSE 0 END)            AS ri_on_demand_equiv,

    -- Regular on-demand usage (no commitment discount)
    SUM(CASE WHEN line_item_line_item_type = 'Usage'
             THEN line_item_unblended_cost ELSE 0 END)                 AS od_cost,

    -- Totals
    SUM(CASE
          WHEN line_item_line_item_type = 'SavingsPlanCoveredUsage'
            THEN savingsplan_savings_plan_effective_cost
          WHEN line_item_line_item_type = 'DiscountedUsage'
            THEN reservation_effective_cost
          ELSE line_item_unblended_cost
        END)                                                           AS total_effective_cost,
    SUM(pricing_public_on_demand_cost)                                 AS total_on_demand_equiv

FROM {table}
WHERE ({partition})
  AND line_item_line_item_type IN ({li_types})
GROUP BY COALESCE(NULLIF({tag_col}, ''), '__untagged__')
ORDER BY total_effective_cost DESC
""".strip()

    try:
        rows = _athena_query(sql, timeout_secs=45)
    except CURQueryError as exc:
        log.warning("CUR get_savings_plan_showback failed: %s", exc)
        return {"error": str(exc), "source": "cur_athena"}

    by_tag: dict[str, dict] = {}
    grand_effective   = 0.0
    grand_on_demand   = 0.0
    grand_sp_savings  = 0.0
    grand_ri_savings  = 0.0
    grand_sp_od       = 0.0
    grand_ri_od       = 0.0

    for row in rows:
        tag_val       = row.get("tag_value") or "__untagged__"
        sp_eff        = _float(row.get("sp_effective_cost", "0"))
        sp_od         = _float(row.get("sp_on_demand_equiv", "0"))
        ri_eff        = _float(row.get("ri_effective_cost", "0"))
        ri_od         = _float(row.get("ri_on_demand_equiv", "0"))
        od_cost       = _float(row.get("od_cost", "0"))
        total_eff     = _float(row.get("total_effective_cost", "0"))
        total_od      = _float(row.get("total_on_demand_equiv", "0"))

        sp_savings    = max(0.0, sp_od - sp_eff)
        ri_savings    = max(0.0, ri_od - ri_eff) if include_ri else 0.0
        total_savings = sp_savings + ri_savings

        discount_pct  = (total_savings / total_od * 100) if total_od > 0 else 0.0

        by_tag[tag_val] = {
            "effective_cost":    round(total_eff, 4),
            "on_demand_equiv":   round(total_od, 4),
            "savings_captured":  round(total_savings, 4),
            "discount_rate_pct": round(discount_pct, 2),
            "sp_savings":        round(sp_savings, 4),
            "ri_savings":        round(ri_savings, 4) if include_ri else 0.0,
            "on_demand_cost":    round(od_cost, 4),   # usage not covered by any commitment
        }

        grand_effective  += total_eff
        grand_on_demand  += total_od
        grand_sp_savings += sp_savings
        grand_ri_savings += ri_savings if include_ri else 0.0
        grand_sp_od      += sp_od
        grand_ri_od      += ri_od

    grand_savings     = grand_sp_savings + grand_ri_savings
    overall_discount  = (grand_savings / grand_on_demand * 100) if grand_on_demand > 0 else 0.0
    sp_coverage_pct   = (grand_sp_od / grand_on_demand * 100) if grand_on_demand > 0 else 0.0
    ri_coverage_pct   = (grand_ri_od / grand_on_demand * 100) if grand_on_demand > 0 else 0.0

    return {
        "by_tag": by_tag,
        "summary": {
            "total_effective_cost":   round(grand_effective, 4),
            "total_on_demand_equiv":  round(grand_on_demand, 4),
            "total_savings_captured": round(grand_savings, 4),
            "overall_discount_pct":   round(overall_discount, 2),
            "sp_coverage_pct":        round(sp_coverage_pct, 2),
            "ri_coverage_pct":        round(ri_coverage_pct, 2),
        },
        "tag_key": tag_key,
        "period":  f"{start_date} to {end_date}",
        "source":  "cur_athena",
    }


# ── helpers ───────────────────────────────────────────────────────────────────

def _float(value: Any) -> float:
    """Safely convert Athena string values to float."""
    try:
        return float(value) if value not in (None, "", "NULL") else 0.0
    except (TypeError, ValueError):
        return 0.0


def _error(message: str) -> dict[str, Any]:
    return {"error": message, "source": "cur_athena"}
