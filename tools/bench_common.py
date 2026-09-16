"""Shared fixtures for the schema benchmarks.

Split out so the benchmarks that use them are not coupled to each other. The
FOCUS-shaped generator in particular is the part that has to stay stable: a
result is only comparable across runs if the rows being measured are identical,
so `generate` is seeded and its defaults are the measured customer shape.

Set before `app.warehouse` is imported by anything downstream, because importing
it constructs a Warehouse at module scope which reads the application's own
connection settings and refuses to start without them. Without these a benchmark
dies on `PGHOST is not set`, and then on `server does not support SSL` -- the
application quite correctly defaults `PGSSLMODE` to `require` for the deployed
server, and a local scratch server offers neither SSL nor an Entra identity.
`setdefault`, so a real environment still wins.
"""
from __future__ import annotations

import datetime as dt
import os
import random
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ.setdefault("PGHOST", "localhost")
os.environ.setdefault("PGPORT", "5433")
os.environ.setdefault("PGDATABASE", "cloudlens_v2")
os.environ.setdefault("PGUSER", "cloudlens")
os.environ.setdefault("PGSSLMODE", "disable")

from app.warehouse import COLUMNS, NUMERIC  # noqa: E402

PERIOD = "ChargePeriodStart"


class Con:
    """The tiny slice of the warehouse connection API the schema helpers use."""

    def __init__(self, raw):
        self.raw = raw

    def execute(self, sql, params=None):
        cur = self.raw.cursor()
        cur.execute(sql.replace("?", "%s"), params or None)
        return cur


def sql_type(column: str) -> str:
    if column == PERIOD:
        return "date"
    return "double precision" if column in NUMERIC else "text"


def flat_ddl(schema: str) -> str:
    """v1's table: 37 columns, no partitioning, no indexes."""
    cols = ",\n  ".join(f'"{c}" {sql_type(c)}' for c in COLUMNS)
    return f'CREATE TABLE IF NOT EXISTS "{schema}".costs (\n  {cols}\n)'


def generate(rows: int, resources: int, months: int, seed: int = 7):
    """FOCUS-shaped rows with realistic repetition.

    Caveat worth stating wherever these numbers are quoted: the tag blob is drawn
    from six fixed values, where the real customer data has millions of distinct
    tag value groups. Any conclusion about tag storage drawn from this generator
    is an artefact of the generator. The resource ratio is faithful; the tag
    cardinality is not.
    """
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
    for _ in range(rows):
        res = pool[rnd.randrange(resources)]
        row = [None] * len(COLUMNS)
        row[idx[PERIOD]] = start + dt.timedelta(days=rnd.randrange(days))
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
    """Bytes held by one ordinary relation."""
    return con.execute(
        "SELECT pg_total_relation_size(%s)", [f'"{schema}".{rel}']
    ).fetchone()[0]


def partitioned_size(con, schema):
    """Bytes held by every ordinary and partitioned relation in a schema.

    `pg_total_relation_size` on a partitioned parent reports only the parent's
    own empty storage, so a naive call returns zero however many rows sit
    underneath -- the kind of detail that otherwise produces a triumphant and
    completely fictional size reduction.
    """
    return int(con.execute(
        "SELECT coalesce(sum(pg_total_relation_size(c.oid)), 0) FROM pg_class c "
        "JOIN pg_namespace n ON n.oid = c.relnamespace "
        "WHERE n.nspname = %s AND c.relkind IN ('r','p')", [schema]
    ).fetchone()[0])
