"""Commitments: reservations, savings plans and Spot — paying a different rate for the same thing.

Three ways to pay less without using less, and one view over all of them:

  * **Reservations** — commit to a specific SKU in a region for one or three years.
  * **Savings plans** — commit to an hourly spend across compute, more flexible, less discount.
  * **Spot** — take surplus capacity at a steep discount, accept eviction.

Everything here reads the warehouse, so it is milliseconds and answers for exactly the
subscriptions the caller is entitled to. What it can say depends on how the warehouse was
built: an **amortized** ingest spreads a reservation purchase across the term it covers and
attributes usage to the resources that consumed it, which is what makes "what did the
commitment actually save" answerable. An **actual-cost** ingest bills the purchase as one lump
on the day it happened, so the same question has no honest answer — and this module says so
rather than showing a chart of zeroes.
"""
from __future__ import annotations

import logging
from typing import Any

log = logging.getLogger("cloudlens.commitments")

# How Azure labels the pricing model. FOCUS and the legacy exports disagree, and a reservation
# appears as "Reservation" in one and "Committed" in another, so match on a set rather than a
# string and keep the vocabulary in one place.
COMMITTED = ("reservation", "committed", "savingsplan", "savings plan", "commitmentdiscount")
SPOT = ("spot",)
ON_DEMAND = ("ondemand", "on demand", "payg", "standard")

# How many resources the drill-down under one commitment shows before folding the rest into a
# count. A savings plan spanning a whole compute family can cover hundreds of resources — the
# point of the drill-down is "what does this actually pay for", which a top-cost handful answers
# as well as the full list would, at a fraction of the payload.
MAX_RESOURCES_SHOWN = 8

# How many drill-down rows the resource query is allowed to return in total, across every
# commitment. This has to be stated to PostgreSQL rather than applied after the fact: the query
# groups every covered resource and orders by cost, so without a LIMIT the server sorts the whole
# estate and the reader throws almost all of it away. Measured on the live estate, the same 4000
# rows cost 34.3s fetched from an unbounded sort and 1.65s from a bounded one.
MAX_RESOURCE_ROWS = 4000


def _bucket(pricing_model: str | None) -> str:
    """Group a raw PricingModel value into something a person would recognise."""
    low = str(pricing_model or "").strip().lower().replace("_", "")
    if not low:
        return "unknown"
    if any(k in low for k in SPOT):
        return "spot"
    if any(k in low for k in COMMITTED):
        return "committed"
    if any(k in low for k in ON_DEMAND):
        return "on demand"
    return "other"


class Commitments:
    """Reads the warehouse. Holds no state."""

    def __init__(self) -> None:
        from .warehouse import warehouse

        self.wh = warehouse

    def coverage(self, scope: list[str] | None, days: int = 30,
                 currency: str | None = None) -> dict[str, Any]:
        """How spend splits across on-demand, committed and Spot rates."""
        from . import reporting

        days = reporting.period_days(days)
        with self.wh.reader(scope, currency=currency, include_commitments=True) as r:
            agg = r.agg
            hi = r.rows(f'SELECT max("ChargePeriodStart") AS hi FROM {agg}')
            hi = hi[0]["hi"] if hi and hi[0]["hi"] else None
            if not hi:
                return {"empty": True, "days": days}

            start = reporting.start_sql(hi, days)
            period = f'"ChargePeriodStart" > {start}'

            split = r.rows(
                'SELECT coalesce("PricingModel", \'\') AS model, '
                '"BillingCurrency" AS currency, '
                'sum("BilledCost") AS cost '
                f"FROM {agg} WHERE {period} GROUP BY 1, 2", None)

            # Compute is where commitments actually apply, so the headline percentage is about
            # compute rather than total spend — a storage-heavy estate would otherwise look
            # permanently uncovered no matter how many VMs were reserved.
            compute = r.rows(
                'SELECT coalesce("PricingModel", \'\') AS model, '
                '"BillingCurrency" AS currency, sum("BilledCost") AS cost '
                f"FROM {agg} WHERE " + period + " AND ("
                '"ServiceFamily" = \'Compute\' OR "ServiceName" ILIKE \'%virtual machine%\''
                ") GROUP BY 1, 2", None)

            # Grouped byte for byte, and the distinct resource count with it. Benefit names and
            # resource ids are long strings that share their leading characters, and under the
            # live server's `en_US.utf8` collation grouping them took 65.7s of this statement
            # against 5.7s byte-wise — the same cost `a1bc0cd` removed from Uptime and Tags.
            # Equality is identical under both collations because both are deterministic, so
            # the groups and the counts cannot differ; measured on the live estate, both
            # spellings return the same 43 rows. Nothing here joins or tie-breaks on text —
            # `ORDER BY cost` is numeric — so the keys never need casting back.
            benefits = r.rows(
                'SELECT "BenefitName" COLLATE "C" AS name, '
                'coalesce("PricingModel", \'\') AS model, '
                '"BillingCurrency" AS currency, sum("BilledCost") AS cost, '
                'count(DISTINCT "ResourceId" COLLATE "C") AS resources '
                f'FROM {r.benefit} WHERE {period} AND "BenefitName" IS NOT NULL '
                "AND \"BenefitName\" <> '' GROUP BY 1, 2, 3 ORDER BY cost DESC", None)

            monthly = r.rows(
                'SELECT strftime("ChargePeriodStart", \'%Y-%m\') AS month, '
                'coalesce("PricingModel", \'\') AS model, '
                '"BillingCurrency" AS currency, '
                f'sum("BilledCost") AS cost FROM {agg} GROUP BY 1, 2, 3 ORDER BY 1', None)

        def fold(rows: list[dict]) -> tuple[dict[str, float], float]:
            out: dict[str, float] = {}
            for row in rows:
                out[_bucket(row["model"])] = out.get(_bucket(row["model"]), 0.0) + float(
                    row.get("cost") or 0.0
                )
            return {k: round(v, 2) for k, v in out.items()}, round(sum(out.values()), 2)

        from .currency import currency_code

        def code(row: dict) -> str:
            return currency_code(row.get("currency")) or "unknown"

        codes = {code(row) for row in split}
        single = next(iter(codes)) if len(codes) == 1 and "unknown" not in codes else None

        def pct(part: float, whole: float) -> float | None:
            return round(part / whole * 100, 1) if whole else None

        # Month on month, so a coverage change is visible rather than just today's snapshot.
        by_currency: dict[str, dict[str, Any]] = {}
        for cur in sorted(codes):
            all_rows = [row for row in split if code(row) == cur]
            compute_rows = [row for row in compute if code(row) == cur]
            all_split, total = fold(all_rows)
            compute_split, compute_total = fold(compute_rows)

            def status(values: dict[str, float]) -> str:
                known = set(values) - {"unknown", "other"}
                unknown = bool(set(values) & {"unknown", "other"})
                return "unknown" if not known else "partial" if unknown else "known"

            overall_status, compute_status = status(all_split), status(compute_split)
            by_currency[cur] = {
                "currency": cur if cur != "unknown" else None,
                "total": total if cur != "unknown" else None,
                "split": all_split, "compute_total": compute_total if cur != "unknown" else None,
                "compute_split": compute_split,
                "coverage_pct": pct(all_split.get("committed", 0.0), total)
                                if overall_status == "known" and cur != "unknown" else None,
                "compute_coverage_pct": pct(compute_split.get("committed", 0.0), compute_total)
                                        if compute_status == "known" and cur != "unknown" else None,
                "coverage_status": overall_status, "compute_coverage_status": compute_status,
                "spot_pct": pct(all_split.get("spot", 0.0), total)
                            if overall_status == "known" and cur != "unknown" else None,
            }

        by_month: dict[tuple[str, str], dict[str, float]] = {}
        for row in monthly:
            cur = currency_code(row.get("currency")) or "unknown"
            m = by_month.setdefault((cur, str(row["month"])), {})
            b = _bucket(row["model"])
            m[b] = m.get(b, 0.0) + float(row.get("cost") or 0.0)
        trend_by_currency: dict[str, list[dict[str, Any]]] = {}
        for (cur, month), vals in sorted(by_month.items()):
            trend_by_currency.setdefault(cur, []).append({
                "month": month,
                "currency": cur if cur != "unknown" else None,
                "unpriced": cur == "unknown",
                "on_demand": round(vals.get("on demand", 0.0), 2) if cur != "unknown" else None,
                "committed": round(vals.get("committed", 0.0), 2) if cur != "unknown" else None,
                "spot": round(vals.get("spot", 0.0), 2) if cur != "unknown" else None,
                "total": round(sum(vals.values()), 2) if cur != "unknown" else None,
                "coverage_pct": pct(vals.get("committed", 0.0), sum(vals.values()))
                                if not (set(vals) & {"unknown", "other"}) and cur != "unknown" else None,
            })
        flat = by_currency.get(single, {
            "total": None, "compute_total": None, "split": {}, "compute_split": {},
            "coverage_pct": None, "compute_coverage_pct": None, "spot_pct": None,
            "coverage_status": "unknown", "compute_coverage_status": "unknown",
        })

        return {
            "days": days,
            "as_of": str(hi),
            **flat,
            "currency": single,
            "mixed_currency": len(codes) > 1,
            "history_partial": "unknown" in trend_by_currency,
            "history_note": "Historical currency is unavailable for some billing rows."
                            if "unknown" in trend_by_currency else None,
            "by_currency": by_currency,
            "total_by_currency": {cur: value["total"] for cur, value in by_currency.items()},
            "benefits": [
                {
                    "name": b["name"],
                    "model": _bucket(b["model"]),
                    "cost": round(float(b.get("cost") or 0), 2),
                    "resources": int(b.get("resources") or 0),
                    "currency": b.get("currency"),
                }
                for b in benefits
            ],
            "trend": trend_by_currency.get(single, [])[-13:],
            "trend_by_currency": {cur: values[-13:] for cur, values in trend_by_currency.items()},
            # Whether the warehouse can answer the commitment questions at all. Stated as data
            # rather than inferred in the UI, so the reason travels with the numbers.
            "amortized": bool(benefits) or any(
                _bucket(s["model"]) == "committed" for s in split
            ),
        }

    def inventory(self, scope: list[str] | None, days: int = 90,
                  currency: str | None = None) -> dict[str, Any]:
        """Every reservation and savings plan the billing data shows in use.

        Deliberately built from billing rather than from `Microsoft.Capacity/reservationOrders`.
        The orders API is the obvious source and is unavailable to most callers: it is scoped to
        the billing account, not the subscription, so a person with Owner across a whole estate
        is still refused — verified against this tenant, where it returns 403 for a Global
        Administrator. Asking billing instead needs no permission the app does not already have.

        It also answers a slightly different and more useful question. The orders API lists what
        was *bought*; this lists what is being *consumed*, in the subscriptions the caller may
        see. A reservation somebody bought and nothing is using does not appear here, which is
        itself worth knowing — and is exactly what the unused-commitment gap below reports.

        Term length is not in billing data. It is left null rather than guessed, because "1 year"
        printed beside a three-year commitment is worse than no answer.
        """
        from . import reporting

        days = reporting.period_days(days)
        with self.wh.reader(scope, currency=currency, include_commitments=True) as r:
            hi = r.rows(f'SELECT max("ChargePeriodStart") AS hi FROM {r.agg}')
            hi = hi[0]["hi"] if hi and hi[0]["hi"] else None
            if not hi:
                return {"empty": True, "days": days, "commitments": []}

            period = f'"ChargePeriodStart" > {reporting.start_sql(hi, days)}'

            # Byte-order grouping, for the reason set out in `coverage` above: the benefit id is
            # a full ARM resource path and the distinct-resource count is over resource ids, so
            # this one statement spent 70.6s in libc string comparison on the live estate and
            # 7.9s byte-wise — the same 81 rows either way. Deterministic collations agree on
            # equality, and nothing downstream joins or orders on these keys.
            rows = r.rows(
                'SELECT "BenefitId" COLLATE "C" AS id, "BenefitName" COLLATE "C" AS name, '
                'coalesce("PricingModel", \'\') AS model, '
                '"BillingCurrency" AS currency, '
                'sum("BilledCost") AS cost, '
                'count(DISTINCT "ResourceId" COLLATE "C") AS resources, '
                'count(DISTINCT "SubAccountId" COLLATE "C") AS subscriptions, '
                'min("ChargePeriodStart") AS first_seen, '
                'max("ChargePeriodStart") AS last_seen '
                f'FROM {r.benefit} WHERE {period} '
                'AND coalesce("BenefitId", "BenefitName") IS NOT NULL '
                'AND coalesce("BenefitId", "BenefitName") <> \'\' '
                "GROUP BY 1, 2, 3, 4 ORDER BY cost DESC", None)

            # What each commitment is actually being spent on. Two services under one savings
            # plan is the normal case and the reason a savings plan was chosen over a
            # reservation, so it belongs in the row rather than behind a drill-down.
            services = r.rows(
                'SELECT coalesce("BenefitId", "BenefitName") AS id, "ServiceName" AS service, '
                'sum("BilledCost") AS cost '
                f'FROM {r.benefit} WHERE {period} '
                'AND coalesce("BenefitId", "BenefitName") IS NOT NULL '
                'AND coalesce("BenefitId", "BenefitName") <> \'\' '
                "GROUP BY 1, 2 ORDER BY cost DESC", None)

            # Which resources are actually covered, for the drill-down beneath each row. Bounded
            # in SQL at the same number the reader keeps, so the two cannot drift: `rows(..., n)`
            # fetches n rows but the server had already sorted every group to produce them, which
            # is the whole cost of this statement. The per-commitment share is still taken in
            # Python (`MAX_RESOURCES_SHOWN`) — a LIMIT cannot give each commitment a fair slice,
            # and this cap is a ceiling on the sort, not a per-commitment budget.
            #
            # Unlike the two statements above, byte-order grouping does *not* help here and was
            # measured making it worse (9.8s → 22.6s): the collation changes the plan rather than
            # just the comparison cost. Bounding the sort is the lever; the collation is not.
            resource_rows = r.rows(
                'SELECT coalesce("BenefitId", "BenefitName") AS id, '
                'coalesce("ResourceName", "ResourceId") AS name, '
                'coalesce("ResourceGroup", \'—\') AS grp, '
                'sum("BilledCost") AS cost '
                f'FROM {r.benefit} WHERE {period} '
                'AND coalesce("BenefitId", "BenefitName") IS NOT NULL '
                'AND coalesce("BenefitId", "BenefitName") <> \'\' '
                'AND "ResourceId" IS NOT NULL '
                f"GROUP BY 1, 2, 3 ORDER BY cost DESC LIMIT {MAX_RESOURCE_ROWS}",
                MAX_RESOURCE_ROWS)

        by_id: dict[str, list[str]] = {}
        for row in services:
            key = str(row["id"])
            if len(by_id.setdefault(key, [])) < 4:
                by_id[key].append(str(row["service"]))

        # Same shape as `by_id` above, but the resources themselves rather than just their
        # service names — capped per commitment so a savings plan covering hundreds of VMs
        # does not turn its row into the whole inventory.
        resources_by_id: dict[str, list[dict[str, Any]]] = {}
        for row in resource_rows:
            key = str(row["id"])
            bucket = resources_by_id.setdefault(key, [])
            if len(bucket) < MAX_RESOURCES_SHOWN:
                bucket.append({"name": str(row["name"]), "resource_group": str(row["grp"]),
                               "cost": round(float(row.get("cost") or 0), 2)})

        items = []
        for row in rows:
            model = _bucket(row["model"])
            raw = str(row["model"] or "").lower().replace("_", "").replace(" ", "")
            # Reservation and savings plan are both "committed" spend and are not the same
            # purchase: one is a specific SKU in a region, the other an hourly spend commitment
            # anywhere in a family. Advising on them together would recommend replacing a thing
            # with itself.
            kind = ("savings plan" if "savingsplan" in raw
                    else "reservation" if "reservation" in raw
                    # A bare "CommitmentDiscount" names neither. It is FOCUS's category rather
                    # than its type, and some exports carry it in the field the type usually
                    # occupies. Called a commitment rather than forced into whichever of the two
                    # it resembles: guessing here produces advice about a purchase the customer
                    # may not hold.
                    else "commitment" if "commitment" in raw or "committed" in raw
                    else model)
            key = str(row["id"] or row["name"])
            resource_count = int(row.get("resources") or 0)
            shown = resources_by_id.get(key, [])
            items.append({
                "id": row["id"],
                "name": row["name"] or row["id"],
                "kind": kind,
                "cost": round(float(row.get("cost") or 0), 2),
                "currency": row.get("currency"),
                "resources": resource_count,
                "subscriptions": int(row.get("subscriptions") or 0),
                "first_seen": str(row["first_seen"]) if row.get("first_seen") else None,
                "last_seen": str(row["last_seen"]) if row.get("last_seen") else None,
                "services": by_id.get(key, []),
                # The drill-down: which resources this commitment is actually covering, ranked
                # by what they cost against it, and how many more there are beyond the cap.
                "resources_top": shown,
                "resources_more": max(0, resource_count - len(shown)),
                # Not derivable from billing. Null rather than a guess.
                "term": None,
                # A commitment still being charged for but attached to nothing is the one this
                # view exists to surface: it is money leaving every month for no workload.
                "idle": resource_count == 0,
            })

        from .currency import currency_code

        totals_by_currency: dict[str, dict[str, float]] = {}
        for it in items:
            cur = currency_code(it.get("currency")) or "unknown"
            totals = totals_by_currency.setdefault(cur, {})
            totals[it["kind"]] = round(totals.get(it["kind"], 0.0) + it["cost"], 2)
        codes = set(totals_by_currency)
        single = next(iter(codes)) if len(codes) == 1 and "unknown" not in codes else None

        return {
            "days": days,
            "as_of": str(hi),
            "currency": single,
            "mixed_currency": len(codes) > 1,
            "commitments": items[:200],
            "count": len(items),
            "returned_count": min(len(items), 200),
            "truncated": len(items) > 200,
            "totals": totals_by_currency.get(single, {}),
            "totals_by_currency": totals_by_currency,
            "inventory_basis": "observed_billing_usage",
            "idle": [i["name"] for i in items if i["idle"]],
            # Said as data rather than left for the UI to infer, so the caveat travels with the
            # numbers wherever they are exported to.
            "basis": ("Read from billing data, so this is what your subscriptions are consuming "
                      "rather than everything the billing account has purchased. A commitment "
                      "bought but used by nothing in scope will not appear."),
            "term_note": ("Term length and utilisation percentage are not in billing data. They "
                          "need Microsoft.Capacity/reservationOrders at billing-account scope, "
                          "which subscription-level access does not include."),
        }

    def spot_savings(self, scope: list[str] | None, days: int = 90,
                     currency: str | None = None) -> dict[str, Any]:
        """What Spot is saving, by comparing its rate to on-demand for the same meter.

        The comparison is per meter and per month: the same VM size bought Spot and on-demand
        in the same month gives a like-for-like unit price, and the gap times the Spot quantity
        is the saving. Where a meter has no on-demand rate to compare against, it is left out
        rather than valued at a guess.

        Converting inside the reader keeps the like-for-like property intact: both sides of every
        comparison move by the same rate, so the saving is the same number in different units.
        """
        from . import reporting
        from .currency import currency_code

        days = reporting.period_days(days)
        with self.wh.reader(scope, currency=currency, include_commitments=True) as r:
            edge = r.rows(f'SELECT max("ChargePeriodStart") AS hi FROM {r.agg}', 1)
            hi = edge[0]["hi"] if edge else None
            if not hi:
                return {"empty": True, "days": days, "reason": "No billing data is available."}
            period = f'"ChargePeriodStart" > {reporting.start_sql(hi, days)}'
            rows = r.rows(
                'SELECT strftime("ChargePeriodStart", \'%Y-%m\') AS month, '
                '"MeterName" AS meter, coalesce("PricingModel", \'\') AS model, '
                '"BillingCurrency" AS currency, '
                'lower(trim(coalesce("RegionName", \'\'))) AS region, '
                'lower(trim(coalesce("UnitOfMeasure", \'\'))) AS unit, '
                'sum("BilledCost") AS cost, sum("PricingQuantity") AS qty '
                f'FROM {r.spot} WHERE "PricingQuantity" > 0 AND {period} '
                "GROUP BY 1, 2, 3, 4, 5, 6", None)

        spot_rows = [x for x in rows if _bucket(x["model"]) == "spot"]
        if not spot_rows:
            return {"empty": True, "days": days, "reason": "No Spot usage in the selected period."}

        base: dict[tuple, tuple[float, float]] = {}

        def identity(x: dict) -> tuple:
            return (x["month"], x["meter"], currency_code(x.get("currency")),
                    str(x.get("region") or "").lower(), str(x.get("unit") or "").lower())

        for x in rows:
            if _bucket(x["model"]) != "on demand":
                continue
            key = identity(x)
            if not all(key):
                continue
            cost, qty = base.get(key, (0.0, 0.0))
            base[key] = (cost + float(x.get("cost") or 0), qty + float(x.get("qty") or 0))

        by_month: dict[tuple[str, str], dict[str, float]] = {}
        priced, unpriced = 0, 0
        for x in spot_rows:
            month = str(x["month"])
            code = currency_code(x.get("currency")) or "unknown"
            spot_cost = float(x.get("cost") or 0)
            spot_qty = float(x.get("qty") or 0)
            m = by_month.setdefault((code, month), {
                "spot_cost": 0.0, "priced_spot_cost": 0.0, "would_have_cost": 0.0, "unpriced": 0})
            m["spot_cost"] += spot_cost

            ref = base.get(identity(x))
            if ref and ref[1] > 0 and ref[0] >= 0 and spot_qty > 0:
                m["would_have_cost"] += (ref[0] / ref[1]) * spot_qty
                m["priced_spot_cost"] += spot_cost
                priced += 1
            else:
                m["unpriced"] += 1
                unpriced += 1

        trend_by_currency: dict[str, list[dict[str, Any]]] = {}
        for (code, month), v in sorted(by_month.items()):
            saved = v["would_have_cost"] - v["priced_spot_cost"]
            trend_by_currency.setdefault(code, []).append({
                "month": month,
                "spot_cost": round(v["spot_cost"], 2),
                "would_have_cost": round(v["would_have_cost"], 2) if not v["unpriced"] else None,
                "saved": round(saved, 2) if not v["unpriced"] else None,
                "priced_saving": round(saved, 2),
                "unpriced_meters": int(v["unpriced"]),
                "saved_pct": (
                    round(saved / v["would_have_cost"] * 100, 1)
                    if v["would_have_cost"] and not v["unpriced"] else None
                ),
            })
        codes = set(trend_by_currency)
        single = next(iter(codes)) if len(codes) == 1 and "unknown" not in codes else None
        summaries = {
            code: {
                "total_saved": round(sum(t["saved"] for t in trend), 2)
                               if code != "unknown" and all(t["saved"] is not None for t in trend) else None,
                "priced_saving": round(sum(t["priced_saving"] for t in trend), 2),
                "total_spot_cost": round(sum(t["spot_cost"] for t in trend), 2)
                                   if code != "unknown" else None,
            } for code, trend in trend_by_currency.items()
        }

        return {
            "days": days, "as_of": str(hi),
            "trend": trend_by_currency.get(single, []),
            "trend_by_currency": trend_by_currency,
            "total_saved": summaries.get(single, {}).get("total_saved"),
            "total_spot_cost": summaries.get(single, {}).get("total_spot_cost"),
            "totals_by_currency": summaries,
            "currency": single,
            "mixed_currency": len(codes) > 1,
            "partial": bool(unpriced),
            "meters_priced": priced,
            "meters_unpriced": unpriced,
            "note": (
                "Saving is the gap between the Spot rate and the on-demand rate for the same "
                "meter, region, unit and currency in the selected month/window. Savings for "
                "meters without a confirmed on-demand reference are unavailable."
            ),
        }


commitments: Commitments | None = None


def get_commitments() -> Commitments:
    global commitments
    if commitments is None:
        commitments = Commitments()
    return commitments
