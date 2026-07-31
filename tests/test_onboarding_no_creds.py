"""The no-credentials path must never recommend a command that cannot work.

Found by walking the real flow on a machine with aws-cli/1.38.38:

    Run `aws configure sso` for you now? [Y/n] y
    aws: error: argument subcommand: Invalid choice, valid choices are:
        list | get | set | add-model

    Leave this running. nable connects the moment credentials appear.
    | waiting for credentials... (1m, Ctrl-C for other options)

`aws configure sso` is AWS CLI v2 only. v1 is still what pip and older Homebrew
formulas install. nable checked `shutil.which("aws")`, which is true for v1,
recommended a v2-only subcommand, ignored the non-zero exit, and then sat in a
15-minute watch for credentials that could never arrive. For someone with no AWS
credentials, which is exactly the person this screen exists for, that is the end
of the funnel.

Two invariants:
  - never offer `configure sso` to a CLI that does not have it
  - a login that fails must say so and offer a path that needs no CLI at all
"""
from __future__ import annotations

import subprocess

import pytest

from finops import setup_wizard as w


# ── version detection ─────────────────────────────────────────────────────────

@pytest.mark.parametrize("banner,expected", [
    ("aws-cli/1.38.38 Python/3.8.8 Darwin/25.4.0 botocore/1.37.38", 1),
    ("aws-cli/2.15.0 Python/3.11.6 Darwin/23.1.0 source/arm64", 2),
    ("aws-cli/2.0.0 Python/3.8.0", 2),
])
def test_reads_the_major_version_from_the_banner(monkeypatch, banner, expected):
    monkeypatch.setattr(subprocess, "run",
                        lambda *a, **k: subprocess.CompletedProcess(a, 0, banner, ""))
    assert w._aws_cli_major() == expected


def test_version_on_stderr_is_still_read(monkeypatch):
    """Older CLIs print --version to stderr."""
    monkeypatch.setattr(subprocess, "run",
                        lambda *a, **k: subprocess.CompletedProcess(a, 0, "", "aws-cli/1.16.0 Python/2.7"))
    assert w._aws_cli_major() == 1


@pytest.mark.parametrize("boom", [FileNotFoundError, OSError, subprocess.TimeoutExpired("aws", 5)])
def test_an_undetectable_version_is_none_not_a_crash(monkeypatch, boom):
    def raise_it(*a, **k):
        raise boom if not isinstance(boom, type) else boom()
    monkeypatch.setattr(subprocess, "run", raise_it)
    assert w._aws_cli_major() is None


def test_unparseable_banner_is_none(monkeypatch):
    monkeypatch.setattr(subprocess, "run",
                        lambda *a, **k: subprocess.CompletedProcess(a, 0, "some other tool v3", ""))
    assert w._aws_cli_major() is None


# ── the exit code has to matter ───────────────────────────────────────────────

def test_a_failed_login_reports_failure(monkeypatch, capsys):
    monkeypatch.setattr(w, "_prompt", lambda *a, **k: "y")
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: subprocess.CompletedProcess(a, 252))
    assert w._offer_run_aws_login(["aws", "configure", "sso"]) is False
    out = capsys.readouterr().out
    assert "252" in out, "a failing login must say it failed, not stay silent"


def test_a_successful_login_reports_success(monkeypatch):
    monkeypatch.setattr(w, "_prompt", lambda *a, **k: "y")
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: subprocess.CompletedProcess(a, 0))
    assert w._offer_run_aws_login(["aws", "sso", "login"]) is True


def test_declining_is_not_a_success(monkeypatch):
    monkeypatch.setattr(w, "_prompt", lambda *a, **k: "n")
    called = []
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: called.append(a))
    assert w._offer_run_aws_login(["aws", "configure", "sso"]) is False
    assert not called, "declined, so nothing should have run"


def test_a_missing_binary_is_not_a_success(monkeypatch):
    monkeypatch.setattr(w, "_prompt", lambda *a, **k: "y")
    def boom(*a, **k):
        raise FileNotFoundError("no aws")
    monkeypatch.setattr(subprocess, "run", boom)
    assert w._offer_run_aws_login(["aws", "configure", "sso"]) is False


# ── the recommendation itself ─────────────────────────────────────────────────

def _offered_command(monkeypatch, capsys, *, cli_major, login_ok=True):
    """Run the guide with a terminal attached and capture the command it offers
    to run. Asserting on the offered command rather than the printed text is the
    point: the v1 copy legitimately mentions `aws configure sso` while explaining
    why it is unavailable, and a substring check cannot tell that apart from a
    recommendation."""
    seen: list[list[str]] = []
    monkeypatch.setattr("shutil.which", lambda n: "/usr/bin/aws" if n == "aws" else None)
    monkeypatch.setattr(w, "_aws_cli_major", lambda: cli_major)
    monkeypatch.setattr(w, "_detect_sso_profiles_needing_login", lambda: [])
    monkeypatch.setattr("sys.stdin.isatty", lambda: True)
    monkeypatch.setattr("sys.stdout.isatty", lambda: True)
    monkeypatch.setattr(w, "_offer_run_aws_login",
                        lambda cmd: (seen.append(cmd), login_ok)[1])
    monkeypatch.setattr(w, "_watch_for_aws_creds", lambda *a, **k: [])
    w._guide_and_watch_for_creds(set())
    return (seen[0] if seen else []), capsys.readouterr().out


def test_v1_is_never_offered_configure_sso(monkeypatch, capsys):
    cmd, out = _offered_command(monkeypatch, capsys, cli_major=1)
    assert cmd != ["aws", "configure", "sso"], (
        "offered a v2-only subcommand to a v1 CLI; it exits Invalid choice and "
        "the user then waits 15 minutes for credentials that never arrive"
    )
    assert cmd == ["aws", "configure"]


def test_v2_still_gets_the_sso_flow(monkeypatch, capsys):
    cmd, _ = _offered_command(monkeypatch, capsys, cli_major=2)
    assert cmd == ["aws", "configure", "sso"]


def test_an_unknown_version_does_not_assume_v2(monkeypatch, capsys):
    """which() found something but --version did not parse. Guessing v2 is how
    the original bug behaved; prefer the path that works everywhere."""
    cmd, _ = _offered_command(monkeypatch, capsys, cli_major=None)
    assert cmd != ["aws", "configure", "sso"]


def test_a_failed_login_offers_a_path_that_needs_no_cli(monkeypatch, capsys):
    """The whole bug: the login fails, and nable says 'waiting for credentials'
    anyway. The user must be told it failed and given a way out."""
    _, out = _offered_command(monkeypatch, capsys, cli_major=1, login_ok=False)
    assert "did not produce credentials" in out
    assert "CloudShell" in out, "no CLI-free path offered after the CLI failed"


def test_a_successful_login_does_not_show_the_failure_help(monkeypatch, capsys):
    _, out = _offered_command(monkeypatch, capsys, cli_major=2, login_ok=True)
    assert "did not produce credentials" not in out
