"""FOCUS scheduling, scoped status and automatic/manual ingestion without Azure writes."""
import asyncio
import copy
import json
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException

from tests.test_focus_schedules import offline_modules  # noqa: F401


@pytest.fixture
def services(monkeypatch):
    from app import cost, exports, focus_sync, main, schedules, warehouse

    records = {}
    monkeypatch.setattr(warehouse.warehouse, "get_meta", lambda key: None)
    monkeypatch.setattr(focus_sync, "_active", set())
    monkeypatch.setattr(focus_sync, "_read", lambda sub: copy.deepcopy(
        records.get(sub, {"subscription": sub, "status": "idle"})))

    def write(sub, data):
        records[sub] = {**records.get(sub, {}), **data, "subscription": sub}

    monkeypatch.setattr(focus_sync, "_write", write)
    row = {"name": "owned", "subscription_id": "sub-a", "owned": True, "status": "Active",
           "account": "storage", "container": "focus-exports", "folder": "focus/sub-a"}
    listing = AsyncMock(return_value={"schedules": [row],
                                     "subscriptions": [{"id": "sub-a", "name": "A"}], "errors": []})
    monkeypatch.setattr(schedules, "listing", listing)
    monkeypatch.setattr(main, "_ingest_task", None)
    monkeypatch.setattr(main, "permitted", AsyncMock(return_value=["sub-a"]))
    monkeypatch.setattr(cost, "_resolve", AsyncMock(return_value=[{"id": "sub-a", "name": "A"}]))

    @asynccontextmanager
    async def caller(user):
        yield

    monkeypatch.setattr(main, "as_user", caller)
    return SimpleNamespace(cost=cost, exports=exports, sync=focus_sync, main=main, schedules=schedules,
                           records=records, listing=listing, row=row)


def request(admin=True):
    return SimpleNamespace(state=SimpleNamespace(user=SimpleNamespace(
        admin=admin, source="local", name="Test", sid="session")), query_params={})


def test_manual_refresh_waits_for_a_new_current_month_report(services, monkeypatch):
    s = services
    month = datetime.now(UTC).strftime("%Y-%m")
    old = SimpleNamespace(run_id="old", submitted="old", start=f"{month}-01")
    new = SimpleNamespace(run_id="new", submitted="new", start=f"{month}-01")
    monkeypatch.setattr(s.sync, "_runs", AsyncMock(side_effect=[[old], [old], [old, new]]))
    sleep = AsyncMock()
    monkeypatch.setattr(s.sync.asyncio, "sleep", sleep)
    run = AsyncMock()
    monkeypatch.setattr(s.schedules, "run_now", run)
    ingest = AsyncMock(return_value={"to": f"{month}-10", "rows": 100,
                                    "changed_days": 2, "unchanged_days": 8})
    monkeypatch.setattr(s.exports, "ingest_export", ingest)
    result = asyncio.run(s.sync.run(["sub-a"], request_new=True))
    run.assert_awaited_once_with("sub-a", "owned", current_month=True)
    sleep.assert_awaited_once()
    assert ingest.await_args.kwargs["scope"] == ["sub-a"]
    assert ingest.await_args.kwargs["required_metric"] == "FocusCost"
    assert result["status"] == "ready"
    assert s.records["sub-a"]["changed_days"] == 2
    assert s.records["sub-a"]["last_ingested"]


def test_automatic_ingestion_never_creates_or_runs_exports(services, monkeypatch):
    s = services
    monkeypatch.setattr(s.sync, "_runs", AsyncMock(return_value=[SimpleNamespace(run_id="r", submitted="now")]))
    create, run = AsyncMock(), AsyncMock()
    monkeypatch.setattr(s.schedules, "create", create)
    monkeypatch.setattr(s.schedules, "run_now", run)
    ingest = AsyncMock(return_value={"to": "2026-08-31", "rows": 0,
                                    "changed_days": 0, "unchanged_days": 31})
    monkeypatch.setattr(s.exports, "ingest_export", ingest)
    result = asyncio.run(s.sync.run(["sub-a"]))
    assert result["status"] == "ready"
    create.assert_not_awaited()
    run.assert_not_awaited()
    assert result["subscriptions"][0]["through"] == "2026-08-31"


def test_missing_reports_and_failures_are_not_reported_as_import_success(services, monkeypatch):
    s = services
    monkeypatch.setattr(s.sync, "_runs", AsyncMock(return_value=[]))
    ingest = AsyncMock()
    monkeypatch.setattr(s.exports, "ingest_export", ingest)
    result = asyncio.run(s.sync.run(["sub-a"]))
    assert result["status"] == "idle"
    ingest.assert_not_awaited()
    monkeypatch.setattr(s.sync, "_runs", AsyncMock(side_effect=RuntimeError("Storage access denied")))
    result = asyncio.run(s.sync.run(["sub-a"]))
    assert result["status"] == "failed"
    assert "Storage access denied" in result["subscriptions"][0]["detail"]
    assert "last_ingested" not in result["subscriptions"][0]


def test_status_never_exposes_another_subscription(services):
    services.records.update({"sub-a": {"status": "ready", "subscription": "sub-a"},
                            "sub-b": {"status": "failed", "subscription": "sub-b", "detail": "private"}})
    result = asyncio.run(services.sync.status(["sub-a"]))
    assert result["status"] == "ready"
    assert "private" not in json.dumps(result)
    assert len(result["subscriptions"]) == 1
    assert asyncio.run(services.sync.owned([])) == []


def test_app_identity_permissions_are_reported_per_subscription(services, monkeypatch):
    s = services
    s.listing.return_value["subscriptions"].append({"id": "sub-b", "name": "B"})
    s.listing.return_value["schedules"].append({**s.row, "subscription_id": "sub-b"})

    async def permission(user, sub):
        if sub == "sub-b":
            raise HTTPException(403, "Requires Cost Management Contributor.")

    monkeypatch.setattr(s.main, "assert_may_write_schedule", permission)
    response = asyncio.run(s.main.list_schedules(request()))
    data = json.loads(response.body)
    assert data["can_write"]
    assert [sub["can_write"] for sub in data["subscriptions"]] == [True, False]
    assert [row["can_write"] for row in data["schedules"]] == [True, False]


def test_refresh_authorizes_every_subscription_before_starting(services, monkeypatch):
    s = services
    started = []
    monkeypatch.setattr(s.main, "_start_ingest_task", lambda factory: started.append(factory))
    monkeypatch.setattr(s.main, "assert_may_write_schedule", AsyncMock(side_effect=HTTPException(403, "Read only")))
    with pytest.raises(HTTPException) as error:
        asyncio.run(s.main.refresh_latest_focus(s.main.FocusRefreshRequest(scope=["sub-a"]), request()))
    assert error.value.status_code == 403
    assert not started
    with pytest.raises(HTTPException):
        asyncio.run(s.main.refresh_latest_focus(s.main.FocusRefreshRequest(scope=["sub-b"]), request()))
    assert not started


def test_manual_month_override_does_not_change_saved_cadence(services, monkeypatch):
    call = AsyncMock(return_value={})
    monkeypatch.setattr(services.schedules, "_request", call)
    asyncio.run(services.schedules.run_now("sub-a", "owned", current_month=True))
    assert call.await_args.args[0] == "POST"
    assert call.await_args.args[1].endswith("/owned/run")
    period = call.await_args.kwargs["body"]["timePeriod"]
    assert period["from"][8:19] == "01T00:00:00"
    assert period["to"][:7] == period["from"][:7]
    assert "schedule" not in call.await_args.kwargs["body"]


def test_export_run_failure_cannot_ingest_an_old_report(services, monkeypatch):
    s = services
    old = SimpleNamespace(run_id="old", submitted="old", start="2026-08-01")
    monkeypatch.setattr(s.sync, "_runs", AsyncMock(return_value=[old]))
    monkeypatch.setattr(s.schedules, "run_now", AsyncMock(side_effect=RuntimeError("Azure throttled the run")))
    ingest = AsyncMock()
    monkeypatch.setattr(s.exports, "ingest_export", ingest)
    result = asyncio.run(s.sync.run(["sub-a"], request_new=True))
    assert result["status"] == "failed"
    assert "throttled" in result["subscriptions"][0]["detail"]
    ingest.assert_not_awaited()


def test_wait_timeout_reports_no_new_data_and_preserves_prior_success(services, monkeypatch):
    s = services
    s.records["sub-a"] = {"subscription": "sub-a", "last_ingested": "previous-success"}
    monkeypatch.setattr(s.sync, "_runs", AsyncMock(return_value=[]))
    monkeypatch.setattr(s.sync, "WAIT_SECONDS", 0)
    monkeypatch.setattr(s.schedules, "run_now", AsyncMock())
    ingest = AsyncMock()
    monkeypatch.setattr(s.exports, "ingest_export", ingest)
    result = asyncio.run(s.sync.run(["sub-a"], request_new=True))
    assert result["status"] == "waiting-for-Azure"
    assert "can take hours" in result["subscriptions"][0]["detail"]
    assert result["subscriptions"][0]["last_ingested"] == "previous-success"
    ingest.assert_not_awaited()


def test_refresh_starts_one_shared_job_after_preflight(services, monkeypatch):
    from app import focus_workflow
    s = services
    permission = AsyncMock()
    monkeypatch.setattr(s.main, "assert_may_write_schedule", permission)
    jobs = []

    plan = {"purpose": "refresh"}
    monkeypatch.setattr(focus_workflow, "preview", AsyncMock(return_value=plan))
    create = AsyncMock(return_value={"job_id": "test-job"})
    monkeypatch.setattr(focus_workflow, "create", create)
    monkeypatch.setattr(focus_workflow, "launch", jobs.append)
    result = asyncio.run(s.main.refresh_latest_focus(
        s.main.FocusRefreshRequest(scope=["sub-a"]), request()))
    assert result.status_code == 202
    assert jobs[0]["job_id"] == "test-job"
    assert create.await_args.kwargs["force_new"] is True
    permission.assert_awaited_once()
    monkeypatch.setattr(s.main, "_ingest_task", SimpleNamespace(done=lambda: False))
    with pytest.raises(HTTPException) as error:
        asyncio.run(s.main.refresh_latest_focus(s.main.FocusRefreshRequest(scope=["sub-a"]), request()))
    assert error.value.status_code == 409
    assert len(jobs) == 1


def test_native_schedule_cannot_silently_replace_compact_mode(services, monkeypatch):
    from app.warehouse import warehouse
    s = services
    monkeypatch.setattr(s.main, "assert_may_write_schedule", AsyncMock())
    monkeypatch.setattr(warehouse, "get_meta", lambda key: '{"enabled":true,"mode":"compact"}')
    create = AsyncMock()
    monkeypatch.setattr(s.schedules, "create", create)
    with pytest.raises(HTTPException) as error:
        asyncio.run(s.main.create_schedule(
            s.main.ScheduleRequest(subscription="sub-a", run_now=False), request()))
    assert error.value.status_code == 409
    create.assert_not_awaited()


def test_queued_schedule_does_not_reingest_its_old_report(services, monkeypatch):
    s = services
    old = SimpleNamespace(run_id="old", submitted="before")
    s.records["sub-a"] = {"subscription": "sub-a", "status": "waiting",
                          "awaiting_runs": [["old", "before"]]}
    monkeypatch.setattr(s.sync, "_runs", AsyncMock(return_value=[old]))
    ingest = AsyncMock()
    monkeypatch.setattr(s.exports, "ingest_export", ingest)
    result = asyncio.run(s.sync.run(["sub-a"]))
    assert result["status"] == "idle"
    assert s.records["sub-a"]["awaiting_runs"] == [["old", "before"]]
    ingest.assert_not_awaited()
    new = SimpleNamespace(run_id="new", submitted="after")
    monkeypatch.setattr(s.sync, "_runs", AsyncMock(return_value=[old, new]))
    ingest.return_value = {"rows": 5, "to": "2026-09-10", "changed_days": 1, "unchanged_days": 0}
    result = asyncio.run(s.sync.run(["sub-a"]))
    assert result["status"] == "ready"
    assert s.records["sub-a"]["awaiting_runs"] is None


def test_owned_run_records_the_old_reports_before_starting_ingestion(services, monkeypatch):
    from app import focus_workflow
    from app import jobs as durable_jobs
    s = services
    name = s.schedules.export_name("FocusCost", **s.schedules.destination("sub-a"))
    capture = AsyncMock(return_value=[["old", "before"]])
    execute = AsyncMock(return_value={"name": name, "started": True})
    monkeypatch.setattr(s.sync, "capture", capture)
    monkeypatch.setattr(s.schedules, "run_now", execute)
    monkeypatch.setattr(s.main, "assert_may_write_schedule", AsyncMock())
    jobs = []
    record = {"job_id": "test-job", "created_at": "2026-09-14", "tasks": [{"task_id": "one"}]}
    monkeypatch.setattr(focus_workflow, "preview", AsyncMock(return_value={}))
    monkeypatch.setattr(focus_workflow, "create", AsyncMock(return_value=record))
    monkeypatch.setattr(focus_workflow, "launch", jobs.append)
    changes = []
    monkeypatch.setattr(durable_jobs, "update", lambda jid, **kw: changes.append(kw))
    monkeypatch.setattr(durable_jobs, "get", lambda jid: record)
    result = asyncio.run(s.main.run_schedule(request(), "sub-a", name))
    assert result.status_code == 200
    capture.assert_awaited_once_with("sub-a")
    execute.assert_awaited_once_with("sub-a", name)
    assert s.records["sub-a"]["awaiting_runs"] == [["old", "before"]]
    assert jobs[0]["job_id"] == "test-job"
    assert changes[0]["baseline"] == [["old", "before"]]


def test_failed_subscription_does_not_block_healthy_imports_or_hide_its_error(services, monkeypatch):
    s = services
    s.listing.return_value["errors"] = [{"subscription": "sub-b", "detail": "Export list denied"}]
    monkeypatch.setattr(s.sync, "_runs", AsyncMock(return_value=[SimpleNamespace(run_id="r", submitted="now")]))
    ingest = AsyncMock(return_value={"to": "2026-09-10", "rows": 61, "changed_days": 1, "unchanged_days": 0})
    monkeypatch.setattr(s.exports, "ingest_export", ingest)
    monkeypatch.setenv("EXPORT_MAX_FILES", "100")
    result = asyncio.run(s.sync.run(["sub-a", "sub-b"]))
    assert result["status"] == "partial"
    assert s.records["sub-a"]["status"] == "ready"
    assert s.records["sub-b"]["detail"] == "Export list denied"
    ingest.assert_awaited_once()
    assert ingest.await_args.kwargs["subscription_id"] == "sub-a"
    assert ingest.await_args.kwargs["max_files"] == 100
    with pytest.raises(s.schedules.ScheduleError, match="Export list denied"):
        asyncio.run(s.sync.owned(["sub-a", "sub-b"]))


def test_automatic_loop_keeps_healthy_scopes_when_discovery_is_partial(services, monkeypatch):
    from app import archive, focus_workflow

    s = services
    monkeypatch.setenv("AUTO_FOCUS_SYNC", "true")
    monkeypatch.setattr(archive, "enabled", lambda: True)
    monkeypatch.setattr(s.cost, "list_subscriptions", AsyncMock(
        return_value={"subscriptions": [{"id": "sub-a"}, {"id": "sub-b"}]}))
    s.listing.return_value["errors"] = [{"subscription": "sub-b", "detail": "Denied"}]
    run = AsyncMock(return_value={"status": "partial"})
    monkeypatch.setattr(s.sync, "run", run)
    async def start(factory, **kwargs):
        return asyncio.create_task(factory())
    monkeypatch.setattr(s.main, "_start_ingest_task", start)
    monkeypatch.setattr(focus_workflow, "daily_tick", AsyncMock(return_value=set()))
    monkeypatch.setattr(s.main.asyncio, "sleep", AsyncMock(side_effect=[None, asyncio.CancelledError()]))
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(s.main._focus_refresh_loop())
    run.assert_awaited_once_with(["sub-a", "sub-b"])
