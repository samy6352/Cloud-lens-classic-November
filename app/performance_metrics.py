"""Read-only, bounded Azure Monitor observations alongside native-currency spend.

Profiles follow Azure Monitor's supported-metrics reference for each resource type:
https://learn.microsoft.com/azure/azure-monitor/reference/supported-metrics/metrics-index
Definitions are checked live; catalog membership alone does not establish availability.
"""
from __future__ import annotations

import asyncio
import math
import re
from datetime import UTC, datetime
from urllib.parse import quote

import httpx

from .cost import ARM, CostError, azure
from .observability import evidence_window, resource_costs, scoped_graph

MAX_RESOURCES = 24
MAX_CONCURRENT = 4
RESOURCE_TIMEOUT = 12
INTERVAL = "PT1H"
SOURCE = "Azure Resource Graph + Azure Monitor + resource cost evidence"
_UUID = re.compile(r"[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}")

# API name, display label, required unit, required aggregation. Never substitute
# Average for Total: summing hourly averages is not a request or byte count.
PROFILES = {
    "microsoft.compute/virtualmachines": (
        ("Percentage CPU", "CPU", "Percent", "Average"),
        ("Network In Total", "Network received", "Bytes", "Total"),
        ("Network Out Total", "Network sent", "Bytes", "Total"),
    ),
    "microsoft.web/sites": (
        ("Requests", "Requests", "Count", "Total"),
        ("HttpResponseTime", "Response time", "Seconds", "Average"),
        ("Http5xx", "HTTP server errors (5xx)", "Count", "Total"),
    ),
    "microsoft.web/serverfarms": (
        ("CpuPercentage", "CPU", "Percent", "Average"),
        ("MemoryPercentage", "Memory", "Percent", "Average"),
    ),
    "microsoft.sql/servers/databases": (
        ("cpu_percent", "CPU", "Percent", "Average"),
        ("storage_percent", "Storage used", "Percent", "Average"),
    ),
    "microsoft.storage/storageaccounts": (
        ("Transactions", "Transactions", "Count", "Total"),
        ("SuccessE2ELatency", "Successful end-to-end latency", "MilliSeconds", "Average"),
        ("SuccessServerLatency", "Successful server latency", "MilliSeconds", "Average"),
        ("Availability", "Availability", "Percent", "Average"),
    ),
}


def _scope(scope: list[str]) -> list[str]:
    if not isinstance(scope, list) or not scope or len(scope) > 500:
        raise ValueError("Explicit subscription UUIDs are required.")
    if any(not isinstance(s, str) or not _UUID.fullmatch(s) for s in scope):
        raise ValueError("Explicit subscription UUIDs are required.")
    return sorted({s.lower() for s in scope})


def _resource(row: dict, scope: list[str]) -> dict | None:
    rid = row.get("id")
    if not isinstance(rid, str) or not rid.startswith("/"):
        return None
    parts = rid.split("/")[1:]
    if any(not p or p in {".", ".."} or re.search(r"[%?#\\\x00-\x1f\x7f]", p)
           for p in parts):
        return None
    if len(parts) not in (8, 10):
        return None
    if (parts[0].lower() != "subscriptions" or parts[2].lower() != "resourcegroups"
            or parts[4].lower() != "providers" or parts[1].lower() not in scope):
        return None
    kind = "/".join([parts[5], *parts[6::2]]).lower()
    if kind not in PROFILES or str(row.get("type", "")).lower() != kind:
        return None
    if str(row.get("subscriptionId", "")).lower() != parts[1].lower():
        return None
    parts[0], parts[1] = "subscriptions", parts[1].lower()
    return {
        "id": "/" + "/".join(parts), "name": str(row.get("name") or parts[-1]),
        "type": kind, "subscription_id": parts[1], "region": str(row.get("location") or ""),
        "status": "unavailable", "metrics": [], "cost": None, "notes": [],
    }


def _empty(spec: tuple, days: int) -> dict:
    name, label, unit, aggregation = spec
    return {
        "name": name, "label": label, "unit": unit, "aggregation": aggregation,
        "value": None, "samples": 0, "last_sample": None, "status": "unavailable",
        "note": f"Requested {INTERVAL}; expected {days * 24} hourly observations.",
    }


def _missing(metric: dict, status: str, note: str) -> None:
    metric.update(status=status, value=None, samples=0, last_sample=None)
    metric["note"] += " " + note


def _timestamp(raw: object) -> datetime | None:
    if not isinstance(raw, str):
        return None
    try:
        value = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    return value.astimezone(UTC) if value.tzinfo else None


def _read_metric(metric: dict, payload: dict | None, window: dict) -> None:
    if payload is None:
        _missing(metric, "no_data", "Azure Monitor returned no matching metric.")
        return
    if payload.get("errorCode") not in (None, "", "Success", "0"):
        _missing(metric, "unavailable", "Azure Monitor returned a metric-level error.")
        return
    if str(payload.get("unit", "")).lower() != metric["unit"].lower():
        _missing(metric, "unavailable", "Returned unit differs from the documented profile.")
        return
    series = payload.get("timeseries") or []
    if not series:
        _missing(metric, "no_data", "No time series was returned; this is not measured zero.")
        return
    # No dimension filter is requested: Azure should return the resource roll-up.
    # Refuse a split response, even a single named dimension; it is not the whole resource.
    if len(series) != 1 or series[0].get("metadatavalues"):
        _missing(metric, "unavailable", "Dimension-split series cannot safely represent a resource roll-up.")
        return
    start, end = _timestamp(window["start"]), _timestamp(window["end"])
    points: dict[datetime, float] = {}
    invalid = 0
    for point in series[0].get("data") or []:
        value = point.get(metric["aggregation"].lower())
        if value is None:
            continue
        stamp = _timestamp(point.get("timeStamp"))
        if (isinstance(value, bool) or not isinstance(value, (int, float))
                or not math.isfinite(value) or stamp is None or not start <= stamp < end):
            invalid += 1
            continue
        if stamp in points:
            _missing(metric, "unavailable", "Duplicate timestamps prevent an unambiguous hourly aggregation.")
            return
        points[stamp] = value
    if not points:
        _missing(metric, "no_data", "No valid numeric observations; nulls are not zero."
                 + (f" Ignored {invalid} invalid observations." if invalid else ""))
        return
    value = math.fsum(points.values())
    if metric["aggregation"] == "Average":
        value /= len(points)
        method = "Unweighted average of returned hourly observations; not request-weighted or a percentile."
    else:
        method = "Sum of returned hourly totals only; not extrapolated across missing hours."
    metric.update(value=value, samples=len(points), last_sample=max(points).isoformat().replace("+00:00", "Z"),
                  status="ok")
    metric["note"] += f" Observed {len(points)}/{window['days'] * 24}. {method}"
    if len(points) < window["days"] * 24:
        metric["note"] += " Incomplete coverage; missing hours are not treated as zero."
    if invalid:
        metric["note"] += f" Ignored {invalid} invalid observations."


async def _request(rid: str, endpoint: str, params: dict) -> dict:
    # The explicit subscription in this URL selects the caller's correct tenant token.
    payload = await azure.request(
        "GET", f"{ARM}{quote(rid, safe='/')}/providers/microsoft.insights/{endpoint}",
        params=params, attempts=1, cache=False,
    )
    if payload.get("error"):
        raise CostError("Azure Monitor returned an error response.")
    if not isinstance(payload.get("value"), list):
        raise CostError("Azure Monitor returned an invalid response.")
    return payload


async def _probe(resource: dict, window: dict, semaphore: asyncio.Semaphore) -> dict:
    metrics = [_empty(spec, window["days"]) for spec in PROFILES[resource["type"]]]
    resource["metrics"] = metrics
    async with semaphore:
        try:
            async with asyncio.timeout(RESOURCE_TIMEOUT):
                definitions = await _request(resource["id"], "metricDefinitions",
                                             {"api-version": "2018-01-01"})
                by_name = {d.get("name", {}).get("value", "").lower(): d for d in definitions["value"]}
                groups: dict[str, list[dict]] = {}
                for metric in metrics:
                    definition = by_name.get(metric["name"].lower())
                    if definition is None:
                        _missing(metric, "unsupported", "Metric is absent from this resource's definitions.")
                        continue
                    aggregations = {a.lower() for a in definition.get("supportedAggregationTypes") or []}
                    if metric["aggregation"].lower() not in aggregations:
                        _missing(metric, "unsupported", "Required aggregation is not supported; no substitute used.")
                        continue
                    if str(definition.get("unit", "")).lower() != metric["unit"].lower():
                        _missing(metric, "unsupported", "Definition unit differs from the documented profile.")
                        continue
                    groups.setdefault(metric["aggregation"], []).append(metric)
                for aggregation, group in groups.items():
                    try:
                        response = await _request(resource["id"], "metrics", {
                            "api-version": "2023-10-01", "metricnamespace": resource["type"],
                            "metricnames": ",".join(m["name"] for m in group),
                            "aggregation": aggregation, "interval": INTERVAL,
                            "timespan": f"{window['start']}/{window['end']}",
                            "AutoAdjustTimegrain": "false", "ValidateDimensions": "true",
                        })
                        if response.get("interval", INTERVAL) != INTERVAL:
                            raise CostError("Azure Monitor did not return the requested hourly interval.")
                        returned = {}
                        for item in response["value"]:
                            key = item.get("name", {}).get("value", "").lower()
                            if key in returned:
                                raise CostError("Azure Monitor returned duplicate metric entries.")
                            returned[key] = item
                        for metric in group:
                            _read_metric(metric, returned.get(metric["name"].lower()), window)
                    except (CostError, httpx.HTTPError):
                        for metric in group:
                            _missing(metric, "unavailable", "Metric read denied or failed; no utilization inference.")
        except (CostError, httpx.HTTPError):
            for metric in metrics:
                if metric["status"] == "unavailable":
                    _missing(metric, "unavailable", "Metric definitions read denied or failed.")
        except TimeoutError:
            for metric in metrics:
                if metric["status"] == "unavailable":
                    _missing(metric, "unavailable", "Resource observation time budget exceeded.")
    if resource["type"] == "microsoft.compute/virtualmachines":
        memory = _empty(("guest_memory", "Guest memory", "Percent", "Average"), window["days"])
        _missing(memory, "unsupported", "Guest memory requires additional guest telemetry; not collected by this profile.")
        metrics.append(memory)
    available = sum(m["status"] == "ok" for m in metrics)
    resource["status"] = "ok" if available == len(metrics) else "partial" if available else "unavailable"
    if any(m["status"] == "ok" and m["samples"] < window["days"] * 24 for m in metrics):
        resource["status"] = "partial"
    resource["notes"].append("Observations support manual review, not automatic resizing or guaranteed savings.")
    return resource


async def report(scope: list[str], days: int = 7) -> dict:
    """Collect a deterministic bounded inventory sample, not an estate-wide ranking."""
    scope = _scope(scope)
    window = evidence_window(days)
    response = {
        "status": "ok", "source": SOURCE,
        "generated_at": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
        "window": window, "notices": [], "truncated": False,
        "resources": [], "inventory_count": None, "sampled_count": 0,
    }
    types = ", ".join(f"'{kind}'" for kind in PROFILES)
    query = (
        f"Resources | where type in~ ({types})"
        " | project id, name, type, subscriptionId, location"
        f" | order by id asc | take {MAX_RESOURCES + 1}"
    )
    inventory = await scoped_graph(query, scope)
    candidates: dict[str, dict] = {}
    rejected = 0
    for row in inventory["rows"]:
        resource = _resource(row, scope)
        if resource is None:
            rejected += 1
        else:
            candidates.setdefault(resource["id"].lower(), resource)
    truncated = bool(inventory.get("truncated") or len(inventory["rows"]) > MAX_RESOURCES
                     or (inventory.get("total") or 0) > MAX_RESOURCES)
    resources = [candidates[key] for key in sorted(candidates)[:MAX_RESOURCES]]
    response.update(truncated=truncated, sampled_count=len(resources),
                    inventory_count=None if truncated or rejected else len(candidates))
    response["notices"].append(
        f"Deterministic ARM-ID sample of at most {MAX_RESOURCES} supported resources; "
        "not top spend or an all-estate total. No currencies are combined."
    )
    if truncated:
        response["notices"].append("Inventory is truncated; resources outside this sample were not assessed.")
    if rejected:
        response["notices"].append(f"Excluded {rejected} malformed, unsupported, or out-of-scope inventory rows.")
    if not resources:
        response["status"] = "unavailable" if truncated or rejected else "ok"
        response["notices"].append("No supported resources were available in the validated inventory.")
        return response
    # Helper errors deliberately propagate: a broken inventory/cost query is not evidence
    # that a resource is absent or free.
    costs = await asyncio.to_thread(
        resource_costs, scope, [r["id"] for r in resources], window["start"], window["end"])
    for resource in resources:
        resource["cost"] = costs.get(resource["id"].lower())
        if resource["cost"] is None:
            resource["notes"].append("No matching cost evidence; missing cost is not zero.")
    semaphore = asyncio.Semaphore(MAX_CONCURRENT)
    response["resources"] = await asyncio.gather(*(_probe(r, window, semaphore) for r in resources))
    missing_cost = sum(r["cost"] is None for r in resources)
    failed = sum(any(m["status"] == "unavailable" for m in r["metrics"]) for r in resources)
    no_metrics = sum(r["status"] == "unavailable" for r in resources)
    if failed:
        response["notices"].append(f"{failed} resources had denied, failed, ambiguous, or timed-out metric reads.")
    if no_metrics:
        response["notices"].append(f"{no_metrics} resources had no usable measurements; do not infer idleness.")
    if missing_cost:
        response["notices"].append(f"{missing_cost} resources have no matching cost evidence.")
    if no_metrics == len(resources):
        response["status"] = "unavailable"
    elif truncated or rejected or missing_cost or any(r["status"] != "ok" for r in resources):
        response["status"] = "partial"
    return response
