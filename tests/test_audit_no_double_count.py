# SPDX-License-Identifier: Apache-2.0
"""The audit total must not sum fixes you can only pick one of.

Why this file exists, stated plainly: run_full_cost_audit fans out to 21
scanners, each normalising its results into {title, monthly_savings, category,
detail} with no resource identity attached. Several of those scanners land on
the SAME instance with answers that are alternatives, not additions: migrate it
to Graviton, or convert it to Spot, or schedule it off out of hours, or shut it
down as idle. You can bank one of those. Summed raw, the audit claimed 141-156%
of an instance's own monthly cost as savings on that instance.

That number is worse than useless. It is the headline figure of the flagship
audit, it is the one a customer checks first, and it cannot survive being
checked. A total that overstates by half destroys the credibility of every
correct row underneath it.

The rule pinned here: one finding per resource, the largest saving wins, and the
alternatives ride along on the winner so nothing is hidden. Aggregate findings
("delete 12 orphaned alarms") carry no resource_id, cannot double count a single
resource, and pass through untouched.

The last two tests guard the wiring rather than the arithmetic, and they exist
because of a mistake made while writing this fix: the helper was first inserted
between `@_srv.mcp.tool()` and `async def run_full_cost_audit`, which registered
a private helper as a callable MCP tool and silently left the audit tool itself
unregistered. Nothing about that is visible in a diff read quickly, and no
existing test noticed.
"""
from __future__ import annotations

import asyncio

import pytest

import finops.server as _srv  # establishes the package; cost_queries imports back
from finops.tools.cost_queries import _collapse_per_resource


def test_mutually_exclusive_fixes_on_one_instance_are_not_summed():
    """The bug, at its narrowest. Four fixes, one instance, one banked saving."""
    findings = [
        {"title": "Migrate i-1 to Graviton", "monthly_savings": 40.0, "resource_id": "i-1"},
        {"title": "Convert i-1 to Spot", "monthly_savings": 70.0, "resource_id": "i-1"},
        {"title": "Schedule i-1 off-hours", "monthly_savings": 55.0, "resource_id": "i-1"},
    ]
    kept, collapsed = _collapse_per_resource(findings)
    total = sum(f["monthly_savings"] for f in kept)
    assert total == 70.0, (
        f"counted ${total} of savings on one instance whose best available fix is "
        f"worth $70. Raw sum would be $165, which is more than the instance costs."
    )
    assert collapsed == 2


def test_the_alternatives_are_kept_not_deleted():
    """Collapsing must not lose information, only stop double counting."""
    kept, _ = _collapse_per_resource([
        {"title": "Migrate i-1 to Graviton", "monthly_savings": 40.0, "resource_id": "i-1"},
        {"title": "Convert i-1 to Spot", "monthly_savings": 70.0, "resource_id": "i-1"},
    ])
    assert len(kept) == 1
    alts = kept[0].get("alternatives") or []
    assert [a["title"] for a in alts] == ["Migrate i-1 to Graviton"]
    assert alts[0]["monthly_savings"] == 40.0


def test_different_resources_still_add_up():
    """Two instances are two savings. Collapsing everything would be a bug too."""
    kept, collapsed = _collapse_per_resource([
        {"title": "Convert i-1 to Spot", "monthly_savings": 70.0, "resource_id": "i-1"},
        {"title": "Convert i-2 to Spot", "monthly_savings": 30.0, "resource_id": "i-2"},
    ])
    assert sum(f["monthly_savings"] for f in kept) == 100.0
    assert collapsed == 0


def test_aggregate_findings_pass_through_untouched():
    """"Delete 12 orphaned alarms" has no single resource and cannot collide."""
    agg = [
        {"title": "Delete 12 orphaned CloudWatch alarms", "monthly_savings": 3.6},
        {"title": "Release 4 unattached Elastic IPs", "monthly_savings": 14.4},
    ]
    kept, collapsed = _collapse_per_resource(list(agg))
    assert sum(f["monthly_savings"] for f in kept) == 18.0
    assert collapsed == 0


def test_a_retracted_claim_carrying_none_does_not_crash_the_collapse():
    """The critique sets monthly_savings to None; that must not become a winner."""
    kept, _ = _collapse_per_resource([
        {"title": "Unconfirmed fix on i-1", "monthly_savings": None, "resource_id": "i-1"},
        {"title": "Convert i-1 to Spot", "monthly_savings": 70.0, "resource_id": "i-1"},
    ])
    assert len(kept) == 1
    assert kept[0]["monthly_savings"] == 70.0


def test_the_per_resource_normalisers_emit_a_resource_id():
    """Without an id there is nothing to collapse on, so the fix is inert.

    Checked by AST so it holds under reformatting and covers the whole file,
    rather than matching one hand-picked source string.
    """
    import ast
    import pathlib

    import finops

    src = (pathlib.Path(finops.__file__).parent / "tools" / "cost_queries.py").read_text()
    tree = ast.parse(src)
    # every dict literal that has both "title" and "monthly_savings" is a finding
    findings_without_id = 0
    per_resource_with_id = 0
    for node in ast.walk(tree):
        if not isinstance(node, ast.Dict):
            continue
        keys = {k.value for k in node.keys if isinstance(k, ast.Constant)}
        if not {"title", "monthly_savings"} <= keys:
            continue
        if "resource_id" in keys:
            per_resource_with_id += 1
        else:
            findings_without_id += 1
    assert per_resource_with_id >= 6, (
        f"only {per_resource_with_id} normalisers attach a resource_id; the "
        f"per-resource scanners (graviton, spot, nonprod, lambda_pc, "
        f"s3_bucket_keys, nlb) must all attach one or their findings cannot be "
        f"collapsed and the total inflates again"
    )


def test_run_full_cost_audit_is_still_a_registered_tool():
    """Guards the wiring, because this is how the fix first broke it.

    Inserting the collapse helper between @_srv.mcp.tool() and the audit
    function made the DECORATOR apply to the helper. A private function became a
    callable MCP tool and the flagship audit silently stopped being one. The
    module still imported, every other test still passed.
    """
    names = {t.name for t in asyncio.run(_srv.mcp.list_tools())}
    registry = getattr(getattr(_srv.mcp, "_tool_manager", None), "_tools", None)
    all_names = set(registry.keys()) if registry else names
    assert "run_full_cost_audit" in all_names, (
        "run_full_cost_audit is no longer registered as an MCP tool, so the "
        "flagship audit is unreachable from any client"
    )


def test_no_private_helper_is_exposed_as_a_tool():
    """An underscore-prefixed function is internal and must never be callable."""
    registry = getattr(getattr(_srv.mcp, "_tool_manager", None), "_tools", None)
    names = set(registry.keys()) if registry else {
        t.name for t in asyncio.run(_srv.mcp.list_tools())}
    leaked = sorted(n for n in names if n.startswith("_"))
    assert not leaked, (
        f"private helpers registered as MCP tools and therefore callable by the "
        f"model: {leaked}"
    )


def test_run_full_cost_audit_actually_collapses_before_it_totals():
    """The call site. Every test above exercises the helper in isolation.

    That is not enough, and this test exists because it was not: deleting the
    `_collapse_per_resource(top)` line from run_full_cost_audit left all eight
    other tests green while restoring the original 141-156% bug in full. A helper
    nothing calls is not a fix.

    Asserted by AST so it survives reformatting: inside run_full_cost_audit, the
    call must appear, and it must appear BEFORE the statement that computes the
    total, since collapsing after the sum would change nothing.
    """
    import ast
    import pathlib

    import finops

    src = (pathlib.Path(finops.__file__).parent / "tools" / "cost_queries.py").read_text()
    fn = next(
        n for n in ast.walk(ast.parse(src))
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
        and n.name == "run_full_cost_audit"
    )

    collapse_line = None
    total_line = None
    for node in ast.walk(fn):
        if (isinstance(node, ast.Call)
                and getattr(node.func, "id", None) == "_collapse_per_resource"):
            collapse_line = node.lineno if collapse_line is None else collapse_line
        if isinstance(node, ast.Name) and node.id == "total_monthly" and isinstance(
                getattr(node, "ctx", None), ast.Store):
            total_line = node.lineno if total_line is None else total_line

    assert collapse_line is not None, (
        "run_full_cost_audit never calls _collapse_per_resource, so mutually "
        "exclusive fixes on one resource are summed again and the headline "
        "overstates by up to half an instance's cost"
    )
    assert total_line is not None, "total_monthly assignment not found"
    assert collapse_line < total_line, (
        f"_collapse_per_resource is called at line {collapse_line} but the total "
        f"is computed at line {total_line}: collapsing after the sum changes nothing"
    )
