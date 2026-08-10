# SPDX-License-Identifier: Apache-2.0
"""meta MCP tools (extracted from server.py; see finops/tools/__init__.py).

Server-local helpers, globals, and the mcp instance are reached through the live
server module (_srv.NAME) so monkeypatching finops.server.* still works and no
import-order coupling exists."""
from __future__ import annotations

from .. import server as _srv


def _month_pair(today):
    """The two month windows a month-over-month view should compare.

    Returns (cur_start, cur_end, prev_start, prev_end, on_the_first).

    On the 1st, month-to-date is an empty range. Cost providers reject it, the
    fetch errors, and the caller's `.get("total_usd", 0.0)` turns that into a
    clean-looking $0.00 with a -100% change against last month. So one day in
    thirty, the mom and by_service views reported that every provider's spend
    had collapsed to nothing. On that day there is no month-to-date to compare,
    so compare the two months that have actually closed instead, and let the
    caller say so.
    """
    first_this = today.replace(day=1)
    last_prev = first_this - _srv.timedelta(days=1)
    first_prev = last_prev.replace(day=1)
    if today == first_this:
        last_prev2 = first_prev - _srv.timedelta(days=1)
        return first_prev, last_prev, last_prev2.replace(day=1), last_prev2, True
    return first_this, today, first_prev, last_prev, False


@_srv.mcp.tool()
async def list_connected_providers() -> dict:
    """
    List every cloud, SaaS, and LLM provider nable knows, each marked connected or
    not-configured, plus the active plan. The starting point for "what am I
    connected to" and for spotting which connector still needs credentials
    (each not-configured entry names the setup command to run).

    Examples:
        - "Which providers are connected?"
        - "Is GCP set up yet?"
        - "What plan am I on?"
    """
    from ..demo_data import is_demo, connected_providers as _demo_connected

    result: dict[str, dict] = {}

    # Demo mode: advertise the seeded provider set as connected. The live probes
    # below read real credentials, which a demo instance does not have, so without
    # this the "what am I connected to" view would show everything not-configured.
    if is_demo():
        for entry in _demo_connected():
            result[entry["name"]] = {
                "category": entry["category"],
                "configured": True,
                "status": "connected",
            }
        status = _srv.get_status()
        if status.mode == "trial":
            result["_plan"] = {"plan": "trial", "days_remaining": status.days_remaining}
        elif status.mode == "pro":
            result["_plan"] = {"plan": "pro", "email": status.email}
        else:
            result["_plan"] = {"plan": status.mode}
        return result

    for category, pool in [("cloud", _srv.CLOUD_CONNECTORS), ("saas", _srv.SAAS_CONNECTORS)]:
        for name, connector in pool.items():
            configured = await connector.is_configured()
            result[name] = {
                "category": category,
                "configured": configured,
                "status": "connected" if configured else "not connected: call connect_aws, or run 'uvx nable'",
            }

    # LLM / AI providers are module-level (not in the class registry above), so
    # surface them explicitly. This is where AI-native accounts actually spend:
    # direct model APIs, gateways (OpenRouter/LiteLLM), and GPU/inference infra.
    from ..connectors.saas import (
        openai_usage, anthropic_usage, vertex_costs, openrouter, litellm, gpu_infra,
    )
    _llm_async = {
        "openai": openai_usage.is_configured,
        "anthropic": anthropic_usage.is_configured,
        "vertex": vertex_costs.is_configured,
        "openrouter": openrouter.is_configured,
        "litellm": litellm.is_configured,
    }
    for name, check in _llm_async.items():
        try:
            configured = await check()
        except Exception:
            configured = False
        result[name] = {
            "category": "llm",
            "configured": configured,
            "status": "connected" if configured else "not connected: call connect_aws, or run 'uvx nable'",
        }
    _llm_sync = {
        "modal": gpu_infra.modal_configured,
        "together": gpu_infra.together_configured,
        "replicate": gpu_infra.replicate_configured,
    }
    for name, check in _llm_sync.items():
        configured = bool(check())
        result[name] = {
            "category": "llm",
            "configured": configured,
            "status": "connected (cost via invoice import)" if configured
                      else "not connected: call connect_aws, or run 'uvx nable'",
        }

    # Surface plan status so Claude can proactively mention upgrade when relevant
    status = _srv.get_status()
    if status.mode == "trial":
        result["_plan"] = {
            "plan": "trial",
            "days_remaining": status.days_remaining,
            "note": (
                f"Team trial active: {status.days_remaining} day{'s' if status.days_remaining != 1 else ''} remaining. "
                f"All features unlocked. Subscribe at {_srv._UPGRADE_URL} before trial ends to keep Team features."
            ),
        }
    elif status.mode == "free":
        result["_plan"] = {
            "plan": "free",
            "note": (
                f"Free tier: cost queries, anomaly detection, rightsizing, Slack/Teams alerts, "
                f"PR comments, budgets, K8s analysis, Helm visibility, and all connectors included. "
                f"Pro plan ($25/mo) adds: Slack anomaly alerts, ticket auto-creation "
                f"(Jira/Linear/GitHub), email reports, commitment recommendations, "
                f"and org rollup. Upgrade at {_srv._UPGRADE_URL}."
            ),
        }
    elif status.mode == "pro":
        result["_plan"] = {"plan": "pro", "email": status.email}

    return result


@_srv.mcp.tool()
async def check_connector_health() -> dict:
    """
    Actively test every configured connector with a real API call. Reports
    health status, last successful data time, and fix instructions for failures.

    Examples:
        - "Are all my connectors healthy?"
        - "Which connectors are broken or stale?"
        - "Why am I not getting data from Datadog?"
    """
    from ..demo_data import is_demo, connected_providers as _demo_connected
    if is_demo():
        probes = [{
            "name": e["name"], "configured": True, "healthy": True,
            "last_data": "12m ago", "response_ms": 180, "error": None, "fix": None,
        } for e in _demo_connected()]
        return {
            "summary": f"{len(probes)} healthy",
            "healthy_count": len(probes),
            "broken_count": 0,
            "unconfigured_count": 0,
            "connectors": probes,
            "broken": [],
            "tip": None,
        }

    import asyncio
    import time
    from datetime import datetime, timezone
    from sqlalchemy import select, func, text as sql_text
    from ..storage.db import get_engine, cost_snapshots

    # Get last-seen data per provider from DB
    last_seen: dict[str, str] = {}
    try:
        engine = get_engine()
        with engine.connect() as conn:
            rows = conn.execute(
                select(
                    cost_snapshots.c.provider,
                    func.max(cost_snapshots.c.captured_at).label("last_at"),
                ).group_by(cost_snapshots.c.provider)
            ).fetchall()
            for r in rows:
                last_seen[r.provider] = r.last_at.isoformat() if r.last_at else None
    except Exception:
        pass

    def _age_label(ts: str | None) -> str:
        if not ts:
            return "never"
        try:
            dt = datetime.fromisoformat(ts)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            delta = datetime.now(timezone.utc) - dt
            h = delta.total_seconds() / 3600
            if h < 2:
                return f"{int(delta.total_seconds() / 60)}m ago"
            if h < 48:
                return f"{int(h)}h ago"
            return f"{int(h / 24)}d ago"
        except Exception:
            return "unknown"

    async def _probe(name: str, connector) -> dict:
        t0 = time.monotonic()
        result: dict = {"name": name, "configured": False, "healthy": False,
                        "last_data": _age_label(last_seen.get(name)),
                        "response_ms": None, "error": None, "fix": None}
        try:
            result["configured"] = await connector.is_configured()
            if not result["configured"]:
                result["fix"] = f"Run: finops setup {name}"
                return result

            # Minimal live probe, list_accounts is the lightest call on every connector
            await asyncio.wait_for(connector.list_accounts(), timeout=10.0)
            result["healthy"] = True
            result["response_ms"] = int((time.monotonic() - t0) * 1000)
        except asyncio.TimeoutError:
            result["error"] = "Timeout (>10s), credentials may be valid but API is slow"
            result["fix"] = "Check network connectivity or API endpoint status"
        except Exception as e:
            msg = str(e)
            result["error"] = msg[:200]
            # Map common errors to actionable fixes
            if any(k in msg.lower() for k in ("expired", "token", "refresh")):
                result["fix"] = f"Credentials expired. Run: finops setup {name}"
            elif any(k in msg.lower() for k in ("access denied", "unauthorized", "403", "401")):
                result["fix"] = f"Permission denied. Re-authorize: finops setup {name}"
            elif any(k in msg.lower() for k in ("not found", "404")):
                result["fix"] = f"Resource not found. Re-configure: finops setup {name}"
            elif any(k in msg.lower() for k in ("rate limit", "throttl", "429")):
                result["fix"] = "Rate limited, nable will auto-retry. No action needed."
            else:
                result["fix"] = f"Re-run setup: finops setup {name}"
        return result

    # Run all probes in parallel (don't await serially, would take minutes)
    tasks = [_probe(name, conn) for name, conn in _srv._ALL_CONNECTORS.items()]
    probes = await asyncio.gather(*tasks, return_exceptions=False)

    healthy = [p for p in probes if p["healthy"]]
    broken = [p for p in probes if p["configured"] and not p["healthy"]]
    unconfigured = [p for p in probes if not p["configured"]]
    stale = [p for p in probes if p["healthy"] and p["last_data"] not in ("never",) and
             any(x in p["last_data"] for x in ("d ago",)) and
             int(p["last_data"].split("d")[0]) > 2]

    summary_parts = []
    if healthy:
        summary_parts.append(f"{len(healthy)} healthy")
    if broken:
        summary_parts.append(f"{len(broken)} broken")
    if unconfigured:
        summary_parts.append(f"{len(unconfigured)} not configured")
    if stale:
        summary_parts.append(f"{len(stale)} stale (>2 days since last data)")

    return {
        "summary": ", ".join(summary_parts) or "No connectors found",
        "healthy_count": len(healthy),
        "broken_count": len(broken),
        "unconfigured_count": len(unconfigured),
        "connectors": sorted(probes, key=lambda p: (p["healthy"], not p["configured"])),
        "broken": [{"name": p["name"], "error": p["error"], "fix": p["fix"]} for p in broken],
        "tip": "Run 'finops-doctor' for a full credential and permission audit." if broken else None,
    }


@_srv.mcp.tool()
async def compare_providers(
    category: str | None = None,
    start_date: str | None = None,
    end_date: str | None = None,
) -> dict:
    """
    Side-by-side cost comparison across all configured providers.

    Args:
        category: "cloud" or "saas". None = all.
        start_date: ISO date. Defaults to 30 days ago.
        end_date: ISO date. Defaults to today.

    Examples:
        - "Which cloud are we spending the most on?"
        - "Compare our SaaS tool spending"
        - "How does AWS compare to Azure and GCP?"
    """
    from ..demo_data import (
        is_demo, _PROVIDER_SERVICES, _DEMO_PROVIDERS, _DEMO_PROVIDER_CATEGORY,
    )
    if is_demo():
        want = None
        if category == "cloud":
            want = {"cloud"}
        elif category == "saas":
            want = {"saas", "llm"}
        provs = [p for p in _DEMO_PROVIDERS
                 if want is None or _DEMO_PROVIDER_CATEGORY.get(p) in want]
        rows: list[dict] = []
        grand = 0.0
        for p in provs:
            svcs = _PROVIDER_SERVICES[p]
            total = round(sum(s["amount"] for s in svcs), 2)
            grand += total
            rows.append({
                "provider": p,
                "category": _DEMO_PROVIDER_CATEGORY.get(p, "cloud"),
                "total_usd": total,
                "total_formatted": _srv._fmt_usd(total),
                "top_services": [
                    {"service": s["service"], "amount_usd": round(s["amount"], 2)}
                    for s in sorted(svcs, key=lambda x: -x["amount"])[:5]
                ],
            })
        for r in rows:
            r["pct_of_total"] = round(r["total_usd"] / grand * 100, 1) if grand else 0
        rows.sort(key=lambda x: -x["total_usd"])
        return {
            "period": {"start": _srv._default_dates()[0].isoformat(),
                       "end": _srv._default_dates()[1].isoformat()},
            "grand_total_usd": round(grand, 2),
            "grand_total_formatted": _srv._fmt_usd(grand),
            "providers": rows,
        }

    sd, ed = _srv._default_dates()
    if start_date:
        sd = _srv.date.fromisoformat(start_date)
    if end_date:
        ed = _srv.date.fromisoformat(end_date)

    pool = _srv.CLOUD_CONNECTORS if category == "cloud" else _srv.SAAS_CONNECTORS if category == "saas" else _srv._ALL_CONNECTORS
    targets = await _srv._active(pool)
    if not targets:
        return {"error": "No cloud accounts connected yet. Connect one right here in the chat: call connect_aws or connect_gcp (they detect credentials already on this machine) or connect_azure. No terminal, no restart. Prefer a guided terminal setup? Run 'uvx nable' instead."}

    provider_totals: list[dict] = []
    grand_total = 0.0

    async def _one_total(name: str, connector: _srv.Any):
        try:
            return name, await _srv._fetch_costs_cached(name, connector, sd, ed), None
        except Exception as exc:
            return name, None, str(exc)

    for name, summary, err in await _srv.asyncio.gather(*[_one_total(n, c) for n, c in targets.items()]):
        if err is not None:
            provider_totals.append({"provider": name, "error": err})
            continue
        provider_totals.append({
            "provider": name,
            "category": "cloud" if name in _srv.CLOUD_CONNECTORS else "saas",
            "total_usd": round(summary.total_usd, 4),
            "total_formatted": _srv._fmt_usd(summary.total_usd),
            "top_services": [
                {"service": k, "amount_usd": round(v, 4)}
                for k, v in sorted(summary.by_service.items(), key=lambda x: -x[1])[:5]
            ],
        })
        grand_total += summary.total_usd

    for p in provider_totals:
        if "total_usd" in p:
            p["pct_of_total"] = round(p["total_usd"] / grand_total * 100, 1) if grand_total else 0

    provider_totals.sort(key=lambda x: -x.get("total_usd", 0))

    return {
        "period": {"start": sd.isoformat(), "end": ed.isoformat()},
        "grand_total_usd": round(grand_total, 4),
        "grand_total_formatted": _srv._fmt_usd(grand_total),
        "providers": provider_totals,
    }


@_srv.mcp.tool()
async def list_accounts(provider: str | None = None) -> dict:
    """
    List all cloud accounts, subscriptions, and SaaS org IDs that nable can see,
    grouped by provider: AWS account ids, Azure subscriptions, GCP billing
    accounts, and each SaaS provider's org. Use it to find the account id or
    name other tools accept as their account argument.

    Args:
        provider: Limit to one provider (e.g. "aws"). None = all providers.

    Examples:
        - "What accounts is nable connected to?"
        - "List my Azure subscriptions"
    """
    from ..demo_data import is_demo, demo_accounts as _demo_accounts
    if is_demo():
        accts = _demo_accounts()
        return {provider: accts[provider]} if provider and provider in accts else accts

    pool = {provider: _srv._ALL_CONNECTORS[provider]} if provider and provider in _srv._ALL_CONNECTORS else _srv._ALL_CONNECTORS
    targets = await _srv._active(pool)
    async def _one_accts(name: str, connector: _srv.Any):
        try:
            return name, await connector.list_accounts()
        except Exception as exc:
            return name, [{"error": str(exc)}]

    pairs = await _srv.asyncio.gather(*[_one_accts(n, c) for n, c in targets.items()])
    return dict(pairs)


@_srv.mcp.tool()
async def set_alert_policy(
    provider: str = "*",
    service_pattern: str = "*",
    muted: bool = False,
    min_pct_change: float | None = None,
    min_usd_change: float | None = None,
    note: str = "",
) -> dict:
    """
    Set a custom alert policy for anomaly detection on a specific provider or service.

    Use this to:
    - Mute noisy services you don't care about (e.g. DataTransfer, Tax)
    - Raise the threshold for services that are naturally volatile
    - Set a minimum $ delta to ignore tiny fluctuations

    Supports glob patterns: "DataTransfer*", "*Transfer*", "EC2*"

    Args:
        provider: "aws", "azure", "gcp", or "*" for all providers
        service_pattern: Exact service name or glob pattern (e.g. "DataTransfer*", "*")
        muted: If True, all anomalies matching this rule are silenced
        min_pct_change: Only alert if change exceeds this % (overrides default 20%)
        min_usd_change: Only alert if absolute change exceeds this $ amount
        note: Why this policy exists (shown in list_alert_policies)

    Examples:
        - "Mute DataTransfer anomalies, they're always noisy"
        - "Only alert on EC2 if it changes by more than 40%"
        - "Ignore AWS Tax service anomalies"
        - "Only alert on changes over $500, ignore tiny fluctuations"
        - "Set a 50% threshold for Support charges"
    """
    if (err := _srv.require_pro("alerts")):
        return err
    if err := _srv.require_role("analyst"):
        return err
    try:
        from ..storage.db import get_engine, alert_policies as _ap_table
        from sqlalchemy import select, delete
        from datetime import datetime
        engine = get_engine()
        with engine.begin() as conn:
            # Delete existing policy for same provider+service
            conn.execute(
                _ap_table.delete().where(
                    _ap_table.c.provider == provider,
                    _ap_table.c.service_pattern == service_pattern,
                )
            )
            conn.execute(
                _ap_table.insert().values(
                    provider=provider,
                    service_pattern=service_pattern,
                    muted=muted,
                    min_pct_change=min_pct_change,
                    min_usd_change=min_usd_change,
                    note=note or None,
                    created_at=datetime.utcnow(),
                )
            )
        action = "muted" if muted else f"threshold set to {min_pct_change or 20}%"
        return {
            "created": True,
            "provider": provider,
            "service_pattern": service_pattern,
            "action": action,
            "message": f"Alert policy saved: {provider}/{service_pattern} → {action}",
        }
    except Exception as e:
        return {"error": str(e)}


@_srv.mcp.tool()
async def list_alert_policies() -> dict:
    """
    List all custom alert policies for anomaly detection.

    Shows which services are muted, which have custom thresholds, and why.

    Examples:
        - "What alert policies do I have?"
        - "Which services are muted from anomaly detection?"
        - "Show my alert thresholds"
    """
    policies = _srv._load_alert_policies()
    if not policies:
        return {
            "policies": [],
            "message": "No custom alert policies set. All services use the default 20% / z=2.0 threshold.",
        }
    formatted = []
    for p in policies:
        desc_parts = []
        if p["muted"]:
            desc_parts.append("MUTED")
        if p.get("min_pct_change"):
            desc_parts.append(f"min {p['min_pct_change']:.0f}% change to alert")
        if p.get("min_usd_change"):
            desc_parts.append(f"min ${p['min_usd_change']:,.0f} delta to alert")
        formatted.append({
            "id": p["id"],
            "provider": p["provider"],
            "service_pattern": p["service_pattern"],
            "description": " · ".join(desc_parts) or "no filter",
            "note": p.get("note"),
            "created_at": p["created_at"].isoformat() if p.get("created_at") else None,
        })
    return {
        "count": len(formatted),
        "policies": formatted,
        "tip": "Use set_alert_policy() to add or update a policy. Use delete_alert_policy(id) to remove one.",
    }


@_srv.mcp.tool()
async def delete_alert_policy(policy_id: int) -> dict:
    """
    Remove a custom alert policy. The service will revert to the default threshold.

    Args:
        policy_id: The ID from list_alert_policies()

    Examples:
        - "Delete alert policy 3"
        - "Remove the mute on DataTransfer"
    """
    if err := _srv.require_role("analyst"):
        return err
    try:
        from ..storage.db import get_engine, alert_policies as _ap_table
        engine = get_engine()
        with engine.begin() as conn:
            r = conn.execute(
                _ap_table.delete().where(_ap_table.c.id == policy_id)
            )
        if r.rowcount == 0:
            return {"error": f"Policy {policy_id} not found. Use list_alert_policies() to see IDs."}
        return {"deleted": True, "policy_id": policy_id}
    except Exception as e:
        return {"error": str(e)}


@_srv.mcp.tool()
async def list_vault_credentials() -> dict:
    """
    List the names of credentials stored in the encrypted vault (never the values).

    Examples:
        - "What credentials are stored in the vault?"
        - "Which providers have been configured via setup?"
    """
    try:
        from ..security.vault import Vault
        vault = Vault.default()
        keys = [k for k in vault.list_keys() if not k.startswith("_")]  # hide internal keys
        return {
            "count": len(keys),
            "credentials": keys,
            "note": "Values are never exposed. Connect in-chat (connect_aws / connect_gcp) or run 'uvx nable' to add or update credentials.",
        }
    except Exception as e:
        return {"error": str(e)}


@_srv.mcp.tool()
async def list_savings_recommendations(
    status: str | None = None,
    source: str | None = None,
    limit: int = 30,
) -> dict:
    """
    List tracked recommendations with their current status.

    Args:
        status: Filter by status: "open", "acted_on", "verified", "dismissed", "expired". None = all.
        source: Filter by source: "rightsizing", "idle", "kubernetes", "waste", "commitment". None = all.
        limit: Max results (default 30).

    Examples:
        - "Show all open recommendations"
        - "Which recommendations have we acted on?"
        - "List verified savings"
        - "Show dismissed recommendations"
    """
    from ..recommendations.savings_tracker import list_recommendations
    recs = list_recommendations(status=status, source=source, limit=limit)

    if not recs:
        msg = f"No {status or ''} recommendations found.".strip()
        return {"recommendations": [], "message": msg}

    total_potential = sum(r["estimated_monthly_savings_usd"] for r in recs if r["status"] == "open")
    total_verified = sum(r["verified_monthly_savings_usd"] or 0 for r in recs if r["status"] == "verified")

    out = {
        "count": len(recs),
        "recommendations": recs,
        "open_potential_usd": round(total_potential, 2),
        "verified_savings_usd": round(total_verified, 2),
    }
    # Learning loop: on the actionable (open) view, rank + suppress per what this
    # customer actually acts on. Propose-only; a no-op until the ledger has signal.
    if status in (None, "open"):
        # Critique before ranking: a claim that did not survive review must not be
        # ranked on the figure it just lost, and must not be counted in the
        # headline total we are asking someone to believe. Deterministic only by
        # default, so this costs nothing and never leaves the machine.
        try:
            from ..recommendations.critique import critique
            recs = critique(recs, savings_key="estimated_monthly_savings_usd")
            out["recommendations"] = recs
            retracted = [r for r in recs if not (r.get("critique") or {}).get("survived", True)]
            if retracted:
                out["open_potential_usd"] = round(
                    sum(r.get("estimated_monthly_savings_usd") or 0
                        for r in recs if r["status"] == "open"), 2)
                out["retracted_count"] = len(retracted)
                out["critique_note"] = (
                    f"{len(retracted)} recommendation(s) did not survive review and are shown "
                    "as investigations with a size band instead of a dollar figure. "
                    "Their `critique.objections` say why.")
        except Exception as exc:
            _srv.log.debug("critique skipped in list_savings_recommendations: %s", exc)

        try:
            from ..recommendations.learning import customer_signal, rescore
            sig = customer_signal()
            rs = rescore(recs, sig, savings_key="estimated_monthly_savings_usd", source_key="source")
            out["recommendations"] = rs["ranked"]
            if rs["suppressed_count"]:
                out["suppressed_for_you"] = rs["suppressed_for_you"]
                out["suppressed_count"] = rs["suppressed_count"]
            # Context memory: findings a human already marked intentional. Held out of
            # the ranked list so nable stops re-flagging them; shown separately with why.
            ctx_suppressed = rs.get("suppressed_by_context") or []
            if ctx_suppressed:
                out["suppressed_by_context"] = ctx_suppressed
                out["suppressed_by_context_count"] = len(ctx_suppressed)
                out["open_potential_usd"] = round(
                    # `or 0`: a critiqued-down recommendation carries None here, and
                    # summing it raises. It is also correctly excluded from a total
                    # we are presenting as real money.
                    sum(r.get("estimated_monthly_savings_usd") or 0 for r in rs["ranked"]
                        if r["status"] == "open"), 2)
            if any(s.get("coverage") != "COLD" for s in sig.get("by_source", [])):
                out["learning_note"] = ("Ranked for you from which recommendation types you act on. "
                                        "Call get_recommendation_learning() for the why.")
        except Exception as exc:
            _srv.log.debug("learning rescore skipped in list_savings_recommendations: %s", exc)
    return out


@_srv.mcp.tool()
async def list_profiles() -> str:
    """
    List all configured nable profiles (for multi-account or multi-client setups).

    Profiles allow engineers who manage multiple accounts to switch context
    cleanly. Each profile has its own database and credential namespace.

    Use when:
        - "What profiles do I have configured?"
        - "Show me my nable profiles"
        - "Which profile is active?"
    Examples:
        - "List my nable profiles"
        - "Which cost profiles are configured?"

    """
    from pathlib import Path

    profiles_dir = Path.home() / ".finops" / "profiles"
    active = _srv.os.environ.get("FINOPS_PROFILE", "").strip()

    if not profiles_dir.exists():
        lines = [
            "No profiles configured.",
            "",
            "Profiles let you manage separate contexts (e.g. different clients or AWS orgs).",
            "Each profile has its own database and credential namespace.",
            "",
            "Create a profile:  finops profile create <name>",
            "Activate:          export FINOPS_PROFILE=<name>",
        ]
        return "\n".join(lines)

    profile_dirs = sorted(p for p in profiles_dir.iterdir() if p.is_dir())

    if not profile_dirs:
        lines = [
            "No profiles found in ~/.finops/profiles/.",
            "",
            "Create one with: finops profile create <name>",
        ]
        return "\n".join(lines)

    lines = ["## nable Profiles", ""]

    for p in profile_dirs:
        marker = "(active)" if p.name == active else ""
        db_path = p / "finops.db"
        vault_path = p / "vault.db"
        db_note = "db exists" if db_path.exists() else "no db yet"
        vault_note = ", vault exists" if vault_path.exists() else ""
        lines.append(f"  {p.name:<20} {marker:<8} [{db_note}{vault_note}]")

    lines.append("")
    if active:
        lines.append(f"Active profile: {active} (FINOPS_PROFILE env var)")
    else:
        lines.append("Active profile: default (no FINOPS_PROFILE set)")

    lines += [
        "",
        "Switch profile:  export FINOPS_PROFILE=<name>",
        "Create profile:  finops profile create <name>",
        "List profiles:   finops profile list",
    ]

    return "\n".join(lines)


@_srv.mcp.tool()
def create_api_key(
    name: str,
    role: str = "viewer",
    email: str = "",
    scope_team: str | None = None,
    scope_provider: str | None = None,
) -> dict:
    """
    Create a new API key for a team member. Requires admin role in shared mode.

    Roles:
      viewer  , read-only cost queries, optionally scoped to one team/provider
      analyst , viewer + attribution writes, budget management, snapshot triggers
      admin   , full access, can manage keys and connectors

    The raw key (nbl_...) is shown ONCE, it is not stored. Save it immediately.

    Examples:
        - "Create a viewer key for Alice scoped to the platform team"
        - "Give Bob an analyst key"
        - "Create an admin key for the CI system"
    Args:
        name: Human-readable key name (e.g. "ci-reporter").
        role: "viewer", "analyst", or "admin".
        email: Owner email recorded for audit.
        scope_team: Restrict the key to one team's data.
        scope_provider: Restrict the key to one provider.

    """
    if err := _srv.require_role("admin"):
        return err
    result = _srv.create_key(
        name=name, role=role, email=email,
        scope_team=scope_team, scope_provider=scope_provider,
        created_by=_srv.current_identity().name if _srv.current_identity() else "admin",
    )
    _srv.audit("key_create", name, f"role={role} scope_team={scope_team}")
    return result


@_srv.mcp.tool()
def list_api_keys() -> list[dict]:
    """
    List all active API keys (names, roles, scopes). Raw keys are never shown.
    Requires admin role in shared mode.

    Examples:
        - "Who has access to finops?"
        - "List all API keys"
        - "Show team member access levels"
    """
    if err := _srv.require_role("admin"):
        return [err]
    return _srv.list_keys()


@_srv.mcp.tool()
def revoke_api_key(key_id: int) -> dict:
    """
    Revoke an API key by ID. The key is soft-deleted, it stops working immediately.
    Requires admin role. Use list_api_keys to find the key ID first.

    Examples:
        - "Revoke Alice's key"
        - "Remove access for key ID 3"
    Args:
        key_id: The key id from list_api_keys().

    """
    if err := _srv.require_role("admin"):
        return err
    ok = _srv.revoke_key(key_id)
    if ok:
        _srv.audit("key_revoke", f"id={key_id}", None)
    return {"revoked": ok, "key_id": key_id}


@_srv.mcp.tool()
def whoami() -> dict:
    """
    Show the current identity and access level. Works in both permissive and
    shared auth mode.

    Examples:
        - "Who am I logged in as?"
        - "What's my role?"
        - "Do I have analyst access?"
    """
    from ..persona import get_persona, PERSONAS
    current_persona = get_persona()
    persona_label = PERSONAS[current_persona]["label"]

    ident = _srv.current_identity()
    if ident is None:
        from ..storage.db import storage_mode
        mode = storage_mode()
        return {
            "mode": "permissive",
            "role": "admin",
            "note": (
                "Running in single-user mode. No authentication required. "
                "Set FINOPS_REQUIRE_AUTH=1 and issue API keys to enforce RBAC."
            ),
            "storage": mode,
            "persona": current_persona,
            "persona_label": persona_label,
        }
    return {
        "mode": "authenticated",
        **ident.as_dict(),
        "persona": current_persona,
        "persona_label": persona_label,
    }


@_srv.mcp.tool()
async def get_ai_budget_status() -> dict:
    """Where your AI coding agent stands against its budget, right now.

    Reads your agent's real token usage locally (Claude Code session logs) and reports
    this rolling window, month-to-date, your budget, burn rate, cost per 1M tokens, and
    a verdict (ok / warn / over). Nothing leaves your machine. It does NOT claim a
    percentage of a Claude/Cursor plan's hidden rate limit (no API exposes that); it
    reports your real usage against the budget you set with set_ai_budget. Token counts
    are exact; any dollar figure is a list-price estimate, never presented as your bill."""
    from ..ai_budget import status
    return status()


@_srv.mcp.tool()
async def check_ai_budget(estimated_next_tokens: int = 0) -> dict:
    """Advisory gate: before a big task, is the agent about to blow its AI budget?

    Call this before an expensive run. Returns a verdict (ok / warn / over), the
    reason, and a recommendation. Advice only, it never blocks; relay the verdict and
    let the human decide. Pass estimated_next_tokens to test whether the next task
    would tip a token budget over."""
    from ..ai_budget import check
    return check(estimated_next_tokens=int(estimated_next_tokens or 0))


@_srv.mcp.tool()
async def set_ai_budget(mode: str | None = None,
                        plan_cost: float | None = None,
                        spend_cap: float | None = None,
                        monthly_tokens: int | None = None,
                        plan_label: str | None = None) -> dict:
    """Set the coding agent's monthly AI budget. Two lenses:

    - Flat subscription (Claude Pro/Max, Cursor): pass plan_cost = what you pay per
      month (any number). The verdict is about usage, not a dollar overage: a flat
      fee is what you actually pay, so the agent tracks how much subsidized compute
      it is pulling and, if you add monthly_tokens, warns before you run low.
    - Metered API / enterprise: pass spend_cap = your monthly dollar cap. The agent
      gates on estimated spend against it, and reports cost per 1M tokens.

    monthly_tokens is an optional usage cap for either lens. Ask the human which
    lens fits and what they pay rather than assuming. Stored locally in ~/.nable,
    nothing uploaded."""
    from ..ai_budget import set_budget
    return {"budget": set_budget(mode=mode, plan_cost=plan_cost, spend_cap=spend_cap,
                                 monthly_tokens=monthly_tokens, plan_label=plan_label),
            "note": "Saved. Call get_ai_budget_status to see where you stand."}


@_srv.mcp.tool()
async def check_action_policy(
    action_type: str,
    terraform_plan_json: str | None = None,
    terraform_plan_file: str | None = None,
    tf_dir: str | None = None,
    helm_diff: str | None = None,
    monthly_delta_usd: float | None = None,
    budget_name: str = "",
) -> dict:
    """Advisory policy gate: should a proposed remediation action proceed?

    The request-path guardrail, advisory. Describe a remediation action you are
    considering (action_type), optionally with the change to cost (a Terraform plan,
    a helm diff, or a known monthly delta), and nable returns a machine verdict
    against your human-authored policy:
      - allow:    reversible, allowlisted, and within budget. A human can apply it.
      - escalate: a one-way door (delete, terminate, buy a commitment) or an
                  over-budget / large-cost change. A human must review it first.
      - block:    the action type is not in your allowlist.

    ADVICE ONLY. nable never applies the action, a human does. This is the
    propose-only guardrail; nable does not auto-execute anything.

    action_type examples: rightsizing, tag_fix, stop_idle, spot_migration, ticket
    (reversible); idle_cleanup, purchase_commitment, terminate_instance, delete_resource
    (one-way). Policy knobs via env: FINOPS_POLICY_MAX_AUTO_USD,
    FINOPS_POLICY_ALLOWED_ACTIONS (comma-separated). Read-only.

    Good triggers: "can the agent do X", "is this action within policy", "should I
    apply this fix", "is it safe to auto-apply this".
    Args:
        action_type: The infra action being attempted (e.g. "terraform_apply").
        terraform_plan_json: Terraform plan JSON string to evaluate.
        terraform_plan_file: Path to a terraform plan JSON file.
        tf_dir: Terraform directory to plan and evaluate.
        helm_diff: Helm diff text to evaluate instead of terraform.
        monthly_delta_usd: Known monthly delta, when you already have the number.
        budget_name: Budget to evaluate the action against.

    Examples:
        - "Is this apply within policy?"
        - "Check this change against our cost guardrails"

    """
    # Free, permanently. This is the same capability as the `nable guard` hook,
    # reached through MCP instead of a shell hook, and gating one surface while
    # the other is open would be incoherent. See the note on "agent_gate" in
    # license.py: stopping your own agent is the front door, not the upsell.
    from ..policy import evaluate_action_gate, load_policy

    cost = None
    if any([terraform_plan_json, terraform_plan_file, tf_dir, helm_diff,
            monthly_delta_usd is not None]):
        cost = await _srv.estimate_change_cost(
            terraform_plan_json=terraform_plan_json, terraform_plan_file=terraform_plan_file,
            tf_dir=tf_dir, helm_diff=helm_diff, monthly_delta_usd=monthly_delta_usd,
            budget_name=budget_name)
        if isinstance(cost, dict) and cost.get("error"):
            return cost

    from ..agent_controls import suggest_cheaper_path, remediation_status, data_age_hours

    delta = (cost or {}).get("monthly_delta_usd", 0.0)
    verdict = (cost or {}).get("verdict")
    # Learning: fold this customer's decision history for this action type into the
    # gate. Caution-only ratchet (see policy._apply_learning). Deterministic math over
    # the local ledger, no LLM loop, so it stays cheap and propose-only. Best-effort:
    # any failure leaves the static gate untouched.
    learn_signal = None
    try:
        from ..recommendations.learning import customer_signal
        from ..recommendations.learning.signal import signal_for
        learn_signal = signal_for(customer_signal(), action_type)
    except Exception:
        learn_signal = None
    gate = evaluate_action_gate(action_type, delta, verdict, policy=load_policy(), signal=learn_signal)
    if cost is not None:
        # Label the budget verdict with the age of the cost data it rests on, so the
        # agent knows how fresh the "over budget" call is. Cached by design: no live
        # Cost Explorer call on this request path. Best-effort, never fails the gate.
        b = cost.get("budget")
        if isinstance(b, dict):
            try:
                from ..storage.snapshots import latest_captured_at
                as_of = latest_captured_at()
            except Exception:
                as_of = None
            b["as_of"] = as_of
            b["age_hours"] = data_age_hours(as_of)
        gate["cost"] = cost
        cheaper = suggest_cheaper_path(cost.get("breakdown"), delta)
        if cheaper:
            gate["cheaper_path"] = cheaper
    gate["remediation"] = remediation_status()
    gate["policy_note"] = ("Advisory only. nable proposes, a human approves and applies. "
                           "It never executes actions in your environment on its own.")
    return gate


@_srv.mcp.tool()
async def list_views() -> dict:
    """
    List all pre-built cost views available to your team.

    These are ready-to-run reports anyone on the team can call by name using get_view().
    Useful to paste into a Claude Project system prompt so teammates know what is available.

    Examples:
        - "What views are available?"
        - "Show me the list of shared cost reports"
    """
    return {
        "views": [
            {"id": k, "name": v["name"], "description": v["description"]}
            for k, v in _srv._VIEWS.items()
        ],
        "usage": (
            "Call get_view(view='<id>') to run any view. "
            "Some views accept extra args: by_tag needs tag_key, "
            "dod/daily_trend accept a days parameter."
        ),
        "tip": (
            "Add 'Use list_views() to show available cost reports' to your Claude Project "
            "instructions so every teammate knows what to ask for."
        ),
    }


@_srv.mcp.tool()
async def get_view(
    view: str,
    tag_key: str | None = None,
    tag_value: str | None = None,
    provider: str | None = None,
    days: int | None = None,
) -> dict:
    """
    Run a pre-built cost view by name. These are standard reports your whole team can share.

    Args:
        view:      View ID from list_views(). Required.
        tag_key:   Tag key to group by (required for 'by_tag' view, e.g. 'team', 'env').
        tag_value: Optional filter to a single tag value within by_tag.
        provider:  Optional provider filter (aws, azure, gcp, datadog, etc.).
        days:      Override the default lookback window for time-series views.

    Examples:
        - "Show me the month over month view"
        - "Run the by_tag view for the team tag"
        - "Get the anomalies view for AWS"
        - "What does the top_spenders view show?"
        - "Run daily_trend for the last 7 days"

    Tip: Share these view names in your team's Slack or Claude Project so everyone
         runs the same report instead of writing queries from scratch each time.
    """
    if view not in _srv._VIEWS:
        return {
            "error": f"Unknown view '{view}'.",
            "available_views": list(_srv._VIEWS.keys()),
            "tip": "Call list_views() to see all available views with descriptions.",
        }

    meta = _srv._VIEWS[view]
    today = _srv.date.today()

    # ── mom ──────────────────────────────────────────────────────────────────
    if view == "mom":
        first_this, this_end, first_last, last_last, on_the_first = _month_pair(today)

        active = await _srv._active()
        if provider:
            active = {k: v for k, v in active.items() if k == provider}

        this_total, this_by, _  = await _srv._gather_costs(active, first_this, this_end)
        last_total, last_by, _  = await _srv._gather_costs(active, first_last, last_last)

        rows = []
        all_providers = sorted(set(list(this_by.keys()) + list(last_by.keys())))
        for p in all_providers:
            t = this_by.get(p, {}).get("total_usd", 0.0)
            l = last_by.get(p, {}).get("total_usd", 0.0)
            pct = ((t - l) / l * 100) if l else None
            rows.append({
                "provider": p,
                "this_month": _srv._fmt_usd(t),
                "last_month": _srv._fmt_usd(l),
                "change_pct": f"{pct:+.1f}%" if pct is not None else "n/a",
            })

        total_pct = ((this_total - last_total) / last_total * 100) if last_total else None
        return {
            "view": meta["name"],
            "this_month": {"period": f"{first_this} to {this_end}", "total": _srv._fmt_usd(this_total)},
            "last_month": {"period": f"{first_last} to {last_last}", "total": _srv._fmt_usd(last_total)},
            "total_change": f"{total_pct:+.1f}%" if total_pct is not None else "n/a",
            "by_provider": rows,
            # The keys stay this_month/last_month for consumers, so on the 1st say
            # plainly what they hold: the two closed months, not a $0 current one.
            **({"note": "It is the 1st, so there is no month-to-date yet. "
                        "'this_month' is the month that just closed and 'last_month' "
                        "is the one before it."} if on_the_first else {}),
        }

    # ── wow ──────────────────────────────────────────────────────────────────
    if view == "wow":
        this_start = today - _srv.timedelta(days=7)
        last_start = today - _srv.timedelta(days=14)
        last_end   = today - _srv.timedelta(days=8)

        active = await _srv._active()
        if provider:
            active = {k: v for k, v in active.items() if k == provider}

        this_total, this_by, _  = await _srv._gather_costs(active, this_start, today, granularity="DAILY")
        last_total, last_by, _  = await _srv._gather_costs(active, last_start, last_end, granularity="DAILY")

        pct = ((this_total - last_total) / last_total * 100) if last_total else None
        return {
            "view": meta["name"],
            "this_week": {"period": f"{this_start} to {today}", "total": _srv._fmt_usd(this_total)},
            "last_week": {"period": f"{last_start} to {last_end}", "total": _srv._fmt_usd(last_total)},
            "change_pct": f"{pct:+.1f}%" if pct is not None else "n/a",
            "by_provider": [
                {
                    "provider": p,
                    "this_week": _srv._fmt_usd(this_by.get(p, {}).get("total_usd", 0)),
                    "last_week": _srv._fmt_usd(last_by.get(p, {}).get("total_usd", 0)),
                }
                for p in sorted(set(list(this_by.keys()) + list(last_by.keys())))
            ],
        }

    # ── dod / daily_trend ────────────────────────────────────────────────────
    if view in ("dod", "daily_trend"):
        n = days or (14 if view == "dod" else 30)
        start = today - _srv.timedelta(days=n)

        active = await _srv._active()
        if provider:
            active = {k: v for k, v in active.items() if k == provider}

        _, _, grand_by_service = await _srv._gather_costs(active, start, today, granularity="DAILY")

        # Aggregate per-connector daily data directly
        daily: dict[str, float] = {}

        async def _one_daily(name: str, connector: _srv.Any):
            try:
                return await _srv._fetch_costs_cached(name, connector, start, today, granularity="DAILY")
            except Exception:
                return None

        for summary in await _srv.asyncio.gather(*[_one_daily(n, c) for n, c in active.items()]):
            if summary is None:
                continue
            # daily_breakdown is a dict[str, float] keyed by date string if available
            breakdown = getattr(summary, "daily_breakdown", None) or {}
            for day_str, amt in breakdown.items():
                daily[day_str] = daily.get(day_str, 0.0) + amt

        rows = [{"date": d, "spend": _srv._fmt_usd(v)} for d, v in sorted(daily.items())]
        return {
            "view": meta["name"],
            "period": f"{start} to {today}",
            "daily_spend": rows if rows else {"note": "Daily granularity not available for configured connectors."},
        }

    # ── by_service ───────────────────────────────────────────────────────────
    if view == "by_service":
        first_this, this_end, first_last, last_last, on_the_first = _month_pair(today)

        active = await _srv._active()
        if provider:
            active = {k: v for k, v in active.items() if k == provider}

        _, _, this_svc = await _srv._gather_costs(active, first_this, this_end)
        _, _, last_svc = await _srv._gather_costs(active, first_last, last_last)

        rows = []
        for svc, amt in sorted(this_svc.items(), key=lambda x: -x[1])[:20]:
            last_amt = last_svc.get(svc, 0.0)
            pct = ((amt - last_amt) / last_amt * 100) if last_amt else None
            rows.append({
                "service": svc,
                "this_month": _srv._fmt_usd(amt),
                "last_month": _srv._fmt_usd(last_amt),
                "change": f"{pct:+.1f}%" if pct is not None else "new",
            })

        return {
            "view": meta["name"],
            "period": f"{first_this} to {this_end}",
            "services": rows,
            **({"note": "It is the 1st, so there is no month-to-date yet. "
                        "'this_month' is the month that just closed and 'last_month' "
                        "is the one before it."} if on_the_first else {}),
        }

    # ── by_tag ───────────────────────────────────────────────────────────────
    if view == "by_tag":
        if not tag_key:
            return {
                "error": "tag_key is required for the by_tag view.",
                "example": "get_view(view='by_tag', tag_key='team')",
            }
        start, end = _srv._default_dates()
        active = await _srv._active()
        if provider:
            active = {k: v for k, v in active.items() if k == provider}

        tag_totals: dict[str, float] = {}

        async def _one_tags(connector: _srv.Any):
            try:
                if hasattr(connector, "get_costs_by_tag"):
                    return await connector.get_costs_by_tag(start, end, tag_key=tag_key)
            except Exception:
                pass
            return {}

        for result in await _srv.asyncio.gather(*[_one_tags(c) for c in active.values()]):
            for tag_val, amt in result.items():
                if tag_value and tag_val != tag_value:
                    continue
                tag_totals[tag_val] = tag_totals.get(tag_val, 0.0) + amt

        if not tag_totals:
            return {
                "view": meta["name"],
                "tag_key": tag_key,
                "note": (
                    f"No cost data found for tag '{tag_key}'. "
                    "Make sure resources are tagged and Cost Explorer tag activation is enabled."
                ),
            }

        all_rows = [
            {"tag_value": k, "spend": _srv._fmt_usd(v)}
            for k, v in sorted(tag_totals.items(), key=lambda x: -x[1])
        ]
        rows, omitted = _srv.fit_to_budget(all_rows)
        return {
            "view": meta["name"],
            "tag_key": tag_key,
            "period": f"{start} to {end}",
            "by_tag": rows,
            **({"by_tag_truncated": True, "hint": f"Showing {len(rows)} of {len(all_rows)} tag values by spend to stay within token budget."} if omitted else {}),
            "total": _srv._fmt_usd(sum(tag_totals.values())),
        }

    # ── by_team ──────────────────────────────────────────────────────────────
    if view == "by_team":
        start, end = _srv._default_dates()
        try:
            from ..attribution.engine import AttributionEngine
            engine = AttributionEngine()
            result = await engine.attribute(start, end)
            all_rows = [
                {"team": t, "spend": _srv._fmt_usd(v)}
                for t, v in sorted(result.items(), key=lambda x: -x[1])
            ]
            rows, omitted = _srv.fit_to_budget(all_rows)
            return {
                "view": meta["name"],
                "period": f"{start} to {end}",
                "by_team": rows,
                **({"by_team_truncated": True, "hint": f"Showing {len(rows)} of {len(all_rows)} teams by spend to stay within token budget."} if omitted else {}),
                "total": _srv._fmt_usd(sum(result.values())),
            }
        except Exception as e:
            return {"view": meta["name"], "error": str(e)}

    # ── top_spenders ─────────────────────────────────────────────────────────
    if view == "top_spenders":
        start, end = _srv._default_dates()
        active = await _srv._active()
        if provider:
            active = {k: v for k, v in active.items() if k == provider}

        _, _, grand_svc = await _srv._gather_costs(active, start, end)
        rows = [
            {"service": svc, "spend": _srv._fmt_usd(amt)}
            for svc, amt in sorted(grand_svc.items(), key=lambda x: -x[1])[:10]
        ]
        return {
            "view": meta["name"],
            "period": f"{start} to {end}",
            "top_10": rows,
        }

    # ── anomalies ────────────────────────────────────────────────────────────
    if view == "anomalies":
        return await _srv.get_anomalies(provider=provider)

    # ── rightsizing ──────────────────────────────────────────────────────────
    if view == "rightsizing":
        return await _srv.get_rightsizing_recommendations()

    # ── waste ────────────────────────────────────────────────────────────────
    if view == "waste":
        try:
            from ..analyzers.waste import scan_waste
            result = scan_waste()
            return {"view": meta["name"], **result}
        except Exception as e:
            return {"view": meta["name"], "error": str(e)}

    # ── saas ─────────────────────────────────────────────────────────────────
    if view == "saas":
        start, end = _srv._default_dates()
        active = await _srv._active(subset=_srv.SAAS_CONNECTORS)
        if provider:
            active = {k: v for k, v in active.items() if k == provider}

        grand_total, by_provider, _ = await _srv._gather_costs(active, start, end)
        return {
            "view": meta["name"],
            "period": f"{start} to {end}",
            "total": _srv._fmt_usd(grand_total),
            "by_provider": {
                k: _srv._fmt_usd(v.get("total_usd", 0)) for k, v in by_provider.items()
            },
        }

    return {"error": f"View '{view}' is defined but not yet implemented."}


@_srv.mcp.tool()
async def pin_view(
    title: str,
    dimensions: list[str] | None = None,
    filters: list[dict] | None = None,
    exclusions: list[dict] | None = None,
    metric: str = "EffectiveCost",
    granularity: str = "TOTAL",
    order_by: str = "metric",
    limit: int = 50,
    days: int = 30,
    scope: str = "instance",
) -> dict:
    """
    Pin a cost slice to the dashboard as a saved card. Takes the same slicing
    arguments as slice_costs (dimensions / filters / exclusions / metric / etc.),
    plus a title and a rolling lookback `days`. The pinned card re-runs its slice
    live on each dashboard load over the trailing `days`, so it always shows fresh
    numbers. scope: "instance" (shared on this nable) or "me". Read-only on the cloud:
    this only saves a view definition locally.
    Args:
        title: Card title shown on the dashboard.
        dimensions: Fields to group by (as in slice_costs).
        filters: Include-filters, {field: [values]}.
        exclusions: Exclude-filters, {field: [values]}.
        metric: "cost" (default) or another supported metric.
        granularity: "DAILY" or "MONTHLY".
        order_by: Sort field, defaults to the metric descending.
        limit: Max rows in the card.
        days: Look-back window in days (default 30).
        scope: "instance" (default) pins for this machine.

    Examples:
        - "Pin this S3-by-region view to my dashboard"
        - "Save that as a card"

    """
    if err := _srv.require_role("viewer"):
        return err
    from ..slice import parse_spec
    from ..slice.engine import derive_card
    from ..slice.spec import SliceResult, SliceSpecError
    from ..slice.views import pin_view as _pin

    try:
        spec = parse_spec({
            "dimensions": dimensions or [], "filters": filters or [],
            "exclusions": exclusions or [], "metric": metric,
            "granularity": granularity, "order_by": order_by, "limit": limit,
        })
    except SliceSpecError as exc:
        return {"error": str(exc)}
    empty = SliceResult(rows=[], total=0.0, metric=spec.metric, dimensions=spec.dimensions)
    card = derive_card(spec, empty, title=title).to_dict()
    card["days"] = max(1, int(days or 30))
    vid = _pin(card, owner="instance", scope=scope)
    return {"pinned": True, "id": vid, "title": card["title"]}


@_srv.mcp.tool()
async def list_pinned_views() -> dict:
    """
    List the cost cards pinned to the dashboard: every saved view with its id,
    title, template, metric and dimensions, so you can re-run one with
    get_pinned_view(id) or remove one with unpin_view(id).

    Examples:
        - "What views do I have pinned?"
        - "Show my saved cost cards"
    """
    if err := _srv.require_role("viewer"):
        return err
    from ..slice.views import list_pinned_views as _list
    views = _list(owner="instance")
    return {"count": len(views), "views": [
        {"id": v["id"], "title": v["title"], "template": v["template"],
         "metric": v["slice"].get("metric"), "dimensions": v["slice"].get("dimensions")}
        for v in views
    ]}


@_srv.mcp.tool()
async def get_pinned_view(view_id: int) -> dict:
    """
    Re-run a pinned view by id and return fresh cost data plus its rendered card.
    Read-only: nothing is modified, the saved definition is executed against
    current data.

    Args:
        view_id: The pinned card's id, from list_pinned_views().

    Examples:
        - "Refresh my S3 spend card"
        - "Re-run pinned view 2"
    """
    if err := _srv.require_role("viewer"):
        return err
    from ..slice.views import get_pinned_view as _get
    v = _get(int(view_id), owner="instance")
    if not v:
        return {"error": f"No pinned view with id {view_id}."}
    out = await _srv._run_stored_slice(v["slice"], v["card"].get("days", 30), v["title"], v["template"])
    out["id"] = v["id"]
    return out


@_srv.mcp.tool()
async def unpin_view(view_id: int) -> dict:
    """
    Remove a pinned cost card from the dashboard by id, so the dashboard stops
    tracking that saved view. The underlying saved view is not deleted, only
    unpinned; pin_view() puts it back.

    Args:
        view_id: The pinned card's id, from list_pinned_views().

    Examples:
        - "Unpin the S3 spend card from my dashboard"
        - "Remove pinned view 3"
    """
    if err := _srv.require_role("viewer"):
        return err
    from ..slice.views import unpin_view as _unpin
    return {"unpinned": _unpin(int(view_id), owner="instance"), "id": int(view_id)}


@_srv.mcp.tool()
async def what_can_nable_do(detailed: bool = False) -> str:
    """
    Show everything nable can do, tailored to what you've connected.

    Call this when the user asks "what can you do?", "what features do you have?",
    "what should I try first?", "show me what's available", or "help". Always call
    it right after a user connects their first account, so they see what just
    became possible. Pass detailed=True to also list the underlying tool names.
    Args:
        detailed: True returns the full capability list instead of the summary.

    Examples:
        - "What can nable do?"
        - "List your capabilities"

    """
    connected: set[str] = set()

    # Cloud + SaaS connectors live in the class registries.
    for pool in (_srv.CLOUD_CONNECTORS, _srv.SAAS_CONNECTORS):
        for name, connector in pool.items():
            try:
                if await connector.is_configured():
                    connected.add(name)
            except Exception:
                pass

    # LLM / AI providers are module-level detectors (where AI-native accounts
    # actually spend: direct APIs, gateways, and GPU infra).
    from ..connectors.saas import (
        openai_usage, anthropic_usage, vertex_costs, openrouter, litellm, gpu_infra,
    )
    for name, check in {
        "openai": openai_usage.is_configured,
        "anthropic": anthropic_usage.is_configured,
        "vertex": vertex_costs.is_configured,
        "openrouter": openrouter.is_configured,
        "litellm": litellm.is_configured,
    }.items():
        try:
            if await check():
                connected.add(name)
        except Exception:
            pass
    for name, check in {
        "modal": gpu_infra.modal_configured,
        "together": gpu_infra.together_configured,
        "replicate": gpu_infra.replicate_configured,
    }.items():
        try:
            if check():
                connected.add(name)
        except Exception:
            pass

    from ..capabilities import has_llm as _has_llm, render_capabilities
    if _has_llm(connected):
        connected.add("llm")

    # Best-effort Kubernetes detection: a reachable kubeconfig (no agent needed).
    try:
        import os
        from pathlib import Path
        if os.environ.get("KUBECONFIG") or (Path.home() / ".kube" / "config").exists():
            connected.add("kubernetes")
    except Exception:
        pass

    try:
        plan = _srv.get_status().mode
    except Exception:
        plan = "free"
    return render_capabilities(connected, plan=plan, detailed=detailed)


@_srv.mcp.tool()
async def get_tableau_connection_info(port: int = 8080) -> str:
    """
    Returns the Tableau Web Data Connector URL for connecting Tableau Desktop to nable.

    Use when:
        - "How do I connect Tableau?"
        - "Tableau integration"
        - "What's the Tableau URL?"
        - "Connect Tableau to nable"
    Args:
        port: Local TCP port to serve on.

    Examples:
        - "How do I connect Tableau?"
        - "Give me the Tableau connector URL"

    """
    try:
        try:
            from ..server_web import _local_ip
        except ImportError:
            return (
                "Tableau access runs through the local web dashboard, which is a hosted/enterprise "
                "feature and is not part of the open-source nable package. The local product is the MCP "
                "server you're using now in your editor. For a hosted dashboard, see https://getnable.com."
            )
        ip = _local_ip()
        base = f"http://{ip}:{port}"
        return f"""## Connecting Tableau to nable

1. Open Tableau Desktop
2. Click "Connect" -> "To a Server" -> "Web Data Connector"
3. Enter this URL: {base}/tableau
4. Click "Connect" - Tableau will load the nable connector
5. Select the tables you want (Costs, Opportunities, or Anomalies)
6. Click "Update Now" to fetch data

Or download CSVs directly:
- Costs: {base}/tableau/costs.csv
- Opportunities: {base}/tableau/opportunities.csv
- Anomalies: {base}/tableau/anomalies.csv

Run `finops serve` first if the server is not running.
"""
    except Exception as exc:
        _srv.log.error("get_tableau_connection_info failed: %s", exc)
        return f"Error: {exc}"
