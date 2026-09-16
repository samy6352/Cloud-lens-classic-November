"""Build the v2 schema on the local database and prove it behaves like v1.

Run directly; it talks to the local PostgreSQL only.
"""
from __future__ import annotations

import datetime as dt
import os
import random
import sys
import time

import psycopg

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Set before `app.warehouse` is imported, because importing it constructs a Warehouse
# at module scope, which reads the application's own connection settings rather than
# the DSN below and refuses to start without them. Defaulted here so the benchmark
# runs standalone: without this it dies on `PGHOST is not set`, and then on `server
# does not support SSL` -- the application quite correctly defaults `PGSSLMODE` to
# `require` for the deployed server, and a local scratch server offers neither SSL
# nor an Entra identity. `setdefault`, so a real environment still wins.
os.environ.setdefault("PGHOST", "localhost")
os.environ.setdefault("PGPORT", "5433")
os.environ.setdefault("PGDATABASE", "cloudlens_v2")
os.environ.setdefault("PGUSER", "cloudlens")
os.environ.setdefault("PGSSLMODE", "disable")

from app import schema_v2  # noqa: E402
from app.warehouse import COLUMNS, NUMERIC  # noqa: E402

DSN = os.environ.get(
    "CLOUDLENS_LOCAL_DSN",
    "host=localhost port=5433 dbname=cloudlens_v2 user=cloudlens password=devlocal",
)

# The row-level INSTEAD OF trigger is the correctness path, not the ingest path, and
# it is orders of magnitude slower: at 1.5M rows it was still running after 23
# minutes, while the bulk loader does the same work in seconds. Measuring it is
# worth doing once; letting it gate the size, query and retention numbers that
# actually decide the design is not. Skipped above a modest row count.
TRIGGER_LIMIT = int(os.environ.get("BENCH_TRIGGER_MAX_ROWS", "200000"))


class Con:
    """The tiny slice of the warehouse connection API schema_v2 uses."""

    def __init__(self, raw):
        self.raw = raw

    def execute(self, sql, params=None):
        cur = self.raw.cursor()
        cur.execute(sql.replace("?", "%s"), params or None)
        return cur


def flat_ddl(schema: str) -> str:
    cols = ",\n  ".join(
        f'"{c}" ' + ("double precision" if c in NUMERIC
                     else "date" if c == "ChargePeriodStart" else "text")
        for c in COLUMNS
    )
    return f'CREATE TABLE IF NOT EXISTS "{schema}".costs (\n  {cols}\n)'


def generate(rows: int, resources: int, months: int, seed: int = 7):
    """FOCUS-shaped rows with realistic repetition."""
    rnd = random.Random(seed)
    groups = [f"rg-{i:03d}" for i in range(60)]
    regions = ["eastus", "westeurope", "centralindia", "swedencentral", "uksouth"]
    services = ["Virtual Machines", "Storage", "SQL Database", "Load Balancer",
                "Bandwidth", "App Service", "Cosmos DB", "Monitor"]
    meters = ["D2s v3", "Hot LRS", "vCore", "Data Processed", "Inter-Region",
              "P1v3", "RU/s", "Ingestion"]
    subs = [(f"{i:08d}-0000-0000-0000-00000000000{i}", f"sub-{i}") for i in range(1, 6)]
    tagsets = [None, "", '{"env":"prod"}', '{"env":"dev"}', '{"env":"prod","owner":"fin"}',
               '{"env":"test","owner":"eng","cc":"4711"}']

    pool = []
    for i in range(resources):
        sub = rnd.choice(subs)
        g = rnd.choice(groups)
        pool.append((
            f"/subscriptions/{sub[0]}/resourceGroups/{g}/providers/Microsoft.Compute/x/vm-{i:07d}",
            f"vm-{i:07d}", g, sub[0], sub[1], rnd.choice(regions),
        ))

    start = dt.date(2026, 1, 1)
    days = months * 30
    idx = {c: i for i, c in enumerate(COLUMNS)}
    out = []
    for n in range(rows):
        res = pool[rnd.randrange(resources)]
        row = [None] * len(COLUMNS)
        row[idx["ChargePeriodStart"]] = start + dt.timedelta(days=rnd.randrange(days))
        row[idx["BilledCost"]] = round(rnd.uniform(0.001, 40), 6)
        row[idx["CostInUsd"]] = row[idx["BilledCost"]]
        row[idx["BillingCurrency"]] = "USD"
        row[idx["PricingQuantity"]] = round(rnd.uniform(0.1, 24), 4)
        row[idx["UnitPrice"]] = round(rnd.uniform(0.001, 2), 6)
        row[idx["UnitOfMeasure"]] = "1 Hour"
        row[idx["ChargeCategory"]] = "Usage"
        row[idx["ServiceName"]] = rnd.choice(services)
        row[idx["ServiceFamily"]] = "Compute"
        row[idx["MeterName"]] = rnd.choice(meters)
        row[idx["ProductName"]] = "Azure"
        row[idx["Tags"]] = rnd.choice(tagsets)
        row[idx["ResourceId"]] = res[0]
        row[idx["ResourceName"]] = res[1]
        row[idx["ResourceGroup"]] = res[2]
        row[idx["SubAccountId"]] = res[3]
        row[idx["SubAccountName"]] = res[4]
        row[idx["RegionName"]] = res[5]
        out.append(row)
    return out


def copy_in(con, target, records):
    names = ",".join(f'"{c}"' for c in COLUMNS)
    with con.raw.cursor() as cur, cur.copy(f"COPY {target} ({names}) FROM STDIN") as cp:
        for r in records:
            cp.write_row(r)


def size(con, schema, rel):
    return con.execute(
        "SELECT pg_total_relation_size(%s)", [f'"{schema}".{rel}']
    ).fetchone()[0]


def main():
    rows = int(os.environ.get("ROWS", "300000"))
    resources = int(os.environ.get("RESOURCES", "60000"))
    months = int(os.environ.get("MONTHS", "6"))

    raw = psycopg.connect(DSN, autocommit=True)
    con = Con(raw)

    print(f"generating {rows:,} rows over {resources:,} resources, {months} months")
    data = generate(rows, resources, months)

    for schema in ("bench_v1", "bench_v2"):
        con.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')
        con.execute(f'CREATE SCHEMA "{schema}"')

    # --- v1 flat baseline -------------------------------------------------
    con.execute(flat_ddl("bench_v1"))
    t0 = time.monotonic()
    copy_in(con, '"bench_v1".costs', data)
    v1_load = time.monotonic() - t0
    con.execute('ANALYZE "bench_v1".costs')

    # --- v2 partitioned + dimensions -------------------------------------
    for stmt in schema_v2.create_sql("bench_v2", list(COLUMNS), NUMERIC):
        con.execute(stmt)
    first = min(r[0] for r in data)
    last = max(r[0] for r in data)
    parts = schema_v2.ensure_partitions(con, "bench_v2", first, last)
    print(f"partitions: {len(parts)} ({parts[0]} .. {parts[-1]})")

    t0 = time.monotonic()
    if rows <= TRIGGER_LIMIT:
        copy_in(con, '"bench_v2".costs', data)
        v2_load = time.monotonic() - t0
        con.execute(f'TRUNCATE "bench_v2".{schema_v2.FACT}')
        con.execute(f'TRUNCATE "bench_v2".{schema_v2.DIM_RESOURCE} CASCADE')
        con.execute(f'TRUNCATE "bench_v2".{schema_v2.DIM_TAGSET} CASCADE')
    else:
        v2_load = None
        print(f"skipping the trigger load leg ({rows:,} rows > "
              f"BENCH_TRIGGER_MAX_ROWS={TRIGGER_LIMIT:,})")

    # And again through the bulk path the ingest will actually use.
    for stmt in schema_v2.staging_sql("bench_v2", list(COLUMNS), NUMERIC):
        con.execute(stmt)
    t0 = time.monotonic()
    copy_in(con, f'"bench_v2".{schema_v2.STAGING}', data)
    for stmt in schema_v2.promote_sql("bench_v2", list(COLUMNS)):
        con.execute(stmt)
    v2_bulk = time.monotonic() - t0

    for rel in (schema_v2.FACT, schema_v2.DIM_RESOURCE, schema_v2.DIM_TAGSET):
        con.execute(f'ANALYZE "bench_v2".{rel}')

    # --- equivalence ------------------------------------------------------
    print("\n--- equivalence ---")
    checks = [
        ("row count", 'SELECT count(*) FROM {s}.costs'),
        # Rounded to the cent. `sum()` over double precision is not associative, so
        # the same rows aggregated in a different physical order can differ in the
        # last bit -- v1 returned ...982869 and v2 ...982870 on identical input.
        # That is float addition, not a difference in what was summed, and the cent
        # is the precision these numbers are actually presented at.
        ("total BilledCost", 'SELECT round(sum("BilledCost")::numeric, 2) FROM {s}.costs'),
        ("total CostInUsd", 'SELECT round(sum("CostInUsd")::numeric, 2) FROM {s}.costs'),
        ("distinct resources", 'SELECT count(DISTINCT "ResourceId") FROM {s}.costs'),
        ("distinct tags", 'SELECT count(DISTINCT coalesce("Tags",\'~\')) FROM {s}.costs'),
        ("group-by service",
         'SELECT md5(string_agg(x, \'|\' ORDER BY x)) FROM ('
         ' SELECT "ServiceName" || \':\' || round(sum("BilledCost")::numeric,4)::text AS x'
         ' FROM {s}.costs GROUP BY "ServiceName") q'),
        ("group-by rg+region",
         'SELECT md5(string_agg(x, \'|\' ORDER BY x)) FROM ('
         ' SELECT "ResourceGroup" || \'/\' || "RegionName" || \':\''
         ' || round(sum("BilledCost")::numeric,4)::text AS x'
         ' FROM {s}.costs GROUP BY "ResourceGroup", "RegionName") q'),
        ("daily trend",
         'SELECT md5(string_agg(x, \'|\' ORDER BY x)) FROM ('
         ' SELECT "ChargePeriodStart"::text || \':\' || round(sum("BilledCost")::numeric,4)::text AS x'
         ' FROM {s}.costs GROUP BY "ChargePeriodStart") q'),
    ]
    ok = True
    for label, sql in checks:
        a = con.execute(sql.format(s='"bench_v1"')).fetchone()[0]
        b = con.execute(sql.format(s='"bench_v2"')).fetchone()[0]
        same = a == b
        ok &= same
        print(f"  {'OK ' if same else 'DIFF'}  {label:22} {a}")
        if not same:
            print(f"        v2 -> {b}")

    # --- size -------------------------------------------------------------
    print("\n--- size ---")
    v1 = size(con, "bench_v1", "costs")
    parts = schema_v2.total_size(con, "bench_v2")
    fact = parts[schema_v2.FACT]
    dr = parts[schema_v2.DIM_RESOURCE]
    dt_ = parts[schema_v2.DIM_TAGSET]
    v2 = fact + dr + dt_
    mb = lambda n: f"{n/1024/1024:8.1f} MB"
    print(f"  v1 flat costs        {mb(v1)}")
    print(f"  v2 costs_fact        {mb(fact)}  (all partitions)")
    print(f"  v2 dim_resource      {mb(dr)}")
    print(f"  v2 dim_tagset        {mb(dt_)}")
    print(f"  v2 total             {mb(v2)}   ({v2/v1*100:.1f}% of v1)")

    dimr = con.execute('SELECT count(*) FROM "bench_v2".dim_resource').fetchone()[0]
    dimt = con.execute('SELECT count(*) FROM "bench_v2".dim_tagset').fetchone()[0]
    print(f"  dim_resource rows    {dimr:,}   (fact rows {len(data):,})")
    print(f"  dim_tagset rows      {dimt:,}")

    trigger_line = ("\n  v2 via trigger        skipped"
                    if v2_load is None else
                    f"\n  v2 via trigger    {v2_load:7.1f}s   ({v2_load/v1_load:.1f}x v1)")
    print(f"\n--- load ---\n  v1 COPY           {v1_load:7.1f}s"
          + trigger_line
          + f"\n  v2 bulk (ingest)  {v2_bulk:7.1f}s   ({v2_bulk/v1_load:.1f}x v1)")

    # --- query time -------------------------------------------------------
    print("\n--- query (one month window) ---")
    lo = schema_v2.month_start(first)
    hi = schema_v2.next_month(lo)
    q = ('SELECT "ServiceName", sum("BilledCost") FROM {s}.costs '
         f"WHERE \"ChargePeriodStart\" >= '{lo}' AND \"ChargePeriodStart\" < '{hi}' "
         'GROUP BY 1')
    for label, s in (("v1 flat", '"bench_v1"'), ("v2 partitioned", '"bench_v2"')):
        best = min(_timed(con, q.format(s=s)) for _ in range(3))
        print(f"  {label:16} {best*1000:8.1f} ms")

    # --- retention --------------------------------------------------------
    print("\n--- retention (drop oldest month) ---")
    before = con.execute('SELECT count(*) FROM "bench_v2".costs').fetchone()[0]
    t0 = time.monotonic()
    dropped = schema_v2.drop_partitions_before(con, "bench_v2", schema_v2.next_month(lo))
    el = time.monotonic() - t0
    after = con.execute('SELECT count(*) FROM "bench_v2".costs').fetchone()[0]
    print(f"  dropped {dropped} in {el*1000:.1f} ms; rows {before:,} -> {after:,}")

    print("\nRESULT:", "all equivalence checks passed" if ok else "EQUIVALENCE FAILED")
    raw.close()
    return 0 if ok else 1


def _timed(con, sql):
    t0 = time.monotonic()
    con.execute(sql).fetchall()
    return time.monotonic() - t0


if __name__ == "__main__":
    raise SystemExit(main())
