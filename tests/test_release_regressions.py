"""Cross-contract failures identified during the integrated release review."""
import asyncio
from collections import OrderedDict
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import httpx
import pytest
from fastapi.responses import JSONResponse
from starlette.requests import Request

from app import cost, main, rates, snapshot_cache
from app.auth import User

pytestmark = pytest.mark.offline
SUB = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"


@pytest.mark.asyncio
async def test_delegated_discovery_uses_the_real_request_keyword_contract(monkeypatch):
    calls = []

    async def request(method, url, *, json_body=None, params=None, cache=True):
        calls.append((method, url, json_body, cache))
        if method == "GET":
            return {"value": [{"subscriptionId": SUB, "state": "Enabled", "displayName": "Example"}]}
        assert json_body["dataset"]["aggregation"]["totalCost"]["function"] == "Sum"
        return {"properties": {"rows": []}}

    monkeypatch.setattr(cost.azure, "request", request)
    previous = cost._caller.get()
    result = await cost.subscriptions_with("synthetic-delegated-credential")
    assert result[0]["id"] == SUB
    assert [call[0] for call in calls] == ["GET", "POST"]
    assert all(call[3] is False for call in calls)
    assert cost._caller.get() is previous


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [401, 429, 503])
async def test_discovery_does_not_report_transient_or_auth_failures_as_no_access(monkeypatch, status):
    async def request(method, _url, *, json_body=None, params=None, cache=True):
        if method == "GET":
            return {"value": [{"subscriptionId": SUB, "state": "Enabled"}]}
        raise cost.CostError("Request not completed", status_code=status)

    monkeypatch.setattr(cost.azure, "request", request)
    with pytest.raises(cost.CostError) as failure:
        await cost.subscriptions_with("synthetic")
    assert failure.value.status_code == status


@pytest.mark.asyncio
async def test_discovery_excludes_only_confirmed_denied_cost_access(monkeypatch):
    async def request(method, _url, *, json_body=None, params=None, cache=True):
        if method == "GET":
            return {"value": [{"subscriptionId": SUB, "state": "Enabled"}]}
        raise cost.CostError("Forbidden", status_code=403)

    monkeypatch.setattr(cost.azure, "request", request)
    assert await cost.subscriptions_with("synthetic") == []


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [400, 401, 403, 429, 503])
async def test_azure_http_errors_preserve_status_and_retry_metadata(monkeypatch, status):
    response = httpx.Response(
        status, headers={"Retry-After": "17"}, json={"error": {"message": "Synthetic"}},
        request=httpx.Request("POST", f"{cost.ARM}/subscriptions/{SUB}"),
    )

    class Client:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            pass

        async def request(self, *_args, **_kwargs):
            return response

    monkeypatch.setattr(cost.httpx, "AsyncClient", lambda **_kwargs: Client())
    client = cost.Azure()
    monkeypatch.setattr(client, "_bearer", AsyncMock(return_value="synthetic"))
    with pytest.raises(cost.CostError) as failure:
        await client._fetch("", "POST", f"{cost.ARM}/subscriptions/{SUB}", {}, None, 1)
    assert failure.value.status_code == status
    if status in {400, 429, 503}:
        assert failure.value.retry_after == "17"


def recommendation(annual=0):
    return {
        "problem": "Consider purchasing a savings plan to unlock lower prices",
        "term": "P1Y", "lookback": "30", "annualSavings": annual,
        "savings": annual / 12, "currency": "USD", "skuName": "Example",
    }


def test_zero_baseline_is_known_zero_without_dividing_by_zero():
    result = rates._comparison([recommendation()], [SUB], 0, 30, "USD", "USD")
    assert result["baseline"]["annual"] == result["baseline"]["monthly"] == 0
    assert result["models"][0]["annual_cost"] == 0
    assert result["options"][0]["annual_cost"] == 0
    assert result["options"][0]["monthly_cost"] == 0
    assert result["options"][0]["saving_pct"] is None


@pytest.mark.parametrize("baseline", [0, 100])
def test_incompatible_savings_do_not_create_a_negative_estimated_bill(baseline):
    result = rates._comparison([recommendation(120)], [SUB], baseline, 30, "USD", "USD")
    assert result["baseline"]["annual"] == baseline
    assert result["options"][0]["annual_cost"] is None
    assert result["options"][0]["saving_pct"] is None
    assert result["partial"] is True
    assert "not comparable" in result["verdict"]


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["billing", "savings"])
async def test_inner_scan_caches_partition_source_generation_and_fx_context(monkeypatch, kind):
    from app import billing_opportunities, savings

    amounts = iter([10, 20, 30])
    built = []

    async def build(*_args, **_kwargs):
        amount = next(amounts)
        built.append(amount)
        return {"total": amount, "scan": {
            "status": "ready", "completed": ["synthetic"], "total": 1, "errors": {}}}

    monkeypatch.setattr(main, "_billing_cache", OrderedDict())
    monkeypatch.setattr(main, "_billing_pending", {})
    monkeypatch.setattr(main, "_savings_cache", OrderedDict())
    monkeypatch.setattr(main, "_savings_pending", {})
    monkeypatch.setattr(billing_opportunities, "build", build)
    monkeypatch.setattr(savings, "build", build)
    monkeypatch.setattr(savings, "assemble", lambda *_args: {"total": 0})
    user = User("synthetic", admin=True, source="file")
    values = []
    for context in [("fx-a", "data:1"), ("fx-a", "data:2"), ("fx-b", "data:2"), ("fx-b", "data:2")]:
        token = snapshot_cache.calculation_context.set(context)
        try:
            if kind == "billing":
                result = await main._get_billing_opportunities(user, [SUB], "USD")
            else:
                state, pending = main._start_savings(user, [SUB], 30, "USD")
                if pending is not None:
                    await pending
                result = state.result
            values.append(result["total"])
            await asyncio.sleep(0)
        finally:
            snapshot_cache.calculation_context.reset(token)
    assert built == [10, 20, 30]
    assert values == [10, 20, 30, 30]


@pytest.mark.asyncio
async def test_snapshot_factory_propagates_and_restores_inner_calculation_context():
    context = snapshot_cache.CacheContext("person", "tenant", (SUB,), (SUB,))
    request = Request({"type": "http", "method": "GET", "path": "/api/dashboard/executive",
                       "query_string": b"", "headers": [], "scheme": "https",
                       "server": ("example.invalid", 443)})
    seen = []

    async def origin():
        seen.append(snapshot_cache.calculation_context.get())
        return JSONResponse({"total": 10})

    async def authorize(_request):
        return context

    store = SimpleNamespace(max_payload=10000, put=lambda _item: True)
    cache = snapshot_cache.SnapshotCache(store, version="code-version")
    result = await cache._refresh(request, origin, authorize, context, "context-key", "data:2",
                                  snapshot_cache.WAREHOUSE, None)
    assert result.generation == "data:2"
    assert seen == [("context-key", "data:2")]
    assert snapshot_cache.calculation_context.get() is None
    await cache.close()


@pytest.mark.asyncio
async def test_import_queue_keeps_the_pre_submission_cutoff(monkeypatch):
    from app import focus_sync, focus_workflow, jobs

    cutoff = "2026-09-14T17:00:00+00:00"
    submitted = "2026-09-14T17:00:01+00:00"
    record = {"job_id": "synthetic-job", "created_at": "2026-09-14T17:00:30+00:00",
              "tasks": [{"task_id": "synthetic-task"}]}
    plan = {"subscriptions": [{"export": {"timeframe": "MonthToDate"}}]}
    monkeypatch.setattr(focus_sync, "record", AsyncMock())
    monkeypatch.setattr(focus_workflow, "preview", AsyncMock(return_value=plan))
    monkeypatch.setattr(focus_workflow, "create", AsyncMock(return_value=record))
    monkeypatch.setattr(focus_workflow, "launch", Mock())
    update = Mock()
    monkeypatch.setattr(jobs, "update", update)
    monkeypatch.setattr(jobs, "get", lambda _job_id: record)
    assert await main._queue_focus_import(
        User("synthetic", admin=True, source="file"), SUB, [], requested_at=cutoff)
    assert update.call_args.kwargs["requested_at"] == cutoff
    assert focus_workflow.submitted_after(SimpleNamespace(submitted=submitted), cutoff)
    assert not focus_workflow.submitted_after(SimpleNamespace(submitted=submitted), record["created_at"])
