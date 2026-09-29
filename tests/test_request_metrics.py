"""Privacy-safe server timing for API requests."""
from __future__ import annotations

import asyncio
import json
import logging
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
from fastapi import FastAPI, Request, Response

from app import main


def test_api_timing_uses_route_template_and_omits_query_values(monkeypatch, caplog):
    app = FastAPI()
    app.add_middleware(main.CacheHeaders)

    @app.get("/api/subscriptions/{subscription_id}")
    async def get_subscription(subscription_id: str, request: Request):
        request.state.overview_timings = {
            "warehouse_summary": 12.3, "subscriptions": 4.5, "untrusted": 99.0,
        }
        return {"ok": bool(subscription_id)}

    async def request():
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test",
        ) as client:
            return await client.get(
                "/api/subscriptions/private-id?token=not-for-logs",
            )

    monkeypatch.setattr(main, "SLOW_API_LOG_MS", 0)
    caplog.set_level(logging.WARNING, logger="cloudlens")
    response = asyncio.run(request())

    assert response.status_code == 200
    assert response.headers["Server-Timing"].startswith("app;dur=")
    assert "overview_warehouse_summary;dur=12.3" in response.headers["Server-Timing"]
    assert "overview_subscriptions;dur=4.5" in response.headers["Server-Timing"]
    assert "overview_untrusted" not in response.headers["Server-Timing"]
    assert "route=/api/subscriptions/{subscription_id}" in caplog.text
    assert "private-id" not in caplog.text
    assert "not-for-logs" not in caplog.text


def test_phase_timing_and_error_marker_survive_response_middleware():
    app = FastAPI()
    app.add_middleware(main.CacheHeaders)

    @app.get("/api/overview-test")
    async def get_overview_test(request: Request):
        request.state.overview_timings = {"budgets": 7.2}
        return Response(status_code=200, headers={
            "Server-Timing": 'overview_budgets;dur=7.2;desc="error"',
        })

    async def request():
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test",
        ) as client:
            return await client.get("/api/overview-test")

    response = asyncio.run(request())
    assert response.status_code == 200
    assert "app;dur=" in response.headers["Server-Timing"]
    assert 'overview_budgets;dur=7.2;desc="error"' in response.headers["Server-Timing"]
    assert response.headers["Server-Timing"].count("overview_budgets;") == 1


def test_mixed_native_overview_totals_convert_each_currency_separately(monkeypatch):
    from app import cost, currency

    user = SimpleNamespace(admin=True, source="local", name="test", sid="test")
    monkeypatch.setattr(main, "current_user", lambda _request: user)
    monkeypatch.setattr(main, "permitted", AsyncMock(return_value=["sub-a", "sub-b"]))
    monkeypatch.setattr(main, "narrow", lambda requested, _allowed: requested)

    @asynccontextmanager
    async def acting_as(_user):
        yield

    monkeypatch.setattr(main, "as_user", acting_as)
    monkeypatch.setattr(
        currency, "rate_to",
        lambda code: {"USD": 1.0, "INR": 80.0}.get(code.upper()),
    )
    native = {
        "month_to_date": None,
        "last_month": None,
        "month_to_date_by_currency": [
            {"currency": "INR", "cost": 1000.0},
            {"currency": "USD", "cost": 10.0},
        ],
        "last_month_by_currency": [
            {"currency": "INR", "cost": 2000.0},
            {"currency": "USD", "cost": 20.0},
        ],
        "currency": None,
        "currency_note": "As billed, totals are separate.",
        "budget": None,
    }
    monkeypatch.setattr(cost, "overview", AsyncMock(return_value=native))
    result = asyncio.run(main.overview(
        SimpleNamespace(state=SimpleNamespace()), scope="sub-a,sub-b", currency="USD"))

    assert result.status_code == 200
    body = json.loads(result.body)
    assert body["month_to_date"] == 22.5
    assert body["last_month"] == 45.0
    assert body["currency"] == "USD"
    assert body["currency_note"] is None
    assert body["month_to_date_by_currency"] == []


def test_converted_overview_does_not_double_convert_mixed_currency_totals():
    from app import currency

    source = {
        "month_to_date": None,
        "month_to_date_by_currency": [
            {"currency": "INR", "cost": 1000},
            {"currency": "USD", "cost": 10},
        ],
        "currency": None,
        "currency_note": "As billed, totals are separated.",
    }
    response = currency.convert_money(
        {key: value for key, value in source.items()
         if key not in {"month_to_date_by_currency", "last_month_by_currency"}},
        "USD",
    )
    assert response["month_to_date"] is None
    assert response["currency_note"] == source["currency_note"]
