<#
.SYNOPSIS
    Deploy CloudLens into an Azure subscription.

.DESCRIPTION
    Provisions the infrastructure from infra/main.bicep, grants the application's
    managed identity the Cost Management access it needs on the subscriptions it
    will analyse, and optionally publishes the application code.

    Safe to re-run: the Bicep deployment is incremental and the role assignments
    are idempotent.

.EXAMPLE
    ./infra/deploy.ps1 -Location centralindia

.EXAMPLE
    ./infra/deploy.ps1 -Location eastus -ResourceGroup rg-cloudlens-prod `
        -CostScopeSubscriptions @('0000...','1111...') -SkipCode
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory)]
    [string]$Location,

    [string]$ResourceGroup = 'rg-cloudlens',

    [string]$NamePrefix = 'cloudlens',
    [string]$NameSuffix = '',

    # Optional reviewed legacy name. Existing configured storage is always preserved.
    [string]$StorageAccountName = '',

    # Keep this file between preview/apply and pipeline runs. Default is scoped to subscription/RG.
    [string]$StorageNameStatePath = '',

    # Region for the AI Foundry account. Deliberately separate from -Location:
    # the regions with App Service quota and the regions offering a model on a
    # pay-as-you-go SKU are different sets, and centralindia -- the default for
    # the app -- offers gpt-4.1-mini only as reserved capacity.
    [string]$AiLocation = '',

    [string]$ModelName = 'gpt-4.1-mini',

    [string]$ModelSku = 'GlobalStandard',

    # Create the Entra app registration people sign in through, and point the app
    # at it. Needs directory rights: Application Developer or higher. Where the
    # identity team owns app registrations, have them create one and pass its
    # client ID with -EntraClientId instead.
    [switch]$SkipEntra,

    # Existing customer registration. In delegated mode it is verified read-only;
    # the identity team must preconfigure the exact callback.
    [string]$EntraClientId = '',

    # Keep the existing default for redeployments; use a new stable value for an independent stack.
    [string]$EntraAppUniqueName = 'cloudlens',

    # Explicit legacy mode only; not the customer delegated onboarding solution.
    [switch]$AllowAppIdentityFallback,

    # Customer default; requires preapproved delegated ARM user_impersonation.
    [switch]$DelegatedArm = $true,

    # Host provisioning still requires resource creation AND bootstrap RBAC rights.
    # Defer only cross-subscription cost grants, not hosting permissions.
    [switch]$DeferCostAccess,

    # Read-only handoff for an existing host; never provisions or changes Azure.
    [switch]$InstructionsOnly,
    [string]$AppName = '',
    [string]$HostingSubscriptionId = '',

    # Required when deploying as a service principal; its display name is not a user sign-in.
    [string[]]$Admins = @(),

    # False deploys the development shape: public endpoints restricted by
    # firewall, no virtual network. Some tenants forbid public storage endpoints
    # by policy, in which case only the isolated shape will deploy at all.
    [bool]$NetworkIsolation = $true,

    [string]$VnetAddressPrefix = '10.70.0.0/16',
    [string]$AppSubnetAddressPrefix = '10.70.1.0/24',
    [string]$PostgresSubnetAddressPrefix = '10.70.2.0/24',
    [string]$PrivateEndpointSubnetAddressPrefix = '10.70.3.0/24',

    [ValidateRange(1, 1000000)]
    [int]$ModelCapacity = 30,

    # Reference capacity was measured against the multi-million-row estate. Override
    # these values deliberately for smaller deployments or where a region lacks a SKU.
    [string]$PostgresSku = 'Standard_E2ds_v5',

    [ValidateRange(32, 65536)]
    [int]$PostgresStorageGb = 128,

    [ValidateSet('11', '12', '13', '14', '15', '16', '17', '18')]
    [string]$PostgresVersion = '16',

    # PremiumV2_LRS provisions IOPS and throughput independently of capacity.
    [ValidateSet('Premium_LRS', 'PremiumV2_LRS')]
    [string]$PostgresStorageType = 'PremiumV2_LRS',

    [ValidateRange(360, 80000)]
    [int]$PostgresIops = 3000,

    [ValidateRange(25, 1200)]
    [int]$PostgresThroughput = 125,

    # Partition a NEW costs table by month. Read once at application import and
    # unable to convert a table that already exists, so it has to be right before
    # the app first starts. Passed through the template rather than set afterwards
    # with `az webapp config appsettings set`: appSettings is declared in the
    # Bicep, so a later deployment discards anything the CLI added.
    [switch]$PartitionCosts = $true,

    [string]$PgWorkMem = '',

    [string]$PgStatementTimeout = '25s',

    # Off for a deployment whose data is loaded by other means: a boot with an
    # empty warehouse would otherwise ingest this subscription's own costs and
    # mix them into a restored estate. Production runs -AutoIngest:$false.
    [bool]$AutoIngest = $true,

    [bool]$AutoRefresh = $true,

    [bool]$SnapshotWarmingEnabled = $true,

    [bool]$SnapshotServerWarmingEnabled = $true,

    # Username/password sign-in alongside Entra, as production runs.
    [bool]$AuthAllowLocal = $false,

    # name:hash:role, PBKDF2. Never a plaintext password.
    [string]$AuthUsers = '',

    [string]$PgSchema = '',

    [string]$AppServicePlanSku = 'B2',

    # Subscriptions the application should be able to read cost data from.
    # Defaults to the subscription being deployed into.
    [string[]]$CostScopeSubscriptions = @(),

    # Grant cost access on every enabled subscription this deployer can see. The blunt
    # instrument for app-identity mode, where anything CloudLens cannot read is invisible to
    # everyone regardless of their own rights.
    [switch]$CostScopeAllVisible,

    # Opt in to managing export schedules/budgets; read-only cost access is the default.
    [switch]$EnableExportManagement,

    # Skip provisioning the FOCUS exports CloudLens imports from.
    #
    # They are provisioned by default because a deployment without them is quietly
    # half-built: Quick refresh works, so the dashboard looks healthy, while Hourly Billing,
    # Applications & Tags and scheduling savings stay empty because a query cannot return
    # UnitOfMeasure, PricingQuantity, MeterName or Tags. Nothing errors. Somebody notices
    # weeks later.
    #
    # This step needs rights the running application deliberately never has -- see
    # infra/focus-exports.ps1 -- so deployment time, as an operator, is the only honest
    # moment to do it. Pass this to defer it, then run focus-exports.ps1 separately.
    [switch]$SkipFocusExports,

    # Non-secret identity/role handoff. Defaults to ignored data\cost-access\<run>.json.
    [string]$CostAccessReportPath = '',

    # Skip the code publish and only touch infrastructure.
    [switch]$SkipCode,

    # Show what the deployment would change and stop. The equivalent of
    # `terraform plan`, and the thing to run before touching a customer's
    # subscription.
    [switch]$WhatIf,

    # Also write the compiled ARM template to infra/main.json, for customers
    # whose platform team deploys through Terraform. See infra/terraform.
    [switch]$EmitArm,

    # PostgreSQL SKUs tried in order if Azure refuses -PostgresSku with SkuNotAvailable.
    # The capabilities API lists SKUs a region offers but cannot see regional capacity, so
    # this is only discoverable at deployment time. General Purpose D-series have half the
    # memory of the default E-series; the substitution is reported when it happens.
    [string[]]$PostgresSkuFallback = @(),

    # Skip infra\preflight.ps1, which otherwise runs first and stops on any blocker.
    [switch]$SkipPreflight
)

$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
. (Join-Path $PSScriptRoot 'deployment-checks.ps1')
. (Join-Path $PSScriptRoot 'cost-access.ps1')
. (Join-Path $PSScriptRoot 'storage-naming.ps1')
. (Join-Path $PSScriptRoot 'delegated-consent.ps1')
if ($AllowAppIdentityFallback -and -not $PSBoundParameters.ContainsKey('DelegatedArm')) {
    $DelegatedArm = $false
}
if ($DelegatedArm -and $AllowAppIdentityFallback) {
    throw 'Choose -DelegatedArm OR managed-identity mode, not both.'
}
if ($InstructionsOnly) {
    if (-not $AppName) { throw '-InstructionsOnly requires the existing -AppName.' }
    Invoke-CloudLensCostAccess -HostingSubscriptionId $HostingSubscriptionId -ResourceGroup $ResourceGroup `
        -AppName $AppName -CostScopeSubscriptions $CostScopeSubscriptions -CostScopeAllVisible:$CostScopeAllVisible `
        -EnableExportManagement:$EnableExportManagement -InstructionsOnly -ReportPath $CostAccessReportPath
    return
}
if (-not $AiLocation) {
    throw 'Pass -AiLocation with the customer-approved AI region; no cross-region default is assumed.'
}
if ($NetworkIsolation) {
    $network = Get-CloudLensIpv4Range $VnetAddressPrefix
    $subnets = @($AppSubnetAddressPrefix, $PostgresSubnetAddressPrefix, $PrivateEndpointSubnetAddressPrefix)
    foreach ($subnet in $subnets) {
        $range = Get-CloudLensIpv4Range $subnet
        if ($range.Start -lt $network.Start -or $range.End -gt $network.End) {
            throw "Subnet $subnet must be contained in $VnetAddressPrefix."
        }
    }
    foreach ($subnet in @($AppSubnetAddressPrefix, $PostgresSubnetAddressPrefix)) {
        if ([int]$subnet.Split('/')[1] -gt 28) { throw "Delegated subnet $subnet must be /28 or larger." }
    }
    for ($i = 0; $i -lt $subnets.Count; $i++) {
        for ($j = $i + 1; $j -lt $subnets.Count; $j++) {
            if (Test-CloudLensIpv4Overlap $subnets[$i] $subnets[$j]) {
                throw "Subnets $($subnets[$i]) and $($subnets[$j]) overlap."
            }
        }
    }
}

function Step($message) {
    Write-Host ""
    Write-Host "==> $message" -ForegroundColor Cyan
}

function Assert-AzSuccess([string]$Operation) {
    if ($LASTEXITCODE -ne 0) {
        throw "$Operation failed (Azure CLI exit $LASTEXITCODE). Deployment is incomplete."
    }
}

# The permissions CloudLens asks for, and the only ones this installer will ever
# consent to on a registration it did not create. Kept in step with
# infra/modules/identity.bicep, which requests exactly these.
$CloudLensResourceAccess = @{
    # Microsoft Graph: sign someone in and learn their name. Nothing more --
    # cost data is read with the app's own identity or the caller's ARM token.
    '00000003-0000-0000-c000-000000000000' = @{
        name = 'Microsoft Graph'
        scopes = @{
            '37f7f235-527c-4136-accd-4a02d197296e' = 'openid'
            '14dad69e-099b-42c9-810b-d002981feec1' = 'profile'
            '7427e0e9-2fba-42fe-b0c0-848c9e6a8182' = 'offline_access'
            'e1fe6dd8-ba31-4d61-89e7-88639da4683d' = 'User.Read'
        }
    }
    # Azure Resource Manager: ask ARM as the signed-in person rather than as the app.
    '797f4846-ba00-4fd7-ba43-dac1f8f63013' = @{
        name = 'Azure Resource Manager'
        scopes = @{ '41094075-9dad-400e-a0bd-54e686782033' = 'user_impersonation' }
    }
}

function Assert-AdoptableRegistration([string]$ClientId) {
    <#
    .SYNOPSIS
        Refuse to touch an app registration that is not a CloudLens sign-in app.

    .DESCRIPTION
        `-EntraClientId` exists so an identity team can create the registration and
        hand back its client id. The cost of that convenience is that a mistyped or
        pasted-from-the-wrong-window id names somebody else's application, and the
        three things this script then does to it are all silent and all wrong:

          * adds a redirect URI pointing at this deployment,
          * adds delegated Azure Resource Manager access,
          * grants tenant-wide administrator consent for *everything it requests*.

        The third is the severe one. `az ad app permission admin-consent` consents
        to the whole of `requiredResourceAccess`, so pointing this at an app that
        happens to request `Directory.ReadWrite.All` would grant that, to that app,
        for every user in the tenant -- from a typo, with no prompt.

        So the registration is read once and checked before anything is written to
        it. The checks are deliberately conservative: each one describes a property
        a genuine CloudLens sign-in registration has, and anything else is refused
        by name rather than adopted hopefully.
    #>
    Step "Checking the registration $ClientId may be adopted"
    $json = az ad app show --id $ClientId -o json 2>$null
    if ($LASTEXITCODE -ne 0 -or -not $json) {
        throw ("The sign-in registration '$ClientId' could not be read. Check the client id, " +
               "and that you can see it in this tenant. Nothing has been changed.")
    }
    $app = $json | ConvertFrom-Json
    $name = if ($app.displayName) { $app.displayName } else { '(no display name)' }
    Write-Host "    adopting     : $name"

    $refusals = @()

    # A multi-tenant registration is used by directories this deployment has no
    # business altering, and consenting for it reaches further than this tenant.
    if ($app.signInAudience -ne 'AzureADMyOrg') {
        $refusals += "its sign-in audience is '$($app.signInAudience)', not the single-tenant AzureADMyOrg"
    }

    # An app exposing roles or an API is a platform component; CloudLens's own
    # registration exposes neither.
    if (@($app.appRoles | Where-Object { $_ }).Count) {
        $refusals += "it exposes $(@($app.appRoles).Count) app role(s)"
    }
    if (@($app.api.oauth2PermissionScopes | Where-Object { $_ }).Count) {
        $refusals += "it exposes its own API scope(s)"
    }

    # CloudLens signs people in as a public client with PKCE and never holds a
    # secret, so credentials here mean this is a different kind of application --
    # and a `web` redirect URI would make Entra refuse the code exchange anyway
    # (AADSTS7000218), which is a confusing failure to ship into.
    if (@($app.passwordCredentials | Where-Object { $_ }).Count -or
        @($app.keyCredentials | Where-Object { $_ }).Count) {
        $refusals += "it holds client credentials, which a PKCE public client does not use"
    }
    if (@($app.web.redirectUris | Where-Object { $_ }).Count) {
        $refusals += "it has web-platform redirect URIs, which make it a confidential client"
    }

    # The check that makes the later admin-consent safe.
    foreach ($resource in @($app.requiredResourceAccess | Where-Object { $_ })) {
        $known = $CloudLensResourceAccess[$resource.resourceAppId]
        if (-not $known) {
            $refusals += "it requests permissions from an API CloudLens does not use ($($resource.resourceAppId))"
            continue
        }
        foreach ($access in @($resource.resourceAccess | Where-Object { $_ })) {
            if ($access.type -ne 'Scope') {
                $refusals += "it requests an application permission on $($known.name), which CloudLens never needs"
            }
            elseif (-not $known.scopes.ContainsKey($access.id)) {
                $refusals += "it requests an unrecognised $($known.name) permission ($($access.id))"
            }
        }
    }

    if ($refusals.Count) {
        Write-Warning "This registration is not a CloudLens sign-in application:"
        foreach ($refusal in $refusals) { Write-Warning "  - $refusal" }
        Write-Warning ""
        Write-Warning "Nothing has been changed. Either pass the correct -EntraClientId, or omit"
        Write-Warning "it and let this script create a registration for you."
        throw ("Refusing to modify app registration '$ClientId' ($name): it is not owned by this " +
               "deployment and adopting it would alter, and consent for, an application in use elsewhere.")
    }
    Write-Host "    checks       : single-tenant, no credentials, no extra permissions" -ForegroundColor Green
}

# ------------------------------------------------------------ preconditions

Step "Checking sign-in"
$account = az account show -o json | ConvertFrom-Json
Assert-AzSuccess 'Reading the active Azure account'
if (-not $account) { throw "Not signed in. Run 'az login' first." }
if ($DelegatedArm) {
    if ($SkipEntra -or -not $EntraClientId -or -not $NameSuffix) {
        throw 'Customer delegated deployment requires -EntraClientId for an already admin-approved registration and an explicit -NameSuffix for a preapproved deterministic callback. -SkipEntra is incompatible.'
    }
    Assert-AdoptableRegistration $EntraClientId
    Assert-CloudLensDelegatedConsent -ClientId $EntraClientId `
        -RedirectUri "https://$NamePrefix-web-$NameSuffix.azurewebsites.net/auth/callback"
}
Write-Host "    subscription : $($account.name) ($($account.id))"
Write-Host "    tenant       : $($account.tenantId)"

# Everything preflight can know before anything is created is checked first, so a missing
# role, quota or capability stops the run here rather than after a long provisioning.
if (-not $SkipPreflight) {
    Step 'Running preflight checks'
    $preflightArgs = @{
        Location          = $Location
        ResourceGroup     = $ResourceGroup
        AiLocation        = $AiLocation
        ModelName         = $ModelName
        ModelSku          = $ModelSku
        ModelCapacity     = $ModelCapacity
        PostgresSku       = $PostgresSku
        AppServicePlanSku = $AppServicePlanSku
        NetworkIsolation  = $NetworkIsolation
        VnetAddressPrefix = $VnetAddressPrefix
        DelegatedArm      = $DelegatedArm
        EntraClientId     = $EntraClientId
        RedirectUri       = "https://$NamePrefix-web-$NameSuffix.azurewebsites.net/auth/callback"
    }
    if ($CostScopeSubscriptions.Count) { $preflightArgs.CostScopeSubscriptions = $CostScopeSubscriptions }
    & (Join-Path $PSScriptRoot 'preflight.ps1') @preflightArgs
    if ($LASTEXITCODE -ne 0) {
        throw 'Preflight reported blockers (listed above). Nothing has been created. Resolve them, or pass -SkipPreflight only if you have verified them another way.'
    }
}

# The deploying principal becomes the PostgreSQL administrator. Password
# authentication is disabled on the server, so without this nobody -- including
# the person running this script -- can connect to it afterwards.
Step "Resolving the deploying principal"
$signedIn = az ad signed-in-user show -o json 2>$null | ConvertFrom-Json
if ($signedIn) {
    $adminObjectId = $signedIn.id
    $adminName = $signedIn.userPrincipalName
    $adminType = 'User'
} else {
    # Running as a service principal, e.g. in a pipeline.
    $spAppId = (az account show --query user.name -o tsv)
    $sp = az ad sp show --id $spAppId -o json | ConvertFrom-Json
    Assert-AzSuccess 'Resolving the deployment service principal'
    $adminObjectId = $sp.id
    $adminName = $sp.displayName
    $adminType = 'ServicePrincipal'
}
Write-Host "    administrator: $adminName ($adminType)"
if (-not $adminObjectId -or -not $adminName) {
    throw 'The deployment principal could not be resolved.'
}
if (-not $SkipEntra -or $EntraClientId) {
    if (-not $Admins.Count) {
        if ($adminType -ne 'User') {
            throw 'Pass -Admins with the customer administrator sign-in addresses when deploying from a pipeline.'
        }
        $Admins = @($signedIn.mail, $adminName) | Where-Object { $_ } | Select-Object -Unique
    }
}

$developerIp = ''
if (-not $NetworkIsolation) {
    # Only needed to punch a hole in the public PostgreSQL firewall, which the
    # isolated shape does not have.
    $developerIp = (Invoke-RestMethod -Uri 'https://api.ipify.org?format=json').ip
    Write-Host "    client IP    : $developerIp"
}

# ---------------------------------------------------- stable export storage
Step 'Resolving deployment-owned export storage'
if (-not $StorageNameStatePath) {
    $scopeBytes = [Text.Encoding]::UTF8.GetBytes("$($account.id)/$ResourceGroup".ToLowerInvariant())
    $sha = [Security.Cryptography.SHA256]::Create()
    try { $scopeKey = [BitConverter]::ToString($sha.ComputeHash($scopeBytes)).Replace('-', '').ToLowerInvariant() }
    finally { $sha.Dispose() }
    $StorageNameStatePath = Join-Path $root "data\deployments\$scopeKey.storage.json"
}
$StorageAccountName = Get-CloudLensStorageAccountName -SubscriptionId $account.id `
    -ResourceGroup $ResourceGroup -NamePrefix $NamePrefix -NameSuffix $NameSuffix `
    -StorageAccountName $StorageAccountName -StatePath $StorageNameStatePath

# ---------------------------------------------------- deterministic name check
#
# Only when -NameSuffix is used. The default naming carries a hash of the resource
# group id on these resources, so a collision there is already
# improbable; a caller who asks for CL-web-prod has given that up on purpose and
# is asking for a name another Azure tenant may already hold.
#
# PostgreSQL and the web app are unique across the whole of Azure, not
# just this subscription. Checked here because the alternative is a partial
# deployment: ARM creates the identity, the VNet and the workspace, then fails on
# the name and leaves the resource group half-built.
if ($NameSuffix) {
    Step 'Checking globally-unique names are free'
    $postgresCandidate = "$($NamePrefix.ToLower())-postgres-$($NameSuffix.ToLower())"
    $webCandidate = "$NamePrefix-web-$NameSuffix"

    # The request body goes via a file rather than an inline argument. A JSON string
    # containing double quotes does not survive PowerShell's native-command argument
    # handling intact, and az reports the result as an invalid URL segment or a missing
    # content type -- which reads like the name was rejected rather than never checked.
    function Test-AzureName {
        param([string]$Url, [hashtable]$Body)
        $file = New-TemporaryFile
        try {
            $Body | ConvertTo-Json -Compress | Set-Content -LiteralPath $file -Encoding utf8 -NoNewline
            $raw = az rest --method post --url $Url --headers 'Content-Type=application/json' --body "@$file" -o json 2>&1
            if ($LASTEXITCODE -ne 0) { throw "name check failed: $raw" }
            return ($raw | ConvertFrom-Json)
        }
        finally { Remove-Item -LiteralPath $file -ErrorAction SilentlyContinue }
    }

    $results = @(
        @{ Kind = 'PostgreSQL server'; Name = $postgresCandidate; Type = 'Microsoft.DBforPostgreSQL/flexibleServers' }
        @{ Kind = 'Web app'; Name = $webCandidate; Type = 'Microsoft.Web/sites' }
    )

    $taken = @()
    foreach ($item in $results) {
        # Ours already? Then this is a redeploy, not a collision. Checked before the
        # availability API because that API answers "can this be created", and for a
        # resource that already exists the answer is always no -- which would make the
        # script refuse to run twice against its own deployment.
        $mine = az resource list --resource-group $ResourceGroup --name $item.Name `
            --resource-type $item.Type --query "[0].id" -o tsv 2>$null
        if ($mine) {
            Write-Host "    [ok]   $($item.Kind): $($item.Name) (exists in $ResourceGroup, will be updated)"
            continue
        }

        $result = switch ($item.Kind) {
            'PostgreSQL server' {
                Test-AzureName -Url "https://management.azure.com/subscriptions/$($account.id)/providers/Microsoft.DBforPostgreSQL/checkNameAvailability?api-version=2024-08-01" `
                               -Body @{ name = $item.Name; type = 'Microsoft.DBforPostgreSQL/flexibleServers' }
            }
            'Web app' {
                Test-AzureName -Url "https://management.azure.com/subscriptions/$($account.id)/providers/Microsoft.Web/checknameavailability?api-version=2023-12-01" `
                               -Body @{ name = $item.Name; type = 'Site' }
            }
        }

        if ($result.nameAvailable) {
            Write-Host "    [ok]   $($item.Kind): $($item.Name)"
        }
        else {
            $why = $result.reason, $result.message | Where-Object { $_ } | Select-Object -First 1
            Write-Host "    [FAIL] $($item.Kind): $($item.Name) -- $why" -ForegroundColor Red
            $taken += $item.Name
        }
    }
    if ($taken.Count) {
        throw ("These names are taken elsewhere in Azure, or are invalid: " +
               ($taken -join ', ') +
               ". Choose a different -NameSuffix or -NamePrefix; nothing has been created.")
    }
}

# ------------------------------------------------------------- provisioning

Step "Deploying infrastructure to $ResourceGroup ($Location)"
$deploymentName = "cloudlens-$(Get-Date -Format 'yyyyMMdd-HHmmss')"

$templateFile = Join-Path $PSScriptRoot 'main.bicep'
$templateParameters = @(
    "location=$Location"
    "resourceGroupName=$ResourceGroup"
    "namePrefix=$NamePrefix"
    "nameSuffix=$NameSuffix"
    "storageAccountName=$StorageAccountName"
    "adminPrincipalObjectId=$adminObjectId"
    "adminPrincipalName=$adminName"
    "adminPrincipalType=$adminType"
    "networkIsolation=$($NetworkIsolation.ToString().ToLower())"
    "vnetAddressPrefix=$VnetAddressPrefix"
    "appSubnetAddressPrefix=$AppSubnetAddressPrefix"
    "postgresSubnetAddressPrefix=$PostgresSubnetAddressPrefix"
    "privateEndpointSubnetAddressPrefix=$PrivateEndpointSubnetAddressPrefix"
    "developerClientIp=$developerIp"
    "aiLocation=$AiLocation"
    "modelName=$ModelName"
    "modelSku=$ModelSku"
    "modelCapacity=$ModelCapacity"
    "postgresSku=$PostgresSku"
    "postgresStorageGb=$PostgresStorageGb"
    "postgresVersion=$PostgresVersion"
    "postgresStorageType=$PostgresStorageType"
    "postgresIops=$PostgresIops"
    "postgresThroughput=$PostgresThroughput"
    "partitionCosts=$($PartitionCosts.IsPresent.ToString().ToLower())"
    "pgWorkMem=$PgWorkMem"
    "pgStatementTimeout=$PgStatementTimeout"
    "autoIngest=$($AutoIngest.ToString().ToLower())"
    "autoRefresh=$($AutoRefresh.ToString().ToLower())"
    "snapshotWarmingEnabled=$($SnapshotWarmingEnabled.ToString().ToLower())"
    "snapshotServerWarmingEnabled=$($SnapshotServerWarmingEnabled.ToString().ToLower())"
    "authAllowLocal=$($AuthAllowLocal.ToString().ToLower())"
    "authUsers=$AuthUsers"
    "pgSchema=$PgSchema"
    "appServicePlanSku=$AppServicePlanSku"
    "entraAppUniqueName=$EntraAppUniqueName"
    "deployEntraApp=$((-not $SkipEntra -and -not $EntraClientId).ToString().ToLower())"
    "authDelegatedArm=$($DelegatedArm.ToString().ToLower())"
    "authClientId=$EntraClientId"
    "authTenantId=$($account.tenantId)"
    "authRedirectUri=$(if ($EntraClientId -and $NameSuffix) { "https://$NamePrefix-web-$NameSuffix.azurewebsites.net/auth/callback" })"
    "authAdmins=$($Admins -join ',')"
)

if ($EmitArm) {
    # Terraform consumes the compiled ARM rather than a hand-written second
    # definition, so there is only ever one description of the stack.
    $armPath = Join-Path $PSScriptRoot 'main.json'
    az bicep build --file $templateFile --outfile $armPath
    Assert-AzSuccess 'Compiling the ARM template'
    Write-Host "    ARM template : $armPath"
}

if ($WhatIf) {
    Step "Previewing changes (nothing will be deployed)"
    az deployment sub what-if `
        --name $deploymentName `
        --location $Location `
        --template-file $templateFile `
        --parameters $templateParameters
    Assert-AzSuccess 'Previewing the infrastructure deployment'
    Write-Host ""
    Write-Host "Preview only. Re-run without -WhatIf to apply." -ForegroundColor Yellow
    return
}

$skuAttempts = @($PostgresSku) + @($PostgresSkuFallback | Where-Object { $_ -and $_ -ne $PostgresSku })
$result = $null
for ($i = 0; $i -lt $skuAttempts.Count; $i++) {
    $sku = $skuAttempts[$i]
    $attemptParameters = @($templateParameters | ForEach-Object { if ($_ -like 'postgresSku=*') { "postgresSku=$sku" } else { $_ } })
    $attemptName = if ($i -eq 0) { $deploymentName } else { "$deploymentName-r$i" }
    $errFile = New-TemporaryFile
    try {
        $raw = az deployment sub create `
            --name $attemptName `
            --location $Location `
            --template-file $templateFile `
            --parameters $attemptParameters `
            --query properties.outputs -o json 2>$errFile
        $exit = $LASTEXITCODE
        $errText = Get-Content -LiteralPath $errFile -Raw -ErrorAction SilentlyContinue
    }
    finally { Remove-Item -LiteralPath $errFile -ErrorAction SilentlyContinue }

    if ($exit -eq 0) {
        $result = ($raw | Out-String) | ConvertFrom-Json
        if ($i -gt 0) {
            Write-Warning "PostgreSQL was provisioned as $sku because $($skuAttempts[0]) was refused in $Location."
            Write-Warning "$sku is General Purpose with less memory than the E-series default; review database sizing if reports are slow."
        }
        break
    }
    if ($errText -match 'SkuNotAvailable' -and $errText -match 'flexibleServers' -and $i -lt ($skuAttempts.Count - 1)) {
        Write-Warning "Azure refused PostgreSQL SKU $sku in $Location (SkuNotAvailable). Retrying with $($skuAttempts[$i + 1])."
        continue
    }
    Write-Host $errText
    throw 'Provisioning the infrastructure failed. Deployment is incomplete; re-running is safe and reuses what was created.'
}

if (-not $result) { throw "Deployment produced no outputs." }

$webAppName = $result.webAppName.value
$webAppUrl = $result.webAppUrl.value
$redirectUri = "$webAppUrl/auth/callback"

Write-Host "    web app      : $webAppName"
Write-Host "    postgres     : $($result.postgresHost.value)"
Write-Host "    storage      : $($result.storageAccount.value)"
Write-Host "    foundry      : $($result.foundryAccount.value) ($AiLocation, $ModelName)"

# Cost grants target the app's verified active identity, never the deploying human or
# the sign-in registration. Every failure is reported before required scopes stop the run.
Step 'Granting managed-identity cost access'
$costAccess = Invoke-CloudLensCostAccess -HostingSubscriptionId $account.id -ResourceGroup $ResourceGroup `
    -AppName $webAppName -CostScopeSubscriptions $CostScopeSubscriptions -CostScopeAllVisible:$CostScopeAllVisible `
    -EnableExportManagement:$EnableExportManagement -InstructionsOnly:$DeferCostAccess -ReportPath $CostAccessReportPath
if ($costAccess.pendingSubscriptions.Count) {
    Write-Warning 'Selected cost scopes remain pending; this deployment does not have complete cost coverage. Use infra\grant-cost-access.ps1 to repair access without redeploying.'
}

# ------------------------------------------------------------ FOCUS exports
#
# The data path the whole dashboard rests on, provisioned here because here is the only
# place it can be. An export authenticates to storage with a *system-assigned* identity --
# the API accepts no other kind -- so Azure mints a new identity per export that must then
# be granted Storage Blob Data Contributor on the destination. Granting a role needs
# Microsoft.Authorization/roleAssignments/write, and the runtime identity is deliberately
# denied it (cost-access.ps1: "Never grant the runtime app subscription Owner or User Access
# Administrator"). The operator running this deployment already holds those rights
# legitimately; spending them once here is what keeps the application read-only forever.
#
# Never fatal. The stack is complete and usable without exports -- Quick refresh reads cost
# through ARM -- so a missing grant is reported and the deployment stands.
if (-not $SkipFocusExports -and -not $DeferCostAccess) {
    Step 'Provisioning FOCUS exports'
    $focusScope = @($costAccess.subscriptions |
        Where-Object { $_.validation -eq 'valid' } |
        ForEach-Object { $_.subscriptionId })
    if (-not $focusScope -or $focusScope.Count -eq 0) { $focusScope = @($account.id) }

    try {
        # -WhatIf already returned above, so this only ever runs for real.
        & (Join-Path $PSScriptRoot 'focus-exports.ps1') `
            -ResourceGroup $ResourceGroup -WebAppName $webAppName `
            -Subscriptions $focusScope -HostingSubscriptionId $account.id -RunNow
        $focusProvisioned = $true
    } catch {
        $focusProvisioned = $false
        Write-Warning "FOCUS exports were not provisioned: $($_.Exception.Message)"
        Write-Warning 'Data visibility is not verified. Detailed reporting requires readable exports and successful ingestion.'
        Write-Warning 'Re-run infra\focus-exports.ps1 once the reported access is granted.'
    }
} else {
    $focusProvisioned = $false
}

# ------------------------------------------------------------------ sign-in
#
# The app shows the Microsoft button when AUTH_CLIENT_ID and AUTH_TENANT_ID are
# set, and falls back to a local password when they are not. These cannot be set
# in stack.bicep: the registration's redirect URI needs the web app's hostname,
# so the app has to exist before the registration, and the client ID then has to
# come back to the app it depends on.

Step "Configuring sign-in"

$clientId = $EntraClientId
if (-not $clientId -and -not $SkipEntra) {
    $clientId = $result.entraClientId.value
}

if ($clientId) {
    if ($EntraClientId -and -not $DelegatedArm) {
        # Verify before writing. Everything below this line modifies the registration
        # or consents on its behalf, so the one chance to notice it is the wrong one
        # is here, before the first write.
        Assert-AdoptableRegistration $EntraClientId

        # An existing registration. Its redirect URIs are whatever the identity
        # team set, which will not include this deployment's hostname -- add it
        # without disturbing the others.
        #
        # Public-client platform, not web: CloudLens redeems the code with PKCE
        # and no secret, and Entra only allows that for a public client.
        $existingUris = @(az ad app show --id $clientId --query "publicClient.redirectUris" -o tsv 2>$null)
        Assert-AzSuccess 'Reading the existing sign-in registration'
        if ($existingUris -notcontains $redirectUri) {
            $merged = @($existingUris | Where-Object { $_ }) + $redirectUri
            az ad app update --id $clientId --public-client-redirect-uris @merged --only-show-errors -o none 2>$null
            Assert-AzSuccess "Adding the public-client redirect URI $redirectUri"
            Write-Host "    redirect URI added to the existing registration"
        }
        $requiredAccess = az ad app show --id $clientId --query requiredResourceAccess -o json | ConvertFrom-Json
        Assert-AzSuccess 'Reading requested API permissions'
        $armAccess = $requiredAccess | Where-Object { $_.resourceAppId -eq '797f4846-ba00-4fd7-ba43-dac1f8f63013' }
        $armScope = @($armAccess.resourceAccess | Where-Object {
            $_.id -eq '41094075-9dad-400e-a0bd-54e686782033' -and $_.type -eq 'Scope'
        })
        if ($DelegatedArm -and -not $armScope.Count) {
            az ad app permission add --id $clientId --api '797f4846-ba00-4fd7-ba43-dac1f8f63013' `
                --api-permissions '41094075-9dad-400e-a0bd-54e686782033=Scope' --only-show-errors -o none
            Assert-AzSuccess 'Requesting delegated ARM permission'
        }
    }

    az webapp config appsettings set `
        --resource-group $ResourceGroup `
        --name $webAppName `
        --settings "AUTH_CLIENT_ID=$clientId" `
                   "AUTH_TENANT_ID=$($account.tenantId)" `
                   "AUTH_REDIRECT_URI=$redirectUri" `
                   "AUTH_ADMINS=$($Admins -join ',')" `
        --only-show-errors -o none
    Assert-AzSuccess 'Applying sign-in settings'

    Write-Host "    client id    : $clientId"
    Write-Host "    redirect URI : $redirectUri"

    # Consent is never attempted in the default managed-identity mode.
    $consented = $false
    if ($DelegatedArm) {
        Assert-CloudLensDelegatedConsent -ClientId $clientId -RedirectUri $redirectUri
        $consented = $true
    }
} else {
    Write-Host "    skipped -- the app will use a local password" -ForegroundColor DarkGray
    Write-Host "    the generated password is printed once into the log at first start:"
    Write-Host "      az webapp log tail -g $ResourceGroup -n $webAppName"
}

# ------------------------------------------------------------------- code

if (-not $SkipCode) {
    Step "Packaging application"
    $zip = Join-Path $root "cloudlens-$([guid]::NewGuid().ToString('N')).zip"
    $package = & (Join-Path $root 'tools/package.ps1') -Destination $zip -SourceRoot $root
    Write-Host "    package      : $($package.Files) files, $([math]::Round($package.Bytes / 1MB, 1)) MB"
    Write-Host "    SHA-256      : $($package.Sha256)"

    Step "Publishing to $webAppName"
    try {
        # The platform's own startup tracking waits ten minutes and reports a failure even
        # when the site comes up moments later (a transient VNet or first-start failure).
        # Upload only, then judge the result by the application's own readiness check.
        az webapp deploy `
            --resource-group $ResourceGroup `
            --name $webAppName `
            --src-path $zip `
            --type zip `
            --track-status false `
            --only-show-errors -o none
        Assert-AzSuccess 'Publishing application code'
    } finally {
        Remove-Item -LiteralPath $zip -Force
    }

    Step "Checking application readiness and sign-in"
    try {
        Wait-CloudLensApplication -BaseUrl $webAppUrl -TimeoutSeconds 600 -Attempts 60
    } catch {
        Write-Warning "The app was not ready after publishing: $($_.Exception.Message)"
        Write-Warning 'Restarting it once; first starts occasionally fail on a transient platform error.'
        az webapp restart --resource-group $ResourceGroup --name $webAppName --only-show-errors -o none
        Assert-AzSuccess 'Restarting the web app'
        try {
            Wait-CloudLensApplication -BaseUrl $webAppUrl -TimeoutSeconds 600 -Attempts 60
        } catch {
            throw ("$($_.Exception.Message) Inspect startup with: az webapp log download -g $ResourceGroup -n $webAppName")
        }
    }
}

Step "Installation stages finished (data visibility unverified)"
if ($costAccess.status -ne 'complete') {
    Write-Warning "PENDING cost access: $($costAccess.pendingSubscriptions -join ', '). Report: $($costAccess.reportPath)"
}
Write-Host "    $webAppUrl" -ForegroundColor Green
Write-Host ""

# State the data path explicitly. Without exports the dashboard still loads and Quick refresh
# still works, so "it looks fine" is exactly the wrong signal to leave someone with.
if ($focusProvisioned) {
    Write-Host "Cost detail  : FOCUS exports provisioned. Azure delivers the first reports" -ForegroundColor Green
    Write-Host "               asynchronously; delivery, ingestion and per-user visibility remain unverified." -ForegroundColor Yellow
} else {
    Write-Host "Cost detail  : export provisioning skipped or failed; existing exports were not verified." -ForegroundColor Yellow
    Write-Host "               Fix with: infra\focus-exports.ps1 -ResourceGroup $ResourceGroup -WebAppName $webAppName -RunNow" -ForegroundColor Yellow
}
Write-Host ""

if ($SkipCode) {
    Write-Host 'Infrastructure/configuration only: application code was not published by this run.' -ForegroundColor Yellow
    return
}
if ($clientId -and $consented) {
    Write-Host "Delegated sign-in configured; validate sign-in and per-user data visibility." -ForegroundColor Green
} elseif ($clientId -and -not $DelegatedArm) {
    Write-Host "Sign in with the Microsoft button. Running in app-identity mode -- see the warning above." -ForegroundColor Yellow
} elseif ($clientId) {
    Write-Host "Sign-in is configured but not yet consented -- see the warning above." -ForegroundColor Yellow
} else {
    Write-Host "Sign-in uses a local password. Re-run without -SkipEntra, or pass" -ForegroundColor Yellow
    Write-Host "-EntraClientId <id>, to switch to the Microsoft button." -ForegroundColor Yellow
}
