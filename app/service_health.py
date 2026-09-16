"""Subscription-scoped Service Health, separate from the public Azure Status feed."""
from __future__ import annotations

import hashlib
import json
import re
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import quote
from uuid import UUID

import httpx

from .azure_updates import SourceCache, SourceError, envelope, iso_date, plain_text, read_response
from .cost import ARM, azure

SOURCE = {"name": "Azure Service Health", "visibility": "private",
          "url": "https://portal.azure.com/#view/Microsoft_Azure_Health/AzureHealthBrowseBlade/~/serviceIssues"}
HEALTH_TTL = 120
MAX_SCOPES = 100
MAX_PAGES = 10
PAGE_SIZE = 250
MAX_EVENTS = 200
MAX_RESOURCES = 100
MAX_ARG_BYTES = 4 * 1024 * 1024
ARG_URL = f"{ARM}/providers/Microsoft.ResourceGraph/resources?api-version=2022-10-01"

# No EventType or Status predicate: ServiceIssue, PlannedMaintenance, advisories, billing
# and other retained event types must remain visible, including resolved events.
EVENTS_QUERY = """
    servicehealthresources
    | where type =~ 'microsoft.resourcehealth/events'
    | extend p = properties
    | project id, eventId = name, subscriptionId,
        tracking = tostring(p.TrackingId), headline = tostring(p.Title),
        summary = substring(tostring(p.Summary), 0, 8000),
        eventType = tostring(p.EventType), eventSubtype = tostring(p.EventSubType),
        level = tostring(p.Level), eventLevel = tostring(p.EventLevel),
        status = tostring(p.Status), startTicks = tostring(p.ImpactStartTime),
        dueTicks = tostring(p.ImpactMitigationTime), updatedTicks = tostring(p.LastUpdateTime),
        actions = substring(tostring(p.RecommendedActions), 0, 8000),
        impact = substring(tostring(p.Impact), 0, 16000),
        region = substring(tostring(p.Region), 0, 4000)
    | order by updatedTicks desc, id asc
"""

IMPACTED_QUERY = """
    servicehealthresources
    | where type =~ 'microsoft.resourcehealth/events/impactedresources'
    | extend p = properties
    | project id, subscriptionId,
        tracking = tostring(split(tostring(split(tolower(id), '/impactedresources/')[0]), '/events/')[1]),
        resourceId = tostring(p.targetResourceId),
        resourceType = tostring(p.targetResourceType),
        resourceName = tostring(p.resourceName),
        targetResourceName = tostring(p.targetResourceName),
        region = tostring(p.targetRegion)
    | order by id asc
"""

NOTICE = (
    "Private, subscription-scoped Azure Service Health from Azure Resource Graph. "
    "All retained event types and statuses are queried; resolved events remain explicitly labeled. "
    "Results depend on Azure RBAC and Resource Graph retention/indexing (updates can lag). "
    "Missing resource details do not mean a subscription is unaffected. "
    "Subscription-less emerging issues are not available through this table."
)
_cache = SourceCache(HEALTH_TTL, 32, timeout=55)


def normalize_scopes(values: list[str]) -> list[str]:
    if not values or len(values) > MAX_SCOPES or any(not value.strip() for value in values):
        raise SourceError(f"Select between 1 and {MAX_SCOPES} explicit subscriptions.")
    try:
        return sorted({str(UUID(value.strip())) for value in values})
    except (ValueError, AttributeError) as exc:
        raise SourceError("Subscription scope must contain comma-separated GUIDs.") from exc


def actor_key(user: Any, scopes: list[str]) -> str:
    # SID is essential: the same Entra identity in a new session must not inherit old RBAC data.
    identity = [str(getattr(user, field, "")) for field in ("source", "name", "email", "sid")]
    return hashlib.sha256(json.dumps([identity, scopes], separators=(",", ":")).encode()).hexdigest()


def scope_error(message: str) -> dict[str, Any]:
    return envelope(None, SOURCE, HEALTH_TTL, error=message)


def health_date(value: Any) -> str | None:
    text = str(value or "").strip()
    if re.fullmatch(r"\d{15,20}", text):
        try:
            dt = datetime(1, 1, 1, tzinfo=UTC) + timedelta(microseconds=int(text) // 10)
            return dt.isoformat().replace("+00:00", "Z")
        except (ValueError, OverflowError):
            return None
    return iso_date(text)


def _dimensions(impact: Any, region: Any) -> tuple[list[str], list[str]]:
    services: set[str] = set()
    regions: set[str] = set()

    def visit(value: Any, destination: set[str] | None = None) -> None:
        if isinstance(value, str):
            if value.lstrip().startswith(("[", "{")):
                try:
                    visit(json.loads(value), destination)
                except (ValueError, RecursionError):
                    pass
            elif destination is not None and value:
                destination.add(plain_text(value, 160))
        elif isinstance(value, list):
            for item in value[:100]:
                visit(item, destination)
        elif isinstance(value, dict):
            for key, item in value.items():
                if key.lower() in {"servicename", "impactedservice", "impactedservices", "service", "services"}:
                    visit(item, services)
                elif key.lower() in {"regionname", "region", "regions", "impactedregions"}:
                    visit(item, regions)
                elif isinstance(item, (dict, list)):
                    visit(item)

    visit(impact)
    visit(region, regions)
    return sorted(services - {""})[:100], sorted(regions - {""})[:100]


async def _groups(scopes: list[str]) -> list[tuple[str, list[str]]]:
    groups: dict[str, list[str]] = {}
    for sub in scopes:
        # ARG's root URL cannot select a cross-tenant delegated token by itself.
        # Choose the token with a subscription URL, then query only its subscriptions.
        token = await azure._bearer(f"{ARM}/subscriptions/{sub}")
        groups.setdefault(token, []).append(sub)
    return list(groups.items())


async def _arg_page(query: str, scopes: list[str], token: str,
                    options: dict[str, Any]) -> dict[str, Any]:
    if not scopes:
        raise SourceError("Service Health requires an explicit subscription scope.")
    async with (
        httpx.AsyncClient(timeout=httpx.Timeout(18, connect=5), follow_redirects=False) as client,
        client.stream(
            "POST", ARG_URL,
            headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
            json={"query": query, "subscriptions": scopes, "options": options},
        ) as response,
    ):
        data = await read_response(response, MAX_ARG_BYTES)
    try:
        payload = json.loads(data)
    except (ValueError, UnicodeError) as exc:
        raise SourceError("Resource Graph returned invalid JSON.") from exc
    if not isinstance(payload, dict):
        raise SourceError("Resource Graph returned an unexpected response.")
    return payload


async def _query(query: str, groups: list[tuple[str, list[str]]]) -> dict[str, Any]:
    rows: list[dict[str, Any]] = []
    pages = 0
    truncated = False
    total = 0
    total_known = True
    for group_index, (token, scopes) in enumerate(groups):
        skip = 0
        continuation: str | None = None
        tokens: set[str] = set()
        while pages < MAX_PAGES:
            options: dict[str, Any] = {"resultFormat": "objectArray", "$top": PAGE_SIZE}
            if continuation:
                options["$skipToken"] = continuation
            elif skip:
                options["$skip"] = skip
            payload = await _arg_page(query, scopes, token, options)
            pages += 1
            batch = payload.get("data")
            if not isinstance(batch, list) or any(not isinstance(row, dict) for row in batch):
                raise SourceError("Resource Graph returned an unexpected row format.")
            if len(batch) > PAGE_SIZE:
                raise SourceError("Resource Graph exceeded the requested page size.")
            if skip == 0:
                reported_total = payload.get("totalRecords")
                if isinstance(reported_total, int) and reported_total >= 0:
                    total += reported_total
                else:
                    total_known = False
            # Defense in depth: never trust a returned row to widen the requested scope.
            permitted = set(scopes)
            if any(str(row.get("subscriptionId", "")).lower() not in permitted for row in batch):
                raise SourceError("Resource Graph returned a row outside the requested scope.")
            rows.extend(batch)
            skip += len(batch)
            continuation = payload.get("$skipToken") or None
            more = bool(continuation) or str(payload.get("resultTruncated", "")).lower() == "true"
            reported_total = payload.get("totalRecords")
            if isinstance(reported_total, int):
                more = more or skip < reported_total
            if not more:
                break
            if not batch:
                truncated = True
                break
            if continuation:
                if not isinstance(continuation, str) or continuation in tokens:
                    raise SourceError("Resource Graph returned a repeated or invalid continuation token.")
                tokens.add(continuation)
            if pages >= MAX_PAGES:
                truncated = True
        if pages >= MAX_PAGES and group_index < len(groups) - 1:
            truncated = True
            total_known = False
            break
    return {"rows": rows, "pages": pages, "truncated": truncated,
            "total": total if total_known else None}


def normalize_events(events: list[dict], impacted: list[dict],
                     scopes: list[str], resources_incomplete: bool = False) -> list[dict]:
    allowed = set(scopes)
    resources: dict[tuple[str, str], dict[str, dict]] = {}
    resource_regions: dict[tuple[str, str], set[str]] = {}
    for row in impacted:
        sub = str(row.get("subscriptionId") or "").lower()
        tracking = str(row.get("tracking") or "").lower()
        rid = str(row.get("resourceId") or "")
        resource_sub = re.match(r"^/subscriptions/([^/]+)/", rid, re.I)
        if sub not in allowed or not tracking or not resource_sub or resource_sub[1].lower() != sub:
            continue
        key = (sub, tracking)
        resources.setdefault(key, {})[rid.lower()] = {
            "id": plain_text(rid, 2048),
            "name": plain_text(row.get("resourceName") or row.get("targetResourceName")
                               or rid.rsplit("/", 1)[-1], 300),
            "type": plain_text(row.get("resourceType"), 300),
        }
        if row.get("region"):
            resource_regions.setdefault(key, set()).add(plain_text(row["region"], 160))
    seen: dict[tuple[str, str], dict] = {}
    for row in events:
        sub = str(row.get("subscriptionId") or "").lower()
        tracking = plain_text(row.get("tracking") or row.get("eventId"), 512)
        if sub not in allowed:
            continue
        if not tracking or not row.get("headline"):
            raise SourceError("A Service Health event is missing its identifier or title.")
        key = (sub, tracking.lower())
        aliases = {tracking.lower(), str(row.get("eventId") or "").lower()} - {""}
        matched: dict[str, dict] = {}
        services, regions = _dimensions(row.get("impact"), row.get("region"))
        for alias in aliases:
            matched.update(resources.get((sub, alias), {}))
            regions.extend(resource_regions.get((sub, alias), set()))
        status = plain_text(row.get("status"), 100) or "Unknown"
        event_type = plain_text(row.get("eventType"), 100) or "Unknown"
        updated = health_date(row.get("updatedTicks"))
        item = {
            "id": sub + ":" + tracking, "tracking_id": tracking,
            "title": plain_text(row.get("headline"), 500), "summary": plain_text(row.get("summary")),
            "url": "https://portal.azure.com/#view/Microsoft_Azure_Health/EventDetailsBlade/eventTrackingId/"
                   + quote(tracking, safe="") + "/subscriptionId/" + sub,
            "published_at": health_date(row.get("startTicks")), "updated_at": updated,
            "impact_end_at": health_date(row.get("dueTicks")),
            "category": event_type, "event_type": event_type,
            "event_subtype": plain_text(row.get("eventSubtype"), 100),
            "services": services, "regions": sorted(set(regions)),
            "status": status, "level": plain_text(row.get("level") or row.get("eventLevel"), 100)
            or "Not specified",
            "customer_impact": "Azure reported this event for the selected subscription; "
                               "review its summary and resource details for the reported impact.",
            "impact_basis": "subscription_service_health",
            "actions": plain_text(row.get("actions")), "subscription_ids": [sub],
            "resources": sorted(matched.values(), key=lambda r: r["id"])[:MAX_RESOURCES],
            "resource_count": len(matched),
            "resource_count_kind": "observed",
            "resources_truncated": resources_incomplete or len(matched) > MAX_RESOURCES,
        }
        prior = seen.get(key)
        if prior is None or (updated or "") > (prior["updated_at"] or ""):
            seen[key] = item
    return sorted(seen.values(),
                  key=lambda item: (item["updated_at"] or item["published_at"] or "", item["id"]),
                  reverse=True)


async def _load(scopes: list[str]) -> dict[str, Any]:
    groups = await _groups(scopes)
    events = await _query(EVENTS_QUERY, groups)
    impacted = await _query(IMPACTED_QUERY, groups) if events["rows"] else {
        "rows": [], "pages": 0, "truncated": False, "total": 0,
    }
    items = normalize_events(events["rows"], impacted["rows"], scopes, impacted["truncated"])
    truncated = events["truncated"] or impacted["truncated"] or len(items) > MAX_EVENTS
    notice = NOTICE
    if not items:
        notice = "No Service Health events returned for the selected subscriptions. " + notice
    if truncated or any(item["resources_truncated"] for item in items[:MAX_EVENTS]):
        truncated = True
        notice += " Results or resource details are capped; observed counts may be lower bounds."
    result = {
        "items": items[:MAX_EVENTS], "truncated": truncated, "notice": notice,
        "coverage": {
            "kind": "subscription_resource_graph", "subscription_ids": scopes,
            "event_pages": events["pages"], "resource_pages": impacted["pages"],
            "page_limit_per_query": MAX_PAGES, "page_size": PAGE_SIZE,
            "event_rows": len(events["rows"]), "event_rows_total": events["total"],
            "resource_rows": len(impacted["rows"]), "resource_rows_total": impacted["total"],
            "events_truncated": events["truncated"], "resources_truncated": impacted["truncated"],
            "item_limit": MAX_EVENTS, "resources_per_item_limit": MAX_RESOURCES,
        },
    }
    if not events["truncated"]:
        result["total"] = len(items)
    return result


async def service_health(user: Any, subscription_ids: list[str],
                         refresh: bool = False) -> dict[str, Any]:
    """Call only after authorization, inside as_user(user); empty scope is never expanded."""
    try:
        scopes = normalize_scopes(subscription_ids)
    except SourceError as exc:
        return scope_error(str(exc))
    return await _cache.get(actor_key(user, scopes), SOURCE, lambda: _load(scopes), refresh)
