"""`nable brief` — the overnight run, on demand.

The scheduled job writes a brief every morning. This is the same run, triggered
by hand, plus a way to read the one that already ran.

Delivery is OFF unless `--deliver` is passed, even when NABLE_BRIEF_DELIVER lists
channels. Someone running this in a terminal to look at the output should not
discover they have posted to the team channel a fourth time this morning.
"""
from __future__ import annotations

import json
import os
import sys
import webbrowser
from datetime import date
from pathlib import Path

EXIT_OK = 0
EXIT_ERROR = 1
EXIT_NOTHING = 0        # an empty brief is good news, not a failure


def add_parser(sub) -> None:
    p = sub.add_parser(
        "brief",
        help="What nable found overnight: ranked, reviewed, with each change drafted",
    )
    p.add_argument("--latest", action="store_true",
                   help="show the last saved brief instead of running a new scan")
    p.add_argument("--json", action="store_true", help="machine-readable output on stdout")
    p.add_argument("--html", action="store_true",
                   help="write the brief as HTML and open it in a browser")
    p.add_argument("--deliver", action="store_true",
                   help=f"also push to the channels in $NABLE_BRIEF_DELIVER "
                        f"(off by default, even when that is set)")
    p.add_argument("--limit", type=int, default=10, metavar="N",
                   help="how many findings reach the brief (default 10)")
    p.add_argument("--no-save", action="store_true",
                   help="do not write the brief to disk")


def _print_saved(payload: dict, as_json: bool) -> int:
    if as_json:
        print(json.dumps(payload, indent=2))
        return EXIT_OK
    print(payload.get("headline", "No brief."))
    print()
    for item in payload.get("items", []):
        amount = item.get("monthly_usd")
        money = f"  ${amount:,.0f}/mo" if amount is not None else \
                f"  {item.get('magnitude') or 'unconfirmed'}"
        print(f"{item.get('title', '')}{money}")
        print(f"    {item.get('in_words', '')}")
        fix = item.get("drafted_fix") or {}
        if fix.get("summary"):
            print(f"    fix: {fix['summary']}")
        for cmd in fix.get("commands") or []:
            print(f"       $ {cmd}")
        print()
    for gap in payload.get("gaps") or []:
        print(f"  not checked: {gap}")
    return EXIT_OK


def run(args) -> int:
    as_json = bool(getattr(args, "json", False))

    from .briefing.run import latest, run_overnight

    if getattr(args, "latest", False):
        saved = latest()
        if saved is None:
            msg = ("No brief saved yet. Run `nable brief` to build one, or let the "
                   "scheduled overnight run produce it.")
            print(json.dumps({"error": "no_brief"}) if as_json else msg,
                  file=sys.stdout if as_json else sys.stderr)
            return EXIT_NOTHING
        return _print_saved(saved, as_json)

    try:
        result = run_overnight(
            deliver_to=None if getattr(args, "deliver", False) else (),
            do_persist=not getattr(args, "no_save", False),
            limit=int(getattr(args, "limit", 10) or 10),
        )
    except KeyboardInterrupt:
        print("\nCancelled.", file=sys.stderr)
        return EXIT_ERROR
    except Exception as exc:
        # The type, never the message: it can carry account ids and ARNs.
        if as_json:
            print(json.dumps({"error": type(exc).__name__}))
        else:
            print(f"The overnight run failed ({type(exc).__name__}).", file=sys.stderr)
            print("Run with FINOPS_DEBUG=1 for the traceback.", file=sys.stderr)
        if os.getenv("FINOPS_DEBUG") == "1":
            raise
        return EXIT_ERROR

    brief = result["brief"]
    delivered = result.get("delivered") or {}

    if getattr(args, "html", False):
        from .briefing.render import to_html
        out = Path(result["path"]).with_suffix(".html") if result.get("path") else \
            Path.cwd() / f"nable-brief-{date.today().isoformat()}.html"
        out.write_text(to_html(brief))
        print(f"Wrote {out}")
        try:
            webbrowser.open(out.as_uri())
        except Exception:
            pass
        return EXIT_OK

    if as_json:
        print(json.dumps(result["summary"], indent=2))
    else:
        from .briefing.render import to_markdown
        print(to_markdown(brief))
        if delivered:
            landed = ", ".join(k for k, v in delivered.items() if v) or "nothing"
            print(f"\nDelivered to: {landed}")
        if result.get("path"):
            print(f"Saved: {result['path']}")
    return EXIT_OK
