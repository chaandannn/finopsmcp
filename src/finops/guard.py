"""Seamless agent cost guardrail: a PreToolUse hook for AI coding agents.

`finops guard install` wires nable's advisory policy gate (policy.py) into
Claude Code so it runs automatically whenever an agent is about to execute an
infrastructure-mutating shell command (terraform destroy, kubectl delete,
aws ec2 terminate-instances, a commitment purchase, ...). The agent no longer
has to remember to call check_action_policy; the harness enforces the check.

Verdict mapping (advisory, propose-only stays intact):
  escalate -> "ask"   the human sees the command plus the policy reason
  block    -> "deny"  the agent is told why and proposes something else
  allow    -> silent  zero friction, the command runs as normal

The hook never executes anything itself and it fails open: any internal error
exits 0 so a guard bug can never break the user's agent.

Strict mode (FINOPS_GUARD_STRICT=1) additionally asks on reversible
mutations (terraform apply, helm upgrade, kubectl apply/scale,
aws ec2 run-instances) with a nudge to cost the change first.
"""
from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path
from typing import Any

from .policy import GATE_ALLOW, GATE_BLOCK, GATE_ESCALATE, evaluate_action_gate

# ── Command classification ─────────────────────────────────────────────────────
# Ordered: first match wins. Maps shell commands to the policy action types in
# policy.py. Over-matching is tolerable (worst case an unnecessary confirm);
# missing a one-way door is not, so patterns are deliberately broad.

_ONE_WAY_CLASSIFIERS: list[tuple[str, str]] = [
    (r"\bterraform\s+(?:\S+\s+)*destroy\b", "delete_resource"),
    (r"\btofu\s+(?:\S+\s+)*destroy\b", "delete_resource"),
    # destroy hidden behind the apply verb: `terraform apply -destroy` is destroy.
    # Must sit in the one-way list (checked first) or the two-way apply pattern
    # would classify it as a reversible mutation.
    (r"\b(?:terraform|tofu)\s+(?:\S+\s+)*apply\b[^|;&]*\s-destroy\b", "delete_resource"),
    (r"\bpulumi\s+(?:\S+\s+)*destroy\b", "delete_resource"),
    (r"\beksctl\s+delete\b", "delete_resource"),
    # bucket/object wipes: `aws s3 rb` removes a bucket, `aws s3 rm --recursive`
    # empties one; gsutil is the GCP equivalent. Data deletion is a one-way door.
    (r"\baws\s+s3\s+r[mb]\b", "delete_resource"),
    (r"\bgsutil\s+(?:-\S+\s+)*r[mb]\b", "delete_resource"),
    (r"\bhelm\s+(?:uninstall|delete)\b", "delete_resource"),
    (r"\bkubectl\s+(?:\S+\s+)*delete\b", "delete_resource"),
    (r"\baws\s+ec2\s+terminate-instances\b", "terminate_instance"),
    (r"\baws\s+ec2\s+release-address\b", "release_ip"),
    (r"\baws\s+ec2\s+delete-snapshot\b", "snapshot_delete"),
    (r"\baws\s+(?:savingsplans\s+create-savings-plan|"
     r"ec2\s+purchase-reserved-instances-offering|"
     r"ec2\s+purchase-host-reservation|"
     r"rds\s+purchase-reserved-db-instances-offering)", "purchase_commitment"),
    (r"\baws\s+\S+\s+delete-[a-z0-9-]+", "delete_resource"),
    (r"\bgcloud\s+(?:\S+\s+)*delete\b", "delete_resource"),
    (r"\baz\s+(?:\S+\s+)*delete\b", "delete_resource"),
]

_TWO_WAY_CLASSIFIERS: list[tuple[str, str]] = [
    (r"\baws\s+ec2\s+stop-instances\b", "stop_idle"),
    (r"\bterraform\s+(?:\S+\s+)*apply\b", "infra_apply"),
    (r"\btofu\s+(?:\S+\s+)*apply\b", "infra_apply"),
    (r"\bhelm\s+(?:install|upgrade)\b", "infra_apply"),
    (r"\bkubectl\s+(?:apply|scale)\b", "infra_apply"),
    (r"\baws\s+ec2\s+run-instances\b", "infra_apply"),
]


def classify_command(command: str) -> tuple[str, str] | None:
    """Classify a shell command as ("one_way"|"two_way", action_type), or None
    when it is not an infrastructure mutation nable cares about."""
    cmd = " ".join(command.split())  # normalize whitespace
    for pattern, action in _ONE_WAY_CLASSIFIERS:
        if re.search(pattern, cmd):
            return ("one_way", action)
    for pattern, action in _TWO_WAY_CLASSIFIERS:
        if re.search(pattern, cmd):
            return ("two_way", action)
    return None


def _strict() -> bool:
    return os.getenv("FINOPS_GUARD_STRICT", "").strip().lower() in ("1", "true", "yes")


# ── Command cost estimation ────────────────────────────────────────────────────
# The gap this closes: `aws ec2 run-instances --instance-type p4d.24xlarge
# --count 8` (~$191k/mo) classified as a reversible in-policy mutation and the
# guard stayed silent, while policy.py's dollar threshold sat unreachable
# because nothing on the shell path ever computed a dollar figure. Reversible
# is not the same as cheap.

_RUN_INSTANCES_RE = re.compile(r"\baws\s+ec2\s+run-instances\b")
_INSTANCE_TYPE_RE = re.compile(r"--instance-type[=\s]+([a-z0-9]+\.[a-z0-9]+)")
# `--count 8` or the min:max form `--count 2:8`; price the max, because the
# guard's job is the ceiling a human is about to authorise, not the floor.
_COUNT_RE = re.compile(r"--count[=\s]+(\d+)(?::(\d+))?")


def estimate_command_monthly_cost(command: str) -> dict[str, Any] | None:
    """A local, list-price monthly estimate for a shell command, or None.

    Covers `aws ec2 run-instances` with an instance type in the local price
    table (on-demand us-east-1 list, the same table the Terraform estimator
    uses). Anything unpriceable returns None: an unknown type must degrade to
    the guard's existing behaviour, never to an invented figure.
    """
    cmd = " ".join(command.split())
    if not _RUN_INSTANCES_RE.search(cmd):
        return None
    m = _INSTANCE_TYPE_RE.search(cmd)
    if not m:
        return None
    itype = m.group(1)
    try:
        from .connectors.terraform_estimate import HOURS_PER_MONTH, _EC2_HOURLY
        hourly = _EC2_HOURLY.get(itype)
    except Exception:
        return None
    if not hourly:
        return None
    count = 1
    cm = _COUNT_RE.search(cmd)
    if cm:
        count = max(int(cm.group(1)), int(cm.group(2) or 0)) or 1
    monthly = hourly * count * HOURS_PER_MONTH
    return {
        "monthly_usd": round(monthly, 2),
        "hourly_usd": hourly,
        "instance_type": itype,
        "count": count,
        "basis": "on-demand us-east-1 list price",
    }


def _cost_line(est: dict[str, Any]) -> str:
    return (f"{est['count']}x {est['instance_type']} at "
            f"${est['hourly_usd']:,.2f}/hr ({est['basis']}) is "
            f"~${est['monthly_usd']:,.0f}/mo")


def _prod_context(command: str) -> bool:
    """Does this command look aimed at production?

    Word-boundary match so 'product' never trips it. Patterns are overridable
    (comma-separated regexes) via FINOPS_GUARD_PROD_PATTERNS; set it to 'off'
    to disable production-context confirmation entirely. Over-matching costs an
    unnecessary confirm, which is the tolerable failure direction here.
    """
    raw = os.getenv("FINOPS_GUARD_PROD_PATTERNS", "")
    if raw.strip().lower() == "off":
        return False
    patterns = [p.strip() for p in raw.split(",") if p.strip()] or [r"\bprod(uction)?\b"]
    for pat in patterns:
        try:
            if re.search(pat, command, re.IGNORECASE):
                return True
        except re.error:
            continue
    return False


def _stop_on_budget() -> bool:
    """Whether a blown AI budget HARD-STOPS the agent (deny) or just asks.

    The user is asked once, at `nable guard install`, and the answer is stored on
    the budget as `on_breach`. The env var overrides it for a single session or a
    CI run. Default is notify: a tool that silently halts your agent gets
    uninstalled before anyone finds the setting that caused it, so stopping has
    to be something you chose.
    """
    env = os.getenv("FINOPS_GUARD_STOP_ON_BUDGET", "").strip().lower()
    if env in ("1", "true", "yes"):
        return True
    if env in ("0", "false", "no"):
        return False
    try:
        from .ai_budget import get_budget
        return (get_budget().get("on_breach") or "notify") == "stop"
    except Exception:
        return False


def check_budget_gate() -> dict[str, Any] | None:
    """Stop the agent when its own token spend is over the budget the user set.

    This runs BEFORE command classification and applies to every tool call, not
    just infrastructure ones. "Stop the agent because it is spending too much"
    means stop it, not stop it from touching Terraform.

    Reads the local Claude Code session logs (ai_budget), so it needs no cloud
    account, no API key and no network. Returns None when no budget is set, when
    usage is under it, or on ANY error: a guard that cannot read its own budget
    must not take a position.
    """
    try:
        from .ai_budget import BUDGET_OVER, status
        st = status()
        if st.get("verdict") != BUDGET_OVER:
            return None
        budget = st.get("budget") or {}
        pct = st.get("pct_of_budget")
        over = f"{pct * 100:.0f}% of" if isinstance(pct, (int, float)) else "over"
        if st.get("verdict_basis") == "spend":
            detail = (f"~${st.get('est_usd_mtd_list_price', 0):,.0f} estimated this month, "
                      f"{over} your ${budget.get('spend_cap', 0):,.0f} cap")
        else:
            detail = (f"{st.get('billable_tokens_mtd', 0):,} tokens this month, "
                      f"{over} your {budget.get('monthly_tokens', 0):,} budget")
        hard = _stop_on_budget()
        return {
            "decision": "deny" if hard else "ask",
            "action_type": "ai_budget",
            "reason": (
                f"nable guard: your agent is over its AI budget. {detail}. "
                + ("Stopped because FINOPS_GUARD_STOP_ON_BUDGET is on. "
                   "Raise it with `nable ai-budget set`, or unset that variable to "
                   "downgrade this to a confirmation."
                   if hard else
                   "Confirm to continue, or raise it with `nable ai-budget set`. "
                   "Set FINOPS_GUARD_STOP_ON_BUDGET=1 to make this a hard stop.")
            ),
        }
    except Exception:
        return None  # unreadable budget is not a reason to block anyone


def gate_command(command: str) -> dict[str, Any] | None:
    """Evaluate a shell command against the policy gate.

    Returns None when the guard has no opinion (not infra, or an in-policy
    reversible action), else {decision: "ask"|"deny", reason, action_type}.
    """
    # The AI budget stop comes first and is not conditioned on the command: an
    # agent burning through its budget should be stopped whatever it is doing.
    budget_hit = check_budget_gate()
    if budget_hit is not None:
        return budget_hit

    hit = classify_command(command)
    if hit is None:
        return None
    door, action_type = hit

    if action_type == "infra_apply":
        # Reversible mutation. Zero friction by default; strict mode confirms,
        # and a production context always confirms: practitioners run agents
        # loose in staging but want a human nod before prod changes.
        #
        # Priceable commands additionally go through the policy's dollar
        # threshold: reversible does not mean cheap, and launching 8x
        # p4d.24xlarge is a ~$191k/mo decision whichever door it is. The
        # estimate rides the same evaluate_action_gate as everything else, so
        # the user's FINOPS_POLICY_MAX_AUTO_USD and learned adjustments apply.
        est = estimate_command_monthly_cost(command)
        if est is not None:
            verdict = evaluate_action_gate(action_type,
                                           monthly_delta_usd=est["monthly_usd"])
            if verdict.get("gate") != GATE_ALLOW:
                return {
                    "decision": "ask" if verdict.get("gate") == GATE_ESCALATE else "deny",
                    "action_type": action_type,
                    "monthly_delta_usd": est["monthly_usd"],
                    "reason": (f"nable guard: {_cost_line(est)}. "
                               f"{verdict.get('reason', 'a human must review this action.')}"),
                }
        if _strict():
            cost = f" {_cost_line(est)}." if est else ""
            return {
                "decision": "ask",
                "action_type": action_type,
                "reason": ("nable guard (strict): this changes infrastructure and "
                           f"therefore the bill.{cost} Cost it first (ask nable to "
                           "estimate_change_cost) or confirm to proceed."),
            }
        if _prod_context(command):
            cost = f" {_cost_line(est)}." if est else ""
            return {
                "decision": "ask",
                "action_type": action_type,
                "reason": ("nable guard: this mutates infrastructure in what looks "
                           f"like a PRODUCTION context.{cost} Confirm to proceed, or "
                           "cost it first (ask nable to estimate_change_cost)."),
            }
        return None

    verdict = evaluate_action_gate(action_type)
    gate = verdict.get("gate")
    if gate == GATE_ESCALATE:
        return {
            "decision": "ask",
            "action_type": action_type,
            "reason": f"nable guard: {verdict.get('reason', 'a human must review this action.')}",
        }
    if gate == GATE_BLOCK:
        return {
            "decision": "deny",
            "action_type": action_type,
            "reason": f"nable guard: {verdict.get('reason', 'this action is not in your policy allowlist.')}",
        }
    return None  # allow -> stay silent


# ── Claude Code hook protocol ──────────────────────────────────────────────────

def run_hook(stdin: Any = None, stdout: Any = None) -> int:
    """PreToolUse hook body: JSON in on stdin, optional JSON verdict on stdout.

    Fails open by design: any error, unknown payload, or non-Bash tool exits 0
    with no output so the guard can never break the user's agent.
    """
    stdin = stdin or sys.stdin
    stdout = stdout or sys.stdout
    try:
        payload = json.load(stdin)
        if payload.get("tool_name") != "Bash":
            return 0
        command = (payload.get("tool_input") or {}).get("command") or ""
        if not command:
            return 0
        verdict = gate_command(command)
        if not verdict:
            return 0
        json.dump({
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "permissionDecision": verdict["decision"],
                "permissionDecisionReason": verdict["reason"],
            }
        }, stdout)
        return 0
    except Exception:
        return 0


# ── Installer ──────────────────────────────────────────────────────────────────

# Marker used to find our hook in settings regardless of how the command is
# prefixed (bare, absolute path, or uvx wrapper).
_HOOK_MARKER = "guard hook"
_HOOK_CMD = "finops guard hook"


_UVX_HOOK_CMD = "uvx --from finops-mcp finops guard hook"


def _is_ephemeral(path: str) -> bool:
    """True when `path` lives somewhere that is not ours to depend on.

    `uvx nable guard install` runs from uv's content-addressed archive cache
    (…/uv/archive-v0/<hash>/bin/finops). That path is real while the command is
    running and is a trap to persist: uv garbage-collects it on `uv cache clean`
    or `uv cache prune`, and the hash changes on the next release. Baking it into
    settings.json produces a hook that works today, dies silently later (Claude
    Code fails open on a hook that cannot execute), and still reports itself as
    installed. The user believes they are guarded and they are not.
    """
    import tempfile
    p = Path(path)
    if "archive-v0" in p.parts:          # uv's content-addressed store
        return True
    roots = [os.getenv("UV_CACHE_DIR"),
             str(Path.home() / ".cache" / "uv"),
             str(Path.home() / "Library" / "Caches" / "uv"),
             tempfile.gettempdir()]
    for r in roots:
        if not r:
            continue
        try:
            if p.resolve().is_relative_to(Path(r).resolve()):
                return True
        except (OSError, ValueError):
            continue
    return False


def _hook_command() -> str:
    """The command Claude Code should run for the hook, resolved to something
    that will still exist tomorrow.

    A uvx user has no `finops` on PATH afterwards, so a bare command would fail
    with command-not-found on every Bash call. A persistent binary is best. An
    ephemeral one is worse than none, because it fails open and lies about it, so
    those fall through to the uvx form, which re-resolves at run time."""
    import shutil
    found = shutil.which("finops")
    if found and not _is_ephemeral(found):
        # Quote in case the path has spaces (framework installs on macOS do not,
        # but user venvs can).
        return f'"{found}" guard hook' if " " in found else f"{found} guard hook"
    return _UVX_HOOK_CMD


def _settings_path(global_scope: bool) -> Path:
    if global_scope:
        return Path.home() / ".claude" / "settings.json"
    return Path.cwd() / ".claude" / "settings.json"


def _refuse(path: Path, what: str):
    """Never guess at a settings file we do not understand. Someone's editor
    config is not ours to repair, and a wrong repair silently breaks their agent."""
    return SystemExit(f"  {path} {what}; fix it first, nothing was changed.")


def _load_settings(path: Path) -> dict:
    if path.exists():
        try:
            data = json.loads(path.read_text())
        except Exception:
            raise _refuse(path, "exists but is not valid JSON")
        # Valid JSON is not the same as the shape we expect. A top-level array or
        # string parses fine and then blows up on .setdefault deeper in, after we
        # may already have decided to write.
        if not isinstance(data, dict):
            raise _refuse(path, f"contains a JSON {type(data).__name__}, not an object")
        return data
    return {}


def _hook_list(settings: dict, path: Path, *, create: bool):
    """The PreToolUse list, or None when there is not one and we are not creating.

    Tolerates the keys being absent or explicitly null (a real state: some tools
    write "hooks": null). Refuses when they hold a value of the wrong type, for
    the same reason _load_settings does."""
    hooks = settings.get("hooks")
    if hooks is None:
        if not create:
            return None
        hooks = settings["hooks"] = {}
    elif not isinstance(hooks, dict):
        raise _refuse(path, f"has a 'hooks' value that is a {type(hooks).__name__}, not an object")

    pre = hooks.get("PreToolUse")
    if pre is None:
        if not create:
            return None
        pre = hooks["PreToolUse"] = []
    elif not isinstance(pre, list):
        raise _refuse(path, f"has a 'hooks.PreToolUse' value that is a "
                            f"{type(pre).__name__}, not an array")
    return pre


def is_installed(path: Path) -> bool:
    """Read-only predicate, so any structural surprise means "not installed"
    rather than a traceback. Answering False on a file we cannot parse is safe:
    the caller's next move is to install, which refuses loudly on the same file."""
    try:
        s = json.loads(path.read_text())
        for entry in (s.get("hooks") or {}).get("PreToolUse") or []:
            for h in entry.get("hooks") or []:
                cmd = h.get("command") or ""
                if _HOOK_MARKER in cmd and "finops" in cmd:
                    return True
    except Exception:
        return False
    return False


def broken_hook_command(path: Path) -> str | None:
    """Our installed hook command, when the thing it invokes is not runnable.

    Returns None when the hook is absent or healthy. A hook Claude Code cannot
    execute is skipped silently and fails open, so "installed" on its own is not
    a safe thing to report."""
    import shutil
    try:
        s = json.loads(path.read_text())
        for entry in (s.get("hooks") or {}).get("PreToolUse") or []:
            for h in entry.get("hooks") or []:
                cmd = h.get("command") or ""
                if not (_HOOK_MARKER in cmd and "finops" in cmd):
                    continue
                exe = cmd[1:cmd.index('"', 1)] if cmd.startswith('"') else cmd.split()[0]
                # The uvx form re-resolves at run time; it is healthy if uv exists.
                probe = "uvx" if exe == "uvx" else exe
                if shutil.which(probe) or Path(probe).exists():
                    return None
                return cmd
    except Exception:
        return None
    return None


def install(global_scope: bool = False) -> Path:
    """Idempotently add the guard hook to Claude Code settings. Returns the path."""
    path = _settings_path(global_scope)
    settings = _load_settings(path)
    if is_installed(path):
        return path
    pre = _hook_list(settings, path, create=True)
    cmd = _hook_command()
    pre.append({
        "matcher": "Bash",
        # uvx resolves an environment per call; give the cold-cache case room.
        # Timeouts fail open in Claude Code, so a slow first call cannot block.
        "hooks": [{"type": "command", "command": cmd,
                   "timeout": 30 if cmd.startswith("uvx") else 10}],
    })
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(settings, indent=2) + "\n")
    return path


def uninstall(global_scope: bool = False) -> bool:
    """Remove the guard hook. Returns True when something was removed."""
    path = _settings_path(global_scope)
    settings = _load_settings(path)
    pre = _hook_list(settings, path, create=False)
    if not pre:
        return False
    removed = False
    kept = []
    for entry in pre:
        if not isinstance(entry, dict):
            kept.append(entry)          # not ours; leave it exactly as found
            continue
        inner = [h for h in (entry.get("hooks") or [])
                 if not (isinstance(h, dict)
                         and _HOOK_MARKER in (h.get("command") or "")
                         and "finops" in (h.get("command") or ""))]
        if len(inner) != len(entry.get("hooks") or []):
            removed = True
        if inner or not entry.get("hooks"):
            entry["hooks"] = inner
            if inner:
                kept.append(entry)
        else:
            removed = True
    if removed:
        settings["hooks"]["PreToolUse"] = kept
        if not kept:
            del settings["hooks"]["PreToolUse"]
        if not settings.get("hooks"):
            settings.pop("hooks", None)
        path.write_text(json.dumps(settings, indent=2) + "\n")
    return removed
