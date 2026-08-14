# SPDX-License-Identifier: Apache-2.0
"""`nable scan` has to say when it is running an old build.

Why this file exists, stated plainly: on 2026-08-14 the telemetry showed 19 of 20
scans failing across 7 machines in 48 hours, the most recent two minutes before
anyone looked. The failures carrying a version were on 0.8.201 and 0.8.202 while
PyPI had 0.8.210, and the message they printed was:

    boto3 is not installed; reinstall with `pip install finops-mcp`

boto3 was installed. The real cause was a boto3/botocore version skew, which
0.8.207 diagnoses properly by separating "absent" from "present but will not
import" and recording all four version strings. Those users could not see any of
that, because they were five releases short of it.

The advice was worse than useless: `pip install` without -U on an already
installed package prints "Requirement already satisfied" and changes nothing. A
user following it exactly stays broken and now believes they have tried.

Staleness was not an accident either. setup_wizard pins finops-mcp==X into
editor configs on purpose, so a PyPI release never stalls a working client, and
that pin only moves when someone runs `finops upgrade`. server.py has run the
staleness check since it was written. The CLI never has, and the CLI is where a
stale build is most expensive: `nable scan` is the first thing a new user runs.

So the check is not new code, it is a call to update_check from a second place.
The tests below pin the parts that are easy to get wrong: it must not slow the
scan down, it must stay silent for anyone who asked not to be contacted, and it
must not nag someone who is already current.
"""
from __future__ import annotations

import io
import sys
import threading
import time
import types

import pytest

import finops
import finops.cli_scan as cli_scan
import finops.update_check as update_check


class _FakeResponse:
    def __init__(self, version: str):
        self._version = version

    def json(self) -> dict:
        return {"info": {"version": self._version}}


@pytest.fixture
def pypi(monkeypatch):
    """Stand in for PyPI at the NETWORK boundary, not at latest_version().

    This matters and it caught a mistake while writing these tests. Replacing
    latest_version() wholesale also replaces the _disabled() check that lives
    inside it, so the airgap cases below passed a fake that could never have
    honoured the setting. Patching httpx keeps every line of update_check in the
    path, which is the only way the airgap assertions mean anything.
    """
    state = {"version": "0.8.210", "calls": 0, "delay": 0.0}

    def _get(*_a, **_kw):
        state["calls"] += 1
        if state["delay"]:
            time.sleep(state["delay"])
        return _FakeResponse(state["version"])

    monkeypatch.setitem(sys.modules, "httpx", types.SimpleNamespace(get=_get))
    update_check._checked.clear()
    yield state
    update_check._checked.clear()


@pytest.fixture(autouse=True)
def quiet_env(monkeypatch):
    """No telemetry from the tests, and no inherited opt-out masking a result."""
    monkeypatch.setattr(cli_scan, "_emit", lambda *a, **k: None)
    for var in ("FINOPS_AIRGAP", "FINOPS_NO_UPDATE_CHECK", "NABLE_NO_TELEMETRY"):
        monkeypatch.delenv(var, raising=False)


def _run_check_then_fail(monkeypatch, running_version: str) -> str:
    """Drive the real sequence: start the background check, then hit a failure."""
    monkeypatch.setattr(finops, "__version__", running_version)

    def _check():
        cli_scan._stale_note = update_check.staleness_note()

    cli_scan._stale_note = None
    cli_scan._stale_thread = threading.Thread(target=_check)
    cli_scan._stale_thread.start()

    out = io.StringIO()
    cli_scan._fail(out, 1, ["something broke"], "other", time.time())
    return out.getvalue()


# ── the case that motivated this ───────────────────────────────────────────────

def test_a_failing_scan_on_an_old_build_says_so(monkeypatch, pypi):
    """The exact situation from 2026-08-14, minus the AWS."""
    body = _run_check_then_fail(monkeypatch, "0.8.201")
    assert "0.8.210 is out (you are on 0.8.201)" in body, body
    assert "finops upgrade" in body, (
        "the note must name the command that actually moves the pin. `pip "
        "install finops-mcp` is what the old build said and it is a no-op on an "
        "already installed package"
    )


def test_the_failure_message_itself_is_still_shown(monkeypatch, pypi):
    """Staleness is additional context, never a replacement for the error.

    A version gap does not prove THIS failure is fixed upstream, so swallowing
    the real message in favour of "you are out of date" would trade one
    unactionable answer for another.
    """
    body = _run_check_then_fail(monkeypatch, "0.8.201")
    assert "something broke" in body


def test_a_current_build_is_not_nagged(monkeypatch, pypi):
    body = _run_check_then_fail(monkeypatch, "0.8.210")
    assert "is out (you are on" not in body


def test_a_dev_build_ahead_of_pypi_is_not_nagged(monkeypatch, pypi):
    """Working from a checkout must not produce a downgrade suggestion."""
    body = _run_check_then_fail(monkeypatch, "0.9.0")
    assert "is out (you are on" not in body


# ── the promises the check must not break ─────────────────────────────────────

@pytest.mark.parametrize(
    "var", ["FINOPS_AIRGAP", "FINOPS_NO_UPDATE_CHECK", "NABLE_NO_TELEMETRY"])
def test_an_opted_out_user_is_never_contacted(monkeypatch, pypi, var):
    """No note, and more importantly no packet.

    nable publishes a network manifest and the enterprise audit is an air-gapped
    run plus a packet capture. A staleness check that phoned PyPI anyway would
    break that claim to save someone a version bump.
    """
    monkeypatch.setenv(var, "1")
    body = _run_check_then_fail(monkeypatch, "0.8.201")
    assert "is out (you are on" not in body
    assert pypi["calls"] == 0, (
        f"{var} is set and the check still called PyPI {pypi['calls']} time(s)"
    )


def test_a_slow_pypi_does_not_hold_up_the_result(monkeypatch, pypi):
    """The scan's own speed is the product. The note is not worth waiting for.

    `nable scan` exists to print something useful in about two seconds. If PyPI
    is slow the note is dropped, because a user staring at a failed scan should
    not also wait on an advisory lookup.
    """
    pypi["delay"] = 3.0
    started = time.time()
    body = _run_check_then_fail(monkeypatch, "0.8.201")
    elapsed = time.time() - started

    assert elapsed < 1.5, f"the failure path waited {elapsed:.1f}s on the update check"
    assert "is out (you are on" not in body, (
        "the note arrived late and was printed anyway, which means the join is "
        "not bounded"
    )


def test_the_check_runs_from_the_scan_entry_point(monkeypatch):
    """The wiring, not just the helper.

    Every test above drives _fail directly with a thread this file started. That
    proves the rendering and leaves the real question open: does `nable scan`
    actually start the check? It did not for the entire life of the CLI, which is
    the whole reason those 7 machines never saw a word about it.
    """
    import ast
    import inspect

    src = inspect.getsource(cli_scan)
    tree = ast.parse(src)
    fn = next(
        (n for n in ast.walk(tree)
         if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == "run"),
        None,
    )
    assert fn is not None, "cli_scan.run is gone; `nable scan` no longer has an entry point"
    body = ast.unparse(fn)
    assert "staleness_note" in body, (
        "cli_scan.run never starts the staleness check, so the note can only "
        "ever be printed by a test"
    )
    assert "Thread" in body, (
        "the check is called inline; a 2 second PyPI round trip now sits in "
        "front of a scan that promises to print in about two"
    )
