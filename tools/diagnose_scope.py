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
        with psycopg.connect(conninfo()) as con:
            while not stop.is_set():
                try:
                    rows = con.execute(
                        "SELECT pid, state, wait_event_type, wait_event, "
                        "extract(epoch from now() - query_start), "
                        "extract(epoch from now() - xact_start), query "
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
    # always present is being run over and over.
    by_sql: dict[str, dict] = defaultdict(
        lambda: {"pids": set(), "active": 0, "idle": 0, "max_age": 0.0, "waits": defaultdict(int)})
    states: dict[str, int] = defaultdict(int)
    for _, pid, state, wtype, wevent, age, _xage, sql in samples:
        key = shape(sql or "")
        entry = by_sql[key]
        entry["pids"].add(pid)
        states[state or "?"] += 1
        if state == "active":
            entry["active"] += 1
            entry["max_age"] = max(entry["max_age"], float(age or 0))
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
        print(f"\n   {secs:7.1f}s active  across {len(entry['pids'])} backend(s)  "
              f"longest single execution {entry['max_age']:.2f}s")
        top = sorted(entry["waits"].items(), key=lambda kv: -kv[1])[:3]
        print(f"      waits: " + ", ".join(f"{w} x{n}" for w, n in top))
        print(f"      {key}")

    print("\nidle backends holding a statement (text is the LAST one, not a running one):")
    for key, entry in sorted(by_sql.items(), key=lambda kv: -kv[1]["idle"])[:3]:
        if entry["idle"]:
            print(f"   {entry['idle'] * INTERVAL:7.1f}s idle  {key}")


if __name__ == "__main__":
    main()
