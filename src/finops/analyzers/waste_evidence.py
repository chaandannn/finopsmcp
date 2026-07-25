"""Evidence classification for the AWS deep-audit findings.

`analyzers/waste.py` predates `recommendations/envelope.py`, so its ~24 detectors
ship a precise `estimated_monthly_savings` on every finding regardless of how much
we actually know. Some of those numbers are solid (an unattached EBS volume at a
known size, a Compute Optimizer finding). Others are a CPU heuristic priced at list
rate, which is a guess wearing a dollar sign.

This module puts the existing envelope rule, evidence decides the claim, over those
findings WITHOUT rewriting the detectors:

  measured -> "recommendation". We observed the resource state or read a real
              metric, and the price follows from it. The figure stands.
  inferred  -> "investigation". A proxy, a single-signal heuristic, or a price we
              assumed. We keep the internal number for ranking but tell the caller
              why we are unsure and how to confirm it.

Deliberately ADDITIVE. `estimated_monthly_savings` stays on every finding because
_dedup_findings ranks on it and the savings ledger reads it. The honesty happens in
what we *say*: `evidence`, `kind`, `why_unsure`, `confirm_steps`, and a headline
that splits measured savings from unconfirmed opportunity instead of adding them
into one number nobody believes.

Adding a detector without classifying it fails `test_waste_evidence.py`, so the map
cannot silently rot.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from ..recommendations.envelope import INFERRED, MEASURED, classify, magnitude_band


@dataclass(frozen=True)
class EvidenceSpec:
    """How much we actually know about one waste_type."""
    evidence: str
    confidence: str = "medium"
    why_unsure: str = ""                                    # required when inferred
    confirm_steps: tuple[str, ...] = ()
    assumptions: tuple[str, ...] = ()


_LIST_PRICE = "Priced at public on-demand list rate, not your negotiated or committed rate."

# One entry per waste_type emitted by waste.py and optimizer.py.
#
# The rule applied: evidence is MEASURED only when we observed the resource state
# or read a real per-resource metric AND the dollar figure follows from that
# observation. A single-signal heuristic (CPU without memory), a configuration we
# saw but whose cost we modelled, or a resource whose *safety to remove* we cannot
# see, is INFERRED.
WASTE_EVIDENCE: dict[str, EvidenceSpec] = {
    # ── observed state, price follows directly ──────────────────────────────
    "unattached_ebs_volume": EvidenceSpec(
        MEASURED, "high",
        assumptions=(_LIST_PRICE,),
    ),
    "gp2_should_migrate_to_gp3": EvidenceSpec(
        MEASURED, "high",
        assumptions=("Same-size gp3 with the free 3,000 IOPS / 125 MB/s baseline.",),
    ),
    "unassociated_elastic_ip": EvidenceSpec(MEASURED, "high"),
    "s3_incomplete_multipart_uploads": EvidenceSpec(
        MEASURED, "high",
        assumptions=("Orphaned parts billed at the bucket's current storage class rate.",),
    ),
    "ecr_old_untagged_images": EvidenceSpec(
        MEASURED, "high",
        assumptions=("Untagged and older than the cutoff means unreferenced.",),
    ),
    "duplicate_cloudtrail_management_events": EvidenceSpec(
        MEASURED, "high",
        assumptions=("The first management-events trail is free; each extra copy bills.",),
    ),

    # ── real metric read straight off the resource ──────────────────────────
    "idle_nat_gateway": EvidenceSpec(MEASURED, "high"),
    "rds_idle_no_connections": EvidenceSpec(
        MEASURED, "high",
        assumptions=("Zero connections over the window means no consumer.",),
    ),
    "idle_load_balancer": EvidenceSpec(MEASURED, "high"),
    "lambda_zero_invocations": EvidenceSpec(MEASURED, "high"),
    "lambda_memory_overprovisioned": EvidenceSpec(
        MEASURED, "medium",
        assumptions=("Max-memory-used comes from Lambda Insights; needs it enabled.",),
    ),

    # ── AWS's own multi-signal recommendation, with AWS's own figure ────────
    "compute_optimizer_overprovisioned_ec2": EvidenceSpec(MEASURED, "high"),
    "compute_optimizer_overprovisioned_rds": EvidenceSpec(MEASURED, "high"),
    "compute_optimizer_overprovisioned_lambda": EvidenceSpec(MEASURED, "high"),

    # ── single-signal heuristics: the classic false positives ───────────────
    "idle_ec2_low_cpu": EvidenceSpec(
        INFERRED, "low",
        why_unsure=(
            "This is CPU only. A memory-bound, disk-bound, or network-bound instance "
            "sits at low CPU while doing real work, and a bursty workload can average "
            "low and still need the headroom at peak. Low CPU is not idle."
        ),
        confirm_steps=(
            "Check memory and disk utilization (CloudWatch agent, or the host's own metrics).",
            "Compare average against p99 CPU. A wide gap means bursty, not idle.",
            "Ask the workload owner what runs here before resizing or stopping it.",
        ),
        assumptions=(_LIST_PRICE,),
    ),
    "rds_overprovisioned": EvidenceSpec(
        INFERRED, "low",
        why_unsure=(
            "Sized from CPU alone. Databases are usually memory- and IOPS-bound, so CPU "
            "headroom says little about whether a smaller class holds the working set."
        ),
        confirm_steps=(
            "Check FreeableMemory and ReadIOPS/WriteIOPS against the target class limits.",
            "Confirm the buffer cache still fits after the downsize.",
        ),
        assumptions=(_LIST_PRICE,),
    ),
    "ecs_overprovisioned_cpu": EvidenceSpec(
        INFERRED, "low",
        why_unsure=(
            "Task CPU reservation compared against observed CPU only. Memory limits and "
            "burst behaviour are not in this signal."
        ),
        confirm_steps=(
            "Check task memory utilization before trimming the reservation.",
            "Confirm the service is not scaling on CPU, or you will change its scaling behaviour.",
        ),
    ),

    # ── configuration we saw, cost we modelled ──────────────────────────────
    "excessive_rds_backup_retention": EvidenceSpec(
        INFERRED, "medium",
        why_unsure=(
            "The retention setting is real, but billable backup storage is only what "
            "exceeds the instance's allocated size, and that depends on change rate, "
            "which we have not measured."
        ),
        confirm_steps=(
            "Read BackupStorageBilled or the RDS backup storage line item in the bill.",
            "Confirm the retention window against your compliance requirement first.",
        ),
    ),
    "log_group_infinite_retention": EvidenceSpec(
        INFERRED, "medium",
        why_unsure=(
            "Never-expire is real, but the saving depends on stored volume and future "
            "ingestion rate, which we projected rather than measured."
        ),
        confirm_steps=(
            "Read storedBytes on the log group and the CloudWatch Logs bill line.",
            "Confirm no audit or compliance rule requires the current retention.",
        ),
    ),
    "cloudtrail_data_events_enabled": EvidenceSpec(
        INFERRED, "medium",
        why_unsure=(
            "Data events are on, but their cost scales with S3/Lambda event volume, "
            "which we have not counted for this trail."
        ),
        confirm_steps=(
            "Check the CloudTrail data-events line in Cost Explorer.",
            "Confirm no detection or compliance control depends on these events.",
        ),
    ),
    "cloudtrail_stopped_but_s3_bucket_costs_persist": EvidenceSpec(
        INFERRED, "medium",
        why_unsure=(
            "The trail is stopped, but the ongoing cost is whatever the destination "
            "bucket still stores, which we estimated rather than read."
        ),
        confirm_steps=(
            "Check the destination bucket's size and storage class.",
            "Confirm the retained logs are not needed for audit before deleting.",
        ),
    ),
    "s3_suboptimal_storage_class": EvidenceSpec(
        INFERRED, "low",
        why_unsure=(
            "Based on bucket-level size metrics and an assumed access pattern. Without "
            "the real request and retrieval profile, a colder class can cost MORE once "
            "retrieval and minimum-duration charges land."
        ),
        confirm_steps=(
            "Open S3 Storage Lens for the real access pattern and tier split.",
            "Check retrieval frequency: colder classes bill per-GB retrieval plus a "
            "30/90-day minimum duration.",
        ),
    ),
    "old_unmanaged_snapshot": EvidenceSpec(
        INFERRED, "low",
        why_unsure=(
            "Age and size are real, but nothing in the API tells us whether this "
            "snapshot is someone's disaster-recovery copy or a compliance artifact. "
            "Deleting a needed backup is not recoverable."
        ),
        confirm_steps=(
            "Check for a retention or compliance tag, and whether an AMI references it.",
            "Confirm ownership with the workload owner before deleting.",
        ),
        assumptions=(_LIST_PRICE,),
    ),
    "data_transfer_cost": EvidenceSpec(
        INFERRED, "medium",
        why_unsure=(
            "The spend is real, but transfer is not automatically waste. Reducing it "
            "means an architecture change (cross-AZ placement, a VPC endpoint, "
            "compression), not a setting to flip."
        ),
        confirm_steps=(
            "Break the transfer down by flow to find which path dominates.",
            "Check whether a VPC endpoint or same-AZ placement removes the hop.",
        ),
    ),
}


def spec_for(waste_type: str) -> EvidenceSpec:
    """Classification for a waste_type. Unknown types are treated as INFERRED:
    fail safe, never promote an unclassified finding to a firm recommendation.
    The completeness test is what stops this default from hiding a gap."""
    return WASTE_EVIDENCE.get(
        waste_type,
        EvidenceSpec(
            INFERRED, "low",
            why_unsure="This finding type has no evidence classification yet.",
        ),
    )


def annotate(findings: list[dict]) -> list[dict]:
    """Attach evidence fields to deep-audit findings, in place, and return them.

    Additive on purpose: `estimated_monthly_savings` is untouched so dedup ranking
    and the savings ledger keep working. Empty fields are dropped, since findings
    ship to the model in lists of dozens and every empty key is pure token cost.
    """
    for f in findings:
        spec = spec_for(f.get("waste_type", ""))
        f["evidence"] = spec.evidence
        f["kind"] = classify(spec.evidence)
        f["confidence"] = spec.confidence
        if spec.why_unsure:
            f["why_unsure"] = spec.why_unsure
        if spec.confirm_steps:
            f["confirm_steps"] = list(spec.confirm_steps)
        if spec.assumptions:
            f["assumptions"] = list(spec.assumptions)
        if f["kind"] != "recommendation":
            # An investigation must not present its internal number as a claim.
            # The raw value stays on the finding for ranking; this is the figure
            # any caller should SHOW.
            f["magnitude"] = magnitude_band(f.get("estimated_monthly_savings"))
    return findings


def split_totals(findings: list[dict]) -> dict[str, Any]:
    """Split the audit headline into what we can stand behind and what we cannot.

    A single "we found $43,400/mo of waste" number is the thing every FinOps tool
    inflates and no engineer believes. Two numbers are both more honest and more
    useful: the measured figure is a commitment, the unconfirmed one is a work
    queue with a next step attached.
    """
    measured = unconfirmed = 0.0
    n_measured = n_unconfirmed = 0
    for f in findings:
        amount = float(f.get("estimated_monthly_savings") or 0.0)
        if f.get("kind") == "recommendation":
            measured += amount
            n_measured += 1
        else:
            unconfirmed += amount
            n_unconfirmed += 1
    return {
        "measured_monthly_savings": round(measured, 2),
        "measured_count": n_measured,
        "unconfirmed_monthly_opportunity": round(unconfirmed, 2),
        "unconfirmed_count": n_unconfirmed,
        "unconfirmed_band": magnitude_band(unconfirmed) if n_unconfirmed else None,
    }
