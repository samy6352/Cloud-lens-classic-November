"""Hour-billed usage by resource, meter and day, so the Uptime tab need not read `costs`.

Why
---
The Uptime tab asked four questions of the detail table on every load. On the 16.9M-row
estate that took longer than the 120s origin budget, so the tab returned HTTP 500 to
every viewer, warm or cold -- a failed response is never cached, so nothing ever
improved it.

What it is
----------
Only rows whose unit is a pure duration (`uptime.HOUR_UNIT`), grouped to the columns
the tab reads, with `BilledCost` and `PricingQuantity` summed. Measured on one real
month:

    detail rows                  5,813,121
    hour-billed rows               986,145
    this grain                     632,719   (0.11 of the month)

Summing is exact for everything the tab computes. Its hours are
`PricingQuantity * multiplier(UnitOfMeasure)`, and the multiplier is constant within a
group because the unit is part of the grain, so the product of the sums is the sum of
the products. The same query runs against this table or against `costs` and returns
the same rows.

`Tags` is deliberately absent. It averages ~1KB per row and differs between rows of
the same resource on the same day (per-job tags on Databricks clusters), so carrying it
would have made this table nearly as large as the detail table. Tags come from
`resource_tags` instead.

`ChargeCategory` and `PricingModel` are carried so the whole filter bar can be served
here: without them a `charge` or `pricing` filter would send the tab back to `costs`.
"""
from __future__ import annotations

import datetime as dt

from . import derived, environment

TABLE = "hourly_usage"

# Defined here and imported by `uptime`, which filters `costs` with the same pattern when
# this table cannot serve a connection; one definition, so the two cannot disagree.
HOUR_UNIT = r'^(?:[0-9]+(?:\.[0-9]+)?\s*/?\s*)?hours?$'

DIMENSIONS = [
    "ChargePeriodStart",
    "SubAccountId",
    "SubAccountName",
    "ResourceId",
    "ResourceName",
    "ResourceGroup",
    "RegionName",
    "ServiceName",
    "MeterName",
    "UnitOfMeasure",
    "BillingCurrency",
    "ChargeCategory",
    "PricingModel",
]
if environment.ENABLED:
    DIMENSIONS.append(environment.COLUMN)

PERIOD = DIMENSIONS[0]
MEASURES = ["BilledCost", "PricingQuantity"]
FILTERABLE = derived.filterable(DIMENSIONS)
PREDICATE = f"lower(trim(COALESCE(\"UnitOfMeasure\", ''))) ~ '{HOUR_UNIT}'"


def serves_filters(filters: dict[str, list[str]] | None) -> bool:
    return derived.serves_filters(filters, FILTERABLE)


def _validate() -> None:
    from .warehouse import COLUMNS, NUMERIC

    unknown = [name for name in environment.source_columns(DIMENSIONS + MEASURES)
               if name not in COLUMNS]
    if unknown:
        raise RuntimeError(f"hourly usage names unknown columns: {', '.join(unknown)}")
    if any(name not in NUMERIC for name in MEASURES):
        raise RuntimeError("hourly usage measures must be numeric")


def table_sql(schema: str, table: str = TABLE) -> str:
    _validate()
    columns = (
        [f'"{name}" {"date" if name == PERIOD else "text"}'
         for name in DIMENSIONS if name != environment.COLUMN]
        + [f'"{name}" double precision' for name in MEASURES]
        + ([f'"{environment.COLUMN}" text'] if environment.ENABLED else [])
    )
    return f'CREATE TABLE IF NOT EXISTS "{schema}".{table} ({", ".join(columns)})'


def index_sql(schema: str, table: str = TABLE) -> list[str]:
    return [
        f'CREATE INDEX IF NOT EXISTS {table}_period_idx '
        f'ON "{schema}".{table} ("{PERIOD}")',
        f'CREATE INDEX IF NOT EXISTS {table}_sub_idx '
        f'ON "{schema}".{table} (lower("SubAccountId"), "{PERIOD}")',
    ]


def select_sql(source: str, window: tuple[dt.date, dt.date] | None = None) -> str:
    if environment.ENABLED:
        source = environment.project(source, DIMENSIONS + MEASURES)
    dimensions = ", ".join(f'"{name}"' for name in DIMENSIONS if name != environment.COLUMN)
    groups = ", ".join(f'"{name}"' for name in DIMENSIONS)
    measures = ", ".join(
        f'CAST(sum("{name}") AS double precision) AS "{name}"' for name in MEASURES)
    predicates = [PREDICATE]
    if window is not None:
        start, stop = window
        predicates.extend((
            f'"{PERIOD}" >= DATE \'{start.isoformat()}\'',
            f'"{PERIOD}" < DATE \'{stop.isoformat()}\'',
        ))
    env = f', "{environment.COLUMN}"' if environment.ENABLED else ""
    return (f"SELECT {dimensions}, {measures}{env} FROM {source} "
            f"WHERE {' AND '.join(predicates)} GROUP BY {groups}")
