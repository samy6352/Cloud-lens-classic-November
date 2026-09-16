"""Resumable Quick/Cost Details tasks using the existing warehouse parsers and atomic writer."""
from __future__ import annotations

import asyncio
import calendar
import time
from datetime import date, datetime
from uuid import uuid4

import httpx

from . import jobs
from .job_policy import read_only_mode, require_writable


def frozen_periods(months: int, created_at: str) -> list[tuple[str, str, str]]:
    end = datetime.fromisoformat(created_at).date()
    current = end.year * 12 + end.month - 1
    periods = []
    for number in range(current - max(1, min(months, 12)) + 1, current + 1):
        year, month = number // 12, number % 12 + 1
        first = date(year, month, 1)
        last = min(end, date(year, month, calendar.monthrange(year, month)[1]))
        periods.append((first.isoformat(), last.isoformat(), first.strftime("%B %Y")))
    return periods


async def ingest(subscriptions: list[dict], months: int, metric: str, *, quick: bool) -> dict:
    require_writable()
    from .warehouse import CLIENT_TYPE, COLUMNS_INDEX, QUICK_DEADLINE, _token, warehouse

    jid = jobs.current_job.get()
    if not jid:
        return await (warehouse.quick_ingest(subscriptions, months=months, metric=metric) if quick
                      else warehouse.ingest(subscriptions, months=months, metric=metric))
    record = await asyncio.to_thread(jobs.get, jid)
    if not record["tasks"]:
        periods = frozen_periods(months, record["created_at"])
        spans = [(periods[0][0], periods[-1][1], "Quick requested window")] if quick else periods
        tasks = [
            {"task_id": str(uuid4()), "subscription": sub["id"].lower(),
             "name": sub.get("name") or sub["id"], "from": start, "to": end,
             "label": label, "kind": "quick" if quick else "details",
             "metric": metric, "status": "queued", "attempts": 0}
            for sub in subscriptions for start, end, label in spans
        ]
        record = await asyncio.to_thread(jobs.update, jid, tasks=tasks)
    progress = jobs.current_progress.get()
    progress.update(status="running", metric=metric, mode="quick" if quick else "details",
                    total=len(record["tasks"]), done=0, rows=0, failed=0, progress=0)
    async with httpx.AsyncClient(timeout=300, follow_redirects=True) as client:
        for task in record["tasks"]:
            if task["status"] == "ready":
                progress["done"] += 1
                progress["rows"] += task.get("rows", 0)
                continue
            token = jobs.current_task.set(task["task_id"])
            sub = {"id": task["subscription"], "name": task["name"]}
            try:
                await asyncio.to_thread(jobs.update, jid, task_id=task["task_id"],
                                        status="downloading", attempts=task["attempts"] + 1)
                bearer = await asyncio.to_thread(_token, f"/subscriptions/{sub['id']}")
                headers = {"Authorization": f"Bearer {bearer}", "Content-Type": "application/json",
                           "ClientType": CLIENT_TYPE}
                if task["kind"] == "quick":
                    records = await warehouse._query_period(
                        client, sub, task["from"], task["to"], headers, task["metric"],
                        deadline=time.monotonic() + QUICK_DEADLINE)
                    if records and sum(bool(r[COLUMNS_INDEX["ServiceFamily"]]) for r in records) < len(records) / 2:
                        raise RuntimeError("Most Quick rows lack service classification; existing costs are unchanged.")
                else:
                    links = await warehouse._report(
                        client, sub["id"], task["from"], task["to"], headers, task["metric"])
                    records = await warehouse._fetch(client, links, sub, task["metric"]) if links else []
                if not records:
                    raise RuntimeError("No usable report rows returned; existing costs were retained, not zeroed.")
                await jobs.phase("committing")
                rows = await warehouse._replace(
                    sub["id"], task["from"], task["to"], records,
                    preserve_detail=task["kind"] == "quick")
                await asyncio.to_thread(warehouse._log, sub, task["label"], rows, "ok", task["kind"])
                await asyncio.to_thread(jobs.update, jid, task_id=task["task_id"],
                                        status="ready", rows=rows, finished=jobs.now())
                progress["rows"] += rows
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                progress["failed"] += 1
                await asyncio.to_thread(jobs.update, jid, task_id=task["task_id"],
                                        status="failed", detail=str(exc)[:500])
            finally:
                jobs.current_task.reset(token)
                progress["done"] += 1
                progress["progress"] = progress["done"] / max(1, progress["total"])
    await jobs.phase("warming-cache")
    await warehouse.refresh_rollup_async()
    status = ("partial" if progress["failed"] and progress["rows"] else
              "failed" if progress["failed"] else "ready")
    progress.update(status=status, finished=jobs.now())
    return dict(progress)


async def resume_request(record: dict):
    """Recreate only supported request descriptors; credentials stay in the caller context."""
    require_writable()
    from . import main, onboarding

    payload = record["payload"]
    months = int(payload["months"])
    subs = [{"id": sub, "name": sub} for sub in record["scope"]]
    if record["kind"] == "onboarding":
        return await onboarding._run(subs, months)
    if record["kind"] == "backfill" or payload.get("handler") == "primary":
        return await main._refresh_primary(subs, months=months)
    return await main._run_ingest(months, payload.get("metric", "FocusCost"),
                                  quick=payload.get("quick", False), scope=record["scope"] or None)


def resumable_request(record: dict) -> bool:
    return (record["kind"] in {"automatic", "backfill", "refresh", "onboarding"}
            and isinstance(record.get("payload", {}).get("months"), int))


async def recover() -> None:
    """Resume only explicitly workload-owned tasks; interactive jobs require a fresh caller."""
    if read_only_mode():
        return
    records = await asyncio.to_thread(jobs.listing, active_only=True, limit=500)
    running = {getattr(task, "job_id", None) for task in jobs._workers}
    for record in records:
        if (record["identity"] != "workload"
                or record["job_id"] in running):
            continue
        if record["kind"] == "focus-sync":
            from . import focus_sync
            jobs.spawn(record, lambda record=record: focus_sync.run(record["scope"]))
            continue
        if record["kind"].startswith("focus-"):
            continue
        if not record["tasks"]:
            if resumable_request(record):
                jobs.spawn(record, lambda record=record: resume_request(record))
            continue
        first = record["tasks"][0]
        if first.get("kind") not in {"quick", "details"}:
            continue
        subs = [{"id": t["subscription"], "name": t["name"]} for t in record["tasks"]]
        jobs.spawn(record, lambda subs=subs, first=first: ingest(
            subs, 1, first["metric"], quick=first["kind"] == "quick"))
