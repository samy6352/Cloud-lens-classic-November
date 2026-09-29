"""Currency provenance, estimate periods, and authoritative anomaly coverage regressions."""
# cloudlens-test: requires-offline
from __future__ import annotations

import asyncio
from contextlib import contextmanager
from datetime import date, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app import anomalies, commitments, currency, savings, shutdown, waste
from tests.test_domain_regressions import SUB_A, install_warehouse, rid


@pytest.fixture
def fx(monkeypatch):
    monkeypatch.setattr(currency, "rate_to", lambda code: {"USD": 1.0, "INR": 80.0}.get(code))


@pytest.mark.parametrize("explicit_null", [False, True])
@pytest.mark.parametrize("target", [None, "USD", "INR"])
def test_advisor_amount_cannot_inherit_another_recommendations_currency(fx, explicit_null, target):
    unknown = {"resource_id": rid("unknown"), "problem": "Resize", "annual_savings": 36500}
    if explicit_null:
        unknown["currency"] = None
    known = {"resource_id": rid("known"), "problem": "Resize", "annual_savings": 365,
             "currency": "USD", "impact": "High"}
    result = savings.assemble({
        "waste": {"currency": "USD", "findings": []},
        "advisor": {"currency": "USD", "recommendations": [known, unknown]},
    }, {}, 30, target)
    assert result["count"] == 1
    assert result["total"] == (2400 if target == "INR" else 30)
    assert result["partial"] and "advisor_currency" in result["unavailable"]
    assert result["unconverted"][0]["finding"] == unknown


def test_unknown_source_cannot_borrow_currency_from_another_source(fx):
    result = savings.assemble({
        "waste": {"currency": "USD", "findings": []},
        "rightsizing": {"vms": [{"id": rid("unknown"), "state": "deallocated", "cost": 100}]},
    }, {}, 7, "USD")
    assert result["total"] == 0 and result["partial"]
    assert "rightsizing_currency" in result["unavailable"]


@pytest.mark.parametrize("days", [7, 30, 90])
def test_shutdown_monthly_estimate_is_scaled_to_combined_window(fx, days):
    result = savings.assemble({"shutdown": {
        "estimate_period_days": 30,
        "candidates": [{"id": rid("vm"), "name": "vm", "currency": "USD", "saving": 300}],
    }}, {}, days, "USD")
    assert result["projected_total"] == days * 10
    assert result["opportunities"][0]["sources"][0]["window"] == days * 10
    assert f"projected over {days} days" in result["opportunities"][0]["sources"][0]["claim"]


@pytest.fixture
def mixed_costs(monkeypatch):
    class Reader:
        # `named` is the relation per-resource money is read from -- the presence
        # table in production, `costs` when a converted currency puts that money
        # out of reach. This double answers from a fixed list either way, so the
        # name only has to exist for the SQL to be well formed.
        named = "costs"

        def rows(self, sql, limit=None):
            return [{"rid": rid("vm").lower(), "cost": 100, "currency": "USD"},
                    {"rid": rid("vm").lower(), "cost": 8000, "currency": "INR"}]

    @contextmanager
    def reader():
        yield Reader()

    install_warehouse(monkeypatch, SimpleNamespace(reader=reader))
    return waste._costs_for([rid("vm")], 30, allow_mixed=True)


def test_resource_currency_buckets_are_not_ranked_against_one_another(mixed_costs):
    value = mixed_costs[rid("vm").lower()]
    assert value["cost"] is value["currency"] is None
    assert value["cost_by_currency"] == {"USD": 100, "INR": 8000}
    with pytest.raises(waste.CostError, match="currency-aware"):
        waste._costs_for([rid("vm")], 30)


def test_mixed_resource_waste_converts_every_bucket(monkeypatch, mixed_costs, fx):
    monkeypatch.setattr(waste, "_arg", AsyncMock(return_value=[{"id": rid("vm"), "name": "vm"}]))
    result = asyncio.run(waste.find_waste(
        categories=["deallocated_vms"], subscription_ids=[SUB_A], days=30))
    assert result["total_cost"] is None
    assert result["total_by_currency"] == {"USD": 100, "INR": 8000}
    assert result["total_items"] == 1
    merged = savings.assemble({"waste": result}, {}, 30, "USD")
    assert not merged["partial"]

    # A deallocated VM's retained disks are `cost_at_risk`, not recoverable savings --
    # deleting the VM is not what stops that bill -- so the finding is reported beside
    # the bankable total rather than inside it. Every currency bucket is still converted
    # on the way there, which is what this test exists to prove: 100 USD + 8000 INR at
    # 80/USD is 200 USD however the finding is later classified.
    assert merged["opportunities"] == []
    assert merged["at_risk_count"] == 1
    assert merged["at_risk_total"] == merged["at_risk"][0]["items"][0]["cost"] == 200


def test_mixed_resource_rightsizing_converts_every_bucket(monkeypatch, mixed_costs, fx):
    monkeypatch.setattr(waste, "_arg", AsyncMock(return_value=[{
        "id": rid("vm"), "name": "vm", "resourceGroup": "rg", "subscriptionId": SUB_A,
        "state": "PowerState/deallocated",
    }]))
    result = asyncio.run(waste.vm_utilisation(subscription_ids=[SUB_A]))
    assert result["stopped_cost"] is None
    assert result["stopped_cost_by_currency"] == {"USD": 100, "INR": 8000}
    merged = savings.assemble({"rightsizing": result}, {}, 30, "USD")
    assert not merged["partial"] and merged["total"] == 200


@pytest.mark.parametrize("target", [None, "USD", "INR"])
def test_mixed_resource_shutdown_preserves_monthly_currency_buckets(monkeypatch, mixed_costs, fx, target):
    monkeypatch.setattr(waste, "_arg", AsyncMock(return_value=[{
        "id": rid("vm"), "name": "vm", "resourceGroup": "rg", "subscriptionId": SUB_A,
    }]))
    monkeypatch.setattr(shutdown, "_hourly_cpu", AsyncMock(return_value={}))
    monkeypatch.setattr(shutdown, "profile", lambda _: {"enough_data": True, "schedulable": True})
    result = asyncio.run(shutdown.find_schedulable([SUB_A], currency=target))
    assert result["count"] == 1 and not result["partial"]
    candidate = result["candidates"][0]
    assert candidate["estimate_period_days"] == 30
    if target is None:
        assert candidate["saving"] is result["total_saving"] is None
        assert set(candidate["saving_by_currency"]) == {"USD", "INR"}
    merged = savings.assemble({"shutdown": result}, {}, 7, "USD")
    assert not merged["partial"]
    assert merged["total"] == pytest.approx(shutdown.saving(200)["saving"] * 7 / 30, abs=0.01)


def test_historical_unknown_currency_is_not_assigned_to_current_usd():
    class Reader:
        agg = "costs"
        benefit = "costs"
        spot = "costs"

        def rows(self, sql, limit=None):
            if 'max("ChargePeriodStart")' in sql:
                return [{"hi": "2026-09-14"}]
            if '"BenefitName"' in sql:
                return []
            if "strftime" in sql:
                return [{"month": "2026-08", "currency": None, "model": "OnDemand", "cost": 999},
                        {"month": "2026-09", "currency": "USD", "model": "OnDemand", "cost": 100}]
            return [{"currency": "USD", "model": "OnDemand", "cost": 100}]

    @contextmanager
    def reader(*args, **kwargs):
        yield Reader()

    subject = commitments.Commitments.__new__(commitments.Commitments)
    subject.wh = SimpleNamespace(reader=reader)
    result = subject.coverage([SUB_A], 30)
    assert result["currency"] == "USD" and result["total"] == 100
    assert len(result["trend_by_currency"]["USD"]) == 1
    assert result["trend_by_currency"]["unknown"][0]["currency"] is None
    assert result["trend_by_currency"]["unknown"][0]["total"] is None
    assert result["history_partial"]


def test_only_authoritative_missing_days_can_be_zero_filled():
    days = [(date(2026, 7, 17) + timedelta(days=i)).isoformat() for i in range(60)]
    missing = days[55]
    rows = [("Compute", day, 20.0) for day in days if day != missing]
    assert anomalies.detect(rows, "ServiceName") == []
    assert missing not in anomalies._series(rows, "ServiceName")["Compute"]
    result = anomalies.detect(rows, "ServiceName", covered_days=set(days))
    assert len(result) == 1 and result[0]["day"] == missing and result[0]["cost"] == 0


def test_unpriced_resource_bucket_is_explicit_and_never_relabelled(monkeypatch, fx):
    value = {"cost": None, "currency": None, "cost_by_currency": {"USD": 100, "unknown": None},
             "pricing_status": "unavailable"}
    monkeypatch.setattr(waste, "_costs_for", lambda *args, **kwargs: {rid("vm").lower(): value})
    monkeypatch.setattr(waste, "_arg", AsyncMock(return_value=[{
        "id": rid("vm"), "name": "vm", "resourceGroup": "rg", "subscriptionId": SUB_A,
    }]))
    raw = asyncio.run(waste.find_waste(categories=["deallocated_vms"], subscription_ids=[SUB_A]))
    assert raw["total_cost"] is None and raw["partial"]
    assert raw["findings"][0]["cost_by_currency"] == {"USD": 100, "unknown": None}
    result = savings.assemble({"waste": raw}, {}, 30, "USD")
    assert result["total"] == 0 and result["partial"]
    assert result["unconverted"]
    monkeypatch.setattr(shutdown, "_hourly_cpu", AsyncMock(return_value={}))
    monkeypatch.setattr(shutdown, "profile", lambda _: {"enough_data": True, "schedulable": True})
    scheduled = asyncio.run(shutdown.find_schedulable([SUB_A], currency="USD"))
    assert scheduled["count"] == 0 and scheduled["total_saving"] is None
    assert scheduled["partial"] and scheduled["unpriced_candidates"]
