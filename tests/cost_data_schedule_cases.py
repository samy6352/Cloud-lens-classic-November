"""Schedule-only approval cases shared by SQLite and isolated PostgreSQL."""
import asyncio
import copy
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException

from tests.cost_data_fixtures import update_plan
from tests.cost_data_review_cases import configuration, environment


def schedule_enables_without_history_tasks(monkeypatch):
    p = environment(monkeypatch)

    async def run():
        for cadence, expected in [
            ("Weekly", "2026-09-21T02:00:00+00:00"),
            ("Monthly", "2026-10-01T02:00:00+00:00"),
        ]:
            plan = await p.service.preview([{"id": "a"}], action="schedule", cadence=cadence)
            assert plan["from"] is None and plan["to"] is None
            assert plan["subscriptions"][0]["window"] is None
            assert not plan["subscriptions"][0]["blockers"]
            record = await p.service.create(plan, cadence)
            assert len(record["tasks"]) == 1
            assert record["tasks"][0]["operation"] == "configure"
            assert "from" not in record["tasks"][0]
            result = await p.jobs.run(record["job_id"], lambda record=record: p.flow.step(record["job_id"]))
            assert result["status"] == "ready"
            saved = p.service.read("a")
            assert saved["enabled"] and saved["cadence"] == cadence
            assert saved["next_check"] == expected
            assert saved["history"] is None
            assert (await p.service.create(plan, cadence))["job_id"] == record["job_id"]
            state = p.service.status([{"id": "a"}])["subscriptions"][0]
            assert state["configured"] and state["status"] == "ready"
            assert state["coverage"]["missing_days"] > 0
        p.schedules.run_now.assert_not_awaited()

    asyncio.run(run())


def schedule_preserves_saved_and_failed_setup_history(monkeypatch):
    p = environment(monkeypatch)
    original = configuration(p, through="2026-09-02")
    original["enabled"] = False
    p.service.write("a", original)

    async def run():
        plan = await p.service.preview([{"id": "a"}], action="schedule", cadence="Monthly")
        record = await p.service.create(plan, "recover-configuration")
        result = await p.jobs.run(record["job_id"], lambda: p.flow.step(record["job_id"]))
        assert result["status"] == "ready"
        saved = p.service.read("a")
        for field in ("history", "last_through", "catchup_anchor", "correction_due"):
            assert saved[field] == original[field]
        assert saved["cadence"] == "Monthly" and saved["enabled"]
        assert len(record["tasks"]) == 1
        p.schedules.run_now.assert_not_awaited()

    asyncio.run(run())


def schedule_does_not_discard_failed_update(monkeypatch):
    p = environment(monkeypatch)
    original = configuration(p)
    job = p.jobs.create("focus-update", ["a"], [{"subscription": "a"}])
    p.jobs.update(job["job_id"], status="failed")
    original["update_job"] = job["job_id"]
    p.service.write("a", original)
    before = copy.deepcopy(p.service.read("a"))

    async def run():
        for action in ("schedule", "frequency"):
            plan = await p.service.preview([{"id": "a"}], action=action, cadence="Weekly")
            assert any("Retry missing work" in b for b in plan["subscriptions"][0]["blockers"])
            with pytest.raises(ValueError, match="blockers"):
                await p.service.create(plan, action)
        assert p.service.read("a") == before

    asyncio.run(run())


def schedule_failure_can_be_reviewed_after_repair(monkeypatch):
    p = environment(monkeypatch)
    p.sync.owned.return_value = []
    p.sync.discover.return_value = {"schedules": [], "errors": []}
    provision = AsyncMock(side_effect=PermissionError("Storage delivery refused"))
    monkeypatch.setattr(p.schedules, "create", provision)

    async def run():
        plan = await p.service.preview([{"id": "a"}], action="schedule", cadence="Weekly")
        record = await p.service.create(plan, "first-attempt")
        result = await p.jobs.run(record["job_id"], lambda: p.flow.step(record["job_id"]))
        assert result["status"] == "failed"
        assert not p.service.read("a")["enabled"]
        assert p.service.status([{"id": "a"}])["subscriptions"][0]["next_check"] is None

        async def repaired(*args, **kwargs):
            assert kwargs["run_now"] is False
            p.sync.owned.return_value = [p.row]
        provision.side_effect = repaired
        fresh = await p.service.preview([{"id": "a"}], action="schedule", cadence="Monthly")
        assert not fresh["subscriptions"][0]["blockers"]
        recovered = await p.service.create(fresh, "review-after-repair")
        await p.jobs.run(recovered["job_id"], lambda: p.flow.step(recovered["job_id"]))
        assert p.service.read("a")["enabled"]
        assert p.service.read("a")["cadence"] == "Monthly"
        p.schedules.run_now.assert_not_awaited()

    asyncio.run(run())


def schedule_api_rechecks_approval_and_rejects_history(monkeypatch):
    p = environment(monkeypatch)
    from app import main

    request = SimpleNamespace(state=SimpleNamespace(user=SimpleNamespace(
        source="local", admin=True, name="Test", sid="session")))
    permission = AsyncMock()
    resolve = AsyncMock(return_value=[{"id": "a"}])
    monkeypatch.setattr(main, "_assert_cost_data_approval", permission)
    monkeypatch.setattr(main, "_cost_data_subscriptions", resolve)

    async def run():
        for handler in (main.preview_cost_data, main.start_cost_data):
            for extra in ({"start": "2026-09-01"}, {"history_days": 90}):
                with pytest.raises(HTTPException) as exc:
                    await handler(main.CostDataRequest(scope=["a"], action="schedule", **extra), request)
                assert exc.value.status_code == 400
        resolve.assert_not_awaited()
        body = main.CostDataRequest(scope=["a"], action="schedule", cadence="Weekly")
        preview = json.loads((await main.preview_cost_data(body, request)).body)
        assert preview["can_start"] and p.jobs.listing() == []
        approved = body.model_copy(update={"preview_token": preview["preview_token"], "idempotency_key": "schedule-api"})
        permission.side_effect = HTTPException(403, "Caller access revoked")
        with pytest.raises(HTTPException) as exc:
            await main.start_cost_data(approved, request)
        assert exc.value.status_code == 403 and p.jobs.listing() == []
        permission.side_effect = None
        changed = approved.model_copy(update={"cadence": "Monthly"})
        with pytest.raises(HTTPException) as exc:
            await main.start_cost_data(changed, request)
        assert exc.value.status_code == 409 and p.jobs.listing() == []
        started = json.loads((await main.start_cost_data(approved, request)).body)
        assert started["payload"]["action"] == "schedule"
        assert len(started["tasks"]) == 1
        assert json.loads((await main.start_cost_data(approved, request)).body)["job_id"] == started["job_id"]
        assert len(p.jobs.listing()) == 1

    asyncio.run(run())


def schedule_only_runs_its_cadence_when_due(monkeypatch):
    p = environment(monkeypatch)

    async def run():
        plan = await p.service.preview([{"id": "a"}], action="schedule", cadence="Weekly")
        record = await p.service.create(plan, "weekly-due")
        await p.jobs.run(record["job_id"], lambda: p.flow.step(record["job_id"]))
        saved = p.service.read("a")
        async def preview_window(subscriptions, *, start, end, **kwargs):
            return update_plan("a", start, end, p.row)
        preview = AsyncMock(side_effect=preview_window)
        monkeypatch.setattr(p.flow, "preview", preview)
        await p.service.tick([("a", saved)])
        preview.assert_not_awaited()
        clock = p.now.replace(day=21)
        class DueClock(type(p.now)):
            @classmethod
            def now(cls, tz=None):
                return clock
        monkeypatch.setattr(p.service, "datetime", DueClock)
        monkeypatch.setattr(p.flow, "datetime", DueClock)
        await p.service.tick([("a", saved)])
        assert preview.await_args.kwargs["start"] == "2026-09-08"
        assert preview.await_args.kwargs["end"] == "2026-09-21"
        current = p.service.read("a")
        assert current["next_check"] == "2026-09-28T02:00:00+00:00"
        assert p.jobs.get(current["update_job"])["kind"] == "focus-update"
        await p.service.tick([("a", current)])
        preview.assert_awaited_once()

    asyncio.run(run())


CASES = [
    schedule_enables_without_history_tasks,
    schedule_preserves_saved_and_failed_setup_history,
    schedule_does_not_discard_failed_update,
    schedule_failure_can_be_reviewed_after_repair,
    schedule_api_rechecks_approval_and_rejects_history,
    schedule_only_runs_its_cadence_when_due,
]
