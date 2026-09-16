"""The warehouse on PostgreSQL, against a real server.

Skipped unless CLOUDLENS_PG_TEST names one, so the ordinary test run needs no
database. The point of these is the things a unit test with a fake connection
cannot show: that the translated SQL is accepted by PostgreSQL, that COPY
round-trips hostile values unchanged, and that a pooled connection does not carry
one caller's subscription scope to the next.
"""

from __future__ import annotations

import sys as _sys
from pathlib import Path as _Path

# Tests live in tests/ and import the application from the checkout root.
_sys.path.insert(0, str(_Path(__file__).resolve().parents[1]))

import os
import uuid
from typing import Any

import pytest

pytestmark = pytest.mark.skipif(
    not os.getenv("CLOUDLENS_PG_TEST"),
    reason="set CLOUDLENS_PG_TEST=<host> to run against a PostgreSQL server",
)

# Set before anything imports app.warehouse, which builds a Warehouse at module scope and
# would otherwise either fail for want of PGHOST or, worse, create its tables in `public`
# on the shared server. A schema of this module's own means neither can happen.
if os.getenv("CLOUDLENS_PG_TEST"):
    os.environ["PGHOST"] = os.environ["CLOUDLENS_PG_TEST"]
    os.environ["PGDATABASE"] = os.getenv("CLOUDLENS_PG_DB", "postgres")
    os.environ["PGUSER"] = os.environ["CLOUDLENS_PG_USER"]
    os.environ["PGSCHEMA"] = f"cl_test_{uuid.uuid4().hex[:8]}"


@pytest.fixture()
def wh(monkeypatch):
    """A warehouse on PostgreSQL, in a schema of its own that is dropped afterwards.

    A schema per test rather than a database: creating a database on a burstable
    server takes seconds, and the isolation a schema gives is the isolation these
    tests need.

    `PGSCHEMA` is the application's own setting rather than a monkeypatched connection
    hook, so this exercises the same code a deployment sharing one server between
    environments would run.
    """
    schema = f"cl_test_{uuid.uuid4().hex[:8]}"
    monkeypatch.setenv("PGSCHEMA", schema)

    from app.warehouse import Warehouse

    warehouse = Warehouse()
    yield warehouse

    with warehouse._store.connect() as con:
        con.execute(f"DROP SCHEMA IF EXISTS {schema} CASCADE")
    warehouse._store.close()


@pytest.fixture()
def reboot():
    """Build a second warehouse over the same schema, and close its pool afterwards.

    The boot-time repairs — the subscription-spelling merge and the column-order check —
    run in `Warehouse.__init__`, so the only way to test them is to construct one. Each
    construction opens a connection pool of its own, and a pool nobody closes outlives the
    test: psycopg reports it as a connection deleted while still open, and against a
    burstable server enough of them will exhaust `max_connections`.
    """
    built: list[Any] = []

    def _build():
        from app.warehouse import Warehouse

        warehouse = Warehouse()
        built.append(warehouse)
        return warehouse

    yield _build
    for warehouse in built:
        warehouse._store.close()


def _row(sub: str, day: str, cost: float, **over):
    """One cost row in COLUMNS order."""
    from app.warehouse import COLUMNS, NUMERIC

    values = {
        "SubAccountId": sub,
        "SubAccountName": over.get("name", f"sub-{sub[:4]}"),
        "ChargePeriodStart": day,
        "BilledCost": cost,
        "BillingCurrency": over.get("currency", "USD"),
        "ServiceName": over.get("service", "Virtual Machines"),
        "ResourceId": over.get("resource", f"/rg/{sub}/vm1"),
    }
    values.update(over)
    return [values.get(c, 0.0 if c in NUMERIC else None) for c in COLUMNS]


def test_the_schema_is_created_and_reports_itself_empty(wh):
    assert wh.row_count() == 0
    assert wh.summary()["rows"] == 0


def test_rows_written_by_copy_come_back_intact(wh):
    from app.warehouse import insert_rows

    with wh.connect() as con:
        insert_rows(con, [_row("aaa", "2026-01-05", 10.0),
                          _row("bbb", "2026-01-06", 5.5)])

    assert wh.row_count() == 2
    summary = wh.summary()
    assert summary["rows"] == 2
    assert summary["total_cost"] == pytest.approx(15.5)


def test_copy_carries_hostile_values_unchanged(wh):
    """A comma, a newline, a quote and a NULL -- the four things a CSV loader loses."""
    from app.warehouse import insert_rows

    nasty = 'rg,with"quote\nand newline'
    with wh.connect() as con:
        insert_rows(con, [_row("aaa", "2026-01-05", 1.0, name=nasty, currency=None)])

    with wh.reader() as r:
        rows = r.rows('SELECT "SubAccountName", "BillingCurrency" FROM costs')
    assert rows[0]["SubAccountName"] == nasty
    assert rows[0]["BillingCurrency"] is None


def test_a_rounded_total_is_a_float_not_a_decimal(wh):
    """Decimal / float raises, and it would raise in a dashboard tab rather than here."""
    from app.warehouse import insert_rows

    with wh.connect() as con:
        insert_rows(con, [_row("aaa", "2026-01-05", 1.5)])

    cost = wh.subscriptions()[0]["cost"]
    assert isinstance(cost, float)
    assert cost / 2 == pytest.approx(0.75)


def test_scope_hides_other_subscriptions(wh):
    from app.warehouse import insert_rows

    with wh.connect() as con:
        insert_rows(con, [_row("aaa", "2026-01-05", 10.0),
                          _row("bbb", "2026-01-06", 90.0)])

    with wh.reader(scope=["aaa"]) as r:
        rows = r.rows('SELECT sum("BilledCost") AS total FROM costs')
    assert rows[0]["total"] == pytest.approx(10.0)


def test_a_scope_does_not_survive_into_the_next_connection(wh):
    """The pool must not hand one caller's scope to the next.

    Scope is enforced by a temp view that shadows `costs`. A pooled session that
    kept it would answer the next person's questions through the previous
    person's filter -- silently, and with a plausible number.
    """
    from app.warehouse import insert_rows

    with wh.connect() as con:
        insert_rows(con, [_row("aaa", "2026-01-05", 10.0),
                          _row("bbb", "2026-01-06", 90.0)])

    # Narrow the pool to one connection, so the next open must reuse this one.
    wh._store.max_idle = 1
    with wh.reader(scope=["aaa"]) as r:
        assert r.rows('SELECT sum("BilledCost") AS t FROM costs')[0]["t"] == pytest.approx(10.0)

    with wh.reader() as r:
        assert r.rows('SELECT sum("BilledCost") AS t FROM costs')[0]["t"] == pytest.approx(100.0)


def test_meta_survives_a_round_trip(wh):
    assert wh.get_meta("onboarding", "unset") == "unset"
    wh.set_meta("onboarding", "done")
    assert wh.get_meta("onboarding") == "done"
    wh.set_meta("onboarding", "again")
    assert wh.get_meta("onboarding") == "again"


def test_duplicate_spellings_preserve_charges_pending_authoritative_reingest(wh, reboot):
    """Matching rows alone cannot establish whether two charges are duplicates."""
    from app.warehouse import insert_rows

    with wh.connect() as con:
        insert_rows(con, [
            _row("/subscriptions/AAA", "2026-01-05", 10.0, resource="/rg/x/vm1"),
            _row("aaa", "2026-01-05", 10.0, resource="/rg/x/vm1"),
        ])

    # The repair runs at construction, so build a second warehouse over the same schema.
    again = reboot()
    assert again.row_count() == 2
    summary = again.summary()
    assert summary["total_cost"] == pytest.approx(20.0)
    assert summary["reconciliation_required"]
    assert summary["reconciliation"] == [
        {"subscription_id": "aaa", "from": "2026-01-05", "to": "2026-01-05"}]
    with again.connect() as con:
        assert con.execute('SELECT DISTINCT "SubAccountId" FROM costs').fetchall() == [("aaa",)]


def test_two_genuinely_different_charges_are_both_kept(wh, reboot):
    """The repair must not collapse rows that only look similar."""
    from app.warehouse import insert_rows

    with wh.connect() as con:
        insert_rows(con, [
            _row("/subscriptions/AAA", "2026-01-05", 10.0, resource="/rg/x/vm1"),
            _row("aaa", "2026-01-05", 10.0, resource="/rg/x/vm2"),
        ])

    again = reboot()
    assert again.row_count() == 2


def test_dashboard_native_currency_views_rank_before_bounds(wh):
    from app.dashboard import Dashboard
    from app.warehouse import insert_rows

    rows = [_row("aaa", "2026-09-10", 1, service=f"service-{i:03}",
                 ResourceGroup=f"group-{i:03}", ServiceFamily="Compute")
            for i in range(405)]
    rows += [
        _row("aaa", "2026-09-10", 1_000_000, service="zz-largest",
             ResourceGroup="zz-largest", ServiceFamily="Compute"),
        _row("aaa", "2026-08-10", 900_000, service="removed", ServiceFamily="Compute"),
        _row("bbb", "2026-09-10", 80, currency="EUR", ServiceFamily=None),
    ]
    with wh.connect() as con:
        insert_rows(con, rows)
    dashboard = Dashboard.__new__(Dashboard)
    dashboard.wh = wh
    result = dashboard.executive(["aaa", "bbb"], days=30)
    assert result["mixed_currency"] and result["kpis"]["total"] is None
    views = {view["currency"]: view for view in result["by_currency"]}
    assert views["USD"]["kpis"]["total"] == 1_000_405
    assert views["USD"]["resource_groups"][0]["name"] == "zz-largest"
    assert any(row["name"] == "removed" for row in views["USD"]["movers"])
    assert views["EUR"]["trend"]["values"] == [80]
    other = dashboard.section("other", ["bbb"], days=30)
    assert other["kpis"]["total"] == 80


def test_scoped_setup_failures_return_real_postgres_pool_slot(wh, monkeypatch):
    import threading

    monkeypatch.setattr(wh._store, "_slots", threading.BoundedSemaphore(1))
    monkeypatch.setattr(wh._store, "_pool_timeout", 0)
    with monkeypatch.context() as patch:
        def failed_filter(*args):
            raise RuntimeError("fixture setup failure")

        patch.setattr(wh, "_filter_sql", failed_filter)
        for _ in range(3):
            with pytest.raises(RuntimeError, match="fixture setup failure"), wh.reader(["aaa"]):
                pytest.fail("incomplete scoped setup must not yield a reader")
    with wh.reader(["aaa"]) as reader:
        assert reader.rows("SELECT 1 AS ok") == [{"ok": 1}]


def test_normalization_rolls_back_when_postgres_notice_write_fails(wh, monkeypatch):
    from app.warehouse import insert_rows

    with wh.connect() as con:
        insert_rows(con, [_row("/subscriptions/AAA", "2026-09-10", 10)])
        execute = con.execute

        def fail_notice(sql, params=None):
            if sql.startswith("INSERT INTO meta"):
                raise RuntimeError("fixture notice failure")
            return execute(sql, params)

        with monkeypatch.context() as patch:
            patch.setattr(con, "execute", fail_notice)
            wh._merge_subscription_spellings(con)
        assert con.execute('SELECT "SubAccountId", "BilledCost" FROM costs').fetchall() == [
            ("/subscriptions/AAA", 10.0)]


def test_a_costs_table_in_another_schema_does_not_destroy_this_one(wh, reboot):
    """The boot-time column check must look only at its own table.

    It counts the columns of `costs` and, if the order does not match the code,
    concludes the stored rows have values shifted between columns and drops the
    table to rebuild it. Counting every `costs` the role can see turned that
    remedy into silent, total data loss on every boot -- and a database CloudLens
    shares with anything else is the normal case at a customer.
    """
    from app.warehouse import insert_rows

    with wh.connect() as con:
        insert_rows(con, [_row("aaa", "2026-01-05", 10.0)])
        con.execute("CREATE TABLE IF NOT EXISTS public.costs (unrelated text)")

    try:
        again = reboot()
        assert again.row_count() == 1, "the warehouse dropped its own table"
    finally:
        with wh._store.connect() as con:
            con.execute("DROP TABLE IF EXISTS public.costs")


def test_the_translated_dashboard_shapes_execute(wh):
    """The constructs the translator exists for, run against the real server."""
    from app.warehouse import insert_rows

    with wh.connect() as con:
        insert_rows(con, [_row("aaa", "2026-01-05", 10.0),
                          _row("aaa", "2026-02-05", 20.0)])

    with wh.reader() as r:
        months = r.rows(
            'SELECT strftime("ChargePeriodStart", \'%Y-%m\') AS month, '
            'round(sum("BilledCost"), 2) AS cost '
            "FROM costs GROUP BY 1 ORDER BY 1")
    assert [m["month"] for m in months] == ["2026-01", "2026-02"]
    assert months[1]["cost"] == pytest.approx(20.0)


def test_the_archive_can_be_written_from_postgres(wh, tmp_path):
    """Archiving used to be impossible on this backend, and said so only in a log line.

    The writer issued `COPY (...) TO '<file>' (FORMAT PARQUET)`, which is an embedded-engine
    extension: PostgreSQL refuses a server-side file write to anyone who is not a
    superuser -- which nobody is on Azure Flexible Server -- and has no Parquet format at
    all. So the deployed app archived nothing, and the failure looked like an empty
    archive rather than a broken one.
    """
    import pyarrow.parquet as pq

    from app import archive
    from app.warehouse import insert_rows

    with wh.connect() as con:
        insert_rows(con, [_row("aaa", "2026-01-05", 10.0, service="Storage"),
                          _row("bbb", "2026-01-06", 5.5, service="Compute")])

    destination = tmp_path / "archive.parquet"
    with wh.connect() as con:
        rows, size = archive._write_rows(destination, "parquet", source_sql="costs",
                                         scope=None, since=None, until=None, con=con)

    assert rows == 2
    assert size > 0

    table = pq.read_table(destination)
    assert table.num_rows == 2
    costs = sorted(c for c in table["BilledCost"].to_pylist())
    assert costs == pytest.approx([5.5, 10.0])
    assert sorted(table["ServiceName"].to_pylist()) == ["Compute", "Storage"]


def test_a_csv_archive_is_written_from_postgres(wh, tmp_path):
    """The CSV path shared the same `COPY ... TO` problem, so it is proven too."""
    import csv as _csv

    from app import archive
    from app.warehouse import insert_rows

    with wh.connect() as con:
        insert_rows(con, [_row("aaa", "2026-01-05", 10.0, name='has,comma "and" quote')])

    destination = tmp_path / "archive.csv"
    with wh.connect() as con:
        rows, _ = archive._write_rows(destination, "csv", source_sql="costs",
                                      scope=None, since=None, until=None, con=con)

    assert rows == 1
    with destination.open(newline="", encoding="utf-8") as fh:
        read = list(_csv.DictReader(fh))
    assert len(read) == 1
    # The quoting survives a round trip, which is the thing a hand-rolled writer loses.
    assert read[0]["SubAccountName"] == 'has,comma "and" quote'
    assert float(read[0]["BilledCost"]) == pytest.approx(10.0)
