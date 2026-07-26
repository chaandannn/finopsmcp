"""Agent cost controls: the pure helpers behind the pre-action gate.

An agent calls `check_action_policy` before it acts. These functions add the parts
that make the verdict useful in the agent's loop: a cheaper alternative when one
genuinely exists, the remediation posture (propose by default; auto is a later
opt-in mode), and a data-age helper so a budget verdict can say how fresh it is.

Pure and import-light on purpose: no server or DB import here, so it stays testable
in environments where the full MCP server import cannot load.
"""
from __future__ import annotations

import os
import re
from datetime import datetime, timezone
from typing import Any

# Spot instances run well below on-demand for interruptible workloads. The exact
# discount varies by instance type and Availability Zone; ~70% off on-demand is a
# widely-cited typical figure. Every number derived from it is labeled an estimate
# and never presented as a measured saving.
_SPOT_OF_ONDEMAND = 0.30  # spot price ~= 30% of on-demand (a ~70% discount)

# Recognizes an EC2/RDS-style instance type, ANCHORED to a real size suffix so it
# does not match storage classes ("s3.standard"), version strings ("v2.0"), or
# arbitrary dotted tokens. Matches m5.large, r6g.4xlarge, m5.16xlarge, t3.micro,
# db.r6g.xlarge, c5.metal.
_INSTANCE_RE = re.compile(
    r"\b(?:db\.|cache\.)?[a-z]+\d[a-z]*\.(?:nano|micro|small|medium|large|\d*xlarge|metal)\b",
    re.IGNORECASE,
)
# GPU, accelerator, and bare-metal families. Spot for these is capacity-constrained
# and its discount varies far from the ~70% average, so a blended spot estimate
# would mislead. We only offer spot for general/compute/memory families.
_NON_SPOT_RE = re.compile(
    r"\bmetal\b|\b(?:p\d|g\d|inf\d|trn\d|dl\d)[a-z]*\.", re.IGNORECASE
)


def suggest_cheaper_path(
    breakdown: list[dict[str, Any]] | None,
    monthly_delta_usd: float | None,
) -> dict[str, Any] | None:
    """A conservative, honest cheaper alternative for a proposed change, or None.

    v1 covers the common, high-value case: a compute (instance) ADD. For those it
    offers the spot-priced equivalent as a clearly labeled estimate. It returns None
    for savings, non-compute changes, and anything it cannot price honestly, so the
    agent never sees a fabricated number.
    """
    if not breakdown or monthly_delta_usd is None or monthly_delta_usd <= 0:
        return None

    compute_add = 0.0
    resources: list[str] = []
    for line in breakdown:
        if not isinstance(line, dict) or line.get("action") != "add":
            continue
        delta = float(line.get("monthly_delta") or 0.0)
        if delta <= 0:
            continue
        blob = f"{line.get('resource_type', '')} {line.get('detail', '')}"
        if not _INSTANCE_RE.search(blob):
            continue  # not a recognizable compute instance add
        if _NON_SPOT_RE.search(blob):
            continue  # GPU / accelerator / metal: a blended spot estimate would mislead
        compute_add += delta
        resources.append(str(line.get("address") or line.get("resource_type") or "resource"))

    if compute_add <= 0:
        return None

    spot_monthly = round(compute_add * _SPOT_OF_ONDEMAND, 2)
    saving = round(compute_add - spot_monthly, 2)
    if saving <= 0:
        return None

    return {
        "summary": "Run the added compute on spot instead of on-demand.",
        "estimated_monthly_usd": spot_monthly,
        "estimated_saving_usd": saving,
        "applies_to": resources[:5],
        "basis": ("Spot is typically ~70% below on-demand, but it varies by instance "
                  "type and Availability Zone, so this is an estimate, not a measured "
                  "saving. Spot suits interruptible workloads."),
        "is_estimate": True,
    }


def remediation_status() -> dict[str, Any]:
    """The remediation posture for this instance. Default 'propose' (advisory).

    'auto' is the opt-in, bounded mode specced for a later release. Until it ships,
    `applied` is always False and nable only proposes, even if the env asks for auto.
    """
    mode = (os.environ.get("FINOPS_REMEDIATION_MODE") or "propose").strip().lower()
    if mode not in ("propose", "auto"):
        mode = "propose"
    out: dict[str, Any] = {"mode": mode, "applied": False}
    if mode == "auto":
        out["note"] = ("auto-remediation is not enabled in this build; nable is "
                       "proposing. A human still approves and applies every change.")
    return out


def data_age_hours(as_of_iso: str | None) -> float | None:
    """Whole-tenths of hours between an ISO-8601 timestamp and now (UTC), or None."""
    if not as_of_iso:
        return None
    try:
        ts = datetime.fromisoformat(str(as_of_iso).replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return None
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    hours = (datetime.now(timezone.utc) - ts).total_seconds() / 3600
    return round(max(0.0, hours), 1)  # clamp: a future timestamp (clock skew) reads as age 0


def agent_team_status() -> dict[str, Any]:
    """The agent team in one compact, local-only payload: what each agent does,
    whether it is active on this install, and the one step that finishes setup.

    Three agents, all propose-only:
      Budget Guard     gates agent actions (cost + budget + policy + your history)
      Savings Analyst  judges genuine savings on your real rates, drafts the PR
      the Ledger       records decisions, verifies savings landed, learns you

    Every check is cheap and local (license file, settings.json, SQLite counts).
    Never raises; a failed check reads as needs_setup with the step shown.
    """
    from .license import get_status, feature_available

    try:
        plan = get_status().mode
    except Exception:
        plan = "free"
    # Per-agent, not one shared flag. This used to read feature_available(
    # "agent_gate") once and apply it to all three, which silently coupled the
    # Analyst and the Ledger to a feature that has nothing to do with them. When
    # the guard became free-forever that coupling would have un-gated both.
    def _has(feature: str) -> bool:
        try:
            return feature_available(feature)
        except Exception:
            return False

    guard_free = True                      # the guard is the front door, never gated
    analyst_pro = _has("remediation")
    ledger_pro = _has("agent_learning")

    unlock = ("Unlock the agent team: finops login in a terminal, or ask your AI "
              "to run activate_pro. 7-day free trial at getnable.com/pricing.")

    # ── Budget Guard ────────────────────────────────────────────────────────────
    guard_setup: list[str] = []
    if guard_free:
        try:
            from . import guard as _guard
            hook = (_guard.is_installed(_guard._settings_path(False))
                    or _guard.is_installed(_guard._settings_path(True)))
        except Exception:
            hook = False
        if not hook:
            guard_setup.append("nable guard install  (auto-checks agent commands in Claude Code)")
        try:
            from .budget.enforcer import list_budgets
            if not list_budgets():
                guard_setup.append('ask: "set a monthly budget of $X" (gives the gate a budget to enforce)')
        except Exception:
            pass
    budget_guard = {
        "agent": "Budget Guard",
        "does": "Your agents ask it before they act: cost + budget + policy + what you approve, as allow/block/escalate.",
        # No "pro_required" branch: the guard works without a license, so its
        # status can only be active or needs_setup.
        "status": "active" if not guard_setup else "needs_setup",
        "setup": guard_setup,
        "try": 'ask: "check this terraform plan against our cost guardrails"',
    }

    # ── Savings Analyst ─────────────────────────────────────────────────────────
    analyst_setup: list[str] = []
    if analyst_pro:
        try:
            from .demo_data import _real_provider_connected
            if not _real_provider_connected():
                analyst_setup.append("connect a cloud account: ask your AI to run connect_aws (or finops aws)")
        except Exception:
            pass
    savings_analyst = {
        "agent": "Savings Analyst",
        "does": "Finds savings, judges which are genuine on your real rates, and drafts the fix as a PR you approve.",
        "status": ("active" if analyst_pro and not analyst_setup
                   else "needs_setup" if analyst_pro else "pro_required"),
        "setup": analyst_setup if analyst_pro else [unlock],
        "try": 'ask: "find our genuine savings and open a PR for the top one"',
    }

    # ── the Ledger ──────────────────────────────────────────────────────────────
    warmth = "cold"
    resolved = 0
    if ledger_pro:
        try:
            from .recommendations.savings_tracker import get_summary
            counts = (get_summary() or {}).get("counts", {}) or {}
            resolved = sum(int(counts.get(k, 0) or 0)
                           for k in ("acted_on", "verified", "dismissed"))
            warmth = "warm" if resolved >= 5 else "warming" if resolved >= 1 else "cold"
        except Exception:
            pass
    ledger_setup = []
    if ledger_pro and warmth == "cold":
        ledger_setup.append("act on a recommendation, then: mark it acted on and run verify_savings; "
                            "the team learns from every decision")
    ledger = {
        "agent": "the Ledger",
        "does": "Records every decision, verifies savings landed on the next bill, and teaches the other two what you approve.",
        "status": ("active" if ledger_pro and not ledger_setup
                   else "needs_setup" if ledger_pro else "pro_required"),
        "learning": {"state": warmth.upper(), "decisions_recorded": resolved} if ledger_pro else None,
        "setup": ledger_setup if ledger_pro else [unlock],
        "try": 'ask: "what has nable learned from my decisions?"',
    }

    return {
        "plan": plan,
        "agents": [budget_guard, savings_analyst, ledger],
        "note": ("Propose-only: every agent advises and drafts; a human approves and "
                 "applies. Nothing here executes in your cloud on its own."),
    }
