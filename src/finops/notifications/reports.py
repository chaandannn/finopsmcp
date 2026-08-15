"""
Scheduled custom reports — user-configurable, multi-channel, any frequency.

Each report subscription defines:
  - What to include (sections: spend, anomalies, scorecard, k8s, commitments,
                     rightsizing, budgets, teams)
  - Where to send it (Slack channels, email addresses, Teams webhook)
  - When to send it  (cron expression or preset: daily, weekly, monthly)
  - Scope filters    (team, provider, env tag)

Reports are stored in the DB (report_subscriptions table) and run by the
APScheduler job `job_run_reports`. They can also be triggered on-demand
via the MCP tool `send_report_now`.
"""
from __future__ import annotations

import json
import logging
from datetime import date, datetime, timedelta, timezone
from typing import Any

log = logging.getLogger(__name__)

# ── Preset cron expressions ───────────────────────────────────────────────────

PRESET_CRONS = {
    "daily":   "0 9 * * *",      # 09:00 UTC every day
    "weekday": "0 9 * * 1-5",    # 09:00 UTC Mon–Fri
    "weekly":  "0 9 * * 1",      # 09:00 UTC Monday
    "monthly": "0 9 1 * *",      # 09:00 UTC first of month
}


# ── Section generators — each returns a Slack blocks list + plain-text summary ─

async def _section_spend(filters: dict, lookback_days: int) -> tuple[list[dict], str]:
    """Total spend + provider breakdown."""
    try:
        from ..storage.db import cost_snapshots, get_engine
        from sqlalchemy import func, select

        today = date.today()
        start = (today - timedelta(days=lookback_days)).isoformat()
        prev_start = (today - timedelta(days=lookback_days * 2)).isoformat()

        engine = get_engine()
        with engine.connect() as conn:
            def period_total(s: str, e: str) -> float:
                q = select(func.sum(cost_snapshots.c.amount_usd)).where(
                    cost_snapshots.c.snapshot_date >= s,
                    cost_snapshots.c.snapshot_date < e,
                )
                if filters.get("provider"):
                    q = q.where(cost_snapshots.c.provider == filters["provider"])
                return float(conn.execute(q).scalar() or 0)

            total = period_total(start, today.isoformat())
            prev  = period_total(prev_start, start)

            rows = conn.execute(
                select(
                    cost_snapshots.c.provider,
                    func.sum(cost_snapshots.c.amount_usd).label("t"),
                )
                .where(cost_snapshots.c.snapshot_date >= start)
                .group_by(cost_snapshots.c.provider)
                .order_by(func.sum(cost_snapshots.c.amount_usd).desc())
            ).fetchall()

            svc_rows = conn.execute(
                select(
                    cost_snapshots.c.service,
                    func.sum(cost_snapshots.c.amount_usd).label("t"),
                )
                .where(cost_snapshots.c.snapshot_date >= start)
                .group_by(cost_snapshots.c.service)
                .order_by(func.sum(cost_snapshots.c.amount_usd).desc())
                .limit(5)
            ).fetchall()

        delta_pct = ((total - prev) / prev * 100) if prev else 0
        trend = "📈" if delta_pct > 2 else "📉" if delta_pct < -2 else "➡️"
        sign = "+" if delta_pct >= 0 else ""

        provider_lines = "\n".join(
            f"  • *{r.provider.upper()}*: ${float(r.t):,.0f}" for r in rows
        )
        svc_lines = "\n".join(
            f"  {i+1}. {r.service}: *${float(r.t):,.0f}*" for i, r in enumerate(svc_rows)
        )

        blocks: list[dict] = [
            {"type": "section", "fields": [
                {"type": "mrkdwn", "text": f"*💰 Total spend ({lookback_days}d)*\n${total:,.0f}"},
                {"type": "mrkdwn", "text": f"*vs prior period*\n{trend} {sign}{delta_pct:.1f}%"},
            ]},
        ]
        if provider_lines:
            blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": f"*By provider*\n{provider_lines}"}})
        if svc_lines:
            blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": f"*Top services*\n{svc_lines}"}})

        text = f"Total spend: ${total:,.0f} ({sign}{delta_pct:.1f}% vs prior {lookback_days}d)"
        return blocks, text
    except Exception as e:
        log.warning("spend section failed: %s", e)
        return [], ""


async def _section_anomalies(filters: dict, lookback_days: int) -> tuple[list[dict], str]:
    try:
        from ..anomaly.detector import get_active_anomalies
        anomalies = get_active_anomalies(limit=10)
        if filters.get("provider"):
            anomalies = [a for a in anomalies if a.get("provider") == filters["provider"]]

        if not anomalies:
            return [{"type": "section", "text": {"type": "mrkdwn", "text": "✅ *Anomalies* — None detected"}}], ""

        high = [a for a in anomalies if a.get("severity") == "high"]
        med  = [a for a in anomalies if a.get("severity") == "medium"]
        summary_lines = []
        for a in anomalies[:5]:
            emoji = "🔴" if a.get("severity") == "high" else "🟡"
            d = "📈" if a.get("direction") == "spike" else "📉"
            pct = abs(a.get("pct_change", 0))
            summary_lines.append(f"{emoji} {d} {a.get('provider','').upper()}/{a.get('service','')} {pct:.0f}% ({a.get('severity','')})")

        text_block = "\n".join(summary_lines)
        blocks = [{"type": "section", "text": {"type": "mrkdwn", "text": f"⚠️ *Anomalies* — {len(high)} high, {len(med)} medium\n{text_block}"}}]
        return blocks, f"{len(anomalies)} active anomalies ({len(high)} high)"
    except Exception as e:
        log.warning("anomalies section failed: %s", e)
        return [], ""


async def _section_scorecard(filters: dict, **_) -> tuple[list[dict], str]:
    try:
        from ..scoring.scorecard import build_scorecard
        tag_filter = {}
        if filters.get("team"):
            tag_filter["team"] = filters["team"]

        sc = build_scorecard(tag_filter=tag_filter or None)
        if not sc:
            return [], ""

        d = sc.as_dict()
        grade = d.get("grade", "?")
        score = d.get("overall_score", 0)
        trend = d.get("trend", "")
        trend_emoji = {"improving": "📈", "declining": "📉", "stable": "➡️"}.get(trend, "")

        dim_lines = "\n".join(
            f"  • {dim['dimension'].replace('_',' ').title()}: *{dim['score']}* ({dim['grade']})"
            for dim in d.get("dimensions", [])
        )
        scope_label = f" — {filters['team']}" if filters.get("team") else ""
        blocks = [{"type": "section", "text": {"type": "mrkdwn", "text": (
            f"📊 *Scorecard{scope_label}* — Grade *{grade}* ({score}/100) {trend_emoji}\n{dim_lines}"
        )}}]
        return blocks, f"Scorecard: {grade} ({score}/100)"
    except Exception as e:
        log.warning("scorecard section failed: %s", e)
        return [], ""


async def _section_k8s(**_) -> tuple[list[dict], str]:
    try:
        from ..connectors.kubernetes import KubernetesConnector
        reports = KubernetesConnector().analyze_all_clusters()
        if not reports:
            return [], ""

        lines = []
        total_waste = 0.0
        for r in reports:
            # r is a ClusterReport dataclass; rightsizing_opportunities is list[dict]
            waste = sum(opp.get("potential_savings_usd", 0) for opp in r.rightsizing_opportunities)
            total_waste += waste
            lines.append(f"  • *{r.cluster}*: ${r.total_monthly_cost:,.0f}/mo, ${waste:,.0f} waste")

        blocks = [{"type": "section", "text": {"type": "mrkdwn", "text": (
            f"☸️ *Kubernetes* — {len(reports)} cluster(s), ${total_waste:,.0f}/mo waste\n" + "\n".join(lines)
        )}}]
        return blocks, f"K8s: {len(reports)} clusters, ${total_waste:,.0f}/mo waste"
    except Exception as e:
        log.warning("k8s section failed: %s", e)
        return [], ""


async def _section_commitments(filters: dict, **_) -> tuple[list[dict], str]:
    try:
        from ..recommendations.commitments import analyze_commitments
        tag_filter = {k: filters[k] for k in ("team", "env") if filters.get(k)}
        analysis = analyze_commitments(tag_filter=tag_filter or None)
        if not analysis:
            return [], ""

        # None means the CE coverage call was denied, not that coverage is zero.
        # Say "unknown" rather than crashing on the comparison or, worse, painting
        # a red light on a customer whose coverage nobody could read.
        sp_cov = analysis.savings_plan_coverage_pct
        ri_cov = analysis.ri_coverage_pct
        waste  = analysis.total_waste_usd
        score  = analysis.combined_coverage_pct
        emoji  = "⚪" if score is None else "🟢" if score >= 80 else "🟡" if score >= 50 else "🔴"

        blocks = [{"type": "section", "text": {"type": "mrkdwn", "text": (
            f"{emoji} *Commitments* — SP coverage "
            f"{'unknown' if sp_cov is None else format(sp_cov, '.0f') + '%'}, "
            f"RI coverage {'unknown' if ri_cov is None else format(ri_cov, '.0f') + '%'}\n"
            f"  Unused commitment waste: *${waste:,.0f}/mo*"
        )}}]
        # The plain-text fallback needs the same None handling as the blocks
        # above. It did not have it, and because this whole section is wrapped in
        # a try/except that logs and returns empty, the result was not a crash: a
        # customer missing ce:GetSavingsPlansCoverage simply had the commitments
        # section VANISH from their weekly digest, every week, with the reason
        # only in a debug log they never see. A missing section reads as "nothing
        # to report here", which is the opposite of true for an account with
        # unreadable coverage and $50,000/mo uncovered on-demand.
        _sp = "unknown" if sp_cov is None else f"{sp_cov:.0f}%"
        _ri = "unknown" if ri_cov is None else f"{ri_cov:.0f}%"
        return blocks, f"SP {_sp} / RI {_ri} coverage, ${waste:,.0f}/mo waste"
    except Exception as e:
        log.warning("commitments section failed: %s", e)
        return [], ""


async def _section_rightsizing(**_) -> tuple[list[dict], str]:
    try:
        from ..recommendations.rightsizing import analyze_rightsizing, rightsizing_summary
        rs = analyze_rightsizing()
        if not rs:
            return [], ""
        summary = rightsizing_summary(rs)
        recs = summary.get("recommendations", [])[:5]
        total_savings = sum(r.get("monthly_savings", 0) for r in recs)
        lines = "\n".join(
            f"  • {r.get('title','')}: save *${r.get('monthly_savings',0):,.0f}/mo*"
            for r in recs
        )
        blocks = [{"type": "section", "text": {"type": "mrkdwn", "text": (
            f"⚡ *Rightsizing* — ${total_savings:,.0f}/mo potential savings\n{lines}"
        )}}]
        return blocks, f"Rightsizing: ${total_savings:,.0f}/mo potential savings"
    except Exception as e:
        log.warning("rightsizing section failed: %s", e)
        return [], ""


async def _section_budgets(**_) -> tuple[list[dict], str]:
    try:
        from ..budget.enforcer import check_all_budgets
        results = check_all_budgets()
        if not results:
            return [{"type": "section", "text": {"type": "mrkdwn", "text": "💚 *Budgets* — All within limits"}}], ""

        exceeded = [b for b in results if b.get("status") == "exceeded"]
        warning  = [b for b in results if b.get("status") == "warning"]
        lines = []
        for b in results[:6]:
            status_emoji = "🔴" if b.get("status") == "exceeded" else "🟡"
            lines.append(f"  {status_emoji} *{b['name']}*: ${b['spent']:,.0f} / ${b['limit']:,.0f} ({b['pct_used']:.0f}%)")

        blocks = [{"type": "section", "text": {"type": "mrkdwn", "text": (
            f"💰 *Budgets* — {len(exceeded)} exceeded, {len(warning)} warnings\n" + "\n".join(lines)
        )}}]
        return blocks, f"{len(exceeded)} budgets exceeded, {len(warning)} warnings"
    except Exception as e:
        log.warning("budgets section failed: %s", e)
        return [], ""


async def _section_teams(filters: dict, lookback_days: int) -> tuple[list[dict], str]:
    try:
        from ..storage.db import attributed_costs, get_engine
        from sqlalchemy import func, select

        today = date.today()
        start = (today - timedelta(days=lookback_days)).isoformat()
        engine = get_engine()

        with engine.connect() as conn:
            rows = conn.execute(
                select(
                    attributed_costs.c.team,
                    func.sum(attributed_costs.c.amount_usd).label("t"),
                )
                .where(attributed_costs.c.snapshot_date >= start)
                .group_by(attributed_costs.c.team)
                .order_by(func.sum(attributed_costs.c.amount_usd).desc())
                .limit(8)
            ).fetchall()

        if not rows:
            return [], ""

        total = sum(float(r.t) for r in rows)
        lines = "\n".join(
            f"  {i+1}. *{r.team}*: ${float(r.t):,.0f} ({float(r.t)/total*100:.1f}%)"
            for i, r in enumerate(rows)
        )
        blocks = [{"type": "section", "text": {"type": "mrkdwn", "text": f"👥 *By team* ({lookback_days}d)\n{lines}"}}]
        return blocks, f"Top team: {rows[0].team} (${float(rows[0].t):,.0f})"
    except Exception as e:
        log.warning("teams section failed: %s", e)
        return [], ""


# Section registry
_SECTION_HANDLERS = {
    "spend":        _section_spend,
    "anomalies":    _section_anomalies,
    "scorecard":    _section_scorecard,
    "k8s":          _section_k8s,
    "commitments":  _section_commitments,
    "rightsizing":  _section_rightsizing,
    "budgets":      _section_budgets,
    "teams":        _section_teams,
}

VALID_SECTIONS = list(_SECTION_HANDLERS.keys())


# ── Report builder ────────────────────────────────────────────────────────────

async def build_report(
    sections: list[str],
    filters: dict,
    lookback_days: int,
    report_name: str = "FinOps Report",
) -> tuple[list[dict], str]:
    """
    Build a full report from the requested sections.
    Returns (slack_blocks, plain_text_summary).
    """
    today = date.today()
    header_blocks: list[dict] = [
        {"type": "header", "text": {"type": "plain_text", "text": f"📊 {report_name} — {today.strftime('%B %d, %Y')}"}},
    ]
    if filters:
        scope_parts = [f"{k}={v}" for k, v in filters.items() if v]
        if scope_parts:
            header_blocks.append({
                "type": "context",
                "elements": [{"type": "mrkdwn", "text": f"Scope: {', '.join(scope_parts)} · Last {lookback_days} days"}],
            })

    all_blocks: list[dict] = list(header_blocks)
    summary_parts: list[str] = []

    for section in sections:
        handler = _SECTION_HANDLERS.get(section)
        if not handler:
            continue
        try:
            blocks, text = await handler(filters=filters, lookback_days=lookback_days)
            if blocks:
                all_blocks.extend(blocks)
                all_blocks.append({"type": "divider"})
            if text:
                summary_parts.append(text)
        except Exception as e:
            log.warning("Section %s failed: %s", section, e)

    footer = [{"type": "context", "elements": [
        {"type": "mrkdwn", "text": "_Generated by <https://github.com/nable-finops/nable|nable FinOps> — ask Claude anything about these numbers_"},
    ]}]
    all_blocks.extend(footer)

    plain_text = f"{report_name} ({today.isoformat()}): " + " · ".join(summary_parts)
    return all_blocks, plain_text


# ── Delivery ──────────────────────────────────────────────────────────────────

async def deliver_report(
    sub: dict[str, Any],
    blocks: list[dict],
    plain_text: str,
) -> dict[str, Any]:
    """Send a report to all configured delivery channels for this subscription."""
    from . import slack as slack_mod
    from .email_digest import send_custom_digest
    from ..config import is_airgap

    results: dict[str, Any] = {"slack": [], "email": [], "teams": []}

    # Air-gap mode promises provider-only traffic. Report delivery posts cost data to
    # Slack/Teams/email (non-provider endpoints), so fail closed: skip delivery and
    # say so, rather than silently egressing cost data from an air-gapped environment.
    if is_airgap():
        results["skipped"] = "FINOPS_AIRGAP is set; external report delivery is disabled."
        return results

    # Slack
    slack_channels = json.loads(sub.get("slack_channels") or "[]")
    bot_token = slack_mod._bot_token()
    webhook_url = slack_mod._webhook_url()

    for channel in slack_channels:
        try:
            if bot_token:
                import httpx
                async with httpx.AsyncClient(timeout=15) as client:
                    r = await client.post(
                        "https://slack.com/api/chat.postMessage",
                        headers={"Authorization": f"Bearer {bot_token}"},
                        json={"channel": channel, "text": plain_text, "blocks": blocks},
                    )
                    ok = r.json().get("ok", False)
                    results["slack"].append({"channel": channel, "ok": ok})
            elif webhook_url:
                ok = await slack_mod.send_webhook(blocks, plain_text)
                results["slack"].append({"channel": "webhook", "ok": ok})
        except Exception as e:
            log.warning("Slack delivery to %s failed: %s", channel, e)
            results["slack"].append({"channel": channel, "ok": False, "error": str(e)})

    # Email — Pro only (scheduled_email_digests)
    email_addresses = json.loads(sub.get("email_addresses") or "[]")
    if email_addresses:
        try:
            from ..license import require_pro
            gate = require_pro("scheduled_email_digests")
        except Exception:
            gate = None
        if gate is not None:
            results["email"] = [{
                "skipped": True,
                "reason": "Email delivery requires Pro (scheduled_email_digests). Slack delivery is free.",
                "upgrade_url": gate.get("upgrade_url", ""),
            }]
        else:
            for addr in email_addresses:
                try:
                    ok = send_custom_digest(
                        recipient=addr,
                        subject=plain_text[:80],
                        body_text=plain_text,
                        report_name=sub.get("name", "FinOps Report"),
                    )
                    results["email"].append({"to": addr, "ok": ok})
                except Exception as e:
                    log.warning("Email delivery to %s failed: %s", addr, e)
                    results["email"].append({"to": addr, "ok": False, "error": str(e)})

    # Teams
    teams_webhook = sub.get("teams_webhook", "")
    if teams_webhook:
        try:
            from . import teams as teams_mod
            ok = await teams_mod.send_to_webhook(teams_webhook, plain_text)
            results["teams"].append({"ok": ok})
        except Exception as e:
            log.warning("Teams delivery failed: %s", e)

    return results


# ── CRUD helpers ──────────────────────────────────────────────────────────────

def create_subscription(
    name: str,
    sections: list[str],
    frequency: str,
    slack_channels: list[str] | None = None,
    email_addresses: list[str] | None = None,
    teams_webhook: str = "",
    filters: dict | None = None,
    lookback_days: int = 7,
    timezone: str = "UTC",
    cron: str | None = None,
) -> dict[str, Any]:
    """Create a new report subscription. Returns the created record."""
    from ..storage.db import report_subscriptions, get_engine
    from sqlalchemy import insert

    resolved_cron = cron or PRESET_CRONS.get(frequency, PRESET_CRONS["weekly"])
    now = datetime.now(timezone if isinstance(timezone, type(datetime.now().tzinfo)) else tz_utc())

    engine = get_engine()
    with engine.begin() as conn:
        result = conn.execute(insert(report_subscriptions).values(
            name=name,
            slack_channels=json.dumps(slack_channels or []),
            email_addresses=json.dumps(email_addresses or []),
            teams_webhook=teams_webhook,
            cron=resolved_cron,
            timezone=timezone,
            sections=json.dumps([s for s in sections if s in VALID_SECTIONS]),
            filters=json.dumps(filters or {}),
            lookback_days=lookback_days,
            created_at=now,
            # Baseline to creation time, NOT None. Otherwise the scheduler's
            # due-check treats the subscription as "never sent" and blasts a full
            # report within ~5 minutes of creation instead of on its schedule.
            last_sent_at=now,
            is_active=True,
            created_by="mcp",
        ))
        sub_id = result.inserted_primary_key[0]

    return {"id": sub_id, "name": name, "cron": resolved_cron, "sections": sections}


def list_subscriptions() -> list[dict[str, Any]]:
    from ..storage.db import report_subscriptions, get_engine
    from sqlalchemy import select
    engine = get_engine()
    with engine.connect() as conn:
        rows = conn.execute(select(report_subscriptions).where(
            report_subscriptions.c.is_active == True
        ).order_by(report_subscriptions.c.created_at.desc())).fetchall()
    out = []
    for r in rows:
        d = dict(r._mapping)
        d["sections"] = json.loads(d.get("sections") or "[]")
        d["slack_channels"] = json.loads(d.get("slack_channels") or "[]")
        d["email_addresses"] = json.loads(d.get("email_addresses") or "[]")
        d["filters"] = json.loads(d.get("filters") or "{}")
        out.append(d)
    return out


def cancel_subscription(sub_id: int) -> bool:
    from ..storage.db import report_subscriptions, get_engine
    from sqlalchemy import update
    engine = get_engine()
    with engine.begin() as conn:
        result = conn.execute(
            update(report_subscriptions)
            .where(report_subscriptions.c.id == sub_id)
            .values(is_active=False)
        )
    return result.rowcount > 0


def tz_utc():
    return timezone.utc


def _is_report_due(cron: str, last_sent_at: Any, created_at: Any = None) -> bool:
    """Return True if a subscription with this cron is due to run now.

    Due means the most recent scheduled fire time at or before now is strictly
    later than the baseline. The baseline is the last time the report was sent;
    if it was never sent, the baseline is when the subscription was created, so a
    brand-new subscription does NOT blast a report on the next 5-minute tick, it
    waits for its first scheduled time after creation. If neither timestamp is
    available we return False (wait) rather than risk an unscheduled blast.

    Requires croniter (the `croniter` extra). Without it we cannot evaluate the
    schedule, so we return False rather than risk spamming.
    """
    now = datetime.now(timezone.utc)
    try:
        from croniter import croniter
    except ImportError:
        log.warning("croniter not installed; scheduled reports are paused. "
                    "Install with: pip install finops-mcp[croniter]")
        return False
    try:
        prev_fire = croniter(cron, now).get_prev(datetime)
    except Exception as exc:  # malformed cron expression
        log.error("Invalid cron '%s' on a report subscription: %s", cron, exc)
        return False
    if prev_fire.tzinfo is None:
        prev_fire = prev_fire.replace(tzinfo=timezone.utc)

    baseline = last_sent_at if last_sent_at is not None else created_at
    if baseline is None:
        return False  # no baseline: do not blast, wait for a real send/created time
    if isinstance(baseline, str):
        try:
            baseline = datetime.fromisoformat(baseline)
        except ValueError:
            return False
    if baseline.tzinfo is None:
        baseline = baseline.replace(tzinfo=timezone.utc)
    return prev_fire > baseline


async def run_subscription(sub_id: int) -> dict[str, Any]:
    """
    Run a single report subscription immediately (used by scheduler + MCP on-demand).
    """
    from ..storage.db import report_subscriptions, get_engine
    from sqlalchemy import select, update

    engine = get_engine()
    with engine.connect() as conn:
        row = conn.execute(
            select(report_subscriptions).where(report_subscriptions.c.id == sub_id)
        ).fetchone()

    if not row:
        return {"error": f"Subscription {sub_id} not found"}

    sub = dict(row._mapping)
    sections = json.loads(sub.get("sections") or "[]")
    filters  = json.loads(sub.get("filters") or "{}")
    lookback = sub.get("lookback_days", 7)
    name     = sub.get("name", "FinOps Report")

    blocks, plain_text = await build_report(sections, filters, lookback, name)
    delivery = await deliver_report(sub, blocks, plain_text)

    # Update last_sent_at
    with engine.begin() as conn:
        conn.execute(
            update(report_subscriptions)
            .where(report_subscriptions.c.id == sub_id)
            .values(last_sent_at=datetime.now(timezone.utc))
        )

    return {
        "subscription_id": sub_id,
        "name": name,
        "sections_sent": sections,
        "delivery": delivery,
    }
