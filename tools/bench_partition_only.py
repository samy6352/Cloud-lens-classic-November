"""Measure a month-partitioned flat table against the plain flat table.

Why this exists
---------------
An earlier benchmark measured the four-object star schema and it lost: at the real
customer ratio of 2.86 rows per resource, `dim_resource` grew to 315.6 MB against a
267.2 MB fact, making it 118.4% of the flat table it was meant to shrink, and the
bulk load 7.3x slower than a plain COPY. That script has been removed along with
the design it measured; both are preserved in history at commit 2af27ef.

What did pay in that run came from partitioning alone -- dropping a month took
24.6 ms, and the one-month query ran 203 ms against the flat table's 366 ms.
Neither needs a dimension table.

So this isolates the variable. Same 37 columns as v1, same rows, no dimensions, no
view, no trigger: the only difference is RANGE partitioning by month. If size and
load come out at parity with flat while the query and retention wins survive, then
partitioning on its own is the whole design and the rest is complexity to delete.

Run directly. Honours ROWS / RESOURCES / MONTHS from the environment, and defaults
to the local server configured in `bench_common`.
"""
from __future__ import annotations

import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import psycopg  # noqa: E402

# bench_common sets the PG* defaults the application needs, and must therefore be
# imported before anything that pulls in app.warehouse.
from bench_common import (  # noqa: E402
    Con, copy_in, flat_ddl, generate, partitioned_size, size, sql_type,
)

from app import schema_v2  # noqa: E402
from app.warehouse import COLUMNS  # noqa: E402

DSN = os.environ.get(
    "CLOUDLENS_LOCAL_DSN",
    "host=localhost port=5433 dbname=cloudlens_v2 user=cloudlens "
    "password=devlocal sslmode=disable",
)

PERIOD = "ChargePeriodStart"


def partitioned_ddl(schema: str) -> str:
    """The v1 table, partitioned by month and otherwise identical.

    Deliberately the same column list and the same types as `flat_ddl`. The
    comparison is only worth anything if partitioning is the single difference,
    so nothing here narrows a column or adds an index that v1 does not have.
    """
    cols = ",\n  ".join(f'"{c}" {sql_type(c)}' for c in COLUMNS)
    return (f'CREATE TABLE IF NOT EXISTS "{schema}".costs (\n  {cols}\n) '
            f'PARTITION BY RANGE ("{PERIOD}")')


def _timed(con, sql):
    t0 = time.monotonic()
    con.execute(sql).fetchall()
    return time.monotonic() - t0


def main() -> int:
    rows = int(os.environ.get("ROWS", "1500000"))
    resources = int(os.environ.get("RESOURCES", "524476"))
    months = int(os.environ.get("MONTHS", "6"))

    raw = psycopg.connect(DSN, autocommit=True)
    con = Con(raw)

    print(f"generating {rows:,} rows over {resources:,} resources, {months} months")
    data = generate(rows, resources, months)
    period = COLUMNS.index(PERIOD) if isinstance(COLUMNS, list) else list(COLUMNS).index(PERIOD)
    first = min(r[period] for r in data)
    last = max(r[period] for r in data)

    for schema in ("bench_v1", "bench_v3"):
        con.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')
        con.execute(f'CREATE SCHEMA "{schema}"')

    # --- v1: flat, unpartitioned -----------------------------------------
    con.execute(flat_ddl("bench_v1"))
    t0 = time.monotonic()
    copy_in(con, '"bench_v1".costs', data)
    v1_load = time.monotonic() - t0
    con.execute('ANALYZE "bench_v1".costs')

    # --- v3: flat, month-partitioned -------------------------------------
    con.execute(partitioned_ddl("bench_v3"))
    parts = []
    cursor = schema_v2.month_start(first)
    while cursor <= last:
        upper = schema_v2.next_month(cursor)
        name = f"costs_{cursor:%Y%m}"
        con.execute(
            f'CREATE TABLE IF NOT EXISTS "bench_v3".{name} '
            f'PARTITION OF "bench_v3".costs '
            f"FOR VALUES FROM ('{cursor}') TO ('{upper}')")
        parts.append(name)
        cursor = upper
    print(f"partitions: {len(parts)} ({parts[0]} .. {parts[-1]})")

    # COPY straight into the parent. PostgreSQL routes each row to its partition,
    # so this is the same one-step ingest v1 has -- no staging, no promote, no
    # trigger, which is the entire point of the comparison.
    t0 = time.monotonic()
    copy_in(con, '"bench_v3".costs', data)
    v3_load = time.monotonic() - t0
    con.execute('ANALYZE "bench_v3".costs')

    # --- equivalence ------------------------------------------------------
    print("\n--- equivalence ---")
    checks = [
        ("row count", 'SELECT count(*) FROM {s}.costs'),
        # To the cent: sum() over double precision is not associative, so a
        # different physical row order can move the last bit.
        ("total BilledCost", 'SELECT round(sum("BilledCost")::numeric, 2) FROM {s}.costs'),
        ("distinct resources", 'SELECT count(DISTINCT "ResourceId") FROM {s}.costs'),
        ("group-by service",
         'SELECT md5(string_agg(x, \'|\' ORDER BY x)) FROM ('
         ' SELECT "ServiceName" || \':\' || round(sum("BilledCost")::numeric,4)::text AS x'
         ' FROM {s}.costs GROUP BY "ServiceName") q'),
        ("daily trend",
         'SELECT md5(string_agg(x, \'|\' ORDER BY x)) FROM ('
         ' SELECT "ChargePeriodStart"::text || \':\''
         ' || round(sum("BilledCost")::numeric,4)::text AS x'
         ' FROM {s}.costs GROUP BY "ChargePeriodStart") q'),
    ]
    ok = True
    for label, sql in checks:
        a = con.execute(sql.format(s='"bench_v1"')).fetchone()[0]
        b = con.execute(sql.format(s='"bench_v3"')).fetchone()[0]
        same = a == b
        ok &= same
        print(f"  {'OK ' if same else 'DIFF'}  {label:22} {a}")
        if not same:
            print(f"        v3 -> {b}")

    # --- size -------------------------------------------------------------
    print("\n--- size ---")
    v1 = size(con, "bench_v1", "costs")
    v3 = partitioned_size(con, "bench_v3")
    mb = lambda n: f"{n/1024/1024:8.1f} MB"  # noqa: E731
    print(f"  v1 flat              {mb(v1)}")
    print(f"  v3 partitioned       {mb(v3)}   ({v3/v1*100:.1f}% of v1)")

    print(f"\n--- load ---\n  v1 COPY           {v1_load:7.1f}s"
          f"\n  v3 COPY           {v3_load:7.1f}s   ({v3_load/v1_load:.1f}x v1)")

    # --- query ------------------------------------------------------------
    print("\n--- query (one month window) ---")
    lo = schema_v2.month_start(first)
    hi = schema_v2.next_month(lo)
    q = ('SELECT "ServiceName", sum("BilledCost") FROM {s}.costs '
         f"WHERE \"{PERIOD}\" >= '{lo}' AND \"{PERIOD}\" < '{hi}' GROUP BY 1")
    for label, s in (("v1 flat", '"bench_v1"'), ("v3 partitioned", '"bench_v3"')):
        best = min(_timed(con, q.format(s=s)) for _ in range(3))
        print(f"  {label:16} {best*1000:8.1f} ms")

    # --- retention --------------------------------------------------------
    # Both ways of removing a month, measured against each other. This is the
    # comparison partitioning exists for: v1 must find and delete the rows and
    # leave dead tuples behind for VACUUM, v3 unlinks a file.
    print("\n--- retention (remove oldest month) ---")
    before = con.execute('SELECT count(*) FROM "bench_v1".costs').fetchone()[0]
    t0 = time.monotonic()
    con.execute(f"DELETE FROM \"bench_v1\".costs WHERE \"{PERIOD}\" < '{hi}'")
    v1_drop = time.monotonic() - t0
    after_v1 = con.execute('SELECT count(*) FROM "bench_v1".costs').fetchone()[0]

    t0 = time.monotonic()
    con.execute(f'DROP TABLE "bench_v3".{parts[0]}')
    v3_drop = time.monotonic() - t0
    after_v3 = con.execute('SELECT count(*) FROM "bench_v3".costs').fetchone()[0]

    print(f"  v1 DELETE         {v1_drop*1000:9.1f} ms   rows {before:,} -> {after_v1:,}")
    print(f"  v3 DROP partition {v3_drop*1000:9.1f} ms   rows {before:,} -> {after_v3:,}")
    if after_v1 != after_v3:
        ok = False
        print("  DIFF  the two retention paths removed different rows")

    # Size after removal, before any VACUUM. A DELETE leaves the space behind.
    v1_after = size(con, "bench_v1", "costs")
    v3_after = partitioned_size(con, "bench_v3")
    print(f"  v1 size after     {mb(v1_after)}   (was {mb(v1)}, DELETE does not reclaim)")
    print(f"  v3 size after     {mb(v3_after)}   (was {mb(v3)})")

    print("\nRESULT:", "all equivalence checks passed" if ok else "EQUIVALENCE FAILED")
    raw.close()
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
