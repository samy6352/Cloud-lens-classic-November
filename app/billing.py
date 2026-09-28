"""Calendar-month statements from scoped warehouse data, never live Azure calls.

Invoice payments and consumption valuations are deliberately separate. Missing
prices remain unknown; a commitment payment is not a consumption saving.
"""
from __future__ import annotations

import calendar
import math
import re
from datetime import UTC, date, datetime, timedelta
from typing import Any

METRICS = (
    "recorded_cost", "billed_cost", "effective_cost", "list_cost", "contracted_cost",
    "azure_discounts", "reservation_savings", "savings_plan_savings",
    "other_commitment_savings", "non_commitment_savings", "actual_savings",
)
SAVINGS_KINDS = {
    "reservation_savings": "reservation", "savings_plan_savings": "savings_plan",
    "other_commitment_savings": "other", "non_commitment_savings": "none",
}
BREAKDOWN_LIMIT = 200
_SERVING_ENABLED = True


def period(month: str | None = None, today: date | None = None) -> dict[str, Any]:
    today = today or datetime.now(UTC).date()
    current = today.replace(day=1)
    if month:
        if not re.fullmatch(r"\d{4}-\d{2}", month):
            raise ValueError("month must be YYYY-MM.")
        try:
            start = date.fromisoformat(month + "-01")
        except ValueError as exc:
            raise ValueError("month must be a valid YYYY-MM.") from exc
        if start >= current:
            raise ValueError("Billing supports completed calendar months only.")
        if start.year < 2:
            raise ValueError("month must be year 0002 or later.")
    else:
        start = (current - timedelta(days=1)).replace(day=1)
    days = calendar.monthrange(start.year, start.month)[1]
    end = start + timedelta(days=days - 1)
    previous = (start - timedelta(days=1)).replace(day=1)
    return {
        "month": start.strftime("%Y-%m"), "label": start.strftime("%B %Y"),
        "start": start.isoformat(), "end": end.isoformat(), "days": days,
        "previous_month": previous.strftime("%Y-%m"),
        "previous_start": previous.isoformat(),
        "previous_end": (start - timedelta(days=1)).isoformat(),
        "previous_days": calendar.monthrange(previous.year, previous.month)[1],
        "timezone": "UTC",
    }


def normalize_currency(value: str | None) -> str:
    value = (value or "").strip().upper()
    if value in ("", "BILLED"):
        return ""
    if not re.fullmatch(r"[A-Z]{3}", value):
        raise ValueError("currency must be a three-letter currency code or BILLED.")
    return value


def statement_period(month: str | None = None, today: date | None = None) -> dict[str, Any]:
    """An explicit Billing month wins; otherwise inherit the global calendar selection."""
    from . import reporting

    selected = reporting.selected_month()
    if month or not selected:
        return period(month, today)
    start, end = reporting.month_bounds()
    # Reuse Billing's completed-month calculation, including the preceding calendar month.
    following = (start.replace(day=28) + timedelta(days=4)).replace(day=1)
    result = period(selected, following)
    result.update(end=end.isoformat(), days=(end - start).days + 1,
                  partial=end < start.replace(day=calendar.monthrange(start.year, start.month)[1]))
    return result


def _number(value: Any) -> float | None:
    try:
        result = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return result if math.isfinite(result) else None


def _token(value: Any) -> str:
    return re.sub(r"[^a-z0-9]", "", str(value or "").lower())


def _classification(row: dict) -> tuple[bool, bool, str]:
    category = _token(row.get("ChargeCategory"))
    status = _token(row.get("CommitmentDiscountStatus"))
    kind = _token(row.get("CommitmentDiscountType"))
    model = _token(row.get("PricingModel"))
    unused = category in ("unusedreservation", "unusedsavingsplan") or (
        status == "unused" and _token(row.get("BillingCostBasis")) in ("focus", "amortizedcost")
        and category not in ("purchase", "refund", "credit", "tax"))
    comparable = category == "usage" or unused
    if kind in ("reservation", "reservedinstance", "reservedinstances", "ri"):
        bucket = "reservation"
    elif kind in ("savingsplan", "azuresavingsplan", "savingsplanforcompute"):
        bucket = "savings_plan"
    elif kind:
        bucket = "other"
    elif model in ("reservation", "reservedinstance", "reservedinstances", "ri") or category == "unusedreservation":
        bucket = "reservation"
    elif model in ("savingsplan", "azuresavingsplan", "savingsplanforcompute") or category == "unusedsavingsplan":
        bucket = "savings_plan"
    elif row.get("BenefitId") or model not in ("", "ondemand", "spot"):
        bucket = "other"
    else:
        bucket = "none"
    return comparable, unused, bucket


def _empty() -> dict[str, float]:
    result = {"rows": 0, "comparable_rows": 0, "unused_rows": 0,
              "covered_rows": 0, "actual_list_basis": 0, "excluded_rows": 0}
    for metric in METRICS:
        for suffix in ("total", "covered", "rows", "reconciled"):
            result[f"{metric}_{suffix}"] = 0
    return result


def _row_stats(row: dict) -> dict[str, float]:
    """Offline counterpart of the SQL aggregation, also useful for deterministic fixtures."""
    comp, unused, kind = _classification(row)
    source = _token(row.get("BillingCostBasis"))
    known_basis = source in ("focus", "actualcost", "amortizedcost")
    listed = (0.0 if unused else _number(row.get("ListCost"))) if known_basis else None
    contracted = (0.0 if unused else _number(row.get("ContractedCost"))) if known_basis else None
    effective = _number(row.get("EffectiveCost")) if known_basis else None
    def difference(left, right):
        return None if left is None or right is None else left - right
    values = {
        "recorded_cost": _number(row.get("BilledCost")),
        "billed_cost": _number(row.get("InvoiceCost")) if source in ("focus", "actualcost") else None,
        "list_cost": listed, "contracted_cost": contracted, "effective_cost": effective,
        "azure_discounts": difference(listed, contracted),
        "actual_savings": difference(listed, effective),
        **{metric: difference(contracted, effective) for metric in SAVINGS_KINDS},
    }
    result = _empty()
    result.update(rows=1, comparable_rows=int(comp), unused_rows=int(unused),
                  excluded_rows=int(not comp))
    complete = comp and all(v is not None for v in (listed, contracted, effective))
    result["covered_rows"] = int(complete)
    for metric, value in values.items():
        relevant = metric in ("recorded_cost", "billed_cost") or (
            comp and (metric not in SAVINGS_KINDS or kind == SAVINGS_KINDS[metric]))
        result[f"{metric}_rows"] = int(relevant)
        result[f"{metric}_covered"] = int(relevant and value is not None)
        result[f"{metric}_total"] = value if relevant and value is not None else 0
        result[f"{metric}_reconciled"] = value if complete and relevant and value is not None else 0
    if comp and listed is not None and effective is not None:
        result["actual_list_basis"] = listed
    return result


def _merge(items: list[dict], rates: list[float] | None = None) -> dict:
    out = _empty()
    for key in out:
        is_money = key.endswith(("_total", "_reconciled")) or key == "actual_list_basis"
        out[key] = math.fsum(float(item.get(key) or 0) * (
            rates[index] if rates and is_money else 1) for index, item in enumerate(items))
    return out


def _metrics(stats: dict, available: bool = True) -> dict:
    out = {}
    complete_prices = stats["comparable_rows"] > 0 and stats["covered_rows"] == stats["comparable_rows"]
    for metric in METRICS:
        rows, covered = stats[f"{metric}_rows"], stats[f"{metric}_covered"]
        known = rows > 0 and rows == covered
        # No observed RI/SP usage is a real zero only on a fully priced consumption view.
        known = known or (metric in SAVINGS_KINDS and rows == 0 and complete_prices)
        out[metric] = round(stats[f"{metric}_total"], 2) if known and available else None
    denominator = stats["actual_list_basis"]
    out["effective_discount_pct"] = (
        round(stats["actual_savings_total"] / denominator * 100, 2)
        if out["actual_savings"] is not None and denominator > 0 else None)
    return out


def _pricing(stats: dict, available: bool) -> dict:
    covered, total = int(stats["covered_rows"]), int(stats["comparable_rows"])
    any_prices = any(stats[f"{metric}_covered"] for metric in ("list_cost", "contracted_cost", "effective_cost"))
    status = "complete" if total and covered == total else "partial" if any_prices else "unavailable"
    independently_known = {
        metric: round(stats[f"{metric}_total"], 2)
        if available and stats[f"{metric}_covered"] else None for metric in METRICS
    }
    if not available:
        status = "unavailable"
    return {
        "status": status, "covered_rows": covered, "total_rows": total,
        "excluded_rows": int(stats["excluded_rows"]), "unused_rows": int(stats["unused_rows"]),
        "known_totals": independently_known,
        "metric_coverage": {
            metric: {"covered_rows": int(stats[f"{metric}_covered"]),
                     "total_rows": int(stats[f"{metric}_rows"])} for metric in METRICS
        },
        "reconciled_totals": {
            metric: round(stats[f"{metric}_reconciled"], 2) if covered and available else None
            for metric in METRICS if metric not in ("recorded_cost", "billed_cost")
        },
        "note": (
            "Prices cover loaded comparable Usage and identifiable unused commitment allocations, "
            "not proof of complete ingestion. Recognized dataset provenance is required; legacy "
            "default zeroes are not evidence. Known totals are independently covered subsets; "
            "reconciled totals use the same fully priced rows. Purchases, credits, refunds and "
            "other non-usage charges remain in payments/recorded cost, not achieved savings."
        ),
    }


def _coverage(daily: list[dict], start: str, end: str, days: int) -> dict:
    present = sorted({str(row["day"])[:10] for row in daily if row.get("day")})
    bases = sorted({row.get("basis") or "unknown" for row in daily})
    rows = int(sum(row["rows"] for row in daily))
    return {
        "rows": rows, "first_day": present[0] if present else None,
        "last_day": present[-1] if present else None, "days_present": len(present),
        "calendar_days": days, "missing_days": days - len(present),
        "status": "missing" if not rows else "all_days_present" if len(present) == days else "partial",
        "start": start, "end": end, "cost_bases": bases,
        "invoice_reconciled": False,
        "note": "Days with rows measure observed coverage, not invoice or ingestion completeness.",
    }


def _basis(stats: dict, coverage: dict) -> dict:
    billed = _metrics(stats)["billed_cost"]
    bases = coverage["cost_bases"]
    code = "invoice" if billed is not None else (
        "amortized" if bases == ["AmortizedCost"] else
        "recorded" if bases and "unknown" not in bases else "unknown")
    return {
        "code": code, "metric": "billed_cost" if billed is not None else "recorded_cost",
        "label": "Total billed" if billed is not None else "Recorded amortized cost" if code == "amortized" else "Recorded cost",
        "source_bases": bases,
        "note": "Total billed requires explicit InvoiceCost and FOCUS/ActualCost provenance on every loaded row; recorded cost is not an invoice.",
    }


def _fx(native: set[str], requested: str) -> tuple[str | None, dict, dict]:
    from . import currency as fx

    target = requested or (next(iter(native)) if len(native) == 1 and "" not in native else None)
    pinned = fx._pinned()
    live = dict(fx._cache.get("rates") or {})
    rates = {**live, **pinned, "USD": 1.0}
    factors, details = {}, []
    for source in sorted(native):
        factor = None
        if source and target and source == target:
            factor = 1.0
        elif source and target:
            a, b = _number(rates.get(source)), _number(rates.get(target))
            if a and b and a > 0 and b > 0:
                factor = _number(b / a)
        factors[source] = factor
        details.append({
            "from": source or None, "to": target, "rate": factor,
            "source": "native" if source == target and source else (
                " / ".join("USD base" if code == "USD" else "operator pinned" if code in pinned
                           else fx._cache.get("source") or "cached reference rate"
                           for code in (source, target)) if factor is not None else "unavailable"),
            "as_of": fx._cache.get("date") if factor is not None and source != target
            and any(code and code != "USD" and code not in pinned for code in (source, target)) else None,
        })
    return target, factors, {
        "rates": details, "complete": all(v is not None for v in factors.values()),
        "note": "Current cached/pinned display FX, applied consistently to every monetary metric; "
                "not invoice-period FX. No network refresh is performed by this statement.",
    }


def assemble(daily: list[dict], groups: list[dict], window: dict,
             currency: str = "", filters: dict | None = None) -> dict:
    """Build a bounded statement from per-currency SQL aggregates."""
    current = [row for row in daily if str(row["day"])[:10] >= window["start"]]
    previous = [row for row in daily if str(row["day"])[:10] < window["start"]]
    native = {row.get("currency") or "" for row in daily}
    current_native = {row.get("currency") or "" for row in current}
    requested = normalize_currency(currency)
    if not requested and len(current_native) == 1:
        requested = next(iter(current_native))
    target, factors, fx = _fx(native, requested)

    def aggregate(rows: list[dict]) -> tuple[dict, bool]:
        available = all(factors.get(row.get("currency") or "") is not None for row in rows)
        rates = [factors[row.get("currency") or ""] for row in rows] if available else [0] * len(rows)
        return _merge(rows, rates), available

    stats, available = aggregate(current)
    prior, prior_available = aggregate(previous)
    cov = _coverage(current, window["start"], window["end"], window["days"])
    pcov = _coverage(previous, window["previous_start"], window["previous_end"], window["previous_days"])
    basis = _basis(stats, cov)
    kpis = _metrics(stats, available)
    prior_kpis = _metrics(prior, prior_available)
    prior_basis = _basis(prior, pcov)
    comparable = (
        not window.get("partial")
        and cov["status"] == pcov["status"] == "all_days_present"
        and basis["metric"] == prior_basis["metric"]
        and (basis["metric"] == "billed_cost" or (
            cov["cost_bases"] == pcov["cost_bases"] and len(cov["cost_bases"]) == 1
            and "unknown" not in cov["cost_bases"]))
        and available and prior_available
    )
    now_value, prior_value = kpis[basis["metric"]], prior_kpis[prior_basis["metric"]]
    change = now_value - prior_value if comparable and now_value is not None and prior_value is not None else None
    change_pct = round(change / prior_value * 100, 2) if change is not None and prior_value > 0 else None
    per_currency = []
    for code in sorted({row.get("currency") or "" for row in current}):
        selected = [row for row in current if (row.get("currency") or "") == code]
        cs = _merge(selected)
        per_currency.append({
            "currency": code or None, "kpis": _metrics(cs, bool(code)),
            "pricing": _pricing(cs, bool(code)),
            "spend_basis": _basis(cs, _coverage(selected, window["start"], window["end"], window["days"])),
        })
    breakdowns = {}
    rounding_adjusted = False
    for bucket in ("subscriptions", "services", "resources"):
        entries = []
        combined = {}
        # SQL result order is unspecified. Choose merged metadata and display ties
        # deterministically across hash joins, indexed probes and native currencies;
        # candidate membership/ranking remains the complete native SQL ranking.
        for row in sorted(
                (r for r in groups if r["bucket"] == bucket),
                key=lambda r: (str(r.get("currency") or ""), str(r.get("id")),
                               str(r.get("subscription_id")), str(r.get("name")),
                               str(r.get("resource_group")))):
            code = row.get("currency") or ""
            factor = factors.get(code)
            # A failed aggregate conversion does not relabel partial native entries as the target.
            display_currency = target if available else code or None
            adjusted = _merge([row], [factor if available and factor is not None else 1])
            identity = (row.get("id"), row.get("subscription_id"), display_currency, bool(row.get("is_other")))
            if identity in combined:
                existing, meta, display = combined[identity]
                if row.get("is_other"):
                    meta = {**meta, "group_count": meta["group_count"] + row["group_count"]}
                combined[identity] = (_merge([existing, adjusted]), meta, display)
            else:
                combined[identity] = (adjusted, row, display_currency)
        for adjusted, row, display_currency in combined.values():
            metrics = _metrics(adjusted, bool(display_currency))
            metric = basis["metric"]
            entries.append({
                "id": row.get("id"), "name": row.get("name") or "(Unallocated)",
                "subscription_id": row.get("subscription_id"),
                "resource_group": row.get("resource_group"), "currency": display_currency,
                "cost": metrics[metric], "spend_basis": metric, **metrics,
                "rows": int(adjusted["rows"]), "is_other": bool(row.get("is_other")),
                "group_count": int(row.get("group_count") or 1),
            })
        entries.sort(key=lambda r: (
            -abs(r["cost"] or 0), str(r["id"]), str(r["currency"] or ""),
            str(r["subscription_id"])))
        metric = basis["metric"]
        totals = ({target: kpis[metric]} if available else
                  {item["currency"]: item["kpis"][metric] for item in per_currency})
        for code, total in totals.items():
            if total is None:
                continue
            shown = [entry for entry in entries if entry["currency"] == code]
            if not shown or any(entry["cost"] is None for entry in shown):
                continue
            difference = round(total - math.fsum(entry["cost"] for entry in shown), 2)
            if difference:
                rounding_adjusted = True
                entries.append({
                    "id": f"rounding:{bucket}:{code}", "name": "Rounding adjustment",
                    "subscription_id": None, "resource_group": None, "currency": code,
                    **dict.fromkeys(METRICS),
                    "cost": difference, metric: difference, "spend_basis": metric,
                    "rows": 0, "is_other": False, "is_rounding": True, "group_count": 0,
                    "note": "Display rounding only; not an additional charge or saving.",
                })
        breakdowns[bucket] = entries
    warnings = []
    if window.get("partial"):
        warnings.append("The selected month is month-to-date; it is not compared with a complete prior month.")
    if cov["status"] != "all_days_present":
        warnings.append("The selected month has missing days or no loaded data; totals cover only loaded rows.")
    if not comparable:
        warnings.append("Month-over-month change is unavailable: month coverage, currency or cost basis is not comparable.")
    if kpis["billed_cost"] is None:
        warnings.append("Total billed is unavailable without explicit invoice cost and FOCUS/ActualCost provenance on every loaded row. Recorded cost is not an invoice.")
    if not available:
        warnings.append("Native currencies could not all be converted. Aggregate money is unavailable; see per-currency totals.")
    if stats["comparable_rows"] != stats["covered_rows"]:
        warnings.append("List/contract/effective prices are incomplete; covered subsets are disclosed separately.")
    if stats["excluded_rows"]:
        warnings.append("Non-usage charges are included in invoice/recorded totals but excluded from achieved savings.")
    if filters:
        warnings.append("Historical dimension filters apply to this statement only; future opportunities use subscription scope only.")
    if rounding_adjusted:
        warnings.append("Explicit rounding-adjustment rows reconcile displayed breakdown amounts to the statement total.")
    return {
        "period": window, "currency": target, "spend_basis": basis, "kpis": kpis,
        "pricing": _pricing(stats, available), "coverage": {"current": cov, "previous": pcov},
        "month_over_month": {
            "current": now_value, "previous": prior_value, "change": round(change, 2) if change is not None else None,
            "change_pct": change_pct, "comparable": comparable,
            "current_basis": basis["metric"], "previous_basis": prior_basis["metric"],
            "current_daily_average": round(now_value / window["days"], 2) if now_value is not None and cov["status"] == "all_days_present" else None,
            "previous_daily_average": round(prior_value / window["previous_days"], 2) if prior_value is not None and pcov["status"] == "all_days_present" else None,
            "note": "Only completed calendar months are compared. Full months have different day counts. All days present does not prove complete loading; zero/negative prior totals have no percentage change.",
        },
        "breakdowns": breakdowns, "per_currency": per_currency, "fx": fx,
        "trend": {"labels": [window["previous_month"], window["month"]],
                  "values": [prior_value, now_value],
                  "bases": [prior_basis["metric"], basis["metric"]]},
        "warnings": warnings, "filters": filters or {},
        "definitions": [
            {"label": "Total billed", "explanation": "Explicit InvoiceCost with FOCUS/ActualCost provenance on all loaded rows, including purchases, credits and refunds; not proof of reconciliation to an invoice."},
            {"label": "List price / effective cost", "explanation": "Valuation of comparable usage plus identifiable unused commitment allocations; payments and other non-usage charges are excluded."},
            {"label": "Azure discounts", "explanation": "Comparable ListCost minus ContractedCost."},
            {"label": "RI / Savings Plan achieved savings", "explanation": "ContractedCost minus EffectiveCost on explicitly typed commitments, net of identifiable unused allocations. Unclassified commitments stay Other."},
            {"label": "Actual achieved savings", "explanation": "Comparable ListCost minus EffectiveCost. Equals Azure discounts + RI + Savings Plan + other commitment + non-commitment adjustments on the same fully priced rows."},
            {"label": "Effective discount", "explanation": "Actual achieved savings divided by the same covered comparable list valuation; unavailable for a zero or negative denominator."},
            {"label": "Potential savings", "explanation": "Separate current scope-only opportunity scan; prospective estimates must not be added to this month's achieved savings."},
        ],
    }


def _sql_cte(window: dict | None, source: str = "costs", where: str = "", *,
             extra_columns: tuple[str, ...] = ()) -> str:
    """The normalise/classify/value chain every Billing aggregate is computed over.

    Shared by two callers with different jobs, which is why the source and the date
    predicate are parameters rather than literals:

    * `build`, reading the detail table for one statement period. Passes `window`.
    * `billing_rollup.select_sql`, precomputing the same values at ingest across
      whatever span the derived framework is rebuilding. Passes `where`.

    One definition rather than two, because the rollup's stored measures are only
    correct while they are computed exactly as the query they replace computes them.
    A second copy of this classification would be a second thing to keep in step,
    and the failure mode is a Billing statement that disagrees with the detail table
    by however much the two copies drifted.
    """
    def token(column: str) -> str:
        return f"regexp_replace(lower(coalesce(\"{column}\", '')), '[^a-z0-9]', '', 'g')"

    def finite(column: str) -> str:
        return f"""CASE WHEN "{column}" > '-Infinity'::float8 AND "{column}" < 'Infinity'::float8 THEN "{column}" END"""

    known = "basis_token IN ('focus','actualcost','amortizedcost')"
    if any(not isinstance(column, str) or not re.fullmatch(r"[A-Za-z_][A-Za-z_0-9]*", column)
           for column in extra_columns):
        raise ValueError("extra_columns must contain SQL column identifiers")
    extra = "".join(f'"{column}", ' for column in extra_columns)
    if window is not None:
        where = (f"WHERE \"ChargePeriodStart\" BETWEEN DATE '{window['previous_start']}'"
                 f" AND DATE '{window['end']}'")
    return f"""
    WITH normalized AS (
      SELECT {extra}"ChargePeriodStart", "BillingCostBasis", "BilledCost", "InvoiceCost",
        "ListCost", "ContractedCost", "EffectiveCost", "SubAccountName", "ResourceName",
        "ResourceGroup", "ServiceName", "BenefitId", {token('ChargeCategory')} AS category,
        -- Carried raw as well as tokenised: the tokens drive classification, but the
        -- filter bar compares against the stored column, so a grain that holds only
        -- the token cannot narrow itself by `charge`, `pricing` or `region`.
        "ChargeCategory", "PricingModel", "RegionName",
        {token('CommitmentDiscountStatus')} AS status,
        {token('CommitmentDiscountType')} AS ctype,
        {token('PricingModel')} AS model, {token('BillingCostBasis')} AS basis_token,
        upper(trim(coalesce("BillingCurrency", ''))) AS currency,
        lower(coalesce("SubAccountId", '')) AS sub_id,
        coalesce(nullif(lower("ResourceId"), ''), 'unallocated:' ||
          lower(coalesce("ResourceGroup", '')) || ':' || lower(coalesce("ResourceName", '')) ||
          ':' || lower(coalesce("ServiceName", ''))) AS resource_id
      FROM {source}
      {where}
    ), classified AS (
      SELECT *, (category IN ('unusedreservation','unusedsavingsplan') OR (
          status = 'unused' AND basis_token IN ('focus','amortizedcost')
          AND category NOT IN ('purchase','refund','credit','tax'))) AS unused,
        CASE WHEN ctype IN ('reservation','reservedinstance','reservedinstances','ri') THEN 'reservation'
          WHEN ctype IN ('savingsplan','azuresavingsplan','savingsplanforcompute') THEN 'savings_plan'
          WHEN ctype <> '' THEN 'other'
          WHEN model IN ('reservation','reservedinstance','reservedinstances','ri') OR category = 'unusedreservation' THEN 'reservation'
          WHEN model IN ('savingsplan','azuresavingsplan','savingsplanforcompute') OR category = 'unusedsavingsplan' THEN 'savings_plan'
          WHEN coalesce("BenefitId", '') <> '' OR model NOT IN ('','ondemand','spot') THEN 'other'
          ELSE 'none' END AS kind
      FROM normalized
    ), valued AS (
      SELECT *, category = 'usage' OR unused AS comparable,
        CASE WHEN {known} THEN CASE WHEN unused THEN 0 ELSE {finite('ListCost')} END END AS list_value,
        CASE WHEN {known} THEN CASE WHEN unused THEN 0 ELSE {finite('ContractedCost')} END END AS contract_value,
        CASE WHEN {known} THEN {finite('EffectiveCost')} END AS effective_value,
        CASE WHEN basis_token IN ('focus','actualcost') THEN {finite('InvoiceCost')} END AS invoice_value,
        {finite('BilledCost')} AS recorded_value
      FROM classified
    )
    """


def _sql_measures() -> str:
    full = "comparable AND list_value IS NOT NULL AND contract_value IS NOT NULL AND effective_value IS NOT NULL"
    counts = {
        "rows": "TRUE", "comparable_rows": "comparable", "unused_rows": "unused",
        "excluded_rows": "NOT comparable", "covered_rows": full,
    }
    parts = [f"count(*) FILTER (WHERE {condition}) AS {key}" for key, condition in counts.items()]
    values = {
        "recorded_cost": "recorded_value", "billed_cost": "invoice_value",
        "list_cost": "list_value", "contracted_cost": "contract_value",
        "effective_cost": "effective_value", "azure_discounts": "list_value - contract_value",
        "actual_savings": "list_value - effective_value",
        **dict.fromkeys(SAVINGS_KINDS, "contract_value - effective_value"),
    }
    for metric, value in values.items():
        relevant = "TRUE" if metric in ("recorded_cost", "billed_cost") else "comparable"
        if metric in SAVINGS_KINDS:
            relevant += f" AND kind = '{SAVINGS_KINDS[metric]}'"
        parts.extend((
            f"count(*) FILTER (WHERE {relevant}) AS {metric}_rows",
            f"count(({value})) FILTER (WHERE {relevant}) AS {metric}_covered",
            f"coalesce(sum(({value})) FILTER (WHERE {relevant}), 0) AS {metric}_total",
            f"coalesce(sum(({value})) FILTER (WHERE {full} AND {relevant}), 0) AS {metric}_reconciled",
        ))
    parts.append("coalesce(sum(list_value) FILTER (WHERE comparable AND effective_value IS NOT NULL), 0) AS actual_list_basis")
    return ",\n".join(parts)


def _sql_remeasures(prefix: str = "") -> str:
    """The same measures, re-aggregated from a table that already holds them.

    Every measure `_sql_measures` produces is a `count` or a `sum`, so every one is
    additive and a coarser cut is the sum of the finer rows beneath it. That is the
    whole property the rollup rests on: `reservation_savings_total` was filtered to
    `kind = 'reservation'` when it was computed, so the classification is already
    inside the stored number and does not need to be in the grain to be re-applied.

    Driven off `_empty()` rather than a list written out here, because `_empty()` is
    already the canonical set of measure names -- it is what `_merge` iterates and
    what `_metrics` indexes. Sharing it means a measure cannot be added to the
    aggregate without also being re-aggregated, which is the drift that would show
    up as a Billing total quietly short by one metric.
    """
    return ",\n".join(f'coalesce(sum({prefix}"{name}"), 0) AS "{name}"' for name in _empty())


def _statement_sql(window: dict, source: str, *, stored: bool = False,
                   indexed: bool = False) -> str:
    """Keep the expensive computed source single-pass within each aggregate.

    Only a confirmed stored source can cheaply rank resource keys before summing
    their full measure set. The relation name alone cannot establish that: scoped
    warehouse views can name either stored measures or the detail computation.

    On the 2.8M-row stored estate, wide resource grouping took 135.8s; rank-first
    with resource windows took 82.5s. Bounded per-currency sorts, materialized
    selected keys and independently parallel coarse totals then measured 31.6s
    versus 87.4s for the window plan. All fifty measures agreed within tolerance;
    assembled payloads were identical. These were uncached-payload reads with
    192MB work_mem, not flushed database/OS caches or a universal speedup claim.
    """
    measures = _sql_remeasures()
    stat_columns = ", ".join(_empty())
    period = (f'WHERE "ChargePeriodStart" BETWEEN DATE \'{window["previous_start"]}\''
              f' AND DATE \'{window["end"]}\'')
    current = (f'WHERE "ChargePeriodStart" BETWEEN DATE \'{window["start"]}\''
               f' AND DATE \'{window["end"]}\'')
    resource_ctes = ""
    resource_select = ""
    grouped = f"""
            SELECT currency,
              CASE WHEN grouping(resource_id)=0 THEN 'resources'
                   WHEN grouping("ServiceName")=0 THEN 'services' ELSE 'subscriptions' END AS bucket,
              CASE WHEN grouping(resource_id)=0 THEN resource_id
                   WHEN grouping("ServiceName")=0 THEN coalesce("ServiceName",'') ELSE "SubAccountId" END AS id,
              CASE WHEN grouping(resource_id)=0 THEN min("ResourceName")
                   WHEN grouping("ServiceName")=0 THEN "ServiceName" ELSE min("SubAccountName") END AS name,
              CASE WHEN grouping("SubAccountId")=0 THEN "SubAccountId" END AS subscription_id,
              CASE WHEN grouping(resource_id)=0 THEN min("ResourceGroup") END AS resource_group,
              {measures}
            FROM {source} {current}
            GROUP BY GROUPING SETS ((currency,"SubAccountId"),(currency,"ServiceName"),(currency,"SubAccountId",resource_id))
    """
    if stored:
        resources = f"""
            SELECT r.currency, r.resource_id AS id, min(r."ResourceName") AS name,
              r."SubAccountId" AS subscription_id, min(r."ResourceGroup") AS resource_group,
              k.total_groups, {_sql_remeasures('r.')}
            FROM {source} AS r
            JOIN resource_selected k USING (currency, "SubAccountId", resource_id)
            WHERE r."ChargePeriodStart" BETWEEN DATE '{window['start']}' AND DATE '{window['end']}'
            GROUP BY r.currency, r."SubAccountId", r.resource_id, k.total_groups
        """
        if indexed:
            # The complete native key set still determines rank and exact counts.
            # Only the fifty-measure retrieval becomes bounded indexed probes.
            # Retain raw equality alongside the canonical index expression, and
            # HAVING preserves the former inner join's NULL/unmatched-key behavior.
            resources = f"""
              SELECT k.currency, k.resource_id AS id, p.name,
                k."SubAccountId" AS subscription_id, p.resource_group, k.total_groups,
                {", ".join(f'p."{name}"' for name in _empty())}
              FROM resource_selected k CROSS JOIN LATERAL (
                SELECT min(r."ResourceName") AS name, min(r."ResourceGroup") AS resource_group,
                  {_sql_remeasures('r.')}
                FROM {source} r
                WHERE lower(r."SubAccountId")=lower(k."SubAccountId")
                  AND r.currency=k.currency AND r."SubAccountId"=k."SubAccountId"
                  AND r.resource_id=k.resource_id
                  AND r."ChargePeriodStart" BETWEEN DATE '{window["start"]}' AND DATE '{window["end"]}'
                HAVING count(*) > 0
              ) p
            """
        # Only the narrow cost/key set is spooled, not fifty measures for every
        # resource. LIMIT permits top-N heapsort with the original collation/ties.
        # Materializing the selected keys also avoids a wide source merge sort;
        # independent coarse CTEs permit partial parallel sums, unlike grouping sets.
        resource_ctes = f"""
          resource_costs AS MATERIALIZED (
            SELECT currency, "SubAccountId", resource_id,
              coalesce(sum(recorded_cost_total), 0) AS recorded_cost_total
            FROM {source} {current}
            GROUP BY currency, "SubAccountId", resource_id
          ), resource_counts AS (
            SELECT currency, count(*) AS total_groups FROM resource_costs GROUP BY currency
          ), resource_selected AS MATERIALIZED (
            SELECT c.currency, c.total_groups, k."SubAccountId", k.resource_id
            FROM resource_counts c CROSS JOIN LATERAL (
              SELECT "SubAccountId", resource_id FROM resource_costs g
              WHERE g.currency = c.currency
              ORDER BY abs(recorded_cost_total) DESC, resource_id, "SubAccountId"
              LIMIT {BREAKDOWN_LIMIT}
            ) k
          ), resources AS (
            {resources}
          ), subscription_totals AS MATERIALIZED (
            SELECT currency, 'subscriptions' AS bucket, "SubAccountId" AS id,
              min("SubAccountName") AS name, "SubAccountId" AS subscription_id,
              NULL::text AS resource_group, {measures}
            FROM {source} {current} GROUP BY currency, "SubAccountId"
          ), service_totals AS MATERIALIZED (
            SELECT currency, 'services' AS bucket, coalesce("ServiceName",'') AS id,
              "ServiceName" AS name, NULL::text AS subscription_id,
              NULL::text AS resource_group, {measures}
            FROM {source} {current} GROUP BY currency, "ServiceName"
          ),
        """
        grouped = "SELECT * FROM subscription_totals UNION ALL SELECT * FROM service_totals"
        resource_select = f"""
          UNION ALL
          SELECT 'resources', NULL::date, currency, NULL::text, id, name, subscription_id,
            resource_group, total_groups, {stat_columns}
          FROM resources
        """
    return f"""
          WITH daily AS (
            SELECT "ChargePeriodStart" AS day, currency, "BillingCostBasis" AS basis,
              {measures} FROM {source} {period} GROUP BY 1, 2, 3
          ),
          {resource_ctes}
          grouped AS (
            {grouped}
          ), ranked AS (
            SELECT *, row_number() OVER (
              PARTITION BY currency,bucket ORDER BY abs(recorded_cost_total) DESC,id,subscription_id) AS rank,
              count(*) OVER (PARTITION BY currency,bucket) AS total_groups
            FROM grouped
          )
          SELECT 'daily' AS bucket, day, currency, basis,
            NULL::text AS id, NULL::text AS name, NULL::text AS subscription_id,
            NULL::text AS resource_group, 0 AS total_groups, {stat_columns}
          FROM daily
          UNION ALL
          SELECT bucket, NULL::date, currency, NULL::text, id, name, subscription_id,
            resource_group, total_groups, {stat_columns}
          FROM ranked WHERE rank <= {BREAKDOWN_LIMIT}
          {resource_select}
        """


def build(scopes: list[str] | None, currency: str = "", month: str | None = None,
          filters: dict | None = None, *, today: date | None = None, warehouse: Any = None) -> dict:
    """A statement for one calendar month and the month before it for comparison."""
    from . import billing_serving, reporting

    window = statement_period(month, today)
    currency = normalize_currency(currency)
    if warehouse is None:
        from .warehouse import warehouse
    with reporting.select(None), warehouse.reader(scopes, None, filters) as reader:
        # One SQL statement gives totals and breakdowns the same MVCC snapshot during ingest.
        serving = getattr(reader, "bill_serving", None)
        result = None
        if (_SERVING_ENABLED and serving and billing_serving.serves_window(window)
                and billing_serving.serves_filters(filters)):
            served = reader.rows(billing_serving.statement_sql(window, serving, BREAKDOWN_LIMIT), 20001)
            if len(served) >= 20001:
                raise ValueError("Too many Billing currency groups; narrow the subscription scope.")
            status = [row for row in served if row["bucket"] == "__serving__"]
            if len(status) == 1 and status[0]["total_groups"] == 1:
                result = [row for row in served if row["bucket"] != "__serving__"]
        if result is None:
            result = reader.rows(_statement_sql(
                window, reader.bill, stored=getattr(reader, "bill_stored", False),
                indexed=getattr(reader, "bill_indexed", False)), 20000)
        if len(result) >= 20000:
            raise ValueError("Too many Billing currency groups; narrow the subscription scope.")
    daily = [row for row in result if row["bucket"] == "daily"]
    groups = [row for row in result if row["bucket"] != "daily"]
    # Subtract the selected groups from complete native totals to retain every excluded dollar.
    for code in {row.get("currency") or "" for row in daily if str(row["day"])[:10] >= window["start"]}:
        total = _merge([row for row in daily if (row.get("currency") or "") == code and str(row["day"])[:10] >= window["start"]])
        for bucket in ("subscriptions", "services", "resources"):
            selected = [row for row in groups if row["bucket"] == bucket and row["currency"] == code]
            count = int(selected[0]["total_groups"]) if selected else 0
            if count > len(selected):
                known = _merge(selected)
                remainder = {key: total[key] - known[key] for key in total}
                groups.append({
                    **remainder, "bucket": bucket, "currency": code, "id": "__other__",
                    "name": "Other", "is_other": True, "group_count": count - len(selected),
                })
    return assemble(daily, groups, window, currency, filters)
