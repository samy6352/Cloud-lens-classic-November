"""Where a rolling window ends when subscriptions were loaded to different days.

One subscription quick-refreshed past the rest of the estate used to move every view's
window to its own newest day, so every other subscription showed those days as zero.
"""
from __future__ import annotations

from datetime import date, timedelta

import pytest

from app import reporting

BIG_1 = "aaaaaaaa-0000-0000-0000-000000000001"
BIG_2 = "aaaaaaaa-0000-0000-0000-000000000002"
FRESH = "aaaaaaaa-0000-0000-0000-000000000003"
QUIET = "aaaaaaaa-0000-0000-0000-000000000004"


@pytest.fixture(autouse=True)
def clock(monkeypatch):
    monkeypatch.setattr(reporting, "today", lambda: date(2026, 10, 1))


def _days(first: str, last: str) -> list[str]:
    lo, hi = date.fromisoformat(first), date.fromisoformat(last)
    return [(lo + timedelta(days=i)).isoformat() for i in range((hi - lo).days + 1)]


@pytest.fixture(scope="module")
def database(module_postgres_warehouse):
    from app.warehouse import COLUMNS, insert_rows

    wh = module_postgres_warehouse
    plan = ([(d, 100, BIG_1) for d in _days("2026-09-01", "2026-09-28")]
            + [(d, 80, BIG_2) for d in _days("2026-09-01", "2026-09-28")]
            # Small, and loaded three days further than everyone else.
            + [(d, 5, FRESH) for d in _days("2026-09-01", "2026-10-01")]
            # Deleted in August: no recent spend, so it must not hold the window back.
            + [(d, 500, QUIET) for d in _days("2026-08-01", "2026-08-15")])
    records = []
    for day, amount, sub in plan:
        row = {
            "ChargePeriodStart": day, "BilledCost": amount, "CostInUsd": amount,
            "InvoiceCost": amount, "EffectiveCost": amount,
            "BillingCostBasis": "FOCUS", "BillingCurrency": "USD",
            "SubAccountId": sub, "SubAccountName": sub, "ResourceName": f"vm-{sub[-1]}",
            "ResourceId": f"/subscriptions/{sub}/resourceGroups/rg/providers/"
                          f"Microsoft.Compute/virtualMachines/vm-{sub[-1]}",
            "ResourceGroup": "rg", "ServiceFamily": "Compute", "ServiceName": "Virtual Machines",
            "RegionName": "eastus", "ChargeCategory": "Usage", "PricingModel": "OnDemand",
            "PricingQuantity": 1,
        }
        records.append([row.get(column) for column in COLUMNS])
    with wh.connect() as con:
        insert_rows(con, records)
    return wh


def _edge(wh, scope):
    with wh.reader(scope) as r:
        return str(reporting.edge(r))[:10]


@pytest.mark.postgres
def test_window_ends_where_most_recent_spend_has_reached(database):
    everyone = [BIG_1, BIG_2, FRESH, QUIET]
    assert _edge(database, everyone) == "2026-09-28"
    # On its own, a subscription still gets its own newest day.
    assert _edge(database, [FRESH]) == "2026-10-01"
    # A deleted subscription carries no recent spend and cannot drag the window back.
    assert _edge(database, [BIG_1, QUIET]) == "2026-09-28"
    # When the fresh one is most of the recent spend, it sets the day.
    assert _edge(database, [FRESH, QUIET]) == "2026-10-01"


@pytest.mark.postgres
def test_views_stop_at_the_window_end(database):
    from app.dashboard import Dashboard

    everyone = [BIG_1, BIG_2, FRESH, QUIET]
    sections = Dashboard().sections(everyone, days=7)
    # 22-28 Sep: seven days of 100 + 80 + 5; the fresh one's 29 Sep - 1 Oct are left out.
    assert sections["total"] == 7 * 185
    with database.reader(everyone) as r:
        hi, _, _ = reporting.window(r, 7)
    assert hi == "2026-09-28"
    executive = Dashboard().executive(everyone, days=7)
    labels = [str(d)[:10] for d in executive["trend"]["labels"]]
    assert labels and max(labels) == "2026-09-28"


@pytest.mark.postgres
def test_a_chosen_month_still_ends_with_the_month(database):
    from app.dashboard import Dashboard

    with reporting.select("2026-10"):
        assert Dashboard().sections([BIG_1, BIG_2, FRESH], days=7)["total"] == 5
