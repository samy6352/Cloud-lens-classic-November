"""The overview distinguishes confirmed absence from unreadable or incomplete budgets."""
from __future__ import annotations

import asyncio
import sys
from datetime import UTC, datetime
from types import ModuleType, SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from app import cost

pytestmark = pytest.mark.offline

SUBS = [{"id": "a", "name": "Alpha"}, {"id": "b", "name": "Beta"}]


def budget(percent=25, **changes):
    return {
        "name": "Monthly", "subscription": "Alpha", "subscription_id": "a",
        "percent_used": percent, "status": cost._budget_status(percent), **changes,
    }


def payload(rows=(), **changes):
    return {
        "budgets": list(rows), "count": len(rows), "over": 0,
        "subscriptions_requested": 2, "subscriptions_read": 2,
        "partial": False, "error": None, "unavailable_subscriptions": [], **changes,
    }


@pytest.fixture
def overview(monkeypatch):
    warehouse = SimpleNamespace(
        summary=Mock(return_value={
            "rows": 1, "to": datetime.now(UTC).date().isoformat(),
            "currency": "USD", "subscriptions": 2,
        }),
        query=Mock(return_value={"rows": [{"mtd": 123.45, "prev": 98.76}]}),
    )
    module = ModuleType("app.warehouse")
    module.warehouse = warehouse
    monkeypatch.setitem(sys.modules, "app.warehouse", module)
    azure_request = AsyncMock(side_effect=AssertionError("Unexpected Azure request"))
    monkeypatch.setattr(cost.azure, "request", azure_request)
    summary = AsyncMock(side_effect=AssertionError("Unexpected live cost query"))
    monkeypatch.setattr(cost, "cost_summary", summary)
    forecast = AsyncMock(return_value={"projected_total": 234.56})
    monkeypatch.setattr(cost, "_forecast_by_subscription", forecast)

    def run(result, scope=None, subscriptions=None):
        listing = AsyncMock(return_value={"subscriptions": SUBS})
        if isinstance(subscriptions, Exception):
            listing.side_effect = subscriptions
        elif subscriptions is not None:
            listing.return_value = {"subscriptions": subscriptions}
        monkeypatch.setattr(cost, "list_subscriptions", listing)
        lookup = AsyncMock(return_value=result)
        if isinstance(result, Exception):
            lookup.side_effect = result
        monkeypatch.setattr(cost, "budgets", lookup)

        answer = asyncio.run(cost.overview(scope))
        lookup.assert_awaited_once_with(scope)
        listing.assert_awaited_once_with()
        assert forecast.await_args.args == (scope,)
        assert warehouse.query.call_args.kwargs == {"scope": scope}
        azure_request.assert_not_awaited()
        summary.assert_not_awaited()
        assert answer["month_to_date"] == 123.45
        assert answer["last_month"] == 98.76
        assert answer["forecast_30d"] == 234.56
        assert answer["currency"] == "USD"
        assert answer["scope"] == (scope or None)
        return answer

    return run


@pytest.mark.parametrize("percent", [0, 25, 85, 105])
def test_known_budget_readings_keep_compatible_summary(overview, percent):
    row = budget(percent)
    answer = overview(payload([row], over=int(percent >= 100)))
    assert answer["budget_status"] == "ok"
    assert answer["budget_note"] is None
    assert answer["budget"] == {
        "name": "Monthly", "percent_used": percent, "status": row["status"],
        "over": int(percent >= 100), "tracked": 1, "subscription": "Alpha",
    }


def test_selects_highest_known_reading(overview):
    answer = overview(payload([budget(0), budget(110, name="Highest"), budget(50)]))
    assert answer["budget_status"] == "ok"
    assert answer["budget"]["name"] == "Highest"
    assert answer["budget"]["percent_used"] == 110


@pytest.mark.parametrize("scope", [None, ["a", "b"], ["ALPHA", "b"]])
def test_only_successful_complete_empty_read_means_none(overview, scope):
    answer = overview(payload(), scope)
    assert answer["budget_status"] == "none"
    assert answer["budget"] is None
    assert "No budgets defined" in answer["budget_note"]


def test_single_selected_scope_can_confirm_absence(overview):
    answer = overview(payload(subscriptions_requested=1, subscriptions_read=1), ["Alpha"])
    assert answer["budget_status"] == "none"


@pytest.mark.parametrize("exception", [cost.CostError("secret detail"), RuntimeError("secret detail")])
def test_budget_lookup_exception_is_unavailable_without_exposing_error(overview, exception):
    answer = overview(exception, ["a"])
    assert answer["budget_status"] == "unavailable"
    assert answer["budget"] is None
    assert "secret detail" not in answer["budget_note"]


@pytest.mark.parametrize("rows", [[], [budget(0)], [budget(None)]])
def test_partial_scope_reads_with_or_without_budgets(overview, rows):
    answer = overview(payload(
        rows, subscriptions_read=1, partial=True, error="secret detail",
        unavailable_subscriptions=[SUBS[1]],
    ))
    assert answer["budget_status"] == "partial"
    assert "selected scope" in answer["budget_note"]
    assert "secret detail" not in answer["budget_note"]
    assert (answer["budget"] is not None) == bool(rows and rows[0]["percent_used"] is not None)


@pytest.mark.parametrize("signal", [
    {"partial": True}, {"error": "secret detail"},
    {"unavailable_subscriptions": [SUBS[1]]}, {"subscriptions_read": 1},
])
def test_each_failure_signal_prevents_confirmed_absence(overview, signal):
    answer = overview(payload(**signal))
    assert answer["budget_status"] == "partial"
    assert answer["budget"] is None


def test_all_subscription_reads_failed(overview):
    answer = overview(payload(
        subscriptions_read=0, partial=True, error="secret detail", unavailable_subscriptions=SUBS,
    ))
    assert answer["budget_status"] == "unavailable"
    assert answer["budget"] is None


@pytest.mark.parametrize("missing", [budget(None), {"name": "Missing", "subscription": "Alpha"}])
def test_existing_budgets_without_usage_are_unknown_not_absent(overview, missing):
    answer = overview(payload([missing]))
    assert answer["budget_status"] == "unknown"
    assert answer["budget"] is None
    assert "Budgets exist" in answer["budget_note"]


@pytest.mark.parametrize("missing", [
    budget(None), {"name": "Missing"}, None, "bad row",
    budget(5, percent_used="5"), budget(5, percent_used=True),
    budget(5, percent_used=float("nan")), budget(5, percent_used=float("inf")),
])
def test_known_and_missing_or_malformed_readings_are_partial(overview, missing):
    answer = overview(payload([budget(0), missing]))
    assert answer["budget_status"] == "partial"
    assert answer["budget"]["percent_used"] == 0


@pytest.mark.parametrize("result", [
    None, [], "bad payload", {}, {"budgets": None}, {"budgets": {}},
    {"budgets": "bad list"}, {"budgets": []}, payload([None]),
    payload([{"percent_used": 20}]), payload(count=1),
    payload(subscriptions_requested=None), payload(subscriptions_read=None),
    payload(subscriptions_requested="2"), payload(subscriptions_read="2"),
    payload(subscriptions_requested=True), payload(subscriptions_read=-1),
    payload(subscriptions_read=3),
])
def test_malformed_or_unconfirmed_payload_never_claims_none(overview, result):
    answer = overview(result)
    assert answer["budget_status"] in {"unavailable", "partial"}
    assert answer["budget"] is None
    assert answer["budget_note"]


def test_old_payload_can_keep_known_reading_but_not_claim_complete_scope(overview):
    answer = overview({"budgets": [budget(0)], "count": 1, "over": 0})
    assert answer["budget_status"] == "partial"
    assert answer["budget"]["percent_used"] == 0


@pytest.mark.parametrize("scope", [None, ["missing"]])
def test_no_requested_subscriptions_is_not_confirmed_absence(overview, scope):
    answer = overview(payload(subscriptions_requested=0, subscriptions_read=0), scope, [])
    assert answer["budget_status"] == "unavailable"
    assert answer["budget"] is None


def test_scope_enumeration_failure_is_not_absence(overview):
    answer = overview(payload(), ["a"], cost.CostError("secret detail"))
    assert answer["budget_status"] == "unavailable"
    assert answer["budget"] is None
    assert "secret detail" not in answer["budget_note"]


@pytest.mark.parametrize("rows", [[], [budget(0)]])
def test_unresolved_scope_member_does_not_silently_narrow_summary(overview, rows):
    answer = overview(
        payload(rows, subscriptions_requested=1, subscriptions_read=1), ["a", "missing"],
    )
    assert answer["budget_status"] == ("partial" if rows else "unavailable")
