"""Endpoint timings against the running local server.

Endpoint benchmarks rather than browser page loads: on this box the database does
not fit in memory, so page-load numbers move by 3x between identical runs while
endpoint timings stay comparable.

Each endpoint is hit twice -- the first pays whatever the snapshot cache has not
got, the second shows what a second viewer sees.
"""
import os
import pathlib
import sys
import time

import httpx

ROOT = pathlib.Path(__file__).resolve().parents[1]
for line in (ROOT / ".env").read_text().splitlines():
    line = line.strip()
    if line and not line.startswith("#") and "=" in line:
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip())

BASE = "http://127.0.0.1:8100"
USER = os.environ.get("AUTH_USERNAME", "admin")
PASSWORD = os.environ["AUTH_PASSWORD"]

PATHS = [
    "/api/overview",
    "/api/dashboard/sections",
    "/api/filters?days=30",
    "/api/dashboard/executive?days=30",
    "/api/subscriptions",
    "/api/onboarding",
    "/api/dashboard/billing?days=30",
]
if len(sys.argv) > 1:
    PATHS = sys.argv[1:]

with httpx.Client(base_url=BASE, timeout=600.0, follow_redirects=False) as c:
    r = c.post("/api/auth/login", json={"username": USER, "password": PASSWORD})
    print(f"sign-in: {r.status_code}", flush=True)
    if r.status_code != 200:
        print(r.text[:400])
        raise SystemExit(1)

    print(f"\n{'endpoint':<44}{'cold':>10}{'warm':>10}  status  bytes")
    for path in PATHS:
        timings = []
        status = size = None
        for _ in range(2):
            t = time.perf_counter()
            resp = c.get(path)
            timings.append(time.perf_counter() - t)
            status, size = resp.status_code, len(resp.content)
        flag = "" if status == 200 else "   <-- NOT 200"
        print(f"{path:<44}{timings[0]:>9.2f}s{timings[1]:>9.2f}s"
              f"  {status}  {size:,}{flag}", flush=True)
