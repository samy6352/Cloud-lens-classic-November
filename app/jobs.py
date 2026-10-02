"""Durable ingest ownership and progress, stored in the deployment's PostgreSQL meta table."""
from __future__ import annotations

import asyncio
import contextvars
import hashlib
import json
import logging
from collections.abc import Sequence
from contextlib import contextmanager
from datetime import UTC, date, datetime
from typing import Any
from uuid import uuid4

log = logging.getLogger("cloudlens.jobs")
TERMINAL = frozenset({"ready", "partial", "failed", "cancelled"})
ACTIVE = frozenset({"queued", "waiting-for-Azure", "downloading", "validating",
                    "committing", "warming-cache", "running"})
LEASE_SECONDS = 300
HEARTBEAT_SECONDS = 30
GENERATION_KEY = "data_generation"
ROLLUP_GENERATION_KEY = "data_rollup_generation"
ROLLUP_TABLES_KEY = "data_rollup_tables"
ROLLUP_DIRTY_KEY = "data_rollup_dirty"
ROLLUP_LAYOUT_KEY = "data_rollup_layout"
ROLLUP_LAYOUT_VERSION = "1"
# What a generation stamp written before `ROLLUP_TABLES_KEY` existed covered. A
# stamp carries no roster of its own, and the two tables below are the only ones
# that could have been rebuilt under it, so naming them here reads an old stamp
# exactly rather than distrusting it and forcing a needless rebuild on upgrade.
LEGACY_ROLLUP_TABLES = ("costs_rollup", "resource_presence")
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


def advance_generation(con: Any, window: tuple[date, date] | None = None, *,
                       windows: Sequence[tuple[date, date]] | None = None) -> None:
    """Record source-write coverage; omitted/unknown bounds require a full rebuild.

    The caller holds the generation lock in its write transaction. Explicit windows
    must cover a real change, so an empty collection cannot certify a new generation.
    """
    if window is not None and windows is not None:
        raise ValueError("Specify window or windows, not both")
    affected = windows if windows is not None else ([window] if window is not None else None)
    if affected is not None:
        try:
            affected = list(affected)
            if not affected or any(type(lo) is not date or type(hi) is not date
                                   for lo, hi in affected):
                raise ValueError("Rebuild windows require nonempty half-open date bounds")
            affected = merge_windows(affected)
        except (TypeError, ValueError) as exc:
            raise ValueError("Rebuild windows require nonempty half-open date bounds") from exc
    source, ready = _generation_pair(con)
    pending = dirty_windows(con, source, ready)
    if affected is None or pending is None:
        pending = None
    else:
        pending = merge_windows([*pending, *affected])
    con.execute(
        "UPDATE meta SET value=CAST(CAST(value AS BIGINT)+1 AS TEXT),updated_at=now() WHERE key=?",
        [GENERATION_KEY],
    )
    _write_dirty(con, source + 1, ready, pending)


def read_generation() -> int:
    from .warehouse import warehouse

    with warehouse.connect(read_only=True) as con:
        row = con.execute("SELECT value FROM meta WHERE key=?", [GENERATION_KEY]).fetchone()
        return int(row[0]) if row else 0


def _generation_pair(con: Any) -> tuple[int, int]:
    values = dict(con.execute("SELECT key,value FROM meta WHERE key IN (?,?)",
                              [GENERATION_KEY, ROLLUP_GENERATION_KEY]).fetchall())
    return int(values.get(GENERATION_KEY, 0)), int(values.get(ROLLUP_GENERATION_KEY, -1))


def merge_windows(windows: Sequence[tuple[date, date]]) -> list[tuple[date, date]]:
    """Coalesce overlapping/adjacent ranges without filling gaps in unchanged history."""
    merged: list[tuple[date, date]] = []
    for lo, hi in sorted(windows):
        if not isinstance(lo, date) or not isinstance(hi, date) or lo >= hi:
            raise ValueError("Rebuild windows must have ordered, half-open date bounds")
        if merged and lo <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(hi, merged[-1][1]))
        else:
            merged.append((lo, hi))
    return merged


def _write_dirty(con: Any, source: int, ready: int,
                 windows: list[tuple[date, date]] | None) -> None:
    _put(con, ROLLUP_DIRTY_KEY, {
        "generation": source, "base_generation": ready,
        "windows": None if windows is None else [
            [lo.isoformat(), hi.isoformat()] for lo, hi in windows],
    })


def dirty_windows(con: Any, source: int, ready: int) -> list[tuple[date, date]] | None:
    """None means unknown coverage; never infer a narrow repair from a stale ledger."""
    state = _read(con, ROLLUP_DIRTY_KEY)
    if state is None:
        return [] if source == ready else None
    if state.get("generation") != source or state.get("base_generation") != ready:
        log.warning("derived: dirty-period ledger does not match source/ready generations; "
                    "a full rebuild is required")
        return None
    windows = state.get("windows")
    if windows is None:
        return None
    return merge_windows([(date.fromisoformat(lo), date.fromisoformat(hi))
                          for lo, hi in windows])


def request_rollup(warehouse: Any, window: tuple[date, date] | None) -> None:
    """Persist an explicit maintenance request before its rebuild can fail."""
    with warehouse.connect() as con:
        con.execute("BEGIN")
        try:
            lock_generation(con)
            source, ready = _generation_pair(con)
            pending = dirty_windows(con, source, ready)
            pending = (None if pending is None or window is None
                       else merge_windows([*pending, window]))
            _write_dirty(con, source, ready, pending)
            con.execute("COMMIT")
        except BaseException:
            con.execute("ROLLBACK")
            raise


def rebuild_windows(con: Any, tables: Sequence[str],
                    requested: tuple[date, date] | None = None) -> list[tuple[date, date]] | None:
    """Resolve all pending periods while coherent_rollup holds the source lock."""
    layout = con.execute("SELECT value FROM meta WHERE key=?", [ROLLUP_LAYOUT_KEY]).fetchone()
    if not layout or layout[0] != ROLLUP_LAYOUT_VERSION or not set(tables) <= _stamped_tables(con):
        return None
    source, ready = _generation_pair(con)
    pending = dirty_windows(con, source, ready)
    if pending is None:
        return None
    return merge_windows([*pending, requested]) if requested is not None else pending


def invalidate_rollup(con: Any) -> None:
    """A recreated relation cannot inherit the previous relation's ready stamp."""
    source, _ = _generation_pair(con)
    con.execute(
        "INSERT INTO meta(key,value,updated_at) VALUES (?, '-1', now()) "
        "ON CONFLICT (key) DO UPDATE SET value=excluded.value,updated_at=excluded.updated_at",
        [ROLLUP_GENERATION_KEY],
    )
    _write_dirty(con, source, -1, None)


def _ready(con: Any, source: int, ready: int) -> bool:
    return source >= 0 and source == ready and dirty_windows(con, source, ready) == []


def snapshot_generation(con: Any, scope: list[str] | None = None) -> str:
    """Cache callback using its supplied connection; the revision is intentionally global."""
    source, ready = _generation_pair(con)
    return f"data:{source}" if _ready(con, source, ready) else f"pending:{source}:{ready}"


def rollup_current(con: Any, table: str | None = None) -> bool:
    """Whether the rollup holds the generation the detail table is now at.

    The read-side counterpart to `coherent_rollup`, which stamps the rollup's
    generation inside the same transaction that rebuilt it. Three rows from `meta`,
    so it is cheap enough to ask on every connection that might read the rollup.

    A rollup that was never built reads as generation -1 and is therefore never
    current, which is the answer that keeps an empty rollup from serving zeroes.

    `table` asks the question of one derived table rather than of the set. It is
    not a refinement but a correctness requirement: the generation stamp is a
    single integer covering whichever tables that rebuild actually produced, so a
    table added to the code *after* the last rebuild would otherwise inherit a
    stamp it was never part of and serve an empty relation as though it were
    current. Asking without a table keeps the older, coarser meaning for callers
    that only want to know whether derived data as a whole has caught up.
    """
    source, ready = _generation_pair(con)
    if not _ready(con, source, ready):
        return False
    return table is None or table in _stamped_tables(con)


def _stamped_tables(con: Any) -> frozenset[str]:
    """The derived tables the current generation stamp was written for."""
    row = con.execute("SELECT value FROM meta WHERE key=?", [ROLLUP_TABLES_KEY]).fetchone()
    if not row or not row[0]:
        return frozenset(LEGACY_ROLLUP_TABLES)
    return frozenset(name for name in (part.strip() for part in row[0].split(",")) if name)


def stamped_tables(con: Any) -> frozenset[str]:
    return _stamped_tables(con)


def unstamp(con: Any, tables: Sequence[str]) -> None:
    """Withdraw the ready stamp from `tables` alone, leaving every other table trusted.

    For a table whose contents could not be preserved -- new, recreated for a changed
    grain, or failing its storage check. Invalidating the whole generation instead
    sends *every* dashboard read back to the detail table until a full rebuild of every
    table finishes: hours at a year of retained history, for a change to one table.
    Readers already refuse a table missing from the roster, and `refresh_rollup` builds
    exactly the tables missing from it, so this is sufficient on both sides.
    """
    drop = set(tables)
    stamped = _stamped_tables(con)
    if not drop & stamped:
        return
    remaining = stamped - drop
    if not remaining:
        # An empty roster reads back as the legacy default, which would trust tables
        # that were never stamped. Nothing is left to protect, so rebuild everything.
        invalidate_rollup(con)
        return
    con.execute(
        "INSERT INTO meta (key,value,updated_at) VALUES (?,?,now()) "
        "ON CONFLICT (key) DO UPDATE SET value=excluded.value,updated_at=excluded.updated_at",
        [ROLLUP_TABLES_KEY, ",".join(sorted(remaining))],
    )


def fresh_derived_tables(con: Any) -> frozenset[str]:
    """Which derived tables may be read as current, in one round trip.

    `_derived_sources` needs the answer for three tables at once and is on the path
    of every connection, so it asks here rather than calling `rollup_current` per
    table and paying for the same two `meta` reads three times over.
    """
    source, ready = _generation_pair(con)
    if not _ready(con, source, ready):
        return frozenset()
    return _stamped_tables(con)


def generation_ready(expected: int) -> bool:
    from .warehouse import warehouse

    with warehouse.connect(read_only=True) as con:
        source, ready = _generation_pair(con)
        return source == expected and _ready(con, source, ready)


def derived_complete() -> bool:
    """Whether the ready stamp covers every derived table this release defines.

    A release that adds a table boots with the generation still coherent for the old
    roster, so `generation_ready` alone would never build the new one until the next
    import -- and its readers would stay on the detail table until then.
    """
    from .warehouse import warehouse

    with warehouse.connect(read_only=True) as con:
        return rollup_complete(con)


def rollup_complete(con: Any) -> bool:
    """`rollup_current`, and every table this release defines is covered by the stamp.

    The question an import asks before deciding whether a rebuild is owed: a table
    withdrawn by `unstamp` leaves the generation coherent, but still has to be built.
    """
    from .warehouse import DERIVED_MODULES

    if not rollup_current(con):
        return False
    stamped = _stamped_tables(con)
    return all(module.TABLE in stamped for module in DERIVED_MODULES)


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
def coherent_rollup(warehouse: Any, tables: Sequence[str] | None = None):
    """Freeze source writes until the rebuilt rollup and its ready version are published.

    `tables` names the derived tables the body actually rebuilt, and is stamped
    beside the generation so a reader can tell which tables that generation covers.
    Omitting it stamps every derived table the warehouse knows about, which is the
    right default for callers that rebuild the lot.
    """
    if tables is None:
        from .warehouse import DERIVED_MODULES

        tables = [module.TABLE for module in DERIVED_MODULES]
    roster = ",".join(sorted(tables))
    with warehouse.connect() as con:
        con.execute("BEGIN")
        try:
            lock_generation(con)
            version = con.execute("SELECT value FROM meta WHERE key=?", [GENERATION_KEY]).fetchone()[0]
            yield con
            for key, value in ((ROLLUP_GENERATION_KEY, str(version)),
                               (ROLLUP_TABLES_KEY, roster),
                               (ROLLUP_LAYOUT_KEY, ROLLUP_LAYOUT_VERSION)):
                con.execute(
                    "INSERT INTO meta (key,value,updated_at) VALUES (?,?,now()) "
                    "ON CONFLICT (key) DO UPDATE SET value=excluded.value,updated_at=excluded.updated_at",
                    [key, value],
                )
            _write_dirty(con, int(version), int(version), [])
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
            limit: int = 50, kind: str | None = None) -> list[dict]:
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
                if kind is not None and record["kind"] != kind:
                    continue
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
        if (record["kind"] == "archive" and "progress" in values
                and record.get("progress", {}).get("phase") == "publishing"
                and values["progress"].get("phase") in {"extracting", "uploading"}):
            values["progress"] = {**values["progress"], "phase": "publishing"}
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
    try:
        while True:
            claiming = asyncio.create_task(asyncio.to_thread(claim, job_id, owner))
            try:
                claimed = await asyncio.shield(claiming)
            except asyncio.CancelledError:
                await _settle(claiming)
                raise
            if claimed:
                break
            await asyncio.sleep(2)
        record = await asyncio.to_thread(get, job_id)
        if record is None:
            raise ValueError("Job not found.")
    except BaseException:
        await _settle(asyncio.create_task(asyncio.to_thread(release, owner)))
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
    worker = asyncio.current_task()

    async def heartbeat():
        while True:
            await asyncio.sleep(HEARTBEAT_SECONDS)
            if not await asyncio.to_thread(claim, job_id, owner):
                raise RuntimeError("Lost job lease.")
            saved = await asyncio.to_thread(update, job_id, progress=dict(progress))
            if record.get("kind") == "archive" and saved.get("cancel_requested"):
                worker.cancel()
                return

    beat = asyncio.create_task(heartbeat())
    def heartbeat_finished(done):
        if record.get("kind") == "archive" and not done.cancelled() and done.exception():
            worker.cancel()
    beat.add_done_callback(heartbeat_finished)
    try:
        await asyncio.to_thread(update, job_id, status="running")
        if record.get("kind") == "archive" and record.get("cancel_requested"):
            raise asyncio.CancelledError
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
        async def interrupted():
            saved = await asyncio.to_thread(get, job_id)
            cancelled = record.get("kind") == "archive" and saved.get("cancel_requested")
            try:
                await asyncio.to_thread(
                    update, job_id, status="cancelled" if cancelled else "queued",
                    detail="Cancelled by requester." if cancelled else "Interrupted; awaiting authorized resumption.",
                    finished=now() if cancelled else None, progress=dict(progress))
            except RuntimeError:
                # A replacement owner must never have its ledger overwritten.
                if record.get("kind") != "archive":
                    raise
        await _settle(asyncio.create_task(interrupted()))
        raise
    except Exception as exc:
        saved = await asyncio.to_thread(get, job_id) if record.get("kind") == "archive" else {}
        cancelled = saved.get("cancel_requested", False)
        await asyncio.to_thread(update, job_id, status="cancelled" if cancelled else "failed", detail=str(exc)[:500],
                                finished=now(), progress=dict(progress))
        raise
    finally:
        async def cleanup():
            beat.cancel()
            await asyncio.gather(beat, return_exceptions=True)
            await asyncio.to_thread(release, owner)
        await _settle(asyncio.create_task(cleanup()))
        current_progress.reset(tokens[2])
        current_lease.reset(tokens[1])
        current_job.reset(tokens[0])
        if identity_tokens:
            cost._caller_id.reset(identity_tokens[1])
            cost._caller.reset(identity_tokens[0])


async def _settle(task: asyncio.Task) -> Any:
    """Finish lease/ledger cleanup even if shutdown cancels the worker repeatedly."""
    while True:
        try:
            return await asyncio.shield(task)
        except asyncio.CancelledError:
            if task.done():
                return task.result()


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
