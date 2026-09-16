"""Durable ingest ownership and progress, stored in the deployment's PostgreSQL meta table."""
from __future__ import annotations

import asyncio
import contextvars
import hashlib
import json
import logging
from contextlib import contextmanager
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

log = logging.getLogger("cloudlens.jobs")
TERMINAL = frozenset({"ready", "partial", "failed", "cancelled"})
ACTIVE = frozenset({"queued", "waiting-for-Azure", "downloading", "validating",
                    "committing", "warming-cache", "running"})
LEASE_SECONDS = 300
GENERATION_KEY = "data_generation"
ROLLUP_GENERATION_KEY = "data_rollup_generation"
_LOCK = 741209031
current_progress: contextvars.ContextVar[dict | None] = contextvars.ContextVar(
    "ingest_progress", default=None)
current_job: contextvars.ContextVar[str | None] = contextvars.ContextVar("ingest_job", default=None)
current_task: contextvars.ContextVar[str | None] = contextvars.ContextVar("ingest_task", default=None)
current_lease: contextvars.ContextVar[str | None] = contextvars.ContextVar("ingest_lease", default=None)
_workers: set[asyncio.Task] = set()


def now() -> str:
    return datetime.now(UTC).isoformat()


def _read(con: Any, key: str) -> dict | None:
    row = con.execute("SELECT value FROM meta WHERE key = ?", [key]).fetchone()
    return json.loads(row[0]) if row else None


def _put(con: Any, key: str, value: dict) -> None:
    con.execute(
        "INSERT INTO meta (key,value,updated_at) VALUES (?,?,now()) "
        "ON CONFLICT (key) DO UPDATE SET value=excluded.value,updated_at=excluded.updated_at",
        [key, json.dumps(value, default=str)],
    )


def lock_generation(con: Any) -> None:
    """Lock the source-version row before cost locks, inside the writer's transaction."""
    con.execute(
        "INSERT INTO meta (key,value,updated_at) VALUES (?,?,now()) "
        "ON CONFLICT (key) DO UPDATE SET value=meta.value",
        [GENERATION_KEY, "0"],
    )


def advance_generation(con: Any) -> None:
    """Advance only with an actual cost write; transaction rollback also rolls this back."""
    con.execute(
        "UPDATE meta SET value=CAST(CAST(value AS BIGINT)+1 AS TEXT),updated_at=now() WHERE key=?",
        [GENERATION_KEY],
    )


def read_generation() -> int:
    from .warehouse import warehouse

    with warehouse.connect(read_only=True) as con:
        row = con.execute("SELECT value FROM meta WHERE key=?", [GENERATION_KEY]).fetchone()
        return int(row[0]) if row else 0


def _generation_pair(con: Any) -> tuple[int, int]:
    values = dict(con.execute("SELECT key,value FROM meta WHERE key IN (?,?)",
                              [GENERATION_KEY, ROLLUP_GENERATION_KEY]).fetchall())
    return int(values.get(GENERATION_KEY, 0)), int(values.get(ROLLUP_GENERATION_KEY, -1))


def snapshot_generation(con: Any, scope: list[str] | None = None) -> str:
    """Cache callback using its supplied connection; the revision is intentionally global."""
    source, ready = _generation_pair(con)
    return f"data:{source}" if source >= 0 and source == ready else f"pending:{source}:{ready}"


def generation_ready(expected: int) -> bool:
    from .warehouse import warehouse

    with warehouse.connect(read_only=True) as con:
        source, ready = _generation_pair(con)
    return source == expected and ready == expected


class RollupTransaction:
    """Map the existing rebuilder's transaction to a savepoint in the publication transaction."""
    def __init__(self, connection: Any):
        self.connection = connection

    def execute(self, sql: str, params: Any = None):
        statement = {
            "BEGIN": "SAVEPOINT coherent_rollup",
            "COMMIT": "RELEASE SAVEPOINT coherent_rollup",
            "ROLLBACK": "ROLLBACK TO SAVEPOINT coherent_rollup",
        }.get(sql.strip().upper(), sql)
        return (self.connection.execute(statement) if params is None
                else self.connection.execute(statement, params))


@contextmanager
def coherent_rollup(warehouse: Any):
    """Freeze source writes until the rebuilt rollup and its ready version are published."""
    with warehouse.connect() as con:
        con.execute("BEGIN")
        try:
            lock_generation(con)
            version = con.execute("SELECT value FROM meta WHERE key=?", [GENERATION_KEY]).fetchone()[0]
            yield con
            con.execute(
                "INSERT INTO meta (key,value,updated_at) VALUES (?,?,now()) "
                "ON CONFLICT (key) DO UPDATE SET value=excluded.value,updated_at=excluded.updated_at",
                [ROLLUP_GENERATION_KEY, str(version)],
            )
            con.execute("COMMIT")
        except BaseException:
            con.execute("ROLLBACK")
            raise


@contextmanager
def transaction():
    from .warehouse import warehouse

    with warehouse.connect() as con:
        con.execute("BEGIN")
        try:
            con.execute("SELECT pg_advisory_xact_lock(?)", [_LOCK])
            yield con
            con.execute("COMMIT")
        except BaseException:
            con.execute("ROLLBACK")
            raise


def create(kind: str, scope: list[str], tasks: list[dict] | None = None, *,
           idempotency_key: str | None = None, payload: dict | None = None,
           job_id: str | None = None, identity: str = "interactive") -> dict:
    """Persist before returning an ID. Payloads contain parameters, never credentials."""
    from .job_policy import require_writable
    require_writable()
    with transaction() as con:
        return create_in_transaction(con, kind, scope, tasks, idempotency_key=idempotency_key,
                                     payload=payload, job_id=job_id, identity=identity)


def create_in_transaction(con: Any, kind: str, scope: list[str], tasks: list[dict] | None = None, *,
                          idempotency_key: str | None = None, payload: dict | None = None,
                          job_id: str | None = None, identity: str = "interactive") -> dict:
    """Create using the caller's jobs.transaction connection; do not check out another one."""
    from .job_policy import require_writable
    require_writable()
    assert_lease(con)
    scopes = sorted({s.lower() for s in scope if s})
    fingerprint = hashlib.sha256(json.dumps(
        [kind, scopes, tasks, payload], sort_keys=True).encode()).hexdigest()
    index = ("jobs:idempotency:" + hashlib.sha256(
        f"{kind}|{scopes}|{idempotency_key}".encode()).hexdigest()) if idempotency_key else None
    previous = _read(con, index) if index else None
    if previous:
        if previous["fingerprint"] != fingerprint:
            raise ValueError("The idempotency key already names a different request.")
        return _read(con, "job:" + previous["job_id"])
    jid = job_id or str(uuid4())
    record = {
        "job_id": jid, "started": jid, "created_at": now(), "updated_at": now(),
        "kind": kind, "scope": scopes, "identity": identity, "status": "queued",
        "payload": payload or {}, "progress": {}, "tasks": [
            {**task, "task_id": str(uuid4()), "status": "queued", "attempts": 0}
            for task in (tasks or [])],
    }
    _put(con, "job:" + jid, record)
    if index:
        _put(con, index, {"job_id": jid, "fingerprint": fingerprint})
    return record


def get(job_id: str) -> dict | None:
    from .warehouse import warehouse

    with warehouse.connect(read_only=True) as con:
        return _read(con, "job:" + job_id)


def find_request(kind: str, scope: list[str], idempotency_key: str) -> dict | None:
    from .warehouse import warehouse

    scopes = sorted({s.lower() for s in scope if s})
    key = "jobs:idempotency:" + hashlib.sha256(
        f"{kind}|{scopes}|{idempotency_key}".encode()).hexdigest()
    with warehouse.connect(read_only=True) as con:
        index = _read(con, key)
        return _read(con, "job:" + index["job_id"]) if index else None


def listing(scope: list[str] | None = None, *, active_only: bool = False,
            limit: int = 50) -> list[dict]:
    from .warehouse import warehouse

    # Filter before limiting: unrelated tenants' jobs must not crowd out this scope.
    allowed = None if scope is None else {s.lower() for s in scope}
    result = []
    with warehouse.connect(read_only=True) as con:
        cursor = con.execute(
            "SELECT value FROM meta WHERE key LIKE 'job:%' ORDER BY updated_at DESC")
        while batch := cursor.fetchmany(100):
            for row in batch:
                record = json.loads(row[0])
                if allowed is not None and (not record["scope"] or
                                             not set(record["scope"]).issubset(allowed)):
                    continue
                if active_only and record["status"] not in ACTIVE:
                    continue
                result.append(record)
                if len(result) >= limit:
                    return result
    return result


def update(job_id: str, *, task_id: str | None = None, **values: Any) -> dict:
    with transaction() as con:
        assert_lease(con)
        record = _read(con, "job:" + job_id)
        if record is None:
            raise ValueError("Job not found.")
        target = record if task_id is None else next(
            t for t in record["tasks"] if t["task_id"] == task_id)
        # Job identity and original request never change during a retry.
        for name in ("job_id", "started", "scope", "created_at", "task_id", "identity", "kind", "payload"):
            values.pop(name, None)
        target.update(values)
        record["updated_at"] = now()
        _put(con, "job:" + job_id, record)
        return record


def claim(job_id: str, owner: str) -> bool:
    with transaction() as con:
        clock = float(con.execute("SELECT extract(epoch FROM clock_timestamp())").fetchone()[0])
        lease = _read(con, "jobs:lease") or {}
        if lease.get("expires", 0) > clock and lease.get("owner") != owner:
            return False
        _put(con, "jobs:lease", {"job_id": job_id, "owner": owner,
                                 "expires": clock + LEASE_SECONDS})
        return True


def release(owner: str) -> None:
    with transaction() as con:
        lease = _read(con, "jobs:lease") or {}
        if lease.get("owner") == owner:
            con.execute("DELETE FROM meta WHERE key = 'jobs:lease'")


def assert_lease(con: Any) -> None:
    """Fence a commit inside its transaction, before locking cost partitions."""
    from .job_policy import require_writable
    require_writable()
    owner = current_lease.get()
    if owner is None:
        return
    con.execute("SELECT pg_advisory_xact_lock(?)", [_LOCK])
    lease = _read(con, "jobs:lease") or {}
    clock = float(con.execute("SELECT extract(epoch FROM clock_timestamp())").fetchone()[0])
    if lease.get("owner") != owner or lease.get("expires", 0) <= clock:
        raise RuntimeError("Ingest lease expired; no data was changed. Resume this job.")


async def run(job_id: str, factory: Any) -> Any:
    """One deployment writer; other jobs remain queued, not failed or overwritten."""
    from .job_policy import require_writable
    require_writable()
    owner = str(uuid4())
    while not await asyncio.to_thread(claim, job_id, owner):
        await asyncio.sleep(2)
    try:
        record = await asyncio.to_thread(get, job_id)
        if record is None:
            raise ValueError("Job not found.")
    except BaseException:
        await asyncio.shield(asyncio.to_thread(release, owner))
        raise
    if record["status"] in TERMINAL:
        await asyncio.to_thread(release, owner)
        return record.get("result")
    progress = dict(record.get("progress") or {})
    progress.update(job_id=job_id, started=job_id, status="running")
    tokens = (current_job.set(job_id), current_lease.set(owner), current_progress.set(progress))
    identity_tokens = None
    if record.get("identity") == "workload":
        from . import cost
        identity_tokens = (cost._caller.set(None), cost._caller_id.set("server"))

    async def heartbeat():
        while True:
            await asyncio.sleep(30)
            if not await asyncio.to_thread(claim, job_id, owner):
                raise RuntimeError("Lost job lease.")
            await asyncio.to_thread(update, job_id, progress=dict(progress))

    beat = asyncio.create_task(heartbeat())
    try:
        await asyncio.to_thread(update, job_id, status="running")
        result = await factory()
        state = result if isinstance(result, dict) and result.get("status") else progress
        status = state.get("status", "ready")
        if status not in TERMINAL | {"waiting-for-Azure", "queued"}:
            status = "ready" if not state.get("failed") else "partial"
        await asyncio.to_thread(update, job_id, status=status, progress=dict(progress),
                                result=result if isinstance(result, dict) else dict(progress),
                                finished=now() if status in TERMINAL else None)
        return result
    except asyncio.CancelledError:
        await asyncio.shield(asyncio.to_thread(
            update, job_id, status="queued", detail="Interrupted; awaiting authorized resumption.",
            progress=dict(progress)))
        raise
    except Exception as exc:
        await asyncio.to_thread(update, job_id, status="failed", detail=str(exc)[:500],
                                finished=now(), progress=dict(progress))
        raise
    finally:
        beat.cancel()
        await asyncio.gather(beat, return_exceptions=True)
        await asyncio.shield(asyncio.to_thread(release, owner))
        current_progress.reset(tokens[2])
        current_lease.reset(tokens[1])
        current_job.reset(tokens[0])
        if identity_tokens:
            cost._caller_id.reset(identity_tokens[1])
            cost._caller.reset(identity_tokens[0])


def spawn(record: dict, factory: Any) -> asyncio.Task:
    from .job_policy import require_writable
    require_writable()
    task = asyncio.create_task(run(record["job_id"], factory))
    task.job_id = record["job_id"]
    _workers.add(task)

    def finished(done):
        _workers.discard(done)
        if not done.cancelled() and done.exception():
            log.warning("Job %s: %s", record["job_id"], done.exception())

    task.add_done_callback(finished)
    return task


async def stop() -> None:
    tasks = list(_workers)
    for task in tasks:
        task.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)


async def phase(status: str) -> None:
    jid, tid = current_job.get(), current_task.get()
    if jid:
        await asyncio.to_thread(update, jid, status=status)
        if tid:
            await asyncio.to_thread(update, jid, task_id=tid, status=status)


async def publish_generation(scope: list[str], windows: dict, generation: Any = None,
                             event_id: str | None = None) -> None:
    """Postcommit outbox for authorized cache warmers; consumers choose their own cache keys."""
    event = {"id": event_id or str(uuid4()), "scope": scope, "windows": windows, "status": "ready",
             "generation": generation, "published_at": now(), "job_id": current_job.get()}
    def persist():
        with transaction() as con:
            previous = _read(con, "data-publication:" + event["id"])
            if previous and previous.get("status") == "ready":
                return
            if previous and event["generation"] is None:
                event["generation"] = previous.get("generation")
            _put(con, "data-publication:" + event["id"], event)
    await asyncio.to_thread(persist)
