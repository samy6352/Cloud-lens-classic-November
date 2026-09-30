# CloudLens — handover, 30 September → 1 October 2026 (final, 00:15 IST)

Supersedes the 28 Sep handover, kept as `HANDOVER-2026-09-28.md`. Its architecture
section (§6) and measurement traps (§3) still hold and are not repeated here; its
performance table, test list and open items are stale.

> **Update, 1 Oct ~03:30 IST — read this before the rest.** Items 2, 3, 4, 6 and 7 of §6
> are done, pushed to `backup/cloud-lens-v2/main` and deployed to Scale (`ac9bf8e`,
> RuntimeSuccessful, warm pass 21:34–21:38 UTC). Full suite at `ac9bf8e`: **2243 passed,
> 0 failed, 10 skipped** (12m18s, `-n 4`); the ordering-dependent `test_fx_rollup` error
> did not recur.
>
> - `8cdae1a` Insights reads untagged spend from `tags.untagged_summary` (same ranking, cut,
>   currency and tag lookup as `cost_by_tag`; tests hold them equal). Standalone on live
>   **31s → 5.9s** (one 5.0s query). Inside Insights the source still reads 26.7s because
>   eight sources share the 2-vCPU database at once; the ceiling is now `waste` (20.0s) and
>   `rightsizing` (19.1s), as predicted.
> - `54afda3` Write-time eligibility check (`_eligible`) on a worker thread: longest
>   event-loop stall on the real 15.3MB Resources body **1,766ms → 247ms**.
> - `a171364` The seven old failures were all harness drift (deferred `summary()`, a
>   hard-coded date, a missing `log` global, a fake reader without `budget_ms`, renamed
>   variables in a source check). No product change.
> - `ac9bf8e` `/api/schedules` deliberately stays out of `DEFAULT_POLICIES` (live sync
>   progress and per-viewer write rights; its own ARM cache answers repeats in 0.08s).
> - Warm-pass after the deploy showed Resources 30.1s → 45.7s: **not a regression**. MCAPS
>   had stopped the database (00:35 IST, principal `da847d8c…`, §1) and it was started just
>   before the deploy, so buffers were cold; Resources' SQL re-measured standalone on live
>   is 17.8–18.3s, as before. Re-read `warm_compare` after the next deploy on a warm DB.
> - **§1 was wrong:** `Warehouse()` *does* run DDL on construction (`_init_schema`), and
>   `CLOUDLENS_READ_INDEXES=true` (from the local `.env`) makes a reader
>   `CREATE INDEX` on `resource_presence_a`. A profile run on 1 Oct did exactly that on live
>   for ~6 min, blocking one app query; it was cancelled (rolled back, no index left). Live
>   profiling now uses `…\79e1dc8d-…\files\live_tags_profile.py`: live's own app settings
>   only, `_init_schema` skipped, and a client-side guard refusing any non-`TEMP` write.
> - **Local database:** v3 now owns a plain `public.resource_ids` and v2's
>   `resource_costs` is gone, so a local v2 fails at boot (`"resource_ids" is not a view`,
>   rolled back, nothing damaged). Local benches skip `_ensure_derived`; decide with Ravi
>   whether to give v2 and v3 separate databases.
> - Still open: items 1, 5, 9 (need Ravi) and 8 (July partition; now unblocked while the DB
>   is up — read-only catalog probe).

**Start here:** §0 (the rules), then §6 (the pending list, with what each item waits on),
then §5b (why paging is no longer the obvious next step). §9 is the literal first moves.
Nothing is in flight: no background job, test run or deploy was left running, `main` is
pushed, and Scale runs `main`'s code.

**State: every data tab on the Scale deployment answers, repeat views come from the
snapshot cache — now including Anomalies and Billing opportunities, which were
never cached before `e99b71c` — and since `383f72e` the server itself recomputes the views
people use after a deploy, so the first view after a deploy is a cache hit too
(Commitments 98.6s → 0.10s, Insights 46.6s → 0.13s). Scale now runs `f1b2446`, the same
code as `main`. Commitments' cold path was collation and an unbounded sort (`9920e2d`,
13.3s server compute). The shared ARM client (`d3a6fcb`) was deployed and re-measured: it
changes nothing on Scale's own cold path, because Scale's identity sees one subscription
and its Advisor fan-out is one call, not the 71 the 59.4s was measured on (§5a). Insights'
cold time is its `tags` source — the full Tags tab query, 31s, run to read three numbers.
The 2–4s repeat views of Resources/Tags/Uptime were not payload size as such but ~1s of
per-hit CPU re-deriving the stored body (parse + walk for one header, then gzip level 9),
on the event loop; `f1b2446` stores both at write time — 1,329ms → 47ms per hit, measured
locally on the real 14.6MB body (§5b). Its live end-to-end number needs the admin password.
Server-side paging is therefore a product decision now, not a performance fix (§6 item 1).**

Everything below is measured on the live Scale deployment or read from the code.
Guesses say so.

---

## 0. Read this first

1. **Deploy to Scale only.** `cloudlens-web-scale` in resource group `CloudLens-Scale`.
   Ravi decided on 30 Sep that `cloudlens.site` (the old B1 stack in `cloud-lens-cx`)
   is **not** to be touched.
2. **Generic fixes only.** The HDFC data is a demo; the same code must run on live
   customer estates. Everything goes through the app's own import and derived-rebuild
   pipeline — no scripts tuned to this dataset, no hand-built tables.
3. **Speed over freshness is the agreed policy.** Data up to 24h old is fine; fresh data
   loads only when the user presses Refresh (`fresh=true`). Snapshot TTL is 24h.
4. **Parameterised SQL in the app, not stored procedures.** Discussed and agreed: a view
   or procedure runs the same plan at the same speed; the lever is summary tables built
   at import time. Scope isolation depends on per-connection temp views (§4).
5. **Never edit source while pytest runs** — several tests read or `exec` source.
   Run the full suite **detached** (§1); attached runs were killed by interruptions.
6. **`main` is pushed to `backup/cloud-lens-v2/main`** (private repo `samy6352/CloudLensv2`)
   at Ravi's instruction — last on 30 Sep ~23:59 IST, and that push included the handover
   commit that says so. Anything newer is unpushed — count with
   `git rev-list --count backup/cloud-lens-v2/main..main` rather than trusting a number
   written here. Git Credential Manager holds working credentials (pushes need no prompt).
   **Still ask Ravi before every push.**
7. **The live admin password is never written down.** Ask Ravi and pass it only in the
   process environment. It was typed into the 30 Sep chat, so Ravi was asked to rotate it —
   if the old one still works, remind him. Preferred hand-over, so it never appears in chat:
   Ravi sets it himself as a user environment variable
   (`[Environment]::SetEnvironmentVariable('CLOUDLENS_LIVE_ADMIN_PASSWORD', '<pw>', 'User')`
   in his own terminal), the bench process copies it into `AUTH_PASSWORD`
   (`$env:AUTH_PASSWORD = [Environment]::GetEnvironmentVariable('CLOUDLENS_LIVE_ADMIN_PASSWORD','User')`
   — `bench_tabs.py` reads `.env` with `setdefault`, so the process value wins), and Ravi
   removes the variable afterwards. It sits in plain text under `HKCU\Environment` while set.

---

## 1. Running, testing, deploying

Repo: `C:\Users\raviverma\OneDrive - Microsoft\Documents\Microsoft Scout\cloud-lens-v2`
Venv: `C:\Users\raviverma\.venvs\cloudlenstony\Scripts\python.exe`

**Tests** (the suite refuses an implicit database):

```powershell
$env:CLOUDLENS_PG_TEST='localhost'; $env:CLOUDLENS_PG_USER='postgres'
$env:PGPASSWORD='6352'; $env:PGSSLMODE='disable'
python -m pytest tests\test_usage_summaries.py -q          # targeted
python -m pytest -q -n 4                                   # full, ~14 min
```

- Do not pass `-p no:cacheprovider` — conftest needs `config.cache`.
- Full suite ≈ 34–40 min serial, **14m13s with `-n 4`** (measured 30 Sep at `d3a6fcb`).
  `-n 4` is the reason it no longer needs detaching, but an attached run still dies with
  the chat session — if you may be interrupted, redirect to a log and `Start-Process pwsh`.
- Lint: `python -m ruff check app tests\<files>` (targets py3.11: no nested same-quote
  f-strings; SIM rules are on).
- The local PostgreSQL is **shared with other sessions** (e.g. `D:\cloud lens v3`
  runs its own suites and rebuilds). Check `pg_stat_activity` before timing anything.
- **Table-name collision with v3 (from code reading, not checked in the database).** v3's
  `resource_ranks.py` drops and renames a plain `public.resource_ids`; v2 publishes its own
  `resource_ids` as a view over `resource_ids_a`/`_b` and adopts a plain table of that name
  as `_a` (`derived.py:195-236`). Either start order is hazardous on the shared local
  database: run `\d+ public.resource_ids` before starting both apps, and rename v3's tables
  if its ranking design is ever ported here. Scale is unaffected (no v3 there).

**Deploy** (≈ 3 min, then background derived builds):

```powershell
.\tools\package.ps1 -Destination <zip> -SourceRoot (Get-Location).Path -Python <venv python>
az webapp deploy -g CloudLens-Scale -n cloudlens-web-scale --src-path <zip> --type zip --async false
```

Do not change an app setting and deploy back to back: a setting change restarts the Kudu
container too, and on 30 Sep that orphaned the Oryx build mid-venv (deployment status 3,
CLI reported 502). Wait for the restart, then deploy. If the CLI returns 502, read
`https://cloudlens-web-scale.scm.azurewebsites.net/api/deployments/latest` before
retrying — the build may still be running.

On startup the app builds any derived table missing from the stamp **on its own**,
while the existing tables keep serving (§4). Watch progress with `pg_stat_activity`
and the meta key `data_rollup_tables`.

**Live benchmark:** `tools\bench_tabs.py` with `BENCH_BASE=https://cloudlens-web-scale.azurewebsites.net`,
`AUTH_USERNAME=admin`, `AUTH_PASSWORD=<ask Ravi; never commit it>` set in the process
environment (the repo `.env` holds the *local* password, which live rejects with 401). Pass paths or group
names (`shell spend monitor optimize govern reports`) to run a subset. Response headers
carry `X-CloudLens-Cache` (`hit`/`miss`/`response-too-large`/…) and `Server-Timing`.

**Live database, read-only probing** (how every number here was measured):

- `az postgres flexible-server start -g CloudLens-Scale -n cloudlens-postgres-scale`
  first if MCAPS stopped it overnight (~4–5 min).
- Firewall rule `ScoutSeed-20260928` covers `223.181.21.208`; add one if the IP changed.
- Password = `az account get-access-token --resource https://ossrdbms-aad.database.windows.net`,
  user `raviverma_microsoft.com#EXT#@fdpo.onmicrosoft.com`, db `cloudlens`, `sslmode=require`.
- `Warehouse()` **runs DDL on construction** (`_init_schema`: CREATE TABLE/INDEX, derived
  checks) whatever `CLOUDLENS_READ_ONLY` says, and `CLOUDLENS_READ_INDEXES=true` makes a
  reader build indexes. Against live, skip `_init_schema`, use live's own app settings (not
  the local `.env`), and refuse non-`TEMP` writes client-side — see the 1 Oct update above.
  Readers still need a writable session for their temp views.
- App SQL is a portable dialect; run it by hand through `sqldialect.to_postgres(sql, bind=False)`.

---

## 2. Live environment (Scale)

| | |
|---|---|
| App | `cloudlens-web-scale`, plan `cloudlens-plan-scale` (B2), Central India |
| Database | `cloudlens-postgres-scale`, Standard_E2ds_v5, PostgreSQL 18, 2 vCPU |
| DB settings | `shared_buffers` 4GB, `max_parallel_workers_per_gather` 2, default collation **`en_US.utf8`** |
| App settings | `PG_WORK_MEM=128MB`, `PG_STATEMENT_TIMEOUT=180s` (read-only connections only), `SNAPSHOT_CACHE_ENABLED=true`, `SNAPSHOT_WARMING_ENABLED=true`, `SNAPSHOT_SERVER_WARMING_ENABLED=true`, `AUTH_ALLOW_LOCAL=true`, `AUTO_INGEST=false`, `AUTO_REFRESH=false` |
| Platform | One uvicorn worker. `alwaysOn` **enabled 30 Sep** (had drifted to `false` from `infra/modules/stack.bicep`) |
| Data | 16.9M cost rows, 2026-07-01 → 2026-09-28, 70 subscriptions (HDFC demo) |
| Sign-in | SSO shows only the user's own RBAC subscriptions; use local `admin` to see all 70 |
| App identity | `cloudlens-id-scale` (user-assigned): Reader + Cost Management Reader on **one** subscription, the one hosting `CloudLens-Scale`. Local `admin` has no delegated token, so its live Azure calls (Advisor, Resource Graph, Monitor) go out as this identity and cover that one subscription — not the 70 in the warehouse |

Deployed commit: **`ac9bf8e`** (30 Sep ~21:33 UTC = 1 Oct ~03:03 IST, RuntimeSuccessful;
see the update at the top). Previously **`f1b2446`** (30 Sep 18:18 UTC, `az webapp deploy`, RuntimeSuccessful;
warm pass claimed 18:18:06, all 17 targets recomputed by 18:21:40; the migration added
`private_snapshots.coverage` and `.body_gzip`, and all 17 rewritten rows carry them —
Resources 14.6MB stored with a 1.10MB gzip copy, Tags 10.0MB/1.43MB, Uptime 7.5MB/0.67MB).
All 11 derived tables stamped. Scale runs the same application code as `main`.

---

## 3. What this session changed — commits on `main`

| Commit | What | Why / measured |
|---|---|---|
| `e7b08fb` | Checkpoint of pre-existing uncommitted work | Safety |
| `f833e30` | `hourly_usage`, `usage_signals`, `resource_tags` summaries; 24h snapshot cache | Uptime/Tags/AI Billing timed out (500/504); Budgets 504; Insights 220s uncached |
| `dd0c68a` | Test-harness drift fixes; Resources as one query | 6 regressions → 0 |
| `63e2403` | `resource_costs` + `resource_ids` (hashed per-resource money); build missing tables from the published stamped ones | Resources: 500 at 180s → 200 |
| `0c1ad5d` | Resources ranks narrowly first, details for winners only; snapshot entry cap 8MB → 32MB, store 256MB → 1GB | 33s → ~17s SQL; 15MB answer was never cached |
| `a1bc0cd` | Uptime and Tags group on byte-order keys; daily cells only for returned rows | Uptime 34.3s → 3.8s SQL, Tags 151s → 13s SQL |
| `383f72e` | Server-side snapshot warming (§4) | First view after a deploy: Commitments 98.6s → 0.10s, Insights 46.6s → 0.13s, Recommendations 33.8s → 0.08s, Resources 31.3s → 4.6s, Tags 29.9s → 2.5s |
| `e99b71c` | Opportunity prices count as evidence; Anomalies declares `partial_basis` | Billing opportunities 12.9s → 0.15s and Anomalies 2.2s → 0.14s on repeat (were never cached) |
| `9920e2d` | Commitments: `COLLATE "C"` on the benefit grouping/DISTINCT keys, `LIMIT 4000` on the inventory drill-down | Inventory headline 70.6s → 7.9s, coverage benefits 65.7s → 5.7s, drill-down 34.3s → 1.65s, identical rows. Deployed and verified live |
| `fbbf999` | Test stub identifies the benefit query by what it reads, not how it spells it | `9920e2d` changed the spelling; a stub matching on text went stale. Caught by the full suite |
| `d3a6fcb` | One shared `httpx.AsyncClient` for all ARM calls instead of one per call | Advisor fan-out, 71 subscriptions, same 4 slots, same outcome: 59.4s → 4.2s wall, 141.3s → 13.3s summed, 2.05s → 0.18s mean per refused call — **measured on the dev machine with the 71 ids passed in**. Deployed; no end-to-end change on Scale, whose fan-out is one call (§5a) |
| `f1b2446` | Snapshot hits send bytes prepared at write: coverage header and a gzip copy (level 6) stored with the body; old rows served as before | One Resources repeat view, real 14.6MB body through the real route + `GZipMiddleware`, dev laptop: **1,329ms → 47ms**, same bytes and headers out (§5b). Deployed; live end-to-end repeat view not yet measured (needs the admin password) |

`c88bb58` (`plan.md`) is **another session's v3 plan**, not this work.

---

## 4. Architecture added this session

**Opt-in usage summaries.** `USAGE_MODULES` in `warehouse.py` (`hourly`, `signals`,
`tags`, `resource_costs`, `resource_ids`) are shadowed per connection only when a reader
asks: `wh.reader(..., usage=("tags",))` exposes `r.tags`, etc.

- Ordinary summaries fall back to the same grain computed from scoped `costs` when the
  stored table is stale or cannot answer, so the SQL is identical either way.
- **`STORED_ONLY` summaries** (`resource_costs`, `resource_ids`) are exposed only when the
  stored table is current, the filters are served by its grain, and no currency conversion
  applies; otherwise the reader attribute is `None` and the tab runs its own query.
  Recomputing those grains from `costs` per request would cost more than the query they
  replace.
- All shadows carry the scope predicate as temp views — the security boundary asserted in
  `test_production_hardening`. Never read `"public".<table>` directly from a tab.

**`resource_costs` / `resource_ids`.** Resources ranks 5.4M presence rows collapsing to
2.0M resources; grouping by the ~150-byte id text was the cost. `resource_costs` keys the
same money by `ResourceHash` = `md5(lower(trim(ResourceId)))::uuid`; `resource_ids`
(month grain) resolves the hash back to the id for returned rows only. Both are built from
the published `resource_presence` (`SOURCE_TABLE`).

**`refresh_rollup` ordering.** Stamped tables are repaired by pending period **first**;
tables missing from the stamp are then built in full, reading the repaired sources
(`derived.rebuild(..., published=...)`). A full rebuild of everything happens only when a
stamped table's own source is missing. Before this, adding one summary to a large estate
forced a rebuild of every table (40+ min on live).

**The collation rule — the biggest lever found.** The live database compares text with
`en_US.utf8`. Resource ids share their first ~100 characters, so sorting/grouping them
linguistically dominated Uptime (30 of 34s) and Tags (most of 151s).

- Group on `... COLLATE "C"` keys. Equality is identical under both collations, so the
  groups cannot change.
- **Cast back to `COLLATE "default"` before any ORDER BY tie-break or join.** Letting
  `"C"` reach the `resource_tags` lateral lookup made it lose its index: a probe ran
  10+ minutes and had to be cancelled.
- `dashboard.py::_ranked` deliberately does *not* use it: there, ordering (not grouping)
  is on text, and `"C"` would change which equal-cost row wins.

**Daily cells inline.** Uptime and Tags each ran a second pass producing a
per-resource-per-day series for **every** resource (594K cells on live). That exceeded
their caps (150K / 200K) and was discarded whole, so filtered charts were silently
estate-wide. Each returned row now carries `json_agg(... ORDER BY day)` cells from the
same aggregation (`reporting.json_list` decodes them). The Tags `costs` fallback keeps
its original two queries.

**Snapshot cache** (`app/snapshot_cache.py`, settings in `main.snapshot_cache_settings`):

- TTL 24h, retention 7d; `PARTIAL_TTL` 1h only for reports whose evidence is partial
  *and* that fail the normal cacheability check. A partial result never replaces a
  complete one.
- `family_key` serves yesterday's snapshot across the UTC day boundary while today's
  computes.
- Keys include the **app version**, so **every deploy makes every view cold** — which is
  what server-side warming (below) now covers.
- A data publish does **not** make views cold: the key survives, the stored generation
  differs, so the last answer is served stale while it recomputes. The UTC day boundary
  is the same via `family_key`. (Earlier notes said publish was cold; the code says not.)
- Stored in PostgreSQL (TOAST-compressed); byte limits count the uncompressed JSON.
- **A hit re-derives nothing** (`f1b2446`). The coverage header and a gzip copy are
  computed once per write, in a worker thread (`_prepared`), and stored beside the body
  (`coverage`, `body_gzip`). `_response` sends the gzip copy as-is to clients that accept
  it; `GZipMiddleware` passes anything already carrying `Content-Encoding` through, so
  nothing is compressed twice. Rows written before `f1b2446` have `''`/`NULL` and are served
  the old way until rewritten. Still on the event loop, once per *write*: `_body`'s own
  cacheability parse and `_safe_content` scan (~2.3s on the 15MB Resources answer, §6
  item 4).
- `/api/budgets` and `/api/insights` are cached (MONITOR); a budget POST bumps
  `budget_revision` so a cached "no budgets" cannot outlive a create.
- Local accounts get a stable cache partition; each Entra user has their own partition,
  so each user's first view per day is cold.

**Server-side warming** (`app/snapshot_warmer.py`, `SnapshotCache.warm`, flag
`SNAPSHOT_SERVER_WARMING_ENABLED`, default false, on for Scale and in the Bicep template):

- *Targets.* A view whose computation took ≥ `SNAPSHOT_WARM_MIN_SECONDS` (1s) is recorded
  in `snapshot_warm_targets`: principal, tenant, path, normalised query, cost — never a
  credential. Only a person's use creates or renews a target (hits touch it at most hourly
  per process); warming never renews one, so a target lapses 7 days after the person last
  looked. Capped at 1000 rows.
- *Pass.* Once per (app version, data generation, UTC day), claimed in `meta`
  (`snapshot_warm_claim`) so one instance runs it; skipped while the generation is
  `pending:`. Starts 30s after boot, then checks every 60s. Targets run one at a time
  (`SNAPSHOT_WARM_CONCURRENCY`), at most `SNAPSHOT_WARM_LIMIT` (100) per pass, most
  recently used first.
- *Replay.* Each target goes through the whole ASGI app as its owner, so auth, entitlement,
  reporting month and cache fencing are a real request's. A scope key
  (`cloudlens.snapshot_warm`) no HTTP client can set turns the cached route into "store,
  don't send" (204 + `X-CloudLens-Warm: stored|fresh|backoff|…`). Credentials are minted
  in process: local `file`/`env` accounts get a 1h session cookie; API tokens their bearer;
  Entra users a cookie for their *live* session.
- *Entra.* Sessions are in memory, so after a restart there is none to warm as; those
  users' views are warmed when they next sign in (`sso_callback` → `warm_principal_soon`).
- `SnapshotCache.warm` computes only if the exact current answer is missing, shares `get`'s
  task map (a viewer who arrives meanwhile joins the calculation), and keeps every refresh
  rule: re-authorisation, generation fence, back-off, partial never replacing complete.
- Warm requests are excluded from the access audit and the slow-request log.
- Verified on live 30 Sep: bench recorded 16 targets for `env:admin`; forcing a new version
  (`CLOUDLENS_BUILD_ID`) → pass claimed 12:49:04 UTC, all 16 computed by 12:54:09 (≈5 min,
  serial); a restart on an already-warm version claimed its pass and recomputed nothing.
  After the `e99b71c` deploy the pass computed all 19 current targets (≈5–6 min).
- **Not yet verified on live: Entra sign-in warming.** Every live test used local `admin`.
  It is covered by tests (`test_snapshot_warming.py`, incl. replay through the real
  `main.app`), but nobody has signed in via SSO after a deploy and checked the pass.
- The benchmark itself creates targets (it signs in as `admin` and opens every tab), so
  running it on a new deploy both measures cold views and registers them for warming.
- A target whose warm computation is cheap drops below the 1s floor and stops being warmed
  (live: `/api/budgets` recomputed in 0.0s, so it is skipped from now on) — by design.

**Partial-report rules** (`e99b71c`): `monthly_savings` and `annual_savings` count as
money evidence. A report may declare `"partial_basis": "data"` when its partial coverage is
a property of the committed data (missing receipts, no rows); the cache then keeps it for
the normal TTL and the generation fence retires it on new data. `"source"` (a failed read),
a missing basis, errors and unfinished jobs are still refused. Only Anomalies declares it
today (`anomalies.RECEIPTS_UNREADABLE` / `NO_VERIFIED_RATE` mark the `"source"` cases; a
row with *no* currency code is data, a real code with no rate is source).

---

## 5. Performance — live, 30 Sep after `383f72e` and `e99b71c`

**Two measures, not one.** The bench columns are client-side at `e99b71c` and include
transfer. The **server compute** column is newer: each view's cold cost as the server timed
it during the warm pass that followed the `d3a6fcb` deploy (`snapshot_warm_targets.seconds`,
written in `snapshot_cache._refresh`: from after the slot and authorisation to the snapshot
being stored — so the route's computation plus the cache's own body checks and write, but
no transfer and no queueing; the pass runs one view at a time). For the 8–15MB views the
body checks are ~2s of it (§6 item 4). It supersedes the bench's cold column wherever they
disagree, which is Commitments, Insights and Recommendations (§5a). Every other bench
column still holds.

`tools\bench_tabs.py`, full runs. "Cold" = first view right after the deploy of `383f72e`,
before any warm targets existed. "After warming" = first view on a *new* app version
(`CLOUDLENS_BUILD_ID` bumped), measured once the server's warm pass had finished and before
anyone opened a tab. Session files `bench-deploy1-cold.txt`, `bench-deploy2-warmed.txt`.

| Tab | Before this session | Cold after deploy (bench, `e99b71c`) | **Server compute, cold (`d3a6fcb`)** | After warming | Repeat | Payload |
|---|---|---|---|---|---|---|
| Commitments | 98–101s | 98.6s | **13.3s** | **0.10s** | 0.10s | 61KB |
| Insights | 209–220s | 46.6s | **31.2s** | **0.13s** | 0.10s | 6KB |
| Recommendations (AI) | — | 33.8s | **25.6s** | **0.08s** | 0.08s | — |
| Resources | 500 at 180s | 31.3s | 26.6s | **4.6s** | 3.7s | 15.3MB |
| Tags | 184–212s | 29.9s | 25.4s | **2.5s** | 2.3–3.9s | 10.5MB |
| Uptime | 78–94s | 13.5s | 12.0s | **1.7s** | 1.6s | 7.8MB |
| Savings | — | 12.1s | 12.0s | **0.08s** | 0.08s | 24KB |
| AI Billing | 500 at 180s | 9.9s | 6.5s | **0.39s** | 0.38s | 47KB |
| Governance | — | 7.6s | 6.9s | **0.10s** | 0.09s | 17KB |
| Executive | — | 6.9s | 6.9s | **0.09s** | 0.09s | 17KB |
| Billing opportunities | — | 13.2s | 13.1s | 12.9s → **0.15s repeat since `e99b71c`** (was never cached) | 0.15s | 9KB |
| Anomalies | 2.2s | 2.1s | 2.1s | 2.2s → **0.14s repeat since `e99b71c`** (was never cached) | 0.14s | 5KB |
| Schedules | — | 8.2s | not a warm target | 8.6s (not a cached route) | 0.08s | 10KB |
| Budgets | 504 at 240s | 0.3s | 0.0s (below the 1s floor, not re-warmed) | 1.5s | 0.19s | 4KB |

Also warm targets, server compute at `d3a6fcb`: Filters 5.5s, Rightsizing 6.0s, Waste 5.5s,
AKS 5.4s, Health 2.0s, ESU 0.7s.

45 endpoints: 42 × 200; the three 400s (`change-analysis`, `performance`,
`service-health`) need parameters the bench does not send — by design.
Repeat views of Resources/Tags were 2–4s because of payload size, not queries — more
exactly, ~1s of per-hit CPU re-deriving the stored body, which `f1b2446` removed (§5b). The
"After warming" and "Repeat" columns predate it and have not been re-measured live.

### 5a. The cold path, profiled — and what the old §6 item 1 got wrong

The old item 1 assumed all three slow cold tabs "wait on live Azure/Advisor/AI calls".
**That was wrong for Commitments, and — measured after the deploy — wrong on Scale for the
other two as well.** Profiling statement by statement, as §1 prescribes, found two unrelated
causes; deploying the second showed it does not apply to Scale's own cold path.

**Commitments never touched Azure.** Its 98.6s was SQL, and the same `en_US.utf8` collation
trap already recorded for Uptime and Tags: grouping and `DISTINCT` on long resource ids.
`9920e2d` puts `COLLATE "C"` on the benefit grouping keys and bounds the inventory
drill-down's sort with `LIMIT 4000`:

| Statement | Before | After |
|---|---|---|
| Inventory headline | 70.6s | **7.9s** |
| Coverage benefits | 65.7s | **5.7s** |
| Inventory drill-down | 34.3s | **1.65s** |

Rows are identical. Deployed to Scale and verified live. Note the asymmetry, because it is
a trap: `COLLATE "C"` on the *drill-down* was measured **worse** (9.8s → 22.6s) — it changes
the plan. The lever there is bounding the sort, not the collation.

**What the Advisor profile measured — correct on the dev machine, not Scale's cold path.**
The profile below ran the app's code on the dev machine with the 71 seeded HDFC ids passed
in explicitly, because locally the fan-out cannot be reproduced otherwise (the dev
credential sees one subscription). On that input:
`waste.advisor_recommendations` issues one ARM call per subscription through the
process-wide `cost._CONCURRENCY = asyncio.Semaphore(4)`, and 67 of the 71 return 401,
because the seeded HDFC subscription ids are real subscriptions in a foreign tenant.
Measured: 157.5s of HTTP compressed into 59.3s wall, worst call waiting 55.9s for a slot.

The cost was not the requests. `_fetch` opened a fresh `httpx.AsyncClient` per call, so every
one of the 71 paid a new TCP connect and TLS handshake. `d3a6fcb` shares a single pool:

| | Wall | Summed HTTP | Mean per refused call |
|---|---|---|---|
| A client per call | 59.4s | 141.3s | 2.05s |
| One shared client | **4.2s** | **13.3s** | **0.18s** |

Same 71 calls, same four slots, same outcome (14 recommendations, 68 refusals). The
semaphore was deliberately **not** raised — throttling behaviour is what it exists to manage,
and the handshake was the whole problem.

**Deployed and re-measured on Scale (30 Sep 16:41 UTC): no end-to-end change.** A deploy
changes the version digest, so the server's warm pass recomputes all 18 of `admin`'s
targets above the 1s floor, serially, in fixed `used DESC` order, from an empty process.
That makes the pass after `9920e2d` (14:35 UTC) and the pass after `d3a6fcb` (16:41 UTC)
like for like, read from `snapshot_warm_targets.seconds` without signing in
(`warm_compare.py`, §8):

| View | `9920e2d` | `d3a6fcb` |
|---|---|---|
| Insights | 31.4s | 31.2s |
| Recommendations | 20.2s | 25.6s |
| Commitments | 13.6s | 13.3s |
| Billing opportunities (first in the pass) | 17.8s | 13.1s |
| All 18 targets, summed | 214.1s | 206.7s |

Movements of ±3–5s appear in both directions on views `d3a6fcb` cannot touch (Resources
−2.7s, Tags +1.0s), so none of these is attributable from one pass each. Recommendations'
+5.4s is unexplained: its two data sources (Savings, Anomalies) did not move in their own
tabs, and its model call's latency is recorded nowhere.

**Why nothing moved: on Scale the fan-out is one call.** The warm pass runs as local
`admin`, which carries no delegated token, so ARM calls go out as the app's identity, and
`cloudlens-id-scale` holds Reader on one subscription (§2). `list_subscriptions()` returns
that one; Advisor is asked once. There were never 71 handshakes on Scale to save. Insights'
own per-source timings, which it stores in every payload (`timing_ms`) and so in every
snapshot, confirm it:

| Insights computed (UTC) | tags | waste | rightsizing | commitments | anomalies | advisor |
|---|---|---|---|---|---|---|
| 14:36, `9920e2d` warm pass | 31.1s | 22.0s | 15.7s | 11.0s | 9.8s | **0.2s** |
| 16:41, `d3a6fcb` warm pass | 31.0s | 21.6s | 15.4s | 10.9s | 10.0s | **0.1s** |

Advisor's 7.6–8.8s at 12:49 and 13:26 is what the first Advisor read in a fresh process
cost when Insights was the view that made it (token, connection and Advisor itself — not
broken down). Billing opportunities became a target at 13:27, has been first in the pass
order since, and makes that read; Insights then gets Advisor from the in-process ARM cache
(same URL, same caller, inside its 300s TTL), hence 0.1–0.2s.

`d3a6fcb` stays: it is correct, the suite passes, and it is what a real estate needs — an
SSO user whose own token sees many subscriptions, or a customer install that grants the
identity Reader at management-group scope (the v3 deployment model) fans out exactly as the
dev-machine profile did. It has not yet been measured end to end on such an estate (§6).

**Insights' cold time is its `tags` source.** `_gather` runs eight sources concurrently and
waits for the slowest; that is `tags.cost_by_tag`, the Tags tab's full per-resource query
(25.4s alone, 31s when sharing 2 vCPUs with seven others). `_from_tags` reads three numbers
from its result: `total`, `untagged.cost`, `untagged.resources`.

Two things were considered and rejected:

- **Raising `_CONCURRENCY`.** Unnecessary once handshakes went; it would trade a solved
  problem for a throttling risk.
- **A negative cache for refused subscriptions.** Caching a 401/403 hides access that has
  just been granted. Dropped.

Measurement trap found the hard way: `asyncio.run()` leaves `_inflight` tasks bound to a dead
event loop, so a second scenario in the same process measures contaminated numbers. **One
process per scenario.**

### 5b. The 2–4s repeat views — measured, fixed, and what it means for paging

Paging was approved as "what removes the 8–15MB payloads and the 2–4s repeat views". Before
building it, the repeat view was taken apart on the real 14.6MB Resources body (dev laptop,
Snapdragon X Elite, roughly 3× the Scale B2's single-thread speed — an estimate):

| Per repeat view, before `f1b2446` | Time | Where |
|---|---|---|
| `json.loads` of the stored body + `_partial_evidence` walk of every row, for the one `X-CloudLens-Coverage` header | 791ms | event loop |
| `GZipMiddleware` recompressing the same bytes at level 9 | 253ms | worker thread (Starlette offloads ≥128KB) |
| PostgreSQL read of the TOASTed body (live, measured) | 15–32ms | database |
| Whole hit through the real route and middleware | **1,329ms** | |
| Same hit after `f1b2446` | **47ms** | |

Live repeat views scaled with body size in the same way (Uptime 7.8MB 1.6s, Tags 10.5MB
2.3–3.9s, Resources 15.3MB 3.7s). Also measured: parsing the 14.6MB body takes 69MiB and
~250ms; filtering, searching and sorting its 8,000 rows in Python takes 3–135ms; tags are
62% of its bytes; gzip takes it to 1.07MB on the wire.

**What the four read-only surveys found** (Resources, Tags, Uptime, cache layer — reports
in the session folder, §8):

- All three tabs were **deliberately** built so that a filter click never touches the
  network, and say so in code (`dashboard-spend.js:999-1003`, `main.py:4512-4514`). Dozens
  of tests pin it — e.g. `browser_ui_performance.js` asserts one request for five searches,
  `browser_resources.js` "local filtering does not refetch".
- Every KPI, facet list, sort (with `Intl.Collator` numeric collation), outlier cut, footer
  total and details panel is computed in the browser over the capped row set (Resources and
  Tags 8,000 rows, Uptime 4,000 meters). Paging means porting all of it to Python with exact
  parity (rounding, collation, IEEE edge cases) — the Resources survey lists 20 separate
  computations for that tab alone; Tags adds any/all tag selection, chargeback and budget
  creation; Uptime adds schedule savings and charts drawn from daily cells.
- Per-click requests cannot go through the snapshot cache as it stands: every distinct query
  string is a new entry (4,096-row FIFO shared by all users) and a new warm target (1,000
  cap). The design all four converged on: keep the existing full answer as the cached base,
  and serve filter/sort/page clicks from an in-process, byte-capped copy of it on uncached
  routes. v3's SQL-ranked paging (`D:\cloud lens v3` `1090ed9`) is 0.4–0.9s per page only
  when a stored ranking matches; filter bar/FX/odd windows fall back to 14–57s, it needs 7
  new tables, and its `resource_ids` collides with v2's (shared database).
- Cheaper alternative the Uptime survey costed: keep client-side filtering and slim the
  payload (drop unread keys, stop repeating the ~150-byte id, columnar daily cells).

---

## 6. Pending work — in the order I would do it

Done on 30 Sep and no longer pending: server-side warming (`383f72e`), Always On enabled
on Scale, Anomalies and Billing opportunities cached (`e99b71c`), the cold-path profile and
both fixes it produced (`9920e2d`, `d3a6fcb` — §5a), `d3a6fcb` deployed and re-measured on
Scale (no end-to-end change there, and why — §5a), the stale §5 column replaced by
server-measured cold compute, the repeat-view cost found and fixed (`f1b2446`, deployed —
§5b), and the full suite, which ran at
`d3a6fcb`: **2220 passed, 7 failed, 10 skipped, 1 error, 14m13s with `-n 4`** (log in the
session folder, §8). `f1b2446` was validated with `test_snapshot_cache.py` (141 passed, incl.
PostgreSQL) and the warming/cache/asset suites (101 passed); the full suite has not been
re-run since. See §3–§5.

| # | Step | Waits on |
|---|---|---|
| 1 | Measure `f1b2446`'s repeat views on live, then decide paging: stop / slim / page | **Ravi**: admin password (§0.7), then the decision |
| 2 | Insights: replace its 31s `tags` source with a small aggregate for the 3 numbers it reads | **Done** `8cdae1a`, deployed |
| 3 | Re-run the full suite at `f1b2446` (~14 min, `-n 4`) | **Done** (at `ac9bf8e`: 2243 passed, 0 failed) |
| 4 | Move the write-time body check (~2.3s on Resources) off the event loop | **Done** `54afda3`, deployed |
| 5 | Verify Entra sign-in warming on live; first end-to-end test of `d3a6fcb` | **Ravi**: one SSO sign-in after a deploy |
| 6 | Seven pre-existing test failures + one ordering-dependent error | **Done** `a171364` (error did not recur) |
| 7 | `/api/schedules` 8.2s cold and uncached — cache it or not | **Done** `ac9bf8e`: not cached, reason recorded |
| 8 | Possibly bloated July partition (~20GB vs ~10GB for August) | Nothing — ready |
| 9 | Carried: `stash@{0}` Environment filter, browser-suite clean pass, two UI decisions; Tags/Uptime ignore the global filter bar | **Ravi**, for the UI decisions |

At the close of 30 Sep, Ravi was offered: start the unblocked work (3 in the background,
then 2 and 4), measure live first (1), or stop. He asked for this handover instead, so no
choice was made — ask him which, recommending the unblocked work if he has no preference.

Details, in the same order:

1. **Decide paging with Ravi — it is now a product decision, not a speed fix.** First
   measure what `f1b2446` did end to end: ask Ravi for the admin password (process
   environment only) and run `tools\bench_tabs.py spend` against live; before it,
   Resources/Tags/Uptime repeat views were 3.7s/2.3–3.9s/1.6s. If they are now well under a
   second, what paging still buys is a smaller download (1.1MB gzip for Resources) and less
   browser memory, at the cost of a round trip per filter click and porting all of §5b's
   client logic. Options, cheapest first: (a) stop here; (b) slim the payloads (Uptime
   survey's plan; tags are 62% of Resources' bytes); (c) the in-process-base paging design
   in §5b, tab by tab, Resources first. Do not start (c) without Ravi's go-ahead.
2. **Insights: stop running the whole Tags query to read three numbers** (§5a). Its cold
   time is the `tags` source, 31s; `_from_tags` needs only the window's total, untagged
   cost and untagged resource count. Serve those from one aggregate over the `tags` usage
   summary, through the reader like every other tab, and prove it equal to
   `cost_by_tag`'s `total`/`untagged` at three scopes before switching (§8). The next
   ceiling is then `waste` (21.6s), then `rightsizing` (15.4s). Small, generic, measured.
3. **Re-run the full suite at `f1b2446`** (detached, `-n 4`, ~14 min) and compare with
   `suite-d3a6fcb.log`: the seven known failures and one ordering error, nothing new.
4. **Resources cold ~30s** (30.1s server compute at `f1b2446`). SQL is ~17s (9.7s narrow
   ranking over `resource_costs` + ~6s detail gather); the rest is app-side on a 2-vCPU
   box, including ~2.3s of snapshot-cache body checks on a 15MB answer (`_body`'s parse and
   `_safe_content` regex scan, still on the event loop at write time). Moving that check
   into a worker thread, as `f1b2446` did for the hit path, frees the loop during cold
   computes; paging would shrink both.
5. **Verify Entra sign-in warming on live** (§4): after a deploy, sign in via SSO, confirm a
   `sign-in` pass runs for that oid (its targets' `computed` moves) and the next views hit.
   The same sign-in is the only way to measure `d3a6fcb` end to end without a customer
   estate: if that user's token sees many subscriptions, Insights' stored `timing_ms.advisor`
   is the fan-out's cost for them (§5a shows how to read it without the admin password).
6. **The seven pre-existing test failures, still unfixed.** Proved pre-existing on 30 Sep
   by re-running them against a clean tree with `app/cost.py` stashed — all seven fail
   identically, so no 28–30 Sep work caused them:
   `test_budget_reading`, `test_datastore_safety::test_summary_does_not_label_unknown_plus_usd_as_single_currency`,
   `test_overview`, `test_postgres::test_rows_written_by_copy_come_back_intact`,
   `test_postgres::test_duplicate_spellings_preserve_charges_pending_authoritative_reingest`,
   `test_summary_pending::test_warehouse_summary_returns_pending_after_an_exists_probe`,
   `test_visuals` (399/400: "the server hands over the parsed filter for that link").
   Start with `test_postgres`: `summary()["rows"]` comes back `None` where 2 is expected,
   which looks like one cause sitting behind several of the seven. Separately, the 1 error,
   `test_fx_rollup::test_failed_build_preserves_native_statistics`, is ordering-dependent —
   it passes on its own, so it is a test-isolation bug, not a product one.
7. `/api/schedules` is 8.2s cold and not a snapshot-cached route (0.08s repeat from its
   own cache). Small; check whether it belongs in `DEFAULT_POLICIES`.
8. Unconfirmed: live July partition looks bloated (≈20GB heap for 5.0M rows vs ≈10GB for
   5.8M in August, 0 dead tuples). Not investigated.
9. Carried from 28 Sep and still open: `stash@{0}` (`env_tags`, Environment filter
   always empty), browser-suite clean pass, the two UI decisions in the old §7e. Also: Tags
   and Uptime ignore the global filter bar (`rg`, `service`, `region`, `charge`, `pricing`,
   `env`) although the browser sends it, so those params only split their cache keys.

---

## 7. Measured dead ends — do not repeat

- **`try_cast_double` is not Uptime's cost.** Replacing it with a plain cast: 42.6s vs
  42.5s, identical output. The grouping sort was the cost.
- **Resources:** raising `work_mem` 128MB → 512MB made no difference (76s). A hashed
  expression index on presence was slow to build and blocked by locks.
- **Tags cells gathered *after* the top-N cut** (lateral per resource into the grouped
  days) timed out at 300s; inline `json_agg` costs only +1.5s.
- **`COLLATE "C"` applied to the whole Uptime query** (reaching the tag lookup): 10+ min.
- **`COLLATE "C"` on the Commitments inventory drill-down**: 9.8s → **22.6s**. It changes
  the plan. The same collation is a large win on that tab's *grouping* keys (§5a) — it is
  not a blanket fix. Bounding the sort (`LIMIT 4000`) is the lever there.
- **Raising `cost._CONCURRENCY` above 4 to speed the Advisor fan-out.** Rejected: the
  handshake per call was the cost, not the concurrency, and the semaphore exists to manage
  throttling. Sharing one client fixed it without touching the limit (§5a).
- **Negative-caching refused (401/403) subscriptions in the fan-out.** Rejected: it would
  hide access that has just been granted.
- **Two `asyncio.run()` scenarios in one process give contaminated numbers** — `_inflight`
  tasks survive bound to a dead loop. One process per scenario.
- **Timing right after a deploy is noisy.** Autovacuum/analyse of new tables and cold
  tab builds on 2 vCPUs tripled some timings (Resources read 30s, then 10s, for the same
  7-day query). Re-measure in isolation before believing a regression.
- An early "10s" Resources measurement was the 7-day query saved by mistake — check the
  window in any captured SQL before comparing.
- **Changing an app setting right before `az webapp deploy`** restarted Kudu mid-build
  (see §1). Not a code problem; wait for the restart.
- **Measuring a live Azure fan-out from the dev machine and treating it as the server's.**
  The dev credential sees one subscription, so the Advisor profile passed the 71 seeded ids
  in by hand and measured a 59.4s fan-out Scale never performs — Scale's identity lists one
  subscription (§2). The fix was right; the prediction that it would move Scale's cold tabs
  was not (§5a). Before profiling a live Azure call, check what the *server's* caller
  (`list_subscriptions()` as that identity) returns, and measure on that input.
- **Measuring warming right after the restart that follows a setting change:** the app
  took ~100s to pass its warm-up probe and the warmer waits 30s more, so the claim did not
  move for ~2.5 min. Check `meta.snapshot_warm_claim.updated_at`, not the clock.

---

## 8. Working agreements

- Measure before and after on live, statement by statement, before changing anything.
- Prove equivalence with tests: `tests/test_usage_summaries.py` builds each tab routed
  and forced onto `costs` at three scopes and compares payloads; extend it for any new
  summary. New tests this session also cover stored-only gating, scope isolation,
  build-alone, truncation money equality, Uptime cells integrity and Tags vs the
  detail-table query.
- Prove a failure is pre-existing by running it on the base commit.
- Commit messages explain *why*, with the numbers.
- Session scratch (bench outputs, captured SQL, plans, probe scripts) lives in the Scout
  session folders under `C:\Users\raviverma\.scout\copilot\session-state\`
  (`…` below): `…\93969277-…\files\` (up to `a1bc0cd`),
  `…\e27b8924-…\files\` (warming: `live_probe.py` prints live meta, snapshot
  and warm-target state read-only; `live_watch.py` follows a warm pass; both bench
  outputs), `…\1ccfca3b-…\files\` (the 30 Sep full-suite log
  `suite-d3a6fcb.log` and the clean-tree baseline `suite-baseline-clean-tree.log` that
  proves the seven failures pre-existing), and `…\81c61fc4-…\files\` (the
  `d3a6fcb` and `f1b2446` deploys: `cloudlens-d3a6fcb.zip`, `cloudlens-f1b2446.zip` as
  deployed; `warm_compare.py`, which snapshots `snapshot_warm_targets`, follows a warm pass
  and compares two passes, read-only; `warm-before/after-*.json`, the passes in §5a/§5;
  `survey-resources.md`, `survey-uptime.md`, `survey-cache-layer.md`,
  `survey-tags-partial.md`, the four paging surveys behind §5b — every client-side
  computation per tab with file:line, and the tests that pin them). Not in the repo. Copy
  the probes into the new session's folder if useful.
- **Cold compute on live without the admin password.** Any deploy (new version digest) makes
  the warmer recompute every target serially; `warm_compare.py snap` before the deploy and
  `watch <before> <after>` after it gives a per-view before/after from the server's own
  timer. Insights also stores per-source `timing_ms` in its body, readable from
  `private_snapshots` (plain JSON; filter `byte_size < 300000` so large bodies are never
  detoasted). Neither needs a sign-in; both are read-only.

---

## 9. First moves in the new chat

1. `git status` (only `tools/bench-tabs-latest.json` should be dirty) and
   `git log --oneline -8` — top should be this handover, then `16a6807`, `e60a263`,
   `f1b2446`, `34ba93d`. Scale runs `f1b2446`: the same application code as `main` (§2).
   `git rev-list --count backup/cloud-lens-v2/main..main` shows what is unpushed (§0.6).
2. Start the Scale database if stopped; confirm the firewall rule for today's IP (§1).
3. Baseline without the password: `warm_compare.py snap` (§8) records every warm target's
   server compute time and whether the last pass finished. Expect hits almost everywhere:
   17 of the 19 warm targets were recomputed after the last deploy (Budgets and ESU sit
   below the 1s floor) and Always On keeps them warm.
4. Ask Ravi which path (the choice left open at the end of §6's table): the unblocked work
   (item 3 in the background, then 2 and 4), or the live measurement first (item 1 — he
   sets the password as in §0.7; then `tools\bench_tabs.py spend`, the first live number
   for `f1b2446`), and after it stop / slim / page. No paging code before that decision.
5. Before believing any number: read §7 first, and re-measure in isolation. Three of the
   entries there are traps this session walked into and had to back out of.
