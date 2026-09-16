"""Pay-as-you-go against the commitments Azure will actually recommend.

The dangerous source here is the one that looks best. Cost Management's
`benefitRecommendations` returns a beautifully structured comparison — term, hourly commitment,
utilisation — and returns it for the whole *billing account* even when asked at subscription
scope. Measured on this estate at $4.0M of eligible spend against Advisor's $20–$79 for the same
subscriptions: wrong by three orders of magnitude, and carrying other people's aggregates
through the RBAC boundary the rest of the app enforces. So this reads Advisor, and these tests
exist mostly to stop the tempting source coming back.

The other failure is arithmetic that reads as generosity: adding a one-year and a three-year
recommendation for the same workload, which describes a purchase nobody would make.

    python test_commitment_comparison.py
"""


from __future__ import annotations

import sys as _sys
from pathlib import Path as _Path

# Tests live in tests/ and import the application from the checkout root.
_sys.path.insert(0, str(_Path(__file__).resolve().parents[1]))

import asyncio
import os

os.environ.setdefault("AUTH_DISABLED", "true")

from app import rates

passed = failed = 0


def check(name: str, ok: bool, detail: str = "") -> None:
    global passed, failed
    if ok:
        passed += 1
        print(f"  OK   {name}" + (f"  {detail}" if detail else ""))
    else:
        failed += 1
        print(f"  FAIL {name}  {detail}")


def rec(problem="Consider purchasing a savings plan to unlock lower prices", term="P1Y",
        lookback="30", savings=10.0, annual=120.0, sku="", currency="USD"):
    return {"problem": problem, "solution": "", "impact": "Medium", "impactedType": "",
            "impactedName": "", "subscriptionId": "s", "resourceGroup": "",
            "savings": savings, "annualSavings": annual, "currency": currency,
            "term": term, "lookback": lookback, "skuName": sku, "displaySKU": sku,
            "quantity": 1, "region": "eastus"}


def run(recs, baseline=None, subs=("sub-a",)):
    async def fake_arg(query, subscriptions, top=1000):
        return list(recs)

    async def no_subs():
        return {"subscriptions": []}

    real_arg, real_list = rates._arg, rates.list_subscriptions
    rates._arg = fake_arg
    if not subs:
        rates.list_subscriptions = no_subs
    try:
        return asyncio.run(rates.commitment_comparison(
            list(subs), baseline_annual=baseline, baseline_currency="USD"))
    finally:
        rates._arg, rates.list_subscriptions = real_arg, real_list


RI = "Consider App Service reserved instance to save over the on-demand costs"
SP = "Consider purchasing a savings plan to unlock lower prices"

print("=" * 72)
print("the options, as alternatives")

r = run([rec(term="P1Y", annual=120.0), rec(term="P3Y", annual=300.0)])
check("both terms are offered", len(r["options"]) == 2)
check("the better one leads", r["best"]["term"] == "3 year" and r["best"]["annual_saving"] == 300.0)
check("terms are never summed",
      all(o["annual_saving"] in (120.0, 300.0) for o in r["options"]),
      "adding 1yr and 3yr for one workload describes a purchase nobody would make")
check("and the note says so", "not a saving to add" in (r.get("note") or ""))

print()
print("=" * 72)
print("a reservation is not a savings plan")

r = run([rec(problem=SP, term="P3Y", annual=300.0), rec(problem=RI, term="P3Y", annual=90.0)])
kinds = {o["kind"] for o in r["options"]}
check("both kinds are separated", kinds == {"savings plan", "reservation"},
      "a reservation buys a SKU in a region; a savings plan buys an hourly spend")

# `has` in KQL is token-based, so filtering server-side on 'reserv' silently returns nothing.
# That is why the match happens here, and why this case is worth pinning.
check("'reserved instance' is recognised as a reservation",
      rates._kind_of(RI) == "reservation", rates._kind_of(RI))
check("and a savings plan as a savings plan", rates._kind_of(SP) == "savings plan")

print()
print("=" * 72)
print("what Azure is not recommending")

r = run([rec(problem=SP, term="P1Y", annual=120.0)])
check("the missing kind is named, not shown as zero",
      r["not_recommended"] == ["reservation"],
      "a reservation row reading 0 reads as one worth nothing")

r = run([rec(problem=SP), rec(problem=RI)])
check("and nothing is named when both are present", r["not_recommended"] == [])

print()
print("=" * 72)
print("a recommendation that moves with the window")

r = run([rec(term="P1Y", lookback="30", annual=100.0),
         rec(term="P1Y", lookback="60", annual=270.0)])
one = r["options"][0]
check("the longest window is the headline", one["lookback_days"] == 60,
      "the most evidence Advisor has, and the least swayed by a quiet week")
check("the figure is that window's, not a blend", one["annual_saving"] == 270.0)
check("and the spread is reported", one["lookback_spread"] == 2.7,
      "measured live at 2.7x on this estate — a number to treat with caution, not commit to")

r = run([rec(term="P1Y", lookback="30", annual=100.0)])
check("one window alone claims no spread", r["options"][0]["lookback_spread"] is None)

print()
print("=" * 72)
print("summed across subscriptions, because a commitment covers an estate")

r = run([rec(term="P3Y", annual=100.0), rec(term="P3Y", annual=50.0)])
check("two subscriptions' recommendations add up for the same option",
      r["options"][0]["annual_saving"] == 150.0 and r["options"][0]["recommendations"] == 2,
      "one savings plan covers an estate, not a subscription")

print()
print("=" * 72)
print("against the estate's own bill")

r = run([rec(term="P3Y", annual=300.0)], baseline=1200.0)
check("the baseline is carried", r["baseline"]["annual"] == 1200.0)
check("and the saving is a fraction of it", r["options"][0]["saving_pct"] == 25.0)

r = run([rec(term="P3Y", annual=300.0)], baseline=None)
check("without a baseline it ranks but claims no percentage",
      r["options"][0]["saving_pct"] is None and r["baseline"]["annual"] is None,
      "Advisor reports the saving, not the bill")

print()
print("=" * 72)
print("nothing to compare")

r = run([])
check("no recommendations is an empty list, not an error",
      r["options"] == [] and r["best"] is None)
check("and both kinds are reported as not recommended",
      set(r["not_recommended"]) == {"savings plan", "reservation"})

r = run([rec()], subs=())
check("no subscriptions in scope says so", r.get("empty") is True)

print()
print("=" * 72)
print("the source is the scoped one")

check("Advisor is named as the source", run([rec()])["source"] == "Azure Advisor")

# A guard rather than a behaviour test. The tempting API returns a better-shaped answer to the
# same question, at billing-account scope, and the mistake would be invisible in any output that
# did not happen to be cross-checked against Advisor.
from pathlib import Path  # noqa: E402

src = (Path(__file__).resolve().parents[1] / "app" / "rates.py").read_text(encoding="utf-8")
# Explaining why not to call it is the point of the docstring, so only lines that would
# actually reach the API count — a URL, or a request going out.
calls = [ln.strip() for ln in src.splitlines()
         if "benefitRecommendations" in ln
         and ("management.azure.com" in ln or "request(" in ln or "_arm" in ln)]
check("benefitRecommendations is never called, only explained",
      not calls, "; ".join(calls)[:120])

print()
print("=" * 72)
print("three prices, side by side")

# A saving on its own cannot be compared with anything. "$300 a year" says nothing about whether
# that is a rounding error or a quarter of the bill, and nothing at all about what the estate
# would then cost — which is the number somebody is being asked to approve.
r = run([rec(problem=SP, term="P1Y", annual=300.0),
         rec(problem=RI, term="P3Y", annual=600.0)], baseline=2400.0)
models = {m["kind"]: m for m in r["models"]}
check("there is a column per pricing model",
      set(models) == {"pay-as-you-go", "savings plan", "reservation"},
      str(sorted(models)))
check("pay-as-you-go leads, as the baseline",
      r["models"][0]["baseline"] is True and r["models"][0]["annual_cost"] == 2400.0)
check("each option states what a year costs under it, not only what it saves",
      models["savings plan"]["annual_cost"] == 2100.0
      and models["reservation"]["annual_cost"] == 1800.0,
      "2400 - 300 and 2400 - 600")
check("the cheapest column is the best offer",
      r["best"]["kind"] == "reservation" and r["best"]["annual_saving"] == 600.0)
check("and the verdict says which, in words and in the estate's currency",
      "reserved instance" in r["verdict"] and "USD 600" in r["verdict"], r["verdict"])
check("the models carry what each one commits you to",
      models["savings plan"]["commits_to"] == "an hourly spend"
      and models["reservation"]["commits_to"] == "a specific SKU and region",
      "the difference being chosen between is not visible in the money column")

# Both terms stay attached to the model's column, so "1 year saves X, 3 year saves Y" needs no
# second request and does not become two rows describing one product.
r = run([rec(problem=RI, term="P1Y", annual=100.0),
         rec(problem=RI, term="P3Y", annual=250.0)], baseline=1000.0)
ri = next(m for m in r["models"] if m["kind"] == "reservation")
check("one column per product, with its terms inside it",
      len([m for m in r["models"] if m["kind"] == "reservation"]) == 1
      and len(ri["terms"]) == 2,
      "a reader chooses a product first and a term second")
check("and the column leads with the better term",
      ri["annual_saving"] == 250.0 and ri["term"] == "3 year")

# A product Advisor declined to offer is a finding. Omitting the column implies there were only
# ever two choices; showing it at zero implies it is worth nothing.
r = run([rec(problem=SP, term="P1Y", annual=120.0)], baseline=1000.0)
missing = next(m for m in r["models"] if m["kind"] == "reservation")
check("a model Azure will not recommend still gets a column, marked as such",
      missing["unavailable"] is True and missing["annual_cost"] is None
      and bool(missing.get("reason")))

r = run([], baseline=1000.0)
check("with nothing recommended the verdict says pay-as-you-go is the right answer",
      "pay-as-you-go is the right answer" in r["verdict"], r["verdict"])

r = run([rec(term="P3Y", annual=300.0)], baseline=None)
check("without a baseline no column claims an annual cost",
      all(m["annual_cost"] is None for m in r["models"]),
      "an annual cost with nothing to subtract from is a made-up number")


print()
print("=" * 72)
print(f"  {passed} passed, {failed} failed")
raise SystemExit(1 if failed else 0)
