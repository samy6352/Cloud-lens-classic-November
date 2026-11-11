# Export-backed subscription billing

The app's **Export-backed subscription billing** link opens `/export-reporting`.
This is a deliberately narrow complete-run billing report, not a way to make
export subscriptions available to the live dashboard, agent, inventory, budgets,
optimization or resource actions. Existing live authorization is unchanged.

## Operator setup

Reports, explicit grants and audit records use the existing PostgreSQL warehouse
pool, managed-identity authentication and configured `PGSCHEMA`. No billing or
grant data is stored in SQLite or on `/home` network-mounted files. Separate
`export_reporting_*` tables keep these immutable snapshots out of live warehouse
tables. Back them up with the application database. Multiple workers/replicas
share committed grants; reads never cache authorization.

Schema version 1 is installed transactionally on first use, serialized with a
PostgreSQL advisory lock across workers. An unknown version fails closed.
The existing runtime role must already have schema USAGE/CREATE privileges;
do not grant broader roles to bypass a failed prerequisite.
`CLOUDLENS_READ_ONLY` blocks imports, grant changes and initial schema creation,
but permits existing approved report reads. Publish evidence, report and audit
atomically; errors roll back the entire publication. Existing local experimental
SQLite stores are not silently imported or treated as authoritative grants.

Enable `EXPORT_REPORTING_ENABLED=true` and set `EXPORT_REPORTING_SOURCES` to a
JSON object (single-line when supplied through environment/app settings):

```json
{
  "example": {
    "label": "Example subscription",
    "subscription": "11111111-1111-4111-8111-111111111111",
    "account": "examplestorage123",
    "container": "focus-exports",
    "prefix": "focus/example/approved-complete-run",
    "export_name": "example-focus-export"
  }
}
```

The source is pinned to one run. Do not substitute the export root: importing
multiple/overlapping manifests is refused. The existing read-only blob client
uses the app's normal Azure credential chain. No SAS, keys, copied CLI token,
storage network changes, ARM role changes or export creation are needed.
Storage must already be readable from the runtime's approved network/identity.
Nothing automatically refreshes or requests a new Azure export.

## Administrator onboarding and recipient approval

1. Sign in with an **existing Entra app administrator**. Local admins, API keys,
   expired sessions and revoked app administrators cannot onboard.
2. Open `/export-reporting`, select the operator-approved source, and click
   **Import pinned run**. Every partition, row count, byte count, date window,
   subscription ID, currency and cost is validated before publication.
   Failure publishes no report or grant and never writes to the live warehouse.
3. Select the returned immutable dataset. Copy the recipient's
   `entra:<tenant UUID>:<object UUID>` from **their authenticated report page**
   (they can see their identity even with no approved reports), or use **Use my
   authenticated principal** for the current administrator. Do not infer identity
   from an Owner assignment, email or display name.
4. Click **Approve read access**, then select the report in the
   **Export-backed subscription** picker. Even administrators need explicit grants.
5. To revoke, select the same dataset/principal and click **Revoke read access**.
   The next catalog/report request checks fresh grants, without session/snapshot
   cache reuse. Already delivered data cannot be recalled from a browser.

`GET /api/export-reporting` exposes only explicitly granted datasets.
`GET /api/export-reporting/{dataset UUID}/billing` reads only that report.
`GET /api/export-reporting/admin`, `POST /api/export-reporting/import` with
`{"source":"example"}`, and `POST /api/export-reporting/{dataset UUID}/grant` with
`{"principal":"entra:...:...","allow":true}` require the current Entra app
administrator policy. Mutations also require a matching `Origin` header.
No endpoint accepts arbitrary storage URLs or subscription lists.

The report contains all daily/service costs in the selected run, with original
currencies kept separate, billed and effective costs, charge counts, source
submission time, manifest coverage and last import time. Decimal cost strings
preserve original precision; credits and zero charges are retained. This is a
snapshot, not a final invoice. A new run requires a new dataset and fresh grants;
old reports stay immutable. There is no CSV download or background report job.

Import limits: 60 partitions, 256 MiB encoded data, two million charge rows,
100,000 aggregate groups, plus existing parser decoded-byte safety limits.
Larger runs fail closed; this surface does not silently truncate them.

## Validation and release checks

Run `tests/test_export_access.py`, `tests/test_export_reporting.py`,
`tests/test_focus_incremental.py` and `tests/test_delegated_default.py` against an
explicitly configured empty local test PostgreSQL database (see the test guide).
Never point tests at the deployed application database. Route tests use synthetic
authenticated identities and mocked blob storage, but real PostgreSQL persistence.
Coverage includes transactional rollback, concurrent schema installation,
independent worker pools, immediate revocation, cross-tenant isolation, current
administrator rechecks, malformed runs and unchanged live delegated scope.

Before enabling in production, verify the existing runtime PostgreSQL identity's
schema privileges and blob connectivity from the integrated VNet. An approved
blob private endpoint exists in the classic VNet; public storage access remains
disabled. Resource configuration alone is not proof of a successful runtime read.
Final import and recipient approval require a real current Entra administrator
session at `/export-reporting`; never synthesize that session or infer grants.

## Release acceptance and rollback

Keep deployment-specific subscription IDs, storage paths, import totals, recipient
identities and verification evidence in the operator's protected change record,
not in this reusable example.

Before acceptance, verify readiness, real Entra administrator sign-in, a complete
pinned import, explicit recipient approval, denial after revocation and unchanged
live Azure authorization. Anonymous API requests must remain unauthorized.
Compilation, packaging and mocked-storage tests do not establish customer acceptance.

Retain a verified prior source ZIP and reviewed app settings before deployment.
Rollback is a reviewed source-only ZIP redeploy and restoration of the previous
export settings. Retain additive PostgreSQL tables rather than destroying any
subsequently approved data. Never enable an export report by inferring a grant
from an Azure role or by synthesizing an administrator session.
