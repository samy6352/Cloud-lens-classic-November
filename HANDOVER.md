# CloudLens — handover, 1 October 2026 (16:00 IST), updated 2 October 15:00 IST

**Release approved, 2 Oct 18:17 IST:** Ravi approved committing these browser/archive
changes, deploying **Scale only**, taking a new whole-estate archive today (not
reconstructing 1 Oct), and pushing `main:cloud-lens-v2/main` to `backup`.
Release validation: streaming/raw-export/browser-runner regressions **16 passed**;
archive/browser/visual selection **16 passed, 1 Azure-gated script skipped**; Ruff and
`git diff --check` clean. The earlier full standard browser run remains **31/31**.
Scale is Running, B2, Always On. Existing Windows SSO signs Ravi in as an app admin;
no local/live password was used. Predeployment `GET /api/archive` reports storage
**403/unreachable** for `stf2e87216abb1b0fa9f64/cloudlens`; production publication
may be blocked even after successful extraction. Final release results follow here.

**Update, 2 Oct 15:00 IST — read §3a first.** Since this file was written: quick refresh
fills cost type (`5f178dd`), report windows anchor on the spend-weighted edge (`180e3d2`),
both deployed to Scale; `e3d73cb0` was quick-refreshed on live; overnight the Scale plan
was found downgraded to **F1** and the database stopped — both restored. §6 items 3 and 4
are closed.

Supersedes the 30 Sep → 1 Oct handover, kept as `HANDOVER-2026-09-30.md`. That file still
holds the architecture (its §4), the per-tab performance history (§5, §5a, §5b) and the
paging surveys; they are not repeated here. `HANDOVER-2026-09-28.md` is older still.

**Start here:** §0 (rules), §4 (decisions Ravi has already made — do not reopen them),
§6 (what is left). §9 is the literal first moves.

**State (2 Oct 15:00 IST):** nothing in flight. No server, test run, deploy or build is
running. `main` is pushed up to `180e3d2`; this handover update is the only commit on top.
Scale runs `180e3d2` (deployed 1 Oct 21:31 IST) plus `CLOUDLENS_FX_ROLLUP=true`. Every item on the 30 Sep pending list is
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

- Baseline **24/31** (1 Oct 17:30 IST, `332c9e1`, warm server). Then failing: dashboard and
  reporting_period (fixture drift, part-fixed, see §6 item 2), month_hover, sidebar,
  ui_responsive (timeouts, not yet triaged), focus_schedules and focus_workflow (they test
  `renderLegacySettings` in `dashboard-reports.js`, which nothing calls since the 16 Sep
  snapshot: dead UI). cost_data flakes occasionally; rerun before believing it.
- **2 Oct isolated follow-up:** dashboard, month_hover and sidebar fixtures repaired;
  reporting_period and ui_responsive each pass unchanged in four fresh contexts.
  Both focus suites are retargeted to Cost data per Ravi's decision and pass locally.
  **Latest standard-runner baseline: 31/31**, 2 Oct, on the warm real local app.
  All seven historical failures and the four follow-up failures are resolved
  (see §6). Changes remain local and uncommitted.
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
| Data | 16.9M cost rows, 2026-07-01 → 2026-09-28, 70 subscriptions (HDFC demo, 99.99% INR + 3 small USD subscriptions of Ravi's). All end 28 Sep except one (6 Sep) and `e3d73cb0`, quick-refreshed 1 Oct to **1 Oct** (§3a). Estate window still ends 28 Sep (`180e3d2`) |
| Derived tables | 12, all stamped, incl. `costs_fx_rollup` (built 1 Oct 09:57–10:03 UTC) |
| Sign-in | Entra SSO shows the user's own RBAC subscriptions (Ravi: 1). Local `admin` sees all 70 |
| App identity | `cloudlens-id-scale`: Reader + Cost Management Reader on **one** subscription. Local `admin` has no delegated token, so its live Azure calls (Advisor, Resource Graph, Monitor) see that one subscription |

**Deployed:** `180e3d2` (1 Oct 21:31 IST). Before it `5f178dd` (1 Oct 18:28 IST),
`fa481a1` (1 Oct 13:13 IST), `ac9bf8e` (1 Oct 03:03 IST),
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

## 3a. What 1 Oct evening → 2 Oct changed

| Commit / action | What | Measured / verified |
|---|---|---|
| `226acb9`, `332c9e1` | Tags budget double chart fixed; six browser suites brought up to date (24/31 pass) | See §6 item 2 |
| `5f178dd` | **Quick refresh fills cost type.** The Query API also groups by `ChargeType` (six groupings work; seven too), mapped to `ChargeCategory`; dropped from `QUICK_OMITS`. `PricingModel` deliberately left out: the API returns "OnDemand" where full imports store nothing | Live API on all three quick subs: same row counts, totals to the cent, values Usage/Purchase as full imports |
| `180e3d2` | **Window anchoring (Ravi chose spend-weighted majority).** `reporting.edge_sql`: the end day is the newest day reached by subscriptions holding ≥ half of the last `EDGE_WEIGHT_DAYS`=7 days' spend (USD where present, else BilledCost). A quiet/deleted sub carries no weight; a single sub in view gets its own last day. `period_sql(hi, days)` bounds windows on **both** ends (most views only had `> start`); calendar-month mode ends at month end. New `tests/test_window_anchor.py`; five test stubs keyed on `AS hi` | Local: edge 28 Sep vs max 30 Sep (3 subs ~$350/wk vs ~$270k/wk), 0.1s on rollup, 18s on `costs`. Live after refresh: edge **28 Sep**, max 1 Oct. Offline suite 1,682 passed; **PostgreSQL suite not run on this commit** |
| Live quick refresh | `e3d73cb0`, ActualCost, `months=4` (so July is included), via `/api/ingest` as Ravi's SSO (the only sub it can write). Job `12122d0d…`, generation 3, all 12 derived tables rebuilt (~2h on live: started ~21:50, job finished 23:55 IST) | Jul $872→$459.32, Aug $494.09 (unchanged), Sep $875.53 to 30 Sep, Oct $20.92; no blank cost type. Ravi's view (1 sub) ends 1 Oct, as the rule intends. **Archive step failed** (statement timeout writing `actual/2026/10/actual-2026-10-01.parquet`) — data unaffected |
| Overnight outage | ~00:15 IST 2 Oct: DB **stopped** (governance) and the App Service plan **downgraded B2 → F1**, app `QuotaExceeded` / 403, Always On off. Activity log showed no caller for either | DB started 00:34 IST (Ravi's account). 2 Oct 14:00 IST plan back to **B2**, Always On re-enabled, app 200, data verified signed in |

The other two quick-refreshed subscriptions (`bd2e809d…`, `616dc9b8…`) still have blank
cost type on live; Ravi's sign-in cannot refresh them (local `admin` can, with the password).

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
| **Window end day = spend-weighted majority** (not subscription majority, not "every sub") | 1 Oct | `180e3d2` |
| **Quick refresh: fetch `ChargeType`**, not a filter-bar note | 1 Oct | `5f178dd` |
| **Restore Scale to B2** after the F1 downgrade | 2 Oct | Done 14:00 IST |
| **Retarget both legacy focus browser suites to Cost data; remove the dead renderer** | 2 Oct | Implemented locally; uncommitted, no deployment |

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
  Their empty `ChargeCategory` was by design (`QUICK_OMITS`), so a Cost type filter of
  "Usage" excluded their ~$7.1k on live. *(Superseded: `5f178dd` fills it; `e3d73cb0`
  re-refreshed on live, §3a.)*

---

## 6. Pending — in the order I would do it

| # | Item | Waits on |
|---|---|---|
| ~~1~~ | ~~Login page for the demo account~~ — **closed 1 Oct 16:40 IST, not a bug.** Live `/login` returns 200 and renders our page; App Service auth is off; nothing in `app/` or `web/login.html` redirects to Microsoft on its own; sign-out clears the session (`/api/auth/me` → `authenticated: false`); "Sign in with a local account" reveals the form in one click. The "bounce" was the 1 Oct chat firing sign-out, navigate, click and snapshot in parallel — the snapshot caught the old dashboard mid-navigation. The demo account's real failure was the INR timeout, fixed by `CLOUDLENS_FX_ROLLUP` (§5) | — |
| ~~2~~ | ~~Browser suites~~ — **closed locally: 31/31 pass through the standard runner**, 2 Oct. Fixtures updated, focus suites retargeted and unused renderer removed. Cold Executive still has its existing 30s application limit; harness now surfaces startup errors promptly instead of masking them. No commit/push/deploy | — |
| ~~3~~ | ~~Quick refresh and `ChargeCategory`~~ — **closed, `5f178dd`** (§3a). `bd2e809d`/`616dc9b8` still need a quick refresh on live to pick it up | Admin password for those two |
| ~~4~~ | ~~Window anchoring~~ — **closed, `180e3d2`** (§3a) | — |
| ~~4a~~ | ~~Run the PostgreSQL suite on `180e3d2`~~ — **closed 2 Oct: 447 passed, 1 skipped, 0 failed** in 598.63s. `pytest -q -n 4 -m "postgres and not azure"` against isolated schemas in local `postgres` (PostgreSQL 18.6); app/tests on HEAD match `180e3d2`. The retention skip requires the parent's explicitly isolated `cloudlens_envfx_test_6282cfcc` database. No live changes | — |
| ~~4b~~ | ~~Archive timeout~~ — **remediation implemented locally, 2 Oct**: named server cursor, no whole-estate sort/count, finite bulk limits, worker extraction and atomic block upload. Local regressions pass; **not deployed or production-scale measured**, no missed snapshot replay (details below) | Deployment requires Ravi's approval |

**Dashboard browser suite (2 Oct, local changes only):**

- Fixture drift only; no application changes. Billing's Refresh button is gated on
  the historical statement, not current opportunities. The held-opportunities check
  now waits for the loaded statement, enabled button and refreshing-estimates note.
  After interrupted navigation it waits for opportunity completion and verifies that
  only the unfinished opportunities request resumes, not the completed statement.
- Billing's expected subsections now include History (five, not four). Mobile
  orientation is checked on the nested `role="tablist"`, not its navigation wrapper.
- The complete existing `browser_dashboard.js` passed twice consecutively in its
  own fully mocked context, each covering 54 header layouts and 54 premium layouts,
  with no failures/page errors. Related existing navigation/lifecycle tests: 12 passed.
- The standard runner's initial **real** dashboard readiness gate timed out twice
  before entering this mocked suite, including after warming. The suite was instead
  invoked directly with Playwright using a source page at the local `/assets/app.css`;
  all suite APIs remained mocked and local app assets were served unchanged. This
  does not constitute a new full-runner baseline; its bootstrap timeout remains open.
- Only `tests/browser_dashboard.js` and this handover changed. No commit, push or
  deployment. The temporary loopback server was stopped after the run.

**Reporting-period browser suite (2 Oct, unchanged):**

- Historical failure did not reproduce. The complete existing
  `browser_reporting_period.js` passed on the first run and three consecutive repeats,
  each in a fresh mocked context with its existing frozen September clock and
  America/Los_Angeles timezone. Each covers month selection, rolling presets,
  leap-year dates, persistence/reload, mobile calendar behaviour and 15 header layouts.
  No page errors. Existing reporting-period frontend regressions: 5 passed.
- Used only a loopback static server over `web` on port 8021 and invoked the existing
  suite with Playwright, using `/assets/app.css` as the source page. No application
  server, database or Azure calls; no source/test edits. The standard runner's
  real-dashboard readiness issue was bypassed, not fixed. No new full-runner baseline.
- Result log: session `1294072a-c1c5-49db-9ea0-0a63fe2c7aae`,
  `files/reporting-period-result.log`. Temporary static server stopped afterwards.
  Next individual suite at that point: month_hover (now repaired below).

**Month-hover browser suite (2 Oct, fixture-only repair):**

- Reproduced the 5s timeout at `locator("#cl-tooltip").hover()`. The fixture tried
  to hover a short, deliberately click-through chart tooltip. `tooltips.js` and
  the companion tooltip suite already establish that only scrollable content
  accepts pointer events; making short tips hoverable would regress chart interaction.
- `browser_month_hover.js` now explicitly verifies short tips are click-through,
  adds a long Chart.js footer to exercise a genuinely scrollable tooltip, checks
  hover readability/Escape, then restores the normal short content. No forced
  hover or application/CSS changes.
- Complete suite passed three times, 67 assertions per run across three themes,
  stacked segments, negative values, horizontal bars and keyboard navigation.
  No page errors and zero hover requests. Companion `browser_tooltips.js` passed;
  existing monthly-chart frontend regressions: 2 passed.
- Used only the loopback static asset server on 8021; no app, DB or Azure calls.
  Result log: this session's `files/month-hover-result.log`. Temporary server
  stopped afterwards. No commit, push or deployment. Next individual suite at that
  point: sidebar (now repaired below).

**Sidebar browser suite (2 Oct, fixture-only repair):**

- Reproduced its wait for expanded sidebar width `>190px`. CSS has
  `--ui-scale:.8`, so the intended expanded rail is 160px, not an unscaled 200px.
  Expanded/collapsed expectations now apply the current scale to 200px/60px and
  require the resulting widths within 0.5px. Expected labels now include the
  seventh primary destination, All resources. Original readability, 40px row
  density, keyboard, persistence, contrast, account docking and 36 layouts remain.
- Waits for the account trigger before measuring initial layout. Initial/reload
  dashboard bootstrap gets the existing runner/responsive suite's 120s allowance;
  interaction waits remain 8s. Concurrent runs initially hit bootstrap timeouts
  against the small static asset server; final confirmation was serial.
- Complete sidebar suite passed twice serially after the final edit: 124
  assertions and 36 layouts per run, no page errors. Unchanged sidebar-theme
  companion passed serially across all four themes; 5 existing navigation tests passed.
- Actual HTML, app.js, dashboard.js, CSS, account wiring and images were unchanged.
  Session-only `files/sidebar_driver.mjs` supplies deterministic API responses
  to fresh browser contexts and rejects unexpected APIs/writes, while port 8021
  serves local frontend assets. No app server, database or Azure calls. This is
  isolated frontend coverage, not a new real-backend/full-runner baseline.
- Results: this session's `files/sidebar-result.log`. Static server stopped.
  No commit, push or deployment. Next individual suite at that point: ui_responsive
  (now confirmed passing below).

**Responsive UI browser suite (2 Oct, unchanged):**

- Historical failure did not reproduce. Complete `browser_ui_responsive.js` passed
  on the first run and three serial repeats. Each covers all nine combinations of
  light/dark/vivid at 600/390/320px, with no page overflow, pinned resource names,
  16px input text, 40px input/pagination controls and no page errors.
- Hover loads only the Resources module: zero resource data requests before click,
  exactly one afterwards; all 45 fixture rows are rendered through the actual
  frontend. Existing resource frontend and module-preload regressions: 5 passed.
- No application/CSS/test changes. Reused the session-only sidebar driver to mock
  bootstrap APIs; the suite's own resources route remains authoritative. HTML,
  JS and CSS served unchanged from loopback port 8021. No database or Azure calls.
  Logs: this session's `files/responsive-run1.log` through `responsive-run3.log`.
  Static server stopped afterwards. No commit, push or deployment.
- All five original non-legacy failures are now repaired or unreproduced in isolated
  frontend coverage. This does not close the real-backend runner bootstrap issue or
  establish a new 31-suite baseline. Ravi subsequently chose retargeting below.

**Legacy focus suites retargeted (2 Oct, Ravi approved):**

- `browser_focus_schedules.js` now exercises Scheduled updates in Cost data:
  scoped cadence preview, destination/side-effect review, cancel, draft invalidation,
  one token-bound/idempotent approval, queued-not-enabled status, readiness blockers,
  all 12 read-only external exports, empty inventory, read-only access and empty scope.
- `browser_focus_workflow.js` now exercises First-time import, ninety-day history
  plus automatic cadence, cancel, transient approval retry with the same key/token,
  queued/Azure-waiting/partial/ready coverage, cached stop/resume without catalog
  reload, cadence preservation, reviewed refresh cancellation, mobile layout and
  header routing to Cost data without an implicit write.
- Shared `tests/cost_data_browser_fixture.mjs` uses actual reports JS and CSS,
  mocks every fixture request, records previews separately from approvals, and
  rejects unexpected APIs/writes. The existing runner supplies it via suite options;
  direct invocation is documented in `tests/README.md`.
- Removed unused `renderLegacySettings`, its private pagination helper/state and
  stale renderer documentation. Kept other compatibility/header helpers untouched.
  Updated the coupled visual source test to the actual review/read-only inventory UI.
- Both retargeted browser suites passed twice (24 and 19 assertions respectively).
  Existing `browser_cost_data.js` also passed (64 assertions). Existing Cost data,
  visual, permissions, reports, period and shell tests: 50 passed across targeted runs.
  No live requests/writes. Session result: `files/focus-result.log`.
- Changes remain local and uncommitted. No push/deploy; temporary static servers
  stopped. Next remaining browser task: diagnose the real-dashboard readiness gate,
  then establish a full-runner baseline. Archive streaming remediation is still open.

**Standard runner startup and full baseline (2 Oct):**

- Real local diagnostic: cold `/api/dashboard/executive` exceeded its explicit
  30s browser deadline. The backend completed in **47.710s**. Browser set
  `dashboardReady="error"` with "The request timed out. Please retry." The
  harness waited only for `"true"` and therefore hid that error behind a 120s
  readiness timeout. Warm repeat completed startup in **0.416s**.
- `tests/browser_runner_readiness.mjs` now waits for either ready or error and
  surfaces the displayed error. The runner calls it; new targeted regression
  covers success, error, missing error text and pending/timeout semantics.
  Application timeouts/UX were not changed; the cold Executive limit remains.
- Ran all 31 suites through the normal runner against the warm real local app:
  **27 passed, 4 failed**, exit 1 (superseded by the 31/31 follow-up below). The original repaired suites and both
  retargeted focus suites pass in this baseline, not only isolated invocation.
- Remaining failure details, in runner order:
  `browser_compact.js`: readiness timeout (153s overall, 120s wait);
  `browser_sidebar_theme.js`: vivid/paper popover backgrounds both
  `rgb(16, 44, 55)` in this run;
  `browser_tag_dropdowns.js`: "Values no longer has its disabled grey background";
  `browser_ui_responsive.js`: 30s wait for Resources module response.
  Do not assume the isolated responsive pass closes its sequence-dependent failure.
- Exact run log: session `1294072a-c1c5-49db-9ea0-0a63fe2c7aae`,
  `files/browser-full.log`. No source was edited during the run. App was local
  with AUTO_INGEST/AUTO_REFRESH disabled; no live settings/data were changed.
  Temporary app server stopped after the run. All edits remain uncommitted,
  no push/deployment. The four follow-up failures were resolved below.

**Remaining browser fixes and final baseline (2 Oct):**

- `browser_compact.js` was still comparing the current assets with the same
  current assets as if they were a before/after redesign, demanding a 15% shrink.
  Removed ineffective unversioned asset overrides and that historical comparison.
  Retained the 320px evidence-header budget, desktop/mobile board-height and
  clipping/overflow checks, all primary views and cost areas, keyboard help and
  a no-growth reload check. Terminal view errors now fail promptly instead of
  waiting 120s for a success state that cannot arrive.
- Sidebar-theme and tag-dropdown colour checks sampled styles before the browser
  settled them. They now wait for the popover's actual theme token colour and for
  Values to leave its disabled background. Existing four-theme distinctness,
  contrast and enabled/disabled assertions remain; no application CSS change.
- Responsive preload was not sequence/cache failure: it only matched
  `/assets/dashboard-resources.js`, while the real app serves
  `/assets/v/<content-hash>/dashboard-resources.js`. Match both supported paths
  and require a successful response. The zero-data-on-hover and exactly-one-data-
  read-on-click checks remain.
- Targeted four-suite real-app run passed; adjacent existing regressions:
  27 passed. Then reran the **entire standard 31-suite harness: 31/31 passed,
  exit 0**, including compact (11.2s), sidebar_theme (3.6s), tag_dropdowns (6.3s)
  and ui_responsive (2.1s). No source edits during the run.
- Final log: this session's `files/browser-full-final.log`. Local app and temporary
  static server stopped after completion. No live settings/data changed.
  All edits remain uncommitted; no push/deployment. Browser item 2 is closed locally.
  Archive streaming remediation (4b) was subsequently implemented locally below.

**4b remediation (2 Oct, local implementation):**

- Daily whole-estate extraction now uses a named, non-holdable PostgreSQL cursor
  in one transaction/query snapshot. Rows are counted while writing, with no
  preliminary count or whole-estate date sort. Scoped archives retain the
  warehouse's enforced scope view. Daily row ordering is unspecified; interactive
  selection/download export ordering is unchanged.
- `ARCHIVE_QUERY_TIMEOUT_SECONDS=300` default applies locally to each cursor
  statement/fetch; `ARCHIVE_EXPORT_TIMEOUT_SECONDS=3600` bounds extraction and,
  separately, upload. Validated integer seconds (1-86400). Transaction-local
  settings restore the caller's timeout; dashboard limits remain unchanged.
- Parquet uses warehouse column types, so an all-null first batch cannot cause
  later non-null values to fail. Python/Arrow extraction runs off the event loop.
  Repeated cancellation waits for the actual file-owning worker to finish before
  deleting its temporary directory or closing its upload file.
- Upload reads 4 MiB blocks, gives them unique per-upload block IDs, then commits
  the complete block list atomically under the existing daily name. No complete
  Parquet byte buffer. Failed staging or empty exports do not replace a good blob.
  Disk space for the full compressed file is still needed.
- Targeted final run: 21 passed, including isolated PostgreSQL batching (2/2/1),
  concurrent-write snapshot consistency, scope, null-first schema, negative cost,
  timeout rollback/restoration, block bounds/order, failed-upload nonpublication,
  off-loop extraction, repeated cancellation cleanup and existing raw export/
  archive restatement compatibility. Lint clean. No production-scale run.
- README and .env.example document limits and ordering. Application archive code
  and new tests remain uncommitted with the earlier browser work. No push/deploy,
  no live settings/data changes, and no attempt to recreate the missed 1 Oct snapshot.

**4b findings (2 Oct, read-only live investigation):**

- Retained Scale log: `ingest complete: ready` at **1 Oct 18:22:11.584 UTC**;
  `archive failed: canceling statement due to statement timeout` at
  **18:25:14.371 UTC** (182.8s later). This was PostgreSQL cancellation, before blob upload,
  not a storage/network failure or the later overnight shutdown.
- `_archive_after_ingest` calls `archive_current` without a scope. `_export_parquet`
  therefore archives the **whole estate**, even when the refresh changed one subscription.
  Live holds **16,928,527 rows**; this is the intentional whole-estate daily archive, not
  a copy of only the new Query API rows.
- `_write_rows` first counts the rows, then executes
  `SELECT * FROM costs ORDER BY "ChargePeriodStart"`. The live EXPLAIN chooses parallel
  sequential scans across the partitions, a wide-row Sort and Gather Merge (estimated row
  width 1,820 bytes), despite the date indexes. The count completed in **1.879s** during
  this investigation. Combined with the original 182.8s interval, the ordered SELECT is
  the likely timeout; the original log does not record the SQL, so this attribution is
  not directly proved. No full export or EXPLAIN ANALYZE of that SELECT was run on live.
- `warehouse.connect(read_only=True)` applies live `PG_STATEMENT_TIMEOUT=180s`. The
  archive shares this dashboard limit despite being bulk work. `_cursor_batches`
  calls `fetchmany(50000)`, but `PostgresConnection.execute` uses an ordinary unnamed
  psycopg cursor: the database result is buffered before the Python batches are consumed,
  so this is **not bounded-memory database streaming**. `archive_current` also performs
  the synchronous extraction on the event loop and then reads the whole Parquet file
  into memory before upload.
- Recommended remediation: a genuine streaming bulk-export path with its own bounded
  timeout, counted as it writes; avoid a whole-estate blocking sort (or preserve ordering
  through bounded date/partition chunks), offload extraction, and use bounded-memory blob
  upload. Preserve whole-estate naming and snapshot consistency. Raising the global
  dashboard timeout or silently scoping the daily archive is not the fix.
- No live settings/data or application code changed. No archive was replayed: a new
  archive cannot honestly reconstruct the missed 1 Oct snapshot. This handover update is
  local and uncommitted; no push.
| 4c | **Who downgraded Scale to F1?** Check the activity log again (it was empty at 00:40 and 14:00 IST); if it is governance, it may repeat nightly | Ravi, if it recurs |
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
  windows can differ (anchoring is spend-weighted per scope since `180e3d2`). Compare
  per-row money for the same window.
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

New on 1–2 October evening:

- **Starting a live refresh late in the evening.** A quick refresh rebuilds all 12 derived
  tables on live (~2h). Governance stops the DB around 00:15 IST; start before ~21:30.
- **Polling `/api/warehouse` in one long browser `evaluate`** — the MCP call times out;
  poll `pg_stat_activity` read-only instead.
- **`pytest -p no:cacheprovider`** — `conftest.py` needs `config.cache`; INTERNALERROR.
- **No venv in the repo** — `package.ps1` takes `-Python`; omit it (defaults to `python`)
  or pass the venv in §1.

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
   — top should be this handover update, then `180e3d2`, `5f178dd`, `59e1e20`, `332c9e1`,
   `226acb9`. Check Scale is still B2 and Running (§3a outage).
   `git rev-list --count backup/cloud-lens-v2/main..main` shows what is unpushed.
2. If touching live: start the database if stopped (§1); confirm the firewall IP.
3. Items 1, 2, 3, 4, 4a, 4b are closed locally. Browser/renderer/archive/handover
   edits are uncommitted and not deployed. Archive still needs a controlled
   production-scale run after approved deployment; ask Ravi before any push.
4. Before believing any number, read §7.
