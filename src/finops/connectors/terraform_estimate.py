"""
Terraform plan cost estimator.

Parses `terraform plan -json` (or a saved plan JSON file) and returns a
cost estimate BEFORE `terraform apply`.

Usage:
    $ terraform plan -out=plan.tfplan
    $ terraform show -json plan.tfplan | finops estimate -
    # or
    $ finops estimate plan.json

What it prices:
    EC2 instances, RDS, Aurora, ElastiCache, EKS, NAT Gateways,
    ALB/NLB, ECS Fargate, Lambda, S3, EBS volumes,
    OpenSearch domains, MSK clusters, Redshift nodes.

Prices: AWS on-demand, us-east-1, as of May 2026.
For accurate multi-region pricing, use INFRACOST_API_KEY (optional).
"""
from __future__ import annotations

import json
import logging
import os
import subprocess
import tempfile
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

# ── Pricing tables (USD/hour unless noted) ───────────────────────────────────
# All prices on-demand us-east-1, May 2026

_EC2_HOURLY: dict[str, float] = {
    # General purpose
    "t3.nano": 0.0052, "t3.micro": 0.0104, "t3.small": 0.0208,
    "t3.medium": 0.0416, "t3.large": 0.0832, "t3.xlarge": 0.1664,
    "t3.2xlarge": 0.3328,
    "t3a.nano": 0.0047, "t3a.micro": 0.0094, "t3a.small": 0.0188,
    "t3a.medium": 0.0376, "t3a.large": 0.0752, "t3a.xlarge": 0.1504,
    "t3a.2xlarge": 0.3008,
    "t4g.nano": 0.0042, "t4g.micro": 0.0084, "t4g.small": 0.0168,
    "t4g.medium": 0.0336, "t4g.large": 0.0672, "t4g.xlarge": 0.1344,
    "t4g.2xlarge": 0.2688,
    "m5.large": 0.096, "m5.xlarge": 0.192, "m5.2xlarge": 0.384,
    "m5.4xlarge": 0.768, "m5.8xlarge": 1.536, "m5.12xlarge": 2.304,
    "m5.16xlarge": 3.072, "m5.24xlarge": 4.608,
    "m6i.large": 0.096, "m6i.xlarge": 0.192, "m6i.2xlarge": 0.384,
    "m6i.4xlarge": 0.768, "m6i.8xlarge": 1.536, "m6i.12xlarge": 2.304,
    "m6g.large": 0.077, "m6g.xlarge": 0.154, "m6g.2xlarge": 0.308,
    "m6g.4xlarge": 0.616, "m6g.8xlarge": 1.232, "m6g.12xlarge": 1.848,
    "m7i.large": 0.1008, "m7i.xlarge": 0.2016, "m7i.2xlarge": 0.4032,
    "m7i.4xlarge": 0.8064, "m7g.large": 0.0816, "m7g.xlarge": 0.1632,
    "m7g.2xlarge": 0.3264, "m7g.4xlarge": 0.6528,
    # Compute optimised
    "c5.large": 0.085, "c5.xlarge": 0.17, "c5.2xlarge": 0.34,
    "c5.4xlarge": 0.68, "c5.9xlarge": 1.53, "c5.18xlarge": 3.06,
    "c6i.large": 0.085, "c6i.xlarge": 0.17, "c6i.2xlarge": 0.34,
    "c6g.large": 0.068, "c6g.xlarge": 0.136, "c6g.2xlarge": 0.272,
    "c7g.large": 0.0725, "c7g.xlarge": 0.145, "c7g.2xlarge": 0.29,
    "c7i.large": 0.08925, "c7i.xlarge": 0.1785, "c7i.2xlarge": 0.357,
    # Memory optimised
    "r5.large": 0.126, "r5.xlarge": 0.252, "r5.2xlarge": 0.504,
    "r5.4xlarge": 1.008, "r5.8xlarge": 2.016, "r5.12xlarge": 3.024,
    "r6i.large": 0.126, "r6i.xlarge": 0.252, "r6i.2xlarge": 0.504,
    "r6g.large": 0.1008, "r6g.xlarge": 0.2016, "r6g.2xlarge": 0.4032,
    "r7g.large": 0.1071, "r7g.xlarge": 0.2142, "r7g.2xlarge": 0.4284,
    "x1e.xlarge": 0.834, "x1e.2xlarge": 1.668, "x1e.4xlarge": 3.336,
    "x2idn.16xlarge": 6.669,
    # GPU (on-demand, us-east-1). List price, not your discounted/Spot rate.
    "p3.2xlarge": 3.06, "p3.8xlarge": 12.24, "p3.16xlarge": 24.48,
    "p4d.24xlarge": 32.77, "p4de.24xlarge": 40.97,
    "p5.48xlarge": 98.32, "p5e.48xlarge": 98.32, "p5en.48xlarge": 98.32,
    "g4dn.xlarge": 0.526, "g4dn.2xlarge": 0.752,
    "g4dn.4xlarge": 1.204, "g4dn.8xlarge": 2.264, "g4dn.12xlarge": 3.912,
    "g4dn.16xlarge": 4.352, "g4dn.metal": 7.824,
    "g5.xlarge": 1.006, "g5.2xlarge": 1.212, "g5.4xlarge": 1.624,
    "g5.8xlarge": 2.448, "g5.12xlarge": 5.672, "g5.16xlarge": 4.096,
    "g5.24xlarge": 8.144, "g5.48xlarge": 16.288,
    "g6.xlarge": 0.8048, "g6.2xlarge": 0.9776, "g6.4xlarge": 1.323,
    "g6.8xlarge": 2.0144, "g6.12xlarge": 4.6016, "g6.16xlarge": 3.3968,
    "g6.24xlarge": 6.6752, "g6.48xlarge": 13.3504,
    "g6e.xlarge": 1.861, "g6e.2xlarge": 2.24208, "g6e.4xlarge": 3.00424,
    "g6e.8xlarge": 4.52856, "g6e.12xlarge": 10.49264, "g6e.16xlarge": 7.577,
    "g6e.24xlarge": 15.066, "g6e.48xlarge": 30.13,
    # Trainium / Inferentia accelerators
    "trn1.2xlarge": 1.3438, "trn1.32xlarge": 21.50, "trn1n.32xlarge": 24.78,
    "inf1.xlarge": 0.228, "inf1.2xlarge": 0.362, "inf1.6xlarge": 1.180, "inf1.24xlarge": 4.721,
    "inf2.xlarge": 0.7582, "inf2.8xlarge": 1.9679, "inf2.24xlarge": 6.4906, "inf2.48xlarge": 12.9813,
    # Storage optimised
    "i3.large": 0.156, "i3.xlarge": 0.312, "i3.2xlarge": 0.624,
    "i3.4xlarge": 1.248, "i3.8xlarge": 2.496,
    "i4i.large": 0.156, "i4i.xlarge": 0.312, "i4i.2xlarge": 0.624,
}

_RDS_HOURLY: dict[str, float] = {
    # MySQL / PostgreSQL / MariaDB
    "db.t3.micro": 0.017, "db.t3.small": 0.034, "db.t3.medium": 0.068,
    "db.t3.large": 0.136, "db.t4g.micro": 0.016, "db.t4g.small": 0.032,
    "db.t4g.medium": 0.065, "db.t4g.large": 0.13,
    "db.m5.large": 0.171, "db.m5.xlarge": 0.342, "db.m5.2xlarge": 0.684,
    "db.m5.4xlarge": 1.368, "db.m5.8xlarge": 2.736, "db.m5.12xlarge": 4.104,
    "db.m6i.large": 0.171, "db.m6i.xlarge": 0.342, "db.m6i.2xlarge": 0.684,
    "db.m6g.large": 0.152, "db.m6g.xlarge": 0.304, "db.m6g.2xlarge": 0.608,
    "db.r5.large": 0.24, "db.r5.xlarge": 0.48, "db.r5.2xlarge": 0.96,
    "db.r5.4xlarge": 1.92, "db.r5.8xlarge": 3.84,
    "db.r6i.large": 0.24, "db.r6i.xlarge": 0.48, "db.r6i.2xlarge": 0.96,
    "db.r6g.large": 0.192, "db.r6g.xlarge": 0.384, "db.r6g.2xlarge": 0.768,
    "db.r7g.large": 0.204, "db.r7g.xlarge": 0.408, "db.r7g.2xlarge": 0.816,
    # Aurora Serverless v2 priced separately below
}

_ELASTICACHE_HOURLY: dict[str, float] = {
    "cache.t3.micro": 0.017, "cache.t3.small": 0.034, "cache.t3.medium": 0.068,
    "cache.t4g.micro": 0.016, "cache.t4g.small": 0.032, "cache.t4g.medium": 0.065,
    "cache.m5.large": 0.139, "cache.m5.xlarge": 0.278, "cache.m5.2xlarge": 0.556,
    "cache.m6g.large": 0.128, "cache.m6g.xlarge": 0.256, "cache.m6g.2xlarge": 0.512,
    "cache.r5.large": 0.207, "cache.r5.xlarge": 0.414, "cache.r5.2xlarge": 0.828,
    "cache.r6g.large": 0.186, "cache.r6g.xlarge": 0.372, "cache.r6g.2xlarge": 0.744,
}

# EBS: $/GB/month
_EBS_PER_GB_MONTH: dict[str, float] = {
    "gp2": 0.10, "gp3": 0.08, "io1": 0.125, "io2": 0.125,
    "st1": 0.045, "sc1": 0.025, "standard": 0.05,
}

# Flat rates ($/hour)
_FLAT_RATES: dict[str, float] = {
    "aws_nat_gateway": 0.045,                    # + data processing
    "aws_lb": 0.008,                             # ALB/NLB base (+ LCU)
    "aws_alb": 0.008,
    "aws_elb": 0.025,                            # classic ELB
    "aws_eks_cluster": 0.10,                     # control plane only
    "aws_elasticsearchdomain": 0.0,              # priced by node below
    "aws_opensearch_domain": 0.0,                # priced by node below
    "aws_msk_cluster": 0.0,                      # priced by broker below
    "aws_kinesis_firehose_delivery_stream": 0.0, # $/GB ingested
    "aws_cloudfront_distribution": 0.0,          # $/request + transfer
}

# OpenSearch / Elasticsearch node pricing ($/hour)
_OPENSEARCH_HOURLY: dict[str, float] = {
    "t3.small.search": 0.036, "t3.medium.search": 0.073,
    "m5.large.search": 0.142, "m5.xlarge.search": 0.285,
    "m6g.large.search": 0.128, "m6g.xlarge.search": 0.256,
    "r5.large.search": 0.187, "r5.xlarge.search": 0.374,
    "r6g.large.search": 0.167, "r6g.xlarge.search": 0.335,
    "c5.large.search": 0.096, "c5.xlarge.search": 0.192,
}

# MSK broker pricing ($/hour per broker)
_MSK_BROKER_HOURLY: dict[str, float] = {
    "kafka.t3.small": 0.021, "kafka.m5.large": 0.142,
    "kafka.m5.xlarge": 0.284, "kafka.m5.2xlarge": 0.568,
    "kafka.m5.4xlarge": 1.136, "kafka.m5.8xlarge": 2.272,
    "kafka.m7g.large": 0.128, "kafka.m7g.xlarge": 0.256,
}

# Redshift node pricing ($/hour)
_REDSHIFT_HOURLY: dict[str, float] = {
    "dc2.large": 0.25, "dc2.8xlarge": 4.80,
    "ra3.xlplus": 1.086, "ra3.4xlplus": 3.26, "ra3.16xlarge": 13.04,
}

HOURS_PER_MONTH = 730.0
DAYS_PER_MONTH = 30.0


# ── Plan parsing ──────────────────────────────────────────────────────────────

@dataclass
class ResourceChange:
    address: str
    type: str
    actions: list[str]          # ["create"], ["delete"], ["update"], ["no-op"]
    before: dict | None
    after: dict | None
    module: str = ""

    @property
    def is_create(self) -> bool:
        return "create" in self.actions

    @property
    def is_delete(self) -> bool:
        return "delete" in self.actions

    @property
    def is_update(self) -> bool:
        return self.actions == ["update"]

    @property
    def net_config(self) -> dict:
        """Best-effort config: prefer `after`, fall back to `before`."""
        return self.after or self.before or {}


def parse_plan(plan_data: dict) -> list[ResourceChange]:
    """Extract resource_changes from a `terraform show -json` plan."""
    changes: list[ResourceChange] = []
    for rc in plan_data.get("resource_changes", []):
        change = rc.get("change", {})
        actions = change.get("actions", ["no-op"])
        if actions == ["no-op"]:
            continue
        changes.append(ResourceChange(
            address=rc.get("address", ""),
            type=rc.get("type", ""),
            actions=actions,
            before=change.get("before"),
            after=change.get("after"),
            module=rc.get("module_address", ""),
        ))
    return changes


# ── Cost estimators per resource type ─────────────────────────────────────────

@dataclass
class CostLine:
    address: str
    resource_type: str
    action: str           # "add", "remove", "change"
    monthly_delta: float  # positive = cost increase, negative = saving
    detail: str           # human note about what drove the price
    confidence: str = "medium"   # low / medium / high


def _sign(rc: ResourceChange) -> int:
    """+1 for creates, -1 for destroys, 0 for updates (recalculated below)."""
    if rc.is_create:
        return 1
    if rc.is_delete:
        return -1
    return 0  # updates handled specially


def _action_label(rc: ResourceChange) -> str:
    if rc.is_create:
        return "add"
    if rc.is_delete:
        return "remove"
    return "change"


def _estimate_ec2(rc: ResourceChange) -> CostLine | None:
    cfg = rc.net_config
    instance_type = cfg.get("instance_type", "")
    hourly = _EC2_HOURLY.get(instance_type)
    if hourly is None:
        return CostLine(rc.address, rc.type, _action_label(rc), 0.0,
                        f"unknown instance type '{instance_type}' — skipped", "low")
    if rc.is_update:
        before_type = (rc.before or {}).get("instance_type", instance_type)
        after_type  = (rc.after  or {}).get("instance_type", instance_type)
        before_h = _EC2_HOURLY.get(before_type, 0.0)
        after_h  = _EC2_HOURLY.get(after_type,  0.0)
        delta = (after_h - before_h) * HOURS_PER_MONTH
        return CostLine(rc.address, rc.type, "change", delta,
                        f"{before_type} → {after_type}", "high")
    monthly = hourly * HOURS_PER_MONTH * _sign(rc)
    return CostLine(rc.address, rc.type, _action_label(rc), monthly,
                    f"{instance_type} @ ${hourly:.4f}/hr", "high")


def _estimate_rds(rc: ResourceChange) -> CostLine | None:
    cfg = rc.net_config
    class_ = cfg.get("instance_class", "")
    multi_az = bool(cfg.get("multi_az", False))
    hourly = _RDS_HOURLY.get(class_, 0.0)
    if multi_az:
        hourly *= 2
    if rc.is_update:
        before_class = (rc.before or {}).get("instance_class", class_)
        after_class  = (rc.after  or {}).get("instance_class", class_)
        before_maz   = bool((rc.before or {}).get("multi_az", False))
        after_maz    = bool((rc.after  or {}).get("multi_az", False))
        bh = _RDS_HOURLY.get(before_class, 0.0) * (2 if before_maz else 1)
        ah = _RDS_HOURLY.get(after_class,  0.0) * (2 if after_maz  else 1)
        delta = (ah - bh) * HOURS_PER_MONTH
        note = f"{before_class}{'×2' if before_maz else ''} → {after_class}{'×2' if after_maz else ''}"
        return CostLine(rc.address, rc.type, "change", delta, note, "high")
    monthly = hourly * HOURS_PER_MONTH * _sign(rc)
    az_note = " (Multi-AZ)" if multi_az else ""
    return CostLine(rc.address, rc.type, _action_label(rc), monthly,
                    f"{class_}{az_note} @ ${hourly:.4f}/hr", "high" if hourly else "low")


def _estimate_aurora(rc: ResourceChange) -> CostLine | None:
    """Aurora cluster — price per instance in cluster."""
    cfg = rc.net_config
    class_ = cfg.get("instance_class", "")
    # Aurora uses same pricing tiers as RDS roughly
    hourly = _RDS_HOURLY.get(class_, 0.0) * 1.1  # ~10% premium
    monthly = hourly * HOURS_PER_MONTH * _sign(rc)
    return CostLine(rc.address, rc.type, _action_label(rc), monthly,
                    f"{class_} (Aurora) @ ${hourly:.4f}/hr", "medium")


def _estimate_elasticache(rc: ResourceChange) -> CostLine | None:
    cfg = rc.net_config
    node_type = cfg.get("node_type", "")
    num_nodes = int(cfg.get("num_cache_nodes", 1))
    hourly = _ELASTICACHE_HOURLY.get(node_type, 0.0)
    monthly = hourly * num_nodes * HOURS_PER_MONTH * _sign(rc)
    return CostLine(rc.address, rc.type, _action_label(rc), monthly,
                    f"{num_nodes}× {node_type} @ ${hourly:.4f}/hr each",
                    "high" if hourly else "low")


def _estimate_ebs(rc: ResourceChange) -> CostLine | None:
    cfg = rc.net_config
    vol_type = cfg.get("type", "gp2")
    size_gb  = float(cfg.get("size", 0))
    iops     = float(cfg.get("iops", 0))
    price_gb = _EBS_PER_GB_MONTH.get(vol_type, 0.10)
    monthly  = size_gb * price_gb
    # io1/io2: additional IOPS charge
    iops_charge = 0.0
    if vol_type in ("io1", "io2") and iops:
        iops_charge = iops * 0.065  # $/IOPS-month
        monthly += iops_charge
    monthly *= _sign(rc)
    detail = f"{size_gb:.0f} GB {vol_type} @ ${price_gb}/GB-mo"
    if iops_charge:
        detail += f" + {iops:.0f} IOPS"
    return CostLine(rc.address, rc.type, _action_label(rc), monthly, detail, "high")


def _estimate_nat_gateway(rc: ResourceChange) -> CostLine | None:
    hourly  = _FLAT_RATES["aws_nat_gateway"]
    monthly = hourly * HOURS_PER_MONTH * _sign(rc)
    return CostLine(rc.address, rc.type, _action_label(rc), monthly,
                    f"${hourly}/hr base (+ $0.045/GB data processed)", "high")


def _estimate_load_balancer(rc: ResourceChange) -> CostLine | None:
    rtype   = rc.type
    hourly  = _FLAT_RATES.get(rtype, 0.008)
    monthly = hourly * HOURS_PER_MONTH * _sign(rc)
    return CostLine(rc.address, rtype, _action_label(rc), monthly,
                    f"${hourly}/hr base (+ LCU/data charges)", "medium")


def _estimate_eks_cluster(rc: ResourceChange) -> CostLine | None:
    hourly  = 0.10
    monthly = hourly * HOURS_PER_MONTH * _sign(rc)
    return CostLine(rc.address, rc.type, _action_label(rc), monthly,
                    "$0.10/hr control plane (node groups billed separately)", "high")


def _estimate_lambda(rc: ResourceChange) -> CostLine | None:
    # Lambda is invocation-priced — we can only give a rough idea
    memory = float((rc.net_config or {}).get("memory_size", 128))
    # Assume 1M invocations/month × 100ms avg — just inform, not charged at rest
    monthly = 0.0
    return CostLine(rc.address, rc.type, _action_label(rc), monthly,
                    f"Pay-per-invocation — {memory:.0f} MB; $0.20/1M reqs + compute",
                    "low")


def _estimate_fargate_task(rc: ResourceChange) -> CostLine | None:
    cfg = rc.net_config
    # ECS task definition — extract vCPU and memory from container definitions
    # Fargate: $0.04048/vCPU-hr + $0.004445/GB-hr
    cpu_units  = float(cfg.get("cpu",    "256"))
    memory_mib = float(cfg.get("memory", "512"))
    vcpu   = cpu_units / 1024
    mem_gb = memory_mib / 1024
    hourly = vcpu * 0.04048 + mem_gb * 0.004445
    monthly = hourly * HOURS_PER_MONTH * _sign(rc)
    return CostLine(rc.address, rc.type, _action_label(rc), monthly,
                    f"{vcpu:.2f} vCPU, {mem_gb:.2f} GB @ ${hourly:.4f}/hr", "medium")


def _estimate_s3(rc: ResourceChange) -> CostLine | None:
    # S3 bucket creation has no standing cost — charged by usage
    return CostLine(rc.address, rc.type, _action_label(rc), 0.0,
                    "Pay-per-use: $0.023/GB-mo standard storage + request fees", "low")


def _estimate_opensearch(rc: ResourceChange) -> CostLine | None:
    cfg = rc.net_config
    cluster_cfg = cfg.get("cluster_config", [{}])
    if isinstance(cluster_cfg, list):
        cluster_cfg = cluster_cfg[0] if cluster_cfg else {}
    node_type  = cluster_cfg.get("instance_type", "m5.large.search")
    node_count = int(cluster_cfg.get("instance_count", 1))
    dedicated_masters = int(cluster_cfg.get("dedicated_master_count", 0))
    master_type = cluster_cfg.get("dedicated_master_type", node_type)

    hourly = _OPENSEARCH_HOURLY.get(node_type, 0.142)
    master_hourly = _OPENSEARCH_HOURLY.get(master_type, hourly)
    total_hourly = hourly * node_count + master_hourly * dedicated_masters
    monthly = total_hourly * HOURS_PER_MONTH * _sign(rc)
    note = f"{node_count}× {node_type}"
    if dedicated_masters:
        note += f" + {dedicated_masters}× {master_type} (master)"
    return CostLine(rc.address, rc.type, _action_label(rc), monthly,
                    f"{note} @ ${total_hourly:.4f}/hr total", "medium")


def _estimate_msk(rc: ResourceChange) -> CostLine | None:
    cfg = rc.net_config
    broker_info = cfg.get("broker_node_group_info", [{}])
    if isinstance(broker_info, list):
        broker_info = broker_info[0] if broker_info else {}
    broker_type  = broker_info.get("instance_type", "kafka.m5.large")
    az_count     = len(broker_info.get("client_subnets", ["a", "b", "c"]))
    hourly = _MSK_BROKER_HOURLY.get(broker_type, 0.142)
    total_hourly = hourly * az_count
    monthly = total_hourly * HOURS_PER_MONTH * _sign(rc)
    return CostLine(rc.address, rc.type, _action_label(rc), monthly,
                    f"{az_count}× {broker_type} @ ${hourly:.4f}/hr/broker", "medium")


def _estimate_redshift(rc: ResourceChange) -> CostLine | None:
    cfg = rc.net_config
    node_type  = cfg.get("node_type",     "dc2.large")
    node_count = int(cfg.get("number_of_nodes", 1))
    hourly = _REDSHIFT_HOURLY.get(node_type, 0.25)
    monthly = hourly * node_count * HOURS_PER_MONTH * _sign(rc)
    return CostLine(rc.address, rc.type, _action_label(rc), monthly,
                    f"{node_count}× {node_type} @ ${hourly}/hr", "high")


def _estimate_cloudwatch_alarms(rc: ResourceChange) -> CostLine | None:
    # $0.10/alarm-month for standard; $0.30 for high-resolution
    monthly = 0.10 * _sign(rc)
    return CostLine(rc.address, rc.type, _action_label(rc), monthly,
                    "$0.10/alarm-month", "high")


_ESTIMATORS: dict[str, Any] = {
    "aws_instance": _estimate_ec2,
    "aws_spot_instance_request": _estimate_ec2,
    "aws_db_instance": _estimate_rds,
    "aws_rds_cluster_instance": _estimate_aurora,
    "aws_elasticache_cluster": _estimate_elasticache,
    "aws_elasticache_replication_group": _estimate_elasticache,
    "aws_ebs_volume": _estimate_ebs,
    "aws_nat_gateway": _estimate_nat_gateway,
    "aws_lb": _estimate_load_balancer,
    "aws_alb": _estimate_load_balancer,
    "aws_elb": _estimate_load_balancer,
    "aws_eks_cluster": _estimate_eks_cluster,
    "aws_lambda_function": _estimate_lambda,
    "aws_ecs_task_definition": _estimate_fargate_task,
    "aws_s3_bucket": _estimate_s3,
    "aws_opensearch_domain": _estimate_opensearch,
    "aws_elasticsearch_domain": _estimate_opensearch,
    "aws_msk_cluster": _estimate_msk,
    "aws_redshift_cluster": _estimate_redshift,
    "aws_cloudwatch_metric_alarm": _estimate_cloudwatch_alarms,
}


# ── Public API ────────────────────────────────────────────────────────────────

def estimate_plan(plan_data: dict) -> dict[str, Any]:
    """
    Estimate cost delta for a Terraform plan.

    Args:
        plan_data: parsed JSON from `terraform show -json <planfile>`

    Returns:
        {
          "monthly_delta_usd": float,   # + = cost increase, - = saving
          "adds":    float,             # total monthly cost of new resources
          "removes": float,             # total monthly saving from destroyed resources
          "changes": float,             # net delta from modified resources
          "lines": [ {address, resource_type, action, monthly_delta, detail, confidence} ],
          "summary": str,               # human-readable 1-liner
          "unpriced": [ {address, type} ],  # resources we couldn't price
          "confidence": str,            # low / medium / high
        }
    """
    changes = parse_plan(plan_data)
    lines: list[CostLine] = []
    unpriced: list[dict] = []

    for rc in changes:
        estimator = _ESTIMATORS.get(rc.type)
        if estimator:
            try:
                line = estimator(rc)
                if line is not None:
                    lines.append(line)
            except Exception as exc:
                log.debug("estimator failed for %s: %s", rc.address, exc)
                unpriced.append({"address": rc.address, "type": rc.type})
        else:
            unpriced.append({"address": rc.address, "type": rc.type})

    adds    = sum(l.monthly_delta for l in lines if l.action == "add"    and l.monthly_delta > 0)
    removes = sum(l.monthly_delta for l in lines if l.action == "remove" and l.monthly_delta < 0)
    chg     = sum(l.monthly_delta for l in lines if l.action == "change")
    total   = adds + removes + chg

    # Confidence: low if many unpriced, high if everything covered
    pct_priced = len(lines) / max(1, len(lines) + len(unpriced))
    confidence = "high" if pct_priced > 0.8 else ("medium" if pct_priced > 0.4 else "low")

    if total > 0:
        summary = f"+${total:,.2f}/mo (cost increase)"
    elif total < 0:
        summary = f"−${abs(total):,.2f}/mo (cost saving)"
    else:
        summary = "No net monthly cost change"

    if unpriced:
        summary += f" · {len(unpriced)} resource(s) not priced"

    untagged = _untagged_creates(changes)
    if untagged:
        summary += f" · {len(untagged)} new resource(s) missing owner/cost tags"

    out = {
        "monthly_delta_usd": round(total, 2),
        "adds":    round(adds,    2),
        "removes": round(removes, 2),
        "changes": round(chg,     2),
        "lines":   [
            {
                "address":       l.address,
                "resource_type": l.resource_type,
                "action":        l.action,
                "monthly_delta": round(l.monthly_delta, 2),
                "detail":        l.detail,
                "confidence":    l.confidence,
            }
            for l in sorted(lines, key=lambda x: abs(x.monthly_delta), reverse=True)
        ],
        "summary":    summary,
        "unpriced":   unpriced,
        "confidence": confidence,
    }
    if untagged:
        out["untagged_resources"] = untagged[:10]
        out["untagged_note"] = (
            "These new resources carry no owner or cost-allocation tag, so their "
            "spend will land unattributed on the bill. Tag them at deploy time "
            "(owner, cost_center, team) to avoid billing surprises later."
        )
    return out


# Cost-attribution tag keys that make a resource traceable to a team/owner.
_ATTRIBUTION_TAG_KEYS = frozenset({
    "owner", "cost_center", "cost-center", "costcenter", "team", "project",
    "cost_allocation", "business_unit",
})


def _untagged_creates(changes: list[ResourceChange]) -> list[dict]:
    """Newly created resources that support tagging but carry no attribution tag.

    Only resources whose planned config exposes a tags/labels key are judged
    (the provider schema supports tagging); resources without the key at all
    (e.g. bucket policies, rule attachments) are skipped rather than flagged,
    so untaggable types never produce noise.
    """
    flagged: list[dict] = []
    for rc in changes:
        if not rc.is_create or not isinstance(rc.after, dict):
            continue
        tag_fields = [k for k in ("tags", "tags_all", "labels") if k in rc.after]
        if not tag_fields:
            continue
        # Union across all present tag fields: AWS provider default_tags land in
        # tags_all, not tags, so judging only the first field falsely flags every
        # resource in a shop that sets owner/team org-wide via default_tags.
        keys: set[str] = set()
        for tf in tag_fields:
            tags = rc.after.get(tf) or {}
            if isinstance(tags, dict):
                keys |= {str(k).lower() for k in tags}
        if not (keys & _ATTRIBUTION_TAG_KEYS):
            flagged.append({
                "address": rc.address,
                "type": rc.type,
                "missing": ("no tags at all" if not keys
                            else "no owner/cost_center/team tag"),
            })
    return flagged


def estimate_from_file(path: str) -> dict[str, Any]:
    """Load a plan JSON file (or '-' for stdin) and estimate."""
    if path == "-":
        data = json.load(sys.stdin)
    else:
        with open(path) as f:
            data = json.load(f)
    return estimate_plan(data)


def estimate_from_dir(tf_dir: str) -> dict[str, Any]:
    """Run `terraform plan -json` in tf_dir and estimate cost delta."""
    tf_bin = os.environ.get("TERRAFORM_BIN", "terraform")
    # env=: terraform runs in a directory the caller named, loads the provider
    # plugins that directory declares, and executes its `data "external"`
    # programs. With no env= every one of those held the decrypted contents of
    # the user's vault. Being a read does not help here: the read is what spawns
    # the process. The user's own exported credentials still pass through, so a
    # plan against a backend authenticated from their shell keeps working; only
    # nable's decrypted copies are withheld.
    from ..security.vault import child_env

    tf_env = child_env()

    # The plan file goes to a temp dir, not `-out=.plan.tmp` inside the caller's
    # repo. Costing a change is a read, and a read has no business writing into
    # someone's working tree. It also left the file behind on every path,
    # including the error paths, so a `git status` in the customer's
    # infrastructure repo came back dirty because they asked what something cost.
    #
    # -lock=false because a plan does not need the state lock. Taking it meant a
    # cost estimate blocked the customer's own applies, and on the 300s timeout
    # below the process is killed without terraform ever releasing it, leaving a
    # stale lock on their remote state that a human has to go and break.
    with tempfile.TemporaryDirectory(prefix="nable-tfplan-") as tmp:
        plan_path = os.path.join(tmp, "plan.bin")
        r1 = subprocess.run(
            [tf_bin, "plan", "-out=" + plan_path, "-input=false", "-lock=false"],
            cwd=tf_dir, capture_output=True, text=True, timeout=300, env=tf_env,
        )
        if r1.returncode != 0:
            raise RuntimeError(f"terraform plan failed:\n{r1.stderr[:2000]}")
        r2 = subprocess.run(
            [tf_bin, "show", "-json", plan_path],
            cwd=tf_dir, capture_output=True, text=True, timeout=60, env=tf_env,
        )
        if r2.returncode != 0:
            raise RuntimeError(f"terraform show -json failed:\n{r2.stderr[:2000]}")
        data = json.loads(r2.stdout)
    return estimate_plan(data)


def format_estimate(result: dict, color: bool = True) -> str:
    """Pretty-print an estimate result for terminal output."""
    RESET = "\033[0m"
    GREEN = "\033[32m"
    RED   = "\033[31m"
    YELLOW = "\033[33m"
    BOLD  = "\033[1m"
    DIM   = "\033[2m"

    if not color:
        RESET = GREEN = RED = YELLOW = BOLD = DIM = ""

    lines = [
        f"\n{BOLD}╔══ Terraform Cost Estimate ══╗{RESET}",
    ]

    for l in result["lines"]:
        delta = l["monthly_delta"]
        if delta > 0:
            sign_str = f"{RED}+${delta:8.2f}/mo{RESET}"
        elif delta < 0:
            sign_str = f"{GREEN}−${abs(delta):8.2f}/mo{RESET}"
        else:
            sign_str = f"{DIM}   $    0.00/mo{RESET}"
        action_sym = {"add": "+", "remove": "-", "change": "~"}.get(l["action"], " ")
        conf_note = f" {DIM}[{l['confidence']} confidence]{RESET}" if l["confidence"] != "high" else ""
        lines.append(f"  {action_sym} {l['address']:<50} {sign_str}")
        lines.append(f"    {DIM}{l['detail']}{conf_note}{RESET}")

    if result["unpriced"]:
        lines.append(f"\n  {YELLOW}Unpriced resources ({len(result['unpriced'])}):{RESET}")
        for u in result["unpriced"][:10]:
            lines.append(f"    · {u['address']} ({u['type']})")
        if len(result["unpriced"]) > 10:
            lines.append(f"    … and {len(result['unpriced'])-10} more")

    total = result["monthly_delta_usd"]
    total_annual = total * 12
    colour = RED if total > 0 else (GREEN if total < 0 else "")
    lines += [
        "",
        f"  Monthly delta:  {colour}{BOLD}{'+' if total > 0 else ''}${total:,.2f}{RESET}",
        f"  Annual delta:   {colour}{BOLD}{'+' if total > 0 else ''}${total_annual:,.2f}{RESET}",
        f"  Confidence:     {result['confidence']}",
        f"  {DIM}Prices: AWS on-demand us-east-1. Actual costs may vary.{RESET}",
        "",
    ]
    return "\n".join(lines)


# ── CLI entry point ───────────────────────────────────────────────────────────

def main(args: list[str] | None = None) -> int:
    import argparse
    p = argparse.ArgumentParser(
        prog="finops estimate",
        description="Estimate AWS cost change from a Terraform plan.",
        epilog="""
Examples:
  terraform plan -out=plan.tfplan
  terraform show -json plan.tfplan | finops estimate -

  finops estimate plan.json
  finops estimate --dir ./infra/prod
  finops estimate plan.json --json
""",
        formatter_class=argparse.RawTextHelpFormatter,
    )
    p.add_argument("plan_file", nargs="?", default=None,
                   help="Path to plan JSON file, or '-' for stdin")
    p.add_argument("--dir", default=None,
                   help="Run terraform plan in this directory")
    p.add_argument("--json", action="store_true",
                   help="Output raw JSON instead of formatted table")
    p.add_argument("--no-color", action="store_true",
                   help="Disable ANSI color output")
    ns = p.parse_args(args)

    try:
        if ns.dir:
            result = estimate_from_dir(ns.dir)
        elif ns.plan_file:
            result = estimate_from_file(ns.plan_file)
        else:
            # Try reading stdin
            if sys.stdin.isatty():
                p.print_help()
                return 1
            data = json.load(sys.stdin)
            result = estimate_plan(data)
    except Exception as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1

    if ns.json:
        print(json.dumps(result, indent=2))
    else:
        print(format_estimate(result, color=not ns.no_color))

    return 0
