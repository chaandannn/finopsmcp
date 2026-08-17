# nable for Claude Code

Local-first FinOps, installed as a Claude Code plugin. Ask what's driving your AWS, Azure, GCP, Kubernetes, and SaaS bill, right where you already work.

## Install

These are Claude Code slash commands, so run them at the **terminal Claude Code CLI** prompt (`claude`), not a plain shell. Some managed or GUI Claude surfaces don't expose `/plugin`.

Run them **one at a time**, waiting for the first to finish before the second. Pasting both at once makes the first command swallow the second and fail.

1. Add the marketplace:

```
/plugin marketplace add getnable/finopsmcp
```

2. Once it says the marketplace was added, install the plugin:

```
/plugin install nable@nable
```

That registers the `nable` MCP server (`uvx finops-mcp`). Restart Claude Code if prompted, then ask a cost question.

## Guided connect

After installing, run the bundled command to connect a cloud account and see your first cost number without leaving the editor:

```
/nable:connect
```

It checks what nable can already see (AWS often works with zero setup, it reuses your existing credential chain), shows your spend if you are connected, and gives you the exact terminal command if you are not.

## Connect your cloud

The plugin wires up the runtime. To link your accounts and see your first cost number, run the setup wizard once. Requires Python 3.10 or newer, check with `python3 --version`:

```
uvx --python 3.12 --from finops-mcp finops welcome
```

It writes your config, stores credentials in your OS keychain, and runs a read-only scan. Want to see it on sample data first:

```
uvx --from finops-mcp finops welcome --demo
```

## What you get

- 180+ tools for cost queries, anomaly detection, rightsizing, and PRs
- AI spend tracked by model, alongside cloud, Kubernetes, and SaaS
- Read-only by default. It runs on your machine. No vendor holds your data.

Docs: https://getnable.com
