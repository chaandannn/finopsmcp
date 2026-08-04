"""The morning brief: what nable found while you were asleep.

The product this assembles is not a list of findings. Every cost tool has one of
those and nobody reads them. It is a short, ranked set of decisions, each already
carrying the four things a reviewer needs before they will act:

  1. what is happening and what it costs        (the finding, already critiqued)
  2. what breaks if I touch it                  (the resource map)
  3. exactly what change to make                (the drafted fix)
  4. how I will know it worked                  (the verification step)

RANKING IS BY ACTIONABILITY, NOT SIZE. The morning question is "what can I safely
do today", and the biggest number is frequently the one you cannot touch without a
change window and three approvals. Ease tilts the order without dominating it: a
$300/mo volume nothing references outranks a $500/mo instance inside an
auto-scaling group, because one of them will actually get done today, while a
large enough number still wins on its own merits. Every item carries
`why_this_rank`, so the order is arguable rather than magic.

PROPOSE-ONLY. Nothing here executes, opens a PR, or files a ticket. It drafts.
The whole value is that a human reads four lines and makes one decision.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from typing import Any, Callable

from ..recommendations.critique import SAVINGS_KEYS, critique
from ..recommendations.envelope import INVESTIGATION, RECOMMENDATION, magnitude_band
from .resource_map import ResourceMap, map_resource

# Ease multipliers applied to a finding's dollars to get its rank score. An
# unknown blast radius is penalised harder than a known small one: "we could not
# check" is a worse position to act from than "it touches two things".
_EASE_ISOLATED = 1.0
_EASE_SMALL_RADIUS = 0.7      # 1-2 attached resources
_EASE_LARGE_RADIUS = 0.4      # 3+
_EASE_UNKNOWN = 0.35          # isolation could not be determined

# Investigations never outrank recommendations, whatever their rough size: an
# unconfirmed number is not a thing to action before breakfast.
_INVESTIGATION_PENALTY = 0.01


def _savings(rec: dict) -> float | None:
    for k in SAVINGS_KEYS:
        v = rec.get(k)
        if v is None:
            continue
        try:
            f = float(v)
        except (TypeError, ValueError):
            continue
        import math
        if math.isfinite(f):
            return f
    return None


@dataclass
class BriefItem:
    finding: dict
    resource_map: ResourceMap
    drafted_fix: dict                      # {"summary","steps","commands","reversible","caveat"}
    verification: str
    rank_score: float
    why_this_rank: str

    @property
    def is_recommendation(self) -> bool:
        return self.finding.get("kind") != INVESTIGATION

    @property
    def monthly_usd(self) -> float | None:
        return _savings(self.finding) if self.is_recommendation else None

    @property
    def title(self) -> str:
        return str(self.finding.get("title")
                   or self.finding.get("issue")
                   or self.finding.get("resource_id") or "Finding")

    def in_words(self) -> str:
        """The explanation for someone who does not read AWS resource ids.

        Deterministic and assembled from what the finding already proved. No
        model call: this text is the trust surface, and a fluent invented
        sentence here costs more than it saves.
        """
        bits = []
        why = str(self.finding.get("why") or self.finding.get("reason") or "").strip()
        if why:
            bits.append(why.rstrip("."))

        amount = self.monthly_usd
        if amount is not None:
            bits.append(f"It costs ${amount:,.0f} a month")
        else:
            band = self.finding.get("magnitude") or magnitude_band(
                self.finding.get("rough_monthly"))
            bits.append(f"The amount is unconfirmed, roughly {band}")

        bits.append(self.resource_map.summary().rstrip("."))
        return ". ".join(b for b in bits if b) + "."

    def to_dict(self) -> dict[str, Any]:
        return {
            "title": self.title,
            "kind": self.finding.get("kind", RECOMMENDATION),
            "monthly_usd": self.monthly_usd,
            "magnitude": self.finding.get("magnitude"),
            "in_words": self.in_words(),
            "resource_map": self.resource_map.to_dict(),
            "drafted_fix": self.drafted_fix,
            "verification": self.verification,
            "rank_score": round(self.rank_score, 2),
            "why_this_rank": self.why_this_rank,
            "critique": self.finding.get("critique"),
            "resource_id": self.finding.get("resource_id", ""),
            "source": self.finding.get("source", ""),
        }


@dataclass
class Brief:
    generated_at: str
    items: list[BriefItem] = field(default_factory=list)
    # Everything the run could NOT do, named. A brief that hides its own gaps is
    # how "we scanned everything" becomes a lie nobody notices for a quarter.
    gaps: list[str] = field(default_factory=list)
    scanned: dict[str, Any] = field(default_factory=dict)

    @property
    def actionable(self) -> list[BriefItem]:
        return [i for i in self.items if i.is_recommendation]

    @property
    def investigations(self) -> list[BriefItem]:
        return [i for i in self.items if not i.is_recommendation]

    @property
    def total_monthly_usd(self) -> float:
        """Only dollars that survived critique. An investigation contributes
        nothing to the headline, by design."""
        return round(sum(i.monthly_usd or 0.0 for i in self.actionable), 2)

    def headline(self) -> str:
        n = len(self.actionable)
        if not n:
            if self.investigations:
                return (f"Nothing confirmed to act on. "
                        f"{len(self.investigations)} thing(s) worth a look.")
            return "Nothing new found."
        safe = [i for i in self.actionable if i.resource_map.isolated is True]
        head = f"${self.total_monthly_usd:,.0f}/mo across {n} item(s)"
        if safe:
            safe_total = sum(i.monthly_usd or 0.0 for i in safe)
            head += (f". ${safe_total:,.0f}/mo of that is on {len(safe)} resource(s) "
                     f"nothing else references")
        return head + "."

    def to_dict(self) -> dict[str, Any]:
        return {
            "generated_at": self.generated_at,
            "headline": self.headline(),
            "total_monthly_usd": self.total_monthly_usd,
            "actionable_count": len(self.actionable),
            "investigation_count": len(self.investigations),
            "items": [i.to_dict() for i in self.items],
            "gaps": list(self.gaps),
            "scanned": dict(self.scanned),
            "executed": False,
            "note": "nable drafts changes. It does not make them.",
        }


# ── the drafted fix ───────────────────────────────────────────────────────────

def _draft_fix(finding: dict, rmap: ResourceMap) -> dict[str, Any]:
    """The concrete change, or an honest statement that we cannot draft one.

    Reuses the cleanup planners so there is exactly one place that knows how to
    phrase a delete, and it is the place that also refuses to run it.
    """
    rtype = rmap.resource_type
    rid = rmap.resource_id
    region = str((finding.get("metadata") or {}).get("region")
                 or finding.get("region") or "")

    # Rightsizing carries a target type; that is a change, not a delete.
    current = (finding.get("current_type") or finding.get("instance_type")
               or (finding.get("metadata") or {}).get("instance_type"))
    target = (finding.get("recommended_type") or finding.get("target_type")
              or (finding.get("metadata") or {}).get("recommended_type"))
    if current and target:
        return {
            "summary": f"Resize {rid} from {current} to {target}.",
            "steps": [
                f"Stop {rid} during a change window.",
                f"Change the instance type to {target}.",
                f"Start {rid} and confirm the service is healthy.",
            ],
            "commands": [
                f"aws ec2 stop-instances --instance-ids {rid}"
                + (f" --region {region}" if region else ""),
                f"aws ec2 modify-instance-attribute --instance-id {rid} "
                f"--instance-type {target}" + (f" --region {region}" if region else ""),
                f"aws ec2 start-instances --instance-ids {rid}"
                + (f" --region {region}" if region else ""),
            ],
            "reversible": True,
            "caveat": "Requires a stop/start, so it needs a change window.",
        }

    # Deletes: borrow the propose-only planners, so exactly one place in the
    # codebase knows how to phrase a cleanup and it is the place that refuses
    # to run one.
    from ..cleanup.actions import draft_command

    plan = draft_command(rtype, rid, region, finding.get("metadata"))
    if plan:
        confirm = (f"Confirm nothing depends on {rid}." if rmap.isolated is not True
                   else f"nable checked every relationship it tracks: nothing references {rid}.")
        return {
            "summary": f"Delete {rid}.",
            "steps": [confirm, "Run the command below."],
            "commands": [plan["command"]],
            "reversible": False,
            "caveat": plan.get("caution", "Deletion cannot be undone."),
        }

    remediation = [str(s) for s in (finding.get("remediation") or []) if s]
    if remediation:
        return {
            "summary": remediation[0],
            "steps": remediation,
            "commands": [],
            "reversible": None,
            "caveat": "",
        }
    return {
        "summary": "No change drafted.",
        "steps": [],
        "commands": [],
        "reversible": None,
        "caveat": ("nable could not draft a specific change for this finding type. "
                   "The evidence is above; the call is yours."),
    }


def _verification(finding: dict, rmap: ResourceMap) -> str:
    """How the reader will know it worked. Every claim nable makes should end
    with the thing that would falsify it."""
    amount = _savings(finding)
    rid = rmap.resource_id or "the resource"
    if amount:
        return (f"Check next month's bill for {rid}: the line should drop by about "
                f"${amount:,.0f}. nable re-checks this against the bill and records "
                f"the measured result, not the estimate.")
    return (f"Confirm {rid}'s own billed cost before and after. nable records the "
            f"measured difference rather than repeating this estimate.")


def _rank(finding: dict, rmap: ResourceMap) -> tuple[float, str]:
    amount = _savings(finding)
    is_rec = finding.get("kind") != INVESTIGATION
    base = amount if (amount and is_rec) else (finding.get("rough_monthly") or 0.0)
    try:
        base = float(base)
    except (TypeError, ValueError):
        base = 0.0

    iso = rmap.isolated
    if iso is True:
        ease, reason = _EASE_ISOLATED, "nothing else references it"
    elif rmap.unexamined:
        # Finding one attachment is not evidence there are no others. A NAT
        # gateway whose Elastic IP we found but whose route tables we never read
        # is not a "small blast radius", it is an unknown one, and it must not
        # outrank something we fully verified.
        ease, reason = _EASE_UNKNOWN, "we could not confirm everything that depends on it"
    elif rmap.blast_radius <= 2:
        ease, reason = _EASE_SMALL_RADIUS, f"touches {rmap.blast_radius} other resource(s)"
    else:
        ease, reason = _EASE_LARGE_RADIUS, f"touches {rmap.blast_radius} other resources"

    score = base * ease
    if not is_rec:
        score *= _INVESTIGATION_PENALTY
        return score, f"unconfirmed, so it ranks below anything actionable ({reason})"

    money = f"${base:,.0f}/mo" if base else "no confirmed figure"
    return score, f"{money} and {reason}"


def build_brief(
    findings: list[dict],
    *,
    today: date | None = None,
    prober: Callable[..., list[tuple[str, str]] | None] | None = None,
    use_llm: bool | None = None,
    gaps: list[str] | None = None,
    scanned: dict[str, Any] | None = None,
    limit: int = 10,
    now: datetime | None = None,
) -> Brief:
    """Assemble the brief. Critique runs FIRST, so a retracted figure can never
    reach the ranking, the headline, or the drafted fix.

    limit: how many items reach the brief. The overflow is reported in `gaps`
    rather than silently dropped, because a truncated list that looks complete is
    how a $40k finding goes unread for a month.
    """
    reviewed = critique(list(findings or []), today=today, use_llm=use_llm)

    items: list[BriefItem] = []
    for f in reviewed:
        rmap = map_resource(f, prober=prober)
        score, why = _rank(f, rmap)
        items.append(BriefItem(
            finding=f,
            resource_map=rmap,
            drafted_fix=_draft_fix(f, rmap),
            verification=_verification(f, rmap),
            rank_score=score,
            why_this_rank=why,
        ))

    items.sort(key=lambda i: (-i.rank_score, i.title))

    all_gaps = list(gaps or [])
    if len(items) > limit:
        dropped = items[limit:]
        held = sum(i.monthly_usd or 0.0 for i in dropped)
        all_gaps.append(
            f"{len(dropped)} further finding(s) worth about ${held:,.0f}/mo are not "
            f"shown here. They are in the full report.")
        items = items[:limit]

    stamp = (now or datetime.now(timezone.utc)).isoformat()
    return Brief(generated_at=stamp, items=items, gaps=all_gaps,
                 scanned=dict(scanned or {}))
