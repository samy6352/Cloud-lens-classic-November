"""The v2 physical design: a partitioned fact table behind dimension tables.

Why
---
`costs` in v1 is one flat table of 37 columns, most of them text, most of them
repeated on every row. A single resource billed daily for a month stores its
resource id, resource name, resource group, subscription name and region thirty
times over, and its tag blob thirty times with it. On the measured customer
shape -- 5.8M rows in one month, 2.03M distinct resource identities -- that is
the dominant cost of the table, and it grows linearly with retention.

It also has no cheap way to forget a month. Retention on a flat table is a
`DELETE` that has to find the rows, write them all to the WAL, and leave the
space to `VACUUM`. On a partitioned table it is a `DROP TABLE` of one partition:
constant time, no bloat.

What this builds
----------------
    dim_resource     one row per distinct resource identity tuple
    dim_tagset       one row per distinct tag blob
    costs_fact       narrow, RANGE-partitioned by month, holding keys + money
    costs            a VIEW presenting the original 37 columns

The view is the point. Every reader in the application -- the dashboard, the
rollup rebuild, the scope view, model-authored SQL through `query()` -- names
`costs` and selects the same columns it always did. None of them need to know
the rows are now assembled from three tables, so the UI and the query layer stay
untouched by construction rather than by careful editing.

Writes work unchanged too: an INSTEAD OF INSERT trigger resolves the dimension
rows and writes the fact. That matters because the ingest path streams batches
with `COPY ... FROM STDIN`, and PostgreSQL permits COPY into a view precisely
when it has an INSTEAD OF INSERT trigger.

Dedupe on the tuple, not on an assumption
-----------------------------------------
`dim_resource` is keyed on the *exact combination* of its columns, not on
`ResourceId` alone. That distinction is load-bearing. Azure does emit rows that
share a resource id while differing in region or subscription spelling, and a
dimension keyed on the id alone would silently discard one of them. Keying on
the whole tuple cannot lose information whatever the data does: worst case the
dimension is as large as the fact and the design merely stops helping.

Nothing here deletes, rewrites or migrates an existing v1 table. The v2 database
is new, so the schema is simply built this way from the start.
"""
from __future__ import annotations

import datetime as dt
import logging
from typing import Any, Iterable, Sequence

log = logging.getLogger("cloudlens.schema_v2")

FACT = "costs_fact"
VIEW = "costs"
DIM_RESOURCE = "dim_resource"
DIM_TAGSET = "dim_tagset"

# The resource identity tuple. These are the widest columns in the table and the
# ones that repeat on every daily charge for the same resource, which is what
# makes them worth storing once.
#
# Meter and product names are deliberately NOT here. They vary per charge within
# a single resource, so folding them in would multiply the dimension's row count
# toward the fact's own and give back the saving. They stay on the fact.
RESOURCE_COLUMNS: tuple[str, ...] = (
    "ResourceId",
    "ResourceName",
    "ResourceGroup",
    "SubAccountId",
    "SubAccountName",
    "RegionName",
)

# The tag blob, stored once per distinct value. One column rather than several
# because Azure hands it over as a single serialized string and the application
# treats it as one.
TAGSET_COLUMNS: tuple[str, ...] = ("Tags",)


def fact_columns(columns: Iterable[str]) -> list[str]:
    """The columns that stay on the fact table, in the warehouse's own order."""
    moved = set(RESOURCE_COLUMNS) | set(TAGSET_COLUMNS)
    return [c for c in columns if c not in moved]


def _sql_type(column: str, numeric: set[str]) -> str:
    if column == "ChargePeriodStart":
        return "date"
    return "double precision" if column in numeric else "text"


def month_start(day: dt.date) -> dt.date:
    return day.replace(day=1)


def next_month(day: dt.date) -> dt.date:
    return (day.replace(day=1) + dt.timedelta(days=32)).replace(day=1)


def partition_name(day: dt.date) -> str:
    return f"{FACT}_{day:%Y%m}"


def _tuple_key(alias: str, columns: Sequence[str]) -> str:
    """A NULL-safe hash of a dimension tuple, as a SQL expression.

    Needed because the natural join condition -- `IS NOT DISTINCT FROM` across
    every column -- is not hashable, so PostgreSQL can only answer it with a
    nested loop. On a batch of any size that is the difference between a bulk
    load and a stall: measured at 60k rows it made the set-based promote *slower*
    than the per-row trigger it was meant to replace.

    Hashing a serialization of the tuple gives the planner something it can hash
    join on. The separator is a control character rather than a comma so that
    ('a','b') and ('a,b', NULL) cannot serialize alike, and NULL is rendered as a
    distinct marker so it is never confused with an empty string.
    """
    parts = " || E'\\x1f' || ".join(
        f"""coalesce({alias + "." if alias else ""}"{c}", E'\\x1e')""" for c in columns)
    return f"md5({parts})"


def create_sql(schema: str, columns: Sequence[str], numeric: set[str]) -> list[str]:
    """DDL for the whole v2 shape, safe to run repeatedly.

    Ordered so each statement only depends on what precedes it: dimensions, then
    the partitioned fact, then its default partition, then the view and the
    trigger that makes the view writable.
    """
    q = f'"{schema}"'
    kept = fact_columns(columns)

    res_cols = ",\n  ".join(f'"{c}" text' for c in RESOURCE_COLUMNS)
    tag_cols = ",\n  ".join(f'"{c}" text' for c in TAGSET_COLUMNS)
    fact_cols = ",\n  ".join(f'"{c}" {_sql_type(c, numeric)}' for c in kept)

    statements: list[str] = [
        # Dimensions. The surrogate key is an identity column rather than the
        # tuple itself: the fact stores one bigint per row instead of six text
        # values, which is the entire saving.
        #
        # `tuple_key` is a stored hash of the tuple, maintained by PostgreSQL so
        # it can never disagree with the columns it summarizes. It exists purely
        # to give the bulk loader something hashable to join on.
        f"""CREATE TABLE IF NOT EXISTS {q}.{DIM_RESOURCE} (
  resource_key bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  {res_cols},
  tuple_key text GENERATED ALWAYS AS ({_tuple_key("", RESOURCE_COLUMNS)}) STORED
)""",
        f"""CREATE TABLE IF NOT EXISTS {q}.{DIM_TAGSET} (
  tagset_key bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  {tag_cols},
  tuple_key text GENERATED ALWAYS AS ({_tuple_key("", TAGSET_COLUMNS)}) STORED
)""",
        # The lookup the trigger does on every insert, and the guarantee that a
        # tuple is never stored twice. NULLS NOT DISTINCT is required, not
        # cosmetic: most of these columns are nullable, and under the default
        # rule two identical all-NULL tuples would each be considered unique and
        # the dimension would grow without bound.
        f'CREATE UNIQUE INDEX IF NOT EXISTS {DIM_RESOURCE}_tuple_idx '
        f'ON {q}.{DIM_RESOURCE} ({", ".join(chr(34) + c + chr(34) for c in RESOURCE_COLUMNS)}) '
        f'NULLS NOT DISTINCT',
        f'CREATE UNIQUE INDEX IF NOT EXISTS {DIM_TAGSET}_tuple_idx '
        f'ON {q}.{DIM_TAGSET} ({", ".join(chr(34) + c + chr(34) for c in TAGSET_COLUMNS)}) '
        f'NULLS NOT DISTINCT',
        f'CREATE INDEX IF NOT EXISTS {DIM_RESOURCE}_key_idx '
        f'ON {q}.{DIM_RESOURCE} (tuple_key)',
        f'CREATE INDEX IF NOT EXISTS {DIM_TAGSET}_key_idx '
        f'ON {q}.{DIM_TAGSET} (tuple_key)',
        # The fact. Partitioned by month on the date every query already filters
        # on, so a one-month dashboard window reads one partition and a retention
        # cutoff is a DROP rather than a DELETE.
        f"""CREATE TABLE IF NOT EXISTS {q}.{FACT} (
  {fact_cols},
  resource_key bigint NOT NULL REFERENCES {q}.{DIM_RESOURCE}(resource_key),
  tagset_key bigint NOT NULL REFERENCES {q}.{DIM_TAGSET}(tagset_key)
) PARTITION BY RANGE ("ChargePeriodStart")""",
        # A row whose month has no partition yet would be rejected outright.
        # Ingest creates months ahead of time, but a default partition means the
        # failure mode of missing one is a slower query, never a lost charge.
        f'CREATE TABLE IF NOT EXISTS {q}.{FACT}_default '
        f'PARTITION OF {q}.{FACT} DEFAULT',
        # Local indexes, created on the parent so every partition inherits them.
        f'CREATE INDEX IF NOT EXISTS {FACT}_period_idx '
        f'ON {q}.{FACT} ("ChargePeriodStart")',
        f'CREATE INDEX IF NOT EXISTS {FACT}_resource_idx '
        f'ON {q}.{FACT} (resource_key)',
    ]

    statements.extend(_view_sql(schema, columns))
    return statements


def _view_sql(schema: str, columns: Sequence[str]) -> list[str]:
    """The compatibility view and the trigger that lets it be written to."""
    q = f'"{schema}"'
    kept = set(fact_columns(columns))

    # Projected in the warehouse's own column order so `SELECT *` returns what it
    # always did, whichever table each value now physically lives in.
    projection = []
    for c in columns:
        if c in kept:
            projection.append(f'f."{c}"')
        elif c in RESOURCE_COLUMNS:
            projection.append(f'r."{c}"')
        else:
            projection.append(f't."{c}"')
    select = ",\n  ".join(projection)

    assign_res = " AND ".join(
        f'd."{c}" IS NOT DISTINCT FROM NEW."{c}"' for c in RESOURCE_COLUMNS)
    assign_tag = " AND ".join(
        f'd."{c}" IS NOT DISTINCT FROM NEW."{c}"' for c in TAGSET_COLUMNS)
    res_names = ", ".join(f'"{c}"' for c in RESOURCE_COLUMNS)
    res_values = ", ".join(f'NEW."{c}"' for c in RESOURCE_COLUMNS)
    tag_names = ", ".join(f'"{c}"' for c in TAGSET_COLUMNS)
    tag_values = ", ".join(f'NEW."{c}"' for c in TAGSET_COLUMNS)

    fact_names = ", ".join(f'"{c}"' for c in fact_columns(columns))
    fact_values = ", ".join(f'NEW."{c}"' for c in fact_columns(columns))

    return [
        f"""CREATE OR REPLACE VIEW {q}.{VIEW} AS
SELECT
  {select}
FROM {q}.{FACT} f
JOIN {q}.{DIM_RESOURCE} r ON r.resource_key = f.resource_key
JOIN {q}.{DIM_TAGSET} t ON t.tagset_key = f.tagset_key""",
        # Resolve-or-create, then write the fact.
        #
        # The insert is attempted before the select rather than after. Under
        # concurrent ingest the obvious order -- look, then insert if absent --
        # has a window between the two in which another session inserts the same
        # tuple, and the loser gets a unique violation instead of a row. ON
        # CONFLICT DO NOTHING closes it; the follow-up SELECT then finds the
        # tuple whether this session or the other one created it.
        f"""CREATE OR REPLACE FUNCTION {q}.{VIEW}_insert() RETURNS trigger
LANGUAGE plpgsql AS $$
DECLARE
  v_resource bigint;
  v_tagset bigint;
BEGIN
  INSERT INTO {q}.{DIM_RESOURCE} ({res_names})
  VALUES ({res_values})
  ON CONFLICT DO NOTHING;
  SELECT d.resource_key INTO v_resource
  FROM {q}.{DIM_RESOURCE} d WHERE {assign_res};

  INSERT INTO {q}.{DIM_TAGSET} ({tag_names})
  VALUES ({tag_values})
  ON CONFLICT DO NOTHING;
  SELECT d.tagset_key INTO v_tagset
  FROM {q}.{DIM_TAGSET} d WHERE {assign_tag};

  INSERT INTO {q}.{FACT} ({fact_names}, resource_key, tagset_key)
  VALUES ({fact_values}, v_resource, v_tagset);
  RETURN NEW;
END;
$$""",
        f'DROP TRIGGER IF EXISTS {VIEW}_insert_trg ON {q}.{VIEW}',
        f'CREATE TRIGGER {VIEW}_insert_trg INSTEAD OF INSERT ON {q}.{VIEW} '
        f'FOR EACH ROW EXECUTE FUNCTION {q}.{VIEW}_insert()',
        # DELETE has to work because the ingest clears a period before reloading
        # it. Dimension rows are deliberately left behind: they are small, they
        # are almost certainly about to be referenced again by the reload, and
        # removing them would mean proving no other partition still points at
        # them.
        f"""CREATE OR REPLACE FUNCTION {q}.{VIEW}_delete() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
  DELETE FROM {q}.{FACT} f
  WHERE f.ctid = (
    SELECT f2.ctid FROM {q}.{FACT} f2
    JOIN {q}.{DIM_RESOURCE} r ON r.resource_key = f2.resource_key
    WHERE f2."ChargePeriodStart" IS NOT DISTINCT FROM OLD."ChargePeriodStart"
      AND r."ResourceId" IS NOT DISTINCT FROM OLD."ResourceId"
      AND f2."BilledCost" IS NOT DISTINCT FROM OLD."BilledCost"
    LIMIT 1
  );
  RETURN OLD;
END;
$$""",
        f'DROP TRIGGER IF EXISTS {VIEW}_delete_trg ON {q}.{VIEW}',
        f'CREATE TRIGGER {VIEW}_delete_trg INSTEAD OF DELETE ON {q}.{VIEW} '
        f'FOR EACH ROW EXECUTE FUNCTION {q}.{VIEW}_delete()',
    ]


# --- bulk load ---------------------------------------------------------------
#
# The INSTEAD OF trigger above makes the view behave exactly like the old table,
# which is what keeps every existing reader and writer working. It is also, by
# construction, a per-row function call that does two lookups and three inserts,
# and measurement put it at roughly 50x the cost of a plain COPY.
#
# That is tolerable for a handful of rows and completely unusable for a seed
# ingest of several million. So ingest does the same work set-based: stream the
# batch into an unlogged staging table with one ordinary COPY, resolve both
# dimensions with two statements over the whole batch, then insert the facts with
# a join. Same rows, same dimension contents, a fraction of the time.
#
# The trigger is not removed in favour of this. It is the correctness boundary --
# anything that writes to `costs` without knowing about this module still lands
# in the right place, just slowly.

STAGING = "costs_stage"


def staging_sql(schema: str, columns: Sequence[str], numeric: set[str]) -> list[str]:
    """An unlogged staging table shaped exactly like the old flat `costs`.

    UNLOGGED because its entire contents are rewritten into the fact inside the
    same ingest and it is truncated afterwards; paying WAL for a scratch table
    buys durability nobody wants. Its rows are not customer state -- the source
    report is.
    """
    cols = ",\n  ".join(f'"{c}" {_sql_type(c, numeric)}' for c in columns)
    return [
        f'CREATE UNLOGGED TABLE IF NOT EXISTS "{schema}".{STAGING} (\n  {cols}\n)',
        f'TRUNCATE "{schema}".{STAGING}',
    ]


def promote_sql(schema: str, columns: Sequence[str]) -> list[str]:
    """Move everything in staging into the dimensions and the fact.

    The two dimension inserts come first and are idempotent, so a batch that
    reuses resources already seen adds nothing. `DISTINCT` is applied before the
    insert rather than relying on ON CONFLICT alone: without it the statement can
    try to insert the same new tuple twice within one command, which ON CONFLICT
    does not protect against.
    """
    q = f'"{schema}"'
    kept = fact_columns(columns)

    res_names = ", ".join(f'"{c}"' for c in RESOURCE_COLUMNS)
    tag_names = ", ".join(f'"{c}"' for c in TAGSET_COLUMNS)
    fact_names = ", ".join(f'"{c}"' for c in kept)
    fact_values = ", ".join(f's."{c}"' for c in kept)
    join_res = " AND ".join(
        f'r."{c}" IS NOT DISTINCT FROM s."{c}"' for c in RESOURCE_COLUMNS)
    join_tag = " AND ".join(
        f't."{c}" IS NOT DISTINCT FROM s."{c}"' for c in TAGSET_COLUMNS)

    return [
        f'INSERT INTO {q}.{DIM_RESOURCE} ({res_names}) '
        f'SELECT DISTINCT {res_names} FROM {q}.{STAGING} '
        f'ON CONFLICT DO NOTHING',
        f'INSERT INTO {q}.{DIM_TAGSET} ({tag_names}) '
        f'SELECT DISTINCT {tag_names} FROM {q}.{STAGING} '
        f'ON CONFLICT DO NOTHING',
        # Joined on the hash so the planner can hash join, then confirmed on the
        # full tuple so a hash collision cannot attach a charge to the wrong
        # resource. The exact comparison only ever runs against the handful of
        # rows already in the matching bucket, so it costs almost nothing.
        f'INSERT INTO {q}.{FACT} ({fact_names}, resource_key, tagset_key) '
        f'SELECT {fact_values}, r.resource_key, t.tagset_key '
        f'FROM {q}.{STAGING} s '
        f'JOIN {q}.{DIM_RESOURCE} r '
        f'  ON r.tuple_key = {_tuple_key("s", RESOURCE_COLUMNS)} AND {join_res} '
        f'JOIN {q}.{DIM_TAGSET} t '
        f'  ON t.tuple_key = {_tuple_key("s", TAGSET_COLUMNS)} AND {join_tag}',
        f'TRUNCATE {q}.{STAGING}',
    ]


def total_size(con: Any, schema: str) -> dict[str, int]:
    """Bytes held by the v2 shape, counting partitions.

    `pg_total_relation_size` on a partitioned parent reports only the parent's
    own (empty) storage, so asking it about `costs_fact` returns zero however
    many rows are underneath. The partitions have to be summed explicitly, which
    is the kind of detail that otherwise produces a triumphant and completely
    fictional size reduction.
    """
    fact = con.execute(
        "SELECT coalesce(sum(pg_total_relation_size(c.oid)), 0) "
        "FROM pg_class c "
        "JOIN pg_inherits i ON i.inhrelid = c.oid "
        "JOIN pg_class p ON p.oid = i.inhparent "
        "JOIN pg_namespace n ON n.oid = p.relnamespace "
        "WHERE p.relname = ? AND n.nspname = ?",
        [FACT, schema],
    ).fetchone()[0]
    dims = {}
    for rel in (DIM_RESOURCE, DIM_TAGSET):
        dims[rel] = con.execute(
            "SELECT pg_total_relation_size(?)", [f'"{schema}".{rel}']
        ).fetchone()[0]
    return {FACT: int(fact), **{k: int(v) for k, v in dims.items()}}


def ensure_partitions(con: Any, schema: str, first: dt.date, last: dt.date) -> list[str]:
    """Create the monthly partitions covering [first, last], skipping any that exist.

    Called by ingest before loading rather than from a trigger. Creating a
    partition takes a lock on the parent table, and taking that lock in the
    middle of a COPY of several hundred thousand rows is a good way to stall
    every reader on the database.
    """
    created: list[str] = []
    cursor = month_start(first)
    stop = month_start(last)
    while cursor <= stop:
        name = partition_name(cursor)
        upper = next_month(cursor)
        con.execute(
            f'CREATE TABLE IF NOT EXISTS "{schema}".{name} '
            f'PARTITION OF "{schema}".{FACT} '
            f"FOR VALUES FROM ('{cursor:%Y-%m-%d}') TO ('{upper:%Y-%m-%d}')"
        )
        created.append(name)
        cursor = upper
    return created


def attached_partitions(con: Any, schema: str) -> list[str]:
    """Partition names currently attached to the fact, oldest first."""
    rows = con.execute(
        "SELECT c.relname FROM pg_class c "
        "JOIN pg_inherits i ON i.inhrelid = c.oid "
        "JOIN pg_class p ON p.oid = i.inhparent "
        "JOIN pg_namespace n ON n.oid = p.relnamespace "
        "WHERE p.relname = ? AND n.nspname = ? ORDER BY c.relname",
        [FACT, schema],
    ).fetchall()
    return [r[0] for r in rows]


def drop_partitions_before(con: Any, schema: str, cutoff: dt.date) -> list[str]:
    """Forget whole months in constant time.

    The reason the fact is partitioned at all. Retention on the flat table meant
    a DELETE that wrote every removed row to the WAL and left the space for
    VACUUM to reclaim; here it is a DROP of one relation.

    The default partition is never dropped -- it may hold rows from any month,
    including months on the right side of the cutoff.
    """
    dropped: list[str] = []
    boundary = f"{month_start(cutoff):%Y%m}"
    for name in attached_partitions(con, schema):
        if name.endswith("_default"):
            continue
        stamp = name.rsplit("_", 1)[-1]
        if len(stamp) == 6 and stamp.isdigit() and stamp < boundary:
            con.execute(f'DROP TABLE "{schema}".{name}')
            dropped.append(name)
    if dropped:
        log.info("schema_v2: dropped %d partition(s) before %s", len(dropped), boundary)
    return dropped
