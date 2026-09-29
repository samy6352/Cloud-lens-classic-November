# CloudLens v3 — Master Plan and 24-Hour Sprint

> **Created:** 30 Sep 2026 00:45 IST · **Owner:** Ravi Verma · **Status:** approved plan, sprint not started
> **How to use this file in a new chat:** read sections 1–4 for context, then execute **section 7 (24-hour sprint)** task by task,
> updating the **status tracker (section 12)** as you go. Sections 8–11 are the longer v3 roadmap after the sprint.
> This file deliberately contains **no secrets** — credentials come from the local environment / `.env` (never commit them).

---

## 1. Goal

Turn CloudLens (Azure FinOps dashboard, currently "v2" in this repo) into **v3**: a product that a customer installs **inside their own
Azure subscription with two lines in Cloud Shell**, which on first start **creates its own FOCUS cost exports into a new storage account,
backfills history, loads it, and serves a superfast, premium, interactive dashboard** — with all heavy processing done in PostgreSQL.

Two horizons:
- **24-hour sprint (section 7):** a *v3 preview* built on top of v2 (reuse ~70% of v2; change only what v3 requires).
- **12-week roadmap (sections 8–11):** the full from-scratch-quality v3 (React UI, split identities, in-DB pipeline, RLS, reconciliation).

---

## 2. Requirements (from Ravi — non-negotiable)

| # | Requirement |
|---|---|
| R1 | Scalable, superfast, super-interactive, premium UI |
| R2 | Deployed inside each **customer's own Azure subscription** |
| R3 | Data source = **Cost Management FOCUS export scheduled to blob** → ingest from blob → dashboard |
| R4 | All permissions granted at deployment by **a few lines of script**; **plain Azure RBAC only** — no EA enrollment / billing-role assignments |
| R5 | **First run of the app** creates the FOCUS export in a **new storage account**, backfills history, then loads |
| R6 | **All processing happens in the database**; the app stays thin |
| R7 | **Low cost per customer** — Azure Data Explorer rejected as too expensive |
| R8 | Speed over freshness: data up to 24 h old is fine; a manual Refresh exists |

## 3. Decisions log

| Date | Decision | Why |
|---|---|---|
| 29 Sep | Evidence-gated approach; every phase ends at a measured gate | Past fixes proved the bottleneck was data shape, not engine |
| 30 Sep | **ADX rejected** | 2 × E2d_v5 + ADX markup ≈ **$540/mo** compute (Central India) |
| 30 Sep | **PostgreSQL Flexible only** | ≈ $73–416/mo; `azure_storage` can read Parquet from blob in-DB |
| 30 Sep | **No EA / billing roles**; subscription or management-group RBAC only | Keep customer install to a few lines |
| 30 Sep | **One FOCUS export per subscription** | Management-group FOCUS exports are not supported |
| 30 Sep | Build a **v3 preview in 24 h on top of v2**, then continue the 12-week roadmap | v2 already has most infra and export plumbing |

## 4. Verified platform facts (with sources)

- **Export run API** — `POST {scope}/providers/Microsoft.CostManagement/exports/{name}/run?api-version=2026-06-01`, optional body
  `{"timePeriod":{"from","to"}}`, **max range 1 calendar month** → used for backfill.
  https://learn.microsoft.com/rest/api/cost-management/exports/execute
- **Exports** — FOCUS dataset, formats CSV(+Gzip) or **Parquet(+Snappy)**, file partitioning always on, `manifest.json` per run,
  overwrite optional; "Export selected dates" allows **13 months** history in 1-month chunks; **new exports can take up to 24 h**; once a
  run starts data lands within ~4 h (often ~15 min). Management-group exports: **EA only, usage only** (no FOCUS).
  https://learn.microsoft.com/azure/cost-management-billing/costs/tutorial-improved-exports
- **Firewalled storage** — export creator needs `Microsoft.Authorization/roleAssignments/write` on the storage account; Cost Management
  gives the export a system MI with Storage Blob Data Contributor; "Allow trusted Azure services" must be on; storage
  `publicNetworkAccess` must be **Enabled (selected networks)**, not Disabled; "Permitted scope for copy operations" = any account.
- **Subscription-scope exports** give **FOCUS only** (no price sheet / reservation details / recommendations datasets).
  https://learn.microsoft.com/cloud-computing/finops/toolkit/hubs/configure-scopes
- **Azure PostgreSQL Flexible (PG 16–18)**: `azure_storage` 1.9 (`blob_list`, `blob_get` with decoder **`parquet`**, auth via the
  **server's managed identity**), `pg_cron` 1.6, `pg_partman` 5.4, `hll`, `tdigest`, `pg_hint_plan`. **No** columnar / pg_duckdb.
  https://learn.microsoft.com/azure/postgresql/extensions/concepts-storage-extension ·
  https://learn.microsoft.com/azure/postgresql/extensions/reference-azure-storage-extension ·
  https://learn.microsoft.com/azure/postgresql/extensions/concepts-extensions-versions
- **Prices** (Azure retail API, Central India, USD PAYG, 730 h): PG B2s ≈ $73 · D2ds_v5 ≈ $183 · E2ds_v5 ≈ $204 · E4ds_v5 ≈ $416;
  App Service Linux B1 ≈ $13–15 · B2 ≈ $29; ADX 2×E2d_v5 incl. markup ≈ $540.

---

## 5. Current state — v2 (what already exists)

**Repo:** `C:\Users\raviverma\OneDrive - Microsoft\Documents\Microsoft Scout\cloud-lens-v2` · branch `main` · remote `backup` →
`https://github.com/samy6352/CloudLensv2.git` (ask Ravi before pushing).
**Size:** 80 Python files / ~36K lines (`app/main.py` 250 KB, 94 routes), ~18K lines vanilla JS, `web/assets/app.css` 5,305 lines,
Chart.js, 21 tabs (list in `web/assets/dashboard.js` ≈ lines 348–381), 168 test files.

### 5.1 Reusable v2 assets (do NOT rebuild these in the sprint)

| v3 need | v2 asset |
|---|---|
| Infra in customer sub | `infra/main.bicep`, `infra/modules/stack.bicep` (807 lines): VNet, PG + blob private DNS, blob private endpoint, PG Flexible (`-PostgresSku Standard_E2ds_v5`, `PremiumV2_LRS`, 3000 IOPS, PG 16), UAMI, App Service, App Insights, Foundry (AI) |
| Entra app automatically | `Microsoft.Graph/applications` + `servicePrincipals` in Bicep |
| Firewall-safe export creation | `storageUserAccessAdmin` role in `stack.bicep`, ABAC condition limits delegation to Storage Blob Data Contributor (`ba92f5b4-…`) |
| Installer | `infra/deploy.ps1` (PowerShell 7; params `-Location -ResourceGroup -PostgresSku -CostScopeAllVisible -EnableExportManagement -SkipFocusExports -SkipCode -WhatIf …`), `infra/preflight.ps1`, `infra/cost-access.ps1`, `infra/grant-cost-access.ps1` (Reader, Cost Management Reader, + Cost Management Contributor with `-EnableExportManagement`) |
| Create/run exports **from the app** | `app/schedules.py`: `create()` (PUT export, `_grant_storage()` for firewalled storage), `run_now()` (POST run with `timePeriod`), `listing()`, `remove()`; `app/capabilities.py` checks export rights |
| Script-created exports | `infra/focus-exports.ps1` (api-version `2025-03-01`, `FocusCost`, `CreateNewReport`, **format `Csv`** today) |
| Manifest-driven loader | `app/exports.py` (reads `manifest.json`, supports `.csv/.csv.gz/.parquet/.snappy.parquet`, Parquet batch 65,536), `app/scopes.py`, `app/staging.py`, `app/derived.py` (CTAS rebuilds) |
| Fast serving tables | `billing_serving`, `hourly_usage`, `usage_signals`, `resource_tags` (last three **uncommitted**) |
| Security default | `AUTH_ALLOW_LOCAL` is opt-in only (`app/auth.py:416`) |
| Benchmarks | `tools/bench_tabs.py`, `tools/_bench_local.py`, `tools/bench-tabs-latest.json` |
| Browser tests | `npm run test:browser` (`tests/run_browser_suites.mjs`), `npm run audit:a11y` |

### 5.2 Uncommitted work at plan time (commit in task S0)
Modified: `.env.example`, `app/ai_billing.py`, `app/esu.py`, `app/exports.py`, `app/jobs.py`, `app/main.py`, `app/resources.py`,
`app/snapshot_cache.py`, `app/tags.py`, `app/uptime.py`, `app/warehouse.py`, several `tests/*`, `tools/bench-tabs-latest.json`,
`web/assets/app.js`, `web/assets/dashboard.js`.
Untracked: `app/hourly_usage.py`, `app/resource_tags.py`, `app/usage_signals.py`, `tests/test_usage_summaries.py`,
plus scratch files `_probe_range.sql`, `tmp_live_probe.py`, `tmp_*.log` (do **not** commit scratch files).

### 5.3 Measured baseline (`tools/bench-tabs-latest.json`, 29 Sep 22:55, local 17M-row estate)
25 endpoints · **3 return 400** (`/api/dashboard/change-analysis`, `/performance`, `/service-health`) · **13 are > 1 s cold** · 6 > 0.3 s warm.

| Endpoint | Cold s | Warm s |
|---|---|---|
| `/api/insights` | **225.70** | **220.59** |
| `/api/dashboard/commitments?days=30` | **101.71** | **97.38** |
| `/api/dashboard/recommendations?days=30` | 33.36 | 0.06 |
| `/api/dashboard/esu?days=30` | 31.27 | 0.06 |
| `/api/dashboard/savings?days=30` | 13.53 | 0.07 |
| `/api/schedules` | 8.27 | 0.06 |
| `/api/dashboard/governance?days=30` | 7.98 | 0.06 |
| `/api/dashboard/rightsizing?days=30` | 6.27 | 0.06 |
| `/api/dashboard/waste?days=30` | 5.97 | **5.88** |
| `/api/dashboard/health` | 2.26 | 0.07 |
| `/api/dashboard/anomalies?days=30` | 2.19 | 2.13 |

### 5.4 Local environment (no secrets here)
- Venv: `C:\Users\raviverma\.venvs\cloudlenstony`
- Tests: set `CLOUDLENS_PG_TEST=localhost`, `CLOUDLENS_PG_USER=postgres`, `PGSSLMODE=disable`, and `PGPASSWORD` **from your local
  environment**, then `python -m pytest`.
- Run app: `python -m uvicorn app.main:app --host 127.0.0.1 --port 8100` (local admin credentials: from local `.env`, not this file).
- Scale environment: rg `CloudLens-Scale`, https://cloudlens-web-scale.azurewebsites.net, PG `cloudlens-postgres-scale` — MCAPS stops
  it nightly: `az postgres flexible-server start -g CloudLens-Scale -n cloudlens-postgres-scale` (~4 min); client IP changes between
  sessions, so a new firewall rule may be needed.

### 5.5 Hard-won lessons (apply everywhere)
1. **Never edit source files while pytest is running** — several tests read/exec source at run time.
2. PostgreSQL **never parallelises `INSERT … SELECT`**; use **`CREATE TABLE AS`** for big builds (2445 s → 925 s measured).
3. **Never benchmark with `SELECT count(*) FROM (…)`** — PostgreSQL elides the aggregates. Use `EXPLAIN (ANALYZE, BUFFERS)` or
   materialise the real result.
4. Default row limits silently truncate (`r.rows()` limit=200 bug) — always paginate explicitly and flag truncation.
5. Disk IOPS matter: P4 (120 IOPS) broke production; use Premium SSD v2 ≥ 3,000 IOPS or ≥ P15.
6. Screens must read **small summary tables**, never the 5.8M-rows/month detail table.

---

## 6. Target architecture (v3)

```
Customer subscription — one resource group
 ├─ Cost Management: 1 FOCUS export per subscription ──Parquet──▶ Storage (exports container, firewalled + trusted services)
 ├─ Storage ──private endpoint──▶ PostgreSQL Flexible (VNet-injected, Entra-only)   ← ALL processing
 │     pg_cron discover manifests → leased loaders (azure_storage parquet → normalise via CTAS → marts) → safe publish
 ├─ Worker (not internet-facing): creates exports, backfills, reconciles with Cost Management, runs migrations
 └─ Web app (App Service, Easy Auth): thin API (calls api.* SQL functions only) + UI
```
Principles: complexity lives in the template, never in customer steps · one ingestion path (everything lands in blob, PG ingests) ·
app moves bytes + renders, SQL computes · screens read marts, line items are drill-down only · zero secrets · every number
reconcilable with Azure · nothing internet-facing holds admin rights.

**Customer install (target):**
```powershell
az account set -s <subscription-id>
iex "& { $(irm https://<host>/install.ps1) } -Location centralindia"      # optional: -Scope <mg-or-sub> -Size small|medium|large
```
**Deployer needs:** Owner on host subscription · Owner/User Access Administrator on the monitored scope · Entra app-registration right
(or an Entra admin pre-creates the app and passes `-EntraClientId`).

**App identity RBAC (assigned automatically):** Reader + Cost Management Contributor on monitored scope · Storage Blob Data Contributor ·
ABAC-limited User Access Administrator on storage (exists) · PG Entra login. **PG server MI:** Storage Blob Data Reader (for `azure_storage`).
**Export MI:** assigned by Cost Management.

**Known subscription-scope limits:** no unused-commitment cost, no price-sheet dataset (FOCUS ListCost/ContractedCost suffice),
reservation recommendations via Advisor (Reader). Optional billing-scope add-on later.

---

## 7. 24-HOUR SPRINT — v3 preview on top of v2  ⟵ START HERE

**Rules for the sprint**
- Work on branch **`v3`**. One commit per task, message ending with
  `Co-authored-by: Copilot <223556219+Copilot@users.noreply.github.com>`. Ask Ravi before any `git push`.
- Never edit source while pytest runs. Re-bench with `tools/bench_tabs.py` after S1. Measure with real results (lesson 3).
- Azure test deployments go into a **temporary resource group** (proposed `rg-cloudlens-v3-test` in the CloudLens-Scale subscription,
  Central India) and are **deleted at the end** (≈ $7–10/day). Get Ravi's OK before creating Azure resources.
- If a task overruns its box by > 50 %, stop, record findings in section 12, move on.

### S0 · Freeze and branch (H0–H1)
1. `git status`; commit the in-flight summary-table work (5.2) on `main` as a checkpoint — **exclude** scratch files (`tmp_*`,
   `_probe_range.sql`); add them to `.gitignore` if appropriate.
2. `git switch -c v3`.
3. Run full pytest (env in 5.4) → record pass/fail counts in section 12.
4. Start the app locally, run `tools/bench_tabs.py`, save as `tools/bench-tabs-v3-baseline.json`.
**Done when:** branch `v3` exists, tests green (or known failures listed), baseline saved.

### S1 · Speed: fix the slowest screens (H1–H6)
Targets: **every benched endpoint < 1 s cold and < 300 ms warm; zero 400s.**
1. Profile `/api/insights` (226 s), `/api/dashboard/commitments` (102 s / 97 s warm), `/api/dashboard/waste` (5.9 s warm),
   `/api/dashboard/anomalies` (2.1 s warm), `/api/anomalies/alerts` (1.3 s warm) with `EXPLAIN (ANALYZE, BUFFERS)`; find the source
   module (`app/insights.py`, `app/commitments.py`, `app/waste.py`, `app/anomalies.py`).
2. For each: precompute into a **serving table built during the derived rebuild** (same pattern as `billing_serving`: CTAS, keyed by
   month/subscription, invalidated by data version), and make the endpoint read only that table.
3. Cold paths that call live Azure APIs (recommendations 33 s, ESU 31 s, governance 8 s, rightsizing 6 s, savings 13.5 s):
   warm them in the background after start/load (existing warming in `app/snapshot_cache.py` / browser warming) so the first user never
   pays the cold cost; return cached data with a "refreshing" flag.
4. The three 400s (change-analysis, performance, service-health): determine cause (likely no ARM access locally); return **200 with an
   explicit empty-state payload** (`{"available": false, "reason": …}`) instead of 400; update UI to render the empty state.
5. Tests for each serving table (row-set equality vs the old query on a fixture), re-bench → `tools/bench-tabs-v3-s1.json`.
**Done when:** targets met or each miss explained in section 12.

### S2 · First run creates exports and backfills — inside the app (H6–H10)
Build on `app/schedules.py` (`create`, `run_now`, `_grant_storage`) and `app/capabilities.py`.
1. **Format:** change export body to **Parquet + Snappy** and set FOCUS dataset version (`1.0r2`) in both `app/schedules.py::_body`
   and `infra/focus-exports.ps1`; keep `dataOverwriteBehavior = CreateNewReport`. Confirm the loader path (`app/exports.py`
   `_rows_from_parquet`) handles Snappy Parquet end-to-end with a fixture file.
2. **Bootstrap orchestrator** (new `app/bootstrap.py`, started from `lifespan` in `app/main.py`, guarded by a PG advisory lock so only
   one instance runs; feature flag `CLOUDLENS_MANAGED_EXPORTS=true`):
   - self-check capabilities (export write/run, storage role-assignment write) and surface missing roles with the exact fix command;
   - discover subscriptions visible to the app identity (ARM `GET /subscriptions`, follow `nextLink`);
   - for each subscription ensure exactly one CloudLens FOCUS export (daily MTD) exists (idempotent; reuse `owned()`);
   - queue runs in a PG table `export_jobs(subscription_id, month, status, attempts, next_attempt_at, last_error, run_started_at)`:
     current month first, then previous **12 months newest-first, round-robin across subscriptions**; call `run_now()` with a
     1-calendar-month `timePeriod`; honour `429`/`Retry-After` with exponential backoff; cap concurrent runs;
   - on days 1–6 of each month re-queue the previous month (late adjustments); newly visible subscriptions auto-onboard.
3. **Setup page** (Admin/"Cost data" tab): progress grid months × subscriptions (queued / running / landed / loaded / failed), and the
   expectation text "first data usually within hours; brand-new exports can take up to 24 h".
4. Tests: mock ARM with httpx `MockTransport` — idempotent create, backfill ordering, 429 backoff, restart resumes from table.
**Done when:** on a fresh DB with mocked ARM the queue fills correctly and drains; on real Azure (S3 deployment) exports are created and
runs accepted (HTTP 200/202).

### S3 · Two-line Cloud Shell installer (H10–H13)
1. New `infra/install.ps1` (PowerShell 7, runs in Azure Cloud Shell): params `-Location` (required), `-Scope` (default current
   subscription; accepts `/providers/Microsoft.Management/managementGroups/<id>` or a subscription id), `-Size small|medium|large`
   (maps to `-PostgresSku` B2s-or-D2ds_v5 / D2ds_v5 / E4ds_v5 and storage), `-EntraClientId` (optional).
2. Steps: verify `az` login and pwsh 7 → register providers (`Microsoft.CostManagementExports`, `Microsoft.DBforPostgreSQL`,
   `Microsoft.Web`, `Microsoft.Storage`, `Microsoft.Network`, `Microsoft.ManagedIdentity`, `Microsoft.OperationalInsights`,
   `Microsoft.Insights`, `Microsoft.CognitiveServices` if AI kept) → `deploy.ps1` with mapped params and `-SkipFocusExports`
   (the app now creates exports) and `-EnableExportManagement` → role assignments on scope (Reader + Cost Management Contributor; at MG
   scope one assignment covers all subs) → set app setting `CLOUDLENS_MANAGED_EXPORTS=true` → wait for `/healthz` → print URL.
3. Cloud Shell compatibility: `tools/package.ps1` defaults `-Python python`; detect and pass `python3` when `python` is absent.
   Remove any Windows-only paths.
4. Distribution (decide with Ravi): **public GitHub release** (`install.ps1` downloads a release zip) **or** a zip handed to the
   customer and uploaded to Cloud Shell. For the sprint, run from a clone.
5. Test: fresh deploy to `rg-cloudlens-v3-test`; record wall-clock time; verify URL, sign-in, export creation (S2). Keep for S5, delete in S6.
**Done when:** two commands → working URL; rerun is idempotent; time recorded.

### S4 · Premium UI refresh on the existing JS (H13–H18)
No React rewrite in the sprint.
1. **Navigation:** regroup the 21 tabs (`web/assets/dashboard.js` ≈ 348–381) into 5 workspaces + Admin:
   - **Overview:** overview
   - **Explore:** billing, uptime ("Hourly Billing"), tags, resources, anomalies, history, ai-billing
   - **Optimize:** savings, commitments, performance
   - **Govern:** budgets, governance, esu, health (Retirements), changes, azure-updates
   - **Reports:** report (Export data)
   - **Admin:** settings (Cost data + new Setup progress), audit (admin only)
2. **Design tokens** in `web/assets/app.css` `:root` (colour scale, type scale with tabular numerals, spacing, radii, elevation, motion
   durations); **dark theme** via `[data-theme="dark"]` + toggle persisted in `localStorage`; respect `prefers-reduced-motion`.
3. **Skeleton loaders** instead of spinners for KPI tiles, charts and tables; smooth tab transitions.
4. **⌘K / Ctrl+K command palette** (vanilla JS, fuzzy match over workspaces/tabs/subscriptions/actions such as Refresh, Theme).
5. Number formatting option: Indian lakh/crore toggle.
6. Run `npm run test:browser` and `npm run audit:a11y`; capture before/after screenshots into the session folder (not the repo).
**Done when:** browser suites pass, a11y audit no new violations, screenshots reviewed by Ravi.

### S5 · Spike: load inside the database with azure_storage (H18–H20)
On the **test deployment's** PostgreSQL only (never the Scale server):
1. Enable server params: `azure.extensions` += `AZURE_STORAGE,PG_CRON`; `shared_preload_libraries` += `azure_storage,pg_cron`;
   restart. Enable the server's **system-assigned managed identity**; grant it **Storage Blob Data Reader** on the storage account.
2. `CREATE EXTENSION azure_storage;` register the account with the managed-identity option
   (`azure_storage.account_add(azure_storage.account_options_managed_identity('<account>', 'blob'))` — **verify exact signature in the
   reference doc**), then `azure_storage.blob_list(...)` on the exports container.
3. Load one real FOCUS Parquet run via `blob_get(..., NULL::<typed row>, decoder := 'parquet')` into a table with **CTAS**; measure
   rows/s and memory vs the Python loader for the same run; check type fidelity (timestamps, decimals, JSON tags).
4. Write results as an ADR in section 12. **Switch the loader only if** it is correct and at least as fast; otherwise keep the Python
   byte-mover (transforms stay in SQL either way).

### S6 · Verify end to end and hand over (H20–H24)
1. Full pytest; `npm run test:browser`; re-bench → `tools/bench-tabs-v3-final.json`; compare with baseline in section 12.
2. Fresh install once more from the two-line command → exports created → backfill queued/running → dashboard renders (data may lag up
   to 24 h for new exports — record status).
3. **Delete `rg-cloudlens-v3-test`** (and any exports it created) unless Ravi asks to keep it.
4. Update section 12 (status, numbers, deferred items) and commit.

### Sprint acceptance scorecard (v3 preview vs v2)

| Check | v2 baseline | Target |
|---|---|---|
| Install steps | Windows + PowerShell 7 + Azure CLI + Python, `deploy.ps1` | **2 lines in Cloud Shell** |
| Exports + backfill | separate script, CSV | **automatic on first start, Parquet, 13 months** |
| Slowest screen | 226 s | **< 1 s cold / < 300 ms warm** |
| Endpoints returning 400 | 3 | **0** |
| UI | 21 flat tabs | **5 workspaces, dark mode, skeletons, ⌘K** |
| In-DB loading | none | **spike result recorded (go/no-go)** |

### Explicitly NOT in the sprint (roadmap)
React rewrite · separate web/worker identities · pg_cron pipeline as default · row-level security · "Reconciled with Azure" badge ·
partition per (month, subscription) with safe publish · collectors redesign · AI "Ask" redesign.

---

## 8. Roadmap after the sprint — full v3 (≈ 12 weeks to pilot MVP, 1 dev + AI)

### 8.1 Database design (all processing in PostgreSQL)
- Schemas: `raw` (unlogged staging) · `core` (facts + dims) · `mart` (screen summaries) · `app` (users, settings, jobs, export_runs,
  audit) · `api` (the only surface the web app may call).
- Dims with integer keys: subscription, resource_group, resource, service, region, meter, **tagset** (deduped tag JSON), tag.
- `core.cost_line`: `RANGE(month)` → `LIST(sub_key)`; each leaf = one export unit (subscription-month).
- Marts: `cost_daily` (day × sub × RG × service × region × charge/pricing category × commitment type × currency) · `cost_tag_daily`
  (configured tag keys only) · `cost_resource_month` · `usage_month` (hours `smallint[31]`) · `anomaly` (rolling median/MAD) ·
  `forecast` (run-rate regression) · `savings` (list/contracted/effective; *observed* commitment coverage) · `variance`.
- Pipeline: `pg_cron` `cl.discover()` every 5 min (paged `blob_list`, runs keyed by subscription, month, runId, manifest ETag) → N
  distinct loader jobs (pg_cron never overlaps a job with itself) each **leasing** one unit (`SKIP LOCKED`, lease expiry, retries,
  dead-letter), one transaction, no internal COMMIT: stage via `blob_get(parquet)` → upsert dims → CTAS normalised leaf → add NOT NULL
  + CHECK(month, sub) + matching indexes → ANALYZE → build mart rows in staging.
- **Safe publish** (short txn, `lock_timeout` + retry, only if the run is newer than the active revision): DETACH old leaf, ATTACH new
  (no validation scan, no index build), replace mart rows for (sub, month), bump per-(sub, dataset) version, NOTIFY; drop old leaf async.
- Retention via `pg_partman` (default 13 months). Support envelope v1: ≤ 250 subscriptions/instance; planner tested at 5,000 leaves.
- Security: roles `cl_owner` / `cl_pipeline` / `cl_api`; **FORCE ROW LEVEL SECURITY**; scope via `SET LOCAL` per transaction.
- Correctness: keyset pagination + truncation flags; follow every continuation token; never sum across currencies without an FX
  policy; perf gates via `EXPLAIN (ANALYZE, BUFFERS)`.

### 8.2 Identities, network, deployment
- **id-web** (App Service): `cl_api` DB role only. **id-worker** (Container Apps Job, not internet-facing): Reader + Cost Management
  Contributor on scope, Storage Blob Data Contributor, ABAC-limited UAA on storage, PG Entra admin (migrations).
  **PG server MI:** Storage Blob Data Reader.
- Easy Auth: auth-code flow with the web UAMI as **federated credential** (no client secret, no implicit grant). MVP authorisation =
  in-app allowlist seeded with the installer (no Entra P1 group dependency).
- Network: VNet (snet-app, snet-jobs, snet-pg, snet-pe), private DNS; storage `publicNetworkAccess=Enabled`, `defaultAction=Deny`,
  bypass AzureServices, blob private endpoint; startup probe = PG `blob_get` of a canary blob + export test write, with RBAC-propagation
  retries.
- Installer preflight uses `az deployment sub validate` to surface Azure Policy denies; `--uninstall`; idempotent rerun = upgrade;
  Entra mode (b) = admin runs `entra-setup.ps1` and passes `-EntraClientId`.

### 8.3 App and UI
- API: FastAPI + psycopg3 pool (Entra token); ~12 endpoints; cache keyed by per-(sub, dataset) versions + ETag/304; OpenAPI → TS client.
- UI: React 19 + TypeScript + Vite; TanStack Router (typed URL state) / Query / Table + Virtual; ECharts (lazy); Tailwind v4 +
  Radix/shadcn; Framer Motion; cmdk; Geist/Inter tabular numerals; dark/light; density; WCAG 2.2 AA; lakh/crore.
- IA: Overview · Explore (one explorer: group by any dims, cross-filter, drill breadcrumbs, compare periods, resource drawer with cost
  trend + uptime heatmap + tags + inventory) · Optimize · Govern · Reports · Admin (+ optional Ask).
- Budgets: initial JS < 200 KB gz · LCP < 1.5 s · API p95 < 300 ms · cached interactions < 100 ms.

### 8.4 Phases and gates

| Weeks | Phase | Gate |
|---|---|---|
| 1–2 | Spikes (before UI): Easy Auth + UAMI FIC; network (exports to firewalled storage + PG `blob_get` via PE); `azure_storage` parquet on real FOCUS; 5.6M-row month load + publish on real disks for B2s / D2ds_v5 / E4ds_v5; overwrite-off manifest behaviour; UI prototype | ADR with numbers, go/no-go |
| 3–4 | Installer + infra (Bicep, install/uninstall, entra-setup) | 2 lines → URL < 20 min on a clean sub; idempotent |
| 4–6 | DB core + pipeline + RLS + api schema + DB tests | idempotent reload; out-of-order runs never regress; reconciliation exact |
| 6–7 | Worker: orchestrator, reconciliation, setup/health UI | clean deploy → first dashboard, zero manual steps |
| 7–10 | API + React UI foundation + Overview + Explore + Excel export | Lighthouse ≥ 90, p95 < 300 ms, Playwright |
| 11–12 | Hardening + pilot: 20M rows/mo × 13 scale test, security pack, docs | pilot customer live |
| +8 | v1.1: collectors (ARG, Advisor, policy, retirements, changes, metrics), Optimize, Govern, PowerPoint/scheduled reports, saved views, SSE, group access, private-UI, Ask CloudLens | per-workspace parity + perf |

**Switch-over rule:** v3 replaces v2 only when (1) totals match Azure and v2 on the same estate, (2) every screen beats v2's measured
time, (3) clean install < 20 min, (4) it covers every tab the pilot customer uses. Until then v2 stays the demo/production app.

## 9. Sizing and cost per customer (compute verified; storage/network estimated)

| Tier | Rows/month | PostgreSQL | App | ≈ $/month |
|---|---|---|---|---|
| Small | < 1M | B2s (only if spike proves backfill on its real disk; else D2ds_v5) | B1 | 110–130 |
| Medium | 1–5M | D2ds_v5 + Premium SSD v2 3,000 IOPS | B2 | 240–260 |
| Large | 5–20M (e.g. 5.6M) | E4ds_v5 + Premium SSD v2 | B2 / P0v3 | 480–520 |
Every tier: defined minimum IOPS (never P4), 2.5× largest-month temp headroom, backfill concurrency per tier; option to bootstrap on a
larger SKU and downsize after backfill.

## 10. Risks and mitigations
- `azure_storage` parquet limits → keep Python byte-mover; transforms stay SQL.
- Cost Management throttling with many subscriptions → queue, backoff, newest-first.
- New exports take up to 24 h → progress UI + clear expectation.
- Customer Azure Policy denies → `validate` preflight; private-mode / ACR options.
- Entra app registration blocked → `-EntraClientId` mode.
- Subscription-scope blind spots → documented; optional billing add-on.
- Burstable CPU credits during backfill → tier floor decided by spike.
- Row-store ad-hoc limits → marts + drill-down design.
- Partition count → support envelope.

## 11. Open decisions for Ravi
1. Default retention 13 months?
2. Distribution: public GitHub release vs zip handed to customer (and container image: ghcr.io vs customer ACR)?
3. UI public-with-Entra (default) vs private-only?
4. Keep Foundry/AI in the default install or make it opt-in (`-EnableAi`)?
5. Pilot customer?

---

## 12. Status tracker (update as you go)

| Task | Status | Started | Finished | Result / notes |
|---|---|---|---|---|
| S0 Freeze & branch | ☐ not started | | | |
| S1 Speed fixes | ☐ not started | | | |
| S2 First-run exports in app | ☐ not started | | | |
| S3 Two-line installer | ☐ not started | | | |
| S4 UI refresh | ☐ not started | | | |
| S5 In-DB load spike | ☐ not started | | | |
| S6 Verify & hand over | ☐ not started | | | |

**Baseline tests:** _ · **Final tests:** _ · **Install wall-clock:** _ · **Test RG deleted:** _

### ADR notes
_(record spike outcomes and decisions here)_
