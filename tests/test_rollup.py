"""The rollup must agree with the detail table, exactly.

This is the only property that matters. A rollup that is fast and slightly wrong
is worse than no rollup: CloudLens exists to explain spend to a finance team, and
a number that is quietly 3% off is one nobody can catch by looking at it.

So every test here computes the same figure twice -- once from `costs` and once
from `costs_rollup` -- and requires them to match. Skipped unless
CLOUDLENS_PG_TEST names a server, as test_postgres.py is.
"""

from __future__ import annotations

import sys as _sys
from pathlib import Path as _Path

# Tests live in tests/ and import the application from the checkout root.
_sys.path.insert(0, str(_Path(__file__).resolve().parents[1]))

import os
import random
import uuid

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
    os.environ["PGSCHEMA"] = f"cl_roll_{uuid.uuid4().hex[:8]}"

SERVICES = ["Virtual Machines", "Storage", "SQL Database", "Bandwidth"]
FAMILIES = {"Virtual Machines": "Compute", "Storage": "Storage",
            "SQL Database": "Databases", "Bandwidth": "Networking"}
REGIONS = ["eastus", "westeurope", "centralindia"]
CURRENCIES = ["USD", "EUR"]


@pytest.fixture()
def wh(monkeypatch):
    """A warehouse in a schema of its own, dropped afterwards.

    A schema per test rather than a database: creating a database on a burstable server
    takes seconds, and the isolation a schema gives is the isolation these tests need.
    `PGSCHEMA` is the application's own setting, so this exercises the same code a
    deployment sharing one server between environments would run.
    """
    schema = f"cl_roll_{uuid.uuid4().hex[:8]}"
    monkeypatch.setenv("PGSCHEMA", schema)

    from app.warehouse import Warehouse

    warehouse = Warehouse()
    yield warehouse

    with warehouse._store.connect() as con:
        con.execute(f"DROP SCHEMA IF EXISTS {schema} CASCADE")
    warehouse._store.close()


def _messy_rows(count: int = 4000):
    """Rows with the awkward values real billing data has.

    NULL regions and resource groups matter: NULL is not equal to NULL in SQL, so
    a GROUP BY that treats them as one bucket and one that treats them as many
    give different answers. The rollup has to make the same choice the detail
    query makes, and the only way to know it does is to include them.
    """
    from app.warehouse import COLUMNS, NUMERIC

    random.seed(11)
    subs = [str(uuid.UUID(int=i)) for i in range(6)]
    out = []
    for i in range(count):
        service = SERVICES[i % len(SERVICES)]
        sub = subs[i % len(subs)]
        values = {
            "SubAccountId": sub,
            "SubAccountName": f"sub-{i % len(subs)}",
            "ChargePeriodStart": f"2026-0{1 + (i % 3)}-{1 + (i % 28):02d}",
            "BilledCost": round(random.uniform(0.001, 900.0), 4),
            "EffectiveCost": round(random.uniform(0.001, 900.0), 4),
            "CostInUsd": round(random.uniform(0.001, 900.0), 4),
            "BillingCurrency": CURRENCIES[i % len(CURRENCIES)],
            "ServiceName": service,
            "ServiceFamily": FAMILIES[service],
            # Every fifth row has no region and no resource group.
            "RegionName": None if i % 5 == 0 else REGIONS[i % len(REGIONS)],
            "ResourceGroup": None if i % 5 == 0 else f"rg-{i % 30}",
            "ResourceId": f"/subscriptions/{sub}/rg-{i % 30}/res-{i % 400}",
            "MeterName": f"meter-{i % 20}",
        }
        out.append([values.get(c, 0.0 if c in NUMERIC else None) for c in COLUMNS])
    return out


@pytest.fixture()
def loaded(wh):
    from app.warehouse import insert_rows

    rows = _messy_rows()
    with wh.connect() as con:
        insert_rows(con, rows)
    wh.refresh_rollup()
    return wh


def _both(wh, group_by: str, where: str = ""):
    """The same aggregation from the detail table and from the rollup."""
    from app.rollup import TABLE

    clause = f"WHERE {where}" if where else ""
    detail = (f'SELECT {group_by}, round(sum("BilledCost"), 2) AS cost '
              f"FROM costs {clause} GROUP BY 1 ORDER BY 1")
    rolled = (f'SELECT {group_by}, round(sum("BilledCost"), 2) AS cost '
              f"FROM {TABLE} {clause} GROUP BY 1 ORDER BY 1")
    with wh.reader() as r:
        return r.rows(detail, limit=2000), r.rows(rolled, limit=2000)


def test_the_rollup_actually_built(loaded):
    """Guard against the whole suite passing vacuously.

    `refresh_rollup` logs and swallows failures by design, so a rollup that never
    built leaves an empty table -- and an empty table trivially "matches" nothing.
    Every equality test below is only meaningful if this one passes first.
    """
    with loaded.reader() as r:
        n = r.rows("SELECT count(*) AS n FROM costs_rollup")[0]["n"]
    assert n > 0, "the rollup is empty; every equality test below proves nothing"


def test_the_rollup_is_smaller_than_the_detail_table(loaded):
    from app.rollup import TABLE

    with loaded.reader() as r:
        detail = r.rows("SELECT count(*) AS n FROM costs")[0]["n"]
        rolled = r.rows(f"SELECT count(*) AS n FROM {TABLE}")[0]["n"]
    assert rolled < detail, "the rollup aggregated nothing"


def test_the_grand_total_matches(loaded):
    with loaded.reader() as r:
        detail = r.rows('SELECT round(sum("BilledCost"), 2) AS c FROM costs')[0]["c"]
        rolled = r.rows(
            'SELECT round(sum("BilledCost"), 2) AS c FROM costs_rollup')[0]["c"]
    assert rolled == pytest.approx(detail, abs=0.01)


@pytest.mark.parametrize("dimension", [
    '"BillingCurrency"',
    '"ServiceName"',
    '"ServiceFamily"',
    '"SubAccountName"',
    '"ResourceGroup"',
    '"RegionName"',
    '"ChargePeriodStart"',
])
def test_every_dashboard_grouping_matches(loaded, dimension):
    detail, rolled = _both(loaded, dimension)
    assert len(detail) == len(rolled), f"{dimension}: different number of groups"
    for d, rw in zip(detail, rolled):
        assert next(iter(d.values())) == next(iter(rw.values())), f"{dimension}: group mismatch"
        assert list(d.values())[1] == pytest.approx(list(rw.values())[1], abs=0.01), \
            f"{dimension}: total mismatch"


def test_a_date_window_matches(loaded):
    detail, rolled = _both(loaded, '"ServiceName"',
                           where="\"ChargePeriodStart\" >= DATE '2026-02-01'")
    assert len(detail) == len(rolled)
    assert detail, "the window matched nothing, so this proves nothing"
    for d, rw in zip(detail, rolled):
        assert list(d.values())[1] == pytest.approx(list(rw.values())[1], abs=0.01)


def test_null_dimensions_group_the_same_way(loaded):
    """NULL is not equal to NULL, so the two paths must bucket it identically."""
    detail, rolled = _both(loaded, '"RegionName"')
    d_nulls = [x for x in detail if next(iter(x.values())) is None]
    r_nulls = [x for x in rolled if next(iter(x.values())) is None]
    assert len(d_nulls) == 1 and len(r_nulls) == 1, "NULL region did not survive"
    assert list(d_nulls[0].values())[1] == pytest.approx(
        list(r_nulls[0].values())[1], abs=0.01)


def test_the_monthly_trend_matches(loaded):
    """The single heaviest query, and the one the rollup exists for."""
    with loaded.reader() as r:
        detail = r.rows(
            'SELECT strftime("ChargePeriodStart", \'%Y-%m\') AS m, '
            'round(sum("BilledCost"), 2) AS c FROM costs GROUP BY 1 ORDER BY 1')
        rolled = r.rows(
            'SELECT strftime("ChargePeriodStart", \'%Y-%m\') AS m, '
            'round(sum("BilledCost"), 2) AS c FROM costs_rollup GROUP BY 1 ORDER BY 1')
    assert [x["m"] for x in detail] == [x["m"] for x in rolled]
    for d, rw in zip(detail, rolled):
        assert d["c"] == pytest.approx(rw["c"], abs=0.01)


def test_every_summed_measure_matches(loaded):
    from app.rollup import MEASURES

    with loaded.reader() as r:
        for measure in MEASURES:
            detail = r.rows(f'SELECT round(sum("{measure}"), 2) AS c FROM costs')[0]["c"]
            rolled = r.rows(
                f'SELECT round(sum("{measure}"), 2) AS c FROM costs_rollup')[0]["c"]
            assert rolled == pytest.approx(detail, abs=0.01), measure


def test_a_second_refresh_does_not_double_the_totals(loaded):
    """The rebuild replaces rather than appends.

    An append would double every number on the dashboard, and the app has already
    shipped that bug once -- two loaders each wrote a subscription under a
    different spelling and a month billed at $476 read as $920.
    """
    before = loaded.refresh_rollup()
    with loaded.reader() as r:
        first = r.rows('SELECT round(sum("BilledCost"), 2) AS c FROM costs_rollup')[0]["c"]
    after = loaded.refresh_rollup()
    with loaded.reader() as r:
        second = r.rows('SELECT round(sum("BilledCost"), 2) AS c FROM costs_rollup')[0]["c"]
    assert before == after
    assert first == pytest.approx(second, abs=0.01)


# ------------------------------------------------------ replacing without bloating

def _relfilenode(warehouse, table: str) -> int:
    """The physical file behind a table.

    Asked for rather than `n_dead_tup`, which would be the more direct measurement
    but comes from the statistics collector and is therefore updated asynchronously
    -- a test reading it straight after a rebuild races the collector and fails
    intermittently. The relfilenode is catalogue state, visible the moment the
    transaction commits, and it answers the question that actually matters: whether
    the old heap was dropped or merely tombstoned.
    """
    with warehouse._store.connect() as con:  # noqa: SLF001
        row = con.execute(
            "SELECT c.relfilenode FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace "
            "WHERE n.nspname = ? AND c.relname = ?",
            [warehouse._rollup_schema, table]).fetchone()  # noqa: SLF001
    return int(row[0])


@pytest.mark.parametrize("table", ["costs_rollup", "resource_presence"])
def test_a_full_rebuild_replaces_the_heap_rather_than_tombstoning_it(loaded, table):
    """DELETE marks every row dead rather than reclaiming it.

    Replacing a whole table that way writes a complete second copy into the heap
    before the first can be reused. Measured on the real estate after one full
    rebuild: 2,784,069 dead tuples against 2,781,983 live, and a heap at 861 B/row
    against a real ~430 -- an entire extra copy.

    A full rebuild now assembles the replacement in a staging table and swaps it in.
    A new relfilenode is the evidence: the table is reading from a different file,
    so the old heap was dropped outright and there is nothing left for VACUUM to
    reclaim.
    """
    loaded.refresh_rollup()
    before = _relfilenode(loaded, table)
    loaded.refresh_rollup()
    assert _relfilenode(loaded, table) != before, (
        f"{table} was rewritten in place, which leaves the old rows dead in the heap")


@pytest.mark.parametrize("table", ["costs_rollup", "resource_presence"])
def test_a_windowed_rebuild_writes_in_place(loaded, table):
    """Staging the whole table to replace one month of it would be slower for nothing.

    A window replaces a bounded span, so the dead tuples it leaves are bounded by
    that span and autovacuum reclaims them between runs. Keeping the same
    relfilenode is what says the rebuild wrote in place.
    """
    import datetime as dt

    loaded.refresh_rollup()
    before = _relfilenode(loaded, table)
    loaded.refresh_rollup(window=(dt.date(2026, 2, 1), dt.date(2026, 3, 1)))
    assert _relfilenode(loaded, table) == before


def test_a_full_rebuild_keeps_the_indexes_it_is_read_through(loaded):
    """The swap drops the old table, so the indexes have to be rebuilt with it.

    They are created after the rename rather than on the staging table, so they
    take the names the table's own DDL gives them. Created on the staging table
    they would keep a staging-derived name for ever, and the next rebuild would
    collide with its own previous output.
    """
    from app import presence, rollup

    loaded.refresh_rollup()
    loaded.refresh_rollup()
    with loaded._store.connect() as con:  # noqa: SLF001
        found = {row[0] for row in con.execute(
            "SELECT indexname FROM pg_indexes WHERE schemaname = ?",
            [loaded._rollup_schema]).fetchall()}  # noqa: SLF001
    for table in (rollup.TABLE, presence.TABLE):
        assert f"{table}_period_idx" in found
        assert f"{table}_sub_idx" in found


def test_a_full_rebuild_leaves_no_staging_table_behind(loaded):
    from app import derived, presence, rollup

    loaded.refresh_rollup()
    with loaded._store.connect() as con:  # noqa: SLF001
        found = {row[0] for row in con.execute(
            "SELECT table_name FROM information_schema.tables WHERE table_schema = ?",
            [loaded._rollup_schema]).fetchall()}  # noqa: SLF001
    for module in (rollup, presence):
        assert f"{module.TABLE}{derived.STAGING_SUFFIX}" not in found


def test_a_failed_rebuild_leaves_the_previous_contents_in_place(loaded, monkeypatch):
    """The swap must be as atomic as the DELETE and INSERT it replaces.

    Staging plus a rename is more moving parts than a delete, so this checks the
    property that actually matters: a rebuild that dies partway leaves the table a
    reader can still use, holding exactly what the last good rebuild produced. It
    dies between the two swaps, which is the moment with the most to go wrong --
    the rollup has already been replaced when the presence table fails.
    """
    from app import derived

    loaded.refresh_rollup()
    with loaded.reader() as r:
        before = r.rows('SELECT round(sum("BilledCost"), 2) AS c FROM costs_rollup')[0]["c"]

    real_publish = derived.publish

    def die_between_the_two_swaps(con, schema, module, staged):
        if module.TABLE == "resource_presence":
            raise RuntimeError("rebuild failed on purpose, after the rollup swapped")
        return real_publish(con, schema, module, staged)

    monkeypatch.setattr(derived, "publish", die_between_the_two_swaps)
    assert loaded.refresh_rollup() == 0

    monkeypatch.undo()
    with loaded.reader() as r:
        after = r.rows('SELECT round(sum("BilledCost"), 2) AS c FROM costs_rollup')[0]["c"]
    assert after == pytest.approx(before, abs=0.01)


def test_a_windowed_rebuild_reports_the_live_table_as_its_staging(loaded):
    """`stage` returning the live table's own name is what tells `publish` not to swap."""
    import datetime as dt

    from app import derived, rollup

    with loaded.connect() as con:
        staged = derived.stage(con, loaded._rollup_schema, rollup,  # noqa: SLF001
                               loaded._pg_table,  # noqa: SLF001
                               (dt.date(2026, 2, 1), dt.date(2026, 3, 1)))
    assert staged == rollup.TABLE


def test_covers_refuses_what_it_cannot_answer():
    from app.rollup import covers

    assert covers(["ServiceName"], ["BilledCost"])
    assert covers(["ChargePeriodStart", "BillingCurrency"], ["BilledCost", "CostInUsd"])
    # PricingQuantity is additive -- hours and GB genuinely add up -- so it is held.
    assert covers(["ServiceName"], ["PricingQuantity"])
    # ResourceId is not in the grain: a resource-level query must use the detail table.
    assert not covers(["ResourceId"], ["BilledCost"])
    assert not covers(["MeterName"], ["BilledCost"])
    # UnitPrice is a rate. Summing rates produces a number with no meaning, so it
    # is not a measure and must not be served from here.
    assert not covers(["ServiceName"], ["UnitPrice"])


def test_the_grain_is_checked_against_the_real_schema():
    """Three invented column names got as far as a live server once.

    rebuild() logs and continues on failure, so the only symptom was one warning
    line, an empty rollup, and a dashboard quietly back on full table scans.
    """
    from app import rollup
    from app.warehouse import COLUMNS, NUMERIC

    for name in rollup.DIMENSIONS + rollup.MEASURES:
        assert name in COLUMNS, f"{name} is not a warehouse column"
    for name in rollup.MEASURES:
        assert name in NUMERIC, f"{name} is not numeric and cannot be summed"


# --------------------------------------------------------------- windowed rebuild

def test_month_window_snaps_outwards():
    """The window must cover whole months, and must never narrow.

    Snapping outwards is what lets a scoped rebuild prune to `costs_fact`'s
    monthly partitions. Snapping inwards, or off-by-one on the exclusive end,
    would silently leave the last day of a write out of the rebuild -- which is
    the one failure mode this whole feature must not have.
    """
    import datetime as dt

    from app.rollup import month_window

    assert month_window([]) is None
    assert month_window([None]) is None

    lo, hi = month_window([dt.date(2026, 2, 14)])
    assert (lo, hi) == (dt.date(2026, 2, 1), dt.date(2026, 3, 1))

    # A day that is already a month start still yields a whole month.
    lo, hi = month_window([dt.date(2026, 2, 1)])
    assert (lo, hi) == (dt.date(2026, 2, 1), dt.date(2026, 3, 1))

    # The last day of a 31-day month must fall inside, not on, the upper bound.
    lo, hi = month_window([dt.date(2026, 1, 31)])
    assert (lo, hi) == (dt.date(2026, 1, 1), dt.date(2026, 2, 1))
    assert lo <= dt.date(2026, 1, 31) < hi

    # Spanning a year boundary.
    lo, hi = month_window([dt.date(2026, 12, 9), dt.date(2025, 11, 30)])
    assert (lo, hi) == (dt.date(2025, 11, 1), dt.date(2027, 1, 1))


def test_a_windowed_rebuild_over_everything_equals_a_full_rebuild(loaded):
    """The window is an optimisation, so widening it to all of history is a no-op.

    Compared to the cent rather than bit-for-bit. `sum()` over double precision is
    not associative, so recomputing the same group in a different physical row
    order can move the last bits -- an artefact of float addition, not a
    difference in what was aggregated. Rounding is also what the dashboard does
    with these numbers, so it is the equality that actually matters.
    """
    import datetime as dt

    from app.rollup import DIMENSIONS, MEASURES, TABLE

    dims = ", ".join(f'"{d}"' for d in DIMENSIONS)
    sums = ", ".join(f'round("{m}"::numeric, 2) AS "{m}"' for m in MEASURES)

    def snapshot():
        with loaded.reader() as r:
            return r.rows(f'SELECT {dims}, {sums}, charges FROM {TABLE} '
                          'ORDER BY 1,2,3,4,5,6,7,8', limit=100000)

    full = snapshot()
    assert full, "fixture produced no rollup rows"
    loaded.refresh_rollup(window=(dt.date(2000, 1, 1), dt.date(2100, 1, 1)))
    assert snapshot() == full


def test_a_windowed_rebuild_corrects_its_month_and_leaves_the_others_alone(loaded):
    """The actual contract: in-window rows are recomputed, out-of-window rows are untouched.

    Written as a real correction rather than a no-op rebuild. February gains rows
    after the rollup was last built, so a scoped rebuild of February alone has to
    move February's total to agree with the detail table while leaving January and
    March byte-identical -- which is what makes a daily ingest able to skip the
    rest of a year of history.
    """
    import datetime as dt

    from app.rollup import TABLE
    from app.warehouse import COLUMNS, NUMERIC, insert_rows

    def by_month():
        with loaded.reader() as r:
            return {row["m"]: row["c"] for row in r.rows(
                'SELECT to_char("ChargePeriodStart", \'YYYY-MM\') AS m, '
                f'round(sum("BilledCost"), 2) AS c FROM {TABLE} GROUP BY 1 ORDER BY 1')}

    def detail_by_month():
        with loaded.reader() as r:
            return {row["m"]: row["c"] for row in r.rows(
                'SELECT to_char("ChargePeriodStart", \'YYYY-MM\') AS m, '
                'round(sum("BilledCost"), 2) AS c FROM costs GROUP BY 1 ORDER BY 1')}

    def other_months():
        with loaded.reader() as r:
            return r.rows(
                f'SELECT * FROM {TABLE} WHERE "ChargePeriodStart" < DATE \'2026-02-01\' '
                'OR "ChargePeriodStart" >= DATE \'2026-03-01\' ORDER BY 1,2,3,4,5,6,7,8',
                limit=100000)

    before_rollup = by_month()
    before_others = other_months()
    assert before_rollup == detail_by_month(), "fixture should start in agreement"

    extra = []
    for i in range(50):
        values = {
            "SubAccountId": str(uuid.UUID(int=0)),
            "SubAccountName": "sub-0",
            "ChargePeriodStart": f"2026-02-{1 + (i % 28):02d}",
            "BilledCost": 100.0,
            "CostInUsd": 100.0,
            "BillingCurrency": "USD",
            "ServiceName": "Virtual Machines",
            "ServiceFamily": "Compute",
            "RegionName": "eastus",
            "ResourceGroup": "rg-new",
            "ResourceId": f"/subscriptions/x/rg-new/res-{i}",
            "MeterName": "meter-new",
        }
        extra.append([values.get(c, 0.0 if c in NUMERIC else None) for c in COLUMNS])

    with loaded.connect() as con:
        insert_rows(con, extra)

    # Stale by construction: the detail table has moved, the rollup has not.
    assert by_month() == before_rollup
    assert by_month() != detail_by_month()

    loaded.refresh_rollup(window=(dt.date(2026, 2, 1), dt.date(2026, 3, 1)))

    after = by_month()
    assert after["2026-02"] == pytest.approx(
        detail_by_month()["2026-02"], abs=0.01), "February was not corrected"
    assert after["2026-02"] != pytest.approx(before_rollup["2026-02"], abs=0.01)
    assert other_months() == before_others, "a month outside the window was rewritten"
    assert after == pytest.approx(detail_by_month(), abs=0.01)


def test_a_windowed_rebuild_is_ignored_after_a_failed_rebuild(loaded, monkeypatch):
    """A failure must force the next rebuild to be full, whatever the caller asks for.

    This is the one way narrowing can go permanently wrong. A failed rebuild leaves
    days stale; if every later windowed rebuild stepped over them they would never
    be corrected, and the dashboard would keep showing them with no indication that
    anything was wrong.
    """
    import datetime as dt

    from app import derived as derived_module

    calls: list[object] = []
    real = derived_module.rebuild

    def spy(con, schema, modules, source, window=None):
        calls.append(window)
        return real(con, schema, modules, source, window)

    def boom(*_args, **_kwargs):
        raise RuntimeError("rebuild failed on purpose")

    # `derived.rebuild` is the seam `refresh_rollup` goes through for both derived
    # tables at once, so failing it fails the rebuild exactly as a real one would.
    monkeypatch.setattr(derived_module, "rebuild", boom)
    assert loaded.refresh_rollup() == 0

    # The very next windowed request must be widened to a full rebuild.
    monkeypatch.setattr(derived_module, "rebuild", spy)
    loaded.refresh_rollup(window=(dt.date(2026, 2, 1), dt.date(2026, 3, 1)))
    assert calls == [None], "a window was honoured while the rollup was known stale"

    # And once one has succeeded, windows are honoured again.
    loaded.refresh_rollup(window=(dt.date(2026, 2, 1), dt.date(2026, 3, 1)))
    assert calls[-1] == (dt.date(2026, 2, 1), dt.date(2026, 3, 1))


def test_the_ingest_window_covers_every_committed_date():
    """Derived from the task bounds an ingest actually committed."""
    import datetime as dt

    from app.ingest_jobs import _written_window

    assert _written_window([]) is None

    lo, hi = _written_window(["2026-02-01", "2026-02-28"])
    assert (lo, hi) == (dt.date(2026, 2, 1), dt.date(2026, 3, 1))

    # Several tasks over several months collapse to one covering span.
    lo, hi = _written_window(["2026-01-15", "2026-01-31", "2026-03-01", "2026-03-31"])
    assert (lo, hi) == (dt.date(2026, 1, 1), dt.date(2026, 4, 1))


def test_an_unreadable_ingest_date_abandons_the_optimisation():
    """Uncertain input must widen to a full rebuild, never guess a narrower one."""
    from app.ingest_jobs import _written_window

    assert _written_window(["2026-02-01", "not-a-date"]) is None
    assert _written_window([None]) is None
