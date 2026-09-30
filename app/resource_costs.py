"""Per-resource money keyed by a compact hash, so Resources can rank a whole estate.

Why
---
Resources ranks every resource in a 60-day comparison window. On the 16.9M-row estate
that window holds 5.4M `resource_presence` rows collapsing to 2.0M distinct
resources -- short-lived clusters make most resources a few days long, so no coarser
grain exists. Grouping those rows by `lower(trim("ResourceId"))` failed the 180s
statement bound on the live server (89s even on a local copy with the data cached):
the Azure resource id is most of each row's width, and hashing and comparing a
~150-byte string 5.4M times is the cost.

Measured on the live server with this table's exact shape: the same full ranking,
with every display column, in 13s. The id text lives once per resource and month in
`resource_ids`, looked up only for the rows a response returns.

What it is
----------
`resource_presence` without rows that have no resource id (Resources excludes them
anyway), with the resource id replaced by `ResourceHash` -- the MD5 of the same
normalised id Resources groups by, as a 16-byte uuid. At two million resources the
chance of any two ids colliding is below 1e-25.

Built from `resource_presence` rather than from `costs`: the per-resource summary
already has the grain this needs, so the rebuild reads it instead of the detail table.
"""
from __future__ import annotations

import datetime as dt

from . import derived

TABLE = "resource_costs"
SOURCE_TABLE = "resource_presence"
PERIOD = "ChargePeriodStart"
HASH = "ResourceHash"
# The id Resources has always grouped by, and the one `resource_ids` resolves back.
KEY_SQL = 'lower(trim("ResourceId"))'
HASH_SQL = f"CAST(md5({KEY_SQL}) AS uuid)"

DIMENSIONS = [
    PERIOD,
    "SubAccountId",
    HASH,
    "ResourceName",
    "BillingCurrency",
    "RegionName",
    "ServiceFamily",
    "ServiceName",
    "ResourceGroup",
]
MEASURES = ["BilledCost"]
FILTERABLE = derived.filterable(DIMENSIONS)
# Read only when the stored table can answer; the caller keeps its own detail-table
# query otherwise, rather than having this grain recomputed from `costs` per request.
STORED_ONLY = True
PREDICATE = '"ResourceId" IS NOT NULL AND trim("ResourceId") <> \'\''


def serves_filters(filters: dict[str, list[str]] | None) -> bool:
    return derived.serves_filters(filters, FILTERABLE)


def _validate() -> None:
    from .warehouse import COLUMNS, NUMERIC

    unknown = [name for name in DIMENSIONS + MEASURES + ["ResourceId"]
               if name != HASH and name not in COLUMNS]
    if unknown:
        raise RuntimeError(f"resource costs names unknown columns: {', '.join(unknown)}")
    if any(name not in NUMERIC for name in MEASURES):
        raise RuntimeError("resource cost measures must be numeric")


def table_sql(schema: str, table: str = TABLE) -> str:
    _validate()
    types = {PERIOD: "date", HASH: "uuid"}
    columns = ([f'"{name}" {types.get(name, "text")}' for name in DIMENSIONS]
               + [f'"{name}" double precision' for name in MEASURES])
    return f'CREATE TABLE IF NOT EXISTS "{schema}".{table} ({", ".join(columns)})'


def index_sql(schema: str, table: str = TABLE) -> list[str]:
    return [
        f'CREATE INDEX IF NOT EXISTS {table}_period_idx ON "{schema}".{table} ("{PERIOD}")',
        f'CREATE INDEX IF NOT EXISTS {table}_sub_idx '
        f'ON "{schema}".{table} (lower("SubAccountId"), "{PERIOD}")',
    ]


def select_sql(source: str, window: tuple[dt.date, dt.date] | None = None) -> str:
    dims = [f'{HASH_SQL} AS "{HASH}"' if name == HASH else f'"{name}"' for name in DIMENSIONS]
    groups = [HASH_SQL if name == HASH else f'"{name}"' for name in DIMENSIONS]
    measures = ", ".join(
        f'CAST(sum("{name}") AS double precision) AS "{name}"' for name in MEASURES)
    predicates = [PREDICATE]
    if window is not None:
        start, stop = window
        predicates.extend((f'"{PERIOD}" >= DATE \'{start.isoformat()}\'',
                           f'"{PERIOD}" < DATE \'{stop.isoformat()}\''))
    return (f"SELECT {', '.join(dims)}, {measures} FROM {source} "
            f"WHERE {' AND '.join(predicates)} GROUP BY {', '.join(groups)}")
