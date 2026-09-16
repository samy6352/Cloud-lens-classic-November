"""Resource aggregation regressions against an in-memory SQL fixture; no external services."""
import json
import re
import sqlite3
from contextlib import contextmanager
from datetime import date, timedelta

import pytest

from app import reporting, resources


@pytest.fixture
def inventory(monkeypatch):
    connection = sqlite3.connect(":memory:")
    connection.row_factory = sqlite3.Row

    class JsonAggregate:
        def __init__(self):
            self.values = []

        def step(self, value):
            self.values.append(value)

        def finalize(self):
            return json.dumps(self.values)

    connection.create_aggregate("json_agg", 1, JsonAggregate)
    columns = (
        "ResourceId", "ResourceName", "ResourceGroup", "SubAccountName", "SubAccountId",
        "ServiceName", "ServiceFamily", "RegionName", "BillingCurrency", "BilledCost",
        "Tags", "ChargePeriodStart",
    )
    connection.execute("CREATE TABLE costs (" + ", ".join(
        f'"{name}" {"REAL" if name == "BilledCost" else "TEXT"}' for name in columns) + ")")

    class Reader:
        def rows(self, sql, limit=200):
            def rolling(match):
                start = date.fromisoformat(match[1]) - timedelta(days=int(match[2]))
                return f"'{start.isoformat()}'"

            sql = re.sub(r"DATE '([\d-]+)' - INTERVAL (\d+) DAY", rolling, sql)
            sql = re.sub(r"DATE '([\d-]+)'", r"'\1'", sql)
            return [dict(row) for row in connection.execute(sql).fetchmany(limit)]

    class Warehouse:
        @contextmanager
        def reader(self, *args, **kwargs):
            yield Reader()

    service = resources.Resources.__new__(resources.Resources)
    service.wh = Warehouse()

    def run(rows, **kwargs):
        for values in rows:
            row = {
                "ResourceId": "/subscriptions/a/resources/vm", "ResourceName": "vm",
                "ResourceGroup": "shared", "SubAccountId": "a",
                "ServiceName": "Virtual Machines", "ServiceFamily": "Compute",
                "BillingCurrency": "USD", "BilledCost": 10, "ChargePeriodStart": "2026-09-06",
                **values,
            }
            connection.execute("INSERT INTO costs VALUES (" + ",".join("?" for _ in columns) + ")",
                               [row.get(column) for column in columns])
        return service.inventory(["a", "b"], **kwargs)

    with reporting.select(None):
        yield run
    connection.close()


def test_resource_limit_keeps_highest_cost_groups(inventory, monkeypatch):
    monkeypatch.setattr(resources, "MAX_RESOURCES", 1)
    result = inventory([
        {"ResourceId": "/a", "BilledCost": 1},
        {"ResourceId": "/z", "BilledCost": 900},
    ])
    assert result["truncated"]
    assert result["resources"][0]["id"] == "/z"


def test_resources_do_not_add_native_currencies(inventory):
    result = inventory([{}, {"BillingCurrency": "EUR", "BilledCost": 20}])
    assert result["total_cost"] is None
    assert result["total_cost_by_currency"] == {"EUR": 20, "USD": 10}
    assert result["mixed_currency"]
    assert result["unique_count"] == 1 and result["count"] == 2


def test_resource_identity_and_groups_include_subscription(inventory):
    result = inventory([
        {"ResourceId": "/subscriptions/a/resources/VM"},
        {"ResourceId": "/subscriptions/a/resources/vm", "BillingCurrency": " usd "},
        {"ResourceId": "/subscriptions/b/resources/vm", "SubAccountId": "b"},
    ])
    assert result["count"] == result["unique_count"] == 2
    assert result["total_cost"] == 30
    assert result["resource_groups"] == 2
    assert not result["mixed_currency"]


def test_unknown_currency_is_not_a_monetary_total(inventory):
    result = inventory([{"BillingCurrency": ""}])
    assert result["currency"] is None
    assert result["total_cost"] is None
    assert result["total_cost_by_currency"] == {"unknown": None}


def test_primary_service_uses_current_aggregate_and_preserves_associations(inventory):
    rows = [{"ServiceName": "Azure App Service", "ServiceFamily": "Web",
             "BilledCost": 2, "ChargePeriodStart": "2026-09-14"} for _ in range(5)]
    rows += [
        {"ServiceName": "Microsoft Defender for Cloud", "ServiceFamily": "Security",
         "BilledCost": 8, "ChargePeriodStart": "2026-09-14"},
        {"ServiceName": "ZZ Historical Service", "ServiceFamily": "ZZ Historical",
         "BilledCost": 999, "ChargePeriodStart": "2026-08-01"},
    ]
    result = inventory(rows)
    resource = result["resources"][0]
    assert resource["service"] == "Azure App Service"
    assert resource["area_id"] == "compute"
    assert resource["service_names"] == ["Azure App Service", "Microsoft Defender for Cloud"]
    assert resource["cost"] == 18


def test_prior_only_resource_keeps_prior_service_associations(inventory):
    result = inventory([
        {"ResourceId": "/old", "ServiceName": "Storage", "ChargePeriodStart": "2026-08-01"},
        {"ResourceId": "/current", "ServiceName": "Compute", "ChargePeriodStart": "2026-09-14"},
    ])
    old = next(row for row in result["resources"] if row["id"] == "/old")
    assert old["cost"] == 0
    assert old["service_names"] == ["Storage"]


def test_current_zero_net_service_is_not_replaced_by_historical_metadata(inventory):
    result = inventory([
        {"ServiceName": "Azure App Service", "BilledCost": 0, "ChargePeriodStart": "2026-09-14"},
        {"ServiceName": "Defender", "BilledCost": 100, "ChargePeriodStart": "2026-08-01"},
    ])
    assert result["resources"][0]["service"] == "Azure App Service"


def test_resource_total_rounds_only_after_aggregation(inventory):
    result = inventory([{"ResourceId": "/a", "BilledCost": 0.004},
                        {"ResourceId": "/b", "BilledCost": 0.004}])
    assert result["total_cost"] == 0.01


def test_comparison_window_does_not_imply_resource_creation(inventory):
    result = inventory([
        {"ChargePeriodStart": "2026-08-07", "BilledCost": 0},
        {"ChargePeriodStart": "2026-09-06", "BilledCost": 20},
    ], days=30)
    item = result["resources"][0]
    assert item["prior_cost"] == 0
    assert item["change_pct"] is None
    assert item["first_seen"] == "2026-08-07"


def test_resource_calendar_bounds_keep_equal_length_comparison(inventory):
    with reporting.select("2024-02"):
        result = inventory([
            {"ChargePeriodStart": day, "BilledCost": cost}
            for day, cost in [
                ("2024-01-02", 999), ("2024-01-03", 7), ("2024-01-31", 3),
                ("2024-02-01", 11), ("2024-02-29", 29), ("2024-03-01", 1000),
            ]
        ], days=1)
    assert result["days"] == 29
    assert result["as_of"] == "2024-02-29"
    item = result["resources"][0]
    assert item["cost"] == 40 and item["prior_cost"] == 10
    assert item["first_seen"] == "2024-01-03"
    assert item["last_seen"] == "2024-02-29"


def test_charges_without_resource_identity_do_not_inflate_resource_cost(inventory):
    result = inventory([{"ResourceId": " "}])
    assert result["count"] == result["unique_count"] == 0
    assert result["total_cost"] == 0
