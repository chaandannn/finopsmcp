# SPDX-License-Identifier: Apache-2.0
"""Recommendations & learning MCP tools.

Extracted verbatim from server.py (the 14k-line monolith) as the first step of
splitting tools into per-family modules. These drive the savings ledger, the
propose-only recommendation lifecycle (act-on / dismiss / verify), the
context-memory loop (remember / recall / forget an intentional finding), and the
learning-quality readouts. They register against the shared, telemetry-wrapped
`mcp` instance the moment server.py imports this module, so behavior is identical
to when they lived inline.
"""
from __future__ import annotations

from ..server import mcp, log
from ..license import require_pro

@mcp.tool()
async def mark_recommendation_acted_on(recommendation_id: int) -> dict:
    """
    Mark a savings recommendation as acted on (you've implemented the change).
    nable will then attempt to verify the change next time verify_savings() runs.

    Args:
        recommendation_id: The ID from list_savings_recommendations() or get_savings_summary().

    Examples:
        - "I resized that EC2 instance, mark recommendation 42 as done"
        - "We shut down the idle RDS, mark it acted on"
        - "Mark recommendation 7 as complete"
    """
    # The acted-on/verify/learn loop is the Ledger agent (Pro). Free = read-only.
    if (err := require_pro("agent_learning")):
        return err
    from ..recommendations.savings_tracker import mark_acted_on
    ok = mark_acted_on(recommendation_id)
    if ok:
        return {
            "status": "acted_on",
            "message": f"Recommendation {recommendation_id} marked as acted on. Run verify_savings() in a few days to confirm the change and lock in the realized savings.",
        }
    return {
        "error": f"Recommendation {recommendation_id} not found or not in 'open' status.",
        "tip": "Use list_savings_recommendations() to see current IDs and statuses.",
    }


@mcp.tool()
async def dismiss_recommendation(recommendation_id: int, reason: str = "") -> dict:
    """
    Dismiss a recommendation you've decided not to act on (won't fix, accepted risk, etc.).
    Dismissed recommendations won't appear in open potential savings.

    Always pass the user's reason when they give one. It is how nable learns which
    recommendation types fit this environment. A business reason ("reserved for peak",
    "SLA-sensitive", "another team owns it") is recorded but kept OUT of the act-rate,
    so a valid "keep it" never trains a good recommendation type down. A quality reason
    ("the estimate is wrong") does count against that type. Pass the user's own words;
    nable categorizes them.

    Args:
        recommendation_id: The ID from list_savings_recommendations().
        reason: Why you're dismissing it, in the user's words (e.g. "reserved for burst traffic").

    Examples:
        - "Dismiss recommendation 15, we need that instance for peak load"
        - "Mark recommendation 8 as won't fix"
    """
    from ..recommendations.savings_tracker import mark_dismissed
    from ..recommendations.learning.reasons import classify_dismiss_reason
    ok = mark_dismissed(recommendation_id, reason)
    if ok:
        category = classify_dismiss_reason(reason)
        out = {
            "status": "dismissed",
            "reason_category": category,
            "message": f"Recommendation {recommendation_id} dismissed." + (f" Reason: {reason}" if reason else ""),
        }
        # Make the learning loop visible: a business-reason dismissal is a choice we
        # honor and will not hold against this recommendation type in future ranking.
        if category in ("reserved_for_peak", "sla_sensitive", "not_our_resource"):
            out["learning_note"] = (
                "Recorded as a business reason, so this will not count against this "
                "recommendation type when nable ranks future proposals for you."
            )
            # Context memory: a business reason means "this is fine." Remember it so
            # nable never re-flags this exact resource, and note that the user can
            # generalize the rule (e.g. to the whole environment) if they want.
            try:
                from ..recommendations.savings_tracker import get_recommendation
                from ..recommendations import context_memory
                rec = get_recommendation(recommendation_id)
                if rec and rec.get("resource_id"):
                    ann = context_memory.remember(
                        scope="resource",
                        match_value=rec["resource_id"],
                        reason=reason,
                        provider=rec.get("provider"),
                        account_id=rec.get("account_id"),
                        created_by="dismiss",
                        source_rec_id=recommendation_id,
                    )
                    out["context_learned"] = {
                        "annotation_id": ann["id"],
                        "scope": "resource",
                        "match_value": rec["resource_id"],
                        "message": (
                            f"nable will stop flagging {rec['resource_id']} for this. "
                            "To generalize (e.g. every resource in this environment, or "
                            "all findings of this type), use remember_cost_context."
                        ),
                    }
                    # Policy inference: has this dismissal pushed a whole class over the
                    # threshold? If so, offer to generalize the exception into one rule.
                    try:
                        from ..recommendations.learning.policy_inference import policy_for_rec
                        cand = policy_for_rec(rec)
                        if cand:
                            out["policy_suggestion"] = {
                                "scope": cand["scope"],
                                "match_value": cand["match_value"],
                                "evidence": cand["evidence"],
                                "confirm": cand["confirm"],
                                "message": ("You've rejected this whole class repeatedly. "
                                            "Call suggest_cost_policies() to turn it into one "
                                            "standing rule instead of dismissing each finding."),
                            }
                    except Exception as exc:
                        log.debug("policy_inference nudge skipped: %s", exc)
            except Exception as exc:
                log.debug("context_memory.remember skipped on dismiss: %s", exc)
        elif not reason:
            out["learning_note"] = (
                "No reason captured. A short reason helps nable learn what fits your "
                "environment and avoid suggesting it again."
            )
        return out
    return {
        "error": f"Recommendation {recommendation_id} not found or already in a terminal state.",
    }


@mcp.tool()
async def suggest_cost_policies() -> dict:
    """
    Propose standing cost rules nable has inferred from what your team keeps rejecting.

    When you dismiss the same CLASS of finding for the same business reason again and
    again (spot on prod, idle in the dr environment, a whole provider you don't
    optimize), nable notices and proposes one durable rule instead of nagging every
    time. Each proposal shows the evidence and the exact remember_cost_context() call
    that enacts it. Nothing is applied until you confirm one, so you stay in control.

    Examples:
        - "What cost rules has nable figured out about how we work?"
        - "Turn my repeated dismissals into standing policies"
    """
    from ..recommendations.learning.policy_inference import infer_policies
    cands = infer_policies()
    if not cands:
        return {
            "candidates": [],
            "message": (
                "No standing rules to propose yet. As your team dismisses recurring "
                "findings with a business reason, nable will spot the pattern and offer "
                "to generalize it. Confirm any proposal with remember_cost_context()."
            ),
        }
    return {
        "count": len(cands),
        "candidates": cands,
        "message": (
            f"{len(cands)} inferred cost policy proposal(s) from your dismissal history. "
            "Confirm one with the remember_cost_context() call in its `confirm` field; "
            "nable then stops surfacing that whole class."
        ),
    }


@mcp.tool()
async def remember_cost_context(
    scope: str,
    match_value: str,
    reason: str,
    provider: str | None = None,
    account_id: str | None = None,
) -> dict:
    """
    Teach nable a standing fact about how THIS environment runs, so it stops
    flagging what you've already decided is fine and can generalize the rule.

    This is nable's memory. Answer once ("that idle box is our DR standby", "spot
    is never OK on prod"), and nable will not re-surface matching findings. Findings
    it silences are still shown under `suppressed_by_context` with your reason, never
    deleted, so nothing is hidden without a trail.

    scope, narrowest to broadest:
      - "resource"       one exact resource id (match_value = the resource id)
      - "resource_type"  every resource of a type (e.g. "nat_gateway", "ec2")
      - "bucket"         an environment bucket (e.g. "dr", "nonprod")
      - "source"         a finding type (e.g. "rightsizing", "idle", "spot")
      - "provider"       a whole provider (e.g. "snowflake")

    Narrow a broad scope to one org boundary with provider and/or account_id.

    Args:
        scope: One of resource | resource_type | bucket | source | provider.
        match_value: The value that scope matches (a resource id, type, bucket, source, or provider).
        reason: Why it's intentional, in the user's words ("DR standby, must stay warm").
        provider: Optional, restrict a broad rule to one provider.
        account_id: Optional, restrict a broad rule to one account.

    Examples:
        - "Remember that i-0abc123 is fine, it's our DR standby"  -> scope=resource
        - "Stop flagging anything in the dr environment"           -> scope=bucket, match_value=dr
        - "We never want spot recommendations on prod"             -> scope=source, match_value=rightsizing
    """
    from ..recommendations import context_memory
    try:
        ann = context_memory.remember(
            scope=scope, match_value=match_value, reason=reason,
            provider=provider, account_id=account_id, created_by="user",
        )
    except ValueError as exc:
        return {"error": str(exc),
                "valid_scopes": sorted(context_memory.VALID_SCOPES)}
    return {
        "status": "remembered",
        "annotation": ann,
        "message": (
            f"nable will stop flagging {scope}={match_value}. "
            "See it anytime with get_learned_cost_context(); undo with "
            f"forget_cost_context({ann['id']})."
        ),
    }


@mcp.tool()
async def get_learned_cost_context() -> dict:
    """
    Show the operating model nable has learned about this environment: every
    standing exception a human has taught it, newest first, with the reason.

    This is "what does nable know about how we run" in one place: the DR standbys,
    the SLA-critical boxes, the finding types you've told it to ignore. Each entry
    is what is being suppressed, at what scope, and why.

    Examples:
        - "What has nable learned about our environment?"
        - "Show the cost exceptions we've told nable about"
    """
    from ..recommendations import context_memory
    entries = context_memory.list_context()
    return {
        "count": len(entries),
        "learned_context": entries,
        "message": (
            "No standing context learned yet. Dismiss a finding with a business "
            "reason, or use remember_cost_context, to teach nable how you run."
            if not entries else
            f"{len(entries)} learned exception(s) shaping which findings nable surfaces."
        ),
    }


@mcp.tool()
async def forget_cost_context(annotation_id: int) -> dict:
    """
    Remove a learned exception so nable resumes flagging matching findings.

    Args:
        annotation_id: The id from get_learned_cost_context().

    Examples:
        - "Forget that context rule 4, we decommissioned that DR box"
    """
    from ..recommendations import context_memory
    ok = context_memory.forget(annotation_id)
    if ok:
        return {"status": "forgotten",
                "message": f"Context {annotation_id} removed. nable will flag matching findings again."}
    return {"error": f"Context {annotation_id} not found or already inactive."}


@mcp.tool()
async def verify_savings() -> dict:
    """
    Auto-verify acted-on recommendations by checking if changes were actually
    implemented in AWS (EC2 instance type changes, etc.).

    Moves verified recommendations from 'acted_on' to 'verified' status and
    records the actual measured savings.

    Examples:
        - "Verify our savings, check if the rightsizing changes were made"
        - "Confirm which recommendations actually happened"
        - "Check if our EC2 downsizes are done"
    """
    # Verification is the Ledger agent's job (Pro): it proves the saving landed.
    if (err := require_pro("agent_learning")):
        return err
    from ..recommendations.savings_tracker import auto_verify_acted_on, get_summary
    newly_verified = auto_verify_acted_on()
    summary = get_summary()

    if not newly_verified:
        acted_count = summary["by_status"].get("acted_on", 0)
        if acted_count == 0:
            return {
                "message": "No acted-on recommendations to verify. Mark recommendations as acted on first with mark_recommendation_acted_on().",
                "verified_count": 0,
            }
        return {
            "message": f"{acted_count} recommendation{'s' if acted_count != 1 else ''} marked as acted on but changes not yet confirmed in AWS. Check back after the instance restarts or give it a few minutes.",
            "verified_count": 0,
            "tip": "For EC2 rightsizing, the instance needs to be stopped/started before the new type shows up.",
        }

    total_verified = sum(r["verified_monthly_savings_usd"] for r in newly_verified)
    banked_monthly = summary["verified_monthly_usd"]
    banked_annual = summary["verified_annual_usd"]
    return {
        "verified_count": len(newly_verified),
        "newly_verified": newly_verified,
        "total_new_monthly_savings_usd": round(total_verified, 2),
        "total_new_annual_savings_usd": round(total_verified * 12, 2),
        "message": f"Verified {len(newly_verified)} change{'s' if len(newly_verified) != 1 else ''}: ${total_verified:,.0f}/mo (${total_verified * 12:,.0f}/yr) newly banked. "
                   f"Total verified banked savings: ${banked_monthly:,.0f}/mo (${banked_annual:,.0f}/yr) confirmed off your bill.",
        "cumulative_verified_monthly_usd": banked_monthly,
        "cumulative_verified_annual_usd": banked_annual,
        # Explicit banked figures, distinct from predicted/found savings.
        "verified_banked_monthly_usd": banked_monthly,
        "verified_banked_annual_usd": banked_annual,
    }


@mcp.tool()
async def get_savings_ledger(
    days: int = 30,
    account_id: str | None = None,
) -> str:
    """
    Shows a clean summary of savings found, acted on, and verified.

    Use when:
        - "Show me the savings ledger"
        - "What savings have we achieved?"
        - "How much money has nable saved us?"
        - "Show me what opportunities were acted on"

    Args:
        days: Lookback window in days (default 30). Filters by generated_at.
        account_id: Filter to a specific cloud account ID. None = all accounts.
    Examples:
        - "Show the savings ledger"
        - "What savings has nable found and what happened to them?"

    """
    from datetime import datetime, timedelta, timezone
    from ..storage.db import get_engine, savings_recommendations
    from sqlalchemy import select

    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    sr = savings_recommendations
    engine = get_engine()

    with engine.connect() as conn:
        q = select(sr).where(sr.c.generated_at >= cutoff)
        if account_id:
            q = q.where(sr.c.account_id == account_id)
        rows = conn.execute(q).fetchall()

    if not rows:
        period = f"last {days} day{'s' if days != 1 else ''}"
        return (
            f"No savings recommendations found in the {period}. "
            "Run get_rightsizing_recommendations() or scan_waste_patterns() to surface opportunities."
        )

    found_rows = [r for r in rows if r.status not in ("dismissed", "expired")]
    acted_rows = [r for r in rows if r.status in ("acted_on", "verified")]
    verified_rows = [r for r in rows if r.status == "verified"]
    open_rows = [r for r in rows if r.status == "open"]

    found_total = sum(r.estimated_monthly_savings_usd or 0 for r in found_rows)
    acted_total = sum(r.estimated_monthly_savings_usd or 0 for r in acted_rows)
    verified_total = sum(
        r.verified_monthly_savings_usd or r.estimated_monthly_savings_usd or 0
        for r in verified_rows
    )

    period_label = f"Last {days} day{'s' if days != 1 else ''}"
    lines = [
        f"## Savings Ledger: {period_label}",
        "",
        f"FOUND:    ${found_total:,.0f}/mo across {len(found_rows)} opportunit{'ies' if len(found_rows) != 1 else 'y'}",
        f"ACTED ON: ${acted_total:,.0f}/mo across {len(acted_rows)} opportunit{'ies' if len(acted_rows) != 1 else 'y'}",
        f"VERIFIED: ${verified_total:,.0f}/mo in realized savings ({len(verified_rows)} confirmed)",
    ]

    if acted_rows:
        lines += ["", "### Opportunities acted on"]
        lines.append("| Date       | Opportunity                              | Est. Saving | Status   |")
        lines.append("|------------|------------------------------------------|-------------|----------|")
        for r in sorted(acted_rows, key=lambda x: (x.acted_on_at or x.generated_at), reverse=True)[:20]:
            ts = r.acted_on_at or r.generated_at
            date_str = ts.strftime("%Y-%m-%d") if ts else "unknown"
            desc = (r.description or r.resource_name or "")[:40]
            saving = f"${r.estimated_monthly_savings_usd:,.0f}/mo"
            lines.append(f"| {date_str} | {desc:<40} | {saving:<11} | {r.status:<8} |")

    if open_rows:
        lines += ["", "### Still open (not yet acted on)"]
        lines.append("| Date found | Opportunity                              | Est. Saving |")
        lines.append("|------------|------------------------------------------|-------------|")
        for r in sorted(open_rows, key=lambda x: x.estimated_monthly_savings_usd or 0, reverse=True)[:20]:
            date_str = r.generated_at.strftime("%Y-%m-%d") if r.generated_at else "unknown"
            desc = (r.description or r.resource_name or "")[:40]
            saving = f"${r.estimated_monthly_savings_usd:,.0f}/mo"
            lines.append(f"| {date_str} | {desc:<40} | {saving:<11} |")

    lines += [
        "",
        "Run mark_recommendation_acted_on(id) to move an opportunity to acted_on.",
        "Run verify_savings() to confirm realized savings from acted-on recommendations.",
    ]

    return "\n".join(lines)


@mcp.tool()
async def get_recommendation_quality() -> dict:
    """
    The recommendation-quality flywheel: per recommendation type, how often recs
    get acted on and how close the predicted savings were to the measured realized
    savings. The verified-savings proof, and the signal for which recommendation
    types actually pay off.

    Use when:
        - "Which of our recommendations actually saved money?"
        - "How accurate are nable's savings estimates?"
        - "How much have we verifiably saved, and from what?"
    Examples:
        - "How accurate have nable's recommendations been?"
        - "Show recommendation quality stats"

    """
    try:
        from ..recommendations.savings_tracker import quality_signal
        return quality_signal()
    except Exception as e:
        return {"error": str(e)}


@mcp.tool()
async def get_recommendation_learning() -> dict:
    """
    What nable has learned about how YOU use recommendations, and how it adapts.

    Per recommendation type (rightsizing, commitment, idle, spot, ...): your act-rate
    (how often you act on that type, vs blanket assumptions), how accurate the past
    savings estimates were, a COLD/WARMING/WARM confidence state, and the resulting
    verdict (boosted, suppressed-for-you, or neutral) with a plain-English reason.

    This is the adaptive moat: instead of blanket advice, recommendations are ranked
    and filtered to fit your environment and your track record. It is propose-only,
    it changes what you see and in what order, never the cloud.

    Use when:
        - "Why am I seeing this recommendation?" / "Why did this rank high?"
        - "What recommendation types did you stop showing me?"
        - "How is nable tailoring recommendations to us?"
    Examples:
        - "What has nable learned from my accepted and dismissed recommendations?"

    """
    # The learned approval profile is the Ledger agent (Pro).
    if (err := require_pro("agent_learning")):
        return err
    try:
        from ..recommendations.learning import customer_signal
        result = customer_signal()
        # Record verdict transitions as durable lessons, then attach them: every
        # learned adjustment is visible, evidenced and reversible. Best-effort;
        # a ledger problem must never break the signal itself.
        try:
            from ..recommendations.learning.ledger import lessons, sync_lessons
            sync_lessons()
            result["lessons"] = lessons()
            result["lessons_note"] = (
                "Each lesson is one learned adjustment, recorded when it happened "
                "with the evidence that caused it. Rolling one back pins that key "
                "to standard ranking until you restore it.")
        except Exception:
            pass
        return result
    except Exception as e:
        return {"error": str(e)}
