"""Additive migrations must work on PostgreSQL, not just SQLite.

The bug this locks down: detection used `PRAGMA table_info`, which is SQLite-only.
On a shared-team Postgres deployment it raised, the exception was swallowed as a
warning, and the ALTER never ran. `metadata.create_all` does not compensate, since
it creates missing TABLES and never missing columns on a table that already
exists. So every column added to the migration list after a Postgres database was
first created stayed missing, and any query naming it failed with an
undefined-column error.

Compounding it, the DDL was hand-written with SQLite type names: `DATETIME` and
`REAL` are not PostgreSQL types, so even with working detection the ALTER would
have been a syntax error.

No Postgres server is needed here. SQLAlchemy can compile DDL for a dialect
without connecting, which is exactly the layer both bugs lived at.
"""
from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from finops.storage.db import _add_column_ddl, metadata, savings_recommendations


class _FakeEngine:
    """Just enough engine to compile DDL for a dialect we cannot connect to."""

    def __init__(self, dialect):
        self.dialect = dialect


_PG = _FakeEngine(postgresql.dialect())
_SQLITE = sa.create_engine("sqlite://")


# ── the DDL is valid for the target backend ─────────────────────────────────


def test_datetime_column_renders_as_timestamp_on_postgres():
    """DATETIME is not a PostgreSQL type. Hardcoding it made the ALTER a syntax
    error on the one backend that needed the migration most."""
    ddl = _add_column_ddl(_PG, "savings_recommendations", "regressed_at")
    assert "TIMESTAMP" in ddl.upper()
    assert "DATETIME" not in ddl.upper()


def test_float_column_renders_from_the_model_not_a_hardcoded_real():
    """REAL is a valid PostgreSQL type, so this one was not a syntax error, but it
    is float4 while the model says Float. Deriving from the model keeps the column
    the width the model declares on every backend."""
    ddl = _add_column_ddl(_PG, "budgets", "critical_at_pct")
    assert "FLOAT" in ddl.upper()
    assert "DEFAULT 100.0" in ddl


def test_sqlite_ddl_is_unchanged_in_spirit():
    ddl = _add_column_ddl(_SQLITE, "savings_recommendations", "regressed_at")
    assert ddl.startswith("ALTER TABLE savings_recommendations ADD COLUMN regressed_at")
    assert "DATETIME" in ddl.upper()


def test_not_null_column_carries_its_default():
    """A NOT NULL column added to a table with existing rows is rejected outright
    unless it has a default."""
    for eng in (_PG, _SQLITE):
        ddl = _add_column_ddl(eng, "savings_recommendations", "regression_count")
        assert "NOT NULL" in ddl
        assert "DEFAULT 0" in ddl


def test_nullable_column_gets_no_not_null_clause():
    ddl = _add_column_ddl(_PG, "savings_recommendations", "verified_basis")
    assert "NOT NULL" not in ddl


def test_string_default_is_quoted():
    """A bare unquoted string default is a syntax error. Only exercised if a
    NOT NULL string column is ever added to the migration list, so pin it now."""
    tbl = sa.Table(
        "_ddl_probe", metadata,
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("mode", sa.String(16), nullable=False, default="auto"),
    )
    try:
        ddl = _add_column_ddl(_PG, "_ddl_probe", "mode")
        assert "DEFAULT 'auto'" in ddl
    finally:
        metadata.remove(tbl)


# ── every migration entry actually exists in the model ──────────────────────


def test_every_migration_names_a_real_model_column():
    """The DDL is derived from the model now, so a typo'd or removed column would
    raise at migration time instead of being a silently skipped warning."""
    import inspect as _inspect

    from finops.storage import db as _db

    src = _inspect.getsource(_db._run_sqlite_migrations)
    # Only the additive migrations list. The trailing legacy-cleanup tuple names
    # budgets.block_at_pct, a column deliberately DROPPED from the model, so
    # including it here would be asserting the opposite of what it is for.
    src = src[src.index("migrations: list"):src.index("inspector = inspect(")]
    import re
    pairs = re.findall(r'\(\s*"([a-z_]+)",\s*"([a-z_]+)"\s*\)', src)
    assert pairs, "could not parse the migration list"
    for table, column in pairs:
        assert table in metadata.tables, f"migration names unknown table {table!r}"
        assert column in metadata.tables[table].c, (
            f"migration names {table}.{column!r}, which is not in the model"
        )
        # And it must compile for both backends.
        for eng in (_PG, _SQLITE):
            assert _add_column_ddl(eng, table, column).startswith("ALTER TABLE")


# ── the end-to-end behaviour on a database missing the columns ──────────────


def test_migration_adds_missing_columns_to_an_existing_table():
    """Simulates upgrading: the table exists in its pre-0.8.192 shape, and the new
    columns have to be added to it rather than the table recreated."""
    from finops.storage.db import _run_sqlite_migrations

    eng = sa.create_engine("sqlite://")
    old = sa.MetaData()
    sa.Table(
        "savings_recommendations", old,
        *[c._copy() for c in savings_recommendations.columns
          if c.name not in ("regressed_at", "regression_count")],
    )
    old.create_all(eng)

    with eng.connect() as c:
        before = {r["name"] for r in sa.inspect(eng).get_columns("savings_recommendations")}
    assert "regressed_at" not in before

    _run_sqlite_migrations(eng)

    after = {r["name"] for r in sa.inspect(eng).get_columns("savings_recommendations")}
    assert "regressed_at" in after, "migration did not add the missing column"
    assert "regression_count" in after

    # And the query that used to fail now works.
    with eng.connect() as c:
        c.execute(sa.select(savings_recommendations).limit(1)).fetchall()


def test_migration_is_idempotent():
    from finops.storage.db import _run_sqlite_migrations

    eng = sa.create_engine("sqlite://")
    metadata.create_all(eng)
    _run_sqlite_migrations(eng)
    _run_sqlite_migrations(eng)  # must not raise or duplicate
    cols = [c["name"] for c in sa.inspect(eng).get_columns("savings_recommendations")]
    assert cols.count("regressed_at") == 1


def test_detection_does_not_use_a_sqlite_only_pragma():
    """A source-level guard, and deliberately so.

    The original bug was that detection used `PRAGMA table_info`, which succeeds on
    SQLite and raises on PostgreSQL. No test in this suite can catch a regression
    of it, because there is no PostgreSQL to connect to here: swapping the
    inspector back for PRAGMA leaves every other test in this file green. Until CI
    runs a real Postgres service, this pins the one thing that actually broke.
    """
    import inspect as _inspect

    from finops.storage import db as _db

    src = _inspect.getsource(_db._run_sqlite_migrations)
    # Check executable code only. The docstring and the comments both name PRAGMA
    # to explain the bug, and a guard that fails on its own explanation is useless.
    body = src.split('"""')[-1]
    code = "\n".join(
        ln for ln in body.splitlines() if not ln.strip().startswith("#")
    )
    assert "PRAGMA" not in code.upper(), (
        "column detection is using a SQLite-only PRAGMA again; on PostgreSQL it "
        "raises, the error is swallowed as a warning, and the ALTER never runs"
    )
    assert "get_columns" in body
