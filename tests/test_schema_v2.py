"""The v2 physical schema: `costs`, partitioned by month.

These tests exist because the whole design rests on two claims that are easy to
assert and easy to get subtly wrong: that partition bounds are contiguous and
half-open so no charge date can fall between two months or into two at once, and
that retention drops exactly the months on the old side of a cutoff and nothing
else. A partitioning bug does not raise -- it silently routes rows to the default
partition, or drops a month that was still in retention.

Skipped unless CLOUDLENS_PG_TEST names a server, like the other Postgres tests.
"""
from __future__ import annotations

import sys as _sys
from pathlib import Path as _Path

_sys.path.insert(0, str(_Path(__file__).resolve().parents[1]))

import datetime as dt  # noqa: E402
import os  # noqa: E402
import uuid  # noqa: E402

import pytest  # noqa: E402

pytestmark = pytest.mark.skipif(
    not os.getenv("CLOUDLENS_PG_TEST"),
    reason="set CLOUDLENS_PG_TEST=<host> to run against a PostgreSQL server",
)

if os.getenv("CLOUDLENS_PG_TEST"):
    os.environ["PGHOST"] = os.environ["CLOUDLENS_PG_TEST"]
    os.environ["PGDATABASE"] = os.getenv("CLOUDLENS_PG_DB", "postgres")
    os.environ["PGUSER"] = os.environ["CLOUDLENS_PG_USER"]
    os.environ.setdefault("PGSCHEMA", f"cl_sch_{uuid.uuid4().hex[:8]}")


# ------------------------------------------------------------------ pure helpers

def test_month_bounds_are_contiguous_and_half_open():
    """Every day belongs to exactly one month, with no gap and no overlap.

    The property that makes RANGE partitioning safe. If `next_month` were ever a
    day early the last day of a month would have no partition and land in the
    default; if it were a day late two partitions would claim the same date and
    PostgreSQL would refuse to attach the second.
    """
    from app import schema_v2

    day = dt.date(2025, 11, 1)
    previous_upper = None
    while day < dt.date(2027, 3, 1):
        lo = schema_v2.month_start(day)
        hi = schema_v2.next_month(day)
        assert lo <= day < hi
        assert lo.day == 1 and hi.day == 1
        if previous_upper is not None:
            assert lo == previous_upper, "a gap or overlap between adjacent months"
        previous_upper = hi
        day = hi


@pytest.mark.parametrize("day,expected", [
    (dt.date(2026, 1, 31), dt.date(2026, 2, 1)),   # 31-day month
    (dt.date(2026, 2, 28), dt.date(2026, 3, 1)),   # short month
    (dt.date(2028, 2, 29), dt.date(2028, 3, 1)),   # leap day
    (dt.date(2026, 12, 15), dt.date(2027, 1, 1)),  # year boundary
])
def test_next_month_handles_awkward_dates(day, expected):
    from app import schema_v2

    assert schema_v2.next_month(day) == expected


def test_partition_names_sort_chronologically():
    """`drop_partitions_before` compares the YYYYMM suffix as a string.

    That only selects the right partitions if lexicographic order matches
    chronological order, which is why the month is zero-padded.
    """
    from app import schema_v2

    days = [dt.date(2026, m, 1) for m in range(1, 13)] + [dt.date(2027, 1, 1)]
    names = [schema_v2.partition_name(d) for d in days]
    assert names == sorted(names)
    assert schema_v2.partition_name(dt.date(2026, 9, 1)).endswith("_202609")


def test_the_schema_builds_the_table_readers_already_name():
    """No view, no fact table, no dimensions -- `costs` is the relation itself.

    The application has 25 modules naming `costs`. Renaming it, or replacing it
    with a view over something else, is what this design deliberately does not do.
    """
    from app import schema_v2
    from app.warehouse import COLUMNS, NUMERIC

    assert schema_v2.TABLE == "costs"
    statements = schema_v2.create_sql("s", list(COLUMNS), NUMERIC)
    joined = "\n".join(statements)
    assert 'PARTITION BY RANGE ("ChargePeriodStart")' in joined
    assert "CREATE VIEW" not in joined.upper()
    assert "TRIGGER" not in joined.upper()
    assert "dim_resource" not in joined
    assert "dim_tagset" not in joined
    # Every warehouse column survives into the partitioned table.
    for column in COLUMNS:
        assert f'"{column}"' in statements[0]


# ------------------------------------------------------------------ against PostgreSQL

@pytest.fixture()
def con():
    """A connection and a schema of its own, dropped afterwards."""
    import psycopg

    schema = f"cl_sch_{uuid.uuid4().hex[:8]}"
    raw = psycopg.connect(
        host=os.environ["PGHOST"],
        port=int(os.getenv("PGPORT", "5432")),
        dbname=os.environ["PGDATABASE"],
        user=os.environ["PGUSER"],
        password=os.getenv("PGPASSWORD"),
        sslmode=os.getenv("PGSSLMODE", "require"),
        autocommit=True,
    )

    class Con:
        def execute(self, sql, params=None):
            cur = raw.cursor()
            cur.execute(sql.replace("?", "%s"), params or None)
            return cur

    c = Con()
    c.execute(f'CREATE SCHEMA "{schema}"')
    c.schema = schema
    yield c
    c.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')
    raw.close()


@pytest.fixture()
def built(con):
    from app import schema_v2
    from app.warehouse import COLUMNS, NUMERIC

    for statement in schema_v2.create_sql(con.schema, list(COLUMNS), NUMERIC):
        con.execute(statement)
    return con


def _insert(con, schema, day, cost=1.0):
    con.execute(
        f'INSERT INTO "{schema}".costs ("ChargePeriodStart", "BilledCost") VALUES (?, ?)',
        [day, cost])


def test_rows_route_to_the_partition_for_their_month(built):
    from app import schema_v2

    schema = built.schema
    schema_v2.ensure_partitions(built, schema, dt.date(2026, 1, 5), dt.date(2026, 3, 20))

    _insert(built, schema, dt.date(2026, 1, 31))
    _insert(built, schema, dt.date(2026, 2, 1))
    _insert(built, schema, dt.date(2026, 3, 20))

    for month, expected in (("202601", 1), ("202602", 1), ("202603", 1)):
        got = built.execute(
            f'SELECT count(*) FROM "{schema}".costs_{month}').fetchone()[0]
        assert got == expected, f"costs_{month} holds {got} rows, expected {expected}"

    # Nothing fell through to the default partition.
    assert built.execute(
        f'SELECT count(*) FROM "{schema}".costs_default').fetchone()[0] == 0


def test_a_month_with_no_partition_is_kept_not_rejected(built):
    """The default partition is a safety net, not decoration.

    Without it an insert for an uncreated month raises and the charge is lost.
    With it the charge is stored and merely queried more slowly.
    """
    from app import schema_v2

    schema = built.schema
    schema_v2.ensure_partitions(built, schema, dt.date(2026, 1, 1), dt.date(2026, 1, 31))

    _insert(built, schema, dt.date(2026, 7, 4))

    assert built.execute(
        f'SELECT count(*) FROM "{schema}".costs_default').fetchone()[0] == 1
    assert built.execute(f'SELECT count(*) FROM "{schema}".costs').fetchone()[0] == 1


def test_ensure_partitions_is_repeatable(built):
    from app import schema_v2

    schema = built.schema
    first = schema_v2.ensure_partitions(built, schema, dt.date(2026, 1, 1), dt.date(2026, 4, 1))
    assert len(first) == 4
    _insert(built, schema, dt.date(2026, 2, 14))

    again = schema_v2.ensure_partitions(built, schema, dt.date(2026, 1, 1), dt.date(2026, 4, 1))
    assert again == first
    # Re-running did not replace a partition and discard its rows.
    assert built.execute(f'SELECT count(*) FROM "{schema}".costs').fetchone()[0] == 1


def test_retention_drops_only_months_before_the_cutoff(built):
    from app import schema_v2

    schema = built.schema
    schema_v2.ensure_partitions(built, schema, dt.date(2026, 1, 1), dt.date(2026, 5, 1))
    for month in range(1, 6):
        _insert(built, schema, dt.date(2026, month, 10), cost=float(month))

    assert built.execute(f'SELECT count(*) FROM "{schema}".costs').fetchone()[0] == 5

    dropped = schema_v2.drop_partitions_before(built, schema, dt.date(2026, 3, 1))

    assert dropped == ["costs_202601", "costs_202602"]
    remaining = built.execute(
        f'SELECT "ChargePeriodStart" FROM "{schema}".costs ORDER BY 1').fetchall()
    assert [r[0] for r in remaining] == [
        dt.date(2026, 3, 10), dt.date(2026, 4, 10), dt.date(2026, 5, 10)]


def test_retention_never_drops_the_default_partition(built):
    """It may hold rows from any month, including months after the cutoff."""
    from app import schema_v2

    schema = built.schema
    schema_v2.ensure_partitions(built, schema, dt.date(2026, 1, 1), dt.date(2026, 2, 1))
    _insert(built, schema, dt.date(2026, 1, 10))
    _insert(built, schema, dt.date(2026, 11, 10))  # no partition -> default

    schema_v2.drop_partitions_before(built, schema, dt.date(2026, 6, 1))

    assert "costs_default" in schema_v2.attached_partitions(built, schema)
    survivors = [r[0] for r in built.execute(
        f'SELECT "ChargePeriodStart" FROM "{schema}".costs ORDER BY 1').fetchall()]
    assert survivors == [dt.date(2026, 11, 10)]


def test_a_cutoff_before_all_data_drops_nothing(built):
    from app import schema_v2

    schema = built.schema
    schema_v2.ensure_partitions(built, schema, dt.date(2026, 1, 1), dt.date(2026, 2, 1))
    _insert(built, schema, dt.date(2026, 1, 10))

    assert schema_v2.drop_partitions_before(built, schema, dt.date(2025, 1, 1)) == []
    assert built.execute(f'SELECT count(*) FROM "{schema}".costs').fetchone()[0] == 1


def test_total_size_counts_partitions_not_the_empty_parent(built):
    """pg_total_relation_size on a partitioned parent reports zero.

    Asking it directly is how a size comparison produces a triumphant and
    completely fictional reduction, so the helper sums the partitions instead.
    """
    from app import schema_v2

    schema = built.schema
    schema_v2.ensure_partitions(built, schema, dt.date(2026, 1, 1), dt.date(2026, 2, 1))
    for day in range(1, 28):
        _insert(built, schema, dt.date(2026, 1, day))

    parent_only = built.execute(
        "SELECT pg_total_relation_size(?)", [f'"{schema}".costs']).fetchone()[0]
    counted = schema_v2.total_size(built, schema)

    assert parent_only == 0
    assert counted > 0


# ------------------------------------------------------- the warehouse write path

@pytest.fixture()
def partitioned_wh(monkeypatch):
    """A warehouse whose `costs` is partitioned, in a schema of its own.

    `PARTITION_COSTS` is read once at import, so it is patched on the module
    rather than in the environment -- setting the variable after import would do
    nothing and the test would silently exercise the flat path instead.
    """
    from app import warehouse as wh_module

    schema = f"cl_part_{uuid.uuid4().hex[:8]}"
    monkeypatch.setenv("PGSCHEMA", schema)
    monkeypatch.setattr(wh_module, "PARTITION_COSTS", True)

    warehouse = wh_module.Warehouse()
    yield warehouse

    with warehouse._store.connect() as c:
        c.execute(f"DROP SCHEMA IF EXISTS {schema} CASCADE")
    warehouse._store.close()


def _rows_for(days):
    from app.warehouse import COLUMNS, NUMERIC

    out = []
    for i, day in enumerate(days):
        values = {
            "ChargePeriodStart": day,
            "BilledCost": 10.0,
            "CostInUsd": 10.0,
            "BillingCurrency": "USD",
            "SubAccountId": str(uuid.UUID(int=1)),
            "SubAccountName": "sub-1",
            "ServiceName": "Virtual Machines",
            "ServiceFamily": "Compute",
            "RegionName": "eastus",
            "ResourceGroup": "rg-1",
            "ResourceId": f"/subscriptions/x/rg-1/res-{i}",
        }
        out.append([values.get(c, 0.0 if c in NUMERIC else None) for c in COLUMNS])
    return out


def test_the_warehouse_creates_costs_partitioned_when_enabled(partitioned_wh):
    from app import schema_v2

    schema = partitioned_wh._rollup_schema
    with partitioned_wh.connect() as c:
        kind = c.execute(
            "SELECT relkind FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace "
            "WHERE c.relname = 'costs' AND n.nspname = ?", [schema]).fetchone()[0]
        assert kind == "p"
        # The safety net must exist from the start, not on first overflow.
        assert "costs_default" in schema_v2.attached_partitions(c, schema)


def test_inserting_rows_creates_the_partitions_they_need(partitioned_wh):
    """The actual wiring. Without it every charge lands in the default partition.

    That failure is silent -- the rows are all present and every total is right --
    so it is only detectable by looking at where they were stored.
    """
    from app import schema_v2
    from app.warehouse import insert_rows

    schema = partitioned_wh._rollup_schema
    days = [dt.date(2026, 1, 15), dt.date(2026, 2, 20), dt.date(2026, 3, 5)]

    with partitioned_wh.connect() as c:
        insert_rows(c, _rows_for(days))

        attached = schema_v2.attached_partitions(c, schema)
        for month in ("202601", "202602", "202603"):
            assert f"costs_{month}" in attached, f"no partition for {month}"
            assert c.execute(
                f'SELECT count(*) FROM "{schema}".costs_{month}').fetchone()[0] == 1

        assert c.execute(
            f'SELECT count(*) FROM "{schema}".costs_default').fetchone()[0] == 0
        assert c.execute(f'SELECT count(*) FROM "{schema}".costs').fetchone()[0] == 3


def test_iso_string_dates_route_as_well_as_date_objects(partitioned_wh):
    """CSV paths hand over '2026-04-05'; API paths hand over a date. Both must route."""
    from app import schema_v2
    from app.warehouse import insert_rows

    schema = partitioned_wh._rollup_schema
    with partitioned_wh.connect() as c:
        insert_rows(c, _rows_for(["2026-04-05", "2026-05-06"]))

        attached = schema_v2.attached_partitions(c, schema)
        assert "costs_202604" in attached and "costs_202605" in attached
        assert c.execute(
            f'SELECT count(*) FROM "{schema}".costs_default').fetchone()[0] == 0


def test_retention_and_the_rollup_still_agree_on_a_partitioned_table(partitioned_wh):
    """End to end: write, roll up, drop a month, roll up again, totals still match."""
    from app import rollup, schema_v2
    from app.warehouse import insert_rows

    schema = partitioned_wh._rollup_schema
    days = [dt.date(2026, 1, 10), dt.date(2026, 2, 10), dt.date(2026, 3, 10)]
    with partitioned_wh.connect() as c:
        insert_rows(c, _rows_for(days))
    partitioned_wh.refresh_rollup()

    with partitioned_wh.reader() as r:
        detail = r.rows('SELECT round(sum("BilledCost"), 2) AS c FROM costs')[0]["c"]
        rolled = r.rows(f'SELECT round(sum("BilledCost"), 2) AS c FROM {rollup.TABLE}')[0]["c"]
    assert detail == pytest.approx(rolled, abs=0.01)

    with partitioned_wh.connect() as c:
        dropped = schema_v2.drop_partitions_before(c, schema, dt.date(2026, 2, 1))
    assert dropped == ["costs_202601"]

    partitioned_wh.refresh_rollup()
    with partitioned_wh.reader() as r:
        detail = r.rows('SELECT round(sum("BilledCost"), 2) AS c FROM costs')[0]["c"]
        rolled = r.rows(f'SELECT round(sum("BilledCost"), 2) AS c FROM {rollup.TABLE}')[0]["c"]
    assert detail == pytest.approx(rolled, abs=0.01)
    assert detail == pytest.approx(20.0, abs=0.01), "a month should have been forgotten"


def test_a_flat_costs_table_is_left_alone(monkeypatch):
    """The flag must not convert, move or rewrite an existing flat table.

    A database created before partitioning keeps the table it has. The insert path
    reads the catalog rather than the flag, so it simply does no partition work.
    """
    from app import warehouse as wh_module
    from app.warehouse import insert_rows

    schema = f"cl_flat_{uuid.uuid4().hex[:8]}"
    monkeypatch.setenv("PGSCHEMA", schema)
    monkeypatch.setattr(wh_module, "PARTITION_COSTS", False)
    flat = wh_module.Warehouse()
    try:
        # Now the flag flips on, as it would if an operator enabled it later.
        monkeypatch.setattr(wh_module, "PARTITION_COSTS", True)
        wh_module.Warehouse()  # a second boot against the same schema

        with flat.connect() as c:
            kind = c.execute(
                "SELECT relkind FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace "
                "WHERE c.relname = 'costs' AND n.nspname = ?", [schema]).fetchone()[0]
            assert kind == "r", "an existing flat table was converted"

            insert_rows(c, _rows_for([dt.date(2026, 1, 10), dt.date(2026, 2, 10)]))
            assert c.execute(f'SELECT count(*) FROM "{schema}".costs').fetchone()[0] == 2
    finally:
        with flat._store.connect() as c:
            c.execute(f"DROP SCHEMA IF EXISTS {schema} CASCADE")
        flat._store.close()
