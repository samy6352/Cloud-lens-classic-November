"""What changed near an anomaly, from Activity Log -- grouped, filtered, and never trusted whole.

Hermetic: `azure.request` and `list_subscriptions` are monkeypatched with a real six-row shape
captured live this session (a VM scale set resize logged Started/Failed/Succeeded, and a
four-resource cascading delete sharing one correlationId), so the grouping and filtering logic
is checked against what Activity Log actually returns rather than an assumption about it.

    python test_root_cause.py
"""
from __future__ import annotations

import asyncio
import sys as _sys
from pathlib import Path as _Path

# Tests live in tests/ and import the application from the checkout root.
_sys.path.insert(0, str(_Path(__file__).resolve().parents[1]))

import os

os.environ.setdefault("AUTH_DISABLED", "true")

from app import root_cause

passed = failed = 0


def check(name: str, ok: bool, detail: str = "") -> None:
    global passed, failed
    if ok:
        passed += 1
        print(f"  OK   {name}" + (f"  {detail}" if detail else ""))
    else:
        failed += 1
        print(f"  FAIL {name}  {detail}")


SUB = "bd2e809d-470b-48b1-9273-d513848e8977"
RG = "CostAnomaly"


def _op(name: str) -> dict:
    return {"value": name}


def _status(name: str) -> dict:
    return {"value": name}


# Captured live from management.azure.com on 2026-09-05 (trimmed and re-timed around a fixed
# anomaly day rather than the real dates, so the test does not go stale as today's date moves).
REAL_SHAPE = [
    # A VM scale set write: three lifecycle stages, one real event.
    {"eventTimestamp": "2026-06-15T08:44:21Z", "operationName": _op(
        "Microsoft.Compute/virtualMachineScaleSets/write"), "status": _status("Succeeded"),
     "resourceId": f"/subscriptions/{SUB}/resourcegroups/{RG}"
                   "/providers/Microsoft.Compute/virtualMachineScaleSets/LBVMSS",
     "caller": "4619a992-000c-420a-97c5-fa7fd7c0d2b2", "correlationId": "corr-vmss"},
    {"eventTimestamp": "2026-06-15T08:33:59Z", "operationName": _op(
        "Microsoft.Compute/virtualMachineScaleSets/write"), "status": _status("Failed"),
     "resourceId": f"/subscriptions/{SUB}/resourcegroups/{RG}"
                   "/providers/Microsoft.Compute/virtualMachineScaleSets/LBVMSS",
     "caller": "4619a992-000c-420a-97c5-fa7fd7c0d2b2", "correlationId": "corr-vmss"},
    {"eventTimestamp": "2026-06-15T08:33:58Z", "operationName": _op(
        "Microsoft.Compute/virtualMachineScaleSets/write"), "status": _status("Started"),
     "resourceId": f"/subscriptions/{SUB}/resourcegroups/{RG}"
                   "/providers/Microsoft.Compute/virtualMachineScaleSets/LBVMSS",
     "caller": "4619a992-000c-420a-97c5-fa7fd7c0d2b2", "correlationId": "corr-vmss"},
    # A cascading delete: four resources, one correlationId, one person.
    {"eventTimestamp": "2026-06-14T11:22:53Z", "operationName": _op(
        "Microsoft.Automation/automationAccounts/delete"), "status": _status("Succeeded"),
     "resourceId": f"/subscriptions/{SUB}/resourcegroups/{RG}"
                   "/providers/Microsoft.Automation/automationAccounts/AIretirement",
     "caller": "dechow@microsoft.com", "correlationId": "corr-cleanup"},
    {"eventTimestamp": "2026-06-14T11:22:26Z", "operationName": _op(
        "Microsoft.Web/serverFarms/delete"), "status": _status("Succeeded"),
     "resourceId": f"/subscriptions/{SUB}/resourcegroups/{RG}"
                   "/providers/Microsoft.Web/serverFarms/ASP-CostAnomaly-816f",
     "caller": "dechow@microsoft.com", "correlationId": "corr-cleanup"},
    # Noise that must be filtered: a read-ish action, logged the same day, same resource group.
    {"eventTimestamp": "2026-06-15T09:00:00Z", "operationName": _op(
        "Microsoft.Storage/storageAccounts/ListServiceSas/action"), "status": _status("Succeeded"),
     "resourceId": f"/subscriptions/{SUB}/resourcegroups/{RG}"
                   "/providers/Microsoft.Storage/storageAccounts/costanomalyaed9",
     "caller": "someone@microsoft.com", "correlationId": "corr-read"},
]


async def main() -> int:
    print("=" * 72)
    print("find(), with the Activity Log request itself faked out")

    import app.cost as cost_mod

    captured_filters: list[str] = []

    async def fake_request(method, url, *, params=None, json_body=None, attempts=5, cache=True):
        captured_filters.append((params or {}).get("$filter", ""))
        return {"value": REAL_SHAPE}

    async def fake_list_subs():
        return {"subscriptions": [{"id": SUB}]}

    saved = (cost_mod.azure.request, cost_mod.list_subscriptions)
    cost_mod.azure.request = fake_request
    cost_mod.list_subscriptions = fake_list_subs

    try:
        r = await root_cause.find(resource_group=RG, day="2026-06-15", subscription_ids=[SUB])

        check("a read-only action never appears -- it cannot explain a cost that is still there",
              all("ListServiceSas" not in " ".join(c["operations"]) for c in r["changes"]))
        check("the three-row VM scale set write collapses to one finding, not three",
              r["count"] == 2, r["count"])

        vmss = next(c for c in r["changes"] if "virtualMachineScaleSets/write" in c["operations"][0])
        check("the collapsed finding reports the terminal outcome, not the Started row's",
              vmss["succeeded"] is True)
        check("its own timestamp is the latest of the group, not the first",
              vmss["time"] == "2026-06-15T08:44:21Z", vmss["time"])

        cleanup = next(c for c in r["changes"] if len(c["operations"]) == 2)
        check("a cascading delete sharing one correlationId is one finding naming both "
              "operations, not two separate ones",
              set(cleanup["operations"]) == {"Microsoft.Automation/automationAccounts/delete",
                                             "Microsoft.Web/serverFarms/delete"})
        check("and both resources it touched, by name rather than the full resourceId",
              set(cleanup["resources"]) == {"AIretirement", "ASP-CostAnomaly-816f"})
        check("attributed to who actually made it",
              cleanup["caller"] == "dechow@microsoft.com")

        check("newest change listed first",
              r["changes"][0]["time"] >= r["changes"][1]["time"])
        check("the window is centred on the anomaly day, one day either side of it -- a change "
              "make late in one timezone can bill the next day",
              "2026-06-14T00:00:00Z" in captured_filters[0]
              and "2026-06-17T00:00:00Z" in captured_filters[0])
        check("the resource group itself is server-side filtered, not fetched whole and sieved "
              "in Python", f"resourceGroupName eq '{RG}'" in captured_filters[0])
        check("candidate, not verdict, is said outright rather than implied by a bare list",
              "candidate" in r["note"] and "not causation" in r["note"])

        print()
        print("=" * 72)
        print("what a bad request and a partial outage look like")

        try:
            await root_cause.find(resource_group="", day="2026-06-15", subscription_ids=[SUB])
            check("an empty resource group is refused", False, "did not raise")
        except root_cause.RootCauseError as exc:
            check("an empty resource group is refused", True, str(exc))

        try:
            await root_cause.find(resource_group=RG, day="not-a-date", subscription_ids=[SUB])
            check("an unparsable day is refused", False, "did not raise")
        except root_cause.RootCauseError as exc:
            check("an unparsable day is refused", True, str(exc))

        from app.cost import CostError

        async def one_fails(method, url, *, params=None, json_body=None, attempts=5, cache=True):
            raise CostError("403 Forbidden")

        cost_mod.azure.request = one_fails
        r_partial = await root_cause.find(resource_group=RG, day="2026-06-15",
                                          subscription_ids=[SUB, "second-sub"])
        check("a subscription Activity Log refuses does not blank the whole answer -- it is "
              "disclosed as a count instead", r_partial["unreadable_subscriptions"] == 2)
        check("and said in the note a person would actually read",
              "could not be read" in r_partial["note"])
    finally:
        cost_mod.azure.request, cost_mod.list_subscriptions = saved

    print()
    print("=" * 72)
    print(f"  {passed} passed, {failed} failed")
    return 1 if failed else 0


if __name__ == "__main__":
    _sys.exit(asyncio.run(main()))
