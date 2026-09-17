"""Which statement dominates a slow endpoint, sampled rather than guessed.

Reading the module and picking the query that looks expensive is how the last
round of assumptions went wrong: `billing` looked like an unrouted read and was
in fact a 540s query needing columns no grain carries. So this asks the server.

For each endpoint it fires one request against the probe instance and samples
`pg_stat_activity` while the request is in flight, attributing elapsed time to
whichever statement is active. The statement holding the most samples is the one
worth changing; anything else is noise.

Run against the probe instance (snapshot cache off, long statement timeout) so
that a slow endpoint reports its true cost instead of being cancelled at 120s.

    python tools/profile_endpoints.py <port> <endpoint> [<endpoint> ...]
"""
from __future__ import annotations

import os
import re
import sys
import threading
import time
from collections import Counter

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import psycopg  # noqa: E402
import urllib.request  # noqa: E402
from dotenv import load_dotenv  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
load_dotenv(os.path.join(ROOT, ".env"))

COOKIE = open(os.path.join(os.environ["TEMP"], "cl-cookie.txt")).read().strip()
INTERVAL = 0.25

# Which instance's backends to count. Two applications against one database are
# indistinguishable in `pg_stat_activity` unless they say who they are, and an
# earlier run of this tool attributed 56.5s to a statement the *other* instance
# was running. Start the instance under test with a matching PG_APPLICATION_NAME.
APPNAME = os.getenv("PROFILE_APPNAME", "cloudlens-probe")


def conninfo() -> str:
    return (f"host={os.getenv('PGHOST', 'localhost')} dbname={os.getenv('PGDATABASE', 'cloudlens')} "
            f"user={os.getenv('PGUSER', 'postgres')} password={os.getenv('PGPASSWORD', '')} "
            "sslmode=disable")


def shape(sql: str) -> str:
    """A statement's identity, short enough to group on."""
    return re.sub(r"\s+", " ", sql).strip()[:110]


# Where a statement's time belongs. Scope construction is separated from query
# execution because the two have different fixes: a slow shadow is paid once per
# connection no matter what is asked, a slow query is paid by the question.
SCOPE_MARKERS = ("CREATE TEMP VIEW", "CREATE TEMP TABLE", "INSERT INTO _scope",
                 "set_config", "DROP VIEW", "DISCARD")


def bucket(sql: str) -> str:
    upper = sql.upper()
    for marker in SCOPE_MARKERS:
        if marker.upper() in upper:
            return "scope/shadow"
    return "query"


def sample(stop: threading.Event, seen: Counter, texts: dict, buckets: Counter,
           since: float) -> None:
    """Sample active statements, ignoring any that predate this request.

    The `since` guard is not paranoia. An abandoned request leaves its query
    running on the server -- that is the whole reason `PG_STATEMENT_TIMEOUT`
    exists -- and on a probe instance with a long timeout one such query ran for
    542s and was attributed to two endpoints that had not issued it, making both
    look 95% dominated by a statement neither runs. Compare `query_start` against
    the moment this request began and count only what this request caused.
    """
    with psycopg.connect(conninfo()) as con:
        while not stop.is_set():
            try:
                rows = con.execute(
                    "SELECT query, extract(epoch from query_start) FROM pg_stat_activity "
                    "WHERE datname = current_database() AND state = 'active' "
                    "AND application_name = %s "
                    "AND pid <> pg_backend_pid() AND query NOT LIKE '%%pg_stat_activity%%'",
                    (APPNAME,)
                ).fetchall()
            except Exception:
                rows = []
            # One vote per distinct statement per tick: parallel workers run the
            # same query and would otherwise count it five times.
            live = {shape(r[0]) for r in rows if r[1] and r[1] >= since}
            for s in live:
                seen[s] += 1
                texts.setdefault(s, s)
            if live:
                # One tick of wall clock, attributed to the most expensive kind of
                # work in flight: a tick spent running a query is a tick the shadow
                # is already built.
                kinds = {bucket(s) for s in live}
                buckets["query" if "query" in kinds else "scope/shadow"] += 1
            stop.wait(INTERVAL)


def run(port: int, path: str) -> None:
    seen: Counter = Counter()
    texts: dict = {}
    buckets: Counter = Counter()
    stop = threading.Event()
    # A second of slack, because `query_start` is the server's clock and this is
    # the client's; a statement issued moments before must not be excluded.
    started = time.time()
    sampler = threading.Thread(target=sample,
                               args=(stop, seen, texts, buckets, started - 1), daemon=True)
    sampler.start()

    status = "?"
    try:
        req = urllib.request.Request(f"http://127.0.0.1:{port}{path}",
                                     headers={"Cookie": COOKIE})
        with urllib.request.urlopen(req, timeout=900) as resp:
            body = resp.read()
            status = f"{resp.status} {len(body)}B"
    except Exception as exc:  # noqa: BLE001
        status = f"FAILED {str(exc)[:60]}"
    elapsed = time.time() - started

    stop.set()
    sampler.join(timeout=5)

    print(f"\n{path}  ->  {status}  in {elapsed:.1f}s")
    # The split matters more than the statements. Time outside PostgreSQL cannot
    # be fixed by routing a query, and three endpoints measured at ~30s turned out
    # to spend none of it in the database at all.
    query = buckets["query"] * INTERVAL
    shadow = buckets["scope/shadow"] * INTERVAL
    other = max(0.0, elapsed - query - shadow)
    print(f"   PostgreSQL query   {query:7.1f}s ({query / elapsed * 100:4.1f}%)")
    print(f"   scope/shadow build {shadow:7.1f}s ({shadow / elapsed * 100:4.1f}%)")
    print(f"   outside PostgreSQL {other:7.1f}s ({other / elapsed * 100:4.1f}%)")
    if not seen:
        print("   -- no database activity sampled")
        return
    for statement, hits in seen.most_common(3):
        secs = hits * INTERVAL
        print(f"     {secs:6.1f}s  {texts[statement]}")


def main() -> None:
    port = int(sys.argv[1])
    for path in sys.argv[2:]:
        run(port, path)


if __name__ == "__main__":
    main()
