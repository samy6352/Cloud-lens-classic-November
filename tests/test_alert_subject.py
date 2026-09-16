"""Azure scheduled-action subject limits fail locally without changing approved text."""
import asyncio
from unittest.mock import AsyncMock

import pytest

from app import alerts


@pytest.mark.parametrize("subject", [None, ""])
def test_default_subject_is_within_azure_limit(subject):
    body = alerts.build_body(
        subscription_id="sub-a", emails=["test@example.com"],
        display_name="Daily anomaly", subject=subject)
    assert body["properties"]["notification"]["subject"] == "Cost anomaly detected"


@pytest.mark.parametrize("subject", ["CloudLens cost anomaly detected", "x" * 50])
def test_valid_subject_is_preserved_exactly(subject):
    body = alerts.build_body(
        subscription_id="sub-a", emails=["test@example.com"],
        display_name="Daily anomaly", subject=subject)
    assert body["properties"]["notification"]["subject"] == subject


@pytest.mark.parametrize("subject", ["x" * 51, "x" * 70, "x" * 100])
def test_long_subject_is_rejected_before_azure_write(monkeypatch, subject):
    request = AsyncMock()
    monkeypatch.setattr(alerts, "_request", request)
    with pytest.raises(alerts.AlertError, match="at most 50 characters"):
        asyncio.run(alerts.create(
            subscription_id="sub-a", emails=["test@example.com"], subject=subject))
    request.assert_not_awaited()


@pytest.mark.parametrize("subject", ["   ", 17, False, []])
def test_invalid_subject_has_a_user_facing_error(subject):
    with pytest.raises(alerts.AlertError, match="must contain text"):
        alerts.build_body(
            subscription_id="sub-a", emails=["test@example.com"],
            display_name="Daily anomaly", subject=subject)
