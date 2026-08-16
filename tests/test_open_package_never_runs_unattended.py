# SPDX-License-Identifier: Apache-2.0
"""The MCP server and the CLI answer questions. They never run anything on a timer.

That sentence is the product boundary, and this file is what makes it a fact
rather than a description. Note how narrow it is: the MCP server and the CLI.
An earlier draft of this docstring said "the open package", and that was false
and this file passed anyway. See UNMARKED_BACKGROUND_PATHS below for what it
missed and how.

The history matters for why it is checked structurally. This morning the
unattended rule was real policy in billing_access.py and completely bypassed:
AWSConnector._make_client called ce_client() only to trip the kill switch, then
built its own client, so the nightly cron billed every customer $0.01 a request
forever. The fix threaded an unattended mark through a contextvar. Then, because
the cron itself moved to nable-enterprise, the mark became unreachable from open
code at all, which is a stronger guarantee than the rule it enforced:

    before   the MCP server armed nine cron jobs and the rule stopped them billing
    now      the MCP server arms nothing, so there is nothing to stop

    still    `finops-slack`, a separate opt-in console script, runs its own
             5-minute scheduler and can still reach a billed Cost Explorer
             call. Measured, not assumed. It is on the ratchet list below.

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

# Modules in the open package that run work on their own timer or loop WITHOUT
# marking it unattended. This list may SHRINK and may never grow.
#
# It exists because the first version of this file asked the wrong question. It
# checked that nothing CALLS unattended_context, and passed, and I reported "the
# open package has no path to a billed Cost Explorer request with nobody
# watching" on the strength of it. The actual failure mode is the opposite:
# something that runs unattended and never marks itself. Testing for the
# presence of a marker cannot find the absence of one.
#
# What that missed, measured by driving the path:
#
#   slack_bot/app.py  BackgroundScheduler, every 5 minutes
#     -> _run_reports -> run_subscription -> build_report
#       -> _section_commitments -> recommendations/commitments.py
#         -> boto3.client("ce")            BILLED, and in_unattended_context() False
#
# So `finops-slack` with a subscription carrying the commitments section still
# bills per report. It is opt-in, a separate console script, and strictly better
# than the nightly cron this replaced, but the absolute claim was wrong.
#
# pr_comments/webhook.py is on the list as a background path too, though its
# reads go to the AWS Pricing API, which is free.
UNMARKED_BACKGROUND_PATHS: frozenset[str] = frozenset({
    "slack_bot/app.py",
    "pr_comments/webhook.py",
})

_BACKGROUND_MARKERS = {
    "BackgroundScheduler", "BlockingScheduler", "AsyncIOScheduler",
    "SocketModeHandler", "serve_forever", "HTTPServer", "ThreadingHTTPServer",
}


def _background_modules() -> set[str]:
    """Every open module that starts a timer or a serving loop, found structurally."""
    found: set[str] = set()
    for path in sorted(PKG.rglob("*.py")):
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except SyntaxError:                # pragma: no cover
            continue
        for node in ast.walk(tree):
            name = getattr(node, "id", None) or getattr(node, "attr", None)
            if name in _BACKGROUND_MARKERS:
                found.add(str(path.relative_to(PKG)))
                break
    return found


def test_no_new_unattended_background_path_appears():
    """The ratchet. A new timer in the open package is a new way to bill someone.

    Asks the question the first version of this file got backwards: not "does
    anything mark itself unattended" but "does anything RUN unattended". The
    MCP server and the CLI must never appear here.
    """
    new = _background_modules() - UNMARKED_BACKGROUND_PATHS
    assert not new, (
        "these open modules run work on a timer or a serving loop and are not "
        "on the known list:\n  " + "\n  ".join(sorted(new)) +
        "\n\nIf this is deliberate, wrap the work in unattended_context() so "
        "billing_access refuses per-request charges, or move it to "
        "nable-enterprise with the rest of the always-on layer.")


def test_the_known_list_may_not_rot():
    """A fixed entry must leave the list in the same commit.

    Without this the allowlist becomes a permanent exemption, which is how a
    ratchet quietly turns into a rule nobody enforces.
    """
    stale = UNMARKED_BACKGROUND_PATHS - _background_modules()
    assert not stale, (
        "these no longer run anything in the background; drop them from "
        "UNMARKED_BACKGROUND_PATHS:\n  " + "\n  ".join(sorted(stale)))


def test_the_mcp_server_and_cli_are_not_on_that_list():
    """The claim that is actually true, stated narrowly enough to be true.

    finops-mcp (the MCP server) and nable (the CLI) have no background path.
    finops-slack and finops-pr-webhook are separate console scripts a user has
    to run deliberately, and they are the two entries above.
    """
    background = _background_modules()
    for entry in ("server.py", "entry.py", "setup_wizard.py", "cli_scan.py",
                  "scheduler/jobs.py"):
        assert entry not in background, (
            f"{entry} is on the MCP/CLI path and now starts something in the "
            f"background; that is the shape that billed people nightly")


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
