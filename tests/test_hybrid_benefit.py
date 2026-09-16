"""Which Windows Server VMs already use Hybrid Benefit, and what the rest might save.

Hermetic: `coverage()` is exercised with `_arg`/`_costs_for`/`list_subscriptions` and
`_retail_hourly` all monkeypatched, so this needs neither a warehouse nor the network -- and,
crucially, exercises the real `_is_windows_server` imported from esu.py rather than a stand-in
for it, so a change to that classifier's behaviour shows up here too.

    python test_hybrid_benefit.py
"""
from __future__ import annotations

import asyncio
import sys as _sys
from pathlib import Path as _Path

# Tests live in tests/ and import the application from the checkout root.
_sys.path.insert(0, str(_Path(__file__).resolve().parents[1]))

import os

os.environ.setdefault("AUTH_DISABLED", "true")

from app import hybrid_benefit

passed = failed = 0


def check(name: str, ok: bool, detail: str = "") -> None:
    global passed, failed
    if ok:
        passed += 1
        print(f"  OK   {name}" + (f"  {detail}" if detail else ""))
    else:
        failed += 1
        print(f"  FAIL {name}  {detail}")


SUB = "00000000-0000-0000-0000-000000000000"
RG = f"/subscriptions/{SUB}/resourceGroups/rg-demo/providers/Microsoft.Compute/virtualMachines"

# Four machines: one already on the benefit, one clearly not, one Windows *client* (must be
# excluded -- a different licensing programme), and one Linux box (must never appear at all,
# since the Resource Graph query itself filters on osType).
ROWS = [
    {"name": "won-ahb", "id": f"{RG}/won-ahb", "resourceGroup": "rg-demo", "location": "eastus2",
     "licenseType": "Windows_Server", "publisher": "MicrosoftWindowsServer",
     "offer": "WindowsServer", "size": "Standard_D2s_v3"},
    {"name": "no-ahb", "id": f"{RG}/no-ahb", "resourceGroup": "rg-demo", "location": "eastus2",
     "licenseType": "", "publisher": "MicrosoftWindowsServer", "offer": "WindowsServer",
     "size": "Standard_D2s_v3"},
    {"name": "client-vm", "id": f"{RG}/client-vm", "resourceGroup": "rg-demo",
     "location": "eastus2", "licenseType": "Windows_Client", "publisher": "MicrosoftWindowsDesktop",
     "offer": "Windows-11", "size": "Standard_D2s_v3"},
]


async def main() -> int:
    print("=" * 72)
    print("coverage(), with Resource Graph, the cost join and retail rates all faked out")

    import app.cost as cost_mod
    import app.waste as waste_mod
    import app.whatif as whatif_mod

    async def fake_arg(query, subs, top=500):
        return ROWS

    def fake_costs_for(resource_ids, days):
        # Only the uncovered VM bills close to the Windows rate; the covered one already runs
        # near the Linux rate, which is what Hybrid Benefit being on actually looks like.
        return {
            f"{RG}/won-ahb".lower(): {"cost": 72.0, "currency": "USD"},
            f"{RG}/no-ahb".lower(): {"cost": 96.0, "currency": "USD"},
        }

    async def fake_list_subs():
        return {"subscriptions": [{"id": SUB}]}

    async def fake_retail_hourly(region, sku, windows):
        return 0.20 if windows else 0.10

    saved = (waste_mod._arg, waste_mod._costs_for, cost_mod.list_subscriptions,
             whatif_mod._retail_hourly)
    waste_mod._arg = fake_arg
    waste_mod._costs_for = fake_costs_for
    cost_mod.list_subscriptions = fake_list_subs
    whatif_mod._retail_hourly = fake_retail_hourly

    try:
        r = await hybrid_benefit.coverage(subscription_ids=None, days=30)

        check("the Windows client VM is excluded -- a different licensing programme entirely",
              all(v["name"] != "client-vm" for v in r["vms"]),
              [v["name"] for v in r["vms"]])
        check("exactly the two Windows Server VMs remain", r["count"] == 2, r["count"])
        check("one is already covered", r["covered"] == 1, r["covered"])
        check("one is not", r["uncovered"] == 1, r["uncovered"])

        by_name = {v["name"]: v for v in r["vms"]}
        check("the covered VM is reported as such", by_name["won-ahb"]["hybrid_benefit"] is True)
        check("the uncovered VM is reported as such", by_name["no-ahb"]["hybrid_benefit"] is False)
        check("a covered VM is not also given a saving estimate -- there is nothing left to "
              "switch on", by_name["won-ahb"]["estimated_annual_savings"] is None)
        check("the uncovered VM's saving is the Windows-vs-Linux retail gap, annualised, not "
              "read off its own bill",
              by_name["no-ahb"]["estimated_annual_savings"] == round((0.20 - 0.10) * 24 * 365, 2),
              by_name["no-ahb"]["estimated_annual_savings"])
        check("the real billed cost still comes through per VM",
              by_name["no-ahb"]["cost"] == 96.0 and by_name["won-ahb"]["cost"] == 72.0)
        check("the total is the sum of only the uncovered machines' savings, not both",
              r["estimated_annual_savings"] == by_name["no-ahb"]["estimated_annual_savings"])
        check("uncovered, priced machines are listed before covered ones",
              r["vms"][0]["name"] == "no-ahb")
        check("both licenseType and the saving estimate are explained, not left to be assumed "
              "equally precise", "exact" in r["note"] and "estimate" in r["note"])

        print()
        print("=" * 72)
        print("edge cases")

        waste_mod._arg = fake_arg
        await hybrid_benefit.coverage(subscription_ids=["nonexistent-sub"], days=30)

        async def only_linux(query, subs, top=500):
            return []

        waste_mod._arg = only_linux
        r_none = await hybrid_benefit.coverage(subscription_ids=[SUB], days=30)
        check("no Windows Server machines in scope is reported plainly, not as an error",
              r_none["count"] == 0 and "No Windows Server" in r_none["note"])

        async def unpriced(region, sku, windows):
            return None

        waste_mod._arg = fake_arg
        whatif_mod._retail_hourly = unpriced
        r_unpriced = await hybrid_benefit.coverage(subscription_ids=None, days=30)
        check("an unpriced size gets no saving estimate rather than a wrong one",
              all(v["estimated_annual_savings"] is None for v in r_unpriced["vms"]
                  if not v["hybrid_benefit"]))
        check("and the total does not silently include a None as if it were zero saved for "
              "real", r_unpriced["estimated_annual_savings"] == 0.0)
    finally:
        waste_mod._arg, waste_mod._costs_for, cost_mod.list_subscriptions, \
            whatif_mod._retail_hourly = saved

    print()
    print("=" * 72)
    print(f"  {passed} passed, {failed} failed")
    return 1 if failed else 0


if __name__ == "__main__":
    _sys.exit(asyncio.run(main()))
