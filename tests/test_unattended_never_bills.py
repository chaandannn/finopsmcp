# SPDX-License-Identifier: Apache-2.0
"""A charge that repeats on a timer with nobody watching is the one nobody agreed to.

billing_access.py has said that in words since it was written. The code went
around it. AWSConnector._make_client called ce_client() only to trip the
NABLE_NO_COST_EXPLORER kill switch, then built its own boto3 CE client and never
passed `unattended`, so the unattended branch was unreachable from the one place
that mattered. Measured 2026-08-15: the guard refused a scheduled call while the
scheduler's own path billed the customer $0.01 per request, nightly, forever.

That is the fourth "advertised is not wired" defect of this session and the only
one that spends someone else's money.

What is left here is the RULE: ce_client refuses under the unattended mark,
every branch of _make_client goes through it, and the mark propagates the way
the AWS connector actually works (contextvars across asyncio.to_thread) without
leaking across threads. The cron that sets the mark moved to nable-enterprise
later the same day, and its tests went with it; tests/test_open_package_never_runs_unattended.py
is what checks the open package has no background path at all now.

The on-ramp is deliberately NOT closed. Somebody pointing nable at credentials
they already have and asking a question still reaches Cost Explorer, because one
request is the right price for an answer a person is waiting for. Two tests
below exist only to make sure a later tightening does not take that away.
"""
from __future__ import annotations

import asyncio
import threading

import pytest

from finops import billing_access
from finops.billing_access import (
    BillingAccessError, attended_context, in_unattended_context, unattended_context,
)


# ── the fix, at the level the bug lived ──────────────────────────────────────

def test_the_connector_refuses_to_build_a_billed_client_on_a_timer(monkeypatch):
    """_make_client is where the money was spent, so it is where this is pinned."""
    from finops.connectors.aws import AWSConnector

    monkeypatch.delenv("NABLE_NO_COST_EXPLORER", raising=False)
    monkeypatch.setenv("FINOPS_DEMO", "0")

    with unattended_context():
        with pytest.raises(BillingAccessError) as exc:
            AWSConnector()._make_client()

    assert "scheduled or background" in str(exc.value)
    assert "billing export" in str(exc.value).lower(), (
        "the refusal has to name the fix; a user reads this text, not the rule")


def test_an_interactive_question_still_reaches_cost_explorer(monkeypatch):
    """The on-ramp is the reason Cost Explorer is here at all.

    Somebody points nable at credentials they already have and gets a real
    number in a minute, with no stack to deploy and no 24-hour wait. Closing
    that to save a cent would trade a working first impression for tidiness.
    """
    import boto3

    from finops.connectors.aws import AWSConnector

    monkeypatch.delenv("NABLE_NO_COST_EXPLORER", raising=False)
    monkeypatch.setenv("FINOPS_DEMO", "0")
    built = []
    monkeypatch.setattr(boto3, "client",
                        lambda name, **kw: built.append(name) or object())

    client = AWSConnector()._make_client()

    assert client is not None and built == ["ce"], (
        "an interactive question could not build a Cost Explorer client")


def test_the_assume_role_path_goes_through_the_gate_too(monkeypatch):
    """Multi-account is where per-request charges multiply.

    This branch used to build its own credentialled client, which meant the org
    path was the most expensive one AND the least guarded.
    """
    import boto3

    from finops.connectors.aws import AWSConnector

    monkeypatch.setenv("FINOPS_DEMO", "0")

    class STS:
        def assume_role(self, **kw):
            return {"Credentials": {"AccessKeyId": "a", "SecretAccessKey": "b",
                                    "SessionToken": "c"}}

    monkeypatch.setattr(boto3, "client",
                        lambda name, **kw: STS() if name == "sts" else object())

    with unattended_context():
        with pytest.raises(BillingAccessError):
            AWSConnector()._make_client(role_arn="arn:aws:iam::1:role/r")


# ── the reason it is a contextvar ────────────────────────────────────────────

def test_the_mark_does_not_leak_into_another_thread():
    """APScheduler runs jobs in a pool.

    A module-level flag would leak "unattended" from a running job into an
    interactive question that happened to share a worker thread, and the user
    would be told to configure a billing export in the middle of a conversation.
    """
    observed: dict[str, bool] = {}

    def elsewhere():
        observed["other_thread"] = in_unattended_context()

    with unattended_context():
        assert in_unattended_context() is True
        t = threading.Thread(target=elsewhere)
        t.start()
        t.join()

    assert observed["other_thread"] is False, (
        "a scheduled job's mark leaked into an unrelated thread")
    assert in_unattended_context() is False, "the mark outlived its block"


def test_the_mark_survives_into_tasks_and_threads_the_job_itself_spawns():
    """The opposite failure: a mark that does not reach the client is no mark.

    The AWS connector does its blocking botocore work through asyncio.to_thread,
    so if the context stopped at that hop the guard would be decorative.
    """
    async def inner():
        return await asyncio.to_thread(in_unattended_context)

    with unattended_context():
        assert asyncio.run(inner()) is True, (
            "the unattended mark did not survive asyncio.to_thread, which is "
            "exactly the hop every Cost Explorer call goes through")


def test_an_explicit_flag_beats_the_ambient_context_both_ways(monkeypatch):
    """Ambient default, explicit override. Neither one alone is enough.

    Without the ambient default the original bug returns: an unannotated path
    bills. Without the override there is no way to run a genuinely interactive
    request from inside a scheduled process.
    """
    import boto3

    monkeypatch.delenv("NABLE_NO_COST_EXPLORER", raising=False)
    monkeypatch.setattr(boto3, "client", lambda name, **kw: object())

    with unattended_context():
        assert billing_access.ce_client(unattended=False) is not None
        with attended_context():
            assert billing_access.ce_client() is not None

    with pytest.raises(BillingAccessError):
        billing_access.ce_client(unattended=True)


def test_cost_explorer_is_skipped_entirely_when_the_export_exists(monkeypatch):
    """Not "cheaper", not "second choice". Not called.

    The export answers the same question with more detail and no per-request
    charge, so reaching for Cost Explorer anyway is pure waste on the customer's
    bill. That is the actual problem, not Cost Explorer itself.
    """
    monkeypatch.setattr(billing_access, "provisioned", lambda p: True)
    assert billing_access.should_use_cost_explorer("aws") is False
    assert billing_access.should_use_cost_explorer("aws", unattended=False) is False


def test_demo_mode_cannot_reach_cost_explorer_through_the_gate(monkeypatch):
    """The guard has to live at the chokepoint, not next to it.

    Measured 2026-08-16 by running the demo dashboard on a laptop with real AWS
    credentials: the anomaly backfill reached ce_client, built a live client,
    and pulled 307 rows of the presenter's ACTUAL spend into the demo database.
    Billed, mid-demo, by the product that exists to stop surprise charges.

    AWSConnector._make_client had this check and always has. ce_client did not.
    So the guard held on the path everyone looks at and failed on the one nobody
    did, which is the same shape as the unattended bug above: a chokepoint that
    enforces part of the policy is trusted for all of it.
    """
    import boto3

    monkeypatch.setattr("finops.demo_data.is_demo", lambda: True)
    monkeypatch.delenv("NABLE_NO_COST_EXPLORER", raising=False)
    built: list[str] = []
    monkeypatch.setattr(boto3, "client",
                        lambda name, **kw: built.append(name) or object())

    with pytest.raises(BillingAccessError, match="Demo mode"):
        billing_access.ce_client(reason="anomaly backfill")
    assert built == [], "demo mode built a real Cost Explorer client"


def test_the_demo_check_runs_before_every_other_gate(monkeypatch):
    """Demo is checked first on purpose.

    An interactive demo is exactly the context where the other gates all say
    "allowed": somebody is watching, Cost Explorer is not banned, no export is
    configured. Every other check passing is what makes this one load-bearing.
    """
    import boto3

    monkeypatch.setattr("finops.demo_data.is_demo", lambda: True)
    monkeypatch.delenv("NABLE_NO_COST_EXPLORER", raising=False)
    monkeypatch.setattr(boto3, "client", lambda name, **kw: object())

    # attended, permitted, unbanned: every other gate is open.
    with attended_context():
        with pytest.raises(BillingAccessError, match="Demo mode"):
            billing_access.ce_client(unattended=False)
