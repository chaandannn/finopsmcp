"""NAT data-processing and GCS request overhead: the per-operation charges.

Both detections are INFERRED investigations by design: the fee is on the bill,
but the recoverable fraction needs flow logs / access logs we do not read. These
tests attack the gating conditions, the matcher, and the arithmetic, and then
pin the failure direction: a renamed SKU may cause a MISS, never a fabrication.
"""
from __future__ import annotations

import pytest

from finops.recommendations.envelope import INFERRED
from finops.recommendations.gcp_traffic_costs import (
    HEALTHY_GCS_OPS_RATIO,
    find_gcs_request_overhead,
    find_nat_processing_overhead,
)


def _row(project="proj-a", service="Networking", sku="", cost=0.0):
    return {"project_id": project, "service": service, "sku": sku, "cost_usd": cost}


def _nat_rows(processing=300.0, uptime=60.0, project="proj-a"):
    return [
        _row(project, "Networking", "Cloud NAT Gateway: Data Processing", processing),
        _row(project, "Networking", "Cloud NAT Gateway: Uptime", uptime),
    ]


def _gcs_rows(ops_a=200.0, ops_b=100.0, storage=400.0, project="proj-a"):
    return [
        _row(project, "Cloud Storage", "Class A Operations", ops_a),
        _row(project, "Cloud Storage", "Class B Operations", ops_b),
        _row(project, "Cloud Storage", "Standard Storage US Multi-region", storage),
    ]


# ── NAT: the gating conditions ───────────────────────────────────────────────

def test_nat_processing_dominating_uptime_is_flagged():
    out = find_nat_processing_overhead(_nat_rows(processing=300.0, uptime=60.0))
    assert len(out) == 1
    f = out[0]
    assert f.evidence == INFERRED
    assert f.est_monthly_savings is None, "no precise figure without flow logs"
    assert f.rough_monthly == pytest.approx(300.0)
    assert f.metadata["processing_to_uptime_ratio"] == pytest.approx(5.0)


def test_nat_proportionate_processing_is_not_flagged():
    """Processing below the multiple is a gateway doing gateway things."""
    assert find_nat_processing_overhead(_nat_rows(processing=100.0, uptime=60.0)) == []


def test_nat_below_the_fee_floor_is_ignored():
    assert find_nat_processing_overhead(_nat_rows(processing=40.0, uptime=5.0)) == []


def test_nat_missing_uptime_rows_fall_back_to_the_absolute_floor():
    """No uptime SKU means the multiple cannot be computed. The floor decides,
    and the finding says no uptime charge was found rather than inventing one."""
    rows = [_row(sku="Cloud NAT Gateway: Data Processing", cost=200.0)]
    out = find_nat_processing_overhead(rows)
    assert len(out) == 1
    assert out[0].metadata["uptime_fee_usd"] == 0.0
    assert out[0].metadata["processing_to_uptime_ratio"] is None
    assert "no matching uptime charge" in out[0].why


def test_nat_projects_are_independent():
    rows = _nat_rows(processing=300.0, uptime=60.0, project="hot") + \
           _nat_rows(processing=10.0, uptime=60.0, project="quiet")
    out = find_nat_processing_overhead(rows)
    assert [f.metadata["project_id"] for f in out] == ["hot"]


def test_nat_gateway_enrichment_is_context_not_a_condition():
    out = find_nat_processing_overhead(
        _nat_rows(), gateways_by_project={"proj-a": ["nat-gw-1", "nat-gw-2"]})
    assert out[0].metadata["gateways"] == ["nat-gw-1", "nat-gw-2"]
    assert out[0].metadata["gateways_examined"] is True
    # absence changes nothing
    out2 = find_nat_processing_overhead(_nat_rows())
    assert len(out2) == 1
    assert out2[0].metadata["gateways_examined"] is False


def test_nat_matcher_ignores_unrelated_data_processing_skus():
    """'Data processing' appears in other services (Dataflow). Only NAT SKUs count."""
    rows = [_row(service="Dataflow", sku="Data Processing vCPU", cost=5000.0)]
    assert find_nat_processing_overhead(rows) == []


# ── GCS: the gating conditions ───────────────────────────────────────────────

def test_gcs_filesystem_shaped_usage_is_flagged():
    out = find_gcs_request_overhead(_gcs_rows(ops_a=200.0, ops_b=100.0, storage=400.0))
    assert len(out) == 1
    f = out[0]
    assert f.evidence == INFERRED
    assert f.metadata["operations_fee_usd"] == pytest.approx(300.0)
    assert f.metadata["ops_to_storage_pct"] == pytest.approx(75.0)


def test_gcs_recoverable_is_the_excess_over_the_healthy_share():
    """$300 ops on $400 storage; healthy share is 15% = $60. Recoverable = $240.
    Normal usage below the healthy line is never counted."""
    f = find_gcs_request_overhead(_gcs_rows())[0]
    assert f.rough_monthly == pytest.approx(300.0 - 400.0 * HEALTHY_GCS_OPS_RATIO)


def test_gcs_healthy_ratio_is_not_flagged():
    """$50 ops on $1000 storage is 5%: ordinary access."""
    assert find_gcs_request_overhead(
        _gcs_rows(ops_a=30.0, ops_b=20.0, storage=1000.0)) == []


def test_gcs_below_the_ops_floor_is_ignored_even_at_a_bad_ratio():
    assert find_gcs_request_overhead(
        _gcs_rows(ops_a=10.0, ops_b=5.0, storage=10.0)) == []


def test_gcs_ops_with_no_storage_at_all_still_flags():
    """Real ops cost against zero storage is its own smell (pure churn), and
    with no denominator the excess is the whole ops figure."""
    rows = [_row(service="Cloud Storage", sku="Class A Operations", cost=80.0)]
    out = find_gcs_request_overhead(rows)
    assert len(out) == 1
    assert out[0].metadata["ops_to_storage_pct"] is None
    assert out[0].rough_monthly == pytest.approx(80.0)
    assert "no storage cost found" in out[0].why


def test_gcs_storage_sku_matching_excludes_operations():
    """'Operations' must not be double-counted into storage: the matcher for
    storage excludes it explicitly."""
    f = find_gcs_request_overhead(_gcs_rows())[0]
    assert f.metadata["storage_fee_usd"] == pytest.approx(400.0)


def test_gcs_other_services_never_leak_in():
    rows = [_row(service="BigQuery", sku="Class A Operations", cost=900.0)]
    assert find_gcs_request_overhead(rows) == []


# ── shared robustness ────────────────────────────────────────────────────────

@pytest.mark.parametrize("finder", [find_nat_processing_overhead, find_gcs_request_overhead])
def test_empty_and_malformed_input_never_raises(finder):
    assert finder([]) == []
    assert finder(None) == []
    assert finder([{}]) == []
    assert finder([{"project_id": "", "sku": None, "cost_usd": "n/a"}]) == []


@pytest.mark.parametrize("bad", [float("nan"), float("inf"), -50.0, "oops", None])
def test_garbage_costs_read_as_zero_not_as_money(bad):
    rows = [_row(sku="Cloud NAT Gateway: Data Processing", cost=bad)]
    assert find_nat_processing_overhead(rows) == []


def test_sku_matching_is_case_insensitive():
    rows = [
        _row(service="NETWORKING", sku="CLOUD NAT GATEWAY: DATA PROCESSING", cost=300.0),
        _row(service="networking", sku="cloud nat gateway: uptime", cost=10.0),
    ]
    assert len(find_nat_processing_overhead(rows)) == 1


def test_a_renamed_sku_misses_and_never_fabricates():
    """The chosen failure direction: if GCP renames the SKU, the detection goes
    quiet. It must not fall through into some looser match."""
    rows = [_row(sku="Cloud NAT Gateway: Egress Handling", cost=900.0)]
    assert find_nat_processing_overhead(rows) == []


def test_both_finders_survive_the_critique_pass():
    from finops.recommendations.critique import critique

    dicts = [f.to_dict() for f in
             find_nat_processing_overhead(_nat_rows())
             + find_gcs_request_overhead(_gcs_rows())]
    assert len(dicts) == 2
    for reviewed in critique(dicts, use_llm=False):
        assert reviewed["critique"]["survived"] is True, reviewed["critique"]["objections"]
        assert reviewed["kind"] == "investigation"
        assert reviewed.get("est_monthly_savings") is None


# ── the wiring ───────────────────────────────────────────────────────────────

def _run_tool(monkeypatch, tool_name, payload):
    import asyncio

    import finops.connectors.gcp as gcp_conn
    from finops import server  # noqa: F401
    from finops.tools import gcp as gcp_tools

    monkeypatch.setattr(gcp_conn, "get_sku_costs_by_project", lambda sd, ed: payload)
    tool = getattr(gcp_tools, tool_name)
    fn = getattr(tool, "fn", tool)
    return asyncio.run(fn())


def test_the_nat_tool_runs_the_whole_pipeline(monkeypatch):
    out = _run_tool(monkeypatch, "audit_gcp_nat_fees",
                    {"rows": _nat_rows(), "period": "x"})
    assert out.get("error") is None
    assert len(out["findings"]) == 1
    assert out["findings"][0]["critique"]["survived"] is True
    assert out["projects_checked"] == 1


def test_the_gcs_tool_runs_the_whole_pipeline(monkeypatch):
    out = _run_tool(monkeypatch, "audit_gcs_request_overhead",
                    {"rows": _gcs_rows(), "period": "x"})
    assert out.get("error") is None
    assert len(out["findings"]) == 1


def test_a_missing_billing_export_is_an_error_not_an_empty_all_clear(monkeypatch):
    """Without the export both detections are blind. Blind must not read as
    clean."""
    payload = {"error": "GCP_BQ_BILLING_TABLE is not set. SKU-level detection "
                        "needs the BigQuery billing export."}
    for tool in ("audit_gcp_nat_fees", "audit_gcs_request_overhead"):
        out = _run_tool(monkeypatch, tool, payload)
        assert out.get("error"), tool
        assert "findings" not in out


def test_both_tools_are_registered_and_in_the_gcp_family():
    from finops import server, tool_surface as ts

    names = {t.name for t in server.mcp._tool_manager.list_tools()}
    for t in ("audit_gcp_nat_fees", "audit_gcs_request_overhead"):
        assert t in names, t
        assert t in ts.FAMILY_TOOLS["gcp"], t


def test_nat_matcher_excludes_other_networking_data_processing_skus():
    """Caught by mutation testing: the Dataflow test above is excluded by the
    SERVICE gate, so it never exercised the 'nat' word requirement. Load
    balancing bills a data-processing SKU under the same Networking service, and
    it must not count as NAT."""
    rows = [_row(service="Networking",
                 sku="Cloud Load Balancing: Data Processing", cost=900.0)]
    assert find_nat_processing_overhead(rows) == []


def test_gcs_a_sku_containing_both_storage_and_operations_counts_as_ops():
    """Caught by mutation testing: Class A/B SKUs do not contain the word
    'storage', so the storage matcher's operations-exclusion was never
    exercised. A SKU carrying both words must land on the operations side; on
    the storage side it would both understate ops and inflate the denominator."""
    rows = [
        _row(service="Cloud Storage", sku="Standard Storage Operations Class A", cost=90.0),
        _row(service="Cloud Storage", sku="Standard Storage US", cost=100.0),
    ]
    out = find_gcs_request_overhead(rows)
    assert len(out) == 1
    assert out[0].metadata["operations_fee_usd"] == pytest.approx(90.0)
    assert out[0].metadata["storage_fee_usd"] == pytest.approx(100.0)
