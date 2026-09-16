"""The same four review regressions against offline storage and isolated PostgreSQL."""
import asyncio
import copy
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from datetime import UTC, date, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException

from tests.cost_data_fixtures import finish_windows, receipt_days, update_plan


def environment(monkeypatch, clock="2026-09-15T02:00:00+00:00"):
    from app import archive, cost_data, focus_sync, focus_workflow, jobs, schedules
    now = datetime.fromisoformat(clock)
    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return now.astimezone(tz or UTC)
    monkeypatch.setattr(cost_data, "datetime", Clock)
    monkeypatch.setenv("AZURE_CLIENT_ID", "offline-workload")
    monkeypatch.setenv("AUTO_FOCUS_SYNC", "true")
    monkeypatch.setattr(archive, "enabled", lambda: True)
    monkeypatch.setattr(archive, "ACCOUNT_ID", "/subscriptions/storage/storageAccounts/offline")
    row = {"name": "owned", "subscription_id": "a", "owned": True, "status": "Inactive",
           "etag": "v1", "account": "offline", "container": "focus", "folder": "a"}
    monkeypatch.setattr(focus_sync, "owned", AsyncMock(return_value=[row]))
    monkeypatch.setattr(focus_sync, "discover", AsyncMock(return_value={"schedules": [row], "errors": []}))
    monkeypatch.setattr(focus_workflow, "manifests", AsyncMock(return_value=[]))
    monkeypatch.setattr(schedules, "set_native_active", AsyncMock())
    monkeypatch.setattr(schedules, "run_now", AsyncMock())
    launched = []
    monkeypatch.setattr(focus_workflow, "launch", launched.append)
    return SimpleNamespace(service=cost_data, jobs=jobs, flow=focus_workflow, sync=focus_sync,
                           row=row, schedules=schedules, launched=launched, now=now)


def configuration(p, *, cadence="Daily", through="2026-09-02"):
    setup = p.jobs.create("focus-setup", ["a"], payload={
        "action": "setup", "from": "2026-09-01", "to": through})
    p.jobs.update(setup["job_id"], status="ready")
    config = {"version": 2, "cadence": cadence, "identity": "workload", "mode": "managed",
              "enabled": True, "setup_job": setup["job_id"], "last_through": through,
              "history": {"from": "2026-09-01", "to": through},
              "catchup_anchor": "2026-09-01", "next_check": "2026-01-01T02:00:00+00:00",
              "correction_due": "2026-10-07"}
    p.service.write("a", config)
    return config


def capture_previews(p, monkeypatch):
    async def preview(subscriptions, *, start, end, **kwargs):
        return update_plan("a", start, end, p.row)
    mocked = AsyncMock(side_effect=preview)
    monkeypatch.setattr(p.flow, "preview", mocked)
    return mocked


def initial_anchor_survives_frequency_change(monkeypatch, *, legacy=False):
    p = environment(monkeypatch)
    async def run():
        first_plan = await p.service.preview([{"id": "a"}], cadence="Monthly",
                                             start="2026-09-01", end="2026-09-02")
        first = await p.service.create(first_plan, "initial")
        await p.jobs.run(first["job_id"], lambda: p.flow.step(first["job_id"]))
        finish_windows(first["job_id"])
        assert p.service.read("a")["last_through"] is None
        if legacy:
            old_config = p.service.read("a")
            old_config.pop("history")
            old_config.pop("catchup_anchor")
            p.service.write("a", old_config)
        frequency = await p.service.preview([{"id": "a"}], cadence="Daily", action="frequency")
        changed = await p.service.create(frequency, "frequency")
        await p.jobs.run(changed["job_id"], lambda: p.flow.step(changed["job_id"]))
        saved = p.service.read("a")
        assert saved["setup_job"] == changed["job_id"]
        assert saved["catchup_anchor"] == "2026-09-01"
        assert saved["last_through"] is None
        saved["next_check"] = p.now.isoformat()
        p.service.write("a", saved)
        preview = capture_previews(p, monkeypatch)
        await p.service.tick([("a", saved)])
        assert p.service.read("a")["last_through"] == "2026-09-02"
        assert preview.await_args.kwargs["start"] <= "2026-09-02"
        assert preview.await_args.kwargs["end"] == "2026-09-15"
    asyncio.run(run())


def existing_configuration_recovers_anchor_before_frequency_change(monkeypatch):
    initial_anchor_survives_frequency_change(monkeypatch, legacy=True)


def incomplete_initial_history_does_not_advance_watermark(monkeypatch):
    p = environment(monkeypatch)
    config = configuration(p)
    config["last_through"] = None
    p.service.write("a", config)
    receipt_days("a", "2026-09-01", "2026-09-01")
    captured = capture_previews(p, monkeypatch)
    asyncio.run(p.service.tick([("a", config)]))
    assert p.service.read("a")["last_through"] is None
    assert captured.await_args.kwargs["start"] == "2026-09-01"


def downtime_revisits_provisional_watermark_day(monkeypatch):
    p = environment(monkeypatch, "2026-09-10T02:00:00+00:00")
    config = configuration(p, through="2026-09-01")
    receipt_days("a", "2026-09-01", "2026-09-01")
    captured = capture_previews(p, monkeypatch)
    asyncio.run(p.service.tick([("a", config)]))
    assert captured.await_args.kwargs["start"] == "2026-08-26"
    assert captured.await_args.kwargs["end"] == "2026-09-10"
    job = p.jobs.get(p.service.read("a")["update_job"])
    assert any(t["from"] <= "2026-09-01" <= t["to"] for t in job["tasks"])
    assert p.service.read("a")["last_through"] == "2026-09-01"


def older_corrections_run_monthly_not_daily(monkeypatch):
    p = environment(monkeypatch, "2026-06-07T02:00:00+00:00")
    config = configuration(p, through="2026-06-01")
    config.update(history={"from": "2026-06-01", "to": "2026-06-01"},
                  catchup_anchor="2026-06-01", correction_due="2026-06-07")
    p.service.write("a", config)
    capture_previews(p, monkeypatch)
    asyncio.run(p.service.tick([("a", config)]))
    saved = p.service.read("a")
    job = p.jobs.get(saved["update_job"])
    assert (date.fromisoformat(job["payload"]["to"]) - date.fromisoformat(job["payload"]["from"])).days == 89
    finish_windows(job["job_id"])
    p = environment(monkeypatch, "2026-06-08T02:00:00+00:00")
    capture_previews(p, monkeypatch)
    asyncio.run(p.service.tick([("a", saved)]))
    saved = p.service.read("a")
    assert saved["correction_due"] == "2026-07-07"
    next_job = p.jobs.get(saved["update_job"])
    assert (next_job["payload"]["from"], next_job["payload"]["to"]) == ("2026-06-02", "2026-06-08")
    assert next_job["payload"]["reconcile_due"] is None


def frequency_change_during_preflight_cannot_be_overwritten(monkeypatch):
    p = environment(monkeypatch)
    config = configuration(p)
    async def run():
        entered, release = asyncio.Event(), asyncio.Event()
        async def preview(subscriptions, *, start, end, **kwargs):
            entered.set()
            await release.wait()
            return update_plan("a", start, end, p.row)
        monkeypatch.setattr(p.flow, "preview", preview)
        tick = asyncio.create_task(p.service.tick([("a", config)]))
        await entered.wait()
        new_job = p.jobs.create("focus-setup", ["a"], payload={"action": "frequency"})
        p.jobs.update(new_job["job_id"], status="ready")
        approved = p.service.read("a")
        approved.update(cadence="Monthly", setup_job=new_job["job_id"])
        p.service.write("a", approved)
        release.set()
        await tick
        assert p.service.read("a") == approved
        assert not [j for j in p.jobs.listing() if j["kind"] == "focus-update"]
    asyncio.run(run())


def pending_job_created_during_preflight_prevents_attachment(monkeypatch):
    p = environment(monkeypatch)
    config = configuration(p)
    async def run():
        entered, release = asyncio.Event(), asyncio.Event()
        async def preview(subscriptions, *, start, end, **kwargs):
            entered.set()
            await release.wait()
            return update_plan("a", start, end, p.row)
        monkeypatch.setattr(p.flow, "preview", preview)
        tick = asyncio.create_task(p.service.tick([("a", config)]))
        await entered.wait()
        p.jobs.create("focus-setup", ["a"], payload={"action": "frequency"})
        release.set()
        await tick
        assert "update_job" not in p.service.read("a")
        assert not [j for j in p.jobs.listing() if j["kind"] == "focus-update"]
    asyncio.run(run())


def creation_and_attachment_rollback_together(monkeypatch):
    p = environment(monkeypatch)
    configuration(p)
    expected, _ = p.service._prepare_tick("a", p.now)
    plan = update_plan("a", "2026-09-01", "2026-09-15")
    original = p.service._put_config
    def fail_attachment(con, sub, config):
        if config.get("update_job"):
            raise RuntimeError("Synthetic attachment failure")
        return original(con, sub, config)
    monkeypatch.setattr(p.service, "_put_config", fail_attachment)
    with pytest.raises(RuntimeError, match="attachment failure"):
        p.service._attach_update("a", expected, plan, p.now, reconcile_due=None)
    assert p.service.read("a") == expected
    assert not [j for j in p.jobs.listing() if j["kind"] == "focus-update"]


def concurrent_schedulers_attach_exactly_one_job(monkeypatch):
    p = environment(monkeypatch)
    configuration(p)
    expected, _ = p.service._prepare_tick("a", p.now)
    plan = update_plan("a", "2026-09-01", "2026-09-15")
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(p.service._attach_update, "a", expected, plan, p.now, reconcile_due=None)
                   for _ in range(2)]
        records = [future.result(timeout=15) for future in futures]
    assert sum(record is not None for record in records) == 1
    only = next(record for record in records if record)
    assert p.service.read("a")["update_job"] == only["job_id"]
    assert len([j for j in p.jobs.listing() if j["kind"] == "focus-update"]) == 1


def incomplete_or_obsolete_work_never_advances_catchup(monkeypatch):
    p = environment(monkeypatch)
    configuration(p)
    expected, _ = p.service._prepare_tick("a", p.now)
    record = p.service._attach_update("a", expected, update_plan("a", "2026-09-03", "2026-09-15"),
                                     p.now, reconcile_due=None)
    for task in record["tasks"]:
        p.jobs.update(record["job_id"], task_id=task["task_id"], status="ready")
    p.jobs.update(record["job_id"], status="ready")
    p.service._prepare_tick("a", p.now)
    assert p.service.read("a")["last_through"] == "2026-09-02"
    assert p.jobs.get(record["job_id"])["status"] in {"failed", "partial"}
    finish_windows(record["job_id"])
    changed = p.service.read("a")
    changed["setup_job"] = "newer-approved-setup"
    p.service.write("a", changed)
    p.service._prepare_tick("a", p.now)
    assert p.service.read("a")["last_through"] == "2026-09-02"


def recovered_unattached_job_cannot_execute(monkeypatch):
    p = environment(monkeypatch)
    configuration(p)
    record = p.jobs.create("focus-update", ["a"], [{
        "subscription": "a", "from": "2026-09-03", "to": "2026-09-15",
        "schedule": p.row, "force_new": True}], identity="workload")
    result = asyncio.run(p.jobs.run(record["job_id"], lambda: p.flow.step(record["job_id"])))
    assert result["status"] == "failed"
    p.sync.owned.assert_not_awaited()
    p.schedules.run_now.assert_not_awaited()
    assert p.service.read("a")["last_through"] == "2026-09-02"


def delegated_reader_cannot_resume_workload_jobs(monkeypatch):
    p = environment(monkeypatch)
    from app import cost, main
    user = SimpleNamespace(source="entra", sid="session", admin=True, name="Reader")
    request = SimpleNamespace(state=SimpleNamespace(user=user))
    monkeypatch.setattr(main, "permitted", AsyncMock(return_value=["a"]))
    monkeypatch.setattr(main, "auth_service", lambda: SimpleNamespace(sso=SimpleNamespace(
        delegated=True, get=lambda sid: SimpleNamespace(oid="reader-object-id"))))
    @asynccontextmanager
    async def as_user(user):
        yield
    monkeypatch.setattr(main, "as_user", as_user)
    capability = AsyncMock(return_value=False)
    monkeypatch.setattr(cost, "principal_can_write_cost", capability)
    reset = AsyncMock(side_effect=p.flow.resume)
    monkeypatch.setattr(p.flow, "resume", reset)
    async def run():
        for kind in ("focus-setup", "focus-update"):
            for handler in (main.resume_focus_job, main.resume_ingest_job):
                record = p.jobs.create(kind, ["a"], [{"subscription": "a"}], identity="workload")
                p.jobs.update(record["job_id"], status="failed")
                before = copy.deepcopy(p.jobs.get(record["job_id"]))
                with pytest.raises(HTTPException) as error:
                    await handler(record["job_id"], request)
                assert error.value.status_code == 403
                assert p.jobs.get(record["job_id"]) == before
        reset.assert_not_awaited()
        assert not p.launched
        capability.return_value = True
        response = await main.resume_focus_job(record["job_id"], request)
        assert response.status_code == 202
        reset.assert_awaited_once()
        capability.assert_awaited_with("reader-object-id", "a")
    asyncio.run(run())


CASES = [
    initial_anchor_survives_frequency_change,
    existing_configuration_recovers_anchor_before_frequency_change,
    incomplete_initial_history_does_not_advance_watermark,
    downtime_revisits_provisional_watermark_day,
    older_corrections_run_monthly_not_daily,
    frequency_change_during_preflight_cannot_be_overwritten,
    pending_job_created_during_preflight_prevents_attachment,
    creation_and_attachment_rollback_together,
    concurrent_schedulers_attach_exactly_one_job,
    incomplete_or_obsolete_work_never_advances_catchup,
    recovered_unattached_job_cannot_execute,
    delegated_reader_cannot_resume_workload_jobs,
]
