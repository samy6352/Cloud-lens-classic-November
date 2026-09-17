"""Real PostgreSQL leases, transactional FOCUS receipts and bounded CSV. Isolated DB only."""
import asyncio
import csv
import io
import json
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

pytestmark = pytest.mark.postgres


def test_cross_worker_lease_and_persisted_identity(postgres_warehouse):
    from app import jobs
    record = jobs.create("refresh", ["sub-a"], idempotency_key="same-request")
    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(lambda owner: jobs.claim(record["job_id"], owner),
                                    ["worker-a", "worker-b"]))
    assert sum(results) == 1
    assert jobs.get(record["job_id"])["started"] == record["job_id"]
    assert jobs.create("refresh", ["sub-a"], idempotency_key="same-request")["job_id"] == record["job_id"]
    assert not jobs.listing(["sub-b"])


def test_progress_context_cannot_restore_or_reidentify_another_job(postgres_warehouse):
    from app import jobs
    wh = postgres_warehouse
    wh.state = {"started": "manual", "status": "running"}
    progress = {"started": "background", "job_id": "background", "status": "queued"}
    token = jobs.current_progress.set(progress)
    try:
        wh.state = {"status": "ready", "started": "importer-timestamp", "rows": 10}
        wh.state = wh.state
        assert wh.state["started"] == "background"
        assert wh.state["job_id"] == "background"
        assert wh.state["rows"] == 10
    finally:
        jobs.current_progress.reset(token)
    assert wh.state == {"started": "manual", "status": "running"}


def _report():
    text = io.StringIO(newline="")
    writer = csv.writer(text)
    writer.writerow(["ChargePeriodStart", "BilledCost", "EffectiveCost", "BillingCurrency",
                     "SubAccountId", "ResourceId"])
    writer.writerows([["2026-06-17", "2", "2", "USD", "sub-a", "r"]] * 2 +
                     [["2026-06-16", "9", "9", "USD", "sub-a", "outside"]])
    data = text.getvalue().encode()
    manifest = {
        "runInfo": {"runId": "run", "startDate": "2026-06-01", "endDate": "2026-06-30",
                    "submittedTime": "2026-07-01"},
        "exportConfig": {"exportName": "owned", "type": "FocusCost", "timeFrame": "MonthToDate"},
        "dataRowCount": 3, "byteCount": len(data), "blobCount": 1,
        "blobs": [{"blobName": "data.csv", "dataRowCount": 3, "byteCount": len(data)}],
    }
    return {"manifest.json": json.dumps(manifest).encode(), "data.csv": data}


def test_manifest_receipt_and_exact_window_replay(postgres_warehouse, monkeypatch):
    from app import exports
    files, fetched = _report(), []
    class Reader:
        account, container = "offline", "focus"
        async def list(self, prefix=""):
            return [{"name": key, "modified": "2026-07-01", "etag": "v1"} for key in files]
        async def get(self, key):
            fetched.append(key)
            return files[key]
        async def close(self):
            pass
    monkeypatch.setattr(exports, "BlobReader", lambda *args: Reader())
    monkeypatch.setattr(postgres_warehouse, "refresh_rollup_async", AsyncMock())
    pending = {"sub-a": {"from": "2026-06-17", "to": "2026-06-30"},
               "sub-b": {"from": "2026-06-17", "to": "2026-06-30"}}
    postgres_warehouse.set_meta("subscription_reconciliation", json.dumps(pending))
    args = {"account": "offline", "container": "focus", "subscription_id": "sub-a", "scope": ["sub-a"],
            "required_metric": "FocusCost", "export_name": "owned",
            "run_identity": ("run", "2026-07-01"), "requested_window": ("2026-06-17", "2026-06-30")}
    result = asyncio.run(exports.ingest_export(**args))
    assert result["rows"] == 2 and result["changed_days"] == 1
    with postgres_warehouse.connect() as con:
        assert con.execute("SELECT count(*) FROM costs").fetchone()[0] == 2
        assert con.execute("SELECT count(*) FROM meta WHERE key LIKE 'focus-day:%'").fetchone()[0] == 14
        publication = con.execute("SELECT value FROM meta WHERE key LIKE 'data-publication:%'").fetchone()[0]
        assert json.loads(publication)["status"] == "ready"
    remaining = json.loads(postgres_warehouse.get_meta("subscription_reconciliation"))
    assert remaining == {"sub-b": pending["sub-b"]}
    fetched.clear()
    replay = asyncio.run(exports.ingest_export(**args))
    assert replay["data_bytes"] == 0 and replay["skipped_unchanged_run"]
    assert replay["source_bytes"] == replay["manifest_bytes"]
    assert "data.csv" not in fetched
    # A partial/corrupt replacement must not remove the two legitimate identical rows.
    manifest = json.loads(files["manifest.json"])
    manifest["runInfo"]["runId"] = "bad"
    manifest["blobs"][0]["byteCount"] += 1
    files["manifest.json"] = json.dumps(manifest).encode()
    with pytest.raises(exports.ExportError, match="bytes differ"):
        asyncio.run(exports.ingest_export(**{**args, "run_identity": ("bad", "2026-07-01")}))
    assert postgres_warehouse.row_count() == 2


def test_older_wide_run_preserves_newer_compact_correction(postgres_warehouse, monkeypatch):
    from app import exports
    original = _report()
    files = dict(original)
    document = json.loads(files["manifest.json"])
    document["runInfo"].update(runId="newer", submittedTime="2026-07-02")
    files["manifest.json"] = json.dumps(document).encode()
    files["data.csv"] = files["data.csv"].replace(b",2,2,USD", b",5,5,USD")
    class Reader:
        account, container = "offline", "focus"
        async def list(self, prefix=""):
            return [{"name": key, "modified": "2026-07-02", "etag": "v1"} for key in files]
        async def get(self, key):
            return files[key]
        async def close(self):
            pass
    monkeypatch.setattr(exports, "BlobReader", lambda *args: Reader())
    monkeypatch.setattr(postgres_warehouse, "refresh_rollup_async", AsyncMock())
    pending = {"sub-a": {"from": "2026-06-17", "to": "2026-06-30"}}
    postgres_warehouse.set_meta("subscription_reconciliation", json.dumps(pending))
    args = {"account": "offline", "container": "focus", "subscription_id": "sub-a", "scope": ["sub-a"],
            "required_metric": "FocusCost", "export_name": "owned"}
    asyncio.run(exports.ingest_export(
        **args, run_identity=("newer", "2026-07-02"), requested_window=("2026-06-17", "2026-06-17")))
    assert json.loads(postgres_warehouse.get_meta("subscription_reconciliation")) == pending
    files.update(original)
    result = asyncio.run(exports.ingest_export(
        **args, run_identity=("run", "2026-07-01"), requested_window=("2026-06-17", "2026-06-30")))
    assert result["preserved_newer_days"] == 1 and result["changed_days"] == 0
    with postgres_warehouse.connect(read_only=True) as con:
        assert con.execute('SELECT sum("BilledCost") FROM costs').fetchone()[0] == 10
    receipt = json.loads(postgres_warehouse.get_meta("focus-day:sub-a:2026-06-17"))
    assert receipt["run_id"] == "newer"
    assert json.loads(postgres_warehouse.get_meta("subscription_reconciliation")) == {}


@pytest.mark.parametrize("fail_first", [False, True])
def test_sas_focus_change_invalidates_only_changed_day_receipts(
        postgres_warehouse, monkeypatch, fail_first):
    from app import exports, jobs
    from app.warehouse import COLUMNS_INDEX

    wh = postgres_warehouse
    original = _report()
    files, fetched = dict(original), []

    class Reader:
        account, container = "offline", "focus"
        def __init__(self, *args):
            pass
        @classmethod
        def from_sas_url(cls, url):
            return cls()
        async def list(self, prefix=""):
            return [{"name": key, "modified": "2026-07-01", "etag": "v1"} for key in files]
        async def get(self, key):
            fetched.append(key)
            return files[key]
        async def close(self):
            pass

    monkeypatch.setattr(exports, "BlobReader", Reader)
    args = {"account": "offline", "container": "focus", "subscription_id": "sub-a", "scope": ["sub-a"],
            "required_metric": "FocusCost", "export_name": "owned",
            "run_identity": ("run", "2026-07-01"), "requested_window": ("2026-06-17", "2026-06-30")}
    asyncio.run(exports.ingest_export(**args))
    changed_key = "focus-day:sub-a:2026-06-17"
    zero_key = "focus-day:sub-a:2026-06-18"
    other_key = "focus-day:sub-b:2026-06-17"
    outside_key = "focus-day:sub-a:2026-07-01"
    other = _cost_row()
    other[COLUMNS_INDEX["SubAccountId"]] = "sub-b"
    with jobs.transaction() as con:
        exports.insert_rows(con, [other, _cost_row("2026-07-01")])
        jobs._put(con, other_key, {"run_id": "other-sub"})
        jobs._put(con, outside_key, {"run_id": "outside-window"})
    before = {key: wh.get_meta(key) for key in (changed_key, zero_key, other_key, outside_key)}

    text = io.StringIO(newline="")
    writer = csv.writer(text)
    writer.writerow(next(csv.reader(io.StringIO(original["data.csv"].decode()))))
    writer.writerows([["2026-06-17", "5", "5", "USD", "sub-a", "r"]] * 2 +
                     [["2026-06-17", "99", "99", "USD", "sub-b", "excluded"]])
    files["data.csv"] = text.getvalue().encode()
    manifest = json.loads(files["manifest.json"])
    manifest["runInfo"].update(runId="sas", startDate="2026-06-17", endDate="2026-06-17")
    manifest["exportConfig"]["exportName"] = "sas-export"
    manifest["byteCount"] = manifest["blobs"][0]["byteCount"] = len(files["data.csv"])
    files["manifest.json"] = json.dumps(manifest).encode()
    sas_args = {"sas_url": "https://offline.blob.core.windows.net/focus?sig=offline-test",
                "scope": ["sub-a"], "required_metric": "FocusCost"}

    def totals():
        with wh.connect(read_only=True) as con:
            return con.execute(
                'SELECT "SubAccountId", "ChargePeriodStart", sum("BilledCost") FROM costs '
                'GROUP BY 1,2 ORDER BY 1,2').fetchall()

    original_totals = totals()
    if fail_first:
        with monkeypatch.context() as patch:
            def fail_insert(*args):
                raise RuntimeError("injected SAS write failure")
            patch.setattr(exports, "insert_rows", fail_insert)
            with pytest.raises(RuntimeError, match="injected SAS write failure"):
                asyncio.run(exports.ingest_export(**sas_args))
        assert totals() == original_totals
        assert {key: wh.get_meta(key) for key in before} == before

    result = asyncio.run(exports.ingest_export(**sas_args))
    assert result["changed_days"] == 1
    assert wh.get_meta(changed_key) is None
    assert {key: wh.get_meta(key) for key in (zero_key, other_key, outside_key)} == {
        key: before[key] for key in (zero_key, other_key, outside_key)}
    assert totals()[0][2] == 10
    assert totals()[1:] == original_totals[1:]

    files.update(original)
    fetched.clear()
    result = asyncio.run(exports.ingest_export(**args))
    assert not result.get("skipped_unchanged_run")
    assert result["changed_days"] == 1 and "data.csv" in fetched
    assert totals() == original_totals
    assert json.loads(wh.get_meta(changed_key))["run_id"] == "run"
    assert wh.get_meta(other_key) == before[other_key]
    assert wh.get_meta(outside_key) == before[outside_key]
    assert asyncio.run(exports.ingest_export(**args))["skipped_unchanged_run"]


def test_job_and_receipt_updates_rollback_together(postgres_warehouse):
    from app import jobs
    record = jobs.create("focus-bootstrap", ["sub-a"])
    with pytest.raises(RuntimeError, match="injected"), jobs.transaction() as con:
        jobs._put(con, "focus-day:sub-a:2026-06-17", {"run_id": "partial"})
        jobs._put(con, "job:" + record["job_id"], {**record, "status": "ready"})
        raise RuntimeError("injected")
    assert jobs.get(record["job_id"])["status"] == "queued"
    with postgres_warehouse.connect(read_only=True) as con:
        assert con.execute("SELECT count(*) FROM meta WHERE key LIKE 'focus-day:%'").fetchone()[0] == 0


def test_raw_csv_stream_exceeds_old_cap_and_stays_scoped(postgres_warehouse, monkeypatch):
    from app import main
    wh = postgres_warehouse
    with wh.connect() as con:
        con.execute(
            'INSERT INTO costs ("SubAccountId","ChargePeriodStart","BilledCost","BillingCurrency","ResourceName") '
            "SELECT 'sub-a', DATE '2026-07-17', 1, 'USD', '=1+1' FROM generate_series(1,200005)")
        con.execute(
            'INSERT INTO costs ("SubAccountId","ChargePeriodStart","BilledCost","BillingCurrency") '
            "VALUES ('sub-b', DATE '2026-07-17', 9, 'USD')")
    monkeypatch.setattr(main, "current_user", lambda request: SimpleNamespace(admin=True))
    monkeypatch.setattr(main, "permitted", AsyncMock(return_value=["sub-a"]))
    monkeypatch.setattr(main, "request_filters", lambda request: {})

    async def consume():
        response = await main.download_raw_costs(SimpleNamespace(), scope="sub-a")
        count, name_index = 0, None
        async for chunk in response.body_iterator:
            rows = list(csv.reader(io.StringIO(chunk)))
            if name_index is None:
                name_index = rows[0].index("ResourceName")
                rows = rows[1:]
            if rows:
                assert rows[0][name_index] == "'=1+1"
            count += len(rows)
        return count

    assert asyncio.run(consume()) == 200005


def _cost_row(day="2026-06-17"):
    from app.warehouse import COLUMNS, COLUMNS_INDEX
    row = [None] * len(COLUMNS)
    for key, value in {"SubAccountId": "sub-a", "ChargePeriodStart": day,
                       "BilledCost": 2, "BillingCurrency": "USD"}.items():
        row[COLUMNS_INDEX[key]] = value
    return row


def test_quick_preserves_complete_zero_day_and_details_invalidate_receipt(postgres_warehouse):
    from app import jobs
    wh = postgres_warehouse
    key = "focus-day:sub-a:2026-06-17"
    with jobs.transaction() as con:
        jobs._put(con, key, {"fingerprint": "complete-empty-day", "day_rows": 0})
    assert asyncio.run(wh._replace(
        "sub-a", "2026-06-17", "2026-06-17", [_cost_row()], preserve_detail=True)) == 0
    assert wh.row_count() == 0
    assert wh.get_meta(key) is not None
    assert asyncio.run(wh._replace("sub-a", "2026-06-17", "2026-06-17", [_cost_row()])) == 1
    assert wh.get_meta(key) is None


def test_expired_generic_writer_cannot_commit(postgres_warehouse):
    from app import jobs
    record = jobs.create("refresh", ["sub-a"])
    assert jobs.claim(record["job_id"], "current-owner")
    token = jobs.current_lease.set("expired-owner")
    try:
        with pytest.raises(RuntimeError, match="lease expired"):
            asyncio.run(postgres_warehouse._replace(
                "sub-a", "2026-06-17", "2026-06-17", [_cost_row()]))
    finally:
        jobs.current_lease.reset(token)
    assert postgres_warehouse.row_count() == 0


def test_generation_commit_rollback_and_coherent_rollup(postgres_warehouse):
    from app import jobs
    wh = postgres_warehouse
    assert jobs.read_generation() == 0 and not jobs.generation_ready(0)
    with pytest.raises(RuntimeError, match="injected"), jobs.transaction() as con:
        jobs.lock_generation(con)
        jobs.advance_generation(con)
        raise RuntimeError("injected")
    assert jobs.read_generation() == 0
    asyncio.run(wh._replace("sub-a", "2026-06-17", "2026-06-17", [_cost_row()]))
    assert jobs.read_generation() == 1 and not jobs.generation_ready(1)
    wh.refresh_rollup(strict=True)
    assert jobs.generation_ready(1)
    wh.refresh_rollup(strict=True)
    assert jobs.read_generation() == 1 and jobs.generation_ready(1)
    jobs.create("refresh", ["sub-a"])
    assert jobs.read_generation() == 1


def test_failed_rollup_keeps_publication_pending_until_unchanged_replay(postgres_warehouse, monkeypatch):
    from app import derived, exports, jobs
    files = _report()
    class Reader:
        account, container = "offline", "focus"
        async def list(self, prefix=""):
            return [{"name": key, "modified": "2026-07-01", "etag": "v1"} for key in files]
        async def get(self, key):
            return files[key]
        async def close(self):
            pass
    monkeypatch.setattr(exports, "BlobReader", lambda *args: Reader())
    args = {"account": "offline", "container": "focus", "subscription_id": "sub-a", "scope": ["sub-a"],
            "required_metric": "FocusCost", "export_name": "owned",
            "run_identity": ("run", "2026-07-01"), "requested_window": ("2026-06-17", "2026-06-30")}
    with monkeypatch.context() as patch:
        def fail(*args, **kwargs):
            raise RuntimeError("injected rollup failure")
        # The seam `refresh_rollup` rebuilds both derived tables through.
        patch.setattr(derived, "rebuild", fail)
        with pytest.raises(RuntimeError, match="injected rollup"):
            asyncio.run(exports.ingest_export(**args))
    assert jobs.read_generation() == 1 and not jobs.generation_ready(1)
    with postgres_warehouse.connect(read_only=True) as con:
        event = json.loads(con.execute(
            "SELECT value FROM meta WHERE key LIKE 'data-publication:%'").fetchone()[0])
    assert event["status"] == "committed" and event["generation"] == 1
    result = asyncio.run(exports.ingest_export(**args))
    assert result["skipped_unchanged_run"]
    assert jobs.read_generation() == 1 and jobs.generation_ready(1)
    assert json.loads(postgres_warehouse.get_meta("data-publication:" + event["id"]))["status"] == "ready"


def test_rollup_publication_blocks_source_writes(postgres_warehouse, monkeypatch):
    from threading import Event

    from app import jobs
    wh = postgres_warehouse
    attempted, acquired = Event(), Event()
    original = jobs.lock_generation
    def tracked(con):
        attempted.set()
        original(con)
        acquired.set()
    with ThreadPoolExecutor(max_workers=1) as executor:
        with jobs.coherent_rollup(wh):
            monkeypatch.setattr(jobs, "lock_generation", tracked)
            future = executor.submit(
                asyncio.run, wh._replace("sub-a", "2026-06-17", "2026-06-17", [_cost_row()]))
            assert attempted.wait(2)
            assert not acquired.wait(0.05)
            assert jobs.read_generation() == 0
        assert future.result(timeout=5) == 1
    assert jobs.read_generation() == 1 and not jobs.generation_ready(1)


def test_coherent_rollup_needs_only_one_pool_slot(postgres_warehouse, monkeypatch):
    import threading

    from app import jobs
    wh = postgres_warehouse
    asyncio.run(wh._replace("sub-a", "2026-06-17", "2026-06-17", [_cost_row()]))
    monkeypatch.setattr(wh._store, "_slots", threading.BoundedSemaphore(1))
    monkeypatch.setattr(wh._store, "_pool_timeout", 0.1)
    wh.refresh_rollup(strict=True)
    assert jobs.read_generation() == 1 and jobs.generation_ready(1)


def test_focus_job_end_to_end_persists_identity_receipts_and_ready_generation(postgres_warehouse, monkeypatch):
    from app import exports, focus_sync, focus_workflow, jobs
    files = _report()
    class Reader:
        account, container = "offline", "focus"
        async def list(self, prefix=""):
            return [{"name": key, "modified": "2026-07-01", "etag": "v1"} for key in files]
        async def get(self, key):
            return files[key]
        async def close(self):
            pass
    monkeypatch.setattr(exports, "BlobReader", lambda *args: Reader())
    schedule = {"subscription_id": "sub-a", "name": "owned", "account": "offline",
                "container": "focus", "folder": ""}
    monkeypatch.setattr(focus_sync, "owned", AsyncMock(return_value=[schedule]))
    tasks = [{"subscription": "sub-a", "from": "2026-06-17", "to": "2026-06-30",
              "schedule": schedule, "force_new": False}]
    record = jobs.create("focus-bootstrap", ["sub-a"], tasks, idempotency_key="end-to-end")
    result = asyncio.run(jobs.run(record["job_id"], lambda: focus_workflow.step(record["job_id"])))
    assert result["status"] == "ready"
    saved = jobs.get(record["job_id"])
    assert saved["started"] == saved["job_id"] == saved["progress"]["started"]
    assert saved["tasks"][0]["status"] == "ready"
    assert saved["tasks"][0]["result"]["rows"] == 2
    assert postgres_warehouse.row_count() == 2
    assert jobs.read_generation() == 1 and jobs.generation_ready(1)
    with postgres_warehouse.connect(read_only=True) as con:
        assert con.execute("SELECT count(*) FROM meta WHERE key LIKE 'focus-day:%'").fetchone()[0] == 14
    assert jobs.create("focus-bootstrap", ["sub-a"], tasks, idempotency_key="end-to-end")["job_id"] == record["job_id"]
    repeated = AsyncMock()
    asyncio.run(jobs.run(record["job_id"], repeated))
    repeated.assert_not_awaited()
    assert jobs.read_generation() == 1


def test_read_only_keeps_restored_jobs_idle_and_allows_local_cache_rebuild(postgres_warehouse, monkeypatch):
    from app import focus_workflow, ingest_jobs, jobs
    wh = postgres_warehouse
    asyncio.run(wh._replace("sub-a", "2026-06-17", "2026-06-17", [_cost_row()]))
    copied = jobs.create("automatic", ["sub-a"], identity="workload", payload={"months": 3})
    copied_focus = jobs.create("focus-daily", ["sub-a"], identity="workload")
    daily = {"enabled": True, "identity": "workload", "mode": "compact", "lookback": 7}
    wh.set_meta("focus-daily:sub-a", json.dumps(daily))
    monkeypatch.setenv("CLOUDLENS_READ_ONLY", "true")
    async def recover():
        await ingest_jobs.recover()
        await focus_workflow.recover()
        assert await focus_workflow.daily_tick() == set()
    asyncio.run(recover())
    assert jobs.get(copied["job_id"])["status"] == "queued"
    assert jobs.get(copied_focus["job_id"])["status"] == "queued"
    assert json.loads(wh.get_meta("focus-daily:sub-a")) == daily
    with pytest.raises(PermissionError, match="CLOUDLENS_READ_ONLY"):
        asyncio.run(wh._replace("sub-a", "2026-06-17", "2026-06-17", [_cost_row()]))
    wh.refresh_rollup(strict=True)
    assert jobs.read_generation() == 1 and jobs.generation_ready(1)
    assert wh.row_count() == 1


def test_snapshot_generation_observes_generic_commits_without_focus_outbox(postgres_warehouse):
    from app import snapshot_cache
    wh = postgres_warehouse
    wh.refresh_rollup(strict=True)
    with wh.connect(read_only=True) as con:
        before = snapshot_cache.read_generation(con, ["sub-a"])
    asyncio.run(wh._replace("sub-a", "2026-06-17", "2026-06-17", [_cost_row()]))
    wh.refresh_rollup(strict=True)
    with wh.connect(read_only=True) as con:
        after = snapshot_cache.read_generation(con, ["sub-a"])
        assert con.execute("SELECT count(*) FROM meta WHERE key LIKE 'data-publication:%'").fetchone()[0] == 0
    assert after != before, "Cache generation must observe Quick/Details commits, not only FOCUS outbox events"


def test_connection_supplied_cache_callback_fences_pending_and_ignores_late_outbox(
        postgres_warehouse, monkeypatch):
    from app import jobs
    wh = postgres_warehouse
    with wh.connect(read_only=True) as con:
        assert jobs.snapshot_generation(con, ["sub-a"]) == "pending:0:-1"
    asyncio.run(wh._replace("sub-a", "2026-06-17", "2026-06-17", [_cost_row()]))
    with wh.connect(read_only=True) as con:
        assert jobs.snapshot_generation(con, ["sub-a"]) == "pending:1:-1"
    wh.refresh_rollup(strict=True)
    with jobs.transaction() as con:
        jobs._put(con, "data-publication:late", {
            "id": "late", "status": "committed", "generation": 1, "scope": ["sub-a"]})
    with wh.connect(read_only=True) as con, monkeypatch.context() as patch:
        def forbidden(*args, **kwargs):
            pytest.fail("Cache generation must use the supplied transaction connection")
        patch.setattr(wh, "connect", forbidden)
        assert jobs.snapshot_generation(con, ["sub-a"]) == "data:1"
        assert jobs.snapshot_generation(con, ["sub-b"]) == "data:1"
