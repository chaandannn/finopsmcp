"""
Storage layer — SQLite (default, local) or Postgres (shared team mode).

Single-engineer setup:  no config needed → SQLite at ~/.finops/finops.db
Shared team setup:      set DATABASE_URL=postgresql://user:pass@host/dbname
                        → connects directly, all engineers share one DB

The DATABASE_URL env var is the only config change needed to go from local
to shared mode. All table definitions work on both backends.
"""
from __future__ import annotations

import logging
import os
import stat
from datetime import datetime, timedelta, timezone
from pathlib import Path

from sqlalchemy import (
    Boolean, Column, DateTime, Float, Index, Integer, JSON, MetaData,
    String, Table, Text, create_engine, delete, event, inspect, select, text,
)
from sqlalchemy.engine import Engine

log = logging.getLogger(__name__)

_DATA_DIR: Path | None = None
_ENGINE: Engine | None = None

metadata = MetaData()

# ── Core tables ───────────────────────────────────────────────────────────────

cost_snapshots = Table(
    "cost_snapshots", metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("provider", String(64), nullable=False),
    Column("service", String(256), nullable=False),
    Column("account_id", String(128), nullable=False),
    Column("region", String(64), nullable=False, default=""),
    Column("snapshot_date", String(10), nullable=False),   # YYYY-MM-DD
    Column("amount_usd", Float, nullable=False, default=0.0),
    Column("granularity", String(16), nullable=False, default="DAILY"),
    Column("captured_at", DateTime, nullable=False),
)

cost_snapshots_archive = Table(
    "cost_snapshots_archive", metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("provider", String(64), nullable=False),
    Column("service", String(256), nullable=False),
    Column("account_id", String(128), nullable=False),
    Column("region", String(64), nullable=False, default=""),
    Column("snapshot_date", String(10), nullable=False),
    Column("amount_usd", Float, nullable=False, default=0.0),
    Column("granularity", String(16), nullable=False, default="DAILY"),
    Column("captured_at", DateTime, nullable=False),
)

anomalies = Table(
    "anomalies", metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("provider", String(64), nullable=False),
    Column("service", String(256), nullable=False),
    Column("account_id", String(128), nullable=False),
    Column("detected_at", DateTime, nullable=False),
    Column("snapshot_date", String(10), nullable=False),
    Column("severity", String(16), nullable=False),        # high / medium / low
    Column("direction", String(8), nullable=False),        # spike / drop
    Column("pct_change", Float, nullable=False),
    Column("z_score", Float, nullable=False),
    Column("baseline_mean", Float, nullable=False),
    Column("current_amount", Float, nullable=False),
    Column("acknowledged", Boolean, nullable=False, default=False),
    Column("notified", Boolean, nullable=False, default=False),
    Column("metadata", Text, nullable=True),               # JSON: ticket_url, ack_by, etc.
)

tag_rules = Table(
    "tag_rules", metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("provider", String(64), nullable=False, default="*"),
    Column("tag_key", String(128), nullable=False),
    Column("tag_value_pattern", String(256), nullable=False, default="*"),
    Column("maps_to_field", String(64), nullable=False),   # team / service / env
    Column("maps_to_value", String(256), nullable=False),
    Column("priority", Integer, nullable=False, default=100),
)

attributed_costs = Table(
    "attributed_costs", metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("provider", String(64), nullable=False),
    Column("service", String(256), nullable=False),
    Column("account_id", String(128), nullable=False),
    Column("team", String(256), nullable=False, default="unattributed"),
    Column("environment", String(64), nullable=False, default=""),
    Column("snapshot_date", String(10), nullable=False),
    Column("amount_usd", Float, nullable=False, default=0.0),
    Column("captured_at", DateTime, nullable=False),
)

audit_log = Table(
    "audit_log", metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("ts", DateTime, nullable=False),
    Column("operation", String(32), nullable=False),
    Column("key_name", String(256), nullable=False),
    Column("client_pid", Integer, nullable=True),
    Column("client_user", String(128), nullable=True),
    Column("detail", Text, nullable=True),
)

# ── Business metrics — time-series store for unit economics ──────────────────

business_metrics = Table(
    "business_metrics", metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("metric_date", String(10), nullable=False),          # YYYY-MM-DD
    Column("arr_usd", Float, nullable=True),                    # Annual Recurring Revenue
    Column("mrr_usd", Float, nullable=True),                    # Monthly Recurring Revenue
    Column("mau", Integer, nullable=True),                      # Monthly Active Users
    Column("dau", Integer, nullable=True),                      # Daily Active Users
    Column("paying_customers", Integer, nullable=True),         # paying customer count
    Column("api_calls_monthly", Integer, nullable=True),        # API calls per month
    Column("employees", Integer, nullable=True),                # headcount
    Column("custom_metrics", Text, nullable=True),              # JSON: {"metric": value}
    Column("notes", Text, nullable=True),                       # optional free-text context
    # Runway inputs (Phase 1 business-context layer). nable sees infra spend, not
    # payroll, so company runway needs monthly_opex supplied by the user.
    Column("cash_on_hand_usd", Float, nullable=True),           # cash in the bank
    Column("last_raise_amount_usd", Float, nullable=True),      # size of last round
    Column("last_raise_date", String(10), nullable=True),       # YYYY-MM-DD
    Column("monthly_opex_usd", Float, nullable=True),           # total monthly burn incl payroll
    Column("captured_at", DateTime, nullable=False),
)

# ── Moat tables — data that accumulates value over time ───────────────────────

effective_rates = Table(
    "effective_rates", metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("provider", String(64), nullable=False),
    Column("account_id", String(128), nullable=False),
    Column("snapshot_date", String(10), nullable=False),
    Column("list_price_usd", Float, nullable=False),
    Column("actual_usd", Float, nullable=False),
    Column("discount_pct", Float, nullable=False),
    Column("source", String(64), nullable=False, default=""),
    Column("captured_at", DateTime, nullable=False),
)

resource_inventory = Table(
    "resource_inventory", metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("provider", String(64), nullable=False),
    Column("account_id", String(128), nullable=False),
    Column("region", String(64), nullable=False, default=""),
    Column("resource_id", String(512), nullable=False),
    Column("resource_type", String(256), nullable=False),
    Column("resource_name", String(512), nullable=False, default=""),
    Column("tags", Text, nullable=False, default="{}"),
    Column("monthly_cost_usd", Float, nullable=False, default=0.0),
    Column("first_seen", String(10), nullable=False),
    Column("last_seen", String(10), nullable=False),
    Column("is_active", Boolean, nullable=False, default=True),
    Column("metadata", Text, nullable=False, default="{}"),
)

cost_trends = Table(
    "cost_trends", metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("provider", String(64), nullable=False),
    Column("service", String(256), nullable=False),
    Column("account_id", String(128), nullable=False),
    Column("computed_date", String(10), nullable=False),
    Column("avg_7d", Float, nullable=True),
    Column("avg_30d", Float, nullable=True),
    Column("avg_90d", Float, nullable=True),
    Column("pct_change_7d", Float, nullable=True),
    Column("pct_change_30d", Float, nullable=True),
    Column("trend_slope", Float, nullable=True),
    Column("seasonality_detected", Boolean, nullable=False, default=False),
    Column("updated_at", DateTime, nullable=False),
)

kubernetes_costs = Table(
    "kubernetes_costs", metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("cluster", String(256), nullable=False),
    Column("namespace", String(256), nullable=False),
    Column("workload_kind", String(64), nullable=False, default=""),
    Column("workload_name", String(256), nullable=False, default=""),
    Column("snapshot_date", String(10), nullable=False),
    Column("cpu_requested_cores", Float, nullable=False, default=0.0),
    Column("cpu_used_cores", Float, nullable=True),
    Column("mem_requested_gib", Float, nullable=False, default=0.0),
    Column("mem_used_gib", Float, nullable=True),
    Column("node_count", Integer, nullable=False, default=0),
    Column("pod_count", Integer, nullable=False, default=0),
    Column("monthly_cost_usd", Float, nullable=False, default=0.0),
    Column("cpu_efficiency_pct", Float, nullable=True),
    Column("mem_efficiency_pct", Float, nullable=True),
    Column("wasted_usd", Float, nullable=False, default=0.0),
    Column("labels", Text, nullable=False, default="{}"),
    Column("captured_at", DateTime, nullable=False),
)

# ── Budget tables ─────────────────────────────────────────────────────────────

budgets = Table(
    "budgets", metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("name", String(256), nullable=False),             # human-readable label
    Column("scope_type", String(32), nullable=False),        # "team" | "provider" | "service" | "total"
    Column("scope_value", String(256), nullable=False, default="*"),
    Column("period", String(16), nullable=False, default="monthly"), # monthly | weekly
    Column("limit_usd", Float, nullable=False),
    Column("alert_at_pct", Float, nullable=False, default=80.0),      # warning alert at 80%
    Column("critical_at_pct", Float, nullable=False, default=100.0), # critical alert at 100% (never blocks)
    Column("created_at", DateTime, nullable=False),
    Column("updated_at", DateTime, nullable=False),
    Column("created_by", String(256), nullable=False, default=""),
    Column("is_active", Boolean, nullable=False, default=True),
)

budget_alerts = Table(
    "budget_alerts", metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("budget_id", Integer, nullable=False),
    Column("alert_date", String(10), nullable=False),        # YYYY-MM-DD
    Column("period_start", String(10), nullable=False),
    Column("period_end", String(10), nullable=False),
    Column("spent_usd", Float, nullable=False),
    Column("limit_usd", Float, nullable=False),
    Column("pct_used", Float, nullable=False),
    Column("alert_type", String(16), nullable=False),        # "warning" | "exceeded"
    Column("notified", Boolean, nullable=False, default=False),
    Column("created_at", DateTime, nullable=False),
)

# ── Report subscription tables ────────────────────────────────────────────────

report_subscriptions = Table(
    "report_subscriptions", metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("name", String(256), nullable=False),
    # Delivery
    Column("slack_channels", Text, nullable=False, default="[]"),  # JSON list
    Column("email_addresses", Text, nullable=False, default="[]"), # JSON list
    Column("teams_webhook", String(512), nullable=False, default=""),
    # Schedule
    Column("cron", String(64), nullable=False),              # "0 9 * * 1" = Mon 9am
    Column("timezone", String(64), nullable=False, default="UTC"),
    # Content
    Column("sections", Text, nullable=False, default='["spend","anomalies"]'),  # JSON list
    # sections: spend | anomalies | scorecard | k8s | commitments | rightsizing | budgets | teams
    Column("filters", Text, nullable=False, default="{}"),   # JSON: {team, provider, env}
    Column("lookback_days", Integer, nullable=False, default=7),
    # Meta
    Column("created_at", DateTime, nullable=False),
    Column("last_sent_at", DateTime, nullable=True),
    Column("is_active", Boolean, nullable=False, default=True),
    Column("created_by", String(256), nullable=False, default=""),
)

# ── RBAC tables ───────────────────────────────────────────────────────────────

api_keys = Table(
    "api_keys", metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("key_hash", String(64), nullable=False),           # SHA-256 of raw key
    Column("name", String(256), nullable=False),              # human label ("Alice — analyst")
    Column("email", String(256), nullable=False, default=""),
    Column("role", String(32), nullable=False, default="viewer"),  # viewer|analyst|admin
    Column("scope_team", String(256), nullable=True),         # NULL = all teams
    Column("scope_provider", String(64), nullable=True),      # NULL = all providers
    Column("created_at", DateTime, nullable=False),
    Column("last_used_at", DateTime, nullable=True),
    Column("created_by", String(256), nullable=False, default=""),
    Column("is_active", Boolean, nullable=False, default=True),
)


# ── Org / multi-account tables ────────────────────────────────────────────────

org_accounts = Table(
    "org_accounts", metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("cloud_provider", String(32), nullable=False),    # aws | azure | gcp
    Column("account_id", String(128), nullable=False),
    Column("account_name", String(256), nullable=False, default=""),
    Column("parent_id", String(128), nullable=False, default=""),  # OU / folder / MG
    Column("status", String(32), nullable=False, default="ACTIVE"),
    Column("tags", Text, nullable=False, default="{}"),
    Column("assume_role_arn", String(512), nullable=False, default=""),  # for cross-account
    Column("last_synced", String(10), nullable=True),
    Column("is_management_account", Boolean, nullable=False, default=False),
)


terraform_tag_audits = Table(
    "terraform_tag_audits", metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("tf_dir", String(512), nullable=False),
    Column("audit_date", String(10), nullable=False),          # YYYY-MM-DD
    Column("resource_address", String(512), nullable=False),   # "aws_instance.web"
    Column("resource_type", String(256), nullable=False),
    Column("resource_name", String(256), nullable=False),
    Column("current_tags", Text, nullable=False, default="{}"),   # JSON dict
    Column("missing_tags", Text, nullable=False, default="[]"),   # JSON list[str]
    Column("status", String(16), nullable=False, default="open"), # open|fixed|ignored
    Column("pr_url", String(512), nullable=True),
    Column("file_path", String(512), nullable=False, default=""),
)

# ── ML / intelligence tables ──────────────────────────────────────────────────

forecast_models = Table(
    "forecast_models", metadata,
    Column("model_key", String(256), primary_key=True),   # "account:service"
    Column("params_json", Text, nullable=False),           # {"alpha": 0.3, "beta": 0.1, ...}
    Column("updated_at", DateTime, nullable=False),
)

pattern_findings = Table(
    "pattern_findings", metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("account_id", String(128), nullable=False),
    Column("pattern_id", String(64), nullable=False),
    Column("detected_at", DateTime, nullable=False),
    Column("severity", String(16), nullable=False),
    Column("monthly_waste_usd", Float, nullable=False, default=0.0),
    Column("status", String(16), nullable=False, default="open"),   # open|resolved|ignored
    Column("evidence_json", Text, nullable=False, default="[]"),
    Column("resources_json", Text, nullable=False, default="[]"),
    Column("dedup_key", String(64), nullable=False),   # SHA256 of account+pattern
)

# ── Learning lessons — the durable record of what nable learned about this org ─
#
# The learning signal (recommendations/learning/signal.py) recomputes verdicts
# live from the ledger, so without this table they change silently: a source
# gets suppressed one day and nobody can point at when or why. Each row records
# one learned adjustment as a first-class object: what changed, the evidence at
# the moment it changed, and a status the customer controls. rolled_back is a
# standing override (the signal pins that key to neutral until restored), which
# is what makes the learning trustworthy: every lesson is visible, evidenced,
# and reversible. Single-tenant like everything else in this DB.

learning_lessons = Table(
    "learning_lessons", metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    # "source:idle" | "bucket:idle|prod|steady" | "approval_floor"
    Column("key", String(256), nullable=False),
    Column("kind", String(24), nullable=False),          # verdict | approval_floor
    Column("from_verdict", String(16), nullable=True),   # what it was before (None = neutral)
    Column("to_verdict", String(16), nullable=False),    # boost | suppress | floor
    Column("lesson", Text, nullable=False, default=""),  # the human sentence (signal's why)
    Column("evidence", Text, nullable=False, default="{}"),  # JSON snapshot: acted/resolved/accuracy/...
    Column("first_seen", DateTime, nullable=False),
    Column("last_confirmed", DateTime, nullable=False),
    Column("status", String(16), nullable=False, default="active"),  # active | superseded | rolled_back
    Column("superseded_at", DateTime, nullable=True),
    Column("rolled_back_at", DateTime, nullable=True),
    Column("rollback_note", Text, nullable=False, default=""),
)

# ── Alert policies — per-service anomaly thresholds and mute rules ────────────

alert_policies = Table(
    "alert_policies", metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("provider", String(64), nullable=False, default="*"),        # "aws" | "azure" | "*"
    Column("service_pattern", String(256), nullable=False, default="*"), # exact or "*" wildcard
    Column("muted", Boolean, nullable=False, default=False),            # silence all alerts for this service
    Column("min_pct_change", Float, nullable=True),                     # override global 20% threshold
    Column("min_usd_change", Float, nullable=True),                     # ignore if delta < $X
    Column("min_z_score", Float, nullable=True),                        # override global z=2.0
    Column("note", Text, nullable=True),                                # why this policy exists
    Column("created_at", DateTime, nullable=False),
    Column("created_by", String(256), nullable=False, default=""),
)

# ── Savings tracking — lifecycle from recommendation → verified savings ───────

savings_recommendations = Table(
    "savings_recommendations", metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    # Source
    Column("source", String(32), nullable=False),           # rightsizing|idle|commitment|kubernetes|waste
    Column("provider", String(64), nullable=False),
    Column("account_id", String(128), nullable=False, default=""),
    Column("region", String(64), nullable=False, default=""),
    # Resource identity
    Column("resource_id", String(512), nullable=False, default=""),
    Column("resource_type", String(256), nullable=False, default=""),  # e.g. "ec2", "rds", "k8s_workload"
    Column("resource_name", String(512), nullable=False, default=""),
    # What to change
    Column("current_config", Text, nullable=False, default="{}"),      # JSON: current state
    Column("recommended_config", Text, nullable=False, default="{}"),  # JSON: what to change to
    Column("description", Text, nullable=False, default=""),           # human-readable summary
    # Economics
    Column("estimated_monthly_savings_usd", Float, nullable=False, default=0.0),
    Column("verified_monthly_savings_usd", Float, nullable=True),      # actual measured after change
    # How the verified figure was obtained: bill_measured (CUR before/after),
    # effective_rate (type delta at the customer's measured discount), or
    # list_price (public on-demand delta). The ledger says "measured off your
    # bill" only when the basis actually is.
    Column("verified_basis", String(24), nullable=True),
    # Lifecycle
    Column("status", String(16), nullable=False, default="open"),  # open|acted_on|verified|dismissed|expired
    Column("generated_at", DateTime, nullable=False),
    Column("acted_on_at", DateTime, nullable=True),
    Column("verified_at", DateTime, nullable=True),
    Column("dismissed_at", DateTime, nullable=True),
    Column("dismiss_reason", Text, nullable=True),
    # Canonical dismiss category (classify_dismiss_reason), set at dismiss time so the
    # learning signal can tell a quality miss ("estimate is wrong") apart from a
    # business reason ("reserved for peak") without re-parsing free text on every query.
    Column("dismiss_reason_category", String(32), nullable=True),
    # Drift: the fix was applied, then the resource came back. Nothing stays
    # optimized, and a scanner that only reports current state can never say
    # "you already fixed this once". regression_count is how many times.
    Column("regressed_at", DateTime, nullable=True),
    Column("regression_count", Integer, nullable=False, default=0),
    # Dedup
    Column("dedup_key", String(64), nullable=False),   # SHA256 of source+resource_id+recommended_config
    # Learning loop: coarse env/workload bucket so the signal can roll up per
    # (source, bucket), e.g. spot is fine for nonprod-batch but not prod-steady.
    Column("environment_bucket", String(64), nullable=True),
)

# ── Learned cost context — the operating model nable remembers ────────────────
# When a human answers "why this is fine" once ("that idle box is our DR standby"),
# we store the answer as an annotation so nable never re-flags the same thing and
# can generalize it. Each row is one learned exception. scope says how broadly it
# applies: 'resource' (this exact resource_id), 'source' (this finding type, e.g.
# spot recs), 'bucket' (an environment_bucket like dr/nonprod), 'resource_type'
# (all NAT gateways), or 'provider'. Optional provider/account_id narrow a broad
# scope to one org boundary. verdict is 'intentional' today (suppress the finding);
# the column leaves room for future verdicts. Nothing here calls a cloud; it only
# shapes which proposals surface. Soft-deleted via active=False so forget() keeps
# the audit trail of what was learned and unlearned.
context_annotations = Table(
    "context_annotations", metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("scope", String(32), nullable=False),          # resource|source|bucket|resource_type|provider
    Column("match_value", String(512), nullable=False),   # the value that scope matches on
    Column("provider", String(64), nullable=True),        # optional extra narrowing
    Column("account_id", String(128), nullable=True),     # optional extra narrowing
    Column("verdict", String(32), nullable=False, default="intentional"),
    Column("reason", Text, nullable=False, default=""),   # the human's "why it's fine"
    Column("created_by", String(128), nullable=False, default=""),
    Column("source_rec_id", Integer, nullable=True),      # the finding that prompted it, for provenance
    Column("created_at", DateTime, nullable=False),
    Column("active", Boolean, nullable=False, default=True),
    Index("ix_context_active_scope", "active", "scope"),
)

# ── Moldable dashboard — pinned views (agent-built cost cards) ────────────────
# A pinned view stores the SliceSpec that regenerates its data, not the data
# itself, so each card re-runs live on load. owner is a local identity or
# "instance" for shared team pins (single-tenant / local-first; never a
# multi-tenant tenant id).
# ── Dashboards — the container a saved card belongs to ───────────────────────
#
# Cards used to pin into one flat list per owner, so an install had exactly one
# wall of charts: no way to keep "Platform team weekly" apart from "CFO monthly
# review", no way to name or filter a set, and nothing to link to. A dashboard
# is that container: a named, addressable object with its own scope.
#
# `filters` and `date_range` are the dashboard's scope, applied on top of every
# card it holds (SliceSpec.with_scope). Storing scope here rather than editing
# each card means one team filter narrows twenty charts at once, and removing it
# restores exactly what the cards said before.

dashboards = Table(
    "dashboards", metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("slug", String(96), nullable=False, unique=True),   # URL identity, stable across renames
    Column("title", String(256), nullable=False, default="Untitled dashboard"),
    Column("description", Text, nullable=False, default=""),
    Column("owner", String(128), nullable=False, default="instance"),
    Column("scope", String(16), nullable=False, default="instance"),  # me | instance
    Column("filters", Text, nullable=False, default="[]"),      # JSON [FilterClause]
    Column("date_range", Text, nullable=False, default="{}"),   # JSON DateRange
    Column("position", Integer, nullable=False, default=0),
    Column("created_at", DateTime, nullable=False),
    Column("updated_at", DateTime, nullable=False),
    Column("created_by", String(128), nullable=False, default=""),
    Index("ix_dashboards_owner", "owner"),
)

dashboard_views = Table(
    "dashboard_views", metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    # NULL keeps every card pinned before dashboards existed working exactly as
    # it did: it belongs to no dashboard and still renders on the overview.
    Column("dashboard_id", Integer, nullable=True),
    Column("owner", String(128), nullable=False, default="instance"),
    Column("scope", String(16), nullable=False, default="instance"),   # me | instance
    Column("title", String(256), nullable=False, default=""),
    Column("template", String(32), nullable=False, default="bar"),     # line|bar|stacked_bar|table|kpi|heatmap
    Column("slice_spec", Text, nullable=False, default="{}"),          # JSON SliceSpec that regenerates the data
    Column("card_spec", Text, nullable=False, default="{}"),           # JSON CardSpec (title/dims/days/refresh)
    Column("position", Integer, nullable=False, default=0),
    Column("refresh_secs", Integer, nullable=False, default=43200),
    Column("created_at", DateTime, nullable=False),
    Column("updated_at", DateTime, nullable=False),
    Column("created_by", String(128), nullable=False, default=""),
)

# ── Slack bot — thread conversation memory + remediation approvals ───────────

slack_threads = Table(
    "slack_threads", metadata,
    Column("thread_key", String(128), primary_key=True),    # "channel:thread_ts" (or channel for DMs)
    Column("channel", String(64), nullable=False, default=""),
    Column("messages", Text, nullable=False, default="[]"), # JSON list of {role, content} text turns
    Column("updated_at", DateTime, nullable=False),
)

pending_actions = Table(
    "pending_actions", metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("kind", String(32), nullable=False),              # rightsizing_pr | ticket
    Column("payload", Text, nullable=False, default="{}"),   # JSON args used at execution time
    Column("preview", Text, nullable=False, default=""),     # human-readable summary shown in the card
    Column("status", String(16), nullable=False, default="pending"),  # pending|approved|cancelled|expired|failed
    Column("requested_by", String(128), nullable=False, default=""),  # slack user id
    Column("resolved_by", String(128), nullable=True),
    Column("created_at", DateTime, nullable=False),
    Column("resolved_at", DateTime, nullable=True),
    Column("result", Text, nullable=True),                   # JSON outcome (pr_url, ticket_url, error)
)

# ── Indexes — keep hot query paths O(log n) instead of O(n) ──────────────────
# cost_snapshots: every budget check and spend query filters by date + provider/service
Index("ix_cs_date_provider",  cost_snapshots.c.snapshot_date, cost_snapshots.c.provider)
Index("ix_cs_date_service",   cost_snapshots.c.snapshot_date, cost_snapshots.c.service)
Index("ix_cs_provider",       cost_snapshots.c.provider)

# attributed_costs: team budget checks and team cost queries
Index("ix_ac_date_team",      attributed_costs.c.snapshot_date, attributed_costs.c.team)
Index("ix_ac_team",           attributed_costs.c.team)

# anomalies: report sections filter by date and ack status
Index("ix_anom_date",         anomalies.c.snapshot_date)
Index("ix_anom_ack",          anomalies.c.acknowledged)

# org_accounts: sync looks up by (account_id, provider) — must be unique
Index("ix_org_acct_provider", org_accounts.c.account_id, org_accounts.c.cloud_provider,
      unique=True)

# budgets: list_budgets filters by is_active; sync_from_yaml looks up by name
Index("ix_budgets_active",    budgets.c.is_active)
Index("ix_budgets_name",      budgets.c.name, unique=True)

# api_keys: auth middleware looks up by key_hash; admin lists active keys
Index("ix_keys_hash",         api_keys.c.key_hash, unique=True)
Index("ix_keys_active",       api_keys.c.is_active)

# report_subscriptions: scheduler filters by is_active
Index("ix_rsub_active",       report_subscriptions.c.is_active)

# cost_trends: trend queries filter by provider + service
Index("ix_trends_prov_svc",   cost_trends.c.provider, cost_trends.c.service)

# terraform_tag_audits: queries filter by tf_dir + date and by status
Index("ix_tfa_dir_date", terraform_tag_audits.c.tf_dir, terraform_tag_audits.c.audit_date)
Index("ix_tfa_status",   terraform_tag_audits.c.status)

# pattern_findings: queries filter by account and status
Index("ix_pf_account",  pattern_findings.c.account_id)
Index("ix_pf_status",   pattern_findings.c.status)
Index("ix_pf_dedup",    pattern_findings.c.dedup_key, unique=True)

# alert_policies: fast lookup by provider + service
Index("ix_ap_provider_svc", alert_policies.c.provider, alert_policies.c.service_pattern)

# slack bot: TTL prune scans by updated_at; approval lookups filter by status
Index("ix_slack_threads_updated", slack_threads.c.updated_at)
Index("ix_pending_status",        pending_actions.c.status)

# savings_recommendations: status checks and dedup
Index("ix_srec_status",   savings_recommendations.c.status)
Index("ix_srec_source",   savings_recommendations.c.source)
Index("ix_srec_provider", savings_recommendations.c.provider)
Index("ix_srec_dedup",    savings_recommendations.c.dedup_key, unique=True)

# ── Engine factory ────────────────────────────────────────────────────────────

def _profile_name() -> str:
    """Return the active profile name, or empty string for the default profile."""
    return os.environ.get("FINOPS_PROFILE", "").strip()


def _profile_data_dir(profile: str) -> Path:
    """Return the data directory for a named profile, creating it if needed."""
    d = Path.home() / ".finops" / "profiles" / profile
    d.mkdir(parents=True, exist_ok=True)
    d.chmod(stat.S_IRWXU)
    return d


def data_dir() -> Path:
    global _DATA_DIR
    if _DATA_DIR is None:
        profile = _profile_name()
        if profile:
            _DATA_DIR = _profile_data_dir(profile)
        else:
            raw = os.environ.get("FINOPS_DATA_DIR", "")
            _DATA_DIR = Path(raw).expanduser() if raw else Path.home() / ".finops"
            _DATA_DIR.mkdir(parents=True, exist_ok=True)
            _DATA_DIR.chmod(stat.S_IRWXU)
    return _DATA_DIR


def _is_postgres(url: str) -> bool:
    return url.startswith(("postgresql://", "postgres://", "postgresql+", "postgres+"))


def get_engine() -> Engine:
    """
    Return the database engine.

    Priority:
      1. DATABASE_URL env var → connect to Postgres (shared team mode)
      2. FINOPS_DB_PATH env var → SQLite at custom path
      3. Default → SQLite at ~/.finops/finops.db

    Postgres shared mode:
      Set DATABASE_URL=postgresql://user:pass@host:5432/finops
      All engineers sharing this URL use the same database — no sync needed.
      Credentials never leave the machine; only the DB connection is shared.
    """
    global _ENGINE
    if _ENGINE is not None:
        return _ENGINE

    database_url = os.environ.get("DATABASE_URL", "")

    if database_url and _is_postgres(database_url):
        # Shared Postgres mode
        # psycopg2 or asyncpg must be installed: pip install finops-mcp[postgres]
        _ENGINE = create_engine(
            database_url,
            pool_pre_ping=True,          # detect stale connections
            pool_size=5,
            max_overflow=10,
            connect_args={"connect_timeout": 10},
        )
        metadata.create_all(_ENGINE)
        _run_sqlite_migrations(_ENGINE)
    else:
        # Local SQLite mode (default)
        # Priority: FINOPS_DB_PATH > FINOPS_PROFILE > default ~/.finops/finops.db
        db_path_env = os.environ.get("FINOPS_DB_PATH", "")
        if db_path_env:
            db_path = Path(db_path_env).expanduser()
        else:
            db_path = data_dir() / "finops.db"
        _ENGINE = create_engine(
            f"sqlite:///{db_path}",
            connect_args={
                "check_same_thread": False,
                # SQLAlchemy's SQLite dialect maps this to PRAGMA busy_timeout at the
                # Python sqlite3 level — prevents immediate failure under concurrent writes.
                "timeout": 15,
            },
        )

        @event.listens_for(_ENGINE, "connect")
        def _set_sqlite_pragmas(dbapi_conn, _connection_record):
            """Apply WAL mode and busy_timeout on every new connection."""
            cursor = dbapi_conn.cursor()
            cursor.execute("PRAGMA journal_mode=WAL")
            cursor.execute("PRAGMA busy_timeout=15000")
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.close()

        metadata.create_all(_ENGINE)
        _run_sqlite_migrations(_ENGINE)
        db_path.chmod(stat.S_IRUSR | stat.S_IWUSR)
        # WAL mode creates -wal/-shm sidecars under the process umask; they
        # briefly hold recently written rows, so clamp them too when present.
        for _suffix in ("-wal", "-shm"):
            _side = db_path.with_name(db_path.name + _suffix)
            if _side.exists():
                _side.chmod(stat.S_IRUSR | stat.S_IWUSR)

    return _ENGINE


def _add_column_ddl(engine: Engine, table: str, column: str) -> str:
    """ADD COLUMN statement for `column`, rendered for THIS engine's dialect.

    Derived from the model rather than hand-written, for two reasons. The type
    names differ per backend (a DATETIME column is TIMESTAMP on PostgreSQL, and a
    REAL is DOUBLE PRECISION), so a hardcoded string that works on SQLite is a
    syntax error on Postgres. And a hand-written DDL can drift from the Table
    definition it is supposed to mirror; deriving it means they cannot disagree.
    """
    col = metadata.tables[table].c[column]
    ddl = f"ALTER TABLE {table} ADD COLUMN {column} {col.type.compile(engine.dialect)}"
    if not col.nullable:
        # A NOT NULL column added to a table with existing rows needs a default,
        # or the ALTER is rejected outright.
        default = getattr(col.default, "arg", None)
        if default is None:
            return ddl + " NOT NULL"
        rendered = f"'{default}'" if isinstance(default, str) else str(default)
        return f"{ddl} NOT NULL DEFAULT {rendered}"
    return ddl


def _run_sqlite_migrations(engine: Engine) -> None:
    """Apply additive schema migrations. Runs for SQLite AND PostgreSQL.

    Only ADD COLUMN is portable (SQLite supports neither DROP COLUMN before 3.35
    nor MODIFY), so every entry here is additive and idempotent: an existing
    column is detected and skipped.

    Detection goes through SQLAlchemy's inspector, not `PRAGMA table_info`. PRAGMA
    is SQLite-only, so on a shared-team Postgres deployment it raised, the error
    was swallowed as a warning, and the ALTER never ran. `metadata.create_all`
    does not repair that: it creates missing TABLES, never missing columns on a
    table that already exists. The result was that every column added here after
    a Postgres database was first created stayed missing, and any query naming it
    (`select(savings_recommendations)`) failed with an undefined-column error.
    """
    migrations: list[tuple[str, str]] = [
        # (table, column) — the DDL is derived from the model by _add_column_ddl.
        ("anomalies", "metadata"),
        ("budgets", "critical_at_pct"),
        ("budgets", "alert_at_pct"),
        # Runway inputs (Phase 1 business-context layer)
        ("business_metrics", "cash_on_hand_usd"),
        ("business_metrics", "last_raise_amount_usd"),
        ("business_metrics", "last_raise_date"),
        ("business_metrics", "monthly_opex_usd"),
        # Learning loop: per-(source, bucket) signal
        ("savings_recommendations", "environment_bucket"),
        # Learning loop: canonical dismiss category, so business-reason dismissals don't
        # count against a source's act-rate the way a quality miss does.
        ("savings_recommendations", "dismiss_reason_category"),
        # Verified-savings loop: how the verified dollar figure was obtained
        # (bill_measured | effective_rate | list_price).
        ("savings_recommendations", "verified_basis"),
        # Drift loop: a fix that was applied and then came undone. Nothing stays
        # optimized, and a regression is the one signal a point-in-time scanner
        # cannot produce, so it is worth its own columns rather than being
        # inferred from a re-opened row.
        ("savings_recommendations", "regressed_at"),
        ("savings_recommendations", "regression_count"),
        # Dashboards: a saved card now belongs to one. Nullable on purpose, so
        # cards pinned before dashboards existed keep working and stay on the
        # overview instead of vanishing into a dashboard nobody created.
        ("dashboard_views", "dashboard_id"),
    ]

    inspector = inspect(engine)
    with engine.connect() as conn:
        for table, column in migrations:
            try:
                existing = {c["name"] for c in inspector.get_columns(table)}
                if column not in existing:
                    conn.execute(text(_add_column_ddl(engine, table, column)))
                    conn.commit()
                    log.info("Migration applied: %s.%s", table, column)
            except Exception as exc:
                log.warning("Migration skipped (%s.%s): %s", table, column, exc)

        # Legacy cleanup, not additive: old schemas defined budgets.block_at_pct as
        # NOT NULL with no default. The current model dropped that column (it folded
        # into critical_at_pct), so every INSERT through the model omits it and dies
        # on the NOT NULL constraint, crashing `nable`'s first-run budget step for
        # anyone who upgraded from an old DB. Drop the orphan if present. SQLite
        # >= 3.35 (bundled with Python >= 3.11, which is nable's floor) supports
        # DROP COLUMN; on anything older this warns and the onboarding budget step
        # degrades to a clean one-line message instead of a traceback.
        for _tbl, _col in (("budgets", "block_at_pct"),):
            try:
                # Inspector, not PRAGMA: same reason as above. This orphan-column
                # drop was silently skipped on PostgreSQL too, so a team that
                # upgraded a shared database still had the NOT NULL orphan and
                # still crashed on the first-run budget step.
                if _col in {c["name"] for c in inspect(engine).get_columns(_tbl)}:
                    conn.execute(text(f"ALTER TABLE {_tbl} DROP COLUMN {_col}"))
                    conn.commit()
                    log.info("Migration: dropped legacy %s.%s", _tbl, _col)
            except Exception as exc:
                log.warning("legacy %s.%s drop skipped: %s", _tbl, _col, exc)


def archive_old_snapshots(days_to_keep: int = 365) -> int:
    """Move cost_snapshots older than days_to_keep to cost_snapshots_archive.

    Returns the number of rows archived.
    """
    engine = get_engine()
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days_to_keep)).strftime("%Y-%m-%d")

    with engine.begin() as conn:
        # Select rows to archive
        old_rows = conn.execute(
            select(
                cost_snapshots.c.provider,
                cost_snapshots.c.service,
                cost_snapshots.c.account_id,
                cost_snapshots.c.region,
                cost_snapshots.c.snapshot_date,
                cost_snapshots.c.amount_usd,
                cost_snapshots.c.granularity,
                cost_snapshots.c.captured_at,
            ).where(cost_snapshots.c.snapshot_date < cutoff)
        ).fetchall()

        if not old_rows:
            return 0

        # Insert into archive
        conn.execute(
            cost_snapshots_archive.insert(),
            [
                {
                    "provider": r.provider,
                    "service": r.service,
                    "account_id": r.account_id,
                    "region": r.region,
                    "snapshot_date": r.snapshot_date,
                    "amount_usd": r.amount_usd,
                    "granularity": r.granularity,
                    "captured_at": r.captured_at,
                }
                for r in old_rows
            ],
        )

        # Delete from source
        conn.execute(
            delete(cost_snapshots).where(cost_snapshots.c.snapshot_date < cutoff)
        )

    count = len(old_rows)
    log.info("Archived %d cost snapshots older than %s", count, cutoff)
    return count


def storage_mode() -> dict:
    """Return info about the current storage backend."""
    database_url = os.environ.get("DATABASE_URL", "")
    if database_url and _is_postgres(database_url):
        # Mask credentials for display
        import re
        masked = re.sub(r"://([^:]+):([^@]+)@", r"://\1:***@", database_url)
        return {"mode": "postgres", "url": masked, "shared": True}
    db_path_env = os.environ.get("FINOPS_DB_PATH", "")
    db_path = Path(db_path_env).expanduser() if db_path_env else data_dir() / "finops.db"
    return {"mode": "sqlite", "path": str(db_path), "shared": False}
