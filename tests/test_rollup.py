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
