"""Hermetic domain integrity regressions: synthetic inputs, no Azure or persistent database."""
# cloudlens-test: requires-offline
from __future__ import annotations

import asyncio
import io
import json
import re
import sqlite3
import sys
import threading
import time
from contextlib import contextmanager
from datetime import date, timedelta
from types import ModuleType, SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest

import app
from app import (
    aks,
    anomalies,
    archive,
    commitments,
    cost,
    currency,
    insights,
    rates,
    savings,
    shutdown,
    uptime,
    waste,
)

SUB_A = "11111111-1111-1111-1111-111111111111"
SUB_B = "22222222-2222-2222-2222-222222222222"


def rid(name, sub=SUB_A):
    return f"/subscriptions/{sub}/resourceGroups/rg/providers/Microsoft.Compute/virtualMachines/{name}"


def install_warehouse(monkeypatch, value):
    # Doubles that stand in for the warehouse must answer `relations`, which is how a
    # caller holding a raw connection asks which relation to read. These fixtures hold
    # only `costs`, so that is the honest answer for them.
    #
    # Guarded rather than assumed: some tests here install a bare `object()` because
    # the warehouse is never reached at all, and a bare object has no `__dict__` to
    # attach to. Those cases need nothing, so failing to attach is the right outcome.
    if not hasattr(value, "relations") and hasattr(value, "__dict__"):
        value.relations = lambda *args, **kwargs: ("costs", "costs", "costs")
    module = ModuleType("app.warehouse")
    module.warehouse = value
    monkeypatch.setitem(sys.modules, "app.warehouse", module)
    monkeypatch.setattr(app, "warehouse", module, raising=False)


@pytest.fixture
def fx(monkeypatch):
    monkeypatch.setattr(currency, "rate_to", lambda code: {"USD": 1.0, "INR": 80.0, "EUR": 0.8}.get(code))


@pytest.mark.parametrize("revalidation", [False, True])
@pytest.mark.parametrize("invalidation", ["forget", "clear"])
def test_invalidated_old_fetch_cannot_publish(monkeypatch, revalidation, invalidation):
    async def run():
        for name, value in {
            "_cache": {}, "_cache_tokens": {}, "_inflight": {},
            "_refreshing": set(), "_refresh_tasks": set(),
            "_CONCURRENCY": asyncio.Semaphore(4), "_CACHE_TTL": 300, "_CACHE_STALE": 900,
        }.items():
            monkeypatch.setattr(cost, name, value)
        entered, release = asyncio.Event(), asyncio.Event()
        responses = ([("instant", 50)] if revalidation else []) + [("wait", 100), ("instant", 200)]

        class Client:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *args):
                pass

            async def request(self, *args, **kwargs):
                mode, amount = responses.pop(0)
                if mode == "wait":
                    entered.set()
                    await release.wait()
                return httpx.Response(200, json={"amount": amount})

        monkeypatch.setattr(cost.httpx, "AsyncClient", lambda **kwargs: Client())
        api = cost.Azure()
        monkeypatch.setattr(api, "_bearer", AsyncMock(return_value="offline-token"))
        url = f"{cost.ARM}/subscriptions/{SUB_A}/budgets"
        if revalidation:
            await api.request("GET", url)
            key = next(iter(cost._cache))
            _, payload, cached_url = cost._cache[key]
            cost._cache[key] = (time.time() - 1, payload, cached_url)
        old = asyncio.create_task(api.request("GET", url))
        await entered.wait()
        if invalidation == "clear":
            cost.clear_cache()
        else:
            cost.forget("/budgets")
        assert (await api.request("GET", url))["amount"] == 200
        release.set()
        await old
        await asyncio.gather(*list(cost._refresh_tasks))
        assert (await api.request("GET", url))["amount"] == 200
        assert not responses

    asyncio.run(run())


def test_arg_partitions_by_tenant_token(monkeypatch):
    calls = []

    async def request(method, url, **kwargs):
        subs = kwargs["json_body"]["subscriptions"]
        calls.append((cost.caller_token(), subs))
        return {"data": [{"subscriptionId": sub} for sub in subs], "totalRecords": len(subs)}

    monkeypatch.setattr(waste, "azure", SimpleNamespace(request=request))

    async def run():
        reset = cost.act_as(cost.Caller("home", {SUB_A: "home", SUB_B: "guest"}), "offline")
        try:
            rows = await waste._arg("Resources | project subscriptionId", [SUB_A, SUB_B])
            assert len(rows) == 2
            assert cost.caller_token() == "home"
        finally:
            cost.stop_acting(reset)

    asyncio.run(run())
    assert calls == [("home", [SUB_A]), ("guest", [SUB_B])]


def test_live_trend_keeps_currency_series(monkeypatch):
    monkeypatch.setattr(cost, "_resolve", AsyncMock(return_value=[
        {"id": SUB_A, "name": "a"}, {"id": SUB_B, "name": "b"}]))
    monkeypatch.setattr(cost, "_query", AsyncMock(side_effect=["USD", "EUR"]))
    monkeypatch.setattr(cost, "_rows", lambda code: (
        [{"BillingMonth": "2026-09-01", "Cost": 100, "Currency": code}], "Cost", code))
    result = asyncio.run(cost.cost_trend([SUB_A, SUB_B]))
    assert result["currency"] is None
    assert result["mixed_currency"]
    assert result["latest_vs_previous_pct"] is None
    assert {c: rows[0]["cost"] for c, rows in result["months_by_currency"].items()} == {"USD": 100, "EUR": 100}


def test_live_trend_does_not_invent_unknown_currency_totals(monkeypatch):
    monkeypatch.setattr(cost, "_resolve", AsyncMock(return_value=[
        {"id": SUB_A, "name": "a"}, {"id": SUB_B, "name": "b"}]))
    monkeypatch.setattr(cost, "_query", AsyncMock(return_value={}))
    monkeypatch.setattr(cost, "_rows", lambda value: (
        [{"BillingMonth": "2026-09-01", "Cost": 100, "Currency": None}], "Cost", None))
    result = asyncio.run(cost.cost_trend([SUB_A, SUB_B]))
    assert result["currency"] is None and result["incomplete"]
    assert result["months_by_currency"] == {}
    assert len(result["unclassified"]) == 2


def test_advisor_preserves_scope_identity(monkeypatch):
    async def request(method, url, **kwargs):
        sub = SUB_A if SUB_A in url else SUB_B
        return {"value": [{"properties": {
            "resourceMetadata": {"resourceId": rid("same-name", sub)},
            "shortDescription": {"problem": "Right-size an underutilized VM"},
            "extendedProperties": {"annualSavingsAmount": 120, "savingsCurrency": "USD"},
        }}]}

    monkeypatch.setattr(waste, "azure", SimpleNamespace(request=request))
    result = asyncio.run(waste.advisor_recommendations(subscription_ids=[SUB_A, SUB_B]))
    assert result["distinct_count"] == 2
    assert result["estimated_annual_savings"] == 240
    assert {r["subscriptionId"] for r in result["recommendations"]} == {SUB_A, SUB_B}
    assembled = savings.assemble({"advisor": result}, {}, 30, "USD")
    assert assembled["count"] == 2


def test_advisor_failure_is_not_empty_success(monkeypatch):
    monkeypatch.setattr(waste, "azure", SimpleNamespace(request=AsyncMock(side_effect=cost.CostError("denied"))))
    result = asyncio.run(waste.advisor_recommendations(subscription_ids=[SUB_A]))
    assert result["partial"] and result["error"]
    assert result["errors"][0]["subscription_id"] == SUB_A
    assert result["note"] is None
    assembled = savings.assemble({"advisor": result}, {}, 30, "USD")
    assert assembled["partial"]
    assert "advisor" in assembled["unavailable"]


def test_advisor_follows_pages_without_detail_cap(monkeypatch):
    page = {"value": [{"properties": {"resourceMetadata": {"resourceId": rid("one")},
                                    "extendedProperties": {"annualSavingsAmount": 10, "savingsCurrency": "USD"}}}],
            "nextLink": f"{cost.ARM}/subscriptions/{SUB_A}/providers/Microsoft.Advisor/recommendations?$skip=1"}
    second = {"value": [{"properties": {"resourceMetadata": {"resourceId": rid("two")},
                                      "extendedProperties": {"annualSavingsAmount": 20, "savingsCurrency": "USD"}}}]}
    request = AsyncMock(side_effect=[page, second])
    monkeypatch.setattr(waste, "azure", SimpleNamespace(request=request))
    result = asyncio.run(waste.advisor_recommendations(subscription_ids=[SUB_A]))
    assert request.await_count == result["count"] == 2


def test_full_waste_membership_deduplicates_beyond_display_cap():
    items = [{"id": rid(f"vm{i}"), "name": f"vm{i}", "resource_group": "rg",
              "cost": 100, "currency": "USD"} for i in range(100)]
    data = {
        "waste": {"currency": "USD", "findings": [{
            "category": "deallocated_vms", "count": 100, "cost": 10000,
            "cost_by_currency": {"USD": 10000}, "items": items[:60],
            "resource_ids": [i["id"] for i in items], "more": 40,
        }]},
        "rightsizing": {"currency": "USD", "vms": [
            {**item, "state": "deallocated", "subscription_id": SUB_A} for item in items]},
    }
    result = savings.assemble(data, {}, 30, "USD")
    assert result["count"] == 1
    assert result["total"] == 10000


def test_same_name_different_subscriptions_do_not_merge():
    data = {"rightsizing": {"currency": "USD", "vms": [
        {"id": rid("vm", sub), "name": "vm", "resource_group": "rg",
         "subscription_id": sub, "state": "deallocated", "cost": 100, "currency": "USD"}
        for sub in (SUB_A, SUB_B)]}}
    result = savings.assemble(data, {}, 30, "USD")
    assert result["count"] == 2
    assert result["total"] == 200


def test_distinct_reservation_purchases_are_additive():
    rows = [{"problem": "Purchase reserved instances", "resource": SUB_A, "currency": "USD",
             "sku": sku, "region": region, "term": "P3Y", "quantity": 1,
             "lookback_days": 30, "annual_savings": amount}
            for sku, region, amount in (("D2 v3", "eastus", 400), ("F2 v2", "westus", 600))]
    result = savings.assemble({"advisor": {"currency": "USD", "recommendations": rows}}, {}, 30, "USD")
    assert result["commitment_count"] == 2
    assert result["commitment_annual"] == 1000


def advisor_rows():
    return [{"problem": "Purchase reserved instances", "term": "P1Y", "lookback": "30",
             "annualSavings": 120, "savings": 10, "currency": "USD"}]


def test_comparison_converts_baseline_and_estimates(fx):
    result = rates._comparison(advisor_rows(), [SUB_A], 80000, 30, "INR", "INR")
    assert result["currency"] == "INR"
    assert result["best"]["annual_saving"] == 9600
    assert result["best"]["annual_cost"] == 70400
    assert result["best"]["saving_pct"] == 12


def test_comparison_requires_baseline_currency(fx):
    result = rates._comparison(advisor_rows(), [SUB_A], 80000, 30, None, "INR")
    assert result["baseline"]["annual"] is None
    assert result["baseline"]["unavailable_reason"]
    assert result["best"]["annual_cost"] is None
    assert result["best"]["annual_saving"] == 9600


def test_comparison_preserves_native_currency_groups(fx):
    rows = [*advisor_rows(), {**advisor_rows()[0], "currency": "EUR"}]
    result = rates._comparison(rows, [SUB_A], None, 30, None, None)
    assert result["currency"] is None and result["best"] is None
    assert set(result["by_currency"]) == {"USD", "EUR"}


def test_unpriced_recommendation_is_not_reported_as_absent(fx):
    result = rates._comparison(
        [{**advisor_rows()[0], "currency": None}], [SUB_A], 80000, 30, "INR", "INR")
    assert result["partial"] and result["best"] is None
    assert "reservation" not in result["not_recommended"]
    reservation = next(model for model in result["models"] if model["kind"] == "reservation")
    assert "currency" in reservation["reason"]


def test_shutdown_converts_rows_and_offloads_cost_scan(monkeypatch, fx):
    vms = [{"id": rid("usd"), "name": "usd", "resourceGroup": "rg", "subscriptionId": SUB_A, "tags": {}},
           {"id": rid("inr"), "name": "inr", "resourceGroup": "rg", "subscriptionId": SUB_A, "tags": {}}]
    native = {vms[0]["id"].lower(): {"cost": 100, "currency": "USD"},
              vms[1]["id"].lower(): {"cost": 8000, "currency": "INR"}}
    threads = []

    def read_cost(*args, **kwargs):
        threads.append(threading.current_thread())
        return native

    monkeypatch.setattr(waste, "_arg", AsyncMock(return_value=vms))
    monkeypatch.setattr(waste, "_costs_for", read_cost)
    monkeypatch.setattr(shutdown, "_hourly_cpu", AsyncMock(return_value={}))
    monkeypatch.setattr(shutdown, "profile", lambda _: {"enough_data": True, "schedulable": True})
    result = asyncio.run(shutdown.find_schedulable([SUB_A], currency="USD"))
    assert result["currency"] == "USD"
    assert result["total_saving"] == 2 * shutdown.saving(100)["saving"]
    assert {row["currency"] for row in result["candidates"]} == {"USD"}
    assert all(thread is not threading.main_thread() for thread in threads)
    for row in native.values():
        row["currency"] = None
    unpriced = asyncio.run(shutdown.find_schedulable([SUB_A], currency="USD"))
    assert unpriced["partial"]
    assert unpriced["total_saving"] is unpriced["total_current"] is None


def test_cost_lookup_pushes_ids_into_sql(monkeypatch):
    seen = []

    class Reader:
        # The relation per-resource money is read from. Present because `_costs_for`
        # names it; the double answers from a fixed list regardless.
        named = "costs"

        def rows(self, sql, limit=None):
            seen.append(sql)
            assert limit is None
            return []

    @contextmanager
    def reader():
        yield Reader()

    install_warehouse(monkeypatch, SimpleNamespace(reader=reader))
    assert waste._costs_for([rid("wanted")], 30) == {}
    assert " IN (" in seen[0] and rid("wanted").lower() in seen[0]


def test_unknown_commitment_coverage_is_unavailable():
    class Reader:
        def rows(self, sql, limit=None):
            if 'max("ChargePeriodStart")' in sql:
                return [{"hi": "2026-09-14"}]
            if '"BenefitName" AS name' in sql:
                return []
            if "strftime" in sql:
                return [{"month": "2026-09", "model": "", "currency": "USD", "cost": 1458.02}]
            return [{"model": "", "currency": "USD", "cost": 174.93 if '"ServiceFamily"' in sql else 1458.02}]

    @contextmanager
    def reader(*args, **kwargs):
        yield Reader()

    subject = commitments.Commitments.__new__(commitments.Commitments)
    subject.wh = SimpleNamespace(reader=reader)
    result = subject.coverage([SUB_A], 30, "USD")
    assert result["total"] == 1458.02
    assert result["coverage_pct"] is result["compute_coverage_pct"] is None
    assert result["coverage_status"] == "unknown"


def test_spot_uses_only_comparable_on_demand_and_selected_window():
    queries = []
    records = [{"month": "2026-09", "meter": "D2", "currency": "USD", "region": "eastus", "unit": "hour",
                "model": model, "cost": amount, "qty": qty}
               for model, amount, qty in (("OnDemand", 100, 100), ("Reservation", 90, 900), ("Spot", 30, 100))]

    class Reader:
        def rows(self, sql, limit=None):
            queries.append(sql)
            return [{"hi": "2026-09-14"}] if 'max("ChargePeriodStart")' in sql else records

    @contextmanager
    def reader(*args, **kwargs):
        yield Reader()

    subject = commitments.Commitments.__new__(commitments.Commitments)
    subject.wh = SimpleNamespace(reader=reader)
    result = subject.spot_savings([SUB_A], 7, "USD")
    assert result["total_saved"] == 70
    assert 'INTERVAL 7 DAY' in queries[-1]
    records[0]["region"] = "westus"
    assert subject.spot_savings([SUB_A], 7, "USD")["total_saved"] is None


def test_insights_returns_every_waste_category_and_empty_list():
    findings = [{"title": title, "cost": amount, "items": [{"name": title}]}
                for title, amount in (("Disks", 100), ("IPs", 50))]
    result = insights._from_waste({"findings": findings}, "USD")
    assert len(result) == 2 and sum(row.impact for row in result) == 150
    assert insights._from_waste({"findings": []}, "USD") == []


def test_app_service_plan_is_not_schedulable():
    row = {"id": f"/subscriptions/{SUB_A}/resourceGroups/rg/providers/Microsoft.Web/serverfarms/plan",
           "name": "plan", "service": "Azure App Service", "meter": "B1", "cost": 26.16,
           "hours": 720, "peak_day_hours": 24, "first_day": "2026-08-16", "last_day": "2026-09-14"}
    assert not uptime._shape(row)["schedulable"]
    assert uptime._shape({**row, "id": rid("vm"), "service": "Virtual Machines"})["schedulable"]
    assert not uptime._shape({**row, "id": rid("vm"), "service": "Virtual Machines", "meter": "Windows license"})["schedulable"]


def test_anomaly_severity_is_currency_invariant():
    values = [20.0] * 30
    values[20] = 1.0
    rows = [("Compute", (date(2026, 8, 16) + timedelta(days=i)).isoformat(), value)
            for i, value in enumerate(values)]
    usd = anomalies.detect(rows, "ServiceName")
    inr = anomalies.detect([(key, day, value * 80) for key, day, value in rows], "ServiceName",
                           min_delta=1200, min_baseline=80, money_scale=80)
    assert usd[0]["severity"] == inr[0]["severity"] == "low"


class SqliteReader:
    def __init__(self, connection):
        self.connection = connection

    def execute(self, sql, parameters=()):
        return self.connection.execute(re.sub(r"DATE ('[^']*')", r"\1", sql), parameters)

    def close(self):
        pass


def test_anomaly_rows_have_thirty_inclusive_days():
    con = sqlite3.connect(":memory:")
    try:
        con.execute("CREATE TABLE costs (ChargePeriodStart TEXT, ServiceName TEXT, BilledCost REAL)")
        con.executemany("INSERT INTO costs VALUES (?,?,?)", [
            ((date(2026, 8, 1) + timedelta(days=i)).isoformat(), "Compute", 10) for i in range(45)])
        rows = anomalies._rows(SqliteReader(con), "ServiceName", 30)
        assert len(rows) == 30
        assert rows[0][1] == "2026-08-16"
        assert rows[-1][1] == "2026-09-14"
    finally:
        con.close()


@pytest.mark.parametrize("codes", [("USD",), ("USD", "EUR", "ZZZ")])
def test_anomaly_analysis_forwards_filters_and_loads_warmup(monkeypatch, fx, codes):
    con = sqlite3.connect(":memory:")
    forwarded = []
    try:
        con.execute("CREATE TABLE costs (ChargePeriodStart TEXT, ServiceName TEXT, BilledCost REAL, BillingCurrency TEXT)")
        start = date(2026, 7, 17)
        con.executemany("INSERT INTO costs VALUES (?,?,?,?)", [
            ((start + timedelta(days=i)).isoformat(), "Compute",
             (1 if i == 55 else 20) * (currency.rate_to(code) or 1), code)
            for code in codes for i in range(60)])
        con.execute("CREATE TABLE meta (key TEXT, value TEXT)")
        con.executemany("INSERT INTO meta VALUES (?,?)", [
            (f"focus-day:{SUB_A}:{(start + timedelta(days=i)).isoformat()}",
             json.dumps({"fingerprint": "verified-offline", "day_rows": len(codes)}))
            for i in range(60)])

        def connect(**kwargs):
            forwarded.append(kwargs)
            return SqliteReader(con)

        install_warehouse(monkeypatch, SimpleNamespace(connect=connect))
        result = anomalies.analyse([SUB_A], 7, ("ServiceName",), filters={"service": ["Compute"]})
        assert forwarded[0]["filters"] == {"service": ["Compute"]}
        assert result["requested_from"] == "2026-09-08"
        assert result["requested_to"] == "2026-09-14"
        assert result["count"] == len(set(codes) - {"ZZZ"})
        assert result["anomalies"][0]["day"] == "2026-09-10"
        if len(codes) > 1:
            assert result["currency"] is None and result["impact"] is None
            assert result["partial"] and not result["unavailable"]
            assert set(result["unavailable_currencies"]) == {"ZZZ"}
            assert {a["currency"] for a in result["anomalies"]} == {"USD", "EUR"}
            assert {a["severity"] for a in result["anomalies"]} == {"low"}
    finally:
        con.close()


def test_aks_preserves_native_totals_and_detail_limit(monkeypatch):
    rows = [{"rg": f"MC_rg_cluster{i}_eastus", "sub": SUB_A, "service": "Virtual Machines",
             "region": "eastus", "currency": "USD", "cost": 1, "resources": 1} for i in range(1001)]
    rows.append({**rows[0], "rg": "MC_rg_eur_eastus", "sub": SUB_B, "currency": "EUR", "cost": 100})
    daily = [{"day": "2026-09-14", "currency": "USD", "cost": 1001},
             {"day": "2026-09-14", "currency": "EUR", "cost": 100}]
    install_warehouse(monkeypatch, object())
    monkeypatch.setattr(aks, "_clusters", AsyncMock(return_value=[]))
    monkeypatch.setattr(aks, "_cost_rows", lambda *args: (rows, daily, "2026-09-14"))
    result = asyncio.run(aks.clusters([SUB_A, SUB_B]))
    assert result["total"] is None and result["currency"] is None
    assert result["total_by_currency"] == {"USD": 1001, "EUR": 100}
    assert result["total_cluster_records"] == 1002
    assert result["count"] == 1000 and result["truncated"]
    assert result["trend_by_currency"]["USD"][0]["cost"] == 1001


@pytest.mark.parametrize("unknown", [False, True])
def test_aks_rounds_full_totals_and_does_not_assign_missing_currency(monkeypatch, unknown):
    rows = [{"rg": f"MC_rg_cluster{i}_eastus", "sub": SUB_A, "service": "Virtual Machines",
             "region": "eastus", "currency": None if unknown and i else "USD",
             "cost": 0.004, "resources": 1} for i in range(2)]
    daily = [{"day": "2026-09-14", "currency": row["currency"], "cost": row["cost"]} for row in rows]
    install_warehouse(monkeypatch, object())
    monkeypatch.setattr(aks, "_clusters", AsyncMock(return_value=[]))
    monkeypatch.setattr(aks, "_cost_rows", lambda *args: (rows, daily, "2026-09-14"))
    result = asyncio.run(aks.clusters([SUB_A]))
    if unknown:
        assert result["total"] is None and result["currency"] is None
        assert result["monetary_status"] == "unavailable"
    else:
        assert result["total"] == result["trend"][0]["cost"] == 0.01
        assert sum(result["by_category"].values()) == 0.01


@pytest.mark.parametrize("disjoint", [False, True])
def test_archive_separates_new_coverage_from_restatement(monkeypatch, disjoint):
    import pyarrow as pa
    import pyarrow.parquet as pq

    def snapshot(dates, amounts):
        table = pa.table({"SubAccountId": [SUB_A] * len(dates), "BillingCurrency": ["USD"] * len(dates),
                          "ServiceName": ["Compute"] * len(dates),
                          "ChargePeriodStart": [date.fromisoformat(d) for d in dates], "BilledCost": amounts})
        sink = io.BytesIO()
        pq.write_table(table, sink)
        return sink.getvalue()

    before = snapshot(["2026-09-01"], [100.0])
    after = snapshot(["2026-09-02"], [50.0]) if disjoint else snapshot(["2026-09-01", "2026-09-02"], [100.0, 50.0])

    class Writer:
        async def get(self, name):
            return before if name.endswith("2026-09-03.parquet") else after

        async def close(self):
            pass

    monkeypatch.setattr(archive, "enabled", lambda: True)
    monkeypatch.setattr(archive, "BlobWriter", Writer)
    result = asyncio.run(archive.compare("ActualCost", "2026-09-03", "2026-09-04", scope=[SUB_A]))
    assert not result["restated"]
    assert result["total_delta"] is None if disjoint else result["total_delta"] == 0
    assert result["coverage_added"]["cost"] == 50
    assert result["coverage_changed"]
    assert result["full_snapshot_totals"]["later"]["cost"] == (50 if disjoint else 150)


@pytest.mark.parametrize("scoped", [False, True])
def test_archive_restatement_uses_common_subscription_days(monkeypatch, scoped):
    import pyarrow as pa
    import pyarrow.parquet as pq

    def snapshot(records):
        table = pa.table({
            "SubAccountId": [row[0] for row in records],
            "ChargePeriodStart": [date.fromisoformat(row[1]) for row in records],
            "BilledCost": [row[2] for row in records],
            "BillingCurrency": ["USD"] * len(records),
            "ServiceName": ["Compute"] * len(records),
        })
        sink = io.BytesIO()
        pq.write_table(table, sink)
        return sink.getvalue()

    before = snapshot([(SUB_A, "2026-09-01", 100.0), (SUB_B, "2026-09-01", 200.0)])
    after = snapshot([(SUB_A, "2026-09-01", 120.0), (SUB_A, "2026-09-02", 50.0),
                      (SUB_B, "2026-09-02", 999.0)])

    class Writer:
        async def get(self, name):
            return before if name.endswith("2026-09-03.parquet") else after

        async def close(self):
            pass

    monkeypatch.setattr(archive, "enabled", lambda: True)
    monkeypatch.setattr(archive, "BlobWriter", Writer)
    result = asyncio.run(archive.compare(
        "ActualCost", "2026-09-03", "2026-09-04", scope=[SUB_A] if scoped else None))
    assert result["total_delta"] == 20
    assert result["restated"] and result["coverage_basis"] == "subscription_day"
    assert result["comparison_period"] == {"from": "2026-09-01", "to": "2026-09-01", "days": 1}
    assert result["coverage_added"]["cost"] == (50 if scoped else 1049)
    assert result["coverage_removed"]["cost"] == (0 if scoped else 200)


def test_empty_shutdown_scan_has_zero_not_unavailable(monkeypatch):
    monkeypatch.setattr(waste, "_arg", AsyncMock(return_value=[]))
    monkeypatch.setattr(waste, "_costs_for", lambda *args, **kwargs: {})
    result = asyncio.run(shutdown.find_schedulable([SUB_A], 30))
    assert result["total_saving"] == result["total_current"] == 0
    assert not result["partial"]
