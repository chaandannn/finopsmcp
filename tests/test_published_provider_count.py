# SPDX-License-Identifier: Apache-2.0
"""A provider count printed on a website has to come from the registry.

Why this file exists, stated plainly: getnable.com said "14 providers" and no
part of this codebase produced a 14. The connector registry holds 12 (3 cloud, 9
SaaS) and the tool a user actually calls, list_connected_providers, enumerates
20, because it also covers the LLM and GPU providers that are wired up
separately. Three numbers, none of which agreed, and the published one was
derivable from nothing.

That is not a rounding argument. The whole product claim is that nable's numbers
are checkable, and the first number a visitor reads was a guess. It happened to
be an UNDER-claim, which is how it survived: nobody complains that you support
more things than advertised, so nothing ever corrected it.

The rule pinned here: the published figure is whatever list_connected_providers
enumerates, because that is the answer the product itself gives when a user asks
what it supports. A marketing number and a product answer that disagree is a bug
in whichever one is wrong, and the code is the one that can be checked.

This test does not read the website; that lives in another repository. It pins
the count and names the surfaces that publish it, so changing the registry fails
here and the failure says exactly which copy to go and update. That is the point:
the number cannot drift silently in either direction.
"""
from __future__ import annotations

import asyncio

import pytest

import finops.server as server

# The figure published on getnable.com and in any launch copy. Update this in the
# same commit that adds or removes a provider, and update the surfaces listed in
# PUBLISHED_SURFACES below, or the claim and the product disagree again.
PUBLISHED_PROVIDER_COUNT = 20

PUBLISHED_SURFACES = (
    "nable-web: index.html hero fine print ('N providers · read-only access')",
    "nable-web: app.jsx, the same line in the React hero",
    "internal/reddit-selfhosted-megathread-2026-08-13.md",
    "any launch copy in internal/",
)


def _enumerated_providers() -> list[str]:
    registry = getattr(getattr(server.mcp, "_tool_manager", None), "_tools", {}) or {}
    tool = registry.get("list_connected_providers")
    assert tool is not None, "list_connected_providers is no longer a registered tool"
    result = asyncio.run(tool.fn())
    # keys prefixed with _ are metadata (_plan and friends), not providers
    return sorted(k for k in result if not k.startswith("_"))


def test_the_published_count_matches_what_the_product_reports():
    """The number on the site is the number the tool enumerates."""
    providers = _enumerated_providers()
    assert len(providers) == PUBLISHED_PROVIDER_COUNT, (
        f"list_connected_providers now enumerates {len(providers)} providers but "
        f"PUBLISHED_PROVIDER_COUNT says {PUBLISHED_PROVIDER_COUNT}.\n\n"
        f"Providers: {providers}\n\n"
        f"Update the constant AND every surface that prints it:\n  - "
        + "\n  - ".join(PUBLISHED_SURFACES)
        + "\n\nA count on a marketing page that no code produces is how this "
          "started: the site said 14, the registry held 12, and the tool "
          "answered 20."
    )


def test_the_count_is_not_quietly_measuring_an_empty_set():
    """Guards this file against passing because the tool returned nothing.

    If list_connected_providers ever returns {} the assertion above becomes
    0 == 20 and fails loudly, but a future refactor that returns a metadata-only
    dict would sail through the underscore filter. So: the well-known providers
    must be in there by name.
    """
    providers = set(_enumerated_providers())
    for essential in ("aws", "azure", "gcp"):
        assert essential in providers, (
            f"{essential} is missing from list_connected_providers, so whatever "
            f"this test is counting is not the provider list"
        )


def test_the_registry_subset_is_still_a_subset():
    """The enumerated list must cover the connector registry, not diverge from it.

    list_connected_providers reports more than the registry because LLM and GPU
    providers are wired separately. That is fine. What is not fine is the two
    drifting apart, which would mean a connector exists that the product never
    tells anyone about.
    """
    enumerated = set(_enumerated_providers())
    registry = set(server.CLOUD_CONNECTORS) | set(server.SAAS_CONNECTORS)
    missing = sorted(registry - enumerated)
    assert not missing, (
        f"these connectors are registered but list_connected_providers never "
        f"names them, so a user asking what nable supports is told less than it "
        f"does: {missing}"
    )
