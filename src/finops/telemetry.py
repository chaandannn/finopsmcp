"""
nable anonymous telemetry — OPT-IN. Nothing is sent unless you switch it on.

What we collect (and only this):
  - A random install ID (UUID4, generated once, stored locally — never your email or keys)
  - Date of the ping (day-level granularity only)
  - Which MCP tools were invoked (feature names, not query content)
  - Number of connected providers (count only, not which accounts)
  - Plan tier: free | trial | pro
  - Which nable version you are running, your Python MAJOR.MINOR (e.g. "3.12"),
    how nable was installed (uvx | pipx | venv | docker | system), and your OS
    family (e.g. "darwin"). Never the install path: it contains your home
    directory and usually your name, so only the one-word classification is sent.

Why that last line exists: a cohort on Python 3.10 got a five-week-old build
that crashed on import, and it was invisible for two months because no event
carried a version or an interpreter. A tool that cannot see which of its own
builds is running cannot tell a quiet outage from low demand.

What we never collect:
  - Cloud account IDs, ARNs, or credentials
  - Cost figures or billing data
  - IP addresses (PostHog is configured to drop them server-side)
  - Email addresses (unless you've identified yourself via the website)

How to turn it ON (it is off by default):
  export NABLE_TELEMETRY=1

How to guarantee it stays off, even if something else sets the above:
  export NABLE_NO_TELEMETRY=1

Flipped to opt-in on 2026-08-08. This runs on an operator's own machine against
their own cloud account, and the people most likely to install it are the least
willing to have it phone home by default. The cost is ours to carry: field
diagnosis gets harder, and a month of scan failures was already invisible partly
because events carried too little. Better than a default nobody agreed to.
"""
from __future__ import annotations

import hashlib
import json
import os
import threading
import uuid
from datetime import date
from pathlib import Path
from typing import Optional

# ─── Config ──────────────────────────────────────────────────────────────────

_POSTHOG_KEY  = os.environ.get("NABLE_POSTHOG_KEY", "phc_zcaQqoAXrSghjtbE6VB83p4RjfmcpqezKWV9GdZy4dPv")
_POSTHOG_HOST = "https://us.i.posthog.com"
_ID_FILE      = Path.home() / ".config" / "finops" / ".install_id"
_CONSENT_FILE = Path.home() / ".config" / "finops" / ".telemetry"
_OPT_OUT_ENV  = "NABLE_NO_TELEMETRY"   # hard override, always wins
_OPT_IN_ENV   = "NABLE_TELEMETRY"      # opt-in: nothing is sent without it


# ─── Consent, asked once, out loud ───────────────────────────────────────────
#
# Telemetry became opt-in on 2026-08-08 (0.8.210) and the trade was deliberate:
# "is telemetry off by default" is a question this product has to be able to
# answer yes to. The cost was not deliberate, because nobody measured it.
# Measured 2026-08-15: ZERO events have ever arrived from 0.8.210 or later,
# while PyPI served 86-630 downloads a day straight through the change. The
# switch did not reduce the signal, it ended it. Every number we have describes
# builds <= 0.8.209, and "no scan has completed since 08-07" turned out to be
# the measurement going dark on 08-08 rather than anything about the product.
#
# Setting an environment variable is not a realistic ask. Asking once, in
# words, is. The promise survives intact: nothing is sent unless the user says
# yes, NABLE_NO_TELEMETRY still hard-overrides, and the honest answer to "is it
# off by default" is still yes.
#
# The prompt also does a second job for free. Only an interactive human can
# answer it. A package-analysis sandbox or a container has no TTY, never sees
# it, and stays silent, which is most of the robot-versus-customer problem
# solved without a classifier: 19 "machines" in 36 hours on 2026-08-14 were one
# per released version, three runs each, seconds apart, never returning.


def _stored_consent() -> bool | None:
    """The answer the user gave once, or None if they have never been asked."""
    try:
        if not _CONSENT_FILE.exists():
            return None
        return _CONSENT_FILE.read_text().strip().lower().startswith("y")
    except Exception:
        return None


def _store_consent(agreed: bool) -> None:
    """Record the answer so the question is asked exactly once, ever."""
    try:
        _CONSENT_FILE.parent.mkdir(parents=True, exist_ok=True)
        _CONSENT_FILE.write_text("yes" if agreed else "no")
        try:
            _CONSENT_FILE.chmod(0o600)
        except OSError:
            pass
    except Exception:
        pass  # a read-only FS just means we ask again next time; never fatal

_PROMPT_ARMED = False


def _can_ask() -> bool:
    """Is there a human at a terminal who could answer, and should we ask?

    Every one of these is a reason NOT to ask, and the TTY pair is the one doing
    the heavy lifting. stdin must be a terminal so a read cannot hang, and stdout
    must be one so the question cannot be swallowed by a pipe or land in the
    middle of `nable scan --json` output someone is parsing.

    The MCP server is the case that matters most: it speaks JSON-RPC over stdio,
    so stdin is a pipe and it can never be prompted. A prompt written into that
    stream would corrupt the protocol.
    """
    import sys

    if _stored_consent() is not None:
        return False                       # asked once already. Once means once.
    if os.environ.get(_OPT_OUT_ENV, "").strip() not in ("", "0", "false", "no"):
        return False                       # they already said no, louder
    if os.environ.get(_OPT_IN_ENV, "").strip() not in ("", "0", "false", "no"):
        return False                       # they already said yes
    if os.environ.get("FINOPS_AIRGAP", "").strip() not in ("", "0", "false", "no"):
        return False
    if is_ci():
        return False
    try:
        return bool(sys.stdin.isatty() and sys.stdout.isatty())
    except Exception:
        return False


def prompt_for_consent() -> bool | None:
    """Ask once. Returns the answer, or None if we did not ask.

    Default is NO: a bare Enter, an EOF, a Ctrl-C or anything unparseable all
    mean no and are recorded as no, so the question is never asked twice. That
    matters more than capturing an ambiguous yes.
    """
    if not _can_ask():
        return None
    try:
        print()
        print("  Share anonymous usage data? It is off unless you say yes.")
        print("  Sent:     which commands ran, how many providers are connected,")
        print("            nable + Python version, OS family.")
        print("  Never:    cost figures, account IDs, resource names, file paths,")
        print("            credentials, or your IP.")
        try:
            answer = input("  Share? [y/N] ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            answer = ""
        agreed = answer.startswith("y")
        _store_consent(agreed)
        print("  Thanks, that genuinely helps." if agreed
              else "  Not sending anything. Change your mind: export NABLE_TELEMETRY=1")
        print()
        return agreed
    except Exception:
        return None      # a prompt is never worth failing a command over


def arm_consent_prompt() -> None:
    """Ask AFTER the command finishes, via atexit, on the interactive CLI only.

    Placement is the whole design here, and it was chosen against two worse
    options.

    Before the command: adds friction to the first run, which the funnel says is
    exactly where people leave. 545 ran, 74 used a tool. Nothing goes in front of
    that.

    After a SUCCESSFUL command only: biases the sample towards people for whom
    nable already worked, which is precisely backwards. The failures are what we
    most need to see, and 162 of 174 scans on record failed.

    atexit fires after the command's output is printed and on every exit path,
    including the SystemExit that `nable scan` raises and an unhandled traceback.
    So it costs the first run nothing, gates nothing, and asks the people who had
    a bad time as readily as the people who did not.

    Armed only from the CLI entry point. The MCP server never calls this, and
    _can_ask would refuse anyway because its stdin is a pipe.
    """
    global _PROMPT_ARMED
    if _PROMPT_ARMED or not _can_ask():
        return
    _PROMPT_ARMED = True
    try:
        import atexit
        atexit.register(prompt_for_consent)
    except Exception:
        pass


# ─── Install ID ──────────────────────────────────────────────────────────────

def _get_install_id() -> str:
    """
    Stable anonymous ID for this install. Stored in ~/.config/finops/.install_id.
    Generated once as a random UUID — completely disconnected from the user's identity.
    """
    try:
        _ID_FILE.parent.mkdir(parents=True, exist_ok=True)
        if _ID_FILE.exists():
            id_ = _ID_FILE.read_text().strip()
            if len(id_) == 36:
                return id_
        id_ = str(uuid.uuid4())
        _ID_FILE.write_text(id_)
        try:
            _ID_FILE.chmod(0o600)
        except OSError:
            pass
        return id_
    except Exception:
        # If we can't write (e.g. read-only FS), generate a session-only ID
        return str(uuid.uuid4())


def _is_opted_out() -> bool:
    """Telemetry is OFF unless explicitly switched on. Opt-in, not opt-out.

    Changed 2026-08-08. It used to send unless NABLE_NO_TELEMETRY was set, and
    that default is wrong for what this tool is: it runs on an operator's own
    machine against their own cloud account, and the people most likely to
    install it are the people least willing to have it phone home by default.
    Asked directly on r/selfhosted whether telemetry was disabled by default;
    "no, but here is what it doesn't collect" is a much weaker answer than "yes".

    The cost is real and worth stating: field diagnosis gets harder. A month of
    scan failures was invisible partly because the events carried too little,
    and this reduces volume on top of that. The trade is deliberate. Anyone who
    wants to help sets one variable, and the events still carry no cost figures,
    no account IDs, no resource names and no paths.

    NABLE_NO_TELEMETRY keeps working as a hard override, so a user who already
    set it in a dotfile or an image stays off no matter what else is configured.
    """
    if not _POSTHOG_KEY:
        return True  # no key configured — silently skip all telemetry
    _airgap = os.environ.get("FINOPS_AIRGAP", "").strip()
    if _airgap not in ("", "0", "false", "no"):
        return True  # air-gap mode: no non-provider outbound allowed
    if is_ci():
        return True  # CI / build runners are not users; never count or ping
    # The explicit opt-out still wins, even against an explicit opt-in: someone
    # who set it once meant it, and a later env var should not quietly undo it.
    _out = os.environ.get(_OPT_OUT_ENV, "").strip()
    if _out not in ("", "0", "false", "no"):
        return True
    _in = os.environ.get(_OPT_IN_ENV, "").strip()
    if _in not in ("", "0", "false", "no"):
        return False        # explicit env opt-in still works, unchanged
    # Otherwise: the answer the user gave when asked, once, in words. None means
    # never asked, which stays OFF. The env var is no longer the only way in,
    # because requiring one is what took the signal to zero.
    stored = _stored_consent()
    if stored is not None:
        return not stored
    return True


# Build and CI runners fire the CLI on every cold job, which used to look like
# a flood of fresh installs. They are never real users, so they never send
# telemetry. Detected from the standard env vars these systems set.
_CI_ENV_VARS = (
    "CI", "CONTINUOUS_INTEGRATION", "GITHUB_ACTIONS", "GITLAB_CI", "CIRCLECI",
    "BUILDKITE", "JENKINS_URL", "TF_BUILD", "TEAMCITY_VERSION", "TRAVIS",
    "APPVEYOR", "DRONE", "CODEBUILD_BUILD_ID", "BITBUCKET_BUILD_NUMBER",
    "RUNNER_OS",
)


def is_ci() -> bool:
    return any(os.environ.get(v) for v in _CI_ENV_VARS)


# ─── Session state (accumulated in-process, flushed periodically) ─────────────

_session: dict = {
    "tools_used": set(),
    "provider_count": 0,
    "plan": "free",
}
_lock = threading.Lock()


def record_tool_call(tool_name: str) -> None:
    """
    Record a tool invocation. Accumulates in-session and fires a lightweight
    PostHog event (fire-and-forget, background thread) for per-tool analytics.
    Thread-safe.
    """
    if _is_opted_out():
        return
    with _lock:
        _session["tools_used"].add(tool_name)
        # Track call counts for frequency analysis
        counts = _session.setdefault("tool_counts", {})
        counts[tool_name] = counts.get(tool_name, 0) + 1

    # Fire a lightweight per-tool event (does not block caller)
    install_id = _get_install_id()
    props = {
        "tool": tool_name,
        "plan": _session.get("plan", "free"),
        "date": date.today().isoformat(),
    }
    t = threading.Thread(
        target=_send_event,
        args=(install_id, "tool_called", props),
        daemon=True,
    )
    t.start()


def set_plan(plan: str) -> None:
    """Call with 'free', 'trial', or 'pro' after license check."""
    with _lock:
        _session["plan"] = plan


def set_provider_count(count: int) -> None:
    with _lock:
        _session["provider_count"] = count


# ─── Heartbeat ────────────────────────────────────────────────────────────────

def _send(install_id: str, properties: dict) -> None:
    """Fire-and-forget POST to PostHog. Runs in background thread."""
    _send_event(install_id, "heartbeat", properties)


# ── runtime facts stamped on every event ──────────────────────────────────────

_RUNTIME_CACHE: dict | None = None


def _install_method() -> str:
    """How this copy of nable was installed, as a coarse label.

    Never the path itself: sys.prefix contains the user's home directory and
    therefore usually their name. Only the classification leaves the machine.
    """
    import sys

    prefix = sys.prefix
    try:
        if os.path.exists("/.dockerenv"):
            return "docker"
        # uv's ephemeral tool envs live under an archive-v0 cache directory;
        # `uv tool install` sets UV_TOOL_NAME in the launcher environment.
        if "/archive-v0/" in prefix or os.environ.get("UV_TOOL_NAME"):
            return "uvx"
        if "pipx" in prefix:
            return "pipx"
        if prefix != getattr(sys, "base_prefix", prefix):
            return "venv"
        return "system"
    except Exception:
        return "unknown"


def _runtime_props() -> dict:
    """Which build, which interpreter, how installed.

    This exists because a broken cohort was invisible for two months. `version`
    was stamped on scan failures only, so 328 machines reported version=None on
    every other event and there was no way to tell a stale install from a fresh
    one. The Python version was not collected at all, which meant the 3.10
    resolution trap could not be seen, sized, or argued about with numbers.

    All three are non-sensitive: a version string, an interpreter version, and a
    one-word install label. No paths, no account data, no cost figures. Computed
    once per process.
    """
    global _RUNTIME_CACHE
    if _RUNTIME_CACHE is None:
        import sys

        try:
            from . import __version__ as _v
        except Exception:
            _v = "unknown"
        _RUNTIME_CACHE = {
            "nable_version": _v,
            "python_version": f"{sys.version_info.major}.{sys.version_info.minor}",
            "install_method": _install_method(),
            "platform": sys.platform,
            **_environment_props(),
        }
    return dict(_RUNTIME_CACHE)


def _environment_props() -> dict:
    """What KIND of thing is running nable. Categories only, never identifiers.

    This exists because the numbers could not tell a customer from a robot. On
    2026-08-14, 19 "machines" appeared in 36 hours: one per released version,
    0.8.201 through 0.8.209 in order, three scans each seconds apart, five of
    them running a complete wizard -> scan -> tool -> heartbeat sequence in under
    a minute, none ever seen again. That is a harness walking the release
    history, and it is indistinguishable in the data from 19 people having a
    terrible first day. Every funnel number is worthless until the two can be
    told apart.

    The consent prompt does most of this work by construction, because only an
    interactive human can answer it. These fields cover what is left, and let the
    classifier be checked rather than believed:

      tty             a human at a terminal, or a pipe
      container       /.dockerenv or /run/.containerenv
      env_kind        uv | venv | pipx | conda | system
      layout          site-packages | source | other
      install_age_s   how old this install ID was when the process started

    install_age_s is the sharpest of them. A returning user's ID is days or
    months old. An ID created four seconds ago that is already emitting a scan
    belongs to an environment that will not exist in a minute. It is a stat() on
    a file we already read.

    None of these identify a person or a machine: no paths, no hostname, no
    username, no IP. Same bar as install_health.install_shape, which this reuses
    rather than restating, because two copies of a rule are only equal on the day
    they are written.
    """
    import sys
    import time

    props: dict = {}
    try:
        from .install_health import install_shape
        shape = install_shape()
        for k in ("env_kind", "layout", "container"):
            if k in shape:
                props[k] = shape[k]
    except Exception:
        pass
    try:
        props["tty"] = bool(sys.stdin.isatty() and sys.stdout.isatty())
    except Exception:
        props["tty"] = False
    try:
        # Age at process start, which is the question that matters: how
        # established was this install when it did the thing we are looking at.
        props["install_age_s"] = max(0, int(time.time() - _ID_FILE.stat().st_mtime))
    except Exception:
        props["install_age_s"] = 0
    return props


def _send_event(install_id: str, event: str, properties: dict) -> None:
    """Send a single named event to PostHog.

    Every caller funnels through here, so the opt-out check lives here too:
    direct call sites (setup wizard, server) must never bypass air-gap, CI,
    or NABLE_NO_TELEMETRY.

    Prefer httpx because it ships certifi's CA bundle. urllib relies on the
    platform OpenSSL trust store, which on python.org macOS builds is empty until
    the user runs "Install Certificates.command", so urllib silently fails cert
    verification and drops every event. That silent loss skews active-install
    counts downward for exactly the macOS-on-python.org segment. httpx avoids it;
    urllib is only a fallback when httpx is not installed.
    """
    if _is_opted_out():
        return
    body = {
        "api_key": _POSTHOG_KEY,
        "event": event,
        "distinct_id": install_id,
        "properties": {
            # Runtime facts first so an explicit property can still override
            # one deliberately, and so EVERY event carries them.
            **_runtime_props(),
            **properties,
            # PostHog is configured to drop $ip server-side
            "$ip": "0.0.0.0",
        },
        "timestamp": date.today().isoformat(),
    }
    try:
        import httpx
        httpx.post(f"{_POSTHOG_HOST}/capture/", json=body, timeout=5)
        return
    except Exception:
        pass
    try:
        import urllib.request
        req = urllib.request.Request(
            f"{_POSTHOG_HOST}/capture/",
            data=json.dumps(body).encode(),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        urllib.request.urlopen(req, timeout=4)
    except Exception:
        pass  # Never let telemetry break the tool


def ping(extra: Optional[dict] = None) -> None:
    """
    Send a single heartbeat. Called once at MCP server startup.
    Runs in a daemon thread so it never blocks tool startup.
    """
    if _is_opted_out():
        return

    install_id = _get_install_id()

    with _lock:
        properties = {
            "plan": _session["plan"],
            "provider_count": _session["provider_count"],
            "tool_count": len(_session["tools_used"]),
            # Hash tool names so we know which features are popular,
            # but the list itself stays local
            "tools_hash": hashlib.sha256(
                ",".join(sorted(_session["tools_used"])).encode()
            ).hexdigest()[:16],
            "date": date.today().isoformat(),
        }

    if extra:
        properties.update(extra)

    t = threading.Thread(target=_send, args=(install_id, properties), daemon=True)
    t.start()


def _startup_surface() -> str:
    """"cli" when a human ran nable in a terminal (TTY stdin), else "mcp_server"
    (an MCP client launched the stdio server with a piped stdin). Pure and
    side-effect free so it is testable without the telemetry send path."""
    import sys
    try:
        return "cli" if sys.stdin.isatty() else "mcp_server"
    except Exception:
        return "mcp_server"


def ping_startup(provider_count: int = 0, plan: str = "free") -> None:
    """Convenience wrapper called from server.py on startup.

    Tags the heartbeat with the surface so the "ran nable but never issued a
    command" cliff is splittable: a piped stdin means an MCP client launched the
    stdio server (wired in), a TTY means someone ran the CLI in a terminal. The
    471 machines that start nable and never call a tool are a mix of the two, and
    the fixes differ (in-client discoverability vs finishing the wizard's
    handoff), so we need to see the split.
    """
    set_provider_count(provider_count)
    set_plan(plan)
    # Version on the heartbeat makes build freshness directly queryable (the
    # funnel read had to infer it from the surface tag's absence before).
    try:
        from importlib.metadata import version as _v
        ver = _v("finops-mcp")
    except Exception:
        ver = ""
    ping({"event_type": "startup", "surface": _startup_surface(), "version": ver})
