"""Automatic import selection must never borrow another deployment's storage."""
# cloudlens-test: requires-offline
import asyncio
import copy
import json
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from fastapi import HTTPException

from tests.test_focus_schedules import ACCOUNT, destination, offline_modules  # noqa: F401


@pytest.fixture
def services(monkeypatch, request):
    request.getfixturevalue("destination")
    import app.warehouse as warehouse_module
    from app import archive, exports, main, onboarding

    owned = {
        "id": "owned-export", "name": "owned-export", "subscription_id": "sub-a",
        "type": "FocusCost", "timeframe": "MonthToDate", "storage_account": "offline",
        "storage_resource_id": ACCOUNT, "container": "focus-exports", "root_folder": "focus/sub-a",
    }
    foreign = {
        **owned, "id": "foreign-export", "name": "foreign-export",
        "storage_account": "anotherdeployment", "storage_resource_id": ACCOUNT + "other",
    }
    discover_real = exports.discover_exports
    discover = AsyncMock(return_value={"exports": [foreign, owned]})
    reachable = AsyncMock(return_value=([owned], []))
    load = AsyncMock(return_value={"loaded": ["owned-export"], "problems": []})
    warehouse = SimpleNamespace(
        state={}, row_count=lambda: 0, quick_ingest=AsyncMock(return_value={"rows": 2}),
    )
    monkeypatch.setattr(warehouse_module, "warehouse", warehouse)
    monkeypatch.setattr(exports, "discover_exports", discover)
    monkeypatch.setattr(exports, "reachable", reachable)
    monkeypatch.setattr(main, "_load_export_pair", load)
    monkeypatch.setattr(main, "permitted", AsyncMock(return_value=["sub-a"]))
    monkeypatch.setattr(main, "_ingest_task", None)
    monkeypatch.delenv("COST_EXPORT_SAS_URL", raising=False)

    @asynccontextmanager
    async def caller(user):
        yield

    monkeypatch.setattr(main, "as_user", caller)
    jobs = []
    async def start_job(factory, **kwargs):
        jobs.append((factory, kwargs))
        return SimpleNamespace(job_id=str(uuid4()))

    monkeypatch.setattr(main, "_start_ingest_task", start_job)
    http_request = SimpleNamespace(state=SimpleNamespace(user=SimpleNamespace(
        admin=True, source="local", name="Test", sid="session")), query_params={})
    return SimpleNamespace(
        archive=archive, exports=exports, main=main, onboarding=onboarding, owned=owned,
        foreign=foreign, discover=discover, reachable=reachable, load=load,
        warehouse=warehouse, request=http_request, jobs=jobs, discover_real=discover_real,
    )


def test_storage_identity_is_case_insensitive_and_not_a_name_only_match(services):
    s = services
    assert s.exports.uses_deployment_storage(s.owned)
    assert s.exports.uses_deployment_storage({
        **s.owned, "storage_resource_id": ACCOUNT.upper() + "/",
    })
    assert not s.exports.uses_deployment_storage(s.foreign)
    assert not s.exports.uses_deployment_storage({
        **s.owned, "storage_resource_id": "/subscriptions/elsewhere/" + ACCOUNT.rsplit("/", 1)[1],
    })
    assert not s.exports.uses_deployment_storage({**s.owned, "storage_resource_id": None})


def test_explicit_legacy_account_name_works_but_missing_configuration_does_not(services, monkeypatch):
    s = services
    monkeypatch.setattr(s.archive, "ACCOUNT_ID", "")
    assert s.exports.uses_deployment_storage({**s.owned, "storage_account": "OFFLINE"})
    assert not s.exports.uses_deployment_storage(s.foreign)
    monkeypatch.setattr(s.archive, "ACCOUNT", "")
    assert not s.exports.uses_deployment_storage(s.owned)
    assert "No export storage is configured" in s.exports.deployment_export_hint()


def test_background_refresh_only_probes_its_own_storage(services):
    s = services
    result = asyncio.run(s.main._refresh_primary([{"id": "sub-a"}]))
    s.reachable.assert_awaited_once_with([s.owned])
    s.load.assert_awaited_once()
    assert s.load.await_args.args[0] == [s.owned]
    assert s.load.await_args.kwargs["scope"] == ["sub-a"]
    assert result["loaded"] == ["owned-export"]


def test_background_refresh_reports_missing_owned_export_and_uses_quick_only(services):
    s = services
    s.discover.return_value = {"exports": [s.foreign]}
    result = asyncio.run(s.main._refresh_primary([{"id": "sub-a"}]))
    s.reachable.assert_not_awaited()
    s.load.assert_not_awaited()
    s.warehouse.quick_ingest.assert_awaited_once()
    assert result["loaded"] == []
    assert "offline" in s.warehouse.state["focus_problems"][0][1]
    # The meaning, not the phrasing: a foreign export is never adopted silently, and the
    # operator is pointed at the one route that works.
    assert "not used automatically" in s.warehouse.state["focus_problems"][0][1]
    assert "focus-exports.ps1" in s.warehouse.state["focus_problems"][0][1]


def test_onboarding_never_probes_foreign_storage(services):
    s = services
    s.discover.return_value = {"exports": [s.foreign]}
    result = asyncio.run(s.onboarding._use_existing_exports([{"id": "sub-a"}]))
    assert result["rows"] == 0
    assert "offline" in result["detail"]
    s.reachable.assert_not_awaited()
    s.load.assert_not_awaited()


def test_onboarding_creates_own_export_after_ignoring_foreign_reports(services, monkeypatch):
    from app import jobs

    s = services
    s.discover.return_value = {"exports": [s.foreign]}
    create = AsyncMock(return_value={"created": 1, "detail": "created"})
    monkeypatch.setattr(s.onboarding, "_create_exports", create)
    monkeypatch.setattr(s.onboarding, "_load_from_api", AsyncMock(return_value=0))
    monkeypatch.setattr(s.onboarding, "_lock", asyncio.Lock())
    monkeypatch.setattr(s.onboarding, "_progress", {
        "running": False, "steps": [], "started": 0.0, "finished": 0.0,
    })
    subs = [{"id": "sub-a"}]
    token = jobs.current_job.set(str(uuid4()))
    try:
        asyncio.run(s.onboarding._run(subs, 3))
    finally:
        jobs.current_job.reset(token)
    create.assert_awaited_once_with(subs)
    s.reachable.assert_not_awaited()


def test_unselected_manual_refresh_never_probes_foreign_storage(services):
    s = services
    s.discover.return_value = {"exports": [s.foreign]}
    before = copy.deepcopy(s.foreign)
    with pytest.raises(HTTPException) as error:
        asyncio.run(s.main.ingest_from_export(s.main.ExportIngest(metric="FocusCost"), s.request))
    assert error.value.status_code == 404
    assert "offline" in error.value.detail
    # Points at the provisioning script rather than at the in-app button, which cannot work
    # on a deployment whose runtime identity is correctly denied roleAssignments/write.
    assert "focus-exports.ps1" in error.value.detail
    assert s.foreign == before
    assert not s.jobs
    s.reachable.assert_not_awaited()


def test_unselected_refresh_selects_owned_export_even_if_foreign_ranks_first(services):
    s = services
    response = asyncio.run(s.main.ingest_from_export(s.main.ExportIngest(), s.request))
    assert json.loads(response.body)["export"] == "owned-export"
    s.reachable.assert_awaited_once_with([s.owned])
    assert len(s.jobs) == 1
    assert s.jobs[0][1]["scope"] == ["sub-a"]


def test_explicitly_selected_external_export_remains_supported(services):
    s = services
    s.reachable.return_value = ([s.foreign], [])
    response = asyncio.run(s.main.ingest_from_export(
        s.main.ExportIngest(export_id="foreign-export", metric="FocusCost"), s.request,
    ))
    assert json.loads(response.body)["export"] == "foreign-export"
    s.reachable.assert_awaited_once_with([s.foreign])
    assert len(s.jobs) == 1
    assert s.jobs[0][1]["scope"] == ["sub-a"]


def test_unreachable_owned_storage_is_reported_not_replaced_with_foreign(services):
    s = services
    s.reachable.return_value = ([], [(s.owned, "403 Access denied")])
    s.load.return_value = {"loaded": [], "problems": []}
    asyncio.run(s.main._refresh_primary([{"id": "sub-a"}]))
    s.reachable.assert_awaited_once_with([s.owned])
    assert ("offline", "403 Access denied") in s.warehouse.state["focus_problems"]


def test_owned_discovery_does_not_replace_subscription_exports_with_billing_scopes(services, monkeypatch):
    s = services
    planning = AsyncMock(side_effect=AssertionError("Billing planning must not replace own schedules"))
    monkeypatch.setattr(s.exports, "resolve_scopes", planning)
    payload = [
        {
            "id": row["id"], "name": row["name"],
            "properties": {
                "definition": {"type": "FocusCost", "timeframe": "MonthToDate"},
                "deliveryInfo": {"destination": {
                    "resourceId": row["storage_resource_id"], "container": row["container"],
                    "rootFolderPath": row["root_folder"],
                }},
            },
        }
        for row in (s.foreign, s.owned)
    ]
    arm = AsyncMock(return_value={"value": payload})
    monkeypatch.setattr(s.exports.azure, "request", arm)
    result = asyncio.run(s.discover_real(["sub-a"], deployment_only=True))
    assert result["count"] == 1
    assert result["exports"][0]["id"] == "owned-export"
    assert result["strategy"] == "deployment-owned"
    assert result["scopes"] == ["/subscriptions/sub-a"]
    assert all("/subscriptions/sub-a/" in call.args[1] for call in arm.await_args_list)
    planning.assert_not_awaited()


def test_empty_owned_scope_cannot_expand_to_all_accessible_subscriptions(services, monkeypatch):
    s = services
    subscriptions = AsyncMock(side_effect=AssertionError("Empty scope must remain empty"))
    monkeypatch.setattr(s.exports, "list_subscriptions", subscriptions)
    arm = AsyncMock()
    monkeypatch.setattr(s.exports.azure, "request", arm)
    result = asyncio.run(s.discover_real([], deployment_only=True))
    assert result["exports"] == []
    assert result["scopes"] == []
    subscriptions.assert_not_awaited()
    arm.assert_not_awaited()
