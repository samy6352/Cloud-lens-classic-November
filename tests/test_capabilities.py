"""Hermetic capability diagnostics: exact declarations, no Azure or trial writes."""
from __future__ import annotations

import asyncio
import contextvars
import json
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock

import httpx
import pytest

from app import capabilities as cap
from app.cost import CostError

pytestmark = pytest.mark.offline

A = "11111111-1111-1111-1111-111111111111"
B = "22222222-2222-2222-2222-222222222222"
C = "33333333-3333-3333-3333-333333333333"
IDENTITY = {"id": "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
            "tenant_id": "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb", "kind": "application"}
CONTAINER = (f"/subscriptions/{A}/resourceGroups/fixture/providers/Microsoft.Storage/"
             "storageAccounts/fixturestore/blobServices/default/containers/focus")
FULL = {"actions": ["*"], "notActions": [], "dataActions": ["*"], "notDataActions": []}


@pytest.fixture
def arm(monkeypatch):
    arm = AsyncMock(return_value={"value": [FULL]})
    monkeypatch.setattr(cap.azure, "request", arm)
    return arm


def run(subscriptions=None, **kwargs):
    return asyncio.run(cap.report(subscriptions or [A], identity=kwargs.pop("identity", IDENTITY), **kwargs))


def feature(result, name, scope=None):
    return next(item for item in result["capabilities"]
                if item["feature"] == name and (scope is None or item["scope"] == scope))


@pytest.mark.parametrize("pattern,operation,expected", [
    ("Microsoft.CostManagement/*", "Microsoft.CostManagement/exports/run/action", True),
    ("*/read", "Microsoft.CostManagement/query/read", True),
    ("*/read", "Microsoft.CostManagement/exports/run/action", False),
    ("MICROSOFT.COSTMANAGEMENT/EXPORTS/WRITE", "Microsoft.CostManagement/exports/write", True),
    ("Microsoft.CostManagement/exports/read", "Microsoft.CostManagement/exports/write", False),
    ("Microsoft.?ostManagement/*", "Microsoft.CostManagement/exports/read", False),
    ("Microsoft.[C]ostManagement/*", "Microsoft.CostManagement/exports/read", False),
])
def test_arm_wildcards_are_exact_and_case_insensitive(pattern, operation, expected):
    assert cap.action_matches(pattern, operation) is expected


def test_exclusions_are_per_entry_not_global_deny():
    operation = "Microsoft.CostManagement/exports/write"
    entries = [{"actions": ["*"], "notActions": ["Microsoft.CostManagement/exports/*"]}]
    assert cap.evaluate_permission(entries, operation)["status"] == "denied"
    entries.append({"actions": [operation]})
    assert cap.evaluate_permission(entries, operation)["status"] == "declared-allowed"


def test_data_actions_never_grant_arm_actions_and_arm_owner_does_not_grant_blob_data():
    blob = cap.STORAGE_OPERATIONS["focus-read"][0]
    assert cap.evaluate_permission([{"actions": ["*"]}], blob, data_plane=True)["status"] == "denied"
    assert cap.evaluate_permission([{"dataActions": ["*"]}], cap.OPERATIONS[0][1])["status"] == "denied"
    entries = [{"dataActions": ["*"], "notDataActions": ["*/blobs/write"]}]
    assert cap.evaluate_permission(entries, blob, data_plane=True)["status"] == "declared-allowed"
    assert cap.evaluate_permission(entries, cap.STORAGE_OPERATIONS["focus-write"][0],
                                   data_plane=True)["status"] == "denied"


def test_conditional_grants_and_incomplete_collections_are_unknown():
    operation = cap.OPERATIONS[0][1]
    entries = [{"actions": ["*"], "condition": "unverified condition", "conditionVersion": "2.0"}]
    result = cap.evaluate_permission(entries, operation)
    assert result["status"] == "unknown" and result["conditional"]
    assert cap.evaluate_permission([], operation, complete=False)["status"] == "unknown"
    assert cap.evaluate_permission([], operation, complete=True)["status"] == "denied"
    entries.append({"actions": ["*/read"]})
    assert cap.evaluate_permission(entries, operation)["status"] == "declared-allowed"


def test_three_subscriptions_need_three_gets_and_no_role_regrant(arm):
    result = run([A, B, C])
    assert arm.await_count == 3
    assert {call.args[0] for call in arm.await_args_list} == {"GET"}
    for call in arm.await_args_list:
        assert call.args[1].endswith("/providers/Microsoft.Authorization/permissions")
        assert call.kwargs == {"params": {"api-version": "2022-04-01"}, "attempts": 1, "cache": True}
        assert any(f"/subscriptions/{sub}/" in call.args[1] for sub in (A, B, C))
    for item in result["capabilities"]:
        if item["plane"] == "control":
            assert item["authorization_status"] == "declared-allowed"
            assert item["status"] in {"declared-allowed", "approval-required"}
            assert "administrator" not in item["next_step"].lower()
    assert not result["writes_executed"] and not result["roles_granted"] and not result["notifications_sent"]
    assert all(item["required_role"] not in {"Owner", "Contributor", "User Access Administrator"}
               for item in result["capabilities"])


def test_exact_provider_actions_not_http_verbs(arm):
    arm.return_value = {"value": [{"actions": ["*/read"]}]}
    result = run()
    assert feature(result, "cost-query")["status"] == "declared-allowed"
    assert feature(result, "cost-query")["operation"] == "Microsoft.CostManagement/query/read"
    assert feature(result, "exports-run")["operation"] == "Microsoft.CostManagement/exports/run/action"
    assert feature(result, "exports-run")["status"] == "denied"
    assert feature(result, "budgets-write")["operation"] == "Microsoft.Consumption/budgets/write"
    assert feature(result, "budgets-write")["status"] == "denied"


def test_force_bypasses_existing_caller_partitioned_client_cache(arm):
    run(force=True)
    assert arm.await_args.kwargs["cache"] is False


@pytest.mark.parametrize("value,blocked", [("true", True), ("1", True), ("YES", True),
                                         ("false", False), ("0", False)])
def test_deployment_read_only_policy_is_separate_from_azure_permissions(arm, monkeypatch, value, blocked):
    monkeypatch.setenv("CLOUDLENS_READ_ONLY", value)
    result = run()
    assert result["deployment_policy"]["mutations_blocked"] is blocked
    assert result["deployment_policy"]["read_only"] is blocked
    assert feature(result, "cost-query")["status"] == "declared-allowed"
    assert feature(result, "exports-configure")["authorization_status"] == "declared-allowed"
    assert feature(result, "exports-configure")["status"] == "approval-required"
    assert arm.await_count == 1 and arm.await_args.args[0] == "GET"


def test_context_and_subscription_tenants_are_preserved(arm):
    marker = contextvars.ContextVar("capability-caller", default="server")
    captured = []

    async def get(*args, **kwargs):
        captured.append(marker.get())
        return {"value": [FULL]}

    arm.side_effect = get
    identity = {**IDENTITY, "by_subscription": {B: {"tenant_id": "guest-tenant", "id": "guest-object"}}}
    token = marker.set("delegated")
    try:
        result = run([A, B], identity=identity)
        assert marker.get() == "delegated"
    finally:
        marker.reset(token)
    assert captured == ["delegated", "delegated"]
    assert feature(result, "cost-query", f"/subscriptions/{B}")["tenant_id"] == "guest-tenant"
    assert feature(result, "cost-query", f"/subscriptions/{B}")["principal"]["id"] == "guest-object"


@pytest.mark.parametrize("error,code", [
    (httpx.HTTPStatusError("forbidden", request=httpx.Request("GET", cap.ARM),
                          response=httpx.Response(403)), "permission-discovery-denied"),
    (CostError("Azure refused this on that scope: fixture-private-message"), "permission-discovery-denied"),
    (CostError("NoRegisteredProviderFound: fixture-private-message"), "provider-unsupported"),
    (CostError("Azure rejected the credential (401)"), "credential-rejected"),
    (httpx.HTTPStatusError("throttled", request=httpx.Request("GET", cap.ARM),
                          response=httpx.Response(429)), "throttled"),
    (httpx.ConnectError("fixture-private-message"), "permission-discovery-failed"),
])
def test_discovery_failure_is_unknown_never_denied_runtime_access(arm, error, code):
    arm.side_effect = error
    result = run()
    assert result["status"] == "partial" and result["errors"][0]["code"] == code
    assert all(item["status"] == "unknown" for item in result["capabilities"])
    assert "fixture-private-message" not in json.dumps(result)
    if code == "provider-unsupported":
        assert result["observations"][0]["provider_supported"] is False


def test_explicit_provider_error_and_invalid_payload_are_not_empty_permissions(arm):
    for payload, code in [
        ({"error": {"code": "NoRegisteredProviderFound", "message": "private"}}, "provider-unsupported"),
        ({"value": "not a list"}, "invalid-response"),
        ({"value": [{"actions": ["*"], "notActions": "invalid"}]}, "invalid-response"),
        ({"value": [{}]}, "invalid-response"),
    ]:
        arm.return_value = payload
        result = run()
        assert result["errors"][0]["code"] == code
        assert feature(result, "cost-query")["status"] == "unknown"


def test_complete_empty_permission_collection_is_an_explicit_denial(arm):
    arm.return_value = {"value": []}
    result = run()
    assert not result["errors"]
    assert feature(result, "cost-query")["status"] == "denied"


def test_one_failed_subscription_does_not_hide_other_declared_permissions(arm):
    async def get(method, url, **kwargs):
        if f"/subscriptions/{B}/" in url:
            raise CostError("AuthorizationFailed")
        return {"value": [FULL]}

    arm.side_effect = get
    result = run([A, B])
    assert feature(result, "cost-query", f"/subscriptions/{A}")["status"] == "declared-allowed"
    assert feature(result, "cost-query", f"/subscriptions/{B}")["status"] == "unknown"
    assert len(result["errors"]) == 1 and result["errors"][0]["scope"] == f"/subscriptions/{B}"


def test_nullable_optional_action_arrays_do_not_mutate_cached_payload(arm):
    payload = {"value": [{"actions": ["*/read"], "notActions": None,
                          "dataActions": None, "notDataActions": None}]}
    arm.return_value = payload
    result = run()
    assert not result["errors"]
    assert feature(result, "cost-query")["status"] == "declared-allowed"
    assert feature(result, "exports-run")["status"] == "denied"
    assert payload["value"][0]["notActions"] is None


def test_safe_pagination_unions_permission_entries(arm):
    url = f"{cap.ARM}/subscriptions/{A}/providers/Microsoft.Authorization/permissions"
    arm.side_effect = [
        {"value": [{"actions": ["*"], "notActions": ["Microsoft.CostManagement/*"]}],
         "nextLink": url + "?api-version=2022-04-01&$skiptoken=page2"},
        {"value": [{"actions": ["Microsoft.CostManagement/query/read"]}]},
    ]
    result = run()
    assert result["observations"][0]["complete"] and result["observations"][0]["pages"] == 2
    assert feature(result, "cost-query")["status"] == "declared-allowed"
    assert feature(result, "exports-run")["status"] == "denied"


@pytest.mark.parametrize("link", [
    "https://example.invalid/permissions",
    "https://[invalid",
    f"{cap.ARM}/subscriptions/{B}/providers/Microsoft.Authorization/permissions",
    f"http://management.azure.com/subscriptions/{A}/providers/Microsoft.Authorization/permissions",
    f"{cap.ARM}/subscriptions/{A}/providers/Microsoft.Authorization/roleAssignments",
])
def test_permission_pagination_cannot_leave_original_scope(arm, link):
    arm.return_value = {"value": [], "nextLink": link}
    result = run()
    assert arm.await_count == 1
    assert result["errors"][0]["code"] == "invalid-pagination"
    assert feature(result, "cost-query")["status"] == "unknown"


def test_page_and_entry_budgets_are_explicit_partial_results(arm, monkeypatch):
    monkeypatch.setattr(cap, "MAX_PAGES", 1)
    url = f"{cap.ARM}/subscriptions/{A}/providers/Microsoft.Authorization/permissions"
    arm.return_value = {"value": [], "nextLink": url + "?next=2"}
    assert run()["errors"][0]["code"] == "page-limit"
    arm.return_value = {"value": [{"actions": []}, {"actions": []}]}
    monkeypatch.setattr(cap, "MAX_PERMISSION_ENTRIES", 1)
    assert run()["errors"][0]["code"] == "permission-limit"


def test_storage_requirements_are_per_container_and_do_not_prove_data_plane(arm):
    archive = CONTAINER.replace("/containers/focus", "/containers/archive")
    storage = [
        {"purpose": "focus-read", "scope": CONTAINER, "principal": IDENTITY},
        {"purpose": "focus-write", "scope": CONTAINER, "principal": IDENTITY},
        {"purpose": "archive-write", "scope": archive, "principal": IDENTITY},
    ]
    result = run(storage=storage)
    assert arm.await_count == 3, "same-container roles share one permissions GET"
    for purpose, role in (("focus-read", "Storage Blob Data Reader"),
                          ("focus-write", "Storage Blob Data Contributor"),
                          ("archive-write", "Storage Blob Data Contributor")):
        item = feature(result, purpose)
        assert item["status"] == "declared-allowed" and item["required_role"] == role
        assert item["plane"] == "data" and not item["verified"]
        assert item["network_status"] == "unknown" and item["data_plane_status"] == "not-probed"
    assert feature(result, "focus-read")["scope"] != feature(result, "archive-write")["scope"]


def test_export_identity_does_not_borrow_callers_blob_permissions(arm):
    storage = [{"purpose": "focus-write", "scope": CONTAINER,
                "principal": {**IDENTITY, "id": "different-export-identity"}}]
    result = run(storage=storage)
    assert arm.await_count == 1
    assert feature(result, "focus-write")["status"] == "unknown"
    assert "another principal" in feature(result, "focus-write")["reason"]


@pytest.mark.parametrize("subscriptions,storage", [
    ([], None), (["not-a-uuid"], None), ([A] * 51, None),
    ([A], [{"purpose": "focus-read", "scope": CONTAINER.replace(A, B)}]),
    ([A], [{"purpose": "focus-read", "scope": CONTAINER + "/../other"}]),
    ([A], [{"purpose": "focus-read", "scope": CONTAINER + "?secret=bad"}]),
    ([A], [{"purpose": "other", "scope": CONTAINER}]),
])
def test_invalid_scopes_fail_before_any_get(arm, subscriptions, storage):
    with pytest.raises(ValueError):
        asyncio.run(cap.report(subscriptions, storage=storage))
    arm.assert_not_awaited()


def test_injected_identity_secrets_are_never_serialized(arm):
    result = run(identity={**IDENTITY, "token": "fixture-secret", "password": "fixture-secret"})
    assert "fixture-secret" not in json.dumps(result)
    assert result["identity"] == IDENTITY


def test_product_access_requires_recent_matching_actual_inventory_evidence(arm):
    now = datetime.now(UTC)
    record = {"product": "reservations", "scope": "/providers/Microsoft.Capacity/reservationOrders/order",
              "principal": IDENTITY, "observed_at": (now - timedelta(seconds=1)).isoformat(),
              "inventory_read_succeeded": True, "coverage": "partial"}
    result = run(product_evidence=[record])
    assert arm.await_count == 1, "subscription permissions must not fan out into product inventory calls"
    assert feature(result, "reservations")["verified"]
    assert feature(result, "reservations")["coverage"] == "partial"
    assert feature(result, "savings-plans")["status"] == "unknown"
    for change in ({"inventory_read_succeeded": False},
                   {"observed_at": (now - timedelta(hours=2)).isoformat()},
                   {"principal": {**IDENTITY, "tenant_id": "another-tenant"}}):
        result = run(product_evidence=[{**record, **change}])
        assert feature(result, "reservations")["status"] == "unknown"
        assert not feature(result, "reservations")["verified"]


def test_expired_alerts_require_preview_without_sending_or_exposing_recipients(arm):
    result = run(alerts=[{"id": "alert", "scope": f"/subscriptions/{A}",
                         "expires_at": "2000-01-01T00:00:00Z", "recipients": ["private@example.invalid"]}])
    item = feature(result, "alert-expiry")
    assert item["status"] == "approval-required" and item["expired"]
    assert "preview" in item["reason"]
    assert not result["notifications_sent"]
    assert "private@example.invalid" not in json.dumps(result)


def test_bounded_concurrency(arm, monkeypatch):
    active, maximum = 0, 0

    async def get(*args, **kwargs):
        nonlocal active, maximum
        active += 1
        maximum = max(maximum, active)
        try:
            await asyncio.sleep(0.005)
            return {"value": [FULL]}
        finally:
            active -= 1

    arm.side_effect = get
    monkeypatch.setattr(cap, "MAX_CONCURRENT", 2)
    subscriptions = [f"{index:08x}-1111-1111-1111-111111111111" for index in range(8)]
    run(subscriptions)
    assert maximum == 2 and active == 0 and arm.await_count == 8


@pytest.mark.parametrize("overall", [False, True])
def test_time_budgets_cancel_pending_reads_and_return_unknown(arm, monkeypatch, overall):
    active = 0

    async def slow(*args, **kwargs):
        nonlocal active
        active += 1
        try:
            await asyncio.sleep(10)
            return {"value": [FULL]}
        finally:
            active -= 1

    arm.side_effect = slow
    monkeypatch.setattr(cap, "SCOPE_TIMEOUT", 5 if overall else 0.01)
    monkeypatch.setattr(cap, "REPORT_TIMEOUT", 0.01 if overall else 1)
    result = run([A, B])
    assert active == 0
    assert all(error["code"] == ("report-timeout" if overall else "timeout") for error in result["errors"])
    assert feature(result, "cost-query")["status"] == "unknown"


def test_programming_errors_are_not_swallowed_as_permission_denials(arm):
    arm.side_effect = TypeError("fixture programming error")
    with pytest.raises(TypeError, match="fixture programming error"):
        run([A, B])
