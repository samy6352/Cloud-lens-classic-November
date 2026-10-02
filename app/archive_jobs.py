"""Daily archive requests on the existing durable, single-writer job queue."""
from __future__ import annotations

import asyncio
import hashlib
import json
from datetime import UTC, date, datetime

from . import archive, jobs
from .job_policy import require_writable


def owner(user) -> str:
    return f"{user.source}:{user.email or user.name}".lower()


def create(metric: str, *, scope: list[str] | None = None, source: str = "manual",
           identity: str = "interactive", requested_by: str = "",
           idempotency_key: str | None = None) -> dict:
    require_writable()
    if not archive.enabled():
        raise archive.ArchiveError("No archive account is configured (ARCHIVE_ACCOUNT).")
    archive._archive_limits()
    day = datetime.now(UTC).date()
    scopes = sorted({s.lower() for s in (scope or []) if s})
    suffix = ("selected-" + hashlib.sha256(",".join(scopes).encode()).hexdigest()[:16]
              if scopes else None)
    name = archive.blob_name(metric, day)
    if suffix:
        name = name.replace(".parquet", f"-{suffix}.parquet")
    payload = {"metric": metric, "day": day.isoformat(), "source": source,
               "suffix": suffix, "name": name, "requested_by": requested_by,
               "account": archive.ACCOUNT, "container": archive.CONTAINER}
    fingerprint = hashlib.sha256(json.dumps(
        ["archive", scopes, None, payload], sort_keys=True).encode()).hexdigest()
    with jobs.transaction() as con:
        if idempotency_key:
            scopes_key = hashlib.sha256(
                f"archive|{scopes}|{idempotency_key}".encode()).hexdigest()
            previous_key = jobs._read(con, "jobs:idempotency:" + scopes_key)
            if previous_key:
                previous = jobs._read(con, "job:" + previous_key["job_id"])
                if previous_key["fingerprint"] != fingerprint or previous["identity"] != identity:
                    raise ValueError("The idempotency key already names a different request.")
                return previous
        # Serialize deduplication with ingest and all other job submissions.
        cursor = con.execute("SELECT value FROM meta WHERE key LIKE 'job:%'")
        for row in cursor.fetchall():
            record = json.loads(row[0])
            previous = record.get("payload", {})
            if (record["kind"] == "archive" and record["status"] in jobs.ACTIVE
                    and previous.get("name") == name
                    and previous.get("account") == archive.ACCOUNT
                    and previous.get("container") == archive.CONTAINER):
                if (record["identity"] != identity
                        or previous.get("requested_by") != requested_by):
                    raise ValueError("An archive for this destination is already queued or running.")
                if idempotency_key:
                    jobs._put(con, "jobs:idempotency:" + scopes_key,
                              {"job_id": record["job_id"], "fingerprint": fingerprint})
                return record
        return jobs.create_in_transaction(
            con, "archive", scopes, payload=payload, identity=identity,
            idempotency_key=idempotency_key)


def launch(record: dict):
    if record["status"] not in jobs.ACTIVE:
        return None
    if any(getattr(task, "job_id", None) == record["job_id"] for task in jobs._workers):
        return None
    return jobs.spawn(record, lambda: run(record))


async def run(record: dict) -> dict:
    payload = record["payload"]
    if (payload["account"], payload["container"]) != (archive.ACCOUNT, archive.CONTAINER):
        raise archive.ArchiveError("Archive destination changed; submit a new authorized request.")
    # A restarted process cannot recreate yesterday's snapshot from today's warehouse.
    if payload["day"] != datetime.now(UTC).date().isoformat():
        raise archive.ArchiveError(
            "The requested archive day has passed. No historical snapshot was reconstructed; "
            "submit a new request for today.")
    progress = jobs.current_progress.get()
    progress.update(phase="extracting", rows=0, uploaded_bytes=0, name=payload["name"])
    await asyncio.to_thread(jobs.update, record["job_id"], progress=dict(progress), detail=None)
    written = await archive.archive_current(
        payload["metric"], when=date.fromisoformat(payload["day"]),
        source=payload["source"], scope=record["scope"] or None, suffix=payload["suffix"])
    progress.update(phase="complete", rows=written["rows"], uploaded_bytes=written["bytes"],
                    status="ready")
    return {**written, "status": "ready"}


async def recover() -> None:
    for record in await asyncio.to_thread(jobs.listing, active_only=True, limit=500, kind="archive"):
        if record["kind"] == "archive" and record["identity"] == "workload":
            launch(record)


def describe(record: dict) -> dict:
    with jobs.transaction() as con:
        lease = jobs._read(con, "jobs:lease") or {}
        clock = float(con.execute("SELECT extract(epoch FROM clock_timestamp())").fetchone()[0])
    owned = lease.get("job_id") == record["job_id"] and lease.get("expires", 0) > clock
    return {**record,
            "can_resume": record["status"] != "ready" and not owned
                          and not any(getattr(task, "job_id", None) == record["job_id"]
                                      for task in jobs._workers),
            "can_cancel": record["status"] in jobs.ACTIVE
                          and record.get("progress", {}).get("phase") != "publishing"}


async def resume(record: dict) -> dict:
    require_writable()
    if any(getattr(task, "job_id", None) == record["job_id"] for task in jobs._workers):
        return await asyncio.to_thread(jobs.get, record["job_id"])
    def reset():
        with jobs.transaction() as con:
            current = jobs._read(con, "job:" + record["job_id"])
            if current["status"] == "ready" or current["status"] in jobs.ACTIVE:
                return current
            for row in con.execute("SELECT value FROM meta WHERE key LIKE 'job:%'").fetchall():
                other = json.loads(row[0])
                if (other["job_id"] != current["job_id"] and other["kind"] == "archive"
                        and other["status"] in jobs.ACTIVE
                        and all(other["payload"].get(key) == current["payload"].get(key)
                                for key in ("account", "container", "name"))):
                    raise ValueError("An archive for this destination is already queued or running.")
            current.update(status="queued", finished=None, detail=None, progress={},
                           cancel_requested=False, updated_at=jobs.now())
            current.pop("result", None)
            jobs._put(con, "job:" + current["job_id"], current)
            return current
    record = await asyncio.to_thread(reset)
    launch(record)
    return record


async def cancel(record: dict) -> dict:
    require_writable()
    def request():
        with jobs.transaction() as con:
            current = jobs._read(con, "job:" + record["job_id"])
            if current["status"] in jobs.TERMINAL:
                return current
            if current.get("progress", {}).get("phase") == "publishing":
                raise ValueError("The atomic publication has started; wait for its outcome.")
            current.update(cancel_requested=True, updated_at=jobs.now())
            if current["status"] == "queued":
                current.update(status="cancelled", finished=jobs.now())
            jobs._put(con, "job:" + record["job_id"], current)
            return current
    saved = await asyncio.to_thread(request)
    if saved.get("cancel_requested"):
        for task in list(jobs._workers):
            if getattr(task, "job_id", None) == record["job_id"]:
                task.cancel()
    return saved
