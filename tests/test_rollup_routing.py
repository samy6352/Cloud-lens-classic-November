"""The dashboard may read the rollup only where the rollup is exactly right.

`test_rollup.py` proves the rollup agrees with the detail table. This file proves
the *routing* is sound: that a connection which cannot be answered from the grain
falls back rather than guessing, that a scoped connection cannot see past its
scope through the rollup, and that a stale rollup is not read at all.

The payload tests are the ones that matter most. They build the same tab twice --
once letting it route normally, once forced onto `costs` -- and require the two to
be identical. That is the property a finance team depends on: which table answered
must never be visible in the answer.
"""

from __future__ import annotations

import sys as _sys
from pathlib import Path as _Path

# Tests live in tests/ and import the application from the checkout root.
_sys.path.insert(0, str(_Path(__file__).resolve().parents[1]))

import json
import os
import uuid

import pytest

pytestmark = pytest.mark.skipif(
    not os.getenv("CLOUDLENS_PG_TEST"),
    reason="set CLOUDLENS_PG_TEST=<host> to run against a PostgreSQL server",
)

if os.getenv("CLOUDLENS_PG_TEST"):
    os.environ["PGHOST"] = os.environ["CLOUDLENS_PG_TEST"]
    os.environ["PGDATABASE"] = os.getenv("CLOUDLENS_PG_DB", "postgres")
    os.environ["PGUSER"] = os.environ["CLOUDLENS_PG_USER"]
    os.environ["PGSCHEMA"] = f"cl_route_{uuid.uuid4().hex[:8]}"

from tests.test_rollup import _messy_rows  # noqa: E402  - shares the awkward fixture data


@pytest.fixture()
def wh(monkeypatch):
    schema = f"cl_route_{uuid.uuid4().hex[:8]}"
    monkeypatch.setenv("PGSCHEMA", schema)

    from app.warehouse import Warehouse

    warehouse = Warehouse()
    yield warehouse

    with warehouse._store.connect() as con:
        con.execute(f"DROP SCHEMA IF EXISTS {schema} CASCADE")
    warehouse._store.close()


@pytest.fixture()
def loaded(wh):
    from app.warehouse import insert_rows

    with wh.connect() as con:
        insert_rows(con, _messy_rows())
    wh.refresh_rollup()
    return wh


def _total(reader, table: str, where: str = "") -> float:
    clause = f"WHERE {where}" if where else ""
    rows = reader.rows(f'SELECT round(sum("BilledCost"), 2) AS cost FROM {table} {clause}', 2)
    return float(rows[0]["cost"] or 0.0)


# --------------------------------------------------------------------- routing

def test_a_plain_reader_is_pointed_at_the_rollup(loaded):
    from app.rollup import TABLE

    with loaded.reader() as r:
        assert r.agg == TABLE
        assert r.aggregated


def test_a_converted_currency_is_not_answered_from_the_rollup_table(loaded):
    """The grain has no PricingCurrency or exchange rate, so conversion must derive.

    The view still has to exist and still has to be right -- the caller reads
    `costs_rollup` either way -- so this checks the total as well as the routing.
    """
    with loaded.reader(currency="USD") as r:
        rolled = _total(r, r.agg)
        detail = _total(r, "costs")
    assert rolled == pytest.approx(detail, abs=0.01)


def test_a_filter_outside_the_grain_is_not_answered_from_the_rollup_table(loaded):
    """`charge` narrows ChargeCategory, which the rollup does not carry."""
    with loaded.reader(filters={"charge": ["Usage"]}) as r:
        assert _total(r, r.agg) == pytest.approx(_total(r, "costs"), abs=0.01)


def test_a_filter_inside_the_grain_still_reaches_the_rollup(loaded):
    """`service` narrows ServiceName, which it does, so this one keeps the fast path."""
    with loaded.reader(filters={"service": ["Storage"]}) as r:
        assert _total(r, r.agg) == pytest.approx(_total(r, "costs"), abs=0.01)


def test_an_offered_but_unused_filter_does_not_disqualify_the_rollup(loaded):
    """An empty selection adds no predicate, so it must not force the slow path."""
    from app.rollup import serves_filters

    assert serves_filters({"charge": [], "service": ["Storage"]})
    assert not serves_filters({"charge": ["Usage"]})


# ----------------------------------------------------------------------- scope

def test_the_rollup_view_cannot_see_past_its_scope(loaded):
    """The rollup table is not scope-filtered; the view standing in for it must be.

    The bug this guards against is silent and serious: a scoped user reading an
    unshadowed `costs_rollup` would be shown every subscription's money.
    """
    one = str(uuid.UUID(int=2))
    with loaded.reader(scope=[one]) as r:
        scoped_rollup = _total(r, r.agg)
        scoped_detail = _total(r, "costs")
    with loaded.reader() as r:
        everything = _total(r, r.agg)

    assert scoped_rollup == pytest.approx(scoped_detail, abs=0.01)
    assert scoped_rollup < everything


def test_scope_and_an_in_grain_filter_compose_on_the_rollup(loaded):
    one = str(uuid.UUID(int=3))
    with loaded.reader(scope=[one], filters={"service": ["Storage"]}) as r:
        assert _total(r, r.agg) == pytest.approx(_total(r, "costs"), abs=0.01)


# ------------------------------------------------------------------- staleness

def test_a_rollup_behind_the_detail_table_is_not_read(loaded):
    """A write that the rollup has not caught up with must send reads back to `costs`.

    Staleness is recorded in `meta` rather than in the process, so it survives a
    restart -- which is the whole reason the read side can trust it.
    """
    from app.jobs import GENERATION_KEY

    with loaded.connect() as con:
        con.execute("UPDATE meta SET value = ? WHERE key = ?", ["99", GENERATION_KEY])

    with loaded.reader() as r:
        assert r.agg == "costs"
        assert not r.aggregated


def test_catching_the_rollup_up_puts_it_back_in_service(loaded):
    from app.jobs import GENERATION_KEY
    from app.rollup import TABLE

    with loaded.connect() as con:
        con.execute("UPDATE meta SET value = ? WHERE key = ?", ["99", GENERATION_KEY])
    with loaded.reader() as r:
        assert r.agg == "costs"

    loaded.refresh_rollup()
    with loaded.reader() as r:
        assert r.agg == TABLE


# ------------------------------------------------------------------- payloads

def _dashboard(warehouse):
    from app.dashboard import Dashboard

    dash = Dashboard()
    dash.wh = warehouse
    return dash


def _without_rollup(monkeypatch):
    """Force every routed query in this module back onto the detail table.

    All three routes, not just the aggregate one. `_counted` and `_named` reach the
    presence table, and since that table carries the per-resource money the tab
    publishes, leaving them routed would compare presence against presence and prove
    nothing about whether presence agrees with `costs`.
    """
    import app.dashboard as dashboard

    monkeypatch.setattr(dashboard, "_aggregate", lambda reader: "costs")
    monkeypatch.setattr(dashboard, "_counted", lambda reader: "costs")
    monkeypatch.setattr(dashboard, "_named", lambda reader: "costs")


def _same(routed, plain, path: str = "") -> None:
    """Require two payloads to agree, allowing a cent on any individual figure.

    Not exact equality, because `BilledCost` is `double precision` and addition of
    doubles is not associative: pre-aggregating a day and then summing the days
    reaches the same total by a different route, and the last bits can differ. Where
    that lands on a rounding boundary the published figure moves by one cent, and a
    figure derived by subtracting two of those can move by two.

    That is the tolerance `test_rollup.py` already holds the rollup to, and it is not
    a new kind of variance -- PostgreSQL may sum a `double precision` column in any
    order across parallel workers, so the detail table does not promise a
    bit-identical total between two runs either. Structure, keys, ordering and
    every non-numeric field still have to match exactly, and
    `test_the_grand_total_is_not_merely_close` pins the one figure that must not move
    at all.
    """
    assert type(routed) is type(plain), f"{path}: {type(routed)} vs {type(plain)}"
    if isinstance(routed, dict):
        assert routed.keys() == plain.keys(), f"{path}: keys differ"
        for key in routed:
            _same(routed[key], plain[key], f"{path}.{key}")
    elif isinstance(routed, list):
        assert len(routed) == len(plain), f"{path}: {len(routed)} vs {len(plain)} rows"
        for i, (a, b) in enumerate(zip(routed, plain)):
            _same(a, b, f"{path}[{i}]")
    elif isinstance(routed, float):
        assert routed == pytest.approx(plain, abs=0.02), f"{path}: {routed} vs {plain}"
    else:
        assert routed == plain, f"{path}: {routed!r} vs {plain!r}"


@pytest.mark.parametrize("days", [7, 30, 90])
def test_the_overview_is_the_same_either_way(loaded, monkeypatch, days):
    routed = _dashboard(loaded).executive(None, days=days)
    _without_rollup(monkeypatch)
    _same(routed, _dashboard(loaded).executive(None, days=days))


def test_the_tab_bar_is_the_same_either_way(loaded, monkeypatch):
    routed = _dashboard(loaded).sections(None, days=30)
    _without_rollup(monkeypatch)
    _same(routed, _dashboard(loaded).sections(None, days=30))


@pytest.mark.parametrize("section_id", ["compute", "storage", "other", "marketplace"])
def test_every_tab_is_the_same_either_way(loaded, monkeypatch, section_id):
    routed = _dashboard(loaded).section(section_id, None, days=30)
    _without_rollup(monkeypatch)
    _same(routed, _dashboard(loaded).section(section_id, None, days=30))


def test_a_scoped_overview_is_the_same_either_way(loaded, monkeypatch):
    scope = [str(uuid.UUID(int=1))]
    routed = _dashboard(loaded).executive(scope, days=30)
    _without_rollup(monkeypatch)
    _same(routed, _dashboard(loaded).executive(scope, days=30))


def test_a_converted_overview_is_the_same_either_way(loaded, monkeypatch):
    routed = _dashboard(loaded).executive(None, days=30, currency="USD")
    _without_rollup(monkeypatch)
    _same(routed, _dashboard(loaded).executive(None, days=30, currency="USD"))


def test_the_grand_total_is_not_merely_close(loaded):
    """The one figure that must be exact, because it is the one people reconcile."""
    with loaded.reader() as r:
        assert _total(r, r.agg) == _total(r, "costs")


# --------------------------------------------------------------- warehouse summary

def test_the_summary_counts_charges_not_rollup_groups(loaded):
    """`summary` reads the rollup, where one row stands for many charges.

    The number on the page is a count of charges, so summing `charges` is the
    only reading of the rollup that reproduces it. Taking `count(*)` there would
    report the number of groups -- a smaller number, and a plausible-looking one.
    """
    with loaded.reader() as r:
        detail = r.rows('SELECT count(*) AS n FROM costs')[0]["n"]
        groups = r.rows(f'SELECT count(*) AS n FROM {r.agg}')[0]["n"]
    assert groups < detail, "fixture must aggregate, or this test proves nothing"
    assert loaded.summary()["rows"] == detail


def test_the_summary_row_count_survives_the_json_encoder(loaded):
    """`sum(bigint)` is `numeric` in PostgreSQL, and a Decimal cannot be encoded.

    Caught in a browser rather than here the first time: the query was correct,
    the total was right, and `/api/warehouse` still returned 500 because the
    count came back as a `Decimal`. The type is part of the contract.
    """
    summary = loaded.summary()
    assert isinstance(summary["rows"], int)
    assert isinstance(summary["subscriptions"], int)
    json.dumps(summary)


def test_the_summary_is_the_same_without_the_rollup(loaded, monkeypatch):
    """The fast path and the detail table must agree on every field."""
    routed = loaded.summary()
    _without_rollup(monkeypatch)
    assert routed == loaded.summary()


def test_the_subscription_picker_is_the_same_without_the_rollup(loaded, monkeypatch):
    routed = loaded.subscriptions()
    _without_rollup(monkeypatch)
    assert routed == loaded.subscriptions()


def test_the_currency_list_is_the_same_without_the_rollup(loaded, monkeypatch):
    routed = loaded.currencies()
    _without_rollup(monkeypatch)
    assert routed == loaded.currencies()


def test_the_summary_cannot_see_past_its_scope(loaded):
    """Routing a scoped read to the rollup must not widen what it can see."""
    scoped = loaded.summary(scope=[str(uuid.UUID(int=2))])
    whole = loaded.summary()
    assert scoped["rows"] < whole["rows"]
    assert scoped["subscriptions"] == 1


# ------------------------------------------------------------------- anomalies

def _anomalies(warehouse, monkeypatch, **kwargs):
    """`anomalies.analyse` against this fixture's warehouse rather than the global one."""
    import app.anomalies as anomalies
    import app.warehouse as wh_mod

    monkeypatch.setattr(wh_mod, "warehouse", warehouse)
    return anomalies.analyse(**kwargs)


def _on_the_detail_table(monkeypatch, warehouse):
    """Make `relations` name `costs` for all three routes, as it does when stale."""
    monkeypatch.setattr(type(warehouse), "relations",
                        lambda self, *a, **k: ("costs", "costs", "costs"))


def test_the_anomaly_tab_is_the_same_either_way(loaded, monkeypatch):
    """The tab published 44s of its 45.7s waiting on `min/max/count(*) FROM costs`.

    Routing that to the rollup is only legitimate if the answer does not move --
    including the dimensions the rollup cannot serve, which must still reach the
    detail table. `MeterName` is in neither grain and `ResourceName` only in the
    presence table, so this exercises all three routes in one payload.
    """
    dims = ("ServiceName", "SubAccountName", "ResourceName", "MeterName")
    routed = _anomalies(loaded, monkeypatch, scope=None, days=30, dimensions=dims)
    _on_the_detail_table(monkeypatch, loaded)
    _same(routed, _anomalies(loaded, monkeypatch, scope=None, days=30, dimensions=dims))


def test_a_scoped_anomaly_tab_is_the_same_either_way(loaded, monkeypatch):
    one = [str(uuid.UUID(int=2))]
    routed = _anomalies(loaded, monkeypatch, scope=one, days=30,
                        dimensions=("ServiceName",))
    _on_the_detail_table(monkeypatch, loaded)
    _same(routed, _anomalies(loaded, monkeypatch, scope=one, days=30,
                             dimensions=("ServiceName",)))


# ------------------------------------------------- per-resource money (waste)

def _costs_for(warehouse, monkeypatch, ids, days=3650):
    import app.warehouse as wh_mod
    import app.waste as waste

    monkeypatch.setattr(wh_mod, "warehouse", warehouse)
    return waste._costs_for(ids, days, allow_mixed=True)


def _every_resource_id(warehouse) -> list[str]:
    with warehouse.reader() as r:
        return [row["id"] for row in r.rows(
            'SELECT DISTINCT "ResourceId" AS id FROM costs '
            'WHERE "ResourceId" IS NOT NULL', None)]


def test_per_resource_money_is_the_same_either_way(loaded, monkeypatch):
    """`waste._costs_for` backs rightsizing, recommendations and the waste tab.

    It was reading the detail table for money the presence table already holds,
    at 27.0s of rightsizing's 28.4s and 84.0s of recommendations' 87.4s. Routing
    it is only legitimate if every resource's money is unchanged, so this compares
    every resource rather than a sample.
    """
    ids = _every_resource_id(loaded)
    assert ids, "fixture must have resources, or this test proves nothing"
    routed = _costs_for(loaded, monkeypatch, ids)
    assert routed, "routed read returned nothing -- the query failed and was swallowed"
    _on_the_detail_table(monkeypatch, loaded)
    _same(routed, _costs_for(loaded, monkeypatch, ids))


def test_per_resource_money_adds_up_casings_of_one_id(loaded, monkeypatch):
    """The presence grain keeps `ResourceId` as the export spelled it.

    The export spells it inconsistently -- both `resourceGroups` and
    `resourcegroups` appear -- so one resource can be several presence rows.
    `lower(trim(...))` has to stay in the query to add them back together, and
    the fixture's ids are uniformly cased, so this inserts the awkward case
    rather than assuming it is already there.
    """
    import uuid as _uuid

    from app.warehouse import COLUMNS, NUMERIC, insert_rows

    one = "/subscriptions/x/resourceGroups/rg-cased/res-1"
    rows = []
    for spelling, cost in ((one, 10.0), (one.lower(), 20.0), (one.upper(), 40.0)):
        values = {
            "SubAccountId": str(_uuid.UUID(int=0)), "SubAccountName": "sub-0",
            "ChargePeriodStart": "2026-02-10", "BilledCost": cost, "CostInUsd": cost,
            "BillingCurrency": "USD", "ServiceName": "Virtual Machines",
            "ServiceFamily": "Compute", "RegionName": "eastus",
            "ResourceGroup": "rg-cased", "ResourceId": spelling,
        }
        rows.append([values.get(c, 0.0 if c in NUMERIC else None) for c in COLUMNS])
    with loaded.connect() as con:
        insert_rows(con, rows)
    loaded.refresh_rollup()

    found = _costs_for(loaded, monkeypatch, [one])
    assert found[one.lower()]["cost_by_currency"]["USD"] == pytest.approx(70.0, abs=0.01)


# ------------------------------------------------------- resource presence table

def _counts(reader, table: str, where: str = "") -> list[dict]:
    clause = f"WHERE {where}" if where else ""
    return reader.rows(
        'SELECT "BillingCurrency" AS currency, count(DISTINCT "ResourceId") AS resources, '
        'count(DISTINCT "ServiceName") AS services, '
        'count(DISTINCT "SubAccountId") AS subscriptions, '
        f'count(DISTINCT "RegionName") AS regions FROM {table} {clause} '
        'GROUP BY 1 ORDER BY 1', 500)


def test_a_plain_reader_is_pointed_at_the_presence_table(loaded):
    from app.presence import TABLE

    with loaded.reader() as r:
        assert r.res == TABLE


def test_every_count_matches_the_detail_table(loaded):
    """Exact, not close. A resource count is a headline figure on the overview."""
    with loaded.reader() as r:
        assert _counts(r, r.res) == _counts(r, "costs")


def test_counts_match_inside_a_date_window(loaded):
    with loaded.reader() as r:
        window = '"ChargePeriodStart" >= DATE \'2026-02-01\''
        assert _counts(r, r.res, window) == _counts(r, "costs", window)


def test_counts_match_per_region(loaded):
    """The shape `regions_top` asks for, which is where the 55s used to go."""
    def by_region(table):
        return _reader.rows(
            'SELECT coalesce("RegionName", \'-\') AS name, "BillingCurrency" AS currency, '
            f'count(DISTINCT "ResourceId") AS resources FROM {table} GROUP BY 1, 2 '
            'ORDER BY 1, 2', 500)

    with loaded.reader() as _reader:
        assert by_region(_reader.res) == by_region("costs")


def test_counts_match_per_service_family(loaded):
    """The shape each tab asks for."""
    def by_family(table):
        return _reader.rows(
            'SELECT coalesce("ServiceFamily", \'-\') AS name, "BillingCurrency" AS currency, '
            f'count(DISTINCT "ResourceId") AS resources FROM {table} GROUP BY 1, 2 '
            'ORDER BY 1, 2', 500)

    with loaded.reader() as _reader:
        assert by_family(_reader.res) == by_family("costs")


def test_the_presence_table_cannot_see_past_its_scope(loaded):
    one = str(uuid.UUID(int=4))
    with loaded.reader(scope=[one]) as r:
        scoped = _counts(r, r.res)
        assert scoped == _counts(r, "costs")
    with loaded.reader() as r:
        everything = _counts(r, r.res)
    assert sum(row["resources"] for row in scoped) < sum(row["resources"] for row in everything)


def test_a_converted_currency_still_counts_correctly(loaded):
    """Conversion relabels the currency column; it must not change how many resources.

    The rollup cannot serve a converted connection at all, but this table can still
    count for one: the only dimension conversion touches is `BillingCurrency`, and
    relabelling it does not change how many resources there were. Its *money* is a
    separate question, which `test_a_converted_currency_takes_its_money_elsewhere`
    covers.
    """
    with loaded.reader(currency="USD") as r:
        assert _counts(r, r.res) == _counts(r, "costs")


def test_a_filter_outside_the_grain_still_counts_correctly(loaded):
    with loaded.reader(filters={"charge": ["Usage"]}) as r:
        assert _counts(r, r.res) == _counts(r, "costs")


def test_a_stale_generation_stops_the_presence_table_being_read(loaded):
    from app.jobs import GENERATION_KEY

    with loaded.connect() as con:
        con.execute("UPDATE meta SET value = ? WHERE key = ?", ["99", GENERATION_KEY])
    with loaded.reader() as r:
        assert r.res == "costs"


def test_rows_without_a_resource_id_are_kept(loaded):
    """Dropping them would lose a service that only appears on a subscription charge.

    `count(DISTINCT ...)` ignores NULLs by itself, so keeping them cannot inflate the
    resource count -- which `test_every_count_matches_the_detail_table` also proves.
    """
    from app.presence import DIMENSIONS, TABLE

    assert "ResourceId" in DIMENSIONS
    with loaded.reader() as r:
        detail = r.rows('SELECT count(*) AS n FROM costs WHERE "ResourceId" IS NULL', 2)
        kept = r.rows(f'SELECT count(*) AS n FROM {TABLE} WHERE "ResourceId" IS NULL', 2)
    if detail[0]["n"]:
        assert kept[0]["n"] > 0


def test_presence_covers_refuses_what_it_cannot_answer(loaded):
    from app.presence import covers

    assert covers(["RegionName"], ["ResourceId"])
    assert covers(["ServiceFamily", "BillingCurrency"], ["ResourceId", "ServiceName"])
    # Listing resources is not counting them; that belongs on the detail table.
    assert not covers(["ResourceId"], ["ResourceId"])
    assert not covers(["MeterName"], ["ResourceId"])
    assert not covers(["RegionName"], ["BilledCost"])


# ------------------------------------------------- money on the presence table

def _named_money(reader, table: str) -> list[tuple]:
    """The per-named-resource spend the top-spend list is built from.

    Rounded, and ordered by every key, so two backings can be compared as values
    rather than as whatever order each planner happened to return.
    """
    rows = reader.rows(
        'SELECT coalesce("ResourceName", \'-\') AS name, '
        'coalesce("ResourceGroup", \'-\') AS grp, "ServiceName" AS service, '
        'coalesce("RegionName", \'-\') AS region, "BillingCurrency" AS currency, '
        f'round(sum("BilledCost")::numeric, 2) AS cost FROM {table} '
        'WHERE "ResourceId" IS NOT NULL GROUP BY 1, 2, 3, 4, 5 '
        'ORDER BY 1, 2, 3, 4, 5', None)
    return [tuple(row.values()) for row in rows]


def test_the_presence_money_matches_the_detail_table(loaded):
    """The whole point of putting money here: it has to be the same money.

    Exact rather than within a cent, because both sides are summing the same
    `double precision` values into the same groups -- the grain the presence table
    holds *is* this grouping, so there is no re-aggregation to reorder the addition.
    """
    with loaded.reader() as r:
        assert _named_money(r, r.named) == _named_money(r, "costs")


def test_the_presence_grand_total_matches_the_detail_table(loaded):
    """Including the rows with no `ResourceId`, which the per-resource cut excludes.

    Those are subscription-level charges. They are kept in the grain so the service
    and region counts beside the resource count do not go missing, and now that the
    money is here too, dropping them would leave this total short.
    """
    with loaded.reader() as r:
        assert _total(r, r.named) == _total(r, "costs")


def test_a_converted_currency_takes_its_money_elsewhere(loaded):
    """The conversion factor needs columns the grain does not carry.

    It is row-dependent on `PricingCurrency` and `ExchangeRatePricingToBilling`, so
    two rows sharing every dimension here can still convert differently and a summed
    figure cannot be converted after the fact. Money goes to `costs`; counting, which
    conversion cannot affect, keeps the fast table.
    """
    from app.presence import TABLE

    with loaded.reader(currency="USD") as r:
        assert r.named == "costs"
        assert r.res == TABLE


def test_a_converted_presence_view_reads_null_rather_than_the_wrong_currency(loaded):
    """Belt and braces behind the routing above.

    If something ever reads the measures on a converted connection anyway, it must
    get nothing rather than a number that is silently still in the billed currency.
    """
    from app.presence import MEASURES, TABLE

    with loaded.reader(currency="USD") as r:
        measures = ", ".join(f'count("{m}") AS "{m}"' for m in MEASURES)
        row = r.rows(f"SELECT {measures} FROM {TABLE}", 2)[0]
    assert all(value == 0 for value in row.values())


def test_an_as_billed_connection_takes_its_money_from_the_presence_table(loaded):
    from app.presence import TABLE

    with loaded.reader() as r:
        assert r.named == TABLE


def test_a_filter_outside_the_grain_still_prices_resources_correctly(loaded):
    """The view is derived from the already-filtered `costs`, so it stays exact."""
    with loaded.reader(filters={"charge": ["Usage"]}) as r:
        assert _named_money(r, r.named) == _named_money(r, "costs")


def test_the_presence_money_cannot_see_past_its_scope(loaded):
    one = str(uuid.UUID(int=4))
    with loaded.reader(scope=[one]) as r:
        scoped = _total(r, r.named)
        assert scoped == _total(r, "costs")
    with loaded.reader() as r:
        everything = _total(r, r.named)
    assert scoped < everything


def test_a_stale_generation_stops_the_presence_money_being_read(loaded):
    from app.jobs import GENERATION_KEY

    with loaded.connect() as con:
        con.execute("UPDATE meta SET value = ? WHERE key = ?", ["99", GENERATION_KEY])
    with loaded.reader() as r:
        assert r.named == "costs"


def test_presence_serves_money_only_without_a_conversion(loaded):
    from app.presence import serves_money

    assert serves_money(None)
    assert not serves_money(1.0)
    assert not serves_money(83.2)


def test_the_resource_name_is_part_of_the_grain(loaded):
    """Without it the top-spend list has nothing readable to show.

    Measured at one extra row across 5.8M before it was added, because a resource is
    renamed within a day almost never and the day is already in the grain.
    """
    from app.presence import DIMENSIONS, GROUPABLE

    assert "ResourceName" in DIMENSIONS
    assert "ResourceName" in GROUPABLE
