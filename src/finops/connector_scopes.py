"""The tightest credential each billing source can be given, and what nable calls with it.

Asked on r/selfhosted, 2026-08-08: can the scope be reduced down to billing? For
some providers, yes, exactly. For others the honest answer is no, and the useful
thing is to say which is which rather than claim least privilege everywhere and
be wrong in the one place it matters.

So every entry carries a grade:

  scoped   The provider has a billing-specific permission and nable uses exactly
           it. A leaked credential reads billing and nothing else.
  role     No per-key scope, but you can put the key behind a limited role or
           user. Tight in the end, though it is your role assignment doing the
           work, not the key itself.
  account  The provider offers nothing narrower. A leaked credential is as
           powerful as the account it was minted from. Stated plainly, because
           an operator deciding whether to install this deserves to know.

`gap` is where we know a narrower path exists that nable does not use yet. It is
a work list kept in the open, not a footnote: a manifest that only records good
news is marketing.

This is the single source for `nable connect --scopes`, the setup hints, and the
trust page. A test asserts every registered billing source has an entry, so a
new connector cannot ship with its blast radius undocumented.
"""
from __future__ import annotations

from dataclasses import dataclass, field

SCOPED = "scoped"
ROLE = "role"
ACCOUNT = "account"

GRADE_ORDER = (ACCOUNT, ROLE, SCOPED)
GRADE_LABEL = {
    SCOPED: "Billing-only scope. A leaked key reads billing and nothing else.",
    ROLE: "No per-key scope. Put the key behind a limited role or user.",
    ACCOUNT: "No narrower option exists. The key is as powerful as the account.",
}


@dataclass(frozen=True)
class Scope:
    provider: str                          # display name
    credential: str                        # what you create
    permission: str                        # the exact scope, role or grant to pick
    grade: str                             # SCOPED | ROLE | ACCOUNT
    calls: tuple[str, ...]                 # every endpoint nable hits with it
    mint_url: str | None = None
    note: str = ""                         # what the scope does and does not allow
    gap: str = ""                          # narrower path that exists but is unused
    env: tuple[str, ...] = field(default_factory=tuple)


# slug -> Scope. Slugs match PROVIDER_ENV in setup_scan.py, plus the three clouds.
CONNECTOR_SCOPES: dict[str, Scope] = {

    # ── Clouds ───────────────────────────────────────────────────────────────
    "aws": Scope(
        provider="AWS",
        credential="IAM role or user",
        permission="The policy printed by `nable scan --dry-run --json`",
        grade=SCOPED,
        calls=("Describe/List/Get only, per scan_manifest.py",
               "ce:GetCostAndUsage, only on `--spend`, billed at $0.01/request"),
        mint_url="https://console.aws.amazon.com/iam/",
        note="Every action in the policy is a read. Cost Explorer is not in the "
             "default set, so the default scan cannot spend your money.",
        env=("AWS_ACCESS_KEY_ID", "AWS_PROFILE"),
    ),
    "azure": Scope(
        provider="Azure",
        credential="Service principal",
        permission="Cost Management Reader",
        grade=ROLE,
        calls=("Cost Management query API", "Consumption usage details"),
        mint_url="https://portal.azure.com/",
        note="A built-in read-only role scoped to cost. Assign it at the billing "
             "or subscription scope, not at tenant root.",
        env=("AZURE_CLIENT_ID", "AZURE_TENANT_ID"),
    ),
    "gcp": Scope(
        provider="Google Cloud",
        credential="Service account, or your own ADC",
        permission="roles/billing.viewer, plus BigQuery Data Viewer on the billing export dataset",
        grade=ROLE,
        calls=("Cloud Billing API", "BigQuery read of the billing export table"),
        mint_url="https://console.cloud.google.com/iam-admin/serviceaccounts",
        note="Both roles are read-only. The BigQuery grant can be narrowed to the "
             "single export dataset rather than the project.",
        env=("GOOGLE_APPLICATION_CREDENTIALS",),
    ),

    # ── Billing-only scope available and used ────────────────────────────────
    "cloudflare": Scope(
        provider="Cloudflare",
        credential="API token",
        permission="Account > Billing > Read",
        grade=SCOPED,
        calls=("GET /accounts/{id}/billable-usage",
               "GET /accounts/{id}/billing-history (fallback)",
               "GET /accounts/{id}/subscriptions (fallback)"),
        mint_url="https://dash.cloudflare.com/profile/api-tokens",
        note="One permission. The token cannot read DNS, edit zones, or touch "
             "Workers.",
        env=("CLOUDFLARE_API_TOKEN", "CLOUDFLARE_ACCOUNT_ID"),
    ),
    "datadog": Scope(
        provider="Datadog",
        credential="API key plus an application key scoped to those two",
        permission="usage_read and billing_read",
        grade=SCOPED,
        calls=("GET /api/v2/usage/estimated_cost",
               "GET /api/v1/usage/estimated_cost (fallback)",
               "GET /api/v1/orgs (name only, optional)"),
        mint_url="https://app.datadoghq.com/organization-settings/application-keys",
        note="An unscoped application key inherits everything you can do in "
             "Datadog. Scope it to those two and it reads cost only. The org "
             "lookup is not covered by them and degrades to a default label, "
             "which costs you nothing but a display name.",
        env=("DATADOG_API_KEY", "DATADOG_APP_KEY"),
    ),
    "mongodb": Scope(
        provider="MongoDB Atlas",
        credential="Organization API key",
        permission="Organization Billing Viewer",
        grade=SCOPED,
        calls=("GET /orgs/{id}", "GET /orgs/{id}/invoices"),
        mint_url="https://cloud.mongodb.com",
        note="Billing-only by construction. The key cannot read a cluster, a "
             "database, or a single document.",
        env=("MONGODB_ATLAS_PUBLIC_KEY", "MONGODB_ATLAS_PRIVATE_KEY"),
    ),
    "twilio": Scope(
        provider="Twilio",
        credential="Restricted API key",
        permission="Usage, read",
        grade=SCOPED,
        calls=("GET /Accounts/{sid}/Usage/Records.json",
               "GET /Accounts/{sid}.json (name only, optional)"),
        mint_url="https://console.twilio.com/us1/account/keys-credentials/api-keys",
        note="Your Auth Token can send messages, buy numbers and spend money, so "
             "nable takes a Restricted API key instead when you set "
             "TWILIO_API_KEY and TWILIO_API_SECRET. The account lookup is not "
             "covered by a Usage-only key and degrades to a default label.",
        env=("TWILIO_ACCOUNT_SID", "TWILIO_API_KEY", "TWILIO_API_SECRET"),
    ),

    # ── Scope it with a role, not with the key ───────────────────────────────
    "snowflake": Scope(
        provider="Snowflake",
        credential="User plus a dedicated role",
        permission="IMPORTED PRIVILEGES ON DATABASE SNOWFLAKE, plus USAGE on one warehouse",
        grade=ROLE,
        calls=("SELECT on SNOWFLAKE.ACCOUNT_USAGE.WAREHOUSE_METERING_HISTORY",
               "SELECT on SNOWFLAKE.ACCOUNT_USAGE.STORAGE_USAGE"),
        mint_url=None,
        note="Set SNOWFLAKE_ROLE to that role and the login reaches the two "
             "metering views and nothing in your own databases.",
        env=("SNOWFLAKE_ACCOUNT", "SNOWFLAKE_USER", "SNOWFLAKE_ROLE"),
    ),
    "newrelic": Scope(
        provider="New Relic",
        credential="User key",
        permission="The key inherits its user's role: use a read-only user",
        grade=ROLE,
        calls=("POST /graphql, NRQL reads against your account",),
        mint_url="https://one.newrelic.com/api-keys",
        note="New Relic has no billing-only key scope. Mint the key as a user "
             "whose role is read-only, and the key is read-only.",
        env=("NEW_RELIC_API_KEY", "NEW_RELIC_ACCOUNT_ID"),
    ),
    "langfuse": Scope(
        provider="Langfuse",
        credential="Project key pair",
        permission="Scoped to the one project it was created in",
        grade=ROLE,
        calls=("GET /api/public/metrics/daily",),
        mint_url="https://cloud.langfuse.com",
        note="Project-scoped by construction, so the blast radius is one "
             "project's observability data.",
        env=("LANGFUSE_PUBLIC_KEY", "LANGFUSE_SECRET_KEY"),
    ),

    # ── No narrower option exists ────────────────────────────────────────────
    "databricks": Scope(
        provider="Databricks",
        credential="Account-level token or service principal",
        permission="Account admin",
        grade=ACCOUNT,
        calls=("GET /api/2.0/accounts/{id}/usage/download",),
        mint_url="https://docs.databricks.com/en/dev-tools/auth/pat.html",
        note="The billable-usage download API is account-admin only. There is no "
             "narrower role that can call it.",
        gap="system.billing.usage can be read with three grants (USE CATALOG "
            "system, USE SCHEMA system.billing, SELECT on the table) instead of "
            "account admin. nable does not query it over SQL yet.",
        env=("DATABRICKS_HOST", "DATABRICKS_TOKEN", "DATABRICKS_ACCOUNT_ID"),
    ),
    "vercel": Scope(
        provider="Vercel",
        credential="Access token",
        permission="Team scope and an expiry date; no per-permission scopes",
        grade=ACCOUNT,
        calls=("GET /v1/billing/invoices",),
        mint_url="https://vercel.com/account/tokens",
        note="A Vercel token carries whatever its owner can do. Narrow it by "
             "scoping to one team and setting the shortest expiry you can live "
             "with.",
        env=("VERCEL_TOKEN", "VERCEL_TEAM_ID"),
    ),
    "openai": Scope(
        provider="OpenAI",
        credential="Admin key (sk-admin-…)",
        permission="Organization admin; the Costs API accepts nothing less",
        grade=ACCOUNT,
        calls=("GET /v1/organization/costs", "GET /v1/organization/usage/*"),
        mint_url="https://platform.openai.com/settings/organization/admin-keys",
        note="An admin key can also manage API keys and members. That is OpenAI's "
             "design, not a choice nable makes, and it is the single broadest "
             "credential nable accepts.",
        env=("OPENAI_ADMIN_KEY", "OPENAI_API_KEY"),
    ),
    "anthropic": Scope(
        provider="Anthropic",
        credential="Admin key plus organization ID",
        permission="Organization admin; the Cost API accepts nothing less",
        grade=ACCOUNT,
        calls=("GET /v1/organizations/cost_report",
               "GET /v1/organizations/usage_report/messages"),
        mint_url="https://console.anthropic.com/settings/admin-keys",
        note="Same shape as OpenAI: the cost endpoint is admin-gated, and an "
             "admin key can also manage workspaces and members.",
        env=("ANTHROPIC_ADMIN_KEY", "ANTHROPIC_ORGANIZATION_ID"),
    ),
    "openrouter": Scope(
        provider="OpenRouter",
        credential="Provisioning key, or a standard API key",
        permission="No billing-only scope",
        grade=ACCOUNT,
        calls=("GET /api/v1/activity", "GET /api/v1/credits"),
        mint_url="https://openrouter.ai/settings/keys",
        note="Per-model usage needs the provisioning key. A standard key still "
             "gets you the credits balance, which is the smaller ask if the "
             "provisioning key is more power than you want to hand over.",
        env=("OPENROUTER_API_KEY", "OPENROUTER_PROVISIONING_KEY"),
    ),
    "litellm": Scope(
        provider="LiteLLM proxy",
        credential="Master key",
        permission="No billing-only scope",
        grade=ACCOUNT,
        calls=("GET /spend/logs",),
        mint_url=None,
        note="Self-hosted, so the key never leaves your network. The proxy's "
             "master key is admin over the proxy itself.",
        env=("LITELLM_PROXY_URL", "LITELLM_MASTER_KEY"),
    ),
    "modal": Scope(
        provider="Modal",
        credential="API token",
        permission="No billing-only scope",
        grade=ACCOUNT,
        calls=("Workspace and app listing",),
        mint_url="https://modal.com/settings/tokens",
        env=("MODAL_TOKEN_ID", "MODAL_TOKEN_SECRET"),
    ),
    "together": Scope(
        provider="Together AI",
        credential="API key",
        permission="No billing-only scope",
        grade=ACCOUNT,
        calls=("Account and usage endpoints",),
        mint_url="https://api.together.ai/settings/api-keys",
        env=("TOGETHER_API_KEY",),
    ),
    "replicate": Scope(
        provider="Replicate",
        credential="API token",
        permission="No billing-only scope",
        grade=ACCOUNT,
        calls=("Account and prediction endpoints",),
        mint_url="https://replicate.com/account/api-tokens",
        env=("REPLICATE_API_TOKEN",),
    ),
    "cohere": Scope(
        provider="Cohere",
        credential="API key",
        permission="No billing-only scope",
        grade=ACCOUNT,
        calls=("Usage endpoints",),
        mint_url="https://dashboard.cohere.com/api-keys",
        env=("COHERE_API_KEY",),
    ),
    "mistral": Scope(
        provider="Mistral AI",
        credential="API key",
        permission="No billing-only scope",
        grade=ACCOUNT,
        calls=("Usage endpoints",),
        mint_url="https://console.mistral.ai/api-keys",
        env=("MISTRAL_API_KEY",),
    ),
}


def by_grade() -> dict[str, list[Scope]]:
    """Scopes bucketed by grade, worst first. Bad news should not be last."""
    out: dict[str, list[Scope]] = {g: [] for g in GRADE_ORDER}
    for scope in CONNECTOR_SCOPES.values():
        out.setdefault(scope.grade, []).append(scope)
    for scopes in out.values():
        scopes.sort(key=lambda s: s.provider.lower())
    return out


def gaps() -> list[Scope]:
    """Sources where a narrower credential exists that nable does not use yet."""
    return [s for s in CONNECTOR_SCOPES.values() if s.gap]


def render() -> str:
    """The human-readable table printed by `nable connect --scopes`."""
    out = [
        "Least privilege, per billing source",
        "",
        "nable only ever reads. But a credential you hand it can be more powerful",
        "than the reading it does, and that is the part worth checking. Below is",
        "the tightest credential each source can be given, and what nable calls",
        "with it.",
    ]
    buckets = by_grade()
    for grade in GRADE_ORDER:
        scopes = buckets.get(grade) or []
        if not scopes:
            continue
        out += ["", f"{grade.upper()}  {GRADE_LABEL[grade]}", ""]
        for s in scopes:
            out.append(f"  {s.provider}")
            out.append(f"      credential  {s.credential}")
            out.append(f"      permission  {s.permission}")
            for i, call in enumerate(s.calls):
                out.append(f"      {'calls' if i == 0 else '     '}       {call}")
            if s.note:
                out.append(f"      note        {s.note}")
            if s.gap:
                out.append(f"      known gap   {s.gap}")
            if s.mint_url:
                out.append(f"      create at   {s.mint_url}")
            out.append("")

    holes = gaps()
    if holes:
        out += [
            "Known gaps: " + ", ".join(sorted(s.provider for s in holes)) + ".",
            "A narrower credential exists there and nable does not use it yet.",
            "",
        ]
    out += [
        "Nothing above grants write access. For the AWS policy in full:",
        "  nable scan --dry-run --json",
    ]
    return "\n".join(out)
