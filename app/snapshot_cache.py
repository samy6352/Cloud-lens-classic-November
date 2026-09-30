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
A report whose partial coverage is a property of the committed data declares
`"partial_basis": "data"` and is kept like any complete answer until the data changes.
Poll using ETag plus As-Of/Stale/Revalidating headers, including on HTTP 304.

AI Billing alone reuses a whole report until the scoped imported data day changes
(or explicit retry/retention expiry). Same-day corrections do not repeat telemetry
reads. Source-revision write fences, private keys and coverage rules still apply.

Views people use are also recorded as warm targets (identity, path, normalised query and
cost; never a credential) so `snapshot_warmer` can compute them again after a deploy,
publish or UTC day boundary through `SnapshotCache.warm`, before anyone asks.

A hit sends stored bytes and nothing else. The coverage header and a gzip copy of the body
are derived once, off the event loop, when a snapshot is written -- not on every view (see
`_prepared`).
"""
from __future__ import annotations

import asyncio
import functools
import gzip
import hashlib
import json
import logging
import math
import os
import re
import time
from collections import OrderedDict
from collections.abc import Awaitable, Callable
from contextvars import ContextVar
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

from fastapi import HTTPException
from fastapi.routing import APIRoute, request_response
from starlette.requests import Request
from starlette.responses import Response

from .assets import _accepts_gzip
from .store import PoolBusy

try:
    from psycopg.errors import LockNotAvailable
except ImportError:  # pragma: no cover - psycopg is present in deployed/test warehouses
    class LockNotAvailable(Exception):
        pass

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
# Set only by the in-process warmer (`snapshot_warmer`). ASGI servers build the scope from
# the wire, so no HTTP client can put this key there.
WARM_SCOPE_KEY = "cloudlens.snapshot_warm"
WARM_CLAIM_KEY = "snapshot_warm_claim"
# A view served from cache is recorded as used at most this often per process.
_TOUCH_INTERVAL = 3600.0
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
    # Billing opportunities prices every row this way. Without these its partial report --
    # partial because Azure refuses reservation reads (403) -- looked like it carried no money
    # and was recomputed on every view (12.9s on live, 30 Sep).
    "monthly_savings", "annual_savings",
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


def _positive_int_env(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        value = int(raw.strip())
    except ValueError as exc:
        raise ValueError(f"{name} must be a positive integer.") from exc
    if value <= 0:
        raise ValueError(f"{name} must be a positive integer.")
    return value


_DEFAULT_TTL = _positive_int_env("SNAPSHOT_TTL_SECONDS", 24 * 60 * 60)
_DEFAULT_RETENTION = _positive_int_env("SNAPSHOT_RETENTION_SECONDS", 7 * 24 * 60 * 60)
# How long a report with partial evidence is served before a view refreshes it behind
# the scenes. Short because the gap is often transient (a throttled subscription); long
# enough that nobody waits for the full origin on every load while it persists.
PARTIAL_TTL = _positive_int_env("SNAPSHOT_PARTIAL_TTL_SECONDS", 60 * 60)

WAREHOUSE = Policy("warehouse", _DEFAULT_TTL, _DEFAULT_RETENTION)
MONITOR = Policy("monitor", _DEFAULT_TTL, _DEFAULT_RETENTION)
ADVISOR = Policy("advisor", _DEFAULT_TTL, _DEFAULT_RETENTION)
AI = Policy("ai", _DEFAULT_TTL, _DEFAULT_RETENTION)
AI_BILLING = Policy("ai", _DEFAULT_TTL, _DEFAULT_RETENTION, daily=True)
# Exact paths only. Job/progress/auth/chat/download/write routes are not eligible.
DEFAULT_POLICIES = {
    "/api/overview": Policy("overview", _DEFAULT_TTL, _DEFAULT_RETENTION),
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
    # Both read live Azure per subscription and were never cached: measured on the live
    # deployment, Budgets hit the 240s gateway limit and Insights took 220s on every view.
    # A created budget changes the Budgets key (see `authorize_snapshot`), so a cached
    # "no budgets" can never outlive the create that disproves it.
    "/api/budgets": MONITOR,
    "/api/insights": MONITOR,
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


def _normalised_query(request: Request) -> dict[str, list[str]]:
    # Sorting names is safe; sorting/deduplicating repeated VALUES is not (last
    # value wins for scalar parameters, and list parameters preserve order).
    query: dict[str, list[str]] = {}
    for name, value in request.query_params.multi_items():
        if name in _FORCE_FLAGS:
            continue
        if name == "scope":
            value = ",".join(_scopes(value.split(",")))
        query.setdefault(name, []).append(value)
    return query


def cache_key(request: Request, context: CacheContext, version: str,
              policy: Policy) -> str:
    return _digest([context.partition(), request.url.path, _normalised_query(request),
                    context.fx_version, context.period_version, version, policy.source])


def cache_family_key(request: Request, context: CacheContext, version: str,
                     policy: Policy) -> str:
    return _digest([context.partition(), request.url.path, _normalised_query(request),
                    context.fx_version, version, policy.source])


def warm_query(request: Request) -> str:
    """The query a view is replayed with: the cache key's own normalisation, re-encoded."""
    return urlencode([(name, value)
                      for name, values in sorted(_normalised_query(request).items())
                      for value in values])


def warm_target_key(principal: str, tenant: str, path: str, query: str) -> str:
    # Identity rather than partition: a partition also carries the budget revision and the
    # account secret, and a view must stay one target when either of those moves.
    return _digest(["warm-target-v1", principal, tenant, path, query])


@dataclass(frozen=True)
class WarmTarget:
    """A view somebody used, stored so the server can compute it again before they return.

    Holds who (stable principal and tenant, never a credential), which path and which
    normalised query -- the same values a cache key is built from -- plus what it last cost.
    """
    key: str
    principal: str
    tenant: str
    path: str
    query: str
    seconds: float = 0.0
    used: float = 0.0


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
    family_key: str = ""
    # Derived from `body` when it is written (`_prepared`). Empty/None on rows written before
    # these were stored; `_response` then derives the header the old way, per view.
    coverage: str = ""
    gzipped: bytes | None = None


# A cache hit used to pay for the body on every view. Measured on the live Resources answer
# (14.6MB), per hit, on a dev laptop: `json.loads` 231ms plus a walk of every row for the one
# `X-CloudLens-Coverage` header, 791ms together, then `GZipMiddleware` recompressing the same
# bytes at level 9, 253ms -- about 1s of CPU, the parse and walk on the event loop, where every
# other request waits behind them. Live repeat views scaled with body size (Uptime 7.8MB 1.6s,
# Tags 10.5MB 2.3-3.9s, Resources 15.3MB 3.7s). Both are now derived once per write.
#
# Level 6 rather than the middleware's 9: on that body it is 40% faster and 1% smaller. The
# floor matches `GZipMiddleware(minimum_size=1024)` in main.py -- below it both send plain.
_GZIP_LEVEL = 6
_GZIP_MIN_BYTES = 1024


def _prepared(body: bytes) -> tuple[str, bytes | None]:
    """The coverage header and gzip copy a stored body is served with. CPU-bound; thread it."""
    coverage = "partial" if _partial_evidence(json.loads(body)) else "as-reported"
    packed = (gzip.compress(body, _GZIP_LEVEL, mtime=0)
              if len(body) >= _GZIP_MIN_BYTES else None)
    return coverage, packed


class SnapshotBusy(RuntimeError):
    """The generation fence is locked by a publisher; serve the response uncached."""


class SnapshotStore:
    """Bounded PostgreSQL storage using the existing warehouse connection API."""

    def __init__(self, connect: Callable, *, generation: Callable = read_generation,
                 max_entries: int = 512, max_bytes: int = 64 * 1024 * 1024,
                 max_payload: int = 8 * 1024 * 1024,
                 lock_timeout_ms: int | None = None, max_targets: int = 1000,
                 target_retention: float = _DEFAULT_RETENTION):
        """`max_payload` bounds one entry; `max_bytes` bounds the whole store.

        8MB rather than 2MB because the total is what actually protects memory --
        `_prune` evicts by cumulative `byte_size` against `max_bytes`, so a larger
        per-entry ceiling buys fewer retained entries, never more memory. The old
        2MB ceiling silently excluded the single most expensive endpoint in the
        app: `/api/dashboard/uptime` ships 6.19MB of per-resource detail for the
        client-side filtering the tab is built on, so it was never cached and cost
        84 seconds on every single load, warm or cold.
        """
        lock_timeout_ms = (_positive_int_env("SNAPSHOT_LOCK_TIMEOUT_MS", 2000)
                           if lock_timeout_ms is None else lock_timeout_ms)
        if min(max_entries, max_bytes, max_payload, lock_timeout_ms, max_targets) < 1:
            raise ValueError("Snapshot bounds must be positive.")
        self.connect, self.generation = connect, generation
        self.max_entries, self.max_bytes = max_entries, max_bytes
        self.max_payload = min(max_payload, max_bytes)
        self.lock_timeout = f"{lock_timeout_ms}ms"
        self.max_targets, self.target_retention = max_targets, target_retention

    def initialize(self):
        with self.connect() as con:
            con.execute("BEGIN")
            try:
                con.execute("SELECT pg_advisory_xact_lock(?)", [_CACHE_LOCK])
                con.execute("""CREATE TABLE IF NOT EXISTS private_snapshots (
                    cache_key TEXT PRIMARY KEY, partition_hash TEXT NOT NULL,
                    family_key TEXT NOT NULL DEFAULT '',
                    source_generation TEXT NOT NULL, body TEXT NOT NULL,
                    created DOUBLE PRECISION NOT NULL, expires DOUBLE PRECISION NOT NULL,
                    retain_until DOUBLE PRECISION NOT NULL, etag TEXT NOT NULL,
                    byte_size BIGINT NOT NULL, failures INTEGER NOT NULL DEFAULT 0,
                    retry_at DOUBLE PRECISION NOT NULL DEFAULT 0,
                    error_code TEXT NOT NULL DEFAULT ''
                )""")
                con.execute("ALTER TABLE private_snapshots ADD COLUMN IF NOT EXISTS family_key TEXT")
                # Derived at write time so a hit does not re-derive them (`_prepared`). Rows
                # from before these existed keep '' / NULL and are served the old way.
                con.execute("ALTER TABLE private_snapshots "
                            "ADD COLUMN IF NOT EXISTS coverage TEXT NOT NULL DEFAULT ''")
                con.execute("ALTER TABLE private_snapshots "
                            "ADD COLUMN IF NOT EXISTS body_gzip BYTEA")
                con.execute(
                    "CREATE INDEX IF NOT EXISTS private_snapshots_family_idx "
                    "ON private_snapshots (family_key,partition_hash,created DESC)")
                con.execute("""CREATE TABLE IF NOT EXISTS snapshot_warm_targets (
                    target_key TEXT PRIMARY KEY, principal TEXT NOT NULL,
                    tenant TEXT NOT NULL, path TEXT NOT NULL, query TEXT NOT NULL,
                    seconds DOUBLE PRECISION NOT NULL DEFAULT 0,
                    used DOUBLE PRECISION NOT NULL,
                    computed DOUBLE PRECISION NOT NULL DEFAULT 0
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
        # A target lives as long as somebody keeps looking at it; warming never renews it.
        con.execute("DELETE FROM snapshot_warm_targets WHERE used<=?",
                    [now - self.target_retention])
        con.execute(
            "DELETE FROM snapshot_warm_targets WHERE target_key IN ("
            "SELECT target_key FROM (SELECT target_key,"
            "ROW_NUMBER() OVER (ORDER BY used DESC,target_key) AS position "
            "FROM snapshot_warm_targets) AS ranked WHERE position>?)",
            [self.max_targets])

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

    @staticmethod
    def _snapshot(row, scope=None):
        values = list(row)
        values[3] = values[3].encode("utf-8")
        packed = values[13]
        return Snapshot(*values[:11], scope=scope, family_key=values[11] or "",
                        coverage=values[12] or "",
                        gzipped=bytes(packed) if packed is not None else None)

    _COLUMNS = ("cache_key,partition_hash,source_generation,body,created,expires,"
                "retain_until,etag,failures,retry_at,error_code,family_key,coverage,body_gzip")

    def load(self, key: str, partition: str, now: float, scope=None):
        with self.connect() as con:
            generation = self.generation(con, scope)
            row = con.execute(
                f"SELECT {self._COLUMNS} FROM private_snapshots "
                "WHERE cache_key=? AND partition_hash=? AND retain_until>?",
                [key, partition, now]).fetchone()
        if row:
            return self._snapshot(row, scope), generation
        return None, generation

    def load_family(self, family_key: str, partition: str, now: float, scope=None):
        with self.connect() as con:
            row = con.execute(
                f"SELECT {self._COLUMNS} FROM private_snapshots "
                "WHERE family_key=? AND partition_hash=? AND retain_until>? "
                "ORDER BY created DESC,cache_key LIMIT 1",
                [family_key, partition, now]).fetchone()
        return self._snapshot(row, scope) if row else None

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
                con.execute("SELECT set_config('lock_timeout', ?, true)", [self.lock_timeout])
                lock_generation(con)
                if self.generation(con, item.scope) != item.generation:
                    con.execute("ROLLBACK")
                    return False
                family = item.family_key or item.key
                row = con.execute(
                    "INSERT INTO private_snapshots "
                    "(cache_key,partition_hash,family_key,source_generation,body,created,expires,"
                    "retain_until,etag,byte_size,coverage,body_gzip) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?,?,?) "
                    "ON CONFLICT(cache_key) DO UPDATE SET "
                    "partition_hash=excluded.partition_hash,"
                    "family_key=excluded.family_key,"
                    "source_generation=excluded.source_generation,body=excluded.body,"
                    "created=excluded.created,expires=excluded.expires,"
                    "retain_until=excluded.retain_until,etag=excluded.etag,"
                    "byte_size=excluded.byte_size,coverage=excluded.coverage,"
                    "body_gzip=excluded.body_gzip,failures=0,retry_at=0,error_code='' "
                    "WHERE private_snapshots.created<=excluded.created RETURNING cache_key",
                    [item.key, item.partition, family, item.generation, item.body.decode("utf-8"),
                     item.created, item.expires, item.retain_until, item.etag,
                     len(item.body), item.coverage, item.gzipped]).fetchone()
                self._prune(con, item.created)
                con.execute("COMMIT")
                return bool(row)
            except LockNotAvailable as exc:
                con.execute("ROLLBACK")
                raise SnapshotBusy("Snapshot generation lock is busy.") from exc
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

    def record_target(self, target: WarmTarget, now: float, seconds: float, used: bool):
        """Remember a computed view. Only a person's use (`used`) creates or renews one."""
        with self.connect() as con:
            if used:
                con.execute(
                    "INSERT INTO snapshot_warm_targets "
                    "(target_key,principal,tenant,path,query,seconds,used,computed) "
                    "VALUES (?,?,?,?,?,?,?,?) ON CONFLICT(target_key) DO UPDATE SET "
                    "seconds=excluded.seconds,used=excluded.used,computed=excluded.computed",
                    [target.key, target.principal, target.tenant, target.path, target.query,
                     seconds, now, now])
            else:
                con.execute(
                    "UPDATE snapshot_warm_targets SET seconds=?,computed=? WHERE target_key=?",
                    [seconds, now, target.key])

    def touch_target(self, key: str, now: float):
        with self.connect() as con:
            con.execute("UPDATE snapshot_warm_targets SET used=? WHERE target_key=? AND used<?",
                        [now, key, now])

    def warm_targets(self, since: float, min_seconds: float, limit: int) -> list[WarmTarget]:
        with self.connect() as con:
            rows = con.execute(
                "SELECT target_key,principal,tenant,path,query,seconds,used "
                "FROM snapshot_warm_targets WHERE used>? AND seconds>=? "
                "ORDER BY used DESC,target_key LIMIT ?",
                [since, min_seconds, limit]).fetchall()
        return [WarmTarget(*row) for row in rows]

    def claim_warm(self, token: str) -> bool:
        """True for exactly one caller per token, across workers and instances."""
        with self.connect() as con:
            row = con.execute(
                "INSERT INTO meta (key,value,updated_at) VALUES (?,?,now()) "
                "ON CONFLICT (key) DO UPDATE SET value=excluded.value,"
                "updated_at=excluded.updated_at "
                "WHERE meta.value IS NULL OR meta.value<>excluded.value RETURNING key",
                [WARM_CLAIM_KEY, token]).fetchone()
        return row is not None

    def current_generation(self) -> str:
        with self.connect() as con:
            return self.generation(con, None)


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
    # A report may declare that its partial coverage is what the committed data says --
    # missing day receipts, no rows yet -- rather than a failed read. Asking again cannot
    # change that answer; a new data generation can, and the generation fence retires it.
    if isinstance(value, dict) and value.get("partial_basis") == "data":
        return True
    # Partial means evidence coverage, not an unfinished request. Keep all original
    # unknowns/notices/statuses; only failure-shaped empty reports are rejected.
    return not _partial_evidence(value) or _has_evidence(value)


def oversize(response: Response, limit: int) -> int:
    """How far a complete, well-formed JSON response exceeds the per-entry cap, else 0.

    Worth telling apart from every other reason a body is not cacheable. An
    oversized body is not incomplete, not failed and not pending -- it is a
    perfectly good answer that simply will not fit, and the consequence is that
    *every* viewer pays the full origin cost forever rather than the first one.

    Measured on the real estate: `/api/dashboard/uptime` returns 6.19MB against a
    2MB cap, so it was never once cached and every load cost 84 seconds. Nothing
    said so -- it was reported as `source-incomplete`, which sent anyone looking
    at the query instead of at the size.
    """
    body = getattr(response, "body", None)
    if (response.status_code != 200 or not isinstance(body, bytes) or len(body) <= limit
            or response.headers.get("content-type", "").split(";")[0] != "application/json"):
        return 0
    return len(body)


def _body(response: Response, limit: int) -> bytes | None:
    body = _json_body(response, limit)
    if body is None:
        return None
    try:
        value = json.loads(body)
        return body if _cacheable_report(value) else None
    except (ValueError, UnicodeError):
        return None


def _json_body(response: Response, limit: int) -> bytes | None:
    """The body, if the response is a plain, private-safe JSON 200 within the size limit."""
    body = getattr(response, "body", None)
    if (response.status_code != 200 or not isinstance(body, bytes) or len(body) > limit
            or "set-cookie" in response.headers
            or "content-encoding" in response.headers
            or "content-disposition" in response.headers
            or response.headers.get("content-type", "").split(";")[0] != "application/json"):
        return None
    return body


def _partial_body(response: Response, limit: int) -> bytes | None:
    """A report that is complete as an answer, though its evidence is not.

    Everything `_body` refuses for secrets, unfinished jobs, size and headers is still
    refused. What is admitted is a report carrying per-source errors or partial
    coverage *and real money somewhere in it*. A failure-shaped report -- an error, a
    zero total, a coverage block that says "unavailable" -- carries none, is usually
    transient, and must not be served back for the next hour.
    """
    body = _json_body(response, limit)
    if body is None:
        return None
    try:
        value = json.loads(body)
    except (ValueError, UnicodeError):
        return None
    if not isinstance(value, (dict, list)) or not _safe_content(value) or _unfinished(value):
        return None
    return body if _carries_money(value) else None


def _carries_money(value: Any) -> bool:
    """A non-zero money or measurement figure anywhere, including in sections that
    `_has_evidence` deliberately skips: Commitments keeps its whole report in `coverage`.
    """
    if isinstance(value, dict):
        for name, item in value.items():
            if name in {"error", "errors", "problem"}:
                continue
            if (name in _EVIDENCE_FIELDS and isinstance(item, (int, float))
                    and not isinstance(item, bool) and math.isfinite(item) and item != 0):
                return True
            if isinstance(item, (dict, list)) and _carries_money(item):
                return True
    elif isinstance(value, list):
        return any(_carries_money(item) for item in value)
    return False


def _complete(body: bytes) -> bool:
    try:
        return _cacheable_report(json.loads(body))
    except (ValueError, UnicodeError):
        return False


def _eligible(response: Response, limit: int, old_body: bytes | None, full_ttl: float,
              data_day: str | None) -> tuple[bytes | None, float, bool]:
    """What of an origin answer may be stored, for how long, and whether it is pending.

    Returns `(body, ttl, pending)`. Synchronous and CPU-bound, so the caller runs it on a
    worker thread. Stamps a daily policy's `data_day` into the response it is given, which
    is safe there: nothing else holds the response until this returns.
    """
    body = _body(response, limit)
    ttl = full_ttl
    if body is None and not _pending_body(response, limit):
        # A complete answer about incomplete evidence -- one subscription
        # throttled, some days not yet verified -- is still the answer the
        # next viewer would get after waiting the full origin time, so it is
        # kept briefly rather than recomputed on every view. Measured on the
        # 16.9M-row estate: Commitments 98s and Waste 6s on *every* load, because
        # each always reported partial coverage somewhere. It never replaces a
        # complete snapshot: the last good answer is worth more than a new
        # partial one, and the failure path keeps serving it.
        partial = _partial_body(response, limit)
        if partial is not None and not (old_body is not None and _complete(old_body)):
            body, ttl = partial, min(full_ttl, PARTIAL_TTL)
    if body is not None and data_day:
        value = json.loads(body)
        value["snapshot"] = {"cadence": "data-day", "data_day": data_day}
        response.body = json.dumps(value, separators=(",", ":"), ensure_ascii=True).encode()
        response.headers["Content-Length"] = str(len(response.body))
        body = (_body(response, limit) if ttl == full_ttl
                else _partial_body(response, limit))
    pending = body is None and _pending_body(response, limit)
    return body, ttl, pending


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
                 clock: Callable[[], float] = time.time, warm_min_seconds: float = 1.0):
        if concurrency < 1 or max_inflight < concurrency or origin_timeout <= 0 or not version:
            raise ValueError("Invalid snapshot cache configuration.")
        if not math.isfinite(warm_min_seconds) or warm_min_seconds < 0:
            raise ValueError("warm_min_seconds must be a finite, non-negative number.")
        self.store, self.version = store, version
        self.policies = dict(DEFAULT_POLICIES if policies is None else policies)
        self.clock, self.origin_timeout = clock, origin_timeout
        self.max_inflight = max_inflight
        # Views cheaper than this are not worth a server computing ahead of time.
        self.warm_min_seconds = warm_min_seconds
        self._slots = asyncio.Semaphore(concurrency)
        self._tasks: dict[tuple[str, str], asyncio.Task] = {}
        self._db_tasks: set[asyncio.Task] = set()
        self._completion_serial = 0
        self._maintenance: asyncio.Task | None = None
        self._maintenance_error = ""
        self._failures: OrderedDict[str, _Failure] = OrderedDict()
        self._touched: OrderedDict[str, float] = OrderedDict()
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
            if error is not None and not isinstance(error, SnapshotBusy):
                self._log_failure("database", None, error)

    def _background_db(self, function, *args):
        """Bookkeeping a response must not wait for; still drained before shutdown."""
        task = asyncio.create_task(asyncio.to_thread(function, *args))
        self._db_tasks.add(task)
        task.add_done_callback(self._db_finished)

    def _target(self, request, context) -> WarmTarget | None:
        # A session-derived principal (Entra without an object id) cannot be signed in as
        # again, so it is never a target.
        if context.principal.startswith("session:"):
            return None
        query = warm_query(request)
        return WarmTarget(warm_target_key(context.principal, context.tenant,
                                          request.url.path, query),
                          context.principal, context.tenant, request.url.path, query)

    def _note_use(self, request, context):
        touch = getattr(self.store, "touch_target", None)
        if touch is None or request.scope.get(WARM_SCOPE_KEY):
            return
        target = self._target(request, context)
        if target is None:
            return
        now = self.clock()
        last = self._touched.get(target.key)
        if last is not None and now - last < _TOUCH_INTERVAL:
            return
        self._touched[target.key] = now
        self._touched.move_to_end(target.key)
        while len(self._touched) > 4096:
            self._touched.popitem(last=False)
        self._background_db(touch, target.key, now)

    def _record_target(self, request, context, seconds, warming):
        record = getattr(self.store, "record_target", None)
        if record is None or (not warming and seconds < self.warm_min_seconds):
            return
        target = self._target(request, context)
        if target is not None:
            self._background_db(record, target, self.clock(), seconds, not warming)

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
        family = cache_family_key(request, context, self.version, policy)
        force = any(request.query_params.get(name, "").lower() in {"1", "true", "yes"}
                    for name in _FORCE_FLAGS)
        family_fallback = False
        load_family = getattr(self.store, "load_family", None)
        try:
            while True:
                completion_serial = self._completion_serial
                # A coalesced producer may have committed and left the task map
                # while this DB read was in flight. Recheck before issuing work.
                item, generation = await self._db(
                    self.store.load, key, partition, self.clock(), context.selected)
                # No exact entry: yesterday's calculation of the same view, same partition,
                # is served stale while today's is computed, so the UTC day boundary is not
                # a cold miss for every viewer. Never across partitions, never when forced.
                family_fallback = False
                if item is None and not force and load_family is not None:
                    item = await self._db(
                        load_family, family, partition, self.clock(), context.selected)
                    family_fallback = item is not None
                # Both reads are waits during which a producer can finish; only an exact
                # hit is final, anything else is re-read if one did.
                if ((item is not None and not family_fallback)
                        or completion_serial == self._completion_serial):
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
        if _source_pending(generation):
            await self._authorize(request, authorize, partition, context)
            if force or item is None:
                raise HTTPException(503, "Committed source data is still being prepared.",
                                    headers={"X-CloudLens-Cache": "source-pending",
                                             "X-CloudLens-Stale": "true",
                                             "X-CloudLens-Revalidating": "true",
                                             "X-CloudLens-Source-State": "pending",
                                             "Retry-After": "3"})
            self._note_use(request, context)
            return self._source_state(
                self._response(request, item, stale=True, revalidating=True),
                "source-pending", generation)
        daily = policy.daily and bool(context.data_day)
        if daily and item:
            saved_day = json.loads(item.body).get("snapshot", {}).get("data_day")
            source_changed = saved_day != context.data_day
            # The source revision still fences writes. A completed daily report,
            # however, survives same-day imports and midnight without new data.
            stale = family_fallback or source_changed or bool(item.error)
        else:
            source_changed = bool(item and item.generation != generation)
            stale = bool(item and (
                family_fallback or item.expires <= self.clock() or source_changed or item.error))
        task_key = (key, generation)
        task = self._tasks.get(task_key)
        owns_origin = False
        if ((not item or stale or force)
                and (not item or force or family_fallback or (source_changed and not daily)
                     or item.retry_at <= self.clock())
                and task is None and not self._closed and len(self._tasks) < self.max_inflight):
            task = asyncio.create_task(self._refresh(
                request, origin, authorize, context, key, generation, policy, item))
            self._tasks[task_key] = task
            task.add_done_callback(functools.partial(self._finished, task_key))
            owns_origin = True
        if item and not force:
            await self._authorize(request, authorize, partition, context)
            self._note_use(request, context)
            return self._response(request, item, stale=stale or force,
                                  revalidating=bool(task and not task.done()))
        if task is None:
            # Bounded overload, rather than an unbounded queue or uncoalesced Azure fanout.
            raise HTTPException(503, "Snapshot refresh capacity is busy.",
                                headers={"Retry-After": "2"})
        result = await asyncio.shield(task)
        await self._authorize(request, authorize, partition, context)
        if isinstance(result, Snapshot):
            if not owns_origin:
                # Joined a calculation somebody else -- possibly the warmer -- started.
                self._note_use(request, context)
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
        if result.headers.get("X-CloudLens-Cache") == "bypass":
            return result
        if owns_origin:
            return result
        # Do not share a streaming iterator, Set-Cookie, or any other non-cacheable
        # response between requests (even two sessions of the same principal).
        async with self._slots:
            return await origin()

    async def warm(self, request: Request, origin: Callable[[], Awaitable[Response]],
                   authorize: Authorizer) -> str:
        """Compute this view now, unless its exact current answer is already stored.

        For the server-side warmer, which replays views people used once a new code
        version, data generation or UTC day has made their stored answer cold or stale.
        It shares `get`'s task map, so a person who opens the view meanwhile joins this
        calculation rather than starting a second, and it keeps every rule `get` applies
        to a refresh: re-authorisation, the generation fence, failure back-off, and a
        partial answer never replacing a complete one. Returns an outcome label.
        """
        policy = self.policies.get(request.url.path)
        if (request.method != "GET" or policy is None
                or any(name.lower() in _SECRET_KEYS for name in request.query_params)):
            return "ineligible"
        try:
            context = await self._authorize(request, authorize)
            partition = context.partition()
        except HTTPException as exc:
            return f"denied-{exc.status_code}"
        key = cache_key(request, context, self.version, policy)
        now = self.clock()
        item, generation = await self._db(
            self.store.load, key, partition, now, context.selected)
        if _source_pending(generation):
            return "source-pending"
        if item is not None:
            if item.error and item.retry_at > now:
                return "backoff"
            if self._current(item, policy, context, generation, now):
                return "fresh"
        old = item
        load_family = getattr(self.store, "load_family", None)
        if old is None and load_family is not None:
            old = await self._db(load_family, cache_family_key(
                request, context, self.version, policy), partition, now, context.selected)
        task_key = (key, generation)
        task = self._tasks.get(task_key)
        if task is None:
            if self._closed or len(self._tasks) >= self.max_inflight:
                return "busy"
            task = asyncio.create_task(self._refresh(
                request, origin, authorize, context, key, generation, policy, old,
                warming=True))
            self._tasks[task_key] = task
            task.add_done_callback(functools.partial(self._finished, task_key))
        try:
            result = await asyncio.shield(task)
        except HTTPException as exc:
            return f"failed-{exc.status_code}"
        except TimeoutError:
            return "timeout"
        except Exception:  # noqa: BLE001 - `_refresh` has logged it and recorded back-off
            return "failed"
        if isinstance(result, Snapshot):
            return "stored"
        return result.headers.get("X-CloudLens-Cache") or f"status-{result.status_code}"

    @staticmethod
    def _current(item, policy, context, generation, now) -> bool:
        """What `get` would serve as a hit without starting a refresh."""
        if item.error:
            return False
        if policy.daily and context.data_day:
            try:
                saved = json.loads(item.body).get("snapshot", {}).get("data_day")
            except (ValueError, UnicodeError, AttributeError):
                return False
            return saved == context.data_day
        return item.generation == generation and item.expires > now

    def _finished(self, key, task):
        self._completion_serial += 1
        if self._tasks.get(key) is task:
            self._tasks.pop(key, None)
        if not task.cancelled():
            task.exception()  # Background exceptions are observed, never logged with secrets.

    async def _refresh(self, request, origin, authorize, context, key, generation, policy, old,
                       warming=False):
        partition = context.partition()
        try:
            async with self._slots:
                await self._authorize(request, authorize, partition, context)
                started = self.clock()
                timer = time.perf_counter()
                calculation = calculation_context.set((key, generation))
                try:
                    response = await asyncio.wait_for(origin(), self.origin_timeout)
                finally:
                    calculation_context.reset(calculation)
                await self._authorize(request, authorize, partition, context)
                if response.status_code in {401, 403}:
                    await self._db(self.store.discard, key, partition)
                    return response
                ttl = policy.ttl
                # Parsing and scanning the answer is CPU-bound and scales with its size:
                # 3.3s for a 15MB Resources body, and a partial answer re-reads the old
                # one too. On the event loop that stalled every other request, cache hits
                # included, for the length of each cold compute's check.
                body, ttl, pending = await asyncio.to_thread(
                    _eligible, response, self.store.max_payload,
                    old.body if old is not None else None, policy.ttl,
                    context.data_day if policy.daily else None)
                if body is None:
                    if pending:
                        return self._source_state(response, "source-pending", generation)
                    # Size is its own diagnosis. Saying "incomplete" about a complete
                    # answer sends the next person to read the query rather than the
                    # response length, which is how a 6MB tab stayed uncached for as
                    # long as it did.
                    too_big = oversize(response, self.store.max_payload)
                    if too_big:
                        log.warning(
                            "snapshot: %s returned %.1fMB, over the %.1fMB cache limit -- "
                            "not cached, so every viewer pays the full origin cost",
                            request.url.path, too_big / 1048576,
                            self.store.max_payload / 1048576)
                        response.headers["X-CloudLens-Refresh-Error"] = "response_too_large"
                        return self._source_state(response, "response-too-large", generation)
                    self._log_failure("eligibility", request, ValueError())
                    await self._record_failure(old, request, "source_incomplete")
                    response.headers["X-CloudLens-Refresh-Error"] = "source_incomplete"
                    return self._source_state(response, "source-incomplete", generation)
                if _source_pending(generation):
                    return self._source_state(response, "source-pending", generation)
                coverage, packed = await asyncio.to_thread(_prepared, body)
                item = Snapshot(key, partition, generation, body, started,
                                started + ttl, started + policy.retention,
                                '"' + _digest([generation, self.version,
                                               hashlib.sha256(body).hexdigest()]) + '"',
                                scope=context.selected, family_key=cache_family_key(
                                    request, context, self.version, policy),
                                coverage=coverage, gzipped=packed)
                try:
                    accepted = await self._db(self.store.put, item)
                except SnapshotBusy:
                    response.headers["Cache-Control"] = "private, no-store"
                    response.headers["X-CloudLens-Cache"] = "bypass"
                    return response
                except Exception as exc:
                    self._log_failure("write", request, exc)
                    await self._record_failure(old, request, "cache_write_failed")
                    response.headers["X-CloudLens-Refresh-Error"] = "cache_write_failed"
                    return self._unavailable(response, "write_failed")
                if accepted:
                    self._failures.pop(key, None)
                    self._record_target(request, context, time.perf_counter() - timer, warming)
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
            "X-CloudLens-Coverage": item.coverage or (
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
        if unchanged:
            return Response(b"", status_code=304, media_type="application/json", headers=headers)
        # The stored gzip copy goes out as-is; `GZipMiddleware` passes a response that already
        # carries Content-Encoding straight through, so the body is not compressed again.
        headers["Vary"] = "Accept-Encoding"
        if item.gzipped is not None and _accepts_gzip(request.headers.get("accept-encoding", "")):
            headers["Content-Encoding"] = "gzip"
            return Response(item.gzipped, status_code=200, media_type="application/json",
                            headers=headers)
        return Response(item.body, status_code=200, media_type="application/json",
                        headers=headers)


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
            if isinstance(request, Request) and request.scope.get(WARM_SCOPE_KEY):
                # The warmer wants the answer stored, not sent: nothing crosses the ASGI
                # stack but an outcome label.
                if instance is None:
                    outcome = "unavailable"
                elif request.url.path not in allowed:
                    outcome = "ineligible"
                else:
                    try:
                        outcome = await instance.warm(
                            request, lambda: _endpoint(**kwargs), authorize)
                    except Exception as exc:  # noqa: BLE001 - reported as an outcome
                        instance._log_failure("warm", request, exc)
                        outcome = "failed"
                return Response(status_code=204, headers={
                    "Cache-Control": "private, no-store", "X-CloudLens-Warm": outcome})
            if (instance is None or not isinstance(request, Request)
                    or request.url.path not in allowed):
                return await _endpoint(**kwargs)
            return await instance.get(request, lambda: _endpoint(**kwargs), authorize)

        route.dependant.call = wrapped
        route.app = request_response(route.get_route_handler())
        route._snapshot_wrapped = True
