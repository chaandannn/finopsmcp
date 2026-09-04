# SPDX-License-Identifier: Apache-2.0
"""anomalies MCP tools (extracted from server.py; see finops/tools/__init__.py).

Server-local helpers, globals, and the mcp instance are reached through the live
server module (_srv.NAME) so monkeypatching finops.server.* still works and no
import-order coupling exists."""
from __future__ import annotations

from .. import server as _srv


@_srv.mcp.tool()
async def get_anomalies(
    provider: str | None = None,
    severity: str | None = None,
    limit: int = 20,
    account: str | None = None,
) -> dict:
    """
    Return active (unacknowledged) cost anomalies detected from historical baselines.

    Args:
        provider: Filter to a specific provider. None = all.
        severity: "high", "medium", or "low". None = all severities.
        limit: Max anomalies to return (default 20).
        account: Named AWS account from accounts.yaml to filter results.

    Examples:
        - "Are there any cost anomalies I should know about?"
        - "Show me high-severity cost spikes"
        - "What spiked in AWS this week?"
        - "Any anomalies in the production account?"

    Note: Anomalies require at least 7 days of snapshot history.
          Run 'finops snapshot' or wait for the daily job to accumulate data.
    """
    from ..demo_data import is_demo, get_demo_response
    if is_demo():
        return get_demo_response("get_anomalies") or {}

    from ..anomaly.detector import get_active_anomalies, has_enough_history

    # Resolve account_id filter when a named account is requested
    account_id_filter: str | None = None
    if account:
        from ..accounts import get_account, get_default_account
        acct_cfg = get_account(account) or get_default_account()
        if acct_cfg and acct_cfg.account_id:
            account_id_filter = acct_cfg.account_id

    rows = get_active_anomalies(provider=provider, severity=severity, limit=limit)
    if account_id_filter and rows:
        rows = [r for r in rows if r.get("account_id") == account_id_filter]
    if not rows:
        # An empty result means one of two very different things. With enough
        # days of snapshots behind us it is a real all-clear. On a fresh account
        # it just means we cannot judge yet, and saying "all clear" there is a
        # false reassurance. Check the actual history before we pick the message.
        if has_enough_history(provider):
            message = "No active anomalies."
        else:
            message = (
                "Not enough history yet to detect anomalies. Anomaly detection "
                "needs about 7 days of daily snapshots to build a baseline. Run "
                "daily snapshots or wait for the daily job to accumulate data."
            )
        return {
            "anomalies": [],
            "message": message,
        }

    formatted = []
    for r in rows:
        pct = abs(r["pct_change"])
        sign = "+" if r["direction"] == "spike" else "-"
        formatted.append({
            "id": r["id"],
            "provider": r["provider"],
            "service": r["service"],
            "account_id": r["account_id"],
            "severity": r["severity"],
            "direction": r["direction"],
            "change": f"{sign}{pct:.0f}%",
            "today": f"${r['current_amount']:,.2f}",
            "baseline_avg": f"${r['baseline_mean']:,.2f}",
            "z_score": r["z_score"],
            "detected": r["detected_at"],
            "snapshot_date": r["snapshot_date"],
        })

    # Apply custom alert policies (mutes, custom thresholds)
    policies = _srv._load_alert_policies()
    before_count = len(formatted)
    formatted = _srv._apply_alert_policies(formatted, policies)
    muted_count = before_count - len(formatted)

    # Spikes and drops are different events: a spike is the problem the user is
    # hunting; a drop is usually good news (a fix landed, something was cleaned up)
    # or, rarely, a sign that something stopped running. Splitting the counts keeps
    # "5 anomalies!" from meaning "1 real spike and 4 pieces of good news".
    spike_count = sum(1 for a in formatted if a.get("direction") == "spike")
    drop_count = len(formatted) - spike_count
    result: dict = {
        "count": len(formatted),
        "spikes": spike_count,
        "drops": drop_count,
        "anomalies": formatted,
        "tip": "Use acknowledge_anomaly(id) to dismiss resolved anomalies. Use set_alert_policy() to mute noisy services.",
    }
    if drop_count:
        result["drops_note"] = (
            "Drops are usually good news (a fix landed or a resource was removed). "
            "Treat a large sudden drop as a check that nothing stopped running "
            "unintentionally, not as a problem."
        )
    if muted_count > 0:
        result["muted_by_policy"] = muted_count

    # Nudge free users toward Slack alerts -- most useful next step after seeing
    # anomalies. Lead with spikes; counting good-news drops as alarm inflates the
    # pitch and reads as noise the moment the user looks at the list.
    high_spikes = sum(1 for a in formatted
                      if a.get("severity") == "high" and a.get("direction") == "spike")
    if spike_count:
        nudge_msg = (
            f"You have {spike_count} cost spike{'s' if spike_count != 1 else ''}"
            + (f" ({high_spikes} high-severity)" if high_spikes else "")
            + ". To get Slack or Teams alerts the moment these fire so you catch spikes live,"
            + " upgrade to Pro:"
        )
    else:
        nudge_msg = (
            "To get Slack or Teams alerts the moment a cost spike fires so you catch"
            " it live, upgrade to Pro:"
        )
    nudge = _srv._team_nudge(nudge_msg, context="anomalies")
    if nudge:
        result["_upgrade"] = nudge

    return result


@_srv.mcp.tool()
async def acknowledge_anomaly(anomaly_id: int) -> dict:
    """
    Mark an anomaly as acknowledged (dismissed). It will no longer appear in active anomalies.

    Args:
        anomaly_id: The ID from get_anomalies().

    Examples:
        - "Dismiss anomaly 42, it was a planned migration"
        - "Acknowledge that spike, it was expected"
    """
    if err := _srv.require_role("analyst"):
        return err

    from ..anomaly.detector import acknowledge_anomaly as _ack
    ok = _ack(anomaly_id)
    return {"acknowledged": ok, "id": anomaly_id}


@_srv.mcp.tool()
async def get_account_anomalies(days_back: int = 30) -> dict:
    """
    Detect accounts with unusual spend changes versus their prior period.
    Returns accounts that significantly spiked or dropped in cost.
    Requires a Pro plan (org_reports).

    Args:
        days_back: Look-back period to compare (default 30 vs prior 30)

    Examples:
        - "Which accounts had unusual spend changes?"
        - "Are any accounts spiking this month?"
        - "Show me account-level anomalies"
    """
    if err := _srv.require_pro("org_reports"):
        return err
    try:
        from ..connectors.aws_org import account_anomalies
        anomalies = account_anomalies(days_back=days_back)
        spikes = [a for a in anomalies if a["direction"] == "spike"]
        drops  = [a for a in anomalies if a["direction"] == "drop"]
        total_current = round(sum(a.get("current_usd", 0) for a in anomalies), 2)
        total_previous = round(sum(a.get("previous_usd", 0) for a in anomalies), 2)
        # Sort by absolute dollar swing (real money moved), most-important-first, then cap.
        ranked = sorted(
            anomalies,
            key=lambda a: abs(a.get("current_usd", 0) - a.get("previous_usd", 0)),
            reverse=True,
        )
        kept, omitted = _srv.fit_to_budget(ranked, max_tokens=6000)
        result = {
            "total_anomalies": len(anomalies),
            "spikes": len(spikes),
            "drops": len(drops),
            "total_current_usd": total_current,
            "total_previous_usd": total_previous,
            "anomalies": kept,
        }
        if omitted > 0:
            result["anomalies_truncated"] = omitted
            result["hint"] = (
                f"showing top {len(kept)} of {len(anomalies)} account anomalies by dollar "
                f"swing; query a specific account for full detail"
            )
        return result
    except Exception as e:
        return {"error": str(e)}
