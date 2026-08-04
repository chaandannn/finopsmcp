"""Renderings of a morning brief: dashboard HTML, Slack, email, terminal.

One brief, four surfaces. The content is identical everywhere; only the density
changes. Slack gets the top three and a link, because a wall of blocks in a
channel is how people mute the channel.

ESCAPING IS NOT OPTIONAL HERE. Every string in a brief is derived from cloud
data: resource names, tag values, IaC file paths. A resource tagged
`<img src=x onerror=...>` is a perfectly legal AWS tag, and this HTML is served
to a logged-in operator on the dashboard. Everything interpolated goes through
html.escape, and the drafted commands are rendered as text, never as anything a
browser or shell would act on.

Styling follows DESIGN.md: true-black background, Newsreader headings, Geist
body, Geist Mono reserved for actual technical content (resource ids, commands,
figures), ice blue accent used sparingly, no grid backgrounds, no pulse
animations.
"""
from __future__ import annotations

from html import escape
from typing import Any

from .brief import Brief, BriefItem
from .resource_map import OWNED_BY, ResourceMap

# DESIGN.md tokens, inlined so a brief renders standalone (email, a saved file,
# a dashboard panel) without depending on the site's stylesheet being present.
_CSS = """
:root{
  --bg:#000;--bg-1:#0a0a0c;--bg-2:#121214;--bg-3:#1a1a1d;
  --line:#232327;--line-2:#2d2d32;
  --fg:#f0f2f3;--fg-2:#94a3ab;--fg-3:#56656d;
  --accent:#4db8d4;--accent-dim:#2c7d91;
  --success:#3cba7a;--warn:#e6a840;--alert:#e05c4b;
  --font-display:'Newsreader',Georgia,serif;
  --font-ui:'Geist',-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;
  --font-mono:'Geist Mono','JetBrains Mono',ui-monospace,SFMono-Regular,monospace;
}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--fg);font-family:var(--font-ui);
  font-size:15px;line-height:1.6;-webkit-font-smoothing:antialiased}
.wrap{max-width:860px;margin:0 auto;padding:80px 24px}
h1,h2,h3{font-family:var(--font-display);font-weight:300;letter-spacing:-.02em;margin:0}
h1{font-size:38px;font-weight:200;line-height:1.15}
h2{font-size:22px;margin:56px 0 16px}
h3{font-size:18px;margin:0}
.eyebrow{font-family:var(--font-ui);font-weight:500;font-size:13px;
  text-transform:uppercase;letter-spacing:.08em;color:var(--accent-dim);margin-bottom:12px}
.sub{color:var(--fg-2);margin-top:12px}
.mono{font-family:var(--font-mono);font-variant-numeric:tabular-nums}
.card{background:var(--bg-1);border:1px solid var(--line);border-radius:12px;
  padding:24px;margin-bottom:16px}
.card-head{display:flex;align-items:baseline;gap:12px;flex-wrap:wrap}
.rank{font-family:var(--font-mono);color:var(--fg-3);font-size:13px}
.amount{margin-left:auto;font-family:var(--font-mono);font-size:20px;color:var(--success)}
.amount.unknown{color:var(--fg-3);font-size:15px}
.chip{display:inline-block;font-size:12px;padding:2px 8px;border-radius:2px;
  border:1px solid var(--line-2);color:var(--fg-2);background:var(--bg-2)}
.chip.safe{color:var(--success);border-color:#245c41}
.chip.coupled{color:var(--warn);border-color:#5c4a20}
.chip.unknown{color:var(--fg-3);border-style:dashed}
.words{color:var(--fg-2);margin:14px 0 0}
.label{font-size:12px;text-transform:uppercase;letter-spacing:.08em;
  color:var(--fg-3);margin:22px 0 8px;font-weight:500}
.map{display:flex;flex-wrap:wrap;gap:8px;align-items:center}
.node{font-family:var(--font-mono);font-size:12px;padding:4px 10px;border-radius:2px;
  background:var(--bg-2);border:1px solid var(--line);color:var(--fg-2);
  overflow-wrap:anywhere}
.node.self{background:var(--bg-3);border-color:var(--accent-dim);color:var(--fg)}
.node.unchecked{border-style:dashed;color:var(--fg-3);font-family:var(--font-ui)}
.edge{color:var(--fg-3);font-size:12px}
pre{font-family:var(--font-mono);font-size:13px;background:var(--bg-2);
  border:1px solid var(--line);border-radius:8px;padding:14px 16px;margin:0;
  overflow-x:auto;color:var(--fg)}
pre + pre{margin-top:8px}
ol{margin:0;padding-left:20px;color:var(--fg-2)}
.foot{color:var(--fg-3);font-size:13px;border-top:1px solid var(--line);
  margin-top:56px;padding-top:20px}
.gap{color:var(--warn)}
.caveat{color:var(--fg-3);font-size:13px;margin-top:10px}
@media (max-width:600px){.wrap{padding:48px 16px}h1{font-size:28px}
  .amount{margin-left:0;width:100%}}
"""


def _money(v: float | None) -> str:
    return f"${v:,.0f}/mo" if v is not None else ""


def _safety_chip(m: ResourceMap) -> tuple[str, str]:
    if m.isolated is True:
        return "safe", "nothing references it"
    # Unexamined wins over a known count: "touches 1" next to an unread route
    # table is a more dangerous label than "unverified".
    if m.unexamined:
        return ("unknown", "dependencies unverified" if not m.dependencies
                else f"touches {m.blast_radius}, more unchecked")
    return "coupled", f"touches {m.blast_radius}"


# ── HTML ──────────────────────────────────────────────────────────────────────

def _map_html(m: ResourceMap) -> str:
    """The picture: this resource, then what it touches, then what we could not
    check. The unchecked band is dashed and always rendered when non-empty; it
    is the difference between a map and a claim."""
    parts = [f'<span class="node self">{escape(m.resource_id or "resource")}</span>']
    deps = m.dependencies
    if deps:
        by_kind: dict[str, list[str]] = {}
        for e in deps:
            by_kind.setdefault(e.kind, []).append(e.target_id)
        for kind, ids in by_kind.items():
            parts.append(f'<span class="edge">&rarr; {escape(kind.replace("_", " "))} &rarr;</span>')
            for i in ids[:6]:
                parts.append(f'<span class="node">{escape(i)}</span>')
            if len(ids) > 6:
                parts.append(f'<span class="edge">+{len(ids) - 6} more</span>')
    elif not m.unexamined:
        parts.append('<span class="edge">&rarr;</span>'
                     '<span class="node">nothing</span>')

    if m.unexamined:
        parts.append('<span class="edge">not checked:</span>')
        for u in m.unexamined:
            parts.append(f'<span class="node unchecked">{escape(u)}</span>')

    owners = m.owners()
    if owners:
        parts.append('<span class="edge">owner:</span>')
        for o in owners:
            parts.append(f'<span class="node">{escape(o)}</span>')
    return '<div class="map">' + "".join(parts) + "</div>"


def _item_html(item: BriefItem, rank: int) -> str:
    m = item.resource_map
    chip_cls, chip_txt = _safety_chip(m)
    amount = item.monthly_usd
    amount_html = (f'<span class="amount mono">{_money(amount)}</span>' if amount is not None
                   else f'<span class="amount unknown mono">'
                        f'{escape(str(item.finding.get("magnitude") or "unconfirmed"))}</span>')

    fix = item.drafted_fix
    cmds = "".join(f"<pre>{escape(c)}</pre>" for c in fix.get("commands") or [])
    steps = "".join(f"<li>{escape(s)}</li>" for s in fix.get("steps") or [])

    caveat = fix.get("caveat") or ""
    rev = fix.get("reversible")
    if rev is False:
        caveat = ("This cannot be undone. " + caveat).strip()

    return f"""
<div class="card">
  <div class="card-head">
    <span class="rank mono">{rank:02d}</span>
    <h3>{escape(item.title)}</h3>
    <span class="chip {chip_cls}">{escape(chip_txt)}</span>
    {amount_html}
  </div>
  <p class="words">{escape(item.in_words())}</p>

  <div class="label">What it touches</div>
  {_map_html(m)}

  <div class="label">Drafted change</div>
  <ol>{steps}</ol>
  {cmds}
  {f'<p class="caveat">{escape(caveat)}</p>' if caveat else ''}

  <div class="label">How you will know it worked</div>
  <p class="words">{escape(item.verification)}</p>
  <p class="caveat">Ranked here because {escape(item.why_this_rank)}.</p>
</div>"""


def to_html(brief: Brief, *, title: str = "This morning") -> str:
    items = "".join(_item_html(i, n) for n, i in enumerate(brief.actionable, 1))
    invs = "".join(_item_html(i, n) for n, i in
                   enumerate(brief.investigations, len(brief.actionable) + 1))

    gaps = ""
    if brief.gaps:
        rows = "".join(f"<li>{escape(g)}</li>" for g in brief.gaps)
        gaps = f'<h2>What nable could not check</h2><ol class="gap">{rows}</ol>'

    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{escape(title)} &middot; nable</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link href="https://fonts.googleapis.com/css2?family=Newsreader:opsz,wght@6..72,200..400&family=Geist:wght@100..900&family=Geist+Mono:wght@300..600&display=swap" rel="stylesheet">
<style>{_CSS}</style></head>
<body><div class="wrap">
  <div class="eyebrow">Overnight run &middot; {escape(brief.generated_at[:16].replace("T", " "))} UTC</div>
  <h1>{escape(brief.headline())}</h1>
  <p class="sub">nable scanned while you were asleep, reviewed what it found, and
  drafted each change. It has not run any of them.</p>

  {f"<h2>Ready to act on</h2>{items}" if items else ""}
  {f"<h2>Worth a look, not yet confirmed</h2>{invs}" if invs else ""}
  {gaps}

  <div class="foot">nable drafts changes. It does not make them. Every figure here
  survived an automated review that tries to refute it first; anything that failed
  that review is listed above as unconfirmed, without a precise figure.</div>
</div></body></html>"""


# ── terminal / markdown ───────────────────────────────────────────────────────

def to_markdown(brief: Brief) -> str:
    out = [f"# {brief.headline()}", "",
           f"_Overnight run, {brief.generated_at[:16].replace('T', ' ')} UTC. "
           f"nable drafted these changes and ran none of them._", ""]

    def block(item: BriefItem, n: int) -> list[str]:
        m = item.resource_map
        _, chip = _safety_chip(m)
        head = f"## {n:02d}. {item.title}"
        if item.monthly_usd is not None:
            head += f"  ({_money(item.monthly_usd)})"
        lines = [head, "", item.in_words(), "",
                 f"**What it touches:** {m.summary()}  _({chip})_", "",
                 f"**Drafted change:** {item.drafted_fix.get('summary', '')}"]
        for s in item.drafted_fix.get("steps") or []:
            lines.append(f"  - {s}")
        cmds = item.drafted_fix.get("commands") or []
        if cmds:
            lines += ["", "```bash", *cmds, "```"]
        if item.drafted_fix.get("caveat"):
            lines += ["", f"> {item.drafted_fix['caveat']}"]
        lines += ["", f"**How you will know it worked:** {item.verification}",
                  f"_Ranked here because {item.why_this_rank}._", ""]
        return lines

    n = 0
    if brief.actionable:
        out.append("---")
        for item in brief.actionable:
            n += 1
            out += block(item, n)
    if brief.investigations:
        out += ["---", "# Worth a look, not yet confirmed", ""]
        for item in brief.investigations:
            n += 1
            out += block(item, n)
    if brief.gaps:
        out += ["---", "# What nable could not check", ""]
        out += [f"- {g}" for g in brief.gaps]
    return "\n".join(out)


# ── Slack ─────────────────────────────────────────────────────────────────────

_SLACK_TOP_N = 3


def to_slack_blocks(brief: Brief, *, url: str | None = None) -> list[dict[str, Any]]:
    """Block Kit for the channel. Deliberately the top few plus a link: a wall of
    blocks every morning is how a channel gets muted, and a muted channel is
    worth less than no channel."""
    blocks: list[dict[str, Any]] = [
        {"type": "header",
         "text": {"type": "plain_text", "text": "nable overnight run", "emoji": False}},
        {"type": "section",
         "text": {"type": "mrkdwn", "text": f"*{brief.headline()}*"}},
    ]

    shown = brief.actionable[:_SLACK_TOP_N]
    for n, item in enumerate(shown, 1):
        m = item.resource_map
        _, chip = _safety_chip(m)
        amount = _money(item.monthly_usd) or "unconfirmed"
        text = (f"*{n}. {item.title}*  `{amount}`  _{chip}_\n"
                f"{item.in_words()}\n"
                f"*Drafted:* {item.drafted_fix.get('summary', '')}")
        cmds = item.drafted_fix.get("commands") or []
        if cmds:
            text += "\n```" + "\n".join(cmds) + "```"
        blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": text}})

    remaining = len(brief.actionable) - len(shown)
    tail = []
    if remaining > 0:
        tail.append(f"{remaining} more ready to act on")
    if brief.investigations:
        tail.append(f"{len(brief.investigations)} unconfirmed")
    if brief.gaps:
        tail.append(f"{len(brief.gaps)} gap(s) nable could not check")

    footer = "nable drafted these and ran none of them."
    if tail:
        footer = " · ".join(tail) + ". " + footer
    if url:
        footer += f" <{url}|Open the full brief>"
    blocks.append({"type": "context",
                   "elements": [{"type": "mrkdwn", "text": footer}]})
    return blocks


def to_slack_text(brief: Brief) -> str:
    """Notification fallback text. Slack shows this in the push and the sidebar,
    so it has to carry the number on its own."""
    return f"nable overnight run: {brief.headline()}"


# ── email ─────────────────────────────────────────────────────────────────────

def to_email(brief: Brief, *, url: str | None = None) -> dict[str, str]:
    """Subject plus both bodies. Mail clients strip most CSS, so the HTML body is
    the same document as the dashboard: it degrades to readable text rather than
    to a broken layout."""
    n = len(brief.actionable)
    subject = (f"nable: {_money(brief.total_monthly_usd)} across {n} item(s)"
               if n else "nable: nothing new to act on")
    text = to_markdown(brief)
    if url:
        text += f"\n\nFull brief: {url}"
    return {"subject": subject, "text": text, "html": to_html(brief)}
