"""Hermetic Azure Monitor performance evidence tests; no Azure or warehouse reads."""
from __future__ import annotations

import asyncio
import copy
import unittest
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, Mock, patch

import httpx

from app import cost
from app import performance_metrics as perf

A = "11111111-1111-1111-1111-111111111111"
B = "22222222-2222-2222-2222-222222222222"
WINDOW = {"start": "2026-09-01T00:00:00Z", "end": "2026-09-02T00:00:00Z", "days": 1}
PLAN = "microsoft.web/serverfarms"
VM = "microsoft.compute/virtualmachines"


def resource(name="plan", kind=PLAN, sub=A):
    provider, *types = kind.split("/")
    path = f"{provider}/{types[0]}/{name}"
    if len(types) == 2:
        path += f"/{types[1]}/database"
    return {"id": f"/subscriptions/{sub}/resourceGroups/rg/providers/{path}",
            "name": name, "type": kind, "subscriptionId": sub, "location": "westus"}


def definitions(kind):
    return {"value": [{"name": {"value": name}, "unit": unit,
                       "supportedAggregationTypes": [aggregation]}
                      for name, _, unit, aggregation in perf.PROFILES[kind]]}


def metric_payload(spec, values=None):
    name, _, unit, aggregation = spec
    values = [0] * 24 if values is None else values
    start = datetime(2026, 9, 1, tzinfo=UTC)
    return {"name": {"value": name}, "unit": unit,
            "timeseries": [{"data": [
                {"timeStamp": (start + timedelta(hours=i)).isoformat().replace("+00:00", "Z"),
                 aggregation.lower(): value}
                for i, value in enumerate(values)]}]}


class Performance(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.rows = [resource()]
        self.costs = {self.rows[0]["id"].lower(): {
            "by_currency": [{"currency": "USD", "cost": 0}], "rows": 1,
            "from": WINDOW["start"], "to": WINDOW["end"],
        }}
        self.graph = AsyncMock(side_effect=lambda *_: {
            "rows": copy.deepcopy(self.rows), "total": len(self.rows), "truncated": False, "pages": 1})
        self.cost_reader = Mock(side_effect=lambda *_: copy.deepcopy(self.costs))
        self.request = AsyncMock(side_effect=self.azure)
        for target, replacement in (("scoped_graph", self.graph), ("resource_costs", self.cost_reader),
                                    ("evidence_window", Mock(return_value=WINDOW))):
            patcher = patch.object(perf, target, replacement)
            patcher.start()
            self.addCleanup(patcher.stop)
        patcher = patch.object(perf.azure, "request", self.request)
        patcher.start()
        self.addCleanup(patcher.stop)

    async def azure(self, method, url, *, params, **kwargs):
        self.assertEqual(method, "GET")
        self.assertTrue(url.startswith(cost.ARM + "/subscriptions/"))
        self.assertEqual(kwargs["attempts"], 1)
        self.assertFalse(kwargs["cache"])
        row = next(r for r in self.rows if r["id"].lower() in url.lower())
        kind = row["type"]
        if url.endswith("/metricDefinitions"):
            self.assertEqual(params["api-version"], "2018-01-01")
            return definitions(kind)
        self.assertEqual(params["api-version"], "2023-10-01")
        self.assertEqual(params["interval"], "PT1H")
        self.assertEqual(params["AutoAdjustTimegrain"], "false")
        self.assertNotIn("$filter", params)
        names = params["metricnames"].split(",")
        self.assertLessEqual(len(names), 4)
        specs = [s for s in perf.PROFILES[kind] if s[0] in names]
        self.assertEqual(len(specs), len(names))
        self.assertTrue(all(s[3] == params["aggregation"] for s in specs))
        return {"interval": "PT1H", "value": [metric_payload(s) for s in specs]}

    async def test_genuine_zero_cost_and_metrics_are_preserved(self):
        result = await perf.report([A], 1)
        item = result["resources"][0]
        self.assertEqual(result["status"], "ok")
        self.assertEqual(item["status"], "ok")
        self.assertEqual(item["cost"], self.costs[item["id"].lower()])
        for metric in item["metrics"]:
            self.assertEqual((metric["status"], metric["value"], metric["samples"]), ("ok", 0, 24))
            self.assertEqual(metric["last_sample"], "2026-09-01T23:00:00Z")
            self.assertIn("expected 24", metric["note"])

    async def test_missing_cost_is_null_not_zero_and_currencies_unchanged(self):
        self.rows.append(resource("second"))
        self.costs[self.rows[0]["id"].lower()]["by_currency"].append({"currency": "EUR", "cost": 4})
        result = await perf.report([A], 1)
        self.assertEqual(result["status"], "partial")
        self.assertIsNone(result["resources"][1]["cost"])
        self.assertEqual(len(result["resources"][0]["cost"]["by_currency"]), 2)
        self.assertIn("not top spend", " ".join(result["notices"]))

    async def test_unsupported_and_vm_memory_never_queried(self):
        self.rows = [resource(kind=VM)]
        async def only_cpu(method, url, **kwargs):
            if url.endswith("/metricDefinitions"):
                return {"value": definitions(VM)["value"][:1]}
            return await self.azure(method, url, **kwargs)
        self.request.side_effect = only_cpu
        result = await perf.report([A], 1)
        metrics = result["resources"][0]["metrics"]
        self.assertEqual([m["status"] for m in metrics], ["ok", "unsupported", "unsupported", "unsupported"])
        self.assertIsNone(metrics[-1]["value"])
        self.assertIn("guest telemetry", metrics[-1]["note"])
        self.assertEqual(self.request.await_count, 2)

    async def test_wrong_aggregation_and_units_are_not_queried(self):
        payload = definitions(PLAN)
        payload["value"][0]["supportedAggregationTypes"] = ["Maximum"]
        payload["value"][1]["unit"] = "Bytes"
        self.request.return_value = payload
        self.request.side_effect = None
        result = await perf.report([A], 1)
        self.assertEqual(result["status"], "unavailable")
        self.assertEqual(self.request.await_count, 1)
        self.assertTrue(all(m["status"] == "unsupported" for m in result["resources"][0]["metrics"]))

    async def test_denied_resource_preserves_useful_peer(self):
        self.rows.append(resource("second"))
        async def denied(method, url, **kwargs):
            if "/serverfarms/plan/" in url:
                raise cost.CostError("Forbidden")
            return await self.azure(method, url, **kwargs)
        self.request.side_effect = denied
        result = await perf.report([A], 1)
        self.assertEqual(result["status"], "partial")
        self.assertEqual([r["status"] for r in result["resources"]], ["unavailable", "ok"])
        self.assertIn("1 resources had denied", " ".join(result["notices"]))

    async def test_http_error_and_error_response_are_unavailable(self):
        for error in (httpx.ConnectError("offline"), {"error": {"code": "Forbidden"}}, {}):
            self.request.side_effect = error if isinstance(error, Exception) else None
            self.request.return_value = error
            result = await perf.report([A], 1)
            self.assertEqual(result["status"], "unavailable")
            self.assertTrue(all(m["status"] == "unavailable" for m in result["resources"][0]["metrics"]))

    async def test_group_failure_does_not_drop_other_aggregation(self):
        self.rows = [resource(kind="microsoft.web/sites")]
        async def fail_totals(method, url, **kwargs):
            if kwargs["params"].get("aggregation") == "Total":
                raise cost.CostError("blocked")
            return await self.azure(method, url, **kwargs)
        self.request.side_effect = fail_totals
        result = await perf.report([A], 1)
        self.assertEqual([m["status"] for m in result["resources"][0]["metrics"]],
                         ["unavailable", "ok", "unavailable"])

    async def test_caps_deduplication_scope_validation_and_determinism(self):
        self.rows = [resource(f"plan-{i:02}") for i in reversed(range(30))]
        self.rows.append(self.rows[0])
        result = await perf.report([A, A], 1)
        self.assertEqual(result["sampled_count"], 24)
        self.assertIsNone(result["inventory_count"])
        self.assertTrue(result["truncated"])
        self.assertEqual(result["resources"][0]["name"], "plan-00")
        self.assertEqual(self.request.await_count, 48)
        self.assertIn("take 25", self.graph.await_args.args[0])
        self.assertEqual(self.graph.await_args.args[1], [A])
        self.assertEqual(len(self.cost_reader.call_args.args[1]), 24)
        for scope in ([], [""], ["wrong"], [A, B + "/providers/x"], A, [None], [A] * 501):
            with self.assertRaises(ValueError):
                await perf.report(scope, 1)

    async def test_malformed_or_out_of_scope_ids_never_reach_cost_or_metrics(self):
        original = resource()["id"]
        ids = [original.replace(A, B), original + "/extra", original + "?x=y",
               original + "#fragment", original.replace("/rg/", "/../"),
               original.replace("/rg/", "/%2f../"), "https://evil.invalid" + original,
               original.replace("/rg/", "/rg\\x/"), original + "/", original.replace(A, "not-guid")]
        self.rows = [dict(resource(), id=rid) for rid in ids]
        self.rows.extend([dict(resource(), subscriptionId=B), dict(resource(), type=VM)])
        result = await perf.report([A], 1)
        self.assertEqual(result["sampled_count"], 0)
        self.assertEqual(result["status"], "unavailable")
        self.cost_reader.assert_not_called()
        self.request.assert_not_awaited()

    async def test_all_profiles_use_bounded_documented_names(self):
        self.rows = [resource(str(i), kind) for i, kind in enumerate(perf.PROFILES)]
        result = await perf.report([A], 1)
        self.assertEqual(len(result["resources"]), 5)
        self.assertTrue(all(any(m["status"] == "ok" for m in r["metrics"]) for r in result["resources"]))
        self.assertLessEqual(self.request.await_count, 15)

    async def test_concurrency_and_per_resource_timeout(self):
        self.rows = [resource(str(i)) for i in range(9)]
        active = peak = 0
        async def slow(method, url, **kwargs):
            nonlocal active, peak
            active += 1
            peak = max(peak, active)
            try:
                await asyncio.sleep(0.01)
                return await self.azure(method, url, **kwargs)
            finally:
                active -= 1
        self.request.side_effect = slow
        await perf.report([A], 1)
        self.assertEqual(peak, 4)
        self.rows = [resource()]
        with patch.object(perf, "RESOURCE_TIMEOUT", 0.001):
            result = await perf.report([A], 1)
        self.assertEqual(result["status"], "unavailable")
        self.assertIn("time budget", result["resources"][0]["metrics"][0]["note"])
        self.assertEqual(active, 0)

    async def test_programming_and_shared_helper_errors_propagate(self):
        self.request.side_effect = TypeError("programming error")
        with self.assertRaises(TypeError):
            await perf.report([A], 1)
        self.request.side_effect = self.azure
        self.graph.side_effect = cost.CostError("inventory error")
        with self.assertRaises(cost.CostError):
            await perf.report([A], 1)
        self.graph.side_effect = None
        self.graph.return_value = {"rows": self.rows, "total": 1, "truncated": False}
        self.cost_reader.side_effect = RuntimeError("cost query error")
        with self.assertRaises(RuntimeError):
            await perf.report([A], 1)

    async def test_returned_interval_and_duplicate_metric_errors(self):
        original = self.azure
        for failure in ("interval", "duplicate"):
            async def altered(method, url, failure=failure, **kwargs):
                payload = await original(method, url, **kwargs)
                if url.endswith("/metrics"):
                    if failure == "interval":
                        payload["interval"] = "P1D"
                    else:
                        payload["value"].append(payload["value"][0])
                return payload
            self.request.side_effect = altered
            result = await perf.report([A], 1)
            self.assertEqual(result["status"], "unavailable")

    async def test_empty_complete_inventory_is_not_a_failure(self):
        self.rows = []
        result = await perf.report([A], 1)
        self.assertEqual((result["status"], result["inventory_count"], result["sampled_count"]), ("ok", 0, 0))
        self.cost_reader.assert_not_called()


class Aggregation(unittest.TestCase):
    def read(self, spec=None, values=None, alter=None):
        spec = spec or perf.PROFILES[PLAN][0]
        output = perf._empty(spec, 1)
        payload = metric_payload(spec, values)
        if alter:
            alter(payload)
        perf._read_metric(output, payload, WINDOW)
        return output

    def test_nulls_remain_missing_and_sparse_average_is_labelled(self):
        missing = self.read(values=[None, None])
        self.assertEqual((missing["status"], missing["value"], missing["samples"]), ("no_data", None, 0))
        sparse = self.read(values=[10, None, 30])
        self.assertEqual((sparse["value"], sparse["samples"]), (20, 2))
        self.assertIn("Incomplete coverage", sparse["note"])
        self.assertIn("Unweighted", sparse["note"])
        self.assertIn("not request-weighted or a percentile", sparse["note"])

    def test_counts_and_bytes_sum_totals_and_ignore_average_fields(self):
        for spec in (perf.PROFILES[VM][1], perf.PROFILES["microsoft.web/sites"][0]):
            result = self.read(spec, [2, None, 3], lambda p: [
                point.update(average=9999) for point in p["timeseries"][0]["data"]])
            self.assertEqual((result["value"], result["samples"]), (5, 2))
            self.assertIn("hourly totals", result["note"])

    def test_metric_level_errors_missing_and_dimension_ambiguity(self):
        for alter, status in (
            (lambda p: p.update(errorCode="BadRequest"), "unavailable"),
            (lambda p: p.update(timeseries=[]), "no_data"),
            (lambda p: p["timeseries"].append(copy.deepcopy(p["timeseries"][0])), "unavailable"),
            (lambda p: p["timeseries"][0].update(metadatavalues=[{"value": "instance1"}]), "unavailable"),
            (lambda p: p.update(unit="Bytes"), "unavailable"),
            (lambda p: p["timeseries"][0]["data"].append(p["timeseries"][0]["data"][0]), "unavailable"),
        ):
            self.assertEqual(self.read(alter=alter)["status"], status)
        metric = perf._empty(perf.PROFILES[PLAN][0], 1)
        perf._read_metric(metric, None, WINDOW)
        self.assertEqual(metric["status"], "no_data")

    def test_invalid_numbers_and_timestamps_do_not_manufacture_zero(self):
        result = self.read(values=[float("nan"), float("inf"), True, "1", None, 0])
        self.assertEqual((result["value"], result["samples"]), (0, 1))
        result = self.read(values=[5], alter=lambda p: p["timeseries"][0]["data"][0].update(timeStamp="wrong"))
        self.assertEqual(result["status"], "no_data")
        result = self.read(values=[5], alter=lambda p: p["timeseries"][0]["data"][0].update(timeStamp=WINDOW["end"]))
        self.assertEqual(result["status"], "no_data")

    def test_window_validation_uses_real_helper(self):
        from app.azure_updates import SourceError
        from app.observability import evidence_window
        for days in (0, 15, True, 1.5, "7"):
            with self.assertRaises(SourceError):
                evidence_window(days)
        self.assertEqual(evidence_window(14)["days"], 14)


if __name__ == "__main__":
    unittest.main()
