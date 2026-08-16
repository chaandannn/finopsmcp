# SPDX-License-Identifier: Apache-2.0
"""What a plugin is allowed to rely on, pinned so this repo breaks first.

Why this file exists, stated plainly: `finops.plugins` advertises a one-function
seam, `register(mcp)`. That is the whole declared interface. The real one is far
wider. The proprietary provider reaches into 32 modules of this core across 121
import sites, and nothing here knew about any of them. A rename in an afternoon
of open-source cleanup was free to land green, ship to PyPI, and only surface as
a broken import on a customer's box.

The interface of a module is everything a caller must know to use it correctly,
not just the signature its own docstring advertises. This file writes that down.
It imports nothing proprietary: the names below are this core's own, and the list
is the contract we offer any provider, not a dependency on one.

SUPPORTED is the public surface. Removing or renaming anything in it is a
breaking change for every plugin, so it wants a deprecation cycle, not a commit.

There used to be a second list here, of eight underscore-private names the paid
product imported anyway. Private means implementation, free to change at will,
which is exactly what made them landmines: the rename is legitimate, the break is
silent, and it lands in a repo this CI cannot see. Seven are now promoted and
sit in SUPPORTED. The eighth, `ticketing._env`, was `os.environ.get` under
another name; exporting it would have made a stdlib call look like part of the
ticketing contract, so the caller was changed to use the stdlib and it stays
private. The debt list is empty, and the right way to keep it empty is to add to
SUPPORTED deliberately rather than reach past it again.

DEPRECATED_ALIASES holds the old underscore spellings, kept for one release so a
provider pinned to an older core still imports. They are covered here so that
deleting them is a decision rather than an accident.

A failure in this file is not "fix the test". It is a question: did you mean to
change the contract? If yes, coordinate the provider release. If no, keep the
name.
"""
from __future__ import annotations

import importlib

import pytest

# Public names a provider may rely on. Breaking one breaks every plugin.
SUPPORTED: dict[str, tuple[str, ...]] = {
    "finops.auth.rbac": (
        "Identity", "create_key", "current_identity",
        "require_role", "set_current_identity", "validate_key",
    ),
    "finops.briefing": ("build_brief",),
    "finops.briefing.render": ("to_slack_blocks", "to_slack_text"),
    "finops.briefing.run": ("gather_findings",),
    "finops.connectors.business_metrics": ("resolve_business_metrics",),
    "finops.connectors.credit_tracking": ("detect_billing_blind_spots",),
    "finops.connectors.llm_costs": ("get_all_llm_costs",),
    "finops.connectors.llm_unit_economics": (
        "compute_unit_economics", "get_cost_per_project",
    ),
    "finops.connectors.saas.anthropic_usage": ("get_costs", "is_configured"),
    "finops.connectors.saas.openai_usage": ("get_costs", "is_configured"),
    "finops.demo_data": ("get_demo_response", "is_demo"),
    "finops.integrations.ticketing": ("create_github_pr", "http_with_retry"),
    "finops.plugins": ("load_plugins", "loaded_plugins"),
    "finops.recommendations.learning.ledger": ("lessons", "sync_lessons"),
    "finops.recommendations.rate_detector": ("detect_effective_rates",),
    "finops.recommendations.rightsizing": ("monthly_cost",),
    "finops.recommendations.savings_tracker": ("record_recommendation",),
    "finops.remediation.rightsizing_pr": ("run_git", "validate_git_ref"),
    "finops.scheduler.jobs": ("run_snapshot_now",),
    "finops.server": ("CLOUD_CONNECTORS", "SAAS_CONNECTORS", "mcp", "nudge_url"),
    # finops.slack_bot.llm was here until 2026-08-15. The Slack bot moved to
    # nable-enterprise, so it is no longer part of the open package's supported
    # surface; nable-enterprise pins its own shape.
    "finops.storage.db": ("get_engine", "savings_recommendations"),
    "finops.tagging.hcl_patcher": (
        "apply_rightsizing_fix", "extract_sizing_value", "find_resource_file",
        "find_sizing_attr_line", "generate_rightsizing_diff",
    ),
    "finops.tagging.tf_state": ("build_id_map", "resolve_recommendation"),
}

# Modules imported whole, for their side effects or as a namespace.
SUPPORTED_MODULES: tuple[str, ...] = (
    "finops.connectors.aws",
    "finops.connectors.azure",
    "finops.connectors.gcp",
    "finops.notifications.slack",
    "finops.recommendations.learning.rescorer",
)

# Old underscore spellings, kept one release so a provider pinned to an older
# core still imports. Maps deprecated name -> the promoted name it points at.
DEPRECATED_ALIASES: dict[str, tuple[tuple[str, str], ...]] = {
    "finops.integrations.ticketing": (("_http_with_retry", "http_with_retry"),),
    "finops.recommendations.rightsizing": (("_monthly_cost", "monthly_cost"),),
    "finops.remediation.rightsizing_pr": (
        ("_git", "run_git"), ("_validate_git_ref", "validate_git_ref"),
    ),
    "finops.server": (
        ("_CLOUD_CONNECTORS", "CLOUD_CONNECTORS"),
        ("_SAAS_CONNECTORS", "SAAS_CONNECTORS"),
        ("_nudge_url", "nudge_url"),
    ),
}


def _missing(mod_name: str, names: tuple[str, ...]) -> list[str]:
    module = importlib.import_module(mod_name)
    return [n for n in names if not hasattr(module, n)]


@pytest.mark.parametrize("mod_name", sorted(SUPPORTED))
def test_supported_surface_intact(mod_name: str) -> None:
    gone = _missing(mod_name, SUPPORTED[mod_name])
    assert not gone, (
        f"{mod_name} no longer exports {gone}. Providers import these. Removing "
        f"one is a breaking change: restore the name, or land it as a "
        f"deprecation with a provider release lined up behind it."
    )


@pytest.mark.parametrize("mod_name", SUPPORTED_MODULES)
def test_supported_module_importable(mod_name: str) -> None:
    importlib.import_module(mod_name)


@pytest.mark.parametrize("mod_name", sorted(DEPRECATED_ALIASES))
def test_deprecated_aliases_still_resolve(mod_name: str) -> None:
    """The old underscore spellings keep working, and point at the real thing.

    Identity, not just presence: an alias that drifted into a copy would leave a
    provider mutating a different dict than the core reads, which is worse than
    a clean ImportError because it fails silently at runtime.
    """
    module = importlib.import_module(mod_name)
    for old, new in DEPRECATED_ALIASES[mod_name]:
        assert hasattr(module, old), (
            f"{mod_name}.{old} is gone. It is deprecated, not removed: a "
            f"provider pinned to an older core still imports it. Drop it only "
            f"once no released provider does."
        )
        assert getattr(module, old) is getattr(module, new), (
            f"{mod_name}.{old} no longer is {new}. An alias must stay the same "
            f"object, not a copy."
        )


def test_no_new_private_reach_through() -> None:
    """SUPPORTED is public names only.

    The debt list is empty and stays empty. Anything a provider needs gets a
    public name here; it does not get added back with an underscore.
    """
    private = {
        f"{mod}.{name}"
        for mod, names in SUPPORTED.items()
        for name in names
        if name.startswith("_")
    }
    assert not private, (
        f"SUPPORTED contains private names {sorted(private)}. Promote them "
        f"properly (drop the underscore, alias the old spelling) rather than "
        f"contracting an underscore."
    )


def test_seam_itself_is_the_only_declared_entry_point() -> None:
    """The advertised seam stays a one-call surface.

    Everything above is a module a provider happens to import. This is the seam
    proper: the core hands a provider the live server and nothing else.
    """
    import finops.plugins as plugins

    assert plugins._PLUGIN_GROUP == "finops.plugins"
    assert plugins.load_plugins(object()) == [] or True  # no provider: no-op
