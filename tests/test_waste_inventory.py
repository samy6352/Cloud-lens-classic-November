"""Inventory presence is not waste evidence; Resource Graph paging stays complete and valid."""
from __future__ import annotations

import asyncio
import sys
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import savings, waste


class WasteEvidenceTests(unittest.TestCase):
    def test_healthy_inventory_cannot_generate_whole_bill_savings(self):
        healthy = {
            "microsoft.network/virtualnetworkgateways": {
                "id": "/subscriptions/sub-a/gateways/healthy",
                "properties": {"gatewayType": "Vpn", "connections": ["active"]},
            },
            "microsoft.documentdb/databaseaccounts": {
                "id": "/subscriptions/sub-a/cosmos/healthy",
                "properties": {"capabilities": [], "locations": ["eastus"]},
            },
            "microsoft.storage/storageaccounts": {
                "id": "/subscriptions/sub-a/storage/healthy",
                "properties": {"provisioningState": "Succeeded"},
                "sku": {"name": "Premium_LRS"},
            },
        }

        async def inventory(query, subscriptions, top=500):
            return [row for resource_type, row in healthy.items() if resource_type in query.lower()]

        def costs(ids, days, **kwargs):
            return {rid: {"cost": 1000.0, "currency": "USD"} for rid in ids}

        with patch.object(waste, "_arg", side_effect=inventory), \
                patch.object(waste, "_costs_for", side_effect=costs), \
                patch.object(waste.azure, "request", side_effect=AssertionError("No Azure calls")):
            result = asyncio.run(waste.find_waste(subscription_ids=["sub-a"]))

        self.assertEqual(result["total_items"], 0)
        self.assertEqual(result["total_cost"], 0)
        self.assertIn("no savings are assumed", result["review_note"])
        for removed in ("idle_vpn_gateways", "idle_cosmos_accounts", "unused_storage_accounts"):
            self.assertNotIn(removed, waste.RULES)
        opportunities = savings._from_waste(result, savings._Index(), "USD")
        self.assertFalse(any(o.window for o in opportunities))
        self.assertFalse(any(o.items for o in opportunities))

    def test_verified_orphan_remains_priced(self):
        orphan = {"id": "/subscriptions/sub-a/disks/orphan", "name": "orphan"}
        with patch.object(waste, "_arg", new=AsyncMock(return_value=[orphan])), \
                patch.object(waste, "_costs_for", return_value={
                    orphan["id"]: {"cost": 25, "currency": "USD"},
                }):
            result = asyncio.run(waste.find_waste(
                categories=["unattached_disks"], subscription_ids=["sub-a"]))
        self.assertIn("properties.diskState) == 'Unattached'", waste.RULES["unattached_disks"]["query"])
        self.assertEqual(result["total_cost"], 25)
        self.assertFalse(result["partial"])

    def test_incomplete_rule_is_explicit(self):
        with patch.object(waste, "_arg", new=AsyncMock(
                side_effect=waste.CostError("Resource Graph results are incomplete"))), \
                patch.object(waste, "_costs_for", return_value={}):
            result = asyncio.run(waste.find_waste(
                categories=["unattached_disks"], subscription_ids=["sub-a"]))
        self.assertTrue(result["partial"])
        self.assertIn("incomplete", result["errors"]["unattached_disks"])


class ResourceGraphPagingTests(unittest.TestCase):
    def fetch(self, total, top=2000, **kwargs):
        calls = []

        async def request(method, url, *, json_body):
            self.assertEqual(method, "POST")
            self.assertTrue(url.startswith(waste.ARM + "/providers/Microsoft.ResourceGraph/"))
            self.assertEqual(json_body["subscriptions"], ["sub-a"])
            options = json_body["options"]
            calls.append(dict(options))
            size, skip = options["$top"], options["$skip"]
            self.assertGreater(size, 0)
            self.assertLessEqual(size, 1000)
            return {"data": [{"id": i} for i in range(skip, min(skip + size, total))],
                    "totalRecords": total}

        with patch.object(waste.azure, "request", side_effect=request):
            result = asyncio.run(waste._arg("Resources", ["sub-a"], top=top, **kwargs))
        return result, calls

    def test_top_2000_is_valid_and_fetches_beyond_first_thousand(self):
        rows, calls = self.fetch(2500)
        self.assertEqual([r["id"] for r in rows], list(range(2500)))
        self.assertEqual([c["$top"] for c in calls], [1000, 1000, 1000])
        self.assertEqual([c["$skip"] for c in calls], [0, 1000, 2000])

    def test_small_page_size_is_preserved(self):
        rows, calls = self.fetch(1200, top=500)
        self.assertEqual(len(rows), 1200)
        self.assertEqual([c["$top"] for c in calls], [500, 500, 500])

    def test_remaining_record_allowance_sizes_final_page(self):
        rows, calls = self.fetch(2500, max_records=2500)
        self.assertEqual(len(rows), 2500)
        self.assertEqual([c["$top"] for c in calls], [1000, 1000, 500])

    def test_larger_overall_limit_is_honored(self):
        rows, calls = self.fetch(6000, max_records=6000)
        self.assertEqual(len(rows), 6000)
        self.assertEqual(len(calls), 6)

    def test_record_cap_never_silently_returns_truncated_inventory(self):
        with self.assertRaisesRegex(waste.CostError, "5000-record.*incomplete"):
            self.fetch(5001)

    def test_page_cap_never_silently_returns_truncated_inventory(self):
        with self.assertRaisesRegex(waste.CostError, "2-page.*incomplete"):
            self.fetch(3000, max_pages=2)

    def test_continuation_token_is_used_even_for_short_page(self):
        request = AsyncMock(side_effect=[
            {"data": [{"id": 1}], "$skipToken": "opaque", "resultTruncated": "true"},
            {"data": [{"id": 2}], "resultTruncated": "false"},
        ])
        with patch.object(waste.azure, "request", new=request):
            rows = asyncio.run(waste._arg("Resources", ["sub-a"], top=2000))
        self.assertEqual(rows, [{"id": 1}, {"id": 2}])
        options = request.call_args_list[1].kwargs["json_body"]["options"]
        self.assertEqual(options, {"resultFormat": "objectArray", "$top": 1000, "$skipToken": "opaque"})

    def test_missing_total_does_not_stop_a_full_page(self):
        request = AsyncMock(side_effect=[
            {"data": [{"id": i} for i in range(1000)]},
            {"data": [{"id": 1000}]},
        ])
        with patch.object(waste.azure, "request", new=request):
            rows = asyncio.run(waste._arg("Resources", ["sub-a"], top=2000))
        self.assertEqual(len(rows), 1001)

    def test_empty_page_with_more_results_fails_explicitly(self):
        with patch.object(waste.azure, "request", new=AsyncMock(
                return_value={"data": [], "totalRecords": 10})), \
                self.assertRaisesRegex(waste.CostError, "no progress"):
            asyncio.run(waste._arg("Resources", ["sub-a"]))

    def test_upstream_error_is_not_replaced_with_partial_success(self):
        request = AsyncMock(side_effect=[
            {"data": [{"id": i} for i in range(1000)], "totalRecords": 2000},
            waste.CostError("upstream unavailable"),
        ])
        with patch.object(waste.azure, "request", new=request), \
                self.assertRaisesRegex(waste.CostError, "upstream unavailable"):
            asyncio.run(waste._arg("Resources", ["sub-a"], top=2000))

    def test_no_visible_subscription_is_one_plain_reason_not_sixteen_raw_400s(self):
        """Live Scale: a selection of only cost-data subscriptions made every rule log
        `Azure returned 400: {"error":...NoValidSubscriptionsInQueryRequest...}`."""
        raw = ('Azure returned 400: {"error":{"code":"BadRequest","message":"Please provide '
               'below info when asking for support: timestamp = 2026-10-08T11:25:35Z, '
               'correlationId = x.","details":[{"code":"NoValidSubscriptionsInQueryRequest",'
               '"message":"There must be at least one subscription that')
        request = AsyncMock(side_effect=waste.CostError(raw, status_code=400))
        with patch.object(waste.azure, "request", new=request), \
                self.assertRaises(waste.NoVisibleSubscriptions) as raised:
            asyncio.run(waste._arg("Resources", ["sub-a"]))
        self.assertNotIn("{", str(raised.exception))
        self.assertIn("can be read live", str(raised.exception))

        with patch.object(waste.azure, "request", new=request), \
                patch.object(waste, "_costs_for", side_effect=AssertionError("no cost read")), \
                self.assertRaises(waste.NoVisibleSubscriptions):
            asyncio.run(waste.find_waste(subscription_ids=["demo-only"]))

    def test_other_400s_are_still_reported_as_they_are(self):
        request = AsyncMock(side_effect=waste.CostError("Azure returned 400: bad query",
                                                        status_code=400))
        with patch.object(waste.azure, "request", new=request), \
                self.assertRaisesRegex(waste.CostError, "bad query") as raised:
            asyncio.run(waste._arg("Resources", ["sub-a"]))
        self.assertNotIsInstance(raised.exception, waste.NoVisibleSubscriptions)

    def test_no_visible_subscription_wins_over_rules_throttled_on_a_cold_start(self):
        """Live Scale, 8 Oct: after a restart some rules were throttled before Resource Graph
        could say the scope was invisible, so Savings never saw the access refusal."""
        invisible = waste.NoVisibleSubscriptions()
        throttled = waste.CostError("Azure is rate-limiting", status_code=429)
        calls = iter([invisible, throttled] * len(waste.RULES))

        async def arg(query, subs):
            raise next(calls)

        with patch.object(waste, "_arg", new=arg), \
                patch.object(waste, "_costs_for", side_effect=AssertionError("no cost read")), \
                self.assertRaises(waste.NoVisibleSubscriptions):
            asyncio.run(waste.find_waste(subscription_ids=["cost-only"]))

    def test_a_rule_that_succeeded_keeps_the_partial_report(self):
        results = iter([[]] + [waste.NoVisibleSubscriptions()] * (len(waste.RULES) - 1))

        async def arg(query, subs):
            res = next(results)
            if isinstance(res, Exception):
                raise res
            return res

        with patch.object(waste, "_arg", new=arg), \
                patch.object(waste, "_costs_for", return_value={}):
            report = asyncio.run(waste.find_waste(subscription_ids=["sub-a"]))
        self.assertTrue(report["partial"])
        self.assertEqual(len(report["errors"]), len(waste.RULES) - 1)


if __name__ == "__main__":
    unittest.main()
