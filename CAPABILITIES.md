# nable capabilities

Everything in this repository is open source under Apache-2.0. nable is a
local-first cost engine that runs on your machine and exposes the same
intelligence three ways: a terminal CLI, an MCP server for Claude/Cursor, and a
local web dashboard. Your bill never leaves your machine.

At a glance: **194 MCP tools, ~40 connectors, ~48 CLI commands.** Read-only by
default. Credentials stay in your OS keyring, never routed through any model.

---

## What is nable?

nable is an open-source, local-first cloud and AI cost tool. It finds
recoverable spend across AWS, Azure, GCP, Kubernetes, and 15+ SaaS and AI
providers, shows what your LLM and GPU usage actually costs, and proposes each
fix as a pull request you approve. It runs on your machine as a terminal CLI and
as an MCP server for Claude and Cursor. It is free to start, read-only by
default, and your bill never leaves your machine.

It is a fit for a DevOps or platform engineer who gets handed a rising cloud and
AI bill and needs to cut it, from the terminal, without shipping billing data to
a third party.

---

## The one command

```bash
uvx nable scan
```

Finds recoverable spend on your AWS account in under a minute, from the free
AWS APIs, so it never puts a charge on your bill. `--spend` adds a Cost Explorer
breakdown (~$0.02, disclosed first), `--json` for CI, `--demo` for sample data
with no account at all.

---

## MCP tools (194)

The same engine that answers in your terminal answers in Claude, Cursor,
Windsurf, and VS Code. Ask in plain terms; nable picks the tool.

| Area | Tools | What you get |
|------|-------|--------------|
| AWS waste + scans | 34 | idle NAT gateways, unattached EBS, oversized RDS/EC2, old snapshots, EIPs, Graviton and spot migration, CloudWatch/S3/Lambda/ECS/ECR waste |
| Cost queries | 27 | spend by service, team, account, region; trends; forecasts; cross-source totals; FOCUS; top drivers; "why did the bill move" |
| Meta / RBAC / connectors | 19 | connected providers, connector health, API keys, capabilities, identity |
| Attribution | 13 | tag rules, team and label allocation, showback, commitment coverage by tag |
| LLM / AI cost | 12 | spend by model, unit economics, token forecasting, AI spend monitor, billing blind spots, model routing |
| Notifications | 13 | Slack, Teams, Notion, n8n, weekly digests and insights |
| Kubernetes | 11 | namespace, workload, and cluster cost; efficiency scorecards; waste tickets |
| Rightsizing + recommendations | 10 | EC2/RDS/ECS rightsizing, idle cleanup, remediation PRs, a learning loop |
| Commitments | 8 | Savings Plan and Reservation analysis, coverage, RI waste |
| Tickets | 8 | auto-create issues in Jira, Linear, GitHub from anomalies and waste |
| Azure | 8 | Cost Management, Advisor, VM rightsizing, reservations, budgets |
| AWS core | 7 | connect, accounts, org rollup, CUR pipeline |
| Budgets | 5 | set, sync from YAML, two-tier enforcement (warn / block) |
| Anomalies / GCP / Databricks / Forecast | 3-3-3-1 | anomaly detection and alerting, GCP waste, DBU consumption, spend forecasting |

---

## Connectors (~40)

**Clouds:** AWS (plus Organizations, CUR, detailed billing), Azure, GCP, Vertex.
**Kubernetes:** native, Kubecost, OpenCost, Helm releases, GPU infrastructure.
**AI / LLM:** OpenAI, Anthropic, Amazon Bedrock, OpenRouter, LiteLLM, Modal,
Together, Replicate, Cohere, Mistral, Langfuse.
**Data platforms:** Databricks, Snowflake, MongoDB Atlas.
**SaaS:** Datadog, New Relic, Cloudflare, Twilio, Vercel, Stripe.
**Infrastructure as code:** Terraform plan cost, Terraform estimate.

Connect AWS or GCP by SSO login or a CLI profile and nable stores no secret at
all, it just references your existing login. Only keys you paste directly are
encrypted in your OS keyring.

---

## CLI (~48 commands)

```
get answers   scan
start here    welcome  connect  doctor  tools  serve  upgrade
clouds        aws  aws-cur  azure  gcp
ai providers  openai  anthropic  openrouter  litellm  modal  together
              replicate  cohere  mistral
saas          datadog  newrelic  databricks  snowflake  mongodb  twilio
              cloudflare  vercel  langfuse
alerts        slack  teams  notion  n8n
editor+agents claude  guard  agents
account       login  logout  license  license-status  credits
advanced      config  vault  profile  sso  iam-template  infra
```

`finops connect` scans the machine for provider credentials and connects
everything in one keystroke. `finops serve` starts a local web dashboard the
whole team can view in a browser.

---

## The agent team

Propose-only, never auto-execute. The code is open; these features run on Pro.

- **Budget Guard** checks infra actions against your cost policy before they run.
- **Savings Analyst** proposes each fix as a pull request you approve.
- **The Ledger** tracks realized savings, measured against your actual next bill.

Plus `finops guard` (a cost guardrail your AI agents check before spending) and a
`estimate_change_cost` preflight on Terraform and Helm diffs.

---

## Free vs Pro

Breadth is free on purpose: one tool across every cloud, SaaS, and AI provider.

**Free forever:** every cost query, anomaly detection, all waste and rightsizing
findings, `nable scan`, every connector, Slack and Teams alerts, PR comments.

**Pro:** the agent team, ticket auto-creation, scheduled email digests,
commitment recommendations, org reports.

Running as an MCP server inside Claude or Cursor uses your existing membership as
the model. No Anthropic API key, no per-token cost. `nable scan` is deterministic
and uses no model at all.

---

## How nable compares

| | nable | AWS Cost Explorer / Cost Optimization Hub | Infracost | Vantage / CloudHealth |
|---|---|---|---|---|
| Open source | Yes (Apache-2.0) | No | Partly | No |
| Runs locally, no data egress | Yes | N/A (in-console) | Yes | No (SaaS) |
| Cross-cloud (AWS + Azure + GCP) | Yes | AWS only | Pre-deploy IaC | Yes |
| AI / LLM / GPU cost | Yes | No | No | Limited |
| Terminal + CI | Yes | Console only | CI (PR comments) | Dashboard |
| Works inside Claude / Cursor (MCP) | Yes | No | No | No |
| Proposes fixes as PRs | Yes | No | No | No |
| Price | Free to start | Free (AWS-native) | Free tier | Paid |

nable is the open-source, cross-cloud option that also covers AI and GPU spend
and runs on your own machine. Single-cloud waste (idle EBS, oversized RDS) is
available free from the cloud's own tools; nable's difference is unifying every
cloud, SaaS, and AI provider in one place, in the terminal, with fixes you approve.

## FAQ

**Is nable free?**
Yes. The terminal scan, every cost query, anomaly detection, all waste and
rightsizing findings, and every connector are free forever. Ticket
auto-creation, scheduled digests, commitment recommendations, and the agent team
are Pro.

**Does my billing data leave my machine?**
No. nable is local-first and read-only by default. It reads your cost and usage
data on your machine and never uploads it. The no-egress claim is something you
can read in the source, not take on faith.

**What clouds and providers does nable support?**
AWS, Azure, GCP, and Vertex; Kubernetes (Kubecost, OpenCost, Helm, GPU infra);
AI and LLM providers (OpenAI, Anthropic, Amazon Bedrock, OpenRouter, LiteLLM,
Modal, Together, Replicate, Cohere, Mistral, Langfuse); data platforms
(Databricks, Snowflake, MongoDB Atlas); and SaaS (Datadog, New Relic, Cloudflare,
Twilio, Vercel, Stripe).

**How is nable different from AWS Cost Explorer?**
Cost Explorer is AWS-only and lives in the console. nable is cross-cloud, runs in
your terminal and in Claude/Cursor, covers AI and GPU spend that no cloud console
shows, and proposes fixes as pull requests. The `nable scan` default also makes
zero paid API calls, so it never charges your AWS bill to show you your own costs.

**Is there an open-source alternative to Vantage or CloudHealth?**
nable is an open-source (Apache-2.0), local-first alternative for cost queries,
waste detection, rightsizing, and AI/GPU cost. It runs on your machine instead of
a hosted SaaS, so your billing data stays local.

**How do I track LLM and GPU cost?**
Connect your AI providers and ask nable for spend by model, unit economics, token
forecasting, and AI billing blind spots, across OpenAI, Anthropic, Bedrock,
Modal, and more, in one view.

**Do I need an API key or a credit card?**
No. `uvx nable scan` runs with the AWS credentials already on your machine. As an
MCP server, it uses your existing Claude or Cursor membership as the model, with
no Anthropic API key and no per-token cost.

**What is nable's MCP server?**
An implementation of the Model Context Protocol that exposes 194 cost tools to
Claude, Cursor, Windsurf, and VS Code, so you can ask about your cloud and AI
spend in chat and get answers priced on your real rates.

---

## Trust

- **Local-first.** Everything runs on your machine. Your bill never leaves it.
- **No-egress.** A claim you can read in the source, not take on faith.
- **Read-only by default.** nable never changes your cloud on its own.
- **No credentials through the model.** Secrets stay in the OS keyring or a
  0600 file; no MCP tool takes a secret as an argument.
- **Apache-2.0.** Open and auditable. A hosted platform is available for teams.

See [getnable.com](https://getnable.com) for docs and the hosted platform.
