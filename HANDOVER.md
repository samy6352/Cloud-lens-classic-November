# CloudLens — handover, 1 October 2026 (16:00 IST)

Supersedes the 30 Sep → 1 Oct handover, kept as `HANDOVER-2026-09-30.md`. That file still
holds the architecture (its §4), the per-tab performance history (§5, §5a, §5b) and the
paging surveys; they are not repeated here. `HANDOVER-2026-09-28.md` is older still.

**Start here:** §0 (rules), §4 (decisions Ravi has already made — do not reopen them),
§6 (what is left). §9 is the literal first moves.

**State:** nothing in flight. No server, test run, deploy or build is running. `main` is
pushed (`git rev-list --count backup/cloud-lens-v2/main..main` = 0 after this commit is
pushed). Scale runs `fa481a1` application code plus `CLOUDLENS_FX_ROLLUP=true`; `main` is
that code plus handover/infra commits only. Every item on the 30 Sep pending list is
either done, decided against by Ravi, or listed in §6.

---

## 0. Read this first

1. **Deploy to Scale only** — `cloudlens-web-scale`, resource group `CloudLens-Scale`.
   `cloudlens.site` (old B1 stack in `cloud-lens-cx`) is **not** to be touched.
2. **Generic fixes only.** The HDFC data is a demo; the code must run on live customer
   estates. Everything goes through the app's own import and derived-rebuild pipeline —
   no scripts tuned to this dataset, no hand-built tables.
3. **Speed over freshness** is the agreed policy: data up to 24h old is fine; fresh data
   only on Refresh. Snapshot TTL 24h.
4. **Parameterised SQL in the app**, not stored procedures; the lever is summary tables
   built at import time.
5. **Never edit source while pytest runs** — tests read and `exec` source. Run the full
   suite detached (§1).
6. **Ask Ravi before every `git push`.** `main` goes to `backup/cloud-lens-v2/main`
   (private `samy6352/CloudLensv2`); Git Credential Manager holds credentials. Nothing to
   Saby007.
7. **The live admin password is never written down** — not in files, commits or logs.
   Ravi typed it in the 1 Oct chat and chose **not** to rotate it for now; ask him for it
   and pass it only in the process environment (`AUTH_PASSWORD`).
8. **Live database is production-shaped: read-only unless Ravi says otherwise.** Ravi
   declined `VACUUM FULL` on July (§4). See §1 for the DDL trap that bit on 1 Oct.
9. **`D:\cloud lens v3` belongs to another workstream.** Its own handover governs it
   (its push is two hops and needs Ravi). Only touch it if asked — the one 1 Oct change
   there (`aac1314`) was at Ravi's request.

---

## 1. Running, testing, deploying

Repo: `C:\Users\raviverma\OneDrive - Microsoft\Documents\Microsoft Scout\cloud-lens-v2`
Venv: `C:\Users\raviverma\.venvs\cloudlenstony\Scripts\python.exe`

**Python tests** (the suite refuses an implicit database):

```powershell
$env:CLOUDLENS_PG_TEST='localhost'; $env:CLOUDLENS_PG_USER='postgres'
$env:PGPASSWORD='6352'; $env:PGSSLMODE='disable'
python -m pytest tests\test_usage_summaries.py -q        # targeted
python -m pytest -q -n 4                                 # full: 2251 passed, 0 failed, 10 skipped
```

- Full suite at `fa481a1`: **2251 passed, 0 failed** in 12–20 min with `-n 4`. Run it
  detached (`Start-Process pwsh … *> log`) — an attached run dies with the chat.
- Do not pass `-p no:cacheprovider` (conftest needs `config.cache`).
- Lint: `python -m ruff check app tests\<files>`.

**Browser suites** (31 Playwright suites, need a local server on 8100):

```powershell
python -m uvicorn app.main:app --host 127.0.0.1 --port 8100     # from the repo, detached
$env:CLOUDLENS_URL='http://localhost:8100'; $env:CLOUDLENS_USER='admin'
$env:CLOUDLENS_PASSWORD=<AUTH_PASSWORD from the repo .env — the LOCAL one>
node tests/run_browser_suites.mjs [name-filter ...]
```

- Baseline **24/31** (1 Oct 17:30 IST, `332c9e1`, warm server). Still failing: dashboard and
  reporting_period (fixture drift, part-fixed, see §6 item 2), month_hover, sidebar,
  ui_responsive (timeouts, not yet triaged), focus_schedules and focus_workflow (they test
  `renderLegacySettings` in `dashboard-reports.js`, which nothing calls since the 16 Sep
  snapshot: dead UI). cost_data flakes occasionally; rerun before believing it.
- After a code change the snapshot cache is cold: Executive took 111s locally on the first
  view, past the runner's 120s readiness wait. Warm it (one signed-in GET) before running.

**Local database** (`localhost`, db `cloudlens`) is shared with v3. Check
`pg_stat_activity` before timing anything. The v2/v3 table-name clash is fixed (v3
`aac1314` prefixes its tables `resource_rank_*`); if v2 ever fails at boot with
`"resource_ids" is not a view`, an older v3 build ran. Local `.env` sets
`CLOUDLENS_ENV_ROLLUP=true`, `CLOUDLENS_FX_ROLLUP=true`, `CLOUDLENS_READ_INDEXES=true`,
`CLOUDLENS_DERIVED_LAYOUT=monthly` — **Scale differs** (§2), so a local number is not a
Scale number without checking the routing.

**Deploy** (~3 min):

```powershell
.\tools\package.ps1 -Destination <zip> -SourceRoot (Get-Location).Path -Python <venv python>
az webapp deploy -g CloudLens-Scale -n cloudlens-web-scale --src-path <zip> --type zip --async false
```

- The CLI sometimes prints only `InputStream`; check
  `az webapp log deployment list -g CloudLens-Scale -n cloudlens-web-scale` (status 4 = done).
- Do not change an app setting and deploy back to back (a setting change restarts Kudu).
- On startup the app builds any derived table missing from the stamp by itself, while the
  rest keep serving; the cache keeps serving too (its key is the data generation, which a
  missing-table build does not change). Watch with `pg_stat_activity` and
  `meta.data_rollup_tables`.

**Live database, read-only probing:**

- Start it first if MCAPS stopped it overnight (it does — 30 Sep 00:35 IST, principal
  `da847d8c…`): `az postgres flexible-server start -g CloudLens-Scale -n cloudlens-postgres-scale`
  (~5 min; expect cold buffers — timings right after are 1.5× worse).
- Firewall rule `ScoutSeed-20260928` covers `223.181.21.208`.
- Password = `az account get-access-token --resource https://ossrdbms-aad.database.windows.net`,
  user `raviverma_microsoft.com#EXT#@fdpo.onmicrosoft.com`, db `cloudlens`, `sslmode=require`,
  and add `options="-c default_transaction_read_only=on"` for anything catalog-only.
- **Trap:** `Warehouse()` runs DDL on construction (`_init_schema`), and
  `CLOUDLENS_READ_INDEXES=true` (local `.env`) makes a reader `CREATE INDEX`. On 1 Oct a
  profile did that on live for ~6 min before it was cancelled (rolled back). To run app code
  against live use `…\79e1dc8d-…\files\live_tags_profile.py`: live's own settings only,
  `_init_schema` skipped, a client-side guard refusing any non-`TEMP` write.

**Live benchmark:** `tools\bench_tabs.py <groups>` with
`BENCH_BASE=https://cloudlens-web-scale.azurewebsites.net`, `AUTH_USERNAME=admin`,
`AUTH_PASSWORD=<live password from Ravi>` in the process environment.

**Cold compute on live without any password:** `warm_compare.py snap <file>` before a
deploy, `watch <before> <after>` after it (server's own timer per warmed view, read-only).
Insights stores per-source `timing_ms` in its body (`private_snapshots`, filter
`byte_size < 300000`).

---

## 2. Live environment (Scale)

| | |
|---|---|
| App | `cloudlens-web-scale`, plan B2, Central India, one uvicorn worker, Always On |
| Database | `cloudlens-postgres-scale`, Standard_E2ds_v5, PostgreSQL 18, 2 vCPU, 128GB (67GB used), collation `en_US.utf8` |
| App settings | `PG_WORK_MEM=128MB`, `PG_STATEMENT_TIMEOUT=180s`, `SNAPSHOT_CACHE_ENABLED`, `SNAPSHOT_WARMING_ENABLED`, `SNAPSHOT_SERVER_WARMING_ENABLED`, **`CLOUDLENS_FX_ROLLUP=true` (since 1 Oct 14:24 IST)**, `CLOUDLENS_PARTITION_COSTS=1`, `AUTH_ALLOW_LOCAL=true`, `AUTO_INGEST=false`, `AUTO_REFRESH=false`. **Not set:** `CLOUDLENS_ENV_ROLLUP`, `CLOUDLENS_READ_INDEXES`, `CLOUDLENS_DERIVED_LAYOUT` |
| Data | 16.9M cost rows, 2026-07-01 → 2026-09-28, 70 subscriptions (HDFC demo, 99.99% INR + 3 small USD subscriptions of Ravi's). All 70 end 28 Sep except one (6 Sep) |
| Derived tables | 12, all stamped, incl. `costs_fx_rollup` (built 1 Oct 09:57–10:03 UTC) |
| Sign-in | Entra SSO shows the user's own RBAC subscriptions (Ravi: 1). Local `admin` sees all 70 |
| App identity | `cloudlens-id-scale`: Reader + Cost Management Reader on **one** subscription. Local `admin` has no delegated token, so its live Azure calls (Advisor, Resource Graph, Monitor) see that one subscription |

**Deployed:** `fa481a1` (1 Oct 13:13 IST). Previously `ac9bf8e` (1 Oct 03:03 IST),
`f1b2446` (30 Sep). `infra/modules/stack.bicep` now carries `CLOUDLENS_FX_ROLLUP=true`
(`0989c4f`), so a redeploy from the template keeps it.

---

## 3. What 1 October changed

| Commit | What | Measured |
|---|---|---|
| `8cdae1a` | Insights reads untagged spend from `tags.untagged_summary` instead of building the whole Tags tab; same ranking, cut, currency and tag lookup as `cost_by_tag` (tests hold them equal) | Tags source standalone on live 31s → 5.9s. Inside Insights still ~20–27s cold (8 sources share 2 vCPUs); repeat views are cached (~0.1s) |
| `54afda3` | Write-time snapshot eligibility check (`_eligible`) runs on a worker thread | Longest event-loop stall on the real 15.3MB Resources body 1,766ms → 247ms |
| `a171364` | The seven long-standing test failures: all harness drift (deferred `summary()`, a hard-coded date, a missing `log` global, a fake reader without `budget_ms`, renamed variables in a source check) | Full suite 0 failures since |
| `ac9bf8e` | `/api/schedules` deliberately not snapshot-cached (comment records why) | Its ARM cache answers repeats in 0.08s |
| `fa481a1` | **Tags and Uptime honour the filter bar.** Uptime: all five dimensions (its summary holds them). Tags: resource group / service / region; charge type and pricing are skipped, returned as `filters_skipped`, and named in a banner (`resource_presence` has no such columns — applying them timed out >110s). Environment filter hidden while unresolvable with nothing selected | Live: Tags `service=` 1.7s / 47 rows; Uptime `service=` 3.4s; unfiltered unchanged (Tags 17.8s, Uptime 7.6s cold) |
| `0989c4f` | `CLOUDLENS_FX_ROLLUP=true` in the Bicep template (setting applied on Scale first) | Demo account fix, §5 |
| v3 `aac1314` | (in `D:\cloud lens v3`, local only) v3's ranking tables renamed `resource_rank_ids/_subs/_raw/_daily`; existing tables renamed in place | v2 then rebuilt its own `resource_costs`/`resource_ids` (542s, ~4.3GB on C). Both apps coexist |

Plus handover commits. New tests: `tests/test_usage_summaries.py` (untagged summary vs tab
at three scopes, routed and on detail, truncated and converted; filtered Tags/Uptime vs
hand-narrowed answers and summary vs detail; skipped filters), `test_snapshot_cache.py`
(eligibility off the loop), `browser_reliability.js` (hidden unavailable Environment).

---

## 4. Decisions Ravi has made — do not reopen

| Decision | Date | Note |
|---|---|---|
| **No server-side paging** | 1 Oct | Live repeat views: Resources 0.30s, Tags 0.51s, Uptime 0.25s; 45 endpoints all < 2s warm (worst `/api/anomalies/alerts` 1.02s) |
| **Drop waste/rightsizing cold-time work** | 1 Oct | Only the background warmer pays it; users get Insights from cache |
| **No `VACUUM FULL` on the July partition** | 1 Oct | `costs_202607` is 2× bloated (imported twice: 11.64M inserted, 5.81M deleted; 4,188 B/row vs ~1,820). Known, accepted |
| **No full local database copy** for v2/v3 (disk) | 1 Oct | Solved by renaming v3's tables instead |
| **Tags: skip charge type / pricing, with a banner** | 1 Oct | Rather than rebuilding the per-resource summary |
| **Hide the Environment filter while unavailable**; resolve `env` at ingest later | 1 Oct | |
| **`dashboardReady` stays gated on Executive; `chart.umd.min.js` stays eager** | 1 Oct | Executive is cached (~0.07s); ~25 suites depend on the contract; no chart pop-in |
| **Enable `CLOUDLENS_FX_ROLLUP` on Scale** | 1 Oct | Done. `CLOUDLENS_ENV_ROLLUP` deliberately **not** enabled: it alters `costs_rollup` in place and needs a versioned rollback to undo |
| **Admin password: not rotating for now** | 1 Oct | Still never write it down |

---

## 5. Verified on live, 1 October

- **Entra sign-in warming works** (Ravi's SSO, oid `75aa28a0…`). The deploy's startup pass
  skips Entra users' targets as `no-identity` (sessions do not survive a restart — by
  design); each Entra sign-in runs a `sign-in` pass over exactly that user's targets. The
  first page after sign-in is still cold (Executive 14.1s) because the pass starts at the
  same moment the browser asks — single-flighted, not computed twice. `d3a6fcb` (shared ARM
  client) still cannot be measured end to end: Ravi's token sees one subscription.
- **Demo (local `admin`) account fixed.** Its browser keeps `costCurrency=INR` and 67 of 70
  subscriptions. Without the FX rollup every non-"as billed" currency converted from the
  detail table: Executive in INR **504 at 240s / 500 at 120–180s**, so the dashboard failed
  at the browser's 30s. After `CLOUDLENS_FX_ROLLUP=true`: Executive INR 16.9s cold → 0.1s,
  USD 11.8s → 0.1s; the demo browser ready in 18.4s cold, **0.42s** on reload.
- **The "30 Sep rows" are not a live problem.** Three of Ravi's own USD subscriptions
  (`e3d73cb0…`, `bd2e809d…`, `616dc9b8…`) are loaded by *quick refresh* (Cost Management
  Query API). Locally they were quick-refreshed on 29–30 Sep and so ended two days after
  the FOCUS-exported 67, which moved every *local* window. On Scale all end 28 Sep.
  Their empty `ChargeCategory` is by design (`QUICK_OMITS`), so a Cost type filter of
  "Usage" excludes their ~$7.1k on live.

---

## 6. Pending — in the order I would do it

| # | Item | Waits on |
|---|---|---|
| ~~1~~ | ~~Login page for the demo account~~ — **closed 1 Oct 16:40 IST, not a bug.** Live `/login` returns 200 and renders our page; App Service auth is off; nothing in `app/` or `web/login.html` redirects to Microsoft on its own; sign-out clears the session (`/api/auth/me` → `authenticated: false`); "Sign in with a local account" reveals the form in one click. The "bounce" was the 1 Oct chat firing sign-out, navigate, click and snapshot in parallel — the snapshot caught the old dashboard mid-navigation. The demo account's real failure was the INR timeout, fixed by `CLOUDLENS_FX_ROLLUP` (§5) | — |
| 2 | **Browser suites, 7 left** (§1). Done 1 Oct: six fixed (`332c9e1`), plus one real bug found and fixed: Tags drew two charts for a budget whose trend loads lazily (`226acb9`). Left: dashboard (next stall is the Billing refresh hold near line 713), reporting_period, month_hover, sidebar, ui_responsive; the two focus suites need Ravi's call: delete the dead `renderLegacySettings` UI and its suites, or retarget them at the Cost data tab | Nothing for the five; Ravi for the focus pair |
| 3 | **Quick refresh and `ChargeCategory`.** Either ask the Query API to group by `ChargeType` as well (check its grouping limit live), or say in the filter bar that quick-loaded subscriptions have no cost type | Nothing — small decision for Ravi on approach |
| 4 | **Window anchoring.** Every view ends its window on the newest day *in view*, so one freshly quick-refreshed subscription moves every user's window and shows the others' missing days as gaps. A generic rule (e.g. the newest day most of the estate has reached) needs Ravi's view | Ravi |
| 5 | **Environment filter for real:** resolve `env` at ingest (in `staging`, where rows are already touched once). The `stash@{0}` design rebuilt >24 min and was cancelled; `CLOUDLENS_ENV_ROLLUP` exists but alters `costs_rollup` in place | Ravi, if the filter is wanted |
| 6 | Charge type / pricing on Tags (add to the per-resource summary grain — a large rebuild) | Only if people need it |
| 7 | v3 `aac1314` push | v3 workstream / Ravi |

---

## 7. Measured dead ends and traps — do not repeat

From earlier sessions (detail in `HANDOVER-2026-09-30.md` §7): `try_cast_double` is not
Uptime's cost; `work_mem` 512MB did nothing for Resources; Tags cells gathered after the
top-N cut timed out; `COLLATE "C"` on the whole Uptime query (10+ min) or on the
Commitments drill-down (slower) — it helps grouping keys only; raising `cost._CONCURRENCY`;
negative-caching 401/403 subscriptions; two `asyncio.run()` scenarios in one process;
timing right after a deploy; measuring an Azure fan-out from the dev machine; measuring
warming right after a setting change.

New on 1 October:

- **Running app code against live with the local `.env`** — built an index on live
  (`CLOUDLENS_READ_INDEXES`). Use `live_tags_profile.py` (§1).
- **Reading `warm_compare` right after the DB was started** — Resources looked +15.6s
  (45.7s); it was cold buffers. Re-measured standalone: 17.8–18.3s SQL, 30.5s warm pass.
- **Comparing a filtered and unfiltered total and expecting filtered ≤ unfiltered** — the
  windows can differ (anchoring, §6 item 4). Compare per-row money for the same window.
- **Comparing Uptime/Tags rows by their `v` arrays** — `v` indexes the response's own value
  pool (`tag_values` / `values`), which shrinks with the answer. Resolve to strings first.
- **Applying charge/pricing filters to Tags** — detail table, >110s locally (§4).
- **A 13-suite browser failure after a code change** — first run the same suites on the
  previous frontend (copy the two assets from `HEAD`, run, restore); all 13 were
  pre-existing.
- **A full local DB clone** (`CREATE DATABASE … TEMPLATE`) — ~81GB; Ravi declined for disk.
- **Firing browser actions in parallel** (sign-out + navigate + click + snapshot) — the
  snapshot lands mid-navigation and invents a redirect. That is how §6 item 1 got opened.
  Run browser steps one after another.

---

## 8. Working agreements

- Measure before and after on live, statement by statement, before changing anything.
- Prove equivalence with tests (`tests/test_usage_summaries.py` pattern: routed vs forced
  onto `costs`, at three scopes).
- Prove a failure pre-existing by running it on the base commit.
- Commit messages explain *why*, with the numbers. Co-authored-by trailer on every commit.
- Explain findings to Ravi plainly, with a recommendation, and ask before anything that
  writes to live or pushes.

**Scratch** (not in the repo), `C:\Users\raviverma\.scout\copilot\session-state\`:

- `…\79e1dc8d-…\files\` (1 Oct): `live_tags_profile.py` (safe app-code profiling on live),
  `watch_fx_build.py` (follow a derived build on live, read-only), `warm_compare.py` and
  `warm-before/after-{ac9bf8e,fa481a1,sso}.json`, `bench_tags_summary.py`,
  `bench_filtered_tabs.py`, `bench_write_check.py`, `bench_loop_stall.py`,
  `v2_restore_derived.py`, `cloudlens-{ac9bf8e,fa481a1}.zip` (as deployed), suite logs
  `suite-ac9bf8e.log`, `suite-ui.log`, `browser-suites.log`, `browser-suites-baseline.log`.
- Older: `…\93969277-…`, `…\e27b8924-…` (`live_probe.py`, `live_watch.py`),
  `…\1ccfca3b-…` (30 Sep suite logs), `…\81c61fc4-…` (paging surveys, earlier deploys).

---

## 9. First moves in the new chat

1. `git status` (clean, or only `tools/bench-tabs-latest.json`) and `git log --oneline -6`
   — top should be this handover, then `f673e0a`, `0989c4f`, `c69fd56`, `919be08`, `61fb0e6`.
   `git rev-list --count backup/cloud-lens-v2/main..main` shows what is unpushed.
2. If touching live: start the database if stopped (§1); confirm the firewall IP.
3. Ask Ravi which item from §6 to take; item 1 is closed (not a bug), item 2 is next.
4. Before believing any number, read §7.
