"""Execute both AKS cost queries against isolated PostgreSQL, not a SQL-blind reader."""
from __future__ import annotations

import asyncio
import sys
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import aks, waste
from app.warehouse import COLUMNS, NUMERIC, insert_rows, warehouse


def cluster(name, sub, node_group):
    return {"name": name, "id": f"/subscriptions/{sub}/resourceGroups/clusters/{name}",
            "subscriptionId": sub, "nodeResourceGroup": node_group, "location": "eastus"}


class AksInventoryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        rows = []
        for sub, rg, amount in [
            ("sub-a", "production-nodes", 1000),
            ("sub-b", "production-nodes", 700),
            ("sub-a", "MC_legacy_old_eastus", 50),
            ("sub-b", "MC_legacy_old_eastus", 75),
            ("sub-a", "unrelated", 20),
        ]:
            values = {
                "ChargePeriodStart": "2026-09-01", "BilledCost": amount,
                "CostInUsd": amount, "BillingCurrency": "USD", "SubAccountId": sub,
                "ResourceGroup": rg, "ServiceName": "Virtual Machines", "RegionName": "eastus",
                "ResourceId": f"/subscriptions/{sub}/resourceGroups/{rg}/vm/node",
            }
            rows.append([values.get(c, 0 if c in NUMERIC else None) for c in COLUMNS])
        with warehouse.connect() as con:
            insert_rows(con, rows)

    def scan(self, live):
        with patch.object(aks, "_clusters", new=AsyncMock(return_value=live)), \
                patch.object(waste.azure, "request", side_effect=AssertionError("No Azure calls")):
            return asyncio.run(aks.clusters(["sub-a", "sub-b"], currency="USD"))

    def test_custom_node_cost_is_in_both_queries_without_other_subscription_leak(self):
        result = self.scan([cluster("production", "SUB-A", "PRODUCTION-NODES")])
        live = [c for c in result["clusters"] if c["live"]]
        self.assertEqual(len(live), 1)
        self.assertEqual(live[0]["cluster"], "production")
        self.assertEqual(live[0]["cost"], 1000)
        self.assertEqual(live[0]["by_category"], {"nodes": 1000})
        self.assertEqual(live[0]["subscriptions"], 1)
        self.assertEqual(result["currency"], "USD")
        self.assertEqual(result["total"], 1125)
        self.assertEqual(sum(d["cost"] for d in result["trend"]), 1125)
        self.assertEqual(result["count"], 3)

    def test_identical_node_group_names_are_attributed_per_subscription(self):
        result = self.scan([
            cluster("production-a", "sub-a", "production-nodes"),
            cluster("production-b", "sub-b", "production-nodes"),
        ])
        live = {c["cluster"]: c for c in result["clusters"] if c["live"]}
        self.assertEqual(live["production-a"]["cost"], 1000)
        self.assertEqual(live["production-b"]["cost"], 700)
        self.assertEqual(result["total"], 1825)
        self.assertEqual(sum(d["cost"] for d in result["trend"]), 1825)

    def test_live_mc_mapping_does_not_claim_other_sub_or_double_count(self):
        result = self.scan([cluster("legacy-live", "sub-a", "MC_legacy_old_eastus")])
        live = [c for c in result["clusters"] if c["live"]]
        historical = [c for c in result["clusters"] if not c["live"]]
        self.assertEqual(len(live), 1)
        self.assertEqual(live[0]["cost"], 50)
        self.assertEqual(len(historical), 1)
        self.assertEqual(historical[0]["cost"], 75)
        self.assertEqual(result["total"], 125)
        self.assertEqual(sum(d["cost"] for d in result["trend"]), 125)

    def test_no_inventory_keeps_historical_mc_groups(self):
        result = self.scan([])
        self.assertEqual(result["count"], 2)
        self.assertEqual(result["total"], 125)
        self.assertTrue(all(not c["live"] for c in result["clusters"]))


if __name__ == "__main__":
    unittest.main()
