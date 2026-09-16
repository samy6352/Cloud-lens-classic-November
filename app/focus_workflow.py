"""Exact-window FOCUS work. Azure acceptance is pending, never proof of complete costs."""
from __future__ import annotations

import asyncio
import hashlib
import json
from datetime import UTC, date, datetime, timedelta

from . import jobs, schedules
from .job_policy import read_only_mode, require_writable

DEFAULT_LOOKBACK = 7
_running: set[str] = set()


def window(days: int = 90, today: date | None = None) -> tuple[str, str]:
    hi = today or datetime.now(UTC).date()
    return (hi - timedelta(days=days - 1)).isoformat(), hi.isoformat()


def chunks(start: str, end: str) -> list[dict]:
    lo, hi = date.fromisoformat(start), date.fromisoformat(end)
    if lo > hi or (hi - lo).days >= 366 or hi > datetime.now(UTC).date():
        raise ValueError("Choose an ordered historical window of at most 366 days.")
    result = []
    while lo <= hi:
        next_month = (lo.replace(day=28) + timedelta(days=4)).replace(day=1)
        stop = min(hi, next_month - timedelta(days=1))
        result.append({"from": lo.isoformat(), "to": stop.isoformat()})
        lo = stop + timedelta(days=1)
    return result


def coverage(subscription: str, start: str, end: str) -> dict:
    from .warehouse import warehouse

    with warehouse.connect(read_only=True) as con:
        rows = con.execute(
            "SELECT key,value FROM meta WHERE key >= ? AND key <= ? ORDER BY key",
            [f"focus-day:{subscription.lower()}:{start}", f"focus-day:{subscription.lower()}:{end}"],
        ).fetchall()
    known = {r[0].rsplit(":", 1)[-1]: json.loads(r[1]) for r in rows}
    days = (date.fromisoformat(end) - date.fromisoformat(start)).days + 1
    return {"requested_days": days, "authoritative_days": len(known),
            "missing_days": days - len(known), "dates": sorted(known),
            "source": "validated FOCUS manifests only", "current_day_provisional": end == window(1)[1]}


async def manifests(schedule: dict) -> list:
    from .exports import BlobReader, ExportError, read_manifests

    reader = BlobReader(schedule["account"], schedule["container"])
    try:
        try:
            found = await read_manifests(reader, prefix=schedule["folder"], required_metric="FocusCost")
        except ExportError as exc:
            if "not found on" in str(exc):
                return []
            raise
        # Never collapse custom executions using a native MonthToDate definition's month key.
        return [m for m in found if m.export_name == schedule["name"] and m.dataset == "FocusCost"]
    finally:
        await reader.close()


def actual_window(manifest) -> tuple[str, str]:
    from .exports import _focus_windows

    periods = set(_focus_windows([manifest]).values())
    if len(periods) != 1:
        raise ValueError("A run must declare one unambiguous complete period.")
    return next(iter(periods))


def submitted_after(manifest, requested_at: str) -> bool:
    try:
        submitted = datetime.fromisoformat(manifest.submitted.replace("Z", "+00:00"))
        requested = datetime.fromisoformat(requested_at.replace("Z", "+00:00"))
        return submitted.replace(tzinfo=submitted.tzinfo or UTC) >= requested.replace(
            tzinfo=requested.tzinfo or UTC)
    except (TypeError, ValueError, AttributeError):
        return False


async def preview(subscriptions: list[dict], *, start: str | None = None,
                  end: str | None = None, purpose: str = "bootstrap",
                  identity: str = "persistent workload identity") -> dict:
    from . import focus_sync

    lo, hi = (start, end) if start and end else window()
    work = chunks(lo, hi)
    scope = [s["id"].lower() for s in subscriptions]
    found = await focus_sync.discover(scope)
    by_sub = {s["subscription_id"].lower(): s for s in found["schedules"]}
    errors = {s["subscription"].lower(): s["detail"] for s in found["errors"]}
    rows = []
    for sub in subscriptions:
        sid = sub["id"].lower()
        schedule = by_sub.get(sid)
        missing = []
        complete = []
        if sid in errors:
            missing.append({"capability": "exports/read", "detail": errors[sid]})
        elif not schedule:
            missing.append({"capability": "owned-export",
                            "detail": "Configure this deployment's FOCUS export in Cost exports first."})
        else:
            try:
                complete = await manifests(schedule)
            except Exception as exc:
                missing.append({"capability": "storage-read", "detail": str(exc)[:300]})
        saved = await asyncio.to_thread(coverage, sid, lo, hi)
        tasks = []
        for part in work:
            compatible = [m for m in complete if actual_window(m)[0] <= part["from"]
                          and actual_window(m)[1] >= part["to"]]
            newest = max(compatible, key=lambda m: (m.submitted, m.run_id), default=None)
            if purpose in {"reconcile", "verify", "daily"}:
                newest = None
            tasks.append({**part, "reuse_run": newest.run_id if newest else None,
                          "action": "validate and import existing report" if newest else "execute custom window"})
        rows.append({
            "subscription": sid, "name": sub.get("name") or sid,
            "tenant": sub.get("tenant_id") or sub.get("tenantId"),
            "destination": schedules.destination(sid), "export": schedule,
            "coverage": saved, "missing_capabilities": missing, "tasks": tasks,
        })
    result = {
        "purpose": purpose, "from": lo, "to": hi,
        "days": (date.fromisoformat(hi) - date.fromisoformat(lo)).days + 1,
        "subscriptions": rows, "identity": identity,
        "automatic_identity": "separately authorized persistent workload identity",
        "daily_mode": "native-month-to-date until custom coverage is verified and cadence change approved",
        "notes": ["No Quick data establishes FOCUS completeness.",
                  "Missing days are unknown, not zero; current UTC day is provisional.",
                  "Azure verifies export execution permission when submitted; listing access alone does not prove it.",
                  "No grants, provisioning, retention changes or export writes occur during preview."],
    }
    result["preview_token"] = hashlib.sha256(json.dumps(result, sort_keys=True).encode()).hexdigest()
    return result


async def create(previewed: dict, *, idempotency_key: str, identity: str = "interactive",
                 force_new: bool = False) -> dict:
    tasks = []
    for row in previewed["subscriptions"]:
        for part in row["tasks"]:
            tasks.append({"subscription": row["subscription"], "from": part["from"], "to": part["to"],
                          "schedule": row["export"], "force_new": force_new,
                          "missing_capabilities": row["missing_capabilities"]})
    return await asyncio.to_thread(
        jobs.create, "focus-" + previewed["purpose"],
        [r["subscription"] for r in previewed["subscriptions"]], tasks,
        idempotency_key=idempotency_key, identity=identity,
        payload={"from": previewed["from"], "to": previewed["to"],
                 "purpose": previewed["purpose"]})


def _error_chain(exc: BaseException):
    seen = set()
    while exc is not None and id(exc) not in seen:
        seen.add(id(exc))
        yield exc
        exc = exc.__cause__ or (None if exc.__suppress_context__ else exc.__context__)


def _http_failure(exc: BaseException) -> tuple[int | None, str | None]:
    for error in _error_chain(exc):
        response = getattr(error, "response", None)
        status = getattr(error, "status_code", None)
        if status is None:
            status = getattr(response, "status_code", None)
        if status is not None:
            retry = getattr(error, "retry_after", None)
            if retry is None:
                retry = (getattr(response, "headers", {}) or {}).get("Retry-After")
            return status, retry
    return None, None


def _submission_rejected(exc: BaseException) -> bool:
    status, _ = _http_failure(exc)
    # Timeout/gateway/internal failures may follow acceptance. A throttling or
    # explicit client rejection, unlike a lost response, permits a fresh request.
    return status == 503 or (status is not None and 400 <= status < 500 and status != 408)


def retry_delay(exc: Exception) -> int | None:
    import httpx

    status, retry = _http_failure(exc)
    errors = list(_error_chain(exc))
    if status is not None:
        transient = status in (408, 409, 429, 500, 502, 503, 504)
    elif any(isinstance(error, (httpx.TransportError, TimeoutError)) for error in errors):
        transient = True
    elif any(s in str(error) for error in errors for s in ("403", "401", "AuthorizationFailed")):
        return None
    else:
        transient = any(s in str(error).lower() for error in errors
            for s in ("429", "503", "502", "temporarily", "retry after", "no complete"))
    if transient:
        retry = retry or "60"
        try:
            return max(1, int(retry))
        except (ValueError, TypeError):
            from email.utils import parsedate_to_datetime
            try:
                return max(1, int((parsedate_to_datetime(retry) - datetime.now(UTC)).total_seconds()))
            except (ValueError, TypeError):
                return 60
    return None


def reserve_run(sub: str, job_id: str, task_id: str) -> bool:
    with jobs.transaction() as con:
        previous = jobs._read(con, "focus-pending:" + sub)
        if previous and (previous["job_id"], previous["task_id"]) != (job_id, task_id):
            return False
        jobs._put(con, "focus-pending:" + sub, {"job_id": job_id, "task_id": task_id})
        return True


def release_run(sub: str, job_id: str, task_id: str) -> None:
    with jobs.transaction() as con:
        previous = jobs._read(con, "focus-pending:" + sub)
        if previous and (previous["job_id"], previous["task_id"]) == (job_id, task_id):
            con.execute("DELETE FROM meta WHERE key = ?", ["focus-pending:" + sub])


async def step(job_id: str) -> dict:
    require_writable()
    from .exports import ingest_export
    from .warehouse import warehouse

    record = await asyncio.to_thread(jobs.get, job_id)
    busy_subs = set()
    for task in record["tasks"]:
        if task["status"] in {"ready", "failed"}:
            continue
        if task["subscription"] in busy_subs:
            continue
        if task.get("retry_at") and task["retry_at"] > jobs.now():
            busy_subs.add(task["subscription"])
            continue
        tid = task["task_id"]
        task_token = jobs.current_task.set(tid)

        async def save(task=task, tid=tid, **values):
            task.update(values)
            await asyncio.to_thread(jobs.update, job_id, task_id=tid, **values)

        try:
            if task.get("operation") == "configure":
                from . import cost_data
                await save(status="authenticating",
                           detail="Checking the approved workload identity and configuring owned exports.")
                await cost_data.configure(record, task)
                await save(status="ready", finished=jobs.now(), detail="Automatic updates configured.")
                continue
            if record["kind"] == "focus-setup":
                current_job = await asyncio.to_thread(jobs.get, job_id)
                prerequisite = next((t for t in current_job["tasks"] if
                                     t["subscription"] == task["subscription"] and
                                     t.get("operation") == "configure"), None)
                if prerequisite and prerequisite["status"] != "ready":
                    await save(status="failed" if prerequisite["status"] == "failed" else "queued",
                               detail="Waiting for automatic-update configuration; retry missing work after fixing access.")
                    continue
            if record["kind"] == "focus-update":
                from . import cost_data
                config = await asyncio.to_thread(cost_data.read, task["subscription"])
                if (config.get("version") != 2 or not config.get("enabled")
                        or config.get("update_job") != job_id
                        or record["payload"].get("setup_job", config.get("setup_job")) != config.get("setup_job")):
                    raise ValueError("Automatic-update authorization changed or this job is no longer attached.")
            if record["kind"] == "focus-daily":
                configured = await asyncio.to_thread(
                    warehouse.get_meta, "focus-daily:" + task["subscription"])
                config = json.loads(configured) if configured else {}
                if not config.get("enabled") or config.get("mode") != "compact":
                    raise ValueError("Compact workload authorization is no longer enabled for this subscription.")
            schedule = task["schedule"]
            # Recheck ownership/destination in Azure, rather than trusting a stale preview.
            from . import focus_sync
            current = await focus_sync.owned([task["subscription"]])
            expected = schedule["name"] if schedule else schedules.export_name(
                "FocusCost", **schedules.destination(task["subscription"]))
            schedule = next((s for s in current if s["name"] == expected), None)
            if not schedule:
                raise ValueError("The deployment-owned export changed; preview again.")
            runs = await manifests(schedule)
            candidates = runs
            if task.get("requested_at"):
                baseline = {tuple(item) for item in task.get("baseline", [])}
                candidates = [m for m in runs if (m.run_id, m.submitted) not in baseline
                             and submitted_after(m, task["requested_at"])]
            elif task.get("force_new"):
                candidates = []
            compatible = [m for m in candidates if actual_window(m)[0] <= task["from"]
                          and actual_window(m)[1] >= task["to"]]
            if (record["kind"] in {"focus-setup", "focus-update"} and task.get("requested_at")
                    and candidates and not compatible):
                await save(requested_at=None, rejected_coverage=[list(actual_window(m)) for m in candidates])
                raise ValueError("Azure returned a different coverage window. This provider may not support "
                                 "the requested historical period. Existing costs are retained; this window "
                                 "remains incomplete. Ask the administrator to check export support, then retry.")
            if record["kind"] == "focus-daily" and candidates and not compatible:
                config["enabled"] = False
                config["detail"] = "Azure returned incompatible custom coverage. Preview explicit native MTD fallback."
                await asyncio.to_thread(warehouse.set_meta, "focus-daily:" + task["subscription"],
                                        json.dumps(config))
                raise ValueError(config["detail"])
            chosen = max(compatible, key=lambda m: (m.submitted, m.run_id), default=None)
            if chosen is None:
                busy_subs.add(task["subscription"])
                if not task.get("requested_at"):
                    if not await asyncio.to_thread(reserve_run, task["subscription"], job_id, tid):
                        await save(status="queued", detail="Another job owns this subscription's Azure execution.")
                        continue
                    # Persist the submission intent first. A crash after acceptance must poll,
                    # not blindly submit again and create competing Azure runs.
                    await save(status="waiting-for-Azure", requested_at=jobs.now(),
                               baseline=[[m.run_id, m.submitted] for m in runs],
                               attempts=task["attempts"] + 1)
                    try:
                        await schedules.run_now(task["subscription"], schedule["name"],
                                                start=task["from"], end=task["to"])
                    except Exception as exc:
                        # Some providers reject custom execution periods. Only the current
                        # month can safely use the reviewed native MTD compatibility path.
                        status_code, _ = _http_failure(exc)
                        fallback = (record["kind"] in {"focus-setup", "focus-update"} and
                                    status_code in {400, 422} and
                                    schedule.get("timeframe") == "MonthToDate" and
                                    task["from"][:7] == window(1)[1][:7])
                        if fallback:
                            await save(compatibility="native-month-to-date")
                            try:
                                await schedules.run_now(task["subscription"], schedule["name"])
                            except Exception as fallback_error:
                                if _submission_rejected(fallback_error):
                                    await save(requested_at=None)
                                raise
                            continue
                        busy_subs.add(task["subscription"])
                        # Ambiguous transport failures may already have been accepted by Azure.
                        if _submission_rejected(exc):
                            await save(requested_at=None)
                            await asyncio.to_thread(release_run, task["subscription"], job_id, tid)
                        raise
                await save(status="waiting-for-Azure",
                           detail="Azure is still generating a complete report; this can take hours.")
                continue
            await save(status="downloading", run_id=chosen.run_id, submitted=chosen.submitted,
                       actual_window=list(actual_window(chosen)), native_timeframe=chosen.timeframe)
            exact = actual_window(chosen) == (task["from"], task["to"])
            if record["kind"] == "focus-daily" and not exact:
                config["enabled"] = False
                config["detail"] = "Azure returned wider-than-requested coverage. Preview explicit native MTD fallback."
                await asyncio.to_thread(warehouse.set_meta, "focus-daily:" + task["subscription"],
                                         json.dumps(config))
                raise ValueError(config["detail"])
            result = await ingest_export(
                account=schedule["account"], container=schedule["container"], prefix=schedule["folder"],
                export_name=schedule["name"], subscription_id=task["subscription"],
                scope=[task["subscription"]], metric="FocusCost", required_metric="FocusCost",
                run_identity=(chosen.run_id, chosen.submitted),
                requested_window=(task["from"], task["to"]),
            )
            result["compact_coverage_verified"] = bool(
                task.get("requested_at") and exact and date.fromisoformat(task["from"]).day != 1)
            if result["compact_coverage_verified"]:
                await asyncio.to_thread(warehouse.set_meta, "focus-compact-verified:" + task["subscription"],
                                         json.dumps({"run_id": chosen.run_id, "at": jobs.now(),
                                                     "from": task["from"], "to": task["to"]}))
            if record["kind"] in {"focus-setup", "focus-update"}:
                from . import cost_data
                config = await asyncio.to_thread(cost_data.read, task["subscription"])
                if config.get("version") == 2:
                    config["optimization"] = ("Verified exact custom window" if exact else
                                              "Wider native report compatibility; not a changed-byte export")
                    await asyncio.to_thread(cost_data.write, task["subscription"], config)
            await save(status="ready", result=result, finished=jobs.now(),
                       detail=("Returned coverage was wider; compact is not verified. Preview native MTD fallback."
                               if record["kind"] == "focus-verify" and not exact else
                               "A month-start window cannot prove custom capability; verify a window starting after day one."
                               if record["kind"] == "focus-verify" and not result["compact_coverage_verified"] else
                               "Complete manifest validated; requested days reconciled transactionally."))
            await asyncio.to_thread(release_run, task["subscription"], job_id, tid)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            delay = retry_delay(exc)
            await save(status="waiting-for-Azure" if delay else "failed", detail=str(exc)[:500],
                       retry_at=(datetime.now(UTC) + timedelta(seconds=delay)).isoformat() if delay else None)
            if not delay:
                await asyncio.to_thread(release_run, task["subscription"], job_id, tid)
        finally:
            jobs.current_task.reset(task_token)
    fresh = await asyncio.to_thread(jobs.get, job_id)
    states = [t["status"] for t in fresh["tasks"]]
    status = ("waiting-for-Azure" if any(s not in {"ready", "failed"} for s in states)
              else "partial" if "failed" in states and "ready" in states
              else "failed" if "failed" in states else "ready")
    from . import focus_sync
    for sub in fresh["scope"]:
        tasks = [t for t in fresh["tasks"] if t["subscription"] == sub]
        windows = [t for t in tasks if t.get("from")]
        ready = all(t["status"] == "ready" for t in tasks)
        failed = all(t["status"] == "failed" for t in tasks)
        previous_sync = await asyncio.to_thread(focus_sync._read, sub)
        completed = [t for t in windows if t["status"] == "ready"]
        through = max([previous_sync.get("through") or "",
                       *(t["to"] for t in completed)]) or None
        await focus_sync.record(
            sub, status="ready" if ready else "failed" if failed else
            "partial" if all(t["status"] in {"ready", "failed"} for t in tasks) else "waiting-for-Azure",
            started=job_id, job_id=job_id, finished=jobs.now() if ready or failed else None,
            through=through,
            last_ingested=(max(t.get("finished") or "" for t in completed) if completed else
                           previous_sync.get("last_ingested")),
            changed_days=sum(t.get("result", {}).get("changed_days", 0) for t in tasks),
            unchanged_days=sum(t.get("result", {}).get("unchanged_days", 0) for t in tasks),
            detail="Complete FOCUS windows reconciled." if ready else
            "Waiting for complete reports or authorized recovery; existing costs are retained.")
    return {"status": status, "job_id": job_id, "ready": states.count("ready"),
            "failed": states.count("failed"), "total": len(states)}


async def resume(job_id: str) -> dict:
    require_writable()
    record = await asyncio.to_thread(jobs.get, job_id)
    for task in record["tasks"]:
        if task["status"] == "failed":
            # Successful chunks and original submission baselines are immutable on retry.
            await asyncio.to_thread(jobs.update, job_id, task_id=task["task_id"],
                                    status="queued", retry_at=None, missing_capabilities=[])
    return await asyncio.to_thread(jobs.update, job_id, status="queued", finished=None)


def launch(record: dict) -> None:
    if read_only_mode():
        return
    jid = record["job_id"]
    if jid in _running or record["status"] in jobs.TERMINAL:
        return
    _running.add(jid)

    async def execute():
        try:
            while True:
                result = await jobs.run(jid, lambda: step(jid))
                if result["status"] in jobs.TERMINAL:
                    return result
                await asyncio.sleep(60)
        finally:
            _running.discard(jid)

    task = asyncio.create_task(execute())
    jobs._workers.add(task)
    def finished(done):
        jobs._workers.discard(done)
        if not done.cancelled():
            done.exception()
    task.add_done_callback(finished)


async def recover() -> None:
    """Only persisted workload-authorized jobs may run unattended after restart."""
    if read_only_mode():
        return
    records = await asyncio.to_thread(jobs.listing, active_only=True, limit=500)
    for record in records:
        if record["identity"] == "workload" and record["kind"] in {
                "focus-bootstrap", "focus-reconcile", "focus-daily", "focus-verify", "focus-refresh",
                "focus-setup", "focus-update"}:
            launch(record)


async def daily_preview(scope: list[str], mode: str, lookback: int = DEFAULT_LOOKBACK) -> dict:
    from . import focus_sync
    from .warehouse import warehouse

    if mode not in {"compact", "native-month-to-date"} or not 1 <= lookback <= 31:
        raise ValueError("Choose compact (1–31 recent days) or explicit native-month-to-date fallback.")
    rows = []
    owned = {s["subscription_id"].lower(): s for s in await focus_sync.owned(scope)}
    pending = await asyncio.to_thread(jobs.listing, scope, active_only=True, limit=500)
    for sub in scope:
        verified = await asyncio.to_thread(warehouse.get_meta, "focus-compact-verified:" + sub.lower())
        row = owned.get(sub.lower())
        missing = []
        if row:
            try:
                await manifests(row)
            except Exception as exc:
                missing.append({"capability": "workload-storage-read", "detail": str(exc)[:300]})
        rows.append({"subscription": sub.lower(), "export": row,
                     "custom_coverage_verified": bool(verified),
                     "missing_capabilities": missing,
                     "blocked": (not row or bool(missing) or (mode == "compact" and not verified) or any(
                         j["kind"] == "focus-daily" and sub.lower() in j["scope"] for j in pending)),
                     "action": "Pause native schedule; run bounded custom windows"
                               if mode == "compact" else "Enable native Daily month-to-date schedule"})
    result = {"mode": mode, "lookback": lookback, "subscriptions": rows,
              "identity": "persistent workload identity, explicitly authorized for selected scope",
              "periodic_reconciliation": "On the 7th of each month, reconcile the preceding 90 days",
              "retention": "Existing files retained; no lifecycle or external export changes"}
    result["preview_token"] = hashlib.sha256(json.dumps(result, sort_keys=True).encode()).hexdigest()
    return result


async def configure_daily(previewed: dict) -> dict:
    require_writable()
    from .warehouse import warehouse

    if any(row["blocked"] for row in previewed["subscriptions"]):
        raise ValueError("Every subscription needs an owned export; compact also needs verified custom coverage.")
    from . import cost_data
    for row in previewed["subscriptions"]:
        if (await asyncio.to_thread(cost_data.read, row["subscription"])).get("version") == 2:
            raise ValueError("Cost data owns this frequency. Use Review setup to change it.")
    outcomes = []
    for row in previewed["subscriptions"]:
        sub = row["subscription"]
        config = {"mode": previewed["mode"], "lookback": previewed["lookback"],
                  "identity": "workload", "enabled": False, "approved_at": jobs.now()}
        await asyncio.to_thread(warehouse.set_meta, "focus-daily:" + sub, json.dumps(config))
        try:
            await schedules.set_native_active(sub, active=previewed["mode"] != "compact",
                                               etag=row["export"].get("etag"))
            config["enabled"] = True
            config["detail"] = "Authorized workload schedule configured."
        except Exception as exc:
            config["detail"] = str(exc)[:400]
        await asyncio.to_thread(warehouse.set_meta, "focus-daily:" + sub, json.dumps(config))
        outcomes.append({"subscription": sub, **config})
    return {"subscriptions": outcomes}


async def daily_tick() -> set[str]:
    """Create at most one daily work item per approved scope/day, not one per process."""
    if read_only_mode():
        return set()
    from .warehouse import warehouse

    def configs():
        with warehouse.connect(read_only=True) as con:
            return [(key.split(":", 1)[1], json.loads(value)) for key, value in
                    con.execute("SELECT key,value FROM meta WHERE key LIKE 'focus-daily:%'").fetchall()]

    from . import cost_data
    saved_configs = await asyncio.to_thread(configs)
    compact = await cost_data.tick(saved_configs)
    for sub, config in saved_configs:
        if config.get("version") == 2 or sub in compact:
            continue
        if not config.get("enabled") or config.get("identity") != "workload":
            continue
        if config["mode"] != "compact":
            continue
        compact.add(sub)
        today = datetime.now(UTC).date()
        marker = f"focus-daily-job:{sub}:{today}"
        existing_id = await asyncio.to_thread(warehouse.get_meta, marker)
        if existing_id:
            existing = await asyncio.to_thread(jobs.get, existing_id)
            if existing:
                launch(existing)
                continue
        # A periodic wider run replaces, rather than duplicates, that day's compact run.
        days = 90 if today.day == 7 else config["lookback"]
        lo, hi = window(days)
        proposed = await preview([{"id": sub}], start=lo, end=hi, purpose="daily")
        record = await create(proposed, idempotency_key=f"daily:{sub}:{today}",
                              identity="workload", force_new=True)
        await asyncio.to_thread(warehouse.set_meta, marker, record["job_id"])
        launch(record)
    await recover()
    return compact
