"""The `nable` shim must be installable everywhere an older `nable` was.

THE TRAP, measured 2026-08-05. Shim 0.1.0 allowed Python 3.10. 0.1.1 and 0.1.2
raised requires-python to 3.11 to stop uv serving a stale finops-mcp. That did
not close the trap, it created a worse one: on a 3.10 interpreter the two newer
versions excluded THEMSELVES, so the only installable candidate left was 0.1.0.
`uvx nable` there resolved to finops-mcp 0.8.87 — no scan command, no entry
dispatcher, and an mcp pin that raised ModuleNotFoundError on import.

The user got a Python traceback. We got nothing, because it crashed before any
of our code ran, telemetry included. 23 of 24 machines that started a scan in
the 30 days to 2026-08-05 never finished one.

The lesson is counter-intuitive and worth stating plainly: for a shim whose
whole job is to route people to the right thing, RAISING the floor hands the
floor to the oldest release. The newest version must always be installable, so
it always wins resolution, so it is always the thing that decides what happens
next.
"""
from __future__ import annotations

import pathlib
import re
import subprocess
import sys

import pytest

REPO = pathlib.Path(__file__).resolve().parent.parent
SHIM = REPO / "shim"
PYPROJECT = SHIM / "pyproject.toml"


def _field(name: str) -> str:
    text = PYPROJECT.read_text()
    m = re.search(rf'^{name}\s*=\s*"([^"]+)"', text, re.M)
    assert m, f"{name} not found in {PYPROJECT}"
    return m.group(1)


def _tuple(spec: str) -> tuple[int, ...]:
    m = re.search(r"(\d+)\.(\d+)", spec)
    assert m, f"cannot parse a version out of {spec!r}"
    return (int(m.group(1)), int(m.group(2)))


# The floors of every nable shim ever published. A new entry here is fine; the
# invariant below is what matters.
PUBLISHED_FLOORS = {
    "0.1.0": ">=3.10",
    "0.1.1": ">=3.11",
    "0.1.2": ">=3.11",
}


def test_the_shim_is_installable_wherever_any_older_shim_was():
    """The invariant that closes the trap.

    If the current shim's floor is HIGHER than any published version's floor,
    then on an interpreter between the two, the current version is uninstallable
    and an older one wins. For a router package that is fatal: the oldest,
    least-correct release ends up deciding the user's experience.
    """
    current = _tuple(_field("requires-python"))
    lowest_ever = min(_tuple(v) for v in PUBLISHED_FLOORS.values())
    assert current <= lowest_ever, (
        f"the shim now requires Python {current[0]}.{current[1]}, but "
        f"nable {min(PUBLISHED_FLOORS, key=lambda k: _tuple(PUBLISHED_FLOORS[k]))} "
        f"installs on {lowest_ever[0]}.{lowest_ever[1]}. On an interpreter "
        f"between them, the OLD shim is the only installable version and it wins. "
        f"Lower the floor and reject old interpreters at runtime instead."
    )


def test_the_engine_dependency_is_behind_a_python_version_marker():
    """Without the marker the shim cannot install on an old interpreter at all,
    because finops-mcp itself requires 3.11 — and we are straight back to the
    old version being the only candidate."""
    text = PYPROJECT.read_text()
    m = re.search(r'dependencies\s*=\s*\[([^\]]*)\]', text, re.S)
    assert m, "dependencies not found"
    deps = m.group(1)
    assert "finops-mcp" in deps
    assert "python_version" in deps, (
        "the finops-mcp dependency must carry a python_version marker, or the "
        "shim is uninstallable on exactly the interpreters it exists to catch"
    )


def test_the_console_script_points_at_the_version_check():
    """If it points straight at finops.entry, an old interpreter gets an
    ImportError traceback instead of the message."""
    assert _field("nable") == "nable_shim:main", "the entry point must run the guard first"


def test_the_shim_module_ships_in_the_wheel():
    text = PYPROJECT.read_text()
    assert "nable_shim.py" in text, "the module must be included in the wheel"
    assert "bypass-selection" not in text, (
        "bypass-selection ships no modules, so the entry point would not resolve"
    )
    assert (SHIM / "nable_shim.py").exists()


# ── behaviour ────────────────────────────────────────────────────────────────

def _run_shim(fake_version: tuple[int, int]) -> subprocess.CompletedProcess:
    """Drive main() with a patched sys.version_info, in a subprocess so the
    patch cannot leak and so we see exactly what a user's terminal would."""
    code = (
        "import sys, collections\n"
        "V = collections.namedtuple('v','major minor micro releaselevel serial')\n"
        f"sys.version_info = V({fake_version[0]}, {fake_version[1]}, 0, 'final', 0)\n"
        f"sys.path.insert(0, {str(SHIM)!r})\n"
        "import nable_shim\n"
        "raise SystemExit(nable_shim.main())\n"
    )
    return subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)


@pytest.mark.parametrize("version", [(3, 8), (3, 9), (3, 10)])
def test_an_old_interpreter_gets_one_actionable_line_not_a_traceback(version):
    r = _run_shim(version)
    assert r.returncode == 1
    out = r.stderr + r.stdout
    assert "Traceback" not in out, "a stack trace is not an answer"
    assert "needs Python 3.11" in out
    assert "uvx --python 3.12 nable" in out, "the message must carry the exact command"
    assert f"This is Python {version[0]}.{version[1]}" in out


def test_the_message_names_the_running_version_so_it_is_not_generic():
    assert "3.10" in _run_shim((3, 10)).stderr
    assert "3.9" in _run_shim((3, 9)).stderr


def test_a_modern_interpreter_is_not_blocked_by_the_guard():
    """The guard must be invisible on a supported interpreter. It delegates, so
    it fails here on the delegation, never on the version check."""
    sys.path.insert(0, str(SHIM))
    try:
        import nable_shim
        assert nable_shim.MINIMUM == (3, 11)
        assert sys.version_info >= nable_shim.MINIMUM
        # The real check: on this interpreter, _too_old_message is never reached.
        r = _run_shim((3, 12))
        assert "needs Python 3.11" not in (r.stderr + r.stdout)
    finally:
        sys.path.remove(str(SHIM))


def test_a_missing_engine_is_explained_rather_than_traced(monkeypatch):
    """nable installed but finops-mcp absent is the exact state a 3.10 install
    leaves behind if somebody then upgrades their Python."""
    sys.path.insert(0, str(SHIM))
    try:
        import nable_shim

        real_import = __import__

        def no_finops(name, *a, **k):
            if name.startswith("finops"):
                raise ImportError("No module named 'finops'")
            return real_import(name, *a, **k)

        monkeypatch.setattr("builtins.__import__", no_finops)
        import io
        import contextlib

        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            rc = nable_shim.main()
        assert rc == 1
        assert "engine (finops-mcp) is not" in err.getvalue()
        assert "--from finops-mcp" in err.getvalue()
    finally:
        sys.path.remove(str(SHIM))
