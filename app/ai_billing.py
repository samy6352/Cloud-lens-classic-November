"""Scoped AI billing and optional, read-only Azure Monitor token evidence.

Billing products are labels, not deployment inventory. Meter quantities are never
converted to tokens: even a meter labelled "1M Tokens" can report raw Units.
"""
from __future__ import annotations

import asyncio
import hashlib
import math
import re
from datetime import UTC, date, datetime, time, timedelta
from urllib.parse import quote, urlsplit

import httpx

from . import ai_anomalies, reporting
from .cost import ARM, CostError, azure

MAX_GROUPS = 1000
MAX_ACCOUNTS = 12
# Accounts probed at once. Six rather than four because the budget arithmetic below is what
# constrains this, and six is the safer end of it: twelve accounts become two rounds of twenty
# seconds instead of three, so the worst case falls from 60s against a 65s report budget to
# 40s. More headroom and a shorter wait from the same numbers.
#
# Not raised further. Each account probe fans out into several Azure Monitor reads of its own,
# so this multiplies rather than adds, and Azure Monitor answers a throttled read the same way
# it answers a denied one. This module treats that as missing telemetry rather than as zero —
# correct, but a report full of honest gaps is worse than a slower complete one.
MAX_CONCURRENT = 6
ACCOUNT_TIMEOUT = 20
REPORT_TIMEOUT = 65
MAX_DEPLOYMENTS = 100
MAX_PAGES = 3
MAX_SERIES = 101
# Azure Monitor returns at most this many points per series. Ask for more and it does not say
# so: it silently drops the oldest hours *and re-anchors the grid on the end of the timespan*.
# A ragged end therefore yields ragged stamps (13:19, 14:19, ...) for the whole series, and
# _series rejects every one of them as not being an hour boundary. Keeping the request inside
# the cap, and the end on the hour, is what stops that from happening.
MAX_POINTS = 744
RETENTION_DAYS = 93
INTERVAL = "PT1H"
DEPLOYMENT_DIMENSION = "ModelDeploymentName"
INPUT = "ProcessedPromptTokens"
OUTPUT = "GeneratedTokens"
TOTAL = "TokenTransaction"
UTILIZATION = "AzureOpenAIProvisionedManagedUtilizationV2"
METRIC_SOURCE = "Azure Monitor"
COST_SOURCE = "Scoped warehouse BilledCost"
COST_NOTE = "Billing is recorded at resource/meter level; not allocated to this deployment"
_UUID = re.compile(r"[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}")
_FAMILIES = ("ai and machine learning", "ai + machine learning")
_SERVICES = (
    "openai", "cognitive", "ai search", "azure search", "bing", "machine learning",
    "machinelearning", "foundry", "azure ai services", "azure ml",
)
_PRODUCTS = (
    "openai", "foundry", "cognitive", "ai search", "grounding with bing",
    "form recognizer", "defender for ai", "machine learning",
)
_PROVIDERS = (
    "/providers/microsoft.cognitiveservices/", "/providers/microsoft.search/searchservices/",
    "/providers/microsoft.machinelearningservices/", "/providers/microsoft.machinelearning/",
    "/providers/microsoft.bing/",
)


def _now() -> datetime:
    return datetime.now(UTC).replace(microsecond=0)


def _iso(value: datetime) -> str:
    return value.isoformat().replace("+00:00", "Z")


def _scope(scope: list[str]) -> list[str]:
    if not isinstance(scope, list) or len(scope) > 500:
        raise ValueError("Explicit subscription UUIDs are required.")
    if any(not isinstance(s, str) or not _UUID.fullmatch(s) for s in scope):
        raise ValueError("Explicit subscription UUIDs are required.")
    return sorted({s.lower() for s in scope})


def _subscription(value: object) -> str:
    value = str(value or "").lower()
    if value.startswith("/subscriptions/"):
        value = value.removeprefix("/subscriptions/")
    return value if _UUID.fullmatch(value) else ""


def _is_ai(row: dict) -> bool:
    return (
        str(row.get("ServiceFamily") or "").strip().lower() in _FAMILIES
        or any(s in str(row.get("ServiceName") or "").lower() for s in _SERVICES)
        or any(s in str(row.get("ProductName") or "").lower() for s in _PRODUCTS)
        or any(s in str(row.get("ResourceId") or "").lower() for s in _PROVIDERS)
    )


def _ai_predicate() -> str:
    families = ", ".join(f"'{s}'" for s in _FAMILIES)
    clauses = [f'LOWER(TRIM(COALESCE("ServiceFamily", \'\'))) IN ({families})']
    for column, labels in (("ServiceName", _SERVICES), ("ProductName", _PRODUCTS),
                           ("ResourceId", _PROVIDERS)):
        clauses.extend(f'LOWER(COALESCE("{column}", \'\')) LIKE \'%{label}%\''
                       for label in labels)
    return "(" + " OR ".join(clauses) + ")"


def _number(value: object) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        result = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return result if math.isfinite(result) else None


def _sum(values: list[float | None]) -> float | None:
    if not values or any(v is None for v in values):
        return None
    try:
        return _number(math.fsum(values))
    except OverflowError:
        return None


def _blank(window: dict | None, note: str, status: str = "unavailable") -> dict:
    return {
        "value": None, "status": status, "note": note, "source": METRIC_SOURCE,
        "window": window, "samples": 0, "expected_samples": 0,
    }


def _tokens(window: dict | None, note: str, status: str = "unavailable") -> dict:
    return {**_blank(window, note, status), "input": None, "output": None,
            "input_status": status, "output_status": status,
            "aggregation": "Total", "unit": "tokens"}


def _utilization(window: dict | None, note: str, status: str = "unavailable") -> dict:
    return {**_blank(window, note, status), "maximum": None, "aggregation": "Average",
            "unit": "Percent"}


def _billing_key(row: dict) -> tuple:
    sub = _subscription(row.get("subscription_id"))
    rid = str(row.get("resource_id") or "").lower()
    return ((sub, rid) if rid else
            (sub, "", str(row.get("resource_name") or ""), str(row.get("service") or "")))


def _completed_data(reader) -> dict:
    # Use the committed source publication day, never today's moving date:
    # in-progress charges become eligible only after a later-day import, not
    # simply because midnight passed without fetching any new source data.
    # meta.updated_at is a timestamp written by now() in the warehouse session's
    # time zone. Convert that publication instant to a UTC day explicitly.
    published = (
        "(SELECT CAST(updated_at AT TIME ZONE current_setting('TimeZone') "
        "AT TIME ZONE 'UTC' AS DATE) FROM meta WHERE key = 'data_generation')")
    # The rollup holds every charge date `costs` does, so the latest one is the same
    # read at a fraction of the rows; this runs on every AI Billing request.
    source = getattr(reader, "agg", "costs")
    rows = reader.rows(
        f'SELECT max("ChargePeriodStart") AS hi, {published} AS published_day FROM {source} '
        f'WHERE "ChargePeriodStart" < {published}')
    return rows[0] if rows else {}


def _measures(summarised: bool) -> dict[str, str]:
    """Data-quality counts, spelled for the detail table or for `usage_signals`.

    The summary carries each count as a stored sum, so `count(*)` over charges is
    `sum("Charges")` over its groups and every check below keeps its exact meaning.
    """
    if not summarised:
        return {
            "rows": "COUNT(*)",
            "cost_rows": 'COUNT("BilledCost")',
            "negative_rows": 'SUM(CASE WHEN "BilledCost" < 0 THEN 1 ELSE 0 END)',
            "quantity": ('CASE WHEN COUNT(NULLIF(TRIM("UnitOfMeasure"), \'\')) = COUNT(*) '
                         'AND COUNT("PricingQuantity") = COUNT(*) '
                         'THEN SUM("PricingQuantity") END'),
        }
    return {
        "rows": 'CAST(SUM("Charges") AS bigint)',
        "cost_rows": 'CAST(SUM("CostRows") AS bigint)',
        "negative_rows": 'CAST(SUM("NegativeRows") AS bigint)',
        "quantity": ('CASE WHEN SUM(CASE WHEN NULLIF(TRIM("UnitOfMeasure"), \'\') IS NULL '
                     'THEN 0 ELSE "Charges" END) = SUM("Charges") '
                     'AND SUM("QuantityRows") = SUM("Charges") '
                     'THEN SUM("PricingQuantity") END'),
    }


def snapshot_data_day(scope: list[str], filters: dict | None = None) -> str:
    """Latest completed imported day in this scope, not the wall clock.

    Look beyond the selected month so tomorrow's import can refresh corrected
    historical reports too. Same-day corrections require an explicit retry.
    """
    scope = _scope(scope)
    if not scope:
        return "empty"
    from .warehouse import warehouse

    with reporting.select(None), warehouse.reader(scope=scope, filters=filters) as reader:
        available = _completed_data(reader)
    return str(available["hi"])[:10] if available.get("hi") else "empty"


def _cost_history(scope: list[str], currency: str | None, filters: dict | None,
                  response: dict) -> None:
    for item in response["services"]:
        item["_cost_history"] = {}
        item["_cost_history_note"] = ""
    if not response["services"]:
        return
    if response["truncated"]:
        for item in response["services"]:
            item["_cost_history_note"] = "Billing group cap prevents complete, comparable account cost anomaly checks."
        return

    import psycopg

    from .warehouse import warehouse

    first = date.fromisoformat(response["window"]["from"]) - timedelta(days=ai_anomalies.WINDOW)
    last = date.fromisoformat(response["window"]["to"])
    try:
        # Only this explicit bounded baseline read bypasses the selected-month view;
        # the selected cost/quantity query and returned reporting window stay unchanged.
        with reporting.select(None), warehouse.reader(
                scope=scope, currency=currency, filters=filters, usage=("signals",)) as reader:
            source = getattr(reader, "signals", None)
            m = _measures(bool(source))
            rows = reader.rows(
                f"""
                SELECT LOWER(COALESCE("SubAccountId", '')) AS subscription_id,
                  LOWER(COALESCE("ResourceId", '')) AS resource_id,
                  CASE WHEN COALESCE("ResourceId", '') = ''
                       THEN COALESCE("ResourceName", '') ELSE '' END AS resource_name,
                  CASE WHEN COALESCE("ResourceId", '') = ''
                       THEN COALESCE("ServiceName", '') ELSE '' END AS service,
                  COALESCE(NULLIF("BillingCurrency", ''), 'unknown') AS currency,
                  CAST("ChargePeriodStart" AS DATE) AS day,
                  SUM("BilledCost") AS cost, {m["rows"]} AS billing_rows,
                  {m["cost_rows"]} AS cost_rows,
                  {m["negative_rows"]} AS negative_rows
                FROM {source or "costs"} WHERE "ChargePeriodStart" >= DATE '{first.isoformat()}'
                  AND "ChargePeriodStart" <= DATE '{last.isoformat()}' AND {_ai_predicate()}
                GROUP BY 1, 2, 3, 4, 5, 6
                ORDER BY subscription_id, resource_id, resource_name, service, currency, day
                LIMIT {ai_anomalies.MAX_DAILY_GROUPS + 1}
                """, limit=ai_anomalies.MAX_DAILY_GROUPS + 1,
            )
        if len(rows) > ai_anomalies.MAX_DAILY_GROUPS:
            for item in response["services"]:
                item["_cost_history_note"] = (
                    f"Daily billing query exceeded {ai_anomalies.MAX_DAILY_GROUPS} groups; "
                    "partial source data was not used for cost anomaly checks.")
            return
    except (psycopg.Error, ValueError):
        for item in response["services"]:
            item["_cost_history_note"] = "Historical billing read or currency conversion unavailable; selected costs were retained."
        return
    wanted = {item["_billing_key"]: item for item in response["services"]}
    seen = set()
    for row in rows:
        key = _billing_key(row)
        item = wanted.get(key)
        if item is None:
            continue
        code = str(row.get("currency") or "unknown")
        # Never add a historical currency to selected totals or selected-row currencies.
        if code not in {entry["currency"] for entry in item["cost_by_currency"]}:
            continue
        try:
            day = date.fromisoformat(str(row.get("day"))[:10])
        except ValueError:
            item["_cost_history_note"] = "Invalid historical billing dates prevent comparable cost anomaly checks."
            continue
        if not first <= day <= last:
            continue
        series = item["_cost_history"].setdefault(code, {"daily": {}, "negative_days": 0})
        identity = (key, code, day)
        if identity in seen:
            item["_cost_history_note"] = "Duplicate daily billing groups were rejected, not summed."
            continue
        seen.add(identity)
        amount = _number(row.get("cost"))
        if (not row.get("billing_rows") or row.get("cost_rows") != row.get("billing_rows")
                or row.get("negative_rows", 0)):
            amount = None
        if row.get("negative_rows", 0):
            series["negative_days"] += 1
        series["daily"][day.isoformat()] = amount


def _costs(scope: list[str], days: int, currency: str | None, filters: dict | None) -> dict:
    bounds = reporting.month_bounds()
    start = bounds[0] if bounds else None
    end = bounds[1] if bounds else None
    rows = []
    if scope:
        from .warehouse import warehouse

        with warehouse.reader(scope=scope, currency=currency, filters=filters,
                              usage=("signals",)) as reader:
            # Daily evidence stops at a completed UTC day, not the current hour.
            # Closed calendar months retain their exact reporting bounds.
            available = _completed_data(reader)
            hi = available.get("hi")
            if hi:
                completed = date.fromisoformat(str(available["published_day"])[:10]) - timedelta(days=1)
                end = min(date.fromisoformat(str(hi)[:10]), completed)
                start = bounds[0] if bounds else end - timedelta(days=days - 1)
                if bounds:
                    end = min(bounds[1], completed)
                    days = max(0, (end - start).days + 1)
                lower = reporting.start_sql(end, days)
                source = getattr(reader, "signals", None)
                m = _measures(bool(source))
                rows = reader.rows(
                    f"""
                    SELECT
                      LOWER(COALESCE("SubAccountId", '')) AS subscription_id,
                      MAX(COALESCE("SubAccountName", '')) AS subscription,
                      LOWER(COALESCE("ResourceId", '')) AS resource_id,
                      COALESCE("ResourceName", '') AS resource_name,
                      COALESCE("ServiceName", '') AS service,
                      COALESCE("ProductName", '') AS product,
                      COALESCE("MeterName", '') AS meter,
                      COALESCE("UnitOfMeasure", '') AS unit,
                      COALESCE(NULLIF("BillingCurrency", ''), 'unknown') AS currency,
                      SUM("BilledCost") AS cost, {m["rows"]} AS billing_rows,
                      {m["cost_rows"]} AS cost_rows,
                      {m["quantity"]} AS quantity
                    FROM {source or "costs"}
                    WHERE "ChargePeriodStart" > {lower}
                      AND "ChargePeriodStart" <= DATE '{end.isoformat()}'
                      AND {_ai_predicate()}
                    GROUP BY 1, 3, 4, 5, 6, 7, 8, 9
                    ORDER BY currency, cost DESC NULLS LAST, subscription_id,
                             resource_id, resource_name, service, product, meter, unit
                    LIMIT {MAX_GROUPS + 1}
                    """, limit=MAX_GROUPS + 1,
                )
            else:
                start = end = None
    response = {
        "window": {"from": start.isoformat() if start else None,
                   "to": end.isoformat() if end else None, "days": days,
                   "timezone": "UTC", "bounds": "inclusive"},
        "services": [], "totals": [], "truncated": len(rows) > MAX_GROUPS, "notices": [],
    }
    if response["truncated"]:
        response["notices"].append(
            f"Billing is limited to {MAX_GROUPS} resource/service/meter/currency groups; "
            "all displayed costs and totals use only that same subset.")
    grouped: dict[tuple, dict] = {}
    for row in rows[:MAX_GROUPS]:
        sub = _subscription(row.get("subscription_id"))
        if sub not in scope:
            response["notices"].append("An out-of-scope billing group was excluded.")
            continue
        rid = str(row.get("resource_id") or "").lower()
        name, service = str(row.get("resource_name") or ""), str(row.get("service") or "")
        # An absent ID must not merge unrelated unnamed resources or subscriptions.
        key = _billing_key(row)
        item = grouped.setdefault(key, {
            "id": rid or "billing:" + hashlib.sha256(repr(key).encode()).hexdigest()[:20],
            "resource_id": rid or None, "subscription_id": sub,
            "subscription": str(row.get("subscription") or ""), "resource_name": name or "(unnamed)",
            "services": set(), "models": set(), "billing_rows": 0, "meters": [], "_billing_key": key,
        })
        amount = _number(row.get("cost"))
        if row.get("cost_rows") != row.get("billing_rows"):
            amount = None
        item["services"].add(service or "(unlabelled AI service)")
        if row.get("product"):
            item["models"].add(str(row["product"]))
        item["billing_rows"] += int(row.get("billing_rows") or 0)
        item["meters"].append({
            "product": row.get("product") or None, "meter": row.get("meter") or None,
            "service": service, "quantity": _number(row.get("quantity")) if row.get("unit") else None,
            "unit": row.get("unit") or None, "cost": amount,
            "currency": str(row.get("currency") or "unknown"),
        })
    totals: dict[str, list] = {}
    for item in grouped.values():
        by_currency: dict[str, list] = {}
        for meter in item["meters"]:
            by_currency.setdefault(meter["currency"], []).append(meter["cost"])
        item["cost_by_currency"] = [{"currency": code, "cost": _sum(values)}
                                    for code, values in sorted(by_currency.items())]
        item["currency"], item["cost"] = (
            (item["cost_by_currency"][0]["currency"], item["cost_by_currency"][0]["cost"])
            if len(by_currency) == 1 else (None, None))
        item["services"] = sorted(item["services"])
        item["service"] = " / ".join(item["services"])
        item["models"] = sorted(item["models"])
        for entry in item["cost_by_currency"]:
            totals.setdefault(entry["currency"], []).append(entry["cost"])
    response["services"] = sorted(grouped.values(), key=lambda r: r["id"])
    response["totals"] = [{"currency": code, "cost": _sum(values)}
                          for code, values in sorted(totals.items())]
    if any(t["cost"] is None for t in response["totals"]):
        response["notices"].append("Some billed amounts are missing; their currency totals are unavailable, not zero.")
    if currency:
        response["notices"].append(
            f"Billed costs use the warehouse's configured {currency.upper()} display conversion; "
            "they are not estimated token charges.")
    _cost_history(scope, currency, filters, response)
    return response


def _resource_kind(item: dict, scope: list[str]) -> str | None:
    rid = item["resource_id"]
    if not isinstance(rid, str) or len(rid) > 2048 or not rid.startswith("/"):
        return None
    parts = rid.split("/")[1:]
    if len(parts) < 8 or len(parts) % 2 or any(
        not p or p in {".", ".."} or re.search(r"[%?#\\\x00-\x20\x7f]", p) for p in parts
    ):
        return None
    if (parts[0].lower() != "subscriptions" or not _UUID.fullmatch(parts[1])
            or parts[1].lower() not in scope or parts[1].lower() != item["subscription_id"]
            or parts[2].lower() != "resourcegroups" or parts[4].lower() != "providers"):
        return None
    return "/".join([parts[5], *parts[6::2]]).lower()


def _account(item: dict, scope: list[str]) -> str | None:
    return item["resource_id"] if _resource_kind(item, scope) == "microsoft.cognitiveservices/accounts" else None


def _metric_window(window: dict, now: datetime) -> tuple[dict | None, str]:
    if not window["from"]:
        return None, "No scoped billing period is available."
    start = datetime.combine(date.fromisoformat(window["from"]), time(), UTC)
    end = datetime.combine(date.fromisoformat(window["to"]) + timedelta(days=1), time(), UTC)
    # Truncate to the hour rather than stopping at "now". The hour in progress cannot supply a
    # complete PT1H sample, so nothing observable is given up -- and an end on the hour is what
    # keeps Azure Monitor's grid on the hour when it clips a series to MAX_POINTS.
    end = min(end, now).replace(minute=0, second=0, microsecond=0)
    result = {"start": _iso(start), "end": _iso(end), "days": window["days"],
              "interval": INTERVAL, "timezone": "UTC", "bounds": "start-inclusive/end-exclusive"}
    if start >= end:
        return result, "The billing window has no elapsed time available for Azure Monitor."
    if start < now - timedelta(days=RETENTION_DAYS):
        return result, (
            f"The selected billing window exceeds Azure Monitor's {RETENTION_DAYS}-day retention; "
            "no recent substitute window was queried.")
    return result, ""


def _metric_history_window(window: dict, now: datetime) -> dict:
    selected = _stamp(window["start"])
    first = selected - timedelta(days=ai_anomalies.WINDOW)
    retained = now - timedelta(days=RETENTION_DAYS)
    retained_day = datetime.combine(retained.date(), time(), UTC)
    if retained > retained_day:
        retained_day += timedelta(days=1)
    # Never ask for baseline Azure Monitor will refuse to return. Beyond MAX_POINTS it clips
    # without a nextLink and without saying so, so asking for more buys no extra history -- it
    # only hides how little was actually read.
    capped = _stamp(window["end"]) - timedelta(hours=MAX_POINTS)
    start = min(selected, max(first, retained_day, capped))
    return {**window, "start": _iso(start), "history_clipped": start > first,
            "days": math.ceil((_stamp(window["end"]) - start).total_seconds() / 86400)}


async def _get(url: str, params: dict | None = None) -> dict:
    # Individual reads are deliberately uncached: this report asserts what was
    # *observed* and when, distinguishes missing telemetry from zero, and labels deployment
    # metadata "current, not historical". A cached read would make all three claims stale
    # without saying so. The durable daily cache reuses the entire completed report
    # with its original generated_at, observation windows and coverage instead.
    payload = await azure.request("GET", url, params=params, attempts=1, cache=False)
    if not isinstance(payload, dict) or payload.get("error") or not isinstance(payload.get("value"), list):
        raise CostError("Azure returned an invalid or denied read response.")
    return payload


def _endpoint(rid: str, endpoint: str) -> str:
    return f"{ARM}{quote(rid, safe='/')}/providers/microsoft.insights/{endpoint}"


def _definition(definitions: dict, name: str, unit: str, aggregation: str) -> dict | None:
    found = [d for d in definitions["value"] if isinstance(d, dict)
             and isinstance(d.get("name"), dict)
             and str(d["name"].get("value", "")).lower() == name.lower()]
    if len(found) != 1:
        return None
    value = found[0]
    aggregations = value.get("supportedAggregationTypes") or []
    if (str(value.get("unit", "")).lower() != unit.lower()
            or aggregation.lower() not in [str(a).lower() for a in aggregations]):
        return None
    return value


def _can_split(definition: dict) -> bool:
    return any(isinstance(d, dict) and
               str(d.get("value", "")).lower() == DEPLOYMENT_DIMENSION.lower()
               for d in definition.get("dimensions") or [])


def _stamp(value: object) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return result.astimezone(UTC) if result.tzinfo else None


def _series(data: object, aggregation: str, window: dict, history_window: dict | None = None) -> dict:
    start, end = _stamp(window["start"]), _stamp(window["end"])
    query_start = _stamp((history_window or window)["start"])
    query_end = _stamp((history_window or window)["end"])
    expected = math.ceil((end - start).total_seconds() / 3600)
    output = _blank(window, "No numeric observations; missing values are not measured zero.")
    output.update(expected_samples=expected, aggregation=aggregation, _daily={})
    if not isinstance(data, list) or len(data) > math.ceil((query_end - query_start).total_seconds() / 3600) + 1:
        output["note"] = "Invalid or oversized hourly series was not aggregated."
        return output
    points = {}
    seen = set()
    bad_days = set()
    unlocated_invalid = False
    invalid = False
    for point in data:
        if not isinstance(point, dict):
            invalid = True
            unlocated_invalid = True
            continue
        stamp = _stamp(point.get("timeStamp"))
        if stamp is None:
            invalid = True
            unlocated_invalid = True
            continue
        if stamp in seen:
            if start <= stamp < end:
                output["note"] = "Duplicate timestamps were rejected, not summed."
                return output
            bad_days.add(stamp.date())
            points.pop(stamp, None)
            continue
        seen.add(stamp)
        raw_value = point.get(aggregation.lower())
        value = (_number(raw_value) if isinstance(raw_value, (int, float))
                 and not isinstance(raw_value, bool) else None)
        if (not query_start <= stamp < query_end or stamp.minute or stamp.second or stamp.microsecond):
            invalid |= stamp >= start
            bad_days.add(stamp.date())
            continue
        if value is None:
            bad_days.add(stamp.date())
            if point.get(aggregation.lower()) is not None:
                invalid |= start <= stamp < end
            continue
        if value < 0:
            invalid |= start <= stamp < end
            bad_days.add(stamp.date())
            continue
        points[stamp] = value
    daily = {}
    for stamp, value in points.items():
        daily.setdefault(stamp.date(), []).append(value)
    if not unlocated_invalid:
        for day in daily.keys() | bad_days:
            values = daily.get(day, [])
            full_day_end = datetime.combine(day + timedelta(days=1), time(), UTC)
            amount = (_sum(values) if len(values) == 24 and day not in bad_days
                     and full_day_end <= query_end else None)
            if amount is not None and aggregation == "Average":
                amount /= 24
            elif amount is not None and aggregation == "Maximum":
                amount = max(values)
            output["_daily"][day.isoformat()] = amount
    points = {stamp: value for stamp, value in points.items() if start <= stamp < end}
    if not points:
        return output
    values = list(points.values())
    amount = _sum(values)
    if amount is None:
        output["note"] = "Non-finite metric aggregate was rejected."
        return output
    if aggregation == "Average":
        amount /= len(values)
    elif aggregation == "Maximum":
        amount = max(values)
    method = {
        "Total": "Sum of observed hourly totals; no extrapolation.",
        "Average": "Unweighted mean of observed hourly averages; not request-weighted.",
        "Maximum": "Maximum of observed hourly maxima; not a percentile.",
    }[aggregation]
    complete = len(points) == expected and not invalid
    output.update(value=amount, samples=len(points), status="ok" if complete else "partial",
                  last_sample=_iso(max(points)),
                  note=f"Observed {len(points)}/{expected} hourly samples. {method}"
                       + ("" if complete else " Incomplete coverage; missing hours are not zero."))
    return output


def _read_metric(payload: dict | None, name: str, aggregation: str, window: dict,
                 split: bool, history_window: dict | None = None) -> dict[str, dict]:
    missing = _blank(window, f"{name}: no unambiguous matching series was returned.")
    bad = {"": missing}
    if (payload is None or payload.get("errorCode") not in (None, "", "Success", "0")
            or str(payload.get("unit", "")).lower() != ("percent" if name == UTILIZATION else "count")):
        return bad
    series = payload.get("timeseries")
    if not isinstance(series, list) or not series or len(series) >= MAX_SERIES:
        return bad
    results = {}
    seen_keys = set()
    for entry in series:
        if not isinstance(entry, dict):
            return bad
        dims = entry.get("metadatavalues") or []
        if split:
            if (not isinstance(dims, list) or len(dims) != 1 or not isinstance(dims[0], dict)
                    or not isinstance(dims[0].get("name"), dict)
                    or str(dims[0]["name"].get("value", "")).lower() != DEPLOYMENT_DIMENSION.lower()
                    or not isinstance(dims[0].get("value"), str) or not dims[0]["value"]):
                return bad
            key = dims[0]["value"]
        else:
            if len(series) != 1 or dims:
                return bad
            key = ""
        if key.lower() in seen_keys:
            missing["note"] = "Duplicate dimension series were rejected, not summed."
            return bad
        seen_keys.add(key.lower())
        results[key] = _series(entry.get("data"), aggregation, window, history_window)
        results[key]["metric"] = name
    return results


async def _metrics(rid: str, names: list[str], aggregation: str, window: dict,
                   split: bool = False) -> dict[str, dict[str, dict]]:
    history_window = _metric_history_window(window, _now())
    params = {
        "api-version": "2023-10-01", "metricnamespace": "Microsoft.CognitiveServices/accounts",
        "metricnames": ",".join(names), "aggregation": aggregation, "interval": INTERVAL,
        "timespan": f"{history_window['start']}/{history_window['end']}",
        "AutoAdjustTimegrain": "false", "ValidateDimensions": "true",
    }
    if split:
        params.update({"$filter": f"{DEPLOYMENT_DIMENSION} eq '*'", "top": str(MAX_SERIES)})
    failure = {name: {"": _blank(window, "Metric read denied or failed; no usage inference.")}
               for name in names}
    try:
        payload = await _get(_endpoint(rid, "metrics"), params)
        if payload.get("interval", INTERVAL) != INTERVAL or payload.get("nextLink"):
            return failure
        returned = {}
        for item in payload["value"]:
            if not isinstance(item, dict) or not isinstance(item.get("name"), dict):
                return failure
            key = str(item["name"].get("value", "")).lower()
            if key in returned:
                return failure
            returned[key] = item
        return {name: _read_metric(returned.get(name.lower()), name, aggregation, window, split, history_window)
                for name in names}
    except (CostError, httpx.HTTPError):
        return failure


async def _deployments(rid: str) -> tuple[list[dict], bool, str]:
    root = f"{ARM}{quote(rid, safe='/')}/deployments"
    url = root
    params = {"api-version": "2024-10-01"}
    values = []
    seen_names = set()
    seen_links = set()
    for _ in range(MAX_PAGES):
        payload = await _get(url, params)
        for row in payload["value"]:
            if not isinstance(row, dict):
                return values, False, "Malformed deployment metadata was excluded."
            name = row.get("name")
            if (not isinstance(name, str) or not name or len(name) > 256
                    or re.search(r"[/\\%?#\x00-\x1f\x7f]", name) or name in {".", ".."}
                    or str(row.get("id", "")).lower() != f"{rid}/deployments/{name}".lower()):
                return values, False, "Unscoped or malformed deployment metadata was excluded."
            if name.lower() in seen_names:
                return [], False, "Duplicate deployment metadata prevents an exact model mapping."
            seen_names.add(name.lower())
            properties = row.get("properties") or {}
            if not isinstance(properties, dict):
                return values, False, "Malformed deployment properties were excluded."
            model = properties.get("model") or {}
            if not isinstance(model, dict):
                return values, False, "Malformed deployment model metadata was excluded."
            sku = row.get("sku") or {}
            if not isinstance(sku, dict):
                return values, False, "Malformed deployment SKU metadata was excluded."
            if (any(model.get(key) is not None and not isinstance(model[key], str)
                    for key in ("name", "version", "format"))
                    or (sku.get("name") is not None and not isinstance(sku["name"], str))):
                return values, False, "Malformed deployment model or SKU labels were excluded."
            values.append({
                "deployment": name, "model": model.get("name"), "version": model.get("version"),
                "format": model.get("format"), "sku": sku.get("name"),
            })
            if len(values) == MAX_DEPLOYMENTS:
                return values, False, f"Deployment metadata capped at {MAX_DEPLOYMENTS} rows."
        next_link = payload.get("nextLink")
        if not next_link:
            return values, True, ""
        if not isinstance(next_link, str) or next_link in seen_links:
            return values, False, "Invalid deployment continuation was not followed."
        parsed = urlsplit(next_link)
        base = urlsplit(root)
        if (parsed.scheme != "https" or parsed.netloc.lower() != base.netloc.lower()
                or parsed.path.lower() != base.path.lower() or parsed.fragment):
            return values, False, "Out-of-scope deployment continuation was not followed."
        seen_links.add(next_link)
        url, params = next_link, None
    return values, False, f"Deployment metadata limited to {MAX_PAGES} pages."


def _combine_tokens(metrics: dict, key: str, window: dict) -> dict:
    output = _tokens(window, "Token definitions or matching series are unavailable; meter quantities are not tokens.")
    prompt = metrics.get(INPUT, {}).get(key)
    generated = metrics.get(OUTPUT, {}).get(key)
    total = metrics.get(TOTAL, {}).get(key)
    if INPUT in metrics and OUTPUT in metrics:
        prompt_days = (prompt or {}).get("_daily", {})
        generated_days = (generated or {}).get("_daily", {})
        output["_daily"] = {
            day: _sum([prompt_days.get(day), generated_days.get(day)])
            for day in prompt_days.keys() | generated_days.keys()
        }
        output["_daily_basis"] = f"{INPUT} + {OUTPUT}; both components require 24 valid hourly samples per day"
    elif TOTAL in metrics:
        output["_daily"] = (total or {}).get("_daily", {})
        output["_daily_basis"] = f"{TOTAL}; 24 valid hourly samples per day, consistently selected"
    else:
        output["_daily"] = {}
        output["_daily_basis"] = "No consistently complete token source"
    output["input"] = prompt["value"] if prompt else None
    output["output"] = generated["value"] if generated else None
    for field, part in (("input", prompt), ("output", generated)):
        if part:
            output[f"{field}_status"] = part["status"]
            output[f"{field}_samples"] = part["samples"]
    if output["input"] is not None and output["output"] is not None:
        parts = [prompt, generated]
        output["value"] = _sum([output["input"], output["output"]])
        if output["value"] is None:
            output["note"] = "Non-finite combined token aggregate was rejected."
            return output
        output["metrics"] = [INPUT, OUTPUT]
        output["note"] = "Input + output tokens only; TokenTransaction is not added again."
    elif total and total["value"] is not None:
        parts = [total]
        output["value"] = total["value"]
        output["metrics"] = [TOTAL]
        output["note"] = "TokenTransaction total; input/output breakdown may be unavailable."
    else:
        parts = [p for p in (prompt, generated) if p and p["value"] is not None]
        if parts:
            output["status"] = "partial"
            output["note"] = "Only one token component is available; combined tokens are unknown."
            return output
        # Say why. Every component failing produces the same empty result whether the metric
        # does not exist, the read was refused, or the series came back unusable -- and the
        # default note names only the first of those. Reporting what the components actually
        # said is the difference between "this account has no token metrics" and "we could not
        # read them", which is the distinction this whole module exists to preserve.
        observed = list(dict.fromkeys(
            part["note"] for part in (prompt, generated, total) if part and part.get("note")))
        if observed:
            output["note"] = " ".join(observed)
        return output
    output.update(status="ok" if all(p["status"] == "ok" for p in parts) else "partial",
                  samples=min(p["samples"] for p in parts),
                  expected_samples=max(p["expected_samples"] for p in parts))
    if output["input_status"] != "ok" or output["output_status"] != "ok":
        output["status"] = "partial"
    # Input and output are read over the same grid, so they usually say the same thing about
    # coverage. Printing it once per component turns one caveat into two identical sentences.
    output["note"] += " " + " ".join(dict.fromkeys(p["note"] for p in parts if p["note"]))
    return output


def _combine_utilization(metrics: dict, key: str, window: dict) -> dict:
    output = _utilization(window, "Provisioned utilization has no verified metric observations.")
    avg = metrics.get("Average", {}).get(key)
    maximum = metrics.get("Maximum", {}).get(key)
    if avg:
        output.update(avg)
    if maximum and maximum["value"] is not None:
        output["maximum"] = maximum["value"]
        output["maximum_status"] = maximum["status"]
        output["maximum_samples"] = maximum["samples"]
        output["maximum_note"] = maximum["note"]
        if maximum["status"] != "ok":
            output["status"] = "partial"
        if output["value"] is None:
            output["status"] = "partial"
            output["note"] = "Average utilization unavailable. " + maximum["note"]
    return output


def _paygo(sku: object) -> bool:
    # Absence of "Provisioned" is not proof: unknown/new SKUs remain unknown.
    return isinstance(sku, str) and sku.lower() in {
        "standard", "globalstandard", "datazonestandard", "globalbatch", "datazonebatch",
        "developer",
    }


def _provisioned(sku: object) -> bool:
    return isinstance(sku, str) and sku.lower() in {
        "provisioned", "provisionedmanaged", "globalprovisionedmanaged", "datazoneprovisionedmanaged",
    }


def _recommendation(tokens: dict, utilization: dict) -> str:
    if tokens["status"] == "not_applicable":
        return "Review this service's billed meters against workload needs; OpenAI token and provisioned utilization metrics do not apply."
    if utilization.get("maximum") is not None and utilization["maximum"] >= 90:
        return "Review provisioned throttling and workload peaks; a high observed maximum alone does not justify resizing."
    if tokens["status"] in {"unavailable", "partial"}:
        return "Review resource mapping, Azure Monitor metric access and coverage before making token or capacity decisions."
    if utilization["status"] == "not_applicable":
        return "Review model choice and input/output token demand; pay-as-you-go usage has no provisioned utilization percentage."
    return "Compare measured token demand with workload needs; billing alone does not establish a capacity or savings recommendation."


async def _gather_reads(*reads):
    tasks = [asyncio.create_task(read) for read in reads]
    try:
        return await asyncio.gather(*tasks)
    finally:
        # gather alone leaves siblings running after a programming error; these GETs
        # must not outlive either their account budget or the request that owns them.
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


def _finish_models(item: dict, models: list[dict], window: dict) -> None:
    for model in models:
        if (_paygo(model["sku"]) and model["utilization"]["value"] is None
                and model["utilization"]["maximum"] is None):
            model["utilization"] = _utilization(
                window, "Current deployment SKU is non-provisioned, not a historical SKU assertion.",
                "not_applicable")
        model["recommendation"] = _recommendation(model["tokens"], model["utilization"])
    if (models and item["deployment_status"] == "ok"
            and all(m["utilization"]["status"] == "not_applicable" for m in models)
            and item["utilization"]["value"] is None and item["utilization"]["maximum"] is None):
        item["utilization"] = _utilization(
            window, "All current deployment SKUs are non-provisioned; this is not a historical SKU assertion.",
            "not_applicable")


async def _probe(item: dict, window: dict, semaphore: asyncio.Semaphore,
                 models: list[dict] | None = None) -> list[dict]:
    rid = item["resource_id"]
    models = [] if models is None else models
    loop = asyncio.get_running_loop()
    timings = {}
    interrupted = set()

    async def timed(stage, read):
        started = loop.time()
        try:
            return await read
        except asyncio.CancelledError:
            interrupted.add(stage)
            raise
        finally:
            timings[stage] = loop.time() - started

    def diagnostic(message):
        stages = ", ".join(f"{stage} {seconds:.1f}s" for stage, seconds in timings.items())
        waiting = ", ".join(sorted(interrupted)) or "account queue"
        return f"{message} during {waiting} ({stages or 'not started'}); completed observations retained."

    async def read_tokens():
        try:
            definitions = await timed("definitions", _get(
                _endpoint(rid, "metricDefinitions"), {"api-version": "2018-01-01"}))
            if definitions.get("nextLink"):
                raise CostError("Incomplete metric definitions.")
        except (CostError, httpx.HTTPError):
            definitions = {"value": []}
            item["notes"].append("Metric definitions read denied or failed; no telemetry inference.")
        specs = {name: definition for name in (INPUT, OUTPUT)
                 if (definition := _definition(definitions, name, "Count", "Total"))}
        if len(specs) < 2 and (fallback := _definition(definitions, TOTAL, "Count", "Total")):
            specs[TOTAL] = fallback
        account_tokens = {}
        if specs:
            account_tokens = await timed("account tokens", _metrics(rid, list(specs), "Total", window))
        # Persist before awaiting metadata or deployment splits: an optional slow
        # stage cannot erase already-observed account token counts.
        item["tokens"] = _combine_tokens(account_tokens, "", window)
        return definitions, specs

    async def read_deployments():
        try:
            deployments, complete, note = await timed("deployments", _deployments(rid))
            item["deployment_status"] = "ok" if complete else "partial"
            if note:
                item["notes"].append(note)
            for deployment in deployments:
                models.append({
                    **deployment, "resource_id": rid, "resource_name": item["resource_name"],
                    "subscription_id": item["subscription_id"], "service": item["service"],
                    "cost": None, "cost_note": COST_NOTE,
                    "metadata_source": "Azure ARM deployment metadata (current, not historical)",
                    "tokens": _tokens(window, "No deployment-dimension token observations."),
                    "utilization": _utilization(window, "No deployment-dimension utilization observations."),
                })
        except (CostError, httpx.HTTPError):
            item["deployment_status"] = "unavailable"
            item["notes"].append("Deployment metadata read denied or failed; billing products are not model inventory.")

    try:
        async with semaphore:
            item["metric_read_started"] = True
            async with asyncio.timeout(ACCOUNT_TIMEOUT):
                (definitions, specs), _ = await _gather_reads(read_tokens(), read_deployments())
                split_tokens = {}
                split_names = [name for name, definition in specs.items() if _can_split(definition)]
                if split_names and models:
                    split_tokens = await timed(
                        "deployment tokens", _metrics(rid, split_names, "Total", window, True))
                for model in models:
                    model["tokens"] = _combine_tokens(split_tokens, model["deployment"], window)
                known = {m["deployment"] for m in models}
                unknown = {key for value in split_tokens.values() for key in value if key and key not in known}
                if unknown:
                    item["notes"].append("Some metric deployment names could not be mapped exactly to current deployment metadata.")
                account_util, split_util = {}, {}
                aggregations = [aggregation for aggregation in ("Average", "Maximum")
                                if (definition := _definition(definitions, UTILIZATION, "Percent", aggregation))
                                and _can_split(definition) and models]

                async def read_split_utilization(aggregation):
                    split_util[aggregation] = (await timed(
                        f"deployment PTU {aggregation}",
                        _metrics(rid, [UTILIZATION], aggregation, window, True)))[UTILIZATION]
                    for model in models:
                        model["utilization"] = _combine_utilization(split_util, model["deployment"], window)

                await _gather_reads(*(read_split_utilization(aggregation) for aggregation in aggregations))
                single_provisioned = (
                    len(models) == 1 and not unknown and item["deployment_status"] == "ok"
                    and _provisioned(models[0]["sku"])
                    and all(set(observed) == {models[0]["deployment"]} for observed in split_util.values()))

                async def read_account_utilization(aggregation):
                    account_util[aggregation] = (await timed(
                        f"account PTU {aggregation}",
                        _metrics(rid, [UTILIZATION], aggregation, window)))[UTILIZATION]
                    item["utilization"] = _combine_utilization(account_util, "", window)

                # A resource roll-up across unlike PTU deployments does not establish
                # capacity-weighted account utilization, even when Azure labels it Average.
                if single_provisioned:
                    await _gather_reads(*(
                        read_account_utilization(aggregation) for aggregation, observed in split_util.items()
                        if observed[models[0]["deployment"]]["value"] is not None))
                if not account_util:
                    item["utilization"]["note"] = (
                        "Account utilization requires a verified single provisioned deployment and an "
                        "unsplit Azure Monitor roll-up, corroborated by the deployment dimension; "
                        "percentages across deployments are not combined. "
                        "Review exact deployment observations instead.")
                elif item["utilization"]["value"] is not None:
                    item["utilization"]["note"] += (
                        " Azure Monitor account roll-up with one verified current provisioned deployment; "
                        "not an average calculated from deployment percentages.")
    except TimeoutError:
        item["notes"].append(diagnostic("Account observation time budget exceeded"))
    except asyncio.CancelledError:
        item["notes"].append(diagnostic("Account observation cancelled"))
        raise
    finally:
        _finish_models(item, models, window)
    return models


def _priority(accounts: list[dict]) -> list[dict]:
    currencies = {c["currency"] for item in accounts for c in item["cost_by_currency"]}
    rank = {item["id"]: len(accounts) for item in accounts}
    for code in sorted(currencies):
        candidates = [(item, entry["cost"]) for item in accounts for entry in item["cost_by_currency"]
                      if entry["currency"] == code and entry["cost"] is not None]
        for index, (item, _) in enumerate(sorted(candidates, key=lambda pair: (-pair[1], pair[0]["id"]))):
            rank[item["id"]] = min(rank[item["id"]], index)
    return sorted(accounts, key=lambda item: (rank[item["id"]], item["id"]))


def _attach_anomalies(result: dict, now: datetime, metric_history: dict | None) -> None:
    for row in [*result["services"], *result["models"]]:
        checks = []
        for entry in row.get("cost_by_currency", []):
            history = row.get("_cost_history", {}).get(entry["currency"], {})
            checks.append(ai_anomalies.evaluate(
                "cost", history.get("daily", {}), result["window"], now.date(),
                source=COST_SOURCE, currency=entry["currency"],
                unavailable=row.get("_cost_history_note", ""),
                excluded_negative_days=history.get("negative_days", 0)))
        for metric in ("tokens", "utilization"):
            value = row[metric]
            source = METRIC_SOURCE
            if metric == "tokens" and value.get("_daily_basis"):
                source += ": " + value["_daily_basis"]
            elif metric == "utilization":
                source += ": " + UTILIZATION + " daily mean of hourly averages"
            checks.append(ai_anomalies.evaluate(
                metric, value.get("_daily", {}), result["window"], now.date(), source=source,
                not_applicable=value["status"] == "not_applicable"))
        ai_anomalies.attach(row, checks)
        for metric in ("tokens", "utilization"):
            for key in list(row[metric]):
                if key.startswith("_"):
                    del row[metric][key]
        for key in list(row):
            if key.startswith("_"):
                del row[key]
    result["anomaly_detection"] = ai_anomalies.metadata(
        [*result["services"], *result["models"]], result["window"], metric_history)


async def report(scope: list[str], days: int = 30, currency: str | None = None,
                 filters: dict | None = None) -> dict:
    """Return billed costs once per resource, with independently sourced metric evidence."""
    scope = _scope(scope)
    if isinstance(days, bool) or not isinstance(days, int) or not 1 <= days <= 365:
        raise ValueError("Choose a billing window between 1 and 365 days.")
    days = reporting.period_days(days)
    now = _now()
    deadline = asyncio.get_running_loop().time() + REPORT_TIMEOUT
    # to_thread copies reporting and caller ContextVars; blocking SQL never holds the event loop.
    result = await asyncio.to_thread(_costs, scope, days, currency, filters)
    window, unavailable = _metric_window(result["window"], now)
    result.update(status="partial" if result["notices"] else "ok", generated_at=_iso(now),
                  source=COST_SOURCE, metric_window=window, models=[])
    result["notices"].append(
        "Costs are recorded billing amounts, not token-price estimates. Billing products are labels, "
        "not deployments. Meter quantities retain their recorded units; Units and '1M Tokens' labels "
        "are not scaled into token counts. Missing telemetry is not zero.")
    if filters and any(filters.values()):
        result["notices"].append(
            "Warehouse filters narrow billed charges only. Account/deployment telemetry covers the "
            "whole account in this window and may be broader than filtered charges; no cost-per-token ratio is inferred.")
    accounts = []
    for item in result["services"]:
        item["notes"] = []
        item["deployment_status"] = "not_applicable"
        item["tokens"] = _tokens(window, "Token metrics are not available for this billing resource.", "not_applicable")
        item["utilization"] = _utilization(window, "Provisioned OpenAI utilization is not applicable to this billing resource.",
                                           "not_applicable")
        if _account(item, scope):
            accounts.append(item)
            item["tokens"] = _tokens(window, unavailable or "Account not sampled within the read-only telemetry cap.")
            item["utilization"] = _utilization(window, unavailable or "Account not sampled within the read-only telemetry cap.")
            item["deployment_status"] = "unavailable"
        elif (_resource_kind(item, scope) or "").split("/")[0] in {
            "microsoft.search", "microsoft.machinelearningservices", "microsoft.machinelearning",
        }:
            # "Azure Cognitive Search" is a Search service, not a Cognitive Services account.
            pass
        elif item["resource_id"] and "/microsoft.cognitiveservices/accounts/" in item["resource_id"]:
            item["notes"].append("Malformed or out-of-scope account ID was not queried.")
            item["tokens"] = _tokens(window, item["notes"][-1])
            result["status"] = "partial"
        elif any(term in " ".join([*item["services"], *item["models"]]).lower()
                 for term in ("openai", "foundry models", "cognitive", "azure ai services")) or not item["resource_id"]:
            item["tokens"] = _tokens(window, "No validated Cognitive Services account ID is available for this billing group.")
            result["status"] = "partial"
    selected = _priority(accounts)[:MAX_ACCOUNTS] if not unavailable else []
    if unavailable and accounts:
        result["notices"].append(unavailable)
        result["status"] = "partial"
    if len(accounts) > MAX_ACCOUNTS:
        result["notices"].append(
            f"Live reads are capped at {MAX_ACCOUNTS} accounts, prioritizing billed-cost rank within each "
            "native currency then resource ID; amounts in unlike currencies are not compared.")
        result["status"] = "partial"
    semaphore = asyncio.Semaphore(MAX_CONCURRENT)
    batches = [[] for _ in selected]
    try:
        if selected:
            if asyncio.get_running_loop().time() >= deadline:
                raise TimeoutError
            async with asyncio.timeout_at(deadline):
                await _gather_reads(*(_probe(item, window, semaphore, models)
                                      for item, models in zip(selected, batches)))
    except TimeoutError:
        result["status"] = "partial"
        result["notices"].append(
            f"The {REPORT_TIMEOUT}-second report budget was exhausted; completed billing and telemetry "
            "were retained, and outstanding Azure reads were cancelled.")
    result["models"] = sorted((model for batch in batches for model in batch),
                              key=lambda model: (model["resource_id"], model["deployment"]))
    for item in result["services"]:
        item["recommendation"] = _recommendation(item["tokens"], item["utilization"])
        if item["tokens"]["status"] not in {"ok", "not_applicable"} or item["notes"]:
            result["status"] = "partial"
        if item["utilization"]["status"] not in {"ok", "not_applicable"}:
            result["status"] = "partial"
        if item in selected and item["deployment_status"] != "ok":
            result["status"] = "partial"
        if filters and any(filters.values()) and item in accounts:
            item["notes"].append("Telemetry is account-wide and may exceed the filtered billing subset.")
        if result["truncated"] and item in accounts:
            item["notes"].append("Telemetry is account-wide; displayed charges may omit billing groups because of the cap.")
    if any(m["tokens"]["status"] not in {"ok", "not_applicable"}
           or m["utilization"]["status"] not in {"ok", "not_applicable"} for m in result["models"]):
        result["status"] = "partial"
    result["metric_coverage"] = {
        "eligible_accounts": len(accounts),
        "queried_accounts": sum(bool(item.get("metric_read_started")) for item in selected),
        "accounts_with_tokens": sum(item["tokens"]["value"] is not None for item in accounts),
        "accounts_with_complete_tokens": sum(item["tokens"]["status"] == "ok" for item in accounts),
        "deployment_rows": len(result["models"]), "account_cap": MAX_ACCOUNTS,
        "deployment_cap_per_account": MAX_DEPLOYMENTS, "concurrency": MAX_CONCURRENT,
        "account_timeout_seconds": ACCOUNT_TIMEOUT, "report_timeout_seconds": REPORT_TIMEOUT,
        "retention_days": RETENTION_DAYS,
        "note": "Account token roll-ups are queried once, not summed across billing currencies or deployment rows. "
                "Deployment observations overlap account totals; do not add them together. "
                "Inventory is current; historical model versions and deleted deployments are not inferred.",
    }
    _attach_anomalies(result, now, _metric_history_window(window, now) if window and not unavailable else None)
    return result
