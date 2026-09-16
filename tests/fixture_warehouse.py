"""A small, deterministic warehouse for tests that need one.

Six of the tests in this suite read a populated warehouse rather than building
their own: they check that scoping narrows a total, that the overview reconciles
with the detail, that an export carries what the dashboard showed. Until now they
read whatever warehouse happened to be on the machine that ran them.
That made them pass on the author's laptop and fail everywhere else, and it meant
a failure could be either a real regression or simply a different month of data.

So the data is defined here instead: fixed subscriptions, fixed days, fixed costs,
seeded through the application's own writer so the schema can never drift from the
one the app creates.

Deliberate properties, because tests depend on them:

* Three subscriptions with *different* totals, so a scoped query that quietly
  ignored its scope would return a number no single subscription can explain.
* Costs that are exact in binary (halves and quarters), so sums compare equal
  without a tolerance argument about floating point.
* Some resources tagged and some not, so "untagged" is a real set rather than an
  empty one, and at least one tag key carries more than one value.
* One resource group per subscription plus one shared name, so grouping by
  resource group cannot be mistaken for grouping by subscription.
"""
from __future__ import annotations

import json
import sys
from datetime import date, timedelta
from pathlib import Path
from typing import Any

# Importable both as `tests.fixture_warehouse` under pytest and as a plain script.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

DAYS = 60

# Fixed identifiers. Not real subscriptions, and obviously not real: a GUID-shaped
# string here would invite someone to go looking for the subscription it names.
SUBSCRIPTIONS: list[dict[str, Any]] = [
    {"id": "00000000-0000-0000-0000-00000000000a", "name": "Contoso Production",
     "daily": 16.00, "region": "eastus"},
    {"id": "00000000-0000-0000-0000-00000000000b", "name": "Contoso Staging",
     "daily": 8.00, "region": "westeurope"},
    {"id": "00000000-0000-0000-0000-00000000000c", "name": "Contoso Sandbox",
     "daily": 4.00, "region": "centralindia"},
]

# (service, family, share of the subscription's daily spend). Shares are exact
# binary fractions and sum to 1.
SERVICES: list[tuple[str, str, float]] = [
    ("Virtual Machines", "Compute", 0.50),
    ("Storage", "Storage", 0.25),
    ("SQL Database", "Databases", 0.125),
    ("Bandwidth", "Networking", 0.125),
]

# Tags on the first two resources of each subscription only, so the third is
# genuinely untagged. "env" carries more than one value across the estate.
TAGS: list[dict[str, str] | None] = [
    {"env": "prod", "owner": "platform"},
    {"env": "test", "owner": "platform", "cost-centre": "cc-1000"},
    None,
]


def rows(start: date | None = None) -> list[list[Any]]:
    """Every cost row in the fixture, in `COLUMNS` order."""
    from app.warehouse import COLUMNS, NUMERIC

    start = start or date(2026, 1, 1)
    out: list[list[Any]] = []
    for sub in SUBSCRIPTIONS:
        for day in range(DAYS):
            when = (start + timedelta(days=day)).isoformat()
            for index, (service, family, share) in enumerate(SERVICES):
                cost = round(sub["daily"] * share, 4)
                tags = TAGS[index % len(TAGS)]
                # One shared group name across subscriptions, one private: grouping
                # by resource group must not collapse to grouping by subscription.
                group = "shared-rg" if index == 0 else f"rg-{sub['name'].split()[-1].lower()}"
                resource = f"/subscriptions/{sub['id']}/resourceGroups/{group}" \
                           f"/providers/Microsoft.Test/items/item{index}"
                values: dict[str, Any] = {
                    "ChargePeriodStart": when,
                    "BilledCost": cost,
                    "CostInUsd": cost,
                    "BillingCurrency": "USD",
                    "SubAccountId": sub["id"],
                    "SubAccountName": sub["name"],
                    "ResourceGroup": group,
                    "ResourceId": resource,
                    "ResourceName": f"item{index}",
                    "ServiceName": service,
                    "ServiceFamily": family,
                    "ServiceSubcategory": f"{service} standard",
                    "MeterName": f"{service} meter",
                    "ProductName": f"{service} product",
                    "RegionName": sub["region"],
                    "PricingQuantity": 1.0,
                    "UnitOfMeasure": "1 Hour",
                    "UnitPrice": cost,
                    "ChargeCategory": "Usage",
                    "PricingModel": "OnDemand",
                    "PublisherName": "Microsoft",
                    "Tags": json.dumps(tags) if tags else None,
                }
                out.append([values.get(c, 0.0 if c in NUMERIC else None) for c in COLUMNS])
    return out


def seed_current() -> None:
    """Fill the warehouse the environment describes with the fixture rows.

    The destination is the server and schema named in the environment, so the warehouse
    is taken as configured rather than constructed from a path.
    """
    from app.warehouse import insert_rows, warehouse

    with warehouse.connect() as con:
        insert_rows(con, rows())


def expected_total() -> float:
    """What the whole fixture costs, computed the same way the rows are built."""
    return round(sum(round(s["daily"] * share, 4) * DAYS
                     for s in SUBSCRIPTIONS for _, _, share in SERVICES), 2)


if __name__ == "__main__":  # A quick way to eyeball the fixture.
    seed_current()
    from app.warehouse import warehouse as wh

    print(f"{wh.row_count()} rows, total {expected_total()}")
    for s in wh.subscriptions():
        print(f"  {s['name']:<22} {s['cost']:>10,.2f}  {s['id']}")
