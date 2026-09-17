"""What the 56.5s attributed to `CREATE TEMP TABLE _scope` actually is.

Sampling the query text alone cannot tell these apart, and they have different
fixes:

    many short creations      one connection per question, each rebuilding a shadow
    one slow creation         a lock, or DDL behind something else
    a backend that is idle    the text is the *last* statement, not a running one
    waiting for a connection  the pool is exhausted and nothing is running at all

So this records, per sample: every backend's pid, state, wait_event_type/
wait_event, how long the current statement and transaction have been open, and
the statement itself. Then it reports how many distinct backends ran each
statement and how long any single execution actually lasted -- which is the
number that decides whether 56.5s is one slow thing or hundreds of fast ones.

    python tools/diagnose_scope.py <port> <endpoint>
"""
from __future__ import annotations

import os
import re
import sys
import threading
import time
from collections import defaultdict

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import urllib.request  # noqa: E402

import psycopg  # noqa: E402
from dotenv import load_dotenv  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
load_dotenv(os.path.join(ROOT, ".env"))

COOKIE = open(os.path.join(os.environ["TEMP"], "cl-cookie.txt")).read().strip()
INTERVAL = 0.05
APPNAME = os.getenv("PROFILE_APPNAME", "cloudlens-probe")


def conninfo() -> str:
    return (f"host={os.getenv('PGHOST', 'localhost')} dbname={os.getenv('PGDATABASE', 'cloudlens')} "
            f"user={os.getenv('PGUSER', 'postgres')} password={os.getenv('PGPASSWORD', '')} "
            "sslmode=disable")


def shape(sql: str) -> str:
    return re.sub(r"\s+", " ", sql).strip()[:80]


def main() -> None:
    port, path = int(sys.argv[1]), sys.argv[2]
    stop = threading.Event()
    samples: list[tuple] = []

    def sample() -> None:
        # Autocommit, because `now()` in PostgreSQL is the *transaction's* start
        # time. A sampling connection that holds one transaction freezes it, and
        # every `now() - query_start` is then measured from the moment sampling
        # began -- which reported a 9.3s statement as never older than 0.09s.
        # `clock_timestamp()` is the real clock and is used for the same reason.
        with psycopg.connect(conninfo(), autocommit=True) as con:
            while not stop.is_set():
                try:
                    rows = con.execute(
                        "SELECT pid, state, wait_event_type, wait_event, "
                        "extract(epoch from clock_timestamp() - query_start), "
                        "extract(epoch from clock_timestamp() - xact_start), query, "
                        "extract(epoch from query_start) "
                        "FROM pg_stat_activity WHERE datname = current_database() "
                        "AND application_name = %s AND pid <> pg_backend_pid() "
                        "AND query NOT LIKE '%%pg_stat_activity%%'", (APPNAME,)).fetchall()
                    now = time.time()
                    for r in rows:
                        samples.append((now, *r))
                except Exception:
                    pass
                stop.wait(INTERVAL)

    sampler = threading.Thread(target=sample, daemon=True)
    sampler.start()
    started = time.time()
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

    print(f"{path} -> {status} in {elapsed:.1f}s  ({len(samples)} samples)\n")

    # Per statement: which backends ran it, how many ticks it was active for, and
    # the longest single execution seen. A statement that is always young but
    # always present is being run over and over -- which `executions` counts
    # directly, by how many distinct start times were seen for it. That is the
    # number that separates one slow query from a hundred quick ones, and the two
    # have nothing in common as problems.
    by_sql: dict[str, dict] = defaultdict(
        lambda: {"pids": set(), "active": 0, "idle": 0, "max_age": 0.0,
                 "starts": set(), "waits": defaultdict(int)})
    states: dict[str, int] = defaultdict(int)
    for now, pid, state, wtype, wevent, age, _xage, sql, qstart in samples:
        key = shape(sql or "")
        entry = by_sql[key]
        entry["pids"].add(pid)
        states[state or "?"] += 1
        if state == "active":
            entry["active"] += 1
            entry["max_age"] = max(entry["max_age"], float(age or 0))
            # The server's own `query_start`, not one derived from the client's
            # clock. Deriving it counted 234 executions of a statement the request
            # issues once, because client and server clocks drift and every sample
            # looked like a fresh start.
            entry["starts"].add((pid, qstart))
            entry["waits"][f"{wtype or '-'}/{wevent or '-'}"] += 1
        else:
            entry["idle"] += 1

    print("backend states across all samples:")
    for state, n in sorted(states.items(), key=lambda kv: -kv[1]):
        print(f"   {n * INTERVAL:7.1f}s of backend-time  {state}")

    print("\nper statement (active samples only):")
    ranked = sorted(by_sql.items(), key=lambda kv: -kv[1]["active"])
    for key, entry in ranked[:6]:
        if not entry["active"]:
            continue
        secs = entry["active"] * INTERVAL
        runs = len(entry["starts"]) or 1
        print(f"\n   {secs:7.1f}s active  across {len(entry['pids'])} backend(s)  "
              f"~{runs} execution(s)  longest {entry['max_age']:.2f}s  "
              f"mean {secs / runs:.2f}s")
        top = sorted(entry["waits"].items(), key=lambda kv: -kv[1])[:3]
        print(f"      waits: " + ", ".join(f"{w} x{n}" for w, n in top))
        print(f"      {key}")

    print("\nidle backends holding a statement (text is the LAST one, not a running one):")
    for key, entry in sorted(by_sql.items(), key=lambda kv: -kv[1]["idle"])[:3]:
        if entry["idle"]:
            print(f"   {entry['idle'] * INTERVAL:7.1f}s idle  {key}")


if __name__ == "__main__":
    main()
