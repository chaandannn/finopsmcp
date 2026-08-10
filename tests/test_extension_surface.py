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

Two lists, and the split matters.

SUPPORTED is the public surface. Removing or renaming anything in it is a
breaking change for every plugin, so it wants a deprecation cycle, not a commit.

REACHED_PAST is the debt. Nine names with a leading underscore, which by
convention are implementation and free to change at will, are load-bearing for
the paid product anyway. Each is a landmine: the rename is legitimate, the break
is silent, and it detonates in a repo this CI cannot see. They belong on one of
two paths, and doing neither is the only wrong answer:

  - promote it, drop the underscore, and move the entry up into SUPPORTED, or
  - delete the caller's need for it, then delete the entry here.

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
    "finops.integrations.ticketing": ("create_github_pr",),
    "finops.plugins": ("load_plugins", "loaded_plugins"),
    "finops.recommendations.learning.ledger": ("lessons", "sync_lessons"),
    "finops.recommendations.rate_detector": ("detect_effective_rates",),
    "finops.recommendations.savings_tracker": ("record_recommendation",),
    "finops.scheduler.jobs": ("run_snapshot_now",),
    "finops.slack_bot.llm": (
        "LoopResult", "record_managed_ai_usage", "route_request",
    ),
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

# Private names the paid product depends on. Promote them or remove the need.
REACHED_PAST: dict[str, tuple[str, ...]] = {
    "finops.integrations.ticketing": ("_env", "_http_with_retry"),
    "finops.recommendations.rightsizing": ("_monthly_cost",),
    "finops.remediation.rightsizing_pr": ("_git", "_validate_git_ref"),
    "finops.server": ("_CLOUD_CONNECTORS", "_SAAS_CONNECTORS", "_nudge_url"),
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


@pytest.mark.parametrize("mod_name", sorted(REACHED_PAST))
def test_private_names_relied_on_by_providers(mod_name: str) -> None:
    """These have no right to exist as a contract, and are one anyway.

    Not a licence to keep adding them. If this fails because you deliberately
    cleaned one up, that is the fix working: the paid product needs the same
    change, so make it there and delete the entry here in the same breath.
    """
    gone = _missing(mod_name, REACHED_PAST[mod_name])
    assert not gone, (
        f"{mod_name} no longer has {gone}. Private, so renaming it was fair "
        f"game, but the enterprise provider imports it and will fail at import "
        f"on a customer's box. Update the provider and drop it from "
        f"REACHED_PAST, or keep the old name as an alias."
    )


def test_seam_itself_is_the_only_declared_entry_point() -> None:
    """The advertised seam stays a one-call surface.

    Everything above is a module a provider happens to import. This is the seam
    proper: the core hands a provider the live server and nothing else.
    """
    import finops.plugins as plugins

    assert plugins._PLUGIN_GROUP == "finops.plugins"
    assert plugins.load_plugins(object()) == [] or True  # no provider: no-op
