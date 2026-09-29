"""Offline regressions for non-destructive normalization and scoped pool checkout."""
from __future__ import annotations

import ast
import importlib.util
import json
import runpy
import sqlite3
import sys
import threading
from collections import Counter
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, Mock

import pytest

pytestmark = pytest.mark.offline


@pytest.fixture(scope="module")
def offline_warehouse():
    path = Path(__file__).resolve().parents[1] / "app" / "warehouse.py"
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(
                isinstance(target, ast.Name) and target.id == "warehouse" for target in node.targets):
            node.value = ast.parse("Warehouse.__new__(Warehouse)", mode="eval").body
    name = "app._datastore_safety_fixture"
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    exec(compile(ast.fix_missing_locations(tree), str(path), "exec"), module.__dict__)
    yield module
    sys.modules.pop(name, None)


class MemoryConnection(sqlite3.Connection):
    fail_notice = False

    def execute(self, sql, parameters=()):
        if sql == "LOCK TABLE costs IN SHARE ROW EXCLUSIVE MODE":
            return super().execute("SELECT 1")
        if self.fail_notice and sql.startswith("INSERT INTO meta"):
            raise RuntimeError("notice persistence failed")
        return super().execute(sql, parameters)


@pytest.fixture
def memory():
    db = sqlite3.connect(":memory:", isolation_level=None, factory=MemoryConnection)
    db.create_function("now", 0, lambda: "2026-09-14")
    db.execute('CREATE TABLE costs ("SubAccountId", "ChargePeriodStart", "ResourceId", '
               '"BilledCost" REAL, "BillingCurrency", "ChargeCategory", "Tags")')
    db.execute("CREATE TABLE meta (key PRIMARY KEY, value, updated_at)")
    yield db
    db.close()


def test_normalization_preserves_every_charge_and_records_reingest(offline_warehouse, memory):
    canonical = ("sub-a", "2026-09-01", "/vm/a", 100, "USD", "Usage", "{}")
    rows = [
        canonical, canonical,
        ("sub-a", "2026-09-01", "/vm/a", 100, "EUR", "Adjustment", "{}"),
        ("sub-a", "2026-09-01", "/vm/a", 100, "USD", "Usage", '{"env":"test"}'),
        ("/subscriptions/SUB-A", "2026-09-01", "/vm/a", 100, "USD", "Usage", "{}"),
        ("/subscriptions/SUB-A/", "2026-09-02", "/vm/a", -10, "USD", "Credit", "{}"),
        ("SUB-B", "2026-09-03", "/vm/b", 0, "USD", "Usage", "{}"),
    ]
    memory.executemany("INSERT INTO costs VALUES (?,?,?,?,?,?,?)", rows)
    wh = offline_warehouse.Warehouse.__new__(offline_warehouse.Warehouse)
    wh._merge_subscription_spellings(memory)
    expected = Counter((offline_warehouse.canonical_sub_id(row[0]), *row[1:]) for row in rows)
    assert Counter(memory.execute("SELECT * FROM costs").fetchall()) == expected
    notice = memory.execute("SELECT value FROM meta WHERE key='subscription_reconciliation'").fetchone()
    assert json.loads(notice[0]) == {
        "sub-a": {"from": "2026-09-01", "to": "2026-09-02"},
        "sub-b": {"from": "2026-09-03", "to": "2026-09-03"},
    }
    wh._merge_subscription_spellings(memory)
    assert Counter(memory.execute("SELECT * FROM costs").fetchall()) == expected
    assert memory.execute("SELECT value FROM meta WHERE key='subscription_reconciliation'").fetchone() == notice


def test_unrelated_identical_rows_survive_another_subscription_normalization(offline_warehouse, memory):
    rows = [("sub-a", "2026-09-01", "/vm/a", 100, "EUR", "Usage", "{}")] * 3
    rows += [("SUB-B", "2026-09-01", "/vm/b", 1, "USD", "Usage", "{}")]
    memory.executemany("INSERT INTO costs VALUES (?,?,?,?,?,?,?)", rows)
    offline_warehouse.Warehouse._merge_subscription_spellings(object(), memory)
    assert memory.execute('SELECT count(*) FROM costs WHERE "SubAccountId" = ?', ["sub-a"]).fetchone()[0] == 3


def test_normalization_and_notice_are_atomic(offline_warehouse, memory, caplog):
    row = ("/subscriptions/SUB-A", "2026-09-01", "/vm/a", 1, "USD", "Usage", "{}")
    memory.execute("INSERT INTO costs VALUES (?,?,?,?,?,?,?)", row)
    memory.fail_notice = True
    offline_warehouse.Warehouse._merge_subscription_spellings(object(), memory)
    assert memory.execute("SELECT * FROM costs").fetchall() == [row]
    assert memory.execute("SELECT count(*) FROM meta").fetchone()[0] == 0
    assert "notice persistence failed" in caplog.text


def test_reconciliation_notice_is_scoped_and_survives_summary(offline_warehouse, memory):
    memory.executemany("INSERT INTO costs VALUES (?,?,?,?,?,?,?)", [
        ("SUB-A", "2026-09-01", "/vm/a", 1, "USD", "Usage", "{}"),
        ("SUB-B", "2026-09-02", "/vm/b", 1, "USD", "Usage", "{}"),
    ])
    wh = offline_warehouse.Warehouse.__new__(offline_warehouse.Warehouse)
    wh._merge_subscription_spellings(memory)

    @contextmanager
    def connection(**kwargs):
        yield memory

    wh.connect = connection
    # `summary` reads through the `reader` seam so it can be answered by the rollup;
    # the default `Reader` names `costs`, which is the relation this fixture holds.
    @contextmanager
    def reading(*args, **kwargs):
        yield offline_warehouse.Reader(memory)

    wh.reader = reading
    result = wh.summary(scope=["/subscriptions/SUB-A"])
    assert result["reconciliation_required"]
    assert result["reconciliation"] == [
        {"subscription_id": "sub-a", "from": "2026-09-01", "to": "2026-09-01"}]
    assert not wh.summary(scope=["sub-c"])["reconciliation_required"]


def test_summary_does_not_label_unknown_plus_usd_as_single_currency(offline_warehouse, memory):
    memory.executemany("INSERT INTO costs VALUES (?,?,?,?,?,?,?)", [
        ("sub-a", "2026-09-01", "/vm/a", 1, "USD", "Usage", "{}"),
        ("sub-a", "2026-09-02", "/vm/b", 1, None, "Usage", "{}"),
    ])
    wh = offline_warehouse.Warehouse.__new__(offline_warehouse.Warehouse)

    @contextmanager
    def connection(**kwargs):
        yield memory

    wh.connect = connection
    @contextmanager
    def reading(*args, **kwargs):
        yield offline_warehouse.Reader(memory)

    wh.reader = reading
    result = wh.summary()
    assert result["mixed_currency"]
    assert result["currency"] is None and result["total_cost"] is None


def test_existing_subscription_identity_regression_offline(offline_warehouse, monkeypatch):
    db = sqlite3.connect(":memory:", isolation_level=None, factory=MemoryConnection)
    db.create_function("now", 0, lambda: "2026-09-14")
    db.execute("CREATE TABLE costs (" + ",".join(
        f'"{name}" {"REAL" if name in offline_warehouse.NUMERIC else "TEXT"}'
        for name in offline_warehouse.COLUMNS) + ")")
    db.execute("CREATE TABLE meta (key PRIMARY KEY, value, updated_at)")
    monkeypatch.setitem(sys.modules, "app.warehouse", offline_warehouse)
    monkeypatch.setattr(offline_warehouse.warehouse, "connect", lambda: db)
    try:
        script = Path(__file__).with_name("test_sub_identity.py")
        result = runpy.run_path(str(script))
        assert not result["failures"]
    finally:
        db.close()


def test_statement_timeout_is_reader_scoped_and_reset_on_release(offline_warehouse, monkeypatch):
    """The read limit must not outlive the checkout that asked for it.

    An application timeout cannot stop a database query -- cancelling the await leaves
    the thread, the connection and the query running -- so reads carry a server-side
    limit and an abandoned build dies instead of keeping its pool slot forever.

    The same pooled session is handed out later to a rebuild that legitimately runs for
    minutes, so the limit has to come off on release. `work_mem` and the search path do
    not: those belong to the connection rather than to one borrower.
    """
    from app import reporting
    from app.store import PostgresStore

    monkeypatch.setenv("PGHOST", "unused.invalid")
    monkeypatch.setenv("PG_STATEMENT_TIMEOUT", "110s")
    store = PostgresStore()

    statements: list[tuple[str, object]] = []
    con = Mock()
    con.execute.side_effect = lambda sql, params=None: (
        statements.append((sql, params)) or SimpleNamespace(fetchone=lambda: None))

    wh = offline_warehouse.Warehouse.__new__(offline_warehouse.Warehouse)
    wh._store, wh._pg_table = store, "public.costs"
    monkeypatch.setattr(reporting, "view_predicate", lambda: "")

    wh._prepare_connection(con, True, None, None, None)
    assert any("set_config" in sql and params == ["110s"] for sql, params in statements), \
        "a reader must carry the server-side limit"

    statements.clear()
    wh._prepare_connection(con, False, None, None, None)
    assert not any("statement_timeout" in sql for sql, _ in statements), \
        "a write connection must not inherit a limit meant for dashboard reads"

    reset: list[str] = []
    raw = MagicMock()
    raw.closed = False
    raw.info.transaction_status = 0
    raw.cursor.return_value.__enter__.return_value.execute.side_effect = reset.append
    store._return_connection(raw)
    assert "RESET statement_timeout" in reset
    assert "DISCARD TEMP" in reset
    assert not any("work_mem" in statement for statement in reset), \
        "work_mem belongs to the connection, not to one borrower"


@pytest.mark.parametrize("failure", [
    "rate", "period", "projection", "CREATE TEMP TABLE _scope", "INSERT INTO _scope",
    "CREATE TEMP TABLE _filter", "INSERT INTO _filter", "currency-sql",
    'SELECT DISTINCT "BillingCurrency"', "unknown-currency", "CREATE TEMP VIEW costs",
    "cancelled",
])
def test_every_setup_failure_returns_the_pool_slot(offline_warehouse, monkeypatch, failure):
    from app import currency, reporting
    from app.store import PostgresStore

    class Cancelled(BaseException):
        pass

    error = Cancelled("cancelled") if failure == "cancelled" else RuntimeError("setup failed")
    store = PostgresStore.__new__(PostgresStore)
    store._slots = threading.BoundedSemaphore(1)
    store._pool_timeout = 0
    con = Mock()
    con.close.side_effect = store._slots.release
    store._checkout = Mock(return_value=con)

    def execute(sql, params=None):
        if sql.startswith(failure) or (failure == "cancelled" and sql.startswith("CREATE TEMP VIEW")):
            raise error
        return SimpleNamespace(fetchone=lambda: ("ZZZ",) if failure == "unknown-currency" else None)

    con.execute.side_effect = execute
    wh = offline_warehouse.Warehouse.__new__(offline_warehouse.Warehouse)
    wh._store, wh._pg_table = store, "public.costs"
    monkeypatch.setattr(currency, "rate_to", Mock(side_effect=error) if failure == "rate" else lambda _: 1)
    monkeypatch.setattr(reporting, "view_predicate",
                        Mock(side_effect=error) if failure == "period" else lambda: "")
    monkeypatch.setattr(wh, "_columns_sql",
                        Mock(side_effect=error) if failure == "projection" else lambda *_, **__: "*")
    monkeypatch.setattr(wh, "_currency_sql",
                        Mock(side_effect=error) if failure == "currency-sql"
                        else lambda *_, **__: ("1", "1"))
    expected = ValueError if failure == "unknown-currency" else type(error)
    with pytest.raises(expected):
        wh.connect(read_only=True, scope=["sub-a"], currency="USD", filters={"service": ["Compute"]})
    con.close.assert_called_once()
    assert store._slots.acquire(blocking=False), "setup failure leaked the only slot"
    store._slots.release()


def test_reader_can_fetch_complete_sql_bounded_results(offline_warehouse, memory):
    memory.executemany("INSERT INTO costs VALUES (?,?,?,?,?,?,?)",
                       [("sub-a", "2026-09-01", "/vm/a", 1, "USD", "Usage", "{}")] * 201)
    reader = offline_warehouse.Reader(memory)
    assert len(reader.rows("SELECT * FROM costs")) == 200
    assert len(reader.rows("SELECT * FROM costs", None)) == 201
