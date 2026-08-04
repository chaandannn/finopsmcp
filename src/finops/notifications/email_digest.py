"""
Standalone email digest — fires from the scheduler with no AI client required.
Uses Python's stdlib smtplib; supports Gmail, SES, SendGrid SMTP relay.
"""
from __future__ import annotations

import html
import os
import smtplib
import ssl
from datetime import date, timedelta
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText


def _env(key: str, default: str = "") -> str:
    return os.environ.get(key, default)


def _esc(v: object) -> str:
    """Escape provider-derived strings (service names, recommendation titles,
    descriptions) before they enter the HTML email body. They originate from
    cloud metadata a tenant does not fully control."""
    return html.escape(str(v))


def _build_html(
    period_label: str,
    total_spend: float,
    prev_total: float,
    top_providers: list[dict],
    anomalies: list[dict],
    recommendations: list[dict],
) -> str:
    pct_change = ((total_spend - prev_total) / prev_total * 100) if prev_total else 0
    change_color = "#dc2626" if pct_change > 5 else "#16a34a" if pct_change < -5 else "#64748b"
    change_label = f"+{pct_change:.1f}%" if pct_change >= 0 else f"{pct_change:.1f}%"

    provider_rows = "".join(
        f"<tr><td style='padding:8px 12px;border-bottom:1px solid #f1f5f9'>{_esc(p['provider'].upper())}</td>"
        f"<td style='padding:8px 12px;border-bottom:1px solid #f1f5f9;text-align:right'>${p['amount']:,.0f}</td>"
        f"<td style='padding:8px 12px;border-bottom:1px solid #f1f5f9;text-align:right;color:#64748b'>"
        f"{p['pct']:.1f}%</td></tr>"
        for p in top_providers
    )

    anomaly_items = "".join(
        f"<li style='margin-bottom:8px'>"
        f"<span style='color:#dc2626;font-weight:600'>{_esc(a['severity'].upper())}</span> &mdash; "
        f"{_esc(a['provider'].upper())} / {_esc(a['service'])}: "
        f"{'↑' if a['direction']=='spike' else '↓'} {abs(a['pct_change']):.0f}% "
        f"vs baseline (${a['current_amount']:,.0f})</li>"
        for a in anomalies[:5]
    ) or "<li style='color:#64748b'>No anomalies detected this week.</li>"

    rec_items = "".join(
        f"<li style='margin-bottom:8px'>"
        f"<strong>{_esc(r['title'])}</strong>: {_esc(r['description'])} "
        f"<span style='color:#16a34a;font-weight:600'>Save ${r['monthly_savings']:,.0f}/mo</span></li>"
        for r in recommendations[:5]
    ) or "<li style='color:#64748b'>No recommendations at this time.</li>"

    return f"""<!DOCTYPE html>
<html>
<head><meta charset="UTF-8"/></head>
<body style="font-family:-apple-system,BlinkMacSystemFont,'Inter',sans-serif;background:#f8fafc;margin:0;padding:32px">
<div style="max-width:600px;margin:0 auto;background:#ffffff;border-radius:12px;overflow:hidden;border:1px solid #e2e8f0">

  <!-- Header -->
  <div style="background:#0f172a;padding:24px 32px">
    <div style="display:flex;align-items:center;gap:12px">
      <div style="width:32px;height:32px;background:#16a34a;border-radius:8px;display:flex;align-items:center;justify-content:center">
        <span style="color:#fff;font-weight:900;font-size:11px">FO</span>
      </div>
      <span style="color:#fff;font-weight:700;font-size:18px">FinOps MCP</span>
    </div>
    <p style="color:#94a3b8;margin:12px 0 0;font-size:14px">Weekly Cost Digest — {period_label}</p>
  </div>

  <!-- Total spend -->
  <div style="padding:32px;border-bottom:1px solid #f1f5f9">
    <p style="color:#64748b;font-size:12px;font-weight:600;text-transform:uppercase;letter-spacing:0.05em;margin:0 0 8px">Total tracked spend</p>
    <p style="font-size:40px;font-weight:800;color:#0f172a;margin:0">${total_spend:,.0f}</p>
    <p style="font-size:14px;color:{change_color};font-weight:600;margin:4px 0 0">{change_label} vs prior week</p>
  </div>

  <!-- Provider breakdown -->
  <div style="padding:24px 32px;border-bottom:1px solid #f1f5f9">
    <p style="font-size:14px;font-weight:700;color:#0f172a;margin:0 0 16px">By provider</p>
    <table style="width:100%;border-collapse:collapse;font-size:13px">
      <tr style="background:#f8fafc">
        <th style="padding:8px 12px;text-align:left;color:#64748b;font-weight:600">Provider</th>
        <th style="padding:8px 12px;text-align:right;color:#64748b;font-weight:600">Spend</th>
        <th style="padding:8px 12px;text-align:right;color:#64748b;font-weight:600">Share</th>
      </tr>
      {provider_rows}
    </table>
  </div>

  <!-- Anomalies -->
  <div style="padding:24px 32px;border-bottom:1px solid #f1f5f9">
    <p style="font-size:14px;font-weight:700;color:#0f172a;margin:0 0 12px">
      🔴 Anomalies ({len(anomalies)} detected)
    </p>
    <ul style="margin:0;padding-left:20px;font-size:13px;color:#334155;line-height:1.7">
      {anomaly_items}
    </ul>
  </div>

  <!-- Recommendations -->
  <div style="padding:24px 32px;border-bottom:1px solid #f1f5f9">
    <p style="font-size:14px;font-weight:700;color:#0f172a;margin:0 0 12px">
      💡 Savings recommendations
    </p>
    <ul style="margin:0;padding-left:20px;font-size:13px;color:#334155;line-height:1.7">
      {rec_items}
    </ul>
  </div>

  <!-- Footer -->
  <div style="padding:20px 32px;background:#f8fafc">
    <p style="font-size:12px;color:#94a3b8;margin:0">
      Sent by FinOps MCP · <a href="#" style="color:#16a34a">Manage preferences</a>
    </p>
  </div>

</div>
</body>
</html>"""


def send_weekly_digest(
    total_spend: float,
    prev_total: float,
    top_providers: list[dict],
    anomalies: list[dict],
    recommendations: list[dict],
    period_label: str | None = None,
) -> bool:
    """
    Send the weekly digest via SMTP. Returns True on success.

    Required env vars (set via `finops setup email`):
      FINOPS_SMTP_HOST, FINOPS_SMTP_PORT, FINOPS_SMTP_USER,
      FINOPS_SMTP_PASSWORD, FINOPS_DIGEST_TO
    """
    host = _env("FINOPS_SMTP_HOST")
    if not host:
        return False

    port = int(_env("FINOPS_SMTP_PORT", "587"))
    user = _env("FINOPS_SMTP_USER")
    password = _env("FINOPS_SMTP_PASSWORD")
    to_addr = _env("FINOPS_DIGEST_TO")
    from_addr = _env("FINOPS_SMTP_FROM", user)

    if not all([host, user, password, to_addr]):
        return False

    if period_label is None:
        end = date.today()
        start = end - timedelta(days=6)
        period_label = f"{start.strftime('%b %d')} – {end.strftime('%b %d, %Y')}"

    html = _build_html(period_label, total_spend, prev_total, top_providers, anomalies, recommendations)

    msg = MIMEMultipart("alternative")
    msg["Subject"] = f"FinOps Weekly: ${total_spend:,.0f} tracked spend — {period_label}"
    msg["From"] = from_addr
    msg["To"] = to_addr
    msg.attach(MIMEText(html, "html"))

    try:
        ctx = ssl.create_default_context()
        with smtplib.SMTP(host, port) as server:
            server.ehlo()
            server.starttls(context=ctx)
            server.login(user, password)
            server.sendmail(from_addr, to_addr, msg.as_string())
        return True
    except Exception as e:
        import logging
        logging.getLogger(__name__).error("Email digest failed: %s", e)
        return False


def send_custom_digest(
    recipient: str,
    subject: str,
    body_text: str,
    report_name: str = "FinOps Report",
) -> bool:
    """
    Send a plain-text custom report to a specific recipient.
    Used by the scheduled reports system for email delivery.
    """
    host = _env("FINOPS_SMTP_HOST")
    if not host:
        return False

    port = int(_env("FINOPS_SMTP_PORT", "587"))
    user = _env("FINOPS_SMTP_USER")
    password = _env("FINOPS_SMTP_PASSWORD")
    from_addr = _env("FINOPS_SMTP_FROM", user)

    if not all([host, user, password, recipient]):
        return False

    # Escape both: report_name and body_text carry cloud data (resource names,
    # tag values, IaC paths). An AWS tag may legally contain markup, and this
    # lands in a mail client that renders HTML.
    from html import escape as _esc

    html_body = f"""<!DOCTYPE html>
<html><body style="font-family:sans-serif;max-width:700px;margin:auto;padding:24px;">
<h2 style="color:#2d3748;">{_esc(report_name)}</h2>
<pre style="background:#f7fafc;padding:16px;border-radius:8px;white-space:pre-wrap;font-family:monospace;font-size:13px;">{_esc(body_text)}</pre>
<hr style="margin:24px 0;">
<p style="color:#718096;font-size:12px;">Generated by <a href="https://github.com/nable-finops/nable">nable FinOps</a></p>
</body></html>"""

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject[:150]
    msg["From"] = from_addr
    msg["To"] = recipient
    msg.attach(MIMEText(body_text, "plain"))
    msg.attach(MIMEText(html_body, "html"))

    try:
        ctx = ssl.create_default_context()
        with smtplib.SMTP(host, port) as server:
            server.ehlo()
            server.starttls(context=ctx)
            server.login(user, password)
            server.sendmail(from_addr, recipient, msg.as_string())
        return True
    except Exception as e:
        import logging
        logging.getLogger(__name__).error("Custom email digest failed: %s", e)
        return False


def send_message(recipient: str, subject: str, text_body: str,
                 html_body: str | None = None) -> bool:
    """Send a message whose bodies the CALLER has already built.

    send_custom_digest wraps plain text in a fixed template and escapes it.
    This exists for senders that produce their own HTML (the morning brief
    renders the same document the dashboard serves), so that HTML is delivered
    as-is rather than escaped into visible tags. The caller owns escaping of
    anything it interpolated.
    """
    host = _env("FINOPS_SMTP_HOST")
    port = int(_env("FINOPS_SMTP_PORT", "587"))
    user = _env("FINOPS_SMTP_USER")
    password = _env("FINOPS_SMTP_PASSWORD")
    from_addr = _env("FINOPS_SMTP_FROM", user)
    if not all([host, user, password, recipient]):
        return False

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject[:150]
    msg["From"] = from_addr
    msg["To"] = recipient
    msg.attach(MIMEText(text_body, "plain"))
    if html_body:
        msg.attach(MIMEText(html_body, "html"))
    try:
        ctx = ssl.create_default_context()
        with smtplib.SMTP(host, port) as server:
            server.ehlo()
            server.starttls(context=ctx)
            server.login(user, password)
            server.sendmail(from_addr, recipient, msg.as_string())
        return True
    except Exception as e:
        import logging
        logging.getLogger(__name__).error("Email send failed: %s", e)
        return False
