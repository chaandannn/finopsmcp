"""Turn a detected anomaly into something a human can act on.

The alert used to carry a percentage and a z-score and no dollars at all:

    Change: +180% vs 28-day avg     Today: $6,540.00     Z-score: 4.21

A percentage is not a decision. "+180%" on a $12/day service is noise; the same
percentage on a $6,500/day service is a budget event, and the reader has to do
the subtraction themselves to tell which one they are looking at. Every claim we
make is supposed to carry a dollar figure, and this one, the one that wakes people
up, did not.

This is pure arithmetic over fields the anomaly already has. No provider API call,
no LLM. That matters: this runs inside the scheduler for every alerted anomaly, so
anything with a per-call cost does not belong here.

What it deliberately does NOT do is claim to know WHY. Naming the resource behind
the spike needs a per-service drill-down (another Cost Explorer call, per
provider), which is worth doing but is not free and is not this. Instead the alert
says exactly which question to ask next.
"""
from __future__ import annotations

from typing import Any


def _f(value: Any) -> float:
    try:
        return float(value or 0.0)
    except (TypeError, ValueError):
        return 0.0


def impact(anomaly: dict[str, Any]) -> dict[str, Any]:
    """Dollar impact of an anomaly, plus the next question worth asking.

    `delta_usd` is signed: positive means spending more than baseline. The
    run-rate is what this becomes over 30 days IF it persists, which is the
    honest framing. A one-day spike that self-corrects costs the delta once,
    not the run-rate, so the wording has to carry that conditional.
    """
    current = _f(anomaly.get("current_amount"))
    baseline = _f(anomaly.get("baseline_mean"))
    delta = current - baseline
    service = anomaly.get("service") or "this service"
    provider = (anomaly.get("provider") or "").lower()

    out: dict[str, Any] = {
        "delta_usd": round(delta, 2),
        "monthly_run_rate_usd": round(delta * 30, 2),
        "impact_summary": _summary(delta, service),
        "next_step": _next_step(service, provider),
    }
    return out


def _summary(delta: float, service: str) -> str:
    if delta > 0:
        return (
            f"{service} is running ${delta:,.2f}/day above its baseline. "
            f"If it holds, that is ${delta * 30:,.2f} over the next 30 days."
        )
    if delta < 0:
        return (
            f"{service} is running ${abs(delta):,.2f}/day BELOW its baseline "
            f"(${abs(delta) * 30:,.2f}/30d). Worth checking nothing broke."
        )
    return f"{service} matched its baseline in dollar terms."


def _next_step(service: str, provider: str) -> str:
    """One concrete question, not a menu. The complaint about budget alerts is
    that they tell you a threshold moved and leave you to figure out the rest."""
    scope = f'"{service}"' + (f" on {provider.upper()}" if provider else "")
    return (
        f"Ask nable: what changed in {scope} over the last 7 days, broken down by "
        f"usage type and resource?"
    )


def enrich(anomaly: dict[str, Any]) -> dict[str, Any]:
    """Return the anomaly with impact fields merged in. Never raises: an alert
    that fails to enrich must still be delivered."""
    try:
        return {**anomaly, **impact(anomaly)}
    except Exception:  # pragma: no cover - defensive
        return anomaly
