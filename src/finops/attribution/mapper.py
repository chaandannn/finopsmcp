"""
Tag-to-team mapper. Reads rules from ~/.finops/tag_rules.yaml (or FINOPS_TAG_RULES).

Example tag_rules.yaml:
  rules:
    - tag_key: "team"
      maps_to_field: "team"
    - tag_key: "service"
      maps_to_field: "service"
    - tag_key: "env"
      maps_to_field: "environment"
    - tag_key: "environment"
      maps_to_field: "environment"
    - tag_key: "costcenter"
      maps_to_field: "team"

  # Optional: normalize free-form tag values to canonical team names
  team_aliases:
    platform: [infra, infrastructure, platform-eng]
    data: [analytics, ml, ml-platform, data-eng]
    frontend: [fe, web, ui]
"""
from __future__ import annotations

import os
from fnmatch import fnmatch
from pathlib import Path
from typing import Any

_RULES_CACHE: dict | None = None
# Rules and aliases never change between cost entries, but tags_to_attribution is
# called once per entry (thousands of times for a real org). Compiling the rules
# once (sort + field normalization + a flat alias lookup) turns each call from
# "re-sort R rules and rebuild every lowercased alias list" into a linear scan with
# O(1) alias resolution. _compiled() memoizes it; reload_rules() drops it.
_COMPILED: "_Compiled | None" = None


class _Compiled:
    """Pre-processed rules: sorted once, fields lowercased once, aliases flattened
    into a single {variant_lower: canonical} lookup so alias resolution is a dict
    hit instead of a scan over every alias list on every entry."""

    __slots__ = ("rules", "alias_lookup")

    def __init__(self, cfg: dict) -> None:
        raw_rules: list[dict] = cfg.get("rules", []) or []
        # Sort by priority (lower = higher priority) ONCE, then normalize the fields
        # the per-entry loop reads so it never lowercases the same literals again.
        self.rules: list[tuple[str, str, str, str]] = [
            (
                str(r.get("tag_key", "")).lower(),
                str(r.get("tag_value_pattern", "*")).lower(),
                str(r.get("maps_to_field", "")),
                str(r.get("maps_to_value", "")),
            )
            for r in sorted(raw_rules, key=lambda r: r.get("priority", 100))
        ]
        aliases: dict[str, list[str]] = cfg.get("team_aliases", {}) or {}
        lookup: dict[str, str] = {}
        for canonical, variants in aliases.items():
            lookup[str(canonical).lower()] = canonical
            for v in variants or []:
                lookup[str(v).lower()] = canonical
        self.alias_lookup = lookup


def _load_rules() -> dict:
    global _RULES_CACHE
    if _RULES_CACHE is not None:
        return _RULES_CACHE

    raw = os.environ.get("FINOPS_TAG_RULES", "")
    path = Path(raw).expanduser() if raw else Path.home() / ".finops" / "tag_rules.yaml"

    if not path.exists():
        _RULES_CACHE = {"rules": [], "team_aliases": {}}
        return _RULES_CACHE

    import yaml  # type: ignore[import]
    with open(path) as f:
        _RULES_CACHE = yaml.safe_load(f) or {"rules": [], "team_aliases": {}}
    return _RULES_CACHE


def _compiled() -> _Compiled:
    global _COMPILED
    if _COMPILED is None:
        _COMPILED = _Compiled(_load_rules())
    return _COMPILED


def reload_rules() -> None:
    global _RULES_CACHE, _COMPILED
    _RULES_CACHE = None
    _COMPILED = None


def _resolve_alias(value: str, alias_lookup: dict[str, str]) -> str:
    return alias_lookup.get(value.lower(), value)


def tags_to_attribution(tags: dict[str, str]) -> dict[str, str]:
    """
    Given a dict of resource tags, return {team, service, environment}.
    Falls back to 'unattributed' for unmapped fields.
    """
    compiled = _compiled()

    result: dict[str, str] = {
        "team": "unattributed",
        "service": "",
        "environment": "",
    }

    lower_tags = {k.lower(): v for k, v in tags.items()}

    # First matching rule wins, per field.
    #
    # Rules are sorted lowest-priority-number first, and the documented contract
    # is "lower number = higher priority", so the first rule that matches a field
    # is the most specific one the user wrote for it. Everything after it is a
    # fallback and must not overwrite.
    #
    # This used to let every matching rule assign, so the LAST rule won and the
    # priority number meant the exact opposite of what it says. Both examples in
    # the file write_example_rules() generates were wrong as a result: an explicit
    # "team" tag was overwritten by a "costcenter" code, and value normalisation
    # rules ("infra*" -> platform) never survived the plain rule beneath them.
    # Attribution feeds chargeback, so the symptom was a team disputing its bill
    # months later, not an error anyone could see.
    decided: set[str] = set()

    for tag_key, tag_value_pattern, maps_to_field, maps_to_value in compiled.rules:
        if maps_to_field in decided:
            continue  # a higher-priority rule already answered for this field
        if tag_key not in lower_tags:
            continue
        actual_value = lower_tags[tag_key]
        if not fnmatch(actual_value.lower(), tag_value_pattern):
            continue

        resolved = maps_to_value if maps_to_value else actual_value

        if maps_to_field == "team":
            result["team"] = _resolve_alias(resolved, compiled.alias_lookup)
        elif maps_to_field == "service":
            result["service"] = resolved
        elif maps_to_field == "environment":
            result["environment"] = resolved
        else:
            continue  # unknown target field, not a decision

        decided.add(maps_to_field)

    return result


def configured_tag_keys() -> list[str]:
    """The tag keys the rules actually reference, de-duplicated in first-seen order.

    What to ask AWS about when attribution comes back empty: checking every tag
    in the account would be noise, and these are the only ones that could have
    produced a team.
    """
    seen: list[str] = []
    for tag_key, _pattern, _field, _value in _compiled().rules:
        if tag_key and tag_key not in seen:
            seen.append(tag_key)
    return seen


def write_example_rules(path: Path | None = None) -> Path:
    """Write an example tag_rules.yaml to help users get started."""
    target = path or (Path.home() / ".finops" / "tag_rules.yaml")
    target.parent.mkdir(parents=True, exist_ok=True)

    content = """\
# FinOps tag attribution rules
# Map resource tags to team / service / environment
#
# The FIRST rule that matches a field wins. Rules are ordered by "priority",
# lowest number first, so put your most specific rules at the lowest numbers and
# your fallbacks at the highest. A rule for a field that has already been decided
# is skipped, which is what makes the fallbacks below safe to leave in place.

rules:
  # Map the "team" tag directly
  - tag_key: "team"
    maps_to_field: "team"
    priority: 10

  # Map "service" tag directly
  - tag_key: "service"
    maps_to_field: "service"
    priority: 10

  # Map "env" or "environment" tag to the environment field
  - tag_key: "env"
    maps_to_field: "environment"
    priority: 10

  - tag_key: "environment"
    maps_to_field: "environment"
    priority: 20

  # If "costcenter" exists, use it as team (lower priority than "team" tag)
  - tag_key: "costcenter"
    maps_to_field: "team"
    priority: 50

  # Map specific tag values to canonical names
  - tag_key: "team"
    tag_value_pattern: "infra*"
    maps_to_field: "team"
    maps_to_value: "platform"
    priority: 5

# Normalize free-form team names to canonical values
team_aliases:
  platform: [infra, infrastructure, platform-eng, sre]
  data: [analytics, ml, ml-platform, data-eng, dbt]
  frontend: [fe, web, ui, design]
  backend: [api, server, services]
"""
    target.write_text(content)
    return target
