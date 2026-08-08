"""`nable scan --dry-run`: what a scan will touch, before it touches anything.

Asked for on r/selfhosted by a reader evaluating the credential threat model:
"A dry run listing every required permission before the first scan would also be
useful." Telling a security-minded operator to trust a claim they cannot check is
how a tool gets declined at review.

The manifest is also the source for the IAM policy we hand people, so these tests
exist to stop the two from disagreeing, and to stop a mutating call being added
to something advertised as read-only.
"""
from __future__ import annotations

import json
import types

import pytest

from finops.analyzers.optimizer import _ALL_CHECKS
from finops.scan_manifest import (
    BASE_ACTIONS, SCAN_CHECKS, SPEND_ACTIONS, _MUTATING,
    iam_actions, iam_policy, render_dry_run,
)


def test_the_manifest_covers_every_check_the_scanner_runs():
    """The drift guard. A new check with no manifest entry means the dry run and
    the IAM policy both quietly under-report what the scan does."""
    assert set(SCAN_CHECKS) == set(_ALL_CHECKS), (
        f"manifest missing: {sorted(set(_ALL_CHECKS) - set(SCAN_CHECKS))}, "
        f"stale: {sorted(set(SCAN_CHECKS) - set(_ALL_CHECKS))}"
    )


def test_nothing_in_the_manifest_can_mutate():
    """nable asks for read-only credentials. A manifest entry that changes
    something would be both a lie and a permission escalation."""
    for name, (_, calls) in SCAN_CHECKS.items():
        for call, action in calls:
            verb = call.split(".", 1)[1]
            assert not any(verb.startswith(m) for m in _MUTATING), f"{name}: {call}"
            assert any(action.split(":")[1].startswith(p)
                       for p in ("Describe", "List", "Get")), f"{name}: {action}"
    for call, action in BASE_ACTIONS + SPEND_ACTIONS:
        assert any(action.split(":")[1].startswith(p)
                   for p in ("Describe", "List", "Get")), action


def test_cost_explorer_is_absent_unless_spend_is_asked_for():
    """The load-bearing one: every CE request bills the user $0.01, and the
    default scan is advertised as free."""
    default = iam_actions(include_spend=False)
    assert not [a for a in default if a.startswith("ce:")]
    assert "ce:GetCostAndUsage" in iam_actions(include_spend=True)


def test_the_policy_matches_the_calls_exactly():
    """A policy broader than the calls is over-permission; narrower is a scan
    that fails halfway through on AccessDenied."""
    pol = iam_policy()
    assert pol["Statement"][0]["Effect"] == "Allow"
    assert sorted(pol["Statement"][0]["Action"]) == iam_actions()
    assert json.loads(json.dumps(pol))  # serializable as handed to a user


def test_the_dry_run_states_that_it_ran_nothing():
    out = render_dry_run()
    assert "Nothing below was executed" in out
    assert "changes nothing" in out
    assert "Cost Explorer is NOT called" in out


def test_the_dry_run_discloses_the_cost_explorer_charge_when_spend_is_on():
    out = render_dry_run(include_spend=True)
    assert "$0.01" in out and "ce.get_cost_and_usage" in out


def test_dry_run_returns_before_touching_credentials(capsys, monkeypatch):
    """It must answer on a machine with no credentials, no network and no
    config: that is exactly the machine of the person evaluating it."""
    import finops.cli_scan as cs

    def explode(*a, **k):
        raise AssertionError("dry run reached a boto3 session")

    monkeypatch.setattr(cs, "_available_profiles", explode, raising=False)
    args = types.SimpleNamespace(dry_run=True, json=False, spend=False,
                                 demo=False, debug=False, profile=None, regions=None)
    assert cs.run(args) == cs.EXIT_OK
    out = capsys.readouterr().out
    assert "Nothing below was executed" in out
    assert "sts:GetCallerIdentity" in out


def test_dry_run_json_is_a_pasteable_policy(capsys):
    import finops.cli_scan as cs
    args = types.SimpleNamespace(dry_run=True, json=True, spend=False,
                                 demo=False, debug=False, profile=None, regions=None)
    assert cs.run(args) == cs.EXIT_OK
    payload = json.loads(capsys.readouterr().out)
    assert payload["executed"] is False
    assert payload["cost_explorer_called"] is False
    assert payload["iam_policy"]["Statement"][0]["Sid"] == "NableReadOnlyScan"


@pytest.mark.parametrize("check", sorted(SCAN_CHECKS))
def test_every_check_says_what_it_finds_in_plain_terms(check):
    """A permission list nobody can read is not disclosure."""
    what, calls = SCAN_CHECKS[check]
    assert len(what) > 15 and calls


# ── surfacing: an evaluator has to find this without reading the README ──────

def test_the_no_credentials_path_offers_the_dry_run():
    """Someone with no credentials is often deciding whether to grant any. That
    message pointed only at how to hand over access."""
    import inspect
    import finops.cli_scan as cs
    src = inspect.getsource(cs.run)
    i = src.index("no AWS credentials found on this machine")
    assert "--dry-run" in src[i:i + 900]


def test_the_permission_denied_path_points_at_the_exact_policy():
    import inspect
    import finops.cli_scan as cs
    src = inspect.getsource(cs.run)
    i = src.index("cannot call sts:GetCallerIdentity")
    assert "--dry-run --json" in src[i:i + 400]
