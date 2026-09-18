"""A pre-aggregated table the dashboard can be answered from.

Why
---
The dashboard asks the same seven questions of every page load, and all of them
are sums over a date window grouped by one dimension. On a columnar engine that was fine:
a columnar scan of the whole table costs milliseconds. PostgreSQL is a row
store, so summing one column means reading every row in full -- 25 columns of
resource ids and meter descriptions, to add up one float.

Measured on the deployed B1ms, monthly trend over 2M rows:

    from the detail table    11,300 ms
    from this rollup             33 ms      <- 32 ms of which is network

That is not a tuning difference, it is the difference between a dashboard and a
progress bar. And it is not specific to PostgreSQL: any engine that scans per
query has the same problem, which is why a lakehouse serving layer would need
the same table.

What it is
----------
One row per (day, currency, subscription, service, resource group, region), with
the money summed. Every dashboard grouping is a coarser cut of that grain, so
each is answered by re-aggregating a few thousand rows instead of millions.

What it deliberately does not hold
----------------------------------
`count(DISTINCT "ResourceId")`. Distinct counts cannot be re-aggregated -- summing
per-day distinct resources across a month counts a resource that ran all month
thirty times. Rather than store a number that is wrong when the range is not one
day, those queries stay on the detail table where they are correct. `covers()`
is what decides, and it refuses anything it cannot answer exactly.

A rollup that is subtly wrong is worse than no rollup at all: this is a tool
people use to explain spend to a finance team. `test_rollup.py` requires it to
agree with the detail table to the cent, on the same data, for every grouping the
dashboard uses.
"""
from __future__ import annotations

import datetime as dt
import logging
from collections.abc import Iterable
from typing import Any

from . import derived

log = logging.getLogger("cloudlens.rollup")

TABLE = "costs_rollup"

# The grain. Every dashboard grouping is one of these columns or a coarser cut of
# one, which is what makes the rollup able to answer them exactly.
#
# ChargePeriodStart is the finest useful time grain: Cost Management exports are
# daily, so there is nothing below it to lose.
DIMENSIONS = [
    "ChargePeriodStart",
    "BillingCurrency",
    "SubAccountId",
    "SubAccountName",
    "ServiceFamily",
    "ServiceName",
    "ResourceGroup",
    "RegionName",
    # Marketplace/ISV spend is selected by publisher rather than by service family, so
    # without this the Marketplace tab was the one tab that had to read the detail table
    # for its money. Measured on the real estate before adding it: three distinct values
    # across 5.8M rows, so it costs the grain essentially nothing.
    "PublisherName",
    # The two dimensions the filter bar could not answer from here, so it scanned
    # 5,821,770 detail rows to find seven values between them -- 7.6-8.5s, against
    # 0.10-0.12s for the three it could. Measured before adding them: the grain goes
    # from 68,698 rows to 74,267, an 8.1% increase, because both correlate almost
    # entirely with the service already in the grain.
    #
    # They also make the rollup able to *narrow* itself by `charge` and `pricing`,
    # which previously sent any filtered query to the detail table.
    "ChargeCategory",
    "PricingModel",
]

# The date column the rollup is windowed and retained by. Named off DIMENSIONS
# rather than repeated as a literal so the two cannot drift apart.
PERIOD = DIMENSIONS[0]

# Summed. Additive, so they survive re-aggregation at any coarser grain.
#
# `UnitPrice` is deliberately absent even though it is numeric: it is a rate, and
# adding rates together produces a number with no meaning. `PricingQuantity` is
# here because hours and GB genuinely do add up, and the uptime and waste views
# ask for them.
MEASURES = [
    "BilledCost",
    "CostInUsd",
    "PricingQuantity",
]


def _validate() -> None:
    """Fail at import if the grain names a column the warehouse does not have.

    Worth doing because the failure mode without it is quiet. `rebuild` runs
    inside a try/except that logs and continues -- correctly, because a broken
    rollup should not fail the refresh that produced the rows -- so three invented
    column names produced nothing but one warning line, an empty rollup, and a
    dashboard that had silently gone back to full scans.
    """
    from .warehouse import COLUMNS, NUMERIC

    unknown = [c for c in DIMENSIONS + MEASURES if c not in COLUMNS]
    if unknown:
        raise RuntimeError(
            f"rollup names columns the warehouse does not have: {', '.join(unknown)}")

    not_numeric = [m for m in MEASURES if m not in NUMERIC]
    if not_numeric:
        raise RuntimeError(
            f"rollup would sum non-numeric columns: {', '.join(not_numeric)}")

# Groupings the dashboard uses that the rollup holds. Anything outside this set
# goes to the detail table.
SERVABLE = set(DIMENSIONS)


def covers(group_by: list[str], measures: list[str]) -> bool:
    """Whether the rollup can answer this exactly.

    Deliberately conservative. A false negative costs a slow query; a false
    positive costs a wrong number on a page someone is about to send to their
    finance team, and they have no way to tell.
    """
    return (all(g in SERVABLE for g in group_by)
            and all(m in MEASURES for m in measures))


# The filter-bar dimensions the rollup can narrow itself by, worked out from the grain
# rather than listed again -- so adding a column above cannot leave this behind. `env`
# names the `Tags` document, which no grain carries, so a connection filtering on it
# still has to answer from the detail table.
FILTERABLE = derived.filterable(DIMENSIONS)


def serves_filters(filters: dict[str, list[str]] | None) -> bool:
    """Whether every dimension the caller narrowed is one the rollup holds."""
    return derived.serves_filters(filters, FILTERABLE)


def derive_sql(source: str) -> str:
    """The rollup grain computed on the fly from `source`.

    The fallback for connections the rollup table cannot serve -- a converted
    currency, or a filter on a dimension outside the grain. Aggregating the scoped
    `costs` view gives a relation with exactly the rollup's shape, so a query reads
    the same either way and cannot tell which backing answered it.

    No faster than querying the detail table directly, which is the point: it is
    there so that routing a query to the rollup is never *wrong*, not so that every
    route is quick.
    """
    return select_sql(source)


def charges_sql(source: str) -> str:
    """How many detail rows `source` stands for.

    Both backings of the rollup carry the count as `charges` -- the table because
    `select_sql` builds it, the on-the-fly fallback because it is the same
    statement -- so the only relation without the column is the detail table
    itself, where one row is one charge.

    Here rather than at the call site so that a caller holding `Reader.agg` can
    ask for a row count without first working out which relation it is holding.
    Cast because `sum(bigint)` is `numeric` in PostgreSQL, which psycopg hands
    back as a `Decimal` -- and a row count that is a Decimal reaches the JSON
    encoder, which refuses it. `count(*)` is already `bigint`, so the two arms
    return the same Python type and a caller cannot tell them apart.
    """
    return "count(*)" if source == "costs" else "CAST(sum(charges) AS bigint)"


def table_sql(schema: str, table: str = TABLE) -> str:
    """DDL for the rollup table, at whatever name it is being built under.

    Parameterised by name so a full rebuild can assemble the replacement in a
    staging table of the same shape. See `derived` for why that is worth doing.
    """
    _validate()
    cols = ",\n  ".join(
        [f'"{d}" {"date" if d == "ChargePeriodStart" else "text"}' for d in DIMENSIONS]
        + [f'"{m}" double precision' for m in MEASURES]
        + ["charges bigint"]
    )
    return f'CREATE TABLE IF NOT EXISTS "{schema}".{table} (\n  {cols}\n)'


def index_sql(schema: str, table: str = TABLE) -> list[str]:
    """The indexes the rollup is read through.

    Named after `table` so that a staging rebuild, which creates them only after
    the swap has given the table its real name, produces the same names a
    first-time create would.
    """
    return [
        # Every dashboard query is bounded by a date window, so that column leads.
        f'CREATE INDEX IF NOT EXISTS {table}_period_idx '
        f'ON "{schema}".{table} ("ChargePeriodStart")',
        # And most are then narrowed to a set of subscriptions.
        f'CREATE INDEX IF NOT EXISTS {table}_sub_idx '
        f'ON "{schema}".{table} (lower("SubAccountId"), "ChargePeriodStart")',
    ]


def select_sql(source: str, window: tuple[dt.date, dt.date] | None = None) -> str:
    """The rollup grain computed from `source`.

    Public, and named to match `presence.select_sql`, because `derived` drives both
    modules through the same three functions.
    """
    dims = ", ".join(f'"{d}"' for d in DIMENSIONS)
    sums = ", ".join(f'sum("{m}") AS "{m}"' for m in MEASURES)
    where = ""
    if window is not None:
        lo, hi = window
        where = (f" WHERE \"{PERIOD}\" >= DATE '{lo.isoformat()}'"
                 f" AND \"{PERIOD}\" < DATE '{hi.isoformat()}'")
    return (f"SELECT {dims}, {sums}, count(*) AS charges "
            f"FROM {source}{where} "
            f"GROUP BY {', '.join(str(i + 1) for i in range(len(DIMENSIONS)))}")


def month_start(day: dt.date) -> dt.date:
    return day.replace(day=1)


def next_month(day: dt.date) -> dt.date:
    return (day.replace(day=1) + dt.timedelta(days=32)).replace(day=1)


def month_window(days: Iterable[dt.date]) -> tuple[dt.date, dt.date] | None:
    """The smallest whole-month span covering `days`, or None if there are none.

    Whole months rather than the exact day range on purpose. The window is the
    unit the rollup is rebuilt in, and snapping it out to month boundaries means
    a scoped rebuild lines up with `costs_fact`'s monthly partitions, so the
    planner can prune to the partitions that actually changed. Snapping outwards
    can only ever include more source rows than strictly necessary, never fewer,
    so it cannot cause a stale row to survive.
    """
    days = [d for d in days if d is not None]
    if not days:
        return None
    return month_start(min(days)), next_month(max(days))


def rebuild(con: Any, schema: str, source: str,
            window: tuple[dt.date, dt.date] | None = None) -> int:
    """Rebuild the rollup alone from the detail table.

    The mechanism, and the reasoning behind it, is `derived.rebuild`. This is the
    one-table spelling of it, for callers that want only the rollup rebuilt.
    `Warehouse.refresh_rollup` does not use it: it rebuilds both derived tables in
    a single call so their swaps land together rather than one blocking the other.
    """
    import sys

    from . import derived

    return derived.rebuild(con, schema, (sys.modules[__name__],), source, window)[TABLE]
