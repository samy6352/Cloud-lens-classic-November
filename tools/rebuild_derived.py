"""Rebuild costs_rollup + resource_presence against the live dataset and measure."""
from __future__ import annotations

import logging
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(levelname)s %(name)s %(message)s")

from app import presence, rollup  # noqa: E402
from app.warehouse import Warehouse  # noqa: E402

print("rollup grain  :", rollup.DIMENSIONS)
print("presence grain:", presence.DIMENSIONS)

t0 = time.monotonic()
wh = Warehouse()
print(f"warehouse init: {time.monotonic() - t0:.1f}s  state={wh.state.get('status')}")

t0 = time.monotonic()
rows = wh.refresh_rollup(strict=True)
print(f"refresh_rollup: {rows:,} rollup rows in {time.monotonic() - t0:.1f}s")

with wh._store.connect() as con:  # noqa: SLF001
    schema = wh._rollup_schema  # noqa: SLF001
    for table in (rollup.TABLE, presence.TABLE):
        n = con.execute(f'SELECT count(*) FROM "{schema}".{table}').fetchone()[0]
        size = con.execute(
            "SELECT pg_size_pretty(pg_total_relation_size(?))", [f'"{schema}".{table}']
        ).fetchone()[0]
        print(f"{table:20s} {n:>12,} rows  {size}")
