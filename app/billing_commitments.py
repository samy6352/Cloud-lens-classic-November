"""Read-only, bounded commitment lifecycle enrichment for explicitly scoped Billing.

REST contracts (2022-11-01, Microsoft Learn):
  reserved-vm-instances/reservation/{get,list-all}
  billingbenefits/savings-plan/get
  billingbenefits/savings-plan-order/list

Billing usage proves a relationship, not expiry, utilisation, or a refundable amount.
Provider responses are projected only after checking that relationship; billing-account
identifiers, order totals and unrelated/shared purchases never enter the response.
"""
from __future__ import annotations

import asyncio
import json
import math
import re
from datetime import UTC, datetime
from typing import Any
from urllib.parse import quote, urlsplit

import httpx

from . import cost
from .azure_updates import read_response
from .commitments import get_commitments

API_VERSION = "2022-11-01"
SOURCE_DOCS = [
    "https://learn.microsoft.com/en-us/rest/api/reserved-vm-instances/reservation/get"
    "?view=rest-reserved-vm-instances-2022-11-01",
    "https://learn.microsoft.com/en-us/rest/api/reserved-vm-instances/reservation/list-all"
    "?view=rest-reserved-vm-instances-2022-11-01",
    "https://learn.microsoft.com/en-us/rest/api/billingbenefits/savings-plan/get"
    "?view=rest-billingbenefits-2022-11-01",
    "https://learn.microsoft.com/en-us/rest/api/billingbenefits/savings-plan-order/list"
    "?view=rest-billingbenefits-2022-11-01",
]
MAX_METADATA = 24
MAX_ITEMS = 200
MAX_PAGES = 2
MAX_TOKENS = 4
MAX_REQUESTS = 48
REQUEST_TIMEOUT = 15
LIFECYCLE_TIMEOUT = 45
UNDERUTILIZED_PCT = 80
EXPIRING_DAYS = 90
RESERVATIONS = "/providers/Microsoft.Capacity/reservations"
SP_ORDERS = "/providers/Microsoft.BillingBenefits/savingsPlanOrders"
_BENEFIT = re.compile(
    r"^/providers/(?:Microsoft\.Capacity/reservationOrders/[a-z0-9-]+/"
    r"reservations/[a-z0-9-]+|Microsoft\.BillingBenefits/"
    r"savingsPlanOrders/[a-z0-9-]+/savingsPlans/[a-z0-9-]+)$", re.I)
_SUB = re.compile(r"^/subscriptions/([a-z0-9-]+)(?:/resourcegroups/[^/?#]+)?$", re.I)


def number(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        result = float(value)
        return result if math.isfinite(result) else None
    except (TypeError, ValueError):
        return None


def normalize_scopes(scopes: list[str]) -> list[str]:
    """The reused scans/warehouse accept subscriptions, not resource-group restrictions."""
    if scopes is None:
        raise ValueError("An explicit subscription list is required; the route must resolve unrestricted scope.")
    result = []
    for value in scopes:
        text = str(value).strip().rstrip("/").lower()
        text = re.sub(r"^/subscriptions/", "", text)
        if not re.fullmatch(r"[a-z0-9-]+", text):
            raise ValueError("Billing opportunities require explicit subscription IDs.")
        if text not in result:
            result.append(text)
    return result


def _id(value: Any) -> str:
    return str(value or "").rstrip("/").lower()


def _benefit(value: Any) -> str | None:
    value = str(value or "").rstrip("/")
    return value if _BENEFIT.fullmatch(value) else None


def _date(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else parsed.astimezone(UTC)
    except ValueError:
        return None


def _applied(properties: dict) -> list[str]:
    if str(properties.get("appliedScopeType") or "").lower() != "single":
        return []
    details = properties.get("appliedScopeProperties") or {}
    if not isinstance(details, dict):
        return []
    # A resource-group scope must not be widened to its parent subscription.
    direct = details.get("resourceGroupId") or details.get("subscriptionId")
    values = [direct] if direct else properties.get("appliedScopes") or []
    if not isinstance(values, list):
        return []
    return [_id(v) for v in values if isinstance(v, str) and _SUB.fullmatch(v)]


def _within(scope: str, scopes: list[str]) -> bool:
    return any(scope == _id(s) or scope.startswith(_id(s) + "/") for s in scopes)


def _utilization(properties: dict) -> tuple[float | None, dict | None]:
    utilization = properties.get("utilization") or {}
    aggregates = utilization.get("aggregates") if isinstance(utilization, dict) else []
    valid = []
    for row in aggregates if isinstance(aggregates, list) else []:
        if not isinstance(row, dict):
            continue
        value, grain = number(row.get("value")), number(row.get("grain"))
        if (value is not None and 0 <= value <= 100 and grain is not None
                and 0 < grain <= 30 and str(row.get("grainUnit")).lower() == "days"
                and str(row.get("valueUnit")).lower() == "percentage"):
            valid.append((grain, value))
    if not valid:
        return None, None
    grain, value = max(valid)
    return value, {"days": grain, "unit": "percentage",
                   "basis": "Provider rolling aggregate; end timestamp not supplied"}


def _observed(row: dict, index: int) -> dict:
    rid = _benefit(row.get("id"))
    return {
        "id": row.get("id") or f"observed:{index}",
        "name": row.get("name") or row.get("id") or "Observed commitment",
        "kind": row.get("kind") or "commitment",
        "utilization_pct": None, "utilization_period": None,
        "expires_on": None, "days_to_expiry": None, "state": "unknown",
        "underutilized": None, "expiring": None, "term": None,
        "monthly_unused_cost": None,
        "unused_cost_note": "Billing consumption is not an unused-cost allocation.",
        "currency": row.get("currency"),
        "observed_cost": number(row.get("cost")),
        "first_seen": row.get("first_seen"), "last_seen": row.get("last_seen"),
        "source": "warehouse", "sources": ["warehouse"],
        "scope_basis": "Observed benefit usage in the selected subscriptions",
        "portal_url": f"https://portal.azure.com/#resource{quote(rid, safe='/')}" if rid else None,
        "metadata_status": "unavailable", "metadata_as_of": None,
    }


def _enrich(raw: dict, scopes: list[str], existing: dict | None,
            now: datetime, source: str) -> dict | None:
    rid = _benefit(raw.get("id"))
    props = raw.get("properties")
    if not rid or not isinstance(props, dict):
        return None
    applied = _applied(props)
    relates = any(_within(s, scopes) for s in applied)
    if existing is None and not relates:
        return None
    item = dict(existing or _observed(
        {"id": rid, "name": props.get("displayName"), "kind":
         "reservation" if "microsoft.capacity" in rid.lower() else "savings plan"}, 0))
    if existing is None:
        item["sources"] = []
    item.update({
        "id": rid,
        "name": props.get("displayName") or item["name"],
        "kind": "reservation" if "microsoft.capacity" in rid.lower() else "savings plan",
        "source": source, "sources": list(dict.fromkeys([*item["sources"], source])),
        "metadata_status": "available", "metadata_as_of": now.isoformat(),
        "term": props.get("term"), "state": props.get("provisioningState") or "unknown",
        "scope_basis": "Explicit applied scope" if relates else item["scope_basis"],
        "applied_scope_type": props.get("appliedScopeType"),
        "applied_scopes": [s for s in applied if _within(s, scopes)],
        # Replace provider-derived fields together; absent or broader-scope data is unknown.
        "expires_on": None, "days_to_expiry": None, "expiring": None,
        "utilization_pct": None, "utilization_period": None, "underutilized": None,
    })
    item.pop("utilization_note", None)
    expiry = _date(props.get("expiryDateTime")) or _date(props.get("expiryDate"))
    if expiry:
        remaining = (expiry.date() - now.date()).days
        item.update(expires_on=expiry.isoformat(), days_to_expiry=remaining,
                    expiring=0 <= remaining <= EXPIRING_DAYS)
        if expiry < now:
            item.update(state="expired", expiring=False)
    # Shared benefit-wide utilisation can include other subscribers' usage. Retain the
    # observed benefit and its lifecycle, but never expose those cross-scope aggregates.
    exclusive = bool(applied) and all(_within(s, scopes) for s in applied)
    if exclusive:
        pct, period = _utilization(props)
        item.update(utilization_pct=pct, utilization_period=period,
                    underutilized=pct < UNDERUTILIZED_PCT if pct is not None else None)
    else:
        item["utilization_note"] = (
            "Benefit-wide utilisation withheld: its applied scope is not wholly selected.")
    return item


def _failure(exc: Exception) -> str:
    text = str(exc).lower()
    if "403" in text or "authorizationfailed" in text or "forbidden" in text:
        return ("Azure denied read access (403). Subscription Owner does not grant "
                "reservation or savings-plan billing-reader access; no permissions were changed.")
    if "401" in text or "invalidauthenticationtoken" in text:
        return "Azure refused the delegated tenant token (401); lifecycle metadata is unavailable."
    if isinstance(exc, (TimeoutError, httpx.TimeoutException)):
        return "The bounded Azure lifecycle read timed out."
    return "Azure lifecycle metadata could not be read; no permissions were changed."


async def _get_metadata(url: str, *, params: dict | None = None) -> dict:
    """Billing metadata has its own bounded transport, not the cost-query semaphore."""
    target = urlsplit(url)
    if (target.scheme != "https" or target.netloc.lower() != "management.azure.com"
            or not target.path.lower().startswith((
                "/providers/microsoft.capacity/", "/providers/microsoft.billingbenefits/"))):
        raise ValueError("Unsupported commitment metadata URL")
    token = await cost.azure._bearer(url)
    async with (
        httpx.AsyncClient(timeout=httpx.Timeout(REQUEST_TIMEOUT, connect=5), follow_redirects=False) as client,
        client.stream("GET", url, params=params, headers={"Authorization": f"Bearer {token}"}) as response,
    ):
        payload = await read_response(response, 4 * 1024 * 1024)
    result = json.loads(payload)
    if not isinstance(result, dict) or result.get("error"):
        raise ValueError("Invalid commitment metadata response")
    return result


def _warehouse_inventory(scopes: list[str], days: int, currency: str | None) -> dict:
    service = get_commitments()
    inventory = service.inventory(scopes, days=days, currency=currency)
    if not isinstance(inventory, dict):
        raise ValueError("Invalid warehouse response")
    inventory = dict(inventory)
    # savings' grouped orphan rows do not retain cost_by_currency. A small scoped
    # native-currency census lets Billing refuse those ambiguous totals without
    # re-running any resource or Advisor scan. Historical currencies can conservatively
    # exclude a group; they must never make unlike currencies additive.
    try:
        with service.wh.reader(scopes) as reader:
            rows = reader.rows(
                f'SELECT DISTINCT "BillingCurrency" AS currency FROM {reader.agg}', 20)
        inventory["native_billing_currencies"] = sorted({
            str(row.get("currency") or "unknown").upper() for row in rows})
    except Exception:
        inventory["native_billing_currencies"] = None
    return inventory


async def build(scopes: list[str], currency: str | None = None, *, days: int = 30) -> dict:
    """Warehouse inventory plus strictly read-only provider metadata; no global cache."""
    now = datetime.now(UTC)
    scopes = normalize_scopes(scopes)
    unavailable: list[dict] = []
    notes: list[str] = []
    items: dict[str, dict] = {}
    result = {
        "items": [], "unavailable": unavailable, "partial": False,
        "coverage": "bounded scoped inventory", "as_of": None,
        "native_billing_currencies": None,
        "metadata_checked_at": now.isoformat(),
        "api_version": API_VERSION, "source_docs": SOURCE_DOCS,
        "underutilized_threshold_pct": UNDERUTILIZED_PCT, "expiry_window_days": EXPIRING_DAYS,
        "note": ("Only observed benefits or explicitly applied scopes are shown. Unrelated and "
                 "unproven shared purchases are omitted, without exposing account-wide counts. "
                 "Missing expiry/utilisation is unknown, not zero. Lifecycle alerts are not "
                 "monetized savings and do not trigger purchases or changes."),
        "notices": notes,
    }
    if not scopes:
        result.update(coverage="no subscriptions selected", partial=True)
        unavailable.append({"source": "scope", "reason": "No explicit subscriptions selected."})
        return result
    arm_scopes = ["/subscriptions/" + s for s in scopes]
    try:
        inventory = await asyncio.wait_for(asyncio.to_thread(
            _warehouse_inventory, scopes, days, currency), 12)
        if not isinstance(inventory, dict):
            raise ValueError("Invalid warehouse response")
        result["as_of"] = inventory.get("as_of")
        result["native_billing_currencies"] = inventory.get("native_billing_currencies")
        rows = inventory.get("commitments") or []
        if not isinstance(rows, list):
            raise ValueError("Invalid warehouse inventory")
        for index, row in enumerate(rows[:MAX_ITEMS]):
            if isinstance(row, dict):
                item = _observed(row, index)
                items.setdefault(_id(item["id"]), item)
        if len(rows) >= MAX_ITEMS:
            notes.append("Warehouse inventory reached its 200-benefit cap; coverage may be partial.")
        if inventory.get("error") or inventory.get("unavailable"):
            raise ValueError("Partial warehouse inventory")
    except Exception:
        unavailable.append({"source": "warehouse",
                            "reason": "Observed commitment inventory is unavailable or incomplete."})

    tokens = list(dict.fromkeys(cost.caller_token(cost.ARM + s) for s in arm_scopes))
    if len(tokens) > MAX_TOKENS:
        notes.append("Delegated credential cap reached; some selected tenants were not inspected.")
    tokens = tokens[:MAX_TOKENS]
    semaphore = asyncio.Semaphore(4)
    requests = 0

    async def request(path: str, token: str | None, params: dict | None = None) -> dict:
        nonlocal requests
        async with semaphore:
            if requests >= MAX_REQUESTS:
                raise RuntimeError("request cap")
            requests += 1
            reset = cost.act_as(token, cost.acting_as()) if token else None
            try:
                value = await asyncio.wait_for(_get_metadata(
                    cost.ARM + path, params=params if params is not None
                    else {"api-version": API_VERSION}), REQUEST_TIMEOUT)
                if not isinstance(value, dict) or value.get("error"):
                    raise ValueError("Invalid provider response")
                return value
            finally:
                if reset:
                    cost.stop_acting(reset)

    def accept(raw: dict, source: str) -> bool:
        previous = items.get(_id(raw.get("id")))
        row = _enrich(raw, arm_scopes, previous, now, source)
        if row:
            if len(items) < MAX_ITEMS or _id(row["id"]) in items:
                items[_id(row["id"])] = row
            else:
                notes.append("Scoped commitment item cap reached; inventory is partial.")
        return row is not None

    async def exact(rid: str, selected_tokens: list[str | None], *, observed: bool) -> None:
        last: Exception = ValueError("metadata unavailable")
        for token in selected_tokens:
            try:
                raw = await request(rid, token)
                if _id(raw.get("id")) != _id(rid) or not isinstance(raw.get("properties"), dict):
                    raise ValueError("Unexpected resource metadata")
                accept(raw, "Microsoft.Capacity" if "capacity" in rid.lower()
                       else "Microsoft.BillingBenefits")
                return
            except Exception as exc:
                last = exc
        issue = {"source": "benefit metadata", "reason": _failure(last)}
        # Never disclose identifiers for failed, not-yet-attributed purchased benefits.
        if observed:
            issue["id"] = rid
        unavailable.append(issue)

    async def collection(path: str, token: str | None) -> list[dict]:
        found = []
        current = path
        for page in range(MAX_PAGES):
            params = {"api-version": API_VERSION}
            if path == RESERVATIONS:
                params["take"] = MAX_ITEMS
            raw = await request(current, token, params if page == 0 else {})
            rows = raw.get("value")
            if not isinstance(rows, list):
                raise ValueError("Unsupported inventory response")
            found.extend(r for r in rows[:MAX_ITEMS] if isinstance(r, dict))
            if len(rows) > MAX_ITEMS:
                notes.append("Provider page item cap reached; purchased inventory is partial.")
            # Process each page before the next call, so a 403/timeout on a later page
            # cannot erase successfully attributed benefits.
            if path == RESERVATIONS:
                for row in rows[:MAX_ITEMS]:
                    if isinstance(row, dict):
                        accept(row, "Microsoft.Capacity")
            else:
                ids = []
                for order in rows[:MAX_ITEMS]:
                    props = order.get("properties") if isinstance(order, dict) else None
                    children = props.get("savingsPlans") if isinstance(props, dict) else None
                    if not isinstance(children, list):
                        notes.append("Savings-plan order lacks supported child IDs; coverage is partial.")
                        continue
                    valid = [rid for v in children if (rid := _benefit(v))]
                    if len(valid) != len(children):
                        notes.append("Unsupported savings-plan child IDs omitted; coverage is partial.")
                    ids.extend(valid)
                ids = list(dict.fromkeys(ids))
                if len(ids) > MAX_METADATA:
                    notes.append("Purchased savings-plan metadata cap reached; inventory is partial.")
                await asyncio.gather(*(exact(rid, [token], observed=False)
                                       for rid in ids[:MAX_METADATA]
                                       if items.get(_id(rid), {}).get("metadata_status") != "available"))
            next_link = raw.get("nextLink")
            if not next_link:
                return found
            link = urlsplit(str(next_link))
            if (link.scheme != "https" or link.netloc.lower() != "management.azure.com"
                    or _id(link.path) != _id(path) or link.fragment):
                notes.append("Unsupported continuation link omitted; inventory is partial.")
                return found
            current = link.path + ("?" + link.query if link.query else "")
        notes.append("Provider pagination cap reached; purchased inventory is partial.")
        return found

    async def discover(path: str, token: str | None) -> None:
        try:
            await collection(path, token)
        except Exception as exc:
            unavailable.append({"source": path, "reason": _failure(exc)})

    try:
        async with asyncio.timeout(LIFECYCLE_TIMEOUT):
            known = [rid for row in items.values() if (rid := _benefit(row["id"]))]
            if len(known) > MAX_METADATA:
                notes.append("Observed-benefit metadata cap reached; some lifecycle fields are unknown.")
            await asyncio.gather(*(exact(rid, tokens, observed=True) for rid in known[:MAX_METADATA]))
            await asyncio.gather(*(discover(path, token)
                                   for token in tokens for path in (RESERVATIONS, SP_ORDERS)))
    except TimeoutError:
        unavailable.append({"source": "lifecycle", "reason": _failure(TimeoutError())})
    if requests >= MAX_REQUESTS:
        notes.append("Azure read-request cap reached; lifecycle coverage is partial.")
    for item in items.values():
        missing = [key for key in ("expires_on", "utilization_pct") if item[key] is None]
        if missing:
            unavailable.append({"source": item["source"], "id": item["id"], "fields": missing,
                                "reason": "Expiry/utilisation not supplied or not safely scope-attributable."})
    result.update(items=list(items.values()), partial=bool(unavailable or notes),
                  notices=list(dict.fromkeys(notes)), requests=requests)
    return result
