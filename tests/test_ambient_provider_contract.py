"""Run the real Azure and GCP probe bodies against faked provider responses.

Why this file exists, stated plainly: the parity work shipped with two Azure bugs
because every test stubbed the probes themselves. `detect_azure` was replaced
wholesale by `lambda: Ambient(...)`, so its body never ran, and neither did the
import that was broken. The tests were green and the feature was inert.

The difference here is where the seam is. Nothing below replaces a nable
function. The fakes sit at the provider boundary, the HTTP response Azure would
send and the objects the Google client would return, using their documented
shapes. Everything between that boundary and the Ambient result is our real
code: token request, URL, status handling, JSON parsing, the Enabled filter, the
usable rule, and the connector adopting discovered scopes.

That is as close to a live account as it is possible to get without one. What it
still cannot prove: that the endpoints, api-versions and field names are the ones
Azure and Google actually serve today. Only a real credential settles that, and
these payloads are copied from the published API references.
"""
from __future__ import annotations

import asyncio
import sys
import types

import pytest

from finops import ambient


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    for k in ("AZURE_CLIENT_ID", "AZURE_CLIENT_SECRET", "AZURE_TENANT_ID",
              "AZURE_SUBSCRIPTION_IDS", "GOOGLE_APPLICATION_CREDENTIALS",
              "GCP_SERVICE_ACCOUNT_KEY_PATH", "GCP_BILLING_ACCOUNT_IDS"):
        monkeypatch.delenv(k, raising=False)
    ambient.reset_cache()
    yield
    ambient.reset_cache()


# ── Azure: fake ARM at the HTTP boundary ─────────────────────────────────────

# Shape from the ARM "Subscriptions - List" reference. Trimmed to the fields we
# read, with the states we must handle: Enabled counts, the rest do not.
ARM_SUBSCRIPTIONS = {
    "value": [
        {"id": "/subscriptions/aaaa-1111", "subscriptionId": "aaaa-1111",
         "displayName": "Production", "state": "Enabled"},
        {"id": "/subscriptions/bbbb-2222", "subscriptionId": "bbbb-2222",
         "displayName": "Old", "state": "Disabled"},
        {"id": "/subscriptions/cccc-3333", "subscriptionId": "cccc-3333",
         "displayName": "Sandbox", "state": "Enabled"},
        {"id": "/subscriptions/dddd-4444", "subscriptionId": "dddd-4444",
         "displayName": "Unpaid", "state": "PastDue"},
    ]
}


class _Resp:
    def __init__(self, status, payload=None, boom=None):
        self.status_code = status
        self._payload = payload
        self._boom = boom

    def json(self):
        if self._boom:
            raise self._boom
        return self._payload


def _fake_azure(monkeypatch, resp, *, token_ok=True):
    """Install a fake azure.identity and a fake httpx.get. The probe body,
    including the token request and every line of parsing, is ours and real."""
    class _Token:
        token = "fake-arm-token"

    class _Cred:
        def __init__(self, **kw):
            self.kwargs = kw

        def get_token(self, *scopes, **kw):
            if not token_ok:
                raise RuntimeError("ClientAuthenticationError")
            return _Token()

    mod = types.ModuleType("azure.identity")
    mod.DefaultAzureCredential = _Cred
    pkg = types.ModuleType("azure")
    pkg.identity = mod
    monkeypatch.setitem(sys.modules, "azure", pkg)
    monkeypatch.setitem(sys.modules, "azure.identity", mod)

    seen = {}

    def _get(url, headers=None, timeout=None):
        seen["url"] = url
        seen["headers"] = headers or {}
        return resp

    import httpx
    monkeypatch.setattr(httpx, "get", _get)
    return seen


def test_azure_discovers_only_enabled_subscriptions(monkeypatch):
    """The real parsing path against a real-shaped ARM payload."""
    _fake_azure(monkeypatch, _Resp(200, ARM_SUBSCRIPTIONS))
    r = ambient.detect_azure()
    assert r.found is True
    assert r.usable is True
    assert r.scopes == ["aaaa-1111", "cccc-3333"], (
        f"expected only Enabled subscriptions, got {r.scopes}")
    assert r.source == "default-chain"


def test_azure_calls_arm_with_a_bearer_token(monkeypatch):
    """Catches a wrong URL, a missing api-version, or an unauthenticated call:
    the kind of mistake a stubbed probe can never see."""
    seen = _fake_azure(monkeypatch, _Resp(200, ARM_SUBSCRIPTIONS))
    ambient.detect_azure()
    assert seen["url"].startswith("https://management.azure.com/subscriptions")
    assert "api-version=" in seen["url"], "ARM rejects a request with no api-version"
    assert seen["headers"].get("Authorization") == "Bearer fake-arm-token"


def test_azure_does_not_use_the_interactive_browser_credential(monkeypatch):
    """A first run must never pop a browser sign-in the user did not ask for."""
    captured = {}

    class _Token:
        token = "t"

    class _Cred:
        def __init__(self, **kw):
            captured.update(kw)

        def get_token(self, *a, **k):
            return _Token()

    mod = types.ModuleType("azure.identity")
    mod.DefaultAzureCredential = _Cred
    pkg = types.ModuleType("azure")
    pkg.identity = mod
    monkeypatch.setitem(sys.modules, "azure", pkg)
    monkeypatch.setitem(sys.modules, "azure.identity", mod)
    import httpx
    monkeypatch.setattr(httpx, "get", lambda *a, **k: _Resp(200, ARM_SUBSCRIPTIONS))
    ambient.detect_azure()
    assert captured.get("exclude_interactive_browser_credential") is True


@pytest.mark.parametrize("status", [401, 403, 500])
def test_azure_http_failures_are_reported_with_the_status(monkeypatch, status):
    """403 means "your login works but cannot list subscriptions", which is a
    different fix from "log in". Losing that distinction is what the old
    except: pass did."""
    _fake_azure(monkeypatch, _Resp(status, {}))
    r = ambient.detect_azure()
    assert r.found is False
    assert str(status) in r.detail


def test_azure_a_tenant_with_no_subscriptions_is_not_a_connection(monkeypatch):
    _fake_azure(monkeypatch, _Resp(200, {"value": []}))
    r = ambient.detect_azure()
    assert r.found is False and "no subscriptions" in r.detail


def test_azure_malformed_json_does_not_crash(monkeypatch):
    _fake_azure(monkeypatch, _Resp(200, None, boom=ValueError("not json")))
    r = ambient.detect_azure()
    assert r.found is False and r.detail


def test_azure_a_failed_token_request_is_reported(monkeypatch):
    _fake_azure(monkeypatch, _Resp(200, ARM_SUBSCRIPTIONS), token_ok=False)
    r = ambient.detect_azure()
    assert r.found is False
    assert "RuntimeError" in r.detail or "no subscriptions" in r.detail


def test_azure_explicit_subscription_ids_skip_the_api_call(monkeypatch):
    """Someone who set AZURE_SUBSCRIPTION_IDS should not pay for a round trip."""
    monkeypatch.setenv("AZURE_SUBSCRIPTION_IDS", "sub-x, sub-y")
    called = []
    _fake_azure(monkeypatch, _Resp(200, ARM_SUBSCRIPTIONS))
    import httpx
    monkeypatch.setattr(httpx, "get", lambda *a, **k: called.append(1) or _Resp(200, {}))
    r = ambient.detect_azure()
    assert r.found is True and r.scopes == ["sub-x", "sub-y"]
    assert not called, "explicit subscription ids should not trigger an ARM call"


def test_azure_discovered_scopes_reach_the_connector(monkeypatch):
    """End of the chain: probe -> connector -> the subscriptions a query will use."""
    _fake_azure(monkeypatch, _Resp(200, ARM_SUBSCRIPTIONS))
    from finops.connectors.azure import AzureConnector
    c = AzureConnector()
    assert asyncio.run(c.is_configured()) is True
    assert c._subscription_ids == ["aaaa-1111", "cccc-3333"]


# ── GCP: fake google.auth and the billing client ─────────────────────────────

class _Acct:
    def __init__(self, name, open_):
        self.name = name
        self.open = open_


def _fake_gcp(monkeypatch, accounts, *, default_boom=None, list_boom=None):
    auth = types.ModuleType("google.auth")

    def _default(*a, **k):
        if default_boom:
            raise default_boom
        return ("fake-creds", "proj-1")

    auth.default = _default

    class _Client:
        def __init__(self, credentials=None):
            pass

        def list_billing_accounts(self):
            if list_boom:
                raise list_boom
            return accounts

    billing = types.ModuleType("google.cloud.billing_v1")
    billing.CloudBillingClient = _Client
    gcloud = types.ModuleType("google.cloud")
    gcloud.billing_v1 = billing
    g = types.ModuleType("google")
    g.auth = auth
    g.cloud = gcloud
    for name, mod in (("google", g), ("google.auth", auth),
                      ("google.cloud", gcloud), ("google.cloud.billing_v1", billing)):
        monkeypatch.setitem(sys.modules, name, mod)


def test_gcp_discovers_only_open_billing_accounts(monkeypatch):
    monkeypatch.setenv("GOOGLE_CLOUD_PROJECT", "proj-1")   # past the signal pre-check
    _fake_gcp(monkeypatch, [
        _Acct("billingAccounts/01AAAA-BBBBBB-CCCCCC", True),
        _Acct("billingAccounts/02DDDD-EEEEEE-FFFFFF", False),
    ])
    r = ambient.detect_gcp()
    assert r.found is True
    assert r.scopes == ["01AAAA-BBBBBB-CCCCCC"], (
        f"closed billing accounts must be excluded, got {r.scopes}")


def test_gcp_no_billing_accounts_is_not_a_connection(monkeypatch):
    monkeypatch.setenv("GOOGLE_CLOUD_PROJECT", "proj-1")
    _fake_gcp(monkeypatch, [])
    assert ambient.detect_gcp().found is False


def test_gcp_a_failed_default_credential_is_not_a_connection(monkeypatch):
    monkeypatch.setenv("GOOGLE_CLOUD_PROJECT", "proj-1")
    _fake_gcp(monkeypatch, [], default_boom=RuntimeError("DefaultCredentialsError"))
    r = ambient.detect_gcp()
    assert r.found is False and r.detail


def test_gcp_a_credential_that_cannot_list_billing_is_not_a_connection(monkeypatch):
    """ADC often exists without the billing.viewer role. That is not a connection:
    nable would be 'connected' and unable to answer a single cost question."""
    monkeypatch.setenv("GOOGLE_CLOUD_PROJECT", "proj-1")
    _fake_gcp(monkeypatch, [], list_boom=PermissionError("403"))
    assert ambient.detect_gcp().found is False


def test_gcp_discovered_scopes_reach_the_connector(monkeypatch):
    monkeypatch.setenv("GOOGLE_CLOUD_PROJECT", "proj-1")
    _fake_gcp(monkeypatch, [_Acct("billingAccounts/01AAAA-BBBBBB-CCCCCC", True)])
    from finops.connectors.gcp import GCPConnector
    c = GCPConnector()
    assert asyncio.run(c.is_configured()) is True
    assert c._billing_account_ids == ["01AAAA-BBBBBB-CCCCCC"]
