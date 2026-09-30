# CloudLens — handover, 30 September 2026 (final, 22:00 IST)

Supersedes the 28 Sep handover, kept as `HANDOVER-2026-09-28.md`. Its architecture
section (§6) and measurement traps (§3) still hold and are not repeated here; its
performance table, test list and open items are stale.

**Start here:** §0 (the rules), then §6 item 1 (the next task), then §5a (why it is the
next task). §9 is the literal first five commands.

**State: every data tab on the Scale deployment answers, repeat views come from the
snapshot cache in 0.1–3.9s — now including Anomalies and Billing opportunities, which were
never cached before `e99b71c` — and since `383f72e` the server itself recomputes the views
people use after a deploy, so the first view after a deploy is a cache hit too
(Commitments 98.6s → 0.10s, Insights 46.6s → 0.13s). The cold path for users the warmer has
not seen has since been profiled and fixed at both ends: Commitments was never waiting on
Azure at all but on collation and an unbounded sort (`9920e2d`, live), and
Insights/Recommendations were paying a TLS handshake per subscription on the Advisor
fan-out (`d3a6fcb`, 59.4s → 4.2s, committed but **not yet deployed**). What remains:
deploy and re-bench that fix, then payload size and server-side filtering and paging.**

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
6. **`main` is 20 commits ahead of `backup/cloud-lens-v2/main`** (nothing after `e42bfef`
   is pushed). `git push` needs credentials that are not cached on this machine. Ask Ravi
   before pushing.
7. **The live admin password is never written down.** Ask Ravi and pass it only in the
   process environment. It was typed into the 30 Sep chat, so Ravi was asked to rotate it —
   if the old one still works, remind him.

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
- `Warehouse()` does no DDL on construction and a reader only creates temp views, so the
  app's real code path can be profiled against live with `CLOUDLENS_READ_ONLY=true` and
  the `PG*` vars pointed at it. Patch `psycopg.Cursor.execute` to time statements.
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

Deployed commit: **`9920e2d`** (30 Sep — the Commitments collation/sort fix, verified live).
All 11 derived tables stamped. **`d3a6fcb` (shared ARM client) is committed but not
deployed**; `fbbf999` is test-only. Local `main` is therefore two code commits ahead of
Scale, one of which matters.

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
| `d3a6fcb` | One shared `httpx.AsyncClient` for all ARM calls instead of one per call | Advisor fan-out, 71 subscriptions, same 4 slots, same outcome: 59.4s → 4.2s wall, 141.3s → 13.3s summed, 2.05s → 0.18s mean per refused call |

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

**The table below is a snapshot taken at `e99b71c`. Its "Cold after deploy" column is stale
for Commitments, Insights and Recommendations — see §5a for what `9920e2d` and `d3a6fcb`
did to those three. Every other column still holds.**

`tools\bench_tabs.py`, full runs. "Cold" = first view right after the deploy of `383f72e`,
before any warm targets existed. "After warming" = first view on a *new* app version
(`CLOUDLENS_BUILD_ID` bumped), measured once the server's warm pass had finished and before
anyone opened a tab. Session files `bench-deploy1-cold.txt`, `bench-deploy2-warmed.txt`.

| Tab | Before this session | Cold after deploy | After warming | Repeat | Payload |
|---|---|---|---|---|---|
| Commitments | 98–101s | 98.6s | **0.10s** | 0.10s | 61KB |
| Insights | 209–220s | 46.6s | **0.13s** | 0.10s | 6KB |
| Recommendations (AI) | — | 33.8s | **0.08s** | 0.08s | — |
| Resources | 500 at 180s | 31.3s | **4.6s** | 3.7s | 15.3MB |
| Tags | 184–212s | 29.9s | **2.5s** | 2.3–3.9s | 10.5MB |
| Uptime | 78–94s | 13.5s | **1.7s** | 1.6s | 7.8MB |
| Savings | — | 12.1s | **0.08s** | 0.08s | 24KB |
| AI Billing | 500 at 180s | 9.9s | **0.39s** | 0.38s | 47KB |
| Governance | — | 7.6s | **0.10s** | 0.09s | 17KB |
| Executive | — | 6.9s | **0.09s** | 0.09s | 17KB |
| Billing opportunities | — | 13.2s | 12.9s → **0.15s repeat since `e99b71c`** (was never cached) | 0.15s | 9KB |
| Anomalies | 2.2s | 2.1s | 2.2s → **0.14s repeat since `e99b71c`** (was never cached) | 0.14s | 5KB |
| Schedules | — | 8.2s | 8.6s (not a cached route) | 0.08s | 10KB |
| Budgets | 504 at 240s | 0.3s | 1.5s | 0.19s | 4KB |

45 endpoints: 42 × 200; the three 400s (`change-analysis`, `performance`,
`service-health`) need parameters the bench does not send — by design.
Repeat views of Resources/Tags are 2–4s because of payload size, not queries.

### 5a. The cold path, profiled — and what the old §6 item 1 got wrong

The old item 1 assumed all three slow cold tabs "wait on live Azure/Advisor/AI calls".
**That was wrong for Commitments and only half the story for the other two.** Profiling
statement by statement, as §1 prescribes, found two unrelated causes.

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

**Insights and Recommendations do wait on Azure, but not on Azure being slow.**
`waste.advisor_recommendations` issues one ARM call per subscription — 71 on the live estate
— through the process-wide `cost._CONCURRENCY = asyncio.Semaphore(4)`. 67 of the 71 return
401, because the seeded HDFC subscription ids are real subscriptions in a foreign tenant.
Measured: 157.5s of HTTP compressed into 59.3s wall, worst call waiting 55.9s for a slot.

The cost was not the requests. `_fetch` opened a fresh `httpx.AsyncClient` per call, so every
one of the 71 paid a new TCP connect and TLS handshake. `d3a6fcb` shares a single pool:

| | Wall | Summed HTTP | Mean per refused call |
|---|---|---|---|
| A client per call | 59.4s | 141.3s | 2.05s |
| One shared client | **4.2s** | **13.3s** | **0.18s** |

Same 71 calls, same four slots, same outcome (14 recommendations, 68 refusals). The
semaphore was deliberately **not** raised — throttling behaviour is what it exists to manage,
and the handshake was the whole problem. Committed, **not yet deployed to Scale**, and the
end-to-end cold tab timings have not been re-benched since.

Two things were considered and rejected:

- **Raising `_CONCURRENCY`.** Unnecessary once handshakes went; it would trade a solved
  problem for a throttling risk.
- **A negative cache for refused subscriptions.** Caching a 401/403 hides access that has
  just been granted. Dropped.

Measurement trap found the hard way: `asyncio.run()` leaves `_inflight` tasks bound to a dead
event loop, so a second scenario in the same process measures contaminated numbers. **One
process per scenario.**

---

## 6. Pending work — in the order I would do it

Done on 30 Sep and no longer pending: server-side warming (`383f72e`), Always On enabled
on Scale, Anomalies and Billing opportunities cached (`e99b71c`), the cold-path profile and
both fixes it produced (`9920e2d`, `d3a6fcb` — §5a), and the full suite, which ran at
`d3a6fcb`: **2220 passed, 7 failed, 10 skipped, 1 error, 14m13s with `-n 4`** (log in the
session folder, §8). See §3–§5.

1. **Deploy `d3a6fcb` to Scale and re-bench the cold path.** `9920e2d` is already live and
   verified; the shared ARM client is committed but not deployed, and no end-to-end cold
   timing exists for Insights/Recommendations after it. Bench the three tabs in §5a on a
   fresh `CLOUDLENS_BUILD_ID` before the warmer has seen the user, and replace the stale
   column in §5.
2. **Server-side filtering and paging** (approved by Ravi: "summary tables first, then
   this"). Uptime, Tags, Resources, Billing return KPIs + chart data + one page (~50 rows);
   filters/sort/page as query params; same look and feel; update `tests/browser_*.js` and
   frontend tests. This is what removes the 8–15MB payloads and the 2–4s repeat views.
3. **Resources cold ~30s.** SQL is ~17s (9.7s narrow ranking over `resource_costs` + ~6s
   detail gather); the rest is app-side on a 2-vCPU box, including ~2.3s of snapshot-cache
   body checks on a 15MB answer (`_safe_content` regex scan 1.5s). Paging (item 2) shrinks
   both.
4. **Verify Entra sign-in warming on live** (§4): after a deploy, sign in via SSO, confirm a
   `sign-in` pass runs for that oid (its targets' `computed` moves) and the next views hit.
5. **The seven pre-existing test failures, still unfixed.** Proved pre-existing on 30 Sep
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
6. `/api/schedules` is 8.2s cold and not a snapshot-cached route (0.08s repeat from its
   own cache). Small; check whether it belongs in `DEFAULT_POLICIES`.
7. Unconfirmed: live July partition looks bloated (≈20GB heap for 5.0M rows vs ≈10GB for
   5.8M in August, 0 dead tuples). Not investigated.
8. Carried from 28 Sep and still open: `stash@{0}` (`env_tags`, Environment filter
   always empty), browser-suite clean pass, the two UI decisions in the old §7e.

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
  session folders `…\session-state\93969277-…\files\` (up to `a1bc0cd`),
  `…\session-state\e27b8924-…\files\` (warming: `live_probe.py` prints live meta, snapshot
  and warm-target state read-only; `live_watch.py` follows a warm pass; both bench
  outputs), and `…\session-state\1ccfca3b-…\files\` (the 30 Sep full-suite log
  `suite-d3a6fcb.log` and the clean-tree baseline `suite-baseline-clean-tree.log` that
  proves the seven failures pre-existing). Not in the repo. Copy the probes into the new
  session's folder if useful.

---

## 9. First moves in the new chat

1. `git status` (only `tools/bench-tabs-latest.json` should be dirty) and
   `git log --oneline -8` — top should be this handover, then `d3a6fcb`, `fbbf999`,
   `9920e2d`, `e99b71c`, `383f72e`. Scale is running `9920e2d`, so `main` is ahead of
   live by one code commit that matters (§2).
2. Start the Scale database if stopped; confirm the firewall rule for today's IP (§1).
3. Ask Ravi for the live admin password (process environment only) and re-run
   `tools\bench_tabs.py` against live for today's baseline. Expect hits almost everywhere:
   the 19 warm targets were computed after the last deploy and Always On keeps them warm.
4. Pick up §6 item 1 (deploy `d3a6fcb`, re-bench the cold path, replace the stale §5
   column), unless Ravi prefers item 2 (paging).
5. Before believing any number: read §7 first, and re-measure in isolation. Two of the
   entries there are traps this session walked into and had to back out of.
