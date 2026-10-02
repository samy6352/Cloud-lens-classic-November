"""Bounded daily archive extraction and atomic, bounded-memory file uploads."""
from __future__ import annotations

import asyncio
import threading
from datetime import date
from types import SimpleNamespace
from xml.etree import ElementTree

import httpx
import pyarrow.parquet as pq
import pytest

from app import archive


def _seed(wh):
    from app.warehouse import COLUMNS, insert_rows

    records = []
    for i in range(5):
        row = {"SubAccountId": "a" if i < 3 else "b", "SubAccountName": "Alpha",
               "ChargePeriodStart": f"2026-09-{5 - i:02d}", "BilledCost": -0.63 if i == 0 else i,
               "BenefitId": None if i < 2 else "later-value", "BillingCurrency": "USD"}
        records.append([row.get(name) for name in COLUMNS])
    with wh.connect() as con:
        insert_rows(con, records)


def test_archive_streams_one_snapshot_with_typed_null_batches(postgres_warehouse, tmp_path, monkeypatch):
    wh = postgres_warehouse
    _seed(wh)
    monkeypatch.setattr(archive, "_ROW_BATCH", 2)
    from app.warehouse import COLUMNS, insert_rows

    original = archive._write_batches
    seen = []

    def record(destination, fmt, columns, batches, **kwargs):
        def observed():
            for batch in batches:
                seen.append(len(batch))
                if len(seen) == 1:
                    cursor = execute("SELECT statement, is_holdable FROM pg_cursors "
                                     "WHERE name LIKE 'archive_%'").fetchone()
                    assert cursor and "ORDER BY" not in cursor[0] and not cursor[1]
                    with wh.connect() as writer:
                        row = {"ChargePeriodStart": "2026-09-06", "SubAccountId": "c", "BilledCost": 999}
                        insert_rows(writer, [[row.get(name) for name in COLUMNS]])
                yield batch
        return original(destination, fmt, columns, observed(), **kwargs)

    monkeypatch.setattr(archive, "_write_batches", record)
    path = tmp_path / "archive.parquet"
    with wh.connect(read_only=True) as con:
        con.execute("SELECT set_config('statement_timeout', '37s', false)")
        execute = con.execute

        def guarded(sql, params=None):
            assert "count(*)" not in sql.lower() and "select *" not in sql.lower()
            return execute(sql, params)

        monkeypatch.setattr(con, "execute", guarded)
        rows, size = archive._write_archive(path, "parquet", con)
        assert execute("SHOW statement_timeout").fetchone()[0] == "37s"
        assert execute("SELECT count(*) FROM pg_cursors WHERE name LIKE 'archive_%'").fetchone()[0] == 0
    assert rows == 5 and size > 0 and seen == [2, 2, 1]
    table = pq.read_table(path)
    assert set(table["SubAccountId"].to_pylist()) == {"a", "b"}
    assert sorted(table["BilledCost"].to_pylist()) == [-0.63, 1, 2, 3, 4]
    assert table["BenefitId"].to_pylist() == [None, None, "later-value", "later-value", "later-value"]
    assert all(isinstance(day, date) for day in table["ChargePeriodStart"].to_pylist())


def test_archive_stream_respects_scoped_view(postgres_warehouse, tmp_path, monkeypatch):
    wh = postgres_warehouse
    _seed(wh)
    monkeypatch.setattr(archive, "_ROW_BATCH", 2)
    path = tmp_path / "scope.csv"
    with wh.connect(read_only=True, scope=["b"]) as con:
        rows, _ = archive._write_archive(path, "csv", con)
    import csv
    with path.open(newline="") as source:
        records = list(csv.DictReader(source))
    assert rows == 2 and {row["SubAccountId"] for row in records} == {"b"}


def test_durable_archive_job_completes_with_parquet_readback(postgres_warehouse, tmp_path, monkeypatch):
    from app import archive_jobs, jobs
    _seed(postgres_warehouse)
    monkeypatch.setattr(archive, "ACCOUNT", "offline")
    monkeypatch.setattr(archive, "CONTAINER", "cloudlens")
    monkeypatch.setattr(archive, "_ROW_BATCH", 2)
    result_path = tmp_path / "readback.parquet"
    class Writer:
        async def put_file(self, name, path, content_type):
            import shutil
            shutil.copyfile(path, result_path)
            return {"name": name, "bytes": path.stat().st_size, "etag": "verified"}
        async def close(self):
            pass
    monkeypatch.setattr(archive, "BlobWriter", Writer)
    record = archive_jobs.create("FocusCost", requested_by="file:admin")
    async def scenario():
        task = archive_jobs.launch(record)
        await task
    asyncio.run(scenario())
    saved = jobs.get(record["job_id"])
    table = pq.read_table(result_path)
    assert saved["status"] == "ready" and saved["result"]["rows"] == table.num_rows == 5
    assert saved["result"]["bytes"] == result_path.stat().st_size
    assert saved["result"]["etag"] == "verified"
    assert set(table["SubAccountId"].to_pylist()) == {"a", "b"}
    assert saved["result"]["scope"] is None and "-selected-" not in saved["result"]["name"]


def test_archive_timeout_rolls_back_and_returns_connection(postgres_warehouse, tmp_path, monkeypatch):
    wh = postgres_warehouse
    _seed(wh)
    monkeypatch.setenv("ARCHIVE_EXPORT_TIMEOUT_SECONDS", "1")
    clock = iter([0, 2])
    monkeypatch.setattr(archive, "time", SimpleNamespace(monotonic=lambda: next(clock)))
    with wh.connect(read_only=True) as con:
        con.execute("SELECT set_config('statement_timeout', '37s', false)")
        with pytest.raises(archive.ArchiveError, match="total time limit"):
            archive._write_archive(tmp_path / "timeout.parquet", "parquet", con)
        assert con.execute("SHOW statement_timeout").fetchone()[0] == "37s"
        assert con.execute("SELECT 1").fetchone()[0] == 1


@pytest.mark.parametrize("value", ["0", "-1", "no-limit", "1.5", "86401"])
def test_archive_limits_reject_unbounded_or_invalid_values(monkeypatch, value):
    monkeypatch.setenv("ARCHIVE_QUERY_TIMEOUT_SECONDS", value)
    with pytest.raises(archive.ArchiveError, match="ARCHIVE_QUERY_TIMEOUT_SECONDS"):
        archive._archive_limits()


def test_file_upload_stages_bounded_blocks_before_atomic_commit(tmp_path, monkeypatch):
    path = tmp_path / "archive.parquet"
    path.write_bytes(b"preserved-negative-and-null-values" * 3)
    monkeypatch.setattr(archive, "_UPLOAD_CHUNK", 16)
    calls = []
    original = httpx.AsyncClient

    async def transport(request):
        calls.append(request)
        return httpx.Response(201, headers={"ETag": "committed"})

    monkeypatch.setattr(archive.httpx, "AsyncClient",
                        lambda **kwargs: original(transport=httpx.MockTransport(transport), **kwargs))
    writer = archive.BlobWriter("offline", "archive")
    writer._token = "fixture"
    result = asyncio.run(writer.put_file("daily.parquet", path, "application/vnd.apache.parquet"))
    blocks = [r for r in calls if r.url.params.get("comp") == "block"]
    commit = calls[-1]
    assert blocks and all(len(r.content) <= 16 for r in blocks)
    assert b"".join(r.content for r in blocks) == path.read_bytes()
    assert commit.url.params["comp"] == "blocklist"
    assert [node.text for node in ElementTree.fromstring(commit.content)] == [
        r.url.params["blockid"] for r in blocks]
    assert len({r.url.params["blockid"] for r in blocks}) == len(blocks)
    assert result["bytes"] == path.stat().st_size and result["etag"] == "committed"
    assert commit.headers["x-ms-blob-content-type"] == "application/vnd.apache.parquet"


def test_failed_block_never_commits_over_existing_archive(tmp_path, monkeypatch):
    path = tmp_path / "archive.parquet"
    path.write_bytes(b"123456789")
    calls = []
    monkeypatch.setattr(archive, "_UPLOAD_CHUNK", 4)
    original = httpx.AsyncClient

    async def transport(request):
        calls.append(request)
        return httpx.Response(503 if request.url.params.get("comp") == "block" else 201)

    monkeypatch.setattr(archive.httpx, "AsyncClient",
                        lambda **kwargs: original(transport=httpx.MockTransport(transport), **kwargs))
    writer = archive.BlobWriter("offline", "archive")
    writer._token = "fixture"
    with pytest.raises(archive.ArchiveError):
        asyncio.run(writer.put_file("existing.parquet", path, "application/vnd.apache.parquet"))
    assert not any(r.url.params.get("comp") == "blocklist" for r in calls)


def test_archive_extraction_is_off_loop_and_cancellation_joins_worker(tmp_path, monkeypatch):
    started, stopped = threading.Event(), threading.Event()
    paths = []

    def write(path, *, scope, cancel):
        paths.append(path)
        path.write_bytes(b"partial")
        started.set()
        assert cancel.wait(5)
        assert path.exists()
        stopped.set()
        raise archive.ArchiveError("Archive extraction was cancelled.")

    monkeypatch.setattr(archive, "ACCOUNT", "offline")
    monkeypatch.setitem(archive.FORMATS, "parquet", (write, ".parquet", "application/vnd.apache.parquet"))

    async def run():
        task = asyncio.create_task(archive.archive_current("ActualCost"))
        while not started.is_set():
            await asyncio.sleep(0.01)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(run())
    assert stopped.is_set() and paths and not paths[0].exists()


def test_archive_current_keeps_name_scope_and_file_until_upload(monkeypatch):
    main_thread = threading.get_ident()
    paths = []

    def write(path, *, scope, cancel):
        assert threading.get_ident() != main_thread and scope is None and not cancel.is_set()
        path.write_bytes(b"compressed-file")
        paths.append(path)
        return 123, path.stat().st_size

    class Writer:
        async def put_file(self, name, path, content_type):
            assert name == "actual/2026/10/actual-2026-10-02.parquet"
            assert path.exists() and content_type == "application/vnd.apache.parquet"
            return {"name": name, "bytes": path.stat().st_size}

        async def close(self):
            assert paths[0].exists()

    monkeypatch.setattr(archive, "ACCOUNT", "offline")
    monkeypatch.setattr(archive, "BlobWriter", Writer)
    monkeypatch.setitem(archive.FORMATS, "parquet", (write, ".parquet", "application/vnd.apache.parquet"))
    result = asyncio.run(archive.archive_current("ActualCost", when=date(2026, 10, 2)))
    assert result["rows"] == 123 and result["scope"] is None
    assert result["compressed_bytes"] == result["bytes"] == len(b"compressed-file")
    assert not paths[0].exists()


def test_empty_archive_never_starts_an_upload(monkeypatch):
    def write(path, **kwargs):
        return 0, 0

    def writer():
        raise AssertionError("An empty export must not replace a good archive")

    monkeypatch.setattr(archive, "ACCOUNT", "offline")
    monkeypatch.setattr(archive, "BlobWriter", writer)
    monkeypatch.setitem(archive.FORMATS, "parquet", (write, ".parquet", "application/vnd.apache.parquet"))
    with pytest.raises(archive.ArchiveError, match="nothing to archive"):
        asyncio.run(archive.archive_current("ActualCost"))


def test_repeated_cancellation_waits_for_extraction_before_cleanup(monkeypatch):
    started, cancelled, release = threading.Event(), threading.Event(), threading.Event()
    paths = []

    def write(path, *, scope, cancel):
        paths.append(path)
        path.write_bytes(b"partial")
        started.set()
        assert cancel.wait(5)
        cancelled.set()
        assert release.wait(5)
        assert path.exists()
        raise archive.ArchiveError("Cancelled")

    monkeypatch.setattr(archive, "ACCOUNT", "offline")
    monkeypatch.setitem(archive.FORMATS, "parquet", (write, ".parquet", "application/vnd.apache.parquet"))

    async def run():
        task = asyncio.create_task(archive.archive_current("ActualCost"))
        try:
            while not started.is_set():
                await asyncio.sleep(0.01)
            task.cancel()
            while not cancelled.is_set():
                await asyncio.sleep(0.01)
            task.cancel()
            await asyncio.sleep(0.02)
            assert not task.done() and paths[0].exists()
        finally:
            release.set()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(run())
    assert not paths[0].exists()
