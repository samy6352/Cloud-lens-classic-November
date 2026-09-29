"""Explicit behavior for a PostgreSQL demo-cost dataset."""
from __future__ import annotations

import os
from typing import Any


def enabled() -> bool:
    value = os.getenv("CLOUDLENS_DEMO_MODE", "false").strip().lower()
    if value not in {"true", "false"}:
        raise ValueError("CLOUDLENS_DEMO_MODE must be true or false")
    return value == "true"


def budget_note() -> str:
    return (
        "Budgets are not part of the PostgreSQL demo data. Live Azure budget reads and "
        "writes are disabled in demo mode."
    )


def budgets_payload() -> dict[str, Any]:
    return {
        "budgets": [],
        "count": 0,
        "over": 0,
        "near": 0,
        "subscriptions_requested": 0,
        "subscriptions_read": 0,
        "unavailable_subscriptions": [],
        "partial": False,
        "error": None,
        "note": budget_note(),
        "demo_mode": True,
    }
