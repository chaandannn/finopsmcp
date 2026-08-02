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
import time
from dataclasses import dataclass, field

# These SDKs probe metadata endpoints that do not answer on a laptop. A first run
# must not stall on that, and a slow probe is indistinguishable from "no creds"
# for our purposes, so cap it and move on.
PROBE_TIMEOUT_S = 6.0

# Probing is expensive and the answer barely changes within a session. GCP is the
# worst case: with no ADC file, google.auth.default() blocks the full timeout
# probing a GCE metadata server that is not there, so an uncached
# GCPConnector.is_configured() costs ~6s EVERY call. is_configured is called from
# hot paths (tool dispatch, demo detection, the connected-provider surface), so
# without this the cost lands on every tool call. Matches the 30s cache already
# used by demo_data._real_provider_connected and tool_surface.connected_families.
CACHE_TTL_S = 30.0
_cache: dict[str, tuple[float, "Ambient"]] = {}


def reset_cache() -> None:
    """Drop memoised probes. Call after a connect so the new credential is seen
    immediately rather than up to CACHE_TTL_S later, and between tests."""
    _cache.clear()


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


class _quiet_sdk_loggers:
    """Silence the cloud SDK loggers while ANY probe is running.

    azure-identity logs its ENTIRE failed credential chain at WARNING when
    DefaultAzureCredential finds nothing, which is the normal case on almost
    every machine. With no logging config (a CLI), Python's last-resort handler
    prints all ~25 lines to stderr, so onboarding's "Step 3, see your first
    number" opened with a wall of Azure SDK diagnostics. Dogfooded 2026-08-01:
    1.7KB of error text on a clean machine. "Nothing found" is an answer here,
    not an incident, so nothing below ERROR leaves a probe.

    Logger levels are process-global and detect_all runs its probes
    CONCURRENTLY, so this is refcounted: the first probe in silences, the last
    probe out restores. A naive per-probe save/restore let the fastest probe
    (usually AWS) restore the levels while Azure was still mid-chain, which
    re-leaked the noise on a timing roll of the dice; the subprocess test
    caught it failing at ~1.3s and passing at ~2.3s.
    """
    _NAMES = ("azure", "azure.identity", "azure.core", "google", "google.auth",
              "urllib3", "msal")
    _lock = None   # created lazily so module import stays trivially cheap
    _depth = 0
    _saved: list = []

    def __enter__(self):
        import logging
        import threading

        cls = type(self)
        if cls._lock is None:
            cls._lock = threading.Lock()
        with cls._lock:
            if cls._depth == 0:
                cls._saved = []
                for name in cls._NAMES:
                    lg = logging.getLogger(name)
                    cls._saved.append((lg, lg.level, lg.propagate))
                    lg.setLevel(logging.CRITICAL)
                    lg.propagate = False
            cls._depth += 1
        return self

    def __exit__(self, *exc):
        cls = type(self)
        with cls._lock:
            cls._depth -= 1
            if cls._depth == 0:
                for lg, level, prop in cls._saved:
                    lg.setLevel(level)
                    lg.propagate = prop
                cls._saved = []
        return False


def _run(fn, timeout: float = PROBE_TIMEOUT_S):
    """Call fn with a hard timeout, swallowing everything. A probe that hangs or
    explodes is just 'nothing found'; it must never take onboarding with it."""
    def _quietly():
        with _quiet_sdk_loggers():
            return fn()

    try:
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as ex:
            return ex.submit(_quietly).result(timeout=timeout)
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
        why = ""
        if not subs:
            # ARM REST rather than an SDK. The obvious choice,
            # azure.mgmt.resource.SubscriptionClient, does not exist: Azure moved
            # it out of azure-mgmt-resource (26.0.0 ships only `resources`) into a
            # separate azure-mgmt-resource-subscriptions package that is on 1.0.0.
            # Pinning either would re-create the packaging fragility this whole
            # fix exists to remove. azure-identity mints the token and httpx is
            # already a core dependency, so this needs nothing new and cannot
            # break on an SDK reorganisation.
            try:
                import httpx
                token = cred.get_token("https://management.azure.com/.default").token
                resp = httpx.get(
                    "https://management.azure.com/subscriptions?api-version=2022-12-01",
                    headers={"Authorization": f"Bearer {token}"}, timeout=5.0)
                if resp.status_code == 200:
                    subs = [v["subscriptionId"] for v in resp.json().get("value", [])
                            if v.get("subscriptionId") and v.get("state") == "Enabled"]
                else:
                    why = f"HTTP {resp.status_code}"
            except Exception as e:
                # A real failure (expired login, no permission, offline). Keep the
                # reason: it is the difference between "run az login" and "your
                # credential cannot list subscriptions". Never swallow it silently,
                # which is how a packaging bug read as "you have no Azure".
                why = type(e).__name__
        if not subs and source != "env":
            # Nothing to prove the credential works; do not claim a connection.
            return ("no-subscriptions", None, [], why)
        return (source, cred, subs)

    r = _run(probe)
    if not r:
        return Ambient("azure", detail="no Azure credential found (try `az login`)")
    source = r[0]
    if source == "missing-sdk":
        return Ambient("azure", detail="azure SDK not installed (pip install 'finops-mcp[azure]')")
    if source == "no-subscriptions":
        extra = f" ({r[3]})" if len(r) > 3 and r[3] else ""
        return Ambient("azure", detail=f"an Azure credential was found but it can see no "
                                       f"subscriptions{extra}; try `az login` again or set "
                                       f"AZURE_SUBSCRIPTION_IDS")
    return Ambient("azure", found=True, source=source, scopes=r[2])


# ── GCP ───────────────────────────────────────────────────────────────────────

def _gcp_signals_present() -> bool:
    """Is there any local sign of GCP at all? Filesystem and env only, no network.

    Deliberately generous: a false positive costs one slow probe, a false negative
    would hide a real credential, which is the failure this whole module exists to
    remove. GCE/Cloud Run are covered by the metadata env vars their runtimes set."""
    from pathlib import Path
    if any(os.getenv(k) for k in ("GOOGLE_APPLICATION_CREDENTIALS",
                                  "GCP_SERVICE_ACCOUNT_KEY_PATH",
                                  "GOOGLE_CLOUD_PROJECT", "GCLOUD_PROJECT",
                                  "GCE_METADATA_HOST", "K_SERVICE")):
        return True
    cfg = Path(os.getenv("CLOUDSDK_CONFIG") or (Path.home() / ".config" / "gcloud"))
    try:
        return (cfg / "application_default_credentials.json").exists() or cfg.is_dir()
    except OSError:
        return False

def detect_gcp() -> Ambient:
    """google.auth.default(), which covers Application Default Credentials from
    `gcloud auth application-default login`, plus GCE/Cloud Run metadata. The old
    check only looked at GOOGLE_APPLICATION_CREDENTIALS, so the single most common
    developer setup did not register."""
    env_accts = [b.strip() for b in os.getenv("GCP_BILLING_ACCOUNT_IDS", "").split(",") if b.strip()]

    # Cheap negative first. google.auth.default() blocks the full timeout probing
    # a GCE metadata server that is not there, so on the very common "this laptop
    # has nothing to do with GCP" machine the expensive call is pure latency in
    # the first-run path. A few stat() calls answer it instead.
    if not (_gcp_signals_present() or env_accts):
        return Ambient("gcp", detail="no GCP credential found (try `gcloud auth application-default login`)")

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


def _memoized(provider: str):
    """The probe for `provider`, wrapped so a repeat call inside CACHE_TTL_S is free."""
    def run() -> Ambient:
        now = time.monotonic()
        hit = _cache.get(provider)
        if hit and hit[0] > now:
            return hit[1]
        result = _RAW_PROBES[provider]()
        _cache[provider] = (now + CACHE_TTL_S, result)
        return result
    run.__name__ = f"detect_{provider}_cached"
    return run


_RAW_PROBES = {"aws": detect_aws, "azure": detect_azure, "gcp": detect_gcp}

# Adding a cloud means adding it to _RAW_PROBES. Nothing else needs to know the
# difference, and everything gets the cache for free.
PROBES = {name: _memoized(name) for name in _RAW_PROBES}


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
