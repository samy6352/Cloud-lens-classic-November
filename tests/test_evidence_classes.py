"""Evidence classes: what a finding claims, and what it is never allowed to claim.

Three separate rules are asserted here, because all three exist to stop the same thing --
a confident total nobody can defend.

  * `waste` classifies every rule as recoverable spend, spend-at-risk, or free inventory,
    and never adds the three together.
  * `tags` sinks Azure's own bookkeeping keys below keys a person chose, so a coverage
    ranking answers a governance question rather than an AKS one.
  * `anomalies` reports a run of consecutive bad days as one incident with a stable name,
    so the same event is not counted five times and is still recognisable tomorrow.
"""
from __future__ import annotations

# cloudlens-test: requires-offline
import asyncio
import sys
import types
import unittest
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import anomalies, savings, tags, waste


class WasteImpactClassTests(unittest.TestCase):
    def test_every_rule_is_classified(self):
        # A new rule with no class would silently default into the recoverable total,
        # which is the exact mistake the classification exists to prevent.
        self.assertEqual(set(waste.RULES), set(waste.IMPACT))
        self.assertEqual(set(waste.IMPACT.values()) - set(waste.IMPACT_LABELS), set())

    def test_free_inventory_is_never_counted_as_recoverable(self):
        for key in ("orphaned_nics", "unused_nsgs", "empty_resource_groups"):
            self.assertEqual(waste.IMPACT[key], "inventory", key)

    def test_retained_disks_behind_a_deallocated_vm_are_at_risk_not_savings(self):
        # Deleting the VM is not what stops the disk bill, so the money is real but
        # not recoverable by acting on the row as written.
        self.assertEqual(waste.IMPACT["deallocated_vms"], "cost_at_risk")
        self.assertEqual(waste.IMPACT["stopped_vms"], "potential_savings")

    def _run(self, rows_by_category, costs):
        async def arg(query, subscriptions, top=500):
            for key, rows in rows_by_category.items():
                if waste.RULES[key]["query"].strip()[:60] in query:
                    return rows
            return []

        with patch.object(waste, "_arg", side_effect=arg), \
                patch.object(waste, "_costs_for", return_value=costs):
            return asyncio.run(waste.find_waste(
                categories=list(rows_by_category), subscription_ids=["sub-a"]))

    def test_classes_are_totalled_apart_and_empty_categories_are_omitted(self):
        disk = {"id": "/subscriptions/sub-a/disks/orphan", "name": "orphan"}
        vm = {"id": "/subscriptions/sub-a/vm/deallocated", "name": "deallocated"}
        result = self._run(
            {"unattached_disks": [disk], "deallocated_vms": [vm], "unused_nsgs": []},
            {disk["id"]: {"cost": 40.0, "currency": "USD"},
             vm["id"]: {"cost": 60.0, "currency": "USD"}},
        )

        by_category = {f["category"]: f for f in result["findings"]}
        self.assertEqual(by_category["unattached_disks"]["impact_class"], "potential_savings")
        self.assertEqual(by_category["deallocated_vms"]["impact_class"], "cost_at_risk")

        # The two classes are reported apart, not as one 100.00 "waste" figure.
        self.assertEqual(result["by_impact"]["potential_savings"]["USD"], 40.0)
        self.assertEqual(result["by_impact"]["cost_at_risk"]["USD"], 60.0)

        # The empty category is named as checked, not rendered as a zero-count finding.
        self.assertNotIn("unused_nsgs", by_category)
        self.assertIn("unused_nsgs", result["checked_clear"])

    def test_items_carry_their_evidence_type(self):
        disk = {"id": "/subscriptions/sub-a/disks/orphan", "name": "orphan"}
        result = self._run({"unattached_disks": [disk]},
                           {disk["id"]: {"cost": 40.0, "currency": "USD"}})
        item = result["findings"][0]["items"][0]
        self.assertEqual(item["evidence_type"], "verified_cost")

    def test_unpriced_is_said_rather_than_shown_as_zero(self):
        disk = {"id": "/subscriptions/sub-a/disks/orphan", "name": "orphan"}
        result = self._run({"unattached_disks": [disk]}, {})
        self.assertEqual(result["findings"][0]["items"][0]["evidence_type"], "unpriced")

    def test_at_risk_spend_is_reported_beside_the_savings_total_not_inside_it(self):
        data = {"findings": [
            {"category": "unattached_disks", "title": "Unattached managed disks",
             "impact_class": "potential_savings", "count": 1, "cost": 40.0, "currency": "USD",
             "items": [{"id": "/subscriptions/sub-a/disks/orphan", "cost": 40.0}],
             "resource_ids": ["/subscriptions/sub-a/disks/orphan"]},
            {"category": "deallocated_vms", "title": "Deallocated VMs still holding disks",
             "impact_class": "cost_at_risk", "count": 1, "cost": 60.0, "currency": "USD",
             "items": [{"id": "/subscriptions/sub-a/vm/deallocated", "cost": 60.0}],
             "resource_ids": ["/subscriptions/sub-a/vm/deallocated"]},
            {"category": "unused_nsgs", "title": "Network security groups attached to nothing",
             "impact_class": "inventory", "count": 3, "cost": 0.0, "currency": "USD",
             "items": [{"id": "/subscriptions/sub-a/nsg/a", "cost": 0.0}],
             "resource_ids": ["/subscriptions/sub-a/nsg/a"]},
        ]}
        opportunities = savings._from_waste(data, savings._Index(), "USD")
        impacts = {o.impact_class for o in opportunities}

        # Free inventory never becomes a savings opportunity at all.
        self.assertNotIn("inventory", impacts)
        self.assertEqual(impacts, {"potential_savings", "cost_at_risk"})

    def test_the_class_never_lands_in_a_field_the_converter_treats_as_money(self):
        # `impact` is already a money key here, carrying Advisor's low/medium/high
        # severity. Putting "cost_at_risk" in it sent the string to convert_amount,
        # which returned None, which threw out the whole finding as an unconvertible
        # amount -- a classification silently deleting the thing it classifies.
        opp = savings.Opportunity("k", "orphaned", "t", impact_class="cost_at_risk")
        as_dict = opp.as_dict()
        self.assertEqual(as_dict["impact_class"], "cost_at_risk")
        self.assertNotIn("cost_at_risk", str(as_dict.get("impact", "")))


class PlatformTagTests(unittest.TestCase):
    def test_azure_injected_keys_are_recognised(self):
        for key in ("aks-managed-poolName", "k8s-azure-cluster", "hidden-link:/subscriptions/x",
                    "kubernetes.io-created-for"):
            self.assertTrue(tags.is_platform_key(key), key)

    def test_business_keys_are_not(self):
        for key in ("Environment", "CostCentre", "Owner", "Project"):
            self.assertFalse(tags.is_platform_key(key), key)

    def test_a_key_whose_values_are_all_machine_generated_is_platform_noise(self):
        # `virtualMachineProfileTimeCreated` is not on any prefix list, but every value
        # it ever carries is a timestamp Azure wrote.
        self.assertTrue(tags.is_platform_key(
            "virtualMachineProfileTimeCreated",
            ["2026-09-01T10:00:00", "2026-09-02T11:30:00"]))
        self.assertFalse(tags.is_platform_key("Owner", ["ana", "bob"]))

    def test_platform_keys_sink_below_business_keys_however_much_they_cost(self):
        # The point of the ranking is that it answers a governance question. An
        # `aks-managed-*` key rides on every node-pool disk, NIC and public IP in a
        # cluster, so on cost alone it out-ranks a real cost centre applied by people --
        # and the reader is left looking at four rows nobody can act on.
        resources = [
            {"id": "/r/1", "name": "nodepool-disk", "service": "Compute",
             "subscription": "sub", "subscription_id": "sub-a", "resource_group": "aks",
             "currency": "USD", "cost": 1000.0,
             "tags": '{"aks-managed-poolName":"nodepool1"}', "tags_available": True},
            {"id": "/r/2", "name": "ledger-db", "service": "SQL",
             "subscription": "sub", "subscription_id": "sub-a", "resource_group": "fin",
             "currency": "USD", "cost": 10.0,
             "tags": '{"CostCentre":"finance"}', "tags_available": True},
        ]

        class Reader:
            def rows(self, sql, limit=None):
                if 'max("ChargePeriodStart")' in sql:
                    return [{"hi": "2026-09-30"}]
                if "GROUP BY 1, 2, 3" in sql:
                    return [{"id": r["id"], "day": "2026-09-30",
                             "currency": "USD", "cost": r["cost"]} for r in resources]
                if "STRING_AGG" in sql:
                    return [{"subscription_id": "sub-a", "currencies": "USD"}]
                if "GROUP BY 1, 6" in sql:
                    return resources
                return [{"currency": "USD", "cost": 1010.0}]

        @contextmanager
        def reader(**_):
            yield Reader()

        # A stub module rather than the real one: importing `app.warehouse` constructs a
        # Warehouse at import time, which wants a PostgreSQL server. This test is about
        # how keys are ranked, and ranking needs no database.
        stub = types.ModuleType("app.warehouse")
        stub.warehouse = SimpleNamespace(reader=reader)
        with patch.dict(sys.modules, {"app.warehouse": stub}):
            payload = tags.cost_by_tag(days=30)

        self.assertEqual([k["key"] for k in payload["keys"]],
                         ["CostCentre", "aks-managed-poolName"])
        flags = {k["key"]: k["platform"] for k in payload["keys"]}
        self.assertTrue(flags["aks-managed-poolName"])
        self.assertFalse(flags["CostCentre"])
        self.assertEqual(payload["platform_keys"], 1)
        # Flagged and demoted, never deleted: an AKS audit still needs to see it, and a
        # coverage report that silently drops keys disagrees with the portal invisibly.
        self.assertEqual(len(payload["keys"]), 2)


class AnomalyIncidentTests(unittest.TestCase):
    def _day(self, day, delta, *, key="api", direction="up", severity="medium"):
        return {"day": day, "dimension": "service", "key": key, "currency": "USD",
                "direction": direction, "delta": delta, "cost": 100.0 + delta,
                "baseline": 100.0, "score": 5.0, "severity": severity}

    def test_a_run_of_days_is_one_incident(self):
        found = [self._day("2026-09-01", 50.0),
                 self._day("2026-09-02", 90.0),
                 self._day("2026-09-03", 40.0)]
        incidents = anomalies.collapse_incidents(found)
        self.assertEqual(len(incidents), 1)
        one = incidents[0]
        self.assertEqual((one["first_day"], one["last_day"]), ("2026-09-01", "2026-09-03"))
        self.assertEqual(one["duration_days"], 3)
        self.assertEqual(one["days"], 3)
        # Peak-day figures describe a day; the summed movement describes the run.
        self.assertEqual(one["peak_day"], "2026-09-02")
        self.assertEqual(one["peak_delta"], 90.0)
        self.assertEqual(one["total_delta"], 180.0)

    def test_a_gap_wider_than_the_tolerance_starts_a_new_incident(self):
        found = [self._day("2026-09-01", 50.0), self._day("2026-09-09", 50.0)]
        self.assertEqual(len(anomalies.collapse_incidents(found)), 2)

    def test_one_missing_day_inside_a_run_does_not_split_it(self):
        found = [self._day("2026-09-01", 50.0), self._day("2026-09-03", 50.0)]
        self.assertEqual(len(anomalies.collapse_incidents(found)), 1)

    def test_a_spike_and_a_drop_on_one_service_are_different_incidents(self):
        found = [self._day("2026-09-01", 50.0),
                 self._day("2026-09-02", -50.0, direction="down")]
        self.assertEqual(len(anomalies.collapse_incidents(found)), 2)

    def test_different_things_are_never_merged(self):
        found = [self._day("2026-09-01", 50.0, key="api"),
                 self._day("2026-09-01", 50.0, key="storage")]
        self.assertEqual(len(anomalies.collapse_incidents(found)), 2)

    def test_severity_is_the_worst_day_not_the_last(self):
        found = [self._day("2026-09-01", 50.0, severity="high"),
                 self._day("2026-09-02", 10.0, severity="low")]
        self.assertEqual(anomalies.collapse_incidents(found)[0]["severity"], "high")

    def test_the_id_is_stable_across_runs_and_specific_to_the_incident(self):
        found = [self._day("2026-09-01", 50.0), self._day("2026-09-02", 90.0)]
        first = anomalies.collapse_incidents(found)[0]["id"]
        # Same evidence, presented in a different order: same incident, same name.
        second = anomalies.collapse_incidents(list(reversed(found)))[0]["id"]
        self.assertEqual(first, second)

        other = anomalies.collapse_incidents(
            [self._day("2026-09-01", 50.0, key="storage")])[0]["id"]
        self.assertNotEqual(first, other)

    def test_retuning_the_detector_does_not_inherit_old_acknowledgements(self):
        a = anomalies.incident_id("service", "api", "USD", "up", "2026-09-01", "2026-09-02")
        b = anomalies.incident_id("service", "api", "USD", "up", "2026-09-01", "2026-09-03")
        self.assertNotEqual(a, b)


if __name__ == "__main__":
    unittest.main()
