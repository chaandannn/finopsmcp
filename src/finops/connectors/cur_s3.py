# SPDX-License-Identifier: Apache-2.0
"""Read the Cost and Usage Report straight out of S3, without Athena.

WHY THIS EXISTS

nable publishes a promise: scanning never adds to your bill. Measured
2026-08-15, that promise was not true. Every scheduled snapshot called Cost
Explorer, which bills $0.01 per request to the customer's own AWS account, and
the only path we had to the CUR went through Athena at $5 per TB scanned. Both
meters are the customer's money. A charge that repeats on a timer with nobody
watching is the one nobody agreed to, and billing_access.py has said so in
words for months while the code went around it.

The CUR is already sitting in S3. The customer already pays to produce and store
it. Reading a file they own is the cheapest way to answer a cost question, and
it is the only one whose price does not scale with how often we ask:

    Cost Explorer   $0.01 per request        per QUESTION
    Athena          $5.00 per TB scanned     per BYTE, every query
    S3 GET          $0.0004 per 1,000        per FILE, once per delivery

Reading a month of CUR here is a few hundred GETs. That is fractions of a cent,
and it is zero data-transfer when the reader runs in the bucket's region, which
is the hosted case. Those constants live in aws_prices.py so the claim can be
summed instead of asserted, and read_daily_costs() returns exactly what it spent.

WHAT MAKES IT NEARLY FREE THE SECOND TIME

CUR is republished for the whole open month on every delivery, so a naive reader
re-downloads the same month every night. This one lists first, compares ETags
against what it read last time, and downloads only files that actually changed.
A rerun with nothing new costs one LIST per period and nothing else. The
tracking table is what turns "cheap once" into "cheap forever".

THE TRAP THIS MODULE IS SHAPED AROUND

cost_snapshots is keyed on (provider, service, account_id, region, date), and
store_snapshot upserts on exactly that key. Cost Explorer names a service
"Amazon Elastic Compute Cloud - Compute"; the CUR calls the same spend "Amazon
Elastic Compute Cloud". Those are different keys. Writing CUR rows next to CE
rows for a day nobody cleared would not overwrite anything, it would ADD, and
the day's total would silently double. So ingest replaces a provider-day
wholesale rather than upserting row by row: see storage.snapshots.replace_provider_day.
CUR is complete and authoritative for a day, and a partial merge with leftovers
from another source is never the right answer.

WHAT IS SUMMED, AND WHY ALL OF IT

Every line item type, not just Usage. Tax, Credit, Refund, RIFee and the Savings
Plan lines are all real money on the invoice, and Cost Explorer's UnblendedCost
includes them by default. Filtering to Usage-shaped rows the way the Athena
resource queries do would produce a number that is smaller than the bill and
looks like a bug in the customer's favour, which is the worst kind.
"""
from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from typing import Any, Iterable

from sqlalchemy import Column, DateTime, Index, Integer, String, Table, select

from ..aws_prices import S3_EGRESS_PER_GB, S3_GET_PER_1000, S3_LIST_PER_1000
from ..security.env import get_env
from ..storage.db import get_engine, metadata

log = logging.getLogger(__name__)

PROVIDER = "aws"


class CURReadError(RuntimeError):
    """Raised when the CUR is configured but could not be read."""


# ── what we read last time, so we can skip what has not changed ──────────────

cur_ingest_state = Table(
    "cur_ingest_state", metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("bucket", String(256), nullable=False),
    Column("object_key", String(1024), nullable=False),
    Column("etag", String(128), nullable=False),
    Column("size_bytes", Integer, nullable=False, default=0),
    Column("read_at", DateTime, nullable=False),
    # One row per object. The ETag is what makes a rerun free: CUR rewrites the
    # open month on every delivery, but an unchanged file has an unchanged ETag.
    Index("ux_cur_object", "bucket", "object_key", unique=True),
)


@dataclass
class ReadCost:
    """What reading the bill cost the customer, counted rather than estimated.

    Every field is a measurement taken during the read. usd is derived from
    aws_prices constants, so the arithmetic is inspectable and the claim
    "this added nothing to your bill" is a sum somebody can check.
    """
    list_requests: int = 0
    get_requests: int = 0
    bytes_downloaded: int = 0
    files_skipped_unchanged: int = 0
    same_region: bool = True

    @property
    def usd(self) -> float:
        cost = (self.list_requests / 1000.0) * S3_LIST_PER_1000
        cost += (self.get_requests / 1000.0) * S3_GET_PER_1000
        if not self.same_region:
            cost += (self.bytes_downloaded / 1_073_741_824.0) * S3_EGRESS_PER_GB
        return cost

    def as_dict(self) -> dict[str, Any]:
        return {
            "list_requests": self.list_requests,
            "get_requests": self.get_requests,
            "bytes_downloaded": self.bytes_downloaded,
            "files_skipped_unchanged": self.files_skipped_unchanged,
            "same_region": self.same_region,
            # Six places because the honest answer is usually a fraction of a
            # cent, and rounding it to 0.0 would overstate the case by hiding
            # that a number was computed at all.
            "usd": round(self.usd, 6),
        }


@dataclass
class CURReadResult:
    """Daily rows shaped for cost_snapshots, plus what they cost to fetch."""
    rows: list[dict] = field(default_factory=list)
    cost: ReadCost = field(default_factory=ReadCost)
    files_read: int = 0
    days_covered: set[str] = field(default_factory=set)
    backend: str = ""


# ── configuration ────────────────────────────────────────────────────────────

def _bucket() -> str:
    return (get_env("CUR_S3_BUCKET") or "").strip()


def _report_name() -> str:
    """The CUR report name, which is also the Athena table name.

    templates/aws-cur-setup.yaml sets CUR_ATHENA_TABLE=${ReportName}, so an
    operator who ran the stack already has this. CUR_S3_REPORT_NAME overrides it
    for anyone whose CUR predates the template.
    """
    explicit = (get_env("CUR_S3_REPORT_NAME") or "").strip()
    return explicit or (get_env("CUR_ATHENA_TABLE") or "").strip()


def _prefix() -> str:
    """S3Prefix from the report definition. The template uses "cur"."""
    raw = (get_env("CUR_S3_PREFIX") or "cur").strip().strip("/")
    return raw


def is_configured() -> bool:
    """True when the CUR can be read from S3 directly.

    Deliberately a WEAKER requirement than connectors.cur.is_configured(), which
    needs four Athena variables. Reading the files needs a bucket and a report
    name, and nothing else: no Athena database, no results bucket, no workgroup.
    Fewer required settings is not an accident here, it is the point.
    """
    return bool(_bucket() and _report_name())


def _same_region_as_bucket() -> bool:
    """Whether egress is free for this read.

    Data transfer out of S3 is free to the same region and $0.09/GB to the
    internet, so a hosted box in the customer's region pays nothing while a
    laptop pays something. Reported rather than assumed, because assuming zero
    is how a product ends up claiming a cost it did not measure.
    """
    bucket_region = (get_env("CUR_S3_REGION") or "").strip()
    here = (os.getenv("AWS_REGION") or os.getenv("AWS_DEFAULT_REGION") or "").strip()
    if not bucket_region or not here:
        # Unknown, so assume the paying case. An unmeasured cost should read as
        # present, never as zero.
        return False
    return bucket_region == here


# ── S3 discovery ─────────────────────────────────────────────────────────────

# CUR writes Hive-style partitions. The month is NOT zero-padded ("month=8"),
# which is the single most common reason a hand-built CUR path finds nothing.
_PARTITION_RE = re.compile(r"/year=(\d{4})/month=(\d{1,2})/", re.IGNORECASE)


def _partition_prefix() -> str:
    """Where the parquet lives: {prefix}/{report}/{report}/.

    The doubled report name is AWS's layout, not a typo, and it is the same path
    templates/aws-cur-setup.yaml points the Glue crawler at.
    """
    return f"{_prefix()}/{_report_name()}/{_report_name()}/"


def _months_in(start: date, end: date) -> list[tuple[int, int]]:
    out: list[tuple[int, int]] = []
    y, m = start.year, start.month
    while (y, m) <= (end.year, end.month):
        out.append((y, m))
        m += 1
        if m > 12:
            m, y = 1, y + 1
    return out


def _list_objects(s3: Any, bucket: str, prefix: str, cost: ReadCost) -> list[dict]:
    """Every parquet object under a prefix, with ETag and size.

    Counts each page as a LIST request because that is what AWS bills: a
    paginator over 3,000 objects is three requests, not one.
    """
    found: list[dict] = []
    token: str | None = None
    while True:
        kwargs = {"Bucket": bucket, "Prefix": prefix, "MaxKeys": 1000}
        if token:
            kwargs["ContinuationToken"] = token
        cost.list_requests += 1
        page = s3.list_objects_v2(**kwargs)
        for obj in page.get("Contents", []) or []:
            key = obj.get("Key", "")
            if not key.lower().endswith((".parquet", ".snappy.parquet")):
                continue
            found.append({
                "key": key,
                "etag": (obj.get("ETag") or "").strip('"'),
                "size": int(obj.get("Size") or 0),
            })
        if not page.get("IsTruncated"):
            break
        token = page.get("NextContinuationToken")
        if not token:
            break
    return found


def discover_files(s3: Any, start: date, end: date, cost: ReadCost) -> list[dict]:
    """Parquet objects covering a date range, listed per month partition.

    Listing per partition rather than the whole report prefix keeps a two-year
    bucket from being enumerated to answer a question about last week.
    """
    bucket, base = _bucket(), _partition_prefix()
    out: list[dict] = []
    for year, month in _months_in(start, end):
        # Both spellings, because CUR has shipped each and a bucket can hold
        # partitions written by different eras of the same report. dict.fromkeys
        # rather than a tuple: for October through December the two spellings are
        # the same string, and listing it twice would double the one request in
        # here that is not nearly free.
        for candidate in dict.fromkeys((f"{base}year={year}/month={month}/",
                                        f"{base}year={year}/month={month:02d}/")):
            objs = _list_objects(s3, bucket, candidate, cost)
            if objs:
                out.extend(objs)
                break
    return out


# ── change detection ─────────────────────────────────────────────────────────

def _known_etags(bucket: str, keys: Iterable[str]) -> dict[str, str]:
    keys = list(keys)
    if not keys:
        return {}
    engine = get_engine()
    with engine.connect() as conn:
        rows = conn.execute(
            select(cur_ingest_state.c.object_key, cur_ingest_state.c.etag)
            .where((cur_ingest_state.c.bucket == bucket)
                   & (cur_ingest_state.c.object_key.in_(keys)))
        ).fetchall()
    return {r.object_key: r.etag for r in rows}


def _record_read(bucket: str, files: list[dict]) -> None:
    if not files:
        return
    engine = get_engine()
    now = datetime.now(timezone.utc)
    with engine.begin() as conn:
        for f in files:
            conn.execute(cur_ingest_state.delete().where(
                (cur_ingest_state.c.bucket == bucket)
                & (cur_ingest_state.c.object_key == f["key"])))
        conn.execute(cur_ingest_state.insert(), [
            {"bucket": bucket, "object_key": f["key"], "etag": f["etag"],
             "size_bytes": f["size"], "read_at": now}
            for f in files
        ])


def _changed_only(bucket: str, files: list[dict], cost: ReadCost) -> list[dict]:
    known = _known_etags(bucket, [f["key"] for f in files])
    fresh = [f for f in files if known.get(f["key"]) != f["etag"]]
    cost.files_skipped_unchanged = len(files) - len(fresh)
    return fresh


# ── parquet ──────────────────────────────────────────────────────────────────

def _normalise(col: str) -> str:
    """Column names to one spelling.

    CUR has shipped both `line_item/UnblendedCost` and `line_item_unblended_cost`
    depending on format and era, and a reader that hard-codes one silently reads
    nothing from the other. Nothing is more expensive than a cost tool that
    finds no rows and reports a small number.
    """
    return re.sub(r"[^a-z0-9]+", "_", col.strip().lower()).strip("_")


# What we need, in normalised form, with the alternatives CUR has used.
_WANTED = {
    "date": ("line_item_usage_start_date", "lineitem_usagestartdate",
             "identity_time_interval"),
    "account": ("line_item_usage_account_id", "lineitem_usageaccountid"),
    "service": ("product_product_name", "product_productname",
                "line_item_product_code", "lineitem_productcode"),
    "region": ("product_region", "product_region_code", "line_item_availability_zone"),
    "cost": ("line_item_unblended_cost", "lineitem_unblendedcost"),
}


def _resolve_columns(available: Iterable[str]) -> dict[str, str]:
    """Map our field names to the real column names in this file.

    Raises rather than defaulting a missing cost column to zero. A CUR whose
    schema we cannot read is an error; a CUR that reads as $0 is a lie.
    """
    by_norm = {_normalise(c): c for c in available}
    resolved: dict[str, str] = {}
    for field_name, candidates in _WANTED.items():
        for cand in candidates:
            if cand in by_norm:
                resolved[field_name] = by_norm[cand]
                break
    missing = [k for k in ("date", "account", "cost") if k not in resolved]
    if missing:
        raise CURReadError(
            "The CUR parquet is missing columns nable needs: "
            f"{', '.join(missing)}. Found: {', '.join(sorted(by_norm)[:12])}..."
        )
    return resolved


def _aggregate_duckdb(paths: list[str], start: date, end: date) -> list[dict]:
    import duckdb

    con = duckdb.connect()
    try:
        files = "[" + ", ".join(f"'{p}'" for p in paths) + "]"
        # union_by_name because CUR adds columns mid-month when a customer
        # starts using a new service, and files in one month can differ.
        src = f"read_parquet({files}, union_by_name=true)"
        cols = [r[0] for r in con.execute(f"DESCRIBE SELECT * FROM {src}").fetchall()]
        c = _resolve_columns(cols)

        region = f'"{c["region"]}"' if "region" in c else "''"
        service = f'"{c["service"]}"' if "service" in c else "'unknown'"
        rows = con.execute(f"""
            SELECT
                CAST("{c['date']}" AS DATE)              AS d,
                COALESCE(CAST({service} AS VARCHAR), '') AS service,
                COALESCE(CAST("{c['account']}" AS VARCHAR), '') AS account_id,
                COALESCE(CAST({region} AS VARCHAR), '')  AS region,
                SUM(CAST("{c['cost']}" AS DOUBLE))       AS amount
            FROM {src}
            WHERE CAST("{c['date']}" AS DATE) >= DATE '{start.isoformat()}'
              AND CAST("{c['date']}" AS DATE) <= DATE '{end.isoformat()}'
            GROUP BY 1, 2, 3, 4
            HAVING SUM(CAST("{c['cost']}" AS DOUBLE)) <> 0
        """).fetchall()
    finally:
        con.close()

    return [
        {"snapshot_date": r[0].isoformat() if hasattr(r[0], "isoformat") else str(r[0])[:10],
         "service": r[1] or "unknown", "account_id": r[2], "region": r[3] or "",
         "amount_usd": float(r[4] or 0.0)}
        for r in rows
    ]


def _aggregate_pyarrow(paths: list[str], start: date, end: date) -> list[dict]:
    import pyarrow.parquet as pq

    lo, hi = start.isoformat(), end.isoformat()
    acc: dict[tuple[str, str, str, str], float] = {}

    for path in paths:
        pf = pq.ParquetFile(path)
        c = _resolve_columns(pf.schema_arrow.names)
        wanted = [v for v in c.values() if v in pf.schema_arrow.names]
        for batch in pf.iter_batches(columns=wanted, batch_size=50_000):
            d = batch.column(batch.schema.get_field_index(c["date"])).to_pylist()
            acct = batch.column(batch.schema.get_field_index(c["account"])).to_pylist()
            cost = batch.column(batch.schema.get_field_index(c["cost"])).to_pylist()
            svc = (batch.column(batch.schema.get_field_index(c["service"])).to_pylist()
                   if "service" in c else [None] * len(d))
            reg = (batch.column(batch.schema.get_field_index(c["region"])).to_pylist()
                   if "region" in c else [None] * len(d))
            for i in range(len(d)):
                day = d[i]
                day = day.isoformat()[:10] if hasattr(day, "isoformat") else str(day)[:10]
                if not (lo <= day <= hi):
                    continue
                amount = float(cost[i] or 0.0)
                if amount == 0.0:
                    continue
                key = (day, str(svc[i] or "unknown"), str(acct[i] or ""), str(reg[i] or ""))
                acc[key] = acc.get(key, 0.0) + amount

    return [{"snapshot_date": k[0], "service": k[1], "account_id": k[2],
             "region": k[3], "amount_usd": v} for k, v in acc.items()]


def _aggregate(paths: list[str], start: date, end: date) -> tuple[list[dict], str]:
    """DuckDB first, pyarrow second, a message that names the fix third."""
    try:
        import duckdb  # noqa: F401
    except ImportError:
        pass
    else:
        return _aggregate_duckdb(paths, start, end), "duckdb"

    try:
        import pyarrow  # noqa: F401
    except ImportError:
        raise CURReadError(
            "Reading the CUR from S3 needs a parquet reader, and neither duckdb "
            "nor pyarrow is installed. Run: pip install 'finops-mcp[cur]'"
        )
    return _aggregate_pyarrow(paths, start, end), "pyarrow"


# ── the read ─────────────────────────────────────────────────────────────────

def read_daily_costs(start: date, end: date, *, s3: Any = None,
                     force: bool = False) -> CURReadResult:
    """Daily cost rows from the CUR in S3, and what fetching them cost.

    Rows are shaped for cost_snapshots: snapshot_date, service, account_id,
    region, amount_usd. Nothing is written here; see ingest_range().

    force re-reads files whose ETag has not changed. Only useful after a schema
    fix, since the whole point of the ETag check is that this is otherwise free.
    """
    if not is_configured():
        raise CURReadError(
            "The CUR is not configured for direct S3 reads. Set CUR_S3_BUCKET and "
            "CUR_ATHENA_TABLE (or CUR_S3_REPORT_NAME). "
            "Deploy templates/aws-cur-setup.yaml if the report does not exist yet."
        )
    if start > end:
        raise ValueError(f"start {start} is after end {end}")

    import tempfile

    if s3 is None:
        import boto3
        s3 = boto3.client("s3")

    cost = ReadCost(same_region=_same_region_as_bucket())
    bucket = _bucket()

    files = discover_files(s3, start, end, cost)
    if not files:
        # Not an error and NOT zero spend. A CUR that has not been delivered yet
        # is a setup state, and the caller must be able to tell it apart from a
        # month that genuinely cost nothing.
        log.info("no CUR parquet found under s3://%s/%s for %s..%s",
                 bucket, _partition_prefix(), start, end)
        return CURReadResult(rows=[], cost=cost, files_read=0)

    to_read = files if force else _changed_only(bucket, files, cost)
    if not to_read:
        log.info("CUR unchanged since last read (%d files), nothing downloaded",
                 cost.files_skipped_unchanged)
        return CURReadResult(rows=[], cost=cost, files_read=0)

    with tempfile.TemporaryDirectory(prefix="nable-cur-") as tmp:
        paths: list[str] = []
        for i, f in enumerate(to_read):
            local = os.path.join(tmp, f"part-{i:05d}.parquet")
            cost.get_requests += 1
            s3.download_file(bucket, f["key"], local)
            cost.bytes_downloaded += f["size"] or os.path.getsize(local)
            paths.append(local)

        rows, backend = _aggregate(paths, start, end)

    # Only after a successful parse. Recording before would mark a file read
    # that we failed to understand, and the retry would skip it forever.
    _record_read(bucket, to_read)

    result = CURReadResult(
        rows=rows, cost=cost, files_read=len(to_read),
        days_covered={r["snapshot_date"] for r in rows}, backend=backend,
    )
    log.info("CUR read: %d files, %d rows, %d days, $%.6f",
             result.files_read, len(rows), len(result.days_covered), cost.usd)
    return result


def ingest_range(start: date, end: date, *, s3: Any = None,
                 force: bool = False) -> dict[str, Any]:
    """Read the CUR and write it into cost_snapshots, one whole day at a time.

    Whole-day replacement, not per-row upsert. See the module docstring: CE and
    the CUR spell services differently, so merging them by key would add rather
    than overwrite and double the day. The CUR is complete for a day it covers,
    which is exactly the condition that makes replacing it safe.
    """
    from ..storage.snapshots import replace_provider_day

    result = read_daily_costs(start, end, s3=s3, force=force)

    by_day: dict[str, list[dict]] = {}
    for row in result.rows:
        by_day.setdefault(row["snapshot_date"], []).append(row)

    written = 0
    for day, rows in sorted(by_day.items()):
        written += replace_provider_day(
            provider=PROVIDER, day=date.fromisoformat(day), rows=rows)

    return {
        "provider": PROVIDER,
        "days_written": len(by_day),
        "rows_written": written,
        "files_read": result.files_read,
        "backend": result.backend,
        "cost": result.cost.as_dict(),
        "source": "cur_s3",
    }


def _window_has_rows(start: date, end: date) -> bool:
    from sqlalchemy import func

    from ..storage.db import cost_snapshots
    engine = get_engine()
    with engine.connect() as conn:
        n = conn.execute(
            select(func.count()).select_from(cost_snapshots).where(
                (cost_snapshots.c.provider == PROVIDER)
                & (cost_snapshots.c.snapshot_date >= start.isoformat())
                & (cost_snapshots.c.snapshot_date <= end.isoformat()))
        ).scalar()
    return bool(n)


def ingest_recent(days: int = 3, *, s3: Any = None) -> dict[str, Any]:
    """The scheduled call. Re-reads a short trailing window, not just yesterday.

    AWS restates recent days for a week or more after the fact, so ingesting
    only yesterday freezes whatever partial figure the first delivery carried.
    Three days is cheap because unchanged files are skipped, and it means a
    restatement lands instead of being missed.

    The self-heal is the second half. ETag skipping assumes the reason we have
    not re-read a file is that we already have its data, and that assumption
    breaks the moment the two stores disagree: restore the box from a snapshot,
    or lose the database while keeping the state table, and every future run
    happily skips everything and the dashboard stays empty forever. So when the
    window has no rows but the files were skipped as unchanged, read them again.
    """
    today = date.today()
    start = today - timedelta(days=days)
    out = ingest_range(start, today, s3=s3)

    skipped = out["cost"]["files_skipped_unchanged"]
    if out["rows_written"] == 0 and skipped and not _window_has_rows(start, today):
        log.warning("CUR files were unchanged but the window is empty; re-reading "
                    "%d file(s). The ingest state and cost_snapshots disagree.", skipped)
        out = ingest_range(start, today, s3=s3, force=True)
        out["self_healed"] = True
    return out
