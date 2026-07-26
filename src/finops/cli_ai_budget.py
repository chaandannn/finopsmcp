"""`finops ai-budget` — set and check a local budget for your AI coding agent.

Run bare `finops ai-budget` the first time and it asks you two questions (flat
subscription or metered API, and what you pay), then remembers. After that, bare
`finops ai-budget` just prints where you stand: this window, month to date, your
budget, burn rate. Flags (--plan-cost / --spend-cap / --tokens) skip the questions
for scripts. Numbers come from finops.ai_budget: real local token usage from Claude
Code's session logs. Nothing leaves your machine.
"""
from __future__ import annotations

import json
import sys

_ACCENT = "\033[38;5;38m"
_DIM = "\033[2m"
_BOLD = "\033[1m"
_WARN = "\033[38;5;208m"
_OVER = "\033[38;5;203m"
_OK = "\033[38;5;71m"
_RST = "\033[0m"


def _c(s: str, color: str) -> str:
    return s if not sys.stdout.isatty() else f"{color}{s}{_RST}"


def _tok(n: int) -> str:
    if n >= 1_000_000_000:
        return f"{n/1e9:.1f}B"
    if n >= 1_000_000:
        return f"{n/1e6:.1f}M"
    if n >= 1_000:
        return f"{n/1e3:.0f}K"
    return str(n)


def _num(raw: str) -> float:
    """Parse '100', '$2,500', '2.5k', '1m' into a float. Returns 0.0 on garbage."""
    raw = raw.strip().lower().replace(",", "").replace("$", "").replace(" ", "")
    if not raw:
        return 0.0
    mult = 1.0
    if raw[-1] in "kmb":
        mult = {"k": 1e3, "m": 1e6, "b": 1e9}[raw[-1]]
        raw = raw[:-1]
    try:
        return float(raw) * mult
    except ValueError:
        return 0.0


def add_parser(sub) -> None:
    p = sub.add_parser(
        "ai-budget",
        help="Set and check a local budget for your AI coding agent",
        description="A local budget for your coding agent's own spend. First run asks "
                    "two questions; after that it just reports. Reads Claude Code usage "
                    "locally, nothing leaves your machine.",
    )
    p.add_argument("--plan-cost", type=float, metavar="USD",
                   help="Flat plan: what you pay per month, any number, e.g. --plan-cost 100")
    p.add_argument("--spend-cap", type=float, metavar="USD",
                   help="Metered API: monthly dollar cap, e.g. --spend-cap 2500")
    p.add_argument("--tokens", type=int, metavar="N",
                   help="Usage cap: warn before N billable tokens/month (either mode)")
    p.add_argument("--reset", action="store_true", help="Forget the saved budget")
    p.add_argument("--json", action="store_true", help="Emit machine-readable JSON")
    p.set_defaults(cmd="ai-budget")


def _interactive_setup(ab, out) -> None:
    """Two questions, asked only when nothing is configured and we have a TTY."""
    print(_c("Set a budget for your AI coding agent.", _BOLD), file=out)
    print(_c("Two quick questions. Everything stays on this machine.", _DIM), file=out)
    print(file=out)
    print("  How do you pay for your coding agent?", file=out)
    print(f"    {_c('1', _ACCENT)}  Flat subscription   (Claude Pro/Max, Cursor, ...)", file=out)
    print(f"    {_c('2', _ACCENT)}  Metered API / work  (pay per token)", file=out)
    try:
        kind = input("  Choose 1 or 2: ").strip()
        if kind == "2":
            cap = _num(input("  Monthly spend cap in USD (e.g. 2500): $"))
            tok = input("  Optional: warn before N million tokens/mo (blank to skip): ").strip()
            ab.set_budget(mode="metered", spend_cap=cap, plan_label="metered API",
                          monthly_tokens=int(_num(tok) * 1e6) if tok else None)
        else:
            cost = _num(input("  What do you pay per month in USD (e.g. 100): $"))
            tok = input("  Optional: warn before N million tokens/mo (blank to skip): ").strip()
            ab.set_budget(mode="flat", plan_cost=cost, plan_label="subscription",
                          monthly_tokens=int(_num(tok) * 1e6) if tok else None)
    except (EOFError, KeyboardInterrupt):
        print(file=out)
        return
    print(file=out)


def run(args) -> int:
    from . import ai_budget as ab

    out = sys.stdout

    if getattr(args, "reset", False):
        ab.reset_budget()

    gave_flags = (args.plan_cost is not None or args.spend_cap is not None
                  or args.tokens is not None)
    if gave_flags:
        ab.set_budget(plan_cost=args.plan_cost, spend_cap=args.spend_cap,
                      monthly_tokens=args.tokens)

    # First run, nothing set, a real terminal: ask instead of making them read flags.
    if (not gave_flags and not getattr(args, "json", False)
            and not ab.get_budget()["mode"] and sys.stdin.isatty() and out.isatty()):
        _interactive_setup(ab, out)

    st = ab.status()
    if getattr(args, "json", False):
        print(json.dumps(st, indent=2, default=str))
        return 0

    b = st["budget"]
    w = st["window"]
    mode = st["mode"]
    verdict = st["verdict"]
    vcolor = {"ok": _OK, "warn": _WARN, "over": _OVER}[verdict]

    label = st["plan_label"] or {"flat": "subscription", "metered": "metered API"}.get(
        mode, "no budget set")
    print(_c("nable ai-budget", _BOLD) + _c(f"  ·  {label}", _DIM), file=out)
    if not w["source_present"]:
        print(_c("  no Claude Code usage found yet (looked in ~/.claude/projects).", _DIM), file=out)

    def row(lbl: str, value: str) -> None:
        print(f"  {_c(lbl.ljust(16), _DIM)}{value}", file=out)

    print(file=out)
    row("this 5h window", f"{_tok(w['billable_tokens'])} tokens · {w['messages']} msgs · "
                          f"~{_c('$'+format(w['usd_equivalent'], ',.0f'), _ACCENT)} at list price (est.)")
    row("month to date", f"{_tok(st['billable_tokens_mtd'])} tokens · "
                         f"~${st['est_usd_mtd_list_price']:,.0f} at list price (est.)")

    # cost per 1M: for a flat plan the story is effective (your fee) vs list; for
    # metered it is simply the blended list rate.
    eff, lst = st["cost_per_1m_effective"], st["cost_per_1m_list"]
    if eff is not None and lst is not None:
        row("cost / 1M", f"~{_c('$'+format(eff, ',.2f'), _OK)} on your plan  ·  "
                         f"vs ~${lst:,.2f} at list")
    elif lst is not None:
        row("cost / 1M", f"~${lst:,.2f} at list price (est.)")

    sub = st["subsidy"]
    pct = (st["pct_of_budget"] or 0) * 100
    if mode == "metered" and b["spend_cap"] > 0:
        row("spend cap", f"~${st['est_usd_mtd_list_price']:,.0f} est of ${b['spend_cap']:,.0f}  ·  "
                         f"{_c(verdict.upper(), vcolor)} ({pct:.0f}%)")
    elif mode == "flat" and b["plan_cost"] > 0:
        # Always confirm the configured plan, even with no usage yet. The subsidy
        # multiple only appears once there is usage to value against it.
        extra = (f"  ·  ~{sub['multiple']:g}x value pulled {_c('(subsidized)', _OK)}"
                 if sub and sub.get("multiple") else "")
        row("your plan", f"${b['plan_cost']:,.0f}/mo flat{extra}")
    if b["monthly_tokens"] > 0:
        row("usage cap", f"{_tok(st['billable_tokens_mtd'])} of {_tok(b['monthly_tokens'])} tokens  ·  "
                         f"{_c(verdict.upper() if st['verdict_basis'] == 'tokens' else 'tracking', vcolor if st['verdict_basis'] == 'tokens' else _DIM)}"
                         f" ({st['billable_tokens_mtd']/b['monthly_tokens']*100:.0f}%)")
    if not mode:
        row("budget", _c("not set · run `nable ai-budget` to set one", _DIM))
    row("burn rate", f"~{_tok(st['burn_tokens_per_hour'])} tokens/hour")

    print(file=out)
    print("  " + _c(st["summary"], vcolor), file=out)
    print(_c("  local · exact token counts · dollars are list-price estimates, not your bill", _DIM), file=out)
    return 0
