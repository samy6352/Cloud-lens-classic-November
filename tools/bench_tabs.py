"""Every tab's endpoint, timed cold and warm, against the running local server.

Endpoint timings rather than browser page loads: on this box the 16.9M-row estate
does not fit in memory, so page-load numbers move by 3x between identical runs
while endpoint timings stay comparable.

Each endpoint is hit twice. The first pays whatever the snapshot cache has not
got; the second is what a second viewer sees. Both matter and they answer
different questions -- a tab that is fast only when warm is still a tab someone
waits for.

Usage:
    python tools/bench_tabs.py                 # every tab
    python tools/bench_tabs.py spend           # one group
    python tools/bench_tabs.py /api/overview   # explicit paths
"""
from __future__ import annotations

import json
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

BASE = os.environ.get("BENCH_BASE", "http://127.0.0.1:8100")
USER = os.environ.get("AUTH_USERNAME", "admin")
PASSWORD = os.environ["AUTH_PASSWORD"]
DAYS = os.environ.get("BENCH_DAYS", "30")

# Grouped the way the rail is, so a regression can be attributed to a tab rather
# than to "the dashboard". `shell` is what every tab pays before its own call.
GROUPS: dict[str, list[str]] = {
    "shell": [
        "/api/auth/me",
        "/api/health",
        "/api/subscriptions",
        "/api/onboarding",
        "/api/warehouse",
        "/api/currency",
        "/api/reporting-period",
        "/api/dashboard/sections",
        f"/api/filters?days={DAYS}",
        "/api/capabilities",
    ],
    "spend": [
        "/api/overview",
        f"/api/dashboard/executive?days={DAYS}",
        f"/api/dashboard/billing?days={DAYS}",
        f"/api/dashboard/billing/opportunities?days={DAYS}",
        f"/api/dashboard/uptime?days={DAYS}",
        f"/api/dashboard/tags?days={DAYS}",
        f"/api/budgets?days={DAYS}",
        f"/api/dashboard/resources?days={DAYS}",
        f"/api/dashboard/aks?days={DAYS}",
        f"/api/dashboard/ai-billing?days={DAYS}",
    ],
    "monitor": [
        f"/api/dashboard/anomalies?days={DAYS}",
        f"/api/dashboard/change-analysis?days={DAYS}",
        f"/api/dashboard/performance?days={DAYS}",
        "/api/dashboard/azure-updates",
        "/api/dashboard/service-health",
        "/api/anomalies/alerts",
    ],
    "optimize": [
        f"/api/dashboard/savings?days={DAYS}",
        f"/api/dashboard/savings/progress?days={DAYS}",
        f"/api/dashboard/recommendations?days={DAYS}",
        f"/api/dashboard/waste?days={DAYS}",
        f"/api/dashboard/shutdown?days={DAYS}",
        f"/api/dashboard/commitments?days={DAYS}",
        f"/api/dashboard/rightsizing?days={DAYS}",
        f"/api/dashboard/hybrid-benefit?days={DAYS}",
        f"/api/dashboard/rates?days={DAYS}",
        f"/api/dashboard/advisor?days={DAYS}",
    ],
    "govern": [
        f"/api/dashboard/governance?days={DAYS}",
        f"/api/dashboard/esu?days={DAYS}",
        "/api/dashboard/health",
        f"/api/dashboard/audit?days={DAYS}",
    ],
    "reports": [
        f"/api/report/options?days={DAYS}",
        "/api/archive?limit=200",
        "/api/cost-data/subscriptions",
        "/api/schedules",
        "/api/insights",
    ],
}

args = [a for a in sys.argv[1:]]
if args and all(a in GROUPS for a in args):
    paths = [p for a in args for p in GROUPS[a]]
elif args:
    paths = args
else:
    paths = [p for group in GROUPS.values() for p in group]

# A tab a person waits more than this for is a defect, not a slow query.
BUDGET_WARM = float(os.environ.get("BENCH_BUDGET", "2.0"))

with httpx.Client(base_url=BASE, timeout=600.0, follow_redirects=False) as c:
    r = c.post("/api/auth/login", json={"username": USER, "password": PASSWORD})
    print(f"sign-in: {r.status_code}", flush=True)
    if r.status_code != 200:
        print(r.text[:400])
        raise SystemExit(1)

    rows = []
    print(f"\n{'endpoint':<52}{'cold':>9}{'warm':>9}  status      bytes", flush=True)
    print("-" * 92, flush=True)
    for path in paths:
        timings = []
        status = size = None
        error = ""
        for _ in range(2):
            t = time.perf_counter()
            try:
                resp = c.get(path)
                status, size = resp.status_code, len(resp.content)
            except Exception as exc:  # noqa: BLE001 - a hung tab is a result
                status, size, error = "ERR", 0, type(exc).__name__
            timings.append(time.perf_counter() - t)
        flag = ""
        if status != 200:
            flag = f"   <-- {status} {error}"
        elif timings[1] > BUDGET_WARM:
            flag = "   <-- SLOW"
        print(f"{path:<52}{timings[0]:>8.2f}s{timings[1]:>8.2f}s"
              f"  {str(status):<6}{size:>11,}{flag}", flush=True)
        rows.append({"path": path, "cold": round(timings[0], 3),
                     "warm": round(timings[1], 3), "status": status, "bytes": size})

    slow = [r for r in rows if r["status"] == 200 and r["warm"] > BUDGET_WARM]
    broken = [r for r in rows if r["status"] != 200]
    print("-" * 92)
    print(f"{len(rows)} endpoints | {len(broken)} not-200 | {len(slow)} over {BUDGET_WARM:.1f}s warm")
    if rows:
        worst = sorted(rows, key=lambda r: -r["warm"])[:8]
        print("\nworst warm:")
        for r in worst:
            print(f"  {r['warm']:>7.2f}s  {r['path']}")
    out = ROOT / "tools" / "bench-tabs-latest.json"
    out.write_text(json.dumps(rows, indent=2), encoding="utf-8")
    print(f"\nwritten: {out}")
