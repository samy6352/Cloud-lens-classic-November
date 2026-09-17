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
A full rebuild builds the replacement in the *inactive* one of two tables beside
the live one, then repoints a view at it. The long part -- scanning 5.8M rows and
writing 2.8M -- happens against a table nothing else can see, so nothing is locked
while it runs. Only the repoint needs an exclusive lock, and that is a catalogue
update on a view rather than anything touching data.

Why a view rather than a rename
-------------------------------
The published name is a view, and readers bind to it rather than to the table
holding the rows. That is not indirection for its own sake: it is what lets the
rebuild finish at all.

`Warehouse._shadow_rollup` and `_shadow_presence` give every scoped connection a
temp view of the same name, so an unqualified read cannot see outside the caller's
subscriptions. A temp view referring to a table is a *dependency* on it, recorded
in the catalogue, and PostgreSQL refuses to drop a table anything still depends
on. Swapping by `DROP` and `RENAME` therefore failed whenever a dashboard was
being read:

    cannot drop table costs_rollup because other objects depend on it
    DETAIL:  view pg_temp_52.costs_rollup depends on table costs_rollup

The rebuild logged that and kept the previous generation, so nothing broke
visibly and the data simply stopped being refreshed -- a silent freshness
failure, and the worst kind, because the dashboard went on answering confidently
with figures that were quietly getting older. It also failed *more* the better
things were working, since the shadow only refers to the derived table on the
fast path: the one taken when the rollup is fresh enough to be worth reading.

With a view in the middle, the shadows depend on the view. `CREATE OR REPLACE
VIEW` redefines it without dropping it, so those dependencies survive the swap,
and the retired table -- which nothing refers to any more -- can then be dropped.
Readers that were already running keep reading the table they started on.

Two fixed names rather than a staging suffix, because each side keeps its own
indexes under its own names. Nothing has to be renamed, so nothing has to be
renamed back after a failure.

A windowed rebuild keeps `DELETE` and `INSERT`, applied to the active table
directly. It replaces one month rather than the whole table, so the dead tuples
are bounded by that month and autovacuum reclaims them between runs; staging the
whole table to replace a twelfth of it would be slower and would lose nothing.
The active table is addressed explicitly rather than through the view: a view
this simple is auto-updatable, so writing through it would appear to work and
would hide which copy was actually being changed.

Why the swaps land together
---------------------------
`Warehouse.refresh_rollup` rebuilds both derived tables in one transaction so they
can never describe different generations of `costs`. If each swapped as it
finished, the first table's lock would be held for the whole of the second
table's build. `rebuild` therefore stages every table first and publishes them all
at the end, so the swaps are taken back to back at the very end of the
transaction.

Retiring the old copy happens after that transaction commits, not inside it. A
reader part-way through a query still holds the table it started on, and `DROP`
would wait for it -- inside the transaction that would mean holding the swap's
locks for as long as the slowest reader. Outside it, with a short `lock_timeout`,
the space comes back immediately in the ordinary case and waits for the next
rebuild in the rare one. It cannot accumulate: with two names, the next full
rebuild drops whichever side it is about to build into.
"""
from __future__ import annotations

import datetime as dt
import logging
import time
from collections.abc import Sequence
from typing import Any, Protocol

log = logging.getLogger("cloudlens.derived")

# The two names the rows actually live under. The published name is a view naming
# one of them; a full rebuild fills the other and repoints the view. Fixed names
# rather than a staging suffix, so each side keeps its own indexes under its own
# names and nothing is ever renamed.
SIDES = ("_a", "_b")


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


def active_table(con: Any, schema: str, module: Any) -> str | None:
    """Which side the published view currently names, or None if there is no view.

    Read from the catalogue rather than tracked in a column of our own. A stored
    marker is a second source of truth that can disagree with the view after a
    crash between the swap and the marker write; `pg_depend` cannot, because it is
    the same record PostgreSQL uses to decide what may be dropped.
    """
    row = con.execute(
        "SELECT t.relname FROM pg_class v "
        "JOIN pg_namespace n ON n.oid = v.relnamespace "
        "JOIN pg_rewrite r ON r.ev_class = v.oid "
        "JOIN pg_depend d ON d.objid = r.oid AND d.classid = 'pg_rewrite'::regclass "
        "JOIN pg_class t ON t.oid = d.refobjid AND t.relkind = 'r' "
        "WHERE n.nspname = ? AND v.relname = ? AND v.relkind = 'v' AND t.oid <> v.oid "
        "LIMIT 1",
        [schema, module.TABLE]).fetchone()
    return row[0] if row else None


def ensure(con: Any, schema: str, module: Any) -> None:
    """Make the published name a view over one of the two sides, whatever it is now.

    Idempotent, and safe to run on every boot. Three states to land from:

    * Nothing exists yet -- create the `_a` side and the view over it.
    * The name is still a plain table, from before this indirection existed --
      rename it to `_a` in place and put the view over it. The rows are kept: it
      is the current generation's data, and re-deriving it would be a needless
      rebuild on the first boot after an upgrade.
    * The view is already there -- confirm the side it names still exists, and do
      nothing.
    """
    existing = active_table(con, schema, module)
    if existing:
        con.execute(module.table_sql(schema, existing))
        for statement in module.index_sql(schema, existing):
            con.execute(statement)
        return

    kind = con.execute(
        "SELECT relkind FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace "
        "WHERE n.nspname = ? AND c.relname = ?", [schema, module.TABLE]).fetchone()
    side = f"{module.TABLE}{SIDES[0]}"
    if kind and kind[0] == "r":
        # Upgrade in place. The indexes come with the table and keep their old
        # names; `index_sql` is IF NOT EXISTS against the new name, so the next
        # rebuild creates the correctly-named ones on the other side and this
        # copy's stragglers leave with it when it is retired.
        con.execute(f'ALTER TABLE "{schema}".{module.TABLE} RENAME TO {side}')
    elif kind and kind[0] == "v":
        # A view that depends on nothing we recognise -- an empty or hand-made
        # definition. Replaced below.
        pass
    con.execute(module.table_sql(schema, side))
    for statement in module.index_sql(schema, side):
        con.execute(statement)
    con.execute(f'CREATE OR REPLACE VIEW "{schema}".{module.TABLE} AS '
                f'SELECT * FROM "{schema}".{side}')


def _inactive(con: Any, schema: str, module: Any) -> str:
    """The side a full rebuild should be assembled in: whichever is not published."""
    active = active_table(con, schema, module)
    first, second = (f"{module.TABLE}{suffix}" for suffix in SIDES)
    return second if active == first else first


def _scope(module: Any, window: tuple[dt.date, dt.date]) -> str:
    lo, hi = window
    return (f' WHERE "{module.PERIOD}" >= DATE \'{lo.isoformat()}\''
            f' AND "{module.PERIOD}" < DATE \'{hi.isoformat()}\'')


def stage(con: Any, schema: str, module: Any, source: str,
          window: tuple[dt.date, dt.date] | None) -> str:
    """Write the new contents, and return the table now holding them.

    Windowed: in place on the active side, because only a bounded span is being
    replaced. Full: into the inactive side, which `publish` then names. Either way
    the caller must go on to call `publish`, which is what makes the rebuild
    visible and countable.

    Both address a table directly rather than the published view. The view is
    auto-updatable, so writing through it would silently succeed while leaving
    which copy was modified up to PostgreSQL -- correct today and quietly wrong
    the moment the definition grows past a bare `SELECT *`.
    """
    ensure(con, schema, module)
    if window is not None:
        active = active_table(con, schema, module)
        con.execute(f'DELETE FROM "{schema}".{active}{_scope(module, window)}')
        con.execute(f'INSERT INTO "{schema}".{active} '
                    f'{module.select_sql(source, window)}')
        return active

    target = _inactive(con, schema, module)
    # Dropped rather than truncated: a previous rebuild may have been interrupted
    # after building this side but before naming it, and its indexes would then be
    # maintained row by row through the load for nothing.
    con.execute(f'DROP TABLE IF EXISTS "{schema}".{target}')
    con.execute(module.table_sql(schema, target))
    con.execute(f'INSERT INTO "{schema}".{target} {module.select_sql(source, None)}')
    return target


def publish(con: Any, schema: str, module: Any, staged: str) -> int:
    """Name the staged side from the published view, so readers start seeing it.

    The swap is `CREATE OR REPLACE VIEW`, which redefines the view without
    dropping it. That distinction is the whole point: the temp views carrying each
    connection's subscription scope depend on this view, and dropping it would be
    refused while any dashboard was being read.

    Indexes are created before the swap, not after. The staged side is not
    published yet, so nothing can read it and the build blocks nobody -- and an
    index built on a populated table is cheaper than one maintained row by row
    during the load. They are named after the side they belong to, so the two
    copies never collide.

    `ANALYZE` is not optional. Without current statistics the planner may still
    choose a sequential scan of the detail table for a query the derived table
    could have answered, which is the entire cost this table exists to avoid --
    and a freshly built side has no statistics at all.
    """
    for statement in module.index_sql(schema, staged):
        con.execute(statement)
    rows = con.execute(f'SELECT count(*) FROM "{schema}".{staged}').fetchone()[0]
    con.execute(f'ANALYZE "{schema}".{staged}')
    con.execute(f'CREATE OR REPLACE VIEW "{schema}".{module.TABLE} AS '
                f'SELECT * FROM "{schema}".{staged}')
    return int(rows or 0)


def retire(con: Any, schema: str, module: Any) -> None:
    """Drop the side the published view no longer names.

    Called after the rebuild transaction commits. A reader that started before the
    swap still holds the old table and `DROP` waits for it; inside the transaction
    that would mean holding the swap's locks for as long as the slowest reader.

    `lock_timeout` rather than an unbounded wait, and failure is logged rather
    than raised: the rebuild has already succeeded and is already visible, so a
    copy that could not be dropped is wasted space and nothing worse. It cannot
    accumulate -- the next full rebuild drops whichever side it builds into.
    """
    stale = _inactive(con, schema, module)
    try:
        con.execute("SET lock_timeout = '5s'")
        con.execute(f'DROP TABLE IF EXISTS "{schema}".{stale}')
    except Exception as exc:  # noqa: BLE001 - space, not correctness
        log.info("derived: %s is still being read and was left in place (%s); "
                 "the next full rebuild will reclaim it", stale, str(exc)[:120])
    finally:
        try:
            con.execute("RESET lock_timeout")
        except Exception:  # noqa: BLE001
            log.debug("derived: could not reset lock_timeout", exc_info=True)



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

    # After the commit, never inside it: dropping the retired copy waits for
    # readers that are still on it, and the swap's locks must not be held for that.
    if window is None:
        for module in modules:
            retire(con, schema, module)

    scope = "full" if window is None else f"{window[0]}..{window[1]}"
    for table, rows in counts.items():
        log.info("%s: %s rows (%s)", table, f"{rows:,}", scope)
    log.info("derived: %s rebuilt in %.1fs (%s)",
             ", ".join(counts), time.monotonic() - t0, scope)
    return counts
