"""Offline checks for hourly reporting windows and mixed-currency query limits."""
import sys
from contextlib import contextmanager
from types import SimpleNamespace

import pytest

from app import reporting, uptime


def test_identified_vm_compute_is_offered_for_review():
    result = uptime._shape({
        "id": "/subscriptions/s/resourceGroups/rg/providers/Microsoft.Compute/virtualMachines/vm",
        "service": "Virtual Machines", "meter": "D2s v3", "hours": 24, "cost": 10, "peak_day_hours": 24})
    assert result["schedulable"] is True
    assert result["schedule_status"] == "needs_review"
    # The saving remains an estimate derived from billing, whatever the family.
    assert result["coverage_basis"] == "billing_estimate"


@pytest.mark.parametrize("service", ["Azure Cognitive Search", "Microsoft Defender for Cloud",
                                     "Event Hubs", "Virtual Machines Licenses", "Azure App Service",
                                     "SQL Database", "Azure Database for PostgreSQL", "Virtual Machines"])
def test_unschedulable_services_are_never_assessed(service):
    """These bill by the hour and cannot be switched off, so no saving may be implied."""
    result = uptime._shape({"service": service, "hours": 24, "cost": 10, "peak_day_hours": 24})
    assert result["schedulable"] is False
    assert result["schedule_status"] == "not_assessed"
    assert result["coverage_basis"] == "billing_estimate"


@pytest.fixture
def hourly_reader(monkeypatch):
    queries = []
    rows = [
        {"id": "a", "meter": "vm", "currency": "USD", "hours": 24, "cost": 10,
         "peak_day_hours": 24, "first_day": "2026-09-06", "last_day": "2026-09-06"},
        {"id": "b", "meter": "vm", "currency": "EUR", "hours": 24, "cost": 5,
         "peak_day_hours": 24, "first_day": "2026-09-06", "last_day": "2026-09-06"},
    ]
    cells = [{**row, "day": "2026-09-06"} for row in rows]

    class Reader:
        def rows(self, sql, limit=200):
            queries.append(sql)
            if "AS hi" in sql:
                return [{"hi": "2026-09-06"}]
            if "WITH src" not in sql:
                return [{"currency": "USD", "cost": 10}, {"currency": "EUR", "cost": 5}]
            if "peak_day_hours" in sql:
                return rows[:limit]
            return cells[:limit]

    @contextmanager
    def reader(*args, **kwargs):
        yield Reader()

    monkeypatch.setitem(sys.modules, "app.warehouse",
                        SimpleNamespace(warehouse=SimpleNamespace(reader=reader)))
    with reporting.select(None):
        yield queries


def test_hourly_rolling_window_uses_exact_day_count(hourly_reader):
    uptime.hourly_cost(["a"], days=30)
    assert all('"ChargePeriodStart" > DATE \'2026-09-06\' - INTERVAL 30 DAY' in query
               for query in hourly_reader if "WHERE" in query)


def test_native_currency_filter_does_not_hide_truncated_meters(hourly_reader, monkeypatch):
    monkeypatch.setattr(uptime, "MAX_ROWS", 1)
    result = uptime.hourly_cost(["a"])
    assert result["mixed_currency"]
    assert result["truncated"]


def test_native_currency_filter_does_not_hide_truncated_daily_cells(hourly_reader, monkeypatch):
    monkeypatch.setattr(uptime, "MAX_DAILY_CELLS", 1)
    result = uptime.hourly_cost(["a"])
    assert result["daily_dropped"]
    assert result["daily"] == {}


def test_hourly_currency_is_selected_before_detail_limits(hourly_reader):
    uptime.hourly_cost(["a"])
    detail = [query for query in hourly_reader if "WITH src" in query]
    assert len(detail) == 3
    assert all('AND COALESCE(NULLIF("BillingCurrency", \'\'), \'unknown\') = \'USD\'' in query
               for query in detail)


def test_weekend_only_is_offered_as_a_schedule_assumption():
    weekend = next(s for s in uptime.SCHEDULE_PRESETS if s["id"] == "weekend")
    # Saturday and Sunday, around the clock: the complement of "weekdays only".
    assert weekend["hours"] == 48
    weekdays = next(s for s in uptime.SCHEDULE_PRESETS if s["id"] == "weekdays")
    assert weekend["hours"] + weekdays["hours"] == 168
    assert all(0 < s["hours"] < 168 for s in uptime.SCHEDULE_PRESETS)
    assert len({s["id"] for s in uptime.SCHEDULE_PRESETS}) == len(uptime.SCHEDULE_PRESETS)


@pytest.fixture
def tagged_hourly_reader(monkeypatch):
    """One tagged meter, one sharing a key with a different value, and one with no tags."""
    rows = [
        {"id": "a", "meter": "vm", "currency": "USD", "hours": 24, "cost": 10,
         "peak_day_hours": 24, "first_day": "2026-09-06", "last_day": "2026-09-06",
         "tags": '{"app":"web","env":"prod"}'},
        {"id": "b", "meter": "vm", "currency": "USD", "hours": 12, "cost": 6,
         "peak_day_hours": 12, "first_day": "2026-09-06", "last_day": "2026-09-06",
         "tags": '{"app":"batch"}'},
        {"id": "c", "meter": "vm", "currency": "USD", "hours": 6, "cost": 4,
         "peak_day_hours": 6, "first_day": "2026-09-06", "last_day": "2026-09-06",
         "tags": ""},
    ]

    class Reader:
        def rows(self, sql, limit=200):
            if "AS hi" in sql:
                return [{"hi": "2026-09-06"}]
            if "WITH src" not in sql:
                return [{"currency": "USD", "cost": 20}]
            if "peak_day_hours" in sql:
                return rows[:limit]
            return [{**row, "day": "2026-09-06"} for row in rows][:limit]

    @contextmanager
    def reader(*args, **kwargs):
        yield Reader()

    monkeypatch.setitem(sys.modules, "app.warehouse",
                        SimpleNamespace(warehouse=SimpleNamespace(reader=reader)))
    with reporting.select(None):
        yield


def test_hourly_meters_carry_their_tags_and_a_key_breakdown(tagged_hourly_reader):
    result = uptime.hourly_cost(["a"])

    by_id = {r["id"]: r for r in result["resources"]}
    values = result["tag_values"]
    assert by_id["a"]["keys"] == ["app", "env"]
    assert [values[i] for i in by_id["a"]["v"]] == ["web", "prod"]
    assert by_id["c"]["keys"] == [] and by_id["c"]["v"] == []

    keys = {k["key"]: k for k in result["tag_keys"]}
    # `app` is on both tagged meters; `env` only on the first.
    assert keys["app"]["cost"] == 16 and keys["app"]["meters"] == 2
    assert keys["app"]["hours"] == 36
    # Two values under `app`, one under `env`: what tells a label from a breakdown.
    assert keys["app"]["values"] == 2 and keys["env"]["values"] == 1
    # Largest key first, so the picker opens on the one worth opening.
    assert [k["key"] for k in result["tag_keys"]] == ["app", "env"]


def test_untagged_hourly_spend_is_reported_rather_than_dropped(tagged_hourly_reader):
    result = uptime.hourly_cost(["a"])

    assert result["tagged"] == {"cost": 16.0, "hours": 36.0, "meters": 2}
    assert result["untagged"] == {"cost": 4.0, "hours": 6.0, "meters": 1}
    # Every hour-billed meter lands in exactly one of the two, so the card and the table below
    # it cannot disagree about the size of the estate.
    assert (result["tagged"]["meters"] + result["untagged"]["meters"]
            == len(result["resources"]))
    assert result["tagged"]["cost"] + result["untagged"]["cost"] == result["totals"]["cost"]


def test_a_key_is_counted_once_per_meter_not_once_per_value(tagged_hourly_reader):
    result = uptime.hourly_cost(["a"])
    # Summing keys double-counts a meter carrying two of them; that is expected, and is why
    # the tagged total is tracked separately rather than derived from the key list.
    assert sum(k["cost"] for k in result["tag_keys"]) > result["tagged"]["cost"]
    assert max(k["cost"] for k in result["tag_keys"]) <= result["tagged"]["cost"]


def test_an_empty_estate_still_describes_the_tag_dimension(monkeypatch):
    empty = uptime._empty(30)
    assert empty["tag_keys"] == [] and empty["tag_values"] == []
    assert empty["untagged"]["cost"] == 0.0
    assert any(s["id"] == "weekend" for s in empty["schedules"])
