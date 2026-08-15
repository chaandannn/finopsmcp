"""Where cost data comes from, and when Cost Explorer is worth paying for.

nable prefers the billing export each cloud already produces: AWS Cost and Usage
Report in S3 queried through Athena, Azure cost exports under a service
principal, GCP billing export in BigQuery under a service account. Provisioned
once by a template, read cheaply afterwards, and carrying line items — which
resource, which tag, which hour. The resource map, cost-to-code blame and
bill-verified savings all need that detail, and Cost Explorer will never have it.

COST EXPLORER STAYS. It is the reason somebody can point nable at credentials
they already have and get a real number in a minute, with no stack to deploy and
no 24-hour wait for the first report. That on-ramp is worth more than the
per-request charge, and removing it would trade a working first impression for
architectural tidiness.

What it is not worth is calling it when we do not have to. Three rules:

  1. If the billing export is provisioned, CE is never called. The export answers
     the same question with more detail and without the per-request charge, so
     reaching for CE anyway is pure waste on the customer's bill.
  2. For an interactive question a person just asked, with no export configured,
     CE is used. That is the on-ramp, and one request is the right price for an
     answer somebody is waiting for.
  3. In unattended paths — the overnight run, scheduled jobs, the test suite — CE
     is never called. A per-request charge nobody is watching, repeating on a
     timer, is the case that actually costs real money and the one nobody
     consented to.

NABLE_NO_COST_EXPLORER=1 forbids it everywhere, for organisations that want the
guarantee in writing.

`ce_client()` is the ONLY place in the package permitted to construct a Cost
Explorer client, so the rules above live in exactly one place.
tests/test_billing_access.py enforces that with a ratchet over the whole source
tree: the list of legacy direct constructions may shrink and may never grow.
"""
from __future__ import annotations

import contextlib
import contextvars
import logging
import os
from dataclasses import dataclass
from typing import Any

log = logging.getLogger("finops.billing_access")

# Whether the code running right now is unattended. Set once, at the point a
# scheduled job starts, and inherited by everything it calls.
#
# A contextvar rather than a parameter because the alternative is threading an
# `unattended` flag through every function between the cron and the client, and
# the one call site somebody forgets is the one that bills the customer forever.
# This was not hypothetical: AWSConnector._make_client called ce_client() only
# to trip the kill switch, then built its own client and never passed the flag,
# so the nightly snapshot billed every customer while the module that forbids
# exactly that sat one import away.
#
# contextvars, not a global, because they are per-thread and per-task: APScheduler
# runs jobs in a pool, and a global would leak "unattended" into an interactive
# question that happened to share a thread.
_UNATTENDED: contextvars.ContextVar[bool] = contextvars.ContextVar(
    "nable_unattended", default=False)


@contextlib.contextmanager
def unattended_context():
    """Mark everything inside as scheduled work nobody is watching.

    Wrap the job, not the call: the point is that a path nobody thought about
    is covered by default rather than by remembering.
    """
    token = _UNATTENDED.set(True)
    try:
        yield
    finally:
        _UNATTENDED.reset(token)


@contextlib.contextmanager
def attended_context():
    """The escape hatch, for work a person explicitly asked for from inside a
    scheduled process. Rare and deliberate; there is no way to do it by accident."""
    token = _UNATTENDED.set(False)
    try:
        yield
    finally:
        _UNATTENDED.reset(token)


def in_unattended_context() -> bool:
    return _UNATTENDED.get()

# The opt-OUT. Cost Explorer is available by default because it is the on-ramp;
# this is for organisations that want a hard guarantee it is never called.
FORBID_CE_ENV = "NABLE_NO_COST_EXPLORER"

AWS = "aws"
AZURE = "azure"
GCP = "gcp"


class BillingAccessError(RuntimeError):
    """Raised when cost data is requested and no billing export is provisioned."""


@dataclass(frozen=True)
class AccessPath:
    """How a provider's billing export gets provisioned, in the operator's terms."""
    provider: str
    mechanism: str          # what they deploy or create
    artifact: str           # the file or console path that does it
    lead_time: str          # honest: none of these are instant
    env_keys: tuple[str, ...]   # what must be set once it exists

    def as_instructions(self) -> list[str]:
        return [
            f"Deploy {self.mechanism} ({self.artifact}).",
            f"{self.lead_time}",
            f"Set: {', '.join(self.env_keys)}.",
        ]


ACCESS_PATHS: dict[str, AccessPath] = {
    AWS: AccessPath(
        provider=AWS,
        mechanism="the nable CUR CloudFormation stack",
        artifact="templates/aws-cur-setup.yaml",
        lead_time="AWS delivers the first report within 24 hours of the stack completing.",
        env_keys=("CUR_S3_BUCKET", "CUR_ATHENA_DATABASE", "CUR_ATHENA_TABLE",
                  "CUR_ATHENA_RESULTS_BUCKET"),
    ),
    AZURE: AccessPath(
        provider=AZURE,
        mechanism="a service principal with Cost Management Reader",
        artifact="Azure portal: App registrations, then a scheduled cost export",
        lead_time="The first export lands on the next scheduled run, usually within 24 hours.",
        env_keys=("AZURE_TENANT_ID", "AZURE_CLIENT_ID", "AZURE_CLIENT_SECRET",
                  "AZURE_SUBSCRIPTION_ID"),
    ),
    GCP: AccessPath(
        provider=GCP,
        mechanism="a service account plus BigQuery billing export",
        artifact="GCP console: Billing, then Billing export, then BigQuery export",
        lead_time="BigQuery billing export begins populating within 24 hours.",
        env_keys=("GOOGLE_APPLICATION_CREDENTIALS", "GCP_BQ_BILLING_TABLE"),
    ),
}


def _env(name: str) -> str:
    return (os.getenv(name) or "").strip()


def provisioned(provider: str) -> bool:
    """Is this provider's billing export set up and readable?

    Deliberately checks the same thing the reader will use, not a proxy for it.
    A credential that can list subscriptions is not a billing export.
    """
    p = (provider or "").strip().lower()
    if p == AWS:
        try:
            from .connectors.cur import is_configured
        except Exception:                       # pragma: no cover - import guard
            return False
        return bool(is_configured())
    if p == AZURE:
        # The service principal is necessary but not sufficient; a cost export
        # must exist. We can only see the principal locally, so this is the
        # honest floor, and the reader reports the rest.
        return all(_env(k) for k in ("AZURE_TENANT_ID", "AZURE_CLIENT_ID",
                                     "AZURE_SUBSCRIPTION_ID"))
    if p == GCP:
        return bool(_env("GCP_BQ_BILLING_TABLE")) and bool(
            _env("GOOGLE_APPLICATION_CREDENTIALS") or _env("GCP_SERVICE_ACCOUNT_KEY_PATH"))
    return False


def missing_setup(provider: str) -> AccessPath | None:
    """The setup step this provider still needs, or None when it is provisioned.

    An unknown provider RAISES rather than returning None. Returning None would
    make "nothing is missing" and "I do not know what this is" the same answer,
    and a typo'd provider name would read as fully provisioned. Same failure
    shape as an unchecked dependency reading as safe to delete.
    """
    p = (provider or "").strip().lower()
    if p not in ACCESS_PATHS:
        raise ValueError(
            f"unknown provider {provider!r}; known: {', '.join(sorted(ACCESS_PATHS))}")
    return None if provisioned(p) else ACCESS_PATHS[p]


def cost_explorer_forbidden() -> bool:
    """True when an operator has banned Cost Explorer outright.

    Only the exact opt-out strings count. Anything unrecognised leaves CE
    available, because failing closed here would silently break the on-ramp for
    somebody who typed the variable wrong.
    """
    return _env(FORBID_CE_ENV).lower() in ("1", "true", "yes")


def cost_explorer_allowed(*, unattended: bool | None = None) -> bool:
    """May we call Cost Explorer right now?

    unattended: True for the overnight run, scheduled jobs, and anything else
        nobody is sitting in front of. A per-request charge on a timer is the
        case that actually costs money, so it is never allowed there. Left as
        None it is read from the ambient context, which is what makes the rule
        apply to paths nobody remembered to annotate.
    """
    if cost_explorer_forbidden():
        return False
    if unattended is None:
        unattended = in_unattended_context()
    return not unattended


def should_use_cost_explorer(provider: str = AWS, *, unattended: bool | None = None) -> bool:
    """The whole policy in one call: is CE both permitted AND necessary?

    Returns False when the billing export is provisioned, because the export
    answers the same question with more detail and no per-request charge.
    Calling CE anyway would be waste on the customer's bill, which is the actual
    problem — not Cost Explorer itself.
    """
    if provisioned(provider):
        return False
    return cost_explorer_allowed(unattended=unattended)


def ce_client(session: Any = None, *, region: str | None = None,
              config: Any = None, reason: str = "",
              unattended: bool | None = None, **client_kwargs: Any) -> Any:
    """The only permitted Cost Explorer client in this package.

    Raises BillingAccessError when policy says this call should not happen: the
    operator banned CE, or it is an unattended path where a recurring
    per-request charge would accrue with nobody watching. The exception text is
    what a user reads, so it names the fix rather than the rule.

    unattended defaults to the ambient context (see unattended_context), so a
    scheduled path is refused whether or not its author thought about it.
    Passing it explicitly still wins, in both directions.

    client_kwargs pass through to the boto3 client, which is what lets the
    assume-role path build a credentialled client here rather than doing it
    itself and slipping the gate.
    """
    if cost_explorer_forbidden():
        raise BillingAccessError(
            f"Cost Explorer is disabled here ({FORBID_CE_ENV}=1). "
            + " ".join(ACCESS_PATHS[AWS].as_instructions())
        )
    if unattended is None:
        unattended = in_unattended_context()
    if unattended:
        raise BillingAccessError(
            "Cost Explorer is not used in scheduled or background work: it bills "
            "per request, and a charge that repeats on a timer with nobody "
            "watching is the one nobody agreed to. Configure the billing export "
            "and this runs for free. "
            + " ".join(ACCESS_PATHS[AWS].as_instructions())
        )
    log.debug("Cost Explorer call (billed per request to your AWS account)%s",
              f": {reason}" if reason else "")
    import boto3

    region = region or os.getenv("AWS_DEFAULT_REGION", "us-east-1")
    kwargs: dict[str, Any] = {"region_name": region, **client_kwargs}
    if config is not None:
        kwargs["config"] = config
    if session is not None:
        return session.client("ce", **kwargs)
    return boto3.client("ce", **kwargs)


def unavailable(provider: str = AWS, *, question: str = "") -> dict[str, Any]:
    """The structured answer when cost data is asked for and nothing is provisioned.

    Never a zero and never an empty list. A cost tool that returns $0 because it
    could not read the bill is worse than one that refuses: somebody will believe
    it. This says what is missing, what to deploy, and how long it takes.
    """
    path = ACCESS_PATHS.get((provider or "").strip().lower())
    if path is None:
        return {"error": "unknown_provider", "provider": provider}
    return {
        "error": "billing_export_not_configured",
        "provider": path.provider,
        "message": (
            f"No {path.provider.upper()} billing export is configured, so nable "
            f"cannot answer this from measured data."
            + (f" Question was: {question}" if question else "")
        ),
        "setup": path.as_instructions(),
        "mechanism": path.mechanism,
        "artifact": path.artifact,
        "lead_time": path.lead_time,
        "required_env": list(path.env_keys),
        # Said out loud so nobody reads the refusal as "nable found nothing".
        "note": ("This is a missing setup step, not a finding of zero spend. "
                 "Resource-level waste scanning works today without it: run "
                 "`nable scan`."),
    }


def require(provider: str = AWS, *, question: str = "") -> dict[str, Any] | None:
    """Guard for a cost-answering code path.

    Returns None when the provider is provisioned (carry on), or the structured
    unavailable() payload to return to the caller. Used as:

        if err := billing_access.require("aws"):
            return err
    """
    return None if provisioned(provider) else unavailable(provider, question=question)
