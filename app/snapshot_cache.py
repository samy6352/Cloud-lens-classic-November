"""Private, durable last-good JSON responses; authorization is supplied by the app.

Install with ``install_snapshot_routes`` *after* declaring routes. Its endpoint
wrapper runs after FastAPI validation, unlike a response-caching middleware.
The authorizer must recheck current session, entitlements and selected scope on
every call, and return stable identity identifiers, never session credentials.
Construct/start/close one cache inside the application lifespan. All store work
runs in threads; no Redis, browser persistence or Azure calls live in this module.

Normal expired reads use stale-while-revalidate. Explicit force/refresh/fresh
requests await source completion. Coverage is independent of freshness: completed
partial reports retain their original evidence and X-CloudLens-Coverage=partial.
Poll using ETag plus As-Of/Stale/Revalidating headers, including on HTTP 304.

AI Billing alone reuses a whole report until the scoped imported data day changes
(or explicit retry/retention expiry). Same-day corrections do not repeat telemetry
reads. Source-revision write fences, private keys and coverage rules still apply.
"""
from __future__ import annotations

import asyncio
import functools
import hashlib
import json
import logging
import math
import re
import time
from collections import OrderedDict
from collections.abc import Awaitable, Callable
from contextvars import ContextVar
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from fastapi import HTTPException
from fastapi.routing import APIRoute, request_response
from starlette.requests import Request
from starlette.responses import Response

from .store import PoolBusy

calculation_context: ContextVar[tuple[str, str] | None] = ContextVar(
    "cloudlens_snapshot_calculation", default=None)


def inner_cache_key(key: tuple[Any, ...]) -> tuple[Any, ...]:
    """Inner scans must not certify an older source/FX result as a new snapshot."""
    context = calculation_context.get()
    return (*key, context) if context is not None else key

SOURCE_REVISION_KEY = "data_generation"
ROLLUP_REVISION_KEY = "data_rollup_generation"
# Keep job lock ordering; the source-row lock below supplies the revision fence.
PUBLICATION_LOCK = 741209031
_CACHE_LOCK = 741209032
log = logging.getLogger("cloudlens.snapshot_cache")
_FORCE_FLAGS = frozenset({"force", "refresh", "fresh"})
_SECRET_KEYS = frozenset({
    "access_token", "refresh_token", "id_token", "client_secret", "password",
    "authorization", "cookie", "set-cookie", "sas_token", "connection_string",
    "token", "api_key", "api-key", "sig", "session_id", "session_cookie", "credentials",
})
_SECRET_VALUE = re.compile(
    r"(?:[?&](?:sig|access_token|refresh_token|id_token|api_key|token)=|\bBearer\s+"
    r"|\beyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+"
    r"|\b(?:session_cookie|session_id|client_secret|password)\s*=)",
    re.IGNORECASE,
)
_ACTIVE = frozenset({
    "queued", "running", "pending", "loading", "waiting-for-azure", "downloading",
    "validating", "committing", "warming-cache", "in_progress", "in-progress",
})
_FAILED = frozenset({"failed", "error", "unavailable", "cancelled"})
_JOB_FIELDS = frozenset({
    "job", "jobs", "task", "tasks", "refresh_job", "ingest_job", "progress", "scan",
})
_EVIDENCE_FIELDS = frozenset({
    "cost", "total", "total_cost", "billed_cost", "amount", "value", "input", "output",
    "input_tokens", "output_tokens", "saving", "savings",
})
_ROW_FIELDS = frozenset({
    "resources", "services", "models", "rows", "items", "opportunities", "recommendations",
    "metrics", "series", "daily",
})


@dataclass(frozen=True)
class CacheContext:
    principal: str
    tenant: str
    allowed: tuple[str, ...] | None
    selected: tuple[str, ...] | None
    authorization_version: str = ""
    fx_version: str = "native-v1"
    period_version: str = field(default_factory=lambda: datetime.now(UTC).date().isoformat())
    data_day: str = ""

    def partition(self) -> str:
        if not self.principal.strip() or not self.tenant.strip():
            raise HTTPException(401, "A stable authenticated identity is required.")
        allowed = _scopes(self.allowed)
        selected = _scopes(self.selected)
        if allowed is not None and (
            not allowed or not selected or not set(selected).issubset(allowed)
        ):
            raise HTTPException(403, "That subscription is outside your permitted scope.")
        return _digest([self.principal, self.tenant, allowed, selected,
                        self.authorization_version])


@dataclass(frozen=True)
class Policy:
    source: str
    ttl: float
    retention: float = 7 * 86400
    daily: bool = False

    def __post_init__(self):
        if self.ttl <= 0 or self.retention < self.ttl:
            raise ValueError("Cache retention must be at least its positive TTL.")


WAREHOUSE = Policy("warehouse", 300)
MONITOR = Policy("monitor", 900)
ADVISOR = Policy("advisor", 1800)
AI = Policy("ai", 900)
AI_BILLING = Policy("ai", 86400, daily=True)
# Exact paths only. Job/progress/auth/chat/download/write routes are not eligible.
DEFAULT_POLICIES = {
    "/api/overview": Policy("overview", 300),
    **dict.fromkeys((
        "/api/dashboard/sections", "/api/dashboard/executive",
        "/api/dashboard/billing",
        "/api/dashboard/aks", "/api/dashboard/anomalies",
        "/api/dashboard/tags", "/api/dashboard/resources",
        "/api/dashboard/rates", "/api/dashboard/esu",
        "/api/dashboard/hybrid-benefit", "/api/filters",
        "/api/dashboard/compute", "/api/dashboard/networking", "/api/dashboard/storage",
        "/api/dashboard/data", "/api/dashboard/integration", "/api/dashboard/security",
        "/api/dashboard/other", "/api/dashboard/marketplace", "/api/dashboard/uptime",
    ), WAREHOUSE),
    **dict.fromkeys((
        "/api/dashboard/rightsizing", "/api/dashboard/performance",
        "/api/dashboard/shutdown", "/api/dashboard/change-analysis",
        "/api/dashboard/waste", "/api/dashboard/savings", "/api/dashboard/health",
        "/api/dashboard/governance",
    ), MONITOR),
    "/api/dashboard/advisor": ADVISOR,
    "/api/dashboard/commitments": ADVISOR,
    "/api/dashboard/billing/opportunities": ADVISOR,
    "/api/dashboard/ai-billing": AI_BILLING,
    "/api/dashboard/recommendations": AI,
}


def _digest(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"),
                                     ensure_ascii=True).encode()).hexdigest()


def _scopes(values):
    if values is None:
        return None
    return sorted({re.sub(r"^/subscriptions/([^/]+)/?$", r"\1", value.strip().lower())
                   for value in values if value.strip()})


def application_version(directory: Path | None = None, *, build_id: str = "") -> str:
    """Hash app source as well as build ID: even an accidentally reused ID is safe."""
    root = directory or Path(__file__).resolve().parent
    digest = hashlib.sha256(build_id.encode())
    for path in sorted(root.rglob("*.py")):
        digest.update(path.relative_to(root).as_posix().encode())
        digest.update(path.read_bytes())
    return digest.hexdigest()


def cache_key(request: Request, context: CacheContext, version: str,
              policy: Policy) -> str:
    # Sorting names is safe; sorting/deduplicating repeated VALUES is not (last
    # value wins for scalar parameters, and list parameters preserve order).
    query: dict[str, list[str]] = {}
    for name, value in request.query_params.multi_items():
        if name in _FORCE_FLAGS:
            continue
        if name == "scope":
            value = ",".join(_scopes(value.split(",")))
        query.setdefault(name, []).append(value)
    return _digest([context.partition(), request.url.path, query, context.fx_version,
                    context.period_version,
                    version, policy.source])


def read_generation(con, scope: tuple[str, ...] | None = None) -> str:
    """Global committed-cost revision; scope still partitions each private cache key."""
    from . import jobs

    versions = dict(con.execute("SELECT key,value FROM meta WHERE key IN (?,?)",
                                [SOURCE_REVISION_KEY, ROLLUP_REVISION_KEY]).fetchall())
    source_value, ready_value = versions.get(SOURCE_REVISION_KEY), versions.get(ROLLUP_REVISION_KEY)
    source = int(source_value) if source_value is not None else 0
    ready = int(ready_value) if ready_value is not None else -1
    # Outbox IDs/status/timestamps are notifications, not source revisions.
    # A crash after a coherent rebuild but before outbox promotion must not
    # strand the cache, nor may an older event finishing later roll it back.
    # Maintenance can be pending without changing raw costs. Use the same readiness
    # gate as query routing, while retaining the numeric ready-header contract.
    return str(source) if jobs._ready(con, source, ready) else f"pending:{source}:{ready}"


def _source_pending(generation: str) -> bool:
    return generation.startswith("pending:")


@dataclass(frozen=True)
class Snapshot:
    key: str
    partition: str
    generation: str
    body: bytes
    created: float
    expires: float
    retain_until: float
    etag: str
    failures: int = 0
    retry_at: float = 0
    error: str = ""
    scope: tuple[str, ...] | None = None


class SnapshotStore:
    """Bounded PostgreSQL storage using the existing warehouse connection API."""

    def __init__(self, connect: Callable, *, generation: Callable = read_generation,
                 max_entries: int = 512, max_bytes: int = 64 * 1024 * 1024,
                 max_payload: int = 2 * 1024 * 1024):
        if min(max_entries, max_bytes, max_payload) < 1:
            raise ValueError("Snapshot bounds must be positive.")
        self.connect, self.generation = connect, generation
        self.max_entries, self.max_bytes = max_entries, max_bytes
        self.max_payload = min(max_payload, max_bytes)

    def initialize(self):
        with self.connect() as con:
            con.execute("BEGIN")
            try:
                con.execute("SELECT pg_advisory_xact_lock(?)", [_CACHE_LOCK])
                con.execute("""CREATE TABLE IF NOT EXISTS private_snapshots (
                    cache_key TEXT PRIMARY KEY, partition_hash TEXT NOT NULL,
                    source_generation TEXT NOT NULL, body TEXT NOT NULL,
                    created DOUBLE PRECISION NOT NULL, expires DOUBLE PRECISION NOT NULL,
                    retain_until DOUBLE PRECISION NOT NULL, etag TEXT NOT NULL,
                    byte_size BIGINT NOT NULL, failures INTEGER NOT NULL DEFAULT 0,
                    retry_at DOUBLE PRECISION NOT NULL DEFAULT 0,
                    error_code TEXT NOT NULL DEFAULT ''
                )""")
                con.execute("COMMIT")
            except BaseException:
                con.execute("ROLLBACK")
                raise

    def _prune(self, con, now):
        con.execute("DELETE FROM private_snapshots WHERE retain_until<=?", [now])
        con.execute(
            "DELETE FROM private_snapshots WHERE cache_key IN ("
            "SELECT cache_key FROM (SELECT cache_key,"
            "ROW_NUMBER() OVER (ORDER BY created DESC,cache_key) AS position,"
            "SUM(byte_size) OVER (ORDER BY created DESC,cache_key) AS total_bytes "
            "FROM private_snapshots) AS ranked WHERE position>? OR total_bytes>?)",
            [self.max_entries, self.max_bytes])

    def prune(self, now: float):
        with self.connect() as con:
            con.execute("BEGIN")
            try:
                con.execute("SELECT pg_advisory_xact_lock(?)", [_CACHE_LOCK])
                self._prune(con, now)
                con.execute("COMMIT")
            except BaseException:
                con.execute("ROLLBACK")
                raise

    def load(self, key: str, partition: str, now: float, scope=None):
        with self.connect() as con:
            generation = self.generation(con, scope)
            row = con.execute(
                "SELECT cache_key,partition_hash,source_generation,body,created,expires,"
                "retain_until,etag,failures,retry_at,error_code FROM private_snapshots "
                "WHERE cache_key=? AND partition_hash=? AND retain_until>?",
                [key, partition, now]).fetchone()
        if row:
            values = list(row)
            values[3] = values[3].encode("utf-8")
            return Snapshot(*values, scope=scope), generation
        return None, generation

    def put(self, item: Snapshot) -> bool:
        if len(item.body) > self.max_payload or _source_pending(item.generation):
            return False
        from .jobs import lock_generation

        with self.connect() as con:
            con.execute("BEGIN")
            try:
                con.execute("SELECT pg_advisory_xact_lock(?)", [PUBLICATION_LOCK])
                con.execute("SELECT pg_advisory_xact_lock(?)", [_CACHE_LOCK])
                # Generic cost writers and coherent_rollup use this row lock,
                # not necessarily the FOCUS publication advisory lock.
                lock_generation(con)
                if self.generation(con, item.scope) != item.generation:
                    con.execute("ROLLBACK")
                    return False
                row = con.execute(
                    "INSERT INTO private_snapshots "
                    "(cache_key,partition_hash,source_generation,body,created,expires,"
                    "retain_until,etag,byte_size) VALUES (?,?,?,?,?,?,?,?,?) "
                    "ON CONFLICT(cache_key) DO UPDATE SET "
                    "partition_hash=excluded.partition_hash,"
                    "source_generation=excluded.source_generation,body=excluded.body,"
                    "created=excluded.created,expires=excluded.expires,"
                    "retain_until=excluded.retain_until,etag=excluded.etag,"
                    "byte_size=excluded.byte_size,failures=0,retry_at=0,error_code='' "
                    "WHERE private_snapshots.created<=excluded.created RETURNING cache_key",
                    [item.key, item.partition, item.generation, item.body.decode("utf-8"),
                     item.created, item.expires, item.retain_until, item.etag,
                     len(item.body)]).fetchone()
                self._prune(con, item.created)
                con.execute("COMMIT")
                return bool(row)
            except BaseException:
                con.execute("ROLLBACK")
                raise

    def failure(self, item: Snapshot, now: float, code: str):
        # No exception text or source response is retained: it can contain tokens.
        if code not in {"source_incomplete", "source_failed", "cache_write_failed"}:
            raise ValueError("Unknown snapshot failure code.")
        delay = min(300, 5 * 2 ** min(item.failures, 6))
        with self.connect() as con:
            con.execute(
                "UPDATE private_snapshots SET failures=failures+1,retry_at=?,error_code=? "
                "WHERE cache_key=? AND partition_hash=? AND created=?",
                [now + delay, code, item.key, item.partition, item.created])

    def discard(self, key: str, partition: str):
        with self.connect() as con:
            con.execute("DELETE FROM private_snapshots WHERE cache_key=? AND partition_hash=?",
                        [key, partition])


def _safe_content(value: Any) -> bool:
    if isinstance(value, dict):
        for key, item in value.items():
            name = str(key).lower()
            if name in _SECRET_KEYS:
                return False
            if not _safe_content(item):
                return False
    elif isinstance(value, list):
        return all(_safe_content(item) for item in value)
    elif isinstance(value, str) and _SECRET_VALUE.search(value):
        return False
    return True


def _unfinished(value: Any, *, envelope=True, states=_ACTIVE | _FAILED) -> bool:
    if isinstance(value, dict):
        job = envelope or "job_id" in value or "task_id" in value
        if job and (
            any(str(value.get(name, "")).lower() in states
                for name in ("status", "state"))
            or any(value.get(flag) is True for flag in ("running", "pending", "loading"))
        ):
            return True
        return any(_unfinished(item, envelope=str(name).lower() in _JOB_FIELDS, states=states)
                   for name, item in value.items() if isinstance(item, (dict, list)))
    if isinstance(value, list):
        return any(_unfinished(item, envelope=envelope, states=states) for item in value)
    return False


def _partial_evidence(value: Any) -> bool:
    if isinstance(value, dict):
        for name, item in value.items():
            key = str(name).lower()
            if key in {"error", "errors", "problem"} and item:
                return True
            if ((key in {"status", "state"} or key.endswith("_status"))
                    and isinstance(item, str) and item.lower() in _FAILED | {"partial", "unknown"}):
                return True
            if key in {"partial", "unavailable", "truncated"} and item is True:
                return True
            if _partial_evidence(item):
                return True
    elif isinstance(value, list):
        return any(_partial_evidence(item) for item in value)
    return False


def _has_evidence(value: Any, *, row=False) -> bool:
    """Require actual money/measurements, not counts, windows or a failed zero summary."""
    if isinstance(value, dict):
        if str(value.get("status", "")).lower() in _FAILED | _ACTIVE:
            return False
        samples = value.get("samples", 0)
        measured = isinstance(samples, (int, float)) and samples > 0
        for name, item in value.items():
            if name in {"error", "errors", "problem", "notices", "notes", "coverage",
                        "metric_coverage", "inventory", "window", "metric_window"}:
                continue
            if (name in _EVIDENCE_FIELDS and isinstance(item, (int, float))
                    and not isinstance(item, bool) and math.isfinite(item)
                    and (item != 0 or measured or (row and value.get("currency")))):
                return True
            if isinstance(item, (dict, list)) and _has_evidence(
                item, row=row or name in _ROW_FIELDS
            ):
                return True
    elif isinstance(value, list):
        return any(_has_evidence(item, row=row) for item in value)
    return False


def _cacheable_report(value: Any) -> bool:
    if not isinstance(value, (dict, list)) or not _safe_content(value) or _unfinished(value):
        return False
    if isinstance(value, dict) and any(value.get(key) for key in ("error", "errors", "problem")):
        return False
    # Partial means evidence coverage, not an unfinished request. Keep all original
    # unknowns/notices/statuses; only failure-shaped empty reports are rejected.
    return not _partial_evidence(value) or _has_evidence(value)


def _body(response: Response, limit: int) -> bytes | None:
    body = getattr(response, "body", None)
    if (response.status_code != 200 or not isinstance(body, bytes) or len(body) > limit
            or "set-cookie" in response.headers
            or "content-encoding" in response.headers
            or "content-disposition" in response.headers
            or response.headers.get("content-type", "").split(";")[0] != "application/json"):
        return None
    try:
        value = json.loads(body)
        return body if _cacheable_report(value) else None
    except (ValueError, UnicodeError):
        return None


def _pending_body(response: Response, limit: int) -> bool:
    body = getattr(response, "body", None)
    if (response.status_code != 200 or not isinstance(body, bytes) or len(body) > limit
            or response.headers.get("content-type", "").split(";")[0] != "application/json"):
        return False
    try:
        return _unfinished(json.loads(body), states=_ACTIVE)
    except (ValueError, UnicodeError):
        return False


Authorizer = Callable[[Request], Awaitable[CacheContext]]


@dataclass(frozen=True)
class _Failure:
    created: float
    failures: int
    retry_at: float
    code: str
    persisted: bool = False


class SnapshotCache:
    def __init__(self, store: SnapshotStore, *, version: str,
                 policies: dict[str, Policy] | None = None, concurrency: int = 4,
                 max_inflight: int = 32, origin_timeout: float = 120,
                 clock: Callable[[], float] = time.time):
        if concurrency < 1 or max_inflight < concurrency or origin_timeout <= 0 or not version:
            raise ValueError("Invalid snapshot cache configuration.")
        self.store, self.version = store, version
        self.policies = dict(DEFAULT_POLICIES if policies is None else policies)
        self.clock, self.origin_timeout = clock, origin_timeout
        self.max_inflight = max_inflight
        self._slots = asyncio.Semaphore(concurrency)
        self._tasks: dict[tuple[str, str], asyncio.Task] = {}
        self._db_tasks: set[asyncio.Task] = set()
        self._completion_serial = 0
        self._maintenance: asyncio.Task | None = None
        self._maintenance_error = ""
        self._failures: OrderedDict[str, _Failure] = OrderedDict()
        self._closed = False

    async def start(self):
        await self._db(self.store.initialize)
        await self._db(self.store.prune, self.clock())
        if self._maintenance is None:
            self._maintenance = asyncio.create_task(self._maintain())

    async def _maintain(self):
        while not self._closed:
            await asyncio.sleep(300)
            await self._maintenance_pass()

    async def _maintenance_pass(self):
        try:
            await self._db(self.store.prune, self.clock())
            self._maintenance_error = ""
        except Exception as exc:
            self._maintenance_error = "maintenance_failed"
            self._log_failure("maintenance", None, exc)

    def _log_failure(self, operation, request, exc):
        path = request.url.path if request and request.url.path in self.policies else "<cache>"
        kind = re.sub(r"[^A-Za-z0-9_]", "", type(exc).__name__)[:80]
        log.warning("snapshot %s failed path=%s type=%s", operation, path, kind)

    def _unavailable(self, response, code):
        response.headers["Cache-Control"] = "private, no-store"
        response.headers["X-CloudLens-Cache"] = "unavailable"
        response.headers["X-CloudLens-Cache-Error"] = code
        return response

    def _source_state(self, response, state, generation):
        pending = state == "source-pending"
        response.headers.update({
            "Cache-Control": "private, no-store", "X-CloudLens-Cache": state,
            "X-CloudLens-Stale": "true", "X-CloudLens-Revalidating": str(pending).lower(),
            "X-CloudLens-Source-State": "pending" if pending else "incomplete",
            "X-CloudLens-Checked-At": datetime.fromtimestamp(self.clock(), UTC).isoformat(),
        })
        if generation:
            # A last-good body's source generation is unchanged while the new
            # publication waits. Keep both identities distinct for polling clients.
            response.headers.setdefault("X-CloudLens-Source-Generation", generation)
            response.headers["X-CloudLens-Observed-Generation"] = generation
        if pending:
            response.headers["Retry-After"] = "3"
            if "X-CloudLens-Refresh-Error" in response.headers:
                del response.headers["X-CloudLens-Refresh-Error"]
        return response

    async def _record_failure(self, old, request, code):
        if old is None:
            return
        notice = _Failure(old.created, old.failures + 1,
                          self.clock() + min(300, 5 * 2 ** min(old.failures, 6)), code)
        self._failures[old.key] = notice
        self._failures.move_to_end(old.key)
        while len(self._failures) > self.max_inflight:
            self._failures.popitem(last=False)
        try:
            await self._db(self.store.failure, old, self.clock(), code)
        except Exception as exc:
            self._log_failure("failure_write", request, exc)
        else:
            if self._failures.get(old.key) is notice:
                self._failures[old.key] = replace(notice, persisted=True)

    async def _db(self, function, *args):
        task = asyncio.create_task(asyncio.to_thread(function, *args))
        self._db_tasks.add(task)
        task.add_done_callback(self._db_finished)
        # Cancellation cannot stop a sync DB call. Keep its lifetime owned and
        # wait for it before the warehouse pool is closed at shutdown.
        return await asyncio.shield(task)

    def _db_finished(self, task):
        self._db_tasks.discard(task)
        if not task.cancelled():
            error = task.exception()
            if error is not None:
                self._log_failure("database", None, error)

    async def close(self):
        self._closed = True
        tasks = list(self._tasks.values())
        if self._maintenance is not None:
            tasks.append(self._maintenance)
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        self._tasks.clear()
        self._maintenance = None
        await asyncio.gather(*list(self._db_tasks), return_exceptions=True)

    async def _authorize(self, request, authorize, partition=None, expected=None):
        context = await authorize(request)
        current = context.partition()
        if partition is not None and current != partition:
            raise HTTPException(403, "Your permitted scope changed. Reload this view.")
        if expected is not None and (
            context.fx_version != expected.fx_version
            or context.period_version != expected.period_version
            or context.data_day != expected.data_day
        ):
            raise HTTPException(409, "The calculation context changed. Reload this view.")
        return context

    async def get(self, request: Request, origin: Callable[[], Awaitable[Response]],
                  authorize: Authorizer) -> Response:
        policy = self.policies.get(request.url.path)
        if (request.method != "GET" or policy is None
                or any(name.lower() in _SECRET_KEYS for name in request.query_params)):
            return await origin()
        context = await self._authorize(request, authorize)
        partition = context.partition()
        key = cache_key(request, context, self.version, policy)
        try:
            while True:
                completion_serial = self._completion_serial
                # A coalesced producer may have committed and left the task map
                # while this DB read was in flight. Recheck before issuing work.
                item, generation = await self._db(
                    self.store.load, key, partition, self.clock(), context.selected)
                if item is not None or completion_serial == self._completion_serial:
                    break
        except PoolBusy as exc:
            # Not a cache outage, so the fallback below is not the answer. The origin
            # reads the warehouse through the same pool that just refused this request;
            # calling it cannot succeed and only puts another waiter on the queue. Shed
            # the request instead, and say when to come back.
            self._log_failure("read", request, exc)
            raise HTTPException(503, "The warehouse is busy; retry shortly.",
                                headers={"Cache-Control": "private, no-store",
                                         "X-CloudLens-Cache": "unavailable",
                                         "X-CloudLens-Cache-Error": "pool_busy",
                                         "Retry-After": "5"}) from exc
        except Exception as exc:
            # Cache outage must retain the original route's validation/error contract.
            self._log_failure("read", request, exc)
            try:
                response = await origin()
                await self._authorize(request, authorize, partition, context)
            except HTTPException as origin_error:
                origin_error.headers = {
                    **(origin_error.headers or {}), "Cache-Control": "private, no-store",
                    "X-CloudLens-Cache": "unavailable", "X-CloudLens-Cache-Error": "read_failed",
                }
                raise
            return self._unavailable(response, "read_failed")
        notice = self._failures.get(key)
        if notice is not None:
            if item is not None and item.created == notice.created:
                item = replace(item, failures=max(item.failures, notice.failures),
                               retry_at=max(item.retry_at, notice.retry_at), error=notice.code)
            else:
                self._failures.pop(key, None)
        force = any(request.query_params.get(name, "").lower() in {"1", "true", "yes"}
                    for name in _FORCE_FLAGS)
        if _source_pending(generation):
            await self._authorize(request, authorize, partition, context)
            if force or item is None:
                raise HTTPException(503, "Committed source data is still being prepared.",
                                    headers={"X-CloudLens-Cache": "source-pending",
                                             "X-CloudLens-Stale": "true",
                                             "X-CloudLens-Revalidating": "true",
                                             "X-CloudLens-Source-State": "pending",
                                             "Retry-After": "3"})
            return self._source_state(
                self._response(request, item, stale=True, revalidating=True),
                "source-pending", generation)
        daily = policy.daily and bool(context.data_day)
        if daily and item:
            saved_day = json.loads(item.body).get("snapshot", {}).get("data_day")
            source_changed = saved_day != context.data_day
            # The source revision still fences writes. A completed daily report,
            # however, survives same-day imports and midnight without new data.
            stale = source_changed or bool(item.error)
        else:
            source_changed = bool(item and item.generation != generation)
            stale = bool(item and (item.expires <= self.clock() or source_changed or item.error))
        task_key = (key, generation)
        task = self._tasks.get(task_key)
        owns_origin = False
        if ((not item or stale or force)
                and (not item or force or (source_changed and not daily)
                     or item.retry_at <= self.clock())
                and task is None and not self._closed and len(self._tasks) < self.max_inflight):
            task = asyncio.create_task(self._refresh(
                request, origin, authorize, context, key, generation, policy, item))
            self._tasks[task_key] = task
            task.add_done_callback(functools.partial(self._finished, task_key))
            owns_origin = True
        if item and not force:
            await self._authorize(request, authorize, partition, context)
            return self._response(request, item, stale=stale or force,
                                  revalidating=bool(task and not task.done()))
        if task is None:
            # Bounded overload, rather than an unbounded queue or uncoalesced Azure fanout.
            raise HTTPException(503, "Snapshot refresh capacity is busy.",
                                headers={"Retry-After": "2"})
        result = await asyncio.shield(task)
        await self._authorize(request, authorize, partition, context)
        if isinstance(result, Snapshot):
            return self._response(request, result, stale=False, revalidating=False,
                                  state="refresh" if force else "miss")
        if force and _source_pending(generation) and result.status_code == 200:
            raise HTTPException(503, "Committed source data is still being prepared.",
                                headers={"X-CloudLens-Cache": "source-pending",
                                         "X-CloudLens-Stale": "true",
                                         "X-CloudLens-Revalidating": "true",
                                         "X-CloudLens-Source-State": "pending",
                                         "Retry-After": "3"})
        if force and result.headers.get("X-CloudLens-Cache") == "superseded":
            raise HTTPException(409, "Source data changed during refresh. Retry this view.",
                                headers={"X-CloudLens-Cache": "superseded",
                                         "X-CloudLens-Refresh-Error": "source_changed"})
        if owns_origin:
            return result
        # Do not share a streaming iterator, Set-Cookie, or any other non-cacheable
        # response between requests (even two sessions of the same principal).
        async with self._slots:
            return await origin()

    def _finished(self, key, task):
        self._completion_serial += 1
        if self._tasks.get(key) is task:
            self._tasks.pop(key, None)
        if not task.cancelled():
            task.exception()  # Background exceptions are observed, never logged with secrets.

    async def _refresh(self, request, origin, authorize, context, key, generation, policy, old):
        partition = context.partition()
        try:
            async with self._slots:
                await self._authorize(request, authorize, partition, context)
                started = self.clock()
                calculation = calculation_context.set((key, generation))
                try:
                    response = await asyncio.wait_for(origin(), self.origin_timeout)
                finally:
                    calculation_context.reset(calculation)
                await self._authorize(request, authorize, partition, context)
                if response.status_code in {401, 403}:
                    await self._db(self.store.discard, key, partition)
                    return response
                body = _body(response, self.store.max_payload)
                if body is not None and policy.daily and context.data_day:
                    value = json.loads(body)
                    value["snapshot"] = {"cadence": "data-day", "data_day": context.data_day}
                    response.body = json.dumps(value, separators=(",", ":"), ensure_ascii=True).encode()
                    response.headers["Content-Length"] = str(len(response.body))
                    body = _body(response, self.store.max_payload)
                if body is None:
                    if _pending_body(response, self.store.max_payload):
                        return self._source_state(response, "source-pending", generation)
                    self._log_failure("eligibility", request, ValueError())
                    await self._record_failure(old, request, "source_incomplete")
                    response.headers["X-CloudLens-Refresh-Error"] = "source_incomplete"
                    return self._source_state(response, "source-incomplete", generation)
                if _source_pending(generation):
                    return self._source_state(response, "source-pending", generation)
                item = Snapshot(key, partition, generation, body, started,
                                started + policy.ttl, started + policy.retention,
                                '"' + _digest([generation, self.version,
                                               hashlib.sha256(body).hexdigest()]) + '"',
                                scope=context.selected)
                try:
                    accepted = await self._db(self.store.put, item)
                except Exception as exc:
                    self._log_failure("write", request, exc)
                    await self._record_failure(old, request, "cache_write_failed")
                    response.headers["X-CloudLens-Refresh-Error"] = "cache_write_failed"
                    return self._unavailable(response, "write_failed")
                if accepted:
                    self._failures.pop(key, None)
                    return item
                # A generation changed while the source worked. Never mark the old
                # calculation fresh, nor allow it to replace the newer publication.
                response.headers["Cache-Control"] = "private, no-store"
                response.headers["X-CloudLens-Stale"] = "true"
                response.headers["X-CloudLens-Cache"] = "superseded"
                response.headers["X-CloudLens-Revalidating"] = "true"
                response.headers["Retry-After"] = "1"
                return response
        except HTTPException as exc:
            self._log_failure("refresh", request, exc)
            if exc.status_code in {401, 403}:
                try:
                    await self._db(self.store.discard, key, partition)
                except Exception as storage_error:
                    self._log_failure("discard", request, storage_error)
                    exc.headers = {**(exc.headers or {}),
                                   "X-CloudLens-Cache-Error": "discard_failed"}
            else:
                await self._record_failure(old, request, "source_failed")
            exc.headers = {**(exc.headers or {}), "X-CloudLens-Refresh-Error": "source_failed"}
            raise
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self._log_failure("refresh", request, exc)
            await self._record_failure(old, request, "source_failed")
            raise

    def _response(self, request, item, *, stale, revalidating, state="hit"):
        headers = {
            "Cache-Control": "private, no-store",
            "ETag": item.etag,
            "X-CloudLens-Cache": "stale" if stale else state,
            "X-CloudLens-Source-Generation": item.generation,
            "X-CloudLens-Source": self.policies[request.url.path].source,
            "X-CloudLens-As-Of": datetime.fromtimestamp(item.created, UTC).isoformat(),
            "X-CloudLens-Age": str(max(0, int(self.clock() - item.created))),
            "X-CloudLens-Stale": str(stale).lower(),
            "X-CloudLens-Revalidating": str(revalidating).lower(),
            "X-CloudLens-Coverage": (
                "partial" if _partial_evidence(json.loads(item.body)) else "as-reported"),
        }
        if self.policies[request.url.path].daily:
            saved = json.loads(item.body).get("snapshot", {})
            if saved.get("cadence") == "data-day":
                headers["X-CloudLens-Refresh-Policy"] = "data-day"
                headers["X-CloudLens-Data-Day"] = saved["data_day"]
        notice = self._failures.get(item.key)
        if notice and notice.created == item.created and not notice.persisted:
            headers["X-CloudLens-Cache-Error"] = "failure_write_failed"
        elif self._maintenance_error:
            headers["X-CloudLens-Cache-Error"] = self._maintenance_error
        if item.error:
            headers["X-CloudLens-Refresh-Error"] = item.error
            headers["Retry-After"] = str(max(1, int(item.retry_at - self.clock())))
        tags = request.headers.get("if-none-match", "").split(",")
        unchanged = any(tag.strip().removeprefix("W/") in {item.etag, "*"} for tag in tags)
        return Response(b"" if unchanged else item.body, status_code=304 if unchanged else 200,
                        media_type="application/json", headers=headers)


def install_snapshot_routes(app, cache: Callable[[], SnapshotCache | None],
                            authorize: Authorizer, *, paths=None) -> None:
    """Wrap only named GET endpoints, preserving FastAPI's dependency validation.

    ``cache`` is a getter because routes are installed before lifespan creates the
    worker-owned instance. Authorizer should fetch EntraSession.oid/.tenant and
    invoke current_user/permitted/selected_scope. Include capability/tenant-scope
    mapping in authorization_version and FX-policy version in fx_version.
    """
    allowed = set(DEFAULT_POLICIES if paths is None else paths)
    for route in app.routes:
        if (not isinstance(route, APIRoute)
                or (route.path not in allowed and route.path != "/api/dashboard/{section_id}")
                or route.methods != {"GET"} or getattr(route, "_snapshot_wrapped", False)):
            continue
        endpoint = route.dependant.call

        @functools.wraps(endpoint)
        async def wrapped(_endpoint=endpoint, **kwargs):
            request = kwargs.get("request")
            instance = cache()
            if (instance is None or not isinstance(request, Request)
                    or request.url.path not in allowed):
                return await _endpoint(**kwargs)
            return await instance.get(request, lambda: _endpoint(**kwargs), authorize)

        route.dependant.call = wrapped
        route.app = request_response(route.get_route_handler())
        route._snapshot_wrapped = True
