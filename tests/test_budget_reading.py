"""Offline regressions for incomplete budget reads and exact filter interpretation."""
from __future__ import annotations

import json
import os
import sys
import unittest
from contextlib import asynccontextmanager
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
    async def test_demo_mode_returns_no_budgets_without_resolving_or_querying_azure(self):
        from app import cost

        with patch.dict(os.environ, {"CLOUDLENS_DEMO_MODE": "true"}), \
                patch.object(cost, "_resolve", AsyncMock()) as resolve, \
                patch.object(cost.azure, "request", AsyncMock()) as request:
            result = await cost.budgets(["sub-a"])

        self.assertEqual(result["budgets"], [])
        self.assertTrue(result["demo_mode"])
        self.assertIn("PostgreSQL demo data", result["note"])
        resolve.assert_not_awaited()
        request.assert_not_awaited()

    async def test_demo_budget_routes_skip_authorization_and_azure(self):
        from fastapi import HTTPException

        from app import cost, main

        with patch.dict(os.environ, {"CLOUDLENS_DEMO_MODE": "true"}), \
                patch.object(main, "current_user", side_effect=AssertionError("user lookup")), \
                patch.object(main, "permitted", side_effect=AssertionError("Azure scope lookup")), \
                patch.object(cost, "budgets", AsyncMock()) as lookup:
            response = await main.list_budgets(None)
            result = json.loads(response.body)
            self.assertTrue(result["demo_mode"])
            self.assertEqual(result["budgets"], [])
            lookup.assert_not_awaited()

            trend = await main.get_budget_trend(None, "sub-a", "demo")
            self.assertTrue(json.loads(trend.body)["demo_mode"])
            with self.assertRaises(HTTPException) as raised:
                await main.create_budget(None, None)

        self.assertEqual(raised.exception.status_code, 409)
        lookup.assert_not_awaited()

    async def test_budget_list_returns_filter_metadata_without_loading_trends(self):
        from app import cost, main

        user = SimpleNamespace(admin=True, source="local", name="test", sid="test")
        budget = {
            "name": "demo", "subscription_id": "sub-a", "filter": "env = prod",
            "filter_raw": {"tags": {"name": "env", "values": ["prod"]}},
        }
        payload = {"budgets": [budget], "count": 1}

        @asynccontextmanager
        async def acting_as(_user):
            yield

        with patch.object(main, "current_user", return_value=user), \
                patch.object(main, "permitted", AsyncMock(return_value=["sub-a"])), \
                patch.object(main, "narrow", return_value=["sub-a"]), \
                patch.object(main, "as_user", acting_as), \
                patch.object(cost, "budgets", AsyncMock(return_value=payload)), \
                patch.object(main, "_attach_budget_trends", AsyncMock()) as attach:
            response = await main.list_budgets(
                SimpleNamespace(state=SimpleNamespace()), scope="sub-a")

        result = json.loads(response.body)
        self.assertTrue(result["budgets"][0]["filter_exact"])
        self.assertEqual(result["budgets"][0]["tag_filter"], [["env", ["prod"]]])
        self.assertNotIn("trend", result["budgets"][0])
        attach.assert_not_awaited()

    async def test_single_budget_trend_uses_only_the_authorized_exact_budget(self):
        from app import cost, main

        user = SimpleNamespace(admin=True, source="local", name="test", sid="test")
        budget = {
            "name": "demo", "subscription_id": "sub-a", "time_grain": "Monthly",
            "start": "2026-09-01", "end": "2026-10-01", "filter": "env = prod",
            "filter_raw": {"tags": {"name": "env", "values": ["prod"]}},
        }
        query = Mock(return_value={
            "labels": ["2026-09-01", "2026-09-02"],
            "values": [1, 2], "cumulative": [1, 3], "total": 3,
        })
        warehouse_module = SimpleNamespace(warehouse=SimpleNamespace(budget_trend=query))

        @asynccontextmanager
        async def acting_as(_user):
            yield

        async def inline(fn, *args, **kwargs):
            return fn(*args, **kwargs)

        # The period ends today, so pin today: the expected dates must not drift daily.
        class FrozenDateTime(main.datetime):
            @classmethod
            def now(cls, tz=None):
                return main.datetime(2026, 9, 29, 12, 0, tzinfo=tz)

        with patch.object(main, "current_user", return_value=user), \
                patch.object(main, "datetime", FrozenDateTime), \
                patch.object(main, "permitted", AsyncMock(return_value=["sub-a"])), \
                patch.object(main, "as_user", acting_as), \
                patch.object(cost, "budgets", AsyncMock(return_value={"budgets": [budget]})), \
                patch.object(main, "offload", inline), \
                patch.dict(sys.modules, {"app.warehouse": warehouse_module}):
            response = await main.get_budget_trend(
                SimpleNamespace(state=SimpleNamespace()), "sub-a", "demo")

        result = json.loads(response.body)
        self.assertTrue(result["filter_exact"])
        self.assertEqual(result["tag_filter"], [["env", ["prod"]]])
        self.assertTrue(result["trend"]["filtered_exactly"])
        self.assertEqual(query.call_args.args[:3], ("sub-a", "2026-09-01", "2026-09-29"))
        self.assertEqual(query.call_args.kwargs["tags"], [("env", ["prod"])])

    async def test_single_budget_trend_rejects_out_of_scope_subscription(self):
        from fastapi import HTTPException

        from app import cost, main

        user = SimpleNamespace(admin=False, source="entra", name="test", sid="test")
        with patch.object(main, "current_user", return_value=user), \
                patch.object(main, "permitted", AsyncMock(return_value=["other-sub"])), \
                patch.object(cost, "budgets", AsyncMock()) as read, \
                self.assertRaises(HTTPException) as raised:
            await main.get_budget_trend(
                SimpleNamespace(state=SimpleNamespace()), "sub-a", "demo")

        self.assertEqual(raised.exception.status_code, 403)
        read.assert_not_awaited()

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
