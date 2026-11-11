import asyncio
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException

from app import advisor_score as scores
from app.cost import CostError

SUB = "616dc9b8-b4aa-415f-8dcb-71bc462916c5"
OTHER = "11111111-1111-4111-8111-111111111111"
DATE = "2026-11-10T00:00:00Z"


def payload(value=80):
    return {"value": [{"name": name, "properties": {"lastRefreshedScore": {
        "score": value, "date": DATE}}} for name in (*scores.CATEGORIES, "Advisor")]}


def test_five_official_categories_ignore_recommendations():
    body = payload(0)
    body["value"].append({"name": "recommendation-guid", "properties": {
        "lastRefreshedScore": {"score": 99, "date": DATE}}})
    result = scores.parse_scores(body)
    assert len(result) == 6
    assert all(row["score"] == 0 for row in result.values())
    assert result["HighAvailability"]["status"] == "available"


@pytest.mark.parametrize("value", [None, True, -1, 101, float("nan"), float("inf"), "90"])
def test_missing_invalid_scores_are_not_zero(value):
    assert scores.parse_scores(payload(value))["Cost"]["score"] is None


def test_no_denominator_or_weights_are_invented():
    # ARM supplies a percentage, not numerator/maximum. Missing weights cannot justify averaging.
    result = scores.parse_scores(payload(75))
    assert result["Cost"]["score"] == 75
    assert scores.parse_scores({})["Cost"]["status"] == "unavailable"
    body = payload()
    body["value"][0]["properties"]["lastRefreshedScore"]["date"] = None
    assert scores.parse_scores(body)["Cost"]["score"] is None


def test_partial_permission_and_unsupported(monkeypatch):
    async def request(method, url, **kwargs):
        assert kwargs["cache"] is False
        assert kwargs["params"] == {"api-version": "2023-01-01"}
        if OTHER in url:
            raise CostError("denied", status_code=403)
        body = payload()
        body["value"] = body["value"][1:]
        return body

    monkeypatch.setattr(scores.azure, "request", request)
    data = asyncio.run(scores.read_scores([SUB, OTHER]))
    assert data["aggregation"] == "none"
    assert data["subscriptions"][0]["status"] == "partial"
    assert data["subscriptions"][0]["scores"]["Cost"]["score"] is None
    assert data["subscriptions"][1]["status"] == "forbidden"
    for status, expected in [(404, "unsupported"), (500, "error")]:
        monkeypatch.setattr(scores.azure, "request",
                            AsyncMock(side_effect=CostError("failure", status_code=status)))
        assert asyncio.run(scores.read_scores([SUB]))["subscriptions"][0]["status"] == expected


def test_endpoint_live_scope_and_context(monkeypatch):
    from app import main
    user = object()
    entered = []

    @asynccontextmanager
    async def as_user(value):
        assert value is user
        entered.append(True)
        yield

    monkeypatch.setattr(main, "current_user", lambda request: user)
    monkeypatch.setattr(main, "permitted", AsyncMock(return_value=[SUB]))
    monkeypatch.setattr(main, "as_user", as_user)
    reader = AsyncMock(return_value={"subscriptions": []})
    monkeypatch.setattr(scores, "read_scores", reader)
    response = asyncio.run(main.dashboard_advisor_score(None, scope=SUB))
    assert response.status_code == 200
    assert response.headers["cache-control"] == "private, no-store"
    reader.assert_awaited_once_with([SUB])
    assert entered
    with pytest.raises(HTTPException) as exc:
        asyncio.run(main.dashboard_advisor_score(None, scope=OTHER))
    assert exc.value.status_code == 403
    # An empty live entitlement must never fall back to app identity or an export grant.
    monkeypatch.setattr(main, "permitted", AsyncMock(return_value=[]))
    with pytest.raises(HTTPException) as exc:
        asyncio.run(main.dashboard_advisor_score(None))
    assert exc.value.status_code == 403
