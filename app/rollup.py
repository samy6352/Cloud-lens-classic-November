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

import logging
import time
from typing import Any

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
]

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


def create_sql(schema: str) -> list[str]:
    """DDL for the rollup table and its indexes."""
    _validate()
    cols = ",\n  ".join(
        [f'"{d}" {"date" if d == "ChargePeriodStart" else "text"}' for d in DIMENSIONS]
        + [f'"{m}" double precision' for m in MEASURES]
        + ["charges bigint"]
    )
    return [
        f'CREATE TABLE IF NOT EXISTS "{schema}".{TABLE} (\n  {cols}\n)',
        # Every dashboard query is bounded by a date window, so that column leads.
        f'CREATE INDEX IF NOT EXISTS {TABLE}_period_idx '
        f'ON "{schema}".{TABLE} ("ChargePeriodStart")',
        # And most are then narrowed to a set of subscriptions.
        f'CREATE INDEX IF NOT EXISTS {TABLE}_sub_idx '
        f'ON "{schema}".{TABLE} (lower("SubAccountId"), "ChargePeriodStart")',
    ]


def _select(source: str) -> str:
    dims = ", ".join(f'"{d}"' for d in DIMENSIONS)
    sums = ", ".join(f'sum("{m}") AS "{m}"' for m in MEASURES)
    return (f"SELECT {dims}, {sums}, count(*) AS charges "
            f"FROM {source} GROUP BY {', '.join(str(i + 1) for i in range(len(DIMENSIONS)))}")


def rebuild(con: Any, schema: str, source: str) -> int:
    """Rebuild the whole rollup from the detail table.

    A full rebuild rather than an incremental merge. The rollup is derived data
    whose only job is to agree with the table it came from, and a rebuild cannot
    drift: there is no partial-failure state where some months were updated and
    others were not, and no need to work out which rows an ingest touched.

    It costs one scan of the detail table, which is the same scan a single
    unaggregated dashboard query was doing anyway -- except this one happens once
    per refresh instead of seven times per page load.

    Inside a transaction, so readers see either the old rollup or the new one.
    DELETE rather than DROP keeps the indexes and the table's permissions.
    """
    t0 = time.monotonic()
    con.execute("BEGIN")
    try:
        con.execute(f'DELETE FROM "{schema}".{TABLE}')
        con.execute(f'INSERT INTO "{schema}".{TABLE} {_select(source)}')
        con.execute("COMMIT")
    except Exception:
        con.execute("ROLLBACK")
        raise

    rows = con.execute(f'SELECT count(*) FROM "{schema}".{TABLE}').fetchone()[0]
    # The planner needs current statistics or it may still choose a sequential
    # scan of the detail table for a query the rollup could answer.
    con.execute(f'ANALYZE "{schema}".{TABLE}')
    log.info("rollup: %s rows in %.1fs", f"{rows:,}", time.monotonic() - t0)
    return int(rows or 0)
