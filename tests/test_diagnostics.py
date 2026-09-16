"""`/api/diagnostics` has to survive being serialised.

The endpoint reports whether the warehouse has picked up duplicate rows — the check that
guards against "the worst bug this warehouse has had", where one subscription stored under
two spellings is counted twice and the total stays plausible while it happens.

That figure is `sum()` over `count(*)`, and PostgreSQL widens an integer sum to `numeric`,
which psycopg returns as `Decimal`. `Decimal` is not JSON serialisable, so the endpoint
answered 500 — on every estate, duplicates or not, because `coalesce(sum(...), 0)` is
`numeric` even when it matches nothing. The one number that exists to make a silent
doubling visible was the one number that made the page impossible to open.

Nothing else covered this route, which is why it stayed broken.
"""
from __future__ import annotations

import json
from datetime import date
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from app import main
from app.auth import User


@pytest.fixture
def admin_client(monkeypatch):
    """A signed-in admin, without standing up Entra or a password file.

    Constructed without the context manager on purpose: entering it would run the app
    lifespan, which builds the agent and wants PROJECT_ENDPOINT. This route needs neither.
    """
    admin = User(name="diagnostics-test", admin=True)
    monkeypatch.setattr(main, "auth_service",
                        lambda: SimpleNamespace(identify=lambda *_: admin))
    client = TestClient(main.app)
    try:
        yield client
    finally:
        client.close()


def _add_costs(warehouse, rows):
    from app.warehouse import COLUMNS, insert_rows

    with warehouse.connect() as connection:
        insert_rows(connection, [[row.get(column) for column in COLUMNS] for row in rows])


def _cost_row(**overrides):
    row = {
        "ChargePeriodStart": date(2026, 3, 1),
        "SubAccountId": "00000000-0000-0000-0000-00000000000a",
        "SubAccountName": "Contoso Production",
        "ServiceName": "Virtual Machines",
        "ResourceId": "/subscriptions/a/resourceGroups/rg/providers/vm/one",
        "BilledCost": 4.0,
        "EffectiveCost": 4.0,
        "BillingCurrency": "USD",
    }
    row.update(overrides)
    return row


@pytest.mark.postgres
def test_diagnostics_is_serialisable_and_counts_duplicates_as_a_whole_number(
    postgres_warehouse, admin_client
):
    # Two identical rows are one duplicate: the group holds n=2, so n-1 == 1.
    _add_costs(postgres_warehouse, [_cost_row(), _cost_row()])

    response = admin_client.get("/api/diagnostics")

    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["duplicate_rows"] == 1
    assert isinstance(payload["duplicate_rows"], int) and not isinstance(
        payload["duplicate_rows"], bool)
    # The whole document, not just the field that broke: any other numeric that comes back
    # as Decimal would fail the response the same way, and silently until someone opens it.
    json.dumps(payload)


@pytest.mark.postgres
def test_diagnostics_reports_a_clean_warehouse_as_zero_rather_than_failing(
    postgres_warehouse, admin_client
):
    """An empty group still produces `coalesce(sum(...), 0)`, which was Decimal too."""
    _add_costs(postgres_warehouse, [_cost_row(), _cost_row(ResourceId="/two", BilledCost=9.0)])

    payload = admin_client.get("/api/diagnostics").json()

    assert payload["duplicate_rows"] == 0
    assert payload["duplicate_cost"] == 0
    json.dumps(payload)


@pytest.mark.postgres
def test_diagnostics_stays_admin_only(postgres_warehouse, monkeypatch):
    reader = User(name="reader", admin=False)
    monkeypatch.setattr(main, "auth_service",
                        lambda: SimpleNamespace(identify=lambda *_: reader))
    client = TestClient(main.app)
    try:
        assert client.get("/api/diagnostics").status_code == 403
    finally:
        client.close()
