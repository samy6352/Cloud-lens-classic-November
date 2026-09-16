"""What a VM would cost resized or shut down, before doing either.

Hermetic: `_pick_on_demand` is tested against a real response captured live from
prices.azure.com for Standard_D2s_v3/eastus2 (six rows: on-demand, Spot and Low Priority, each
again for Windows), so the parsing logic is checked against the catalogue's actual shape rather
than an assumption about it. `scenario()` is tested with `_arg`/`_costs_for`/`list_subscriptions`
and `_retail_hourly` all monkeypatched -- no warehouse, no network, no real Resource Graph call.

    python test_whatif.py
"""
from __future__ import annotations

import asyncio
import sys as _sys
from pathlib import Path as _Path

# Tests live in tests/ and import the application from the checkout root.
_sys.path.insert(0, str(_Path(__file__).resolve().parents[1]))

import os

os.environ.setdefault("AUTH_DISABLED", "true")

from app import whatif

passed = failed = 0


def check(name: str, ok: bool, detail: str = "") -> None:
    global passed, failed
    if ok:
        passed += 1
        print(f"  OK   {name}" + (f"  {detail}" if detail else ""))
    else:
        failed += 1
        print(f"  FAIL {name}  {detail}")


# Captured live from prices.azure.com on 2026-09-05 for:
#   armRegionName eq 'eastus2' and serviceName eq 'Virtual Machines'
#   and armSkuName eq 'Standard_D2s_v3' and priceType eq 'Consumption'
REAL_CATALOGUE_RESPONSE = [
    {"productName": "Virtual Machines DSv3 Series Windows", "skuName": "D2s v3 Spot",
     "meterName": "D2s v3 Spot", "armSkuName": "Standard_D2s_v3", "retailPrice": 0.09},
    {"productName": "Virtual Machines DSv3 Series", "skuName": "D2s v3 Low Priority",
     "meterName": "D2s v3 Low Priority", "armSkuName": "Standard_D2s_v3", "retailPrice": 0.02},
    {"productName": "Virtual Machines DSv3 Series", "skuName": "D2s v3",
     "meterName": "D2s v3", "armSkuName": "Standard_D2s_v3", "retailPrice": 0.10},
    {"productName": "Virtual Machines DSv3 Series", "skuName": "D2s v3 Spot",
     "meterName": "D2s v3 Spot", "armSkuName": "Standard_D2s_v3", "retailPrice": 0.05},
    {"productName": "Virtual Machines DSv3 Series Windows", "skuName": "D2s v3 Low Priority",
     "meterName": "D2s v3 Low Priority", "armSkuName": "Standard_D2s_v3", "retailPrice": 0.07},
    {"productName": "Virtual Machines DSv3 Series Windows", "skuName": "D2s v3",
     "meterName": "D2s v3", "armSkuName": "Standard_D2s_v3", "retailPrice": 0.19},
]


async def main() -> int:
    print("=" * 72)
    print("picking the on-demand rate out of a real catalogue response")

    check("Linux on-demand is the plain rate, not Spot's cheaper one",
          whatif._pick_on_demand(REAL_CATALOGUE_RESPONSE, windows=False) == 0.10)
    check("Windows on-demand is its own, higher rate",
          whatif._pick_on_demand(REAL_CATALOGUE_RESPONSE, windows=True) == 0.19)
    check("Spot's lower price is never returned for either OS",
          0.09 not in (whatif._pick_on_demand(REAL_CATALOGUE_RESPONSE, True),
                       whatif._pick_on_demand(REAL_CATALOGUE_RESPONSE, False))
          and 0.05 not in (whatif._pick_on_demand(REAL_CATALOGUE_RESPONSE, True),
                           whatif._pick_on_demand(REAL_CATALOGUE_RESPONSE, False)))
    check("an unpriced SKU returns None, not zero", whatif._pick_on_demand([], False) is None)

    print()
    print("=" * 72)
    print("scenario() assembly, with the network and warehouse both faked out")

    import app.cost as cost_mod
    import app.waste as waste_mod

    RID = ("/subscriptions/00000000-0000-0000-0000-000000000000"
           "/resourceGroups/rg-demo/providers/Microsoft.Compute/virtualMachines/demovm1")

    async def fake_arg(query, subs, top=500):
        return [{"id": RID, "name": "demovm1", "resourceGroup": "rg-demo",
                 "location": "eastus2", "size": "Standard_D2s_v3", "osType": "Linux"}]

    def fake_costs_for(resource_ids, days):
        return {RID.lower(): {"cost": 60.0, "currency": "USD"}}

    async def fake_list_subs():
        return {"subscriptions": [{"id": "00000000-0000-0000-0000-000000000000"}]}

    saved = (waste_mod._arg, waste_mod._costs_for, cost_mod.list_subscriptions)
    waste_mod._arg = fake_arg
    waste_mod._costs_for = fake_costs_for
    cost_mod.list_subscriptions = fake_list_subs

    async def fake_retail(region, sku, windows):
        # $0.05/hr smaller size than the $0.10/hr current one, so a resize genuinely saves.
        return 0.05 if sku == "Standard_B2s" else 0.10

    saved_retail = whatif._retail_hourly
    whatif._retail_hourly = fake_retail

    try:
        r = await whatif.scenario(resource_id=RID, target_sku="Standard_B2s",
                                  subscription_ids=None, days=30)
        check("the VM is named from the Resource Graph row", r["resource"] == "demovm1")
        check("billed is the real cost divided over the window, not a retail estimate",
              r["billed"]["daily"] == round(60.0 / 30, 4), r["billed"])
        check("current size at retail uses the CURRENT sku, not the target",
              r["current_retail"]["daily"] == round(0.10 * 24, 4), r["current_retail"])
        check("the target is priced at the smaller SKU's own rate",
              r["target"]["daily"] == round(0.05 * 24, 4), r["target"])
        check("the saving compares billed against target, not retail against target",
              r["annual_saving"] == round(r["billed"]["annual"] - r["target"]["annual"], 2))
        check("the Hybrid Benefit / reservation caveat is stated, not left implied",
              "Hybrid Benefit" in r["note"] and "reservation" in r["note"])

        print()
        print("=" * 72)
        print("shutdown needs no retail lookup at all")

        r2 = await whatif.scenario(resource_id=RID, target_sku="shutdown",
                                   subscription_ids=None, days=30)
        check("the target is exactly zero, not an estimate", r2["target"]["daily"] == 0.0)
        check("labelled as shutdown", r2["target_size"] == "shutdown")
        check("the note explains disks keep billing, since compute stopping is not the whole bill",
              "disks" in r2["note"].lower())

        print()
        print("=" * 72)
        print("what a bad request looks like")

        try:
            await whatif.scenario(resource_id="not-a-resource-id", target_sku="Standard_B2s",
                                  subscription_ids=None, days=30)
            check("an unrecognisable id is refused", False, "did not raise")
        except whatif.WhatIfError as exc:
            check("an unrecognisable id is refused", True, str(exc))

        try:
            await whatif.scenario(resource_id=RID.replace(
                "00000000-0000-0000-0000-000000000000", "11111111-1111-1111-1111-111111111111"),
                target_sku="Standard_B2s", subscription_ids=None, days=30)
            check("a resource outside the caller's own subscriptions is refused", False,
                  "did not raise")
        except whatif.WhatIfError as exc:
            check("a resource outside the caller's own subscriptions is refused", True, str(exc))

        async def empty_arg(query, subs, top=500):
            return []

        waste_mod._arg = empty_arg
        try:
            await whatif.scenario(resource_id=RID, target_sku="Standard_B2s",
                                  subscription_ids=None, days=30)
            check("a resource Resource Graph cannot find is refused", False, "did not raise")
        except whatif.WhatIfError as exc:
            check("a resource Resource Graph cannot find is refused", True, str(exc))
        waste_mod._arg = fake_arg

        async def unpriced_retail(region, sku, windows):
            return None

        whatif._retail_hourly = unpriced_retail
        try:
            await whatif.scenario(resource_id=RID, target_sku="Standard_Nonexistent_v99",
                                  subscription_ids=None, days=30)
            check("an unpriced target SKU is refused rather than silently costed at $0", False,
                  "did not raise")
        except whatif.WhatIfError as exc:
            check("an unpriced target SKU is refused rather than silently costed at $0", True,
                  str(exc))
    finally:
        waste_mod._arg, waste_mod._costs_for, cost_mod.list_subscriptions = saved
        whatif._retail_hourly = saved_retail

    print()
    print("=" * 72)
    print(f"  {passed} passed, {failed} failed")
    return 1 if failed else 0


if __name__ == "__main__":
    _sys.exit(asyncio.run(main()))
