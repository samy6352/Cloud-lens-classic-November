"""Resource id for each `resource_costs` hash, once per subscription and month.

The companion to `resource_costs`: the ranking runs on 16-byte hashes and this turns
the few thousand it returns back into the ids a person reads, through an index,
rather than carrying ~150 bytes of id through every row being ranked.

Month-grain so it fits the ordinary windowed rebuild -- a corrected day repairs its
month -- and keyed by subscription as well, so a scoped reader sees only its own rows,
the same guarantee every other derived table gives.
"""
from __future__ import annotations

import datetime as dt
from collections.abc import Sequence

from . import resource_costs, schema_v2

TABLE = "resource_ids"
SOURCE_TABLE = "resource_presence"
PERIOD = "ChargePeriodStart"
DIMENSIONS = [PERIOD, "SubAccountId", resource_costs.HASH, "ResourceKey"]
MEASURES: list[str] = []
STORED_ONLY = True


def serves_filters(filters: dict[str, list[str]] | None) -> bool:
    """Filters narrow `resource_costs`; this is only ever joined to what that returns."""
    return True


def build_windows(windows: Sequence[tuple[dt.date, dt.date]] | None
                  ) -> list[tuple[dt.date, dt.date]] | None:
    if windows is None:
        return None
    from .jobs import merge_windows

    return merge_windows([
        (schema_v2.month_start(lo), schema_v2.next_month(hi - dt.timedelta(days=1)))
        for lo, hi in merge_windows(windows)
    ])


def table_sql(schema: str, table: str = TABLE) -> str:
    return (f'CREATE TABLE IF NOT EXISTS "{schema}".{table} ('
            f'"{PERIOD}" date, "SubAccountId" text, '
            f'"{resource_costs.HASH}" uuid, "ResourceKey" text)')


def index_sql(schema: str, table: str = TABLE) -> list[str]:
    return [
        f'CREATE INDEX IF NOT EXISTS {table}_hash_idx '
        f'ON "{schema}".{table} ("{resource_costs.HASH}")',
        f'CREATE INDEX IF NOT EXISTS {table}_sub_idx '
        f'ON "{schema}".{table} (lower("SubAccountId"), "{PERIOD}")',
    ]


def select_sql(source: str, window: tuple[dt.date, dt.date] | None = None) -> str:
    predicates = [resource_costs.PREDICATE]
    if window is not None:
        start, stop = window
        predicates.extend((f'"{PERIOD}" >= DATE \'{start.isoformat()}\'',
                           f'"{PERIOD}" < DATE \'{stop.isoformat()}\''))
    return (
        f"SELECT DISTINCT CAST(date_trunc('month', \"{PERIOD}\") AS date) AS \"{PERIOD}\", "
        f'"SubAccountId", {resource_costs.HASH_SQL} AS "{resource_costs.HASH}", '
        f'CAST({resource_costs.KEY_SQL} AS text) AS "ResourceKey" '
        f"FROM {source} WHERE {' AND '.join(predicates)}")
