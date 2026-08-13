"""Storage-layer regressions: the cost cache key, the SQL we bind, the DDL we write.

Why this file exists, stated plainly: every defect pinned here is invisible to a
test that stubs the storage layer, because the storage layer IS the defect. A
cache that answers with a different account's spend. A query that compares a
timestamp column against a string and gets a lexicographic answer. A dedup that
is a promise in a docstring rather than a constraint in the schema. A CREATE
TABLE that only parses on one of the two backends this product sells. None of
those raise. Each returns a plausible number that belongs to somebody else, or
silently returns nothing at all.

The seam is deliberately low. Nothing below replaces a nable function. The fakes
sit where the outside world sits: a boto3 Session whose CE and STS clients return
documented-shape responses, a connector object with a get_costs coroutine, a row
committed from a second database connection, a frozen clock. Everything between
that boundary and the assertion is production code, including the cache key
construction, the bind parameters, the transaction boundaries and the file modes.

Each test says which kind it is in its docstring. Most FAIL against today's code,
which is the point: they are the reproduction, not a description of one. Two are
invariant tests that fail because the invariant the code's own docstring promises
was never written down in the schema. One passes today and must keep passing: it
is the control that stops the cache defects being "fixed" by turning the cache
off.

Not covered here on purpose: get_engine publishing _ENGINE before create_all.
That defect is dual-tagged storage and concurrency, and
tests/test_audit_concurrency.py already pins it with a real second thread, which
is the only shape of that test that stays safe once the fix adds a lock.

No network, no credentials, no Cost Explorer, no LLM calls.
"""
from __future__ import annotations

import asyncio
import os
import stat
from datetime import date, datetime, timedelta, timezone

import pytest
import sqlalchemy as sa
from sqlalchemy import event

import finops.cache as cache_mod
import finops.storage.db as db_mod
from finops.anomaly.detector import AnomalyResult, persist_anomaly
from finops.connectors.aws import AWSConnector
from finops.connectors.base import CostSummary
from finops.scoring import scorecard
from finops.storage.db import anomalies


# ── fixtures: every global these tests touch is restored ─────────────────────


def _reset_cache_module() -> None:
    """Everything a freshly started nable process rebuilds: the in-memory store,
    the disk-table flag, and the lazily loaded HMAC key for the pickle blobs."""
    cache_mod._store.clear()
    cache_mod._disk_ready = False
    cache_mod._hmac_key = None
    cache_mod._hmac_key_loaded = False


@pytest.fixture
def cache_isolated(tmp_path, monkeypatch):
    """Point the cost cache at a throwaway data dir and reset its module state.

    The data dir does not exist yet, so the directory and every file inside it is
    created by cache.py itself under the test's umask. That is what the file mode
    test needs to observe.
    """
    saved = (
        cache_mod._DISABLED,
        cache_mod._DISK_DISABLED,
        cache_mod._disk_ready,
        cache_mod._hmac_key,
        cache_mod._hmac_key_loaded,
        dict(cache_mod._store),
    )
    monkeypatch.setenv("FINOPS_DATA_DIR", str(tmp_path / "nable"))
    for var in ("FINOPS_CACHE_DISABLED", "FINOPS_CACHE_DISK_DISABLED", "AWS_ROLE_ARNS",
                "FINOPS_DEMO", "FINOPS_DEMO_FORCE", "NABLE_NO_COST_EXPLORER"):
        monkeypatch.delenv(var, raising=False)
    cache_mod._DISABLED = False
    cache_mod._DISK_DISABLED = False
    _reset_cache_module()
    yield cache_mod
    (
        cache_mod._DISABLED,
        cache_mod._DISK_DISABLED,
        cache_mod._disk_ready,
        cache_mod._hmac_key,
        cache_mod._hmac_key_loaded,
    ) = saved[:5]
    cache_mod._store.clear()
    cache_mod._store.update(saved[5])


@pytest.fixture
def fresh_db(tmp_path, monkeypatch):
    """A brand new SQLite database, with the engine singleton restored after."""
    prev_engine, prev_dir = db_mod._ENGINE, db_mod._DATA_DIR
    monkeypatch.setenv("FINOPS_DB_PATH", str(tmp_path / "finops.db"))
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.delenv("FINOPS_PROFILE", raising=False)
    db_mod._ENGINE = None
    db_mod._DATA_DIR = None
    yield db_mod
    if db_mod._ENGINE is not None and db_mod._ENGINE is not prev_engine:
        try:
            db_mod._ENGINE.dispose()
        except Exception:
            pass
    db_mod._ENGINE = prev_engine
    db_mod._DATA_DIR = prev_dir


# ── the cost cache does not know which account it is holding ─────────────────
#
# The fakes are a boto3 Session and the two clients it hands out. Shapes come
# from the Cost Explorer GetCostAndUsage and STS GetCallerIdentity references,
# trimmed to the fields AWSConnector._build_summary and _account_id read.


class _FakeCE:
    def __init__(self, amount: float) -> None:
        self.amount = amount
        self.calls = 0

    def get_cost_and_usage(self, **_kw):
        self.calls += 1
        return {
            "ResultsByTime": [
                {
                    "TimePeriod": {"Start": "2026-07-01", "End": "2026-07-31"},
                    "Groups": [
                        {
                            "Keys": ["Amazon Elastic Compute Cloud - Compute"],
                            "Metrics": {
                                "UnblendedCost": {"Amount": str(self.amount), "Unit": "USD"}
                            },
                        }
                    ],
                }
            ]
        }


class _FakeSTS:
    def __init__(self, account_id: str) -> None:
        self.account_id = account_id

    def get_caller_identity(self):
        return {"Account": self.account_id, "Arn": f"arn:aws:iam::{self.account_id}:user/x"}


class _FakeSession:
    """A boto3.Session standing in for one customer account, as the multi-account
    registry injects it."""

    def __init__(self, account_id: str, amount: float) -> None:
        self.account_id = account_id
        self.ce = _FakeCE(amount)
        self.sts = _FakeSTS(account_id)
        self.profile_name = f"acct-{account_id}"

    def client(self, service_name, **_kw):
        return self.ce if service_name == "ce" else self.sts

    def get_credentials(self):
        return object()


def test_aws_cost_cache_does_not_serve_one_account_spend_as_another(cache_isolated):
    """FAILS NOW, the bug is real.

    get_cost_summary_all_accounts loops every configured account inside one tool
    call, building AWSConnector(session=get_boto3_session(acct)) per account. The
    AWS-internal cache key is built from the AWS_ROLE_ARNS env var and nothing
    else, so an injected session collapses to the literal "default" and every
    account after the first is a cache hit on the first one's CostSummary. The
    customer is shown account 1's total, and account 1's account id, under
    account 2's name. COST_TTL is twelve hours and above the persistence floor,
    so the wrong answer is pickled to disk and survives a restart too.
    """
    prod = _FakeSession("111111111111", 412_355.10)
    sandbox = _FakeSession("222222222222", 903.44)
    start, end = date(2026, 7, 1), date(2026, 7, 31)

    first = asyncio.run(AWSConnector(session=prod).get_costs(start, end))
    second = asyncio.run(AWSConnector(session=sandbox).get_costs(start, end))

    assert first.total_usd == pytest.approx(412_355.10)
    assert sandbox.ce.calls == 1, (
        "the sandbox account was never queried: its answer came out of the cache "
        "entry the production account wrote"
    )
    assert second.total_usd == pytest.approx(903.44), (
        f"sandbox reported ${second.total_usd:,.2f}, which is the production "
        f"account's spend"
    )
    assert list(second.by_account) == ["222222222222"], (
        f"sandbox attributed its spend to account {list(second.by_account)}"
    )


def test_the_cache_still_serves_a_repeat_question_for_the_same_account(cache_isolated):
    """PASSES NOW, and must keep passing after the fix.

    This is the control. Cost Explorer bills per request and an agentic session
    re-asks the same question while cross-referencing, which is the whole reason
    the cache exists. Adding an account discriminator to the key must not turn
    every repeat into a second billed request. If this goes red, the cache was
    disabled rather than keyed correctly.
    """
    prod = _FakeSession("111111111111", 412_355.10)
    start, end = date(2026, 7, 1), date(2026, 7, 31)
    connector = AWSConnector(session=prod)

    asyncio.run(connector.get_costs(start, end))
    again = asyncio.run(connector.get_costs(start, end))

    assert prod.ce.calls == 1, "the same question was billed to Cost Explorer twice"
    assert again.total_usd == pytest.approx(412_355.10)


class _FakeConnector:
    """The provider-connector boundary _fetch_costs_cached calls through."""

    def __init__(self, account_id: str, total: float) -> None:
        self.account_id = account_id
        self.total = total
        self.calls = 0

    async def get_costs(self, start, end, granularity="MONTHLY"):
        self.calls += 1
        return CostSummary(
            provider="aws",
            start_date=start,
            end_date=end,
            total_usd=self.total,
            by_service={"Amazon Elastic Compute Cloud - Compute": self.total},
            by_account={self.account_id: self.total},
            by_region={},
            entries=[],
        )


def test_server_cost_cache_key_distinguishes_two_connectors_for_one_provider(cache_isolated):
    """FAILS NOW, the bug is real.

    server._fetch_costs_cached keys on (provider name, start, end, granularity)
    with nothing identifying which account the connector points at. Two
    connectors built for two different customer accounts inside one process share
    a single cache entry, so the second is answered with the first one's numbers
    and its provider is never called.
    """
    from finops import server as srv

    prod = _FakeConnector("111111111111", 412_355.10)
    sandbox = _FakeConnector("222222222222", 903.44)
    start, end = date(2026, 7, 1), date(2026, 7, 31)

    asyncio.run(srv._fetch_costs_cached("aws", prod, start, end))
    second = asyncio.run(srv._fetch_costs_cached("aws", sandbox, start, end))

    assert sandbox.calls == 1, "the second connector was never asked; it was served from cache"
    assert second.total_usd == pytest.approx(903.44), (
        f"the second connector reported ${second.total_usd:,.2f}, which is the "
        f"first one's total"
    )


def test_disk_cost_cache_is_not_shared_between_two_profiles(tmp_path, monkeypatch):
    """FAILS NOW, the bug is real.

    FINOPS_PROFILE exists so one operator can point nable at two different
    customers, and storage/db gives each profile its own database and its own
    vault. The on-disk cost cache is keyed only on FINOPS_DATA_DIR, so both
    profiles read and write one cache.db under ~/.nable. Customer A's
    CostSummary, by_account and by_service included, is served to the session
    that asked about customer B, and it survives a restart for twelve hours.
    """
    saved = (
        cache_mod._DISABLED,
        cache_mod._DISK_DISABLED,
        cache_mod._disk_ready,
        cache_mod._hmac_key,
        cache_mod._hmac_key_loaded,
        dict(cache_mod._store),
        db_mod._DATA_DIR,
    )
    monkeypatch.setenv("HOME", str(tmp_path))
    for var in ("FINOPS_DATA_DIR", "FINOPS_CACHE_DISABLED", "FINOPS_CACHE_DISK_DISABLED"):
        monkeypatch.delenv(var, raising=False)
    cache_mod._DISABLED = False
    cache_mod._DISK_DISABLED = False

    key = cache_mod.make_key("connector.get_costs", "aws", "2026-07-01", "2026-07-31", "MONTHLY")
    try:
        monkeypatch.setenv("FINOPS_PROFILE", "customer-a")
        db_mod._DATA_DIR = None
        dir_a = db_mod.data_dir()
        _reset_cache_module()
        cache_mod.set(key, {"total_usd": 412_355.10, "customer": "a"}, ttl=cache_mod.COST_TTL)

        monkeypatch.setenv("FINOPS_PROFILE", "customer-b")
        db_mod._DATA_DIR = None
        dir_b = db_mod.data_dir()
        _reset_cache_module()

        assert dir_a != dir_b, "profiles are supposed to be separate stores"
        leaked = cache_mod.get(key)
        assert leaked is None, (
            f"the customer-b profile read {leaked} out of the cache. That is "
            f"customer-a's spend, persisted next to {dir_a}"
        )
    finally:
        (
            cache_mod._DISABLED,
            cache_mod._DISK_DISABLED,
            cache_mod._disk_ready,
            cache_mod._hmac_key,
            cache_mod._hmac_key_loaded,
        ) = saved[:5]
        cache_mod._store.clear()
        cache_mod._store.update(saved[5])
        db_mod._DATA_DIR = saved[6]


@pytest.mark.xfail(strict=True, reason="audit finding, not yet fixed. strict=True so that fixing it FAILS here until this marker is removed: the marker count is the work list.")
def test_persisted_cost_cache_is_not_world_readable(cache_isolated):
    """FAILS NOW, the bug is real.

    cache.db holds pickled CostSummary objects for every provider for twelve
    hours: totals, by_service, by_account, by_region, account ids. storage/db
    chmods its data dir to 0700 and finops.db to 0600, and cache.py creates
    cache.key with an explicit 0600, so the intent is established everywhere
    except the file with the cost data in it. Under the ordinary 0022 umask on a
    shared box or a container image with a non-root app user, every other local
    account can read the org's spend.
    """
    prev_umask = os.umask(0o022)
    try:
        cache_isolated.set(
            cache_isolated.make_key("connector.get_costs", "aws", "2026-07-01", "2026-07-31"),
            {"total_usd": 412_355.10},
            ttl=cache_isolated.COST_TTL,
        )
    finally:
        os.umask(prev_umask)

    data_dir = os.environ["FINOPS_DATA_DIR"]

    def mode(name: str) -> int:
        return stat.S_IMODE(os.stat(os.path.join(data_dir, name)).st_mode)

    assert mode("cache.key") == 0o600, (
        "the HMAC key is the control here: cache.py creates it 0600 on purpose"
    )
    assert mode("cache.db") == 0o600, (
        f"cache.db is {oct(mode('cache.db'))} and holds twelve hours of pickled "
        f"cost data for every provider"
    )
    dir_mode = stat.S_IMODE(os.stat(data_dir).st_mode)
    assert dir_mode == 0o700, (
        f"the cache data dir is {oct(dir_mode)}, while storage/db chmods its own "
        f"data dir to 0700"
    )


# ── the scorecard binds ISO strings to a DateTime column ─────────────────────

_FROZEN_NOW = datetime(2026, 8, 8, 12, 0, 0, tzinfo=timezone.utc)


class _FrozenClock(datetime):
    """A fixed clock, faked at the system boundary rather than in the code under
    test. Without one this defect is time-of-day dependent: the lexicographic
    compare only goes wrong when the two values land on the same UTC calendar
    date, which is most of the day but not all of it."""

    @classmethod
    def now(cls, tz=None):
        return _FROZEN_NOW if tz is not None else _FROZEN_NOW.replace(tzinfo=None)


def _insert_anomaly(engine, detected_at: datetime, *, acknowledged: bool = False) -> None:
    """Write the row exactly as persist_anomaly writes it: a tz-aware datetime
    into the DateTime column, an ISO date string into snapshot_date."""
    with engine.begin() as conn:
        conn.execute(
            anomalies.insert().values(
                provider="aws",
                service="Amazon Elastic Compute Cloud - Compute",
                account_id="111111111111",
                detected_at=detected_at,
                snapshot_date=detected_at.date().isoformat(),
                severity="medium",
                direction="spike",
                pct_change=180.0,
                z_score=4.1,
                baseline_mean=900.0,
                current_amount=2520.0,
                acknowledged=acknowledged,
                notified=False,
            )
        )


@pytest.mark.xfail(strict=True, reason="audit finding, not yet fixed. strict=True so that fixing it FAILS here until this marker is removed: the marker count is the work list.")
def test_anomaly_inside_the_response_window_is_not_counted_overdue(fresh_db, monkeypatch):
    """FAILS NOW, the bug is real.

    The scorecard binds ack_cutoff.isoformat() against a DateTime column, so
    SQLAlchemy types the parameter as String and skips the DateTime bind
    processor. SQLite stored '2026-08-06 14:30:00.000000' and receives
    '2026-08-06T12:00:00+00:00'. A space sorts before a T, so every anomaly on
    the same UTC date as the cutoff compares as older than it whatever the clock
    says. An anomaly 45.5 hours old, still inside the 48 hour response window, is
    counted overdue. That count drives overdue_rate, which drives the anomaly
    response score on the customer-facing scorecard and the tickets
    create_scorecard_tickets opens from it. PostgreSQL casts the literal and gets
    this right, so the default local install is the wrong one.
    """
    monkeypatch.setattr(scorecard, "datetime", _FrozenClock)
    engine = fresh_db.get_engine()
    _insert_anomaly(engine, _FROZEN_NOW - timedelta(hours=45, minutes=30))

    dim = scorecard._score_anomaly_response(lookback_days=30, response_window_hours=48)

    assert dim.metadata.get("total_anomalies_30d") == 1, (
        f"the row was not read back at all: {dim.metadata}"
    )
    assert dim.metadata["unacknowledged_past_48h"] == 0, (
        "an anomaly detected 45.5h ago is inside the 48h window and must not "
        "count as overdue"
    )
    assert dim.raw_score == pytest.approx(100.0), (
        f"a clean response record scored {dim.raw_score}"
    )


@pytest.mark.xfail(strict=True, reason="audit finding, not yet fixed. strict=True so that fixing it FAILS here until this marker is removed: the marker count is the work list.")
def test_anomaly_on_the_lookback_boundary_day_stays_inside_the_window(fresh_db, monkeypatch):
    """FAILS NOW, the bug is real.

    The mirror of the same defect on the other comparison. detected_at >= cutoff
    is also a string compare, so an anomaly from 29 days and 18 hours ago, well
    inside the 30 day lookback, sorts below the cutoff string and vanishes from
    total_anomalies_30d. The dimension then reports "No anomalies detected in the
    last 30 days" and hands out a flat 80 for an account that had them.
    """
    monkeypatch.setattr(scorecard, "datetime", _FrozenClock)
    engine = fresh_db.get_engine()
    _insert_anomaly(engine, datetime(2026, 7, 9, 18, 0, 0, tzinfo=timezone.utc), acknowledged=True)

    dim = scorecard._score_anomaly_response(lookback_days=30, response_window_hours=48)

    assert dim.metadata["total_anomalies_30d"] == 1, (
        "an anomaly 29d18h old is inside a 30 day lookback and was dropped by a "
        "lexicographic compare on the boundary day"
    )


# ── persist_anomaly's idempotency is a promise, not a constraint ─────────────


def _anomaly_result() -> AnomalyResult:
    return AnomalyResult(
        provider="aws",
        service="Amazon Elastic Compute Cloud - Compute",
        account_id="111111111111",
        snapshot_date=date(2026, 8, 7),
        severity="high",
        direction="spike",
        pct_change=180.0,
        z_score=4.1,
        baseline_mean=900.0,
        current_amount=2520.0,
    )


@pytest.mark.xfail(strict=True, reason="audit finding, not yet fixed. strict=True so that fixing it FAILS here until this marker is removed: the marker count is the work list.")
def test_persist_anomaly_does_not_duplicate_when_another_writer_wins_the_race(fresh_db):
    """FAILS NOW, the bug is real.

    persist_anomaly's docstring promises that a cron retry, the
    run_anomaly_check_now tool, or a fail-open second scheduler process "cannot
    re-insert the same spend event or re-fire its alerts and tickets". It is a
    plain SELECT then INSERT with nothing making the pair atomic. The competing
    writer here is injected at the database boundary, committed from a second
    connection in the gap between those two statements, so the interleaving is
    exact instead of hopefully-timed. _acquire_scheduler_lock fails open on any
    error, which is how two schedulers exist at once, and scheduler/jobs.py keys
    every downstream action on the is_new flag: both runs post to Slack and
    Teams, push to n8n, and open a ticket for one spend event.
    """
    engine = fresh_db.get_engine()
    result = _anomaly_result()
    injected = {"n": 0}

    def _win_the_race(conn, cursor, statement, parameters, context, executemany):
        if injected["n"] or "FROM anomalies" not in statement:
            return
        injected["n"] = 1
        with engine.begin() as other:
            other.execute(
                anomalies.insert().values(
                    provider=result.provider,
                    service=result.service,
                    account_id=result.account_id,
                    detected_at=datetime.now(timezone.utc),
                    snapshot_date=result.snapshot_date.isoformat(),
                    severity=result.severity,
                    direction=result.direction,
                    pct_change=result.pct_change,
                    z_score=result.z_score,
                    baseline_mean=result.baseline_mean,
                    current_amount=result.current_amount,
                    acknowledged=False,
                    notified=False,
                )
            )

    event.listen(engine, "after_cursor_execute", _win_the_race)
    try:
        _anomaly_id, is_new = persist_anomaly(result)
    finally:
        event.remove(engine, "after_cursor_execute", _win_the_race)

    assert injected["n"] == 1, "the competing insert never landed; this test proved nothing"
    with engine.connect() as conn:
        rows = conn.execute(sa.select(sa.func.count()).select_from(anomalies)).scalar()
    assert rows == 1, f"{rows} rows for one spend event"
    assert is_new is False, (
        "persist_anomaly reported a new anomaly after another writer had already "
        "recorded it, which fires a duplicate alert and opens a duplicate ticket"
    )


@pytest.mark.xfail(strict=True, reason="audit finding, not yet fixed. strict=True so that fixing it FAILS here until this marker is removed: the marker count is the work list.")
def test_the_anomaly_dedup_tuple_is_enforced_by_the_database(fresh_db):
    """INVARIANT TEST, and it FAILS NOW because the invariant does not exist.

    The tuple persist_anomaly dedups on, (provider, service, account_id,
    snapshot_date, direction), has no unique constraint behind it: storage/db
    declares only the non-unique ix_anom_date and ix_anom_ack. Until the database
    is the arbiter, every caller has to win a race it has no way to win. If this
    goes red after the constraint lands, someone dropped the guard and duplicate
    alerts are back.
    """
    engine = fresh_db.get_engine()
    row = dict(
        provider="aws",
        service="Amazon Elastic Compute Cloud - Compute",
        account_id="111111111111",
        detected_at=datetime.now(timezone.utc),
        snapshot_date="2026-08-07",
        severity="high",
        direction="spike",
        pct_change=180.0,
        z_score=4.1,
        baseline_mean=900.0,
        current_amount=2520.0,
        acknowledged=False,
        notified=False,
    )
    with engine.begin() as conn:
        conn.execute(anomalies.insert().values(**row))
    try:
        with engine.begin() as conn:
            conn.execute(anomalies.insert().values(**row))
    except sa.exc.IntegrityError:
        return
    pytest.fail(
        "a second row with the same (provider, service, account_id, "
        "snapshot_date, direction) was accepted, so nothing but a lost race "
        "stands between a cron retry and a duplicate Slack alert plus a "
        "duplicate Jira ticket"
    )


# ── scorecard_history is hand-written SQLite DDL, outside the metadata ───────


@pytest.mark.xfail(strict=True, reason="audit finding, not yet fixed. strict=True so that fixing it FAILS here until this marker is removed: the marker count is the work list.")
def test_scorecard_history_ddl_is_not_sqlite_only(fresh_db):
    """FAILS NOW, the bug is real.

    The statement is captured at the cursor, so this is the SQL nable actually
    executes rather than the SQL its source appears to contain. It carries
    AUTOINCREMENT, which PostgreSQL rejects as a syntax error. Both
    _persist_score and _get_score_trend issue that same DDL and both wrap
    everything in a bare except that only log.debug's, so with DATABASE_URL
    pointed at Postgres, the shared-team mode this product sells, no score is
    ever written, no error is ever surfaced, and every scorecard reports "first
    score recorded" forever.
    """
    engine = fresh_db.get_engine()
    statements: list[str] = []

    def _capture(conn, cursor, statement, parameters, context, executemany):
        statements.append(statement)

    event.listen(engine, "before_cursor_execute", _capture)
    try:
        scorecard._persist_score("overall", 72.5, "B", {"dimension_count": 5})
    finally:
        event.remove(engine, "before_cursor_execute", _capture)

    assert statements, "_persist_score executed no SQL at all"
    sqlite_only = [s for s in statements if "AUTOINCREMENT" in s.upper()]
    assert not sqlite_only, (
        "hand-written DDL uses AUTOINCREMENT, which PostgreSQL rejects as a "
        f"syntax error: {' '.join(sqlite_only[0].split())[:110]}"
    )


@pytest.mark.xfail(strict=True, reason="audit finding, not yet fixed. strict=True so that fixing it FAILS here until this marker is removed: the marker count is the work list.")
def test_scorecard_history_is_a_managed_table(fresh_db):
    """INVARIANT TEST, and it FAILS NOW because the invariant does not exist.

    Every other table nable persists to is declared on storage.db.metadata, so
    create_all renders its DDL for whichever dialect is connected and
    _run_sqlite_migrations can evolve it. scorecard_history is created by a
    CREATE TABLE string inside scoring/scorecard.py instead, which is why it is
    outside create_all, outside the migration list, and written in one backend's
    syntax. This is the structural version of the test above: fix the DDL by hand
    and the next hand-written table repeats the defect.
    """
    assert "scorecard_history" in fresh_db.metadata.tables, (
        "scorecard_history is not declared on storage.db.metadata, so its DDL is "
        "hand-written per backend instead of compiled for the connected dialect"
    )
