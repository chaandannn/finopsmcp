"""The first-run flow offers a monthly budget seeded from the scanned spend.

This is the activation step: it heads off the find-out-the-hard-way bill and sets
the number every agent checks against before it acts. Guards the suggestion math,
persistence, the skip path, and the no-nag-if-one-exists behavior.
"""
import builtins

import pytest

import finops.welcome as w
from finops.budget.enforcer import list_budgets


@pytest.fixture
def isolated_db(monkeypatch, tmp_path):
    import finops.storage.db as db
    monkeypatch.setenv("FINOPS_DATA_DIR", str(tmp_path))
    monkeypatch.delenv("FINOPS_PROFILE", raising=False)
    monkeypatch.delenv("DATABASE_URL", raising=False)
    # data_dir() and get_engine() both cache globally; reset both so this test
    # gets its own SQLite in tmp_path. monkeypatch restores the originals after.
    if db._ENGINE is not None:
        try:
            db._ENGINE.dispose()
        except Exception:
            pass
    monkeypatch.setattr(db, "_ENGINE", None)
    monkeypatch.setattr(db, "_DATA_DIR", None)
    yield tmp_path
    if db._ENGINE is not None:
        try:
            db._ENGINE.dispose()
        except Exception:
            pass


def _totals():
    return [b for b in list_budgets(active_only=True) if b.get("scope_type") == "total"]


def test_nice_budget_rounds_to_legible_numbers():
    assert w._nice_budget(0) == 0
    assert w._nice_budget(47) == 50
    assert w._nice_budget(240) == 300
    assert w._nice_budget(13940 * 1.15) == 17000  # 15% headroom, up to nearest 1000
    assert w._nice_budget(250000) == 250000


def test_accepts_suggested_budget(monkeypatch, isolated_db):
    w._LAST_TOTAL[0] = 13940.0
    monkeypatch.setattr(builtins, "input", lambda *a, **k: "")  # Enter = accept default
    w._offer_budget_guardrail()
    b = _totals()
    assert len(b) == 1
    assert b[0]["limit_usd"] == 17000.0
    assert b[0]["period"] == "monthly"
    assert b[0]["alert_at_pct"] == 80.0


def test_custom_amount_parsed(monkeypatch, isolated_db):
    w._LAST_TOTAL[0] = 5000.0
    monkeypatch.setattr(builtins, "input", lambda *a, **k: "$8,500")
    w._offer_budget_guardrail()
    b = _totals()
    assert len(b) == 1 and b[0]["limit_usd"] == 8500.0


def test_skip_sets_nothing(monkeypatch, isolated_db):
    w._LAST_TOTAL[0] = 5000.0
    monkeypatch.setattr(builtins, "input", lambda *a, **k: "n")
    w._offer_budget_guardrail()
    assert _totals() == []


def test_no_prompt_when_total_budget_exists(monkeypatch, isolated_db):
    from finops.budget.enforcer import create_budget
    create_budget("Existing", "total", 1000.0, period="monthly")
    w._LAST_TOTAL[0] = 5000.0
    calls = {"n": 0}

    def _boom(*a, **k):
        calls["n"] += 1
        return ""

    monkeypatch.setattr(builtins, "input", _boom)
    w._offer_budget_guardrail()
    assert calls["n"] == 0  # never prompted
    assert len(_totals()) == 1  # only the pre-existing one


def test_no_op_without_a_scanned_total(monkeypatch, isolated_db):
    w._LAST_TOTAL[0] = 0.0
    calls = {"n": 0}
    monkeypatch.setattr(builtins, "input", lambda *a, **k: calls.__setitem__("n", calls["n"] + 1) or "")
    w._offer_budget_guardrail()
    assert calls["n"] == 0 and _totals() == []


def test_legacy_block_at_pct_column_is_migrated_away(tmp_path, monkeypatch):
    """Regression: old DBs had budgets.block_at_pct NOT NULL (no default). The current
    model dropped it, so onboarding's create_budget INSERT hit the NOT NULL constraint
    and dumped a traceback on a fresh `nable` run for anyone who upgraded. The engine's
    migrations must drop the orphan so create_budget works."""
    import sqlite3
    monkeypatch.setenv("FINOPS_DATA_DIR", str(tmp_path))
    dbp = tmp_path / "finops.db"
    con = sqlite3.connect(dbp)
    con.execute(
        "CREATE TABLE budgets ("
        "id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL, scope_type TEXT NOT NULL,"
        "scope_value TEXT NOT NULL DEFAULT '*', period TEXT NOT NULL DEFAULT 'monthly',"
        "limit_usd REAL NOT NULL, block_at_pct REAL NOT NULL, created_at TEXT NOT NULL,"
        "updated_at TEXT NOT NULL, created_by TEXT NOT NULL DEFAULT '', is_active INTEGER NOT NULL DEFAULT 1)"
    )
    con.commit()
    con.close()

    # Fresh engine per test dir: import lazily and reset the module-level singleton.
    import importlib
    from finops.storage import db as _db
    importlib.reload(_db)
    from sqlalchemy import text
    eng = _db.get_engine()  # runs migrations, must drop the orphan column
    with eng.connect() as c:
        cols = {r[1] for r in c.execute(text("PRAGMA table_info(budgets)")).fetchall()}
    assert "block_at_pct" not in cols, "legacy block_at_pct must be dropped by migration"

    from finops.budget.enforcer import create_budget
    b = create_budget(name="Monthly budget", scope_type="total", limit_usd=20000.0,
                      period="monthly", created_by="onboarding")
    assert b["limit_usd"] == 20000.0
