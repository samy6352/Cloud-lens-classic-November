"""What reservations and savings plans this estate is actually consuming.

The obvious source is `Microsoft.Capacity/reservationOrders`, and it is unavailable to most
callers: it is scoped to the billing account rather than the subscription, so somebody with
Owner across a whole estate is still refused. Verified against a live tenant, where it returns
403 for a Global Administrator. So this reads billing data instead, which needs no permission
the app does not already have.

That is not merely a workaround. The orders API lists what was bought; billing shows what is
being used, which is the question somebody looking at a cost tool is asking — and it makes a
commitment that nothing is consuming visible, which is money leaving every month for no
workload.

The failure modes are all about claiming to know more than billing says: printing a term length
that is not in the data, merging reservations with savings plans because both read as
"committed", or reporting an unused commitment as simply absent.

    python test_commitments_inventory.py
"""


from __future__ import annotations

import sys as _sys
from pathlib import Path as _Path

# Tests live in tests/ and import the application from the checkout root.
_sys.path.insert(0, str(_Path(__file__).resolve().parents[1]))

import os

os.environ.setdefault("AUTH_DISABLED", "true")

from app import commitments as C

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
    """Answers the four queries inventory() runs, chosen by what the SQL asks for."""

    agg = "costs"
    benefit = "costs"

    def __init__(self, hi, rows, services, resources=()):
        self.hi, self._rows, self._services, self._resources = hi, rows, services, resources

    def rows(self, sql, limit=None):
        if "max(\"ChargePeriodStart\") AS hi" in sql:
            return [{"hi": self.hi}] if self.hi else []
        if "\"ServiceName\" AS service" in sql:
            return list(self._services)
        # Unique to the per-resource drill-down query: nothing else selects a resource group
        # under this alias, so this has to be checked before falling through to the aggregate.
        if "AS grp" in sql:
            return list(self._resources)
        return list(self._rows)

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class FakeWarehouse:
    def __init__(self, reader):
        self._reader = reader

    def reader(self, scope, currency=None, include_commitments=False):
        return self._reader


def inv(rows, services=(), resources=(), hi="2026-08-31", days=90):
    c = C.Commitments.__new__(C.Commitments)
    c.wh = FakeWarehouse(FakeReader(hi, rows, services, resources))
    return c.inventory(None, days=days)


def row(**kw):
    base = {"id": "ri-1", "name": "prod-d2s-ri", "model": "Reservation", "currency": "USD",
            "cost": 1200.0, "resources": 4, "subscriptions": 1,
            "first_seen": "2026-06-01", "last_seen": "2026-08-31"}
    base.update(kw)
    return base


print("=" * 72)
print("what the estate is consuming")

r = inv([row()])
one = r["commitments"][0]
check("a reservation is listed", r["count"] == 1 and one["name"] == "prod-d2s-ri")
check("its cost and reach come through",
      one["cost"] == 1200.0 and one["resources"] == 4, str(one["cost"]))
check("and the window it was seen in", one["first_seen"] == "2026-06-01"
      and one["last_seen"] == "2026-08-31")

print()
print("=" * 72)
print("a reservation and a savings plan are not the same purchase")

r = inv([row(id="ri-1", model="Reservation"),
         row(id="sp-1", name="compute-sp", model="SavingsPlan", cost=800.0)])
kinds = {c["name"]: c["kind"] for c in r["commitments"]}
check("a reservation is called a reservation", kinds["prod-d2s-ri"] == "reservation")
check("a savings plan is called a savings plan", kinds["compute-sp"] == "savings plan",
      "one is a SKU in a region, the other an hourly spend anywhere in a family")
check("and they are totalled separately",
      r["totals"] == {"reservation": 1200.0, "savings plan": 800.0},
      "adding them would recommend replacing a thing with itself")

# FOCUS and the legacy exports disagree on the spelling, and a savings plan that reads as a
# reservation would be advised on as if it were locked to one SKU.
for spelling in ("savingsPlan", "savings plan", "SAVINGS_PLAN"):
    k = inv([row(id="s", name="s", model=spelling)])["commitments"][0]["kind"]
    check(f"'{spelling}' is recognised", k == "savings plan", k)

for spelling in ("Reservation", "Committed"):
    k = inv([row(id="r", name="r", model=spelling)])["commitments"][0]["kind"]
    check(f"'{spelling}' is a reservation" if spelling == "Reservation"
          else f"'{spelling}' is at least known to be committed",
          k in ("reservation", "commitment"), k)

# FOCUS's category, not its type: it says money is committed without saying to what. Forcing it
# into one would produce advice about a purchase the customer may not hold.
k = inv([row(id="c", name="c", model="CommitmentDiscount")])["commitments"][0]["kind"]
check("'CommitmentDiscount' is neither, and says so", k == "commitment",
      "it is FOCUS's category rather than its type")

print()
print("=" * 72)
print("a commitment nothing is using")

r = inv([row(id="idle-1", name="orphan-ri", resources=0, cost=900.0)])
check("is flagged", r["commitments"][0]["idle"] is True,
      "money leaving every month for no workload")
check("and named at the top level so it cannot be missed", r["idle"] == ["orphan-ri"])

r = inv([row(resources=3)])
check("one that is being used is not flagged", r["commitments"][0]["idle"] is False)

print()
print("=" * 72)
print("what billing cannot tell us, it does not claim")

r = inv([row()])
check("term length is null rather than guessed", r["commitments"][0]["term"] is None,
      "'1 year' printed beside a three-year commitment is worse than no answer")
check("and the reason is carried with the data",
      "billing-account scope" in (r.get("term_note") or ""),
      "so the caveat survives being exported")
check("the basis says these are the ones in scope, not everything bought",
      "purchased" in (r.get("basis") or "") and "will not appear" in (r.get("basis") or ""))

print()
print("=" * 72)
print("empty and unknown")

r = inv([], hi=None)
check("an empty warehouse says so rather than reporting no commitments",
      r.get("empty") is True and r["commitments"] == [],
      "no data loaded is not the same as nothing owned")

r = inv([])
check("a loaded warehouse with no commitments is simply zero",
      r.get("empty") is None and r["count"] == 0 and r["totals"] == {},
      "here the estate genuinely has none")

print()
print("=" * 72)
print("services a commitment covers")

r = inv([row(id="sp-1", name="sp", model="SavingsPlan")],
        services=[{"id": "sp-1", "service": "Virtual Machines", "cost": 700.0},
                  {"id": "sp-1", "service": "Azure Functions", "cost": 100.0}])
check("are listed against it",
      r["commitments"][0]["services"] == ["Virtual Machines", "Azure Functions"],
      "two services under one plan is why a savings plan was chosen over a reservation")

print()
print("=" * 72)
print("which resources a commitment actually covers — the drill-down")

r = inv([row(id="sp-1", name="sp", model="SavingsPlan", resources=2)],
        resources=[{"id": "sp-1", "name": "vm-a", "grp": "rg-prod", "cost": 500.0},
                   {"id": "sp-1", "name": "vm-b", "grp": "rg-prod", "cost": 300.0}])
top = r["commitments"][0]["resources_top"]
check("the resources are listed, ranked by what they cost against it",
      [x["name"] for x in top] == ["vm-a", "vm-b"], top)
check("with their resource group and their own share of the cost",
      top[0] == {"name": "vm-a", "resource_group": "rg-prod", "cost": 500.0}, top[0])
check("nothing left over to fold into 'more' when every resource is shown",
      r["commitments"][0]["resources_more"] == 0)

# A savings plan covering more resources than the cap shows only the costliest, and says how
# many were left out rather than silently truncating without a trace.
many = [{"id": "sp-1", "name": f"vm-{i}", "grp": "rg-prod", "cost": float(100 - i)}
        for i in range(C.MAX_RESOURCES_SHOWN + 3)]
r = inv([row(id="sp-1", name="sp", model="SavingsPlan",
              resources=C.MAX_RESOURCES_SHOWN + 3)],
        resources=many)
one = r["commitments"][0]
check(f"capped at {C.MAX_RESOURCES_SHOWN} even though more were seen",
      len(one["resources_top"]) == C.MAX_RESOURCES_SHOWN, len(one["resources_top"]))
check("and the rest are counted rather than dropped without a trace",
      one["resources_more"] == 3, one["resources_more"])

# A commitment with no resources at all (the idle case, tested above) must not KeyError on a
# drill-down that simply has nothing to show.
r = inv([row(id="idle-1", name="orphan-ri", resources=0)])
check("an idle commitment's drill-down is empty, not missing",
      r["commitments"][0]["resources_top"] == [] and r["commitments"][0]["resources_more"] == 0)

print()
print("=" * 72)
print(f"  {passed} passed, {failed} failed")
raise SystemExit(1 if failed else 0)
