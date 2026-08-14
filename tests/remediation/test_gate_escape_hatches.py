# SPDX-License-Identifier: Apache-2.0
"""Every way out that the refusal advertises has to actually work.

The remediation kill switch refuses with a message that names two ways to
proceed: `dry_run=True` to see the diff, `patch_only=True` to write the .tf files
locally and use your own git flow. Both are printed to a user who has just been
told no.

While hardening the switch I gated patch_only as well, on the reasoning that it
still edits the working tree. The reasoning is arguable. The result was not: the
refusal message went on recommending patch_only, so a user who followed it
word for word got the identical refusal a second time, with no remaining way
forward and no hint that the advice was stale. Three tests caught it, and only
because they happened to exercise patching.

That is a general shape, not a one-off. A gate and its refusal text live in
different files and drift apart silently, because nothing executes English. This
file executes it: it reads the actual refusal string, extracts every `foo=True`
it recommends, and calls the gated function with each one. A new hatch in the
message with no code behind it fails here. A hatch quietly closed in code while
the message still names it fails here too.

Scope note, since a reader will ask why patch_only is exempt at all: gate.py
defines the switch as "may nable push a branch and open a pull request in our
repositories at all". patch_only returns before any git, subprocess or HTTP call,
into a directory the caller passed explicitly. Widening the switch to cover local
writes is a real option, but it is a decision for gate.py to state, not for a
call site to infer.
"""
from __future__ import annotations

import inspect
import re
from unittest.mock import MagicMock, patch

import pytest

from finops.remediation.gate import disabled_response
from finops.remediation.rightsizing_pr import open_rightsizing_pr


def _empty_db():
    """Stub the recommendation store as empty.

    An empty result set makes the function return "nothing to patch" before it
    shells out to `terraform show -json`, which is not installed on CI and is
    not what these tests are about. The gate is consulted before the query
    either way, so a refusal still surfaces if the switch is wrong.
    """
    conn = MagicMock()
    conn.__enter__ = lambda s: conn
    conn.__exit__ = MagicMock(return_value=False)
    conn.execute.return_value.fetchall.return_value = []
    engine = MagicMock()
    engine.connect.return_value = conn
    return patch("finops.remediation.rightsizing_pr.get_engine", return_value=engine)


def _advertised_hatches() -> set[str]:
    """The `name=True` escapes the refusal message tells the user to reach for."""
    msg = disabled_response()["message"]
    return set(re.findall(r"\b([a-z_]+)=True\b", msg))


def test_the_refusal_names_at_least_one_way_forward():
    """A dead end is its own defect.

    If a future edit strips the hints, this fails rather than letting the switch
    quietly become a wall. Users who hit a gate with no exit file a bug or leave.
    """
    hatches = _advertised_hatches()
    assert hatches, (
        "the disabled message no longer offers any way to proceed: "
        f"{disabled_response()['message']!r}"
    )


def test_every_advertised_hatch_is_a_real_parameter():
    """Catches a message that recommends a flag the function does not accept."""
    params = inspect.signature(open_rightsizing_pr).parameters
    for hatch in _advertised_hatches():
        assert hatch in params, (
            f"the refusal tells the user to pass {hatch}=True, but "
            f"open_rightsizing_pr has no such parameter. They will get a TypeError "
            f"on top of the refusal"
        )


@pytest.fixture
def tf_dir(tmp_path):
    """A minimal Terraform directory, so the call is real rather than a TypeError.

    The first version of this file called open_rightsizing_pr(**{hatch: True})
    with no tf_dir. That raises TypeError on the missing positional argument,
    which the test's own `except Exception` then swallowed, so all of it passed
    against the very bug it was written for. A test that cannot fail is worse
    than no test, because it also reports that the area is covered.
    """
    (tmp_path / "main.tf").write_text(
        'resource "aws_instance" "api" {\n  instance_type = "m5.xlarge"\n}\n')
    return str(tmp_path)


@pytest.mark.parametrize("hatch", sorted(_advertised_hatches()))
def test_every_advertised_hatch_survives_the_gate(hatch, monkeypatch, tf_dir):
    """The one that failed. Each hatch, called for real with the switch OFF.

    Not asserting the call succeeds at its real job: with no recommendations in
    the database it has nothing to patch, and that is a fine outcome. The single
    thing asserted is that it does not come back with the SAME refusal that
    recommended it, which is the failure that leaves a user with no way forward.
    """
    monkeypatch.setenv("FINOPS_REMEDIATION_ENABLED", "false")

    with _empty_db():
        result = open_rightsizing_pr(tf_dir=tf_dir, **{hatch: True})

    assert isinstance(result, dict), f"expected a result payload, got {type(result)}"
    assert result.get("error") != "remediation_disabled", (
        f"the refusal recommends {hatch}=True and then refuses it. A user "
        f"following the message exactly is now in a loop with no way out"
    )


def test_the_switch_still_closes_the_path_it_exists_for(monkeypatch, tf_dir):
    """The other direction: proving the hatches work is worthless if the door is open.

    Without this, deleting the gate entirely leaves every test above green.
    """
    monkeypatch.setenv("FINOPS_REMEDIATION_ENABLED", "false")
    with _empty_db():
        result = open_rightsizing_pr(tf_dir=tf_dir, github_repo="acme/infra")
    assert isinstance(result, dict) and result.get("error") == "remediation_disabled", (
        "with the switch off, the default path (no dry_run, no patch_only) must "
        f"refuse before touching git. Got: {result}"
    )
    assert result.get("pr_url") is None
