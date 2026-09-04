"""The work: take a snapshot, detect anomalies, send a digest. On demand.

This module is the WHAT. It no longer contains the WHEN.

Every function here runs because something asked it to, right then: an MCP tool
("take a cost snapshot now", "send the digest now"), a CLI command, or the
hosted cron. The cron itself, the nine registrations and the cross-host lock
that stops two boxes double-sending, moved to nable-enterprise on 2026-08-15,
because running unattended forever is the hosted product and answering a
question is the open one.

What that buys the open package, beyond tidiness: nothing here ever sets the
unattended mark, so an Apache-2.0 install has no path that can reach a billed
Cost Explorer request with nobody watching. Not "is discouraged from" — has no
path. finops.billing_access still holds the policy and the single permitted
client construction, open, for anyone who wants to audit that claim.

The pairing to keep in mind when editing: _snapshot_all / _detect_and_alert /
_send_daily_digest do the work, run_*_now are the on-demand entry points MCP
tools call, and finops.scheduler.cron (closed) is what calls them on a timer.
"""
from __future__ import annotations

import asyncio
import logging
import os
from datetime import date, timedelta

log = logging.getLogger("finops.scheduler")


def should_alert(direction: str, severity: str) -> bool:
    """Whether an anomaly earns a Slack/Teams/n8n push.

    Spikes always alert: a spike is the event the alert loop exists for. Drops are
    usually good news (a fix landed, a resource was cleaned up) and pushing every
    one trains the team to mute the integration, which kills the paid loop. A
    HIGH-severity drop still alerts by default because a large sudden drop can mean
    something stopped running that shouldn't have (backups, a data pipeline).

    FINOPS_ALERT_DROPS tunes it: "high" (default), "all", or "never". Every anomaly
    is still recorded and queryable via get_anomalies regardless; this gates only
    the push.
    """
    if direction != "drop":
        return True
    policy = os.getenv("FINOPS_ALERT_DROPS", "high").strip().lower()
    if policy == "all":
        return True
    if policy == "never":
        return False
    return severity == "high"




# ── Core job functions ────────────────────────────────────────────────────────

async def _snapshot_all() -> dict:
    """Fetch today's costs from all configured providers and persist snapshots."""
    # Day-one anomalies: when history is thinner than the detector's minimum,
    # backfill baselines from Cost Explorer first, so the anomaly job that runs
    # right after this has something to compare against on a fresh install.
    # Cost Explorer is reached through billing_access, which refuses in an
    # unattended context. Nothing in the open package sets that mark, so this
    # runs for a person who asked and refuses on the hosted cron, where the
    # billing export covers the same history for free.
    try:
        from ..anomaly.backfill import backfill_from_cost_explorer
        backfill_from_cost_explorer()
    except Exception:
        pass
    from ..connectors.aws import AWSConnector
    from ..connectors.azure import AzureConnector
    from ..connectors.gcp import GCPConnector
    from ..connectors.saas.datadog import DatadogConnector
    from ..connectors.saas.mongodb_atlas import MongoDBAtlasConnector
    from ..connectors.saas.twilio import TwilioConnector
    from ..storage.snapshots import store_snapshot

    today = date.today()
    yesterday = today - timedelta(days=1)

    connectors = {
        "aws": AWSConnector(),
        "azure": AzureConnector(),
        "gcp": GCPConnector(),
        "datadog": DatadogConnector(),
        "mongodb_atlas": MongoDBAtlasConnector(),
        "twilio": TwilioConnector(),
    }

    results: dict[str, str] = {}

    # AWS comes from the billing export when a reader for it is installed. The
    # export is the customer's own file in their own bucket, so reading it costs
    # a few S3 GETs; the connector path below reaches Cost Explorer, which bills
    # them $0.01 per request, on a timer. Same numbers, different meter.
    #
    # The S3-direct reader ships in nable-enterprise and arrives in this
    # namespace through that package's seam, so this import is expected to fail
    # on an open install. Skipping the connector on success is the whole saving:
    # running both would read the bill twice and pay for one of them anyway.
    cur_ok = False
    cur_configured = False
    try:
        from ..connectors import cur_s3  # type: ignore[attr-defined]
    except ImportError:
        cur_s3 = None
    if cur_s3 is not None:
        try:
            if cur_s3.is_configured():
                # AWS is the billing export's job now, so it must never fall to
                # the Cost Explorer connector below. Mark that BEFORE the read:
                # the flag holds whether the read wrote fresh rows, found the
                # export unchanged since last time (data already ingested), or
                # failed outright. Gating the skip on rows-written instead let an
                # unchanged export drop through to Cost Explorer, and the tick
                # went red on a billed call it must never make.
                cur_configured = True
                out = cur_s3.ingest_recent(days=3)
                cur_ok = out["rows_written"] > 0 or out["files_read"] > 0
                if cur_ok:
                    results["aws"] = (
                        f"ok: {out['rows_written']} rows from the billing export, "
                        f"${out['cost']['usd']:.6f}")
                    log.info("Snapshot: aws via CUR, %d rows over %d day(s), $%.6f",
                             out["rows_written"], out["days_written"],
                             out["cost"]["usd"])
                else:
                    # Export unchanged since the last read: the data is already
                    # ingested and current, so there is nothing to write and no
                    # reason to reach Cost Explorer.
                    results["aws"] = "ok: billing export unchanged, data already current"
                    log.info("Snapshot: aws via CUR, export unchanged since last read")
        except Exception as exc:
            # Never fatal, and deliberately NOT a reason to fall back to the
            # billed path: a reader that is installed but failing is a broken
            # deployment, and quietly charging the customer to paper over it is
            # exactly the behaviour this whole change removed. cur_configured
            # stays True, so AWS is skipped below rather than billed.
            log.warning("CUR ingest failed; AWS history will be short this run: %s", exc)

    from ..billing_access import BillingAccessError
    for name, connector in connectors.items():
        if name == "aws" and cur_configured:
            continue
        if not await connector.is_configured():
            continue
        try:
            # No special case for AWS any more. Called on demand this reaches
            # Cost Explorer, which is the right price for an answer somebody is
            # waiting for; called from the hosted cron the unattended mark makes
            # billing_access refuse, and the billing export above has already
            # covered the same day for free.
            summary = await connector.get_costs(yesterday, today, granularity="DAILY")
            for entry in summary.entries:
                if entry.amount > 0:
                    store_snapshot(
                        provider=entry.provider,
                        service=entry.service,
                        account_id=entry.account_id,
                        region=entry.region,
                        snapshot_date=yesterday,
                        amount_usd=entry.amount,
                        granularity="DAILY",
                    )
            results[name] = f"ok — {len(summary.entries)} entries"
            log.info("Snapshot: %s — %d entries, $%.2f", name, len(summary.entries), summary.total_usd)
        except BillingAccessError as exc:
            # A hosted/unattended box with a read-only role but no billing export
            # cannot reach Cost Explorer: billing_access refuses BEFORE any billed
            # call. That is an expected "no data source yet" state, not a failure,
            # so log a WARNING (not log.exception/ERROR) or the hosted cron marks
            # the nightly job red every night, and record an honest message telling
            # the operator to connect a Cost and Usage Report.
            results[name] = ("no billing export connected: enable an AWS Cost and "
                             "Usage Report so nable can read this account without "
                             "billed API calls")
            log.warning("Snapshot: %s has no CUR and Cost Explorer is not permitted "
                        "here; skipping until a billing export is connected (%s)",
                        name, exc)
        except Exception as exc:
            results[name] = f"error: {exc}"
            log.exception("Snapshot failed for %s", name)

    return results


async def _detect_and_alert() -> list[dict]:
    """Run anomaly detection on yesterday's snapshot and send alerts for new ones."""
    from ..anomaly.seasonality import detect_with_seasonality
    from ..anomaly.detector import (
        AnomalyResult, get_active_anomalies,
        mark_notified, persist_anomaly,
    )
    from ..integrations.ticketing import create_ticket
    from ..notifications import slack, teams
    from ..storage.db import cost_snapshots, get_engine
    from sqlalchemy import select, and_

    yesterday = (date.today() - timedelta(days=1)).isoformat()
    engine = get_engine()

    with engine.connect() as conn:
        rows = conn.execute(
            select(cost_snapshots)
            .where(cost_snapshots.c.snapshot_date == yesterday)
        ).fetchall()

    alerted: list[dict] = []
    for row in rows:
        r = dict(row._mapping)
        anomaly = detect_with_seasonality(
            provider=r["provider"],
            service=r["service"],
            account_id=r["account_id"],
            snapshot_date=date.fromisoformat(r["snapshot_date"]),
            current_amount=r["amount_usd"],
        )
        if anomaly is None:
            continue
        anomaly_id, is_new = persist_anomaly(anomaly)
        if not is_new:
            # Already detected and alerted for this spend event (cron retry, the
            # run_anomaly_check_now tool, or a second process). Do not re-alert or
            # re-create the ticket, that is what makes a team mute the integration.
            continue
        if not should_alert(anomaly.direction, anomaly.severity):
            # Recorded and queryable via get_anomalies, but not pushed. Routine
            # cost drops as pages are how alert channels get muted.
            continue
        anomaly_dict = {
            "id": anomaly_id,
            "provider": anomaly.provider,
            "service": anomaly.service,
            "account_id": anomaly.account_id,
            "severity": anomaly.severity,
            "direction": anomaly.direction,
            "pct_change": anomaly.pct_change,
            "z_score": anomaly.z_score,
            "baseline_mean": anomaly.baseline_mean,
            "current_amount": anomaly.current_amount,
            "detected_at": str(date.today()),
        }
        # Attach the dollar impact and a concrete next question. Pure arithmetic
        # over fields we already have, no provider call and no LLM, so it is safe
        # to run for every alerted anomaly. An alert with a percentage and no
        # dollars leaves the reader to work out whether +180% is noise or a budget
        # event.
        from ..anomaly.impact import enrich as _enrich_impact
        anomaly_dict = _enrich_impact(anomaly_dict)
        # Send alerts (fire-and-forget, don't crash on failure)
        notified = False
        if slack.is_configured():
            try:
                ok = await slack.send_anomaly_alert(anomaly_dict)
                notified = notified or ok
            except Exception:
                log.exception("Slack alert failed for anomaly %d", anomaly_id)
        if teams.is_configured():
            try:
                ok = await teams.send_anomaly_alert(anomaly_dict)
                notified = notified or ok
            except Exception:
                log.exception("Teams alert failed for anomaly %d", anomaly_id)
        try:
            from ..connectors.saas.n8n import N8nConnector
            _n8n = N8nConnector()
            if await _n8n.is_configured():
                await _n8n.send_anomaly(anomaly_dict)
        except Exception:
            log.exception("n8n alert failed for anomaly %d", anomaly_id)
        if notified:
            mark_notified(anomaly_id)

        # Auto-create ticket for high/medium severity
        if anomaly.severity in ("high", "medium"):
            try:
                ticket_url = create_ticket(anomaly_dict)
                if ticket_url:
                    anomaly_dict["ticket_url"] = ticket_url
                    log.info("Ticket created: %s", ticket_url)
            except Exception:
                log.exception("Ticket creation failed for anomaly %d", anomaly_id)

        alerted.append(anomaly_dict)
        log.info("Anomaly: %s", anomaly.summary())

    return alerted


async def _send_daily_digest() -> bool:
    from ..anomaly.detector import get_active_anomalies
    from ..notifications import slack, teams
    from ..storage.db import cost_snapshots, get_engine
    from sqlalchemy import func, select

    if not slack.is_configured() and not teams.is_configured():
        return False

    today = date.today()
    yesterday = today - timedelta(days=1)
    two_days_ago = today - timedelta(days=2)

    engine = get_engine()
    with engine.connect() as conn:
        def day_total(d: date) -> float:
            row = conn.execute(
                select(func.sum(cost_snapshots.c.amount_usd))
                .where(cost_snapshots.c.snapshot_date == d.isoformat())
            ).scalar()
            return float(row or 0)

        grand_total = day_total(yesterday)
        prev_total = day_total(two_days_ago)

        # by provider, for BOTH days.
        #
        # The delta used to be a bare subtraction of two whole-day sums with
        # nothing comparing which providers each day covered. _snapshot_all
        # records `results[name] = f"error: {exc}"` for a provider whose fetch
        # failed and job_snapshot discards that dict, so a failed AWS snapshot
        # simply left no rows for yesterday. The digest then compared a
        # Datadog-only yesterday against an AWS+Datadog day before and posted:
        #
        #     vs yesterday: -94.1% (-$90,000.00)
        #
        # Nothing dropped. The only provider present in both windows was flat.
        # Comparing the provider SETS rather than trusting a status flag catches
        # this whatever the cause: a failed snapshot, a disconnected provider,
        # or the first day of a newly connected one, and it works on data
        # already written.
        def by_provider_on(d: date) -> dict[str, float]:
            rows = conn.execute(
                select(
                    cost_snapshots.c.provider,
                    func.sum(cost_snapshots.c.amount_usd).label("total"),
                )
                .where(cost_snapshots.c.snapshot_date == d.isoformat())
                .group_by(cost_snapshots.c.provider)
            ).fetchall()
            return {r.provider: float(r.total) for r in rows}

        by_provider = by_provider_on(yesterday)
        by_provider_prev = by_provider_on(two_days_ago)

        only_before = sorted(set(by_provider_prev) - set(by_provider))
        only_now = sorted(set(by_provider) - set(by_provider_prev))
        coverage_note = ""
        if only_before or only_now:
            # Compare the providers BOTH days actually cover, so the percentage
            # describes spend rather than coverage. The headline total stays the
            # real yesterday figure; only the basis of the delta narrows.
            common = set(by_provider) & set(by_provider_prev)
            prev_total = sum(v for k, v in by_provider_prev.items() if k in common)
            grand_total_for_delta = sum(v for k, v in by_provider.items() if k in common)
            missing = ", ".join(p.upper() for p in only_before)
            added = ", ".join(p.upper() for p in only_now)
            bits = []
            if missing:
                bits.append(f"{missing} has no snapshot for {yesterday.isoformat()}")
            if added:
                bits.append(f"{added} is new since {two_days_ago.isoformat()}")
            coverage_note = (
                "⚠️ Incomplete comparison: " + "; ".join(bits) + ". "
                "The change below covers only "
                + (", ".join(p.upper() for p in sorted(common)) or "no provider")
                + ", so it is not the whole bill moving."
            )
            # Feed the renderer the like-for-like pair. Without this the
            # percentage is a coverage artefact wearing a spend label.
            grand_total_delta_basis = grand_total_for_delta
        else:
            grand_total_delta_basis = grand_total

        # top services
        svc_rows = conn.execute(
            select(
                cost_snapshots.c.service,
                func.sum(cost_snapshots.c.amount_usd).label("total"),
            )
            .where(cost_snapshots.c.snapshot_date == yesterday.isoformat())
            .group_by(cost_snapshots.c.service)
            .order_by(func.sum(cost_snapshots.c.amount_usd).desc())
            .limit(5)
        ).fetchall()
        top_services = [
            {
                "service": r.service,
                "amount_usd": float(r.total),
                "pct": float(r.total) / grand_total * 100 if grand_total else 0,
            }
            for r in svc_rows
        ]

    active = get_active_anomalies()

    sent = False
    if slack.is_configured():
        try:
            sent = await slack.send_daily_digest(
                yesterday, grand_total, prev_total, by_provider, top_services,
                len(active), delta_basis=grand_total_delta_basis,
                coverage_note=coverage_note)
        except Exception:
            log.exception("Slack daily digest failed")
    if teams.is_configured():
        try:
            sent = await teams.send_daily_digest(
                yesterday, grand_total, prev_total, by_provider, top_services,
                len(active), delta_basis=grand_total_delta_basis,
                coverage_note=coverage_note)
        except Exception:
            log.exception("Teams daily digest failed")

    return sent


# ── Sync entry points (MCP tools and the hosted cron both call these) ────────





def _run(coro):
    """Run a coroutine to completion and return its result (or None on error).
    Returning the result lets callers like job_credit_check act on it; the other
    job_* callers ignore the return value, so this is backward-compatible."""
    try:
        return asyncio.run(coro)
    except Exception:
        log.exception("Scheduled job failed")
        return None












def job_weekly_email_digest() -> None:
    """Send the standalone weekly email digest (no AI client required)."""
    try:
        from ..notifications.email_digest import send_weekly_digest
        from ..anomaly.detector import get_active_anomalies
        from ..storage.db import cost_snapshots, get_engine
        from ..recommendations.rightsizing import analyze_rightsizing, rightsizing_summary
        from sqlalchemy import func, select
        from datetime import date, timedelta

        today = date.today()
        week_start = (today - timedelta(days=7)).isoformat()
        prev_week_start = (today - timedelta(days=14)).isoformat()
        prev_week_end = (today - timedelta(days=7)).isoformat()

        engine = get_engine()
        with engine.connect() as conn:
            def week_total(start: str, end: str) -> float:
                row = conn.execute(
                    select(func.sum(cost_snapshots.c.amount_usd))
                    .where(cost_snapshots.c.snapshot_date >= start)
                    .where(cost_snapshots.c.snapshot_date < end)
                ).scalar()
                return float(row or 0)

            current_week_total = week_total(week_start, today.isoformat())
            prev_week_total = week_total(prev_week_start, prev_week_end)

            rows = conn.execute(
                select(
                    cost_snapshots.c.provider,
                    func.sum(cost_snapshots.c.amount_usd).label("total"),
                )
                .where(cost_snapshots.c.snapshot_date >= week_start)
                .group_by(cost_snapshots.c.provider)
                .order_by(func.sum(cost_snapshots.c.amount_usd).desc())
            ).fetchall()

            top_providers = [
                {
                    "provider": r.provider,
                    "amount": float(r.total),
                    "pct": float(r.total) / current_week_total * 100 if current_week_total else 0,
                }
                for r in rows
            ]

        anomalies = get_active_anomalies(limit=10)

        try:
            rs = analyze_rightsizing()
            recs = rightsizing_summary(rs)["recommendations"][:5]
            rec_list = [
                {
                    "title": r["title"],
                    "description": r["description"],
                    "monthly_savings": r["monthly_savings"],
                }
                for r in recs
            ]
        except Exception:
            rec_list = []

        send_weekly_digest(
            total_spend=current_week_total,
            prev_total=prev_week_total,
            top_providers=top_providers,
            anomalies=anomalies,
            recommendations=rec_list,
        )
    except Exception:
        log.exception("Weekly email digest job failed")


async def _check_credits_and_alert() -> dict | None:
    """
    Watch the AWS credit-to-cash flip and alert once when it happens. The credit
    cliff is the #1 real trigger for an early startup to care about cost; native
    AWS tooling sends no notification when credits deplete and billing flips to
    cash. Dedup is keyed on (month, status) so a flip alerts once, not daily.
    """
    from ..connectors.credit_tracking import get_credit_status
    from ..notifications import slack

    try:
        status = await asyncio.to_thread(get_credit_status, 6)
    except Exception:
        log.exception("Credit status check failed")
        return None

    if status.get("status") not in ("critical", "warning"):
        return None

    monthly = status.get("monthly") or []
    latest_month = monthly[-1]["month"] if monthly else str(date.today())
    dedup_key = f"{latest_month}:{status['status']}"
    if _credit_alert_already_sent(dedup_key):
        return None

    headline = status.get("headline", "AWS credit status changed.")
    net = status.get("latest_net_cash_usd", 0.0)
    icon = "🚨" if status["status"] == "critical" else "⚠️"
    text = f"{icon} AWS credits: {headline}"
    blocks = [
        {"type": "header",
         "text": {"type": "plain_text", "text": f"{icon} AWS credit alert"}},
        {"type": "section",
         "text": {"type": "mrkdwn", "text": f"*{headline}*"}},
        {"type": "section", "fields": [
            {"type": "mrkdwn", "text": f"*Latest net cash*\n${net:,.0f}/mo"},
            {"type": "mrkdwn",
             "text": f"*Credit coverage*\n{status.get('latest_credit_coverage_pct', 0):.0f}%"},
        ]},
    ]

    sent = False
    if slack.is_configured():
        try:
            sent = await slack.send(blocks, text) or sent
        except Exception:
            log.exception("Slack credit alert failed")

    if sent:
        _mark_credit_alert_sent(dedup_key)
    return status


def _credit_alert_state_path():
    import os
    from pathlib import Path
    base = Path(os.environ.get("FINOPS_HOME", str(Path.home() / ".finops-mcp")))
    return base / "credit_alert_state.json"


def _credit_alert_already_sent(key: str) -> bool:
    import json
    p = _credit_alert_state_path()
    try:
        if p.exists():
            loaded = json.loads(p.read_text())
            sent = loaded.get("sent", []) if isinstance(loaded, dict) else []
            return key in set(sent)
    except Exception:
        pass
    return False


def _mark_credit_alert_sent(key: str) -> None:
    import json
    p = _credit_alert_state_path()
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        data = {"sent": []}
        if p.exists():
            loaded = json.loads(p.read_text())
            # If the file is corrupt (non-dict JSON from a partial write), drop it and
            # rewrite a valid one, so dedup self-heals instead of failing open forever
            # and re-sending the same alert on every run.
            if isinstance(loaded, dict):
                data = loaded
        sent = set(data.get("sent", []))
        sent.add(key)
        # keep the list bounded
        data["sent"] = sorted(sent)[-50:]
        p.write_text(json.dumps(data))
    except Exception:
        log.debug("Could not persist credit alert state")




# ── AI / token spend monitor ──────────────────────────────────────────────────

def _alert_state_path(name: str):
    import os
    from pathlib import Path
    base = Path(os.environ.get("FINOPS_HOME", str(Path.home() / ".finops-mcp")))
    return base / f"{name}_alert_state.json"


def _alert_already_sent(name: str, key: str) -> bool:
    import json
    p = _alert_state_path(name)
    try:
        if p.exists():
            loaded = json.loads(p.read_text())
            sent = loaded.get("sent", []) if isinstance(loaded, dict) else []
            return key in set(sent)
    except Exception:
        pass
    return False


def _mark_alert_sent(name: str, key: str) -> None:
    import json
    p = _alert_state_path(name)
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        data = {"sent": []}
        if p.exists():
            loaded = json.loads(p.read_text())
            # Self-heal a corrupt (non-dict) state file instead of failing open
            # and re-alerting forever.
            if isinstance(loaded, dict):
                data = loaded
        sent = set(data.get("sent", []))
        sent.add(key)
        data["sent"] = sorted(sent)[-50:]
        p.write_text(json.dumps(data))
    except Exception:
        log.debug("Could not persist %s alert state", name)


async def _check_ai_spend_and_alert() -> dict | None:
    """Watch the token layer: alert on a token-spend spike and on commitment
    contracts that need attention (capacity under-utilized, enterprise minimum
    shortfall, commitment expiring). The credits-to-cash flip is handled by
    job_credit_check, so this passes credit_analysis=None and skips credits
    contracts to avoid double-alerting and an extra AWS call. Dedup keyed on
    (month, conditions) so each condition alerts once per month."""
    from ..connectors.llm_costs import get_all_llm_costs
    from ..analytics.llm_commitments import load_contracts, analyze_portfolio, total_tokens
    from ..anomaly.detector import detect_for_series
    from ..notifications import slack

    try:
        data = await asyncio.to_thread(get_all_llm_costs, None, None, 30)
    except Exception:
        log.exception("AI spend monitor: cost fetch failed")
        return None

    daily = data.get("daily") or []
    series = [float(d.get("total_usd", 0.0)) for d in daily if isinstance(d, dict)]
    findings: list[str] = []
    kinds: list[str] = []

    # 1) Token-spend spike. Only alert on spikes (over-run): the latest day can be
    # partial and under-report, which would look like a drop, never a false spike.
    if len(series) >= 2:
        res = detect_for_series("ai", "LLM tokens", "llm", date.today(), series[-1], series[:-1])
        if res and res.direction == "spike":
            findings.append(f"Token spend spike: {res.summary()}")
            kinds.append("spike")

    # 2) Commitment contracts needing attention (capacity / rate_card).
    contracts = [c for c in load_contracts() if (c.get("type") or "").lower() != "credits"]
    if contracts:
        usage = {"tokens": total_tokens(data.get("by_model_tokens")),
                 "spend_usd": float(data.get("total_usd", 0.0)), "days": 30,
                 "credit_analysis": None}
        port = analyze_portfolio(contracts, usage)
        for item in port.get("needs_attention", []):
            findings.append(item["headline"])
            kinds.append(f"contract:{item['label']}:{item['status']}")

    if not findings:
        return None

    dedup_key = f"{str(date.today())[:7]}:" + "|".join(sorted(kinds))
    if _alert_already_sent("ai", dedup_key):
        return None

    text = "nable AI spend alert"
    blocks = [
        {"type": "header", "text": {"type": "plain_text", "text": "🤖 AI spend alert"}},
        *[{"type": "section", "text": {"type": "mrkdwn", "text": f}} for f in findings],
    ]
    sent = False
    if slack.is_configured():
        try:
            sent = await slack.send(blocks, text) or sent
        except Exception:
            log.exception("Slack AI spend alert failed")
    if sent:
        _mark_alert_sent("ai", dedup_key)
    return {"findings": findings, "sent": sent}






# ── Scheduler lifecycle ───────────────────────────────────────────────────────









# ── Manual triggers (used by MCP tools) ──────────────────────────────────────

async def run_snapshot_now() -> dict:
    return await _snapshot_all()


async def run_anomaly_check_now() -> list[dict]:
    return await _detect_and_alert()


async def run_digest_now() -> bool:
    return await _send_daily_digest()


async def run_weekly_insight_now() -> bool:
    """Trigger the weekly Slack insight immediately (used by push_weekly_insight tool)."""
    from ..notifications import slack
    if not slack.is_configured():
        return False
    from datetime import date, timedelta
    from ..storage.db import get_engine, cost_snapshots
    from sqlalchemy import select, func

    today = date.today()
    this_start = today - timedelta(days=7)
    last_start = today - timedelta(days=14)
    last_end = today - timedelta(days=8)

    def _week(start: date, end: date) -> tuple[float, dict]:
        engine = get_engine()
        with engine.connect() as conn:
            rows = conn.execute(
                select(
                    cost_snapshots.c.provider,
                    cost_snapshots.c.service,
                    func.sum(cost_snapshots.c.amount_usd).label("t"),
                )
                .where(
                    cost_snapshots.c.snapshot_date >= start.isoformat(),
                    cost_snapshots.c.snapshot_date <= end.isoformat(),
                )
                .group_by(cost_snapshots.c.provider, cost_snapshots.c.service)
            ).fetchall()
        by_key = {}
        total = 0.0
        for r in rows:
            by_key[f"{r.provider}::{r.service}"] = {"provider": r.provider, "service": r.service, "total": r.t or 0}
            total += r.t or 0
        return total, by_key

    try:
        grand_total, this_week = _week(this_start, today)
        prev_total, last_week = _week(last_start, last_end)
    except Exception as exc:
        # Do NOT fall through with zeros. This used to swallow the failure and
        # carry on, so a snapshot query that never ran was posted to the team's
        # Slack as "Weekly cost: $0 (+0.0% vs last week)" and the tool reported
        # sent: True. Everyone reading that channel now believes the bill went to
        # nothing. A number nable never read must not be published as one it did.
        log.error("weekly insight: snapshot query failed, refusing to post: %s", exc)
        return False

    movers = []
    for key in set(this_week) | set(last_week):
        tw = this_week.get(key, {}).get("total", 0.0)
        lw = last_week.get(key, {}).get("total", 0.0)
        if tw < 5 and lw < 5:
            continue
        rec = (this_week.get(key) or last_week.get(key) or {})
        pct = ((tw - lw) / lw * 100) if lw else 100.0
        movers.append({"provider": rec.get("provider", ""), "service": rec.get("service", ""),
                       "this_week": tw, "last_week": lw, "pct_change": pct})
    movers.sort(key=lambda m: -abs(m["pct_change"]))

    try:
        from ..recommendations.savings_tracker import get_summary
        s = get_summary()
        open_savings = s.get("potential_monthly_usd", 0)
        verified_savings = s.get("verified_monthly_usd", 0)
    except Exception:
        open_savings = verified_savings = 0.0

    try:
        from ..anomaly.detector import get_active_anomalies
        active = len(get_active_anomalies(limit=100) or [])
    except Exception:
        active = 0

    period_label = f"{this_start.strftime('%b %d')} – {today.strftime('%b %d')}"
    return await slack.send_weekly_insight(
        period_label=period_label,
        grand_total=grand_total,
        prev_total=prev_total,
        top_movers=movers[:5],
        open_savings_usd=open_savings,
        verified_savings_usd=verified_savings,
        active_anomalies=active,
    )
