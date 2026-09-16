"""Offline checks for tag-window boundaries and bounded drill-down consistency."""
import sys
from contextlib import contextmanager
from types import SimpleNamespace

import pytest

from app import reporting, tags


@pytest.fixture
def tag_reader(monkeypatch):
    queries = []
    rows = [
        {"id": "a", "cost": 10, "currency": "USD", "tags": '{"Owner":"A"}'},
        {"id": "b", "cost": 20, "currency": "USD", "tags": "{}"},
    ]
    daily = [{"id": r["id"], "cost": r["cost"], "currency": r["currency"], "day": "2026-09-06"}
             for r in rows]

    class Reader:
        def rows(self, sql, limit=200):
            queries.append(sql)
            if 'AS hi' in sql:
                return [{"hi": "2026-09-06"}]
            if "STRING_AGG" in sql:
                return []
            if "AS day" in sql:
                return daily[:limit]
            return rows[:limit]

    @contextmanager
    def reader(*args, **kwargs):
        yield Reader()

    monkeypatch.setitem(sys.modules, "app.warehouse",
                        SimpleNamespace(warehouse=SimpleNamespace(reader=reader)))
    with reporting.select(None):
        yield SimpleNamespace(queries=queries, rows=rows, daily=daily)


def test_tag_rolling_window_uses_exact_day_count(tag_reader):
    result = tags.cost_by_tag(["a"], days=30)
    assert result["days"] == 30
    assert all('"ChargePeriodStart" > DATE \'2026-09-06\' - INTERVAL 30 DAY' in query
               for query in tag_reader.queries if "WHERE" in query)


def test_tag_limit_keeps_daily_and_resource_totals_consistent(tag_reader, monkeypatch):
    monkeypatch.setattr(tags, "MAX_RESOURCES", 1)
    result = tags.cost_by_tag(["a"])
    assert result["truncated"]
    assert sum(result["daily_total"]) == result["total"] == 10
    assert sum(cost for row in result["resources"] for _, cost in row["d"]) == 10


def test_recorded_zero_days_are_not_dropped(tag_reader):
    tags.cost_by_tag(["a"])
    assert all("HAVING" not in query for query in tag_reader.queries if "AS day" in query)


def test_empty_tag_selection_preserves_calendar_days():
    with reporting.select("2024-02"):
        assert tags.cost_by_tag([], days=1)["days"] == 29


def test_tag_currency_is_selected_before_detail_limits(tag_reader):
    tag_reader.rows[1]["currency"] = "EUR"
    tag_reader.daily[1]["currency"] = "EUR"
    result = tags.cost_by_tag(["a"])
    assert result["mixed"] and result["currency"] == "EUR"
    detail = [q for q in tag_reader.queries if "AS name" in q or "AS day" in q]
    assert len(detail) == 2
    assert all('AND COALESCE(NULLIF("BillingCurrency", \'\'), \'unknown\') = \'EUR\'' in q
               for q in detail)


def test_tag_resource_and_daily_costs_retain_precision_for_client_totals(tag_reader):
    tag_reader.rows[:] = [
        {"id": f"r{i}", "cost": 0.00491, "currency": "USD",
         "tags": '{"Owner":"A"}' if i < 49 else "{}"}
        for i in range(271)
    ]
    tag_reader.daily[:] = [
        {"id": row["id"], "cost": row["cost"], "currency": "USD", "day": "2026-09-06"}
        for row in tag_reader.rows
    ]
    result = tags.cost_by_tag(["a"])
    assert result["resources"][0]["cost"] == 0.00491
    assert result["resources"][0]["d"] == [[0, 0.00491]]
    assert round(sum(r["cost"] for r in result["resources"]), 2) == result["total"] == 1.33
    for name, tagged in (("tagged", True), ("untagged", False)):
        rows = [r for r in result["resources"] if bool(r["keys"]) == tagged]
        assert len(rows) == result[name]["resources"]
        assert round(sum(r["cost"] for r in rows), 2) == result[name]["cost"]
    assert round(sum(cost for r in result["resources"] for _, cost in r["d"]), 2) == result["total"]


def test_tag_availability_is_per_resource_not_the_last_import(tag_reader):
    tag_reader.rows[:] = [
        {"id": "tagged", "cost": 10, "currency": "USD", "tags": '{"Owner":"A"}', "tags_available": False},
        {"id": "untagged", "cost": 20, "currency": "USD", "tags": None, "tags_available": True},
        {"id": "quick", "cost": 30, "currency": "USD", "tags": None, "tags_available": False},
    ]
    result = tags.cost_by_tag(["a"])
    assert [r["tag_status"] for r in result["resources"]] == ["tagged", "untagged", "unknown"]
    assert result["tagged"] == {"cost": 10, "resources": 1}
    assert result["untagged"] == {"cost": 20, "resources": 1}
    assert result["unknown"] == {"cost": 30, "resources": 1}
    assert result["total"] == 60 and result["tags_not_loaded"]
    query = next(q for q in tag_reader.queries if "AS name" in q)
    assert "BOOL_AND" in query
    assert "NOT IN ('', '{}', '[]', 'null')" in query


def test_complete_tag_data_is_not_overridden_by_other_subscription_loads(tag_reader):
    for row in tag_reader.rows:
        row["tags_available"] = True
    result = tags.cost_by_tag(["a"])
    assert result["tags_not_loaded"] is False
    assert result["untagged"]["resources"] == 1
    assert result["unknown"]["resources"] == 0
