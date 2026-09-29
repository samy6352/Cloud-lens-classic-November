"""Keep additive commitment summaries on exact derived data when available."""
from contextlib import contextmanager

import pytest

from app import billing_commitments
from app.commitments import Commitments

pytestmark = pytest.mark.offline


def test_coverage_uses_rollup_for_additive_summaries_and_detail_for_benefits():
    queries = []
    reader_options = []

    class Reader:
        agg = "costs_rollup"
        benefit = "commitment_benefits"
        spot = "spot_rates"

        def rows(self, sql, limit=200):
            queries.append(sql)
            if "AS hi" in sql:
                return [{"hi": "2026-09-17"}]
            if '"BenefitName" AS name' in sql:
                return [{"name": "Plan A", "model": "Reservation",
                         "currency": "USD", "cost": 20, "resources": 1}]
            if 'strftime("ChargePeriodStart"' in sql:
                return [{"month": "2026-09", "model": "Reservation",
                         "currency": "USD", "cost": 30}]
            if '"ServiceFamily" = \'Compute\'' in sql:
                return [
                    {"model": "OnDemand", "currency": "USD", "cost": 20},
                    {"model": "Reservation", "currency": "USD", "cost": 30},
                ]
            return [
                {"model": "OnDemand", "currency": "USD", "cost": 100},
                {"model": "Reservation", "currency": "USD", "cost": 50},
            ]

    @contextmanager
    def reader(*_args, **kwargs):
        reader_options.append(kwargs)
        yield Reader()

    service = object.__new__(Commitments)
    service.wh = type("Warehouse", (), {"reader": staticmethod(reader)})()

    result = service.coverage(["subscription-a"], days=30)

    assert result["coverage_pct"] == 33.3
    assert result["compute_coverage_pct"] == 60.0
    assert result["benefits"][0]["name"] == "Plan A"
    assert result["trend"][0]["committed"] == 30
    assert reader_options[0]["include_commitments"] is True
    summary_queries = [
        sql for sql in queries
        if '"BenefitName" AS name' not in sql
        and ("AS hi" in sql or '"ServiceFamily" = \'Compute\'' in sql
             or 'strftime("ChargePeriodStart"' in sql
             or '"BillingCurrency" AS currency' in sql)
    ]
    assert all("FROM costs_rollup" in sql for sql in summary_queries)
    assert any('"BenefitName" AS name' in sql and "FROM commitment_benefits " in sql
               for sql in queries)
    assert all('count(DISTINCT "ResourceId")' not in sql for sql in summary_queries)


def test_billing_currency_census_uses_reader_aggregate(monkeypatch):
    queries = []

    class Reader:
        agg = "costs_rollup"
        benefit = "commitment_benefits"
        spot = "spot_rates"

        def rows(self, sql, limit=200):
            queries.append(sql)
            return [{"currency": "USD"}, {"currency": "INR"}]

    @contextmanager
    def reader(*_args, **_kwargs):
        yield Reader()

    service = type(
        "CommitmentsService",
        (),
        {
            "inventory": staticmethod(lambda *_args, **_kwargs: {"commitments": []}),
            "wh": type("Warehouse", (), {"reader": staticmethod(reader)})(),
        },
    )()
    monkeypatch.setattr(billing_commitments, "get_commitments", lambda: service)

    result = billing_commitments._warehouse_inventory(["subscription-a"], 90, None)

    assert result["native_billing_currencies"] == ["INR", "USD"]
    assert len(queries) == 1
    assert "FROM costs_rollup" in queries[0]


def test_spot_comparison_reads_compact_meter_rollup():
    queries = []

    class Reader:
        agg = "costs_rollup"
        benefit = "commitment_benefits"
        spot = "spot_rates"

        def rows(self, sql, limit=200):
            queries.append(sql)
            if "AS hi" in sql:
                return [{"hi": "2026-09-17"}]
            return [
                {"month": "2026-09", "meter": "D2", "model": "OnDemand",
                 "currency": "USD", "region": "eastus", "unit": "hour",
                 "cost": 100, "qty": 100},
                {"month": "2026-09", "meter": "D2", "model": "Spot",
                 "currency": "USD", "region": "eastus", "unit": "hour",
                 "cost": 30, "qty": 100},
            ]

    @contextmanager
    def reader(*_args, **kwargs):
        assert kwargs["include_commitments"] is True
        yield Reader()

    service = object.__new__(Commitments)
    service.wh = type("Warehouse", (), {"reader": staticmethod(reader)})()

    result = service.spot_savings(["subscription-a"], days=7)

    assert result["total_saved"] == 70
    assert "FROM spot_rates" in queries[-1]
