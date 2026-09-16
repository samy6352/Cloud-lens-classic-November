"""Offline regressions for incomplete budget reads and exact filter interpretation."""
from __future__ import annotations

import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.budgets import exact_tag_filter


class BudgetFilters(unittest.TestCase):
    def test_supported_filters(self):
        self.assertEqual(exact_tag_filter(None), [])
        self.assertEqual(exact_tag_filter({}), [])
        self.assertEqual(exact_tag_filter({"tags": {"name": "env", "values": ["prod", "", "prod"]}}),
                         [("env", ["prod", ""])])
        self.assertEqual(exact_tag_filter({"and": [
            {"tags": {"name": "env", "operator": "In", "values": ["prod"]}},
            {"and": [{"tags": {"name": "owner", "values": ["team"]}}]},
        ]}), [("env", ["prod"]), ("owner", ["team"])])

    def test_unsupported_filters_are_not_partially_applied(self):
        tag = {"tags": {"name": "env", "operator": "In", "values": ["prod"]}}
        dimension = {"dimensions": {"name": "ResourceGroupName", "values": ["rg"]}}
        for node in (
            dimension, {"and": [tag, dimension]}, {"or": [tag, tag]},
            {"and": [tag, {}]}, {"tags": tag["tags"], "dimensions": dimension["dimensions"]},
            {"tags": {"name": "env", "operator": "NotIn", "values": ["prod"]}},
            {"tags": {"name": "env", "values": []}},
            {"tags": {"name": "env", "values": "prod"}},
            {"tags": {"name": "env", "values": [None]}},
            {"and": []}, {"and": "bad"}, "bad",
            {"and": [tag, {"tags": {"name": "ENV", "values": ["dev"]}}]},
        ):
            with self.subTest(node=node):
                self.assertIsNone(exact_tag_filter(node))


class BudgetReads(unittest.IsolatedAsyncioTestCase):
    async def test_partial_and_failed_reads_do_not_claim_no_budgets(self):
        from app import cost

        subs = [{"id": "a", "name": "A"}, {"id": "b", "name": "B"}]
        listing = {"value": [{"name": "demo", "properties": {
            "amount": 100, "currentSpend": {"amount": 0, "unit": "USD"},
        }}]}
        for first in (listing, {"value": []}, cost.CostError("unavailable")):
            with self.subTest(first=first), patch.object(cost, "_resolve", AsyncMock(return_value=subs)), \
                    patch.object(cost.azure, "request", AsyncMock(side_effect=[first, cost.CostError("denied")])):
                result = await cost.budgets(["a", "b"])
            self.assertTrue(result["partial"])
            self.assertIsNotNone(result["error"])
            self.assertIsNone(result["note"])
            self.assertEqual(result["subscriptions_requested"], 2)
            self.assertEqual(result["unavailable_subscriptions"][-1], subs[-1])
            if result["budgets"]:
                self.assertEqual(result["budgets"][0]["current_spend"], 0)
                self.assertEqual(result["budgets"][0]["status"], "ok")

    async def test_successful_empty_read_is_distinct(self):
        from app import cost

        with patch.object(cost, "_resolve", AsyncMock(return_value=[{"id": "a", "name": "A"}])), \
                patch.object(cost.azure, "request", AsyncMock(return_value={"value": []})):
            result = await cost.budgets(["a"])
        self.assertFalse(result["partial"])
        self.assertIsNone(result["error"])
        self.assertEqual(result["subscriptions_read"], 1)
        self.assertIn("No budgets", result["note"])

    async def test_explicit_refresh_bypasses_azure_cache(self):
        from app import cost

        with patch.object(cost, "_resolve", AsyncMock(return_value=[{"id": "a", "name": "A"}])), \
                patch.object(cost.azure, "request", AsyncMock(return_value={"value": []})) as request:
            await cost.budgets(["a"], refresh=True)
            self.assertFalse(request.call_args.kwargs["cache"])
            await cost.budgets(["a"])
            self.assertTrue(request.call_args.kwargs["cache"])

    async def test_unsupported_trend_never_queries_broader_costs(self):
        from app import main

        async def inline(fn, *args, **kwargs):
            return fn(*args, **kwargs)

        mixed = {"and": [
            {"tags": {"name": "env", "values": ["prod"]}},
            {"dimensions": {"name": "ResourceGroupName", "values": ["rg"]}},
        ]}
        rows = [
            {"name": "mixed", "subscription_id": "a", "filter_raw": mixed, "filter": "env AND rg"},
            {"name": "legacy", "subscription_id": "a", "filter": "unknown filter"},
            {"name": "tagged", "subscription_id": "a",
             "filter_raw": {"tags": {"name": "env", "values": ["prod"]}}, "filter": "env"},
        ]
        query = Mock(return_value={"labels": ["2026-09-01", "2026-09-02"]})
        module = SimpleNamespace(warehouse=SimpleNamespace(budget_trend=query))
        with patch.object(main, "offload", inline), patch.dict(sys.modules, {"app.warehouse": module}):
            await main._attach_budget_trends({"budgets": rows})
        self.assertEqual(query.call_count, 1)
        self.assertEqual(query.call_args.kwargs["tags"], [("env", ["prod"])])
        for row in rows[:2]:
            self.assertFalse(row["filter_exact"])
            self.assertEqual(row["tag_filter"], [])
            self.assertIsNone(row["trend"])
            self.assertIn("no approximate trend", row["trend_note"])
        self.assertTrue(rows[2]["filter_exact"])
        self.assertTrue(rows[2]["trend"]["filtered_exactly"])


if __name__ == "__main__":
    unittest.main()
