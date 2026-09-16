"""Bounded dependency probes, separate from process liveness."""
from __future__ import annotations

import asyncio
import logging
import math
import time
from collections.abc import Callable
from dataclasses import dataclass

log = logging.getLogger("cloudlens.readiness")


@dataclass
class _Attempt:
    task: asyncio.Task[bool]
    started: float
    timed_out: bool = False


class ReadinessProbe:
    """Share one read-only check and cache its result briefly.

    Caller timeouts do not cancel the worker: otherwise repeated probes could queue
    unlimited blocking credential/database work while an outage is in progress.
    """

    def __init__(self, check: Callable[[], bool], *, timeout: float = 5,
                 cache_seconds: float = 5, clock: Callable[[], float] = time.monotonic):
        if not math.isfinite(timeout) or timeout <= 0 or not math.isfinite(cache_seconds) or cache_seconds < 0:
            raise ValueError("Readiness timeout must be positive and cache duration nonnegative.")
        self._check = check
        self._timeout = timeout
        self._cache_seconds = cache_seconds
        self._clock = clock
        self._attempt: _Attempt | None = None
        self._ready = False
        self._valid_until = 0.0
        self._closed = False

    def _complete(self, attempt: _Attempt) -> None:
        ready = False
        try:
            ready = attempt.task.result() is True
        except asyncio.CancelledError:
            pass  # Shutdown cancels the wrapper; the read-only worker closes its own connection.
        except Exception as error:
            # Connection errors can contain hostnames or credentials; keep public and log output bounded.
            log.warning("Database readiness check failed (%s)", type(error).__name__)
        if self._attempt is attempt:
            self._attempt = None
            self._ready = (ready and not self._closed and not attempt.timed_out
                           and self._clock() - attempt.started <= self._timeout)
            self._valid_until = self._clock() + self._cache_seconds

    def _timeout_result(self, attempt: _Attempt) -> bool:
        if not attempt.timed_out:
            log.warning("Database readiness check exceeded its %.1fs deadline", self._timeout)
            attempt.timed_out = True
        return False

    async def ready(self) -> bool:
        if self._closed:
            return False
        now = self._clock()
        if self._attempt is None:
            if now < self._valid_until:
                return self._ready
            attempt = _Attempt(
                asyncio.create_task(asyncio.to_thread(self._check), name="database-readiness"), now,
            )
            self._attempt = attempt
            attempt.task.add_done_callback(lambda _task: self._complete(attempt))
        else:
            attempt = self._attempt
        remaining = self._timeout - (self._clock() - attempt.started)
        if remaining <= 0:
            return self._timeout_result(attempt)
        try:
            result = await asyncio.wait_for(asyncio.shield(attempt.task), timeout=remaining)
        except TimeoutError:
            if attempt.task.done():
                return False
            return self._timeout_result(attempt)
        except Exception:
            return False  # The completion callback records the failure once for all waiting requests.
        return (result is True and not self._closed and not attempt.timed_out
                and self._clock() - attempt.started <= self._timeout)

    async def close(self) -> None:
        self._closed = True
        self._ready = False
        if self._attempt is not None:
            task = self._attempt.task
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
