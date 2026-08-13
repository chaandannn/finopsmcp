# SPDX-License-Identifier: Apache-2.0
"""The CSV export, driven through the real tool.

This file used to test a copy of the tool instead of the tool. Its helper said so
in its own docstring: "Replicate the CSV-writing logic from export_cost_report_csv
so we can test the output format without needing a live AWS connection." Every
test called that helper, so the file asserted that a fixture it had just written
matched what it had just written, and one test wrote a two-row CSV inline and
asserted the file existed, which tests `open`.

Meanwhile the tool itself began with an import of
`scan_spot_adoption_opportunities`, a function that has never existed anywhere in
the package, and raised ImportError on every call it ever received. Seven green
tests, one dead tool, no contradiction, because the two had nothing to do with
each other.

The lesson is not "mock less". It is that a test which re-implements its subject
cannot fail for any reason the subject can fail for. So every test here calls
`export_cost_report_csv`. AWS is replaced at `sweep.build_specs`, the boundary
where our code hands off to boto3, and every line above it is the real one.
"""
from __future__ import annotations

import asyncio
import csv
import pathlib
from datetime import date

import pytest

import finops.server as _srv
from finops.recommendations import sweep as S
from finops.tools import notifications as N

EXPORT = getattr(N.export_cost_report_csv, "fn", N.export_cost_report_csv)


class _FakeAWS:
    async def is_configured(self):
        return True

    def _client(self, name):
        raise RuntimeError("no AWS in tests")


@pytest.fixture(autouse=True)
def aws(monkeypatch):
    monkeypatch.setitem(_srv.CLOUD_CONNECTORS, "aws", _FakeAWS())


def _findings(*rows):
    """Install a scanner table that yields exactly `rows` as ipv4-style findings.

    ipv4 is used as the carrier because its normaliser produces an aggregate
    finding with no resource_id, so nothing collapses and the rows arrive in the
    export exactly as written. Collapse has its own tests elsewhere.
    """
    return rows


@pytest.fixture
def three_findings(monkeypatch):
    """Three findings of descending value, via three separate scanners."""
    monkeypatch.setattr(S, "build_specs", lambda a, r: [
        ("graviton", lambda **k: [{
            "instance_id": "i-abc", "instance_type": "m5.large",
            "graviton_equivalent": "m7g.large", "savings_estimate": 120.0,
            "savings_pct": 0.2, "region": "us-east-1"}], {}),
        ("ipv4", lambda **k: {
            "total_monthly_waste": 10.8, "unattached_eips": [1, 2, 3]}, {}),
        ("s3_bucket_keys", lambda **k: [{
            "bucket_name": "my-bucket", "estimated_savings": 5.0}], {}),
    ])


def _rows(dest: pathlib.Path) -> list[list[str]]:
    with open(dest, newline="", encoding="utf-8") as fh:
        return list(csv.reader(fh))


def _data_rows(rows):
    head = next(i for i, r in enumerate(rows) if r and r[0] == "Rank")
    return [r for r in rows[head + 1:] if r]


# ── structure ─────────────────────────────────────────────────────────────────

def test_the_tool_writes_the_column_headers(three_findings, tmp_path):
    dest = tmp_path / "r.csv"
    asyncio.run(EXPORT(output_path=str(dest)))
    rows = _rows(dest)
    header = next((r for r in rows if r and r[0] == "Rank"), None)
    assert header == ["Rank", "Opportunity", "Category",
                      "Monthly Saving ($)", "Annual Saving ($)", "Detail"]


def test_the_tool_writes_the_summary_block(three_findings, tmp_path):
    dest = tmp_path / "r.csv"
    asyncio.run(EXPORT(output_path=str(dest)))
    rows = _rows(dest)
    assert rows[0][0] == "nable Cost Report"
    labels = [r[0] for r in rows if r]
    for expected in ("Scan timestamp", "AWS account", "Total monthly saving",
                     "Total annual saving", "Opportunities found"):
        assert expected in labels


def test_every_finding_reaches_the_file(three_findings, tmp_path):
    dest = tmp_path / "r.csv"
    asyncio.run(EXPORT(output_path=str(dest)))
    assert len(_data_rows(_rows(dest))) == 3


def test_rows_are_ordered_by_monthly_saving(three_findings, tmp_path):
    dest = tmp_path / "r.csv"
    asyncio.run(EXPORT(output_path=str(dest)))
    values = [float(r[3]) for r in _data_rows(_rows(dest))]
    assert values == sorted(values, reverse=True)


def test_annual_is_twelve_times_monthly(three_findings, tmp_path):
    dest = tmp_path / "r.csv"
    asyncio.run(EXPORT(output_path=str(dest)))
    for row in _data_rows(_rows(dest)):
        assert float(row[4]) == pytest.approx(float(row[3]) * 12, abs=0.01)


def test_the_summary_total_matches_the_rows(three_findings, tmp_path):
    """The headline and the table must agree, or one of them is lying."""
    dest = tmp_path / "r.csv"
    asyncio.run(EXPORT(output_path=str(dest)))
    rows = _rows(dest)
    stated = next(r for r in rows if r and r[0] == "Total monthly saving")[1]
    summed = sum(float(r[3]) for r in _data_rows(rows))
    assert stated == f"${summed:,.2f}"


# ── paths ─────────────────────────────────────────────────────────────────────

def test_it_writes_where_it_was_told(three_findings, tmp_path):
    dest = tmp_path / "reports" / "custom.csv"
    dest.parent.mkdir(parents=True)
    out = asyncio.run(EXPORT(output_path=str(dest)))
    assert dest.exists() and dest.stat().st_size > 0
    assert str(dest) in out


def test_it_creates_a_missing_parent_directory(three_findings, tmp_path):
    dest = tmp_path / "does" / "not" / "exist" / "r.csv"
    asyncio.run(EXPORT(output_path=str(dest)))
    assert dest.exists()


def test_the_default_filename_carries_todays_date(three_findings, tmp_path, monkeypatch):
    """Default is ~/Downloads/nable-report-YYYY-MM-DD.csv.

    The old test asserted this by constructing the expected path and comparing it
    to itself, which holds for any implementation including none.
    """
    monkeypatch.setattr(pathlib.Path, "home", classmethod(lambda cls: tmp_path))
    (tmp_path / "Downloads").mkdir()
    out = asyncio.run(EXPORT())
    expected = tmp_path / "Downloads" / f"nable-report-{date.today().isoformat()}.csv"
    assert expected.exists(), out


# ── edge cases ────────────────────────────────────────────────────────────────

def test_zero_findings_still_produces_a_readable_report(monkeypatch, tmp_path):
    monkeypatch.setattr(S, "build_specs", lambda a, r: [("ipv4", lambda **k: {}, {})])
    dest = tmp_path / "r.csv"
    asyncio.run(EXPORT(output_path=str(dest)))
    rows = _rows(dest)
    assert rows[0][0] == "nable Cost Report"
    assert next(r for r in rows if r and r[0] == "Opportunities found")[1] == "0"


def test_no_aws_connected_is_a_message_not_a_crash(monkeypatch, tmp_path):
    class _Unconfigured:
        async def is_configured(self):
            return False

    monkeypatch.setitem(_srv.CLOUD_CONNECTORS, "aws", _Unconfigured())
    out = asyncio.run(EXPORT(output_path=str(tmp_path / "r.csv")))
    assert "not connected" in out.lower()


def test_a_leading_formula_in_a_cell_is_neutralised(monkeypatch, tmp_path):
    """CWE-1236: finance opens this file in Excel.

    Injected at `normalise` rather than at a scanner, because Excel only treats a
    cell as a formula when the trigger is the FIRST character, and today every
    normaliser happens to prefix a verb ("Enable S3 Bucket Key on ..."), so no
    scanner can currently produce one. That is a property of the wording, not a
    guarantee, and it would evaporate the day someone writes a normaliser that
    leads with the resource name. The writer's guard is what has to hold, so the
    writer is what is tested.
    """
    monkeypatch.setattr(S, "build_specs", lambda a, r: [("ipv4", lambda **k: {}, {})])
    monkeypatch.setattr(S, "normalise", lambda name, data: [{
        "title": "=cmd|' /c calc'!A1", "monthly_savings": 5.0,
        "category": "@SUM(1+1)", "detail": "-2+3"}])
    dest = tmp_path / "r.csv"
    asyncio.run(EXPORT(output_path=str(dest)))
    row = _data_rows(_rows(dest))[0]
    for cell in (row[1], row[2], row[5]):
        assert cell.startswith("'"), f"formula reached the sheet unescaped: {cell!r}"
