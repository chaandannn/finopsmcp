# SPDX-License-Identifier: Apache-2.0
"""tickets MCP tools (extracted from server.py; see finops/tools/__init__.py).

Server-local helpers, globals, and the mcp instance are reached through the live
server module (_srv.NAME) so monkeypatching finops.server.* still works and no
import-order coupling exists."""
from __future__ import annotations

from .. import server as _srv


@_srv.mcp.tool()
async def send_onboarding_email(
    to_email: str,
    variant: str = "welcome",
    days_left: int = 3,
) -> dict:
    """
    Send an onboarding email to a specific address.

    Variants:
      welcome    → "Here's how easy setup is", sent on email capture
      day7       → Nudge for users who haven't connected a provider yet
      trial_end  → Trial expiring in N days, soft upgrade prompt

    Args:
        to_email: Recipient email address
        variant: "welcome", "day7", or "trial_end"
        days_left: For trial_end variant, days until trial expires

    Examples:
        - "Send the welcome email to john@example.com"
        - "Send a day 7 nudge to user@company.com"
        - "Send the trial ending email to someone@corp.com with 3 days left"
    """
    if err := _srv.require_role("admin"):
        return err
    try:
        from ..notifications.onboarding_email import send_welcome, send_day7_nudge, send_trial_ending
        if variant == "welcome":
            ok = send_welcome(to_email)
            subject = "Ask Claude about your cloud bill, here's how (10 min setup)"
        elif variant == "day7":
            ok = send_day7_nudge(to_email)
            subject = "Quick check-in, did nable setup go okay?"
        elif variant == "trial_end":
            ok = send_trial_ending(to_email, days_left)
            subject = f"nable trial ends in {days_left} day{'s' if days_left != 1 else ''}"
        else:
            return {"error": f"Unknown variant '{variant}'. Use: welcome, day7, trial_end"}

        if ok:
            return {"sent": True, "to": to_email, "variant": variant, "subject": subject}
        return {
            "sent": False,
            "error": "SMTP not configured. Set FINOPS_SMTP_HOST, FINOPS_SMTP_USER, FINOPS_SMTP_PASSWORD.",
        }
    except Exception as e:
        return {"error": str(e)}


@_srv.mcp.tool()
async def generate_account_dashboard(
    account_id: str | None = None,
    open_browser: bool = True,
    push_to_notion: bool = False,
) -> dict:
    """
    Generate a cost dashboard for the account and open it in your browser.

    Shows total spend this month vs last month, projected spend, top cost
    drivers by service, open optimization opportunities, realized savings,
    and budget status. Outputs a self-contained HTML file.

    Args:
        account_id:     AWS account ID to scope the dashboard. Auto-detected
                        from your configured credentials when omitted.
        open_browser:   Open the HTML file in the default browser (default True).
        push_to_notion: Also push a summary to your configured Notion page
                        (requires NOTION_API_KEY and NOTION_PAGE_ID env vars).

    Use when:
        - "Show me a dashboard"
        - "Give me a summary of my costs"
        - "Generate the account dashboard"
        - "What does my cost health look like?"
    Examples:
        - "Build me a dashboard for the prod account"
        - "Generate an account cost dashboard and open it"

    """
    import subprocess
    import sys

    aws = _srv.CLOUD_CONNECTORS.get("aws")
    aws_configured = aws and await aws.is_configured()

    try:
        from ..reporting.dashboard import generate_account_dashboard as _gen
        result = await _gen(
            aws_connector=aws if aws_configured else None,
            account_id=account_id,
        )
    except Exception as exc:
        return {"error": str(exc)}

    path = result["path"]

    if open_browser:
        try:
            if sys.platform == "darwin":
                subprocess.Popen(["open", path])
            elif sys.platform.startswith("linux"):
                subprocess.Popen(["xdg-open", path])
            elif sys.platform == "win32":
                _srv.os.startfile(path)  # noqa: S606, startfile is safe; no shell
        except Exception:
            pass  # opening the browser is best-effort

    if push_to_notion:
        try:
            from ..connectors.saas.notion import NotionConnector
            notion = NotionConnector()
            if await notion.is_configured():
                opp_total = result.get("opportunity_savings_usd", 0.0)
                opps: list[dict] = []
                try:
                    from ..recommendations.savings_tracker import list_recommendations
                    opps = list_recommendations(status="open", limit=20)
                except Exception:
                    pass
                notion_report = {
                    "account": result.get("account_id", ""),
                    "total_monthly_savings": opp_total,
                    "total_annual_savings": opp_total * 12,
                    "findings": [
                        {
                            "title": o.get("description", o.get("resource_name", "")),
                            "category": o.get("source", ""),
                            "monthly_savings": o.get("estimated_monthly_savings_usd", 0.0),
                        }
                        for o in opps
                    ],
                }
                notion_url = await notion.write_cost_report(notion_report)
                result["notion_url"] = notion_url
            else:
                result["notion_note"] = (
                    "Notion is not configured. Set NOTION_API_KEY and NOTION_PAGE_ID "
                    "to enable Notion push."
                )
        except Exception as exc:
            result["notion_error"] = str(exc)

    return result


@_srv.mcp.tool()
async def create_anomaly_tickets(limit: int = 20) -> dict:
    """
    Create tickets in Jira, Linear, or GitHub Issues for all active high/medium
    anomalies that don't already have a ticket. Uses the first configured
    ticketing provider.

    Args:
        limit: Max number of anomalies to process (default 20)

    Examples:
        - "Create Jira tickets for all cost anomalies"
        - "File GitHub issues for the anomalies"
        - "Open Linear tasks for cost spikes"
    """
    if err := _srv.require_pro("ticket_creation"):
        return err

    try:
        from ..integrations.ticketing import create_tickets_for_unnotified
        urls = create_tickets_for_unnotified(limit=limit)
        return {
            "tickets_created": len(urls),
            "ticket_urls": urls,
        }
    except Exception as e:
        return {"error": str(e)}


@_srv.mcp.tool()
async def create_rightsizing_tickets(
    min_monthly_savings: float = 100.0,
    provider: str = "aws",
) -> dict:
    """
    Create tickets for rightsizing recommendations, over-provisioned EC2, RDS,
    and other resources that could be downsized to save money.

    Args:
        min_monthly_savings: Only ticket recommendations above this threshold (default $100/mo)
        provider: Cloud provider to pull recommendations from (default: aws)

    Examples:
        - "Create Jira tickets for all rightsizing opportunities"
        - "File issues for EC2 instances we should downsize"
        - "Open Linear tasks for $500+ monthly rightsizing savings"
    """
    if err := _srv.require_pro("ticket_creation"):
        return err

    if provider != "aws":
        return {
            "message": "Rightsizing analysis is AWS-only (Compute Optimizer + CloudWatch).",
            "tickets_created": 0,
        }

    try:
        from ..integrations.ticketing import create_rightsizing_ticket
        from ..recommendations.rightsizing import analyze_rightsizing

        recs = await _srv.asyncio.to_thread(analyze_rightsizing, min_monthly_savings=min_monthly_savings)
        if not recs:
            return {"message": "No rightsizing recommendations found", "tickets_created": 0}

        urls = []
        skipped = 0
        for r in recs:
            savings = r.monthly_savings
            if savings < min_monthly_savings:
                skipped += 1
                continue
            # Map the engine's dataclass to the dict shape create_rightsizing_ticket expects.
            rec = {
                "resource_id": r.instance_id,
                "resource_type": r.resource_type,
                "current_type": r.instance_type,
                "recommended_type": r.recommended_type,
                "monthly_savings_usd": savings,
            }
            url = create_rightsizing_ticket(rec)
            if url:
                urls.append({"resource": r.instance_id, "savings": savings, "url": url})

        return {
            "tickets_created": len(urls),
            "skipped_below_threshold": skipped,
            "threshold_usd": min_monthly_savings,
            "tickets": urls,
        }
    except Exception as e:
        return {"error": str(e)}


@_srv.mcp.tool()
async def create_scorecard_tickets(
    score_threshold: int = 50,
    team: str = "",
) -> dict:
    """
    Create tickets for scorecard dimensions scoring below a threshold.
    Helps teams track and remediate FinOps efficiency gaps.

    Args:
        score_threshold: Create tickets for dimensions below this score (default 50)
        team: Scope to a specific team tag (optional)

    Examples:
        - "Create tickets for all failing scorecard dimensions"
        - "File issues for the platform team's low scores"
        - "Open Jira tasks for scorecard dimensions below 40"
    """
    if err := _srv.require_pro("ticket_creation"):
        return err

    try:
        from ..scoring.scorecard import build_scorecard
        from ..integrations.ticketing import create_scorecard_ticket

        tag_filter = {"team": team} if team else None
        scorecard = build_scorecard(tag_filter=tag_filter)

        if not scorecard:
            return {"error": "Could not build scorecard"}

        urls = []
        for dim in scorecard.as_dict().get("dimensions", []):
            if dim.get("score", 100) < score_threshold:
                url = create_scorecard_ticket(dim, team=team)
                if url:
                    urls.append({
                        "dimension": dim["dimension"],
                        "score": dim["score"],
                        "grade": dim["grade"],
                        "url": url,
                    })

        return {
            "tickets_created": len(urls),
            "overall_score": scorecard.as_dict().get("overall_score"),
            "overall_grade": scorecard.as_dict().get("grade"),
            "tickets": urls,
        }
    except Exception as e:
        return {"error": str(e)}


@_srv.mcp.tool()
async def create_ticket(
    title: str,
    body: str,
    priority: str = "medium",
    labels: list[str] | None = None,
) -> dict:
    """
    Create a ticket in the configured ticketing system (Jira, Linear, or GitHub Issues)
    with a custom title and body. Use this for any finding, recommendation, or action
    item that doesn't fit a specific category.

    Args:
        title: Ticket title / issue summary
        body:  Full ticket description with context and action items
        priority: "low", "medium", "high", or "critical" (default: medium)
        labels: Optional list of labels/tags to apply (default: ["finops"])

    Examples:
        - "Create a Jira ticket to disable Textract in non-prod environments"
        - "File a GitHub issue to switch LambdaClassifier from Sonnet to Haiku"
        - "Open a Linear task for the NAT gateway consolidation"
    """
    if err := _srv.require_pro("ticket_creation"):
        return err

    try:
        from ..integrations.ticketing import create_custom_ticket as _create

        url = _create(title=title, body=body, priority=priority, labels=labels or ["finops"])
        if not url:
            return {
                "error": "Ticket was not created. Check that JIRA_URL / LINEAR_API_KEY / GITHUB_TOKEN is configured.",
                "hint": "Run: finops setup to configure your ticketing integration.",
            }
        return {"ticket_url": url, "title": title, "priority": priority}
    except Exception as e:
        return {"error": str(e)}


@_srv.mcp.tool()
async def export_board_summary(period_days: int = 30) -> dict:
    """
    Generate the cost section of a board / investor update as markdown.

    Pulls your unit economics (cost per customer, AI spend as a share of
    revenue and per customer, hosting as % of revenue), runway, and the latest
    cost-change narrative into a concise, board-ready markdown block you can
    paste into an update. The markdown is built on your machine from your own
    data. No nable backend holds or sees it (it does read your cloud and AI
    billing APIs to total the spend).

    Requires business metrics set with set_business_metrics().

    Examples:
        - "Generate our board update cost section"
        - "Export a board-ready cost summary"
        - "Give me the markdown for our investor update infra section"
    Args:
        period_days: Reporting period in days (default 30).

    """

    econ = await _srv.get_unit_economics(period_days=period_days)
    if econ.get("error"):
        return econ
    change = await _srv.explain_cost_change(compare_days=period_days)

    ue = econ.get("unit_economics", {})
    runway = econ.get("runway", {})
    drivers = change.get("cost_drivers", []) if isinstance(change, dict) else []

    # Resolve metrics (Stripe-fed) and AI spend so the summary shows the margin
    # lens a board actually asks about: what AI costs as a share of revenue and
    # per customer, not just total infra. resolve hits the stored row here (the
    # get_unit_economics call above already triggered any Stripe pull), so this
    # adds no extra external call.
    from ..connectors.business_metrics import resolve_business_metrics
    metrics = await resolve_business_metrics()
    mrr = metrics.get("mrr_usd") or (
        metrics.get("arr_usd") / 12 if metrics.get("arr_usd") else None
    )
    customers = metrics.get("paying_customers")

    ai_monthly = None
    try:
        from ..connectors.llm_costs import get_all_llm_costs
        _ai = get_all_llm_costs(
            start_date=_srv.date.today() - _srv.timedelta(days=period_days),
            end_date=_srv.date.today(),
        )
        _ai_total = _ai.get("total_usd", 0.0) or 0.0
        if _ai_total > 0:
            ai_monthly = _ai_total * (30.0 / period_days) if period_days else _ai_total
    except Exception as e:
        _srv.log.debug("board summary AI spend fetch failed: %s", e)

    lines: list[str] = []
    lines.append("## Infrastructure & AI Spend")
    lines.append("")
    lines.append(f"- **Total infra + AI cost ({period_days}d):** {econ.get('total_infrastructure_cost', 'n/a')}")
    if isinstance(change, dict) and change.get("cost_change", {}).get("now"):
        cc = change["cost_change"]
        lines.append(f"- **Spend vs last period:** {cc.get('now')} ({cc.get('pct', 'n/a')})")
    if ue.get("cost_per_customer_label"):
        lines.append(f"- **Cost per customer (all-in):** {ue['cost_per_customer_label']}")

    # AI margin block: the wedge. AI as a share of revenue and per customer.
    if ai_monthly is not None:
        lines.append(f"- **AI spend (monthly run-rate):** ${ai_monthly:,.0f}")
        if mrr and mrr > 0:
            ai_pct = ai_monthly / mrr * 100
            if ai_pct < 15:
                ai_health = "healthy"
            elif ai_pct < 30:
                ai_health = "watch"
            else:
                ai_health = "margin risk"
            lines.append(f"- **AI as % of MRR:** {ai_pct:.1f}% ({ai_health})")
        if customers and customers > 0:
            lines.append(f"- **AI cost per customer:** ${ai_monthly / customers:,.2f} / month")

    if ue.get("hosting_pct_mrr_label"):
        health = ue.get("hosting_pct_mrr_health")
        suffix = f" ({health})" if health else ""
        lines.append(f"- **Hosting as % of MRR:** {ue['hosting_pct_mrr_label']}{suffix}")
    if runway.get("available") and runway.get("label"):
        lines.append(f"- **Runway:** {runway['label']}")
    elif runway.get("reason"):
        lines.append(f"- **Runway:** not available ({runway['reason']})")

    if isinstance(change, dict) and change.get("findings"):
        lines.append("")
        lines.append("**What changed:**")
        for f in change["findings"][:2]:
            lines.append(f"- {f}")

    if drivers:
        lines.append("")
        lines.append("**Top cost movers:**")
        for d in drivers[:3]:
            arrow = "up" if d["direction"] == "up" else "down"
            lines.append(f"- {d['service']}: {arrow} ${abs(d['change_usd']):,.0f}/period")

    markdown = "\n".join(lines)

    # Write to the exports dir so it can be opened without Claude.
    from pathlib import Path
    out_dir = Path.home() / ".finops" / "exports"
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / f"board-summary-{_srv.date.today().isoformat()}.md"
    try:
        out_path.write_text(markdown, encoding="utf-8")
        saved = str(out_path)
    except Exception:
        saved = None

    return {
        "markdown": markdown,
        "saved_to": saved,
        "period_days": period_days,
        "note": "Built on your machine from your own data. No nable backend holds or sees it.",
    }


@_srv.mcp.tool()
async def start_dashboard_server(
    port: int = 8080,
    host: str = "127.0.0.1",
    expose: bool = False,
) -> dict:
    """
    Starts a local web dashboard you can open in a browser. Binds to localhost by
    default. Pass expose=true to bind all interfaces so others on your network can
    reach it (plain HTTP, so only do this on a trusted network or behind a TLS proxy).

    Use when:
        - "Start the dashboard"
        - "Share the dashboard with my team" (use expose=true)
        - "Start the web server"
        - "My team wants to see costs without installing nable"
    Args:
        port: Local TCP port to serve on.
        host: Interface to bind (default 127.0.0.1, local only).
        expose: True binds beyond localhost. Only on a trusted network.

    Examples:
        - "Start the dashboard"
        - "Serve the web dashboard on port 9000"

    """
    try:
        try:
            from ..server_web import start_server_background, _local_ip, set_connectors
            from .. import server_web as _sw
        except ImportError:
            return {
                "status": "unavailable",
                "message": (
                    "The local web dashboard is a hosted/enterprise feature and is not part of the "
                    "open-source nable package. The local product is the MCP server you're using right "
                    "now in your editor. For a hosted dashboard, see https://getnable.com."
                ),
            }
        # Inject the MCP server's already-initialized connectors so the
        # dashboard uses the correct vault/keyring credentials.
        set_connectors({**_srv.CLOUD_CONNECTORS, **_srv.SAAS_CONNECTORS})
        # expose is the gate, and it has to be the ONLY gate. This used to read
        # `"0.0.0.0" if expose else host`, so the else branch passed `host`
        # through untouched and `host="0.0.0.0", expose=False` bound every
        # interface: the opt-in was bypassed by the argument it was meant to
        # guard. `::` did the same thing for IPv6 and was not even covered by the
        # warning below, which compared against the literal "0.0.0.0".
        _LOOPBACK = {"127.0.0.1", "localhost", "::1", ""}
        if expose:
            bind_host = "0.0.0.0"
        elif host in _LOOPBACK:
            bind_host = host or "127.0.0.1"
        else:
            # A non-loopback host without the opt-in is a mistake, not a request.
            # Bind loopback and say so, rather than silently doing the widest
            # possible thing on the user's behalf.
            bind_host = "127.0.0.1"
            ignored_host = host

        _, actual_port = start_server_background(host=bind_host, port=port)
        local_url = f"http://127.0.0.1:{actual_port}"
        result = {
            "status": "running",
            "local_url": local_url,
            "bound_to": bind_host,
        }
        if not expose and host not in _LOOPBACK:
            result["host_ignored"] = (
                f"host={ignored_host!r} was ignored and the server bound to "
                f"127.0.0.1. Binding past loopback needs expose=true, so that "
                f"exposing the dashboard is always a deliberate choice."
            )

        # The password is NOT returned. It is a live credential for a surface
        # that serves the customer's whole bill, and a tool result goes to the
        # model provider and stays in that history. When the operator set it
        # themselves via FINOPS_DASHBOARD_PASSWORD it is very likely a password
        # they use elsewhere, and nable would be the thing that published it.
        if getattr(_sw, "_AUTH_DISABLED", False):
            result["auth"] = ("DISABLED (FINOPS_DASHBOARD_PASSWORD=off). Anyone who can "
                              "reach the port has full access.")
        elif getattr(_sw, "_PASSWORD_AUTO_GENERATED", False):
            result["auth"] = (
                "An auto-generated password was printed to the terminal running "
                "nable. Read it there, or set FINOPS_DASHBOARD_PASSWORD to choose "
                "your own. It is not repeated here: this response is sent to the "
                "model provider and kept in its history."
            )
        else:
            result["auth"] = (
                "Log in with the password you set in FINOPS_DASHBOARD_PASSWORD."
            )

        if bind_host not in _LOOPBACK:
            result["share_url"] = f"http://{_local_ip()}:{actual_port}"
            result["exposure_warning"] = (
                "Bound to all interfaces. The dashboard is reachable across your LAN/VPN over plain "
                "HTTP, so the password and session cookie travel in cleartext. Only do this on a trusted "
                "network, or put it behind a TLS-terminating proxy."
            )
        result["message"] = (
            f"Dashboard running at {local_url}. "
            + ("Auth is OFF."
               if getattr(_sw, "_AUTH_DISABLED", False)
               else "See `auth` for where to get the password.")
        )
        return result
    except Exception as exc:
        _srv.log.error("start_dashboard_server failed: %s", exc)
        return {"error": str(exc)}
