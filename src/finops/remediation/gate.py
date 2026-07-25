"""Opt-in gate for outward remediation: whether nable may open PRs in a repo.

This is a coarse kill-switch, distinct from the advisory action gate in
``policy.py`` (which classifies a proposed action as allow/block/escalate). This
answers a blunter question a security team asks: "is nable permitted to push a
branch and open a pull request in our repositories at all?"

Default is OFF. nable is propose-only, but "propose" here means writing to the
customer's source repo, so an enterprise should be able to assert the restriction
as declared config, not rely on the absence of a GitHub token. dry-run (diff only)
and patch-only (local files, no git) paths are never gated by this: they touch
nothing outside the box.

Enable with either:
  - FINOPS_REMEDIATION_ENABLED=true            (env, mirrors FINOPS_CLEANUP_ENABLED)
  - remediation.open_prs: true  in nable.policy.yaml   (policy-as-code, when present)
"""
from __future__ import annotations

import os
from typing import Any

_TRUE = ("true", "1", "yes", "on")


def remediation_pr_enabled() -> bool:
    """Return True only if opening PRs has been explicitly enabled.

    Env wins if set. Otherwise consult nable.policy.yaml (remediation.open_prs)
    when the policy file and a YAML parser are available. Absent both, OFF.
    """
    env = os.getenv("FINOPS_REMEDIATION_ENABLED", "").strip().lower()
    if env:
        return env in _TRUE
    return _policy_allows()


def _policy_allows() -> bool:
    """Best-effort read of remediation.open_prs from nable.policy.yaml.

    Fails closed: any missing file, parse error, or missing key means OFF.
    """
    path = os.getenv("FINOPS_POLICY_FILE", "nable.policy.yaml")
    try:
        import yaml  # type: ignore
    except Exception:
        return False
    try:
        with open(path) as f:
            doc = yaml.safe_load(f) or {}
    except (OSError, Exception):
        return False
    if not isinstance(doc, dict):
        return False
    rem = doc.get("remediation")
    if not isinstance(rem, dict):
        return False
    return rem.get("open_prs") is True


def disabled_response(*, dry_run_hint: bool = True) -> dict[str, Any]:
    """Standard payload returned when a PR-open path is called while disabled."""
    msg = (
        "Remediation PRs are disabled (opt-in safety gate). nable will not open a "
        "pull request in your repositories until you enable it. "
        "Set FINOPS_REMEDIATION_ENABLED=true, or add `remediation: {open_prs: true}` "
        "to nable.policy.yaml."
    )
    if dry_run_hint:
        msg += (
            " You can still preview changes without this: pass dry_run=True to see the "
            "diff, or patch_only=True to write the .tf files locally and use your own git flow."
        )
    return {"error": "remediation_disabled", "message": msg, "pr_url": None}
