# SPDX-License-Identifier: Apache-2.0
"""On an old interpreter, do the thing instead of explaining it.

Measured 2026-08-17 on a stock Mac: `uvx` resolved against a Python 3.10
framework install, so `uvx nable` exited 1 and printed the right command to run
instead. That message is good and it is still a failure. The person has to read
it, work out why their own python is the problem, and type again, at exactly the
moment they are deciding whether this tool is worth the trouble.

uv can fetch an interpreter in a couple of seconds. So the shim now re-runs
itself under a managed 3.12 rather than asking.

The three properties that make this safe, each pinned below:

  It cannot loop. The child is marked, and a marked process falls back to the
  message rather than spawning another. Without that, a managed interpreter that
  was somehow still too old would fork forever.

  It cannot become a traceback. Every failure path returns to the old message.
  The entire reason this module exists is that an old interpreter never sees a
  Python traceback, and a re-exec that raises would undo that.

  It says what it is doing. An unexplained pause while uv downloads an
  interpreter reads as a hang, and a hang is worse than the message was.
"""
from __future__ import annotations

import collections
import pathlib
import subprocess
import sys

import pytest

SHIM = pathlib.Path(__file__).resolve().parents[1] / "shim"
sys.path.insert(0, str(SHIM))

import nable_shim  # noqa: E402

# The shim reads sys.version_info.major, so a plain tuple is not a stand-in.
_V = collections.namedtuple("v", "major minor micro releaselevel serial")


def _ver(major: int, minor: int) -> tuple:
    return _V(major, minor, 0, "final", 0)


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    monkeypatch.delenv(nable_shim._REEXEC, raising=False)


# ── it cannot loop ───────────────────────────────────────────────────────────

def test_a_child_never_spawns_another_child(monkeypatch):
    """The loop guard, and the reason the sentinel exists at all."""
    monkeypatch.setenv(nable_shim._REEXEC, "1")
    monkeypatch.setattr(nable_shim.shutil, "which", lambda _: "/usr/bin/uvx")
    called: list = []
    monkeypatch.setattr(nable_shim.subprocess, "run",
                        lambda *a, **k: called.append(a) or None)

    assert nable_shim._reexec_under_managed_python([]) is None, (
        "a process already marked as a re-exec tried to spawn another one")
    assert not called, "the child shelled out again; this is the fork bomb"


# ── it cannot become a traceback ─────────────────────────────────────────────

def test_no_uv_on_the_machine_falls_back_to_the_message(monkeypatch):
    monkeypatch.setattr(nable_shim.shutil, "which", lambda _: None)
    assert nable_shim._reexec_under_managed_python([]) is None


@pytest.mark.parametrize("boom", [OSError("no such file"),
                                  subprocess.SubprocessError("spawn failed")])
def test_a_failed_spawn_degrades_into_the_message_not_an_exception(monkeypatch, boom):
    """The old behaviour is the floor, and nothing is allowed to fall below it."""
    monkeypatch.setattr(nable_shim.shutil, "which", lambda _: "/usr/bin/uvx")

    def _raise(*a, **k):
        raise boom

    monkeypatch.setattr(nable_shim.subprocess, "run", _raise)
    assert nable_shim._reexec_under_managed_python([]) is None


def test_main_prints_the_old_message_when_it_cannot_reexec(monkeypatch, capsys):
    """End to end on the fallback: an old interpreter with no uv is unchanged."""
    monkeypatch.setattr(nable_shim.sys, "version_info", _ver(3, 10))
    monkeypatch.setattr(nable_shim.shutil, "which", lambda _: None)

    assert nable_shim.main([]) == 1
    err = capsys.readouterr().err
    assert "needs Python 3.11 or newer" in err
    assert "uvx --python 3.12 nable" in err, "the remedy has to still be there"


# ── it says what it is doing, and runs the right thing ───────────────────────

def test_it_announces_the_fetch_before_the_pause(monkeypatch, capsys):
    monkeypatch.setattr(nable_shim.sys, "version_info", _ver(3, 10))
    monkeypatch.setattr(nable_shim.shutil, "which", lambda _: "/usr/bin/uvx")
    monkeypatch.setattr(nable_shim.subprocess, "run",
                        lambda *a, **k: type("R", (), {"returncode": 0})())

    nable_shim._reexec_under_managed_python([])

    err = capsys.readouterr().err
    assert "3.10" in err and "3.12" in err, "it has to name both versions"
    assert "continuing" in err.lower(), (
        "silence while uv downloads an interpreter reads as a hang")


def test_the_childs_exit_code_is_the_shims_exit_code(monkeypatch):
    """`nable scan` failing inside the child must not look like success."""
    monkeypatch.setattr(nable_shim.sys, "version_info", _ver(3, 10))
    monkeypatch.setattr(nable_shim.shutil, "which", lambda _: "/usr/bin/uvx")
    monkeypatch.setattr(nable_shim.subprocess, "run",
                        lambda *a, **k: type("R", (), {"returncode": 3})())

    assert nable_shim.main(["scan"]) == 3


def test_the_users_arguments_survive_the_hop(monkeypatch):
    """A re-exec that drops argv turns `nable scan --json` into `nable`."""
    monkeypatch.setattr(nable_shim.sys, "version_info", _ver(3, 10))
    monkeypatch.setattr(nable_shim.shutil, "which", lambda _: "/usr/bin/uvx")
    seen: list[list[str]] = []
    monkeypatch.setattr(nable_shim.subprocess, "run",
                        lambda cmd, **k: seen.append(cmd) or type("R", (), {"returncode": 0})())

    nable_shim.main(["scan", "--json", "--days", "7"])

    assert seen and seen[0][-4:] == ["scan", "--json", "--days", "7"]
    assert "--python" in seen[0] and "3.12" in seen[0]


@pytest.mark.parametrize("path,expected_head", [
    ("/usr/bin/uvx", ["--python", "3.12", "nable"]),
    ("/opt/homebrew/bin/uv", ["tool", "run", "--python", "3.12", "nable"]),
])
def test_it_speaks_both_uvx_and_uv(path, expected_head):
    """uvx and `uv tool run` are the same thing under two names, and a machine
    may have either. Getting this wrong means the re-exec runs nothing."""
    assert nable_shim._reexec_argv(path, ["scan"])[1:] == expected_head + ["scan"]


# ── the modern path is untouched ─────────────────────────────────────────────

def test_a_modern_interpreter_never_shells_out(monkeypatch):
    """The re-exec must be invisible to everybody it is not for."""
    monkeypatch.setattr(nable_shim.sys, "version_info", _ver(3, 12))
    monkeypatch.setattr(nable_shim.shutil, "which",
                        lambda _: pytest.fail("a 3.12 interpreter looked for uv"))
    monkeypatch.setattr(nable_shim.subprocess, "run",
                        lambda *a, **k: pytest.fail("a 3.12 interpreter re-executed"))

    # Delegates to finops.entry, which is present in this checkout.
    monkeypatch.setattr("finops.entry.main", lambda: 0)
    assert nable_shim.main([]) == 0


def test_ctrl_c_during_the_fetch_is_not_a_traceback(monkeypatch, capsys):
    """The most common way anybody stops a download.

    KeyboardInterrupt is not an OSError or a SubprocessError, so it raised
    straight through the handler and printed the stack trace this whole module
    exists to keep off an old interpreter's screen. Found by adversarial review
    after I had tested five failure modes and not the one a person performs.

    130 because that is what a shell expects from SIGINT, and returning it rather
    than re-raising means the parent exits quietly too.
    """
    monkeypatch.setattr(nable_shim.sys, "version_info", _ver(3, 10))
    monkeypatch.setattr(nable_shim.shutil, "which", lambda _: "/usr/bin/uvx")

    def _interrupt(*a, **k):
        raise KeyboardInterrupt

    monkeypatch.setattr(nable_shim.subprocess, "run", _interrupt)

    assert nable_shim.main(["scan"]) == 130, (
        "Ctrl-C during the re-exec did not produce a clean SIGINT exit")
    err = capsys.readouterr().err
    assert "Traceback" not in err, "a stack trace is not an answer"
    assert "Stopped" in err, "silence after Ctrl-C looks like a crash that ate the output"
