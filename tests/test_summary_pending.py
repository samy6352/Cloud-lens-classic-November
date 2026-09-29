"""A missing derived generation must not block or scan the detail table at boot."""
from __future__ import annotations

import ast
import asyncio
import json
import sys
from contextlib import contextmanager
from types import ModuleType, SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest

from app import cost

pytestmark = pytest.mark.offline


def test_warehouse_summary_returns_pending_after_an_exists_probe(monkeypatch):
    path = __import__("pathlib").Path(__file__).resolve().parents[1] / "app" / "warehouse.py"
    tree = ast.parse(path.read_text(encoding="utf-8"))
    warehouse_class = next(
        node for node in tree.body
        if isinstance(node, ast.ClassDef) and node.name == "Warehouse"
    )
    summary_method = next(
        node for node in warehouse_class.body
        if isinstance(node, ast.FunctionDef) and node.name == "summary"
    )
    namespace = {
        "Any": Any,
        "json": json,
        "rollup": SimpleNamespace(TABLE="costs_rollup",
                                  charges_sql=lambda _table: "count(*)"),
        "canonical_sub_id": lambda value: value,
    }
    exec(compile(ast.Module(body=[summary_method], type_ignores=[]),
                 "<warehouse-summary>", "exec"), namespace)
    queries = []

    class Reader:
        agg = "costs_rollup"
        _con = object()

        def rows(self, sql, limit=200):
            queries.append(sql)
            return [{"present": True}]

    class Warehouse:
        @contextmanager
        def reader(self, *_args, **_kwargs):
            yield Reader()

        @staticmethod
        def get_meta(_key):
            return None

        @staticmethod
        def _rollup_fresh(_con, _table):
            return False

    result = namespace["summary"](Warehouse())

    assert result["summary_pending"] is True
    assert result["rows"] is None
    assert result["subscriptions"] is None
    assert len(queries) == 1
    assert "EXISTS" in queries[0]
    assert "count(" not in queries[0].lower()


def test_overview_does_not_fall_back_to_live_cost_queries_while_summary_rebuilds(monkeypatch):
    warehouse = SimpleNamespace(summary=lambda: {
        "rows": None,
        "from": None,
        "to": None,
        "subscriptions": None,
        "currency": None,
        "mixed_currency": None,
        "summary_pending": True,
    })
    monkeypatch.setitem(sys.modules, "app.warehouse", ModuleType("app.warehouse"))
    sys.modules["app.warehouse"].warehouse = warehouse
    subscriptions = [{"id": "sub-a", "name": "Alpha"}]
    monkeypatch.setattr(
        cost, "list_subscriptions", AsyncMock(return_value={"subscriptions": subscriptions}))
    monkeypatch.setattr(
        cost, "_forecast_by_subscription", AsyncMock(return_value={"projected_total": 10}))
    monkeypatch.setattr(
        cost, "budgets", AsyncMock(return_value={
            "budgets": [], "count": 0, "over": 0,
            "subscriptions_requested": 1, "subscriptions_read": 1,
            "partial": False, "error": None, "unavailable_subscriptions": [],
        }))
    live_cost = AsyncMock(side_effect=AssertionError("raw/live fallback must stay deferred"))
    monkeypatch.setattr(cost, "cost_summary", live_cost)

    result = asyncio.run(cost.overview(["sub-a"]))

    assert result["month_to_date"] is None
    assert result["last_month"] is None
    assert "rebuilding" in result["currency_note"]
    live_cost.assert_not_awaited()
