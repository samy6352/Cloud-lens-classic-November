"""The few charges the AI Billing and ESU views ask about, so neither reads `costs`.

Why
---
AI Billing filters the detail table by service, product and provider labels and then
groups what is left. On the 16.9M-row estate the filter alone took longer than the
180s statement bound, so the tab returned HTTP 500 on every load and was never cached.
ESU asked the same kind of question -- a meter or product label -- over *all* history,
with no date bound at all.

What it is
----------
Rows matching either signal, grouped to the columns those views read. Measured on one
real month: 243,668 AI rows of 5,813,121, before grouping.

The build filter is a superset, not the answer. Every column either predicate reads is
part of the grain, so readers re-apply their own predicate here exactly as they would
against `costs`, and a narrower predicate in a later release stays correct without a
rebuild. A *wider* one would miss rows, which is what `LAYOUT` is for: it fingerprints
the build filter, and a changed fingerprint drops and rebuilds this table alone.

The counts are carried as measures because AI Billing reports data quality as well as
money: a group with a missing cost, a missing quantity or a negative charge is shown as
unreliable rather than summed. `count(*)` over detail rows is `sum("Charges")` here, and
the same holds for the other three, so those checks keep their exact meaning.
"""
from __future__ import annotations

import datetime as dt
import hashlib

from . import derived, environment

TABLE = "usage_signals"

DIMENSIONS = [
    "ChargePeriodStart",
    "SubAccountId",
    "SubAccountName",
    "ResourceId",
    "ResourceName",
    "ResourceGroup",
    "RegionName",
    "ServiceFamily",
    "ServiceName",
    "ProductName",
    "MeterName",
    "UnitOfMeasure",
    "BillingCurrency",
    "ChargeCategory",
    "PricingModel",
]
if environment.ENABLED:
    DIMENSIONS.append(environment.COLUMN)

PERIOD = DIMENSIONS[0]
# Money and quantity sum as themselves. The four counts are computed, not source
# columns, so they are declared separately for the column check.
SUMMED = ["BilledCost", "PricingQuantity"]
COUNTS = ["Charges", "CostRows", "QuantityRows", "NegativeRows"]
MEASURES = SUMMED + COUNTS
FILTERABLE = derived.filterable(DIMENSIONS)

ESU_PREDICATE = ("(lower(COALESCE(\"MeterName\", '')) LIKE '%esu%' "
                 "OR lower(COALESCE(\"ProductName\", '')) LIKE '%extended security%')")


def build_predicate() -> str:
    from .ai_billing import _ai_predicate

    return f"({_ai_predicate()} OR {ESU_PREDICATE})"


def layout() -> str:
    """Fingerprint of what the build keeps; a change rebuilds this table alone."""
    return hashlib.sha256(build_predicate().encode()).hexdigest()[:16]


def serves_filters(filters: dict[str, list[str]] | None) -> bool:
    return derived.serves_filters(filters, FILTERABLE)


def _validate() -> None:
    from .warehouse import COLUMNS, NUMERIC

    unknown = [name for name in environment.source_columns(DIMENSIONS + SUMMED)
               if name not in COLUMNS]
    if unknown:
        raise RuntimeError(f"usage signals names unknown columns: {', '.join(unknown)}")
    if any(name not in NUMERIC for name in SUMMED):
        raise RuntimeError("usage signal measures must be numeric")


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
        source = environment.project(source, DIMENSIONS + SUMMED)
    dimensions = ", ".join(f'"{name}"' for name in DIMENSIONS if name != environment.COLUMN)
    groups = ", ".join(f'"{name}"' for name in DIMENSIONS)
    sums = ", ".join(f'CAST(sum("{name}") AS double precision) AS "{name}"' for name in SUMMED)
    counts = (
        'CAST(count(*) AS double precision) AS "Charges", '
        'CAST(count("BilledCost") AS double precision) AS "CostRows", '
        'CAST(count("PricingQuantity") AS double precision) AS "QuantityRows", '
        'CAST(sum(CASE WHEN "BilledCost" < 0 THEN 1 ELSE 0 END) AS double precision) '
        'AS "NegativeRows"')
    predicates = [build_predicate()]
    if window is not None:
        start, stop = window
        predicates.extend((
            f'"{PERIOD}" >= DATE \'{start.isoformat()}\'',
            f'"{PERIOD}" < DATE \'{stop.isoformat()}\'',
        ))
    env = f', "{environment.COLUMN}"' if environment.ENABLED else ""
    return (f"SELECT {dimensions}, {sums}, {counts}{env} FROM {source} "
            f"WHERE {' AND '.join(predicates)} GROUP BY {groups}")
