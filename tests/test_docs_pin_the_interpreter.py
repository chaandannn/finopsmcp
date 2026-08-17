# SPDX-License-Identifier: Apache-2.0
"""The first command in the README has to work on the machine that reads it.

Measured 2026-08-17 on a stock Mac: `uvx` resolved to a Python 3.10 framework
install, so the README's opening line

    uvx nable scan

exited 1 with "nable needs Python 3.11 or newer". The shim handles it politely
and prints the exact command to run instead, which is good engineering and still
a failed first command. Pinned, the same machine goes from nothing to a working
binary in 5.1 seconds.

The docs were already inconsistent about this, which is how it survived: the
editor-config snippet pins `--python 3.12`, the welcome flow's copy-paste pins
it, and the commands a reader tries first did not. The README even explains at
the bottom that uvx is recommended *because* corporate machines have managed
Python installs, which is precisely the case that breaks unpinned.

So this is not a style rule. An unpinned `uvx nable` in a document is a first-run
failure for every reader whose default interpreter is older than 3.11, and they
are exactly the readers the paragraph is aimed at.

CHANGELOG.md is excluded: it is a record of what past releases said, and
rewriting history to match today's advice would make it a worse record.
"""
from __future__ import annotations

import pathlib
import re

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]

# Every doc that hands a reader a command to paste.
DOC_GLOBS = ("README.md", "CAPABILITIES.md", "packaging/*/README.md",
             "plugins/*/README.md", "shim/README.md", "docs/*.md")

# A command invocation, not prose that happens to mention the tool. Matches
# `uvx nable`, `uvx finops-mcp`, and misses `uvx --python 3.12 nable`.
UNPINNED = re.compile(r"\buvx\s+(?!--)(nable|finops-mcp)\b")


def _docs() -> list[pathlib.Path]:
    out: list[pathlib.Path] = []
    for pat in DOC_GLOBS:
        out.extend(sorted(ROOT.glob(pat)))
    return [p for p in out if p.name != "CHANGELOG.md"]


def test_every_pasteable_uvx_command_pins_the_interpreter():
    offenders: list[str] = []
    for path in _docs():
        for i, line in enumerate(path.read_text().splitlines(), 1):
            if UNPINNED.search(line):
                offenders.append(f"{path.relative_to(ROOT)}:{i}: {line.strip()[:90]}")

    assert not offenders, (
        "these commands exit 1 for any reader whose default interpreter is older "
        "than 3.11, which is the machine the uvx advice exists for:\n  "
        + "\n  ".join(offenders)
        + "\nWrite `uvx --python 3.12 nable ...` instead."
    )


def test_the_readmes_first_command_is_one_that_runs():
    """The opening command is the one that decides whether anyone continues."""
    readme = (ROOT / "README.md").read_text().splitlines()
    first = next((l.strip() for l in readme
                  if l.strip().startswith("uvx ") or l.strip().startswith("$ uvx ")), None)

    assert first, "the README no longer opens with a uvx command; update this test with it"
    assert "--python" in first, (
        f"the first command a reader pastes is {first!r}, which picks up whatever "
        f"interpreter uvx defaults to"
    )


@pytest.mark.parametrize("pinned,expected", [
    ("uvx --python 3.12 nable scan", False),
    ("uvx --python 3.12 finops-mcp", False),
    ("uvx nable scan", True),
    ("uvx finops-mcp", True),
    ("run uvx nable to start the server", True),
    # Prose that names the tool without invoking it stays out of scope.
    ("Why uvx? Claude Desktop does not inherit your PATH.", False),
    ("Switch to uvx config or use an absolute path", False),
])
def test_the_matcher_catches_invocations_and_leaves_prose_alone(pinned, expected):
    """A rule that also rewrites prose gets switched off the first time it lies."""
    assert bool(UNPINNED.search(pinned)) is expected
