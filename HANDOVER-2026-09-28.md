# CloudLens — handover, 28 September 2026

Supersedes the 18 Sep handover, kept alongside as `HANDOVER-2026-09-18.md`. Its
architecture and measurement-trap sections are carried forward here; its performance
table and open-item list are stale.

**State: the app is fast. The blocker for customer deployment is now test-suite health
and the fact that a large body of work is uncommitted.**

Everything below is measured or read from the code. Guesses say so.

---

## 0. Read this first — the three things that will bite you

1. **Nothing is committed.** `HEAD` is `f9919a1`, but the entire grain-split /
   partitioning / FX / retention body of work is sitting in the working tree as
   uncommitted modifications plus ~20 untracked files. There is **no remote**. A
   careless `git checkout` or `git stash` destroys weeks of work. **Commit before
   doing anything else.** See §2.
2. **Never edit a source file while `pytest` is running.** Several tests read or `exec`
   source at run time, so a mid-run edit produces phantom failures. This cost time this
   session: a run reported 10 failures, two of which (`test_onboarding`, `test_fx_rollup`)
   existed only because files changed underneath it. A clean re-run gave 8.
3. **8 tests fail, and they are pre-existing — this was proven, not assumed.** See §5.

---

## 1. How to run it

```powershell
cd 'C:\Users\raviverma\OneDrive - Microsoft\Documents\Microsoft Scout\cloud-lens-v2'
C:\Users\raviverma\.venvs\cloudlenstony\Scripts\python.exe -m uvicorn app.main:app `
    --host 127.0.0.1 --port 8100 --log-level warning
```

Sign in at <http://127.0.0.1:8100> — `admin` / `cloudlens-local-6352`
(`AUTH_USERNAME` / `AUTH_PASSWORD` in the gitignored `.env`; local account, not SSO).

Tests — the suite refuses any implicit database fallback, so the vars are required:

```powershell
$env:CLOUDLENS_PG_TEST='localhost'; $env:CLOUDLENS_PG_USER='postgres'
$env:PGPASSWORD='6352'; $env:PGSSLMODE='disable'
C:\Users\raviverma\.venvs\cloudlenstony\Scripts\python.exe -m pytest -q
```

Serial run takes **~19m45s**. `-n 4` is much faster but two tests flake under it on a
loaded machine (`test_deployment.py`,
`test_job_postgres.py::test_raw_csv_stream_exceeds_old_cap_and_stays_scoped`).
**Always re-run a failure in isolation before believing it.**

Endpoint benchmark — the most reliable measurement tool on this box:

```powershell
C:\Users\raviverma\.venvs\cloudlenstony\Scripts\python.exe tools\_bench_local.py
# optionally: ... tools\_bench_local.py /api/subscriptions /api/onboarding
```

Browser suites (31 of them) need the server live on :8100 plus
`CLOUDLENS_USER`/`CLOUDLENS_PASSWORD`: `node tests/run_browser_suites.mjs`.
A11y: `node tests/audit_a11y.mjs`.

Lint: `python -m ruff check app/`.

---

## 2. Repository state — **uncommitted, no remote**

Branch `main`. `package.json` names `github.com/samy6352/CloudLens.git` but no remote is
configured.

```
f9919a1 Build a derived table with CREATE TABLE AS, so PostgreSQL may use every core
70330fb Stop a generation stamp vouching for a table it never covered
ff934b9 Keep a restored estate from being mixed with the deployment's own costs
64829af Put runtime tuning in the template, where a redeploy cannot discard it
57746c6 Let the deployment script choose the database it is sizing for
83b9c7f Stream a FOCUS import instead of holding it, and drop the row cap that hid it
```

**Uncommitted and untracked — this is the bulk of the current product:**

Modified: `app/backfill.py`, `app/billing.py`, `app/billing_rollup.py`,
`app/currency.py`, `app/dashboard.py`, `app/derived.py`, `app/exports.py`,
`app/jobs.py`, `app/main.py`, `app/onboarding.py`, `app/presence.py`, `app/rollup.py`,
`app/snapshot_cache.py`, `app/store.py`, `app/warehouse.py`, `.env.example`,
`web/assets/{app,dashboard-billing,dashboard,filters}.js`,
`tests/{browser_billing.js,test_focus_incremental.py,test_rollup.py,test_rollup_routing.py,test_snapshot_cache.py}`

New: `app/billing_serving.py`, `app/derived_partitions.py`, `app/environment.py`,
`app/fx_rollup.py`, `app/read_indexes.py`, `app/retention.py`,
`web/assets/cache-warming.js`, `tools/{_bench_local,_scope_audit,plan_retention}.py`,
plus 13 new test files and `tests/browser_reliability.js`.

Also outstanding: `stash@{0}` — `app/env_tags.py`. See §7.

---

## 3. The machine and the data — read before trusting any number

| | |
|---|---|
| PostgreSQL | 18.6, local |
| Database size | **18 GB** |
| `shared_buffers` | 8 GB |
| `work_mem` | 192 MB |
| Buffer hit ratio | **75.3%** |
| `costs` | 5,821,770 rows, month-partitioned |
| `costs_rollup` | 81,898 |
| `resource_presence` | 2,797,817 |
| `billing_rollup` | 2,804,850 |
| `billing_serving` | 12,461 |
| `costs_fx_rollup` | 82,570 |
| Data range | 2026-07-01 → 2026-09-17 |

**The database cannot fit in memory.** Every detail-table query re-reads from disk. This
is a property of the box, not a bug, and it is the single most important fact about local
performance.

Consequences:

- The same suite measured **193s** and **579s** on the same commit hours apart.
- Page-load timings varied 6.0s → 9.3s → 20.9s → timeout across identical runs.
- **Endpoint benchmarks beat browser page-load numbers.** Prefer `tools/_bench_local.py`.
- Heavy probing (a full scan, a rebuild) evicts the cache and poisons everything measured
  for the next 10-20 minutes. If results suddenly worsen, suspect this first.
- A server restart leaves the snapshot cache cold: `/api/overview` returns 20 bytes and
  `sections` takes 3.5s until warming completes. **Wait ~45s after boot before
  benchmarking**, or you will chase a phantom regression.

---

## 4. Performance — current, measured

`tools/_bench_local.py`, warm server, 28 Sep:

| Endpoint | Cold | Warm | Was (18 Sep) |
|---|---|---|---|
| `/api/overview` | 0.04s | 0.01s | 0.2s / 0.01s |
| `/api/dashboard/sections` | 0.23s | 0.01s | 0.2s / 0.01s |
| `/api/filters?days=30` | 0.43s | 0.01s | 5.5s / 0.02s |
| `/api/dashboard/executive?days=30` | 6.45s | 0.01s | 8-10s / 0.01s |
| `/api/subscriptions` | **0.13s** | **0.13s** | **4.4s, never cached** |
| `/api/onboarding` | **0.01s** | **0.01s** | **2.6-3.0s, never cached** |
| `/api/dashboard/billing?days=30` | 0.37s | 0.02s | 451s originally |

### What this session fixed

**`/api/subscriptions`: 4.4s → 0.13s (~34x).** The ARM call was never the problem — the
18 Sep handover's open question. `backfill.present()` had four faults at once:

- ran `SELECT DISTINCT "SubAccountId" FROM costs` against the 5.8M-row **detail** table
  (~2.0s measured);
- was called **twice per request** — once in `schedule()`, once in `status_for()`;
- ran **synchronously on the event loop**, so while it worked nothing else was served;
- used `Reader.rows()`'s **default `limit=200`**, so an estate with more than 200
  subscriptions silently looked unloaded past row 200 and re-queued backfills for data
  the warehouse already had. That one is a correctness bug, not just a slow one.

Fix (`app/backfill.py`, `app/main.py`): read `Reader.agg` instead of `costs`; `limit=None`;
new `have=` keyword so the set is read **once per request** and handed to both callers;
moved off the loop with `offload`.

Proof the source swap is safe: `agg` and `costs` return **set-identical** subscription ids
(70 ≡ 70, zero divergence in either direction) at 0.040s vs 2.0s. `Reader.agg` already
falls back to `costs` whenever the derived table cannot answer the connection exactly, so
it cannot serve stale truth.

**`/api/onboarding`: 2.6-3.0s → 0.01s (~260x).** `warehouse.row_count()`
(`SELECT count(*) FROM costs`, 1.6-1.8s) was used only to test `rows == 0`.
`warehouse.has_rows()` (EXISTS, ~1ms) already existed for exactly this — commit `1099b6e`
introduced it and this caller was missed. `onboarding.state()` now takes
`rows: int | None`, where `None` means "not empty, nobody counted"; the published figure is
therefore never a *wrong* number. The `rows` field is consumed by no UI code, no test and
no documentation — checked before changing it.

**Derived-roster audit — closed, no defect.** An earlier note claimed `DERIVED_MODULES`
listed four tables while the database held five. That was reading the literal tuple on
`warehouse.py:55`; `fx_rollup` is appended on lines 56-57 when `fx_rollup.ENABLED`.
Measured: `ENABLED=True`, roster = 5, `roster - db = {}`, all five fresh. Every "extra"
relation in the schema is a physical partition (`_a`/`_b` suffix) or a base table.

---

## 5. Test status — and the 8 failures

Clean serial run, 28 Sep: **2129 passed, 8 failed, 10 skipped** (19m44s).
`ruff check` on the changed files: clean.

The 10 skips are expected: 8 need `CLOUDLENS_AZURE_TEST=1`, 1 needs Windows symlink
privilege, 1 needs an isolated database.

### The 8 failures are pre-existing — proven

I reverted `app/backfill.py`, `app/main.py` and `app/onboarding.py` to `HEAD`
(verified with `git diff`), re-ran the 8, and got **the identical 8 failures**, then
restored my changes. They are not regressions from this session's work.

```
tests/test_datastore_safety.py::test_every_setup_failure_returns_the_pool_slot
    [SELECT DISTINCT "BillingCurrency"] [unknown-currency]
    [CREATE TEMP VIEW costs] [cancelled]
tests/test_focus_schedules.py::test_existing_refresh_contracts
    [test_quick_replacement_filters_whole_protected_days_and_binds_scope]
tests/test_production_hardening.py::test_scope_predicate_never_matches_display_names
tests/test_release_runtime.py::test_release_runtime
tests/test_visuals.py::test_visuals   (399/400 — "opening data starts alongside navigation")
```

**Cause: test-harness drift from the uncommitted grain-split work.** These tests build
hand-rolled `namespace` doubles whose signatures no longer match the real code — e.g.
`test_production_hardening` fakes `_prepare_connection`, which has since grown
`period_filter`, `filters` and billing-shadow handling. Same class of problem as the known
`MemoryWarehouse.connect()` contract mismatch (`connect()` is a `@contextmanager` offline
but returns the connection itself in production).

**These must not stay red before a customer deployment.** They are test debt rather than
product bugs, but `test_production_hardening` guards *scope isolation* and
`test_datastore_safety` guards *pool-slot return under setup failure* — precisely the two
properties you least want silently broken in a multi-tenant deployment.

### Browser suites

31 suites. Last measured 19/31 passing; earlier 18/31 under benchmark load — the
difference was load contamination, not code. Known concrete UI faults, all pre-existing:

- `browser_drilldowns` — strict-mode violation: `locator('dialog')` matches **both**
  `tabSearchDialog` and the forecast dialog. A newer dialog broke an older selector.
- `browser_focus_schedules` — "Only five schedules rendered initially".
- `browser_subsection_styles` — Overview header layout diverges from Billing
  (`sameRow:false, areaLabels:0`).

**Not re-run since this session's fixes.** Worth one clean pass on an idle machine.

---

## 6. Architecture you need to know

**Vanilla ES modules, not React.** The only npm dependencies are `playwright` and
`axe-core`, both test-only. React advice does not apply.

**Derived read path.** The dashboard almost never reads `costs`:

- `costs` — 5.8M-row detail table, month-partitioned (`CLOUDLENS_PARTITION_COSTS=1`).
- `costs_rollup` (81,898) — pre-aggregated money.
- `resource_presence` (2.80M) — per-resource identity and billed money.
- `commitment_benefits` — sparse, exact benefit/resource usage for commitment coverage and
  inventory; only rows with a benefit identity are retained.
- `spot_rates` — daily cost and quantity by meter, unit, region, currency and pricing model.
- `billing_rollup` (2.80M) → `billing_serving` (12,461) — the **grain split**. The rollup
  carries `resource_id` *and* `ResourceName`, so it barely collapses the detail table;
  `billing_serving` drops resource identity for the KPI / subscription / service
  questions that never needed it. That split is what took Billing from 451s to 0.37s.
- `costs_fx_rollup` (82,570) — native-currency conversion, gated on `fx_rollup.ENABLED`
  and on a freshness check at read time.

`Reader.agg` / `.res` / `.named` / `.bill` / `.benefit` / `.spot` name the derived relation
**when it can answer that connection exactly**, and `costs` otherwise. A converted currency or a
filter outside the grain puts a query back on the detail table — that is usually why something
is unexpectedly slow. **When you need a distinct dimension value, read `Reader.agg`, not
`costs`** — that was the whole of the `/api/subscriptions` bug.

Each derived module works out which filters it can serve **from its own grain**
(`derived.filterable`). Do not reintroduce a shared answer: `presence` used to borrow
`rollup.serves_filters`, and `e7a681f` is exactly the change that would have broken it.

`DERIVED_MODULES` (`warehouse.py:51-57`) is the single roster binding the rebuild, the
boot-time `ensure`, the column check and the generation stamp together. **Adding a derived
table without adding it there** means a relation the reader trusts but nothing rebuilds.

**Derived rebuilds** go through `derived.rebuild` — staging-swap into an inactive side then
`CREATE OR REPLACE VIEW`. Full rebuild via `CREATE TABLE AS` (not `INSERT … SELECT`, which
PostgreSQL never parallelises): 2445.7s → 924.6s, 2.65x. Windowed to the months an import
touched: ~4.9s. `refresh_rollup(window=)` ignores the window after any failed rebuild.

**Snapshot cache** (`app/snapshot_cache.py`) — durable, keyed on principal + tenant + scope
+ path + query + generation. Invalidated when `data_generation` advances. Cold-vs-warm is
the whole story for `executive`: the first view after each rebuild pays, everyone after
does not. The remaining lever there is **cache warming**, not query tuning.

**`offload()`** (`main.py:1177`) — psycopg is synchronous, so any warehouse call made
directly from an `async def` handler blocks the entire event loop. Every blocking call in
a handler must go through it. Both defects fixed this session were partly this.

---

## 7. Open items

**a. The 8 failing tests.** Highest priority before deployment (§5).

**b. `/api/dashboard/executive` cold 6.5-9s.** Caches to 0.01s. Already established as
irreducible by query tuning — five approaches were measured and rejected (shared dedup
pass, forced parallel workers, and three others). The browser warmer covers this path after a
source-generation change for URLs already viewed, but it cannot remove the first cold miss for a
new session or deployment. The remaining levers are warm-start policy and database capacity.

**c. `stash@{0}` — `app/env_tags.py`.** The Environment filter is always empty
(`unavailable_dimensions: ["env"]`) because `env` is read from the `Tags` JSON per row,
costs ~161s on this estate, and gives up at its 5s budget. The stash resolves it once per
ingest. **Design right, cost wrong:** full rebuild exceeded 24 minutes and was cancelled.

Two measured facts that save repeating work:
  - A pure-SQL `pg_input_is_valid` replacement for the `try_cast_json` PL/pgSQL shim gives
    identical answers (0 disagreements over 441,393 rows) but is **slower** — 71.6s vs
    38.0s — because inlining defeats the common-subexpression folding the opaque function
    was getting.
  - PostgreSQL already folds the four `TRY_CAST` calls: 4-parses-per-row 170.9s vs
    1-parse-per-row 161.9s over 2.33M rows. The parse count is not the problem; the
    per-row PL/pgSQL call with an `EXCEPTION` block, marked `PARALLEL RESTRICTED`, is.

Likely next idea: resolve `env` during **ingest** (in `staging`, where rows are already
being touched once) rather than in a rebuild pass over all history.

**d. Browser suite clean pass** on an idle machine (§5).

**e. Two changes deliberately NOT made — they need Ravi's decision.**
  - **Firing `dashboardReady` early** for progressive widget loading. It is a contract
    ~25 browser suites and 4 frontend tests depend on; flipping it makes them act on an
    unpopulated page. Currently gated on `executive`, which is why it fires late.
  - **Lazy-loading `chart.umd.min.js`** (206 KB, eager). Real saving, but risks visible
    chart pop-in, and the standing instruction was not to trade visual quality.

**f. Remaining `FROM costs` callers.** Many are legitimately detail-only (tags, resources, raw
export). Commitment coverage/monthly totals and the currency census now use the exact rollup;
benefit inventory and Spot comparisons use dedicated sparse/compact rollups. Those tables join
the normal generation stamp and rebuild, so they cannot be read before they are current. Other
detail callers still need endpoint-level measurements before refactoring.

---

## 8. Working agreements that produced good results

- **Measure before and after, on the same warm machine.** Several "obvious" optimisations
  have been rejected by measurement here, including ones that looked certain.
- **Prove a failure is pre-existing rather than assuming it.** Reverting three files and
  re-running took ten minutes and turned "probably not mine" into a fact.
- **A failing test may be the test, not the code — but check.**
- **Prefer endpoint benchmarks to browser timings** on this box (§3).
- **Never edit source during a test run** (§0).
- Temporary scripts are `tmp_*.py` / `tmp_*.mjs` at the repo root and are deleted after
  use. The repo has no `tmp` gitignore entry — do not commit them.
- Commit messages explain *why*, with the numbers that justified the change.

---

## 9. Suggested first moves in the new chat

1. `git add -A && git commit` the working tree. It is the single largest risk here.
2. Fix the 8 harness-drift failures (§5) — start with `test_production_hardening`, since
   it guards scope isolation.
3. Clean browser-suite pass on an idle machine.
4. Benchmark the remaining `/api/dashboard/*` routes; only then consider §7f.
