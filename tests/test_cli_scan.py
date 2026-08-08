"""
`nable scan` unit tests: the four failure states with their exit codes, the
free-by-default recoverable headline (NO paid Cost Explorer call), the opt-in
`--spend` breakdown and its cost disclosure, the proud low-waste state,
--json schema, --demo labeling, and telemetry event classes.

All AWS calls are mocked; the engine is patched at the optimizer boundary.
"""

from __future__ import annotations

import json
import os
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from botocore.exceptions import ClientError, NoCredentialsError, SSOTokenLoadError

from finops import cli_scan

# Captured before the _mid_month fixture stubs it out, so the boundary tests
# below exercise the real window builder rather than the stub.
_REAL_SPEND_WINDOW = cli_scan._spend_window


@pytest.fixture(autouse=True)
def _isolate_aws_profile():
    # cli_scan.run() intentionally exports AWS_PROFILE for its own process;
    # tests must not leak that into the rest of the suite. monkeypatch cannot
    # help here: it only reverts its OWN changes, and the export happens inside
    # the code under test. Snapshot and restore around each test explicitly.
    before = os.environ.get("AWS_PROFILE")
    os.environ.pop("AWS_PROFILE", None)
    yield
    if before is None:
        os.environ.pop("AWS_PROFILE", None)
    else:
        os.environ["AWS_PROFILE"] = before


@pytest.fixture(autouse=True)
def _mid_month(monkeypatch):
    # Pin the Cost Explorer window so these tests assert on scan OUTPUT, not on
    # what day the CI runner happens to be. They used to read the real clock and
    # went red at midnight UTC on the 1st, which is also how the month-boundary
    # bug was found. The boundary itself is covered deliberately below, by
    # exercising _spend_window directly and by forcing the 1st through run().
    monkeypatch.setattr(
        cli_scan, "_spend_window", lambda today: ("2026-07-01", "2026-07-15", "month-to-date")
    )


@pytest.fixture(autouse=True)
def _aws_only_by_default(monkeypatch):
    # scan v2 is connection-aware: run() reads connected_families(). Keep the
    # existing AWS-only unit tests hermetic by defaulting to no extra providers,
    # so their output stays byte-identical to v1. The multi-provider tests
    # override this explicitly.
    monkeypatch.setattr("finops.tool_surface.connected_families", lambda: frozenset())


def _args(**kw):
    base = dict(json=False, demo=False, spend=False, debug=False, profile=None, regions=None)
    base.update(kw)
    return SimpleNamespace(**base)


def _client_error(code: str, op: str = "GetCallerIdentity") -> ClientError:
    return ClientError({"Error": {"Code": code, "Message": code}}, op)


def _ce_response(groups: dict[str, float]):
    return {
        "ResultsByTime": [
            {
                "Groups": [
                    {"Keys": [k], "Metrics": {"UnblendedCost": {"Amount": str(v)}}}
                    for k, v in groups.items()
                ]
            }
        ]
    }


def _session(
    creds=True,
    sts_exc=None,
    services=None,
    regions=None,
    ce_exc=None,
):
    session = MagicMock()
    session.get_credentials.return_value = object() if creds else None

    sts = MagicMock()
    if sts_exc:
        sts.get_caller_identity.side_effect = sts_exc
    else:
        sts.get_caller_identity.return_value = {"Account": "352112345678"}

    ce = MagicMock()
    if ce_exc:
        ce.get_cost_and_usage.side_effect = ce_exc
    else:
        ce.get_cost_and_usage.side_effect = [
            _ce_response(services or {"Amazon Bedrock": 19200.0, "Amazon EC2": 11400.0, "Amazon S3": 4100.0}),
            _ce_response(regions or {"us-east-1": 30000.0, "eu-west-1": 4700.0, "NoRegion": 12.0, "global": 3.0}),
        ]

    session.client.side_effect = lambda name, **kw: {"sts": sts, "ce": ce}[name]
    return session


def _report(findings=None, timed_out=None, scanned=None):
    findings = findings if findings is not None else [
        {
            "waste_type": "idle_nat_gateway",
            "description": "2 idle NAT gateways",
            "region": "us-east-1",
            "estimated_monthly_savings": 1200.0,
        },
        {
            "waste_type": "unattached_ebs",
            "description": "14 unattached EBS volumes",
            "region": "eu-west-1",
            "estimated_monthly_savings": 610.0,
        },
        {
            "waste_type": "tiny",
            "description": "one $3 bucket",
            "region": "us-east-1",
            "estimated_monthly_savings": 3.0,  # below the $25 floor: hidden
        },
    ]
    total = sum(f["estimated_monthly_savings"] for f in findings)
    return {
        "account_id": "352112345678",
        "regions_scanned": scanned if scanned is not None else ["us-east-1", "eu-west-1"],
        "regions_timed_out": timed_out or [],
        "total_findings": len(findings),
        "total_estimated_monthly_savings": total,
        "total_estimated_annual_savings": total * 12,
        "findings": findings,
        "errors": [],
    }


def _run(args, session, report=None, capsys=None, discovered=("us-east-1", "eu-west-1")):
    # The default (free) scan gets its regions from _discover_regions, not CE,
    # so patch it deterministically for every test. The --spend path calls it
    # too and then reorders by CE spend weight.
    events: list[tuple[str, dict]] = []
    with (
        patch.object(cli_scan, "_emit", side_effect=lambda e, p, wait: events.append((e, p))),
        patch("boto3.Session", return_value=session),
        patch("finops.analyzers.optimizer._discover_regions", return_value=list(discovered)),
        patch("finops.analyzers.optimizer.run_deep_audit", return_value=report or _report()) as engine,
    ):
        code = cli_scan.run(args)
    return code, events, engine


# ── happy path ────────────────────────────────────────────────────────────────

def test_happy_path_output_order_and_exit(capsys):
    code, events, _ = _run(_args(), _session())
    out = capsys.readouterr().out
    lines = [ln for ln in out.splitlines() if ln.strip()]

    assert code == cli_scan.EXIT_OK
    assert lines[0].startswith("nable scan")                       # first line, no network needed
    assert "account 352112345678 · this account only" in lines[1]  # scope always labeled
    # Free default: recoverable-led, NO paid spend headline.
    assert "on AWS this month" not in out
    assert "$1,813/mo recoverable" in out
    assert "nable scan --spend" in out          # points at the opt-in breakdown
    assert cli_scan.DOCS_LINE in out
    assert [e for e, _ in events] == ["cli_scan_started", "cli_scan_completed"]


def test_default_scan_makes_no_paid_ce_call():
    # The load-bearing promise: a free tool never bills the user's AWS account.
    session = _session()
    _run(_args(), session)
    ce = session.client("ce")  # resolve the mock without triggering a query
    ce.get_cost_and_usage.assert_not_called()


def test_aws_timeout_still_shows_other_providers(capsys, monkeypatch):
    # AWS hitting its budget must not blank the cross-provider frame: if other
    # providers are connected and gathered, show them (AWS degraded to a note).
    from finops import scan_assembler as sa
    monkeypatch.setattr("finops.tool_surface.connected_families",
                        lambda: frozenset({"aws", "llm"}))
    ai = sa.ProviderBlock(family="ai", label="AI & GPU", status="ok",
                          spend_usd=13300.0, estimated=True, detail="OpenAI $9.2k")
    monkeypatch.setattr("finops.scan_assembler.gather_extra_providers",
                        lambda fams, *, spend, **kw: ([ai], False))
    empty = _report(scanned=[])   # no regions finished (hit the time limit)
    code, events, _ = _run(_args(), _session(), report=empty)
    out = capsys.readouterr().out
    assert code == cli_scan.EXIT_OK                 # NOT EXIT_PARTIAL_EMPTY
    assert "AI & GPU" in out
    assert "showing your other providers" in out


def test_findings_ranked_and_floored(capsys):
    code, _, _ = _run(_args(), _session())
    out = capsys.readouterr().out
    assert "idle NAT gateways" in out
    assert "unattached EBS" in out
    assert "one $3 bucket" not in out  # below the $25/mo floor
    assert out.index("idle NAT") < out.index("unattached EBS")  # ranked by dollars


def test_proud_low_waste_state(capsys):
    tiny = _report(findings=[{
        "waste_type": "tiny", "description": "a $4 thing",
        "region": "us-east-1", "estimated_monthly_savings": 4.0,
    }])
    code, _, _ = _run(_args(), _session(), report=tiny)
    out = capsys.readouterr().out
    assert code == cli_scan.EXIT_OK
    assert "no material waste found, nice" in out
    assert "recoverable" not in out  # never an apologetic near-zero headline


def test_spend_flag_shows_headline_discloses_cost_and_weights_regions(capsys):
    # --spend is the ONLY path that calls Cost Explorer. It must: disclose the
    # cost up front, show the spend headline, and order regions by spend so a
    # budget cutoff does the valuable ones first.
    _, _, engine = _run(
        _args(spend=True), _session(), discovered=["eu-west-1", "us-east-1"]
    )
    out = capsys.readouterr().out
    assert "about $0.02" in out and "Cost Explorer" in out   # cost disclosed before the call
    assert "on AWS this month" in out and "Bedrock" in out    # spend headline present
    passed = engine.call_args.kwargs["regions"]
    assert passed[0] == "us-east-1"   # reordered: $30k spend outranks eu-west-1's $4.7k


def test_default_scan_never_charges(capsys):
    # The default may HINT that --spend costs ~$0.02, but it must never print
    # the pre-charge disclosure, because it makes no paid call itself.
    _run(_args(), _session())
    assert "on your AWS bill" not in capsys.readouterr().out


def test_partial_with_results_exits_zero_with_banner(capsys):
    rep = _report(timed_out=["ap-south-1"])
    code, _, _ = _run(_args(), _session(), report=rep)
    out = capsys.readouterr().out
    assert code == cli_scan.EXIT_OK
    assert "time limit" in out and "ap-south-1" in out


def test_partial_with_nothing_exits_five(capsys):
    rep = _report(findings=[], timed_out=["us-east-1", "eu-west-1"], scanned=[])
    code, events, _ = _run(_args(), _session(), report=rep)
    assert code == cli_scan.EXIT_PARTIAL_EMPTY
    assert ("cli_scan_failed", ) [0] in [e for e, _ in events]
    failed = [p for e, p in events if e == "cli_scan_failed"]
    assert failed and failed[0]["error_class"] == "timeout"
    assert cli_scan.DOCS_LINE in capsys.readouterr().out


def test_regions_override_skips_pick_and_validates():
    _, _, engine = _run(_args(regions=["eu-central-1"]), _session())
    assert engine.call_args.kwargs["regions"] == ["eu-central-1"]

    code, _, _ = _run(_args(regions=["not-a-region!"]), _session())
    assert code == 1


# ── failure states ────────────────────────────────────────────────────────────

def test_no_creds_points_at_connecting_never_at_sample_data(capsys):
    """Someone who just asked for their own bill does not want StreamCo's. The
    no-creds exit used to advertise `nable scan --demo`; every observed user who hit
    this wall abandoned. It now hands them the command that actually gets them
    connected."""
    code, events, _ = _run(_args(), _session(creds=False))
    out = capsys.readouterr().out
    assert code == cli_scan.EXIT_NO_CREDS
    assert "--demo" not in out                       # no fake-data consolation prize
    assert "aws configure" in out                    # the real fix
    assert "nable connect" in out                    # ...and the flow that waits for it
    assert cli_scan.DOCS_LINE in out
    assert [p["error_class"] for e, p in events if e == "cli_scan_failed"] == ["no-creds"]


def test_expired_sso_exit_three_prints_refresh_command(capsys):
    code, events, _ = _run(
        _args(profile="prod"),
        _session(sts_exc=SSOTokenLoadError(error_msg="token expired")),
    )
    out = capsys.readouterr().out
    assert code == cli_scan.EXIT_EXPIRED
    assert "aws sso login --profile prod" in out
    assert [p["error_class"] for e, p in events if e == "cli_scan_failed"] == ["expired"]


def test_expired_token_client_error_exit_three():
    code, _, _ = _run(_args(), _session(sts_exc=_client_error("ExpiredToken")))
    assert code == cli_scan.EXIT_EXPIRED


def test_access_denied_exit_four_points_at_the_exact_policy(capsys):
    """A denied identity needs the policy for the calls THIS scan makes, which
    --dry-run --json prints. The old generic iam-template was a superset."""
    code, _, _ = _run(_args(), _session(sts_exc=_client_error("AccessDenied")))
    assert code == cli_scan.EXIT_DENIED
    out = capsys.readouterr().out
    assert "--dry-run --json" in out
    assert "least-privilege" in out


def test_no_creds_error_from_sts_exit_six():
    code, _, _ = _run(_args(), _session(sts_exc=NoCredentialsError()))
    assert code == cli_scan.EXIT_NO_CREDS


def test_spend_flag_ce_denied_degrades_and_still_scans(capsys):
    # Under --spend, if CE is billing-locked, degrade to the free recoverable
    # headline rather than failing the whole scan.
    session = _session(ce_exc=_client_error("AccessDeniedException", "GetCostAndUsage"))
    code, _, engine = _run(_args(spend=True), session)
    out = capsys.readouterr().out
    assert code == cli_scan.EXIT_OK
    assert "spend summary unavailable" in out and "iam-template" in out
    assert "recoverable" in out           # recoverable-led headline instead
    assert engine.called                  # the scan still ran


# ── --json ────────────────────────────────────────────────────────────────────

def test_json_schema_and_stdout_purity(capsys):
    code, _, _ = _run(_args(json=True), _session())
    captured = capsys.readouterr()
    doc = json.loads(captured.out)  # stdout parses as a single document
    assert code == cli_scan.EXIT_OK
    assert doc["schema_version"] == 1
    assert doc["command"] == "scan" and doc["demo"] is False
    assert doc["account_id"] == "352112345678"
    assert doc["spend"] is None            # free default: no CE, no spend block
    assert doc["recoverable"]["monthly_usd"] == 1813.0
    assert doc["recoverable"]["pct_of_spend"] is None
    assert doc["scan"]["partial"] is False
    assert "nable scan" in captured.err   # chrome moved to stderr


def test_json_with_spend_flag_includes_breakdown(capsys):
    code, _, _ = _run(_args(json=True, spend=True), _session())
    doc = json.loads(capsys.readouterr().out)
    assert code == cli_scan.EXIT_OK
    assert doc["spend"]["top_services"][0]["service"] == "Amazon Bedrock"
    assert doc["recoverable"]["pct_of_spend"] is not None


# ── the month boundary ────────────────────────────────────────────────────────
#
# On the 1st, month-to-date is an empty window. The old code answered it with a
# fabricated zero, so one day in thirty nable told people their cloud bill was
# $0.00 with no services listed. That is worse than an error: it looks like an
# answer. These tests run every day of a year through the window builder, and
# drive the 1st all the way through run() so the plumbing is covered too.

def test_spend_window_never_asks_cost_explorer_for_an_empty_range():
    from datetime import date, timedelta

    day = date(2026, 1, 1)
    for _ in range(400):  # a full year plus change, crossing both boundaries
        start, end, covers = _REAL_SPEND_WINDOW(day)
        assert start < end, f"empty CE window on {day}: {start}..{end}"
        assert covers == ("last month" if day.day == 1 else "month-to-date")
        day += timedelta(days=1)


def test_spend_window_on_the_first_asks_for_the_month_that_just_closed():
    from datetime import date

    assert _REAL_SPEND_WINDOW(date(2026, 8, 1)) == ("2026-07-01", "2026-08-01", "last month")
    # Year rollover: January 1st must reach back into the previous year.
    assert _REAL_SPEND_WINDOW(date(2026, 1, 1)) == ("2025-12-01", "2026-01-01", "last month")


def test_spend_window_mid_month_is_month_to_date():
    from datetime import date

    assert _REAL_SPEND_WINDOW(date(2026, 8, 14)) == ("2026-08-01", "2026-08-14", "month-to-date")


def _force_first_of_month(monkeypatch):
    from datetime import date

    monkeypatch.setattr(
        cli_scan, "_spend_window", lambda today: _REAL_SPEND_WINDOW(date(2026, 8, 1))
    )


def test_first_of_month_scan_reports_a_real_bill_labelled_last_month(capsys, monkeypatch):
    _force_first_of_month(monkeypatch)
    code, _, _ = _run(_args(spend=True), _session())
    out = capsys.readouterr().out
    assert code == cli_scan.EXIT_OK
    # The bill is real and named, and it does NOT claim to be this month.
    assert "on AWS last month" in out and "Bedrock" in out
    assert "on AWS this month" not in out
    assert "$0.00 on AWS" not in out


def test_first_of_month_json_says_which_window_it_covers(capsys, monkeypatch):
    _force_first_of_month(monkeypatch)
    code, _, _ = _run(_args(json=True, spend=True), _session())
    doc = json.loads(capsys.readouterr().out)
    assert code == cli_scan.EXIT_OK
    assert doc["spend"]["covers"] == "last month"
    assert doc["spend"]["period"] == "2026-07-01 to 2026-08-01"
    assert doc["spend"]["top_services"][0]["service"] == "Amazon Bedrock"


# ── --demo ────────────────────────────────────────────────────────────────────

def test_demo_labels_and_event_flag(capsys):
    events: list[tuple[str, dict]] = []
    with patch.object(cli_scan, "_emit", side_effect=lambda e, p, wait: events.append((e, p))):
        code = cli_scan.run(_args(demo=True))
    out = capsys.readouterr().out
    assert code == cli_scan.EXIT_OK
    assert "(demo data)" in out
    assert "StreamCo" in out
    completed = [p for e, p in events if e == "cli_scan_completed"]
    assert completed and completed[0]["demo"] is True


def test_demo_needs_no_aws(capsys):
    # --demo must work on a machine with no boto3 credentials configured at all.
    with (
        patch.object(cli_scan, "_emit"),
        patch("boto3.Session", side_effect=AssertionError("must not touch AWS")),
    ):
        code = cli_scan.run(_args(demo=True))
    assert code == cli_scan.EXIT_OK


# ── deadline hard-exit (regression: live scan hung 45s past its deadline) ──────

def test_finish_returns_normally_without_lingering_threads():
    # The common case: threads drained, no abandonment flag, normal return.
    assert cli_scan._finish(cli_scan.EXIT_OK, lingering=False) == cli_scan.EXIT_OK


def test_finish_hard_exits_when_threads_abandoned():
    # When the deadline left live boto3 threads running, we must os._exit rather
    # than return, or interpreter shutdown joins them and the shell hangs.
    with patch("os._exit", side_effect=SystemExit(0)) as hard_exit:
        with pytest.raises(SystemExit):
            cli_scan._finish(cli_scan.EXIT_OK, lingering=True)
    hard_exit.assert_called_once_with(cli_scan.EXIT_OK)


def test_partial_scan_with_abandoned_threads_hard_exits(capsys):
    # End to end: a report carrying _threads_abandoned must drive the hard exit
    # AFTER rendering + completion telemetry, so nothing is lost.
    rep = _report(timed_out=["ap-south-1"])
    rep["_threads_abandoned"] = True
    with patch("os._exit", side_effect=SystemExit(0)) as hard_exit:
        with pytest.raises(SystemExit):
            _run(_args(), _session(), report=rep)
    hard_exit.assert_called_once_with(cli_scan.EXIT_OK)


# ── the boto3 preflight probe ────────────────────────────────────────────────

def _run_scan_with_broken_import(monkeypatch, *, exc, boto3_found):
    """Drive the real `nable scan` with a poisoned boto3 import.

    On 2026-08-05, 14 failures across 6 machines on current versions all landed
    on this probe reporting "boto3 is not installed" with an empty exc_type.
    boto3 imports fine on 3.12 and 3.14, so the message was almost certainly
    wrong and the telemetry carried nothing to prove it either way.
    """
    import builtins
    import importlib.util
    import types

    real_import = builtins.__import__

    def poisoned(name, *a, **k):
        if name == "boto3" or name.startswith("botocore"):
            raise exc
        return real_import(name, *a, **k)

    monkeypatch.setattr(builtins, "__import__", poisoned)
    monkeypatch.setattr(importlib.util, "find_spec",
                        lambda n: object() if boto3_found else None)

    emitted = []
    import finops.cli_scan as cs

    monkeypatch.setattr(cs, "_emit", lambda ev, props, wait=False: emitted.append((ev, props)))
    args = types.SimpleNamespace(json=False, demo=False, spend=False, debug=False,
                                 profile=None, regions=None)
    # the scan writes to the real stream; the caller reads it back with capsys
    return cs.run(args), emitted


def test_a_missing_boto3_says_missing_and_records_the_class(monkeypatch):
    code, emitted = _run_scan_with_broken_import(
        monkeypatch, exc=ModuleNotFoundError("No module named 'boto3'"),
        boto3_found=False)
    assert code == 1
    fails = [p for e, p in emitted if e == "cli_scan_failed"]
    assert fails, "no failure event emitted"
    assert fails[0]["error_class"] == "missing_dep"
    assert fails[0]["exc_type"] == "ModuleNotFoundError"


def test_an_installed_but_broken_boto3_is_not_reported_as_missing(monkeypatch, capsys):
    """The bug this replaces: telling somebody to reinstall a package that is
    already installed, and recording nothing that would reveal the real cause."""
    code, emitted = _run_scan_with_broken_import(
        monkeypatch, exc=ImportError("dynamic module does not define module export"),
        boto3_found=True)
    text = capsys.readouterr().out
    assert code == 1
    fails = [p for e, p in emitted if e == "cli_scan_failed"]
    assert fails[0]["error_class"] == "broken_dep"
    assert fails[0]["exc_type"] == "ImportError"
    assert "is not installed" not in text
    assert "will not import" in text


def test_a_non_import_error_during_the_probe_is_still_caught(monkeypatch):
    """An architecture mismatch surfaces as OSError or a bare Exception from the
    extension loader, not ImportError. The old handler let those escape as an
    unhandled traceback."""
    code, emitted = _run_scan_with_broken_import(
        monkeypatch, exc=OSError("incompatible architecture (have 'x86_64', need 'arm64')"),
        boto3_found=True)
    assert code == 1
    fails = [p for e, p in emitted if e == "cli_scan_failed"]
    assert fails[0]["exc_type"] == "OSError"
    assert fails[0]["error_class"] == "broken_dep"


def test_the_probe_never_leaks_the_exception_message(monkeypatch):
    """Messages carry paths, which carry usernames. Only the class name travels."""
    secret = "/Users/somebody/private/path/boto3.so"
    code, emitted = _run_scan_with_broken_import(
        monkeypatch, exc=ImportError(secret), boto3_found=True)
    fails = [p for e, p in emitted if e == "cli_scan_failed"]
    assert secret not in str(fails[0]), "the exception message reached telemetry"


def test_a_broken_import_stamps_the_dep_versions(monkeypatch):
    """The remote diagnosis: "ImportError" alone cannot distinguish a stale
    botocore from a missing urllib3. The four version strings can. Reproduced
    live: boto3 1.43.66 over botocore 1.34.0 dies with "cannot import name
    'DEFAULT_CHECKSUM_ALGORITHM'"; with these props the event names the skew."""
    import re

    code, emitted = _run_scan_with_broken_import(
        monkeypatch, exc=ImportError("cannot import name 'DEFAULT_CHECKSUM_ALGORITHM'"),
        boto3_found=True)
    assert code == 1
    fails = [p for e, p in emitted if e == "cli_scan_failed"]
    for pkg in ("boto3", "botocore", "s3transfer", "urllib3"):
        val = fails[0].get(f"{pkg}_version")
        assert val, f"{pkg}_version missing from the failure event"
        assert val == "absent" or re.match(r"^\d+\.", val), (pkg, val)
        assert "/" not in val, "a path leaked into a version field"


def test_a_broken_import_names_the_versions_to_the_user(monkeypatch, capsys):
    """The user sees the pair too, so "reinstall" stops being a guess."""
    _run_scan_with_broken_import(
        monkeypatch, exc=ImportError("cannot import name 'x'"), boto3_found=True)
    text = capsys.readouterr().out
    assert "found: boto3 " in text
    assert "uvx --python 3.12 nable scan" in text
