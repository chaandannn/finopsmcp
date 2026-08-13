"""
AWS waste pattern detection — goes well beyond Cost Explorer.

Each check function returns a list of finding dicts:
    {
        "resource_id": str,
        "resource_type": str,
        "waste_type": str,
        "estimated_monthly_savings": float,
        "detail": str,
        "severity": "low" | "medium" | "high" | "critical",
        "region": str,
        "account_id": str | None,
    }

Monetary estimates use on-demand approximations — not exact billing figures.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any

log = logging.getLogger(__name__)

# ── Pricing constants (on-demand approximations) ──────────────────────────────

_EIP_MONTHLY = 3.60                   # unassociated EIP / month
_NAT_GW_BASE_MONTHLY = 32.85          # NAT GW fixed cost / month ($0.045/hr * 730hr)
_NAT_GW_DATA_PER_GB = 0.045           # per GB processed
_EBS_GP2_PER_GB_MONTH = 0.10          # gp2 price / GB / month (us-east-1)
_EBS_GP3_PER_GB_MONTH = 0.08          # gp3 price / GB / month (us-east-1)
_EBS_GP2_TO_GP3_SAVINGS_PCT = 0.20    # 20% cheaper
_CW_LOGS_STORAGE_PER_GB_MONTH = 0.03  # archived log storage
_S3_STANDARD_PER_GB_MONTH = 0.023
_S3_INT_TIER_PER_GB_MONTH = 0.0125    # Intelligent-Tiering (frequent-access tier)
_RDS_BACKUP_EXTRA_PER_GB_MONTH = 0.095 # RDS backup storage beyond 1x DB size

# Sentinel for "savings unknown" — will be treated as 0 in sorting but surfaced
_UNKNOWN_SAVINGS = 0.0


# ── Helpers ───────────────────────────────────────────────────────────────────

def _severity_from_savings(monthly_savings: float) -> str:
    if monthly_savings >= 100:
        return "critical"
    if monthly_savings >= 30:
        return "high"
    if monthly_savings >= 10:
        return "medium"
    return "low"


def _get_account_id(sts_client: Any | None) -> str | None:
    if sts_client is None:
        return None
    try:
        return sts_client.get_caller_identity()["Account"]
    except Exception:
        return None


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


# ── EBS volumes ───────────────────────────────────────────────────────────────

def check_ebs_volumes(ec2_client: Any, region: str = "unknown") -> list[dict]:
    """
    Detect:
    - Unattached EBS volumes (paying for storage with nothing using it)
    - gp2 volumes that should be migrated to gp3 (20% cheaper, better baseline perf)
    """
    findings: list[dict] = []

    try:
        paginator = ec2_client.get_paginator("describe_volumes")
        pages = paginator.paginate()
    except Exception as exc:
        log.warning("describe_volumes failed (region=%s): %s", region, exc)
        return findings

    for page in pages:
        for vol in page.get("Volumes", []):
            vol_id = vol["VolumeId"]
            size_gb = vol.get("Size", 0)
            vol_type = vol.get("VolumeType", "")
            state = vol.get("State", "")
            attachments = vol.get("Attachments", [])
            name_tag = next(
                (t["Value"] for t in vol.get("Tags", []) if t["Key"] == "Name"), ""
            )

            # Unattached volumes
            if state == "available" and not attachments:
                monthly_cost = size_gb * _EBS_GP2_PER_GB_MONTH
                findings.append({
                    "resource_id": vol_id,
                    "resource_type": "EBS Volume",
                    "waste_type": "unattached_ebs_volume",
                    "estimated_monthly_savings": round(monthly_cost, 2),
                    "detail": (
                        f"{size_gb} GB {vol_type} volume is unattached (state=available). "
                        f"Name: {name_tag or 'untagged'}. "
                        f"Delete it or snapshot+delete if needed as a backup."
                    ),
                    "severity": _severity_from_savings(monthly_cost),
                    "region": region,
                    "account_id": None,
                    "size_gb": size_gb,
                    "volume_type": vol_type,
                })

            # gp2 → gp3 migration candidates (all gp2 volumes qualify)
            if vol_type == "gp2" and size_gb > 0:
                monthly_savings = size_gb * (_EBS_GP2_PER_GB_MONTH - _EBS_GP3_PER_GB_MONTH)
                findings.append({
                    "resource_id": vol_id,
                    "resource_type": "EBS Volume",
                    "waste_type": "gp2_should_migrate_to_gp3",
                    "estimated_monthly_savings": round(monthly_savings, 2),
                    "detail": (
                        f"{size_gb} GB gp2 volume. Migrating to gp3 saves ~20% "
                        f"(${monthly_savings:.2f}/mo) and gives 3,000 IOPS + 125 MB/s free "
                        f"(vs gp2's variable burst). Zero downtime — API call only. "
                        f"Name: {name_tag or 'untagged'}."
                    ),
                    "severity": _severity_from_savings(monthly_savings),
                    "region": region,
                    "account_id": None,
                    "size_gb": size_gb,
                    "volume_type": vol_type,
                    "attached_to": [a.get("InstanceId") for a in attachments],
                })

    return findings


# ── EBS snapshots ─────────────────────────────────────────────────────────────

def check_ebs_snapshots(ec2_client: Any, region: str = "unknown", older_than_days: int = 30) -> list[dict]:
    """
    Detect snapshots older than `older_than_days` owned by this account
    that have no associated AMI (orphaned) or no lifecycle policy.
    EBS snapshot storage is $0.05/GB-month.
    """
    _SNAPSHOT_STORAGE_PER_GB_MONTH = 0.05
    findings: list[dict] = []
    cutoff = _now_utc() - timedelta(days=older_than_days)

    try:
        # Get the current account ID to filter to owned snapshots.
        account_id = None
        try:
            import boto3
            sts_client = boto3.client("sts", region_name=region if region != "unknown" else "us-east-1")
            account_id = sts_client.get_caller_identity()["Account"]
        except Exception:
            pass

        kwargs: dict[str, Any] = {"Filters": [{"Name": "status", "Values": ["completed"]}]}
        if account_id:
            kwargs["OwnerIds"] = [account_id]

        paginator = ec2_client.get_paginator("describe_snapshots")
        pages = paginator.paginate(**kwargs)
    except Exception as exc:
        log.warning("describe_snapshots failed (region=%s): %s", region, exc)
        return findings

    # Gather AMI snapshot IDs so we don't flag snapshots backing AMIs
    ami_snapshot_ids: set[str] = set()
    try:
        ami_paginator = ec2_client.get_paginator("describe_images")
        for page in ami_paginator.paginate(Owners=["self"]):
            for image in page.get("Images", []):
                for bdm in image.get("BlockDeviceMappings", []):
                    snap_id = bdm.get("Ebs", {}).get("SnapshotId")
                    if snap_id:
                        ami_snapshot_ids.add(snap_id)
    except Exception:
        pass  # If we can't list AMIs, skip this filter

    for page in pages:
        for snap in page.get("Snapshots", []):
            snap_id = snap["SnapshotId"]
            start_time = snap.get("StartTime")
            if not start_time:
                continue

            if start_time.tzinfo is None:
                start_time = start_time.replace(tzinfo=timezone.utc)

            if start_time >= cutoff:
                continue  # Recent — skip

            if snap_id in ami_snapshot_ids:
                continue  # Backing an AMI — needed

            size_gb = snap.get("VolumeSize", 0) or 0
            monthly_cost = size_gb * _SNAPSHOT_STORAGE_PER_GB_MONTH
            age_days = (_now_utc() - start_time).days
            description = snap.get("Description", "")
            name_tag = next(
                (t["Value"] for t in snap.get("Tags", []) if t["Key"] == "Name"), ""
            )

            findings.append({
                "resource_id": snap_id,
                "resource_type": "EBS Snapshot",
                "waste_type": "old_unmanaged_snapshot",
                "estimated_monthly_savings": round(monthly_cost, 2),
                "detail": (
                    f"{size_gb} GB snapshot is {age_days} days old with no AMI association. "
                    f"Description: '{description or 'none'}'. "
                    f"Name: {name_tag or 'untagged'}. "
                    f"Consider a Data Lifecycle Manager policy to auto-expire old snapshots."
                ),
                "severity": _severity_from_savings(monthly_cost),
                "region": region,
                "account_id": account_id,
                "size_gb": size_gb,
                "age_days": age_days,
            })

    return findings


# ── Elastic IPs ───────────────────────────────────────────────────────────────

def check_elastic_ips(ec2_client: Any, region: str = "unknown") -> list[dict]:
    """
    Detect unassociated Elastic IPs. AWS charges $3.60/month per idle EIP.
    """
    findings: list[dict] = []

    try:
        resp = ec2_client.describe_addresses()
    except Exception as exc:
        log.warning("describe_addresses failed (region=%s): %s", region, exc)
        return findings

    for addr in resp.get("Addresses", []):
        allocation_id = addr.get("AllocationId", addr.get("PublicIp", "unknown"))
        public_ip = addr.get("PublicIp", "")
        association_id = addr.get("AssociationId")
        instance_id = addr.get("InstanceId")
        network_interface_id = addr.get("NetworkInterfaceId")

        # Unassociated if no association and not attached to instance or ENI
        if not association_id and not instance_id and not network_interface_id:
            findings.append({
                "resource_id": allocation_id,
                "resource_type": "Elastic IP",
                "waste_type": "unassociated_elastic_ip",
                "estimated_monthly_savings": _EIP_MONTHLY,
                "detail": (
                    f"Elastic IP {public_ip} is not associated with any instance "
                    f"or network interface. AWS charges ${_EIP_MONTHLY:.2f}/mo for idle EIPs. "
                    f"Release it if no longer needed."
                ),
                "severity": "low",
                "region": region,
                "account_id": None,
                "public_ip": public_ip,
            })

    return findings


# ── NAT Gateways ──────────────────────────────────────────────────────────────

def check_nat_gateways(
    ec2_client: Any,
    cw_client: Any,
    region: str = "unknown",
    lookback_days: int = 7,
    low_throughput_gb_per_day: float = 1.0,
) -> list[dict]:
    """
    Detect NAT Gateways with low throughput — they still cost ~$32/mo in fixed
    charges even with zero traffic. If a NAT GW processes <1 GB/day it's likely idle.
    """
    findings: list[dict] = []

    try:
        paginator = ec2_client.get_paginator("describe_nat_gateways")
        pages = paginator.paginate(
            Filter=[{"Name": "state", "Values": ["available"]}]
        )
    except Exception as exc:
        log.warning("describe_nat_gateways failed (region=%s): %s", region, exc)
        return findings

    now = datetime.now(timezone.utc)
    start = now - timedelta(days=lookback_days)
    period_seconds = 86400  # daily

    for page in pages:
        for nat in page.get("NatGateways", []):
            nat_id = nat["NatGatewayId"]
            vpc_id = nat.get("VpcId", "")
            subnet_id = nat.get("SubnetId", "")
            name_tag = next(
                (t["Value"] for t in nat.get("Tags", []) if t["Key"] == "Name"), ""
            )

            # Fetch BytesOutToDestination (egress through NAT GW)
            try:
                resp = cw_client.get_metric_statistics(
                    Namespace="AWS/NATGateway",
                    MetricName="BytesOutToDestination",
                    Dimensions=[{"Name": "NatGatewayId", "Value": nat_id}],
                    StartTime=start,
                    EndTime=now,
                    Period=period_seconds,
                    Statistics=["Sum"],
                )
                datapoints = resp.get("Datapoints", [])
            except Exception as exc:
                # A failed read is not zero traffic. Collapsing it to 0.0 flagged
                # every NAT gateway in the region as idle the moment CloudWatch
                # was unreachable or the IAM permission was missing, and the
                # trust envelope then stamped each one MEASURED/high. The sibling
                # detectors (EBS at :673, EC2 CPU at :1018) already skip on this
                # exact failure. BytesOutToDestination is the only evidence this
                # detector has, so without it there is no finding to make.
                log.debug("CW metrics failed for NAT GW %s: %s", nat_id, exc)
                continue

            if not datapoints:
                # The read SUCCEEDED and returned nothing, which is different from
                # the read failing: the gateway is new, or genuinely carrying no
                # traffic. That is a real observation.
                avg_bytes_per_day = 0.0
            else:
                total_bytes = sum(dp.get("Sum", 0) for dp in datapoints)
                avg_bytes_per_day = total_bytes / len(datapoints)

            avg_gb_per_day = avg_bytes_per_day / (1024 ** 3)

            if avg_gb_per_day < low_throughput_gb_per_day:
                # Savings: fixed hourly cost only (data processing cost is minimal at low volume)
                monthly_savings = _NAT_GW_BASE_MONTHLY
                findings.append({
                    "resource_id": nat_id,
                    "resource_type": "NAT Gateway",
                    "waste_type": "idle_nat_gateway",
                    "estimated_monthly_savings": round(monthly_savings, 2),
                    "detail": (
                        f"NAT Gateway {nat_id} in {subnet_id} (VPC: {vpc_id}) averaged "
                        f"{avg_gb_per_day:.3f} GB/day over {lookback_days} days "
                        f"(threshold: {low_throughput_gb_per_day} GB/day). "
                        f"Fixed cost ~${_NAT_GW_BASE_MONTHLY:.2f}/mo regardless of usage. "
                        f"Name: {name_tag or 'untagged'}. "
                        f"Consider consolidating to fewer AZs or using VPC endpoints."
                    ),
                    "severity": _severity_from_savings(monthly_savings),
                    "region": region,
                    "account_id": None,
                    "avg_gb_per_day": round(avg_gb_per_day, 4),
                    "vpc_id": vpc_id,
                })

    return findings


# ── RDS backup retention ──────────────────────────────────────────────────────

def check_rds_backups(rds_client: Any, region: str = "unknown", max_retention_days: int = 7) -> list[dict]:
    """
    Detect RDS instances with excessive backup retention.
    AWS keeps automated backups for the configured retention window and charges
    $0.095/GB-month for backup storage beyond 1x the provisioned database size.
    Most teams need 7 days max; 30+ day retention is almost always accidental.
    """
    findings: list[dict] = []

    try:
        paginator = rds_client.get_paginator("describe_db_instances")
        pages = paginator.paginate()
    except Exception as exc:
        log.warning("describe_db_instances failed (region=%s): %s", region, exc)
        return findings

    for page in pages:
        for db in page.get("DBInstances", []):
            db_id = db["DBInstanceIdentifier"]
            retention = db.get("BackupRetentionPeriod", 0)
            engine = db.get("Engine", "unknown")
            instance_class = db.get("DBInstanceClass", "unknown")
            allocated_gb = db.get("AllocatedStorage", 0)

            if retention <= max_retention_days:
                continue

            # Extra retention beyond the recommended window
            excess_days = retention - max_retention_days
            # Rough estimate: each extra day of retention ≈ 1x allocated storage / 30
            excess_gb = allocated_gb * (excess_days / 30)
            monthly_savings = excess_gb * _RDS_BACKUP_EXTRA_PER_GB_MONTH

            findings.append({
                "resource_id": db_id,
                "resource_type": "RDS Instance",
                "waste_type": "excessive_rds_backup_retention",
                "estimated_monthly_savings": round(monthly_savings, 2),
                "detail": (
                    f"RDS {db_id} ({engine}, {instance_class}, {allocated_gb} GB) "
                    f"has {retention}-day backup retention (recommended: {max_retention_days} days). "
                    f"{excess_days} extra days ≈ {excess_gb:.0f} GB extra backup storage "
                    f"at ~${_RDS_BACKUP_EXTRA_PER_GB_MONTH}/GB-mo. "
                    f"Reduce retention period in DB parameter settings."
                ),
                "severity": _severity_from_savings(monthly_savings),
                "region": region,
                "account_id": None,
                "engine": engine,
                "instance_class": instance_class,
                "allocated_storage_gb": allocated_gb,
                "retention_days": retention,
                "recommended_retention_days": max_retention_days,
            })

    return findings


# ── CloudTrail waste ──────────────────────────────────────────────────────────

def check_cloudtrail_waste(
    cloudtrail_client: Any,
    region: str = "unknown",
) -> list[dict]:
    """
    Detect CloudTrail waste patterns:
    - Trails recording in all regions when only one is needed
    - Data events enabled on S3/Lambda when nobody has set up analysis (expensive!)
    - Multi-region trails duplicating management events into separate S3 prefixes

    CloudTrail management events: free for first trail, $2/100k events for additional.
    Data events: $0.10/100k events — these add up FAST on busy S3 buckets.
    """
    findings: list[dict] = []

    try:
        resp = cloudtrail_client.describe_trails(includeShadowTrails=False)
        trails = resp.get("trailList", [])
    except Exception as exc:
        log.warning("describe_trails failed (region=%s): %s", region, exc)
        return findings

    management_event_trails = []
    for trail in trails:
        trail_arn = trail.get("TrailARN", "")
        trail_name = trail.get("Name", "unknown")
        is_multi_region = trail.get("IsMultiRegionTrail", False)

        # Check event selectors for data events
        try:
            sel_resp = cloudtrail_client.get_event_selectors(TrailName=trail_arn)
            event_selectors = sel_resp.get("EventSelectors", [])

            for selector in event_selectors:
                data_resources = selector.get("DataResources", [])
                if data_resources:
                    # Estimate: data events are expensive — flag as high severity
                    findings.append({
                        "resource_id": trail_arn,
                        "resource_type": "CloudTrail Trail",
                        "waste_type": "cloudtrail_data_events_enabled",
                        "estimated_monthly_savings": 50.0,  # conservative — can be $1000s on busy S3
                        "detail": (
                            f"Trail '{trail_name}' has data events enabled for: "
                            f"{[r.get('Type') for r in data_resources]}. "
                            f"Data events cost $0.10/100k events — on a busy S3 bucket this can "
                            f"reach hundreds of dollars/month. Only enable if actively consuming "
                            f"these logs in a SIEM or security tool. "
                            f"Multi-region: {is_multi_region}."
                        ),
                        "severity": "high",
                        "region": region,
                        "account_id": None,
                        "trail_name": trail_name,
                        "is_multi_region": is_multi_region,
                    })

            # Track management event trails for duplicate detection
            records_mgmt = any(
                selector.get("IncludeManagementEvents", False)
                for selector in event_selectors
            )
            if records_mgmt or not event_selectors:
                management_event_trails.append(trail_name)

        except Exception as exc:
            log.debug("get_event_selectors failed for %s: %s", trail_name, exc)

        # Get trail status — check if trail is actually logging
        try:
            status_resp = cloudtrail_client.get_trail_status(Name=trail_arn)
            is_logging = status_resp.get("IsLogging", False)
            if not is_logging:
                findings.append({
                    "resource_id": trail_arn,
                    "resource_type": "CloudTrail Trail",
                    "waste_type": "cloudtrail_stopped_but_s3_bucket_costs_persist",
                    "estimated_monthly_savings": _UNKNOWN_SAVINGS,
                    "detail": (
                        f"Trail '{trail_name}' exists but is NOT currently logging. "
                        f"The S3 bucket and log group may still be incurring storage costs. "
                        f"Delete the trail if truly unused, or re-enable logging."
                    ),
                    "severity": "low",
                    "region": region,
                    "account_id": None,
                    "trail_name": trail_name,
                })
        except Exception:
            pass

    # Flag duplicate management event trails (more than 1 trail = paying for duplicates)
    if len(management_event_trails) > 1:
        findings.append({
            "resource_id": f"region:{region}",
            "resource_type": "CloudTrail Region",
            "waste_type": "duplicate_cloudtrail_management_events",
            "estimated_monthly_savings": 20.0,  # rough estimate per extra trail
            "detail": (
                f"{len(management_event_trails)} trails are recording management events in {region}: "
                f"{management_event_trails}. Only the first trail per region is free — "
                f"additional trails cost $2/100k events. Consolidate to one trail."
            ),
            "severity": "medium",
            "region": region,
            "account_id": None,
            "trail_names": management_event_trails,
        })

    return findings


# ── CloudWatch Log Groups (infinite retention) ────────────────────────────────

def check_cloudwatch_logs(logs_client: Any, region: str = "unknown") -> list[dict]:
    """
    Detect CloudWatch Log Groups with no retention policy (infinite retention).
    Stored logs cost $0.03/GB-month. Many teams have VPC Flow Logs, Lambda logs,
    and ECS logs accumulating for years with no expiry.

    Recommends appropriate retention by log group name pattern.
    """
    _RETENTION_RECOMMENDATIONS = {
        "vpc-flow": 30,
        "flow-log": 30,
        "/aws/lambda": 14,
        "/aws/rds": 30,
        "/aws/ecs": 30,
        "/aws/eks": 30,
        "/aws/codebuild": 30,
        "cloudtrail": 90,
        "access-log": 90,
        "audit": 365,
        "security": 365,
    }

    findings: list[dict] = []

    try:
        paginator = logs_client.get_paginator("describe_log_groups")
        pages = paginator.paginate()
    except Exception as exc:
        log.warning("describe_log_groups failed (region=%s): %s", region, exc)
        return findings

    for page in pages:
        for lg in page.get("logGroups", []):
            group_name = lg.get("logGroupName", "")
            retention_days = lg.get("retentionInDays")  # None = infinite
            stored_bytes = lg.get("storedBytes", 0)

            if retention_days is not None:
                continue  # Has a retention policy — fine

            # Estimate monthly cost from stored bytes
            stored_gb = stored_bytes / (1024 ** 3)
            monthly_cost = stored_gb * _CW_LOGS_STORAGE_PER_GB_MONTH

            # Recommend retention period based on log group name patterns
            recommended_days = 30  # default
            for pattern, days in _RETENTION_RECOMMENDATIONS.items():
                if pattern in group_name.lower():
                    recommended_days = days
                    break

            findings.append({
                "resource_id": group_name,
                "resource_type": "CloudWatch Log Group",
                "waste_type": "log_group_infinite_retention",
                "estimated_monthly_savings": round(monthly_cost, 2),
                "detail": (
                    f"Log group '{group_name}' has no retention policy (infinite). "
                    f"Currently storing {stored_gb:.2f} GB (${monthly_cost:.2f}/mo). "
                    f"Recommended retention: {recommended_days} days based on log type. "
                    f"Set via: aws logs put-retention-policy --log-group-name '{group_name}' "
                    f"--retention-in-days {recommended_days}"
                ),
                "severity": _severity_from_savings(monthly_cost),
                "region": region,
                "account_id": None,
                "stored_gb": round(stored_gb, 3),
                "recommended_retention_days": recommended_days,
            })

    return findings


# ── S3 storage class ──────────────────────────────────────────────────────────

def check_s3_storage_class(
    s3_client: Any,
    cw_client: Any,
    region: str = "unknown",
    min_size_gb: float = 10.0,
    lookback_days: int = 30,
) -> list[dict]:
    """
    Detect S3 buckets storing data in STANDARD storage class with low access
    frequency where a cheaper storage class would actually save money.

    We do NOT blindly recommend Intelligent-Tiering — its $0.0025/1k objects/month
    monitoring fee can exceed the storage savings for buckets with many small objects
    or high request rates. Instead:

    1. Calculate Intelligent-Tiering monitoring cost from object count.
    2. Only recommend IT if net savings (storage reduction minus monitoring) > $5/mo.
    3. For write-once / read-rarely patterns (very low GETs), recommend STANDARD-IA
       instead ($0.0125/GB-mo, no monitoring fee, retrieval fee applies).
    4. If object count is unavailable, skip rather than give a bad recommendation.

    STANDARD:           $0.023/GB-mo
    STANDARD-IA:        $0.0125/GB-mo + $0.01/GB retrieval
    INTELLIGENT-TIERING: $0.023/GB-mo (frequent) / $0.0125/GB-mo (infrequent)
                         + $0.0025/1000 objects/mo monitoring
    """
    findings: list[dict] = []

    try:
        resp = s3_client.list_buckets()
        buckets = resp.get("Buckets", [])
    except Exception as exc:
        log.warning("list_buckets failed: %s", exc)
        return findings

    now = datetime.now(timezone.utc)
    start = now - timedelta(days=lookback_days)

    for bucket in buckets:
        bucket_name = bucket["Name"]

        # Get bucket size via CloudWatch
        try:
            size_resp = cw_client.get_metric_statistics(
                Namespace="AWS/S3",
                MetricName="BucketSizeBytes",
                Dimensions=[
                    {"Name": "BucketName", "Value": bucket_name},
                    {"Name": "StorageType", "Value": "StandardStorage"},
                ],
                StartTime=start,
                EndTime=now,
                Period=86400,
                Statistics=["Average"],
            )
            size_datapoints = size_resp.get("Datapoints", [])
            if not size_datapoints:
                continue
            avg_bytes = max(dp.get("Average", 0) for dp in size_datapoints)
            size_gb = avg_bytes / (1024 ** 3)
        except Exception:
            continue

        if size_gb < min_size_gb:
            continue

        # Check request frequency (GetRequests)
        try:
            req_resp = cw_client.get_metric_statistics(
                Namespace="AWS/S3",
                MetricName="GetRequests",
                Dimensions=[
                    {"Name": "BucketName", "Value": bucket_name},
                    {"Name": "FilterId", "Value": "AllRequests"},
                ],
                StartTime=start,
                EndTime=now,
                Period=86400,
                Statistics=["Sum"],
            )
            req_datapoints = req_resp.get("Datapoints", [])
            total_gets = sum(dp.get("Sum", 0) for dp in req_datapoints)
            avg_daily_gets = total_gets / lookback_days if lookback_days else 0
        except Exception:
            # S3 request metrics require request metrics to be enabled on the bucket
            avg_daily_gets = None

        # Only flag confirmed low-access buckets — skip if we can't verify
        is_low_access = avg_daily_gets is not None and avg_daily_gets < 100
        if not is_low_access:
            continue

        monthly_standard_cost = size_gb * _S3_STANDARD_PER_GB_MONTH

        # Get object count to compute Intelligent-Tiering monitoring cost
        try:
            obj_resp = cw_client.get_metric_statistics(
                Namespace="AWS/S3",
                MetricName="NumberOfObjects",
                Dimensions=[
                    {"Name": "BucketName", "Value": bucket_name},
                    {"Name": "StorageType", "Value": "AllStorageTypes"},
                ],
                StartTime=start,
                EndTime=now,
                Period=86400,
                Statistics=["Average"],
            )
            obj_datapoints = obj_resp.get("Datapoints", [])
            object_count = max((dp.get("Average", 0) for dp in obj_datapoints), default=0)
        except Exception:
            object_count = 0

        # Intelligent-Tiering: monitoring fee = $0.0025 per 1,000 objects/mo
        it_monitoring_cost = (object_count / 1000) * 0.0025
        monthly_it_storage_cost = size_gb * _S3_INT_TIER_PER_GB_MONTH
        monthly_it_total = monthly_it_storage_cost + it_monitoring_cost
        it_net_savings = monthly_standard_cost - monthly_it_total

        # STANDARD-IA: no monitoring fee, retrieval cost applies but negligible for low-access
        monthly_ia_cost = size_gb * 0.0125
        ia_net_savings = monthly_standard_cost - monthly_ia_cost

        # Only flag if there's meaningful ROI (>$5/mo net) and we can be specific
        if object_count > 0 and it_net_savings > 5:
            # IT makes sense: savings outweigh monitoring cost
            recommendation = "INTELLIGENT_TIERING"
            net_savings = it_net_savings
            detail = (
                f"S3 bucket '{bucket_name}': {size_gb:.1f} GB in STANDARD "
                f"(${monthly_standard_cost:.2f}/mo). {int(object_count):,} objects. "
                f"IT monitoring cost: ${it_monitoring_cost:.2f}/mo. "
                f"Net saving with Intelligent-Tiering: ${it_net_savings:.2f}/mo. "
                f"Command: aws s3api put-bucket-intelligent-tiering-configuration "
                f"--bucket {bucket_name} --id default --intelligent-tiering-configuration "
                f"'{{\"Id\":\"default\",\"Status\":\"Enabled\",\"Tierings\":[{{\"Days\":90,\"AccessTier\":\"ARCHIVE_ACCESS\"}}]}}'"
            )
        elif ia_net_savings > 5 and avg_daily_gets < 10:
            # Very low access: STANDARD-IA is better — no monitoring fee, retrieval is cheap
            recommendation = "STANDARD_IA"
            net_savings = ia_net_savings
            detail = (
                f"S3 bucket '{bucket_name}': {size_gb:.1f} GB in STANDARD "
                f"(${monthly_standard_cost:.2f}/mo), avg {avg_daily_gets:.0f} GETs/day. "
                f"STANDARD-IA costs ${monthly_ia_cost:.2f}/mo with no monitoring fee. "
                f"Net saving: ${ia_net_savings:.2f}/mo. "
                f"Note: Intelligent-Tiering monitoring would cost ${it_monitoring_cost:.2f}/mo "
                f"{'(not worth it for this object count)' if object_count > 0 else '(object count unknown)'}. "
                f"Command: aws s3 cp s3://{bucket_name} s3://{bucket_name} "
                f"--recursive --storage-class STANDARD_IA"
            )
        else:
            # Either monitoring cost eats the savings or bucket is too small to matter
            continue

        findings.append({
            "resource_id": bucket_name,
            "resource_type": "S3 Bucket",
            "waste_type": "s3_suboptimal_storage_class",
            "estimated_monthly_savings": round(net_savings, 2),
            "recommendation": recommendation,
            "detail": detail,
            "severity": _severity_from_savings(net_savings),
            "region": region,
            "account_id": None,
            "size_gb": round(size_gb, 2),
            "object_count": int(object_count) if object_count else None,
            "avg_daily_gets": round(avg_daily_gets, 1),
            "it_monitoring_cost_mo": round(it_monitoring_cost, 2) if object_count else None,
        })

    return findings


# ── Lambda memory over-provisioning ──────────────────────────────────────────

def check_lambda_memory(
    lambda_client: Any,
    cw_client: Any,
    region: str = "unknown",
    lookback_days: int = 14,
) -> list[dict]:
    """
    Detect Lambda functions where configured memory > 2x the p99 actual usage.

    CloudWatch publishes max_memory_used in the REPORT log lines, but this
    isn't a standard metric. We use the Lambda Insights metric
    `memory_utilization` if available, falling back to the heuristic that
    if Duration p99 is very short the function is likely not using its memory.

    Also checks for functions with zero invocations over the lookback period
    (dead functions still charge for storage).
    """
    findings: list[dict] = []

    try:
        paginator = lambda_client.get_paginator("list_functions")
        pages = paginator.paginate()
    except Exception as exc:
        log.warning("list_functions failed (region=%s): %s", region, exc)
        return findings

    for page in pages:
        for fn in page.get("Functions", []):
            fn_name = fn["FunctionName"]
            configured_memory_mb = fn.get("MemorySize", 128)
            runtime = fn.get("Runtime", "unknown")
            code_size_mb = fn.get("CodeSize", 0) / (1024 * 1024)

            dims = [{"Name": "FunctionName", "Value": fn_name}]

            # Check invocations — zero invocations = potentially dead function
            try:
                inv_resp = cw_client.get_metric_statistics(
                    Namespace="AWS/Lambda",
                    MetricName="Invocations",
                    Dimensions=dims,
                    StartTime=datetime.now(timezone.utc) - timedelta(days=lookback_days),
                    EndTime=datetime.now(timezone.utc),
                    Period=86400 * lookback_days,
                    Statistics=["Sum"],
                )
                inv_datapoints = inv_resp.get("Datapoints", [])
                total_invocations = sum(dp.get("Sum", 0) for dp in inv_datapoints)
            except Exception:
                total_invocations = None

            if total_invocations == 0:
                findings.append({
                    "resource_id": fn_name,
                    "resource_type": "Lambda Function",
                    "waste_type": "lambda_zero_invocations",
                    "estimated_monthly_savings": _UNKNOWN_SAVINGS,
                    "detail": (
                        f"Lambda function '{fn_name}' ({runtime}) had 0 invocations "
                        f"over the past {lookback_days} days. "
                        f"Code size: {code_size_mb:.1f} MB. "
                        f"Consider deleting if no longer needed — stored code doesn't cost "
                        f"much but orphaned functions indicate technical debt."
                    ),
                    "severity": "low",
                    "region": region,
                    "account_id": None,
                    "runtime": runtime,
                    "configured_memory_mb": configured_memory_mb,
                    "total_invocations": 0,
                })
                continue

            # Try Lambda Insights for actual memory usage
            max_memory_used_mb = None
            try:
                mem_resp = cw_client.get_metric_statistics(
                    Namespace="LambdaInsights",
                    MetricName="memory_utilization",
                    Dimensions=dims,
                    StartTime=datetime.now(timezone.utc) - timedelta(days=lookback_days),
                    EndTime=datetime.now(timezone.utc),
                    Period=86400 * lookback_days,
                    Statistics=["Maximum"],
                )
                mem_datapoints = mem_resp.get("Datapoints", [])
                if mem_datapoints:
                    max_utilization_pct = max(dp.get("Maximum", 0) for dp in mem_datapoints)
                    max_memory_used_mb = configured_memory_mb * (max_utilization_pct / 100.0)
            except Exception:
                pass

            if max_memory_used_mb is not None and max_memory_used_mb > 0:
                # We have real data from Lambda Insights
                ratio = configured_memory_mb / max_memory_used_mb
                if ratio >= 2.0:
                    # Recommend sizing down to 1.5x actual usage (headroom)
                    recommended_mb = _next_lambda_memory_size(int(max_memory_used_mb * 1.5))
                    memory_savings_pct = (configured_memory_mb - recommended_mb) / configured_memory_mb

                    # Lambda pricing: $0.0000166667/GB-second
                    # Savings depend on invocation volume — use relative savings
                    estimated_savings = 10.0 * memory_savings_pct  # rough $10 base * savings %

                    findings.append({
                        "resource_id": fn_name,
                        "resource_type": "Lambda Function",
                        "waste_type": "lambda_memory_overprovisioned",
                        "estimated_monthly_savings": round(estimated_savings, 2),
                        "detail": (
                            f"Lambda function '{fn_name}' is configured for {configured_memory_mb} MB "
                            f"but p99 actual usage (via Lambda Insights) is {max_memory_used_mb:.0f} MB "
                            f"({ratio:.1f}x over-provisioned). "
                            f"Recommended: {recommended_mb} MB (1.5x headroom). "
                            f"This reduces cost by ~{memory_savings_pct*100:.0f}%. "
                            f"Test with AWS Lambda Power Tuning tool for optimal size."
                        ),
                        "severity": _severity_from_savings(estimated_savings),
                        "region": region,
                        "account_id": None,
                        "runtime": runtime,
                        "configured_memory_mb": configured_memory_mb,
                        "max_used_memory_mb": round(max_memory_used_mb, 1),
                        "recommended_memory_mb": recommended_mb,
                        "total_invocations": total_invocations,
                    })

    return findings


def _next_lambda_memory_size(target_mb: int) -> int:
    """Round up to nearest valid Lambda memory increment (64 MB steps above 128)."""
    if target_mb <= 128:
        return 128
    remainder = target_mb % 64
    return target_mb if remainder == 0 else target_mb + (64 - remainder)


# ── Idle EC2 instances ────────────────────────────────────────────────────────

# vCPU per instance-size suffix. Used to size idle-EC2 savings. The old parser
# did int(inst_type.split(".")[1][0]) which threw on "m5.large" (int("l")) and
# silently fell back to 2 vCPU for everything, making savings noise.
_SIZE_VCPU: dict[str, int] = {
    "nano": 1, "micro": 1, "small": 1, "medium": 1, "large": 2,
    "xlarge": 4, "2xlarge": 8, "3xlarge": 12, "4xlarge": 16, "6xlarge": 24,
    "8xlarge": 32, "9xlarge": 36, "10xlarge": 40, "12xlarge": 48, "16xlarge": 64,
    "18xlarge": 72, "24xlarge": 96, "32xlarge": 128, "48xlarge": 192,
    # 'metal' is intentionally omitted: metal vCPU counts vary widely by family
    # (mac1.metal=12, z1d.metal=48, i3.metal=72, m5.metal=96). Mapping all of them
    # to 96 over-estimated idle savings, the exact failure this table was added to
    # fix. An unknown suffix falls through to the conservative default below.
}

# Average hourly NetworkOut above which an instance is treated as doing real work
# (network/disk-bound or warm-standby), so a low CPU reading is NOT "idle".
_IDLE_NET_BYTES_PER_HR: float = 100 * 1024 ** 2  # ~100 MB/hr


def _vcpus_from_type(inst_type: str) -> int:
    """Best-effort vCPU count from an EC2 instance type (e.g. m5.4xlarge -> 16)."""
    try:
        size = inst_type.split(".", 1)[1]
        return _SIZE_VCPU.get(size, 2)
    except Exception:
        return 2


def check_idle_ec2(
    ec2_client: Any,
    cw_client: Any,
    region: str = "unknown",
    cpu_threshold_pct: float = 5.0,
    lookback_days: int = 14,
) -> list[dict]:
    """
    Detect EC2 instances with average CPU < cpu_threshold_pct over lookback_days.
    Goes beyond Compute Optimizer by checking ALL instances (not just those already
    flagged) and using a more aggressive threshold.

    Savings estimate based on rough on-demand pricing (actual savings depend on
    instance type — we use a conservative $50/mo baseline for a t3.medium equivalent).
    """
    _APPROX_MONTHLY_PER_VCPU = 15.0  # very rough: $15/vCPU/month on-demand

    findings: list[dict] = []

    try:
        paginator = ec2_client.get_paginator("describe_instances")
        pages = paginator.paginate(
            Filters=[{"Name": "instance-state-name", "Values": ["running"]}]
        )
    except Exception as exc:
        log.warning("describe_instances failed (region=%s): %s", region, exc)
        return findings

    now = datetime.now(timezone.utc)
    start = now - timedelta(days=lookback_days)

    for page in pages:
        for reservation in page.get("Reservations", []):
            for inst in reservation.get("Instances", []):
                inst_id = inst["InstanceId"]
                inst_type = inst.get("InstanceType", "unknown")
                name_tag = next(
                    (t["Value"] for t in inst.get("Tags", []) if t["Key"] == "Name"), ""
                )
                launch_time = inst.get("LaunchTime")

                # Skip instances launched less than lookback_days ago — not enough data
                if launch_time:
                    if launch_time.tzinfo is None:
                        launch_time = launch_time.replace(tzinfo=timezone.utc)
                    if (now - launch_time).days < lookback_days:
                        continue

                # Fetch CPU utilization
                try:
                    resp = cw_client.get_metric_statistics(
                        Namespace="AWS/EC2",
                        MetricName="CPUUtilization",
                        Dimensions=[{"Name": "InstanceId", "Value": inst_id}],
                        StartTime=start,
                        EndTime=now,
                        Period=3600,  # hourly
                        Statistics=["Average"],
                    )
                    datapoints = resp.get("Datapoints", [])
                except Exception as exc:
                    log.debug("CW CPU metrics failed for %s: %s", inst_id, exc)
                    continue

                if not datapoints:
                    continue

                avg_cpu = sum(dp.get("Average", 0) for dp in datapoints) / len(datapoints)
                max_cpu = max(dp.get("Average", 0) for dp in datapoints)

                if avg_cpu >= cpu_threshold_pct:
                    continue

                # Low CPU alone does not mean idle. Batch, network- or disk-bound
                # workloads and warm-standby DR boxes run with low CPU but real
                # I/O. Skip flagging when network shows sustained activity, so a
                # working instance is not falsely called idle.
                try:
                    # Sum over a 1-hour Period gives total bytes per hour. Averaging
                    # the per-collection-interval samples (Statistics=Average) would
                    # return mean bytes-per-sample, ~12x too low against a per-hour
                    # threshold, so the guard would never fire. Use Sum.
                    net_resp = cw_client.get_metric_statistics(
                        Namespace="AWS/EC2",
                        MetricName="NetworkOut",
                        Dimensions=[{"Name": "InstanceId", "Value": inst_id}],
                        StartTime=start,
                        EndTime=now,
                        Period=3600,
                        Statistics=["Sum"],
                    )
                    net_dps = net_resp.get("Datapoints", [])
                    avg_net_per_hr = (
                        sum(dp.get("Sum", 0) for dp in net_dps) / len(net_dps)
                        if net_dps else 0.0
                    )
                    net_unavailable = False
                except Exception as exc:
                    # 0.0 here does not mean "no traffic", it means "we could not
                    # look". The very next line is the guard that protects a busy
                    # instance from being called idle, so a failed read used to
                    # DISABLE the check that would have saved it. Low CPU is real
                    # evidence and was measured, so the finding survives; it just
                    # stops claiming to be measured.
                    log.debug("CW NetworkOut failed for %s: %s", inst_id, exc)
                    avg_net_per_hr = 0.0
                    net_unavailable = True

                if not net_unavailable and avg_net_per_hr > _IDLE_NET_BYTES_PER_HR:
                    continue  # network-active: treat as in-use, not idle

                vcpus = _vcpus_from_type(inst_type)
                monthly_savings = vcpus * _APPROX_MONTHLY_PER_VCPU

                findings.append({
                    "resource_id": inst_id,
                    "resource_type": "EC2 Instance",
                    "waste_type": "idle_ec2_low_cpu",
                    "estimated_monthly_savings": round(monthly_savings, 2),
                    "detail": (
                        f"EC2 instance {inst_id} ({inst_type}) averaged {avg_cpu:.1f}% CPU "
                        f"(peak: {max_cpu:.1f}%) over {lookback_days} days "
                        f"— well below the {cpu_threshold_pct}% idle threshold. "
                        f"Name: {name_tag or 'untagged'}. "
                        f"Consider stopping, downsizing, or terminating. "
                        f"Check Network/Disk metrics before terminating — "
                        f"some instances are disk/network bound with low CPU."
                    ),
                    "severity": _severity_from_savings(monthly_savings),
                    "region": region,
                    "account_id": None,
                    "instance_type": inst_type,
                    "avg_cpu_pct": round(avg_cpu, 2),
                    "max_cpu_pct": round(max_cpu, 2),
                    "name": name_tag,
                    "lookback_days": lookback_days,
                    # Carries provenance to waste_evidence.annotate, which refuses
                    # to stamp a finding MEASURED when its metric could not be read.
                    **({"metrics_unavailable": True,
                        "metrics_unavailable_reason":
                            "NetworkOut could not be read, so 'not network-active' "
                            "is an assumption. Low CPU was measured; the traffic "
                            "check that would confirm it was not."}
                       if net_unavailable else {}),
                })

    return findings


# ── RDS rightsizing ───────────────────────────────────────────────────────────

# Approximate on-demand hourly prices per instance class (us-east-1, single-AZ).
# Used only when Cost Explorer data is unavailable.
_RDS_HOURLY: dict[str, float] = {
    "db.t3.micro": 0.017, "db.t3.small": 0.034, "db.t3.medium": 0.068,
    "db.t3.large": 0.136, "db.t3.xlarge": 0.272, "db.t3.2xlarge": 0.544,
    "db.m5.large": 0.171, "db.m5.xlarge": 0.342, "db.m5.2xlarge": 0.684,
    "db.m5.4xlarge": 1.368,
    "db.m6g.large": 0.162, "db.m6g.xlarge": 0.325, "db.m6g.2xlarge": 0.650,
    "db.r5.large": 0.240, "db.r5.xlarge": 0.480, "db.r5.2xlarge": 0.960,
    "db.r6g.large": 0.228, "db.r6g.xlarge": 0.456,
}

_RDS_DOWNSIZE: dict[str, str] = {
    "db.t3.medium": "db.t3.small",    "db.t3.large": "db.t3.medium",
    "db.t3.xlarge": "db.t3.large",    "db.t3.2xlarge": "db.t3.xlarge",
    "db.m5.xlarge": "db.m5.large",    "db.m5.2xlarge": "db.m5.xlarge",
    "db.m5.4xlarge": "db.m5.2xlarge",
    "db.m6g.xlarge": "db.m6g.large",  "db.m6g.2xlarge": "db.m6g.xlarge",
    "db.r5.xlarge": "db.r5.large",    "db.r5.2xlarge": "db.r5.xlarge",
    "db.r6g.xlarge": "db.r6g.large",
}


def check_rds_rightsizing(
    rds_client: Any,
    cw_client: Any,
    region: str = "unknown",
    cpu_threshold_pct: float = 20.0,
    lookback_days: int = 14,
) -> list[dict]:
    """
    Detect RDS instances with consistently low CPU utilization.

    RDS Compute Optimizer integration is limited, so we use CloudWatch
    CPUUtilization as the primary signal. Instances with average CPU below
    cpu_threshold_pct over lookback_days are candidates for downsizing.

    Excludes Aurora Serverless (scales automatically) and read replicas.
    """
    findings: list[dict] = []

    try:
        paginator = rds_client.get_paginator("describe_db_instances")
        pages = paginator.paginate()
    except Exception as exc:
        log.warning("describe_db_instances failed (region=%s): %s", region, exc)
        return findings

    now = datetime.now(timezone.utc)
    start = now - timedelta(days=lookback_days)

    for page in pages:
        for db in page.get("DBInstances", []):
            db_id = db["DBInstanceIdentifier"]
            db_class = db.get("DBInstanceClass", "")
            engine = db.get("Engine", "")
            status = db.get("DBInstanceStatus", "")
            multi_az = db.get("MultiAZ", False)

            if status != "available":
                continue
            if "aurora-serverless" in engine:
                continue
            if db.get("ReadReplicaSourceDBInstanceIdentifier"):
                continue

            try:
                resp = cw_client.get_metric_statistics(
                    Namespace="AWS/RDS",
                    MetricName="CPUUtilization",
                    Dimensions=[{"Name": "DBInstanceIdentifier", "Value": db_id}],
                    StartTime=start,
                    EndTime=now,
                    Period=3600,
                    Statistics=["Average"],
                )
                datapoints = resp.get("Datapoints", [])
            except Exception as exc:
                log.debug("CW CPU metrics failed for RDS %s: %s", db_id, exc)
                continue

            if not datapoints or len(datapoints) < 24:
                continue

            avg_cpu = sum(dp.get("Average", 0) for dp in datapoints) / len(datapoints)
            max_cpu = max(dp.get("Average", 0) for dp in datapoints)

            if avg_cpu >= cpu_threshold_pct:
                continue

            recommended_class = _RDS_DOWNSIZE.get(db_class)
            if not recommended_class:
                continue

            current_hourly = _RDS_HOURLY.get(db_class, 0.0)
            recommended_hourly = _RDS_HOURLY.get(recommended_class, 0.0)
            factor = 2.0 if multi_az else 1.0
            monthly_savings = (current_hourly - recommended_hourly) * 730 * factor

            if monthly_savings <= 0:
                continue

            findings.append({
                "resource_id": db_id,
                "resource_type": "RDS Instance",
                "waste_type": "rds_overprovisioned",
                "estimated_monthly_savings": round(monthly_savings, 2),
                "detail": (
                    f"RDS instance '{db_id}' ({db_class}, {engine}) averaged "
                    f"{avg_cpu:.1f}% CPU (peak: {max_cpu:.1f}%) over {lookback_days} days. "
                    f"Recommend downsizing to {recommended_class}. "
                    f"{'Multi-AZ: savings doubled. ' if multi_az else ''}"
                    f"Verify FreeStorageSpace and DatabaseConnections before resizing."
                ),
                "severity": _severity_from_savings(monthly_savings),
                "region": region,
                "account_id": None,
                "current_class": db_class,
                "recommended_class": recommended_class,
                "engine": engine,
                "multi_az": multi_az,
                "avg_cpu_pct": round(avg_cpu, 2),
                "max_cpu_pct": round(max_cpu, 2),
            })

    return findings


def check_rds_idle(
    rds_client: Any,
    cw_client: Any,
    region: str = "unknown",
    connection_threshold: float = 1.0,
    lookback_days: int = 14,
) -> list[dict]:
    """
    Detect RDS instances with near-zero database connections over the lookback
    period. Zero-connection instances are likely unused and can be stopped or deleted.
    """
    findings: list[dict] = []

    try:
        paginator = rds_client.get_paginator("describe_db_instances")
        pages = paginator.paginate()
    except Exception as exc:
        log.warning("describe_db_instances failed (region=%s): %s", region, exc)
        return findings

    now = datetime.now(timezone.utc)
    start = now - timedelta(days=lookback_days)

    for page in pages:
        for db in page.get("DBInstances", []):
            db_id = db["DBInstanceIdentifier"]
            db_class = db.get("DBInstanceClass", "")
            engine = db.get("Engine", "")
            status = db.get("DBInstanceStatus", "")

            if status != "available":
                continue

            try:
                resp = cw_client.get_metric_statistics(
                    Namespace="AWS/RDS",
                    MetricName="DatabaseConnections",
                    Dimensions=[{"Name": "DBInstanceIdentifier", "Value": db_id}],
                    StartTime=start,
                    EndTime=now,
                    Period=86400,
                    Statistics=["Maximum"],
                )
                datapoints = resp.get("Datapoints", [])
            except Exception as exc:
                log.debug("CW connections failed for RDS %s: %s", db_id, exc)
                continue

            if not datapoints or len(datapoints) < 7:
                continue

            max_connections = max(dp.get("Maximum", 0) for dp in datapoints)

            if max_connections >= connection_threshold:
                continue

            multi_az = db.get("MultiAZ", False)
            # Do NOT fabricate a price for an unknown class. The idle signal (no
            # connections) is measured and real; the dollar is not, so an unknown
            # class stays unpriced rather than emitting a made-up $0.10/hr, which
            # on a large instance is off by 100x. A wrong number that looks real
            # is worse than an honest "cost unknown".
            current_hourly = _RDS_HOURLY.get(db_class)
            if current_hourly is None:
                monthly_cost = None
                savings_val = None
                cost_txt = (f"cost unknown ({db_class} is not in nable's price table; "
                            f"check the real rate in Cost Explorer)")
                severity = "unknown"
            else:
                monthly_cost = current_hourly * 730 * (2 if multi_az else 1)
                savings_val = round(monthly_cost, 2)
                cost_txt = f"~${monthly_cost:.0f}/mo"
                severity = _severity_from_savings(monthly_cost)

            findings.append({
                "resource_id": db_id,
                "resource_type": "RDS Instance",
                "waste_type": "rds_idle_no_connections",
                "estimated_monthly_savings": savings_val,
                "unpriced": savings_val is None,
                "detail": (
                    f"RDS instance '{db_id}' ({db_class}, {engine}) had "
                    f"max {max_connections:.0f} connections over the past {lookback_days} days. "
                    f"Running cost: {cost_txt}. "
                    f"Consider stopping (preserves data) or deleting with a final snapshot."
                ),
                "severity": severity,
                "region": region,
                "account_id": None,
                "current_class": db_class,
                "engine": engine,
                "max_connections_14d": max_connections,
                "estimated_monthly_cost": savings_val,
            })

    return findings


def scan_all_regions_rds_idle(regions: list[str] | None = None) -> list[dict]:
    """check_rds_idle needs its own rds/cloudwatch clients per region, unlike the
    universal aws_client the other run_full_cost_audit scanners share. This wraps
    the per-region loop into one callable with the same fn(**kwargs) shape as
    every other scanner, so it plugs into the audit's scanner list and its tests
    the same way."""
    import boto3

    scan_regions = regions or ["us-east-1", "us-west-2", "eu-west-1", "eu-central-1"]
    out: list[dict] = []
    for r in scan_regions:
        try:
            out.extend(check_rds_idle(
                boto3.client("rds", region_name=r),
                boto3.client("cloudwatch", region_name=r),
                r,
            ))
        except Exception as exc:
            log.warning("RDS idle scan failed for region %s: %s", r, exc)
    return out


# ── Load balancer waste ───────────────────────────────────────────────────────

_ALB_HOURLY = 0.008
_NLB_HOURLY = 0.008
_CLB_HOURLY = 0.025


def check_idle_load_balancers(
    elbv2_client: Any,
    elb_client: Any,
    cw_client: Any,
    region: str = "unknown",
    lookback_days: int = 14,
    request_threshold: float = 100.0,
) -> list[dict]:
    """
    Detect ALBs and NLBs with no or near-zero request traffic.

    Load balancers with fewer than request_threshold total requests over
    lookback_days are flagged as idle. They still incur the hourly LCU base cost.
    """
    findings: list[dict] = []

    now = datetime.now(timezone.utc)
    start = now - timedelta(days=lookback_days)

    # ALB and NLB via ELBv2
    try:
        paginator = elbv2_client.get_paginator("describe_load_balancers")
        for page in paginator.paginate():
            for lb in page.get("LoadBalancers", []):
                lb_name = lb.get("LoadBalancerName", "")
                lb_arn = lb.get("LoadBalancerArn", "")
                lb_type = lb.get("Type", "application")
                state = lb.get("State", {}).get("Code", "")

                if state != "active":
                    continue

                metric = "RequestCount" if lb_type == "application" else "ActiveFlowCount"
                namespace = "AWS/ApplicationELB" if lb_type == "application" else "AWS/NetworkELB"
                lb_dim_value = lb_arn.split("loadbalancer/")[-1] if "loadbalancer/" in lb_arn else lb_arn

                try:
                    resp = cw_client.get_metric_statistics(
                        Namespace=namespace,
                        MetricName=metric,
                        Dimensions=[{"Name": "LoadBalancer", "Value": lb_dim_value}],
                        StartTime=start,
                        EndTime=now,
                        Period=86400,
                        Statistics=["Sum"] if lb_type == "application" else ["Average"],
                    )
                    datapoints = resp.get("Datapoints", [])
                except Exception as exc:
                    log.debug("CW metrics failed for LB %s: %s", lb_name, exc)
                    continue

                if not datapoints:
                    total_requests = 0.0
                else:
                    total_requests = sum(dp.get("Sum", dp.get("Average", 0)) for dp in datapoints)

                if total_requests >= request_threshold:
                    continue

                hourly = _ALB_HOURLY if lb_type == "application" else _NLB_HOURLY
                monthly_cost = hourly * 730

                findings.append({
                    "resource_id": lb_arn,
                    "resource_type": "ALB" if lb_type == "application" else "NLB",
                    "waste_type": "idle_load_balancer",
                    "estimated_monthly_savings": round(monthly_cost, 2),
                    "detail": (
                        f"Load balancer '{lb_name}' ({lb_type}) had {total_requests:.0f} "
                        f"total requests over {lookback_days} days. "
                        f"Running cost: ~${monthly_cost:.0f}/mo. "
                        f"Check target groups before deleting."
                    ),
                    "severity": _severity_from_savings(monthly_cost),
                    "region": region,
                    "account_id": None,
                    "lb_name": lb_name,
                    "lb_type": lb_type,
                    "total_requests_14d": total_requests,
                })
    except Exception as exc:
        log.warning("ELBv2 describe failed (region=%s): %s", region, exc)

    # Classic ELBs
    try:
        paginator = elb_client.get_paginator("describe_load_balancers")
        for page in paginator.paginate():
            for lb in page.get("LoadBalancerDescriptions", []):
                lb_name = lb.get("LoadBalancerName", "")

                try:
                    resp = cw_client.get_metric_statistics(
                        Namespace="AWS/ELB",
                        MetricName="RequestCount",
                        Dimensions=[{"Name": "LoadBalancerName", "Value": lb_name}],
                        StartTime=start,
                        EndTime=now,
                        Period=86400,
                        Statistics=["Sum"],
                    )
                    datapoints = resp.get("Datapoints", [])
                except Exception:
                    continue

                total_requests = sum(dp.get("Sum", 0) for dp in datapoints)

                if total_requests >= request_threshold:
                    continue

                monthly_cost = _CLB_HOURLY * 730

                findings.append({
                    "resource_id": lb_name,
                    "resource_type": "Classic Load Balancer",
                    "waste_type": "idle_load_balancer",
                    "estimated_monthly_savings": round(monthly_cost, 2),
                    "detail": (
                        f"Classic ELB '{lb_name}' had {total_requests:.0f} requests over "
                        f"{lookback_days} days. Running cost: ~${monthly_cost:.0f}/mo. "
                        f"Migrate to ALB/NLB or delete if unused."
                    ),
                    "severity": _severity_from_savings(monthly_cost),
                    "region": region,
                    "account_id": None,
                    "lb_name": lb_name,
                    "lb_type": "classic",
                    "total_requests_14d": total_requests,
                })
    except Exception as exc:
        log.warning("Classic ELB describe failed (region=%s): %s", region, exc)

    return findings


# ── S3 incomplete multipart uploads ──────────────────────────────────────────

def check_s3_incomplete_multipart(
    s3_client: Any,
    region: str = "unknown",
    older_than_days: int = 7,
) -> list[dict]:
    """
    Detect S3 buckets with incomplete multipart uploads older than older_than_days.

    Incomplete multipart uploads accumulate silently and are billed at STANDARD
    storage rates. A lifecycle rule is the fix.
    """
    findings: list[dict] = []

    cutoff = datetime.now(timezone.utc) - timedelta(days=older_than_days)

    try:
        buckets_resp = s3_client.list_buckets()
        buckets = buckets_resp.get("Buckets", [])
    except Exception as exc:
        log.warning("list_buckets failed: %s", exc)
        return findings

    for bucket in buckets:
        bucket_name = bucket["Name"]
        total_size_bytes = 0
        old_upload_count = 0

        try:
            paginator = s3_client.get_paginator("list_multipart_uploads")
            for page in paginator.paginate(Bucket=bucket_name):
                for upload in page.get("Uploads", []):
                    initiated = upload.get("Initiated")
                    if initiated and initiated < cutoff:
                        upload_id = upload.get("UploadId", "")
                        try:
                            parts_resp = s3_client.list_parts(
                                Bucket=bucket_name,
                                Key=upload.get("Key", ""),
                                UploadId=upload_id,
                            )
                            for part in parts_resp.get("Parts", []):
                                total_size_bytes += part.get("Size", 0)
                        except Exception:
                            pass
                        old_upload_count += 1
        except Exception as exc:
            log.debug("list_multipart_uploads failed for %s: %s", bucket_name, exc)
            continue

        if old_upload_count == 0:
            continue

        size_gb = total_size_bytes / (1024 ** 3)
        monthly_cost = size_gb * _S3_STANDARD_PER_GB_MONTH

        findings.append({
            "resource_id": f"s3://{bucket_name}",
            "resource_type": "S3 Bucket",
            "waste_type": "s3_incomplete_multipart_uploads",
            "estimated_monthly_savings": round(monthly_cost, 2),
            "detail": (
                f"Bucket '{bucket_name}' has {old_upload_count} incomplete multipart "
                f"upload(s) older than {older_than_days} days, consuming {size_gb:.2f} GB "
                f"(~${monthly_cost:.2f}/mo). Add a lifecycle rule: "
                f"AbortIncompleteMultipartUpload with DaysAfterInitiation=7."
            ),
            "severity": _severity_from_savings(monthly_cost) if monthly_cost > 1 else "low",
            "region": region,
            "account_id": None,
            "bucket": bucket_name,
            "incomplete_upload_count": old_upload_count,
            "wasted_gb": round(size_gb, 3),
        })

    return findings


# ── ECR image cleanup ─────────────────────────────────────────────────────────

_ECR_STORAGE_PER_GB_MONTH = 0.10  # $0.10/GB-month after first 500MB free


def check_ecr_old_images(
    ecr_client: Any,
    region: str = "unknown",
    older_than_days: int = 90,
    keep_tagged: bool = True,
) -> list[dict]:
    """
    Detect old untagged ECR images incurring storage charges.

    ECR charges $0.10/GB-month. Lifecycle policies that expire untagged
    images after 14 days are the standard fix.
    """
    findings: list[dict] = []

    cutoff = datetime.now(timezone.utc) - timedelta(days=older_than_days)

    try:
        paginator = ecr_client.get_paginator("describe_repositories")
        repos = []
        for page in paginator.paginate():
            repos.extend(page.get("repositories", []))
    except Exception as exc:
        log.warning("ECR describe_repositories failed (region=%s): %s", region, exc)
        return findings

    for repo in repos:
        repo_name = repo["repositoryName"]
        repo_uri = repo.get("repositoryUri", repo_name)
        old_image_count = 0
        total_size_bytes = 0

        try:
            img_paginator = ecr_client.get_paginator("describe_images")
            filters: dict[str, Any] = {}
            if keep_tagged:
                filters = {"filter": {"tagStatus": "UNTAGGED"}}
            for page in img_paginator.paginate(repositoryName=repo_name, **filters):
                for img in page.get("imageDetails", []):
                    pushed_at = img.get("imagePushedAt")
                    if pushed_at:
                        if pushed_at.tzinfo is None:
                            pushed_at = pushed_at.replace(tzinfo=timezone.utc)
                        if pushed_at >= cutoff:
                            continue
                    old_image_count += 1
                    total_size_bytes += img.get("imageSizeInBytes", 0)
        except Exception as exc:
            log.debug("ECR describe_images failed for %s: %s", repo_name, exc)
            continue

        if old_image_count == 0:
            continue

        size_gb = total_size_bytes / (1024 ** 3)
        monthly_cost = max(0, size_gb - 0.5) * _ECR_STORAGE_PER_GB_MONTH

        if monthly_cost < 1.0 and old_image_count < 10:
            continue

        findings.append({
            "resource_id": repo_uri,
            "resource_type": "ECR Repository",
            "waste_type": "ecr_old_untagged_images",
            "estimated_monthly_savings": round(monthly_cost, 2),
            "detail": (
                f"ECR repository '{repo_name}' has {old_image_count} "
                f"{'untagged ' if keep_tagged else ''}image(s) older than {older_than_days} days, "
                f"consuming {size_gb:.2f} GB (~${monthly_cost:.2f}/mo). "
                f"Add an ECR lifecycle policy to expire untagged images after 14 days."
            ),
            "severity": _severity_from_savings(monthly_cost),
            "region": region,
            "account_id": None,
            "repository": repo_name,
            "old_image_count": old_image_count,
            "size_gb": round(size_gb, 3),
        })

    return findings


# ── Data transfer analysis ────────────────────────────────────────────────────

def check_data_transfer_costs(
    ce_client: Any,
    start: str,
    end: str,
    threshold_usd: float = 50.0,
) -> list[dict]:
    """
    Identify expensive data transfer line items from Cost Explorer.

    Surfaces cross-AZ transfer, internet egress, inter-region transfer,
    and NAT Gateway data charges. Each finding includes a targeted
    remediation recommendation.
    """
    findings: list[dict] = []

    data_transfer_keywords = [
        "DataTransfer", "Transfer", "data transfer",
        "inter-region", "NAT",
    ]

    try:
        resp = ce_client.get_cost_and_usage(
            TimePeriod={"Start": start, "End": end},
            Granularity="MONTHLY",
            GroupBy=[{"Type": "DIMENSION", "Key": "USAGE_TYPE"}],
            Metrics=["UnblendedCost"],
        )
    except Exception as exc:
        log.warning("Data transfer CE query failed: %s", exc)
        return findings

    for period in resp.get("ResultsByTime", []):
        for group in period.get("Groups", []):
            usage_type = group.get("Keys", [""])[0]
            amount = float(group.get("Metrics", {}).get("UnblendedCost", {}).get("Amount", 0))

            if amount < threshold_usd:
                continue

            is_transfer = any(kw.lower() in usage_type.lower() for kw in data_transfer_keywords)
            if not is_transfer:
                continue

            if "NAT" in usage_type:
                rec = (
                    "Replace NAT Gateway with VPC endpoints for S3 and DynamoDB traffic "
                    "(free for gateway endpoints). Use Interface endpoints for other services."
                )
            elif "inter-region" in usage_type.lower() or "Region to Region" in usage_type:
                rec = (
                    "Consolidate workloads to a single region where possible. "
                    "Use S3 replication only for DR, not hot data access."
                )
            elif "Internet" in usage_type:
                rec = (
                    "Use CloudFront to cache and reduce egress costs. "
                    "Ensure API clients are in the same region as your endpoints."
                )
            else:
                rec = "Review usage type. Consider VPC endpoints, CloudFront, or regional consolidation."

            findings.append({
                "resource_id": usage_type,
                "resource_type": "Data Transfer",
                "waste_type": "data_transfer_cost",
                "estimated_monthly_savings": round(amount * 0.3, 2),
                "detail": (
                    f"Usage type '{usage_type}' cost ${amount:,.2f} in the period. "
                    f"Recommendation: {rec}"
                ),
                "severity": "high" if amount >= 200 else "medium",
                "region": "global",
                "account_id": None,
                "usage_type": usage_type,
                "monthly_cost": round(amount, 2),
            })

    return findings


# ── ECS Fargate rightsizing ───────────────────────────────────────────────────

def check_ecs_task_rightsizing(
    ecs_client: Any,
    cw_client: Any,
    region: str = "unknown",
    cpu_threshold_pct: float = 20.0,
    lookback_days: int = 14,
) -> list[dict]:
    """
    Detect ECS Fargate services with over-provisioned CPU allocations.

    Uses Container Insights CpuUtilized metric. Only clusters with
    Container Insights enabled produce usable data.

    Fargate billing: $0.04048/vCPU-hr, $0.004445/GB-hr.
    """
    findings: list[dict] = []

    now = datetime.now(timezone.utc)
    start = now - timedelta(days=lookback_days)

    _FARGATE_VCPU_HOURLY = 0.04048

    try:
        cluster_paginator = ecs_client.get_paginator("list_clusters")
        clusters = []
        for page in cluster_paginator.paginate():
            clusters.extend(page.get("clusterArns", []))
    except Exception as exc:
        log.warning("ECS list_clusters failed (region=%s): %s", region, exc)
        return findings

    for cluster_arn in clusters:
        cluster_name = cluster_arn.split("/")[-1]

        try:
            service_paginator = ecs_client.get_paginator("list_services")
            service_arns = []
            for page in service_paginator.paginate(cluster=cluster_arn):
                service_arns.extend(page.get("serviceArns", []))
        except Exception:
            continue

        for i in range(0, len(service_arns), 10):
            batch = service_arns[i:i+10]
            try:
                resp = ecs_client.describe_services(cluster=cluster_arn, services=batch)
            except Exception:
                continue

            for svc in resp.get("services", []):
                svc_name = svc.get("serviceName", "")
                task_def_arn = svc.get("taskDefinition", "")
                launch_type = svc.get("launchType", "")

                if launch_type != "FARGATE":
                    continue

                try:
                    td_resp = ecs_client.describe_task_definition(taskDefinition=task_def_arn)
                    td = td_resp.get("taskDefinition", {})
                    allocated_cpu = int(td.get("cpu", 256))
                    allocated_memory_mb = int(td.get("memory", 512))
                except Exception:
                    continue

                try:
                    cpu_resp = cw_client.get_metric_statistics(
                        Namespace="ECS/ContainerInsights",
                        MetricName="CpuUtilized",
                        Dimensions=[
                            {"Name": "ClusterName", "Value": cluster_name},
                            {"Name": "ServiceName", "Value": svc_name},
                        ],
                        StartTime=start,
                        EndTime=now,
                        Period=3600,
                        Statistics=["Average"],
                    )
                    cpu_datapoints = cpu_resp.get("Datapoints", [])
                except Exception:
                    continue

                if not cpu_datapoints or len(cpu_datapoints) < 24:
                    continue

                avg_cpu_units = sum(dp.get("Average", 0) for dp in cpu_datapoints) / len(cpu_datapoints)
                avg_cpu_pct = (avg_cpu_units / allocated_cpu) * 100 if allocated_cpu > 0 else 0

                if avg_cpu_pct >= cpu_threshold_pct:
                    continue

                recommended_cpu = max(256, allocated_cpu // 2)
                cpu_vcpu_saved = (allocated_cpu - recommended_cpu) / 1024
                desired_count = svc.get("desiredCount", 1)
                monthly_cpu_savings = cpu_vcpu_saved * _FARGATE_VCPU_HOURLY * 730 * desired_count

                if monthly_cpu_savings < 5:
                    continue

                findings.append({
                    "resource_id": svc.get("serviceArn", svc_name),
                    "resource_type": "ECS Fargate Service",
                    "waste_type": "ecs_overprovisioned_cpu",
                    "estimated_monthly_savings": round(monthly_cpu_savings, 2),
                    "detail": (
                        f"ECS Fargate service '{svc_name}' (cluster: {cluster_name}) "
                        f"uses {avg_cpu_pct:.1f}% of its {allocated_cpu} CPU units on average. "
                        f"Desired count: {desired_count}. "
                        f"Recommend reducing CPU to {recommended_cpu} units. "
                        f"Requires Container Insights enabled."
                    ),
                    "severity": _severity_from_savings(monthly_cpu_savings),
                    "region": region,
                    "account_id": None,
                    "cluster": cluster_name,
                    "service": svc_name,
                    "allocated_cpu_units": allocated_cpu,
                    "recommended_cpu_units": recommended_cpu,
                    "allocated_memory_mb": allocated_memory_mb,
                    "avg_cpu_pct": round(avg_cpu_pct, 2),
                    "desired_count": desired_count,
                })

    return findings
