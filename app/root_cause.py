"""What changed, near the day a cost anomaly happened.

The anomalies tab says a resource group's spend broke its own pattern on a given day; it does
not say why, because "why" lives in a different Azure API than cost data does at all -- the
Activity Log, which is the tenant's own record of every write and delete anyone made through ARM.

Scoped to the *resource group* dimension specifically, and to that dimension alone:
`resourceGroupName` is a field Activity Log actually lets a server-side `$filter` match, and a
resource group is usually close to the blast radius a single change lands in. A resource-level
anomaly's own display name is not reliably the same string as any one `resourceId` -- the
warehouse derives it from the id after load, and Activity Log wants the id itself -- so that
dimension is not attempted here rather than half-attempted with a name that may not match.

This is the only place in the app that reads Activity Log; there was no existing client for it.
"""
from __future__ import annotations

import logging
from datetime import date, timedelta
from typing import Any

log = logging.getLogger("cloudlens.root_cause")

# ARM's own convention: a state-changing operation's name ends in one of these two. Everything
# else -- list, get, and action verbs like restart or listKeys -- is a read or a transient action,
# neither of which can explain a cost that is still there weeks later.
_CHANGE_SUFFIXES = ("/write", "/delete")

# Azure logs one operation at several lifecycle stages (Started, then Succeeded or Failed); only
# the terminal ones represent something that actually happened.
_TERMINAL_STATUSES = {"Succeeded", "Failed"}

API_VERSION = "2015-04-01"


class RootCauseError(ValueError):
    """Something the caller can fix, phrased for them rather than for a log."""


async def find(*, resource_group: str, day: str,
               subscription_ids: list[str] | None) -> dict[str, Any]:
    """Management operations against one resource group, in the days around an anomaly.

    The window is the day before through the day after: a change made late in the evening in
    one timezone can show its cost impact starting only the next billing day, and an anomaly
    detector working in UTC dates is already a day of slack away from whoever made the change.
    """
    from .cost import ARM, CostError, azure, list_subscriptions

    resource_group = (resource_group or "").strip()
    if not resource_group:
        raise RootCauseError("No resource group to look at.")
    try:
        center = date.fromisoformat((day or "").strip())
    except ValueError as exc:
        raise RootCauseError(f"{day!r} is not a date Activity Log can be searched around.") \
            from exc

    subs = subscription_ids or [s["id"] for s in (await list_subscriptions())["subscriptions"]]
    if not subs:
        return {"resource_group": resource_group, "day": day, "changes": [], "count": 0,
                "note": "No subscriptions in scope."}

    start = f"{(center - timedelta(days=1)).isoformat()}T00:00:00Z"
    end = f"{(center + timedelta(days=2)).isoformat()}T00:00:00Z"
    escaped = resource_group.replace("'", "''")
    filter_ = (f"eventTimestamp ge '{start}' and eventTimestamp le '{end}' "
               f"and resourceGroupName eq '{escaped}'")

    events: list[dict[str, Any]] = []
    unreadable = 0
    for sub in subs:
        try:
            payload = await azure.request(
                "GET",
                f"{ARM}/subscriptions/{sub}"
                "/providers/Microsoft.Insights/eventtypes/management/values",
                params={"api-version": API_VERSION, "$filter": filter_},
            )
            events.extend(payload.get("value", []) or [])
        except CostError as exc:
            # One subscription's Activity Log being unreadable -- a role that covers cost but
            # not Monitor's own reader action -- must not blank the whole answer; the rest still
            # stand, and the count is disclosed rather than silently dropped.
            unreadable += 1
            log.warning("activity log read failed for %s: %s", sub, str(exc)[:150])

    changes = [
        e for e in events
        if str((e.get("operationName") or {}).get("value", "")).endswith(_CHANGE_SUFFIXES)
        and (e.get("status") or {}).get("value") in _TERMINAL_STATUSES
    ]

    # One logical operation is logged several times over its lifecycle, and can touch several
    # resources at once (a template deployment, a cascading delete) -- grouped by correlationId
    # so each shows once, as the change it was, not once per row Azure happened to log for it.
    groups: dict[str, list[dict[str, Any]]] = {}
    for e in changes:
        cid = e.get("correlationId") or e.get("eventDataId") or e.get("id") or ""
        groups.setdefault(cid, []).append(e)

    findings: list[dict[str, Any]] = []
    for group in groups.values():
        group.sort(key=lambda e: e.get("eventTimestamp") or "")
        latest = group[-1]
        resources = sorted({
            (e.get("resourceId") or "").rsplit("/", 1)[-1]
            for e in group if e.get("resourceId")
        })
        findings.append({
            "time": latest.get("eventTimestamp"),
            "caller": latest.get("caller"),
            "operations": sorted({
                (e.get("operationName") or {}).get("value", "") for e in group
            } - {""}),
            "resources": resources,
            "succeeded": any((e.get("status") or {}).get("value") == "Succeeded" for e in group),
        })

    findings.sort(key=lambda f: f["time"] or "", reverse=True)

    return {
        "resource_group": resource_group,
        "day": day,
        "changes": findings,
        "count": len(findings),
        "unreadable_subscriptions": unreadable,
        "note": (
            f"Writes and deletes in {resource_group} from the day before through the day after "
            "the anomaly, from Activity Log, grouped by the change that produced them -- reads "
            "and other actions are left out; they cannot explain a cost that is still there. "
            "A change here is a candidate, not a verdict: correlation in time is not causation, "
            "and the real cause may be a change to a related resource group, or a usage pattern "
            "with no deployment behind it at all."
            + (f" Activity Log could not be read for {unreadable} subscription(s) in scope, so "
               "this list may be incomplete." if unreadable else "")
        ),
    }
