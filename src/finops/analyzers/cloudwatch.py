"""
CloudWatch metrics client for deep AWS infrastructure utilization analysis.

Provides helpers to fetch metric statistics over configurable lookback windows
and pre-built helpers for EC2, RDS, and Lambda utilization profiles.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any

log = logging.getLogger(__name__)


# ── Low-level metric helper ───────────────────────────────────────────────────

def get_metric_stats(
    cw_client: Any,
    namespace: str,
    metric_name: str,
    dimensions: list[dict],
    period_days: int = 14,
    stat: str = "Average",
    extended_stats: list[str] | None = None,
) -> dict:
    """
    Fetch aggregated metric statistics over a lookback window.

    Args:
        cw_client: boto3 CloudWatch client
        namespace: e.g. "AWS/EC2"
        metric_name: e.g. "CPUUtilization"
        dimensions: list of {"Name": "...", "Value": "..."}
        period_days: lookback window in days (default 14)
        stat: standard statistic — "Average", "Maximum", "Minimum", "Sum"
        extended_stats: optional list like ["p99", "p95"] for percentile stats

    Returns:
        {
            "average": float | None,
            "maximum": float | None,
            "minimum": float | None,
            "p95": float | None,  # only if requested
            "p99": float | None,  # only if requested
            "datapoints": int,
            "unit": str,
        }
    """
    now = datetime.now(timezone.utc)
    start = now - timedelta(days=period_days)
    # Use 1-day periods for long windows to stay within CW limits
    period_seconds = 86400  # 1 day

    queries = []
    query_ids: list[str] = []

    # Standard stats
    for s in ["Average", "Maximum", "Minimum"]:
        qid = f"q_{s.lower()}"
        query_ids.append(qid)
        queries.append(
            {
                "Id": qid,
                "MetricStat": {
                    "Metric": {
                        "Namespace": namespace,
                        "MetricName": metric_name,
                        "Dimensions": dimensions,
                    },
                    "Period": period_seconds,
                    "Stat": s,
                },
                "ReturnData": True,
            }
        )

    # Extended (percentile) stats
    if extended_stats:
        for ext in extended_stats:
            qid = f"q_{ext.replace('.', '_')}"
            query_ids.append(qid)
            queries.append(
                {
                    "Id": qid,
                    "MetricStat": {
                        "Metric": {
                            "Namespace": namespace,
                            "MetricName": metric_name,
                            "Dimensions": dimensions,
                        },
                        "Period": period_seconds,
                        "Stat": ext,
                    },
                    "ReturnData": True,
                }
            )

    try:
        resp = cw_client.get_metric_data(
            MetricDataQueries=queries,
            StartTime=start,
            EndTime=now,
        )
    except Exception as exc:
        log.debug("CloudWatch get_metric_data failed: %s", exc)
        return {"average": None, "maximum": None, "minimum": None, "datapoints": 0, "unit": "None"}

    results_by_id: dict[str, list[float]] = {}
    unit = "None"
    for result in resp.get("MetricDataResults", []):
        vals = result.get("Values", [])
        results_by_id[result["Id"]] = vals
        if vals and result.get("Label"):
            unit = result.get("Label", "None")

    def _agg(qid: str, agg_fn) -> float | None:
        vals = results_by_id.get(qid, [])
        return round(agg_fn(vals), 4) if vals else None

    output: dict[str, Any] = {
        "average": _agg("q_average", lambda v: sum(v) / len(v)),
        "maximum": _agg("q_maximum", max),
        "minimum": _agg("q_minimum", min),
        "datapoints": len(results_by_id.get("q_average", [])),
        "unit": unit,
    }

    if extended_stats:
        for ext in extended_stats:
            qid = f"q_{ext.replace('.', '_')}"
            vals = results_by_id.get(qid, [])
            # CW returns one value per period; p99 over the window is max of daily p99s
            output[ext] = round(max(vals), 4) if vals else None

    return output


# ── EC2 utilization ───────────────────────────────────────────────────────────

def get_ec2_utilization(
    ec2_client: Any,
    cw_client: Any,
    instance_id: str,
    period_days: int = 14,
) -> dict:
    """
    Fetch a comprehensive utilization profile for an EC2 instance.

    Returns:
        {
            "instance_id": str,
            "instance_type": str | None,
            "state": str | None,
            "cpu": {average, maximum, p95, p99},
            "network_in_bytes":  {average_per_day, maximum_per_day},
            "network_out_bytes": {average_per_day, maximum_per_day},
            "disk_read_ops":     {average_per_day, maximum_per_day},
            "disk_write_ops":    {average_per_day, maximum_per_day},
            "period_days": int,
        }
    """
    dims = [{"Name": "InstanceId", "Value": instance_id}]

    instance_type = None
    state = None
    try:
        resp = ec2_client.describe_instances(InstanceIds=[instance_id])
        reservations = resp.get("Reservations", [])
        if reservations:
            inst = reservations[0]["Instances"][0]
            instance_type = inst.get("InstanceType")
            state = inst.get("State", {}).get("Name")
    except Exception as exc:
        log.debug("describe_instances failed for %s: %s", instance_id, exc)

    cpu = get_metric_stats(
        cw_client, "AWS/EC2", "CPUUtilization", dims,
        period_days=period_days, extended_stats=["p95", "p99"],
    )
    net_in = get_metric_stats(cw_client, "AWS/EC2", "NetworkIn", dims, period_days=period_days)
    net_out = get_metric_stats(cw_client, "AWS/EC2", "NetworkOut", dims, period_days=period_days)
    disk_read = get_metric_stats(cw_client, "AWS/EC2", "DiskReadOps", dims, period_days=period_days)
    disk_write = get_metric_stats(cw_client, "AWS/EC2", "DiskWriteOps", dims, period_days=period_days)

    return {
        "instance_id": instance_id,
        "instance_type": instance_type,
        "state": state,
        "period_days": period_days,
        "cpu": {
            "average": cpu.get("average"),
            "maximum": cpu.get("maximum"),
            "p95": cpu.get("p95"),
            "p99": cpu.get("p99"),
        },
        "network_in_bytes": {
            "average_per_day": net_in.get("average"),
            "maximum_per_day": net_in.get("maximum"),
        },
        "network_out_bytes": {
            "average_per_day": net_out.get("average"),
            "maximum_per_day": net_out.get("maximum"),
        },
        "disk_read_ops": {
            "average_per_day": disk_read.get("average"),
            "maximum_per_day": disk_read.get("maximum"),
        },
        "disk_write_ops": {
            "average_per_day": disk_write.get("average"),
            "maximum_per_day": disk_write.get("maximum"),
        },
    }


# ── RDS utilization ───────────────────────────────────────────────────────────

def get_rds_utilization(
    rds_client: Any,
    cw_client: Any,
    db_identifier: str,
    period_days: int = 14,
) -> dict:
    """
    Fetch a comprehensive utilization profile for an RDS instance.

    Returns:
        {
            "db_identifier": str,
            "engine": str | None,
            "instance_class": str | None,
            "cpu": {average, maximum},
            "connections": {average, maximum},
            "free_storage_bytes": {average, minimum},
            "read_iops": {average, maximum},
            "write_iops": {average, maximum},
            "period_days": int,
        }
    """
    dims = [{"Name": "DBInstanceIdentifier", "Value": db_identifier}]

    engine = None
    instance_class = None
    allocated_storage_gb = None
    try:
        resp = rds_client.describe_db_instances(DBInstanceIdentifier=db_identifier)
        instances = resp.get("DBInstances", [])
        if instances:
            db = instances[0]
            engine = db.get("Engine")
            instance_class = db.get("DBInstanceClass")
            allocated_storage_gb = db.get("AllocatedStorage")
    except Exception as exc:
        log.debug("describe_db_instances failed for %s: %s", db_identifier, exc)

    cpu = get_metric_stats(cw_client, "AWS/RDS", "CPUUtilization", dims, period_days=period_days)
    connections = get_metric_stats(cw_client, "AWS/RDS", "DatabaseConnections", dims, period_days=period_days)
    free_storage = get_metric_stats(cw_client, "AWS/RDS", "FreeStorageSpace", dims, period_days=period_days)
    read_iops = get_metric_stats(cw_client, "AWS/RDS", "ReadIOPS", dims, period_days=period_days)
    write_iops = get_metric_stats(cw_client, "AWS/RDS", "WriteIOPS", dims, period_days=period_days)

    return {
        "db_identifier": db_identifier,
        "engine": engine,
        "instance_class": instance_class,
        "allocated_storage_gb": allocated_storage_gb,
        "period_days": period_days,
        "cpu": {
            "average": cpu.get("average"),
            "maximum": cpu.get("maximum"),
        },
        "connections": {
            "average": connections.get("average"),
            "maximum": connections.get("maximum"),
        },
        "free_storage_bytes": {
            "average": free_storage.get("average"),
            "minimum": free_storage.get("minimum"),
        },
        "read_iops": {
            "average": read_iops.get("average"),
            "maximum": read_iops.get("maximum"),
        },
        "write_iops": {
            "average": write_iops.get("average"),
            "maximum": write_iops.get("maximum"),
        },
    }


# ── Lambda utilization ────────────────────────────────────────────────────────

def get_lambda_utilization(
    lambda_client: Any,
    cw_client: Any,
    function_name: str,
    period_days: int = 14,
) -> dict:
    """
    Fetch a comprehensive utilization profile for a Lambda function.

    Note: Lambda does not expose actual memory usage via CloudWatch natively.
    We use InitDuration as a proxy for cold-start overhead, and Duration p99
    to estimate actual execution time vs. configured timeout.

    Returns:
        {
            "function_name": str,
            "runtime": str | None,
            "configured_memory_mb": int | None,
            "configured_timeout_s": int | None,
            "duration_ms": {average, p99, maximum},
            "errors": {sum},
            "throttles": {sum},
            "invocations": {sum},
            "concurrent_executions": {average, maximum},
            "init_duration_ms": {average, maximum},
            "period_days": int,
        }
    """
    dims = [{"Name": "FunctionName", "Value": function_name}]

    runtime = None
    configured_memory_mb = None
    configured_timeout_s = None
    try:
        resp = lambda_client.get_function_configuration(FunctionName=function_name)
        runtime = resp.get("Runtime")
        configured_memory_mb = resp.get("MemorySize")
        configured_timeout_s = resp.get("Timeout")
    except Exception as exc:
        log.debug("get_function_configuration failed for %s: %s", function_name, exc)

    duration = get_metric_stats(
        cw_client, "AWS/Lambda", "Duration", dims,
        period_days=period_days, extended_stats=["p99"],
    )
    errors = get_metric_stats(cw_client, "AWS/Lambda", "Errors", dims, period_days=period_days, stat="Sum")
    throttles = get_metric_stats(cw_client, "AWS/Lambda", "Throttles", dims, period_days=period_days, stat="Sum")
    invocations = get_metric_stats(cw_client, "AWS/Lambda", "Invocations", dims, period_days=period_days, stat="Sum")
    concurrent = get_metric_stats(cw_client, "AWS/Lambda", "ConcurrentExecutions", dims, period_days=period_days)
    init_duration = get_metric_stats(cw_client, "AWS/Lambda", "InitDuration", dims, period_days=period_days)

    return {
        "function_name": function_name,
        "runtime": runtime,
        "configured_memory_mb": configured_memory_mb,
        "configured_timeout_s": configured_timeout_s,
        "period_days": period_days,
        "duration_ms": {
            "average": duration.get("average"),
            "p99": duration.get("p99"),
            "maximum": duration.get("maximum"),
        },
        "errors": {
            "sum": errors.get("average"),  # stat=Sum so "average" field holds sum-per-period
        },
        "throttles": {
            "sum": throttles.get("average"),
        },
        "invocations": {
            "sum": invocations.get("average"),
        },
        "concurrent_executions": {
            "average": concurrent.get("average"),
            "maximum": concurrent.get("maximum"),
        },
        "init_duration_ms": {
            "average": init_duration.get("average"),
            "maximum": init_duration.get("maximum"),
        },
    }
