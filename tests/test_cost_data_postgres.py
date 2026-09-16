"""One approval through real durable jobs and FOCUS import; isolated PostgreSQL, fake Azure."""
import asyncio
import copy
import json
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from tests.cost_data_fixtures import finish_windows, receipt_days
from tests.cost_data_history_cases import CASES as HISTORY_CASES
from tests.cost_data_review_cases import CASES
from tests.cost_data_schedule_cases import CASES as SCHEDULE_CASES
from tests.test_job_postgres import _report

pytestmark = pytest.mark.postgres


@pytest.mark.parametrize("case", CASES + HISTORY_CASES + SCHEDULE_CASES, ids=lambda case: case.__name__)
def test_review_regressions_postgres(postgres_warehouse, monkeypatch, case):
    case(monkeypatch)


def test_one_click_setup_reuses_report_persists_cadence_and_coverage(postgres_warehouse, monkeypatch):
    from app import (
        archive,
        cost,
        cost_data,
        exports,
        focus_sync,
        focus_workflow,
        jobs,
        main,
        schedules,
    )
    wh = postgres_warehouse
    monkeypatch.setenv("AZURE_CLIENT_ID", "synthetic-workload")
    monkeypatch.setenv("AUTO_FOCUS_SYNC", "true")
    monkeypatch.setattr(archive, "enabled", lambda: True)
    monkeypatch.setattr(archive, "ACCOUNT_ID", "/subscriptions/storage/storageAccounts/offline")
    monkeypatch.setattr(main, "permitted", AsyncMock(return_value=["sub-a"]))
    monkeypatch.setattr(cost, "_resolve", AsyncMock(return_value=[{"id": "sub-a", "name": "Alpha"}]))
    permission = AsyncMock()
    monkeypatch.setattr(main, "assert_may_write_schedule", permission)
    @asynccontextmanager
    async def as_user(user):
        yield
    monkeypatch.setattr(main, "as_user", as_user)
    launched = []
    monkeypatch.setattr(focus_workflow, "launch", launched.append)
    row = {"name": "owned", "subscription_id": "sub-a", "status": "Active", "etag": "v1",
           "account": "offline", "container": "focus", "folder": "", "owned": True,
           "timeframe": "MonthToDate"}
    monkeypatch.setattr(focus_sync, "discover", AsyncMock(side_effect=lambda scope:
        {"schedules": [copy.deepcopy(row)], "errors": []}))
    monkeypatch.setattr(focus_sync, "owned", AsyncMock(side_effect=lambda scope: [copy.deepcopy(row)]))
    files = _report()
    class Reader:
        account, container = "offline", "focus"
        async def list(self, prefix=""):
            return [{"name": name, "etag": "v1", "modified": "2026-07-01"} for name in files]
        async def get(self, name):
            return files[name]
        async def close(self):
            pass
    monkeypatch.setattr(exports, "BlobReader", lambda *args: Reader())
    monkeypatch.setattr(wh, "refresh_rollup_async", AsyncMock())
    async def pause(sub, *, active, etag):
        assert sub == "sub-a" and not active and etag == "v1"
        row.update(status="Inactive", etag="v2")
    pause_mock = AsyncMock(side_effect=pause)
    monkeypatch.setattr(schedules, "set_native_active", pause_mock)
    execute = AsyncMock()
    monkeypatch.setattr(schedules, "run_now", execute)
    request = SimpleNamespace(state=SimpleNamespace(user=SimpleNamespace(
        admin=True, source="local", name="Test", sid="session")), query_params={})
    body = main.CostDataRequest(scope=["sub-a"], cadence="Weekly", start="2026-06-17", end="2026-06-30")
    async def run():
        preview = json.loads((await main.preview_cost_data(body, request)).body)
        assert preview["can_start"]
        assert jobs.listing() == []
        pause_mock.assert_not_awaited()
        approved = body.model_copy(update={"preview_token": preview["preview_token"], "idempotency_key": "approved"})
        started = json.loads((await main.start_cost_data(approved, request)).body)
        assert started["identity"] == "workload" and started["status"] == "queued"
        result = await jobs.run(started["job_id"], lambda: focus_workflow.step(started["job_id"]))
        assert result["status"] == "ready"
        state = cost_data.status([{"id": "sub-a", "name": "Alpha"}])
        assert state["subscriptions"][0]["coverage"]["authoritative_days"] == 14
        assert not state["setup_needed"]
        assert cost_data.read("sub-a")["cadence"] == "Weekly"
        assert cost_data.read("sub-a")["enabled"]
        replay = json.loads((await main.start_cost_data(approved, request)).body)
        assert replay["job_id"] == started["job_id"]
        assert wh.row_count() == 2
        assert jobs.get(started["job_id"])["tasks"][1]["result"]["source_bytes"] > 0
        execute.assert_not_awaited()
        pause_mock.assert_awaited_once()
    asyncio.run(run())


@pytest.mark.parametrize("cadence", ["Daily", "Weekly", "Monthly"])
def test_due_job_and_watermark_are_durable_and_single_authority(postgres_warehouse, monkeypatch, cadence):
    from app import cost_data, focus_workflow, jobs
    setup = jobs.create("focus-setup", ["sub-a"], payload={"action": "setup", "to": "2026-06-30"})
    jobs.update(setup["job_id"], status="ready")
    receipt_days("sub-a", "2026-06-30", "2026-06-30")
    config = {"version": 2, "mode": "managed", "cadence": cadence, "enabled": True,
              "identity": "workload", "setup_job": setup["job_id"], "next_check": "2026-07-01T02:00:00+00:00"}
    cost_data.write("sub-a", config)
    plan = {"purpose": "update", "from": "2026-07-01", "to": "2026-07-31",
            "subscriptions": [{"subscription": "sub-a", "export": {"name": "owned"},
                               "missing_capabilities": [], "tasks": [{"from": "2026-07-01", "to": "2026-07-31"}]}]}
    preview = AsyncMock(return_value=plan)
    monkeypatch.setattr(focus_workflow, "preview", preview)
    monkeypatch.setattr(focus_workflow, "launch", lambda record: None)
    async def run():
        assert await cost_data.tick([("sub-a", config)]) == {"sub-a"}
        saved = cost_data.read("sub-a")
        assert jobs.get(saved["update_job"])["identity"] == "workload"
        assert saved["last_through"] == "2026-06-30"
        await cost_data.tick([("sub-a", saved)])
        preview.assert_awaited_once()
        finish_windows(saved["update_job"])
        await cost_data.tick([("sub-a", saved)])
        assert cost_data.read("sub-a")["last_through"] == "2026-07-31"
    asyncio.run(run())
