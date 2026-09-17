"""Time the dashboard entry points against the live dataset, with the derived tables
routing normally and with them forced off, so the work is judged on measurement.

Routing is not an env switch: `Warehouse.reader` sets `agg`/`res` on the reader and
`dashboard._aggregate`/`_counted` read them. Forcing the baseline therefore means
making those two helpers return the detail table, which is what they already do for
any reader that cannot answer from the derived tables.
"""
from __future__ import annotations

import logging
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

logging.basicConfig(level=logging.WARNING)

from app import dashboard as dash_mod  # noqa: E402

REAL_AGGREGATE = dash_mod._aggregate
REAL_COUNTED = dash_mod._counted


def timed(label, fn, *args, **kwargs):
    t0 = time.monotonic()
    try:
        fn(*args, **kwargs)
        took = time.monotonic() - t0
        print(f"  {label:32s} {took:8.2f}s", flush=True)
        return took
    except Exception as exc:
        print(f"  {label:32s}  FAILED  {type(exc).__name__}: {exc}", flush=True)
        return None


CASES = [
    ("sections(30d)", lambda d: d.sections(None, 30)),
    ("section(marketplace, 30d)", lambda d: d.section("marketplace", None, 30)),
    ("section(compute, 30d)", lambda d: d.section("compute", None, 30)),
]

d = dash_mod.Dashboard()
results: dict[str, dict[str, float | None]] = {}

for mode in ("derived", "detail"):
    if mode == "detail":
        dash_mod._aggregate = lambda reader: "costs"
        dash_mod._counted = lambda reader: "costs"
    else:
        dash_mod._aggregate = REAL_AGGREGATE
        dash_mod._counted = REAL_COUNTED
    print(f"\n--- {mode} ---", flush=True)
    results[mode] = {label: timed(label, fn, d) for label, fn in CASES}

print("\n--- summary ---")
for label, _ in CASES:
    new, old = results["derived"][label], results["detail"][label]
    if new and old:
        print(f"{label:32s} {old:7.2f}s -> {new:7.2f}s   {old / new:5.1f}x")
    else:
        print(f"{label:32s} incomplete")
