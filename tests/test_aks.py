"""AKS cost, attributed to the cluster that caused it.

An AKS cluster bills almost nothing under its own name: the money is in the managed node
resource group Azure creates beside it. So the failure mode is a page that reports a large
Kubernetes estate as costing nearly nothing, with the real spend scattered across VM and
networking lines nobody connects to a cluster.

The subtler failure is the one this estate actually exhibits. Resource Graph reports zero
clusters here, while the warehouse holds 228 rows and $81 of MC_ spend for two clusters that
were deleted. Building the view on Resource Graph alone would show an empty page and imply the
AKS bill was zero — the same empty-means-zero error as everywhere else. Deleted clusters still
charging is precisely when somebody asks the question.

    python test_aks.py
"""


from __future__ import annotations

import sys as _sys
from pathlib import Path as _Path

# Tests live in tests/ and import the application from the checkout root.
_sys.path.insert(0, str(_Path(__file__).resolve().parents[1]))

import asyncio
import os

os.environ.setdefault("AUTH_DISABLED", "true")

from app import aks

passed = failed = 0


def check(name: str, ok: bool, detail: str = "") -> None:
    global passed, failed
    if ok:
        passed += 1
        print(f"  OK   {name}" + (f"  {detail}" if detail else ""))
    else:
        failed += 1
        print(f"  FAIL {name}  {detail}")


class FakeReader:
    # The real `Reader` exposes the relation each query should name -- `agg` for the
    # rollup, `named` for the presence table where it can also price -- defaulting to
    # `costs`. Modelled here so a caller that routes itself off the detail table is
    # exercised rather than crashing on a missing attribute.
    agg = res = named = bill = "costs"

    def __init__(self, hi, rows, daily):
        self.hi, self._rows, self._daily = hi, rows, daily

    def rows(self, sql, limit=None):
        if 'max("ChargePeriodStart") AS hi' in sql:
            return [{"hi": self.hi}] if self.hi else []
        if '"ChargePeriodStart" AS day' in sql:
            return list(self._daily)
        return list(self._rows)

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class FakeWarehouse:
    def __init__(self, reader):
        self._r = reader

    def reader(self, scope, currency=None):
        return self._r


def run(cost_rows, live=(), daily=(), hi="2026-08-31", days=30):
    import app.warehouse as wh
    import app.waste as waste

    real_wh, real_arg = wh.warehouse, waste._arg
    wh.warehouse = FakeWarehouse(FakeReader(hi, cost_rows, daily))

    async def fake_arg(q, subs, top=500):
        return list(live)

    waste._arg = fake_arg
    try:
        return asyncio.run(aks.clusters(["sub-a"], days=days))
    finally:
        wh.warehouse, waste._arg = real_wh, real_arg


def crow(rg, service="Virtual Machines", cost=100.0, region="eastus", resources=2):
    return {"rg": rg, "sub": "sub-a", "service": service, "region": region,
            "currency": "USD", "cost": cost, "resources": resources}


def cluster(name="prod", node_rg="MC_rg-prod_prod_eastus", **kw):
    base = {"name": name, "id": f"/subscriptions/s/rg/{name}", "clusterResourceGroup": "rg-prod",
            "subscriptionId": "sub-a", "location": "eastus", "nodeResourceGroup": node_rg,
            "kubernetesVersion": "1.29.2", "tier": "Standard", "poolCount": 2,
            "powerState": "Running"}
    base.update(kw)
    return base


print("=" * 72)
print("the money is in the node resource group")

r = run([crow("MC_rg-prod_prod_eastus", cost=500.0),
         crow("MC_rg-prod_prod_eastus", service="Load Balancer", cost=34.0),
         crow("MC_rg-prod_prod_eastus", service="Storage", cost=12.0)],
        live=[cluster()])
c = r["clusters"][0]
check("one cluster, not three line items", r["count"] == 1)
check("its cost is the whole node group", c["cost"] == 546.0, str(c["cost"]))
check("named from Azure rather than from the group name", c["cluster"] == "prod" and c["live"])
check("split by what you would do about it",
      c["by_category"] == {"nodes": 500.0, "networking": 34.0, "storage": 12.0},
      "nodes are a rightsizing question; disks often an orphan one")

print()
print("=" * 72)
print("a cluster that was deleted and is still charging")

# The live case on this estate: warehouse has the spend, Resource Graph has no cluster.
r = run([crow("mc_rg-aks-portal-test_aks-portal-test-1_eastus",
              service="Load Balancer", cost=33.9)], live=[])
c = r["clusters"][0]
check("it still appears", r["count"] == 1 and c["cost"] == 33.9,
      "Resource Graph alone would show an empty page and imply zero")
check("the name is recovered from the group", c["cluster"] == "aks-portal-test-1", c["cluster"])
check("it is marked as not live", c["live"] is False)
check("and called out at the top level", r["orphaned"] == ["aks-portal-test-1"])
check("an unmatched group does not prove deletion or current charges",
      "Neither status establishes deletion or ongoing charges" in (r["orphan_note"] or ""))

r = run([crow("MC_rg-prod_prod_eastus")], live=[cluster()])
check("a live cluster is never called orphaned", r["orphaned"] == [])

print()
print("=" * 72)
print("Azure's mapping beats the naming convention")

# A custom node resource group defeats the convention entirely — which is why the convention is
# only ever the fallback.
r = run([crow("my-custom-nodes", cost=200.0)],
        live=[cluster(name="renamed", node_rg="my-custom-nodes")])
c = r["clusters"][0]
check("a custom node group is still attributed", c["cluster"] == "renamed" and c["cost"] == 200.0,
      "the MC_ convention would have missed this entirely")
check("and marked certain", c["certain"] is True)

print()
print("=" * 72)
print("parsing a node group name, honestly")

p = aks.parse_node_group("MC_rg-prod_prod_eastus")
check("the ordinary shape splits", p["cluster"] == "prod" and p["region"] == "eastus")
check("and says it is sure", p["certain"] is True)

# Underscores are legal in both halves, so this genuinely has more than one reading.
p = aks.parse_node_group("MC_my_rg_my_cluster_westus2")
check("an ambiguous name is not split into a confident guess", p["certain"] is False,
      "a wrong cluster name is worse than an unsplit one")
check("but the region is still read", p["region"] == "westus2")
check("a name that is not a node group is refused",
      aks.parse_node_group("rg-something") is None)

print()
print("=" * 72)
print("a cluster with no spend in the window")

r = run([], live=[cluster(name="quiet")])
check("is still listed, at zero",
      r["count"] == 1 and r["clusters"][0]["cost"] == 0.0,
      "'we have five clusters and this shows three' makes people distrust the page")
check("and carries what Azure knows about it",
      r["clusters"][0]["tier"] == "Standard"
      and r["clusters"][0]["kubernetes_version"] == "1.29.2")

print()
print("=" * 72)
print("empty, and unreachable")

r = run([], hi=None)
check("an empty warehouse says empty rather than 'no clusters'", r.get("empty") is True,
      "no data loaded is not an estate without Kubernetes")

# Resource Graph being unavailable must not lose the historic cost, which lives in the warehouse.
import app.waste as waste  # noqa: E402

real = waste._arg


async def boom(q, subs, top=500):
    raise RuntimeError("403 AuthorizationFailed")


waste._arg = boom
try:
    r = run([crow("MC_rg-prod_prod_eastus", cost=90.0)], live=[])
    check("a failed cluster lookup still reports the cost",
          r["count"] == 1 and r["clusters"][0]["cost"] == 90.0,
          "the warehouse half of the answer does not depend on ARM")
finally:
    waste._arg = real

print()
print("=" * 72)
print("categorising the bill")

for svc, want in [("Virtual Machines", "nodes"), ("Load Balancer", "networking"),
                  ("Storage", "storage"), ("Azure Monitor", "monitoring"),
                  ("Something Else", "other"), ("", "other")]:
    check(f"{svc or '(blank)'!r} -> {want}", aks.categorise(svc) == want, aks.categorise(svc))

print()
print("=" * 72)
print(f"  {passed} passed, {failed} failed")
raise SystemExit(1 if failed else 0)
