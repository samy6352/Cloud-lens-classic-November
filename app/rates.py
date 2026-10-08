"""Rate optimisation: paying less for the same resources, rather than using fewer of them.

The rest of CloudLens reduces *usage*. This reduces the *rate* — reservations and savings plans,
where you commit to a level of spend and Azure discounts it.

Everything here comes from **Azure Advisor**, read through Resource Graph, which is what the
built-in Cost Optimization workbook does. That choice matters more than it looks. The raw
Consumption `benefitRecommendations` API returns figures scoped to the whole *billing account*:
on this estate it reports $3.99M of eligible spend and $1.5M of savings, which is thousands of
other people's subscriptions. Presenting that to someone who can see one subscription would be
wrong by three orders of magnitude, and would leak aggregate billing data straight through the
RBAC boundary the rest of the app enforces.

Advisor's version of the same recommendation is scoped to the subscription and attributed to
the person asking. Smaller numbers, but they are *their* numbers.

Everything here is read-only.
"""
from __future__ import annotations

import logging
from typing import Any

from .cost import list_subscriptions
from .waste import _arg

log = logging.getLogger("cloudlens.rates")

# Advisor labels these by the text of the recommendation rather than a stable enum, so match on
# the shape of the problem statement. Kept here rather than inline so a wording change is one
# edit and shows up in a diff.
COMMITMENT_PATTERNS = (
    "savings plan",
    "reserved instance",
    "reservation",
)

ADVISOR_COST_QUERY = """
    advisorresources
    | where type =~ 'microsoft.advisor/recommendations'
    | where tostring(properties.category) == 'Cost'
    | extend props = properties, ext = properties.extendedProperties
    | project
        problem      = tostring(props.shortDescription.problem),
        solution     = tostring(props.shortDescription.solution),
        impact       = tostring(props.impact),
        impactedType = tostring(props.impactedField),
        impactedName = tostring(props.impactedValue),
        subscriptionId,
        resourceGroup,
        savings      = ext.savingsAmount,
        currency     = tostring(ext.savingsCurrency),
        annualSavings= ext.annualSavingsAmount,
        term         = tostring(ext.term),
        lookback     = tostring(ext.lookbackPeriod),
        skuName      = tostring(ext.displaySKU),
        quantity     = ext.displayQty,
        region       = tostring(ext.region),
        scopeHint    = tostring(ext.scope)
"""


def _num(v: Any) -> float | None:
    try:
        return round(float(v), 2)
    except (TypeError, ValueError):
        return None


def _is_commitment(problem: str) -> bool:
    low = (problem or "").lower()
    return any(p in low for p in COMMITMENT_PATTERNS)


def _lookback_days(v: Any) -> int:
    try:
        return int(str(v).strip())
    except (TypeError, ValueError):
        return 0


def _best_per_option(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """One row per distinct decision, rather than one per Advisor lookback window.

    Advisor publishes the same commitment recommendation evaluated over 7, 30 and 60 days. All
    three describe the same purchase, so showing them together inflates a handful of choices
    into a wall of near-duplicates and makes the list read as more opportunity than exists.

    Keyed by the recommendation text, the term and the subscription — the things that actually
    distinguish one decision from another. The longest lookback wins: it rests on the most
    evidence and is least distorted by one unusually quiet or busy week.
    """
    best: dict[tuple, dict[str, Any]] = {}
    for it in items:
        key = (it.get("problem"), it.get("term"), it.get("subscriptionId"), it.get("sku"))
        prev = best.get(key)
        if prev is None or it["lookback_days"] > prev["lookback_days"]:
            best[key] = it
    return list(best.values())


def _kind_of(problem: str) -> str:
    """Savings plan or reservation, from Advisor's wording.

    They are different purchases and the comparison exists to choose between them: a reservation
    buys a specific SKU in a region, a savings plan buys an hourly spend anywhere in a family.
    Matched in Python rather than in KQL because `has` is token-based — `has 'reserv'` matches
    nothing at all, which silently drops every reservation from the result.
    """
    low = (problem or "").lower()
    if "savings plan" in low:
        return "savings plan"
    if "reserv" in low:
        return "reservation"
    return "other"


TERM_LABEL = {"P1Y": "1 year", "P3Y": "3 year"}


async def commitment_comparison(subscription_ids: list[str] | None = None,
                                baseline_annual: float | None = None,
                                baseline_days: int | None = None, *,
                                baseline_currency: str | None = None,
                                display_currency: str | None = None,
                                baseline_by_currency: dict[str, float] | None = None,
                                baseline_unavailable: dict[str, str] | None = None) -> dict[str, Any]:
    """Pay-as-you-go against each commitment Azure will actually recommend for this estate.

    From Advisor, for the reason the module docstring gives at length: the Cost Management
    `benefitRecommendations` API answers the same question with billing-account-wide figures
    even when asked at subscription scope — measured on this estate at $4.0M of eligible spend
    against Advisor's $20-$79 — so presenting it would be wrong by three orders of magnitude and
    would carry other subscriptions' aggregates through the RBAC boundary the rest of the app
    enforces.

    Options are alternatives, never a total. A one-year and a three-year recommendation for the
    same workload are two ways to buy the same thing, and adding them describes a purchase
    nobody would make.

    `baseline_annual` is the estate's own on-demand run rate, supplied by the caller from the
    warehouse because that is where a correctly-scoped figure lives. Without it the table still
    ranks the options; it just cannot say what fraction of the bill they remove.
    `baseline_by_currency` offers each native currency's figure for a mixed-currency estate,
    and `baseline_unavailable` says why a currency (or, under "", the scope) has none.
    """
    subs = subscription_ids or [s["id"] for s in (await list_subscriptions())["subscriptions"]]
    if not subs:
        return {"empty": True, "reason": "No subscriptions in scope."}

    rows = await _arg(ADVISOR_COST_QUERY, subs, top=1000)
    return _comparison(rows, subs, baseline_annual, baseline_days,
                       baseline_currency, display_currency,
                       baseline_by_currency=baseline_by_currency,
                       baseline_unavailable=baseline_unavailable)


def _comparison(rows: list[dict[str, Any]], subs: list[str],
                baseline_annual: float | None, baseline_days: int | None,
                baseline_currency: str | None, display_currency: str | None, *,
                baseline_by_currency: dict[str, float] | None = None,
                baseline_unavailable: dict[str, str] | None = None) -> dict[str, Any]:
    from . import currency as fx

    scanned = len(rows)
    eligible = [r for r in rows if _is_commitment(r.get("problem") or "")]
    codes = {fx.currency_code(r.get("currency")) for r in eligible}
    target = fx.currency_code(display_currency)
    if not target and len(codes - {None}) > 1:
        groups = {
            code: _comparison([r for r in eligible if fx.currency_code(r.get("currency")) == code],
                              subs, baseline_annual, baseline_days, baseline_currency, code,
                              baseline_by_currency=baseline_by_currency,
                              baseline_unavailable=baseline_unavailable)
            for code in sorted(codes - {None})
        }
        return {
            "currency": None, "mixed_currency": True, "by_currency": groups,
            "options": [o for group in groups.values() for o in group["options"]],
            "models": [], "best": None, "baseline": {"annual": None, "monthly": None},
            "partial": None in codes, "unavailable": None in codes,
            "verdict": "Compare each native currency separately, or select a display currency.",
            "scanned": {"subscriptions": len(subs), "recommendations": scanned},
            "source": "Azure Advisor",
        }
    target = target or next(iter(codes - {None}), None) or fx.currency_code(baseline_currency)
    converted, unconverted = [], []
    for row in eligible:
        annual = fx.convert_amount(row.get("annualSavings"), row.get("currency"), target)
        monthly = fx.convert_amount(row.get("savings"), row.get("currency"), target)
        if annual is None:
            unconverted.append({"recommendation": row,
                                "reason": "Annual savings or a verified currency conversion is unavailable."})
            continue
        converted.append({**row, "annualSavings": annual,
                          "savings": monthly if monthly is not None else annual / 12,
                          "currency": target})
    rows = converted
    if baseline_annual is None and target and target in (baseline_by_currency or {}):
        # The native baseline in exactly the currency being compared; never a conversion
        # of another currency's figure, which would need a rate this path does not check.
        baseline_annual, baseline_currency = baseline_by_currency[target], target
    baseline_supplied = baseline_annual is not None
    baseline_annual = fx.convert_amount(baseline_annual, baseline_currency, target)
    unavailable = baseline_unavailable or {}
    baseline_reason = (
        "The on-demand baseline requires its source currency and an available exchange rate."
        if baseline_supplied and baseline_annual is None
        else None if baseline_supplied
        else unavailable.get(target or "") or unavailable.get("") or (
            "The on-demand baseline is not available for this currency."
            if baseline_by_currency else None))

    # Grouped by the decision somebody actually makes — buy this kind, for this term — and
    # summed across subscriptions, because one savings plan covers an estate rather than a
    # subscription. Lookbacks are kept apart until the spread has been measured.
    groups: dict[tuple[str, str, int], dict[str, Any]] = {}
    for r in rows:
        problem = r.get("problem") or ""
        if not _is_commitment(problem):
            continue
        kind = _kind_of(problem)
        if kind == "other":
            continue
        term = str(r.get("term") or "")
        days = _lookback_days(r.get("lookback"))
        key = (kind, term, days)
        g = groups.setdefault(key, {"annual": 0.0, "monthly": 0.0, "count": 0,
                                    "currency": target, "skus": set()})
        # Advisor's own field names. `rate_optimisation` renames these on the way out, but this
        # reads the raw row, and reading the renamed ones here silently scored every option at
        # zero — which the tests caught by way of the best option not being the best.
        g["annual"] += _num(r.get("annualSavings")) or 0.0
        g["monthly"] += _num(r.get("savings")) or 0.0
        g["count"] += 1
        if r.get("skuName") or r.get("displaySKU"):
            g["skus"].add(str(r.get("skuName") or r.get("displaySKU")))

    # One row per option, taking the longest lookback as the headline: it is the most evidence
    # Advisor has and the least swayed by a quiet week.
    options: list[dict[str, Any]] = []
    for (kind, term), _ in {(k[0], k[1]): None for k in groups}.items():
        windows = sorted((k[2] for k in groups if k[0] == kind and k[1] == term), reverse=True)
        if not windows:
            continue
        best = groups[(kind, term, windows[0])]
        annuals = [round(groups[(kind, term, w)]["annual"], 2) for w in windows]
        # A recommendation that moves sharply with the window is a recommendation to treat with
        # caution rather than a number to commit three years to. Measured on this estate at 2.7x
        # between the 30- and 60-day windows for the same one-year term.
        spread = None
        if len(annuals) > 1 and min(annuals) > 0:
            spread = round(max(annuals) / min(annuals), 1)
        annual_saving = round(best["annual"], 2)
        comparable = baseline_annual is not None and 0 <= annual_saving <= baseline_annual
        options.append({
            "kind": kind,
            "term": TERM_LABEL.get(term, term or "unspecified"),
            "term_code": term,
            "annual_saving": annual_saving,
            "monthly_saving": round(best["monthly"], 2),
            # What the same workload would *cost* under this model, not just what it saves.
            # A saving on its own cannot be compared with anything: "$558 a year" says nothing
            # about whether that is a rounding error or a third of the bill until it is put
            # beside the pay-as-you-go figure it is measured against.
            "annual_cost": (round(baseline_annual - annual_saving, 2)
                            if comparable else None),
            "monthly_cost": (round((baseline_annual - annual_saving) / 12, 2)
                             if comparable else None),
            "comparison_note": (
                "The estimate is not comparable with the observed on-demand baseline."
                if baseline_annual is not None and not comparable else None),
            "currency": best["currency"],
            "recommendations": best["count"],
            "skus": sorted(best["skus"])[:6],
            "lookback_days": windows[0],
            "lookback_days_available": windows,
            "lookback_spread": spread,
            "saving_pct": (round(annual_saving / baseline_annual * 100, 1)
                           if comparable and baseline_annual else None),
            # How each model behaves, stated once here so the UI does not have to carry
            # product knowledge in its markup. This is the difference somebody is actually
            # choosing between, and it is not visible in the money column.
            "flexibility": ("Any compute in the family, any region — the discount follows "
                            "the spend rather than the SKU."
                            if kind == "savings plan" else
                            "One SKU in one region. Exchangeable, and the deepest discount "
                            "available for a workload that will not move."),
            "commits_to": ("an hourly spend" if kind == "savings plan"
                           else "a specific SKU and region"),
        })

    options.sort(key=lambda o: -(o["annual_saving"] or 0))

    kinds_present = {_kind_of(r.get("problem") or "") for r in eligible}
    missing = [k for k in ("savings plan", "reservation") if k not in kinds_present]

    # The comparison the tab is named for: pay-as-you-go on one side, the best each commitment
    # model can do on the other. Best *per kind* rather than per term, because a reader choosing
    # between a savings plan and a reservation is choosing between two products first and two
    # term lengths second — and a table that mixes both axes reads as four unrelated rows.
    models: list[dict[str, Any]] = [{
        "kind": "pay-as-you-go",
        "label": "Pay-as-you-go",
        "term": "no commitment",
        "annual_cost": round(baseline_annual, 2) if baseline_annual is not None else None,
        "annual_saving": 0.0,
        "saving_pct": 0.0 if baseline_annual else None,
        "baseline": True,
        "flexibility": "Stop any time. The list price, and the price everything else is "
                       "measured against.",
        "commits_to": "nothing",
    }]
    for kind in ("savings plan", "reservation"):
        same = [o for o in options if o["kind"] == kind]
        if not same:
            models.append({
                "kind": kind,
                "label": "Savings plan" if kind == "savings plan" else "Reserved instance",
                "term": None,
                "annual_cost": None,
                "annual_saving": None,
                "saving_pct": None,
                "unavailable": True,
                "reason": (
                    "Advisor estimates require complete currency evidence."
                    if any(_kind_of(r["recommendation"].get("problem") or "") == kind for r in unconverted)
                    else "Azure is not recommending this for the current usage pattern."),
            })
            continue
        top = max(same, key=lambda o: o["annual_saving"] or 0)
        models.append({
            **top,
            "label": "Savings plan" if kind == "savings plan" else "Reserved instance",
            # Both terms stay attached, so the tab can show "1 year saves X, 3 year saves Y"
            # without a second request and without repeating the row.
            "terms": [{"term": o["term"], "term_code": o["term_code"],
                       "annual_saving": o["annual_saving"],
                       "annual_cost": o["annual_cost"],
                       "saving_pct": o["saving_pct"]}
                      for o in sorted(same, key=lambda o: o["term"] or "")],
        })

    best = options[0] if options else None
    incompatible_baseline = any(option["comparison_note"] for option in options)
    return {
        "baseline": {
            "option": "Pay-as-you-go",
            "annual": round(baseline_annual, 2) if baseline_annual is not None else None,
            "monthly": round(baseline_annual / 12, 2) if baseline_annual is not None else None,
            "saving": 0.0,
            "measured_over_days": baseline_days,
            "source": "your own billed on-demand spend",
            "currency": target,
            "unavailable_reason": baseline_reason,
        },
        "options": options,
        "models": models,
        "best": best,
        "verdict": ("Some Advisor estimates could not be converted; the comparison is incomplete."
                    if unconverted else
                    "Advisor savings estimates are not comparable with the observed on-demand "
                    "baseline; no negative annual cost or saving percentage is inferred."
                    if incompatible_baseline else _verdict(best, baseline_annual, missing, target or "")),
        "currency": target,
        "mixed_currency": False,
        "partial": bool(unconverted or incompatible_baseline),
        "unconverted": unconverted,
        "scanned": {"subscriptions": len(subs), "recommendations": scanned},
        "source": "Azure Advisor",
        # Named rather than shown as a zero row. "Azure is not recommending reservations here"
        # is a finding; a reservation line reading 0 reads as one worth nothing.
        "not_recommended": missing,
        "note": (
            "Each option is an alternative to pay-as-you-go and to the others, not a saving to "
            "add to them. Figures are Advisor's, scoped to the subscriptions you can see."
        ),
    }


def _verdict(best: dict[str, Any] | None, baseline_annual: float | None,
             missing: list[str], currency: str = "USD") -> str:
    """One sentence saying what the comparison actually concludes.

    A table of three numbers still leaves the reader to do the arithmetic and decide whether the
    difference is worth a three-year signature. This does that once, in the same place the
    numbers come from, so the answer cannot drift from them.
    """
    if not best:
        if len(missing) == 2:
            return ("Azure is not recommending a reservation or a savings plan here. That is "
                    "usually a workload too new, too small or too bursty for a steady-state "
                    "discount to pay for itself — pay-as-you-go is the right answer today.")
        return "No commitment recommendation is available for this scope."
    kind = "savings plan" if best["kind"] == "savings plan" else "reserved instance"
    cur = currency or "USD"
    share = (f", about {best['saving_pct']}% of the on-demand bill"
             if best.get("saving_pct") else "")
    flex = (" It also keeps the flexibility a reservation gives up: the discount follows the "
            "spend rather than one SKU in one region."
            if best["kind"] == "savings plan" else
            " It is the deeper discount, and it binds you to that SKU in that region.")
    ceiling = ""
    if baseline_annual and best.get("annual_cost") is not None:
        ceiling = (f" The same workload would run at {cur} {best['annual_cost']:,.0f} a year "
                   f"instead of {cur} {baseline_annual:,.0f}.")
    return (f"A {best['term']} {kind} is the best offer on this estate: it saves about "
            f"{cur} {best['annual_saving']:,.0f} a year{share}.{ceiling}{flex}")


async def rate_optimisation(subscription_ids: list[str] | None = None,
                            top: int = 100) -> dict[str, Any]:
    """Commitment-based savings Azure is currently recommending, plus the rest of Cost Advisor.

    Split rather than merged: a reservation recommendation asks for a purchase decision and a
    budget, while "delete this unattached disk" asks for ten seconds and a right-click. Showing
    them in one list makes both harder to act on.
    """
    subs = subscription_ids or [s["id"] for s in (await list_subscriptions())["subscriptions"]]
    if not subs:
        return {"empty": True, "reason": "No subscriptions in scope."}

    rows = await _arg(ADVISOR_COST_QUERY, subs, top=1000)

    commitments, usage = [], []
    for r in rows:
        item = {
            "problem": r.get("problem"),
            "solution": r.get("solution"),
            "impact": r.get("impact"),
            "type": str(r.get("impactedType") or "").split("/")[-1],
            "name": r.get("impactedName"),
            "resourceGroup": r.get("resourceGroup"),
            "subscriptionId": r.get("subscriptionId"),
            "savings": _num(r.get("savings")),
            "annual_savings": _num(r.get("annualSavings")),
            "currency": r.get("currency") or "USD",
            # A savings plan is quoted over its term, so P1Y and P3Y for the same estate are
            # alternatives rather than things to add together.
            "term": {"P1Y": "1 year", "P3Y": "3 year"}.get(r.get("term"), r.get("term") or ""),
            "lookback_days": _lookback_days(r.get("lookback")),
            "lookback": f"{r.get('lookback')} days" if r.get("lookback") else "",
            "sku": r.get("skuName"),
            "quantity": r.get("quantity"),
            "region": r.get("region"),
        }
        (commitments if _is_commitment(item["problem"]) else usage).append(item)

    # Advisor emits the same commitment at several lookback windows — 7, 30 and 60 days — and
    # listing all of them turns five real decisions into forty rows that look like forty
    # opportunities. Keep one per recommendation-and-term, preferring the longest window
    # available, which is the most evidence Advisor has and the least swayed by a quiet week.
    commitments = _best_per_option(commitments)

    commitments.sort(key=lambda x: -(x.get("savings") or 0))
    usage.sort(key=lambda x: -(x.get("savings") or 0))

    # Terms are alternatives, so the headline is the best single option rather than a sum —
    # adding a one-year and a three-year recommendation for the same workload double-counts it.
    best = max((c.get("savings") or 0 for c in commitments), default=0)
    best_term = next((c["term"] for c in commitments if (c.get("savings") or 0) == best), "")

    return {
        "commitments": commitments[:top],
        "usage": usage[:top],
        "best_commitment_saving": round(best, 2) if best else 0,
        "best_commitment_term": best_term,
        "usage_saving": round(sum(u.get("savings") or 0 for u in usage), 2),
        "currency": next(
            (r.get("currency") for r in commitments + usage if r.get("currency")), "USD"
        ),
        "scanned": {"subscriptions": len(subs), "recommendations": len(rows)},
        "source": "Azure Advisor",
        "note": (
            "From Azure Advisor, scoped to the subscriptions you can see — the same source the "
            "built-in Cost Optimization workbook uses. Terms are alternatives to choose "
            "between, not savings to add up."
        ),
        # Existing reservation utilisation needs Microsoft.Capacity/reservationOrders/read at
        # billing scope, which most people do not hold. Say so rather than showing an empty
        # panel that reads as "you have no reservations".
        "utilisation_available": False,
        "utilisation_note": (
            "How your existing reservations and savings plans are performing needs the "
            "Microsoft.Capacity/reservationOrders/read permission at billing scope, which this "
            "sign-in does not have. Opportunity is shown here; what you already hold is on the "
            "Commitments tab, read from billing data rather than from that API."
        ),
    }
