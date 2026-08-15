# SPDX-License-Identifier: Apache-2.0
"""A charge that repeats on a timer with nobody watching is the one nobody agreed to.

billing_access.py has said that in words since it was written. The code went
around it. AWSConnector._make_client called ce_client() only to trip the
NABLE_NO_COST_EXPLORER kill switch, then built its own boto3 CE client and never
passed `unattended`, so the unattended branch was unreachable from the one place
that mattered. Measured 2026-08-15: the guard refused a scheduled call while the
scheduler's own path billed the customer $0.01 per request, nightly, forever.

That is the fourth "advertised is not wired" defect of this session and the only
one that spends someone else's money, so the tests here are shaped to catch it
coming back rather than to describe the fix:

  - The end-to-end one runs the REAL job_snapshot with boto3.client stubbed to
    explode on "ce". It reproduces the original bug exactly and fails on it.
  - The structural ones pin the chokepoint: every scheduled job is registered
    through _add_job, and nothing constructs a CE client outside billing_access.

The on-ramp is deliberately NOT closed. Somebody pointing nable at credentials
they already have and asking a question still reaches Cost Explorer, because one
request is the right price for an answer a person is waiting for. Two tests
below exist only to make sure a later tightening does not take that away.
"""
from __future__ import annotations

import ast
import asyncio
import inspect
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


# ── the reproduction ─────────────────────────────────────────────────────────

def test_the_real_nightly_snapshot_constructs_no_cost_explorer_client(
        tmp_path, monkeypatch):
    """The bug, end to end, through the code the cron actually runs.

    Every unit above could pass while the scheduled path still billed, because
    the original defect was not in any single function: it was that the mark
    never travelled from the job to the client. So this drives job_snapshot and
    fails if anything anywhere below it asks boto3 for a "ce" client.

    The chain under test is job_snapshot -> asyncio.run -> Task -> to_thread ->
    _make_client. Each hop copies the context; a hop that did not would show up
    here and nowhere else.
    """
    import boto3

    from finops.scheduler import jobs
    from finops.storage import db

    monkeypatch.setenv("FINOPS_DB_PATH", str(tmp_path / "s.db"))
    monkeypatch.setenv("FINOPS_DATA_DIR", str(tmp_path / "d"))
    monkeypatch.setenv("FINOPS_DEMO", "0")
    monkeypatch.delenv("NABLE_NO_COST_EXPLORER", raising=False)
    db._ENGINE, db._DATA_DIR = None, None

    billed: list[str] = []
    real_client = boto3.client

    def watching_client(name, *a, **kw):
        if name == "ce":
            billed.append(name)
            raise AssertionError(
                "the nightly snapshot built a Cost Explorer client; that is "
                "$0.01 of the customer's money, per request, on a timer")
        return real_client(name, *a, **kw)

    monkeypatch.setattr(boto3, "client", watching_client)

    try:
        jobs._as_unattended(jobs.job_snapshot)()
    finally:
        db._ENGINE, db._DATA_DIR = None, None

    assert billed == [], f"billed calls: {billed}"


# ── the chokepoint, structurally ─────────────────────────────────────────────

def test_every_scheduled_job_is_registered_through_the_marking_helper():
    """A job registered directly would run unmarked and bill.

    Checked as a Call node rather than a substring: `"_add_job" in source` stays
    true when a registration is switched back to _scheduler.add_job, because the
    helper is still defined above. That exact false pass has happened twice in
    this session.
    """
    from finops.scheduler import jobs

    tree = ast.parse(inspect.getsource(jobs))
    direct = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        fn = node.func
        if isinstance(fn, ast.Attribute) and fn.attr == "add_job":
            owner = getattr(fn.value, "id", None)
            if owner == "_scheduler":
                direct.append(node.lineno)

    # Exactly one: the call inside _add_job itself.
    inner = next(n for n in ast.walk(tree)
                 if isinstance(n, ast.FunctionDef) and n.name == "_add_job")
    allowed = {n.lineno for n in ast.walk(inner) if isinstance(n, ast.Call)}

    leaked = [ln for ln in direct if ln not in allowed]
    assert not leaked, (
        f"jobs.py lines {leaked} register a scheduled job without the unattended "
        f"mark, so anything they call can reach Cost Explorer on a timer")


def test_the_mark_actually_wraps_what_gets_registered():
    """_add_job could exist and still hand the raw function to APScheduler."""
    from finops.scheduler import jobs

    seen = {}

    def probe():
        seen["unattended"] = in_unattended_context()

    class FakeScheduler:
        def add_job(self, fn, trigger, *, id, **kw):
            seen["registered"] = fn
            return fn

    original = jobs._scheduler
    jobs._scheduler = FakeScheduler()
    try:
        jobs._add_job(probe, None, id="probe")
    finally:
        jobs._scheduler = original

    assert seen["registered"] is not probe, "the raw function was registered"
    seen["registered"]()
    assert seen["unattended"] is True, (
        "the registered wrapper ran without the unattended mark set")


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
