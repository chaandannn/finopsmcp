"""
Idle resource detection across AWS.

Scans for resources that are costing money but doing nothing:
  - EBS volumes unattached for N days
  - Elastic IPs not associated with a running instance
  - EBS snapshots older than N days with no known AMI dependency
  - Stopped EC2 instances (still paying for attached EBS)
  - Unused Application/Network Load Balancers (no healthy targets)

All detection is read-only. No mutations happen here.
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

log = logging.getLogger(__name__)

# Monthly cost estimates (us-east-1 on-demand)
_EBS_GP3_PER_GB_MONTH = 0.08
_EBS_GP2_PER_GB_MONTH = 0.10
_EBS_IO1_PER_GB_MONTH = 0.125
_EIP_PER_MONTH = 3.60
_SNAPSHOT_PER_GB_MONTH = 0.05
# Re-exported from aws_prices so analyzers/waste.py and this module cannot
# hold two different prices for one load balancer, which they did.
from ..aws_prices import ALB_PER_MONTH as _ALB_PER_MONTH

_EBS_PRICE = {
    "gp3": _EBS_GP3_PER_GB_MONTH,
    "gp2": _EBS_GP2_PER_GB_MONTH,
    "io1": _EBS_IO1_PER_GB_MONTH,
    "io2": _EBS_IO1_PER_GB_MONTH,
    "st1": 0.045,
    "sc1": 0.025,
}


@dataclass
class IdleResource:
    resource_type: str        # "ebs_volume" | "elastic_ip" | "snapshot" | "stopped_ec2" | "load_balancer"
    resource_id: str
    region: str
    account_id: str
    name: str
    idle_since: str           # ISO date string
    idle_days: int
    monthly_cost_usd: float
    reason: str               # human-readable explanation
    protected: bool = False
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def annual_cost_usd(self) -> float:
        return round(self.monthly_cost_usd * 12, 2)


def _protected_tag_pairs() -> set[tuple[str, str]]:
    """Parse FINOPS_PROTECTED_TAGS into (key, value) pairs."""
    raw = os.getenv(
        "FINOPS_PROTECTED_TAGS",
        "env=prod,protected=true,do-not-delete=true,finops-skip=true",
    )
    pairs: set[tuple[str, str]] = set()
    for item in raw.split(","):
        item = item.strip()
        if "=" in item:
            k, v = item.split("=", 1)
            pairs.add((k.strip().lower(), v.strip().lower()))
    return pairs


def _is_protected(tags: list[dict]) -> bool:
    protected = _protected_tag_pairs()
    for tag in tags:
        k = tag.get("Key", "").lower()
        v = tag.get("Value", "").lower()
        if (k, v) in protected or (k, "true") in protected and v == "true":
            return True
        # bare key check: "do-not-delete" with any value
        if k in {"do-not-delete", "finops-skip", "protected"}:
            return True
    return False


def _tag_name(tags: list[dict]) -> str:
    raw = next((t["Value"] for t in tags if t["Key"] == "Name"), "")
    # Name tags are set by whoever can tag resources in the account, not by
    # nable, so an unbounded or control-character-laden value would flow
    # as-is into tool output and later into ticket titles/bodies. Strip
    # control chars and cap length so it stays a display string, never a
    # vector for injecting content into a downstream LLM or ticket reader.
    cleaned = "".join(c for c in raw if c.isprintable())
    return cleaned[:256]


def _days_since(dt: datetime) -> int:
    now = datetime.now(timezone.utc)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return max(0, (now - dt).days)


# ─── detectors ────────────────────────────────────────────────────────────────

def _scan_ebs_volumes(
    ec2_client: Any,
    account_id: str,
    region: str,
    min_idle_days: int,
) -> list[IdleResource]:
    results = []
    paginator = ec2_client.get_paginator("describe_volumes")
    for page in paginator.paginate(
        Filters=[{"Name": "status", "Values": ["available"]}]
    ):
        for vol in page["Volumes"]:
            tags = vol.get("Tags", [])
            create_time = vol["CreateTime"]
            idle_days = _days_since(create_time)
            if idle_days < min_idle_days:
                continue

            vol_type = vol.get("VolumeType", "gp2")
            size_gb = vol.get("Size", 0)
            price_per_gb = _EBS_PRICE.get(vol_type, _EBS_GP2_PER_GB_MONTH)
            monthly = round(size_gb * price_per_gb, 2)

            results.append(IdleResource(
                resource_type="ebs_volume",
                resource_id=vol["VolumeId"],
                region=region,
                account_id=account_id,
                name=_tag_name(tags),
                idle_since=create_time.date().isoformat(),
                idle_days=idle_days,
                monthly_cost_usd=monthly,
                reason=f"Unattached {vol_type} volume, {size_gb} GB, created {create_time.date()}",
                protected=_is_protected(tags),
                metadata={"volume_type": vol_type, "size_gb": size_gb},
            ))
    return results


def _scan_elastic_ips(
    ec2_client: Any,
    account_id: str,
    region: str,
) -> list[IdleResource]:
    results = []
    resp = ec2_client.describe_addresses()
    for addr in resp.get("Addresses", []):
        # Only unassociated EIPs cost money
        if addr.get("AssociationId"):
            continue
        tags = addr.get("Tags", [])
        allocation_id = addr.get("AllocationId", addr.get("PublicIp"))
        public_ip = addr.get("PublicIp", "")
        results.append(IdleResource(
            resource_type="elastic_ip",
            resource_id=allocation_id,
            region=region,
            account_id=account_id,
            name=_tag_name(tags) or public_ip,
            idle_since="unknown",
            idle_days=0,
            monthly_cost_usd=_EIP_PER_MONTH,
            reason=f"Elastic IP {public_ip} not associated with any instance",
            protected=_is_protected(tags),
            metadata={"public_ip": public_ip},
        ))
    return results


def _scan_old_snapshots(
    ec2_client: Any,
    account_id: str,
    region: str,
    min_idle_days: int,
) -> list[IdleResource]:
    """Snapshots older than min_idle_days not referenced by any AMI."""
    # Build set of snapshot IDs used by AMIs
    ami_snapshot_ids: set[str] = set()
    try:
        ami_resp = ec2_client.describe_images(Owners=["self"])
        for image in ami_resp.get("Images", []):
            for mapping in image.get("BlockDeviceMappings", []):
                snap_id = mapping.get("Ebs", {}).get("SnapshotId")
                if snap_id:
                    ami_snapshot_ids.add(snap_id)
    except Exception as e:
        log.warning("Could not list AMIs: %s", e)

    results = []
    paginator = ec2_client.get_paginator("describe_snapshots")
    for page in paginator.paginate(OwnerIds=["self"]):
        for snap in page["Snapshots"]:
            snap_id = snap["SnapshotId"]
            if snap_id in ami_snapshot_ids:
                continue
            tags = snap.get("Tags", [])
            start_time = snap["StartTime"]
            idle_days = _days_since(start_time)
            if idle_days < min_idle_days:
                continue
            size_gb = snap.get("VolumeSize", 0)
            monthly = round(size_gb * _SNAPSHOT_PER_GB_MONTH, 2)
            results.append(IdleResource(
                resource_type="snapshot",
                resource_id=snap_id,
                region=region,
                account_id=account_id,
                name=_tag_name(tags) or snap.get("Description", ""),
                idle_since=start_time.date().isoformat(),
                idle_days=idle_days,
                monthly_cost_usd=monthly,
                reason=f"{size_gb} GB snapshot, {idle_days} days old, no AMI dependency",
                protected=_is_protected(tags),
                metadata={"size_gb": size_gb, "description": snap.get("Description", "")},
            ))
    return results


def _scan_stopped_ec2(
    ec2_client: Any,
    account_id: str,
    region: str,
    min_idle_days: int,
) -> list[IdleResource]:
    """Stopped EC2 instances still paying for attached EBS volumes."""
    results = []
    # Pass 1: collect qualifying stopped instances and their volume ids, then price
    # ALL volumes in batched describe_volumes calls (500 ids max per call). The old
    # shape made one describe_volumes call per instance, so a fleet with N stopped
    # instances paid N serial round-trips.
    candidates: list[tuple[dict, list, Any, int, list[str]]] = []
    paginator = ec2_client.get_paginator("describe_instances")
    for page in paginator.paginate(
        Filters=[{"Name": "instance-state-name", "Values": ["stopped"]}]
    ):
        for reservation in page["Reservations"]:
            for inst in reservation["Instances"]:
                tags = inst.get("Tags", [])
                # Use launch time as proxy — stopped time not directly available
                launch_time = inst.get("LaunchTime", datetime.now(timezone.utc))
                idle_days = _days_since(launch_time)
                if idle_days < min_idle_days:
                    continue
                vol_ids = [m["Ebs"]["VolumeId"] for m in inst.get("BlockDeviceMappings", [])
                           if m.get("Ebs", {}).get("VolumeId")]
                candidates.append((inst, tags, launch_time, idle_days, vol_ids))

    vol_monthly: dict[str, float] = {}
    all_vol_ids = [v for _, _, _, _, vids in candidates for v in vids]
    for i in range(0, len(all_vol_ids), 500):
        try:
            vol_resp = ec2_client.describe_volumes(VolumeIds=all_vol_ids[i:i + 500])
            for vol in vol_resp.get("Volumes", []):
                vt = vol.get("VolumeType", "gp2")
                sg = vol.get("Size", 0)
                vol_monthly[vol["VolumeId"]] = sg * _EBS_PRICE.get(vt, _EBS_GP2_PER_GB_MONTH)
        except Exception:
            pass

    for inst, tags, launch_time, idle_days, vol_ids in candidates:
        ebs_monthly = sum(vol_monthly.get(v, 0.0) for v in vol_ids)
        itype = inst.get("InstanceType", "")
        results.append(IdleResource(
            resource_type="stopped_ec2",
            resource_id=inst["InstanceId"],
            region=region,
            account_id=account_id,
            name=_tag_name(tags),
            idle_since=launch_time.date().isoformat(),
            idle_days=idle_days,
            monthly_cost_usd=round(ebs_monthly, 2),
            reason=(
                f"Stopped {itype} instance with {len(vol_ids)} attached EBS volume(s). "
                f"Paying ~${ebs_monthly:.2f}/mo for storage while stopped."
            ),
            protected=_is_protected(tags),
            metadata={"instance_type": itype, "volume_ids": vol_ids},
        ))
    return results


def _scan_idle_load_balancers(
    elbv2_client: Any,
    account_id: str,
    region: str,
) -> list[IdleResource]:
    """ALBs/NLBs with no healthy targets in any target group."""
    results = []
    try:
        lbs: list[dict] = []
        paginator = elbv2_client.get_paginator("describe_load_balancers")
        for page in paginator.paginate():
            lbs.extend(page["LoadBalancers"])
        if not lbs:
            return results

        # Tags: the API takes up to 20 ARNs per call; the old shape paid one call
        # per load balancer.
        tags_by_arn: dict[str, list] = {}
        arns = [lb["LoadBalancerArn"] for lb in lbs]
        for i in range(0, len(arns), 20):
            try:
                tags_resp = elbv2_client.describe_tags(ResourceArns=arns[i:i + 20])
                for td in tags_resp.get("TagDescriptions", []):
                    tags_by_arn[td.get("ResourceArn", "")] = td.get("Tags", [])
            except Exception:
                pass

        # Health: no batch API exists (one describe_target_groups + one
        # describe_target_health per target group), so run per-LB checks in a
        # bounded pool instead of serially.
        def _healthy_targets(lb: dict) -> tuple[dict, int]:
            healthy = 0
            try:
                tg_resp = elbv2_client.describe_target_groups(
                    LoadBalancerArn=lb["LoadBalancerArn"])
                for tg in tg_resp.get("TargetGroups", []):
                    health_resp = elbv2_client.describe_target_health(
                        TargetGroupArn=tg["TargetGroupArn"])
                    healthy += sum(
                        1 for t in health_resp.get("TargetHealthDescriptions", [])
                        if t.get("TargetHealth", {}).get("State") == "healthy")
            except Exception:
                healthy = 1  # unknown health must never be flagged as idle
            return lb, healthy

        import concurrent.futures
        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
            checked = list(pool.map(_healthy_targets, lbs))

        for lb, healthy in checked:
            if healthy > 0:
                continue  # has healthy targets (or health unknown), skip
            lb_arn = lb["LoadBalancerArn"]
            lb_type = lb.get("Type", "application")
            tags = tags_by_arn.get(lb_arn, [])
            created = lb.get("CreatedTime", datetime.now(timezone.utc))
            results.append(IdleResource(
                resource_type="load_balancer",
                resource_id=lb_arn.split("/")[-2] + "/" + lb_arn.split("/")[-1],
                region=region,
                account_id=account_id,
                name=lb.get("LoadBalancerName", ""),
                idle_since=created.date().isoformat() if hasattr(created, "date") else str(created),
                idle_days=_days_since(created),
                monthly_cost_usd=_ALB_PER_MONTH,
                reason=f"{lb_type.upper()} load balancer with no healthy targets",
                protected=_is_protected(tags),
                metadata={"lb_type": lb_type, "lb_arn": lb_arn},
            ))
    except Exception as e:
        log.warning("Load balancer scan failed in %s: %s", region, e)
    return results


# ─── public entry point ───────────────────────────────────────────────────────

def scan_idle_resources(
    resource_types: list[str] | None = None,
    regions: list[str] | None = None,
    min_idle_days: int = 7,
) -> list[IdleResource]:
    """
    Scan for idle/wasted resources across AWS.

    resource_types: subset of ["ebs_volume","elastic_ip","snapshot","stopped_ec2","load_balancer"]
    regions: list of AWS regions to scan (default: all opted-in regions)
    min_idle_days: minimum days idle before flagging (default: 7)
    """
    try:
        import boto3
    except ImportError:
        log.error("boto3 not installed")
        return []

    all_types = {"ebs_volume", "elastic_ip", "snapshot", "stopped_ec2", "load_balancer"}
    types = set(resource_types or all_types) & all_types

    if regions is None:
        ec2_global = boto3.client("ec2", region_name="us-east-1")
        try:
            resp = ec2_global.describe_regions(
                Filters=[{"Name": "opt-in-status", "Values": ["opt-in-not-required", "opted-in"]}]
            )
            regions = [r["RegionName"] for r in resp.get("Regions", [])]
        except Exception:
            regions = ["us-east-1", "us-west-2", "eu-west-1", "eu-central-1"]

    sts = boto3.client("sts")
    try:
        account_id = sts.get_caller_identity()["Account"]
    except Exception:
        account_id = "unknown"

    results: list[IdleResource] = []

    def _scan_region(region: str) -> list[IdleResource]:
        out: list[IdleResource] = []
        try:
            ec2 = boto3.client("ec2", region_name=region)

            if "ebs_volume" in types:
                out.extend(_scan_ebs_volumes(ec2, account_id, region, min_idle_days))

            if "elastic_ip" in types:
                out.extend(_scan_elastic_ips(ec2, account_id, region))

            if "snapshot" in types:
                out.extend(_scan_old_snapshots(ec2, account_id, region, min_idle_days))

            if "stopped_ec2" in types:
                out.extend(_scan_stopped_ec2(ec2, account_id, region, min_idle_days))

            if "load_balancer" in types:
                elbv2 = boto3.client("elbv2", region_name=region)
                out.extend(_scan_idle_load_balancers(elbv2, account_id, region))

        except Exception as e:
            log.warning("Idle resource scan failed in region %s: %s", region, e)
        return out

    # Each region runs several independent blocking boto3 calls. Scan regions
    # concurrently so a many-region account finishes inside the value moment's
    # 10s cap instead of hitting it and showing partial idle data. Blocking I/O
    # releases the GIL, so threads give a real speedup here.
    import concurrent.futures
    with concurrent.futures.ThreadPoolExecutor(max_workers=min(len(regions), 12) or 1) as pool:
        for sub in pool.map(_scan_region, regions):
            results.extend(sub)

    results.sort(key=lambda r: r.monthly_cost_usd, reverse=True)
    return results


def idle_resources_summary(resources: list[IdleResource]) -> dict[str, Any]:
    total_monthly = sum(r.monthly_cost_usd for r in resources)
    by_type: dict[str, int] = {}
    for r in resources:
        by_type[r.resource_type] = by_type.get(r.resource_type, 0) + 1

    # Build the detail rows costliest-first, then cap to a token budget. The
    # totals above are computed over ALL resources, so the summary stays accurate;
    # only the per-resource detail list is bounded so a large account does not dump
    # hundreds of rows into the model context (every row is re-read each turn).
    rows = [
        {
            "resource_type": r.resource_type,
            "resource_id": r.resource_id,
            "name": r.name,
            "region": r.region,
            "idle_days": r.idle_days,
            "monthly_cost_usd": r.monthly_cost_usd,
            "reason": r.reason,
            "protected": r.protected,
        }
        for r in sorted(resources, key=lambda r: r.monthly_cost_usd, reverse=True)
    ]
    from ..token_budget import fit_to_budget
    kept, omitted = fit_to_budget(rows)

    out: dict[str, Any] = {
        "total_resources_found": len(resources),
        "total_monthly_waste_usd": round(total_monthly, 2),
        "total_annual_waste_usd": round(total_monthly * 12, 2),
        "by_type": by_type,
        "resources": kept,
        "scope": (
            "Covers only ebs_volume, elastic_ip, snapshot, stopped_ec2, and load_balancer. "
            "A low or zero total here does not mean the account is clean: RDS, DocumentDB, "
            "Kendra, and Textract waste live in their own dedicated tools "
            "(get_idle_rds_instances, get_documentdb_costs, audit_textract_environment_waste, "
            "run_full_cost_audit for the full sweep)."
        ),
    }
    if omitted:
        out["resources_truncated"] = True
        out["resources_omitted"] = omitted
        out["hint"] = (
            f"Showing the {len(kept)} costliest of {len(resources)} idle resources "
            f"to bound token cost. Narrow with resource_types or regions for the rest."
        )
    return out
