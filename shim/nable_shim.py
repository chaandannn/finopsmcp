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

import sys

MINIMUM = (3, 11)
_RECOMMENDED = "3.12"


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


def main() -> int:
    if sys.version_info < MINIMUM:
        # Not a traceback. Somebody typed one command and deserves one answer.
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
