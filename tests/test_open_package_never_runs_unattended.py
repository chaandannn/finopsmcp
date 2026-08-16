# SPDX-License-Identifier: Apache-2.0
"""The open package answers questions. It never runs anything on a timer.

That sentence is the product boundary, and this file is what makes it a fact
rather than a description.

The history matters for why it is checked structurally. This morning the
unattended rule was real policy in billing_access.py and completely bypassed:
AWSConnector._make_client called ce_client() only to trip the kill switch, then
built its own client, so the nightly cron billed every customer $0.01 a request
forever. The fix threaded an unattended mark through a contextvar. Then, because
the cron itself moved to nable-enterprise, the mark became unreachable from open
code at all, which is a stronger guarantee than the rule it enforced:

    before   a cron existed in the open package and the rule stopped it billing
    now      the open package has no cron, so there is nothing to stop

A guarantee that rests on "we removed the thing" only holds while nobody adds it
back, and "do not add a scheduler to the open package" is a rule in a document,
which fails nothing. So the tests below fail instead.

Cost Explorer is deliberately still reachable for an interactive question. One
request is the right price for an answer a person is waiting for, and it is what
lets somebody point nable at credentials they already have and get a number in a
minute. Two tests exist to stop a later tightening taking that away.
"""
from __future__ import annotations

import ast
import inspect
import pathlib

import pytest

from finops import billing_access
from finops.billing_access import BillingAccessError, unattended_context

PKG = pathlib.Path(billing_access.__file__).parent


# ── the guarantee ────────────────────────────────────────────────────────────

def test_nothing_in_the_open_package_marks_work_unattended():
    """The mark is set by the hosted cron and by nothing shipped here.

    If an open module ever calls unattended_context(), the open package has
    grown a background path, which is the thing that used to bill people.
    Structural because a comment cannot fail.
    """
    offenders: list[str] = []
    for path in sorted(PKG.rglob("*.py")):
        if path.name == "billing_access.py":
            continue                       # defines it; does not use it
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except SyntaxError:                # pragma: no cover
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and (
                    getattr(node.func, "id", None) == "unattended_context"
                    or getattr(node.func, "attr", None) == "unattended_context"):
                offenders.append(f"{path.relative_to(PKG)}:{node.lineno}")

    assert not offenders, (
        "these open modules mark work as unattended, so the open package has a "
        "background path again:\n  " + "\n  ".join(offenders))


def test_the_open_package_ships_no_cron():
    """No scheduler lifecycle, no job_* registrations, no APScheduler.

    scheduler/jobs.py keeps the WORK (_snapshot_all, _detect_and_alert) because
    MCP tools call it on demand. What must not come back is the thing that calls
    it forever.
    """
    from finops.scheduler import jobs

    tree = ast.parse(inspect.getsource(jobs))
    names = {n.name for n in ast.walk(tree)
             if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}

    banned = {"start_scheduler", "stop_scheduler", "_add_job", "_as_unattended",
              "_acquire_scheduler_lock", "scheduler_enabled"}
    assert not (names & banned), (
        f"the open scheduler module defines {sorted(names & banned)}; the cron "
        f"belongs in nable-enterprise")

    src = inspect.getsource(jobs)
    assert "apscheduler" not in src.lower(), (
        "the open package imports APScheduler again, which is the dependency "
        "that exists only to run things on a timer")


def test_the_on_demand_verbs_survived_the_split():
    """The other half. Moving the cron must not take the MCP surface with it.

    These are what "take a snapshot now" and "send the digest now" call, and a
    split that removed them would be a broken product rather than a clean line.
    """
    from finops.scheduler import jobs

    for name in ("run_snapshot_now", "run_anomaly_check_now", "run_digest_now",
                 "run_weekly_insight_now", "job_weekly_email_digest",
                 "_snapshot_all", "_detect_and_alert", "_send_daily_digest"):
        assert hasattr(jobs, name), (
            f"{name} went with the cron; an MCP tool or CLI command calls it")


def test_the_mcp_tools_that_call_the_scheduler_still_resolve():
    """Checked as real imports, because the tools import lazily inside functions
    and a broken one would only surface when a user ran it."""
    from finops.scheduler.jobs import (  # noqa: F401
        job_weekly_email_digest, run_digest_now, run_snapshot_now,
    )
    # watchdog/job.py imports these FROM scheduler.jobs; they are dedup
    # helpers for alert state, not cron, so they stayed open.
    from finops.scheduler.jobs import (  # noqa: F401
        _alert_already_sent, _mark_alert_sent,
    )


# ── the seams, all three ─────────────────────────────────────────────────────

@pytest.mark.parametrize("module", [
    "finops.connectors.cur_s3",     # the S3 billing-export reader
    "finops.storage.rollups",       # precomputed dashboard aggregates
    "finops.scheduler.cron",        # the timer
])
def test_each_enterprise_module_is_absent_and_imported_safely(module):
    """Absent from the open package, and every open caller guards the import.

    An unguarded import would turn the optional seam into a hard dependency and
    kill the nightly job on every open install with ImportError.
    """
    import importlib.util

    assert importlib.util.find_spec(module) is None, (
        f"{module} is importable from the open package; it ships in "
        f"nable-enterprise")

    tail = module.rsplit(".", 1)[1]
    unguarded: list[str] = []
    for path in sorted(PKG.rglob("*.py")):
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except SyntaxError:                # pragma: no cover
            continue
        protected = {n for t in ast.walk(tree) if isinstance(t, ast.Try)
                     for n in ast.walk(t)}
        for node in ast.walk(tree):
            names: list[str] = []
            if isinstance(node, ast.Import):
                names = [a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom):
                names = [a.name for a in node.names] + [node.module or ""]
            if tail in names and node not in protected:
                unguarded.append(f"{path.relative_to(PKG)}:{node.lineno}")

    assert not unguarded, (
        f"unguarded imports of {module}:\n  " + "\n  ".join(unguarded))


def test_starting_the_mcp_server_does_not_need_the_cron():
    """server.py used to import start_scheduler unconditionally."""
    import finops.server as server

    fn_src = inspect.getsource(server)
    idx = fn_src.find("scheduler.cron")
    assert idx != -1, "server.py no longer looks for the hosted cron at all"

    tree = ast.parse(fn_src)
    guarded = any(
        isinstance(t, ast.Try)
        and any(isinstance(h.type, ast.Name) and h.type.id == "ImportError"
                for h in t.handlers)
        and "scheduler.cron" in ast.unparse(t)
        for t in ast.walk(tree)
    )
    assert guarded, (
        "server.py imports the hosted cron without an ImportError guard, so "
        "every open MCP session would crash on startup")


# ── what stays reachable, on purpose ─────────────────────────────────────────

def test_an_interactive_question_still_reaches_cost_explorer(monkeypatch):
    """The on-ramp. Deliberately not closed."""
    import boto3

    from finops.connectors.aws import AWSConnector

    monkeypatch.delenv("NABLE_NO_COST_EXPLORER", raising=False)
    monkeypatch.setenv("FINOPS_DEMO", "0")
    built: list[str] = []
    monkeypatch.setattr(boto3, "client",
                        lambda name, **kw: built.append(name) or object())

    assert AWSConnector()._make_client() is not None
    assert built == ["ce"]


def test_the_rule_still_refuses_if_something_ever_does_mark_work_unattended(
        monkeypatch):
    """Belt and braces. The policy outlives the cron's departure.

    billing_access stays open so the guarantee is auditable in the Apache-2.0
    package, and it must keep working for the hosted cron that does set the mark.
    """
    monkeypatch.setenv("FINOPS_DEMO", "0")
    monkeypatch.delenv("NABLE_NO_COST_EXPLORER", raising=False)

    from finops.connectors.aws import AWSConnector

    with unattended_context():
        with pytest.raises(BillingAccessError, match="scheduled or background"):
            AWSConnector()._make_client()
