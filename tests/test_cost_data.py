"""Approved cost-data setup and calendar scheduling, without Azure writes."""
import asyncio
import copy
from datetime import UTC, date, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from tests.cost_data_history_cases import CASES as HISTORY_CASES
from tests.cost_data_review_cases import CASES
from tests.cost_data_schedule_cases import CASES as SCHEDULE_CASES
from tests.test_focus_schedules import offline_modules  # noqa: F401
from tests.test_job_orchestration import durable  # noqa: F401


@pytest.mark.parametrize("case", CASES + HISTORY_CASES + SCHEDULE_CASES, ids=lambda case: case.__name__)
def test_review_regressions_offline(durable, monkeypatch, case):  # noqa: F811
    case(monkeypatch)


@pytest.mark.parametrize("cadence,clock,expected", [
    ("Daily", "2026-01-01T01:59:00+00:00", "2026-01-01T02:00:00+00:00"),
    ("Daily", "2026-01-01T02:00:00+00:00", "2026-01-02T02:00:00+00:00"),
    ("Weekly", "2026-09-14T01:59:00+00:00", "2026-09-14T02:00:00+00:00"),
    ("Weekly", "2026-09-14T02:00:00+00:00", "2026-09-21T02:00:00+00:00"),
    ("Monthly", "2024-02-29T15:00:00+00:00", "2024-03-01T02:00:00+00:00"),
    ("Monthly", "2026-01-01T01:59:00+00:00", "2026-01-01T02:00:00+00:00"),
    ("Monthly", "2026-01-01T02:00:00+00:00", "2026-02-01T02:00:00+00:00"),
])
def test_calendar_boundaries(cadence, clock, expected):
    from app import cost_data
    assert cost_data.next_check(cadence, datetime.fromisoformat(clock)) == expected


@pytest.mark.parametrize("cadence,today,last,expected", [
    ("Daily", date(2026, 1, 1), None, ("2025-12-26", "2026-01-01")),
    ("Weekly", date(2026, 3, 2), None, ("2026-02-17", "2026-03-02")),
    ("Weekly", date(2026, 3, 2), "2026-01-10", ("2025-12-28", "2026-03-02")),
    ("Monthly", date(2024, 3, 1), None, ("2024-01-01", "2024-02-29")),
    ("Monthly", date(2026, 1, 1), None, ("2025-11-01", "2025-12-31")),
    ("Monthly", date(2026, 1, 1), "2025-08-15", ("2025-07-01", "2025-12-31")),
    ("Daily", date(2026, 1, 1), "2023-01-01", ("2022-12-26", "2023-12-26")),
])
def test_overlap_and_downtime_windows(cadence, today, last, expected):
    from app import cost_data
    assert cost_data.update_window(cadence, today, last) == expected


@pytest.fixture
def pipeline(durable, monkeypatch):  # noqa: F811
    from app import archive, cost_data, exports, focus_sync, focus_workflow, schedules
    monkeypatch.setenv("AZURE_CLIENT_ID", "synthetic-workload")
    monkeypatch.setenv("AUTO_FOCUS_SYNC", "true")
    monkeypatch.setattr(archive, "enabled", lambda: True)
    monkeypatch.setattr(archive, "ACCOUNT_ID", "/subscriptions/storage/storageAccounts/offline")
    row = {"name": "owned", "subscription_id": "a", "status": "Active", "etag": "v1",
           "account": "offline", "container": "focus", "folder": "a", "owned": True,
           "timeframe": "MonthToDate"}
    monkeypatch.setattr(focus_sync, "discover", AsyncMock(return_value={"schedules": [row], "errors": []}))
    monkeypatch.setattr(focus_sync, "owned", AsyncMock(side_effect=lambda scope: [copy.deepcopy(row)]))
    monkeypatch.setattr(focus_workflow, "manifests", AsyncMock(return_value=[]))
    monkeypatch.setattr(focus_workflow, "launch", lambda record: None)
    monkeypatch.setattr(schedules, "run_now", AsyncMock())
    async def pause(sub, *, active, etag):
        assert etag == row["etag"]
        row.update(status="Inactive", etag="v2")
    monkeypatch.setattr(schedules, "set_native_active", AsyncMock(side_effect=pause))
    monkeypatch.setattr(exports, "ingest_export", AsyncMock(return_value={"rows": 1, "changed_days": 1}))
    return SimpleNamespace(service=cost_data, flow=focus_workflow, sync=focus_sync,
                           jobs=durable[0], row=row, schedules=schedules, exports=exports)


def plan(p, **kwargs):
    return copy.deepcopy(asyncio.run(p.service.preview([{"id": "a", "name": "Alpha"}],
                                         start="2026-06-17", end="2026-06-30", **kwargs)))


def test_setup_reuses_an_existing_complete_report_rather_than_re_exporting(pipeline, monkeypatch):
    """The Cost data screen tells customers that days already imported are reused from their
    existing reports rather than re-exported from Azure. That sentence is only honest while
    a setup plan over a covered window resolves to a reuse, so it is pinned here."""
    from app import focus_workflow

    covering = SimpleNamespace(export_name="owned", dataset="FocusCost",
                               submitted="2026-07-01T00:00:00+00:00", run_id="already-produced")
    monkeypatch.setattr(focus_workflow, "manifests", AsyncMock(return_value=[covering]))
    monkeypatch.setattr(focus_workflow, "actual_window", lambda m: ("2026-06-01", "2026-07-31"))

    tasks = plan(pipeline)["subscriptions"][0]
    assert tasks["action"] == "Reuse owned export and pause its native recurrence"

    prepared = asyncio.run(focus_workflow.preview(
        [{"id": "a", "name": "Alpha"}], start="2026-06-17", end="2026-06-30", purpose="setup"))
    work = prepared["subscriptions"][0]["tasks"]
    assert [t["reuse_run"] for t in work] == ["already-produced"] * len(work)
    assert {t["action"] for t in work} == {"validate and import existing report"}

    # A reconcile deliberately re-runs the window, so the same wording must never appear there.
    rechecked = asyncio.run(focus_workflow.preview(
        [{"id": "a", "name": "Alpha"}], start="2026-06-17", end="2026-06-30", purpose="reconcile"))
    rerun = rechecked["subscriptions"][0]["tasks"]
    assert [t["reuse_run"] for t in rerun] == [None] * len(rerun)
    assert {t["action"] for t in rerun} == {"execute custom window"}


def test_preview_is_pure_stable_and_bound_to_etag(pipeline):
    p = pipeline
    first = plan(p)
    assert p.service.read("a") == {}
    assert first["preview_token"] == plan(p)["preview_token"]
    p.row["etag"] = "changed"
    assert first["preview_token"] != plan(p)["preview_token"]
    p.schedules.set_native_active.assert_not_awaited()
    p.schedules.run_now.assert_not_awaited()


def test_one_approval_configures_and_persists_waiting_history_without_reissuing(pipeline):
    p = pipeline
    approved = plan(p, cadence="Weekly")
    record = asyncio.run(p.service.create(approved, "one-click"))
    assert record["identity"] == "workload"
    assert len(record["tasks"]) == 2
    first = asyncio.run(p.jobs.run(record["job_id"], lambda: p.flow.step(record["job_id"])))
    assert first["status"] == "waiting-for-Azure"
    assert p.service.read("a")["cadence"] == "Weekly"
    assert p.service.read("a")["enabled"]
    assert p.row["status"] == "Inactive"
    asyncio.run(p.jobs.run(record["job_id"], lambda: p.flow.step(record["job_id"])))
    p.schedules.run_now.assert_awaited_once()
    p.schedules.set_native_active.assert_awaited_once()
    assert asyncio.run(p.service.create(approved, "one-click"))["job_id"] == record["job_id"]


def test_missing_export_created_once_under_approved_workload_then_configured(pipeline, monkeypatch):
    p = pipeline
    p.sync.discover.return_value["schedules"] = []
    available = []
    p.sync.owned.side_effect = lambda scope: copy.deepcopy(available)
    async def provision(*args, **kwargs):
        assert kwargs["run_now"] is False
        available.append(p.row)
    monkeypatch.setattr(p.schedules, "create", AsyncMock(side_effect=provision))
    record = asyncio.run(p.service.create(plan(p), "provision"))
    asyncio.run(p.jobs.run(record["job_id"], lambda: p.flow.step(record["job_id"])))
    asyncio.run(p.jobs.run(record["job_id"], lambda: p.flow.step(record["job_id"])))
    p.schedules.create.assert_awaited_once()
    assert p.service.read("a")["enabled"]


def test_permission_failure_preserves_history_and_retry_skips_completed_configuration(pipeline):
    p = pipeline
    record = asyncio.run(p.service.create(plan(p), "failed-run"))
    p.schedules.run_now.side_effect = PermissionError("403 Cost Management Contributor needed")
    result = asyncio.run(p.jobs.run(record["job_id"], lambda: p.flow.step(record["job_id"])))
    assert result["status"] == "partial"
    assert p.jobs.get(record["job_id"])["tasks"][0]["status"] == "ready"
    p.schedules.run_now.side_effect = None
    asyncio.run(p.flow.resume(record["job_id"]))
    asyncio.run(p.jobs.run(record["job_id"], lambda: p.flow.step(record["job_id"])))
    p.schedules.set_native_active.assert_awaited_once()
    p.exports.ingest_export.assert_not_awaited()


def test_status_is_database_only_and_never_marks_quick_as_focus(pipeline):
    p = pipeline
    state = p.service.status([{"id": "a", "name": "Alpha"}])
    assert state["setup_needed"]
    assert state["subscriptions"][0]["coverage"]["authoritative_days"] == 0
    p.sync.discover.assert_not_awaited()
    p.sync.owned.assert_not_awaited()


def test_configuration_conflict_does_not_pause_replaced_export(pipeline):
    p = pipeline
    record = asyncio.run(p.service.create(copy.deepcopy(plan(p)), "conflict"))
    p.row["etag"] = "v3"
    result = asyncio.run(p.jobs.run(record["job_id"], lambda: p.flow.step(record["job_id"])))
    assert result["status"] == "failed"
    p.schedules.set_native_active.assert_not_awaited()
    p.schedules.run_now.assert_not_awaited()
    assert not p.service.read("a")["enabled"]


def test_interactive_only_identity_gets_explicit_blocker(pipeline, monkeypatch):
    for key in ("AZURE_CLIENT_ID", "IDENTITY_ENDPOINT", "MSI_ENDPOINT", "AZURE_FEDERATED_TOKEN_FILE"):
        monkeypatch.delenv(key, raising=False)
    approved = plan(pipeline)
    assert any("Interactive sign-in alone" in b for b in approved["subscriptions"][0]["blockers"])
    with pytest.raises(ValueError, match="blockers"):
        asyncio.run(pipeline.service.create(approved, "no-workload"))


def test_legacy_cadence_is_not_migrated_until_approved(pipeline):
    p = pipeline
    legacy = {"mode": "native-month-to-date", "enabled": True, "identity": "workload"}
    p.service.write("a", legacy)
    assert asyncio.run(p.service.tick([("a", legacy)])) == set()
    assert p.service.read("a") == legacy
    plan(p)
    assert p.service.read("a") == legacy


def test_weekly_tick_catches_up_then_avoids_parallel_or_repeated_work(pipeline, monkeypatch):
    p = pipeline
    setup = p.jobs.create("focus-setup", ["a"], payload={"action": "setup", "to": "2026-06-30"})
    p.jobs.update(setup["job_id"], status="ready")
    config = {"version": 2, "enabled": True, "identity": "workload", "mode": "managed",
              "cadence": "Weekly", "setup_job": setup["job_id"],
              "last_through": "2026-06-30", "next_check": "2026-07-06T02:00:00+00:00"}
    p.service.write("a", config)
    monkeypatch.setattr(p.flow, "preview", AsyncMock(return_value={
        "purpose": "update", "from": "2026-07-01", "to": "2026-09-15",
        "subscriptions": [{"subscription": "a", "export": p.row, "missing_capabilities": [],
                           "tasks": [{"from": "2026-07-01", "to": "2026-07-31"}]}]}))
    assert asyncio.run(p.service.tick([("a", config)])) == {"a"}
    saved = p.service.read("a")
    assert saved["update_job"]
    assert p.flow.preview.await_args.kwargs["start"] == "2026-06-17"
    assert saved["last_through"] == "2026-06-30"
    asyncio.run(p.service.tick([("a", saved)]))
    p.flow.preview.assert_awaited_once()


def test_current_month_unsupported_custom_falls_through_to_native_without_false_success(pipeline):
    p = pipeline
    today = datetime.now(UTC).date().isoformat()
    approved = asyncio.run(p.service.preview([{"id": "a"}], start=today, end=today))
    record = asyncio.run(p.service.create(approved, "unsupported"))
    error = RuntimeError("Custom periods unsupported")
    error.status_code = 400
    p.schedules.run_now.side_effect = [error, {}]
    result = asyncio.run(p.jobs.run(record["job_id"], lambda: p.flow.step(record["job_id"])))
    assert result["status"] == "waiting-for-Azure"
    assert p.schedules.run_now.await_count == 2
    assert p.schedules.run_now.await_args.kwargs == {}
    p.exports.ingest_export.assert_not_awaited()
    task = p.jobs.get(record["job_id"])["tasks"][1]
    assert task["compatibility"] == "native-month-to-date"


def test_unsupported_historical_window_stays_incomplete(pipeline):
    p = pipeline
    record = asyncio.run(p.service.create(plan(p), "historical-unsupported"))
    error = RuntimeError("Custom periods unsupported")
    error.status_code = 400
    p.schedules.run_now.side_effect = error
    result = asyncio.run(p.jobs.run(record["job_id"], lambda: p.flow.step(record["job_id"])))
    assert result["status"] == "partial"
    p.schedules.run_now.assert_awaited_once()
    p.exports.ingest_export.assert_not_awaited()
    assert p.service.status([{"id": "a"}])["subscriptions"][0]["coverage"]["missing_days"] == 14


def test_legacy_import_loop_cannot_bypass_managed_weekly_cadence(pipeline):
    p = pipeline
    p.service.write("a", {"version": 2, "cadence": "Weekly", "enabled": True})
    assert asyncio.run(p.sync.run(["a"]))["status"] == "cancelled"
    p.sync.discover.assert_not_awaited()
    p.schedules.run_now.assert_not_awaited()
    with pytest.raises(ValueError, match="Cost data owns"):
        asyncio.run(p.sync.run(["a"], request_new=True))


def test_accepted_but_wrong_historical_coverage_is_partial_not_infinite_wait(pipeline, monkeypatch):
    p = pipeline
    record = asyncio.run(p.service.create(plan(p), "wrong-window"))
    asyncio.run(p.jobs.run(record["job_id"], lambda: p.flow.step(record["job_id"])))
    p.flow.manifests.return_value = [SimpleNamespace(run_id="wrong", submitted="2099-01-01T00:00:00Z")]
    monkeypatch.setattr(p.flow, "actual_window", lambda m: ("2026-09-01", "2026-09-15"))
    result = asyncio.run(p.jobs.run(record["job_id"], lambda: p.flow.step(record["job_id"])))
    assert result["status"] == "partial"
    assert "remains incomplete" in p.jobs.get(record["job_id"])["tasks"][1]["detail"]
    p.exports.ingest_export.assert_not_awaited()


def test_delegated_admin_must_prove_their_own_right_to_authorize_workload(pipeline, monkeypatch):
    from fastapi import HTTPException

    from app import cost, main
    user = SimpleNamespace(source="entra", sid="signed-in", admin=True)
    monkeypatch.setattr(main, "assert_may_write_schedule", AsyncMock())
    monkeypatch.setattr(main, "auth_service", lambda: SimpleNamespace(sso=SimpleNamespace(
        delegated=True, get=lambda sid: SimpleNamespace(oid="person"))))
    from contextlib import asynccontextmanager
    @asynccontextmanager
    async def as_user(user):
        yield
    monkeypatch.setattr(main, "as_user", as_user)
    check = AsyncMock(return_value=False)
    monkeypatch.setattr(cost, "principal_can_write_cost", check)
    with pytest.raises(HTTPException) as error:
        asyncio.run(main._assert_cost_data_approval(user, "a"))
    assert error.value.status_code == 403
    check.assert_awaited_once_with("person", "a")
    pipeline.schedules.set_native_active.assert_not_awaited()


def test_read_only_blocks_setup_and_cadence_before_any_network(pipeline, monkeypatch):
    p = pipeline
    monkeypatch.setenv("CLOUDLENS_READ_ONLY", "true")
    assert asyncio.run(p.service.tick([("a", {"version": 2, "enabled": True})])) == set()
    with pytest.raises(PermissionError):
        asyncio.run(p.service.configure({}, {}))
    with pytest.raises(PermissionError):
        asyncio.run(p.service.create({}, "read-only"))
    p.sync.discover.assert_not_awaited()
    p.schedules.set_native_active.assert_not_awaited()


def test_competing_approvals_cannot_disable_or_replace_first_accepted_configuration(pipeline):
    p = pipeline
    approved = plan(p)
    first = asyncio.run(p.service.create(approved, "first"))
    second = asyncio.run(p.service.create(approved, "second"))
    asyncio.run(p.jobs.run(first["job_id"], lambda: p.flow.step(first["job_id"])))
    configured = p.service.read("a")
    assert configured["enabled"]
    result = asyncio.run(p.jobs.run(second["job_id"], lambda: p.flow.step(second["job_id"])))
    assert result["status"] == "failed"
    assert p.service.read("a") == configured
    p.schedules.set_native_active.assert_awaited_once()
