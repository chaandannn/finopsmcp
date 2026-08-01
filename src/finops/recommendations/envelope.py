"""
Trust envelope for recommendations.

Every finding is one of two kinds, decided by the STRENGTH OF EVIDENCE behind it,
never by how big the dollar number is:

  - "recommendation": we measured it. Precise dollar figure, a safe action, and a
    post-action verifier can later confirm the saving actually landed. We stake the
    word "recommend" on it.
  - "investigation": we noticed a real signal but cannot confirm it yet (a proxy, a
    heuristic, missing tags or data). NO precise dollar claim, only an
    order-of-magnitude band, and the first step is always how to get to certainty.
    The agent can run the investigation and graduate it into a recommendation.

A low-confidence "recommendation" is impossible by construction: classify() routes
anything not backed by measured evidence to "investigation", and Finding.to_dict()
strips any precise figure off an investigation. That is the whole point: we never
put a precise number on something we did not measure.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any

# Evidence strength.
#   "measured" -> real per-resource data: CloudTrail call counts, tagged spend,
#                 Compute Optimizer findings, an observed idle/stopped state.
#   "inferred" -> a proxy or heuristic: name matching, an even per-resource split,
#                 an assumption we have not confirmed.
MEASURED = "measured"
INFERRED = "inferred"

RECOMMENDATION = "recommendation"
INVESTIGATION = "investigation"


def classify(evidence: str) -> str:
    """Measured evidence earns a recommendation; anything inferred is an
    investigation. This single gate is what makes a low-confidence recommendation
    impossible."""
    return RECOMMENDATION if evidence == MEASURED else INVESTIGATION


def magnitude_band(usd_per_month: float | None) -> str:
    """Order-of-magnitude label for an UNCONFIRMED figure, so an investigation can
    convey size without faking precision. Never returns an exact dollar amount.
    None means we have no estimate at all, which is "unknown", not "small"."""
    if usd_per_month is None:
        return "unknown size"
    try:
        v = abs(float(usd_per_month))
    except (TypeError, ValueError):
        return "unknown size"
    import math
    if not math.isfinite(v):
        # NaN fails every comparison below and would fall through to the LARGEST
        # band, turning garbage into "~hundreds of thousands/mo".
        return "unknown size"
    if v < 100:
        return "under ~$100/mo"
    if v < 1000:
        return "~hundreds/mo"
    if v < 10000:
        return "~thousands/mo"
    if v < 100000:
        return "~tens of thousands/mo"
    return "~hundreds of thousands/mo"


@dataclass
class Finding:
    """One classified finding. Recommenders build this; the agent and reports read
    it and must not invent precision the evidence does not carry.

    Set ``est_monthly_savings`` only when evidence is measured (it is forced to None
    on an investigation). For an investigation, pass the rough internal number as
    ``rough_monthly`` and to_dict() converts it to a magnitude band."""
    source: str                            # scanner that produced it, e.g. "textract_env"
    title: str
    why: str                               # plain English: what is happening and why it is waste
    evidence: str                          # MEASURED | INFERRED
    confidence: str = "medium"             # "high" | "medium" | "low"
    why_unsure: str = ""                   # investigations: the exact gap in our evidence
    assumptions: list[str] = field(default_factory=list)
    remediation: list[str] = field(default_factory=list)   # confirm-first, then the safe fix
    confirm_steps: list[str] = field(default_factory=list)  # how YOU can confirm it (free, manual)
    # The honest upsell: many investigations are only investigations because we are
    # limited to Cost Explorer. With deeper data access (CUR line items, CloudTrail),
    # nable can confirm them automatically and turn them into recommendations. That
    # deeper access is a Pro capability. We never hide the signal, we offer to remove
    # the manual work of confirming it.
    pro_can_confirm: bool = False     # can nable auto-confirm this with deeper (Pro) data access?
    pro_unlock: str = ""              # plain English: the CUR/CloudTrail access that confirms it
    est_monthly_savings: float | None = None    # ONLY set for recommendations (measured)
    rough_monthly: float | None = None          # investigations: internal proxy number -> band
    resource_id: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def kind(self) -> str:
        return classify(self.evidence)

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        kind = self.kind
        d["kind"] = kind
        if kind == INVESTIGATION:
            # Enforce the invariant: an investigation never ships a precise figure.
            d["est_monthly_savings"] = None
            d["magnitude"] = magnitude_band(self.rough_monthly if self.rough_monthly is not None
                                            else self.est_monthly_savings)
        d.pop("rough_monthly", None)
        # Findings ship to the model in lists of dozens, so every empty field is pure
        # token cost (measured ~30 tokens of empty keys per audit finding). Drop the
        # valueless ones. est_monthly_savings survives even as None: on an
        # investigation the explicit None IS the message, we refuse to invent a number.
        return {
            k: v for k, v in d.items()
            if k == "est_monthly_savings" or v not in ("", [], {}, None, False)
        }
