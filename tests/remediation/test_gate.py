"""Tests for the opt-in remediation-PR gate."""
from __future__ import annotations

import textwrap

import pytest

from finops.remediation import gate


@pytest.fixture(autouse=True)
def _clear_env(monkeypatch):
    monkeypatch.delenv("FINOPS_REMEDIATION_ENABLED", raising=False)
    monkeypatch.delenv("FINOPS_POLICY_FILE", raising=False)


def test_disabled_by_default(monkeypatch, tmp_path):
    # No env, no policy file -> fail closed.
    monkeypatch.chdir(tmp_path)
    assert gate.remediation_pr_enabled() is False


@pytest.mark.parametrize("val", ["true", "1", "yes", "on", "TRUE", "Yes"])
def test_env_enables(monkeypatch, val):
    monkeypatch.setenv("FINOPS_REMEDIATION_ENABLED", val)
    assert gate.remediation_pr_enabled() is True


@pytest.mark.parametrize("val", ["false", "0", "no", "", "nope"])
def test_env_off_stays_off(monkeypatch, val):
    monkeypatch.setenv("FINOPS_REMEDIATION_ENABLED", val)
    assert gate.remediation_pr_enabled() is False


def test_policy_yaml_enables(monkeypatch, tmp_path):
    pol = tmp_path / "nable.policy.yaml"
    pol.write_text(textwrap.dedent("""
        remediation:
          open_prs: true
    """))
    monkeypatch.setenv("FINOPS_POLICY_FILE", str(pol))
    assert gate.remediation_pr_enabled() is True


def test_policy_yaml_false_stays_off(monkeypatch, tmp_path):
    pol = tmp_path / "nable.policy.yaml"
    pol.write_text("remediation:\n  open_prs: false\n")
    monkeypatch.setenv("FINOPS_POLICY_FILE", str(pol))
    assert gate.remediation_pr_enabled() is False


def test_env_overrides_policy(monkeypatch, tmp_path):
    # Explicit env=false wins even when the policy file says true.
    pol = tmp_path / "nable.policy.yaml"
    pol.write_text("remediation:\n  open_prs: true\n")
    monkeypatch.setenv("FINOPS_POLICY_FILE", str(pol))
    monkeypatch.setenv("FINOPS_REMEDIATION_ENABLED", "false")
    assert gate.remediation_pr_enabled() is False


def test_missing_policy_file_fails_closed(monkeypatch, tmp_path):
    monkeypatch.setenv("FINOPS_POLICY_FILE", str(tmp_path / "does-not-exist.yaml"))
    assert gate.remediation_pr_enabled() is False


def test_disabled_response_shape():
    r = gate.disabled_response()
    assert r["error"] == "remediation_disabled"
    assert r["pr_url"] is None
    assert "FINOPS_REMEDIATION_ENABLED" in r["message"]
    assert "dry_run" in r["message"]  # hint present by default
    assert "dry_run" not in gate.disabled_response(dry_run_hint=False)["message"]
