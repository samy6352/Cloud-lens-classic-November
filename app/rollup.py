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
import time
from collections.abc import Iterable
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


def _select(source: str, window: tuple[dt.date, dt.date] | None = None) -> str:
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
    """Rebuild the rollup from the detail table.

    A rebuild rather than an incremental merge. The rollup is derived data whose
    only job is to agree with the table it came from, and a rebuild cannot drift:
    there is no partial-failure state where some rows were updated and others
    were not, and no need to work out which individual rows an ingest touched.

    `window` narrows that rebuild to a half-open `[lo, hi)` span of charge dates.
    The property above is preserved *within the window*, because everything in it
    is deleted and recomputed from source -- nothing is merged. What the caller
    takes on is the obligation to pass a window that actually covers every day the
    write touched; days outside it keep whatever the last rebuild produced. A
    caller that cannot state that with certainty must pass `None` and rebuild the
    lot, which is why that remains the default.

    This matters at retention scale. A full rebuild costs one scan of all retained
    history, and at a year of daily ingestion that is the same scan repeated every
    day to correct one day's rows. A windowed rebuild scans the month that changed,
    and because the window is snapped to month boundaries it prunes to the matching
    `costs_fact` partitions instead of reading the rest of the year to discard it.

    Inside a transaction, so readers see either the old rollup or the new one.
    DELETE rather than DROP keeps the indexes and the table's permissions.
    """
    t0 = time.monotonic()
    scope = ""
    if window is not None:
        lo, hi = window
        scope = (f' WHERE "{PERIOD}" >= DATE \'{lo.isoformat()}\''
                 f' AND "{PERIOD}" < DATE \'{hi.isoformat()}\'')
    con.execute("BEGIN")
    try:
        con.execute(f'DELETE FROM "{schema}".{TABLE}{scope}')
        con.execute(f'INSERT INTO "{schema}".{TABLE} {_select(source, window)}')
        con.execute("COMMIT")
    except Exception:
        con.execute("ROLLBACK")
        raise

    rows = con.execute(f'SELECT count(*) FROM "{schema}".{TABLE}').fetchone()[0]
    # The planner needs current statistics or it may still choose a sequential
    # scan of the detail table for a query the rollup could answer.
    con.execute(f'ANALYZE "{schema}".{TABLE}')
    log.info("rollup: %s rows in %.1fs (%s)", f"{rows:,}", time.monotonic() - t0,
             "full" if window is None else f"{window[0]}..{window[1]}")
    return int(rows or 0)
