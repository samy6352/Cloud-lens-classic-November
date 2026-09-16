"""Backfill cancellation releases only the subscriptions still owned by that job."""
from __future__ import annotations

import asyncio
import sys
from types import ModuleType, SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app import backfill

A = {"id": "subscription-a", "name": "A"}
B = {"id": "subscription-b", "name": "B"}


@pytest.fixture
def isolated_backfill(monkeypatch):
    module = ModuleType("app.warehouse")
    module.warehouse = SimpleNamespace()
    monkeypatch.setitem(sys.modules, "app.warehouse", module)
    monkeypatch.setattr(backfill, "present", set)
    monkeypatch.setattr(backfill, "_gate", asyncio.Lock())
    monkeypatch.setattr(backfill, "_seen", {})
    monkeypatch.setattr(backfill, "_running", set())
    monkeypatch.setattr(backfill, "_tasks", set())


@pytest.mark.parametrize("phase", ["before_start", "waiting_for_gate", "during_ingest"])
def test_cancelled_batch_can_be_scheduled_again(isolated_backfill, monkeypatch, phase):
    async def run():
        entered = asyncio.Event()

        async def ingest(*args):
            entered.set()
            await asyncio.Event().wait()

        monkeypatch.setattr(backfill, "_ingest_one", ingest)
        if phase == "waiting_for_gate":
            await backfill._gate.acquire()
        try:
            backfill.schedule([A, B])
            task = next(iter(backfill._tasks))
            if phase == "waiting_for_gate":
                await asyncio.sleep(0)
            elif phase == "during_ingest":
                await entered.wait()
            assert backfill._running == {A["id"], B["id"]}
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            assert not backfill._running
            assert not backfill._tasks
            assert not backfill._seen
        finally:
            if backfill._gate.locked():
                backfill._gate.release()

        completed = AsyncMock(return_value={"rows": 1})
        monkeypatch.setattr(backfill, "_ingest_one", completed)
        backfill.schedule([A, B])
        await asyncio.gather(*list(backfill._tasks))
        assert completed.await_count == 2
        assert not backfill._running and not backfill._tasks

    asyncio.run(run())


def test_old_batch_cleanup_does_not_release_a_rescheduled_subscription(isolated_backfill, monkeypatch):
    async def check():
        second_started = asyncio.Event()
        allow_retry = asyncio.Event()
        calls = 0

        async def ingest(warehouse, sub, months):
            nonlocal calls
            calls += 1
            if sub["id"] == B["id"]:
                second_started.set()
                await asyncio.Event().wait()
            if calls > 2:
                await allow_retry.wait()
            return {"rows": 1}

        monkeypatch.setattr(backfill, "_ingest_one", ingest)
        backfill.schedule([A, B])
        original = next(iter(backfill._tasks))
        await second_started.wait()
        # Model A's retry interval expiring while B still runs.
        backfill._seen.clear()
        backfill.schedule([A])
        retry = next(task for task in backfill._tasks if task is not original)
        try:
            original.cancel()
            await asyncio.gather(original, return_exceptions=True)
            assert backfill._running == {A["id"]}
            assert retry in backfill._tasks
        finally:
            allow_retry.set()
            await retry
        assert not backfill._running and not backfill._tasks

    asyncio.run(check())
