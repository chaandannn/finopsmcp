"""Telling someone why their attribution is empty, without guessing.

In AWS a tag has to be activated as a cost allocation tag in Billing before it
appears on any cost record. Until then the tag is on the resource, visible in the
console, and absent from Cost Explorer and the CUR entirely. Nothing errors: the
report just says every dollar is unattributed, and a user who tagged their whole
estate correctly concludes the product is broken.

The diagnostic that fixes that has to obey two rules, and the second is the one
worth testing hardest:

  it must never claim a tag is inactive when it simply could not ask
  it must never cost the user money to produce
"""
from __future__ import annotations

import pytest

from finops import billing_access as ba
from finops.attribution import activation


class _FakeCE:
    def __init__(self, entries, pages=1):
        self.entries, self.pages, self.calls = entries, pages, 0

    def list_cost_allocation_tags(self, **kw):
        self.calls += 1
        if self.pages > 1 and "NextToken" not in kw:
            half = len(self.entries) // 2
            return {"CostAllocationTags": self.entries[:half], "NextToken": "more"}
        start = len(self.entries) // 2 if self.pages > 1 else 0
        return {"CostAllocationTags": self.entries[start:]}


def _tags(*pairs):
    return [{"TagKey": k, "Status": s, "Type": "UserDefined"} for k, s in pairs]


@pytest.fixture
def fake_ce(monkeypatch):
    def _install(entries, pages=1):
        fake = _FakeCE(entries, pages)
        monkeypatch.setattr(ba, "ce_client", lambda **kw: fake)
        return fake
    return _install


# ── the answer ───────────────────────────────────────────────────────────────

def test_an_active_tag_is_reported_active(fake_ce):
    fake_ce(_tags(("team", "Active")))
    got = activation.check_aws_tag_activation(["team"])
    assert got["available"] and got["active"] == ["team"] and got["inactive"] == []


def test_a_registered_but_inactive_tag_is_the_thing_we_are_looking_for(fake_ce):
    fake_ce(_tags(("team", "Inactive")))
    got = activation.check_aws_tag_activation(["team"])
    assert got["inactive"] == ["team"]


def test_a_tag_aws_has_never_seen_counts_as_inactive(fake_ce):
    """Absent from the list means no cost record will ever carry it, which is
    the same outcome for the user as registered-but-off."""
    fake_ce(_tags(("costcenter", "Active")))
    assert activation.check_aws_tag_activation(["team"])["inactive"] == ["team"]


@pytest.mark.parametrize("aws_key,asked", [
    ("Team", "team"),      # AWS keeps the user's casing; rules lowercase theirs
    ("team", "Team"),      # a caller passing a raw key straight from a config file
    ("TEAM", "Team"),
])
def test_matching_ignores_case_in_both_directions(fake_ce, aws_key, asked):
    """Casing differs on both sides and neither side is authoritative.

    AWS returns the key as the user typed it on the resource. Rules lowercase
    theirs when compiled, but check_aws_tag_activation is a public function and a
    caller can hand it anything. Comparing raw either way reports a correctly
    activated tag as missing, which sends someone to reconfigure billing they
    already configured.
    """
    fake_ce(_tags((aws_key, "Active")))
    assert activation.check_aws_tag_activation([asked])["active"] == [asked]


def test_every_page_is_read(fake_ce):
    """An account past one page would otherwise have its later tags reported
    inactive purely for sorting late."""
    fake = fake_ce(_tags(("a", "Active"), ("b", "Active"), ("team", "Active"), ("z", "Active")), pages=2)
    got = activation.check_aws_tag_activation(["team"])
    assert fake.calls == 2 and got["active"] == ["team"]


# ── never guess ──────────────────────────────────────────────────────────────

def test_a_failed_check_is_not_a_finding(monkeypatch):
    """The property that matters most.

    If the call fails for any reason, a missing permission, an expired token,
    the answer is "I do not know", never "your tags are inactive". A user acting
    on a wrong diagnosis reconfigures billing for nothing.
    """
    def boom(**kw):
        raise RuntimeError("AccessDenied")
    monkeypatch.setattr(ba, "ce_client", boom)

    got = activation.check_aws_tag_activation(["team"])
    assert got["available"] is False
    assert got["inactive"] == [] and got["active"] == []
    assert activation.explain_empty_attribution(["team"]) is None, "guessed with no data"


def test_nothing_to_check_asks_nothing(monkeypatch):
    called = []
    monkeypatch.setattr(ba, "ce_client", lambda **kw: called.append(1))
    assert activation.check_aws_tag_activation([])["available"] is False
    assert not called, "asked AWS about an empty tag list"


def test_all_active_produces_no_message(fake_ce):
    """Silence when there is nothing wrong. A diagnostic that always fires is
    noise the user learns to skip past."""
    fake_ce(_tags(("team", "Active")))
    assert activation.explain_empty_attribution(["team"]) is None


def test_the_message_names_the_fix(fake_ce):
    fake_ce(_tags(("team", "Inactive")))
    msg = activation.explain_empty_attribution(["team"])
    assert msg and "cost allocation tag" in msg.lower()
    assert "going forward" in msg.lower(), (
        "activation is not retroactive; a user who does not know that will "
        "wait for history that is never coming")


# ── never bill for a diagnostic ──────────────────────────────────────────────

def test_a_scheduled_run_never_pays_for_this(monkeypatch):
    """Cost Explorer bills per request. A diagnostic on a timer is precisely the
    recurring charge nobody consented to, so it goes through the same chokepoint
    as every other CE call rather than restating the rule."""
    with ba.unattended_context():
        assert activation.check_aws_tag_activation(["team"])["available"] is False


def test_an_operator_ban_is_respected(monkeypatch):
    monkeypatch.setenv("NABLE_NO_COST_EXPLORER", "1")
    assert activation.check_aws_tag_activation(["team"])["available"] is False
