"""Cache contracts with synthetic identities and a private real PostgreSQL schema."""
from __future__ import annotations

import asyncio
import json
import threading
from dataclasses import replace

import pytest
from fastapi import FastAPI, HTTPException, Request
from starlette.responses import JSONResponse, Response, StreamingResponse
from starlette.testclient import TestClient

from app.snapshot_cache import (
    AI_BILLING,
    CacheContext,
    Policy,
    SnapshotCache,
    SnapshotStore,
    application_version,
    cache_key,
    install_snapshot_routes,
)
from app.store import PoolBusy

PATH = "/api/dashboard/executive"
CONTEXT = CacheContext("oid:synthetic-reader", "synthetic-tenant", ("a", "b"), ("a",))
POLICY = Policy("warehouse", 10, 100)
AI_PATH = "/api/dashboard/ai-billing"
DAILY_CONTEXT = replace(CONTEXT, period_version="ai-rolling-data-day-v1", data_day="2026-09-14")


def request(query="", path=PATH, headers=(), method="GET"):
    return Request({"type": "http", "http_version": "1.1", "method": method,
                    "path": path, "root_path": "", "scheme": "https",
                    "server": ("test", 443), "query_string": query.encode(),
                    "headers": list(headers)})


class MemoryStore:
    max_payload = 10000

    def __init__(self):
        self.rows = {}
        self.generation = "1"
        self.writes = 0

    def initialize(self):
        pass

    def prune(self, now):
        self.rows = {key: item for key, item in self.rows.items() if item.retain_until > now}

    def load(self, key, partition, now, scope=None):
        item = self.rows.get(key)
        if item and (item.partition != partition or item.retain_until <= now):
            item = None
        return item, self.generation

    def put(self, item):
        if item.generation != self.generation:
            return False
        previous = self.rows.get(item.key)
        if previous and previous.created > item.created:
            return False
        self.rows[item.key] = item
        self.writes += 1
        return True

    def failure(self, item, now, code):
        current = self.rows.get(item.key)
        if current and current.created == item.created:
            self.rows[item.key] = replace(item, failures=item.failures + 1,
                                          error=code, retry_at=now + 5)

    def discard(self, key, partition):
        item = self.rows.get(key)
        if item and item.partition == partition:
            del self.rows[key]


class Authorize:
    def __init__(self, context=CONTEXT):
        self.context, self.calls, self.denied = context, 0, False

    async def __call__(self, request):
        self.calls += 1
        if self.denied:
            raise HTTPException(403, "Access revoked")
        return self.context


def make_cache(store=None, **kwargs):
    return SnapshotCache(store or MemoryStore(), version="calculation-v1",
                         policies={PATH: POLICY}, **kwargs)


async def settle(cache):
    await asyncio.gather(*list(cache._tasks.values()), return_exceptions=True)
    await asyncio.sleep(0)


def daily_cache(store=None, **kwargs):
    return SnapshotCache(store or MemoryStore(), version="calculation-v1",
                         policies={AI_PATH: AI_BILLING}, **kwargs)


def test_only_ai_billing_uses_imported_data_day_policy():
    from app.snapshot_cache import DEFAULT_POLICIES

    assert [path for path, policy in DEFAULT_POLICIES.items() if policy.daily] == [AI_PATH]
    assert DEFAULT_POLICIES["/api/dashboard/recommendations"].ttl == 900


def test_daily_snapshot_survives_same_day_import_midnight_and_worker_restart():
    async def scenario():
        now = [1000.]
        store, auth, calls = MemoryStore(), Authorize(DAILY_CONTEXT), []
        cache = daily_cache(store, clock=lambda: now[0])

        async def origin():
            calls.append(1)
            return JSONResponse({
                "status": "partial", "generated_at": "2026-09-15T10:00:00Z",
                "window": {"from": "2026-08-16", "to": "2026-09-14", "days": 30},
                "totals": [{"cost": 12, "currency": "USD"}],
                "tokens": {"status": "unavailable", "value": None},
            })

        first = await cache.get(request("days=30", path=AI_PATH), origin, auth)
        store.generation = "2"  # Corrected costs, but no new imported data day.
        now[0] += 2 * 86400
        await cache.close()
        cache = daily_cache(store, clock=lambda: now[0])
        again = await cache.get(request("days=30", path=AI_PATH), origin, auth)
        assert again.body == first.body
        assert again.headers["x-cloudlens-as-of"] == first.headers["x-cloudlens-as-of"]
        assert again.headers["x-cloudlens-source-generation"] == "1"
        assert again.headers["x-cloudlens-refresh-policy"] == "data-day"
        assert again.headers["x-cloudlens-data-day"] == "2026-09-14"
        assert again.headers["x-cloudlens-coverage"] == "partial"
        assert again.headers["x-cloudlens-stale"] == again.headers["x-cloudlens-revalidating"] == "false"
        conditional = await cache.get(request("days=30", path=AI_PATH, headers=[
            (b"if-none-match", first.headers["etag"].encode())]), origin, auth)
        assert conditional.status_code == 304
        assert conditional.headers["x-cloudlens-data-day"] == "2026-09-14"
        assert calls == [1] and store.writes == 1
        auth.denied = True
        with pytest.raises(HTTPException) as exc:
            await cache.get(request("days=30", path=AI_PATH), origin, auth)
        assert exc.value.status_code == 403
        await cache.close()

    asyncio.run(scenario())


@pytest.mark.parametrize("day", ["2026-09-15", "2026-09-13", "empty"])
def test_daily_data_day_changes_serve_honest_last_good_then_coalesce_refresh(day):
    async def scenario():
        now = [1000.]
        cache, auth, calls = daily_cache(clock=lambda: now[0]), Authorize(DAILY_CONTEXT), []
        started, release = asyncio.Event(), asyncio.Event()

        async def origin():
            calls.append(1)
            if len(calls) > 1:
                started.set()
                await release.wait()
            return JSONResponse({"total": len(calls)})

        first = await cache.get(request(path=AI_PATH), origin, auth)
        cache.store.generation = "2"
        auth.context = replace(auth.context, data_day=day)
        now[0] += 1
        stale = await cache.get(request(path=AI_PATH), origin, auth)
        await started.wait()
        assert stale.body == first.body
        assert stale.headers["x-cloudlens-stale"] == stale.headers["x-cloudlens-revalidating"] == "true"
        assert stale.headers["x-cloudlens-data-day"] == "2026-09-14"
        await cache.get(request(path=AI_PATH), origin, auth)
        assert len(calls) == 2
        release.set()
        await settle(cache)
        fresh = await cache.get(request(path=AI_PATH), origin, auth)
        assert fresh.headers["x-cloudlens-stale"] == "false"
        assert fresh.headers["x-cloudlens-data-day"] == day
        assert json.loads(fresh.body)["total"] == 2
        await cache.close()

    asyncio.run(scenario())


@pytest.mark.parametrize("query,changes", [
    ("days=7", {}), ("period_month=2026-08", {}), ("currency=EUR", {}),
    ("service=Foundry", {}), ("rg=other", {}), ("region=east", {}),
    ("scope=b", {"selected": ("b",)}),
    ("", {"principal": "different-user"}), ("", {"tenant": "different-tenant"}),
    ("", {"allowed": ("a",)}), ("", {"authorization_version": "new-roles"}),
    ("", {"fx_version": "new-fx"}), ("", {"period_version": "2026-08"}),
])
def test_daily_new_selection_never_borrows_incompatible_snapshot(query, changes):
    async def scenario():
        cache, auth, calls = daily_cache(), Authorize(DAILY_CONTEXT), []

        async def origin():
            calls.append(1)
            return JSONResponse({"total": len(calls)})

        await cache.get(request(path=AI_PATH), origin, auth)
        auth.context = replace(auth.context, **changes)
        fresh = await cache.get(request(query, path=AI_PATH), origin, auth)
        assert fresh.headers["x-cloudlens-cache"] == "miss"
        assert json.loads(fresh.body)["total"] == 2
        assert len(cache.store.rows) == 2
        await cache.close()

    asyncio.run(scenario())


@pytest.mark.parametrize("flag", ["force", "fresh", "refresh"])
def test_daily_explicit_retry_awaits_new_read_even_on_same_data_day(flag):
    async def scenario():
        cache, auth, calls = daily_cache(), Authorize(DAILY_CONTEXT), []

        async def origin():
            calls.append(1)
            return JSONResponse({"total": len(calls)})

        await cache.get(request(path=AI_PATH), origin, auth)
        cache.store.generation = "2"
        result = await cache.get(request(f"{flag}=true", path=AI_PATH), origin, auth)
        assert result.headers["x-cloudlens-cache"] == "refresh"
        assert result.headers["x-cloudlens-source-generation"] == "2"
        assert json.loads(result.body)["total"] == 2
        assert len(cache.store.rows) == 1
        await cache.close()

    asyncio.run(scenario())


def test_daily_failed_new_day_refresh_preserves_evidence_and_honors_backoff():
    async def scenario():
        now = [1000.]
        cache, auth, calls = daily_cache(clock=lambda: now[0]), Authorize(DAILY_CONTEXT), []

        async def origin():
            calls.append(1)
            if len(calls) > 1:
                return JSONResponse({"status": "unavailable", "total": 0})
            return JSONResponse({"total": 12})

        first = await cache.get(request(path=AI_PATH), origin, auth)
        cache.store.generation = "2"
        auth.context = replace(auth.context, data_day="2026-09-15")
        await cache.get(request(path=AI_PATH), origin, auth)
        await settle(cache)
        result = await cache.get(request(path=AI_PATH), origin, auth)
        assert result.body == first.body
        assert result.headers["x-cloudlens-refresh-error"] == "source_incomplete"
        assert result.headers["x-cloudlens-stale"] == "true"
        assert result.headers["x-cloudlens-revalidating"] == "false"
        assert len(calls) == 2
        await cache.get(request("force=true", path=AI_PATH), origin, auth)
        assert len(calls) == 3  # Explicit retry can bypass backoff, not coverage.
        assert cache.store.writes == 1
        await cache.close()

    asyncio.run(scenario())


@pytest.mark.parametrize("change_day", [False, True])
def test_daily_new_writes_still_reject_source_publication_races(change_day):
    async def scenario():
        cache, auth = daily_cache(), Authorize(DAILY_CONTEXT)

        async def origin():
            cache.store.generation = "2"
            if change_day:
                auth.context = replace(auth.context, data_day="2026-09-15")
            return JSONResponse({"total": 12})

        if change_day:
            with pytest.raises(HTTPException) as exc:
                await cache.get(request(path=AI_PATH), origin, auth)
            assert exc.value.status_code == 409
        else:
            response = await cache.get(request(path=AI_PATH), origin, auth)
            assert response.headers["x-cloudlens-cache"] == "superseded"
            assert response.headers["x-cloudlens-stale"] == "true"
        assert not cache.store.rows
        await cache.close()

    asyncio.run(scenario())


def test_daily_retention_remains_bounded_without_new_imports():
    async def scenario():
        now = [1000.]
        cache, auth, calls = daily_cache(clock=lambda: now[0]), Authorize(DAILY_CONTEXT), []

        async def origin():
            calls.append(1)
            return JSONResponse({"total": len(calls)})

        await cache.get(request(path=AI_PATH), origin, auth)
        now[0] += AI_BILLING.retention + 1
        result = await cache.get(request(path=AI_PATH), origin, auth)
        assert result.headers["x-cloudlens-cache"] == "miss"
        assert len(calls) == 2
        await cache.close()

    asyncio.run(scenario())


@pytest.fixture
def ready_cache_warehouse(postgres_warehouse):
    postgres_warehouse.refresh_rollup(strict=True)
    return postgres_warehouse


def test_key_preserves_repeated_query_semantics_but_canonicalizes_scope():
    assert cache_key(request("scope=b,a&days=30"), CONTEXT, "v", POLICY) == cache_key(
        request("days=30&scope=A,b,a"), CONTEXT, "v", POLICY)
    first = cache_key(request("days=1&days=30"), CONTEXT, "v", POLICY)
    assert first != cache_key(request("days=30&days=1"), CONTEXT, "v", POLICY)
    assert first != cache_key(request("days=30"), CONTEXT, "v", POLICY)
    assert first == cache_key(request("days=1&days=30&force=true"), CONTEXT, "v", POLICY)
    assert first == cache_key(request("days=1&days=30&refresh=true"), CONTEXT, "v", POLICY)
    assert first == cache_key(request("days=1&days=30&fresh=1"), CONTEXT, "v", POLICY)
    for query in ("currency=EUR", "days=7", "month=2026-01", "rg=x", "region=north"):
        assert cache_key(request(query), CONTEXT, "v", POLICY) != cache_key(
            request(), CONTEXT, "v", POLICY)


@pytest.mark.parametrize("context", [
    replace(CONTEXT, principal="another-oid"),
    replace(CONTEXT, tenant="another-tenant"),
    replace(CONTEXT, allowed=("a",)),
    replace(CONTEXT, selected=("b",)),
    replace(CONTEXT, authorization_version="new-roles"),
    replace(CONTEXT, fx_version="new-fx"),
    replace(CONTEXT, period_version="next-reporting-day"),
])
def test_identity_entitlements_scope_and_fx_are_isolated(context):
    assert cache_key(request(), context, "v", POLICY) != cache_key(request(), CONTEXT, "v", POLICY)


@pytest.mark.parametrize("context", [
    replace(CONTEXT, principal=""), replace(CONTEXT, tenant=""),
    replace(CONTEXT, allowed=()), replace(CONTEXT, selected=("c",)),
    replace(CONTEXT, selected=None),
])
def test_invalid_authorization_context_fails_closed(context):
    with pytest.raises(HTTPException):
        context.partition()


def test_warm_hit_reauthorizes_no_origin_or_database_writes_and_etag():
    async def scenario():
        cache, auth, calls = make_cache(), Authorize(), []

        async def origin():
            calls.append(1)
            return JSONResponse({"total": 12})

        await cache.start()
        first = await cache.get(request(), origin, auth)
        assert first.headers["x-cloudlens-cache"] == "miss"
        before = auth.calls
        again = await cache.get(request(headers=[(b"if-none-match", first.headers["etag"].encode())]),
                                origin, auth)
        assert again.status_code == 304
        assert again.headers["cache-control"] == "private, no-store"
        assert auth.calls > before
        assert len(calls) == cache.store.writes == 1
        auth.denied = True
        with pytest.raises(HTTPException) as exc:
            await cache.get(request(), origin, auth)
        assert exc.value.status_code == 403
        await cache.close()

    asyncio.run(scenario())


def test_stale_immediately_serves_last_good_and_failed_refresh_backs_off():
    async def scenario():
        now = [1000.0]
        cache, auth = make_cache(clock=lambda: now[0]), Authorize()
        fail, calls = False, []

        async def origin():
            calls.append(1)
            if fail:
                raise RuntimeError("sensitive-exception-must-not-be-stored")
            return JSONResponse({"total": 12})

        await cache.get(request(), origin, auth)
        now[0] += 11
        fail = True
        result = await cache.get(request(), origin, auth)
        assert json.loads(result.body)["total"] == 12
        assert result.headers["x-cloudlens-stale"] == "true"
        assert result.headers["x-cloudlens-revalidating"] == "true"
        await settle(cache)
        result = await cache.get(request(), origin, auth)
        assert result.headers["x-cloudlens-refresh-error"] == "source_failed"
        assert result.headers["x-cloudlens-revalidating"] == "false"
        assert len(calls) == 2
        assert "sensitive" not in repr(cache.store.rows)
        await cache.close()

    asyncio.run(scenario())


def test_expired_retention_cannot_be_served_as_last_good():
    async def scenario():
        now = [1000.]
        cache, auth = make_cache(clock=lambda: now[0]), Authorize()

        async def good():
            return JSONResponse({"total": 12})

        async def failed():
            raise HTTPException(502, "source unavailable")

        await cache.get(request(), good, auth)
        now[0] += 101
        with pytest.raises(HTTPException) as exc:
            await cache.get(request(), failed, auth)
        assert exc.value.status_code == 502
        await cache.close()

    asyncio.run(scenario())


@pytest.mark.parametrize("flag", ["force", "refresh", "fresh"])
def test_explicit_refresh_reuses_context_key_and_waits_for_fresh_source(flag):
    async def scenario():
        cache, auth, calls = make_cache(), Authorize(), []
        started, release = asyncio.Event(), asyncio.Event()

        async def origin():
            calls.append(1)
            if len(calls) > 1:
                started.set()
                await release.wait()
            return JSONResponse({"total": len(calls)})

        await cache.get(request(), origin, auth)
        refreshing = asyncio.create_task(cache.get(request(f"{flag}=true"), origin, auth))
        await started.wait()
        assert not refreshing.done()
        release.set()
        forced = await refreshing
        assert json.loads(forced.body)["total"] == 2
        assert forced.headers["x-cloudlens-revalidating"] == "false"
        assert forced.headers["x-cloudlens-cache"] == "refresh"
        fresh = await cache.get(request(), origin, auth)
        assert json.loads(fresh.body)["total"] == 2
        assert len(cache.store.rows) == 1
        await cache.close()

    asyncio.run(scenario())


@pytest.mark.parametrize("kind", ["ai-30", "ai-90", "performance"])
def test_completed_partial_live_reports_are_cached_without_rewriting_coverage(kind):
    async def scenario():
        from app.snapshot_cache import AI, MONITOR

        ai = kind.startswith("ai")
        days = 30 if kind == "ai-30" else 90
        path = "/api/dashboard/ai-billing" if ai else "/api/dashboard/performance"
        policy = AI if ai else MONITOR
        cache = SnapshotCache(MemoryStore(), version="v", policies={path: policy})
        auth, calls = Authorize(), []
        if ai:
            payload = {
                "status": "partial", "generated_at": "2026-09-14T12:00:00Z",
                "window": {"days": days}, "totals": [{"currency": "USD", "cost": 45}],
                "services": [{
                    "resource_id": "/subscriptions/a/providers/Microsoft.CognitiveServices/accounts/test",
                    "currency": "USD", "cost": 45, "billing_rows": 10,
                    "deployment_status": "partial",
                    "tokens": {"status": "partial", "value": 1234, "input": 1000,
                               "output": 234, "samples": 744, "expected_samples": 2153},
                    "utilization": {"status": "unavailable", "value": None,
                                    "note": "Unknown utilization is not zero"},
                }],
                "models": [],
                "metric_coverage": {"accounts_with_tokens": 1, "accounts_with_complete_tokens": 0},
            }
        else:
            payload = {
                "status": "partial", "generated_at": "2026-09-14T12:00:00Z",
                "sampled_count": 24, "inventory_count": None, "truncated": True,
                "inventory": {"status": "partial", "sampled": 24},
                "resources": [{
                    "id": f"/subscriptions/a/resources/vm-{i}", "state": "running",
                    "status": "partial", "cost": {"currency": "USD", "amount": 12},
                    "metrics": [{"status": "ok", "value": 0, "samples": 144},
                                {"status": "unavailable", "value": None, "samples": 0}],
                } for i in range(24)],
                "notices": ["Inventory is truncated; resources outside this sample were not assessed."],
            }

        async def origin():
            calls.append(1)
            return JSONResponse(payload)

        first = await cache.get(request(path=path), origin, auth)
        second = await cache.get(request(path=path), origin, auth)
        assert json.loads(second.body) == payload
        assert first.headers["x-cloudlens-coverage"] == "partial"
        assert second.headers["x-cloudlens-coverage"] == "partial"
        assert second.headers["x-cloudlens-cache"] == "hit"
        assert second.headers["x-cloudlens-stale"] == "false"
        assert calls == [1]
        await cache.close()

    asyncio.run(scenario())


@pytest.mark.parametrize("job", [
    {"status": "running"}, {"state": "queued"}, {"status": "failed"},
    {"status": "committing"}, {"status": "ready", "running": True},
    {"status": "partial", "state": "running"},
])
def test_nested_job_states_cannot_be_cached_as_completed_reports(job):
    async def scenario():
        cache, auth = make_cache(), Authorize()

        async def origin():
            return JSONResponse({"status": "partial", "total": 100, "job": job,
                                 "resources": [{"state": "running", "cost": 100}]})

        await cache.get(request(), origin, auth)
        assert not cache.store.rows
        await cache.close()

    asyncio.run(scenario())


@pytest.mark.parametrize("payload", [
    {"status": "running", "rows": []},
    {"status": "partial", "total": 12, "job": {"status": "committing"}},
])
def test_in_progress_origin_response_has_explicit_pollable_pending_headers(payload):
    async def scenario():
        cache, auth = make_cache(), Authorize()

        async def origin():
            return JSONResponse(payload)

        result = await cache.get(request(), origin, auth)
        assert json.loads(result.body) == payload
        assert result.headers["x-cloudlens-cache"] == "source-pending"
        assert result.headers["x-cloudlens-source-state"] == "pending"
        assert result.headers["x-cloudlens-revalidating"] == "true"
        assert result.headers["x-cloudlens-stale"] == "true"
        assert result.headers["retry-after"] == "3"
        assert "x-cloudlens-checked-at" in result.headers
        assert "x-cloudlens-refresh-error" not in result.headers
        assert not cache.store.rows
        await cache.close()

    asyncio.run(scenario())


@pytest.mark.parametrize("failed", [
    {"status": "partial", "services": [], "totals": [{"currency": "USD", "cost": 0}]},
    {"status": "unavailable", "resources": [], "inventory_count": 0},
    {"status": "partial", "total": 0, "coverage": {"status": "unavailable"}},
    {"status": "partial", "resources": [{"status": "unavailable", "metrics": []}]},
    {"status": "partial", "total": 0, "coverage": {"errors": [{"value": 403}]}},
])
def test_failure_shaped_empty_refresh_never_replaces_healthy_snapshot(failed):
    async def scenario():
        cache, auth = make_cache(), Authorize()

        async def healthy():
            return JSONResponse({"status": "ok", "total": 45})

        async def failed_origin():
            return JSONResponse(failed)

        first = await cache.get(request(), healthy, auth)
        refresh = await cache.get(request("fresh=1"), failed_origin, auth)
        assert refresh.headers["x-cloudlens-refresh-error"] == "source_incomplete"
        assert refresh.headers["x-cloudlens-cache"] == "source-incomplete"
        assert refresh.headers["x-cloudlens-revalidating"] == "false"
        last_good = await cache.get(request(), failed_origin, auth)
        assert last_good.body == first.body
        assert last_good.headers["x-cloudlens-refresh-error"] == "source_incomplete"
        assert last_good.headers["x-cloudlens-stale"] == "true"
        assert cache.store.writes == 1
        await cache.close()

    asyncio.run(scenario())


def test_force_source_failure_returns_original_failure_not_old_success():
    async def scenario():
        cache, auth = make_cache(), Authorize()

        async def healthy():
            return JSONResponse({"total": 12})

        async def failed():
            raise HTTPException(502, "source unavailable")

        await cache.get(request(), healthy, auth)
        with pytest.raises(HTTPException) as exc:
            await cache.get(request("force=true"), failed, auth)
        assert exc.value.status_code == 502
        assert exc.value.headers["X-CloudLens-Refresh-Error"] == "source_failed"
        assert len(cache.store.rows) == 1
        await cache.close()

    asyncio.run(scenario())


def test_force_cannot_report_success_for_a_superseded_generation():
    async def scenario():
        cache, auth = make_cache(), Authorize()
        started, release = asyncio.Event(), asyncio.Event()

        async def origin():
            return JSONResponse({"total": 12})

        async def slow():
            started.set()
            await release.wait()
            return JSONResponse({"total": 13})

        async def newer():
            return JSONResponse({"total": 14})

        await cache.get(request(), origin, auth)
        old = asyncio.create_task(cache.get(request("force=1"), slow, auth))
        await started.wait()
        cache.store.generation = "2"
        fresh = await cache.get(request("force=1"), newer, auth)
        assert json.loads(fresh.body)["total"] == 14
        release.set()
        with pytest.raises(HTTPException) as exc:
            await old
        assert exc.value.status_code == 409
        assert exc.value.headers["X-CloudLens-Refresh-Error"] == "source_changed"
        await cache.close()

    asyncio.run(scenario())


def test_forced_refresh_overload_does_not_return_an_old_success():
    async def scenario():
        cache, auth = make_cache(concurrency=1, max_inflight=1), Authorize()
        started, release = asyncio.Event(), asyncio.Event()

        async def origin():
            return JSONResponse({"total": 12})

        async def slow():
            started.set()
            await release.wait()
            return JSONResponse({"total": 13})

        await cache.get(request("days=1"), origin, auth)
        busy = asyncio.create_task(cache.get(request("days=2"), slow, auth))
        await started.wait()
        with pytest.raises(HTTPException) as exc:
            await cache.get(request("days=1&fresh=1"), origin, auth)
        assert exc.value.status_code == 503
        release.set()
        await busy
        await cache.close()

    asyncio.run(scenario())


def test_generation_race_never_overwrites_newer_snapshot():
    async def scenario():
        cache, auth = make_cache(), Authorize()
        started, release = asyncio.Event(), asyncio.Event()

        async def slow():
            started.set()
            await release.wait()
            return JSONResponse({"total": "old"})

        async def fresh():
            return JSONResponse({"total": "new"})

        old = asyncio.create_task(cache.get(request(), slow, auth))
        await started.wait()
        cache.store.generation = "2"
        await cache.get(request(), fresh, auth)
        release.set()
        old_response = await old
        assert old_response.headers["x-cloudlens-cache"] == "superseded"
        result = await cache.get(request(), fresh, auth)
        assert json.loads(result.body)["total"] == "new"
        assert cache.store.writes == 1
        await cache.close()

    asyncio.run(scenario())


def test_generation_change_returns_compatible_last_good_while_refreshing():
    async def scenario():
        cache, auth = make_cache(), Authorize()

        async def origin():
            return JSONResponse({"total": 12})

        first = await cache.get(request(), origin, auth)
        cache.store.generation = "2"
        next_response = await cache.get(request(), origin, auth)
        assert next_response.body == first.body
        assert next_response.headers["x-cloudlens-stale"] == "true"
        await settle(cache)
        fresh = await cache.get(request(), origin, auth)
        assert fresh.headers["x-cloudlens-source-generation"] == "2"
        await cache.close()

    asyncio.run(scenario())


def test_concurrent_misses_coalesce_and_cancelled_caller_does_not_poison_waiters():
    async def scenario():
        cache, auth = make_cache(), Authorize()
        started, release, calls = asyncio.Event(), asyncio.Event(), []

        async def origin():
            calls.append(1)
            started.set()
            await release.wait()
            return JSONResponse({"total": 12})

        one = asyncio.create_task(cache.get(request(), origin, auth))
        await started.wait()
        two = asyncio.create_task(cache.get(request(), origin, auth))
        one.cancel()
        with pytest.raises(asyncio.CancelledError):
            await one
        release.set()
        assert (await two).status_code == 200
        assert calls == [1]
        await cache.close()

    asyncio.run(scenario())


def test_bounded_origin_concurrency_queue_and_shutdown():
    async def scenario():
        cache, auth = make_cache(concurrency=1, max_inflight=1), Authorize()
        started, release = asyncio.Event(), asyncio.Event()

        async def origin():
            started.set()
            await release.wait()
            return JSONResponse({"total": 12})

        first = asyncio.create_task(cache.get(request("days=1"), origin, auth))
        await started.wait()
        with pytest.raises(HTTPException) as exc:
            await cache.get(request("days=2"), origin, auth)
        assert exc.value.status_code == 503
        await cache.close()
        with pytest.raises(asyncio.CancelledError):
            await first
        assert not cache._tasks

    asyncio.run(scenario())


def test_entitlement_change_during_origin_fails_closed():
    async def scenario():
        cache, auth = make_cache(), Authorize()

        async def origin():
            auth.context = replace(CONTEXT, allowed=("a",))
            return JSONResponse({"total": 12})

        with pytest.raises(HTTPException) as exc:
            await cache.get(request(), origin, auth)
        assert exc.value.status_code == 403
        assert not cache.store.rows
        await cache.close()

    asyncio.run(scenario())


def test_fx_change_during_origin_cannot_publish_under_old_calculation_context():
    async def scenario():
        cache, auth = make_cache(), Authorize()

        async def origin():
            auth.context = replace(CONTEXT, fx_version="new-reference-rate")
            return JSONResponse({"total": 12})

        with pytest.raises(HTTPException) as exc:
            await cache.get(request(), origin, auth)
        assert exc.value.status_code == 409
        assert not cache.store.rows
        await cache.close()

    asyncio.run(scenario())


@pytest.mark.parametrize("result", [
    {"error": "source failed", "total": 0}, {"status": "running"},
    {"status": "partial", "total": 0}, {"coverage": {"status": "unavailable"}},
    {"access_token": "synthetic-never-retain"}, {"data": {"refresh_token": "synthetic"}},
    {"url": "https://synthetic.invalid/?sig=synthetic-never-retain"},
    {"header": "Bearer synthetic-never-retain"},
    {"status": "partial", "total": 12,
     "coverage": {"error": "Upstream said Bearer synthetic-never-retain"}},
    {"status": "partial", "total": 12,
     "coverage": {"error": "Upstream session_cookie=synthetic-never-retain"}},
])
def test_error_incomplete_and_credential_payloads_are_never_persisted(result):
    async def scenario():
        cache, auth = make_cache(), Authorize()

        async def origin():
            return JSONResponse(result)

        response = await cache.get(request(), origin, auth)
        assert json.loads(response.body) == result
        assert not cache.store.rows
        await cache.close()

    asyncio.run(scenario())


@pytest.mark.parametrize("kind", ["cookie", "stream", "error", "download", "html"])
def test_non_json_errors_cookies_streams_downloads_never_persist(kind):
    async def scenario():
        cache, auth = make_cache(), Authorize()

        async def origin():
            if kind == "stream":
                return StreamingResponse(iter([b'{"total":12}']), media_type="application/json")
            if kind == "html":
                return Response("hello", media_type="text/html")
            response = JSONResponse({"total": 12}, status_code=502 if kind == "error" else 200)
            if kind == "cookie":
                response.set_cookie("session", "synthetic")
            if kind == "download":
                response.headers["Content-Disposition"] = "attachment; filename=data.json"
            return response

        await cache.get(request(), origin, auth)
        assert not cache.store.rows
        await cache.close()

    asyncio.run(scenario())


@pytest.mark.parametrize("path,method", [
    ("/api/auth/me", "GET"), ("/api/chat", "GET"), ("/api/report/csv", "GET"),
    (PATH, "POST"), ("/api/focus/jobs", "GET"),
])
def test_explicit_get_allowlist_only(path, method):
    async def scenario():
        cache, auth = make_cache(), Authorize()
        auth.denied = True

        async def origin():
            return JSONResponse({"original": True})

        response = await cache.get(request(path=path, method=method), origin, auth)
        assert json.loads(response.body) == {"original": True}
        assert auth.calls == 0
        assert not cache.store.rows
        await cache.close()

    asyncio.run(scenario())


def test_installation_preserves_fastapi_parameter_validation_and_error_contracts():
    app, cache, auth, calls = FastAPI(), make_cache(), Authorize(), []

    @app.get(PATH)
    async def endpoint(request: Request, days: int = 30):
        calls.append(days)
        if days == 99:
            raise HTTPException(400, "original validation detail")
        return JSONResponse({"days": days})

    install_snapshot_routes(app, lambda: cache, auth)
    with TestClient(app) as client:
        assert client.get(PATH + "?days=7").json() == {"days": 7}
        assert client.get(PATH + "?days=7").json() == {"days": 7}
        assert calls == [7]
        assert client.get(PATH + "?days=invalid").status_code == 422
        bad = client.get(PATH + "?days=99")
        assert bad.status_code == 400
        assert bad.json()["detail"] == "original validation detail"
        assert calls == [7, 99]


def test_code_version_changes_with_source_even_if_build_id_reused(tmp_path):
    code = tmp_path / "calculation.py"
    code.write_text("VALUE = 1")
    before = application_version(tmp_path, build_id="same")
    code.write_text("VALUE = 2")
    assert application_version(tmp_path, build_id="same") != before


def test_store_failures_do_not_change_successful_route_contract():
    async def scenario():
        store, auth = MemoryStore(), Authorize()
        cache = make_cache(store)

        def broken(item):
            raise RuntimeError("database failure")

        store.put = broken

        async def origin():
            return JSONResponse({"total": 12})

        response = await cache.get(request(), origin, auth)
        assert response.status_code == 200
        assert json.loads(response.body)["total"] == 12
        assert response.headers["x-cloudlens-cache"] == "unavailable"
        assert response.headers["x-cloudlens-cache-error"] == "write_failed"
        assert response.headers["x-cloudlens-refresh-error"] == "cache_write_failed"
        await cache.close()

    asyncio.run(scenario())


def test_pool_exhaustion_sheds_the_request_instead_of_re_entering_the_pool(caplog):
    """A busy pool is congestion, not a cache outage, and must not reach the origin.

    The generic read-failure path falls back to the origin route, which is right when
    the cache is the broken part. `PoolBusy` means every connection is checked out --
    the origin reads through the same pool, so calling it cannot succeed and only adds
    another waiter. The request is shed with a time to come back instead, and the
    status has to be 503: the application did not fail, it declined.
    """
    async def scenario():
        cache, auth = make_cache(), Authorize()
        called = []

        def busy(*args):
            raise PoolBusy("PostgreSQL connection pool is busy; retry shortly.")

        cache.store.load = busy

        async def origin():
            called.append(1)
            return JSONResponse({"total": 12})

        with pytest.raises(HTTPException) as raised:
            await cache.get(request("days=30&private=must-not-log"), origin, auth)
        await cache.close()
        return raised.value, called

    error, called = asyncio.run(scenario())
    assert error.status_code == 503
    assert error.headers["Retry-After"] == "5"
    assert error.headers["X-CloudLens-Cache-Error"] == "pool_busy"
    assert error.headers["Cache-Control"] == "private, no-store"
    assert called == [], "the origin needs the same exhausted pool and must not be called"
    assert "PoolBusy" in caplog.text
    assert "must-not-log" not in caplog.text


def test_read_failure_has_cache_unavailable_headers_and_sanitized_log(caplog):
    async def scenario():
        cache, auth = make_cache(), Authorize()

        def broken(*args):
            raise RuntimeError("synthetic-private-database-message")

        cache.store.load = broken

        async def origin():
            return JSONResponse({"total": 12})

        result = await cache.get(request("days=30&private=must-not-log"), origin, auth)
        assert result.status_code == 200
        assert result.headers["x-cloudlens-cache"] == "unavailable"
        assert result.headers["x-cloudlens-cache-error"] == "read_failed"
        assert result.headers["cache-control"] == "private, no-store"
        await cache.close()

    asyncio.run(scenario())
    assert "RuntimeError" in caplog.text
    assert PATH in caplog.text
    assert "synthetic-private-database-message" not in caplog.text
    assert "must-not-log" not in caplog.text


def test_failed_failure_persistence_is_visible_and_keeps_bounded_local_backoff(caplog):
    async def scenario():
        now = [1000.]
        cache, auth, calls = make_cache(clock=lambda: now[0]), Authorize(), []

        async def healthy():
            return JSONResponse({"total": 12})

        async def failed():
            calls.append(1)
            raise RuntimeError("synthetic-private-source-message")

        def broken(*args):
            raise OSError("synthetic-private-storage-message")

        first = await cache.get(request(), healthy, auth)
        cache.store.failure = broken
        now[0] += 11
        await cache.get(request(), failed, auth)
        await settle(cache)
        result = await cache.get(request(), failed, auth)
        assert result.body == first.body
        assert result.headers["x-cloudlens-refresh-error"] == "source_failed"
        assert result.headers["x-cloudlens-cache-error"] == "failure_write_failed"
        assert result.headers["x-cloudlens-revalidating"] == "false"
        assert result.headers["retry-after"] == "5"
        assert calls == [1]
        assert len(cache._failures) <= cache.max_inflight
        await cache.close()

    asyncio.run(scenario())
    assert "RuntimeError" in caplog.text and "OSError" in caplog.text
    assert "synthetic-private" not in caplog.text


def test_maintenance_failure_is_logged_visible_and_cleared_after_recovery(caplog):
    async def scenario():
        cache, auth = make_cache(), Authorize()

        async def origin():
            return JSONResponse({"total": 12})

        await cache.get(request(), origin, auth)
        original = cache.store.prune

        def failed(*args):
            raise OSError("synthetic-private-maintenance-message")

        cache.store.prune = failed
        await cache._maintenance_pass()
        result = await cache.get(request(), origin, auth)
        assert result.headers["x-cloudlens-cache-error"] == "maintenance_failed"
        cache.store.prune = original
        await cache._maintenance_pass()
        result = await cache.get(request(), origin, auth)
        assert "x-cloudlens-cache-error" not in result.headers
        await cache.close()

    asyncio.run(scenario())
    assert "maintenance" in caplog.text
    assert "OSError" in caplog.text
    assert "synthetic-private-maintenance-message" not in caplog.text


@pytest.mark.parametrize("changed", [False, True])
def test_background_completion_exposes_pollable_etag_asof_and_revalidation_state(changed):
    async def scenario():
        now, calls = [1000.], []
        cache, auth = make_cache(clock=lambda: now[0]), Authorize()

        async def origin():
            calls.append(1)
            return JSONResponse({"total": len(calls) if changed else 12})

        first = await cache.get(request(), origin, auth)
        conditional = request(headers=[(b"if-none-match", first.headers["etag"].encode())])
        now[0] += 11
        stale = await cache.get(conditional, origin, auth)
        assert stale.status_code == 304
        assert stale.headers["x-cloudlens-revalidating"] == "true"
        await settle(cache)
        updated = await cache.get(conditional, origin, auth)
        assert updated.status_code == (200 if changed else 304)
        assert updated.headers["x-cloudlens-revalidating"] == "false"
        assert updated.headers["x-cloudlens-stale"] == "false"
        assert updated.headers["x-cloudlens-as-of"] != first.headers["x-cloudlens-as-of"]
        assert updated.headers["x-cloudlens-source-generation"] == first.headers["x-cloudlens-source-generation"]
        assert len(calls) == 2
        await cache.close()

    asyncio.run(scenario())


def test_running_resource_state_does_not_look_like_an_incomplete_job():
    async def scenario():
        cache, auth = make_cache(), Authorize()

        async def origin():
            return JSONResponse({"resources": [{"state": "running", "tokens": 12}]})

        await cache.get(request(), origin, auth)
        assert len(cache.store.rows) == 1
        await cache.close()

    asyncio.run(scenario())


def test_many_simultaneous_cold_requests_remain_coalesced():
    async def scenario():
        for _ in range(10):
            cache, auth, calls = make_cache(), Authorize(), []

            async def origin(calls=calls):
                calls.append(1)
                return JSONResponse({"total": 12})

            responses = await asyncio.gather(*[
                cache.get(request(), origin, auth) for _ in range(20)])
            assert all(response.status_code == 200 for response in responses)
            assert calls == [1]
            await cache.close()

    asyncio.run(scenario())


def test_shutdown_awaits_in_progress_sync_database_operation():
    async def scenario():
        cache, entered, release = make_cache(), threading.Event(), threading.Event()

        def blocking():
            entered.set()
            assert release.wait(5)

        operation = asyncio.create_task(cache._db(blocking))
        while not entered.is_set():
            await asyncio.sleep(.001)
        operation.cancel()
        with pytest.raises(asyncio.CancelledError):
            await operation
        closing = asyncio.create_task(cache.close())
        await asyncio.sleep(.01)
        assert not closing.done()
        release.set()
        await closing
        assert not cache._db_tasks

    asyncio.run(scenario())


def test_dynamic_sections_remain_an_explicit_concrete_path_allowlist():
    path = "/api/dashboard/compute"
    app, auth, calls = FastAPI(), Authorize(), []
    cache = SnapshotCache(MemoryStore(), version="v", policies={path: POLICY})

    @app.get("/api/dashboard/{section_id}")
    async def endpoint(section_id: str, request: Request):
        calls.append(section_id)
        if section_id != "compute":
            raise HTTPException(404, "No such section")
        return JSONResponse({"section": section_id})

    install_snapshot_routes(app, lambda: cache, auth)
    with TestClient(app) as client:
        assert client.get(path).status_code == 200
        assert client.get(path).status_code == 200
        assert calls == ["compute"]
        assert client.get("/api/dashboard/not-allowlisted").status_code == 404
        assert calls == ["compute", "not-allowlisted"]


def test_default_cache_never_wraps_audit_or_changes_its_role_contract():
    from app.snapshot_cache import DEFAULT_POLICIES

    app, auth, calls = FastAPI(), Authorize(), []
    cache = SnapshotCache(MemoryStore(), version="v")
    role = [True]
    path = "/api/dashboard/audit"
    assert path not in DEFAULT_POLICIES

    @app.get(path)
    async def audit(request: Request, days: int = 30):
        if not role[0]:
            raise HTTPException(403, "Admin required")
        calls.append(days)
        return JSONResponse({"fetched": len(calls)})

    install_snapshot_routes(app, lambda: cache, auth)
    with TestClient(app) as client:
        assert client.get(path).json() == {"fetched": 1}
        assert client.get(path).json() == {"fetched": 2}
        assert client.get(path + "?days=invalid").status_code == 422
        role[0] = False
        assert client.get(path).status_code == 403
        assert not cache.store.rows


def test_committed_pending_generation_preserves_last_good_without_origin_until_ready():
    async def scenario():
        cache, auth, calls = make_cache(), Authorize(), []

        async def origin():
            calls.append(1)
            return JSONResponse({"total": len(calls)})

        first = await cache.get(request(), origin, auth)
        cache.store.generation = "pending:committed-data"
        for _ in range(3):
            result = await cache.get(request(), origin, auth)
            assert result.body == first.body
            assert result.headers["x-cloudlens-cache"] == "source-pending"
            assert result.headers["x-cloudlens-source-generation"] == "1"
            assert result.headers["x-cloudlens-observed-generation"] == "pending:committed-data"
            assert result.headers["x-cloudlens-revalidating"] == "true"
        assert calls == [1]
        with pytest.raises(HTTPException) as exc:
            await cache.get(request("fresh=1"), origin, auth)
        assert exc.value.status_code == 503
        assert exc.value.headers["X-CloudLens-Cache"] == "source-pending"
        cache.store.generation = "ready-data"
        result = await cache.get(request("fresh=1"), origin, auth)
        assert json.loads(result.body)["total"] == 2
        assert result.headers["x-cloudlens-stale"] == "false"
        await cache.close()

    asyncio.run(scenario())


def test_pending_generation_cache_miss_cannot_compute_unready_rollups():
    app, cache, auth, calls = FastAPI(), make_cache(), Authorize(), []
    cache.store.generation = "pending:committed-data"

    @app.get(PATH)
    async def endpoint(request: Request, days: int = 30):
        calls.append(days)
        return JSONResponse({"total": 12})

    install_snapshot_routes(app, lambda: cache, auth)
    with TestClient(app) as client:
        assert client.get(PATH + "?days=invalid").status_code == 422
        for suffix in ("", "?fresh=1"):
            result = client.get(PATH + suffix)
            assert result.status_code == 503
            assert result.headers["x-cloudlens-cache"] == "source-pending"
            assert result.headers["x-cloudlens-revalidating"] == "true"
        auth.denied = True
        assert client.get(PATH).status_code == 403
        assert calls == []
        assert not cache.store.rows


@pytest.mark.postgres
@pytest.mark.usefixtures("ready_cache_warehouse")
@pytest.mark.parametrize("partial", [False, True])
def test_postgres_new_cache_instance_reads_persistent_snapshot(postgres_warehouse, partial):
    store = SnapshotStore(postgres_warehouse.connect)

    async def scenario():
        auth = Authorize()
        first = make_cache(store)
        await first.start()

        async def origin():
            return JSONResponse({"total": 123, "currency": "USD",
                                 "status": "partial" if partial else "ok",
                                 "services": [{"tokens": {"status": "partial" if partial else "ok",
                                                         "value": 123, "samples": 744}}]})

        original = await first.get(request(), origin, auth)
        await first.close()
        restarted = make_cache(SnapshotStore(postgres_warehouse.connect))
        await restarted.start()

        async def must_not_run():
            raise AssertionError("A persisted valid hit must not call Azure/origin")

        result = await restarted.get(request(), must_not_run, auth)
        assert result.body == original.body
        assert result.headers["x-cloudlens-cache"] == "hit"
        assert result.headers["x-cloudlens-coverage"] == ("partial" if partial else "as-reported")
        incompatible = SnapshotCache(restarted.store, version="calculation-v2",
                                     policies={PATH: POLICY})
        with pytest.raises(AssertionError):
            await incompatible.get(request(), must_not_run, auth)
        await incompatible.close()
        await restarted.close()

    asyncio.run(scenario())


@pytest.mark.postgres
@pytest.mark.usefixtures("ready_cache_warehouse")
def test_postgres_generation_cas_bounds_and_failure_sanitization(postgres_warehouse):
    from app.snapshot_cache import Snapshot

    store = SnapshotStore(postgres_warehouse.connect, max_entries=2,
                          max_bytes=32, max_payload=25)
    store.initialize()

    def item(key, created, body=b'{"total":12}', generation="0"):
        return Snapshot(key, "partition", generation, body, created, created + 10,
                        created + 100, '"etag"')

    assert store.put(item("a", 100))
    assert store.put(item("b", 101))
    assert store.put(item("c", 102))
    assert store.load("a", "partition", 105)[0] is None
    assert store.load("c", "different-partition", 105)[0] is None
    assert not store.put(item("big", 103, b"x" * 26))
    assert not store.put(item("c", 100, b'{"total":0}'))
    assert store.load("c", "partition", 105)[0].body == b'{"total":12}'
    store.failure(item("c", 102), 105, "source_failed")
    loaded = store.load("c", "partition", 105)[0]
    assert loaded.error == "source_failed"
    assert loaded.retry_at == 110
    with postgres_warehouse.connect() as con:
        con.execute("UPDATE meta SET value=? WHERE key=?", ["1", "data_generation"])
    assert not store.put(item("c", 106))
    assert store.load("c", "partition", 105)[0].body == b'{"total":12}'
    with postgres_warehouse.connect() as con:
        row = con.execute("SELECT COUNT(*),SUM(byte_size) FROM private_snapshots").fetchone()
    assert row[0] <= 2 and row[1] <= 32
    assert store.load("c", "partition", 203)[0] is None
    store.prune(203)
    with postgres_warehouse.connect() as con:
        assert con.execute("SELECT COUNT(*) FROM private_snapshots").fetchone()[0] == 0


@pytest.mark.postgres
@pytest.mark.usefixtures("ready_cache_warehouse")
def test_stranded_committed_outbox_cannot_block_coherent_integer_revision(postgres_warehouse):
    from app.snapshot_cache import read_generation

    with postgres_warehouse.connect() as con:
        assert read_generation(con) == "0"
        con.execute("INSERT INTO meta(key,value) VALUES (?,?)",
                    ["data-publication:synthetic", '{"id":"test","status":"committed"}'])
        assert read_generation(con) == "0"
        con.execute("UPDATE meta SET value=? WHERE key=?",
                    ['{"id":"test","status":"ready"}', "data-publication:synthetic"])
        assert read_generation(con) == "0"


@pytest.mark.postgres
@pytest.mark.usefixtures("ready_cache_warehouse")
def test_global_counter_readiness_is_deliberately_shared_across_selected_scopes(postgres_warehouse):
    from app import jobs
    from app.snapshot_cache import read_generation

    with postgres_warehouse.connect() as con:
        assert read_generation(con, ("a",)) == read_generation(con, ("b",)) == "0"
        con.execute("UPDATE meta SET value=? WHERE key=?", ["1", "data_generation"])
        assert read_generation(con, ("a",)) == read_generation(con, ("b",)) == "pending:1:0"
        con.execute("UPDATE meta SET value=? WHERE key=?", ["1", "data_rollup_generation"])
        jobs._write_dirty(con, 1, 1, [])
        assert read_generation(con, ("a",)) == read_generation(con, ("b",)) == "1"


@pytest.mark.postgres
@pytest.mark.usefixtures("ready_cache_warehouse")
def test_default_cache_revision_fences_pending_maintenance_without_a_source_write(postgres_warehouse):
    from app import jobs
    from app.snapshot_cache import Snapshot, read_generation

    wh = postgres_warehouse
    store = SnapshotStore(wh.connect)
    store.initialize()
    snapshot = Snapshot("maintenance", "partition", "0", b'{"total":12}',
                        100, 110, 200, '"etag"')
    assert store.put(snapshot)
    jobs.request_rollup(wh, None)
    with wh.connect() as con:
        assert jobs._generation_pair(con) == (0, 0)
        assert not jobs.rollup_current(con)
        assert read_generation(con) == "pending:0:0"
    assert not store.put(snapshot)
    wh.refresh_rollup(strict=True)
    with wh.connect() as con:
        assert read_generation(con) == "0"
    assert store.put(snapshot)


@pytest.mark.postgres
@pytest.mark.usefixtures("ready_cache_warehouse")
def test_focus_transaction_counter_fences_writes_independently_of_outbox_promotion(postgres_warehouse):
    from concurrent.futures import ThreadPoolExecutor

    from app import jobs
    from app.snapshot_cache import PUBLICATION_LOCK, Snapshot, read_generation

    store = SnapshotStore(postgres_warehouse.connect)
    store.initialize()
    old = Snapshot("key", "partition", "0", b'{"total":12}', 100, 110, 200, '"etag"',
                   scope=("a",))
    begun = threading.Event()

    def obsolete_writer():
        begun.set()
        return store.put(old)

    with ThreadPoolExecutor(max_workers=1) as executor:
        with postgres_warehouse.connect() as con:
            con.execute("BEGIN")
            con.execute("SELECT pg_advisory_xact_lock(?)", [PUBLICATION_LOCK])
            jobs.lock_generation(con)
            jobs.advance_generation(con)
            con.execute("INSERT INTO meta(key,value) VALUES (?,?)",
                        ["data-publication:atomic", '{"id":"atomic","scope":["a"],"status":"committed"}'])
            with postgres_warehouse.connect() as reader:
                assert read_generation(reader, ("a",)) == "0"
            pending_writer = executor.submit(obsolete_writer)
            assert begun.wait(5)
            assert not pending_writer.done()
            con.execute("COMMIT")
        assert pending_writer.result(timeout=5) is False
    with postgres_warehouse.connect() as con:
        pending = read_generation(con, ("a",))
        assert pending.startswith("pending:")
        assert read_generation(con).startswith("pending:")
        assert read_generation(con, ("b",)) == pending
        # Notifications do not promote an incoherent source/rollup revision.
        con.execute("INSERT INTO meta(key,value) VALUES (?,?)",
                    ["data-publication:later", '{"id":"later","scope":["a"],"status":"ready"}'])
        assert read_generation(con, ("a",)).startswith("pending:")
    assert not store.put(replace(old, generation=pending))
    with postgres_warehouse.connect() as con:
        con.execute("UPDATE meta SET value=?,updated_at=now() WHERE key=?",
                    ['{"id":"atomic","scope":["a"],"status":"ready"}', "data-publication:atomic"])
        assert read_generation(con, ("a",)).startswith("pending:")
    with jobs.coherent_rollup(postgres_warehouse):
        pass
    with postgres_warehouse.connect() as con:
        ready = read_generation(con, ("a",))
    assert not ready.startswith("pending:")
    assert store.put(replace(old, generation=ready))


@pytest.mark.postgres
@pytest.mark.usefixtures("ready_cache_warehouse")
def test_generation_does_not_treat_uuid_or_transaction_timestamp_as_commit_order(postgres_warehouse):
    from app import jobs
    from app.snapshot_cache import read_generation

    with postgres_warehouse.connect() as con:
        con.execute("INSERT INTO meta(key,value,updated_at) VALUES (?,?,?)",
                    ["data-publication:z", '{"id":"z","scope":["a"],"status":"ready"}',
                     "2026-09-15T10:00:00Z"])
        first = read_generation(con, ("a",))
        # Later commit from an earlier-started transaction, with a lower random ID.
        con.execute("INSERT INTO meta(key,value,updated_at) VALUES (?,?,?)",
                    ["data-publication:a", '{"id":"a","scope":["a"],"status":"ready"}',
                     "2026-09-15T09:00:00Z"])
        assert read_generation(con, ("a",)) == first
        con.execute("UPDATE meta SET value=? WHERE key IN (?,?)",
                    ["1", "data_generation", "data_rollup_generation"])
        jobs._write_dirty(con, 1, 1, [])
        second = read_generation(con, ("a",))
        assert second == "1"
        assert second != first
        con.execute("UPDATE meta SET updated_at=now() WHERE key=?", ["data-publication:a"])
        assert read_generation(con, ("a",)) == second
        con.execute("INSERT INTO meta(key,value) VALUES (?,?)",
                    ["data-publication:unrelated", '{"id":"other","scope":["b"],"status":"ready"}'])
        assert read_generation(con, ("a",)) == second


@pytest.mark.postgres
@pytest.mark.usefixtures("ready_cache_warehouse")
def test_generic_source_and_rollup_counters_both_fence_cache_publication(postgres_warehouse):
    from concurrent.futures import ThreadPoolExecutor

    from app import jobs
    from app.snapshot_cache import Snapshot, read_generation

    store = SnapshotStore(postgres_warehouse.connect)
    store.initialize()
    original = Snapshot("generic", "partition", "0", b'{"total":12}', 100, 110, 200,
                        '"etag"', scope=("a",))
    assert store.put(original)
    begun = threading.Event()

    def old_writer():
        begun.set()
        return store.put(replace(original, created=101))

    with ThreadPoolExecutor(max_workers=1) as executor:
        with postgres_warehouse.connect() as con:
            con.execute("BEGIN")
            jobs.lock_generation(con)
            jobs.advance_generation(con)
            future = executor.submit(old_writer)
            assert begun.wait(5)
            assert not future.done()
            con.execute("COMMIT")
        assert future.result(timeout=5) is False
    with postgres_warehouse.connect() as con:
        pending = read_generation(con, ("a",))
        assert pending.startswith("pending:")
    assert not store.put(replace(original, generation=pending))
    assert store.load("generic", "partition", 105, ("a",))[0].body == original.body
    assert jobs.read_generation() == 1
    assert not jobs.generation_ready(1)
    with jobs.coherent_rollup(postgres_warehouse):
        pass
    assert jobs.generation_ready(1)
    with postgres_warehouse.connect() as con:
        ready = read_generation(con, ("a",))
    assert not ready.startswith("pending:")
    assert ready != original.generation
    assert store.put(replace(original, generation=ready, created=102))
    with postgres_warehouse.connect() as con:
        con.execute("UPDATE meta SET value=? WHERE key=?", ["0", "data_rollup_generation"])
    assert not store.put(replace(original, generation=ready, created=103))


@pytest.mark.postgres
@pytest.mark.parametrize("source", [None, 0, 5])
def test_missing_rollup_revision_is_unready_even_for_generation_zero(postgres_warehouse, source):
    from app import jobs
    from app.snapshot_cache import read_generation

    with postgres_warehouse.connect() as con:
        con.execute("DELETE FROM meta WHERE key IN (?,?)",
                    ["data_generation", "data_rollup_generation"])
        if source is not None:
            con.execute("INSERT INTO meta(key,value) VALUES (?,?)", ["data_generation", str(source)])
        assert read_generation(con, ("a",)).startswith("pending:")
        assert read_generation(con).startswith("pending:")
    assert not jobs.generation_ready(source or 0)

    async def scenario():
        cache, auth, calls = make_cache(SnapshotStore(postgres_warehouse.connect)), Authorize(), []
        await cache.start()

        async def origin():
            calls.append(1)
            return JSONResponse({"total": 12})

        with pytest.raises(HTTPException) as exc:
            await cache.get(request(), origin, auth)
        assert exc.value.status_code == 503
        assert exc.value.headers["X-CloudLens-Cache"] == "source-pending"
        assert calls == []
        # Parent lifespan schedules this one recovery rebuild, not a cost factory.
        await postgres_warehouse.refresh_rollup_async()
        assert jobs.generation_ready(source or 0)
        result = await cache.get(request(), origin, auth)
        assert result.status_code == 200
        assert result.headers["x-cloudlens-stale"] == "false"
        assert calls == [1]
        await cache.close()

    asyncio.run(scenario())


@pytest.mark.postgres
@pytest.mark.usefixtures("ready_cache_warehouse")
@pytest.mark.parametrize("preserve_detail", [True, False], ids=["quick", "details"])
def test_actual_warehouse_replace_invalidates_cache_without_focus_outbox(
    postgres_warehouse, preserve_detail,
):
    from app import jobs
    from app.warehouse import COLUMNS, COLUMNS_INDEX

    wh = postgres_warehouse
    day = "2026-06-17"

    def row(amount):
        record = [None] * len(COLUMNS)
        for name, value in {"SubAccountId": "a", "ChargePeriodStart": day,
                            "BilledCost": amount, "BillingCurrency": "USD"}.items():
            record[COLUMNS_INDEX[name]] = value
        return record

    def total():
        with wh.connect(read_only=True) as con:
            return float(con.execute(
                'SELECT COALESCE(SUM("BilledCost"),0) FROM costs '
                'WHERE "SubAccountId"=? AND "ChargePeriodStart"=?', ["a", day],
            ).fetchone()[0])

    async def scenario():
        assert await wh._replace("a", day, day, [row(12)]) == 1
        await wh.refresh_rollup_async()
        assert jobs.generation_ready(1)
        cache, auth, calls = make_cache(SnapshotStore(wh.connect)), Authorize(), []
        await cache.start()

        async def origin():
            calls.append(1)
            return JSONResponse({"total": await asyncio.to_thread(total), "currency": "USD"})

        req = request("month=2026-06")
        first = await cache.get(req, origin, auth)
        assert json.loads(first.body)["total"] == 12
        assert first.headers["x-cloudlens-source-generation"] == "1"
        assert (await cache.get(req, origin, auth)).headers["x-cloudlens-cache"] == "hit"
        assert calls == [1]
        old, _ = await asyncio.to_thread(
            cache.store.load, cache_key(req, CONTEXT, cache.version, POLICY),
            CONTEXT.partition(), cache.clock(), CONTEXT.selected)

        assert await wh._replace(
            "a", day, day, [row(24)], preserve_detail=preserve_detail) == 1
        assert jobs.read_generation() == 2
        assert not jobs.generation_ready(2)
        pending = await cache.get(req, origin, auth)
        assert pending.body == first.body
        assert pending.headers["x-cloudlens-cache"] == "source-pending"
        with pytest.raises(HTTPException) as exc:
            await cache.get(request("month=2026-06&fresh=1"), origin, auth)
        assert exc.value.status_code == 503
        assert calls == [1]
        assert not await asyncio.to_thread(cache.store.put, replace(old, created=old.created + 1))

        await wh.refresh_rollup_async()
        assert jobs.generation_ready(2)
        updated = await cache.get(request("month=2026-06&fresh=1"), origin, auth)
        assert json.loads(updated.body)["total"] == 24
        assert updated.headers["x-cloudlens-source-generation"] == "2"
        assert updated.headers["x-cloudlens-stale"] == "false"
        assert calls == [1, 1]
        await cache.close()

        restarted = make_cache(SnapshotStore(wh.connect))
        await restarted.start()
        persisted = await restarted.get(req, origin, auth)
        assert persisted.body == updated.body
        assert persisted.headers["x-cloudlens-cache"] == "hit"
        assert calls == [1, 1]
        await restarted.close()

    asyncio.run(scenario())
    with wh.connect() as con:
        assert con.execute("SELECT COUNT(*) FROM meta WHERE key LIKE ?",
                           ["data-publication:%"]).fetchone()[0] == 0
