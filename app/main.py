"""CloudLens — a small web app that answers cost questions about your subscriptions."""
from __future__ import annotations

import asyncio
import base64
import functools
import hashlib
import json
import logging
import math
import os
import re
import secrets
import time
from collections import OrderedDict
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any, Literal
from urllib.parse import quote

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import (
    JSONResponse,
    RedirectResponse,
    Response,
    StreamingResponse,
)
from pydantic import BaseModel, Field
from starlette.concurrency import run_in_threadpool
from starlette.middleware.base import BaseHTTPMiddleware

# Tests override this path to avoid loading a developer's local configuration.
try:
    from dotenv import load_dotenv

    load_dotenv(os.getenv("CLOUDLENS_ENV_FILE")
                or Path(__file__).resolve().parents[1] / ".env")
except ImportError:  # pragma: no cover - env vars can always be exported instead
    pass

from .agent import CostAgent
from .assets import PrecompressedStatic, stamp_asset_urls
from .auth import COOKIE, Auth, User
from .canonical import CanonicalOriginMiddleware, validate_public_base_url
from .entra import needs_admin_consent as entra_needs_consent
from .entra import normalise_email as entra_email
from .entra import sign as entra_sign
from .snapshot_cache import (
    WARM_SCOPE_KEY,
    CacheContext,
    SnapshotCache,
    SnapshotStore,
    application_version,
    inner_cache_key,
    install_snapshot_routes,
)
from .snapshot_warmer import SnapshotWarmer
from .store import PoolBusy

logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"))
log = logging.getLogger("cloudlens")

WEB = Path(__file__).resolve().parents[1] / "web"
_static_assets = PrecompressedStatic(directory=WEB / "assets")
_snapshot_cache: SnapshotCache | None = None
_snapshot_warmer: SnapshotWarmer | None = None


def read_only_mode() -> bool:
    from .job_policy import read_only_mode as enabled
    return enabled()


@functools.lru_cache(maxsize=1)
def _content_security_policy() -> str:
    """A policy that actually restricts script execution, rather than only framing.

    This was `frame-ancestors 'none'` alone, which stops the page being embedded and does
    nothing else — no `default-src`, no `script-src`, so it offered no second line behind the
    escaping in the frontend. That matters here more than in most applications: resource
    names, resource group names and tag values are all chosen by whoever created the resource,
    and they are rendered in this dashboard by somebody else.

    The inline `<script>` in the shipped HTML sets the theme before first paint, so it cannot
    become an external file without a flash of the wrong one on every load. It is allowed by
    hash rather than by `'unsafe-inline'`, and the hash is computed from the file at startup
    rather than pasted in — a policy that has to be updated by hand when a script changes is
    one that eventually gets `'unsafe-inline'` added to make the page work again.
    """
    inline = re.compile(r"<script(?![^>]*\ssrc=)[^>]*>(.*?)</script>", re.S | re.I)
    hashes: set[str] = set()
    for page in ("index.html", "login.html"):
        try:
            body = (WEB / page).read_text(encoding="utf-8")
        except OSError:  # a partial checkout must not stop the app from starting
            continue
        for block in inline.findall(body):
            digest = hashlib.sha256(block.encode("utf-8")).digest()
            hashes.add(f"'sha256-{base64.b64encode(digest).decode()}'")

    script = " ".join(["'self'", *sorted(hashes)])
    return "; ".join([
        "default-src 'self'",
        f"script-src {script}",
        # The theme system and the charts set element styles from JavaScript.
        "style-src 'self' 'unsafe-inline'",
        "img-src 'self' data:",
        "font-src 'self'",
        # Everything the page talks to is this origin. No CDN, no telemetry beacon.
        "connect-src 'self'",
        "object-src 'none'",
        "base-uri 'none'",
        "form-action 'self'",
        "frame-ancestors 'none'",
    ])
_agent: CostAgent | None = None
_auth: Auth | None = None

# The header strip is expensive to compute (a dozen-plus Cost Management calls across every
# subscription, each of which can be throttled). Computing it per request made the page take two
# minutes to become useful. Instead it is refreshed on a timer and served from this snapshot, so
# the endpoint always answers instantly.
_overview: dict = {"status": "loading"}
_refresh_seconds = float(os.getenv("OVERVIEW_REFRESH_SECONDS", "600"))
_ingest_task: asyncio.Task | None = None
_ingest_start_lock = asyncio.Lock()


@dataclass
class _SavingsState:
    partial: dict[str, Any]
    scan: dict[str, Any]
    result: dict[str, Any] | None = None
    generated_at: str | None = None
    generated_time: float | None = None
    finished_at: float | None = None
    auth_error: HTTPException | None = None


_savings_cache: OrderedDict[tuple[Any, ...], _SavingsState] = OrderedDict()
_savings_pending: dict[tuple[Any, ...], asyncio.Task] = {}
_SAVINGS_CACHE_SECONDS = 60.0
_SAVINGS_CACHE_LIMIT = 32
_SAVINGS_PENDING_LIMIT = 16
_SAVINGS_SCAN_TIMEOUT_SECONDS = 180.0
_billing_cache: OrderedDict[tuple[Any, ...], tuple[float, dict]] = OrderedDict()
_billing_pending: dict[tuple[Any, ...], asyncio.Task] = {}
_BILLING_CACHE_SECONDS = 60.0
_BILLING_CACHE_LIMIT = 32
_BILLING_PENDING_LIMIT = 16
_BILLING_SCAN_TIMEOUT_SECONDS = 180.0


def _savings_failed(state: _SavingsState, message: str) -> None:
    from .savings import SCAN_SOURCES

    errors = dict(state.scan["errors"])
    unfinished = [name for name in SCAN_SOURCES if name not in state.scan["completed"]]
    for name in unfinished or SCAN_SOURCES:
        errors.setdefault(name, message)
    state.scan = {
        "status": "error", "completed": [*state.scan["completed"], *unfinished],
        "total": len(SCAN_SOURCES), "errors": errors, "revision": state.scan["revision"] + 1,
    }
    state.partial = {**state.partial, "unavailable": errors}
    state.finished_at = asyncio.get_running_loop().time()


def _finish_savings(key: tuple[Any, ...], task: asyncio.Task) -> None:
    owns_scan = _savings_pending.get(key) is task
    if owns_scan:
        _savings_pending.pop(key, None)
    try:
        task.result()
    except (Exception, asyncio.CancelledError) as exc:
        state = _savings_cache.get(key) if owns_scan else None
        if state and state.scan["status"] == "running":
            message = ("The scan was cancelled." if task.cancelled()
                       else (str(exc) or type(exc).__name__)[:200])
            _savings_failed(state, message)


async def _refresh_overview() -> None:
    from . import cost

    while True:
        try:
            snapshot = await cost.overview()
            snapshot["status"] = "ok"
            globals()["_overview"] = snapshot
            log.info("overview refreshed: MTD=%s last=%s",
                     snapshot.get("month_to_date"), snapshot.get("last_month"))
        except Exception as exc:  # noqa: BLE001 - a failed refresh must not kill the loop
            log.warning("overview refresh failed: %s", exc)
            if _overview.get("status") == "loading":
                globals()["_overview"] = {"status": "error", "detail": str(exc)[:200]}
        await asyncio.sleep(_refresh_seconds)


async def _daily_refresh() -> None:
    """Reload the warehouse once a day, so nobody waits for it.

    Every route to fresh cost data is slow in its own way: a detail report is ten minutes of
    Azure building it, and the Query API is rate-limited hard enough that a busy day still
    takes minutes. Neither is a thing to do while someone watches. But none of it needs
    watching — the data changes once a day, overnight, when Azure writes the exports.

    So this runs on its own, at an hour after those exports land, and the person who opens the
    dashboard in the morning finds the work already done. It prefers the export path for the
    same reason the menu now lists it first: it is a blob read, so it is both the fastest and
    the richest evidence. Missing reports fall back to Query estimates without replacing
    richer days; Cost Details Actual/Amortized reports are never a silent FOCUS substitute.
    """
    global _ingest_task
    if read_only_mode():
        return
    from .warehouse import warehouse

    hour = max(0, min(int(os.getenv("AUTO_REFRESH_HOUR", "6")), 23))
    if os.getenv("AUTO_REFRESH", "true").lower() == "false":
        log.info("daily refresh disabled by AUTO_REFRESH")
        return
    from . import archive
    if os.getenv("AUTO_FOCUS_SYNC", "true").lower() != "false" and archive.enabled():
        # The durable FOCUS pipeline owns routine work; do not also reload three Quick months.
        return

    while True:
        now = datetime.now(UTC)
        nxt = now.replace(hour=hour, minute=0, second=0, microsecond=0)
        if nxt <= now:
            nxt += timedelta(days=1)
        await asyncio.sleep((nxt - now).total_seconds())

        try:
            # Never fight a refresh someone is watching: theirs is the one with a person
            # attached to it, and two ingests would race for the same periods.
            if _ingest_task and not _ingest_task.done():
                log.info("daily refresh skipped: an ingest is already running")
                continue

            log.info("daily refresh starting")
            sas = os.getenv("COST_EXPORT_SAS_URL", "").strip()
            if sas:
                task = await _start_ingest_task(functools.partial(
                    _run_ingest, int(os.getenv("INGEST_MONTHS", "3"))),
                    kind="automatic", identity="workload")
                if task is None:
                    continue
                await task
            else:
                # It used to call ingest_export() with no arguments, which cannot work: with no
                # account, container or SAS the reader raises before it reads anything, so every
                # night would have failed with "Provide either sas_url, or both account and
                # container" into the log and nowhere else. Discover the same way the button
                # does, and load the same complementary pair.
                from . import cost
                subs = (await cost.list_subscriptions())["subscriptions"]
                task = await _start_ingest_task(functools.partial(
                    _refresh_primary, subs, months=int(os.getenv("INGEST_MONTHS", "3"))),
                    scope=[s["id"] for s in subs], kind="automatic", identity="workload")
                if task is None:
                    continue
                result = await task
                log.info("daily refresh loaded %s", result.get("loaded") or "nothing")
            log.info("daily refresh finished: %s, data to %s",
                     warehouse.state.get("status"), warehouse.summary().get("to"))
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - a bad night must not stop tomorrow's run
            log.warning("daily refresh failed: %s", str(exc)[:300])


async def _focus_refresh_loop() -> None:
    """Pick up completed owned exports independently of their Azure delivery hour."""
    from . import archive, cost, focus_sync, focus_workflow

    if read_only_mode():
        return
    if os.getenv("AUTO_FOCUS_SYNC", "true").lower() == "false" or not archive.enabled():
        return
    await asyncio.sleep(30)
    while True:
        try:
            compact = await focus_workflow.daily_tick()
            if not (_ingest_task and not _ingest_task.done()):
                scope = [s["id"] for s in (await cost.list_subscriptions())["subscriptions"]]
                found = await focus_sync.discover(scope)
                active = list(dict.fromkeys([
                    *(s["subscription_id"] for s in found["schedules"]
                      if s["status"] == "Active" and s["subscription_id"].lower() not in compact),
                    *(e["subscription"] for e in found["errors"]),
                ]))
                if active:
                    task = await _start_ingest_task(functools.partial(focus_sync.run, active),
                                                    scope=active, kind="focus-sync",
                                                    identity="workload")
                    if task is not None:
                        await task
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.warning("automatic FOCUS sync failed: %s", str(exc)[:300])
        await asyncio.sleep(focus_sync.POLL_SECONDS)


async def _recover_ingest_jobs() -> None:
    from . import ingest_jobs
    if read_only_mode():
        return
    while True:
        try:
            await ingest_jobs.recover()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.warning("Durable ingest recovery: %s", str(exc)[:250])
        await asyncio.sleep(60)


async def _load_export_pair(candidates: list[dict], sas_url: str | None = None,
                            max_files: int = 60,
                            scope: list[str] | None = None,
                            required_metric: str | None = None) -> dict[str, Any]:
    """Load one export covering the current period and one covering the closed months.

    Two groups, not one ranked list, because a MonthToDate export and a TheLastMonth export
    answer different questions and neither substitutes for the other. Loading only the first
    that worked meant a FOCUS refresh rewrote June and July and left August untouched — on an
    estate where August is the month anyone is actually looking at.

    Each ingest replaces only the date range it covers, so the two are complementary.
    """
    from .exports import covers_current_period, ingest_export
    from .warehouse import warehouse

    problems: list[tuple[str, str]] = []
    loaded: list[str] = []

    async def first_that_works(group: list[dict], load_scope: list[str] | None = None) -> bool:
        for candidate in group:
            try:
                await ingest_export(
                    account=candidate["storage_account"],
                    container=candidate["container"],
                    prefix=candidate["root_folder"] or "",
                    sas_url=sas_url,
                    subscription_id=candidate["subscription_id"],
                    max_files=max_files,
                    metric=candidate.get("type"),
                    export_name=candidate.get("name"),
                    **({"required_metric": required_metric} if required_metric else {}),
                    **({"scope": load_scope} if load_scope is not None else {}),
                )
            except Exception as exc:  # noqa: BLE001 - try the next export in this group
                log.warning("export ingest failed for %s: %s",
                            candidate["name"], str(exc)[:200])
                problems.append((candidate["storage_account"], str(exc)))
                continue

            # Recorded the moment the rows are in, before anything optional runs after it.
            # `ingest_export` marks the run ready as its last act and the archive below takes
            # seconds, so a stamp placed after the archive leaves a window where a poll sees a
            # finished refresh with no record of what it loaded — which is exactly what was
            # observed: exports_loaded came back null on a run that had loaded two.
            loaded.append(candidate["name"])
            warehouse.state = {**warehouse.state, "exports_loaded": list(loaded)}

            # Archiving is a follow-up, not part of the load. It was inside the same try, so a
            # failure writing the parquet snapshot would have discarded a perfectly good
            # refresh and sent the caller on to the next export to do it all again.
            try:
                await _archive_after_ingest(
                    candidate.get("type") or "FocusCost",
                    source=f"{candidate['name']} ({candidate['storage_account']})",
                )
            except Exception as exc:  # noqa: BLE001 - the data is already loaded
                log.warning("archive after %s failed: %s", candidate["name"], str(exc)[:200])
            return True
        return False

    if required_metric:
        candidates = [c for c in candidates if c.get("type") == required_metric]
    if scope is None and candidates and all(c.get("subscription_id") for c in candidates):
        scope = sorted({c["subscription_id"] for c in candidates})
    current = [c for c in candidates if covers_current_period(c)]
    closed = [c for c in candidates if not covers_current_period(c)]

    # Current period first: if only one of the two can be read, the month in progress is the one
    # worth having, because it is the only one still changing.
    missing: list[str] = []
    if scope is None:
        got_current = await first_that_works(current)
        got_closed = await first_that_works(closed)
    else:
        got_current = got_closed = False
        for subscription in scope:
            # A billing export can contain several subscriptions. Apply the row boundary
            # even there, and do not let one subscription's successful export skip another.
            def eligible(candidate: dict, subscription: str = subscription) -> bool:
                owner = candidate.get("subscription_id")
                return not owner or owner.lower() == subscription.lower()

            current_ok = await first_that_works([c for c in current if eligible(c)], [subscription])
            closed_ok = await first_that_works([c for c in closed if eligible(c)], [subscription])
            got_current = got_current or current_ok
            got_closed = got_closed or closed_ok
            if not current_ok and not closed_ok:
                missing.append(subscription)
                problems.append((subscription, "No readable export covered this subscription."))

    if got_current or got_closed:
        state = dict(warehouse.state)
        state["exports_loaded"] = loaded
        if not got_current:
            state["detail"] = (
                "Loaded closed-month data only — nothing covering the current month could be "
                "read, so today's figures are unchanged."
                if current else
                "Loaded closed-month data only. No export covers the current month, so today's "
                "figures come from the last full refresh.")
        if missing:
            state["status"] = "partial"
            state["detail"] = (
                f"Refreshed some selected subscriptions; {len(missing)} had no readable export.")
            state["missing_subscriptions"] = missing
        warehouse.state = state
    return {"loaded": loaded, "problems": problems,
            "current": got_current, "closed": got_closed, "missing": missing}


async def _refresh_primary(subs: list[dict], months: int = 3) -> dict:
    """FOCUS evidence first; Query fills only days without richer report evidence."""
    from .exports import (
        deployment_export_hint,
        discover_exports,
        reachable,
        uses_deployment_storage,
    )
    from .warehouse import warehouse

    scope = [s["id"] for s in subs]
    if not scope:
        return {"rows": 0, "failed": 0, "detail": "No accessible subscriptions."}
    result: dict = {"loaded": [], "problems": []}
    try:
        found = await discover_exports(subscription_ids=scope, deployment_only=True)
        candidates = [e for e in found["exports"]
                      if e.get("type") == "FocusCost" and e.get("container")
                      and uses_deployment_storage(e)]
        if candidates:
            ok, bad = await reachable(candidates)
            result = await _load_export_pair(
                ok, scope=scope, required_metric="FocusCost",
                max_files=int(os.getenv("EXPORT_MAX_FILES", "60")))
            result["problems"].extend((e["storage_account"], error) for e, error in bad)
        else:
            detail = deployment_export_hint("FocusCost")
            result["problems"].append(("FOCUS", detail))
            log.info("FOCUS background load: %s", detail)
    except Exception as exc:
        result["problems"].append(("FOCUS", str(exc)))
        log.warning("FOCUS background load unavailable: %s", str(exc)[:200])
    focus_rows = int(warehouse.state.get("rows") or 0) if result["loaded"] else 0
    from . import ingest_jobs
    quick = await ingest_jobs.ingest(subs, months, "ActualCost", quick=True)
    status = quick.get("status") or warehouse.state.get("status", "ready")
    if result["loaded"] and status == "failed":
        status = "partial"
    warehouse.state = {**warehouse.state, "exports_loaded": result["loaded"],
                       "status": status,
                       "detail": "FOCUS reports where available; Quick estimates only on days "
                                 "without richer reports. Missing FOCUS is not full billing coverage.",
                       "focus_problems": result["problems"]}
    return {**quick, "status": status, "rows": focus_rows + int(quick.get("rows") or 0),
            "loaded": result["loaded"]}


async def _start_ingest_task(factory: Any, *, scope: list[str] | None = None,
                             kind: str = "refresh", payload: dict | None = None,
                             identity: str = "interactive",
                             idempotency_key: str | None = None) -> asyncio.Task | None:
    """Persist identity before accepting work; PostgreSQL owns the cross-process writer lease."""
    global _ingest_task
    from . import jobs

    if read_only_mode():
        raise HTTPException(403, "This deployment is read-only; ingest workers are disabled.")
    async with _ingest_start_lock:
        if _ingest_task and not _ingest_task.done():
            return None
        if payload is None and kind == "automatic" and isinstance(factory, functools.partial):
            if factory.func is _run_ingest:
                payload = {"handler": "ingest", "months": int(factory.args[0]), "metric": "FocusCost"}
            elif factory.func is _refresh_primary:
                payload = {"handler": "primary", "months": int(factory.keywords.get("months", 3))}
        try:
            record = await asyncio.to_thread(jobs.create, kind, scope or [], payload=payload,
                                             identity=identity, idempotency_key=idempotency_key)
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from exc
        _ingest_task = jobs.spawn(record, factory)
        return _ingest_task


def _claim_ingest(task: asyncio.Task, *, mode: str | None = None,
                  metric: str | None = None) -> str:
    """The legacy started field is an immutable UUID, not an importer-owned timestamp."""
    from .warehouse import warehouse

    started = task.job_id
    warehouse.state = {"status": "running", "started": started, "job_id": started, "progress": 0.0,
                       "total": 0, "done": 0, "failed": 0, "empty": 0,
                       **({"mode": mode} if mode else {}),
                       **({"metric": metric} if metric else {})}

    return started


async def _run_ingest(months: int, metric: str = "FocusCost", quick: bool = False,
                      scope: list[str] | None = None) -> None:
    """Populate the warehouse. Slow (minutes), so always a background job.

    Prefers a configured export over ARM. Where COST_EXPORT_SAS_URL is set, the operator has
    said that blob storage is this deployment's route to cost data — usually because the app's
    identity holds no role on the subscription, which makes the ARM path not merely slower but
    guaranteed to fail. Trying ARM first there means every startup logs an authorization error
    and leaves the warehouse empty while a readable export sits untouched.
    """
    from . import cost
    from .exports import ingest_export
    from .warehouse import warehouse

    sas = os.getenv("COST_EXPORT_SAS_URL", "").strip()
    if sas and scope is None:
        try:
            log.info("ingest starting from configured export")
            # The requested report metric does not identify a SAS export's dataset; its manifest does.
            await ingest_export(sas_url=sas, max_files=int(os.getenv("EXPORT_MAX_FILES", "60")),
                                **({"required_metric": "FocusCost"} if metric == "FocusCost" else {}))
            log.info("export ingest complete: %s", warehouse.state.get("status"))
            await _archive_after_ingest(metric, source="configured export")
            return
        except Exception as exc:  # noqa: BLE001
            log.warning("export ingest failed: %s", str(exc)[:300])
            warehouse.state = {"status": "failed", "detail": str(exc)[:300],
                               **warehouse.summary()}
            if metric != "FocusCost":
                return

    try:
        subs = (await cost.list_subscriptions())["subscriptions"]
        if scope is not None:
            wanted = {s.lower() for s in scope}
            subs = [s for s in subs if s["id"].lower() in wanted]
        if not subs:
            raise cost.CostError("No accessible subscriptions are available to refresh.")
        log.info("ingest starting for %d subscription(s), %d month(s), metric=%s, quick=%s",
                 len(subs), months, metric, quick)
        if metric == "FocusCost" and not quick:
            await _refresh_primary(subs, months)
            await _archive_after_ingest("ActualCost", source="Quick fallback after FOCUS reports")
            return
        if quick:
            if metric == "FocusCost":
                metric = "ActualCost"
            from . import ingest_jobs
            await ingest_jobs.ingest(subs, months, metric, quick=True)
        else:
            from . import ingest_jobs
            await ingest_jobs.ingest(subs, months, metric, quick=False)
        log.info("ingest complete: %s", warehouse.state.get("status"))
        await _archive_after_ingest(
            metric, source=f"Cost {'Query' if quick else 'Details'} API ({metric})")
    except Exception as exc:  # noqa: BLE001
        log.exception("ingest failed")
        warehouse.state = {"status": "failed", "detail": str(exc)[:300]}


async def _archive_after_ingest(metric: str, source: str | None = None) -> None:
    """Write the day's copy of whatever was just loaded.

    Never allowed to fail the refresh. The refresh has already succeeded by this point — the
    warehouse holds new data and the dashboard will show it — so turning an unreachable storage
    account into a failed refresh would report a false problem and hide a real success. The
    outcome is recorded on `warehouse.state` instead, which is what the UI reads.
    """
    from . import archive
    from .warehouse import warehouse

    if warehouse.state.get("status") not in ("ready", "partial"):
        return
    if not archive.enabled():
        return

    try:
        written = await archive.archive_current(metric, source=source)
        warehouse.state = {**warehouse.state, "archive": written}
        log.info("archive written: %s (%s bytes)", written["name"], written["bytes"])
    except Exception as exc:  # noqa: BLE001 - a refresh that worked must not report failure
        log.warning("archive failed: %s", str(exc)[:300])
        warehouse.state = {**warehouse.state,
                           "archive": {"error": str(exc)[:300],
                                       "name": archive.blob_name(metric)}}


async def _prepare_snapshot_generation(warehouse: Any, state: Any) -> None:
    from . import jobs

    for attempt in range(3):
        try:
            generation = await offload(jobs.read_generation)
            if (not await offload(jobs.generation_ready, generation)
                    or not await offload(jobs.derived_complete)):
                await warehouse.refresh_rollup_async()
                generation = await offload(jobs.read_generation)
                if not await offload(jobs.generation_ready, generation):
                    raise RuntimeError("The rebuilt rollup has not published a coherent generation.")
            state.snapshot_bootstrap_ready = True
            state.snapshot_bootstrap_error = None
            return
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            state.snapshot_bootstrap_error = type(exc).__name__
            log.error("Snapshot startup preparation failed (%s), attempt %d",
                      type(exc).__name__, attempt + 1)
            if attempt < 2:
                await asyncio.sleep(5 * (attempt + 1))
    # Said once, at the end, because the per-attempt lines above each look survivable
    # and nothing else reports this now. Readiness used to: `/readyz` stayed 503 while
    # `snapshot_bootstrap_ready` was false, which advertised the failure by refusing to
    # come up -- an expensive way to raise an alarm, and one that also refused to come
    # up for the far more common case of a rollup that was merely slow. With readiness
    # back to its documented meaning, this log is the signal.
    log.error("Snapshot startup preparation gave up after 3 attempts (%s); "
              "the rollup generation is not coherent and cached reads will stay pending",
              state.snapshot_bootstrap_error or "unknown")


async def _open_snapshot_cache_later(cache: Any) -> None:
    """Keep trying to open the snapshot cache after the first attempt failed.

    `SnapshotCache.start()` opens PostgreSQL, and tenant policy stops the database
    every night, so the inline first attempt is the one call in startup most likely
    to meet a server that is not there. It used to take the process with it: the
    exception left `lifespan`, uvicorn exited non-zero, and App Service -- seeing
    several of those in a row -- blocked the site. Measured 29 Sep 2026 on
    cloudlens-web-scale: `Container exited with exit code 3 during startup after
    73.5s`, repeatedly, then `Site is blocked due to multiple, consecutive cold
    start failures`. The database being asleep turned into the app being unable to
    start at all, which is a far worse failure than the one it was reporting.

    Serving without the cache is not free -- every request goes to the origin and
    pays full price -- but it is strictly better than serving nothing, and
    `SnapshotCache.get` already treats a failing store as a cache outage and falls
    through to the origin rather than returning an error.
    """
    delay = 5.0
    while True:
        await asyncio.sleep(delay)
        try:
            await cache.start()
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - keep retrying whatever the database says
            log.warning("Snapshot cache start retry failed (%s)", type(exc).__name__)
            # Capped, because a database stopped overnight should not be retried
            # at ever-widening intervals into the next working day.
            delay = min(delay * 2, 60.0)
        else:
            log.info("Snapshot cache opened on a later attempt")
            return


def snapshot_warming_enabled() -> bool:
    value = os.getenv("SNAPSHOT_WARMING_ENABLED", "false").strip().lower()
    if value not in {"true", "false", "1", "0"}:
        raise ValueError("SNAPSHOT_WARMING_ENABLED must be true, false, 1, or 0.")
    return value in {"true", "1"}


def snapshot_server_warming_enabled() -> bool:
    """The server recomputes used views after a deploy, publish or UTC day boundary.

    Separate from `SNAPSHOT_WARMING_ENABLED` (the browser re-fetching what it has open):
    this one spends server time on views nobody has asked for yet, so it is opted into.
    """
    value = os.getenv("SNAPSHOT_SERVER_WARMING_ENABLED", "false").strip().lower()
    if value not in {"true", "false", "1", "0"}:
        raise ValueError("SNAPSHOT_SERVER_WARMING_ENABLED must be true, false, 1, or 0.")
    return value in {"true", "1"}


def snapshot_warm_min_seconds() -> float:
    raw = os.getenv("SNAPSHOT_WARM_MIN_SECONDS", "").strip()
    if not raw:
        return 1.0
    try:
        value = float(raw)
    except ValueError:
        value = -1.0
    if not math.isfinite(value) or value < 0:
        raise ValueError("SNAPSHOT_WARM_MIN_SECONDS must be a non-negative number of seconds.")
    return value


def _positive_int_env(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        value = int(raw.strip())
    except ValueError as exc:
        raise ValueError(f"{name} must be a positive integer.") from exc
    if value <= 0:
        raise ValueError(f"{name} must be a positive integer.")
    return value


def snapshot_cache_settings() -> dict[str, int]:
    # Sizes are of the uncompressed JSON; PostgreSQL stores it compressed, and a body is
    # in app memory only while one response is built or sent. 32MB per entry because
    # Resources returns up to 8,000 resources -- 16MB on the 16.9M-row estate -- and an
    # answer that does not fit is recomputed on every view, which cost 33s each time.
    return {
        "max_bytes": _positive_int_env("SNAPSHOT_MAX_BYTES", 1024 * 1024 * 1024),
        "max_entries": _positive_int_env("SNAPSHOT_MAX_ENTRIES", 4096),
        "max_payload": _positive_int_env("SNAPSHOT_MAX_PAYLOAD", 32 * 1024 * 1024),
        "origin_timeout": _positive_int_env("SNAPSHOT_ORIGIN_TIMEOUT", 300),
    }


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    global _agent, _ingest_task, _overview, _snapshot_cache, _snapshot_warmer
    app.state.readiness = None
    app.state.snapshot_bootstrap_ready = False
    app.state.snapshot_bootstrap_error = None
    snapshot_warming_enabled()
    server_warming = snapshot_server_warming_enabled()
    warm_min_seconds = snapshot_warm_min_seconds()
    if not os.getenv("PROJECT_ENDPOINT"):
        raise RuntimeError("PROJECT_ENDPOINT is not set. See README.md.")
    from . import azure_updates, backfill, currency, onboarding, service_health
    from .readiness import ReadinessProbe
    from .warehouse import warehouse

    # Database and core schema only, which is exactly what `/readyz` is documented to
    # mean: "an empty initialised warehouse is ready ... readiness does not establish
    # data freshness". It had drifted to also require `snapshot_bootstrap_ready`, and
    # that flag is only set once `_prepare_snapshot_generation` has finished -- which
    # rebuilds the rollup when the generation is incoherent. So the warmup path waited
    # on a full rebuild: measured 382.2s on the 16.9M-row estate, against a container
    # start limit of 1800s, with `/readyz` returning 504 at the gateway meanwhile.
    # Freshness is not dropped, only moved back to where it is already answered
    # honestly -- `SnapshotCache.get` returns 503 with `Retry-After` per request while
    # the generation is pending, which tells a caller what to do; a container that
    # never reports ready does not.
    readiness = ReadinessProbe(warehouse.check_readiness)
    background: list[asyncio.Task] = []
    try:
        auth = auth_service()
        _agent = CostAgent()
        _overview = {"status": "loading"}
        if os.getenv("SNAPSHOT_CACHE_ENABLED", "true").lower() != "false":
            snapshot_settings = snapshot_cache_settings()
            _snapshot_cache = SnapshotCache(
                SnapshotStore(
                    warehouse.connect,
                    max_bytes=snapshot_settings["max_bytes"],
                    max_entries=snapshot_settings["max_entries"],
                    max_payload=snapshot_settings["max_payload"],
                ),
                version=application_version(build_id=os.getenv("CLOUDLENS_BUILD_ID", "")),
                origin_timeout=snapshot_settings["origin_timeout"],
                warm_min_seconds=warm_min_seconds,
            )
            try:
                await _snapshot_cache.start()
            except Exception as exc:  # noqa: BLE001 - a closed database must not stop the app booting
                log.error("Snapshot cache could not open at startup (%s); serving uncached",
                          type(exc).__name__)
                background.append(asyncio.create_task(
                    _open_snapshot_cache_later(_snapshot_cache), name="snapshot-cache-retry"))
            background.append(asyncio.create_task(
                _prepare_snapshot_generation(warehouse, app.state), name="snapshot-bootstrap"))
            if server_warming:
                public = validate_public_base_url(os.getenv("PUBLIC_BASE_URL"))
                _snapshot_warmer = SnapshotWarmer(
                    app, lambda: _snapshot_cache, snapshot_warm_credentials,
                    host=public.removeprefix("https://") if public else "localhost",
                    scheme="https" if public else "http",
                    concurrency=_positive_int_env("SNAPSHOT_WARM_CONCURRENCY", 1),
                    limit=_positive_int_env("SNAPSHOT_WARM_LIMIT", 100),
                    min_seconds=warm_min_seconds,
                )
                _snapshot_warmer.start()
        else:
            app.state.snapshot_bootstrap_ready = True
        background.append(asyncio.create_task(_refresh_overview(), name="overview-refresh"))
        if not read_only_mode():
            background.extend([
                asyncio.create_task(_daily_refresh(), name="daily-refresh"),
                asyncio.create_task(_focus_refresh_loop(), name="focus-sync"),
                asyncio.create_task(_recover_ingest_jobs(), name="ingest-recovery"),
            ])
        # `AUTO_INGEST` before `summary()`, because `summary()` reads PostgreSQL and this
        # is the last database call left on the startup critical path. Asking whether
        # automatic ingest is wanted costs nothing and is false wherever ingest is driven
        # externally, so the deployment that most needs to survive a stopped database no
        # longer touches it here. Where ingest *is* automatic the call still happens and a
        # database failure is still fatal, which is right: there is no ingest without one.
        if (not read_only_mode() and os.getenv("AUTO_INGEST", "true").lower() != "false"
                and warehouse.summary()["rows"] == 0):
            await _start_ingest_task(functools.partial(
                _run_ingest, int(os.getenv("INGEST_MONTHS", "3"))),
                kind="automatic", identity="workload")

        # Warm slow dependencies without delaying startup. MSAL does blocking tenant discovery.
        background.append(asyncio.create_task(currency.refresh(), name="currency-warmup"))
        if auth.sso.enabled:
            targets = {"", auth.sso.tenant}
            if auth.sso.home_tenant:
                targets.add(auth.sso.home_tenant)
            background.extend(
                asyncio.create_task(run_in_threadpool(auth.sso._app_for_login, tenant),
                                    name="sso-warmup")
                for tenant in targets
            )
        log.info("cost agent ready, model=%s", _agent.model)
        app.state.readiness = readiness
        yield
    finally:
        app.state.readiness = None
        await readiness.close()
        if _snapshot_warmer is not None:
            # Before the cache: its passes run through the cache and the warehouse pool.
            await _snapshot_warmer.close()
            _snapshot_warmer = None
        if _snapshot_cache is not None:
            await _snapshot_cache.close()
            _snapshot_cache = None
        # Await cancellation before closing resources that background jobs still use.
        pending = set(background) | set(_savings_pending.values()) | set(_billing_pending.values())
        pending.update(backfill._tasks)
        pending.update(task for task in (_ingest_task, onboarding._task) if task is not None)
        for task in pending:
            task.cancel()
        await asyncio.gather(*pending, return_exceptions=True)
        from . import jobs
        await jobs.stop()
        # Audit writes run in worker threads; drain them rather than abandoning the DB work.
        await asyncio.gather(*list(_audit_tasks), return_exceptions=True)
        _audit_tasks.clear()
        _ingest_task = None
        _savings_pending.clear()
        _billing_pending.clear()
        _savings_cache.clear()
        _billing_cache.clear()
        try:
            closers = [azure_updates._public_cache.close(), service_health._cache.close()]
            if _agent is not None:
                closers.append(_agent.close())
            outcomes = await asyncio.gather(*closers, return_exceptions=True)
            for outcome in outcomes:
                if isinstance(outcome, BaseException):
                    log.warning("background resource cleanup failed: %s", outcome)
        finally:
            _agent = None
            warehouse._store.close()


app = FastAPI(title="CloudLens", version="1.0.0", lifespan=lifespan)

# SSE is already chunked and must not be buffered, so exclude it by minimum size rather than
# compressing everything: JSON and the HTML/CSS/JS bundle benefit, the event stream is unaffected
# because each frame is far below the threshold.
app.add_middleware(GZipMiddleware, minimum_size=1024)


def auth_service() -> Auth:
    """One shared Auth, built on first use so importing this module has no side effects."""
    global _auth
    if _auth is None:
        _auth = Auth()
    return _auth


# Signing in, the login page itself and the static assets it needs. Everything else — including
# the app shell — requires a session, so an unauthenticated visitor learns nothing about the
# estate, not even how many subscriptions there are.
PUBLIC_PATHS = {"/login", "/healthz", "/readyz", "/api/auth/login", "/api/auth/logout", "/api/auth/me",
                # Admin consent has to be reachable by someone who cannot sign in yet — that is
                # the entire situation it exists to resolve.
                "/auth/login", "/auth/callback", "/auth/admin-consent"}
PUBLIC_PREFIXES = ("/assets/",)


def _wants_html(request: Request) -> bool:
    return "text/html" in request.headers.get("accept", "")


def safe_next(target: str | None) -> str:
    """Sanitise a `next=` value into a path on this site, or fall back to the app root.

    A login page that will redirect anywhere is a phishing tool: the link looks like ours, the
    landing page is not. `//evil.example.com` and `/\\evil.example.com` are the ones that catch
    people out — both start with a slash, and both are read by browsers as *another origin*.
    API and callback paths are excluded too, so a redirect can never dump someone on raw JSON.
    """
    if not target or not target.startswith("/"):
        return "/"
    if target.startswith(("//", "/\\")):
        return "/"
    if target.startswith(("/api/", "/auth/", "/login")):
        return "/"
    return target


# Retain audit writes until completion and drain them before closing the warehouse.
_audit_tasks: set[asyncio.Task] = set()


def _log_access(user: User, tab: str) -> None:
    from .warehouse import warehouse

    task = asyncio.create_task(
        offload(warehouse.record_access, user_name=user.name, email=user.email,
                source=user.source, is_admin=user.admin, tab=tab)
    )
    _audit_tasks.add(task)

    def finished(done: asyncio.Task) -> None:
        _audit_tasks.discard(done)
        if not done.cancelled() and (error := done.exception()) is not None:
            log.warning("access audit write failed: %s", error)

    task.add_done_callback(finished)


class RequireAuth(BaseHTTPMiddleware):
    """Gate every request that isn't explicitly public.

    A browser navigation is redirected to the login page (with `next`, so the person lands back
    where they were aiming); anything else gets a 401 with a JSON body, which is what fetch and
    curl can actually act on.
    """

    async def dispatch(self, request: Request, call_next):
        path = request.url.path
        if (read_only_mode() and request.method not in {"GET", "HEAD", "OPTIONS"}
                and not (request.method == "POST" and path in {
                    "/api/auth/login", "/api/auth/logout",
                    "/api/focus/preview", "/api/focus/daily/preview", "/api/cost-data/preview",
                })):
            return JSONResponse(
                {"detail": "This deployment is read-only. Changes and background imports are disabled.",
                 "read_only": True}, status_code=403)
        auth = auth_service()
        request.state.user = auth.identify(
            request.cookies.get(COOKIE), request.headers.get("authorization")
        )

        if path in PUBLIC_PATHS or path.startswith(PUBLIC_PREFIXES):
            return await call_next(request)

        if request.state.user is None:
            if _wants_html(request):
                target = request.url.path
                if request.url.query:
                    target = f"{target}?{request.url.query}"
                return RedirectResponse(f"/login?next={quote(target, safe='')}", status_code=303)
            return JSONResponse({"detail": "Sign in to continue.", "auth": "required"},
                                status_code=401)

        # One line per tab actually fetched from the server -- not `sections`, which repaints
        # the rail's own costs on every navigation and is not itself a view of anything, and
        # would drown the log in noise it if were counted. Never awaited: a slow or failing
        # audit write must not be the reason a tab took longer to load. Nor is the server
        # warming a view a person looking at it.
        if (request.method == "GET" and path.startswith("/api/dashboard/")
                and path != "/api/dashboard/sections"
                and not request.scope.get(WARM_SCOPE_KEY)):
            _log_access(request.state.user, path.rsplit("/", 1)[-1])

        from . import reporting

        # Context propagation only; date predicates belong to the warehouse and its callers.
        month = request.query_params.get("period_month")
        try:
            reporting.validate_month(month)
        except ValueError as exc:
            return JSONResponse({"detail": str(exc)}, status_code=400)
        live_routes = {
            "/api/overview", "/api/dashboard/shutdown", "/api/dashboard/waste",
            "/api/dashboard/savings", "/api/dashboard/savings/progress",
            "/api/dashboard/rightsizing", "/api/dashboard/whatif",
            "/api/dashboard/advisor", "/api/dashboard/governance", "/api/dashboard/rates",
            "/api/dashboard/health", "/api/dashboard/azure-updates", "/api/dashboard/azure-status",
            "/api/dashboard/service-health", "/api/dashboard/esu", "/api/dashboard/hybrid-benefit",
            "/api/dashboard/billing/opportunities",
            "/api/dashboard/change-analysis", "/api/dashboard/performance",
        }
        report_route = (
            path.startswith("/api/dashboard/") or path.startswith("/api/report")
            or path in {"/api/filters", "/api/costs/raw.csv", "/api/insights", "/api/ask",
                        "/api/archive/export", "/api/archive/export/download"}
        )
        active = month if report_route and path not in live_routes else None
        with reporting.select(active):
            response = await call_next(request)
        if month:
            response.headers["X-Reporting-Month"] = month
            response.headers["X-Reporting-Window"] = "live" if path in live_routes else "calendar"
            response.headers["X-Reporting-Live-Note"] = reporting.LIVE_NOTE
        return response


@app.exception_handler(PoolBusy)
async def _pool_busy(request: Request, exc: PoolBusy) -> JSONResponse:
    """Load shedding, not a fault.

    Every pooled connection was checked out and none came back within the pool
    timeout. Nothing is broken and the request was never served, so the honest answer
    is 503 with a time to come back -- a 500 would tell the browser, and whoever reads
    the logs, that the application failed.

    Registered globally because the pool is reached from routes that do not go through
    the snapshot cache at all; without this, each one would have to know about the pool
    to avoid reporting congestion as a crash.
    """
    log.warning("pool exhausted serving %s; shedding the request", request.url.path)
    return JSONResponse(
        {"detail": "The warehouse is busy; retry shortly."}, status_code=503,
        headers={"Retry-After": "5", "Cache-Control": "private, no-store",
                 "X-CloudLens-Cache-Error": "pool_busy"})


app.add_middleware(RequireAuth)# Starlette wraps in reverse registration order: route aliases before creating or
# checking host-only auth cookies, but retain the outer security/cache headers.
app.add_middleware(
    CanonicalOriginMiddleware,
    public_base_url=validate_public_base_url(os.getenv("PUBLIC_BASE_URL")),
)


class CacheHeaders(BaseHTTPMiddleware):
    """Preserve immutable content-versioned assets; revalidate unversioned URLs.

    It also carries the handful of security headers that apply site-wide. Framing is denied
    outright: nothing here is meant to be embedded, and a login form that can be framed is a
    login form that can be overlaid.
    """

    async def dispatch(self, request: Request, call_next):
        started = time.perf_counter()
        response = await call_next(request)
        path = request.url.path
        if path.startswith("/api/"):
            elapsed_ms = (time.perf_counter() - started) * 1000
            route = request.scope.get("route")
            route_path = getattr(route, "path", None)
            if not isinstance(route_path, str) or not route_path.startswith("/api/"):
                route_path = "<unmatched>"
            timing_parts = [f"app;dur={elapsed_ms:.1f}"]
            seen_timings = {"app"}
            previous = response.headers.get("Server-Timing", "")
            if previous:
                for part in previous.split(","):
                    value = part.strip()
                    name = value.split(";", 1)[0]
                    if value and name not in seen_timings:
                        timing_parts.append(value)
                        seen_timings.add(name)
            phases = getattr(request.state, "overview_timings", {})
            for name, duration in sorted(phases.items()):
                metric = f"overview_{name}"
                if (name in OVERVIEW_TIMING_PHASES and metric not in seen_timings
                        and isinstance(duration, (int, float))):
                    timing_parts.append(f"{metric};dur={max(0, duration):.1f}")
                    seen_timings.add(metric)
            response.headers["Server-Timing"] = ", ".join(timing_parts)
            if elapsed_ms >= SLOW_API_LOG_MS and not request.scope.get(WARM_SCOPE_KEY):
                phase_log = ",".join(
                    f"{name}:{duration:.1f}" for name, duration in sorted(phases.items())
                    if name in OVERVIEW_TIMING_PHASES and isinstance(duration, (int, float)))
                if phase_log:
                    log.warning(
                        "slow API response method=%s route=%s status=%d app_ms=%.1f phases_ms=%s",
                        request.method, route_path, response.status_code, elapsed_ms, phase_log)
                else:
                    log.warning("slow API response method=%s route=%s status=%d app_ms=%.1f",
                                request.method, route_path, response.status_code, elapsed_ms)
        if path.startswith("/assets/") and response.status_code in {200, 206, 304}:
            response.headers.setdefault("Cache-Control", "no-cache")
        elif path.startswith("/api/"):
            response.headers.setdefault("Cache-Control", "private, no-store")
        else:
            # The app shell and the login page are behind a sign-in, so they must not sit in a
            # shared cache or come back from history after someone signs out.
            response.headers.setdefault("Cache-Control", "no-store")
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("Referrer-Policy", "same-origin")
        response.headers.setdefault("X-Frame-Options", "DENY")
        response.headers.setdefault("Content-Security-Policy", _content_security_policy())
        # The site is HTTPS-only at the platform (`httpsOnly`), so say so to the browser too
        # and stop the first request of a session going out in the clear.
        response.headers.setdefault(
            "Strict-Transport-Security", "max-age=31536000; includeSubDomains")
        return response


SLOW_API_LOG_MS = 1000.0
OVERVIEW_TIMING_PHASES = frozenset({
    "warehouse_summary", "warehouse_month_totals", "subscriptions",
    "forecast", "budgets", "mtd_cost_query", "last_cost_query",
})


def _overview_timing_header(timings: dict[str, float], failures: set[str]) -> str:
    return ", ".join(
        f'overview_{name};dur={max(0, timings[name]):.1f}'
        + (';desc="error"' if name in failures else "")
        for name in sorted(timings) if name in OVERVIEW_TIMING_PHASES
    )


app.add_middleware(CacheHeaders)


class Ask(BaseModel):
    question: str = Field(min_length=1, max_length=2000)
    history: list[dict] = Field(default_factory=list, max_length=60)
    scope: list[str] = Field(default_factory=list)


def agent() -> CostAgent:
    if _agent is None:
        raise HTTPException(503, "Agent not ready")
    return _agent


def current_user(request: Request) -> User:
    user = getattr(request.state, "user", None)
    if user is None:  # the middleware should have stopped this already
        raise HTTPException(401, "Sign in to continue.")
    return user


def require_admin(request: Request) -> User:
    """Ingests rewrite shared data and hammer the Azure APIs, so they are admin-only."""
    user = current_user(request)
    if not user.admin:
        raise HTTPException(403, "Only an admin can start an ingest.")
    return user


def authorize_budget(user: User, subscription: str, scope: list[str] | None) -> str:
    """Authorize the write before a model or HTTP request can borrow the server identity."""
    from uuid import UUID

    delegated = user.source == "entra" and auth_service().sso.delegated
    if not user.admin and not delegated:
        raise PermissionError(
            "Budget creation requires an admin or delegated Azure access "
            "(AUTH_DELEGATED_ARM), so it runs under your own Azure permissions.")
    try:
        target = str(UUID(str(subscription).strip()))
    except (ValueError, AttributeError):
        raise ValueError("Choose a valid subscription ID for the budget.") from None
    if scope is not None and target not in {s.lower() for s in scope}:
        raise PermissionError("That subscription is outside your permitted, selected scope.")
    return target


# --------------------------------------------------------------- who sees what
class NoAccess(HTTPException):
    """Signed in, but Azure says they can see nothing. Not an error — an answer."""

    def __init__(self, detail: str) -> None:
        super().__init__(status_code=403, detail=detail)


async def permitted(user: User) -> list[str] | None:
    """The subscriptions this person may see, or None for 'whatever the server can see'.

    For an Entra sign-in the list comes from ARM using their *own* delegated token, so it is
    their RBAC — not a copy of it that we maintain and get wrong.
    """
    if user.source != "entra":
        return None

    auth = auth_service()
    session = auth.sso.get(user.sid)
    if session is None:
        raise HTTPException(401, "Your session expired. Sign in again.")

    subs = await auth.sso.subscriptions(session)
    if auth.sso.get(user.sid) is None:
        raise HTTPException(401, "Your Azure session expired. Sign in again.")
    if not subs:
        if session.problem:
            raise HTTPException(503, session.problem, headers={"Retry-After": "5"})
        raise NoAccess(_no_access_detail(session, auth))
    return [s["id"] for s in subs]


def _no_access_detail(session, auth) -> str:
    """Say which of the dead ends this is, because the fix differs.

    'No subscriptions' after checking eight directories is a different problem from 'no
    subscriptions' after being turned away at all eight, and someone told the wrong one will go
    asking the wrong people for the wrong thing. In app-identity mode there is a third: the
    person may be entitled to plenty and CloudLens simply cannot see any of it.
    """
    if auth.sso.multi_tenant and session.skipped and not session.tenants:
        names = ", ".join(t["name"] for t in session.skipped[:4])
        return (f"None of your Azure directories ({names}) would grant this app access on your "
                "behalf. An administrator in the directory that owns the subscriptions has to "
                "approve it once, after which sign-in works for everyone there.")
    if auth.sso.multi_tenant:
        checked = len(session.tenants) or 1
        return (f"Your account has no access to any Azure subscription in the {checked} "
                "directory(ies) it can reach. Ask a subscription owner for the Cost Management "
                "Reader role.")
    if not auth.sso.delegated:
        # Two different asks, and guessing between them wastes somebody's afternoon.
        if not session.app_visible:
            return ("CloudLens cannot read any Azure subscription, so it cannot tell what you "
                    "are entitled to. Grant CloudLens's managed identity Cost Management Reader "
                    "and Reader on the subscriptions to analyse, then sign in again.")
        return (f"You hold no cost-capable role on the {session.app_visible} subscription(s) "
                "CloudLens can read. Ask a subscription owner for the Cost Management Reader "
                "role — or, if the subscription you expected is missing, for CloudLens's own "
                "identity to be granted Reader on it too.")
    return ("Your account has no access to any Azure subscription in this tenant. "
            "Ask a subscription owner for the Cost Management Reader role.")


def narrow(picked: list[str], allowed: list[str] | None) -> list[str]:
    """Apply the shared scope policy, using [] for unrestricted legacy callers."""
    return selected_scope(picked, allowed) or []


# Views whose cached answer must not survive a budget CloudLens itself created. Budgets
# are read live from Azure and cached for speed; without this, a cached "no budgets" could
# outlast the create that disproves it and invite a second, duplicate budget.
BUDGET_VIEWS = frozenset({"/api/budgets", "/api/insights"})
BUDGET_REVISION_KEY = "budget_revision"


def _budget_revision() -> str:
    from .warehouse import warehouse

    try:
        with warehouse.connect(read_only=True) as con:
            row = con.execute("SELECT value FROM meta WHERE key = ?",
                              [BUDGET_REVISION_KEY]).fetchone()
    except Exception as exc:  # noqa: BLE001 - an unknown revision must not reuse a known one
        log.warning("budget revision unavailable (%s)", type(exc).__name__)
        return f"unavailable:{time.time()}"
    return row[0] if row else "0"


def _bump_budget_revision() -> None:
    from .warehouse import warehouse

    with warehouse.connect() as con:
        con.execute(
            "INSERT INTO meta (key, value, updated_at) VALUES (?, '1', now()) "
            "ON CONFLICT (key) DO UPDATE SET "
            "value = CAST(CAST(meta.value AS bigint) + 1 AS text), updated_at = now()",
            [BUDGET_REVISION_KEY])


def selected_scope(picked: list[str], allowed: list[str] | None) -> list[str] | None:
    """An explicit selection never silently expands when it is no longer authorized."""
    selected = list(dict.fromkeys(s.strip().lower() for s in picked if s.strip()))
    if allowed is not None:
        by_id = {s.lower(): s for s in allowed}
        if not by_id or any(s not in by_id for s in selected):
            raise HTTPException(403, "That subscription is outside your permitted scope.")
        return [by_id[s] for s in selected] if selected else list(by_id.values())
    return selected or None


async def authorize_snapshot(request: Request) -> CacheContext:
    """Use the same current entitlement policy as an uncached endpoint."""
    from . import currency

    user = current_user(request)
    allowed = await permitted(user)
    selected = selected_scope(request.query_params.get("scope", "").split(","), allowed)
    auth = auth_service()
    policy: dict[str, Any] = {
        "admin": user.admin, "delegated": auth.sso.delegated,
        "read_only": read_only_mode(), "workload_client": os.getenv("AZURE_CLIENT_ID", ""),
    }
    if request.url.path in BUDGET_VIEWS:
        policy["budget_revision"] = await offload(_budget_revision)
    if user.source == "entra":
        session = auth.sso.get(user.sid)
        if session is None or session.reauth_required:
            raise HTTPException(401, "Your Azure session expired. Sign in again.")
        tenant = session.tenant
        principal = session.oid
        if not principal:
            # Missing OID may not broaden identity to a display name or email.
            principal = "session:" + hashlib.sha256(user.sid.encode()).hexdigest()
        policy["tenant_mapping"] = sorted(
            (str(sub["id"]).lower(), str(sub.get("tenant") or tenant).lower())
            for sub in session.subscriptions or [])
    else:
        tenant, principal = "local", f"{user.source}:{user.name.casefold()}"
        if user.source in {"file", "env"}:
            account = user.name.casefold()
            credential = json.dumps([account, auth.users.secret_for(user.name)],
                                    separators=(",", ":"))
        else:
            credential = request.cookies.get(COOKIE) or request.headers.get("authorization", "")
        policy["session_partition"] = hashlib.sha256(credential.encode()).hexdigest()
    target = request.query_params.get("currency", "").strip().upper()
    conversion = currency.cache_identity(target)
    data_day = ""
    period_version = request.query_params.get("period_month") or datetime.now(UTC).date().isoformat()
    if request.url.path == "/api/dashboard/ai-billing":
        from . import cost
        from .ai_billing import snapshot_data_day

        async with as_user(user):
            if selected is None:
                selected = [sub["id"] for sub in (await cost.list_subscriptions())["subscriptions"]]
            data_day = await offload(snapshot_data_day, selected, request_filters(request))
        # Rolling AI evidence is anchored to imported data, not to midnight.
        # days/month/filter selectors remain in the private durable cache key.
        period_version = request.query_params.get("period_month") or "ai-rolling-data-day-v1"
    return CacheContext(
        principal=principal, tenant=tenant,
        allowed=tuple(allowed) if allowed is not None else None,
        selected=tuple(selected) if selected is not None else None,
        authorization_version=hashlib.sha256(
            json.dumps(policy, sort_keys=True).encode()).hexdigest(),
        fx_version=hashlib.sha256(json.dumps(conversion, sort_keys=True).encode()).hexdigest(),
        period_version=period_version, data_day=data_day,
    )


async def snapshot_warm_credentials(principal: str, tenant: str) -> dict[str, str] | None:
    """Request headers that sign the snapshot warmer in as a cache principal, or None.

    Minted in process and never sent anywhere: the warmer hands them straight to the ASGI
    app, which then applies the ordinary sign-in and entitlement checks. Each form below
    rebuilds exactly the principal and partition `authorize_snapshot` derived for the
    person's own requests, so the warmed answers land where that person will read them.
    """
    auth = auth_service()
    if not auth.enabled:
        return {} if (principal, tenant) == ("disabled:local", "local") else None
    if tenant == "local":
        source, _, name = principal.partition(":")
        if source == "token":
            for token, holder in auth.tokens.items():
                if holder.casefold() == name:
                    return {"authorization": f"Bearer {token}"}
            return None
        if source not in {"file", "env"} or (auth.sso.enabled and not auth.allow_local):
            return None
        auth.users.refresh_if_changed()
        user = auth.users.get(name)
        if user is None or user.source != source:
            return None
        return {"cookie": f"{COOKIE}={auth.sessions.issue(user, hours=1)}"}
    if not auth.sso.enabled:
        return None
    session = auth.sso.live_session(principal, tenant)
    if session is None:
        # Entra sessions are held in memory; after a restart this person's views are
        # warmed when they next sign in.
        return None
    return {"cookie": f"{COOKIE}={entra_sign(session.sid, auth.sessions.secret)}"}


# The filter bar's dimensions, as they arrive on a query string.
#
# An allow-list rather than "anything with an f_ prefix": the key reaches a view definition, so
# an unknown one must be dropped here rather than carried further in. The warehouse checks it
# again — this is the first of two, not the only one.
FILTER_KEYS = ("rg", "service", "region", "charge", "pricing", "env")

# How the environment is read out of a cost row.
#
# Azure has no environment field — it is a convention people implement with a tag — so this
# reads the tag, and reads the four spellings that convention actually uses. Matching only
# `env` would silently report half of an estate that writes `Environment`.
#
# `Tags` is stored as text rather than jsonb, and a bare `::json` cast raises on any row
# holding something that is not JSON. `try_cast_json` is the warehouse's own shim for exactly
# that: it returns NULL where a cast would fail, so one malformed tag document cannot take the
# whole filter bar down with it.
from . import environment  # noqa: E402

ENV_TAG = environment.tag_sql()

# How long the filter bar may hold a database connection before giving up.
#
# It is a secondary control and already degrades to "unavailable" when its query fails, so
# the only thing a long wait buys is a pool slot held against every other request. On the
# real estate the query needs 171s -- well past the server's 110s bound -- so it never
# succeeded and spent 110s of a 8-connection pool failing. Three page loads were enough to
# take the dashboard from 9.8s to a 180s timeout.
#
# Deliberately not zero: on an estate where this completes, it should still complete. This
# only removes the part of the wait that was never going to produce an answer.
FILTER_BUDGET_MS = int(os.getenv("FILTER_TIMEOUT_MS", "15000"))

# The environment tag gets a tighter bound still. It is read out of the `Tags` document per
# row rather than from a column, so it is the one dimension that cannot be answered from an
# index, and it is asked for separately so that when it does not arrive the other five are
# still there. Nothing waits on it: an empty environment list is what an untagged estate
# returns anyway, and `unavailable_dimensions` says which of the two happened.
FILTER_ENV_BUDGET_MS = int(os.getenv("FILTER_ENV_TIMEOUT_MS", "5000"))


def request_filters(request: Request) -> dict[str, list[str]] | None:
    """The filter bar's selection, read from `?rg=a,b&region=eastus`.

    Values are passed through as text and bound by the warehouse; only the *keys* are checked
    here, because a key becomes part of a SQL identifier and a value never does.

    Returns None rather than an empty dict when nothing is selected, so the warehouse can skip
    building a filtered view at all — which is the common case and the one worth keeping fast.
    """
    out: dict[str, list[str]] = {}
    for key in FILTER_KEYS:
        raw = request.query_params.get(key, "")
        values = [v.strip() for v in raw.split(",") if v.strip()]
        if values:
            # A ceiling, so a hand-written query string cannot ask for a ten-thousand-row
            # IN list. Nobody narrows to more than a few dozen of anything from a filter bar.
            out[key] = values[:200]
    return out or None


async def offload(fn: Any, *args: Any, **kwargs: Any) -> Any:
    """Run a blocking call on a worker thread instead of on the event loop.

    Every warehouse-backed endpoint is `async def`, but psycopg is synchronous — so the query ran
    *on the event loop* and nothing else could be served until it finished. The symptom was
    subtle: eight parallel requests took 2.0s against 2.6s sequential, which looks like slow
    endpoints rather than a queue. Individually none of them exceeded 800ms.

    `asyncio.to_thread` releases the loop for the duration. This is safe because each
    call opens and closes its own connection.
    """
    return await asyncio.to_thread(functools.partial(fn, *args, **kwargs))


@asynccontextmanager
async def as_user(user: User) -> AsyncIterator[None]:
    """Run live Azure calls inside this block as the signed-in person, where that is possible.

    With delegated ARM access the call carries their own token — the one issued by the tenant
    that owns whichever subscription is being read — so Azure itself refuses anything they
    aren't entitled to. Without it — the common case, because that permission usually needs an
    administrator to approve — the call runs as the app's identity and the entitlement is
    enforced by the subscription scope worked out from their role assignments.
    """
    from . import cost

    reset = None
    if user.source == "entra":
        auth = auth_service()
        if auth.sso.delegated:
            session = auth.sso.get(user.sid)
            caller = await auth.sso.caller(session) if session else None
            if caller is None:
                if session and session.problem and not session.reauth_required:
                    raise HTTPException(503, session.problem, headers={"Retry-After": "5"})
                raise HTTPException(401, "Your Azure session expired. Sign in again.")
            reset = cost.act_as(caller, session.oid or session.sid)
    try:
        yield
    finally:
        if reset is not None:
            cost.stop_acting(reset)


class Credentials(BaseModel):
    # Deliberately no minimum length: a blank username should be answered by the normal
    # "wrong username or password" — and counted by the throttle — rather than by a validation
    # error, which both looks different and tells an attacker their input never reached the
    # credential check.
    username: str = Field(max_length=120)
    password: str = Field(max_length=256)


@app.get("/login", include_in_schema=False)
async def login_page(request: Request, next: str = "/") -> Response:
    if getattr(request.state, "user", None) is not None:
        return RedirectResponse(safe_next(next), status_code=303)
    html = await offload((WEB / "login.html").read_text, encoding="utf-8")
    return Response(await offload(stamp_asset_urls, html, _static_assets),
                    media_type="text/html", headers={"Cache-Control": "no-store"})


@app.get("/auth/login", include_in_schema=False)
async def sso_start(request: Request, next: str = "/", tenant: str = "",
                    email: str = "") -> Response:
    """Hand the browser to Entra. Public: this *is* the way in.

    `email` is the address typed on our sign-in page. It does two things: it picks the
    authority, and it is forwarded to Entra as a login hint so nobody types it twice.

    `tenant` picks a specific directory's authority explicitly, and still overrides the
    address when both are given.
    """
    auth = auth_service()
    if not auth.sso.enabled:
        return RedirectResponse("/login", status_code=303)

    # Anything that is not plainly one address is dropped rather than rejected. It is only ever
    # a hint, so a malformed one costs the visitor an extra field on Entra's page, whereas
    # failing the sign-in outright over it would strand them with no way in.
    hint = entra_email(email)

    # Only a directory this deployment already names. Otherwise the sign-in link becomes an
    # open redirect into any tenant an attacker likes, with our client id on it.
    chosen = ""
    if tenant:
        allowed = {auth.sso.home_tenant.lower(), auth.sso.tenant.lower()} - {""}
        if tenant.lower() not in allowed:
            raise HTTPException(400, "Unknown directory.")
        chosen = tenant
    elif hint:
        # Derived, so it can only ever be one of the same two the branch above allows.
        chosen, why = auth.sso.route_for(hint)
        log.info("routing %s to %s (%s)", hint, chosen or "the multi-tenant endpoint", why)

    binding = secrets.token_urlsafe(32)
    response = RedirectResponse(await auth.sso.start(
        safe_next(next), tenant=chosen, login_hint=hint, browser_binding=binding), status_code=303)
    response.set_cookie("cloudlens_oauth", binding, max_age=600,
                        **auth.cookie_kwargs(request.url.scheme == "https"))
    return response



def _explain_denied(code: str, detail: str) -> str:
    """'access_denied' is an OAuth code, not a sentence. Put something readable on the page."""
    if code == "access_denied" and detail == code:
        return ("Sign-in was not completed. This usually means your organisation requires an "
                "administrator to approve CloudLens before anyone there can use it.")
    return detail


def _consent_redirect(auth, detail: str) -> str:
    """Send someone blocked by consent policy to the login page *with the way out attached*.

    Without this the flow ends on 'ask your administrator' with nothing to forward, and the
    person has to work out for themselves which app, which permission and which URL.

    Only a flag travels in the query string, not the URL. The page turns it into a link to
    /auth/admin-consent, so the destination is always one this server built — a login page that
    renders whatever URL it is handed is a ready-made phishing page.
    """
    return "/login?error=" + quote(detail[:300], safe="") + "&consent=1"


@app.get("/auth/admin-consent", include_in_schema=False)
async def admin_consent(request: Request) -> Response:
    """Start the tenant-wide consent flow, for an administrator to approve once."""
    auth = auth_service()
    if not auth.sso.enabled:
        return RedirectResponse("/login", status_code=303)
    return RedirectResponse(auth.sso.consent_url(), status_code=303)


@app.get("/auth/callback", include_in_schema=False)
async def sso_callback(request: Request) -> Response:
    """Where Entra sends them back with an authorization code."""
    auth = auth_service()
    if not auth.sso.enabled:
        return RedirectResponse("/login", status_code=303)

    params = dict(request.query_params)

    # An admin coming back from the consent screen. Entra returns them here with no code, so
    # the normal exchange would fail with something unhelpful about a missing authorization.
    if params.get("admin_consent"):
        if params.get("error"):
            detail = params.get("error_description") or params["error"]
            return RedirectResponse(f"/login?error={quote(detail[:300], safe='')}",
                                    status_code=303)
        return RedirectResponse(
            "/login?notice=" + quote(
                "Consent granted for your organisation. Everyone there can sign in now.",
                safe=""),
            status_code=303)

    if params.get("error"):
        # The user declined consent, or the tenant refused. Say so on the login page rather
        # than showing a raw Microsoft error screen.
        detail = params.get("error_description") or params["error"]
        code = params.get("error", "")

        # Entra shows its own "Need admin approval" page and keeps the AADSTS90094 detail
        # there: all that reaches us is a bare access_denied. So the specific reason is not
        # available to match on, and the two causes -- consent policy, or the person clicking
        # cancel -- are indistinguishable from here. Offer the link in both cases: it is the
        # remedy for the first and harmless noise for the second, whereas withholding it
        # leaves the first with nowhere to go.
        #
        # `delegated` and not just `multi_tenant`: a deployment inside one customer's directory
        # is the normal install, and it asks for `user_impersonation` exactly like a multi-tenant
        # one does. Keying this on multi_tenant alone meant the single-directory case -- the one
        # most likely to be a fresh install whose administrator has not approved anything yet --
        # got a raw OAuth code and no way forward.
        if (entra_needs_consent(detail)
                or (code == "access_denied" and (auth.sso.multi_tenant or auth.sso.delegated))):
            return RedirectResponse(_consent_redirect(auth, _explain_denied(code, detail)),
                                    status_code=303)
        return RedirectResponse(f"/login?error={quote(detail[:300], safe='')}", status_code=303)

    session, target, problem = await auth.sso.complete(
        params, browser_binding=request.cookies.get("cloudlens_oauth", ""))
    if session is None:
        if entra_needs_consent(problem):
            return RedirectResponse(_consent_redirect(auth, problem), status_code=303)
        return RedirectResponse(f"/login?error={quote(problem, safe='')}", status_code=303)

    response = RedirectResponse(safe_next(target), status_code=303)
    response.delete_cookie("cloudlens_oauth", path="/")
    response.set_cookie(
        COOKIE,
        entra_sign(session.sid, auth.sessions.secret),
        max_age=int(float(os.getenv("AUTH_SESSION_HOURS", "12")) * 3600),
        **auth.cookie_kwargs(request.url.scheme == "https"),
    )
    if _snapshot_warmer is not None and session.oid:
        # Entra sessions do not survive a restart, so this is the first moment since a
        # deploy that the server can compute this person's views as them.
        _snapshot_warmer.warm_principal_soon(session.oid, session.tenant)
    return response


@app.post("/api/auth/login")
def login(body: Credentials, request: Request) -> JSONResponse:
    """Deliberately a sync endpoint: FastAPI runs it in a worker thread, so the ~200 ms of
    PBKDF2 that verifying a password costs never blocks the event loop for everyone else."""
    auth = auth_service()
    client = request.client.host if request.client else "?"
    user, problem, retry_after = auth.login(body.username, body.password, client)
    if user is None:
        log.warning("failed sign-in for %r from %s", body.username[:40], client)
        # 429 when throttled, so the page can say "wait" rather than "wrong password", and
        # Retry-After so a client knows how long without parsing the sentence.
        headers = {"Retry-After": str(retry_after)} if retry_after else None
        return JSONResponse({"detail": problem},
                            status_code=429 if retry_after else 401, headers=headers)

    log.info("%s signed in from %s", user.name, client)
    response = JSONResponse({"user": user.public()})
    response.set_cookie(
        COOKIE,
        auth.sessions.issue(user),
        max_age=int(float(os.getenv("AUTH_SESSION_HOURS", "12")) * 3600),
        **auth.cookie_kwargs(request.url.scheme == "https"),
    )
    return response


@app.post("/api/auth/logout")
async def logout(request: Request) -> JSONResponse:
    """Public: signing out must work even when the session has already gone, or someone whose
    cookie expired is left with a Sign out button that returns 401 and appears to do nothing."""
    auth = auth_service()
    user = getattr(request.state, "user", None)
    if user is not None and user.sid:
        auth.sso.drop(user.sid)  # the refresh token dies with the session, not at expiry
    response = JSONResponse({"status": "signed out"})
    # Cleared with the same flags it was set with. The value is already empty and the session is
    # already revoked server-side, so this is cosmetic — but a Set-Cookie without HttpOnly and
    # Secure is what a scanner sees, and a security review that has to be argued down is a cost
    # of its own. This one was: it was reported as "the session cookie is readable by
    # JavaScript", which it is not.
    response.delete_cookie(COOKIE, **auth.cookie_kwargs(request.url.scheme == "https"))
    return response


@app.get("/api/auth/me")
async def me(request: Request) -> JSONResponse:
    """Public on purpose: the page uses it to decide whether to show itself or the login form."""
    user = getattr(request.state, "user", None)
    auth = auth_service()
    body: dict = {
        "authenticated": user is not None,
        "required": auth.enabled,
        "mode": auth.mode,
        "local": auth.mode == "local" or auth.allow_local,
        "user": user.public() if user else None,
        "features": {"snapshot_warming": bool(
            auth.enabled and user is not None and snapshot_warming_enabled())},
    }
    # A personal Microsoft account cannot use the multi-tenant endpoints for an ARM scope, so
    # the page has to offer them a tenant sign-in instead of letting them bounce off a message
    # about needing a work account. Only advertised when a directory has been named for it.
    if auth.sso.enabled and auth.sso.multi_tenant and auth.sso.home_tenant:
        body["home_tenant"] = auth.sso.home_tenant
        # Whether to also advertise the general work-account button. Hiding it needs somewhere
        # else to send people, so it only applies once a home tenant exists.
        body["hide_work_signin"] = auth.sso.hide_work_button
    # Whether the sign-in page should ask for an address before handing over to Microsoft. It is
    # worth asking wherever SSO is on: it skips Entra's account picker, and where two authorities
    # exist it also chooses between them.
    body["email_first"] = auth.sso.email_first
    # Delegated ARM needs a one-time administrator approval, and until it happens nobody can sign
    # in. Telling the page lets it offer that approval to whoever is setting the deployment up,
    # rather than waiting for the first colleague to walk into a refusal and then explaining.
    body["consent_needed"] = auth.sso.consent_needed
    if user is not None and user.source == "entra":
        # Surface the RBAC picture so the header can say what this person is looking at.
        session = auth.sso.get(user.sid)
        if session is not None:
            subs = await auth.sso.subscriptions(session)
            body["subscriptions"] = len(subs)
            body["tenant"] = session.tenant
            body["access_mode"] = "delegated" if auth.sso.delegated else "app-identity"
            if not auth.sso.delegated:
                # In app-identity mode the list is capped by what CloudLens itself can read,
                # and that cap is the difference between "you have no spend" and "CloudLens
                # was never shown your subscriptions". Report it so the page can say which.
                body["app_visible_subscriptions"] = session.app_visible
            if auth.sso.multi_tenant:
                body["tenants"] = session.tenants
                # A directory that would not let this app act for them is why a subscription
                # they expected is missing. Saying nothing turns that into a silent gap.
                if session.skipped:
                    body["skipped_tenants"] = session.skipped
            if session.problem:
                body["problem"] = session.problem
    return JSONResponse(body)


@app.get("/healthz", include_in_schema=False)
async def healthz() -> JSONResponse:
    """Liveness for a load balancer. Deliberately says nothing about the estate."""
    return JSONResponse({"status": "ok"})


@app.get("/readyz", include_in_schema=False)
async def readyz(request: Request) -> JSONResponse:
    """Public readiness exposes only availability, never database or tenant details."""
    probe = getattr(request.app.state, "readiness", None)
    ready = probe is not None and await probe.ready()
    headers = {"Cache-Control": "no-store"}
    if not ready:
        headers["Retry-After"] = "5"
    return JSONResponse(
        {"status": "ready" if ready else "not_ready"},
        status_code=200 if ready else 503, headers=headers,
    )


@app.get("/api/subscriptions")
async def subscriptions(request: Request) -> JSONResponse:
    """Subscriptions available to pick from, with their spend in the loaded period.

    In SSO mode this is the signed-in person's list from ARM, so two colleagues open the same
    URL and legitimately see different pickers.
    """
    from . import cost
    from .warehouse import warehouse

    user = current_user(request)
    allowed = await permitted(user)

    stored = {s["id"]: s for s in await offload(warehouse.subscriptions)}
    try:
        async with as_user(user):
            live = (await cost.list_subscriptions())["subscriptions"]
    except Exception:  # noqa: BLE001 - the warehouse alone is enough to populate the picker
        live = []

    merged: list[dict] = []
    for s in live or stored.values():
        row = stored.get(s["id"], {})
        entry = {"id": s["id"], "name": s.get("name") or row.get("name"),
                 "cost": row.get("cost"), "in_warehouse": s["id"] in stored}
        # Which directory it came from. With one tenant this is noise; with several, two
        # subscriptions can share a name and the picker becomes a guess without it.
        if s.get("tenant_name"):
            entry["tenant_name"] = s["tenant_name"]
        merged.append(entry)
    for sid, row in stored.items():
        if not any(m["id"] == sid for m in merged):
            merged.append({**row, "in_warehouse": True})

    if allowed is not None:
        # Belt and braces: `live` already came back under their own token, but the warehouse
        # rows did not, and those must not widen the picker.
        permitted_ids = set(allowed)
        merged = [m for m in merged if m["id"] in permitted_ids]

    merged.sort(key=lambda m: (m["cost"] is None, -(m["cost"] or 0)))

    # Anything they are entitled to that the warehouse has never loaded is queued here. The
    # picker is the first thing the page asks for, so this is the earliest honest moment to
    # notice that their estate is wider than the ingest was, and to start closing the gap.
    #
    # Scheduled inside `as_user` on purpose. asyncio.create_task copies the current context, so
    # the background ingest inherits this person's delegated tokens and reads their estate with
    # their own access. Outside the block the context is already reset, and the ingest would run
    # as the app -- which in a multi-tenant deployment can see almost nothing, and would leave
    # every warehouse-backed tab empty for everyone but the tenant the app happens to live in.
    from . import backfill, onboarding

    mine = [{"id": m["id"], "name": m.get("name")} for m in merged]

    # Not while first-run setup is still on the table. The backfill closes a *gap* between what
    # someone is entitled to and what the warehouse holds; on a first run there is no gap,
    # there is nothing at all, and the onboarding card is the deliberate path — it explains
    # itself, and it sets up the export pipeline that the backfill's API route never touches.
    # Letting both run means two ingests writing the same rows while a progress card claims to
    # be the one doing the work.
    awaiting_setup = onboarding.settled() is None and not await offload(warehouse.has_rows)

    # Read once, off the event loop, and hand the same answer to both callers below. It used to
    # be read inside each of them, synchronously, which put two full warehouse scans on the loop
    # for every picker request — the picker never got faster on a second load because nothing
    # about it was cached, and while it ran no other request could be served at all.
    have: set[str] | None = None
    if not awaiting_setup and not read_only_mode():
        have = await offload(backfill.present)

    if os.getenv("USER_BACKFILL", "true").lower() != "false" and not awaiting_setup:
        try:
            async with as_user(user):
                if not read_only_mode():
                    backfill.schedule(mine, months=int(os.getenv("INGEST_MONTHS", "3")),
                                      have=have)
        except HTTPException:
            # An expired Azure session must not fail the picker; the page still renders from
            # what the warehouse already holds.
            log.info("skipping backfill: no usable Azure session for %s", user.name)

    return JSONResponse({"subscriptions": merged, "count": len(merged),
                         "restricted": allowed is not None,
                         # Nothing is pending while setup is unanswered: the card is saying so
                         # already, and a second banner repeating it under different words
                         # reads as two different problems.
                         "backfill": ({"pending": [], "failed": [], "loading": False}
                                      if awaiting_setup
                                      else backfill.status_for(mine, have=have))})


@app.get("/api/health")
async def health() -> JSONResponse:
    try:
        return JSONResponse(await agent().health())
    except Exception as exc:  # noqa: BLE001 - health must never raise
        return JSONResponse({"status": "degraded", "detail": str(exc)[:300]})


@app.get("/api/overview")
async def overview(request: Request, scope: str = "", currency: str = "") -> JSONResponse:
    """Unscoped comes from the background snapshot (instant). A scoped view is computed on
    demand — the warehouse part is milliseconds, and only forecast/budgets touch Azure.

    An Entra user is never unscoped: their figures are computed over the subscriptions they
    can actually see, so the tiles at the top of the page are their numbers, not the estate's.
    """
    from . import currency as fx

    user = current_user(request)
    allowed = await permitted(user)
    picked = narrow([s for s in scope.split(",") if s.strip()], allowed)

    def shown(snapshot: dict) -> dict:
        native_months = snapshot.get("month_to_date_by_currency") or []
        native_last = snapshot.get("last_month_by_currency") or []
        if currency and currency.upper() not in {"BILLED"} and (native_months or native_last):
            # The header can convert each native bucket with its own source currency. Never
            # run mixed native amounts through the single-source converter.
            source = {
                key: value for key, value in snapshot.items()
                if key not in {"month_to_date_by_currency", "last_month_by_currency"}
            }
            out = fx.convert_money(source, currency)

            def converted_total(rows: list[dict]) -> float | None:
                converted = [
                    fx.convert_amount(row["cost"], row["currency"], currency)
                    for row in rows
                ]
                return (round(sum(converted), 2)
                        if converted and all(value is not None for value in converted)
                        else None)

            month = converted_total(native_months)
            last = converted_total(native_last)
            out["month_to_date"], out["last_month"] = month, last
            out["month_to_date_by_currency"] = []
            out["last_month_by_currency"] = []
            if month is not None or last is not None:
                out["currency"] = currency.upper()
                out["currency_note"] = None
            else:
                out["currency"] = None
                out["currency_note"] = (
                    f"No authoritative exchange rates are available to convert every "
                    f"selected billing currency to {currency.upper()}."
                )
            return out
        # The snapshot's `currency` is what Azure billed in. Once the figures are converted that
        # label would be wrong, and a wrong currency label on a right number is worse than an
        # unconverted number — the header tiles are the first thing anyone reads.
        out = fx.convert_money(snapshot, currency)
        if out is not snapshot:
            out["currency"] = currency.upper()
            out["converted_from"] = snapshot.get("currency")
        return out

    if not picked and allowed is None:
        return JSONResponse(shown(_overview))

    from . import cost

    timings: dict[str, float] = {}
    failed_phases: set[str] = set()
    request.state.overview_timings = timings
    request.state.overview_failures = failed_phases

    def timed_response(response: JSONResponse) -> JSONResponse:
        phases = _overview_timing_header(timings, failed_phases)
        if phases:
            response.headers["Server-Timing"] = phases
        return response

    try:
        async with as_user(user):
            snapshot = await cost.overview(
                scope=picked, timings=timings, failed_phases=failed_phases)
        snapshot["status"] = "ok"
        return timed_response(JSONResponse(shown(snapshot)))
    except HTTPException:
        raise
    except Exception as exc:  # noqa: BLE001
        return timed_response(JSONResponse({"status": "error", "detail": str(exc)[:200]}))


@app.post("/api/ask")
async def ask(body: Ask, request: Request) -> StreamingResponse:
    # Entitlement first, then readiness: whether someone may ask does not depend on whether the
    # model happens to be up, and a refusal should say so even while it is starting.
    user = current_user(request)
    allowed = await permitted(user)
    a = agent()
    scope = selected_scope(body.scope, allowed)

    names: list[str] = []
    if scope:
        from .warehouse import warehouse

        lookup = {s["id"]: s["name"] for s in await offload(warehouse.subscriptions)}
        names = [lookup.get(s, s) for s in scope]

    log.info("Q(%s): %s  [scope: %s]", user.email or user.name, body.question[:100],
             names or "all")

    async def stream() -> AsyncIterator[str]:
        # The delegated token is established inside the generator, not in the handler: this is
        # where the agent's tool calls actually run, and a ContextVar set outside would not
        # reliably reach them.
        from . import reporting

        with reporting.select(request.query_params.get("period_month")):
            async with as_user(user):
                # Keep only the last few turns; cost answers rarely need deep history and long
                # transcripts make the model slower and more expensive.
                async for event in a.ask(body.question, body.history,
                                         scope=scope, scope_names=names,
                                         authorize_budget=lambda target: authorize_budget(
                                             user, target, scope or allowed)):
                    yield event.sse()

    return StreamingResponse(
        stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.get("/api/warehouse")
async def warehouse_status(request: Request, scope: str = "", job_id: str = "") -> JSONResponse:
    """What the cost warehouse currently holds, plus ingest progress.

    Honours `scope` like every other read. It used to accept only the caller's entitlement and
    ignore the parameter, so the header's "local data" figure stayed at the whole estate while
    every other tile followed the subscription picker — the one number on the page that did not
    agree with the others.
    """
    from .warehouse import warehouse

    user = current_user(request)
    allowed = await permitted(user)
    picked = narrow([s for s in scope.split(",") if s.strip()], allowed)

    # What this is allowed to say about an ingest.
    #
    # An allow-list rather than the raw dict: `warehouse.state` ends a run with `summary()`
    # spread over it, so it carries estate-wide financials that must never reach someone
    # entitled to part of the estate. But the list was these five keys for everyone, and the
    # header reports a refresh with far more than five: the progress strip reads `done`,
    # `total`, `metric`, `mode` and the phase counters, and the outcome toast reads
    # `loaded_rows`, `preserved_days`, `detail`, `error` and `archive`. With none of them
    # present the strip sat on "Starting..." for the whole run and the refresh ended in
    # silence -- no row count, no reason for a failure, no word on the archive. The refresh
    # worked; only the reporting was missing, which looks exactly like a button that does
    # nothing.
    #
    # The extra fields are operational, not financial, and they go to admins only -- which is
    # also the only role that can start a refresh or sees the button at all. Everyone else
    # keeps the minimal projection.
    public = {"status", "started", "job_id", "phase", "finished", "progress", "unit"}
    if getattr(user, "admin", False):
        public = public | {
            "mode", "metric", "total", "done",                      # what is running
            "generating", "submitting", "loading",                  # where it is waiting
            "fetching", "fetching_name", "fetching_bytes", "writing",
            "loaded_rows", "preserved_days", "failed", "empty",     # what it did
            "detail", "error", "archive", "timed_out", "budget_seconds", "omits", "export",
        }
    from . import jobs
    if job_id:
        record = await offload(jobs.get, job_id)
        if not record or (allowed is not None and
                          (not record["scope"] or not set(record["scope"]).issubset(
                              {s.lower() for s in allowed}))):
            raise HTTPException(404, "Job not found in your scope.")
    else:
        recent = await offload(jobs.listing, picked or allowed, limit=1)
        record = recent[0] if recent else None
    raw = warehouse.state
    if record:
        raw = {**record.get("progress", {}), "job_id": record["job_id"],
               "started": record["job_id"], "phase": record["status"],
               "status": "running" if record["status"] in jobs.ACTIVE else record["status"],
               "finished": record.get("finished"),
               "detail": record.get("detail", record.get("progress", {}).get("detail"))}
    state = {key: value for key, value in raw.items() if key in public}
    return JSONResponse({**(await offload(warehouse.summary, scope=picked or allowed)),
                         "ingest": state})


@app.get("/api/diagnostics")
async def diagnostics(request: Request) -> JSONResponse:
    """Measure this instance, rather than reasoning about it from a laptop.

    Refresh timings only make sense against what the hardware can actually do. Two rounds of
    optimisation were aimed at the wrong bottleneck because the numbers being compared came
    from a developer machine: writes measured 2,052 rows/s locally and 39 rows/s here, and the
    gap was attributed first to the query API, then to the filesystem, before anyone measured
    the box itself. This endpoint is the measurement.

    Admin-only, and it writes nothing outside a temporary table.
    """
    import platform
    import time

    from .warehouse import COLUMNS, insert_rows, warehouse

    require_admin(request)

    def _measure() -> dict[str, Any]:
        out: dict[str, Any] = {
            "cpu_count": os.cpu_count(),
            "python": platform.python_version(),
            "pg_host": warehouse._store.host,
            "pg_database": warehouse._store.database,
        }
        try:
            with open("/proc/meminfo") as fh:
                for line in fh:
                    if line.startswith(("MemTotal", "MemAvailable")):
                        out[line.split(":")[0]] = line.split()[1] + " kB"
        except Exception:  # noqa: BLE001 - absent on Windows
            pass

        con = warehouse.connect()
        try:
            out["pg_version"] = con.execute("SELECT version()").fetchone()[0]
            out["pg_schema"] = con.execute("SELECT current_schema()").fetchone()[0]
            # Whether the role model-authored SQL runs as exists. Without it the text checks in
            # `Warehouse.query` are the only thing keeping a question inside the caller's scope.
            out["query_role_isolated"] = bool(warehouse._query_role)
            # The two settings that decide whether a scan is parallel and whether it has
            # room to sort in memory -- the equivalents of the old engine's thread count
            # and memory limit, and the first things to look at when a query is slow.
            out["pg_max_parallel_workers"] = con.execute(
                "SELECT current_setting('max_parallel_workers_per_gather')").fetchone()[0]
            out["pg_work_mem"] = con.execute(
                "SELECT current_setting('work_mem')").fetchone()[0]
            out["rows_in_costs"] = con.execute("SELECT count(*) FROM costs").fetchone()[0]
            # The invariant behind the worst bug this warehouse has had: a subscription stored
            # under two spellings is counted twice, and the total stays plausible while it
            # happens. Cheap enough to check every time someone opens this page.
            out["subscription_identity"] = con.execute(
                'SELECT count(*) FROM costs WHERE "SubAccountId" IS NOT NULL '
                'AND ("SubAccountId" LIKE \'%/%\' OR "SubAccountId" <> lower("SubAccountId"))'
            ).fetchone()[0]

            # A plain CPU yardstick, so the write figure can be read against how fast this box
            # is at all rather than against a laptop.
            t0 = time.perf_counter()
            acc = 0
            for i in range(3_000_000):
                acc += i * 3 % 7
            out["cpu_loop_3m_seconds"] = round(time.perf_counter() - t0, 2)

            # Real rows, round-tripped: read some out and write them straight back into a temp
            # table of the same shape. Synthetic strings will not do -- the table has a DATE
            # column and four DOUBLEs, and a benchmark that ignores that is measuring something
            # the ingest never does.
            names = ",".join(f'"{c}"' for c in COLUMNS)
            con.execute(f"CREATE TEMP TABLE bench AS SELECT {names} FROM costs LIMIT 0")
            sample = [list(r) for r in
                      con.execute(f"SELECT {names} FROM costs LIMIT 2000").fetchall()]
            out["sample_rows"] = len(sample)
            if sample:
                t0 = time.perf_counter()
                insert_rows(con, sample, table="bench")
                dt = time.perf_counter() - t0
                out["insert_seconds"] = round(dt, 2)
                out["insert_rows_per_second"] = round(len(sample) / dt) if dt else None

            t0 = time.perf_counter()
            con.execute('SELECT count(*), sum("BilledCost") FROM costs').fetchone()
            out["scan_costs_seconds"] = round(time.perf_counter() - t0, 3)

            # How much of the loaded data actually carries a tag, and where it came from.
            # "Cost by Tag is empty" has several possible causes -- the route that loaded the
            # data cannot return tags, the export does not contain them, or the estate really
            # is untagged -- and they are indistinguishable from the tab alone.
            tagged, total = con.execute(
                'SELECT count(*) FILTER (WHERE "Tags" IS NOT NULL AND "Tags" NOT IN (\'\', \'{}\')),'
                " count(*) FROM costs").fetchone()
            out["rows_with_tags"] = tagged
            out["rows_total"] = total
            out["recent_loads"] = [
                {"subscription": r[0], "period": r[1], "rows": r[2], "status": r[3],
                 "detail": (r[4] or "")[:80], "at": str(r[5])}
                for r in con.execute(
                    "SELECT subscription_name, period, rows, status, detail, ingested_at "
                    "FROM ingest_log ORDER BY ingested_at DESC LIMIT 8").fetchall()
            ]

            # Duplicate detection.
            #
            # Every loader replaces the range it writes, so a row should appear once. If two
            # loaders disagree about how a subscription is *identified* — a bare GUID versus the
            # ARM path FOCUS uses — the delete misses and both copies survive. The totals still
            # look like money, just twice as much of it, which is the hardest kind of wrong to
            # notice from a dashboard.
            out["subaccount_ids"] = [
                {"id": r[0], "name": r[1], "rows": r[2], "cost": round(r[3] or 0, 2)}
                for r in con.execute(
                    'SELECT "SubAccountId", MAX("SubAccountName"), count(*), sum("BilledCost") '
                    'FROM costs GROUP BY 1 ORDER BY 3 DESC LIMIT 12').fetchall()
            ]
            # `sum()` over `count(*)` widens to `numeric` in PostgreSQL, which psycopg hands
            # back as `Decimal` — and `Decimal` is not JSON serialisable, so the whole endpoint
            # used to 500 on exactly the field this check exists to report. Cast to bigint here,
            # for the same reason sqldialect._round casts its result back: the type has to be
            # right where it is produced, not repaired by every caller downstream. The cost sum
            # is over doubles and already comes back as a float.
            dup_rows, dup_cost = con.execute(
                'SELECT coalesce(sum(n - 1), 0)::bigint, coalesce(sum(c - c / n), 0) FROM ('
                '  SELECT count(*) AS n, sum("BilledCost") AS c FROM costs'
                '  GROUP BY "ChargePeriodStart", "ResourceId", "ServiceName", "BilledCost"'
                '  HAVING count(*) > 1) t').fetchone()
            out["duplicate_rows"] = dup_rows
            out["duplicate_cost"] = round(dup_cost or 0, 2)

            # What the billing units actually look like, for anything that wants to reason in
            # hours. Azure bills per day, so an hourly *profile* is not available at all -- but
            # a per-running-hour rate is, wherever the meter is sold by time and the quantity is
            # the number of hours consumed. This says how much of the estate that covers.
            out["units"] = [
                {"unit": r[0], "rows": r[1], "cost": round(r[2] or 0, 2),
                 "qty": round(r[3] or 0, 2)}
                for r in con.execute(
                    'SELECT "UnitOfMeasure", count(*), sum("BilledCost"), sum("PricingQuantity")'
                    ' FROM costs WHERE "UnitOfMeasure" IS NOT NULL'
                    " AND \"UnitOfMeasure\" <> '' GROUP BY 1 ORDER BY 3 DESC LIMIT 15").fetchall()
            ]
            out["rows_with_quantity"] = con.execute(
                'SELECT count(*) FROM costs WHERE "PricingQuantity" IS NOT NULL'
                ' AND "PricingQuantity" > 0').fetchone()[0]
            out["distinct_charge_days"] = con.execute(
                'SELECT count(DISTINCT "ChargePeriodStart") FROM costs').fetchone()[0]
        finally:
            con.close()

        return out

    return JSONResponse(await asyncio.to_thread(_measure))


@app.get("/api/diagnostics/export")
async def diagnostics_export(request: Request, account: str = "", container: str = "",
                             prefix: str = "") -> JSONResponse:
    """Which columns the configured export actually contains.

    The loader fills any column it cannot find with NULL, which is right — a missing
    PublisherName should not fail a refresh — but it means an export that omits Tags is
    indistinguishable, from the dashboard, from an estate that has none.
    """
    from .exports import describe_export, discover_exports

    user = current_user(request)
    require_admin(request)

    if account and container:
        return JSONResponse(await describe_export(account, container, prefix))

    allowed = await permitted(user)
    found = await discover_exports(allowed or None)
    out = []
    for e in [x for x in (found.get("exports") or []) if x.get("container")][:6]:
        entry = {"export": e.get("name"), "type": e.get("type"),
                 "subscription": e.get("subscription_id")}
        try:
            entry.update(await describe_export(e["storage_account"], e["container"],
                                               e.get("root_folder") or ""))
        except Exception as exc:  # noqa: BLE001 - one unreachable export must not hide the rest
            entry["error"] = str(exc)[:200]
        out.append(entry)
    return JSONResponse({"exports": out})


@app.post("/api/ingest")
async def start_ingest(request: Request, months: int = 3,
                       metric: str = "AmortizedCost", quick: bool = False,
                       scope: str = "", idempotency_key: str = "") -> JSONResponse:
    """Kick off a warehouse refresh. Returns immediately; poll /api/warehouse for progress.

    `quick` swaps the asynchronous Cost Details report for the synchronous Query API: seconds
    instead of minutes, at the cost of the columns a query cannot return.
    """
    from .warehouse import warehouse

    who = require_admin(request)
    if len(idempotency_key) > 128:
        raise HTTPException(400, "The idempotency key must be at most 128 characters.")
    picked = selected_scope(scope.split(","), await permitted(who))
    if _ingest_task and not _ingest_task.done():
        return JSONResponse({"status": "already running", "ingest": warehouse.state})

    # Only the two metrics the UI offers. An unrecognised value would be passed straight to
    # Cost Management and come back as an opaque 400 several minutes into a background job.
    if metric not in ("AmortizedCost", "ActualCost"):
        raise HTTPException(400, "metric must be AmortizedCost or ActualCost")

    log.info("ingest requested by %s (metric=%s, quick=%s)", who.name, metric, quick)
    async with as_user(who):
        task = await _start_ingest_task(
            lambda: _run_ingest(max(1, min(months, 12)), metric, quick=quick, scope=picked),
            scope=picked, payload={"months": max(1, min(months, 12)), "metric": metric, "quick": quick},
            idempotency_key=idempotency_key or None)
    if task is None:
        return JSONResponse({"status": "already running", "ingest": warehouse.state})
    started = _claim_ingest(task, mode="quick" if quick else None, metric=metric)
    return JSONResponse({"status": "started", "months": months, "metric": metric,
                         "quick": quick, "started": started, "job_id": started})



@app.get("/api/exports")
async def list_exports(request: Request) -> JSONResponse:
    """Cost Management exports visible to the signed-in user, best candidates first.

    This is the scalable onboarding path: reading an export that Azure already writes on a
    schedule avoids the per-subscription, per-month API calls that make a large tenant take
    hours to load.
    """
    from .exports import discover_exports

    user = current_user(request)
    # The entitlement is used, not merely checked. `discover_exports()` with no argument
    # enumerates every subscription and billing account the *calling context* can reach --
    # which under AUTH_DELEGATED_ARM=false is the application's own managed identity, not this
    # person. That returned the whole tenant's storage accounts, containers and billing
    # account ids to anyone entitled to a single subscription. `/api/diagnostics/export`
    # above has always passed the scope; this did not.
    allowed = await permitted(user)  # also refuses early if they can see nothing

    try:
        async with as_user(user):
            found = await discover_exports(allowed or None)
    except HTTPException:
        raise
    except Exception as exc:  # noqa: BLE001 - surface the reason rather than an empty list
        raise HTTPException(status_code=502, detail=f"Could not list exports: {exc}") from exc
    return JSONResponse(found)


class ExportIngest(BaseModel):
    scope: list[str] = Field(default_factory=list)
    metric: Literal["FocusCost", "ActualCost", "AmortizedCost"] | None = Field(
        None, description="Require this export type. Omit for legacy ranked discovery."
    )
    export_id: str | None = Field(
        None,
        description="Resource id of an explicitly selected export. Omitted means choose only "
                    "exports targeting this deployment's configured storage.",
    )
    sas_url: str | None = Field(
        None, description="Container SAS URL, for storage where the data plane is firewalled"
    )
    max_files: int = Field(60, ge=1, le=500)
    idempotency_key: str | None = Field(None, max_length=128)


def _sas_source(url: str) -> str:
    """Account and container from a SAS URL, with the token removed.

    The query string is the credential. It travels through logs, API responses and anything
    the browser caches, so it is cut here rather than anywhere further downstream.
    """
    try:
        from urllib.parse import urlsplit

        parts = urlsplit(url)
        host = parts.netloc.split(".", 1)[0]
        return f"{host}{parts.path}".rstrip("/") or "SAS URL"
    except Exception:  # noqa: BLE001 - a label must never be the reason a request fails
        return "SAS URL"


@app.post("/api/exports/ingest")
async def ingest_from_export(body: ExportIngest, request: Request) -> JSONResponse:
    """Load an export's blobs into the warehouse. Poll /api/warehouse for progress."""
    from . import cost

    # `ingest_export` is used by the SAS branch below. It was missing from this import
    # list, so pressing Refresh on a deployment with COST_EXPORT_SAS_URL configured
    # raised NameError inside the background task -- where it was caught and reported
    # to the UI as an ingest failure, which is why it read as a broken export rather
    # than a missing import.
    from .exports import (
        deployment_export_hint,
        discover_exports,
        ingest_export,
        uses_deployment_storage,
    )
    from .warehouse import warehouse

    user = require_admin(request)
    picked = selected_scope(body.scope, await permitted(user))
    if _ingest_task and not _ingest_task.done():
        return JSONResponse({"status": "already running", "ingest": warehouse.state})

    # A SAS URL names its own account and container, so it needs no discovery -- and must not
    # wait on any. Discovery is an ARM call, and the whole reason to hand this endpoint a SAS is
    # that the caller's route to the data does not go through ARM: storage behind a firewall, or
    # an app whose identity has no role on the subscription the export belongs to. Insisting on
    # a successful ARM listing first made the one credential that was always going to work
    # depend on the one that was always going to fail.
    #
    # COST_EXPORT_SAS_URL is the deployment-time answer to the same problem. Where the app's
    # identity cannot list exports at all, the operator configures the URL once and the refresh
    # button works for everyone -- rather than every person who presses it being told to go and
    # find a SAS the UI has no field for.
    sas = body.sas_url or os.getenv("COST_EXPORT_SAS_URL", "").strip()
    if sas and not body.export_id:
        async def run_sas() -> None:
            try:
                await ingest_export(
                    sas_url=sas, max_files=body.max_files, scope=picked,
                    **({"required_metric": body.metric} if body.metric else {}),
                )
                await _archive_after_ingest("FocusCost", source=_sas_source(sas))
            except Exception as exc:  # noqa: BLE001 - record it so the UI can show the reason
                log.exception("SAS export ingest failed")
                warehouse.state = {
                    "status": "failed",
                    "detail": str(exc)[:300],
                    "export": "SAS URL",
                    **warehouse.summary(),
                }

        async with as_user(user):
            task = await _start_ingest_task(run_sas, scope=picked, kind="export",
                                            idempotency_key=body.idempotency_key,
                                            payload={"source": _sas_source(sas), "metric": body.metric})
        if task is None:
            return JSONResponse({"status": "already running", "ingest": warehouse.state})
        return JSONResponse({"status": "started", "export": "SAS URL", "type": "sas",
                             "source": _sas_source(sas),
                             "started": _claim_ingest(task, metric=body.metric)})

    async with as_user(user):
        found = await discover_exports(picked, deployment_only=not bool(body.export_id))
    usable = [e for e in found["exports"] if e.get("container")]

    if body.export_id:
        match = next((e for e in found["exports"] if e["id"] == body.export_id), None)
        if match is None:
            raise HTTPException(status_code=404, detail="No export with that id is visible to you.")
        if body.metric and match.get("type") != body.metric:
            raise HTTPException(status_code=400, detail=(
                f"This export is not {body.metric}. Choose a matching export or use Quick refresh."
            ))
        candidates = [match]
    else:
        candidates = [e for e in usable if uses_deployment_storage(e)]
        if body.metric:
            candidates = [e for e in candidates if e.get("type") == body.metric]
        if not candidates:
            # Distinguish "you have no exports" from "this app cannot see anything at all".
            # They call for opposite actions -- create an export, versus grant the app access --
            # and the second one also rules out the advice the first one wants to give, because
            # 'Latest from Azure' reads cost through the same identity that just came back empty.
            async with as_user(user):
                blind = not found["exports"] and not (await cost.list_subscriptions())["count"]
            raise HTTPException(
                status_code=404,
                detail=(
                    "This app cannot see any subscription, so it can neither list exports nor "
                    "read cost directly. Its managed identity needs Reader on the subscription, "
                    "or set COST_EXPORT_SAS_URL to a container SAS for an export's storage."
                    if blind else
                    deployment_export_hint(body.metric)
                ),
            )
        match = candidates[0]

    if not match["container"]:
        raise HTTPException(
            status_code=400,
            detail=f"Export '{match['name']}' has no storage destination configured.",
        )

    # Pre-flight, before anything is started. Without it, an estate whose export storage is
    # private-endpoint only — the normal case in a governed tenant — gives the same experience
    # every time: press Refresh, watch a spinner for half a minute, get a failure. The check is
    # one listing per account in parallel, so it costs a couple of seconds and turns that into
    # an immediate answer that names the wall.
    if not body.sas_url:
        from .exports import reachable

        async with as_user(user):
            ok, bad = await reachable(candidates)
        if not ok:
            raise HTTPException(
                status_code=400,
                detail=_explain_export_failures([(e.get("storage_account") or "?", err)
                                                 for e, err in bad]),
            )
        # Rank order is preserved, but an export that just answered goes ahead of one that did
        # not: trying a known-unreachable account first only spends the caller's time.
        candidates = ok
        match = candidates[0]

    async def run() -> None:
        result = await _load_export_pair(candidates, sas_url=body.sas_url,
                                         max_files=body.max_files, scope=picked,
                                         **({"required_metric": body.metric} if body.metric else {}))
        if result["loaded"]:
            return
        log.error("every visible export failed across %d candidate(s)",
                  len(result["problems"]))
        # Replace the state rather than merging into it. Merging kept `detail` from
        # whatever ran last, and since the UI prefers detail over error it would show a
        # stale message describing a completely different failure.
        warehouse.state = {
            "status": "failed",
            "detail": _explain_export_failures(result["problems"]),
            "export": match["name"],
            **warehouse.summary(),
        }

    async with as_user(user):
        task = await _start_ingest_task(run, scope=picked, kind="export",
                                        idempotency_key=body.idempotency_key,
                                        payload={"exports": [c["id"] for c in candidates if c.get("id")],
                                                 "metric": body.metric})
    if task is None:
        return JSONResponse({"status": "already running", "ingest": warehouse.state})
    return JSONResponse({
        "status": "started",
        "export": match["name"],
        "type": match["type"],
        "started": _claim_ingest(task, metric=body.metric or match.get("type")),
        "source": f"{match['storage_account']}/{match['container']}/{match['root_folder'] or ''}",
    })


def _explain_export_failures(problems: list[tuple[str, str]]) -> str:
    """One sentence about why no export could be read, not three stack traces spliced together.

    Every one of these failures is the same shape — the export's storage account is unreachable
    or refuses this identity — and the old message concatenated three of them and truncated the
    result at 300 characters, which produced a string that ended mid-word and told the reader
    nothing they could act on. What matters is whether the wall is the network or the role,
    because those need opposite fixes, so that is what gets said.

    Where every failing export belongs to somebody else, Quick refresh is a workaround rather
    than the answer: this deployment has its own storage account, reachable by construction,
    and can create an export into it. Offer that, or a tenant inherits another deployment's
    unreachable exports forever and never learns there was a durable fix.
    """
    if not problems:
        return "No export could be read."

    from .archive import ACCOUNT as own_account

    accounts = sorted({a for a, _ in problems})
    blob = " ".join(e.lower() for _, e in problems)
    network = "does not resolve" in blob or "private-endpoint" in blob
    denied = "denied" in blob or "403" in blob

    named = ", ".join(accounts[:3]) + (f" and {len(accounts) - 3} more" if len(accounts) > 3 else "")
    head = (f"None of the {len(problems)} Cost Management export(s) could be read "
            f"({named}).")

    if network and denied:
        why = ("Some are private-endpoint only and unreachable from this app's network; the rest "
               "refused this identity.")
    elif network:
        why = "Their storage accounts are private-endpoint only and do not resolve from this app."
    elif denied:
        why = "Their storage accounts refused this app's identity."
    else:
        why = problems[0][1][:120]

    # None of them is ours, so there is a real fix rather than only a way around.
    if own_account and own_account not in accounts:
        fix = (f" They all belong to other deployments. Create CloudLens's own export into "
               f"{own_account} from the Exports tab — that account sits in this app's own "
               "network and is reachable by construction. Quick refresh reads cost through ARM "
               "meanwhile.")
    else:
        fix = (" Use Quick refresh instead — it reads cost through ARM and does not touch blob "
               "storage.")

    return f"{head} {why}{fix}"[:700]


@app.get("/api/onboarding")
async def onboarding_state(request: Request) -> JSONResponse:
    """Whether this deployment still needs first-run setup, and how a scan is going.

    Public-ish in the sense that any signed-in person may ask: an empty dashboard needs an
    explanation whoever is looking at it. Only an admin is offered the button.
    """
    from . import cost, onboarding
    from .warehouse import warehouse

    user = current_user(request)
    try:
        allowed = await permitted(user)
    except HTTPException:
        # Signed in but Azure shows them nothing. That is an answer the card should explain,
        # not a reason for it to fail to load.
        allowed = []

    # Only ever compared against zero, so ask whether *a* row exists rather than counting
    # 5.8M of them. `row_count` measured 1.6-1.8s here against ~1ms for EXISTS, and this
    # endpoint is polled while a scan runs — it was the whole of its ~2.6s, on every call,
    # and it never cached because there is nothing here to cache.
    rows = None if await offload(warehouse.has_rows) else 0

    # How many subscriptions they can actually see, asked of Azure rather than of the
    # warehouse. On a first run the warehouse is empty *by definition*, so counting its
    # subscriptions returns zero at exactly the moment the answer matters — and the card
    # would tell someone with a perfectly good estate that they have no access to anything.
    if allowed is not None:
        count = len(allowed)
    else:
        try:
            async with as_user(user):
                count = (await cost.list_subscriptions())["count"]
        except Exception as exc:  # noqa: BLE001 - an ARM hiccup must not break the card
            log.info("onboarding: could not count subscriptions: %s", str(exc)[:200])
            count = len(await offload(warehouse.subscriptions))

    # Whether the first-run button should be offered. In app-identity mode the write runs as
    # CloudLens rather than as this person, so Azure is asked whether they hold a role that
    # could have done it themselves — the same question `assert_may_write_schedule` enforces
    # when the button is actually pressed.
    can_write = bool(user.admin)
    if can_write and user.source == "entra" and not auth_service().sso.delegated:
        session = auth_service().sso.get(user.sid)
        oid = getattr(session, "oid", "") if session else ""
        try:
            async with as_user(user):
                can_write = any(
                    await asyncio.gather(*(cost.principal_can_write_cost(oid, s)
                                           for s in (allowed or [])))
                )
        except Exception as exc:  # noqa: BLE001 - an ARM hiccup must not break the card
            log.info("onboarding: could not check write access: %s", str(exc)[:200])
            can_write = False

    state = onboarding.state(rows, can_write, count, ingesting=_ingest_running())
    if user.admin and not can_write:
        state["blocked"] = (
            "Creating the first export writes to Azure. Without delegated sign-in that write "
            "runs as CloudLens rather than as you, so it needs Cost Management Contributor, "
            "Contributor or Owner on a subscription in scope. Use Quick refresh meanwhile."
        )
    return JSONResponse(state)


def _ingest_running() -> bool:
    """Whether any ingest is in flight — this app's boot-time one, or a manual refresh."""
    from .warehouse import warehouse

    if _ingest_task is not None and not _ingest_task.done():
        return True
    return str((warehouse.state or {}).get("status") or "") == "running"


@app.post("/api/onboarding/scan")
async def onboarding_scan(request: Request, months: int = 3) -> JSONResponse:
    """Find cost data for this deployment and load it. First run only.

    Runs as the signed-in person, so every Azure call — listing exports, creating one,
    reading cost — is authorised against their own access rather than the app's.
    """
    from . import cost, onboarding
    from .warehouse import warehouse

    user = require_schedule_writer(request)
    allowed = await permitted(user)
    if await offload(warehouse.has_rows):
        # Somebody else got there first, or a refresh landed while this page was open. Not an
        # error: the outcome they wanted has already happened.
        raise HTTPException(409, "This deployment already has cost data loaded.")
    if onboarding.settled():
        raise HTTPException(409, "First-time setup has already been completed.")

    async with as_user(user):
        subs = (await cost.list_subscriptions())["subscriptions"]
        if allowed is not None:
            subs = [s for s in subs if s["id"].lower() in {v.lower() for v in allowed}]
        if not subs:
            raise HTTPException(
                400, "Your account cannot see any Azure subscriptions, so there is nothing "
                     "to load.")
        for sub in subs:
            await assert_may_write_schedule(user, sub["id"])
        # Started inside `as_user`: create_task copies the current context, so the scan
        # inherits this person's delegated tokens. Outside the block the context is already
        # reset and every Azure call would go out as the app, which in a multi-tenant
        # deployment can often see nothing at all.
        started = await onboarding.start([{"id": s["id"], "name": s.get("name")} for s in subs],
                                         months=max(1, min(months, 12)))

    if not started:
        return JSONResponse({"status": "already running",
                             "progress": onboarding.progress()})
    log.info("first-run scan started by %s over %d subscription(s)", user.name, len(subs))
    return JSONResponse({"status": "started", "subscriptions": len(subs),
                         "progress": onboarding.progress()})


@app.post("/api/onboarding/dismiss")
async def onboarding_dismiss(request: Request) -> JSONResponse:
    """Stop offering first-run setup. Deliberately irreversible from the UI.

    The offer is meant to be answered once. Someone who dismisses it and later wants data has
    the Refresh button and the Cost exports tab, which is where that job belongs anyway.
    """
    from . import onboarding

    user = require_admin(request)
    await offload(onboarding.settle, "dismissed")
    log.info("first-run setup dismissed by %s", user.name)
    return JSONResponse({"status": "dismissed"})


@app.get("/api/archive")
async def archive_status(request: Request, limit: int = 20) -> JSONResponse:
    """What the daily archive holds, and whether it can be reached.

    Not admin-only: knowing whether a copy of today's numbers exists is part of trusting the
    numbers, and the response carries no cost data — only file names and sizes.
    """
    from . import archive

    current_user(request)
    return JSONResponse(await archive.status(limit=max(1, min(limit, 200))))


@app.post("/api/archive")
async def archive_now(request: Request, metric: str = "FocusCost") -> JSONResponse:
    """Write today's archive from whatever the warehouse currently holds.

    A refresh archives on its own, so this exists for the case where the refresh succeeded but
    the archive did not — a storage outage, a role granted five minutes too late — and someone
    wants today's copy without reloading gigabytes of cost data to get it.
    """
    from . import archive

    require_admin(request)
    try:
        return JSONResponse(await archive.archive_current(metric, source="manual"))
    except archive.ArchiveError as exc:
        raise HTTPException(400, str(exc)) from exc
    except Exception as exc:  # noqa: BLE001
        log.warning("manual archive failed: %s", str(exc)[:300])
        raise HTTPException(502, f"Could not write the archive: {str(exc)[:300]}") from exc


@app.get("/api/archive/compare")
async def archive_compare(request: Request, dataset: str = "actual",
                          earlier: str = "", later: str = "",
                          group_by: str = "ServiceName", top: int = 15,
                          scope: str = "") -> JSONResponse:
    """What changed between two archived snapshots of the same dataset.

    The dashboard can only ever show the latest read. Azure restates cost data for days after
    the fact, so "what did we think this cost when we reported it" is a question the warehouse
    cannot answer at all — the archive exists to answer it, and this is the endpoint that does.
    """
    from . import archive

    user = current_user(request)
    allowed = await permitted(user)
    picked = selected_scope(scope.split(","), allowed)
    if not earlier or not later:
        raise HTTPException(400, "Give two days to compare, as earlier= and later=.")
    try:
        return JSONResponse(await archive.compare(dataset, earlier, later,
                                                  group_by=group_by,
                                                  top=max(1, min(top, 100)),
                                                  scope=picked or None))
    except archive.ArchiveError as exc:
        raise HTTPException(400, str(exc)) from exc
    except Exception as exc:  # noqa: BLE001
        log.warning("archive compare failed: %s", str(exc)[:300])
        raise HTTPException(502, f"Could not compare the archives: {str(exc)[:300]}") from exc


@app.get("/api/schedules")
async def list_schedules(request: Request, scope: str = "", fresh: bool = False) -> JSONResponse:
    """Scheduled Cost Management exports, and where they write.

    A schedule is how a refresh stops taking minutes: Azure writes the data to blob storage on
    its own timetable, and the refresh becomes a blob read rather than a report it has to wait
    for Azure to generate.
    """
    from . import focus_sync, schedules

    user = current_user(request)
    allowed = await permitted(user)
    picked = narrow([s for s in scope.split(",") if s.strip()], allowed)

    try:
        async with as_user(user):
            result = await schedules.listing(picked or allowed, fresh=fresh)

            # One ARM role lookup per subscription, asked for all of them at once.
            #
            # This loop used to await inside itself, so an estate of a dozen subscriptions paid
            # a dozen sequential round trips before the Reports tab could draw anything --
            # measured at 16.5s for three subscriptions here. The checks are independent; only
            # the waiting was shared.
            async def writable_state(sub: dict) -> None:
                sub["can_write"] = False
                if not user.admin:
                    sub["write_reason"] = (
                        "A CloudLens administrator must approve Azure export changes.")
                    return
                try:
                    await assert_may_write_schedule(user, sub["id"])
                    sub["can_write"] = True
                except HTTPException as exc:
                    if exc.status_code != 403:
                        raise
                    sub["write_reason"] = str(exc.detail)

            await asyncio.gather(*(writable_state(s) for s in result["subscriptions"]))
            writable = {sub["id"] for sub in result["subscriptions"] if sub["can_write"]}
            result["can_write"] = bool(writable)
            for schedule in result["schedules"]:
                schedule["can_write"] = schedule["subscription_id"] in writable
            result["sync"] = await focus_sync.status([s["id"] for s in result["subscriptions"]])
            if result.get("errors"):
                result["error"] = "Some subscription schedules could not be read. " + "; ".join(
                    f"{e['subscription']}: {e['detail']}" for e in result["errors"])
            return JSONResponse(result)
    except HTTPException:
        raise
    except Exception as exc:  # noqa: BLE001 - the tab must still render
        log.info("could not list schedules: %s", str(exc)[:200])
        return JSONResponse({"schedules": [], "count": 0, "error": str(exc)[:300],
                             "metrics": list(schedules.METRICS), "subscriptions": []})


class ScheduleRequest(BaseModel):
    """A daily export to create, so refreshes stop waiting on the Cost Details API."""

    subscription: str = Field(min_length=1, max_length=64)
    metric: str = Field("FocusCost", max_length=24)
    recurrence: str = Field("Daily", max_length=12)
    run_now: bool = True
    etag: str | None = None


def require_schedule_writer(request: Request) -> User:
    return require_admin(request)


async def assert_may_write_schedule(user: User, subscription: str) -> None:
    """Azure decides a write, in both modes.

    With a delegated token Azure enforces the caller's rights directly. Without one the write
    would execute as the application — which holds Cost Management Contributor and would carry
    it out just as happily for a Reader — so the person's own role is checked first. Refusing
    outright was the old behaviour and it made app-identity deployments unable to create the
    one export they can actually reach, leaving them on the slow ARM path permanently.
    """
    if user.source != "entra" or auth_service().sso.delegated:
        return

    from . import cost

    session = auth_service().sso.get(user.sid)
    oid = getattr(session, "oid", "") if session else ""
    async with as_user(user):
        allowed = await cost.principal_can_write_cost(oid, subscription)
    if not allowed:
        raise HTTPException(
            403,
            "Creating an export writes to Azure, and without delegated sign-in that write runs "
            "as CloudLens rather than as you — so Azure is asked whether you could have done it "
            "yourself. This needs Cost Management Contributor, Contributor or Owner on that "
            "subscription; Cost Management Reader is not enough.",
        )


@app.post("/api/schedules")
async def create_schedule(body: ScheduleRequest, request: Request) -> JSONResponse:
    """Create a scheduled export writing to this deployment's own archive container.

    Runs as the signed-in person where delegated ARM is on, so Azure enforces their rights
    directly — the same reasoning as budget creation, and for the same reason: this writes.
    """
    import json

    from . import archive, focus_sync, schedules

    user = require_schedule_writer(request)
    allowed = await permitted(user)
    if allowed is not None and body.subscription.lower() not in {s.lower() for s in allowed}:
        raise HTTPException(403, "That subscription is not one you have access to.")
    await assert_may_write_schedule(user, body.subscription)
    from .warehouse import warehouse
    compact = await offload(warehouse.get_meta, "focus-daily:" + body.subscription.lower())
    if compact and (json.loads(compact).get("version") == 2 or
                    (json.loads(compact).get("enabled") and json.loads(compact).get("mode") == "compact")):
        raise HTTPException(409, "Compact sync owns this export. Preview a daily-mode change before enabling a native cadence.")
    if not archive.enabled():
        raise HTTPException(400, "No archive storage is configured to write the export to.")

    # The destination is always *this* deployment's archive account, wherever the subscription
    # being exported happens to live. Deriving it from `body.subscription` produced a resource
    # id that simply does not exist as soon as anyone scheduled an export for a second
    # subscription — the account is in one place, the data comes from several.
    account_id = archive.ACCOUNT_ID
    if not account_id:
        raise HTTPException(
            400,
            "ARCHIVE_ACCOUNT_ID is not configured, so there is no storage account to write the "
            "export to. Set it to the full resource id of the archive storage account.",
        )
    try:
        async with as_user(user):
            baseline = await focus_sync.capture(body.subscription) if body.run_now else None
            requested_at = datetime.now(UTC).isoformat()
            made = await schedules.create(
                body.subscription, body.metric,
                account_id=account_id,
                container=schedules.FOCUS_CONTAINER,
                recurrence=body.recurrence,
                run_now=body.run_now,
                etag=body.etag,
            )
    except schedules.ScheduleError as exc:
        raise HTTPException(400, str(exc)) from exc
    except HTTPException:
        raise
    except Exception as exc:  # noqa: BLE001
        log.warning("schedule create failed: %s", str(exc)[:300])
        raise HTTPException(502, f"Azure refused the export: {str(exc)[:300]}") from exc

    log.info("schedule %s created on %s by %s", made["name"], body.subscription, user.name)
    if made.get("first_run_started"):
        made["sync_started"] = await _queue_focus_import(
            user, body.subscription, baseline, requested_at=requested_at)
        made["sync_note"] = ("Waiting for the report, then ingesting changed cost days."
                            if made["sync_started"] else "Another import is running. Automatic FOCUS sync will pick up this report.")
    elif body.run_now:
        await focus_sync.record(
            body.subscription, status="failed", started=datetime.now(UTC).isoformat(),
            finished=datetime.now(UTC).isoformat(), awaiting_runs=baseline,
            detail="The schedule was saved, but its first run did not start. "
                   + (made.get("first_run_error") or "Use Refresh latest to retry."),
        )
    return JSONResponse(made, status_code=201)


async def _queue_focus_import(user: User, subscription: str, baseline: list, *,
                             requested_at: str) -> bool:
    from uuid import uuid4

    from . import focus_sync, focus_workflow, jobs

    await focus_sync.record(subscription, status="waiting", started=datetime.now(UTC).isoformat(),
                            finished=None, awaiting_runs=baseline,
                            detail="Azure was asked for a new report. Automatic ingestion will follow.")
    async with as_user(user):
        today = datetime.now(UTC).date()
        plan = await focus_workflow.preview(
            [{"id": subscription}], start=today.replace(day=1).isoformat(),
            end=today.isoformat(), purpose="refresh")
        export = next((row.get("export") for row in plan.get("subscriptions", [])), None)
        if export and export.get("timeframe") == "TheLastMonth":
            previous = today.replace(day=1) - timedelta(days=1)
            plan = await focus_workflow.preview(
                [{"id": subscription}], start=previous.replace(day=1).isoformat(),
                end=previous.isoformat(), purpose="refresh")
        record = await focus_workflow.create(plan, idempotency_key=str(uuid4()))
        for task in record["tasks"]:
            await offload(jobs.update, record["job_id"], task_id=task["task_id"],
                          requested_at=requested_at, baseline=baseline or [],
                          status="waiting-for-Azure")
        record = await offload(jobs.get, record["job_id"])
        await focus_sync.record(subscription, started=record["job_id"], job_id=record["job_id"],
                                status="waiting-for-Azure", finished=None, awaiting_runs=baseline)
        focus_workflow.launch(record)
    return True


class FocusRefreshRequest(BaseModel):
    scope: list[str] = Field(default_factory=list, max_length=200)


class FocusJobRequest(BaseModel):
    scope: list[str] = Field(default_factory=list, max_length=200)
    purpose: Literal["bootstrap", "reconcile", "verify"] = "bootstrap"
    start: str | None = None
    end: str | None = None
    preview_token: str = ""
    idempotency_key: str = Field("", max_length=128)


class CostDataRequest(BaseModel):
    scope: list[str] | None = Field(None, max_length=200)
    cadence: Literal["Daily", "Weekly", "Monthly"] = "Daily"
    action: Literal["setup", "schedule", "frequency", "refresh", "reconcile"] = "setup"
    start: str | None = None
    end: str | None = None
    history_days: int | None = Field(None, strict=True, ge=1, le=365)
    preview_token: str = ""
    idempotency_key: str = Field("", max_length=128)


async def _cost_data_subscriptions(scope: list[str] | None, user: User) -> list[dict]:
    from . import cost
    if scope is not None and not any(s.strip() for s in scope):
        raise HTTPException(400, "Choose at least one subscription.")
    selected = selected_scope(scope or [], await permitted(user))
    async with as_user(user):
        subs = await cost._resolve(selected)
    if not subs:
        raise HTTPException(400, "Select at least one accessible subscription.")
    return subs


async def _assert_cost_data_approval(user: User, subscription: str) -> None:
    await assert_may_write_schedule(user, subscription)
    # The existing guard relies on delegated Azure writes to enforce the person's role.
    # This pipeline deliberately runs as the workload, so also prove that person's right
    # to authorize it when SSO is delegated.
    if user.source == "entra" and auth_service().sso.delegated:
        from . import cost
        session = auth_service().sso.get(user.sid)
        oid = getattr(session, "oid", "") if session else ""
        async with as_user(user):
            allowed = await cost.principal_can_write_cost(oid, subscription)
        if not allowed:
            raise HTTPException(403, "Your Azure export write access could not be verified. "
                                "A Cost Management Contributor must authorize background setup for this subscription.")


@app.get("/api/cost-data")
async def cost_data_status(request: Request, scope: str | None = None) -> JSONResponse:
    from . import cost_data
    user = current_user(request)
    subs = await _cost_data_subscriptions(scope.split(",") if scope is not None else None, user)
    result = await offload(cost_data.status, subs)
    result["can_review"] = bool(user.admin) and not read_only_mode()
    result["read_only"] = read_only_mode()
    return JSONResponse(result)


@app.get("/api/cost-data/subscriptions")
async def cost_data_subscriptions(request: Request) -> JSONResponse:
    """Entitled catalog only: unlike the legacy picker this never schedules backfill."""
    from . import cost
    user = current_user(request)
    allowed = await permitted(user)
    async with as_user(user):
        catalog = (await cost.list_subscriptions())["subscriptions"]
    permitted_ids = {s.lower() for s in allowed} if allowed is not None else None
    return JSONResponse({"subscriptions": [
        {"id": s["id"].lower(), "name": s.get("name") or s["id"]}
        for s in catalog if permitted_ids is None or s["id"].lower() in permitted_ids
    ]})


async def _cost_data_plan(body: CostDataRequest, user: User, subs: list[dict]) -> dict:
    from . import cost, cost_data
    # Do not mistake delegated SSO access for the identity used after the browser closes.
    token, identity = cost._caller.set(None), cost._caller_id.set("server")
    try:
        result = await cost_data.preview(subs, cadence=body.cadence, start=body.start,
                                         end=body.end, action=body.action, history_days=body.history_days)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    finally:
        cost._caller.reset(token)
        cost._caller_id.reset(identity)
    for row in result["subscriptions"]:
        row["can_write"] = False
        if user.admin:
            try:
                await _assert_cost_data_approval(user, row["subscription"])
                row["can_write"] = True
            except HTTPException as exc:
                if exc.status_code != 403:
                    raise
                row["blockers"].append(str(exc.detail))
    result["can_start"] = (not read_only_mode() and bool(user.admin) and
                           all(r["can_write"] and not r["blockers"] for r in result["subscriptions"]))
    return result


@app.post("/api/cost-data/preview")
async def preview_cost_data(body: CostDataRequest, request: Request) -> JSONResponse:
    body = _validate_cost_data_request(body)
    user = current_user(request)
    subs = await _cost_data_subscriptions(body.scope, user)
    return JSONResponse(await _cost_data_plan(body, user, subs))


def _validate_cost_data_request(body: CostDataRequest) -> CostDataRequest:
    from . import cost_data
    if "scope" in body.model_fields_set and body.scope is None:
        raise HTTPException(400, "Choose at least one subscription; an explicit null scope is not a selection.")
    try:
        if body.action == "setup":
            lo, hi, count = cost_data.history_window(body.start, body.end, body.history_days)
            return body.model_copy(update={"start": lo, "end": hi, "history_days": count})
        if body.history_days is not None:
            raise ValueError("History length is only used for first-time import.")
        if body.action == "schedule" and (body.start is not None or body.end is not None):
            raise ValueError("Schedule-only approval does not accept history dates. Use First-time import for history.")
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    return body


@app.post("/api/cost-data/setup")
async def start_cost_data(body: CostDataRequest, request: Request) -> JSONResponse:
    from . import cost_data, focus_workflow, jobs
    body = _validate_cost_data_request(body)
    user = require_schedule_writer(request)
    subs = await _cost_data_subscriptions(body.scope, user)
    for sub in subs:
        await _assert_cost_data_approval(user, sub["id"])
    if not body.idempotency_key or not body.preview_token:
        raise HTTPException(409, "Review the exact setup before starting.")
    previous = await offload(jobs.find_request, "focus-setup", [s["id"] for s in subs], body.idempotency_key)
    if previous:
        if (previous["payload"].get("preview_token") != body.preview_token or
                previous["payload"].get("cadence") != body.cadence or
                previous["payload"].get("action") != body.action or
                (body.history_days is not None and previous["payload"].get("history_days", body.history_days) != body.history_days) or
                (body.start and previous["payload"].get("from") != body.start) or
                (body.end and previous["payload"].get("to") != body.end)):
            raise HTTPException(409, "This approval key already belongs to a different setup.")
        focus_workflow.launch(previous)
        return JSONResponse(previous, status_code=202)
    plan = await _cost_data_plan(body, user, subs)
    if not plan["can_start"] or plan["preview_token"] != body.preview_token:
        raise HTTPException(409, "Setup or access changed. Review again before starting.")
    try:
        record = await cost_data.create(plan, body.idempotency_key)
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc
    focus_workflow.launch(record)
    return JSONResponse(record, status_code=202)


async def _focus_plan(body: FocusJobRequest, user: User) -> dict:
    from . import cost, focus_workflow
    selected = selected_scope(body.scope, await permitted(user))
    async with as_user(user):
        subs = await cost._resolve(selected)
        if not subs:
            raise HTTPException(400, "Select at least one accessible subscription.")
        if body.purpose in {"bootstrap", "verify"}:
            start, end = focus_workflow.window(7 if body.purpose == "verify" else 90)
        else:
            if not body.start or not body.end:
                raise HTTPException(400, "Reconciliation needs an explicit start and end.")
            start, end = body.start, body.end
        try:
            identity = ("Delegated signed-in caller" if cost._caller.get() is not None else
                        "Application workload identity, authorized by the signed-in administrator")
            result = await focus_workflow.preview(
                subs, start=start, end=end, purpose=body.purpose,
                identity=identity + "; interrupted manual jobs require fresh authorization to resume")
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
    return result


@app.post("/api/focus/preview")
async def focus_bootstrap_preview(body: FocusJobRequest, request: Request) -> JSONResponse:
    user = current_user(request)
    result = await _focus_plan(body, user)
    result["can_start"] = bool(user.admin)
    for sub in result["subscriptions"]:
        sub["can_write"] = False
        if user.admin:
            try:
                await assert_may_write_schedule(user, sub["subscription"])
                sub["can_write"] = True
            except HTTPException as exc:
                if exc.status_code != 403:
                    raise
                sub["write_reason"] = str(exc.detail)
    result["can_start"] = not read_only_mode() and all(
        s["can_write"] for s in result["subscriptions"])
    result["read_only"] = read_only_mode()
    return JSONResponse(result)


@app.post("/api/focus/jobs")
async def start_focus_job(body: FocusJobRequest, request: Request) -> JSONResponse:
    from . import focus_workflow, jobs
    user = require_schedule_writer(request)
    plan = await _focus_plan(body, user)
    for sub in plan["subscriptions"]:
        await assert_may_write_schedule(user, sub["subscription"])
    if body.idempotency_key:
        previous = await offload(jobs.find_request, "focus-" + body.purpose,
                                 [sub["subscription"] for sub in plan["subscriptions"]],
                                 body.idempotency_key)
        if previous:
            if body.purpose == "reconcile" and (
                    previous["payload"].get("from"), previous["payload"].get("to")) != (body.start, body.end):
                raise HTTPException(409, "The idempotency key already names a different date window.")
            async with as_user(user):
                focus_workflow.launch(previous)
            return JSONResponse(previous, status_code=202)
    if not body.idempotency_key or body.preview_token != plan["preview_token"]:
        raise HTTPException(409, "Preview the exact dates, identities and destination before starting.")
    try:
        record = await focus_workflow.create(plan, idempotency_key=body.idempotency_key,
                                             force_new=body.purpose != "bootstrap")
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc
    async with as_user(user):
        focus_workflow.launch(record)
    return JSONResponse(record, status_code=202)


async def _authorized_job(job_id: str, user: User) -> dict:
    from . import jobs
    allowed = await permitted(user)
    record = await offload(jobs.get, job_id)
    if not record or (allowed is not None and (not record["scope"] or
            not set(record["scope"]).issubset({s.lower() for s in allowed}))):
        raise HTTPException(404, "Job not found in your scope.")
    return record


@app.get("/api/focus/jobs")
async def focus_jobs(request: Request, scope: str = "") -> JSONResponse:
    from . import jobs
    user = current_user(request)
    selected = selected_scope(scope.split(","), await permitted(user))
    return JSONResponse({"jobs": [r for r in await offload(jobs.listing, selected)
                                  if r["kind"].startswith("focus-")]})


@app.get("/api/focus/jobs/{job_id}")
@app.get("/api/jobs/{job_id}")
async def focus_job_status(job_id: str, request: Request) -> JSONResponse:
    return JSONResponse(await _authorized_job(job_id, current_user(request)))


@app.post("/api/jobs/{job_id}/resume")
async def resume_ingest_job(job_id: str, request: Request) -> JSONResponse:
    from . import ingest_jobs, jobs
    user = require_admin(request)
    record = await _authorized_job(job_id, user)
    if record["kind"].startswith("focus-"):
        return await resume_focus_job(job_id, request)
    if not record["tasks"] and ingest_jobs.resumable_request(record):
        if record["kind"] == "onboarding":
            for sub in record["scope"]:
                await assert_may_write_schedule(user, sub)
        record = await offload(jobs.update, job_id, status="queued", finished=None)
        async with as_user(user):
            jobs.spawn(record, lambda: ingest_jobs.resume_request(record))
        return JSONResponse(record, status_code=202)
    if not record["tasks"] or record["tasks"][0].get("kind") not in {"quick", "details"}:
        raise HTTPException(400, "This legacy job has no resumable date tasks. Start a new authorized refresh.")
    for task in record["tasks"]:
        if task["status"] != "ready":
            await offload(jobs.update, job_id, task_id=task["task_id"], status="queued")
    record = await offload(jobs.update, job_id, status="queued", finished=None)
    subs = [{"id": t["subscription"], "name": t["name"]} for t in record["tasks"]]
    first = record["tasks"][0]
    async with as_user(user):
        jobs.spawn(record, lambda: ingest_jobs.ingest(
            subs, 1, first["metric"], quick=first["kind"] == "quick"))
    return JSONResponse(record, status_code=202)


@app.post("/api/focus/jobs/{job_id}/resume")
async def resume_focus_job(job_id: str, request: Request, scope: str | None = None) -> JSONResponse:
    from . import focus_sync, focus_workflow, jobs
    user = require_schedule_writer(request)
    record = await _authorized_job(job_id, user)
    if scope is not None:
        selected = {s.strip().lower() for s in scope.split(",") if s.strip()}
        if not selected or not set(record["scope"]).issubset(selected):
            raise HTTPException(409, "This job also covers other subscriptions. Select all of its subscriptions before retrying.")
        selected_scope(list(selected), await permitted(user))
    if not record["kind"].startswith("focus-"):
        raise HTTPException(400, "This is not a FOCUS window job.")
    for sub in record["scope"]:
        if record["kind"] in {"focus-setup", "focus-update"}:
            await _assert_cost_data_approval(user, sub)
        else:
            await assert_may_write_schedule(user, sub)
    async with as_user(user):
        if record["kind"] == "focus-sync":
            record = await offload(jobs.update, job_id, status="queued", finished=None)
            jobs.spawn(record, lambda: focus_sync.run(record["scope"]))
        else:
            record = await focus_workflow.resume(job_id)
            focus_workflow.launch(record)
    return JSONResponse(record, status_code=202)


class FocusDailyRequest(BaseModel):
    scope: list[str] = Field(default_factory=list, max_length=200)
    mode: Literal["compact", "native-month-to-date"] = "compact"
    lookback: int = Field(7, ge=1, le=31)
    preview_token: str = ""


async def _daily_focus_plan(body: FocusDailyRequest, user: User) -> dict:
    from . import cost, focus_workflow
    selected = selected_scope(body.scope, await permitted(user))
    async with as_user(user):
        scope = [s["id"] for s in await cost._resolve(selected)]
    if not scope:
        raise HTTPException(400, "Select at least one accessible subscription.")
    # Readiness must be checked using the identity which will actually run after restart.
    token, identity = cost._caller.set(None), cost._caller_id.set("server")
    try:
        return await focus_workflow.daily_preview(scope, body.mode, body.lookback)
    finally:
        cost._caller.reset(token)
        cost._caller_id.reset(identity)


@app.post("/api/focus/daily/preview")
async def preview_daily_focus(body: FocusDailyRequest, request: Request) -> JSONResponse:
    return JSONResponse(await _daily_focus_plan(body, current_user(request)))


@app.post("/api/focus/daily")
async def save_daily_focus(body: FocusDailyRequest, request: Request) -> JSONResponse:
    from . import focus_workflow
    user = require_schedule_writer(request)
    plan = await _daily_focus_plan(body, user)
    for sub in plan["subscriptions"]:
        await assert_may_write_schedule(user, sub["subscription"])
    if plan["preview_token"] != body.preview_token:
        raise HTTPException(409, "Preview the workload identity and native cadence change first.")
    try:
        async with as_user(user):
            result = await focus_workflow.configure_daily(plan)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    return JSONResponse(result)


@app.get("/api/schedules/refresh")
async def focus_refresh_status(request: Request, scope: str = "") -> JSONResponse:
    from . import cost, focus_sync

    user = current_user(request)
    selected = selected_scope(scope.split(","), await permitted(user))
    async with as_user(user):
        subs = await cost._resolve(selected)
    return JSONResponse(await focus_sync.status([s["id"] for s in subs]))


@app.post("/api/schedules/refresh")
async def refresh_latest_focus(body: FocusRefreshRequest, request: Request) -> JSONResponse:
    from uuid import uuid4

    from . import cost, focus_sync, focus_workflow

    user = require_schedule_writer(request)
    selected = selected_scope(body.scope, await permitted(user))
    if _ingest_task and not _ingest_task.done():
        raise HTTPException(409, "Another cost import is running. Wait for it to finish before refreshing.")
    async with as_user(user):
        subs = await cost._resolve(selected)
        scope = [s["id"] for s in subs]
        if not scope:
            raise HTTPException(400, "No accessible subscriptions to refresh.")
        for sub in scope:
            await assert_may_write_schedule(user, sub)
        configured = {s["subscription_id"] for s in await focus_sync.owned(scope)}
        missing = [s["name"] for s in subs if s["id"] not in configured]
        if missing:
            raise HTTPException(400, "Create a FOCUS schedule in Cost exports first: " + ", ".join(missing))
        today = datetime.now(UTC).date()
        plan = await focus_workflow.preview(
            subs, start=today.replace(day=1).isoformat(), end=today.isoformat(), purpose="refresh")
        record = await focus_workflow.create(plan, idempotency_key=str(uuid4()), force_new=True)
        for sub in scope:
            await focus_sync.record(sub, status="waiting-for-Azure", started=record["job_id"],
                                    job_id=record["job_id"], finished=None,
                                    detail="Current-month execution queued; completed costs remain available.")
        focus_workflow.launch(record)
    return JSONResponse({"status": "started", "scope": scope, "job_id": record["job_id"],
                         "started": record["job_id"],
                         "detail": "Generating current-month FOCUS reports, then ingesting changed cost days."},
                        status_code=202)


@app.post("/api/schedules/run")
async def run_schedule(request: Request, subscription: str, name: str) -> JSONResponse:
    """Produce a scheduled export now instead of waiting for its next run."""
    from . import focus_sync, schedules

    user = require_schedule_writer(request)
    allowed = await permitted(user)
    if allowed is not None and subscription.lower() not in {s.lower() for s in allowed}:
        raise HTTPException(403, "That subscription is not one you have access to.")
    await assert_may_write_schedule(user, subscription)
    try:
        async with as_user(user):
            ours = name == schedules.export_name("FocusCost", **schedules.destination(subscription))
            baseline = await focus_sync.capture(subscription) if ours else None
            requested_at = datetime.now(UTC).isoformat()
            result = await schedules.run_now(subscription, name)
            if ours:
                result["sync_started"] = await _queue_focus_import(
                    user, subscription, baseline, requested_at=requested_at)
            return JSONResponse(result)
    except schedules.ScheduleError as exc:
        raise HTTPException(400, str(exc)) from exc


@app.delete("/api/schedules")
async def delete_schedule(request: Request, subscription: str, name: str) -> JSONResponse:
    """Remove a schedule. Data it has already written is left in place."""
    from . import schedules

    user = require_schedule_writer(request)
    allowed = await permitted(user)
    if allowed is not None and subscription.lower() not in {s.lower() for s in allowed}:
        raise HTTPException(403, "That subscription is not one you have access to.")
    await assert_may_write_schedule(user, subscription)
    try:
        async with as_user(user):
            return JSONResponse(await schedules.remove(subscription, name))
    except schedules.ScheduleError as exc:
        raise HTTPException(400, str(exc)) from exc


@app.post("/api/archive/export")
async def export_selection(request: Request, scope: str = "", label: str = "",
                           fmt: str = "csv", since: str = "", until: str = "",
                           source: str = "", dataset: str = "actual") -> JSONResponse:
    """Export chosen subscriptions, over a chosen date range, from a chosen day's read.

    Two different meanings of "date", kept apart on purpose:

      * `since`/`until` narrow the **cost period** — which days of spend end up in the file.
      * `source` picks **which read** they come from: today's warehouse, or an archived snapshot
        from an earlier day. Azure restates cost data for days afterwards, so August as read on
        the 27th is not August as read on the 28th.

    CSV by default, in the same column order a FOCUS export produces, so the file is
    interchangeable with one Azure wrote.
    """
    from . import archive

    user = current_user(request)
    allowed = await permitted(user)
    picked = narrow([s for s in scope.split(",") if s.strip()], allowed)
    if not picked:
        raise HTTPException(400, "Select at least one subscription to export.")

    try:
        return JSONResponse(await archive.export_selection(
            scope=picked, fmt=fmt, label=label,
            since=since or None, until=until or None,
            source=source or None, dataset=dataset))
    except archive.ArchiveError as exc:
        raise HTTPException(400, str(exc)) from exc
    except Exception as exc:  # noqa: BLE001
        log.warning("scoped export failed: %s", str(exc)[:300])
        raise HTTPException(502, f"Could not write the export: {str(exc)[:300]}") from exc


@app.get("/api/archive/export/download")
async def download_selection(request: Request, scope: str = "", label: str = "",
                             fmt: str = "csv", since: str = "", until: str = "",
                             source: str = "", dataset: str = "actual") -> Response:
    """The same selected export, sent to the browser instead of to storage.

    A GET rather than a POST, and reached from a real link, because that is what makes the
    browser download a file natively — with its own progress and its own downloads list —
    instead of the page having to fetch the bytes and synthesise a click. It is the same
    reasoning the report builder's download links already follow.

    Deliberately does not require the archive to be configured. Writing a copy to storage needs
    an account; handing someone their own data does not, and refusing to export because there is
    nowhere to file a second copy would be a strange thing to insist on.
    """
    from . import archive

    user = current_user(request)
    allowed = await permitted(user)
    picked = narrow([s for s in scope.split(",") if s.strip()], allowed)
    if not picked:
        raise HTTPException(400, "Select at least one subscription to export.")

    try:
        built = await archive.build_selection(
            scope=picked, fmt=fmt, label=label,
            since=since or None, until=until or None,
            source=source or None, dataset=dataset)
    except archive.ArchiveError as exc:
        raise HTTPException(400, str(exc)) from exc
    except Exception as exc:  # noqa: BLE001
        log.warning("scoped export download failed: %s", str(exc)[:300])
        raise HTTPException(502, f"Could not build the export: {str(exc)[:300]}") from exc

    return Response(
        content=built["data"],
        media_type=built["content_type"],
        headers={
            "Content-Disposition": f'attachment; filename="{built["filename"]}"',
            # The row count is worth having without opening the file, and a link download gives
            # the page no other way to learn it.
            "X-CloudLens-Rows": str(built["rows"]),
            "Cache-Control": "no-store",
        },
    )


@app.get("/api/currency")
async def currency_options(request: Request, refresh: bool = False) -> JSONResponse:
    """Which display currencies this deployment can honestly offer, and on what authority.

    USD is always available where the data carries `CostInUsd` — Azure records the USD
    equivalent on the invoice itself, so it needs no exchange rate. Everything else uses a
    published reference rate fetched daily, and the response carries the source and the date so
    a converted figure can be quoted rather than presented as a fact from nowhere.
    """
    from . import currency

    current_user(request)
    if refresh:
        await currency.refresh(force=True)
    return JSONResponse(await currency.available())


@app.get("/api/dashboard/shutdown")
async def dashboard_shutdown(request: Request, days: int = 14, scope: str = "",
                             currency: str = "") -> JSONResponse:
    """VMs billed for 168 hours a week that are only used for about 60.

    Needs hourly CPU rather than the daily average `rightsizing` uses — the whole question is
    *when* a machine is busy, and a daily mean cannot tell 09:00–18:00 from 03:00.
    """
    from . import shutdown

    user = current_user(request)
    allowed = await permitted(user)
    picked = narrow([s for s in scope.split(",") if s.strip()], allowed)

    try:
        async with as_user(user):
            found = await shutdown.find_schedulable(picked or None, days=days, currency=currency or None)
        return JSONResponse(found)
    except HTTPException:
        raise
    except Exception as exc:  # noqa: BLE001 - one failing tab must not look like an outage
        log.warning("shutdown tab failed: %s", str(exc)[:200])
        raise HTTPException(502, f"Could not analyse schedules: {str(exc)[:200]}") from exc


@app.get("/api/dashboard/anomalies")
async def dashboard_anomalies(request: Request, days: int = 60, scope: str = "",
                              currency: str = "") -> JSONResponse:
    """Days that did not look like the others.

    Every other tab answers a question someone thought to ask; this one has to speak first. A
    cost spike is expensive because nobody noticed it for three weeks, and nobody notices by
    reading a chart every morning.

    Warehouse-only, so it lands in milliseconds and needs no Azure call.
    """
    from . import anomalies

    user = current_user(request)
    allowed = await permitted(user)
    picked = narrow([s for s in scope.split(",") if s.strip()], allowed)

    try:
        async with as_user(user):
            return JSONResponse(await offload(anomalies.analyse, picked or None,
                                              days=max(1, min(days, 365)),
                                              display_currency=currency or None,
                                              filters=request_filters(request)))
    except HTTPException:
        raise
    except anomalies.AnomalyError as exc:
        raise HTTPException(400, str(exc)) from exc
    except Exception as exc:  # noqa: BLE001 - one failing tab must not look like an outage
        log.warning("anomaly tab failed: %s", str(exc)[:200])
        raise HTTPException(502, f"Could not analyse anomalies: {str(exc)[:200]}") from exc


@app.post("/api/dashboard/anomalies/root-cause")
async def dashboard_anomaly_root_cause(request: Request, scope: str = "") -> JSONResponse:
    """What changed in one resource group, around the day one of its anomalies happened.

    A POST for the same reason What-If is one: Activity Log can refuse a subscription for a
    reason worth showing (a role that covers cost but not Monitor's own reader action), and
    only post()'s error handling keeps that sentence rather than collapsing it to a status code.
    """
    from .root_cause import RootCauseError, find

    body = await request.json()
    resource_group = str(body.get("resource_group") or "").strip()
    day = str(body.get("day") or "").strip()

    user = current_user(request)
    allowed = await permitted(user)
    picked = narrow([s for s in scope.split(",") if s.strip()], allowed)

    try:
        async with as_user(user):
            result = await find(resource_group=resource_group, day=day,
                                subscription_ids=picked or None)
        return JSONResponse(result)
    except RootCauseError as exc:
        raise HTTPException(400, str(exc)) from exc
    except HTTPException:
        raise
    except Exception as exc:  # noqa: BLE001 - a lookup failure must not look like a bad request
        log.warning("anomaly root-cause failed: %s", str(exc)[:200])
        raise HTTPException(502, str(exc)[:300]) from exc


@app.get("/api/anomalies/alerts")
async def anomaly_alerts_list(request: Request, subscription: str = "") -> JSONResponse:
    """Every anomaly alert across the subscriptions in scope, and whether they still send.

    Not admin-only. Knowing whether anyone is being told about a cost spike is part of trusting
    the tab above it, and the response carries recipients rather than cost.

    Two things this used to get wrong, both of which made the panel look broken rather than
    empty. It read `picked[0]` only, so an estate of three subscriptions reported the alerts on
    one of them and silently implied the other two had none. And for a caller whose entitlement
    is "whatever the server can see" — every local account, and the one used to demo this —
    `permitted` returns None, `narrow` returns [], and the endpoint answered 400 to a request
    that was perfectly valid. A scheduled action lives on a subscription, so the honest answer
    spans all of them and names which is which.
    """
    from . import alerts
    from .cost import list_subscriptions

    user = current_user(request)
    allowed = await permitted(user)
    picked = narrow([s for s in subscription.split(",") if s.strip()], allowed)

    async with as_user(user):
        if not picked:
            # Not an error: an unrestricted caller picking nothing means every subscription the
            # server can see, which is what the rest of the app already reads an empty list as.
            picked = [s["id"] for s in (await list_subscriptions())["subscriptions"]]
        if not picked:
            raise HTTPException(400, "No subscriptions are in scope.")

        names = {s["id"]: s.get("name") or s["id"]
                 for s in (await list_subscriptions())["subscriptions"]}

        # Bounded, and concurrent. One scheduled-actions call per subscription is fast, but an
        # estate of fifty would otherwise turn a panel into a minute of waiting.
        targets = picked[:12]
        results = await asyncio.gather(
            *(alerts.listing(sub) for sub in targets), return_exceptions=True)

        found: list[dict[str, Any]] = []
        refused: list[str] = []
        note = ""
        for sub, res in zip(targets, results):
            if isinstance(res, BaseException):
                log.info("anomaly alerts unreadable on %s: %s", sub, str(res)[:200])
                refused.append(names.get(sub, sub))
                continue
            note = note or res.get("note") or ""
            for a in res.get("alerts") or []:
                found.append({**a, "subscription_id": sub,
                              "subscription": names.get(sub, sub)})

    return JSONResponse({
        "alerts": found,
        "count": len(found),
        "expired": [a["name"] for a in found if a.get("expired")],
        # Which subscription a new alert would be created on. A scheduled action is per
        # subscription, so this cannot be left implicit when there is more than one.
        "subscriptions": [{"id": s, "name": names.get(s, s)} for s in targets],
        "unreadable": refused,
        "note": note or ("Anomaly alerts are sent by Azure Cost Management, not by this app, so "
                         "they keep working whether or not it is running."),
    })


@app.post("/api/anomalies/alerts")
async def anomaly_alerts_save(request: Request) -> JSONResponse:
    """Ask Azure to email somebody when it detects a cost anomaly.

    Admin-only and run as the signed-in person, so Azure decides whether they may: this writes a
    scheduled action into the customer's subscription, which is the same bar the budget writer
    is held to.
    """
    from . import alerts

    user = require_admin(request)
    allowed = await permitted(user)
    body = await request.json()

    sub = str(body.get("subscription_id") or "").strip()
    if allowed is not None and sub.lower() not in {s.lower() for s in allowed}:
        raise HTTPException(403, "That subscription is not one you have access to.")

    async with as_user(user):
        try:
            saved = await alerts.create(
                subscription_id=sub,
                emails=body.get("emails") or [],
                name=body.get("name") or None,
                display_name=body.get("display_name") or "Daily cost anomaly",
                hour_of_day=int(body.get("hour_of_day") or 8),
                years=int(body.get("years") or 1),
                subject=body.get("subject") or None,
            )
        except alerts.AlertError as exc:
            raise HTTPException(400, str(exc)) from exc

    log.info("anomaly alert saved by %s on %s for %d recipient(s)",
             user.name, sub, len(saved.get("emails") or []))
    return JSONResponse(saved)


@app.delete("/api/anomalies/alerts/{name}")
async def anomaly_alerts_delete(request: Request, name: str,
                                subscription: str = "") -> JSONResponse:
    from . import alerts

    user = require_admin(request)
    allowed = await permitted(user)
    sub = subscription.strip()
    if allowed is not None and sub.lower() not in {s.lower() for s in allowed}:
        raise HTTPException(403, "That subscription is not one you have access to.")

    async with as_user(user):
        try:
            return JSONResponse(await alerts.delete(subscription_id=sub, name=name))
        except alerts.AlertError as exc:
            raise HTTPException(400, str(exc)) from exc


@app.get("/api/dashboard/aks")
async def dashboard_aks(request: Request, days: int = 30, scope: str = "",
                        currency: str = "") -> JSONResponse:
    """AKS spend attributed to the cluster that caused it.

    Every other tab reports an AKS estate as unattributed VM and networking lines, because the
    money is in the managed node resource group rather than under the cluster's own name.
    """
    from . import aks

    user = current_user(request)
    allowed = await permitted(user)
    picked = narrow([s for s in scope.split(",") if s.strip()], allowed)

    async with as_user(user):
        try:
            return JSONResponse(await aks.clusters(picked or allowed or None,
                                                   days=max(1, min(days, 365)),
                                                   currency=currency or None))
        except Exception as exc:  # noqa: BLE001 - one tab failing must not read as an outage
            log.warning("aks tab failed: %s", str(exc)[:200])
            raise HTTPException(502, f"Could not analyse AKS cost: {str(exc)[:200]}") from exc


@app.get("/api/insights")
async def insights(request: Request, days: int = 30, scope: str = "",
                   currency: str = "") -> JSONResponse:
    """Every source read at once, cross-referenced, and ranked by what it is worth.

    The one endpoint in the app that answers a question nobody asked. Every other tab responds to
    a choice someone made; this one has to decide for itself what is worth saying, which is why
    each finding carries the source it came from and the reasoning behind its number.

    Slow by the standards of this app — it runs the live Azure tabs as well as the warehouse
    ones — so it is deliberately not part of any page load. It runs when asked.
    """
    from . import insights as engine

    user = current_user(request)
    allowed = await permitted(user)
    picked = narrow([s for s in scope.split(",") if s.strip()], allowed)

    try:
        async with as_user(user):
            return JSONResponse(await engine.analyse(picked or None,
                                                     days=max(7, min(days, 365)),
                                                     currency=currency or None))
    except HTTPException:
        raise
    except Exception as exc:  # noqa: BLE001
        log.warning("insights failed: %s", str(exc)[:300])
        raise HTTPException(502, f"Could not complete the analysis: {str(exc)[:200]}") from exc


@app.get("/api/dashboard/sections")
async def dashboard_sections(request: Request, days: int = 30, scope: str = "",
                             currency: str = "") -> JSONResponse:
    """The tab bar, with what each tab is costing — so the labels carry information."""
    from .dashboard import get_dashboard

    user = current_user(request)
    allowed = await permitted(user)
    picked = narrow([s for s in scope.split(",") if s.strip()], allowed)
    return JSONResponse(await offload(get_dashboard().sections, picked or None,
                                      days=max(1, min(days, 365)), currency=currency,
                                      filters=request_filters(request)))


@app.get("/api/dashboard/executive")
async def dashboard_executive(request: Request, days: int = 30, scope: str = "",
                              currency: str = "") -> JSONResponse:
    """The opening view. One warehouse pass, no Azure calls, so it lands in milliseconds."""
    from .dashboard import get_dashboard

    user = current_user(request)
    allowed = await permitted(user)
    picked = narrow([s for s in scope.split(",") if s.strip()], allowed)
    return JSONResponse(await offload(get_dashboard().executive, picked or None,
                                      days=max(1, min(days, 365)), currency=currency,
                                      filters=request_filters(request)))


async def _billing_scope(user: User, scope: str) -> list[str]:
    def normal(value: str) -> str:
        value = value.strip().lower()
        match = re.match(r"^/subscriptions/([^/]+)/?$", value)
        return match.group(1) if match else value

    allowed = await permitted(user)
    allowed = sorted({normal(s) for s in allowed}) if allowed is not None else None
    requested = sorted({normal(s) for s in scope.split(",") if s.strip()})
    if allowed == [] or (allowed is not None and requested and not set(requested).issubset(allowed)):
        raise NoAccess("You do not have access to the requested Billing subscriptions.")
    return sorted(set(narrow(requested, allowed)))


@app.get("/api/dashboard/billing")
async def dashboard_billing(request: Request, scope: str = "", currency: str = "",
                            month: str = "") -> JSONResponse:
    """Historical calendar statement: scoped warehouse work only, no Azure scan."""
    from . import billing

    user = current_user(request)
    picked = await _billing_scope(user, scope)
    try:
        billing.statement_period(month or None)
        target = billing.normalize_currency(currency)
    except ValueError as exc:
        raise HTTPException(422, str(exc), headers={"Cache-Control": "no-store"}) from exc
    try:
        result = await offload(billing.build, picked or None, target, month or None,
                               filters=request_filters(request))
    except ValueError as exc:
        raise HTTPException(422, str(exc), headers={"Cache-Control": "no-store"}) from exc
    return JSONResponse(result, headers={"Cache-Control": "no-store"})


async def _get_billing_opportunities(user: User, picked: list[str],
                                     currency: str, *, refresh: bool = False) -> dict:
    import copy

    from .billing_opportunities import build

    key = inner_cache_key((user.source, user.name, user.email, user.sid, tuple(picked), currency))
    now = asyncio.get_running_loop().time()
    snapshot = _billing_cache.get(key)
    pending = _billing_pending.get(key)
    cached = False
    if pending is None and snapshot and not refresh and now - snapshot[0] < _BILLING_CACHE_SECONDS:
        _billing_cache.move_to_end(key)
        cached = True
    else:
        if pending is None:
            if len(_billing_pending) >= _BILLING_PENDING_LIMIT:
                raise HTTPException(503, "Billing opportunity scans are busy. Try again shortly.",
                                    headers={"Cache-Control": "no-store", "Retry-After": "5"})

            async def load() -> tuple[float, dict]:
                async with asyncio.timeout(_BILLING_SCAN_TIMEOUT_SECONDS), as_user(user):
                    data = await build(picked, currency, days=30)
                value = (asyncio.get_running_loop().time(), data)
                _billing_cache[key] = value
                _billing_cache.move_to_end(key)
                while len(_billing_cache) > _BILLING_CACHE_LIMIT:
                    _billing_cache.popitem(last=False)
                return value

            pending = asyncio.create_task(load())
            _billing_pending[key] = pending

            def finished(task: asyncio.Task) -> None:
                if _billing_pending.get(key) is task:
                    _billing_pending.pop(key, None)
                if not task.cancelled():
                    task.exception()  # Consume failures if every waiting HTTP request disconnected.

            pending.add_done_callback(finished)
        else:
            cached = True
        try:
            snapshot = await asyncio.shield(pending)
        except TimeoutError as exc:
            raise HTTPException(504, "The current opportunity scan timed out; the historical statement is still available.",
                                headers={"Cache-Control": "no-store"}) from exc
    result = copy.deepcopy(snapshot[1])
    result["cache"] = {
        "cached": cached, "age_seconds": round(max(0, asyncio.get_running_loop().time() - snapshot[0]), 1),
        "max_age_seconds": _BILLING_CACHE_SECONDS, "refresh_requested": refresh,
    }
    result["scope_note"] = (
        "Current 30-day subscription-scope opportunity scan; historical Billing month and "
        "dimension filters do not apply. Prospective savings are separate from achieved savings."
    )
    return result


@app.get("/api/dashboard/billing/opportunities")
async def dashboard_billing_opportunities(request: Request, scope: str = "",
                                          currency: str = "", refresh: bool = False) -> JSONResponse:
    from .billing import normalize_currency

    user = current_user(request)
    picked = await _billing_scope(user, scope)
    try:
        target = normalize_currency(currency)
    except ValueError as exc:
        raise HTTPException(422, str(exc), headers={"Cache-Control": "no-store"}) from exc
    if not picked:
        from .cost import list_subscriptions

        try:
            async with asyncio.timeout(30), as_user(user):
                subscriptions = (await list_subscriptions())["subscriptions"]
            picked = sorted({str(item["id"]).strip().rstrip("/").rsplit("/", 1)[-1].lower()
                             for item in subscriptions if item.get("id")})
        except HTTPException:
            raise
        except Exception as exc:
            raise HTTPException(503, "Could not resolve current subscriptions. Select an explicit subscription scope.",
                                headers={"Cache-Control": "no-store"}) from exc
        if not picked:
            raise NoAccess("No subscriptions are available for the Billing opportunity scan.")
    if any(not re.fullmatch(r"[a-z0-9-]+", value) for value in picked):
        raise HTTPException(422, "Billing opportunities require subscription IDs.",
                            headers={"Cache-Control": "no-store"})
    result = await _get_billing_opportunities(user, picked, target, refresh=refresh)
    return JSONResponse(result, headers={"Cache-Control": "no-store"})


@app.get("/api/dashboard/audit")
async def dashboard_audit(request: Request, limit: int = 200) -> JSONResponse:
    """Who looked at which tab, and when. Admin-only -- this is a record about people, not spend.

    Reads `access_log`, written by the auth middleware on every dashboard tab actually fetched
    from the server (`RequireAuth.dispatch`, see `_log_access`). Not scoped by subscription:
    unlike every other tab, the thing being read here is not billing data, so the usual
    entitlement narrowing does not apply -- an admin sees every account's activity, which is
    the point of an access log.
    """
    require_admin(request)
    from .warehouse import warehouse

    rows = await offload(warehouse.recent_access, max(1, min(limit, 500)))
    return JSONResponse({"entries": rows, "count": len(rows)})


@app.get("/api/dashboard/waste")
async def dashboard_waste(request: Request, days: int = 30, scope: str = "",
                          currency: str = "") -> JSONResponse:
    """Stale and idle resources. Live inventory, so slower than the other tabs — the cost
    export cannot tell you a disk is attached to nothing.

    The money here comes from Azure rather than the warehouse, so it is converted on the way
    out rather than in SQL.
    """
    from . import currency as fx
    from .waste import find_waste

    user = current_user(request)
    allowed = await permitted(user)
    picked = narrow([s for s in scope.split(",") if s.strip()], allowed)

    try:
        async with as_user(user):
            found = await find_waste(subscription_ids=picked or None,
                                     days=max(1, min(days, 365)), top=25)
        return JSONResponse(fx.convert_money(found, currency))
    except HTTPException:
        raise
    except Exception as exc:  # noqa: BLE001 - one failing tab must not look like an outage
        log.warning("waste tab failed: %s", str(exc)[:200])
        return JSONResponse({"error": str(exc)[:300], "findings": []}, status_code=502)


def _start_savings(user: User, picked: list[str], window: int, currency: str, *,
                   refresh: bool = False) -> tuple[_SavingsState, asyncio.Task | None]:
    """Start/join one scan without awaiting Azure; retain completed data during revalidation."""
    from . import savings as savings_mod

    actor = (user.source, user.name, user.email, user.sid if user.source == "entra" else "")
    scope = tuple(sorted({s.strip().lower() for s in picked if s.strip()}))
    currency = currency.strip().upper() or "BILLED"
    cache_key = inner_cache_key((actor, scope, window, currency))
    pending = _savings_pending.get(cache_key)
    if pending is not None and pending.done():
        _finish_savings(cache_key, pending)
        pending = None
    state = _savings_cache.get(cache_key)
    now = asyncio.get_running_loop().time()
    if state:
        _savings_cache.move_to_end(cache_key)
        # A terminal failure is cached too: polling must not immediately retry all four sources.
        if pending is not None or (
            not refresh and state.finished_at is not None
            and now - state.finished_at < _SAVINGS_CACHE_SECONDS
        ):
            if state.auth_error is not None:
                raise state.auth_error
            return state, pending
    if len(_savings_pending) >= _SAVINGS_PENDING_LIMIT:
        raise HTTPException(503, "Savings scans are busy. Please retry shortly.",
                            headers={"Retry-After": "5"})
    if state is None:
        while len(_savings_cache) >= _SAVINGS_CACHE_LIMIT:
            victim = next((key for key in _savings_cache if key not in _savings_pending), None)
            if victim is None:
                raise HTTPException(503, "Savings scans are busy. Please retry shortly.",
                                    headers={"Retry-After": "5"})
            _savings_cache.pop(victim)
        state = _SavingsState(
            partial=savings_mod.assemble({}, {}, window, currency),
            scan={"status": "running", "completed": [], "total": len(savings_mod.SCAN_SOURCES),
                  "errors": {}, "revision": 0},
        )
        _savings_cache[cache_key] = state
    state.scan = {
        "status": "running", "completed": [], "total": len(savings_mod.SCAN_SOURCES),
        "errors": {}, "revision": state.scan["revision"] + 1,
    }
    state.partial = savings_mod.assemble({}, {}, window, currency)
    state.finished_at = None
    state.auth_error = None

    def progress(result: dict[str, Any]) -> None:
        state.partial = result
        # build() still drains its child tasks after the final callback. Publish a terminal
        # status only alongside the committed snapshot, or a poll can stop on the old data.
        state.scan = {**result["scan"], "status": "running",
                      "revision": state.scan["revision"] + 1}

    async def build() -> dict[str, Any]:
        try:
            async with asyncio.timeout(_SAVINGS_SCAN_TIMEOUT_SECONDS), as_user(user):
                result = await savings_mod.build(
                    list(scope) or None, days=window, currency=currency, on_progress=progress)
            state.partial = result
            state.scan = {**result["scan"], "revision": state.scan["revision"] + 1}
            if state.scan["status"] != "error":
                state.result = result
                state.generated_time = asyncio.get_running_loop().time()
                state.generated_at = datetime.now(UTC).isoformat()
            state.finished_at = asyncio.get_running_loop().time()
            return result
        except HTTPException as exc:
            state.auth_error = exc
            _savings_failed(state, str(exc.detail)[:200])
            raise
        except Exception as exc:
            message = ("The scan timed out." if isinstance(exc, TimeoutError)
                       else (str(exc) or type(exc).__name__)[:200])
            log.warning("savings scan failed: %s", message)
            _savings_failed(state, message)
            return state.partial

    pending = asyncio.create_task(build())
    _savings_pending[cache_key] = pending
    pending.add_done_callback(functools.partial(_finish_savings, cache_key))
    return state, pending


def _savings_response(state: _SavingsState, *, cached: bool | None = None) -> dict[str, Any]:
    age = (max(0.0, asyncio.get_running_loop().time() - state.generated_time)
           if state.generated_time is not None else None)
    return {
        **(state.result if state.result is not None else state.partial),
        "scan": {**state.scan, "completed": list(state.scan["completed"]),
                 "errors": dict(state.scan["errors"])},
        "freshness": {
            "generated_at": state.generated_at, "age_seconds": age,
            "max_age_seconds": _SAVINGS_CACHE_SECONDS,
            "stale": age is None or age >= _SAVINGS_CACHE_SECONDS,
            "cached": (state.result is not None or state.finished_at is not None)
                      if cached is None else cached,
            "refreshing": state.scan["status"] == "running",
        },
    }


async def _get_savings(user: User, picked: list[str], window: int, currency: str) -> dict[str, Any]:
    """Full results for Savings/insights; only cold callers wait, shielded from disconnects."""
    from . import reporting

    with reporting.select(None):
        state, pending = _start_savings(user, picked, window, currency)
    if state.result is not None or pending is None:
        return _savings_response(state)
    await asyncio.shield(pending)
    return _savings_response(state, cached=False)


@app.get("/api/dashboard/savings/progress")
async def dashboard_savings_progress(request: Request, days: int = 30, scope: str = "",
                                    currency: str = "", refresh: bool = False) -> JSONResponse:
    """Poll a shared scan. Use refresh only on the initial explicit refresh, not every poll."""
    user = current_user(request)
    allowed = await permitted(user)
    picked = narrow([s.strip() for s in scope.split(",") if s.strip()], allowed)
    state, _ = _start_savings(user, picked, max(1, min(days, 365)), currency, refresh=refresh)
    return JSONResponse(_savings_response(state), headers={"Cache-Control": "no-store"})


@app.get("/api/dashboard/savings")
async def dashboard_savings(request: Request, days: int = 30, scope: str = "",
                            currency: str = "") -> JSONResponse:
    """Every savings source, merged so each resource is counted once.

    Six tabs used to answer this question separately, and their totals overlapped: the same
    deallocated VM was inside both the orphan figure and the rightsizing figure, so adding the
    tabs up produced a saving that could not be banked. This endpoint is the one place that
    reconciles them.
    """
    user = current_user(request)
    allowed = await permitted(user)
    picked = narrow([s.strip() for s in scope.split(",") if s.strip()], allowed)
    window = max(1, min(days, 365))

    try:
        result = await _get_savings(user, picked, window, currency)
        return JSONResponse(result, headers={"Cache-Control": "no-store"})
    except HTTPException:
        raise
    except Exception as exc:  # noqa: BLE001 - one failing tab must not look like an outage
        log.warning("savings tab failed: %s", str(exc)[:200])
        return JSONResponse({"error": str(exc)[:300], "opportunities": []}, status_code=502)


@app.get("/api/dashboard/recommendations")
async def dashboard_recommendations(request: Request, days: int = 30, scope: str = "",
                                    currency: str = "") -> JSONResponse:
    """One AI-written paragraph over data the Savings and Anomalies tabs already produced.

    Not a third scan: every figure the model can mention is already sitting in the same
    `savings.build()`/`anomalies.analyse()` result those tabs read, including the shared cache
    above, so opening this costs nothing extra beyond the model call itself if the Savings tab
    was already warm. What this adds is judgement across a list a person has to read in full to
    get the same answer from -- which finding to act on first, and why.
    """
    from . import anomalies as anomalies_mod
    from . import recommendations as rec_mod

    user = current_user(request)
    allowed = await permitted(user)
    picked = narrow([s for s in scope.split(",") if s.strip()], allowed)
    window = max(1, min(days, 365))

    try:
        savings_result, anomalies_result = await asyncio.gather(
            _get_savings(user, picked, window, currency),
            offload(anomalies_mod.analyse, picked or None, days=max(days, 60),
                   display_currency=currency or None),
        )
    except HTTPException:
        raise
    except Exception as exc:  # noqa: BLE001 - a failed read must not break the summary card
        log.warning("recommendations: could not gather source data: %s", str(exc)[:200])
        return JSONResponse({"narrative": None,
                            "error": "Could not read enough data to summarise."}, status_code=502)

    result = await rec_mod.synthesize(
        agent(), savings=savings_result, anomalies=anomalies_result,
        currency=(currency or savings_result.get("currency") or "USD").upper())
    return JSONResponse(result)


@app.get("/api/dashboard/rightsizing")
async def dashboard_rightsizing(request: Request, days: int = 30,
                                scope: str = "", currency: str = "") -> JSONResponse:
    """Per-VM CPU from Azure Monitor, with power state and what each machine cost.

    Stale resources finds what is dead; this finds what is oversized, which is usually the
    larger number.
    """
    from . import currency as fx
    from .waste import vm_utilisation

    user = current_user(request)
    allowed = await permitted(user)
    picked = narrow([s for s in scope.split(",") if s.strip()], allowed)

    try:
        async with as_user(user):
            vms = await vm_utilisation(subscription_ids=picked or None,
                                       days=max(1, min(days, 90)))
        return JSONResponse(fx.convert_money(vms, currency))
    except HTTPException:
        raise
    except Exception as exc:  # noqa: BLE001
        log.warning("rightsizing tab failed: %s", str(exc)[:200])
        return JSONResponse({"error": str(exc)[:300], "vms": []}, status_code=502)


@app.post("/api/dashboard/whatif")
async def dashboard_whatif(request: Request, days: int = 30, scope: str = "") -> JSONResponse:
    """One VM, priced under another size or shut down, before doing either.

    A POST, not a GET, on purpose: `resource_id` and `target_sku` are typed by a person one
    guess at a time, so a wrong SKU name has to come back as the sentence explaining that --
    which is what `post()` preserves and the plain `get()` used by every read-only tab does not.

    `resource_id` has to be one the caller is actually entitled to -- checked against their own
    subscription list the same way every other live tab is, since this reaches Resource Graph
    with the caller's own delegated identity.
    """
    from .whatif import WhatIfError, scenario

    body = await request.json()
    resource_id = str(body.get("resource_id") or "").strip()
    target_sku = str(body.get("target_sku") or "").strip()

    user = current_user(request)
    allowed = await permitted(user)
    picked = narrow([s for s in scope.split(",") if s.strip()], allowed)

    try:
        async with as_user(user):
            result = await scenario(resource_id=resource_id, target_sku=target_sku or None,
                                    subscription_ids=picked or allowed, days=max(1, min(days, 365)))
        return JSONResponse(result)
    except WhatIfError as exc:
        raise HTTPException(400, str(exc)) from exc
    except HTTPException:
        raise
    except Exception as exc:  # noqa: BLE001 - a bad scenario request must not look like an outage
        log.warning("whatif scenario failed: %s", str(exc)[:200])
        raise HTTPException(502, str(exc)[:300]) from exc


@app.get("/api/dashboard/advisor")
async def dashboard_advisor(request: Request, category: str = "Cost",
                            scope: str = "", currency: str = "") -> JSONResponse:
    """Microsoft's own recommendations, with its own savings estimates."""
    from . import currency as fx
    from .waste import advisor_recommendations

    user = current_user(request)
    allowed = await permitted(user)
    picked = narrow([s for s in scope.split(",") if s.strip()], allowed)
    wanted = category if category in ("Cost", "Security", "HighAvailability",
                                      "OperationalExcellence", "Performance", "All") else "Cost"

    try:
        async with as_user(user):
            recs = await advisor_recommendations(subscription_ids=picked or None,
                                                 category=wanted)
        return JSONResponse(fx.convert_money(recs, currency))
    except HTTPException:
        raise
    except Exception as exc:  # noqa: BLE001
        log.warning("advisor tab failed: %s", str(exc)[:200])
        return JSONResponse({"error": str(exc)[:300], "recommendations": []}, status_code=502)


@app.get("/api/dashboard/governance")
async def dashboard_governance(request: Request, days: int = 30, scope: str = "") -> JSONResponse:
    """Tag coverage, accelerated networking and empty resource groups — configuration rather
    than spend.

    All three are gathered together because the tab shows them together, and all three are
    Resource Graph queries whose latency is dominated by the round trip rather than the work.
    A failure in one must not lose the others, so they are gathered independently.
    """
    from .governance import accelerated_networking, orphaned_resource_groups, tagging

    user = current_user(request)
    allowed = await permitted(user)
    picked = narrow([s for s in scope.split(",") if s.strip()], allowed)

    async with as_user(user):
        tags, accel, orphaned_rg = await asyncio.gather(
            tagging(subscription_ids=picked or None, days=days),
            accelerated_networking(subscription_ids=picked or None),
            orphaned_resource_groups(subscription_ids=picked or None),
            return_exceptions=True,
        )

    def unwrap(result: Any, label: str) -> dict[str, Any]:
        if isinstance(result, Exception):
            log.warning("governance/%s failed: %s", label, str(result)[:200])
            return {"error": str(result)[:300]}
        return result

    return JSONResponse({
        "tagging": unwrap(tags, "tagging"),
        "accelerated_networking": unwrap(accel, "accelerated_networking"),
        "orphaned_resource_groups": unwrap(orphaned_rg, "orphaned_resource_groups"),
    })


@app.get("/api/dashboard/rates")
async def dashboard_rates(request: Request, scope: str = "", currency: str = "") -> JSONResponse:
    """Commitment savings Azure is recommending — reservations and savings plans.

    Read from Advisor rather than the Consumption benefit API, because Advisor's figures are
    scoped to the subscription while Consumption's are scoped to the whole billing account.
    """
    from . import currency as fx
    from .rates import rate_optimisation

    user = current_user(request)
    allowed = await permitted(user)
    picked = narrow([s for s in scope.split(",") if s.strip()], allowed)

    try:
        async with as_user(user):
            opt = await rate_optimisation(subscription_ids=picked or None)
        return JSONResponse(fx.convert_money(opt, currency))
    except HTTPException:
        raise
    except Exception as exc:  # noqa: BLE001
        log.warning("rates tab failed: %s", str(exc)[:200])
        return JSONResponse({"error": str(exc)[:300], "commitments": []}, status_code=502)


@app.get("/api/dashboard/health")
async def dashboard_health(request: Request, scope: str = "") -> JSONResponse:
    """Retirements, deprecations and required migrations, from Azure Service Health."""
    from .health import advisories

    user = current_user(request)
    allowed = await permitted(user)
    picked = narrow([s for s in scope.split(",") if s.strip()], allowed)

    try:
        async with as_user(user):
            return JSONResponse(await advisories(subscription_ids=picked or None))
    except HTTPException:
        raise
    except Exception as exc:  # noqa: BLE001
        log.warning("service health tab failed: %s", str(exc)[:200])
        return JSONResponse({"error": str(exc)[:300], "advisories": []}, status_code=502)


@app.get("/api/dashboard/azure-updates")
async def dashboard_azure_updates(request: Request, refresh: bool = False) -> JSONResponse:
    """Official public product announcements; independent of both health sources."""
    from .azure_updates import public_feed

    current_user(request)
    return JSONResponse(await public_feed("updates", refresh), headers={"Cache-Control": "no-store"})


@app.get("/api/dashboard/azure-status")
async def dashboard_azure_status(request: Request, refresh: bool = False) -> JSONResponse:
    """The public incident feed; successful retrieval is not a global health verdict."""
    from .azure_updates import public_feed

    current_user(request)
    return JSONResponse(await public_feed("status", refresh), headers={"Cache-Control": "no-store"})


@app.get("/api/dashboard/service-health")
async def dashboard_service_health(request: Request, scope: str = "",
                                   refresh: bool = False) -> JSONResponse:
    """Private, explicitly scoped Service Health, without changing the Retirements endpoint."""
    from .azure_updates import SourceError, error_text
    from .service_health import normalize_scopes, scope_error, service_health

    user = current_user(request)
    headers = {"Cache-Control": "private, no-store"}
    try:
        allowed = await permitted(user)
        requested = normalize_scopes(scope.split(","))
        if allowed is not None:
            allowed = [s.lower() for s in allowed]
            # Keep this endpoint's structured error response for forbidden selections.
            if not set(requested).issubset(allowed):
                return JSONResponse(scope_error("One or more selected subscriptions are not permitted."),
                                    status_code=403, headers=headers)
        picked = narrow(requested, allowed)
        if not picked:
            return JSONResponse(scope_error("Select at least one permitted subscription."),
                                status_code=400, headers=headers)
        async with as_user(user):
            result = await service_health(user, picked, refresh)
        return JSONResponse(result, headers=headers)
    except SourceError as exc:
        return JSONResponse(scope_error(str(exc)), status_code=400, headers=headers)
    except HTTPException:
        raise
    except Exception as exc:  # noqa: BLE001 - only this independent source should fail
        error = error_text(exc)
        log.warning("scoped Service Health failed: %s", error)
        return JSONResponse(scope_error(error), status_code=502, headers=headers)


async def _live_evidence_response(
    request: Request, scope: str, window_days: int,
    loader: Callable[[list[str], int], Awaitable[dict[str, Any]]],
) -> JSONResponse:
    import httpx

    from .azure_updates import SourceError, error_text
    from .cost import CostError
    from .observability import evidence_window
    from .service_health import normalize_scopes

    user = current_user(request)
    headers = {"Cache-Control": "private, no-store"}
    try:
        picked = normalize_scopes(scope.split(","))
        evidence_window(window_days)
    except SourceError as exc:
        raise HTTPException(400, str(exc)) from exc
    allowed = await permitted(user)
    if allowed is not None and not set(picked).issubset({s.lower() for s in allowed}):
        raise HTTPException(403, "One or more selected subscriptions are not permitted.")
    try:
        async with asyncio.timeout(110), as_user(user):
            result = await loader(picked, window_days)
    except (SourceError, CostError, httpx.HTTPError, TimeoutError) as exc:
        error = error_text(SourceError(str(exc))) if isinstance(exc, CostError) else error_text(exc)
        log.warning("live evidence source failed: %s", error)
        return JSONResponse({"status": "unavailable", "error": error, "notices": [error]},
                            status_code=502, headers=headers)
    return JSONResponse(result, headers=headers)


@app.get("/api/dashboard/change-analysis")
async def dashboard_change_analysis(request: Request, scope: str = "",
                                    window_days: int = 7) -> JSONResponse:
    """Recent resource changes and scoped billing context, without modifying Azure."""
    from .change_analysis import report

    return await _live_evidence_response(request, scope, window_days, report)


@app.get("/api/dashboard/performance")
async def dashboard_performance(request: Request, scope: str = "",
                                window_days: int = 7) -> JSONResponse:
    """Bounded service-specific metrics, with unknown observations kept distinct from zero."""
    from .performance_metrics import report

    return await _live_evidence_response(request, scope, window_days, report)


@app.get("/api/dashboard/ai-billing")
async def dashboard_ai_billing(request: Request, days: int = 30, scope: str = "",
                               currency: str = "") -> JSONResponse:
    """Billed AI resources and measured tokens; no inferred per-deployment charges."""
    from . import cost
    from .ai_billing import report

    user = current_user(request)
    picked = selected_scope(scope.split(","), await permitted(user))
    async with as_user(user):
        if picked is None:
            picked = [s["id"] for s in (await cost.list_subscriptions())["subscriptions"]]
        result = await report(picked, days=max(1, min(days, 365)),
                              currency=currency or None, filters=request_filters(request))
    return JSONResponse(result, headers={"Cache-Control": "private, no-store"})


@app.get("/api/dashboard/commitments")
async def dashboard_commitments(request: Request, days: int = 30,
                                scope: str = "", currency: str = "") -> JSONResponse:
    """Reservation, savings plan and Spot coverage, from the warehouse."""
    from .commitments import get_commitments

    user = current_user(request)
    allowed = await permitted(user)
    picked = narrow([s for s in scope.split(",") if s.strip()], allowed)

    c = get_commitments()
    days = max(1, min(days, 365))
    # Three warehouse passes, all blocking; running them off the event loop together costs the
    # slowest rather than their sum.
    cover, spot, owned = await asyncio.gather(
        offload(c.coverage, picked or None, days=days, currency=currency or None),
        offload(c.spot_savings, picked or None, days=days, currency=currency or None),
        offload(c.inventory, picked or None, days=max(days, 90), currency=currency or None),
    )

    # The baseline has to come from the warehouse rather than from Advisor. Advisor reports the
    # saving, not the bill, and the only correctly-scoped on-demand figure in the app is the one
    # coverage() just computed for exactly these subscriptions.
    on_demand = (cover.get("split") or {}).get("on demand") if not cover.get("empty") else None
    baseline_annual = None
    if on_demand is not None and days:
        baseline_annual = float(on_demand) * (365.0 / days)

    from .rates import commitment_comparison
    try:
        async with as_user(user):
            compare = await commitment_comparison(
                picked or None, baseline_annual=baseline_annual, baseline_days=days,
                baseline_currency=cover.get("currency"),
                display_currency=currency or None)
    except Exception as exc:  # noqa: BLE001 - Advisor being unreachable must not blank the tab
        log.info("commitment comparison failed: %s", str(exc)[:200])
        compare = {"error": str(exc)[:300], "options": []}

    return JSONResponse({"coverage": cover, "spot": spot, "inventory": owned,
                         "comparison": compare})


@app.get("/api/dashboard/uptime")
async def dashboard_uptime(request: Request, days: int = 30, scope: str = "",
                           currency: str = "") -> JSONResponse:
    """Daily hour-meter billing quantities and derived rates, not monitored availability."""
    from .uptime import hourly_cost

    user = current_user(request)
    allowed = await permitted(user)
    picked = narrow([s for s in scope.split(",") if s.strip()], allowed)
    payload = await offload(hourly_cost, picked or None,
                            days=max(1, min(days, 365)),
                            currency=currency or None,
                            filters=request_filters(request))
    # The tag breakdown on this tab carries the same caveat as the Tags tab: a load that cannot
    # carry tags leaves every meter looking untagged, which is a different statement from "these
    # meters have no tags" and must not be made silently.
    payload["tags_not_loaded"] = await offload(_tags_missing_from_load)
    return JSONResponse(payload)


@app.get("/api/dashboard/tags")
async def dashboard_tags(request: Request, days: int = 30, scope: str = "",
                         currency: str = "") -> JSONResponse:
    """Tagged resources with their costs and tag keys, for the Cost by Tag tab.

    Deliberately returns the resources rather than an answer: which resources match depends on
    the tags someone has selected and whether they want all of them or any, and that changes on
    every click. Answering here would put a network round trip behind each one.

    The live key lookup is a second, optional pass. Cost data only knows a tag once usage has
    been metered while it was attached, so a tag set today — or one on a stopped or free
    resource — is invisible here no matter how correct it is. Asking Azure separately lets the
    tab distinguish "no such tag" from "no spend behind that tag yet".
    """
    from .tags import cost_by_tag, live_tag_keys, merge_live_keys

    user = current_user(request)
    allowed = await permitted(user)
    picked = narrow([s for s in scope.split(",") if s.strip()], allowed)
    subs = picked or allowed or None

    # Both halves at once: the warehouse query is offloaded to a thread while the Azure call for
    # live tag keys is in flight, so the tab costs the slower of the two rather than their sum.
    async with as_user(user):
        payload, live = await asyncio.gather(
            offload(cost_by_tag, picked or None, days=max(1, min(days, 365)),
                    currency=currency or None, filters=request_filters(request)),
            live_tag_keys(subs),
        )
    merged = merge_live_keys(payload, live)
    # Tag availability comes from these resource rows, not another subscription's last import.
    return JSONResponse(merged)


def _tags_missing_from_load() -> bool:
    """Whether the newest successful load was one that cannot carry tags."""
    from .warehouse import warehouse

    if "Tags" in (warehouse.state.get("omits") or []):
        return True
    try:
        with warehouse.connect() as con:
            row = con.execute(
                "SELECT detail FROM ingest_log WHERE status = 'ok' "
                "ORDER BY ingested_at DESC LIMIT 1").fetchone()
        return bool(row) and "quick" in (row[0] or "").lower()
    except Exception:  # noqa: BLE001 - never let this be why the tab fails to render
        return False


@app.get("/api/dashboard/resources")
async def dashboard_resources(request: Request, days: int = 30, scope: str = "",
                              currency: str = "") -> JSONResponse:
    """Every billed resource in the window, with its cost and context, for the Resources tab."""
    from .resources import get_resources

    user = current_user(request)
    allowed = await permitted(user)
    picked = narrow([s for s in scope.split(",") if s.strip()], allowed)

    return JSONResponse(await offload(
        get_resources().inventory, picked or None, days=max(1, min(days, 365)),
        currency=currency or None, filters=request_filters(request)))


class TagFilter(BaseModel):
    """One tag key and the values to match, already resolved by the tab."""

    key: str = Field(min_length=1, max_length=512)
    values: list[str] = Field(default_factory=list)


class BudgetRequest(BaseModel):
    """A budget to create from the tag selection currently on screen."""

    subscription: str = Field(min_length=1, max_length=64)
    name: str = Field(min_length=1, max_length=63)
    amount: float = Field(gt=0)
    time_grain: str = Field("Monthly", max_length=16)
    start: str = Field("", max_length=32)
    end: str = Field("", max_length=32)
    tags: list[TagFilter] = Field(default_factory=list)
    mode: str = Field("all", max_length=8)
    thresholds: list[float] = Field(default_factory=lambda: [80.0, 100.0])
    emails: list[str] = Field(default_factory=list)


def _budget_period(budget: dict) -> tuple[str, str]:
    """The budget's *current* period, not the whole span it was created for.

    Azure gives a budget a start date and an end date years away — `2026-08-01` to `2036-08-01`
    on this estate — and resets the counter every grain. `current_spend` is the figure for the
    period in progress, so a chart drawn across the full span would put one month of spend on a
    ten-year axis and show a flat line at zero.
    """
    from calendar import monthrange

    today = datetime.now(UTC).date()
    months = {"Monthly": 1, "Quarterly": 3, "Annually": 12}.get(
        str(budget.get("time_grain") or "Monthly"), 1)

    try:
        began = date.fromisoformat(str(budget.get("start"))[:10])
    except (TypeError, ValueError):
        began = today.replace(day=1)

    # Walk whole grains forward from the start date until the one containing today. Anchored on
    # the budget's own start rather than the calendar, because a quarterly budget beginning in
    # February runs Feb–Apr, not Jan–Mar.
    start = began
    while True:
        nxt_month = start.month - 1 + months
        nxt = date(start.year + nxt_month // 12, nxt_month % 12 + 1, 1)
        nxt = nxt.replace(day=min(began.day, monthrange(nxt.year, nxt.month)[1]))
        if nxt > today:
            break
        start = nxt
        if start.year > today.year + 1:      # a malformed start date must not spin
            break

    return start.isoformat(), today.isoformat()


def _budget_tags(node: Any) -> list[tuple[str, list[str]]]:
    """The tag clauses of an ARM budget filter, as (key, values) pairs.

    Dimension filters (resource group, service name) are deliberately not translated: the
    warehouse holds those columns, but a budget filtered on one would need its own mapping and
    a half-applied filter is worse than an honestly absent one. `filtered_exactly` on the
    response says which case a given budget is.
    """
    from .budgets import exact_tag_filter

    return exact_tag_filter(node) or []


def _prepare_budget_filter(budget: dict) -> tuple[list[tuple[str, list[str]]] | None, bool]:
    from .budgets import exact_tag_filter

    raw_filter = budget.get("filter_raw")
    tags = exact_tag_filter(raw_filter)
    exact = tags is not None and not (budget.get("filter") and not raw_filter)
    budget["filter_exact"] = exact
    budget["tag_filter"] = [[key, values] for key, values in tags or []] if exact else []
    if exact:
        budget.pop("trend_note", None)
    else:
        budget["trend_note"] = (
            "The budget's full filter cannot be reproduced in this view. "
            "Use Azure Portal for its exact scope; no approximate trend is shown."
        )
    return tags, exact


async def _attach_budget_trends(payload: dict, currency: str = "") -> None:
    """Build every chart only for callers that explicitly request the full trend set."""
    from .warehouse import warehouse

    budgets = payload.get("budgets") or []
    if not budgets:
        return

    def build(b: dict) -> dict:
        tags, exact = _prepare_budget_filter(b)
        if not exact:
            return {"labels": [], "filtered_exactly": False}
        start, end = _budget_period(b)
        trend = warehouse.budget_trend(
            b.get("subscription_id") or "", start, end,
            tags=tags or None, currency=currency or None)
        trend["filtered_exactly"] = exact
        return trend

    try:
        trends = await offload(lambda: [build(b) for b in budgets])
    except Exception as exc:  # noqa: BLE001 - the list is useful without the charts
        log.info("could not build budget trends: %s", str(exc)[:200])
        return

    for b, t in zip(budgets, trends):
        # Only worth carrying if there is a shape to draw. One point is not a trend, and an
        # empty one would draw an axis with nothing on it.
        b["trend"] = t if len(t.get("labels") or []) > 1 else None


@app.get("/api/budgets/trend")
async def get_budget_trend(request: Request, subscription_id: str, name: str,
                           currency: str = "") -> JSONResponse:
    """Load one budget's warehouse trend after its card is opened."""
    from . import demo_mode

    if demo_mode.enabled():
        return JSONResponse({
            "trend": None,
            "filter_exact": False,
            "tag_filter": [],
            "trend_note": demo_mode.budget_note(),
            "demo_mode": True,
        })

    from . import cost
    from . import currency as fx

    user = current_user(request)
    allowed = await permitted(user)
    selected = narrow([subscription_id], allowed)
    if not selected:
        raise HTTPException(403, "Choose a subscription in your permitted scope.")

    async with as_user(user):
        found = await cost.budgets(selected)
    if found.get("partial"):
        raise HTTPException(503, "Could not confirm the budget on this subscription.")

    budget = next((item for item in found.get("budgets", []) if item.get("name") == name), None)
    if budget is None:
        raise HTTPException(404, "That budget was not found on this subscription.")

    converted = fx.convert_money({"budgets": [budget]}, currency)
    budget = converted.get("budgets", [budget])[0]
    tags, exact = _prepare_budget_filter(budget)
    if not exact:
        return JSONResponse({
            "trend": None,
            "filter_exact": False,
            "tag_filter": [],
            "trend_note": budget["trend_note"],
        })

    from .warehouse import warehouse

    start, end = _budget_period(budget)
    trend = await offload(
        lambda: warehouse.budget_trend(
            selected[0], start, end, tags=tags or None, currency=currency or None))
    if trend.get("error"):
        return JSONResponse({
            "trend": None,
            "filter_exact": True,
            "tag_filter": budget["tag_filter"],
            "trend_note": "Warehouse trend data is temporarily unavailable; Azure-reported spend is unchanged.",
        })
    if len(trend.get("labels") or []) <= 1:
        return JSONResponse({
            "trend": None,
            "filter_exact": True,
            "tag_filter": budget["tag_filter"],
            "trend_note": "No comparable trend data is available for this budget period.",
        })
    trend["filtered_exactly"] = True
    return JSONResponse({
        "trend": trend,
        "filter_exact": True,
        "tag_filter": budget["tag_filter"],
        "trend_note": None,
    })


@app.get("/api/budgets")
async def list_budgets(request: Request, scope: str = "", currency: str = "",
                       refresh: bool = False, trends: bool = False) -> JSONResponse:
    """Budgets already defined on the subscriptions in scope.

    Read live rather than from the warehouse: budgets are not in a cost export, and a stale
    "no budgets" would invite someone to create a second one with the same job.

    Under a display currency the limit is converted alongside the spend. Converting only one of
    them would leave a rupee figure being measured against a dollar threshold — and the meter
    beside it would draw a percentage that is simply false.
    """
    from . import demo_mode

    if demo_mode.enabled():
        return JSONResponse(demo_mode.budgets_payload())

    from . import cost
    from . import currency as fx

    user = current_user(request)
    allowed = await permitted(user)
    picked = narrow([s for s in scope.split(",") if s.strip()], allowed)

    try:
        async with as_user(user):
            found = await cost.budgets(picked or None, refresh=refresh)
        out = fx.convert_money(found, currency)
        if out is not found:
            for b in out.get("budgets", []):
                b["currency"] = currency.upper()
        for budget in out.get("budgets", []):
            _prepare_budget_filter(budget)
        if trends:
            await _attach_budget_trends(out, currency)
        return JSONResponse(out)
    except HTTPException:
        raise
    except Exception as exc:  # noqa: BLE001 - retain an explicit unavailable state for the tab
        log.info("could not list budgets: %s", str(exc)[:200])
        return JSONResponse({"budgets": [], "count": 0, "error": str(exc)[:300]})


@app.post("/api/budgets")
async def create_budget(body: BudgetRequest, request: Request) -> JSONResponse:
    """Create a tag-filtered budget on one subscription.

    The only write this app makes to Azure, so who it runs as is the whole question.

    With delegated ARM the call carries the signed-in person's own token and Azure enforces
    their RBAC directly — someone who cannot create a budget gets Azure's refusal, which is the
    correct answer from the correct authority. Without it the call would run as the app's
    managed identity, which is generally *more* privileged than the person driving it, so that
    path is restricted to admins rather than quietly lending out the app's access.
    """
    from . import demo_mode

    if demo_mode.enabled():
        raise HTTPException(409, demo_mode.budget_note())

    from . import budgets as budget_api

    user = current_user(request)
    # The subscription has to be one they can already see. Azure would refuse an unauthorised
    # one anyway, but only after we had accepted and forwarded it.
    allowed = await permitted(user)
    try:
        subscription = authorize_budget(user, body.subscription, allowed)
    except PermissionError as exc:
        raise HTTPException(403, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc

    tags = [{"key": t.key, "values": t.values} for t in body.tags]
    try:
        async with as_user(user):
            created = await budget_api.create(
                subscription_id=subscription,
                name=body.name,
                amount=body.amount,
                time_grain=body.time_grain,
                start=body.start or None,
                end=body.end or None,
                tags=tags,
                mode=body.mode,
                thresholds=body.thresholds,
                emails=body.emails or ([user.email] if user.email else []),
            )
    except budget_api.BudgetError as exc:
        raise HTTPException(400, str(exc)) from exc
    except HTTPException:
        raise
    except Exception as exc:  # noqa: BLE001
        log.warning("budget create failed: %s", str(exc)[:300])
        raise HTTPException(502, f"Azure refused the budget: {str(exc)[:300]}") from exc

    log.info("budget %s created on %s by %s", created["name"], body.subscription, user.name)
    try:
        await offload(_bump_budget_revision)
    except Exception as exc:  # noqa: BLE001 - the budget exists; say so rather than fail
        log.warning("budget created but cached budget views were not invalidated (%s)",
                    type(exc).__name__)
    return JSONResponse(created, status_code=201)


@app.get("/api/costs/raw.csv")
async def download_raw_costs(request: Request, days: int = 30, scope: str = "",
                             machine_raw: bool = False) -> Response:
    """The underlying cost rows for the selected subscriptions, as CSV.

    The report formats are summaries. This is the detail behind them — one row per charge, the
    shape a FinOps team wants to pivot themselves. Scoped to the caller's entitlement and to
    whatever subscriptions they have picked, which is the point: the Cost Management portal
    hands you the whole billing account's file and leaves you to filter it.
    """
    import csv
    import io

    from .warehouse import warehouse

    user = current_user(request)
    allowed = await permitted(user)
    picked = narrow([s for s in scope.split(",") if s.strip()], allowed)
    days = max(1, min(days, 365))

    # `reader(scope)` physically shadows the costs table, so this cannot return a row the
    # caller is not entitled to even if the SQL below were wrong.
    from .reporting import edge, start_sql, until_sql

    filters = request_filters(request)

    def newest():
        with warehouse.reader(picked or allowed, filters=filters) as r:
            return edge(r)

    hi = await offload(newest)
    if not hi:
        raise HTTPException(404, "No cost data loaded yet.")

    def stream():
        from .spreadsheet import safe_cell
        from .warehouse import COLUMNS
        # Server-side cursor bounds memory and retains one consistent transaction snapshot.
        # Starlette iterates synchronous generators in its worker pool, not on the event loop.
        with warehouse.connect(read_only=True, scope=picked or allowed, filters=filters) as con:
            con.execute("BEGIN TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
            try:
                columns = ",".join(f'"{name}"' for name in COLUMNS)
                con.execute(f"DECLARE raw_cost_export NO SCROLL CURSOR FOR SELECT {columns} FROM costs "
                            f'WHERE "ChargePeriodStart" > {start_sql(hi, days)} '
                            f'AND "ChargePeriodStart" <= {until_sql(hi)} '
                            'ORDER BY "ChargePeriodStart" DESC, "BilledCost" DESC')
                buf = io.StringIO()
                writer = csv.writer(buf)
                writer.writerow(list(COLUMNS))
                yield buf.getvalue()
                while True:
                    batch = con.execute("FETCH FORWARD 2000 FROM raw_cost_export").fetchall()
                    if not batch:
                        break
                    buf.seek(0)
                    buf.truncate()
                    writer.writerows(batch if machine_raw else (
                        [safe_cell(value) for value in row] for row in batch))
                    yield buf.getvalue()
            finally:
                con.execute("ROLLBACK")

    from contextvars import copy_context
    context = copy_context()

    async def chunks():
        iterator = stream()
        try:
            while True:
                fetch = asyncio.create_task(offload(context.run, next, iterator, None))
                try:
                    chunk = await asyncio.shield(fetch)
                except asyncio.CancelledError:
                    # A worker-thread fetch cannot be cancelled: let it leave the Context
                    # before closing the generator and releasing its database connection.
                    await fetch
                    raise
                if chunk is None:
                    break
                yield chunk
        finally:
            await asyncio.shield(offload(context.run, iterator.close))

    stamp = str(hi).replace("-", "")
    return StreamingResponse(
        content=chunks(),
        media_type="text/csv",
        headers={
            "Content-Disposition": f'attachment; filename="cloudlens-costs-{stamp}.csv"',
            "Cache-Control": "no-store",
        },
    )


@app.get("/api/dashboard/esu")
async def dashboard_esu(request: Request, days: int = 30, scope: str = "",
                        currency: str = "") -> JSONResponse:
    """What is out of support, whether it is covered, and what ESU costs or would cost."""
    from . import currency as fx
    from .esu import esu_report

    user = current_user(request)
    allowed = await permitted(user)
    picked = narrow([s for s in scope.split(",") if s.strip()], allowed)

    try:
        async with as_user(user):
            report = await esu_report(subscription_ids=picked or None, days=days)
        return JSONResponse(fx.convert_money(report, currency))
    except HTTPException:
        raise
    except Exception as exc:  # noqa: BLE001
        log.warning("ESU tab failed: %s", str(exc)[:200])
        return JSONResponse({"error": str(exc)[:300], "machines": []}, status_code=502)


@app.get("/api/dashboard/hybrid-benefit")
async def dashboard_hybrid_benefit(request: Request, days: int = 30, scope: str = "",
                                   currency: str = "") -> JSONResponse:
    """Windows Server VMs not already using Hybrid Benefit, and the estimated saving.

    A second card on the same tab as end-of-support, filled in after it and separately from it:
    the two read different Resource Graph queries and price different things, and a slow retail
    lookup here must never hold up a report that is already complete on its own.
    """
    from . import currency as fx
    from .hybrid_benefit import coverage

    user = current_user(request)
    allowed = await permitted(user)
    picked = narrow([s for s in scope.split(",") if s.strip()], allowed)

    try:
        async with as_user(user):
            report = await coverage(subscription_ids=picked or None, days=days)
        return JSONResponse(fx.convert_money(report, currency))
    except HTTPException:
        raise
    except Exception as exc:  # noqa: BLE001
        log.warning("hybrid benefit tab failed: %s", str(exc)[:200])
        return JSONResponse({"error": str(exc)[:300], "vms": []}, status_code=502)


class ReportRequest(BaseModel):
    """What to put in the report. Absent fields mean 'everything from the warehouse'."""

    format: str = Field("xlsx", max_length=8)
    days: int = Field(30, ge=1, le=365)
    period_month: str | None = Field(None, max_length=7)
    scope: list[str] = Field(default_factory=list)
    summary: bool = True
    sections: list[str] | None = None       # None = every area
    blocks: list[str] = Field(default_factory=lambda: ["trend", "services", "regions", "resources"])
    live: list[str] = Field(default_factory=list)  # waste / rightsizing / esu / advisor
    # The display currency the report was ordered in. A file that disagrees with the screen it
    # was exported from is worse than no file — someone will paste it into a deck.
    currency: str = Field("", max_length=8)


@app.get("/api/report/options")
async def report_options(request: Request, days: int = 30, scope: str = "",
                         currency: str = "") -> JSONResponse:
    """What this person could put in a report — the areas that actually have spend, plus the
    live datasets. Driven by their data and their scope, so nothing offered comes back empty."""
    from .dashboard import get_dashboard
    from .report import BLOCKS, FORMATS, LIVE

    user = current_user(request)
    allowed = await permitted(user)
    picked = narrow([s for s in scope.split(",") if s.strip()], allowed)

    summary = await offload(get_dashboard().sections, picked or None,
                            days=max(1, min(days, 365)), currency=currency or None)
    return JSONResponse({
        "areas": [{"id": s["id"], "label": s["label"], "cost": s["cost"]}
                  for s in summary.get("sections", []) if s.get("cost")],
        "blocks": list(BLOCKS),
        "live": [{"id": k, "label": v} for k, v in LIVE.items()],
        "formats": [{"id": k, "label": v[0]} for k, v in FORMATS.items()],
        "currency": summary.get("currency"),
        "as_of": summary.get("as_of"),
    })


@app.post("/api/report")
async def build_report(body: ReportRequest, request: Request) -> Response:
    """Build exactly what was selected. Live datasets are fetched only if asked for."""
    from . import reporting

    try:
        month = body.period_month if body.period_month is not None else reporting.selected_month()
        with reporting.select(month):
            return await _report_response(request, body)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc


async def _report_response(request: Request, body: ReportRequest,
                           probe: bool = False) -> Response:
    """Shared by the POST builder and the GET download link.

    Kept in one place because a browser can only *link* to a GET, and a link is what makes the
    browser download a file natively — with its own progress and its own downloads list —
    instead of the page having to fetch bytes and synthesise a click.
    """
    from . import currency as fx
    from . import reporting
    from .report import FORMATS, LIVE, build

    user = current_user(request)
    allowed = await permitted(user)
    picked = narrow([s for s in body.scope if s.strip()], allowed)

    if body.format not in FORMATS:
        raise HTTPException(404, f"Unknown format: {body.format}")

    wanted_live = [name for name in body.live if name in LIVE]

    # A probe answers "would this work?" without paying to build anything. The download link
    # uses it so an expired session becomes a message instead of a file that never arrives.
    if probe:
        return Response(status_code=204, headers={"Cache-Control": "no-store"})

    # These call Azure, so they are gathered here — inside the request's identity context —
    # rather than inside the synchronous report writer.
    live_data: dict[str, Any] = {}
    if wanted_live:
        from .esu import esu_report
        from .waste import advisor_recommendations, find_waste, vm_utilisation

        jobs = {
            "waste": lambda: find_waste(subscription_ids=picked or None, days=body.days, top=25),
            "rightsizing": lambda: vm_utilisation(subscription_ids=picked or None,
                                                  days=min(body.days, 90)),
            "esu": lambda: esu_report(subscription_ids=picked or None, days=body.days),
            "advisor": lambda: advisor_recommendations(subscription_ids=picked or None,
                                                       category="Cost"),
        }
        with reporting.select(None):
            async with as_user(user):
                results = await asyncio.gather(*(jobs[n]() for n in wanted_live),
                                               return_exceptions=True)
        for name, result in zip(wanted_live, results):
            if isinstance(result, Exception):
                # One unavailable dataset must not lose the rest of the report.
                log.warning("report: live %s failed: %s", name, str(result)[:200])
                continue
            # These come from Azure in USD and never touch the warehouse, so they are converted
            # the same way their tabs are — otherwise a report ordered in rupees would have a
            # rupee summary and dollar savings sitting on the next page.
            live_data[name] = fx.convert_money(result, body.currency)
            live_data[name]["reporting_note"] = reporting.LIVE_NOTE

    selection = {
        "summary": body.summary,
        "sections": body.sections,
        "blocks": body.blocks,
        "live": wanted_live,
    }

    try:
        payload, filename, media = await run_in_threadpool(
            build, body.format, picked or None, body.days, selection, live_data,
            body.currency or None)
    except Exception as exc:  # noqa: BLE001
        log.exception("report generation failed")
        raise HTTPException(500, f"Could not build the report: {str(exc)[:200]}") from exc

    log.info("report %s (%d KB, live=%s) for %s", body.format, len(payload) // 1024,
             ",".join(wanted_live) or "none", user.email or user.name)
    return Response(
        content=payload,
        media_type=media,
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"',
            "Cache-Control": "no-store",
        },
    )


@app.get("/api/report/{fmt}")
async def download_report(fmt: str, request: Request, days: int = 30,
                          scope: str = "", summary: bool = True,
                          sections: str | None = None, blocks: str | None = None,
                          live: str = "", probe: int = 0,
                          currency: str = "") -> Response:
    """The dashboard as a file, as a plain link the browser can download natively.

    Everything the POST builder accepts can be expressed here as query parameters, so the UI
    never has to fetch bytes and fabricate a click: the button is a real link, and the browser
    handles the transfer, the filename and the downloads list itself.

    Generated synchronously — a warehouse-only report is a few hundred KB and takes well under
    a second. FastAPI runs the blocking build in a worker thread, so it never stalls the loop.
    """
    from .report import BLOCKS, FORMATS

    # Checked before the model is built, not after. `ReportRequest.format` is capped at eight
    # characters, so a longer value — "badformat" — raised a Pydantic ValidationError inside the
    # handler and surfaced as a bare 500, while "exe" and "pdf" got the clean 404 below. Same
    # class of mistake, two different answers, depending only on how long the word was.
    if fmt not in FORMATS:
        raise HTTPException(404, f"Unknown format: {fmt[:20]}. "
                                 f"Choose from: {', '.join(sorted(FORMATS))}.")

    def split(value: str | None, default: list[str] | None = None) -> list[str] | None:
        if value is None:
            return default
        return [part for part in value.split(",") if part.strip()]

    body = ReportRequest(
        format=fmt,
        days=max(1, min(days, 365)),
        scope=[s for s in scope.split(",") if s.strip()],
        summary=summary,
        sections=split(sections),                 # absent = every area
        blocks=split(blocks, list(BLOCKS)) or [],
        live=split(live, []) or [],
        currency=currency,
    )
    return await _report_response(request, body, probe=bool(probe))


@app.get("/api/reporting-period")
async def reporting_period(request: Request, days: int = 30, scope: str = "",
                           period_month: str | None = None) -> JSONResponse:
    from . import reporting
    from .warehouse import warehouse

    user = current_user(request)
    picked = selected_scope(scope.split(","), await permitted(user))
    try:
        with reporting.select(period_month):
            result = await offload(reporting.metadata, warehouse, picked, days,
                                   request_filters(request))
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    return JSONResponse(result)


@app.get("/api/filters")
async def filter_values(request: Request, days: int = 30, scope: str = "",
                        currency: str = "") -> JSONResponse:
    """What the filter bar can offer, read from the data rather than guessed.

    A filter listing values the estate does not contain is worse than no filter: it invites
    someone to narrow to nothing and then wonder which of the two is broken. So every option
    here came back from a `GROUP BY` over the period in view, ordered by what it costs — the
    resource group spending the most is the one most likely to be looked for, and an
    alphabetical list buries it among a hundred others.
    """
    from .warehouse import warehouse

    user = current_user(request)
    allowed = await permitted(user)
    picked = narrow([s for s in scope.split(",") if s.strip()], allowed)
    window = max(1, min(days, 365))

    def collect() -> dict[str, Any]:
        out: dict[str, Any] = {}
        # Every column here is now part of the rollup's grain, so `Reader.agg` answers
        # all five exactly. Before `ChargeCategory` and `PricingModel` were added to it,
        # those two alone scanned 5,821,770 detail rows to find seven values between
        # them -- 7.6-8.5s against 0.10-0.12s for the three the rollup already held.
        rollup_dims = (
            ("rg", '"ResourceGroup"'),
            ("service", '"ServiceName"'),
            ("region", '"RegionName"'),
            ("charge", '"ChargeCategory"'),
            ("pricing", '"PricingModel"'),
        )
        unavailable: list[str] = []

        def absorb(rows: Any) -> None:
            for row in rows:
                if row["v"]:
                    item = next((v for v in out[row["d"]]
                                 if v["value"].lower() == row["v"].lower()), None)
                    if item is None:
                        item = {"value": row["v"], "cost": None,
                                "currency": None, "currency_buckets": {}}
                        out[row["d"]].append(item)
                    item["currency_buckets"][row["currency"] or "UNKNOWN"] = row["c"]

        def ranked(recent: str, grouped: str, cap: int) -> str:
            return (f"WITH recent AS MATERIALIZED ({recent}), grouped AS ({grouped}), "
                    "ranked AS (SELECT d, v, currency, c, dense_rank() OVER "
                    "(PARTITION BY d ORDER BY lower(v)) AS rn FROM grouped) "
                    f"SELECT d, v, currency, c FROM ranked WHERE rn <= {cap} "
                    "ORDER BY d, c DESC")

        def by_column(dims: tuple[tuple[str, str], ...], source: str, since: str) -> str:
            # Materialise the date/scope cut once. The old implementation scanned the same
            # cost view six times in sequence; on a large estate that made this secondary
            # control hold the database for several seconds and queue the dashboard behind it.
            selected = ", ".join(
                [f"{column} AS {key}" for key, column in dims]
                + ['"BilledCost" AS cost', '"BillingCurrency" AS currency'])
            return ranked(
                f'SELECT {selected} FROM {source} WHERE "ChargePeriodStart" > {since}',
                " UNION ALL ".join(
                    f"SELECT '{key}' AS d, min({key}) AS v, currency, sum(cost) AS c "
                    f"FROM recent WHERE {key} IS NOT NULL AND {key} <> '' "
                    f"GROUP BY lower({key}), currency"
                    for key, _ in dims),
                200)

        with warehouse.reader(picked or None, currency=currency or None,
                              budget_ms=FILTER_BUDGET_MS) as r:
            # Anchored to the newest data, not to today: a warehouse that ends last Friday would
            # otherwise offer an empty list every Monday.
            newest = r.rows('SELECT max("ChargePeriodStart") AS d FROM costs', 1)
            hi = newest[0]["d"] if newest and newest[0]["d"] else None
            if not hi:
                return {"dimensions": {}, "empty": True}
            from .reporting import start_sql
            since = start_sql(hi, window)
            for key, _ in rollup_dims:
                out[key] = []
            out["env"] = []

            # `Reader.agg` is the rollup when it can answer this connection exactly, and
            # `costs` when it cannot -- a converted currency, or a filter on `env`. Either
            # way the same query reads the same five columns; only the speed differs.
            try:
                absorb(r.rows(by_column(rollup_dims, r.agg, since), 10000))
            except Exception as exc:  # noqa: BLE001 - the bar, not the page
                unavailable.extend(key for key, _ in rollup_dims)
                log.info("filter values: %s not available in time (%s)",
                         ", ".join(key for key, _ in rollup_dims), str(exc)[:120])

        # Fresh environment-aware summaries avoid raw JSON entirely. Keep the raw
        # fallback independently bounded for deployments not yet migrated and FX.
        try:
            with warehouse.reader(picked or None, currency=currency or None,
                                  budget_ms=FILTER_ENV_BUDGET_MS) as r:
                stored = (environment.ENABLED and not currency and r.agg == "costs_rollup"
                          and warehouse._rollup_fresh(r._con, "costs_rollup"))
                env_sql = '"Environment"' if stored else environment.RAW_SQL
                source = r.agg if stored else "costs"
                absorb(r.rows(ranked(
                    f'SELECT {env_sql} AS env, "BilledCost" AS cost, '
                    f'"BillingCurrency" AS currency FROM {source} '
                    f'WHERE "ChargePeriodStart" > {since}',
                    "SELECT 'env' AS d, min(env) AS v, currency, sum(cost) AS c FROM recent "
                    "WHERE env IS NOT NULL AND env <> '' GROUP BY env, currency",
                    50), 10000))
        except Exception as exc:  # noqa: BLE001 - one dimension, not the whole bar
            unavailable.append("env")
            log.info("filter values: the environment tag was not available in time (%s)",
                     str(exc)[:120])

        for values in out.values():
            for item in values:
                if len(item["currency_buckets"]) == 1:
                    item["currency"], item["cost"] = next(iter(item["currency_buckets"].items()))
        result = {"dimensions": out, "as_of": str(hi), "days": window,
                  "currency": currency.upper() or None,
                  "monetary_contract": "currency_buckets", "dimension_limit": 200}
        if unavailable:
            # Named rather than silent: an empty list here would otherwise be
            # indistinguishable from a dimension the estate genuinely never uses.
            result["unavailable_dimensions"] = unavailable
        return result

    try:
        return JSONResponse(await offload(collect))
    except Exception as exc:  # noqa: BLE001 - an unavailable filter bar must not fail the page
        log.warning("filter values failed: %s", str(exc)[:200])
        return JSONResponse({"dimensions": {}, "unavailable": True})


@app.get("/api/dashboard/{section_id}")
async def dashboard_section(section_id: str, request: Request,
                            days: int = 30, scope: str = "",
                            currency: str = "") -> JSONResponse:
    """One tab's figures. Warehouse only, so this is milliseconds and never calls the model."""
    from .dashboard import get_dashboard

    user = current_user(request)
    allowed = await permitted(user)
    picked = narrow([s for s in scope.split(",") if s.strip()], allowed)

    try:
        return JSONResponse(
            await offload(get_dashboard().section, section_id, picked or None,
                          days=max(1, min(days, 365)), currency=currency,
                          filters=request_filters(request)))
    except KeyError:
        raise HTTPException(404, f"No such section: {section_id}") from None


@app.get("/api/capabilities")
async def access_capabilities(request: Request, scope: str = "",
                              mode: Literal["caller", "workload"] = "caller",
                              refresh: bool = False) -> JSONResponse:
    from . import archive, capabilities, cost, schedules

    user = require_admin(request) if mode == "workload" else current_user(request)
    selected = selected_scope(scope.split(","), await permitted(user))
    async with as_user(user):
        subscriptions = await cost._resolve(selected)
    ids = [sub["id"] for sub in subscriptions]
    if not ids:
        raise HTTPException(400, "Select at least one accessible subscription.")
    workload = {
        "id": os.getenv("CLOUDLENS_PRINCIPAL_ID") or None,
        "tenant_id": os.getenv("CLOUDLENS_TENANT_ID") or None,
        "kind": "workload",
    }
    delegated = user.source == "entra" and auth_service().sso.delegated and mode == "caller"
    if delegated:
        session = auth_service().sso.get(user.sid)
        if session is None:
            raise HTTPException(401, "Your Azure session expired. Sign in again.")
        identity = {"id": session.oid or None, "tenant_id": session.tenant,
                    "kind": "delegated-user", "by_subscription": {
                        sub["id"]: {
                            "id": session.oid if (sub.get("tenant") or session.tenant)
                            .lower() == session.tenant.lower() else None,
                            "tenant_id": sub.get("tenant") or session.tenant,
                        } for sub in subscriptions}}
    else:
        identity = workload
    storage = []
    account = archive.ACCOUNT_ID.rstrip("/")
    account_sub = account.split("/")[2].lower() if account.startswith("/subscriptions/") else ""
    if account_sub and account_sub in {sub.lower() for sub in ids}:
        for purpose, container, principal in (
            ("focus-read", schedules.FOCUS_CONTAINER, workload),
            ("focus-write", schedules.FOCUS_CONTAINER,
             {"id": None, "tenant_id": workload["tenant_id"], "kind": "export-managed-identity"}),
            ("archive-write", archive.CONTAINER, workload),
        ):
            storage.append({"purpose": purpose,
                            "scope": f"{account}/blobServices/default/containers/{container}",
                            "principal": principal})
    try:
        async with as_user(user):
            reset = cost._caller.set(None) if mode == "workload" else None
            reset_id = cost._caller_id.set("server") if mode == "workload" else None
            try:
                result = await capabilities.report(ids, identity=identity, storage=storage,
                                                   force=refresh)
            finally:
                if reset is not None:
                    cost._caller.reset(reset)
                if reset_id is not None:
                    cost._caller_id.reset(reset_id)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    result["read_only"] = read_only_mode()
    result["mode"] = mode
    result["workload_client_id"] = os.getenv("AZURE_CLIENT_ID") or None
    return JSONResponse(result, headers={"Cache-Control": "private, no-store"})


app.mount("/assets", _static_assets, name="assets")


@app.get("/")
async def index() -> Response:
    html = await offload((WEB / "index.html").read_text, encoding="utf-8")
    return Response(await offload(stamp_asset_urls, html, _static_assets),
                    media_type="text/html", headers={"Cache-Control": "no-store"})


install_snapshot_routes(app, lambda: _snapshot_cache, authorize_snapshot)
