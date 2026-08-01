"""
critique(): try to refute a recommendation before a human ever sees it.

Every other module in this package is trying to FIND savings. This one is trying to
prove them wrong, and it runs last. The reason is narrow and commercial: the first
recommendation a new install shows is the whole trust relationship. A confident
"$1,200/mo, downsize this" that turns out to assume 730 hours on an instance
launched last Tuesday does not read as a rounding error, it reads as a tool that
makes things up, and nobody audits the second one.

Two layers, in this order:

  1. FALSIFIERS (deterministic, always on, no network, no LLM). Arithmetic and
     provenance traps that make a claim plainly wrong: a full month billed on a
     resource that has not existed for a full month, savings larger than the
     resource's own spend, a three-year commitment argued from nine days of data.
     These are cheap, reproducible, and they are the guarantee.

  2. LLM OBJECTIONS (opt-in, gated, off by default). Judgment a rule cannot encode:
     the workload is idle on average because it is a month-end batch job, the peer
     you priced against is in a cheaper region, this instance is small because it
     fronts something that is not. The model is prompted to REFUTE, never to agree,
     and it is only reached for claims big enough to be worth the tokens.

Propose-only, like `rescorer`: critique NEVER deletes a recommendation and never
touches a cloud API. The strongest thing it can do is downgrade a "recommendation"
to an "investigation" through the existing trust envelope, which strips the precise
dollar figure and asks the human to confirm. That is the correct outcome for a
claim we can no longer stand behind: keep the signal, drop the false precision.
"""
from __future__ import annotations

import os
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, timezone
from typing import Any

from .envelope import INVESTIGATION, RECOMMENDATION, magnitude_band

# A month of billable hours. Every "save $X/mo" claim is implicitly this number
# times an hourly delta, which is exactly why a resource younger than a month is
# the most common way for a claim to be silently wrong.
HOURS_PER_MONTH = 730.0

# Below this, an LLM call costs more attention than the claim is worth. The
# deterministic falsifiers still run on every recommendation regardless.
DEFAULT_LLM_FLOOR_USD = 500.0

# Commitment terms are 12 or 36 months. Arguing one from a fortnight of usage is
# not a judgment call, it is insufficient evidence.
MIN_DAYS_FOR_COMMITMENT = 30

# Every field name a scanner has used for "dollars per month saved". The audit
# path calls it `monthly_savings`; the ledger calls it
# `estimated_monthly_savings_usd`. A critic that reads only one of them sees no
# claim at all and passes everything, which is the most dangerous way for this
# module to fail: it reports a clean bill of health it never earned. Callers
# that know their key pass it as `savings_key` and it is tried first.
SAVINGS_KEYS = ("estimated_monthly_savings_usd", "estimated_monthly_savings",
                "est_monthly_savings", "monthly_savings_usd", "monthly_savings")


@dataclass
class Objection:
    """One reason a recommendation might be wrong.

    `blocking` is the load-bearing field. A blocking objection means the claim as
    stated cannot be true, so the finding is downgraded to an investigation. A
    non-blocking objection is a caveat: worth showing the human, not worth
    retracting the number over.
    """
    code: str
    detail: str                 # plain English, names the specific conflict
    blocking: bool = False
    source: str = "falsifier"   # "falsifier" (deterministic) | "llm"

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


# ── layer 1: deterministic falsifiers ────────────────────────────────────────
#
# Each one answers a question of the form "is there something in the evidence that
# contradicts the claim on its face". They read only the recommendation dict; they
# never call a cloud API, so they are free and run on everything.

def _f(rec: dict, *keys: str) -> float | None:
    """First finite numeric value among keys, or None. Recommendation dicts come
    from a dozen scanners with drifting field names, and a critic that silently
    reads nothing is worse than no critic: it prints a clean bill of health it
    never earned. Returning None keeps 'absent' distinct from 'zero'.

    Non-finite values are treated as absent here and blocked by the garbage
    falsifier below: every comparison against NaN is False, so a NaN claim
    would otherwise sail through every check while rendering as "$nan"."""
    import math

    for k in keys:
        if k not in rec:
            continue
        v = rec.get(k)
        if v is None or isinstance(v, bool):
            continue
        try:
            f = float(v)
        except (TypeError, ValueError):
            continue
        if math.isfinite(f):
            return f
    return None


def _claim_is_garbage(rec: dict, keys: tuple) -> str | None:
    """The raw claimed value when it exists but is not a finite number, else None.
    Distinct from _f returning None: absent is fine, present-and-NaN is a claim
    we must refuse to print."""
    import math

    for k in keys:
        v = rec.get(k)
        if v is None or isinstance(v, bool):
            continue
        try:
            f = float(v)
        except (TypeError, ValueError):
            if isinstance(v, str) and v.strip():
                return v  # a non-numeric string where a dollar figure belongs
            continue
        if not math.isfinite(f):
            return repr(v)
    return None


def _age_days(rec: dict, *, today: date | None = None) -> float | None:
    """Age of the resource in days, from whichever launch/creation field it carries."""
    raw = None
    for k in ("launch_time", "launched_at", "created_at", "creation_date", "start_time"):
        if rec.get(k):
            raw = rec[k]
            break
    if raw is None:
        meta = rec.get("metadata")
        if isinstance(meta, dict):
            for k in ("launch_time", "launched_at", "created_at"):
                if meta.get(k):
                    raw = meta[k]
                    break
    if raw is None:
        return None

    ref = today or date.today()
    if isinstance(raw, datetime):
        started = raw.date()
    elif isinstance(raw, date):
        started = raw
    else:
        text = str(raw).strip().replace("Z", "+00:00")
        try:
            parsed = datetime.fromisoformat(text)
        except ValueError:
            return None
        started = (parsed.astimezone(timezone.utc) if parsed.tzinfo else parsed).date()
    return float((ref - started).days)


def _claimed(rec: dict, savings_key: str | None = None) -> float | None:
    keys = (savings_key, *SAVINGS_KEYS) if savings_key else SAVINGS_KEYS
    return _f(rec, *keys)


def falsifiers(rec: dict, *, today: date | None = None,
               savings_key: str | None = None) -> list[Objection]:
    """Every deterministic objection to `rec`. Pure function, no I/O."""
    out: list[Objection] = []
    claimed = _claimed(rec, savings_key)

    # 0. A claim that exists but is not a finite number. NaN and infinity pass
    #    through arithmetic checks unchallenged (every comparison is False), so
    #    they must be refused explicitly or they print as "$nan" with a straight
    #    face.
    keys = (savings_key, *SAVINGS_KEYS) if savings_key else SAVINGS_KEYS
    garbage = _claim_is_garbage(rec, keys)
    if garbage is not None:
        out.append(Objection(
            code="claim_not_a_number",
            detail=f"the claimed saving is {garbage}, which is not a finite dollar "
                   f"figure and cannot be shown as one.",
            blocking=True,
        ))

    # 1. A full month billed against a resource that has not existed for one.
    #    The single most common way a savings figure is quietly inflated.
    age = _age_days(rec, today=today)
    # A launch date in the future is clock skew or bad data; either way the
    # resource has zero observed history, which is the strongest version of
    # "too new for a monthly claim", not an exemption from it.
    if age is not None and age < 0:
        age = 0.0
    if claimed and claimed > 0 and age is not None and 0 <= age < 30:
        realized = claimed * (max(age, 1.0) / 30.0)
        out.append(Objection(
            code="full_month_on_new_resource",
            detail=(f"the ${claimed:,.0f}/mo figure assumes a full month, but this "
                    f"resource is {age:.0f} days old. At its actual age the saving so "
                    f"far is nearer ${realized:,.0f}. It reaches the full figure only "
                    f"if it would have run all month."),
            blocking=age < 7,   # under a week there is not yet a monthly rate to claim
        ))

    # 2. You cannot save more on a resource than the resource costs.
    spend = _f(rec, "current_monthly_cost_usd", "current_monthly_cost",
               "resource_monthly_cost_usd", "monthly_cost_usd")
    if claimed and spend and spend > 0 and claimed > spend * 1.01:
        out.append(Objection(
            code="savings_exceed_resource_spend",
            detail=(f"claimed saving ${claimed:,.0f}/mo is larger than the resource's "
                    f"own ${spend:,.0f}/mo cost. A change to one resource cannot "
                    f"return more than that resource is billed."),
            blocking=True,
        ))

    # 3. A multi-year commitment argued from a handful of days.
    lookback = _f(rec, "lookback_days", "observation_days", "days_observed")
    if lookback is not None and lookback < MIN_DAYS_FOR_COMMITMENT and _is_commitment(rec):
        out.append(Objection(
            code="commitment_outlives_evidence",
            detail=(f"this commits spend for a year or more on {lookback:.0f} days of "
                    f"usage. One quiet or one busy fortnight moves the break-even, and "
                    f"the commitment cannot be undone."),
            blocking=True,
        ))

    # 4. Steady-state language on a window too short to see a weekly cycle.
    if lookback is not None and 0 < lookback < 14 and not _is_commitment(rec):
        out.append(Objection(
            code="lookback_shorter_than_a_cycle",
            detail=(f"{lookback:.0f} days does not contain a full weekly cycle, so a "
                    f"weekday-only or weekend-only workload reads as steady here."),
        ))

    # 5. Savings already claimed by an existing commitment: counting them again
    #    double-books the same dollar, and the customer finds out at renewal.
    if _truthy(rec, "covered_by_commitment", "ri_covered", "sp_covered",
               "reserved_instance_covered"):
        out.append(Objection(
            code="already_covered_by_commitment",
            detail=("this resource is already covered by a reservation or savings plan. "
                    "Removing it does not return the committed spend, so the saving is "
                    "smaller than the on-demand rate implies, and may be zero until the "
                    "commitment expires."),
            blocking=True,
        ))

    # 6. A price comparison against a peer in another region is not a comparison.
    here, there = rec.get("region"), rec.get("comparison_region")
    if here and there and here != there:
        out.append(Objection(
            code="peer_region_mismatch",
            detail=(f"the comparison is priced in {there} but the resource runs in "
                    f"{here}. Per-region pricing differs, so the delta is partly a "
                    f"region difference rather than a saving."),
            blocking=True,
        ))

    # 7. A negative or zero claim is not a recommendation at all.
    if claimed is not None and claimed <= 0:
        out.append(Objection(
            code="no_positive_saving",
            detail=f"the computed saving is ${claimed:,.2f}/mo, which is not a saving.",
            blocking=True,
        ))
    return out


def _is_commitment(rec: dict) -> bool:
    hay = " ".join(str(rec.get(k, "")) for k in ("source", "type", "waste_type", "title")).lower()
    return any(w in hay for w in ("commitment", "savings_plan", "savings plan",
                                  "reserved", "reservation", "_ri", "ri_"))


def _truthy(rec: dict, *keys: str) -> bool:
    meta = rec.get("metadata") if isinstance(rec.get("metadata"), dict) else {}
    return any(bool(rec.get(k)) or bool(meta.get(k)) for k in keys)


# ── layer 2: the LLM critic (opt-in, off by default) ─────────────────────────

CRITIC_SYSTEM = """You are reviewing a cloud cost recommendation before it is shown \
to the engineer who owns the resource. Your job is to REFUTE it, not to improve it \
and not to agree with it.

Raise an objection only when you can name the specific thing that makes the claim \
wrong or unsafe. Reasons that count:
- the utilization average hides a periodic spike (batch, month-end, business hours)
- the resource is sized for a dependency, a failover, or a licence, not its own load
- the change has a blast radius the claim does not mention
- the arithmetic or the baseline is wrong
- the evidence window cannot support the claim being made

Do NOT object that a change carries generic risk, that you lack context, or that \
the engineer should review it. Those are not objections, they are noise, and noise \
here costs more than a missed objection.

If the recommendation survives, say so. An empty objection list is a valid and \
common answer."""

_TOOL = {
    "name": "report_objections",
    "description": "Report every specific objection to the recommendation. Empty is valid.",
    "input_schema": {
        "type": "object",
        "properties": {
            "objections": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "code": {"type": "string",
                                 "description": "short snake_case label, e.g. periodic_spike_hidden_by_average"},
                        "detail": {"type": "string",
                                   "description": "one or two sentences naming the specific conflict"},
                        "blocking": {"type": "boolean",
                                     "description": "true only if the claim as stated cannot be true"},
                    },
                    "required": ["code", "detail", "blocking"],
                },
            }
        },
        "required": ["objections"],
    },
}


def llm_enabled() -> bool:
    """The LLM critic is opt-in and silent when not configured.

    Two gates, both required. NABLE_CRITIC_LLM=1 is the explicit opt-in, because
    this spends the customer's Anthropic tokens on their behalf and nothing in
    nable does that without being asked. The API key is the capability gate.
    """
    if os.getenv("NABLE_CRITIC_LLM", "").strip() not in ("1", "true", "yes"):
        return False
    return bool(os.getenv("ANTHROPIC_API_KEY", "").strip())


def _llm_objections(rec: dict, *, context: str = "") -> list[Objection]:
    """Ask the model to refute one recommendation. Never raises: a critic that
    breaks the audit is worse than no critic, so every failure yields no
    objections and the deterministic layer still stands."""
    try:
        import anthropic

        client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
        body = _prompt_payload(rec)
        if context:
            body += f"\n\nWhat this install has already learned about this environment:\n{context}"
        resp = client.messages.create(
            model=os.getenv("NABLE_CRITIC_MODEL", "claude-sonnet-5"),
            max_tokens=1024,
            system=CRITIC_SYSTEM,
            tools=[_TOOL],
            tool_choice={"type": "tool", "name": "report_objections"},
            messages=[{"role": "user", "content": body}],
        )
        for block in resp.content:
            if getattr(block, "type", "") == "tool_use" and block.name == "report_objections":
                raw = (block.input or {}).get("objections") or []
                if not isinstance(raw, list):
                    return []
                # Hard caps on count and length. The model is untrusted input
                # like any other: a miscalibrated or prompt-injected response
                # must not be able to flood the finding with unbounded text.
                return [
                    Objection(
                        code=str(o.get("code") or "llm_objection")[:64],
                        detail=str(o.get("detail") or "").strip()[:500],
                        blocking=bool(o.get("blocking")),
                        source="llm",
                    )
                    for o in raw[:10]
                    if isinstance(o, dict) and str(o.get("detail") or "").strip()
                ]
    except Exception:
        return []
    return []


# Key names that must never ride along to the model provider, wherever they
# appear. Matched as substrings against lowercased key names, because the danger
# is the whole CLASS of field, not one spelling. `kms_key_id` and friends are
# resource identifiers, not secrets, and stay allowed by the _id/_arn escape.
_SENSITIVE_KEY_HINTS = ("secret", "password", "passwd", "token", "credential",
                        "api_key", "apikey", "access_key", "private_key",
                        "session_key", "authorization", "bearer", "cookie")


def _key_is_sensitive(name: str) -> bool:
    low = str(name).lower()
    if low.endswith(("_id", "_arn", "_alias")):  # identifiers, the point of the payload
        return False
    return any(h in low for h in _SENSITIVE_KEY_HINTS)


def _safe_metadata(meta: Any) -> dict:
    """Scanner metadata, minus anything secret-shaped. Scalars only: nesting is
    where surprises hide, and no falsifier needs a nested structure to argue."""
    if not isinstance(meta, dict):
        return {}
    out = {}
    for k, v in meta.items():
        if _key_is_sensitive(k) or isinstance(v, (dict, list, tuple, set)):
            continue
        out[str(k)] = v
    return out


def _prompt_payload(rec: dict) -> str:
    """The recommendation as the critic sees it. Resource identifiers and metrics
    only. This is an allow-list, not a deny-list, because the rec dict comes from
    a dozen scanners and the critic must stay safe against fields it has never
    seen; metadata is the one open-ended field and goes through _safe_metadata."""
    keep = ("source", "title", "type", "waste_type", "why", "description", "region",
            "resource_id", "resource_type", "instance_type", "recommended_type",
            "current_monthly_cost_usd", "estimated_monthly_savings_usd",
            "estimated_monthly_savings", "cpu_avg_pct", "cpu_max_pct", "memory_avg_pct",
            "lookback_days", "environment", "environment_bucket")
    lines = [f"{k}: {rec[k]}" for k in keep if rec.get(k) not in (None, "", [], {})]
    meta = _safe_metadata(rec.get("metadata"))
    if meta:
        lines.append(f"metadata: {meta}")
    return "Recommendation under review:\n" + "\n".join(lines)


# ── the pass ─────────────────────────────────────────────────────────────────

@dataclass
class Verdict:
    survived: bool
    objections: list[Objection] = field(default_factory=list)

    @property
    def blocking(self) -> list[Objection]:
        return [o for o in self.objections if o.blocking]

    def to_dict(self) -> dict[str, Any]:
        return {
            "survived": self.survived,
            "objection_count": len(self.objections),
            "objections": [o.to_dict() for o in self.objections],
        }


def critique_one(
    rec: dict,
    *,
    use_llm: bool | None = None,
    llm_floor_usd: float = DEFAULT_LLM_FLOOR_USD,
    context: str = "",
    today: date | None = None,
    savings_key: str | None = None,
) -> Verdict:
    """Every objection to one recommendation, deterministic first."""
    objections = falsifiers(rec, today=today, savings_key=savings_key)

    want_llm = llm_enabled() if use_llm is None else use_llm
    claimed = _claimed(rec, savings_key) or 0.0
    # Skip the model when a falsifier already blocked: the claim is going to be
    # downgraded either way, and paying for a second opinion changes nothing.
    if want_llm and claimed >= llm_floor_usd and not any(o.blocking for o in objections):
        objections.extend(_llm_objections(rec, context=context))

    return Verdict(survived=not any(o.blocking for o in objections), objections=objections)


def critique(
    recs: list[dict],
    *,
    use_llm: bool | None = None,
    llm_floor_usd: float = DEFAULT_LLM_FLOOR_USD,
    context: str = "",
    today: date | None = None,
    savings_key: str | None = None,
) -> list[dict]:
    """Attach a `critique` block to every recommendation, downgrading the ones that
    did not survive. Propose-only: the list that goes out is the same length as the
    list that came in, in the same order, and nothing is deleted.

    A blocked recommendation becomes an investigation through the trust envelope:
    `kind` flips, `estimated_monthly_savings*` is cleared, and a `magnitude` band
    replaces it. The signal survives. The false precision does not.
    """
    out: list[dict] = []
    for rec in recs:
        # Idempotence: a finding this pass already retracted carries None where
        # its figure was. Re-reviewing that sees "no claim", raises nothing, and
        # would flip survived back to True while the finding still says
        # investigation. The pass already ruled; a prior verdict is final.
        prior = rec.get("critique")
        if isinstance(prior, dict) and prior.get("survived") is False:
            out.append(dict(rec))
            continue
        verdict = critique_one(rec, use_llm=use_llm, llm_floor_usd=llm_floor_usd,
                               context=context, today=today, savings_key=savings_key)
        annotated = dict(rec)  # copy: never mutate the caller's recommendation
        annotated["critique"] = verdict.to_dict()

        if not verdict.survived:
            rough = _claimed(rec, savings_key)
            annotated["kind"] = INVESTIGATION
            annotated["magnitude"] = magnitude_band(rough)
            # Clear every alias, plus the annual figures derived from them. A
            # retracted monthly claim that leaves its annual twin behind is the
            # same lie with a 12x on it.
            for k in (*( (savings_key,) if savings_key else () ), *SAVINGS_KEYS,
                      "estimated_annual_savings", "estimated_annual_savings_usd",
                      "annual_savings"):
                if k in annotated:
                    annotated[k] = None
            annotated["why_unsure"] = " ".join(o.detail for o in verdict.blocking)
            annotated["confirm_steps"] = list(rec.get("confirm_steps") or []) or [
                "Confirm the figure above against this resource's own billed cost "
                "before acting on it."
            ]
        elif "kind" not in annotated:
            annotated["kind"] = RECOMMENDATION
        out.append(annotated)
    return out
