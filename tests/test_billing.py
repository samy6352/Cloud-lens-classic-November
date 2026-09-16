"""Billing math offline; production SQL is opt-in on an isolated dev PostgreSQL schema."""
from __future__ import annotations

import asyncio
import json
import os
import sys
from contextlib import asynccontextmanager
from datetime import date, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException
from starlette.requests import Request

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.environ.setdefault("CLOUDLENS_ENV_FILE", str(ROOT / "tests" / "absent.env"))

from app import billing, currency, main  # noqa: E402
from app.auth import User  # noqa: E402

USER = User("alice", email="alice@example.invalid")
MONTH = billing.period("2026-08", date(2026, 9, 6))


def row(**values):
    return {
        "ChargePeriodStart": "2026-08-01", "SubAccountId": "sub-a",
        "SubAccountName": "Subscription A", "ResourceId": "/shared/resource",
        "ResourceName": "Same name", "ResourceGroup": "shared",
        "ServiceName": "Virtual Machines", "BillingCurrency": "USD",
        "BilledCost": 60, "InvoiceCost": 60, "ListCost": 100,
        "ContractedCost": 80, "EffectiveCost": 60, "BillingCostBasis": "FOCUS",
        "ChargeCategory": "Usage", "PricingModel": "OnDemand", **values,
    }


def fixture_statement(rows, target="", window=None):
    daily = [
        {**billing._row_stats(r), "day": r["ChargePeriodStart"],
         "currency": r.get("BillingCurrency") or "", "basis": r.get("BillingCostBasis")}
        for r in rows
    ]
    return billing.assemble(daily, [], window or MONTH, target)


@pytest.fixture(autouse=True)
def no_live_fx(monkeypatch):
    monkeypatch.delenv("FX_RATES", raising=False)
    monkeypatch.setattr(currency, "_cache", {"at": 0, "rates": {}, "date": None, "source": None})


@pytest.mark.parametrize(("today", "month", "days", "prior"), [
    (date(2026, 1, 1), "2025-12", 31, "2025-11"),
    (date(2024, 3, 1), "2024-02", 29, "2024-01"),
    (date(2026, 3, 31), "2026-02", 28, "2026-01"),
    (date(2026, 9, 6), "2026-08", 31, "2026-07"),
])
def test_calendar_months(today, month, days, prior):
    got = billing.period(today=today)
    assert (got["month"], got["days"], got["previous_month"]) == (month, days, prior)
    assert date.fromisoformat(got["end"]).day == days


def test_default_month_uses_utc_clock(monkeypatch):
    class Clock:
        @staticmethod
        def now(zone):
            assert zone is billing.UTC
            return SimpleNamespace(date=lambda: date(2026, 1, 1))
    monkeypatch.setattr(billing, "datetime", Clock)
    assert billing.period()["month"] == "2025-12"


@pytest.mark.parametrize("value", ["2026-9", "2026-13", "2026-09", "2027-01", "2026-08' OR 1=1", "0001-01"])
def test_invalid_or_unfinished_month(value):
    with pytest.raises(ValueError):
        billing.period(value, date(2026, 9, 6))


def test_achieved_savings_decompose_and_unused_is_negative():
    rows = [
        row(CommitmentDiscountType="Reservation"),
        row(CommitmentDiscountType="SavingsPlan", InvoiceCost=0, EffectiveCost=50),
        row(CommitmentDiscountType="Unknown benefit", EffectiveCost=70),
        row(ContractedCost=90, EffectiveCost=80),
        row(ChargeCategory="UnusedReservation", PricingModel="Reservation",
            ListCost=None, ContractedCost=None, EffectiveCost=15, InvoiceCost=0),
        row(ChargeCategory="Purchase", ListCost=900, ContractedCost=900,
            EffectiveCost=900, InvoiceCost=900, BilledCost=900),
        row(ChargeCategory="Credit", InvoiceCost=-12, BilledCost=-12),
        row(ChargeCategory="Refund", InvoiceCost=-5, BilledCost=-5),
    ]
    got = fixture_statement(rows)
    k = got["kpis"]
    assert k["list_cost"] == 400
    assert k["effective_cost"] == 275
    assert k["azure_discounts"] == 70
    assert k["reservation_savings"] == 5
    assert k["savings_plan_savings"] == 30
    assert k["other_commitment_savings"] == 10
    assert k["non_commitment_savings"] == 10
    assert k["actual_savings"] == 125
    assert k["effective_discount_pct"] == 31.25
    assert k["billed_cost"] == 1063
    assert sum(k[key] for key in ("azure_discounts", *billing.SAVINGS_KINDS)) == k["actual_savings"]
    assert got["pricing"]["excluded_rows"] == 3
    assert got["pricing"]["unused_rows"] == 1
    assert got["pricing"]["status"] == "complete"


def test_negative_usage_adjustments_are_not_clipped():
    got = fixture_statement([row(ListCost=-100, ContractedCost=-80, EffectiveCost=-60,
                                 InvoiceCost=-60, BilledCost=-60)])
    assert got["kpis"]["actual_savings"] == -40
    assert got["kpis"]["billed_cost"] == -60
    assert got["kpis"]["effective_discount_pct"] is None


@pytest.mark.parametrize(("missing", "metric"), [
    ("ListCost", "list_cost"), ("ContractedCost", "contracted_cost"),
    ("EffectiveCost", "effective_cost"), ("InvoiceCost", "billed_cost"),
])
def test_incomplete_metrics_null_with_known_subset(missing, metric):
    got = fixture_statement([row(), row(**{missing: None})])
    assert got["kpis"][metric] is None
    assert got["pricing"]["known_totals"][metric] == billing._metrics(billing._row_stats(row()))[metric]
    assert got["pricing"]["metric_coverage"][metric] == {"covered_rows": 1, "total_rows": 2}


def test_complete_actual_savings_with_missing_contract_does_not_fake_decomposition():
    got = fixture_statement([row(ContractedCost=None)])
    assert got["kpis"]["actual_savings"] == 40
    assert got["kpis"]["azure_discounts"] is None
    assert got["pricing"]["reconciled_totals"]["actual_savings"] is None
    assert got["pricing"]["status"] == "partial"


def test_legacy_rows_are_recorded_not_invoice():
    got = fixture_statement([row(InvoiceCost=None, EffectiveCost=None, ListCost=None,
                                 ContractedCost=None, BillingCostBasis=None)])
    assert got["kpis"]["recorded_cost"] == 60
    assert got["kpis"]["billed_cost"] is None
    assert got["kpis"]["actual_savings"] is None
    assert got["spend_basis"]["code"] == "unknown"
    assert got["kpis"]["reservation_savings"] is None


def test_optional_fixture_zeroes_without_basis_are_not_verified_financials():
    got = fixture_statement([row(InvoiceCost=0, ListCost=0, ContractedCost=0,
                                 EffectiveCost=0, BillingCostBasis=None)])
    assert got["kpis"]["recorded_cost"] == 60
    assert all(value is None for key, value in got["kpis"].items() if key != "recorded_cost")
    assert got["spend_basis"]["code"] == "unknown"
    assert got["pricing"]["status"] == "unavailable"
    assert got["pricing"]["metric_coverage"]["billed_cost"]["covered_rows"] == 0


@pytest.mark.parametrize("basis", [None, "AmortizedCost", "unknown"])
def test_invoice_zero_requires_invoice_dataset_provenance(basis):
    got = fixture_statement([row(InvoiceCost=0, BillingCostBasis=basis)])
    assert got["kpis"]["billed_cost"] is None
    assert got["spend_basis"]["metric"] == "recorded_cost"


@pytest.mark.parametrize("basis", ["FOCUS", "ActualCost"])
def test_provenanced_invoice_zero_is_preserved(basis):
    got = fixture_statement([row(InvoiceCost=0, BillingCostBasis=basis)])
    assert got["kpis"]["billed_cost"] == 0
    assert got["spend_basis"]["metric"] == "billed_cost"


def test_amortized_basis_and_no_data():
    got = fixture_statement([row(InvoiceCost=None, BillingCostBasis="AmortizedCost")])
    assert got["spend_basis"]["code"] == "amortized"
    empty = fixture_statement([])
    assert all(value is None for value in empty["kpis"].values())
    assert empty["coverage"]["current"]["status"] == "missing"
    assert empty["pricing"]["known_totals"]["billed_cost"] is None


def test_unknown_commitment_does_not_turn_into_ri():
    got = fixture_statement([row(BenefitId="/benefit/123", PricingModel="")])
    assert got["kpis"]["other_commitment_savings"] == 20
    assert got["kpis"]["reservation_savings"] == 0
    conflicting = fixture_statement([row(CommitmentDiscountType="unknown", PricingModel="Reservation")])
    assert conflicting["kpis"]["other_commitment_savings"] == 20
    assert conflicting["kpis"]["reservation_savings"] == 0


def test_unused_focus_allocation_included_but_upfront_payment_is_not():
    got = fixture_statement([
        row(CommitmentDiscountStatus="Unused", CommitmentDiscountType="SavingsPlan",
            ListCost=None, ContractedCost=None, EffectiveCost=25),
        row(ChargeCategory="Purchase", CommitmentDiscountStatus="Unused", EffectiveCost=999),
    ])
    assert got["kpis"]["actual_savings"] == -25
    assert got["kpis"]["savings_plan_savings"] == -25
    assert got["kpis"]["effective_discount_pct"] is None


def test_mixed_currency_refuses_to_sum_without_rate():
    got = fixture_statement([row(), row(BillingCurrency="EUR")], "USD")
    assert all(value is None for value in got["kpis"].values())
    assert {item["currency"]: item["kpis"]["billed_cost"] for item in got["per_currency"]} == {"EUR": 60, "USD": 60}
    assert not got["fx"]["complete"]


def test_native_currency_unlabeled_is_not_reported_as_money():
    got = fixture_statement([row(BillingCurrency=None)], "USD")
    assert got["kpis"]["recorded_cost"] is None
    assert got["per_currency"][0]["kpis"]["recorded_cost"] is None


def test_unconvertible_previous_currency_does_not_hide_current_month():
    got = fixture_statement([row(), row(ChargePeriodStart="2026-07-01", BillingCurrency="EUR")])
    assert got["currency"] == "USD"
    assert got["kpis"]["billed_cost"] == 60
    assert got["month_over_month"]["previous"] is None
    assert got["month_over_month"]["change"] is None


def test_consistent_fx_on_all_metrics_not_costinusd_replacement(monkeypatch):
    monkeypatch.setenv("FX_RATES", '{"EUR": 0.8, "INR": 80}')
    got = fixture_statement([row(CostInUsd=999999), row(BillingCurrency="EUR")], "INR")
    assert got["kpis"]["recorded_cost"] == 10800
    assert got["kpis"]["list_cost"] == 18000
    assert got["kpis"]["actual_savings"] == 7200
    assert got["kpis"]["effective_discount_pct"] == 40
    assert got["fx"]["complete"]
    assert any("operator pinned" in r["source"] for r in got["fx"]["rates"])


def test_cached_reference_fx_source_date(monkeypatch):
    monkeypatch.setattr(currency, "_cache", {
        "rates": {"EUR": 0.8}, "date": "2026-09-04", "source": "ECB", "at": 1,
    })
    got = fixture_statement([row(BillingCurrency="EUR")], "USD")
    assert got["kpis"]["billed_cost"] == 75
    assert got["fx"]["rates"][0]["as_of"] == "2026-09-04"
    assert "ECB" in got["fx"]["rates"][0]["source"]


def full_month_rows(month, **values):
    start = date.fromisoformat(month + "-01")
    days = billing.calendar.monthrange(start.year, start.month)[1]
    return [row(ChargePeriodStart=(start + timedelta(days=n)).isoformat(), **values) for n in range(days)]


def test_mom_different_month_lengths_and_zero_denominator():
    window = billing.period("2026-03", date(2026, 4, 1))
    got = fixture_statement(full_month_rows("2026-02") + full_month_rows("2026-03"), window=window)
    mom = got["month_over_month"]
    assert mom["comparable"]
    assert (mom["current"], mom["previous"], mom["change"]) == (1860, 1680, 180)
    assert mom["current_daily_average"] == mom["previous_daily_average"] == 60
    got = fixture_statement(full_month_rows("2026-02", InvoiceCost=0) + full_month_rows("2026-03"), window=window)
    assert got["month_over_month"]["change_pct"] is None
    assert got["month_over_month"]["change"] == 1860


def test_mom_missing_month_partial_month_and_mixed_basis():
    got = fixture_statement(full_month_rows("2026-08"))
    assert got["month_over_month"]["previous"] is None
    assert got["month_over_month"]["change"] is None
    got = fixture_statement([*full_month_rows("2026-08"), row(ChargePeriodStart="2026-07-01")])
    assert not got["month_over_month"]["comparable"]
    assert got["coverage"]["previous"]["missing_days"] == 30
    got = fixture_statement(full_month_rows("2026-08", InvoiceCost=None, BillingCostBasis="AmortizedCost")
                            + full_month_rows("2026-07"))
    assert got["month_over_month"]["change"] is None


def test_current_month_to_date_is_not_compared_with_a_complete_prior_month(monkeypatch):
    from app import reporting

    monkeypatch.setattr(reporting, "today", lambda: date(2026, 9, 6))
    with reporting.select("2026-09"):
        window = billing.statement_period()
    got = fixture_statement(
        full_month_rows("2026-08") + full_month_rows("2026-09")[:6], window=window)
    mom = got["month_over_month"]
    assert got["coverage"]["current"]["status"] == "all_days_present"
    assert not mom["comparable"]
    assert mom["change"] is None and mom["change_pct"] is None
    assert mom["current_daily_average"] == mom["previous_daily_average"] == 60
    assert any("month-to-date" in warning for warning in got["warnings"])


def test_nonfinite_prices_are_unknown():
    got = fixture_statement([row(InvoiceCost=float("nan"), ListCost=float("inf"))])
    assert got["kpis"]["billed_cost"] is None
    assert got["kpis"]["list_cost"] is None
    json.dumps(got, allow_nan=False)


def request(user=USER, query=""):
    req = Request({"type": "http", "headers": [], "query_string": query.encode()})
    if user is not None:
        req.state.user = user
    return req


def test_routes_are_registered_before_section_fallback():
    paths = [route.path for route in main.app.routes]
    assert paths.index("/api/dashboard/billing") < paths.index("/api/dashboard/{section_id}")
    assert "/api/dashboard/billing/opportunities" in paths


def test_route_auth_filters_offload_and_independence(monkeypatch):
    async def run():
        monkeypatch.setattr(main, "permitted", AsyncMock(return_value=["sub-a"]))
        worker = AsyncMock(return_value={"statement": True})
        monkeypatch.setattr(main, "offload", worker)
        response = await main.dashboard_billing(
            request(query="service=Storage&rg=shared&evil=ignored"), scope=" SUB-A ",
            currency="usd", month="2026-08")
        assert response.headers["cache-control"] == "no-store"
        assert worker.call_args.args == (billing.build, ["sub-a"], "USD", "2026-08")
        assert worker.call_args.kwargs == {"filters": {"rg": ["shared"], "service": ["Storage"]}}
        for endpoint in (main.dashboard_billing, main.dashboard_billing_opportunities):
            with pytest.raises(HTTPException) as exc:
                await endpoint(request(None))
            assert exc.value.status_code == 401
            with pytest.raises(HTTPException) as exc:
                await endpoint(request(), scope="sub-z")
            assert exc.value.status_code == 403
            with pytest.raises(HTTPException) as exc:
                await endpoint(request(), scope="sub-a,sub-z")
            assert exc.value.status_code == 403
        assert worker.call_count == 1
        with pytest.raises(HTTPException) as exc:
            await main.dashboard_billing(request(), month="bad")
        assert exc.value.status_code == 422
    asyncio.run(run())


@pytest.mark.parametrize("value", [0.014, -0.014])
def test_displayed_breakdowns_reconcile_with_explicit_rounding(value):
    stats = [billing._row_stats({
        "BilledCost": value, "InvoiceCost": value, "BillingCostBasis": "FOCUS",
        "ChargeCategory": "Usage", "BillingCurrency": "USD",
    }) for _ in range(3)]
    daily = [{"day": "2026-08-01", "currency": "USD", "basis": "FOCUS", **billing._merge(stats)}]
    groups = [
        {**item, "bucket": bucket, "currency": "USD", "id": f"item-{index}",
         "name": f"Item {index}", "group_count": 1}
        for bucket in ("subscriptions", "services", "resources")
        for index, item in enumerate(stats)
    ]
    result = billing.assemble(daily, groups, billing.period("2026-08", date(2026, 9, 6)), "USD")
    for rows in result["breakdowns"].values():
        assert sum(row["cost"] for row in rows) == pytest.approx(result["kpis"]["billed_cost"])
        adjustment = next(row for row in rows if row.get("is_rounding"))
        assert adjustment["rows"] == 0
        assert "not an additional charge" in adjustment["note"]


def test_live_cache_singleflight_actor_scope_currency_and_authorization(monkeypatch):
    async def run():
        main._billing_cache.clear()
        main._billing_pending.clear()
        calls, contexts = [], []
        gate = asyncio.Event()

        async def build(scopes, currency, days=30):
            calls.append((scopes, currency, days))
            await gate.wait()
            return {"status": "ready", "opportunities": [], "freshness": {}}

        @asynccontextmanager
        async def as_user(user):
            contexts.append(user)
            yield

        monkeypatch.setitem(sys.modules, "app.billing_opportunities", SimpleNamespace(build=build))
        monkeypatch.setattr(main, "as_user", as_user)
        allowed = AsyncMock(return_value=["sub-a", "sub-b"])
        monkeypatch.setattr(main, "permitted", allowed)
        a = asyncio.create_task(main.dashboard_billing_opportunities(request(), scope="sub-b,sub-a", currency="usd"))
        b = asyncio.create_task(main.dashboard_billing_opportunities(request(), scope=" SUB-A,sub-b ", currency="USD"))
        for _ in range(10):
            await asyncio.sleep(0)
        assert len(calls) == 1
        gate.set()
        responses = await asyncio.gather(a, b)
        assert all(r.headers["cache-control"] == "no-store" for r in responses)
        assert calls == [(["sub-a", "sub-b"], "USD", 30)]
        assert contexts == [USER]
        cached = await main.dashboard_billing_opportunities(request(), scope="sub-a,sub-b", currency="USD")
        assert json.loads(cached.body)["cache"]["cached"]
        assert len(calls) == 1
        for other in (User("bob", email=USER.email), User(USER.name, email="other@invalid"),
                      User(USER.name, email=USER.email, sid="new-session")):
            await main.dashboard_billing_opportunities(request(other), scope="sub-a,sub-b", currency="USD")
        await main.dashboard_billing_opportunities(request(), scope="sub-a", currency="USD")
        await main.dashboard_billing_opportunities(request(), scope="sub-a", currency="EUR")
        assert len(calls) == 6
        allowed.side_effect = HTTPException(401, "expired")
        with pytest.raises(HTTPException):
            await main.dashboard_billing_opportunities(request(), scope="sub-a,sub-b", currency="USD")
        assert len(calls) == 6
        main._billing_cache.clear()
        main._billing_pending.clear()
    asyncio.run(run())


def test_live_failures_and_cancellation_do_not_cache_or_cancel_shared_work(monkeypatch):
    async def run():
        main._billing_cache.clear()
        main._billing_pending.clear()
        build = AsyncMock(side_effect=HTTPException(401, "expired"))
        monkeypatch.setitem(sys.modules, "app.billing_opportunities", SimpleNamespace(build=build))
        with pytest.raises(HTTPException):
            await main._get_billing_opportunities(USER, ["sub-a"], "USD")
        assert not main._billing_cache
        gate = asyncio.Event()

        async def delayed(*args, **kwargs):
            await gate.wait()
            return {"status": "ready"}

        build.side_effect = delayed
        a = asyncio.create_task(main._get_billing_opportunities(USER, ["sub-a"], "USD"))
        for _ in range(5):
            await asyncio.sleep(0)
        a.cancel()
        with pytest.raises(asyncio.CancelledError):
            await a
        assert len(main._billing_pending) == 1
        gate.set()
        result = await main._get_billing_opportunities(USER, ["sub-a"], "USD")
        assert result["status"] == "ready"
        assert build.call_count == 2
        main._billing_cache.clear()
        main._billing_pending.clear()
    asyncio.run(run())


def test_live_cache_bounded_and_expires(monkeypatch):
    async def run():
        main._billing_cache.clear()
        main._billing_pending.clear()
        build = AsyncMock(return_value={"status": "ready"})
        monkeypatch.setitem(sys.modules, "app.billing_opportunities", SimpleNamespace(build=build))
        monkeypatch.setattr(main, "_BILLING_CACHE_LIMIT", 2)
        for scope in ("a", "b", "c"):
            await main._get_billing_opportunities(USER, [scope], "USD")
        assert len(main._billing_cache) == 2
        monkeypatch.setattr(main, "_BILLING_CACHE_SECONDS", 0)
        await main._get_billing_opportunities(USER, ["c"], "USD")
        assert build.call_count == 4
        monkeypatch.setattr(main, "_BILLING_PENDING_LIMIT", 0)
        with pytest.raises(HTTPException) as exc:
            await main._get_billing_opportunities(USER, ["d"], "USD")
        assert exc.value.status_code == 503
        main._billing_cache.clear()
        main._billing_pending.clear()
    asyncio.run(run())


def test_unrestricted_live_scope_is_explicit_before_caching(monkeypatch):
    from app import cost

    async def run():
        monkeypatch.setattr(main, "permitted", AsyncMock(return_value=None))
        monkeypatch.setattr(cost, "list_subscriptions", AsyncMock(return_value={
            "subscriptions": [{"id": "/subscriptions/SUB-B"}, {"id": "sub-a"}],
        }))
        loader = AsyncMock(return_value={"status": "ready"})
        monkeypatch.setattr(main, "_get_billing_opportunities", loader)
        await main.dashboard_billing_opportunities(request(), currency="USD")
        loader.assert_awaited_once_with(USER, ["sub-a", "sub-b"], "USD", refresh=False)
        with pytest.raises(HTTPException) as exc:
            await main.dashboard_billing_opportunities(request(), scope="/resourceGroups/not-a-sub")
        assert exc.value.status_code == 422
        assert loader.call_count == 1
    asyncio.run(run())


def test_explicit_live_refresh_bypasses_fresh_cache_and_joins_singleflight(monkeypatch):
    async def run():
        main._billing_cache.clear()
        main._billing_pending.clear()
        calls = []
        gate = asyncio.Event()
        gate.set()

        async def build(scopes, currency, days=30):
            calls.append((scopes, currency, days))
            await gate.wait()
            return {"status": "ready", "revision": len(calls)}

        monkeypatch.setitem(sys.modules, "app.billing_opportunities", SimpleNamespace(build=build))
        monkeypatch.setattr(main, "permitted", AsyncMock(return_value=["sub-a"]))
        initial = await main.dashboard_billing_opportunities(request(), currency="USD")
        assert json.loads(initial.body)["revision"] == 1
        gate.clear()
        first = asyncio.create_task(main.dashboard_billing_opportunities(request(), currency="USD", refresh=True))
        second = asyncio.create_task(main.dashboard_billing_opportunities(request(), currency="USD", refresh=True))
        for _ in range(10):
            await asyncio.sleep(0)
        assert len(calls) == 2
        assert len(main._billing_pending) == 1
        gate.set()
        refreshed = await asyncio.gather(first, second)
        for response in refreshed:
            body = json.loads(response.body)
            assert body["revision"] == 2
            assert body["cache"]["refresh_requested"] is True
            assert body["cache"]["max_age_seconds"] == 60
        cached = await main.dashboard_billing_opportunities(request(), currency="USD")
        assert json.loads(cached.body)["cache"]["cached"] is True
        assert len(calls) == 2
        main._billing_cache.clear()
        main._billing_pending.clear()
    asyncio.run(run())


@pytest.mark.parametrize("metric", ["ActualCost", "AmortizedCost", None])
def test_discovered_export_preserves_authoritative_dataset(monkeypatch, metric):
    async def run():
        ingest = AsyncMock()
        monkeypatch.setitem(sys.modules, "app.exports", SimpleNamespace(
            ingest_export=ingest, covers_current_period=lambda candidate: True))
        monkeypatch.setitem(sys.modules, "app.warehouse", SimpleNamespace(
            warehouse=SimpleNamespace(state={"status": "ready"})))
        monkeypatch.setattr(main, "_archive_after_ingest", AsyncMock())
        candidate = {"name": "current", "storage_account": "account", "container": "costs",
                     "root_folder": "", "subscription_id": "sub-a", "type": metric}
        result = await main._load_export_pair([candidate])
        assert result["loaded"] == ["current"]
        assert ingest.call_args.kwargs["metric"] == metric
    asyncio.run(run())


def test_configured_sas_does_not_assume_requested_report_metric(monkeypatch):
    async def run():
        ingest = AsyncMock()
        monkeypatch.setitem(sys.modules, "app.exports", SimpleNamespace(ingest_export=ingest))
        monkeypatch.setitem(sys.modules, "app.warehouse", SimpleNamespace(
            warehouse=SimpleNamespace(state={"status": "ready"})))
        monkeypatch.setattr(main, "_archive_after_ingest", AsyncMock())
        monkeypatch.setenv("COST_EXPORT_SAS_URL", "https://example.invalid/export")
        await main._run_ingest(1, metric="ActualCost")
        assert ingest.await_count == 1
        assert "metric" not in ingest.call_args.kwargs
    asyncio.run(run())


@pytest.fixture
def pg(postgres_warehouse):
    return postgres_warehouse


def seed(wh, rows):
    from app.warehouse import COLUMNS, insert_rows
    with wh.connect() as con:
        insert_rows(con, [[r.get(column) for column in COLUMNS] for r in rows])


@pytest.mark.postgres
def test_pg_production_sql_matches_offline_financial_math(pg):
    rows = [
        row(CommitmentDiscountType="Reservation"),
        row(CommitmentDiscountType="SavingsPlan", EffectiveCost=50),
        row(ChargeCategory="UnusedReservation", EffectiveCost=15, ListCost=None, ContractedCost=None),
        row(ChargeCategory="Purchase", BilledCost=900, InvoiceCost=900),
        row(ChargeCategory="Refund", BilledCost=-10, InvoiceCost=-10),
        row(InvoiceCost=None, ListCost=None, BillingCostBasis="AmortizedCost"),
        row(BenefitId="a", PricingModel=""),
        row(ContractedCost=None),
        row(EffectiveCost=None),
        row(ListCost=float("nan"), InvoiceCost=float("inf")),
        row(BillingCostBasis=None, InvoiceCost=0, ListCost=0, ContractedCost=0, EffectiveCost=0),
        row(BillingCostBasis="AmortizedCost", InvoiceCost=0),
    ]
    seed(pg, rows)
    got = billing.build(["sub-a"], "USD", "2026-08", today=date(2026, 9, 6), warehouse=pg)
    expected = fixture_statement(rows)
    assert got["kpis"] == expected["kpis"]
    assert got["pricing"] == expected["pricing"]
    assert got["coverage"] == expected["coverage"]
    for bucket in got["breakdowns"].values():
        assert sum(item["cost"] for item in bucket) == got["kpis"]["recorded_cost"]


@pytest.mark.postgres
def test_pg_scope_filters_resource_identity_and_reconciliation(pg):
    rows = [
        row(), row(SubAccountId="sub-b", SubAccountName="Subscription B", BilledCost=100, InvoiceCost=100),
        row(ResourceId="/different/resource", ResourceName="Same name", ServiceName="Storage", BilledCost=5, InvoiceCost=5),
        row(SubAccountId="sub-b", ResourceId="/different/resource", BilledCost=10, InvoiceCost=10),
        row(ChargePeriodStart="2026-09-01", BilledCost=9999, InvoiceCost=9999),
    ]
    seed(pg, rows)
    got = billing.build(["sub-a", "sub-b"], "USD", "2026-08", today=date(2026, 9, 6), warehouse=pg)
    assert got["kpis"]["billed_cost"] == 175
    resources = got["breakdowns"]["resources"]
    assert len(resources) == 4
    assert len({(r["subscription_id"], r["id"]) for r in resources}) == 4
    for bucket in got["breakdowns"].values():
        assert sum(item["cost"] for item in bucket) == 175
    scoped = billing.build(["sub-a"], "USD", "2026-08", warehouse=pg, filters={"service": ["Storage"]})
    assert scoped["kpis"]["billed_cost"] == 5
    assert len(scoped["breakdowns"]["resources"]) == 1
    absent = billing.build(["nobody"], "USD", "2026-08", warehouse=pg)
    assert absent["coverage"]["current"]["rows"] == 0
    assert absent["kpis"]["recorded_cost"] is None


@pytest.mark.postgres
def test_pg_other_buckets_reconcile_without_truncating_money(pg, monkeypatch):
    monkeypatch.setattr(billing, "BREAKDOWN_LIMIT", 2)
    seed(pg, [row(ResourceId=f"/resource/{n}", BilledCost=n, InvoiceCost=n,
                  ServiceName=f"Service {n}", SubAccountId=f"sub-{n}") for n in range(1, 6)])
    got = billing.build(None, "USD", "2026-08", warehouse=pg)
    assert got["kpis"]["billed_cost"] == 15
    for bucket in got["breakdowns"].values():
        assert len(bucket) == 3
        assert sum(item["cost"] for item in bucket) == 15
        other = next(item for item in bucket if item["is_other"])
        assert other["cost"] == 6
        assert other["group_count"] == 3


@pytest.mark.postgres
def test_pg_currency_conversion_combines_subscription_identity(pg, monkeypatch):
    monkeypatch.setenv("FX_RATES", '{"EUR": 0.8}')
    seed(pg, [row(), row(BillingCurrency="EUR")])
    got = billing.build(["sub-a"], "USD", "2026-08", warehouse=pg)
    assert got["kpis"]["billed_cost"] == 135
    for bucket in got["breakdowns"].values():
        assert len(bucket) == 1
        assert bucket[0]["cost"] == 135
        assert bucket[0]["currency"] == "USD"
