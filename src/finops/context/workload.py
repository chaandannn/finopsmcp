# SPDX-License-Identifier: Apache-2.0
"""Is this thing production, or is it somewhere people are allowed to make a mess?

A sandbox where somebody is deliberately hammering an encoder for a week and a
production fleet quietly burning money look identical to a statistical detector.
Both are spend above a baseline. Only one of them is a problem, and only one of
them should ever receive a pull request.

Everything here reads signals the account already carries: tags, the account or
subscription name, the Kubernetes namespace, and the resource's own name. Nobody
is asked to declare anything, because the shops most likely to have this problem
are the least likely to have filled in a policy file.

The asymmetry is deliberate and is the whole safety argument:

    positive evidence of non-production  ->  suppress the action
    anything else, including unknown     ->  behave exactly as before

We never enable an action on a guess. We only ever hold one back, and only when
something in the account actually said "this is not production". An untagged
estate is therefore no worse off than it is today, which is the property that
lets this ship on by default.

`unknown` is a real answer and is kept distinct from `prod` all the way out to
the caller. A classifier that reports its guesses as facts is how "failed read
becomes a number" gets into a product, and callers need to be able to say "no
signal" rather than "production".
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

# Values that appear in an Environment/Stage tag. Ordered longest-first inside
# each class at match time so "preprod" never matches as "prod".
_NONPROD_WORDS = (
    "nonprod", "non-prod", "preprod", "pre-prod", "sandbox", "sbx", "staging",
    "stage", "development", "develop", "dev", "test", "testing", "qa", "uat",
    "demo", "scratch", "playground", "lab", "experiment", "poc", "trial",
)
_EPHEMERAL_WORDS = (
    "ephemeral", "preview", "review-app", "reviewapp", "pr-", "branch-",
    "feature-", "temp", "tmp", "throwaway", "canary-test", "loadtest",
    "load-test", "benchmark", "soak", "stress",
)
_PROD_WORDS = ("production", "prod", "prd", "live", "main")

# Tag keys that carry an environment, in the casings AWS/GCP/Azure users write.
_ENV_TAG_KEYS = (
    "environment", "env", "stage", "tier", "lifecycle", "deployment_environment",
)

# A word inside a name is only a signal when it stands alone as a token, or the
# word "development" inside "development-team-prod-api" flips a production box.
def _has_token(haystack: str, word: str) -> bool:
    if not haystack:
        return False
    return re.search(r"(?:^|[^a-z0-9])" + re.escape(word) + r"(?:[^a-z0-9]|$)",
                     haystack.lower()) is not None


def _mask_token(haystack: str, word: str) -> str:
    """Blank out a matched word so its letters cannot be counted a second time."""
    return re.sub(r"(?:^|[^a-z0-9])" + re.escape(word) + r"(?:[^a-z0-9]|$)",
                  " ", haystack.lower())


@dataclass
class WorkloadContext:
    """What we concluded, and the exact string that made us conclude it.

    `evidence` is not decoration. When a recommendation is held back, the person
    reading the dashboard has to be able to see that it was held back because the
    account is called "acme-sandbox", and to disagree with that.
    """
    kind: str                                    # prod | nonprod | ephemeral | unknown
    evidence: list[str] = field(default_factory=list)

    @property
    def is_nonprod(self) -> bool:
        """True only on positive evidence. `unknown` is deliberately not included."""
        return self.kind in ("nonprod", "ephemeral")

    def reason(self) -> str:
        if not self.evidence:
            return "nothing in the account said what this is for"
        return "; ".join(self.evidence)


def _scan(label: str, value: str) -> tuple[str, str] | None:
    """Return (kind, evidence) for what this one value says, or None for nothing.

    Two rules earn their keep here, and both came from a test.

    Ephemeral beats production outright, because "ephemeral" describes an
    activity rather than a place. A load test is a load test whether or not it
    runs in prod, and "prod-loadtest" is exactly the case that started this.

    Non-production and production in the *same* value cancel instead of one
    winning. "development-team-prod-api" is a production API owned by the
    development team, or a development box; the string genuinely does not say,
    and word order is not a specification. Returning None hands the decision to
    a stronger signal if one exists, and to "unknown" if none does, which is the
    reading that changes nobody's behaviour. Guessing either way here would put
    a fabricated fact into a decision about someone's repository.
    """
    v = (value or "").strip().lower()
    if not v:
        return None

    for word in _EPHEMERAL_WORDS:
        if _has_token(v, word) or v.startswith(word):
            return "ephemeral", f'{label} "{value}" contains "{word}"'

    nonprod = next((w for w in _NONPROD_WORDS if _has_token(v, w)), None)

    # Mask what the non-production word already claimed before looking for a
    # production one. "non-prod" and "pre-prod" contain the literal "prod", and
    # counting those same four letters twice made every hyphenated spelling
    # cancel itself into "unknown". Found by the parametrised test, which is the
    # argument for listing the spellings teams really use rather than two.
    masked = _mask_token(v, nonprod) if nonprod else v
    prod = next((w for w in _PROD_WORDS if _has_token(masked, w)), None)

    if nonprod and prod:
        return None
    if nonprod:
        return "nonprod", f'{label} "{value}" contains "{nonprod}"'
    if prod:
        return "prod", f'{label} "{value}" contains "{prod}"'
    return None


def classify(
    *,
    tags: dict | None = None,
    account_name: str | None = None,
    namespace: str | None = None,
    resource_name: str | None = None,
    cluster: str | None = None,
) -> WorkloadContext:
    """Read every signal on hand and return the strongest one.

    Signals are weighed, not counted. An explicit Environment tag beats a guess
    from a resource name, because one of them is a statement of intent and the
    other is a naming habit. When two signals of the same weight disagree, the
    non-production one wins: holding back a pull request on a production box
    costs a day, opening one against somebody's live fleet costs their trust.
    """
    ranked: list[tuple[int, str, str]] = []   # (weight, kind, evidence)

    for key, value in (tags or {}).items():
        k = str(key).strip().lower().replace("-", "_")
        if k in _ENV_TAG_KEYS:
            hit = _scan(f'tag {key}', str(value))
            if hit:
                ranked.append((3, hit[0], hit[1]))

    for weight, label, value in (
        (2, "account", account_name),
        (2, "namespace", namespace),
        (1, "cluster", cluster),
        (1, "resource name", resource_name),
    ):
        if value:
            hit = _scan(label, str(value))
            if hit:
                ranked.append((weight, hit[0], hit[1]))

    if not ranked:
        return WorkloadContext("unknown", [])

    top = max(w for w, _, _ in ranked)
    at_top = [(k, e) for w, k, e in ranked if w == top]
    # Same weight, disagreeing: the cautious reading wins.
    for want in ("ephemeral", "nonprod", "prod"):
        picked = [e for k, e in at_top if k == want]
        if picked:
            return WorkloadContext(want, picked)
    return WorkloadContext("unknown", [])


def suppression_note(ctx: WorkloadContext, action: str = "open a pull request") -> str:
    """The sentence a person reads when something was held back."""
    return (
        f"nable did not {action} here: this looks like {ctx.kind} "
        f"({ctx.reason()}). Spend moving in a place people are allowed to make a "
        f"mess is usually somebody working, not waste. Re-run with "
        f"include_nonprod=True if you want it anyway."
    )
