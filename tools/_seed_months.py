"""Synthesise neighbouring months from the one real month, for multi-month testing.

The estate holds a single substantive month (August) plus a few thousand boundary
rows either side. A dashboard that compares periods cannot be exercised by that, so
this derives July and September from August.

What it does NOT do is invent a different-looking estate. The point is to test the
period machinery -- month partitions, windowed derived rebuilds, month-on-month
deltas -- so the shape of the estate is kept and only its magnitude is moved.

Values differ per service, per subscription and per day, from `hashtext`, so the
result is deterministic: the same seed run twice produces the same numbers, and a
difference between two runs means something really changed.

Unit prices are deliberately NOT scaled. Cost and quantity move by the same factor,
which keeps `cost = unitPrice * quantity` true. Scaling money alone would leave the
Billing statement's list/contracted/effective arithmetic quietly inconsistent, and
that is precisely one of the things a multi-month test is supposed to check.

Non-destructive: it refuses a month that already holds real volume, so it cannot be
run twice into the same month, and it never deletes the genuine boundary rows.

    python tools\\_seed_months.py --dry-run
    python tools\\_seed_months.py
"""
from __future__ import annotations

import argparse
import datetime as dt
import os
import pathlib
import sys
import time

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

for _line in (ROOT / ".env").read_text().splitlines():
    _line = _line.strip()
    if _line and not _line.startswith("#") and "=" in _line:
        _k, _v = _line.split("=", 1)
        os.environ.setdefault(_k.strip(), _v.strip())

SOURCE_MONTH = dt.date(2026, 8, 1)

# Columns that carry an amount of money or of usage. These move together, so the
# per-unit columns stay true. `PricingQuantity` belongs here for that reason.
SCALED = {
    "BilledCost", "CostInUsd", "InvoiceCost", "ListCost",
    "ContractedCost", "EffectiveCost", "PricingQuantity",
}

# A month below August and a month above it, so a trend has a direction.
TARGETS = [
    {
        "tag": "jul",
        "shift": -31,
        "base": 0.78,
        "window": (dt.date(2026, 7, 1), dt.date(2026, 8, 1)),
        # Every August day maps into July: both months have 31 days.
        "source_max": dt.date(2026, 8, 31),
    },
    {
        "tag": "sep",
        "shift": 31,
        "base": 1.18,
        "window": (dt.date(2026, 9, 1), dt.date(2026, 9, 29)),
        # September is a month in progress, so it stops at today rather than
        # running to the 30th. Future-dated charges are not a thing, and a
        # partial current month is what the dashboard will meet in production.
        "source_max": dt.date(2026, 8, 28),
    },
]

# A month that already holds volume is not one this script may add to.
OCCUPIED = 100_000


def _hashed(tag: str, *parts: str) -> str:
    """A deterministic 0..1 draw. Cast to bigint before abs(): hashtext can return
    -2147483648, and abs() of that overflows int4."""
    joined = " || ".join(parts)
    return (f"((abs(hashtext('{tag}' || {joined})::bigint) % 1000)::double precision "
            f"/ 999.0)")


def _factor(target: dict) -> str:
    tag = target["tag"]
    service = 'coalesce("ServiceName", \'\')'
    account = 'coalesce("SubAccountId", \'\')'
    day = '"ChargePeriodStart"::text'
    # Two independent draws: one fixed for the month (a service costing more this
    # month than last), one varying by day (so the daily shape is not August's
    # traced over again).
    by_service = _hashed(f"{tag}:svc", service, account)
    by_day = _hashed(f"{tag}:day", day, service)
    return (f"({target['base']} * (0.82 + 0.36 * {by_service})"
            f" * (0.94 + 0.12 * {by_day}))")


def _columns(con) -> list[str]:
    rows = con.execute(
        "SELECT column_name FROM information_schema.columns "
        "WHERE table_schema='public' AND table_name='costs' "
        "ORDER BY ordinal_position").fetchall()
    return [row[0] for row in rows]


def _insert_sql(columns: list[str], target: dict) -> str:
    factor = _factor(target)
    projection = []
    for name in columns:
        if name == "ChargePeriodStart":
            projection.append(
                f'"ChargePeriodStart" + {target["shift"]} AS "ChargePeriodStart"')
        elif name in SCALED:
            projection.append(f'"{name}" * {factor} AS "{name}"')
        else:
            projection.append(f'"{name}"')
    quoted = ", ".join(f'"{name}"' for name in columns)
    return (f"INSERT INTO costs ({quoted})\nSELECT {', '.join(projection)}\n"
            f"FROM costs\nWHERE \"ChargePeriodStart\" >= DATE '{SOURCE_MONTH}'\n"
            f"  AND \"ChargePeriodStart\" <= DATE '{target['source_max']}'")


def _month_rows(con, lo: dt.date, hi: dt.date) -> int:
    row = con.execute(
        f"SELECT count(*) FROM costs WHERE \"ChargePeriodStart\" >= DATE '{lo}' "
        f"AND \"ChargePeriodStart\" < DATE '{hi}'").fetchone()
    return int(row[0])


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true",
                        help="print the statements and the guard checks, write nothing")
    parser.add_argument("--no-rebuild", action="store_true",
                        help="insert and record the generation, but leave the derived rebuild "
                             "to the application. Wanted against a remote database, where the "
                             "rebuild should run under the deployment's own identity rather "
                             "than on a token held by this process.")
    args = parser.parse_args()

    from app import jobs
    from app.warehouse import warehouse

    with warehouse.connect() as con:
        columns = _columns(con)
        print(f"costs has {len(columns)} columns")
        source = _month_rows(con, SOURCE_MONTH, dt.date(2026, 9, 1))
        print(f"source month {SOURCE_MONTH:%Y-%m}: {source:,} rows")
        if source < OCCUPIED:
            print("refusing: the source month does not hold a full estate")
            return 1

        planned = []
        for target in TARGETS:
            lo, hi = target["window"]
            existing = _month_rows(con, lo, hi)
            print(f"target {lo:%Y-%m}: {existing:,} rows present", end="")
            if existing >= OCCUPIED:
                print("  -- already seeded, skipping")
                continue
            print("  -- will seed")
            planned.append(target)

        if not planned:
            print("nothing to do")
            return 0

        if args.dry_run:
            for target in planned:
                print(f"\n---- {target['tag']} "
                      f"(base {target['base']}, shift {target['shift']:+d} days) ----")
                print(_insert_sql(columns, target))
            return 0

        # This runs for many minutes; the configured statement timeout is seconds.
        con.execute("SET statement_timeout = 0")
        con.execute("SET synchronous_commit = off")

        con.execute("BEGIN")
        try:
            jobs.lock_generation(con)
            for target in planned:
                started = time.time()
                print(f"seeding {target['tag']} ...", flush=True)
                con.execute(_insert_sql(columns, target))
                print(f"  done in {time.time() - started:,.1f}s", flush=True)
            jobs.advance_generation(
                con, windows=[target["window"] for target in planned])
            con.execute("COMMIT")
        except BaseException:
            con.execute("ROLLBACK")
            raise

        for target in planned:
            lo, hi = target["window"]
            print(f"{lo:%Y-%m}: {_month_rows(con, lo, hi):,} rows after seeding")

    print("\nanalysing ...", flush=True)
    with warehouse.connect() as con:
        con.execute("SET statement_timeout = 0")
        con.execute("ANALYZE costs")

    if args.no_rebuild:
        print("\nrows are in and the generation is recorded; the derived rebuild is pending.\n"
              "Restart the application to have it rebuild under its own identity.")
        return 0

    print("rebuilding derived summaries for the seeded windows ...", flush=True)
    started = time.time()
    rows = warehouse.refresh_rollup(strict=True, incremental=True)
    print(f"derived rebuild returned {rows:,} rollup rows "
          f"in {time.time() - started:,.1f}s")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
