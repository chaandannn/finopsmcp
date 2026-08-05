"""Provisioned billing access. Cost Explorer is not a data source.

nable reads cost from the billing export each cloud already produces: AWS Cost
and Usage Report in S3 queried through Athena, Azure cost exports under a
service principal, GCP billing export in BigQuery under a service account. Each
is provisioned once, by a template, and read for free afterwards.

WHY COST EXPLORER IS OFF BY DEFAULT. Two reasons, and the second is the one that
matters:

  1. Every GetCostAndUsage request bills the account it runs in. A cost tool that
     adds a recurring line to the customer's own bill, on a schedule, has an
     obvious credibility problem.
  2. Cost Explorer aggregates. It cannot tell you which volume, which instance,
     which line item. Everything nable is actually good at — mapping a finding to
     the resources around it, blaming a cost on the code that created it,
     verifying a saving against the bill it claimed — needs line items. CUR has
     them; Cost Explorer never will. Building on CE caps the product.

So CE is not a fallback that quietly fills gaps. It is off, it stays off unless
an operator explicitly turns it on for a one-off, and the absence of billing
access is reported as a setup step with the template to deploy, not as an empty
answer or a zero.

`ce_client()` is the ONLY place in the package permitted to construct a Cost
Explorer client. tests/test_billing_access.py enforces that with a ratchet over
the whole source tree: the list of legacy direct constructions may shrink and may
never grow.
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from typing import Any

log = logging.getLogger("finops.billing_access")

# The one opt-out. Documented, off by default, and deliberately verbose to type:
# nobody sets this by accident, and reading it in a stack trace tells you exactly
# what happened.
ALLOW_CE_ENV = "NABLE_ALLOW_COST_EXPLORER"

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


def cost_explorer_allowed() -> bool:
    """False unless an operator explicitly opted in for this process.

    Accepts only the exact opt-in strings. "0", "false", "no" and anything
    unrecognised mean off, so a half-configured environment fails closed.
    """
    return _env(ALLOW_CE_ENV).lower() in ("1", "true", "yes")


def ce_client(session: Any = None, *, region: str | None = None,
              config: Any = None, reason: str = "") -> Any:
    """The only permitted Cost Explorer client in this package.

    Raises BillingAccessError unless the operator opted in. The exception text is
    the message a user sees, so it names the fix rather than the rule.
    """
    if not cost_explorer_allowed():
        raise BillingAccessError(
            "Cost Explorer is off. nable reads cost from your billing export, "
            "which is free to query and carries the resource-level detail Cost "
            "Explorer cannot. "
            + " ".join(ACCESS_PATHS[AWS].as_instructions())
            + f" To make a one-off Cost Explorer call anyway, set {ALLOW_CE_ENV}=1; "
              "AWS bills your account per request."
        )
    log.warning("Cost Explorer call permitted by %s (billed per request to your "
                "AWS account)%s", ALLOW_CE_ENV, f": {reason}" if reason else "")
    import boto3

    region = region or os.getenv("AWS_DEFAULT_REGION", "us-east-1")
    kwargs: dict[str, Any] = {"region_name": region}
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
