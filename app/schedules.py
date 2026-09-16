"""Scheduled Cost Management exports, created and owned by CloudLens.

Why this exists: refreshing through the Cost Details API means asking Azure to *generate* a
report and then waiting for it. One report is about forty seconds; a three-month refresh across
three subscriptions is nine of them, and the per-subscription submission limit forces them into
a queue. That is minutes of waiting for data Azure could just as easily have written to a blob
overnight.

A scheduled export inverts it. Azure writes the data on its own timetable, and a refresh becomes
a blob read -- seconds, not minutes, and no rate limit to queue behind.

The thing that makes this possible here is **managed identity**. A Cost Management export
normally authenticates to its destination with a storage account key, and this tenant forces
`allowSharedKeyAccess=false` on every account, so the obvious approach fails with "Key-based
authentication is currently disabled". Creating the export with `identity: SystemAssigned` and
granting *that* identity Storage Blob Data Contributor sidesteps keys entirely. Verified working
on 2026-08-28.

One Azure limit shapes what this module offers:

  * **Each export covers one subscription.** Scheduling across an estate is one export per
    subscription, which is why `ensure` takes a list and reports per-subscription results.

It used to record a second: that FocusCost was unavailable at subscription scope. That was
never true — it was the pinned API version refusing it. See EXPORT_API below.
"""
from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import re
from datetime import UTC, date, datetime, timedelta
from typing import Any

log = logging.getLogger("cloudlens.schedules")

# The version that accepts `identity` on an export. Older versions silently ignore it and fall
# back to key auth, which fails on a locked-down account -- so this is pinned deliberately.
#
# 2025-03-01 rather than 2023-08-01 for a second reason: FocusCost. The older version refuses
# it outright -- `Invalid definition type 'FocusCost'; valid values: 'ActualCost,
# 'AmortizedCost'` -- and because that 400 arrives on a subscription-scoped request, it read as
# "Azure does not allow FOCUS at subscription scope" and was written down here as such. It is
# not a scope limit, it is a version limit: the same body against 2025-03-01 creates the export
# and mints its identity. This version also *lists* FOCUS exports that 2023-08-01 silently
# omits, so exports created in the portal were invisible to the app.
EXPORT_API = "2025-03-01"
STORAGE_API = "2023-05-01"
ROLE_API = "2022-04-01"

# Storage Blob Data Contributor. The export writes and overwrites, so Reader is not enough.
BLOB_CONTRIBUTOR = "ba92f5b4-2d11-453d-a403-e96b0029c9fe"

# What a schedule may produce. FOCUS first: it is the one schema that carries actual and
# amortized together, so it answers both questions from one file and is what the refresh
# prefers when it finds one.
METRICS = ("FocusCost",)
RECURRENCE = ("Daily", "Weekly", "Monthly")
FOCUS_CONTAINER = os.getenv("FOCUS_EXPORT_CONTAINER", "focus-exports").strip()
FOCUS_PREFIX = os.getenv("FOCUS_EXPORT_PREFIX", "focus").strip("/")

NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,62}$")


class ScheduleError(RuntimeError):
    """Something the caller can act on, phrased for them."""


def _arm() -> str:
    from . import cost

    return cost.ARM


async def _request(method: str, url: str, *, body: dict | None = None,
                   params: dict | None = None, cache: bool = False) -> dict:
    from .job_policy import require_writable
    if method.upper() not in {"GET", "HEAD"}:
        require_writable()
    from . import cost

    result = await cost.azure.request(method, url, json_body=body, params=params, cache=cache)
    # A write invalidates every cached read of the exports collection on that subscription,
    # for everyone, the same way a budget write does. Without this, caching the listing below
    # would show a schedule that has just been created as absent -- or one just deleted as
    # still there -- which reads as the write having silently failed.
    if method not in ("GET", "HEAD"):
        cost.forget(_exports_scope(url))
    return result


def _exports_scope(url: str) -> str:
    """The exports collection a URL belongs to, so one write drops exactly that list.

    Everything here lives under `/subscriptions/{id}/providers/Microsoft.CostManagement/exports`,
    so truncating at that segment covers the collection, each export, and its runHistory —
    without touching another subscription's cached list.
    """
    marker = "/providers/Microsoft.CostManagement/exports"
    head, sep, _ = url.partition(marker)
    return f"{head}{marker}" if sep else url


def export_name(metric: str, *, account_id: str, container: str, folder: str) -> str:
    """Cadence-independent identity, isolated by deployment destination and subscription folder."""
    key = f"{account_id.lower().rstrip('/')}|{container}|{folder}"
    return f"cloudlens-focus-{hashlib.sha256(key.encode()).hexdigest()[:20]}"


def destination(subscription_id: str) -> dict[str, str]:
    from . import archive

    if not NAME_RE.fullmatch(subscription_id):
        raise ScheduleError("Invalid subscription id.")
    return {"account_id": archive.ACCOUNT_ID, "container": FOCUS_CONTAINER,
            "folder": f"{FOCUS_PREFIX}/{subscription_id.lower()}".strip("/")}


def coverage(recurrence: str) -> str:
    return ("Previous calendar month (TheLastMonth); Azure may later restate charges."
            if recurrence == "Monthly" else
            "Current month to date (MonthToDate), not a rolling day or week.")


def owned(export: dict, subscription_id: str) -> bool:
    dest = destination(subscription_id)
    actual = ((export.get("properties") or {}).get("deliveryInfo") or {}).get("destination") or {}
    return bool(dest["account_id"]) and (
        export.get("name") == export_name("FocusCost", **dest)
        and str(actual.get("resourceId", "")).lower() == dest["account_id"].lower()
        and actual.get("container") == dest["container"]
        and actual.get("rootFolderPath") == dest["folder"]
        and ((export.get("properties") or {}).get("definition") or {}).get("type") == "FocusCost"
    )


def _destination(account_id: str, container: str, folder: str) -> dict[str, Any]:
    return {"destination": {"type": "AzureBlob", "resourceId": account_id, "container": container,
                            "rootFolderPath": folder}}


def _body(metric: str, account_id: str, container: str, folder: str,
          recurrence: str = "Daily", years: int = 3) -> dict[str, Any]:
    """Retain every run; monthly means the previous calendar month, not MTD."""
    now = datetime.now(UTC)
    # Start tomorrow. An export whose window opens in the past is rejected, and one starting in
    # the next few minutes races the caller's own clock skew.
    start = (now + timedelta(days=1)).replace(hour=2, minute=0, second=0, microsecond=0)
    if recurrence == "Monthly":
        start = (now.replace(day=1) + timedelta(days=32)).replace(
            day=1, hour=2, minute=0, second=0, microsecond=0)
    from . import archive

    return {
        "identity": {"type": "SystemAssigned"},
        # Required alongside identity, and must be a real region. Taken from the deployment
        # rather than hardcoded: an export stamped with a region this stack does not live in
        # is refused in every tenant except the one the constant was written for.
        "location": archive.LOCATION,
        "properties": {
            "schedule": {
                "status": "Active",
                "recurrence": recurrence,
                "recurrencePeriod": {
                    "from": start.isoformat().replace("+00:00", "Z"),
                    "to": (start + timedelta(days=365 * years)).isoformat().replace("+00:00", "Z"),
                },
            },
            "format": "Csv",
            "partitionData": True,
            "dataOverwriteBehavior": "CreateNewReport",
            "deliveryInfo": _destination(account_id, container, folder),
            "definition": {
                "type": metric,
                "timeframe": "TheLastMonth" if recurrence == "Monthly" else "MonthToDate",
                "dataSet": {"granularity": "Daily"},
            },
        },
    }


async def _grant_storage(principal_id: str, account_id: str) -> str:
    """Give the export's own identity permission to write to the destination.

    The identity is created by Azure when the export is saved, so this cannot be done in
    advance -- and without it every run fails with a 403 that names nothing the operator has
    heard of. An existing assignment comes back 409, which is success.
    """
    import uuid

    url = f"{_arm()}{account_id}/providers/Microsoft.Authorization/roleAssignments/{uuid.uuid4()}"
    body = {"properties": {
        "roleDefinitionId": ("/providers/Microsoft.Authorization/roleDefinitions/"
                             + BLOB_CONTRIBUTOR),
        "principalId": principal_id,
        "principalType": "ServicePrincipal",
    }}
    try:
        await _request("PUT", url, body=body, params={"api-version": ROLE_API})
        return "granted"
    except Exception as exc:  # noqa: BLE001
        text = str(exc)
        if "RoleAssignmentExists" in text or "409" in text:
            return "already granted"
        if "AuthorizationFailed" in text or "403" in text:
            raise ScheduleError(
                f"The export was created but its managed identity {principal_id} needs "
                f"Storage Blob Data Contributor on {account_id}. Ask the deployment administrator "
                "to grant that scoped access, then retry missing work. No broad roles are required "
                "for the application to run once this access is provisioned."
            ) from exc
        raise ScheduleError(f"Could not grant the export access to storage: {text[:200]}") from exc


async def create(subscription_id: str, metric: str, *, account_id: str, container: str,
                 folder: str | None = None, recurrence: str = "Daily",
                 run_now: bool = True, etag: str | None = None,
                 native_active: bool = True) -> dict[str, Any]:
    """Create or update one scheduled export, and make it able to write.

    A PUT, so re-running is an update rather than a duplicate. The role grant and the first run
    follow, because an export that exists but cannot write, or that will not produce anything
    until 2am tomorrow, is not what someone asking for this actually wants.
    """
    if metric not in METRICS:
        raise ScheduleError(
            "New schedules support FOCUS (FocusCost) only. ActualCost and AmortizedCost "
            "are legacy choices; existing schedules remain available."
        )
    if recurrence not in RECURRENCE:
        raise ScheduleError(f"Recurrence must be one of {', '.join(RECURRENCE)}.")

    if not NAME_RE.fullmatch(subscription_id):
        raise ScheduleError("Invalid subscription id.")
    folder = folder or destination(subscription_id)["folder"]
    if not account_id or not container:
        raise ScheduleError("Configure ARCHIVE_ACCOUNT_ID and FOCUS_EXPORT_CONTAINER first.")
    name = export_name(metric, account_id=account_id, container=container, folder=folder)
    url = (f"{_arm()}/subscriptions/{subscription_id}"
           f"/providers/Microsoft.CostManagement/exports/{name}")
    body = _body(metric, account_id, container, folder, recurrence)

    try:
        try:
            existing = await _request("GET", url, params={"api-version": EXPORT_API})
        except Exception as exc:
            if "404" not in str(exc) and "ExportNotFound" not in str(exc):
                raise
            existing = None
        if existing:
            old = existing.get("properties") or {}
            dest = (old.get("deliveryInfo") or {}).get("destination") or {}
            if (str(dest.get("resourceId", "")).lower() != account_id.lower()
                    or dest.get("container") != container or dest.get("rootFolderPath") != folder
                    or (old.get("definition") or {}).get("type") != metric):
                raise ScheduleError("That export name belongs to another destination; no update made.")
            current_etag = existing.get("eTag")
            if not current_etag or (etag is not None and etag != current_etag):
                raise ScheduleError("The schedule changed. Reload it before saving; no update made.")
            body["eTag"] = current_etag
            if (old.get("schedule") or {}).get("recurrence") == recurrence:
                body["properties"]["schedule"] = old["schedule"]
            body["location"] = existing.get("location") or body["location"]
        elif etag is not None:
            raise ScheduleError("The schedule was deleted. Reload before creating it again.")
        if not native_active:
            body["properties"]["schedule"]["status"] = "Inactive"
        made = await _request("PUT", url, body=body, params={"api-version": EXPORT_API})
    except ScheduleError:
        raise
    except Exception as exc:  # noqa: BLE001
        # Logged in full before it is turned into a sentence. The user-facing message has to be
        # short, and every time this failed the one thing nobody could get at was what Azure
        # actually said — which is where the answer was.
        log.warning("export PUT %s failed: %s", name, str(exc)[:800])
        raise ScheduleError(_explain_create(str(exc), subscription_id)) from exc

    principal = ((made.get("identity") or {}).get("principalId")) if isinstance(made, dict) else None
    if not principal:
        raise ScheduleError("The export was saved, but Azure did not return its managed identity. "
                            "Storage access and the first run could not be configured.")
    grant = await _grant_storage(
        principal, f"{account_id}/blobServices/default/containers/{container}")

    started = False
    run_error = None
    if run_now and principal:
        # Role assignments take a few seconds to reach the data plane. Running immediately
        # produces a 403 and a failed run in the history, which reads as a broken schedule.
        await asyncio.sleep(20)
        try:
            await _request("POST", f"{url}/run", params={"api-version": EXPORT_API})
            started = True
        except Exception as exc:  # noqa: BLE001 - the schedule is still valid either way
            log.warning("first run of %s failed: %s", name, str(exc)[:200])
            run_error = str(exc)[:300]

    return {
        "name": name,
        "subscription": subscription_id,
        "metric": metric,
        "recurrence": recurrence,
        "coverage": coverage(recurrence),
        "retention": "All runs retained; no automatic deletion. Storage charges grow.",
        "destination": f"{account_id.rsplit('/', 1)[-1]}/{container}/{folder}",
        "identity": principal,
        "storage_access": grant,
        "first_run_started": started,
        "first_run_error": run_error,
    }


def _explain_create(detail: str, subscription_id: str) -> str:
    """The refusals that actually happen, in words rather than as an ARM code."""
    low = detail.lower()
    if "key-based authentication" in low or "allow storage account key access" in low:
        return (
            "Azure tried to authenticate to storage with an account key, which this tenant "
            "disables. That happens when the export is created without a managed identity — "
            "this app always sets one, so if you are seeing this, the API version in use does "
            "not support it."
        )
    # The one that cost a long afternoon. Creating an export with `identity: SystemAssigned`
    # makes Azure mint an identity for the export and then grant it access to the destination
    # container *on the caller's behalf* — so the caller needs rights to hand out roles there,
    # not merely to write there. The refusal arrives as HTTP 401 with code RBACAccessDenied and
    # the message "The client does not have authorization to perform action", which names
    # neither the action nor the resource, and reads exactly like an expired token.
    #
    # `rbacaccessdenied` is matched here too, and that matters: the branch above was written
    # from the comment rather than from the wire, and Azure does not actually put
    # "roleAssignments/write" in the body. So the one refusal this function exists to explain
    # fell through to the generic handler and was reported as "Azure rejected the credential
    # ... check the tenant it was issued for and whether it has expired" — sending the reader
    # to debug a token that was perfectly valid.
    if ("roleassignments/write" in low.replace(" ", "")
            or "supplying identity in payload" in low
            or "rbacaccessdenied" in low):
        return (
            "Azure would not let CloudLens create this export, and on a correctly locked-down "
            "deployment it never will. A Cost Management export authenticates to storage with "
            "a managed identity, and the API accepts only a system-assigned one — so Azure "
            "mints a new identity and the caller must then grant it Storage Blob Data "
            "Contributor on the destination. That needs "
            "Microsoft.Authorization/roleAssignments/write, and CloudLens's runtime identity is "
            "deliberately not given it: an application that can hand out roles can widen its own "
            "access, which a read-only cost tool must not be able to do. "
            "Provision the exports once as yourself instead — run infra/focus-exports.ps1 from "
            "the CloudLens repository. It creates exactly the exports this deployment expects, "
            "grants each one storage access, and is safe to re-run. Quick refresh keeps reading "
            "estimates through ARM meanwhile."
        )
    # Checked before the generic 403 branch: an RBAC-shaped word appears in both, and the
    # scope-lock refusal is the one that cannot be fixed by granting a role.
    if "rbacdisallowedoperation" in low or "disallowed by policy" in low:
        return (
            "Azure refused the write itself rather than the identity: this subscription's "
            "cost data is managed at a scope above it — a management group or billing account "
            "— and subscription-level exports are blocked there. Roles will not change this; "
            f"the export has to be created at the owning scope. Azure said: {detail[:400]}"
        )
    if "authorizationfailed" in low or "403" in low:
        return (f"Not allowed to create an export on {subscription_id}. This needs Cost "
                "Management Contributor or Contributor on the subscription.")
    if "invalid definition type" in low:
        # The message that caused the confusion this module used to encode: it names the type,
        # not the API version, so it reads as a scope restriction. It is neither — it means the
        # request went out on a version too old to know the type.
        return ("Azure does not recognise that report type on the API version this request "
                f"used. CloudLens asks for {EXPORT_API}, which supports FOCUS, Amortized and "
                f"Actual. Azure said: {detail[:300]}")
    return f"Azure refused the export: {detail[:400]}"


async def listing(subscription_ids: list[str] | None = None, *,
                  fresh: bool = False, owned_only: bool = False) -> dict[str, Any]:
    """Every export CloudLens can see, with its last run — the Cost exports tab's whole content.

    Read through the cache. This is one ARM call per subscription plus one per export to expand
    `runHistory`, and on this tenant that is seventeen uncached round trips at concurrency four
    -- 16.5s before the Reports tab could draw. Export definitions change when somebody changes
    them, and `_request` purges this subscription's entries on every write, so a cached read is
    never older than the last change made through CloudLens.

    `fresh` bypasses it, for the one case the invalidation cannot see: an export created or
    deleted in the Azure portal, by someone else. That is what the tab's "Reload list" button
    is for, and a reload that returns the identical cached answer is not a reload.
    """
    from . import cost

    subs = await cost._resolve(subscription_ids)

    errors: list[dict[str, str]] = []

    async def one(sub: dict) -> list[dict]:
        base = (f"{_arm()}/subscriptions/{sub['id']}"
                f"/providers/Microsoft.CostManagement/exports")
        if fresh:
            cost.forget(base)
        try:
            payload = await _request("GET", base, params={"api-version": EXPORT_API},
                                     cache=True)
        except Exception as exc:  # noqa: BLE001 - one unreadable subscription is not an outage
            log.info("could not list exports on %s: %s", sub["id"][:8], str(exc)[:120])
            errors.append({"subscription": sub["id"], "detail": str(exc)[:200]})
            return []

        gate = asyncio.Semaphore(4)

        async def with_history(e: dict) -> dict:
            if not NAME_RE.fullmatch(e.get("name") or ""):
                return e
            try:
                async with gate:
                    return await _request("GET", f"{base}/{e['name']}",
                                          params={"api-version": EXPORT_API,
                                                  "$expand": "runHistory"},
                                          cache=True)
            except Exception:
                return e

        entries = ([e for e in payload.get("value", []) if owned(e, sub["id"])]
                   if owned_only else await asyncio.gather(
                       *(with_history(e) for e in payload.get("value", []))))
        out = []
        for e in entries:
            p = e.get("properties") or {}
            dest = (p.get("deliveryInfo") or {}).get("destination") or {}
            sched = p.get("schedule") or {}
            ours = owned(e, sub["id"])
            history = (p.get("runHistory") or {}).get("value") or []
            runs = [r.get("properties", r) for r in history]
            last = max(runs, key=lambda r: r.get("executionTime") or r.get("submittedTime") or "",
                       default={})
            out.append({
                "name": e.get("name"),
                "subscription": sub["name"],
                "subscription_id": sub["id"],
                "metric": (p.get("definition") or {}).get("type"),
                "format": p.get("format"),
                "recurrence": sched.get("recurrence"),
                "status": sched.get("status"),
                "account": (dest.get("resourceId") or "").rsplit("/", 1)[-1],
                "container": dest.get("container"),
                "folder": dest.get("rootFolderPath"),
                "identity": (e.get("identity") or {}).get("type") == "SystemAssigned",
                "owned": ours,
                "legacy": not ours,
                "etag": e.get("eTag"),
                "timeframe": (p.get("definition") or {}).get("timeframe"),
                "coverage": coverage(sched.get("recurrence")) if ours else
                    (p.get("definition") or {}).get("timeframe"),
                "retention": p.get("dataOverwriteBehavior") or "CreateNewReport",
                "last_run": last.get("executionTime") or last.get("submittedTime"),
                "last_status": last.get("status") or "Not reported by Azure",
                # Two different times, and they were previously both wrong. `recurrencePeriod.
                # from` is when the schedule *starts* -- which for anything created here is
                # roughly when it was created, since `_body` sets it to tomorrow. It was called
                # `next_run`. `nextRunTimeEstimate` is the genuine next run, and it was called
                # `last_run` and then never used, so the one useful figure Azure returns about
                # a schedule's future was fetched and discarded.
                "starts": (sched.get("recurrencePeriod") or {}).get("from"),
                "next_run": p.get("nextRunTimeEstimate"),
            })
        return out

    results = await asyncio.gather(*(one(s) for s in subs))
    found = [x for r in results for x in r]
    # Newest first. Alphabetical order buried a schedule someone had just created among a dozen
    # others -- on this tenant the list is fourteen long, and "did that work?" is the question
    # being asked the moment the tab redraws. `starts` is the closest thing Azure gives to a
    # creation time; the name breaks ties so the order is stable between renders.
    found.sort(key=lambda e: (e["starts"] or "", e["name"] or ""), reverse=True)
    return {
        "schedules": found,
        "count": len(found),
        "managed": len([e for e in found if e["owned"]]),
        "metrics": list(METRICS),
        "destination": {**destination("subscription-id"),
                        "folder": f"{FOCUS_PREFIX}/<subscription-id>".strip("/")},
        "subscriptions": [{"id": s["id"], "name": s["name"]} for s in subs],
        "errors": errors,
        "partial": bool(errors),
    }


async def run_now(subscription_id: str, name: str, *, current_month: bool = False,
                  start: str | None = None, end: str | None = None) -> dict[str, Any]:
    """Ask Azure to produce this export immediately rather than waiting for its schedule."""
    if not NAME_RE.fullmatch(subscription_id) or not NAME_RE.fullmatch(name):
        raise ScheduleError("Invalid subscription id or export name.")
    url = (f"{_arm()}/subscriptions/{subscription_id}"
           f"/providers/Microsoft.CostManagement/exports/{name}/run")
    options = {}
    if start is not None or end is not None:
        if current_month or not start or not end:
            raise ScheduleError("Choose current_month or an explicit start and end, not both.")
        try:
            lo, hi = date.fromisoformat(start), date.fromisoformat(end)
        except ValueError as exc:
            raise ScheduleError("Execution dates must be ISO UTC calendar dates.") from exc
        if lo > hi or lo.strftime("%Y-%m") != hi.strftime("%Y-%m") or hi > datetime.now(UTC).date():
            raise ScheduleError("Execution windows must be ordered, month-bounded and not in the future.")
        options["body"] = {"timePeriod": {
            "from": f"{lo.isoformat()}T00:00:00Z",
            "to": (datetime.now(UTC).isoformat().replace("+00:00", "Z")
                   if hi == datetime.now(UTC).date() else f"{hi.isoformat()}T23:59:59Z"),
        }}
    elif current_month:
        now = datetime.now(UTC)
        options["body"] = {"timePeriod": {
           "from": now.replace(day=1, hour=0, minute=0, second=0, microsecond=0).isoformat(),
           "to": now.isoformat(),
        }}
    try:
        await _request("POST", url, params={"api-version": EXPORT_API}, **options)
    except Exception as exc:  # noqa: BLE001
        raise ScheduleError(f"Could not start {name}: {str(exc)[:200]}") from exc
    return {"name": name, "started": True,
            "requested_window": {"from": start, "to": end} if start else None}


async def remove(subscription_id: str, name: str) -> dict[str, Any]:
    """Delete a schedule. The data it already wrote is left alone."""
    if not NAME_RE.fullmatch(subscription_id) or not NAME_RE.fullmatch(name):
        raise ScheduleError("Invalid subscription id or export name.")
    url = (f"{_arm()}/subscriptions/{subscription_id}"
           f"/providers/Microsoft.CostManagement/exports/{name}")
    try:
        await _request("DELETE", url, params={"api-version": EXPORT_API})
    except Exception as exc:  # noqa: BLE001
        raise ScheduleError(f"Could not delete {name}: {str(exc)[:200]}") from exc
    return {"name": name, "deleted": True}


async def set_native_active(subscription_id: str, *, active: bool, etag: str | None) -> None:
    """An explicitly previewed cadence change; never touch another deployment's export."""
    dest = destination(subscription_id)
    name = export_name("FocusCost", **dest)
    url = f"{_arm()}/subscriptions/{subscription_id}/providers/Microsoft.CostManagement/exports/{name}"
    existing = await _request("GET", url, params={"api-version": EXPORT_API})
    if not owned(existing, subscription_id) or not etag or existing.get("eTag") != etag:
        raise ScheduleError("The owned schedule changed; preview the cadence change again.")
    properties = existing["properties"]
    properties["schedule"]["status"] = "Active" if active else "Inactive"
    if active:
        properties["schedule"]["recurrence"] = "Daily"
        properties["definition"]["timeframe"] = "MonthToDate"
    body = {key: existing[key] for key in ("eTag", "location", "identity", "properties") if key in existing}
    await _request("PUT", url, body=body, params={"api-version": EXPORT_API})
