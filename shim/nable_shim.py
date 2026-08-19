"""The `nable` console script.

This module exists for one reason: to own the experience on an interpreter that
is too old to run nable, instead of leaving that to a five-week-old package.

THE TRAP THIS CLOSES. `nable` 0.1.0 declared requires-python >= 3.10. 0.1.1 and
0.1.2 correctly raised the floor to 3.11, which meant that on a 3.10 interpreter
they were both uninstallable and the ONLY remaining candidate was 0.1.0. So
`uvx nable` on any machine whose default python is 3.10 — Ubuntu 22.04's system
python, most older pyenv setups, plenty of CI images — silently resolved to
0.1.0, which dragged in finops-mcp 0.8.87: a build with no `scan` command, no
entry dispatcher, and an mcp pin so old it raised ModuleNotFoundError on import.

The user saw a Python traceback. We saw nothing at all, because the crash
happened before any line of our code ran, telemetry included. Raising the floor
did not fix the trap, it created it: the newer, correct versions excluded
themselves and left the broken one as the only option.

So this version goes the other way. requires-python is deliberately LOW, the
finops-mcp dependency is behind an environment marker, and the version check
lives here at runtime. On an old interpreter the package still installs, wins
the resolution on version number, and prints one actionable line. On a modern
one it delegates and is invisible.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys

MINIMUM = (3, 11)
_RECOMMENDED = "3.12"

# Set on the child so a re-exec can never loop. If the managed interpreter is
# somehow still too old, the child prints the message and stops instead of
# spawning another one forever.
_REEXEC = "NABLE_SHIM_REEXEC"


def _too_old_message() -> str:
    running = f"{sys.version_info.major}.{sys.version_info.minor}"
    return (
        f"\n  nable needs Python {MINIMUM[0]}.{MINIMUM[1]} or newer. "
        f"This is Python {running}.\n"
        f"\n  Run this instead, which fetches its own interpreter:\n"
        f"\n      uvx --python {_RECOMMENDED} nable\n"
        f"\n  Nothing else to install, and it does not change the Python on\n"
        f"  your machine. If you installed with pip, use pipx or uv instead:\n"
        f"\n      uv tool install --python {_RECOMMENDED} nable\n"
    )


def _reexec_argv(uv_path: str, argv: list[str]) -> list[str]:
    """The command that runs nable under a managed interpreter.

    uvx and `uv tool run` are the same thing under two names, and a machine may
    have either on PATH.
    """
    if os.path.basename(uv_path).startswith("uvx"):
        return [uv_path, "--python", _RECOMMENDED, "nable", *argv]
    return [uv_path, "tool", "run", "--python", _RECOMMENDED, "nable", *argv]


def _reexec_under_managed_python(argv: list[str]) -> int | None:
    """Re-run this command under a managed interpreter, or return None.

    Printing the right command and asking somebody to retype it is a handled
    failure, and a handled failure is still a failure: they have to read it,
    understand why their own python is the problem, and type again, at the exact
    moment they were deciding whether this tool is worth the trouble. uv can
    fetch an interpreter in a couple of seconds, so the honest thing is to do it
    rather than to explain it.

    Returns the child's exit code, or None when we could not do it and the
    caller should fall back to the message. Never raises: a re-exec that fails
    has to degrade into the old behaviour, not into a traceback, because the
    whole point of this module is that an old interpreter never sees one.
    """
    if os.environ.get(_REEXEC):
        return None                      # already a child; do not loop
    uv = shutil.which("uvx") or shutil.which("uv")
    if not uv:
        return None                      # nothing to re-exec with

    running = f"{sys.version_info.major}.{sys.version_info.minor}"
    # Say what is happening. An unexplained pause while uv downloads an
    # interpreter reads as a hang, and a hang is worse than the message was.
    print(
        f"  This is Python {running}, and nable needs "
        f"{MINIMUM[0]}.{MINIMUM[1]}. Fetching Python {_RECOMMENDED} and "
        f"continuing.",
        file=sys.stderr,
    )
    env = {**os.environ, _REEXEC: "1"}
    try:
        return subprocess.run(_reexec_argv(uv, argv), env=env).returncode
    except KeyboardInterrupt:
        # Ctrl-C is the most common way anybody stops a download, and it raised
        # straight through to a traceback: the one thing this module exists to
        # keep off an old interpreter's screen. 130 is what a shell expects from
        # SIGINT, and returning it rather than re-raising means the parent exits
        # quietly too.
        print("\n  Stopped.", file=sys.stderr)
        return 130
    except (OSError, subprocess.SubprocessError):
        return None                      # fall back to telling them


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if sys.version_info < MINIMUM:
        rc = _reexec_under_managed_python(argv)
        if rc is not None:
            return rc
        # Could not do it for them. Not a traceback: somebody typed one command
        # and deserves one answer.
        print(_too_old_message(), file=sys.stderr)
        return 1

    try:
        from finops.entry import main as _main
    except ImportError as exc:                    # pragma: no cover - install-time
        print(
            f"\n  nable is installed but its engine (finops-mcp) is not: {exc}\n"
            f"\n  Reinstall with:\n"
            f"\n      uvx --python {_RECOMMENDED} --from finops-mcp nable\n",
            file=sys.stderr,
        )
        return 1
    return _main()


if __name__ == "__main__":                        # pragma: no cover
    raise SystemExit(main())
