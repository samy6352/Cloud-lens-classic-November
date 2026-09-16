"""Scoped read helpers for live change and performance evidence."""
from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta
from typing import Any

from .azure_updates import SourceError
from .service_health import _groups, _query, normalize_scopes

RESOURCE_ID = re.compile(
    r"^/subscriptions/([0-9a-f-]{36})/"
    r"(?:resourcegroups/[^/]+(?:/providers/[^/]+/.+)?|providers/[^/]+/.+)$", re.I)


def evidence_window(days: int) -> dict[str, Any]:
    if isinstance(days, bool) or not isinstance(days, int) or not 1 <= days <= 14:
        raise SourceError("Choose an evidence window between 1 and 14 days.")
    end = datetime.now(UTC).replace(microsecond=0)
    return {"start": (end - timedelta(days=days)).isoformat().replace("+00:00", "Z"),
            "end": end.isoformat().replace("+00:00", "Z"), "days": days}


async def scoped_graph(query: str, scope: list[str]) -> dict[str, Any]:
    scopes = normalize_scopes(scope)
    # Keep the same token partitioning, paging limits and returned-scope checks as Service Health.
    return await _query(query, await _groups(scopes))


def resource_costs(scope: list[str], resource_ids: list[str],
                   start: str, end: str) -> dict[str, dict[str, Any]]:
    scopes = normalize_scopes(scope)
    if any(not isinstance(rid, str) or len(rid) > 2048 for rid in resource_ids):
        raise SourceError("Cost evidence needs valid Azure resource IDs.")
    wanted = sorted({rid.lower() for rid in resource_ids})
    if len(wanted) > 2500:
        raise SourceError("Too many resources for one cost-evidence query.")
    for rid in wanted:
        match = RESOURCE_ID.fullmatch(rid)
        if not match or match[1].lower() not in scopes:
            raise SourceError("Cost evidence includes a resource outside the selected subscriptions.")
    if not wanted:
        return {}
    try:
        first = datetime.fromisoformat(start.replace("Z", "+00:00")).date()
        last = datetime.fromisoformat(end.replace("Z", "+00:00")).date()
    except ValueError as exc:
        raise SourceError("Cost evidence needs valid start and end dates.") from exc
    if last < first:
        raise SourceError("Cost evidence end precedes its start.")

    import psycopg

    from . import reporting
    from .warehouse import warehouse

    try:
        # Live evidence has its own explicit window, not the global historical cost-month selector.
        with reporting.select(None), warehouse.connect(read_only=True, scope=scopes) as con:
            rows = con.execute(
                'SELECT lower("ResourceId"), COALESCE(NULLIF("BillingCurrency", \'\'), \'unknown\'), '
                'SUM("BilledCost"), COUNT(*), MIN("ChargePeriodStart"), MAX("ChargePeriodStart") '
                'FROM costs WHERE "ChargePeriodStart" >= ? AND "ChargePeriodStart" <= ? '
                'AND lower("ResourceId") = ANY(?) GROUP BY 1, 2 ORDER BY 1, 2',
                [first, last, wanted],
            ).fetchall()
    except psycopg.Error as exc:
        raise SourceError("The warehouse could not provide cost evidence; this is not zero spend.") from exc

    result: dict[str, dict[str, Any]] = {}
    for rid, currency, amount, count, low, high in rows:
        item = result.setdefault(rid, {"by_currency": [], "rows": 0,
                                       "from": str(low), "to": str(high)})
        item["by_currency"].append({"currency": currency,
                                     "cost": float(amount) if amount is not None else None})
        item["rows"] += count
        item["from"] = min(item["from"], str(low))
        item["to"] = max(item["to"], str(high))
    return result
