"""Readiness is bounded, private, read-only and independent of liveness."""
from __future__ import annotations

import asyncio
import threading
from types import SimpleNamespace
from unittest.mock import MagicMock, Mock

import psycopg
import pytest
from fastapi.testclient import TestClient

from app import main
from app.readiness import ReadinessProbe
from app.store import PostgresStore


def test_success_failure_and_recovery_use_a_short_cache(caplog):
    now = [100.0]
    check = Mock(side_effect=[True, RuntimeError("private-db.example secret-password"), True])

    async def run():
        probe = ReadinessProbe(check, clock=lambda: now[0])
        assert await probe.ready()
        assert await probe.ready()
        assert check.call_count == 1
        now[0] += 6
        assert not await probe.ready()
        assert not await probe.ready()
        assert check.call_count == 2
        now[0] += 6
        assert await probe.ready()
        await probe.close()
        assert not await probe.ready()
        assert check.call_count == 3

    asyncio.run(run())
    assert "RuntimeError" in caplog.text
    assert "private-db.example" not in caplog.text
    assert "secret-password" not in caplog.text


def test_concurrent_requests_share_one_check():
    started, release = threading.Event(), threading.Event()
    calls = 0

    def check():
        nonlocal calls
        calls += 1
        started.set()
        if not release.wait(2):
            raise TimeoutError("fixture was not released")
        return True

    async def run():
        probe = ReadinessProbe(check)
        requests = [asyncio.create_task(probe.ready()) for _ in range(12)]
        try:
            assert await asyncio.to_thread(started.wait, 1)
            release.set()
            assert all(await asyncio.gather(*requests))
            assert calls == 1
        finally:
            release.set()
            await probe.close()

    asyncio.run(run())


def test_timeout_does_not_queue_more_checks_or_cache_a_late_success():
    started, release = threading.Event(), threading.Event()
    calls = 0

    def check():
        nonlocal calls
        calls += 1
        started.set()
        if not release.wait(2):
            raise TimeoutError("fixture was not released")
        return True

    async def run():
        probe = ReadinessProbe(check, timeout=0.1, cache_seconds=0)
        try:
            request = asyncio.create_task(probe.ready())
            assert await asyncio.to_thread(started.wait, 1)
            attempt = probe._attempt
            assert not await asyncio.wait_for(request, timeout=1)
            assert not attempt.task.cancelled()
            assert not any(await asyncio.gather(*(probe.ready() for _ in range(12))))
            assert calls == 1
            release.set()
            await attempt.task
            assert not probe._ready
            assert await probe.ready()
            assert calls == 2
        finally:
            release.set()
            await probe.close()

    asyncio.run(run())


def test_disconnected_caller_does_not_cancel_a_shared_check():
    started, release = threading.Event(), threading.Event()

    def check():
        started.set()
        return release.wait(2)

    async def run():
        probe = ReadinessProbe(check)
        caller = asyncio.create_task(probe.ready())
        try:
            assert await asyncio.to_thread(started.wait, 1)
            caller.cancel()
            with pytest.raises(asyncio.CancelledError):
                await caller
            assert not probe._attempt.task.cancelled()
            release.set()
            assert await probe.ready()
        finally:
            release.set()
            await probe.close()

    asyncio.run(run())


def test_shutdown_rejects_probes_while_an_old_check_is_running():
    started, release = threading.Event(), threading.Event()
    check = Mock(side_effect=lambda: (started.set(), release.wait(2))[1])

    async def run():
        probe = ReadinessProbe(check)
        caller = asyncio.create_task(probe.ready())
        try:
            assert await asyncio.to_thread(started.wait, 1)
            await probe.close()
            assert not await probe.ready()
            assert check.call_count == 1
            await asyncio.gather(caller, return_exceptions=True)
        finally:
            release.set()

    asyncio.run(run())


@pytest.mark.parametrize(("timeout", "cache"), [(0, 5), (-1, 5), (5, -1), (float("nan"), 5), (5, float("inf"))])
def test_probe_rejects_unbounded_configuration(timeout, cache):
    with pytest.raises(ValueError):
        ReadinessProbe(lambda: True, timeout=timeout, cache_seconds=cache)


@pytest.mark.parametrize("available", [True, False, None, "failure"])
def test_public_readiness_has_no_private_details_and_does_not_open_other_routes(monkeypatch, available):
    check = (Mock(side_effect=RuntimeError("private-host token=do-not-disclose"))
             if available == "failure" else Mock(return_value=available))
    probe = None if available is None else ReadinessProbe(check)
    monkeypatch.setattr(main.app.state, "readiness", probe, raising=False)
    monkeypatch.setattr(main, "auth_service", lambda: SimpleNamespace(identify=lambda *_: None))
    client = TestClient(main.app)
    try:
        response = client.get("/readyz?scope=private-subscription&period_month=invalid")
        expected_ready = available is True
        assert response.status_code == (200 if expected_ready else 503)
        assert response.json() == {"status": "ready" if expected_ready else "not_ready"}
        assert response.headers["cache-control"] == "no-store"
        assert ("retry-after" in response.headers) is not expected_ready
        assert client.get("/healthz").json() == {"status": "ok"}
        assert client.get("/api/subscriptions").status_code == 401
        assert client.post("/readyz").status_code == 405
    finally:
        client.close()
        if probe is not None:
            asyncio.run(probe.close())


@pytest.mark.parametrize("schema", ["", "CloudLens"])
@pytest.mark.parametrize("failure", [None, psycopg.errors.UndefinedColumn, psycopg.errors.InsufficientPrivilege])
def test_database_probe_is_read_only_schema_qualified_and_closes_connections(monkeypatch, schema, failure):
    monkeypatch.setenv("PGHOST", "fixture.invalid")
    monkeypatch.setenv("PGSCHEMA", schema)
    store = PostgresStore()
    monkeypatch.setattr(store, "_password", Mock(return_value="fixture-token"))
    monkeypatch.setattr(store, "connect", Mock(side_effect=AssertionError("must not use the application pool")))
    monkeypatch.setattr(store, "_open", Mock(side_effect=AssertionError("must not run schema setup")))
    connection, cursor = MagicMock(), MagicMock()
    connection.__enter__.return_value = connection
    connection.cursor.return_value.__enter__.return_value = cursor
    if failure:
        cursor.execute.side_effect = failure("fixture failure")
    connect = Mock(return_value=connection)
    monkeypatch.setattr(psycopg, "connect", connect)
    tables = {"costs": ("ChargePeriodStart", "BilledCost"), "meta": ("key", "value")}
    if failure:
        with pytest.raises(failure):
            store.check_readiness(tables)
    else:
        assert store.check_readiness(tables)
    settings = connect.call_args.kwargs
    assert settings["host"] == "fixture.invalid"
    assert settings["password"] == "fixture-token"
    assert settings["connect_timeout"] == 3
    assert settings["autocommit"] is True
    assert "default_transaction_read_only=on" in settings["options"]
    assert "statement_timeout=2000" in settings["options"]
    assert "lock_timeout=1000" in settings["options"]
    statements = [call.args[0].as_string() for call in cursor.execute.call_args_list]
    expected_schema = schema.lower() or "public"
    assert all(statement.startswith("SELECT ") and statement.endswith(" LIMIT 0") for statement in statements)
    assert all(f'FROM "{expected_schema}".' in statement for statement in statements)
    if schema:
        assert all('"public".' not in statement for statement in statements)
    assert connection.__exit__.call_count == 1
    store.connect.assert_not_called()
    store._open.assert_not_called()


@pytest.mark.postgres
def test_real_empty_schema_is_ready_and_a_missing_required_column_is_not(postgres_warehouse):
    warehouse = postgres_warehouse
    assert warehouse.check_readiness()
    with warehouse.connect() as connection:
        connection.execute('ALTER TABLE costs DROP COLUMN "Tags"')
    with pytest.raises(psycopg.errors.UndefinedColumn):
        warehouse.check_readiness()
