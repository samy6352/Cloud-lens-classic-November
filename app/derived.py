"""How a derived table is replaced, without leaving a second copy of it behind.

The problem
-----------
Both derived tables were replaced with `DELETE` followed by `INSERT` in one
transaction. That is correct -- readers see the old contents or the new, never a
half-written mixture -- but it is not free. PostgreSQL's `DELETE` does not reclaim
anything; it marks every row dead and leaves it in the heap for `VACUUM` to tidy
up afterwards. A full rebuild therefore writes a complete second copy of the table
before the first one can be reused.

Measured on `resource_presence` after one full rebuild of the real estate:

    live tuples        2,781,983
    dead tuples        2,784,069      <- an entire extra copy
    heap                  2288 MB     at 861 B/row, against a real ~430

The heap settles at twice the live size rather than growing without bound, because
autovacuum does return the space to the free space map and the next rebuild reuses
it. Twice is still the wrong number: at the year of retained history this
deployment is sized for, `resource_presence` is around 33M rows, and the transient
doubling is measured in tens of gigabytes.

Why not TRUNCATE
----------------
`TRUNCATE` discards the old heap instead of marking it dead, which fixes the space
exactly. It also takes an `ACCESS EXCLUSIVE` lock and holds it until the
transaction commits -- and the transaction is the whole rebuild, 356 s of it on
the current estate. `DELETE` takes `ROW EXCLUSIVE`, which does not block readers at
all, so swapping one for the other trades a storage problem for an availability
one.

What this does instead
----------------------
A full rebuild builds the replacement in a staging table beside the live one, then
swaps it in. The long part -- scanning 5.8M rows and writing 2.8M -- happens
against a table nothing else can see, so nothing is locked while it runs. Only the
swap itself needs `ACCESS EXCLUSIVE`, and that is a `DROP`, a `RENAME` and the
index builds.

Indexes are created after the swap rather than before it. Two reasons: an index
built on a populated table is faster than one maintained row by row during the
load, and the index ends up with the name the live table's DDL gives it. Creating
them on the staging table would leave them named after the staging table forever,
and the next rebuild would then collide with its own previous output.

A windowed rebuild keeps `DELETE` and `INSERT`. It replaces one month rather than
the whole table, so the dead tuples are bounded by that month and autovacuum
reclaims them between runs; staging the whole table to replace a twelfth of it
would be slower and would lose nothing.

Why the swaps land together
---------------------------
`Warehouse.refresh_rollup` rebuilds both derived tables in one transaction so they
can never describe different generations of `costs`. If each swapped as it
finished, the first table's `ACCESS EXCLUSIVE` lock would be held for the whole of
the second table's build -- 356 s of blocking, which is the problem `TRUNCATE` was
rejected for. `rebuild` therefore stages every table first and publishes them all
at the end, so the locks are taken back to back at the very end of the transaction.
"""
from __future__ import annotations

import datetime as dt
import logging
import time
from collections.abc import Sequence
from typing import Any, Protocol

log = logging.getLogger("cloudlens.derived")

# Suffix for the table a full rebuild is assembled in. Dropped at the start of
# every full rebuild, and rolled back with the transaction on failure, so a crashed
# rebuild cannot leave one behind for the next to trip over.
STAGING_SUFFIX = "_rebuild"


class Derived(Protocol):
    """What this module needs of `rollup` and `presence`.

    Spelled out rather than left implicit because it is the whole contract: a
    derived table is a name, a date column, DDL that can be pointed at any table
    name, and a query that computes its contents from a source relation.
    """

    TABLE: str
    PERIOD: str

    def table_sql(self, schema: str, table: str) -> str: ...
    def index_sql(self, schema: str, table: str) -> list[str]: ...
    def select_sql(self, source: str,
                   window: tuple[dt.date, dt.date] | None = None) -> str: ...


def _scope(module: Any, window: tuple[dt.date, dt.date]) -> str:
    lo, hi = window
    return (f' WHERE "{module.PERIOD}" >= DATE \'{lo.isoformat()}\''
            f' AND "{module.PERIOD}" < DATE \'{hi.isoformat()}\'')


def stage(con: Any, schema: str, module: Any, source: str,
          window: tuple[dt.date, dt.date] | None) -> str:
    """Write the new contents, and return the relation now holding them.

    Windowed: in place, because only a bounded span is being replaced. Full: into a
    staging table, which `publish` swaps in. Either way the caller must go on to
    call `publish`, which is what makes the rebuild visible and countable.
    """
    if window is not None:
        con.execute(f'DELETE FROM "{schema}".{module.TABLE}{_scope(module, window)}')
        con.execute(f'INSERT INTO "{schema}".{module.TABLE} '
                    f'{module.select_sql(source, window)}')
        return module.TABLE

    staging = f"{module.TABLE}{STAGING_SUFFIX}"
    con.execute(f'DROP TABLE IF EXISTS "{schema}".{staging}')
    con.execute(module.table_sql(schema, staging))
    con.execute(f'INSERT INTO "{schema}".{staging} {module.select_sql(source, None)}')
    return staging


def publish(con: Any, schema: str, module: Any, staged: str) -> int:
    """Put a staged rebuild in place, and leave the planner able to use the result.

    `ANALYZE` is not optional. Without current statistics the planner may still
    choose a sequential scan of the detail table for a query the derived table
    could have answered, which is the entire cost this table exists to avoid --
    and a freshly swapped table has no statistics at all.
    """
    if staged != module.TABLE:
        con.execute(f'DROP TABLE IF EXISTS "{schema}".{module.TABLE}')
        con.execute(f'ALTER TABLE "{schema}".{staged} RENAME TO {module.TABLE}')
        for statement in module.index_sql(schema, module.TABLE):
            con.execute(statement)

    rows = con.execute(f'SELECT count(*) FROM "{schema}".{module.TABLE}').fetchone()[0]
    con.execute(f'ANALYZE "{schema}".{module.TABLE}')
    return int(rows or 0)


def rebuild(con: Any, schema: str, modules: Sequence[Any], source: str,
            window: tuple[dt.date, dt.date] | None = None) -> dict[str, int]:
    """Rebuild every derived table in `modules` from `source`, in one transaction.

    A rebuild rather than an incremental merge. These tables are derived data whose
    only job is to agree with the table they came from, and a rebuild cannot drift:
    there is no partial-failure state where some rows were updated and others were
    not, and no need to work out which individual rows an ingest touched.

    `window` narrows that rebuild to a half-open `[lo, hi)` span of charge dates.
    The property above is preserved *within the window*, because everything in it is
    deleted and recomputed from source -- nothing is merged. What the caller takes on
    is the obligation to pass a window that actually covers every day the write
    touched; days outside it keep whatever the last rebuild produced. A caller that
    cannot state that with certainty must pass `None` and rebuild the lot, which is
    why that remains the default.

    This matters at retention scale. A full rebuild costs one scan of all retained
    history, and at a year of daily ingestion that is the same scan repeated every
    day to correct one day's rows. A windowed rebuild scans the month that changed,
    and because the window is snapped to month boundaries it prunes to the matching
    `costs_fact` partitions instead of reading the rest of the year to discard it.

    Every table is staged before any is published, so that the exclusive locks the
    swaps need are taken together at the end rather than one being held across
    another table's build. One transaction, so the tables can never describe
    different generations of the source, and readers see all the old contents or
    all the new.
    """
    t0 = time.monotonic()
    con.execute("BEGIN")
    try:
        staged = [(module, stage(con, schema, module, source, window))
                  for module in modules]
        counts = {module.TABLE: publish(con, schema, module, name)
                  for module, name in staged}
        con.execute("COMMIT")
    except Exception:
        con.execute("ROLLBACK")
        raise

    scope = "full" if window is None else f"{window[0]}..{window[1]}"
    for table, rows in counts.items():
        log.info("%s: %s rows (%s)", table, f"{rows:,}", scope)
    log.info("derived: %s rebuilt in %.1fs (%s)",
             ", ".join(counts), time.monotonic() - t0, scope)
    return counts
