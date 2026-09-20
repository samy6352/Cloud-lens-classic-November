"""Verify the billing rollup against the detail table on the real estate.

Rebuilds the derived tables, then builds the same statement twice -- once through
the rollup and once forced onto the detail-table computation -- and compares every
number. A rollup that is fast and wrong is worse than the 451s it replaces.

Be aware of the cost before running this on a full estate: measured 2026-09-20 on
the 5.8M-row estate, the forced detail-table half spilled 11GB of temp files and
had not finished after half an hour. Scope both halves to a subscription if the
question is correctness rather than whole-estate timing.
"""
import os
import pathlib
import sys
import time

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
for line in (ROOT / ".env").read_text().splitlines():
    line = line.strip()
    if line and not line.startswith("#") and "=" in line:
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip())
# The rebuild legitimately runs for minutes; the reader bound is what we are testing.
os.environ["PG_STATEMENT_TIMEOUT"] = "0"

from app import billing, billing_rollup          # noqa: E402
from app.warehouse import Warehouse, warehouse    # noqa: E402

print("rebuilding derived tables from the detail table...", flush=True)
t = time.perf_counter()
warehouse.refresh_rollup(strict=True)
print(f"  refresh_rollup: {time.perf_counter()-t:.1f}s", flush=True)

with warehouse.connect(read_only=True) as con:
    for name in (billing_rollup.TABLE,):
        rows = con.execute(f'SELECT count(*) FROM "{warehouse._rollup_schema}".{name}').fetchone()[0]
        print(f"  {name}: {rows:,} rows", flush=True)

print("\nstatement through the rollup:", flush=True)
t = time.perf_counter()
fast = billing.build(None, "", None)
fast_s = time.perf_counter() - t
print(f"  {fast_s:.2f}s", flush=True)

# Force the detail-table computation by making every route look stale.
original = Warehouse._derived_sources


def detail_only(self, con, scope, currency, filters):
    inline = f"({billing_rollup.select_sql('costs')}) AS {billing_rollup.TABLE}"
    return "costs", "costs", "costs", inline


print("\nstatement through the detail table (forced):", flush=True)
Warehouse._derived_sources = detail_only
try:
    t = time.perf_counter()
    slow = billing.build(None, "", None)
    slow_s = time.perf_counter() - t
    print(f"  {slow_s:.1f}s", flush=True)
finally:
    Warehouse._derived_sources = original

print(f"\nspeedup: {slow_s / fast_s:,.0f}x", flush=True)


def diffs(a, b, path=""):
    """Every leaf where the two statements disagree."""
    out = []
    if isinstance(a, dict) and isinstance(b, dict):
        for key in sorted(set(a) | set(b)):
            out += diffs(a.get(key), b.get(key), f"{path}.{key}")
    elif isinstance(a, list) and isinstance(b, list):
        if len(a) != len(b):
            out.append(f"{path}: length {len(a)} vs {len(b)}")
        for i, (x, y) in enumerate(zip(a, b)):
            out += diffs(x, y, f"{path}[{i}]")
    elif isinstance(a, float) and isinstance(b, float):
        # Float addition is not associative, so a different grouping order can move
        # the last cent. Anything above that is a real disagreement.
        if abs(a - b) > 0.01 + abs(b) * 1e-9:
            out.append(f"{path}: {a!r} vs {b!r}")
    elif a != b:
        out.append(f"{path}: {a!r} vs {b!r}")
    return out


found = diffs(fast, slow)
print(f"\ndifferences: {len(found)}", flush=True)
for line in found[:40]:
    print("  ", line, flush=True)

print("\nheadline check:", flush=True)
for key in ("recorded_cost", "billed_cost", "effective_cost", "list_cost",
            "contracted_cost", "actual_savings"):
    print(f"  {key:<18} rollup={fast['kpis'][key]!r:>22}  detail={slow['kpis'][key]!r:>22}", flush=True)
for bucket in ("subscriptions", "services", "resources"):
    fa = fast["breakdowns"][bucket]["rows"]
    sa = slow["breakdowns"][bucket]["rows"]
    print(f"  {bucket:<18} rollup={len(fa)} rows  detail={len(sa)} rows", flush=True)
