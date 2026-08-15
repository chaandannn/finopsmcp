# SPDX-License-Identifier: Apache-2.0
"""Reading the bill from S3 instead of paying to be told what it says.

The load-bearing tests here are not "it parses parquet". They are:

  1. Ingesting the CUR over a day Cost Explorer already wrote does not DOUBLE
     that day. This is the bug the design exists to prevent, and it is invisible
     to any test that only ever writes from one source.
  2. A second run with nothing new downloads nothing. Without this the reader is
     cheap once and expensive forever, which defeats the point.
  3. The total includes tax, credits and refunds. Summing only Usage rows
     produces a number smaller than the invoice, in the customer's favour, which
     is the kind of wrong nobody reports and everybody eventually trusts.
  4. A CUR that has not been delivered reads as absent, not as zero spend.

Real parquet throughout, written by duckdb, because the point of this module is
that it reads AWS's files correctly and a mocked reader would only prove that
the mock agrees with itself.
"""
from __future__ import annotations

import os
import shutil
from datetime import date, timedelta

import pytest

duckdb = pytest.importorskip("duckdb", reason="the [cur] extra provides the parquet reader")

from finops.connectors import cur_s3
from finops.storage import db
from finops.storage.snapshots import store_snapshot


# ── a fake S3 that behaves like the real one where it matters ────────────────

class FakeS3:
    """Backed by a directory. Counts calls, because cost is the subject here."""

    def __init__(self, root: str):
        self.root = root
        self.list_calls = 0
        self.download_calls = 0
        self.etags: dict[str, str] = {}

    def _abs(self, key: str) -> str:
        return os.path.join(self.root, key)

    def put(self, key: str, local_path: str, etag: str = "v1") -> None:
        dest = self._abs(key)
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        shutil.copyfile(local_path, dest)
        self.etags[key] = etag

    def list_objects_v2(self, **kw):
        self.list_calls += 1
        prefix = kw.get("Prefix", "")
        contents = []
        for key, etag in sorted(self.etags.items()):
            if key.startswith(prefix):
                contents.append({
                    "Key": key, "ETag": f'"{etag}"',
                    "Size": os.path.getsize(self._abs(key)),
                })
        return {"Contents": contents, "IsTruncated": False}

    def download_file(self, bucket: str, key: str, dest: str):
        self.download_calls += 1
        shutil.copyfile(self._abs(key), dest)


def _write_parquet(path: str, rows: list[dict], *, snake: bool = True) -> None:
    """Write a CUR-shaped parquet file with real AWS column names."""
    cols = ({
        "d": "line_item_usage_start_date", "acct": "line_item_usage_account_id",
        "svc": "product_product_name", "reg": "product_region",
        "cost": "line_item_unblended_cost", "typ": "line_item_line_item_type",
    } if snake else {
        # The other spelling CUR has shipped. A reader that hard-codes one
        # silently finds nothing in the other and reports a small number.
        "d": "lineItem/UsageStartDate", "acct": "lineItem/UsageAccountId",
        "svc": "product/ProductName", "reg": "product/region",
        "cost": "lineItem/UnblendedCost", "typ": "lineItem/LineItemType",
    })
    values = ",\n".join(
        "(TIMESTAMP '{d} 00:00:00', '{a}', '{s}', '{r}', {c}, '{t}')".format(
            d=r["date"], a=r["account"], s=r["service"], r=r.get("region", "us-east-1"),
            c=r["cost"], t=r.get("type", "Usage"))
        for r in rows
    )
    select = ", ".join(f'col{i} AS "{name}"' for i, name in enumerate(cols.values()))
    con = duckdb.connect()
    try:
        con.execute(
            f"COPY (SELECT {select} FROM (VALUES {values}) "
            f"AS t(col0, col1, col2, col3, col4, col5)) "
            f"TO '{path}' (FORMAT PARQUET)")
    finally:
        con.close()


@pytest.fixture
def cur(tmp_path, monkeypatch):
    """A configured CUR with one month of parquet in a fake bucket."""
    monkeypatch.setenv("FINOPS_DB_PATH", str(tmp_path / "c.db"))
    monkeypatch.setenv("FINOPS_DATA_DIR", str(tmp_path / "d"))
    monkeypatch.setenv("CUR_S3_BUCKET", "cur-bucket")
    monkeypatch.setenv("CUR_ATHENA_TABLE", "nable-cur")
    monkeypatch.delenv("CUR_S3_PREFIX", raising=False)
    monkeypatch.delenv("CUR_S3_REGION", raising=False)
    db._ENGINE, db._DATA_DIR = None, None
    db.get_engine()

    s3 = FakeS3(str(tmp_path / "s3"))
    yield s3
    db._ENGINE, db._DATA_DIR = None, None


def _stage(s3, tmp_path, rows, *, day: date, snake=True, etag="v1", name="part-0"):
    """Put a parquet file into the fake bucket at the right Hive partition."""
    local = str(tmp_path / f"{name}.parquet")
    _write_parquet(local, rows, snake=snake)
    # month is NOT zero-padded in a CUR partition. Writing it padded here would
    # make the test agree with a bug rather than with AWS.
    key = (f"cur/nable-cur/nable-cur/year={day.year}/month={day.month}/"
           f"{name}.snappy.parquet")
    s3.put(key, local, etag=etag)
    return key


# ── the one that matters ─────────────────────────────────────────────────────

def test_ingesting_cur_over_a_cost_explorer_day_does_not_double_it(cur, tmp_path):
    """The whole reason replace_provider_day exists.

    Cost Explorer names the service "Amazon Elastic Compute Cloud - Compute".
    The CUR calls the same spend "Amazon Elastic Compute Cloud". cost_snapshots
    is keyed on the service name, so an upsert would not overwrite the CE row,
    it would land beside it and the day would read as twice the money.

    Mutation-checked: swapping replace_provider_day back to store_snapshot makes
    this the only test in the file that fails, and it fails with exactly the
    doubled figure.
    """
    day = date.today() - timedelta(days=1)
    store_snapshot(provider="aws", service="Amazon Elastic Compute Cloud - Compute",
                   account_id="111111111111", region="us-east-1",
                   snapshot_date=day, amount_usd=500.0)

    _stage(cur, tmp_path, [
        {"date": day.isoformat(), "account": "111111111111",
         "service": "Amazon Elastic Compute Cloud", "cost": 500.0},
    ], day=day)

    cur_s3.ingest_range(day, day, s3=cur)

    from sqlalchemy import func, select
    with db.get_engine().connect() as conn:
        total = conn.execute(
            select(func.sum(db.cost_snapshots.c.amount_usd)).where(
                db.cost_snapshots.c.snapshot_date == day.isoformat())).scalar()

    assert total == pytest.approx(500.0), (
        f"the day totals ${total:,.2f} after ingesting a $500 CUR over a $500 "
        f"Cost Explorer day; the two sources are being added instead of replaced"
    )


def test_a_second_read_with_nothing_new_downloads_nothing(cur, tmp_path):
    """Cheap once is not the claim. Cheap every night is the claim.

    CUR republishes the whole open month on every delivery, so a reader without
    change detection re-downloads the same month forever.
    """
    day = date.today() - timedelta(days=1)
    _stage(cur, tmp_path, [
        {"date": day.isoformat(), "account": "1", "service": "Amazon S3", "cost": 12.0},
    ], day=day)

    first = cur_s3.read_daily_costs(day, day, s3=cur)
    assert first.files_read == 1 and cur.download_calls == 1

    downloads_before = cur.download_calls
    second = cur_s3.read_daily_costs(day, day, s3=cur)

    assert cur.download_calls == downloads_before, (
        "the second read downloaded a file whose ETag had not changed")
    assert second.cost.files_skipped_unchanged == 1
    assert second.cost.get_requests == 0
    assert second.cost.usd < first.cost.usd


def test_a_changed_etag_is_read_again(cur, tmp_path):
    """The other half: skipping must not become never-updating.

    AWS restates recent days for a week. A reader that skips a file whose
    contents changed would serve the first, partial figure forever.
    """
    day = date.today() - timedelta(days=1)
    _stage(cur, tmp_path, [
        {"date": day.isoformat(), "account": "1", "service": "Amazon S3", "cost": 10.0},
    ], day=day, etag="v1")
    cur_s3.ingest_range(day, day, s3=cur)

    # AWS restates the day upward and republishes.
    _stage(cur, tmp_path, [
        {"date": day.isoformat(), "account": "1", "service": "Amazon S3", "cost": 25.0},
    ], day=day, etag="v2")
    out = cur_s3.ingest_range(day, day, s3=cur)

    assert out["files_read"] == 1, "a restated file was skipped as unchanged"

    from sqlalchemy import func, select
    with db.get_engine().connect() as conn:
        total = conn.execute(
            select(func.sum(db.cost_snapshots.c.amount_usd)).where(
                db.cost_snapshots.c.snapshot_date == day.isoformat())).scalar()
    assert total == pytest.approx(25.0), (
        f"restated day reads as ${total}, not the corrected $25")


def test_the_total_includes_tax_and_credits_not_just_usage(cur, tmp_path):
    """Sum the invoice, not the interesting part of it.

    connectors/cur.py filters to Usage-shaped line items because it answers
    questions about RESOURCES. A snapshot of what the bill WAS has to include
    Tax, Credit and Refund lines, exactly as Cost Explorer's UnblendedCost does.
    Filtering them out produces a number smaller than the invoice, in the
    customer's favour, which is the kind of wrong nobody reports.
    """
    day = date.today() - timedelta(days=1)
    _stage(cur, tmp_path, [
        {"date": day.isoformat(), "account": "1", "service": "Amazon EC2",
         "cost": 100.0, "type": "Usage"},
        {"date": day.isoformat(), "account": "1", "service": "Tax",
         "cost": 8.25, "type": "Tax"},
        {"date": day.isoformat(), "account": "1", "service": "Amazon EC2",
         "cost": -15.0, "type": "Credit"},
        {"date": day.isoformat(), "account": "1", "service": "Amazon EC2",
         "cost": 40.0, "type": "SavingsPlanCoveredUsage"},
    ], day=day)

    result = cur_s3.read_daily_costs(day, day, s3=cur)
    total = sum(r["amount_usd"] for r in result.rows)

    assert total == pytest.approx(133.25), (
        f"total is ${total}, not the ${133.25} on the invoice; some line item "
        f"type is being filtered out of a figure that should match the bill")


def test_no_cur_delivered_reads_as_absent_not_as_zero(cur):
    """A missing export is a setup state, not a finding of no spend.

    Same defect shape this repo has now fixed seven times: a failed read
    becoming a number, and always the one that looks like good news.
    """
    day = date.today() - timedelta(days=1)
    result = cur_s3.read_daily_costs(day, day, s3=cur)

    assert result.rows == []
    assert result.files_read == 0
    assert result.days_covered == set(), (
        "reported covering a day it never read, so a caller cannot tell "
        "'no data' from '$0'")


# ── schema robustness ────────────────────────────────────────────────────────

def test_both_cur_column_spellings_are_read(cur, tmp_path):
    """CUR has shipped `lineItem/UnblendedCost` and `line_item_unblended_cost`.

    A reader that knows one spelling does not fail loudly on the other, it finds
    no rows and reports a small number.
    """
    day = date.today() - timedelta(days=1)
    _stage(cur, tmp_path, [
        {"date": day.isoformat(), "account": "1", "service": "Amazon EC2", "cost": 77.0},
    ], day=day, snake=False)

    result = cur_s3.read_daily_costs(day, day, s3=cur)
    assert sum(r["amount_usd"] for r in result.rows) == pytest.approx(77.0), (
        "the slash-separated CUR column spelling read as no data")


def test_a_cur_without_a_cost_column_raises_instead_of_reading_as_zero(cur, tmp_path):
    """The failure has to be loud. $0.00 is a plausible bill."""
    day = date.today() - timedelta(days=1)
    local = str(tmp_path / "bad.parquet")
    con = duckdb.connect()
    try:
        con.execute(f"COPY (SELECT 1 AS nonsense) TO '{local}' (FORMAT PARQUET)")
    finally:
        con.close()
    cur.put(f"cur/nable-cur/nable-cur/year={day.year}/month={day.month}/x.parquet",
            local, etag="v1")

    with pytest.raises(cur_s3.CURReadError, match="missing columns"):
        cur_s3.read_daily_costs(day, day, s3=cur)


def test_the_hive_month_partition_is_not_zero_padded(cur, tmp_path):
    """`month=8`, not `month=08`. The single commonest reason a hand-built CUR
    path finds nothing at all."""
    day = date(date.today().year, 8, 14)
    _stage(cur, tmp_path, [
        {"date": day.isoformat(), "account": "1", "service": "Amazon EC2", "cost": 5.0},
    ], day=day)

    result = cur_s3.read_daily_costs(day, day, s3=cur)
    assert result.files_read == 1, "did not find an unpadded month partition"


# ── the claim, measured ──────────────────────────────────────────────────────

def test_what_the_read_cost_is_counted_and_is_a_fraction_of_a_cent(cur, tmp_path):
    """The product wants to say 'this added nothing to your bill'.

    That is only sayable if it is summed. Compared against Cost Explorer's
    per-request price, which is the meter this module exists to replace.
    """
    from finops.aws_prices import COST_EXPLORER_PER_REQUEST

    day = date.today() - timedelta(days=1)
    for i in range(3):
        _stage(cur, tmp_path, [
            {"date": day.isoformat(), "account": "1", "service": "Amazon EC2",
             "cost": 10.0},
        ], day=day, name=f"part-{i}")

    result = cur_s3.read_daily_costs(day, day, s3=cur)

    assert result.cost.get_requests == 3
    assert result.cost.list_requests >= 1
    assert result.cost.bytes_downloaded > 0
    assert result.cost.usd < COST_EXPLORER_PER_REQUEST, (
        f"reading the whole bill cost ${result.cost.usd:.6f}, which is not less "
        f"than the ${COST_EXPLORER_PER_REQUEST} a single Cost Explorer request "
        f"costs; the cheaper path is not cheaper")

    d = result.cost.as_dict()
    assert d["usd"] > 0, "reported a free read, which is not true and not the claim"


def test_egress_is_only_free_when_we_know_it_is_free(cur, tmp_path, monkeypatch):
    """An unmeasured cost must read as present, never as zero.

    S3 transfer is free in-region and $0.09/GB out. Assuming in-region is how a
    product ends up claiming a cost it never measured.
    """
    day = date.today() - timedelta(days=1)
    _stage(cur, tmp_path, [
        {"date": day.isoformat(), "account": "1", "service": "Amazon EC2", "cost": 1.0},
    ], day=day)

    monkeypatch.delenv("AWS_REGION", raising=False)
    monkeypatch.delenv("AWS_DEFAULT_REGION", raising=False)
    unknown = cur_s3.read_daily_costs(day, day, s3=cur, force=True)
    assert unknown.cost.same_region is False, (
        "assumed same-region egress with no evidence, which understates the bill")

    monkeypatch.setenv("CUR_S3_REGION", "us-east-2")
    monkeypatch.setenv("AWS_REGION", "us-east-2")
    known = cur_s3.read_daily_costs(day, day, s3=cur, force=True)
    assert known.cost.same_region is True
    assert known.cost.usd < unknown.cost.usd


# ── the operational trap ─────────────────────────────────────────────────────

def test_an_empty_window_re_reads_files_the_etag_check_would_skip(cur, tmp_path):
    """ETag skipping assumes 'not re-read' means 'already have it'.

    Restore the box from a snapshot, or lose cost_snapshots while keeping the
    ingest state, and that assumption is false: every run skips everything and
    the dashboard stays empty forever, with no error anywhere.
    """
    day = date.today() - timedelta(days=1)
    _stage(cur, tmp_path, [
        {"date": day.isoformat(), "account": "1", "service": "Amazon EC2", "cost": 42.0},
    ], day=day)
    cur_s3.ingest_recent(days=2, s3=cur)

    # The data goes away; the memory of having read it does not.
    with db.get_engine().begin() as conn:
        conn.execute(db.cost_snapshots.delete())

    out = cur_s3.ingest_recent(days=2, s3=cur)

    assert out.get("self_healed") is True, (
        "the ingest state and cost_snapshots disagreed and nothing noticed")
    assert out["rows_written"] > 0, "self-heal ran but wrote nothing"


# ── the wiring, driven rather than read ──────────────────────────────────────

def test_the_nightly_snapshot_uses_the_export_and_never_touches_cost_explorer(
        cur, tmp_path, monkeypatch):
    """The saving is skipping the connector, not having a cheaper option.

    A reader nothing calls is the defect shape this repo keeps finding, and a
    reader called ALONGSIDE the expensive path saves nothing at all: the bill
    gets read twice and one of the reads still bills the customer. So this drives
    the real _snapshot_all and asserts the AWS connector was never asked.

    Checked as a call, not as a string in the source. `"cur_s3" in source` stays
    true when the call becomes `pass`, because the import line still mentions it,
    and that exact false pass happened twice earlier in this session.
    """
    import asyncio

    from finops.connectors import aws as aws_mod
    from finops.scheduler import jobs

    asked = []

    class NeverCallMe:
        async def is_configured(self):
            asked.append("is_configured")
            return True

        async def get_costs(self, *a, **kw):
            asked.append("get_costs")
            raise AssertionError(
                "the AWS connector was called even though the billing export "
                "was readable; this is the Cost Explorer charge we set out to "
                "stop paying")

    monkeypatch.setattr(aws_mod, "AWSConnector", NeverCallMe)

    day = date.today() - timedelta(days=1)
    _stage(cur, tmp_path, [
        {"date": day.isoformat(), "account": "1", "service": "Amazon EC2", "cost": 31.0},
    ], day=day)

    # ingest_recent builds its own boto3 client when none is passed, which is
    # what production does; point it at the fake bucket instead of the network.
    monkeypatch.setattr(cur_s3, "ingest_recent",
                        lambda days=3, s3=None: cur_s3.ingest_range(
                            date.today() - timedelta(days=days), date.today(), s3=cur))

    results = asyncio.run(jobs._snapshot_all())

    assert "get_costs" not in asked, "the expensive path ran anyway"
    assert "aws" in results and "billing export" in results["aws"], (
        f"AWS was not recorded as coming from the export: {results.get('aws')!r}")

    from sqlalchemy import func, select
    with db.get_engine().connect() as conn:
        total = conn.execute(
            select(func.sum(db.cost_snapshots.c.amount_usd)).where(
                db.cost_snapshots.c.provider == "aws")).scalar()
    assert total == pytest.approx(31.0), "the export ran but wrote nothing"


def test_a_broken_export_falls_back_instead_of_killing_the_snapshot(
        cur, monkeypatch):
    """A CUR that cannot be read must not take the nightly run down with it.

    cost_snapshots is the source of truth and a lost day may never be restated,
    so the failure mode has to be 'use the path that works', not 'no data'.
    """
    import asyncio

    from finops.connectors import aws as aws_mod
    from finops.scheduler import jobs

    called = []

    class Fallback:
        async def is_configured(self):
            return True

        async def get_costs(self, *a, **kw):
            called.append("get_costs")

            class S:
                entries: list = []
                total_usd = 0.0
            return S()

    monkeypatch.setattr(aws_mod, "AWSConnector", Fallback)
    monkeypatch.setattr(cur_s3, "ingest_recent", lambda **kw: (_ for _ in ()).throw(
        cur_s3.CURReadError("bucket is on fire")))

    results = asyncio.run(jobs._snapshot_all())

    assert called == ["get_costs"], (
        "a broken billing export skipped AWS entirely instead of falling back")
    assert "error" not in str(results.get("aws", "")).lower()


def test_is_configured_needs_less_than_the_athena_path(monkeypatch):
    """Reading files needs a bucket and a report name. Nothing else.

    The Athena reader needs four variables: database, table, results bucket,
    workgroup. Every one of them is a setup step somebody can get wrong, and
    none of them is needed to read a file. Fewer required settings is the point,
    so this pins it rather than leaving it to drift back.
    """
    for k in ("CUR_ATHENA_DATABASE", "CUR_ATHENA_RESULTS_BUCKET",
              "CUR_ATHENA_WORKGROUP", "CUR_S3_REPORT_NAME"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("CUR_S3_BUCKET", "b")
    monkeypatch.setenv("CUR_ATHENA_TABLE", "r")

    assert cur_s3.is_configured() is True

    from finops.connectors import cur as cur_athena
    assert cur_athena.is_configured() is False, (
        "the Athena path claims to be configured without its own settings")
