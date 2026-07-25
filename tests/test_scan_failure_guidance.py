"""Every `nable scan` failure must name a cause and a fix.

Why this file exists: PostHog showed 7 distinct machines run `nable scan` three
times each on 2026-07-24/25 and fail every time, instantly, all reported as
error_class="other". Four unrelated code paths emitted "other", so the telemetry
could not say which. Reproducing them found two that printed a raw botocore
string with NO fix line at all:

    could not reach AWS: The config profile (prod) could not be found
    could not reach AWS: Unable to parse config file: /Users/x/.aws/config

Every other failure path in scan gives the user a `fix:` line. These did not,
which is why people retried and left. A third bug: credentials that exist but are
rejected were classified "expired", sending the user to `aws sso login` on a
profile that is not SSO.
"""
from __future__ import annotations

import pytest

from finops.cli_scan import (
    EXIT_CONFIG,
    EXIT_NO_CREDS,
    _available_profiles,
    _classify_boto_error,
)


class _Boto(Exception):
    """Stand-in for a botocore exception: classification keys off the class name
    and the response Error.Code, both of which we can forge faithfully."""

    def __init__(self, name, code=None, msg="boom"):
        super().__init__(msg)
        self.__class__.__name__ = name
        if code is not None:
            self.response = {"Error": {"Code": code}}


# ── classification ──────────────────────────────────────────────────────────


@pytest.mark.parametrize("name,expected", [
    ("ProfileNotFound", "profile-missing"),
    ("ConfigParseError", "config-broken"),
    ("ConfigNotFound", "config-broken"),
    ("NoRegionError", "no-region"),
    ("NoCredentialsError", "no-creds"),
    ("PartialCredentialsError", "no-creds"),
    ("SSOTokenLoadError", "expired"),
])
def test_local_config_errors_get_their_own_class(name, expected):
    assert _classify_boto_error(_Boto(name)) == expected


@pytest.mark.parametrize("code", [
    "InvalidClientTokenId", "SignatureDoesNotMatch", "AuthFailure",
    "UnrecognizedClientException", "InvalidAccessKeyId",
])
def test_rejected_credentials_are_not_called_expired(code):
    """The bug: an unknown or revoked access key was reported as an expired
    session, so the fix line told the user to re-authenticate. Refreshing a
    session does not conjure a key that AWS has never heard of."""
    assert _classify_boto_error(_Boto("ClientError", code)) == "bad-creds"


@pytest.mark.parametrize("code", ["ExpiredToken", "ExpiredTokenException", "RequestExpired"])
def test_genuinely_expired_is_still_expired(code):
    assert _classify_boto_error(_Boto("ClientError", code)) == "expired"


def test_denied_is_still_denied():
    assert _classify_boto_error(_Boto("ClientError", "AccessDenied")) == "denied"


def test_unknown_errors_still_fall_through_to_other():
    assert _classify_boto_error(_Boto("ClientError", "SomethingNew")) == "other"
    assert _classify_boto_error(RuntimeError("???")) == "other"


# ── the "other" bucket must stay nearly empty ───────────────────────────────


def test_the_four_instant_failures_no_longer_share_one_class():
    """These were 100% of observed scan failures and all reported as "other",
    which is what made the live signal undiagnosable."""
    classes = {
        _classify_boto_error(_Boto("ProfileNotFound")),
        _classify_boto_error(_Boto("ConfigParseError")),
        _classify_boto_error(_Boto("NoRegionError")),
        _classify_boto_error(_Boto("ClientError", "InvalidClientTokenId")),
    }
    assert len(classes) == 4, f"causes still collapsing into one label: {classes}"
    assert "other" not in classes


# ── profile discovery ───────────────────────────────────────────────────────


def test_available_profiles_works_when_aws_profile_is_broken(tmp_path, monkeypatch):
    """The hint has to survive the exact failure it explains. Building a boto3
    Session honors AWS_PROFILE, so on ProfileNotFound it raises and we would tell
    the user they have no profiles while they are staring at the one they meant.
    """
    aws = tmp_path / ".aws"
    aws.mkdir()
    (aws / "credentials").write_text(
        "[work]\naws_access_key_id = AKIAIOSFODNN7EXAMPLE\n"
        "aws_secret_access_key = wJalrXUtnFEMIbK7MDENGbPxRfiCYEXAMPLEKEY\n"  # pragma: allowlist secret
    )
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("AWS_SHARED_CREDENTIALS_FILE", str(aws / "credentials"))
    monkeypatch.setenv("AWS_PROFILE", "does-not-exist")

    assert "work" in _available_profiles()


def test_available_profiles_is_best_effort(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("AWS_SHARED_CREDENTIALS_FILE", str(tmp_path / "nope"))
    monkeypatch.setenv("AWS_CONFIG_FILE", str(tmp_path / "nope2"))
    assert _available_profiles() == []


# ── exit codes stay a pinned contract ───────────────────────────────────────


def test_config_failures_are_distinct_from_missing_credentials():
    """A machine with a broken profile HAS a setup; it just does not resolve.
    Collapsing that into "no credentials" sends the user to `aws configure` when
    the real fix is one word in an env var."""
    assert EXIT_CONFIG == 7
    assert EXIT_NO_CREDS == 6
    assert EXIT_CONFIG != EXIT_NO_CREDS
