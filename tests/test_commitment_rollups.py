"""The specialized commitment summaries preserve the exact raw-query grain."""
import sqlite3
from datetime import date

import pytest

from app import commitment_benefits, spot_rates

pytestmark = pytest.mark.offline


def test_benefit_rollup_is_sparse_scoped_and_retains_resource_identity():
    con = sqlite3.connect(":memory:")
    con.execute("""CREATE TABLE costs (
        "ChargePeriodStart" TEXT, "BillingCurrency" TEXT, "SubAccountId" TEXT,
        "BenefitId" TEXT, "BenefitName" TEXT, "PricingModel" TEXT,
        "ServiceName" TEXT, "ResourceGroup" TEXT, "RegionName" TEXT,
        "ChargeCategory" TEXT, "ResourceId" TEXT, "ResourceName" TEXT,
        "BilledCost" DOUBLE PRECISION
    )""")
    con.executemany(
        'INSERT INTO costs VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)',
        [
            ("2026-09-01", "USD", "sub-a", "benefit-a", "Plan A", "Reservation",
             "Virtual Machines", "rg-a", "eastus", "Usage", "/vm/a", "vm-a", 10),
            ("2026-09-01", "USD", "sub-a", "benefit-a", "Plan A", "Reservation",
             "Virtual Machines", "rg-a", "eastus", "Usage", "/vm/a", "vm-a", 5),
            ("2026-09-02", "USD", "sub-a", None, None, "OnDemand",
             "Storage", "rg-a", "eastus", "Usage", "/disk/a", "disk-a", 7),
            ("2026-09-02", "EUR", "sub-b", None, "Named benefit", "Reservation",
             "Virtual Machines", "rg-b", "westeurope", "Usage", "/vm/b", "vm-b", 3),
        ],
    )

    rows = con.execute(commitment_benefits.select_sql("costs")).fetchall()

    assert len(rows) == 2
    assert sum(row[-1] for row in rows) == pytest.approx(18)
    assert all(row[3] or row[4] for row in rows)
    assert '"ResourceId"' in commitment_benefits.select_sql("costs")
    assert "GROUP BY" in commitment_benefits.select_sql(
        "costs", (date(2026, 9, 1), date(2026, 10, 1)))


def test_spot_rate_rollup_keeps_meter_unit_scope_and_positive_quantity():
    con = sqlite3.connect(":memory:")
    con.execute("""CREATE TABLE costs (
        "ChargePeriodStart" TEXT, "SubAccountId" TEXT, "MeterName" TEXT,
        "PricingModel" TEXT, "BillingCurrency" TEXT, "RegionName" TEXT,
        "UnitOfMeasure" TEXT, "BilledCost" DOUBLE PRECISION,
        "PricingQuantity" DOUBLE PRECISION
    )""")
    con.executemany(
        'INSERT INTO costs VALUES (?,?,?,?,?,?,?,?,?)',
        [
            ("2026-09-01", "sub-a", "D2", "Spot", "USD", "eastus", "Hour", 3, 10),
            ("2026-09-01", "sub-b", "D2", "Spot", "USD", "eastus", "Hour", 2, 5),
            ("2026-09-01", "sub-a", "D2", "Spot", "USD", "eastus", "Hour", 9, 0),
        ],
    )

    rows = con.execute(spot_rates.select_sql("costs")).fetchall()

    assert len(rows) == 2
    assert sum(row[-2] for row in rows) == pytest.approx(5)
    assert sum(row[-1] for row in rows) == pytest.approx(15)
    assert all(name in spot_rates.select_sql("costs") for name in (
        '"MeterName"', '"UnitOfMeasure"', '"SubAccountId"'))
