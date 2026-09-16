"""Read-only permission declarations, not trial writes or proof of end-to-end access.

The endpoint must authorize the selected subscriptions and enter ``as_user`` before calling
``report``. ``identity`` describes that Azure caller, not merely the signed-in application
user. Optional storage principals and successful product-inventory evidence are trusted
server metadata; they must not be copied from unauthenticated request parameters.
"""
from __future__ import annotations

import asyncio
import re
import time
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import quote, urlsplit

import httpx

from .cost import ARM, CostError, azure
from .job_policy import read_only_mode

PERMISSIONS_API = "2022-04-01"
MAX_SUBSCRIPTIONS = 50
MAX_STORAGE_CHECKS = 24
MAX_CONCURRENT = 4
MAX_PAGES = 3
MAX_PERMISSION_ENTRIES = 1000
SCOPE_TIMEOUT = 6.0
REPORT_TIMEOUT = 15.0
_UUID = re.compile(r"[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}", re.I)
_STORAGE = re.compile(
    r"/subscriptions/([^/]+)/resourceGroups/[^/]+/providers/Microsoft\.Storage/"
    r"storageAccounts/[^/]+/blobServices/default/containers/[^/]+", re.I)

OPERATIONS = (
    ("cost-query", "Microsoft.CostManagement/query/read", "Cost Management Reader", False),
    ("exports-read", "Microsoft.CostManagement/exports/read", "Cost Management Reader", False),
    ("exports-configure", "Microsoft.CostManagement/exports/write", "Cost Management Contributor", True),
    ("exports-run", "Microsoft.CostManagement/exports/run/action", "Cost Management Contributor", True),
    ("budgets-read", "Microsoft.Consumption/budgets/read", "Cost Management Reader", False),
    ("budgets-write", "Microsoft.Consumption/budgets/write", "Cost Management Contributor", True),
)
STORAGE_OPERATIONS = {
    "focus-read": ("Microsoft.Storage/storageAccounts/blobServices/containers/blobs/read",
                   "Storage Blob Data Reader"),
    "focus-write": ("Microsoft.Storage/storageAccounts/blobServices/containers/blobs/write",
                    "Storage Blob Data Contributor"),
    "archive-write": ("Microsoft.Storage/storageAccounts/blobServices/containers/blobs/write",
                      "Storage Blob Data Contributor"),
}


def action_matches(pattern: str, operation: str) -> bool:
    """ARM action names are case-insensitive; only '*' is a permission wildcard."""
    return bool(re.fullmatch(re.escape(pattern.strip().lower()).replace(r"\*", ".*"),
                             operation.lower()))


def evaluate_permission(entries: list[dict], operation: str, *, data_plane: bool = False,
                        complete: bool = True) -> dict[str, Any]:
    """Union grants after subtracting exclusions within each individual permission entry."""
    allow_key, exclude_key = ("dataActions", "notDataActions") if data_plane else ("actions", "notActions")
    conditional = False
    for entry in entries:
        allowed = any(action_matches(pattern, operation) for pattern in (entry.get(allow_key) or []))
        excluded = any(action_matches(pattern, operation) for pattern in (entry.get(exclude_key) or []))
        if not allowed or excluded:
            continue
        if entry.get("condition") or entry.get("conditions"):
            conditional = True
            continue
        return {"status": "declared-allowed", "conditional": False,
                "reason": "A returned permission entry grants this exact operation after its exclusions."}
    if conditional:
        return {"status": "unknown", "conditional": True,
                "reason": "The matching grant is conditional; its condition was not evaluated."}
    if not complete:
        return {"status": "unknown", "conditional": False,
                "reason": "Permission discovery is incomplete; absence of a grant is not evidence of denial."}
    return {"status": "denied", "conditional": False,
            "reason": "No matching grant remains in the complete permissions collection after exclusions."}


def _principal(value: dict | None) -> dict[str, Any]:
    value = value or {}
    return {key: value.get(key) for key in ("id", "kind", "tenant_id")}


def _caller(identity: dict | None, subscription: str | None = None) -> dict[str, Any]:
    identity = identity or {}
    overrides = identity.get("by_subscription") or {}
    match = next((value for key, value in overrides.items()
                  if subscription and key.lower() == subscription), {})
    return _principal({**identity, **match})


def _same_principal(first: dict, second: dict) -> bool:
    return bool(first.get("id") and first.get("tenant_id")
                and str(first["id"]).lower() == str(second.get("id") or "").lower()
                and str(first["tenant_id"]).lower() == str(second.get("tenant_id") or "").lower())


def _subscriptions(values: list[str]) -> list[str]:
    if not isinstance(values, list) or not values or len(values) > MAX_SUBSCRIPTIONS:
        raise ValueError(f"Select between 1 and {MAX_SUBSCRIPTIONS} explicit subscription UUIDs.")
    if any(not isinstance(value, str) or not _UUID.fullmatch(value) for value in values):
        raise ValueError("Every subscription must be an explicit UUID.")
    return sorted({value.lower() for value in values})


def _container_scope(value: str, subscriptions: list[str]) -> str:
    if not isinstance(value, str):
        raise ValueError("Storage diagnostics require an ARM blob-container scope.")
    scope = value.rstrip("/")
    if (re.search(r"[%?#\\\x00-\x1f\x7f]", scope)
            or any(part in {"", ".", ".."} for part in scope.split("/")[1:])):
        raise ValueError("Storage diagnostics require an ARM blob-container scope.")
    match = _STORAGE.fullmatch(scope)
    if not match or match[1].lower() not in subscriptions:
        raise ValueError("Storage container scope is outside the selected subscriptions or is invalid.")
    return scope.lower()


class _DiscoveryError(Exception):
    def __init__(self, code: str, reason: str, status: int | None = None):
        self.code, self.reason, self.status = code, reason, status
        super().__init__(reason)


def _source_error(exc: Exception) -> dict[str, Any]:
    if isinstance(exc, _DiscoveryError):
        return {"code": exc.code, "reason": exc.reason, "http_status": exc.status}
    status = exc.response.status_code if isinstance(exc, httpx.HTTPStatusError) else None
    text = str(exc).lower()
    if any(code in text for code in (
            "noregisteredproviderfound", "missingsubscriptionregistration",
            "invalidresourcetype", "invalidapiversionparameter", "notsupported",
            "unsupportedresourcetype", "resourceprovidernotregistered")):
        code, reason = ("provider-unsupported",
                        "The provider or permissions API is unavailable on this scope; inspect provider support separately.")
    elif status == 403 or "authorizationfailed" in text or "azure refused this on that scope" in text:
        status = status or 403
        code, reason = ("permission-discovery-denied",
                        "Reading effective permissions was denied. Operational access remains unknown; do not infer missing runtime roles.")
    elif status == 401 or "(401)" in text:
        status = status or 401
        code, reason = ("credential-rejected",
                        "Permission discovery could not authenticate the caller; check its tenant and session.")
    elif status == 429 or re.search(r"\b429\b", text):
        status = status or 429
        code, reason = "throttled", "Permission discovery was throttled; retry this read later."
    elif isinstance(exc, (TimeoutError, httpx.TimeoutException)):
        code, reason = "timeout", "The read-only permission check exceeded its time budget."
    else:
        code, reason = ("permission-discovery-failed",
                        "The permissions collection could not be read; no operational access conclusion was made.")
    return {"code": code, "reason": reason, "http_status": status}


def _entries(payload: dict) -> list[dict]:
    values = payload.get("value")
    if not isinstance(values, list):
        raise _DiscoveryError("invalid-response", "Azure did not return a permissions collection.")
    normalized = []
    for entry in values:
        if not isinstance(entry, dict) or not any(key in entry for key in ("actions", "dataActions")):
            raise _DiscoveryError("invalid-response", "Azure returned an invalid permission entry.")
        item = dict(entry)
        for key in ("actions", "notActions", "dataActions", "notDataActions"):
            patterns = entry.get(key)
            if patterns is None:
                patterns = []
            if not isinstance(patterns, list) or any(not isinstance(pattern, str) for pattern in patterns):
                raise _DiscoveryError("invalid-response", "Azure returned invalid permission action patterns.")
            item[key] = patterns
        normalized.append(item)
    return normalized


def _next_link(value: str, original: str) -> str:
    if not isinstance(value, str):
        raise _DiscoveryError("invalid-pagination", "Azure returned an invalid permission continuation.")
    try:
        link, source = urlsplit(value), urlsplit(original)
    except ValueError as exc:
        raise _DiscoveryError("invalid-pagination", "Azure returned an invalid permission continuation.") from exc
    if (link.scheme != "https" or link.netloc.lower() != source.netloc.lower()
            or link.path.rstrip("/").lower() != source.path.rstrip("/").lower()
            or link.fragment):
        raise _DiscoveryError("invalid-pagination", "Permission continuation left the authorized ARM scope.")
    return value


async def _observe(scope: str, semaphore: asyncio.Semaphore, force: bool) -> dict[str, Any]:
    observation: dict[str, Any] = {
        "scope": scope, "complete": False, "entries": [], "pages": 0, "error": None,
        "api_version": PERMISSIONS_API, "provider_supported": None,
    }
    started = time.monotonic()
    async with semaphore:
        try:
            async with asyncio.timeout(SCOPE_TIMEOUT):
                original = f"{ARM}{quote(scope, safe='/')}/providers/Microsoft.Authorization/permissions"
                url, seen = original, set()
                for page in range(MAX_PAGES):
                    payload = await azure.request(
                        "GET", url, params={"api-version": PERMISSIONS_API} if page == 0 else None,
                        attempts=1, cache=not force)
                    observation["pages"] += 1
                    if not isinstance(payload, dict):
                        raise _DiscoveryError("invalid-response", "Azure did not return a permissions object.")
                    if payload.get("error"):
                        error = payload["error"]
                        code = error.get("code", "") if isinstance(error, dict) else ""
                        raise CostError(str(code))
                    entries = _entries(payload)
                    available = MAX_PERMISSION_ENTRIES - len(observation["entries"])
                    observation["entries"].extend(entries[:available])
                    if len(entries) > available:
                        raise _DiscoveryError("permission-limit", "The bounded permission-entry limit was reached.")
                    next_link = payload.get("nextLink")
                    if not next_link:
                        observation.update(complete=True, provider_supported=True)
                        break
                    url = _next_link(next_link, original)
                    if url in seen:
                        raise _DiscoveryError("invalid-pagination", "Azure repeated a permission continuation.")
                    seen.add(url)
                else:
                    raise _DiscoveryError("page-limit", "The bounded permission-page limit was reached.")
        except (CostError, httpx.HTTPError, TimeoutError, _DiscoveryError) as exc:
            observation["error"] = _source_error(exc)
            if observation["error"]["code"] == "provider-unsupported":
                observation["provider_supported"] = False
    observation["duration_ms"] = round((time.monotonic() - started) * 1000, 1)
    return observation


def _capability(feature: str, operation: str, role: str, scope: str | None,
                principal: dict, observation: dict | None, *, data_plane: bool = False,
                approval: bool = False, unknown_reason: str | None = None) -> dict[str, Any]:
    verdict = evaluate_permission(
        observation["entries"] if observation else [], operation,
        data_plane=data_plane, complete=bool(observation and observation["complete"]))
    if unknown_reason:
        verdict = {"status": "unknown", "conditional": False, "reason": unknown_reason}
    elif observation and observation["error"] and verdict["status"] == "unknown":
        verdict["reason"] = observation["error"]["reason"]
    authorization = verdict["status"]
    if authorization == "declared-allowed":
        next_step = ("Review an exact scope/operation preview before approving this change."
                     if approval else "No role change is indicated by these declarations.")
    elif authorization == "denied":
        next_step = (f"Have an authorized administrator review {role}, or a custom role containing "
                     "only the required operation, at the listed scope.")
    else:
        next_step = "Verify the existing principal, role scope and conditions before proposing a permission change."
    return {
        "feature": feature, "operation": operation, "required_role": role, "scope": scope,
        "principal": principal, "tenant_id": principal.get("tenant_id"),
        "plane": "data" if data_plane else "control",
        **verdict, "authorization_status": authorization,
        "status": "approval-required" if approval and authorization == "declared-allowed" else authorization,
        "approval_required": approval, "verified": False, "next_step": next_step,
        "evidence": "ARM permission declarations" if observation else "not inspected",
    }


def _products(identity: dict | None, evidence: list[dict], now: datetime) -> list[dict]:
    output = []
    principal = _caller(identity)
    for product, role in (("reservations", "Reservations Reader"), ("savings-plans", "Savings plan Reader")):
        record = next((item for item in evidence if item.get("product") == product), {})
        observed = _date(record.get("observed_at"))
        verified = bool(record.get("inventory_read_succeeded") is True and record.get("scope")
                        and _same_principal(principal, _principal(record.get("principal")))
                        and observed and now - timedelta(hours=1) <= observed <= now)
        reason = ("A recent inventory read succeeded for this principal at the listed product scope."
                  if verified else
                  "Subscription cost permissions do not establish product inventory access. "
                  "A product/billing-scoped reader role and a real inventory read are still required.")
        output.append({
            "feature": product, "operation": "inventory-read", "required_role": role,
            "scope": record.get("scope"), "principal": principal,
            "tenant_id": principal.get("tenant_id"), "plane": "product",
            "status": "declared-allowed" if verified else "unknown",
            "authorization_status": "declared-allowed" if verified else "unknown",
            "verified": verified, "reason": reason, "approval_required": False,
            "verified_at": observed.isoformat() if verified else None,
            "coverage": record.get("coverage") if verified else "unknown",
            "next_step": ("No role change is indicated by the observed read." if verified else
                          f"Verify {role} at the relevant product/billing scope; do not widen runtime subscription roles."),
        })
    return output


def _date(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed.astimezone(UTC) if parsed.tzinfo else None


def alert_review(alerts: list[dict], now: datetime) -> list[dict]:
    """Flag renewal work without changing alerts or sending any notification."""
    output = []
    for alert in alerts:
        expiry = _date(alert.get("expires_at"))
        expired = expiry is not None and expiry <= now
        output.append({
            "feature": "alert-expiry", "id": alert.get("id"), "scope": alert.get("scope"),
            "operation": "review-alert-renewal", "required_role": None, "principal": None,
            "tenant_id": None, "plane": "configuration", "authorization_status": "unknown",
            "status": "approval-required" if expired else "unknown",
            "expired": expired if expiry else None, "expires_at": expiry.isoformat() if expiry else None,
            "approval_required": True, "verified": False,
            "reason": ("This alert has expired. Renewal and any recipients require an explicit preview."
                       if expired else "Expiry alone does not verify alert delivery or recipient approval."),
            "next_step": "Preview expiry, scope and recipients before any update or outbound notification.",
        })
    return output


async def report(subscription_ids: list[str], *, identity: dict | None = None,
                 storage: list[dict] | None = None, product_evidence: list[dict] | None = None,
                 alerts: list[dict] | None = None, force: bool = False) -> dict[str, Any]:
    """Inspect permissions with one GET per subscription, plus explicit container scopes.

    ``identity``: {id, kind, tenant_id, by_subscription: {subscription: {id, tenant_id}}}.
    ``storage``: [{purpose: focus-read|focus-write|archive-write, scope: ARM container id,
    principal: {id, kind, tenant_id}}]. Different runtime principals remain unknown rather
    than borrowing the caller's permissions. Missing purposes are reported as unknown.
    ``product_evidence``: trusted recent {product, scope, principal, observed_at,
    inventory_read_succeeded, coverage} from a real inventory read; no product calls here.
    """
    subscriptions = _subscriptions(subscription_ids)
    storage, product_evidence, alerts = storage or [], product_evidence or [], alerts or []
    if len(storage) > MAX_STORAGE_CHECKS or len(product_evidence) > 20 or len(alerts) > 20:
        raise ValueError("Too many optional diagnostic records.")
    if not isinstance(force, bool):
        raise ValueError("force must be a boolean.")
    selected = []
    scopes = {f"/subscriptions/{subscription}" for subscription in subscriptions}
    for item in storage:
        purpose = item.get("purpose")
        if purpose not in STORAGE_OPERATIONS:
            raise ValueError("Unknown storage diagnostic purpose.")
        scope = _container_scope(item.get("scope"), subscriptions)
        subscription = scope.split("/")[2]
        principal = _principal(item.get("principal"))
        matches = _same_principal(principal, _caller(identity, subscription))
        selected.append((purpose, scope, principal, matches))
        if matches:
            scopes.add(scope)
    for purpose in sorted(STORAGE_OPERATIONS.keys() - {item[0] for item in selected}):
        selected.append((purpose, None, _principal(None), False))

    started = time.monotonic()
    semaphore = asyncio.Semaphore(MAX_CONCURRENT)
    tasks = {scope: asyncio.create_task(_observe(scope, semaphore, force)) for scope in sorted(scopes)}
    try:
        _, pending = await asyncio.wait(tasks.values(), timeout=REPORT_TIMEOUT)
        for task in pending:
            task.cancel()
        await asyncio.gather(*pending, return_exceptions=True)
        observations = {
            scope: task.result() if task not in pending else {
                "scope": scope, "complete": False, "entries": [], "pages": 0,
                "api_version": PERMISSIONS_API, "provider_supported": None,
                "duration_ms": round((time.monotonic() - started) * 1000, 1),
                "error": {"code": "report-timeout", "http_status": None,
                          "reason": "The overall diagnostic time budget was reached; this access remains unknown."},
            }
            for scope, task in tasks.items()
        }
    finally:
        unfinished = [task for task in tasks.values() if not task.done()]
        for task in unfinished:
            task.cancel()
        await asyncio.gather(*tasks.values(), return_exceptions=True)

    capabilities = []
    for subscription in subscriptions:
        scope = f"/subscriptions/{subscription}"
        principal = _caller(identity, subscription)
        for feature, operation, role, approval in OPERATIONS:
            item = _capability(feature, operation, role, scope, principal, observations[scope],
                               approval=approval)
            capabilities.append({**item, "subscription_id": subscription})
    for purpose, scope, principal, matches in selected:
        operation, role = STORAGE_OPERATIONS[purpose]
        reason = None if matches else (
            "The exact container and runtime principal were not supplied." if scope is None else
            "The container runtime principal differs from, or cannot be matched to, this caller. "
            "The effective-permissions API cannot inspect another principal's data roles.")
        item = _capability(purpose, operation, role, scope, principal,
                           observations.get(scope) if matches else None, data_plane=True,
                           unknown_reason=reason)
        capabilities.append({**item, "network_status": "unknown", "data_plane_status": "not-probed"})
    now = datetime.now(UTC)
    capabilities.extend(_products(identity, product_evidence, now))
    capabilities.extend(alert_review(alerts, now))
    errors = [{"scope": scope, **observation["error"]} for scope, observation in observations.items()
              if observation["error"]]
    read_only = read_only_mode()
    return {
        "status": "partial" if errors or any(item["status"] in {"unknown", "denied"}
                                           for item in capabilities) else "ok",
        "generated_at": now.isoformat(), "subscriptions": subscriptions,
        "identity": _caller(identity), "capabilities": capabilities, "errors": errors,
        "observations": [{key: value for key, value in observation.items() if key != "entries"}
                         for observation in observations.values()],
        "duration_ms": round((time.monotonic() - started) * 1000, 1),
        "cache_policy": "caller-partitioned ARM client cache" if not force else "fresh ARM reads requested",
        "deployment_policy": {
            "read_only": read_only, "mutations_blocked": read_only,
            "reason": (
                "Deployment read-only policy blocks operational mutations regardless of Azure permission declarations."
                if read_only else
                "Deployment policy does not itself block mutations; permissions and applicable approvals are still required."),
        },
        "writes_executed": False, "roles_granted": False, "notifications_sent": False,
        "limitations": [
            "Permission declarations are not trial-write success; conditions and deny assignments may still prevent an operation.",
            "Blob dataActions are separate from ARM actions. ARM permissions do not test storage firewall, private DNS, routing, or data-plane access.",
            "FOCUS export identity, FOCUS reader and archive writer may be different principals on different containers.",
            "Export permission does not prove billing-scope support, report generation, delivery, or completeness; billing data can arrive late.",
            "Reservation and savings-plan product lifecycle access is independent of subscription cost-reader access.",
            "No role grants, export runs, budget changes, alert updates or outbound messages were performed.",
        ],
    }
