"""Tests for finops.attribution.mapper.

tags_to_attribution runs once per cost entry (thousands of times on a real org),
so the rules are compiled once and reused. These lock the mapping behavior
(priority, patterns, alias resolution, case-insensitivity) and prove the compile
is memoized and correctly invalidated by reload_rules().
"""
from __future__ import annotations

import textwrap

import pytest

from finops.attribution import mapper


@pytest.fixture
def rules_file(tmp_path, monkeypatch):
    """Point the mapper at a temp rules file and reset its caches around each test."""
    def _write(yaml_text: str):
        p = tmp_path / "tag_rules.yaml"
        p.write_text(textwrap.dedent(yaml_text))
        monkeypatch.setenv("FINOPS_TAG_RULES", str(p))
        mapper.reload_rules()
        return p
    mapper.reload_rules()
    yield _write
    mapper.reload_rules()


def test_no_rules_file_returns_unattributed(monkeypatch, tmp_path):
    monkeypatch.setenv("FINOPS_TAG_RULES", str(tmp_path / "missing.yaml"))
    mapper.reload_rules()
    assert mapper.tags_to_attribution({"team": "platform"}) == {
        "team": "unattributed", "service": "", "environment": "",
    }
    mapper.reload_rules()


def test_direct_mapping_across_fields(rules_file):
    rules_file("""
        rules:
          - {tag_key: team, maps_to_field: team}
          - {tag_key: service, maps_to_field: service}
          - {tag_key: env, maps_to_field: environment}
    """)
    out = mapper.tags_to_attribution({"team": "payments", "service": "api", "env": "prod"})
    assert out == {"team": "payments", "service": "api", "environment": "prod"}


def test_tag_key_matching_is_case_insensitive_value_preserved(rules_file):
    rules_file("""
        rules:
          - {tag_key: Team, maps_to_field: team}
    """)
    # The KEY match is case-insensitive (rule "Team" matches tag "TEAM"), but the
    # tag VALUE is preserved exactly as provided. This locks existing behavior.
    assert mapper.tags_to_attribution({"TEAM": "Platform"})["team"] == "Platform"


def test_the_lowest_priority_number_wins(rules_file):
    # This test used to assert the opposite, deliberately: an earlier perf-only
    # refactor found that "lower = higher priority" in the docs disagreed with the
    # code, flagged it, and pinned the existing behaviour rather than change
    # attribution numbers in a PR about caching. That was the right call there and
    # the wrong thing to leave standing, because the code was overwriting an
    # explicit team tag with a cost centre code and calling it attribution.
    #
    # Now corrected to the documented contract: rules sort lowest-number-first and
    # the first match for a field wins. Note this DOES shift attribution for anyone
    # whose rules had a field collision, which is the point, and is why it belongs
    # in a release note rather than a footnote.
    rules_file("""
        rules:
          - {tag_key: costcenter, maps_to_field: team, priority: 50}
          - {tag_key: team, maps_to_field: team, priority: 10}
    """)
    out = mapper.tags_to_attribution({"costcenter": "cc-1", "team": "other"})
    assert out["team"] == "other", "the higher-priority team tag lost to a fallback"

    # And the fallback still works when the better tag is absent.
    assert mapper.tags_to_attribution({"costcenter": "cc-1"})["team"] == "cc-1"


def test_value_pattern_filters(rules_file):
    rules_file("""
        rules:
          - {tag_key: env, tag_value_pattern: "prod*", maps_to_field: environment}
    """)
    assert mapper.tags_to_attribution({"env": "production"})["environment"] == "production"
    assert mapper.tags_to_attribution({"env": "staging"})["environment"] == ""


def test_maps_to_value_overrides_actual(rules_file):
    rules_file("""
        rules:
          - {tag_key: team, tag_value_pattern: "*", maps_to_field: team, maps_to_value: central}
    """)
    assert mapper.tags_to_attribution({"team": "anything"})["team"] == "central"


def test_alias_resolution_is_case_insensitive_and_covers_variants(rules_file):
    rules_file("""
        rules:
          - {tag_key: team, maps_to_field: team}
        team_aliases:
          platform: [infra, infrastructure, platform-eng]
          data: [analytics, ml]
    """)
    assert mapper.tags_to_attribution({"team": "Infrastructure"})["team"] == "platform"
    assert mapper.tags_to_attribution({"team": "ML"})["team"] == "data"
    # The canonical name itself resolves to itself.
    assert mapper.tags_to_attribution({"team": "platform"})["team"] == "platform"
    # An unknown value passes through untouched.
    assert mapper.tags_to_attribution({"team": "mystery"})["team"] == "mystery"


def test_compile_is_memoized_and_reused(rules_file):
    rules_file("""
        rules:
          - {tag_key: team, maps_to_field: team}
    """)
    first = mapper._compiled()
    second = mapper._compiled()
    assert first is second  # same object: compiled once, not per call
    # A mapping call must not rebuild it.
    mapper.tags_to_attribution({"team": "x"})
    assert mapper._compiled() is first


def test_reload_rules_invalidates_the_compile(rules_file):
    path = rules_file("""
        rules:
          - {tag_key: team, maps_to_field: team}
    """)
    before = mapper._compiled()
    # Change the rules on disk and reload; the compiled object must be rebuilt.
    path.write_text(textwrap.dedent("""
        rules:
          - {tag_key: squad, maps_to_field: team}
    """))
    mapper.reload_rules()
    after = mapper._compiled()
    assert after is not before
    assert mapper.tags_to_attribution({"squad": "red"})["team"] == "red"
    assert mapper.tags_to_attribution({"team": "old"})["team"] == "unattributed"


def test_rules_sorted_once_not_per_call(rules_file, monkeypatch):
    """The sort happens at compile time, so mapping N entries sorts 0 times."""
    rules_file("""
        rules:
          - {tag_key: team, maps_to_field: team, priority: 30}
          - {tag_key: costcenter, maps_to_field: team, priority: 10}
    """)
    mapper._compiled()  # force compile now

    import builtins
    real_sorted = builtins.sorted
    calls = {"n": 0}

    def _counting_sorted(*a, **k):
        calls["n"] += 1
        return real_sorted(*a, **k)

    monkeypatch.setattr(mapper, "sorted", _counting_sorted, raising=False)
    for _ in range(1000):
        mapper.tags_to_attribution({"team": "a", "costcenter": "b"})
    # Zero sorts during the hot loop; the compile already did it.
    assert calls["n"] == 0
