"""Poll the Scale database while the deployment brings itself up.

The interesting question is not "is a query running" but "has the rollup
generation caught up with the data generation". That is one cheap read of
`meta`, so it can be asked often without becoming part of the load it is
measuring. Row counts of the derived tables come from the planner's
statistics rather than count(*), for the same reason.
"""
from __future__ import annotations

import datetime as dt
import subprocess
import sys
import time

import psycopg

HOST = "cloudlens-postgres-scale.postgres.database.azure.com"
USER = "raviverma_microsoft.com#EXT#@fdpo.onmicrosoft.com"
DATABASE = "cloudlens"

ACTIVITY = r"""
SELECT application_name, state,
       round(extract(epoch FROM now() - query_start))::int AS secs,
       left(regexp_replace(query, '\s+', ' ', 'g'), 95)
FROM pg_stat_activity
WHERE datname = 'cloudlens' AND pid <> pg_backend_pid() AND state <> 'idle'
ORDER BY query_start
"""

DERIVED = """
SELECT c.relname, t.n_live_tup, pg_size_pretty(pg_total_relation_size(c.oid))
FROM pg_class c JOIN pg_stat_user_tables t ON t.relid = c.oid
WHERE c.relname ~ '(rollup|presence|serving)' AND c.relkind = 'r'
ORDER BY c.relname
"""


def _token() -> str:
    out = subprocess.run(
        ["az", "account", "get-access-token", "--resource",
         "https://ossrdbms-aad.database.windows.net", "--query", "accessToken", "-o", "tsv"],
        capture_output=True, text=True, shell=True, check=True)
    return out.stdout.strip()


def main() -> int:
    minutes = float(sys.argv[1]) if len(sys.argv) > 1 else 30.0
    deadline = time.time() + minutes * 60
    token = _token()
    issued = time.time()
    previous = None

    while time.time() < deadline:
        # The token outlives a short poll but not a long watch; only a new
        # connection needs it, so refreshing hourly is enough.
        if time.time() - issued > 2400:
            token, issued = _token(), time.time()
        stamp = dt.datetime.now().strftime("%H:%M:%S")
        try:
            with psycopg.connect(host=HOST, dbname=DATABASE, user=USER, password=token,
                                 sslmode="require", connect_timeout=20,
                                 application_name="cloudlens-watch") as con:
                con.autocommit = True
                with con.cursor() as cur:
                    cur.execute("SET statement_timeout = '20s'")
                    cur.execute("SELECT key, value FROM meta WHERE key LIKE 'data_%' ORDER BY key")
                    meta = dict(cur.fetchall())
                    cur.execute(ACTIVITY)
                    activity = cur.fetchall()
                    cur.execute(DERIVED)
                    derived = cur.fetchall()
        except Exception as exc:  # noqa: BLE001 - a watcher must not die with its subject
            print(f"[{stamp}] probe failed: {type(exc).__name__}: {str(exc)[:120]}", flush=True)
            time.sleep(60)
            continue

        data = meta.get("data_generation")
        ready = meta.get("data_rollup_generation")
        line = f"[{stamp}] data_generation={data} rollup_generation={ready}"
        if ready == data:
            print(line + "   <-- ROLLUP IS CAUGHT UP", flush=True)
            for row in derived:
                print(f"           {row[0]:<28} {row[1]:>12,} rows  {row[2]}", flush=True)
            return 0

        current = (tuple(a[3] for a in activity), tuple(d[1] for d in derived))
        print(line, flush=True)
        for app_name, state, secs, query in activity:
            print(f"           {app_name or '(autovacuum)':<14} {state:<8} {secs:>5}s  {query}",
                  flush=True)
        if current != previous:
            for row in derived:
                print(f"           {row[0]:<28} {row[1]:>12,} rows  {row[2]}", flush=True)
        previous = current
        time.sleep(60)

    print("watch window expired without the rollup catching up", flush=True)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
