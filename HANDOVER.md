# CloudLens — handover, 30 September 2026

Supersedes the 28 Sep handover, kept as `HANDOVER-2026-09-28.md`. Its architecture
section (§6) and measurement traps (§3) still hold and are not repeated here; its
performance table, test list and open items are stale.

**State: every data tab on the Scale deployment answers, and repeat views come from the
snapshot cache in 0.1–3.7s. What remains is the first view after a deploy or data
publish, the tabs that wait on live Azure, and payload size.**

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
6. **`main` is 9 commits ahead of the `backup` remote and has not been pushed.**
   `git push` needs credentials that are not cached on this machine. Ask Ravi before
   pushing.

---

## 1. Running, testing, deploying

Repo: `C:\Users\raviverma\OneDrive - Microsoft\Documents\Microsoft Scout\cloud-lens-v2`
Venv: `C:\Users\raviverma\.venvs\cloudlenstony\Scripts\python.exe`

**Tests** (the suite refuses an implicit database):

```powershell
$env:CLOUDLENS_PG_TEST='localhost'; $env:CLOUDLENS_PG_USER='postgres'
$env:PGPASSWORD='6352'; $env:PGSSLMODE='disable'
python -m pytest tests\test_usage_summaries.py -q          # targeted
```

- Do not pass `-p no:cacheprovider` — conftest needs `config.cache`.
- Full suite ≈ 34–40 min serial. Launch it detached: write a small `.ps1` that sets the
  env vars and redirects pytest to a log, then `Start-Process pwsh -File <it>`.
- Lint: `python -m ruff check app tests\<files>` (targets py3.11: no nested same-quote
  f-strings; SIM rules are on).
- The local PostgreSQL is **shared with other sessions** (e.g. `D:\cloud lens v3`
  runs its own suites and rebuilds). Check `pg_stat_activity` before timing anything.

**Deploy** (≈ 5 min, then background derived builds):

```powershell
.\tools\package.ps1 -Destination <zip> -SourceRoot (Get-Location).Path -Python <venv python>
az webapp deploy -g CloudLens-Scale -n cloudlens-web-scale --src-path <zip> --type zip --async false
```

On startup the app builds any derived table missing from the stamp **on its own**,
while the existing tables keep serving (§4). Watch progress with `pg_stat_activity`
and the meta key `data_rollup_tables`.

**Live benchmark:** `tools\bench_tabs.py` with `BENCH_BASE=https://cloudlens-web-scale.azurewebsites.net`,
`AUTH_USERNAME=admin`, `AUTH_PASSWORD=<ask Ravi; never commit it>`. Pass paths or group
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
| App settings | `PG_WORK_MEM=128MB`, `PG_STATEMENT_TIMEOUT=180s` (read-only connections only), `SNAPSHOT_CACHE_ENABLED=true`, `SNAPSHOT_WARMING_ENABLED=true`, `AUTH_ALLOW_LOCAL=true`, `AUTO_INGEST=false`, `AUTO_REFRESH=false` |
| Data | 16.9M cost rows, 2026-07-01 → 2026-09-28, 70 subscriptions (HDFC demo) |
| Sign-in | SSO shows only the user's own RBAC subscriptions; use local `admin` to see all 70 |

Deployed commit: **`a1bc0cd`** (30 Sep). All 11 derived tables stamped.

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
- Keys include the **app version**, so **every deploy makes every view cold** (§6, item 1).
- Stored in PostgreSQL (TOAST-compressed); byte limits count the uncompressed JSON.
- `/api/budgets` and `/api/insights` are cached (MONITOR); a budget POST bumps
  `budget_revision` so a cached "no budgets" cannot outlive a create.
- Local accounts get a stable cache partition; each Entra user has their own partition,
  so each user's first view per day is cold.

---

## 5. Performance — live, 30 Sep after `a1bc0cd`

`tools\bench_tabs.py`, full run right after deploy (so "cold" = first view after deploy).
Full output: session file `live-bench-after-2.txt`.

| Tab | Before this session | Cold now | Repeat now | Payload |
|---|---|---|---|---|
| Resources | 500 at 180s | 29–36s | 3.7s | 15.3MB |
| Tags | 184–212s | 26s | 2.3s | 10.5MB |
| Uptime | 78–94s | 15s | 1.3s | 7.8MB |
| Insights | 209–220s | 49s | 0.07s | 6KB |
| Commitments | 98–101s | 100s | 0.09s | 61KB |
| Recommendations (AI) | — | 32s | 0.07s | — |
| AI Billing | 500 at 180s | 8.8s | 0.6s | 47KB |
| Budgets | 504 at 240s | 0.3s | 0.07s | 4KB |
| Billing | — | 0.75s | 0.11s | 388KB |
| Anomalies | 2.2s | 2.1s | **2.3s (never cached)** | 5KB |

45 endpoints: 42 × 200; the three 400s (`change-analysis`, `performance`,
`service-health`) need parameters the bench does not send — by design.
Repeat views of Resources/Tags are 2–4s because of payload size, not queries.

---

## 6. Pending work — in the order I would do it

1. **Server-side warming after deploy and after data publish.** The app version is in the
   cache key, so each deploy (and each publish) makes every user's views cold: Resources
   ~30s, Tags 26s, Uptime 15s, Insights 49s, Commitments 100s. `SNAPSHOT_WARMING_ENABLED`
   today is the *browser* warmer (`web/assets/cache-warming.js`) — it only re-fetches URLs
   a user already has open. What is needed is the server computing the heavy views itself
   after startup/publish. Caveat: cache partitions are per principal, so decide whether to
   warm per known user or change what the partition covers for warehouse-sourced data.
2. **Commitments (100s), Insights (49s), Recommendations (32s), Billing opportunities
   (14s), Savings (12s) cold.** These wait on live Azure/Advisor/AI calls; not yet
   profiled. Profile first — the collation and "discarded pass" patterns were only found by
   timing statements.
3. **Server-side filtering and paging** (approved by Ravi: "summary tables first, then
   this"). Uptime, Tags, Resources, Billing return KPIs + chart data + one page (~50 rows);
   filters/sort/page as query params; same look and feel; update `tests/browser_*.js` and
   frontend tests. This is what removes the 8–15MB payloads.
4. **Resources cold ~30s.** SQL is ~17s (9.7s narrow ranking over `resource_costs` + ~6s
   detail gather); the rest is app-side on a 2-vCPU box, including ~2.3s of snapshot-cache
   body checks on a 15MB answer (`_safe_content` regex scan 1.5s). Paging (item 3) shrinks
   both.
5. **Anomalies** recomputes on every view (~2.1s); find why the cache refuses it.
6. **7 failing tests — all pre-existing** (identical set before and after this session's
   work):
   `test_budget_reading`, `test_datastore_safety::test_summary_does_not_label_unknown_plus_usd_as_single_currency`,
   `test_overview`, `test_postgres::test_rows_written_by_copy_come_back_intact`,
   `test_postgres::test_duplicate_spellings_preserve_charges_pending_authoritative_reingest`,
   `test_summary_pending::test_warehouse_summary_returns_pending_after_an_exists_probe`,
   `test_visuals` (399/400: "the server hands over the parsed filter for that link").
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
- **Timing right after a deploy is noisy.** Autovacuum/analyse of new tables and cold
  tab builds on 2 vCPUs tripled some timings (Resources read 30s, then 10s, for the same
  7-day query). Re-measure in isolation before believing a regression.
- An early "10s" Resources measurement was the 7-day query saved by mistake — check the
  window in any captured SQL before comparing.

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
  session folder `…\session-state\93969277-…\files\`, not in the repo.

---

## 9. First moves in the new chat

1. `git status` (only `tools/bench-tabs-latest.json` should be dirty) and
   `git log --oneline -8`.
2. Start the Scale database if stopped; confirm the firewall rule for today's IP.
3. Re-run `tools\bench_tabs.py` against live to get today's baseline.
4. Pick up §6 item 1 (server-side warming).
