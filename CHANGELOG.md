# Changelog

All notable changes to finops-mcp (nable).

## 0.8.197

- **Fixed: the MCP server would not start on a fresh install.** The `mcp` SDK released 2.0.0, which removed `mcp.server.fastmcp`. Our requirement had no upper bound, so from that release onward every new install of nable resolved 2.0.0 and `import finops.server` failed with `ModuleNotFoundError`. Anyone installing nable into Claude Desktop, Cursor or any other MCP client got a server that would not launch. The requirement is now `mcp[cli]>=1.28.1,<2`, which keeps the 1.28.1 floor that carries the CVE-2026-59950 fix. **If you installed nable and the MCP server failed to start, upgrade and it will work.** The cap comes off only alongside a port to the 2.x server API.
- The terminal was unaffected throughout, which is what made this hard to see: `entry.py` never imports the server, so `nable ai-budget`, `nable scan` and `nable guard` all kept working normally while the MCP server was dead. A test now asserts the ceiling exists in the packaging metadata, because that is the only thing governing what a stranger's fresh install resolves to.

## 0.8.196

- **Guard installs are now counted.** The guard shipped with no telemetry at all, so when the uv-cache-path bug in 0.8.194 turned up there was no way to answer how many people it reached. `guard_installed` carries three anonymous counters and nothing else: scope (project/global), outcome (new / already / repaired / write_failed), and which command form was written (binary or uvx). `repaired` fires when an install replaced a hook whose command no longer resolves, which is exactly that bug. No paths, no commands, no cost data, and silent under `NABLE_NO_TELEMETRY` like every other event.
- **Removed a real AWS account ID from the source and tests.** It appeared in an audit-log docstring and three test fixtures, and the repo's own screenshot-redaction script listed it as a string to scrub, so it was already understood to be sensitive in one place and published in plain text in four others. Replaced with AWS's documented `123456789012` placeholder. Account IDs are not credentials, but they make enumeration easier and there is no reason to hand them out.
- **The screenshot redaction script no longer publishes what it redacts.** Its find-and-replace map named a real customer and account inline, so anyone reading the script learned the strings it exists to hide. The map moved to a gitignored `scripts/redactions.local.json` and the script now fails with instructions when it is missing.

## 0.8.195

- **Fixed: `uvx nable guard install` installed a guard that stopped working without telling you.** The hook command was resolved with `shutil.which("finops")`, and under `uvx` that points inside uv's content-addressed archive cache (`.../uv/archive-v0/<hash>/bin/finops`). That path is real while the install is running and is a trap to write down: uv reclaims it on `uv cache clean` or `uv cache prune`, and the hash changes on the next release. After either, Claude Code could no longer execute the hook, and a hook that cannot run is skipped silently, so the agent went unguarded on `terraform destroy` and every other one-way door while `nable guard` still reported "installed". An ephemeral path now loses to `uvx --from finops-mcp finops guard hook`, which re-resolves each time it runs. If you installed the guard with `uvx`, run `nable guard install` once more to repair it.
- **`nable guard` now says when the hook is present but broken.** "Installed" used to mean "the entry is in the file", which is not the same as "this will run". It now checks that the hooked command still resolves and reports `installed, but broken` with the fix when it does not.
- **`nable guard install` no longer crashes on an unusual `settings.json`.** A top-level JSON array, a `"hooks": null`, or a `hooks` key holding the wrong type each raised a raw `AttributeError` from inside the installer. Your Claude Code settings hold your model choice, your permissions, and other tools' hooks, so nable now refuses and changes nothing rather than guessing at a repair. A settings file it cannot write reports the path and the reason instead of a traceback.
- Removed a dead "the Budget Guard is part of nable Pro" notice from `guard install`. The guard has been free since 0.8.194 and the branch could no longer fire, but it was one flag flip away from promising a paywall that does not exist.

## 0.8.194

- **The CLI now tells you to run `nable`, not `finops`.** The site teaches `uvx nable`, and `nable ai-budget` is the first command a lot of people run, but its own output pointed at `finops ai-budget`, a name they have never seen and do not have a reason to trust. Same leak in `nable guard`'s install, uninstall and usage lines, and in the agent-team setup steps. All of them say `nable` now. The hook command written into `settings.json` still resolves through the `finops` entry point, which is correct: that one is machinery, not instruction.
- Two regression tests that pinned this copy asserted on the full sentence, so renaming the command inside it would have made them pass without checking anything. They now assert on the wording-independent part.

## 0.8.193

- **`nable scan` now tells you why it failed and what to do.** Telemetry showed every external machine that ran a scan on 2026-07-24/25 failing instantly, three attempts each, then giving up. Two of the failure paths printed a raw AWS library string with no fix line at all: an `AWS_PROFILE` that does not resolve, and an unparseable `~/.aws/config`. Both are now named, and the missing-profile message lists the profiles nable can actually see and offers `--profile <name>`. Since `AWS_PROFILE` is exported in the shell of just about anyone with more than one AWS account, this was the most likely reason a first run went nowhere.
- **Credentials that AWS rejects are no longer reported as expired.** An unknown or revoked access key used to send you to `aws sso login` on a profile that is not SSO. Re-authenticating does not conjure a key AWS has never heard of, so it now says the key is rejected and points at `aws configure`.
- **New exit code 7** for local AWS config that does not resolve, distinct from 6 (no credentials found). A broken profile means the machine has a setup, it is just wrong, and sending that user to `aws configure` when the fix is one word in an environment variable is the wrong advice.
- **Fixed: schema migrations never ran on PostgreSQL.** Column detection used a SQLite-only `PRAGMA`, so on a shared-team Postgres database the error was swallowed and the `ALTER TABLE` silently never happened. Any column added since that database was created stayed missing, and the savings ledger stopped working. Detection is now backend-agnostic and the DDL is generated for whichever database you are on.
- The MCP server is now covered by a smoke test that launches the real process and speaks the actual protocol to it, rather than importing the server in-process. That is the difference between catching "the MCP server will not start" before a release and hearing about it from a user.

## 0.8.192

- **nable now tells you which savings it measured and which it only suspects.** Every deep-audit finding is classified by the strength of the evidence behind it. Something we observed directly (an unattached volume, a Compute Optimizer finding) is a recommendation and keeps its exact dollar figure. Something resting on a single-signal heuristic or an assumed price becomes an investigation: no precise number, an order-of-magnitude band instead, and the steps to confirm it. The audit headline splits accordingly, so instead of one total you get what nable stands behind plus a separate work queue. On a representative account that turned "$1,681/mo of waste found" into "$394/mo measured, $1,288/mo to confirm". The measured number is smaller on purpose. It is the one you can take to a change review.
- **A low-CPU instance is no longer assumed idle.** `get_instance_deep_analysis` now reads the network and disk metrics it was already fetching and ignoring, so a box sitting at 2% CPU while moving gigabytes a day is reported as a workload that is not CPU-bound rather than as waste. It also returns a single verdict instead of two that could disagree: AWS Compute Optimizer wins when it has an answer, because it weighs memory and IO too and prices the change itself, and nable's own read is labelled as the fallback it is. The CPU heuristic no longer attaches a dollar figure at all, since it never measured one.
- **A fix that comes undone is now reported as a regression.** Rightsize an instance, and if it drifts back months later nable says "fixed before, back again (2x)" instead of presenting it as a brand new finding. A regressed recommendation also drops its verified savings, because that figure described a state that no longer holds. A seven-day grace period keeps a change that is still landing from being mistaken for one that was reverted.
- **Anomaly alerts now carry dollars and a next step.** They used to report a percentage and a z-score, which cannot tell you whether +180% is noise on a small service or a budget event on a large one. Alerts now include the daily overspend, what it becomes over 30 days if it holds, and the specific question to ask next. Computed from data nable already has, so no extra API calls and no LLM on the alerting path.

## 0.8.191

- **Opening pull requests in your repos is now opt-in, off by default.** `open_rightsizing_pr` and `open_terraform_tag_pr` are the only paths that write anything outside nable, and they now refuse to push a branch until you explicitly turn them on with `FINOPS_REMEDIATION_ENABLED=true` or `remediation: {open_prs: true}` in `nable.policy.yaml`. Preview modes are unaffected: `dry_run=True` still shows the diff and `patch_only=True` still writes the `.tf` files locally for your own git flow. A security review can now assert "nable cannot open PRs here" as declared config instead of relying on the absence of a GitHub token. Mirrors the existing `FINOPS_CLEANUP_ENABLED` gate, and fails closed on a missing or malformed policy file.

## 0.8.190

- **`nable iam-template` now produces cross-account trust for the managed offering.** When `TrustedAccountId` is set (nable's hosting account), the read-only role trusts that account to assume it, with an optional `ExternalId` for confused-deputy protection; left blank it keeps the same-account instance-profile trust. This is what lets nable host a customer's single-tenant box and read their bill through a persistent role they own and can revoke, no expiring keys, no re-setup.

## 0.8.189

- **`ai-budget` no longer tells you to set a budget you just set.** After configuring a plan with no agent usage yet (no Claude Code logs, or early in the month), the status hid the plan row and printed "Run `finops ai-budget` to set a budget", reading as if nothing saved. It now always confirms the configured plan or spend cap and says usage will fill in as the agent runs. Found by dogfooding the interactive first run.

## 0.8.188

- **Fixed a first-run crash when accepting the budget guardrail on an upgraded install.** Old databases had a `budgets.block_at_pct` column (NOT NULL, no default) that the current model no longer sets, so onboarding's "set a monthly budget" step hit a NOT NULL constraint and dumped a full traceback. A migration now drops that orphaned column so the budget saves cleanly, and if a save ever does fail it degrades to a one-line message instead of a stack trace. Fresh installs were unaffected; this only bit people upgrading from an older schema.

## 0.8.187

- **Connect is now as few keystrokes as `gh auth login`.** When you have no AWS credentials, nable no longer just tells you to open a second terminal and run `aws configure sso`. It offers to run it for you, right there, sharing the terminal so the interactive prompts and the SSO browser sign-in work exactly as if you had run it by hand, then the credential watcher connects the instant the login lands. One flow, one place: run `nable`, answer the browser prompt, you're connected. Consent-gated (a clear `[Y/n]`), TTY-only, and declining falls straight through to watching so nothing is forced.

## 0.8.186

- **The no-credentials setup path no longer dead-ends or stalls.** Telemetry showed the real cliff: of machines that opened AWS connect, most never finished the credential probe, and the ones without credentials sat through 36 to 72 seconds of silence before anything appeared. Two fixes: (1) the probe's deadline is now actually enforced (it was bounded only for result collection, then blocked on stragglers anyway, boto3 sitting on the EC2 metadata endpoint through its retries); it returns in ~0.4s with no credentials and still finds real ones in under a second. (2) When nothing is found, nable now recommends the single command that fits your machine (`aws configure sso`, `aws configure`, or installing the CLI) and then **watches for the credentials to appear and connects automatically**, instead of blocking on an access-key prompt you cannot satisfy without leaving for the console. Set credentials up in another terminal and nable notices, no re-running.
- **No sample data at failure moments.** `nable scan` with no credentials, and the first-run close, used to advertise `--demo`; someone who just tried to see their own bill does not want a sample one. Both now point at the connect flow that waits. `serve --demo` (the dashboard preview) is unchanged.

## 0.8.185

- **`finops ai-budget` now just asks you.** Run it once and it asks two questions, flat subscription or metered API, and what you pay per month, then remembers. No flags to memorize, and any plan cost works, not a hardcoded $200. Flags (`--plan-cost`, `--spend-cap`, `--tokens`) still work for scripts, and the agent can set the budget conversationally through `set_ai_budget`.
- **Two lenses, because the two cases are different.** On a flat subscription the game is not running out: you see how much subsidized compute you pull for your fixed fee (a heavy month can be 20x+ your plan), the cost per 1M tokens on your plan versus list price, and an optional usage cap that warns before you run low. On a metered API key you set a dollar spend cap and the agent gates on estimated spend against it, also with cost per 1M tokens. A subscription is still never called "over budget" off a dollar estimate: the fee is what you pay.
- **The first-run banner leads with your agent's own usage.** When Claude Code logs are on the machine, `nable` opens with a real number pulled locally (tokens this window and burn rate) before any cloud is connected, points at `nable ai-budget`, then bridges to the actual product: guarding the infra that same agent touches (`nable guard install`, cost preflight + policy gate, propose-only). Every coding-agent user has usage, so it is the widest possible front door. Capped so a big log history never stalls the banner, and it shows nothing when there are no logs.
- **`finops upgrade` now actually upgrades the `finops` command.** It used to only warm the uvx cache and repin Claude Desktop, so a `pip install`ed CLI stayed on the old version and the new command still failed right after a "success" message. It now runs `pip install -U` on the environment you are in, then does the editor/Claude Desktop pin as a separate, clearly labeled step. Editable/source checkouts are detected and left untouched; a non-pip install (uv tool, pipx) prints the exact upgrade command instead.

## 0.8.184

- **A local budget for your AI coding agent.** New `finops ai-budget` command and the `check_ai_budget` MCP tool put a budget on the agent itself, not just your cloud. It reads your Claude Code token usage locally (nothing uploaded) and shows where you stand: this window, month to date, burn rate, and a verdict. On a metered API key you set a dollar cap; on a flat plan (Claude Max, Cursor) it shows how much compute you are pulling for your fixed fee, a heavy month can be 20x+ your plan, and the provider covers the difference. Your agent can call `check_ai_budget` before a big task and relay where you stand. Advisory only, it never blocks.
- **Honest by construction.** Token counts and burn rate are exact, read straight from the logs. Dollar figures are always labeled a list-price estimate, never your actual bill, and a flat plan is never called "over budget" off an estimate, because the fee is what you pay. It also does not invent a percentage of a plan's hidden rate limit, no API exposes that. Billable tokens are counted separately from cheap cache re-reads, so the meter measures real work.

## 0.8.183

- **`nable scan` is now cross-provider, not AWS-only.** When you have AI providers, GCP, or Azure connected, one scan shows spend and recoverable waste across all of them in one frame, the normalized cross-provider picture no cloud console gives you. A pure-AWS user sees exactly the v1 output; a machine with only AI keys and no AWS still scans. Built as an assembler over the existing aggregators (`scan_assembler.py`), self-tailoring to `connected_families()`.
- **Free-by-default holds across every provider.** Without `--spend`, the scan touches only free signals: usage-API AI spend (OpenAI/Anthropic/gateways), AWS waste, GCP Recommender, and the free Azure Cost Management + Advisor APIs. Nothing calls AWS Cost Explorer or the GCP BigQuery billing export. Under `--spend`, the cloud spend totals and cloud-native AI (Bedrock/Vertex) fill in, and Bedrock/Vertex are deduped out of the cloud totals so nothing double-counts. A test asserts the default AI path runs with `exclude_cloud_native=True`.
- **One provider never sinks the scan.** Each provider is gathered with its own timeout inside an overall budget; a provider that times out, auth-fails, errors, or returns nothing degrades to a per-provider note while the others still render.
- AI spend is always tagged `[estimated]` and AI recoverable `[early]`, per-provider promotion to trusted comes later.

## 0.8.182

- **One name, one behavior.** `nable`, `finops`, and `finops-mcp` now route through the same dispatcher, so they behave identically. `nable` is the canonical command; the other two are aliases. Previously `finops` skipped the dispatcher and could not act as the MCP server, so it silently diverged from `nable`.
- **Language cleaned up so nable no longer reads as an AWS-only tool.** The CLI `--help`, the onboarding banner, and the PyPI package description now lead with the cross-provider story (AWS, Azure, GCP, Kubernetes, and 15+ SaaS and AI providers) and treat the editor/MCP mode as one way to use nable, not its identity. `nable scan` is stated honestly as the AWS-first on-ramp, with `nable connect` for everything else.
- Onboarding example questions no longer all say "AWS"; they span cloud, Datadog, and AI token spend.

## 0.8.181

- **`nable scan`: the terminal front door, free by default.** One command against your existing AWS credentials prints the recoverable dollars and the top findings ranked by monthly savings, in under a minute, no MCP client involved. It makes **zero paid API calls**: every check reads only free AWS APIs (Describe\*, Compute Optimizer, CloudWatch), so a tool we call free never puts a charge on your AWS bill. It scans all opted-in regions on a 45s budget, returning partial results with a banner instead of hanging, and a clean account gets a proud "no material waste found" instead of an apology.
- **`--spend` is the opt-in spend breakdown.** Month-to-date total + top 3 services + % of bill, added above the recoverable line. It uses Cost Explorer, which AWS meters at ~$0.02 per scan, so `--spend` discloses that cost before making the call. The default scan never touches it.
- **Four typed failure states, zero stack traces.** No credentials (exit 6, offers `--demo`), expired SSO session (exit 3, prints the exact `aws sso login` command), permission denied (exit 4, prints the IAM actions + `nable iam-template`), and partial-with-nothing (exit 5). Partial WITH results exits 0 so `set -e` scripts keep working. Exit 2 stays argparse's for usage errors.
- **`--json` for scripts and CI** (stable schema, chrome on stderr), **`--demo`** (StreamCo sample data, every headline dollar labeled), **`--debug`**, **`--profile`**.
- Caught and fixed on the first real-account run: the EBS-snapshot check crashed in every region on a dead `ClientMeta.client` reference; least-privilege permission errors (ELB/ECR/ECS) flooded stderr instead of staying quiet; and a timed-out scan hung ~45s past its deadline because interpreter shutdown joined the still-running boto3 threads. Snapshot check fixed, analyzer warnings silenced unless `--debug`, and a timed-out scan now hard-exits cleanly the moment it has rendered.
- **First print in well under 2 seconds.** `nable` and `finops-mcp` now route through a light dispatcher (`finops/entry.py`) that lazy-imports the wizard or the MCP server, so a human typing a command never pays the server's import cost first. Piped no-args invocations reach the MCP server exactly as before; a regression test pins both dispatchers.
- **Engine: `run_deep_audit` grew optional `progress_callback` and `deadline_seconds`** for streaming per-region progress and partial-results deadlines. Defaults keep the MCP tool path byte-identical; timed-out regions are reported in `regions_timed_out`.
- `nable --help` now leads with a "get answers" group: the commands that produce value outrank the ones that configure them.
- The `nable` shim is 0.1.2: entry point repointed to the light dispatcher, dependency floor raised to 0.8.181 so cached uv environments can never resolve a build without `scan`.

## 0.8.180

- **Verified savings are now priced off the bill, not the price sheet.** When the scheduled verifier confirms a change landed (EC2 resize, Graviton migration, idle cleanup, RDS downsize), nable measures the realized saving from CUR: the resource's actual daily cost in the two weeks before the change vs after it settled. When CUR is not connected it falls back to the customer's measured effective rate (EDP/commitment discount), then to list price, and every verified row records which basis produced its number (`verified_basis`). The savings ledger reports `verified_bill_measured_monthly_usd` separately, so "measured off your bill" is only claimed when it is true.
- **Fixed: a confirmed change could bank $0.** If a resized instance's type was missing from the static price table, verification recorded a $0 saving. Confirmed changes now fall back to the recommendation's own estimate and are refined against the bill on later runs.
- **RDS rightsizing joined the verified loop.** RDS downsizing recommendations are now persisted to the savings tracker, and a read-only verifier confirms the `DBInstanceClass` switch, so database rightsizing earns verified, bill-measured savings the same way EC2 does.
- CUR per-resource queries accept a `resource_id` filter (matches bare ids and full ARNs).

## 0.8.179

- **Demo mode is now safe for the AI assistant end to end.** In demo mode, no assistant tool call can reach a real cloud account: cost slices, forecasts, commitments, budgets, and savings all answer from the sample dataset, and anything outside it returns a clear "not in the sample" note instead of live data. Previously a free-text question could route to a tool that read real credentials.
- **Demo scale now matches a mid-to-large company.** The sample environment models ~$5.1M/mo across 312 linked accounts and 9 providers, with a realistic 29% untagged share, a mixed-verdict rightsizing set (47 flagged, 12 genuine), and an anomaly with no identified cause. Sample data that looks like a real environment, not a toy account.
- **Saved views ship populated in demo mode**, including tag (`env`), monthly, and untagged-by-account views, so the dashboards gallery answers granularity and attribution questions out of the box.
- **The assistant loop can stream.** `ask()` accepts an `on_event` callback that emits text deltas and tool-start events, so web surfaces can render answers as they generate instead of blocking on the full response. Default behavior is unchanged.

## 0.8.178

- **Demo mode now shows a realistic mid-large company.** The built-in demo dataset (`FINOPS_DEMO=1`) models a CTV/streaming business at ~$673k/mo across 9 providers (AWS, GCP, Azure, Kubernetes, OpenAI, Anthropic, Datadog, Snowflake, Databricks), with a CDN-egress-driven cost story, so a no-credentials walkthrough looks like a real environment instead of a toy account. Every figure is internally consistent across tools.
- **The "what am I connected to" view is populated in demo mode.** `list_connected_providers`, `list_accounts`, `check_connector_health`, and `compare_providers` previously probed real credentials, so with none present they showed nothing connected. In demo mode they now advertise the seeded provider set.

## 0.8.177

- Maintenance: republish the MCP registry entry under the current namespace so directories that mirror the registry stay current. No functional changes to the package.

## 0.8.176

- **The advanced intelligence layer is now part of the nable enterprise product.** Cost-to-code blame (`find_cost_culprit`) and the deep LLM cost-intelligence tools (`get_ai_billing_blind_spots`, `get_llm_unit_economics`) move to the enterprise package. Everything else stays Apache-2.0 and free: the full MCP server, every connector, cost queries, anomaly detection, rightsizing, budgets, forecasting, and the local learning loop.
- **Plugin seam.** The open core loads enterprise tools through the `finops.plugins` entry point when that package is installed; with nothing installed it runs exactly as before.

## 0.8.175

- **nable proposes standing cost rules from what you keep rejecting.** Dismiss the same class of finding for a business reason a few times ("spot on prod, no") and nable offers one durable rule instead of nagging per finding, with the evidence and a one-call confirm. It has an over-suppression guard: a broad rule (a whole provider or environment) only fires when your rejections span multiple finding types, so a run of one type never becomes "ignore all of AWS." New tool: `suggest_cost_policies`.
- **Day-one ranking that already knows your kind of environment.** A brand-new install had no history, so it could only rank findings by raw dollars. Now it starts from expert-seeded priors per segment and finding type (AI-native teams kill idle GPUs first, graviton migrations usually land), then those fade as your own accept/reject history takes over. Findings carry a short rationale for why they're ranked where they are.
- Both are the local learning loop, free. They only shape which proposals surface and never touch a cloud.

## 0.8.174

- **nable now remembers why a finding is intentional.** Tell it once ("that idle box is our DR standby", "spot is fine on batch"), and it stops re-flagging that thing instead of nagging you every cycle. Dismiss a recommendation with a business reason and nable learns it automatically; or teach it directly with `remember_cost_context`. Suppressed findings are never deleted, they move to a `suppressed_by_context` list with your reason, so nothing is hidden without a trail.
- **Generalize the exception, not just the instance.** A learned rule can be one resource, a resource type ("all NAT gateways"), an environment ("anything in dr"), a finding type ("ignore rightsizing"), or a whole provider, optionally scoped to one account. `get_learned_cost_context` shows the operating model nable has built up for your environment; `forget_cost_context` removes a rule.
- This is the local learning loop, and it is free. It shapes which proposals surface and never touches a cloud.

## 0.8.173

- **nable is now open source (Apache-2.0).** The full local product, every MCP tool, connector, the agent team, and the new cost-to-code blame, is Apache-2.0. Read it, fork it, build on it. The hosted control plane is a separate product.
- **New tool: `find_cost_culprit` (cost-to-code blame).** Point it at a resource and it traces the instance-size change to the git commit and pull request that made it, prices the increase at your effective rate, and drafts a propose-only revert. No cloud-native cost tool can do this, because it reads your repo.
- **More is free.** The unified multi-cloud view (compare providers, total spend across every source), unit economics and business metrics, line-item CUR detail, and Azure resource detail are no longer gated. Breadth and insight into your own spend are free; you pay for it to run always-on for a team.
- **The web dashboard moved to the hosted product.** `finops serve` is no longer part of the open tool; the local product is the MCP server in Claude or Cursor.

## 0.8.172

- **MCP tools now carry annotations.** Every tool advertises a title plus a read-only or destructive hint. nable is read-only and propose-only, so 153 of 191 tools are marked read-only, 38 write, and only 3 destructive. Your editor shows cleaner tool names and knows which tools only read.
- **Best-in-class dashboard redesign.** Light mode is now the default (dark is the toggle), with an executive KPI band, an AI-efficiency panel, a what-changed digest, forecast-vs-budget, a saved-dashboards gallery, verified-savings visibility, and provider/date-range controls. The AI Analyst now keeps conversation context, and the what-changed digest is XSS-hardened.
- **Clearer re-auth for expired Azure/GCP sessions.** An expired SSO/OAuth token now tells you how to log back in for that provider instead of surfacing a raw SDK error.
- **First-run polish.** The API-key prompt notes keys are stored in your OS keychain and never sent anywhere; a `uvx` run tells you how to keep nable installed (`uv tool install nable`); and a real connection ends with a come-back nudge.
- **The AI analyst no longer leaks internal tool or function names** into its answers.

## 0.8.171

Upgrade prompt no longer advertises features that are currently free.

With the AI/agent features on the temporary free hold (0.8.168), the Pro upsell
shown when you hit a still-gated feature was still listing Budget Guard, the
Ledger, remediation, and commitment recommendations as paid unlocks. It now skips
any feature that is currently free, so the pitch only lists what actually needs
Pro. Found in the post-swing review.

## 0.8.170

A $0 cash bill no longer reads as free.

When an AWS account is running on Activate (or other promotional) credits, the
cost summary now volunteers the true burn without you having to ask: "Cash bill
$0, but you burned $434 of AWS usage this month. Credits covered $434 (100%).
Your steady-state bill is about $434/mo once the credits run out," plus a runway
estimate when credits are declining. It only appears when credits are materially
covering the bill, so a normal cash-paying account sees nothing extra. The credit
query is cached per account, so it does not add a Cost Explorer round-trip to
every summary. This is the first piece of the burn-comprehension view: gross vs
effective vs cash, surfaced where you already look.

## 0.8.169

When your session expires, nable tells you to log in, not to re-setup.

An expired SSO or MFA session now returns "log back in and ask again", naming the
profile and giving the exact `aws sso login --profile X` command, instead of the
old "re-run finops setup aws". Your account stays configured: nable stored a
profile reference, so once you log back in, the next query just works, no
reconfiguration. Also catches SSO-token expiry, which previously slipped through
as a generic error.

## 0.8.168

The AI and agent features are free for now.

While we get the first users fully set up, the agent team (Budget Guard, the
Ledger), remediation PRs, forecasting, proactive alerts, commitment
recommendations, and AI unit economics all run free. Pricing for them is being
decided separately. This is a deliberate, reversible hold: everything stays
wired the way it was, one flag re-gates it when the paid model ships. Propose-only
is unchanged: the agents still never execute on their own.

## 0.8.167

Honor AWS_PROFILE for account naming, and never store expiring keys.

`AWS_PROFILE=crosstx-mfa finops setup aws` now names the account `crosstx-mfa`,
not `aws-822638974044`. When credentials resolve through the default chain, nable
records the AWS_PROFILE you exported and stores that profile reference, not the
temporary session keys. So a workload account you don't own (no settable alias,
MFA-gated) keeps the name you gave it, and when the session expires nable simply
re-resolves the profile next run and asks you to log in again rather than holding
stale keys. You can list and query these accounts by name with list_aws_accounts.

## 0.8.166

Connected accounts use your profile name, not a bare account id.

When an account has no readable IAM account alias (the common case for an SSO
role, which usually lacks iam:ListAccountAliases), nable was naming it
aws-822638974044. It now falls back to the SSO / CLI profile name you already
use, so accounts read as `fd-sapro` instead of `aws-822638974044`. Order is:
account alias, then profile name, then aws-<account_id>.

## 0.8.165

No more "a keychain cannot be found" popup, and clearer credential messaging.

- On macOS, when there is no login keychain (an altered $HOME, a sandbox, or a
  hardened setup), the OS pops a blocking "a keychain cannot be found to store
  X" modal the instant keyring is called, and catching the Python exception does
  not stop it. nable now checks for the login keychain first and, if it is
  missing, uses its signed file store instead of touching the keychain. The
  trial store (license.py) and the credential vault (vault.py) both carry the
  guard. FINOPS_NO_KEYRING=1 still forces the file-only path everywhere.
- Docs and the site FAQ now say plainly: connecting AWS or GCP by SSO login or
  CLI profile stores no secret at all, nable only references it. Only keys you
  paste directly are encrypted in the OS keyring. This corrects copy that read
  as "nable puts your cloud credentials in your keychain" to security-conscious
  users who keep an empty ~/.aws/credentials and only SSO profiles.

## 0.8.164

AWS Identity Center (SSO) profiles are no longer dropped in silence.

Detection probes each profile with STS GetCallerIdentity, which throws for an SSO
profile whose token is not currently valid, so an Identity-Center-only account
never surfaced and read as "nable only checks ~/.aws/credentials" (real user
report). Now nable finds SSO profiles in ~/.aws/config that are configured but
not logged in, and surfaces them with the exact `aws sso login --profile X`
command, in both connect_aws (in your editor) and `finops setup aws`. Log in,
call connect again, and the account appears. Both the modern sso_session and the
legacy sso_start_url formats are detected; the sso_account_id is read from config
so the account shows even before you log in.

## 0.8.163

One command to a populated dashboard.

- New `finops serve --demo` (so `uvx nable serve --demo` works cold): serves the
  fully populated sample dashboard with auth off and opens your browser. Demo is
  forced, so a machine with a real account connected still shows the sample bill,
  which also makes it the demo-recording path.
- Terraform preflight fix: the untagged-resource check now unions keys across
  tags/tags_all/labels. AWS provider default_tags land in tags_all, so shops
  setting owner/team org-wide were falsely flagged on every resource.

## 0.8.162

Self-hostable dashboard, packaged.

- `FINOPS_DEMO_MODE=1 finops serve` now shows a fully populated dashboard with no
  cloud account connected: month-to-date spend, a projected total, a FinOps
  score, ranked savings opportunities, and the spend trend. Previously demo mode
  populated the MCP tools but left the web dashboard on the empty "connect an
  account" state, so anyone trying it before connecting saw nothing. Demo still
  yields to real data the moment a provider is configured.
- New `docker-compose.selfhost.yml`: a single-container, SQLite, no-Postgres path
  to run the dashboard on a laptop or homelab box. `docker compose -f
  docker-compose.selfhost.yml up -d`, credentials mount read-only, generated
  dashboard password printed to the logs. The full `docker-compose.yml` (TLS,
  SSO, Postgres, control plane) stays for team deployments.
- The public GHCR image now builds for `linux/arm64` as well as `amd64`, so a
  Raspberry Pi or Apple Silicon box pulls it natively.
- README and docs/DEPLOY.md gained a "run it on a server (Docker)" section with a
  one-command demo and the committed dashboard screenshot.

## 0.8.161

Practitioner-driven guard and preflight refinements.

Straight from a platform practitioner describing how their team actually runs
agents ("auto-execute idempotent actions in staging, production still requires
human approval" and "letting an agent tag resources with cost center and owner
avoided billing surprises"):

- The Budget Guard hook now recognizes a production context. A reversible
  mutation aimed at prod (kubectl -n prod, terraform with a prod var, etc.)
  gets a confirm instead of passing silently; staging stays zero-friction.
  Word-boundary matching so "product" never trips it; patterns overridable via
  FINOPS_GUARD_PROD_PATTERNS (set to "off" to disable).
- The Terraform preflight flags newly created resources that support tags but
  carry no owner/cost-attribution tag (owner, cost_center, team, ...), so
  unattributed spend is caught before apply instead of on the bill. Resources
  whose schema has no tags field are never flagged, so untaggable types produce
  no noise.
- Heartbeats now carry the running version, so build freshness is directly
  queryable instead of inferred from the surface tag's absence.

## 0.8.160

Connection-aware tool surface: the model only sees tools you can actually use.

nable advertised every registered tool to the model in every conversation, and
those definitions ride in the model's context on every message: ~44,500 tokens of
recurring overhead for the default 165-tool surface, and models pick tools worse
as the list grows. Now the advertised list is filtered by what this machine has
actually connected, from purely local signals (env vars, vault key names,
accounts.yaml, kubeconfig; cached ~30s; never the network).

- A machine with nothing connected advertises the 105 core tools (cross-provider
  cost/budget/anomaly/forecast plus the connectors), ~29,400 tokens: a 35% cut.
  An AWS-only machine advertises 132 (a 19% cut); Azure, GCP, Kubernetes,
  Databricks, Slack/Teams/Notion, and ticket tools appear only when their
  provider or integration is detected. Bedrock token tools ride the AWS account,
  so AWS alone reveals the LLM family.
- Advertisement-only: nothing is unregistered, and a hidden tool called by name
  still runs (verified against the SDK call path), so the in-chat connect flow
  cannot break. After a successful connect_aws / connect_gcp / connect_azure the
  server re-detects and nudges the client with tools/list_changed; a restart
  also refreshes.
- FINOPS_ALL_TOOLS=1 (the existing escape hatch) and demo mode advertise
  everything.
- Every tool is mapped to exactly one family and a completeness test enforces
  it, so a future tool nobody classifies fails CI instead of silently
  always-advertising.

## 0.8.159

Stale-build users now see the upgrade nudge IN CHAT, not just stderr.

Funnel diagnosis (PostHog, 7 days): ~97% of active machines are running builds
older than 0.8.141, so almost none of this month's onboarding work reaches them.
The staleness self-check already existed but only wrote to a stderr log, which no
human sees inside an editor, so the one channel meant to rescue stale users was
silent.

Now the server startup check stashes the note and the tool wrapper surfaces it on
the first tool result of a session (once per session, only when a newer build
exists): "nable X is out (you are on Y). To upgrade run `finops upgrade` (or `uvx
finops-mcp upgrade` if not found), then restart your editor." The uvx fallback
matters because most editor users launched via uvx and have no `finops` on PATH.
No network in the hot path (the note is computed by the existing background
thread), airgap/no-telemetry honored.

## 0.8.158

The scanner long tail goes parallel.

Continues 0.8.157's audit across the remaining serial scanners:

- Idle scan: one batched describe_volumes per 500 volumes instead of one call per
  stopped instance, and load balancer tags fetched 20 ARNs per call instead of one
  per LB. Per-LB target-health checks (no batch API exists) run through a bounded
  pool. A failed health check counts as healthy so unknown state is never flagged
  idle, and one bad LB no longer aborts the whole region's scan.
- Spot adoption: regions scan concurrently instead of paying the sum of every
  region's latency.
- EFS cross-AZ audit: the async tool ran blocking boto3 serially, which both
  serialized regions and froze the MCP event loop for the whole scan. Each region
  now runs in a thread via asyncio.gather.

Already parallel and left alone: idle's region fan-out, the public IPv4 and
CloudWatch alarm audits, and the provider layer from 0.8.62.

## 0.8.157

Performance audit: parallel scans, no more laptop hangs, a 2.6x faster suite.

- Rightsizing's CloudWatch fallback no longer walks regions x instances serially
  (a 100-instance fleet was 100+ sequential CloudWatch round-trips). Instance
  discovery runs per-region in parallel and CPU metric fetches run through a
  bounded worker pool. An instance whose metrics fail to load is treated as
  fully utilized, so unknown utilization can never be recommended for downsizing.
- GCP project_ids() no longer hangs ~9s on machines with no GCP credentials:
  google.auth's GCE metadata probe (3s x retries) is skipped unless a local ADC
  file or an explicit project env exists, bounded to 1s when it does happen, and
  the answer is cached for the process. Several GCP tools call this per request.
- Contributor DX: the test suite drops from ~42s to ~16s. Web-server test
  teardowns no longer wait on serve_forever's 0.5s poll (26 tests x 0.5s), a
  unit test no longer does a live GitHub org sweep on dev boxes with a real
  GITHUB_TOKEN, and a suite-wide conftest guard strips ambient service
  credentials so that class of leak cannot return.

## 0.8.156

The agent team is Pro. Free stays read-only: talk to your bill.

- New tier boundary, enforced: Budget Guard (check_action_policy + the guard
  hook), the Savings Analyst's drafting (generate_terraform_tag_fixes joins the
  already-gated PR tools), and the Ledger's learning loop (mark acted-on, verify
  savings, the learned approval profile) all require Pro. Free keeps every
  read-only surface: cost queries, anomalies, rightsizing findings, the savings
  ledger view, and the estimate_change_cost preflight.
- The guard hook fails open on the free tier and on any license error: a lapsed
  license can never block someone's terminal. `finops guard install` says
  plainly that the hook stays silent until Pro is active.
- New: get_agent_team MCP tool + `finops agents` CLI. One compact view of the
  three agents, what each does, its status on this install (active / needs
  setup / pro required), and the exact next command: guard install, set a
  budget, connect a cloud, or record a first decision. Ledger warmth
  (COLD/WARMING/WARM) shown so "why isn't nable learning" answers itself.
- Free users hitting a gated tool get one compact payload: what it does, the
  agent-team pitch, the upgrade URL, and the in-chat activate path.
- Pricing surfaces aligned: Dev = "Talk to your bill, read-only"; Pro = the
  agent team (Budget Guard, Savings Analyst, the Ledger) on the site and in the
  structured-data offers.

## 0.8.155

Agent 1 (Budget Guard): the action gate now learns from what you approve.

First of the three agentic pieces. check_action_policy's allow/block/escalate gate
was static (a human-authored allowlist + budget threshold). It now folds in this
customer's decision history: if you habitually decline a kind of action, an action
policy would ALLOW is tightened to ESCALATE so a human confirms it; actions you
habitually approve get a confidence note. This is agent cost-control that learns
from the actions you take, fusing the budget gate with the learning ledger.

Safety invariant (tested): learning is a one-way ratchet toward caution. It can
tighten ALLOW to ESCALATE, but it can NEVER loosen a BLOCK or ESCALATE into ALLOW,
so it can never make an agent act more aggressively than the static policy permits.
The learning is deterministic math over the local ledger, no LLM loop, so it stays
propose-only and token-free.

## 0.8.154

Demo mode now shows the genuine-savings judgment.

The get_rightsizing_recommendations demo fixture was still the old flat "here are
2 underutilized boxes, $828/mo" shape. It now mirrors the real output: per-rec
verdict/score/why/action, savings priced on the customer's real rate (a 22%
effective discount), and the headline that sells the product, $889/mo of raw
"underutilized" collapsing to $218/mo of genuine savings once burst, memory-bound,
and the real rate are accounted for. So a prospect poking the demo sees the
judgment, not a generic list.

## 0.8.153

Savings are now priced on the customer's real rates, not list price.

New shared effective-savings layer (effective_savings.py) that every savings tool
can route through, so a saving is what the customer would actually save on their
environment and their discounts:

- Measured effective rate first: reuses rate_detector (derived from the customer's
  CUR or Cost Explorer, amortized-vs-list), per-service, and already blends in EDP
  / private pricing / negotiated rates that no free recommender sees.
- Commitment coverage as a fallback when no rate data exists. Critically these are
  not stacked: the amortized effective rate already includes the RI/SP discount, so
  applying coverage on top would double-count.
- List price as the last resort, clearly labelled low-confidence with a prompt to
  connect CUR for exact rates.

Rightsizing routes through it. get_rightsizing_recommendations now returns
genuine_monthly_savings priced on real rates plus a pricing_basis block
(effective_discount_pct, rate_source, commitment_coverage_pct, per-row basis +
confidence counts). Token cost unchanged. Same pattern is ready to extend to idle,
Graviton, spot, RDS/ECS, and the audit tools next.

## 0.8.152

Rightsizing now judges genuine savings, not just underutilization.

First pass of deepening existing tools into proprietary reasoning. get_rightsizing_
recommendations used to hand back "this instance is underutilized, here's the
on-demand savings", the same commodity signal every cloud vendor ships free. It now
scores every recommendation against the reasons a rightsizing call is usually wrong
and returns a verdict (genuine_savings / review / likely_false_positive), a 0-100
score, and a one-line rationale:

- Burst/peak guard: low average with high peaks is a workload that needs headroom,
  not a downsize (measured peaks only, so Compute Optimizer's unknown-peak is never
  mistaken for a 0% peak).
- Memory-bound guard: idle CPU with high memory means a CPU downsize starves RAM.
- Commitment coverage: on-demand savings estimates are discounted by the account's
  actual Reserved Instance + Savings Plan coverage, so a downsize on a committed
  instance is not counted as a saving that is already paid for. New top-level
  genuine_monthly_savings reflects the discount.
- Magnitude: trivial changes are demoted on the real (adjusted) dollar figure.

Token cost held flat: the verbose per-row title/description strings were dropped for
the compact verdict/score/why/action, and commitment coverage is fetched once,
cached ~15 min, off-thread, and degrades silently if Cost Explorer is unavailable.
Fixed a dead upgrade nudge that read a key the summary never returned.

## 0.8.151

Security audit pass across the whole codebase, one dependency floor raised.

- cryptography floor raised from >=42.0.0 to >=48.0.1 (48.0.0 has
  GHSA-537c-gmf6-5ccf; fresh installs already resolved past it, the floor now
  guarantees it).
- Audited clean, no changes needed: zero hardcoded secrets in tree or git
  history; all 7 CI workflows SHA-pinned with no pull_request_target; no
  shell=True, eval, exec, pickle, or unsafe yaml anywhere; all SQL parameterized;
  all subprocess calls list-args; TLS verification never disabled; vault and
  accounts files chmod 600; dashboard requires a password by default with
  separate full vs read-only session stores; Slack bot is Socket Mode (no public
  endpoint); outbound hosts are exactly the provider APIs plus opt-out telemetry.

## 0.8.150

The CLI front door: grouped help and did-you-mean.

- finops --help no longer prints a flat wall of 45 subcommands with connectors
  first. Commands render in 8 groups with "start here" (welcome, connect, doctor,
  tools, serve, upgrade) at the top. Self-healing: a future command nobody assigns
  to a group auto-renders under "other" instead of vanishing, and a test flags it.
- A typo'd command gets "unknown command 'welcom'. Did you mean: welcome?" instead
  of argparse dumping all 45 choices in one line.
- iam-template and vault gained the help text they were missing.

## 0.8.149

doctor stops saying "all checks passed" when nothing is connected.

Full-terminal DX audit follow-up. `finops doctor` with zero providers connected
reported "Status: all checks passed" and told the user to restart Claude and ask
about their costs, a question that returns no data at that point. The verdict and
next step now branch on the one thing that gates value: a connected provider.
Unconnected machines get "healthy, but no cloud account is connected yet" and the
exact connect commands (terminal and in-chat).

Also verified in the same audit, no changes needed: all 45 subcommand --help
screens work, Ctrl-C mid-wizard exits without a traceback, `finops serve` prints
its URL and auto-generated password with clear overrides, `finops connect`'s
ambient key scan lists per-provider key URLs, and empty states (vault, profile)
all carry a next step.

## 0.8.148

DX audit: every "not configured" error now points in-chat, not at the terminal.

A live first-run audit found the worst possible contradiction at the exact spot a
freshly wired-in user lands: ask your first cost question with nothing connected
and the error said "Run 'uvx finops-mcp setup' in your terminal ... then restart
your AI client" while the connect hint beside it said "connect in-chat, no
terminal needed." The model repeats the error field, so new users were being sent
back to the terminal-and-restart flow that connect_aws (0.8.140) removed.

- All 27 stale error strings across server.py rewritten: cost tools, AWS tools,
  GCP tools, provider status, vault note, digest message. They now lead with the
  in-chat connect (connect_aws / connect_gcp / connect_azure) and offer
  'uvx nable' as the terminal alternative. No more "restart your AI client."
- The connect hint no longer claims "this is sample data" when it rides on an
  error response with no data attached.
- Dashboard (server_web) empty-state command modernized to 'uvx nable'.

## 0.8.147

Whole-org AWS onboarding in one StackSet, not one grant per account.

- New org StackSet template (iam_setup.org_stackset_template, rendered to
  templates/aws-org-finops-role.yaml): a strictly read-only role deployed to every
  member account at once via a service-managed StackSet, trusting the management
  account. Same least-privilege action set as the single-account role, no write or
  AssumeRole permissions. Auto-deploys to future accounts too.
- finops setup aws --org rebuilt: identifies the payer account, writes the template
  locally, prints the exact create-stack-set / create-stack-instances commands (with
  your account id and org root filled in), then discovers and registers every account
  and verifies the role actually assumes in one, so a missing or still-provisioning
  role surfaces immediately instead of as silent empty scans.
- Reminder in the flow: org-wide COST needs none of this (the payer's Cost Explorer
  already sees every linked account); the role is only for per-account resource scans.

## 0.8.146

First-run activation: catch SSO creds, and stop defaulting to "skip."

PostHog showed the wizard is the wall (199 open it, ~45 finish, 4 connect). Two
first-run fixes, both in the welcome flow:

- Ambient AWS probe timeout raised from 3s to 6s. 3s was too tight for an AWS SSO
  token refresh, so real credentials read as "none" and the user fell through to
  the connect menu instead of seeing their real bill. 6s catches SSO while staying
  bounded (the "Checking..." line is on screen, so it is not a silent hang).
- The no-ambient-creds connect menu no longer defaults to "Skip for now." Pressing
  Enter now takes the AWS connect path; skipping is a deliberate choice. The old
  default made not-connecting the path of least resistance.

No fake data was added: the flow still leads with a real ambient scan and refuses
to show invented numbers.

## 0.8.145

Activate Pro in the editor, no terminal, no restart.

- New activate_pro tool: paste your license key from the receipt email into Claude
  or Cursor and say "activate my license." nable validates it offline, stores it
  locally, and unlocks Pro in the same session immediately. This works because the
  tool runs inside the running MCP server process: store_license clears the cached
  status and get_status re-reads, so the paid plan is live on the next call with no
  editor restart. The old flow (activate in a separate terminal, then restart)
  couldn't do that. The license is still the offline signed key, so the local-first
  no-egress promise is intact.
- Receipt email now leads with the in-editor path (paste the key, ask nable to
  activate), with the terminal commands as the alternative.

## 0.8.144

Green CI and a cleaner README.

- Fix a flaky-on-CI test: the two heartbeat-surface tests asserted on a patched
  module-global `ping`, which cross-test ordering could leave pointing elsewhere
  on Linux CI (passed locally, failed on CI). Extracted the surface logic into a
  pure `_startup_surface()` and test that directly, no telemetry-send dependency.
  Behavior unchanged.
- README: tighter, cleaner intro and description. Same claims, less clutter.

## 0.8.143

Fix the real issue behind 0.8.142: connect_azure no longer routes the Azure
secret through the model.

- connect_azure used to take the Cloud Shell output (which contains the Azure
  client secret) as a tool argument. For the model to fill that argument the
  secret has to be pasted into the chat, which sends it to the model provider and
  any MCP intermediary, breaking nable's local-first, no-egress promise. 0.8.142
  only stopped the secret echoing in a local error string; it missed the actual
  exposure.
- connect_azure now takes no arguments and never accepts the secret. It returns
  the Cloud Shell script and directs you to finish the connect in your own
  terminal with `finops setup azure`, which encrypts the secret into your local
  vault. The model never sees it. It also reports when Azure is already connected.
- connect_aws and connect_gcp were already correct here: they read credentials
  already on the machine, so nothing sensitive ever passes through the model.

## 0.8.142

Security hardening from a scoped audit of the new connect tools.

- connect_azure no longer interpolates a raw store exception into the response.
  That path handles the Azure client secret, so it now returns the exception type
  only, never a raw message that could echo the secret back. No leak was found
  (the audit log never records tool arguments and telemetry sends only the error
  type), this closes the theoretical echo path as defense in depth.

## 0.8.141

Connect any provider in-chat, and start measuring the "ran it but never used it" cliff.

- connect_gcp: in-client Google Cloud connect, same detect-then-confirm shape as
  connect_aws. No-arg call lists the open billing accounts your local gcloud /
  GOOGLE_APPLICATION_CREDENTIALS can see; billing_account_id connects one and
  auto-discovers the BigQuery export. Local-only, never mutates GCP.
- connect_azure: Azure has no local ambient credentials, so this returns the
  Azure Cloud Shell one-paste script (creates a read-only service principal in
  your own tenant) and connects when you paste the single line it prints back.
  nable never runs the script; you run it in your Cloud Shell.
- The unconnected-cost-tool hint now points at all three connect tools, not just
  AWS.
- Heartbeat now carries a 'surface' tag (cli vs mcp_server) so the machines that
  start nable but never call a tool can be split into "ran the CLI once, never
  wired it in" vs "wired into a client, never asked it anything." The fixes
  differ, so we measure before guessing.
- The MCP server now tells the model to proactively offer nable when the user
  mentions a cloud bill or cost, and to offer an in-chat connect when nothing is
  connected, targeting the wired-but-never-used segment.

## 0.8.140

Connect an AWS account from inside your MCP client, no terminal.

The biggest activation leak was that connecting a cloud account required leaving
Claude/Cursor for the terminal wizard, and until you connected, cost tools quietly
returned sample data that looked real, so people never knew to connect.

- New connect_aws tool: detect-then-confirm and local-only. Call it with no
  arguments to see the AWS accounts whose credentials already exist on this
  machine (verified via STS, nothing stored); call it again with account_id to
  connect one. It never creates, modifies, or deletes anything in AWS, and
  credentials stay local.
- Cost tools now flag sample data. When no account is connected, every cost tool
  adds a clear "this is sample data, call connect_aws to see your real numbers"
  hint instead of passing off the demo dataset as real, and records the event
  once per session so the "used a tool but never connected" wall is visible in
  the funnel.
- A named-profile or role connect now correctly counts as connected (it writes
  accounts.yaml but sets no credential env var), so demo data steps aside the
  moment you connect, not only on an access-key connect.

## 0.8.139

More of the OpenCost wiring.

- get_kubernetes_namespace_breakdown now flows through OpenCost when it is
  connected (it already delegated to get_kubernetes_costs); the OpenCost path
  now honors the namespace filter instead of returning every namespace.
- New connect_opencost tool: tells you whether nable is reading real-rate costs
  from OpenCost, and if not, gives the exact copy-paste steps to deploy OpenCost
  and point nable at it. Propose-only: it returns instructions and reads the
  OpenCost API, it never deploys or changes anything in your cluster.

## 0.8.138

OpenCost as a Kubernetes cost source. When you run OpenCost (the CNCF project)
in your cluster and set NABLE_OPENCOST_URL, nable reads its Allocation API and
returns real-rate cost per namespace, including GPU, network, and PV storage,
the things nable's built-in list-price allocator does not price. OpenCost is a
data source here, like Cost Explorer or Datadog; nable adds the agent interface,
remediation, and the verify loop on top, and only ever reads OpenCost.

When OpenCost is not set or not reachable, get_kubernetes_costs falls back to the
built-in zero-setup estimate, now tagged is_estimate with a note that it is list
price and does not cover GPU. So you keep "works with no install" and get real
numbers when OpenCost is present, and the response always says which one you got.

Never fabricate a dollar for a resource we cannot price. Two paths used to emit
a made-up $0.10/hr when an instance type was missing from the price table, which
on a large or GPU instance is off by 100x, and a wrong number that looks real is
worse than an honest "cost unknown".

- idle RDS: an unknown DB class is now left unpriced (savings null, flagged
  unpriced, severity unknown) instead of costed at $0.10/hr. The idle signal
  (no connections) is measured and still surfaces; get_idle_rds_instances counts
  unpriced instances and marks the total a floor.
- Kubernetes node cost: an unknown node type is now excluded and reported under
  unpriced_nodes with cost_is_floor set, instead of silently costed at $0.10/hr.
  Added modern GPU and accelerator on-demand prices (p5/p4de, g5 full family, g6,
  g6e, Trainium, Inferentia) so the common case is priced, not defaulted.

Pre-deploy estimators (terraform_estimate, PR estimator) are unchanged: list
price with fallbacks is correct for hypotheticals, where there is no real bill.

Richer per-account learning signal, so the recommend-and-learn loop has more to
compound on as real accounts use it.

- Approval profile: nable now learns the dollar size and resource types you
  actually act on versus dismiss, not just how often per source. It exposes an
  approval_floor_usd (the 25th percentile of what you have acted on) so a
  recommendation far below what you normally approve can rank lower for you.
  Sparse-data safe: the floor stays unset until there are enough decisions, and
  business-reason dismissals are excluded. Surfaced in get_recommendation_learning.
- Stronger dismiss capture: dismiss_recommendation now nudges for the reason (it
  is what the learning loop runs on), echoes the categorized reason, and tells you
  when a business reason will not be held against that recommendation type. A
  dismissal with no reason gets a gentle nudge to add one.

Still propose-only and single-tenant: this only produces a signal a ranker reads,
it never mutates anything, and it reads only this install's own ledger.

Closes the verify-and-learn loop, the part that turns "here is a list of
maybes" into proven, banked savings.

- Learning now ranks recommendations. get_savings_summary and
  run_full_cost_audit read the per-account track record (how often a
  recommendation type actually gets acted on and how close its predicted
  savings landed to the measured result) and demote or annotate low-yield
  sources, leading with what has paid off on your account. It stays
  propose-only: ordering and annotation change, spend numbers never do, and
  nothing is hidden. On a cold ledger it degrades to plain dollar ordering.
- Verifiers broadened past EC2 rightsizing and idle cleanup: added a
  read-only Graviton verifier (confirms the instance moved to an arm64
  family) and recorded Graviton scans to the ledger so they can be banked.
- Verified banked savings is now the hero number in get_nable_roi and
  verify_savings, the money that actually left the bill, distinct from
  predicted. Fixed an accuracy bug where a verified row missing its measured
  value counted its estimate as banked, overstating the figure.

Also lands the first piece of the watchdog: a read-only spend x utilization
correlator that ranks underutilized resources and prepares (never executes)
the fix. It is dormant foundation, nothing schedules it or pushes yet, and
there is no auto-execute path. Design in internal/watchdog-design.md.

Two things: pricing-name coherence and scan speed.

Pricing: the $25 tier is now called "Pro" everywhere. The product still had
~20 in-app strings (nudges, license errors, docstrings) plus several website
pages calling it "Team plan" while the license and main pricing page said
"Pro." Same price, two names. All unified to Pro; the $1,000 tier's stray
"Team" references corrected to "Startups." No price or model change, only the
name. (The organizational "team" concept, cost-by-team, team scorecards,
shared-Postgres "team mode", is untouched.)

Speed: five scanners that walked AWS regions one at a time now scan them
concurrently, so a multi-region account pays the slowest region instead of the
sum of all of them. Covers Graviton, RDS manual snapshots, EBS snapshot
replication, non-prod scheduling, and the idle-resource scan that runs during
onboarding's first-value moment (it was hitting its 10s cap and showing partial
data on wide accounts). Graviton also stops re-calling STS once per region.

## 0.8.133

Fix from a comprehensive security audit: resource names and tags (Kubernetes
namespaces/releases, EC2/EBS Name tags) are set by whoever has tag or deploy
access in a shared account or cluster, not by nable. They were spliced
unescaped into Jira/Linear/GitHub ticket titles and bodies, and returned
uncapped in tool output, a prompt-injection vector if that text is later
read back into an LLM context. Now stripped of control characters, capped
at 256 characters, and ticket bodies that quote resource metadata carry an
explicit note that it's data, not instructions.

## 0.8.132

`run_full_cost_audit` was missing the two waste categories a plain EC2+RDS
account is most likely to actually have: idle/orphaned resources (unattached
EBS volumes, unused Elastic IPs, old snapshots, stopped EC2, idle load
balancers) and idle RDS instances with no connections in 14 days. A bare
account running "find waste" got a report built from Graviton, Lambda,
S3, Textract, and similar scanners that legitimately found nothing, with no
signal that the two most basic categories were never in the sweep. Both are
now part of the full audit.

## 0.8.131

Fixes from an independent dogfooding review: the Team-plan upgrade nudge
that cites real savings found so far is now prefixed so it never reads as
an audit finding. The Bedrock model-routing tool now returns a top-level
`savings_ceiling_caveat` next to its headline savings number, since a
price-ratio ceiling sitting unqualified next to real dollar figures invited
treating it as a firm plan. `list_idle_resources` now says up front, in
both the docstring and the response, that it only covers 5 resource types
and points to the tools that cover RDS, DocumentDB, Kendra, and Textract,
so a clean scan there isn't mistaken for a clean account. The onboarding
budget save also now logs the real exception on failure instead of
swallowing it silently.

## 0.8.130

Polished the setup flow's visual language: the welcome banner's color
palette (bold/dim/status colors, a fixed-width rule) now carries through
every prompt, not just the banner, and the "3 steps" status column lines
up on the same character offset for every row instead of drifting per label
length.

## 0.8.129

Azure connect now matches AWS and GCP: `finops setup azure` opens Cloud Shell
(already signed in, no local az CLI needed), you paste one generated script
in, and paste one line back. It creates a read-only service principal and
assigns Cost Management Reader, Reader, and Monitoring Reader across every
subscription automatically, replacing three manual `az role assignment`
commands per subscription. Manual service-principal entry and device-code
flow are still available as fallbacks.

## 0.8.128

Fixes MCP Registry publishing, which had silently failed since 0.8.125: the
server.json description exceeded the registry's 100-character limit, so the
registry froze at 0.8.124 while PyPI kept releasing. Description shortened, a
test now guards the limit, and this release catches the registry back up.

## 0.8.127

audit_duplicate_spend now also flags Databricks + Snowflake running at once,
a third cluster alongside LLM inference paths and managed search. Also fixes
a real bug: an invalid provider name passed to get_cost_summary silently
returned every connected provider's spend instead of an error; it now errors
cleanly.

## 0.8.126

New tool: audit_duplicate_spend. Flags spend on two services or providers
doing the same job at once, the kind of waste a plain cost breakdown never
surfaces because every line item looks legitimate alone. Covers duplicate LLM
inference paths (e.g. AWS Bedrock Claude plus a direct Anthropic API key) and
duplicate managed search/retrieval (Kendra plus OpenSearch). Always a
"worth a look" flag, never a precise savings claim.

## 0.8.125

The AWS one-click connect (no local credentials needed) now asks for a single
pasted value instead of two. The CloudFormation stack's Outputs tab has a new
NableSetupPaste entry combining the access key and secret; the setup wizard
accepts it as one paste and falls back to the original two-field entry only if
it doesn't parse. The MCP registry listing also got a fuller description and
an icon.

## 0.8.124

nable never touches the OS keychain when FINOPS_NO_KEYRING=1 is set (also
implied by FINOPS_AIRGAP=1). Use it in CI, tests, demos and any ephemeral
environment: the vault master key and trial state resolve from files only, so
macOS never shows a keychain password prompt and your real recovery key is
never read or rewritten by a scratch run. The test suite now ships with a
global in-memory keyring stub, so developing nable itself no longer prompts
either.

## 0.8.123

Role-based access now reaches SSO sessions on hosted instances. Map your IdP
groups to nable roles with FINOPS_SSO_ADMIN_GROUPS, FINOPS_SSO_ANALYST_GROUPS,
and FINOPS_SSO_VIEWER_GROUPS, and the dashboard agent enforces each session's
ceiling: an analyst login cannot run admin-only tools. Fully backward
compatible: with no mapping configured, SSO behavior is unchanged, so upgrading
cannot lock anyone out. FINOPS_SSO_GROUPS_CLAIM and FINOPS_SSO_DEFAULT_ROLE
tune the claim name and the unmapped-user role.

## 0.8.122

nable now tells you when you are running an old build. A capped, airgap-aware
version check surfaces one line in the welcome flow and the server startup log
when a newer release exists, ending a silent-staleness trap where the uvx
launcher could resolve a five-week-old build on Python 3.10 machines (the
launcher package is fixed too; it now requires Python 3.11+ like the product).
Also fixes a scheduler lock collision where two instances with different
databases on one machine shared a lock and one silently stopped sending
digests and anomaly alerts.

## 0.8.121

The agent guardrail got a hardening pass. It now catches destroy hidden behind
the apply verb (terraform apply -destroy), pulumi destroy, eksctl delete, and
S3/GCS bucket wipes (aws s3 rb/rm, gsutil rm), all one-way doors that
previously slipped through. The shell hook and the check_action_policy tool
now agree: plain terraform apply is allowed by default in both, with budget
escalation intact, where the tool previously blocked what the hook allowed.
And finops guard install writes a hook command that actually exists for uvx
installs instead of failing on every agent command. Clearer errors on
malformed terraform plans, and guard status now points non-Claude-Code agents
at the check_action_policy tool.

## 0.8.120

Faster to value, lighter in your context window. Anomaly detection now works on
day one: nable seeds its baselines from your Cost Explorer history instead of
waiting a week for snapshots. After your first bill renders, nable suggests
three questions tailored to your account's actual shape. A new
nable_setup_status tool lets your AI agent finish your setup for you, without a
secret ever passing through chat. And 27 rarely-used tools no longer load by
default, cutting roughly 25k tokens of tool definitions from every session
(their capabilities remain in the full audit; FINOPS_ALL_TOOLS=1 restores
them).

## 0.8.119

Hosting credits are wired end to end on the software side. A Stripe credit
purchase now records a grant on your customer record, and the new `finops
credits` command applies it to the instance: grants stack within the month,
expire at the period roll, and arm the managed-AI meter on their own. The
dashboard warns in the chat itself once 80% of the month's allowance is used,
so the stop at zero is never a surprise. Upgrade links from in-product nudges
now carry attribution tags so we can see which moments actually convert.

## 0.8.118

Numbers you can defend. The Textract non-prod finding now headlines the
conservative floor of its savings range instead of the best case, with the full
range stated alongside, and that same floor flows through the dashboard, the
savings ledger, and upgrade math. Slack/Teams alerts now page on cost spikes;
routine cost drops are recorded and queryable but no longer page (a
high-severity drop still does, since something may have stopped running;
FINOPS_ALERT_DROPS tunes this). Anomaly summaries separate spikes from drops.
And scan_waste_patterns, benchmark_costs, and forecast_costs no longer require
an account id; nable resolves the connected account automatically.

## 0.8.117

Every tool is now fully documented. All 183 MCP tools describe their parameters
and include example queries, which helps your AI client pick the right tool and
fill its arguments correctly, and completes the description-quality pass started
in 0.8.116. A CI check now blocks any new tool from shipping with a bare
description. The web tool reference is regenerated from the improved docs.

## 0.8.116

Audits got twice as cheap to read. Waste-audit findings carried repeated
boilerplate (the same why/remediation on every resource, empty fields, a
pricing note per finding); the guidance now lives once per category in a
playbooks map and empty fields are dropped, halving tokens per finding, so the
same response budget returns roughly twice the findings. Tool descriptions for
several housekeeping tools were rewritten, with a test that stops new tools
shipping bare ones. And upgrade nudges now cite the deduplicated savings
ledger, so the ROI number in the pitch matches the one in the report.

## 0.8.115

Multi-provider cost answers stay lean as you scale. Cross-provider queries used to
inline every service of every provider, so at ~18 connected providers a single
answer ballooned toward 5k tokens. Now each provider shows its top services and
rolls the rest into a total, cutting the payload ~40% at 18 providers with totals
unchanged. Latency was already flat (the provider fan-out runs concurrently);
this fixes the token cost. Single-provider queries keep full detail.

## 0.8.114

Removed GitHub and PagerDuty as cost connectors. They report usage, not dollars,
so they always showed \$0 and added a line that looked broken rather than any real
cost signal. GitHub ticketing and PR comments are unaffected (separate feature),
and you can still capture GitHub/PagerDuty spend from their invoice emails via
fetch_invoice_emails.

## 0.8.113

Cleaner cost categories and clearer connect-time expectations. Observability
tools (Datadog, New Relic, PagerDuty) and developer tools (GitHub) now get their
own FOCUS service categories instead of landing in a catch-all "Other". And the
providers that report usage rather than dollars now say so when you connect them:
PagerDuty and GitHub explain where the dollar figure comes from, and New Relic
prompts for your contract rates so it can show real dollars instead of $0.

## 0.8.112

Cross-cloud commitment reporting now catches GCP. The FOCUS normalizer detected
AWS Savings Plans and Azure reservations but missed GCP committed-use discounts
whenever the billing export omitted the coded credit type. It now reads the
credit name too, so a GCP CUD is labeled as reliably as the other two and shows
up when you group spend by commitment. The saving already reached effective cost;
this fixes the label.

## 0.8.111

Faster on big orgs. Tag attribution compiles its rules once instead of re-sorting
them for every cost line, and the AWS Organizations reporting path (org rollup,
top-spending accounts, account anomalies, weekly digest) is now read-through
cached like the rest of the cost data. Previously those reporting calls re-hit
Cost Explorer every time, so one session could pay for the same org query several
times over. No behavior change, just fewer API calls and less repeated work.

## 0.8.110

GCP goes deeper. New `get_gcp_recommendations` pulls Google's native Recommender
API: machine-type rightsizing, committed-use-discount buys, Cloud SQL
idle/overprovisioned, Cloud Run cost tuning, and idle VMs/disks/IPs/images,
priced against your real SKU rates instead of list-price estimates. It
complements the resource scanner (`audit_gcp_waste`): the scanner covers you on
day one with read scope; the Recommender API adds depth once Google has ~8 days
of usage. Findings sort by monthly savings and follow the same propose-only
trust model. Needs the Recommender API enabled and `roles/recommender.viewer`.

## 0.8.109

Connect everything in minutes. New `finops connect` scans this machine for
provider credentials (environment, gcloud, gh, ~/.modal.toml,
~/.databrickscfg) and connects them all in one keystroke. GCP setup is now
detect-and-confirm like AWS: it finds gcloud/ADC credentials, discovers your
billing accounts and BigQuery export automatically, no service-account JSON
scavenger hunt. Every paste-a-key wizard now links the exact page where the
key lives, and the welcome flow offers whatever else is on your machine
right after your first bill renders.

## 0.8.108

Accuracy fixes from a platform audit: the savings ledger no longer
double-counts recommendations (the "potential savings" total was inflated
by legacy duplicate rows), cost summaries no longer mislabel an active
account as zero-spend, forecast_costs works without an explicit account id,
and upgrade copy says "$25/mo flat" not "per seat".

## 0.8.107

Pro is now $25/mo ($250/yr, two months free), down from $100. New checkout
links and every price surface updated; Startups is unchanged.

## 0.8.106

The guardrail installs itself, and the docs finally show all 182 tools.

- `finops guard install`: a Claude Code hook that auto-checks every
  infra-mutating command (terraform destroy, kubectl delete,
  terminate-instances, commitment purchases) against your policy before the
  agent runs it. Ask on one-way doors, deny out-of-allowlist actions, silent
  on everything else. Advisory only; nable never executes.
- Tool reference: getnable.com/tools.html lists all 182 MCP tools, searchable,
  generated from source on every release so it cannot drift.
- Terminal URLs (one-click AWS key, docs) are now real clickable hyperlinks
  in iTerm2, VS Code, kitty, and WezTerm.

## 0.8.105

Real data or nothing: the first run never invents numbers.

### Changed
- **The guided onboarding no longer falls back to a sample bill.** Before, a user
  who skipped the credential step, or whose scan came up empty, was shown nable on
  made-up example numbers. Now that path shows an honest empty state ("No numbers
  yet, on purpose") and the fastest way to a real one: the one-click read-only AWS
  key when it is published, plus `finops setup aws` / `finops setup openai`. No toy
  figures ever land in front of someone evaluating the product. `finops welcome
  --demo` still exists as an explicit, clearly-labeled sample walkthrough for
  anyone who wants it.

## 0.8.104

Smarter first run: setup now ends by offering a budget your agents respect.

### Added
- **Post-scan budget step in onboarding.** After the first-run value moment shows
  a real spend number, nable offers to set a monthly budget seeded from that
  number (about 15% headroom, rounded to a clean figure). It heads off the
  find-out-the-hard-way bill and, more to the point, sets the number every agent
  checks against before it acts. The closing line points at `check_action_policy`
  and `check_budget_status`, so an agent builder sees the spend-control story on
  day one instead of in a docs page. Interactive only, skippable with `n`, and it
  never prompts a returning user who already has a total budget.

## 0.8.103

First-run diagnostic fixes: the value moment no longer hangs, and the package metadata is honest again.

### Fixed
- **The first-run value moment no longer hangs.** The headline spend number was
  computed in under a second but printed only after three optional scans (idle
  resources, AI-spend plan, LLM bill) that ran serially with wall-clock caps
  summing to ~45s. A real new user could stare at "Scanning..." for up to ~40s.
  The headline now prints immediately, and the optional scans run concurrently
  (added wait is the slowest single scan, ~10s, not their sum). Demo mode hid
  this, which is why it kept slipping past.
- **`finops doctor` no longer reads the OS keychain on every run.** It now
  resolves the vault master key in the vault's own order (env, then the 0600
  `vault.key` file, then the keyring), so on a file-first install it reports the
  key correctly without touching (or, on macOS, prompting for) the keychain.
- **Friendlier Snowflake missing-dependency error.** A slim install now gets
  "Run: pip install 'finops-mcp[snowflake]'" instead of a raw ModuleNotFoundError,
  and the setup wizard folds the `snowflake` extra into the pinned launch command
  when Snowflake is connected.
- **`__version__` was stale (0.8.101) while the package was 0.8.102.** Now in sync.
- Retired "plain English" from the first-run wizard screen, the finance persona,
  and the dashboard sandbox (the earlier scrub was web-only).
- Corrected tool count (165/160+ to 180+) and plugin version (0.8.77) in the
  Claude plugin and marketplace manifests, fixed a description typo, and replaced
  the inaccurate "credentials stay in your OS keychain" with "on your machine."

## 0.8.102

The keychain-prompt fix and the FOCUS long tail: one normalized cost dataset across clouds, SaaS and AI.

### Fixed
- **No more recurring macOS credential prompts.** Every license check used to
  read the trial date from the OS keychain and rewrite it, which recreated the
  item, reset its ACL, and made macOS re-prompt on every session ("python wants
  to access system.cache.prefs"). The signed trial file is now the primary
  store and the keychain is written exactly once, at creation. The vault master
  key is likewise resolved from a 0600 key file first, with the keyring as the
  durable recovery copy, so version upgrades no longer prompt either. Set
  `FINOPS_VAULT_KEYCHAIN_ONLY=1` to keep the key exclusively in the keychain.
  The trial keychain entry is renamed to an honest `nable-trial` and migrates
  automatically.

### Added
- **FOCUS 2.0 across the usage-based long tail.** Snowflake, Datadog, MongoDB
  Atlas, New Relic, Databricks, Vercel, Langfuse, Cloudflare, PagerDuty, GitHub
  and Twilio now normalize into the same FOCUS 2.0 records as AWS, Azure and
  GCP, via one generic translator. `get_focus_costs` and `slice_costs` query
  all of them in a single shape.
- **LLM/AI spend in the unified dataset.** OpenAI, Anthropic, OpenRouter and
  LiteLLM spend joins the FOCUS dataset, one record per model with token counts
  and request volume preserved as tags. Bedrock and Vertex are excluded from
  the merge since they already arrive through the AWS and GCP exports, so
  nothing is double-counted. Filter with `provider="openai"` or `provider="ai"`.
- **Agent cost-control gate.** Agents can call `check_action_policy` and
  `estimate_change_cost` before applying a change, getting an allow, block or
  escalate answer against your budgets and policies plus a cheaper path when
  one exists.

### Hardened
- FOCUS translators degrade safely on hostile provider data: non-finite costs
  are clamped, malformed gateway responses drop fields instead of records, and
  internal per-provider payloads no longer leak into `get_llm_costs` responses.

## 0.8.101

Hardening pass: credentials at rest, role enforcement on hosted boxes, and a safer fleet and build pipeline.

### Security
- **The vault master key can live off the data volume.** Hosted deployments can
  now supply `FINOPS_VAULT_KEY` from the host environment, so the Fernet key no
  longer has to sit in the same `/data` volume as the encrypted `vault.db`. A
  leaked or snapshotted data volume is then useless on its own.
  `deploy/provision-tenant.sh` generates one per tenant. Existing boxes are
  unaffected: a blank key keeps the previous self-managed key-file behavior.
- **Role enforcement now works on a single-tenant box.** `require_role` honors an
  explicitly attached identity even in permissive SQLite mode, and defaults to
  admin when none is attached, so the box owner can never be locked out. The
  dashboard agent attaches the session's role, and a control-plane analyst login
  gets a real analyst ceiling instead of silent admin.
- **Public image hardening.** `.dockerignore` now excludes `*.db`, `*.key`,
  `*.pem`, `.finops`, and `tenants/`, so no local vault or tenant secret can be
  baked into the published image.

### Fixed
- **Fleet updates fail loudly on a misconfigured fleet.** `deploy/fleet-update.sh`
  preflights that at least one tagged box is reachable via SSM and errors instead
  of dispatching a silent no-op when the per-box role or tag is missing.

### Added
- **CI guard against stale web bundles.** A new workflow rebuilds `web/app.js` and
  `web/tweaks-panel.js` from their JSX with a pinned esbuild and fails if the
  committed bundle is out of date. The pre-commit hook is pinned to the same
  version so the two never disagree.

## 0.8.100

The OS keychain is read once per process, not every few minutes.

### Fixed
- **No more repeated macOS Keychain prompts.** `Vault.default()` and the license
  trial check read the OS keychain on every call, so a long-running server
  re-read it constantly and macOS re-prompted ("python wants to use your
  confidential information") every few minutes. Both now cache the master key and
  the trial date in-process and read the keychain at most once per session. Click
  "Always Allow" on the first prompt and it stays quiet.

## 0.8.99

Audit fixes: tool loading, share links, doctor, and the serve dashboard.

### Fixed
- **MCP tool loading.** The startup banner printed to stdout on the server path,
  the same channel as the JSON-RPC handshake, so a strict MCP client could load
  zero nable tools. The banner now goes to stderr and stdout stays clean.
- **`finops serve` showed $0 for Azure, GCP, and SaaS.** The serve process never
  hydrated the vault into the environment, so providers that read credentials
  from env looked disconnected. It now hydrates and uses the full connector set.
- **Read-only share links are read-only again.** The `/view` flag was injected
  after the page scripts ran, so the "View only" badge never showed and the Ask
  composer stayed live. It is injected in the head now, before the scripts run.
- **`finops doctor` no longer reports "Cost Explorer: ✓" on a real error.** A
  throttle, an expired token, or a network failure was treated as healthy. It
  now reports "could not verify" and never green on an unconfirmed error.
- **The Bedrock cost probe can't hang.** The STS auth check had no timeout; it
  now bounds connect and read and skips Bedrock cleanly on failure.

### Changed
- **A legible "no AI key" message.** The dashboard Ask and the Slack bot now say
  to set `ANTHROPIC_API_KEY` and where to get one, instead of a vague guess.
- **Airgap covers the AI assistant.** `FINOPS_AIRGAP=1` now disables the agent
  (which would otherwise send query results to Anthropic), and SECURITY.md names
  the two egress paths (the assistant and anonymous telemetry) directly.
- **Dashboard polish.** A fresh account reads "not scored" instead of a false
  "healthy", "New view" pre-fills an editable prompt instead of auto-sending,
  and a long answer shows a "still working" note instead of static dots.
- Server error responses (SSO, `/api/data`, mark-done) are generic to the client
  and logged in full server-side, no raw exceptions or account identifiers.
- Docs now state Python 3.11+ (the package requires 3.11), not 3.10.

## 0.8.98

The clean dashboard redesign goes live.

### Dashboard
- **The redesign is now the dashboard, not a mockup.** A left sidebar carries the
  workspace: Dashboards (Cost overview, New view), an Agents group (cost investigation,
  rightsizing, idle cleanup, anomalies, commitments), chat History, and a single-tenant
  security card. The main view opens on a greeting that leads with what nable found
  ("nable found $X/mo to save"), icon-tile metric cards with trend pills, a spend-trend
  chart beside an Ask panel, and a savings-opportunities table.
- **A favicon.** The browser tab showed a generic globe; it now carries the nable mark.
- The agents, the Ask-panel suggestion chips, and New view each open a focused Ask
  session. Review on an opportunity opens it in the agent to draft the change.
- All of it rides the real /api/data wiring: charts, scorecard, opportunities, pinned
  views, and in-session history are unchanged underneath the new design.

## 0.8.97

The dashboard, rebuilt around the agent.

### Dashboard
- **Opportunities-first opening.** Leads with what nable found ("Savings nable found
  $X/mo") and an honest realized counter, instead of a raw cost table. Realized is $0
  on day one, so the opportunity is the value shown first.
- **The sidebar is the AI spine.** A Workspace group, an Agents group (cost
  investigation, rightsizing, idle cleanup, anomalies, commitments) that each launch a
  focused Ask session, and a chat History group.
- **Onboarding checklist** that auto-checks what's already set up (provider connected,
  business context) and hands the rest to the agent, collapsed to a one-line nudge.
- **Cleaner craft.** Surfaced metric cards with icon tiles, one tight metric row
  instead of a sprawling grid, the left rail dropped, tighter panel spacing.
- Fixed a history-click bug that fired a brand-new chat instead of loading the question.
- The 0.8.96 logo edit reached only the fallback copy of the dashboard; it now lands in
  the served file (static/dashboard.html).

## 0.8.96

Faster answers, sharper cost accuracy, a security fix, and managed single-tenant hosting.

### Performance
- **The agent runs its tool calls in parallel.** A question that needs several cost
  lookups used to run them one after another, each a slow Cost Explorer round-trip.
  They now run concurrently, so multi-tool answers come back faster.
- **Cost data is cached to disk, not just memory.** A restart, a redeploy, or the
  next day's first query serves the prior fetch instead of re-hitting Cost Explorer.
  Best-effort: any disk problem falls back to a normal fetch, never to stale numbers.
- Concurrent fetch in the cost-per-commit path and parallel Cost Explorer calls in
  tag-coverage analysis.

### Cost accuracy
- **Bedrock no longer reports $0.** An exact-match SERVICE filter missed Bedrock's
  per-model line items; spend is attributed by service name now.
- **Textract waste no longer inflates the "unknown" bucket N times.** The per-tag
  breakdown resets its buckets correctly.
- Fixed a 30-day-sum bug in cost totals and a GitHub timestamp key mismatch.

### Agent
- **Spend questions get the number, not a waste audit.** "What did we spend this
  month?" now leads with the total and top services instead of pivoting to a savings
  pitch.

### Security
- **Blocked git-ref argument injection (RCE) in the PR tools** and hardened the GCP
  attribution SQL against unsanitized label keys.

### Hosting (managed single-tenant)
- **Closed the deploy gaps found in the first live single-tenant deploy** and pass
  the Anthropic key plus managed-AI budget through to the instance, so the hosted Ask
  tab works.
- Graceful shutdown on SIGTERM so a hosted box survives `docker stop` and restarts.
- Real icon and wordmark logo in the hosted dashboard topbar.

### Site
- Marketing-only front page with the demo front and center; Pricing and FAQ moved to
  /pricing. Centered, consistent layout, Architecture and Security collapsed into
  tabs, and a plain-language overview atop the docs.

### Fixes
- Side-effect-free `--help`, better `finops login` error handling, and genericized
  customer names in tool docstring examples.

## 0.8.95

Login-first activation everywhere.

### Activation
- **Pro-gated tools now point to `finops login`, not a license key.** When a free
  account hits a Pro feature in Claude or Cursor, the upgrade prompt reads "already
  subscribed? sign in: finops login" instead of asking for a key to paste.
- The post-purchase email leads with `finops login` (enter your email, paste the
  6-digit code), keeping manual key activation as a fallback.

## 0.8.94

One-step Pro activation, and managed-AI credits that reset monthly.

### Activation
- **New `finops login` (and `finops logout`): sign in by email, no license key to
  copy.** Enter the email you bought Pro with, paste the 6-digit code we send, and
  nable stores the license locally so the server picks it up automatically. The key
  never has to be copied, pasted, or remembered. `finops logout` removes it.
- `check_license` now reads the license stored by `finops login` from the local
  vault when `FINOPS_LICENSE_KEY` is unset; an explicit env var still wins (CI,
  power users). Validation stays fully offline against the bundled public key.

### Billing
- **Managed-AI credits are use-it-or-lose-it.** The monthly allowance no longer
  carries forward; the ledger resets each period. This backs the credit-based
  hosting add-on (Pro 500 credits, Startups 10,000), billed on top of the flat plan.

## 0.8.93

The policy-bounded guardrail (advisory): an agent can ask "should I apply this?"

### Agent-native
- **New `check_action_policy` tool: an advisory allow / block / escalate gate.**
  Describe a remediation action (rightsizing, idle_cleanup, purchase_commitment, and
  the like) plus the cost change, and nable checks it against a human-authored policy.
  Reversible, allowlisted, in-budget actions return `allow`; one-way doors (delete,
  terminate, buy a commitment) and over-budget changes return `escalate`; disallowed
  action types return `block`. It composes the cost preflight with the policy gate.
- **Advisory only, propose-only intact.** nable returns advice; a human approves and
  applies. It never auto-executes. The auto-execute relaxation (B2) is a separate,
  explicit decision and is deliberately not built.
- New `policy.py` (pure, tested): door classification (two-way vs one-way),
  `evaluate_action_gate`, and `load_policy` with env overrides
  (`FINOPS_POLICY_MAX_AUTO_USD`, `FINOPS_POLICY_ALLOWED_ACTIONS`). 16 new tests; full
  suite 1091 passed.

## 0.8.92

The agent-native on-ramp: a cost preflight agents call before they act.

### Agent-native
- **New `estimate_change_cost` tool: cost preflight with a machine verdict.** Call it
  before applying an infra change (a Terraform plan, a `helm diff`, or a known monthly
  delta) to get `ok` / `warn` / `over_budget` / `no_budget` plus the monthly and annual
  cost delta and the budget headroom. Read-only: it estimates and checks against your
  budget, it never applies anything. Wraps the existing Terraform/Helm estimators and
  the budget enforcer, and is the seed of the policy-bounded guardrail.
- **New `agent` persona for concise, structured output.** Automated callers get terse,
  machine-readable responses instead of human prose. Activate per-process with
  `FINOPS_PERSONA=agent` in the server env (env wins over the config file) or
  `finops config --persona agent`.

## 0.8.91

AI engineering report now works for teams that commit straight to main.

### AI unit economics
- **`get_ai_engineering_report` attributes commits, not just merged PRs.** It only
  looked at merged pull requests, so a repo that pushes directly to `main` (no PRs)
  showed zero AI output even with hundreds of model-authored commits. The model
  trailer ("Co-Authored-By: Claude Opus 4.8 ...") lives in the commit message, so
  commits attribute exactly like PRs do, by model, sized high/medium/low, joined to
  spend for a **cost per commit**.
- **New `unit` argument**: `"pr"`, `"commit"`, or `"auto"` (default). Auto uses PRs
  when the repo has any in the window, else commits, so PR-shops and commit-to-main
  shops both work. The unit actually used comes back in the report's `unit` field.
- Commit sizing uses the GitHub GraphQL history (message + additions/deletions +
  author in one call per 100 commits), so every commit is counted and cost-per-commit
  divides spend by the true count, not a truncated sample.

## 0.8.90

Onboarding fixes from launch-day dogfooding.

### Install
- **`uvx nable` no longer dies on a missing cryptography wheel.** Python 3.10 is the
  one supported version without a prebuilt `cryptography` wheel, so on a 3.10-default
  machine (an Anaconda base, an old system Python) uv fell back to compiling it from
  source and crashed on any arch or toolchain mismatch. Bump `requires-python` to
  `>=3.11` so uv resolves to a 3.11+ interpreter that has wheels. If Python downloads
  are disabled in your uv config, `uvx --python 3.12 nable` forces a good interpreter.

### Setup wizard
- **The closing message now matches the provider you just connected.** It was
  hardcoded to "ask what are my AWS costs" after every connector; connect GitHub and
  it now suggests "what has my AI coding shipped, by model?", connect Slack and it
  says alerts will post to Slack, and so on.
- **Next-step command hints adapt to how you launched nable.** `serve` and `setup`
  hints print as `uvx nable …` for a uvx run (no `finops` on PATH) and `finops …`
  for a pip install.

## 0.8.89

Onboarding seamlessness. The hero command finally launches the good flow, and the
first run can no longer crash or dead-end.

### Onboarding
- **`uvx nable` now runs the guided welcome flow**, not a persona quiz plus a
  26-provider menu. Bare `finops` routes to `finops welcome` (ambient-credential
  scan, value moment, never dead-ends); the full provider menu stays behind the
  explicit `finops setup`. The best onboarding was already built, new users just
  weren't being pointed at it.
- **The value moment can no longer crash the flow.** `_show_value_moment` now
  swallows a failed import (for example a broken or arch-mismatched native
  dependency) or any scan error and degrades to the demo number plus the setup
  close, instead of a raw traceback at the moment the user expects their first
  figure.
- **Next-step hints match how you launched nable.** A `uvx nable` run is
  ephemeral, so `finops doctor` would be "command not found". The welcome flow now
  prints `uvx nable doctor` for uvx users and `finops doctor` for pip installs.
- **No log noise on the first line.** The license status check dropped from INFO
  to debug, so onboarding no longer opens with `INFO License: ... license.py:436`
  before the banner.

### Release hygiene
- Sync `server.json` and the Claude Code plugin pin to the package version (they
  drifted at 0.8.88), so registry and plugin installs never point at a stale
  release. The suite fails if they drift again.

## 0.8.88

Tighten the free tier. Free was handing over the continuous, acting, cross-cloud,
forecasting, and AI-unit-economics surface: the whole product minus a few buttons.
This draws the pull/push line. Free answers when you ask. Pro runs for you.

### Licensing
- **Five new Pro gates** behind `require_pro`: `alerts` (proactive alert policies
  and scheduled push), `forecasting` (cost, Azure, and LLM projections),
  `ai_unit_economics` (cost per PR by model, AI KPIs, the GitHub
  engineering-attribution report), `remediation` (drafting rightsizing and
  terraform-tag PRs), and `cross_cloud` (the unified compare-providers and
  total-spend-all-sources view). 19 tools gate on these.
- **The first value moment stays free.** Cost queries, anomaly detection,
  rightsizing findings, single-provider views, and AI spend totals are never
  gated. Activation is the bottleneck, not generosity: free is connect, ask, see
  a real dollar figure; Pro is it keeps watching, acts, and unifies across clouds.
- **Demo mode unlocks everything.** `require_pro` short-circuits when `is_demo()`
  is true, so the product still demos in full to anyone evaluating it. Gating only
  bites real free-tier accounts.

## 0.8.87

Hardening from a full adversarial audit of the 0.8.86 work.

### Connectors
- **Anthropic Cost API never reports a truncated total as authoritative.** If
  pagination hits the page cap it falls back to the estimate (and logs it) rather
  than returning a partial sum as billed dollars. Undated cost buckets are skipped
  so the daily breakdown and the total stay consistent.

### AI KPIs
- **No more fabricated "F, no cache hits".** A dollars-only Cost API result (no
  token counts) now falls through to the honest "no token-level data" note instead
  of being graded as zero cache usage.

### Hosting and deploy (single-tenant)
- **docker-compose:** persist data into the mounted volume (`FINOPS_DATA_DIR=/data`,
  it was writing to the container home and losing everything on rebuild); bind the
  raw app port to loopback by default so only Caddy's 443 is public; map the
  operator-facing `FINOPS_DATABASE_URL` to the `DATABASE_URL` the app reads so
  team/RBAC mode actually engages.
- **server:** `FINOPS_REQUIRE_AUTH=1` now hard-forbids an unauthenticated dashboard
  even when the password is set to "off".
- **provisioning:** validate the slug/domain, force `chmod 600` on the tenant env
  on every run, pin the EC2 deploy to a release tag (not main HEAD), and document
  revoking the customer's cloud key on offboarding.

### Wizard
- Fix the AWS `provider_connected` telemetry that mislabeled the auth method after
  the one-click menu was renumbered.

## 0.8.86

Anthropic costs now come from the actual billed Cost API, not estimates.

### Connectors
- **Anthropic Cost API.** Anthropic released the organization Cost API
  (`/v1/organizations/cost_report`). nable now pulls actual billed dollars from it
  instead of estimating from token counts × list prices. It activates
  automatically when an Anthropic Admin key is configured (the setup wizard
  collects it) and falls back to the usage-based estimate otherwise. Token-based
  KPIs (cache hit rate, context-window use) keep working, enriched from the
  Usage API.

### Deploy
- **Single-tenant hosted provisioning (AWS EC2).** Hand-cranked runbook plus a
  helper to stand up a managed single-tenant instance per customer, with
  control-plane env wiring (`FINOPS_INSTANCE_ID`, `FINOPS_CONTROL_PLANE_SECRET`)
  for one-click login from the getnable.com account. See docs/PROVISIONING.md.

## 0.8.85

Easier AWS connect: one-click CloudFormation in the setup wizard.

### CLI
- **One-click read-only AWS key.** The wizard's AWS step now offers "One-click
  CloudFormation" alongside paste-a-key and SSO. It opens the AWS console with a
  read-only stack pre-loaded; you click Create, copy the access key from the
  stack Outputs, and paste it back. Collapses the credential step (the spot most
  people abandoned) from a dozen console clicks to two copy-pastes, and it works
  even with no AWS credentials configured locally. The wizard also records which
  connect method you pick (`aws_connect_method`) so we can see which one converts.

## 0.8.84

Fix: no more telemetry log noise in the setup wizard.

### CLI
- **Quieter `uvx nable` setup.** The interactive wizard silenced the AWS SDK and
  the scheduler but not the HTTP client, so `httpx` logged every anonymous
  telemetry POST ("HTTP Request: POST .../capture/") straight into the prompts,
  including on top of "Write config?". The wizard now lowers `httpx`/`httpcore`/
  `urllib3`/`posthog` to WARNING the way `finops welcome` already did. Telemetry
  is unchanged; only the stray log line is gone.

## 0.8.83

The dashboard, reborn: full-screen, true black, and it builds views for you.

### Dashboard
- **Full-screen, advanced-AI redesign.** The Ask tab now fills the screen like a
  real AI console (composer pinned at the bottom, a calm thinking state, crisp
  message rows). The palette is true black, dark-only, and the rigid card grid
  gives way to a flowing layout where panels float on the black with varied
  rhythm.
- **Ask to build a view.** Ask the cost console for a slice ("spend by team this
  quarter") and it pins a live card to your dashboard with no reload, plus a
  managed-AI usage meter per turn for credit billing.
- **Sleek copilot voice.** The chat (and the Slack bot) drop the emoji bullets,
  "TL;DR" headers, and em dashes for a sharp senior-analyst voice: severity in
  plain words, resource ids in backticks.

## 0.8.82

Fix: the dashboard Ask tab works on a default install.

### Dashboard
- **The in-browser cost copilot no longer needs an extra.** The Ask tab calls the
  Anthropic SDK directly, but `anthropic` was only in the `slack` and
  `pr-comments` extras, so a plain `uvx nable serve` returned "nable isn't fully
  set up yet" even with `ANTHROPIC_API_KEY` set. The SDK is now a core dependency
  (it is light: pure Python on httpx and pydantic, both already core), so the chat
  works out of the box once you pass your key.

## 0.8.81

The dashboard, rebuilt.

### Dashboard
- **Full visual redesign of `finops serve` / `nable serve`.** Rebuilt on the Cold
  Graphite design system: dark locked by default, Geist Mono tabular numbers as
  the hero of every metric, ice-blue accents, hairline borders, the proper radius
  scale. KPI cards, charts, the efficiency scorecard, and savings opportunities
  are now instrument-grade, and the Ask tab is a terminal-style cost console with
  a proposed-action card (Approve / Dismiss) instead of generic chat bubbles. All
  data binding is unchanged: this is a restyle, not a behavior change.

## 0.8.80

Fix: the visual dashboard was missing from pip and uvx installs.

### Dashboard
- **`finops serve` / `nable serve` now actually renders.** The bundled dashboard
  template (`static/dashboard.html`) was matched by an over-broad `.gitignore`
  rule, so hatchling's VCS file selection dropped it from the wheel. Every pip or
  uvx install got the fallback "Dashboard template missing" page instead of the
  dashboard. The template is now force-included in the wheel, with a build check
  so it cannot silently drop again.

## 0.8.79

Faster cold start.

### Install and onboarding
- **Slimmer default install.** The Azure and Google Cloud SDKs (and Google's
  grpcio/protobuf chain) are no longer core dependencies. They moved to
  `finops-mcp[azure]` and `finops-mcp[gcp]` extras, so an AWS-only install (the
  common path) downloads far fewer packages and the first cold `uvx` launch is
  much faster. The SDKs are imported lazily and provider detection is env-var
  only, so the AWS path never touches them. The setup wizard folds the right
  extra into the launch command automatically when you connect Azure or GCP, and
  `finops-mcp[all]` still pulls everything. Also synced the Claude Code plugin
  pin (it had lagged at 0.8.77).

## 0.8.78

One command to install.

### Install and onboarding
- **`uvx finops-mcp` is the whole command now.** Run it in a terminal and it
  launches the onboarding wizard; an MCP client (Claude Desktop, Cursor) running
  it over stdio still gets the server. Any subcommand routes to the CLI too, so
  `uvx finops-mcp setup`, `uvx finops-mcp doctor`, and `uvx finops-mcp welcome
  --demo` all work. No more `uvx --python 3.12 --from finops-mcp finops welcome`.
  The site, docs, and README now lead with the short command; the MCP config the
  wizard writes keeps its pinned managed Python for reproducibility.

## 0.8.77

Deep GCP audits, plus a round of security hardening.

### GCP
- **`audit_gcp_waste`.** First resource-level GCP audit: unattached persistent
  disks, reserved static IPs that are not in use, snapshots past an age
  threshold, and idle VMs (CPU joined from Cloud Monitoring). Findings come back
  sorted by estimated monthly savings with by-category, by-severity and
  by-project rollups. Set `GCP_PROJECT_IDS` (the setup wizard now prompts for it)
  to enable it; needs `roles/compute.viewer` and `roles/monitoring.viewer`.

### Security
- **SSO callback hardening.** Removed the unsigned-state escape hatch (state is
  always signature-verified now) and re-validate the post-login redirect to
  same-site paths, closing an account and license-key takeover via open redirect.
- **demo-ask fails closed** when Vercel KV is unset, so the live model never runs
  without a durable cross-instance cost cap.
- **Export path confinement.** `export_cost_report_csv` confines writes to your
  home or temp dir and refuses dotfile targets.
- **Supply chain.** Every GitHub Action is pinned to a commit SHA, `mcp-publisher`
  is pinned and SHA256-verified before it runs, and the trial clock signs with a
  dedicated key.

### Install and onboarding
- **Python 3.10+ is explicit now.** The README, SLACK.md, DEPLOY.md and the plugin
  and editor READMEs state the requirement and decode the cryptic
  `No matching distribution found` pip error (it means your Python is older than
  3.10). A stdlib preflight guard fails every console entry point with a clear
  message on an older interpreter, and `finops doctor` reports the running Python.
- **`finops serve` is secure by default.** It binds `127.0.0.1` instead of
  `0.0.0.0`, so a laptop run never opens a LAN port unprompted; add `--host 0.0.0.0`
  (still password-protected) to share it with your team. It flushes the
  auto-generated password before blocking, so it is visible under a pipe or process
  manager, and skips the LAN-IP probe on a local run.
- **The dashboard tells the truth.** A connected provider whose fetch fails (expired
  token, AccessDenied) now surfaces the error instead of showing a green badge over
  $0.
- **Slack bot tier surfaces before setup.** `finops setup slack` shows your
  Team/trial status up front (the conversational bot is Team-only, with a trial),
  checks the `slack_bolt` dependency instead of letting `finops-slack` crash, and
  every printed `finops-mcp[slack]` install command is quoted so it works in zsh.
- **Brand and font.** The dashboard and login page now use Bricolage Grotesque,
  self-hosted with no external font request, and the MCP server self-reports as
  `nable`.

## 0.8.76

Onboarding instrumentation and a faster ambient connect, aimed at the activation
wall (105 wizard starts, 5 provider connects). Now we can see exactly where people
drop, and the credential-detection step never hangs.

### Activation
- **Step-level funnel events.** The AWS connect flow now emits a `setup_step` event
  at each stage: connect opened, ambient probe done (with candidate count), ambient
  confirmed/declined, no-ambient-creds, manual opened, method selected, one-click
  offered, connect attempted, verify failed. The flow previously fired only start and
  success, so the ~95% who abandoned mid-wizard were invisible.
- **Failed manual connects now report.** A credential that fails verification emits
  `provider_connect_failed` instead of silently returning, so broken-key drop-offs are
  finally measurable.
- **Ambient detection is parallel and time-capped.** `_detect_aws_candidates` probed
  each AWS profile sequentially with multi-second timeouts and no global cap, so a
  machine with several profiles (or an expiring SSO token, or firewalled IMDS) stalled
  onboarding for 30s or more. Probes now run concurrently under a hard 4-second
  deadline, so detect-then-connect stays fast and never freezes. Named profiles are
  still preferred; the default chain is deduped against them.

Full suite: 829 passed.

## 0.8.75

The AI cost moat: optimize, forecast, and monitor token spend, including committed
contracts. Visibility is table stakes; this release moves nable to the layer where
the defensibility is. Plus a stack-tailored "what can you do?" so a connected user
actually discovers the 160+ tools.

### AI commitments & contracts
- **Reserved-Instance analysis for tokens.** A new commitment engine optimizes spend
  against committed AI contracts: prepaid credits, Azure OpenAI PTUs, AWS Bedrock
  Provisioned Throughput, and enterprise rate cards. For each it reports coverage,
  utilization, your effective $/Mtok versus on-demand, break-even, a right-size
  recommendation, and runway, priced against your actual negotiated terms, not list.
  With no contract configured it tells you whether your spend is high and stable
  enough to justify buying one. Tool: `get_llm_commitment_analysis`. Configure
  contracts via `FINOPS_AI_CONTRACTS` or `~/.finops-mcp/ai_contracts.json`, which
  stay on your machine.

### AI forecast & monitor
- **Token-spend forecasting with an exhaustion date.** Projects the AI bill,
  month-over-month growth, and the day your credits or commitment run out, using the
  per-account Holt-Winters forecaster. Tool: `forecast_llm_costs`.
- **Daily token-spend monitor.** Watches for spend spikes and commitment contracts
  needing attention (under-utilized capacity, enterprise minimum shortfall, expiring
  commitment), with a Slack alert and self-healing dedup. Tool: `get_ai_spend_monitor`,
  runs daily via the scheduler. Credits-to-cash stays with the existing credit job to
  avoid double-alerting.

### Discovery
- **"What can you do?" is now tailored to your stack.** It detects everything connected
  (cloud, SaaS, LLM providers, Kubernetes) and renders a capability map grouped by
  outcome, with the asks you'd actually type, the dollar impact, honest per-group tool
  counts, and the highest-value thing left to connect. Replaces a hardcoded AWS-centric
  list that missed most of the product.

### Copy
- The share card and the plugin marketplace listing no longer say "plain English"; the
  positioning is now "FinOps that lives in your AI editor."

Full suite: 822 passed.

## 0.8.74

A security pass (/cso) and a correctness pass (/debug) over the recent work, plus
onboarding polish so a connect-to-value run holds up for a real first user.

### Security
- **Removed `sts:AssumeRole` from the read-only connect key.** It's a privilege-
  escalation primitive, not a read: in an account with a role that trusts the
  account root (common), a holder of the "read-only" key could assume it and gain
  its permissions. The single-account connect never assumes a role, so it's gone,
  and `sts:Assume` is now in the over-privilege guard. The key is now strictly read.
- Corrected the CloudFormation Outputs wording: the secret access key is not "shown
  once," it persists in the stack Outputs; the description now says so and points to
  deleting the stack to revoke.

### Correctness (verified review findings)
- **Credit alarm no longer cries wolf at month start.** AWS posts promotional
  credits on a lag, so a healthy, fully-covered account looked uncovered early in
  the month and could fire a false "credits flipped to cash" alert. It now assesses
  the latest settled month, not the in-progress one.
- **OpenRouter window is end-inclusive.** It was dropping today's spend, and a
  single-day query returned empty. Also hardened the credits fallback against a
  malformed/null API response.
- **Context-window KPI no longer reports nonsense for Anthropic.** With no per-
  request data it was dividing whole-period tokens by 1 and labeling a thousands-
  of-percent figure "healthy"; it now says the data is unavailable, and Anthropic
  populates request counts when the API provides them.
- Fixed `model_sprawl` double-counting `o3-mini`/`o1-mini` as both cheap and
  expensive (substring match), a single-month credit query mislabeled as "no
  credits", a mislabeled `monthly_usd` on the blind-spot tool (now `window_usd`,
  it's a window sum), a dead success-log in the credit-check job, and a corrupt
  alert-dedup file that would re-send daily (now self-heals). Clamped a positive
  credit-clawback row so it can't produce negative coverage.

### Onboarding
- **The value moment shows the token bill, not just the cloud bill.** A user with
  both AWS and a model provider now sees their AI/LLM spend alongside cloud spend,
  the bigger number for an AI-native team and the thing no cloud dashboard shows.
- AI-native fast path: ambient `OPENAI_API_KEY`/`ANTHROPIC_API_KEY` offers an
  instant token-bill scan, plus an explicit OpenAI/Anthropic connect menu option.
- A connected model key that returns no billing data now explains it needs an admin
  key (with the exact URL), instead of dead-ending on an empty bill.
- `finops doctor` reports the license tier, so activating a key is verifiable.
- API-key entry strips wrapping quotes/whitespace and warns on a wrong-provider
  paste, instead of silently storing a broken key.

## 0.8.73

### The one-click connect key is now strictly read-only
The published CloudFormation template advertised "no create, modify, or delete
permissions of any kind" but its action list still carried `logs:PutRetentionPolicy`,
a write, left over from a never-built auto-remediation path. nable never calls it
(the log-retention feature only reads and then hands the user a CLI command to run
themselves), so it was dead permission that contradicted the read-only claim on the
exact artifact a security-minded user reads at connect time.

- Removed `logs:PutRetentionPolicy` from the connect credential. The one-click key
  and the role/Terraform templates are now 100% read (Get/Describe/List + STS auth),
  so "read-only, auditable" is defensible verbatim.
- Added `logs:Put/Create/Delete` to the over-privilege guard so a write can't creep
  back into the policy unnoticed, plus a test asserting every action in the connect
  key is a read verb.

## 0.8.72

### Onboarding: kill the no-creds connect wall
The activation funnel showed the problem plainly: ~99 machines start setup in 30
days, ~5 connect a real provider. The entire drop is at the connect step, and the
worst case is the user with no local AWS credentials, who was forced to hand-mint
an IAM key in the console.

- **One-click AWS connect is now live.** The read-only CloudFormation template is
  published, so `quick_create_available()` is true by default and `finops setup aws`
  surfaces a launch-stack link: the user clicks, creates a read-only stack, and
  pastes two outputs. No pre-existing credentials needed. This stays local-first:
  the template is a static public artifact, the keys are created in the user's own
  account, and nothing routes through nable's servers.
- **The welcome flow leads with it.** When the one-click link is available, the
  connect menu and the skip-path hint both surface "Fastest, one-click read-only
  AWS key" so a no-creds user sees the fast path immediately instead of a key
  prompt they can't satisfy. Gated on publish, so it never shows a dead link.
- **AWS CloudShell fast-path.** For a no-local-key user, the connect offer now
  points at CloudShell (already authenticated): `pip install finops-mcp && finops
  welcome` shows their real bill in seconds via ambient-credential detection, with
  nothing to mint.

## 0.8.71

### AI-native cost coverage
- **New gateway connectors: OpenRouter and LiteLLM.** OpenRouter is where a large
  share of early AI startups route token traffic (one key, 300+ models); the
  connector reads per-model cost, tokens, and requests from the activity endpoint,
  and falls back to a credits-only summary on a standard key. LiteLLM reads a
  self-hosted proxy's `/spend/logs` and aggregates spend by model and day, all on
  the user's own network. Both flow into `get_llm_costs`.
- **GPU/inference-infra connectors: Modal, Together, Replicate** (`get_gpu_infra_costs`).
  These hold the largest variable cost for model-builders. Each confirms the
  credential and reports the gate honestly (billing APIs are Team/Enterprise-only
  or omit per-range cost) rather than fabricate spend. Track these via invoice
  import until a usable usage endpoint exists.
- **`list_connected_providers` now has an `llm` category** so OpenAI, Anthropic,
  Vertex, OpenRouter, LiteLLM, Modal, Together, and Replicate are visible. They
  were previously absent from the provider list entirely.

### AI KPI engine correctness
- **OpenAI now feeds the AI KPIs, not just Anthropic.** `full_kpi_report` built its
  combined token map from `[anthropic_data]` only, and the OpenAI connector
  returned cost without tokens, so an OpenAI-heavy account (the common case) got
  empty cache, context-window, and prompt-efficiency analysis with a misleading
  "connect OpenAI with an admin key" note OpenAI could not satisfy. The OpenAI
  connector now emits `by_model_tokens` (fresh input, cache reads, request counts)
  matching the Anthropic shape, the aggregator surfaces merged per-model tokens
  across all providers, and the KPI engine consumes them. Cache analysis now works
  for OpenAI too. Guarded against double-counting Anthropic when both inputs are
  present.

### Credit cliff + AI-billing blind spots
- **AWS credit-to-cash flip detection** (`get_credit_status`). Reads Cost Explorer's
  RECORD_TYPE (Charge type) to separate gross usage, credits applied, and net cash
  per month, then detects the moment promotional credits stop covering the bill —
  the cliff where an early startup first feels cost pain, which AWS sends no native
  alert for. No CUR/Athena pipeline: works on a read-only key. Honest that AWS
  exposes no API for the remaining Activate balance, so the trend is inferred from
  observed consumption.
- **Cash-flip alarm**: a daily `credit_check` scheduler job fires a Slack alert once
  when the flip trips, deduped by month and status.
- **AI-billing blind spots** (`get_ai_billing_blind_spots`). Flags Bedrock,
  Marketplace, and SageMaker spend that bypasses AWS Cost Anomaly Detection, so a
  spike does not go unnoticed until the invoice lands.

### Setup
- Added setup-wizard entries and CLI subcommands for openrouter, litellm, modal,
  together, and replicate.

## 0.8.70

### Remediation
- **The rightsizing find -> fix -> prove loop now actually closes.** The PR pipeline
  already resolved a cloud resource ID to its Terraform address from state and
  opened a real PR with the fix, but the verification it promised in the PR body
  ("nable will auto-verify the change and record realized savings within 24h") was
  never scheduled, so it only ran if a human remembered to call `verify_savings`.
  Added a daily `auto_verify` scheduler job that re-reads the live resource and
  records the realized saving once a merged change is applied. The promise is now
  kept automatically.
- **Clearer skips.** When a recommendation can't be located in Terraform, the skip
  message now says whether no state was found (run from your IaC dir, or pass
  `resource_overrides`) or the resource isn't managed in this state, instead of a
  generic "cannot locate" line.
- Tests: locked in the auto-resolution-from-real-`tfstate` path (the defensible
  code), plus the auto-verify job wiring.

### Onboarding
- **The first-run "sample bill" no longer renders empty.** On the skip-for-now
  path, the value-moment ran `list_idle_resources` and `optimize_ai_spend`
  alongside the cost summary, but `list_idle_resources` had no demo guard, so in
  demo mode it reached for real AWS and blocked the whole scan, leaving "Here's
  nable on a sample bill" followed by nothing. Now the headline number is fetched
  first on its own wall-clock cap and always renders; the optional idle/AI scans
  run only for real accounts (no demo dataset exists for them) and each on its own
  cap, so a slow or blocking scan can never blank the number the user came for.

## 0.8.68

### Telemetry
- **`provider_connected` now fires on the ambient-credential path too.** When a
  user connects via an existing AWS profile, SSO, or the default credential chain
  (no manual key entry), the welcome flow confirms a real read and emits
  `provider_connected` with `auth_method="ambient"`. Previously only the manual
  access-key/role/profile path emitted it, so the activation metric was blind to
  everyone who connected the easy way and undercounted real connections.

## 0.8.67

### Onboarding
- **The first-run "show your bill" scan can no longer hang setup.** It was
  wrapped in a 35s asyncio timeout, but that timeout cannot fire when a blocking
  call (an SSO token refresh or a slow Cost Explorer request) pins the event
  loop, so for some credential setups the welcome flow hung forever at "Show your
  real AWS bill now?". The scan now runs in a daemon thread with a real
  wall-clock cap that returns on time regardless of blocking I/O, plus a "this
  can take up to ~30s" progress line so it never looks dead. On timeout it falls
  back gracefully; your editor config is already written by this point.

## 0.8.66

### Install reliability
- **Install and launch now pin a clean managed Python (`--python 3.12`).** On an
  Apple Silicon Mac with an x86_64 Anaconda base, uvx would source-build
  cryptography for the wrong architecture and fail ("incompatible architecture"),
  which also meant the MCP server could silently fail to start in Claude or Cursor
  for that cohort. Forcing a managed interpreter makes the build arch-native and
  isolated from any conda/system Python. Applied consistently across the install
  command, the configs the wizard writes (Claude Desktop, Cursor, Claude Code),
  the Cursor one-click deeplink, the Claude Code plugin, the README, and the docs,
  so the interpreter is cached at install time and the launch is a cache hit.

## 0.8.65

### Onboarding
- **The setup wizard now wires Cursor and Claude Code, not just Claude Desktop.**
  `finops welcome` writes `~/.cursor/mcp.json` and prints the exact
  `claude mcp add` command for Claude Code, and it reports which editors were
  actually configured instead of telling a Cursor user "you're set up" with
  nothing written. The shared config builder keeps all three clients in sync and
  never clobbers an unparseable config.
- **Restart guidance on the success screen.** MCP clients only load servers at
  startup, so the finish step now says to fully quit and reopen your editor, with
  a "you should see nable in your tool list" checkpoint.
- **Credential entry is no longer a dead end.** The auth-method menu validates
  your pick (a typo re-prompts instead of silently forcing manual key entry),
  temporary `ASIA` keys are accepted, and a no-key path exits cleanly with the
  steps to get one rather than blocking.
- **The one-click read-only key link is only shown once its template is
  published**, so the wizard never points you at a dead URL; until then it prints
  console steps. Adds the read-only-key CloudFormation template and a publish
  script.
- **Entry friction.** README lists `uv` as a prerequisite and sets the first-run
  download expectation. The `setup_wizard_started` ping is now off the critical
  path so a slow network can't stall the terminal before output.

### Telemetry
- `first_cost_query_success` no longer fires in demo mode, so the activation
  metric counts only people who saw their own real cost number.

## 0.8.64

### Security
- **The published least-privilege IAM policy is now complete.** Three
  free-tier tools (idle load balancer detection, ECR cleanup, ECS
  rightsizing) call AWS describe APIs whose actions were missing from the
  generated policy, so a credential scoped to it hit AccessDenied on those
  tools. Added the read-only actions (ELB, ECR, ECS, S3 multipart, two org
  actions) to `finops setup aws --iam-template`/`--iam-terraform` and
  `--check-scope`. Still write-free except the one documented
  `logs:PutRetentionPolicy`. Full policy published at getnable.com/iam.
- **Air-gap now covers the setup-wizard email capture.** It POSTed to
  getnable.com without checking `FINOPS_AIRGAP`; it is now gated, so air-gap
  mode truly sends nothing to any non-provider endpoint.

## 0.8.63

### Fixed
- **Demo mode now answers the headline questions.** A cold install
  (`finops welcome --demo`, no credentials) is the first thing a cautious
  trialist runs. `optimize_ai_spend` returned "No AI spend detected" there,
  hiding the AI-cost differentiator; it now runs the real optimizer over
  sample data and headlines the signature finding (input-heavy uncached
  Bedrock spend) with honestly confidence-labelled levers.
  `explain_recent_cost_drivers` returned "No providers connected"; it now
  answers with a coherent cost-change story.
- `finops --version` now works (it errored with "unrecognized arguments").
- CLI help no longer says "plain English."

## 0.8.62

### Performance
- **Cost queries drop from ~20s to the slowest single provider, repeats are
  free.** Azure and GCP connectors ran their sync SDK calls on the event
  loop, blocking every other connector; the AI-spend path fetched four
  providers serially with blocking HTTP at 10 call sites; six server loops
  queried providers one at a time; and only AWS had a cache. All connectors
  now run concurrently off the event loop with a shared 12h read-through
  cache. Measured 4x3s providers: 3.0s cold, 0ms warm.
- Hard per-provider timeout (`FINOPS_PROVIDER_TIMEOUT_S`, default 90s) so one
  hung provider API can no longer freeze a query. The Azure SDK ships with no
  timeout at all.
- AWS Cost Explorer / STS clients now set botocore connect/read timeouts;
  the defaults leaked worker threads for minutes under throttling.

### Fixed
- **Teams report delivery was silently broken.** reports.py called
  `teams.send_to_webhook`, which never existed, so every Teams report
  subscription threw and was swallowed. Implemented it (refuses non-Office
  webhook hosts) with regression tests.
- Setup wizard told buyers their license key starts with `FINOPS-1-`; real
  keys are `FINOPS-2-`. The in-product upsell branded the seven Pro features
  as "Team" and linked the $1,000 Team checkout for a $100 Pro gate; both now
  correct.
- Dashboard fetches run in parallel under the same provider timeout and reject
  requests with an unrecognized Host header (DNS-rebinding hardening,
  `FINOPS_DASHBOARD_ALLOWED_HOSTS` extends the allowlist).
- SQLite WAL/SHM sidecar files clamped to 0600 like the main db.
- Slack approval cards strip `<!channel>`/`<@user>` tokens from user-supplied
  ticket previews.

## 0.8.61

### Security
- **v1 license keys are retired.** The legacy HMAC signing secret was exposed
  in public git history, making every v1 key forgeable. v1 keys are no longer
  generated or accepted; v2 (Ed25519) keys are unaffected.

### Fixed
- **All telemetry now honors opt-out, air-gap and CI suppression.** The setup
  wizard and server sent events through a path that skipped the guards, so
  CI runs, air-gapped machines and opted-out users still pinged PostHog, and
  offline machines stalled up to 9 seconds per CLI command. The check now
  lives in the sender itself.
- `NABLE_NO_TELEMETRY=0` no longer opts out; falsy values ("0", "false",
  "no") are treated as off, matching `FINOPS_AIRGAP` parsing.
- A fast-exiting first run could mark the welcome sentinel and exit before
  the `install_completed` event was delivered, permanently uncounting that
  install. The sender now gets a short window to land.

## 0.8.60

### Fixed
- **`install_completed` telemetry now counts real installs, not automation.** It
  fired on any first CLI run, so cache-warm subprocesses, piped invocations, CI
  runners, and fresh uvx environments each logged a phantom install with a
  throwaway id, badly inflating install counts. It now only fires on an
  interactive, non-CI first run (stdin/stdout are a TTY). CI and build runners
  send no telemetry at all. A non-interactive first run is a no-op and leaves
  the first-run sentinel unset, so the first genuine human run still counts once.

## 0.8.59

### Security
- **Rotated the license signing key.** The previous Ed25519 public key shipped
  in the package paired with a private seed that had been committed to the
  public repo (in a test file), so anyone could mint valid license keys. The
  key is rotated; the leaked seed no longer validates against the bundled key,
  and a regression test fails the build if it ever does again. The test suite
  now uses its own throwaway keypair, genuinely separate from production.

## 0.8.58

### Added
- **Team plan ($1,000/mo flat, unlimited seats).** The conversational layer is
  now a paid tier: the Slack bot (questions, thread memory, RCA), chat
  remediation behind the human approval gate, and managed AI. Hard gate: the
  bot checks the license at startup and on every question; free and solo Pro
  keys get a clear upgrade message, trial keys get the full product. New
  "team" license plan with require_team gating; enterprise keys now correctly
  pass pro gates too (they previously failed the is_pro check). Pricing page
  updated: $1,000/mo or $10,000/yr, unlimited seats.

## 0.8.57

### Changed
- **Releases can no longer slow or break existing installs.** Configs written
  by the wizard now pin the exact version (`uvx finops-mcp==X`) instead of
  resolving "latest" at client startup, which could exceed Claude Desktop's
  startup timeout on the first launch after a release ("Server disconnected").
  Upgrades are now explicit: `finops upgrade` resolves the latest version,
  downloads it into the uvx cache up front, then moves the config pin, so the
  next Claude Desktop restart is instant. The Claude Code plugin pins the
  server version the same way, and the test suite fails a release that bumps
  the package without the plugin pin.
- **`finops welcome` never dead-ends, and shows value before asking for keys.**
  Onboarding analytics showed most installers skipped the credential step and
  saw nothing, then left. The first run now (1) detects an existing AWS
  credential chain and offers a one-keystroke read-only scan of your real bill,
  no setup menu; (2) if you skip or decline, still shows nable on a sample bill
  so the value lands before you leave the terminal; (3) labels demo output as
  sample data instead of claiming it scanned your account.

### Fixed
- **Onboarding can no longer hang on the ambient AWS check.** The first-run
  credential probe is capped at 3 seconds, so a firewalled IMDS endpoint or a
  stale SSO profile times out cleanly instead of freezing setup.
- **Slack thread memory no longer loses turns under concurrency.** Two quick
  @nable mentions in the same thread used to race and drop one exchange; the
  read-modify-write is now serialized per thread.
- **`finops-slack --help` works** and prints real usage instead of failing with
  a token error. The bot also prints a clear ready line on start, and warns at
  startup when no cloud provider is connected (cost questions would otherwise
  come back empty with no explanation).
- **Friendlier in-Slack errors.** A misconfigured bot tells end users "nable
  isn't fully set up yet, ask whoever installed me" instead of leaking env-var
  names, with the technical detail logged server-side.
- **Demo mode no longer leaks.** `FINOPS_DEMO` is set only for the duration of a
  sample-data scan and restored after, so it can't switch a later real scan to
  demo data in the same process.

## 0.8.56

### Added
- **The Slack bot now sees the whole platform.** A new tool bridge exposes the
  MCP server's registry to the conversational loop: 57+ tools instead of 6.
  Ask about AI spend, Kubernetes waste, commitments, team attribution, audits,
  forecasts, anything the server can answer. One source of truth, no schema
  drift, role-gated (viewer/analyst/admin) both before the model sees a tool
  and again at call time. Destructive tools and credential management are
  never bridged.
- **Real conversations.** Threads now have memory: follow-ups like "what's
  driving that?" work. Rolling window per thread, 48h TTL, DMs keep a running
  context per channel.
- **Root cause analysis on tap.** The Investigate button and "why did costs
  spike" questions route to a deeper investigation pass built on
  explain_recent_cost_drivers, reporting dollar impact, likely cause with
  evidence, alternatives, and the next step.
- **Remediation with an approval gate.** From a Slack conversation, nable can
  draft a ticket (Jira/Linear/GitHub) or a Terraform rightsizing PR. Drafts
  post a preview card with dry-run details (files, estimated $/mo). Nothing is
  filed or opened until a human with the analyst role or above clicks Approve.
  Approvals expire after 24h.
- **Tiered model routing.** Quick lookups run on Haiku, free-text questions on
  Sonnet, RCA investigations on Opus. Override with FINOPS_SLACK_MODEL or
  per-tier FINOPS_SLACK_MODEL_{SIMPLE,CHAT,RCA}. A cost tool should not burn
  Opus tokens asking what yesterday cost.
- **finops setup slack option 3: conversational bot.** Prints a paste-ready
  Slack app manifest, validates the token against auth.test, stores
  credentials in the OS-keyring vault, and the bot loads them from the vault
  at startup. No .env file required.

### Security
- **Remediation drafting requires real authentication.** With
  FINOPS_REQUIRE_AUTH off, every Slack user resolves to admin, which would
  have let anyone draft and approve their own PR or ticket. Drafting is now
  disabled unless auth is on, or the operator opts in with
  FINOPS_SLACK_ALLOW_REMEDIATION=1. Enforced both when tool schemas are
  exposed to the model and again at execution time.
- **Self-approval blocked.** The requester of a pending action cannot click
  Approve on it. A different person must review. Solo operators can opt out
  with FINOPS_ALLOW_SELF_APPROVE=1.

### Fixed
- **Slack RBAC identity now reaches the tools.** Identity was stored in a
  ContextVar set in the handler thread, but tools ran in a worker thread where
  ContextVars do not propagate, so role enforcement silently saw no identity
  when FINOPS_REQUIRE_AUTH=1. Identity is now passed into the loop and set in
  the worker thread.

## 0.8.55

### Changed
- **`optimize_ai_spend` separates realizable savings from routing ceilings.** The
  headline `addressable_savings` counts only levers we can stand behind (prompt
  caching, measured error spend). Model-routing downgrades (Sonnet to Haiku and
  the like) are reported as `potential_upside`, clearly labeled as a ceiling that
  assumes eligible calls move, with a pointer to the per-function analyzer. No
  more inflated headline number.

### Added
- **Bedrock input/output/cache cost split from Cost Explorer.** `optimize_ai_spend`
  now spots the most common AI waste with no CloudWatch needed: an input-heavy
  Bedrock bill running with no prompt caching. It quantifies the caching
  opportunity and flips the spend-shape driver to caching when input dominates.
- **Bedrock SKU display names fire model-switch recommendations.** Cost Explorer
  reports Bedrock spend as names like "Claude Sonnet 4.5"; these now normalize to
  canonical model ids so Sonnet to Haiku recs work for Bedrock-only users.

## 0.8.54

### Added
- **`optimize_ai_spend`: a ranked, dollar-quantified plan to cut your AI bill.**
  Fuses cross-provider LLM costs (OpenAI, Anthropic, Bedrock, Azure OpenAI,
  Vertex) with the AI KPI report into one ranked set of levers: model routing,
  prompt caching, output reduction, error reduction, and model consolidation.
  Only levers with a grounded basis carry a dollar figure, governance levers are
  listed without inflating the headline, and output-trim savings are skipped for
  any model that already has a routing rec so nothing double-counts. It also
  decomposes spend into its real driver: model choice, token size, or request
  volume. Read-only intelligence, never a runtime proxy.
- **nable is now a Claude Code plugin.** Run `/plugin marketplace add getnable/finopsmcp`
  then `/plugin install nable@nable` to register the MCP server with no
  hand-edited config. Ships a `/nable:connect` command that connects a cloud
  account and shows your first cost number inside the editor.

## 0.8.53

### Changed
- **One-command onboarding: `uvx --from finops-mcp finops welcome`.** This fetches
  a matching Python, installs nable, and runs the setup wizard in a single step
  with no PATH setup. Replaces `uv tool install finops-mcp && finops welcome`,
  which silently failed on any machine where `~/.local/bin` was not on PATH (the
  `finops` command was "not found" right after a successful install). Site, docs,
  and install widgets updated.

### Added
- **The welcome wizard now pays off in the terminal.** After connecting a cloud
  account it scans and prints a real number right there: total spend, the top
  driver, your AI/ML share of the bill, and any idle waste, before you ever open
  your editor. Onboarding used to hand you a config wizard and send you off with
  nothing to show; now it finds money first. Fully guarded, so a slow or failing
  scan never blocks setup.
- **`finops welcome --demo`** runs the entire flow on realistic sample data with
  no account or credentials, so anyone evaluating nable sees the value moment first.
- The wizard now auto-configures Claude/Cursor (prefers `uvx finops-mcp`, no PATH
  dependency) instead of asking you to run a second command and press Enter.

## 0.8.52

### Fixed
- **Sync tools no longer error under MCP dispatch.** `whoami`, `create_api_key`,
  `list_api_keys`, and `revoke_api_key` are synchronous, but the instrumentation
  wrapper awaited every tool result unconditionally, so they failed with "object
  dict can't be used in 'await' expression" when called through the server. The
  wrapper now only awaits coroutines.
- **`explain_recent_cost_drivers` works again.** It unpacked the float grand
  total from `_gather_costs` and called `.keys()` on it ("'float' object has no
  attribute 'keys'"). It now diffs the per-service breakdown as intended.
- **`get_llm_costs` now sees Bedrock spend.** Cost Explorer labels model spend
  under SKU service names like "Claude Sonnet 4.5 (Amazon Bedrock Edition)", which
  the hardcoded `SERVICE == "Amazon Bedrock"` filter missed, reporting $0. nable
  now discovers the actual Bedrock service names via `GetCostAndUsage` (the same
  permission cost queries already use, no extra IAM grant) and attributes spend
  by model.
- Added dispatch-level regression tests so sync-tool breakage and these data
  bugs cannot ship silently again.

## 0.8.51

### Changed
- **Team pricing is now $100/seat/mo ($1,000/seat/yr).** The in-tool upgrade
  nudges, feature-gate messages, onboarding email, and checkout link all
  reflect the new price, so the product matches the site.

### Added
- **Contextual Team upsells.** Free users now get a one-time, topic-specific
  nudge keyed to what they just asked: anomalies surface auto-ticketing and
  Slack/Teams alerts, rightsizing surfaces the auto-PR, attribution surfaces
  scheduled digests, commitments surface coverage-gap modeling, and so on.
  Shown at most once per topic per session, helpful, not spammy, and never to
  paying users.
- **Board-ready export upgrades.** `export_board_summary` now includes AI spend
  as a share of MRR and AI cost per customer, pulling MRR and paying customers
  from Stripe when connected (the cost-per-customer bridge), with a precise
  "built on your machine, no nable backend holds it" note.

## 0.8.50

### Added
- **Unit economics populate themselves from Stripe.** Cost per customer and
  AI-as-percent-of-MRR used to require manually entering MRR and paying customers
  with `set_business_metrics`. If Stripe is connected (`STRIPE_SECRET_KEY`), nable
  now pulls MRR and active paying-customer count from your live subscriptions and
  fills those in automatically, so `get_llm_unit_economics_full`,
  `get_unit_economics`, and `get_business_metrics` answer on the first question
  with no data entry. Manual entry always wins; the Stripe pull only fills gaps,
  and the snapshot is persisted once a day so it trends over time. MRR normalizes
  every billing interval (month, year, week, day, and `interval_count`) to a
  monthly figure and skips metered/usage-based items, so it is a conservative
  floor, never an overstatement. Mixed-currency and truncated-line-item cases are
  surfaced as caveats rather than hidden.

## 0.8.49

Bug-fix pass from a multi-agent debug + security audit (adversarially verified).

### Fixed
- **Cost queries and waste scans no longer freeze the server.** The AWS cost
  connector and the rightsizing / deep-audit / idle scanners ran blocking SDK
  calls directly on the MCP event loop, so every query stalled the editor for
  seconds to minutes. They now run off the loop via `asyncio.to_thread`.
- **The Team tier works on Postgres.** Three queries used SQLite-only
  `date('now', ...)` / `datetime('now')` syntax that raises on Postgres, the
  shared-team mode the Team tier sells (waste-pattern scan, the Slack budget
  command, forecast-model persistence). Dates are now computed in Python and bound.
- **No more duplicate anomaly alerts or tickets.** Anomaly detection now dedups on
  (provider, service, account, date, direction), so a cron retry or the
  `run_anomaly_check_now` tool cannot re-alert or re-create a ticket for the same
  spend event. Also fixed the anomaly id returned wrong on Postgres.

### Security
- **CSV formula injection neutralized** (CWE-1236). Exported reports prefix a
  leading `=`, `+`, `-`, `@`, tab, or CR with an apostrophe, so a resource named
  `=HYPERLINK(...)` can't run as a formula when finance opens the file in Excel.
- **`start_dashboard_server` defaults to localhost** instead of all interfaces, and
  now surfaces the auto-generated password in the result so you can actually log in.
  Pass `expose=true` to bind the network, with a cleartext-HTTP warning.
- **Air-gap mode honors its promise.** Benchmarking egress (to bench.nable.dev) and
  external report delivery (Slack/Teams/email) are now suppressed when
  `FINOPS_AIRGAP` is set, instead of egressing anyway.
- The README data-handling claim is now precise (matches the rest of the docs).

## 0.8.48

Dogfooding fixes: precise errors, cleaner prompts, honest security wording.

### Fixed
- **Rightsizing now names the exact missing permission.** When the IAM identity
  lacks an action like `rds:DescribeDBInstances`, the tool used to return an empty
  result and the model would guess ("maybe CloudWatch, maybe a region"). It now
  reports the precise missing IAM action and how to add it.
- **The role prompt accepts the role name.** Typing `finops` at the persona prompt
  used to silently fall back to the default (Engineer). It now matches the role by
  number, name, or keyword, and warns on an unrecognized answer instead of guessing.
- **No more doubled yes/no markers.** Prompts read `Write config? [Y/n]:` instead
  of `Write config? [Y/n] [y]:`.

### Changed
- **Security wording is precise.** The old "your data never leaves your machine"
  overstated it. Credentials never leave your machine and nable has no backend that
  holds your data, but the figures you ask about go to your editor's own AI to
  answer the question, the same as any prompt. The docs, site, and emails now say
  this plainly, and note the local dashboard / CLI path for zero AI exposure.

## 0.8.47

Onboarding and activation pass. The goal: get a new user from install to a real,
dollar-quantified insight with the fewest possible steps.

### Changed
- **`finops setup aws` is now detect-then-confirm.** It probes the machine for
  credentials you already have (SSO login, AWS CLI profile, or default chain),
  shows the account it found, and connects it on one keystroke. If you use `aws`
  already, you type nothing. The old flow asked for an account name, a region,
  and an auth method up front. Manual entry is now the fallback, and it lists
  your actual profiles instead of asking you to type one blind.
- **No more region prompt.** Cost Explorer is global and the scanners already
  auto-discover regions, so the prompt was asking for something that changed
  nothing. Defaults silently to us-east-1.
- **The editor is wired in the same flow.** Connecting an account now registers
  the MCP server too, so there is no separate `finops setup claude` step.
- **The MCP server standardizes on the name `nable`.** A legacy `finops`
  registration is migrated in place so you are never double-registered.
- **Team price is $40/mo everywhere.** The product had been quoting $19.99 in
  upgrade nudges and trial emails while the site said $40.

### Added
- **First cost answer surfaces real waste.** Your first query now proactively
  reports the idle resources nable found, in plain dollars, instead of only
  answering the literal question.
- **Upgrade prompts cite your actual ROI.** When nable has already found savings
  that dwarf the $40 plan, the nudge says so ("found $X/mo, Nx the Team plan").

### Fixed
- Setup never persists a broken account: credentials are verified before saving.
- Credential detection cannot hang (bounded connect/read timeouts).
- Removed dead `finops-infra` and `finops-license` console aliases.

## 0.8.46

Bug-fix pass from a multi-agent troubleshooting run (13 confirmed defects,
adversarially verified). Several were in the 0.8.45 Azure code.

### Fixed
- **VM rightsizing was broken on every real Azure subscription.** The CPU metrics
  query used `isoformat()` ("+00:00"), and Azure Resource Manager decodes the `+`
  to a space, returning HTTP 400. So every VM read as "no CPU data", rightsizing
  found nothing, and it falsely blamed a missing Monitoring Reader role. Now uses
  a `Z` suffix. The same fix was applied to the doctor probe.
- **Azure forecast** no longer crashes on a short/empty response row, no longer
  sums the wrong column when a `Cost` column is absent (it skips the subscription
  instead of reporting nonsense dollars), and splits actual vs forecast by date
  when the `CostStatus` column is missing instead of labeling everything forecast.
- **The 5 Azure tools** now run their blocking REST calls off the event loop
  (`asyncio.to_thread`), so a large VM scan no longer freezes other tool calls,
  the Slack bot, and the scheduler.
- **Dashboard session store** is now lock-guarded. Under the threaded server, two
  concurrent logins could race the prune and 500 with "dictionary changed size
  during iteration".
- **Scheduled reports no longer blast on subscribe.** A new subscription was
  treated as "never sent" and sent a full report within ~5 minutes. It now waits
  for the first scheduled time after creation.
- **Scheduler shutdown** releases its cross-process single-owner lock, so another
  host can take over (Postgres mode) and the connection/file handle is not leaked.
- A malformed `FINOPS_SNAPSHOT_CRON` no longer aborts MCP server startup (removed
  a dead parse line).

## 0.8.45

Azure gets the same depth nable already had for AWS. Built clean-room from the
Azure REST APIs (no heavy SDKs), so teams on Azure can ask the same questions AWS
teams ask and get real answers.

### Added
- **`get_azure_vm_rightsizing`**: finds idle and oversized VMs from Azure Monitor
  CPU, with real per-VM cost joined from Cost Management. Idle VMs are
  deallocate/delete candidates, underutilized VMs are downsize candidates, and
  bursty VMs are left alone, the Azure parallel of nable's idle-EC2 and
  rightsizing engines. Just ask "vm rightsizing".
- **`get_azure_advisor_recommendations`**: Azure Advisor cost recommendations with
  Microsoft-computed annual savings (the Azure parallel of Compute Optimizer).
- **`get_azure_budgets`**: reads the budgets you already set in the Azure Portal
  (Consumption Budgets API) and reports consumption and warning/exceeded status.
- **`forecast_azure_costs`**: uses Azure Cost Management's own forecast model,
  blending actual billed days with Microsoft's forecast for the rest of the month.
- **`get_azure_cost_by_dimension`**: break spend down by service, resource group,
  location, or meter.

All five are free (matching the AWS optimization tools) and degrade to a clean
error when Azure is not configured.

### Ready-to-use hardening (from a pre-publish security + DX review)
- **RBAC is now surfaced, not silent.** The Azure tools span three roles per
  subscription (Cost Management Reader, Reader, Monitoring Reader). `finops doctor`
  now probes them and names any missing role with the exact `az role assignment`
  fix, `finops setup azure` prints the roles, and the README documents them. A
  missing role no longer shows up as a confusing empty result.
- **VM rightsizing scans the costliest VMs first and caps the scan**
  (`max_vms_scanned`, default 200), so a large estate cannot hang on hundreds of
  serial Azure Monitor calls.
- VM rightsizing and Advisor now return an actionable `permission_hint` when they
  list resources but get no data (the usual sign of a missing role).

## 0.8.44

Fix the usage signal. The telemetry was wired but three bugs were corrupting or
silently dropping it, so active-install counts were unreliable.

### Fixed
- The first-run `install_completed` event was sent with a constant
  `distinct_id="install"`, so every install collapsed into one analytics person
  and installs could not be counted. It now uses the per-install anonymous ID.
- Telemetry was delivered over `urllib`, which fails certificate verification on
  python.org macOS builds (empty trust store until the user runs Install
  Certificates), silently dropping every event for that segment. It now sends via
  httpx (bundled CA store), with a urllib fallback.
- `finops doctor` reported "No usage telemetry active" when telemetry is on by
  default. It now reports the real posture and how to opt out.

### Changed
- First-run output now discloses the anonymous usage ping next to the
  credentials-stay-local line, with the `NABLE_NO_TELEMETRY=1` opt-out. Telemetry
  still never includes cost data, account IDs, or credentials.

## 0.8.43

A multi-agent code review and debugging pass (10 reviewers, every finding
adversarially verified) plus a token-cost reduction for users. 18 confirmed
findings fixed, including three cost-correctness regressions from the 0.8.40
pass and two pre-auth holes in the team-host deploy.

### Fixed
- **S3 Intelligent-Tiering savings were silently dropped** from the three
  consolidated reports (full audit, CSV export, Notion publish). They filtered on
  a recommendation string the engine stopped emitting in 0.8.40, so S3 IT waste
  never reached the consolidated totals. Now matched correctly, with a contract
  test pinning the string.
- **Idle-EC2 network guard read ~12x too low.** It averaged the NetworkOut metric
  instead of summing it, so a busy instance still looked idle. Uses Sum now.
- **Idle-EC2 metal sizing** assumed 96 vCPU for every `*.metal`, over-estimating
  savings on smaller metal types. Falls back to a conservative default.
- **Textract non-prod detection** missed hyphenated `non-prod` names. Restored
  without re-introducing substring false positives.
- **Read-only dashboard share links could escalate to full access.** Read-only and
  full sessions shared one token store, so a share token replayed as a login
  cookie passed the full-access check. They now use separate stores.
- **Pre-auth path traversal** in the dashboard font route let an unauthenticated
  client on the network read arbitrary `.css`/`.woff` files. Paths are now
  confined to the fonts directory.
- **Slack scheduled reports never sent**: the job imported a function that did not
  exist, failing silently every five minutes. Implemented it (`_is_report_due`).
- **Double digests/alerts**: the MCP server and `finops serve` could both run the
  scheduler against one database. Added a single-owner lock (Postgres advisory
  lock, file lock locally).
- Database Savings Plan recommendations now report `data_incomplete` on a Cost
  Explorer failure instead of a confident $0, and size the commitment at the
  discounted rate rather than on-demand.
- Smaller hardening: `Secure` cookies behind TLS, a plaintext-bind warning,
  clean scheduler shutdown on Ctrl+C, HTML-escaped report titles, a guarded
  Slack request verifier, and a date-parse guard on `export_cost_report`.

### Changed
- **Dashboard serves concurrently.** Switched to a threaded HTTP server so one
  slow cost fetch no longer stalls every other finance user.
- **Lower token cost on large results.** `list_idle_resources` and
  `get_rightsizing_recommendations` capped their detail lists to a token budget
  (costliest first), with the omitted count surfaced. On an 800-resource account
  the idle response drops from ~44k to ~6k tokens, and the totals stay exact.

## 0.8.42

DX polish on the team-host deploy, found in review.

### Changed
- `finops serve --help` now discloses that the command also hosts the scheduler
  and the Slack bot, when each turns on, and how dashboard auth works. The help
  text used to describe only the dashboard, so an operator did not know `serve`
  was the finance host.

### Fixed
- The startup banner no longer reports `Slack bot: ON` when `slack_bolt` is not
  installed. `slack_bolt` imports lazily inside the bot thread, so a missing
  dependency failed in-thread after the banner already claimed ON. The banner
  now prechecks the dependency and reports `OFF` with the reason.

## 0.8.41

Give non-engineers access without an install. The engineer sets nable up; finance
consumes it through Slack, email, and the dashboard, and credentials stay inside
your own infrastructure.

### Added
- `finops serve` is now an always-on team host. It starts the scheduler (pushed
  snapshots, anomaly alerts, daily and weekly digests) when
  `FINOPS_ENABLE_SCHEDULER=1`, and the Slack bot (two-way cost Q&A) when
  `SLACK_BOT_TOKEN` and `SLACK_APP_TOKEN` are set. A startup banner shows which
  finance interfaces are live. A failed service degrades to OFF; the dashboard
  still serves.
- `DEPLOY.md`: a one-command team deploy guide (`docker compose up -d`).

### Changed
- `docker-compose.yml` and `.env.example` now pass the Slack, scheduler, and SMTP
  settings, so the existing compose file is the finance deploy.

## 0.8.40

False-positive correctness pass: nine optimization engines were flagging healthy
resources as waste. Each fix shipped with a regression test, and the full set was
adversarially re-verified (FP removed, no new bug, no over-suppression of real waste).

### Fixed
- **S3 Intelligent-Tiering**: read bucket size across the IntelligentTiering* storage
  classes, not StandardStorage alone. On a real IT bucket the bytes live under
  `IntelligentTieringFAStorage`, so the old query read ~0 and flagged every IT bucket
  as tiny-object waste.
- **S3 bucket keys**: stop fabricating a 100,000 KMS-calls fallback. When bucket
  metrics are absent, the bucket is still surfaced but savings are reported as
  unknown (0.0) instead of an invented number.
- **Textract environment**: match non-prod name signals (`qa`, `dev`, `test`, `uat`,
  `staging`) on token boundaries, not raw substrings, so `latest-invoice-handler` and
  `developer-portal` are no longer flagged. Acronym runs (`QA-doc-processor`,
  `UATPipeline`) are preserved so real non-prod callers still match.
- **Database Savings Plans**: size commitments off instance-hour usage only, excluding
  storage, IOPS, and backup line items that a Database SP does not discount.
- **NLB cross-zone**: single-AZ load balancers have no cross-AZ charge to remove;
  multi-AZ recommendations now carry an availability caveat instead of a blind
  "disable" action.
- **Spot diversification**: attribute-based instance selection and the
  `price-capacity-optimized` allocation strategy are no longer flagged as
  under-diversified.
- **Idle EC2**: skip instances with sustained network egress, and size vCPU from a
  real instance-type map instead of a broken string parse.
- **Rightsizing dedup**: collapse a resource flagged by both the heuristic and Compute
  Optimizer into one recommendation so savings are not double-counted.
- **EBS snapshot replication**: mark cross-region snapshot costs as an upper bound
  (snapshots are incremental, so real storage is a fraction of the provisioned size).

### Changed
- **S3 Intelligent-Tiering** is now framed as an ROI decision: the recommendation
  reports the monitoring fee as a percentage of the storage savings it unlocks. Under
  8% it is worth keeping, 8-100% is marginal, at or above 100% it is waste, with the
  math surfaced in `roi_summary`.

## 0.8.38

Faster, clearer terminal experience and proactive trust checks.

### Added
- `finops doctor` now verifies the extended Cost Explorer permissions nable needs
  (`ce:GetSavingsPlansCoverage`, `ce:GetReservationUtilization`), not just
  `ce:GetCostAndUsage`. A credential that passes the core check but misses these
  used to fail only mid-query ("no identity-based policy allows the
  ce:GetSavingsPlansCoverage action"). Doctor now catches it up front and prints
  the exact fix.
- `finops doctor` ends a clean run with a "what to ask next" nudge.
- `finops tools`: a grouped cheat-sheet of example questions to ask nable in
  Claude (costs, waste/savings, network/traffic, Kubernetes, AI/LLM, anomalies).

### Changed
- `finops --help` leads with a quick-start (`finops welcome` / `setup` / `doctor`
  / `serve` / `tools`) instead of dumping every subcommand.

## 0.8.37

Trust and correctness pass, plus a cross-cloud traffic tool.

### Fixed
- Corrected wrong pricing constants used in savings estimates (all independently
  re-verified against AWS pricing):
  - CloudWatch Logs Infrequent Access ingestion ($0.50/$0.25 per GB, was 6.6x low)
  - Lambda provisioned-concurrency keep-warm rate ($0.0000041667/GB-s, was 2.3x high)
  - CloudWatch composite alarms ($0.50/alarm/mo)
  - Public IPv4, NAT Gateway, and EFS cross-AZ rates
- Two wiring bugs that could abort the full cost audit, export, and scorecard
  tools (commitment and database-savings-plans analysis were called incorrectly).
- `create_rightsizing_tickets` no longer errors on a missing import.

### Added
- `get_traffic_cost_breakdown`: cross-cloud network spend, split internal
  (cross-AZ, cross-region, NAT) vs external (internet egress, CDN), with a
  per-scope solve playbook (VPC endpoints, topology-aware routing, CDN).
- Business-context layer (Pro): cost-per-customer, runway (infra and company),
  and a board-ready markdown cost summary. New `set_business_metrics` inputs for
  cash, last raise, and monthly opex.
- Local dashboard headline showing cost-per-customer and runway.
