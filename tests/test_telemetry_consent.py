# SPDX-License-Identifier: Apache-2.0
"""Asking once, in words, instead of requiring an environment variable.

Why this exists, measured 2026-08-15: telemetry went opt-in on 2026-08-08 in
0.8.210, gated on NABLE_TELEMETRY, and ZERO events have arrived from 0.8.210 or
later since. Not fewer. None. PyPI served 86-630 downloads a day straight
through the change, so the package was being installed the whole time and simply
stopped reporting. Every number we still have describes builds <= 0.8.209, and
what looked like "no scan has completed since 08-07" was the measurement going
dark on 08-08.

The opt-in decision was right and stays. Requiring an env var to express it was
the part that failed, because approximately nobody sets one.

The prompt does a second job the funnel needs. On 2026-08-14, 19 "machines"
appeared in 36 hours: one per released version, 0.8.201 through 0.8.209 in
order, three scans each seconds apart, five running a whole wizard-to-heartbeat
sequence in under a minute, none ever seen again. That is a harness walking the
release history, and in the data it is indistinguishable from 19 people having a
terrible first day. Only an interactive human can answer a prompt, so consent
sorts most of that by construction.

What these tests hold:
  - default is no, and every ambiguous answer is a no
  - asked exactly once, ever, whatever the answer
  - never asked where a prompt would hang or corrupt a stream
  - the promise still holds: nothing sends until someone says yes
"""
from __future__ import annotations

import builtins

import pytest

import finops.telemetry as tel


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    """Every test gets its own consent file and a clean environment."""
    monkeypatch.setattr(tel, "_CONSENT_FILE", tmp_path / ".telemetry")
    monkeypatch.setattr(tel, "_ID_FILE", tmp_path / ".install_id")
    monkeypatch.setattr(tel, "_PROMPT_ARMED", False)
    for var in ("NABLE_TELEMETRY", "NABLE_NO_TELEMETRY", "FINOPS_AIRGAP",
                *tel._CI_ENV_VARS):
        monkeypatch.delenv(var, raising=False)
    yield


def _tty(monkeypatch, on: bool = True):
    """Pretend there is (or is not) a human at a terminal.

    Overrides isatty on the EXISTING streams rather than replacing them. The
    first version swapped in a stub object, which also swallowed every print,
    so the test asserting the prompt names what it collects was reading an empty
    string and would have passed against a prompt that said nothing at all.
    """
    import sys
    for stream in (sys.stdin, sys.stdout):
        monkeypatch.setattr(stream, "isatty", lambda _on=on: _on, raising=False)


def _answers(monkeypatch, text: str):
    monkeypatch.setattr(builtins, "input", lambda *_a: text)


# ── the answer ────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("typed,expected", [
    ("y", True), ("Y", True), ("yes", True), ("Yeah", True),
    ("", False), ("n", False), ("no", False), ("maybe", False), ("  ", False),
])
def test_only_an_explicit_yes_turns_it_on(monkeypatch, capsys, typed, expected):
    """Default is no, and anything that is not a yes is a no.

    A bare Enter is the common case and must mean no. If an ambiguous answer
    ever counted as consent, the "off unless you say yes" claim would be false
    for exactly the people who did not read the question.
    """
    _tty(monkeypatch)
    _answers(monkeypatch, typed)
    assert tel.prompt_for_consent() is expected
    assert tel._stored_consent() is expected


@pytest.mark.parametrize("boom", [EOFError, KeyboardInterrupt])
def test_a_closed_stdin_or_a_ctrl_c_is_a_no_and_is_recorded(monkeypatch, capsys, boom):
    """Recorded, not just declined.

    If Ctrl-C left no answer on disk the question would come back on the next
    command, which is how a one-time prompt turns into nagging.
    """
    _tty(monkeypatch)
    monkeypatch.setattr(builtins, "input", lambda *_a: (_ for _ in ()).throw(boom()))
    assert tel.prompt_for_consent() is False
    assert tel._stored_consent() is False


def test_the_question_is_asked_exactly_once(monkeypatch, capsys):
    asked = {"n": 0}

    def _count(*_a):
        asked["n"] += 1
        return "y"

    _tty(monkeypatch)
    monkeypatch.setattr(builtins, "input", _count)
    tel.prompt_for_consent()
    tel.prompt_for_consent()
    tel.prompt_for_consent()
    assert asked["n"] == 1, f"asked {asked['n']} times; once means once"


# ── where we must never ask ───────────────────────────────────────────────────

def test_a_piped_stdin_is_never_prompted(monkeypatch):
    """The MCP server is this case and it is the dangerous one.

    It speaks JSON-RPC over stdio. A prompt written into that stream corrupts
    the protocol, and a read on a pipe that nobody will answer hangs the server
    forever. Verified end to end too: `nable scan --demo < /dev/null` prints no
    prompt and writes no consent file.
    """
    _tty(monkeypatch, on=False)
    monkeypatch.setattr(builtins, "input",
                        lambda *_a: pytest.fail("prompted with no terminal"))
    assert tel.prompt_for_consent() is None
    assert tel._stored_consent() is None, "a non-answer must not be recorded"


@pytest.mark.parametrize("var", ["CI", "GITHUB_ACTIONS", "RUNNER_OS", "BUILDKITE"])
def test_ci_is_never_prompted(monkeypatch, var):
    _tty(monkeypatch)
    monkeypatch.setenv(var, "1")
    monkeypatch.setattr(builtins, "input", lambda *_a: pytest.fail("prompted in CI"))
    assert tel.prompt_for_consent() is None


@pytest.mark.parametrize("var", ["NABLE_TELEMETRY", "NABLE_NO_TELEMETRY", "FINOPS_AIRGAP"])
def test_someone_who_already_decided_is_not_asked_again(monkeypatch, var):
    """An env var is a decision. Asking anyway would be asking someone to repeat
    themselves, and for FINOPS_AIRGAP it would be asking to break the promise."""
    _tty(monkeypatch)
    monkeypatch.setenv(var, "1")
    monkeypatch.setattr(builtins, "input", lambda *_a: pytest.fail(f"prompted with {var} set"))
    assert tel.prompt_for_consent() is None


# ── the promise ───────────────────────────────────────────────────────────────

def test_nothing_sends_until_someone_says_yes(monkeypatch):
    """The whole claim, checked at the gate every event passes through."""
    monkeypatch.setattr(tel, "_POSTHOG_KEY", "phc_test")

    assert tel._is_opted_out() is True, "never asked must stay OFF"

    tel._store_consent(False)
    assert tel._is_opted_out() is True, "a no must stay off"

    tel._store_consent(True)
    assert tel._is_opted_out() is False, "a yes is the point of asking"


def test_the_hard_override_still_beats_a_stored_yes(monkeypatch):
    """NABLE_NO_TELEMETRY set in a dotfile or an image wins over anything a
    prompt collected, including on another day."""
    monkeypatch.setattr(tel, "_POSTHOG_KEY", "phc_test")
    tel._store_consent(True)
    monkeypatch.setenv("NABLE_NO_TELEMETRY", "1")
    assert tel._is_opted_out() is True


def test_the_env_opt_in_still_works_untouched(monkeypatch):
    """Anyone who already put NABLE_TELEMETRY=1 in an image keeps working, and
    is never prompted."""
    monkeypatch.setattr(tel, "_POSTHOG_KEY", "phc_test")
    monkeypatch.setenv("NABLE_TELEMETRY", "1")
    assert tel._is_opted_out() is False
    assert tel._stored_consent() is None, "the env var must not need a file"


def test_the_prompt_names_what_is_and_is_not_collected(monkeypatch, capsys):
    """Consent that does not say what it covers is not consent.

    The specific words matter: someone deciding in one second needs to see that
    cost figures and account IDs are excluded, because that is the actual worry
    for a tool pointed at their cloud bill.
    """
    _tty(monkeypatch)
    _answers(monkeypatch, "n")
    tel.prompt_for_consent()
    shown = capsys.readouterr().out.lower()

    for promised in ("cost", "account", "path", "credential"):
        assert promised in shown, f"the prompt never mentions {promised}"
    assert "off unless you say yes" in shown
    assert "nable_telemetry" in shown, "no way back for someone who changes their mind"


# ── the wiring ────────────────────────────────────────────────────────────────

def test_the_cli_arms_the_prompt_and_the_server_does_not():
    """Armed from the interactive CLI, nowhere else.

    Testing prompt_for_consent alone would leave the real question open: does
    anything call it? It is registered on atexit so it fires after the command
    has printed, which is why the call site is one line in setup_wizard.main and
    not scattered through every subcommand.
    """
    import ast
    import inspect

    from finops import setup_wizard

    fn = next(
        n for n in ast.walk(ast.parse(inspect.getsource(setup_wizard)))
        if isinstance(n, ast.FunctionDef) and n.name == "main"
    )
    # The CALL, not the name. Checking `"arm_consent_prompt" in source` passes on
    # a main() that imports it and never calls it, which is exactly what a
    # mutation proved: replacing the call with `pass` left this test green while
    # the prompt became unreachable in the shipped product.
    calls = [
        n for n in ast.walk(fn)
        if isinstance(n, ast.Call)
        and (getattr(n.func, "id", None) == "arm_consent_prompt"
             or getattr(n.func, "attr", None) == "arm_consent_prompt")
    ]
    assert calls, (
        "setup_wizard.main imports arm_consent_prompt but never calls it, so "
        "the prompt can only ever be triggered by a test"
    )

    import finops.server as server
    assert "arm_consent_prompt" not in inspect.getsource(server), (
        "the MCP server arms an interactive prompt; its stdin is JSON-RPC"
    )


def test_arming_registers_exactly_one_atexit_hook(monkeypatch):
    """Twice-armed would ask twice in one process."""
    _tty(monkeypatch)
    registered: list = []
    import atexit
    monkeypatch.setattr(atexit, "register", lambda fn, *a, **k: registered.append(fn))

    tel.arm_consent_prompt()
    tel.arm_consent_prompt()
    tel.arm_consent_prompt()
    assert len(registered) == 1, f"{len(registered)} hooks registered"
