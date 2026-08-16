"""Team tier ($1,000/mo flat): plan validation and the license gate itself.

The conversational Slack bot and chat remediation are the Team-only features
these keys unlock, but the bot moved to nable-enterprise on 2026-08-15, so the
tests that drive its gate went with it (tests/test_team_tier_bot_gate.py there).
What stays here is finops.license, which is open and is what actually decides.
"""
from __future__ import annotations

import pytest

import finops.license as lic
from finops.license import LicenseStatus


@pytest.fixture(autouse=True)
def _reset_status_cache():
    lic._status = None
    yield
    lic._status = None


def _status(mode):
    return LicenseStatus(mode=mode, email="x@y.z", issued="2026-06-01", message="")


def test_tier_inclusion_matrix():
    assert not _status("free").is_team
    assert not _status("pro").is_team        # $40 solo keys do not get the bot
    assert _status("team").is_team
    assert _status("enterprise").is_team
    assert _status("trial").is_team          # demos feel the full product
    # Higher tiers include pro features (this also fixes the enterprise hole)
    assert _status("team").is_pro
    assert _status("enterprise").is_pro


def test_team_plan_key_roundtrip(monkeypatch):
    # Throwaway test keypair (not production). Inject the matching public key so
    # the round-trip verifies against the test key, never the bundled one.
    monkeypatch.setenv("FINOPS_LICENSE_PRIVATE_KEY", "8fbe8En53x3KhJ93ZwEmE3L0IVLHQm6yI-gn3FGIpeg")
    monkeypatch.setattr(lic, "_PUBLIC_KEY_B64", "sxzvFKJjtkqH4xZWXQZLvrYhRxQFVoaJ5YRiEu18dMw")
    key = lic.generate_key("buyer@acme.com", plan="team")
    status = lic.validate_key(key)
    assert status.mode == "team"
    assert status.is_team and status.is_pro


def test_require_team_blocks_free_and_pro(monkeypatch):
    for mode in ("free", "pro"):
        monkeypatch.setattr(lic, "get_status", lambda m=mode: _status(m))
        err = lic.require_team("slack_conversational_bot")
        assert err is not None
        assert "$1,000/mo" in err["upgrade"]


def test_require_team_allows_team_and_trial(monkeypatch):
    for mode in ("team", "enterprise", "trial"):
        monkeypatch.setattr(lic, "get_status", lambda m=mode: _status(m))
        assert lic.require_team("slack_conversational_bot") is None


def test_require_team_unknown_feature_fails_open(monkeypatch):
    monkeypatch.setattr(lic, "get_status", lambda: _status("free"))
    assert lic.require_team("not_a_team_feature") is None
