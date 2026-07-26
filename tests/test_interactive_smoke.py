"""Interactive first-run smoke tests, driven through a real PTY.

Why this file exists: the suite kept passing while the *interactive* first run broke
in ways units never saw, a NameError in the no-creds screen, a NOT NULL crash at the
budget step, an ai-budget status that told you to set a budget you just set. Every one
was found by a human dogfooding, not by CI. These tests spawn the actual `finops`
binary in a pty, feed real keystrokes when the expected prompt appears, and assert two
things units can't: no Python traceback reaches the user, and the flow never hangs.

Prompt-driven (not timer-driven) input keeps it reliable. Runs in a scratch HOME with
no ~/.aws so it exercises the no-credentials path, and never touches the network.
"""
from __future__ import annotations

import os
import select
import sys
import time
from pathlib import Path

import pytest

FINOPS = Path(sys.executable).with_name("finops")

pytestmark = [
    pytest.mark.skipif(not hasattr(os, "openpty"), reason="no pty (Windows)"),
    pytest.mark.skipif(not FINOPS.exists(), reason="finops console script not installed"),
]


def _drive(args, steps, env, timeout=30):
    """Spawn args in a pty. `steps` is [(expect_substring, send_text)]; each send fires
    once its prompt substring has appeared. Returns (output, exited_cleanly)."""
    import pty

    pid, fd = pty.fork()
    if pid == 0:  # child
        # Hermetic: strip any inherited cloud creds so "no credentials" paths are
        # deterministic regardless of the developer's / CI runner's environment.
        for _k in list(os.environ):
            if _k.startswith(("AWS", "GOOGLE", "AZURE", "GCP", "OPENAI", "ANTHROPIC")):
                del os.environ[_k]
        os.environ.update(env)
        try:
            os.execv(str(args[0]), [str(a) for a in args])
        except Exception:
            os._exit(127)

    buf = ""
    sent = 0
    matched_upto = 0  # only look for the next prompt in text after the last match
    start = time.time()
    exited_cleanly = False
    while time.time() - start < timeout:
        r, _, _ = select.select([fd], [], [], 0.3)
        if r:
            try:
                data = os.read(fd, 4096)
            except OSError:
                # EIO: the child closed the pty, i.e. it exited on its own. That is a
                # clean termination, not the hang the timeout path represents.
                exited_cleanly = True
                break
            if not data:  # EOF: child exited on its own
                exited_cleanly = True
                break
            buf += data.decode(errors="ignore")
        if sent < len(steps):
            expect, send = steps[sent]
            idx = buf.find(expect, matched_upto)
            if idx != -1:
                os.write(fd, send.encode())
                matched_upto = idx + len(expect)
                sent += 1
        try:
            done, _ = os.waitpid(pid, os.WNOHANG)
            if done:
                exited_cleanly = True
                time.sleep(0.15)
                try:
                    while True:
                        d = os.read(fd, 4096)
                        if not d:
                            break
                        buf += d.decode(errors="ignore")
                except OSError:
                    pass
                break
        except OSError:
            break
    if not exited_cleanly:
        try:
            os.write(fd, b"\x03")  # Ctrl-C
            time.sleep(0.3)
            os.kill(pid, 9)
            os.waitpid(pid, 0)
        except OSError:
            pass
    return buf, exited_cleanly


@pytest.fixture()
def sandbox_env(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    return {
        "HOME": str(home),
        "FINOPS_DATA_DIR": str(tmp_path / "data"),
        "CLAUDE_CONFIG_DIR": str(tmp_path / "noclaude"),  # no agent usage
        "NABLE_NO_TELEMETRY": "1",
        "TERM": "xterm",
        "PATH": os.environ.get("PATH", ""),
    }


def test_ai_budget_interactive_first_run(sandbox_env):
    """The front-door setup that broke twice: run it, answer flat-plan/$100, and it must
    finish, print no traceback, and CONFIRM the budget (not tell you to set one)."""
    out, exited = _drive(
        [FINOPS, "ai-budget"],
        [("Choose 1 or 2:", "1\n"),
         ("What do you pay per month", "100\n"),
         ("warn before", "\n")],
        sandbox_env,
        timeout=30,
    )
    assert "Traceback" not in out, f"traceback in ai-budget first run:\n{out[-800:]}"
    assert exited, f"ai-budget did not exit (hang?):\n{out[-800:]}"
    assert "$100/mo flat" in out                          # the plan is confirmed
    assert "to set a budget" not in out          # wording-independent: no nag after setting one


def _seed_legacy_budgets_db(path: Path) -> None:
    """A pre-current budgets table with the orphaned block_at_pct NOT NULL column,
    the shape that crashed the onboarding budget step before 0.8.188."""
    import sqlite3

    path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(path)
    con.execute(
        "CREATE TABLE budgets ("
        "id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL, scope_type TEXT NOT NULL,"
        "scope_value TEXT NOT NULL DEFAULT '*', period TEXT NOT NULL DEFAULT 'monthly',"
        "limit_usd REAL NOT NULL, block_at_pct REAL NOT NULL, created_at TEXT NOT NULL,"
        "updated_at TEXT NOT NULL, created_by TEXT NOT NULL DEFAULT '', is_active INTEGER NOT NULL DEFAULT 1)"
    )
    con.commit()
    con.close()


def test_welcome_full_flow_reaches_and_saves_the_budget(sandbox_env, tmp_path):
    """The full welcome -> budget step, the one that dumped a traceback on an upgraded
    DB. Seed the legacy schema and drive `finops welcome` to the budget prompt (via the
    test seam that stands in for the live-scan total). It must save cleanly and confirm,
    never crash. This gates the exact regression a human found by dogfooding."""
    _seed_legacy_budgets_db(tmp_path / "data" / "finops.db")
    env = {**sandbox_env, "FINOPS_TEST_ONBOARDING_TOTAL": "17073"}
    out, exited = _drive(
        [FINOPS, "welcome"],
        [("Monthly budget in USD", "\n")],   # accept the suggested amount
        env,
        timeout=30,
    )
    assert "Traceback" not in out, f"traceback in welcome budget step:\n{out[-900:]}"
    assert exited, f"welcome did not exit (hang?):\n{out[-900:]}"
    assert "Budget set" in out, f"budget was not saved cleanly:\n{out[-900:]}"


def test_setup_aws_no_creds_screen_renders_without_crash(sandbox_env):
    """The no-credentials guided screen (which once died on a NameError before it ever
    watched). Decline the offer to run the login; assert the screen rendered and no
    traceback appeared. Then it's watching, so we let the harness Ctrl-C/kill it."""
    out, _ = _drive(
        [FINOPS, "setup", "aws"],
        [("Run `aws configure sso` for you now?", "n\n")],
        sandbox_env,
        timeout=9,
    )
    assert "Traceback" not in out, f"traceback in no-creds screen:\n{out[-800:]}"
    assert "No AWS credentials found" in out
    assert "aws configure sso" in out                     # the guided command is shown
