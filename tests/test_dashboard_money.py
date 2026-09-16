"""Real dashboard SQL on an in-memory database, with date syntax translated only."""
from __future__ import annotations

import re
import sqlite3
from contextlib import contextmanager

import pytest

from app.dashboard import Dashboard

pytestmark = pytest.mark.offline


class Reader:
    def __init__(self, db):
        self.db = db

    def rows(self, sql, limit=200):
        sql = re.sub(
            r"date '([^']+)' - INTERVAL (\d+) (DAY|MONTH)",
            lambda match: f"date('{match[1]}', '-{match[2]} {match[3].lower()}')", sql, flags=re.I)
        sql = re.sub(r"DATE '([^']+)'", r"'\1'", sql, flags=re.I)
        sql = sql.replace('strftime("ChargePeriodStart", \'%Y-%m\')',
                          'strftime(\'%Y-%m\', "ChargePeriodStart")')
        cursor = self.db.execute(sql)
        names = [column[0] for column in cursor.description]
        rows = cursor.fetchall() if limit is None else cursor.fetchmany(limit)
        return [dict(zip(names, row)) for row in rows]


class MemoryWarehouse:
    def __init__(self):
        self.db = sqlite3.connect(":memory:")
        self.db.execute('CREATE TABLE costs ("ChargePeriodStart", "BillingCurrency", '
                        '"BilledCost" REAL, "ResourceId", "ResourceName", "ResourceGroup", '
                        '"ServiceName", "ServiceFamily", "SubAccountId", "SubAccountName", '
                        '"RegionName", "PublisherName")')

    def add(self, amount, *, currency="USD", day="2026-09-10", resource="vm",
            group="group", service="Virtual Machines", family="Compute", subscription="sub-a"):
        self.db.execute("INSERT INTO costs VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                        (day, currency, amount, f"/{subscription}/{group}/{resource}", resource,
                         group, service, family, subscription, subscription, "eastus", "Microsoft"))

    @contextmanager
    def reader(self, scope=None, currency=None, filters=None):
        clauses = []
        if scope:
            clauses.append('"SubAccountId" IN (' + ",".join("'" + s + "'" for s in scope) + ")")
        if filters and filters.get("service"):
            clauses.append('"ServiceName" IN (' + ",".join("'" + s + "'" for s in filters["service"]) + ")")
        select = "*"
        if currency:
            assert currency == "USD"
            columns = [row[1] for row in self.db.execute("PRAGMA main.table_info(costs)")]
            select = ",".join(
                """CASE WHEN "BillingCurrency"='EUR' THEN "BilledCost"*2 ELSE "BilledCost" END AS "BilledCost" """
                if name == "BilledCost" else "'USD' AS \"BillingCurrency\"" if name == "BillingCurrency"
                else f'"{name}"' for name in columns)
        self.db.execute("CREATE TEMP VIEW costs AS SELECT " + select + " FROM main.costs"
                        + (" WHERE " + " AND ".join(clauses) if clauses else ""))
        try:
            yield Reader(self.db)
        finally:
            self.db.execute("DROP VIEW temp.costs")


@pytest.fixture
def dashboard():
    dashboard = Dashboard.__new__(Dashboard)
    dashboard.wh = MemoryWarehouse()
    yield dashboard
    dashboard.wh.db.close()


def views(result):
    return {item["currency"]: item for item in result["by_currency"]}


def test_mixed_executive_is_separate_for_every_money_shape(dashboard):
    dashboard.wh.add(100)
    dashboard.wh.add(80, currency="EUR", subscription="sub-b")
    dashboard.wh.add(10, day="2026-08-10")
    dashboard.wh.add(200, currency="EUR", day="2026-08-10", subscription="sub-b")
    result = dashboard.executive(None, days=30)
    assert result["mixed_currency"] and result["currency"] is None
    assert result["kpis"]["total"] is None
    assert not result["trend"]["values"] and not result["areas"] and result["monthly"] is None
    by_currency = views(result)
    for currency, total, previous in (("USD", 100, 10), ("EUR", 80, 200)):
        item = by_currency[currency]
        assert item["currency"] == currency and not item["mixed_currency"]
        assert item["kpis"]["total"] == total and item["kpis"]["previous"] == previous
        assert item["trend"]["values"] == [total]
        assert item["trend"]["previous"] == [previous]
        assert sum(area["cost"] for area in item["areas"]) == total
        assert item["areas"][0]["share_pct"] == 100
        assert item["kpis"]["top5_share_pct"] == 100
        assert item["monthly"]["areas"][0]["data"] == [previous, total]
        for key in ("movers", "services", "subscriptions", "resource_groups", "regions"):
            assert item[key][0]["cost"] == total


def test_mixed_section_keeps_prior_and_estate_share_in_same_currency(dashboard):
    dashboard.wh.add(100)
    dashboard.wh.add(100, family="Storage")
    dashboard.wh.add(80, currency="EUR")
    dashboard.wh.add(10, day="2026-08-10")
    dashboard.wh.add(200, currency="EUR", day="2026-08-10")
    result = dashboard.section("compute", None, days=30)
    assert result["kpis"]["total"] is None and result["currency"] is None
    by_currency = views(result)
    assert by_currency["USD"]["kpis"]["share_pct"] == 50
    assert by_currency["EUR"]["kpis"]["share_pct"] == 100
    for currency, total, previous in (("USD", 100, 10), ("EUR", 80, 200)):
        item = by_currency[currency]
        assert item["kpis"]["total"] == total and item["kpis"]["previous"] == previous
        assert item["trend"]["values"] == [total]
        assert item["services"][0]["cost"] == total
        assert item["regions"][0]["cost"] == total
        assert item["resources_top"][0]["cost"] == total


def test_single_and_converted_shapes_stay_compatible(dashboard):
    dashboard.wh.add(100)
    result = dashboard.executive(None)
    assert result["currency"] == "USD" and result["kpis"]["total"] == 100
    assert not result["by_currency"] and not result["mixed_currency"]
    dashboard.wh.add(80, currency="EUR")
    converted = dashboard.executive(None, currency="USD")
    assert converted["kpis"]["total"] == 260
    assert converted["trend"]["values"] == [260]
    assert converted["areas"][0]["cost"] == 260
    assert not converted["by_currency"] and not converted["mixed_currency"]
    assert dashboard.section("compute", None, currency="USD")["kpis"]["total"] == 260


def test_null_other_and_rail_buckets_reconcile(dashboard):
    dashboard.wh.add(25, family=None)
    dashboard.wh.add(10, currency="EUR", family=None)
    rail = dashboard.sections(None)
    other = next(item for item in rail["sections"] if item["id"] == "other")
    assert rail["total"] is None and other["cost"] is None
    assert other["cost_by_currency"] == rail["total_by_currency"] == [
        {"currency": "EUR", "cost": 10}, {"currency": "USD", "cost": 25}]
    result = views(dashboard.section("other", None))
    assert result["USD"]["kpis"]["total"] == 25
    assert result["EUR"]["kpis"]["total"] == 10


def test_executive_ranks_before_caps_and_ranks_movers_independently(dashboard):
    for index in range(405):
        dashboard.wh.add(1, group=f"g{index:03}", service=f"s{index:03}", subscription=f"sub{index:03}")
    dashboard.wh.add(1_000_000, group="zz-largest", service="zz-largest", subscription="zz-largest")
    dashboard.wh.add(900_000, day="2026-08-10", service="removed")
    result = dashboard.executive(None, days=30)
    assert result["kpis"]["total"] == 1_000_405
    for key in ("services", "subscriptions", "resource_groups"):
        assert result[key][0]["name"] == "zz-largest"
    assert next(row for row in result["movers"] if row["name"] == "removed")["delta"] == -900_000
    assert result["kpis"]["top5_share_pct"] == 100


def test_presentation_limits_are_per_currency_and_counts_are_not_capped(dashboard):
    for index in range(100):
        dashboard.wh.add(100, service=f"s{index:03}", resource=f"vm{index:03}")
    dashboard.wh.add(1, currency="EUR", service="euro", subscription="sub-b")
    result = views(dashboard.section("compute", None))
    assert result["USD"]["kpis"]["services"] == 100
    assert len(result["USD"]["services"]) == 12
    assert result["EUR"]["kpis"]["total"] == 1
    assert result["EUR"]["services"][0]["name"] == "euro"
    assert result["EUR"]["resources_top"][0]["cost"] == 1


def test_unknown_currency_is_not_combined_with_known_money(dashboard):
    dashboard.wh.add(100)
    dashboard.wh.add(5, currency=None)
    result = views(dashboard.executive(None))
    assert result["unknown"]["kpis"]["total"] == 5
    assert result["USD"]["kpis"]["total"] == 100


def test_family_aggregation_does_not_round_line_groups_before_summing(dashboard):
    dashboard.wh.add(0.004, family="Compute")
    dashboard.wh.add(0.004, family="Web")
    result = dashboard.executive(None)
    assert result["kpis"]["total"] == 0.01
    assert result["areas"][0]["cost"] == 0.01


def test_monthly_only_currency_cannot_contaminate_current_currency(dashboard):
    dashboard.wh.add(100)
    dashboard.wh.add(7, currency="EUR", day="2026-05-10")
    result = views(dashboard.executive(None, days=7))
    assert result["USD"]["kpis"]["total"] == 100
    assert result["EUR"]["kpis"]["total"] == 0


def test_scope_and_service_filters_still_restrict_every_view(dashboard):
    dashboard.wh.add(100)
    dashboard.wh.add(999, subscription="sub-b")
    dashboard.wh.add(999, service="Another")
    result = dashboard.executive(["sub-a"], filters={"service": ["Virtual Machines"]})
    assert result["kpis"]["total"] == 100
    assert result["services"] == [{
        "name": "Virtual Machines", "cost": 100, "previous": 0, "delta": 100,
        "change_pct": None, "share_pct": 100}]
