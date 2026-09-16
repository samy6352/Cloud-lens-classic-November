"""Read-only, subscription-scoped Azure Resource Graph change evidence."""
from __future__ import annotations

import asyncio
import math
import re
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from .azure_updates import SourceError
from .observability import evidence_window, resource_costs, scoped_graph

SOURCE = "Azure Resource Graph (resourcechanges)"
MAX_CHANGES = 200
MAX_PROPERTIES = 20
MAX_TEXT = 500
REDACTED = "[redacted: property value not shown]"
NOTICES = [
    "Read-only Azure Resource Graph Change Analysis; change records are retained for up to "
    "14 days. Indexing and actor/property details can lag; this is not a complete audit log.",
    "Only allowlisted infrastructure property values are shown; other values are redacted. "
    "Create and Delete events may have no property differences or snapshots.",
    "Cost uses billed calendar dates for the evidence window, may be partial or delayed, "
    "and is not a before/after cost comparison. Missing cost is not zero cost. "
    "A change occurring alongside cost does not prove the change caused that cost.",
    "Operation is the recorded RBAC authorization action, not necessarily the actual "
    "operation performed. Actor information may be unavailable.",
]
_SAFE_PROPERTIES = {
    "location", "kind", "sku.name", "sku.size", "sku.capacity", "sku.tier", "sku.family",
    "properties.hardwareprofile.vmsize", "properties.sku.name", "properties.sku.size",
    "properties.sku.capacity", "properties.sku.tier", "properties.sku.family",
    "properties.tier", "properties.numberofworkers", "properties.currentnumberofworkers",
    "properties.targetworkercount", "properties.targetworkersizeid",
    "properties.maximumelasticworkercount", "properties.elasticscaleenabled",
    "properties.autoscaleenabled", "properties.enablescaleout", "properties.enabled",
}
_AUTOSCALE_PROPERTY = re.compile(
    r"properties\.profiles(?:\[\d+\]|\.\d+)\."
    r"(?:capacity\.(?:minimum|maximum|default)|rules(?:\[\d+\]|\.\d+)\."
    r"(?:scaleaction\.(?:direction|type|value|cooldown)|metrictrigger\."
    r"(?:threshold|operator|statistic|timeaggregation|timegrain|timewindow)))"
)
_INFRA_VALUE = re.compile(r"[A-Za-z0-9_. ,:/+\-]{1,500}")


def _text(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    return "".join(char for char in value if char >= " " and char != "\x7f")[:MAX_TEXT] or None


def _optional(value: Any) -> str | None:
    text = _text(value)
    return None if not text or text.strip().lower() in {"unknown", "unspecified", "null"} else text


def _date(value: Any) -> datetime:
    if not isinstance(value, str):
        raise SourceError("Resource Graph returned an invalid change timestamp.")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            raise ValueError("timezone missing")
        return parsed.astimezone(UTC)
    except ValueError as exc:
        raise SourceError("Resource Graph returned an invalid change timestamp.") from exc


def _target(value: Any, subscription: str) -> tuple[str, str, str]:
    if (not isinstance(value, str) or len(value) > 2048
            or any(char in value for char in "\\?#")
            or any(ord(char) < 32 for char in value)):
        raise SourceError("Resource Graph returned an invalid target resource ID.")
    parts = value.split("/")
    lower = [part.lower() for part in parts]
    if (len(parts) < 5 or parts[0] or lower[1] != "subscriptions"
            or lower[2] != subscription or any(part in {"", ".", ".."} for part in parts[1:])):
        raise SourceError("Resource Graph target resource ID does not match its subscription.")
    if len(parts) == 5 and lower[3] == "resourcegroups":
        return value, parts[4], "Microsoft.Resources/subscriptions/resourceGroups"
    offset = 3
    if lower[offset] == "resourcegroups":
        offset += 2
    resource_type = ""
    while offset < len(parts):
        if lower[offset] != "providers" or offset + 3 >= len(parts):
            raise SourceError("Resource Graph returned an invalid target resource ID.")
        namespace = parts[offset + 1]
        offset += 2
        types = []
        while offset < len(parts):
            types.append(parts[offset])
            if offset + 1 >= len(parts):
                raise SourceError("Resource Graph returned an invalid target resource ID.")
            offset += 2
            if offset < len(parts) and lower[offset] == "providers":
                break
        resource_type = namespace + "/" + "/".join(types)
    if not resource_type:
        raise SourceError("Resource Graph returned an invalid target resource ID.")
    return value, parts[-1], resource_type


def _safe_value(name: str, value: Any) -> Any:
    if name not in _SAFE_PROPERTIES and not _AUTOSCALE_PROPERTY.fullmatch(name):
        return REDACTED
    if value is None or isinstance(value, bool):
        return value
    if isinstance(value, int):
        return value if value.bit_length() <= 128 else REDACTED
    if isinstance(value, float):
        return value if math.isfinite(value) else REDACTED
    # Do not stringify objects: even a normally safe property can contain unexpected secrets.
    if isinstance(value, str) and _INFRA_VALUE.fullmatch(value[:MAX_TEXT]):
        if len(value) > MAX_TEXT or "://" in value or "bearer " in value.lower():
            return REDACTED
        return value
    return REDACTED


def _properties(value: Any) -> tuple[list[dict], bool]:
    if value is None:
        return [], False
    if not isinstance(value, dict) or any(not isinstance(key, str) for key in value):
        raise SourceError("Resource Graph returned invalid property differences.")
    properties = []
    truncated = len(value) > MAX_PROPERTIES
    for name, delta in list(value.items())[:MAX_PROPERTIES]:
        if not isinstance(delta, dict):
            raise SourceError("Resource Graph returned invalid property differences.")
        before, after = delta.get("previousValue"), delta.get("newValue")
        truncated |= (str(delta.get("isTruncated", "")).lower() == "true"
                      or len(name) > MAX_TEXT
                      or any(isinstance(item, str) and len(item) > MAX_TEXT
                             for item in (before, after)))
        properties.append({
            "name": _text(name) or "(unknown property)",
            "before": _safe_value(name.lower(), before),
            "after": _safe_value(name.lower(), after),
        })
    return properties, truncated


async def report(scope: list[str], days: int = 7) -> dict:
    if type(days) is not int or not 1 <= days <= 14:
        raise SourceError("Change Analysis requires a window between 1 and 14 days.")
    if not isinstance(scope, list) or not scope or len(scope) > 100:
        raise SourceError("Change Analysis requires between 1 and 100 explicit subscriptions.")
    try:
        scopes = sorted({str(UUID(value.strip())) for value in scope})
    except (ValueError, AttributeError, TypeError) as exc:
        raise SourceError("Change Analysis subscription scope must contain GUIDs.") from exc
    window = evidence_window(days)
    start, end = _date(window["start"]), _date(window["end"])
    query = f"""
        resourcechanges
        | extend changeTime = todatetime(properties.changeAttributes.timestamp)
        | where changeTime >= datetime({window['start']})
            and changeTime <= datetime({window['end']})
        | project id, subscriptionId, changedAt = changeTime,
            targetResourceId = tostring(properties.targetResourceId),
            targetResourceType = tostring(properties.targetResourceType),
            changeType = tostring(properties.changeType),
            changedBy = tostring(properties.changeAttributes.changedBy),
            operation = tostring(properties.changeAttributes.operation),
            clientType = tostring(properties.changeAttributes.clientType),
            changes = properties.changes
        | order by changedAt desc, id asc
    """
    result = await scoped_graph(query, scopes)
    if (not isinstance(result, dict) or not isinstance(result.get("rows"), list)
            or type(result.get("truncated")) is not bool):
        raise SourceError("Resource Graph returned an invalid change response.")
    changes = []
    for row in result["rows"]:
        if not isinstance(row, dict):
            raise SourceError("Resource Graph returned an invalid change row.")
        sub = row.get("subscriptionId")
        if not isinstance(sub, str) or sub.lower() not in scopes:
            raise SourceError("Resource Graph returned a change outside the requested scope.")
        resource_id, name, resource_type = _target(row.get("targetResourceId"), sub.lower())
        changed_at = _date(row.get("changedAt"))
        if not start <= changed_at <= end:
            raise SourceError("Resource Graph returned a change outside the requested time window.")
        change_id = row.get("id")
        if not isinstance(change_id, str) or not change_id or len(change_id) > 2048:
            raise SourceError("Resource Graph returned an invalid change ID.")
        properties, properties_truncated = _properties(row.get("changes"))
        changes.append({
            "id": change_id, "resource_id": resource_id, "resource_name": _text(name),
            "resource_type": _text(resource_type), "subscription_id": sub.lower(),
            "changed_at": changed_at.isoformat().replace("+00:00", "Z"),
            "change_type": _optional(row.get("changeType")) or "Unknown",
            "actor": _optional(row.get("changedBy")), "operation": _optional(row.get("operation")),
            "properties": properties, "properties_truncated": properties_truncated, "cost": None,
        })
    changes.sort(key=lambda item: (_date(item["changed_at"]), item["id"]), reverse=True)
    truncated = result["truncated"] or len(changes) > MAX_CHANGES
    total = result.get("total")
    if type(total) is int and total > len(result["rows"]):
        truncated = True
    changes = changes[:MAX_CHANGES]
    truncated |= any(item["properties_truncated"] for item in changes)
    if changes:
        resource_ids = sorted({item["resource_id"].lower() for item in changes})
        costs = await asyncio.to_thread(resource_costs, scopes, resource_ids,
                                        window["start"], window["end"])
        if not isinstance(costs, dict):
            raise SourceError("The cost warehouse returned an invalid resource-cost response.")
        for item in changes:
            item["cost"] = costs.get(item["resource_id"].lower())
    notices = list(NOTICES)
    if truncated:
        notices.append("Results are partial: change records or property details were limited "
                       "by source pagination, source truncation, or display caps.")
    return {
        "status": "partial" if truncated else "ok", "source": SOURCE,
        "generated_at": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
        "window": window, "notices": notices, "changes": changes, "truncated": truncated,
    }
