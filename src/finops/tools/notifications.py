# SPDX-License-Identifier: Apache-2.0
"""notifications MCP tools (extracted from server.py; see finops/tools/__init__.py).

Server-local helpers, globals, and the mcp instance are reached through the live
server module (_srv.NAME) so monkeypatching finops.server.* still works and no
import-order coupling exists."""
from __future__ import annotations

from .. import server as _srv


@_srv.mcp.tool()
async def send_digest_now() -> dict:
    """
    Manually trigger a cost digest to Slack and/or Teams right now.
    Normally this sends automatically at 09:00 UTC daily.

    Examples:
        - "Send the daily cost digest to Slack"
        - "Push the current cost summary to Teams"
    """
    if (err := _srv.require_pro("alerts")):
        return err
    if err := _srv.require_role("analyst"):
        return err

    from ..scheduler.jobs import run_digest_now
    sent = await run_digest_now()
    return {
        "sent": sent,
        "message": "Digest sent." if sent else "No notification channels configured. Run 'uvx nable slack' or 'uvx nable teams' in a terminal.",
    }


@_srv.mcp.tool()
async def check_notification_config() -> dict:
    """
    Check which notification channels (Slack, Teams) are configured and active,
    returning each channel's status and what is missing when one is not set up.
    Use it to verify where anomaly alerts and digests will be delivered before
    relying on them.

    Examples:
        - "Is Slack configured for alerts?"
        - "Where are cost alerts being sent?"
        - "Why did no alert reach Teams?"
    """
    from ..notifications import slack, teams

    return {
        "slack": {
            "configured": slack.is_configured(),
            "method": "webhook" if _srv.os.environ.get("SLACK_WEBHOOK_URL") else "bot_token" if _srv.os.environ.get("SLACK_BOT_TOKEN") else "none",
            "channel": _srv.os.environ.get("SLACK_CHANNEL", "#finops-alerts"),
        },
        "teams": {
            "configured": teams.is_configured(),
        },
        "schedule": {
            "snapshot": _srv.os.environ.get("FINOPS_SNAPSHOT_CRON", "0 1 * * * (01:00 UTC)"),
            "anomaly_check": _srv.os.environ.get("FINOPS_ANOMALY_CRON", "0 2 * * * (02:00 UTC)"),
            "daily_digest": _srv.os.environ.get("FINOPS_DIGEST_CRON", "0 9 * * * (09:00 UTC)"),
        },
    }


@_srv.mcp.tool()
async def export_cost_report(
    title: str | None = None,
    sections: list[str] | None = None,
    formats: list[str] | None = None,
    start_date: str | None = None,
    end_date: str | None = None,
    open_file: bool = True,
) -> dict:
    """
    Export a cost report as HTML (printable to PDF) and/or CSV. Saved to
    ~/.finops/exports/. No Claude Desktop required to open.

    Args:
        title: Report title. Defaults to "Cloud Cost Report <period>".
        sections: Sections to include: cost_summary, services, anomalies,
                  rightsizing, savings, budgets. Default: all.
        formats: ["html", "csv"]. Default: both.
        start_date: ISO date. Defaults to 30 days ago.
        end_date: ISO date. Defaults to today.
        open_file: Open HTML in browser after export (default True).

    Examples:
        - "Export a cost report for this month"
        - "Give me a CSV export of anomalies and rightsizing"
        - "Make a weekly cost report for the team"
    """
    from datetime import date as _date, timedelta
    try:
        sd = _date.fromisoformat(start_date) if start_date else _date.today() - timedelta(days=30)
        ed = _date.fromisoformat(end_date) if end_date else _date.today()
    except ValueError:
        return {"error": "start_date and end_date must be ISO format YYYY-MM-DD."}
    period_start = sd.isoformat()
    period_end = ed.isoformat()

    if title is None:
        title = f"Cloud Cost Report, {period_start} to {period_end}"

    all_sections = ["cost_summary", "services", "anomalies", "rightsizing", "savings", "budgets"]
    wanted = set(sections or all_sections)
    fmt_list = formats or ["html", "csv"]

    collected: dict = {}

    # Gather data for each requested section (errors are non-fatal)
    if "cost_summary" in wanted:
        try:
            collected["cost_summary"] = await _srv.get_cost_summary(
                start_date=period_start, end_date=period_end
            )
        except Exception:
            pass

    if "services" in wanted:
        try:
            collected["services"] = await _srv.get_costs_by_service(
                start_date=period_start, end_date=period_end
            )
        except Exception:
            pass

    if "anomalies" in wanted:
        try:
            collected["anomalies"] = await _srv.get_anomalies(limit=50)
        except Exception:
            pass

    if "rightsizing" in wanted:
        try:
            collected["rightsizing"] = await _srv.get_rightsizing_recommendations()
        except Exception:
            pass

    if "savings" in wanted:
        try:
            from ..recommendations.savings_tracker import get_summary, list_recommendations
            summary = get_summary()
            summary["recommendations"] = list_recommendations(limit=100)
            collected["savings"] = summary
        except Exception:
            pass

    if "budgets" in wanted:
        try:
            collected["budgets"] = await _srv.list_budgets()
        except Exception:
            pass

    if not collected:
        return {
            "error": "No data available to export. Make sure at least one provider is configured.",
        }

    # Write files
    from ..reporting.exporter import write_report
    output = write_report(
        title=title,
        period_start=period_start,
        period_end=period_end,
        sections=collected,
        formats=fmt_list,
    )

    # Open HTML in browser if requested
    if open_file and "html" in output:
        try:
            import subprocess
            subprocess.Popen(["open", output["html"]])
        except Exception:
            pass

    result = {
        "title": title,
        "period": f"{period_start} to {period_end}",
        "sections_included": list(collected.keys()),
        "files": output,
        "message": (
            f"Report generated with {len(collected)} section(s). "
            + (f"HTML: {output.get('html', '')}. " if "html" in output else "")
            + (f"CSVs: {output.get('csv_dir', '')}." if "csv_dir" in output else "")
        ),
    }
    if "html" in output:
        result["tip"] = "Open the HTML file in your browser, then use File → Print → Save as PDF to create a PDF."

    return result


@_srv.mcp.tool()
async def fetch_invoice_emails() -> dict:
    """
    Fetch unread invoice emails from the configured IMAP mailbox, extract
    amounts, and store them as cost entries. Solves the billing API gap for
    vendors like PagerDuty, New Relic, and GitHub Enterprise.

    Examples:
        - "Parse our billing inbox for new invoices"
        - "How much did PagerDuty charge us this month? (after forwarding invoice)"
        - "Fetch and store any new vendor invoices"
    """
    try:
        from ..connectors.invoice.parser import fetch_and_store_invoices
        stored = fetch_and_store_invoices()
        if not stored:
            host = _srv.os.environ.get("FINOPS_INVOICE_IMAP_HOST", "")
            if not host:
                return {
                    "invoices_stored": 0,
                    "message": "No IMAP mailbox configured. Set FINOPS_INVOICE_IMAP_HOST (and FINOPS_INVOICE_IMAP_USER, FINOPS_INVOICE_IMAP_PASSWORD) in your environment, then restart.",
                }
        return {
            "invoices_stored": len(stored),
            "invoices": stored,
        }
    except Exception as e:
        return {"error": str(e)}


@_srv.mcp.tool()
async def push_weekly_insight() -> dict:
    """
    Push a rich weekly cost intelligence summary to Slack right now.

    Covers: week-over-week spend change, top cost movers, open savings pipeline,
    active anomalies, budget alerts, and a single recommended action.

    This is the proactive format, an analyst briefing, not a metric dump.
    Runs automatically every Monday morning when scheduled. Use this to
    trigger it on demand.

    Examples:
        - "Send a weekly cost summary to Slack"
        - "Push the weekly insight to the team channel"
        - "Send this week's cost intelligence to Slack now"
    """
    if (err := _srv.require_pro("alerts")):
        return err
    from datetime import date, timedelta
    from ..notifications import slack

    if not slack.is_configured():
        return {
            "error": "Slack not configured. Run: finops setup slack",
            "tip": "Supports both webhook URL (SLACK_WEBHOOK_URL) and bot token (SLACK_BOT_TOKEN + SLACK_CHANNEL).",
        }

    today = date.today()
    this_week_start = today - timedelta(days=7)
    last_week_start = today - timedelta(days=14)
    last_week_end = today - timedelta(days=8)

    # Gather this-week and last-week totals from snapshots
    try:
        from ..storage.db import get_engine, cost_snapshots
        from sqlalchemy import select, func
        engine = get_engine()

        def _week_total(start: date, end: date) -> dict[str, dict]:
            with engine.connect() as conn:
                rows = conn.execute(
                    select(
                        cost_snapshots.c.provider,
                        cost_snapshots.c.service,
                        func.sum(cost_snapshots.c.amount_usd).label("total"),
                    )
                    .where(
                        cost_snapshots.c.snapshot_date >= start.isoformat(),
                        cost_snapshots.c.snapshot_date <= end.isoformat(),
                    )
                    .group_by(cost_snapshots.c.provider, cost_snapshots.c.service)
                ).fetchall()
            result: dict[str, dict] = {}
            for r in rows:
                key = f"{r.provider}::{r.service}"
                result[key] = {"provider": r.provider, "service": r.service, "total": r.total or 0.0}
            return result

        this_week = _week_total(this_week_start, today)
        last_week = _week_total(last_week_start, last_week_end)

        grand_total = sum(v["total"] for v in this_week.values())
        prev_total = sum(v["total"] for v in last_week.values())

        # Top movers: biggest absolute changes week-over-week
        movers = []
        all_keys = set(this_week) | set(last_week)
        for key in all_keys:
            tw = this_week.get(key, {}).get("total", 0.0)
            lw = last_week.get(key, {}).get("total", 0.0)
            prov = (this_week.get(key) or last_week.get(key) or {}).get("provider", "")
            svc = (this_week.get(key) or last_week.get(key) or {}).get("service", "")
            if tw < 5 and lw < 5:
                continue  # skip noise
            pct = ((tw - lw) / lw * 100) if lw else 100.0
            movers.append({"provider": prov, "service": svc,
                           "this_week": tw, "last_week": lw, "pct_change": pct})
        movers.sort(key=lambda m: -abs(m["pct_change"]))
    except Exception as e:
        # Refuse rather than substitute zeros. This block used to set
        # grand_total = prev_total = 0.0 and carry on, so a snapshot query that
        # never ran was posted to the team's Slack as "Weekly cost: $0 (+0.0% vs
        # last week)" and the tool returned sent: True. Everyone in that channel
        # then believes the bill went to nothing, which is the single most
        # alarming thing a cost tool can say and it was not read from anywhere.
        _srv.log.error("push_weekly_insight: snapshot query failed: %s", e)
        return {
            "sent": False,
            "error": "Could not read the cost snapshots for this week.",
            "detail": str(e)[:300],
            "why_nothing_was_posted": (
                "nable does not publish a figure it did not read. Posting $0 here "
                "would have told the channel your spend collapsed."
            ),
            "fix": "Check the snapshot store, then run take_snapshot_now() and retry.",
        }

    # Savings pipeline
    try:
        from ..recommendations.savings_tracker import get_summary
        savings_summary = get_summary()
        open_savings = savings_summary.get("potential_monthly_usd", 0)
        verified_savings = savings_summary.get("verified_monthly_usd", 0)
    except Exception:
        open_savings = verified_savings = 0.0

    # Active anomalies
    try:
        from ..anomaly.detector import get_active_anomalies
        active_anomaly_count = len(get_active_anomalies(limit=100) or [])
    except Exception:
        active_anomaly_count = 0

    # Budget alerts
    budget_alert_list = []
    try:
        from ..budget.enforcer import list_budgets as _list_budgets_fn
        budgets_data = _list_budgets_fn()
        for b in budgets_data:
            pct = b.get("pct_used", 0) or 0
            if pct >= 75:
                budget_alert_list.append({"name": b.get("name", ""), "pct_used": pct})
        budget_alert_list.sort(key=lambda x: -x["pct_used"])
    except Exception:
        pass

    # Top action heuristic
    top_action = ""
    if active_anomaly_count >= 1:
        top_action = f'Review {active_anomaly_count} anomaly{"s" if active_anomaly_count > 1 else ""}: _"show me the cost anomalies"_'
    elif open_savings > 500:
        top_action = f'${open_savings:,.0f}/mo in open savings: _"show rightsizing recommendations"_'
    elif budget_alert_list:
        top_action = f'Budget alert: {budget_alert_list[0]["name"]} at {budget_alert_list[0]["pct_used"]:.0f}%'

    period_label = f"{this_week_start.strftime('%b %d')} – {today.strftime('%b %d')}"
    sent = await slack.send_weekly_insight(
        period_label=period_label,
        grand_total=grand_total,
        prev_total=prev_total,
        top_movers=movers[:5],
        open_savings_usd=open_savings,
        verified_savings_usd=verified_savings,
        active_anomalies=active_anomaly_count,
        budget_alerts=budget_alert_list,
        top_action=top_action,
    )

    if sent:
        return {
            "sent": True,
            "period": period_label,
            "grand_total_usd": round(grand_total, 2),
            "prev_total_usd": round(prev_total, 2),
            "top_movers_count": len(movers),
            "active_anomalies": active_anomaly_count,
            "open_savings_usd": round(open_savings, 2),
            "message": f"Weekly insight sent to Slack for {period_label}.",
        }
    return {
        "sent": False,
        "error": "Slack send failed, check SLACK_WEBHOOK_URL or SLACK_BOT_TOKEN.",
    }


@_srv.mcp.tool()
async def send_weekly_digest_now() -> dict:
    """
    Immediately send the weekly email digest to the configured recipient.
    Includes spend summary, anomalies, and top rightsizing recommendations.
    Works without Claude, pure standalone email.

    Examples:
        - "Send the weekly cost digest now"
        - "Trigger the weekly email report"
    """
    if (err := _srv.require_pro("alerts")):
        return err
    if err := _srv.require_pro("scheduled_email_digests"):
        return err

    try:
        from ..scheduler.jobs import job_weekly_email_digest
        job_weekly_email_digest()
        to = _srv.os.environ.get("FINOPS_DIGEST_TO", "")
        return {
            "sent": True,
            "recipient": to or "configured address",
            "note": "Check FINOPS_DIGEST_TO / FINOPS_SMTP_* env vars if not received.",
        }
    except Exception as e:
        return {"error": str(e)}


@_srv.mcp.tool()
async def subscribe_to_report(
    name: str,
    sections: list[str],
    frequency: str = "weekly",
    slack_channels: list[str] | None = None,
    email_addresses: list[str] | None = None,
    team: str = "",
    provider: str = "",
    lookback_days: int = 7,
    cron: str = "",
) -> dict:
    """
    Create a scheduled report subscription. Reports are delivered automatically
    to Slack channels and/or email addresses on the configured schedule.

    Args:
        name: Report name (e.g. "Platform Team Weekly")
        sections: List of sections to include. Options:
                  spend, anomalies, scorecard, k8s, commitments, rightsizing, budgets, teams
        frequency: "daily", "weekday", "weekly", "monthly" (or use cron for custom)
        slack_channels: List of Slack channel IDs or names (e.g. ["#finops-alerts"])
        email_addresses: List of email recipients
        team: Scope report to a specific team tag value
        provider: Scope report to a specific cloud provider (aws, azure, gcp)
        lookback_days: How many days of history to include (default 7)
        cron: Custom cron expression, overrides frequency (e.g. "0 8 * * 1-5")

    Examples:
        - "Send me a daily Slack report with spend and anomalies to #finops"
        - "Set up a weekly report for the platform team every Monday"
        - "Create a monthly rightsizing report emailed to cfo@company.com"
        - "Subscribe to a daily digest in #cost-alerts with spend, anomalies, and budgets"
    """
    if (err := _srv.require_pro("alerts")):
        return err
    if err := _srv.require_role("analyst"):
        return err
    try:
        from ..notifications.reports import create_subscription, VALID_SECTIONS
        invalid = [s for s in sections if s not in VALID_SECTIONS]
        if invalid:
            return {
                "error": f"Invalid sections: {invalid}",
                "valid_sections": VALID_SECTIONS,
            }

        # Email delivery is Pro-only, warn at subscription time, don't block creation
        email_note = None
        if email_addresses and _srv.require_pro("scheduled_email_digests") is not None:
            email_note = (
                f"This is a Team feature ($25/mo). Upgrade at {_srv._UPGRADE_URL} to unlock email delivery. "
                f"The subscription will be created with Slack delivery only."
            )
            email_addresses = []  # clear emails on free tier

        filters = {}
        if team:
            filters["team"] = team
        if provider:
            filters["provider"] = provider

        sub = create_subscription(
            name=name,
            sections=sections,
            frequency=frequency,
            slack_channels=slack_channels or [],
            email_addresses=email_addresses or [],
            filters=filters,
            lookback_days=lookback_days,
            cron=cron or None,
        )
        result = {
            "created": True,
            "subscription": sub,
            "message": f"Report '{name}' scheduled (cron: {sub['cron']}). Slack delivery is active.",
            "note": "Reports check every 5 minutes, or trigger manually with send_report_now.",
        }
        if email_note:
            result["pro_required"] = email_note
        return result
    except Exception as e:
        return {"error": str(e)}


@_srv.mcp.tool()
async def list_report_subscriptions() -> dict:
    """
    List all active report subscriptions, their names, schedules, sections, and delivery channels.

    Examples:
        - "What reports are scheduled?"
        - "Show me all active report subscriptions"
        - "List my scheduled reports"
    """
    try:
        from ..notifications.reports import list_subscriptions
        subs = list_subscriptions()
        return {
            "count": len(subs),
            "subscriptions": [
                {
                    "id": s["id"],
                    "name": s["name"],
                    "cron": s["cron"],
                    "sections": s["sections"],
                    "slack_channels": s["slack_channels"],
                    "email_addresses": s["email_addresses"],
                    "filters": s["filters"],
                    "lookback_days": s.get("lookback_days", 7),
                    "last_sent_at": str(s.get("last_sent_at") or "never"),
                }
                for s in subs
            ],
        }
    except Exception as e:
        return {"error": str(e)}


@_srv.mcp.tool()
async def send_report_now(subscription_id: int) -> dict:
    """
    Trigger a report subscription immediately, regardless of its schedule.

    Args:
        subscription_id: ID of the subscription to run (from list_report_subscriptions)

    Examples:
        - "Send report #3 now"
        - "Run the platform team report immediately"
        - "Trigger report subscription 1"
    """
    if (err := _srv.require_pro("alerts")):
        return err
    if err := _srv.require_role("analyst"):
        return err
    try:
        from ..notifications.reports import run_subscription
        result = await run_subscription(subscription_id)
        return result
    except Exception as e:
        return {"error": str(e)}


@_srv.mcp.tool()
async def cancel_report_subscription(subscription_id: int) -> dict:
    """
    Cancel (deactivate) a scheduled report subscription.

    Args:
        subscription_id: ID of the subscription to cancel

    Examples:
        - "Cancel report #2"
        - "Stop the weekly platform report"
        - "Disable subscription 3"
    """
    if err := _srv.require_role("analyst"):
        return err
    try:
        from ..notifications.reports import cancel_subscription
        ok = cancel_subscription(subscription_id)
        return {"cancelled": ok, "subscription_id": subscription_id}
    except Exception as e:
        return {"error": str(e)}


@_srv.mcp.tool()
async def export_cost_report_csv(
    output_path: str | None = None,
    regions: list[str] | None = None,
    top_n: int = 50,
) -> str:
    """
    Runs the full cost audit and exports results to a CSV file.

    Offer this automatically after run_full_cost_audit completes, or when
    the user says "export that", "save to CSV", "download these results",
    "export to spreadsheet", or similar.

    output_path: optional full path for the CSV file. Defaults to
    ~/Downloads/nable-report-YYYY-MM-DD.csv

    Returns the path where the file was saved and a summary.
    Args:
        output_path: Full path for the CSV. Defaults to ~/Downloads/nable-report-<date>.csv.
        regions: AWS regions to scan. Defaults to all enabled regions.
        top_n: How many top results to return.

    Examples:
        - "Export that audit to CSV"
        - "Save the findings as a spreadsheet"

    """
    import csv
    import pathlib

    if err := _srv.require_role("analyst"):
        return _srv.deny_text(err)

    aws = _srv.CLOUD_CONNECTORS.get("aws")
    if aws is None or not await aws.is_configured():
        return "AWS is not connected. Call connect_aws right here in the chat (it detects credentials already on this machine), or run 'uvx nable' in a terminal."

    # Resolve output path
    today = _srv.date.today().isoformat()
    if output_path:
        resolved = _srv._resolve_safe_path(output_path)
        if isinstance(resolved, dict):
            return resolved["error"]
        dest = pathlib.Path(resolved)
    else:
        dest = pathlib.Path.home() / "Downloads" / f"nable-report-{today}.csv"

    # Ensure parent directory exists
    dest.parent.mkdir(parents=True, exist_ok=True)

    # One sweep, shared with run_full_cost_audit and the other export. This used
    # to be a fork of the audit's scanner block, and the fork had rotted: it
    # imported `scan_spot_adoption_opportunities`, a name that has never existed
    # in recommendations.spot_adoption, so this tool raised ImportError on every
    # call it ever received. The fork also predated the per-resource collapse, so
    # its total counted mutually exclusive fixes on one instance as if you could
    # bank all of them.
    from ..recommendations.sweep import collapse_per_resource, sweep

    result = await sweep(aws, regions)
    if result.timed_out:
        return ("The scan took unusually long (a region or API may be slow). Try again, "
                "or pass a specific region.")
    findings = result.findings
    # Slice first, then collapse. Collapsing the whole list first would let one
    # resource's folded-away alternatives push real findings off the end of the
    # export, so the file would be short by however many duplicates existed.
    top, collapsed = collapse_per_resource(findings[:top_n])

    # `or 0` because the critique sets monthly_savings to None on a claim it
    # retracted. Summing that raises; counting it toward a total we are asking a
    # finance team to believe would be worse.
    total_monthly = sum(f.get("monthly_savings") or 0 for f in top)
    total_annual = total_monthly * 12
    scan_ts = _srv.datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")

    # Try to get account ID for summary
    try:
        sts = aws._client("sts")
        account_id = sts.get_caller_identity()["Account"]
    except Exception:
        account_id = "unknown"

    with open(dest, "w", newline="", encoding="utf-8") as fh:
        writer = csv.writer(fh)

        # Summary header block
        writer.writerow(["nable Cost Report"])
        writer.writerow(["Scan timestamp", scan_ts])
        writer.writerow(["AWS account", account_id])
        writer.writerow(["Total monthly saving", f"${total_monthly:,.2f}"])
        writer.writerow(["Total annual saving", f"${total_annual:,.2f}"])
        writer.writerow(["Opportunities found", len(top)])
        writer.writerow([])

        # Column headers
        writer.writerow(["Rank", "Opportunity", "Category", "Monthly Saving ($)", "Annual Saving ($)", "Detail"])

        # Neutralize spreadsheet formula injection (CWE-1236): title/category/detail
        # come from resource names a lower-privileged user can set, and this CSV is
        # opened in Excel by finance. Prefix a leading formula trigger with an apostrophe.
        def _csv_safe(v):
            s = "" if v is None else str(v)
            return "'" + s if s and s[0] in ("=", "+", "-", "@", "\t", "\r") else s

        for i, f in enumerate(top, 1):
            # A retracted claim carries no figure. Writing 0.00 would read as
            # "we checked and it saves nothing", which is a different and untrue
            # statement, so the cell says what actually happened.
            raw = f.get("monthly_savings")
            mo = round(raw, 2) if raw is not None else ""
            yr = round(mo * 12, 2) if raw is not None else ""
            detail = f.get("detail", "")
            if raw is None:
                detail = f"needs confirming ({f.get('magnitude', 'unconfirmed')}); {detail}".strip("; ")
            writer.writerow([i, _csv_safe(f["title"]), _csv_safe(f["category"]), mo, yr, _csv_safe(detail)])

    note = ""
    if collapsed:
        note = (f" {collapsed} alternative fix(es) on the same resources are excluded from "
                f"the total: you can bank one fix per resource, not all of them.")
    return (
        f"Exported {len(top)} opportunities to {dest}. "
        f"Total estimated saving: ${total_monthly:,.2f}/mo (${total_annual:,.2f}/yr).{note}"
    )


@_srv.mcp.tool()
async def push_to_n8n(
    event_type: str = "audit_complete",
    regions: list[str] | None = None,
) -> str:
    """
    Runs the cost audit and pushes results to your n8n workflow via webhook.

    n8n can then trigger any downstream action: create a Jira ticket,
    send a Slack message, update a spreadsheet, page on-call, or anything
    else in your automation stack.

    Setup: in n8n, add a Webhook node and copy the URL. Set N8N_WEBHOOK_URL
    in your environment. Run: finops setup n8n

    event_type options: audit_complete, anomaly_summary

    Use when:
        - "Send the cost report to n8n"
        - "Trigger my n8n workflow"
        - "Push cost findings to my automation"
        - "Wire this into n8n"
    Args:
        event_type: Which payload to send (e.g. "cost_audit").
        regions: AWS regions to scan. Defaults to all enabled regions.

    Examples:
        - "Push the audit results to n8n"

    """
    if (err := _srv.require_pro("alerts")):
        return _srv.deny_text(err)
    import time
    from ..connectors.saas.n8n import N8nConnector

    n8n = N8nConnector()
    if not await n8n.is_configured():
        return (
            "N8N_WEBHOOK_URL is not set. "
            "In n8n, add a Webhook node and copy the URL. "
            "Then run: finops setup n8n"
        )

    if event_type == "anomaly_summary":
        from ..anomaly.detector import get_active_anomalies
        anomalies_list = get_active_anomalies(limit=20)
        if not anomalies_list:
            return "No active anomalies to push to n8n."
        sent = 0
        for anomaly in anomalies_list:
            ok = await n8n.send_anomaly(anomaly)
            if ok:
                sent += 1
        return (
            f"Pushed {sent}/{len(anomalies_list)} anomaly events to n8n."
        )

    # Default: audit_complete
    try:
        from ..analyzers.optimizer import run_deep_audit
        t0 = time.monotonic()
        report = run_deep_audit(regions=regions)
        duration = time.monotonic() - t0

        findings = report.get("findings", [])
        monthly_savings = report.get("total_estimated_monthly_savings", 0.0)

        aws = _srv.CLOUD_CONNECTORS.get("aws")
        account = ""
        if aws is not None:
            try:
                import boto3
                sts = boto3.client("sts")
                account = sts.get_caller_identity().get("Account", "")
            except Exception:
                pass

        ok = await n8n.send_audit_summary(
            findings=findings,
            total_savings=monthly_savings,
            account=account,
            scan_duration_s=duration,
        )

        if ok:
            return (
                f"Pushed audit_complete event to n8n. "
                f"{len(findings)} findings, ${monthly_savings:,.2f}/mo potential savings."
            )
        return "n8n webhook call failed. Check N8N_WEBHOOK_URL and that the webhook node is active."
    except Exception as exc:
        _srv.log.error("push_to_n8n audit failed: %s", exc, exc_info=True)
        return f"Audit failed: {exc}"


@_srv.mcp.tool()
async def publish_cost_report_to_notion(
    regions: list[str] | None = None,
) -> str:
    """
    Runs the full cost audit and publishes results to your team's Notion page.

    The Notion page can be shared with anyone on the team, they don't need
    nable installed. Use this to give leadership, finance, and engineering
    leads a shared cost view without a separate dashboard.

    Requires NOTION_API_KEY and NOTION_PAGE_ID environment variables.
    Set them with: finops setup notion

    Use when:
        - "Share the cost report with my team"
        - "Publish this to Notion"
        - "Update the team dashboard"
        - "Post the cost summary to Notion"
    Args:
        regions: AWS regions to scan. Defaults to all enabled regions.

    Examples:
        - "Publish the cost report to Notion"
        - "Share this audit with the team"

    """
    if err := _srv.require_role("analyst"):
        return _srv.deny_text(err)

    from ..connectors.saas.notion import NotionConnector
    notion = NotionConnector()

    if not await notion.is_configured():
        return (
            "Notion is not configured. Set NOTION_API_KEY and NOTION_PAGE_ID, "
            "or run: finops setup notion"
        )

    aws = _srv.CLOUD_CONNECTORS.get("aws")
    if aws is None or not await aws.is_configured():
        return "AWS is not connected. Call connect_aws right here in the chat (it detects credentials already on this machine), or run 'uvx nable' in a terminal."

    # One sweep, shared with run_full_cost_audit and the other export. This used
    # to be a fork of the audit's scanner block, and the fork had rotted: it
    # imported `scan_spot_adoption_opportunities`, a name that has never existed
    # in recommendations.spot_adoption, so this tool raised ImportError on every
    # call it ever received. The fork also predated the per-resource collapse, so
    # its total counted mutually exclusive fixes on one instance as if you could
    # bank all of them.
    from datetime import datetime as _dt

    from ..recommendations.sweep import collapse_per_resource, sweep

    result = await sweep(aws, regions)
    if result.timed_out:
        return ("The scan took unusually long (a region or API may be slow). Try again, "
                "or pass a specific region.")
    findings = result.findings
    # Slice first, then collapse: see the CSV export for why the order matters.
    top_findings, collapsed = collapse_per_resource(findings[:20])

    if not top_findings:
        return "No savings opportunities found, nothing to publish."

    # `or 0`: the critique sets monthly_savings to None on a retracted claim.
    total_monthly = sum(f.get("monthly_savings") or 0 for f in top_findings)
    total_annual = total_monthly * 12

    account_name = ""
    try:
        accounts = await aws.list_accounts()
        if accounts:
            account_name = accounts[0].get("name", "")
    except Exception:
        pass

    report = {
        "findings": top_findings,
        "total_monthly_savings": total_monthly,
        "total_annual_savings": total_annual,
        "scan_timestamp": _dt.utcnow().strftime("%Y-%m-%d %H:%M UTC"),
        "account": account_name,
    }

    try:
        page_url = await notion.write_cost_report(report)
    except Exception as e:
        _srv.log.error("publish_cost_report_to_notion failed: %s", e, exc_info=True)
        return f"Failed to publish to Notion: {e}"

    collapsed_note = ""
    if collapsed:
        collapsed_note = (
            f"\n\n{collapsed} alternative fix(es) on the same resources are excluded "
            f"from the total: you can bank one fix per resource, not all of them.")
    return (
        f"Cost report published to Notion.\n\n"
        f"URL: {page_url}\n\n"
        f"Findings: {len(top_findings)} opportunities, "
        f"${total_monthly:,.2f}/mo estimated saving "
        f"(${total_annual:,.2f}/yr).{collapsed_note}\n\n"
        f"Share the page with your team from Notion. "
        f"They don't need nable installed to view it."
    )
