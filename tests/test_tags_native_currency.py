"""Tag budgets retain subscription identity and native units under display conversion."""
import uuid

import pytest


@pytest.fixture
def tag_warehouse(monkeypatch, postgres_warehouse):
    monkeypatch.setenv("FX_RATES", '{"EUR":0.5}')
    return postgres_warehouse


@pytest.mark.postgres
def test_native_currency_and_subscription_identity_preserve_tag_costs(tag_warehouse):
    from app.tags import cost_by_tag
    from app.warehouse import COLUMNS, NUMERIC, insert_rows

    subs = [str(uuid.UUID(int=i)) for i in range(1, 5)]
    records = [
        (subs[0], "same-non-arm-name", "USD", 20),
        (subs[1], "same-non-arm-name", "USD", 10),
        (subs[1], "/subscriptions/" + subs[1] + "/resourceGroups/rg/providers/test/vm", "USD", 100),
        (subs[2], "mixed", "USD", 30),
        (subs[2], "mixed", "EUR", 40),
        (subs[3], "unknown", "", 5),
    ]
    rows = []
    for sub, resource, currency, cost in records:
        values = {
            "SubAccountId": sub, "SubAccountName": "Shared",
            "ResourceId": resource, "ResourceName": resource,
            "ChargePeriodStart": "2026-09-01", "BillingCurrency": currency,
            "BilledCost": cost, "CostInUsd": cost, "Tags": '{"Owner":"Ana"}',
        }
        rows.append([values.get(c, 0.0 if c in NUMERIC else None) for c in COLUMNS])
    with tag_warehouse.connect() as con:
        insert_rows(con, rows)

    data = cost_by_tag(scope=subs[:3], currency="EUR")
    assert data["currency"] == "EUR"
    assert data["native_currencies"] == {
        subs[0]: ["USD"], subs[1]: ["USD"], subs[2]: ["EUR", "USD"],
    }
    by_id = {r["id"]: r for r in data["resources"]}
    assert by_id["same-non-arm-name"]["subscription_id"] == "", "ambiguous aggregated identity stays unknown"
    assert by_id[records[2][1]]["subscription_id"] == subs[1]
    assert by_id[records[2][1]]["subscription"] == "Shared"
    assert data["total"] == 120
    assert data["tagged"] == {"cost": 120, "resources": 3}
    assert sum(data["daily_total"]) == 120
    assert sum(sum(cost for _, cost in r["d"]) for r in data["resources"]) == 120
    assert data["keys"][0]["cost"] == 120
    assert data["values"] == ["Ana"]

    unknown = cost_by_tag(scope=[subs[3]])
    assert unknown["native_currencies"] == {subs[3]: ["unknown"]}
    assert unknown["resources"][0]["subscription_id"] == subs[3]
    assert cost_by_tag(scope=[])["native_currencies"] == {}
