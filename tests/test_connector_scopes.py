"""The least-privilege manifest has to stay true, or it is worse than nothing.

A published scope table that quietly falls out of step with the code is how a
security claim becomes a lie by omission. These guard the two ways that happens:
a new billing source ships with its blast radius undocumented, or an entry
claims a tightness the provider does not actually offer.

They also pin the Twilio credential swap, which is the one place here where the
scope reduction is real code and not documentation.
"""
import asyncio

import pytest

from finops import connector_scopes as cs
from finops.setup_scan import KEY_HELP, PROVIDER_ENV, PROVIDER_ENV_ALT, scan_ambient_credentials
from finops.connectors.saas.twilio import TwilioConnector

_CLOUDS = {"aws", "azure", "gcp"}


# ── The manifest covers everything, and means what it says ───────────────────

def test_every_billing_source_has_a_documented_scope():
    """A connector that ships without an entry ships with an unstated blast
    radius. CI is the only thing that will catch that."""
    documented = set(cs.CONNECTOR_SCOPES)
    expected = set(PROVIDER_ENV) | _CLOUDS
    assert not (expected - documented), f"undocumented: {sorted(expected - documented)}"


def test_no_scope_documents_a_source_that_does_not_exist():
    stray = set(cs.CONNECTOR_SCOPES) - (set(PROVIDER_ENV) | _CLOUDS)
    assert not stray, f"documented but not registered: {sorted(stray)}"


@pytest.mark.parametrize("slug", sorted(cs.CONNECTOR_SCOPES))
def test_scope_entries_are_complete(slug):
    scope = cs.CONNECTOR_SCOPES[slug]
    assert scope.grade in (cs.SCOPED, cs.ROLE, cs.ACCOUNT)
    assert scope.credential and scope.permission, slug
    assert scope.calls, f"{slug} claims a scope but names nothing it calls with it"


@pytest.mark.parametrize("slug", sorted(cs.CONNECTOR_SCOPES))
def test_scoped_grade_names_a_real_permission(slug):
    """"scoped" is the strongest claim on the page. It may not be spent on a
    hand-wave like "no billing-only scope"."""
    scope = cs.CONNECTOR_SCOPES[slug]
    if scope.grade != cs.SCOPED:
        return
    assert "no billing-only scope" not in scope.permission.lower()
    assert len(scope.permission) > 8, scope.permission


def test_account_grade_entries_admit_it():
    """The honest half. If every entry graded itself scoped the table would be
    useless, so assert the broad ones are actually still labelled broad."""
    account = {s for s, v in cs.CONNECTOR_SCOPES.items() if v.grade == cs.ACCOUNT}
    for slug in ("openai", "anthropic", "vercel", "databricks"):
        assert slug in account, f"{slug} should be graded account-wide"


def test_setup_hints_quote_the_manifest_verbatim():
    """The hint a user follows and the scope the trust page publishes come from
    one string, so they cannot drift."""
    for slug, name in (("cloudflare", "Cloudflare"), ("twilio", "Twilio"),
                       ("datadog", "Datadog"), ("mongodb", "MongoDB Atlas"),
                       ("snowflake", "Snowflake"), ("databricks", "Databricks")):
        assert cs.CONNECTOR_SCOPES[slug].permission in KEY_HELP[name][1], name


def test_databricks_gap_is_recorded_not_hidden():
    """Databricks needs account admin today and a narrower path exists. That is
    a work list entry, and it must survive in the open."""
    gaps = {s.provider for s in cs.gaps()}
    assert "Databricks" in gaps


def test_render_leads_with_the_worst_grade():
    """Bad news should not be buried under the good news."""
    out = cs.render()
    assert out.index("ACCOUNT") < out.index("SCOPED")
    assert "Account > Billing > Read" in out          # Cloudflare, scoped
    assert "Account admin" in out                     # Databricks, not scoped
    assert "known gap" in out


def test_render_never_claims_write_access():
    out = cs.render().lower()
    for verb in ("delete", "terminate", "modify", "write access to"):
        assert f"grants {verb}" not in out
    assert "nothing above grants write access" in out


# ── Twilio: the scope reduction that is code, not documentation ──────────────

def _twilio(monkeypatch, **env):
    for key in ("TWILIO_ACCOUNT_SID", "TWILIO_AUTH_TOKEN",
                "TWILIO_API_KEY", "TWILIO_API_SECRET"):
        monkeypatch.delenv(key, raising=False)
    for key, val in env.items():
        monkeypatch.setenv(key, val)
    return TwilioConnector()


def test_restricted_api_key_is_preferred_over_the_auth_token(monkeypatch):
    """Both present means the user has done the safer thing. Use it."""
    conn = _twilio(monkeypatch, TWILIO_ACCOUNT_SID="AC1",
                   TWILIO_AUTH_TOKEN="tok",  # pragma: allowlist secret
                   TWILIO_API_KEY="SK1",
                   TWILIO_API_SECRET="sec")  # pragma: allowlist secret
    assert conn._auth() == ("SK1", "sec")
    assert conn.uses_restricted_key()


def test_auth_token_still_works_alone(monkeypatch):
    """Nobody's existing setup breaks because a better option now exists."""
    conn = _twilio(monkeypatch, TWILIO_ACCOUNT_SID="AC1",
                   TWILIO_AUTH_TOKEN="tok")  # pragma: allowlist secret
    assert conn._auth() == ("AC1", "tok")
    assert not conn.uses_restricted_key()
    assert asyncio.run(conn.is_configured())


def test_restricted_key_alone_is_a_complete_credential(monkeypatch):
    conn = _twilio(monkeypatch, TWILIO_ACCOUNT_SID="AC1", TWILIO_API_KEY="SK1",
                   TWILIO_API_SECRET="sec")  # pragma: allowlist secret
    assert asyncio.run(conn.is_configured())
    assert conn._auth() == ("SK1", "sec")


def test_half_a_key_pair_is_not_a_credential(monkeypatch):
    """A key SID with no secret must not read as configured, and must not
    silently fall back to an Auth Token that is not there either."""
    conn = _twilio(monkeypatch, TWILIO_ACCOUNT_SID="AC1", TWILIO_API_KEY="SK1")
    assert not asyncio.run(conn.is_configured())


def test_account_sid_stays_in_the_path_not_the_credential(monkeypatch):
    """Twilio authenticates a key as (key SID, secret) while the account SID
    stays in the URL. Swapping the credential must not move the account SID."""
    conn = _twilio(monkeypatch, TWILIO_ACCOUNT_SID="AC1", TWILIO_API_KEY="SK1",
                   TWILIO_API_SECRET="sec")  # pragma: allowlist secret
    assert conn._account_sid == "AC1"
    assert "AC1" not in conn._auth()


def test_scan_finds_a_machine_holding_only_the_safer_credential(monkeypatch):
    """The scan must not punish the better choice by failing to see it."""
    monkeypatch.setattr("finops.setup_scan._vault_get", lambda k: None)
    for key in ("TWILIO_ACCOUNT_SID", "TWILIO_AUTH_TOKEN",
                "TWILIO_API_KEY", "TWILIO_API_SECRET"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("TWILIO_ACCOUNT_SID", "AC1234567890")
    monkeypatch.setenv("TWILIO_API_KEY", "SK1234567890")
    monkeypatch.setenv("TWILIO_API_SECRET", "secret-value")  # pragma: allowlist secret

    found = {f["slug"]: f for f in scan_ambient_credentials()}
    assert "twilio" in found
    assert found["twilio"]["env"]["TWILIO_API_SECRET"] == "secret-value"
    assert "TWILIO_AUTH_TOKEN" not in found["twilio"]["env"]


def test_scan_still_finds_the_auth_token_setup(monkeypatch):
    monkeypatch.setattr("finops.setup_scan._vault_get", lambda k: None)
    for key in ("TWILIO_ACCOUNT_SID", "TWILIO_AUTH_TOKEN",
                "TWILIO_API_KEY", "TWILIO_API_SECRET"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("TWILIO_ACCOUNT_SID", "AC1234567890")
    monkeypatch.setenv("TWILIO_AUTH_TOKEN", "auth-token-value")  # pragma: allowlist secret

    found = {f["slug"] for f in scan_ambient_credentials()}
    assert "twilio" in found


def test_alternative_key_sets_share_the_vault_skip_key():
    """scan_ambient_credentials skips a provider when required[0] is already in
    the vault. An alternative set that did not start with the same key would
    re-offer an already-connected provider forever."""
    for slug, alts in PROVIDER_ENV_ALT.items():
        primary_first = PROVIDER_ENV[slug][1][0]
        for alt in alts:
            assert alt[0] == primary_first, f"{slug}: {alt[0]} != {primary_first}"
