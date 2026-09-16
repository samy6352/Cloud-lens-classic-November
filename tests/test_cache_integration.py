"""Application wiring for authorized snapshots and read-only staging."""
from __future__ import annotations

import asyncio
import sys
from types import ModuleType, SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi.responses import JSONResponse
from starlette.testclient import TestClient

from app import capabilities, cost, main
from app.auth import User

pytestmark = pytest.mark.offline
SUB = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
OTHER = "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"
TENANT = "cccccccc-cccc-cccc-cccc-cccccccccccc"
OID = "dddddddd-dddd-dddd-dddd-dddddddddddd"


@pytest.fixture
def web(monkeypatch):
    user = User("Reader", admin=True, source="entra", email="reader@example.invalid", sid="session")
    session = SimpleNamespace(
        oid=OID, tenant=TENANT, reauth_required=False,
        subscriptions=[{"id": SUB, "tenant": TENANT}],
    )
    state = SimpleNamespace(user=user, session=session, allowed=[SUB])
    caller = cost.Caller(fallback="synthetic", by_subscription={},
                         subscriptions=[{"id": SUB, "name": "Synthetic", "tenant": TENANT}])
    auth = SimpleNamespace(
        identify=lambda *_args: state.user,
        sso=SimpleNamespace(
            delegated=True, enabled=False, get=lambda _sid: state.session,
            caller=AsyncMock(return_value=caller)),
    )

    async def permitted(_user):
        return state.allowed

    monkeypatch.setattr(main, "_auth", auth)
    monkeypatch.setattr(main, "_snapshot_cache", None)
    monkeypatch.setattr(main, "_log_access", lambda *_args: None)
    monkeypatch.setattr(main, "permitted", permitted)
    monkeypatch.setattr(cost, "_resolve", AsyncMock(return_value=caller.subscriptions))
    monkeypatch.delenv("CLOUDLENS_READ_ONLY", raising=False)
    monkeypatch.delenv("FX_RATES", raising=False)
    state.client = TestClient(main.app)
    yield state
    state.client.close()


class HitCache:
    def __init__(self):
        self.contexts = []

    async def get(self, request, _origin, authorize):
        context = await authorize(request)
        self.contexts.append(context)
        return JSONResponse({"cached": True, "scope": context.selected})


def test_cached_endpoint_still_authorizes_current_scope(web, monkeypatch):
    cache = HitCache()
    monkeypatch.setattr(main, "_snapshot_cache", cache)
    assert web.client.get("/api/dashboard/executive", params={"scope": SUB}).status_code == 200
    assert cache.contexts[-1].principal == OID
    assert cache.contexts[-1].tenant == TENANT
    forbidden = web.client.get("/api/dashboard/executive", params={"scope": OTHER})
    assert forbidden.status_code == 403
    assert len(cache.contexts) == 1
    web.allowed = []
    assert web.client.get("/api/dashboard/executive", params={"scope": SUB}).status_code == 403
    assert len(cache.contexts) == 1


def test_validation_runs_before_cache_and_anonymous_requests_never_reach_it(web, monkeypatch):
    cache = HitCache()
    monkeypatch.setattr(main, "_snapshot_cache", cache)
    assert web.client.get("/api/dashboard/executive?days=invalid").status_code == 422
    assert not cache.contexts
    web.user = None
    assert web.client.get("/api/dashboard/executive").status_code == 401
    assert not cache.contexts


def test_role_and_tenant_mapping_changes_partition_but_session_renewal_does_not(web, monkeypatch):
    cache = HitCache()
    monkeypatch.setattr(main, "_snapshot_cache", cache)
    web.client.get("/api/dashboard/executive")
    original = cache.contexts[-1].partition()
    web.user = User("Reader", admin=True, source="entra", sid="renewed")
    web.client.get("/api/dashboard/executive")
    assert cache.contexts[-1].partition() == original
    web.user = User("Reader", admin=False, source="entra", sid="renewed")
    web.client.get("/api/dashboard/executive")
    assert cache.contexts[-1].partition() != original
    restricted = cache.contexts[-1].partition()
    web.session.subscriptions[0]["tenant"] = OTHER
    web.client.get("/api/dashboard/executive")
    assert cache.contexts[-1].partition() != restricted


def test_reauthentication_required_cannot_use_a_snapshot(web, monkeypatch):
    cache = HitCache()
    monkeypatch.setattr(main, "_snapshot_cache", cache)
    web.session.reauth_required = True
    assert web.client.get("/api/dashboard/executive").status_code == 401
    assert not cache.contexts


def test_read_only_staging_blocks_writes_before_endpoint_execution(web, monkeypatch):
    monkeypatch.setenv("CLOUDLENS_READ_ONLY", "true")
    response = web.client.post("/api/ingest", json={})
    assert response.status_code == 403
    assert response.json()["read_only"] is True
    response = web.client.post("/api/focus/jobs", json={})
    assert response.status_code == 403
    # Read-only previews still reach ordinary body validation.
    assert web.client.post("/api/focus/preview", content="{").status_code == 422
    # Authentication routes are not blocked by the deployment mutation guard.
    assert web.client.post("/api/auth/login", content="{").status_code == 422


def test_capability_endpoint_does_not_accept_client_identity_or_unauthorized_scope(web, monkeypatch):
    report = AsyncMock(return_value={"capabilities": [], "errors": [], "status": "ok"})
    monkeypatch.setattr(capabilities, "report", report)
    from app import archive
    monkeypatch.setattr(archive, "ACCOUNT_ID", "")
    response = web.client.get("/api/capabilities", params={"scope": SUB, "principal": "forged"})
    assert response.status_code == 200
    assert report.call_args.kwargs["identity"]["id"] == OID
    assert report.call_args.args[0] == [SUB]
    report.reset_mock()
    assert web.client.get("/api/capabilities", params={"scope": OTHER}).status_code == 403
    report.assert_not_called()


def test_workload_diagnostics_require_admin_and_clear_delegated_caller(web, monkeypatch):
    observed = []

    async def report(_ids, **kwargs):
        observed.append((cost._caller.get(), kwargs["identity"]))
        return {"capabilities": [], "errors": [], "status": "ok"}

    monkeypatch.setattr(capabilities, "report", report)
    monkeypatch.setenv("CLOUDLENS_PRINCIPAL_ID", "workload-object")
    monkeypatch.setenv("CLOUDLENS_TENANT_ID", TENANT)
    from app import archive
    monkeypatch.setattr(archive, "ACCOUNT_ID", "")
    response = web.client.get("/api/capabilities", params={"scope": SUB, "mode": "workload"})
    assert response.status_code == 200
    assert observed == [(None, {"id": "workload-object", "tenant_id": TENANT, "kind": "workload"})]
    web.user = User("Reader", admin=False, source="entra", sid="session")
    assert web.client.get("/api/capabilities?mode=workload").status_code == 403
    assert len(observed) == 1


def test_html_uses_versioned_assets_without_weakening_csp(web):
    response = web.client.get("/")
    assert response.status_code == 200
    assert "/assets/v/" in response.text
    assert response.headers["content-type"].startswith("text/html")
    policy = response.headers["content-security-policy"]
    script = next(part for part in policy.split(";") if "script-src" in part)
    assert "'unsafe-inline'" not in script
    web.user = None
    login = web.client.get("/login")
    assert login.status_code == 200
    assert "/assets/v/" in login.text


@pytest.mark.asyncio
@pytest.mark.parametrize("readonly", [True, False])
async def test_lifespan_owns_cache_and_gates_mutating_background_workers(monkeypatch, readonly):
    from app import azure_updates, backfill, currency, jobs, onboarding, service_health

    events = []

    class Cache:
        def __init__(self, *_args, **_kwargs):
            pass

        async def start(self):
            events.append("cache-start")

        async def close(self):
            events.append("cache-close")

    async def wait(name):
        events.append(name)
        await asyncio.Event().wait()

    async def prepare(_warehouse, state):
        events.append("snapshot-bootstrap")
        state.snapshot_bootstrap_ready = True

    fake = SimpleNamespace(
        connect=lambda: None, check_readiness=lambda: True, summary=lambda: {"rows": 1},
        _store=SimpleNamespace(close=lambda: events.append("pool-close")),
    )
    module = ModuleType("app.warehouse")
    module.warehouse = fake
    monkeypatch.setitem(sys.modules, "app.warehouse", module)
    monkeypatch.setenv("PROJECT_ENDPOINT", "https://example.invalid/projects/test")
    monkeypatch.setenv("CLOUDLENS_READ_ONLY", str(readonly).lower())
    monkeypatch.setenv("SNAPSHOT_CACHE_ENABLED", "true")
    monkeypatch.setattr(main, "SnapshotCache", Cache)
    monkeypatch.setattr(main, "application_version", lambda **_kwargs: "test-version")
    monkeypatch.setattr(main, "CostAgent", lambda: SimpleNamespace(model="test", close=AsyncMock()))
    monkeypatch.setattr(main, "_auth", SimpleNamespace(sso=SimpleNamespace(enabled=False)))
    monkeypatch.setattr(main, "_refresh_overview", lambda: wait("overview"))
    monkeypatch.setattr(main, "_daily_refresh", lambda: wait("daily"))
    monkeypatch.setattr(main, "_focus_refresh_loop", lambda: wait("focus"))
    monkeypatch.setattr(main, "_recover_ingest_jobs", lambda: wait("recover"))
    monkeypatch.setattr(main, "_prepare_snapshot_generation", prepare)
    monkeypatch.setattr(main, "_savings_pending", {})
    monkeypatch.setattr(main, "_billing_pending", {})
    monkeypatch.setattr(main, "_audit_tasks", set())
    monkeypatch.setattr(main, "_ingest_task", None)
    monkeypatch.setattr(backfill, "_tasks", set())
    monkeypatch.setattr(onboarding, "_task", None)
    monkeypatch.setattr(currency, "refresh", AsyncMock())
    monkeypatch.setattr(jobs, "stop", AsyncMock())
    monkeypatch.setattr(azure_updates._public_cache, "close", AsyncMock())
    monkeypatch.setattr(service_health._cache, "close", AsyncMock())
    async with main.lifespan(main.app):
        await asyncio.sleep(0)
        assert "overview" in events
        assert "snapshot-bootstrap" in events
        assert {"daily", "focus", "recover"}.issubset(events) is not readonly
    assert events.index("cache-close") < events.index("pool-close")
    assert main._snapshot_cache is None


@pytest.mark.asyncio
@pytest.mark.parametrize("ready", [True, False])
async def test_startup_prepares_missing_generation_without_reimporting_costs(monkeypatch, ready):
    from app import jobs

    current = {"ready": ready}

    async def rebuild():
        current["ready"] = True

    monkeypatch.setattr(jobs, "read_generation", lambda: 0)
    monkeypatch.setattr(jobs, "generation_ready",
                        lambda generation: current["ready"] and generation == 0)
    warehouse = SimpleNamespace(refresh_rollup_async=AsyncMock(side_effect=rebuild))
    state = SimpleNamespace(snapshot_bootstrap_ready=False, snapshot_bootstrap_error=None)
    await main._prepare_snapshot_generation(warehouse, state)
    assert state.snapshot_bootstrap_ready is True
    assert warehouse.refresh_rollup_async.await_count == (0 if ready else 1)
