"""Prove the presence table's money equals the detail table's, on the real estate.

The unit tests prove this on fixture data. This proves it on the rows a customer
would actually see, which is the only version of the claim that matters before the
dashboard starts publishing figures from the derived table.
"""
from __future__ import annotations

import os
import sys
import time

import psycopg

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import presence  # noqa: E402

CN = {"host": os.environ["PGHOST"], "dbname": os.environ["PGDATABASE"],
      "user": os.environ["PGUSER"], "password": os.environ["PGPASSWORD"],
      "sslmode": "disable"}


def one(c, sql):
    t = time.monotonic()
    value = c.execute(sql).fetchone()
    return value, time.monotonic() - t


with psycopg.connect(**CN) as c:
    c.execute("SET work_mem = '256MB'")

    print("-- grand total, every row including subscription-level charges --")
    (det,), dt = one(c, 'SELECT round(sum("BilledCost")::numeric, 2) FROM costs')
    (pre,), pt = one(c, f'SELECT round(sum("BilledCost")::numeric, 2) FROM {presence.TABLE}')
    print(f"  costs             {det:>20,}   ({dt:.1f}s)")
    print(f"  {presence.TABLE:16s}  {pre:>20,}   ({pt:.1f}s)")
    print(f"  difference        {det - pre:>20,}")

    print("\n-- the top-spend list the tab actually publishes --")
    body = (
        'SELECT coalesce("ResourceName", \'-\') AS name, '
        'coalesce("ResourceGroup", \'-\') AS grp, "ServiceName" AS service, '
        'coalesce("RegionName", \'-\') AS region, "BillingCurrency" AS currency, '
        'round(sum("BilledCost")::numeric, 2) AS cost FROM {src} '
        'WHERE "ResourceId" IS NOT NULL AND "ServiceFamily" IN (\'Compute\') '
        'GROUP BY 1, 2, 3, 4, 5'
    )
    mismatch = (
        f"WITH d AS ({body.format(src='costs')}), "
        f"p AS ({body.format(src=presence.TABLE)}) "
        "SELECT count(*) FROM ("
        "  (SELECT * FROM d EXCEPT SELECT * FROM p) UNION ALL "
        "  (SELECT * FROM p EXCEPT SELECT * FROM d)) x"
    )
    (rows,), mt = one(c, mismatch)
    (groups,), _ = one(c, f"SELECT count(*) FROM ({body.format(src='costs')}) x")
    print(f"  groups compared   {groups:>20,}")
    print(f"  rows differing    {rows:>20,}   ({mt:.1f}s)")
    print("\nPASS" if det == pre and rows == 0 else "\nFAIL")
