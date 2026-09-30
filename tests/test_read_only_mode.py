"""Offline staging safeguards: local cache work is allowed, ingest and Azure writes are not."""
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import httpx
import pytest
from fastapi import FastAPI

from tests.test_focus_schedules import offline_modules  # noqa: F401

pytestmark = pytest.mark.offline


@pytest.mark.parametrize("method,path", [
    (method, path)
    for method in ("POST", "PUT", "PATCH", "DELETE")
    for path in ("/api/schedules/run", "/api/ingest", "/api/ask",
                 "/api/focus/preview", "/api/focus/daily/preview", "/api/cost-data/setup",
                 "/api/cost-data/preview", "/healthz")
    if not (method == "POST" and path in {"/api/focus/preview", "/api/focus/daily/preview", "/api/cost-data/preview"})
])
def test_read_only_blocks_mutating_http_before_auth_or_handlers(monkeypatch, path, method):
    from app import main
    monkeypatch.setenv("CLOUDLENS_READ_ONLY", "true")
    auth = Mock(side_effect=AssertionError("A refused mutation must not reach auth or a handler"))
    monkeypatch.setattr(main, "auth_service", auth)
    app = FastAPI()
    app.add_middleware(main.RequireAuth)
    async def request():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://offline") as client:
            return await client.request(method, path)
    response = asyncio.run(request())
    assert response.status_code == 403 and response.json()["read_only"] is True
    auth.assert_not_called()


@pytest.mark.parametrize("path,method", [("/api/auth/login", "POST"), ("/api/auth/logout", "POST"),
                                       ("/api/focus/preview", "POST"),
                                       ("/api/focus/daily/preview", "POST"),
                                       ("/api/cost-data/preview", "POST"),
                                       ("/api/cost-data/subscriptions", "GET"),
                                       ("/api/status", "GET")])
def test_read_only_keeps_auth_pure_previews_and_authenticated_reads(monkeypatch, path, method):
    from app import main
    monkeypatch.setenv("CLOUDLENS_READ_ONLY", "true")
    monkeypatch.setattr(main, "auth_service", lambda: SimpleNamespace(identify=lambda *args: object()))
    app = FastAPI()
    app.add_middleware(main.RequireAuth)
    app.add_api_route(path, lambda: {"ok": True}, methods=[method])
    async def request():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://offline") as client:
            return await client.request(method, path)
    assert asyncio.run(request()).status_code == 200


def test_default_mode_keeps_existing_write_behavior(monkeypatch):
    from app import main
    monkeypatch.delenv("CLOUDLENS_READ_ONLY", raising=False)
    monkeypatch.setattr(main, "auth_service", lambda: SimpleNamespace(identify=lambda *args: object()))
    app = FastAPI()
    app.add_middleware(main.RequireAuth)
    app.add_api_route("/api/example", lambda: {"ok": True}, methods=["POST"])
    async def request():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://offline") as client:
            return await client.post("/api/example")
    assert not main.read_only_mode()
    assert asyncio.run(request()).status_code == 200


def test_read_only_blocks_all_worker_entry_points_before_database_or_network(monkeypatch):
    from fastapi import HTTPException

    from app import (
        backfill,
        exports,
        focus_sync,
        focus_workflow,
        ingest_jobs,
        jobs,
        main,
        onboarding,
        warehouse,
    )
    monkeypatch.setenv("CLOUDLENS_READ_ONLY", "true")
    forbidden = Mock(side_effect=AssertionError("Read-only staging must not start an ingest"))
    monkeypatch.setattr(warehouse.warehouse, "connect", forbidden)
    record = {"job_id": "copied-production-job", "status": "queued"}
    async def check():
        await main._daily_refresh()
        await main._focus_refresh_loop()
        await main._recover_ingest_jobs()
        await ingest_jobs.recover()
        await focus_workflow.recover()
        assert await focus_workflow.daily_tick() == set()
        assert not await onboarding.start([{"id": "sub-a"}])
        focus_workflow.launch(record)
        backfill.schedule([{"id": "sub-a"}])
        assert not backfill.status_for([{"id": "sub-a"}])["loading"]
        with pytest.raises(PermissionError, match="CLOUDLENS_READ_ONLY"):
            await jobs.run(record["job_id"], forbidden)
        with pytest.raises(PermissionError, match="CLOUDLENS_READ_ONLY"):
            jobs.spawn(record, forbidden)
        with pytest.raises(PermissionError, match="CLOUDLENS_READ_ONLY"):
            jobs.create("automatic", ["sub-a"])
        with pytest.raises(PermissionError, match="CLOUDLENS_READ_ONLY"):
            await focus_workflow.step(record["job_id"])
        with pytest.raises(PermissionError, match="CLOUDLENS_READ_ONLY"):
            await focus_sync.run(["sub-a"], request_new=True)
        with pytest.raises(PermissionError, match="CLOUDLENS_READ_ONLY"):
            await exports.ingest_export(account="offline", container="focus")
        with pytest.raises(HTTPException) as raised:
            await main._start_ingest_task(forbidden, scope=["sub-a"])
        assert raised.value.status_code == 403
    asyncio.run(check())
    forbidden.assert_not_called()


def test_read_only_schedule_adapter_allows_reads_not_azure_writes(monkeypatch):
    from app import cost, schedules
    monkeypatch.setenv("CLOUDLENS_READ_ONLY", "true")
    request = AsyncMock(return_value={"read": True})
    monkeypatch.setattr(cost, "azure", SimpleNamespace(request=request))
    async def check():
        for method in ("POST", "PUT", "PATCH", "DELETE"):
            with pytest.raises(PermissionError, match="CLOUDLENS_READ_ONLY"):
                await schedules._request(method, "https://offline.invalid/export")
        assert await schedules._request("GET", "https://offline.invalid/export") == {"read": True}
    asyncio.run(check())
    assert request.await_count == 1


def test_read_only_lifespan_starts_caches_but_no_ingest_workers(monkeypatch):
    from app import azure_updates, currency, main, service_health, warehouse
    monkeypatch.setenv("CLOUDLENS_READ_ONLY", "true")
    monkeypatch.setenv("PROJECT_ENDPOINT", "https://offline.invalid")
    monkeypatch.setenv("AUTO_INGEST", "true")
    monkeypatch.setenv("AUTO_FOCUS_SYNC", "true")
    monkeypatch.setenv("AUTO_REFRESH", "true")
    monkeypatch.setenv("SNAPSHOT_CACHE_ENABLED", "true")
    wh = warehouse.warehouse
    monkeypatch.setattr(wh, "_store", SimpleNamespace(close=Mock()), raising=False)
    monkeypatch.setattr(wh, "check_readiness", lambda: True)
    summary = Mock(return_value={"rows": 0})
    monkeypatch.setattr(wh, "summary", summary)
    monkeypatch.setattr(main, "auth_service", lambda: SimpleNamespace(sso=SimpleNamespace(enabled=False)))
    monkeypatch.setattr(main, "CostAgent", lambda: SimpleNamespace(model="offline", close=AsyncMock()))
    cache = SimpleNamespace(start=AsyncMock(), close=AsyncMock())
    monkeypatch.setattr(main, "SnapshotStore", lambda *args, **kwargs: None)
    monkeypatch.setattr(main, "SnapshotCache", lambda *args, **kwargs: cache)
    monkeypatch.setattr(main, "application_version", lambda **kwargs: "offline")
    monkeypatch.setattr(main, "_prepare_snapshot_generation", AsyncMock())
    monkeypatch.setattr(main, "_refresh_overview", AsyncMock())
    monkeypatch.setattr(currency, "refresh", AsyncMock())
    monkeypatch.setattr(azure_updates._public_cache, "close", AsyncMock())
    monkeypatch.setattr(service_health._cache, "close", AsyncMock())
    started = []
    create_task = asyncio.create_task
    def track(coroutine, **kwargs):
        started.append(kwargs.get("name"))
        return create_task(coroutine, **kwargs)
    monkeypatch.setattr(main.asyncio, "create_task", track)
    async def start():
        async with main.lifespan(FastAPI()):
            assert not {"daily-refresh", "focus-sync", "ingest-recovery"} & set(started)
            assert "snapshot-bootstrap" in started
            summary.assert_not_called()
    asyncio.run(start())
    cache.start.assert_awaited_once()
    cache.close.assert_awaited_once()
