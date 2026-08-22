"""
nable first-run welcome screen + guided onboarding command.

show_welcome()       - prints once on first CLI run, then never again
run_welcome_flow()   - full interactive onboarding, always runs (finops welcome)
"""
from __future__ import annotations

import logging
import os
import sys
import threading
from pathlib import Path

log = logging.getLogger(__name__)

# ── Colour helpers ─────────────────────────────────────────────────────────────

_USE_COLOR = (
    sys.stdout.isatty()
    and os.environ.get("NO_COLOR", "") == ""
    and os.environ.get("TERM", "") != "dumb"
)


def _c(code: str, text: str) -> str:
    if not _USE_COLOR:
        return text
    return f"\033[{code}m{text}\033[0m"


def bold(t: str) -> str:   return _c("1", t)
def dim(t: str) -> str:    return _c("2", t)
def green(t: str) -> str:  return _c("32", t)
def cyan(t: str) -> str:   return _c("36", t)
def yellow(t: str) -> str: return _c("33", t)
def amber(t: str) -> str:  return _c("33", t)
def white(t: str) -> str:  return _c("97", t)
def red(t: str) -> str:    return _c("31", t)


def link(url: str) -> str:
    """A clickable terminal hyperlink (OSC 8), colored like other links.

    The visible text is always the URL itself, so terminals that ignore OSC 8
    (older Terminal.app, plain logs, CI) still show a copyable address, while
    iTerm2 / VS Code / kitty / WezTerm make it a real one-click link.
    """
    if not _USE_COLOR:
        return url
    return f"\033]8;;{url}\033\\{cyan(url)}\033]8;;\033\\"


def _cli(cmd: str = "") -> str:
    """The command to tell the user to run, matched to how they launched nable.

    A `uvx nable ...` run is ephemeral: there is no persistent `finops` on PATH
    after it exits, so hinting `finops doctor` gives the user 'command not found'.
    uvx/uv runs live under the uv cache (sys.prefix like ~/.cache/uv/archive-...),
    so detect that and hint `uvx nable <cmd>`; a pip install has `finops` on PATH.
    """
    prefix = (sys.prefix or "").replace("\\", "/").lower()
    ephemeral = any(s in prefix for s in (
        "/.cache/uv", "/uv/archive", "/uv/environments", "/share/uv/", "/uv/tools",
    ))
    base = "uvx nable" if ephemeral else "finops"
    return f"{base} {cmd}".strip() if cmd else base


# ── Telemetry ──────────────────────────────────────────────────────────────────

def _app_version() -> str:
    """Live package version, not a hardcoded constant that drifts stale."""
    try:
        from importlib.metadata import version
        return version("finops-mcp")
    except Exception:
        return "unknown"


def _fire_telemetry(event: str, properties: dict) -> None:
    """Send a PostHog event via the shared telemetry module.

    Delegates so the event uses the per-install anonymous UUID. The old path here
    hardcoded distinct_id="install" (a constant), which collapsed every install
    into a single PostHog person and made install counts uncountable.
    """
    try:
        from . import telemetry as _tel
        if _tel._is_opted_out():
            return
        props = {"version": _app_version(), **properties}
        t = threading.Thread(
            target=_tel._send_event,
            args=(_tel._get_install_id(), event, props),
            daemon=True,
        )
        t.start()
        # A first run that prints the welcome and exits immediately would kill
        # this daemon thread mid-POST, permanently uncounting the install
        # (the sentinel is already set). A short join lets it land.
        t.join(timeout=2)
    except Exception:
        pass


# ── Sentinel ───────────────────────────────────────────────────────────────────

_SENTINEL = Path.home() / ".config" / "finops" / ".welcomed"


def _is_first_run() -> bool:
    return not _SENTINEL.exists()


def _mark_welcomed() -> None:
    try:
        _SENTINEL.parent.mkdir(parents=True, exist_ok=True)
        _SENTINEL.touch()
    except Exception:
        pass


# ── Layout helpers ─────────────────────────────────────────────────────────────

_W = 58


def _rule(char: str = "─") -> str:
    return dim(char * _W)


def _line(content: str = "") -> None:
    print(f"  {content}")


def _and_list(items: list) -> str:
    """Join client names for prose: ['Cursor'] -> 'Cursor'; ['A','B'] -> 'A and B'."""
    if not items:
        return ""
    if len(items) == 1:
        return items[0]
    if len(items) == 2:
        return f"{items[0]} and {items[1]}"
    return ", ".join(items[:-1]) + f", and {items[-1]}"


def _blank() -> None:
    print()


# ── Shared header ──────────────────────────────────────────────────────────────

def _print_header() -> None:
    _blank()
    _line(bold(white("◆  nable")) + bold("  ·  Cloud Cost Intelligence"))
    _line(dim("   for Claude, Cursor, Windsurf, and any MCP client"))
    _blank()
    _line(_rule())
    _blank()


# ── One-time welcome (auto-shown on first run) ─────────────────────────────────

def _is_interactive_install() -> bool:
    """True only when a human is actually at the terminal.

    install_completed used to fire on ANY first CLI run, so piped invocations,
    `finops --help` in scripts, the cache-warm subprocess inside `finops
    upgrade` (capture_output, so stdin is a pipe), and fresh CI/uvx environments
    all counted as installs. A real first-run welcome is interactive, so we gate
    on stdin/stdout being a TTY and not running under CI.
    """
    try:
        from . import telemetry as _tel
        if _tel.is_ci():
            return False
        return sys.stdin.isatty() and sys.stdout.isatty()
    except Exception:
        return False


def _agent_usage_teaser() -> "list[str] | None":
    """First-run hook for the Claude Code crowd: their agent's real token usage,
    read locally, before they connect anything. This is the cheap front door, every
    coding-agent user has usage even with no cloud connected. Capped so a big log
    history can never stall the banner. Returns display lines, or None if empty."""
    import time
    try:
        from . import ai_budget as ab
        win = ab._WINDOW_HOURS
        u = _run_capped(lambda: ab.read_agent_usage(time.time() - win * 3600), 1.5)
    except Exception:
        return None
    if not u or not u.get("source_present") or u.get("billable_tokens", 0) <= 0:
        return None

    def _t(n: int) -> str:
        if n >= 1_000_000:
            return f"{n / 1e6:.1f}M"
        if n >= 1_000:
            return f"{n / 1e3:.0f}K"
        return str(n)

    tok = u["billable_tokens"]
    per_hr = round(tok / max(win, 0.1))
    return [f"~{_t(tok)} tokens in the last {win:g}h  ·  ~{_t(per_hr)}/hour"]


def show_welcome() -> None:
    """Print on the very first interactive CLI invocation, then never again."""
    if not _is_first_run():
        return

    # Non-interactive / automated first runs are not installs. Stay a no-op and
    # leave the sentinel unset so the first genuine human run still counts once.
    if not _is_interactive_install():
        return

    _mark_welcomed()
    # Banner first: _fire_telemetry briefly joins its sender thread, and on a
    # slow network that wait should happen behind visible output, not before it.
    _print_header()
    _fire_telemetry("install_completed", {"source": "finops_welcome"})

    # Front door for the coding-agent crowd: light up their own agent's usage on
    # run 1, before any cloud is connected, then bridge to the real thing, guarding
    # the infra that agent touches. Only shows when Claude Code logs are present.
    teaser = _agent_usage_teaser()
    if teaser:
        _line(bold("Your AI coding agent, measured locally:"))
        for ln in teaser:
            _line(f"  {white(ln)}")
        _line(dim("   from Claude Code usage on your machine, nothing uploaded"))
        _line(f"   Budget it:  {cyan('nable ai-budget')}")
        _blank()
        _line(dim("   That same agent can touch real infra. Guard what it changes,"))
        _line(dim("   cost preflight + policy gate, propose-only:  ") + cyan("nable guard install"))
        _blank()
        _line(_rule())
        _blank()

    _line(bold("Ask your AI about cloud costs:"))
    _blank()
    for q in [
        '"What drove our cloud costs up 40% this month?"',
        '"Which team is spending the most on Datadog?"',
        '"What are we spending on OpenAI and Anthropic tokens?"',
        '"Create a Jira ticket for any anomaly over $500."',
    ]:
        _line(f"  {cyan(q)}")

    _blank()
    _line(_rule())
    _blank()
    _line(bold("Connected sources:"))
    for label, items in [
        ("Cloud",    "AWS · Azure · GCP · Kubernetes"),
        ("AI / LLM", "OpenAI · Anthropic · Datadog · Langfuse"),
        ("SaaS",     "Snowflake · GitHub · Stripe · Vercel · more"),
    ]:
        _line(f"  {dim(label + ':')}  {items}")

    _blank()
    _line(_rule())
    _blank()
    _line(green("✓") + bold("  7-day free trial, all features unlocked."))
    _line(dim("   No credit card required."))
    _blank()
    _line(f"  Docs  →  {link('https://getnable.com/docs')}")
    _blank()
    _line(_rule())
    _blank()
    _line(bold("Getting started:") + "  connect your first provider below.")
    _line(dim("   Credentials stay on your machine. nable has no backend, so your data never touches our servers."))
    _line(dim("   nable sends anonymous usage pings (no cost data). Opt out: NABLE_NO_TELEMETRY=1"))
    _blank()


# ── The payoff: surface a real number right after connecting ───────────────────

_MAGIC_Q = '"What is driving my cloud bill this month?"'

# AWS service names that are AI/ML spend (Cost Explorer labels them many ways).
_AI_KEYWORDS = (
    "bedrock", "textract", "sagemaker", "comprehend", "rekognition", "kendra",
    "claude", "openai", "anthropic", "transcribe", "translate", "polly", "lex",
)


def _quiet_logs() -> None:
    """Silence the import-time and network INFO chatter so the value moment
    reads clean. Our output uses print(), never logging, so this is safe."""
    import logging
    logging.getLogger().setLevel(logging.ERROR)
    for name in ("botocore", "boto3", "urllib3", "httpx", "httpcore",
                 "posthog", "apscheduler", "finops"):
        logging.getLogger(name).setLevel(logging.ERROR)


def _show_value_moment(demo: bool = False) -> bool:
    """After connecting (or in --demo), scan the account and print a real dollar
    figure in the terminal, so setup pays off before the user opens Claude.

    Returns True if it printed a real number. Fully guarded: every failure or
    slowness is swallowed. This can never block or break onboarding."""
    # Demo mode rides on env flags the server reads. Set them only for the
    # duration of this call and restore after, so a demo fallback can never
    # leak demo mode into a later real scan in the same process.
    _demo_env_prev = None
    if demo:
        _demo_env_prev = {k: os.environ.get(k) for k in ("FINOPS_DEMO", "FINOPS_DEMO_MODE")}
        os.environ["FINOPS_DEMO"] = "1"
        os.environ["FINOPS_DEMO_MODE"] = "1"
    try:
        try:
            return _value_moment_body(demo)
        except Exception:
            # Never let a failed import (e.g. a broken or arch-mismatched native
            # dep), a slow scan, or any runtime error crash onboarding. Degrade to
            # "no number" so the caller falls back to the demo + setup close. The
            # docstring's "can never block or break onboarding" depends on this.
            return False
    finally:
        if _demo_env_prev is not None:
            for _k, _v in _demo_env_prev.items():
                if _v is None:
                    os.environ.pop(_k, None)
                else:
                    os.environ[_k] = _v


_VALUE_MOMENT_TIMEOUT = 30  # seconds; hard wall-clock cap on the first-run scan
_LAST_TOTAL = [0.0]  # 30-day total from the last value moment, for the budget suggestion


def _run_capped(fn, timeout: float):
    """Run fn() in a daemon thread; return its result, or None if it does not
    finish within `timeout` seconds. Returns on time even when fn pins the event
    loop or blocks on I/O, which a plain asyncio timeout cannot interrupt. The
    abandoned thread is a daemon and dies with the process."""
    import threading

    box: dict = {}

    def _w():
        try:
            box["v"] = fn()
        except Exception:
            box["v"] = None

    t = threading.Thread(target=_w, daemon=True)
    t.start()
    t.join(timeout=timeout)
    return box.get("v")


async def _any_llm_configured() -> bool:
    """Fast (vault/env, no network) check for a connected model provider, so the
    value moment only spends time fetching the token bill when there is one."""
    try:
        from .connectors.saas.openai_usage import is_configured as _oai
        from .connectors.saas.anthropic_usage import is_configured as _ant
        from .connectors.saas.openrouter import is_configured as _or
        return bool(await _oai() or await _ant() or await _or())
    except Exception:
        return False


def _value_moment_body(demo: bool = False) -> bool:
    """Scan and print a real dollar figure. Wrapped by _show_value_moment, which
    owns the demo-env lifecycle."""
    if not demo:
        _line(dim("  Scanning your account, this takes a few seconds..."))

    import asyncio
    from . import server  # heavy import, only at the value-moment step

    _quiet_logs()

    # The headline the user came for. Compute and validate it first, on its own
    # wall-clock cap. _run_capped's daemon-thread join returns on time even when a
    # call pins the event loop (a plain asyncio timeout would not fire).
    summary = _run_capped(lambda: asyncio.run(server.get_cost_summary()), _VALUE_MOMENT_TIMEOUT)
    if not isinstance(summary, dict) or summary.get("error"):
        return False

    # Day-one anomalies: seed baselines from CE history in the background so
    # "any cost spikes?" works today, not after 7 days of snapshots. Daemon
    # thread + best-effort: the welcome flow never waits on it or fails from it.
    if not demo:
        try:
            import threading
            from .anomaly.backfill import backfill_from_cost_explorer
            threading.Thread(target=backfill_from_cost_explorer, daemon=True).start()
        except Exception:
            pass
    # Real tool returns grand_total_usd / grand_by_service; demo data returns
    # total_usd / by_service. Accept either.
    total = summary.get("grand_total_usd") or summary.get("total_usd") or 0.0
    by_svc = summary.get("grand_by_service") or summary.get("by_service") or {}
    if total <= 0 or not isinstance(by_svc, dict) or not by_svc:
        return False
    _LAST_TOTAL[0] = float(total)  # anchor for the budget suggestion after this scan

    top = sorted(by_svc.items(), key=lambda kv: kv[1], reverse=True)
    top_name, top_val = top[0]
    ai_total = sum(v for k, v in by_svc.items()
                   if any(w in k.lower() for w in _AI_KEYWORDS))
    ai_share = round(ai_total / total * 100) if total else 0

    # Print the headline NOW, before any optional scan runs. This block used to
    # print only after the idle/AI/LLM scans finished, and those ran serially with
    # caps summing to ~45s, so a real first-run user stared at "Scanning..." for up
    # to ~40s even though this number was ready in under a second. That hang was the
    # top activation risk. The number never waits on the extras again.
    _blank()
    _line(_rule())
    _blank()
    _header = "nable on sample data, last 30 days" if demo else "nable scanned your account, last 30 days"
    _line(green("✓") + bold("  " + _header))
    _blank()
    _line(f"  {dim('Total spend')}      {bold('$' + format(total, ',.0f'))}")
    _line(f"  {dim('Top driver')}       {top_name}  {cyan('$' + format(top_val, ',.0f'))}")
    if ai_share >= 5:
        _line(f"  {dim('AI / ML share')}    {bold(str(ai_share) + '%')}  {dim('of your cloud bill')}")

    # Optional extras, real accounts only. Run CONCURRENTLY so the added wait is
    # the slowest single scan (~10s), not the sum of all three (~45s). Each keeps
    # its own wall-clock cap. There is no demo idle/AI dataset, and calling these
    # in demo would reach for real AWS, so they are skipped in demo.
    if not demo:
        import concurrent.futures
        with concurrent.futures.ThreadPoolExecutor(max_workers=3) as _pool:
            _f_idle = _pool.submit(_run_capped, lambda: asyncio.run(server.list_idle_resources()), 10)
            _f_ai = _pool.submit(_run_capped, lambda: asyncio.run(server.optimize_ai_spend()), 10)
            # Gate the token-bill fetch on a fast config check so cloud-only users
            # never wait on it, but let idle/AI keep running while we check.
            _f_llm = None
            if _run_capped(lambda: asyncio.run(_any_llm_configured()), 3):
                _f_llm = _pool.submit(_run_capped, lambda: asyncio.run(server.get_llm_costs(days=30)), 10)
            idle = _f_idle.result()
            ai_plan = _f_ai.result()
            llm = _f_llm.result() if _f_llm else None

        # The token bill (OpenAI/Anthropic/Bedrock/gateways): the AI-native hero
        # number, shown when a model provider is connected.
        if isinstance(llm, dict):
            llm_total = llm.get("total_usd") or 0
            if llm_total >= 1:
                _line(f"  {dim('AI / LLM spend')}   {bold('$' + format(llm_total, ',.0f'))}  {dim('OpenAI, Anthropic, Bedrock')}")
        if isinstance(idle, dict):
            waste = idle.get("total_monthly_waste_usd") or 0
            if waste and waste >= 1:
                _line(f"  {dim('Idle / wasted')}    {amber('$' + format(waste, ',.0f') + '/mo')}  {dim('doing nothing')}")
        # Realizable AI savings (e.g. prompt caching). Only the addressable figure,
        # never the labeled routing ceiling, so onboarding never overpromises.
        if isinstance(ai_plan, dict) and not ai_plan.get("error"):
            ai_save = ai_plan.get("addressable_savings_monthly_usd") or 0
            if ai_save and ai_save >= 10:
                _line(f"  {dim('AI savings')}       {green('$' + format(ai_save, ',.0f') + '/mo')}  {dim('ready to capture')}")

    # The blank-prompt moment: the bill renders and the user has to invent a
    # question. We already know the bill's shape, so hand them the three
    # questions this account actually makes interesting.
    _blank()
    _line(dim("  Ask your AI, in your editor or Claude Desktop:"))
    for q in _suggest_questions(by_svc, total, ai_share):
        _line(f"    {cyan('·')} \"{q}\"")

    _blank()
    return True


def _suggest_questions(by_svc: dict, total: float, ai_share: int) -> list[str]:
    """Three questions tailored to the bill's shape. The generic 'what can you
    do' answer is a list; a question aimed at the account's own top line gets a
    dollar answer, and that is the moment nable earns a second question."""
    qs: list[str] = []
    top = sorted(by_svc.items(), key=lambda kv: kv[1], reverse=True)
    if top:
        name, val = top[0]
        qs.append(f"Why is {name} ${val:,.0f} this month, and what would cut it?")
    if ai_share >= 20:
        qs.append("Could cheaper models handle part of our AI workload?")
    elif any("textract" in k.lower() or "bedrock" in k.lower() for k in by_svc):
        qs.append("Is non-production usage driving any of our AI spend?")
    else:
        qs.append("What is sitting idle that we are still paying for?")
    qs.append("Any cost spikes I should know about?")
    return qs[:3]


def _llm_ambient_provider() -> str | None:
    """Return 'OpenAI' or 'Anthropic' when a model-provider key is already in the
    shell environment, else None. AI-native startups' biggest cost is the token
    bill, and they usually have OPENAI_API_KEY/ANTHROPIC_API_KEY exported already,
    so this is their fastest path to a real first number, no cloud account needed."""
    import os
    if os.environ.get("OPENAI_API_KEY") or os.environ.get("OPENAI_ADMIN_KEY"):
        return "OpenAI"
    if os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_ADMIN_KEY"):
        return "Anthropic"
    return None


def _llm_value_moment() -> bool:
    """Print the user's real LLM/token bill as the first number. Mirrors the cloud
    value moment but reads get_llm_costs, so an AI-native account sees its actual
    spend (OpenAI + Anthropic + gateways) instead of a cloud summary that would be
    empty for them. Returns True only when a real dollar figure was shown."""
    import asyncio
    from . import server

    _quiet_logs()
    result = _run_capped(lambda: asyncio.run(server.get_llm_costs(days=30)), _VALUE_MOMENT_TIMEOUT)
    if not isinstance(result, dict) or result.get("error"):
        return False
    total = result.get("total_usd") or 0.0
    if total <= 0:
        return False

    by_provider = result.get("by_provider") or {}
    top_spenders = result.get("top_spenders") or []

    _blank()
    _line(_rule())
    _blank()
    _line(green("✓") + bold("  nable scanned your AI spend, last 30 days"))
    _blank()
    _line(f"  {dim('Total AI spend')}   {bold('$' + format(total, ',.0f'))}")
    if by_provider:
        prov, amt = sorted(by_provider.items(), key=lambda kv: kv[1], reverse=True)[0]
        _line(f"  {dim('Top provider')}     {prov}  {cyan('$' + format(amt, ',.0f'))}")
    if top_spenders:
        m = top_spenders[0]
        _line(f"  {dim('Top model')}        {m.get('model', '')}  {cyan('$' + format(m.get('cost_usd', 0), ',.0f'))}")
    _blank()
    return True


def _connect_llm_provider() -> bool:
    """Prompt for an OpenAI or Anthropic key, store it, and show the token bill.
    The AI-native segment's first real number is its model spend, not a cloud bill."""
    from .setup_wizard import setup_saas_api_key
    _line(dim("  Stored in your OS keychain, never sent anywhere. nable reads billing usage only."))
    try:
        pick = input("  Which? 1) OpenAI  2) Anthropic  [1]: ").strip() or "1"
    except (KeyboardInterrupt, EOFError):
        return False
    _blank()
    try:
        if pick == "2":
            setup_saas_api_key("Anthropic", [
                ("ANTHROPIC_API_KEY", "API Key (sk-ant-...)", True),
                ("ANTHROPIC_ADMIN_KEY", "Admin Key for org usage data (optional)", True),
                ("ANTHROPIC_ORGANIZATION_ID", "Organization ID (optional)", False),
            ])
        else:
            setup_saas_api_key("OpenAI", [
                ("OPENAI_API_KEY", "API Key (sk-...)", True),
                ("OPENAI_ADMIN_KEY", "Admin/Org Key for billing data (sk-admin-..., optional)", True),
                ("OPENAI_ORG_ID", "Organization ID (org-..., optional)", False),
            ])
    except (KeyboardInterrupt, EOFError):
        return False
    except Exception:
        return False
    shown = _llm_value_moment()
    if not shown:
        _llm_admin_key_hint("Anthropic" if pick == "2" else "OpenAI")
    return shown


def _llm_admin_key_hint(provider: str) -> None:
    """A connected LLM key that returns no org billing data is almost always a
    regular (non-admin) key. Org cost data needs an ADMIN key, so say that with the
    exact next step instead of dead-ending the user on an empty bill, the single
    most likely confusion on the AI-native connect path."""
    _blank()
    if provider == "OpenAI":
        _line(dim("  Key works, but no org billing data came back. OpenAI cost data needs an"))
        _line(dim("  ") + bold("admin key") + dim(" (sk-admin-...), not a regular key."))
        _line(dim("  Create one:  ") + link("https://platform.openai.com/settings/organization/admin-keys"))
        _line(dim("  Then run:    ") + cyan(_cli("setup openai")) + dim("  and paste it as the Admin key."))
    elif provider == "Anthropic":
        _line(dim("  Key works, but no org usage data came back. Anthropic cost data needs an"))
        _line(dim("  ") + bold("admin key") + dim(" plus your Organization ID."))
        _line(dim("  Create one in the Anthropic Console under Settings -> Admin keys, then run:"))
        _line(dim("  ") + cyan(_cli("setup anthropic")) + dim("  and paste them."))
    _blank()


# ── Full onboarding flow (finops welcome) ──────────────────────────────────────

_AMBIENT_AWS_TIMEOUT = 6.0  # seconds; cap on the first-run ambient credential probe.
# 3s was too tight: AWS SSO token refresh often takes longer, so real creds read
# as "none" and the user fell through to the connect menu. 6s catches SSO while
# still bounded (the "Checking…" line is on screen, so it is not a silent hang).


def _oneclick_aws_url() -> str | None:
    """Return the one-click read-only-key CloudFormation URL when it's published,
    else None. Gated on quick_create_available() so the welcome flow never shows a
    dead link, and lights up automatically once the template goes live. This is
    the fast path for the no-local-creds user, the segment that otherwise stalls
    out the 5-10 minute onboarding."""
    try:
        from .security.iam_setup import quick_create_available, quick_create_url
        if quick_create_available():
            return quick_create_url()
    except Exception:
        pass
    return None


def _nice_budget(x: float) -> int:
    """Round a spend figure up to a clean budget number: nearest 1000 above 10k,
    nearest 100 above 200, else nearest 10. Keeps the suggestion legible, not
    $3,847.11."""
    import math
    if x <= 0:
        return 0
    step = 1000 if x >= 10000 else (100 if x >= 200 else 10)
    return int(math.ceil(x / step) * step)


def _offer_budget_guardrail() -> None:
    """After a real value moment, offer a monthly budget seeded from the scanned
    spend. This is the activation step: it heads off the find-out-the-hard-way
    bill AND sets the number every agent checks against before it acts. Best
    effort, never blocks onboarding, and skips silently if a total budget exists."""
    total = _LAST_TOTAL[0]
    if total <= 0:
        return
    try:
        from .budget.enforcer import create_budget, list_budgets
    except Exception:
        return
    # Returning user who already set one: do not nag.
    try:
        if any(b.get("scope_type") == "total" for b in list_budgets(active_only=True)):
            return
    except Exception:
        pass

    suggested = _nice_budget(total * 1.15)  # ~15% headroom over the current run rate
    if suggested <= 0:
        return

    _blank()
    _line(_rule())
    _blank()
    _line(bold("One more thing: a budget your agents respect."))
    _line(dim(f"  You spent ${total:,.0f} in the last 30 days. Set a monthly budget and"))
    _line(dim("  nable alerts you before you cross it, and any agent can check spend"))
    _line(dim("  against it before it acts, so a runaway agent can't blow past it."))
    _blank()
    try:
        ans = input(f"  Monthly budget in USD [{suggested:,}, Enter to accept, n to skip]: ").strip()
    except (KeyboardInterrupt, EOFError):
        return
    if ans.lower() in ("n", "no", "skip"):
        _blank()
        _line(dim("  No budget set. Ask your editor to \"set a monthly budget\" anytime."))
        return

    amount = float(suggested)
    if ans:
        try:
            amount = float(ans.replace("$", "").replace(",", "").strip())
        except ValueError:
            amount = float(suggested)
    if amount <= 0:
        return

    try:
        create_budget(
            name="Monthly budget",
            scope_type="total",
            limit_usd=amount,
            period="monthly",
            alert_at_pct=80.0,
            critical_at_pct=100.0,
            created_by="onboarding",
        )
    except Exception as exc:
        # Never dump a traceback into the friendly first-run flow; degrade to a line.
        log.debug("onboarding budget save failed (limit_usd=%s): %s", amount, exc)
        _blank()
        _line(dim("  Could not save the budget just now. Set one later from your editor."))
        return

    _fire_telemetry("budget_set", {"source": "onboarding", "limit_usd": amount})
    _blank()
    _line(green("  ✓") + f"  Budget set: {bold('$' + format(amount, ',.0f') + '/mo')}  {dim('· nable warns at 80%, flags at 100%')}")
    _line(dim("  Your agents can now check it before they act:"))
    _line("    " + cyan("check_action_policy") + dim("   and   ") + cyan("check_budget_status"))


def run_welcome_flow(demo: bool = False) -> None:
    """
    Interactive onboarding shown when the user runs `finops welcome`.
    Auto-connects Claude, connects a cloud account, then pays off with a real
    cost number. `--demo` runs the whole thing on sample data, no account needed.
    """
    _print_header()

    # Test seam (inert unless set): the budget step is gated on a live-scan total, so
    # it cannot be driven through the real binary hermetically. This lets the interactive
    # smoke gate seed that total and exercise welcome -> budget end to end. Never set in
    # normal use; nothing here runs otherwise.
    _test_total = os.environ.get("FINOPS_TEST_ONBOARDING_TOTAL")
    if _test_total:
        try:
            _LAST_TOTAL[0] = float(_test_total)
        except ValueError:
            _LAST_TOTAL[0] = 0.0
        _offer_budget_guardrail()
        return

    if demo:
        _line(bold("Demo mode") + dim("  ·  nable on sample data, no account needed"))
        _show_value_moment(demo=True)
        _line(_rule())
        _blank()
        _line(bold(green("That is nable.")) + "  Run it on your own account:")
        _blank()
        _line(f"  {bold(cyan(_cli('welcome')))}")
        _blank()
        _line(f"  Docs  →  {link('https://getnable.com/docs')}")
        _blank()
        return

    # Step indicators
    _line(bold("3 steps to your first cost insight:"))
    _blank()
    # Label column padded on the raw (uncolored) text, then colored, so ANSI
    # escape codes never get counted as visible width and throw the status
    # column out of alignment (the previous hand-padded spacing did exactly
    # that, each row landing its status text one column further right or
    # left than the last).
    _STEP_LABEL_W = 24  # widest label ("See your first number") + 3
    # Value before bureaucracy. This flow used to configure editors as step 2
    # and show a number as step 3, so the first question a brand-new user ever
    # got was about MCP config files. 118 machines entered this flow in the 30
    # days before the swap and 109 left without a trace. Someone with
    # credentials on the machine should see their own dollars before being
    # asked anything about editors.
    for _n, _label, _status, _state in (
        (1, "Install", "done", "done"),
        (2, "See your first number", "reading local credentials", "active"),
        (3, "Connect your editor", "Claude, Cursor, Claude Code", "pending"),
    ):
        _padded = _label.ljust(_STEP_LABEL_W)
        if _state == "done":
            _line(f"  {green(str(_n))}  {bold(_padded)}{green(_status)}")
        elif _state == "active":
            _line(f"  {amber(str(_n))}  {bold(_padded)}{dim(_status)}")
        else:
            _line(f"  {dim(str(_n))}  {dim(_padded)}{dim(_status)}")
    _blank()
    _line(_rule())
    _blank()

    # Step 2: see a number. Zero-config AWS first, then a menu. The editor comes
    # AFTER: a user who has just seen their own bill has a reason to wire it into
    # Claude; a user who has seen nothing is answering config questions on faith.
    _line(bold("Step 2, See your first number"))
    _blank()
    client_result = {"configured": [], "manual": []}

    shown = False

    # Most dev machines already carry an AWS credential chain (env vars,
    # ~/.aws, SSO, instance profile). If so, the fastest path to value is a
    # read-only scan with those creds: no menu, no stored secrets. Ask first,
    # one keystroke. Never touch their account unprompted.
    _line(dim("  Checking for cloud credentials in your environment..."))
    # Every cloud, not just AWS. This probed AWSConnector alone, so someone whose
    # machine was already logged into Azure (`az login`) or GCP (`gcloud auth
    # application-default login`) saw "no credentials found" and was sent to a
    # manual wizard, while an AWS user in the identical position was connected in
    # seconds. ambient.detect_all runs the probes in parallel and each is
    # individually time-boxed, so this stays as fast as the single AWS check was.
    found: list = []
    try:
        from .ambient import detect_all
        found = [a for a in detect_all().values() if a.usable]
    except Exception:
        found = []

    if found:
        try:
            from .setup_wizard import _emit_provider_connected, _emit_step
        except Exception:
            def _emit_provider_connected(*a, **k): pass
            def _emit_step(*a, **k): pass
        names = {"aws": "AWS", "azure": "Azure", "gcp": "Google Cloud"}
        label = " and ".join(names.get(a.provider, a.provider) for a in found)
        _emit_step("welcome_ambient_detected", provider=",".join(a.provider for a in found))
        _line(f"  {green('Found ' + label + ' credentials')} in your environment.")
        _line(dim("  nable can run a read-only cost scan with them right now."))
        _blank()
        ans = "y"
        try:
            ans = input(f"  Show your real {label} bill now? [Y/n]: ").strip().lower() or "y"
        except (KeyboardInterrupt, EOFError):
            ans = "n"
        _blank()
        if ans in ("y", "yes"):
            _emit_step("welcome_ambient_accepted")
            shown = _show_value_moment(demo=False)
            if shown:
                # A confirmed read with ambient creds is a real connection. The
                # ambient path never calls setup_aws_account, so without this the
                # activation metric misses everyone who connects via an existing
                # profile, SSO, or the default chain. Record every provider that
                # contributed, so the funnel stops reading as AWS-only.
                for a in found:
                    _emit_provider_connected("ambient", provider=a.provider)
        else:
            _emit_step("welcome_ambient_declined")

    # No cloud creds shown yet: AI-native users usually have a model-provider key
    # in their env, and the token bill IS their biggest cost. Offer it as the fast
    # first number before falling back to the cloud connect menu.
    if not shown:
        _llm_prov = _llm_ambient_provider()
        if _llm_prov:
            _line(f"  {green('Found ' + _llm_prov + ' credentials')} in your environment.")
            _line(dim("  nable can show your AI / LLM bill with them right now."))
            _blank()
            ans = "y"
            try:
                ans = input(f"  Show your {_llm_prov} bill now? [Y/n]: ").strip().lower() or "y"
            except (KeyboardInterrupt, EOFError):
                ans = "n"
            _blank()
            if ans in ("y", "yes"):
                shown = _llm_value_moment()
                if shown:
                    try:
                        from .setup_wizard import _emit_provider_connected
                        _emit_provider_connected("ambient")
                    except Exception:
                        pass
                else:
                    # Key is present but returned no billing data: almost always a
                    # non-admin key. Point them at the admin key instead of leaving
                    # them staring at an empty AI bill.
                    _llm_admin_key_hint(_llm_prov)

    # No ambient creds, or they declined: offer the full connect menu.
    if not shown:
        # Lead with the one-click read-only key when it's published: a no-creds
        # user gets connected in two copy-pastes instead of hand-minting a key.
        # Cloud Shell first. It is the only connect path that works identically on
        # AWS, Azure and GCP, needs no key minted and nothing installed locally,
        # and it is the ONLY path that reaches a laptop whose corporate proxy or
        # TLS interception blocks the provider outright (a third of the machines
        # that attempted a scan in the 30 days to 2026-08-01 failed exactly that
        # way, instantly, and no local fix can help them).
        from .cloudshell import COMMAND as _CS_CMD
        _line(f"  {green('Fastest')}, your cloud's own browser terminal, already signed in:")
        _line(dim("    Open the >_ icon in the AWS console, Azure portal, or Google Cloud console, then paste:"))
        _line(f"    {cyan(_CS_CMD)}")
        _line(dim("    Read-only. Nothing is installed on this machine and no key is created."))
        _blank()
        _oneclick = _oneclick_aws_url()
        if _oneclick:
            _line(dim("  Or a one-click read-only AWS key, if you would rather connect this machine:"))
            _line(f"    {link(_oneclick)}")
            _line(dim("    Click it, create the stack, then choose 1 below and paste the two outputs."))
            _blank()
        # The model-key option sits at 2, not buried at 4: for the AI-native
        # segment the token bill IS the first real number, and the key is
        # usually one paste away (GTM: lead with AI cost).
        _line(f"  {dim('1)')} AWS          {dim('reads your existing AWS profile')}")
        _line(f"  {dim('2)')} OpenAI / Anthropic   {dim('paste an API key, see your token bill')}")
        _line(f"  {dim('3)')} Azure")
        _line(f"  {dim('4)')} GCP")
        _line(f"  {dim('5)')} Skip for now")
        _blank()
        choice = "5"
        aborted = False
        try:
            choice = input("  Choice [1]: ").strip() or "1"
        except (KeyboardInterrupt, EOFError):
            choice = "5"
            aborted = True
        _blank()
        # The single darkest spot in the funnel: 118 machines reached this menu
        # in 30 days and 109 were never heard from again, and nothing recorded
        # what they picked or whether they Ctrl-C'd out. The choice (never the
        # input beyond it) is the difference between "they skipped" and "they
        # tried AWS and died downstream". Imported here because the `_emit_step`
        # binding above only exists on the ambient-creds-found path, and the
        # no-creds path is precisely the one this measures.
        try:
            from .setup_wizard import _emit_step as _menu_emit
            _menu_emit("cloud_menu_choice",
                       choice={"1": "aws", "2": "ai_keys", "3": "azure",
                               "4": "gcp", "5": "skip"}.get(choice, "other"),
                       aborted=aborted)
        except Exception:
            pass

        if choice == "1":
            from .setup_wizard import setup_aws_account
            setup_aws_account()
            shown = _show_value_moment(demo=False)
        elif choice == "2":
            # AI-native fast path: connect a model provider and show the token
            # bill, the number that actually matters for this segment.
            shown = _connect_llm_provider()
        elif choice == "3":
            from .setup_wizard import setup_azure
            setup_azure()
            shown = _show_value_moment(demo=False)
        elif choice == "4":
            from .setup_wizard import setup_gcp
            setup_gcp()
            shown = _show_value_moment(demo=False)

    # Real data or nothing. If they skipped or the scan came up empty, never show
    # invented numbers, they teach nothing and undercut trust. Point at the fastest
    # path to their own real number so the value lands the moment they connect.
    if not shown:
        _blank()
        _line(bold("No numbers yet, on purpose.") + dim("  nable only ever shows your real spend."))
        _oneclick = _oneclick_aws_url()
        if _oneclick:
            _line(dim("  See yours in two copy-pastes, read-only AWS key, no local creds:"))
            _line(f"    {link(_oneclick)}")
            _line(dim("  Then run  ") + cyan(_cli("welcome")) + dim("  again, or  ") + cyan(_cli("setup aws")) + dim("  if you already have a profile."))
        else:
            _line(dim("  Connect an account to see it:  ") + cyan(_cli("setup aws")) + dim("  (read-only, ~1 min)"))
            _line(dim("  Or paste a model key:  ") + cyan(_cli("setup openai")) + dim("  for your AI / token bill."))
        _blank()
        # No sample-data consolation prize. Someone who just tried to see their own
        # bill does not want a tour of StreamCo's; offering it (and defaulting it to
        # yes) spent the one moment we had on fake numbers instead of on getting
        # their credentials working. Point at the connect flow that waits for them.
        _line(dim("  ") + cyan(_cli("connect")) + dim("  sets credentials up and waits, it connects the moment they land."))
        _blank()

    # Cross-sell the rest of the machine: one scan, one prompt, and the OpenAI
    # key (or Datadog, Modal, gh token…) already sitting in the environment
    # joins the same bill. This is the "whole bill" pitch made real in the
    # first session. Best-effort and silent when there is nothing to offer.
    try:
        from .setup_scan import offer_ambient_connections
        _connected_extra = offer_ambient_connections()
        if _connected_extra:
            _emit_step("welcome_ambient_scan_connected", count=_connected_extra)
    except Exception:
        pass

    # Activation moment: with a real number on screen, offer a budget the agents
    # respect. Only after a real scan (the sample fallback never sets shown), and
    # best-effort so it can never break onboarding.
    if shown:
        try:
            _offer_budget_guardrail()
        except Exception:
            pass

    # Step 3: auto-configure every MCP client we can find (Claude Desktop, Cursor)
    # and surface the exact command for Claude Code. Honest about what got wired,
    # so a Cursor/Claude Code user is never told "you're set up" with nothing
    # written. Deliberately after the value moment: the number is the reason to
    # want this in an editor at all.
    _line(_rule())
    _blank()
    _line(bold("Step 3, Put nable in your editor"))
    _blank()
    try:
        from .setup_wizard import _configure_mcp_clients
        client_result = _configure_mcp_clients()
    except (KeyboardInterrupt, EOFError):
        _line(dim(f"  Skipped. Run '{_cli('setup claude')}' later."))
    except Exception:
        _line(dim(f"  Could not auto-configure. Run '{_cli('setup claude')}' later."))
    _blank()

    # Finish, honest about which clients are wired, and the restart cliff. MCP
    # clients only load servers at startup, so a user with the editor already open
    # sees no nable tools and assumes setup failed. Name the restart explicitly.
    _line(_rule())
    _blank()
    _configured = client_result.get("configured", [])
    _manual = client_result.get("manual", [])
    if _configured:
        _line(bold(green("You're set up.")) + "  " + bold("Fully quit and reopen " + _and_list(_configured)) + dim(" (not just close the window), then ask:"))
    else:
        _line(bold(green("nable is installed.")) + "  Add it to your editor with the command below, then ask:")
    _blank()
    _line(f"  {cyan(_MAGIC_Q)}")
    _blank()
    for _client, _cmd in _manual:
        _line(dim(f"  Using {_client}? Run:  ") + cyan(_cmd))
    if _manual:
        _blank()
    _line(dim(f"  You should see nable in your editor's MCP tool list. Not there? Run '{_cli('doctor')}'."))
    _blank()

    # Ran with uvx? That process was ephemeral, nothing persists on PATH. The MCP
    # config still points at `uvx nable` so the editor keeps working, but the user
    # has no `finops` command. Say so, and give the one line to make it permanent.
    _prefix = (sys.prefix or "").replace("\\", "/").lower()
    if any(s in _prefix for s in ("/.cache/uv", "/uv/archive", "/uv/environments", "/share/uv/", "/uv/tools")):
        _line(dim("  You ran nable with uvx (temporary). Keep it on your machine:  ") + cyan("uv tool install nable"))
        _blank()

    # A reason to come back. Once connected, anomaly baselines seed from cost
    # history in the background, so spike detection works the next day, not after
    # a week of snapshots. Plant the second-session question now.
    if shown:
        _line(dim("  nable is already watching for cost spikes. Tomorrow, ask:  ") + cyan('"any cost spikes I should know about?"'))
        _line(dim("  Prefer it pushed? ") + cyan(_cli("setup slack")) + dim("  sends anomaly alerts and a weekly savings digest."))
        _blank()

    _line(dim("  Your agent runs terraform or kubectl?  ") + cyan(_cli("guard install")))
    _line(dim("  auto-checks every infra command against your budget policy before it runs."))
    _blank()
    _line(f"  Docs    →  {link('https://getnable.com/docs')}")
    _line(f"  Support →  {cyan('chandan@nable.sh')}")
    _blank()
    # Staleness self-check, capped at 2s and silent on any failure. Resolver
    # skew once served users a five-week-old build with nothing telling them.
    try:
        from .update_check import staleness_note
        _stale = staleness_note()
        if _stale:
            _line(amber("  " + _stale))
            _blank()
    except Exception:
        pass
