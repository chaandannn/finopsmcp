"""
Connection-aware tool surface: which of the ~191 registered tools get ADVERTISED
to the model.

Every tool definition advertised over tools/list rides in the model's context on
every message, so a user pays recurring tokens for tools they cannot use: an
AWS-only machine has no use for the Azure, GCP, Kubernetes, or Databricks tools,
and models pick tools worse as the surface grows. This module maps every tool to
exactly one family and decides, from cheap LOCAL signals only, which families are
worth advertising right now.

Advertisement-only: nothing here unregisters a tool. The MCP call path resolves
tools against the registry, not the advertised list (verified on mcp 1.27.2), so
a hidden tool called by name still runs. That keeps the in-chat connect flow
intact: connect_aws works, then the AWS tools are usable immediately and appear
in the list after a tools/list_changed notification or an editor restart.

Rules:
  - core is always advertised, and is deliberately SMALL: only what is useful
    with zero providers connected (connect_*, status/diagnosis, the local agent
    budget, the preflight guard). ~20 tools.
  - cost holds the cross-provider cost/report/recommendation tools. They are not
    provider-specific, but every one needs a data source, so they are advertised
    only once some provider family is connected. On a fresh machine they would
    each answer "no accounts connected", which is pure context tax on precisely
    the user who has not connected yet.
  - a provider family is advertised when local signals say it is connected
    (env vars first, then vault key names, then accounts.yaml / kubeconfig).
    Detection never touches the network and is cached ~30s.
  - llm is advertised when LLM keys exist OR aws is connected, mirroring the
    capabilities gate: Bedrock token tools work off the AWS account alone.
  - FINOPS_ALL_TOOLS=1 (the existing escape hatch) and demo mode advertise
    everything.
  - an unmapped tool advertises with a warning (fail open); the completeness
    test in tests/test_tool_surface.py is the real enforcement, so a new tool
    that nobody classifies fails CI instead of silently hiding.
"""
from __future__ import annotations

import logging
import os
import re
import time
from pathlib import Path

log = logging.getLogger(__name__)

# ── the family map (every registered tool, exactly once) ──────────────────────

_CORE: frozenset[str] = frozenset({
    # Advertised ALWAYS, including on a machine with nothing connected. The test
    # for membership here is narrow: does this tool do something useful with ZERO
    # providers connected? Connecting, diagnosing, discovering what is available,
    # and the two surfaces that need no cloud at all (the local agent budget and
    # the preflight/policy guard). Everything else lives in _COST below, because
    # a tool that can only answer "no accounts connected" is pure context tax on
    # the exact user who has not connected yet.
    "activate_pro",
    "check_action_policy",
    "check_ai_budget",
    "check_connector_health",
    "connect_aws",
    "connect_azure",
    "connect_gcp",
    "connect_opencost",
    "estimate_change_cost",
    "estimate_terraform_cost",
    # The one cost tool that stays visible with nothing connected, on purpose.
    # A fresh user's first question is "what am I spending?", and the model needs
    # something to reach for: get_cost_summary's no-accounts response names
    # connect_aws/connect_gcp/connect_azure as callable next steps, which is how
    # an in-chat connect actually starts. Without it the model can only answer
    # from its own words. ~250 tokens is cheap insurance on the activation path.
    "get_cost_summary",
    "get_agent_team",
    "get_ai_budget_status",
    "list_accounts",
    "list_connected_providers",
    "list_profiles",
    "list_vault_credentials",
    "nable_setup_status",
    "set_ai_budget",
    "what_can_nable_do",
    "whoami",
})

_COST: frozenset[str] = frozenset({
    # Advertised once ANY provider is connected. These are cross-provider rather
    # than provider-specific, so they cannot hang off the aws/azure/gcp families,
    # but every one of them needs at least one data source to say anything. On a
    # fresh machine they would each return "no accounts connected", so they stay
    # out of the list until there is something to talk about.
    #
    # Hiding is advertisement-only: the call path resolves against the registry,
    # so a model that names one of these still runs it, and connect_* fires
    # tools/list_changed so they appear the moment a provider lands.
    "acknowledge_anomaly",
    "audit_duplicate_spend",
    "audit_terraform_tags",
    "benchmark_costs",
    "check_budget_status",
    "check_notification_config",
    "cleanup_idle_resources",
    "compare_providers",
    "create_api_key",
    "delete_alert_policy",
    "delete_budget",
    "dismiss_recommendation",
    "explain_cost_change",
    "explain_recent_cost_drivers",
    "export_board_summary",
    "export_cost_report",
    "export_cost_report_csv",
    "fetch_invoice_emails",
    "forecast_costs",
    "forget_cost_context",
    "generate_account_dashboard",
    "generate_terraform_tag_fixes",
    "get_account_anomalies",
    "get_ai_engineering_report",
    "get_ai_kpis",
    "get_anomalies",
    "get_business_metrics",
    "get_commitment_analysis",
    "get_commitment_coverage_by_tag",
    "get_cost_history",
    "get_cost_summary_all_accounts",
    "get_cost_trends",
    "get_costs_by_service",
    "get_costs_by_team",
    "get_credit_status",
    "get_data_transfer_costs",
    "get_effective_rate_profile",
    "get_efficiency_scorecard",
    "get_focus_costs",
    "get_gpu_infra_costs",
    "get_idle_load_balancers",
    "get_instance_deep_analysis",
    "get_label_costs",
    "get_learned_cost_context",
    "get_nable_roi",
    "get_ou_cost_breakdown",
    "get_pinned_view",
    "get_recommendation_learning",
    "get_recommendation_quality",
    "get_ri_waste_detail",
    "get_rightsizing_recommendations",
    "get_saas_spend_summary",
    "get_savings_ledger",
    "get_savings_plan_showback",
    "get_savings_summary",
    "get_service_cost",
    "get_storage_info",
    "get_tableau_connection_info",
    "get_top_cost_drivers",
    "get_top_spending_accounts",
    "get_total_spend_all_sources",
    "get_traffic_cost_breakdown",
    "get_unit_economics",
    "get_view",
    "get_workload_costs",
    "identify_nonprod_scheduling_opportunities",
    "list_active_services",
    "list_alert_policies",
    "list_api_keys",
    "list_budgets",
    "list_idle_resources",
    "list_pinned_views",
    "list_savings_recommendations",
    "list_views",
    "mark_recommendation_acted_on",
    "open_rightsizing_pr",
    "open_terraform_tag_pr",
    "pin_view",
    "recommend_database_savings_plans",
    "remember_cost_context",
    "revoke_api_key",
    "run_attribution_now",
    "run_full_cost_audit",
    "scan_waste_patterns",
    "set_alert_policy",
    "set_budget",
    "set_business_metrics",
    "slice_costs",
    "start_dashboard_server",
    "suggest_cost_policies",
    "sync_budgets_from_yaml",
    "take_snapshot_now",
    "unpin_view",
    "verify_savings",
})

_AWS: frozenset[str] = frozenset({
    "audit_aws_waste",
    "audit_cloudwatch_logs_ia_opportunities",
    "audit_cloudwatch_metric_cardinality",
    "audit_cloudwatch_orphaned_alarms",
    "audit_ebs_snapshot_replication",
    "audit_efs_cross_az_mounts",
    "audit_nlb_cross_zone_costs",
    "audit_public_ipv4_addresses",
    "audit_rds_manual_snapshots",
    "audit_s3_intelligent_tiering",
    "audit_s3_transfer_acceleration",
    "audit_spot_diversification",
    "audit_textract_environment_waste",
    "get_documentdb_costs",
    "get_ecr_cleanup_recommendations",
    "get_ecs_rightsizing_recommendations",
    "get_idle_rds_instances",
    "get_kendra_costs",
    "get_marketplace_costs",
    "get_org_cost_summary",
    "get_rds_rightsizing_recommendations",
    "get_resource_cost_breakdown_aws",
    "get_s3_incomplete_multipart_uploads",
    "get_tag_cost_breakdown_cur",
    "get_team_scorecards",
    "get_textract_costs",
    "list_aws_accounts",
    "list_org_accounts",
    "recommend_lambda_snapstart",
    "recommend_spot_adoption",
    "scan_cloudwatch_waste",
    "scan_graviton_migration_opportunities",
    "scan_lambda_concurrency_waste",
    "scan_s3_bucket_key_opportunities",
})

_AZURE: frozenset[str] = frozenset({
    "forecast_azure_costs",
    "get_azure_advisor_recommendations",
    "get_azure_budgets",
    "get_azure_cost_by_dimension",
    "get_azure_reservation_utilization", "audit_azure_reservation_scope",
    "get_azure_vm_rightsizing",
    "get_resource_cost_breakdown_azure",
})

_GCP: frozenset[str] = frozenset({
    "audit_gcp_waste", "audit_gcp_nat_fees", "audit_gcs_request_overhead",
    "get_gcp_recommendations",
})

_KUBERNETES: frozenset[str] = frozenset({
    "compare_kubernetes_clusters",
    "create_kubernetes_waste_tickets",
    "estimate_helm_diff_cost",
    "get_cluster_efficiency",
    "get_databricks_cluster_efficiency",
    "get_helm_release_costs",
    "get_kubernetes_cost_trends",
    "get_kubernetes_costs",
    "get_kubernetes_namespace_breakdown",
    "list_kubernetes_contexts",
})

_DATABRICKS: frozenset[str] = frozenset({
    "get_databricks_costs",
    "get_databricks_dbu_breakdown",
    "get_databricks_job_costs",
})

_LLM: frozenset[str] = frozenset({
    "forecast_llm_costs",
    "get_ai_spend_monitor",
    "get_bedrock_costs",
    "get_langfuse_model_costs",
    "get_langfuse_trace_volume",
    "get_llm_commitment_analysis",
    "get_llm_cost_by_model",
    "get_llm_costs",
    "optimize_ai_spend",
    "recommend_bedrock_model_routing",
})

_NOTIFICATIONS: frozenset[str] = frozenset({
    "cancel_report_subscription",
    "list_report_subscriptions",
    "publish_cost_report_to_notion",
    "push_to_n8n",
    "push_weekly_insight",
    "send_digest_now",
    "send_onboarding_email",
    "send_report_now",
    "send_weekly_digest_now",
    "subscribe_to_report",
})

_TICKETS: frozenset[str] = frozenset({
    "create_anomaly_tickets",
    "create_rightsizing_tickets",
    "create_scorecard_tickets",
    "create_ticket",
})

FAMILY_TOOLS: dict[str, frozenset[str]] = {
    "core": _CORE,
    "cost": _COST,
    "aws": _AWS,
    "azure": _AZURE,
    "gcp": _GCP,
    "kubernetes": _KUBERNETES,
    "databricks": _DATABRICKS,
    "llm": _LLM,
    "notifications": _NOTIFICATIONS,
    "tickets": _TICKETS,
}

# Families that actually carry spend data. Used to gate the cross-provider "cost"
# family: notifications and tickets are places to SEND findings, not sources to
# read them from, so they do not by themselves make cost tools meaningful.
_DATA_SOURCE_FAMILIES: frozenset[str] = frozenset(
    {"aws", "azure", "gcp", "kubernetes", "databricks", "llm"}
)

_FAMILY_OF: dict[str, str] = {
    name: family for family, names in FAMILY_TOOLS.items() for name in names
}

# ── read/write classification for MCP tool annotations ─────────────────────────
# The Anthropic Connectors Directory requires every tool to carry a title and a
# readOnlyHint (or destructiveHint). nable is read-only + propose-only by design,
# so the DEFAULT is read-only: only tools that actually mutate state (store creds,
# persist to the local DB, open a PR, send a message, create a ticket) are writes,
# and only the delete/revoke ones are destructive. A completeness test asserts
# every name here is a real registered tool, so a rename can't silently mislabel.
WRITE_TOOLS: frozenset[str] = frozenset({
    "acknowledge_anomaly", "cancel_report_subscription",
    "connect_aws", "connect_azure", "connect_gcp", "connect_opencost",
    "create_anomaly_tickets", "create_api_key", "create_kubernetes_waste_tickets",
    "create_rightsizing_tickets", "create_scorecard_tickets", "create_ticket",
    "delete_alert_policy", "delete_budget", "dismiss_recommendation",
    "generate_terraform_tag_fixes", "mark_recommendation_acted_on",
    "open_rightsizing_pr", "open_terraform_tag_pr", "pin_view", "unpin_view",
    "publish_cost_report_to_notion", "push_to_n8n", "push_weekly_insight",
    "revoke_api_key", "run_attribution_now",
    "send_digest_now", "send_onboarding_email", "send_report_now",
    "send_weekly_digest_now", "set_alert_policy", "set_budget",
    "set_business_metrics", "start_dashboard_server", "subscribe_to_report",
    "sync_budgets_from_yaml", "take_snapshot_now", "verify_savings",
    "remember_cost_context", "forget_cost_context",
})
# The subset that removes/revokes something (destructiveHint = true). Additive
# writes (create/set/send/pin/open-PR) are writes but NOT destructive.
DESTRUCTIVE_TOOLS: frozenset[str] = frozenset({
    "delete_alert_policy", "delete_budget", "revoke_api_key",
})

# Acronyms to upper-case when humanizing a tool name into a display title.
_ACRONYMS = {
    "aws": "AWS", "gcp": "GCP", "azure": "Azure", "gke": "GKE", "eks": "EKS",
    "ec2": "EC2", "ecs": "ECS", "ecr": "ECR", "rds": "RDS", "s3": "S3",
    "ebs": "EBS", "efs": "EFS", "nlb": "NLB", "elb": "ELB", "k8s": "K8s",
    "llm": "LLM", "ai": "AI", "api": "API", "cur": "CUR", "ri": "RI",
    "roi": "ROI", "kpi": "KPI", "sp": "SP", "pr": "PR", "iam": "IAM",
    "gpu": "GPU", "dbu": "DBU", "ipv4": "IPv4", "sla": "SLA", "os": "OS",
    "ou": "OU", "csv": "CSV", "n8n": "n8n", "focus": "FOCUS",
}


def tool_title(name: str) -> str:
    """Humanize a snake_case tool name into a display title, upper-casing known
    acronyms. e.g. get_cost_summary -> 'Get cost summary'; get_ai_kpis -> 'Get AI KPIs'."""
    words = name.split("_")
    out = []
    for i, w in enumerate(words):
        if w in _ACRONYMS:
            out.append(_ACRONYMS[w])
        elif w.endswith("s") and w[:-1] in _ACRONYMS:  # plural acronym: kpis -> KPIs
            out.append(_ACRONYMS[w[:-1]] + "s")
        elif i == 0:
            out.append(w.capitalize())
        else:
            out.append(w)
    return " ".join(out)


def tool_annotation(name: str) -> dict:
    """Annotation fields for a tool: (title, readOnlyHint, destructiveHint).
    Read-only is the default; only WRITE_TOOLS mutate, only DESTRUCTIVE_TOOLS delete."""
    write = name in WRITE_TOOLS
    return {
        "title": tool_title(name),
        "readOnlyHint": not write,
        "destructiveHint": name in DESTRUCTIVE_TOOLS,
    }

# ── local connection signals per family ────────────────────────────────────────
# Env keys are exact matches (mirrored into os.environ by a connect); vault
# prefixes match the key names a setup writes into the local vault.

_ENV_KEYS: dict[str, tuple[str, ...]] = {
    "aws": ("AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "AWS_PROFILE", "AWS_ROLE_ARNS"),
    "azure": ("AZURE_TENANT_ID", "AZURE_CLIENT_ID", "AZURE_SUBSCRIPTION_ID"),
    "gcp": ("GOOGLE_APPLICATION_CREDENTIALS", "GCP_SERVICE_ACCOUNT_KEY_PATH",
             "GCP_BILLING_ACCOUNT_IDS", "GCP_PROJECT_IDS"),
    "databricks": ("DATABRICKS_HOST", "DATABRICKS_TOKEN"),
    "llm": ("OPENAI_API_KEY", "ANTHROPIC_API_KEY", "OPENROUTER_API_KEY",
             "LITELLM_BASE_URL", "LANGFUSE_PUBLIC_KEY", "TOGETHER_API_KEY",
             "REPLICATE_API_TOKEN", "COHERE_API_KEY", "MISTRAL_API_KEY"),
    "notifications": ("SLACK_BOT_TOKEN", "SLACK_WEBHOOK_URL", "TEAMS_WEBHOOK_URL",
                       "NOTION_API_KEY", "N8N_WEBHOOK_URL", "SMTP_HOST"),
    "tickets": ("JIRA_URL", "JIRA_BASE_URL", "JIRA_API_TOKEN", "LINEAR_API_KEY",
                 "GITHUB_TOKEN", "GITHUB_FINOPS_REPO"),
}

_VAULT_PREFIXES: dict[str, tuple[str, ...]] = {
    "aws": ("AWS_",),
    "azure": ("AZURE_",),
    "gcp": ("GCP_", "GOOGLE_"),
    "databricks": ("DATABRICKS_",),
    "llm": ("OPENAI", "ANTHROPIC", "OPENROUTER", "LITELLM", "LANGFUSE",
             "TOGETHER", "REPLICATE", "COHERE", "MISTRAL"),
    "notifications": ("SLACK", "TEAMS", "NOTION", "N8N", "SMTP"),
    "tickets": ("JIRA", "LINEAR", "GITHUB"),
}

_CACHE_TTL = 30.0
_cache: tuple[float, frozenset[str]] | None = None


def _reset_cache_for_tests() -> None:
    global _cache
    _cache = None


def _kubeconfig_present() -> bool:
    """Kubernetes looks usable on this machine. Separate function so tests can
    stub it: the real check touches the developer's actual ~/.kube/config."""
    try:
        return bool(os.environ.get("KUBECONFIG")) or (Path.home() / ".kube" / "config").exists()
    except Exception:
        return False


def _detect_families() -> frozenset[str]:
    """Which provider families look connected, from local signals only."""
    found: set[str] = set()

    for family, keys in _ENV_KEYS.items():
        if any(os.environ.get(k) for k in keys):
            found.add(family)

    remaining = set(_VAULT_PREFIXES) - found
    if remaining:
        try:
            from .security.vault import Vault

            vault_keys = [k.upper() for k in Vault.default().list_keys()]
            for family in list(remaining):
                prefixes = _VAULT_PREFIXES[family]
                if any(k.startswith(prefixes) for k in vault_keys):
                    found.add(family)
        except Exception:
            pass

    if "aws" not in found:
        # A role/profile connect writes accounts.yaml without credential envs.
        try:
            from .accounts import list_accounts

            if list_accounts():
                found.add("aws")
        except Exception:
            pass

    if "kubernetes" not in found and _kubeconfig_present():
        found.add("kubernetes")

    # Bedrock token tools work off the AWS account alone (capabilities.py gate).
    if "aws" in found:
        found.add("llm")

    return frozenset(found)


def connected_families() -> frozenset[str]:
    """Detected families, cached ~30s so tools/list stays effectively free."""
    global _cache
    now = time.monotonic()
    if _cache is not None and _cache[0] > now:
        return _cache[1]
    families = _detect_families()
    _cache = (now + _CACHE_TTL, families)
    return families


def _all_tools_forced() -> bool:
    return os.getenv("FINOPS_ALL_TOOLS", "").strip().lower() in ("1", "true", "yes")


_SECTION = re.compile(
    r"\n[ \t]*(Args|Arguments|Parameters|Returns|Raises|Examples?|Notes?|Use when)"
    r"[ \t]*:[ \t]*\n", re.IGNORECASE)
# Dropped outright: MCP already sends the inputSchema, which carries every
# parameter's name, type, required-ness and description. A prose Args: block is
# the same information a second time, and on this install that duplicate is
# ~6,300 tokens riding in context on every single message.
_DROP = {"args", "arguments", "parameters", "returns", "raises"}
_KEEP_EXAMPLES = 2      # the first two map user phrasing to tool; the rest repeat


def compact_description(desc: str, keep_examples: int = _KEEP_EXAMPLES) -> str:
    """The advertised form of a tool description: what helps the model CHOOSE.

    A description exists so the model can pick the right tool. It does not need
    to teach the domain or re-state the schema. Measured on a connected install:
    147 advertised tools carried ~25,300 tokens of description, of which ~24%
    was a prose copy of the inputSchema and ~23% was five-deep example lists.

    Source docstrings keep their full text. This trims only the ADVERTISED copy,
    so `nable tools`, the docs generator and anyone reading the code still get
    the long form. Reversible by not calling it.
    """
    if not desc:
        return desc
    marks = [(m.start(), m.end(), m.group(1).lower()) for m in _SECTION.finditer(desc)]
    if not marks:
        return desc.strip()

    out, cursor = [desc[:marks[0][0]]], None
    for i, (start, end, name) in enumerate(marks):
        body_end = marks[i + 1][0] if i + 1 < len(marks) else len(desc)
        if name in _DROP:
            continue
        body = desc[end:body_end]
        if name.startswith("example"):
            # Keep the first N bullets. A tool whose examples are prose rather
            # than a list keeps the first line, which is the same idea.
            lines = [ln for ln in body.splitlines() if ln.strip()]
            body = "\n".join(lines[:keep_examples])
            if not body.strip():
                continue
        out.append(desc[start:end] + body.rstrip() + "\n")
        cursor = i
    _ = cursor
    return "".join(out).strip()


def advertise(tool_name: str) -> bool:
    """Should this tool appear in tools/list right now?"""
    if _all_tools_forced():
        return True
    try:
        from .demo_data import is_demo

        if is_demo():
            return True  # the demo showcases the whole product
    except Exception:
        pass

    family = _FAMILY_OF.get(tool_name)
    if family is None:
        # Fail open at runtime; the completeness test is the enforcement.
        log.warning("tool %r has no family in tool_surface; advertising it", tool_name)
        return True
    if family == "core":
        return True
    if family == "cost":
        # Cross-provider tools: not tied to one cloud, but useless without at
        # least one data source. Gate on any PROVIDER being connected, not on
        # connected_families() being non-empty: notifications and tickets are
        # integrations, not sources of spend, and a machine with only a Slack
        # token still has nothing to report on.
        return bool(connected_families() & _DATA_SOURCE_FAMILIES)
    return family in connected_families()
