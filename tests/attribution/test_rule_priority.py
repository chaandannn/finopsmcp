"""Which rule wins, and what happens when none do.

Attribution feeds showback and chargeback, so a wrong answer here is not a wrong
report, it is a team being billed for another team's spend. The failure is also
invisible: every number still adds to the correct total, it is just split along
the wrong lines, and nobody notices until somebody disputes an invoice months
later.

`priority` is documented as "lower number = higher priority" and was implemented
as the opposite: rules sorted ascending, each match overwriting the last, so the
HIGHEST number won. Both examples in the file nable itself generates for users
were wrong because of it. These are written against that example verbatim, since
it is what a user actually runs.
"""
from __future__ import annotations

import pytest

from finops.attribution import mapper


@pytest.fixture
def rules(tmp_path, monkeypatch):
    """The shipped example, byte for byte from write_example_rules()."""
    path = mapper.write_example_rules(tmp_path / "tag_rules.yaml")
    monkeypatch.setenv("FINOPS_TAG_RULES", str(path))
    mapper.reload_rules()
    yield path
    mapper.reload_rules()


def team(tags: dict) -> str:
    return mapper.tags_to_attribution(tags)["team"]


# ── the inversion ────────────────────────────────────────────────────────────

def test_an_explicit_team_tag_beats_a_cost_centre_code(rules):
    """The bug that cost real money.

    The example file comments costcenter as "lower priority than the team tag"
    and gives it priority 50 against team's 10. It was overwriting team anyway,
    so an org tagging properly got teams named CC-4402.
    """
    assert team({"team": "frontend", "costcenter": "CC-9910"}) == "frontend"


def test_a_value_normalising_rule_beats_the_plain_rule_beneath_it(rules):
    """priority 5 maps team=infra* to "platform"; priority 10 maps team through
    as-is. The specific rule is the entire reason the general one is written
    loosely, so it has to win."""
    assert team({"team": "infra-core"}) == "platform"


def test_lower_numbers_win_which_is_what_the_docs_say(rules):
    """Pinned as a contract, not an implementation detail. Anyone reordering
    these rules is reading the documented meaning of the number."""
    assert team({"team": "backend", "costcenter": "CC-1"}) == "backend"


# ── fallbacks must still fire ────────────────────────────────────────────────

def test_a_fallback_still_applies_when_the_better_tag_is_absent(rules):
    """The failure mode of over-correcting: making the first rule win so hard
    that the fallbacks below it become dead config. costcenter exists precisely
    for resources with no team tag."""
    assert team({"costcenter": "CC-9910"}) == "CC-9910"


def test_untagged_stays_untagged(rules):
    assert team({}) == "unattributed"
    assert team({"owner": "someone"}) == "unattributed"


def test_fields_are_decided_independently(rules):
    """team being settled must not stop service or environment resolving."""
    got = mapper.tags_to_attribution({"team": "frontend", "service": "checkout", "env": "prod"})
    assert got == {"team": "frontend", "service": "checkout", "environment": "prod"}


def test_an_alias_normalises_a_free_form_value(rules):
    assert team({"team": "sre"}) == "platform"


# ── the keys we would ask AWS about ──────────────────────────────────────────

def test_configured_tag_keys_lists_what_the_rules_reference(rules):
    """Drives the cost-allocation-tag check. Asking AWS about every tag in the
    account would be noise; these are the only keys that could yield a team."""
    keys = mapper.configured_tag_keys()
    assert "team" in keys and "costcenter" in keys
    assert len(keys) == len(set(keys)), f"duplicates would ask AWS twice: {keys}"
