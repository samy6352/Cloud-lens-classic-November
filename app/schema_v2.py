"""The v2 physical design: `costs`, partitioned by month.

Why
---
`costs` in v1 is one flat table of 37 columns holding every daily charge for
every resource, and it has no cheap way to forget a month. Retention on it is a
`DELETE` that has to find the rows, write every one of them to the WAL, and then
leave the space behind for `VACUUM` to reclaim. Measured locally at 1.5M rows:

    DELETE one month     15,481 ms   492.1 MB -> 492.1 MB   (nothing reclaimed)
    DROP one partition      114 ms   493.8 MB -> 408.7 MB

At a year of daily ingestion that is the difference between a table that holds
steady and one that grows without bound.

What this builds
----------------
    costs        the same 37 columns as v1, RANGE-partitioned by month

That is the whole design. There is no dimension table, no surrogate key, no
compatibility view and no write trigger, because every reader in the application
already names `costs` and `costs` is still a relation with those columns. The UI
and the query layer are untouched because nothing about the shape they see has
changed, and ingest still streams `COPY ... FROM STDIN` straight into it --
PostgreSQL routes each row to the partition its charge date belongs to.

What was tried and rejected
---------------------------
An earlier version of this module built a star schema: a narrow `costs_fact`
behind `dim_resource` and `dim_tagset`, presented through a `costs` VIEW made
writable by an INSTEAD OF INSERT trigger. The reasoning was that resource id,
name, resource group, subscription and region repeat on every daily row, so
storing each distinct identity once should shrink the table.

Measured at the real customer ratio of 2.86 rows per resource, it did the
opposite. `tools/bench_partition_only.py` records the three-way comparison:

                         flat         star        partitioned
      size             492.1 MB      118.4%         100.3%
      load COPY          16.5s        7.3x           1.1x
      one-month query   376.9 ms     203.1 ms       133.3 ms
      remove a month    15,481 ms     24.6 ms        114.4 ms

494,568 distinct identities over 1.5M rows left `dim_resource` (315.6 MB) larger
than the fact it served (267.2 MB): at that ratio each identity is shared by too
few rows to pay for its surrogate key, its stored tuple hash and two indexes.
The star schema was also *slower to query* than plain partitioning, because it
made the planner traverse a view and two joins to reach rows that partition
pruning alone finds directly.

The saving it chased was already banked elsewhere in any case. `costs_rollup`
answers the dashboard's seven hot queries from a pre-aggregated grain that omits
`ResourceId`, `ResourceName`, `MeterName` and `Tags` entirely -- the very columns
the dimensions existed to deduplicate -- and does it in 33 ms against 11,300 ms
on the detail table. The dashboard never reads `costs` at all.

So the dimensions were removed and the partitioning kept. The 0.3% size overhead
above is the partition headers, and it buys the retention numbers at the top.
"""
from __future__ import annotations

import datetime as dt
import logging
from collections.abc import Sequence
from typing import Any

log = logging.getLogger("cloudlens.schema_v2")

# The table keeps v1's name. Readers name `costs`, so leaving it called `costs`
# is what makes this a storage change rather than an application change.
TABLE = "costs"

PERIOD = "ChargePeriodStart"


def _sql_type(column: str, numeric: set[str]) -> str:
    if column == PERIOD:
        return "date"
    return "double precision" if column in numeric else "text"


def month_start(day: dt.date) -> dt.date:
    return day.replace(day=1)


def next_month(day: dt.date) -> dt.date:
    return (day.replace(day=1) + dt.timedelta(days=32)).replace(day=1)


def partition_name(day: dt.date) -> str:
    return f"{TABLE}_{day:%Y%m}"


def create_sql(schema: str, columns: Sequence[str], numeric: set[str]) -> list[str]:
    """DDL for the partitioned table, safe to run repeatedly.

    The column list and types are v1's, unchanged. Partitioning is the only
    difference, which is what lets the rest of the application carry on
    addressing this table exactly as it always has.
    """
    q = f'"{schema}"'
    cols = ",\n  ".join(f'"{c}" {_sql_type(c, numeric)}' for c in columns)

    return [
        # Partitioned on the date every query already filters by, so a one-month
        # dashboard window reads one partition and a retention cutoff is a DROP.
        f"""CREATE TABLE IF NOT EXISTS {q}.{TABLE} (
  {cols}
) PARTITION BY RANGE ("{PERIOD}")""",
        # A row whose month has no partition would otherwise be rejected outright.
        # Ingest creates months ahead of time, but a default partition means the
        # failure mode of missing one is a slower query, never a lost charge.
        f'CREATE TABLE IF NOT EXISTS {q}.{TABLE}_default '
        f'PARTITION OF {q}.{TABLE} DEFAULT',
        # v1's indexes, declared on the parent so every partition inherits them
        # and any partition created later gets them automatically.
        f'CREATE INDEX IF NOT EXISTS {TABLE}_period_idx ON {q}.{TABLE} ("{PERIOD}")',
        f'CREATE INDEX IF NOT EXISTS {TABLE}_sub_period_idx '
        f'ON {q}.{TABLE} (lower("SubAccountId"), "{PERIOD}")',
        f'CREATE INDEX IF NOT EXISTS {TABLE}_resource_idx '
        f'ON {q}.{TABLE} ("ResourceId", "{PERIOD}")',
    ]


def total_size(con: Any, schema: str) -> int:
    """Bytes held by the table, counting its partitions.

    `pg_total_relation_size` on a partitioned parent reports only the parent's
    own (empty) storage, so asking it about `costs` returns zero however many
    rows are underneath. The partitions have to be summed explicitly, which is
    the kind of detail that otherwise produces a triumphant and completely
    fictional size reduction.
    """
    total = con.execute(
        "SELECT coalesce(sum(pg_total_relation_size(c.oid)), 0) "
        "FROM pg_class c "
        "JOIN pg_inherits i ON i.inhrelid = c.oid "
        "JOIN pg_class p ON p.oid = i.inhparent "
        "JOIN pg_namespace n ON n.oid = p.relnamespace "
        "WHERE p.relname = ? AND n.nspname = ?",
        [TABLE, schema],
    ).fetchone()[0]
    return int(total or 0)


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
            f'PARTITION OF "{schema}".{TABLE} '
            f"FOR VALUES FROM ('{cursor:%Y-%m-%d}') TO ('{upper:%Y-%m-%d}')"
        )
        created.append(name)
        cursor = upper
    return created


def attached_partitions(con: Any, schema: str) -> list[str]:
    """Partition names currently attached to the table, oldest first."""
    rows = con.execute(
        "SELECT c.relname FROM pg_class c "
        "JOIN pg_inherits i ON i.inhrelid = c.oid "
        "JOIN pg_class p ON p.oid = i.inhparent "
        "JOIN pg_namespace n ON n.oid = p.relnamespace "
        "WHERE p.relname = ? AND n.nspname = ? ORDER BY c.relname",
        [TABLE, schema],
    ).fetchall()
    return [r[0] for r in rows]


def drop_partitions_before(con: Any, schema: str, cutoff: dt.date) -> list[str]:
    """Forget whole months in constant time.

    The reason the table is partitioned at all. Retention on the flat table was a
    DELETE that wrote every removed row to the WAL and then left the space for
    VACUUM -- 15.5 seconds to remove a month, reclaiming nothing. Here it is a
    DROP of one relation: 114 ms, and the space comes back immediately.

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
