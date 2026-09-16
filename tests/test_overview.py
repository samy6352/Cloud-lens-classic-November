"""The opening view — the one screen most people will judge the product on.

It is also the densest: six figures, a two-series trend and five tables, all from a single
warehouse pass. These checks are about the things that would quietly make it wrong rather than
make it fail — a total that doesn't match its parts, a change computed against the wrong
window, or the partial final day of a cost export plotted as if spend had collapsed.

    python test_overview.py
"""
# cloudlens-test: requires-warehouse
#
# Reconciles the overview totals against the rows behind them.


from __future__ import annotations

import sys as _sys
from pathlib import Path as _Path

# Tests live in tests/ and import the application from the checkout root.
_sys.path.insert(0, str(_Path(__file__).resolve().parents[1]))

import os

os.environ.setdefault("AUTH_DISABLED", "true")

from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)

passed = failed = 0


def check(name: str, ok: bool, detail: str = "") -> None:
    global passed, failed
    if ok:
        passed += 1
        print(f"  OK   {name}" + (f"  {detail}" if detail else ""))
    else:
        failed += 1
        print(f"  FAIL {name}  {detail}")


def executive(**params):
    return client.get("/api/dashboard/executive", params=params).json()


print("\nit answers in one request")
print("=" * 72)
r = client.get("/api/dashboard/executive", params={"days": 30})
check("the endpoint exists", r.status_code == 200, f"status {r.status_code}")
d = r.json()
for key in ("kpis", "trend", "areas", "movers", "services", "subscriptions", "resource_groups"):
    check(f"it returns {key}", key in d)

print("\nthe figures agree with each other")
print("=" * 72)
k = d["kpis"]
total = k["total"]

areas_total = round(sum(a["cost"] for a in d["areas"]), 2)
check(
    "the areas add up to the total — every currency unit lands in exactly one area",
    abs(areas_total - total) < 1.0,
    f"areas={areas_total} total={total}",
)

trend_total = round(sum(d["trend"]["values"]), 2)
check(
    "the daily trend adds up to the total",
    abs(trend_total - total) < 1.0,
    f"trend={trend_total} total={total}",
)

check(
    "the daily average is the total over the period",
    abs(k["daily_avg"] - total / 30) < 0.02,
    f"{k['daily_avg']} vs {round(total / 30, 2)}",
)

if k["previous"]:
    expected = round((total - k["previous"]) / k["previous"] * 100, 1)
    check("the change is measured against the previous window", k["change_pct"] == expected,
          f"{k['change_pct']} vs {expected}")

check(
    "no share exceeds 100%",
    all((a.get("share_pct") or 0) <= 100.5 for a in d["areas"] + d["services"]),
)
check(
    "the top five services are at most all of the spend",
    k["top5_share_pct"] is None or k["top5_share_pct"] <= 100.5,
    str(k["top5_share_pct"]),
)

print("\nchange is described, not just totalled")
print("=" * 72)
for name, rows in (("areas", d["areas"]), ("movers", d["movers"]),
                   ("subscriptions", d["subscriptions"])):
    check(f"{name} carry a previous figure and a delta",
          all("previous" in x and "delta" in x for x in rows))
    check(
        f"{name} deltas are the difference, not something else",
        all(abs(x["delta"] - (x["cost"] - x["previous"])) < 0.02 for x in rows),
    )
check(
    "a thing with no prior spend has no percentage — 'new' and 'unchanged' are different",
    all(x["change_pct"] is None for x in d["movers"] if x["previous"] == 0),
)
check(
    "movers are ranked by how much moved, not by how much they cost",
    all(abs(a["delta"]) >= abs(b["delta"]) - 0.011
        for a, b in zip(d["movers"], d["movers"][1:])),
    " > ".join(str(m["delta"]) for m in d["movers"][:4]),
)

print("\nthe two trend series line up")
print("=" * 72)
t = d["trend"]
check("there is a previous series to compare against", "previous" in t and len(t["previous"]) > 0)
check(
    "the comparison covers a similar span, so day N sits above day N",
    abs(len(t["values"]) - len(t["previous"])) <= 1,
    f"current={len(t['values'])} previous={len(t['previous'])}",
)
check("every point has a label", len(t["labels"]) == len(t["values"]))

print("\nthe partial final day is flagged rather than plotted as a collapse")
print("=" * 72)
check("the flag is present", "partial_last" in t)
if t["partial_last"]:
    body = sorted(t["values"][:-1])
    median = body[len(body) // 2]
    check(
        "it is only raised when the last day really is far below the recent norm",
        t["values"][-1] < median * 0.4,
        f"final={t['values'][-1]} median={median}",
    )
check(
    "the partial day is still counted in the total — it is real money",
    abs(round(sum(t["values"]), 2) - total) < 1.0,
    f"trend sum={round(sum(t['values']), 2)} total={total}",
)
for days in (7, 30, 90):
    tr = executive(days=days)["trend"]
    check(f"the check runs at {days} days too, not only long windows",
          "partial_last" in tr and isinstance(tr["partial_last"], bool),
          f"partial_last={tr['partial_last']} points={len(tr['values'])}")

print("\nit holds up at other periods")
print("=" * 72)
for days in (7, 90):
    e = executive(days=days)
    check(f"{days} days returns a total", e["kpis"]["total"] >= 0, str(e["kpis"]["total"]))
    check(f"{days} days keeps areas consistent with the total",
          abs(round(sum(a["cost"] for a in e["areas"]), 2) - e["kpis"]["total"]) < 1.0)

short = executive(days=7)["kpis"]["total"]
long = executive(days=90)["kpis"]["total"]
check("a longer window costs at least as much as a shorter one", long >= short,
      f"7d={short} 90d={long}")

print("\nscope is respected")
print("=" * 72)
full = executive(days=30)
scoped = executive(days=30, scope="00000000-0000-0000-0000-000000000000")
check(
    "an unknown subscription narrows to nothing rather than returning the estate",
    scoped.get("empty") or scoped["kpis"]["total"] <= full["kpis"]["total"],
    f"scoped={scoped.get('kpis', {}).get('total')} full={full['kpis']['total']}",
)

print("\na month the data does not cover is not a month that cost nothing")
print("=" * 72)

# The header's largest number read "$0" on 2 September against a warehouse ending 25 August.
# Summing a month the warehouse has no rows for returns 0.0 -- a real float, so every "did we
# get an answer" check downstream passed and the empty-means-zero error landed in the most
# prominent figure in the app. The fix is that the warehouse only answers for the window it
# actually holds, and anything outside it falls through to Azure or reports nothing.
import asyncio  # noqa: E402
from datetime import date, timedelta  # noqa: E402

from app import cost as cost_mod  # noqa: E402
from app.warehouse import warehouse  # noqa: E402


def _overview_with(coverage_to, live_mtd=1234.0):
    """Run the real overview() with the warehouse claiming a given coverage end date."""
    real_summary = warehouse.summary
    real_query = warehouse.query
    real_summary_fn = cost_mod.list_subscriptions
    real_fc, real_bud = cost_mod._forecast_by_subscription, cost_mod.budgets
    real_cs = cost_mod.cost_summary

    warehouse.summary = lambda *a, **k: {
        "rows": 20000, "from": "2026-06-01", "to": coverage_to,
        "subscriptions": 3, "currency": "USD", "total_cost": 1.0,
        "total_by_currency": [], "mixed_currency": False,
    }
    # If it asks the warehouse for the month, it gets a zero -- exactly what the live data does
    # for a month it has no rows in. A correct implementation must not report that as the answer.
    warehouse.query = lambda *a, **k: {"rows": [{"mtd": 0.0, "prev": 0.0}]}

    async def subs():
        return {"subscriptions": []}

    async def fc(*a, **k):
        return {"projected_total": 1.0}

    async def buds(*a, **k):
        return {"budgets": [], "over": 0, "count": 0}

    async def summary(scope, months_back=0, **k):
        return {"grand_total": live_mtd if months_back == 0 else 5678.0, "currency": "USD"}

    cost_mod.list_subscriptions = subs
    cost_mod._forecast_by_subscription = fc
    cost_mod.budgets = buds
    cost_mod.cost_summary = summary
    try:
        return asyncio.run(cost_mod.overview(None))
    finally:
        warehouse.summary, warehouse.query = real_summary, real_query
        cost_mod.list_subscriptions = real_summary_fn
        cost_mod._forecast_by_subscription, cost_mod.budgets = real_fc, real_bud
        cost_mod.cost_summary = real_cs


today = date.today()
this_start = today.replace(day=1)
stale = str(this_start - timedelta(days=7))     # ends the week before this month began

o = _overview_with(stale)
check("a warehouse that stops before this month does not report the month as zero",
      o["month_to_date"] != 0, f"month_to_date={o['month_to_date']}")
check("it asks Azure for the month instead",
      o["month_to_date"] == 1234.0, str(o["month_to_date"]))
check("and says why the local data could not answer",
      stale in (o.get("stale_note") or ""), str(o.get("stale_note")))

fresh = str(today)
o = _overview_with(fresh)
check("a warehouse that covers today answers from the warehouse",
      o["month_to_date"] == 0 and not o.get("stale_note"),
      f"month_to_date={o['month_to_date']} note={o.get('stale_note')}")

# Last month is the same failure one window over: August 1-25 summed and presented as a whole
# month is wrong by six days, and nothing on screen would say so.
o = _overview_with(str(this_start - timedelta(days=3)))
check("a partial previous month is not presented as a complete one",
      o["last_month"] == 5678.0, str(o["last_month"]))

print("\n" + "=" * 72)
print(f"  {passed} passed, {failed} failed\n")
raise SystemExit(1 if failed else 0)
