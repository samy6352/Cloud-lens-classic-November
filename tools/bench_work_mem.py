"""Whether `work_mem` and sort collation actually make the top-resources query faster.

The claim under test is the one in `store.py`'s `PG_WORK_MEM` comment: that the
dashboard's slowest query sorts more groups than 64MB can hold, spills to temp files,
and gets faster with more memory. A single before/after pair appeared to show 4.5s ->
1.7s, which is not evidence -- this machine also runs an Azure backfill, an editor and
a browser, and one unpaired measurement on Windows cannot tell a real effect from a
quiet moment.

So: four configurations, run interleaved rather than in blocks, so that anything that
drifts over the life of the run -- cache warmth, background load, thermal throttling --
lands on all four roughly equally instead of flattering whichever ran last.

    A  64MB   default collation     the current server default
    B  256MB  default collation     work_mem alone
    C  64MB   C collation           collation alone
    D  256MB  C collation           both

Collation is varied separately from memory because it is not a free change: the sort
keys are `currency` and `name`, so `COLLATE "C"` alters which of two equal-cost rows
wins the tie-break and the order the currency groups come out in. It is only worth
that if it pays, and this says whether it pays.

Two warnings about reading the output.

Windows collation is not Linux collation. The database is `English_India.1252` with
the libc provider, and libc string comparison on Windows is markedly slower than glibc.
Any collation result here is directional; the production figure has to come from Linux.

`work_mem` is charged per sort or hash node and per connection, so a 256MB setting is
not a 256MB commitment -- this query alone has a Sort and a HashAggregate, and
`PG_POOL_SIZE` of them can run at once. Take the memory number from the plan's Sort
Space Used, not from the setting.

Run with the application's own `.env`:

    python tools/bench_work_mem.py [reps] [warmups]
"""
from __future__ import annotations

import json
import logging
import os
import statistics
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

logging.basicConfig(level=logging.WARNING)

from dotenv import load_dotenv  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
load_dotenv(os.path.join(ROOT, ".env"))

# The benchmark sets `work_mem` itself, per run. Left in place, the application's own
# value would be the starting point for every configuration including the 64MB one.
os.environ.pop("PG_WORK_MEM", None)

from app import warehouse as wh_mod  # noqa: E402

# Captured from `section("compute", 30d)` by `tools/capture_section_sql.py`, which is
# where it was measured at 8.4s of a 10.8s tab. Inlined rather than imported because the
# point is to hold the statement still: if the application's SQL changes, this file
# should be re-captured deliberately, not silently start measuring something else.
QUERY = """
WITH grouped AS (
  SELECT coalesce("ResourceName", '{dash}') AS name,
         coalesce("ResourceGroup", '{dash}') AS grp,
         "ServiceName" AS service,
         coalesce("RegionName", '{dash}') AS region,
         "BillingCurrency" AS currency,
         sum("BilledCost") AS cost
    FROM resource_presence
   WHERE "ServiceFamily" IN ('Compute', 'Web', 'Containers')
     AND "ChargePeriodStart" > DATE '2026-08-31' - INTERVAL 30 DAY
     AND "ResourceId" IS NOT NULL
   GROUP BY 1, 2, 3, 4, 5
), ranked AS (
  SELECT grouped.*,
         row_number() OVER (PARTITION BY {currency} ORDER BY cost DESC, {name}) AS spend_rank
    FROM grouped
) SELECT * FROM ranked WHERE spend_rank <= 20 ORDER BY {currency}, cost DESC, {name}
"""

PLAIN = {"currency": "currency", "name": "name"}
C_COLL = {"currency": 'currency COLLATE "C"', "name": 'name COLLATE "C"'}

# The em dash `dashboard.py` substitutes for a missing name, kept as an escape so the
# file's own encoding cannot quietly turn it into something that sorts differently.
DASH = "\u2014"

CONFIGS = [
    ("A  64MB  default", "64MB", PLAIN),
    ("B 256MB  default", "256MB", PLAIN),
    ("C  64MB  C collate", "64MB", C_COLL),
    ("D 256MB  C collate", "256MB", C_COLL),
]


def sorts(node: dict) -> list[dict]:
    """Every Sort node in a plan tree, outermost first."""
    found = [node] if node.get("Node Type") == "Sort" else []
    for child in node.get("Plans", []):
        found += sorts(child)
    return found


def temp_blocks(node: dict) -> tuple[int, int]:
    """Temp blocks read and written by a plan tree.

    Summed over the tree rather than read off the root: BUFFERS counters on a node
    include its children, but a node that did no temp I/O of its own omits the keys
    entirely, so the root of a plan can be silent about a spill further down.
    """
    read = node.get("Temp Read Blocks", 0)
    written = node.get("Temp Written Blocks", 0)
    for child in node.get("Plans", []):
        r, w = temp_blocks(child)
        read += r
        written += w
    return read, written


def run(reader, work_mem: str, keys: dict) -> dict:
    reader.rows("SELECT set_config('work_mem', %s, false)" % _literal(work_mem))
    sql = "EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON) " + QUERY.format(dash=DASH, **keys)
    plan = reader.rows(sql, limit=1)[0]
    payload = next(iter(plan.values()))
    if isinstance(payload, str):
        payload = json.loads(payload)
    root = payload[0]
    top = root["Plan"]
    nodes = sorts(top)
    biggest = max(nodes, key=lambda n: n.get("Sort Space Used", 0), default={})
    read, written = temp_blocks(top)
    return {
        "exec_ms": root["Execution Time"],
        "plan_ms": root["Planning Time"],
        "sort_method": biggest.get("Sort Method", "-"),
        "sort_kb": biggest.get("Sort Space Used", 0),
        "sort_space": biggest.get("Sort Space Type", "-"),
        "temp_read": read,
        "temp_written": written,
        "shared_read": top.get("Shared Read Blocks", 0),
        "shared_hit": top.get("Shared Hit Blocks", 0),
    }


def _literal(value: str) -> str:
    """`set_config` takes parameters, but `Reader.rows` passes none, so inline safely."""
    if not value.replace("kB", "").replace("MB", "").replace("GB", "").isdigit():
        raise ValueError(f"not a memory value: {value!r}")
    return "'" + value + "'"


def main() -> None:
    reps = int(sys.argv[1]) if len(sys.argv) > 1 else 7
    warmups = int(sys.argv[2]) if len(sys.argv) > 2 else 1

    wh = wh_mod.Warehouse()
    with wh.reader() as reader:
        server = reader.rows("SELECT current_setting('work_mem') AS w")[0]["w"]
        coll = reader.rows(
            "SELECT datcollate AS c, datlocprovider AS p FROM pg_database "
            "WHERE datname = current_database()")[0]
        print(f"server work_mem={server}  collation={coll['c']} provider={coll['p']}")
        print(f"{reps} measured reps after {warmups} warm-up, interleaved A-B-C-D\n")

        for i in range(warmups):
            for _, mem, keys in CONFIGS:
                run(reader, mem, keys)
            print(f"warm-up {i + 1} done")

        results: dict[str, list[dict]] = {name: [] for name, _, _ in CONFIGS}
        for rep in range(reps):
            line = [f"rep {rep + 1}:"]
            for name, mem, keys in CONFIGS:
                r = run(reader, mem, keys)
                results[name].append(r)
                line.append(f"{name[0]}={r['exec_ms'] / 1000:6.2f}s")
            print("  ".join(line))

        print()
        head = f"{'config':<20}{'median':>9}{'min':>9}{'max':>9}{'stdev':>8}  {'sort':<22}{'temp blk':>10}"
        print(head)
        print("-" * len(head))
        for name, _, _ in CONFIGS:
            runs = results[name]
            secs = sorted(r["exec_ms"] / 1000 for r in runs)
            last = runs[-1]
            sort = f"{last['sort_method']} {last['sort_kb'] / 1024:.0f}MB {last['sort_space']}"
            spread = statistics.stdev(secs) if len(secs) > 1 else 0.0
            print(f"{name:<20}{statistics.median(secs):>8.2f}s{secs[0]:>8.2f}s"
                  f"{secs[-1]:>8.2f}s{spread:>8.2f}  {sort:<22}"
                  f"{last['temp_read'] + last['temp_written']:>10}")

        base = [r["exec_ms"] / 1000 for r in results[CONFIGS[0][0]]]
        print()
        for name, _, _ in CONFIGS[1:]:
            other = [r["exec_ms"] / 1000 for r in results[name]]
            diffs = [b - o for b, o in zip(base, other)]
            wins = sum(1 for d in diffs if d > 0)
            print(f"{name} vs A: median saving {statistics.median(diffs):+.2f}s, "
                  f"faster in {wins}/{len(diffs)} paired reps")


if __name__ == "__main__":
    main()
