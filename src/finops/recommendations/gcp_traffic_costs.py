"""Per-operation GCP charges nobody looks at: NAT data processing, GCS requests.

Two detections with the same shape. The resource's own cost is visible and
budgeted; the per-operation charge riding on top of it is not, because every
native view groups by service and the overhead hides inside the total. Both are
computed from the BigQuery billing export's SKU lines, which is the only place
GCP itemises them.

WHY BOTH ARE INVESTIGATIONS, NEVER RECOMMENDATIONS. The FEE is measured: it is
on the bill, to the cent. What is not measurable from billing data is the
recoverable fraction:

  - Cloud NAT data processing is only avoidable for the traffic bound to Google
    APIs (Private Google Access makes that portion free) or for flows that
    should not transit NAT at all. The destination split needs VPC flow logs,
    which we do not read.
  - GCS request overhead is only avoidable where the access pattern can change
    (batching, composite objects, metadata caching, moving a gcsfuse hot path).
    Which requests those are needs access logs, which we do not read.

So a finding here carries the measured fee in metadata, an honest size band for
the recoverable part, and the exact confirm step that would turn the band into a
number. A precise "you will save $X" from billing data alone would be invented,
and the critique pass exists to stop exactly that.

Matching is by SKU description substring, pinned by tests. A description GCP
renames stops matching and the detection goes quiet: it can MISS, it cannot
fabricate. That failure direction is chosen deliberately.
"""
from __future__ import annotations

import logging
from typing import Any

from .envelope import INFERRED, Finding

log = logging.getLogger(__name__)

# ── Cloud NAT ────────────────────────────────────────────────────────────────

# Below this the fee is real but not worth anyone's morning.
DEFAULT_NAT_MIN_FEE_USD = 50.0
# Data processing must dominate the gateway's own uptime charge, or the gateway
# is just... a gateway, and the fee is proportionate to having one.
DEFAULT_NAT_PROCESSING_MULTIPLE = 2.0

# ── GCS ──────────────────────────────────────────────────────────────────────

DEFAULT_GCS_MIN_OPS_USD = 25.0
# Operations above this share of storage cost signal filesystem-style access.
DEFAULT_GCS_OPS_RATIO = 0.30
# The "healthy" share used to size the excess. Ops below this share are normal
# usage and never counted as recoverable.
HEALTHY_GCS_OPS_RATIO = 0.15


def _f(value: Any) -> float:
    try:
        v = float(value)
    except (TypeError, ValueError):
        return 0.0
    import math
    return v if math.isfinite(v) and v > 0 else 0.0


def _sku_has(row: dict, *needles: str) -> bool:
    sku = str(row.get("sku") or "").lower()
    return all(n in sku for n in needles)


def _service_is(row: dict, *names: str) -> bool:
    svc = str(row.get("service") or "").lower()
    return any(n in svc for n in names)


def _by_project(rows: list[dict]) -> dict[str, list[dict]]:
    out: dict[str, list[dict]] = {}
    for row in rows or []:
        pid = str(row.get("project_id") or "").strip()
        if pid:
            out.setdefault(pid, []).append(row)
    return out


def find_nat_processing_overhead(
    sku_rows: list[dict],
    *,
    min_fee_usd: float = DEFAULT_NAT_MIN_FEE_USD,
    processing_multiple: float = DEFAULT_NAT_PROCESSING_MULTIPLE,
    gateways_by_project: dict[str, list[str]] | None = None,
) -> list[Finding]:
    """Cloud NAT gateways whose data-processing fee dwarfs their uptime charge.

    NAT bills two ways: an hourly uptime charge for existing, and $/GB for every
    byte it processes. When processing dominates, the money is in the traffic,
    and the classic driver is Google-API-bound flows that Private Google Access
    would carry for free.

    gateways_by_project: optional Compute-API enrichment naming the gateways in
    each project. Best-effort context only; absence changes nothing about the
    finding, and the map records what was not checked.
    """
    findings: list[Finding] = []

    for project, rows in _by_project(sku_rows).items():
        processing = sum(_f(r.get("cost_usd")) for r in rows
                         if _service_is(r, "networking", "compute engine")
                         and _sku_has(r, "nat") and _sku_has(r, "data process"))
        uptime = sum(_f(r.get("cost_usd")) for r in rows
                     if _service_is(r, "networking", "compute engine")
                     and _sku_has(r, "nat")
                     and (_sku_has(r, "uptime") or _sku_has(r, "hour")))

        if processing < min_fee_usd:
            continue
        # With no uptime rows the multiple cannot be computed; the absolute
        # floor alone decides, and the finding says which test applied.
        dominated = uptime <= 0 or processing >= uptime * processing_multiple
        if not dominated:
            continue

        gateways = (gateways_by_project or {}).get(project) or []
        findings.append(Finding(
            source="gcp_nat_fees",
            title=f"Cloud NAT in {project} spent ${processing:,.0f} processing traffic",
            why=(
                f"Project {project} paid ${processing:,.0f} in Cloud NAT data-processing "
                f"fees over the period"
                + (f", {processing / uptime:.1f}x the gateways' own uptime charge"
                   if uptime > 0 else ", with no matching uptime charge found")
                + ". NAT charges per gigabyte processed, and traffic bound to Google "
                  "APIs does not need to transit NAT at all: Private Google Access "
                  "carries it free."
            ),
            evidence=INFERRED,
            confidence="medium",
            why_unsure=(
                "Only the traffic bound to Google APIs (or misrouted through NAT) is "
                "avoidable, and the destination split needs VPC flow logs, which nable "
                "does not read. The fee itself is measured from the billing export."
            ),
            assumptions=[
                "A meaningful share of NAT-processed traffic is Google-API-bound, "
                "which is the common case for GKE and data workloads.",
            ],
            remediation=[
                "Enable Private Google Access on the subnets behind this NAT so "
                "Google-API traffic bypasses it.",
                "Turn on VPC flow logs briefly to split the traffic by destination "
                "before changing anything.",
                "For third-party endpoints, consider Private Service Connect.",
            ],
            confirm_steps=[
                "Enable VPC flow logs for one representative subnet for 24-48 hours "
                "and total the bytes with a googleapis.com destination.",
                f"Check the billing export: SKUs matching 'NAT' + 'data process' for "
                f"project {project} should total about ${processing:,.0f}.",
            ],
            rough_monthly=processing,
            resource_id=project,
            metadata={
                "project_id": project,
                "processing_fee_usd": round(processing, 2),
                "uptime_fee_usd": round(uptime, 2),
                "processing_to_uptime_ratio": (round(processing / uptime, 1)
                                               if uptime > 0 else None),
                "gateways": gateways[:10],
                "gateways_examined": bool(gateways),
                "remediation_kind": "network_configuration",
            },
        ))

    return findings


def find_gcs_request_overhead(
    sku_rows: list[dict],
    *,
    min_ops_usd: float = DEFAULT_GCS_MIN_OPS_USD,
    ops_ratio: float = DEFAULT_GCS_OPS_RATIO,
) -> list[Finding]:
    """Projects paying more for GCS operations than the data justifies.

    Storage cost is what people budget. Class A and Class B operation charges
    ride on top, and when they reach a meaningful share of storage cost the
    access pattern is filesystem-shaped: many small objects, list-heavy walks,
    gcsfuse on a hot path. The bytes are cheap; the requests are not.

    The recoverable figure is the excess over a healthy operations share
    (HEALTHY_GCS_OPS_RATIO of storage cost): normal usage below that line is
    never counted, so the band cannot claim money that ordinary access would
    spend anyway.
    """
    findings: list[Finding] = []

    for project, rows in _by_project(sku_rows).items():
        gcs = [r for r in rows if _service_is(r, "cloud storage")]
        if not gcs:
            continue
        ops = sum(_f(r.get("cost_usd")) for r in gcs
                  if _sku_has(r, "class a") or _sku_has(r, "class b")
                  or _sku_has(r, "operations"))
        storage = sum(_f(r.get("cost_usd")) for r in gcs
                      if _sku_has(r, "storage") and not _sku_has(r, "operations"))

        if ops < min_ops_usd:
            continue
        # No storage cost at all with real ops cost is its own smell, and the
        # ratio test cannot run; the absolute floor decides.
        if storage > 0 and ops < storage * ops_ratio:
            continue

        excess = ops - (storage * HEALTHY_GCS_OPS_RATIO if storage > 0 else 0.0)
        pct = (ops / storage * 100) if storage > 0 else None

        findings.append(Finding(
            source="gcs_request_overhead",
            title=f"GCS operations in {project} cost ${ops:,.0f} against ${storage:,.0f} of storage",
            why=(
                f"Project {project} paid ${ops:,.0f} for GCS operations over the period"
                + (f", {pct:.0f}% of its ${storage:,.0f} storage cost"
                   if pct is not None else ", with no storage cost found at all")
                + ". Operation charges at that share of storage mean the access "
                  "pattern is filesystem-shaped: many small objects, list-heavy "
                  "walks, or gcsfuse on a hot path. The bytes are cheap; the "
                  "requests are not."
            ),
            evidence=INFERRED,
            confidence="medium",
            why_unsure=(
                "Which requests are avoidable needs access logs or per-bucket "
                "breakdown, which nable does not read. The operation fees themselves "
                "are measured from the billing export; the recoverable share is the "
                "excess over a healthy operations-to-storage ratio."
            ),
            assumptions=[
                f"Operations up to {HEALTHY_GCS_OPS_RATIO:.0%} of storage cost are "
                f"normal usage and are not counted as recoverable.",
            ],
            remediation=[
                "Batch small writes and combine small objects (composite objects).",
                "Cache object metadata instead of re-listing; avoid list-per-request "
                "patterns.",
                "If gcsfuse serves a hot path, front it with a local cache or move "
                "the hot set off GCS.",
            ],
            confirm_steps=[
                f"In the billing export, group project {project}'s Cloud Storage SKUs: "
                f"Class A + Class B should total about ${ops:,.0f}.",
                "Enable usage logs on the top bucket for a day to see which calls "
                "dominate.",
            ],
            rough_monthly=excess,
            resource_id=project,
            metadata={
                "project_id": project,
                "operations_fee_usd": round(ops, 2),
                "storage_fee_usd": round(storage, 2),
                "ops_to_storage_pct": round(pct, 1) if pct is not None else None,
                "excess_over_healthy_usd": round(excess, 2),
                "healthy_ratio": HEALTHY_GCS_OPS_RATIO,
                "remediation_kind": "access_pattern",
            },
        ))

    return findings
