# Tests

```powershell
pip install -r requirements.txt -r requirements-dev.txt

# Pure/frontend tests run with the normal collector, without a database:
python -m pytest tests/test_dashboard_navigation.py tests/test_dashboard_frontend.py
python -m pytest -m "not postgres and not network and not azure"

# For the database suite, use a disposable test server, NEVER production.
$env:CLOUDLENS_PG_TEST = "<server>.postgres.database.azure.com"
$env:CLOUDLENS_PG_USER = "<role>"
$env:PGPASSWORD = (az account get-access-token --resource-type oss-rdbms --query accessToken -o tsv)

pytest                  # everything that can run here
pytest -n 4             # the same, in parallel (about three times faster)
pytest tests/test_scope.py -v
```

Selected PostgreSQL tests fail once with a configuration error if the test host or role
is missing. They are not silently skipped and there is no embedded database fallback.
`--collect-only` and selections that exclude database tests need no server.

Database scripts get private schemas; ordinary database fixtures manage their own.
Cleanup drops these schemas, so only use a server dedicated to testing. Authentication
state and script workspaces are isolated under `.pytest_cache`; local `.env` is not loaded.
Billing, reporting-period and native-currency tag tests use the shared `postgres_warehouse`
or `module_postgres_warehouse` fixtures. These pin host, role and database to
`CLOUDLENS_PG_TEST`, `CLOUDLENS_PG_USER` and `CLOUDLENS_PG_DB` (default `postgres`), generate
a unique disposable schema, and close pools/drop schemas during teardown. Inherited
`PGHOST`, `PGHOSTADDR`, service/password-file settings and `PGSCHEMA` are removed before
collection; only explicitly supplied `PGPASSWORD`, `PGPORT` and `PGSSLMODE` are retained.
Before warehouse initialization, these shared fixtures perform a read-only catalog check
and refuse any tables/views in `public` or `cloudlens_fn`. Use an empty, dedicated test
database, even on a dev server: startup migration can move legacy shared tables into the
selected schema, and teardown would then delete them. Compatibility functions may be
installed in `cloudlens_fn`; that schema is shared within the dedicated test database.

## How the suite is put together

`conftest.py` runs legacy scripts, including their `unittest.main()` entry points,
as subprocesses. This isolates module-level environment changes and `SystemExit`.
Each script is one pytest item; its exit status and captured output determine the result.
`test_release_runtime.py` also imports a real warehouse at module scope. It runs through
an isolated child pytest process with a private PostgreSQL schema, rather than opening
the database during parent collection. This is import-time initialization debt, not an
offline test; new tests should inject their dependencies instead.

A few files are ordinary assert-style pytest modules (`test_postgres.py`,
`test_rollup.py`, `test_sqldialect.py`, `test_store_default.py`, `test_query_guard.py`,
`test_cost_cache.py`, `test_assets.py`) and are collected normally. Write new tests in that
style; the script style is history, not a pattern to copy.

Frontend-only checks do not use the warehouse:

```powershell
python -m pytest tests/test_dashboard_frontend.py tests/test_dashboard_explore.py tests/test_assets.py
python tests/test_visuals.py
```

`test_dashboard_frontend.py` uses Node.js to check the lazy modules and the Savings polling
lifecycle (force once, abort on departure, resume, errors and bounded polling).
`test_dashboard_explore.py` covers exact ARM portal targets, encoded names, unsafe URLs,
multiple subscriptions, nested details and recursive credential-field exclusions.
`browser_dashboard.js` is a Playwright function accepting an existing CloudLens `page`; it
creates and closes its own mocked browser context, testing partial-result interaction state,
stable table sorting/paging, scope isolation and cached navigation timings without Azure calls.
It also covers row-click/keyboard exploration, exact resource links, nested-detail state after
refresh, and separate portal choices for multiple subscriptions.
`browser_tooltips.js` uses the same Playwright-function convention and a separate context to
exercise mouse, keyboard, touch, safe text, viewport placement and Chart.js values with zero
hover requests. It also checks that a short hint is click-through while a scrollable one keeps
hover, and that a line chart's tooltip is placed clear of the caret.
`test_uptime_frontend.py` covers the Hourly Billing tag pickers and the budget panel they open:
which budgets a tag key and value actually match (on the filter Azure holds, not on the name),
case-insensitive keys against case-sensitive values, budgets that filter on more than the
selection, and that no live budget read happens until a value is chosen.
`browser_focus_schedules.js` and `browser_focus_workflow.js` exercise the current Cost data
tabs, not the retired schedules form. The browser runner passes their shared
`createCostDataFixture` from `cost_data_browser_fixture.mjs`; direct invocations must pass
the same helper as the second argument. It uses real reports/CSS assets from the source
page's origin and intercepts all fixture traffic, rejecting unexpected requests.
Coverage includes scoped cadence review/cancellation, approval tokens and retry keys,
read-only export inventory, durable import progress, cached return and header routing.
`browser_premium.js` checks the real shell across all three themes and desktop/mobile widths,
including readable text contrast, full-height navigation alignment and overflow.
`test_azure_updates.py` and `test_service_health.py` exercise official-feed schemas, stale/error
semantics, bounded transport/paging, event-type coverage and user/subscription isolation offline.
The dashboard browser fixture also covers the three Azure Updates sections loading independently,
source failure versus an empty feed, customer filters, refresh-state preservation and safe links.
Billing adds `test_billing.py` for calendar boundaries, price decomposition, currency coverage,
scope and statement reconciliation; `test_billing_ingest.py` for nullable provenance and
non-destructive schema/import upgrades; and `test_billing_opportunities.py` for compatible
estimates, lifecycle bounds and unavailable metadata. The browser fixture checks that historical
answers appear before live estimates, alternatives are not added together, and missing evidence
is not shown as zero savings.
The shell publishes `cl:dashboard-performance` events and `cloudlens:<tab>:initial|cached`
Performance measures, separating feedback, API, module-load and render time.

Every script test is given:

| | |
|---|---|
| `PGSCHEMA` | for database scripts only: a private schema, dropped afterwards |
| `AUTH_DATA_DIR` | a private directory, so signing in cannot write into `data/` |
| a clean environment | `AUTH_*`, `COST_*`, `PG*` and friends are stripped, so your shell cannot change the result |

## What a test may depend on

A legacy script defaults to `postgres` to preserve its existing database contract.
Use a real comment (not text inside a docstring) to declare dependencies:

```python
# cloudlens-test: requires-warehouse
```

For a genuinely database- and network-free script, use `requires-offline`. Ordinary
pytest functions/classes use `@pytest.mark.postgres` for database tests, including tests
in otherwise offline modules. `warehouse` also implies `postgres`. Conflicting
`offline` and external-dependency markers are rejected.

| Marker | Meaning | Run by default? |
|---|---|---|
| `postgres` | Needs an explicitly configured test PostgreSQL server. | yes; missing configuration is an error |
| `requires-offline` | Script needs no database, network or Azure credentials. | yes |
| `requires-warehouse` | Needs a populated warehouse; gets the seeded fixture below. | yes |
| `requires-network` | Needs outbound internet. MSAL fetches OpenID metadata when it builds a client, which is the only reason any of these need it. | yes |
| `requires-azure` | Needs Azure credentials and a real subscription. | no — set `CLOUDLENS_AZURE_TEST=1` |

Use `pytest -m "not postgres and not network and not azure"` for offline selection.
Packaging and deployment regressions require PowerShell and Python; frontend execution
checks require Node.js. The deployment tests mock every Azure/HTTP operation.

## The seeded warehouse

`fixture_warehouse.py` builds three subscriptions, sixty days, four services, some
tagged resources and some untagged, through the application's own writer so the schema
cannot drift. Costs are exact binary fractions, so totals compare equal without a
tolerance: the whole fixture is $1,680.00, split $960 / $480 / $240.

Tests used to read whatever warehouse happened to be on the machine. That made them pass
for the author and fail for everyone else, and it meant a red test could mean either a
regression or simply a different month of data. Nothing reads the real warehouse now.

## Required inputs

| Variable | Purpose |
|---|---|
| `CLOUDLENS_PG_TEST` | the isolated PostgreSQL test server, required only for selected DB tests |
| `CLOUDLENS_PG_USER` | the role to connect as |
| `PGPASSWORD` | a password or an Entra access token; unset uses the ambient Azure identity |
| `CLOUDLENS_PG_DB` | the database, if not `postgres` |
| `PGPORT` | PostgreSQL port, default `5432`; preserved in isolated script subprocesses |
| `PGSSLMODE` | TLS mode; use `disable` only for an isolated local test server |

## Optional inputs

| Variable | Enables |
|---|---|
| `CLOUDLENS_AZURE_TEST=1` | the `azure` tests |
| `CLOUDLENS_TEST_SUBSCRIPTION` | the subscription `test_tags.py` reads |
| `CLOUDLENS_SAS_URL` | the container SAS `test_sas_ingest.py` reads |
| `CLOUDLENS_TEST_TENANT` | the directory the sign-in routing tests resolve against |

## A note on `-n auto`

Prefer `-n 4`. Because each script test is itself a subprocess, `-n auto` starts one
worker per core *and* a child for each, oversubscribing the machine two to one. On a
twelve-core laptop that turned a two-minute run into a stall.
