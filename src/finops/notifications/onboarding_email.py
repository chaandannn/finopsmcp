"""
nable onboarding email — sent when someone submits their email on getnable.com.

Two variants:
  welcome   → fires immediately on email capture (free tier or modal)
  day7      → fires if no provider_connected event after 7 days (nudge)
  trial_end → fires 3 days before trial expires

All plain smtplib, no dependencies beyond stdlib.
"""
from __future__ import annotations

import os
import smtplib
import ssl
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText


def _env(key: str, default: str = "") -> str:
    return os.environ.get(key, default)


# ── HTML helpers ───────────────────────────────────────────────────────────────

_SLATE  = "#0F172A"
_MUTED  = "#64748B"
_GREEN  = "#22C55E"
_BORDER = "#E2E8F0"
_BG     = "#F8FAFC"
_MONO   = "'Courier New', Courier, monospace"


def _base(content: str, preheader: str = "") -> str:
    """Wrap content in a clean, responsive email shell."""
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8"/>
<meta name="viewport" content="width=device-width,initial-scale=1"/>
<meta name="x-apple-disable-message-reformatting"/>
<title>nable</title>
</head>
<body style="margin:0;padding:0;background:#fff;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Helvetica,Arial,sans-serif;color:{_SLATE};-webkit-font-smoothing:antialiased">
{f'<div style="display:none;max-height:0;overflow:hidden;color:#fff">{preheader}&nbsp;&zwnj;&nbsp;&zwnj;&nbsp;&zwnj;&nbsp;&zwnj;&nbsp;&zwnj;&nbsp;&zwnj;&nbsp;&zwnj;&nbsp;&zwnj;&nbsp;&zwnj;&nbsp;&zwnj;&nbsp;&zwnj;&nbsp;&zwnj;&nbsp;&zwnj;&nbsp;&zwnj;&nbsp;&zwnj;</div>' if preheader else ''}
<table width="100%" cellpadding="0" cellspacing="0" border="0" style="background:#fff">
<tr><td align="center" style="padding:40px 20px">
<table width="600" cellpadding="0" cellspacing="0" border="0" style="max-width:600px;width:100%">

  <!-- Logo -->
  <tr><td style="padding-bottom:32px;border-bottom:1px solid {_BORDER}">
    <span style="font-family:{_MONO};font-size:13px;letter-spacing:.06em;color:{_MUTED}">nable</span>
  </td></tr>

  <!-- Content -->
  <tr><td style="padding-top:32px">
    {content}
  </td></tr>

  <!-- Footer -->
  <tr><td style="padding-top:40px;border-top:1px solid {_BORDER};margin-top:40px">
    <p style="font-size:12px;color:{_MUTED};line-height:1.6;margin:0">
      You're receiving this because you signed up at <a href="https://getnable.com" style="color:{_MUTED}">getnable.com</a>.<br/>
      <a href="https://getnable.com/unsubscribe" style="color:{_MUTED}">Unsubscribe</a> · <a href="https://getnable.com" style="color:{_MUTED}">getnable.com</a>
    </p>
  </td></tr>

</table>
</td></tr>
</table>
</body>
</html>"""


def _h1(text: str) -> str:
    return f'<h1 style="font-size:24px;font-weight:400;letter-spacing:-.02em;line-height:1.3;margin:0 0 16px">{text}</h1>'


def _p(text: str, muted: bool = False) -> str:
    color = _MUTED if muted else _SLATE
    return f'<p style="font-size:15px;line-height:1.7;color:{color};margin:0 0 20px">{text}</p>'


def _code(cmd: str) -> str:
    return (
        f'<div style="background:{_BG};border:1px solid {_BORDER};border-left:3px solid {_SLATE};'
        f'border-radius:6px;padding:12px 16px;margin:0 0 12px;font-family:{_MONO};'
        f'font-size:13px;color:{_SLATE};line-height:1.6">{cmd}</div>'
    )


def _step(n: int, title: str, body: str) -> str:
    return (
        f'<div style="margin-bottom:28px">'
        f'<div style="display:flex;align-items:baseline;gap:12px;margin-bottom:8px">'
        f'<span style="font-family:{_MONO};font-size:11px;color:{_MUTED};letter-spacing:.06em;'
        f'text-transform:uppercase;white-space:nowrap">{n:02d}</span>'
        f'<span style="font-size:15px;font-weight:600;color:{_SLATE}">{title}</span>'
        f'</div>'
        f'<div style="padding-left:28px">{body}</div>'
        f'</div>'
    )


def _cta(text: str, url: str) -> str:
    return (
        f'<div style="margin:32px 0">'
        f'<a href="{url}" style="display:inline-block;background:{_SLATE};color:#fff;'
        f'text-decoration:none;padding:12px 24px;border-radius:6px;font-size:14px;'
        f'font-weight:500;letter-spacing:-.01em">{text} →</a>'
        f'</div>'
    )


def _bubble(label: str, text: str, is_user: bool = False) -> str:
    bg = _BG if not is_user else "#fff"
    border = f"border:1px solid {_BORDER};" if is_user else ""
    return (
        f'<div style="margin-bottom:12px">'
        f'<div style="font-family:{_MONO};font-size:10px;color:{_MUTED};letter-spacing:.04em;'
        f'margin-bottom:4px;text-transform:uppercase">{label}</div>'
        f'<div style="background:{bg};{border}border-radius:8px;padding:12px 14px;'
        f'font-size:13.5px;line-height:1.65;color:{_SLATE}">{text}</div>'
        f'</div>'
    )


# ── Email 1: Welcome / How it works ───────────────────────────────────────────

def welcome_html() -> str:
    content = (
        _h1("Ask Claude about your cloud bill in 10 minutes.")
        + _p("Here's exactly what setup looks like.")

        + _step(1, "Install",
            _code("pip install finops-mcp &amp;&amp; finops setup")
            + f'<p style="font-size:13px;color:{_MUTED};margin:6px 0 0">'
            f'One command. Runs an interactive wizard that connects your cloud accounts '
            f'and stores credentials in your OS keyring — never a .env file.</p>'
        )

        + _step(2, "Add to Claude Desktop",
            f'<p style="font-size:13px;color:{_MUTED};margin:0 0 8px">'
            f'Paste this into <span style="font-family:{_MONO}">claude_desktop_config.json</span>:</p>'
            + _code('{"mcpServers":{"finops":{"command":"finops-mcp"}}}')
            + f'<p style="font-size:13px;color:{_MUTED};margin:6px 0 0">'
            f'That\'s the entire config. Restart Claude Desktop and nable appears automatically.</p>'
        )

        + _step(3, "Ask",
            _bubble("You", "What\'s driving our AWS costs up this month?", is_user=True)
            + _bubble("Claude", (
                "Your AWS spend is up 23% this month — $18,400 vs $14,900 last month.<br/><br/>"
                "The spike is almost entirely EC2 in us-east-1: $6,200 this month vs $3,800 last month. "
                "The increase started June 20th.<br/><br/>"
                "I also found 3 instances that are over-provisioned — downsizing them "
                "would save about $890/month. Want me to create Jira tickets for the team?"
            ))
        )

        + f'<div style="background:{_BG};border:1px solid {_BORDER};border-radius:8px;'
        f'padding:20px 24px;margin:28px 0">'
        f'<p style="font-size:13px;color:{_MUTED};margin:0 0 4px;font-family:{_MONO};'
        f'letter-spacing:.04em;text-transform:uppercase">What\'s free</p>'
        f'<p style="font-size:14px;color:{_SLATE};margin:0;line-height:1.7">'
        f'Cost queries · Anomaly detection · Rightsizing recommendations · '
        f'Budgets &amp; alerts · Kubernetes cost analysis · '
        f'All 17 cloud + SaaS connectors · PR cost comments'
        f'</p>'
        f'</div>'

        + _cta("Start free — takes 10 minutes", "https://getnable.com/docs")

        + _p("No credit card. No cloud access on our end. nable has no backend, so your billing data never touches our servers.", muted=True)
    )
    return _base(content, preheader="Setup takes 10 minutes. Here's exactly what it looks like.")


def welcome_text() -> str:
    return """Ask Claude about your cloud bill in 10 minutes.

Here's exactly what setup looks like.

Step 1 — Install
  pip install finops-mcp && finops setup

One command. Connects your cloud accounts, stores credentials in your OS keyring.

Step 2 — Add to Claude Desktop
Paste into claude_desktop_config.json:
  {"mcpServers":{"finops":{"command":"finops-mcp"}}}

That's the entire config. Restart Claude Desktop.

Step 3 — Ask
You: "What's driving our AWS costs up this month?"

Claude: "Your AWS spend is up 23% — $18,400 vs $14,900 last month. The spike is
EC2 in us-east-1, starting June 20th. I found 3 over-provisioned instances that
would save $890/month if downsized. Want me to create Jira tickets for the team?"

What's free: cost queries, anomaly detection, rightsizing, budgets, K8s analysis,
all 17 cloud + SaaS connectors, PR cost comments.

Start free: https://getnable.com/docs

No credit card. nable has no backend, so your billing data never touches our servers.

---
getnable.com · Unsubscribe: https://getnable.com/unsubscribe"""


# ── Email 2: Day 7 nudge (no provider connected yet) ─────────────────────────

def day7_html() -> str:
    content = (
        _h1("Quick check-in — did setup go smoothly?")
        + _p("You signed up for nable a week ago. If you haven't connected a cloud account yet, the most common sticking point is the Claude Desktop config.")
        + _p("The setup wizard handles everything — AWS credentials, credential storage, and the Claude Desktop config file. One command:")
        + _code("finops setup")
        + _p("If you hit any issues, reply to this email. I read every one.", muted=True)
        + _cta("See the setup guide", "https://getnable.com/docs")
    )
    return _base(content, preheader="Most common sticking point: the Claude Desktop config. Here's the fix.")


# ── Email 3: Trial ending (3 days before) ─────────────────────────────────────

def trial_ending_html(days_left: int = 3) -> str:
    content = (
        _h1(f"Your trial ends in {days_left} day{'s' if days_left != 1 else ''}.")
        + _p("After your trial, nable stays free — cost queries, anomaly detection, rightsizing, budgets, and all connectors stay on forever.")
        + _p("The Pro plan ($25/mo) adds:")
        + f'<ul style="font-size:15px;line-height:1.9;color:{_SLATE};margin:0 0 24px;padding-left:24px">'
        f'<li>Ticket auto-creation: Jira, Linear, GitHub Issues</li>'
        f'<li>Scheduled email reports at any cadence</li>'
        f'<li>Commitment purchase recommendations with ROI</li>'
        f'<li>Org-wide multi-account cost rollup</li>'
        f'</ul>'
        + _cta("Keep Team features — first month free", "https://buy.stripe.com/eVq14mbe9ffE3le3wC2Nq02")
        + _p("After checkout, run: <span style=\"font-family:'Courier New',monospace\">finops setup license &lt;your-key&gt;</span>", muted=True)
    )
    return _base(content, preheader=f"Trial ends in {days_left} days. Free tier stays on forever — here's what changes.")


# ── Send helpers ───────────────────────────────────────────────────────────────

def _send(to_email: str, subject: str, html: str, text: str) -> bool:
    host = _env("FINOPS_SMTP_HOST")
    if not host:
        return False
    port = int(_env("FINOPS_SMTP_PORT", "587"))
    user = _env("FINOPS_SMTP_USER")
    password = _env("FINOPS_SMTP_PASSWORD")
    from_addr = _env("FINOPS_SMTP_FROM", "hello@getnable.com")

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = f"Chandan from nable <{from_addr}>"
    msg["To"] = to_email
    msg["Reply-To"] = "chandan@nable.sh"
    msg.attach(MIMEText(text, "plain"))
    msg.attach(MIMEText(html, "html"))

    try:
        ctx = ssl.create_default_context()
        with smtplib.SMTP(host, port) as server:
            server.ehlo()
            server.starttls(context=ctx)
            server.login(user, password)
            server.sendmail(from_addr, to_email, msg.as_string())
        return True
    except Exception:
        return False


def send_welcome(to_email: str) -> bool:
    return _send(
        to_email=to_email,
        subject="Ask Claude about your cloud bill — here's how (10 min setup)",
        html=welcome_html(),
        text=welcome_text(),
    )


def send_day7_nudge(to_email: str) -> bool:
    return _send(
        to_email=to_email,
        subject="Quick check-in — did nable setup go okay?",
        html=day7_html(),
        text="Hey — you signed up for nable last week. If you haven't connected a cloud account yet, the most common sticking point is the Claude Desktop config.\n\nRun: finops setup\n\nIf you hit any issues, reply to this. I read every one.\n\nhttps://getnable.com/docs",
    )


def send_trial_ending(to_email: str, days_left: int = 3) -> bool:
    return _send(
        to_email=to_email,
        subject=f"nable trial ends in {days_left} day{'s' if days_left != 1 else ''} — free tier stays on forever",
        html=trial_ending_html(days_left),
        text=f"Your trial ends in {days_left} days.\n\nAfter your trial, nable stays free — cost queries, anomaly detection, rightsizing, budgets, and all connectors.\n\nPro plan ($25/mo) adds: ticket auto-creation, scheduled email reports, commitment recommendations, org rollup.\n\nUpgrade: https://buy.stripe.com/5kQeVc4PL9Vk4piaZ42Nq0a\n\nAfter checkout, run: finops setup license <your-key>",
    )
