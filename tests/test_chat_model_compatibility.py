import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from openai import BadRequestError

from app.agent import CostAgent
from app.model_compatibility import create_completion
from app.recommendations import synthesize


def rejected(body):
    return BadRequestError(
        "Request rejected",
        response=httpx.Response(400, request=httpx.Request("POST", "https://example.test")),
        body=body,
    )


@pytest.mark.parametrize("nested", [False, True])
@pytest.mark.parametrize("code", ["unsupported_value", "unsupported_parameter"])
def test_temperature_rejection_retries_with_model_default(monkeypatch, nested, code):
    monkeypatch.setenv("PROJECT_ENDPOINT", "https://example.test")
    monkeypatch.setenv("MODEL_DEPLOYMENT_NAME", "arbitrary-customer-alias")
    error = {"param": "temperature", "code": code}
    result = object()
    create = AsyncMock(side_effect=[rejected({"error": error} if nested else error), result])
    client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
    agent = CostAgent()
    messages = [{"role": "user", "content": "Hello"}]

    assert asyncio.run(agent._chat_stream(client, messages)) is result
    first, second = [call.kwargs for call in create.await_args_list]
    assert first["temperature"] == 0.1
    assert second == {key: value for key, value in first.items() if key != "temperature"}
    assert first["stream"] is True
    assert first["model"] == "arbitrary-customer-alias"
    assert first["messages"] == messages
    assert all(tool["function"]["name"] != "create_budget" for tool in first["tools"])


def test_supported_temperature_is_unchanged(monkeypatch):
    monkeypatch.setenv("PROJECT_ENDPOINT", "https://example.test")
    create = AsyncMock(return_value="stream")
    client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
    assert asyncio.run(CostAgent()._chat_stream(client, [])) == "stream"
    create.assert_awaited_once()
    assert create.await_args.kwargs["temperature"] == 0.1


@pytest.mark.parametrize("body", [
    {"param": "messages", "code": "unsupported_value"},
    {"param": "temperature", "code": "invalid_request_error"},
    {"error": "invalid"},
    None,
])
def test_unrelated_errors_are_not_retried(monkeypatch, body):
    monkeypatch.setenv("PROJECT_ENDPOINT", "https://example.test")
    create = AsyncMock(side_effect=rejected(body))
    client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
    with pytest.raises(BadRequestError):
        asyncio.run(CostAgent()._chat_stream(client, []))
    create.assert_awaited_once()


def test_failed_fallback_is_not_retried_again(monkeypatch):
    monkeypatch.setenv("PROJECT_ENDPOINT", "https://example.test")
    failure = rejected({"param": "temperature", "code": "unsupported_value"})
    create = AsyncMock(side_effect=[failure, failure])
    client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
    with pytest.raises(BadRequestError):
        asyncio.run(CostAgent()._chat_stream(client, []))
    assert create.await_count == 2


@pytest.mark.parametrize("parameters", [
    ["temperature", "max_tokens"], ["max_tokens", "temperature"],
])
def test_recommendations_adapt_both_rejected_parameters(parameters):
    response = SimpleNamespace(choices=[
        SimpleNamespace(message=SimpleNamespace(content="Review the idle VM.")),
    ])
    errors = [rejected({"param": parameter, "code": "unsupported_parameter"})
              for parameter in parameters]
    create = AsyncMock(side_effect=[*errors, response])
    client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
    agent = SimpleNamespace(model="customer-alias", _openai=AsyncMock(return_value=client))
    result = asyncio.run(synthesize(
        agent, savings={"opportunities": [{"title": "idle VM"}]}, anomalies=None, currency="USD",
    ))
    assert result["narrative"] == "Review the idle VM."
    assert create.await_count == 3
    final = create.await_args.kwargs
    assert final["max_completion_tokens"] == 400
    assert "temperature" not in final and "max_tokens" not in final


def test_compatibility_does_not_retry_transport_failure():
    create = AsyncMock(side_effect=RuntimeError("transport failure"))
    client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
    with pytest.raises(RuntimeError, match="transport failure"):
        asyncio.run(create_completion(client, model="alias", temperature=0.1))
    create.assert_awaited_once()
