# SPDX-License-Identifier: Apache-2.0
"""The audit and both exports run one sweep, and every one of them runs at all.

Why this file exists, stated plainly: the scan that runs 21 scanners and turns
their output into findings was written down three times. run_full_cost_audit had
the maintained copy; export_cost_report_csv and publish_cost_report_to_notion had
forks of an older one. The forks had drifted four separate ways, and every one of
the four was invisible to a green test suite:

  1. Both imported `scan_spot_adoption_opportunities`, which has never existed,
     so both tools raised ImportError on every call. Dead, not degraded.
  2. Both called that scanner as `f(aws_client=aws, regions=regions)`; the real
     function takes `regions` only. Fixing the name alone buys a TypeError.
  3. Both gathered the scanners as bare coroutines on the running event loop, so
     they ran back-to-back and blocked it. The audit had already moved to threads
     with a deadline. The forks never got either.
  4. Neither attached `resource_id`, so neither could collapse mutually exclusive
     fixes on one resource. The 141-156% overstatement that was fixed in the audit
     was still live in both exports.

The old test file for the CSV tool, test_csv_export.py, had a helper whose own
docstring said it would "replicate the CSV-writing logic from
export_cost_report_csv". It re-implemented the code under test and asserted on the
re-implementation, so seven tests passed against a tool that could not run.

So: these tests drive the real tool functions. The scanner table is replaced at
`build_specs`, which is the boundary between our code and boto3, and everything
above it is the real path.
"""
from __future__ import annotations

import asyncio
import csv
import pathlib

import pytest

import finops.server as _srv
from finops.recommendations import sweep as S
from finops.tools import cost_queries as cq
from finops.tools import notifications as N


def _unwrap(tool):
    """MCP registration wraps the coroutine; the callable is on `.fn`."""
    return getattr(tool, "fn", tool)


class _FakeAWS:
    async def is_configured(self):
        return True

    def _client(self, name):
        raise RuntimeError("no AWS in tests")

    async def list_accounts(self):
        return []


@pytest.fixture
def aws(monkeypatch):
    monkeypatch.setitem(_srv.CLOUD_CONNECTORS, "aws", _FakeAWS())
    return _srv.CLOUD_CONNECTORS["aws"]


@pytest.fixture
def scanners(monkeypatch):
    """Replace the scanner table. Default: one Graviton and one Spot fix on the
    SAME instance (mutually exclusive), plus one aggregate finding that cannot
    collide with anything."""
    state = {"calls": 0}

    def _specs(aws_client, regions):
        state["calls"] += 1
        return [
            ("graviton", lambda **k: [{
                "instance_id": "i-1", "instance_type": "m5.large",
                "graviton_equivalent": "m7g.large", "savings_estimate": 40.0,
                "savings_pct": 0.2, "region": "us-east-1"}], {}),
            ("spot", lambda **k: [{
                "instance_id": "i-1", "instance_type": "m5.large",
                "monthly_savings": 70.0, "savings_pct": 0.7,
                "recommendation": "RECOMMENDED"}], {}),
            ("ipv4", lambda **k: {
                "total_monthly_waste": 10.8, "unattached_eips": [1, 2, 3]}, {}),
        ]

    monkeypatch.setattr(S, "build_specs", _specs)
    return state


# ── the tools run at all ──────────────────────────────────────────────────────

def test_export_cost_report_csv_runs(aws, scanners, tmp_path):
    """The bug, at its narrowest: this call used to raise ImportError, always."""
    dest = tmp_path / "report.csv"
    out = asyncio.run(_unwrap(N.export_cost_report_csv)(output_path=str(dest)))
    assert "Exported" in out, out
    assert dest.exists()


def test_publish_cost_report_to_notion_runs(aws, scanners, monkeypatch):
    """Same defect, same import, second tool."""
    class _Notion:
        async def is_configured(self):
            return True

        async def write_cost_report(self, report):
            _Notion.seen = report
            return "https://notion.so/fake-page"

    import finops.connectors.saas.notion as notion_mod
    monkeypatch.setattr(notion_mod, "NotionConnector", _Notion)

    out = asyncio.run(_unwrap(N.publish_cost_report_to_notion)())
    assert "published to Notion" in out, out
    assert _Notion.seen["findings"], "published a report with no findings in it"


def test_the_scanner_stub_actually_drives_the_tools(aws, scanners, tmp_path):
    """Guards this file against the failure it exists to describe.

    If the fixture stopped being reached, every tool above would return "no
    savings found" and still read as success. So: the sweep must have been asked
    for its scanner table, and the file must contain the finding it produced.
    """
    dest = tmp_path / "report.csv"
    asyncio.run(_unwrap(N.export_cost_report_csv)(output_path=str(dest)))
    assert scanners["calls"] > 0, "the tool never asked for the scanner table"
    assert "i-1" in dest.read_text(), "the stubbed finding never reached the file"


# ── one sweep, not three ──────────────────────────────────────────────────────

def test_all_three_tools_go_through_the_same_sweep(aws, scanners, tmp_path, monkeypatch):
    """The architectural claim, asserted rather than asserted-about.

    Each tool is run with `sweep` replaced by a counter. If any of them grows its
    own scanner block again, its counter stays at zero and this fails, which is
    the only thing that stops the fork from coming back.
    """
    class _Notion:
        async def is_configured(self):
            return True

        async def write_cost_report(self, report):
            return "https://notion.so/fake-page"

    import finops.connectors.saas.notion as notion_mod
    monkeypatch.setattr(notion_mod, "NotionConnector", _Notion)

    seen = []
    real = S.sweep

    async def _counting(aws_client, regions=None, **kw):
        seen.append(True)
        return await real(aws_client, regions, **kw)

    monkeypatch.setattr(S, "sweep", _counting)

    for label, run in (
        ("audit", lambda: _unwrap(cq.run_full_cost_audit)()),
        ("csv", lambda: _unwrap(N.export_cost_report_csv)(
            output_path=str(tmp_path / "r.csv"))),
        ("notion", lambda: _unwrap(N.publish_cost_report_to_notion)()),
    ):
        before = len(seen)
        asyncio.run(run())
        assert len(seen) == before + 1, (
            f"{label} did not call the shared sweep, so it is scanning on its own "
            f"again and will drift away from the other two exactly as before")


def test_only_the_sweep_imports_the_scanners_in_bulk():
    """A second module reaching for the whole scanner set is the fork returning.

    Counted per FUNCTION, not per module. tools/aws_waste.py imports 15 of these
    across 15 separate single-scanner tools, which is correct and must stay
    legal. One function reaching for most of the set at once is only ever one
    thing, and it is the thing that just cost two tools their entire working
    lives.
    """
    import ast

    import finops

    root = pathlib.Path(finops.__file__).parent
    scanner_tails = {
        fn.__module__.rsplit(".", 1)[-1] for _, fn, _ in S.build_specs(object(), None)}
    assert len(scanner_tails) >= 15, "the spec table stopped resolving"

    offenders = {}
    for path in root.rglob("*.py"):
        try:
            tree = ast.parse(path.read_text())
        except SyntaxError:  # pragma: no cover
            continue
        for fn in ast.walk(tree):
            if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            hits = {n.module.rsplit(".", 1)[-1]
                    for n in ast.walk(fn)
                    if isinstance(n, ast.ImportFrom) and n.module
                    and n.module.rsplit(".", 1)[-1] in scanner_tails}
            if len(hits) >= 5:
                offenders[f"{path.relative_to(root)}::{fn.name}"] = len(hits)

    assert offenders == {"recommendations/sweep.py::build_specs": len(scanner_tails)}, (
        f"exactly one function may import the scanner set: sweep.build_specs. "
        f"These reach for it in bulk, which is what a forked copy of the sweep "
        f"looks like: {offenders}")


# ── the double count, at the export boundary ──────────────────────────────────

def test_the_csv_total_does_not_sum_mutually_exclusive_fixes(aws, scanners, tmp_path):
    """i-1 can be migrated to Graviton ($40) or converted to Spot ($70), not both.

    Raw, that is $110 on one instance plus $10.80 of Elastic IPs. The exports had
    no resource_id and no collapse, so this is the number they would have printed
    had they been able to run at all.
    """
    dest = tmp_path / "report.csv"
    out = asyncio.run(_unwrap(N.export_cost_report_csv)(output_path=str(dest)))

    rows = list(csv.reader(dest.open()))
    total_row = next(r for r in rows if r and r[0] == "Total monthly saving")
    assert total_row[1] == "$80.80", (
        f"the CSV reports {total_row[1]} as the monthly saving. $80.80 is the "
        f"best fix on i-1 ($70) plus the Elastic IPs ($10.80); anything higher is "
        f"counting two fixes on one instance that you can only pick one of.")
    assert "1 alternative fix" in out, "the folded-away fix must be reported, not hidden"


def test_the_notion_total_does_not_sum_mutually_exclusive_fixes(aws, scanners, monkeypatch):
    class _Notion:
        async def is_configured(self):
            return True

        async def write_cost_report(self, report):
            _Notion.seen = report
            return "https://notion.so/fake-page"

    import finops.connectors.saas.notion as notion_mod
    monkeypatch.setattr(notion_mod, "NotionConnector", _Notion)

    asyncio.run(_unwrap(N.publish_cost_report_to_notion)())
    assert _Notion.seen["total_monthly_savings"] == pytest.approx(80.80), (
        f"published {_Notion.seen['total_monthly_savings']} to a page the customer's "
        f"team will read; the defensible figure is 80.80")


def test_two_different_instances_still_add_up(aws, monkeypatch, tmp_path):
    """Collapsing everything would be the opposite bug and just as wrong."""
    monkeypatch.setattr(S, "build_specs", lambda a, r: [
        ("spot", lambda **k: [
            {"instance_id": "i-1", "instance_type": "m5.large", "monthly_savings": 70.0,
             "savings_pct": 0.7, "recommendation": "RECOMMENDED"},
            {"instance_id": "i-2", "instance_type": "m5.large", "monthly_savings": 30.0,
             "savings_pct": 0.3, "recommendation": "RECOMMENDED"},
        ], {}),
    ])
    dest = tmp_path / "r.csv"
    asyncio.run(_unwrap(N.export_cost_report_csv)(output_path=str(dest)))
    rows = list(csv.reader(dest.open()))
    total = next(r for r in rows if r and r[0] == "Total monthly saving")
    assert total[1] == "$100.00"


# ── a retracted claim must not crash the writer ───────────────────────────────

def test_a_retracted_claim_does_not_crash_the_csv(aws, monkeypatch, tmp_path):
    """The critique sets monthly_savings to None on a claim it could not stand up.

    Both exports read `f["monthly_savings"]` by direct subscript and passed it to
    round(), which raises on None. They never met one only because they could not
    run; wiring them to the real sweep put a critique in front of them.
    """
    monkeypatch.setattr(S, "build_specs", lambda a, r: [
        ("spot", lambda **k: [{"instance_id": "i-9", "instance_type": "m5.large",
                               "monthly_savings": 70.0, "savings_pct": 0.7,
                               "recommendation": "RECOMMENDED"}], {}),
    ])
    monkeypatch.setattr(
        "finops.recommendations.critique.critique",
        lambda findings, **kw: [{**f, "monthly_savings": None, "magnitude": "small"}
                                for f in findings])

    dest = tmp_path / "r.csv"
    out = asyncio.run(_unwrap(N.export_cost_report_csv)(output_path=str(dest)))
    assert "Exported" in out
    body = dest.read_text()
    assert "needs confirming" in body, (
        "a retracted claim was written as a number; the cell must say the figure "
        "did not survive review rather than print 0.00, which reads as 'we checked "
        "and it saves nothing'")


# ── the sweep's own contract ──────────────────────────────────────────────────

def test_the_sweep_runs_scanners_off_the_event_loop(aws, monkeypatch):
    """Blocking boto3 calls must not run on the caller's loop.

    The forks gathered bare coroutines, so 21 scanners ran back-to-back on the
    main loop and the tool took the SUM of their times while blocking everything
    else. Asserted by having a scanner record its thread.
    """
    import threading

    main = threading.get_ident()
    seen = {}

    def _slow(**kw):
        seen["thread"] = threading.get_ident()
        return []

    monkeypatch.setattr(S, "build_specs", lambda a, r: [("graviton", _slow, {})])
    asyncio.run(S.sweep(aws))
    assert seen["thread"] != main, (
        "the scanner ran on the event loop thread, so a slow region blocks every "
        "other request in the process")


def test_the_sweep_has_a_deadline(aws, monkeypatch):
    """One stuck region must not hang the caller indefinitely."""
    def _hang(**kw):
        import time
        time.sleep(5)
        return []

    monkeypatch.setattr(S, "build_specs", lambda a, r: [("graviton", _hang, {})])
    result = asyncio.run(S.sweep(aws, deadline_s=1))
    assert result.timed_out is True
    assert result.findings == []


def test_a_scanner_that_raises_does_not_kill_the_sweep(aws, monkeypatch):
    """21 scanners, one account: one failure must cost one scanner's findings."""
    def _boom(**kw):
        raise RuntimeError("AccessDenied")

    monkeypatch.setattr(S, "build_specs", lambda a, r: [
        ("graviton", _boom, {}),
        ("ipv4", lambda **k: {"total_monthly_waste": 10.8, "unattached_eips": [1, 2, 3]}, {}),
    ])
    result = asyncio.run(S.sweep(aws))
    assert "graviton" in result.errors
    assert len(result.findings) == 1


def test_the_spot_scanner_is_called_with_the_arguments_it_accepts():
    """Defect 2, pinned on its own.

    The forks passed `aws_client=` to a function that takes only `regions`. A test
    that only checks the import resolves would not have caught it, and the tool
    would have swapped ImportError for TypeError at the same point in the call.
    """
    import inspect

    specs = S.build_specs(object(), ["us-east-1"])
    for name, fn, kwargs in specs:
        sig = inspect.signature(fn)
        if any(p.kind is inspect.Parameter.VAR_KEYWORD for p in sig.parameters.values()):
            continue
        unknown = set(kwargs) - set(sig.parameters)
        assert not unknown, (
            f"the sweep calls the {name!r} scanner ({fn.__module__}.{fn.__name__}) "
            f"with {sorted(unknown)}, which it does not accept")
