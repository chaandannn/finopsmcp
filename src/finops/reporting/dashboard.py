"""
Per-account cost dashboard generator.

Produces a self-contained HTML file showing cost health at a glance:
  - Total spend this month vs last month
  - Projected spend for the month (if forecast data available)
  - Top 5 cost drivers by service
  - Open optimization opportunities and estimated savings
  - Realized savings ledger (acted_on + verified)
  - Budget status (if budgets configured)
  - Last updated timestamp

Outputs to ~/.finops/dashboards/ by default.
"""
from __future__ import annotations

import html
import os
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

_DASHBOARD_DIR = Path.home() / ".finops" / "dashboards"


def _esc(v: Any) -> str:
    """Escape any provider-derived string before it enters generated HTML.
    Resource names, tags, and recommendation descriptions come from cloud
    metadata a tenant does not fully control (an EC2 Name tag or S3 bucket
    named with an <img onerror> payload would otherwise be stored HTML/JS in
    the operator's dashboard). Never interpolate these raw."""
    return html.escape(str(v))


def _dashboard_dir() -> Path:
    _DASHBOARD_DIR.mkdir(parents=True, exist_ok=True)
    return _DASHBOARD_DIR


def _fmt_usd(amount: float, decimals: int = 0) -> str:
    if decimals == 0:
        return f"${amount:,.0f}"
    return f"${amount:,.2f}"


def _delta_label(this_month: float, last_month: float) -> tuple[str, str]:
    """Returns (label, css_class) for the month-over-month delta."""
    if last_month == 0:
        return "n/a", "neutral"
    delta = this_month - last_month
    pct = (delta / last_month) * 100
    sign = "+" if delta >= 0 else ""
    css = "up" if delta > 0 else "down"
    return f"{sign}{_fmt_usd(delta)} ({sign}{pct:.1f}%)", css


def _build_html(
    account_id: str,
    this_month: float,
    last_month: float,
    projected: float | None,
    top_services: list[dict],          # [{service, this_month, last_month}]
    opportunities: list[dict],         # [{description, category, estimated_monthly_savings_usd}]
    savings_summary: dict,             # from savings_tracker.get_summary()
    savings_ledger: list[dict],        # acted_on + verified items
    budgets: list[dict],               # from check_all_budgets()
    generated_at: str,
) -> str:
    delta_text, delta_class = _delta_label(this_month, last_month)
    opp_count = len(opportunities)
    opp_total = sum(o.get("estimated_monthly_savings_usd", 0) for o in opportunities)
    verified_savings = savings_summary.get("verified_monthly_usd", 0)
    acted_savings = savings_summary.get("acted_on_monthly_usd", 0)

    # Summary stat cards
    projected_html = _fmt_usd(projected) if projected else "n/a"

    # Top services table rows
    svc_rows = ""
    for svc in top_services[:5]:
        svc_name = svc.get("service", "Unknown")
        tm = svc.get("this_month", 0.0)
        lm = svc.get("last_month", 0.0)
        d = tm - lm
        sign = "+" if d >= 0 else ""
        delta_cls = "up" if d > 0 else ("down" if d < 0 else "neutral")
        svc_rows += f"""
        <tr>
          <td>{_esc(svc_name)}</td>
          <td class="num">{_fmt_usd(tm, 2)}</td>
          <td class="num">{_fmt_usd(lm, 2)}</td>
          <td class="num {delta_cls}">{sign}{_fmt_usd(d, 2)}</td>
        </tr>"""

    if not svc_rows:
        svc_rows = '<tr><td colspan="4" class="empty">No service data available</td></tr>'

    # Opportunities table rows
    opp_rows = ""
    for opp in opportunities[:20]:
        desc = opp.get("description", opp.get("title", ""))
        cat = opp.get("category", opp.get("source", ""))
        saving = opp.get("estimated_monthly_savings_usd", 0.0)
        opp_rows += f"""
        <tr>
          <td>{_esc(desc)}</td>
          <td class="chip">{_esc(cat)}</td>
          <td class="num success">{_fmt_usd(saving, 2)}/mo</td>
        </tr>"""

    if not opp_rows:
        opp_rows = '<tr><td colspan="3" class="empty">No open opportunities. Run rightsizing or waste scan to populate.</td></tr>'

    # Savings ledger rows
    ledger_rows = ""
    for item in savings_ledger[:20]:
        desc = item.get("description", item.get("resource_name", ""))
        status = item.get("status", "")
        status_cls = "verified" if status == "verified" else "acted"
        est = item.get("estimated_monthly_savings_usd", 0.0)
        verified_amt = item.get("verified_monthly_savings_usd")
        amt_str = _fmt_usd(verified_amt, 2) if verified_amt else _fmt_usd(est, 2)
        source = item.get("source", "")
        ledger_rows += f"""
        <tr>
          <td>{_esc(desc)}</td>
          <td class="chip">{_esc(source)}</td>
          <td class="chip {status_cls}">{_esc(status)}</td>
          <td class="num success">{amt_str}/mo</td>
        </tr>"""

    if not ledger_rows:
        ledger_rows = '<tr><td colspan="4" class="empty">No savings recorded yet. Mark recommendations as acted on to build history.</td></tr>'

    # Budget rows
    budget_rows = ""
    for b in budgets[:10]:
        name = b.get("name", "")
        pct = b.get("pct_used", 0.0)
        status = b.get("status", "ok")
        limit = b.get("limit_usd", 0.0)
        spent = b.get("spent_usd", 0.0)
        status_cls = {"exceeded": "alert", "warning": "warn", "ok": "success"}.get(status, "neutral")
        bar_width = min(int(pct), 100)
        budget_rows += f"""
        <tr>
          <td>{_esc(name)}</td>
          <td class="num">{_fmt_usd(spent, 2)}</td>
          <td class="num">{_fmt_usd(limit, 2)}</td>
          <td>
            <div class="bar-wrap">
              <div class="bar {status_cls}" style="width:{bar_width}%"></div>
            </div>
            <span class="{status_cls}">{pct:.1f}%</span>
          </td>
          <td class="chip {status_cls}">{_esc(status)}</td>
        </tr>"""

    budget_section = ""
    if budgets:
        budget_section = f"""
      <section>
        <h2>Budget Status</h2>
        <table>
          <thead>
            <tr><th>Budget</th><th>Spent</th><th>Limit</th><th>Usage</th><th>Status</th></tr>
          </thead>
          <tbody>{budget_rows}</tbody>
        </table>
      </section>"""

    # Delta card class
    delta_card_cls = "stat-delta up" if delta_class == "up" else ("stat-delta down" if delta_class == "down" else "stat-delta neutral")

    today = date.today().strftime("%B %Y")
    account_label = account_id or "All accounts"

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>nable · Account Dashboard · {_esc(account_label)}</title>
<!-- No webfont link: this page must render with zero network calls. --><style>
  :root {{
    --bg:        #0d0f10;
    --bg-1:      #111416;
    --bg-2:      #181c1f;
    --bg-3:      #1e2327;
    --line:      #242a2e;
    --line-2:    #2e3539;
    --fg:        #f0f2f3;
    --fg-2:      #94a3ab;
    --fg-3:      #56656d;
    --accent:    #4db8d4;
    --success:   #3cba7a;
    --warn:      #e6a840;
    --alert:     #e05c4b;
  }}

  *, *::before, *::after {{ box-sizing: border-box; margin: 0; padding: 0; }}

  body {{
    /* Bricolage Grotesque was retired in DESIGN.md and was never going to load
       here anyway once the remote stylesheet went. Geist is the shipped face. */
    font-family: 'Geist', system-ui, sans-serif;
    background: var(--bg);
    color: var(--fg);
    font-size: 14px;
    line-height: 1.5;
    min-height: 100vh;
    padding: 0 0 64px;
  }}

  header {{
    border-bottom: 1px solid var(--line);
    padding: 20px 32px;
    display: flex;
    align-items: baseline;
    gap: 16px;
  }}

  header .logo {{
    font-size: 13px;
    font-weight: 600;
    letter-spacing: 0.06em;
    text-transform: uppercase;
    color: var(--accent);
  }}

  header h1 {{
    font-size: 14px;
    font-weight: 400;
    color: var(--fg-2);
  }}

  header .period {{
    font-size: 13px;
    color: var(--fg-3);
    margin-left: auto;
  }}

  .container {{
    max-width: 1100px;
    margin: 0 auto;
    padding: 0 32px;
  }}

  .stats {{
    display: grid;
    grid-template-columns: repeat(4, 1fr);
    gap: 1px;
    background: var(--line);
    border: 1px solid var(--line);
    margin: 32px 0;
  }}

  .stat {{
    background: var(--bg-1);
    padding: 24px 20px;
  }}

  .stat-label {{
    font-size: 11px;
    font-weight: 500;
    letter-spacing: 0.08em;
    text-transform: uppercase;
    color: var(--fg-3);
    margin-bottom: 8px;
  }}

  .stat-value {{
    font-size: 28px;
    font-weight: 300;
    letter-spacing: -0.03em;
    color: var(--fg);
    font-variant-numeric: tabular-nums;
  }}

  .stat-sub {{
    font-size: 12px;
    color: var(--fg-3);
    margin-top: 4px;
  }}

  .stat-delta {{ font-size: 13px; margin-top: 4px; }}
  .stat-delta.up {{ color: var(--alert); }}
  .stat-delta.down {{ color: var(--success); }}
  .stat-delta.neutral {{ color: var(--fg-3); }}

  section {{
    margin-bottom: 40px;
  }}

  section h2 {{
    font-size: 11px;
    font-weight: 500;
    letter-spacing: 0.08em;
    text-transform: uppercase;
    color: var(--fg-3);
    border-bottom: 1px solid var(--line);
    padding-bottom: 8px;
    margin-bottom: 0;
  }}

  table {{
    width: 100%;
    border-collapse: collapse;
  }}

  thead th {{
    font-size: 11px;
    font-weight: 500;
    letter-spacing: 0.06em;
    text-transform: uppercase;
    color: var(--fg-3);
    text-align: left;
    padding: 10px 12px;
    border-bottom: 1px solid var(--line);
    background: var(--bg-1);
  }}

  tbody tr {{
    border-bottom: 1px solid var(--line);
    transition: background 0.1s;
  }}

  tbody tr:hover {{ background: var(--bg-2); }}

  tbody td {{
    padding: 10px 12px;
    color: var(--fg-2);
    font-size: 13px;
  }}

  tbody td:first-child {{ color: var(--fg); }}

  .num {{
    font-variant-numeric: tabular-nums;
    text-align: right;
    font-family: 'Geist Mono', 'JetBrains Mono', monospace;
    font-size: 12px;
  }}

  .up {{ color: var(--alert); }}
  .down {{ color: var(--success); }}
  .success {{ color: var(--success); }}
  .warn {{ color: var(--warn); }}
  .alert {{ color: var(--alert); }}
  .neutral {{ color: var(--fg-3); }}

  .empty {{
    color: var(--fg-3);
    font-style: italic;
    text-align: center;
    padding: 20px 12px;
  }}

  .chip {{
    display: inline-block;
    font-size: 10px;
    font-weight: 500;
    letter-spacing: 0.04em;
    text-transform: uppercase;
    padding: 2px 6px;
    border-radius: 2px;
    background: var(--bg-3);
    color: var(--fg-3);
    border: 1px solid var(--line);
  }}

  .chip.verified {{ background: rgba(60,186,122,0.12); color: var(--success); border-color: rgba(60,186,122,0.25); }}
  .chip.acted {{ background: rgba(77,184,212,0.12); color: var(--accent); border-color: rgba(77,184,212,0.25); }}
  .chip.warn {{ background: rgba(230,168,64,0.12); color: var(--warn); border-color: rgba(230,168,64,0.25); }}
  .chip.alert {{ background: rgba(224,92,75,0.12); color: var(--alert); border-color: rgba(224,92,75,0.25); }}
  .chip.success {{ background: rgba(60,186,122,0.12); color: var(--success); border-color: rgba(60,186,122,0.25); }}

  .bar-wrap {{
    display: inline-block;
    width: 80px;
    height: 4px;
    background: var(--bg-3);
    border-radius: 2px;
    vertical-align: middle;
    margin-right: 6px;
  }}

  .bar {{
    height: 100%;
    border-radius: 2px;
    background: var(--accent);
  }}
  .bar.warn {{ background: var(--warn); }}
  .bar.alert {{ background: var(--alert); }}
  .bar.success {{ background: var(--success); }}
  .bar.neutral {{ background: var(--fg-3); }}

  footer {{
    margin-top: 48px;
    padding-top: 16px;
    border-top: 1px solid var(--line);
    font-size: 11px;
    color: var(--fg-3);
  }}

  @media (max-width: 700px) {{
    header {{ flex-wrap: wrap; padding: 16px; }}
    .container {{ padding: 0 16px; }}
    .stats {{ grid-template-columns: repeat(2, 1fr); }}
    .stat-value {{ font-size: 22px; }}
    header .period {{ width: 100%; }}
  }}
</style>
</head>
<body>
<header>
  <span class="logo">nable</span>
  <h1>Account Dashboard &middot; {_esc(account_label)}</h1>
  <span class="period">{today}</span>
</header>

<div class="container">
  <div class="stats">
    <div class="stat">
      <div class="stat-label">Total spend this month</div>
      <div class="stat-value">{_fmt_usd(this_month)}</div>
      <div class="stat-sub">Month to date</div>
    </div>
    <div class="stat">
      <div class="stat-label">vs last month</div>
      <div class="stat-value">{_fmt_usd(last_month)}</div>
      <div class="{delta_card_cls}">{delta_text}</div>
    </div>
    <div class="stat">
      <div class="stat-label">Projected this month</div>
      <div class="stat-value">{projected_html}</div>
      <div class="stat-sub">Full-month estimate</div>
    </div>
    <div class="stat">
      <div class="stat-label">Savings found</div>
      <div class="stat-value success">{_fmt_usd(opp_total)}/mo</div>
      <div class="stat-sub">{opp_count} open opportunit{'y' if opp_count == 1 else 'ies'} &middot; {_fmt_usd(verified_savings)}/mo verified</div>
    </div>
  </div>

  <section>
    <h2>Top Cost Drivers</h2>
    <table>
      <thead>
        <tr><th>Service</th><th>This month</th><th>Last month</th><th>Delta</th></tr>
      </thead>
      <tbody>{svc_rows}</tbody>
    </table>
  </section>

  <section>
    <h2>Optimization Opportunities</h2>
    <table>
      <thead>
        <tr><th>Opportunity</th><th>Category</th><th>Est. Saving</th></tr>
      </thead>
      <tbody>{opp_rows}</tbody>
    </table>
  </section>

  <section>
    <h2>Savings Ledger</h2>
    <table>
      <thead>
        <tr><th>Item</th><th>Source</th><th>Status</th><th>Saving</th></tr>
      </thead>
      <tbody>{ledger_rows}</tbody>
    </table>
  </section>

  {budget_section}

  <footer>
    Generated by nable &middot; {generated_at}
  </footer>
</div>
</body>
</html>"""


async def generate_account_dashboard(
    aws_connector: Any = None,
    account_id: str | None = None,
    output_path: str | None = None,
) -> dict:
    """
    Generate a per-account cost dashboard as a local HTML file.

    Pulls this month's and last month's AWS costs, open optimization
    opportunities from the savings tracker, realized savings, and budget
    status. Renders a self-contained HTML file and returns its path.

    Args:
        aws_connector: AWSConnector instance (or None to skip AWS data).
        account_id:    AWS account ID to label the dashboard. Auto-detected
                       from the connector when not provided.
        output_path:   Full path for the output HTML file. Defaults to
                       ~/.finops/dashboards/dashboard-{account}-{date}.html

    Returns:
        {"path": str, "summary": str}
    """
    from datetime import date as _date, timedelta

    today = _date.today()
    month_start = today.replace(day=1)
    if today.month == 1:
        last_month_start = _date(today.year - 1, 12, 1)
        last_month_end = _date(today.year - 1, 12, 31)
    else:
        last_month_start = _date(today.year, today.month - 1, 1)
        last_month_end = month_start - timedelta(days=1)

    # ── AWS cost data ─────────────────────────────────────────────────────────
    this_month_total = 0.0
    last_month_total = 0.0
    top_services: list[dict] = []
    resolved_account = account_id or "unknown"

    if aws_connector is not None:
        try:
            this_summary = await aws_connector.get_costs(month_start, today)
            this_month_total = this_summary.total_usd
            if not account_id:
                accounts = await aws_connector.list_accounts()
                resolved_account = accounts[0]["id"] if accounts else "unknown"

            last_summary = await aws_connector.get_costs(last_month_start, last_month_end)
            last_month_total = last_summary.total_usd

            # Build top-5 services with month-over-month comparison
            this_by_svc = this_summary.by_service
            last_by_svc = last_summary.by_service
            all_svcs = set(this_by_svc) | set(last_by_svc)
            svc_list = sorted(all_svcs, key=lambda s: -this_by_svc.get(s, 0))
            for svc in svc_list[:5]:
                top_services.append({
                    "service": svc,
                    "this_month": this_by_svc.get(svc, 0.0),
                    "last_month": last_by_svc.get(svc, 0.0),
                })
        except Exception:
            pass  # surface what we can; missing AWS data is non-fatal

    # ── Forecast data ─────────────────────────────────────────────────────────
    projected: float | None = None
    if resolved_account != "unknown":
        try:
            from ..ml.forecasting import Forecaster
            f = Forecaster.for_account(resolved_account, days=90, aws_connector=aws_connector)
            if f._series:
                # Project full month = month-to-date actual + forecast of the
                # remaining days. The old code used the wrong dict key
                # (monthly_projection_usd, which never exists, so it always
                # rendered "n/a") and forecast only the remaining days without
                # adding MTD, under-counting the month.
                remaining_days = max(0, 30 - today.day)
                remaining_forecast = 0.0
                if remaining_days > 0:
                    pred = f.predict_dict(remaining_days)
                    remaining_forecast = pred.get("monthly_projection") or 0.0
                if this_month_total or remaining_forecast:
                    projected = round(this_month_total + remaining_forecast, 2)
        except Exception:
            pass

    # ── Savings tracker ───────────────────────────────────────────────────────
    savings_summary: dict = {
        "potential_monthly_usd": 0.0,
        "acted_on_monthly_usd": 0.0,
        "verified_monthly_usd": 0.0,
    }
    opportunities: list[dict] = []
    savings_ledger: list[dict] = []

    try:
        from ..recommendations.savings_tracker import (
            get_summary,
            list_recommendations,
            expire_stale,
        )
        expire_stale()
        savings_summary = get_summary()
        opportunities = list_recommendations(status="open", limit=20)
        # Honor the learned operating model: don't re-surface findings a human
        # already marked intentional (DR standbys, SLA-critical boxes, etc.).
        try:
            from ..recommendations.context_memory import partition
            opportunities, _ctx_suppressed = partition(opportunities)
        except Exception:
            pass
        ledger_raw = list_recommendations(status="acted_on", limit=10)
        ledger_raw += list_recommendations(status="verified", limit=10)
        savings_ledger = sorted(
            ledger_raw,
            key=lambda r: r.get("estimated_monthly_savings_usd", 0),
            reverse=True,
        )[:20]
    except Exception:
        pass

    # ── Budget status ─────────────────────────────────────────────────────────
    budgets: list[dict] = []
    try:
        from ..budget.enforcer import check_all_budgets
        budgets = check_all_budgets()
    except Exception:
        pass

    # ── Render ────────────────────────────────────────────────────────────────
    generated_at = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    html = _build_html(
        account_id=resolved_account,
        this_month=this_month_total,
        last_month=last_month_total,
        projected=projected,
        top_services=top_services,
        opportunities=opportunities,
        savings_summary=savings_summary,
        savings_ledger=savings_ledger,
        budgets=budgets,
        generated_at=generated_at,
    )

    # ── Write file ────────────────────────────────────────────────────────────
    if output_path:
        out = Path(output_path).expanduser().resolve()
    else:
        slug = resolved_account.replace("/", "-")
        filename = f"dashboard-{slug}-{today.isoformat()}.html"
        out = _dashboard_dir() / filename

    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(html, encoding="utf-8")

    # ── Summary text ──────────────────────────────────────────────────────────
    opp_count = len(opportunities)
    opp_total = sum(o.get("estimated_monthly_savings_usd", 0) for o in opportunities)
    verified = savings_summary.get("verified_monthly_usd", 0)
    delta = this_month_total - last_month_total
    sign = "+" if delta >= 0 else ""
    summary_parts = [
        f"Account {resolved_account}.",
        f"Spend this month: {_fmt_usd(this_month_total, 2)} "
        f"({sign}{_fmt_usd(delta, 2)} vs last month).",
    ]
    if opp_count:
        summary_parts.append(
            f"{opp_count} open opportunit{'y' if opp_count == 1 else 'ies'} "
            f"worth {_fmt_usd(opp_total, 2)}/mo."
        )
    if verified:
        summary_parts.append(f"Verified savings: {_fmt_usd(verified, 2)}/mo.")

    return {
        "path": str(out),
        "summary": " ".join(summary_parts),
        "account_id": resolved_account,
        "this_month_usd": round(this_month_total, 2),
        "last_month_usd": round(last_month_total, 2),
        "projected_usd": round(projected, 2) if projected else None,
        "open_opportunities": opp_count,
        "opportunity_savings_usd": round(opp_total, 2),
        "verified_savings_usd": round(verified, 2),
        "budget_count": len(budgets),
    }
