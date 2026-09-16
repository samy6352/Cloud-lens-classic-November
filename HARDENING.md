# Production Hardening Checkpoint

This batch changes local source only. No deployment, production data migration, Git
commit, branch, reset, or stash was performed. The snapshot includes the pre-existing
uncommitted contents of modified files.

## Rollback

The source snapshot and SHA-256 manifests are outside this repository:

`%LOCALAPPDATA%\CloudLens\checkpoints\prod-hardening-20260908-221829`

Preview from the repository root in PowerShell:

```powershell
$checkpoint = Join-Path $env:LOCALAPPDATA 'CloudLens\checkpoints\prod-hardening-20260908-221829'
.\tools\rollback-hardening.ps1 -Checkpoint $checkpoint -WhatIf
```

To restore this batch, run the same command without `-WhatIf`. The script checks all
files first and refuses if any changed after this batch or any backup is corrupted.
It only restores recorded files and removes recorded new files, including itself.
Unrelated changes are untouched. Already-restored files are skipped. Preserve the
external checkpoint until release validation is complete.

This restores source code, not cloud resources, deployed packages, or imported data.
Rolling back also reintroduces the reviewed security risks. Do not use Git reset
for rollback: the starting worktree already contained substantial uncommitted work.

## Behavior Changes

- Entra subscriptions require a successful uncached subscription-wide cost query
  under the user's delegated identity. Without a delegated token, access is
  resolved as the app and counts only cost-capable role assignments, bounded by
  what the app's own identity can read.
  Permission errors and throttling fail closed; no cached cost access is granted.
- Warehouse scope matches subscription IDs only. Ingest status exposes only an
  allow-listed set of nonfinancial progress fields.
- OAuth redemption requires the initiating browser's HttpOnly correlation cookie,
  matching state, and a flow younger than ten minutes. A second simultaneous login
  in that browser replaces the cookie; the older flow must be restarted.
- Chat is read-only, including for admins. Budget creation remains available through
  the existing authenticated Budgets form. Chat does not yet have a server-held
  proposal and approval workflow. Client history retains bounded plain text only;
  system/developer roles and tool transcripts are discarded.
- `PG_POOL_SIZE` now bounds concurrent checkouts as well as idle connections.
  `PG_POOL_TIMEOUT_SECONDS` defaults to 10; exhausted requests fail with a timeout.
- Replacement writers acquire a PostgreSQL table lock within the transaction before
  reading or deleting rows. Imports serialize across workers; ordinary readers can
  continue. This is deliberately broader than per-subscription locking.
- Export downloads and decompression are capped at 64 MiB per file. Runs are capped
  at 100,000 retained rows. Oversized runs fail before replacement. Parquet decoding
  is lazy and parsing is off the event loop. Disk-backed staging for larger runs is
  still pending; these guards are not a large-estate ingestion solution.
- CSV downloads neutralize formula-like text. Raw cost CSV accepts
  `machine_raw=true` for machine consumers only; never open that raw export in a
  spreadsheet. JSON and Parquet preserve exact values. XLSX table strings are stored
  as text cells.

## Release Gates

Run offline checks with:

```powershell
python -m pytest -m "not postgres and not network and not azure" -q
python -m ruff check app tests
```

Before production, validate concurrent replacement and failure rollback on an
explicitly isolated PostgreSQL database using the safety guidance in tests/README.md.
Also verify Entra login/callback cookies in a browser, allowed and denied cost access
with test identities, normal dashboard progress and exports, pool exhaustion behavior,
and representative import sizes. These live and browser gates were not run in this
batch. Do not infer production readiness from offline mocks or lock-order tests.
