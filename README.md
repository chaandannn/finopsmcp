# nable

**See where your cloud and AI bills go, and spend less. Runs in your terminal or inside Claude, Cursor, and VS Code.**

[![PyPI](https://img.shields.io/pypi/v/finops-mcp?label=pypi&color=4db8d4)](https://pypi.org/project/finops-mcp/)
[![PyPI Downloads](https://static.pepy.tech/personalized-badge/finops-mcp?period=total&units=INTERNATIONAL_SYSTEM&left_color=BLACK&right_color=GREEN&left_text=downloads)](https://pepy.tech/projects/finops-mcp)
[![Tests](https://github.com/getnable/finopsmcp/actions/workflows/test.yml/badge.svg)](https://github.com/getnable/finopsmcp/actions/workflows/test.yml)
[![License: Apache-2.0](https://img.shields.io/badge/license-Apache--2.0-4db8d4)](LICENSE)
[![MCP Toplist](https://mcptoplist.com/badge/io.github.getnable%2Ffinops-mcp.svg)](https://mcptoplist.com/server/io.github.getnable%2Ffinops-mcp)

You do not need to be a cloud-cost expert. nable does three things:

- **Shows what you spend** across AWS, Azure, GCP, Kubernetes, and 15+ AI and SaaS providers, in one place.
- **Finds what you are wasting** (idle servers, oversized databases, forgotten storage) and puts a dollar figure on each one.
- **Fixes it, with your approval,** by opening a pull request, then checks your next bill to prove the saving was real.

Everything runs on your machine, read-only, and your billing data never leaves it.

## Try it

```bash
uvx nable scan
```

```text
nable scan · profile prod
account 3521… · this account only
scanning 17 regions …
  us-east-1 ......... 3 findings
  eu-west-1 ......... 1 finding
────────────────────────────────────────────
$2,140/mo recoverable
    $1,200/mo  3 idle NAT gateways, us-east-1
      $610/mo  14 unattached EBS volumes (2.1 TB), us-east-1
      $330/mo  idle RDS instance (db.r5.xlarge, <2% CPU), eu-west-1
run `nable scan --spend` for the spend breakdown (uses Cost Explorer, ~$0.02)
```

Reads only free cloud APIs, so scanning never adds to your bill. `uvx nable scan --demo` runs on sample data with no account at all. Add `--json` for CI, or `--spend` for a deeper breakdown.

![nable demo: a sample bill in seconds](https://raw.githubusercontent.com/getnable/finopsmcp/main/docs/demo.gif)

## Use it in your editor

`uvx nable` runs as a local MCP server inside Claude, Cursor, and VS Code, on your existing Claude or Cursor membership, no API key and no per-token cost. Then ask:

- "Why did our AWS bill jump last month?"
- "How much are we spending on OpenAI and Anthropic?"
- "Which instances should we downsize?"
- "Open a Jira ticket for any waste over $200/mo"

## Setup

Requires Python 3.11+. Need `uv`? `curl -LsSf https://astral.sh/uv/install.sh | sh` (or `brew install uv`).

```bash
uvx nable
```

The setup wizard finds AWS or GCP credentials already on your machine (an SSO login, a CLI profile, or default credentials), connects the one you pick, and configures your editor. Usually you never type a key.

**Cursor one-click:** [`Add nable to Cursor`](cursor://anysphere.cursor-deeplink/mcp/install?name=nable&config=eyJjb21tYW5kIjogInV2eCIsICJhcmdzIjogWyItLXB5dGhvbiIsICIzLjEyIiwgImZpbm9wcy1tY3AiXX0=)

Free forever for the local tool. A hosted version for teams (dashboards without a terminal, SSO, scheduled reports, always-on agents) is at [getnable.com/pricing](https://getnable.com/pricing).

<details>
<summary><b>Manual editor config</b> — only needed if setup didn't auto-configure</summary>

If `finops setup` doesn't auto-configure, run:

```bash
finops setup claude
```

Or add manually to `claude_desktop_config.json`:

**With uvx (recommended):**
```json
{
  "mcpServers": {
    "nable": { "command": "uvx", "args": ["--python", "3.12", "finops-mcp"] }
  }
}
```

**With absolute path:**
```json
{
  "mcpServers": {
    "nable": { "command": "/usr/local/bin/finops-mcp" }
  }
}
```
Use the path from `which finops-mcp`.

Config file locations:
- **macOS**: `~/Library/Application Support/Claude/claude_desktop_config.json`
- **Windows**: `%APPDATA%\Claude\claude_desktop_config.json`
- **Linux**: `~/.config/Claude/claude_desktop_config.json`

> **Why uvx?** Claude Desktop is a GUI app and doesn't inherit your shell's PATH. uvx runs finops-mcp in its own isolated environment. It's the most reliable option on corporate machines with managed Python installs.

</details>

<details>
<summary><b>Give your agent cost controls</b> — a pre-action budget gate for coding agents</summary>

nable is not just tools your agent reads from. It is a pre-action gate your agent
calls **before** it makes a cost-affecting change: it prices the change, checks it
against your budget, and offers a cheaper path. It never applies anything itself.
Propose-only, your agent proposes and a human approves.

Add one line to your agent's system prompt (Claude Code, Cursor, or any MCP client):

> Before you apply any infrastructure change (a terraform apply, a helm upgrade,
> creating or resizing a resource) or start an expensive job, first call
> `check_action_policy` with the action and the change (a terraform plan, a helm
> diff, or a `monthly_delta_usd`). Relay the verdict, the dollar impact, and the
> cheaper path when one is offered. Never apply a `block` or an `escalate` action;
> surface it to the human. nable is advisory and propose-only.

The gate returns `allow` / `warn` / `block` / `escalate` against your policy, the
monthly and annual dollar impact, and a spot alternative when the change is compute.
One-way doors (delete, terminate, buy a commitment) and over-budget changes always
escalate to a human.

**And a budget for the agent itself.** Run `nable ai-budget` once, it asks whether
you are on a flat plan or a metered API and what you pay, then remembers. On a flat
plan it tracks how much subsidized compute you pull for your fixed fee and warns
before you run low; on metered it gates on a dollar spend cap. `check_ai_budget` does
the same for the agent mid-task. It reads your Claude Code usage locally, nothing
uploaded. Add to your system prompt:

> Before starting a large task, call `check_ai_budget`. If it returns `warn` or
> `over`, tell me where I stand before continuing.

It reports your real usage and burn rate against your budget, not a fabricated
percentage of a plan's hidden rate limit.

</details>

<details>
<summary><b>Connectors (17)</b> — every provider and what it pulls, plus Azure roles</summary>

| Provider | What it pulls |
|---|---|
| AWS | Cost Explorer (free tier) · CUR via S3 (Pro: line-item granularity, savings plans, reservations) |
| Azure | Cost Management API · Advisor cost recs · VM rightsizing (Azure Monitor) · native budgets · forecast |
| GCP | Cloud Billing API + BigQuery export |
| Datadog | Usage Metering API v2: real dollar amounts |
| Snowflake | ACCOUNT_USAGE.METERING_HISTORY |
| Langfuse | Daily metrics API: model cost, token usage, trace volume |
| MongoDB Atlas | Invoice API |
| Twilio | Usage Records API |
| Cloudflare | Billing API |
| Vercel | Invoice API (Enterprise) |
| New Relic | Data ingest + user counts |
| Stripe | Fees and billing activity |
| Databricks | DBU usage and SQL warehouse spend |
| OpenAI | API usage and token spend by model |
| Anthropic | Claude API usage and token spend |

**Azure roles.** The Azure tools span three RBAC roles, granted to the service principal on each subscription (run `finops doctor` to check):

```bash
# repeat per subscription
az role assignment create --assignee <client-id> --role 'Cost Management Reader' --scope /subscriptions/<sub-id>
az role assignment create --assignee <client-id> --role Reader --scope /subscriptions/<sub-id>
az role assignment create --assignee <client-id> --role 'Monitoring Reader' --scope /subscriptions/<sub-id>
```

</details>

<details>
<summary><b>FAQ</b> — free vs paid, providers, how it compares to Cost Explorer / Vantage</summary>

**Is nable free?** Yes. The terminal scan, every cost query, anomaly detection, all waste and rightsizing findings, and every connector are free forever. The agent team, ticket auto-creation, scheduled digests, and commitment recommendations are Pro.

**Does my billing data leave my machine?** No. nable is local-first and read-only by default. It reads your cost data on your machine and never uploads it, and you can confirm the no-egress behavior in the source.

**What clouds and providers does it support?** AWS, Azure, GCP, and Vertex; Kubernetes (Kubecost, OpenCost); AI and LLM providers (OpenAI, Anthropic, Bedrock, OpenRouter, LiteLLM, Modal, Together, Replicate, Cohere, Mistral, Langfuse); data platforms (Databricks, Snowflake, MongoDB); and SaaS (Datadog, New Relic, Cloudflare, Twilio, Vercel, Stripe).

**How is it different from AWS Cost Explorer?** Cost Explorer is AWS-only and console-bound. nable is cross-cloud, runs in your terminal and in Claude/Cursor, covers AI and GPU spend no cloud console shows, and proposes fixes as pull requests. `nable scan` also makes zero paid API calls by default.

**Is there an open-source alternative to Vantage or CloudHealth?** nable is an open-source (Apache-2.0), local-first alternative for cost queries, waste detection, rightsizing, and AI/GPU cost, running on your machine instead of a hosted SaaS.

</details>

<details>
<summary><b>Troubleshooting</b> — install and setup fixes</summary>

```bash
finops-doctor          # checks credentials, DB, network, audit log
finops setup claude    # re-run editor configuration only
```

| Symptom | Fix |
|---|---|
| Tools don't appear in Claude | Switch to uvx config or use absolute path |
| `command not found: finops-mcp` | Re-install with `pip install finops-mcp` or use `uvx` |
| AWS returns no data | Run `finops setup aws` |
| `No matching distribution found for finops-mcp` | Your Python is older than 3.11. Install on 3.11+ (`uvx --python 3.12 nable`, or `python3.11 -m pip install finops-mcp`). |
| `cryptography` build error / `maturin failed` | uv tried to compile on Python 3.10. Use 3.11+: `uvx --python 3.12 nable`. |
| Corporate SSL errors | `pip install --trusted-host pypi.org --trusted-host files.pythonhosted.org finops-mcp` |
| Works at home, not at work | Use `uvx` (corporate IT often strips custom PATH entries) |

</details>

## License

Apache-2.0 in full. The hosted enterprise layer (web dashboard, SSO, control plane) lives in a separate private repo. Full tool list in [CAPABILITIES.md](CAPABILITIES.md).

[getnable.com](https://getnable.com) · [Docs](https://getnable.com/docs) · [Privacy](https://getnable.com/privacy) · [Security](https://scorecard.dev/viewer/?uri=github.com/getnable/finopsmcp)

<sub>mcp-name: io.github.getnable/finops-mcp</sub>
