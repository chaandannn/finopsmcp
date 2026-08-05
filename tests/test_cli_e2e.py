"""
End-to-end subprocess tests for the CLI front door.

These run finops.entry:main as a real child process (the same code path the
`nable` console script and the shim resolve to), so entry wiring, output, exit
codes and first-print latency are exercised for real, not mocked. The dev venv
may be older than the packaging floor, so console scripts are pinned via
pyproject metadata assertions instead of regenerating them here.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
# CI boxes are slow and shared; wall-clock asserts there flake. 2s is the
# product budget, asserted locally; CI gets slack.
FIRST_PRINT_BUDGET_S = 5.0 if os.getenv("CI") else 2.0

_ENV = {**os.environ, "NABLE_NO_TELEMETRY": "1", "NO_COLOR": "1"}


def _run_entry(*argv: str, timeout: float = 60.0):
    """Run finops.entry:main in a child process exactly as the console script would."""
    code = (
        "import sys; sys.argv = ['nable', *sys.argv[1:]]; "
        "from finops.entry import main; main()"
    )
    return subprocess.run(
        [sys.executable, "-c", code, *argv],
        capture_output=True,
        text=True,
        timeout=timeout,
        env=_ENV,
        cwd=str(REPO),
    )


def test_scan_demo_end_to_end():
    t0 = time.monotonic()
    proc = _run_entry("scan", "--demo")
    elapsed = time.monotonic() - t0

    assert proc.returncode == 0, proc.stderr
    assert "nable scan" in proc.stdout
    assert "recoverable" in proc.stdout
    assert "(demo data)" in proc.stdout
    assert "docs: https://getnable.com/docs/cli" in proc.stdout
    # Whole demo run (interpreter + imports + render) inside the first-print
    # budget: proof the light dispatcher never drags in the 0.9s server import.
    assert elapsed < FIRST_PRINT_BUDGET_S, f"took {elapsed:.2f}s"


def test_scan_demo_json_stdout_is_pure():
    proc = _run_entry("scan", "--demo", "--json")
    assert proc.returncode == 0, proc.stderr
    doc = json.loads(proc.stdout)  # any chrome on stdout breaks this parse
    assert doc["command"] == "scan" and doc["demo"] is True
    assert doc["recoverable"]["monthly_usd"] > 0


def test_help_leads_with_get_answers():
    proc = _run_entry("--help")
    assert proc.returncode == 0
    out = proc.stdout
    assert "get answers" in out
    assert out.index("get answers") < out.index("start here")
    assert out.index("scan") < out.index("welcome")


def test_console_script_targets_pinned_in_pyproject():
    # The generated `nable`/`finops-mcp` scripts come from this metadata; if it
    # regresses to finops.server:main, every `nable scan` pays the server
    # import and the shim repoint ships pointing at the wrong target.
    text = (REPO / "pyproject.toml").read_text()
    scripts = text.split("[project.scripts]", 1)[1].split("[", 1)[0]
    assert 'nable             = "finops.entry:main"' in scripts
    assert 'finops-mcp        = "finops.entry:main"' in scripts


def test_shim_targets_entry_and_floors_scan_release():
    text = (REPO / "shim" / "pyproject.toml").read_text()
    # 0.1.3 puts nable_shim between the console script and the dispatcher, so an
    # interpreter too old to install finops-mcp gets one actionable line instead
    # of an ImportError traceback. The requirement is unchanged in substance:
    # reach the light dispatcher, never the server module.
    assert 'nable = "nable_shim:main"' in text, (
        "shim must enter through the version guard"
    )
    guard = (REPO / "shim" / "nable_shim.py").read_text()
    assert "from finops.entry import main" in guard, (
        "the guard must delegate to the light dispatcher, not finops.server"
    )
    assert "finops.server" not in guard, (
        "importing the server costs ~0.9s before anything prints"
    )
    # The floor must be a version that ships finops/entry.py + scan, or cached
    # uv environments serve an old build with no scan subcommand (the exact
    # staleness trap that burned 0.1.0).
    import re

    m = re.search(r'finops-mcp>=([0-9.]+)', text)
    assert m, "shim must floor finops-mcp"
    floor = tuple(int(x) for x in m.group(1).split("."))
    assert floor >= (0, 8, 181), f"shim floor {m.group(1)} predates the scan release"
