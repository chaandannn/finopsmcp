"""Reservations locked to one subscription while a sibling pays on demand.

The whole point of this detector is that it needs THREE conditions. Single scope
alone is not waste — Microsoft documents two legitimate reasons to choose it
(capacity priority, which shared scope gives up, and chargeback) — so a detector
that fires on scope alone produces confident false positives on deliberate
architecture.

These tests attack each condition in isolation, then attack the arithmetic.
"""
from __future__ import annotations

import pytest

from finops.recommendations.azure_reservation_scope import (
    find_scope_limited_reservations,
)
from finops.recommendations.envelope import INFERRED, MEASURED


def _res(**over):
    base = {
        "reservation_id": "/providers/Microsoft.Capacity/reservationOrders/abc/reservations/def",
        "sku_name": "Standard_D4s_v5",
        "meter_id": "meter-d4s-v5",
        "region": "eastus",
        "applied_scope_type": "Single",
        "applied_scopes": ["/subscriptions/11111111-1111-1111-1111-111111111111"],
        "avg_utilization_pct": 55.0,
        "wasted_hours": 320.0,
        "reserved_hours": 720.0,
        "reserved_cost_usd": 720.0,          # -> $1.00/reserved hour
    }
    base.update(over)
    return base


def _usage(**over):
    base = {
        "subscription_id": "22222222-2222-2222-2222-222222222222",
        "meter_id": "meter-d4s-v5",
        "sku_name": "Standard_D4s_v5",
        "region": "eastus",
        "on_demand_cost_usd": 900.0,
    }
    base.update(over)
    return base


# ── all three, or nothing ────────────────────────────────────────────────────

def test_all_three_conditions_produce_one_finding():
    out = find_scope_limited_reservations([_res()], [_usage()])
    assert len(out) == 1
    f = out[0]
    assert f.evidence == MEASURED
    assert "locked to one subscription" in f.title
    assert f.metadata["uncovered_subscriptions"] == ["22222222-2222-2222-2222-222222222222"]


def test_shared_scope_is_never_flagged():
    """The reservation already reaches everything. Nothing to fix."""
    assert find_scope_limited_reservations(
        [_res(applied_scope_type="Shared")], [_usage()]) == []


def test_management_group_scope_is_not_narrow():
    """It already spans subscriptions, and Azure auto-converts it to Shared when
    the last subscription leaves it. Flagging it would be noise."""
    assert find_scope_limited_reservations(
        [_res(applied_scope_type="ManagementGroup")], [_usage()]) == []


def test_a_fully_utilised_reservation_is_never_flagged():
    """Condition 2. A reservation at 100% is doing its job; the scope is a
    deliberate choice and there is no money on the table."""
    assert find_scope_limited_reservations(
        [_res(avg_utilization_pct=99.0)], [_usage()]) == []


def test_no_sibling_spend_means_no_finding():
    """Condition 3, the one that makes this worth building. Underutilised AND
    single-scoped is still not actionable if nobody else is paying full price —
    the right fix there is to shrink or exchange, not to re-scope."""
    assert find_scope_limited_reservations([_res()], []) == []


def test_spend_inside_the_reservations_own_scope_does_not_count():
    """The discount already applies there. Counting it would invent savings."""
    covered = _usage(subscription_id="11111111-1111-1111-1111-111111111111")
    assert find_scope_limited_reservations([_res()], [covered]) == []


def test_a_resource_group_scope_still_cannot_reach_another_subscription():
    """Scoped to an RG inside sub 1: sub 2 is still unreachable."""
    res = _res(
        applied_scope_type="SingleResourceGroup",
        applied_scopes=["/subscriptions/11111111-1111-1111-1111-111111111111"
                        "/resourceGroups/prod-rg"],
    )
    out = find_scope_limited_reservations([res], [_usage()])
    assert len(out) == 1
    assert "11111111-1111-1111-1111-111111111111" in out[0].metadata["reachable_subscriptions"]


# ── matching rules ───────────────────────────────────────────────────────────

def test_a_different_meter_is_not_a_match():
    assert find_scope_limited_reservations(
        [_res()], [_usage(meter_id="meter-e8s-v5", sku_name="Standard_E8s_v5")]) == []


def test_a_region_mismatch_downgrades_to_an_investigation():
    """Same meter, different region. The discount may not apply, so this is a
    lead, not a figure."""
    out = find_scope_limited_reservations([_res()], [_usage(region="westus2")])
    assert len(out) == 1
    f = out[0]
    assert f.evidence == INFERRED
    assert f.est_monthly_savings is None, "an investigation must not carry a precise figure"
    assert f.rough_monthly is not None
    assert "region could not be confirmed" in f.why_unsure


def test_an_unknown_region_on_either_side_is_not_a_strict_match():
    """A blank region must not be treated as 'matches everything'."""
    for res_region, use_region in (("", "eastus"), ("eastus", ""), ("", "")):
        out = find_scope_limited_reservations(
            [_res(region=res_region)], [_usage(region=use_region)])
        assert out and out[0].evidence == INFERRED, (res_region, use_region)


def test_trivial_sibling_spend_is_ignored():
    """A few dollars of drift is not worth anyone's attention."""
    assert find_scope_limited_reservations(
        [_res()], [_usage(on_demand_cost_usd=5.0)]) == []


def test_strict_matches_win_over_loose_ones():
    """With both available, the finding must be the confident one."""
    out = find_scope_limited_reservations(
        [_res()],
        [_usage(region="westus2", on_demand_cost_usd=5000.0),   # loose, bigger
         _usage(subscription_id="33333333-3333-3333-3333-333333333333")],  # strict
    )
    assert len(out) == 1
    assert out[0].evidence == MEASURED
    assert out[0].metadata["match"] == "meter+region"
    # the loose, larger sibling must NOT inflate the figure
    assert out[0].metadata["sibling_on_demand_usd"] == 900.0


# ── the arithmetic ───────────────────────────────────────────────────────────

def test_savings_are_bounded_by_the_wasted_capacity():
    """320 wasted hours at $1/hr = $320. The sibling spent $900. You cannot save
    more than the reservation actually wasted."""
    f = find_scope_limited_reservations([_res()], [_usage()])[0]
    assert f.est_monthly_savings == pytest.approx(320.0)


def test_savings_are_also_bounded_by_what_the_sibling_actually_spent():
    """The mirror image: 320 hours wasted, but the sibling only spent $80. You
    cannot recover spend that never happened."""
    f = find_scope_limited_reservations(
        [_res()], [_usage(on_demand_cost_usd=80.0)])[0]
    assert f.est_monthly_savings == pytest.approx(80.0)


def test_an_hourly_rate_is_used_directly_when_given():
    f = find_scope_limited_reservations(
        [_res(hourly_rate_usd=0.5, reserved_cost_usd=None)], [_usage()])[0]
    assert f.est_monthly_savings == pytest.approx(160.0)   # 320h * $0.50


def test_no_rate_means_no_invented_figure():
    """Without a rate we cannot price the wasted hours. A fabricated number is
    worse than an honest band, so the signal survives and the precision does not."""
    res = _res(reserved_cost_usd=None, reserved_hours=None, hourly_rate_usd=None)
    f = find_scope_limited_reservations([res], [_usage()])[0]
    assert f.est_monthly_savings is None
    assert f.rough_monthly is None
    d = f.to_dict()
    # No figure may appear anywhere: not a savings number, not a band derived
    # from one. (magnitude is an investigation-only field; a measured finding
    # with an unpriceable value simply carries no figure.)
    assert d.get("est_monthly_savings") is None
    assert d.get("magnitude") in (None, "unknown size")
    # ...but the measured facts that ARE known still reach the reader.
    assert d["metadata"]["wasted_hours"] == 320.0
    assert d["metadata"]["sibling_on_demand_usd"] == 900.0


@pytest.mark.parametrize("bad", [None, "n/a", -5, 0])
def test_a_garbage_rate_is_refused_rather_than_used(bad):
    res = _res(hourly_rate_usd=bad, reserved_cost_usd=None, reserved_hours=None)
    f = find_scope_limited_reservations([res], [_usage()])[0]
    assert f.est_monthly_savings is None


def test_multiple_uncovered_subscriptions_are_summed_and_listed():
    out = find_scope_limited_reservations([_res()], [
        _usage(subscription_id="22222222-2222-2222-2222-222222222222", on_demand_cost_usd=200.0),
        _usage(subscription_id="33333333-3333-3333-3333-333333333333", on_demand_cost_usd=150.0),
    ])
    f = out[0]
    assert f.metadata["sibling_on_demand_usd"] == pytest.approx(350.0)
    assert len(f.metadata["uncovered_subscriptions"]) == 2
    assert "2 other subscription(s)" in f.title


# ── what the reader is told ──────────────────────────────────────────────────

def test_the_finding_says_the_fix_is_free():
    """The whole reason this outranks other reservation findings: no exchange,
    no refund, no commercial transaction. If the reader does not know that, they
    will queue it behind things that cost money."""
    f = find_scope_limited_reservations([_res()], [_usage()])[0]
    text = f.why + " " + " ".join(f.remediation)
    assert "free" in text.lower()
    assert "no exchange" in text.lower()


def test_the_finding_warns_about_the_capacity_priority_tradeoff():
    """Shared scope turns instance-size flexibility on and gives up reserved
    capacity for a specific size. Somebody who chose single scope on purpose must
    be told that before they change it."""
    f = find_scope_limited_reservations([_res()], [_usage()])[0]
    joined = " ".join(f.remediation).lower()
    assert "capacity priority" in joined
    assert "instance-size flexibility" in joined or "instance size flexibility" in joined


def test_the_remediation_is_a_setting_not_a_resource_change():
    """Nothing is created, resized or deleted. The resource map has nothing to
    find here, and the metadata says so rather than letting an empty map read as
    'nothing depends on this'."""
    f = find_scope_limited_reservations([_res()], [_usage()])[0]
    assert f.metadata["remediation_kind"] == "billing_configuration"
    assert not any(v in " ".join(f.remediation).lower()
                   for v in ("delete", "terminate", "resize"))


def test_the_finding_survives_the_critique_pass():
    """End to end through the real critic: a well-formed finding must not be
    retracted by its own falsifiers."""
    from finops.recommendations.critique import critique

    f = find_scope_limited_reservations([_res()], [_usage()])[0].to_dict()
    reviewed = critique([f], use_llm=False)[0]
    assert reviewed["critique"]["survived"] is True, reviewed["critique"]["objections"]


# ── robustness ───────────────────────────────────────────────────────────────

def test_empty_and_malformed_input_never_raises():
    assert find_scope_limited_reservations([], []) == []
    assert find_scope_limited_reservations(None, None) == []
    assert find_scope_limited_reservations([{}], [{}]) == []
    assert find_scope_limited_reservations([_res(applied_scopes=None)], [_usage()])


def test_scope_type_casing_and_whitespace_do_not_matter():
    for variant in ("Single", "single", " SINGLE ", "Subscription"):
        assert find_scope_limited_reservations(
            [_res(applied_scope_type=variant)], [_usage()]), variant


def test_a_missing_utilisation_figure_is_not_treated_as_zero():
    """None must not read as '0% utilised, definitely wasted'."""
    assert find_scope_limited_reservations(
        [_res(avg_utilization_pct=None)], [_usage()]) == []


# ── the wiring: tool -> connector -> detector -> critique ────────────────────

def _fake_util(**over):
    base = {"reservations": [_res()], "avg_utilization_pct": 55.0,
            "period": "x", "source": "azure_reservations_api"}
    base.update(over)
    return base


def _fake_usage_payload(**over):
    base = {"usage": [_usage()], "period": "x", "source": "azure_cost_management"}
    base.update(over)
    return base


def _run_tool(monkeypatch, util, usage):
    import asyncio

    import finops.connectors.azure_detail as det
    from finops import server  # noqa: F401
    from finops.tools import azure as az

    monkeypatch.setattr(det, "get_reservation_utilization", lambda **k: util)
    monkeypatch.setattr(det, "get_on_demand_usage_by_subscription", lambda **k: usage)
    fn = getattr(az.audit_azure_reservation_scope, "fn", az.audit_azure_reservation_scope)
    return asyncio.run(fn())


def test_the_tool_runs_the_whole_pipeline(monkeypatch):
    out = _run_tool(monkeypatch, _fake_util(), _fake_usage_payload())
    assert out.get("error") is None
    assert out["reservations_checked"] == 1
    assert out["subscriptions_checked"] == 1
    assert len(out["findings"]) == 1
    f = out["findings"][0]
    assert f["critique"]["survived"] is True
    assert out["est_monthly_savings_usd"] == pytest.approx(320.0)
    assert "changes nothing" in out["note"]


def test_the_tool_headline_counts_only_findings_that_survived_critique(monkeypatch):
    """The trust envelope reaching the tool's own total: a retracted figure must
    not be summed."""
    out = _run_tool(monkeypatch, _fake_util(), _fake_usage_payload())
    total = out["est_monthly_savings_usd"]
    summed = sum((f.get("est_monthly_savings") or 0.0) for f in out["findings"]
                 if (f.get("critique") or {}).get("survived", True))
    assert total == pytest.approx(summed)


def test_a_connector_error_is_returned_not_swallowed(monkeypatch):
    out = _run_tool(monkeypatch, {"error": "Azure not configured."},
                    _fake_usage_payload())
    assert out["error"] == "Azure not configured."


def test_a_clean_estate_returns_empty_findings_not_an_error(monkeypatch):
    out = _run_tool(monkeypatch,
                    _fake_util(reservations=[_res(applied_scope_type="Shared")]),
                    _fake_usage_payload())
    assert out.get("error") is None
    assert out["findings"] == []
    assert out["est_monthly_savings_usd"] == 0.0


def test_the_tool_is_registered_and_in_the_azure_family():
    """The wiring test that catches an unregistered tool or a family-map miss,
    which would advertise it never (or always)."""
    from finops import server, tool_surface as ts

    names = {t.name for t in server.mcp._tool_manager.list_tools()}
    assert "audit_azure_reservation_scope" in names
    assert "audit_azure_reservation_scope" in ts.FAMILY_TOOLS["azure"]
