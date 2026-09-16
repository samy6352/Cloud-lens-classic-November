# CloudLens

**Understand your Azure spending. Find savings. Ask questions in plain English.**

CloudLens brings your Azure costs, resources and savings opportunities into one dashboard.
You can explore spending, compare periods, download reports and ask the AI analyst for help.

CloudLens runs in **your Azure subscription**. Installing it creates Azure services that
incur charges, including a web app, database, private storage and an AI service.

[Full documentation](Cloudlens-Documentation.Md) |
[Detailed deployment guide](infra/README.md)

## Before you start

No programming is required, but Azure permissions and service availability matter.
An IT administrator may need to help with the initial approval.

| You need | What to do |
|---|---|
| An Azure subscription | Use the subscription approved by your organisation for CloudLens. |
| Permission to install | Ask your Azure administrator to confirm that you can create resources and assign access. Typically this requires **Owner**, or **Contributor plus User Access Administrator**. |
| Microsoft sign-in approval | Ask your Microsoft Entra administrator to approve the sign-in setup. Being an Azure subscription Owner does not automatically provide this permission. |
| Approved regions and budget | Confirm where the app, data and AI service may run, and approve the ongoing Azure charges. |
| Access to this GitHub repository | If the repository is private, its owner must give you access before you can download it. |

The steps below are for a **new installation from a Windows computer**, using the
recommended Bicep/PowerShell installer. Do not use them to replace an existing deployment.
For upgrades or an existing sign-in registration, use the [deployment guide](infra/README.md).

## Install CloudLens

### 1. Install three tools

Install these from their official websites. Git and Docker are not required.

- [PowerShell 7](https://learn.microsoft.com/powershell/scripting/install/installing-powershell-on-windows)
- [Azure CLI](https://learn.microsoft.com/cli/azure/install-azure-cli-windows)
- [Python 3.11 or newer](https://www.python.org/downloads/windows/) -- enable **Add Python to PATH** if the installer offers it.

Close and reopen your terminal after installation. Use **PowerShell 7**, not Command Prompt
or the older Windows PowerShell.

### 2. Download the files from GitHub

1. On this repository's GitHub page, select **Code**, then **Download ZIP**.
2. Right-click the downloaded ZIP and choose **Extract All**.
3. Open the extracted folder containing this `README.md` and the `infra` folder.
4. Open a PowerShell 7 terminal in that folder.

Paste this into the terminal:

```powershell
$PSVersionTable.PSVersion.Major
az version
python --version
Test-Path .\infra\deploy.ps1
```

The first result should be **7 or higher**, Python should be **3.11 or newer**,
and the last result should be **True**.
If a tool is "not recognised", reopen the terminal or complete its installation before continuing.
If the last result is False, open the folder that contains `infra` and try again.

### 3. Sign in and choose the subscription

Run:

```powershell
az login
if ($LASTEXITCODE -ne 0) { throw "Sign-in did not finish. Stop here and resolve the sign-in error." }
az account list --query "[?state=='Enabled'].{Subscription:name,ID:id}" --output table
$subscriptionId = Read-Host "Paste the ID of the subscription where CloudLens should be installed"
az account set --subscription $subscriptionId
if ($LASTEXITCODE -ne 0) { throw "The subscription could not be selected. Do not continue." }
az account show --query "{Subscription:name,ID:id}" --output table
```

Complete the browser sign-in. Check the subscription name in the final table carefully:
this is where CloudLens will create resources and generate charges.

### 4. Choose a new installation name and check readiness

A **resource group** is the Azure folder that will contain this installation.
Choose a new name using letters, numbers and hyphens, for example `cloudlens-myteam`.

The example below places the app and database in **Central India**, and the AI service
in **East US 2**. Use these only if approved by your organisation. Ask IT for different
Azure region codes if necessary, then change the two region values before running.

```powershell
$resourceGroup = Read-Host "Enter a NEW resource group name, for example cloudlens-myteam"
$appRegion = "centralindia"
$aiRegion = "eastus2"

$exists = az group exists --name $resourceGroup --subscription $subscriptionId --output tsv
if ($LASTEXITCODE -ne 0) { throw "Could not check the resource group. Stop and ask your administrator." }
if ($exists -ne "false") { throw "That resource group already exists. Choose a NEW name for this guide." }

.\infra\preflight.ps1 -Location $appRegion -AiLocation $aiRegion -ResourceGroup $resourceGroup
```

This checks access, service availability and network settings without creating Azure resources.
**Do not continue if it reports a BLOCK or an error.** Resolve approval, capacity and network
warnings with your administrator first. A successful check does not guarantee that Azure will
have capacity when the installation starts.

### 5. Preview, then install

Keep the same terminal open so the values entered above remain available.

First, preview the resources:

```powershell
.\infra\deploy.ps1 -Location $appRegion -AiLocation $aiRegion `
  -ResourceGroup $resourceGroup -EntraAppUniqueName $resourceGroup -WhatIf
```

Check that the preview names your new resource group. The preview saves a local storage-name
file but does not create Azure resources. Keep that file and the downloaded folder.

When the preview and required approvals are satisfactory, install:

```powershell
.\infra\deploy.ps1 -Location $appRegion -AiLocation $aiRegion `
  -ResourceGroup $resourceGroup -EntraAppUniqueName $resourceGroup
```

Leave the terminal open. Installation can take **20-40 minutes or longer**, depending on
Azure capacity and download speed. Avoid starting a second installation while one is running.

The installer creates dedicated storage with a generated name, sets up the application and
attempts to grant its managed identity cost-reading access. **Access belongs to the app's
managed identity, not just to the person installing it.** If permissions are missing, follow
the administrator handoff; do not grant the application subscription Owner.

If the command stops with an error, do not assume installation finished. Keep the message
and use the help below before retrying.

### 6. Open CloudLens and load your first data

After installation reports completion without unresolved errors, open the
`https://...azurewebsites.net` address printed by the installer and sign in with your
Microsoft account.

1. Choose the subscriptions you want to analyse.
2. Open **Reports > Cost data**, choose subscriptions to ingest in its shared picker, then use
   **First-time import**. Keep the recommended **90-day history** or choose 1–365 days.
   **Scheduled updates** holds the automatic-frequency draft (Daily recommended).
   Select **Review import**.
3. Resolve any listed permission blockers with your deployment administrator, then select
   **Start import and updates** once. The review explicitly authorizes history and the chosen
   automatic frequency together. The same background job loads history and enables future updates;
   you do not need to configure separate exports or verification runs.

A blank dashboard immediately after installation does not mean installation failed:
the new database starts empty. Azure may take time to prepare the reports; follow progress
in Cost data while the dashboard retains any existing results. **Quick refresh** remains an
optional estimate-only fallback, not a required setup step or proof of FOCUS completeness.
Retained export files incur storage charges.

### Cost-data setup and automatic updates

**Reports > Cost data** is the single setup flow, also opened by first-run setup and
**Refresh latest**. Its three local tabs are **First-time import**, **Scheduled updates**, and
**Access & fixes**. Above them, a shared searchable picker shows all authenticated readable
subscriptions, initially selecting the header scope (or all accessible subscriptions).
The local ingest selection is independent of dashboard filters; clearing it disables actions
and never means “all.” Reloading the catalog rechecks entitlements.

To enable ongoing updates without importing history again, open **Scheduled updates**,
choose the visible **Daily**, **Weekly**, or **Monthly** option, then select
**Review weekly schedule** (or the chosen frequency). The confirmation appears immediately
below the options on the same tab; select **Enable weekly updates** to approve it.
This schedule-only action preserves existing history and catch-up progress and creates no
historical import tasks. New installations can enable a schedule independently, then use
First-time import if they need older data. Normal scheduled windows still reconcile corrections.

Previous failures are retained under **Subscription details and previous errors**, not inferred
to mean a particular missing role. A fresh review is allowed after a failed attempt and reports
the current blockers inline. **Recheck readiness** repeats the read-only review after repair;
**Access & fixes** remains available without replacing the schedule action. Read-only users,
empty selections, and active jobs cannot submit changes. An unfinished automatic-update job
must be retried before replacing its schedule, so its catch-up position is not lost.

First-time import defaults to 90 inclusive UTC days, with 30/90/180/365-day presets and custom
1–365-day or exact-date choices. **1 year means 365 days**, including across leap years.
Existing data is not deleted: this is an import horizon, not a purge or rolling-retention policy.
The service rejects invalid, future, conflicting or oversized first-import windows before
Azure calls or job creation, including legacy date-based API requests.
The read-only `/api/cost-data/subscriptions` catalog never schedules legacy backfill.
`POST /api/cost-data/preview` accepts optional integer `history_days` for initial setup,
or existing `start`/`end` dates; when both are supplied they must agree. The preview and
idempotency contract bind the canonical dates and length. Explicit empty/null selections
are rejected; omitted scope remains compatible with legacy all-permitted callers.
Frequency-only requests omit history and do not reload it. Scoped job retries refuse to
resume a multi-subscription job unless its entire scope is selected.

**Review import** is read-only; **Start import and updates** is the one approval for
owned-export provisioning (if missing), history import and future checks. Existing reports
are reused before requesting missing month-bounded windows. Quick rows never count toward
FOCUS coverage and missing days are not assumed zero. Initial rendering reads database status;
export inventory and detailed permission checks are deferred until requested.

Jobs retain immutable UUIDs, complete manifests and per-window results in PostgreSQL.
Azure generation can take hours. **Retry missing work** preserves completed windows.
New setup jobs use explicitly authorized persistent workload identity, survive browser closure
and resume after backend restart. Interactive sign-in alone is not sufficient for unattended
setup; the review explains that deployment blocker. No delegated token is stored in a job.
The backend and database must remain running for scheduled checks. Permission errors identify
the principal and scope; the app never grants Owner or User Access Administrator.

The app is the single cadence authority after approval; owned native recurrence is paused:

- **Daily:** 02:00 UTC each day, rereading the most recent seven inclusive dates.
- **Weekly:** Mondays at 02:00 UTC, rereading fourteen dates, including across month boundaries.
  Azure's 2025-03-01 API supports native Weekly recurrence; this approved pipeline uses
  app-owned execution/import timing instead so overlap and missed-run catch-up are explicit.
- **Monthly:** first of the month at 02:00 UTC, the previous completed calendar month plus
  the month before it for corrections. This is billing-review cadence, with up to a month lag.

Missed checks catch up from the saved successful watermark in bounded batches. Failed windows
never advance that watermark, and catch-up retains correction overlap around the previous
successful period, including a provisional final day. Changing frequency preserves the
initial history's catch-up anchor. Starting the month after setup, Daily and Weekly modes
also reconcile 90 days on the first scheduled check on or after the seventh, to catch older
adjustments without downloading 90 days on every update.
**Refresh now** reviews the saved cadence's exact coverage;
**Review schedule** uses the same approval flow. Returned manifests establish optimization
evidence internally—there are no customer-operated verification steps. Wider MTD reports
remain a compatibility mode, not changed-byte exports. If custom historical execution is
unsupported, history remains visibly partial rather than silently falling back to Quick.
Existing native Daily/Weekly and legacy compact configurations remain unchanged until
explicitly migrated. Native Daily/Weekly definitions use month-to-date coverage, not a
rolling day/week; generation frequency and coverage are separate.
CSV/gzip parsing, strict complete-run validation and transactional reconciliation are unchanged.
External exports, retention policies and existing good data are not modified.

API: `GET /api/cost-data` returns scoped saved state and coverage;
`POST /api/cost-data/preview` reviews `scope`, `action` (setup/schedule/frequency/refresh/reconcile),
`cadence`, `start` and `end`; `POST /api/cost-data/setup` requires that stable preview token
and an idempotency key. Existing FOCUS job status/resume APIs handle the durable work.
The `schedule` action accepts no history dates or length; it configures only ongoing updates.
The additive version-2 configuration uses the existing `focus-daily:<subscription>` key.

Cache integrations read the integer `jobs.read_generation()` and require
`jobs.generation_ready(version)` before querying and before publishing a result. The numeric
`meta.data_generation` counter advances only with committed source writes;
`meta.data_rollup_generation` catches up only after a coherent rollup rebuild. Failed rebuilds
retain the previous ready version and leave publication events pending for resumable recovery.
An absent ready-version key is not ready, including on the first startup of an existing database.
For a cache-owned transaction, `jobs.snapshot_generation(con, scope)` reads through the supplied
connection and returns `data:<version>` or `pending:<source>:<ready>`; the revision is global.

For an isolated restore/staging deployment, set `CLOUDLENS_READ_ONLY=true` (default: false).
Mutating HTTP methods return 403 except local login/logout and the three read-only FOCUS/cost-data
preview endpoints; chat and actual import/configuration changes remain blocked.
Startup, daily, FOCUS, onboarding, sign-in backfill and durable job recovery cannot
start ingest work, and the schedule client refuses Azure writes. Authenticated reads and local
cache/rollup/DDL work remain available. Use a separate database and staging-only credentials;
this flag does not replace those isolation controls.

### Cached views and access readiness

Private PostgreSQL snapshots keep compatible last-good dashboard responses across worker
restarts. Authorization, selected subscriptions, reporting period, filters, currency and
calculation versions remain part of the cache boundary. Expired snapshots stay visible with
their timestamp while the active view refreshes; partial source coverage is still partial,
not a clean bill of health. An explicit refresh waits for new results. New scopes or periods,
sign-in, incompatible versions and genuinely cold starts can still require work.

`SNAPSHOT_CACHE_ENABLED` defaults to `true`. The current source revision is deliberately
global: a committed cost change invalidates snapshot freshness, while the previous authorized
result remains available until a coherent rollup is published. Static assets use validated
content-versioned URLs; unversioned URLs revalidate.

In **Reports > Cost data > Access & fixes**, inspect caller
access or, for application admins, the background identity. The matrix distinguishes declared
permissions, denied access, unknown evidence and actions needing approval. Storage data roles
and network reachability are separate; subscription Owner does not establish reservation or
savings-plan lifecycle access. Diagnostics never grant roles or send notifications.

Deployments may set `CLOUDLENS_PRINCIPAL_ID` and `CLOUDLENS_TENANT_ID` to identify their
workload principal accurately. The installer supplies these; they are metadata, not role
assignments. Never grant the running application Owner or User Access Administrator merely
to clear a diagnostic warning.

## If something stops you

| Message or symptom | Next step |
|---|---|
| "Running scripts is disabled" or "not digitally signed" | Ask IT to approve the trusted GitHub download under your organisation's PowerShell policy. Do not disable an organisation policy to bypass the restriction. |
| "Need admin approval" | Ask your **Microsoft Entra administrator** to approve sign-in. Do not disable authentication. |
| "Authorization failed" or missing roles | Share the managed-identity object ID, subscription and requested roles with your Azure administrator. Your own Owner access does not grant the app access. |
| A subscription is missing | The app usually needs **Reader** and **Cost Management Reader** on that subscription. See the [access guide](infra/README.md#cost-access). |
| No export is available | Use Quick refresh first. Set up an export into CloudLens's own storage; do not open another deployment's private storage. |
| Region, quota or capacity error | Ask your administrator to approve an available region or increase the relevant quota. |
| Timeout, failed build, or the site does not open | Save the error and ask IT to follow the [deployment recovery guide](infra/README.md#when-a-deploy-leaves-the-app-down). A timed-out upload may still be running; do not repeatedly rerun it. |

## More information

- [Cloudlens-Documentation.Md](Cloudlens-Documentation.Md): complete product, architecture, configuration and developer documentation, preserved from the original README.
- [Deployment guide](infra/README.md): permissions, regions, private networking, exports, upgrades and troubleshooting.
- [Test guide](tests/README.md): for developers maintaining CloudLens.

Terraform is currently an advanced infrastructure wrapper, **not** the recommended complete
installer. Use the Bicep/PowerShell steps above for a new installation.
