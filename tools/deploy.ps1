<#
.SYNOPSIS
  Deploy CloudLens into an existing Azure subscription, without Bicep.

.DESCRIPTION
  NOT THE DEFAULT. `infra/deploy.ps1` is the supported path: it provisions the whole stack
  from nothing -- resource group, App Service, PostgreSQL, AI Foundry, managed identity and
  the Entra app registration for sign-in -- from one Bicep deployment.

  Reach for this script only when that does not fit: you already have the infrastructure --
  including a PostgreSQL server -- or you are not permitted to run a subscription-scope
  deployment. It creates no app registration, so sign-in has to be configured by hand
  afterwards, and it provisions no database: -PgHost and -PgUser must name a server and a
  role that already exist.

  Idempotent: safe to re-run. Creates the resource group, plan and web app if they are missing,
  grants the app's managed identity the read access it needs, points the warehouse at the
  PostgreSQL server you named, deploys the code, and verifies the result.

  Three things this script will not let you get wrong, because each one produces an app that
  looks deployed and is not:

    * One worker, one instance. Sessions and the header snapshot live in process memory, so a
      second worker sends a signed-in person to a process that has never heard of them.

    * AUTH_DATA_DIR outside wwwroot. A zip deploy replaces /home/site/wwwroot, so the account
      file at a default path is deleted by the next deploy and everyone is signed out.

    * The managed identity's own access is the ceiling. A subscription it has no Reader on is
      invisible to every user, however much access they personally hold.

  It also grants the identity a data-plane role on the Foundry account behind -ProjectEndpoint.
  Reader on the subscription does not cover that, so without it the dashboard loads and every
  question comes back 401.

.EXAMPLE
  ./tools/deploy.ps1 -ResourceGroup rg-cloudlens -AppName cloudlens-me -Location centralus `
    -PgHost my-server.postgres.database.azure.com -PgUser cloudlens-identity `
    -ProjectEndpoint https://my-foundry.services.ai.azure.com/api/projects/my-project

.EXAMPLE
  # With Entra sign-in, so people use their Azure credentials rather than a local password.
  ./tools/deploy.ps1 -ResourceGroup rg-cloudlens -AppName cloudlens-me `
    -PgHost my-server.postgres.database.azure.com -PgUser cloudlens-identity `
    -ProjectEndpoint https://... -EntraClientId <app-id> -EntraTenantId <tenant-id> `
    -Admins you@yourdomain.com
#>
[CmdletBinding()]
param(
  [Parameter(Mandatory)] [string] $ResourceGroup,
  [Parameter(Mandatory)] [string] $AppName,
  [string] $Location = "centralus",

  # F1 is free but has no Always On, so the app cold-starts and a long ingest can be killed
  # mid-run. B1 is about 13 USD a month and is what you want if you care about scheduled loads.
  [ValidateSet("F1", "B1", "B2", "S1", "P0v3")] [string] $Sku = "B1",

  # The PostgreSQL server that holds the warehouse. Required, because it is the only place
  # rows are stored -- this script provisions no database of its own, which is the whole
  # reason to reach for it rather than infra/deploy.ps1.
  #
  # The server must have Entra authentication enabled and `PgUser` must already exist on it
  # as a role mapped to this app's managed identity. Creating that role is a data-plane
  # operation on the server, so it is done there rather than from here.
  [Parameter(Mandatory)] [string] $PgHost,
  [string] $PgDatabase = "cloudlens",
  [Parameter(Mandatory)] [string] $PgUser,

  # Required: the app refuses to start without a Foundry project.
  [string] $ProjectEndpoint,
  [string] $ModelDeployment = "gpt-4.1",

  # Subscriptions the app may read. Defaults to the one you are currently signed in to.
  [string[]] $Subscriptions,

  [switch] $EnableExportManagement,
  [string] $CostAccessReportPath = '',

  # Entra sign-in. Leave these unset to use a local username and password instead.
  [string] $EntraClientId,
  [string] $EntraTenantId,
  [string] $EntraClientSecret,
  [string[]] $Admins,

  # Let anyone with an Azure account sign in, from any Entra directory, and see whatever their own
  # Azure RBAC allows -- across every tenant they belong to. Their account does not need to exist
  # in the tenant this app is registered in.
  #
  # This uses the 'common' authority, which admits personal Microsoft accounts as well as work
  # ones. That is not a loosening for its own sake: a Visual Studio or pay-as-you-go subscription
  # is very often owned by an MSA that is a guest in its own directory, and Entra issues such an
  # account a normal organisational ARM token. Excluding them can lock out the only person with
  # access to the subscription.
  #
  # The registration needs "Accounts in any organizational directory and personal Microsoft
  # accounts", and this implies -DelegatedArm: resolving access as the app only ever works inside
  # the app's own directory, so without a delegated token every external user would sign in and
  # see nothing.
  [switch] $MultiTenant,

  # Names a directory whose sign-in page is offered as a second option, for people whose Azure
  # access comes through a personal Microsoft account. Azure Resource Manager is an Entra-only
  # resource, so the multi-tenant endpoints refuse a personal account for an ARM scope -- but a
  # tenant authority federates them to login.live.com and issues an ordinary token. Without this
  # such a person is told to "use your work or school account" and has no way in, which matters
  # because those accounts commonly own exactly the subscriptions this app reports on.
  [string] $HomeTenant,

  # As -MultiTenant, but work and school accounts only. Use where personal accounts should not
  # be able to sign in even if they hold Azure access.
  [switch] $WorkAccountsOnly,

  # Use the signed-in person's own ARM token rather than resolving their role assignments as
  # the app. Stronger -- Azure refuses rather than the app filtering -- but it needs admin
  # consent for Azure Service Management user_impersonation. In your own tenant you are the
  # admin, so this is usually the right choice for a personal deployment.
  [switch] $DelegatedArm,

  [switch] $SkipRoleAssignment
)

$ErrorActionPreference = "Stop"
$repo = Split-Path $PSScriptRoot -Parent
. (Join-Path $repo 'infra/deployment-checks.ps1')
. (Join-Path $repo 'infra/cost-access.ps1')

function Step($msg) { Write-Host "`n=== $msg" -ForegroundColor Cyan }
function Ok($msg)   { Write-Host "    $msg" -ForegroundColor Green }
function Warn($msg) { Write-Host "    $msg" -ForegroundColor Yellow }

# az is a native command, so a failure sets $LASTEXITCODE and nothing else: ErrorActionPreference
# does not apply. Calling it bare gives a script that prints ERROR, keeps going, and reports a
# deployment that never happened. Every call goes through one of these two.
#
# The wrapper is named Az on purpose: PowerShell resolves names case-insensitively and prefers
# functions to executables, so a bare `az` anywhere below lands here and gets checked. That also
# means the wrapper must not say `az` itself, or it calls itself forever -- hence the resolved path.
$AzExe = (Get-Command az -CommandType Application -ErrorAction SilentlyContinue |
          Select-Object -First 1).Source
if (-not $AzExe) { throw "Azure CLI not found on PATH. Install it from https://aka.ms/azcli" }

function Az {
  $errFile = Join-Path $repo ".cloudlens-cli-$([guid]::NewGuid().ToString('N')).stderr"
  try {
    $out = & $AzExe @args 2>$errFile
    if ($LASTEXITCODE -ne 0) {
      $operation = ($args | Select-Object -First 3) -join ' '
      throw "Azure CLI operation '$operation' failed (exit $LASTEXITCODE):`n$(Get-Content $errFile -Raw)"
    }
    $out
  }
  finally { Remove-Item $errFile -Force -ErrorAction SilentlyContinue }
}

# For calls that are allowed to fail: a role that already exists, a resource being probed for.
function AzTry {
  $errFile = Join-Path $repo ".cloudlens-cli-$([guid]::NewGuid().ToString('N')).stderr"
  try {
    $out = & $AzExe @args 2>$errFile
    if ($LASTEXITCODE -eq 0) { $out }
  }
  finally { Remove-Item $errFile -Force -ErrorAction SilentlyContinue }
}

Push-Location $repo
try {
  Step "Checking prerequisites"
  $account = Az account show --only-show-errors | ConvertFrom-Json
  if (-not $account) { throw "Not signed in. Run 'az login' first." }
  Ok "signed in as $($account.user.name)"
  Ok "subscription  $($account.name) ($($account.id))"
  Ok "tenant        $($account.tenantId)"

  if (-not $Subscriptions) { $Subscriptions = @($account.id) }

  if (-not $ProjectEndpoint) {
    throw @"
-ProjectEndpoint is required: the app calls RuntimeError on startup without it.

Create one in Azure AI Foundry (https://ai.azure.com): a project, then a model deployment named
'$ModelDeployment'. The endpoint looks like
  https://<resource>.services.ai.azure.com/api/projects/<project>

If you only want the cost dashboard and would rather not deploy a model at all, say so and the
agent can be made optional -- it is a single startup check.
"@
  }

  Step "Resource group"
  # A group's location is metadata only -- resources inside it can live anywhere -- so an existing
  # group in another region is fine. Recreating it with a new -Location just errors.
  $rgLocation = AzTry group show -n $ResourceGroup --query location -o tsv --only-show-errors
  if ($rgLocation) {
    Ok "$ResourceGroup already exists in $rgLocation"
    if ($rgLocation -ne $Location) { Ok "resources will be created in $Location" }
  } else {
    Az group create -n $ResourceGroup -l $Location --only-show-errors | Out-Null
    Ok "$ResourceGroup in $Location"
  }

  Step "App Service plan and web app"
  $planName = "asp-$AppName"
  try {
    Az appservice plan create -g $ResourceGroup -n $planName --is-linux --sku $Sku -l $Location `
      --only-show-errors | Out-Null
  }
  catch {
    if ("$_" -match "quota") {
      throw @"
$Location has no App Service compute quota on this subscription, so the plan cannot be created.
This is common on Visual Studio and other credit-based subscriptions, which start at zero VMs
in most regions.

Find a region that will take it:
  az appservice list-locations --sku $Sku -o table

Then re-run with -Location <region>. Raising the quota instead is a support request:
  https://aka.ms/appservicequota

Original error:
$_
"@
    }
    throw
  }
  Ok "plan $planName ($Sku)"

  $exists = AzTry webapp show -g $ResourceGroup -n $AppName --only-show-errors
  if (-not $exists) {
    Az webapp create -g $ResourceGroup -p $planName -n $AppName --runtime "PYTHON:3.13" `
      --only-show-errors | Out-Null
    Ok "created $AppName"
  } else {
    Ok "$AppName already exists"
  }

  # One worker, deliberately. Sessions are in process memory.
  $startup = "python -m uvicorn app.main:app --host 0.0.0.0 --port 8000 --workers 1"
  $alwaysOn = if ($Sku -eq "F1") { "false" } else { "true" }
  Az webapp config set -g $ResourceGroup -n $AppName --always-on $alwaysOn `
    --startup-file $startup --number-of-workers 1 --only-show-errors | Out-Null
  Ok "single worker, always-on=$alwaysOn"
  if ($Sku -eq "F1") {
    Warn "F1 has no Always On: the app cold-starts and a long ingest may be killed part-way."
  }

  Step "Managed identity"
  $attached = Az webapp identity show -g $ResourceGroup -n $AppName --only-show-errors | ConvertFrom-Json
  if (-not $attached.type -or $attached.type -eq 'None') {
    $selector = Az webapp config appsettings list -g $ResourceGroup -n $AppName `
      --query "[?name=='AZURE_CLIENT_ID'].value" -o tsv --only-show-errors
    if (-not $selector) {
      # Bootstrap only a genuinely unassigned app. Never add/select a system identity
      # in place of an existing user-assigned runtime identity.
      Az webapp identity assign -g $ResourceGroup -n $AppName --only-show-errors | Out-Null
    }
  }
  $costAccess = Invoke-CloudLensCostAccess -HostingSubscriptionId $account.id -ResourceGroup $ResourceGroup `
    -AppName $AppName -CostScopeSubscriptions $Subscriptions -EnableExportManagement:$EnableExportManagement `
    -InstructionsOnly:$SkipRoleAssignment -ReportPath $CostAccessReportPath -AzCommand $AzExe
  $mi = $costAccess.identity.principalObjectId
  if ($SkipRoleAssignment) {
    Warn "Role writes skipped. Review the concrete runtime identity/role commands in $($costAccess.reportPath)."
    Warn "Foundry is separate: the same principal $mi also needs Cognitive Services OpenAI User on the Foundry account behind $ProjectEndpoint."
  } else {

    # The Foundry account behind -ProjectEndpoint is a separate data plane. Subscription Reader
    # does not reach it, so skipping this gives an app that loads and 401s on every question.
    $foundry = ([Uri]$ProjectEndpoint).Host.Split('.')[0]
    $foundryId = Az cognitiveservices account list `
      --query "[?name=='$foundry'].id | [0]" -o tsv --only-show-errors
    if (-not $foundryId) {
      throw "Could not find Foundry account '$foundry'. Grant Cognitive Services OpenAI User on the correct account and explicitly use -SkipRoleAssignment."
    } else {
      foreach ($role in @("Cognitive Services OpenAI User")) {
        AzTry role assignment create --assignee-object-id $mi `
          --assignee-principal-type ServicePrincipal --role $role `
          --scope $foundryId --only-show-errors | Out-Null
      }
      $have = @(Az role assignment list --assignee $mi --scope $foundryId `
        --include-inherited --query "[].roleDefinitionName" -o tsv --only-show-errors)
      if ("Cognitive Services OpenAI User" -notin $have) {
        throw "$foundry is missing Cognitive Services OpenAI User. Verify role-assignment rights and retry."
      }
      Ok "$foundry -> $($have -join ', ')"
    }
  }

  Step "Application settings"
  $authDirectory = Az webapp config appsettings list -g $ResourceGroup -n $AppName `
    --query "[?name=='AUTH_DATA_DIR'].value" -o tsv --only-show-errors
  if (-not $authDirectory) { $authDirectory = "/home/data" }
  if (-not $authDirectory.StartsWith("/home/") -or $authDirectory.StartsWith("/home/site/wwwroot")) {
    throw "AUTH_DATA_DIR must be outside wwwroot on persistent /home storage. Migrate existing auth state explicitly before deploying."
  }
  $settings = @(
    "PGHOST=$PgHost"
    "PGDATABASE=$PgDatabase"
    "PGUSER=$PgUser"
    "PGSSLMODE=require"
    "CLOUDLENS_PRINCIPAL_ID=$mi"
    "CLOUDLENS_TENANT_ID=$($account.tenantId)"
    "SNAPSHOT_CACHE_ENABLED=true"
    "WEBSITE_WARMUP_PATH=/readyz"
    "WEBSITE_WARMUP_STATUSES=200"
    "AUTH_DATA_DIR=$authDirectory"
    "PROJECT_ENDPOINT=$ProjectEndpoint"
    "MODEL_DEPLOYMENT_NAME=$ModelDeployment"
    "AUTH_COOKIE_SECURE=true"
    "AUTH_DISABLED=false"
    "SCM_DO_BUILD_DURING_DEPLOYMENT=true"
    # Kudu kills a build command that has produced no output for SCM_COMMAND_IDLE_TIMEOUT
    # seconds, and the default is 180. Extracting the Python SDK and resolving wheels on a B1
    # is silent for longer than that, so the build is killed mid-extraction and reported as a
    # failure with a log that simply stops. Nothing is wrong with the dependencies.
    "SCM_COMMAND_IDLE_TIMEOUT=1800"
    "SCM_LOGSTREAM_TIMEOUT=1800"
    # The default 230s is not enough: this app opens the warehouse and builds its first
    # overview before serving, and a cold B1 in a busy region needs longer. Falling short
    # gets the container killed mid-start and looks exactly like a crash.
    "WEBSITES_CONTAINER_START_TIME_LIMIT=600"
  )

  # Set once and keep: regenerating it on every deploy signs everyone out.
  $existingSecret = Az webapp config appsettings list -g $ResourceGroup -n $AppName `
    --query "[?name=='AUTH_SECRET'].value" -o tsv --only-show-errors
  if (-not $existingSecret) {
    $bytes = New-Object byte[] 32
    [Security.Cryptography.RandomNumberGenerator]::Create().GetBytes($bytes)
    $settings += "AUTH_SECRET=$([Convert]::ToBase64String($bytes))"
    Ok "generated AUTH_SECRET"
  } else {
    Ok "kept existing AUTH_SECRET (sessions survive this deploy)"
  }

  if ($EntraClientId -and ($EntraTenantId -or $MultiTenant -or $WorkAccountsOnly)) {
    # 'common' admits work, school and personal Microsoft accounts; 'organizations' excludes the
    # last. Personal accounts are included by default because they frequently own exactly the
    # kind of subscription this app is pointed at.
    $tenantSetting = if ($WorkAccountsOnly) { "organizations" }
                     elseif ($MultiTenant)  { "common" }
                     else                   { $EntraTenantId }
    $anyDirectory = $MultiTenant -or $WorkAccountsOnly
    $settings += "AUTH_CLIENT_ID=$EntraClientId"
    $settings += "AUTH_TENANT_ID=$tenantSetting"
    $settings += "AUTH_REDIRECT_URI=https://$AppName.azurewebsites.net/auth/callback"
    if ($EntraClientSecret) { $settings += "AUTH_CLIENT_SECRET=$EntraClientSecret" }
    if ($DelegatedArm -or $anyDirectory) { $settings += "AUTH_DELEGATED_ARM=true" }
    if ($Admins)            { $settings += "AUTH_ADMINS=$($Admins -join ',')" }
    if ($HomeTenant)        { $settings += "AUTH_HOME_TENANT=$HomeTenant" }

    if ($anyDirectory) {
      $wanted = if ($WorkAccountsOnly) { "AzureADMultipleOrgs" }
                else { "AzureADandPersonalMicrosoftAccount" }
      Ok "Entra sign-in configured for $tenantSetting"
      $audience = AzTry ad app show --id $EntraClientId --query signInAudience -o tsv
      if ($audience -eq $wanted) {
        Ok "app registration audience is $audience"
      } else {
        Warn "The app registration says signInAudience=$audience, which will turn away the"
        Warn "accounts this deployment is meant to admit. Fix it with:"
        Warn "  az ad app update --id $EntraClientId --sign-in-audience $wanted"
        if ($wanted -eq "AzureADandPersonalMicrosoftAccount") {
          Warn "That change needs v2 access tokens first, if it is refused:"
          Warn "  az rest --method PATCH --url https://graph.microsoft.com/v1.0/applications/<objectId> \"
          Warn "    --headers Content-Type=application/json --body '{\"api\":{\"requestedAccessTokenVersion\":2}}'"
        }
      }
    } else {
      Ok "Entra sign-in configured"
    }
    $platform = if ($EntraClientSecret) { "Web" } else { "Mobile and desktop applications (public client)" }
    Warn "Add this exact redirect URI to the app registration (type: $platform):"
    Warn "  https://$AppName.azurewebsites.net/auth/callback"
  } else {
    Warn "No Entra changes requested; existing sign-in settings are preserved."
    Warn "A new app uses a local password, written to the log on first start:"
    Warn "  az webapp log tail -g $ResourceGroup -n $AppName"
  }

  Az webapp config appsettings set -g $ResourceGroup -n $AppName --settings $settings `
    --only-show-errors | Out-Null
  Ok "$($settings.Count) settings applied"

  Step "Packaging"
  $zip = Join-Path $repo "cloudlens-$([guid]::NewGuid().ToString('N')).zip"
  $package = & (Join-Path $PSScriptRoot 'package.ps1') -Destination $zip -SourceRoot (Split-Path -Parent $PSScriptRoot)
  Ok "$($package.Files) files, $([Math]::Round($package.Bytes / 1MB, 1)) MB; SHA-256 $($package.Sha256)"

  Step "Deploying"
  # Never stop the live app or infer this release's success from an older deployment.
  try {
    Az webapp deploy -g $ResourceGroup -n $AppName --src-path $zip --type zip `
      --only-show-errors | Out-Null
    Ok "uploaded"
  } catch {
    Warn "Publication failed or its status is unknown. Inspect this deployment before retrying:"
    Warn "  az webapp log deployment show -g $ResourceGroup -n $AppName"
    throw
  } finally {
    Remove-Item -LiteralPath $zip -Force -ErrorAction SilentlyContinue
  }
  Step "Checking application readiness and sign-in"
  $base = "https://$AppName.azurewebsites.net"
  Wait-CloudLensApplication -BaseUrl $base
  Ok "ready; anonymous requests require sign-in"
  Write-Host "`nOpen $base" -ForegroundColor Green
}
finally {
  Pop-Location
}
