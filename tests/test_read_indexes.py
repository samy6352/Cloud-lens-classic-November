"""Exact ordered counts/indexed Billing, including absent indexes and publication."""
from __future__ import annotations

from datetime import date

import pytest


@pytest.fixture
def indexed_wh(module_postgres_warehouse, monkeypatch):
    from app import currency, read_indexes
    from app.warehouse import COLUMNS, insert_rows

    monkeypatch.setattr(read_indexes, "ENABLED", True)
    monkeypatch.setenv("CLOUDLENS_DERIVED_LAYOUT", "monthly")
    monkeypatch.setenv("FX_RATES", '{"EUR":0.8,"INR":80}')
    monkeypatch.setattr(currency, "_cache", {"at": 0, "rates": {}, "date": None, "source": None})
    wh = module_postgres_warehouse
    rows = []
    for code in ("USD", "EUR", None):
        for sub in ("sub-a", "SUB-A", "sub-b", None):
            for i in range(36):
                for day in (date(2026, 7, 31), date(2026, 8, 1), date(2026, 8, 2),
                            date(2026, 9, 1)):
                    cost = (i % 7 - 3) if code else 0
                    row = {
                        "ChargePeriodStart": day, "BillingCurrency": code,
                        "PricingCurrency": code, "SubAccountId": sub,
                        "SubAccountName": sub, "ResourceId": f"/id/{i // 2}" if i else None,
                        "ResourceName": f"Name {i}", "ResourceGroup": "group",
                        "RegionName": None if i == 0 else f"region-{i % 24}",
                        "ServiceFamily": "Compute", "ServiceName": "Compute",
                        "ChargeCategory": "Usage", "PricingModel": "OnDemand",
                        "BillingCostBasis": "FOCUS", "ExchangeRatePricingToBilling": 1,
                        "BilledCost": cost, "InvoiceCost": cost, "CostInUsd": cost,
                        "EffectiveCost": cost, "ListCost": cost * 2,
                        "ContractedCost": cost * 1.5,
                        "Tags": '{"env":"prod"}' if i % 2 else '{"env":"dev"}',
                    }
                    rows.append([row.get(c) for c in COLUMNS])
    with wh.connect() as con:
        con.execute("DELETE FROM costs")
        insert_rows(con, rows)
    wh.refresh_rollup(strict=True)
    return wh


def test_index_default_and_computed_source(monkeypatch):
    from app import billing, read_indexes

    monkeypatch.setattr(read_indexes, "ENABLED", False)
    assert read_indexes.index_sql("public", "billing_rollup_a", "billing_rollup") == []
    window = billing.period("2026-08", date(2026, 9, 6))
    assert billing._statement_sql(window, "source", indexed=True) == billing._statement_sql(
        window, "source")


@pytest.mark.postgres
@pytest.mark.parametrize("target", ["", "INR", "USD"])
def test_executive_complete_equivalence_and_null_identity(indexed_wh, monkeypatch, target):
    from app import dashboard, read_indexes, reporting

    view = dashboard.Dashboard()
    view.wh = indexed_wh
    with reporting.select("2026-08"):
        with indexed_wh.reader(currency=target) as r:
            assert r.res_ordered
            expected = {x["currency"]: x["n"] for x in r.rows(
                'SELECT "BillingCurrency" AS currency, count(*) AS n FROM '
                '(SELECT DISTINCT "BillingCurrency","ResourceId" FROM costs) x GROUP BY 1')}
        actual = view.executive(None, currency=target)
        monkeypatch.setattr(read_indexes, "ENABLED", False)
        baseline = view.executive(None, currency=target)
    assert actual == baseline
    for native in actual["by_currency"] or [actual]:
        code = native["currency"]
        assert native["kpis"]["resources"] == expected[None if code == "unknown" else code]


@pytest.mark.postgres
@pytest.mark.parametrize("filters", [
    {"env": ["prod"]}, {"env": ["prod", "dev"]},
    {"service": ["Compute"]}, {"charge": ["Usage"]},
])
@pytest.mark.parametrize("target", ["", "INR", "XYZ"])
def test_scoped_billing_complete_equivalence(indexed_wh, monkeypatch, filters, target):
    from app import billing, billing_rollup, read_indexes

    monkeypatch.setattr(billing, "BREAKDOWN_LIMIT", 7)
    with indexed_wh.reader(["SUB-A"], filters=filters) as r:
        assert r.bill_indexed == billing_rollup.serves_filters(filters)
        assert not r.res_ordered
        window = billing.period("2026-08", date(2026, 9, 6))
        candidate_rows = r.rows(billing._statement_sql(
            window, r.bill, stored=r.bill_stored, indexed=r.bill_indexed), None)
        baseline_rows = r.rows(billing._statement_sql(window, r.bill), None)
        def key(row):
            return tuple(str(row.get(k)) for k in (
                "bucket", "day", "currency", "basis", "id", "subscription_id"))
        assert sorted(candidate_rows, key=key) == sorted(baseline_rows, key=key)
    candidate = billing.build(["SUB-A"], target, "2026-08", filters, warehouse=indexed_wh)
    monkeypatch.setattr(read_indexes, "ENABLED", False)
    baseline = billing.build(["SUB-A"], target, "2026-08", filters, warehouse=indexed_wh)
    assert candidate == baseline
    assert {x["subscription_id"] for x in candidate["breakdowns"]["resources"]
            if not x["is_other"]} <= {"sub-a", "SUB-A"}
    if target == "XYZ":
        assert not candidate["fx"]["complete"]


@pytest.mark.postgres
def test_missing_wrong_partial_and_stale_indexes_fall_back(indexed_wh, monkeypatch):
    from app import billing_rollup, derived, jobs, presence, read_indexes

    wh, schema = indexed_wh, indexed_wh._rollup_schema
    with wh.connect() as con:
        active = derived.active_table(con, schema, billing_rollup)
        index = f'"{schema}".{active}_read_lookup_v1'
        con.execute(f"DROP INDEX {index}")
    with wh.reader() as r:
        assert r.bill_stored and not r.bill_indexed and r.res_ordered
    # Neither a familiar name with the wrong shape nor a partial key index certifies.
    for definition in ['("ChargePeriodStart")',
                       '(lower("SubAccountId"),currency,resource_id,"ChargePeriodStart") '
                       'WHERE recorded_cost_total > 0']:
        # Partial indexes are not supported on the partitioned parent in every PG
        # release; create it on a flat stand-in and test the same validator.
        with wh.connect() as con:
            con.execute(billing_rollup.table_sql(schema, "index_standin"))
            con.execute(f'CREATE INDEX index_standin_read_lookup_v1 '
                        f'ON "{schema}".index_standin {definition}')
            assert not read_indexes.available(con, schema, "index_standin", billing_rollup.TABLE)
            con.execute(f'DROP TABLE "{schema}".index_standin')
    with wh.connect() as con:
        for sql in billing_rollup.index_sql(schema, active):
            con.execute(sql)
        assert read_indexes.available(con, schema, active, billing_rollup.TABLE)
    monkeypatch.setattr(wh, "_rollup_fresh", lambda *args: False)
    monkeypatch.setattr(jobs, "fresh_derived_tables", lambda con: frozenset())
    with wh.reader() as r:
        assert not r.bill_stored and not r.bill_indexed and not r.res_ordered
    with wh.reader(["sub-a"], "INR") as r:
        assert not r.bill_indexed and not r.res_ordered and r.res == presence.TABLE


@pytest.mark.postgres
def test_month_repair_full_swap_failure_and_gate_off(indexed_wh, monkeypatch):
    from app import billing_rollup, derived, jobs, presence, read_indexes

    wh, schema = indexed_wh, indexed_wh._rollup_schema
    with wh.connect() as con:
        before = derived.active_table(con, schema, presence)
    wh.refresh_rollup(strict=True, window=(date(2026, 8, 2), date(2026, 8, 3)))
    with wh.reader() as r:
        assert r.res_ordered and r.bill_indexed
        assert derived.active_table(r._con, schema, presence) == before
    wh.refresh_rollup(strict=True)
    with wh.reader() as r:
        assert r.res_ordered and r.bill_indexed
        active = derived.active_table(r._con, schema, presence)
        assert active != before
        before_rows = r.rows('SELECT sum("BilledCost") AS money, sum(charges) AS n FROM costs_rollup')
    original = billing_rollup.index_sql
    monkeypatch.setattr(billing_rollup, "index_sql", lambda *args: [*original(*args), "SELECT 1 / 0"])
    with pytest.raises(Exception, match="division by zero"):
        wh.refresh_rollup(strict=True)
    with wh.reader() as r:
        assert not r.res_ordered and not r.bill_indexed
        assert derived.active_table(r._con, schema, presence) == active
        assert r.rows(f'SELECT sum("BilledCost") AS money, sum(charges) AS n '
                      f'FROM "{schema}".costs_rollup') == before_rows
        assert not jobs.rollup_current(r._con)
    monkeypatch.setattr(billing_rollup, "index_sql", original)
    wh.refresh_rollup(strict=True)
    with wh.reader() as r:
        assert r.res_ordered and r.bill_indexed
        assert jobs.rollup_current(r._con)
    monkeypatch.setattr(read_indexes, "ENABLED", False)
    with wh.reader() as r:
        assert r.bill_stored and not r.bill_indexed and not r.res_ordered
        assert jobs.rollup_current(r._con)


@pytest.mark.postgres
def test_certification_pins_publication_and_releases_on_error(indexed_wh):
    from psycopg.errors import LockNotAvailable

    from app import derived, presence

    wh, schema = indexed_wh, indexed_wh._rollup_schema
    with (pytest.raises(RuntimeError, match="abandoned"),
          wh.reader() as r, wh.connect() as publisher):
        assert r.res_ordered
        active = derived.active_table(r._con, schema, presence)
        publisher.execute("SET lock_timeout='100ms'")
        with pytest.raises(LockNotAvailable):
            publisher.execute(f'CREATE OR REPLACE VIEW "{schema}".resource_presence '
                              f'AS SELECT * FROM "{schema}".{active}')
        # Compatible source writes remain allowed; no rows are changed here.
        publisher.execute("UPDATE costs SET \"BilledCost\"=\"BilledCost\" WHERE false")
        raise RuntimeError("abandoned")
    with wh.connect() as publisher:
        publisher.execute("SET lock_timeout='100ms'")
        publisher.execute(f'CREATE OR REPLACE VIEW "{schema}".resource_presence '
                          f'AS SELECT * FROM "{schema}".{active}')


@pytest.mark.postgres
def test_new_month_inherits_indexes_and_reader_prunes(indexed_wh, monkeypatch):
    import json

    from app import billing_rollup, derived, presence, read_indexes, reporting
    from app.warehouse import COLUMNS, insert_rows

    monkeypatch.setattr(reporting, "today", lambda: date(2026, 11, 1))
    wh = indexed_wh
    row = {"ChargePeriodStart": date(2026, 10, 2), "BillingCurrency": "USD",
           "SubAccountId": "sub-a", "ResourceId": "/new-month", "BilledCost": 7}
    with wh.connect() as con:
        insert_rows(con, [[row.get(c) for c in COLUMNS]])
    wh.refresh_rollup(strict=True, window=(date(2026, 10, 2), date(2026, 10, 3)))
    with reporting.select("2026-10"), wh.reader() as r:
        assert r.res_ordered and r.bill_indexed
        assert r.rows(f'SELECT DISTINCT "ResourceId" FROM {r.res}') == [
            {"ResourceId": "/new-month"}]
        for module in (presence, billing_rollup):
            table = derived.active_table(r._con, wh._rollup_schema, module)
            assert read_indexes.available(r._con, wh._rollup_schema, table, module.TABLE)
        plan = json.dumps(r._con.execute(
            f'EXPLAIN (FORMAT JSON) SELECT DISTINCT "ResourceId","BillingCurrency" '
            f'FROM {r.res} ORDER BY "ResourceId","BillingCurrency"').fetchone()[0])
        assert "_202610" in plan
        assert not any(month in plan for month in ("_202607", "_202608", "_202609"))


@pytest.mark.postgres
@pytest.mark.parametrize("target", ["", "INR"])
def test_native_ties_and_converted_metadata_ignore_query_order(indexed_wh, target):
    from app import billing

    window = billing.period("2026-08", date(2026, 9, 6))
    with indexed_wh.reader(["sub-a"]) as r:
        rows = r.rows(billing._statement_sql(
            window, r.bill, stored=True, indexed=r.bill_indexed), None)
    daily = [row for row in rows if row["bucket"] == "daily" and row["currency"]]
    groups = [{**row, "name": f'{row["currency"]}-{row["name"]}'}
              for row in rows if row["bucket"] != "daily" and row["currency"]]
    assert billing.assemble(daily, groups, window, target) == billing.assemble(
        daily, list(reversed(groups)), window, target)
