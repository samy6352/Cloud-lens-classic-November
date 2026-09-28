"""Stored Billing rank-first plan must preserve all measures, not just spend."""
from __future__ import annotations

from contextlib import nullcontext
from datetime import date
from types import SimpleNamespace

import pytest

from app import billing, currency


@pytest.fixture(autouse=True)
def exercise_rollup_fallback(monkeypatch):
    # These comparisons exercise stored/detail SQL, not the compact serving layer.
    monkeypatch.setattr(billing, "_SERVING_ENABLED", False)


def _row(**values):
    return {
        "ChargePeriodStart": "2026-08-01", "SubAccountId": "sub-a",
        "SubAccountName": "Subscription A", "ResourceId": "/shared",
        "ResourceName": "Same name", "ResourceGroup": "group",
        "ServiceName": "Compute", "RegionName": "west",
        "BillingCurrency": "USD", "BillingCostBasis": "FOCUS",
        "ChargeCategory": "Usage", "PricingModel": "OnDemand",
        "BilledCost": 10, "InvoiceCost": 10, "ListCost": 20,
        "ContractedCost": 15, "EffectiveCost": 10, "Tags": '{"env":"prod"}',
        **values,
    }


@pytest.fixture
def estate(module_postgres_warehouse):
    from app.warehouse import COLUMNS, insert_rows

    wh = module_postgres_warehouse
    rows = []
    for code in ("USD", "EUR", None):
        for sub in ("sub-a", "sub-b"):
            for i, cost in enumerate((100, -100, 100, 0, -8, 3)):
                rows.append(_row(
                    BillingCurrency=code, SubAccountId=sub, ResourceId=f"/r/{i}",
                    BilledCost=cost, InvoiceCost=cost,
                    ServiceName="Compute" if i % 2 else "Storage",
                    RegionName="west" if i % 2 else "east",
                    CommitmentDiscountType="Reservation" if i % 2 else "SavingsPlan",
                ))
        rows.extend([
            _row(BillingCurrency=code, ResourceId=None, ResourceName=None,
                 ResourceGroup=None, ServiceName=None, SubAccountId=None,
                 SubAccountName=None, InvoiceCost=None, ContractedCost=None),
            _row(BillingCurrency=code, ResourceId="", ResourceName="orphan", BilledCost=-7),
            _row(BillingCurrency=code, ResourceId="/r/0", ResourceName="A name",
                 ResourceGroup="a-group", ChargePeriodStart="2026-08-02", BilledCost=0),
            _row(BillingCurrency=code, ResourceId="/unused", ChargeCategory="UnusedReservation",
                 EffectiveCost=5, ListCost=None, ContractedCost=None),
            _row(BillingCurrency=code, ResourceId="/purchase", ChargeCategory="Purchase",
                 BilledCost=9, InvoiceCost=9),
            _row(BillingCurrency=code, ResourceId="/unverified", BillingCostBasis=None,
                 ListCost=0, EffectiveCost=0, ContractedCost=0, InvoiceCost=0),
            _row(BillingCurrency=code, ResourceId="/prior", ChargePeriodStart="2026-07-31",
                 BilledCost=9999, InvoiceCost=9999),
            _row(BillingCurrency=code, ResourceId="/future", ChargePeriodStart="2026-09-01",
                 BilledCost=99999, InvoiceCost=99999),
        ])
    with wh.connect() as con:
        con.execute("DELETE FROM costs")
        insert_rows(con, [[row.get(column) for column in COLUMNS] for row in rows])
    wh.refresh_rollup(strict=True)
    return wh


def _equivalent(actual, expected):
    if isinstance(expected, dict):
        assert actual.keys() == expected.keys()
        for key in expected:
            _equivalent(actual[key], expected[key])
    elif isinstance(expected, list):
        assert len(actual) == len(expected)
        for a, b in zip(actual, expected):
            _equivalent(a, b)
    elif isinstance(expected, float):
        assert actual == pytest.approx(expected, rel=1e-12, abs=1e-8)
    else:
        assert actual == expected


@pytest.mark.postgres
@pytest.mark.parametrize("indexed", [False, True])
@pytest.mark.parametrize(("scope", "filters", "month", "limit", "target"), [
    (None, None, "2026-08", 3, ""),
    (["sub-a", "sub-b"], None, "2026-08", 3, "USD"),
    (["sub-a"], {"service": ["Storage"]}, "2026-08", 3, ""),
    (["sub-b"], {"region": ["west"]}, "2026-08", 3, ""),
    (None, {"charge": ["Usage"], "pricing": ["OnDemand"]}, "2026-08", 3, ""),
    (None, {"rg": ["group"]}, "2026-08", 3, ""),
    (["nobody"], None, "2026-08", 3, ""),
    (None, None, "2026-07", 3, ""),
    (None, None, "2026-06", 3, ""),
    (None, None, "2026-08", 200, ""),
])
def test_stored_plan_all_measures_and_statement(
        estate, monkeypatch, scope, filters, month, limit, target, indexed):
    from app import read_indexes

    monkeypatch.setattr(read_indexes, "ENABLED", indexed)
    if indexed:
        estate.refresh_rollup(strict=True)
    monkeypatch.setattr(billing, "BREAKDOWN_LIMIT", limit)
    monkeypatch.setattr(currency, "_cache", {"at": 0, "rates": {}, "date": None, "source": None})
    monkeypatch.setenv("FX_RATES", '{"EUR": 0.8}')
    window = billing.period(month, date(2026, 9, 6))
    with estate.reader(scope, None, filters) as reader:
        assert reader.bill_indexed == indexed
        baseline = reader.rows(billing._statement_sql(window, reader.bill), 20000)
        candidate = reader.rows(billing._statement_sql(
            window, reader.bill, stored=True, indexed=indexed), 20000)
    def key(row):
        return tuple(str(row.get(k)) for k in (
            "bucket", "day", "currency", "basis", "id", "subscription_id"))

    _equivalent(sorted(candidate, key=key), sorted(baseline, key=key))
    original_sql = billing._statement_sql
    monkeypatch.setattr(billing, "_statement_sql", lambda w, s, **kw: original_sql(w, s))
    expected = billing.build(scope, target, month, filters, warehouse=estate)
    monkeypatch.setattr(billing, "_statement_sql", original_sql)
    actual = billing.build(scope, target, month, filters, warehouse=estate)
    _equivalent(actual, expected)
    assert actual["coverage"]["current"]["rows"] == expected["coverage"]["current"]["rows"]
    if scope is None and filters is None and month == "2026-08" and limit == 3:
        selected = [r for r in candidate if r["bucket"] == "resources" and r["currency"] == "USD"]
        assert {(r["id"], r["subscription_id"]) for r in selected} == {
            ("/r/0", "sub-a"), ("/r/0", "sub-b"), ("/r/1", "sub-a")}
        first = next(r for r in selected if r["id"] == "/r/0" and r["subscription_id"] == "sub-a")
        assert (first["name"], first["resource_group"]) == ("A name", "a-group")
        others = [r for r in actual["breakdowns"]["resources"] if r["is_other"]]
        assert len(others) == 3
        assert all(r["group_count"] == 14 for r in others)
        for native in actual["per_currency"]:
            if native["currency"]:
                shown = [r for r in actual["breakdowns"]["resources"]
                         if r["currency"] == native["currency"]]
                assert sum(r["cost"] for r in shown) == native["kpis"]["recorded_cost"]


def test_computed_source_keeps_original_aggregation():
    window = billing.period("2026-08", date(2026, 9, 6))
    sql = billing._statement_sql(window, "billing_rollup")
    assert "resource_costs" not in sql
    assert '(currency,"SubAccountId",resource_id)' in sql
    candidate = billing._statement_sql(window, "billing_rollup", stored=True)
    assert "JOIN resource_selected k USING" in candidate
    assert '(currency,"SubAccountId",resource_id)' not in candidate
    assert "resource_selected AS MATERIALIZED" in candidate
    assert "CROSS JOIN LATERAL" in candidate
    assert f"LIMIT {billing.BREAKDOWN_LIMIT}" in candidate


@pytest.mark.postgres
@pytest.mark.parametrize(("mode", "scope"), [
    ("stored", None), ("stored", ["sub-a"]),
    ("env", ["sub-a"]), ("stale", None), ("stale", ["sub-a"]),
])
def test_build_uses_only_certified_stored_plan(estate, monkeypatch, mode, scope):
    from app import jobs

    selected = []
    original_sql = billing._statement_sql

    def capture(window, source, *, stored=False, indexed=False):
        selected.append(stored)
        return original_sql(window, source, stored=stored, indexed=indexed)

    monkeypatch.setattr(billing, "_statement_sql", capture)
    if mode == "stale":
        monkeypatch.setattr(jobs, "fresh_derived_tables", lambda con: frozenset())
        monkeypatch.setattr(estate, "_rollup_fresh", lambda con, table=None: False)
    filters = {"env": ["prod"]} if mode == "env" else None
    actual = billing.build(scope, "", "2026-08", filters, warehouse=estate)
    assert selected == [mode == "stored"]
    assert actual["coverage"]["current"]["rows"] > 0
    if scope:
        assert all(row["subscription_id"] == "sub-a"
                   for row in actual["breakdowns"]["resources"] if not row["is_other"])


def test_build_without_capability_keeps_computed_plan():
    statements = []
    reader = SimpleNamespace(
        bill="billing_rollup", rows=lambda sql, limit: statements.append(sql) or [])
    wh = SimpleNamespace(reader=lambda *args: nullcontext(reader))
    actual = billing.build(None, "", "2026-08", warehouse=wh)
    assert "resource_costs" not in statements[0]
    assert actual["coverage"]["current"]["rows"] == 0
