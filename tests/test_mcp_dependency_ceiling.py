"""The `mcp` dependency needs a ceiling, and this test is why.

On 2026-07-28 the SDK released 2.0.0, which removed `mcp.server.fastmcp`. Our
requirement was `mcp[cli]>=1.28.1` with no upper bound, so every fresh install
from that moment resolved 2.0.0 and `import finops.server` died with
ModuleNotFoundError.

What made it dangerous is where it did NOT show up. `entry.py` is a light
dispatcher that never imports the server, so `nable ai-budget`, `nable scan`
and `nable guard` all kept working perfectly. The only broken surface was the
MCP server itself: the thing Claude Desktop and Cursor actually launch. A
smoke test of the CLI would have said everything was fine.

The suite here also could not catch it, because CI and dev machines install
from a resolved environment that already had 1.x. It surfaced only when CI
rebuilt its environment from scratch.

So this test asserts the ceiling exists in the declared metadata, which is the
one place that governs what a stranger's fresh install resolves to.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

PYPROJECT = Path(__file__).resolve().parent.parent / "pyproject.toml"


def _mcp_requirement() -> str:
    text = PYPROJECT.read_text()
    for line in text.splitlines():
        s = line.strip().strip(",").strip('"')
        if s.startswith("mcp[") or s.startswith("mcp>") or s.startswith("mcp="):
            return s
    pytest.fail("no mcp requirement found in pyproject.toml")


def test_mcp_has_an_upper_bound():
    """Without this, the next major SDK release breaks every new install of the
    MCP server while the CLI keeps working and hides it."""
    req = _mcp_requirement()
    assert "<" in req, (
        f"mcp requirement {req!r} has no upper bound. mcp 2.0.0 removed "
        "mcp.server.fastmcp; an uncapped resolve breaks finops.server on every "
        "fresh install. Lift the cap only together with a port to the 2.x API."
    )


def test_the_ceiling_actually_excludes_2_x():
    req = _mcp_requirement()
    m = re.search(r"<\s*=?\s*([0-9][0-9.]*)", req)
    assert m, f"could not parse an upper bound out of {req!r}"
    major = int(m.group(1).split(".")[0])
    assert major <= 2, f"{req!r} still admits mcp 2.x, which has no mcp.server.fastmcp"


def test_the_floor_keeps_the_cve_fix():
    """1.28.1 fixes CVE-2026-59950 (WS transport Host/Origin bypass). Capping the
    top must never come at the cost of dropping the floor."""
    req = _mcp_requirement()
    m = re.search(r">=\s*([0-9][0-9.]*)", req)
    assert m, f"no lower bound in {req!r}"
    parts = [int(p) for p in m.group(1).split(".")]
    assert parts >= [1, 28, 1], f"{req!r} allows an mcp older than the CVE fix"


def test_the_import_the_server_depends_on_is_present():
    """The actual symbol. If a future SDK moves it again, this fails here rather
    than in a stranger's editor."""
    from mcp.server.fastmcp import FastMCP  # noqa: F401
