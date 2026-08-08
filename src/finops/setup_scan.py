"""Ambient credential scan: find every provider credential already on this
machine and connect them in one keystroke.

The AWS connect flow proved the pattern (detect, confirm, done); this module
generalizes it to the whole connector surface so "connect everything" is a
sub-10-minute job instead of 30 paste-a-key wizards:

  - environment variables for every API-key provider nable supports
  - well-known credential files (~/.modal.toml, ~/.databrickscfg)
  - a gcloud ADC flag that routes into the GCP detect-confirm flow

Used by `finops connect` (the standalone command) and by the welcome flow
(the "also on this machine" moment right after the first bill renders).
Secrets are never printed; values go straight into the encrypted vault.
"""
from __future__ import annotations

import configparser
import os
import re
from pathlib import Path

# ── Where to mint a key, per provider (display-name keyed) ────────────────────
# Used by setup_saas_api_key deep links and by the "not found" section of the
# scan report. url may be None when there is no key page (e.g. self-hosted).
KEY_HELP: dict[str, tuple[str | None, str]] = {
    "Notion":        ("https://www.notion.so/my-integrations", "Create an internal integration, copy the token."),
    "Datadog":       ("https://app.datadoghq.com/organization-settings/api-keys", "App key lives next door under Application Keys."),
    "Langfuse":      ("https://cloud.langfuse.com", "Project → Settings → API Keys."),
    "Snowflake":     (None, "Your regular Snowflake login; the account identifier is in your Snowflake URL (e.g. xy12345.us-east-1)."),
    "MongoDB Atlas": ("https://cloud.mongodb.com", "Organization → Access Manager → API Keys (Org Billing Viewer role)."),
    "Twilio":        ("https://console.twilio.com", "Account SID and Auth Token are on the console home page."),
    "Cloudflare":    ("https://dash.cloudflare.com/profile/api-tokens", "Create Token → Permissions: Account, Billing, Read. The Account ID is on the dashboard overview page."),
    "Vercel":        ("https://vercel.com/account/tokens", "The invoice API needs a Vercel Enterprise plan."),
    "OpenAI":        ("https://platform.openai.com/settings/organization/admin-keys", "Billing data needs an ADMIN key (sk-admin-…), a regular key is not enough."),
    "Anthropic":     ("https://console.anthropic.com/settings/admin-keys", "Cost data needs an Admin key plus your Organization ID."),
    "OpenRouter":    ("https://openrouter.ai/settings/keys", "The provisioning key (per-model usage) is under Settings → Provisioning."),
    "LiteLLM proxy": (None, "The master key is LITELLM_MASTER_KEY in your proxy's own config."),
    "Modal":         ("https://modal.com/settings/tokens", "Or just run `modal token new`, nable reads ~/.modal.toml."),
    "Together AI":   ("https://api.together.ai/settings/api-keys", ""),
    "Replicate":     ("https://replicate.com/account/api-tokens", ""),
    "Cohere":        ("https://dashboard.cohere.com/api-keys", ""),
    "Mistral AI":    ("https://console.mistral.ai/api-keys", ""),
    "New Relic":     ("https://one.newrelic.com/api-keys", "Use a USER key (NRAK-…)."),
    "Databricks":    ("https://docs.databricks.com/en/dev-tools/auth/pat.html", "Workspace → Settings → Developer → Access tokens; nable also reads ~/.databrickscfg."),
    "GCP":           ("https://console.cloud.google.com/iam-admin/serviceaccounts", "Or run `gcloud auth application-default login`, nable detects it."),
}

# ── What the env scan looks for ────────────────────────────────────────────────
# slug -> (display name, required env keys (all present = a find), optional keys
# captured alongside). AWS/Azure/GCP have richer dedicated flows and are handled
# separately; alert outputs (Slack, Notion, n8n) are not billing sources.
PROVIDER_ENV: dict[str, tuple[str, list[str], list[str]]] = {
    "openai":     ("OpenAI",        ["OPENAI_API_KEY"],                                ["OPENAI_ADMIN_KEY", "OPENAI_ORG_ID"]),
    "anthropic":  ("Anthropic",     ["ANTHROPIC_API_KEY"],                             ["ANTHROPIC_ADMIN_KEY", "ANTHROPIC_ORGANIZATION_ID"]),
    "openrouter": ("OpenRouter",    ["OPENROUTER_API_KEY"],                            ["OPENROUTER_PROVISIONING_KEY"]),
    "litellm":    ("LiteLLM proxy", ["LITELLM_PROXY_URL", "LITELLM_MASTER_KEY"],       []),
    "modal":      ("Modal",         ["MODAL_TOKEN_ID", "MODAL_TOKEN_SECRET"],          []),
    "together":   ("Together AI",   ["TOGETHER_API_KEY"],                              []),
    "replicate":  ("Replicate",     ["REPLICATE_API_TOKEN"],                           []),
    "cohere":     ("Cohere",        ["COHERE_API_KEY"],                                []),
    "mistral":    ("Mistral AI",    ["MISTRAL_API_KEY"],                               []),
    "datadog":    ("Datadog",       ["DATADOG_API_KEY", "DATADOG_APP_KEY"],            ["DATADOG_SITE"]),
    "langfuse":   ("Langfuse",      ["LANGFUSE_PUBLIC_KEY", "LANGFUSE_SECRET_KEY"],    ["LANGFUSE_HOST"]),
    "snowflake":  ("Snowflake",     ["SNOWFLAKE_ACCOUNT", "SNOWFLAKE_USER", "SNOWFLAKE_PASSWORD"],
                                                                                       ["SNOWFLAKE_WAREHOUSE", "SNOWFLAKE_ROLE", "SNOWFLAKE_CREDIT_PRICE"]),
    "mongodb":    ("MongoDB Atlas", ["MONGODB_ATLAS_PUBLIC_KEY", "MONGODB_ATLAS_PRIVATE_KEY"],
                                                                                       ["MONGODB_ATLAS_ORG_IDS"]),
    "twilio":     ("Twilio",        ["TWILIO_ACCOUNT_SID", "TWILIO_AUTH_TOKEN"],       []),
    # Account ID is required, not optional: every billing endpoint is
    # account-scoped, and treating it as optional made the scan report
    # Cloudflare as found while the connector reported itself unconfigured.
    "cloudflare": ("Cloudflare",    ["CLOUDFLARE_API_TOKEN", "CLOUDFLARE_ACCOUNT_ID"], []),
    "vercel":     ("Vercel",        ["VERCEL_TOKEN"],                                  ["VERCEL_TEAM_ID"]),
    "newrelic":   ("New Relic",     ["NEW_RELIC_API_KEY"],                             ["NEW_RELIC_ACCOUNT_ID"]),
    "databricks": ("Databricks",    ["DATABRICKS_HOST", "DATABRICKS_TOKEN"],           ["DATABRICKS_ACCOUNT_ID", "DATABRICKS_ACCOUNT_TOKEN", "DATABRICKS_DBU_PRICE"]),
}


def _plausible(value: str | None) -> bool:
    return bool(value) and len(value.strip()) > 3


def _vault_get(key: str) -> str | None:
    try:
        from .security.vault import Vault
        return Vault.default().get(key)
    except Exception:
        return None


# ── File-based probes (dev machines keep creds in tool config files) ──────────

def _probe_modal_toml(home: Path) -> dict[str, str] | None:
    """~/.modal.toml: [profile] token_id = "ak-…" / token_secret = "as-…"."""
    p = home / ".modal.toml"
    if not p.exists():
        return None
    try:
        text = p.read_text()
    except Exception:
        return None
    tid = re.search(r'token_id\s*=\s*"([^"]+)"', text)
    tsec = re.search(r'token_secret\s*=\s*"([^"]+)"', text)
    if tid and tsec:
        return {"MODAL_TOKEN_ID": tid.group(1), "MODAL_TOKEN_SECRET": tsec.group(1)}
    return None


def _probe_databrickscfg(home: Path) -> dict[str, str] | None:
    """~/.databrickscfg: [DEFAULT] host = …, token = dapi…."""
    p = home / ".databrickscfg"
    if not p.exists():
        return None
    try:
        cfg = configparser.ConfigParser()
        cfg.read(p)
        host = cfg.get("DEFAULT", "host", fallback=None)
        token = cfg.get("DEFAULT", "token", fallback=None)
        if _plausible(host) and _plausible(token):
            return {"DATABRICKS_HOST": host.strip(), "DATABRICKS_TOKEN": token.strip()}
    except Exception:
        pass
    return None


def gcloud_adc_path(home: Path | None = None) -> Path | None:
    """The gcloud Application Default Credentials file, if present."""
    home = home or Path.home()
    candidates = [
        home / ".config" / "gcloud" / "application_default_credentials.json",
        Path(os.environ.get("APPDATA", "")) / "gcloud" / "application_default_credentials.json",
    ]
    for c in candidates:
        try:
            if str(c) != "gcloud/application_default_credentials.json" and c.exists():
                return c
        except Exception:
            continue
    return None


_FILE_PROBES = {
    "modal": _probe_modal_toml,
    "databricks": _probe_databrickscfg,
}


# ── The scan ───────────────────────────────────────────────────────────────────

def scan_ambient_credentials(home: Path | None = None) -> list[dict]:
    """Find provider credentials already on this machine that nable has not
    stored yet. Returns [{slug, name, source, env}] sorted by display name.
    Providers whose primary key is already in the vault are skipped: they are
    connected, re-offering them is noise."""
    home = home or Path.home()
    findings: list[dict] = []

    for slug, (name, required, optional) in PROVIDER_ENV.items():
        if _vault_get(required[0]):
            continue  # already connected

        # 1) environment variables (all required present)
        env_vals = {k: os.environ.get(k, "") for k in required}
        if all(_plausible(v) for v in env_vals.values()):
            captured = dict(env_vals)
            for k in optional:
                if _plausible(os.environ.get(k)):
                    captured[k] = os.environ[k]
            findings.append({"slug": slug, "name": name, "source": "environment", "env": captured})
            continue

        # 2) well-known credential files
        probe = _FILE_PROBES.get(slug)
        if probe:
            file_env = probe(home)
            if file_env:
                src = {"modal": "~/.modal.toml", "databricks": "~/.databrickscfg"}[slug]
                findings.append({"slug": slug, "name": name, "source": src, "env": file_env})

    findings.sort(key=lambda f: f["name"].lower())
    return findings


def connect_finding(finding: dict) -> bool:
    """Store a finding's credentials in the vault and emit telemetry.
    Returns True on success. Never raises; a vault failure must not kill the
    batch, the remaining finds still connect."""
    try:
        from .security.vault import Vault
        vault = Vault.default()
        for k, v in finding["env"].items():
            vault.store(k, v)
        try:
            from . import telemetry as _tel
            _tel._send_event(_tel._get_install_id(), "provider_connected", {
                "provider": finding["slug"],
                "auth_method": "ambient_scan",
                "source": finding["source"],
            })
        except Exception:
            pass
        return True
    except Exception:
        return False


# ── Interactive surfaces ───────────────────────────────────────────────────────

def _print_not_found_help(found_slugs: set[str]) -> None:
    """The 'want more?' tail: providers not on this machine, with the exact key
    page so nobody has to go hunting through a vendor console."""
    missing = [(slug, name) for slug, (name, _, _) in PROVIDER_ENV.items() if slug not in found_slugs]
    if not missing:
        return
    print("\n  Not on this machine (connect any of them in ~1 minute):")
    for slug, name in sorted(missing, key=lambda x: x[1].lower()):
        url, _hint = KEY_HELP.get(name, (None, ""))
        loc = f"  key: {url}" if url else ""
        print(f"    finops setup {slug:<11}{loc}")


def run_connect_command() -> None:
    """`finops connect`: one scan, one keystroke, everything on this machine
    connected. Then the deep-linked menu for whatever is not here."""
    print("\n  Scanning this machine for provider credentials…\n")
    findings = scan_ambient_credentials()

    # GCP is a flag, not a paste: route into the detect-confirm flow.
    gcp_hint = False
    if not _vault_get("GCP_BILLING_ACCOUNT_IDS") and (
        os.environ.get("GOOGLE_APPLICATION_CREDENTIALS") or gcloud_adc_path()
    ):
        gcp_hint = True

    if not findings and not gcp_hint:
        print("  No unconnected credentials found in the environment or config files.")
        _print_not_found_help(set())
        return

    if findings:
        print(f"  Found {len(findings)} unconnected provider(s):\n")
        for i, f in enumerate(findings, 1):
            print(f"   {i}) {f['name']:<15} ({f['source']})")
        print()
        try:
            ans = input("  Connect all? [Y/n, or numbers like 1,3]: ").strip().lower() or "y"
        except (KeyboardInterrupt, EOFError):
            print()
            return
        picked: list[dict] = []
        if ans in ("y", "yes"):
            picked = findings
        elif ans not in ("n", "no"):
            idx = {int(x) for x in re.findall(r"\d+", ans)}
            picked = [f for i, f in enumerate(findings, 1) if i in idx]
        ok = 0
        for f in picked:
            if connect_finding(f):
                ok += 1
                print(f"  ✓ {f['name']} connected ({f['source']})")
            else:
                print(f"  ✗ {f['name']} could not be stored, run: finops setup {f['slug']}")
        if ok:
            print(f"\n  {ok} provider(s) connected. Ask your editor: \"What's our total spend across everything?\"")

    if gcp_hint:
        print("\n  Google Cloud credentials detected (gcloud / ADC).")
        try:
            g = input("  Connect GCP now? [Y/n]: ").strip().lower() or "y"
        except (KeyboardInterrupt, EOFError):
            g = "n"
        if g in ("y", "yes"):
            from .setup_wizard import setup_gcp
            setup_gcp()

    _print_not_found_help({f["slug"] for f in findings})


def offer_ambient_connections(quiet: bool = True) -> int:
    """Welcome-flow hook: after the first bill renders, offer whatever else is
    sitting on this machine in a single prompt. Returns how many connected.
    Silent when there is nothing to offer, onboarding stays short."""
    try:
        findings = scan_ambient_credentials()
    except Exception:
        return 0
    if not findings:
        return 0
    names = ", ".join(f["name"] for f in findings[:6])
    print(f"\n  Also on this machine: {names}.")
    print("  nable can add them to the same bill, nothing leaves your machine.")
    try:
        ans = input(f"  Connect {'them all' if len(findings) > 1 else 'it'}? [Y/n]: ").strip().lower() or "y"
    except (KeyboardInterrupt, EOFError):
        print()
        return 0
    if ans not in ("y", "yes"):
        return 0
    ok = 0
    for f in findings:
        if connect_finding(f):
            ok += 1
            print(f"  ✓ {f['name']}")
    return ok
