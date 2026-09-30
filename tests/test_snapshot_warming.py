"""Server-side snapshot warming: targets, `SnapshotCache.warm`, and replay through the app."""
from __future__ import annotations

import asyncio
import json
from dataclasses import replace

import httpx
import pytest
from fastapi import FastAPI, HTTPException, Request
from starlette.responses import JSONResponse, Response

from app.snapshot_cache import (
    WARM_SCOPE_KEY,
    CacheContext,
    Policy,
    SnapshotCache,
    SnapshotStore,
    WarmTarget,
    cache_key,
    install_snapshot_routes,
    warm_query,
    warm_target_key,
)
from app.snapshot_warmer import SnapshotWarmer

PATH = "/api/dashboard/resources"
POLICY = Policy("warehouse", 10, 100)
ALICE = CacheContext("file:alice", "local", None, None)
DAY = 86400.0


def request(query="", path=PATH, warm=False):
    scope = {"type": "http", "http_version": "1.1", "method": "GET", "path": path,
             "root_path": "", "scheme": "https", "server": ("test", 443),
             "query_string": query.encode(), "headers": []}
    if warm:
        scope[WARM_SCOPE_KEY] = True
    return Request(scope)


class MemoryStore:
    max_payload = 1_000_000

    def __init__(self):
        self.rows, self.targets = {}, {}
        self.generation, self.claimed = "1", None

    def initialize(self):
        pass

    def prune(self, now):
        pass

    def load(self, key, partition, now, scope=None):
        item = self.rows.get(key)
        if item and (item.partition != partition or item.retain_until <= now):
            item = None
        return item, self.generation

    def load_family(self, family_key, partition, now, scope=None):
        rows = [item for item in self.rows.values()
                if item.family_key == family_key and item.partition == partition
                and item.retain_until > now]
        return max(rows, key=lambda item: (item.created, item.key), default=None)

    def put(self, item):
        if item.generation != self.generation:
            return False
        self.rows[item.key] = item
        return True

    def failure(self, item, now, code):
        current = self.rows.get(item.key)
        if current and current.created == item.created:
            self.rows[item.key] = replace(current, failures=current.failures + 1,
                                          error=code, retry_at=now + 5)

    def discard(self, key, partition):
        self.rows.pop(key, None)

    def record_target(self, target, now, seconds, used):
        existing = self.targets.get(target.key)
        if used:
            self.targets[target.key] = replace(target, seconds=seconds, used=now)
        elif existing is not None:
            self.targets[target.key] = replace(existing, seconds=seconds)

    def touch_target(self, key, now):
        existing = self.targets.get(key)
        if existing is not None and existing.used < now:
            self.targets[key] = replace(existing, used=now)

    def warm_targets(self, since, min_seconds, limit):
        rows = [t for t in self.targets.values() if t.used > since and t.seconds >= min_seconds]
        return sorted(rows, key=lambda t: (-t.used, t.key))[:limit]

    def claim_warm(self, token):
        if token == self.claimed:
            return False
        self.claimed = token
        return True

    def current_generation(self):
        return self.generation


class Authorize:
    def __init__(self, context=ALICE):
        self.context, self.denied = context, False

    async def __call__(self, request):
        if self.denied:
            raise HTTPException(403, "Access revoked")
        return self.context


def make_cache(store=None, version="calculation-v1", policy=POLICY, **kwargs):
    kwargs.setdefault("warm_min_seconds", 0)
    return SnapshotCache(store or MemoryStore(), version=version,
                         policies={PATH: policy}, **kwargs)


async def drain(cache):
    await asyncio.gather(*list(cache._tasks.values()), return_exceptions=True)
    await asyncio.gather(*list(cache._db_tasks), return_exceptions=True)


# ------------------------------------------------------------------ SnapshotCache.warm
def test_warm_computes_a_missing_view_once_then_reports_it_fresh():
    async def scenario():
        cache, calls = make_cache(), []

        async def origin():
            calls.append(1)
            return JSONResponse({"total": 5})

        assert await cache.warm(request("days=7"), origin, Authorize()) == "stored"
        assert await cache.warm(request("days=7"), origin, Authorize()) == "fresh"
        viewed = await cache.get(request("days=7"), origin, Authorize())
        assert viewed.headers["x-cloudlens-cache"] == "hit"
        assert json.loads(viewed.body) == {"total": 5}
        assert calls == [1]
        await cache.close()

    asyncio.run(scenario())


def test_warm_recomputes_after_a_publish_and_after_a_deploy():
    async def scenario():
        store, calls = MemoryStore(), []

        async def origin():
            calls.append(1)
            return JSONResponse({"total": len(calls)})

        first = make_cache(store)
        assert await first.warm(request(), origin, Authorize()) == "stored"
        store.generation = "2"
        assert await first.warm(request(), origin, Authorize()) == "stored"
        published = await first.get(request(), origin, Authorize())
        assert (published.headers["x-cloudlens-cache"], json.loads(published.body)) == (
            "hit", {"total": 2})
        await first.close()

        deployed = make_cache(store, version="calculation-v2")
        assert await deployed.warm(request(), origin, Authorize()) == "stored"
        viewed = await deployed.get(request(), origin, Authorize())
        assert (viewed.headers["x-cloudlens-cache"], json.loads(viewed.body)) == (
            "hit", {"total": 3})
        assert len(calls) == 3
        await deployed.close()

    asyncio.run(scenario())


def test_a_viewer_joins_the_warmers_calculation_instead_of_starting_another():
    async def scenario():
        cache, calls, gate = make_cache(), [], asyncio.Event()

        async def origin():
            calls.append(1)
            await gate.wait()
            return JSONResponse({"total": 9})

        warming = asyncio.create_task(cache.warm(request(), origin, Authorize()))
        while not cache._tasks:
            await asyncio.sleep(0)
        viewing = asyncio.create_task(cache.get(request(), origin, Authorize()))
        await asyncio.sleep(0.05)
        gate.set()
        assert await warming == "stored"
        viewed = await viewing
        # Joined the calculation ("miss") or, had it already finished, read its result.
        assert viewed.headers["x-cloudlens-cache"] in {"miss", "hit"}
        assert json.loads(viewed.body) == {"total": 9}
        assert calls == [1]
        await cache.close()

    asyncio.run(scenario())


def test_warm_waits_for_pending_rollups_and_honours_failure_back_off():
    async def scenario():
        now = [1000.0]
        store, calls = MemoryStore(), []
        cache = make_cache(store, clock=lambda: now[0])
        store.generation = "pending:2:1"

        async def origin():
            calls.append(1)
            return JSONResponse({"total": 1})

        assert await cache.warm(request(), origin, Authorize()) == "source-pending"
        assert calls == []
        store.generation = "2"
        assert await cache.warm(request(), origin, Authorize()) == "stored"

        async def failing():
            calls.append(1)
            raise HTTPException(502, "Azure said no")

        store.generation = "3"
        assert await cache.warm(request(), failing, Authorize()) == "failed-502"
        assert await cache.warm(request(), failing, Authorize()) == "backoff"
        assert len(calls) == 2
        await cache.close()

    asyncio.run(scenario())


def test_warm_cannot_compute_what_the_principal_may_not_see():
    async def scenario():
        cache, auth, calls = make_cache(), Authorize(), []
        auth.denied = True

        async def origin():
            calls.append(1)
            return JSONResponse({"total": 1})

        assert await cache.warm(request(), origin, auth) == "denied-403"
        assert await cache.warm(request(path="/api/audit"), origin, auth) == "ineligible"
        assert calls == [] and not cache.store.rows
        await cache.close()

    asyncio.run(scenario())


# ------------------------------------------------------------------ targets
def test_warm_query_replays_to_the_same_cache_key():
    original = request("scope=B,a&days=30&force=true&days=7")
    assert warm_query(original) == "days=30&days=7&scope=a%2Cb"
    replayed = request(warm_query(original))
    assert cache_key(replayed, ALICE, "v", POLICY) == cache_key(original, ALICE, "v", POLICY)


def test_only_a_persons_use_creates_or_renews_a_target():
    async def scenario():
        now = [1000.0]
        store = MemoryStore()
        # A TTL long enough that every read below is a hit, not a refresh of its own.
        cache = make_cache(store, policy=Policy("warehouse", 10_000, 100_000),
                           clock=lambda: now[0])

        async def origin():
            return JSONResponse({"total": 3})

        await cache.get(request("days=7"), origin, Authorize())
        await drain(cache)
        key = warm_target_key("file:alice", "local", PATH, "days=7")
        target = store.targets[key]
        assert (target.principal, target.tenant, target.path, target.query, target.used) == (
            "file:alice", "local", PATH, "days=7", 1000.0)

        now[0] = 1100.0
        store.generation = "2"
        assert await cache.warm(request("days=7"), origin, Authorize()) == "stored"
        await drain(cache)
        assert store.targets[key].used == 1000.0

        now[0] = 1200.0
        await cache.get(request("days=7"), origin, Authorize())
        await drain(cache)
        assert store.targets[key].used == 1200.0
        now[0] = 1300.0
        await cache.get(request("days=7"), origin, Authorize())
        await drain(cache)
        assert store.targets[key].used == 1200.0, "touched at most hourly per process"
        now[0] = 1200.0 + 3601
        await cache.get(request("days=7", warm=True), origin, Authorize())
        await drain(cache)
        assert store.targets[key].used == 1200.0, "the warmer's own read is not a use"
        await cache.get(request("days=7"), origin, Authorize())
        await drain(cache)
        assert store.targets[key].used == 1200.0 + 3601
        await cache.close()

    asyncio.run(scenario())


def test_cheap_views_and_unreplayable_principals_are_not_targets():
    async def scenario():
        store = MemoryStore()

        async def origin():
            return JSONResponse({"total": 3})

        cheap = make_cache(store, warm_min_seconds=60)
        await cheap.get(request(), origin, Authorize())
        await drain(cheap)
        await cheap.close()
        anonymous = make_cache(store)
        await anonymous.get(request("days=1"), origin,
                            Authorize(CacheContext("session:abc", "tenant", None, None)))
        await drain(anonymous)
        await anonymous.close()
        assert store.targets == {}

    asyncio.run(scenario())


# ------------------------------------------------------------------ SnapshotWarmer
def _app(holder, calls):
    app = FastAPI()

    @app.middleware("http")
    async def sign_in(request, call_next):
        request.state.user = request.cookies.get("session")
        if request.state.user is None:
            return JSONResponse({"detail": "Sign in to continue."}, status_code=401)
        return await call_next(request)

    @app.get(PATH)
    async def resources(request: Request, days: int = 30):
        calls.append((request.state.user, days))
        return JSONResponse({"total": days, "who": request.state.user})

    class AppAuthorize:
        async def __call__(self, request):
            return CacheContext(f"file:{request.state.user}", "local", None, None)

    install_snapshot_routes(app, lambda: holder["cache"], AppAuthorize(), paths={PATH})
    return app


async def _credentials(principal, tenant):
    if tenant != "local" or not principal.startswith("file:"):
        return None
    return {"cookie": "session=" + principal.removeprefix("file:")}


def test_warmer_replays_used_views_as_their_owner_after_deploy_publish_and_day():
    async def scenario():
        now = [10 * DAY + 3600]
        store, calls = MemoryStore(), []
        holder = {"cache": make_cache(store, clock=lambda: now[0])}
        app = _app(holder, calls)
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            for who, days in (("alice", 7), ("bob", 30)):
                viewed = await client.get(PATH, params={"days": days},
                                          headers={"cookie": f"session={who}"})
                assert viewed.headers["x-cloudlens-cache"] == "miss"
            await drain(holder["cache"])
            assert len(store.targets) == 2
            await holder["cache"].close()

            holder["cache"] = make_cache(store, version="calculation-v2", clock=lambda: now[0])
            warmer = SnapshotWarmer(app, lambda: holder["cache"], _credentials,
                                    min_seconds=0, clock=lambda: now[0])
            summary = await warmer.tick()
            assert (summary["reason"], summary["targets"], summary["stored"]) == (
                "startup", 2, 2)
            assert sorted(calls[2:]) == [("alice", 7), ("bob", 30)]
            for who, days in (("alice", 7), ("bob", 30)):
                viewed = await client.get(PATH, params={"days": days},
                                          headers={"cookie": f"session={who}"})
                assert viewed.headers["x-cloudlens-cache"] == "hit"
                assert viewed.json() == {"total": days, "who": who}
            assert len(calls) == 4
            assert await warmer.tick() is None, "nothing changed, so no second pass"

            store.generation = "2"
            assert (await warmer.tick())["reason"] == "publish"
            now[0] += DAY
            assert (await warmer.tick())["reason"] == "day"
            assert len(calls) == 8

            twin = SnapshotWarmer(app, lambda: holder["cache"], _credentials,
                                  min_seconds=0, clock=lambda: now[0])
            assert await twin.tick() is None, "the pass was already claimed by an instance"
            store.generation = "pending:3:2"
            assert await warmer.tick() is None
            assert len(calls) == 8
            await holder["cache"].close()

    asyncio.run(scenario())


def test_warmer_skips_people_it_cannot_sign_in_as_and_routes_that_stopped_caching():
    async def scenario():
        store, calls = MemoryStore(), []
        holder = {"cache": make_cache(store)}
        app = _app(holder, calls)
        for principal, tenant, path in (("oid-carol", "contoso", PATH),
                                        ("file:dave", "local", "/api/dashboard/retired"),
                                        ("file:erin", "local", PATH)):
            query = "days=5"
            store.targets[warm_target_key(principal, tenant, path, query)] = WarmTarget(
                warm_target_key(principal, tenant, path, query), principal, tenant, path, query,
                seconds=12.0, used=holder["cache"].clock())
        warmer = SnapshotWarmer(app, lambda: holder["cache"], _credentials)
        summary = await warmer.warm_all("startup")
        assert (summary["targets"], summary["no-identity"], summary["stored"]) == (2, 1, 1)
        assert calls == [("erin", 5)]
        await holder["cache"].close()

    asyncio.run(scenario())


def test_sign_in_warms_only_that_persons_views():
    async def scenario():
        store, calls = MemoryStore(), []
        holder = {"cache": make_cache(store)}
        app = _app(holder, calls)
        for who in ("alice", "bob"):
            key = warm_target_key(f"file:{who}", "local", PATH, "days=3")
            store.targets[key] = WarmTarget(key, f"file:{who}", "local", PATH, "days=3",
                                            seconds=5.0, used=holder["cache"].clock())
        warmer = SnapshotWarmer(app, lambda: holder["cache"], _credentials)
        warmer.warm_principal_soon("file:bob", "local")
        warmer.warm_principal_soon("file:bob", "local")
        await asyncio.gather(*list(warmer._principal_tasks.values()))
        assert calls == [("bob", 3)]
        assert warmer.last == {}, "a sign-in pass is not reported as the scheduled pass"
        await warmer.close()
        await holder["cache"].close()

    asyncio.run(scenario())


def test_a_view_that_fails_during_warming_does_not_stop_the_pass():
    async def scenario():
        store = MemoryStore()
        holder = {"cache": make_cache(store)}
        app = FastAPI()

        @app.middleware("http")
        async def sign_in(request, call_next):
            request.state.user = "alice"
            return await call_next(request)

        @app.get(PATH)
        async def resources(request: Request, days: int = 30):
            if days == 1:
                raise HTTPException(502, "Azure throttled")
            return JSONResponse({"total": days})

        install_snapshot_routes(app, lambda: holder["cache"], Authorize(), paths={PATH})
        for days in (1, 2):
            key = warm_target_key("file:alice", "local", PATH, f"days={days}")
            store.targets[key] = WarmTarget(key, "file:alice", "local", PATH, f"days={days}",
                                            seconds=5.0, used=holder["cache"].clock() - days)
        warmer = SnapshotWarmer(app, lambda: holder["cache"], _credentials)
        summary = await warmer.warm_all("startup")
        assert (summary["failed-502"], summary["stored"]) == (1, 1)
        await holder["cache"].close()

    asyncio.run(scenario())


# ------------------------------------------------------------------ PostgreSQL store
@pytest.fixture
def ready_cache_warehouse(postgres_warehouse):
    postgres_warehouse.refresh_rollup(strict=True)
    return postgres_warehouse


@pytest.mark.postgres
@pytest.mark.usefixtures("ready_cache_warehouse")
def test_postgres_targets_are_renewed_by_use_only_and_bounded(postgres_warehouse):
    store = SnapshotStore(postgres_warehouse.connect, max_targets=2, target_retention=100)
    store.initialize()

    def target(name):
        return WarmTarget(warm_target_key("file:alice", "local", PATH, name),
                          "file:alice", "local", PATH, name)

    store.record_target(target("days=1"), 1000, 4.0, True)
    store.record_target(target("days=2"), 1001, 0.5, True)
    store.record_target(target("days=9"), 1002, 7.0, False)
    assert [t.query for t in store.warm_targets(0, 0, 10)] == ["days=2", "days=1"]
    assert [t.query for t in store.warm_targets(0, 1.0, 10)] == ["days=1"]
    assert [t.query for t in store.warm_targets(1000, 0, 10)] == ["days=2"]

    store.record_target(target("days=1"), 1010, 30.0, False)
    renewed = {t.query: t for t in store.warm_targets(0, 0, 10)}
    assert (renewed["days=1"].seconds, renewed["days=1"].used) == (30.0, 1000)
    store.touch_target(target("days=1").key, 1020)
    store.touch_target(target("days=1").key, 1015)
    assert {t.query: t.used for t in store.warm_targets(0, 0, 10)}["days=1"] == 1020

    store.record_target(target("days=3"), 1030, 1.0, True)
    store.prune(1030)
    assert [t.query for t in store.warm_targets(0, 0, 10)] == ["days=3", "days=1"]
    store.prune(1125)
    assert [t.query for t in store.warm_targets(0, 0, 10)] == ["days=3"]

    assert store.claim_warm("token-a")
    assert not store.claim_warm("token-a")
    assert store.claim_warm("token-b")
    assert store.current_generation() == "0"


# ------------------------------------------------------------------ app wiring
def test_warm_credentials_rebuild_each_kind_of_principal(tmp_path, monkeypatch):
    from app import main
    from app.auth import COOKIE, Sessions, Users
    from app.entra import unsign

    for name in ("AUTH_USERS", "AUTH_PASSWORD", "AUTH_USERNAME"):
        monkeypatch.delenv(name, raising=False)
    users = Users(path=tmp_path / "users.json")
    users.add("Alice", "correct horse battery staple")
    sessions = Sessions(users, secret=b"s" * 48)

    class SSO:
        enabled = True

        def live_session(self, oid, tenant):
            if (oid, tenant) == ("oid-1", "contoso"):
                return type("Session", (), {"sid": "sid-1"})()
            return None

    class FakeAuth:
        enabled, allow_local = True, True

    auth = FakeAuth()
    auth.tokens = {"tok-123": "CI"}
    auth.users, auth.sessions, auth.sso = users, sessions, SSO()
    monkeypatch.setattr(main, "auth_service", lambda: auth)

    def resolve(principal, tenant):
        return asyncio.run(main.snapshot_warm_credentials(principal, tenant))

    cookie = resolve("file:alice", "local")["cookie"]
    name, _, value = cookie.partition("=")
    assert name == COOKIE and sessions.verify(value).name == "Alice"
    assert resolve("env:alice", "local") is None, "a file account is not an env account"
    assert resolve("file:mallory", "local") is None
    assert resolve("token:ci", "local") == {"authorization": "Bearer tok-123"}
    entra = resolve("oid-1", "contoso")["cookie"].partition("=")[2]
    assert unsign(entra, sessions.secret) == "sid-1"
    assert resolve("oid-2", "contoso") is None
    auth.allow_local = False
    assert resolve("file:alice", "local") is None, "SSO-only deployments have no local sign-in"
    auth.enabled = False
    assert resolve("disabled:local", "local") == {}


def test_live_session_is_the_most_recent_usable_one(monkeypatch):
    from app.entra import EntraSSO, Session

    for name in ("AUTH_CLIENT_ID", "AUTH_TENANT_ID"):
        monkeypatch.delenv(name, raising=False)
    sso = EntraSSO()

    def session(sid, oid, seen, **flags):
        made = Session(sid=sid, name=sid, email="", oid=oid, tenant="contoso", app=None,
                       account={})
        made.seen = seen
        for name, value in flags.items():
            setattr(made, name, value)
        return made

    for made in (session("old", "oid-1", 1), session("new", "oid-1", 5),
                 session("locked", "oid-1", 9, reauth_required=True),
                 session("other", "oid-2", 7)):
        sso._sessions[made.sid] = made
    assert sso.live_session("oid-1", "contoso").sid == "new"
    assert sso.live_session("oid-1", "fabrikam") is None
    assert sso.live_session("", "contoso") is None


def test_warm_requests_are_not_audited_as_someone_opening_a_tab(monkeypatch):
    from app import main
    from app.auth import User

    opened = []

    class Identify:
        def identify(self, cookie, authorization):
            return User("alice", source="file")

    monkeypatch.setattr(main, "_log_access", lambda user, tab: opened.append(tab))
    monkeypatch.setattr(main, "auth_service", lambda: Identify())
    monkeypatch.setattr(main, "read_only_mode", lambda: False)
    middleware = main.RequireAuth(app=None)

    async def call_next(request):
        return Response(status_code=204)

    for warm in (False, True):
        asyncio.run(middleware.dispatch(request(warm=warm), call_next))
    assert opened == ["resources"]


@pytest.mark.parametrize(("value", "expected"), [
    (None, False), ("false", False), ("0", False), ("true", True), ("1", True)])
def test_server_warming_is_opt_in(monkeypatch, value, expected):
    from app import main

    if value is None:
        monkeypatch.delenv("SNAPSHOT_SERVER_WARMING_ENABLED", raising=False)
    else:
        monkeypatch.setenv("SNAPSHOT_SERVER_WARMING_ENABLED", value)
    assert main.snapshot_server_warming_enabled() is expected


@pytest.mark.parametrize("value", ["perhaps", "-1", "nan", "inf"])
def test_warming_settings_are_validated(monkeypatch, value):
    from app import main

    monkeypatch.setenv("SNAPSHOT_SERVER_WARMING_ENABLED", "perhaps")
    with pytest.raises(ValueError, match="SNAPSHOT_SERVER_WARMING_ENABLED"):
        main.snapshot_server_warming_enabled()
    monkeypatch.setenv("SNAPSHOT_WARM_MIN_SECONDS", value)
    with pytest.raises(ValueError, match="SNAPSHOT_WARM_MIN_SECONDS"):
        main.snapshot_warm_min_seconds()


def test_replay_through_the_real_app_is_authorised_as_the_person_and_not_audited(monkeypatch):
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    from app import cost, main
    from app.auth import User

    sub, tenant, oid = ("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
                        "cccccccc-cccc-cccc-cccc-cccccccccccc",
                        "dddddddd-dddd-dddd-dddd-dddddddddddd")
    user = User("Reader", source="entra", email="reader@example.invalid", sid="session")
    session = SimpleNamespace(oid=oid, tenant=tenant, reauth_required=False,
                              subscriptions=[{"id": sub, "tenant": tenant}])
    presented = []

    def identify(cookie, authorization):
        presented.append(cookie)
        return user if cookie == "minted" else None

    auth = SimpleNamespace(identify=identify, sso=SimpleNamespace(
        delegated=False, enabled=False, get=lambda _sid: session))

    async def permitted(_user):
        return [sub]

    opened, contexts = [], []

    class RecordingCache:
        async def warm(self, request, _origin, authorize):
            contexts.append(await authorize(request))
            return "stored"

    monkeypatch.setattr(main, "_auth", auth)
    monkeypatch.setattr(main, "permitted", permitted)
    monkeypatch.setattr(main, "_log_access", lambda _user, tab: opened.append(tab))
    monkeypatch.setattr(main, "_snapshot_cache", RecordingCache())
    monkeypatch.setattr(cost, "_resolve", AsyncMock(return_value=[]))
    monkeypatch.delenv("CLOUDLENS_READ_ONLY", raising=False)
    monkeypatch.delenv("FX_RATES", raising=False)

    async def credentials(principal, tenant_id):
        return {"cookie": f"{main.COOKIE}=minted"}

    warmer = SnapshotWarmer(main.app, lambda: main._snapshot_cache, credentials)
    target = WarmTarget("k", oid, tenant, "/api/dashboard/executive", f"days=7&scope={sub}")
    assert asyncio.run(warmer._replay(target, {"cookie": f"{main.COOKIE}=minted"})) == "stored"
    assert presented == ["minted"]
    assert (contexts[0].principal, contexts[0].tenant, contexts[0].selected) == (
        oid, tenant, (sub,))
    assert opened == []
    signed_out = asyncio.run(warmer._replay(target, {}))
    assert signed_out == "http-401" and len(contexts) == 1


@pytest.mark.asyncio
async def test_lifespan_starts_the_warmer_only_when_asked_and_stops_it_before_the_cache(
        monkeypatch):
    import sys
    from types import ModuleType, SimpleNamespace
    from unittest.mock import AsyncMock

    from app import azure_updates, backfill, currency, jobs, main, onboarding, service_health

    events = []

    class Cache:
        def __init__(self, *_args, **kwargs):
            events.append(("min-seconds", kwargs.get("warm_min_seconds")))

        async def start(self):
            events.append("cache-start")

        async def close(self):
            events.append("cache-close")

    class Warmer:
        def __init__(self, app, cache, credentials, **kwargs):
            events.append(("warmer", kwargs["host"], kwargs["scheme"], kwargs["concurrency"]))

        def start(self):
            events.append("warmer-start")

        async def close(self):
            events.append("warmer-close")

    async def wait(name):
        await asyncio.Event().wait()

    async def prepare(_warehouse, state):
        state.snapshot_bootstrap_ready = True

    fake = SimpleNamespace(
        connect=lambda: None, check_readiness=lambda: True, summary=lambda: {"rows": 1},
        _store=SimpleNamespace(close=lambda: events.append("pool-close")))
    module = ModuleType("app.warehouse")
    module.warehouse = fake
    monkeypatch.setitem(sys.modules, "app.warehouse", module)
    monkeypatch.setenv("PROJECT_ENDPOINT", "https://example.invalid/projects/test")
    monkeypatch.setenv("CLOUDLENS_READ_ONLY", "true")
    monkeypatch.setenv("SNAPSHOT_CACHE_ENABLED", "true")
    monkeypatch.setenv("SNAPSHOT_WARM_MIN_SECONDS", "2.5")
    monkeypatch.setenv("SNAPSHOT_WARM_CONCURRENCY", "2")
    monkeypatch.setenv("PUBLIC_BASE_URL", "https://cloudlens.example.com")
    monkeypatch.setattr(main, "SnapshotCache", Cache)
    monkeypatch.setattr(main, "SnapshotWarmer", Warmer)
    monkeypatch.setattr(main, "application_version", lambda **_kwargs: "test-version")
    monkeypatch.setattr(main, "CostAgent", lambda: SimpleNamespace(model="test", close=AsyncMock()))
    monkeypatch.setattr(main, "_auth", SimpleNamespace(sso=SimpleNamespace(enabled=False)))
    monkeypatch.setattr(main, "_refresh_overview", lambda: wait("overview"))
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

    for enabled in ("false", "true"):
        events.clear()
        monkeypatch.setenv("SNAPSHOT_SERVER_WARMING_ENABLED", enabled)
        async with main.lifespan(main.app):
            await asyncio.sleep(0)
        assert ("min-seconds", 2.5) in events
        if enabled == "false":
            assert "warmer-start" not in events
            continue
        assert ("warmer", "cloudlens.example.com", "https", 2) in events
        assert events.index("warmer-start") < events.index("warmer-close")
        assert events.index("warmer-close") < events.index("cache-close")
        assert main._snapshot_warmer is None
