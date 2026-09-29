"""Exact, sparse benefit usage for commitment coverage and inventory."""
from __future__ import annotations

import datetime as dt

from . import derived, environment

TABLE = "commitment_benefits"
DIMENSIONS = [
    "ChargePeriodStart",
    "BillingCurrency",
    "SubAccountId",
    "BenefitId",
    "BenefitName",
    "PricingModel",
    "ServiceName",
    "ResourceGroup",
    "RegionName",
    "ChargeCategory",
    "ResourceId",
    "ResourceName",
]
if environment.ENABLED:
    DIMENSIONS.append(environment.COLUMN)

PERIOD = DIMENSIONS[0]
MEASURES = ["BilledCost"]
FILTERABLE = derived.filterable(DIMENSIONS)
BENEFIT_PREDICATE = (
    'coalesce("BenefitId", "BenefitName") IS NOT NULL '
    'AND coalesce("BenefitId", "BenefitName") <> \'\''
)


def serves_filters(filters: dict[str, list[str]] | None) -> bool:
    return derived.serves_filters(filters, FILTERABLE)


def _validate() -> None:
    from .warehouse import COLUMNS, NUMERIC

    unknown = [
        name for name in environment.source_columns(DIMENSIONS + MEASURES)
        if name not in COLUMNS
    ]
    if unknown:
        raise RuntimeError(
            f"commitment benefit rollup names unknown columns: {', '.join(unknown)}")
    if any(name not in NUMERIC for name in MEASURES):
        raise RuntimeError("commitment benefit rollup measures must be numeric")


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
    dimensions = ", ".join(
        f'"{name}"' for name in DIMENSIONS if name != environment.COLUMN)
    groups = ", ".join(f'"{name}"' for name in DIMENSIONS)
    measures = ", ".join(
        f'CAST(sum("{name}") AS double precision) AS "{name}"' for name in MEASURES)
    predicates = [BENEFIT_PREDICATE]
    if window is not None:
        start, stop = window
        predicates.extend((
            f'"{PERIOD}" >= DATE \'{start.isoformat()}\'',
            f'"{PERIOD}" < DATE \'{stop.isoformat()}\'',
        ))
    env = f', "{environment.COLUMN}"' if environment.ENABLED else ""
    return (
        f"SELECT {dimensions}, {measures}{env} FROM {source} "
        f"WHERE {' AND '.join(predicates)} GROUP BY {groups}"
    )
