"""Hermetic release regressions: atomic exports, currency arithmetic and history migration."""
from __future__ import annotations

import asyncio
import copy
import csv
import io
import os
import unittest
import uuid
from datetime import date
from unittest.mock import AsyncMock, patch

assert os.environ.get("PGSCHEMA", "").startswith("t_cl_test_release_data_safety_")

from app import currency as fx
from app import exports, savings, store, whatif
from app.scopes import Manifest
from app.warehouse import Warehouse, insert_rows, warehouse

DAY = date.today().isoformat()
RID = "/subscriptions/sub-a/resourceGroups/rg/providers/Microsoft.Compute/virtualMachines/vm"


def csv_data(rows):
    out = io.StringIO()
    writer = csv.DictWriter(out, fieldnames=list(dict.fromkeys(k for row in rows for k in row)))
    writer.writeheader()
    writer.writerows(rows)
    return out.getvalue().encode()


def charge(sub="sub-a", cost=8000, currency="INR", **extra):
    return {"ChargePeriodStart": DAY, "BilledCost": cost, "BillingCurrency": currency,
            "SubAccountId": sub, "ResourceId": RID, "ServiceName": "Virtual Machines",
            "ServiceFamily": "Compute", **extra}


def records(rows):
    header, values = exports._rows_from(csv_data(rows))
    return exports._load_rows(header, values, None)[0]


class Reader:
    account = "offline"
    container = "exports"

    def __init__(self, blobs):
        self.blobs = blobs
        self.closed = False
        self.downloads = []

    async def list(self, prefix=""):
        return [{"name": name, "size": len(value), "modified": "2026-09-01T01:00:00Z"}
                for name, value in self.blobs.items()]

    async def get(self, name):
        self.downloads.append(name)
        return self.blobs[name]

    async def close(self):
        self.closed = True


def manifest(names, count=None):
    return Manifest.parse({
        "dataRowCount": len(names) if count is None else count,
        "runInfo": {"runId": "run", "submittedTime": "2026-09-01T01:00:00Z",
                    "startDate": DAY, "endDate": DAY},
        "exportConfig": {"type": "FocusCost"},
        "blobs": [{"blobName": name, "dataRowCount": 1} for name in names],
    })


class ExportSafety(unittest.TestCase):
    def setUp(self):
        with warehouse.connect() as con:
            con.execute("DELETE FROM costs")
            con.execute("DELETE FROM ingest_log")
            insert_rows(con, records([charge("sub-a", 123), charge("sub-b", 456)]))
        warehouse.refresh_rollup()
        self.before = self.snapshot()

    def snapshot(self):
        with warehouse.connect() as con:
            return (
                con.execute('SELECT * FROM costs ORDER BY "SubAccountId"').fetchall(),
                con.execute("SELECT * FROM ingest_log").fetchall(),
                con.execute('SELECT * FROM costs_rollup ORDER BY "SubAccountId"').fetchall(),
            )

    def ingest(self, rows=None, manifests=None, blobs=None, **kwargs):
        reader = Reader(blobs or {"run/part.csv": csv_data(rows)})
        with patch.object(exports, "BlobReader", return_value=reader), \
                patch.object(exports, "read_manifests", AsyncMock(return_value=manifests or [])):
            try:
                return asyncio.run(exports.ingest_export(account="offline", container="exports",
                                                        **kwargs))
            finally:
                self.assertTrue(reader.closed)

    def test_all_or_some_missing_subscriptions_never_replace(self):
        for rows in ([charge("")], [charge("sub-a"), charge("")], [charge(" ")]):
            with self.subTest(rows=rows), self.assertRaisesRegex(exports.ExportError, "SubAccountId"):
                self.ingest(rows)
            self.assertEqual(self.snapshot(), self.before)
            self.assertEqual(warehouse.state["status"], "failed")

    def test_authoritative_fallback_replaces_only_its_subscription(self):
        result = self.ingest([charge("")], subscription_id="sub-a")
        self.assertEqual(result["subscriptions"], 1)
        with warehouse.connect() as con:
            self.assertEqual(con.execute(
                'SELECT "SubAccountId", "BilledCost" FROM costs ORDER BY "SubAccountId"'
            ).fetchall(), [("sub-a", 8000.0), ("sub-b", 456.0)])

    def test_selected_scope_filters_complete_export_without_deleting_other_history(self):
        result = self.ingest([charge("sub-a", 80), charge("sub-b", 90)],
                             scope=["/subscriptions/SUB-A"])
        self.assertEqual(result["rows"], 1)
        self.assertEqual(result["excluded_rows"], 1)
        with warehouse.connect() as con:
            self.assertEqual(con.execute(
                'SELECT "SubAccountId", "BilledCost" FROM costs ORDER BY "SubAccountId"'
            ).fetchall(), [("sub-a", 80.0), ("sub-b", 456.0)])

    def test_manifest_is_validated_before_subscription_filter(self):
        blobs = {"run/a.csv": csv_data([charge("sub-a", 80)]),
                 "run/b.csv": csv_data([charge("sub-b", 90)])}
        result = self.ingest(blobs=blobs, manifests=[manifest(list(blobs))], scope=["sub-a"])
        self.assertEqual(result["rows"], 1)
        self.assertEqual(result["declared_rows"], 2)
        self.assertEqual(result["excluded_rows"], 1)
        self.assertIsNone(result["row_count_mismatch"])

    def test_replacement_dates_are_bounded_per_present_subscription(self):
        with warehouse.connect() as con:
            con.execute("DELETE FROM costs")
            insert_rows(con, records([
                charge("sub-a", 11, ChargePeriodStart="2026-08-02"),
                charge("sub-b", 22, ChargePeriodStart="2026-08-01"),
            ]))
        self.ingest([
            charge("sub-a", 33, ChargePeriodStart="2026-08-01"),
            charge("sub-b", 44, ChargePeriodStart="2026-08-02"),
        ], scope=["sub-a", "sub-b"])
        with warehouse.connect() as con:
            self.assertEqual(con.execute(
                'SELECT "BilledCost" FROM costs ORDER BY "BilledCost"'
            ).fetchall(), [(11.0,), (22.0,), (33.0,), (44.0,)])

    def test_empty_or_absent_selected_scope_never_means_all(self):
        for scope in ([], ["not-in-export"]):
            with self.subTest(scope=scope), self.assertRaises(exports.ExportError):
                # Empty scope is refused before a reader is created.
                if not scope:
                    asyncio.run(exports.ingest_export(scope=scope))
                else:
                    self.ingest([charge()], scope=scope)
            self.assertEqual(self.snapshot(), self.before)

    def test_manifest_and_listing_caps_refuse_before_write(self):
        blobs = {f"run/part-{i}.csv": csv_data([charge()]) for i in range(61)}
        for manifests in ([], [manifest(list(blobs))]):
            with self.subTest(manifest=bool(manifests)), \
                    self.assertRaisesRegex(exports.ExportError, "61.*max_files=60"):
                self.ingest(blobs=blobs, manifests=manifests)
            self.assertEqual(self.snapshot(), self.before)

    def test_complete_partitioned_run_can_raise_cap(self):
        blobs = {f"run/part-{i}.csv": csv_data([charge(cost=1)]) for i in range(61)}
        result = self.ingest(blobs=blobs, manifests=[manifest(list(blobs))], max_files=61)
        self.assertEqual(result["rows"], 61)
        self.assertEqual(result["declared_rows"], 61)
        self.assertEqual(result["row_count_mismatch"], None)

    def test_declared_total_shortfall_refuses_before_write(self):
        with self.assertRaisesRegex(exports.ExportError, "declares 500"):
            self.ingest([charge()], manifests=[manifest(["run/part.csv"], count=500)])
        self.assertEqual(self.snapshot(), self.before)

    def test_insert_failure_rolls_back_delete_and_log(self):
        # Patched on the warehouse module, which is where `staging` reads it from: the
        # importer copies each parsed batch through the bulk helper rather than
        # inserting a list of rows itself.
        with patch("app.warehouse.insert_rows", side_effect=RuntimeError("insert failed")), \
                self.assertRaisesRegex(RuntimeError, "insert failed"):
            self.ingest([charge()])
        self.assertEqual(self.snapshot(), self.before)

    def test_focus_only_refuses_legacy_schema_or_non_focus_manifest_without_write(self):
        non_focus = Manifest.parse({
            "exportConfig": {"type": "ActualCost"},
            "runInfo": {"runId": "actual"},
            "blobs": [{"blobName": "run/part.csv"}],
        })
        for rows, manifests in (
            ([{"Date": DAY, "Cost": 5, "SubscriptionId": "sub-a"}], []),
            ([charge(EffectiveCost=5)], [non_focus]),
            ([charge(EffectiveCost=5, BillingCostBasis="ActualCost")], []),
        ):
            with self.subTest(rows=rows), self.assertRaises(exports.ExportError):
                self.ingest(rows, manifests=manifests, required_metric="FocusCost")
            self.assertEqual(self.snapshot(), self.before)

    def test_focus_only_selects_focus_manifest_in_mixed_sas_container(self):
        focus = manifest(["focus/part.csv"])
        actual = Manifest.parse({
            "exportConfig": {"type": "ActualCost"},
            "runInfo": {"runId": "actual", "submittedTime": "2026-09-02T00:00:00Z",
                        "startDate": DAY, "endDate": DAY},
            "blobs": [{"blobName": "actual/part.csv"}],
        })
        blobs = {"focus/part.csv": csv_data([charge(cost=17, EffectiveCost=15)]),
                 "actual/part.csv": csv_data([{"Date": DAY, "Cost": 999}])}
        reader = Reader(blobs)
        with patch.object(exports.BlobReader, "from_sas_url", return_value=reader), \
                patch.object(exports, "read_manifests", AsyncMock(return_value=[actual, focus])):
            result = asyncio.run(exports.ingest_export(
                sas_url="https://offline.invalid/exports", required_metric="FocusCost", scope=["sub-a"]))
        self.assertEqual(result["rows"], 1)
        self.assertEqual(reader.downloads, ["focus/part.csv"])
        self.assertTrue(reader.closed)
        with warehouse.connect() as con:
            self.assertEqual(con.execute(
                'SELECT "SubAccountId", "BilledCost" FROM costs ORDER BY 1'
            ).fetchall(), [("sub-a", 17.0), ("sub-b", 456.0)])

    @staticmethod
    def query_rows(amounts, sub="/subscriptions/SUB-A"):
        return Warehouse._map_query({
            "columns": [{"name": c} for c in ("UsageDate", "Cost", "Currency", "ServiceFamily")],
            "rows": [[int(day.replace("-", "")), cost, "USD", "Compute"]
                     for day, cost in amounts],
        }, {"id": sub, "name": "one"}, "ActualCost")

    def totals(self):
        with warehouse.connect() as con:
            return con.execute(
                'SELECT "SubAccountId", count(*), sum("BilledCost") FROM costs GROUP BY 1 ORDER BY 1'
            ).fetchall()

    def test_repeated_cross_source_refresh_preserves_detail_scope_and_dates(self):
        with warehouse.connect() as con:
            con.execute("DELETE FROM costs")
            insert_rows(con, records([charge("sub-b", 456),
                                     charge("sub-a", 7, ChargePeriodStart="2026-01-01")]))
        start, end = "2026-08-01", "2026-08-02"
        initial = self.query_rows([(start, 10), (end, 20)])
        for _ in range(2):
            self.assertEqual(asyncio.run(warehouse._replace(
                "/subscriptions/SUB-A", start, end, initial, preserve_detail=True)), 2)
            self.assertEqual(self.totals(), [("sub-a", 3, 37.0), ("sub-b", 1, 456.0)])
        focus = [charge("/subscriptions/SUB-A", 4, ChargePeriodStart=start, EffectiveCost=3,
                        Tags='{"env":"prod"}'),
                 charge("sub-a", 8, ChargePeriodStart=start, EffectiveCost=6)]
        def ingest_focus(rows, day):
            complete = Manifest.parse({
                "dataRowCount": len(rows),
                "runInfo": {"runId": day, "submittedTime": f"{day}T01:00:00Z",
                            "startDate": day, "endDate": day},
                "exportConfig": {"type": "FocusCost"},
                "blobs": [{"blobName": "run/part.csv", "dataRowCount": len(rows)}],
            })
            return self.ingest(rows, manifests=[complete],
                               required_metric="FocusCost", scope=["sub-a"])

        for _ in range(2):
            ingest_focus(focus, start)
            self.assertEqual(self.totals(), [("sub-a", 4, 39.0), ("sub-b", 1, 456.0)])
        newer = self.query_rows([(start, 999), (end, 25)])
        for _ in range(2):
            self.assertEqual(asyncio.run(warehouse._replace(
                "SUB-A", start, end, newer, preserve_detail=True)), 1)
            self.assertEqual(self.totals(), [("sub-a", 4, 44.0), ("sub-b", 1, 456.0)])
        ingest_focus([charge("sub-a", 15, ChargePeriodStart=end, EffectiveCost=11)], end)
        self.assertEqual(asyncio.run(warehouse._replace(
            "sub-a", start, end, newer, preserve_detail=True)), 0)
        self.assertEqual(self.totals(), [("sub-a", 4, 34.0), ("sub-b", 1, 456.0)])
        with warehouse.connect() as con:
            # `{}` is not a tag. The FOCUS loader writes it to record "this export had a Tags
            # column and it was blank for this row" -- known untagged, which is a different fact
            # from NULL ("no Tags column at all, so unknown"). The app draws the same line:
            # /api/diagnostics counts tagged rows with `Tags NOT IN ('', '{}')`. Matching on
            # `<> ''` alone counted the known-untagged row as tagged.
            self.assertEqual(con.execute(
                'SELECT "Tags", "EffectiveCost" FROM costs'
                ' WHERE "Tags" IS NOT NULL AND "Tags" NOT IN (\'\', \'{}\')'
                ' ORDER BY "Tags"'
            ).fetchall(), [('{"env":"prod"}', 3.0)])
            # And the untagged row is recorded as known-untagged rather than left unknown.
            self.assertEqual(con.execute(
                'SELECT count(*) FROM costs WHERE "Tags" = \'{}\''
            ).fetchone()[0], 1)

    def test_quick_preserves_legacy_detail_and_rolls_back_failed_insert(self):
        legacy = records([charge("sub-a", 13, BillingCostBasis=None, Tags='{"team":"a"}')])
        asyncio.run(warehouse._replace("sub-a", DAY, DAY, legacy))
        self.assertEqual(asyncio.run(warehouse._replace(
            "sub-a", DAY, DAY, self.query_rows([(DAY, 999)]), preserve_detail=True)), 0)
        self.assertEqual(self.totals(), [("sub-a", 1, 13.0), ("sub-b", 1, 456.0)])
        before = self.snapshot()
        with patch("app.warehouse.insert_rows", side_effect=RuntimeError("write failed")), \
                self.assertRaisesRegex(RuntimeError, "write failed"):
            asyncio.run(warehouse._replace("sub-a", DAY, DAY, self.query_rows([(DAY, 999)])))
        self.assertEqual(self.snapshot(), before)

    def test_replace_refuses_foreign_subscription_or_out_of_window_rows(self):
        for rows in (self.query_rows([(DAY, 1)], sub="sub-b"),
                     self.query_rows([("2025-01-01", 1)])):
            with self.assertRaisesRegex(ValueError, "outside"):
                asyncio.run(warehouse._replace("sub-a", DAY, DAY, rows, preserve_detail=True))
            self.assertEqual(self.snapshot(), self.before)

    def test_quick_ingest_preserves_focus_and_reports_preserved_days(self):
        with patch("app.warehouse._token", return_value="test-token"), \
                patch("app.warehouse._periods", return_value=[(DAY, DAY)]), \
                patch.object(warehouse, "_query_period",
                             AsyncMock(return_value=self.query_rows([(DAY, 999)]))):
            result = asyncio.run(warehouse.quick_ingest([{"id": "sub-a", "name": "one"}]))
        self.assertEqual(result["status"], "ready")
        self.assertEqual(result["preserved_days"], 1)
        self.assertEqual(result["loaded_rows"], 0)
        self.assertEqual(self.totals(), [("sub-a", 1, 123.0), ("sub-b", 1, 456.0)])


class MoneySafety(unittest.TestCase):
    def setUp(self):
        self.env = patch.dict(os.environ, {"FX_RATES": '{"INR":80,"EUR":0.9}'})
        self.cache = patch.dict(fx._cache, {"rates": {}, "at": 0, "source": None, "date": None})
        self.env.start()
        self.cache.start()
        self.addCleanup(self.env.stop)
        self.addCleanup(self.cache.stop)

    def test_focus_native_cost_without_usd_projects_consistently(self):
        values = [charge(ListCost=16000, ContractedCost=12000, EffectiveCost=8000),
                  charge("sub-b", 100, "USD", ListCost=200, EffectiveCost=100)]
        with warehouse.connect() as con:
            con.execute("DELETE FROM costs")
            insert_rows(con, records(values))
        for currency, expected in (("USD", 200), ("EUR", 180), ("INR", 16000)):
            with warehouse.reader(currency=currency) as reader:
                row = reader.rows(
                    'SELECT sum("BilledCost") AS billed, sum("InvoiceCost") AS invoice, '
                    'sum("EffectiveCost") AS effective FROM costs')[0]
            for amount in row.values():
                self.assertAlmostEqual(amount, expected)
        warehouse.refresh_rollup()
        with warehouse.connect() as con:
            detail = con.execute(
                'SELECT "BillingCurrency", sum("BilledCost") FROM costs GROUP BY 1 ORDER BY 1'
            ).fetchall()
            rolled = con.execute(
                'SELECT "BillingCurrency", sum("BilledCost") FROM costs_rollup GROUP BY 1 ORDER BY 1'
            ).fetchall()
        self.assertEqual(detail, rolled)
        from app import report
        result = report.collect(None, days=30, currency="USD")
        self.assertEqual(result["currency"], "USD")
        self.assertAlmostEqual(result["total"], 200)

    def test_unknown_nonzero_billed_fx_refuses_partial_aggregate(self):
        with warehouse.connect() as con:
            con.execute("DELETE FROM costs")
            insert_rows(con, records([charge(), charge("unknown", 8000, "ZZZ")]))
        with self.assertRaisesRegex(ValueError, "no authoritative exchange rate"), \
                warehouse.reader(currency="USD"):
            self.fail("A reader must not silently sum only the convertible rows")
        with warehouse.reader(scope=["sub-a"], currency="USD") as reader:
            self.assertEqual(reader.rows('SELECT sum("BilledCost") AS cost FROM costs')[0]["cost"],
                             100)

    def test_per_opportunity_conversion_precedes_aggregation(self):
        data = {"rightsizing": {"currency": "USD", "vms": [
            {"id": "a", "name": "usd", "cost": 30, "currency": "USD", "cpu_avg": 0},
            {"id": "b", "name": "inr", "cost": 2400, "currency": "INR", "cpu_avg": 0},
        ]}}
        before = copy.deepcopy(data)
        result = savings.assemble(data, {}, 30, "EUR")
        self.assertEqual(result["total"], 54)
        self.assertEqual(result["billed_total"], 54)
        self.assertEqual([o["window"] for o in result["opportunities"]], [27, 27])
        self.assertEqual(data, before)

    def test_per_source_conversion_precedes_duplicate_merge(self):
        data = {
            "waste": {"currency": "USD", "findings": [
                {"category": "deallocated_vms", "cost": 30,
                 "items": [{"id": RID, "name": "vm", "cost": 30}]}]},
            "rightsizing": {"currency": "INR", "vms": [
                {"id": RID, "name": "vm", "cost": 4800, "cpu_avg": 0}]},
        }
        result = savings.assemble(data, {}, 30, "EUR")
        self.assertEqual(result["total"], 54)
        self.assertEqual(result["count"], 1)
        row = result["opportunities"][0]
        self.assertEqual(row["disputed"], {"low": 27, "high": 54})
        self.assertEqual([s["window"] for s in row["sources"]], [27, 54])
        self.assertEqual(row["items"][0]["cost"], 27)

    def test_commitment_options_compared_only_after_conversion(self):
        data = {"advisor": {"currency": "USD", "recommendations": [
            {"problem": "Buy a reservation", "resource": "plan", "term": "P1Y",
             "annual_savings": 100, "currency": "USD"},
            {"problem": "Buy a reservation", "resource": "plan", "term": "P3Y",
             "annual_savings": 12000, "currency": "INR"},
        ]}}
        result = savings.assemble(data, {}, 30, "EUR")
        self.assertEqual(result["total"], 0)
        self.assertEqual(result["commitment_annual"], 135)
        self.assertEqual([o["annual"] for o in result["commitments"][0]["options"]], [135, 90])

    def test_mixed_waste_items_are_converted_before_group_total(self):
        data = {"waste": {"currency": "mixed", "findings": [
            {"category": "unattached_disks", "cost": 2430, "count": 2,
             "items": [{"id": "a", "cost": 30, "currency": "USD"},
                       {"id": "b", "cost": 2400, "currency": "INR"}]}]}}
        result = savings.assemble(data, {}, 30, "EUR")
        self.assertEqual(result["total"], 54)
        self.assertFalse(result["partial"])

    def test_waste_currency_buckets_cover_truncated_item_list(self):
        data = {"waste": {"currency": None, "findings": [
            {"category": "unattached_disks", "cost": 2430, "count": 61,
             "cost_by_currency": {"USD": 30, "INR": 2400},
             "items": [{"id": "a", "cost": 30, "currency": "USD"}]}]}}
        result = savings.assemble(data, {}, 30, "EUR")
        self.assertEqual(result["total"], 54)
        self.assertEqual(result["opportunities"][0]["count"], 61)
        self.assertFalse(result["partial"])

    def test_missing_fx_has_explicit_partial_savings_and_preserves_original(self):
        data = {"rightsizing": {"currency": "USD", "vms": [
            {"id": "a", "name": "usd", "cost": 30, "currency": "USD", "cpu_avg": 0},
            {"id": "b", "name": "other", "cost": 2400, "currency": "ZZZ", "cpu_avg": 0},
        ]}}
        result = savings.assemble(data, {}, 30, "EUR")
        self.assertEqual(result["total"], 27)
        self.assertTrue(result["partial"])
        self.assertIn("rightsizing_currency", result["unavailable"])
        self.assertEqual(result["unconverted"][0]["finding"]["cost"], 2400)
        unavailable = savings.assemble(data, {}, 30, "ZZY")
        self.assertEqual(unavailable["opportunities"], [])
        self.assertTrue(unavailable["partial"])

    def test_source_partial_flag_alone_is_reported_as_unavailable(self):
        from app import shutdown, waste
        with patch.object(waste, "find_waste", AsyncMock(return_value={"partial": True})), \
                patch.object(waste, "vm_utilisation", AsyncMock(return_value={})), \
                patch.object(waste, "advisor_recommendations", AsyncMock(return_value={})), \
                patch.object(shutdown, "find_schedulable", AsyncMock(return_value={})):
            result = asyncio.run(savings.build(["sub-a"], 30, "USD"))
        self.assertTrue(result["partial"])
        self.assertIn("waste", result["unavailable"])
        self.assertIn("waste", result["scan"]["errors"])

    def scenario(self, currency="INR", cost=4800, target="smaller", retail=0.05):
        from app import waste
        with patch.object(waste, "_arg", AsyncMock(return_value=[
            {"name": "vm", "resourceGroup": "rg", "location": "eastus",
             "size": "current", "osType": "Linux"}
        ])), patch.object(waste, "_costs_for",
                          return_value={RID.lower(): {"cost": cost, "currency": currency}}), \
                patch.object(whatif, "_retail_hourly", AsyncMock(return_value=retail)):
            return asyncio.run(whatif.scenario(resource_id=RID, target_sku=target,
                                              subscription_ids=["sub-a"], days=30))

    def test_whatif_retail_and_invoice_share_currency(self):
        result = self.scenario()
        self.assertEqual(result["currency"], "INR")
        self.assertEqual(result["billed"]["daily"], 160)
        self.assertEqual(result["target"]["daily"], 96)
        self.assertEqual(result["annual_saving"], 64 * 365)
        self.assertIn("80 INR/USD", result["pricing_basis"])
        usd = self.scenario("USD", 60)
        self.assertEqual(usd["target"]["daily"], 1.2)
        self.assertEqual(usd["annual_saving"], 292)
        expensive = self.scenario(cost=2400, retail=1)
        self.assertEqual(expensive["target"]["daily"], 1920)
        self.assertEqual(expensive["annual_saving"], -671600)

    def test_whatif_missing_conversion_or_bill_never_claims_savings(self):
        result = self.scenario("ZZZ")
        self.assertIsNone(result["target"]["daily"])
        self.assertIsNone(result["annual_saving"])
        self.assertIn("unavailable", result["pricing_basis"])
        result = self.scenario("USD", None)
        self.assertIsNone(result["annual_saving"])


class MigrationSafety(unittest.TestCase):
    def test_store_preserves_empty_and_incomplete_destination_collisions(self):
        for existing in ([], [11]):
            with self.subTest(existing=existing):
                suffix = uuid.uuid4().hex[:12]
                shim, destination = f"migration_fn_{suffix}", f"migration_data_{suffix}"
                con = warehouse._store._open()
                try:
                    with con.cursor() as cur:
                        cur.execute(f'CREATE SCHEMA "{shim}"')
                        cur.execute(f'CREATE SCHEMA "{destination}"')
                        cur.execute(f'CREATE TABLE "{destination}".costs (id serial PRIMARY KEY)')
                        for value in existing:
                            cur.execute(f'INSERT INTO "{destination}".costs VALUES (%s)', (value,))
                        cur.execute(f'CREATE TABLE "{shim}".costs (id serial PRIMARY KEY)')
                        cur.execute(f'INSERT INTO "{shim}".costs VALUES (7), (8)')
                        cur.execute(f'CREATE VIEW "{shim}".history AS SELECT * FROM "{shim}".costs')
                    with patch.object(store, "SHIM_SCHEMA", shim), \
                            patch.object(warehouse._store, "schema", destination):
                        warehouse._store._evacuate_shim_schema(con)
                        warehouse._store._evacuate_shim_schema(con)
                    with con.cursor() as cur:
                        cur.execute("SELECT relname FROM pg_class c JOIN pg_namespace n "
                                    "ON c.relnamespace=n.oid WHERE n.nspname=%s", (shim,))
                        self.assertEqual(cur.fetchall(), [])
                        cur.execute(f'SELECT id FROM "{destination}".costs ORDER BY id')
                        self.assertEqual(cur.fetchall(), [(v,) for v in existing])
                        cur.execute(f'SELECT id FROM "{destination}".history ORDER BY id')
                        self.assertEqual(cur.fetchall(), [(7,), (8,)])
                        cur.execute("SELECT relname FROM pg_class c JOIN pg_namespace n "
                                    "ON c.relnamespace=n.oid WHERE n.nspname=%s "
                                    "AND relkind='r' AND relname LIKE 'costs__stranded_%%'",
                                    (destination,))
                        self.assertEqual(len(cur.fetchall()), 1)
                finally:
                    with con.cursor() as cur:
                        cur.execute(f'DROP SCHEMA IF EXISTS "{shim}" CASCADE')
                        cur.execute(f'DROP SCHEMA IF EXISTS "{destination}" CASCADE')
                    con.close()

    def test_failed_store_move_rolls_back_rename(self):
        shim = f"migration_fn_{uuid.uuid4().hex[:12]}"
        con = warehouse._store._open()
        try:
            with con.cursor() as cur:
                cur.execute(f'CREATE SCHEMA "{shim}"')
                cur.execute(f'CREATE TABLE "{shim}".costs (id int)')
                cur.execute(f'INSERT INTO "{shim}".costs VALUES (42)')
            with patch.object(store, "SHIM_SCHEMA", shim), \
                    patch.object(warehouse._store, "schema", "nonexistent_migration_destination"), \
                    self.assertRaisesRegex(RuntimeError, "no history was discarded"):
                warehouse._store._evacuate_shim_schema(con)
            with con.cursor() as cur:
                cur.execute(f'SELECT id FROM "{shim}".costs')
                self.assertEqual(cur.fetchall(), [(42,)])
        finally:
            with con.cursor() as cur:
                cur.execute(f'DROP SCHEMA IF EXISTS "{shim}" CASCADE')
            con.close()


if __name__ == "__main__":
    unittest.main()
