from __future__ import annotations

import os
from datetime import date
from typing import Any

import httpx

_SEVERITY_EMOJI = {"high": "🔴", "medium": "🟡", "low": "🟢"}
_DIRECTION_EMOJI = {"spike": "📈", "drop": "📉"}


def _webhook_url() -> str:
    return os.environ.get("SLACK_WEBHOOK_URL", "")


def _bot_token() -> str:
    return os.environ.get("SLACK_BOT_TOKEN", "")


def _channel() -> str:
    return os.environ.get("SLACK_CHANNEL", "#finops-alerts")


async def send_webhook(blocks: list[dict], text: str = "") -> bool:
    url = _webhook_url()
    if not url:
        return False
    async with httpx.AsyncClient(timeout=15) as client:
        r = await client.post(url, json={"text": text, "blocks": blocks})
        return r.status_code == 200


async def send_bot(blocks: list[dict], text: str = "") -> bool:
    token = _bot_token()
    if not token:
        return False
    async with httpx.AsyncClient(timeout=15) as client:
        r = await client.post(
            "https://slack.com/api/chat.postMessage",
            headers={"Authorization": f"Bearer {token}"},
            json={"channel": _channel(), "text": text, "blocks": blocks},
        )
        return r.json().get("ok", False)


async def send(blocks: list[dict], text: str = "") -> bool:
    if _webhook_url():
        return await send_webhook(blocks, text)
    if _bot_token():
        return await send_bot(blocks, text)
    return False


def is_configured() -> bool:
    return bool(_webhook_url() or _bot_token())


# ── Block Kit builders ────────────────────────────────────────────────────────

def anomaly_blocks(anomaly: dict[str, Any]) -> list[dict]:
    emoji = _SEVERITY_EMOJI.get(anomaly["severity"], "⚠️")
    d_emoji = _DIRECTION_EMOJI.get(anomaly["direction"], "↕️")
    pct = abs(anomaly["pct_change"])
    sign = "+" if anomaly["direction"] == "spike" else "-"

    # Dollar impact and the next question. Added by anomaly.impact.enrich before
    # dispatch; absent when a caller builds blocks from a bare anomaly, so both
    # are optional and the alert renders without them.
    impact_blocks: list[dict] = []
    if anomaly.get("impact_summary"):
        impact_blocks.append(
            {"type": "section",
             "text": {"type": "mrkdwn", "text": f"*Impact*\n{anomaly['impact_summary']}"}}
        )
    if anomaly.get("next_step"):
        impact_blocks.append(
            {"type": "section",
             "text": {"type": "mrkdwn", "text": f"*Next*\n{anomaly['next_step']}"}}
        )

    return [
        {"type": "header", "text": {"type": "plain_text", "text": f"{emoji} Cost Anomaly — {anomaly['severity'].upper()} severity"}},
        {"type": "section", "fields": [
            {"type": "mrkdwn", "text": f"*Provider*\n{anomaly['provider'].upper()}"},
            {"type": "mrkdwn", "text": f"*Service*\n{anomaly['service']}"},
            {"type": "mrkdwn", "text": f"*Change*\n{d_emoji} {sign}{pct:.0f}% vs 28-day avg"},
            {"type": "mrkdwn", "text": f"*Today*\n${anomaly['current_amount']:,.2f}"},
            {"type": "mrkdwn", "text": f"*Baseline avg*\n${anomaly['baseline_mean']:,.2f}"},
            {"type": "mrkdwn", "text": f"*Z-score*\n{anomaly['z_score']:.2f}"},
        ]},
        *impact_blocks,
        {"type": "context", "elements": [
            {"type": "mrkdwn", "text": f"Detected {anomaly.get('detected_at', '')} · Account: {anomaly.get('account_id', '')}"}
        ]},
        {"type": "divider"},
    ]


def daily_digest_blocks(
    report_date: date,
    grand_total: float,
    prev_total: float,
    by_provider: dict[str, float],
    top_services: list[dict],
    active_anomaly_count: int,
    delta_basis: float | None = None,
    coverage_note: str = "",
) -> list[dict]:
    # delta_basis, not grand_total. When yesterday and the day before do not
    # cover the same providers, the caller passes the like-for-like pair so the
    # percentage is about spend and not about which snapshots landed.
    _basis = grand_total if delta_basis is None else delta_basis
    delta = _basis - prev_total
    delta_pct = (delta / prev_total * 100) if prev_total else 0
    trend_emoji = "📈" if delta > 0 else "📉" if delta < 0 else "➡️"
    sign = "+" if delta >= 0 else ""

    provider_text = "\n".join(
        f"• *{p.upper()}*: ${v:,.2f}" for p, v in sorted(by_provider.items(), key=lambda x: -x[1])
    )
    service_text = "\n".join(
        f"{i+1}. {s['service']}: *${s['amount_usd']:,.2f}* ({s.get('pct', 0):.1f}%)"
        for i, s in enumerate(top_services[:5])
    )

    blocks: list[dict] = [
        {"type": "header", "text": {"type": "plain_text", "text": f"📊 FinOps Daily — {report_date.strftime('%B %d, %Y')}"}},
        {"type": "section", "fields": [
            {"type": "mrkdwn", "text": f"*Total spend*\n${grand_total:,.2f}"},
            {"type": "mrkdwn", "text": f"*vs yesterday*\n{trend_emoji} {sign}{delta_pct:.1f}% ({sign}${abs(delta):,.2f})"},
        ]},
        *([{"type": "section", "text": {"type": "mrkdwn", "text": coverage_note}}]
          if coverage_note else []),
        {"type": "section", "text": {"type": "mrkdwn", "text": f"*By provider*\n{provider_text}"}},
        {"type": "section", "text": {"type": "mrkdwn", "text": f"*Top services*\n{service_text}"}},
    ]

    if active_anomaly_count > 0:
        blocks.append({
            "type": "section",
            "text": {"type": "mrkdwn", "text": f"⚠️ *{active_anomaly_count} active anomaly{'s' if active_anomaly_count > 1 else ''}* detected — review with Claude: _\"show me the cost anomalies\"_"},
        })

    blocks.append({"type": "divider"})
    return blocks


def weekly_insight_blocks(
    period_label: str,
    grand_total: float,
    prev_total: float,
    top_movers: list[dict],       # [{service, provider, this_week, last_week, pct_change}]
    open_savings_usd: float,
    verified_savings_usd: float,
    active_anomalies: int,
    budget_alerts: list[dict],    # [{name, pct_used, status}]
    top_action: str = "",
) -> list[dict]:
    """
    Rich weekly insight push — reads like an analyst briefing, not a metric dump.
    Covers: week-over-week spend, top movers, open savings, budget status, next action.
    """
    delta = grand_total - prev_total
    delta_pct = (delta / prev_total * 100) if prev_total else 0
    trend = "📈" if delta > 0 else "📉" if delta < 0 else "➡️"
    sign = "+" if delta >= 0 else ""

    blocks: list[dict] = [
        {
            "type": "header",
            "text": {"type": "plain_text", "text": f"💡 Weekly Cost Intelligence — {period_label}"},
        },
        {
            "type": "section",
            "fields": [
                {"type": "mrkdwn", "text": f"*This week*\n${grand_total:,.0f}"},
                {"type": "mrkdwn", "text": f"*vs last week*\n{trend} {sign}{delta_pct:.1f}% (${delta:+,.0f})"},
            ],
        },
    ]

    # Top movers
    if top_movers:
        mover_lines = []
        for m in top_movers[:5]:
            pct = m.get("pct_change", 0)
            arrow = "↑" if pct > 0 else "↓"
            mover_lines.append(
                f"• *{m['service']}* ({m.get('provider','').upper()}): "
                f"{arrow}{abs(pct):.0f}% · ${m.get('this_week', 0):,.0f} this week"
            )
        blocks.append({
            "type": "section",
            "text": {"type": "mrkdwn", "text": "*Top movers*\n" + "\n".join(mover_lines)},
        })

    # Savings row
    savings_parts = []
    if open_savings_usd > 0:
        savings_parts.append(f"*${open_savings_usd:,.0f}/mo* open")
    if verified_savings_usd > 0:
        savings_parts.append(f"*${verified_savings_usd:,.0f}/mo* verified ✓")
    if savings_parts:
        blocks.append({
            "type": "section",
            "fields": [
                {"type": "mrkdwn", "text": f"*Savings pipeline*\n{' · '.join(savings_parts)}"},
                {"type": "mrkdwn", "text": f"*Anomalies*\n{'⚠️ ' + str(active_anomalies) + ' active' if active_anomalies else '✅ None'}"},
            ],
        })

    # Budget alerts
    if budget_alerts:
        alert_lines = []
        for b in budget_alerts[:3]:
            pct = b.get("pct_used", 0)
            emoji = "🔴" if pct >= 100 else "🟡"
            alert_lines.append(f"{emoji} *{b['name']}*: {pct:.0f}% of budget used")
        blocks.append({
            "type": "section",
            "text": {"type": "mrkdwn", "text": "*Budget alerts*\n" + "\n".join(alert_lines)},
        })

    # Top action
    action_text = top_action or "Ask Claude: _\"what should I focus on this week?\"_"
    blocks.append({
        "type": "context",
        "elements": [{"type": "mrkdwn", "text": f"💬 {action_text}"}],
    })
    blocks.append({"type": "divider"})
    return blocks


async def send_weekly_insight(
    period_label: str,
    grand_total: float,
    prev_total: float,
    top_movers: list[dict],
    open_savings_usd: float = 0.0,
    verified_savings_usd: float = 0.0,
    active_anomalies: int = 0,
    budget_alerts: list[dict] | None = None,
    top_action: str = "",
) -> bool:
    blocks = weekly_insight_blocks(
        period_label=period_label,
        grand_total=grand_total,
        prev_total=prev_total,
        top_movers=top_movers,
        open_savings_usd=open_savings_usd,
        verified_savings_usd=verified_savings_usd,
        active_anomalies=active_anomalies,
        budget_alerts=budget_alerts or [],
        top_action=top_action,
    )
    delta_pct = ((grand_total - prev_total) / prev_total * 100) if prev_total else 0
    sign = "+" if delta_pct >= 0 else ""
    text = f"Weekly cost: ${grand_total:,.0f} ({sign}{delta_pct:.1f}% vs last week)"
    return await send(blocks, text)


async def send_anomaly_alert(anomaly: dict[str, Any]) -> bool:
    blocks = anomaly_blocks(anomaly)
    pct = abs(anomaly["pct_change"])
    sign = "+" if anomaly["direction"] == "spike" else "-"
    text = f"Cost anomaly: {anomaly['provider']} / {anomaly['service']} {sign}{pct:.0f}% ({anomaly['severity']})"
    return await send(blocks, text)


async def send_daily_digest(
    report_date: date,
    grand_total: float,
    prev_total: float,
    by_provider: dict[str, float],
    top_services: list[dict],
    active_anomaly_count: int,
    delta_basis: float | None = None,
    coverage_note: str = "",
) -> bool:
    """`delta_basis` is the yesterday figure the change is computed FROM, which
    is not always `grand_total`. When the two days do not cover the same
    providers, the caller narrows both sides to the providers present in both,
    so the percentage describes spend instead of coverage. `grand_total` stays
    the real, complete yesterday number for the headline.

    `coverage_note`, when set, names what is missing and must be rendered. A
    percentage a reader cannot see the caveat for is the defect this fixes: a
    failed AWS snapshot produced "vs yesterday: -94.1% (-$90,000.00)" when
    nothing had dropped."""
    blocks = daily_digest_blocks(
        report_date, grand_total, prev_total, by_provider, top_services,
        active_anomaly_count, delta_basis=delta_basis, coverage_note=coverage_note)
    return await send(blocks, f"FinOps daily digest — ${grand_total:,.2f} total spend")
