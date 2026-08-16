# SPDX-License-Identifier: Apache-2.0
"""The hole in the unattended rule, and the four ways it must not widen.

The rule, unchanged: Cost Explorer bills $0.01 per request and is never called
on a timer. The exception, decided 2026-08-15: the S3-direct billing-export
reader ships in nable-enterprise, so an OPEN install has no free path to nightly
cost history, and a scheduler that silently collects nothing is a worse product
than one that spends a cent a night with the charge announced.

That exception is a fallback keyed on ABSENCE, which is the shape that produced
the `PASSWORD=off` auth bypass: a switch that opens a door, triggered by exactly
the state a broken deployment produces. So it is built to be narrow, and these
tests are what keep it narrow:

  1. A reader that is INSTALLED closes the door, even when it is failing. A
     hosted box whose CUR breaks must not quietly start billing the customer.
  2. NABLE_NO_COST_EXPLORER still wins, so a hosted box that hard-forbids CE
     gets no data loudly rather than a silent charge when its plugin fails.
  3. The charge is announced every time. The objection was never the cent, it
     was spending someone's money without telling them.
  4. Outside the fallback the rule is absolute, including in the same process.
"""
from __future__ import annotations

import ast
import inspect
import logging

import pytest

from finops import billing_access
from finops.billing_access import (
    BillingAccessError, billed_fallback, export_reader_available, unattended_context,
)


@pytest.fixture(autouse=True)
def _no_ce_ban(monkeypatch):
    monkeypatch.delenv("NABLE_NO_COST_EXPLORER", raising=False)


# ── the open install: the door opens ─────────────────────────────────────────

def test_an_open_install_has_no_export_reader(monkeypatch):
    """The premise. If this ever fails, the reader leaked back into the open
    package and the whole fallback is dead code."""
    monkeypatch.setattr(billing_access, "_EXPORT_READER", "finops.connectors.cur_s3")
    assert export_reader_available() is False, (
        "finops.connectors.cur_s3 is importable from the open package; it is "
        "supposed to ship only in nable-enterprise")


def test_the_nightly_snapshot_may_bill_when_nothing_free_exists(monkeypatch):
    """The decision, at the level it takes effect."""
    import boto3

    from finops.connectors.aws import AWSConnector

    monkeypatch.setenv("FINOPS_DEMO", "0")
    monkeypatch.setattr(boto3, "client", lambda name, **kw: object())

    with unattended_context():
        with pytest.raises(BillingAccessError):
            AWSConnector()._make_client()          # the rule, still on

        with billed_fallback("nightly aws snapshot") as opened:
            assert opened is True
            assert AWSConnector()._make_client() is not None


# ── the four ways it must not widen ──────────────────────────────────────────

def test_an_installed_reader_closes_the_door_even_when_it_is_failing(monkeypatch):
    """The one that matters.

    A hosted box has the reader. If its CUR breaks (bucket permissions changed,
    export deleted, a bad deploy), the fallback must NOT open, because a silent
    per-request charge is precisely what a broken deployment must not turn into.
    Keyed on the reader being PACKAGED, never on the ingest succeeding.
    """
    # Any importable module stands in for the enterprise reader: what is under
    # test is "present closes the door", not the reader itself.
    monkeypatch.setattr(billing_access, "_EXPORT_READER", "finops.billing_access")
    assert export_reader_available() is True

    with unattended_context():
        with billed_fallback("nightly aws snapshot") as opened:
            assert opened is False, (
                "the fallback opened on a box that HAS a reader; a broken CUR "
                "would start billing the customer with nobody watching")
            with pytest.raises(BillingAccessError):
                billing_access.ce_client()


def test_the_hard_ban_still_wins(monkeypatch):
    """NABLE_NO_COST_EXPLORER=1 is the guarantee an org can put in writing.

    A hosted box sets it, so a plugin that fails to load produces no data and a
    loud error instead of a quiet charge. The fallback must not override it.
    """
    monkeypatch.setenv("NABLE_NO_COST_EXPLORER", "1")
    with unattended_context():
        with billed_fallback("nightly aws snapshot") as opened:
            assert opened is False
            with pytest.raises(BillingAccessError, match="disabled here"):
                billing_access.ce_client()


def test_the_charge_is_announced_with_its_price(caplog, monkeypatch):
    """Spending the customer's money quietly is the actual objection."""
    monkeypatch.setattr(billing_access, "_EXPORT_READER", "finops.connectors.cur_s3")
    with caplog.at_level(logging.WARNING, logger="finops.billing_access"):
        with unattended_context():
            with billed_fallback("nightly aws snapshot"):
                pass

    text = caplog.text
    assert "0.01" in text, f"the price is not in the warning: {text!r}"
    assert "billing export" in text.lower(), "does not name the fix"
    assert "nightly aws snapshot" in text, "does not say what was billed for"


def test_the_rule_is_restored_the_moment_the_block_ends(monkeypatch):
    """A fallback that leaks is just the rule switched off."""
    import boto3

    from finops.connectors.aws import AWSConnector

    monkeypatch.setenv("FINOPS_DEMO", "0")
    monkeypatch.setattr(boto3, "client", lambda name, **kw: object())

    with unattended_context():
        with billed_fallback("nightly aws snapshot"):
            AWSConnector()._make_client()
        with pytest.raises(BillingAccessError):
            AWSConnector()._make_client()


def test_it_does_nothing_at_all_when_a_person_is_waiting(monkeypatch):
    """Interactive work was never blocked, so the fallback has nothing to open."""
    monkeypatch.setattr(billing_access, "_EXPORT_READER", "finops.connectors.cur_s3")
    with billed_fallback("interactive question") as opened:
        assert opened is False, (
            "reported opening a hole outside an unattended context, which would "
            "make the log warn about a charge that was always permitted")


# ── the seam, structurally ───────────────────────────────────────────────────

def test_the_open_scheduler_survives_the_reader_being_absent():
    """The import is expected to fail on an open install.

    If it were a bare import, every open install's nightly job would die on
    ImportError, which is how a plugin seam turns into a hard dependency.
    """
    from finops.scheduler import jobs

    src = inspect.getsource(jobs)
    tree = ast.parse(src)

    guarded = {"_snapshot_all": False, "job_snapshot": False}
    for node in ast.walk(tree):
        # AsyncFunctionDef too: _snapshot_all is `async def`, and matching only
        # FunctionDef silently skipped it, so this test passed on job_snapshot
        # alone and reported the pair as covered.
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        if node.name not in guarded:
            continue
        for handler in (h for n in ast.walk(node) if isinstance(n, ast.Try)
                        for h in n.handlers):
            names = ([handler.type.id] if isinstance(handler.type, ast.Name)
                     else [e.id for e in getattr(handler.type, "elts", [])
                           if isinstance(e, ast.Name)])
            if "ImportError" in names:
                guarded[node.name] = True

    assert all(guarded.values()), (
        f"these do not guard the enterprise import with ImportError: "
        f"{[k for k, v in guarded.items() if not v]}. On an open install the "
        f"nightly job would crash instead of degrading.")


def test_the_fallback_wraps_only_aws():
    """Only AWS is metered per request, so only AWS needs the hole.

    Wrapping the whole loop would hand every provider a permission it does not
    need, and the next metered connector would inherit it silently.
    """
    from finops.scheduler import jobs

    fn = next(n for n in ast.walk(ast.parse(inspect.getsource(jobs)))
              if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
              and n.name == "_snapshot_all")
    body = ast.unparse(fn)

    assert "billed_fallback" in body
    assert "nullcontext" in body, (
        "every provider goes through billed_fallback; it must be conditional on "
        "AWS, the only one billed per request")
    assert "name == 'aws'" in body or 'name == "aws"' in body


def test_no_open_module_imports_the_enterprise_reader_unguarded():
    """A bare import anywhere turns the optional seam into a hard dependency."""
    import pathlib

    pkg = pathlib.Path(billing_access.__file__).parent
    offenders: list[str] = []
    for path in sorted(pkg.rglob("*.py")):
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except SyntaxError:                    # pragma: no cover
            continue
        protected = {n for t in ast.walk(tree) if isinstance(t, ast.Try)
                     for n in ast.walk(t)}
        for node in ast.walk(tree):
            names: list[str] = []
            if isinstance(node, ast.Import):
                names = [a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom):
                names = [f"{node.module or ''}.{a.name}" for a in node.names]
            if any("cur_s3" in n or n.endswith("rollups") for n in names):
                if node not in protected:
                    offenders.append(f"{path.relative_to(pkg)}:{node.lineno}")

    assert not offenders, (
        "unguarded imports of enterprise-only modules:\n  " + "\n  ".join(offenders))
