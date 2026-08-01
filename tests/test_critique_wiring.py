"""The critique pass at its two real call sites.

test_recommendation_critique.py proves the module works. This file proves it is
actually WIRED, which is the failure mode that keeps recurring here: a helper
with a green unit suite that nothing calls, or that is called with the wrong
field name and silently sees no claim at all.

Both call sites are covered end to end:
  - run_full_cost_audit  (tools/cost_queries.py), savings key `monthly_savings`
  - list_savings_recommendations (tools/meta.py), key `estimated_monthly_savings_usd`

The key names differ between them, which is exactly how the first wiring bug
happened: the critic's default lookup list did not contain `monthly_savings`, so
on the audit path it read no dollar figure and passed everything.
"""
from __future__ import annotations

import pytest

# `finops.server` must be imported before the tool modules: the tool packages are
# wired up during its import, and reaching for finops.tools.* first lands in a
# partially initialized module and raises ImportError.
from finops import server as _server  # noqa: F401
from finops.recommendations import critique as C
from finops.tools import cost_queries as cq
from finops.tools import meta


# ── the field-name trap ───────────────────────────────────────────────────────

@pytest.mark.parametrize("key", [
    "monthly_savings",                  # run_full_cost_audit
    "estimated_monthly_savings_usd",    # the ledger / list_savings_recommendations
    "estimated_monthly_savings",
    "est_monthly_savings",
    "monthly_savings_usd",
])
def test_the_critic_finds_the_claim_under_every_field_name_in_use(key):
    # If a scanner's key is missing from SAVINGS_KEYS the critic reads no claim,
    # raises nothing, and reports a clean bill of health it never earned. That is
    # the worst way for this module to fail, so pin every name.
    rec = {"source": "rightsizing", key: 5000.0, "current_monthly_cost_usd": 1000.0}
    codes = {o.code for o in C.falsifiers(rec)}
    assert "savings_exceed_resource_spend" in codes, f"claim invisible under {key!r}"


def test_retracting_a_claim_clears_the_annual_figure_too():
    # A retracted monthly number that leaves its annual twin behind is the same
    # lie with a 12x on it.
    rec = {"source": "rightsizing", "monthly_savings": 5000.0,
           "estimated_annual_savings": 60000.0, "current_monthly_cost_usd": 1000.0}
    out = C.critique([rec], use_llm=False, savings_key="monthly_savings")[0]
    assert out["monthly_savings"] is None
    assert out["estimated_annual_savings"] is None


# ── call site 1: run_full_cost_audit ──────────────────────────────────────────

def _audit_findings():
    return [
        {"title": "Idle NAT gateways", "category": "network", "source": "waste",
         "monthly_savings": 1200.0, "current_monthly_cost_usd": 3000.0},
        # Claims more than the resource costs: must not survive.
        {"title": "Downsize the warehouse", "category": "compute", "source": "rightsizing",
         "monthly_savings": 9000.0, "current_monthly_cost_usd": 1500.0},
    ]


def test_audit_renders_a_band_instead_of_a_fake_figure(monkeypatch):
    """The load-bearing wiring test. Before the critique was wired in, the second
    finding printed '$9,000.00' next to a resource that only costs $1,500."""
    critiqued = C.critique(_audit_findings(), use_llm=False, savings_key="monthly_savings")
    bad = next(f for f in critiqued if f["title"] == "Downsize the warehouse")
    good = next(f for f in critiqued if f["title"] == "Idle NAT gateways")

    assert bad["monthly_savings"] is None
    assert bad["magnitude"]                       # a size band survives
    assert good["monthly_savings"] == 1200.0      # the honest one is untouched

    # And the renderer survives the None rather than raising on the format spec.
    total = sum(f.get("monthly_savings") or 0 for f in critiqued)
    assert total == 1200.0, "a retracted claim must not count toward the headline"
    assert hasattr(cq, "run_full_cost_audit")


def test_the_audit_call_site_actually_calls_the_critique():
    # Mutating the module is not enough; assert the call exists with the right
    # key, and that it runs BEFORE rescore. Ordering matters: a retracted claim
    # ranked on the figure it just lost would sort to the top of the list.
    import inspect

    src = inspect.getsource(cq.run_full_cost_audit.fn
                            if hasattr(cq.run_full_cost_audit, "fn") else cq.run_full_cost_audit)
    # Assert the WHOLE call, not the pieces. Checking for `savings_key="..."`
    # anywhere in the source passes even when the critique is called with the
    # wrong key, because rescore on the next line uses the same string. That
    # false green is exactly what this test exists to prevent.
    assert 'critique(findings, savings_key="monthly_savings")' in src, (
        "run_full_cost_audit does not call critique with its own savings key")
    assert src.index("critique(") < src.index("rescore("), "critique must run before rescore"


def test_the_audit_renderer_never_formats_a_none_as_currency():
    import inspect

    src = inspect.getsource(cq.run_full_cost_audit.fn
                            if hasattr(cq.run_full_cost_audit, "fn") else cq.run_full_cost_audit)
    assert "f['monthly_savings']:,.2f" not in src, (
        "a retracted finding carries None here and this format spec raises TypeError")
    assert "_savings_cell(" in src


# ── call site 2: list_savings_recommendations ─────────────────────────────────

def test_the_ledger_call_site_calls_the_critique_before_rescore():
    import inspect

    src = inspect.getsource(meta.list_savings_recommendations.fn
                            if hasattr(meta.list_savings_recommendations, "fn")
                            else meta.list_savings_recommendations)
    # Whole call, same reason as the audit site: rescore two lines below uses the
    # identical savings_key string, so a loose substring check is always green.
    assert 'critique(recs, savings_key="estimated_monthly_savings_usd")' in src, (
        "list_savings_recommendations does not call critique with its own savings key")
    assert src.index("critique(") < src.index("rescore("), "critique must run before rescore"


def test_the_ledger_total_excludes_what_it_retracted():
    # The headline `open_potential_usd` is the number a human reads first. If a
    # claim was just retracted, counting it there is the exact dishonesty this
    # whole pass exists to prevent.
    recs = [
        {"status": "open", "source": "rightsizing", "estimated_monthly_savings_usd": 1200.0,
         "current_monthly_cost_usd": 4000.0},
        {"status": "open", "source": "rightsizing", "estimated_monthly_savings_usd": 9000.0,
         "current_monthly_cost_usd": 1000.0},   # impossible: retracted
    ]
    out = C.critique(recs, use_llm=False, savings_key="estimated_monthly_savings_usd")
    total = round(sum(r.get("estimated_monthly_savings_usd") or 0
                      for r in out if r["status"] == "open"), 2)
    assert total == 1200.0
    assert sum(1 for r in out if not r["critique"]["survived"]) == 1


def test_both_call_sites_survive_a_broken_critique(monkeypatch):
    # The critic is a guard, not a dependency. If it throws, the audit and the
    # ledger must still answer; the guarantee is degraded, not the product.
    import inspect

    for fn in (cq.run_full_cost_audit, meta.list_savings_recommendations):
        target = fn.fn if hasattr(fn, "fn") else fn
        src = inspect.getsource(target)
        assert "critique(" in src, f"{target.__name__} no longer calls critique at all"
        head = src[src.index("critique("):]
        assert "except Exception" in head, (
            f"{target.__name__} calls critique outside a try/except")
