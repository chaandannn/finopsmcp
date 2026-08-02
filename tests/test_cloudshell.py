"""The Cloud Shell connect path.

The instructions this replaces were broken in a way that only bit the most
fragile cohort. They said `pip install finops-mcp && finops welcome`:

  - AWS CloudShell runs Amazon Linux with Python 3.9 and finops-mcp requires
    >=3.11, so pip refuses and a no-credentials user's first contact with nable
    is a resolver error.
  - `welcome` configures MCP clients. There is no Claude Desktop in a browser
    shell, so it asked questions that cannot mean anything there.

Nothing in the suite caught either, because nothing asserted on the string. These
tests pin the properties that make the command work on a shell we cannot run in
CI: it must bring its own interpreter, it must not route through the stale shim,
and it must scan rather than run the editor wizard.
"""
from __future__ import annotations

import re

import pytest

from finops import cloudshell


def test_the_command_brings_its_own_interpreter():
    """The load-bearing property. AWS CloudShell ships Python 3.9, below our
    floor, so any command that depends on the shell's preinstalled Python is
    broken there. uv fetches its own, which is why it is used at all."""
    cmd = cloudshell.COMMAND
    assert "astral.sh/uv/install.sh" in cmd, "uv must be installed, not assumed"
    assert f"--python {cloudshell.PYTHON}" in cmd, "the interpreter must be pinned explicitly"
    assert cloudshell.PYTHON >= "3.11", "pinned interpreter must satisfy requires-python"


def test_the_command_never_tells_anyone_to_pip_install():
    # The exact regression: `pip install finops-mcp` cannot resolve on a 3.9
    # shell, and it was what every no-creds user was told to run.
    assert "pip install" not in cloudshell.COMMAND


def test_the_command_runs_scan_not_the_editor_wizard():
    """`welcome` writes MCP client configs. A browser shell has no editor to
    configure, so it asked meaningless questions instead of showing a number."""
    assert cloudshell.COMMAND.rstrip().endswith("nable scan")
    assert "welcome" not in cloudshell.COMMAND


def test_the_command_names_the_real_package_not_the_shim():
    """An un-yanked shim 0.1.0 still resolves to a months-old build on some
    interpreters. A first-run user must never land on that, so the command
    names finops-mcp directly."""
    assert "--from finops-mcp" in cloudshell.COMMAND


def test_uv_is_called_by_absolute_path():
    # The installer drops uv in ~/.local/bin, which is not on PATH in the shell
    # that just ran the installer. Calling it by name fails with "command not
    # found" on a fresh shell, which is every shell this is pasted into.
    assert "~/.local/bin/uvx" in cloudshell.COMMAND


def test_the_command_is_a_single_pasteable_line():
    assert "\n" not in cloudshell.COMMAND.strip()


@pytest.mark.parametrize("provider", ["aws", "azure", "gcp"])
def test_every_major_cloud_has_a_shell_and_identical_instructions(provider):
    """Parity is the point: one line, three clouds. A provider missing here is a
    provider whose users have no zero-install path."""
    sh = cloudshell.shell_for(provider)
    assert sh is not None
    assert sh.url.startswith("https://")
    assert ">_" in sh.open_hint, "the hint must describe how to find the terminal icon"
    steps = cloudshell.instructions(provider)
    assert len(steps) == 2, "two steps, no prose"
    assert cloudshell.COMMAND in steps[1]
    assert sh.name in steps[0]
    # Same command everywhere: the shells differ, the ambient credentials differ,
    # our instruction does not.
    assert cloudshell.command(provider) == cloudshell.COMMAND


def test_an_unknown_provider_degrades_quietly():
    assert cloudshell.shell_for("digitalocean") is None
    assert cloudshell.instructions("digitalocean") == []
    assert cloudshell.shell_for("") is None


def test_provider_lookup_is_case_and_space_insensitive():
    assert cloudshell.shell_for("  AWS ") is cloudshell.SHELLS["aws"]


# ── the call sites ────────────────────────────────────────────────────────────

def test_no_shipped_string_still_teaches_the_broken_pip_command():
    """The regression lived in four places. Scan the package for anyone still
    telling a user to pip install and then run the editor wizard."""
    import pathlib

    root = pathlib.Path(cloudshell.__file__).parent
    offenders = []
    for path in root.rglob("*.py"):
        if path.name == "cloudshell.py":   # documents the mistake on purpose
            continue
        text = path.read_text(encoding="utf-8", errors="ignore")
        if re.search(r"pip install finops-mcp\s*&&\s*finops welcome", text):
            offenders.append(str(path.relative_to(root)))
    assert not offenders, f"still teaching the broken CloudShell command: {offenders}"


def test_the_connect_menu_leads_with_cloud_shell():
    """It must be the FIRST option offered to a user with no credentials: it is
    the only path that works with no local install, no key minted, and on a
    machine whose network blocks the provider outright."""
    import inspect

    from finops import welcome

    src = inspect.getsource(welcome.run_welcome_flow)
    assert "from .cloudshell import COMMAND" in src
    cs = src.index("cloudshell")
    oneclick = src.index("_oneclick_aws_url()", cs)
    menu = src.index('1)')
    assert cs < oneclick < menu, "Cloud Shell must be offered before the key paths"


def test_the_mcp_connect_tool_quotes_the_live_command():
    """connect_aws hands its hint to a model. A hardcoded string there goes
    stale silently and the model confidently teaches a command that fails."""
    import inspect

    from finops import server  # noqa: F401  (tool modules wire up during its import)
    from finops.tools import aws

    fn = aws.connect_aws.fn if hasattr(aws.connect_aws, "fn") else aws.connect_aws
    src = inspect.getsource(fn)
    assert "from ..cloudshell import COMMAND" in src
    assert "_CLOUDSHELL_CMD" in src
    assert "pip install finops-mcp" not in src
