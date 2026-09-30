"""Daily billing quantities and effective rates for pure hour-priced meters.

Usage quantities are billed hours, not measured uptime. Coverage uses estimated capacity
from the busiest billing day and the first-to-last billed-date span. It does not establish
service availability, provisioning dates, instance count or shutdown eligibility.

Legacy response names (uptime_pct, always_on, run_rate) remain for API compatibility.
"""
from __future__ import annotations

import logging
import re
from typing import Any

log = logging.getLogger("cloudlens.uptime")

# Enough for a large estate; beyond this the response says it is partial rather than quietly
# dropping the tail of a table someone is reading as complete.
MAX_ROWS = 4000

# Per-meter-per-day cells, sent so the tab can redraw its charts under a filter without asking
# the server again. A filter that changed the table but left the charts showing the whole estate
# would be answering a question nobody asked, so the detail has to be here — but a large estate
# over 90 days can reach six figures, and past this the charts fall back to estate-wide totals
# and the tab says so rather than drawing a series from a silently truncated half of the data.
MAX_DAILY_CELLS = 150_000

# Pure time units only: "Hours", "1 Hour", "1/Hour", "10 Hours", "100 Hours". Anchored at
# both ends so "1 GiB Hour" and "1 GiB Second" — which are quantity x duration, not duration —
# cannot match. See the module docstring. Shared with `hourly_usage`, which keeps only these
# rows; one definition so the summary and this filter cannot disagree.
from .hourly_usage import HOUR_UNIT  # noqa: E402

# The leading multiplier: a "10 Hours" row of quantity 3 is 30 hours, not 3.
# No capture group: PostgreSQL substring would otherwise return only the decimal part.
HOUR_MULTIPLIER = r'^[0-9]+(?:\.[0-9]+)?'

# High billing coverage threshold, not proof that the workload stayed running.
ALWAYS_ON = 0.97

# Only identified VM compute meters support this proportional deallocation scenario.
SCHEDULABLE = ("virtual machine",)

# Meters that ride alongside a schedulable resource without being one. A licence line is the
# common case: it is named after the service it licenses, so a plain substring match counts it
# twice — once as the machine and once as its licence.
NOT_SCHEDULABLE = ("licen", "disk", "storage", "bandwidth", "ip address", "software")

WEEKDAYS = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")


def hourly_cost(scope: list[str] | None = None, days: int = 30,
                currency: str | None = None) -> dict[str, Any]:
    """Billed hours, derived rates and estimated billed coverage for the window."""
    from . import reporting, resource_tags
    from .warehouse import warehouse

    days = reporting.period_days(days)
    with warehouse.reader(scope=scope, currency=currency, usage=("hourly", "tags")) as r:
        # Each relation below is the precomputed summary when it can answer this
        # connection exactly and the same grain over the scoped `costs` view when it
        # cannot, so the SQL is identical either way; only the speed differs. Reading
        # `costs` here directly timed out on a 16.9M-row estate.
        agg = getattr(r, "agg", "costs")
        source = getattr(r, "hourly", None) or "costs"
        tag_source = getattr(r, "tags", None)
        # Anchored to the newest data rather than to today, for the same reason every other
        # warehouse view is: cost lands a day or two late, so a window ending at CURRENT_DATE
        # drops the most recent complete day and, on a stale warehouse, returns nothing at all
        # — which reads as "nothing ran" instead of "this data is old".
        edge = r.rows(f'SELECT max("ChargePeriodStart") AS hi FROM {agg}')
        hi = edge[0]["hi"] if edge and edge[0]["hi"] else None
        if hi is None:
            return _empty(days)

        start = reporting.start_sql(hi, days)
        window = f'"ChargePeriodStart" > {start}'

        # Every row in the window, so the tab can say what share of spend is hour-billed at
        # all. Without it a small hourly total is indistinguishable from a small estate.
        totals = r.rows(
            f"""
            SELECT COALESCE(NULLIF("BillingCurrency", ''), 'unknown') AS currency,
                   SUM(COALESCE("BilledCost", 0))         AS cost
            FROM {agg} WHERE {window} GROUP BY 1
            """,
            limit=64,
        )
        currency_code, mixed = _dominant(totals)
        if currency_code is None:
            return _empty(days)
        if mixed:
            # A minority currency must not consume the dominant currency's detail cap.
            code = currency_code.replace("'", "''")
            window += f' AND COALESCE(NULLIF("BillingCurrency", \'\'), \'unknown\') = \'{code}\''

        hourly = f"""
            SELECT
              COALESCE(NULLIF("ResourceId", ''), "ResourceName", '(unnamed)') COLLATE "C" AS id,
              COALESCE(NULLIF("ResourceName", ''), '(unnamed)')               AS name,
              COALESCE("ServiceName", '')                                     AS service,
              COALESCE("SubAccountName", '')                                  AS subscription,
              COALESCE("ResourceGroup", '')                                   AS resource_group,
              COALESCE("RegionName", '')                                      AS region,
              COALESCE("MeterName", '') COLLATE "C"                           AS meter,
              COALESCE(NULLIF("BillingCurrency", ''), 'unknown')              AS currency,
              "ChargePeriodStart"                                             AS day,
              COALESCE("PricingQuantity", 0) * COALESCE(
                TRY_CAST(regexp_extract(trim("UnitOfMeasure"), '{HOUR_MULTIPLIER}') AS DOUBLE),
                1) AS hours,
              COALESCE("BilledCost", 0)                                       AS cost
            FROM {source}
            WHERE {window}
              AND regexp_matches(lower(trim(COALESCE("UnitOfMeasure", ''))), '{HOUR_UNIT}')
        """

        # Tags for the meters actually returned, looked up per resource rather than carried
        # through every row: they are most of the bytes of a charge and vary within a day.
        if tag_source:
            tagged = ("SELECT meters.*, tg.tags FROM meters "
                      + resource_tags.lateral_sql(tag_source, "meters.id", "meters.currency",
                                                  start, str(hi)[:10])
                      + " ORDER BY meters.cost DESC")
        else:
            tagged = "SELECT meters.*, NULL AS tags FROM meters ORDER BY meters.cost DESC"

        # One row per resource, meter and day is the grain everything else is built from: the
        # peak day gives the instance count, and the day list gives the weekday profile.
        #
        # The resource id and meter are grouped byte for byte (`COLLATE "C"` above): under
        # the database's linguistic collation, sorting 600K long resource ids that share
        # their first 100 characters took 30 of this query's 34s on the live estate, and
        # 3.8s byte-wise. Equality is the same under both -- the collation is deterministic
        # -- so the groups cannot differ. The keys return to the default collation before
        # anything joins on them, or the tag lookup can no longer use its index.
        #
        # Each returned meter carries its own daily cells. They were a second pass over
        # every meter in the estate -- 594K cells on the live estate, over the cap and then
        # discarded whole -- when only the returned meters' cells are ever used.
        rows = r.rows(
            f"""
            WITH src AS ({hourly}),
                 daily AS (
                   SELECT id, meter, currency, day,
                          SUM(hours) AS hours, SUM(cost) AS cost,
                          MAX(name) AS name, MAX(service) AS service,
                          MAX(subscription) AS subscription,
                          MAX(resource_group) AS resource_group, MAX(region) AS region
                   FROM src GROUP BY id, meter, currency, day
                 ),
                 ranked AS (
                   SELECT id, meter, currency,
                          MAX(name)            AS name,
                          MAX(service)         AS service,
                          MAX(subscription)    AS subscription,
                          MAX(resource_group)  AS resource_group,
                          MAX(region)          AS region,
                          SUM(hours)           AS hours,
                          SUM(cost)            AS cost,
                          MAX(hours)           AS peak_day_hours,
                          COUNT(*)             AS days_billed,
                          MIN(day)             AS first_day,
                          MAX(day)             AS last_day,
                          json_agg(json_build_array(day, hours, cost) ORDER BY day)
                            FILTER (WHERE hours > 0) AS cells
                   FROM daily
                   GROUP BY id, meter, currency
                   HAVING SUM(hours) > 0
                   ORDER BY cost DESC
                   LIMIT {MAX_ROWS + 1}
                 ),
                 meters AS (
                   SELECT id COLLATE "default" AS id, meter COLLATE "default" AS meter,
                          currency, name, service, subscription, resource_group, region,
                          hours, cost, peak_day_hours, days_billed, first_day, last_day, cells
                   FROM ranked
                 )
            {tagged}
            """,
            limit=MAX_ROWS + 1,
        )

        # The estate day by day. Two series in one pass: what the hour-billed part of the
        # estate cost, and how many hours it ran — their ratio is the effective rate, and it
        # moving is the signal that something was resized rather than merely left on.
        daily = r.rows(
            f"""
            WITH src AS ({hourly})
            SELECT day, currency, SUM(hours) AS hours, SUM(cost) AS cost
            FROM src GROUP BY day, currency ORDER BY day
            """,
            limit=800,
        )

        # The same series, but per meter, so a filter can rebuild the charts from the rows it
        # kept -- already gathered per returned meter above. The estate series is kept
        # separate because the estate totals must stay exact even when this is dropped.
        cells = [
            {"id": row["id"], "meter": row["meter"], "currency": row["currency"],
             "day": day, "hours": hours, "cost": cost}
            for row in rows[:MAX_ROWS]
            for day, hours, cost in reporting.json_list(row.pop("cells", None))
        ]
        for row in rows[MAX_ROWS:]:
            row.pop("cells", None)

    # A later currency filter cannot restore rows omitted by the database fetch cap.
    truncated = len(rows) > MAX_ROWS
    cells_truncated = len(cells) > MAX_DAILY_CELLS
    if cells_truncated:
        cells = []
    if mixed:
        rows = [x for x in rows if (x.get("currency") or "unknown") == currency_code]
        daily = [x for x in daily if (x.get("currency") or "unknown") == currency_code]
        cells = [x for x in cells if (x.get("currency") or "unknown") == currency_code]

    if truncated:
        log.info("hourly_cost: more than %d hour-billed meters; response is partial", MAX_ROWS)
        rows = rows[:MAX_ROWS]

    total_spend = sum(float(t["cost"] or 0.0) for t in totals
                      if not mixed or (t.get("currency") or "unknown") == currency_code)

    resources = [_shape(row) for row in rows]
    # Attaches each meter's tags and builds the estate-wide breakdown in one pass, before the
    # sort below reorders `resources` away from `rows`.
    tags = _tag_dimension(rows, resources)
    # Resource before meter: someone reading this wants the expensive machine, not the
    # expensive line item on it, and a VM's compute and licence meters are two rows here.
    resources.sort(key=lambda x: x["cost"], reverse=True)

    trend = _trend(daily)
    dates, per_meter, dropped = _cells(cells, {r["key"] for r in resources}, trend["labels"])
    return {
        "days": days,
        "latest": str(hi),
        "currency": currency_code,
        "mixed_currency": mixed,
        "truncated": truncated,
        "totals": _totals(resources, total_spend, trend),
        "resources": resources,
        "trend": trend,
        "weekday": _weekday(daily),
        "schedules": SCHEDULE_PRESETS,
        **tags,
        # What the client needs to answer the same questions about a subset: the day axis, and
        # each meter's hours and cost against it.
        "dates": dates,
        "daily": per_meter,
        "daily_dropped": dropped or cells_truncated,
        "total_spend": round(total_spend, 4),
    }


def _cells(cells: list[dict[str, Any]], keep: set[str],
           labels: list[str]) -> tuple[list[str], dict[str, list[list[float]]], bool]:
    """Per-meter daily hours and cost, indexed against the trend's day axis.

    Positions rather than dates, and one array per meter rather than a row per cell: the client
    sums these on every filter change, and a list of objects carrying a repeated date string
    costs both bandwidth and a parse on each redraw.

    Days come from the data, so a day Azure never billed is absent rather than zero — a gap
    stays a gap, and drawing it as nothing spent would be a statement the data does not make.
    """
    if len(cells) > MAX_DAILY_CELLS:
        log.info("hourly_cost: over %d daily cells; dropping per-meter detail", MAX_DAILY_CELLS)
        return labels, {}, True

    index = {d: i for i, d in enumerate(labels)}
    out: dict[str, list[list[float]]] = {}
    for cell in cells:
        key = f"{cell.get('id')}\u0000{cell.get('meter')}"
        # A meter dropped by the row cap has no line in the table, so its cells would be
        # invisible weight in the payload and would push the "all" charts above the table
        # they are meant to describe.
        if key not in keep:
            continue
        position = index.get(str(cell.get("day")))
        if position is None:
            continue
        out.setdefault(key, []).append([
            position,
            round(float(cell.get("hours") or 0.0), 3),
            round(float(cell.get("cost") or 0.0), 6),
        ])
    # In day order: the query does not order its groups, so the same data could otherwise
    # arrive as a differently ordered series from one run, or one source table, to the next.
    for series in out.values():
        series.sort(key=lambda cell: cell[0])
    return labels, out, False


# Weekly assumptions for a conditional client-side estimate, not approved workload schedules.
SCHEDULE_PRESETS = [
    {"id": "business", "label": "Business hours (7am–7pm, Mon–Fri)", "hours": 60},
    {"id": "extended", "label": "Extended hours (6am–8pm, Mon–Fri)", "hours": 70},
    {"id": "weekdays", "label": "Weekdays only (24x5)", "hours": 120},
    {"id": "nights", "label": "Nights off (6am–10pm, every day)", "hours": 112},
    # The inverse of "weekdays only", for the workloads that exist for the opposite reason:
    # weekend batch runs, month-end reporting and test estates that are only wanted when
    # nobody is using the production one.
    {"id": "weekend", "label": "Weekends only (Sat–Sun, 24x2)", "hours": 48},
]


def _shape(row: dict[str, Any]) -> dict[str, Any]:
    """One resource-meter with estimated billing capacity, not operational uptime."""
    hours = float(row.get("hours") or 0.0)
    cost = float(row.get("cost") or 0.0)
    peak = float(row.get("peak_day_hours") or 0.0)

    # Approximate capacity only: billing meters and scaling can differ from instance counts.
    units = max(1, round(peak / 24.0)) if peak > 0 else 1

    first, last = row.get("first_day"), row.get("last_day")
    span = _span_days(first, last)
    capacity = units * 24.0 * span
    uptime = min(1.0, hours / capacity) if capacity > 0 else 0.0

    service = str(row.get("service") or "")
    schedulable = bool(row.get("id")) and _schedulable(service, row.get("id"), row.get("meter"))
    return {
        "id": row.get("id"),
        # Joins this row to its daily cells. A resource can bill several hourly meters — a VM's
        # compute and its licence are two lines here — so the resource id alone is not unique.
        # NUL separates because it cannot occur in either half.
        "key": f"{row.get('id')}\u0000{row.get('meter')}",
        "name": row.get("name"),
        "service": service,
        "subscription": row.get("subscription"),
        "resource_group": row.get("resource_group"),
        "region": row.get("region"),
        "meter": row.get("meter"),
        "hours": round(hours, 1),
        "cost": round(cost, 4),
        # The number the whole tab exists for: what one hour of this resource costs.
        "rate": round(cost / hours, 6) if hours > 0 else 0.0,
        "units": units,
        "span_days": span,
        "days_billed": int(row.get("days_billed") or 0),
        "uptime_pct": round(uptime * 100, 1),
        "always_on": uptime >= ALWAYS_ON,
        "schedulable": schedulable,
        # "possible for this kind of resource, and worth a human look" — never "approved".
        # The browser applies the resource-shape guards on top of this.
        "schedule_status": "needs_review" if schedulable else "not_assessed",
        "coverage_basis": "billing_estimate",
        "first_day": str(first) if first else None,
        "last_day": str(last) if last else None,
    }


def _schedulable(service: str, resource_id: str | None = None,
                 meter: str | None = None) -> bool:
    """Whether stopping this out of hours is a thing Azure actually lets you do.

    A service-family judgement, not a workload one: it says a schedule is *possible* for this
    kind of resource, never that this particular instance is safe to stop. The tab carries that
    distinction — the row is marked for review and the saving is stated as a ceiling.
    """
    low = f"{service} {meter or ''}".lower()
    if any(word in low for word in NOT_SCHEDULABLE):
        return False
    if not any(word in service.lower() for word in SCHEDULABLE):
        return False
    if resource_id is None:
        return True
    return bool(meter and re.fullmatch(
        r"/subscriptions/[^/]+/resourcegroups/[^/]+/providers/microsoft\.compute/virtualmachines/[^/]+/?",
        str(resource_id), re.I))


def _tag_dimension(rows: list[dict[str, Any]],
                   resources: list[dict[str, Any]]) -> dict[str, Any]:
    """Attach each meter's tags and total hour-billed spend by tag key and value.

    Hourly meters are where a schedule decision is actually taken, and "which application owns
    these hours" is the question that decides it. The Tags tab answers it for all spend; without
    it here, someone narrowing to one team's VMs had to leave, filter, and come back with a list
    of resource names to re-type.

    Values are counted separately from keys for the same reason they are in the Tags tab:
    `Environment` is not a cost centre, `Environment=prod` is. Tags are read from the cost rows,
    so a tag attached after the last metered hour is invisible here — the same limit the Tags
    tab carries, and the reason an empty result is reported as untagged spend rather than as a
    missing tag.
    """
    from .tags import _pairs

    pool: list[str] = []
    pool_index: dict[str, int] = {}

    def intern(value: str) -> int:
        idx = pool_index.get(value)
        if idx is None:
            idx = len(pool)
            pool_index[value] = idx
            pool.append(value)
        return idx

    keys: dict[str, dict[str, Any]] = {}
    tagged = {"cost": 0.0, "hours": 0.0, "meters": 0}
    untagged = {"cost": 0.0, "hours": 0.0, "meters": 0}

    for row, resource in zip(rows, resources):
        pairs = _pairs(row.get("tags"))
        resource["keys"] = [k for k, _ in pairs]
        # Parallel to `keys`: v[i] indexes the value of keys[i] in `tag_values`.
        resource["v"] = [intern(v) for _, v in pairs]

        cost, hours = resource["cost"], resource["hours"]
        bucket = tagged if pairs else untagged
        bucket["cost"] += cost
        bucket["hours"] += hours
        bucket["meters"] += 1

        for k, v in pairs:
            slot = keys.setdefault(
                k, {"key": k, "cost": 0.0, "hours": 0.0, "meters": 0, "distinct": set()})
            slot["cost"] += cost
            slot["hours"] += hours
            slot["meters"] += 1
            slot["distinct"].add(v)

    out = sorted(keys.values(), key=lambda k: (-k["cost"], k["key"].lower()))
    for slot in out:
        slot["cost"] = round(slot["cost"], 2)
        slot["hours"] = round(slot["hours"], 1)
        # How many values sit under this key: one is a label, nine is a breakdown worth opening.
        slot["values"] = len(slot.pop("distinct"))

    return {
        "tag_keys": out,
        "tag_values": pool,
        "tagged": {"cost": round(tagged["cost"], 2), "hours": round(tagged["hours"], 1),
                   "meters": tagged["meters"]},
        "untagged": {"cost": round(untagged["cost"], 2), "hours": round(untagged["hours"], 1),
                     "meters": untagged["meters"]},
    }


def _span_days(first: Any, last: Any) -> int:
    """Inclusive first-to-last billed-date span, not the resource's lifetime."""
    try:
        return max(1, (last - first).days + 1)
    except (TypeError, AttributeError):
        return 1


def _trend(daily: list[dict[str, Any]]) -> dict[str, Any]:
    """Hours, cost and the effective rate, day by day."""
    by_day: dict[str, dict[str, float]] = {}
    for row in daily:
        day = str(row.get("day"))
        cell = by_day.setdefault(day, {"hours": 0.0, "cost": 0.0})
        cell["hours"] += float(row.get("hours") or 0.0)
        cell["cost"] += float(row.get("cost") or 0.0)

    labels = sorted(by_day)
    return {
        "labels": labels,
        "hours": [round(by_day[d]["hours"], 1) for d in labels],
        "cost": [round(by_day[d]["cost"], 4) for d in labels],
        # Cost per hour actually consumed. Flat means the estate is the same size; a step means
        # something was resized or a reservation started, which a cost line alone cannot tell
        # you apart from something simply running longer.
        "rate": [
            round(by_day[d]["cost"] / by_day[d]["hours"], 6) if by_day[d]["hours"] else 0.0
            for d in labels
        ],
    }


def _weekday(daily: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Average hours and cost per day of the week.

    This is the closest thing to a clock that daily billing supports, and unlike an invented
    hour-of-day curve it is measured: if the estate is smaller at weekends, the hours are
    genuinely not there. A flat profile is itself the answer — nothing is on a schedule.
    """
    buckets: dict[int, dict[str, float]] = {}
    for row in daily:
        day = row.get("day")
        try:
            index = day.weekday()
        except AttributeError:
            continue
        cell = buckets.setdefault(index, {"hours": 0.0, "cost": 0.0, "n": 0.0})
        cell["hours"] += float(row.get("hours") or 0.0)
        cell["cost"] += float(row.get("cost") or 0.0)
        cell["n"] += 1

    out = []
    for index, label in enumerate(WEEKDAYS):
        cell = buckets.get(index)
        n = cell["n"] if cell else 0
        out.append({
            "day": label,
            "short": label[:3],
            "days": int(n),
            "hours": round(cell["hours"] / n, 1) if n else 0.0,
            "cost": round(cell["cost"] / n, 4) if n else 0.0,
        })
    return out


def _totals(resources: list[dict[str, Any]], total_spend: float,
            trend: dict[str, Any]) -> dict[str, Any]:
    """The headline figures, all derived from the rows already computed."""
    hours = sum(r["hours"] for r in resources)
    cost = sum(r["cost"] for r in resources)
    always_on = [r for r in resources if r["always_on"]]
    idle_candidates = [r for r in resources if r["always_on"] and r["schedulable"]]

    # What the estate burns per hour right now: recent, so a resource deleted three weeks ago
    # does not drag it down, but not the last day alone. The newest day in a cost export is
    # almost always partial — this estate's final day reads 17.50 against a 29.00 run rate —
    # so taking it literally understates the burn by 40%. The median of the last week is
    # recent and immune to that one short day.
    return {
        "hours": round(hours, 1),
        "cost": round(cost, 4),
        "total_spend": round(total_spend, 4),
        "coverage_pct": round(cost / total_spend * 100, 1) if total_spend > 0 else 0.0,
        # The estate-wide blended rate: every hour-billed thing, averaged by spend.
        "avg_rate": round(cost / hours, 6) if hours > 0 else 0.0,
        "run_rate": round(_recent_daily(trend["cost"]) / 24.0, 6),
        "run_hours": round(_recent_daily(trend["hours"]), 1),
        "resources": len({r["id"] for r in resources}),
        "meters": len(resources),
        "always_on": len(always_on),
        "always_on_cost": round(sum(r["cost"] for r in always_on), 4),
        "schedulable": len(idle_candidates),
        "schedulable_cost": round(sum(r["cost"] for r in idle_candidates), 4),
    }


def _recent_daily(series: list[float]) -> float:
    """A typical recent day, resistant to the partial one at the end.

    Cost exports restate for a day or two, so the newest day is routinely a fraction of a real
    one. A mean would carry that straight into the headline; a median over the last week
    ignores it unless several days are short, in which case the estate genuinely did shrink.
    """
    tail = [float(v) for v in series[-7:]]
    if not tail:
        return 0.0
    tail.sort()
    mid = len(tail) // 2
    return tail[mid] if len(tail) % 2 else (tail[mid - 1] + tail[mid]) / 2


def _dominant(rows: list[dict[str, Any]]) -> tuple[str | None, bool]:
    """The currency most of the money is in, and whether there was more than one.

    Adding EUR to USD produces a confident, meaningless number, so the estate is reported in
    the currency that dominates it and the tab says that is what it did.
    """
    totals: dict[str, float] = {}
    for row in rows:
        code = row.get("currency") or "unknown"
        totals[code] = totals.get(code, 0.0) + float(row.get("cost") or 0.0)
    if not totals:
        return None, False
    if len(totals) == 1:
        return next(iter(totals)), False
    return max(totals, key=lambda c: totals[c]), True


def _empty(days: int) -> dict[str, Any]:
    return {
        "days": days, "latest": None, "currency": None, "mixed_currency": False,
        "truncated": False, "resources": [], "weekday": [],
        "trend": {"labels": [], "hours": [], "cost": [], "rate": []},
        "schedules": SCHEDULE_PRESETS,
        "tag_keys": [], "tag_values": [],
        "tagged": {"cost": 0.0, "hours": 0.0, "meters": 0},
        "untagged": {"cost": 0.0, "hours": 0.0, "meters": 0},
        "dates": [], "daily": {}, "daily_dropped": False, "total_spend": 0.0,
        "totals": {"hours": 0.0, "cost": 0.0, "total_spend": 0.0, "coverage_pct": 0.0,
                   "avg_rate": 0.0, "run_rate": 0.0, "run_hours": 0.0, "resources": 0,
                   "meters": 0, "always_on": 0, "always_on_cost": 0.0, "schedulable": 0,
                   "schedulable_cost": 0.0},
    }
