"""Every cloud answers "is there a credential here" the same way.

nable was AWS-first by accident. AWS asked boto3, which resolves the whole
default chain (env, ~/.aws/credentials, profiles, SSO, IMDS, ECS roles). Azure
asked only whether three service-principal env vars were set. GCP asked only
whether GOOGLE_APPLICATION_CREDENTIALS was set.

The effect on a real person: an engineer with `~/.aws/credentials` connected in
three seconds, while an engineer who had run `az login` or
`gcloud auth application-default login` (the normal state in those clouds) read
as unconfigured and was pushed through a manual service-principal wizard. The one
user who connected on 2026-07-31 connected Azure, twice, and never saw a number.

These tests pin the parity itself, not three separate behaviours, so a fourth
cloud cannot quietly ship with a weaker check.
"""
from __future__ import annotations

import asyncio
import sys
import types

import pytest

from finops import ambient


@pytest.fixture(autouse=True)
def _no_ambient_env(monkeypatch):
    """Strip every credential env var so a developer's real cloud login cannot
    decide the result of these tests."""
    for k in ("AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "AWS_PROFILE",
              "AWS_SESSION_TOKEN", "AZURE_CLIENT_ID", "AZURE_CLIENT_SECRET",
              "AZURE_TENANT_ID", "AZURE_SUBSCRIPTION_IDS",
              "GOOGLE_APPLICATION_CREDENTIALS", "GCP_SERVICE_ACCOUNT_KEY_PATH",
              "GCP_BILLING_ACCOUNT_IDS"):
        monkeypatch.delenv(k, raising=False)
    # Probes are memoized per process, so without this one test's result decides
    # the next one's.
    ambient.reset_cache()
    yield
    ambient.reset_cache()


# ── the parity property ───────────────────────────────────────────────────────

def test_every_cloud_has_a_probe():
    """The registry is the parity guarantee. A new cloud added to the product but
    not here would silently keep the old env-var-only behaviour."""
    assert set(ambient.PROBES) >= {"aws", "azure", "gcp"}


@pytest.mark.parametrize("provider", ["aws", "azure", "gcp"])
def test_a_probe_never_raises(provider, monkeypatch):
    """Onboarding must survive a missing SDK, a broken config, or a hung metadata
    endpoint. Every one of those is 'nothing found', never an exception."""
    def explode(*a, **k):
        raise RuntimeError("network on fire")
    monkeypatch.setattr(ambient, "_run", lambda fn, timeout=None: explode())
    with pytest.raises(RuntimeError):
        explode()          # the stand-in really does raise
    monkeypatch.setattr(ambient, "_run", lambda fn, timeout=None: None)
    r = ambient.PROBES[provider]()
    assert r.found is False and r.provider == provider


@pytest.mark.parametrize("provider", ["azure", "gcp"])
def test_a_missing_optional_sdk_is_reported_not_raised(provider, monkeypatch):
    """azure and google are optional extras. Not installed is a normal answer."""
    monkeypatch.setenv("GOOGLE_CLOUD_PROJECT", "p")   # get past the GCP signal pre-check
    monkeypatch.setattr(ambient, "_run", lambda fn, timeout=None: ("missing-sdk", None, [])
                        if provider == "azure" else ("missing-sdk", []))
    r = ambient.PROBES[provider]()
    assert r.found is False
    assert "not installed" in r.detail


def test_a_credential_with_no_scope_is_not_a_connection():
    """A credential that can see no subscription or billing account cannot answer
    a cost question. Counting it as connected is how a user ends up connected and
    still looking at nothing, which is exactly what happened on 2026-07-31."""
    assert ambient.Ambient("azure", found=True, scopes=[]).usable is False
    assert ambient.Ambient("gcp", found=True, scopes=[]).usable is False
    assert ambient.Ambient("azure", found=True, scopes=["sub-1"]).usable is True


def test_aws_is_usable_without_an_explicit_scope():
    """AWS is the documented exception: the account is derived from the
    credential, so there is no separate scope to discover."""
    assert ambient.Ambient("aws", found=True, scopes=[]).usable is True


# ── the behaviour that was actually broken ────────────────────────────────────

def test_azure_counts_an_az_login_not_only_a_service_principal(monkeypatch):
    """`az login` sets none of AZURE_CLIENT_ID/SECRET/TENANT_ID. Before this it
    read as unconfigured."""
    from finops.connectors.azure import AzureConnector
    monkeypatch.setitem(ambient.PROBES, "azure",
                        lambda: ambient.Ambient("azure", found=True, source="default-chain",
                                                scopes=["sub-abc"]))
    c = AzureConnector()
    assert c._subscription_ids == []                      # nothing in the env
    assert asyncio.run(c.is_configured()) is True
    assert c._subscription_ids == ["sub-abc"], "discovered scope must be adopted"


def test_gcp_counts_application_default_credentials(monkeypatch):
    """`gcloud auth application-default login` sets no env var at all."""
    from finops.connectors.gcp import GCPConnector
    monkeypatch.setitem(ambient.PROBES, "gcp",
                        lambda: ambient.Ambient("gcp", found=True, source="adc",
                                                scopes=["01ABCD-2345"]))
    c = GCPConnector()
    assert c._billing_account_ids == []
    assert asyncio.run(c.is_configured()) is True
    assert c._billing_account_ids == ["01ABCD-2345"]


@pytest.mark.parametrize("provider,cls_path,probe", [
    ("azure", "finops.connectors.azure.AzureConnector", "azure"),
    ("gcp", "finops.connectors.gcp.GCPConnector", "gcp"),
])
def test_no_credential_anywhere_is_still_unconfigured(monkeypatch, provider, cls_path, probe):
    """The fix must not turn 'no credentials' into a false positive."""
    monkeypatch.setitem(ambient.PROBES, probe, lambda: ambient.Ambient(provider))
    mod, name = cls_path.rsplit(".", 1)
    cls = getattr(__import__(mod, fromlist=[name]), name)
    assert asyncio.run(cls().is_configured()) is False


def test_the_service_principal_path_still_works(monkeypatch):
    """Explicit env config must not regress, and must not pay for a probe."""
    from finops.connectors.azure import AzureConnector
    for k, v in (("AZURE_CLIENT_ID", "id"), ("AZURE_CLIENT_SECRET", "sec"),
                 ("AZURE_TENANT_ID", "ten"), ("AZURE_SUBSCRIPTION_IDS", "sub-1")):
        monkeypatch.setenv(k, v)
    probed = []
    monkeypatch.setitem(ambient.PROBES, "azure",
                        lambda: probed.append(1) or ambient.Ambient("azure"))
    assert asyncio.run(AzureConnector().is_configured()) is True
    assert not probed, "explicit env config should short-circuit before probing"


# ── detect_all ────────────────────────────────────────────────────────────────

def test_detect_all_always_returns_every_provider(monkeypatch):
    """A caller iterating the result must never KeyError because one probe hung."""
    monkeypatch.setattr(ambient, "PROBES", {
        "aws": lambda: ambient.Ambient("aws", found=True),
        "azure": lambda: (_ for _ in ()).throw(RuntimeError("boom")),
        "gcp": lambda: ambient.Ambient("gcp"),
    })
    out = ambient.detect_all()
    assert set(out) == {"aws", "azure", "gcp"}
    assert out["aws"].found is True
    assert out["azure"].found is False      # exploded, reported as not found


# ── cost: probing must not land on every call ────────────────────────────────

def test_a_probe_result_is_memoized(monkeypatch):
    """GCP is the reason this exists: with no ADC file, google.auth.default()
    blocks the full 6s timeout probing a metadata server that is not there. That
    used to be paid on EVERY is_configured() call, which is a hot path (tool
    dispatch, demo detection, the connected-provider surface). It took the test
    suite from ~70s to over 600s before this cache went in."""
    calls = []
    monkeypatch.setattr(ambient, "_RAW_PROBES",
                        {"gcp": lambda: calls.append(1) or ambient.Ambient("gcp")})
    monkeypatch.setattr(ambient, "PROBES", {"gcp": ambient._memoized("gcp")})
    ambient.reset_cache()
    for _ in range(5):
        ambient.PROBES["gcp"]()
    assert len(calls) == 1, f"probed {len(calls)} times; the cache is not holding"


def test_reset_cache_forces_a_fresh_probe(monkeypatch):
    """A connect has to be visible immediately, not up to CACHE_TTL_S later."""
    calls = []
    monkeypatch.setattr(ambient, "_RAW_PROBES",
                        {"aws": lambda: calls.append(1) or ambient.Ambient("aws")})
    monkeypatch.setattr(ambient, "PROBES", {"aws": ambient._memoized("aws")})
    ambient.reset_cache()
    ambient.PROBES["aws"]()
    ambient.reset_cache()
    ambient.PROBES["aws"]()
    assert len(calls) == 2


def test_gcp_skips_the_slow_probe_when_nothing_local_suggests_gcp(monkeypatch, tmp_path):
    """The expensive SDK call is pure latency on a laptop with no GCP."""
    monkeypatch.setenv("CLOUDSDK_CONFIG", str(tmp_path / "absent"))
    for k in ("GOOGLE_APPLICATION_CREDENTIALS", "GCP_SERVICE_ACCOUNT_KEY_PATH",
              "GOOGLE_CLOUD_PROJECT", "GCLOUD_PROJECT", "GCE_METADATA_HOST", "K_SERVICE"):
        monkeypatch.delenv(k, raising=False)
    probed = []
    monkeypatch.setattr(ambient, "_run", lambda fn, timeout=None: probed.append(1))
    r = ambient.detect_gcp()
    assert r.found is False
    assert not probed, "ran the expensive probe with no local sign of GCP"


@pytest.mark.parametrize("env", ["GOOGLE_APPLICATION_CREDENTIALS", "K_SERVICE", "GOOGLE_CLOUD_PROJECT"])
def test_gcp_still_probes_when_there_is_any_sign_of_gcp(monkeypatch, tmp_path, env):
    """A false negative would hide a real credential, which is the exact failure
    this module exists to remove. Err toward probing."""
    monkeypatch.setenv("CLOUDSDK_CONFIG", str(tmp_path / "absent"))
    monkeypatch.setenv(env, "something")
    probed = []
    monkeypatch.setattr(ambient, "_run", lambda fn, timeout=None: probed.append(1) or None)
    ambient.detect_gcp()
    assert probed, f"{env} is set but nable skipped the probe"


def test_a_gcloud_config_dir_counts_as_a_signal(monkeypatch, tmp_path):
    cfg = tmp_path / "gcloud"; cfg.mkdir()
    monkeypatch.setenv("CLOUDSDK_CONFIG", str(cfg))
    for k in ("GOOGLE_APPLICATION_CREDENTIALS", "K_SERVICE", "GOOGLE_CLOUD_PROJECT"):
        monkeypatch.delenv(k, raising=False)
    assert ambient._gcp_signals_present() is True
