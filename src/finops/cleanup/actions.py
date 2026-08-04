"""
Idle-resource cleanup PROPOSALS. nable never deletes anything.

This module used to execute. It called ec2.delete_volume, ec2.release_address,
ec2.delete_snapshot, ec2.terminate_instances and elbv2.delete_load_balancer
whenever a caller passed dry_run=False, gated only by FINOPS_CLEANUP_ENABLED and
an admin role check. Two things made that indefensible:

  1. The MCP tool that reached it, `cleanup_idle_resources`, was advertised to
     every client with readOnlyHint=True and destructiveHint=False. Any client
     configured to auto-approve read-only tools had silently granted the model
     permission to terminate EC2 instances. The annotation is a safety contract
     with the client, not a description of intent, and it was false.
  2. nable asks for read-only credentials. A tool that mutates is outside the
     permission the user believed they granted, whatever the env var says.

So the execution path is gone, not relabelled. Every planner below returns the
exact command an operator can run themselves, and nothing here constructs a
mutating API call. `dry_run` is accepted for signature compatibility and has no
effect: there is no longer a mode in which this deletes.

The proposal is the product. It carries the resource, the dollars, and the
command, which is what a reviewer needs and is safe to hand to a model.
"""
from __future__ import annotations

import json
import logging
import shlex
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .idle import IdleResource, scan_idle_resources

log = logging.getLogger(__name__)

_AUDIT_LOG = Path.home() / ".finops-mcp" / "cleanup_audit.jsonl"


def _write_audit(action: str, resource: IdleResource, result: str) -> None:
    _AUDIT_LOG.parent.mkdir(parents=True, exist_ok=True)
    entry = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "action": action,
        "executed": False,          # nable proposes; it never acts
        "resource_type": resource.resource_type,
        "resource_id": resource.resource_id,
        "region": resource.region,
        "name": resource.name,
        "monthly_cost_usd": resource.monthly_cost_usd,
        "result": result,
    }
    with open(_AUDIT_LOG, "a") as f:
        f.write(json.dumps(entry) + "\n")


def _cmd(*parts: str) -> str:
    return " ".join(shlex.quote(p) if " " in p else p for p in parts)


# ─── planners: each returns the command, never runs it ───────────────────────

def _plan_ebs_volume(resource: IdleResource) -> dict:
    return {
        "action": "delete_volume",
        "resource_id": resource.resource_id,
        "command": _cmd("aws", "ec2", "delete-volume",
                        "--volume-id", resource.resource_id,
                        "--region", resource.region),
    }


def _plan_elastic_ip(resource: IdleResource) -> dict:
    return {
        "action": "release_eip",
        "resource_id": resource.resource_id,
        "command": _cmd("aws", "ec2", "release-address",
                        "--allocation-id", resource.resource_id,
                        "--region", resource.region),
    }


def _plan_snapshot(resource: IdleResource) -> dict:
    return {
        "action": "delete_snapshot",
        "resource_id": resource.resource_id,
        "command": _cmd("aws", "ec2", "delete-snapshot",
                        "--snapshot-id", resource.resource_id,
                        "--region", resource.region),
    }


def _plan_stopped_ec2(resource: IdleResource) -> dict:
    return {
        "action": "terminate_ec2",
        "resource_id": resource.resource_id,
        "command": _cmd("aws", "ec2", "terminate-instances",
                        "--instance-ids", resource.resource_id,
                        "--region", resource.region),
        # Worth saying out loud next to a terminate: attached volumes go too.
        "caution": ("Terminating also deletes attached EBS volumes whose "
                    "DeleteOnTermination is true. Confirm the data is not needed."),
    }


def _plan_load_balancer(resource: IdleResource) -> dict:
    lb_arn = resource.metadata.get("lb_arn")
    if not lb_arn:
        return {"status": "skipped", "reason": "missing lb_arn in metadata"}
    return {
        "action": "delete_lb",
        "resource_id": lb_arn,
        "command": _cmd("aws", "elbv2", "delete-load-balancer",
                        "--load-balancer-arn", lb_arn,
                        "--region", resource.region),
    }


_PLAN_MAP = {
    "ebs_volume":    _plan_ebs_volume,
    "elastic_ip":    _plan_elastic_ip,
    "snapshot":      _plan_snapshot,
    "stopped_ec2":   _plan_stopped_ec2,
    "load_balancer": _plan_load_balancer,
}


# ─── public entry point ───────────────────────────────────────────────────────

def cleanup_resources(
    resource_ids: list[str],
    dry_run: bool = True,
    resource_types: list[str] | None = None,
    regions: list[str] | None = None,
    min_idle_days: int = 7,
) -> dict[str, Any]:
    """
    Propose cleanup for idle resources. Nothing is deleted, ever.

    Returns one entry per resource with the exact command to run. Protected
    resources are excluded from the proposal entirely.

    dry_run: accepted for signature compatibility and ignored. There is no mode
        in which this executes, so there is nothing for the flag to switch.
    """
    targets_all = scan_idle_resources(
        resource_types=resource_types,
        regions=regions,
        min_idle_days=min_idle_days,
    )

    if resource_ids:
        id_set = set(resource_ids)
        targets = [r for r in targets_all if r.resource_id in id_set]
        not_found = id_set - {r.resource_id for r in targets}
    else:
        targets = targets_all
        not_found = set()

    proposals: list[dict] = []
    skipped_protected = 0
    proposed_savings = 0.0

    for resource in targets:
        if resource.protected:
            skipped_protected += 1
            proposals.append({
                "resource_id": resource.resource_id,
                "resource_type": resource.resource_type,
                "status": "skipped",
                "reason": "protected tag — never proposed for cleanup",
            })
            continue

        plan_fn = _PLAN_MAP.get(resource.resource_type)
        if not plan_fn:
            proposals.append({
                "resource_id": resource.resource_id,
                "resource_type": resource.resource_type,
                "status": "skipped",
                "reason": f"no cleanup defined for type {resource.resource_type}",
            })
            continue

        plan = plan_fn(resource)
        if plan.get("status") == "skipped":
            proposals.append({**plan, "resource_type": resource.resource_type,
                              "name": resource.name})
            continue

        _write_audit(plan["action"], resource, "proposed")
        proposed_savings += resource.monthly_cost_usd
        proposals.append({
            **plan,
            "status": "proposed",
            "resource_type": resource.resource_type,
            "name": resource.name,
            "monthly_cost_usd": resource.monthly_cost_usd,
            "idle_reason": resource.reason,
        })

    return {
        "executed": False,
        "note": ("nable is read-only and propose-only. These commands are for you "
                 "to review and run; nable will not run them."),
        "total_targeted": len(targets),
        "skipped_protected": skipped_protected,
        "not_found": list(not_found),
        "estimated_monthly_savings_usd": round(proposed_savings, 2),
        "proposals": proposals,
        "audit_log": str(_AUDIT_LOG),
    }
