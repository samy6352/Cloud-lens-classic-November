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
    [string]$AiLocation = 'eastus2',

    [string]$ModelName = 'gpt-4.1-mini',

    [string]$ModelSku = 'GlobalStandard',

    # Create the Entra app registration people sign in through, and point the app
    # at it. Needs directory rights: Application Developer or higher. Where the
    # identity team owns app registrations, have them create one and pass its
    # client ID with -EntraClientId instead.
    [switch]$SkipEntra,

    # An existing app registration to use rather than creating one. Its redirect
    # URI is updated to match this deployment's hostname.
    [string]$EntraClientId = '',

    # Keep the existing default for redeployments; use a new stable value for an independent stack.
    [string]$EntraAppUniqueName = 'cloudlens',

    # Explicit opt-in only: limited mode uses the application's ARM identity.
    [switch]$AllowAppIdentityFallback,

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

    # Database sizing. Previously absent, so every deployment silently took the
    # Bicep defaults (Standard_B1ms / 32GB / B1) regardless of the estate it was
    # being asked to serve.
    [string]$PostgresSku = 'Standard_B1ms',

    [ValidateRange(32, 65536)]
    [int]$PostgresStorageGb = 32,

    [ValidateSet('11', '12', '13', '14', '15', '16', '17', '18')]
    [string]$PostgresVersion = '16',

    # Premium_LRS ties IOPS to capacity: 32GB buys 120 IOPS. PremiumV2_LRS
    # provisions IOPS and throughput separately, which is both cheaper and
    # faster for a working set that cannot be cached.
    [ValidateSet('Premium_LRS', 'PremiumV2_LRS')]
    [string]$PostgresStorageType = 'Premium_LRS',

    [ValidateRange(360, 80000)]
    [int]$PostgresIops = 3000,

    [ValidateRange(25, 1200)]
    [int]$PostgresThroughput = 125,

    # Partition a NEW costs table by month. Read once at application import and
    # unable to convert a table that already exists, so it has to be right before
    # the app first starts. Passed through the template rather than set afterwards
    # with `az webapp config appsettings set`: appSettings is declared in the
    # Bicep, so a later deployment discards anything the CLI added.
    [switch]$PartitionCosts,

    [string]$PgWorkMem = '',

    [string]$PgStatementTimeout = '',

    [string]$AppServicePlanSku = 'B1',

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
    [switch]$EmitArm
)

$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
. (Join-Path $PSScriptRoot 'deployment-checks.ps1')
. (Join-Path $PSScriptRoot 'cost-access.ps1')
. (Join-Path $PSScriptRoot 'storage-naming.ps1')
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

# ------------------------------------------------------------ preconditions

Step "Checking sign-in"
$account = az account show -o json | ConvertFrom-Json
Assert-AzSuccess 'Reading the active Azure account'
if (-not $account) { throw "Not signed in. Run 'az login' first." }
Write-Host "    subscription : $($account.name) ($($account.id))"
Write-Host "    tenant       : $($account.tenantId)"

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
    "appServicePlanSku=$AppServicePlanSku"
    "entraAppUniqueName=$EntraAppUniqueName"
    "deployEntraApp=$((-not $SkipEntra -and -not $EntraClientId).ToString().ToLower())"
    "authDelegatedArm=$((-not $AllowAppIdentityFallback).ToString().ToLower())"
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

$result = az deployment sub create `
    --name $deploymentName `
    --location $Location `
    --template-file $templateFile `
    --parameters $templateParameters `
    --query properties.outputs -o json | ConvertFrom-Json
Assert-AzSuccess 'Provisioning the infrastructure'

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
    -EnableExportManagement:$EnableExportManagement -ReportPath $CostAccessReportPath
if ($costAccess.pendingSubscriptions.Count) {
    Write-Warning 'Optional cost scopes remain pending; this deployment does not have complete cost coverage. Use infra\grant-cost-access.ps1 to repair access without redeploying.'
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
if (-not $SkipFocusExports) {
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
        Write-Warning 'The deployment is complete and Quick refresh works. Hourly Billing,'
        Write-Warning 'Applications & Tags and scheduling savings stay empty until exports exist.'
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
    if ($EntraClientId) {
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
        if (-not $armScope.Count) {
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

    # Delegated ARM is the default, and it is the reason the subscription list is correct
    # rather than bounded by what this app's own identity can read. It needs a one-time tenant
    # admin consent for `user_impersonation`, without which nobody can sign in at all -- so try
    # it here rather than leaving a deployment that looks finished and refuses everyone at the
    # door. AUTH_DELEGATED_ARM itself is carried by the template, not patched on here.
    Step "Granting admin consent for delegated ARM access"
    az ad app permission admin-consent --id $clientId --only-show-errors -o none 2>$null
    $consented = $LASTEXITCODE -eq 0
    if ($consented -and -not $AllowAppIdentityFallback) {
        Write-Host "    consented    : ARM user_impersonation" -ForegroundColor Green
    }
    elseif ($consented) {
        # Fallback was requested but is no longer needed. Take the better mode rather than
        # leaving a deployment permanently limited by a flag nobody remembers passing.
        az webapp config appsettings set `
            --resource-group $ResourceGroup `
            --name $webAppName `
            --settings "AUTH_DELEGATED_ARM=true" `
            --only-show-errors -o none
        Assert-AzSuccess 'Upgrading to delegated ARM after consent succeeded'
        Write-Host "    consented    : ARM user_impersonation" -ForegroundColor Green
        Write-Host "    -AllowAppIdentityFallback was not needed and has been ignored."
    }
    else {
        Write-Warning "Consent was refused. This needs Cloud Application Administrator or"
        Write-Warning "higher in $($account.tenantId), and is usually not the person who owns"
        Write-Warning "the subscription. An administrator grants it with:"
        Write-Warning "  az ad app permission admin-consent --id $clientId"
        Write-Warning ""
        if (-not $AllowAppIdentityFallback) {
            throw 'Delegated ARM consent is required. Have the tenant administrator grant consent and rerun, or explicitly choose -AllowAppIdentityFallback for limited app-identity mode.'
        }

        # Leaving delegated ARM on after a refusal ships a deployment that looks finished and
        # turns every single person away at the sign-in page, because the authorize request
        # asks for a scope nobody is allowed to consent to. The template already carries
        # AUTH_DELEGATED_ARM=false for this run, so people get in, and the subscription list
        # is bounded by what CloudLens itself holds Reader on -- visible in the UI, unlike a
        # door that will not open.
        Write-Warning "Continuing in app-identity mode (AUTH_DELEGATED_ARM=false). People see the"
        Write-Warning "subscriptions they hold a cost-capable role on, among those CloudLens was"
        Write-Warning "granted Reader on. Once consent is granted, rerun without the switch."
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
        az webapp deploy `
            --resource-group $ResourceGroup `
            --name $webAppName `
            --src-path $zip `
            --type zip `
            --only-show-errors -o none
        Assert-AzSuccess 'Publishing application code'
    } finally {
        Remove-Item -LiteralPath $zip -Force
    }

    Step "Checking application readiness and sign-in"
    Wait-CloudLensApplication -BaseUrl $webAppUrl
}

Step "Done"
Write-Host "    $webAppUrl" -ForegroundColor Green
Write-Host ""

# State the data path explicitly. Without exports the dashboard still loads and Quick refresh
# still works, so "it looks fine" is exactly the wrong signal to leave someone with.
if ($focusProvisioned) {
    Write-Host "Cost detail  : FOCUS exports provisioned. Azure delivers the first reports" -ForegroundColor Green
    Write-Host "               within a few hours; CloudLens imports them automatically." -ForegroundColor Green
} else {
    Write-Host "Cost detail  : no FOCUS exports. Quick refresh works, but Hourly Billing," -ForegroundColor Yellow
    Write-Host "               Applications & Tags and scheduling savings will be empty." -ForegroundColor Yellow
    Write-Host "               Fix with: infra\focus-exports.ps1 -ResourceGroup $ResourceGroup -WebAppName $webAppName -RunNow" -ForegroundColor Yellow
}
Write-Host ""

if ($SkipCode) {
    Write-Host 'Infrastructure/configuration only: application code was not published by this run.' -ForegroundColor Yellow
    return
}
if ($clientId -and $consented) {
    Write-Host "Sign in with the Microsoft button. Nothing further is required." -ForegroundColor Green
} elseif ($clientId -and $AllowAppIdentityFallback) {
    Write-Host "Sign in with the Microsoft button. Running in app-identity mode -- see the warning above." -ForegroundColor Yellow
} elseif ($clientId) {
    Write-Host "Sign-in is configured but not yet consented -- see the warning above." -ForegroundColor Yellow
} else {
    Write-Host "Sign-in uses a local password. Re-run without -SkipEntra, or pass" -ForegroundColor Yellow
    Write-Host "-EntraClientId <id>, to switch to the Microsoft button." -ForegroundColor Yellow
}
