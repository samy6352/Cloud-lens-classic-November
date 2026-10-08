"""Offline regressions for source delivery, actor-scoped caching and shared scan lifetime.

Run directly with Python; no warehouse, Azure calls or application lifespan is needed.
"""
from __future__ import annotations

import asyncio
import copy
import json
import os
import sys
import unittest
from contextlib import ExitStack
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.environ.setdefault("CLOUDLENS_ENV_FILE", str(ROOT / "tests" / "absent.env"))

from fastapi import HTTPException
from starlette.requests import Request

from app import cost, currency, main, savings, shutdown, waste
from app.auth import User

RID = "/subscriptions/sub-a/resourceGroups/Group/providers/Microsoft.Compute/virtualMachines/vm"
DATA = {
    "waste": {
        "currency": "USD",
        "findings": [{"category": "deallocated_vms", "title": "VM disks", "count": 1,
                      "cost": 12, "items": [{"id": RID, "name": "vm",
                                            "resource_group": "Group", "cost": 12}]}],
    },
    "rightsizing": {
        "currency": "USD", "vms": [{"name": "vm", "resource_group": "group",
                                  "state": "deallocated", "cost": 12}],
    },
    "shutdown": {"currency": "USD", "candidates": []},
    "advisor": {"currency": "USD", "recommendations": []},
}
USER = User("alice", email="alice@example.invalid")


class Sources:
    """Release individual sources deterministically, without sleeps or real service calls."""

    def __init__(self, data=None):
        self.data = copy.deepcopy(DATA if data is None else data)
        self.gates = {name: asyncio.Event() for name in savings.SCAN_SOURCES}
        self.calls = []
        self.finished = []
        self.tasks = []

    def install(self, stack):
        for module, attribute, name in (
            (waste, "find_waste", "waste"),
            (waste, "vm_utilisation", "rightsizing"),
            (shutdown, "find_schedulable", "shutdown"),
            (waste, "advisor_recommendations", "advisor"),
        ):
            stack.enter_context(patch.object(module, attribute, self.reader(name)))
        return self

    def reader(self, name):
        async def read(*args, **kwargs):
            self.calls.append((name, args, kwargs, cost.acting_as()))
            self.tasks.append(asyncio.current_task())
            try:
                await self.gates[name].wait()
                value = self.data[name]
                if isinstance(value, BaseException):
                    raise value
                return copy.deepcopy(value)
            finally:
                self.finished.append(name)
        return read

    def release(self, *names):
        for name in names or savings.SCAN_SOURCES:
            self.gates[name].set()


async def until(predicate):
    async with asyncio.timeout(2):
        while not predicate():
            await asyncio.sleep(0)


def request(user=USER):
    req = Request({"type": "http", "headers": []})
    if user:
        req.state.user = user
    return req


class SavingsProgressTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        main._savings_cache.clear()
        main._savings_pending.clear()
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.stack.enter_context(patch.object(
            currency, "rate_to", side_effect=lambda code: {"USD": 1, "INR": 80}.get(code)))

    async def asyncTearDown(self):
        tasks = list(main._savings_pending.values())
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        main._savings_cache.clear()
        main._savings_pending.clear()

    def sources(self, data=None):
        return Sources(data).install(self.stack)

    async def poll(self, **kwargs):
        response = await main.dashboard_savings_progress(request(), **kwargs)
        self.assertEqual(response.headers["cache-control"], "no-store")
        return json.loads(response.body)

    async def drain(self):
        tasks = list(main._savings_pending.values())
        await asyncio.gather(*tasks)
        await asyncio.sleep(0)

    def expire(self, state):
        state.generated_time -= main._SAVINGS_CACHE_SECONDS + 10
        state.finished_at -= main._SAVINGS_CACHE_SECONDS + 10

    async def test_source_completion_order_and_deterministic_dedupe(self):
        final = None
        for order in (
            ("rightsizing", "advisor", "shutdown", "waste"),
            ("waste", "shutdown", "advisor", "rightsizing"),
        ):
            with ExitStack() as stack:
                sources = Sources().install(stack)
                snapshots = []
                task = asyncio.create_task(savings.build(["sub-a"], 30, on_progress=snapshots.append))
                for index, name in enumerate(order, 1):
                    sources.release(name)
                    await until(lambda snapshots=snapshots, index=index: len(snapshots) == index)
                    self.assertEqual(snapshots[-1]["scan"]["completed"], list(order[:index]))
                    self.assertEqual(snapshots[-1]["scan"]["revision"], index)
                result = await task
            self.assertEqual(result["count"], 1)
            self.assertEqual(result["total"], 12)
            self.assertEqual(result["merged"], 1)
            self.assertEqual(result["scan"]["status"], "complete")
            self.assertEqual(result["opportunities"][0]["key"], "waste:deallocated_vms")
            if final:
                self.assertEqual(result["opportunities"], final["opportunities"])
            else:
                final = result
            # Publishing the final merged result must not mutate a previously delivered partial.
            self.assertEqual(snapshots[0]["opportunities"][0]["sources"][0]["source"], order[0])

    async def test_source_failures_are_named_including_invalid_and_cancelled(self):
        sources = self.sources({
            "waste": DATA["waste"],
            "rightsizing": {"error": "metrics forbidden"},
            "shutdown": {"candidates": [None]},
            "advisor": asyncio.CancelledError(),
        })
        sources.release()
        result = await savings.build(None, 30)
        self.assertEqual(result["total"], 12)
        self.assertEqual(result["scan"]["status"], "complete")
        self.assertEqual(set(result["scan"]["errors"]), {"rightsizing", "shutdown", "advisor"})
        self.assertEqual(result["unavailable"], result["scan"]["errors"])
        self.assertEqual(len(result["scan"]["completed"]), 4)
        self.assertIn("invalid response", result["unavailable"]["shutdown"])
        self.assertIn("cancelled", result["unavailable"]["advisor"])

    async def test_structured_source_errors_read_as_sentences_not_python(self):
        """The live Savings banner showed `['1 VM(s) ...']` and `{'unassociated_public_ips_
        pricing': ...` cut off mid-word at a fixed 200 characters."""
        waste_errors = {
            "unassociated_public_ips_pricing":
                "4 resource(s) have no complete currency-qualified billing cost.",
            "orphaned_nics_pricing":
                "12 resource(s) have no complete currency-qualified billing cost.",
            "deallocated_vms_pricing":
                "3 resource(s) have no complete currency-qualified billing cost.",
        }
        sources = self.sources({
            "waste": {**DATA["waste"], "errors": waste_errors},
            "rightsizing": {**DATA["rightsizing"],
                            "errors": ["1 VM(s) have no complete currency-qualified billing cost."]},
            "shutdown": DATA["shutdown"],
            "advisor": DATA["advisor"],
        })
        sources.release()
        result = await savings.build(None, 30)
        errors = result["scan"]["errors"]
        self.assertEqual(errors["rightsizing"],
                         "1 VM(s) have no complete currency-qualified billing cost.")
        self.assertTrue(errors["waste"].startswith(
            "unassociated public ips pricing: 4 resource(s) have no complete"))
        for text in errors.values():
            self.assertNotRegex(text, r"[\[\]{}]|'\w")
            self.assertLessEqual(len(text), 200)
        self.assertTrue(errors["waste"].endswith("…"))
        self.assertRegex(errors["waste"], r"\w…$")
        self.assertNotRegex(errors["waste"][:-1], r"\s$")

    def test_issue_text_keeps_short_text_and_cuts_long_text_at_a_word(self):
        self.assertEqual(savings._issue_text("metrics forbidden"), "metrics forbidden")
        self.assertEqual(savings._issue_text({"a_b": ["x", "y"]}), "a b: x; y")
        cut = savings._issue_text("alpha " * 50, 40)
        self.assertLessEqual(len(cut), 40)
        self.assertTrue(cut.endswith("alpha…"))

    async def test_source_timeout_cancels_only_unfinished_source(self):
        sources = self.sources()
        sources.release("waste", "advisor", "shutdown")
        self.stack.enter_context(patch.object(savings, "_SOURCE_TIMEOUT_SECONDS", 0.03))
        snapshots = []
        result = await savings.build(None, 30, on_progress=snapshots.append)
        self.assertEqual(result["scan"]["completed"][-1], "rightsizing")
        self.assertEqual(result["unavailable"], {"rightsizing": "The source timed out."})
        self.assertEqual(result["total"], 12)
        self.assertTrue(all(task.done() for task in sources.tasks))
        self.assertEqual(len(snapshots), 4)

    async def test_build_cancellation_drains_all_children(self):
        sources = self.sources()
        task = asyncio.create_task(savings.build(None, 30))
        await until(lambda: len(sources.calls) == 4)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertCountEqual(sources.finished, savings.SCAN_SOURCES)
        self.assertTrue(all(task.done() for task in sources.tasks))

    async def test_cold_poll_partial_full_waiter_and_force_join_one_scan(self):
        sources = self.sources()
        initial = await self.poll(currency="INR", scope="sub-a")
        self.assertEqual(initial["count"], 0)
        self.assertEqual(initial["scan"]["status"], "running")
        self.assertEqual(initial["scan"]["total"], 4)
        self.assertEqual(initial["freshness"], {
            "generated_at": None, "age_seconds": None, "stale": True,
            "max_age_seconds": main._SAVINGS_CACHE_SECONDS,
            "cached": False, "refreshing": True,
        })
        full = asyncio.create_task(main._get_savings(USER, ["sub-a"], 30, "INR"))
        await until(lambda: len(sources.calls) == 4)
        for _ in range(3):
            joined = await self.poll(currency="inr", scope="sub-a", refresh=True)
            self.assertEqual(joined["scan"], initial["scan"])
        sources.release("rightsizing")
        state = next(iter(main._savings_cache.values()))
        await until(lambda: len(state.scan["completed"]) == 1)
        partial = await self.poll(currency="INR", scope="sub-a")
        self.assertEqual(partial["total"], 960)
        self.assertEqual(partial["currency"], "INR")
        self.assertFalse(full.done())
        self.assertFalse(partial["freshness"]["cached"])
        sources.release()
        result = await full
        self.assertEqual(result["count"], 1)
        self.assertEqual(result["total"], 960)
        self.assertEqual(result["opportunities"][0]["sources"][0]["window"], 960)
        self.assertEqual(result["opportunities"][0]["items"][0]["cost"], 960)
        self.assertEqual(result["categories"][0]["total"], 960)
        self.assertFalse(result["freshness"]["cached"])
        for _ in range(3):
            done = await self.poll(currency="INR", scope="sub-a")
            self.assertEqual(done["scan"]["status"], "complete")
            self.assertTrue(done["freshness"]["cached"])
            self.assertFalse(done["freshness"]["stale"])
        generated = datetime.fromisoformat(done["freshness"]["generated_at"])
        self.assertEqual(generated.utcoffset(), UTC.utcoffset(generated))
        self.assertGreaterEqual(done["freshness"]["age_seconds"], 0)
        self.assertEqual(len(sources.calls), 4)

    async def test_stale_full_data_retained_until_refresh_finishes(self):
        sources = self.sources()
        sources.release()
        initial = await main._get_savings(USER, ["sub-a"], 30, "USD")
        state = next(iter(main._savings_cache.values()))
        self.expire(state)
        sources = self.sources({name: {"currency": "USD"} for name in savings.SCAN_SOURCES})
        stale = await self.poll(scope="sub-a", currency="USD")
        self.assertEqual(stale["opportunities"], initial["opportunities"])
        self.assertTrue(stale["freshness"]["stale"])
        self.assertTrue(stale["freshness"]["cached"])
        self.assertTrue(stale["freshness"]["refreshing"])
        self.assertGreaterEqual(stale["freshness"]["age_seconds"], 70)
        sources.release("waste")
        await until(lambda: len(state.scan["completed"]) == 1)
        partial = await self.poll(scope="sub-a", currency="USD")
        legacy = await main.dashboard_savings(request(), scope="sub-a", currency="USD")
        self.assertEqual(partial["opportunities"], initial["opportunities"])
        self.assertEqual(json.loads(legacy.body)["total"], initial["total"])
        self.assertEqual(partial["freshness"]["generated_at"], initial["freshness"]["generated_at"])
        sources.release()
        await self.drain()
        done = await self.poll(scope="sub-a", currency="USD")
        self.assertEqual(done["count"], 0)
        self.assertFalse(done["freshness"]["stale"])
        self.assertFalse(done["freshness"]["refreshing"])
        self.assertGreater(done["scan"]["revision"], stale["scan"]["revision"])
        self.assertEqual(len(sources.calls), 4)

    async def test_fresh_forced_refresh_joins_and_polls_do_not_restart(self):
        sources = self.sources()
        sources.release()
        await main._get_savings(USER, [], 30, "")
        refresh_sources = self.sources()
        first = await self.poll(refresh=True)
        second = await self.poll(refresh=True)
        self.assertEqual(first["scan"], second["scan"])
        self.assertFalse(first["freshness"]["stale"])
        self.assertTrue(first["freshness"]["refreshing"])
        self.assertEqual(first["total"], 12)
        refresh_sources.release()
        await self.drain()
        await self.poll()
        await self.poll()
        self.assertEqual(len(refresh_sources.calls), 4)

    async def test_terminal_status_is_atomic_with_committed_snapshot(self):
        sources = self.sources()
        sources.release()
        initial = await main._get_savings(USER, [], 30, "")
        state = next(iter(main._savings_cache.values()))
        self.expire(state)
        final_callback = asyncio.Event()
        drained = asyncio.Event()

        async def build(subs, days, currency, *, on_progress):
            result = savings.assemble({}, {}, days, currency)
            result["scan"] = {
                "status": "complete", "completed": list(savings.SCAN_SOURCES),
                "total": 4, "errors": {}, "revision": 4,
            }
            on_progress(result)
            final_callback.set()
            await drained.wait()
            return result

        self.stack.enter_context(patch.object(savings, "build", side_effect=build))
        await self.poll()
        await final_callback.wait()
        still_running = await self.poll()
        self.assertEqual(still_running["scan"]["status"], "running")
        self.assertTrue(still_running["freshness"]["refreshing"])
        self.assertEqual(still_running["opportunities"], initial["opportunities"])
        drained.set()
        await self.drain()
        done = await self.poll()
        self.assertEqual(done["scan"]["status"], "complete")
        self.assertEqual(done["total"], 0)
        self.assertFalse(done["freshness"]["stale"])

    async def test_all_failures_have_cooldown_and_no_generated_timestamp(self):
        sources = self.sources({name: RuntimeError(f"{name} failed") for name in savings.SCAN_SOURCES})
        sources.release()
        await self.poll()
        await self.drain()
        for _ in range(5):
            result = await self.poll()
            self.assertEqual(result["scan"]["status"], "error")
            self.assertEqual(set(result["scan"]["errors"]), set(savings.SCAN_SOURCES))
            self.assertIsNone(result["freshness"]["generated_at"])
            self.assertIsNone(result["freshness"]["age_seconds"])
            self.assertFalse(result["freshness"]["refreshing"])
            self.assertTrue(result["freshness"]["cached"])
        self.assertEqual(len(sources.calls), 4)
        self.assertFalse(main._savings_pending)

    async def test_failed_revalidation_keeps_old_data_and_throttles_retries(self):
        sources = self.sources()
        sources.release()
        initial = await main._get_savings(USER, [], 30, "")
        state = next(iter(main._savings_cache.values()))
        self.expire(state)
        sources = self.sources(dict.fromkeys(savings.SCAN_SOURCES))
        sources.release()
        await self.poll()
        await self.drain()
        for _ in range(3):
            result = await self.poll()
            self.assertEqual(result["total"], 12)
            self.assertEqual(result["scan"]["status"], "error")
            self.assertTrue(result["freshness"]["stale"])
            self.assertFalse(result["freshness"]["refreshing"])
            self.assertEqual(result["freshness"]["generated_at"], initial["freshness"]["generated_at"])
        self.assertEqual(len(sources.calls), 4)
        state.finished_at -= main._SAVINGS_CACHE_SECONDS + 1
        await self.poll()
        await self.drain()
        self.assertEqual(len(sources.calls), 8)

    async def test_actor_scope_days_and_currency_are_isolated(self):
        sources = self.sources()
        sources.release()
        first = await main._get_savings(USER, ["sub-b", "sub-a"], 30, "USD")
        normalized = await main._get_savings(USER, ["SUB-A", "sub-b", "sub-a"], 30, "usd")
        self.assertTrue(normalized["freshness"]["cached"])
        self.assertEqual(len(sources.calls), 4)
        await main._get_savings(User("bob", email=USER.email), ["sub-a", "sub-b"], 30, "USD")
        await main._get_savings(USER, ["sub-a"], 30, "USD")
        await main._get_savings(USER, ["sub-a", "sub-b"], 90, "USD")
        converted = await main._get_savings(USER, ["sub-a", "sub-b"], 30, "INR")
        self.assertEqual(first["total"], 12)
        self.assertEqual(converted["total"], 960)
        self.assertEqual(len(sources.calls), 20)
        self.assertEqual(len(main._savings_cache), 5)

    async def test_delegated_context_permissions_and_session_isolation(self):
        sources = self.sources()
        session = SimpleNamespace(oid="oid-alice", sid="sso-1")
        sso = SimpleNamespace(
            delegated=True, multi_tenant=False, get=Mock(return_value=session),
            caller=AsyncMock(return_value="test-token"),
            subscriptions=AsyncMock(return_value=[{"id": "sub-a"}]),
        )
        self.stack.enter_context(patch.object(main, "auth_service", return_value=SimpleNamespace(sso=sso)))
        user = User("alice", source="entra", email=USER.email, sid="sso-1")
        # A pick that mixes a permitted subscription with one the caller does not hold is
        # refused outright, not quietly reduced to the permitted part -- the shared policy in
        # main.selected_scope, covered by test_api_lifecycle and test_sso. Asserted here too
        # because this is the delegated path, where "allowed" comes from the SSO session
        # rather than the account, and because a refusal must not reach the sources at all.
        with self.assertRaises(HTTPException) as refused:
            await main.dashboard_savings_progress(
                request(user), scope="sub-a,forbidden", days=999, currency="USD")
        self.assertEqual(refused.exception.status_code, 403)
        self.assertEqual(len(sources.calls), 0)
        reply = await main.dashboard_savings_progress(
            request(user), scope="sub-a", days=999, currency="USD")
        self.assertEqual(json.loads(reply.body)["days"], 365)
        await until(lambda: len(sources.calls) == 4)
        for _, args, kwargs, actor in sources.calls:
            self.assertEqual(args[0] if args else kwargs["subscription_ids"], ["sub-a"])
            self.assertEqual(actor, "oid-alice")
        self.assertEqual(cost.acting_as(), "server")
        sources.release()
        await self.drain()
        other = User("alice", source="entra", email=USER.email, sid="sso-2")
        await main.dashboard_savings_progress(request(other), scope="sub-a", days=365, currency="USD")
        await self.drain()
        self.assertEqual(len(sources.calls), 8)
        sso.subscriptions.return_value = []
        # Empty for two quite different reasons, and they must not answer alike. A recorded
        # `problem` means ARM could not be asked, which is temporary and retryable; telling
        # that person "you have no access" sends them to request roles they may already hold.
        session.problem = "Azure Resource Manager did not respond."
        with self.assertRaises(HTTPException) as unavailable:
            await main.dashboard_savings_progress(request(user))
        self.assertEqual(unavailable.exception.status_code, 503)
        self.assertEqual(unavailable.exception.headers.get("Retry-After"), "5")
        # No problem recorded: Azure answered, and the answer is that they may see nothing.
        session.problem = None
        with self.assertRaises(HTTPException) as denied:
            await main.dashboard_savings_progress(request(user))
        self.assertEqual(denied.exception.status_code, 403)
        with self.assertRaises(HTTPException) as unsigned:
            await main.dashboard_savings_progress(request(None))
        self.assertEqual(unsigned.exception.status_code, 401)
        self.assertEqual(len(sources.calls), 8)

    async def test_waiter_disconnect_does_not_cancel_scan(self):
        sources = self.sources()
        waiter = asyncio.create_task(main._get_savings(USER, [], 30, ""))
        await until(lambda: len(sources.calls) == 4)
        scan = next(iter(main._savings_pending.values()))
        waiter.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await waiter
        self.assertFalse(scan.done())
        sources.release()
        await self.drain()
        result = await self.poll()
        self.assertEqual(result["scan"]["status"], "complete")
        self.assertEqual(len(sources.calls), 4)

    async def test_scan_timeout_and_cancellation_are_terminal_and_retry_bounded(self):
        self.stack.enter_context(patch.object(main, "_SAVINGS_SCAN_TIMEOUT_SECONDS", 0.03))
        sources = self.sources()
        await self.poll()
        sources.release("waste")
        await self.drain()
        result = await self.poll()
        self.assertEqual(result["scan"]["status"], "error")
        self.assertEqual(set(result["scan"]["errors"]), {"advisor", "rightsizing", "shutdown"})
        self.assertEqual(result["total"], 12)
        self.assertIsNone(result["freshness"]["generated_at"])
        self.assertEqual(len(sources.calls), 4)
        self.assertTrue(all(task.done() for task in sources.tasks))
        await self.poll(refresh=True)
        task = next(iter(main._savings_pending.values()))
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        await asyncio.sleep(0)
        result = await self.poll()
        self.assertEqual(result["scan"]["status"], "error")
        self.assertFalse(result["freshness"]["refreshing"])
        self.assertFalse(main._savings_pending)

    async def test_expired_delegated_context_remains_unauthorized_on_cached_failure(self):
        sso = SimpleNamespace(delegated=True, get=Mock(return_value=None))
        self.stack.enter_context(patch.object(main, "auth_service", return_value=SimpleNamespace(sso=sso)))
        user = User("alice", source="entra", email=USER.email, sid="expired")
        for _ in range(2):
            with self.assertRaises(HTTPException) as failure:
                await main._get_savings(user, ["sub-a"], 30, "")
            self.assertEqual(failure.exception.status_code, 401)
        self.assertFalse(main._savings_pending)

    async def test_cache_and_inflight_are_bounded_without_evicting_running_scan(self):
        self.stack.enter_context(patch.object(main, "_SAVINGS_CACHE_LIMIT", 3))
        self.stack.enter_context(patch.object(main, "_SAVINGS_PENDING_LIMIT", 2))
        sources = self.sources()
        active, task = main._start_savings(USER, ["active"], 30, "")
        joined, same = main._start_savings(USER, ["active"], 30, "", refresh=True)
        self.assertIs(joined, active)
        self.assertIs(task, same)
        main._start_savings(USER, ["second"], 30, "")
        with self.assertRaises(HTTPException) as busy:
            main._start_savings(USER, ["third"], 30, "")
        self.assertEqual(busy.exception.status_code, 503)
        self.assertEqual(len(main._savings_pending), 2)
        sources.release()
        await self.drain()
        for scope in ("third", "fourth", "fifth"):
            await main._get_savings(USER, [scope], 30, "")
        self.assertEqual(len(main._savings_cache), 3)
        self.assertFalse(main._savings_pending)
        self.assertEqual([key[1] for key in main._savings_cache],
                         [("third",), ("fourth",), ("fifth",)])

    async def test_overloaded_refresh_reports_busy_without_losing_cached_snapshot(self):
        self.stack.enter_context(patch.object(main, "_SAVINGS_PENDING_LIMIT", 2))
        sources = self.sources()
        sources.release()
        initial = await main._get_savings(USER, ["cached"], 30, "USD")
        state = next(iter(main._savings_cache.values()))
        self.expire(state)
        saved_result = state.result
        saved_scan = copy.deepcopy(state.scan)
        saved_generated = state.generated_at
        sources = self.sources()
        main._start_savings(USER, ["first"], 30, "USD")
        main._start_savings(USER, ["second"], 30, "USD")
        running = dict(main._savings_pending)
        for force in (True, False):
            with self.assertRaises(HTTPException) as busy:
                await self.poll(scope="cached", currency="USD", refresh=force)
            self.assertEqual(busy.exception.status_code, 503)
            self.assertEqual(busy.exception.headers["Retry-After"], "5")
            self.assertIs(state.result, saved_result)
            self.assertEqual(state.scan, saved_scan)
            self.assertEqual(state.generated_at, saved_generated)
            self.assertEqual(state.result["opportunities"], initial["opportunities"])
            self.assertEqual(main._savings_pending, running)
            self.assertTrue(all(not task.done() for task in running.values()))
        sources.release()
        await self.drain()
        self.assertEqual(len(sources.calls), 8)
        retry = await self.poll(scope="cached", currency="USD", refresh=True)
        self.assertEqual(retry["scan"]["status"], "running")
        self.assertEqual(retry["opportunities"], initial["opportunities"])
        await self.drain()

    async def test_shutdown_only_partial_retains_billed_currency(self):
        result = savings.assemble(
            {"shutdown": {"currency": "INR", "candidates": [
                {"name": "schedule-vm", "saving": 800}]}}, {}, 30, "USD")
        self.assertEqual(result["currency"], "USD")
        self.assertEqual(result["total"], 10)


if __name__ == "__main__":
    unittest.main(verbosity=2)
