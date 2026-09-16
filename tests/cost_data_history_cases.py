"""Import-horizon and selected-scope regressions shared by SQLite and PostgreSQL."""
import asyncio
import copy
import json
from contextlib import asynccontextmanager
from datetime import date
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException
from pydantic import ValidationError

from tests.cost_data_review_cases import environment


def history_boundaries_and_invalid_inputs(monkeypatch):
    p = environment(monkeypatch, "2024-03-01T02:00:00+00:00")
    assert p.service.history_window(history_days=1) == ("2024-03-01", "2024-03-01", 1)
    assert p.service.history_window(history_days=365) == ("2023-03-03", "2024-03-01", 365)
    assert p.service.history_window("2024-02-29", "2024-03-01", 2)[2] == 2
    for kwargs in [
        {"history_days": n} for n in (0, -1, 1.5, 366, True, "90")
    ] + [
        {"start": "2023-03-02", "end": "2024-03-01"},
        {"start": "2024-02-30", "end": "2024-03-01"},
        {"start": "20240229", "end": "2024-03-01"},
        {"start": "2024-03-01", "end": "2024-03-02"},
        {"start": "2024-03-01"}, {"start": "", "end": ""},
        {"start": "2024-03-01", "end": "2024-02-29"},
        {"start": "2024-02-29", "end": "2024-03-01", "history_days": 90},
    ]:
        with pytest.raises(ValueError):
            asyncio.run(p.service.preview([{"id": "a"}], **kwargs))
    p.sync.discover.assert_not_awaited()
    assert p.jobs.listing() == []
    # Internal reconciliation remains independently bounded at 366 inclusive days.
    assert p.flow.chunks("2023-03-02", "2024-03-01")


def maximum_import_is_durable_and_bound_to_approval(monkeypatch):
    p = environment(monkeypatch, "2024-03-01T02:00:00+00:00")
    async def run():
        plan = await p.service.preview([{"id": "a"}], history_days=365)
        smaller = await p.service.preview([{"id": "a"}], history_days=180)
        assert smaller["preview_token"] != plan["preview_token"]
        record = await p.service.create(plan, "maximum-import")
        assert record["payload"]["history_days"] == 365
        windows = [t for t in record["tasks"] if t.get("from")]
        assert windows[0]["from"] == "2023-03-03" and windows[-1]["to"] == "2024-03-01"
        assert sum((date.fromisoformat(t["to"]) - date.fromisoformat(t["from"])).days + 1 for t in windows) == 365
        assert (await p.service.create(plan, "maximum-import"))["job_id"] == record["job_id"]
        with pytest.raises(ValueError, match="different request"):
            await p.service.create(smaller, "maximum-import")
        forged = copy.deepcopy(plan)
        forged.update({"from": "2023-03-02", "history_days": 366})
        with pytest.raises(ValueError):
            await p.service.create(forged, "maximum-import")
        forged = copy.deepcopy(plan)
        forged["subscriptions"][0]["window"]["from"] = "2023-03-02"
        with pytest.raises(ValueError, match="windows"):
            await p.service.create(forged, "forged-window")
        assert len(p.jobs.listing()) == 1
    asyncio.run(run())
    p.schedules.run_now.assert_not_awaited()


def api_validation_precedes_azure_and_idempotent_retry(monkeypatch):
    p = environment(monkeypatch)
    from app import main
    user = SimpleNamespace(source="local", admin=True, name="Test", sid="session")
    request = SimpleNamespace(state=SimpleNamespace(user=user))
    resolve = AsyncMock(return_value=[{"id": "a"}])
    permission = AsyncMock()
    monkeypatch.setattr(main, "_cost_data_subscriptions", resolve)
    monkeypatch.setattr(main, "_assert_cost_data_approval", permission)
    for n in (0, -1, 1.5, True, 366, "365"):
        with pytest.raises(ValidationError):
            main.CostDataRequest(scope=["a"], history_days=n)
    async def run():
        for handler in (main.preview_cost_data, main.start_cost_data):
            for kwargs in (
                {"start": "2023-03-02", "end": "2024-03-01"},
                {"start": "2026-09-16", "end": "2026-09-16"},
                {"start": "malformed", "end": "2024-03-01"},
                {"start": "2024-02-29", "end": "2024-03-01", "history_days": 90},
            ):
                with pytest.raises(HTTPException) as exc:
                    await handler(main.CostDataRequest(scope=["a"], **kwargs), request)
                assert exc.value.status_code == 400
        resolve.assert_not_awaited()
        permission.assert_not_awaited()
        p.sync.discover.assert_not_awaited()
        body = main.CostDataRequest(scope=["a"], start="2024-02-29", end="2024-03-01")
        preview = json.loads((await main.preview_cost_data(body, request)).body)
        approved = body.model_copy(update={"preview_token": preview["preview_token"], "idempotency_key": "api"})
        started = json.loads((await main.start_cost_data(approved, request)).body)
        assert started["payload"]["history_days"] == 2
        assert json.loads((await main.start_cost_data(approved, request)).body)["job_id"] == started["job_id"]
        before = resolve.await_count
        invalid = approved.model_copy(update={"start": "2023-03-02"})
        with pytest.raises(HTTPException) as exc:
            await main.start_cost_data(invalid, request)
        assert exc.value.status_code == 400 and resolve.await_count == before
        changed = approved.model_copy(update={"start": "2024-02-28"})
        with pytest.raises(HTTPException) as exc:
            await main.start_cost_data(changed, request)
        assert exc.value.status_code == 409
        assert len(p.jobs.listing()) == 1
    asyncio.run(run())


def empty_and_revoked_scopes_never_expand(monkeypatch):
    p = environment(monkeypatch)
    from app import cost, main
    user = SimpleNamespace(source="local", admin=True, name="Test", sid="session")
    request = SimpleNamespace(state=SimpleNamespace(user=user))
    permission = AsyncMock(return_value=["a"])
    resolve = AsyncMock(return_value=[{"id": "a"}])
    monkeypatch.setattr(main, "permitted", permission)
    monkeypatch.setattr(cost, "_resolve", resolve)
    async def run():
        for handler in (main.preview_cost_data, main.start_cost_data):
            for scope in ([], None, [" "]):
                with pytest.raises(HTTPException) as exc:
                    await handler(main.CostDataRequest(scope=scope), request)
                assert exc.value.status_code == 400
        with pytest.raises(HTTPException) as exc:
            await main.cost_data_status(request, scope="")
        assert exc.value.status_code == 400
        permission.assert_not_awaited()
        with pytest.raises(HTTPException) as exc:
            await main.preview_cost_data(main.CostDataRequest(scope=["revoked"]), request)
        assert exc.value.status_code == 403
        resolve.assert_not_awaited()
        with pytest.raises(ValueError):
            await p.service.preview([])
        record = p.jobs.create("focus-setup", ["a"], [{"subscription": "a"}], identity="workload")
        p.jobs.update(record["job_id"], status="failed")
        before = p.jobs.get(record["job_id"])
        with pytest.raises(HTTPException) as exc:
            await main.resume_focus_job(record["job_id"], request, scope="other")
        assert exc.value.status_code == 409 and p.jobs.get(record["job_id"]) == before
        p.sync.discover.assert_not_awaited()
        assert not p.launched
    asyncio.run(run())


def entitled_catalog_is_read_only_and_not_header_filtered(monkeypatch):
    p = environment(monkeypatch)
    from app import backfill, cost, main
    user = SimpleNamespace(source="local", admin=True, name="Test", sid="session")
    request = SimpleNamespace(state=SimpleNamespace(user=user), query_params={"scope": "a"})
    monkeypatch.setattr(main, "permitted", AsyncMock(return_value=["a", "b"]))
    monkeypatch.setattr(cost, "list_subscriptions", AsyncMock(return_value={"subscriptions": [
        {"id": "a", "name": "Alpha"}, {"id": "b", "name": "Beta"}, {"id": "revoked", "name": "Revoked"},
    ]}))
    @asynccontextmanager
    async def as_user(user):
        yield
    monkeypatch.setattr(main, "as_user", as_user)
    def no_backfill(*args, **kwargs):
        raise AssertionError("Catalog reads must not create import work")
    monkeypatch.setattr(backfill, "schedule", no_backfill)
    data = json.loads(asyncio.run(main.cost_data_subscriptions(request)).body)
    assert [s["id"] for s in data["subscriptions"]] == ["a", "b"]
    assert not p.jobs.listing()
    p.sync.discover.assert_not_awaited()


CASES = [history_boundaries_and_invalid_inputs, maximum_import_is_durable_and_bound_to_approval,
         api_validation_precedes_azure_and_idempotent_retry, empty_and_revoked_scopes_never_expand,
         entitled_catalog_is_read_only_and_not_header_filtered]
