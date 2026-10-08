"""Offline Billing opportunity and bounded lifecycle regressions; no Azure or database."""
# cloudlens-test: requires-offline
from __future__ import annotations

import asyncio
import copy
import sys
import unittest
from contextlib import ExitStack, nullcontext
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import billing_commitments as lifecycle
from app import billing_opportunities as billing
from app import cost

SUB = "11111111-1111-1111-1111-111111111111"
OTHER = "22222222-2222-2222-2222-222222222222"
SCOPE = "/subscriptions/" + SUB
RID = SCOPE + "/resourceGroups/group/providers/Microsoft.Compute/virtualMachines/vm"
RI = "/providers/Microsoft.Capacity/reservationOrders/order/reservations/ri"
SP = "/providers/Microsoft.BillingBenefits/savingsPlanOrders/order/savingsPlans/sp"
NOW = datetime(2026, 9, 6, tzinfo=UTC)


def operational(key="vm", window=20, days=60, **kwargs):
    return {
        "key": key, "category": "rightsize", "title": "Right-size VM",
        "resource_id": RID, "subscription": SUB, "resource_group": "group",
        "window": window, "annual": 0, "currency": "USD", "confidence": "medium",
        "sources": [{"source": "rightsizing", "basis": "billed", "window": window, "annual": 0}],
        **kwargs,
    }


def purchase(key="ri", annual=120, kind="reservation", **kwargs):
    return {
        "key": key, "category": "commitment", "title": f"Buy {kind}",
        "subscription": SUB, "annual": annual, "window": 10, "currency": "USD",
        "sources": [{"source": "advisor", "basis": "estimate", "annual": annual, "window": 10}],
        **kwargs,
    }


def metadata(rid=RI, scope=SCOPE, **props):
    return {"id": rid, "properties": {
        "displayName": "Scoped commitment", "appliedScopeType": "Single",
        "appliedScopeProperties": {"subscriptionId": scope},
        "expiryDateTime": (NOW + timedelta(days=90)).isoformat(),
        "provisioningState": "Succeeded", "term": "P1Y",
        "utilization": {"aggregates": [
            {"grain": 1, "grainUnit": "days", "value": 95, "valueUnit": "percentage"},
            {"grain": 30, "grainUnit": "days", "value": 70, "valueUnit": "percentage"},
        ]}, **props}}


class OpportunityTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.scan = self.stack.enter_context(patch.object(billing.savings, "build", AsyncMock()))
        self.inventory = self.stack.enter_context(patch.object(lifecycle, "build", AsyncMock(
            return_value={"items": [], "unavailable": [], "partial": False,
                          "native_billing_currencies": ["USD"]})))
        self.fx = self.stack.enter_context(patch.object(billing.fx, "rate_to",
            side_effect=lambda cur: {"USD": 1, "INR": 80, "EUR": 0.9}.get(cur)))

    async def run_build(self, ops=(), purchases=(), currency=None, days=30, **data):
        self.scan.return_value = {
            "opportunities": list(ops), "commitments": list(purchases), "currency": "USD",
            "days": days, "sources_read": ["waste", "rightsizing", "advisor", "shutdown"],
            "unavailable": {}, **data,
        }
        return await billing.build([SUB], currency, days=days)

    async def test_advisor_alternatives_count_only_best_and_keep_direct_annual(self):
        result = await self.run_build(purchases=[
            purchase(annual=240, options=[{"label": "1 year", "annual": 120}]),
            purchase("sp", 180, "savings plan"),
        ])
        self.assertEqual(result["potential"]["monthly"], 20)
        self.assertEqual(result["potential"]["annual"], 240)
        ri, sp = result["opportunities"]
        self.assertEqual(ri["action"], "Buy Reservation")
        self.assertEqual(sp["action"], "Increase Savings Plan coverage")
        self.assertTrue(ri["included_in_total"])
        self.assertFalse(sp["included_in_total"])
        self.assertEqual(ri["provider_annual_savings"], 240)
        self.assertEqual(sp["overlap_group"], ri["overlap_group"])
        self.assertEqual(ri["options"][0]["monthly_savings"], 10)
        self.assertFalse(ri["options"][0]["included_in_total"])

    async def test_discount_never_layered_on_resizing(self):
        result = await self.run_build([operational()], [purchase(annual=1200)], days=60)
        self.assertEqual(result["potential"]["monthly"], 10)
        self.assertEqual(result["potential"]["annual"], 120)
        self.assertEqual(result["opportunities"][0]["action"], "Right-size resources")
        self.assertIn("operational", result["opportunities"][1]["exclusion_reason"])

    async def test_disjoint_subscriptions_are_compatible(self):
        result = await self.run_build([operational()], [purchase(subscription=OTHER)], days=60)
        self.assertEqual(result["potential"]["monthly"], 20)

    async def test_exact_disjoint_resources_are_compatible(self):
        result = await self.run_build([operational()], [
            purchase(resource_id=RID + "2"), purchase("sp", kind="savings plan", resource_id=RID)
        ])
        self.assertEqual(result["potential"]["monthly"], 30)
        self.assertEqual(result["potential"]["included_count"], 2)

    async def test_truncated_group_blocks_unknown_subscriptions_conservatively(self):
        grouped = operational(resource_id="", subscription="", items=[{"id": RID}], more=15)
        result = await self.run_build([grouped], [purchase(subscription=OTHER)])
        self.assertFalse(result["opportunities"][1]["included_in_total"])

    async def test_normalize_source_windows_before_choosing_estimate(self):
        item = operational(window=80, sources=[
            {"source": "waste", "basis": "billed", "window": 80},
            {"source": "shutdown", "basis": "billed", "window": 30},
        ])
        result = await self.run_build([item], currency="INR", days=120)
        row = result["opportunities"][0]
        self.assertEqual(row["monthly_savings"], 2400)
        self.assertEqual(row["annual_savings"], 28800)
        self.assertEqual(row["window_days"], 30)
        self.assertTrue(row["extrapolated"])
        self.assertIn("Extrapolated", row["basis"])
        self.scan.assert_awaited_once_with([SUB], 120, currency=None)

    async def test_rightsizing_ninety_day_cap(self):
        result = await self.run_build([operational(window=90)], days=180)
        self.assertEqual(result["potential"]["monthly"], 30)

    async def test_each_native_currency_converts_independently(self):
        result = await self.run_build([
            operational(window=10), operational("other", window=800, currency="INR"),
        ], currency="INR")
        self.assertEqual(result["potential"]["monthly"], 1600)
        self.assertEqual(result["potential"]["currency"], "INR")

    async def test_grouped_mixed_native_currency_is_never_summed(self):
        self.inventory.return_value["native_billing_currencies"] = ["USD", "INR"]
        mixed = operational(window=810, sources=[
            {"source": "waste", "basis": "billed", "window": 810}])
        result = await self.run_build([mixed], currency="INR")
        self.assertIsNone(result["potential"]["monthly"])
        self.assertIn("native-currency", result["opportunities"][0]["exclusion_reason"])
        self.assertIsNone(result["opportunities"][0]["source_monthly_savings"])

    async def test_unknown_grouped_currency_is_also_not_zero(self):
        self.inventory.return_value["native_billing_currencies"] = None
        mixed = operational(window=20, sources=[
            {"source": "waste", "basis": "billed", "window": 20}])
        result = await self.run_build([mixed])
        self.assertIsNone(result["potential"]["monthly"])
        self.assertEqual(result["status"], "partial")

    async def test_fx_unavailable_preserves_source_amounts_not_zero(self):
        result = await self.run_build([operational(window=10)], currency="XYZ")
        self.assertIsNone(result["potential"]["monthly"])
        self.assertEqual(result["opportunities"][0]["source_monthly_savings"], 10)
        self.assertIsNone(result["opportunities"][0]["monthly_savings"])
        self.assertEqual(result["status"], "partial")

    async def test_nonfinite_or_nonpositive_fx_rates_are_unavailable(self):
        for value in (float("inf"), float("nan"), -1, 0):
            with self.subTest(rate=value):
                self.fx.side_effect = lambda code, rate=value: 1 if code == "USD" else rate
                result = await self.run_build([operational(window=10)], currency="EUR")
                self.assertIsNone(result["potential"]["monthly"])
                self.assertIsNone(result["opportunities"][0]["conversion_rate"])
                self.assertEqual(result["opportunities"][0]["source_monthly_savings"], 10)
                self.assertEqual(result["status"], "partial")

    async def test_missing_prices_are_null(self):
        result = await self.run_build([operational(window=None)], [purchase(annual=0)])
        self.assertIsNone(result["potential"]["monthly"])
        self.assertTrue(result["potential"]["partial"])
        self.assertTrue(all(o["monthly_savings"] is None for o in result["opportunities"]))

    async def test_source_failure_returns_partial_priced_subset(self):
        result = await self.run_build([operational(window=10)], unavailable={"advisor": "403"})
        self.assertEqual(result["potential"]["monthly"], 10)
        self.assertEqual(result["status"], "partial")
        self.assertTrue(any("advisor" in warning for warning in result["warnings"]))

    async def test_all_source_failure_is_not_zero(self):
        result = await self.run_build(unavailable={"advisor": "403", "waste": "timeout"})
        self.assertIsNone(result["potential"]["monthly"])
        self.assertNotIn("partial_basis", result, "an undeclared scan gap stays retryable")

    async def test_gaps_that_are_all_access_refusals_are_declared(self):
        # Live Scale, 8 Oct: the scan was refused by tenant/visibility and reservation
        # metadata by 403, so this report was "Refresh failed" on every view.
        denied = {"advisor": "Advisor could not be read for any selected subscription."}
        refused = [{"source": "benefit metadata", "reason": "Azure denied read access (403).",
                    "access": True},
                   {"source": "Microsoft.Capacity", "id": RI, "fields": ["expires_on"],
                    "reason": "Expiry/utilisation not supplied or not safely scope-attributable."}]
        self.inventory.return_value = {"items": [{"id": RI}], "unavailable": refused,
                                       "partial": True, "native_billing_currencies": ["USD"]}
        result = await self.run_build(unavailable=denied, partial=True, partial_basis="access")
        self.assertEqual(result["status"], "partial")
        self.assertEqual(result["partial_basis"], "access")
        for inventory_gap, scan in (
            ({"source": "lifecycle", "reason": "The bounded Azure lifecycle read timed out."},
             {"partial_basis": "access"}),
            ({"source": "warehouse", "reason": "Observed commitment inventory is unavailable."},
             {"partial_basis": "access"}),
            (None, {}),
        ):
            with self.subTest(inventory_gap=inventory_gap, scan=scan):
                self.inventory.return_value = {
                    "items": [{"id": RI}], "partial": True, "native_billing_currencies": ["USD"],
                    "unavailable": refused + ([inventory_gap] if inventory_gap else [])}
                result = await self.run_build(unavailable=denied, partial=True, **scan)
                self.assertNotIn("partial_basis", result)

    def test_only_refused_provider_reads_are_marked_as_access(self):
        self.assertTrue(lifecycle._issue("x", RuntimeError("HTTP 403 Forbidden"))["access"])
        self.assertTrue(lifecycle._issue("x", cost.CostError(
            "InvalidAuthenticationTokenTenant", status_code=401))["access"])
        for exc in (TimeoutError(), RuntimeError("HTTP 500"),
                    cost.CostError("expired token (401)", status_code=401)):
            self.assertNotIn("access", lifecycle._issue("x", exc))

    async def test_outer_scan_failure_preserves_inventory(self):
        self.scan.side_effect = RuntimeError("offline")
        self.inventory.return_value = {"items": [{"id": RI}], "partial": True}
        result = await billing.build([SUB])
        self.assertEqual(result["status"], "partial")
        self.assertIsNone(result["potential"]["monthly"])
        self.assertEqual(result["commitments"]["items"][0]["id"], RI)

    async def test_empty_success_is_zero_identified_not_actual_savings(self):
        result = await self.run_build()
        self.assertEqual(result["potential"]["monthly"], 0)
        self.assertIn("Not realized", result["potential"]["basis"])

    async def test_no_scopes_never_broaden(self):
        result = await billing.build([])
        self.assertEqual(result["status"], "error")
        self.assertIsNone(result["potential"]["monthly"])
        self.scan.assert_not_called()
        self.inventory.assert_not_called()

    async def test_resource_group_scope_and_invalid_days_fail_closed(self):
        with self.assertRaisesRegex(ValueError, "explicit subscription list"):
            await billing.build(None)
        with self.assertRaises(ValueError):
            await billing.build([SCOPE + "/resourceGroups/limited"])
        for days in (0, 366, True, 0.5):
            with self.assertRaises(ValueError):
                await billing.build([SUB], days=days)
        self.scan.assert_not_called()

    async def test_without_advisor_never_say_buy(self):
        result = await self.run_build(purchases=[purchase(sources=[])])
        self.assertEqual(result["opportunities"][0]["action"], "Review commitment options")
        self.assertFalse(result["opportunities"][0]["included_in_total"])

    async def test_advisor_review_does_not_imply_purchase(self):
        result = await self.run_build(purchases=[purchase(title="Review unused reservations")])
        self.assertEqual(result["opportunities"][0]["action"], "Review commitment options")
        self.assertFalse(result["opportunities"][0]["included_in_total"])

    async def test_real_advisor_reservation_wording(self):
        result = await self.run_build(purchases=[purchase(
            title="Consider App Service reserved instance to save over the on-demand costs")])
        self.assertEqual(result["opportunities"][0]["action"], "Buy Reservation")

    async def test_merged_provider_annual_retained_without_assuming_its_currency(self):
        item = operational(sources=[
            {"source": "waste", "basis": "billed", "window": 10},
            {"source": "advisor", "basis": "estimate", "annual": 80, "window": 7},
        ], annual=80)
        result = await self.run_build([item])
        row = result["opportunities"][0]
        self.assertEqual(row["annual_savings"], 120)
        self.assertEqual(row["provider_annual_estimates"][0]["annual"], 80)
        self.assertIsNone(row["provider_annual_estimates"][0]["currency"])

    async def test_cancelled_commitments_not_advised_for_renewal(self):
        self.inventory.return_value = {"items": [{
            "id": RI, "state": "Cancelled", "expiring": True, "underutilized": True,
        }]}
        result = await self.run_build()
        self.assertEqual(result["opportunities"], [])

    async def test_all_scan_sources_failed_report_error(self):
        result = await self.run_build(scan={"status": "error"})
        self.assertEqual(result["status"], "error")
        self.assertIsNone(result["potential"]["annual"])

    async def test_lifecycle_alerts_are_not_purchases_or_monetized(self):
        item = lifecycle._enrich(metadata(), [SCOPE], None, NOW, "Microsoft.Capacity")
        self.inventory.return_value = {"items": [item], "partial": False}
        result = await self.run_build()
        self.assertEqual(len(result["opportunities"]), 2)
        self.assertEqual(result["potential"]["monthly"], 0)
        self.assertEqual({row["action"] for row in result["opportunities"]},
                         {"Review expiring commitment", "Review underutilized commitment"})
        self.assertTrue(all(row["monthly_savings"] is None for row in result["opportunities"]))


class MetadataTests(unittest.TestCase):
    def test_expiry_thresholds_and_provider_period(self):
        for days, expected in ((-1, False), (0, True), (90, True), (91, False)):
            raw = metadata(expiryDateTime=(NOW + timedelta(days=days)).isoformat())
            item = lifecycle._enrich(raw, [SCOPE], None, NOW, "Microsoft.Capacity")
            self.assertEqual(item["expiring"], expected)
            self.assertEqual(item["days_to_expiry"], days)
            self.assertEqual(item["utilization_pct"], 70)
            self.assertEqual(item["utilization_period"]["days"], 30)
            self.assertEqual(item["utilization_period"]["unit"], "percentage")
            self.assertTrue(item["underutilized"])
            self.assertIsNone(item["monthly_unused_cost"])

    def test_legacy_expiry_date_is_supported_without_guessing_term(self):
        raw = metadata(expiryDateTime="bad", expiryDate="2026-12-05", term=None)
        item = lifecycle._enrich(raw, [SCOPE], None, NOW, "Microsoft.Capacity")
        self.assertEqual(item["days_to_expiry"], 90)
        self.assertIsNone(item["term"])

    def test_unknown_units_or_invalid_utilization_are_not_zero(self):
        for value, unit, grain in ((0.7, "ratio", 30), (101, "percentage", 30),
                                   (70, "percentage", 90), (None, "percentage", 30),
                                   (float("nan"), "percentage", 30)):
            raw = metadata(utilization={"aggregates": [{
                "grain": grain, "grainUnit": "days", "value": value, "valueUnit": unit}]})
            item = lifecycle._enrich(raw, [SCOPE], None, NOW, "Microsoft.Capacity")
            self.assertIsNone(item["utilization_pct"])
            self.assertIsNone(item["underutilized"])

    def test_real_zero_utilization_is_preserved(self):
        raw = metadata(utilization={"aggregates": [{
            "grain": 30, "grainUnit": "days", "value": 0, "valueUnit": "percentage"}]})
        item = lifecycle._enrich(raw, [SCOPE], None, NOW, "Microsoft.Capacity")
        self.assertEqual(item["utilization_pct"], 0)
        self.assertTrue(item["underutilized"])

    def test_underutilization_boundary_is_strictly_below_eighty(self):
        raw = metadata(utilization={"aggregates": [{
            "grain": 7, "grainUnit": "days", "value": 80, "valueUnit": "percentage"}]})
        item = lifecycle._enrich(raw, [SCOPE], None, NOW, "Microsoft.Capacity")
        self.assertFalse(item["underutilized"])

    def test_multiple_applied_scopes_only_selected_relationship_is_returned(self):
        raw = metadata(appliedScopeProperties={}, appliedScopes=[
            SCOPE, "/subscriptions/" + OTHER])
        item = lifecycle._enrich(raw, [SCOPE], None, NOW, "Microsoft.Capacity")
        self.assertEqual(item["applied_scopes"], [SCOPE])
        self.assertIsNone(item["utilization_pct"])
        self.assertNotIn(OTHER, str(item))

    def test_unknown_expiry_is_not_last_seen_or_start_plus_term(self):
        existing = lifecycle._observed({"id": RI, "first_seen": "2025-01-01",
                                        "last_seen": "2026-09-06", "resources": 0}, 0)
        raw = metadata(expiryDateTime=None, effectiveDateTime="2025-01-01", utilization={})
        item = lifecycle._enrich(raw, [SCOPE], existing, NOW, "Microsoft.Capacity")
        self.assertIsNone(item["expires_on"])
        self.assertIsNone(item["utilization_pct"])
        self.assertIsNone(item["underutilized"])
        self.assertEqual(item["last_seen"], "2026-09-06")

    def test_later_metadata_clears_stale_lifecycle_fields(self):
        existing = lifecycle._enrich(metadata(), [SCOPE], None, NOW, "provider")
        raw = metadata(appliedScopeType="Shared", expiryDateTime=None)
        refreshed = lifecycle._enrich(raw, [SCOPE], existing, NOW, "provider")
        self.assertIsNone(refreshed["expires_on"])
        self.assertIsNone(refreshed["days_to_expiry"])
        self.assertIsNone(refreshed["expiring"])
        self.assertIsNone(refreshed["utilization_pct"])
        self.assertIsNone(refreshed["utilization_period"])
        self.assertIsNone(refreshed["underutilized"])
        self.assertEqual(existing["utilization_pct"], 70)

    def test_unrelated_shared_or_billing_scope_only_omitted(self):
        for raw in (
            metadata(scope="/subscriptions/" + OTHER),
            metadata(appliedScopeType="Shared", billingScopeId=SCOPE),
            metadata(appliedScopeType="ManagementGroup"),
        ):
            self.assertIsNone(lifecycle._enrich(raw, [SCOPE], None, NOW, "provider"))

    def test_observed_shared_retained_without_cross_scope_totals(self):
        observed = lifecycle._observed({"id": RI, "cost": 25, "currency": "USD"}, 0)
        raw = metadata(appliedScopeType="Shared", billingAccountId="DO NOT EXPOSE")
        item = lifecycle._enrich(raw, [SCOPE], observed, NOW, "Microsoft.Capacity")
        self.assertEqual(item["observed_cost"], 25)
        self.assertIsNotNone(item["expires_on"])
        self.assertIsNone(item["utilization_pct"])
        self.assertNotIn("DO NOT EXPOSE", str(item))
        self.assertEqual(item["sources"], ["warehouse", "Microsoft.Capacity"])

    def test_group_scope_not_broadened_for_direct_metadata_projection(self):
        raw = metadata(appliedScopeProperties={"subscriptionId": SCOPE,
            "resourceGroupId": SCOPE + "/resourceGroups/other"})
        self.assertIsNone(lifecycle._enrich(raw, [SCOPE + "/resourceGroups/limited"],
                                          None, NOW, "provider"))
        self.assertIsNotNone(lifecycle._enrich(raw, [SCOPE], None, NOW, "provider"))

    def test_unsupported_metadata_or_host_id_rejected(self):
        for raw in ({"id": RI, "properties": []}, {"id": "https://evil.invalid", "properties": {}},
                    {"id": RI + "?api-version=evil", "properties": {}}):
            self.assertIsNone(lifecycle._enrich(raw, [SCOPE], None, NOW, "provider"))


class MetadataTransportTests(unittest.IsolatedAsyncioTestCase):
    async def test_metadata_does_not_wait_for_the_cost_scan_semaphore(self):
        import httpx

        client = httpx.AsyncClient
        requests = []

        def handler(request):
            requests.append(request)
            return httpx.Response(200, json={"value": []})

        with (
            patch.object(cost, "_CONCURRENCY", asyncio.Semaphore(0)),
            patch.object(cost.azure, "_bearer", AsyncMock(return_value="test-token")),
            patch.object(cost.azure, "request", AsyncMock(side_effect=AssertionError("cost queue used"))),
            patch.object(httpx, "AsyncClient",
                         lambda **kwargs: client(transport=httpx.MockTransport(handler), **kwargs)),
        ):
            result = await asyncio.wait_for(lifecycle._get_metadata(
                cost.ARM + lifecycle.RESERVATIONS, params={"api-version": lifecycle.API_VERSION}), 1)
        self.assertEqual(result, {"value": []})
        self.assertEqual(requests[0].method, "GET")
        self.assertEqual(requests[0].headers["Authorization"], "Bearer test-token")

    async def test_metadata_url_is_restricted_before_credentials_are_used(self):
        with patch.object(cost.azure, "_bearer", AsyncMock()) as token:
            for url in ("https://evil.invalid/providers/Microsoft.Capacity/reservations",
                        "http://management.azure.com/providers/Microsoft.Capacity/reservations",
                        cost.ARM + "/subscriptions/not-a-billing-read"):
                with self.assertRaises(ValueError):
                    await lifecycle._get_metadata(url)
            token.assert_not_awaited()


class LifecycleReadTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.wh = Mock(return_value={"commitments": [], "as_of": "2026-09-05"})
        self.stack.enter_context(patch.object(lifecycle, "get_commitments",
                                             return_value=SimpleNamespace(inventory=self.wh)))
        self.calls = []
        self.responses = {}

        async def read(url, **kwargs):
            self.calls.append(("GET", url, kwargs, cost.caller_token()))
            value = self.responses.get(url, {"value": []})
            if isinstance(value, Exception):
                raise value
            return copy.deepcopy(value)

        self.request = self.stack.enter_context(patch.object(lifecycle, "_get_metadata",
                                                              AsyncMock(side_effect=read)))

    async def test_scoped_purchase_inventory_and_exact_metadata(self):
        self.wh.return_value["commitments"] = [{"id": RI, "name": "Observed", "cost": 10}]
        self.responses[cost.ARM + RI] = metadata()
        self.responses[cost.ARM + lifecycle.RESERVATIONS] = {
            "value": [metadata(rid=RI + "2"), metadata(rid=RI + "3", scope="/subscriptions/" + OTHER)]}
        self.responses[cost.ARM + lifecycle.SP_ORDERS] = {
            "value": [{"properties": {"savingsPlans": [SP]}}]}
        self.responses[cost.ARM + SP] = metadata(SP)
        result = await lifecycle.build([SUB])
        self.assertEqual({row["id"] for row in result["items"]}, {RI, RI + "2", SP})
        self.assertTrue(all(c[0] == "GET" for c in self.calls))
        self.assertTrue(all(c[2]["params"]["api-version"] == "2022-11-01" for c in self.calls))
        self.wh.assert_called_once_with([SUB], days=30, currency=None)

    async def test_forbidden_keeps_observed_and_explicitly_unknown(self):
        self.wh.return_value["commitments"] = [{"id": RI, "name": "Observed", "resources": 0}]
        for path in (RI, lifecycle.RESERVATIONS, lifecycle.SP_ORDERS):
            self.responses[cost.ARM + path] = cost.CostError("403 denied secret account-id")
        result = await lifecycle.build([SUB])
        self.assertTrue(result["partial"])
        self.assertEqual(len(result["items"]), 1)
        self.assertIsNone(result["items"][0]["utilization_pct"])
        self.assertIsNone(result["items"][0]["expires_on"])
        self.assertTrue(any("(403)" in u["reason"] for u in result["unavailable"]))
        self.assertNotIn("secret account-id", str(result))

    async def test_partial_pagination_failure_keeps_first_page(self):
        link = cost.ARM + lifecycle.RESERVATIONS + "?api-version=2022-11-01&skip=1"
        self.responses[cost.ARM + lifecycle.RESERVATIONS] = {
            "value": [metadata()], "nextLink": link}
        self.responses[link] = cost.CostError("403")
        result = await lifecycle.build([SUB])
        self.assertEqual(result["items"][0]["id"], RI)
        self.assertTrue(result["partial"])

    async def test_untrusted_next_link_never_followed(self):
        self.responses[cost.ARM + lifecycle.RESERVATIONS] = {
            "value": [metadata()], "nextLink": "https://untrusted.invalid/?token=do-not-send"}
        result = await lifecycle.build([SUB])
        self.assertTrue(result["partial"])
        self.assertFalse(any("untrusted" in call[1] for call in self.calls))
        self.assertNotIn("do-not-send", str(result))

    async def test_metadata_cap_is_explicit(self):
        self.wh.return_value["commitments"] = [{"id": RI + str(i)} for i in range(3)]
        for i in range(3):
            self.responses[cost.ARM + RI + str(i)] = metadata(RI + str(i))
        with patch.object(lifecycle, "MAX_METADATA", 1):
            result = await lifecycle.build([SUB])
        self.assertEqual(len(result["items"]), 3)
        self.assertEqual(sum(row["metadata_status"] == "available" for row in result["items"]), 1)
        self.assertTrue(any("metadata cap" in n for n in result["notices"]))

    async def test_unsupported_observed_ids_are_never_requested(self):
        self.wh.return_value["commitments"] = [{"id": "legacy-id", "resources": 0}]
        result = await lifecycle.build([SUB])
        self.assertEqual(len(self.calls), 2)
        self.assertIsNone(result["items"][0]["portal_url"])
        self.assertTrue(result["partial"])

    async def test_unsupported_order_shape_is_explicit(self):
        self.responses[cost.ARM + lifecycle.SP_ORDERS] = {"value": [{"properties": {}}]}
        result = await lifecycle.build([SUB])
        self.assertTrue(result["partial"])
        self.assertTrue(any("child IDs" in n for n in result["notices"]))

    async def test_global_request_cap_and_no_account_identifiers_leak(self):
        self.responses[cost.ARM + lifecycle.SP_ORDERS] = {
            "value": [{"properties": {"savingsPlans": [SP], "billingAccountId": "PRIVATE"}}]}
        with patch.object(lifecycle, "MAX_REQUESTS", 2):
            result = await lifecycle.build([SUB])
        self.assertLessEqual(len(self.calls), 2)
        self.assertTrue(result["partial"])
        self.assertNotIn(SP, str(result))
        self.assertNotIn("PRIVATE", str(result))

    async def test_delegated_auth_refusal_is_clear(self):
        self.responses[cost.ARM + lifecycle.RESERVATIONS] = cost.CostError("401 token refused")
        result = await lifecycle.build([SUB])
        self.assertTrue(any("tenant token (401)" in u["reason"] for u in result["unavailable"]))

    async def test_multitenant_uses_only_selected_subscription_tokens_and_restores_context(self):
        actor = cost.Caller("home", {SUB: "selected", OTHER: "unselected"})
        reset = cost.act_as(actor, "test-billing-user")
        try:
            await lifecycle.build([SUB])
            self.assertTrue(all(call[3] == "selected" for call in self.calls))
            self.assertEqual(cost.caller_token(), "home")
        finally:
            cost.stop_acting(reset)

    async def test_warehouse_failure_still_returns_attributable_purchases(self):
        self.wh.side_effect = RuntimeError("db offline")
        self.responses[cost.ARM + lifecycle.RESERVATIONS] = {"value": [metadata()]}
        result = await lifecycle.build([SUB])
        self.assertEqual(result["items"][0]["id"], RI)
        self.assertTrue(result["partial"])

    async def test_timeout_preserves_observed_row(self):
        self.wh.return_value["commitments"] = [{"id": RI}]

        async def slow(*args, **kwargs):
            await asyncio.sleep(1)

        self.request.side_effect = slow
        with patch.object(lifecycle, "LIFECYCLE_TIMEOUT", 0.01):
            result = await lifecycle.build([SUB])
        self.assertEqual(result["items"][0]["id"], RI)
        self.assertTrue(any("timed out" in u["reason"] for u in result["unavailable"]))

    async def test_no_scopes_no_calls(self):
        result = await lifecycle.build([])
        self.assertTrue(result["partial"])
        self.wh.assert_not_called()
        self.request.assert_not_called()

    async def test_native_currency_census_scoped_and_not_display_converted(self):
        reader = Mock()
        reader.rows.return_value = [{"currency": "INR"}, {"currency": "USD"}]
        wh = SimpleNamespace(reader=Mock(return_value=nullcontext(reader)))
        service = SimpleNamespace(inventory=self.wh, wh=wh)
        with patch.object(lifecycle, "get_commitments", return_value=service):
            result = await lifecycle.build([SUB], "EUR")
        self.assertEqual(result["native_billing_currencies"], ["INR", "USD"])
        wh.reader.assert_called_once_with([SUB])
        self.wh.assert_called_once_with([SUB], days=30, currency="EUR")


if __name__ == "__main__":
    unittest.main()
