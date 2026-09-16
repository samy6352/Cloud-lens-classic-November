"""Consume deployment-owned FOCUS reports; exporting and ingesting are separate phases."""
from __future__ import annotations

import asyncio
import json
import logging
import os
from datetime import UTC, datetime
from typing import Any

from . import schedules

log = logging.getLogger("cloudlens.focus_sync")
POLL_SECONDS = 900
WAIT_SECONDS = 900
RETRY_SECONDS = 15
_active: set[str] = set()


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _key(subscription: str) -> str:
    return f"focus-sync:{subscription.lower()}"


def _read(subscription: str) -> dict[str, Any]:
    from .warehouse import warehouse

    with warehouse.connect(read_only=True) as con:
        row = con.execute("SELECT value FROM meta WHERE key = ?", [_key(subscription)]).fetchone()
    return json.loads(row[0]) if row else {"subscription": subscription, "status": "idle"}


def _write(subscription: str, update: dict[str, Any]) -> None:
    from .warehouse import warehouse

    record = {**_read(subscription), **update, "subscription": subscription}
    with warehouse.connect() as con:
        con.execute(
            "INSERT INTO meta (key, value, updated_at) VALUES (?, ?, now()) "
            "ON CONFLICT (key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at",
            [_key(subscription), json.dumps(record)],
        )


async def record(subscription: str, **update: Any) -> None:
    await asyncio.to_thread(_write, subscription, update)


async def status(scope: list[str]) -> dict[str, Any]:
    from . import archive

    rows = [await asyncio.to_thread(_read, sub) for sub in scope]
    running = any(row["status"] in {"waiting", "reading", "running", "waiting-for-Azure"}
                  for row in rows)
    failed = sum(row["status"] == "failed" for row in rows)
    ready = sum(row["status"] == "ready" for row in rows)
    return {
        "status": "running" if running else "partial" if failed and ready
        else "failed" if failed else "ready" if ready else "idle",
        "subscriptions": rows,
        "started": max((row.get("started") or "" for row in rows), default="") or None,
        "finished": None if running else max((row.get("finished") or "" for row in rows), default="") or None,
        "poll_seconds": POLL_SECONDS,
        "auto_enabled": os.getenv("AUTO_FOCUS_SYNC", "true").lower() != "false" and archive.enabled(),
        "detail": "Azure billing can lag; latest means the newest completed report, not real-time usage.",
    }


async def discover(scope: list[str]) -> dict[str, Any]:
    if not scope:
        return {"schedules": [], "errors": []}
    result = await schedules.listing(scope, owned_only=True)
    selected = {sub.lower() for sub in scope}
    return {
        "schedules": [row for row in result["schedules"]
                      if row["owned"] and row["subscription_id"].lower() in selected],
        "errors": [error for error in result.get("errors", [])
                   if error["subscription"].lower() in selected],
    }


async def owned(scope: list[str]) -> list[dict[str, Any]]:
    result = await discover(scope)
    if result.get("errors"):
        raise schedules.ScheduleError("; ".join(
            f"{e['subscription']}: {e['detail']}" for e in result["errors"]))
    return result["schedules"]


async def _runs(schedule: dict[str, Any], *, awaiting: bool) -> list:
    from .exports import BlobReader, ExportError, read_manifests
    from .scopes import latest_per_period

    reader = BlobReader(schedule["account"], schedule["container"])
    try:
        try:
            manifests = await read_manifests(reader, prefix=schedule["folder"], required_metric="FocusCost")
        except ExportError as exc:
            # Azure creates the destination container with the first completed run.
            if awaiting and str(exc).startswith("Container '") and "not found on" in str(exc):
                return []
            raise
        return latest_per_period(m for m in manifests
                                 if m.export_name == schedule["name"] and m.dataset == "FocusCost")
    finally:
        await reader.close()


async def capture(subscription: str) -> list[list[str]]:
    """Remember existing reports before Azure is asked to generate another one."""
    dest = schedules.destination(subscription)
    row = {"name": schedules.export_name("FocusCost", **dest),
           "account": dest["account_id"].rsplit("/", 1)[-1],
           "container": dest["container"], "folder": dest["folder"]}
    return [[m.run_id, m.submitted] for m in await _runs(row, awaiting=True)]


async def run(scope: list[str], *, request_new: bool = False,
              wait_for_reports: bool = False) -> dict[str, Any]:
    """Only the caller's shared ingest slot may run this job. No schedule is created here."""
    from .job_policy import require_writable
    require_writable()
    from .exports import ingest_export

    if not scope:
        raise ValueError("Select at least one subscription.")
    from . import cost_data
    managed = {sub for sub in scope if (await asyncio.to_thread(cost_data.read, sub)).get("version") == 2}
    if managed and request_new:
        raise ValueError("Cost data owns this subscription's update cadence. Review Refresh now there.")
    scope = [sub for sub in scope if sub not in managed]
    if not scope:
        return {"status": "cancelled", "subscriptions": [],
                "detail": "The approved Cost data pipeline owns these subscriptions; no legacy import was run."}
    _active.update(scope)
    pending: dict[str, dict[str, Any]] = {}
    from . import jobs
    started = jobs.current_job.get() or _now()
    try:
        max_files = int(os.getenv("EXPORT_MAX_FILES", "60"))
        if max_files < 1:
            raise ValueError("EXPORT_MAX_FILES must be positive.")
        saved = {sub: await asyncio.to_thread(_read, sub) for sub in scope}
        for sub in scope:
            await record(sub, status="running", started=started, finished=None,
                         detail="Checking the FOCUS export.", changed_days=0, unchanged_days=0, rows=0)
        found = await discover(scope)
        by_sub = {row["subscription_id"]: row for row in found["schedules"]}
        errors = {e["subscription"]: e["detail"] for e in found["errors"]}
        for sub in scope:
            if sub in errors:
                await record(sub, status="failed", finished=_now(), detail=errors[sub])
                continue
            schedule = by_sub.get(sub)
            if schedule is None:
                await record(sub, status="failed", finished=_now(),
                             detail="No deployment-owned FOCUS schedule. Create one in Cost exports.")
                continue
            try:
                previous = await _runs(schedule, awaiting=request_new or wait_for_reports)
                awaiting = saved[sub].get("awaiting_runs")
                baseline = ({(m.run_id, m.submitted) for m in previous} if request_new
                            else {tuple(item) for item in awaiting} if awaiting is not None else set())
                if request_new:
                    await record(sub, awaiting_runs=[list(item) for item in baseline])
                    await schedules.run_now(sub, schedule["name"], current_month=True)
                pending[sub] = {"schedule": schedule, "baseline": baseline}
                await record(sub, status="waiting", detail="Waiting for Azure's complete FOCUS report.",
                             export=schedule["name"])
            except Exception as exc:
                log.warning("FOCUS refresh could not start on %s: %s", sub, exc)
                await record(sub, status="failed", finished=_now(), detail=str(exc)[:300])
        deadline = asyncio.get_running_loop().time() + WAIT_SECONDS
        while pending:
            for sub, item in list(pending.items()):
                schedule = item["schedule"]
                try:
                    runs = await _runs(schedule, awaiting=request_new or wait_for_reports)
                    fresh = [m for m in runs if (m.run_id, m.submitted) not in item["baseline"]]
                    if request_new:
                        fresh = [m for m in fresh if (m.start or "")[:7] == started[:7]]
                    if not fresh:
                        if not (request_new or wait_for_reports):
                            await record(sub, status="idle", finished=_now(),
                                         detail="No complete FOCUS report yet. The next check will retry.")
                            del pending[sub]
                        continue
                    await record(sub, status="reading", detail="Applying changed cost days to the database.")
                    result = {}
                    for manifest in fresh:
                        result = await ingest_export(
                            account=schedule["account"], container=schedule["container"],
                            prefix=schedule["folder"], export_name=schedule["name"],
                            subscription_id=sub, scope=[sub], metric="FocusCost", required_metric="FocusCost",
                            max_files=max_files, run_identity=(manifest.run_id, manifest.submitted),
                        )
                    await record(
                        sub, status="ready", finished=_now(), last_ingested=_now(), through=result["to"],
                        detail="Complete report ingested; unchanged cost days were retained.",
                        rows=result["rows"], changed_days=result.get("changed_days", 0),
                        unchanged_days=result.get("unchanged_days", 0),
                        awaiting_runs=None,
                    )
                    del pending[sub]
                except Exception as exc:
                    log.warning("FOCUS import failed on %s: %s", sub, exc)
                    await record(sub, status="failed", finished=_now(), detail=str(exc)[:300])
                    del pending[sub]
            if not pending:
                break
            if asyncio.get_running_loop().time() >= deadline:
                for sub in pending:
                    await record(sub, status="waiting-for-Azure", finished=None, detail=(
                        "Azure is still generating the report; generation can take hours. "
                        "Existing costs are unchanged; the persisted run will be checked again."))
                break
            await asyncio.sleep(RETRY_SECONDS)
    except asyncio.CancelledError:
        for sub in scope:
            previous = await asyncio.to_thread(_read, sub)
            if previous.get("status") in {"running", "waiting", "reading"}:
                await record(sub, status="waiting-for-Azure", finished=None,
                             detail="Report sync interrupted. The persisted run will be checked again.")
        raise
    except Exception as exc:
        for sub in scope:
            await record(sub, status="failed", finished=_now(), detail=str(exc)[:300])
        raise
    finally:
        _active.difference_update(scope)
    result = await status(scope)
    if pending:
        result["status"] = "waiting-for-Azure"
    return result
