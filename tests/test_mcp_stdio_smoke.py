"""Launch the real `finops-mcp` process over stdio, the way a client does.

Every other MCP test in this repo uses the in-memory shortcut
(`create_connected_server_and_client_session`), which imports the server object
directly. That skips the console script, the packaging metadata, and process
startup, so it cannot catch an entry point that no longer resolves, an import
that only breaks under a different __main__, or a dependency that is imported at
module scope but missing from the wheel. Those are precisely the failures a user
sees as "the MCP server won't start" with nothing in the logs.

This spawns the installed script and speaks real JSON-RPC to it.

It is the same class of gap as the CLI failures we just fixed: the unit was
covered, the thing users actually run was not. Kept deliberately small (start,
list, call one demo-safe tool) so it stays fast enough to run every time.
"""
from __future__ import annotations

import asyncio
import os
import subprocess
import sys

import pytest

# Resolve the script from THIS interpreter's environment, never PATH. shutil.which
# finds whichever finops-mcp happens to be first on PATH, and this machine has
# three; the first version of this test silently drove a stale 0.8.158 install and
# passed happily while the working tree was broken.
_SCRIPT = os.path.join(os.path.dirname(sys.executable), "finops-mcp")


def _env() -> dict:
    """Demo mode so the server needs no cloud credentials, telemetry off, and no
    scheduler racing the test."""
    env = {k: v for k, v in os.environ.items() if not k.startswith("AWS_")}
    env.update({
        "NABLE_NO_TELEMETRY": "1",
        "FINOPS_DEMO": "1",
        "FINOPS_DEMO_FORCE": "1",
        "FINOPS_DEMO_MODE": "1",
        "FINOPS_ENABLE_SCHEDULER": "0",
        "AWS_EC2_METADATA_DISABLED": "true",
    })
    return env


@pytest.mark.skipif(not os.path.exists(_SCRIPT), reason="finops-mcp script not installed")
def test_the_script_under_test_is_the_working_tree():
    """Guard the guard. If this test ever drives a different installation than the
    code under test, everything below is theatre: it will pass while the working
    tree is broken. That is exactly what happened when this file resolved the
    script through shutil.which and got a stale 0.8.158 from another venv."""
    import finops

    out = subprocess.run([_SCRIPT, "--version"], capture_output=True, text=True, timeout=120)
    reported = (out.stdout + out.stderr).strip()
    assert finops.__version__ in reported, (
        f"{_SCRIPT} reports {reported!r}, but the imported package is "
        f"{finops.__version__}. The test would be driving a different install."
    )


async def _session():
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    params = StdioServerParameters(command=_SCRIPT, args=[], env=_env())
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await asyncio.wait_for(session.initialize(), timeout=120)
            yield session


@pytest.mark.skipif(not os.path.exists(_SCRIPT), reason="finops-mcp script not installed")
def test_real_server_process_starts_and_lists_tools():
    """If the entry point or a module-scope import is broken, this is where it
    shows up. The in-memory tests would still pass."""
    async def run():
        async for session in _session():
            tools = await asyncio.wait_for(session.list_tools(), timeout=60)
            names = {t.name for t in tools.tools}
            assert names, "server advertised no tools"
            # Core cost tools are always advertised, connected or not.
            assert "get_cost_summary" in names
            assert "what_can_nable_do" in names
            return names

    names = asyncio.run(run())
    assert len(names) > 20, f"suspiciously small tool surface: {len(names)}"


@pytest.mark.skipif(not os.path.exists(_SCRIPT), reason="finops-mcp script not installed")
def test_real_server_process_answers_a_tool_call():
    """A tool call over real JSON-RPC, not a direct function call. Catches a
    server that starts but cannot actually serve."""
    async def run():
        async for session in _session():
            res = await asyncio.wait_for(
                session.call_tool("get_cost_summary", {"days": 30}), timeout=120
            )
            return res

    res = asyncio.run(run())
    assert not res.isError, f"tool call errored: {res.content}"
    text = "".join(getattr(c, "text", "") for c in res.content)
    assert text.strip(), "tool returned an empty payload"
    # Demo mode is the only way this runs without credentials; if the flag stops
    # reaching the subprocess the call would fail on missing creds instead.
    assert "total_usd" in text or "summary" in text
