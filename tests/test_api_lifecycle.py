"""API scope, authorization failure, and application cleanup regressions without Azure or a DB."""
from __future__ import annotations

import asyncio
import sys
from types import ModuleType, SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from fastapi import HTTPException

from app import cost, entra, main
from app.auth import User
from app.scopes import plan_scopes

A = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
B = "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"
HOME = "11111111-1111-1111-1111-111111111111"
OTHER = "22222222-2222-2222-2222-222222222222"
USER = User("reader", source="entra", sid="test-session")


@pytest.mark.parametrize("picked", [[B], [A, B]])
def test_explicit_unauthorized_scope_is_never_widened(picked):
    with pytest.raises(HTTPException) as refused:
        main.narrow(picked, [A])
    assert refused.value.status_code == 403


def test_scope_selection_handles_case_whitespace_and_empty_entitlement():
    assert main.narrow([f" {B.upper()} "], [A, B]) == [B]
    assert main.narrow([B, B.upper(), f" {B} "], [A, B]) == [B]
    assert main.narrow([], [A, B]) == [A, B]
    assert main.narrow([], None) == []
    assert main.narrow([f" {A} "], None) == [A]
    with pytest.raises(HTTPException) as refused:
        main.narrow([], [])
    assert refused.value.status_code == 403


def test_rejected_billing_account_does_not_hide_its_subscriptions():
    plan = plan_scopes(
        [{"id": A}, {"id": B}],
        [
            {"name": "enterprise", "agreementType": "MicrosoftCustomerAgreement",
             "subscriptions": [{"id": A}]},
            {"name": "payg", "agreementType": "MicrosoftOnlineServicesProgram",
             "subscriptions": [{"id": B}]},
        ],
    )
    assert plan["strategy"] == "mixed"
    assert plan["covered"] == [A]
    assert [(scope.kind, scope.parts) for scope in plan["scopes"]] == [
        ("billingAccount", ("enterprise",)), ("subscription", (B,)),
    ]


def make_session(monkeypatch, *, multi_tenant=False):
    sso = entra.EntraSSO.__new__(entra.EntraSSO)
    sso.delegated = True
    sso.multi_tenant = multi_tenant
    session = entra.Session(
        sid=USER.sid, name=USER.name, email="reader@example.invalid", oid="reader",
        tenant=HOME, app=None, account={},
    )
    sso._sessions = {session.sid: session}
    monkeypatch.setattr(sso, "token", AsyncMock(return_value="home-token"))
    monkeypatch.setattr(sso, "token_for_tenant", AsyncMock(return_value="other-token"))
    monkeypatch.setattr(main, "auth_service", lambda: SimpleNamespace(sso=sso))
    return sso, session


@pytest.mark.parametrize("entrypoint", ["permitted", "as_user"])
def test_transient_azure_failure_returns_retryable_error_without_signing_out(monkeypatch, entrypoint):
    sso, session = make_session(monkeypatch)
    monkeypatch.setattr(cost, "subscriptions_with", AsyncMock(side_effect=TimeoutError("Azure timeout")))

    async def run():
        with pytest.raises(HTTPException) as unavailable:
            if entrypoint == "permitted":
                await main.permitted(USER)
            else:
                async with main.as_user(USER):
                    pytest.fail("failed authorization must not enter the operation")
        assert unavailable.value.status_code == 503
        assert unavailable.value.headers["Retry-After"] == "5"
        assert sso.get(USER.sid) is session
        assert session.caller is None
        assert session.subscriptions is None

        monkeypatch.setattr(cost, "subscriptions_with", AsyncMock(return_value=[{"id": A}]))
        assert await main.permitted(USER) == [A]

    asyncio.run(run())


def test_confirmed_empty_access_is_forbidden_not_service_unavailable(monkeypatch):
    make_session(monkeypatch)
    monkeypatch.setattr(cost, "subscriptions_with", AsyncMock(return_value=[]))
    with pytest.raises(HTTPException) as refused:
        asyncio.run(main.permitted(USER))
    assert refused.value.status_code == 403


def test_revoked_session_still_requires_signin(monkeypatch):
    sso, _ = make_session(monkeypatch)
    monkeypatch.setattr(sso, "token", AsyncMock(return_value=None))
    with pytest.raises(HTTPException) as expired:
        asyncio.run(main.permitted(USER))
    assert expired.value.status_code == 401
    assert sso.get(USER.sid) is None


@pytest.mark.parametrize(("all_failed", "empty_home"), [(False, False), (True, False), (False, True)])
def test_tenant_listing_failure_is_not_reported_as_zero_subscriptions(monkeypatch, all_failed, empty_home):
    sso, session = make_session(monkeypatch, multi_tenant=True)
    monkeypatch.setattr(cost, "list_tenants", AsyncMock(return_value=[
        {"id": HOME, "name": "Home"}, {"id": OTHER, "name": "Other"},
    ]))

    async def subscriptions(token):
        if all_failed or token == "other-token":
            raise TimeoutError("ARM unavailable")
        return [] if empty_home else [{"id": A, "name": "Production"}]

    monkeypatch.setattr(cost, "subscriptions_with", subscriptions)
    asyncio.run(sso.subscriptions(session))
    assert any(item["id"] == OTHER and item["unavailable"] for item in session.skipped)
    assert all(item["id"] != OTHER for item in session.tenants)
    if all_failed or empty_home:
        assert session.subscriptions is None
        assert session.caller is None
        assert session.checked == 0
        assert "Try again" in session.problem
    else:
        assert [item["id"] for item in session.subscriptions] == [A]
        assert session.problem == ""


@pytest.fixture
def runtime(monkeypatch):
    warehouse = SimpleNamespace(summary=Mock(return_value={"rows": 0}), connect=Mock(),
                                _store=SimpleNamespace(close=Mock()))
    warehouse.check_readiness = Mock(return_value=True)
    module = ModuleType("app.warehouse")
    module.warehouse = warehouse
    monkeypatch.setitem(sys.modules, "app.warehouse", module)

    from app import azure_updates, backfill, currency, onboarding, service_health

    closed_jobs = set()

    async def job(name):
        try:
            await asyncio.Event().wait()
        finally:
            await asyncio.sleep(0)
            closed_jobs.add(name)

    agent = SimpleNamespace(model="test", close=AsyncMock())
    cache = SimpleNamespace(start=AsyncMock(), close=AsyncMock())

    async def bootstrap(owner, state):
        assert owner is warehouse
        state.snapshot_bootstrap_ready = True
        await job("bootstrap")

    async def start_ingest(factory, **kwargs):
        assert kwargs == {"kind": "automatic", "identity": "workload"}
        main._ingest_task = asyncio.create_task(factory())
        return main._ingest_task

    monkeypatch.setattr(main, "_agent", None)
    monkeypatch.setattr(main, "_snapshot_cache", None)
    monkeypatch.setattr(main, "SnapshotStore", Mock())
    monkeypatch.setattr(main, "SnapshotCache", Mock(return_value=cache))
    monkeypatch.setattr(main, "application_version", lambda **kwargs: "test-version")
    monkeypatch.setattr(main, "_prepare_snapshot_generation", bootstrap)
    monkeypatch.setattr(main, "_overview", {"status": "loading"})
    monkeypatch.setattr(main, "_ingest_task", None)
    monkeypatch.setattr(main, "_audit_tasks", set())
    monkeypatch.setattr(main, "_savings_pending", {})
    monkeypatch.setattr(main, "_billing_pending", {})
    monkeypatch.setattr(main, "_savings_cache", {})
    monkeypatch.setattr(main, "_billing_cache", {})
    monkeypatch.setattr(main, "CostAgent", lambda: agent)
    monkeypatch.setattr(main, "auth_service", lambda: SimpleNamespace(
        sso=SimpleNamespace(enabled=False, tenant="", home_tenant="not-enabled")))
    monkeypatch.setattr(main, "_refresh_overview", lambda: job("overview"))
    monkeypatch.setattr(main, "_daily_refresh", lambda: job("daily"))
    monkeypatch.setattr(main, "_focus_refresh_loop", lambda: job("focus"))
    monkeypatch.setattr(main, "_recover_ingest_jobs", lambda: job("recovery"))
    monkeypatch.setattr(main, "_start_ingest_task", start_ingest)
    monkeypatch.setattr(main, "_run_ingest", lambda months: job("ingest"))
    monkeypatch.setattr(currency, "refresh", lambda: job("rates"))
    monkeypatch.setattr(backfill, "_tasks", set())
    monkeypatch.setattr(onboarding, "_task", None)
    monkeypatch.setattr(azure_updates._public_cache, "close", AsyncMock())
    monkeypatch.setattr(service_health._cache, "close", AsyncMock())
    monkeypatch.setenv("PROJECT_ENDPOINT", "https://example.invalid")
    monkeypatch.setenv("AUTO_INGEST", "true")
    monkeypatch.setenv("INGEST_MONTHS", "3")
    monkeypatch.setenv("SNAPSHOT_CACHE_ENABLED", "true")
    monkeypatch.setenv("CLOUDLENS_READ_ONLY", "false")
    return SimpleNamespace(
        warehouse=warehouse, agent=agent, job=job, closed_jobs=closed_jobs,
        onboarding=onboarding, backfill=backfill, source_cache=service_health._cache, cache=cache,
    )


@pytest.mark.parametrize("lifespan_error", [False, True])
def test_lifespan_drains_background_and_audit_jobs_before_closing_resources(runtime, lifespan_error):
    expected = {"bootstrap", "overview", "daily", "focus", "recovery", "ingest", "rates",
                "savings", "billing", "onboarding", "backfill", "audit"}

    def check_jobs_finished():
        assert runtime.closed_jobs == expected
        assert main.app.state.readiness is None

    runtime.warehouse._store.close.side_effect = check_jobs_finished

    async def audit():
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        runtime.closed_jobs.add("audit")

    async def run():
        try:
            async with main.lifespan(main.app):
                probe = main.app.state.readiness
                assert await probe.ready()
                main._savings_pending["test"] = asyncio.create_task(runtime.job("savings"))
                main._billing_pending["test"] = asyncio.create_task(runtime.job("billing"))
                runtime.onboarding._task = asyncio.create_task(runtime.job("onboarding"))
                runtime.backfill._tasks.add(asyncio.create_task(runtime.job("backfill")))
                main._audit_tasks.add(asyncio.create_task(audit()))
                await asyncio.sleep(0)
                if lifespan_error:
                    raise RuntimeError("lifespan failed")
        except RuntimeError as exc:
            assert lifespan_error and str(exc) == "lifespan failed"
        runtime.agent.close.assert_awaited_once()
        runtime.cache.start.assert_awaited_once()
        runtime.cache.close.assert_awaited_once()
        runtime.warehouse._store.close.assert_called_once()
        assert not main._savings_pending and not main._billing_pending and not main._audit_tasks
        assert main._agent is None and main._ingest_task is None
        assert main.app.state.readiness is None
        assert not await probe.ready()
        runtime.warehouse.check_readiness.assert_called_once()

    asyncio.run(run())


def test_startup_failure_cancels_tasks_and_closes_resources(runtime, monkeypatch):
    runtime.warehouse.summary.side_effect = RuntimeError("startup failed")
    spawned = []
    create_task = asyncio.create_task

    def track(coro, **kwargs):
        task = create_task(coro, **kwargs)
        spawned.append(task)
        return task

    monkeypatch.setattr(main.asyncio, "create_task", track)

    async def run():
        with pytest.raises(RuntimeError, match="startup failed"):
            async with main.lifespan(main.app):
                pytest.fail("startup must not succeed")
        assert spawned and all(task.done() for task in spawned)
        runtime.agent.close.assert_awaited_once()
        runtime.warehouse._store.close.assert_called_once()
        assert main._agent is None
        assert main.app.state.readiness is None
        runtime.warehouse.check_readiness.assert_not_called()

    asyncio.run(run())


def test_snapshot_cache_failure_does_not_stop_startup(runtime):
    """A stopped database must cost the cache, not the whole app.

    `SnapshotCache.start()` opens PostgreSQL, and tenant policy stops the database
    nightly. When this raised inline the exception left `lifespan`, uvicorn exited 3,
    and App Service blocked the site for consecutive cold-start failures -- so a
    sleeping database became an app that could not be started at all.
    """
    runtime.cache.start.side_effect = RuntimeError("the database is not accepting connections")

    async def run():
        async with main.lifespan(main.app):
            probe = main.app.state.readiness
            assert probe is not None
            await asyncio.sleep(0)

    asyncio.run(run())
    runtime.cache.start.assert_awaited_once()
    runtime.agent.close.assert_awaited_once()
    runtime.warehouse._store.close.assert_called_once()


def test_readiness_does_not_wait_for_the_rollup_rebuild(runtime, monkeypatch):
    """`/readyz` means the database and core schema answer, nothing about freshness.

    It had drifted to also require `snapshot_bootstrap_ready`, which is only set once
    the rollup generation is coherent -- a rebuild measured at 382.2s on the 16.9M-row
    estate, during which the documented readiness contract ("an empty initialised
    warehouse is ready") was not being honoured.
    """
    started = asyncio.Event()

    async def never_finishes(owner, state):
        started.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(main, "_prepare_snapshot_generation", never_finishes)

    async def run():
        async with main.lifespan(main.app):
            await started.wait()
            assert main.app.state.snapshot_bootstrap_ready is False
            assert await main.app.state.readiness.ready()

    asyncio.run(run())


@pytest.mark.parametrize("failing_resource", ["agent", "source_cache"])
def test_one_cleanup_failure_does_not_skip_other_resources(runtime, failing_resource):
    getattr(runtime, failing_resource).close.side_effect = RuntimeError("close failed")

    async def run():
        async with main.lifespan(main.app):
            await asyncio.sleep(0)

    asyncio.run(run())
    runtime.agent.close.assert_awaited_once()
    runtime.source_cache.close.assert_awaited_once()
    runtime.warehouse._store.close.assert_called_once()


def test_audit_failure_is_consumed_and_logged(runtime, monkeypatch, caplog):
    monkeypatch.setattr(main, "offload", AsyncMock(side_effect=RuntimeError("audit unavailable")))
    runtime.warehouse.record_access = Mock()

    async def run():
        main._log_access(USER, "costs")
        await asyncio.gather(*list(main._audit_tasks), return_exceptions=True)
        await asyncio.sleep(0)
        assert not main._audit_tasks

    asyncio.run(run())
    assert "access audit write failed" in caplog.text
