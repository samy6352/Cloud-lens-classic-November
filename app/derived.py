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
from contextlib import contextmanager
from typing import Any, Protocol

from psycopg.errors import LockNotAvailable

from . import derived_partitions
from .derived_partitions import RebuildRequired

log = logging.getLogger("cloudlens.derived")
RETIRE_LOCK_TIMEOUT_MS = 5000

# The two names the rows actually live under. The published name is a view naming
# one of them; a full rebuild fills the other and repoints the view. Fixed names
# rather than a staging suffix, so each side keeps its own indexes under its own
# names and nothing is ever renamed.
SIDES = ("_a", "_b")

# Which column each filter-bar dimension names. One map, because two copies of it
# drift: the set of filters a derived table can serve is a property of its grain, and
# working that out per module from the same map means adding a column to one grain
# cannot silently widen what another claims to serve.
#
# Computed dimensions are usable only in grains that explicitly carry them.
FILTER_COLUMNS = {
    "rg": "ResourceGroup",
    "service": "ServiceName",
    "region": "RegionName",
    "charge": "ChargeCategory",
    "pricing": "PricingModel",
    "env": "Environment",
}


def filterable(dimensions: Sequence[str]) -> frozenset[str]:
    """The filter-bar dimensions a grain of `dimensions` can narrow itself by."""
    held = set(dimensions)
    return frozenset(key for key, column in FILTER_COLUMNS.items() if column in held)


def serves_filters(filters: dict[str, list[str]] | None,
                   allowed: frozenset[str]) -> bool:
    """Whether every dimension the caller narrowed is one this grain holds.

    Only dimensions with something actually selected count. An empty list adds no
    predicate, so a filter bar that merely *offers* a dimension does not disqualify
    the derived table until somebody picks a value from it.
    """
    return all(
        key in allowed
        for key, values in (filters or {}).items()
        if any(v and v.strip() for v in (values or []))
    )


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
        "JOIN pg_class t ON t.oid = d.refobjid AND t.relkind IN ('r', 'p') "
        "WHERE n.nspname = ? AND v.relname = ? AND v.relkind = 'v' AND t.oid <> v.oid "
        "LIMIT 1",
        [schema, module.TABLE]).fetchone()
    return row[0] if row else None


def ensure(con: Any, schema: str, module: Any) -> bool:
    """Make the published name a view over one of the two sides, whatever it is now.

    Idempotent, and safe to run on every boot. Three states to land from:

    * Nothing exists yet -- create the `_a` side and the view over it.
    * The name is still a plain table, from before this indirection existed --
      rename it to `_a` in place and put the view over it. The rows are kept: it
      is the current generation's data, and re-deriving it would be a needless
      rebuild on the first boot after an upgrade.
    * The view is already there -- confirm the side it names still exists, and do
      nothing.
    Returns True when existing published contents could not be preserved.
    """
    derived_partitions.enabled()  # Validate configuration without migrating on boot.
    existing = active_table(con, schema, module)
    if existing:
        con.execute(module.table_sql(schema, existing))
        for statement in module.index_sql(schema, existing):
            con.execute(statement)
        return False

    kind = con.execute(
        "SELECT relkind FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace "
        "WHERE n.nspname = ? AND c.relname = ?", [schema, module.TABLE]).fetchone()
    side = f"{module.TABLE}{SIDES[0]}"
    if kind and kind[0] in ("r", "p"):
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
    return not (kind and kind[0] in ("r", "p"))


def validate_storage(con: Any, schema: str, module: Any) -> None:
    """Cheap readiness validation under generation/rebuild locks, without row scans.

    After ensure(), callers should catch RebuildRequired and invalidate the
    rollup generation so the next refresh repairs all data, not just dirty dates.
    """
    active = active_table(con, schema, module)
    if active is None:
        raise RebuildRequired(f"{module.TABLE} has no published side; a full rebuild is required")
    derived_partitions.validate(con, schema, active)


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

    The full rebuild is a `CREATE TABLE AS` rather than a create-then-insert, and
    that is a performance decision with a correctness cost to manage. PostgreSQL
    will not produce a parallel plan for a query that writes data, so
    `INSERT INTO ... SELECT` aggregates the entire detail table on a single core:
    measured on a 5.8M-row estate the planner put the insert form at 37.9M cost
    units against 11.2M for the same select, the difference being four workers it
    was not allowed to use. `CREATE TABLE AS` is not subject to that restriction.

    The cost is that `CREATE TABLE AS` takes its column types from the query rather
    than from `table_sql`, so the select has to produce the declared types itself --
    a `count` is `bigint` until something casts it, while the DDL declares every
    measure `double precision`. That is a property of the modules' SQL rather than
    of this function, and `test_a_full_rebuild_builds_the_declared_column_types`
    is what holds them to it.
    """
    recreated = ensure(con, schema, module)
    select = getattr(module, "build_select_sql", module.select_sql)
    if window is not None:
        if recreated:
            raise RuntimeError(f"{module.TABLE} was recreated; a full rebuild is required")
        active = active_table(con, schema, module)
        if derived_partitions.partitioned(con, schema, active):
            derived_partitions.repair(con, schema, module, active, source, window)
            return active
        con.execute(f'DELETE FROM "{schema}".{active}{_scope(module, window)}')
        con.execute(f'INSERT INTO "{schema}".{active} '
                    f'{select(source, window)}')
        return active

    target = _inactive(con, schema, module)
    # Dropped rather than truncated: a previous rebuild may have been interrupted
    # after building this side but before naming it, and its indexes would then be
    # maintained row by row through the load for nothing.
    derived_partitions.drop_detached(con, schema, target)
    con.execute(f'DROP TABLE IF EXISTS "{schema}".{target}')
    active = active_table(con, schema, module)
    if (derived_partitions.enabled()
            or (active and derived_partitions.partitioned(con, schema, active))):
        derived_partitions.build(con, schema, module, target, source)
        return target
    derived_partitions.forget(con, schema, target)
    con.execute(f'CREATE TABLE "{schema}".{target} AS {select(source, None)}')
    return target


def prepare(con: Any, schema: str, module: Any, staged: str) -> int:
    """Finish every expensive step before taking any published-view locks."""
    for statement in module.index_sql(schema, staged):
        con.execute(statement)
    if derived_partitions.partitioned(con, schema, staged):
        rows = derived_partitions.prepare(con, schema, module, staged)
        if active_table(con, schema, module) != staged:
            con.execute(f'ANALYZE "{schema}".{staged}')
        return rows
    rows = con.execute(f'SELECT count(*) FROM "{schema}".{staged}').fetchone()[0]
    con.execute(f'ANALYZE "{schema}".{staged}')
    return int(rows or 0)


def publish(con: Any, schema: str, module: Any, staged: str) -> None:
    """Repoint the stable view without dropping dependent scoped views."""
    con.execute(f'CREATE OR REPLACE VIEW "{schema}".{module.TABLE} AS '
                f'SELECT * FROM "{schema}".{staged}')


def lock_rebuild(con: Any, schema: str) -> None:
    # Cleanup must not drop the inactive side while another rebuild is filling it.
    con.execute("SELECT pg_advisory_xact_lock(hashtext(?))",
                [f"cloudlens:derived:{schema}"])


@contextmanager
def build_options(con: Any, module: Any):
    """Scope measured module-specific planner choices to its rebuild statements.

    SET LOCAL cannot leak through the connection pool after rollback. Restore on
    success too, so subsequent modules and the caller's outer transaction retain
    their own settings. On an SQL error, the rebuild must roll back before issuing
    more SQL; trying to restore inside the aborted transaction masks the failure.
    """
    previous = {}
    for key, value in getattr(module, "BUILD_SETTINGS", {}).items():
        previous[key] = con.execute("SELECT current_setting(?)", [key]).fetchone()[0]
        con.execute("SELECT set_config(?, ?, true)", [key, value])
    try:
        yield
    except BaseException:
        raise
    else:
        for key, value in previous.items():
            con.execute("SELECT set_config(?, ?, true)", [key, value])


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
    con.execute("BEGIN")
    try:
        con.execute("SELECT set_config('lock_timeout', ?, true)",
                    [f"{RETIRE_LOCK_TIMEOUT_MS}ms"])
        lock_rebuild(con, schema)
        if active_table(con, schema, module) is None:
            raise RuntimeError(f"Cannot retire {module.TABLE}: no published side exists")
        stale = _inactive(con, schema, module)
        derived_partitions.drop_detached(con, schema, stale)
        con.execute(f'DROP TABLE IF EXISTS "{schema}".{stale}')
        derived_partitions.forget(con, schema, stale)
        con.execute("COMMIT")
    except LockNotAvailable:
        con.execute("ROLLBACK")
        log.info("derived: cleanup of %s deferred because a reader or rebuild holds "
                 "a lock; the next full rebuild will reclaim it", module.TABLE)
    except BaseException:
        con.execute("ROLLBACK")
        raise



def rebuild(con: Any, schema: str, modules: Sequence[Any], source: str,
            window: tuple[dt.date, dt.date] | None = None, *,
            cleanup: bool = True,
            windows: Sequence[tuple[dt.date, dt.date]] | None = None) -> dict[str, int]:
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

    Every table is staged, indexed and analysed before any is published. Callers
    mapping COMMIT to a savepoint must pass cleanup=False and retire the old sides
    only after their outer transaction actually commits.

    `windows` replaces several disjoint periods atomically, without touching the
    intervening history. It is mutually exclusive with `window`.

    A module may name SOURCE_TABLE from an earlier module and expand repair
    periods through build_windows. Dependencies are prepared before use, but no
    view is published until every module is complete.
    """
    from .jobs import merge_windows

    if windows is not None and (window is not None or not windows):
        raise ValueError("Supply either one window or a non-empty list of windows")
    ranges = list(windows) if windows is not None else [window]
    full = ranges == [None]
    if not full:
        ranges = merge_windows(ranges)
    plans = []
    seen = set()
    effective_ranges = {}
    for module in modules:
        if module.TABLE in seen:
            raise ValueError(f"Duplicate derived table: {module.TABLE}")
        dependency = getattr(module, "SOURCE_TABLE", None)
        if dependency is not None and dependency not in seen:
            raise ValueError(f"{module.TABLE} requires {dependency} to be built first")
        source_ranges = effective_ranges[dependency] if dependency else ranges
        periods = source_ranges
        if not full and (expand := getattr(module, "build_windows", None)) is not None:
            periods = merge_windows(expand(tuple(source_ranges)))
            if any(not any(start <= lo and stop >= hi for start, stop in periods)
                   for lo, hi in source_ranges):
                raise ValueError(f"{module.TABLE} rebuild periods do not cover the source changes")
        plans.append((module, dependency, periods))
        seen.add(module.TABLE)
        effective_ranges[module.TABLE] = periods
    t0 = time.monotonic()
    con.execute("BEGIN")
    try:
        lock_rebuild(con, schema)
        staged = []
        built = {}
        counts = {}
        for module, dependency, periods in plans:
            module_source = f'"{schema}".{built[dependency]}' if dependency else source
            with build_options(con, module):
                for period in periods:
                    name = stage(con, schema, module, module_source, period)
            counts[module.TABLE] = prepare(con, schema, module, name)
            staged.append((module, name))
            built[module.TABLE] = name
        if full:
            for module, name in staged:
                publish(con, schema, module, name)
        con.execute("COMMIT")
    except BaseException:
        con.execute("ROLLBACK")
        raise

    # After the commit, never inside it: dropping the retired copy waits for
    # readers that are still on it, and the swap's locks must not be held for that.
    if cleanup and full:
        for module in modules:
            retire(con, schema, module)

    scope = "full" if full else ", ".join(f"{lo}..{hi}" for lo, hi in ranges)
    for table, rows in counts.items():
        log.info("%s: %s rows (%s)", table, f"{rows:,}", scope)
    log.info("derived: %s rebuilt in %.1fs (%s)",
             ", ".join(counts), time.monotonic() - t0, scope)
    return counts
