"""The one connect path that is identical on every cloud.

Every major cloud ships a browser terminal that is already authenticated as the
signed-in console user: AWS CloudShell, Azure Cloud Shell, Google Cloud Shell.
Our ambient credential detection fires there with nothing to configure, so a
user with no local credentials, or a laptop behind a corporate proxy that kills
every AWS call, can still see their real bill in about a minute. Two cohorts
nothing we ship to a laptop can reach:

  - no local credentials at all (the connect menu's dominant drop-off)
  - a working laptop on a network that will not let it talk to the provider
    (13 of ~40 machines that attempted a scan in the 30 days to 2026-08-01 died
    instantly on proxy/TLS-interception errors)

WHY NOT `pip install`. The instructions this module replaces said
`pip install finops-mcp && finops welcome`, which is broken twice over:

  1. AWS CloudShell runs Amazon Linux with Python 3.9. finops-mcp requires
     >=3.11, so pip refuses to install it and the user's first experience of
     nable is a resolver error. uv is used instead precisely because it fetches
     its own interpreter, which makes the command independent of whatever the
     shell happens to ship, on every cloud, forever.
  2. `welcome` writes MCP client configs. In a browser shell there is no Claude
     Desktop and no Cursor to configure, so it asked questions that cannot mean
     anything there. `scan` is the right verb: read-only, no paid API call, and
     it prints the recoverable dollars, which is the entire reason to be here.
"""
from __future__ import annotations

from dataclasses import dataclass

# Pinned so the command is independent of the shell's own interpreter. uv
# downloads this itself; nothing preinstalled has to match.
PYTHON = "3.12"

# `--from finops-mcp` names the real package rather than going through the
# `nable` shim on PyPI: an un-yanked shim 0.1.0 still resolves to a months-old
# build on some interpreters, and a first-run user must never land on that.
_RUN = f"uvx --python {PYTHON} --from finops-mcp nable scan"

# uv is not preinstalled on any of the three shells. The installer puts it in
# ~/.local/bin, which is not yet on PATH in the current shell, so the command
# calls it by absolute path rather than asking the user to re-source anything.
COMMAND = f"curl -LsSf https://astral.sh/uv/install.sh | sh && ~/.local/bin/{_RUN}"


@dataclass(frozen=True)
class Shell:
    provider: str
    name: str
    open_hint: str      # how to find it in that provider's console
    url: str            # direct link, so the terminal can make it clickable


SHELLS: dict[str, Shell] = {
    "aws": Shell(
        provider="aws",
        name="AWS CloudShell",
        open_hint="the >_ icon in the top bar of the AWS console",
        url="https://console.aws.amazon.com/cloudshell/home",
    ),
    "azure": Shell(
        provider="azure",
        name="Azure Cloud Shell",
        open_hint="the >_ icon in the top bar of the Azure portal (choose Bash)",
        url="https://portal.azure.com/#cloudshell/",
    ),
    "gcp": Shell(
        provider="gcp",
        name="Google Cloud Shell",
        open_hint="the >_ icon in the top bar of the Google Cloud console",
        url="https://shell.cloud.google.com/",
    ),
}


def command(provider: str = "aws") -> str:
    """The line to paste. Identical on every cloud: the shell's own ambient
    credentials are what differ, and detection already handles all three."""
    return COMMAND


def shell_for(provider: str) -> Shell | None:
    return SHELLS.get((provider or "").strip().lower())


def instructions(provider: str = "aws") -> list[str]:
    """Two steps, no prose. Returned as lines so each caller owns its own
    styling (the wizard colors them; docs and README do not)."""
    sh = shell_for(provider)
    if sh is None:
        return []
    return [
        f"1. Open {sh.name}: {sh.open_hint}",
        f"2. Paste:  {COMMAND}",
    ]
