"""Apply the month seeding to a remote database, without booting the application.

`_seed_months.py` imports `app.warehouse`, and importing that against a remote
database runs schema initialisation with *this* machine's settings -- which are
not the deployment's. Against CloudLens-Scale that would mean a local
`CLOUDLENS_DERIVED_LAYOUT=monthly` meeting a flat remote layout.

So this connects with psycopg directly and reuses the SQL builder from the seed
script, which is deliberately free of application imports at module scope. The
rows and the generation bookkeeping are written here; the derived rebuild is
left to the deployment, which owns its own identity and its own layout.

Authentication is an Entra access token passed as the password. Only the initial
connection needs it -- PostgreSQL does not revalidate a session mid-flight -- so
a long insert outliving the token is not a problem.

    python tools\\_seed_remote.py --dry-run
    python tools\\_seed_remote.py
"""
from __future__ import annotations

import argparse
import importlib.util
import os
import pathlib
import subprocess
import sys
import time

ROOT = pathlib.Path(__file__).resolve().parents[1]

_spec = importlib.util.spec_from_file_location(
    "_seed_months_sql", ROOT / "tools" / "_seed_months.py")
_seed = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_seed)

HOST = "cloudlens-postgres-scale.postgres.database.azure.com"
USER = "raviverma_microsoft.com#EXT#@fdpo.onmicrosoft.com"
DATABASE = "cloudlens"


def _token() -> str:
    out = subprocess.run(
        ["az", "account", "get-access-token", "--resource",
         "https://ossrdbms-aad.database.windows.net", "--query", "accessToken", "-o", "tsv"],
        capture_output=True, text=True, shell=True, check=True)
    return out.stdout.strip()


def _month_rows(cur, lo, hi) -> int:
    cur.execute(f'SELECT count(*) FROM costs WHERE "ChargePeriodStart" >= DATE \'{lo}\' '
                f'AND "ChargePeriodStart" < DATE \'{hi}\'')
    return int(cur.fetchone()[0])


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    import psycopg

    print(f"connecting to {HOST} as {USER}", flush=True)
    with psycopg.connect(host=HOST, dbname=DATABASE, user=USER, password=_token(),
                         sslmode="require", connect_timeout=30,
                         application_name="cloudlens-seed") as con:
        con.autocommit = True
        with con.cursor() as cur:
            cur.execute("SET statement_timeout = 0")
            cur.execute("SELECT column_name FROM information_schema.columns "
                        "WHERE table_schema='public' AND table_name='costs' "
                        "ORDER BY ordinal_position")
            columns = [row[0] for row in cur.fetchall()]
            print(f"costs has {len(columns)} columns")

            source = _month_rows(cur, _seed.SOURCE_MONTH, "2026-09-01")
            print(f"source month {_seed.SOURCE_MONTH:%Y-%m}: {source:,} rows")
            if source < _seed.OCCUPIED:
                print("refusing: the source month does not hold a full estate")
                return 1

            planned = []
            for target in _seed.TARGETS:
                lo, hi = target["window"]
                existing = _month_rows(cur, lo, hi)
                print(f"target {lo:%Y-%m}: {existing:,} rows present", end="")
                if existing >= _seed.OCCUPIED:
                    print("  -- already seeded, skipping")
                    continue
                print("  -- will seed")
                planned.append(target)

            if not planned:
                print("nothing to do")
                return 0

            cur.execute("SELECT key, value FROM meta WHERE key LIKE 'data_%%' ORDER BY key")
            print("\nmeta before:")
            for key, value in cur.fetchall():
                print(f"  {key} = {value}")

            if args.dry_run:
                for target in planned:
                    print(f"\n---- {target['tag']} ----")
                    print(_seed._insert_sql(columns, target)[:400] + " ...")
                return 0

            cur.execute("SET synchronous_commit = off")
            for target in planned:
                started = time.time()
                print(f"\nseeding {target['tag']} ...", flush=True)
                cur.execute(_seed._insert_sql(columns, target))
                print(f"  {cur.rowcount:,} rows in {time.time() - started:,.1f}s", flush=True)

            # The same bookkeeping `jobs.advance_generation(window=None)` writes: the
            # source generation moves on, and the dirty ledger records unknown coverage,
            # which is what requires a full rebuild rather than a windowed repair. The
            # ready generation is left alone -- it is already -1 here, because the
            # deployment invalidated it when it created the new derived tables.
            cur.execute("UPDATE meta SET value=CAST(CAST(value AS BIGINT)+1 AS TEXT), "
                        "updated_at=now() WHERE key='data_generation'")
            cur.execute("SELECT value FROM meta WHERE key='data_generation'")
            generation = int(cur.fetchone()[0])
            cur.execute("SELECT value FROM meta WHERE key='data_rollup_generation'")
            row = cur.fetchone()
            ready = int(row[0]) if row else -1
            cur.execute(
                "INSERT INTO meta(key,value,updated_at) VALUES ('data_rollup_dirty',%s,now()) "
                "ON CONFLICT (key) DO UPDATE SET value=excluded.value,updated_at=now()",
                ('{"generation": %d, "base_generation": %d, "windows": null}'
                 % (generation, ready),))

            print("\nanalysing ...", flush=True)
            started = time.time()
            cur.execute("ANALYZE costs")
            print(f"  analyzed in {time.time() - started:,.1f}s")

            cur.execute("SELECT to_char(date_trunc('month',\"ChargePeriodStart\"),'YYYY-MM'), "
                        "count(*), round(sum(\"BilledCost\")::numeric,0) "
                        "FROM costs GROUP BY 1 ORDER BY 1")
            print("\nmonths now:")
            for month, rows, billed in cur.fetchall():
                print(f"  {month}  {rows:>12,}  {billed:>16,}")

            cur.execute("SELECT key, value FROM meta WHERE key LIKE 'data_%%' ORDER BY key")
            print("\nmeta after:")
            for key, value in cur.fetchall():
                print(f"  {key} = {value}")

    print("\nRestart the web app so it rebuilds the derived tables under its own identity.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
