"""Calendar-window regression tests; PostgreSQL cases require the explicit dev test host."""
from __future__ import annotations

import asyncio
import csv
import io
from contextlib import contextmanager
from datetime import date
from types import SimpleNamespace

import pytest

from app import reporting

SUB_A = "11111111-1111-1111-1111-111111111111"
SUB_B = "22222222-2222-2222-2222-222222222222"


@pytest.fixture(autouse=True)
def clock(monkeypatch):
    monkeypatch.setattr(reporting, "today", lambda: date(2026, 9, 6))


@pytest.mark.parametrize("month,start,end,days", [
    ("2024-02", "2024-02-01", "2024-02-29", 29),
    ("2025-02", "2025-02-01", "2025-02-28", 28),
    ("2024-01", "2024-01-01", "2024-01-31", 31),
    ("2026-09", "2026-09-01", "2026-09-06", 6),
])
def test_calendar_bounds_ignore_downstream_days(month, start, end, days):
    with reporting.select(month):
        assert tuple(map(str, reporting.month_bounds())) == (start, end)
        for default in (1, 7, 14, 30, 90, 365):
            assert reporting.period_days(default) == days
            assert end in reporting.view_predicate()
        assert reporting.since_date("2026-08-01") == start
    assert reporting.selected_month() is None


@pytest.mark.parametrize("value", [
    "", "2024-2", "2024-00", "2024-13", "0000-01", "0001-01",
    "2026-10", "9999-12", "2024-02-01", "2024-02' OR 1=1--",
])
def test_invalid_and_future_fail_closed(value):
    with pytest.raises(ValueError), reporting.select(value):
        pytest.fail("Invalid period entered the request context")
    assert reporting.selected_month() is None


def test_metadata_exact_rolling_scope_and_empty():
    calls = []

    class Reader:
        def rows(self, sql, limit=200):
            assert reporting.selected_month() is None
            return ([{"month": "2026-09"}, {"month": "2024-02"}]
                    if "DISTINCT" in sql else [{"hi": "2026-09-04", "lo": "2024-02-01"}])

    class Warehouse:
        @contextmanager
        def reader(self, scope, filters=None):
            calls.append((scope, filters))
            yield Reader()

    result = reporting.metadata(Warehouse(), [SUB_A], 30, {"rg": ["a"]})
    assert (result["from"], result["to"]) == ("2026-08-06", "2026-09-04")
    assert result["days"] == 30 and result["month"] is None
    assert calls == [([SUB_A], {"rg": ["a"]})]
    with reporting.select("2025-03"):
        result = reporting.metadata(Warehouse(), [SUB_A], 1)
        assert (result["from"], result["to"], result["days"]) == (
            "2025-03-01", "2025-03-31", 31)
        assert result["empty"] is True
        assert result["available_months"] == ["2026-09", "2024-02"]
        assert reporting.selected_month() == "2025-03"


def test_context_isolation_and_comparison_history():
    async def run(month):
        with reporting.select(month):
            await asyncio.sleep(0)
            return await asyncio.to_thread(reporting.selected_month)

    async def gather():
        return await asyncio.gather(run("2024-02"), run("2024-01"))

    assert asyncio.run(gather()) == ["2024-02", "2024-01"]
    with reporting.select("2024-02"):
        assert "2024-02-01" in reporting.view_predicate()
        with reporting.comparison_history():
            assert "2024-01-03" in reporting.view_predicate()
        assert "2024-02-01" in reporting.view_predicate()


def test_billing_local_override_and_current_global():
    from app import billing

    with reporting.select("2024-02"):
        inherited = billing.statement_period()
        assert inherited["month"] == "2024-02" and inherited["days"] == 29
        assert billing.statement_period("2024-01")["month"] == "2024-01"
    with reporting.select("2026-09"):
        inherited = billing.statement_period()
        assert inherited["end"] == "2026-09-06" and inherited["partial"]
        with pytest.raises(ValueError):
            billing.statement_period("2026-09", date(2026, 9, 6))


@pytest.fixture(scope="module")
def database(module_postgres_warehouse):
    from app.warehouse import COLUMNS, insert_rows

    wh = module_postgres_warehouse
    records = []
    for day, amount, sub, name in [
        ("2023-12-31", 5, SUB_A, "previous-year"),
        ("2024-01-01", 10, SUB_A, "january-first"),
        ("2024-01-31", 20, SUB_A, "january-last"),
        ("2024-02-01", 1, SUB_A, "february-first"),
        ("2024-02-15", 15, SUB_A, "february-middle"),
        ("2024-02-29", 29, SUB_A, "february-last"),
        ("2024-03-01", 100, SUB_A, "march-first"),
        ("2024-02-05", 700, SUB_B, "other-subscription"),
        ("2026-08-06", 999, SUB_A, "outside-rolling"),
        ("2026-08-07", 70, SUB_A, "rolling-first"),
        ("2026-09-01", 50, SUB_A, "september-first"),
        ("2026-09-05", 500, SUB_A, "latest"),
    ]:
        row = {
            "ChargePeriodStart": day, "BilledCost": amount, "InvoiceCost": amount,
            "EffectiveCost": amount, "ListCost": amount, "ContractedCost": amount,
            "BillingCostBasis": "FOCUS", "BillingCurrency": "USD",
            "SubAccountId": sub, "SubAccountName": sub, "ResourceName": name,
            "ResourceId": f"/subscriptions/{sub}/resourceGroups/{name}/providers/Microsoft.Compute/virtualMachines/{name}",
            "ResourceGroup": name, "ServiceFamily": "Compute", "ServiceName": "Virtual Machines",
            "RegionName": "eastus", "ChargeCategory": "Usage", "PricingModel": "OnDemand",
            "Tags": '{"env":"test"}', "PricingQuantity": 1,
        }
        records.append([row.get(column) for column in COLUMNS])
    with wh.connect() as con:
        insert_rows(con, records)
    return wh


@pytest.fixture
def client(database, monkeypatch):
    from fastapi.testclient import TestClient

    from app import dashboard, main, resources

    monkeypatch.setattr(main, "auth_service", lambda: SimpleNamespace(
        identify=lambda cookie, auth: SimpleNamespace(
            source="local", name="test", email="test@example.invalid", admin=True, sid="")
        if auth == "Bearer test" else None))
    monkeypatch.setattr(main, "_log_access", lambda *args: None)
    monkeypatch.setattr(dashboard, "dashboard", None)
    monkeypatch.setattr(resources, "resources", None)
    return TestClient(main.app, headers={"Authorization": "Bearer test"})


@pytest.mark.postgres
def test_postgres_old_month_all_edges_scope_and_helpers(database):
    from app.commitments import Commitments
    from app.dashboard import Dashboard
    from app.resources import Resources
    from app.tags import cost_by_tag
    from app.uptime import hourly_cost

    with reporting.select("2024-02"):
        assert Dashboard().sections([SUB_A], days=1)["total"] == 45
        assert Dashboard().executive([SUB_A], days=1)["days"] == 29
        section = Dashboard().section("compute", [SUB_A], days=1)
        assert section["kpis"]["total"] == 45
        assert section["days"] == 29
        resources = Resources().inventory([SUB_A], days=1)
        assert resources["total_cost"] == 45
        assert any(r["name"] == "february-first" and r["cost"] == 1 for r in resources["resources"])
        assert Commitments().coverage([SUB_A], days=1)["days"] == 29
        assert cost_by_tag([SUB_A], days=1)["days"] == 29
        assert hourly_cost([SUB_A], days=1)["days"] == 29
        with database.reader([SUB_B]) as reader:
            assert reader.rows('SELECT sum("BilledCost") AS cost FROM costs')[0]["cost"] == 700
    with reporting.select("2024-01"):
        assert Dashboard().sections([SUB_A], days=1)["total"] == 30
    assert Dashboard().sections([SUB_A], days=30)["total"] == 620


@pytest.mark.postgres
def test_authenticated_metadata_validation_scope_and_empty(client, monkeypatch):
    result = client.get(f"/api/reporting-period?scope={SUB_A}&days=30").json()
    assert (result["from"], result["to"]) == ("2026-08-07", "2026-09-05")
    result = client.get(f"/api/reporting-period?scope={SUB_B}&period_month=2024-02").json()
    assert result["available_months"] == ["2024-02"]
    assert (result["from"], result["to"], result["days"]) == ("2024-02-01", "2024-02-29", 29)
    empty = client.get(f"/api/reporting-period?scope={SUB_A}&period_month=2025-03").json()
    assert empty["empty"] and empty["from"] == "2025-03-01"
    no_data = client.get("/api/reporting-period?scope=33333333-3333-3333-3333-333333333333").json()
    assert no_data["empty"] and no_data["from"] is None and no_data["to"] is None
    for endpoint in ("reporting-period", "dashboard/sections", "costs/raw.csv", "report/csv"):
        assert client.get(f"/api/{endpoint}?period_month=2026-10").status_code == 400
    assert client.get("/api/reporting-period", headers={"Authorization": ""}).status_code == 401
    from app import main

    async def allowed(user):
        return [SUB_A]

    monkeypatch.setattr(main, "permitted", allowed)
    assert client.get(f"/api/reporting-period?scope={SUB_B}").status_code == 403


@pytest.mark.postgres
def test_api_filters_downloads_and_reports(client):
    query = f"scope={SUB_A}&period_month=2024-01&days=1"
    sections = client.get(f"/api/dashboard/sections?{query}")
    assert sections.status_code == 200
    assert sections.json()["total"] == 30 and sections.json()["days"] == 31
    choices = client.get(f"/api/filters?{query}").json()["dimensions"]["rg"]
    assert {r["value"] for r in choices} == {"january-first", "january-last"}
    raw = client.get(f"/api/costs/raw.csv?{query}")
    assert raw.status_code == 200
    rows = list(csv.DictReader(io.StringIO(raw.text)))
    assert {r["ChargePeriodStart"] for r in rows} == {"2024-01-01", "2024-01-31"}
    # The CSV's provenance header carries the same facts the JSON export used to assert on:
    # the reporting period is stated in the file itself, not only in the filename.
    report = client.get(f"/api/report/csv?{query}")
    assert report.status_code == 200, report.text
    assert "# Period: 2024-01: 2024-01-01 to 2024-01-31 (inclusive)" in report.text
    assert "2024-01-01_to_2024-01-31" in report.headers["content-disposition"]
    post = client.post("/api/report", json={
        "format": "csv", "scope": [SUB_A], "days": 1, "period_month": "2024-02"})
    assert post.status_code == 200
    assert "# Period: 2024-02: 2024-02-01 to 2024-02-29 (inclusive)" in post.text
    assert client.post("/api/report", json={"period_month": "2026-10"}).status_code == 400


@pytest.mark.postgres
def test_billing_api_global_and_explicit_override(client):
    global_only = client.get(f"/api/dashboard/billing?scope={SUB_A}&period_month=2024-02")
    assert global_only.status_code == 200, global_only.text
    assert global_only.json()["period"]["month"] == "2024-02"
    local = client.get(
        f"/api/dashboard/billing?scope={SUB_A}&period_month=2024-02&month=2024-01")
    assert local.status_code == 200 and local.json()["period"]["month"] == "2024-01"


@pytest.mark.postgres
def test_live_route_retains_current_context(client, monkeypatch):
    from app import waste

    async def live(*args, **kwargs):
        assert reporting.selected_month() is None
        return {"period_days": kwargs.get("days"), "findings": []}

    monkeypatch.setattr(waste, "find_waste", live)
    response = client.get(f"/api/dashboard/waste?scope={SUB_A}&period_month=2024-02&days=14")
    assert response.status_code == 200, response.text
    assert response.headers["x-reporting-window"] == "live"
