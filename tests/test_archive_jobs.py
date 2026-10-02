"""Archive queue, ownership, progress, failure and authorized restart contracts."""
import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException

from tests import test_job_orchestration as orchestration

pytestmark = pytest.mark.offline
durable = orchestration.durable
offline_modules = orchestration.offline_modules


@pytest.fixture
def configured(monkeypatch):
    from app import archive
    monkeypatch.setattr(archive, "ACCOUNT", "offline")
    monkeypatch.setattr(archive, "CONTAINER", "archive")
    return archive


def test_returns_202_before_background_completion(durable, configured, monkeypatch):
    from app import archive_jobs, main
    jobs, _ = durable
    user = SimpleNamespace(name="admin", email="", source="file", admin=True)
    monkeypatch.setattr(main, "require_admin", lambda request: user)
    monkeypatch.setattr(main, "permitted", AsyncMock(return_value=None))
    entered, finish = None, None

    async def write(*args, **kwargs):
        entered.set()
        await finish.wait()
        return {"name": "focus/today.parquet", "rows": 9, "bytes": 123}

    monkeypatch.setattr(configured, "archive_current", write)
    async def scenario():
        nonlocal entered, finish
        entered, finish = asyncio.Event(), asyncio.Event()
        response = await asyncio.wait_for(main.archive_now(None), 1)
        data = json.loads(response.body)
        assert response.status_code == 202 and data["job_id"]
        assert response.headers["location"] == "/api/jobs/" + data["job_id"]
        assert "result" not in data
        await asyncio.wait_for(entered.wait(), 1)
        assert jobs.get(data["job_id"])["status"] == "running"
        assert archive_jobs.launch(data) is None
        workers = list(jobs._workers)
        finish.set()
        await asyncio.gather(*workers)
        saved = jobs.get(data["job_id"])
        assert saved["status"] == "ready" and saved["result"]["rows"] == 9
        assert saved["progress"]["uploaded_bytes"] == 123 and saved["finished"]
    asyncio.run(scenario())


def test_failed_publication_is_persisted_not_claimed_written(durable, configured, monkeypatch):
    from app import archive_jobs
    jobs, _ = durable
    record = archive_jobs.create("FocusCost", requested_by="file:admin")
    monkeypatch.setattr(configured, "archive_current", AsyncMock(side_effect=RuntimeError("storage unavailable")))
    with pytest.raises(RuntimeError, match="storage unavailable"):
        asyncio.run(jobs.run(record["job_id"], lambda: archive_jobs.run(record)))
    saved = jobs.get(record["job_id"])
    assert saved["status"] == "failed" and saved["detail"] == "storage unavailable"
    assert saved["finished"] and "result" not in saved


def test_progress_persisted_while_worker_is_running(durable, configured, monkeypatch):
    from app import archive_jobs
    jobs, _ = durable
    monkeypatch.setattr(jobs, "HEARTBEAT_SECONDS", .01)
    record = archive_jobs.create("FocusCost")
    async def write(*args, **kwargs):
        configured._job_progress(phase="extracting", rows=50000)
        for _ in range(100):
            await asyncio.sleep(.01)
            if jobs.get(record["job_id"])["progress"].get("rows") == 50000:
                break
        assert jobs.get(record["job_id"])["progress"]["rows"] == 50000
        return {"rows": 50000, "bytes": 2}
    monkeypatch.setattr(configured, "archive_current", write)
    asyncio.run(jobs.run(record["job_id"], lambda: archive_jobs.run(record)))


def test_deduplicates_active_target_and_preserves_selection_naming(durable, configured):
    from app import archive_jobs
    first = archive_jobs.create("FocusCost", requested_by="a")
    assert archive_jobs.create("FocusCost", requested_by="a")["job_id"] == first["job_id"]
    with pytest.raises(ValueError, match="already queued"):
        archive_jobs.create("FocusCost", requested_by="b")
    selected = archive_jobs.create("FocusCost", scope=["B", "a"], requested_by="a")
    assert selected["scope"] == ["a", "b"]
    assert "-selected-" in selected["payload"]["name"]
    assert selected["payload"]["name"] != first["payload"]["name"]


def test_idempotency_preserves_terminal_outcome(durable, configured):
    from app import archive_jobs
    jobs, _ = durable
    first = archive_jobs.create("FocusCost", requested_by="a", idempotency_key="retry")
    jobs.update(first["job_id"], status="ready", result={"rows": 3})
    second = archive_jobs.create("FocusCost", requested_by="a", idempotency_key="retry")
    assert second["job_id"] == first["job_id"] and archive_jobs.launch(second) is None


def test_owner_and_scoped_status_authorization(durable, configured, monkeypatch):
    from app import archive_jobs, main
    user = SimpleNamespace(source="entra", name="admin", email="admin@example.invalid", admin=True)
    whole = archive_jobs.create("FocusCost", requested_by=archive_jobs.owner(user))
    monkeypatch.setattr(main, "permitted", AsyncMock(return_value=["a"]))
    assert asyncio.run(main._authorized_job(whole["job_id"], user))["job_id"] == whole["job_id"]
    other = SimpleNamespace(source="entra", name="other", email="other@example.invalid", admin=True)
    with pytest.raises(HTTPException) as error:
        asyncio.run(main._authorized_job(whole["job_id"], other))
    assert error.value.status_code == 404
    scoped = archive_jobs.create("FocusCost", scope=["b"], requested_by=archive_jobs.owner(user))
    with pytest.raises(HTTPException) as error:
        asyncio.run(main._authorized_job(scoped["job_id"], user))
    assert error.value.status_code == 404


def test_automatic_archives_recover_but_interactive_require_authorized_resume(durable, configured, monkeypatch):
    from app import archive_jobs
    jobs, _ = durable
    auto = archive_jobs.create("ActualCost", identity="workload")
    manual = archive_jobs.create("FocusCost", requested_by="file:admin")
    jobs.update(auto["job_id"], status="running")
    jobs.update(manual["job_id"], status="running")
    launched = []
    monkeypatch.setattr(archive_jobs, "launch", lambda record: launched.append(record["job_id"]))
    asyncio.run(archive_jobs.recover())
    assert launched == [auto["job_id"]]
    resumed = asyncio.run(archive_jobs.resume(jobs.get(manual["job_id"])))
    assert resumed["job_id"] == manual["job_id"] and resumed["status"] == "running"
    assert launched[-1] == manual["job_id"]


def test_restart_does_not_fabricate_an_old_snapshot(durable, configured, monkeypatch):
    from app import archive_jobs
    jobs, _ = durable
    record = archive_jobs.create("FocusCost")
    record["payload"]["day"] = "2000-01-01"
    write = AsyncMock()
    monkeypatch.setattr(configured, "archive_current", write)
    with pytest.raises(configured.ArchiveError, match="day has passed"):
        asyncio.run(jobs.run(record["job_id"], lambda: archive_jobs.run(record)))
    write.assert_not_awaited()
    assert jobs.get(record["job_id"])["status"] == "failed"


def test_stop_persists_interrupted_then_resume_completes(durable, configured, monkeypatch):
    from app import archive_jobs
    jobs, _ = durable
    record = archive_jobs.create("FocusCost")
    async def scenario():
        entered = asyncio.Event()
        async def write(*args, **kwargs):
            entered.set()
            await asyncio.Event().wait()
        monkeypatch.setattr(configured, "archive_current", write)
        archive_jobs.launch(record)
        await asyncio.wait_for(entered.wait(), 1)
        await jobs.stop()
        saved = jobs.get(record["job_id"])
        assert saved["status"] == "queued" and "Interrupted" in saved["detail"]
        monkeypatch.setattr(configured, "archive_current", AsyncMock(return_value={"rows": 5, "bytes": 9}))
        await archive_jobs.resume(saved)
        await asyncio.gather(*list(jobs._workers))
        assert jobs.get(record["job_id"])["status"] == "ready"
    asyncio.run(scenario())


def test_requester_cancel_is_terminal_and_publication_cannot_be_cancelled(durable, configured, monkeypatch):
    from app import archive_jobs
    jobs, _ = durable
    record = archive_jobs.create("FocusCost")
    async def scenario():
        entered = asyncio.Event()
        async def write(*args, **kwargs):
            entered.set()
            await asyncio.Event().wait()
        monkeypatch.setattr(configured, "archive_current", write)
        archive_jobs.launch(record)
        await asyncio.wait_for(entered.wait(), 1)
        tasks = list(jobs._workers)
        await archive_jobs.cancel(jobs.get(record["job_id"]))
        await asyncio.gather(*tasks, return_exceptions=True)
        assert jobs.get(record["job_id"])["status"] == "cancelled"
    asyncio.run(scenario())
    second = archive_jobs.create("FocusCost")
    jobs.update(second["job_id"], status="running", progress={"phase": "publishing"})
    with pytest.raises(ValueError, match="publication has started"):
        asyncio.run(archive_jobs.cancel(second))


def test_active_deduplication_records_each_idempotency_key(durable, configured):
    from app import archive_jobs
    jobs, _ = durable
    first = archive_jobs.create("FocusCost", requested_by="a", idempotency_key="first")
    assert archive_jobs.create("FocusCost", requested_by="a", idempotency_key="second")["job_id"] == first["job_id"]
    jobs.update(first["job_id"], status="ready")
    assert archive_jobs.create("FocusCost", requested_by="a", idempotency_key="second")["job_id"] == first["job_id"]


def test_resume_cannot_reset_other_workers_publication_or_cancel(durable, configured, monkeypatch):
    from app import archive_jobs
    jobs, _ = durable
    record = archive_jobs.create("FocusCost")
    jobs.claim(record["job_id"], "another-process")
    jobs.update(record["job_id"], status="running", progress={"phase": "publishing"})
    monkeypatch.setattr(archive_jobs, "launch", lambda record: None)
    saved = asyncio.run(archive_jobs.resume(record))
    assert saved["status"] == "running" and saved["progress"]["phase"] == "publishing"
    assert not archive_jobs.describe(saved)["can_resume"]
    jobs.release("another-process")
    assert archive_jobs.describe(saved)["can_resume"]
    jobs.update(record["job_id"], progress={"phase": "extracting"}, cancel_requested=True)
    saved = asyncio.run(archive_jobs.resume(record))
    assert saved["cancel_requested"]


def test_resume_failed_job_cannot_compete_with_new_destination_job(durable, configured):
    from app import archive_jobs
    jobs, _ = durable
    first = archive_jobs.create("FocusCost")
    jobs.update(first["job_id"], status="failed")
    archive_jobs.create("FocusCost")
    with pytest.raises(ValueError, match="already queued"):
        asyncio.run(archive_jobs.resume(first))
    assert jobs.get(first["job_id"])["status"] == "failed"


def test_jobs_only_status_never_contacts_blob_storage(durable, configured, monkeypatch):
    from app import archive_jobs, main
    user = SimpleNamespace(source="file", name="admin", email="", admin=True)
    record = archive_jobs.create("FocusCost", requested_by=archive_jobs.owner(user))
    monkeypatch.setattr(main, "current_user", lambda request: user)
    storage = AsyncMock(side_effect=AssertionError("Polling must not access Azure"))
    monkeypatch.setattr(configured, "status", storage)
    response = asyncio.run(main.archive_status(None, jobs_only=True))
    data = json.loads(response.body)
    assert data["jobs"][0]["job_id"] == record["job_id"]
    assert data["jobs"][0]["can_resume"] and data["jobs"][0]["can_cancel"]
    storage.assert_not_awaited()


@pytest.mark.parametrize("action", ["create", "resume", "cancel"])
def test_read_only_rejects_archive_mutations(durable, configured, monkeypatch, action):
    from app import archive_jobs
    record = archive_jobs.create("FocusCost")
    monkeypatch.setenv("CLOUDLENS_READ_ONLY", "true")
    with pytest.raises(PermissionError, match="READ_ONLY"):
        if action == "create":
            archive_jobs.create("ActualCost")
        else:
            asyncio.run(getattr(archive_jobs, action)(record))


def test_cancellation_during_lease_acquisition_releases_claim(durable, configured, monkeypatch):
    import threading

    from app import archive_jobs
    jobs, _ = durable
    record = archive_jobs.create("FocusCost")
    claimed, release = threading.Event(), threading.Event()
    original = jobs.claim
    def slow_claim(job_id, owner):
        result = original(job_id, owner)
        claimed.set()
        assert release.wait(5)
        return result
    monkeypatch.setattr(jobs, "claim", slow_claim)
    async def scenario():
        task = archive_jobs.launch(record)
        while not claimed.is_set():
            await asyncio.sleep(.01)
        task.cancel()
        await asyncio.sleep(.01)
        task.cancel()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
    asyncio.run(scenario())
    assert original(record["job_id"], "replacement")
    jobs.release("replacement")


def test_publication_phase_is_not_regressed_by_stale_heartbeat(durable, configured):
    from app import archive_jobs
    jobs, _ = durable
    record = archive_jobs.create("FocusCost")
    jobs.update(record["job_id"], progress={"phase": "publishing"})
    saved = jobs.update(record["job_id"], progress={"phase": "uploading", "rows": 99})
    assert saved["progress"] == {"phase": "publishing", "rows": 99}


def test_repeated_cancellation_during_cleanup_waits_for_lease_release(durable, configured, monkeypatch):
    import threading

    from app import archive_jobs
    jobs, _ = durable
    record = archive_jobs.create("FocusCost")
    entered, finish = threading.Event(), threading.Event()
    release = jobs.release

    def delayed_release(owner):
        entered.set()
        assert finish.wait(5)
        release(owner)

    monkeypatch.setattr(jobs, "release", delayed_release)
    monkeypatch.setattr(configured, "archive_current", AsyncMock(return_value={"rows": 5, "bytes": 10}))

    async def scenario():
        task = archive_jobs.launch(record)
        try:
            while not entered.is_set():
                await asyncio.sleep(.01)
            task.cancel()
            task.cancel()
            await asyncio.sleep(.02)
            assert not task.done()
        finally:
            finish.set()
        await task

    asyncio.run(scenario())
    assert jobs.get(record["job_id"])["status"] == "ready"
    assert jobs.claim(record["job_id"], "replacement")
    release("replacement")


def test_automatic_ingest_queues_whole_estate_not_a_completion_claim(durable, configured, monkeypatch):
    from app import archive_jobs, main
    from app.warehouse import warehouse
    monkeypatch.setattr(warehouse, "_state", {"status": "ready"}, raising=False)
    monkeypatch.setattr(archive_jobs, "launch", lambda record: None)
    asyncio.run(main._archive_after_ingest("FocusCost", source="automatic fixture"))
    saved = warehouse.state["archive"]
    assert saved["status"] == "queued" and saved["job_id"]
    assert "bytes" not in saved
    jobs, _ = durable
    record = jobs.get(saved["job_id"])
    assert record["scope"] == [] and record["identity"] == "workload"


def test_publication_survives_repeated_shutdown_and_records_success(durable, configured, tmp_path, monkeypatch):
    import httpx

    from app import archive_jobs
    jobs, _ = durable
    record = archive_jobs.create("FocusCost")
    path = tmp_path / "archive.parquet"
    path.write_bytes(b"bounded-file")
    original = httpx.AsyncClient
    async def scenario():
        entered, finish = asyncio.Event(), asyncio.Event()
        async def transport(request):
            if request.url.params.get("comp") == "blocklist":
                assert jobs.get(record["job_id"])["progress"]["phase"] == "publishing"
                entered.set()
                await finish.wait()
            return httpx.Response(201, headers={"ETag": "atomic"})
        monkeypatch.setattr(configured.httpx, "AsyncClient",
                            lambda **kwargs: original(transport=httpx.MockTransport(transport), **kwargs))
        async def write(*args, **kwargs):
            writer = configured.BlobWriter("offline", "archive")
            writer._token = "fixture"
            result = await writer.put_file("daily.parquet", path, "application/vnd.apache.parquet")
            return {**result, "rows": 5}
        monkeypatch.setattr(configured, "archive_current", write)
        task = archive_jobs.launch(record)
        await asyncio.wait_for(entered.wait(), 2)
        with pytest.raises(ValueError, match="publication has started"):
            await archive_jobs.cancel(jobs.get(record["job_id"]))
        task.cancel()
        await asyncio.sleep(.01)
        task.cancel()
        finish.set()
        await task
    asyncio.run(scenario())
    saved = jobs.get(record["job_id"])
    assert saved["status"] == "ready" and saved["result"]["etag"] == "atomic"
