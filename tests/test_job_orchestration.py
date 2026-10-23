"""Offline durable job contracts; PostgreSQL locking is also exercised in the PG suite."""
import asyncio
import sqlite3
import threading
import time
from contextlib import asynccontextmanager, contextmanager
from datetime import date
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import UUID

import pytest

from tests.test_focus_schedules import offline_modules  # noqa: F401

pytestmark = pytest.mark.offline


@pytest.fixture
def durable(monkeypatch):
    from app import jobs, warehouse
    db = sqlite3.connect(":memory:", check_same_thread=False, isolation_level=None)
    db.execute("CREATE TABLE meta(key TEXT PRIMARY KEY, value TEXT, updated_at TEXT)")
    gate = threading.RLock()

    class Connection:
        def execute(self, sql, params=()):
            if "pg_advisory_xact_lock" in sql:
                return db.execute("SELECT 1")
            if "extract(epoch" in sql:
                return db.execute("SELECT ?", [time.time()])
            return db.execute(sql.replace("now()", "CURRENT_TIMESTAMP"), params)

    @contextmanager
    def connect(**kwargs):
        with gate:
            yield Connection()

    monkeypatch.setattr(warehouse.warehouse, "connect", connect)
    yield jobs, db
    db.close()


def test_durable_uuid_idempotency_and_scope(durable):
    jobs, _ = durable
    task = {"subscription": "a", "from": "2026-06-17", "to": "2026-06-30"}
    record = jobs.create("focus-bootstrap", ["a"], [task], idempotency_key="retry")
    assert str(UUID(record["job_id"])) == record["started"]
    assert jobs.create("focus-bootstrap", ["a"], [task], idempotency_key="retry") == record
    with pytest.raises(ValueError, match="different request"):
        jobs.create("focus-bootstrap", ["a"], [], idempotency_key="retry")
    assert jobs.listing(["b"]) == []
    assert jobs.listing(["a"])[0]["job_id"] == record["job_id"]
    jobs.update(record["job_id"], started="mutated", scope=["b"], status="ready")
    assert jobs.get(record["job_id"])["started"] == record["job_id"]
    assert jobs.get(record["job_id"])["scope"] == ["a"]


def test_lease_ownership_and_commit_fence(durable):
    jobs, _ = durable
    record = jobs.create("refresh", ["a"])
    assert jobs.claim(record["job_id"], "first")
    assert not jobs.claim(record["job_id"], "second")
    token = jobs.current_lease.set("second")
    try:
        with jobs.transaction() as con, pytest.raises(RuntimeError, match="lease expired"):
            jobs.assert_lease(con)
        with pytest.raises(RuntimeError, match="lease expired"):
            jobs.update(record["job_id"], status="failed")
        assert jobs.get(record["job_id"])["status"] == "queued"
    finally:
        jobs.current_lease.reset(token)
    jobs.release("second")
    assert not jobs.claim(record["job_id"], "second")
    jobs.release("first")
    assert jobs.claim(record["job_id"], "second")


def test_waiting_is_durable_and_terminal_jobs_not_reexecuted(durable):
    jobs, _ = durable
    record = jobs.create("focus-bootstrap", ["a"])
    wait = AsyncMock(return_value={"status": "waiting-for-Azure"})
    asyncio.run(jobs.run(record["job_id"], wait))
    assert jobs.get(record["job_id"])["status"] == "waiting-for-Azure"
    ready = AsyncMock(return_value={"status": "ready"})
    asyncio.run(jobs.run(record["job_id"], ready))
    replay = AsyncMock()
    asyncio.run(jobs.run(record["job_id"], replay))
    replay.assert_not_awaited()
    assert jobs.get(record["job_id"])["started"] == record["job_id"]


def test_exact_90_days_crosses_four_months():
    from app.focus_workflow import chunks, window
    lo, hi = window(90, date(2026, 9, 14))
    assert (lo, hi) == ("2026-06-17", "2026-09-14")
    assert chunks(lo, hi) == [
        {"from": "2026-06-17", "to": "2026-06-30"},
        {"from": "2026-07-01", "to": "2026-07-31"},
        {"from": "2026-08-01", "to": "2026-08-31"},
        {"from": "2026-09-01", "to": "2026-09-14"},
    ]
    assert window(90, date(2024, 3, 1))[0] == "2023-12-03"


def test_explicit_run_window_is_bounded_and_keeps_definition(monkeypatch):
    from app import schedules
    request = AsyncMock(return_value={})
    monkeypatch.setattr(schedules, "_request", request)
    asyncio.run(schedules.run_now("sub-a", "owned", start="2026-06-17", end="2026-06-30"))
    assert request.await_args.kwargs["body"] == {"timePeriod": {
        "from": "2026-06-17T00:00:00Z", "to": "2026-06-30T23:59:59Z"}}
    with pytest.raises(schedules.ScheduleError, match="month-bounded"):
        asyncio.run(schedules.run_now("sub-a", "owned", start="2026-06-17", end="2026-07-01"))


def test_quick_data_never_establishes_focus_coverage(durable):
    from app.focus_workflow import coverage
    assert coverage("a", "2026-06-17", "2026-09-14")["missing_days"] == 90


def test_pending_run_resumes_without_reissuing_and_only_one_per_subscription(durable, monkeypatch):
    from app import focus_sync, schedules
    from app import focus_workflow as flow
    jobs, _ = durable
    schedule = {"name": "owned", "subscription_id": "a", "account": "store",
                "container": "focus", "folder": "a", "owned": True}
    tasks = [{"subscription": "a", "from": lo, "to": hi, "schedule": schedule,
              "missing_capabilities": [], "force_new": False} for lo, hi in
             [("2026-06-17", "2026-06-30"), ("2026-07-01", "2026-07-31")]]
    record = jobs.create("focus-bootstrap", ["a"], tasks)
    monkeypatch.setattr(focus_sync, "owned", AsyncMock(return_value=[schedule]))
    monkeypatch.setattr(focus_sync, "record", AsyncMock())
    monkeypatch.setattr(flow, "manifests", AsyncMock(return_value=[]))
    run = AsyncMock()
    monkeypatch.setattr(schedules, "run_now", run)
    asyncio.run(flow.step(record["job_id"]))
    asyncio.run(flow.step(record["job_id"]))
    assert run.await_count == 1
    saved = jobs.get(record["job_id"])
    assert saved["tasks"][0]["status"] == "waiting-for-Azure"
    assert saved["tasks"][1]["status"] == "queued"
    assert saved["tasks"][0]["requested_at"]


def test_resume_preserves_completed_chunks_and_identity(durable):
    from app import focus_workflow as flow
    jobs, _ = durable
    record = jobs.create("focus-bootstrap", ["a"], [{"subscription": "a"}, {"subscription": "a"}])
    first, second = record["tasks"]
    jobs.update(record["job_id"], task_id=first["task_id"], status="ready", result={"rows": 10})
    jobs.update(record["job_id"], task_id=second["task_id"], status="failed")
    resumed = asyncio.run(flow.resume(record["job_id"]))
    assert resumed["tasks"][0]["status"] == "ready"
    assert resumed["tasks"][0]["result"] == {"rows": 10}
    assert resumed["tasks"][1]["status"] == "queued"
    assert resumed["job_id"] == record["job_id"]


def test_retry_after_and_denial_are_distinct():
    import httpx

    from app.focus_workflow import retry_delay
    for status, expected in [(429, 1700), (503, 1700), (403, None)]:
        response = httpx.Response(status, headers={"Retry-After": "1700"})
        error = RuntimeError(str(status))
        error.response = response
        assert retry_delay(error) == expected


def test_incomplete_manifest_remains_retryable_through_validation_cause():
    from app.focus_workflow import retry_delay
    try:
        raise ValueError("Partitions are not all present yet")
    except ValueError as cause:
        error = RuntimeError("No data changed; retry after Azure finishes writing the manifest.")
        error.__cause__ = cause
    assert retry_delay(error) == 60


@pytest.fixture
def submission_job(durable, monkeypatch):
    from app import focus_sync
    from app import focus_workflow as flow

    jobs, _ = durable
    schedule = {"name": "owned", "subscription_id": "a", "account": "store",
                "container": "focus", "folder": "a", "owned": True}
    monkeypatch.setattr(focus_sync, "owned", AsyncMock(return_value=[schedule]))
    monkeypatch.setattr(focus_sync, "record", AsyncMock())
    monkeypatch.setattr(flow, "manifests", AsyncMock(return_value=[]))
    record = jobs.create("focus-bootstrap", ["a"], [{
        "subscription": "a", "from": "2026-06-17", "to": "2026-06-30",
        "schedule": schedule, "force_new": True}])
    return jobs, flow, record


@pytest.mark.parametrize("status", [400, 403, 429, 503])
@pytest.mark.parametrize("wrapped", [False, True])
def test_actual_cost_error_rejected_submission_retries_without_stale_intent(
        submission_job, monkeypatch, status, wrapped):
    import httpx

    from app import cost, schedules

    jobs, flow, record = submission_job
    requests, errors = [], []
    reject = True
    original_client = httpx.AsyncClient
    original_run = schedules.run_now

    def respond(request):
        requests.append(request)
        return httpx.Response(status if reject else 202, headers={"Retry-After": "1700"},
                              json={"error": {"message": "offline response"}} if reject else {})

    monkeypatch.setattr(cost.azure, "_bearer", AsyncMock(return_value="offline-token"))
    monkeypatch.setattr(cost.httpx, "AsyncClient", lambda **kwargs: original_client(
        transport=httpx.MockTransport(respond), **kwargs))
    sleep = AsyncMock()
    monkeypatch.setattr(cost.asyncio, "sleep", sleep)

    async def submit(*args, **kwargs):
        try:
            return await original_run(*args, **kwargs)
        except schedules.ScheduleError as failure:
            error = failure.__cause__
            assert isinstance(error, cost.CostError)
            errors.append(error)
            if wrapped:
                raise RuntimeError("Export submission wrapper") from failure
            raise

    monkeypatch.setattr(schedules, "run_now", submit)
    asyncio.run(flow.step(record["job_id"]))
    assert len(errors) == 1 and errors[0].status_code == status
    task = jobs.get(record["job_id"])["tasks"][0]
    assert task["requested_at"] is None
    assert task["attempts"] == 1
    with jobs.transaction() as con:
        assert jobs._read(con, "focus-pending:a") is None
    rejected_requests = len(requests)
    if status in (429, 503):
        # Exercise Azure._fetch's real exhaustion path, whose message has no status number.
        assert errors[0].retry_after == "1700"
        assert rejected_requests > 1 and sleep.await_count == rejected_requests - 1
        assert str(status) not in str(errors[0])
        assert flow.retry_delay(errors[0]) == 1700
        assert task["status"] == "waiting-for-Azure" and task["retry_at"]
        asyncio.run(flow.step(record["job_id"]))
        assert len(requests) == rejected_requests
        monkeypatch.setattr(jobs, "now", lambda: task["retry_at"])
    else:
        assert rejected_requests == 1 and task["status"] == "failed"
        assert flow.retry_delay(errors[0]) is None

    reject = False
    asyncio.run(flow.resume(record["job_id"]))
    asyncio.run(flow.step(record["job_id"]))
    assert len(requests) == rejected_requests + 1
    saved = jobs.get(record["job_id"])["tasks"][0]
    assert saved["requested_at"] and saved["status"] == "waiting-for-Azure"
    assert saved["attempts"] == 2
    asyncio.run(flow.step(record["job_id"]))
    assert len(requests) == rejected_requests + 1


@pytest.mark.parametrize("status", [None, 408, 500, 502, 504])
def test_uncertain_submission_preserves_intent_across_resume(submission_job, monkeypatch, status):
    import httpx

    from app import cost, schedules

    jobs, flow, record = submission_job
    error = (cost.CostError("Upstream outcome unknown", status_code=status, retry_after="60")
             if status else httpx.ReadTimeout("Response lost for request 403-429"))
    wrapper = RuntimeError("Export submission wrapper")
    wrapper.__cause__ = error
    run = AsyncMock(side_effect=wrapper)
    monkeypatch.setattr(schedules, "run_now", run)
    asyncio.run(flow.step(record["job_id"]))
    task = jobs.get(record["job_id"])["tasks"][0]
    assert task["requested_at"] and task["retry_at"]
    with jobs.transaction() as con:
        assert jobs._read(con, "focus-pending:a")["job_id"] == record["job_id"]
    monkeypatch.setattr(jobs, "now", lambda: task["retry_at"])
    asyncio.run(flow.resume(record["job_id"]))
    asyncio.run(flow.step(record["job_id"]))
    saved = jobs.get(record["job_id"])["tasks"][0]
    assert saved["requested_at"] == task["requested_at"]
    run.assert_awaited_once()


def test_structured_retry_after_http_date_and_context():
    from datetime import UTC, datetime, timedelta
    from email.utils import format_datetime

    from app.cost import CostError
    from app.focus_workflow import retry_delay

    retry = format_datetime(datetime.now(UTC) + timedelta(minutes=5), usegmt=True)
    error = RuntimeError("Wrapped export failure")
    error.__context__ = CostError("Throttled", status_code=429, retry_after=retry)
    assert 298 <= retry_delay(error) <= 300
    denied = RuntimeError("Retry after later")
    denied.__cause__ = CostError("Denied", status_code=403, retry_after="1700")
    assert retry_delay(denied) is None


def test_workload_execution_never_reuses_delegated_context(durable):
    from app import cost
    jobs, _ = durable
    caller = object()
    token = cost._caller.set(caller)
    record = jobs.create("automatic", ["a"], identity="workload")
    async def execute():
        assert cost._caller.get() is None
        assert cost._caller_id.get() == "server"
        return {"status": "ready"}
    try:
        asyncio.run(jobs.run(record["job_id"], execute))
        assert cost._caller.get() is caller
    finally:
        cost._caller.reset(token)


@pytest.mark.parametrize("start,end,verified", [
    ("2026-07-01", "2026-07-07", False), ("2026-07-17", "2026-07-23", True)])
def test_custom_proof_cannot_be_native_mtd_coincidence(durable, monkeypatch, start, end, verified):
    from app import exports, focus_sync, warehouse
    from app import focus_workflow as flow
    jobs, _ = durable
    schedule = {"subscription_id": "a", "name": "owned", "account": "store",
                "container": "focus", "folder": "a"}
    run = SimpleNamespace(run_id="new", submitted="2026-08-01T01:00:00+00:00", timeframe="MonthToDate")
    monkeypatch.setattr(focus_sync, "owned", AsyncMock(return_value=[schedule]))
    monkeypatch.setattr(focus_sync, "record", AsyncMock())
    monkeypatch.setattr(flow, "manifests", AsyncMock(return_value=[run]))
    monkeypatch.setattr(flow, "actual_window", lambda manifest: (start, end))
    monkeypatch.setattr(exports, "ingest_export", AsyncMock(return_value={"rows": 2}))
    saved = {}
    monkeypatch.setattr(warehouse.warehouse, "set_meta", lambda key, value: saved.update({key: value}))
    record = jobs.create("focus-verify", ["a"], [
        {"subscription": "a", "from": start, "to": end, "schedule": schedule,
         "requested_at": "2026-08-01T00:00:00+00:00", "baseline": []}])
    asyncio.run(flow.step(record["job_id"]))
    task = jobs.get(record["job_id"])["tasks"][0]
    assert task["status"] == "ready"
    assert task["result"]["compact_coverage_verified"] is verified
    assert ("focus-compact-verified:a" in saved) is verified


@pytest.mark.parametrize("actual", [("2026-07-01", "2026-07-31"), ("2026-08-01", "2026-08-07")])
def test_compact_returned_coverage_mismatch_disables_without_import(durable, monkeypatch, actual):
    import json

    from app import exports, focus_sync, warehouse
    from app import focus_workflow as flow
    jobs, _ = durable
    schedule = {"subscription_id": "a", "name": "owned", "account": "store",
                "container": "focus", "folder": "a"}
    run = SimpleNamespace(run_id="new", submitted="2026-08-01T01:00:00+00:00", timeframe="MonthToDate")
    monkeypatch.setattr(focus_sync, "owned", AsyncMock(return_value=[schedule]))
    monkeypatch.setattr(focus_sync, "record", AsyncMock())
    monkeypatch.setattr(flow, "manifests", AsyncMock(return_value=[run]))
    monkeypatch.setattr(flow, "actual_window", lambda manifest: actual)
    importer = AsyncMock()
    monkeypatch.setattr(exports, "ingest_export", importer)
    saved = {"focus-daily:a": json.dumps({"enabled": True, "mode": "compact"})}
    monkeypatch.setattr(warehouse.warehouse, "get_meta", saved.get)
    monkeypatch.setattr(warehouse.warehouse, "set_meta", lambda key, value: saved.update({key: value}))
    record = jobs.create("focus-daily", ["a"], [
        {"subscription": "a", "from": "2026-07-17", "to": "2026-07-23", "schedule": schedule,
         "requested_at": "2026-08-01T00:00:00+00:00", "baseline": []}])
    result = asyncio.run(flow.step(record["job_id"]))
    assert result["status"] == "failed"
    assert not json.loads(saved["focus-daily:a"])["enabled"]
    importer.assert_not_awaited()


def test_delayed_older_manifest_is_not_the_requested_run():
    from app.focus_workflow import submitted_after
    run = SimpleNamespace(submitted="2026-08-01T00:00:00Z")
    assert not submitted_after(run, "2026-08-01T00:01:00Z")
    assert submitted_after(run, "2026-07-31T23:59:00Z")


def test_manifest_accepted_after_original_cutoff_before_job_creation(submission_job, monkeypatch):
    from app import exports, schedules

    jobs, flow, record = submission_job
    task = record["tasks"][0]
    requested_at = "2026-07-01T00:00:00+00:00"
    accepted_at = "2026-07-01T00:00:01+00:00"
    assert requested_at < accepted_at < record["created_at"]
    jobs.update(record["job_id"], task_id=task["task_id"],
                requested_at=requested_at, baseline=[], status="waiting-for-Azure")
    manifest = exports.Manifest.parse({
        "runInfo": {"runId": "accepted", "submittedTime": accepted_at,
                    "startDate": "2026-06-01", "endDate": "2026-06-30"},
        "exportConfig": {"exportName": "owned", "type": "FocusCost", "timeFrame": "MonthToDate"},
        "dataRowCount": 2, "byteCount": 2,
        "blobs": [{"blobName": "data.csv", "dataRowCount": 2, "byteCount": 2}],
    })
    monkeypatch.setattr(flow, "manifests", AsyncMock(return_value=[manifest]))
    importer = AsyncMock(return_value={"rows": 2})
    run = AsyncMock()
    monkeypatch.setattr(exports, "ingest_export", importer)
    monkeypatch.setattr(schedules, "run_now", run)
    assert asyncio.run(flow.step(record["job_id"]))["status"] == "ready"
    assert importer.await_args.kwargs["run_identity"] == ("accepted", accepted_at)
    assert jobs.get(record["job_id"])["tasks"][0]["requested_at"] == requested_at
    run.assert_not_awaited()


@pytest.mark.parametrize("verified,denied,blocked", [(False, False, True), (True, True, True), (True, False, False)])
def test_compact_approval_requires_proof_and_workload_storage_access(
        durable, monkeypatch, verified, denied, blocked):
    from app import focus_sync, warehouse
    from app import focus_workflow as flow
    schedule = {"subscription_id": "a", "name": "owned"}
    monkeypatch.setattr(focus_sync, "owned", AsyncMock(return_value=[schedule]))
    monkeypatch.setattr(warehouse.warehouse, "get_meta", lambda key: "verified" if verified else None)
    monkeypatch.setattr(flow, "manifests", AsyncMock(
        side_effect=PermissionError("403 workload metadata access denied") if denied else None, return_value=[]))
    plan = asyncio.run(flow.daily_preview(["a"], "compact"))
    assert plan["subscriptions"][0]["blocked"] is blocked
    assert bool(plan["subscriptions"][0]["missing_capabilities"]) is denied


def test_generic_resume_cannot_cross_scope(durable, monkeypatch):
    from fastapi import HTTPException

    from app import main
    jobs, _ = durable
    user = SimpleNamespace(admin=True)
    monkeypatch.setattr(main, "require_admin", lambda request: user)
    monkeypatch.setattr(main, "permitted", AsyncMock(return_value=["a"]))
    record = jobs.create("refresh", ["b"])
    with pytest.raises(HTTPException) as raised:
        asyncio.run(main.resume_ingest_job(record["job_id"], SimpleNamespace()))
    assert raised.value.status_code == 404
    assert jobs.get(record["job_id"])["status"] == "queued"


def test_reports_workflow_offers_preview_and_keeps_populated_view():
    from pathlib import Path
    source = (Path(__file__).resolve().parents[1] / "web" / "assets" / "dashboard-reports.js").read_text(
        encoding="utf-8")
    assert "Choose how much history to import" in source
    assert '["90","90 days — recommended"]' in source
    assert "crypto.randomUUID()" in source
    assert "Retry missing work" in source
    assert "Setup queued. You can close this browser" in source
    assert 'preview_token: plan.preview_token' in source
    assert "Newly covered days are separate from restatements" in source


def test_filter_costs_are_currency_buckets_not_a_mixed_sum(monkeypatch):
    from app import main, warehouse
    user = SimpleNamespace(admin=True)
    monkeypatch.setattr(main, "current_user", lambda request: user)
    monkeypatch.setattr(main, "permitted", AsyncMock(return_value=["a"]))
    calls = []
    budgets = []
    sources = []
    @contextmanager
    def reader(scope, currency=None, budget_ms=None):
        calls.append(currency)
        budgets.append(budget_ms)
        def rows(sql, *args):
            if "max(" in sql:
                return [{"d": "2026-09-14"}]
            if "'env'" in sql:
                # The environment tag is asked for on its own connection, under a tighter
                # bound, so that when it cannot be produced the other five still can.
                return [{"d": "env", "v": "prod", "currency": "USD", "c": 3}]
            assert "GROUP BY lower(rg), currency" in sql
            # All five in one query now: `ChargeCategory` and `PricingModel` joined the
            # rollup's grain, so nothing here has to fall back to the detail table.
            assert "GROUP BY lower(charge), currency" in sql
            sources.append("rollup" if "costs_rollup" in sql else "detail")
            return [{"d": "rg", "v": "shared", "currency": "USD", "c": 1},
                    {"d": "rg", "v": "shared", "currency": "EUR", "c": 2},
                    {"d": "charge", "v": "Usage", "currency": "USD", "c": 4}]
        yield SimpleNamespace(rows=rows, agg="costs_rollup")
    monkeypatch.setattr(warehouse.warehouse, "reader", reader)
    response = asyncio.run(main.filter_values(SimpleNamespace(), scope="a"))
    import json
    payload = json.loads(response.body)
    item = payload["dimensions"]["rg"][0]
    assert item["cost"] is None and item["currency"] is None
    assert item["currency_buckets"] == {"USD": 1, "EUR": 2}
    assert payload["dimensions"]["env"][0]["value"] == "prod"
    assert payload["dimensions"]["charge"][0]["value"] == "Usage"
    assert "unavailable_dimensions" not in payload
    assert calls == [None, None]
    # One query, from the rollup. Before charge and pricing were in the grain this took
    # two -- and the second scanned 5.8M detail rows to find seven values.
    assert sources == ["rollup"]
    # Both must bound their own database time, and the environment tag -- the one dimension
    # that is read out of the Tags document per row -- must be bounded at least as tightly.
    assert budgets == [main.FILTER_BUDGET_MS, main.FILTER_ENV_BUDGET_MS]
    assert 0 < main.FILTER_ENV_BUDGET_MS <= main.FILTER_BUDGET_MS


def test_a_slow_environment_tag_does_not_take_the_other_filters_with_it(monkeypatch):
    """The environment tag costs 161s of the 171s the combined query needed on the real
    estate, so asking for all six together made the five cheap ones unavailable too --
    the filter bar came back empty rather than merely short of one dimension."""
    from app import main, warehouse
    monkeypatch.setattr(main, "current_user", lambda request: SimpleNamespace(admin=True))
    monkeypatch.setattr(main, "permitted", AsyncMock(return_value=["a"]))

    @contextmanager
    def reader(scope, currency=None, budget_ms=None):
        def rows(sql, *args):
            if "max(" in sql:
                return [{"d": "2026-09-14"}]
            if "'env'" in sql:
                raise RuntimeError("canceling statement due to statement timeout")
            return [{"d": "region", "v": "uksouth", "currency": "USD", "c": 7}]
        yield SimpleNamespace(rows=rows, agg="costs_rollup")

    monkeypatch.setattr(warehouse.warehouse, "reader", reader)
    response = asyncio.run(main.filter_values(SimpleNamespace(), scope="a"))
    import json
    payload = json.loads(response.body)
    assert payload["dimensions"]["region"][0]["value"] == "uksouth"
    assert payload["dimensions"]["env"] == []
    # Said out loud, because an empty list is otherwise indistinguishable from an estate
    # that tags nothing at all.
    assert payload["unavailable_dimensions"] == ["env"]
    assert "unavailable" not in payload


def test_scoped_filter_bar_dates_come_from_the_rollup_not_the_detail_view(monkeypatch):
    """Under a scope `costs` is a view over the detail table. On the 16.9M-row estate
    `max("ChargePeriodStart")` over it hit FILTER_BUDGET_MS for any selection -- even a
    single subscription -- and, being outside the per-dimension guard, emptied the bar."""
    from app import main, warehouse
    monkeypatch.setattr(main, "current_user", lambda request: SimpleNamespace(admin=True))
    monkeypatch.setattr(main, "permitted", AsyncMock(return_value=["a", "b"]))
    dated = []

    @contextmanager
    def reader(scope, currency=None, budget_ms=None):
        assert scope == ["a"]

        def rows(sql, *args):
            if "max(" in sql:
                dated.append(sql)
                if "FROM costs_rollup" not in sql:
                    raise RuntimeError("canceling statement due to statement timeout")
                return [{"d": "2026-09-28"}]
            if "'env'" in sql:
                return []
            return [{"d": "service", "v": "Storage", "currency": "INR", "c": 5}]
        yield SimpleNamespace(rows=rows, agg="costs_rollup")

    monkeypatch.setattr(warehouse.warehouse, "reader", reader)
    response = asyncio.run(main.filter_values(SimpleNamespace(), scope="a"))
    import json
    payload = json.loads(response.body)
    assert "unavailable" not in payload
    assert payload["dimensions"]["service"][0]["value"] == "Storage"
    assert dated and all("FROM costs_rollup" in sql for sql in dated)


def test_filter_bar_can_defer_environment_dimension(monkeypatch):
    from app import main, warehouse

    monkeypatch.setattr(main.environment, "ENABLED", False)
    monkeypatch.setattr(main, "current_user", lambda request: SimpleNamespace(admin=True))
    monkeypatch.setattr(main, "permitted", AsyncMock(return_value=["a"]))
    queries = []
    budgets = []

    @contextmanager
    def reader(scope, currency=None, budget_ms=None):
        budgets.append(budget_ms)

        def rows(sql, *args):
            queries.append(sql)
            if "max(" in sql:
                return [{"d": "2026-09-28"}]
            if "AS env" in sql:
                return [{"d": "env", "v": "prod", "currency": "USD", "c": 5}]
            return [{"d": "service", "v": "Storage", "currency": "USD", "c": 5}]

        yield SimpleNamespace(rows=rows, agg="costs_rollup")

    monkeypatch.setattr(warehouse.warehouse, "reader", reader)
    fast = asyncio.run(main.filter_values(SimpleNamespace(), scope="a", include_env=False))
    import json
    fast_payload = json.loads(fast.body)
    assert fast_payload["dimensions"]["service"][0]["value"] == "Storage"
    assert fast_payload["deferred_dimensions"] == ["env"]
    assert not any("AS env" in query for query in queries)

    queries.clear()
    env = asyncio.run(main.filter_values(SimpleNamespace(), scope="a", env_only=True))
    env_payload = json.loads(env.body)
    assert env_payload["dimensions"]["env"][0]["value"] == "prod"
    assert sum("AS env" in query for query in queries) == 1
    assert not any("GROUP BY lower(service)" in query for query in queries)
    env_query = next(query for query in queries if "AS env" in query)
    assert env_query.count('TRY_CAST("Tags" AS JSON)') == 1
    assert "OFFSET 0" in env_query
    assert budgets[-1] == main.FILTER_ENV_BUDGET_MS


def test_csv_disconnect_waits_for_fetch_before_closing_context(monkeypatch):
    from app import main, warehouse
    fetching, release, closed = threading.Event(), threading.Event(), threading.Event()
    monkeypatch.setattr(main, "current_user", lambda request: SimpleNamespace(admin=True))
    monkeypatch.setattr(main, "permitted", AsyncMock(return_value=["a"]))
    monkeypatch.setattr(main, "request_filters", lambda request: {})
    @contextmanager
    def reader(*args, **kwargs):
        yield SimpleNamespace(rows=lambda sql, limit=None: [{"hi": "2026-07-17"}])
    class Connection:
        def execute(self, sql):
            if sql.startswith("FETCH"):
                fetching.set()
                assert release.wait(2)
                return SimpleNamespace(fetchall=lambda: [tuple(None for _ in warehouse.COLUMNS)])
            if sql == "ROLLBACK":
                closed.set()
    @contextmanager
    def connect(**kwargs):
        yield Connection()
    monkeypatch.setattr(warehouse.warehouse, "reader", reader)
    monkeypatch.setattr(warehouse.warehouse, "connect", connect)
    async def disconnect():
        response = await main.download_raw_costs(SimpleNamespace(), scope="a")
        await anext(response.body_iterator)
        fetch = asyncio.create_task(anext(response.body_iterator))
        while not fetching.is_set():
            await asyncio.sleep(0.001)
        fetch.cancel()
        asyncio.get_running_loop().call_later(0.05, release.set)
        with pytest.raises(asyncio.CancelledError):
            await fetch
        assert closed.is_set()
    try:
        asyncio.run(disconnect())
    finally:
        release.set()


def test_generic_tasks_resume_only_unsuccessful_scoped_dates(durable, monkeypatch):
    from app import ingest_jobs
    from app import warehouse as module
    jobs, _ = durable
    wh = module.warehouse
    calls = []
    row = [None] * len(module.COLUMNS)
    row[module.COLUMNS_INDEX["ServiceFamily"]] = "Compute"
    async def query(client, sub, start, end, headers, metric, **kwargs):
        calls.append((sub["id"], start, end))
        if sub["id"] == "b" and len(calls) == 2:
            raise RuntimeError("temporary test failure")
        return [row]
    monkeypatch.setattr(module, "_token", lambda path: "offline-not-a-token")
    monkeypatch.setattr(wh, "_query_period", query)
    monkeypatch.setattr(wh, "_replace", AsyncMock(return_value=1))
    monkeypatch.setattr(wh, "_log", lambda *a: None)
    monkeypatch.setattr(wh, "refresh_rollup_async", AsyncMock())
    record = jobs.create("refresh", ["a", "b"])
    def factory():
        return ingest_jobs.ingest([{"id": "a"}, {"id": "b"}], 3, "ActualCost", quick=True)
    asyncio.run(jobs.run(record["job_id"], factory))
    saved = jobs.get(record["job_id"])
    assert saved["status"] == "partial"
    assert [t["status"] for t in saved["tasks"]] == ["ready", "failed"]
    jobs.update(record["job_id"], status="queued")
    monkeypatch.setattr(module, "_periods", lambda months: pytest.fail("A resumed window must not move"))
    asyncio.run(jobs.run(record["job_id"], factory))
    assert [c[0] for c in calls] == ["a", "b", "b"]
    assert calls[1][1:] == calls[2][1:]
    assert jobs.get(record["job_id"])["status"] == "ready"


def test_uninitialized_workload_request_recovers_without_interactive_jobs(durable, monkeypatch):
    from app import ingest_jobs, main
    jobs, _ = durable
    record = jobs.create("automatic", ["a"], identity="workload",
                         payload={"months": 3, "metric": "ActualCost", "quick": True})
    jobs.create("refresh", ["a"], payload={"months": 3})
    execute = AsyncMock(return_value={"status": "ready"})
    monkeypatch.setattr(main, "_run_ingest", execute)
    async def recover():
        await ingest_jobs.recover()
        await asyncio.gather(*jobs._workers)
    asyncio.run(recover())
    execute.assert_awaited_once_with(3, "ActualCost", quick=True, scope=["a"])
    assert jobs.get(record["job_id"])["status"] == "ready"


def test_generic_window_anchors_to_acceptance_not_restart():
    from app.ingest_jobs import frozen_periods
    assert [(lo, hi) for lo, hi, _ in frozen_periods(3, "2026-09-14T23:00:00+00:00")] == [
        ("2026-07-01", "2026-07-31"), ("2026-08-01", "2026-08-31"), ("2026-09-01", "2026-09-14")]
    assert frozen_periods(2, "2024-03-01T00:00:00+00:00")[0][1] == "2024-02-29"


def test_authorized_manual_request_can_resume_before_task_initialization(durable, monkeypatch):
    from app import main
    jobs, _ = durable
    user = SimpleNamespace(admin=True)
    monkeypatch.setattr(main, "require_admin", lambda request: user)
    monkeypatch.setattr(main, "permitted", AsyncMock(return_value=["a"]))
    @asynccontextmanager
    async def as_user(person):
        yield
    monkeypatch.setattr(main, "as_user", as_user)
    execute = AsyncMock(return_value={"status": "ready"})
    monkeypatch.setattr(main, "_run_ingest", execute)
    record = jobs.create("refresh", ["a"], payload={"months": 2, "metric": "ActualCost", "quick": True})
    async def resume():
        response = await main.resume_ingest_job(record["job_id"], SimpleNamespace())
        await asyncio.gather(*jobs._workers)
        return response
    assert asyncio.run(resume()).status_code == 202
    execute.assert_awaited_once_with(2, "ActualCost", quick=True, scope=["a"])
    assert jobs.get(record["job_id"])["status"] == "ready"


def test_accepted_focus_retry_keeps_identity_after_preview_changes(durable, monkeypatch):
    import json

    from fastapi import HTTPException

    from app import focus_workflow, main
    jobs, _ = durable
    user = SimpleNamespace(admin=True)
    monkeypatch.setattr(main, "require_schedule_writer", lambda request: user)
    monkeypatch.setattr(main, "assert_may_write_schedule", AsyncMock())
    monkeypatch.setattr(main, "_focus_plan", AsyncMock(return_value={
        "subscriptions": [{"subscription": "a"}], "preview_token": "changed-after-Azure-acceptance"}))
    @asynccontextmanager
    async def as_user(person):
        yield
    monkeypatch.setattr(main, "as_user", as_user)
    launched = []
    monkeypatch.setattr(focus_workflow, "launch", launched.append)
    record = jobs.create("focus-bootstrap", ["a"], idempotency_key="accepted",
                         payload={"from": "2026-06-17", "to": "2026-09-14"})
    body = main.FocusJobRequest(scope=["a"], preview_token="original-preview", idempotency_key="accepted")
    response = asyncio.run(main.start_focus_job(body, SimpleNamespace()))
    assert json.loads(response.body)["job_id"] == record["job_id"]
    assert launched[0]["job_id"] == record["job_id"]
    body.idempotency_key = "not-accepted"
    with pytest.raises(HTTPException) as error:
        asyncio.run(main.start_focus_job(body, SimpleNamespace()))
    assert error.value.status_code == 409


@pytest.fixture
def financial_route_context(monkeypatch):
    from contextvars import ContextVar

    from app import main
    active = ContextVar("financial_route_user", default=False)
    user = SimpleNamespace(admin=True)
    monkeypatch.setattr(main, "current_user", lambda request: user)
    monkeypatch.setattr(main, "permitted", AsyncMock(return_value=["a"]))
    @asynccontextmanager
    async def as_user(person):
        token = active.set(True)
        try:
            yield
        finally:
            active.reset(token)
    monkeypatch.setattr(main, "as_user", as_user)
    return main, active


@pytest.mark.parametrize("days", [1, 3, 14])
def test_anomaly_route_preserves_selected_window_filters_and_user(financial_route_context, monkeypatch, days):
    from app import anomalies
    main, active = financial_route_context
    monkeypatch.setattr(main, "request_filters", lambda request: {"rg": ["prod"]})
    def analyse(scope, **kwargs):
        assert active.get()
        assert scope == ["a"]
        assert kwargs == {"days": days, "display_currency": "EUR", "filters": {"rg": ["prod"]}}
        return {"days": days}
    monkeypatch.setattr(anomalies, "analyse", analyse)
    response = asyncio.run(main.dashboard_anomalies(SimpleNamespace(), days=days, scope="a", currency="EUR"))
    assert response.status_code == 200


def test_shutdown_route_does_not_convert_domain_money_twice(financial_route_context, monkeypatch):
    import json

    from app import currency, shutdown
    main, active = financial_route_context
    async def find(scope, **kwargs):
        assert active.get()
        assert scope == ["a"] and kwargs == {"days": 7, "currency": "EUR"}
        return {"currency": "EUR", "monthly_saving": 10}
    monkeypatch.setattr(shutdown, "find_schedulable", find)
    monkeypatch.setattr(currency, "convert_money", lambda *args: pytest.fail("Already converted in domain"))
    response = asyncio.run(main.dashboard_shutdown(SimpleNamespace(), days=7, scope="a", currency="EUR"))
    assert json.loads(response.body)["monthly_saving"] == 10


@pytest.mark.parametrize("on_demand,annual", [(12, 1460), (0, 0)])
def test_commitment_baseline_never_substitutes_display_for_unknown_source(
        financial_route_context, monkeypatch, on_demand, annual):
    import json
    from unittest.mock import Mock

    from app import commitments, rates
    main, active = financial_route_context
    spot = Mock(return_value={})
    model = SimpleNamespace(
        coverage=lambda *args, **kwargs: {"empty": False, "currency": None, "split": {"on demand": on_demand}},
        spot_savings=spot, inventory=lambda *args, **kwargs: {})
    monkeypatch.setattr(commitments, "get_commitments", lambda: model)
    async def comparison(scope, **kwargs):
        assert active.get()
        assert scope == ["a"]
        assert kwargs == {"baseline_annual": annual, "baseline_days": 3,
                          "baseline_currency": None, "display_currency": "EUR",
                          "baseline_by_currency": {}, "baseline_unavailable": {}}
        return {"options": []}
    compare = AsyncMock(side_effect=comparison)
    monkeypatch.setattr(rates, "commitment_comparison", compare)
    response = asyncio.run(main.dashboard_commitments(SimpleNamespace(), days=3, scope="a", currency="EUR"))
    assert response.status_code == 200
    assert json.loads(response.body)["comparison"] == {"options": []}
    compare.assert_awaited_once()
    spot.assert_called_once_with(["a"], days=3, currency="EUR")


def test_mixed_currency_commitment_baseline_is_offered_per_currency_with_reasons(
        financial_route_context, monkeypatch):
    """Live Scale: INR + USD estate, flat split `{}`, so every annual cost read "—" and
    the page never said why. Each native currency now offers its own baseline or reason."""
    import json
    from unittest.mock import Mock

    from app import commitments, rates
    main, _ = financial_route_context
    model = SimpleNamespace(
        coverage=lambda *args, **kwargs: {
            "empty": False, "currency": None, "split": {}, "mixed_currency": True,
            "by_currency": {"INR": {"split": {"on demand": 300.0, "committed": 9.0}},
                            "USD": {"split": {"unknown": 2471.11}},
                            "EUR": {"split": {"committed": 5.0}}}},
        spot_savings=Mock(return_value={}), inventory=lambda *args, **kwargs: {})
    monkeypatch.setattr(commitments, "get_commitments", lambda: model)
    seen = {}

    async def comparison(scope, **kwargs):
        seen.update(kwargs)
        return {"options": []}
    monkeypatch.setattr(rates, "commitment_comparison", AsyncMock(side_effect=comparison))
    response = asyncio.run(main.dashboard_commitments(SimpleNamespace(), days=30, scope="a"))
    assert response.status_code == 200
    assert seen["baseline_annual"] is None
    assert seen["baseline_by_currency"] == {"INR": 300.0 * 365 / 30}
    assert "pricing model is not recorded" in seen["baseline_unavailable"]["USD"]
    assert "No on-demand spend" in seen["baseline_unavailable"]["EUR"]
    assert json.loads(response.body)["comparison"] == {"options": []}


def test_commitment_inventory_timeout_is_a_named_gap_not_a_500(financial_route_context, monkeypatch):
    """Live Scale, currency=USD: `inventory()` hit `statement timeout` (QueryCanceled) and the
    exception escaped the route, so the whole Commitments tab returned HTTP 500 after ~100s."""
    import json

    from app import commitments, rates
    main, _ = financial_route_context

    class QueryCanceled(Exception):
        pass

    def inventory(*args, **kwargs):
        raise QueryCanceled("canceling statement due to statement timeout")

    def spot(*args, **kwargs):
        raise RuntimeError("connection reset")

    model = SimpleNamespace(
        coverage=lambda *args, **kwargs: {"empty": False, "currency": "USD",
                                          "split": {"on demand": 30.0}},
        spot_savings=spot, inventory=inventory)
    monkeypatch.setattr(commitments, "get_commitments", lambda: model)
    monkeypatch.setattr(rates, "commitment_comparison", AsyncMock(return_value={"options": []}))
    response = asyncio.run(main.dashboard_commitments(SimpleNamespace(), days=30, scope="a",
                                                      currency="USD"))
    assert response.status_code == 200
    body = json.loads(response.body)
    assert body["coverage"]["split"] == {"on demand": 30.0}
    assert body["inventory"]["unavailable"] is True
    assert "took too long" in body["inventory"]["error"]
    assert "As billed" in body["inventory"]["error"]
    assert body["spot"]["unavailable"] is True
    assert "could not be read" in body["spot"]["error"]
