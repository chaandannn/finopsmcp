# Security Policy

nable is local-first by design: it runs on your machine, your cloud
credentials stay in your OS keyring, cost data caches in a local SQLite
database, and there is no nable backend that receives either. The security
architecture is documented at https://getnable.com/security.

Two egress paths are worth naming directly. The AI assistant (the Slack bot
and the dashboard Ask tab) sends your cost question and its results to
Anthropic's API to generate an answer when you use it, never to a nable
server, and `FINOPS_AIRGAP=1` disables it. Anonymous usage telemetry (tool
names and a random install id, never cost figures, credentials, or account
identifiers) is on by default and turns off with `NABLE_NO_TELEMETRY=1` or
`FINOPS_AIRGAP=1`.

## Reporting a vulnerability

Email **chandan@nable.sh** with the details. Please include steps to
reproduce and the version (`finops --version`).

- You will get an acknowledgment within 48 hours.
- We aim to ship a fix for confirmed vulnerabilities within 14 days, faster
  for anything credential- or license-related.
- Please do not open a public issue for security reports until a fix is
  released. We will credit you in the changelog unless you prefer otherwise.

## Supported versions

Only the latest release on PyPI receives security fixes. `finops upgrade`
updates in place; pinned installs should track the latest patch release.

## Track record

Disclosed and fixed issues are documented in CHANGELOG.md, including the
retirement of v1 license keys after their signing secret appeared in public
git history (0.8.59 rotated the keypair, 0.8.61 retired v1 verification).

## Scope notes for researchers

- The MCP server runs with the invoking user's privileges by design; local
  privilege boundaries are out of scope.
- The interesting surfaces are: credential storage (`src/finops/vault*`),
  license verification (`src/finops/license.py`), the Slack bot's approval
  flow (`slack_bot/`), the account/licensing edge functions (`web/api/`),
  and the cloud-credential scoping templates (`finops setup aws
  --iam-template`).
