"""Billing's own measures, precomputed, so the statement need not re-derive them.

Why
---
`billing.build` issues one statement that classifies every row in two calendar
months and then aggregates it fifty ways. On the deployed lab estate of 13,556
rows that is imperceptible. On the real 5,811,998-row estate it is not:

    the Billing statement, detail table     451 s
    the same statement, from this table     see `billing.build`

It returned HTTP 500 under the server's 110s `statement_timeout`, and without the
cap it ran for six and a half minutes. Production never showed it because
production holds the lab data.

What actually costs
-------------------
Not the grouping. The measure set expands to **315 `FILTER` predicates**, and each
is evaluated against every row -- so the cost follows the row count, not the number
of groups. The daily series is the proof: it produces **93 rows** and still takes
**519.8s**, because it reads the same 5.8M rows to do it.

That is also why three rewrites all failed. Splitting the `GROUPING SETS` into one
pass per bucket measured 485s against the 451s baseline, and a two-stage
rank-then-measure plan took 282s for the resources bucket alone against 146s. Both
*added a pass*, and a pass over the detail table is precisely what is expensive.

An earlier round of probes concluded the opposite -- that the aggregates were
cheap and the 2M-key grouping was the cost. Those probes wrapped the query in
`SELECT count(*) FROM (...)`, which lets PostgreSQL discard the unused aggregate
expressions entirely: `EXPLAIN VERBOSE` shows 315 FILTER clauses selected directly
and none through the wrapper. Every number taken that way was measuring the
`GROUP BY` alone. Recorded here because the same trap is waiting for the next
person who benchmarks this table.

The grain, and what it cost
---------------------------
Measured on the real estate over the two months a statement reads, 5,817,991 rows:

    (currency, subscription, resource)              2,032,131
    + day, cost basis                               2,277,255   +12.0%
    + resource group                                2,773,754   +21.8%
    + service, region, charge category, pricing     2,800,170   +0.95%

The last four dimensions are nearly free once `ResourceGroup` is paid for, and
they are what lets this table narrow itself by the filter bar instead of handing
every filtered statement back to the detail table. So the grain is the whole set.

2.08x fewer rows is not much of a compression, and that is not the point: this
estate genuinely holds 2,032,125 distinct resources, so no table keyed by resource
can be smaller. The saving is that the 315 predicates are evaluated once at ingest
rather than on every page load.

What it deliberately does not hold
----------------------------------
`env`. It is not a column -- it is read out of the `Tags` document with four
possible spellings -- so no grain can carry it, and `derived.FILTER_COLUMNS` says
so in the one place both this table and the filter bar read. A statement filtered
by environment falls back to the detail table, correctly and slowly.

The classification is absent from the grain for a happier reason: it is already
*inside* the measures. `reservation_savings_total` was filtered to
`kind = 'reservation'` when it was computed, so re-aggregating it sums only
reservation rows without needing `kind` as a column. Measured, carrying the
classification explicitly added no rows at all.
"""
from __future__ import annotations

import datetime as dt
import logging
from typing import Any

from . import billing, derived

log = logging.getLogger("cloudlens.billing_rollup")

TABLE = "billing_rollup"

# The stored column names, and the expression in `billing`'s `valued` CTE each is
# computed from. Written as one map rather than two lists because the table's DDL,
# its `SELECT`, and its `GROUP BY` all have to agree about both halves, and three
# copies of a twelve-column grain is three chances to leave one out.
#
# Some names are the warehouse's own and some are billing's. That split is load
# bearing, not untidiness:
#
# * `SubAccountId`, `ResourceGroup`, `ServiceName`, `RegionName`, `ChargeCategory`
#   and `PricingModel` keep their FOCUS spelling because `Warehouse._prepare_connection`
#   applies the subscription scope and the filter bar by naming those columns. A
#   table that called them anything else could not be shadowed by the same clause.
# * `currency` and `resource_id` keep billing's spelling because they are billing's
#   derived keys, not the raw columns -- `resource_id` carries the `unallocated:`
#   fallback for rows with no resource, and grouping by the raw column instead
#   would collapse every such row in a subscription into one.
GRAIN: dict[str, str] = {
    "ChargePeriodStart": '"ChargePeriodStart"',
    # Already `upper(trim(...))`-normalised by the CTE. Stored that way so the read
    # groups on the same value the detail path groups on.
    "currency": "currency",
    "BillingCostBasis": '"BillingCostBasis"',
    # Already lowercased by the CTE, which is what `lower("SubAccountId") IN (...)`
    # in the scope clause expects to match.
    "SubAccountId": "sub_id",
    "SubAccountName": '"SubAccountName"',
    "ServiceName": '"ServiceName"',
    "resource_id": "resource_id",
    "ResourceName": '"ResourceName"',
    "ResourceGroup": '"ResourceGroup"',
    "RegionName": '"RegionName"',
    "ChargeCategory": '"ChargeCategory"',
    "PricingModel": '"PricingModel"',
}

DIMENSIONS = list(GRAIN)
PERIOD = DIMENSIONS[0]

# The measures, which are `billing._empty()`'s keys and nothing else. Taken from
# there rather than restated so that a metric added to `billing.METRICS` widens this
# table too; a measure this table did not carry would read as zero rather than as
# missing, and a Billing statement short by one metric is the failure this whole
# module exists to avoid being slow about.
MEASURES = list(billing._empty())

# The detail-table columns the grain is derived from. Checked at build rather than
# the stored names, which are partly billing's own and would never match `COLUMNS`.
SOURCE_COLUMNS = [
    "ChargePeriodStart", "BillingCurrency", "BillingCostBasis", "SubAccountId",
    "SubAccountName", "ServiceName", "ResourceId", "ResourceName", "ResourceGroup",
    "RegionName", "ChargeCategory", "PricingModel", "BenefitId",
    "CommitmentDiscountStatus", "CommitmentDiscountType",
    "BilledCost", "InvoiceCost", "ListCost", "ContractedCost", "EffectiveCost",
]

# Worked out from this table's own grain, the same way `presence` does it, so that
# adding a filter column here is what widens what it claims to serve.
FILTERABLE = derived.filterable(DIMENSIONS)


def serves_filters(filters: dict[str, list[str]] | None) -> bool:
    """Whether every dimension the caller narrowed is one this grain holds.

    False for `env`, always: it is read from the `Tags` document rather than a
    column, so there is nothing here to narrow. That statement goes to the detail
    table and takes the 451s, which is the correct answer slowly rather than a
    fast one that silently ignored the filter.
    """
    return derived.serves_filters(filters, FILTERABLE)


def _validate() -> None:
    """Fail at import if the grain is computed from a column the warehouse lacks.

    Worth doing for the reason `rollup._validate` gives: `derived.rebuild` runs
    under a `try/except` that logs and carries on, so without this an invented
    column name produces one warning line, an empty table, and a Billing tab that
    has quietly gone back to full scans.
    """
    from .warehouse import COLUMNS

    unknown = [c for c in SOURCE_COLUMNS if c not in COLUMNS]
    if unknown:
        raise RuntimeError(
            f"billing_rollup names columns the warehouse does not have: {', '.join(unknown)}")


def table_sql(schema: str, table: str = TABLE) -> str:
    """DDL for the table, at whatever name it is being built under.

    Every measure is `double precision`, counts included. They are counts of rows
    in a single estate's charge data -- 5.8M at the top end, against 2**53 before a
    double loses integer precision -- and a uniform type means `_sql_remeasures`
    can sum them all the same way without knowing which are counts.
    """
    _validate()
    cols = ",\n  ".join(
        [f'"{name}" {"date" if name == PERIOD else "text"}' for name in DIMENSIONS]
        + [f'"{m}" double precision' for m in MEASURES]
    )
    return f'CREATE TABLE IF NOT EXISTS "{schema}".{table} (\n  {cols}\n)'


def index_sql(schema: str, table: str = TABLE) -> list[str]:
    """The indexes this table is read through.

    Named after `table` so a staging rebuild, which creates them before the swap
    gives the table its real name, produces the names a first-time create would.
    """
    return [
        # Every statement is bounded to a calendar month and the one before it.
        f'CREATE INDEX IF NOT EXISTS {table}_period_idx '
        f'ON "{schema}".{table} ("{PERIOD}")',
        # And then narrowed to the subscriptions the viewer is allowed to see.
        f'CREATE INDEX IF NOT EXISTS {table}_sub_idx '
        f'ON "{schema}".{table} (lower("SubAccountId"), "{PERIOD}")',
    ]


def select_sql(source: str, window: tuple[dt.date, dt.date] | None = None) -> str:
    """The grain computed from `source`, using billing's own classification.

    The date predicate goes *inside* the CTE rather than around it, so a windowed
    rebuild prunes to the matching `costs_fact` partitions instead of normalising
    a year of rows and discarding all but a month.

    The measures are cast to `double precision` in an outer projection rather than
    left as `_sql_measures` writes them. Seven of the fifty are `count`s and so
    arrive as `bigint`, while `table_sql` declares every measure `double precision`
    -- a difference `INSERT INTO ... SELECT` used to absorb silently by coercing to
    the declared type. A full rebuild is a `CREATE TABLE AS`, which takes its types
    from the query instead, and without this cast the swap fails outright:
    `CREATE OR REPLACE VIEW` will not change a published column from `double
    precision` to `bigint`.

    Casting here rather than in `_sql_measures` keeps the live Billing statement's
    SQL exactly as it was, and has the further effect of making the computed
    fallback in `_shadow_billing_rollup` agree with the stored table column for
    column -- which it did not before.

    The projection is over the aggregated rows, not the detail table, so it costs
    a pass over the grain rather than over the estate.
    """
    where = ""
    if window is not None:
        lo, hi = window
        where = (f"WHERE \"{PERIOD}\" >= DATE '{lo.isoformat()}'"
                 f" AND \"{PERIOD}\" < DATE '{hi.isoformat()}'")
    dims = ", ".join(f'{expr} AS "{name}"' for name, expr in GRAIN.items())
    grain = (f"{billing._sql_cte(None, source=source, where=where)}"
             f" SELECT {dims}, {billing._sql_measures()} FROM valued"
             f" GROUP BY {', '.join(str(i + 1) for i in range(len(DIMENSIONS)))}")
    projected = ", ".join(
        [f'"{name}"' for name in DIMENSIONS]
        + [f'CAST("{measure}" AS double precision) AS "{measure}"'
           for measure in MEASURES])
    return f"SELECT {projected} FROM ({grain}) AS grain"


def rebuild(con: Any, schema: str, source: str,
            window: tuple[dt.date, dt.date] | None = None) -> int:
    """Rebuild this table alone from the detail table.

    The one-table spelling of `derived.rebuild`. `Warehouse.refresh_rollup` builds
    it alongside the rollup and the presence table in a single call, so the three
    can never describe different generations of `costs`.
    """
    import sys

    return derived.rebuild(con, schema, (sys.modules[__name__],), source, window)[TABLE]
