"""activate_pro: directs you to the terminal, and refuses to take the key here.

This file used to assert the opposite. It pinned `activate_pro(license_key=...)`,
paste-the-key-in-the-editor, on the grounds that activating in a separate process
left the running server unable to see the new licence without a restart. That
convenience was real and the security cost was not weighed: an MCP tool argument
is serialised into the conversation and shipped to the model provider before
nable ever sees it, and it stays in that provider's history.

license.validate_key checks the signature, the plan and the expiry. There is no
machine binding, so a key read out of a transcript is a working, transferable
entitlement until it expires. The tool then returned "The key was verified
offline and stored locally; nothing left your machine" about the very transport
that had just carried it.

connect_azure was rewritten for exactly this reason and says so in its docstring.
The rule was already the project's; activate_pro predated its enforcement.

So the parameter is gone. The tests below now pin the refusal, and the one that
matters most is the last: the storage layer must keep working, or the fix would
have left a paying customer with no way to activate at all.
"""
from __future__ import annotations

import asyncio

import finops.server as server


def _run(coro):
    return asyncio.run(coro)


def test_activate_pro_takes_no_arguments():
    """The whole fix, expressed as the tool's signature.

    Checked against the live registry rather than the Python signature, because
    what reaches the model is the JSON schema, and that is what carries a secret.
    """
    registry = getattr(getattr(server.mcp, "_tool_manager", None), "_tools", {}) or {}
    tool = registry.get("activate_pro")
    assert tool is not None, "activate_pro is no longer registered at all"
    props = tool.parameters.get("properties") or {}
    assert props == {}, (
        f"activate_pro advertises {list(props)} to the model. Any argument here "
        f"is a place a licence key can be pasted, and the model provider keeps "
        f"whatever is pasted."
    )


def test_it_names_a_command_that_exists():
    """A tool that redirects you is only as good as the command it names.

    An earlier draft of this fix told the user to run `finops activate`, which
    has never been a command. The redirect has to land somewhere real or it is
    just a dead end with better intentions.
    """
    import re
    from pathlib import Path

    import finops

    out = _run(server.activate_pro())
    named = re.findall(r"`finops ([a-z-]+)`", out.get("message", ""))
    named += re.findall(r"^finops ([a-z-]+)$", out.get("activate_with", ""))
    assert named, f"the response names no command to run: {out}"

    wizard = (Path(finops.__file__).parent / "setup_wizard.py").read_text()
    real = set(re.findall(r'add_parser\("([a-z-]+)"', wizard))
    missing = sorted(set(named) - real)
    assert not missing, (
        f"activate_pro tells the user to run {missing}, which the CLI does not "
        f"define. Real subcommands: {sorted(real)[:12]}..."
    )


def test_the_free_user_is_told_where_to_go_and_why():
    out = _run(server.activate_pro())
    assert out["activated"] is False
    assert "finops login" in out["activate_with"] + out["message"]
    # The reason has to travel with the refusal, or it reads as nable being
    # awkward rather than as nable protecting the key.
    assert "why_not_here" in out
    assert "model provider" in out["why_not_here"]
    assert "get_pro" in out


def test_it_does_not_claim_the_key_never_left_the_machine():
    """The old response said exactly that, on the path that had just sent it.

    Guarding the sentence, not just the parameter: a future edit that re-adds a
    reassuring note is the same defect in a different shape.
    """
    out = _run(server.activate_pro())
    blob = " ".join(str(v) for v in out.values()).lower()
    for claim in ("nothing left your machine", "nothing about it is sent anywhere"):
        assert claim not in blob, f"the response still claims: {claim!r}"


def test_an_already_licensed_machine_reports_its_plan(monkeypatch):
    """The tool still answers the question a Pro user would ask it."""
    from finops.license import LicenseStatus

    monkeypatch.setattr(
        "finops.license.get_status",
        lambda: LicenseStatus(mode="pro", email="dev@acme.com",
                              issued="2026-07-08", message=""),
    )
    out = _run(server.activate_pro())
    assert out["activated"] is True
    assert out["plan"] == "pro"
    assert out["email"] == "dev@acme.com"


def test_the_terminal_path_still_works():
    """The point of the fix is to move the key, not to strip the feature.

    If this goes red, activation is impossible by any route and a paying
    customer is stuck.
    """
    import re
    from pathlib import Path

    import finops
    from finops import license as license_mod

    assert callable(getattr(license_mod, "store_license", None))
    assert callable(getattr(license_mod, "validate_key", None))
    # rejected offline, with no network
    assert license_mod.validate_key("FINOPS-2-not-a-real-key").mode == "invalid"

    wizard = (Path(finops.__file__).parent / "setup_wizard.py").read_text()
    real = set(re.findall(r'add_parser\("([a-z-]+)"', wizard))
    assert {"login", "license"} <= real, (
        "both terminal activation routes must exist: `finops login` (email code, "
        "no key to handle) and `finops license` (paste the key locally)"
    )
