"""
nable GitHub App — cost-aware PR comments and check runs.

Handles GitHub webhook events for pull_request (opened/synchronize/reopened).
For each PR touching .tf files:
  1. Fetches changed .tf files from the PR
  2. Parses resource changes (added/modified/removed resource blocks)
  3. Estimates monthly cost delta using terraform_estimate pricing tables
  4. Posts a structured PR comment with cost table + savings tips
  5. Posts a GitHub Checks run (pass if delta < budget, warn if above)

Authentication:
  GitHub App:  GITHUB_APP_ID + GITHUB_APP_PRIVATE_KEY_PATH (or GITHUB_APP_PRIVATE_KEY)
  PAT fallback: GITHUB_TOKEN

Env vars:
  GITHUB_APP_ID              — GitHub App ID
  GITHUB_APP_PRIVATE_KEY     — PEM-encoded private key (or path via _PATH)
  GITHUB_APP_PRIVATE_KEY_PATH — path to .pem file
  GITHUB_TOKEN               — Personal Access Token (fallback, simpler)
  NABLE_COST_GATE_USD        — monthly delta above which PR check fails (default: 500)
  NABLE_COMMENT_TAG          — HTML comment tag for update-in-place (default: nable-cost)
"""
from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import re
import time
from datetime import datetime, timezone
from typing import Any

log = logging.getLogger(__name__)

COST_GATE_USD   = float(os.environ.get("NABLE_COST_GATE_USD", "500"))
COMMENT_TAG     = os.environ.get("NABLE_COMMENT_TAG", "nable-cost")
GITHUB_API_BASE = "https://api.github.com"
# GitHub only permits these characters in an owner or repo name. Validating before
# interpolating a user-supplied owner/repo into an api.github.com path rejects
# nothing real and neutralizes path injection (CodeQL py/partial-ssrf).
#
# Bounded at 100 because that is GitHub's own maximum for a repository name, and
# an unbounded segment from a webhook body is a URL of whatever length the sender
# feels like.
_GH_SEGMENT = re.compile(r"^[A-Za-z0-9._-]{1,100}$")


def _valid_segment(value: str) -> bool:
    """True when `value` is safe to interpolate into an api.github.com path.

    The character class alone is not enough, and this is the gap it left: "."
    and ".." are made entirely of permitted characters, so `owner=".."` passed
    validation and produced

        https://api.github.com/repos/../{repo}/pulls/{n}/files

    which any URL normaliser resolves upwards into a different endpoint than the
    one this code believes it is calling. The host cannot move (GITHUB_API_BASE
    is a constant, and everything interpolated lands after it), and the
    installation token bounds what the resulting call is allowed to do, so this
    was never a route to someone else's data. It is still a request going
    somewhere nobody chose.

    GitHub permits neither "." nor ".." as an owner or a repository name, so
    refusing every all-dots segment rejects nothing real.
    """
    if not _GH_SEGMENT.match(value or ""):
        return False
    return value.strip(".") != ""


# ── Auth helpers ───────────────────────────────────────────────────────────────

def _app_token(installation_id: int) -> str:
    """Generate a short-lived installation access token using JWT auth."""
    import jwt as pyjwt   # PyJWT
    app_id   = os.environ.get("GITHUB_APP_ID", "")
    key_path = os.environ.get("GITHUB_APP_PRIVATE_KEY_PATH", "")
    key_data = os.environ.get("GITHUB_APP_PRIVATE_KEY", "")

    if not app_id:
        raise RuntimeError("GITHUB_APP_ID not set")

    if key_path and not key_data:
        with open(key_path) as f:
            key_data = f.read()

    if not key_data:
        raise RuntimeError("GITHUB_APP_PRIVATE_KEY or GITHUB_APP_PRIVATE_KEY_PATH not set")

    now = int(time.time())
    payload = {"iat": now - 60, "exp": now + 600, "iss": app_id}
    jwt_token = pyjwt.encode(payload, key_data, algorithm="RS256")

    import httpx
    resp = httpx.post(
        f"{GITHUB_API_BASE}/app/installations/{installation_id}/access_tokens",
        headers={"Authorization": f"Bearer {jwt_token}",
                 "Accept": "application/vnd.github+json",
                 "X-GitHub-Api-Version": "2022-11-28"},
        timeout=10,
    )
    resp.raise_for_status()
    return resp.json()["token"]


def _headers(installation_id: int | None = None) -> dict[str, str]:
    """Return auth headers, preferring App auth, falling back to PAT."""
    token = os.environ.get("GITHUB_TOKEN", "")
    if not token and installation_id:
        try:
            token = _app_token(installation_id)
        except Exception as e:
            log.warning("App token failed, need GITHUB_TOKEN: %s", e)

    if not token:
        raise RuntimeError("No GitHub auth: set GITHUB_TOKEN or GITHUB_APP_ID + key")

    return {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "nable-finops/1.0",
    }


# ── GitHub API wrappers ────────────────────────────────────────────────────────

def _gh_get(url: str, headers: dict) -> Any:
    import httpx
    resp = httpx.get(url, headers=headers, timeout=20)
    resp.raise_for_status()
    return resp.json()


def _gh_post(url: str, headers: dict, body: dict) -> Any:
    import httpx
    resp = httpx.post(url, headers=headers, json=body, timeout=20)
    resp.raise_for_status()
    return resp.json()


def _gh_patch(url: str, headers: dict, body: dict) -> Any:
    import httpx
    resp = httpx.patch(url, headers=headers, json=body, timeout=20)
    resp.raise_for_status()
    return resp.json()


# ── Cost analysis ─────────────────────────────────────────────────────────────

def _diff_tf_costs(pr_files: list[dict], headers: dict) -> dict[str, Any]:
    """
    For each .tf file changed in the PR, fetch the patch and estimate cost delta.

    We parse resource blocks from both before (base) and after (head) versions
    and compute the monthly cost change per resource.
    """
    # No parser import: this function does its own regex pass over the diff
    # lines below, deliberately, because a unified diff is not valid HCL and the
    # full parser cannot read one. The line that used to sit here imported
    # `parse_terraform_resources` from .parser, a name that has never existed
    # there, and nothing called it. It raised ImportError on entry, so every PR
    # comment this function was supposed to produce was never produced.
    from ..connectors.terraform_estimate import _ESTIMATORS, ResourceChange, CostLine

    adds:    list[dict] = []
    removes: list[dict] = []
    changes: list[dict] = []
    unpriced: list[str] = []

    for file in pr_files:
        filename = file.get("filename", "")
        if not filename.endswith(".tf"):
            continue

        patch = file.get("patch", "")
        if not patch:
            continue

        # Parse added lines (+) and removed lines (-) from the unified diff
        added_lines   = [l[1:] for l in patch.split("\n") if l.startswith("+") and not l.startswith("+++")]
        removed_lines = [l[1:] for l in patch.split("\n") if l.startswith("-") and not l.startswith("---")]

        added_tf   = "\n".join(added_lines)
        removed_tf = "\n".join(removed_lines)

        # Use the lightweight inline price lookup (no full HCL parser needed for diff)
        for block_text, direction in [(added_tf, "add"), (removed_tf, "remove")]:
            import re
            # Find resource "type" "name" { ... } blocks in the diff lines
            for match in re.finditer(
                r'resource\s+"([^"]+)"\s+"([^"]+)"\s*\{([^}]*)\}',
                block_text, re.DOTALL
            ):
                rtype  = match.group(1)
                rname  = match.group(2)
                body   = match.group(3)

                # Extract attrs from body
                attrs: dict[str, str] = {}
                for am in re.finditer(r'(\w+)\s*=\s*"([^"]+)"', body):
                    attrs[am.group(1)] = am.group(2)
                for am in re.finditer(r'(\w+)\s*=\s*(\d+)', body):
                    attrs[am.group(1)] = am.group(2)
                for am in re.finditer(r'(\w+)\s*=\s*(true|false)', body):
                    attrs[am.group(1)] = am.group(2)

                from ..vscode_extension_prices import price_resource_py
                entry = price_resource_py(rtype, attrs)
                if entry is None:
                    unpriced.append(f"{rtype}.{rname}")
                    continue

                item = {
                    "resource": f"{rtype}.{rname}",
                    "file": filename,
                    "monthly": entry["monthly"],
                    "detail": entry["detail"],
                    "note": entry.get("note"),
                }
                if direction == "add":
                    adds.append(item)
                else:
                    removes.append(item)

    total_add    = sum(a["monthly"] for a in adds)
    total_remove = sum(r["monthly"] for r in removes)
    delta        = total_add - total_remove

    return {
        "delta":    round(delta, 2),
        "adds":     adds,
        "removes":  removes,
        "changes":  changes,
        "unpriced": unpriced,
    }


# ── Comment formatting ─────────────────────────────────────────────────────────

def _format_comment(analysis: dict, repo: str, pr_number: int) -> str:
    delta  = analysis["delta"]
    adds   = analysis["adds"]
    removes = analysis["removes"]
    unpriced = analysis["unpriced"]

    sign  = "+" if delta >= 0 else "−"
    color_emoji = "🔴" if delta > 500 else ("🟡" if delta > 100 else ("🟢" if delta < 0 else "⚪"))
    delta_str = f"**{sign}${abs(delta):,.2f}/mo**"

    lines = [
        f"<!-- {COMMENT_TAG} -->",
        f"## {color_emoji} nable Cost Estimate",
        "",
        "| | Monthly | Annual |",
        "|---|---|---|",
        f"| **Cost delta** | {delta_str} | **{sign}${abs(delta*12):,.0f}/yr** |",
    ]

    if adds:
        lines += ["", "### ➕ Added resources", "", "| Resource | Monthly | Detail |", "|---|---|---|"]
        for a in sorted(adds, key=lambda x: x["monthly"], reverse=True):
            tip = " ⚠️" if a.get("note") else ""
            lines.append(f"| `{a['resource']}`{tip} | ${a['monthly']:,.2f} | {a['detail']} |")

    if removes:
        lines += ["", "### ➖ Removed resources", "", "| Resource | Saving | Detail |", "|---|---|---|"]
        for r in sorted(removes, key=lambda x: x["monthly"], reverse=True):
            lines.append(f"| `{r['resource']}` | −${r['monthly']:,.2f} | {r['detail']} |")

    # Savings tips
    tips = [a["note"] for a in adds if a.get("note")]
    if tips:
        lines += ["", "### 💡 Savings tips", ""]
        for tip in tips:
            lines.append(f"- {tip}")

    if unpriced:
        lines += ["", f"<details><summary>{len(unpriced)} resource(s) not priced</summary>", ""]
        for u in unpriced[:20]:
            lines.append(f"- `{u}`")
        lines.append("</details>")

    lines += [
        "",
        "---",
        "_[nable](https://github.com/getnable/finopsmcp) · prices: AWS on-demand us-east-1 · "
        "[configure cost gate](https://nable.dev/docs/cost-gate)_",
    ]
    return "\n".join(lines)


def _upsert_comment(
    owner: str,
    repo: str,
    pr_number: int,
    body: str,
    headers: dict,
) -> None:
    """Post or update the nable cost comment (find existing by HTML tag)."""
    comments_url = f"{GITHUB_API_BASE}/repos/{owner}/{repo}/issues/{pr_number}/comments"
    existing = _gh_get(comments_url, headers)

    existing_id = None
    for comment in existing:
        if f"<!-- {COMMENT_TAG} -->" in comment.get("body", ""):
            existing_id = comment["id"]
            break

    if existing_id:
        _gh_patch(
            f"{GITHUB_API_BASE}/repos/{owner}/{repo}/issues/comments/{existing_id}",
            headers, {"body": body},
        )
        log.info("Updated nable cost comment %d on PR #%d", existing_id, pr_number)
    else:
        _gh_post(comments_url, headers, {"body": body})
        log.info("Posted nable cost comment on PR #%d", pr_number)


def _post_check_run(
    owner: str,
    repo: str,
    sha: str,
    delta: float,
    headers: dict,
) -> None:
    """Post a GitHub Checks run — pass if delta < gate, fail if above."""
    passed    = delta <= COST_GATE_USD
    conclusion = "success" if passed else "failure"
    title      = (
        f"Cost estimate: +${delta:,.2f}/mo" if delta >= 0 else f"Cost saving: −${abs(delta):,.2f}/mo"
    )
    summary    = (
        f"Monthly cost delta is within the ${COST_GATE_USD:,.0f}/mo gate." if passed
        else f"Monthly cost delta ${delta:,.2f} exceeds the ${COST_GATE_USD:,.0f}/mo gate. "
             f"Review the PR comment for details or adjust NABLE_COST_GATE_USD."
    )

    _gh_post(
        f"{GITHUB_API_BASE}/repos/{owner}/{repo}/check-runs",
        headers,
        {
            "name":        "nable / cost estimate",
            "head_sha":    sha,
            "status":      "completed",
            "conclusion":  conclusion,
            "completed_at": datetime.now(timezone.utc).isoformat() + "Z",
            "output": {"title": title, "summary": summary},
        },
    )


# ── Webhook handler ───────────────────────────────────────────────────────────

def verify_signature(payload: bytes, sig_header: str, secret: str) -> bool:
    """Validate GitHub webhook HMAC-SHA256 signature."""
    expected = "sha256=" + hmac.new(
        secret.encode(), payload, hashlib.sha256
    ).hexdigest()
    return hmac.compare_digest(expected, sig_header or "")


def handle_pull_request_event(payload: dict, installation_id: int | None = None) -> dict[str, Any]:
    """
    Process a pull_request webhook event.

    Args:
        payload:         parsed JSON from GitHub webhook
        installation_id: GitHub App installation ID (for token generation)

    Returns status dict.
    """
    action = payload.get("action", "")
    if action not in ("opened", "synchronize", "reopened"):
        return {"status": "skipped", "reason": f"action={action}"}

    pr    = payload["pull_request"]
    repo  = payload["repository"]
    owner = repo["owner"]["login"]
    repo_name = repo["name"]
    # Validate path segments at the trust boundary, before any value reaches an
    # api.github.com URL, and cast ids to int so they can't carry path text.
    if not (_valid_segment(owner) and _valid_segment(repo_name)):
        return {"status": "rejected", "reason": "invalid owner/repo"}
    try:
        pr_number = int(pr["number"])
        inst_id = int(installation_id or payload.get("installation", {}).get("id"))
    except (TypeError, ValueError):
        return {"status": "rejected", "reason": "invalid pr/installation id"}
    head_sha  = pr["head"]["sha"]

    headers = _headers(inst_id)

    # Fetch changed files
    files_url = f"{GITHUB_API_BASE}/repos/{owner}/{repo_name}/pulls/{pr_number}/files"
    pr_files  = _gh_get(files_url, headers)

    tf_files = [f for f in pr_files if f.get("filename", "").endswith(".tf")]
    if not tf_files:
        return {"status": "skipped", "reason": "no .tf files changed"}

    # Estimate cost delta
    analysis = _diff_tf_costs(tf_files, headers)
    comment  = _format_comment(analysis, f"{owner}/{repo_name}", pr_number)

    _upsert_comment(owner, repo_name, pr_number, comment, headers)
    _post_check_run(owner, repo_name, head_sha, analysis["delta"], headers)

    return {
        "status":  "ok",
        "pr":      pr_number,
        "delta":   analysis["delta"],
        "tf_files": len(tf_files),
    }


# ── FastAPI / Flask webhook endpoint (optional standalone server) ──────────────

def make_webhook_app():
    """
    Returns a FastAPI app that handles GitHub webhooks.
    Mount at /webhooks/github in your main server, or run standalone.
    """
    try:
        from fastapi import FastAPI, Request, HTTPException
        import uvicorn
    except ImportError:
        raise ImportError("pip install fastapi uvicorn")

    app = FastAPI(title="nable GitHub webhook")
    WEBHOOK_SECRET = os.environ.get("GITHUB_WEBHOOK_SECRET", "")

    @app.post("/webhooks/github")
    async def github_webhook(request: Request):
        payload_bytes = await request.body()

        # Fail closed: a missing secret must reject, never process unauthenticated
        # (webhook.py does the same). An unset secret would otherwise let anyone
        # POST a forged pull_request payload and drive the GitHub API calls below.
        if not WEBHOOK_SECRET:
            raise HTTPException(status_code=503, detail="GITHUB_WEBHOOK_SECRET not configured")
        sig = request.headers.get("X-Hub-Signature-256", "")
        if not verify_signature(payload_bytes, sig, WEBHOOK_SECRET):
            raise HTTPException(status_code=401, detail="Invalid signature")

        event   = request.headers.get("X-GitHub-Event", "")
        payload = json.loads(payload_bytes)

        if event == "pull_request":
            result = handle_pull_request_event(payload)
            return result

        return {"status": "ignored", "event": event}

    return app
