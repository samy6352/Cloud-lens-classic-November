"""Hermetic tests: all Resource Graph and warehouse access is mocked."""
from __future__ import annotations

import asyncio
import importlib
import json
import sys
from types import ModuleType
from unittest.mock import AsyncMock, Mock, patch

import pytest

SUB = "11111111-1111-1111-1111-111111111111"
OTHER = "22222222-2222-2222-2222-222222222222"
RID = f"/subscriptions/{SUB}/resourceGroups/rg/providers/Microsoft.Compute/virtualMachines/vm"
WINDOW = {"start": "2026-09-01T12:00:00Z", "end": "2026-09-08T12:00:00Z", "days": 7}


@pytest.fixture
def api(monkeypatch):
    with patch.dict(sys.modules, {"app.warehouse": ModuleType("app.warehouse")}):
        module = importlib.import_module("app.change_analysis")
    monkeypatch.setattr(module, "scoped_graph", AsyncMock(
        return_value={"rows": [], "truncated": False, "total": 0, "pages": 1}))
    monkeypatch.setattr(module, "resource_costs", Mock(return_value={}))
    monkeypatch.setattr(module, "evidence_window", Mock(return_value=dict(WINDOW)))
    return module


def row(**kwargs):
    return {
        "id": RID + "/providers/Microsoft.Resources/changes/event",
        "subscriptionId": SUB, "targetResourceId": RID,
        "changedAt": "2026-09-07T12:00:00Z", "changeType": "Update",
        "changedBy": "operator@example.invalid", "operation": "Microsoft.Compute/virtualMachines/write",
        "changes": {}, **kwargs,
    }


def run(api, rows, **metadata):
    api.scoped_graph.return_value = {
        "rows": rows, "truncated": False, "total": len(rows), "pages": 1, **metadata,
    }
    return asyncio.run(api.report([SUB]))


def test_empty_is_explicit_success_and_query_has_both_utc_bounds(api):
    result = run(api, [])
    assert result["status"] == "ok"
    assert result["changes"] == []
    assert result["source"] == "Azure Resource Graph (resourcechanges)"
    assert result["generated_at"].endswith("Z")
    assert "14 days" in " ".join(result["notices"])
    query, scope = api.scoped_graph.call_args.args
    assert "resourcechanges" in query and "properties.changeAttributes.changedBy" in query
    assert "properties.changeAttributes.clientType" in query
    assert "datetime(2026-09-01T12:00:00Z)" in query
    assert "datetime(2026-09-08T12:00:00Z)" in query
    assert scope == [SUB]
    api.resource_costs.assert_not_called()


@pytest.mark.parametrize("error", [RuntimeError("denied"), TimeoutError("timed out")])
def test_graph_errors_are_not_empty_success(api, error):
    api.scoped_graph.side_effect = error
    with pytest.raises(type(error), match=str(error)):
        asyncio.run(api.report([SUB]))
    api.resource_costs.assert_not_called()


@pytest.mark.parametrize("payload", [None, {}, {"rows": None, "truncated": False},
                                    {"rows": [], "truncated": "false"}])
def test_invalid_response_is_not_empty_success(api, payload):
    api.scoped_graph.return_value = payload
    with pytest.raises(api.SourceError):
        asyncio.run(api.report([SUB]))


def test_safe_infrastructure_values_and_sensitive_values_redacted(api):
    raw = {
        "properties.hardwareProfile.vmSize": {"previousValue": "Standard_D2s_v5",
                                             "newValue": "Standard_D4s_v5"},
        "sku.capacity": {"previousValue": 1, "newValue": 3},
        "properties.profiles[0].capacity.maximum": {"previousValue": "2", "newValue": "5"},
        "tags.owner": {"previousValue": "secret-tag", "newValue": "private-tag"},
        "properties.siteConfig.appSettings": {"newValue": {"API_KEY": "secret-key"}},
        "properties.connectionStrings": {"newValue": "Server=secret-server;Password=secret-password"},
        "identity.credentials": {"newValue": "secret-credential"},
        "sku": {"newValue": {"name": "P1", "password": "secret-nested"}},
        "sku.size": {"newValue": {"password": "secret-safe-key"}},
        "sku.name": {"newValue": "https://secret.example.invalid/token"},
    }
    result = run(api, [row(changes=raw)])
    properties = {item["name"]: item for item in result["changes"][0]["properties"]}
    assert properties["properties.hardwareProfile.vmSize"]["after"] == "Standard_D4s_v5"
    assert properties["sku.capacity"]["after"] == 3
    assert properties["properties.profiles[0].capacity.maximum"]["after"] == "5"
    for name in list(raw)[3:]:
        assert properties[name]["after"] == api.REDACTED
    text = json.dumps(result)
    assert "secret-" not in text and "private-tag" not in text


@pytest.mark.parametrize("overrides", [
    {"subscriptionId": OTHER},
    {"targetResourceId": RID.replace(SUB, OTHER)},
    {"targetResourceId": f"/subscriptions/{SUB}/resourceGroups/rg/providers/Microsoft.Compute/vm"},
    {"targetResourceId": RID + "?secret"},
    {"targetResourceId": None},
    {"changedAt": "not a date"},
    {"changedAt": "2026-09-07T12:00:00"},
    {"changedAt": "2026-08-01T12:00:00Z"},
    {"changes": []},
    {"changes": {"sku.name": "invalid"}},
])
def test_invalid_rows_and_scope_mismatch_fail_closed(api, overrides):
    with pytest.raises(api.SourceError):
        run(api, [row(**overrides)])
    api.resource_costs.assert_not_called()


def test_matching_another_selected_scope_is_still_a_row_scope_mismatch(api):
    api.scoped_graph.return_value = {
        "rows": [row(targetResourceId=RID.replace(SUB, OTHER))], "truncated": False,
    }
    with pytest.raises(api.SourceError):
        asyncio.run(api.report([SUB, OTHER]))


@pytest.mark.parametrize("actor", [None, "", "Unknown", "Unspecified", {"name": "not a string"}])
def test_unknown_actor_and_deleted_resource_without_inventory(api, actor):
    result = run(api, [row(changeType="Delete", changedBy=actor, changes=None)])
    change = result["changes"][0]
    assert change["actor"] is None
    assert change["resource_id"] == RID
    assert change["resource_name"] == "vm"
    assert change["resource_type"] == "Microsoft.Compute/virtualMachines"
    assert change["change_type"] == "Delete"
    assert change["properties"] == []
    assert change["cost"] is None


def test_newest_first_change_cap_and_cost_ids_deduplicated(api, monkeypatch):
    monkeypatch.setattr(api, "MAX_CHANGES", 2)
    result = run(api, [row(id="old", changedAt="2026-09-02T12:00:00Z"),
                       row(id="newest"), row(id="second", changedAt="2026-09-06T12:00:00Z")])
    assert [change["id"] for change in result["changes"]] == ["newest", "second"]
    assert result["status"] == "partial" and result["truncated"]
    assert api.resource_costs.call_args.args == (
        [SUB], [RID.lower()], WINDOW["start"], WINDOW["end"])


@pytest.mark.parametrize("metadata", [{"truncated": True}, {"total": 12}])
def test_helper_pagination_truncation_and_total_are_visible(api, metadata):
    result = run(api, [row()], **metadata)
    assert result["status"] == "partial" and result["truncated"]


def test_property_caps_long_values_and_upstream_truncation(api):
    properties = {f"tags.key{i}": {"newValue": "sensitive"} for i in range(25)}
    result = run(api, [row(changes=properties)])
    assert len(result["changes"][0]["properties"]) == 20
    assert result["changes"][0]["properties_truncated"]
    result = run(api, [row(changes={
        "sku.name": {"newValue": "x" * 501},
        "sku.capacity": {"previousValue": 1, "newValue": 2, "isTruncated": "true"},
    })])
    assert result["status"] == "partial" and result["truncated"]
    assert result["changes"][0]["properties"][0]["after"] == api.REDACTED


def test_cost_currencies_are_separate_and_missing_cost_is_null(api):
    cost = {"by_currency": [{"currency": "USD", "cost": 12.3}, {"currency": "EUR", "cost": 4.5}],
            "rows": 3, "from": "2026-09-01", "to": "2026-09-08"}
    api.resource_costs.return_value = {RID.lower(): cost}
    result = run(api, [row(id="first"), row(id="second", targetResourceId=RID + "2")])
    by_id = {change["id"]: change for change in result["changes"]}
    assert by_id["first"]["cost"] == cost
    assert by_id["second"]["cost"] is None
    assert "does not prove" in " ".join(result["notices"])


def test_warehouse_errors_propagate(api):
    api.resource_costs.side_effect = RuntimeError("warehouse unavailable")
    with pytest.raises(RuntimeError, match="warehouse unavailable"):
        run(api, [row()])


def test_unexpected_scalar_values_and_metadata_do_not_expose_objects(api):
    result = run(api, [row(changedBy={"token": "secret"}, operation=["secret"],
                          changeType={"token": "secret"}, changes={
                              "sku.capacity": {"newValue": 10 ** 1000},
                              "sku.size": {"newValue": float("nan")},
                              "location": {"newValue": ["secret"]},
                          })])
    change = result["changes"][0]
    assert change["actor"] is None and change["operation"] is None
    assert change["change_type"] == "Unknown"
    assert all(prop["after"] == api.REDACTED for prop in change["properties"])
    assert "secret" not in json.dumps(result, allow_nan=False)


def test_nested_and_extension_target_ids_preserve_correct_resource_type(api):
    nested = RID + "/extensions/agent"
    extension = RID + "/providers/Microsoft.Insights/diagnosticSettings/logs"
    result = run(api, [row(id="nested", targetResourceId=nested),
                       row(id="extension", targetResourceId=extension)])
    by_id = {change["id"]: change for change in result["changes"]}
    assert by_id["nested"]["resource_type"] == "Microsoft.Compute/virtualMachines/extensions"
    assert by_id["extension"]["resource_type"] == "Microsoft.Insights/diagnosticSettings"
    assert by_id["extension"]["resource_name"] == "logs"


@pytest.mark.parametrize("target, expected_type, expected_name", [
    (f"/subscriptions/{SUB}/resourceGroups/new-group",
     "Microsoft.Resources/subscriptions/resourceGroups", "new-group"),
    (f"/subscriptions/{SUB}/providers/Microsoft.Authorization/policyAssignments/baseline",
     "Microsoft.Authorization/policyAssignments", "baseline"),
])
def test_resource_group_and_subscription_provider_create_events(api, target,
                                                               expected_type, expected_name):
    result = run(api, [row(targetResourceId=target, changeType="Create", changes=None)])
    change = result["changes"][0]
    assert result["status"] == "ok"
    assert change["resource_id"] == target
    assert change["resource_type"] == expected_type
    assert change["resource_name"] == expected_name
    assert change["change_type"] == "Create"
    assert change["properties"] == []
    assert change["cost"] is None
    assert api.resource_costs.call_args.args[1] == [target.lower()]


@pytest.mark.parametrize("target", [
    f"/subscriptions/{OTHER}/resourceGroups/new-group",
    f"/subscriptions/{SUB}/resourceGroups",
    f"/subscriptions/{SUB}/resourceGroups/",
    f"/subscriptions/{SUB}/resourceGroups/../",
    f"/subscriptions/{SUB}/other/new-group",
    f"/subscriptions/{SUB}/providers/Microsoft.Authorization/policyAssignments",
])
def test_invalid_group_and_subscription_provider_targets_fail_closed(api, target):
    with pytest.raises(api.SourceError):
        run(api, [row(targetResourceId=target, changeType="Create", changes=None)])
    api.resource_costs.assert_not_called()


@pytest.mark.parametrize("days", [0, 15, -1, True, "7", None, 1.5])
def test_days_validation_precedes_source_access(api, days):
    with pytest.raises(api.SourceError):
        asyncio.run(api.report([SUB], days))
    api.scoped_graph.assert_not_called()
    api.evidence_window.assert_not_called()


@pytest.mark.parametrize("scope", [[], ["not-a-guid"], [None], "all"])
def test_explicit_uuid_scope_required(api, scope):
    with pytest.raises(api.SourceError):
        asyncio.run(api.report(scope))
    api.scoped_graph.assert_not_called()


@pytest.mark.parametrize("days", [1, 14])
def test_supported_window_boundaries(api, days):
    asyncio.run(api.report([SUB], days))
    api.evidence_window.assert_called_once_with(days)
