# SPDX-License-Identifier: Apache-2.0
"""gcp MCP tools (extracted from server.py; see finops/tools/__init__.py).

Server-local helpers, globals, and the mcp instance are reached through the live
server module (_srv.NAME) so monkeypatching finops.server.* still works and no
import-order coupling exists."""
from __future__ import annotations

from .. import server as _srv


@_srv.mcp.tool()
async def audit_gcp_waste(
    projects: list[str] | None = None,
    checks: list[str] | None = None,
    idle_days: int = 14,
    snapshot_age_days: int = 30,
) -> dict:
    """
    Deep GCP waste audit: scans Compute Engine across all zones/regions for
    unattached persistent disks, reserved-but-idle static IPs, old snapshots, and
    idle VMs (CPU joined from Cloud Monitoring). Returns findings sorted by
    estimated monthly savings.

    Args:
        projects: GCP project IDs to scan. Defaults to GCP_PROJECT_IDS or the
                  default project on your credentials.
        checks: Subset to run: disks, ips, snapshots, idle_vms. Defaults to all.
        idle_days: Lookback window for the idle-VM CPU check (default 14).
        snapshot_age_days: Flag snapshots older than this many days (default 30).

    Examples:
        - "Run a full GCP waste audit"
        - "Find unattached GCP disks and idle static IPs"
        - "Which GCP VMs are idle this month?"
    """
    gcp = _srv._CLOUD_CONNECTORS.get("gcp")
    if gcp is None or not await gcp.is_configured():
        return {"error": "GCP is not connected. Call connect_gcp right here in the chat (it reads your gcloud login), or run 'uvx nable gcp' in a terminal."}
    try:
        from ..recommendations.gcp_waste import audit_gcp_waste as _run
        report = await _run(
            gcp,
            projects=projects,
            checks=checks,
            idle_days=idle_days,
            snapshot_age_days=snapshot_age_days,
        )
        if report.get("error"):
            return report

        # Same token-budget cap as the AWS audit: keep the highest-value findings,
        # leave the aggregates (computed over the whole list) intact. Hoist the
        # per-category why/remediation boilerplate first so the budget buys
        # findings, not the same two sentences repeated per resource.
        from ..token_budget import hoist_finding_boilerplate
        report = hoist_finding_boilerplate(report)
        all_findings = report.get("findings") or []
        if all_findings:
            kept, omitted = _srv.fit_to_budget(all_findings, max_tokens=6000)
            report["findings"] = kept
            if omitted > 0:
                report["findings_truncated"] = (
                    f"Showing top {len(kept)} of {len(all_findings)} findings by monthly "
                    f"savings. {omitted} lower-value findings omitted. Use by_category, "
                    f"by_project, and by_severity for the full breakdown, or pass checks/"
                    f"projects to narrow the scan."
                )

        monthly = report.get("total_estimated_monthly_savings", 0)
        n = report.get("total_findings", 0)
        report["summary"] = (
            f"Found {n} GCP waste finding(s) across "
            f"{len(report.get('projects_scanned', []))} project(s). "
            f"Estimated savings: ${monthly:,.2f}/mo "
            f"(${report.get('total_estimated_annual_savings', 0):,.2f}/yr)."
        )

        if monthly > 0 and n > 0:
            nudge = _srv._team_nudge(
                f"To auto-create Jira, Linear, or GitHub tickets for these {n} GCP "
                f"findings so your team actually acts on them, upgrade to Pro:"
            , context="gcp_waste")
            if nudge:
                report["_upgrade"] = nudge

        return report
    except Exception as e:
        _srv.log.error("audit_gcp_waste failed: %s", e, exc_info=True)
        return {"error": str(e)}


@_srv.mcp.tool()
async def get_gcp_recommendations(
    projects: list[str] | None = None,
    recommenders: list[str] | None = None,
) -> dict:
    """
    Pull Google's native Recommender API cost recommendations for your GCP projects.

    This is the deeper, GCP-native counterpart to audit_gcp_waste. Instead of
    scanning resources ourselves with list-price estimates, it asks Google's own
    Recommender API, which runs ML on 8+ days of real usage and prices savings
    against your actual SKU rates (including committed-use discounts already in
    effect). It covers what the scanner can't: machine-type rightsizing,
    committed-use-discount purchases, Cloud SQL idle/overprovisioned, Cloud Run
    tuning, plus idle VMs/disks/IPs/images. Findings come back sorted by monthly
    savings and wrapped in the same trust envelope (measured -> recommendation,
    inferred -> investigation).

    Needs the Recommender API enabled (recommender.googleapis.com) and the
    Recommender Viewer role (roles/recommender.viewer). Recommendations only appear
    after Google has ~8 days of usage history.

    Args:
        projects: GCP project IDs to query. Defaults to GCP_PROJECT_IDS or the
                  default project on your credentials.
        recommenders: Subset of recommender ids to run. Defaults to all cost
                  recommenders (idle VM/disk/IP/image, machine-type rightsizing,
                  Cloud SQL idle/overprovisioned, Cloud Run cost, committed-use).

    Examples:
        - "What does Google recommend to cut our GCP costs?"
        - "Any committed-use discounts worth buying on GCP?"
        - "Show GCP rightsizing recommendations from the Recommender API"
    """
    gcp = _srv._CLOUD_CONNECTORS.get("gcp")
    if gcp is None or not await gcp.is_configured():
        return {"error": "GCP is not connected. Call connect_gcp right here in the chat (it reads your gcloud login), or run 'uvx nable gcp' in a terminal."}
    try:
        from ..recommendations.gcp_recommender import get_gcp_recommendations as _run
        report = await _run(gcp, projects=projects, recommenders=recommenders)
        if report.get("error"):
            return report

        from ..token_budget import hoist_finding_boilerplate
        report = hoist_finding_boilerplate(report)
        all_findings = report.get("findings") or []
        if all_findings:
            kept, omitted = _srv.fit_to_budget(all_findings, max_tokens=6000)
            report["findings"] = kept
            if omitted > 0:
                report["findings_truncated"] = (
                    f"Showing top {len(kept)} of {len(all_findings)} recommendations by "
                    f"monthly savings. {omitted} lower-value ones omitted. Use "
                    f"by_category, by_project, and by_severity for the full breakdown, "
                    f"or pass recommenders/projects to narrow the query."
                )

        monthly = report.get("total_estimated_monthly_savings", 0)
        n = report.get("total_findings", 0)
        report["summary"] = (
            f"Google's Recommender API returned {n} cost recommendation(s) across "
            f"{len(report.get('projects_scanned', []))} project(s). "
            f"Estimated savings: ${monthly:,.2f}/mo "
            f"(${report.get('total_estimated_annual_savings', 0):,.2f}/yr)."
        )

        if monthly > 0 and n > 0:
            nudge = _srv._team_nudge(
                f"To auto-create Jira, Linear, or GitHub tickets for these {n} GCP "
                f"recommendations so your team acts on them, upgrade to Pro:"
            , context="gcp_recommendations")
            if nudge:
                report["_upgrade"] = nudge

        return report
    except Exception as e:
        _srv.log.error("get_gcp_recommendations failed: %s", e, exc_info=True)
        return {"error": str(e)}


@_srv.mcp.tool()
async def connect_gcp(billing_account_id: str = "") -> dict:
    """
    Connect a Google Cloud billing account from inside your MCP client, no terminal.

    Propose-then-confirm and local-only. It reads Google Cloud credentials that
    already exist on this machine (GOOGLE_APPLICATION_CREDENTIALS or gcloud
    Application Default Credentials), lists the open billing accounts they can
    see, and connects the one you choose. It never changes anything in GCP, and
    credentials stay on this machine.

    Call it with no arguments to see the billing accounts available (nothing is
    stored). Then call it again with billing_account_id set to connect one.

    Examples:
        - "Connect my Google Cloud billing"
        - "Use my gcloud login to connect GCP"

    Args:
        billing_account_id: The billing account to connect (XXXXXX-XXXXXX-XXXXXX),
            from the candidate list a no-argument call returns. Omit to just list.
    """
    import os as _os
    from pathlib import Path as _Path
    from ..setup_wizard import _detect_gcp_ambient, _discover_bq_export, _gcp_emit_connected
    from ..security.oauth.gcp import store_billing_accounts
    from ..security.vault import Vault
    from ..setup_scan import gcloud_adc_path

    amb = await _srv.asyncio.to_thread(_detect_gcp_ambient)
    if not amb:
        return {
            "connected": False,
            "candidates": [],
            "message": "No Google Cloud credentials were found on this machine.",
            "how_to_connect": [
                "Run: gcloud auth application-default login",
                "Or set GOOGLE_APPLICATION_CREDENTIALS to a service-account key path,",
                "then ask me to connect GCP again.",
            ],
            "note": ("connect_gcp only reads credentials that already exist locally. It never "
                     "creates or changes anything in your Google Cloud account."),
        }

    billing = amb.get("billing") or []
    project = amb.get("project") or ""

    # Credentials work but the billing accounts are not listable (missing the
    # Billing Account Viewer role). Let the user pass an id explicitly.
    if not billing and not billing_account_id:
        return {
            "connected": False,
            "credentials_found": True,
            "source": amb.get("source"),
            "message": ("Credentials work, but they cannot list billing accounts (needs the "
                        "Billing Account Viewer role). Find your billing account ID at "
                        "https://console.cloud.google.com/billing and call connect_gcp with "
                        "billing_account_id set to it."),
        }

    if not billing_account_id:
        return {
            "connected": False,
            "project": project,
            "candidates": [{"billing_account_id": bid, "name": bname} for bid, bname in billing],
            "message": (f"Found {len(billing)} open billing account(s) on these credentials. "
                        "Call connect_gcp again with billing_account_id set to the one to connect."),
            "note": "Nothing was stored. connect_gcp only reads local credentials.",
        }

    known = {bid for bid, _ in billing}
    if billing and billing_account_id not in known:
        return {
            "connected": False,
            "error": f"{billing_account_id} is not among the billing accounts these credentials can see.",
            "available": sorted(known),
        }

    # Persist the credential pointer so the MCP server resolves the same creds.
    gac = _os.environ.get("GOOGLE_APPLICATION_CREDENTIALS")
    cred_path = gac if (gac and _Path(gac).expanduser().exists()) else None
    if not cred_path:
        adc = gcloud_adc_path()
        cred_path = str(adc) if adc else None
    if cred_path:
        Vault.default().store("GCP_SERVICE_ACCOUNT_KEY_PATH", cred_path)

    bq_table = await _srv.asyncio.to_thread(_discover_bq_export, project)
    store_billing_accounts([billing_account_id], bq_table, [project] if project else None)
    auth = "ambient_env" if str(amb.get("source", "")).startswith("GOOGLE") else "ambient_adc"
    _gcp_emit_connected([billing_account_id], bq_table, auth)
    from .. import demo_data as _dd
    _dd._real_provider_cache = None
    _srv._tool_surface_changed()
    return {
        "connected": True,
        "billing_account_id": billing_account_id,
        "project": project,
        "bq_export": bq_table,
        "auth_method": auth,
        "next": "Connected. Ask me for your GCP cost summary or top cost drivers.",
        "note": ("Credentials stay on this machine. nable reads billing data only; it never "
                 "changes your Google Cloud account."),
    }


def _sku_window(start_date: str | None, end_date: str | None):
    sd, ed = _srv._default_dates()
    if start_date:
        sd = _srv.date.fromisoformat(start_date)
    if end_date:
        ed = _srv.date.fromisoformat(end_date)
    return sd, ed


@_srv.mcp.tool()
async def audit_gcp_nat_fees(
    start_date: str | None = None,
    end_date: str | None = None,
) -> dict:
    """
    Find Cloud NAT gateways whose data-processing fees dwarf their uptime charge.

    NAT bills per gigabyte processed on top of the hourly gateway charge, and
    the per-GB side is invisible in every service-level cost view. The classic
    driver is Google-API-bound traffic that Private Google Access would carry
    free. Findings are investigations with the measured fee attached: the
    avoidable share needs VPC flow logs, which nable does not read, so no
    precise savings figure is invented.

    Requires the BigQuery billing export (GCP_BQ_BILLING_TABLE).

    Args:
        start_date: ISO date (YYYY-MM-DD). Defaults to 30 days ago.
        end_date: ISO date. Defaults to today.

    Examples:
        - "Are we overpaying for Cloud NAT?"
        - "Why is NAT data processing so expensive?"
    """
    try:
        sd, ed = _sku_window(start_date, end_date)
    except ValueError:
        return {"error": "dates must be ISO format YYYY-MM-DD."}
    try:
        from ..connectors.gcp import get_sku_costs_by_project
        from ..recommendations.critique import critique
        from ..recommendations.gcp_traffic_costs import find_nat_processing_overhead

        data = await _srv.asyncio.to_thread(get_sku_costs_by_project, sd, ed)
        if data.get("error"):
            return data
        findings = find_nat_processing_overhead(data.get("rows") or [])
        reviewed = critique([f.to_dict() for f in findings], use_llm=False)
        return {
            "findings": reviewed,
            "projects_checked": len({r.get("project_id")
                                     for r in data.get("rows") or [] if r.get("project_id")}),
            "period": f"{sd} to {ed}",
            "note": ("Fees are measured from the billing export; the avoidable share "
                     "needs a flow-log check, so figures are size bands, not claims."),
        }
    except Exception as exc:
        _srv.log.error("audit_gcp_nat_fees failed: %s", exc)
        return {"error": str(exc)}


@_srv.mcp.tool()
async def audit_gcs_request_overhead(
    start_date: str | None = None,
    end_date: str | None = None,
) -> dict:
    """
    Find projects paying more for GCS operations than the stored data justifies.

    Class A/B request charges ride on top of storage cost, and when they reach a
    meaningful share of it the access pattern is filesystem-shaped: many small
    objects, list-heavy walks, gcsfuse on a hot path. Findings are
    investigations sized by the excess over a healthy operations share; normal
    usage is never counted as recoverable.

    Requires the BigQuery billing export (GCP_BQ_BILLING_TABLE).

    Args:
        start_date: ISO date (YYYY-MM-DD). Defaults to 30 days ago.
        end_date: ISO date. Defaults to today.

    Examples:
        - "Why is Cloud Storage so expensive when we store so little?"
        - "Are GCS request charges eating us?"
    """
    try:
        sd, ed = _sku_window(start_date, end_date)
    except ValueError:
        return {"error": "dates must be ISO format YYYY-MM-DD."}
    try:
        from ..connectors.gcp import get_sku_costs_by_project
        from ..recommendations.critique import critique
        from ..recommendations.gcp_traffic_costs import find_gcs_request_overhead

        data = await _srv.asyncio.to_thread(get_sku_costs_by_project, sd, ed)
        if data.get("error"):
            return data
        findings = find_gcs_request_overhead(data.get("rows") or [])
        reviewed = critique([f.to_dict() for f in findings], use_llm=False)
        return {
            "findings": reviewed,
            "projects_checked": len({r.get("project_id")
                                     for r in data.get("rows") or [] if r.get("project_id")}),
            "period": f"{sd} to {ed}",
            "note": ("Operation fees are measured; the recoverable share is the excess "
                     "over a healthy ops-to-storage ratio, reported as a size band."),
        }
    except Exception as exc:
        _srv.log.error("audit_gcs_request_overhead failed: %s", exc)
        return {"error": str(exc)}
