"""
nable Slack slash commands + interactive Block Kit responses.

Slash commands (register in Slack App dashboard):
  /cost          — total spend summary for default account
  /cost [service]— drill into a specific service
  /anomalies     — unacknowledged cost spikes
  /forecast      — 30-day spend projection
  /rightsizing   — top rightsizing opportunities
  /budget        — budget status for all active budgets
  /estimate      — estimate a Terraform plan (paste JSON)
  /nable help    — show all commands

Each command returns rich Block Kit messages with:
  - Summary stat blocks
  - Trend sparkline (unicode bar chart)
  - Action buttons (Acknowledge, Create ticket, Snooze)
  - Overflow menus for drill-down

Env vars:
  SLACK_BOT_TOKEN       — xoxb- token
  SLACK_SIGNING_SECRET  — for request verification
  NABLE_DEFAULT_ACCOUNT — AWS account ID to use when none specified
"""
from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import time
from datetime import date, timedelta
from typing import Any

log = logging.getLogger(__name__)

DEFAULT_ACCOUNT = os.environ.get("NABLE_DEFAULT_ACCOUNT", "")

# ── Sparkline helpers ─────────────────────────────────────────────────────────

_SPARK_CHARS = "▁▂▃▄▅▆▇█"

def _sparkline(values: list[float], width: int = 14) -> str:
    """Render a unicode sparkline from a list of floats."""
    if not values:
        return ""
    vals = values[-width:]
    mn, mx = min(vals), max(vals)
    if mn == mx:
        return _SPARK_CHARS[3] * len(vals)
    return "".join(
        _SPARK_CHARS[int((v - mn) / (mx - mn) * (len(_SPARK_CHARS) - 1))]
        for v in vals
    )


def _trend_emoji(values: list[float]) -> str:
    if len(values) < 2:
        return ""
    pct = (values[-1] - values[-7]) / max(values[-7], 1) * 100 if len(values) >= 7 else 0
    if pct > 20: return "🔴 ↑"
    if pct > 5:  return "🟡 ↗"
    if pct < -10: return "🟢 ↓"
    return "⚪ →"


# ── Block Kit builders ────────────────────────────────────────────────────────

def _header_block(text: str) -> dict:
    return {"type": "header", "text": {"type": "plain_text", "text": text, "emoji": True}}


def _section(text: str, accessory: dict | None = None) -> dict:
    block: dict = {"type": "section", "text": {"type": "mrkdwn", "text": text}}
    if accessory:
        block["accessory"] = accessory
    return block


def _divider() -> dict:
    return {"type": "divider"}


def _fields_block(fields: list[str]) -> dict:
    return {
        "type": "section",
        "fields": [{"type": "mrkdwn", "text": f} for f in fields],
    }


def _button(text: str, action_id: str, value: str, style: str = "default") -> dict:
    btn: dict = {
        "type": "button",
        "text": {"type": "plain_text", "text": text, "emoji": True},
        "action_id": action_id,
        "value": value,
    }
    if style in ("primary", "danger"):
        btn["style"] = style
    return btn


def _actions(*buttons: dict) -> dict:
    return {"type": "actions", "elements": list(buttons)}


def _context(text: str) -> dict:
    return {"type": "context", "elements": [{"type": "mrkdwn", "text": text}]}


# ── Command handlers ──────────────────────────────────────────────────────────

def cmd_cost(account_id: str, service: str | None, days: int = 30) -> list[dict]:
    """Handle /cost [service] — return spend summary blocks."""
    try:
        from ..storage.db import get_engine
        from sqlalchemy import text as sql_text

        engine = get_engine()
        end    = date.today()
        start  = end - timedelta(days=days)

        with engine.connect() as conn:
            if service:
                rows = conn.execute(sql_text("""
                    SELECT snapshot_date, SUM(amount_usd) FROM cost_snapshots
                    WHERE account_id = :aid AND service LIKE :svc
                      AND snapshot_date BETWEEN :start AND :end
                    GROUP BY snapshot_date ORDER BY snapshot_date
                """), {"aid": account_id, "svc": f"%{service}%",
                       "start": start.isoformat(), "end": end.isoformat()}).fetchall()
            else:
                rows = conn.execute(sql_text("""
                    SELECT snapshot_date, SUM(amount_usd) FROM cost_snapshots
                    WHERE account_id = :aid
                      AND snapshot_date BETWEEN :start AND :end
                    GROUP BY snapshot_date ORDER BY snapshot_date
                """), {"aid": account_id,
                       "start": start.isoformat(), "end": end.isoformat()}).fetchall()

        if not rows:
            return [_section(f"No cost data for account `{account_id}`. Run `finops snapshot` first.")]

        values    = [float(r[1]) for r in rows]
        total     = sum(values)
        daily_avg = total / len(values)
        proj_30   = daily_avg * 30
        spark     = _sparkline(values)
        trend     = _trend_emoji(values)

        title = f"☁ {service or 'Total'} spend — last {days}d"
        blocks = [
            _header_block(title),
            _fields_block([
                f"*Total ({days}d)*\n${total:,.2f}",
                f"*Daily avg*\n${daily_avg:,.2f}",
                f"*30-day projection*\n${proj_30:,.2f}",
                f"*Trend*\n{trend}",
            ]),
            _section(f"`{spark}`"),
            _actions(
                _button("🔍 Breakdown by service", "cost_breakdown", account_id),
                _button("📈 Forecast", "show_forecast", account_id),
                _button("🐛 Anomalies", "show_anomalies", account_id),
            ),
            _context(f"Account: `{account_id}` · {start} → {end} · via nable"),
        ]
        return blocks

    except Exception as e:
        log.exception("cmd_cost failed")
        log.debug("cmd_cost exception detail: %s", e)
        return [_section("Error fetching costs. Check server logs for details.")]


def cmd_anomalies(account_id: str, limit: int = 5) -> list[dict]:
    """Handle /anomalies — show unacknowledged spikes."""
    try:
        from ..storage.db import get_engine
        from sqlalchemy import text as sql_text

        engine = get_engine()
        with engine.connect() as conn:
            rows = conn.execute(sql_text("""
                SELECT id, service, snapshot_date, severity, pct_change, current_amount, baseline_mean
                FROM anomalies
                WHERE account_id = :aid AND acknowledged = 0
                ORDER BY detected_at DESC LIMIT :lim
            """), {"aid": account_id, "lim": limit}).fetchall()

        if not rows:
            return [_section("✅ No unacknowledged anomalies. All clear!")]

        blocks: list[dict] = [_header_block(f"🚨 {len(rows)} unacknowledged anomaly(ies)")]
        for row in rows:
            id_, svc, dt, sev, pct, current, baseline = row
            sev_emoji = {"high": "🔴", "medium": "🟡", "low": "⚪"}.get(sev, "⚪")
            blocks += [
                _divider(),
                _section(
                    f"{sev_emoji} *{svc}* on {dt}\n"
                    f"${current:,.2f} vs ${baseline:,.2f} baseline (+{pct:.0f}%)"
                ),
                _actions(
                    _button("✅ Acknowledge", "ack_anomaly", str(id_), "primary"),
                    _button("🎫 Create ticket", "ticket_anomaly", str(id_)),
                    _button("😴 Snooze 7d", "snooze_anomaly", str(id_)),
                ),
            ]

        blocks.append(_context(f"Account: `{account_id}` · via nable"))
        return blocks

    except Exception as e:
        log.exception("cmd_anomalies failed")
        log.debug("cmd_anomalies exception detail: %s", e)
        return [_section("Error fetching anomalies. Check server logs for details.")]


def cmd_forecast(account_id: str, service: str | None = None, horizon: int = 30) -> list[dict]:
    """Handle /forecast — 30-day projection with confidence band."""
    try:
        from ..ml.forecasting import Forecaster

        f = Forecaster.for_account(account_id, service=service, days=90)
        if not f._series:
            return [_section("No history found. Snapshot data needed first.")]

        result = f.predict(horizon)
        total  = result.monthly_projection
        method = result.method.replace("_", " ").title()
        mape   = result.mape

        # Sparkline of forecast
        spark = _sparkline(result.point)
        conf_note = f"Method: {method}" + (f" · MAPE: {mape:.1f}%" if mape else "")

        blocks = [
            _header_block(f"📈 {service or 'Total'} cost forecast — {horizon}d"),
            _fields_block([
                f"*Projected 30-day total*\n${total:,.2f}",
                f"*Daily avg (forecast)*\n${total/30:,.2f}",
                f"*Lower bound (80%)*\n${sum(result.lower[:30]):,.2f}",
                f"*Upper bound (80%)*\n${sum(result.upper[:30]):,.2f}",
            ]),
            _section(f"Forecast `{spark}`"),
            _context(f"{conf_note} · Account: `{account_id}` · via nable"),
        ]
        return blocks

    except Exception as e:
        log.exception("cmd_forecast failed")
        log.debug("cmd_forecast exception detail: %s", e)
        return [_section("Error generating forecast. Check server logs for details.")]


def cmd_rightsizing(account_id: str) -> list[dict]:
    """Handle /rightsizing — top opportunities."""
    try:
        # `get_rightsizing_recommendations` is the name of the MCP TOOL, not of
        # anything in the analyzer, and importing it from here raised ImportError
        # on every /rightsizing the bot has ever received. The analyzer returns
        # dataclasses; the blocks below want dicts, so map once, here.
        from ..recommendations.rightsizing import analyze_rightsizing

        recs = [
            {
                "instance_id": r.instance_id,
                "current_type": r.instance_type,
                "recommended_type": r.recommended_type,
                "estimated_monthly_savings": r.monthly_savings,
                "region": r.region,
                "confidence": r.confidence,
            }
            # analyze_rightsizing sweeps regions, not accounts, so the account
            # filter is applied here rather than dropped. Dropping it would show
            # a caller who asked about one account the whole org's instances.
            for r in analyze_rightsizing()
            if not account_id or r.account_id == account_id
        ][:5]

        if not recs:
            return [_section("✅ No rightsizing opportunities found.")]

        total_saving = sum(r.get("estimated_monthly_savings", 0) for r in recs)
        blocks = [
            _header_block("📐 Top rightsizing opportunities"),
            _section(f"*Estimated total saving: ${total_saving:,.2f}/mo (${total_saving*12:,.0f}/yr)*"),
        ]

        for r in recs[:5]:
            svc  = r.get("instance_id", "")
            curr = r.get("current_type", "")
            rec  = r.get("recommended_type", "")
            save = r.get("estimated_monthly_savings", 0)
            blocks += [
                _divider(),
                _section(
                    f"*{svc}*\n`{curr}` → `{rec}` · *${save:,.2f}/mo saving*"
                ),
                _actions(
                    _button("🎫 Create ticket", "ticket_rightsizing", json.dumps(r)[:100]),
                    _button("📋 View details", "detail_rightsizing", r.get("instance_id", "")),
                ),
            ]

        blocks.append(_context(f"Account: `{account_id}` · via nable"))
        return blocks

    except Exception as e:
        log.exception("cmd_rightsizing failed")
        log.debug("cmd_rightsizing exception detail: %s", e)
        return [_section("Error fetching rightsizing data. Check server logs for details.")]


def cmd_budget(account_id: str) -> list[dict]:
    """Handle /budget — show all budget statuses."""
    try:
        from ..storage.db import get_engine
        from sqlalchemy import text as sql_text

        engine = get_engine()
        with engine.connect() as conn:
            budgets = conn.execute(sql_text("""
                SELECT name, monthly_limit_usd, alert_threshold_pct, is_active
                FROM budgets WHERE is_active = 1 ORDER BY monthly_limit_usd DESC
            """)).fetchall()

        if not budgets:
            return [_section("No active budgets configured. Use `finops budget` to set them up.")]

        blocks = [_header_block("💰 Budget Status")]

        # Month-start cutoff, computed in Python: date('now', ...) is SQLite-only and
        # raises on Postgres (the shared-team mode this Slack path runs in).
        from datetime import date as _date_cls
        _month_start = _date_cls.today().replace(day=1).isoformat()
        for b in budgets:
            name, limit, threshold, _ = b
            # Fetch current month spend
            with engine.connect() as conn:
                row = conn.execute(sql_text("""
                    SELECT SUM(amount_usd) FROM cost_snapshots
                    WHERE snapshot_date >= :month_start
                      AND account_id = :aid
                """), {"aid": account_id, "month_start": _month_start}).fetchone()
            spent = float(row[0] or 0)
            pct   = spent / limit * 100 if limit else 0
            bar_fill = int(pct / 10)
            bar  = "█" * bar_fill + "░" * (10 - bar_fill)
            emoji = "🔴" if pct >= 100 else ("🟡" if pct >= threshold else "🟢")
            blocks.append(_section(
                f"{emoji} *{name}*\n"
                f"`{bar}` {pct:.0f}%  ·  ${spent:,.2f} / ${limit:,.2f}"
            ))

        blocks.append(_context(f"Account: `{account_id}` · via nable"))
        return blocks

    except Exception as e:
        log.exception("cmd_budget failed")
        log.debug("cmd_budget exception detail: %s", e)
        return [_section("Error fetching budget data. Check server logs for details.")]


def cmd_help() -> list[dict]:
    return [
        _header_block("☁ nable — Cloud Cost Intelligence"),
        _section(
            "*Slash commands:*\n\n"
            "• `/cost` — spend summary (last 30 days)\n"
            "• `/cost EC2` — drill into a specific service\n"
            "• `/anomalies` — unacknowledged cost spikes\n"
            "• `/forecast` — 30-day spend projection\n"
            "• `/rightsizing` — top rightsizing opportunities\n"
            "• `/budget` — budget status\n"
            "• `/nable help` — this message"
        ),
        _context("nable · [Docs](https://github.com/getnable/finopsmcp) · EL2 license"),
    ]


# ── Interaction handler (button clicks) ───────────────────────────────────────

def handle_interaction(payload: dict) -> list[dict]:
    """Handle Block Kit button actions."""
    try:
        action = payload["actions"][0]
        action_id = action["action_id"]
        value     = action.get("value", "")
        account   = DEFAULT_ACCOUNT

        if action_id == "ack_anomaly":
            from ..storage.db import get_engine
            from sqlalchemy import text as sql_text
            engine = get_engine()
            with engine.begin() as conn:
                conn.execute(sql_text(
                    "UPDATE anomalies SET acknowledged=1 WHERE id=:id"
                ), {"id": int(value)})
            return [_section(f"✅ Anomaly #{value} acknowledged.")]

        if action_id == "cost_breakdown":
            return cmd_cost(value or account, None, 7)

        if action_id == "show_forecast":
            return cmd_forecast(value or account)

        if action_id == "show_anomalies":
            return cmd_anomalies(value or account)

        return [_section(f"Action `{action_id}` not yet implemented.")]

    except Exception as e:
        log.exception("interaction handler failed")
        log.debug("interaction handler exception detail: %s", e)
        return [_section("An error occurred handling this action. Check server logs for details.")]


# ── Request verification ───────────────────────────────────────────────────────

def verify_slack_request(body: bytes, timestamp: str, signature: str, secret: str) -> bool:
    # Fail closed: an empty signing secret would HMAC with an empty key, letting
    # anyone who knows the body forge a matching signature.
    if not secret:
        return False
    try:
        ts = float(timestamp)
    except (TypeError, ValueError):
        return False  # attacker-controlled non-numeric timestamp
    if abs(time.time() - ts) > 300:
        return False
    basestring = f"v0:{timestamp}:{body.decode()}"
    expected   = "v0=" + hmac.new(
        secret.encode(), basestring.encode(), hashlib.sha256
    ).hexdigest()
    return hmac.compare_digest(expected, signature)


# ── Dispatch ──────────────────────────────────────────────────────────────────

def dispatch(command: str, text: str, account_id: str = "") -> list[dict]:
    """Route a slash command to the right handler. Returns Block Kit blocks."""
    account = account_id or DEFAULT_ACCOUNT
    text    = (text or "").strip()
    cmd     = command.lstrip("/").lower()

    if cmd in ("cost", "spend"):
        parts = text.split(None, 1)
        service = parts[0] if parts else None
        return cmd_cost(account, service)

    if cmd in ("anomalies", "anomaly", "alerts"):
        return cmd_anomalies(account)

    if cmd in ("forecast", "predict"):
        return cmd_forecast(account)

    if cmd in ("rightsizing", "right-sizing", "resize"):
        return cmd_rightsizing(account)

    if cmd in ("budget", "budgets"):
        return cmd_budget(account)

    if text.lower() == "help" or cmd == "help":
        return cmd_help()

    return cmd_help()
