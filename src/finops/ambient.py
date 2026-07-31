"""Ambient cloud credential detection, the same way for every provider.

nable was AWS-first by accident, not by design. AWS asked boto3 for credentials,
which resolves the whole default chain: env vars, ~/.aws/credentials, named
profiles, SSO, IMDS, ECS task roles. Azure asked only whether three service
principal env vars were set. GCP asked only whether GOOGLE_APPLICATION_CREDENTIALS
was set.

So an engineer who had run `az login` or `gcloud auth application-default login`,
which is the normal state for anyone working in those clouds, was invisible.
An AWS user with ~/.aws/credentials connected in three seconds; an Azure user in
the identical position was sent through a manual service principal wizard.

Every provider here answers the same question the same way: is there a usable
credential on this machine, whatever kind, and what can it see. Adding a cloud
means adding a probe to PROBES, which is the point: parity is the default rather
than something each provider has to remember.

Everything degrades to "not found" rather than raising. The Azure and Google SDKs
are optional extras (finops-mcp[azure], [gcp]), so a missing import is an
ordinary answer, not an error. Every probe is time-boxed, because these SDKs
will happily block for ~9s probing a metadata server that is not there.
"""
from __future__ import annotations

import concurrent.futures
import os
from dataclasses import dataclass, field

# These SDKs probe metadata endpoints that do not answer on a laptop. A first run
# must not stall on that, and a slow probe is indistinguishable from "no creds"
# for our purposes, so cap it and move on.
PROBE_TIMEOUT_S = 6.0


@dataclass
class Ambient:
    """What one provider found on this machine."""
    provider: str
    found: bool = False
    # How the credential was obtained, for telemetry and for telling the user
    # what nable is about to use. Never contains the credential itself.
    source: str = ""
    # Subscriptions / billing accounts / account ids the credential can see.
    scopes: list[str] = field(default_factory=list)
    # Human-readable reason when nothing was found, shown only on request.
    detail: str = ""

    @property
    def usable(self) -> bool:
        """A credential with no visible scope cannot answer a cost question, so
        it is not a connection. AWS is the exception: an account id is derived
        from the credential itself."""
        return self.found and (bool(self.scopes) or self.provider == "aws")


def _run(fn, timeout: float = PROBE_TIMEOUT_S):
    """Call fn with a hard timeout, swallowing everything. A probe that hangs or
    explodes is just 'nothing found'; it must never take onboarding with it."""
    try:
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as ex:
            return ex.submit(fn).result(timeout=timeout)
    except Exception:
        return None


# ── AWS ───────────────────────────────────────────────────────────────────────

def detect_aws() -> Ambient:
    """boto3's default chain: env, shared config, profiles, SSO, IMDS, ECS."""
    def probe():
        import boto3
        creds = boto3.Session().get_credentials()
        if creds is None:
            return None
        method = getattr(creds, "method", "") or "default-chain"
        acct = ""
        try:
            acct = boto3.client("sts").get_caller_identity().get("Account", "")
        except Exception:
            pass
        return method, acct

    r = _run(probe)
    if not r:
        return Ambient("aws", detail="no credentials in the default chain")
    method, acct = r
    return Ambient("aws", found=True, source=method, scopes=[acct] if acct else [])


# ── Azure ─────────────────────────────────────────────────────────────────────

def detect_azure() -> Ambient:
    """DefaultAzureCredential, which covers what the old env-var check missed:
    `az login`, managed identity, Workload Identity, VS Code sign-in.

    Subscriptions come from AZURE_SUBSCRIPTION_IDS when set, else from asking
    Azure what this credential can see. Requiring the env var meant a valid
    `az login` still counted as unconfigured."""
    env_subs = [s.strip() for s in os.getenv("AZURE_SUBSCRIPTION_IDS", "").split(",") if s.strip()]

    def probe():
        try:
            from azure.identity import DefaultAzureCredential
        except ImportError:
            return ("missing-sdk", None, [])
        try:
            cred = DefaultAzureCredential(exclude_interactive_browser_credential=True)
        except Exception:
            return None
        subs = list(env_subs)
        source = "env" if all(os.getenv(v) for v in
                              ("AZURE_CLIENT_ID", "AZURE_CLIENT_SECRET", "AZURE_TENANT_ID")) else "default-chain"
        if not subs:
            try:
                from azure.mgmt.resource import SubscriptionClient
                subs = [s.subscription_id for s in SubscriptionClient(cred).subscriptions.list()
                        if getattr(s, "subscription_id", None)]
            except Exception:
                # A credential that cannot list subscriptions may still be able to
                # read a subscription named explicitly, so this is not fatal.
                pass
        if not subs and source != "env":
            # Nothing to prove the credential works; do not claim a connection.
            return None
        return (source, cred, subs)

    r = _run(probe)
    if not r:
        return Ambient("azure", detail="no Azure credential found (try `az login`)")
    source, _cred, subs = r
    if source == "missing-sdk":
        return Ambient("azure", detail="azure SDK not installed (pip install 'finops-mcp[azure]')")
    return Ambient("azure", found=True, source=source, scopes=subs)


# ── GCP ───────────────────────────────────────────────────────────────────────

def detect_gcp() -> Ambient:
    """google.auth.default(), which covers Application Default Credentials from
    `gcloud auth application-default login`, plus GCE/Cloud Run metadata. The old
    check only looked at GOOGLE_APPLICATION_CREDENTIALS, so the single most common
    developer setup did not register."""
    env_accts = [b.strip() for b in os.getenv("GCP_BILLING_ACCOUNT_IDS", "").split(",") if b.strip()]

    def probe():
        try:
            import google.auth
        except ImportError:
            return ("missing-sdk", [])
        try:
            creds, _project = google.auth.default()
        except Exception:
            return None
        if creds is None:
            return None
        source = "env" if os.getenv("GOOGLE_APPLICATION_CREDENTIALS") else "adc"
        accts = list(env_accts)
        if not accts:
            try:
                from google.cloud import billing_v1
                client = billing_v1.CloudBillingClient(credentials=creds)
                accts = [a.name.split("/")[-1] for a in client.list_billing_accounts()
                         if getattr(a, "open", True)]
            except Exception:
                pass
        if not accts and source != "env":
            return None
        return (source, accts)

    r = _run(probe)
    if not r:
        return Ambient("gcp", detail="no GCP credential found (try `gcloud auth application-default login`)")
    source, accts = r
    if source == "missing-sdk":
        return Ambient("gcp", detail="google SDK not installed (pip install 'finops-mcp[gcp]')")
    return Ambient("gcp", found=True, source=source, scopes=accts)


# Adding a cloud means adding it here. Nothing else needs to know the difference.
PROBES = {"aws": detect_aws, "azure": detect_azure, "gcp": detect_gcp}


def detect_all(providers: list[str] | None = None) -> dict[str, Ambient]:
    """Probe every provider at once. Parallel because three sequential 6s
    timeouts is 18 seconds of a first run spent finding nothing."""
    names = providers or list(PROBES)
    out: dict[str, Ambient] = {}
    with concurrent.futures.ThreadPoolExecutor(max_workers=len(names)) as ex:
        futures = {ex.submit(PROBES[n]): n for n in names if n in PROBES}
        for fut in concurrent.futures.as_completed(futures, timeout=PROBE_TIMEOUT_S * 2):
            n = futures[fut]
            try:
                out[n] = fut.result()
            except Exception:
                out[n] = Ambient(n, detail="probe failed")
    for n in names:
        out.setdefault(n, Ambient(n, detail="probe did not finish"))
    return out
