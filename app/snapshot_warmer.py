"""Server-side snapshot warming: compute the views people use before they ask again.

The snapshot cache keys every answer by the application version, so each deploy leaves
every view cold until somebody opens it. Measured on the 16.9M-row Scale estate after the
30 Sep deploy, whoever looked first waited ~30s for Resources, 26s for Tags, 15s for
Uptime, 49s for Insights and 100s for Commitments. A new data generation or UTC day is
not cold -- the last answer is served while it recomputes -- but that first viewer is
shown the previous answer.

This replays the views people actually used, which the cache records as warm targets,
through the application itself and as the person who used them. Going through the whole
ASGI stack rather than calling report functions directly means authentication,
entitlement, the reporting month and the cache's own eligibility and fencing rules are
exactly those of a real request; the only difference is a scope key no HTTP client can
set, which turns the cached route's answer into "store it" instead of "send it".

A pass runs once per (code version, data generation, UTC day), claimed in the database so
one instance does it. Entra sessions live in memory and do not survive a restart, so an
Entra user's views are warmed when that person signs in again, or on a later pass while
their session is live.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import time
from collections import Counter
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Any

from .snapshot_cache import WARM_SCOPE_KEY, SnapshotCache, WarmTarget

log = logging.getLogger("cloudlens.snapshot_warmer")

# (principal, tenant) -> request headers that sign in as them, or None when that is not
# possible right now (account removed, Entra session gone). Never persisted.
Credentials = Callable[[str, str], Awaitable[dict[str, str] | None]]


def _token(*parts: str) -> str:
    return hashlib.sha256(json.dumps(parts, separators=(",", ":")).encode()).hexdigest()


class SnapshotWarmer:
    def __init__(self, app: Any, cache: Callable[[], SnapshotCache | None],
                 credentials: Credentials, *, host: str = "localhost", scheme: str = "http",
                 delay: float = 30.0, poll: float = 60.0, concurrency: int = 1,
                 limit: int = 100, min_seconds: float = 1.0, window: float = 7 * 86400,
                 clock: Callable[[], float] = time.time):
        if concurrency < 1 or limit < 1 or delay < 0 or poll <= 0 or window <= 0:
            raise ValueError("Invalid snapshot warming configuration.")
        self.app, self.cache, self.credentials = app, cache, credentials
        self.host, self.scheme = host, scheme
        self.delay, self.poll, self.limit = delay, poll, limit
        self.min_seconds, self.window, self.clock = min_seconds, window, clock
        self._slots = asyncio.Semaphore(concurrency)
        self._seen: tuple[str, str, str] | None = None
        self._runner: asyncio.Task | None = None
        self._principal_tasks: dict[tuple[str, str], asyncio.Task] = {}
        self.last: dict[str, Any] = {}

    # ---------------------------------------------------------------- lifecycle
    def start(self) -> None:
        if self._runner is None:
            self._runner = asyncio.create_task(self._run(), name="snapshot-warming")

    async def close(self) -> None:
        tasks = [*self._principal_tasks.values(), *([self._runner] if self._runner else [])]
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        self._principal_tasks.clear()
        self._runner = None

    async def _run(self) -> None:
        # Let startup's own work (rollup preparation, currency, SSO discovery) go first.
        await asyncio.sleep(self.delay)
        failures = 0
        while True:
            try:
                await self.tick()
                failures = 0
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - e.g. the database stopped overnight
                failures += 1
                # Once per outage, not once a minute for the length of it.
                (log.warning if failures == 1 else log.debug)(
                    "snapshot warming check failed (%s); retrying", type(exc).__name__)
            await asyncio.sleep(self.poll)

    # ---------------------------------------------------------------- triggers
    async def tick(self) -> dict[str, Any] | None:
        """Run a pass if the version, generation or UTC day changed since the last one."""
        cache = self.cache()
        if cache is None:
            return None
        generation = await asyncio.to_thread(cache.store.current_generation)
        if generation.startswith("pending:"):
            # Rollups are being rebuilt; anything computed now could not be stored.
            return None
        day = datetime.fromtimestamp(self.clock(), UTC).date().isoformat()
        seen = (cache.version, generation, day)
        if seen == self._seen:
            return None
        reason = self._reason(seen)
        claim = getattr(cache.store, "claim_warm", None)
        claimed = claim is None or await asyncio.to_thread(claim, _token(*seen))
        self._seen = seen
        if not claimed:
            log.info("snapshot warming: %s pass already taken by another instance", reason)
            return None
        return await self.warm_all(reason)

    def _reason(self, seen: tuple[str, str, str]) -> str:
        if self._seen is None:
            return "startup"
        version, generation, _ = self._seen
        if seen[0] != version:
            return "deploy"
        return "publish" if seen[1] != generation else "day"

    def warm_principal_soon(self, principal: str, tenant: str) -> None:
        """After a sign-in: warm that person's views now that there is a session to use."""
        key = (principal, tenant)
        running = self._principal_tasks.get(key)
        if running is not None and not running.done():
            return
        task = asyncio.create_task(self.warm_all("sign-in", principal=principal, tenant=tenant),
                                   name="snapshot-warming-sign-in")
        self._principal_tasks[key] = task

        def finished(done: asyncio.Task, key=key) -> None:
            if self._principal_tasks.get(key) is done:
                self._principal_tasks.pop(key, None)
            if not done.cancelled() and done.exception() is not None:
                log.warning("snapshot warming after sign-in failed (%s)",
                            type(done.exception()).__name__)

        task.add_done_callback(finished)

    # ---------------------------------------------------------------- the pass
    async def warm_all(self, reason: str, *, principal: str | None = None,
                       tenant: str | None = None) -> dict[str, Any]:
        cache = self.cache()
        if cache is None:
            return {}
        started = time.perf_counter()
        targets = await asyncio.to_thread(
            cache.store.warm_targets, self.clock() - self.window, self.min_seconds, self.limit)
        targets = [target for target in targets if target.path in cache.policies
                   and (principal is None or (target.principal, target.tenant) == (principal, tenant))]
        counts: Counter[str] = Counter()

        async def one(target: WarmTarget) -> None:
            async with self._slots:
                # Per request, not per pass: a pass can outlast a short-lived credential.
                headers = await self.credentials(target.principal, target.tenant)
                if headers is None:
                    counts["no-identity"] += 1
                    return
                try:
                    outcome = await self._replay(target, headers)
                except asyncio.CancelledError:
                    raise
                except Exception as exc:  # noqa: BLE001 - one view must not stop the pass
                    log.warning("snapshot warming %s failed (%s)", target.path,
                                type(exc).__name__)
                    outcome = "failed"
            counts[outcome] += 1

        await asyncio.gather(*(one(target) for target in targets))
        summary = {"reason": reason, "targets": len(targets),
                   "seconds": round(time.perf_counter() - started, 1), **dict(sorted(counts.items()))}
        if principal is None:
            self.last = summary
        log.info("snapshot warming pass: %s", summary)
        return summary

    async def _replay(self, target: WarmTarget, headers: dict[str, str]) -> str:
        raw = [(b"host", self.host.encode("latin-1")), (b"accept", b"application/json"),
               (b"user-agent", b"cloudlens-snapshot-warmer")]
        raw += [(name.lower().encode("latin-1"), value.encode("latin-1"))
                for name, value in headers.items()]
        scope = {
            "type": "http", "asgi": {"version": "3.0", "spec_version": "2.3"},
            "http_version": "1.1", "method": "GET", "scheme": self.scheme,
            "path": target.path, "raw_path": target.path.encode("latin-1"), "root_path": "",
            "query_string": target.query.encode("latin-1"), "headers": raw,
            "client": ("127.0.0.1", 0),
            "server": (self.host, 443 if self.scheme == "https" else 80),
            WARM_SCOPE_KEY: True,
        }
        done = asyncio.Event()
        delivered = False
        status, outcome = 0, ""

        async def receive() -> dict[str, Any]:
            nonlocal delivered
            if not delivered:
                delivered = True
                return {"type": "http.request", "body": b"", "more_body": False}
            await done.wait()
            return {"type": "http.disconnect"}

        async def send(message: dict[str, Any]) -> None:
            nonlocal status, outcome
            if message["type"] == "http.response.start":
                status = message["status"]
                for name, value in message.get("headers", []):
                    if name.lower() == b"x-cloudlens-warm":
                        outcome = value.decode("latin-1")
            elif message["type"] == "http.response.body" and not message.get("more_body"):
                done.set()

        try:
            await self.app(scope, receive, send)
        finally:
            done.set()
        # No outcome header means the request never reached a cached route: signed out,
        # redirected, or a route that no longer caches.
        return outcome or f"http-{status}"
