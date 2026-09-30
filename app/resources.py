"""Resource-level cost and billing context from the warehouse, without live Azure reads.

This is not an ARM inventory: resources without cost records are absent. First/last seen
describe the reporting and comparison windows, not provisioning or deletion dates.
"""
from __future__ import annotations

import json
import logging
import math
from typing import Any

log = logging.getLogger("cloudlens.resources")

# Matches tags.py's own ceiling for the same reason: large enough to cover a real estate, small
# enough to stay a JSON response and a browser tab someone can still scroll and search. Past
# this the response says so rather than silently dropping the tail.
MAX_RESOURCES = 8000


class Resources:
    """Reads the warehouse. Holds no state."""

    def __init__(self) -> None:
        from .warehouse import warehouse

        self.wh = warehouse

    def inventory(self, scope: list[str] | None, days: int = 30,
                  currency: str | None = None,
                  filters: dict[str, list[str]] | None = None) -> dict[str, Any]:
        """Compare adjacent windows in one aggregation, keeping currency groups separate."""
        from . import reporting, resource_tags
        from .dashboard import _group_for_family
        from .tags import _pairs

        days = reporting.period_days(days)
        with reporting.comparison_history(), \
                self.wh.reader(scope, currency, filters, usage=("tags",)) as r:
            hi, start, prev = reporting.window(r, days)
            if not hi:
                return {"empty": True, "days": days, "resources": []}

            # The per-resource summary and the rollup when they can answer this connection
            # exactly, `costs` when they cannot -- the SQL is the same either way. Reading
            # `costs` directly here timed out on a 16.9M-row estate.
            tag_source = getattr(r, "tags", None)
            source = getattr(r, "named", "costs") if tag_source else "costs"
            agg = getattr(r, "agg", "costs")
            combined = f'"ChargePeriodStart" > {prev} AND "ChargePeriodStart" <= DATE \'{hi}\''
            period = f'"ChargePeriodStart" > {start}'
            previous = f'"ChargePeriodStart" > {prev} AND "ChargePeriodStart" <= {start}'
            resource_identity = (
                'LOWER(TRIM("ResourceId")) AS id, LOWER(COALESCE("SubAccountId", \'\')) '
                'AS subscription_id, COALESCE(NULLIF(NULLIF(UPPER(TRIM("BillingCurrency")), '
                '\'\'), \'UNKNOWN\'), \'unknown\') AS currency')
            # Ranked per service rather than per row: every ranking key but the date is a
            # per-service figure, so the first-ranked service is the same, and the window
            # function sorts services instead of every charge.
            inline_tags = "" if tag_source else ', MAX(COALESCE("Tags", \'\')) AS tags'
            top = (
                'WITH names AS (SELECT LOWER(COALESCE("SubAccountId", \'\')) AS name_sub, '
                'MAX(COALESCE("SubAccountName", "SubAccountId", \'\')) AS name_text '
                f'FROM {agg} WHERE {combined} GROUP BY 1), '
                f'svc AS (SELECT {resource_identity}, "ServiceName", "ServiceFamily", '
                f'SUM(CASE WHEN {period} THEN "BilledCost" ELSE 0 END) AS service_cost, '
                f'SUM(CASE WHEN {previous} THEN "BilledCost" ELSE 0 END) AS service_prior_cost, '
                f'MAX(CASE WHEN {period} THEN 1 ELSE 0 END) AS service_current, '
                'MAX(COALESCE(NULLIF("ResourceName", \'\'), \'(unnamed)\')) AS name, '
                'MAX(COALESCE("ResourceGroup", \'\')) AS resource_group, '
                'MAX(COALESCE("RegionName", \'\')) AS region, '
                'MIN("ChargePeriodStart") AS first_seen, MAX("ChargePeriodStart") AS last_seen'
                f'{inline_tags} '
                f'FROM {source} WHERE {combined} '
                'AND "ResourceId" IS NOT NULL AND TRIM("ResourceId") <> \'\' '
                'GROUP BY 1, 2, 3, "ServiceName", "ServiceFamily"), '
                'ranked AS (SELECT *, row_number() OVER (PARTITION BY id, subscription_id, currency '
                'ORDER BY service_current DESC, abs(service_cost) DESC, '
                'abs(service_prior_cost) DESC, "ServiceName", "ServiceFamily") AS service_rank '
                'FROM svc), '
                'top AS (SELECT id, MAX(name) AS name, MAX(resource_group) AS resource_group, '
                'COALESCE(MAX(name_text), \'\') AS subscription, subscription_id, '
                'MAX(CASE WHEN service_rank = 1 THEN COALESCE("ServiceName", \'\') END) AS service, '
                'MAX(CASE WHEN service_rank = 1 THEN COALESCE("ServiceFamily", \'\') END) AS family, '
                'MAX(region) AS region, currency, '
                'SUM(service_cost) AS cost, SUM(service_prior_cost) AS prior_cost, '
                + ('MAX(tags) AS tags, ' if inline_tags else '')
                + 'MIN(first_seen) AS first_seen, MAX(last_seen) AS last_seen, '
                'COALESCE(json_agg(DISTINCT NULLIF("ServiceName", \'\')) '
                'FILTER (WHERE service_current = 1), json_agg(DISTINCT NULLIF("ServiceName", \'\'))) '
                'AS service_names '
                'FROM ranked LEFT JOIN names ON name_sub = subscription_id '
                'GROUP BY id, subscription_id, currency '
                'ORDER BY cost DESC, prior_cost DESC, id, subscription_id, currency '
                f'LIMIT {MAX_RESOURCES + 1})')
            # Tags for the returned resources only, from the monthly tag summary; the
            # detail-table fallback carries them inline instead.
            finish = (
                ' SELECT top.*, COALESCE(tg.tags, \'\') AS tags FROM top '
                + resource_tags.lateral_sql(
                    tag_source, "top.id", "top.currency", prev, str(hi)[:10],
                    "COALESCE(NULLIF(NULLIF(UPPER(TRIM({c})), ''), 'UNKNOWN'), 'unknown')")
                if tag_source else ' SELECT * FROM top')
            rows = r.rows(
                f'{top}{finish} ORDER BY cost DESC, prior_cost DESC, id, subscription_id, currency',
                limit=MAX_RESOURCES + 1,
            )

        truncated = len(rows) > MAX_RESOURCES
        rows = rows[:MAX_RESOURCES]

        items: list[dict[str, Any]] = []
        currencies: set[str] = set()
        for row in rows:
            cost = round(float(row.get("cost") or 0), 2)
            prior = round(float(row.get("prior_cost") or 0), 2)
            area_id, area_label = _group_for_family(row.get("family") or "")
            pairs = _pairs(row.get("tags"))
            cur = row["currency"]
            currencies.add(cur)
            service_names = row.get("service_names")
            if isinstance(service_names, str):
                service_names = json.loads(service_names)
            items.append({
                "id": row["id"],
                "name": row["name"],
                "resource_group": row["resource_group"] or None,
                "subscription": row["subscription"] or None,
                "subscription_id": row["subscription_id"] or None,
                "service": row["service"] or None,
                "service_names": sorted({name for name in service_names or [row["service"]]
                                         if name}),
                "area": area_label,
                "area_id": area_id,
                "region": row["region"] or None,
                "currency": cur,
                "cost": cost,
                "prior_cost": prior,
                "delta": round(cost - prior, 2),
                # No prior spend does not establish when the resource was created.
                "change_pct": (round((cost - prior) / prior * 100, 1) if prior else None),
                "tags": [{"key": k, "value": v} for k, v in pairs],
                "tag_count": len(pairs),
                "first_seen": str(row["first_seen"]) if row.get("first_seen") else None,
                "last_seen": str(row["last_seen"]) if row.get("last_seen") else None,
            })

        items.sort(key=lambda x: -x["cost"])
        totals = {
            code: (round(math.fsum(float(r.get("cost") or 0) for r in rows if r["currency"] == code), 2)
                   if code != "unknown" else None)
            for code in sorted(currencies)
        }
        single_currency = next(iter(currencies)) if len(currencies) == 1 else None

        return {
            "days": days,
            "as_of": str(hi),
            "currency": single_currency if single_currency != "unknown" else None,
            "mixed_currency": len(currencies) > 1,
            "resources": items,
            "count": len(items),
            "unique_count": len({(i["subscription_id"], i["id"]) for i in items}),
            "total_cost": totals.get(single_currency) if items else 0.0,
            "total_cost_by_currency": totals,
            "totals_scope": "returned_resources",
            "service_metadata_basis": "largest absolute current-period service cost; prior-period fallback",
            "resource_groups": len({
                (i["subscription_id"], i["resource_group"].lower())
                for i in items if i["resource_group"]
            }),
            "regions": len({i["region"] for i in items if i["region"]}),
            "services": len({i["service"] for i in items if i["service"]}),
            "subscriptions": len(
                {i["subscription_id"] for i in items if i["subscription_id"]}
            ),
            "truncated": truncated,
        }


resources: Resources | None = None


def get_resources() -> Resources:
    global resources
    if resources is None:
        resources = Resources()
    return resources
