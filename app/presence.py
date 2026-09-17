"""Named resources and what they cost, so the dashboard need not read the detail table.

Why
---
`costs_rollup` deliberately refuses `count(DISTINCT "ResourceId")`: distinct counts
cannot be re-aggregated, because summing per-day distinct resources counts a
resource that ran all month thirty times. So those queries stayed on the detail
table -- and then *became* the dashboard's cost. Measured on 5,808,945 rows:

    the four distinct counts on the overview      61.5 s
    the per-region resource count beside them     55.6 s
    everything the rollup answers                  0.05 s

117 of the overview's 147 seconds were two queries counting resources.

What it is
----------
One row per distinct combination of the dimensions below, with the billed money
summed. Counting is exact rather than re-aggregated: `count(DISTINCT "ResourceId")`
over this table reads the same identities the detail table holds, because every
distinct combination that appears in `costs` appears here exactly once.

Why it now holds money
----------------------
It did not, originally, and the reasoning was that holding none kept it honest.
Measurement retired that argument. With the rollup carrying the tabs' money, one
query was left reading the detail table -- the per-resource "top resources" list,
because a resource's *name* and its individual cost live nowhere else:

    section(compute, 30d)            22.54 s total
      top resources (detail table)   19.19 s      <- 85% of the tab
      everything else                 3.35 s

The honesty argument does not actually apply to a sum. A distinct count cannot be
re-aggregated; a sum can, and exactly, because every row in `costs` falls into
exactly one group here. That is the same property `costs_rollup` already depends
on. What did have to be preserved is the narrower rule: money may only be read
from this table when the connection has not converted currency, because the
conversion factor is row-dependent on `PricingCurrency` and
`ExchangeRatePricingToBilling`, neither of which is in the grain. `serves_money`
is what decides, and `Warehouse._shadow_presence` nulls the measures rather than
projecting an unconverted number, so a mistake reads as missing and not as wrong.

The grain, and what it cost
---------------------------
Measured against the real estate before choosing it:

    (day, resource)                                 2,269,943
    + currency, subscription, region                2,270,556   +0.03%
    + service family                                2,783,883   +22.6%
    + service name, resource group, publisher       2,784,069   +0.007%
    + resource name                                 2,784,070   +0.00004%

Only `ServiceFamily` costs anything, because a resource is billed under several
families in a day while it sits in exactly one subscription, region and group. The
per-tab counts need that column, and once it is paid for the remaining three are
free -- so the grain is the whole set rather than a careful subset.

`ResourceName` adds a single row across the whole estate. 127 resource ids do carry
more than one name, but never within one day, so the day already in the grain
separates them; the one extra row is a resource renamed mid-day.

Rows with no `ResourceId` are kept rather than filtered out. `count(DISTINCT ...)`
ignores NULLs by itself, so keeping them cannot inflate a resource count; dropping
them *would* lose a service or a region that only ever appears on a
subscription-level charge, and the service and region counts beside the resource
count would quietly go missing -- and, now that the money is here too, the total
would be short by every subscription-level charge in the estate.
"""
from __future__ import annotations

import datetime as dt
import logging
from typing import Any

log = logging.getLogger("cloudlens.presence")

TABLE = "resource_presence"

# The grain. Every column here is one the dashboard either counts, groups by, or
# narrows on -- nothing is carried speculatively, because each one can only add rows.
DIMENSIONS = [
    "ChargePeriodStart",
    "ResourceId",
    # The name the dashboard actually shows. A resource id is unreadable in a top-spend
    # list, and the name lives only on the detail table -- which is why that one query
    # was still reading it. Measured at +1 row across 5.8M, so it is free.
    "ResourceName",
    "BillingCurrency",
    "SubAccountId",
    "RegionName",
    "ServiceFamily",
    "ServiceName",
    "ResourceGroup",
    "PublisherName",
]

PERIOD = DIMENSIONS[0]

# Summed. Additive, so it survives re-aggregation at any coarser cut of the grain.
#
# Only `BilledCost`: this table exists to answer the per-resource money question the
# rollup's grain is too coarse for, and that question is asked of billed money alone.
# Carrying the rollup's other measures would widen every one of 2.8M rows to serve
# nothing that asks for them.
MEASURES = [
    "BilledCost",
]

# What this table can count exactly. `ResourceId` is the one that matters; the rest
# are here because the overview asks for them in the same query, and answering three
# of four from one table and the fourth from another would mean a join for nothing.
#
# `ResourceName` is deliberately absent: two resources in different groups may share
# a name, so counting distinct names is not counting resources.
COUNTABLE = frozenset({"ResourceId", "ServiceName", "SubAccountId", "RegionName"})

# What it can group by. Deliberately not `ResourceId`: a list keyed by raw id is not
# something the dashboard shows, and `ResourceName` beside it is what the per-resource
# view actually groups on.
GROUPABLE = frozenset(DIMENSIONS) - {"ResourceId"}


def _validate() -> None:
    """Fail at import if the grain names a column the warehouse does not have."""
    from .warehouse import COLUMNS, NUMERIC

    unknown = [c for c in DIMENSIONS + MEASURES if c not in COLUMNS]
    if unknown:
        raise RuntimeError(
            f"presence names columns the warehouse does not have: {', '.join(unknown)}")

    not_numeric = [m for m in MEASURES if m not in NUMERIC]
    if not_numeric:
        raise RuntimeError(
            f"presence would sum non-numeric columns: {', '.join(not_numeric)}")


def covers(group_by: list[str], counted: list[str]) -> bool:
    """Whether this table can answer a counting query exactly.

    Conservative in the same way `rollup.covers` is, and for the same reason: a
    false negative costs a slow query, a false positive costs a wrong resource
    count on a page somebody is about to take to a finance review.
    """
    return (all(g in GROUPABLE for g in group_by)
            and all(c in COUNTABLE for c in counted))


def serves_money(rate: float | None) -> bool:
    """Whether this table's summed money is correct for this connection.

    Only when no currency conversion is in play. The factor that converts a row is
    row-dependent on `PricingCurrency` and `ExchangeRatePricingToBilling` -- provider
    exchange rates take priority over display FX -- and neither column is in the
    grain, so there is no way to convert a summed figure here after the fact. Two
    rows sharing every dimension in this table can still have been priced in
    different currencies.

    Scope and the filter bar are not this function's business: those narrow which
    rows are read, which `Warehouse._shadow_presence` applies to the table itself,
    and `rollup.serves_filters` already refuses the filters that name columns
    outside the grain.
    """
    return rate is None


def table_sql(schema: str, table: str = TABLE) -> str:
    """DDL for the presence table, at whatever name it is being built under.

    Parameterised by name so a full rebuild can assemble the replacement in a
    staging table of the same shape. See `derived` for why that is worth doing.
    """
    _validate()
    cols = ",\n  ".join(
        [f'"{d}" {"date" if d == PERIOD else "text"}' for d in DIMENSIONS]
        + [f'"{m}" double precision' for m in MEASURES]
    )
    return f'CREATE TABLE IF NOT EXISTS "{schema}".{table} (\n  {cols}\n)'


def index_sql(schema: str, table: str = TABLE) -> list[str]:
    """The indexes the presence table is read through.

    Named after `table` so that a staging rebuild, which creates them only after
    the swap has given the table its real name, produces the same names a
    first-time create would.
    """
    return [
        # Every query against it is bounded by a date window, as with the rollup.
        f'CREATE INDEX IF NOT EXISTS {table}_period_idx '
        f'ON "{schema}".{table} ("{PERIOD}")',
        # And then usually narrowed to a set of subscriptions.
        f'CREATE INDEX IF NOT EXISTS {table}_sub_idx '
        f'ON "{schema}".{table} (lower("SubAccountId"), "{PERIOD}")',
    ]


def select_sql(source: str, window: tuple[dt.date, dt.date] | None = None) -> str:
    """The grain computed from `source`.

    `GROUP BY` rather than `SELECT DISTINCT` now that there is a measure to sum.
    The two produce identical rows for the dimensions -- grouping by every column
    of a projection is what distinct means -- so the set of identities this table
    holds, and therefore every count taken from it, is unchanged.
    """
    dims = ", ".join(f'"{d}"' for d in DIMENSIONS)
    sums = ", ".join(f'sum("{m}") AS "{m}"' for m in MEASURES)
    where = ""
    if window is not None:
        lo, hi = window
        where = (f" WHERE \"{PERIOD}\" >= DATE '{lo.isoformat()}'"
                 f" AND \"{PERIOD}\" < DATE '{hi.isoformat()}'")
    return (f"SELECT {dims}, {sums} FROM {source}{where} "
            f"GROUP BY {', '.join(str(i + 1) for i in range(len(DIMENSIONS)))}")


def rebuild(con: Any, schema: str, source: str,
            window: tuple[dt.date, dt.date] | None = None) -> int:
    """Rebuild the presence table alone from the detail table.

    The one-table spelling of `derived.rebuild`, which is where the mechanism and
    the reasoning behind it live. `Warehouse.refresh_rollup` rebuilds this table
    alongside the rollup in a single call, so the two can never disagree about
    which generation of `costs` they describe.
    """
    import sys

    from . import derived

    return derived.rebuild(con, schema, (sys.modules[__name__],), source, window)[TABLE]
