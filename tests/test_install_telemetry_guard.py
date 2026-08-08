"""
install_completed must count real human installs, not automation.

It used to fire on ANY first CLI run, so cache-warm subprocesses, piped
invocations, CI runners, and fresh uvx environments each logged a phantom
install with a throwaway id. These tests lock the fix: only an interactive,
non-CI first run counts, and CI sends no telemetry at all.
"""
from __future__ import annotations

from unittest.mock import patch

import finops.telemetry as tel
import finops.welcome as w


def _clear_ci(monkeypatch):
    for v in tel._CI_ENV_VARS:
        monkeypatch.delenv(v, raising=False)


def test_is_ci_detects_runners(monkeypatch):
    _clear_ci(monkeypatch)
    assert not tel.is_ci()
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    assert tel.is_ci()


def test_ci_opts_out_of_all_telemetry(monkeypatch):
    monkeypatch.setattr(tel, "_POSTHOG_KEY", "phc_x")
    monkeypatch.delenv("FINOPS_AIRGAP", raising=False)
    monkeypatch.delenv(tel._OPT_OUT_ENV, raising=False)
    _clear_ci(monkeypatch)
    # Telemetry is opt-in as of 2026-08-08, so the baseline is OFF; opt in
    # explicitly, then prove CI still overrides it.
    monkeypatch.setenv(tel._OPT_IN_ENV, "1")
    assert not tel._is_opted_out()
    monkeypatch.setenv("CI", "1")
    assert tel._is_opted_out()


def test_send_event_itself_honors_opt_out(monkeypatch):
    """_send_event is called directly by the setup wizard and server, so the
    opt-out check must live inside it, not only in the wrappers. A CI/air-gap/
    opted-out environment must produce zero network calls."""
    import sys

    monkeypatch.setattr(tel, "_POSTHOG_KEY", "phc_x")
    monkeypatch.setenv("CI", "1")

    posted = []

    class _FakeHttpx:
        @staticmethod
        def post(*a, **k):
            posted.append(a)

    monkeypatch.setitem(sys.modules, "httpx", _FakeHttpx)
    tel._send_event("install-id", "setup_wizard_started", {})
    assert posted == []

    # Clearing CI is no longer enough: telemetry is opt-in, so still nothing.
    _clear_ci(monkeypatch)
    monkeypatch.delenv("FINOPS_AIRGAP", raising=False)
    monkeypatch.delenv(tel._OPT_OUT_ENV, raising=False)
    monkeypatch.delenv(tel._OPT_IN_ENV, raising=False)
    tel._send_event("install-id", "setup_wizard_started", {})
    assert posted == [], "opt-in missing, so _send_event must still send nothing"

    # Only an explicit opt-in produces a call.
    monkeypatch.setenv(tel._OPT_IN_ENV, "1")
    tel._send_event("install-id", "setup_wizard_started", {})
    assert len(posted) == 1


def test_no_telemetry_env_zero_does_not_by_itself_turn_it_on(monkeypatch):
    """Under opt-in, NABLE_NO_TELEMETRY=0 only means "not force-disabled"; it is
    not consent. Consent is NABLE_TELEMETRY=1 and nothing else."""
    monkeypatch.setattr(tel, "_POSTHOG_KEY", "phc_x")
    monkeypatch.delenv("FINOPS_AIRGAP", raising=False)
    _clear_ci(monkeypatch)
    monkeypatch.delenv(tel._OPT_IN_ENV, raising=False)
    monkeypatch.setenv(tel._OPT_OUT_ENV, "0")
    assert tel._is_opted_out(), "0 on the opt-out var must not imply consent"
    monkeypatch.setenv(tel._OPT_IN_ENV, "1")
    assert not tel._is_opted_out()
    # and the hard override still beats an explicit opt-in
    monkeypatch.setenv(tel._OPT_OUT_ENV, "1")
    assert tel._is_opted_out()


def test_nothing_is_sent_by_default(monkeypatch):
    """The change itself: a clean machine sends nothing until asked."""
    monkeypatch.setattr(tel, "_POSTHOG_KEY", "phc_x")
    monkeypatch.delenv("FINOPS_AIRGAP", raising=False)
    monkeypatch.delenv(tel._OPT_OUT_ENV, raising=False)
    monkeypatch.delenv(tel._OPT_IN_ENV, raising=False)
    _clear_ci(monkeypatch)
    assert tel._is_opted_out(), "telemetry must be off with nothing configured"


def _run_show_welcome(monkeypatch, *, first_run, ci, stdin_tty, stdout_tty):
    _clear_ci(monkeypatch)
    if ci:
        monkeypatch.setenv("GITHUB_ACTIONS", "true")
    monkeypatch.setattr(w, "_is_first_run", lambda: first_run)
    monkeypatch.setattr(w, "_mark_welcomed", lambda: None)
    monkeypatch.setattr(w, "_print_header", lambda: None)
    monkeypatch.setattr("sys.stdin.isatty", lambda: stdin_tty, raising=False)
    monkeypatch.setattr("sys.stdout.isatty", lambda: stdout_tty, raising=False)
    fired = []
    monkeypatch.setattr(w, "_fire_telemetry", lambda e, p: fired.append((e, p)))
    # Stop after the gated telemetry so we don't print the whole welcome.
    monkeypatch.setattr(w, "_line", lambda *a, **k: None)
    monkeypatch.setattr(w, "_blank", lambda *a, **k: None)
    monkeypatch.setattr(w, "bold", lambda t: t)
    w.show_welcome()
    return fired


def test_interactive_first_run_fires_install(monkeypatch):
    fired = _run_show_welcome(monkeypatch, first_run=True, ci=False, stdin_tty=True, stdout_tty=True)
    assert [e for e, _ in fired] == ["install_completed"]


def test_non_interactive_first_run_does_not_fire(monkeypatch):
    # piped stdin (cache-warm subprocess, scripts) -> not an install
    fired = _run_show_welcome(monkeypatch, first_run=True, ci=False, stdin_tty=False, stdout_tty=True)
    assert fired == []


def test_ci_first_run_does_not_fire(monkeypatch):
    fired = _run_show_welcome(monkeypatch, first_run=True, ci=True, stdin_tty=True, stdout_tty=True)
    assert fired == []


def test_already_welcomed_does_not_fire(monkeypatch):
    fired = _run_show_welcome(monkeypatch, first_run=False, ci=False, stdin_tty=True, stdout_tty=True)
    assert fired == []


def test_non_interactive_leaves_sentinel_unset(monkeypatch):
    """A piped first run must NOT consume the first-run sentinel, so the next
    genuine human run still counts."""
    marked = []
    _clear_ci(monkeypatch)
    monkeypatch.setattr(w, "_is_first_run", lambda: True)
    monkeypatch.setattr(w, "_mark_welcomed", lambda: marked.append(True))
    monkeypatch.setattr("sys.stdin.isatty", lambda: False, raising=False)
    monkeypatch.setattr("sys.stdout.isatty", lambda: True, raising=False)
    monkeypatch.setattr(w, "_fire_telemetry", lambda e, p: None)
    w.show_welcome()
    assert marked == []  # sentinel never marked on a non-interactive run
