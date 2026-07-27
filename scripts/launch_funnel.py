#!/usr/bin/env python3
"""Launch-day funnel: site visitor -> ran a command -> connected a provider.

Run it during a launch to see whether traffic is converting, and against which
step it is stalling. Reads PostHog creds from .env.local.

    python3 scripts/launch_funnel.py            # today vs the prior 7-day baseline
    python3 scripts/launch_funnel.py --days 3   # last 3 days, day by day

Why day-over-day and not attribution: nothing links a web visit to a terminal
run (no shared id, and the CLI deliberately phones nothing home about where the
user came from). During a launch that does not matter. Normal days are single
digits, so a spike is unmistakable without any attribution plumbing.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.request
from pathlib import Path

HOST = "https://us.posthog.com"

# The three questions worth asking, in funnel order.
STAGES = [
    ("site visitors", "$pageview"),
    ("ran a command", "setup_wizard_started"),
    ("connected a provider", "provider_connected"),
]


def _load_env() -> tuple[str, str]:
    env = Path(__file__).resolve().parent.parent / ".env.local"
    vals = dict(os.environ)
    if env.is_file():
        for line in env.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                vals.setdefault(k.strip(), v.strip().strip('"').strip("'"))
    pid, key = vals.get("POSTHOG_PROJECT_ID"), vals.get("POSTHOG_PERSONAL_API_KEY")
    if not (pid and key):
        sys.exit("POSTHOG_PROJECT_ID / POSTHOG_PERSONAL_API_KEY not found in .env.local")
    return pid, key


def query(pid: str, key: str, sql: str) -> list:
    req = urllib.request.Request(
        f"{HOST}/api/projects/{pid}/query/",
        data=json.dumps({"query": {"kind": "HogQLQuery", "query": sql}}).encode(),
        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=90) as r:
        return json.load(r)["results"]


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=7, help="how many days to show")
    args = ap.parse_args()
    pid, key = _load_env()

    sel = ",\n  ".join(
        f"countDistinctIf(distinct_id, event='{ev}') AS s{i}"
        for i, (_, ev) in enumerate(STAGES)
    )
    rows = query(pid, key, f"""
SELECT toDate(timestamp) AS d,
  {sel}
FROM events
WHERE timestamp > now() - INTERVAL {args.days} DAY
GROUP BY d ORDER BY d
""")

    labels = [lbl for lbl, _ in STAGES]
    width = max(len(x) for x in labels) + 2
    print(f"\n  {'date':12s}" + "".join(f"{l:>{width}s}" for l in labels))
    print("  " + "-" * (12 + width * len(labels)))
    for row in rows:
        print(f"  {str(row[0]):12s}" + "".join(f"{v:>{width}}" for v in row[1:]))

    if len(rows) >= 2:
        today, prior = rows[-1], rows[:-1]
        print(f"\n  today vs the {len(prior)}-day average before it:")
        for i, (lbl, _) in enumerate(STAGES):
            avg = sum(r[i + 1] for r in prior) / len(prior)
            now = today[i + 1]
            delta = f"{now / avg:.1f}x" if avg else ("new" if now else "flat")
            print(f"    {lbl:22s} {now:>5}   (avg {avg:>5.1f})  {delta}")

        # The step that is losing the most people is the one worth fixing today.
        print("\n  where today's traffic is stalling:")
        for i in range(len(STAGES) - 1):
            a, b = today[i + 1], today[i + 2]
            pct = f"{b / a * 100:.0f}%" if a else "n/a"
            print(f"    {STAGES[i][0]:22s} -> {STAGES[i + 1][0]:22s} {pct:>6}"
                  f"   ({a} -> {b})")

    # Anchor "today" to the newest day that actually has data, NOT to today().
    # PostHog's today() is UTC, so from a US timezone it rolls over mid-evening
    # and the breakdowns silently read empty while the rollup above still shows
    # traffic. That is exactly the kind of "looks like zero" that would panic
    # someone watching a launch.
    day = str(rows[-1][0]) if rows else None
    if not day:
        print("\n  no events in the window\n")
        return

    # Which commands people reach for. On a launch this says whether the post's
    # call to action actually landed, or whether they typed something else.
    print(f"\n  commands run on {day} (UTC):")
    subs = query(pid, key, f"""
SELECT properties.subcommand AS sub, count(DISTINCT distinct_id) AS machines
FROM events
WHERE event='setup_wizard_started' AND toDate(timestamp) = toDate('{day}')
GROUP BY sub ORDER BY machines DESC LIMIT 12
""")
    if subs:
        for sub, n in subs:
            print(f"    {str(sub):24s} {n:>4}")
    else:
        print("    (none)")

    # Shipped 0.8.196+. Empty before that is expected, not a bug.
    print(f"\n  guard installs on {day} (UTC):")
    g = query(pid, key, f"""
SELECT properties.outcome AS outcome, properties.hook_form AS form,
       count(DISTINCT distinct_id) AS machines
FROM events
WHERE event='guard_installed' AND toDate(timestamp) = toDate('{day}')
GROUP BY outcome, form ORDER BY machines DESC
""")
    if g:
        for outcome, form, n in g:
            print(f"    {str(outcome):14s} {str(form):10s} {n:>4}")
    else:
        print("    (none yet; the event ships in 0.8.196)")
    print()


if __name__ == "__main__":
    main()
