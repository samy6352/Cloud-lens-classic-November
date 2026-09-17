"""Finding the days that did not look like the others.

Every other tab answers a question someone thought to ask. This one has to speak first: a cost
spike is only expensive because nobody noticed it for three weeks, and nobody notices by reading
a chart every morning. The dashboard shows *what* spend is; this shows *what changed*.

The method is deliberately boring. A rolling median and a median absolute deviation over the
preceding fortnight, and a day is anomalous when it sits far enough outside that band. Not a
mean and a standard deviation, because both are dragged around by the very outliers being
looked for -- one $900 spike inflates the mean and the deviation together, and the next spike
of the same size then reads as normal. The median does not move, so the second spike is still
a spike.

Three deliberate refusals:

  * **No anomaly without a baseline.** Fewer than `MIN_HISTORY` days of prior data and the day
    is skipped rather than guessed at. A service that appeared on Tuesday is not a Wednesday
    anomaly.

  * **Absolute floor as well as relative.** A meter going from $0.02 to $0.20 is a 900% rise and
    means nothing. Everything must also clear `MIN_DELTA`, or the page fills with noise and
    stops being read -- which is the only real failure mode for an alerting surface.

  * **The most recent days are excluded by default.** Azure restates cost data for several days
    after the fact, so the newest figures are still moving. Reporting them produces "anomalies"
    that quietly resolve themselves, and an alert that cries wolf twice gets ignored forever.
"""
from __future__ import annotations

import json
import logging
import math
from datetime import date, timedelta
from typing import Any

log = logging.getLogger("cloudlens.anomalies")

# Days of history a dimension needs before any of its days can be judged.
MIN_HISTORY = 7

# How many days of the *same kind* (weekday vs weekend) are needed before the baseline is drawn
# from them alone. Four, because two weeks of history only contains four weekend days — demand
# seven and the weekend comparison never engages, which is the bug this number exists to fix.
MIN_KIND = 4

# The trailing window the baseline is computed over. Two weeks covers a weekly cycle -- weekend
# dips are normal and must not read as anomalies -- without reaching so far back that a genuine
# step change stays "anomalous" for a month.
WINDOW = 14

# How many MADs from the median a day must sit to count. 3.5 is the conventional threshold for
# the modified z-score; lower floods the page, higher misses real events.
THRESHOLD = 3.5

# Money floors. Relative change alone is meaningless at the bottom of the scale.
#
# $15 rather than something smaller because of what the first real run looked like: at $5, three
# quarters of the findings were sub-$15 rounding wobble on steady services, and a page where the
# real $97 spike sits ninth is a page nobody reads twice. An alerting surface has exactly one
# fatal failure mode, and it is being ignored.
MIN_DELTA = 15.0         # a day must move at least this much, in billing currency
MIN_BASELINE = 1.0       # and the thing must have been costing at least this much

# Cost data is restated for days after the fact, so the tail is unstable.
SETTLE_DAYS = 2

# How close two findings' deltas must be to count as the same underlying event seen through
# different dimensions. 0.7 accepts the $97 / $96 / $82 spread that one spike produced when
# viewed as a resource group, a subscription and a service.
SAME_EVENT = 0.7

# Beyond this the score stops meaning anything — it is just "far outside anything seen before".
SCORE_CAP = 50.0

# What a spike can be attributed to. Ordered by how actionable the answer is: a service name
# tells you what broke, a subscription only tells you where to start looking.
DIMENSIONS = {
    "ServiceName": "service",
    "ResourceGroup": "resource group",
    "SubAccountName": "subscription",
    "ResourceName": "resource",
    "RegionName": "region",
    "MeterName": "meter",
}


class AnomalyError(RuntimeError):
    """Something the caller can act on."""


def _is_weekend(day: str) -> bool:
    """Saturday or Sunday. Used to keep weekday and weekend baselines apart."""
    try:
        return date.fromisoformat(day).weekday() >= 5
    except ValueError:
        return False


def _median(values: list[float]) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    mid = len(ordered) // 2
    if len(ordered) % 2:
        return ordered[mid]
    return (ordered[mid - 1] + ordered[mid]) / 2


def _mad(values: list[float], centre: float) -> float:
    """Median absolute deviation — a spread that outliers cannot inflate."""
    if not values:
        return 0.0
    return _median([abs(v - centre) for v in values])


def score(value: float, history: list[float],
          min_samples: int = MIN_HISTORY) -> tuple[float, float, float]:
    """How unusual `value` is against `history`. Returns (score, baseline, spread).

    The modified z-score: 0.6745 is the constant that puts MAD on the same scale as a standard
    deviation for normally distributed data, so the familiar "3 sigma" intuition still applies.

    A MAD of zero is the case worth handling carefully — it means every day in the window was
    identical, which is extremely common for a fixed monthly charge. Any change at all is then
    infinitely surprising by the formula, so the fallback compares against the median directly
    and lets the money floors decide whether it matters.

    `min_samples` is a parameter rather than a constant because the weekend baseline is drawn
    from four days where the weekday one has fourteen. Hard-coding the larger figure made the
    weekend comparison silently never engage.
    """
    if len(history) < min_samples:
        return 0.0, 0.0, 0.0
    centre = _median(history)
    spread = _mad(history, centre)
    if spread == 0:
        if centre == 0:
            return (0.0 if value == 0 else 10.0), centre, 0.0
        change = abs(value - centre) / centre
        return (10.0 if change > 0.5 else 0.0), centre, 0.0
    # Capped. A near-zero MAD — a service that billed the same fraction of a cent every day —
    # divides into a real change and produces "1,308,815× the usual variation", which is
    # arithmetically true and reads as a bug. Past about 50 the number has stopped carrying
    # information anyway: it is simply "far outside anything seen before".
    return max(-SCORE_CAP, min(SCORE_CAP, 0.6745 * (value - centre) / spread)), centre, spread


def _series(rows: list[tuple], dimension: str, *,
            covered_days: set[str] | None = None) -> dict[str, dict[str, float]]:
    """Missing charges are zero only on days certified complete for the entire scoped cohort."""
    by_key: dict[str, dict[str, float]] = {}
    for key, day, cost in rows:
        k = key or "(none)"
        d = str(day)[:10]
        if covered_days is not None and d not in covered_days:
            continue
        by_key.setdefault(k, {})[d] = float(cost or 0.0)
    if covered_days is not None:
        for series in by_key.values():
            for day in covered_days:
                series.setdefault(day, 0.0)
    return by_key


def _spanning_days(days: set[str]) -> list[str]:
    """Every calendar day from the first seen to the last, inclusive."""
    if not days:
        return []
    known = sorted(days)
    first, last = date.fromisoformat(known[0]), date.fromisoformat(known[-1])
    out, cursor = [], first
    while cursor <= last:
        out.append(cursor.isoformat())
        cursor += timedelta(days=1)
    return out


def detect(rows: list[tuple], dimension: str, *,
           settle_days: int = SETTLE_DAYS,
           min_delta: float = MIN_DELTA,
           min_baseline: float = MIN_BASELINE,
           money_scale: float = 1.0,
           covered_days: set[str] | None = None,
           as_of: str | None = None,
           assessed_days: set[str] | None = None) -> list[dict[str, Any]]:
    """Every anomalous day in the series, worst first.

    Pure arithmetic over rows already fetched -- no database, no clock -- so the whole detector
    is testable against a handmade series, which is the only way to be confident about a
    threshold.
    """
    if not math.isfinite(money_scale) or money_scale <= 0:
        raise ValueError("Anomaly money scale must be positive and finite.")
    series = _series(rows, dimension, covered_days=covered_days)
    if not series:
        return []

    all_days = sorted({d for s in series.values() for d in s})
    if len(all_days) <= MIN_HISTORY:
        return []

    # Drop the unsettled tail. Reporting a spike that Azure is still revising produces alerts
    # that resolve themselves, and those are worse than no alerts.
    if as_of is not None:
        settled = (date.fromisoformat(as_of) - timedelta(days=settle_days)).isoformat()
        judged = [day for day in all_days if day <= settled]
    else:
        judged = all_days[:-settle_days] if settle_days else all_days

    found: list[dict[str, Any]] = []
    for key, by_day in series.items():
        for i, day in enumerate(judged):
            if i < MIN_HISTORY or day not in by_day:
                continue
            value = by_day[day]

            # Compare like with like. Weekday spend and weekend spend are different populations
            # for most estates — dev boxes idle, pipelines stop — so judging Saturday against a
            # window containing five weekdays reports every single weekend as an anomaly. The
            # first run of this did exactly that: six findings from one five-week series whose
            # only feature was a weekend.
            #
            # The same-kind window reaches back further than WINDOW days, because a fortnight
            # holds only four weekend days. `min_samples` is lowered to match: demanding the
            # usual seven would mean the weekend baseline never has enough to engage, which is
            # precisely the bug this exists to fix.
            kind = _is_weekend(day)
            earliest = (date.fromisoformat(day) - timedelta(days=WINDOW * 4)).isoformat()
            prior = [d for d in judged[:i] if d in by_day and d >= earliest]
            if len(prior) < MIN_HISTORY:
                continue
            same_kind = [d for d in prior if _is_weekend(d) == kind][-WINDOW:]
            if len(same_kind) >= MIN_KIND:
                history, floor = [by_day[d] for d in same_kind], MIN_KIND
            else:
                history, floor = [by_day[d] for d in prior[-WINDOW:]], MIN_HISTORY

            if assessed_days is not None:
                assessed_days.add(day)
            z, baseline, _spread = score(value, history, min_samples=floor)
            if abs(z) < THRESHOLD:
                continue

            delta = value - baseline
            # Both floors, not either: a big relative move on small money is noise, and so is a
            # big absolute move on something that was never predictable.
            if abs(delta) < min_delta or max(baseline, value) < min_baseline:
                continue

            found.append({
                "day": day,
                "dimension": DIMENSIONS.get(dimension, dimension),
                "key": key,
                "cost": round(value, 2),
                "baseline": round(baseline, 2),
                "delta": round(delta, 2),
                "percent": round((delta / baseline) * 100, 1) if baseline else None,
                "score": round(abs(z), 1),
                "direction": "up" if delta > 0 else "down",
                "severity": _severity(abs(delta) / money_scale, abs(z)),
            })

    found.sort(key=lambda a: (a["day"], abs(a["delta"])), reverse=True)
    return found


def _severity(delta: float, z: float) -> str:
    """How loudly to say it.

    Driven mostly by money rather than by the statistic. A 20-sigma deviation on $6 is still
    $6, and someone woken up for it stops trusting the page.
    """
    if delta >= 500 or (delta >= 100 and z >= 10):
        return "high"
    if delta >= 50:
        return "medium"
    return "low"


def _rows(con: Any, dimension: str, days: int, *,
          start: str | None = None, end: str | None = None,
          currency: str | None = None,
          aggregate: str = "costs", named: str = "costs") -> list[tuple]:
    """Daily cost per key for one dimension. The dimension name is from a fixed map, never input.

    Which relation answers this depends on the dimension, because the two derived
    grains carry different ones. `SubAccountName` is in the rollup and not in the
    presence table; `ResourceName` is the reverse; `MeterName` is in neither and
    has to come from the detail table. Decided by asking the grain rather than by
    listing dimensions here, so that widening a grain cannot leave this behind.
    """
    if dimension not in DIMENSIONS:
        raise AnomalyError(f"Cannot analyse by {dimension!r}.")
    from . import presence, reporting, rollup

    if dimension in rollup.SERVABLE:
        source = aggregate
    elif dimension in presence.GROUPABLE:
        # `named` rather than the presence table by name: under a converted currency
        # that money cannot be converted, and `named` is already `costs` in that case.
        source = named
    else:
        source = "costs"

    if end is None:
        edge = con.execute(f'SELECT max("ChargePeriodStart") FROM {source}').fetchone()
        if not edge or not edge[0]:
            return []
        end = str(edge[0])[:10]
    end = date.fromisoformat(end).isoformat()
    since = date.fromisoformat(start).isoformat() if start else reporting.since_date(
        (date.fromisoformat(end) - timedelta(days=max(1, days) - 1)).isoformat())
    currency_filter = ""
    if currency:
        code = currency.replace("'", "''")
        currency_filter = f" AND upper(trim(\"BillingCurrency\")) = '{code}'"
    return con.execute(
        f'SELECT coalesce(nullif("{dimension}", \'\'), \'(none)\') AS k, '
        '"ChargePeriodStart" AS d, sum("BilledCost") AS c '
        f"FROM {source} WHERE \"ChargePeriodStart\" >= DATE '{since}' "
        f"AND \"ChargePeriodStart\" <= DATE '{end}' {currency_filter} "
        'GROUP BY 1, 2 ORDER BY 2'
    ).fetchall()


def _coverage(con: Any, scope: list[str] | None, start: date,
              end: date, source: str = "costs") -> tuple[set[str], dict[str, Any]]:
    subjects = scope if scope is not None else [
        row[0] for row in con.execute(f'SELECT DISTINCT "SubAccountId" FROM {source}').fetchall()]
    def canonical(sub):
        value = str(sub).strip().lower()
        return value.split("/subscriptions/", 1)[1].split("/", 1)[0] if "/subscriptions/" in value else value

    subs = sorted({canonical(sub) for sub in subjects if sub})
    known: dict[str, set[str]] = {sub: set() for sub in subs}
    reason = None
    if subs:
        conditions = " OR ".join("(key >= ? AND key <= ?)" for _ in subs)
        parameters = [bound for sub in subs for bound in (
            f"focus-day:{sub}:{start.isoformat()}", f"focus-day:{sub}:{end.isoformat()}")]
        try:
            rows = con.execute(f"SELECT key,value FROM meta WHERE {conditions}", parameters).fetchall()
            for key, value in rows:
                sub, day = key[len("focus-day:"):].rsplit(":", 1)
                try:
                    receipt = json.loads(value) if isinstance(value, str) else value
                    count = receipt.get("day_rows")
                    valid = (bool(receipt.get("fingerprint")) and type(count) is int and count >= 0
                             and start <= date.fromisoformat(day) <= end)
                except (AttributeError, TypeError, ValueError):
                    valid = False
                if valid and sub in known:
                    known[sub].add(day)
        except Exception:
            log.warning("Authoritative anomaly day coverage is unavailable.")
            reason = "Authoritative FOCUS day coverage is unavailable."
    else:
        reason = "No subscription cohort is available to verify daily completeness."
    common = set.intersection(*known.values()) if known else set()
    expected = max(0, (end - start).days + 1)
    return common, {
        "basis": "validated_focus_day_receipts",
        "subscriptions": len(subs),
        "required_subscription_days": expected * len(subs),
        "verified_subscription_days": sum(len(days) for days in known.values()),
        "missing_by_subscription": {sub: expected - len(days) for sub, days in known.items()},
        "reason": reason,
    }


def analyse(scope: list[str] | None = None, days: int = 60,
            dimensions: tuple[str, ...] = ("ServiceName", "ResourceGroup", "SubAccountName"),
            top: int = 40, display_currency: str | None = None, *,
            filters: dict[str, list[str]] | None = None) -> dict[str, Any]:
    """Everything unusual in the loaded data, across several dimensions at once.

    Several dimensions because the useful answer depends on the question. "Storage jumped" is
    the headline; "it was rg-backups" is where to look; "it was subscription X" is who to tell.
    Running all three costs one extra warehouse pass each and turns a number into an
    explanation.

    `display_currency` converts inside the warehouse connection, so the baseline, the deviation
    and the threshold are all derived from the same converted figures. The money floors are
    scaled by the same rate — otherwise a ₹15 floor would replace a $15 one and the tab would
    report a different set of anomalies depending on which currency you happened to be viewing,
    which is indefensible: the events did not change, only the units.
    """
    from . import currency as fx
    from . import reporting, rollup
    from .warehouse import warehouse

    days = reporting.period_days(days)

    bounds = reporting.month_bounds()
    # Load baseline history without expanding the reported period or the authorization scope.
    with reporting.select(None):
        con = warehouse.connect(read_only=True, scope=scope, currency=display_currency, filters=filters)
    try:
        # The rollup where it can answer this connection. The span is a min, a max
        # and a row count -- all of which the rollup holds, the count as the sum of
        # the `charges` each group stands for. Measured at 44s of this endpoint's
        # 45.7s before routing: the whole tab was waiting on three numbers.
        agg, _, named = warehouse.relations(con, scope, display_currency, filters)
        span = con.execute(
            f'SELECT min("ChargePeriodStart"), max("ChargePeriodStart"), '
            f'{rollup.charges_sql(agg)} FROM {agg}'
        ).fetchone()

        if not span or not span[2]:
            return {"anomalies": [], "count": 0, "currency": fx.currency_code(display_currency),
                    "partial": True, "unavailable": True,
                    "coverage": {"basis": "validated_focus_day_receipts", "verified_days": 0},
                    "note": "No cost data is loaded yet, so there is nothing to compare against."}

        end = bounds[1] if bounds else date.fromisoformat(str(span[1])[:10])
        start = bounds[0] if bounds else end - timedelta(days=days - 1)
        history_start = start - timedelta(days=WINDOW * 4)
        as_of = min(end, date.fromisoformat(str(span[1])[:10]))
        covered, coverage = _coverage(con, scope, history_start, as_of, agg)
        history_currencies = con.execute(
            f'SELECT DISTINCT "ChargePeriodStart", "BillingCurrency" FROM {agg} '
            f'WHERE "ChargePeriodStart" >= DATE \'{history_start.isoformat()}\' '
            f'AND "ChargePeriodStart" <= DATE \'{as_of.isoformat()}\''
        ).fetchall()
        unknown_currency_days = {str(day)[:10] for day, code in history_currencies if not fx.currency_code(code)}
        covered.difference_update(unknown_currency_days)
        expected_days = set(_spanning_days({start.isoformat(), as_of.isoformat()})) if start <= as_of else set()
        verified_days = covered & expected_days
        coverage.update(
            verified_days=len(verified_days), unverified_days=sorted(expected_days - covered),
            history_verified_days=len(covered - expected_days),
            currency_unverified_days=sorted(unknown_currency_days),
        )
        period = (f'"ChargePeriodStart" >= DATE \'{start.isoformat()}\' '
                  f'AND "ChargePeriodStart" <= DATE \'{end.isoformat()}\'')
        codes = {
            str(row[0] or "").strip().upper()
            for row in con.execute(
                f'SELECT DISTINCT "BillingCurrency" FROM {agg} WHERE {period}').fetchall()
        }
        currency = next(iter(codes)) if len(codes) == 1 and fx.currency_code(next(iter(codes))) else None
        results: list[dict[str, Any]] = []
        unavailable: dict[str, str] = {}
        assessed_by_currency: dict[str, set[str]] = {}
        for code in sorted(codes):
            scale = fx.rate_to(code) if fx.currency_code(code) else None
            if scale is None or not math.isfinite(scale) or scale <= 0:
                unavailable[code or "unknown"] = "A verified USD exchange rate is required for monetary thresholds."
                continue
            assessed: set[str] = set()
            for dim in dimensions:
                found = detect(_rows(con, dim, days, start=history_start.isoformat(),
                                    end=end.isoformat(), currency=code,
                                    aggregate=agg, named=named), dim,
                              min_delta=MIN_DELTA * scale, min_baseline=MIN_BASELINE * scale,
                              money_scale=scale, covered_days=covered,
                              as_of=as_of.isoformat(), assessed_days=assessed)
                results.extend({**item, "currency": code} for item in found
                              if start.isoformat() <= item["day"] <= end.isoformat())
            assessed_by_currency[code] = assessed & expected_days
            if not assessed_by_currency[code]:
                unavailable[code] = "Insufficient authoritative subscription/day coverage for the selected days and baselines."

        # The window that was actually judged, which is not the same as the data on hand.
        # `span` is everything the warehouse holds; this is what `days` narrowed it to, and it
        # is what the reader is looking at. Reporting only the former put an unchanging date
        # range next to a finding count that changed with the period selector — two readings of
        # "period" on one screen that disagreed, and the wrong one was the more prominent.
        window = con.execute(
            'SELECT min("ChargePeriodStart"), max("ChargePeriodStart") FROM costs '
            f"WHERE {period}"
        ).fetchone()

        # Daily totals, so the tab can draw the estate's own line with the flagged days on it.
        daily_rows = con.execute(
            'SELECT "ChargePeriodStart", upper(trim("BillingCurrency")), sum("BilledCost") FROM costs '
            f'WHERE {period} GROUP BY 1, 2 ORDER BY 1'
        ).fetchall()
    finally:
        con.close()

    # The same event shows up under several dimensions -- a service spike is also a resource
    # group spike and a subscription spike, with nearly the same delta. Reporting all three says
    # one thing three times and pushes the next real event off the page.
    #
    # Grouped by day and by *proportional* similarity rather than a rounded delta: the three
    # views of one event rarely agree to the cent ($97.18 / $96.38 / $82.10 above), so an exact
    # match never collapsed them. The most specific dimension wins, and the others are kept as
    # corroboration so the detail is still there when someone opens the row.
    rank = {name: i for i, name in enumerate(DIMENSIONS.values())}
    merged: list[dict[str, Any]] = []
    for a in sorted(results, key=lambda x: (x["day"], -abs(x["delta"]))):
        twin = next(
            (m for m in merged
             if m["day"] == a["day"]
             and m["currency"] == a["currency"]
             and m["direction"] == a["direction"]
             and abs(a["delta"]) >= abs(m["delta"]) * SAME_EVENT
             and abs(a["delta"]) <= abs(m["delta"]) / SAME_EVENT),
            None,
        )
        if twin is None:
            merged.append({**a, "also": []})
            continue
        twin.setdefault("also", []).append(
            {"dimension": a["dimension"], "key": a["key"], "delta": a["delta"]})
        # Keep whichever view names the thing most precisely.
        if rank.get(a["dimension"], 9) < rank.get(twin["dimension"], 9):
            twin["dimension"], twin["key"] = a["dimension"], a["key"]

    merged.sort(key=lambda a: (a["day"], abs(a["delta"])), reverse=True)

    high = [a for a in merged if a["severity"] == "high"]
    up = [a for a in merged if a["direction"] == "up"]
    daily_by_currency: dict[str, list[tuple]] = {}
    for day, code, amount in daily_rows:
        if str(day)[:10] in verified_days:
            daily_by_currency.setdefault(str(code or "unknown"), []).append((day, amount))
    for code in codes:
        amounts = {str(day)[:10]: amount for day, amount in daily_by_currency.get(code, [])}
        daily_by_currency[code] = [(day, amounts.get(day, 0.0)) for day in sorted(verified_days)]
    settled_edge = (as_of - timedelta(days=SETTLE_DAYS)).isoformat()
    settled_to = max((day for day in verified_days if day <= settled_edge), default=None)
    coverage["assessed_days_by_currency"] = {code: sorted(days) for code, days in assessed_by_currency.items()}
    coverage_partial = (bool(coverage["unverified_days"] or unknown_currency_days)
                        or any(coverage["missing_by_subscription"].values()))
    if coverage_partial and not coverage["reason"]:
        coverage["reason"] = "Unverified subscription/day gaps are excluded, not treated as zero or used as baselines."
    impact_by_currency = {
        code: round(sum(a["delta"] for a in up if a["currency"] == code), 2)
        for code in codes if code not in unavailable and code
    }
    return {
        "anomalies": merged[:top],
        "count": len(merged),
        "high": len(high),
        "increases": len(up),
        "decreases": len(merged) - len(up),
        "impact": impact_by_currency.get(currency),
        "impact_by_currency": impact_by_currency,
        "currency": currency,
        "mixed_currency": len(codes) > 1,
        "partial": bool(unavailable) or not bool(codes) or coverage_partial,
        "unavailable": not bool(codes) or len(unavailable) == len(codes),
        "unavailable_currencies": unavailable,
        "coverage": coverage,
        "coverage_reason": coverage["reason"] or (
            "Insufficient authoritative history to assess the selected period." if unavailable else None),
        "from": str(span[0]) if span[0] else None,
        "to": str(span[1]) if span[1] else None,
        # The judged window. Named separately from `from`/`to` so nothing that already reads
        # those changes meaning underneath it.
        "window_from": str(window[0]) if window and window[0] else None,
        "window_to": str(window[1]) if window and window[1] else None,
        "days": days,
        "requested_from": start.isoformat(),
        "requested_to": end.isoformat(),
        "settled_to": settled_to,
        "daily": [{"day": str(d), "cost": round(c or 0, 2)}
                  for d, c in daily_by_currency.get(currency, [])],
        "daily_by_currency": {code: [{"day": str(d), "cost": round(c or 0, 2)} for d, c in values]
                              for code, values in daily_by_currency.items()},
        "note": ("Some currency groups could not be assessed from verified billing coverage and currency evidence." if unavailable else
                 coverage["reason"] if coverage_partial else
                 "No billing data covers the selected period." if not codes else None) if merged or unavailable or not codes else (
            "Nothing unusual in the loaded period. Days are compared against a rolling "
            f"{WINDOW}-day median, with a currency-normalized monetary threshold."
        ),
    }


def _settled_to(totals: list[tuple]) -> str | None:
    """The last day whose figure is worth judging, given restatement."""
    if not totals:
        return None
    days = [str(d) for d, _ in totals]
    return days[-(SETTLE_DAYS + 1)] if len(days) > SETTLE_DAYS else days[0]
