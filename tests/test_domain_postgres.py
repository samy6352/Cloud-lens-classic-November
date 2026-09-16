"""Domain SQL contracts on an explicitly configured, fixture-owned PostgreSQL schema."""
from __future__ import annotations

import asyncio
import json
from datetime import UTC, date, datetime, timedelta
from unittest.mock import AsyncMock

import pytest

pytestmark = pytest.mark.postgres

SUB_A = "11111111-1111-1111-1111-111111111111"
SUB_B = "22222222-2222-2222-2222-222222222222"


def rid(name, sub=SUB_A):
    return f"/subscriptions/{sub}/resourceGroups/rg/providers/Microsoft.Compute/virtualMachines/{name}"


def put(wh, records):
    from app.warehouse import COLUMNS, NUMERIC, insert_rows

    rows = []
    for record in records:
        values = {
            "SubAccountId": SUB_A, "SubAccountName": "a", "ResourceGroup": "rg",
            "ResourceId": rid("vm"), "ResourceName": "vm", "RegionName": "eastus",
            "ChargePeriodStart": "2026-09-14", "ChargePeriodEnd": "2026-09-14",
            "BilledCost": 1.0, "BillingCurrency": "USD", "ServiceName": "Virtual Machines",
            "ServiceFamily": "Compute", "MeterName": "D2", "UnitOfMeasure": "hour",
            "PricingQuantity": 1.0,
        }
        values.update(record)
        values.setdefault("CostInUsd", values["BilledCost"] / {"USD": 1, "EUR": 0.8, "INR": 80}.get(
            values["BillingCurrency"], 1))
        rows.append([values.get(column, 0.0 if column in NUMERIC else None) for column in COLUMNS])
    with wh.connect() as connection:
        insert_rows(connection, rows)


def certify(wh, *, missing=(), empty=()):
    with wh.connect() as connection:
        counts = connection.execute(
            'SELECT lower("SubAccountId"), "ChargePeriodStart", count(*) FROM costs GROUP BY 1,2'
        ).fetchall()
        for sub, day, count in [*counts, *((sub, day, 0) for sub, day in empty)]:
            if (sub, str(day)) in missing:
                continue
            connection.execute(
                "INSERT INTO meta (key,value) VALUES (?,?) ON CONFLICT (key) DO UPDATE SET value=EXCLUDED.value",
                [f"focus-day:{sub}:{day}", json.dumps({"fingerprint": "validated-test-manifest", "day_rows": count})],
            )


@pytest.fixture
def fx(monkeypatch):
    from app import currency

    monkeypatch.setattr(currency, "rate_to", lambda code: {"USD": 1.0, "EUR": 0.8, "INR": 80.0}.get(code))


def test_resource_metadata_and_native_currency_sql(postgres_warehouse, fx):
    from app.resources import Resources

    put(postgres_warehouse, [
        *[{"ServiceName": "Azure App Service", "ServiceFamily": "Web", "BilledCost": 2.0}
          for _ in range(5)],
        {"ServiceName": "Defender", "ServiceFamily": "Security", "BilledCost": 8.0},
        {"ServiceName": "ZZ Historical", "ServiceFamily": "Security",
         "BilledCost": 999.0, "ChargePeriodStart": "2026-08-01"},
        {"SubAccountId": SUB_B, "ResourceId": rid("euro", SUB_B),
         "BillingCurrency": "EUR", "BilledCost": 100.0},
    ])
    subject = Resources.__new__(Resources)
    subject.wh = postgres_warehouse
    native = subject.inventory([SUB_A, SUB_B], 30)
    row = next(item for item in native["resources"] if item["subscription_id"] == SUB_A)
    assert row["service"] == "Azure App Service"
    assert row["area_id"] == "compute"
    assert row["service_names"] == ["Azure App Service", "Defender"]
    assert native["total_cost"] is None
    assert native["total_cost_by_currency"] == {"USD": 18, "EUR": 100}
    assert subject.inventory([SUB_A, SUB_B], 30, "USD")["total_cost"] == 143


def test_aks_complete_aggregates_remain_currency_separated(postgres_warehouse, monkeypatch):
    from app import aks

    put(postgres_warehouse, [
        *[{"ResourceGroup": f"MC_rg_cluster{i}_eastus", "ResourceId": rid(f"vm{i}")}
          for i in range(1001)],
        {"SubAccountId": SUB_B, "ResourceGroup": "MC_rg_euro_eastus",
         "ResourceId": rid("euro", SUB_B), "BillingCurrency": "EUR", "BilledCost": 100.0},
    ])
    monkeypatch.setattr(aks, "_clusters", AsyncMock(return_value=[]))
    result = asyncio.run(aks.clusters([SUB_A, SUB_B], 30))
    assert result["total"] is None
    assert result["total_by_currency"] == {"USD": 1001, "EUR": 100}
    assert result["total_cluster_records"] == 1002
    assert result["count"] == 1000 and result["truncated"]
    assert result["trend_by_currency"]["USD"][0]["cost"] == 1001


def test_unknown_commitment_coverage_and_native_groups(postgres_warehouse):
    from app.commitments import Commitments

    put(postgres_warehouse, [
        {"PricingModel": None, "BilledCost": 100.0},
        {"SubAccountId": SUB_B, "BillingCurrency": "EUR", "BilledCost": 50.0, "PricingModel": None},
    ])
    subject = Commitments.__new__(Commitments)
    subject.wh = postgres_warehouse
    result = subject.coverage([SUB_A, SUB_B], 30)
    assert result["total"] is None
    assert result["coverage_pct"] is result["compute_coverage_pct"] is None
    assert result["total_by_currency"] == {"USD": 100, "EUR": 50}
    assert subject.coverage([SUB_A], 30)["coverage_status"] == "unknown"


def test_commitment_inventory_totals_precede_presentation_cap(postgres_warehouse):
    from app.commitments import Commitments

    put(postgres_warehouse, [
        {"BenefitId": f"benefit-{i}", "BenefitName": f"Reservation {i}", "PricingModel": "Reservation",
         "ResourceId": rid(f"vm{i}")}
        for i in range(205)
    ])
    subject = Commitments.__new__(Commitments)
    subject.wh = postgres_warehouse
    result = subject.inventory([SUB_A], 30)
    assert result["count"] == 205
    assert result["returned_count"] == 200 and result["truncated"]
    assert sum(result["totals"].values()) == 205


def test_spot_reference_uses_known_on_demand_in_selected_window(postgres_warehouse):
    from app.commitments import Commitments

    put(postgres_warehouse, [
        {"PricingModel": "Spot", "BilledCost": 30.0, "PricingQuantity": 100},
        {"PricingModel": "OnDemand", "BilledCost": 100.0, "PricingQuantity": 100},
        {"PricingModel": "Reservation", "BilledCost": 90.0, "PricingQuantity": 900},
        {"PricingModel": None, "BilledCost": 1.0, "PricingQuantity": 100},
        {"PricingModel": "OnDemand", "BilledCost": 1000.0,
         "PricingQuantity": 100, "ChargePeriodStart": "2026-09-01"},
        {"PricingModel": "OnDemand", "BilledCost": 1.0,
         "PricingQuantity": 100, "RegionName": "westus"},
    ])
    subject = Commitments.__new__(Commitments)
    subject.wh = postgres_warehouse
    assert subject.spot_savings([SUB_A], 7)["total_saved"] == 70


def test_anomaly_filters_month_warmup_and_display_currency(postgres_warehouse, fx):
    from app import anomalies, reporting

    start = date(2026, 7, 17)
    put(postgres_warehouse, [
        *[{"ChargePeriodStart": (start + timedelta(days=i)).isoformat(),
           "BilledCost": 1.0 if i == 55 else 20.0} for i in range(60)],
        *[{"ChargePeriodStart": (start + timedelta(days=i)).isoformat(),
           "ServiceName": "Storage", "BilledCost": 9000.0 if i == 55 else 20.0}
          for i in range(60)],
    ])
    certify(postgres_warehouse)
    with reporting.select("2026-09"):
        usd = anomalies.analyse([SUB_A], 30, ("ServiceName",), display_currency="USD",
                                filters={"service": ["Virtual Machines"]})
        inr = anomalies.analyse([SUB_A], 30, ("ServiceName",), display_currency="INR",
                                filters={"service": ["Virtual Machines"]})
    assert usd["count"] == inr["count"] == 1
    assert usd["anomalies"][0]["day"] == "2026-09-10"
    assert usd["anomalies"][0]["severity"] == inr["anomalies"][0]["severity"] == "low"
    assert inr["anomalies"][0]["delta"] == pytest.approx(usd["anomalies"][0]["delta"] * 80)
    assert usd["requested_from"] == "2026-09-01"


def test_cost_join_only_reads_requested_resource_ids(postgres_warehouse):
    from app import waste

    today = datetime.now(UTC).date().isoformat()
    put(postgres_warehouse, [
        {"ResourceId": rid("wanted").upper(), "ChargePeriodStart": today, "BilledCost": 7.0},
        {"ResourceId": rid("excluded"), "ChargePeriodStart": today, "BilledCost": 9000.0},
    ])
    result = waste._costs_for([rid("wanted")], 30)
    assert result == {rid("wanted").lower(): {"cost": 7, "currency": "USD"}}


@pytest.mark.parametrize("mode", [
    "no_receipts", "gap", "covered_zero", "partial_subscription", "no_baseline", "unknown_currency",
])
def test_anomalies_require_authoritative_subscription_day_coverage(postgres_warehouse, fx, mode):
    from app import anomalies

    days = [(date(2026, 7, 17) + timedelta(days=i)).isoformat() for i in range(60)]
    missing = days[55]
    subs = [SUB_A, SUB_B] if mode == "partial_subscription" else [SUB_A]
    records = [
        {"SubAccountId": sub, "ChargePeriodStart": day, "BilledCost": 20.0,
         "BillingCurrency": None if mode == "unknown_currency" and day == missing else "USD"}
        for sub in subs for day in days
        if mode in {"no_baseline", "unknown_currency"} or day != missing or (mode == "partial_subscription" and sub == SUB_A)
    ]
    put(postgres_warehouse, records)
    if mode != "no_receipts":
        certify(postgres_warehouse,
                missing={(SUB_A, day) for day in days[:53]} if mode == "no_baseline" else (),
                empty=[(SUB_A, missing)] if mode == "covered_zero" else ())
    result = anomalies.analyse(subs, 7, ("ServiceName",))
    if mode == "covered_zero":
        assert result["count"] == 1
        assert result["anomalies"][0]["day"] == missing
        assert result["anomalies"][0]["cost"] == 0
    else:
        assert result["count"] == 0 and result["partial"]
        assert result["unavailable"] == (mode in {"no_receipts", "no_baseline"})
        assert result["coverage_reason"]
    if mode in {"gap", "partial_subscription", "unknown_currency"}:
        assert missing in result["coverage"]["unverified_days"]
        assert missing not in {row["day"] for row in result["daily"]}


def test_cost_join_retains_same_resource_multiple_currencies(postgres_warehouse):
    from app import waste

    today = datetime.now(UTC).date().isoformat()
    put(postgres_warehouse, [
        {"ChargePeriodStart": today, "BilledCost": 100.0},
        {"ChargePeriodStart": today, "BillingCurrency": "INR", "BilledCost": 8000.0},
    ])
    result = waste._costs_for([rid("vm")], 30, allow_mixed=True)
    assert result[rid("vm").lower()]["cost_by_currency"] == {"USD": 100, "INR": 8000}
    assert result[rid("vm").lower()]["cost"] is None
    with pytest.raises(waste.CostError, match="currency-aware"):
        waste._costs_for([rid("vm")], 30)


def test_commitment_history_preserves_unknown_currency(postgres_warehouse):
    from app.commitments import Commitments

    put(postgres_warehouse, [
        {"BillingCurrency": None, "BilledCost": 999.0, "ChargePeriodStart": "2026-07-01",
         "PricingModel": "OnDemand"},
        {"BillingCurrency": "USD", "BilledCost": 100.0, "PricingModel": "OnDemand"},
    ])
    subject = Commitments.__new__(Commitments)
    subject.wh = postgres_warehouse
    result = subject.coverage([SUB_A], 30)
    assert result["total"] == 100
    assert result["trend_by_currency"]["USD"][0]["total"] == 100
    assert result["trend_by_currency"]["unknown"][0]["total"] is None
    assert result["history_partial"]
