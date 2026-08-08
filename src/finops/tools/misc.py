# SPDX-License-Identifier: Apache-2.0
"""misc MCP tools (extracted from server.py; see finops/tools/__init__.py).

Server-local helpers, globals, and the mcp instance are reached through the live
server module (_srv.NAME) so monkeypatching finops.server.* still works and no
import-order coupling exists."""
from __future__ import annotations

from .. import server as _srv


@_srv.mcp.tool()
async def nable_setup_status() -> dict:
    """
    Agent-driven onboarding: what is connected, what credentials are already on
    this machine, and the exact command to connect each remaining provider.

    Call this when the user asks to connect a provider, says setup is incomplete,
    or asks what they are missing. Detected ambient credentials (gcloud login,
    env keys, ~/.modal.toml) mean the connect is ONE terminal command with no
    secrets involved; run it for the user or hand them the command.

    Rules for the agent, and they are hard rules:
      - NEVER ask the user to paste an API key or secret into the chat. For
        paste-a-key providers, have them run the setup command in their own
        terminal; it deep-links the key page and stores the key locally.
      - Prefer the zero-secret paths: `finops connect` (batch-connects everything
        detected) and `finops setup gcp` / ambient AWS, where no secret ever
        passes through the conversation.

    Examples:
        - "Connect my GCP costs"
        - "What providers am I missing?"
        - "Finish setting up nable"
    """
    from ..install_health import install_health
    from ..setup_scan import scan_ambient_credentials

    # A dependency-less install advertises every tool and answers tools/list
    # looking healthy, then dies on the first cloud call. Lead with that when it
    # is true: no amount of connecting providers fixes a broken install, so the
    # agent must not walk the user through setup on top of it.
    health = install_health()
    if not health["ok"]:
        return {
            "install_broken": True,
            "detail": health["detail"],
            "fix": health["fix"],
            "isolated_fix": health["isolated_fix"],
            "missing_packages": health["missing"],
            "next_step": ("Tell the user their nable install is missing "
                          "dependencies and give them the fix command. Do not "
                          "attempt to connect providers until it is repaired."),
        }

    connected = []
    not_connected = []
    for name, conn in _srv._ALL_CONNECTORS.items():
        try:
            ok = await conn.is_configured()
        except Exception:
            ok = False
        (connected if ok else not_connected).append(name)

    try:
        found = scan_ambient_credentials()
    except Exception:
        found = []
    detected = [
        {"provider": f["name"], "source": f["source"],
         "connect": "finops connect", "secrets_in_chat": False}
        for f in found
    ]

    return {
        "connected": sorted(connected),
        "not_connected": sorted(not_connected),
        "ambient_credentials_detected": detected,
        "zero_secret_paths": {
            "batch": "finops connect  (connects everything detected above, one keystroke)",
            "gcp": "finops setup gcp  (uses gcloud login; no JSON key needed if gcloud is authed)",
            "aws": "finops setup aws  (detects local AWS credentials automatically)",
        },
        "paste_a_key_note": (
            "For any other provider, the user runs `finops setup <provider>` in "
            "their own terminal. It deep-links the exact key page and stores the "
            "key in the local vault. Never relay a secret through this chat."
        ),
    }


@_srv.mcp.tool()
async def get_savings_summary() -> dict:
    """
    Show the realized-savings dashboard: how much nable has recommended, how much
    has been acted on, and how much has been verified as actually saved.

    Tracks the full lifecycle of every recommendation:
      open → acted on → verified (change confirmed in AWS/Azure/GCP)
      open → dismissed (won't fix)

    Examples:
        - "How much have we saved from recommendations so far?"
        - "Show me our realized savings"
        - "Which recommendations have we actually acted on?"
        - "What's our total potential savings sitting open?"
    """
    from ..recommendations.savings_tracker import get_summary, expire_stale
    expire_stale()  # mark 45-day-old open recs as expired
    summary = get_summary()

    potential = summary["potential_monthly_usd"]
    acted = summary["acted_on_monthly_usd"]
    verified = summary["verified_monthly_usd"]
    total = summary["total_recommendations"]

    lines = []
    if total == 0:
        lines.append("No recommendations tracked yet. Run get_rightsizing_recommendations() or scan_waste_patterns() to start building history.")
    else:
        lines.append(f"Tracking {total} recommendation{'s' if total != 1 else ''}.")
        if potential > 0:
            lines.append(f"  Open potential: ${potential:,.0f}/mo still available.")
        if acted > 0:
            lines.append(f"  Acted on: ${acted:,.0f}/mo estimated savings (pending verification).")
        if verified > 0:
            lines.append(f"  Verified savings: ${verified:,.0f}/mo (${summary['verified_annual_usd']:,.0f}/yr confirmed).")

    # Learning loop: annotate each source with what THIS account's ledger shows
    # about it (act-rate, accuracy, verdict). Propose-only: this adds honest
    # confidence context, it never reorders spend numbers or hides anything.
    # Degrades to a no-op on a cold ledger (no source has real signal yet).
    try:
        from ..recommendations.learning import customer_signal
        from ..recommendations.learning.signal import signal_for, _has_signal
        sig = customer_signal()
        learned_notes = []
        for src, block in summary.get("by_source", {}).items():
            s = signal_for(sig, src)
            block["learned"] = {
                "verdict": s["verdict"],
                "coverage": s["coverage"],
                "act_rate": s["act_rate"],
                "accuracy": s["accuracy"],
                "why": s["why"],
            }
            # Only surface a headline note for sources with real, resolved signal,
            # so a near-empty ledger stays quiet rather than over-claiming.
            if _has_signal(s):
                learned_notes.append(f"{src}: {s['why']}")
        if learned_notes:
            summary["learning_note"] = (
                "Confidence context from your own ledger (propose-only, nothing hidden): "
                + " ".join(learned_notes)
            )
    except Exception as exc:
        _srv.log.debug("learning annotation skipped in get_savings_summary: %s", exc)

    summary["summary"] = " ".join(lines)
    summary["tip"] = (
        "Use mark_recommendation_acted_on(id) when you implement a recommendation. "
        "Use verify_savings() to auto-check if EC2/RDS changes were made. "
        "Use dismiss_recommendation(id) for recommendations you've decided not to action."
    )
    return summary


@_srv.mcp.tool()
async def activate_pro(license_key: str = "") -> dict:
    """
    Activate your nable Pro or Team license right here, no terminal, no restart.

    Paste the license key from your receipt email (it starts with FINOPS-2-).
    nable validates it locally, stores it on this machine, and unlocks the paid
    features in this same session immediately. The key is verified offline with a
    public key bundled in nable; nothing about it is sent anywhere.

    Examples:
        - "Activate my license FINOPS-2-..."
        - "I just paid for Pro, here's my key"

    Args:
        license_key: Your license key from the receipt email (FINOPS-2-...).
    """
    from ..license import store_license, get_status

    key = (license_key or "").strip()
    if not key:
        return {
            "activated": False,
            "message": ("Paste your license key to activate (it starts with FINOPS-2- and is in "
                        "your receipt email). No terminal or restart needed."),
            "get_pro": _srv._UPGRADE_URL,
        }

    status = store_license(key)
    if status.mode in ("pro", "team", "enterprise", "trial"):
        # store_license cleared the cached status, so get_status re-reads and this
        # running server is Pro from the next call on. No restart required.
        live = get_status()
        return {
            "activated": True,
            "plan": live.mode,
            "email": getattr(live, "email", "") or None,
            "message": f"{live.mode.upper()} is active on this machine now. No restart needed.",
            "note": "The key was verified offline and stored locally; nothing left your machine.",
        }

    return {
        "activated": False,
        "plan": status.mode,
        "error": status.message or "That license key did not validate.",
        "get_pro": _srv._UPGRADE_URL,
    }
