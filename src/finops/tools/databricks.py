# SPDX-License-Identifier: Apache-2.0
"""databricks MCP tools (extracted from server.py; see finops/tools/__init__.py).

Server-local helpers, globals, and the mcp instance are reached through the live
server module (_srv.NAME) so monkeypatching finops.server.* still works and no
import-order coupling exists."""
from __future__ import annotations

from .. import server as _srv


@_srv.mcp.tool()
async def get_databricks_costs(
    start_date: str | None = None,
    end_date: str | None = None,
) -> dict:
    """
    Return Databricks workspace cost breakdown for the given date range.

    Reports total estimated spend, cost by service type (All-Purpose Compute,
    Jobs, SQL Warehouses, Delta Live Tables) and per-cluster cost.

    Uses the Databricks Billable Usage Download API when DATABRICKS_ACCOUNT_ID
    is set; otherwise estimates from cluster uptime + job run history.

    Args:
        start_date: ISO date string (YYYY-MM-DD). Defaults to 30 days ago.
        end_date:   ISO date string (YYYY-MM-DD). Defaults to today.
    Examples:
        - "What are we spending on Databricks?"
        - "Databricks costs this month"

    """
    from ..connectors.databricks import DatabricksConnector

    conn: DatabricksConnector = _srv.SAAS_CONNECTORS.get("databricks")  # type: ignore
    if not conn or not await conn.is_configured():
        return {
            "error": "Databricks not configured. Set DATABRICKS_HOST and DATABRICKS_TOKEN.",
            "help": "Run: finops setup databricks",
        }

    if start_date and end_date:
        sd = _srv.date.fromisoformat(start_date)
        ed = _srv.date.fromisoformat(end_date)
    else:
        ed = _srv.date.today()
        sd = ed - _srv.timedelta(days=30)

    try:
        summary = await conn.get_costs(sd, ed)
    except Exception as e:
        return {"error": str(e)}

    svc_rows = sorted(summary.by_service.items(), key=lambda x: -x[1])
    ws_rows = sorted(summary.by_account.items(), key=lambda x: -x[1])
    result = {
        "provider": "databricks",
        "period": f"{sd} to {ed}",
        "total_usd": _srv._fmt_usd(summary.total_usd),
        "by_service": {k: _srv._fmt_usd(v) for k, v in svc_rows[:50]},
        "by_workspace": {k: _srv._fmt_usd(v) for k, v in ws_rows[:50]},
        "note": "Costs are estimates based on DBU rates. Set DATABRICKS_ACCOUNT_ID for exact billing data.",
    }
    if len(svc_rows) > 50:
        result["by_service_truncated"] = f"Showing top 50 of {len(svc_rows)} services by spend; total_usd covers all of them."
    if len(ws_rows) > 50:
        result["by_workspace_truncated"] = f"Showing top 50 of {len(ws_rows)} workspaces by spend; total_usd covers all of them."
    return result


@_srv.mcp.tool()
async def get_databricks_dbu_breakdown(
    start_date: str | None = None,
    end_date: str | None = None,
) -> dict:
    """
    Show DBU (Databricks Unit) consumption by cluster, job, and cluster type.

    Identifies the top DBU consumers in the workspace, helping you understand
    which clusters and jobs are driving spend. Surfaces all-purpose clusters
    that should be converted to job clusters for cheaper execution.

    Args:
        start_date: ISO date (YYYY-MM-DD). Defaults to 30 days ago.
        end_date:   ISO date (YYYY-MM-DD). Defaults to today.
    Examples:
        - "Break down our Databricks DBU usage by SKU"

    """
    from ..connectors.databricks import DatabricksConnector

    conn: DatabricksConnector = _srv.SAAS_CONNECTORS.get("databricks")  # type: ignore
    if not conn or not await conn.is_configured():
        return {
            "error": "Databricks not configured. Set DATABRICKS_HOST and DATABRICKS_TOKEN.",
            "help": "Run: finops setup databricks",
        }

    if start_date and end_date:
        sd = _srv.date.fromisoformat(start_date)
        ed = _srv.date.fromisoformat(end_date)
    else:
        ed = _srv.date.today()
        sd = ed - _srv.timedelta(days=30)

    try:
        ws = await conn.get_workspace_summary(sd, ed)
    except Exception as e:
        return {"error": str(e)}

    # Build top consumers table
    top_clusters = [
        {"name": name, "cost": _srv._fmt_usd(cost)}
        for name, cost in list(ws.by_cluster.items())[:10]
    ]
    top_jobs = [
        {"name": name, "cost": _srv._fmt_usd(cost)}
        for name, cost in list(ws.by_job.items())[:10]
    ]

    savings_tip = None
    if ws.by_cluster_type.get("ALL_PURPOSE", 0) > ws.by_cluster_type.get("JOB", 0):
        all_purpose_cost = ws.by_cluster_type.get("ALL_PURPOSE", 0)
        potential = all_purpose_cost * 0.60  # job clusters are ~60% cheaper
        savings_tip = (
            f"All-purpose clusters cost {_srv._fmt_usd(all_purpose_cost)} this period. "
            f"Moving batch workloads to job clusters could save ~{_srv._fmt_usd(potential)}."
        )

    return {
        "provider": "databricks",
        "workspace": ws.workspace_name,
        "period": f"{sd} to {ed}",
        "total_dbu": ws.total_dbu,
        "estimated_total_cost": _srv._fmt_usd(ws.estimated_cost_usd),
        "by_cluster_type": {k: _srv._fmt_usd(v) for k, v in ws.by_cluster_type.items()},
        "top_clusters_by_cost": top_clusters,
        "top_jobs_by_cost": top_jobs,
        **({"savings_tip": savings_tip} if savings_tip else {}),
    }


@_srv.mcp.tool()
async def get_databricks_job_costs(
    start_date: str | None = None,
    end_date: str | None = None,
    top_n: int = 20,
) -> dict:
    """
    Show cost and DBU breakdown by Databricks job run.

    Returns the most expensive job runs in the period, with duration,
    DBU consumed, and estimated cost per run. Useful for finding jobs
    that can be optimised (right-sized clusters, fewer retries, etc.)

    Args:
        start_date: ISO date (YYYY-MM-DD). Defaults to 30 days ago.
        end_date:   ISO date (YYYY-MM-DD). Defaults to today.
        top_n:      Number of top job runs to return (default 20).
    Examples:
        - "What do our Databricks jobs cost?"
        - "Top 10 most expensive Databricks jobs"

    """
    from ..connectors.databricks import DatabricksConnector

    conn: DatabricksConnector = _srv.SAAS_CONNECTORS.get("databricks")  # type: ignore
    if not conn or not await conn.is_configured():
        return {
            "error": "Databricks not configured. Set DATABRICKS_HOST and DATABRICKS_TOKEN.",
            "help": "Run: finops setup databricks",
        }

    if start_date and end_date:
        sd = _srv.date.fromisoformat(start_date)
        ed = _srv.date.fromisoformat(end_date)
    else:
        ed = _srv.date.today()
        sd = ed - _srv.timedelta(days=30)

    try:
        job_costs = await conn.get_job_costs(sd, ed)
    except Exception as e:
        return {"error": str(e)}

    top = job_costs[:top_n]
    total = sum(j.estimated_cost_usd for j in job_costs)

    rows = []
    for j in top:
        rows.append({
            "job_name": j.job_name,
            "run_id": j.run_id,
            "state": j.state,
            "start_time": j.start_time,
            "duration_min": round(j.duration_seconds / 60, 1),
            "dbu": j.dbu_consumed,
            "estimated_cost": _srv._fmt_usd(j.estimated_cost_usd),
        })

    return {
        "provider": "databricks",
        "period": f"{sd} to {ed}",
        "total_runs_analyzed": len(job_costs),
        "total_estimated_cost": _srv._fmt_usd(total),
        "top_runs": rows,
    }
