"""One approved, durable cost-data pipeline; legacy cadence remains unchanged until approval."""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
from datetime import UTC, date, datetime, timedelta

from . import focus_workflow, jobs, schedules
from .job_policy import read_only_mode, require_writable

CADENCES = {
    "Daily": "Check daily at 02:00 UTC; refresh 7 recent days. After downtime, overlap the previous successful window. Starting the month after setup, reconcile 90 days for older corrections on the first monthly check on or after the 7th.",
    "Weekly": "Check Mondays at 02:00 UTC; refresh 14 recent days. After downtime, overlap the previous successful window. Starting the month after setup, reconcile 90 days for older corrections on the first monthly check on or after the 7th.",
    "Monthly": "Billing review on the first at 02:00 UTC; refresh the previous completed calendar month and the month before it for corrections. Costs can lag by up to a month.",
}


def read(sub: str) -> dict:
    from .warehouse import warehouse
    raw = warehouse.get_meta("focus-daily:" + sub.lower())
    return json.loads(raw) if raw else {}


def persistent_identity() -> bool:
    """Configuration evidence only; actual token, RBAC and storage checks still run."""
    return bool(os.getenv("AZURE_CLIENT_ID") or os.getenv("IDENTITY_ENDPOINT") or
                os.getenv("MSI_ENDPOINT") or os.getenv("AZURE_FEDERATED_TOKEN_FILE"))


def write(sub: str, config: dict) -> None:
    require_writable()
    with jobs.transaction() as con:
        jobs.assert_lease(con)
        current = jobs._read(con, "focus-daily:" + sub.lower()) or {}
        if current.get("revision", 0) != config.get("revision", 0):
            raise ValueError("The cost-data configuration changed concurrently; reload it before retrying.")
        saved = _put_config(con, sub, config)
    config["revision"] = saved["revision"]


def _put_config(con, sub: str, config: dict) -> dict:
    saved = {**config, "revision": config.get("revision", 0) + 1}
    jobs._put(con, "focus-daily:" + sub.lower(), saved)
    return saved


def next_correction(today: date) -> str:
    return ((today.replace(day=28) + timedelta(days=4)).replace(day=7)).isoformat()


def boundary(cadence: str, now: datetime) -> datetime:
    """Most recent due boundary, including the 02:00 UTC cutover."""
    clock = now.astimezone(UTC)
    day = clock.date() - timedelta(days=clock.hour < 2)
    if cadence == "Weekly":
        day -= timedelta(days=day.weekday())
    elif cadence == "Monthly":
        day = day.replace(day=1)
    elif cadence != "Daily":
        raise ValueError("Choose Daily, Weekly or Monthly.")
    return datetime.combine(day, datetime.min.time(), UTC).replace(hour=2)


def next_check(cadence: str, now: datetime) -> str:
    due = boundary(cadence, now)
    if cadence == "Monthly":
        due = (due.replace(day=28) + timedelta(days=4)).replace(day=1)
    else:
        due += timedelta(days=7 if cadence == "Weekly" else 1)
    return due.isoformat()


def update_window(cadence: str, today: date, last_through: str | None = None, *,
                  catchup_from: str | None = None, reconcile: bool = False) -> tuple[str, str]:
    if cadence not in CADENCES:
        raise ValueError("Choose Daily, Weekly or Monthly.")
    if cadence == "Monthly":
        hi = today.replace(day=1) - timedelta(days=1)
        lo = (hi.replace(day=1) - timedelta(days=1)).replace(day=1)
    else:
        hi = today
        lo = today - timedelta(days=13 if cadence == "Weekly" else 6)
    if last_through:
        watermark = date.fromisoformat(last_through)
        if watermark < lo:
            lo = ((watermark.replace(day=1) - timedelta(days=1)).replace(day=1)
                  if cadence == "Monthly" else watermark - timedelta(days=13 if cadence == "Weekly" else 6))
    elif catchup_from:
        lo = min(lo, date.fromisoformat(catchup_from))
    if reconcile and cadence != "Monthly":
        lo = min(lo, today - timedelta(days=89))
    # Catch up in bounded batches; the next due item continues from the successful watermark.
    if (hi - lo).days >= 366:
        hi = lo + timedelta(days=365)
    return lo.isoformat(), hi.isoformat()


def status(subscriptions: list[dict]) -> dict:
    """Database-only: never enumerate Azure exports on the initial render."""
    from . import archive, focus_sync
    rows = []
    now = datetime.now(UTC)
    recent = jobs.listing(limit=500)
    for sub in subscriptions:
        sid = sub["id"].lower()
        config = read(sid)
        history = config.get("history") or dict(zip(("from", "to"), focus_workflow.window()))
        coverage = focus_workflow.coverage(sid, history["from"], history["to"])
        sync = focus_sync._read(sid)
        configured = config.get("version") == 2 and bool(config.get("enabled"))
        active = jobs.get(config["setup_job"]) if config.get("setup_job") else None
        update = jobs.get(config["update_job"]) if config.get("update_job") else None
        latest = max((j for j in recent if sid in j["scope"] and
                      j["kind"] in {"focus-setup", "focus-update"}),
                     key=lambda j: j["created_at"], default=None)
        job = update if update and update["status"] in {"failed", "partial"} else latest or active
        retry_candidates = []
        for candidate in recent:
            if sid not in candidate["scope"] or candidate["kind"] not in {"focus-setup", "focus-update"}:
                continue
            own_tasks = [t for t in candidate["tasks"] if t["subscription"] == sid]
            if candidate["status"] != "failed" and not any(t["status"] == "failed" for t in own_tasks):
                continue
            if candidate["kind"] == "focus-update" and config.get("update_job") != candidate["job_id"]:
                continue
            if (config.get("setup_job") and config["setup_job"] != candidate["job_id"] and any(
                    t.get("operation") == "configure" and t["status"] != "ready" for t in own_tasks)):
                continue
            if candidate["payload"].get("action") == "setup" and not coverage["missing_days"] and configured:
                continue
            retry_candidates.append(candidate)
        retry = max(retry_candidates, key=lambda j: j["created_at"], default=None)
        tasks = [t for t in (job or {}).get("tasks", []) if t["subscription"] == sid]
        states = {t["status"] for t in tasks}
        state = (job["status"] if job and job["status"] != "ready" else sync["status"])
        if states == {"ready"}:
            state = ("ready" if all(t.get("operation") == "configure" for t in tasks)
                     else "partial" if coverage["missing_days"] else "ready")
        elif states and states.issubset({"ready", "failed"}):
            state = "partial" if "ready" in states else "failed"
        elif "authenticating" in states:
            state = "authenticating"
        elif "downloading" in states:
            state = "importing"
        rows.append({
            "id": sid, "name": sub.get("name") or sid, "configured": configured,
            "config": config, "history": history, "coverage": coverage,
            "status": state,
            "job_id": job["job_id"] if job else None,
            "retry_job_id": retry["job_id"] if retry else None,
            "through": sync.get("through"), "last_success": sync.get("last_ingested"),
            "next_check": (config.get("next_check") if configured and config.get("enabled") else None),
            "detail": config.get("detail") or sync.get("detail"),
            "tasks": tasks,
        })
    return {
        "subscriptions": rows, "cadences": CADENCES,
        "setup_needed": not rows or any(not r["configured"] or r["coverage"]["missing_days"] for r in rows),
        "auto_enabled": archive.enabled() and os.getenv("AUTO_FOCUS_SYNC", "true").lower() != "false",
        "checked_at": now.isoformat(),
        "note": "Existing costs stay available. Quick estimates do not establish FOCUS coverage. Azure generation can take hours; the backend and database must remain running for scheduled checks.",
    }


def history_window(start: str | None = None, end: str | None = None,
                   history_days: int | None = None) -> tuple[str, str, int]:
    """Validate the customer import horizon, independently of internal catch-up batches."""
    if history_days is not None and (type(history_days) is not int or not 1 <= history_days <= 365):
        raise ValueError("Choose 1–365 whole days of history (1 year is 365 days).")
    if (start is None) != (end is None):
        raise ValueError("Supply both history dates.")
    today = datetime.now(UTC).date()
    if start is None:
        count = history_days if history_days is not None else 90
        return (today - timedelta(days=count - 1)).isoformat(), today.isoformat(), count
    try:
        if not all(isinstance(s, str) and re.fullmatch(r"\d{4}-\d{2}-\d{2}", s) for s in (start, end)):
            raise ValueError
        lo, hi = date.fromisoformat(start), date.fromisoformat(end)
    except ValueError as exc:
        raise ValueError("History dates must be valid YYYY-MM-DD UTC dates.") from exc
    count = (hi - lo).days + 1
    if not 1 <= count <= 365 or hi > today:
        raise ValueError("Import history must cover 1–365 inclusive days, with no future dates.")
    if history_days is not None and count != history_days:
        raise ValueError("History length conflicts with the selected dates.")
    return lo.isoformat(), hi.isoformat(), count


async def preview(subscriptions: list[dict], *, cadence: str = "Daily",
                  start: str | None = None, end: str | None = None, action: str = "setup",
                  history_days: int | None = None) -> dict:
    from . import archive, focus_sync
    if cadence not in CADENCES or action not in {"setup", "schedule", "frequency", "refresh", "reconcile"}:
        raise ValueError("Invalid cost-data setup action or frequency.")
    if not subscriptions:
        raise ValueError("Choose at least one subscription.")
    if action == "setup":
        start, end, history_days = history_window(start, end, history_days)
    elif history_days is not None:
        raise ValueError("History length is only used for first-time import, not frequency changes.")
    if action == "schedule" and (start is not None or end is not None):
        raise ValueError("Schedule-only approval does not accept history dates. Use First-time import for history.")
    if bool(start) != bool(end):
        raise ValueError("Supply both history dates.")
    lo, hi = (None, None) if action == "schedule" else (start, end) if start and end else focus_workflow.window()
    if action != "schedule":
        focus_workflow.chunks(lo, hi)
    scope = [s["id"].lower() for s in subscriptions]
    discovered = await focus_sync.discover(scope)
    found = {r["subscription_id"].lower(): r for r in discovered["schedules"]}
    errors = {r["subscription"].lower(): r["detail"] for r in discovered.get("errors", [])}
    pending = await asyncio.to_thread(jobs.listing, None, active_only=True, limit=500)
    rows = []
    for sub in subscriptions:
        sid = sub["id"].lower()
        config = await asyncio.to_thread(read, sid)
        row, blockers = found.get(sid), []
        if sid in errors:
            blockers.append("Workload identity cannot read exports on this subscription: " + errors[sid])
        if not archive.enabled():
            blockers.append("Deployment storage is not configured. Ask the deployment administrator to connect it.")
        if not persistent_identity():
            blockers.append("No persistent workload identity is configured. Interactive sign-in alone cannot run after restart; ask the deployment administrator to configure a managed identity.")
        if os.getenv("AUTO_FOCUS_SYNC", "true").lower() == "false":
            blockers.append("Automatic FOCUS checks are disabled on this backend. The deployment administrator must enable them before setup.")
        if any(sid in j["scope"] for j in pending):
            blockers.append("A cost-data job is already pending. Check progress or retry that job first.")
        if row and not row.get("etag"):
            blockers.append("Azure did not return the export version. Reload this review before changing it.")
        if row:
            try:
                await focus_workflow.manifests(row)
            except Exception as exc:
                blockers.append("Could not read destination reports using the workload identity: " + str(exc)[:250])
        if action not in {"setup", "schedule"} and config.get("version") != 2:
            blockers.append("Review initial setup before changing frequency or refreshing this pipeline.")
        previous_update = (await asyncio.to_thread(jobs.get, config["update_job"])
                           if config.get("update_job") else None)
        if action in {"schedule", "frequency", "refresh", "reconcile"} and previous_update and previous_update["status"] in {"failed", "partial"}:
            blockers.append("An automatic update has missing windows. Use Retry missing work first so its catch-up position is preserved.")
        bounds = ((lo, hi) if action != "refresh" or config.get("version") != 2 else
                  update_window(config["cadence"], datetime.now(UTC).date(), config.get("last_through"),
                                catchup_from=config.get("catchup_anchor") or (config.get("history") or {}).get("from")))
        rows.append({
            "subscription": sid, "name": sub.get("name") or sid, "export": row,
            "destination": schedules.destination(sid), "blockers": blockers,
            "config": config,
            "window": {"from": bounds[0], "to": bounds[1]} if action not in {"schedule", "frequency"} else None,
            "permissions": [
                {"principal": "Application workload identity", "scope": f"/subscriptions/{sid}",
                 "requirement": "Cost Management Contributor (read, write and execute exports)",
                 "evidence": "Read observed; writes verified only when submitted" if row else "Not yet verified"},
                {"principal": "Application workload identity", "scope": schedules.destination(sid),
                 "requirement": "Storage Blob Data Reader", "evidence": "Report-read check only"},
                {"principal": "Export managed identity", "scope": schedules.destination(sid),
                 "requirement": "Storage Blob Data Contributor on the destination container",
                 "evidence": "Azure validates delivery; interactive SSO roles are not evidence"},
            ],
            "action": (f"Refresh {config.get('cadence', cadence)} coverage; do not change frequency"
                       if action == "refresh" else "Reconcile the reviewed history; do not change frequency"
                       if action == "reconcile" else
                       "Reuse owned export and pause its native recurrence" if row else
                       "Create the deployment-owned FOCUS export and its managed-identity container access"),
        })
    result = {"action": action, "cadence": cadence, "from": lo, "to": hi, "history_days": history_days,
              "subscriptions": rows,
              "identity": "Persistent application workload identity (not your interactive sign-in)",
              "explanation": ("Refresh now uses each subscription's saved update cadence without changing it."
                              if action == "refresh" else CADENCES[cadence]),
              "notes": [
                  ("This approval enables the selected automatic frequency without starting a historical import. Existing history and catch-up progress are preserved; use First-time import to request older data."
                   if action == "schedule" else
                   "One approval authorizes history, automatic updates and internal coverage checks. Browser closure is safe; the backend and database must remain running."),
                  "Only deployment-owned exports are changed. External and legacy exports are untouched; reports are retained and storage charges grow.",
                  "Read checks are advisory, not proof of write permissions. Azure verifies export execution and storage access when work starts.",
                  "For a missing export, only its managed identity's Storage Blob Data Contributor grant on the destination container is attempted. No Owner or User Access Administrator roles are granted.",
                  "Exact windows are used when Azure supports them. Wider month-to-date reports remain a compatibility mode, not changed-byte exports. Unsupported historical windows stay incomplete.",
              ]}
    # Coverage, report arrivals and descriptive errors must not make an otherwise unchanged
    # approval expire. Export ETags, destination, identity, scope and saved config must.
    contract = {k: result[k] for k in ("action", "cadence", "from", "to", "history_days", "identity")}
    contract["subscriptions"] = [{"id": r["subscription"], "destination": r["destination"],
        "export": {k: (r["export"] or {}).get(k) for k in ("name", "etag")},
        "config": r["config"], "window": r["window"]} for r in rows]
    result["preview_token"] = hashlib.sha256(json.dumps(contract, sort_keys=True).encode()).hexdigest()
    return result


async def create(plan: dict, idempotency_key: str) -> dict:
    require_writable()
    if not plan["subscriptions"]:
        raise ValueError("Choose at least one subscription.")
    if plan["action"] == "setup":
        lo, hi, count = history_window(plan["from"], plan["to"], plan.get("history_days"))
        if any(row["window"] != {"from": lo, "to": hi} for row in plan["subscriptions"]):
            raise ValueError("The import windows do not match the reviewed history.")
        plan = {**plan, "history_days": count}
    if any(row["blockers"] for row in plan["subscriptions"]):
        raise ValueError("Resolve the subscription blockers, then review setup again.")
    tasks = []
    for row in plan["subscriptions"]:
        sid = row["subscription"]
        if plan["action"] in {"setup", "schedule", "frequency"}:
            tasks.append({"subscription": sid, "operation": "configure", "approved": row})
        if plan["action"] not in {"schedule", "frequency"}:
            lo, hi = row["window"]["from"], row["window"]["to"]
            tasks.extend({"subscription": sid, **part, "schedule": row["export"],
                          "force_new": plan["action"] != "setup"}
                         for part in focus_workflow.chunks(lo, hi))
    return await asyncio.to_thread(
        jobs.create, "focus-setup", [r["subscription"] for r in plan["subscriptions"]], tasks,
        idempotency_key=idempotency_key, identity="workload",
        payload={k: plan.get(k) for k in ("action", "cadence", "from", "to", "history_days", "preview_token")})


async def configure(record: dict, task: dict) -> None:
    """Run under the shared durable lease. Persist ownership before the Azure transition."""
    require_writable()
    from . import focus_sync
    sid, approved = task["subscription"], task["approved"]
    config = await asyncio.to_thread(read, sid)
    if config.get("approved_at", "") > record["created_at"]:
        raise ValueError("A newer approved setup superseded this configuration. Review the current setup instead.")
    if config.get("setup_job") != record["job_id"] and config != approved["config"]:
        raise ValueError("The saved update configuration changed after review. Review the current setup again.")
    if config.get("setup_job") != record["job_id"]:
        history = (approved["config"].get("history") if record["payload"]["action"] in {"schedule", "frequency"}
                   else {k: record["payload"][k] for k in ("from", "to")})
        if not history and config.get("setup_job"):
            initial = await asyncio.to_thread(jobs.get, config["setup_job"])
            if initial and initial["payload"].get("action") == "setup":
                history = {"from": initial["payload"].get("from") or initial["payload"]["to"],
                           "to": initial["payload"]["to"]}
        config = {"version": 2, "cadence": record["payload"]["cadence"], "mode": "managed",
                  "identity": "workload", "enabled": False, "setup_job": record["job_id"],
                  "revision": config.get("revision", 0), "history": history,
                  "catchup_anchor": config.get("catchup_anchor") or (history or {}).get("from"),
                  "correction_due": config.get("correction_due") or next_correction(datetime.now(UTC).date()),
                  "approved_at": record["created_at"],
                  "last_through": approved["config"].get("last_through"),
                  "detail": "Configuring authorized cost-data updates."}
        await asyncio.to_thread(write, sid, config)
    current = next(iter(await focus_sync.owned([sid])), None)
    expected = approved["export"]
    if current is None and expected:
        raise ValueError("The reviewed owned export was deleted. Review setup again.")
    if expected is None and not task.get("provisioned"):
        if current and not task.get("provisioning_started"):
            raise ValueError("An export appeared after review. Review setup again before changing it.")
        await asyncio.to_thread(jobs.update, record["job_id"], task_id=task["task_id"],
                                provisioning_started=True)
        await schedules.create(sid, "FocusCost", **approved["destination"], run_now=False, native_active=False)
        current = next(iter(await focus_sync.owned([sid])), None)
        if not current:
            raise ValueError("Created export cannot yet be read. Retry missing work after permissions propagate.")
        await asyncio.to_thread(jobs.update, record["job_id"], task_id=task["task_id"], provisioned=True)
    if expected and current["name"] != expected["name"]:
        raise ValueError("The reviewed export changed. Review setup again.")
    if expected and current.get("etag") != expected.get("etag") and not task.get("pause_started"):
        raise ValueError("The owned export changed after review. Review setup again.")
    if current["status"] != "Inactive":
        # Retry after a confirmed pause is safe; a different active ETag is not.
        etag = expected.get("etag") if expected else current.get("etag")
        await asyncio.to_thread(jobs.update, record["job_id"], task_id=task["task_id"], pause_started=True)
        await schedules.set_native_active(sid, active=False, etag=etag)
    config.update(enabled=True, next_check=next_check(config["cadence"], datetime.now(UTC)),
                  detail="Automatic updates configured. Initial coverage is tracked separately.",
                  export=current["name"], destination=approved["destination"],
                  format=current.get("format"), optimization="Awaiting returned-window evidence")
    await asyncio.to_thread(write, sid, config)


def _complete_coverage(con, sub: str, start: str, end: str) -> bool:
    lo, hi = date.fromisoformat(start), date.fromisoformat(end)
    if lo > hi:
        return False
    prefix = f"focus-day:{sub.lower()}:"
    keys = {row[0] for row in con.execute("SELECT key FROM meta WHERE key >= ? AND key <= ?",
                                        [prefix + start, prefix + end]).fetchall()}
    return all(prefix + (lo + timedelta(days=i)).isoformat() in keys
               for i in range((hi - lo).days + 1))


def _prepare_tick(sub: str, now: datetime) -> tuple[dict, dict | None]:
    """Settle only attached, authoritative work and obtain a fresh revision under one lock."""
    require_writable()
    with jobs.transaction() as con:
        config = jobs._read(con, "focus-daily:" + sub.lower()) or {}
        if config.get("version") != 2 or not config.get("enabled") or config.get("identity") != "workload":
            return config, None
        original = dict(config)
        previous = jobs._read(con, "job:" + config["update_job"]) if config.get("update_job") else None
        if previous:
            if previous["payload"].get("setup_job", config.get("setup_job")) != config.get("setup_job"):
                return config, None
            if previous["status"] not in jobs.TERMINAL:
                return config, previous
            if previous["status"] != "ready":
                return config, None
            tasks = [t for t in previous["tasks"] if t["subscription"] == sub]
            complete = (bool(tasks) and all(t["status"] == "ready" for t in tasks) and
                        _complete_coverage(con, sub, previous["payload"]["from"], previous["payload"]["to"]))
            if not complete:
                previous.update(status="partial", detail="Authoritative window receipts are incomplete; retry missing work.")
                for task in tasks:
                    if task["status"] != "ready" or not _complete_coverage(con, sub, task["from"], task["to"]):
                        task.update(status="failed", detail=previous["detail"])
                if not any(t["status"] == "ready" for t in tasks):
                    previous["status"] = "failed"
                jobs._put(con, "job:" + previous["job_id"], previous)
                return config, None
            config["last_through"] = max(config.get("last_through") or "", previous["payload"]["to"])
            if previous["payload"].get("reconcile_due"):
                config["correction_due"] = next_correction(date.fromisoformat(previous["payload"]["to"]))
            config.pop("update_job", None)
        elif config.get("update_job"):
            # An attached job disappearing is not evidence that its dates were imported.
            return config, None
        history = config.get("history") or {}
        if not history and config.get("setup_job"):
            initial = jobs._read(con, "job:" + config["setup_job"])
            if initial and initial["payload"].get("action") == "setup":
                history = {"from": initial["payload"].get("from") or initial["payload"]["to"],
                           "to": initial["payload"]["to"]}
        if history:
            if not config.get("catchup_anchor"):
                config["catchup_anchor"] = history["from"]
            if not config.get("last_through") and _complete_coverage(con, sub, history["from"], history["to"]):
                config["last_through"] = history["to"]
        if not config.get("correction_due"):
            config["correction_due"] = next_correction(now.date())
        if config != original:
            config = _put_config(con, sub, config)
        return config, None


def _attach_update(sub: str, expected: dict, plan: dict, now: datetime, *, reconcile_due: str | None) -> dict | None:
    """CAS configuration and recheck pending work while creating AND attaching the job."""
    require_writable()
    with jobs.transaction() as con:
        current = jobs._read(con, "focus-daily:" + sub.lower()) or {}
        if (current != expected or current.get("version") != 2 or current.get("identity") != "workload"
                or not current.get("enabled") or current.get("update_job")):
            return None
        for (raw,) in con.execute("SELECT value FROM meta WHERE key LIKE 'job:%'").fetchall():
            pending = json.loads(raw)
            if pending["status"] in jobs.ACTIVE and sub in pending["scope"]:
                return None
        tasks = [{"subscription": sub, "from": part["from"], "to": part["to"],
                  "schedule": row["export"], "force_new": True,
                  "missing_capabilities": row["missing_capabilities"]}
                 for row in plan["subscriptions"] for part in row["tasks"]]
        record = jobs.create_in_transaction(
            con, "focus-update", [sub], tasks, identity="workload",
            idempotency_key=f"update:{sub}:{expected.get('revision', 0)}:{boundary(current['cadence'], now).isoformat()}",
            payload={"from": plan["from"], "to": plan["to"], "purpose": "update",
                     "setup_job": current["setup_job"], "reconcile_due": reconcile_due})
        desired_end = update_window(current["cadence"], now.date())[1]
        changed = {**current, "update_job": record["job_id"],
                   "next_check": now.isoformat() if plan["to"] < desired_end else next_check(current["cadence"], now)}
        _put_config(con, sub, changed)
        return record


def _record_preflight_failure(sub: str, expected: dict, detail: str) -> None:
    require_writable()
    with jobs.transaction() as con:
        current = jobs._read(con, "focus-daily:" + sub.lower()) or {}
        if current == expected:
            _put_config(con, sub, {**current, "detail": detail})


async def tick(configs: list[tuple[str, dict]]) -> set[str]:
    """Called by the existing FOCUS loop, never a second independent scheduler."""
    if read_only_mode():
        return set()
    owned = {sub for sub, cfg in configs if cfg.get("version") == 2}
    now = datetime.now(UTC)
    pending = await asyncio.to_thread(jobs.listing, active_only=True, limit=500)
    owned.update(sub for job in pending if job["kind"] == "focus-setup" for sub in job["scope"])
    for sub, _ in configs:
        config, running = await asyncio.to_thread(_prepare_tick, sub, now)
        if config.get("version") != 2 or not config.get("enabled") or config.get("identity") != "workload":
            continue
        owned.add(sub)
        if running:
            focus_workflow.launch(running)
            continue
        if config.get("update_job"):
            continue
        if config.get("next_check", "") > now.isoformat():
            continue
        if any(sub in job["scope"] for job in pending):
            continue
        reconcile = config["cadence"] != "Monthly" and config["correction_due"] <= now.date().isoformat()
        lo, hi = update_window(config["cadence"], now.date(), config.get("last_through"),
                               catchup_from=config.get("catchup_anchor"), reconcile=reconcile)
        try:
            plan = await focus_workflow.preview([{"id": sub}], start=lo, end=hi, purpose="update")
        except Exception as exc:
            await asyncio.to_thread(_record_preflight_failure, sub, config,
                                    "Automatic update preflight needs attention: " + str(exc)[:300])
            continue
        record = await asyncio.to_thread(
            _attach_update, sub, config, plan, now,
            reconcile_due=config["correction_due"] if reconcile and hi == now.date().isoformat() else None)
        if record:
            focus_workflow.launch(record)
    return owned
