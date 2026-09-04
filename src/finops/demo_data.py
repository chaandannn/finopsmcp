"""
Demo / recording mode for nable.

Set FINOPS_DEMO_MODE=1 to make all cost tools return realistic-looking
fake data instead of hitting real cloud APIs.

Use this when:
  - Recording product demos / tutorial videos
  - Sales calls where you don't want to show real account numbers
  - Integration tests that don't need live credentials
  - Docs screenshots

The fake data is internally consistent: the same account IDs, service
names, and cost numbers appear across all tools so the demo flows naturally.
"""
from __future__ import annotations

import os
import time
from datetime import date, datetime, timedelta, timezone
from typing import Any

_TRUTHY = ("1", "true", "yes")
# Accept FINOPS_DEMO as an alias for FINOPS_DEMO_MODE (docs and the landing page
# refer to FINOPS_DEMO; both now work so users don't hit a silently-ignored var).
DEMO_MODE = (
    os.environ.get("FINOPS_DEMO_MODE", "").lower() in _TRUTHY
    or os.environ.get("FINOPS_DEMO", "").lower() in _TRUTHY
)


def _managed_instance() -> bool:
    """True when this process is a managed (control-plane) hosted instance, i.e.
    getnable.com control-plane login is configured (both the per-instance secret
    and the instance id are set). Such an instance serves a real paying customer."""
    return bool(
        os.environ.get("FINOPS_CONTROL_PLANE_SECRET", "").strip()
        and os.environ.get("FINOPS_INSTANCE_ID", "").strip()
    )


# Vault keys that mean a real cloud provider has been connected. setup stores AWS
# under AWS_ACCESS_KEY_ID / AWS_ROLE_ARNS, Azure and GCP under theirs. If any is
# present, the user connected a real account, so demo data must step aside.
_PROVIDER_CRED_KEYS = (
    "AWS_ACCESS_KEY_ID",
    "AWS_ROLE_ARNS",
    "AZURE_TENANT_ID",
    "AZURE_SUBSCRIPTION_IDS",
    "GOOGLE_APPLICATION_CREDENTIALS",
    "_GCP_SERVICE_ACCOUNT_JSON",
    "GCP_BILLING_ACCOUNT_IDS",
)

_real_provider_cache: "tuple[float, bool] | None" = None


def _real_provider_connected() -> bool:
    """True once a real cloud provider is connected. Cheap and cached briefly so
    is_demo stays free on the hot path. Fast path: a connect mirrors credentials
    into os.environ at startup, so a cred already in the env means a real account
    (covers the common connect-then-restart). Fallback: read the vault live, to
    catch a provider connected mid-session before the process restarted."""
    global _real_provider_cache
    now = time.monotonic()
    if _real_provider_cache is not None and _real_provider_cache[0] > now:
        return _real_provider_cache[1]
    found = any(os.environ.get(k) for k in _PROVIDER_CRED_KEYS)
    if not found:
        try:
            from .security.vault import Vault

            keys = set(Vault.default().list_keys())
            found = any(k in keys for k in _PROVIDER_CRED_KEYS)
        except Exception:
            found = False
    if not found:
        # A named-profile or role connect (e.g. via connect_aws or the wizard's
        # profile path) writes accounts.yaml but sets no credential env var, so a
        # configured account is also a real connection. Demo never writes this
        # file, so any entry here is a genuine user connect.
        try:
            from .accounts import list_accounts

            found = bool(list_accounts())
        except Exception:
            pass
    _real_provider_cache = (now + 30.0, found)
    return found


def is_demo() -> bool:
    # A managed hosted instance never serves demo data, even if FINOPS_DEMO is set
    # by a stray env. A paying customer must get real numbers and the real model,
    # never the canned demo_data stubs.
    if _managed_instance():
        return False
    if not DEMO_MODE:
        return False
    # Demo yields to real data: the moment a real provider is connected, show the
    # real numbers, not the canned demo. FINOPS_DEMO_FORCE=1 keeps demo on even
    # then, for recording a demo on a machine that has a real account connected.
    if os.environ.get("FINOPS_DEMO_FORCE", "").lower() in _TRUTHY:
        return True
    if _real_provider_connected():
        return False
    return True


# ── Shared demo constants (internally consistent across all tools) ────────────

_ACCOUNT_ID   = "481516234203"
_ACCOUNT_NAME = "streamco-production"
_REGION       = "us-east-1"

_TODAY = date.today()
_MONTH_START = _TODAY.replace(day=1).isoformat()
_YESTERDAY   = (_TODAY - timedelta(days=1)).isoformat()


# ── Tool response stubs ───────────────────────────────────────────────────────

def cost_summary() -> dict[str, Any]:
    return {
        "period": f"{_MONTH_START} to {_YESTERDAY}",
        "total_usd": 2407600.00,
        "vs_last_month_pct": 16.8,
        "by_service": {
            "Amazon CloudFront":            724800.00,
            "Amazon EC2":                   431600.00,
            "AWS Data Transfer":            408200.00,
            "Amazon S3":                    312400.00,
            "AWS Elemental MediaConvert":   188900.00,
            "AWS Elemental MediaLive":      151300.00,
            "Amazon RDS":                    88400.00,
            "Amazon CloudWatch":             61700.00,
            "AWS Lambda":                    40300.00,
        },
        "account_id":   _ACCOUNT_ID,
        "account_name": _ACCOUNT_NAME,
        "note": "Spans 312 linked accounts; figures are the org rollup.",
        "summary": (
            "Total AWS spend this month: $2.41M (+17% vs last month) across 312 "
            "linked accounts. CloudFront is the top driver at $724,800, up $120,800 "
            "as streaming egress rose after the new season dropped."
        ),
    }


def anomalies() -> dict[str, Any]:
    return {
        "anomalies": [
            {
                "id":          "anom-001",
                "service":     "Amazon CloudFront",
                "account_id":  _ACCOUNT_ID,
                "severity":    "high",
                "detected_at": f"{(_TODAY - timedelta(days=3)).isoformat()}T14:22:00Z",
                "description": (
                    "CloudFront egress spiked $120,800 (+20%) after the new season "
                    "dropped Friday. Delivery to SmartCast devices in us-east-1 and "
                    "eu-west-1 drove the increase."
                ),
                "daily_cost_before": 20130.00,
                "daily_cost_after":  24160.00,
                "projected_monthly_impact": 120800.00,
                "resource_ids":  ["E2QK8S1TREAM01"],
                "tags":          {"team": "streaming-delivery", "env": "production"},
            },
            {
                "id":          "anom-002",
                "service":     "AWS Data Transfer",
                "account_id":  _ACCOUNT_ID,
                "severity":    "medium",
                "detected_at": f"{(_TODAY - timedelta(days=2)).isoformat()}T09:15:00Z",
                "description": (
                    "Origin-to-edge data transfer rose $108,100 (+36%), tracking the "
                    "CloudFront egress jump from the season launch."
                ),
                "daily_cost_before":         10000.00,
                "daily_cost_after":          13600.00,
                "projected_monthly_impact":  108100.00,
            },
            {
                "id":          "anom-003",
                "service":     "Amazon S3",
                "account_id":  _ACCOUNT_ID,
                "severity":    "medium",
                "detected_at": f"{(_TODAY - timedelta(days=1)).isoformat()}T02:41:00Z",
                "description": (
                    "S3 GET/request charges rose $28,400 (+31%) in the ad-platform "
                    "account. No matching deploy or content drop, cause not yet "
                    "isolated, flagged for review."
                ),
                "daily_cost_before":         2960.00,
                "daily_cost_after":          3870.00,
                "projected_monthly_impact":  28400.00,
                "cause":                     "unknown",
            },
        ],
        "total_anomalies": 3,
        "high_severity":   1,
        "summary": "3 cost anomalies detected. The season launch drove CloudFront and data-transfer egress up ~$229k/mo combined; a $28k S3 request spike has no identified cause yet.",
    }


def rightsizing() -> dict[str, Any]:
    # Mirrors the real rightsizing_summary shape: every rec carries a genuine-
    # savings verdict, and savings are priced on the customer's real rates (here a
    # 22% effective discount measured from CUR), not list price. The demo shows the
    # judgment doing its job: $889/mo of raw "underutilized" collapses to $218/mo of
    # genuine savings once burst, memory-bound, and the real rate are accounted for.
    return {
        "total_instances_flagged": 47,
        "total_monthly_savings":   84200.00,
        "total_annual_savings":    1010400.00,
        "genuine_monthly_savings": 21600.00,
        "genuine_annual_savings":  259200.00,
        "verdicts": {"genuine_savings": 12, "review": 19, "likely_false_positive": 16},
        "source": {
            "compute_optimizer": 31,
            "cloudwatch_fallback": 16,
            "note": "Compute Optimizer recommendations include CPU, memory, network, and disk. "
                    "CloudWatch fallback is CPU-only.",
        },
        "savings_by_resource_type": {"ec2": 61400.00, "rds": 22800.00},
        "judgment_note": (
            "47 instances flagged, $84,200/mo of raw 'underutilized'. Only $21,600/mo "
            "survives once burst, memory-bound, and prime-time headroom are checked: "
            "12 genuine, 19 need review, 16 are likely false positives. Showing the top 3."
        ),
        "recommendations": [
            {
                "instance_id":   "i-0a1b2c3d4e5f67890",
                "name":          "vod-encoder-fleet (142 instances)",
                "region":        "us-east-1",
                "resource_type": "ec2",
                "source":        "compute_optimizer",
                "current_type":  "g5.4xlarge",
                "recommended_type": "g5.2xlarge (off-peak)",
                "avg_cpu_pct":   11.2,
                "max_cpu_pct":   None,
                "avg_mem_pct":   24.0,
                "monthly_savings":          12800.00,
                "adjusted_monthly_savings": 8400.00,
                "verdict":       "genuine_savings",
                "score":         86,
                "why":           "GPU encoders sit near-idle off-peak (11% avg util); "
                                 "real saving ≈$8,400/mo on your effective rate, ~26% below list (cur_athena)",
                "action":        "Move off-peak encodes to a scheduled g5.2xlarge pool; fully reversible.",
            },
            {
                "instance_id":   "db-metadata-catalog-01",
                "name":          "metadata-catalog-01",
                "region":        "us-east-1",
                "resource_type": "rds",
                "source":        "compute_optimizer",
                "current_type":  "db.r6g.8xlarge",
                "recommended_type": "db.r6g.4xlarge",
                "avg_cpu_pct":   9.4,
                "max_cpu_pct":   None,
                "avg_mem_pct":   82.0,
                "monthly_savings":          9600.00,
                "adjusted_monthly_savings": 7200.00,
                "verdict":       "review",
                "score":         41,
                "why":           "over-provisioned; memory at 82%, likely memory-bound; "
                                 "real saving ≈$7,200/mo on your effective rate, ~26% below list (cur_athena)",
                "action":        "Modify the instance class in a maintenance window; reversible, brief failover.",
            },
            {
                "instance_id":   "i-07f3c9a1b2d4e6f80",
                "name":          "playback-api-fleet (88 instances)",
                "region":        "us-west-2",
                "resource_type": "ec2",
                "source":        "cloudwatch_fallback",
                "current_type":  "c6i.2xlarge",
                "recommended_type": "c6i.xlarge",
                "avg_cpu_pct":   12.0,
                "max_cpu_pct":   84.0,
                "avg_mem_pct":   None,
                "monthly_savings":          6200.00,
                "adjusted_monthly_savings": 4100.00,
                "verdict":       "likely_false_positive",
                "score":         7,
                "why":           "CPU-only avg 12%; peaks to 84% at prime-time, needs headroom; "
                                 "real saving ≈$4,100/mo on your effective rate, ~26% below list (cur_athena)",
                "action":        "Resize needs a stop/start (brief downtime); fully reversible.",
            },
        ],
        "pricing_basis": {
            "basis":      {"effective_rate": 47},
            "confidence": {"high": 31, "medium": 16},
            "effective_discount_pct": 26.0,
            "rate_source": "cur_athena",
        },
    }


def kubernetes_costs() -> dict[str, Any]:
    return {
        "cluster":               "prod-eks-streaming",
        "provider":              "aws",
        "node_count":            148,
        "pod_count":             1240,
        "total_monthly_cost_usd": 540000.00,
        "wasted_monthly_cost_usd": 96000.00,
        "waste_pct":             17.8,
        "cpu_efficiency_pct":    39.0,
        "mem_efficiency_pct":    56.0,
        "cost_by_namespace": {
            "recommendations": 214000.00,
            "ad-decisioning":  132000.00,
            "playback-api":     88000.00,
            "search":           54000.00,
            "platform":         34000.00,
            "kube-system":      18000.00,
        },
        "top_workloads": [
            {
                "namespace":          "recommendations",
                "workload":           "Deployment/ranker-inference",
                "pods":               120,
                "monthly_cost_usd":   152000.00,
                "wasted_usd":         58000.00,
                "cpu_efficiency_pct": 28.0,
                "mem_efficiency_pct": 51.0,
            },
            {
                "namespace":          "ad-decisioning",
                "workload":           "Deployment/bid-service",
                "pods":               84,
                "monthly_cost_usd":   84000.00,
                "wasted_usd":         11000.00,
                "cpu_efficiency_pct": 67.0,
                "mem_efficiency_pct": 62.0,
            },
        ],
        "idle_nodes":    ["ip-10-2-4-91.ec2.internal", "ip-10-2-7-33.ec2.internal", "ip-10-2-1-88.ec2.internal"],
        "idle_node_cost_usd": 14800.00,
        "summary": (
            "Cluster 'prod-eks-streaming' (AWS, 148 nodes): $540,000/month. "
            "~$96,000/month wasted — the recommendations ranker is 28% CPU efficient."
        ),
    }


def cluster_efficiency() -> dict[str, Any]:
    return {
        "cluster":  "prod-eks-streaming",
        "provider": "aws",
        "score":    54.0,
        "grade":    "C",
        "total_monthly_cost_usd":   540000.00,
        "wasted_monthly_cost_usd":  96000.00,
        "has_metrics_server": True,
        "dimensions": {
            "cpu_efficiency_pct":  39.0,
            "cpu_score":           11.7,
            "mem_efficiency_pct":  56.0,
            "mem_score":           16.8,
            "idle_node_pct":       9.1,
            "idle_node_score":     14.0,
            "waste_pct":           17.8,
            "waste_score":         11.5,
        },
        "headline": (
            "Cluster 'prod-eks-streaming' scores 54/100 (Grade C) — "
            "$540,000/mo total, $96,000/mo estimated waste. "
            "Moderate waste. Tackle the recommendations ranker and 3 idle nodes first."
        ),
        "top_recommendations": [
            {
                "priority": "high",
                "category": "rightsizing",
                "action":   "Rightsize recommendations/ranker-inference: CPU requests 480 cores, using 134 (28%) — reduce to 200 cores.",
                "potential_savings_usd": 44000.0,
            },
            {
                "priority": "medium",
                "category": "idle_nodes",
                "action":   "Drain 3 idle nodes (<10% CPU/mem for 14 days) — saving ~$14,800/mo.",
                "potential_savings_usd": 14800.0,
            },
        ],
    }


def cost_summary_cur() -> dict[str, Any]:
    """Demo response for CUR/Athena line-item query."""
    return {
        "source":  "AWS Cost and Usage Report (Athena)",
        "period":  f"{_MONTH_START} to {_YESTERDAY}",
        "total_usd": 2407600.00,
        "top_resources": [
            {
                "resource_id":   "E2QK8S1TREAM01",
                "resource_name": "smartcast-cdn",
                "service":       "Amazon CloudFront",
                "instance_type": "distribution",
                "region":        "global",
                "monthly_cost":  724800.00,
                "tags": {},  # the biggest line has no owner tag: the core attribution gap
            },
            {
                "resource_id":   "i-0a1b2c3d4e5f67890",
                "resource_name": "vod-encoder-fleet",
                "service":       "Amazon EC2",
                "instance_type": "g5.4xlarge",
                "region":        "us-east-1",
                "monthly_cost":  431600.00,
                "tags": {"team": "content-platform", "env": "production"},
            },
        ],
        "by_tag_team": {
            "streaming-delivery": 742000.00,
            "content-platform":   388000.00,
            "ad-platform":        296000.00,
            "data-analytics":     174000.00,
            "recommendations":    118000.00,
            "untagged":           689600.00,
        },
        "untagged_pct": 28.6,
        "note": "29% of spend ($689,600/mo) is untagged — mostly shared CDN, data transfer, and cross-account networking with no owner tag. That's the first attribution gap to close, and where most of the unallocated egress hides.",
    }


# ── Registry: maps tool name → demo response function ─────────────────────────

def llm_costs() -> dict[str, Any]:
    # AI/LLM spend for the streamco-production story: ~$66,000/mo, ~10% of the
    # ~$673k total bill. AI powers recommendations, content metadata auto-tagging,
    # search relevance, and moderation. gpt-4o leads. The wedge: show the money
    # answer AND the switch that recovers it, with zero creds.
    daily = []
    for i in range(13, -1, -1):
        d = (_TODAY - timedelta(days=i)).isoformat()
        # gentle upward drift, ~$15k/day average
        daily.append({"date": d, "total_usd": round(13800 + (13 - i) * 170.0, 2)})
    return {
        "period": f"{_MONTH_START} to {_YESTERDAY}",
        "total_usd": 450000.00,
        "pct_of_total_cloud_spend": 8.8,
        "by_provider": {
            "openai":    260000.00,
            "anthropic": 150000.00,
            "bedrock":    40000.00,
        },
        "by_model": {
            "gpt-4o":                       168000.00,
            "claude-sonnet-4-5-20250929":   102000.00,
            "o3":                            62000.00,
            "claude-haiku-4-5-20251001":     48000.00,
            "bedrock/anthropic.claude":      40000.00,
            "gpt-4o-mini":                   30000.00,
        },
        "model_count": 6,
        "top_spenders": [
            {"model": "gpt-4o",            "provider": "openai",    "cost_usd": 168000.00},
            {"model": "claude-sonnet-4-5", "provider": "anthropic", "cost_usd": 102000.00},
            {"model": "o3",                "provider": "openai",    "cost_usd":  62000.00},
        ],
        "daily": daily,
        "recommendations": [
            {
                "title": "Route title auto-tagging off o3",
                "detail": (
                    "The nightly metadata auto-tagging job runs on o3 ($62,000/mo). On a "
                    "sampled eval, gpt-4o-mini matches its labels at ~1/15th the price. "
                    "Routing it saves an estimated $52,000/mo."
                ),
                "estimated_savings_usd": 52000.00,
                "effort": "medium",
            },
            {
                "title": "Cache the shared catalog context on gpt-4o",
                "detail": (
                    "Every recommendation call carries the same 8K-token catalog/system "
                    "context, billed uncached (6% cache hit rate). Prompt caching recovers "
                    "an estimated $36,000/mo at current volume."
                ),
                "estimated_savings_usd": 36000.00,
                "effort": "low",
            },
        ],
        "sources": {"openai": "ok", "anthropic": "ok", "bedrock": "ok"},
        "summary": (
            "AI/LLM spend this month: $450,000 (~9% of total cloud cost). gpt-4o drives "
            "37% of it. Two changes recover ~$88,000/mo: route metadata auto-tagging off "
            "o3 to gpt-4o-mini ($52,000) and cache the catalog context ($36,000)."
        ),
    }


def cost_drivers() -> dict[str, Any]:
    """Demo 'why did the bill change' answer, consistent with the $12,847
    acme-production story (up 23.4% vs the prior month)."""
    return {
        "period": f"{_MONTH_START} to {_TODAY}",
        "comparison_period": "prior 30 days",
        "total_current_usd": 2407600.00,
        "total_previous_usd": 2061300.00,
        "net_change_usd": 346300.00,
        "net_change_pct": 16.8,
        "top_increases": [
            {"key": "Amazon CloudFront", "current": 724800.00, "previous": 604000.00, "delta": 120800.00, "delta_pct": 20.0, "direction": "increase"},
            {"key": "AWS Data Transfer", "current": 408200.00, "previous": 300100.00, "delta": 108100.00, "delta_pct": 36.0, "direction": "increase"},
            {"key": "Amazon EC2", "current": 431600.00, "previous": 395000.00, "delta": 36600.00, "delta_pct": 9.3, "direction": "increase"},
            {"key": "AWS Elemental MediaLive", "current": 151300.00, "previous": 128200.00, "delta": 23100.00, "delta_pct": 18.0, "direction": "increase"},
        ],
        "top_decreases": [
            {"key": "Amazon S3", "current": 312400.00, "previous": 326000.00, "delta": -13600.00, "delta_pct": -4.2, "direction": "decrease"},
        ],
        "all_drivers": [],
        "summary": (
            "Costs rose $346,300 (+16.8%) vs the prior 30 days. The season launch is the "
            "story: CloudFront egress up $120,800 (20%) and data transfer up $108,100 (36%) "
            "as delivery to SmartCast devices spiked. EC2 added $36,600 and MediaLive $23,100 "
            "from more live channels. S3 fell $13,600 on Glacier tiering. The top four drivers "
            "account for ~$275k of the $346k; the rest is spread across many small line items "
            "and a $28k S3 request spike with no identified cause yet. Start with CloudFront."
        ),
    }


# Per-provider monthly service inventory for the demo dashboard. Each entry:
# monthly cost, live resource count, and the month-over-month delta. AWS is the
# familiar acme-production story ($12,847, +23.4%); Azure and GCP are smaller,
# realistic multi-cloud footprints so the provider toggle and the active-services
# table have real, distinct data to show. Selecting AWS reproduces the classic
# single-cloud demo numbers exactly.
_PROVIDER_SERVICES: dict[str, list[dict[str, Any]]] = {
    "aws": [
        {"service": "Amazon CloudFront",          "amount": 724800.00, "resources": 0,    "delta_pct": 20.0},
        {"service": "Amazon EC2",                 "amount": 431600.00, "resources": 2140, "delta_pct": 9.3},
        {"service": "AWS Data Transfer",          "amount": 408200.00, "resources": 0,    "delta_pct": 36.0},
        {"service": "Amazon S3",                  "amount": 312400.00, "resources": 180,  "delta_pct": -4.2},
        {"service": "AWS Elemental MediaConvert", "amount": 188900.00, "resources": 0,    "delta_pct": 12.0},
        {"service": "AWS Elemental MediaLive",    "amount": 151300.00, "resources": 48,   "delta_pct": 18.0},
        {"service": "Amazon RDS",                 "amount": 88400.00,  "resources": 34,   "delta_pct": 6.0},
        {"service": "Amazon CloudWatch",          "amount": 61700.00,  "resources": 0,    "delta_pct": 7.0},
        {"service": "AWS Lambda",                 "amount": 40300.00,  "resources": 1240, "delta_pct": 8.0},
    ],
    "gcp": [
        {"service": "BigQuery",           "amount": 452000.00, "resources": 0,   "delta_pct": 22.0},
        {"service": "Compute Engine",     "amount": 138000.00, "resources": 420, "delta_pct": 7.0},
        {"service": "GKE",                "amount": 52000.00,  "resources": 12,  "delta_pct": 5.0},
        {"service": "Cloud Storage",      "amount": 28000.00,  "resources": 240, "delta_pct": -2.0},
        {"service": "Cloud CDN",          "amount": 10000.00,  "resources": 0,   "delta_pct": 11.0},
    ],
    "azure": [
        {"service": "Virtual Machines",   "amount": 80000.00, "resources": 60, "delta_pct": 6.0},
        {"service": "Azure SQL Database", "amount": 22000.00, "resources": 14, "delta_pct": 4.0},
        {"service": "Blob Storage",       "amount": 12000.00, "resources": 40, "delta_pct": -1.0},
        {"service": "App Service",        "amount": 6000.00,  "resources": 22, "delta_pct": 3.0},
    ],
    # Kubernetes, read from kubeconfig (allocation view; namespaces as lines).
    "kubernetes": [
        {"service": "recommendations (ns)", "amount": 214000.00, "resources": 420, "delta_pct": 20.0},
        {"service": "ad-decisioning (ns)",  "amount": 132000.00, "resources": 260, "delta_pct": 9.0},
        {"service": "playback-api (ns)",    "amount": 88000.00,  "resources": 180, "delta_pct": 4.0},
        {"service": "search (ns)",          "amount": 54000.00,  "resources": 120, "delta_pct": 3.0},
        {"service": "platform (ns)",        "amount": 34000.00,  "resources": 90,  "delta_pct": 1.0},
        {"service": "kube-system (ns)",     "amount": 18000.00,  "resources": 70,  "delta_pct": 0.5},
    ],
    # AI / LLM token spend, genuinely separate from cloud (the AI-native wedge).
    "openai": [
        {"service": "GPT-4o",             "amount": 168000.00, "resources": 0,  "delta_pct": 30.0},
        {"service": "o3",                 "amount": 62000.00,  "resources": 0,  "delta_pct": 58.0},
        {"service": "GPT-4o mini",        "amount": 30000.00,  "resources": 0,  "delta_pct": 12.0},
    ],
    "anthropic": [
        {"service": "Claude Sonnet",      "amount": 102000.00, "resources": 0,  "delta_pct": 26.0},
        {"service": "Claude Haiku",       "amount": 48000.00,  "resources": 0,  "delta_pct": 9.0},
    ],
    # SaaS + data platforms.
    "datadog": [
        {"service": "Infrastructure",     "amount": 118000.00, "resources": 0,  "delta_pct": 12.0},
        {"service": "Log Management",     "amount": 82000.00,  "resources": 0,  "delta_pct": 19.0},
        {"service": "APM & Tracing",      "amount": 40000.00,  "resources": 0,  "delta_pct": 7.0},
    ],
    "snowflake": [
        {"service": "Compute (warehouses)","amount": 358000.00,"resources": 38, "delta_pct": 17.0},
        {"service": "Storage",            "amount": 62000.00,  "resources": 0,  "delta_pct": 4.0},
    ],
    "databricks": [
        {"service": "Jobs Compute",       "amount": 232000.00, "resources": 0,  "delta_pct": 14.0},
        {"service": "SQL Warehouses",     "amount": 98000.00,  "resources": 0,  "delta_pct": 6.0},
    ],
}
_DEMO_PROVIDERS = ["aws", "gcp", "azure", "kubernetes", "openai", "anthropic",
                   "datadog", "snowflake", "databricks"]

# Per-provider open opportunities, priced on the customer's real rate.
_PROVIDER_OPPS: dict[str, list[dict[str, Any]]] = {
    "aws": [
        {"description": "Buy a 1-year compute Savings Plan at your steady encoder + services baseline.",
         "monthly_saving": 88000.00, "resource": "compute-savings-plan", "provider": "aws"},
        {"description": "Move the CloudFront egress baseline to a committed private-pricing tier. "
                        "Steady streaming volume qualifies; priced on your ~26% effective discount.",
         "monthly_saving": 62000.00, "resource": "cloudfront-commit", "provider": "aws"},
        {"description": "Move 2.4 PB of cold VOD masters to S3 Glacier Deep Archive.",
         "monthly_saving": 41000.00, "resource": "s3://streamco-vod-masters", "provider": "aws"},
        {"description": "Schedule the off-peak VOD encoder fleet (g5.4xlarge to g5.2xlarge off-hours). "
                        "Genuine after burst + memory check.",
         "monthly_saving": 8400.00, "resource": "vod-encoder-fleet", "provider": "aws"},
    ],
    "gcp": [
        {"description": "Switch the viewership rollups to BigQuery flat-rate slots at this query volume.",
         "monthly_saving": 58000.00, "resource": "bq-flat-slots", "provider": "gcp"},
        {"description": "Set a 90-day lifecycle rule on 1.6 PB of cold Cloud Storage.",
         "monthly_saving": 14000.00, "resource": "gs://streamco-analytics-archive", "provider": "gcp"},
    ],
    "azure": [
        {"description": "Buy a 1-year Azure Reserved VM Instance for the steady D-series baseline.",
         "monthly_saving": 9800.00, "resource": "vm-reservation-dseries", "provider": "azure"},
    ],
    "openai": [
        {"description": "Route the nightly title auto-tagging job from o3 to GPT-4o mini. Same labels "
                        "on a sampled eval, ~1/15th the price.",
         "monthly_saving": 52000.00, "resource": "model-route-autotag", "provider": "openai"},
        {"description": "Cache the shared catalog context on GPT-4o: the same 8K-token context rides "
                        "every recommendation call, billed uncached. Prompt caching recovers most of it.",
         "monthly_saving": 36000.00, "resource": "prompt-cache-gpt4o", "provider": "openai"},
    ],
    "anthropic": [
        {"description": "Move content-moderation summaries from Claude Sonnet to Haiku where quality "
                        "holds on your eval set.",
         "monthly_saving": 22000.00, "resource": "model-route-moderation", "provider": "anthropic"},
    ],
    "kubernetes": [
        {"description": "Right-size the recommendations ranker: requests are 3.5x actual usage across "
                        "120 pods. Trim CPU/memory requests to the p95.",
         "monthly_saving": 44000.00, "resource": "ns/recommendations", "provider": "kubernetes"},
    ],
    "snowflake": [
        {"description": "Auto-suspend six idle ad-analytics warehouses after 60s (currently 5 min). "
                        "They sit warm most of the day.",
         "monthly_saving": 28000.00, "resource": "wh/ad_analytics_xl", "provider": "snowflake"},
    ],
    "datadog": [
        {"description": "Drop custom-metric cardinality on the playback fleet: unused per-device tags "
                        "triple the metric count.",
         "monthly_saving": 19000.00, "resource": "dd-playback-metrics", "provider": "datadog"},
    ],
    "databricks": [
        {"description": "Move nightly recommendation-model training to spot job clusters.",
         "monthly_saving": 31000.00, "resource": "dbx-reco-training", "provider": "databricks"},
    ],
}


# Demo accounts and regions, so Top Accounts and Spend by Region are real panels.
_DEMO_ACCOUNTS = [
    {"name": "Production",         "id": "481516234203", "share": 0.38},
    {"name": "Streaming Delivery", "id": "481516234211", "share": 0.24},
    {"name": "Ad Platform",        "id": "481516234229", "share": 0.16},
    {"name": "Data & Analytics",   "id": "481516234237", "share": 0.13},
    {"name": "Staging",            "id": "481516234245", "share": 0.09},
]
_DEMO_REGIONS = [
    {"region": "us-east-1",      "code": "US", "label": "N. Virginia",  "share": 0.34},
    {"region": "us-west-2",      "code": "US", "label": "Oregon",       "share": 0.20},
    {"region": "eu-west-1",      "code": "IE", "label": "Ireland",      "share": 0.18},
    {"region": "eu-central-1",   "code": "DE", "label": "Frankfurt",    "share": 0.12},
    {"region": "ap-southeast-1", "code": "SG", "label": "Singapore",    "share": 0.09},
    {"region": "ap-northeast-1", "code": "JP", "label": "Tokyo",        "share": 0.07},
]


def _demo_category_payload(active_services: list[dict[str, Any]], window_total: float):
    """Category totals + AI split for the demo, classified the SAME way live is.

    Demo and live share one render path: this runs the real classify_category over
    the demo's service inventory, so the six-bucket segments and the AI card get
    exactly the shape a connected box produces.
    """
    from .categories import CATEGORY_KEYS, ai_kind, ai_label, classify_category
    totals = {k: 0.0 for k in CATEGORY_KEYS}
    ai_rows: list[dict[str, Any]] = []
    ai_delta_num = 0.0
    for s in active_services:
        cat = classify_category(s["provider"], s["service"])
        totals[cat] += s["amount"]
        if cat != "ai":
            continue
        ai_rows.append({
            "key": f"{s['provider']}:{s['service']}",
            "label": ai_label(s["provider"], s["service"]),
            "amount": round(s["amount"], 2),
            "kind": ai_kind(s["provider"], s["service"]),
        })
        ai_delta_num += s["amount"] * s.get("delta_pct", 0.0)
    ai_spend = round(totals["ai"], 2)
    ai_rows.sort(key=lambda r: -r["amount"])
    for r in ai_rows:
        r["pct"] = round(r["amount"] / ai_spend * 100, 1) if ai_spend else 0.0
    ai_pct = round(ai_spend / window_total * 100, 1) if window_total else None
    ai_delta = round(ai_delta_num / ai_spend, 1) if ai_spend else None
    return {k: round(v, 2) for k, v in totals.items()}, ai_spend, ai_pct, ai_delta, ai_rows


def _attach_daily_categories(daily: list[dict[str, Any]], category_totals: dict[str, float],
                             window_total: float) -> None:
    """Split each day's total into the six buckets by the window's category share.
    The per-day series drifts (a 1-day window lands ~115% of window_total), so the
    raw day totals do not sum to window_total. Normalize each day's split back to
    the window first, or the daily category series would not reconcile with
    category_totals."""
    from .categories import CATEGORY_KEYS
    shares = {k: (category_totals.get(k, 0.0) / window_total if window_total else 0.0)
              for k in CATEGORY_KEYS}
    day_totals = [sum(v for k, v in row.items() if k != "date") for row in daily]
    series_sum = sum(day_totals)
    scale = (window_total / series_sum) if series_sum else 0.0
    for row, day_total in zip(daily, day_totals):
        scaled = day_total * scale
        row["categories"] = {k: round(scaled * shares[k], 2) for k in CATEGORY_KEYS}


def _daily_series(days: int, provs: list[str], end: date | None = None) -> list[dict[str, Any]]:
    """A believable per-provider daily spend series over the window. Deterministic
    (seeded by day index) so it does not jump on every refresh, with a gentle
    upward drift and weekly ripple. `end` anchors the last day (defaults to today),
    so a custom date range that ends in the past renders its own window."""
    import math
    out = []
    base = {p: sum(s["amount"] for s in _PROVIDER_SERVICES[p]) / 30.0 for p in provs}
    today = end or date.today()
    for i in range(days - 1, -1, -1):
        d = today - timedelta(days=i)
        pos = (days - i) / max(days, 1)              # 0..1 across the window
        drift = 0.85 + 0.30 * pos                    # ramps up over the window
        ripple = 1.0 + 0.06 * math.sin(i / 7.0 * math.tau)  # weekly wobble
        row: dict[str, Any] = {"date": d.isoformat()}
        for p in provs:
            row[p] = round(base[p] * drift * ripple, 2)
        out.append(row)
    return out


def dashboard_data(
    days: int = 30,
    provider: str = "all",
    start: date | None = None,
    end: date | None = None,
) -> dict[str, Any]:
    """Full payload for the `finops serve` dashboard in demo mode, in the exact
    shape `_fetch_dashboard_data` returns. Provider- and range-aware: selecting a
    provider filters every figure, and the range scales the windowed views. A
    custom `start`/`end` (Vantage-style date range) overrides `days`: the window
    spans those dates and the daily series ends on `end`. AWS reproduces the
    classic acme-production story; Azure and GCP are smaller real footprints, so
    the provider toggle and active-services table show distinct data. MTD and the
    projection stay month-anchored (calendar figures, not lookback figures).
    """
    provider = (provider or "all").lower()
    provs = _DEMO_PROVIDERS if provider == "all" else [provider]
    provs = [p for p in provs if p in _PROVIDER_SERVICES] or ["aws"]

    # A custom date range overrides the preset lookback. Clamp to sane bounds so a
    # malformed range cannot produce a giant or empty series.
    if start and end and end >= start:
        days = max(1, min((end - start).days + 1, 366))

    # Window scaling: figures for the selected lookback. 30d is the reference
    # month; 7d shows ~a quarter of it, 90d ~three months. MTD/projection stay
    # month-anchored (they are calendar figures, not lookback figures).
    win_factor = max(days, 1) / 30.0

    # Flatten selected providers' services into the active-services inventory.
    active_services: list[dict[str, Any]] = []
    for p in provs:
        for s in _PROVIDER_SERVICES[p]:
            active_services.append({
                "service": s["service"],
                "provider": p,
                "resources": s["resources"],
                "amount": round(s["amount"] * win_factor, 2),
                "delta_pct": s["delta_pct"],
            })
    active_services.sort(key=lambda x: -x["amount"])
    window_total = sum(s["amount"] for s in active_services) or 1.0
    for s in active_services:
        s["pct"] = round(s["amount"] / window_total * 100, 1)

    top_services = [
        {"service": s["service"], "amount": s["amount"], "pct": s["pct"]}
        for s in active_services[:8]
    ]

    # Month figures: sum the selected providers' full monthly service cost.
    month_total = round(sum(s["amount"] for p in provs for s in _PROVIDER_SERVICES[p]), 2)
    delta_pct = 16.8 if "aws" in provs else round(sum(
        s["amount"] * s["delta_pct"] for p in provs for s in _PROVIDER_SERVICES[p]
    ) / max(month_total, 1), 1)
    last_month = round(month_total / (1 + delta_pct / 100), 2)
    projected = round(month_total * 1.088, 2)

    recent_opportunities = [o for p in provs for o in _PROVIDER_OPPS.get(p, [])]
    recent_opportunities.sort(key=lambda o: -o["monthly_saving"])
    opp_total = round(sum(o["monthly_saving"] for o in recent_opportunities), 2)

    recent_savings = [
        {"description": "Moved off-peak VOD encodes to a scheduled g5 pool.",
         "monthly_saving": 8400.00, "resource": "vod-encoder-schedule", "provider": "aws"},
    ] if "aws" in provs else []

    # Verified savings ledger: only changes nable proposed AND confirmed landed on
    # the resource (the cloud now matches nable's recommended config). This is the
    # billable figure, kept strictly separate from "identified/potential".
    verified_ledger = [
        {"description": "CloudFront egress moved to a committed private-pricing tier",
         "resource": "cloudfront-commit", "verified_monthly": 62000.00,
         "confirmed_on": (_TODAY - timedelta(days=4)).isoformat(),
         "proof": "egress now billed at the commit rate; CloudFront line down 8%"},
        {"description": "Rightsized the off-peak `vod-encoder-fleet` g5.4xlarge -> g5.2xlarge",
         "resource": "vod-encoder-fleet", "verified_monthly": 8400.00,
         "confirmed_on": (_TODAY - timedelta(days=9)).isoformat(),
         "proof": "142 encoders now g5.2xlarge off-hours; next-day EC2 line fell $280/day"},
    ] if "aws" in provs else []
    verified_monthly = round(sum(v["verified_monthly"] for v in verified_ledger), 2)

    # Score nudges a little by footprint so switching providers visibly moves it.
    score = 66.0 if "aws" in provs else (81.0 if provs == ["azure"] else 69.0)
    grade = "B" if score >= 70 else "C"

    _today = date.today()
    m1 = (_today.replace(day=1) - timedelta(days=1)).replace(day=1)      # last month
    m2 = (m1 - timedelta(days=1)).replace(day=1)                          # two months ago
    trend = [
        {"month": m2.strftime("%B"), "actual": round(last_month * 0.90, 2), "projected": None},
        {"month": m1.strftime("%B"), "actual": last_month, "projected": last_month},
        {"month": f"{_today.strftime('%B')} (projected)", "actual": None, "projected": projected},
    ]

    # Windowed total (what the range actually spans) and the daily provider series.
    window_total_spend = round(window_total, 2)
    daily = _daily_series(days, provs, end=end)

    # AI and GPU as a real number: classify the demo inventory the same way live
    # does and split the six buckets. The per-day category slice is attached at
    # the very end, after every plain sum over the daily rows has run.
    (category_totals, ai_spend_window, ai_spend_pct,
     ai_delta_pct, ai_breakdown_rows) = _demo_category_payload(active_services, window_total_spend)

    # Month TO DATE, not a whole month. This reported `month_total`, the sum of
    # every provider's full monthly figure, so on the 16th the dashboard showed
    # month-to-date equal to the 30-day window and a reader could not tell the
    # two cards apart. Summing the days of the current calendar month out of the
    # series the chart already draws keeps the number consistent with the chart
    # instead of being a second opinion about the same month.
    _m = (end or date.today()).strftime("%Y-%m")
    mtd_total = round(
        sum(v for r in daily if str(r.get("date", "")).startswith(_m)
            for k, v in r.items() if k != "date"),
        2,
    )
    # A window that starts mid-month cannot see the earlier days, so it would
    # under-report. Fall back to the month figure rather than print something
    # smaller than the truth.
    if not mtd_total:
        mtd_total = month_total
    # Headline sparklines: last ~12 windowed daily totals, smoothed.
    def _spark(scale: float) -> list[float]:
        tail = daily[-12:] if len(daily) >= 12 else daily
        return [round(sum(v for k, v in r.items() if k != "date") * scale, 2) for r in tail]

    # Structured recommendations for the table (impact / effort / accounts / saving).
    _rec_meta = {
        "aws": [("High", "Low", 3), ("High", "Medium", 1), ("Medium", "Low", 2), ("Low", "Low", 1)],
        "azure": [("High", "Medium", 1), ("Medium", "Low", 1)],
        "gcp": [("Medium", "Low", 2), ("Low", "Low", 1)],
    }
    recommendations = []
    for o in recent_opportunities:
        p = o.get("provider", "aws")
        meta = _rec_meta.get(p, [("Medium", "Low", 1)])
        m = meta[len(recommendations) % len(meta)]
        recommendations.append({
            "title": o["description"].rstrip("."),
            "subtitle": o.get("resource", ""),
            "provider": p, "impact": m[0], "effort": m[1], "accounts": m[2],
            "monthly_saving": o["monthly_saving"], "resource": o.get("resource", ""),
        })

    # AI Insights rail: the top three savings, phrased as insights.
    ai_insights = [{
        "title": r["title"], "body": r["subtitle"],
        "monthly_saving": r["monthly_saving"], "provider": r["provider"],
    } for r in recommendations[:3]]

    # Top accounts and regions, scaled to the windowed total.
    top_accounts = [{
        "name": a["name"], "id": a["id"],
        "amount": round(window_total_spend * a["share"], 2),
    } for a in _DEMO_ACCOUNTS]
    spend_by_region = [{
        "region": r["region"], "code": r["code"], "label": r["label"],
        "amount": round(window_total_spend * r["share"], 2),
    } for r in _DEMO_REGIONS]

    # Budgets & alerts.
    budgets = [
        {"name": "AWS Monthly Budget",       "provider": "aws",       "used": 2407600, "limit": 2600000},
        {"name": "Snowflake Monthly Budget", "provider": "snowflake", "used": 420000,  "limit": 400000},
        {"name": "GCP Monthly Budget",       "provider": "gcp",       "used": 680000,  "limit": 780000},
    ]
    for b in budgets:
        b["pct"] = round(b["used"] / b["limit"] * 100, 1)
        b["status"] = "over" if b["pct"] >= 100 else ("warn" if b["pct"] >= 85 else "ok")
    alerts = [
        {"kind": "warn",  "title": "Snowflake budget exceeded", "body": "102% of budget used, ad-analytics warehouses running hot"},
        {"kind": "info",  "title": "Forecast alert",            "body": f"{provs[0].upper() if provs else 'AWS'} forecast tracking above run rate after the season launch"},
    ]

    # Executive KPI band (tier-1): unit economics + posture, the board-slide row.
    exec_kpis = [
        {"label": "Infra $ / active account", "value": "$0.27", "delta_pct": -5.0, "good_down": True, "sub": "19.1M active accounts"},
        {"label": "CDN cost per TB delivered", "value": "$7.80", "delta_pct": 6.0, "good_down": True, "sub": "petabyte-scale egress"},
        {"label": "Infra % of revenue", "value": "12.1%", "delta_pct": -0.6, "good_down": True, "sub": "$42M MRR"},
        {"label": "Effective savings rate", "value": "24%", "delta_pct": 2.0, "good_down": False, "sub": "vs on-demand list"},
        {"label": "Commitment coverage", "value": "61%", "delta_pct": 4.0, "good_down": False, "sub": "target 80%"},
    ]
    # AI efficiency panel: the wedge no incumbent shows.
    ai_efficiency = {
        "ai_pct_of_spend": 8.8,
        "ai_spend": round(month_total * 0.088, 2),
        "metrics": [
            {"label": "Cost / 1M tokens", "value": "$2.80", "delta_pct": -12.0, "good_down": True},
            {"label": "Cost / 1M recs served", "value": "$1.90", "delta_pct": -8.0, "good_down": True},
            {"label": "GPU encoder utilization", "value": "39%", "delta_pct": 2.0, "good_down": False, "warn": True},
            {"label": "Cache hit rate", "value": "6%", "delta_pct": 0.0, "good_down": False, "warn": True},
        ],
        "callout": "GPU encoders sit at 39% utilization and prompt caching is nearly off. About $44,000/mo is recoverable by scheduling the encoder fleet and caching the catalog context.",
    }

    # "What changed since you last looked": the always-on loop as a glance.
    whats_changed = {
        "since": "Monday",
        "items": [
            {"kind": "up",    "text": "CloudFront egress up $120,800 (20%)", "prompt": "Why did CloudFront egress jump after the season launch?"},
            {"kind": "alert", "text": "S3 request spike, cause unknown",     "prompt": "Explain the $28k S3 request anomaly in the ad-platform account."},
            {"kind": "warn",  "text": "Snowflake budget crossed 100%",       "prompt": "Show our Snowflake budget status and what's driving it over the cap."},
            {"kind": "good",  "text": "$8,400/mo saved, off-peak encoder schedule", "prompt": "Show the savings we've realized in the last week."},
        ],
    }

    # Forecast vs budget with a confidence band.
    def _money(n: float) -> str:
        if n >= 1e6: return f"${n/1e6:.1f}M"
        if n >= 1e3: return f"${n/1e3:.1f}k"
        return f"${n:,.0f}"
    hist = [round(sum(v for k, v in row.items() if k != "date"), 2) for row in daily]
    last = hist[-1] if hist else (month_total / 30.0)
    forecast = [round(last * (1.0 + 0.012 * i), 2) for i in range(1, 13)]
    budget = round(month_total * 1.14, 2)
    proj_end = round(month_total * 1.088, 2)
    vs_budget = round((proj_end - budget) / budget * 100, 1)
    forecast_panel = {
        "history": hist,
        "forecast": forecast,
        "band_pct": 0.08,
        "budget": budget,
        "projected_end": proj_end,
        "vs_budget_pct": vs_budget,
        "note": f"Projected to finish the month at {_money(proj_end)}, "
                f"{abs(vs_budget)}% {'over' if proj_end > budget else 'under'} "
                f"the {_money(budget)} budget.",
    }

    # Compute the headline sparklines BEFORE attaching category slices: they sum
    # each daily row's provider values, which only works while a row is date +
    # provider numbers and nothing else.
    sparklines = {
        "spend": _spark(1.0), "mtd": _spark(0.62),
        "forecast": _spark(1.09), "savings": _spark(0.065),
    }
    # Now every numeric sum over `daily` (mtd, sparklines, forecast history) has
    # run, so the per-day category slice is safe to attach.
    _attach_daily_categories(daily, category_totals, window_total_spend)

    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "account_id": _ACCOUNT_ID,
        "user": {"name": "Alex R.", "role": "Admin", "email": "alex@streamco.tv"},
        "exec_kpis": exec_kpis,
        "ai_efficiency": ai_efficiency,
        "whats_changed": whats_changed,
        "forecast_panel": forecast_panel,
        "total_spend_mtd": mtd_total,
        "total_spend_window": window_total_spend,
        "total_spend_last_month": last_month,
        "projected_month_total": projected,
        "forecast_delta_pct": -4.7,
        "delta_pct": delta_pct,
        "finops_grade": grade,
        "finops_score": score,
        "sparklines": sparklines,
        "top_services": top_services,
        "active_services": active_services,
        "daily_series": daily,
        "series_providers": provs,
        # AI and GPU category payload (matches the live /api/data shape).
        "categories_available": True,
        "category_totals": category_totals,
        "ai_spend_window": ai_spend_window,
        "ai_spend_pct": ai_spend_pct,
        "ai_delta_pct": ai_delta_pct,
        "ai_breakdown": ai_breakdown_rows,
        "top_accounts": top_accounts,
        "spend_by_region": spend_by_region,
        "recommendations_table": recommendations,
        "ai_insights": ai_insights,
        "budgets": budgets,
        "alerts": alerts,
        "window_days": days,
        "provider": provider,
        "opportunities_count": len(recent_opportunities),
        "opportunities_total_saving": opp_total,
        "savings_achieved_mtd": round(sum(s["monthly_saving"] for s in recent_savings), 2),
        "verified_savings": {
            "monthly": verified_monthly,
            "annual": round(verified_monthly * 12, 2),
            "count": len(verified_ledger),
            "delta_pct": 32.0,
            "ledger": verified_ledger,
        },
        "anomalies_open": 2 if "aws" in provs else 1,
        "budget_pct_used": 68.0,
        "recent_opportunities": recent_opportunities,
        "suppressed_opportunities": [
            {"description": "RDS metadata-catalog-01 flagged underutilized, but memory sits at 82%. "
                            "Held back: rightsizing it risks a memory-bound stall, not genuine savings.",
             "monthly_saving": 0.0, "resource": "metadata-catalog-01", "provider": "aws"},
        ] if "aws" in provs else [],
        "learning_active": True,
        "recent_savings": recent_savings,
        "error": None,
        "connected_providers": _DEMO_PROVIDERS,
        "trend": trend,
        "scorecard": {
            "overall_grade": grade,
            "overall_score": score,
            "dimensions": [
                {"name": "Tagging & allocation", "grade": "D", "score": 42,
                 "detail": "29% of spend is untagged, mostly shared CDN and data transfer with no owner."},
                {"name": "Commitment coverage", "grade": "C", "score": 61,
                 "detail": "61% of steady compute + CDN on commitments (target 80%); a CloudFront egress commit is open."},
                {"name": "Rightsizing", "grade": "C", "score": 58,
                 "detail": "47 instances flagged; 12 genuine, low-risk resizes after burst + memory checks."},
                {"name": "Idle & waste", "grade": "C", "score": 64,
                 "detail": "3 idle EKS nodes and six always-warm Snowflake warehouses."},
                {"name": "Storage tiering", "grade": "B", "score": 78,
                 "detail": "Most VOD masters lifecycle-managed to Glacier; 2.4 PB still on standard."},
            ],
        },
    }


# Category per demo provider, so the MCP connected-view tools can group them the
# same way the live registry does (cloud / llm / saas). Kubernetes reads from a
# kubeconfig, grouped with cloud.
_DEMO_PROVIDER_CATEGORY: dict[str, str] = {
    "aws": "cloud", "gcp": "cloud", "azure": "cloud", "kubernetes": "cloud",
    "openai": "llm", "anthropic": "llm",
    "datadog": "saas", "snowflake": "saas", "databricks": "saas",
}


def connected_providers() -> list[dict[str, str]]:
    """The providers a demo instance advertises as connected, in display order.
    Used by list_connected_providers / check_connector_health so the MCP "what am
    I connected to" view is populated in demo mode instead of showing everything
    as not-configured (those tools otherwise probe real credentials)."""
    return [
        {"name": p, "category": _DEMO_PROVIDER_CATEGORY.get(p, "cloud")}
        for p in _DEMO_PROVIDERS
    ]


def demo_accounts() -> dict[str, list[dict[str, Any]]]:
    """Per-provider account/subscription/org identifiers for the demo, in the
    shape list_accounts returns (provider -> list of account dicts)."""
    # A representative slice of the AWS Org, not the whole thing: a real media
    # company runs hundreds of linked accounts. Showing ~14 named ones plus the
    # count conveys the scale without inventing 312 rows.
    _aws_named = [
        {"id": "481516234203", "name": "Production"},
        {"id": "481516234211", "name": "Streaming-Delivery"},
        {"id": "481516234229", "name": "Ad-Platform"},
        {"id": "481516234237", "name": "Data-Analytics"},
        {"id": "481516234245", "name": "Recommendations"},
        {"id": "481516234253", "name": "Playback"},
        {"id": "481516234261", "name": "Content-Encoding"},
        {"id": "481516234279", "name": "Staging"},
        {"id": "481516234287", "name": "Shared-Services"},
        {"id": "481516234295", "name": "Networking"},
        {"id": "481516234303", "name": "Security-Audit"},
        {"id": "481516234311", "name": "DR"},
        {"id": "481516234329", "name": "Corp-IT"},
        {"id": "481516234337", "name": "Sandbox"},
    ]
    out: dict[str, list[dict[str, Any]]] = {
        "aws": _aws_named + [{"_note": "showing 14 of 312 linked accounts in the Org"}],
        "gcp": [{"billing_account_id": "01A2B3-C4D5E6-F7G8H9", "name": "streamco-billing",
                 "_note": "34 projects under this billing account"}],
        "azure": [{"subscription_id": "9f8e7d6c-5b4a-3210-fedc-ba9876543210",
                   "name": "streamco-prod"}],
        "kubernetes": [{"context": "prod-eks-streaming", "name": "prod-eks-streaming"}],
    }
    for p in ("openai", "anthropic", "datadog", "snowflake", "databricks"):
        out[p] = [{"org": "streamco", "name": f"streamco ({p})"}]
    return out


def saved_views() -> list[dict[str, Any]]:
    """Demo saved dashboards for the gallery, in the shape /api/views returns:
    a list of {id, card, data, saved_by, saved_at}. Lets the Saved dashboards
    surface show a populated shelf with no account connected."""
    _today = date.today()
    return [
        {"id": 9001, "saved_by": "Alex R.", "saved_at": (_today - timedelta(days=2)).isoformat(),
         "card": {"title": "Spend by team, this quarter", "template": "bar", "metric": "EffectiveCost",
                  "dimensions": ["team"]},
         "data": {"rows": [{"team": "Streaming Delivery", "metric": 2226000.0}, {"team": "Content Platform", "metric": 1164000.0},
                           {"team": "Ad Platform", "metric": 888000.0}, {"team": "Data & Analytics", "metric": 522000.0}],
                  "total": 4800000.0, "record_count": 4}},
        {"id": 9002, "saved_by": "Alex R.", "saved_at": (_today - timedelta(days=6)).isoformat(),
         "card": {"title": "AI spend by model", "template": "bar", "metric": "Cost", "dimensions": ["model"]},
         "data": {"rows": [{"model": "gpt-4o", "metric": 168000.0}, {"model": "claude-sonnet-4-5", "metric": 102000.0},
                           {"model": "o3", "metric": 62000.0}, {"model": "bedrock", "metric": 40000.0}],
                  "total": 372000.0, "record_count": 4}},
        {"id": 9003, "saved_by": "Alex R.", "saved_at": (_today - timedelta(days=11)).isoformat(),
         "card": {"title": "CDN egress by region", "template": "bar", "metric": "EffectiveCost",
                  "dimensions": ["region"]},
         "data": {"rows": [{"region": "us-east-1", "metric": 274000.0}, {"region": "eu-west-1", "metric": 201000.0},
                           {"region": "us-west-2", "metric": 162000.0}, {"region": "ap-southeast-1", "metric": 88000.0}],
                  "total": 725000.0, "record_count": 4}},
        # Tag- and time-granularity views, so the gallery answers "show me by tag"
        # and "monthly, not just 30 days" without touching a filter UI. Totals tie
        # to the $5.15M month exactly (untagged matches the 29% story).
        {"id": 9004, "saved_by": "Priya S.", "saved_at": (_today - timedelta(days=1)).isoformat(),
         "card": {"title": "Spend by tag: env", "template": "bar", "metric": "EffectiveCost",
                  "dimensions": ["env"]},
         "data": {"rows": [{"env": "production", "metric": 3708000.0}, {"env": "untagged", "metric": 689600.0},
                           {"env": "staging", "metric": 463000.0}, {"env": "dev", "metric": 287000.0}],
                  "total": 5147600.0, "record_count": 4}},
        {"id": 9005, "saved_by": "Priya S.", "saved_at": (_today - timedelta(days=3)).isoformat(),
         "card": {"title": "Monthly spend, last 3 months", "template": "bar", "metric": "EffectiveCost",
                  "dimensions": ["month"]},
         "data": {"rows": [{"month": "May", "metric": 3966000.0}, {"month": "June", "metric": 4407000.0},
                           {"month": "July (MTD)", "metric": 5147600.0}],
                  "total": 13520600.0, "record_count": 3}},
        {"id": 9006, "saved_by": "Alex R.", "saved_at": (_today - timedelta(days=5)).isoformat(),
         "card": {"title": "Untagged spend by account", "template": "bar", "metric": "EffectiveCost",
                  "dimensions": ["account"]},
         "data": {"rows": [{"account": "Streaming-Delivery", "metric": 273600.0}, {"account": "Networking", "metric": 168000.0},
                           {"account": "Shared-Services", "metric": 142000.0}, {"account": "Ad-Platform", "metric": 106000.0}],
                  "total": 689600.0, "record_count": 4}},
    ]


def bedrock_split() -> dict[str, Any]:
    """Demo Bedrock input/output/cache split, consistent with the ~$330 Bedrock
    line in llm_costs(): input-heavy and uncached, which is the signature
    caching finding. Lets optimize_ai_spend fire the prompt-caching lever with
    no credentials."""
    return {
        "input_cost": 35600.0,     # ~89% of the $40,000 Bedrock bill
        "output_cost": 4400.0,
        "cache_read_cost": 0.0,
        "cache_write_cost": 0.0,
        "input_share_pct": 89.0,
        "caching_active": False,
    }


def ai_engineering_report() -> dict:
    """Demo: what AI shipped this month, by model, joined to AI spend."""
    return {
        "configured": True,
        "window_days": 30,
        "total_pr_count": 210,
        "ai_pr_count": 168,
        "human_pr_count": 42,
        "ai_share_pct": 80.0,
        "total_llm_spend_usd": 18400.0,
        "by_label": {
            "Claude Opus 4.8": {
                "label": "Claude Opus 4.8", "pr_count": 92, "high": 24, "medium": 48, "low": 20,
                "lines_changed": 38200, "llm_spend_usd": 9200.0, "spend_share_pct": 50.0,
                "cost_per_pr_usd": 100.0,
                "examples": [
                    {"title": "Add per-title CDN egress attribution", "magnitude": "high", "lines": 620, "url": "", "repo": "streamco/streaming-platform"},
                    {"title": "Cache catalog context on the recommender", "magnitude": "high", "lines": 480, "url": "", "repo": "streamco/recommendations"},
                ],
            },
            "Claude Sonnet 4.6": {
                "label": "Claude Sonnet 4.6", "pr_count": 58, "high": 0, "medium": 38, "low": 20,
                "lines_changed": 9400, "llm_spend_usd": 5600.0, "spend_share_pct": 30.4,
                "cost_per_pr_usd": 96.6,
                "examples": [
                    {"title": "Tune the ad-decisioning bid timeout", "magnitude": "medium", "lines": 150, "url": "", "repo": "streamco/ad-platform"},
                ],
            },
            "OpenAI Codex": {
                "label": "OpenAI Codex", "pr_count": 18, "high": 2, "medium": 8, "low": 8,
                "lines_changed": 2600, "llm_spend_usd": 3600.0, "spend_share_pct": 19.6,
                "cost_per_pr_usd": 200.0, "examples": [],
            },
            "Human": {
                "label": "Human", "pr_count": 42, "high": 8, "medium": 18, "low": 16,
                "lines_changed": 14800, "examples": [],
            },
        },
        "_demo_mode": True,
    }


DEMO_RESPONSES: dict[str, Any] = {
    "get_cost_summary":             cost_summary,
    "get_anomalies":                anomalies,
    "get_rightsizing_recommendations": rightsizing,
    "get_kubernetes_costs":         kubernetes_costs,
    "get_cluster_efficiency":       cluster_efficiency,
    "get_tag_cost_breakdown_cur":   cost_summary_cur,
    "get_llm_costs":                llm_costs,
    "get_llm_cost_by_model":        llm_costs,
    "explain_recent_cost_drivers":  cost_drivers,
    "get_ai_engineering_report":    ai_engineering_report,
}


def get_demo_response(tool_name: str) -> dict[str, Any] | None:
    """
    Return a demo response for the given tool name, or None if not available.
    Call this at the top of each MCP tool when FINOPS_DEMO_MODE=1.
    """
    fn = DEMO_RESPONSES.get(tool_name)
    if fn is None:
        return None
    result = fn()
    result["_demo_mode"] = True
    return result


# ── Agent (AI Analyst) demo safety net ───────────────────────────────────────
# The chat agent can call ~60 tools. Only the DEMO_RESPONSES set self-serves demo
# data; the rest would query real credentials and leak/contradict the demo. This
# layer sits at the single bridge chokepoint (slack_bot.bridge.execute_bridge_tool)
# so that in demo mode NO agent tool call ever reaches a real cloud account:
#   - self-demo / local-only tools -> None (fall through; already safe),
#   - slice_costs -> a synthetic slice built from the demo dataset,
#   - common cost tools -> a demo dict derived from the same numbers,
#   - anything else -> a safe placeholder that names the sample and leaks nothing.

# Tools that already return demo data (or are local-only) on their own path.
_AGENT_SELF_DEMO = set(DEMO_RESPONSES) | {
    "optimize_ai_spend", "list_connected_providers", "list_accounts",
    "compare_providers", "check_connector_health", "whoami", "what_can_nable_do",
}
_AGENT_LOCAL_OK = {"pin_view", "list_pinned_views", "get_pinned_view", "unpin_view"}


def _demo_total() -> dict[str, Any]:
    by_provider = {p: round(sum(s["amount"] for s in _PROVIDER_SERVICES[p]), 2)
                   for p in _DEMO_PROVIDERS}
    return {"total_usd": round(sum(by_provider.values()), 2), "by_provider": by_provider,
            "period": cost_summary()["period"], "_demo_mode": True}


def _demo_commitment() -> dict[str, Any]:
    return {
        "coverage_pct": 61.0, "on_demand_pct": 39.0, "target_pct": 80.0,
        "by_provider": {"aws": 63.0, "gcp": 58.0, "snowflake": 55.0},
        "recommendation": (
            "Steady CDN egress and the encoder baseline are uncommitted. A 1-year compute "
            "Savings Plan (~$88k/mo) and a CloudFront committed-egress tier (~$62k/mo) close "
            "most of the gap toward the 80% target."),
        "_demo_mode": True,
    }


def _demo_slice(args: dict[str, Any]) -> dict[str, Any]:
    """Synthetic 'moldable view' slice from the demo dataset, in the shape the
    web Ask tab renders as a pinnable cost card."""
    dims = args.get("dimensions") or []
    dim = (str(dims[0]) if dims else "provider").lower()
    metric = args.get("metric") or "EffectiveCost"
    total_all = sum(s["amount"] for p in _DEMO_PROVIDERS for s in _PROVIDER_SERVICES[p])
    if dim in ("service", "product", "service_name"):
        key = "service"
        rows = sorted(
            ({"service": s["service"], "metric": s["amount"]}
             for p in _DEMO_PROVIDERS for s in _PROVIDER_SERVICES[p]),
            key=lambda x: -x["metric"])[:15]
    elif dim in ("team", "tag", "owner", "costcenter", "cost_center"):
        key = "team"
        rows = [{"team": k, "metric": round(v, 2)} for k, v in cost_summary_cur()["by_tag_team"].items()]
    elif dim in ("account", "subaccount", "subaccountid", "account_id", "linkedaccount"):
        key = "account"
        rows = [{"account": a["name"], "metric": round(total_all * a["share"], 2)} for a in _DEMO_ACCOUNTS]
    elif dim in ("region", "regionid", "location"):
        key = "region"
        rows = [{"region": r["region"], "metric": round(total_all * r["share"], 2)} for r in _DEMO_REGIONS]
    else:
        key = "provider"
        rows = [{"provider": p, "metric": round(sum(s["amount"] for s in _PROVIDER_SERVICES[p]), 2)}
                for p in _DEMO_PROVIDERS]
    rows.sort(key=lambda x: -x["metric"])
    total = round(sum(r["metric"] for r in rows), 2)
    return {
        "card": {"title": f"{metric} by {key}", "template": "bar", "metric": metric,
                 "dimensions": [key], "period": {"start": _MONTH_START, "end": _YESTERDAY}},
        "result": {"rows": rows, "total": total, "record_count": len(rows),
                   "metric": metric, "dimensions": [key]},
        "_demo_mode": True,
    }


def _agent_intercepts() -> dict[str, Any]:
    """Lazily built so dashboard_data() (heavier) is only computed on a hit."""
    dd = dashboard_data()
    return {
        "get_costs_by_service": {"by_service": cost_summary()["by_service"],
                                 "total_usd": cost_summary()["total_usd"],
                                 "period": cost_summary()["period"], "_demo_mode": True},
        "get_top_cost_drivers": cost_drivers(),
        "explain_cost_change": cost_drivers(),
        "get_costs_by_team": {"by_team": cost_summary_cur()["by_tag_team"],
                              "untagged_pct": cost_summary_cur()["untagged_pct"],
                              "total_usd": cost_summary_cur()["total_usd"], "_demo_mode": True},
        "get_total_spend_all_sources": _demo_total(),
        "get_cost_summary_all_accounts": _demo_total(),
        "get_saas_spend_summary": {"by_provider": {p: round(sum(s["amount"] for s in _PROVIDER_SERVICES[p]), 2)
                                                   for p in ("datadog", "snowflake", "databricks")},
                                   "_demo_mode": True},
        "forecast_costs": {"projected_month_total": dd["projected_month_total"],
                           "budget": dd["forecast_panel"]["budget"],
                           "vs_budget_pct": dd["forecast_panel"]["vs_budget_pct"],
                           "note": dd["forecast_panel"]["note"], "_demo_mode": True},
        "get_commitment_analysis": _demo_commitment(),
        "get_commitment_coverage_by_tag": _demo_commitment(),
        "check_budget_status": {"budgets": dd["budgets"], "_demo_mode": True},
        "list_budgets": {"budgets": dd["budgets"], "_demo_mode": True},
        "get_savings_summary": {"potential_monthly": dd["opportunities_total_saving"],
                                "verified_monthly": dd["verified_savings"]["monthly"],
                                "recommendations": dd["recommendations_table"], "_demo_mode": True},
        "get_savings_ledger": {"ledger": dd["verified_savings"]["ledger"],
                               "monthly": dd["verified_savings"]["monthly"], "_demo_mode": True},
        "list_savings_recommendations": {"recommendations": dd["recommendations_table"],
                                         "open_potential_usd": dd["opportunities_total_saving"], "_demo_mode": True},
        "get_efficiency_scorecard": {"scorecard": dd["scorecard"], "_demo_mode": True},
        "get_nable_roi": {"verified_monthly": dd["verified_savings"]["monthly"],
                          "verified_annual": dd["verified_savings"]["annual"], "_demo_mode": True},
        "get_cost_trends": {"daily_series": dd["daily_series"], "_demo_mode": True},
        "get_cost_history": {"trend": dd["trend"], "_demo_mode": True},
        "get_ai_kpis": {"metrics": dd["ai_efficiency"]["metrics"],
                        "ai_pct_of_spend": dd["ai_efficiency"]["ai_pct_of_spend"], "_demo_mode": True},
    }


def demo_bridge_result(name: str, args: dict[str, Any] | None) -> dict[str, Any] | None:
    """Demo-safe result for an agent tool call, or None to let the real (already
    demo-safe) tool run. Guarantees no agent tool reaches real credentials in
    demo mode: unknown tools get a placeholder, never a live call."""
    args = args or {}
    if name in _AGENT_SELF_DEMO or name in _AGENT_LOCAL_OK:
        return None
    if name == "slice_costs":
        return _demo_slice(args)
    hit = _agent_intercepts().get(name)
    if hit is not None:
        return hit
    return {
        "demo_mode": True,
        "note": (
            "This is the StreamCo sample environment, so that specific detail isn't in the sample "
            "dataset. Ask about total spend, cost drivers, spend by service / team / account / region, "
            "anomalies, rightsizing, commitments, budgets, forecast, savings, or AI and LLM cost."),
    }
