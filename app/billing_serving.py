"""Exact monthly Billing serving data, derived only from stored Billing measures.

Resource identity includes subscription, so the union of each subscription's
top K resources contains the scoped top K. Totals and counts are stored separately:
the candidate list never stands in for complete financial or resource totals.
"""
from __future__ import annotations

import datetime as dt
from collections.abc import Sequence

from . import billing, derived, schema_v2

TABLE = "billing_serving"
SOURCE_TABLE = "billing_rollup"
PERIOD = "ChargePeriodStart"
CANDIDATE_LIMIT = 200
FORMAT_VERSION = 1
DIMENSIONS = [
    PERIOD, "bucket", "currency", "BillingCostBasis", "SubAccountId", "id",
    "name", "resource_group", "group_count", "candidate_limit", "format_version",
]
MEASURES = list(billing._empty())
FILTERABLE: frozenset[str] = frozenset()


def serves_filters(filters: dict[str, list[str]] | None) -> bool:
    return derived.serves_filters(filters, FILTERABLE)


def serves_window(window: dict) -> bool:
    start, end = dt.date.fromisoformat(window["start"]), dt.date.fromisoformat(window["end"])
    return (not window.get("partial") and start.day == 1
            and end + dt.timedelta(days=1) == schema_v2.next_month(start))


def build_windows(windows: Sequence[tuple[dt.date, dt.date]] | None
                  ) -> list[tuple[dt.date, dt.date]] | None:
    """Monthly rankings must be repaired over the whole affected month, not a day."""
    if windows is None:
        return None
    from .jobs import merge_windows

    validated = merge_windows(windows)
    return merge_windows([
        (schema_v2.month_start(lo), schema_v2.next_month(hi - dt.timedelta(days=1)))
        for lo, hi in validated
    ])


def table_sql(schema: str, table: str = TABLE) -> str:
    types = {PERIOD: "date", "group_count": "bigint",
             "candidate_limit": "integer", "format_version": "integer"}
    columns = [f'"{name}" {types.get(name, "text")}' for name in DIMENSIONS]
    columns += [f'"{name}" double precision' for name in MEASURES]
    return f'CREATE TABLE IF NOT EXISTS "{schema}".{table} ({", ".join(columns)})'


def index_sql(schema: str, table: str = TABLE) -> list[str]:
    return [
        f'CREATE INDEX IF NOT EXISTS {table}_period_idx ON "{schema}".{table} ("{PERIOD}")',
        f'CREATE INDEX IF NOT EXISTS {table}_sub_idx '
        f'ON "{schema}".{table} (lower("SubAccountId"), "{PERIOD}")',
    ]


def select_sql(source: str, window: tuple[dt.date, dt.date] | None = None) -> str:
    """Build from the caller's qualified staged/current Billing rollup relation."""
    where = ""
    if window is not None:
        lo, hi = build_windows([window])[0]
        where = f'WHERE "{PERIOD}" >= DATE \'{lo}\' AND "{PERIOD}" < DATE \'{hi}\''
    measures = billing._sql_remeasures()
    zeros = ", ".join("0::double precision" for _ in MEASURES)
    dimensions = ", ".join(f'"{name}"' for name in DIMENSIONS[:9])
    projected = ", ".join(f'CAST("{name}" AS double precision) AS "{name}"' for name in MEASURES)
    return f"""
      WITH billing_input AS NOT MATERIALIZED (
        SELECT *, date_trunc('month', "{PERIOD}")::date AS month FROM {source} {where}
      ), resource_keys AS MATERIALIZED (
        SELECT month, currency, "SubAccountId", resource_id,
          coalesce(sum(recorded_cost_total), 0) AS recorded_cost_total
        FROM billing_input GROUP BY month, currency, "SubAccountId", resource_id
      ), resource_selected AS MATERIALIZED (
        SELECT * FROM (
          SELECT *, row_number() OVER (
            PARTITION BY month, currency, "SubAccountId"
            ORDER BY abs(recorded_cost_total) DESC, resource_id, "SubAccountId") AS rank
          FROM resource_keys
        ) ranked WHERE rank <= {CANDIDATE_LIMIT}
      ), daily AS MATERIALIZED (
        SELECT "{PERIOD}", 'daily'::text AS bucket, currency, "BillingCostBasis",
          "SubAccountId", NULL::text AS id, NULL::text AS name,
          NULL::text AS resource_group, 0::bigint AS group_count, {measures}
        FROM billing_input GROUP BY "{PERIOD}", currency, "BillingCostBasis", "SubAccountId"
      ), subscriptions AS MATERIALIZED (
        SELECT month, 'subscriptions', currency, NULL::text, "SubAccountId",
          "SubAccountId" AS id, min("SubAccountName") AS name, NULL::text, 1::bigint, {measures}
        FROM billing_input GROUP BY month, currency, "SubAccountId"
      ), services AS MATERIALIZED (
        SELECT month, 'services', currency, NULL::text, "SubAccountId",
          coalesce("ServiceName", '') AS id, "ServiceName" AS name, NULL::text, 1::bigint, {measures}
        FROM billing_input GROUP BY month, currency, "SubAccountId", "ServiceName"
      ), resources AS (
        SELECT r.month, 'resources', r.currency, NULL::text, r."SubAccountId",
          r.resource_id, min(r."ResourceName"), min(r."ResourceGroup"), 1::bigint,
          {billing._sql_remeasures('r.')}
        FROM billing_input r JOIN resource_selected k
          USING (month, currency, "SubAccountId", resource_id)
        GROUP BY r.month, r.currency, r."SubAccountId", r.resource_id
      ), counts AS (
        SELECT month, 'resource_counts', currency, NULL::text, "SubAccountId",
          NULL::text, NULL::text, NULL::text, count(*)::bigint, {zeros}
        FROM resource_keys GROUP BY month, currency, "SubAccountId"
      ), combined AS (
        SELECT * FROM daily UNION ALL SELECT * FROM subscriptions
        UNION ALL SELECT * FROM services UNION ALL SELECT * FROM resources
        UNION ALL SELECT * FROM counts
      )
      SELECT {dimensions}, {CANDIDATE_LIMIT}::integer AS candidate_limit,
        {FORMAT_VERSION}::integer AS format_version, {projected}
      FROM combined
    """


def build_select_sql(source: str, window: tuple[dt.date, dt.date] | None = None) -> str:
    return select_sql(source, window)


def statement_sql(window: dict, source: str, limit: int) -> str:
    """One snapshot supplies native aggregates and a persisted-capacity/version guard."""
    measures = billing._sql_remeasures()
    columns = ", ".join(f'"{name}"' for name in MEASURES)
    zeros = ", ".join(f'0::double precision AS "{name}"' for name in MEASURES)
    current = f""""{PERIOD}" BETWEEN DATE '{window['start']}' AND DATE '{window['end']}'"""
    return f"""
      WITH serving AS MATERIALIZED (
        SELECT * FROM {source} WHERE "{PERIOD}"
          BETWEEN DATE '{window['previous_start']}' AND DATE '{window['end']}'
      ), compatible AS (
        SELECT coalesce(bool_and(coalesce(candidate_limit >= {limit}
          AND format_version = {FORMAT_VERSION}, false)), true) AS supported FROM serving
      ), daily AS (
        SELECT "{PERIOD}" AS day, currency, "BillingCostBasis" AS basis, {measures}
        FROM serving WHERE bucket = 'daily' GROUP BY 1, 2, 3
      ), counts AS (
        SELECT currency, sum(group_count)::bigint AS total_groups FROM serving
        WHERE bucket = 'resource_counts' AND {current} GROUP BY currency
      ), grouped AS (
        SELECT currency, bucket, id, min(name) AS name, "SubAccountId" AS subscription_id,
          NULL::text AS resource_group, 0::bigint AS resource_total_groups, {measures}
        FROM serving WHERE bucket = 'subscriptions' AND {current}
        GROUP BY currency, bucket, id, "SubAccountId"
        UNION ALL
        SELECT currency, bucket, id, name, NULL::text, NULL::text, 0::bigint, {measures}
        FROM serving WHERE bucket = 'services' AND {current} GROUP BY currency, bucket, id, name
        UNION ALL
        SELECT s.currency, s.bucket, s.id, s.name, s."SubAccountId", s.resource_group,
          c.total_groups, {", ".join(f's."{name}"' for name in MEASURES)}
        FROM serving s JOIN counts c ON c.currency = s.currency
        WHERE s.bucket = 'resources' AND s.{current}
      ), ranked AS (
        SELECT *, row_number() OVER (
          PARTITION BY currency, bucket ORDER BY abs(recorded_cost_total) DESC, id, subscription_id) AS rank,
          CASE WHEN bucket = 'resources' THEN resource_total_groups
               ELSE count(*) OVER (PARTITION BY currency, bucket) END AS total_groups
        FROM grouped
      )
      SELECT '__serving__'::text AS bucket, NULL::date AS day, NULL::text AS currency,
        NULL::text AS basis, NULL::text AS id, NULL::text AS name,
        NULL::text AS subscription_id, NULL::text AS resource_group,
        CASE WHEN supported THEN 1 ELSE 0 END::bigint AS total_groups, {zeros}
      FROM compatible
      UNION ALL
      SELECT 'daily', day, currency, basis, NULL::text, NULL::text, NULL::text,
        NULL::text, 0::bigint, {columns} FROM daily WHERE (SELECT supported FROM compatible)
      UNION ALL
      SELECT bucket, NULL::date, currency, NULL::text, id, name, subscription_id,
        resource_group, total_groups, {columns}
      FROM ranked WHERE rank <= {limit} AND (SELECT supported FROM compatible)
    """
