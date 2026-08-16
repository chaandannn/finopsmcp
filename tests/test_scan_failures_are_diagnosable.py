# SPDX-License-Identifier: Apache-2.0
"""A failure event that names no cause is a failure nobody can fix.

Measured 2026-08-16 over 14 days of real telemetry: 44 machines ran `nable scan`
and 42 of them hit a failure. Two ever completed one. That is the activation
cliff, and it was undiagnosable:

    error_class    machines   what the event said
    other               36    exc_type="", no cause at all
    missing_dep          6    ModuleNotFoundError

The 36 came from one site. `run_deep_audit` returns {"error": "Could not create
AWS session: <exc>"}, cli_scan printed it and called _fail(..., "other") with no
exception, so the type was formatted into a string at the source and thrown away
before telemetry ever saw it. The user got a message; we got nothing.

THE RULE, and why it is not just "log more". cli_scan's own docstring forbids
sending the message, because messages interpolate exceptions and carry paths and
account ids. So a site cannot fix this by passing the string along. It has to
carry a SAFE, CATEGORICAL cause: an exception type name, a count, a classifier
output. That is a different discipline from logging, and it is the one this file
enforces.

Related failure of the same shape, in this session: I queried PostHog for
`properties.reason`, a field nothing ever emitted, saw null everywhere and
concluded the instrumentation was blind. It was not; I asked for the wrong name.
Both mistakes have the same root, which is that nobody had written down what a
scan failure event is supposed to contain.
"""
from __future__ import annotations

import ast
import inspect
import pathlib

import pytest

from finops import cli_scan


def _fail_call_sites() -> list[tuple[int, str, bool, bool]]:
    """(lineno, error_class, passes_exc, passes_props) for every _fail call."""
    src = inspect.getsource(cli_scan)
    out = []
    for n in ast.walk(ast.parse(src)):
        if not (isinstance(n, ast.Call) and getattr(n.func, "id", None) == "_fail"):
            continue
        # _fail(out, code, lines, error_class, t0, exc=None, props=None)
        # exc and props can arrive POSITIONALLY (index 5 and 6). Checking only
        # keywords missed `_fail(out, 1, lines, cls, t0, exc, props=deps)` and
        # reported a well-instrumented site as blind.
        ec = ast.unparse(n.args[3]).strip("'\"") if len(n.args) > 3 else "?"
        kw = {k.arg for k in n.keywords}
        has_exc = "exc" in kw or len(n.args) > 5
        has_props = "props" in kw or len(n.args) > 6
        out.append((n.lineno, ec, has_exc, has_props))
    return out


# Classes that name a cause on their own. "no-creds" or "profile-missing" IS the
# diagnosis, and those sites have no exception to pass because they are
# deductions (an empty region list, a None credential), not catches. Demanding a
# cause from them would be cargo-culting the rule rather than applying it.
SPECIFIC = {
    "no-creds", "expired", "bad-creds", "permission", "profile-missing",
    "config-broken", "no-region", "network", "timeout", "bad-region-arg",
}
# missing_dep / broken_dep are deliberately absent: that site passes error_class
# as a VARIABLE, so it reads as non-specific here and is held to the stricter
# rule. It passes an exception, so it clears it anyway. Exempting a name the
# extractor cannot see would be exempting nothing.


def test_every_generic_failure_carries_a_cause():
    """A site that gives up and says "other" must say something else instead.

    This is the exact event 36 machines sent in 14 days: error_class="other",
    exc_type="", no cause anywhere. A precisely classified site has already done
    the job; a catch-all has not, so the catch-all owes an exception type, a
    count, or a classifier output.
    """
    blind = [
        (line, ec) for line, ec, has_exc, has_props in _fail_call_sites()
        if ec not in SPECIFIC and not has_exc and not has_props
    ]
    assert not blind, (
        "these _fail sites use a catch-all error_class and send neither an "
        "exception nor a diagnostic prop, so their events name no cause:\n  " +
        "\n  ".join(f"cli_scan.py:{line}  error_class={ec!r}" for line, ec in blind))


def test_the_specific_list_has_not_gone_stale():
    """An error_class that no site uses any more should leave this list.

    Otherwise SPECIFIC silently becomes a blanket exemption and the ratchet
    above stops catching anything.
    """
    in_use = {ec for _, ec, _, _ in _fail_call_sites()}
    stale = SPECIFIC - in_use
    assert not stale, (
        f"these error_class values are exempted but no longer used: "
        f"{sorted(stale)}. Drop them, or the exemption list is doing no work.")


def test_the_audit_keeps_the_exception_type_when_it_stringifies_the_message():
    """The source of the 36. Fixing cli_scan alone could not have worked.

    run_deep_audit formats the exception into a message it must never send, so
    unless it ALSO returns the type, there is nothing safe left to report.
    """
    from finops.analyzers import optimizer

    src = inspect.getsource(optimizer)
    tree = ast.parse(src)
    bad: list[int] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Return) or not isinstance(node.value, ast.Dict):
            continue
        keys = {k.value for k in node.value.keys if isinstance(k, ast.Constant)}
        if "error" in keys and "error_type" not in keys:
            bad.append(node.lineno)

    assert not bad, (
        "optimizer.py returns an error dict with no error_type at lines "
        f"{bad}; the exception type is the only part of the failure that is "
        f"safe to send, and stringifying the exception is what loses it")


def test_started_and_failed_can_be_joined_by_version():
    """Without a version on BOTH, "is the new build better" is unanswerable.

    Measured: 119 cli_scan_started in 14 days, every one with version=None,
    while every cli_scan_failed carried one. So the failure rate per release
    could not be computed, which is the number that says whether a fix worked.
    """
    src = inspect.getsource(cli_scan)
    started = [
        n for n in ast.walk(ast.parse(src))
        if isinstance(n, ast.Call) and getattr(n.func, "id", None) == "_emit"
        and n.args and isinstance(n.args[0], ast.Constant)
        and n.args[0].value == "cli_scan_started"
    ]
    assert started, "cli_scan_started is no longer emitted"
    for call in started:
        props = ast.unparse(call.args[1])
        assert "version" in props, (
            f"cli_scan.py:{call.lineno} emits cli_scan_started without a "
            f"version, so its failures cannot be attributed to a release")


def test_no_failure_site_sends_a_message(monkeypatch):
    """The rule that makes the rest of this file hard: causes must be safe.

    Messages interpolate the exception and carry paths, profile names and
    account ids. A site may send a type name, a count or a classifier output,
    never `str(exc)` and never a formatted line.
    """
    src = inspect.getsource(cli_scan)
    offenders: list[str] = []
    for n in ast.walk(ast.parse(src)):
        if not (isinstance(n, ast.Call) and getattr(n.func, "id", None) == "_fail"):
            continue
        props_kw = next((k for k in n.keywords if k.arg == "props"), None)
        if props_kw is None:
            continue
        rendered = ast.unparse(props_kw.value)
        if "str(exc" in rendered or "['error']" in rendered or '["error"]' in rendered:
            offenders.append(f"cli_scan.py:{n.lineno}  props={rendered}")
    assert not offenders, (
        "these sites put a message into telemetry props; send a type or a "
        "count instead:\n  " + "\n  ".join(offenders))
