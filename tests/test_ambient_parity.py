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
    monkeypatch.setattr("finops.ambient.detect_azure",
                        lambda: ambient.Ambient("azure", found=True, source="default-chain",
                                                scopes=["sub-abc"]))
    c = AzureConnector()
    assert c._subscription_ids == []                      # nothing in the env
    assert asyncio.run(c.is_configured()) is True
    assert c._subscription_ids == ["sub-abc"], "discovered scope must be adopted"


def test_gcp_counts_application_default_credentials(monkeypatch):
    """`gcloud auth application-default login` sets no env var at all."""
    from finops.connectors.gcp import GCPConnector
    monkeypatch.setattr("finops.ambient.detect_gcp",
                        lambda: ambient.Ambient("gcp", found=True, source="adc",
                                                scopes=["01ABCD-2345"]))
    c = GCPConnector()
    assert c._billing_account_ids == []
    assert asyncio.run(c.is_configured()) is True
    assert c._billing_account_ids == ["01ABCD-2345"]


@pytest.mark.parametrize("provider,cls_path,probe", [
    ("azure", "finops.connectors.azure.AzureConnector", "finops.ambient.detect_azure"),
    ("gcp", "finops.connectors.gcp.GCPConnector", "finops.ambient.detect_gcp"),
])
def test_no_credential_anywhere_is_still_unconfigured(monkeypatch, provider, cls_path, probe):
    """The fix must not turn 'no credentials' into a false positive."""
    monkeypatch.setattr(probe, lambda: ambient.Ambient(provider))
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
    monkeypatch.setattr("finops.ambient.detect_azure",
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
