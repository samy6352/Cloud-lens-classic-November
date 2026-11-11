# Deploying CloudLens

CloudLens deploys into a single resource group using Bicep. Customer deployment
requires an existing customer-owned Entra registration with verified tenant-wide
delegated ARM consent **before** provisioning. See the [guided customer steps](../README.md#install-cloudlens)
and the generated customer release's deployment and acceptance documents.

```powershell
# Use the values collected and verified in the guided customer steps.
.\infra\preflight.ps1 -Location $appRegion -AiLocation $aiRegion -EntraClientId $clientId -RedirectUri $redirectUri
.\infra\deploy.ps1 -Location $appRegion -AiLocation $aiRegion -NameSuffix $nameSuffix -EntraClientId $clientId -WhatIf
```

`deploy.ps1` provisions the infrastructure, grants the app the cost access it needs, wires
sign-in, publishes the code, and polls `/readyz` plus an unauthenticated sign-in challenge.

## Common commands

```powershell
.\infra\deploy.ps1 -Location $appRegion -AiLocation $aiRegion -NameSuffix $nameSuffix -EntraClientId $clientId -SkipCode
.\infra\deploy.ps1 -Location $appRegion -AiLocation $aiRegion -NameSuffix $nameSuffix -EntraClientId $clientId -EmitArm
```

## Custom domain

After binding DNS and valid HTTPS certificates for the primary domain and every alias,
add the primary callback to the existing Entra app registration **without removing its
existing callback URLs**. Then set these App Service settings and restart the app:

```text
PUBLIC_BASE_URL=https://app.example.com
AUTH_REDIRECT_URI=https://app.example.com/auth/callback
```

`PUBLIC_BASE_URL` is optional; unset or empty keeps existing routing. A configured value
must be an HTTPS DNS origin with no port, userinfo, path (including a trailing slash),
query or fragment; invalid values fail startup. Keep `AUTH_REDIRECT_URI` on that same
origin and retain App Service HTTPS-only and secure-cookie settings.

Alternate hosts (including the original `azurewebsites.net` address) redirect GET/HEAD
to the primary before sign-in, preserving ordinary paths and queries. Old-host OAuth
callbacks restart at a clean `/login` without forwarding codes or state. Cookies remain
host-only; users may need to sign in again. Other methods return HTTP 421 with the primary
URL instead of replaying bodies or credentials; API clients must use the primary URL.
GET/HEAD `/healthz` and `/readyz` remain on each host for platform probes. Routing uses
the actual Host header, not client-supplied forwarded-host headers.

## Prerequisites

**Azure RBAC, on the subscription CloudLens is deployed into**

| Need | Why |
|---|---|
| **Owner** or **Contributor** | create the resource group and its resources |
| **Owner** or **User Access Administrator** | create the managed identity's role assignments |

`Microsoft.Authorization/roleAssignments/write` is the one the deployment cannot work
around: without it the stack builds and then fails on the grant.

**Azure RBAC, on the subscriptions whose costs are read.** `deploy.ps1` grants these to the
managed identity, so the deploying principal must be able to grant them.

| Role | Why |
|---|---|
| **Cost Management Reader** | cost analysis, query, and the Cost Details API |
| **Reader** | resolve subscription and resource names |
| **Cost Management Contributor** | *only* if CloudLens should create scheduled exports |

Management group assignments are inherited by every subscription beneath, which is the
least-effort way to cover an estate. Two caveats: management group scope is **not supported
for Microsoft Customer Agreement**, and the **Cost Details API does not support it for EA or
MCA either**. Per-subscription assignment is the portable choice.

**Microsoft Entra.** Sign-in is OIDC with a **public client using PKCE**, so there is **no
client secret** to create, store or rotate. A tenant that forbids app secrets is fine here.

| Need | When |
|---|---|
| Customer-owned single-tenant public-client registration already exists | before deployment |
| Tenant administrator approves ARM delegated `user_impersonation` on the exact service principal | before provisioning |
| Operator can read registration, service principals and OAuth grants | verification; no directory admin role needed to deploy |

## The two access modes

How CloudLens decides what each person may see. Pick this before deployment day — the
consent is usually granted by someone other than the subscription owner.

| | Delegated (default) | App-identity (`-AllowAppIdentityFallback`) |
|---|---|---|
| Who asks Azure | the signed-in person | the application |
| Needs admin consent | yes, ARM `user_impersonation` | no |
| What people see | exactly their own subscriptions | subscriptions they hold a cost-capable role on, among those CloudLens holds Reader on |

**Delegated is the default and the better answer.** Each person's own token asks ARM, so
Azure decides and nothing is bounded by what CloudLens itself was granted. Until an
administrator consents, delegated sign-in is blocked. `deploy.ps1` verifies actual
tenant-wide consent and the exact callback before provisioning, even with
`-SkipPreflight`. It never consents or edits an existing registration in this mode.

**Legacy app-identity mode is not the customer onboarding solution.** Existing
installations may explicitly retain `-AllowAppIdentityFallback`. CloudLens then asks Azure which subscriptions carry a
**cost-capable** role assignment for that person — Owner, Contributor, Reader, Cost
Management Reader/Contributor, Billing Reader, or a custom role granting cost actions. The
check is transitive, so group-granted access counts.

Its one real limitation: a subscription CloudLens's own identity has no Reader on cannot be
inspected, so it is never offered to anyone. Someone entitled to thirty subscriptions may be
shown four, and the dashboard would look healthy while missing money. Two things stop that
being silent:

- `deploy.ps1` reports missing grants for every selected cost subscription and prints exact
  administrator commands. `preflight.ps1` also identifies visible scopes outside the selection.
- The app reports the ceiling it drew the list from. `/api/auth/me` returns `access_mode` and
  `app_visible_subscriptions`, and someone with no access is told whether they lack a
  cost-capable role or whether CloudLens simply cannot see their subscription — different
  people to go and ask.

Unlike admin consent, closing that gap needs no directory rights, only Cost Management Reader
and Reader on each scope. Grant them everywhere people will analyse:

```powershell
./infra/deploy.ps1 -Location centralindia -CostScopeAllVisible
```

The mode is carried by the template (`authDelegatedArm`), not patched onto the web app
afterwards. No automatic fallback or upgrade occurs. Existing MI deployments must
explicitly retain their mode on redeployment; customer delegation does not remove
the managed identity or its separate unattended-ingestion grants.

To move an existing installation to delegated mode, have the identity team
preapprove its exact registration and callback, then use the guided deployment
parameters with explicit delegated mode. Do not blanket-consent unreviewed permissions.

```powershell
.\infra\deploy.ps1 -Location $appRegion -AiLocation $aiRegion -NameSuffix $nameSuffix -EntraClientId $clientId -DelegatedArm -WhatIf
```

## Naming and networks

### Dedicated export storage

Every fresh deployment gets its own storage account named `st` plus 20 randomly generated
hexadecimal characters (80 random bits). Reusing a friendly prefix or suffix does not reuse
another deployment's account. The account, private endpoint, DNS, containers, role assignments
and `ARCHIVE_ACCOUNT`/`ARCHIVE_ACCOUNT_ID` app settings all reference that single identity.
Use a separate resource group and naming state for each independent stack.

The random name is generated **once**, not on every deployment or refresh:

- PowerShell checks Azure name availability and records the chosen name in
  `data\deployments\<subscription-and-resource-group-hash>.storage.json`. This ignored local
  file is written even during `-WhatIf`; Azure resources are not changed by that preview.
  Retain it between preview and apply, and between pipeline runs. Use
  `-StorageNameStatePath <persistent-path>` for a pipeline-managed location.
- On updates, the script recovers and verifies the existing account from app settings,
  stack outputs/parameters, the blob private endpoint or unambiguous CloudLens ownership
  tags. Conflicting, missing or foreign accounts stop deployment rather than generating a
  replacement. `-StorageAccountName <existing-name>` supports reviewed legacy adoption; it
  cannot override an already configured account or repoint its private endpoint.
- Direct Bicep/ARM now requires `storageAccountName` in the reviewed parameter file. For a
  new stack, generate/check/save the name once using `infra\storage-naming.ps1`; for an
  existing stack, pass its actual account name. Do not use a rotating `newGuid()` default.
- Terraform requires an explicit `storage_account_name` migration decision. Set it to the
  existing account name on upgrade/import, or explicitly `null` for a genuinely new stack.
  `random_id` persists the generated identity in Terraform state, and a lifecycle
  precondition blocks subsequent name changes. Preserve and back up that state.

For example, upgrades of the reference `cloud-lens-cx` deployment must retain
`storageAccountName=cloudlensstrv0913`; new naming does not rename or migrate that account.

Storage provisioning and report scheduling are distinct. Automatic onboarding, startup and
unselected export refresh only read exports targeting the configured account; they no longer
probe other deployments' storage as a fallback. Owned exports are discovered at subscription
scope so billing-account discovery cannot hide them. Explicitly selected export IDs and
operator-supplied SAS imports remain available. First-run setup still requires an admin's
confirmation before creating recurring reports. If no owned report exists, the app explains
how to set one up and offers Quick estimates rather than silently borrowing another account.

### Other resource names and address ranges

For a fresh stack choose a resource group, a distinct `-NamePrefix`, and a distinct
`-EntraAppUniqueName` — the last prevents an independent deployment from reusing another
stack's tenant-wide application identity. Keep all three unchanged on redeployments. Guest
administrators should include their actual sign-in email in `-Admins`.

```powershell
./infra/deploy.ps1 -Location centralindia -AiLocation eastus2 `
  -ResourceGroup rg-cloudlens-new -NamePrefix clnew -EntraAppUniqueName cloudlens-new `
  -VnetAddressPrefix 10.80.0.0/16 -AppSubnetAddressPrefix 10.80.1.0/24 `
  -PostgresSubnetAddressPrefix 10.80.2.0/24 -PrivateEndpointSubnetAddressPrefix 10.80.3.0/24 `
  -WhatIf
```

These ranges are examples, not globally safe defaults. Preflight checks visible VNets in the
selected subscription; you must also check hub, on-premises and other-subscription ranges.
The script validates containment and non-overlapping subnets before making Azure calls.
**Never change a live stack's address plan as an incidental redeployment.**

## What gets created

| Resource | Name | Purpose |
|---|---|---|
| User-assigned identity | `id-cloudlens` | the app's only credential |
| Log Analytics workspace | `log-cloudlens` | logs |
| Application Insights | `appi-cloudlens` | traces and metrics, local auth disabled |
| Storage account | `st<20-random-hex-characters>` | deployment-owned cost exports and Parquet archive |
| PostgreSQL Flexible Server | `cloudlens-pg-<suffix>` | the cost warehouse |
| App Service plan | `plan-cloudlens` | Linux B2 |
| Web app | `cloudlens-<suffix>` | Python 3.13, uvicorn |
| AI Foundry account + project | `aif-cloudlens-<suffix>` | the chat agent, which the app will not start without |
| Model deployment | `gpt-4.1-mini` | the model the agent runs on |
| Virtual network | `vnet-cloudlens` | isolated shape only |

The five-character suffix on the default web app and PostgreSQL names is derived from the
resource group ID. Export storage uses its separate, persisted random name instead.

## Capacity for large estates

New deployments default to the measured reference profile for multi-million-row estates:
Linux App Service B2, PostgreSQL `Standard_E2ds_v5`, 128 GB `PremiumV2_LRS` storage with
3,000 IOPS and 125 MB/s throughput, and monthly partitioning for new cost tables. This is a
capacity baseline, not a latency guarantee; customer data shape, region, concurrency and
retention still need representative load testing. The profile costs materially more than the
former B1/B1ms/32 GB demo defaults. Check regional availability and pricing before deployment;
use `-PostgresSku`, `-PostgresStorageGb`, `-PostgresStorageType`, `-PostgresIops`,
`-PostgresThroughput`, `-AppServicePlanSku` and `-PartitionCosts:$false` only when the smaller
workload and its performance trade-offs are understood. Reader queries default to a 25-second
server-side timeout, below the browser's 30-second request deadline; override
`-PgStatementTimeout` only after measuring a legitimate slow query. Existing `costs` tables are
never converted to partitions by a redeployment.

Generation-triggered dashboard snapshot warming is enabled by default. It warms at most three
recent private views per active browser after a new data generation, and can be disabled with
`-SnapshotWarmingEnabled:$false` where its bounded background refresh is not desired.

## The two network shapes

`-NetworkIsolation $true` (default) is the shape to deploy at a customer:

- PostgreSQL is injected into `snet-postgres` and has **no public endpoint**
- storage has `publicNetworkAccess` disabled, reached through a private endpoint and zone
- the web app routes all outbound traffic through `snet-app`

`-NetworkIsolation $false` is for development: public endpoints, the PostgreSQL firewall
opened to Azure services and the running machine's IP, no virtual network. Some tenants
forbid public storage endpoints by policy, in which case only the isolated shape deploys.

## The AI account lives in its own region

`-AiLocation` defaults to `eastus2` and is deliberately not `-Location`. App Service quota
and pay-as-you-go model availability are both regional and rarely overlap — `centralindia`
lists `gpt-4.1-mini` but offers it only as `GlobalProvisionedManaged`, which is reserved
capacity bought by the month.

The chat agent is not optional; the app refuses to start without `PROJECT_ENDPOINT`, so a
region that cannot host the model is a blocker. `preflight.ps1` checks this first.

```powershell
./infra/deploy.ps1 -Location centralindia -AiLocation swedencentral -ModelName gpt-4.1
```

The web app authenticates with its managed identity — local auth is disabled, so there is no
key. The template grants `Cognitive Services OpenAI User`; subscription Reader does **not**
cover the Foundry data plane, and without that role the app is healthy and every question
returns 401.

## Credentials

There are no stored passwords on the Azure data path.

- PostgreSQL has **password authentication disabled** — nothing to rotate or vault.
- Storage has **shared key access disabled** — no connection string; the managed identity
  presents an Entra token for every blob read.
- Application Insights has **local auth disabled**.

The person or pipeline running `deploy.ps1` becomes an Entra administrator of the PostgreSQL
server, so they can connect with their own account:

```powershell
$token = az account get-access-token --resource-type oss-rdbms --query accessToken -o tsv
psql "host=<server>.postgres.database.azure.com dbname=cloudlens user=<your-upn> sslmode=require"
```

The application's managed identity is also registered as an administrator, for bootstrap.
That is what makes the isolated shape deployable without a jump host, since a non-admin
identity must be introduced with `pgaadauth_create_principal` from inside the VNet. **This is
elevated server access, not a least-privilege application role.** Run
[`sql/create-app-role.sql`](sql/create-app-role.sql) from a VNet-connected client and remove
the administrator entry before production; the application connects identically either way.

## Cost access

`deploy.ps1` grants the identity **Cost Management Reader** and **Reader** on the
subscription it deploys into. Name others explicitly, or take everything you can see:

```powershell
./infra/deploy.ps1 -Location eastus -CostScopeSubscriptions @('<sub-a>','<sub-b>')
./infra/deploy.ps1 -Location eastus -CostScopeAllVisible
```

The default covers only the hosting subscription. Explicitly selected subscriptions are
required: failed grants stop deployment after saving a handoff for all missing scopes.
`-CostScopeAllVisible` is an opt-in sweep of enabled subscriptions in the hosting tenant;
refused scopes remain reported as pending rather than failing optional coverage.

For a whole tenant, assign the same two roles once at management group scope.

### Automatic grants and administrator handoff

Both PowerShell deployment paths identify the App Service's active managed identity from
its attachment and `AZURE_CLIENT_ID`. They do not use the deploying person's object ID or
the Entra sign-in registration's client ID. Ambiguous or mismatched identities are refused.

By default the scripts grant **Reader** and **Cost Management Reader** and confirm the
assignments after writing. Add `-EnableExportManagement` only when the app should also
receive **Cost Management Contributor** for scheduled exports and budgets. This does not
grant storage access, change network controls, create schedules or grant subscription Owner.

Every attempt saves a non-secret JSON report under ignored `data\cost-access\<run>.json`,
or the supplied `-CostAccessReportPath`. The report identifies the runtime identity name,
type, resource ID, principal/object ID and client ID separately, and lists each subscription,
required/optional role, status, error and exact grant/verification commands. Share it only
with the administrator responsible for those scopes. A command being printed is not
evidence that its grant succeeded.

To inspect or repair an **already deployed** app without deploying infrastructure or code:

```powershell
# Read Azure configuration and write a local report; do not change permissions.
.\infra\grant-cost-access.ps1 -HostingSubscriptionId '<hosting-subscription-id>' `
  -ResourceGroup '<resource-group>' -AppName '<app-service-name>' `
  -CostScopeSubscriptions @('<cost-subscription-id>') -InstructionsOnly

# Run after an authorised administrator approves the selected role assignments.
.\infra\grant-cost-access.ps1 -HostingSubscriptionId '<hosting-subscription-id>' `
  -ResourceGroup '<resource-group>' -AppName '<app-service-name>' `
  -CostScopeSubscriptions @('<cost-subscription-id>') -EnableExportManagement
```

Omit `-EnableExportManagement` for cost-reading access only. The standalone command accepts
`-ReportPath` for a chosen report location; `-WhatIf` also performs only reads and produces a
local handoff. Existing grants are retained. `tools\deploy.ps1 -SkipRoleAssignment` produces
the handoff without cost-role writes and names the separate Foundry data-plane requirement.
These commands cannot bypass Azure RBAC, tenant boundaries or hosting bootstrap permissions.

### After an operator receives additional access

An Owner grant to the deploying person does **not** grant anything to CloudLens. Read the
App Service's attached managed identity and `AZURE_CLIENT_ID`, then check that identity's
role assignments on every intended cost subscription, including inherited/group grants.
Do not confuse it with the Entra sign-in registration or a retired staging identity.

On the reference rollout, the operator's new Owner grants enabled the missing app grants,
but both extra subscriptions remained invisible until Reader and Cost Management roles
were assigned to the active managed identity. Azure propagation and the app's access
caches must then refresh; a user sign-in alone may still reuse cached ARM inventory.
An approved runtime cache reset restored the three-subscription view and automatic Quick
backfill populated the missing scopes. No other deployment's storage permissions or
recurring schedules were needed. Quick data is still an estimate, not a completed FOCUS report.

## Scheduled exports

Creating an export with a system-assigned identity makes Azure grant that identity a role on
the destination *on your behalf*, so the **caller** needs
`Microsoft.Authorization/roleAssignments/write` (or Owner) on the storage account. Azure
reports refusal as `401 RBACAccessDenied`, which reads like an authentication problem and is
not one. RBAC changes take about ten minutes to propagate — do not retest immediately.

**`deploy.ps1` provisions these automatically**, for every subscription it granted cost access
to, and reports which state it left behind in its final summary. Pass `-SkipFocusExports` to
defer it. The rest of this section explains why it is a deployment step rather than something
the application does for itself.

**Provision exports with `infra/focus-exports.ps1`, not from inside the app.** The requirement
above is the reason. An export can only carry a *system-assigned* identity — the API types the
field `SystemAssignedServiceIdentity`, whose values are `None` and `SystemAssigned`, so a
user-assigned identity that already holds the storage role cannot be reused. Azure therefore
mints a new identity per export and it must be granted `Storage Blob Data Contributor` before
it can write. CloudLens's runtime identity is deliberately denied the ability to grant roles
(see *Cost access* below: "Never grant the runtime app subscription Owner or User Access
Administrator"), because an application that can hand out roles can widen its own access. The
in-app button therefore fails by design on a correctly locked-down deployment, and widening the
runtime identity to make it succeed trades a real security property for a convenience.

```powershell
# Rehearse first; this changes nothing.
./infra/focus-exports.ps1 -ResourceGroup cloud-lens-cx -WebAppName <app> -WhatIf

# Provision, and ask Azure for the first report immediately.
./infra/focus-exports.ps1 -ResourceGroup cloud-lens-cx -WebAppName <app> -RunNow
```

Run it by hand when a deployment used `-SkipFocusExports`, when the operator who deployed
lacked the rights at the time, or when a new subscription is added to an existing deployment.

It reads `ARCHIVE_ACCOUNT_ID`, `FOCUS_EXPORT_CONTAINER`, `FOCUS_EXPORT_PREFIX` and
`ARCHIVE_LOCATION` from the app, derives the same export names CloudLens derives (a hash of the
destination — `tests/test_focus_export_provisioning.py` pins the two derivations together), and
is safe to re-run: an export that already matches is left alone and an existing role assignment
is not duplicated. Run it as an operator holding Cost Management Contributor on each
subscription and Owner or User Access Administrator on the storage account.

Until an export exists and Azure has produced its first report, the warehouse holds only Quick
refresh data. That is not a failure, but it omits `UnitOfMeasure`, `PricingQuantity`,
`MeterName` and `Tags` — so **Hourly Billing, Applications & Tags and scheduling savings are
empty**, and say so rather than reporting zero.

The stack adds a private `focus-exports` container to the existing storage account, alongside
the unchanged `cloudlens` snapshot container. Settings: `ARCHIVE_ACCOUNT_ID`,
`FOCUS_EXPORT_CONTAINER=focus-exports`, `FOCUS_EXPORT_PREFIX=focus`. Another existing
container or prefix may be configured; create it through approved IaC first.

New schedules are **FOCUS only**, with Daily/Weekly `MonthToDate` or Monthly `TheLastMonth`
(the previous calendar month, not guaranteed final invoice data). Azure's managed export
identity writes separate `CreateNewReport` run folders and **no lifecycle deletion policy is
added — every run consumes storage**, so agree retention and budget alerts before production.
Legacy schedules and snapshot download paths are not migrated.

Manifest ingestion fails closed on incomplete runs or safety caps rather than summing
retained copies: `EXPORT_MAX_BLOBS=10000`, `EXPORT_MAX_MANIFESTS=2000`, `EXPORT_MAX_FILES=60`.
Raise these only after reviewing app memory and storage growth. A completed refresh does not
guarantee that all historical months or billing fields are available.

## Building a package without Azure access

```powershell
./tools/package.ps1 -Destination ./cloudlens-app.zip
```

Needs PowerShell and Python 3.11+ (`-Python <executable>` selects an interpreter); Git is not
required. Both deployment scripts use this same packager.

It includes only Python source under `app`, approved assets under `web`, and
`requirements.txt`. Untracked runtime modules are included; `.env`, `.localdata`, auth state,
test data, reports, caches, tests and `pyproject.toml` are excluded. Python syntax, local
imports, JavaScript imports, HTML assets and CSS asset URLs are checked with Linux case
sensitivity before writing, without importing the app or touching a database. Linked source
paths are refused. The result carries a per-file SHA-256 manifest, prints the ZIP's SHA-256,
and never overwrites an existing destination.

`tools/deploy.ps1` is the existing-infrastructure alternative, not a second provisioner. It
preserves an existing `AUTH_DATA_DIR` and signing key, and stops rather than regenerating a
signing key it cannot read.

## Before hand-off

### Staged rollout when matching an existing deployment

Use separate provisioning, identity, publication and acceptance gates when a customer must
retain an existing sign-in policy. The customer `deploy.ps1` path now requires
preapproved delegated consent and never upgrades or grants consent automatically.
An existing app-identity installation must explicitly retain its approved mode.
Direct Bicep callers must explicitly pass `authDelegatedArm=false` to retain that
legacy mode; deployment templates now default to delegated. Bare ARM/Bicep does
not perform the installer's Graph consent check: run the verified installer
preflight and preserve its evidence before an advanced direct-template deployment.

1. Establish the final subscription, tenant, resource group, names, regions and address
   ranges before creating the managed identity. Record its principal ID, not just its name.
   On the 13 September 2026 rollout, Azure rejected moving a user-assigned identity to a
   different resource group with `ResourceMoveNotSupported`; a replacement required new
   RBAC grants and a corrected Owner handoff.
2. Inspect the source registration and enterprise application: tenant/audience, public-client
   redirects, credential presence, assignment requirement, assigned users/groups, actual
   OAuth consent grants, application admin list and local-login setting. Requested API
   permissions are not evidence of consent. Six assigned users with assignment requirement
   disabled is not a six-user allowlist. Reuse a registration only within the same approved
   trust boundary; provision a customer-owned registration for an independent customer.
3. Compare managed-identity grants across every agreed cost subscription and export-storage
   scope. A deployer with Contributor can create resources but cannot grant the new
   identity access. Prepare Owner commands and stop, or obtain explicit acceptance of the
   missing scopes before continuing. Never enable local login, broaden the tenant audience,
   grant tenant-wide consent, or reuse another app's credentials to mask a permissions gap.
4. Run preflight, ARM validation and What-If against explicit subscription IDs. Check actual
   model/SKU quota independently of App Service and PostgreSQL SKU availability. Keep the
   AI account in the final resource group even when its region differs. Inspect all affected
   scopes; a What-If resource marked unsupported still needs post-deployment confirmation.
5. Provision `main.bicep` with `deployEntraApp=false` and the agreed `authDelegatedArm` value.
   The resource group can contain the prepared identity if its name matches the template.
   Configure Entra before publishing code: append the new HTTPS `/auth/callback` to the
   **public-client** redirect list without replacing existing redirects. Keep the audience,
   assignment policy, consent grants and existing administrator list unchanged.
6. Set `AUTH_CLIENT_ID`, `AUTH_TENANT_ID`, `AUTH_REDIRECT_URI`, `AUTH_ADMINS`,
   `AUTH_ALLOW_LOCAL=false`, `AUTH_DISABLED=false`, `AUTH_COOKIE_SECURE=true`,
   `AUTH_DATA_DIR=/home/cloudlens` and the agreed `AUTH_DELEGATED_ARM` value. Preserve the
   source's ingestion policy explicitly (`AUTO_INGEST=false` for the reference rollout).
   Do not copy its database, session-signing key, user files, token caches or storage contents.
7. Compare the actual deployed source with the chosen release, not a remembered Git SHA,
   a successful Kudu history entry, or stale settings. Record intentional differences.
   Package with `tools/package.ps1`; retain the ZIP hash and per-file manifest. Its explicit
   forward-slash ZIP entry names avoid the Windows PowerShell 5.1 archive-path failure on
   Linux. Deploy the source ZIP, not another installation's Oryx virtual environment.
8. Finish all app-setting and plan changes before publication. Wait for SCM to be responsive
   and avoid management changes while Oryx is building. A timeout or HTTP 502 is an unknown
   result, not permission to start another deployment: inspect the exact deployment ID,
   status and logs. `/readyz` alone may belong to an old release; also compare served assets
   and the deployed release manifest with the intended package.
9. Confirm real Microsoft sign-in, scoped subscription visibility, private PostgreSQL/storage
   connectivity and a model response. Record initial data loading separately: an empty
   database can be ready. Missing external Owner grants, second-user access tests, production
   sizing, retention and bootstrap-admin demotion remain explicit acceptance items.

For a staged deployment, use a reviewed ARM parameter file containing no secrets:

```powershell
$subscription = '<customer-subscription-id>'
$parameters = '@<absolute-path-to-reviewed-parameters.json>'
az deployment sub validate --subscription $subscription --location centralindia `
  --template-file .\infra\main.bicep --parameters $parameters
az deployment sub what-if --subscription $subscription --location centralindia `
  --template-file .\infra\main.bicep --parameters $parameters
az deployment sub create --subscription $subscription --location centralindia `
  --name '<release-id>' --template-file .\infra\main.bicep --parameters $parameters
# Apply the approved Entra configuration before publishing; do not invoke admin consent.
.\tools\package.ps1 -Destination '<new-absolute-path-to-release.zip>'
az webapp deploy --subscription $subscription --resource-group '<customer-rg>' `
  --name '<customer-web-app>' --type zip --src-path '<release.zip>'
```

The parameter file and ARM history describe infrastructure, not subsequent app-setting
overrides. Retain a separate non-secret record of approved sign-in settings, role scopes,
code-deployment ID and ZIP hash. Reapply the approved settings after an infrastructure
redeployment: the template owns the app-setting collection and does not contain
`AUTH_CLIENT_ID`, `AUTH_TENANT_ID`, `AUTH_REDIRECT_URI` or `AUTH_ADMINS`.

Normal interactive `az login --tenant <tenant-id> --scope https://graph.microsoft.com/.default`
resolved a stale Graph authentication failure during the reference rollout. If tenant
Conditional Access still refuses sign-in, stop for the tenant administrator; do not disable
the policy. Use an explicit tenant when requesting Graph tokens, and never log the tokens.

### Customer acceptance

1. Confirm the deployer's resource and role-assignment rights, the Foundry model/SKU quota in
   `AiLocation`, and permitted outbound destinations.
2. Agree the sign-in registration and the access mode above.
3. Set administrator sign-in addresses with `-Admins`. Required for service-principal or
   pipeline deployments — a pipeline's display name is not a human administrator account.
4. Resolve the PostgreSQL bootstrap administrator (see **Credentials**).
5. Test sign-in with two users who have different subscription access. Check Overview,
   Billing, Tags, exports, archive comparison and chat against each user's scope. Confirm
   that missing pricing shows as unavailable, not as fabricated savings.
6. Verify private PostgreSQL and storage connectivity, a complete first ingest, a
   warehouse-only report download, and budget creation only by an authorized user. Compare a
   completed billing period against the customer's authoritative Azure report.
7. Agree backup/restore, retention, support ownership and sizing. The default B1 plan and
   non-HA burstable database are an entry-level deployment, not a production SLA.

Preflight, local tests and template compilation do not establish customer quota, tenant
policy, delegated consent, private DNS connectivity or live billing access.

## Health and failure behaviour

Errors in role grants, app settings, code publication and health polling stop the script
rather than ending with a successful hand-off message. `-SkipCode` reports infrastructure-only
completion explicitly.

`/healthz` is a cheap liveness signal. `/readyz` returns 200 only after startup and a
successful PostgreSQL schema read probe, otherwise 503; probes use fresh read-only
connections, select no customer rows, migrate nothing, and share one in-flight check with a
five-second deadline and result cache. An empty initialised warehouse is ready. Readiness
does not establish data freshness, model inference, Azure permissions or a successful ingest.

Neither script stops the site before publication, nor treats an older successful deployment as
proof the new ZIP succeeded. **A CLI timeout is an unknown deployment state** — inspect that
deployment's logs before retrying. ZIP publication is not an atomic rollback strategy; plan
deployment slots or a tested recovery procedure for production.

## When a deploy leaves the app down

App Service restarts a container that fails its startup probe. The Oryx dependency build runs
on the same instance, so each restart kills it: the build never finishes, no virtual
environment is produced, the container fails to start, and the loop repeats. The deployment
reports success, the site returns 503, and the container log says:

```
Could not find build manifest file at '/home/site/wwwroot/oryx-manifest.toml'
WARNING: Could not find virtual environment directory /home/site/wwwroot/antenv
/opt/python/3.13.14/bin/python: No module named uvicorn
```

For a new site or an agreed maintenance window, publish with the site stopped. Do not
rerun the infrastructure/configuration phase while the dependency build is running:

```powershell
az webapp stop --subscription '<subscription-id>' -g '<rg>' -n '<web-app>'
az webapp deploy --subscription '<subscription-id>' -g '<rg>' -n '<web-app>' `
  --type zip --src-path '<release.zip>' --async true --restart false `
  --track-status false --enable-kudu-warmup false
# Wait for this exact Kudu deployment to reach status=4 and complete=true.
# Status=0/1/2 is still in progress; status=3 is failure.
az webapp start --subscription '<subscription-id>' -g '<rg>' -n '<web-app>'
```

The start command is a separate step, not an immediate follow-on to an asynchronous upload.
Read `az webapp log deployment show -g <rg> -n <web-app>` and the exact
`https://<web-app>.scm.azurewebsites.net/api/deployments/<deployment-id>` status. A healthy
build ends in "Deployment successful"; stalled log output alone is not proof of failure.
On the reference B1 rollout, the first Python SDK extraction took 81 seconds before pip
installation even began. Initial SCM warm-up also timed out, while the later publish was
accepted and visible through the ARM deployment history.

If a rebuilt archive is complete but the running app still serves an old version, use a
controlled stop/start of the affected app and compare the served bytes again. This is a
recovery step, not the default publication strategy or a substitute for deployment slots.

### First-deployment recovery observed on 13 September 2026

Stopping the empty app did not by itself complete the reference deployment. SCM restarted,
the original build process disappeared, and its deployment record remained at `status=1`.
The record was stale, not a running build. Recovery used a temporary B2 plan and a small
standard-library maintenance responder outside `wwwroot`, started with
`python /home/site/maintenance.py`. It returned 200 only for `/healthz` and 503 for other
requests; it did not serve files, expose data, or report application readiness.

An initial inline `python -c '...'` maintenance command failed because the App Service
image's startup script wrapped it in another single-quoted Oryx command. Use a script-file
path rather than nested quoting. Keep the maintenance responder active throughout the build;
restore the ordinary Uvicorn command only after the exact release finishes. Restore the
agreed plan SKU after the application works and repeat the live checks at that size.
Temporary scaling was a recovery measure, not proof that memory caused the interruption.

The retry initially returned HTTP 409: a persistent deployment-directory lock named a
retired worker. Recovery first confirmed the current App Service instance list, lock owner,
and absence of build processes, then archived that specific orphaned lock directory and
submitted one new asynchronous ZIP deployment. **Do not delete active locks or automate this
as a generic retry.** Preserve the failed deployment ID and lock evidence; if ownership is
uncertain, stop for Azure support. Do not mark an interrupted record as successful.

The retry used authenticated `POST /api/zipdeploy?isAsync=true` on the app's SCM host,
retained the 202 response and new deployment ID, and monitored that ID. An accepted upload
still requires build completion, real app startup, source-manifest matching and live sign-in.

The completed reference rollout placed all resources in `cloud-lens-cx`, with the application
and data services in Central India and the AI account in East US 2. The successful build took
about ten minutes. Its deployed manifest matched the source ZIP and all 102 source files;
58 installed distribution versions were captured in a separate resolved-dependency snapshot.
The source requirements use lower bounds, so archive that snapshot with the ZIP and review
it as constraints for subsequent releases rather than assuming a later build resolves the
same versions. A version snapshot alone is not a wheel-hash lock or a production approval.

After restoring B1, the reference app completed real Entra sign-in in app-identity mode,
read the private PostgreSQL schema, reached its private archive container and received a
`gpt-4.1` response. The maintenance and verification helpers were removed from the server.
The existing application was not redeployed. The new warehouse remained empty with
automatic ingestion disabled; two additional subscription scopes still needed their Owners'
grants. Those gaps remain customer acceptance items, not successful same-access coverage.

The later storage-isolation code-only update built successfully on the existing B1 plan
without maintenance mode, scaling or infrastructure changes. The new archive and all source
hashes were correct, but the running OpenAPI export-selection contract was still old. A
target-only stop/start activated the release. After cold start settled, the new contract
and an unselected FOCUS refresh confirmed own-storage isolation; 4,927 existing warehouse
rows were unchanged. This is why handoff must establish the running release, not just a
green build or the first successful readiness response. The dedicated report schedule
still required a separate administrator-approved setup.

## Terraform environments

**Status: infrastructure wrapper, not an end-to-end customer installer.** Do not treat
`terraform apply` as equivalent to `infra/deploy.ps1`. The wrapper does not publish code,
configure the app's Entra sign-in settings or grant cost-subscription roles. The
`cost_access_handoff` output describes the runtime identity and roles; it does not grant them.
Bicep plus the staged PowerShell workflow remains the reference deployment path.

The stack is defined in Bicep, and [`terraform/main.tf`](terraform/main.tf) deploys the *same*
compiled template through `azurerm_subscription_template_deployment`, so a platform team keeps
its state, pipeline and `terraform plan`:

```powershell
az bicep build --file .\infra\main.bicep --outfile .\infra\main.json
cd infra/terraform && terraform init && terraform apply
```

Set `storage_account_name` in the customer's `.tfvars` before planning: an actual existing
name for an upgrade, or `null` for a new deployment. Do not switch a live stack to `null`.
The AzureRM and Random provider versions/checksums are recorded in `.terraform.lock.hcl`.
The PowerShell naming-state file and Terraform state are different mechanisms; do not
assume a PowerShell preview reserves the same name as a separate fresh Terraform rollout.

A hand-written second Terraform module is deliberately not shipped: two definitions drift.

### Configuration audit: 13 September 2026

Compilation, Azure template validation and Terraform's mocked tests passed, and the
compiled ARM file matched the current Bicep. Those results do not establish configuration
parity or a safe unattended upgrade:

| Finding | Customer deployment consequence |
|---|---|
| Terraform omits 13 Bicep parameters, including `nameSuffix`, `authDelegatedArm`, AI model/region/capacity, compute sizing and Entra-registration options. | Its inputs cannot reproduce the reference stack's names, `gpt-4.1` configuration and app-identity sign-in mode. Do not import the reference stack into this wrapper as-is. |
| The Terraform subscription-level deployment name is fixed at `cloudlens`. | Separate states in the same subscription would manage the same deployment record, even with different resource groups. A reviewed per-stack deployment identity is required. |
| No remote state backend is supplied, and CIDR validation checks syntax rather than subnet containment/overlap. | A customer must configure protected, locked state and run the address-plan preflight; a valid Terraform plan is not enough. |
| The bare Bicep app-setting collection does not contain the live `AUTH_CLIENT_ID`, `AUTH_TENANT_ID`, `AUTH_REDIRECT_URI`, `AUTH_ADMINS`, `AUTH_ALLOW_LOCAL`, `AUTO_INGEST` or `PGSCHEMA`. | Preserve and reapply these settings during a reviewed upgrade. Losing `PGSCHEMA` can point the app at a different schema and make existing data appear missing. Do not blindly reapply the template to the reference app. |
| Live What-If predicts removal of a private-endpoint subnet NSG association and a PostgreSQL-subnet service endpoint. | Tenant-added network configuration needs review/preservation before an infrastructure update. Other service-generated What-If differences may be noise; do not assume every reported change is harmless. |

No Azure changes were applied by this audit. Terraform remains unapproved as a complete
customer deployment strategy until the above gaps and the application configuration,
publication and access handoff are addressed.

## Known limitations

**Money is stored as `double precision`, not `numeric`.** At Azure's magnitudes the
arithmetic error stays far below a cent, but the same total summed in a different order can
differ in the last digit. `numeric` maps to `Decimal`, which is not JSON-serialisable, so the
move is a migration affecting every endpoint and chart rather than a column change.

**`costs` has no unique constraint.** FOCUS has no natural unique key — the same resource
legitimately produces several rows a day for different meters, charge types and pricing tiers
— so any key would be a guess, and a wrong guess silently discards real charges. Over-reporting
gets queried; under-reporting does not. Guarding it instead: canonicalisation at every write
(`canonical_sub_id`), a startup repair pass, and the **Diagnostics** page invariant
`subscription_identity`, which counts non-canonical subscription ids and should always read 0.

## Running costs

The previous B1/B1ms demo estimate below is retired; it does not price the large-estate
defaults. Check the Azure Pricing Calculator for the selected region before deployment:

| | |
|---|---|
| App Service B2 | regional price; billed while the plan is provisioned |
| PostgreSQL E2ds_v5 + 128 GB PremiumV2 (3,000 IOPS / 125 MB/s) | regional compute, storage, IOPS and throughput charges |
| AI Foundry | usage-driven |
| Storage, Log Analytics, App Insights | a few dollars, usage-driven |

The actual total depends on region, uptime, backups, data retention and telemetry volume.
Do not use the old B1/B1ms estimate to approve a customer deployment.
# Staged installation and administrator handoff

The runtime's legacy unset `AUTH_DELEGATED_ARM` behavior remains app-identity
for compatibility. The customer installer explicitly sets it to `true` by default
and requires preapproved ARM `user_impersonation` before provisioning. It does
not invoke tenant-wide admin consent. Never automatically switch modes.
An existing registration may retain previously requested permissions; this installer
does not silently remove them.

Reader/Contributor plus Cost Management Reader/Contributor on data subscriptions
does **not** confer role-assignment authority. Host provisioning includes bootstrap
RBAC and must be run by an authorised resource/RBAC administrator (or a suitably
scoped, approved pipeline). `-SkipPreflight` cannot bypass Azure permissions.

1. A hosting administrator deploys with the desired explicit
   `-CostScopeSubscriptions @('<ssamadda-id>','<raviverma-id>','<dechow-id>')`
   and `-DeferCostAccess`. This writes a cost-access report without attempting cost
   grants and defers export creation. It is not complete cost coverage.
2. A reader/contributor can generate an exact, read-only handoff for an existing
   host without redeployment:

   ```powershell
   .\infra\grant-cost-access.ps1 -HostingSubscriptionId '<host-sub-id>' `
     -ResourceGroup '<host-rg>' -AppName '<existing-app>' `
     -CostScopeSubscriptions @('<selected-sub-id>') -InstructionsOnly `
     -ReportPath '.\data\cost-access\handoff.json'
   ```

   The installer also supports `-InstructionsOnly -Location '<region>'` with the
   same host/scope parameters (`-CostAccessReportPath` instead of `-ReportPath`).
   No Azure changes occur in this mode. Share the report with each selected scope's
   authorised RBAC administrator. It identifies the verified runtime principal,
   not the sign-in registration, and supplies exact grant/verification commands.
3. Each administrator executes only their authorised scope's commands. Resume
   without host redeployment by repeating `grant-cost-access.ps1` with those exact
   parameters and `-InstructionsOnly` to verify. `pending`/`blocked` is not success;
   verify all selected scopes, including scopes owned by a different team. A
   non-instructions run attempts missing grants and fails on required pending scopes.
4. An authorised export/storage administrator separately runs
   `focus-exports.ps1 -HostingSubscriptionId '<host-sub-id>' -ResourceGroup '<host-rg>'`
   `-WebAppName '<existing-app>' -Subscriptions @('<selected-sub-id>') -RunNow`
   if exports need provisioning. Existing FOCUS exports are not proof of readable
   blobs, ingestion, or dashboard visibility.

Reports verify role assignments, not effective data access or ingestion. Validate
sign-in, the server-side per-user RBAC intersection, export delivery/read access,
ingestion and user-visible data separately. Selected scopes are coverage targets,
never authorization whitelists. Missing rights on dechow must be granted by its
authorised administrators; ssamadda/raviverma grants do not extend there.
