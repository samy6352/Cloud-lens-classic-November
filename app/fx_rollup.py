"""Opt-in native FX sufficient statistics; never persist display-rate money.

The ordinary cost grain loses invoice FX provenance. This companion keeps each
provider exchange rate and eligible legacy USD ratio separate, so the warehouse's
row-aware conversion can run on aggregates without substituting today's rates for
invoice rates. Resource money and Billing's independent measures still fall back.
"""
from __future__ import annotations

import datetime as dt
import os

from . import derived, environment, rollup


def enabled() -> bool:
    value = os.getenv("CLOUDLENS_FX_ROLLUP", "false").strip().lower()
    if value not in {"true", "false"}:
        raise ValueError("CLOUDLENS_FX_ROLLUP must be true or false")
    return value == "true"


ENABLED = enabled()
TABLE = "costs_fx_rollup"
PERIOD = rollup.PERIOD
RATIO = "FxLegacyRatio"
NONZERO = "FxNonzeroCharges"
DIMENSIONS = [
    *rollup.DIMENSIONS, "PricingCurrency", "ExchangeRatePricingToBilling",
    "BillingCostBasis", RATIO,
]
MEASURES = [*rollup.MEASURES, "charges", NONZERO]
FILTERABLE = derived.filterable(DIMENSIONS)
LEGACY_RATIO = (
    'CASE WHEN ("BillingCostBasis" IN (\'ActualCost\', \'AmortizedCost\') '
    'OR "BillingCostBasis" IS NULL) AND "BilledCost" <> 0 '
    'AND "CostInUsd" / NULLIF("BilledCost", 0) > 0 '
    'THEN "CostInUsd" / NULLIF("BilledCost", 0) ELSE NULL END'
)


def serves_filters(filters: dict[str, list[str]] | None) -> bool:
    return derived.serves_filters(filters, FILTERABLE)


def table_sql(schema: str, table: str = TABLE) -> str:
    columns = []
    for name in DIMENSIONS + MEASURES:
        if name == environment.COLUMN:
            continue
        kind = ("date" if name == PERIOD else "bigint"
                if name in {"charges", NONZERO} else "double precision"
                if name in {*rollup.MEASURES, RATIO, "ExchangeRatePricingToBilling"}
                else "text")
        columns.append(f'"{name}" {kind}')
    if environment.ENABLED:
        columns.append(f'"{environment.COLUMN}" text')
    return f'CREATE TABLE IF NOT EXISTS "{schema}".{table} ({", ".join(columns)})'


def index_sql(schema: str, table: str = TABLE) -> list[str]:
    return rollup.index_sql(schema, table)


def select_sql(source: str, window: tuple[dt.date, dt.date] | None = None) -> str:
    columns = [c for c in DIMENSIONS if c != RATIO] + rollup.MEASURES
    if environment.ENABLED:
        source = environment.project(source, columns)
    where = ""
    if window is not None:
        lo, hi = window
        where = (f' WHERE "{PERIOD}" >= DATE \'{lo.isoformat()}\''
                 f' AND "{PERIOD}" < DATE \'{hi.isoformat()}\'')
    selected = ", ".join(f'"{c}"' for c in columns)
    source = f'(SELECT {selected}, {LEGACY_RATIO} AS "{RATIO}" FROM {source}{where}) fx_source'
    dims = ", ".join(f'"{c}"' for c in DIMENSIONS if c != environment.COLUMN)
    groups = ", ".join(f'"{c}"' for c in DIMENSIONS)
    sums = ", ".join(f'sum("{c}") AS "{c}"' for c in rollup.MEASURES)
    env = f', "{environment.COLUMN}"' if environment.ENABLED else ""
    return (
        f'SELECT {dims}, {sums}, count(*) AS charges, '
        f'count(*) FILTER (WHERE "BilledCost" <> 0) AS "{NONZERO}"{env} '
        f'FROM {source} GROUP BY {groups}'
    )


def converted_sql(source: str, factor: str, target: str) -> str:
    """Reproduce the ordinary rollup shape, merging native currencies only now."""
    dims = [c for c in rollup.DIMENSIONS if c != environment.COLUMN]
    selected = [
        f"'{target}'::text AS \"BillingCurrency\"" if c == "BillingCurrency" else f'"{c}"'
        for c in dims
    ]
    selected += [
        f'sum("BilledCost" * ({factor})) AS "BilledCost"',
        'sum("CostInUsd") AS "CostInUsd"',
        'sum("PricingQuantity") AS "PricingQuantity"',
        'CAST(sum(charges) AS bigint) AS charges',
    ]
    if environment.ENABLED:
        selected.append(f'"{environment.COLUMN}"')
    groups = ", ".join(f'"{c}"' for c in rollup.DIMENSIONS if c != "BillingCurrency")
    return f'SELECT {", ".join(selected)} FROM {source} GROUP BY {groups}'
