from __future__ import annotations

import os
from datetime import date
from typing import Any

import httpx
import logging

log = logging.getLogger(__name__)

_SEVERITY_COLOR = {"high": "attention", "medium": "warning", "low": "good"}
_SEVERITY_EMOJI = {"high": "🔴", "medium": "🟡", "low": "🟢"}


def _webhook_url() -> str:
    return os.environ.get("TEAMS_WEBHOOK_URL", "")


def is_configured() -> bool:
    return bool(_webhook_url())


async def _post(card: dict) -> bool:
    url = _webhook_url()
    if not url:
        return False
    payload = {
        "type": "message",
        "attachments": [{
            "contentType": "application/vnd.microsoft.card.adaptive",
            "content": card,
        }],
    }
    async with httpx.AsyncClient(timeout=15) as client:
        r = await client.post(url, json=payload)
        return r.status_code in (200, 202)


# ── Adaptive Card builders ────────────────────────────────────────────────────

def anomaly_card(anomaly: dict[str, Any]) -> dict:
    emoji = _SEVERITY_EMOJI.get(anomaly["severity"], "⚠️")
    color = _SEVERITY_COLOR.get(anomaly["severity"], "default")
    pct = abs(anomaly["pct_change"])
    sign = "+" if anomaly["direction"] == "spike" else "-"

    # Dollar impact and the next question, added by anomaly.impact.enrich before
    # dispatch. Optional so a bare anomaly still renders.
    extra: list[dict] = []
    if anomaly.get("impact_summary"):
        extra.append({"type": "TextBlock", "text": f"**Impact** {anomaly['impact_summary']}",
                      "wrap": True})
    if anomaly.get("next_step"):
        extra.append({"type": "TextBlock", "text": f"**Next** {anomaly['next_step']}",
                      "wrap": True, "isSubtle": True})

    return {
        "$schema": "http://adaptivecards.io/schemas/adaptive-card.json",
        "type": "AdaptiveCard",
        "version": "1.4",
        "body": [
            {
                "type": "TextBlock",
                "text": f"{emoji} Cost Anomaly — {anomaly['severity'].upper()} severity",
                "weight": "Bolder",
                "size": "Medium",
                "color": color,
            },
            {
                "type": "FactSet",
                "facts": [
                    {"title": "Provider", "value": anomaly["provider"].upper()},
                    {"title": "Service", "value": anomaly["service"]},
                    {"title": "Change", "value": f"{sign}{pct:.0f}% vs 28-day baseline"},
                    {"title": "Today", "value": f"${anomaly['current_amount']:,.2f}"},
                    {"title": "Baseline avg", "value": f"${anomaly['baseline_mean']:,.2f}"},
                    {"title": "Z-score", "value": f"{anomaly['z_score']:.2f}"},
                    {"title": "Account", "value": anomaly.get("account_id", "")},
                ],
            },
            *extra,
            {
                "type": "TextBlock",
                "text": f"Detected: {anomaly.get('detected_at', '')}",
                "isSubtle": True,
                "size": "Small",
            },
        ],
    }


def daily_digest_card(
    report_date: date,
    grand_total: float,
    prev_total: float,
    by_provider: dict[str, float],
    top_services: list[dict],
    active_anomaly_count: int,
) -> dict:
    delta = grand_total - prev_total
    delta_pct = (delta / prev_total * 100) if prev_total else 0
    sign = "+" if delta >= 0 else ""
    trend = "📈" if delta > 0 else "📉" if delta < 0 else "➡️"

    provider_facts = [
        {"title": p.upper(), "value": f"${v:,.2f}"}
        for p, v in sorted(by_provider.items(), key=lambda x: -x[1])
    ]
    service_facts = [
        {"title": f"{i+1}. {s['service']}", "value": f"${s['amount_usd']:,.2f} ({s.get('pct', 0):.1f}%)"}
        for i, s in enumerate(top_services[:5])
    ]

    body: list[dict] = [
        {
            "type": "TextBlock",
            "text": f"📊 FinOps Daily — {report_date.strftime('%B %d, %Y')}",
            "weight": "Bolder",
            "size": "Large",
        },
        {
            "type": "ColumnSet",
            "columns": [
                {
                    "type": "Column",
                    "items": [
                        {"type": "TextBlock", "text": "Total spend", "isSubtle": True, "size": "Small"},
                        {"type": "TextBlock", "text": f"${grand_total:,.2f}", "weight": "Bolder", "size": "ExtraLarge"},
                    ],
                },
                {
                    "type": "Column",
                    "items": [
                        {"type": "TextBlock", "text": "vs yesterday", "isSubtle": True, "size": "Small"},
                        {"type": "TextBlock", "text": f"{trend} {sign}{delta_pct:.1f}% ({sign}${abs(delta):,.2f})", "weight": "Bolder"},
                    ],
                },
            ],
        },
        {"type": "TextBlock", "text": "By provider", "weight": "Bolder", "spacing": "Medium"},
        {"type": "FactSet", "facts": provider_facts},
        {"type": "TextBlock", "text": "Top services", "weight": "Bolder", "spacing": "Medium"},
        {"type": "FactSet", "facts": service_facts},
    ]

    if active_anomaly_count > 0:
        body.append({
            "type": "TextBlock",
            "text": f"⚠️ {active_anomaly_count} active anomaly{'s' if active_anomaly_count > 1 else ''} — ask Claude: \"show me the cost anomalies\"",
            "color": "warning",
            "weight": "Bolder",
            "spacing": "Medium",
        })

    return {
        "$schema": "http://adaptivecards.io/schemas/adaptive-card.json",
        "type": "AdaptiveCard",
        "version": "1.4",
        "body": body,
    }


_ALLOWED_WEBHOOK_SUFFIXES = (".webhook.office.com", ".office.com", ".office365.com")


async def send_to_webhook(url: str, text: str) -> bool:
    """Send plain text to an explicit Teams webhook URL (report subscriptions
    carry their own URL, unlike alerts which use the configured default)."""
    from urllib.parse import urlparse
    host = urlparse(url).hostname or ""
    if not any(host == s.lstrip(".") or host.endswith(s) for s in _ALLOWED_WEBHOOK_SUFFIXES):
        log.warning("Teams webhook host %r is not an Office webhook domain; refusing to post.", host)
        return False
    card = {
        "type": "message",
        "attachments": [{
            "contentType": "application/vnd.microsoft.card.adaptive",
            "content": {
                "$schema": "http://adaptivecards.io/schemas/adaptive-card.json",
                "type": "AdaptiveCard",
                "version": "1.4",
                "body": [{"type": "TextBlock", "text": text, "wrap": True}],
            },
        }],
    }
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.post(url, json=card)
            return resp.status_code in (200, 202)
    except Exception as exc:
        log.warning("Teams webhook post failed: %s", exc)
        return False


async def send_anomaly_alert(anomaly: dict[str, Any]) -> bool:
    return await _post(anomaly_card(anomaly))


async def send_daily_digest(
    report_date: date,
    grand_total: float,
    prev_total: float,
    by_provider: dict[str, float],
    top_services: list[dict],
    active_anomaly_count: int,
) -> bool:
    card = daily_digest_card(report_date, grand_total, prev_total, by_provider, top_services, active_anomaly_count)
    return await _post(card)
