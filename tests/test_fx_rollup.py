"""Exact converted aggregates against raw rows, only in disposable fixture schemas."""
from __future__ import annotations

import asyncio
import json
import runpy
from datetime import date
from pathlib import Path
from unittest.mock import AsyncMock

import pytest


def test_gate_and_cache_identity(monkeypatch):
    from app import currency, fx_rollup

    monkeypatch.delenv("CLOUDLENS_FX_ROLLUP", raising=False)
    assert not fx_rollup.enabled()
    monkeypatch.setenv("CLOUDLENS_FX_ROLLUP", "true")
    assert fx_rollup.enabled()
    monkeypatch.setenv("CLOUDLENS_FX_ROLLUP", "typo")
    with pytest.raises(ValueError):
        fx_rollup.enabled()
    monkeypatch.delenv("FX_RATES", raising=False)
    monkeypatch.setattr(currency, "_cache", {
        "rates": {"INR": 80, "ZAR": 20}, "source": "test", "date": "2026-09-01", "at": 0})
    before = currency.cache_identity("INR")
    native = currency.cache_identity("")
    currency._cache["rates"]["ZAR"] = 25
    assert before != currency.cache_identity("INR")
    assert native == currency.cache_identity("")


def _insert(wh, overrides):
    from app.warehouse import COLUMNS, insert_rows

    base = {
        "ChargePeriodStart": date(2026, 8, 2), "BillingCurrency": "EUR",
        "SubAccountId": "sub-a", "SubAccountName": "A", "ServiceFamily": "Compute",
        "ServiceName": "Compute", "ResourceGroup": "rg", "RegionName": "west",
        "PublisherName": "Microsoft", "ChargeCategory": "Usage", "PricingModel": "OnDemand",
        "ResourceId": "/vm/a", "ResourceName": "VM", "Tags": '{"env":"Prod"}',
        "BilledCost": 10.0, "CostInUsd": 99.0, "PricingQuantity": 2.0,
        "BillingCostBasis": "FOCUS", "PricingCurrency": "USD",
        "InvoiceCost": 10.0, "EffectiveCost": 8.0, "ListCost": 15.0, "ContractedCost": 12.0,
        "ExchangeRatePricingToBilling": 0.8,
    }
    with wh.connect() as con:
        insert_rows(con, [[{**base, **values}.get(c) for c in COLUMNS] for values in overrides])


@pytest.fixture
def fx_warehouse(monkeypatch, postgres_warehouse):
    from app import currency, fx_rollup
    from app import warehouse as module

    monkeypatch.setenv("FX_RATES", '{"EUR":0.5,"INR":80,"GBP":0.4,"ZAR":20}')
    monkeypatch.setattr(currency, "_cache", {
        "at": 0, "rates": {}, "source": None, "date": None})
    monkeypatch.setattr(fx_rollup, "ENABLED", True)
    if fx_rollup not in module.DERIVED_MODULES:
        monkeypatch.setattr(module, "DERIVED_MODULES", (*module.DERIVED_MODULES, fx_rollup))
    rows = [
        {}, {"BilledCost": -10.0},  # cancellation must not lose invoice FX
        {"ExchangeRatePricingToBilling": 0.7},
        {"PricingCurrency": "GBP", "ExchangeRatePricingToBilling": 2.0},
        {"PricingCurrency": "INR", "ExchangeRatePricingToBilling": 0.01},
        {"PricingCurrency": "", "ExchangeRatePricingToBilling": None},
        {"BillingCurrency": "USD"},
        {"BillingCurrency": " gbp ", "PricingCurrency": " xyz ",
         "ExchangeRatePricingToBilling": -1},
        {"BillingCostBasis": "ActualCost", "CostInUsd": 5.0},
        {"BillingCostBasis": "AmortizedCost", "CostInUsd": 5.0,
         "ExchangeRatePricingToBilling": None},
        {"BillingCostBasis": None, "CostInUsd": 0.0, "ExchangeRatePricingToBilling": 0},
        {"BillingCostBasis": "ActualCost", "BilledCost": -10.0, "CostInUsd": -5.0,
         "ExchangeRatePricingToBilling": None},
        {"BillingCostBasis": "ActualCost", "BilledCost": 0.0, "CostInUsd": 5.0},
        {"BilledCost": None, "CostInUsd": None, "PricingQuantity": None},
        {"BillingCurrency": "", "PricingCurrency": "", "BilledCost": 0.0,
         "ExchangeRatePricingToBilling": None},
        {"BillingCurrency": None, "PricingCurrency": None, "BilledCost": None,
         "ExchangeRatePricingToBilling": None},
    ]
    expanded = [
        {**r, "SubAccountId": sub, "ChargePeriodStart": day, "Tags": tags}
        for r in rows for sub in ("sub-a", "sub-b")
        for day in (date(2026, 7, 3), date(2026, 8, 2))
        for tags in ('{"env":"Prod"}', '{"env":"dev"}')
    ]
    _insert(postgres_warehouse, expanded * 2)
    postgres_warehouse.refresh_rollup(strict=True)
    return postgres_warehouse


def _plan(reader, table):
    return json.dumps(reader._con.execute(
        f'EXPLAIN (VERBOSE, FORMAT JSON) SELECT sum("BilledCost") FROM {table} '
        '''WHERE "ChargePeriodStart" >= DATE '2026-08-01'
        AND "ChargePeriodStart" < DATE '2026-09-01' '''
    ).fetchone()[0])


def _compare(reader):
    columns = (
        '"ChargePeriodStart", "ServiceName", "BillingCurrency", '
        'sum("BilledCost") AS money, sum("CostInUsd") AS usd, '
        'sum("PricingQuantity") AS quantity, {} AS charges'
    )
    tail = ' GROUP BY 1,2,3 ORDER BY 1,2,3'
    a = reader.rows(f'SELECT {columns.format("sum(charges)")} FROM {reader.agg}{tail}')
    b = reader.rows(f'SELECT {columns.format("count(*)")} FROM costs{tail}')
    assert len(a) == len(b)
    for actual, expected in zip(a, b, strict=True):
        for name in actual:
            if name in {"money", "usd", "quantity"} and expected[name] is not None:
                assert actual[name] == pytest.approx(expected[name], abs=1e-8)
            else:
                assert actual[name] == expected[name]


@pytest.mark.parametrize("target", ["USD", "EUR", "INR", "GBP", "ZAR"])
@pytest.mark.parametrize("options", [
    {}, {"scope": ["SUB-A"]}, {"scope": ["no-access"]},
    {"scope": ["sub-a"], "filters": {"service": ["compute"], "charge": ["Usage"]}},
    {"filters": {"env": ["prod"]}}, {"filters": {"env": ["prod", "dev"]}},
])
def test_converted_aggregate_is_exact(fx_warehouse, target, options):
    from app import environment

    with fx_warehouse.reader(currency=target, **options) as reader:
        _compare(reader)
        plan = _plan(reader, reader.agg)
        supported = not options.get("filters", {}).get("env") or environment.ENABLED
        assert ("costs_fx_rollup" in plan) == supported
        assert reader.named == "costs"
        assert not reader.bill_stored
        assert reader.bill_serving is None


def test_unknown_nonzero_rows_fail_even_when_the_net_is_zero(fx_warehouse):
    for amount in (10.0, -10.0):
        _insert(fx_warehouse, [{
            "SubAccountId": "forbidden", "BillingCurrency": "XXX", "PricingCurrency": "",
            "ExchangeRatePricingToBilling": None, "BilledCost": amount,
        }])
    fx_warehouse.refresh_rollup(strict=True)
    with (pytest.raises(ValueError, match="no authoritative exchange rate"),
          fx_warehouse.reader(currency="USD")):
        pass
    with fx_warehouse.reader(scope=["sub-a"], currency="USD") as reader:
        _compare(reader)
    with fx_warehouse.reader(filters={"service": ["absent"]}, currency="USD") as reader:
        assert reader.rows(f'SELECT count(*) AS n FROM {reader.agg}') == [{"n": 0}]


def test_rate_changes_recompute_without_rebuild(fx_warehouse, monkeypatch):
    from app import fx_rollup

    with fx_warehouse.connect() as con:
        before = con.execute(f'SELECT * FROM {fx_rollup.TABLE}').fetchall()
    with fx_warehouse.reader(currency="INR") as reader:
        first = reader.rows(f'SELECT sum("BilledCost") AS total FROM {reader.agg}')[0]["total"]
    monkeypatch.setenv("FX_RATES", '{"EUR":0.8,"INR":100,"GBP":0.6,"ZAR":25}')
    with fx_warehouse.reader(currency="INR") as reader:
        _compare(reader)
        second = reader.rows(f'SELECT sum("BilledCost") AS total FROM {reader.agg}')[0]["total"]
    assert first != second
    with fx_warehouse.connect() as con:
        assert con.execute(f'SELECT * FROM {fx_rollup.TABLE}').fetchall() == before


@pytest.mark.parametrize("reason", ["stale", "unstamped", "disabled"])
def test_fallback_and_recovery(fx_warehouse, monkeypatch, reason):
    from app import fx_rollup, jobs

    if reason == "disabled":
        monkeypatch.setattr(fx_rollup, "ENABLED", False)
    else:
        with fx_warehouse.connect() as con:
            if reason == "stale":
                jobs.invalidate_rollup(con)
            else:
                con.execute("UPDATE meta SET value=? WHERE key=?",
                            ["costs_rollup,resource_presence,billing_rollup,billing_serving,"
                             "commitment_benefits,spot_rates",
                             jobs.ROLLUP_TABLES_KEY])
    with fx_warehouse.reader(currency="INR") as reader:
        _compare(reader)
        assert "costs_fx_rollup" not in _plan(reader, reader.agg)
    monkeypatch.setattr(fx_rollup, "ENABLED", True)
    fx_warehouse.refresh_rollup(strict=True)
    with fx_warehouse.reader(currency="INR") as reader:
        _compare(reader)
        assert "costs_fx_rollup" in _plan(reader, reader.agg)


def test_failed_build_preserves_native_statistics(fx_warehouse, monkeypatch):
    from app import fx_rollup, jobs

    with fx_warehouse.connect() as con:
        before = con.execute(f'SELECT * FROM {fx_rollup.TABLE}').fetchall()
        jobs.invalidate_rollup(con)
    with monkeypatch.context() as broken:
        def fail(*args, **kwargs):
            raise RuntimeError("FX build failed")
        broken.setattr(fx_rollup, "select_sql", fail)
        with pytest.raises(RuntimeError, match="FX build failed"):
            fx_warehouse.refresh_rollup(strict=True)
    with fx_warehouse.connect() as con:
        assert con.execute(f'SELECT * FROM {fx_rollup.TABLE}').fetchall() == before
        assert not jobs.rollup_current(con, fx_rollup.TABLE)
    with fx_warehouse.reader(currency="USD") as reader:
        _compare(reader)
        assert "costs_fx_rollup" not in _plan(reader, reader.agg)
    fx_warehouse.refresh_rollup(strict=True)
    assert fx_warehouse.check_readiness()


def test_partition_pruning_and_incremental_repair(fx_warehouse, monkeypatch):
    from app import derived, fx_rollup

    monkeypatch.setenv("CLOUDLENS_DERIVED_LAYOUT", "monthly")
    fx_warehouse.refresh_rollup(strict=True)
    with fx_warehouse.connect() as con:
        active = derived.active_table(con, fx_warehouse._rollup_schema, fx_rollup)
    with fx_warehouse.reader(currency="USD") as reader:
        plan = _plan(reader, reader.agg)
        assert f"{active}_202608" in plan
        assert f"{active}_202607" not in plan
    _insert(fx_warehouse, [{"BilledCost": 3.0, "CostInUsd": 1.0}])
    fx_warehouse.refresh_rollup(strict=True, window=(date(2026, 8, 1), date(2026, 9, 1)))
    with fx_warehouse.connect() as con:
        assert derived.active_table(con, fx_warehouse._rollup_schema, fx_rollup) == active
    with fx_warehouse.reader(currency="USD") as reader:
        _compare(reader)


@pytest.mark.parametrize("values,target,expected", [
    ({}, "USD", 12.5),
    ({}, "EUR", 10.0),
    ({"BillingCostBasis": "ActualCost", "ExchangeRatePricingToBilling": None,
      "CostInUsd": 3.0}, "USD", 3.0),
    ({"BillingCostBasis": None, "ExchangeRatePricingToBilling": None,
      "CostInUsd": 3.0}, "INR", 240.0),
    ({"PricingCurrency": "GBP", "ExchangeRatePricingToBilling": 2.0}, "GBP", 5.0),
    ({"PricingCurrency": "", "ExchangeRatePricingToBilling": None}, "USD", 20.0),
    ({"BillingCurrency": "", "PricingCurrency": "", "BilledCost": 0,
      "ExchangeRatePricingToBilling": None}, "USD", None),
    ({"BilledCost": None}, "USD", None),
])
def test_authoritative_branch_values(fx_warehouse, values, target, expected):
    _insert(fx_warehouse, [{**values, "SubAccountId": "isolated"}])
    fx_warehouse.refresh_rollup(strict=True)
    with fx_warehouse.reader(scope=["isolated"], currency=target) as reader:
        row = reader.rows(
            f'SELECT sum("BilledCost") AS money, sum(charges) AS charges FROM {reader.agg}')[0]
        assert row["money"] == expected
        assert row["charges"] == 1
        _compare(reader)


def test_first_enable_falls_back_until_rebuilt(monkeypatch, postgres_warehouse):
    from app import fx_rollup, jobs
    from app import warehouse as module

    wh = postgres_warehouse
    monkeypatch.setenv("FX_RATES", '{"EUR":0.5}')
    monkeypatch.setattr(fx_rollup, "ENABLED", False)
    original = tuple(m for m in module.DERIVED_MODULES if m is not fx_rollup)
    monkeypatch.setattr(module, "DERIVED_MODULES", original)
    _insert(wh, [{}])
    wh.refresh_rollup(strict=True)
    monkeypatch.setattr(fx_rollup, "ENABLED", True)
    monkeypatch.setattr(module, "DERIVED_MODULES", (*original, fx_rollup))
    with wh.connect() as con:
        con.execute("BEGIN")
        jobs.lock_generation(con)
        wh._ensure_derived(con, wh._rollup_schema)
        con.execute("COMMIT")
        assert not jobs.rollup_current(con, fx_rollup.TABLE)
        assert jobs.rebuild_windows(con, [m.TABLE for m in module.DERIVED_MODULES]) is None
    with wh.reader(currency="USD") as reader:
        _compare(reader)
        assert "costs_fx_rollup" not in _plan(reader, reader.agg)
    wh.refresh_rollup(strict=True)
    with wh.reader(currency="USD") as reader:
        _compare(reader)
        assert "costs_fx_rollup" in _plan(reader, reader.agg)


def test_legacy_currency_contract_without_network(monkeypatch, postgres_warehouse):
    from app import currency

    monkeypatch.setenv("FX_RATES", "")
    monkeypatch.setattr(currency, "_cache", {
        "at": 0, "rates": {}, "source": None, "date": None})
    monkeypatch.setattr(currency, "_fetch", AsyncMock(return_value={
        "rates": {"INR": 80.0, "AED": 3.6725, "EUR": 0.9, "GBP": 0.8},
        "date": "2026-09-01", "source": "mocked ECB",
    }))
    tests = Path(__file__).parent
    monkeypatch.syspath_prepend(str(tests))
    with pytest.raises(SystemExit) as stopped:
        runpy.run_path(str(tests / "test_currency.py"), run_name="__main__")
    assert stopped.value.code == 0


@pytest.mark.parametrize("environments", [["prod"], ["prod", "dev"]])
def test_request_route_preserves_native_currency_and_filtered_scope(
        fx_warehouse, monkeypatch, environments):
    from fastapi import HTTPException
    from starlette.requests import Request

    from app import dashboard, main, reporting
    from app.auth import User

    monkeypatch.setattr(reporting, "today", lambda: date(2026, 9, 21))
    view = dashboard.Dashboard()
    view.wh = fx_warehouse
    monkeypatch.setattr(dashboard, "dashboard", view)
    monkeypatch.setattr(main, "permitted", AsyncMock(return_value=["sub-a"]))
    request = Request({
        "type": "http", "query_string": ("env=" + ",".join(environments)).encode(),
        "state": {"user": User("test-user")},
    })
    assert main.request_filters(request) == {"env": environments}
    with reporting.select("2026-08"):
        native = json.loads(asyncio.run(main.dashboard_sections(request)).body)
        assert native["mixed"] and native["total"] is None
        assert {"USD", "EUR"} <= {r["currency"] for r in native["total_by_currency"]}
        converted = json.loads(asyncio.run(
            main.dashboard_sections(request, currency="INR")).body)
        with fx_warehouse.reader(
                scope=["sub-a"], currency="INR", filters={"env": environments}) as reader:
            expected = reader.rows('SELECT sum("BilledCost") AS money FROM costs')[0]["money"]
        assert converted["total"] == round(expected, 2)
        assert converted["currency"] == "INR" and not converted["mixed"]
        assert converted["days"] == 31 and converted["as_of"] == "2026-08-31"
        with pytest.raises(HTTPException) as denied:
            asyncio.run(main.dashboard_sections(request, scope="sub-b", currency="INR"))
        assert denied.value.status_code == 403


def test_authority_preflight_obeys_selected_and_comparison_months(fx_warehouse, monkeypatch):
    from app import reporting

    monkeypatch.setattr(reporting, "today", lambda: date(2026, 9, 21))
    _insert(fx_warehouse, [{
        "ChargePeriodStart": date(2026, 7, 3), "BillingCurrency": "XXX",
        "PricingCurrency": "", "ExchangeRatePricingToBilling": None,
    }])
    fx_warehouse.refresh_rollup(strict=True)
    with reporting.select("2026-08"):
        with fx_warehouse.reader(currency="USD", scope=["sub-a"]) as reader:
            _compare(reader)
            assert "costs_fx_rollup" in _plan(reader, reader.agg)
        with (reporting.comparison_history(),
              pytest.raises(ValueError, match="no authoritative exchange rate"),
              fx_warehouse.reader(currency="USD", scope=["sub-a"])):
            pass
