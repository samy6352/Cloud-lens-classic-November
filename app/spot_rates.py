"""Compact exact meter rates for Spot/on-demand comparisons."""
from __future__ import annotations

import datetime as dt

from . import derived

TABLE = "spot_rates"
DIMENSIONS = [
    "ChargePeriodStart",
    "SubAccountId",
    "MeterName",
    "PricingModel",
    "BillingCurrency",
    "RegionName",
    "UnitOfMeasure",
]
PERIOD = DIMENSIONS[0]
MEASURES = ["BilledCost", "PricingQuantity"]
FILTERABLE = derived.filterable(DIMENSIONS)


def serves_filters(filters: dict[str, list[str]] | None) -> bool:
    return derived.serves_filters(filters, FILTERABLE)


def _validate() -> None:
    from .warehouse import COLUMNS, NUMERIC

    unknown = [name for name in DIMENSIONS + MEASURES if name not in COLUMNS]
    if unknown:
        raise RuntimeError(f"Spot rate rollup names unknown columns: {', '.join(unknown)}")
    if any(name not in NUMERIC for name in MEASURES):
        raise RuntimeError("Spot rate rollup measures must be numeric")


def table_sql(schema: str, table: str = TABLE) -> str:
    _validate()
    columns = [
        f'"{name}" {"date" if name == PERIOD else "text"}'
        for name in DIMENSIONS
    ] + [f'"{name}" double precision' for name in MEASURES]
    return f'CREATE TABLE IF NOT EXISTS "{schema}".{table} ({", ".join(columns)})'


def index_sql(schema: str, table: str = TABLE) -> list[str]:
    return [
        f'CREATE INDEX IF NOT EXISTS {table}_period_idx '
        f'ON "{schema}".{table} ("{PERIOD}")',
        f'CREATE INDEX IF NOT EXISTS {table}_sub_idx '
        f'ON "{schema}".{table} (lower("SubAccountId"), "{PERIOD}")',
    ]


def select_sql(source: str, window: tuple[dt.date, dt.date] | None = None) -> str:
    dimensions = ", ".join(f'"{name}"' for name in DIMENSIONS)
    groups = dimensions
    measures = ", ".join(
        f'CAST(sum("{name}") AS double precision) AS "{name}"' for name in MEASURES)
    predicates = ['"PricingQuantity" > 0']
    if window is not None:
        start, stop = window
        predicates.extend((
            f'"{PERIOD}" >= DATE \'{start.isoformat()}\'',
            f'"{PERIOD}" < DATE \'{stop.isoformat()}\'',
        ))
    return (
        f"SELECT {dimensions}, {measures} FROM {source} "
        f"WHERE {' AND '.join(predicates)} GROUP BY {groups}"
    )
