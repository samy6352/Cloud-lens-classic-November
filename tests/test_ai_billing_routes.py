"""AI billing keeps caller identity, selected scopes and historical reporting context."""
import asyncio
import json
import sys
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from fastapi import HTTPException

from tests.test_focus_schedules import offline_modules  # noqa: F401


@pytest.fixture
def api(monkeypatch):
    from app import cost, main

    user = SimpleNamespace(source="entra", name="Viewer")
    request = SimpleNamespace(state=SimpleNamespace(user=user), query_params={"service": "Foundry Models"})
    report = AsyncMock(return_value={"status": "ok", "services": [], "models": [], "totals": []})
    monkeypatch.setitem(sys.modules, "app.ai_billing", SimpleNamespace(report=report))
    monkeypatch.setattr(main, "permitted", AsyncMock(return_value=["sub-a"]))
    calls = []

    @asynccontextmanager
    async def caller(who):
        calls.append(who)
        yield

    monkeypatch.setattr(main, "as_user", caller)
    return SimpleNamespace(main=main, cost=cost, report=report, request=request, user=user, calls=calls)


def test_route_preserves_permitted_scope_currency_and_filters(api):
    result = asyncio.run(api.main.dashboard_ai_billing(api.request, days=60, scope="sub-a", currency="EUR"))
    assert result.status_code == 200
    assert result.headers["cache-control"] == "private, no-store"
    assert json.loads(result.body)["status"] == "ok"
    api.report.assert_awaited_once_with(["sub-a"], days=60, currency="EUR", filters={"service": ["Foundry Models"]})
    assert api.calls == [api.user]


def test_no_scope_uses_entitlement_not_other_subscriptions(api):
    asyncio.run(api.main.dashboard_ai_billing(api.request))
    assert api.report.await_args.args == (["sub-a"],)


def test_forbidden_scope_never_calls_the_report(api):
    with pytest.raises(HTTPException) as error:
        asyncio.run(api.main.dashboard_ai_billing(api.request, scope="sub-b"))
    assert error.value.status_code == 403
    api.report.assert_not_awaited()


def test_local_unrestricted_user_resolves_explicit_visible_scopes(api, monkeypatch):
    monkeypatch.setattr(api.main, "permitted", AsyncMock(return_value=None))
    listing = AsyncMock(return_value={"subscriptions": [{"id": "sub-a"}, {"id": "sub-b"}]})
    monkeypatch.setattr(api.cost, "list_subscriptions", listing)
    asyncio.run(api.main.dashboard_ai_billing(api.request, days=500))
    assert api.report.await_args.args == (["sub-a", "sub-b"],)
    assert api.report.await_args.kwargs["days"] == 365
    listing.assert_awaited_once()


@pytest.fixture
def snapshot_auth(api, monkeypatch):
    api.user.admin, api.user.sid = False, "test-session"
    api.request.url = SimpleNamespace(path="/api/dashboard/ai-billing")
    session = SimpleNamespace(tenant="test-tenant", oid="test-reader",
                              reauth_required=False, subscriptions=[])
    sso = SimpleNamespace(delegated=True, get=lambda sid: session)
    monkeypatch.setattr(api.main, "auth_service", lambda: SimpleNamespace(sso=sso))
    monkeypatch.setattr(api.main, "read_only_mode", lambda: True)
    monkeypatch.setenv("FX_RATES", "")
    monkeypatch.setenv("AZURE_CLIENT_ID", "")
    api.day = Mock(return_value="2026-09-14")
    monkeypatch.setattr(sys.modules["app.ai_billing"], "snapshot_data_day", api.day, raising=False)
    return api


def test_snapshot_authorizer_passes_effective_scope_filters_and_caller(snapshot_auth):
    api = snapshot_auth
    context = asyncio.run(api.main.authorize_snapshot(api.request))
    assert context.selected == context.allowed == ("sub-a",)
    assert context.principal == "test-reader" and context.tenant == "test-tenant"
    assert context.data_day == "2026-09-14"
    assert context.period_version == "ai-rolling-data-day-v1"
    api.day.assert_called_once_with(["sub-a"], {"service": ["Foundry Models"]})
    assert api.calls == [api.user]


def test_ai_snapshot_authorizer_is_not_invalidated_by_midnight(snapshot_auth, monkeypatch):
    api = snapshot_auth
    day = [15]

    class Clock:
        @staticmethod
        def now(tz):
            return datetime(2026, 9, day[0], tzinfo=UTC)

    monkeypatch.setattr(api.main, "datetime", Clock)
    first = asyncio.run(api.main.authorize_snapshot(api.request))
    day[0] += 1
    second = asyncio.run(api.main.authorize_snapshot(api.request))
    assert first == second
    api.request.query_params["period_month"] = "2026-08"
    assert asyncio.run(api.main.authorize_snapshot(api.request)).period_version == "2026-08"
    api.request.query_params.pop("period_month")
    api.request.url.path = "/api/dashboard/billing"
    other = asyncio.run(api.main.authorize_snapshot(api.request))
    assert other.period_version == "2026-09-16" and other.data_day == ""
    assert api.day.call_count == 3  # Other dashboard authorizers do no daily source lookup.


def test_ai_snapshot_authorizer_resolves_unrestricted_scope_before_day_lookup(snapshot_auth, monkeypatch):
    api = snapshot_auth
    monkeypatch.setattr(api.main, "permitted", AsyncMock(return_value=None))
    listing = AsyncMock(return_value={"subscriptions": [{"id": "sub-a"}, {"id": "sub-b"}]})
    monkeypatch.setattr(api.cost, "list_subscriptions", listing)
    context = asyncio.run(api.main.authorize_snapshot(api.request))
    assert context.allowed is None and context.selected == ("sub-a", "sub-b")
    api.day.assert_called_once_with(["sub-a", "sub-b"], {"service": ["Foundry Models"]})
    listing.assert_awaited_once()


def test_ai_snapshot_authorizer_rejects_forbidden_scope_before_day_lookup(snapshot_auth):
    api = snapshot_auth
    api.request.query_params["scope"] = "sub-b"
    with pytest.raises(HTTPException) as exc:
        asyncio.run(api.main.authorize_snapshot(api.request))
    assert exc.value.status_code == 403
    api.day.assert_not_called()
