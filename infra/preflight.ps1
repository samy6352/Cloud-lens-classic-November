<#
.SYNOPSIS
    Check that a subscription is ready for CloudLens, before deploying anything.

.DESCRIPTION
    Reads only. Nothing here creates, changes or deletes a resource, so it is safe
    to run against a customer subscription during a planning call.

    It answers the question "what will stop this deployment", which is otherwise
    discovered halfway through a deployment that has already created half a stack:

      * Am I signed in, and to the right place
      * Do I hold enough Azure RBAC to create the stack and its role assignments
      * Do I hold enough directory rights to create the sign-in app registration,
        or does the customer's identity team need to
      * Are the resource providers registered
      * Is there quota for the App Service plan in this region
      * Which cost scopes can actually be read, and can they be exported

.EXAMPLE
    ./infra/preflight.ps1 -Location centralindia

.EXAMPLE
    ./infra/preflight.ps1 -Location eastus -CostScopeSubscriptions @('<sub-a>','<sub-b>')
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory)]
    [string]$Location,

    [string]$ResourceGroup = 'rg-cloudlens',

    [string[]]$CostScopeSubscriptions = @(),

    [string]$ManagementGroupId = '',

    # Region for the AI Foundry account, which is deliberately not $Location:
    # App Service quota and pay-as-you-go model availability are different sets.
    [string]$AiLocation = 'eastus2',

    [string]$ModelName = 'gpt-4.1-mini',

    [string]$ModelSku = 'GlobalStandard',

    [ValidateRange(1, 1000000)]
    [int]$ModelCapacity = 30,

    [string]$PostgresSku = 'Standard_E2ds_v5',

    [string]$AppServicePlanSku = 'B2',

    [bool]$NetworkIsolation = $true,

    [string]$VnetAddressPrefix = '10.70.0.0/16'
)

$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'deployment-checks.ps1')

$script:Blockers = @()
$script:Warnings = @()

function Section($text) {
    Write-Host ""
    Write-Host "== $text" -ForegroundColor Cyan
}

function Ok($text)   { Write-Host "   [ok]    $text" -ForegroundColor Green }
function Warn($text) { Write-Host "   [warn]  $text" -ForegroundColor Yellow; $script:Warnings += $text }
function Bad($text)  { Write-Host "   [BLOCK] $text" -ForegroundColor Red;   $script:Blockers += $text }
function Info($text) { Write-Host "           $text" -ForegroundColor DarkGray }

# ----------------------------------------------------------------- identity

Section "Sign-in"

$account = az account show -o json 2>$null | ConvertFrom-Json
if (-not $account) {
    Bad "Not signed in. Run 'az login'."
    Write-Host ""
    exit 1
}
Ok "$($account.name)"
Info "subscription $($account.id)"
Info "tenant       $($account.tenantId)"

$signedIn = az ad signed-in-user show -o json 2>$null | ConvertFrom-Json
$principalId = $null
if ($signedIn) {
    $principalId = $signedIn.id
    Ok "signed in as $($signedIn.userPrincipalName)"
    if ($signedIn.userPrincipalName -match '#EXT#') {
        Warn "the deployer is a guest identity; use the actual sign-in email in -Admins, not only the guest UPN"
        Info "guest UPN: $($signedIn.userPrincipalName); email: $($signedIn.mail)"
    }
} else {
    $spAppId = az account show --query user.name -o tsv 2>$null
    $sp = az ad sp show --id $spAppId -o json 2>$null | ConvertFrom-Json
    if ($sp) {
        $principalId = $sp.id
        Ok "signed in as service principal $($sp.displayName)"
    } else {
        Warn "could not resolve the signed-in principal; role checks will be skipped"
    }
}

# --------------------------------------------------------------- azure RBAC

Section "Azure permissions on this subscription"

# Role assignments are what the deployment cannot work around. Everything else
# it can create; a missing roleAssignments/write means it gets most of the way
# and then fails on the grant, leaving a half-built stack behind.
if ($principalId) {
    $subScope = "/subscriptions/$($account.id)"
    $roles = az role assignment list --assignee $principalId --scope $subScope `
        --include-inherited --include-groups --query "[].roleDefinitionName" -o tsv 2>$null

    $roleList = @($roles -split "`n" | Where-Object { $_ })
    if ($roleList.Count -eq 0) {
        Warn "no role assignments visible (you may still have access through a group)"
    } else {
        Info ("roles: " + (($roleList | Select-Object -Unique) -join ", "))
    }

    $canAssign = $roleList | Where-Object { $_ -in @('Owner', 'User Access Administrator', 'Role Based Access Control Administrator') }
    if ($canAssign) {
        Ok "can create role assignments ($($canAssign | Select-Object -First 1))"
    } else {
        Bad "needs Owner or User Access Administrator to grant the app its cost access"
        Info "the deployment creates role assignments for the managed identity"
    }

    $canWrite = $roleList | Where-Object { $_ -in @('Owner', 'Contributor') }
    if ($canWrite) {
        Ok "can create resources ($($canWrite | Select-Object -First 1))"
    } else {
        Bad "needs Owner or Contributor to create the resource group and its resources"
    }
} else {
    Warn "deployment-principal RBAC could not be verified"
}

# ------------------------------------------------------------ directory rights

Section "Directory rights for the sign-in app registration"

# Reported rather than enforced, but the wording has to match what `deploy.ps1`
# actually does by default -- which is to CREATE a registration, not to take one.
# `deployEntraApp` is true unless -SkipEntra or -EntraClientId is passed, so saying
# "not required to deploy" here sent people into a deployment that then failed on the
# one step this section exists to warn about.
$canCreateApps = $false
try {
    # Creating an app registration is normally open to every user, but a tenant
    # can turn that off. There is no read-only "may I" API, so this reads the
    # tenant-wide setting the same way the portal does.
    $policy = az rest --method GET `
        --url "https://graph.microsoft.com/v1.0/policies/authorizationPolicy" `
        -o json 2>$null | ConvertFrom-Json
    if ($null -ne $policy) {
        if ($policy.defaultUserRolePermissions.allowedToCreateApps) {
            Ok "this tenant lets users create app registrations"
            $canCreateApps = $true
        } else {
            Warn "this tenant does not let ordinary users create app registrations"
            Info "The default deployment creates one. Either have the identity team make it"
            Info "and pass it with -EntraClientId, or deploy with -SkipEntra and sign in with"
            Info "a local password instead:"
            Info ""
            Info "  ./infra/deploy.ps1 -Location $Location -SkipEntra"
        }
    } else {
        Warn "could not read the tenant's app registration policy; creation rights remain unverified"
    }
} catch {
    Warn "could not read the tenant's app registration policy"
}

Info ""
Info "Admin consent for delegated ARM access (user_impersonation) needs Cloud"
Info "Application Administrator or higher, and is a one-time action per tenant."
Info "Without it nobody can sign in, so agree who does this before deployment day."
Warn "delegated ARM tenant consent has not been verified; subscription Owner does not establish directory consent rights"
Info ""
Info "Where that consent cannot be obtained, deploy with -AllowAppIdentityFallback."
Info "CloudLens then resolves who holds a cost-capable role, which needs no directory"
Info "rights -- but shows only subscriptions CloudLens itself was granted Reader on."
Info "Select each scope to be analysed with -CostScopeSubscriptions (a same-tenant"
Info "-CostScopeAllVisible sweep is opt-in), or people"
Info "silently see less than they are entitled to."

# ---------------------------------------------------------- resource providers

Section "Resource providers"

$needed = @(
    'Microsoft.Web',
    'Microsoft.DBforPostgreSQL',
    'Microsoft.Storage',
    'Microsoft.ManagedIdentity',
    'Microsoft.OperationalInsights',
    'Microsoft.Insights',
    # The AI Foundry account and its model deployment. Not optional and not conditional:
    # app/main.py refuses to start without PROJECT_ENDPOINT, so a subscription that cannot
    # register this provider cannot run CloudLens at all -- which is exactly the kind of
    # answer this script exists to give before anything is created rather than after.
    'Microsoft.CognitiveServices'
)
if ($NetworkIsolation) { $needed += 'Microsoft.Network' }

foreach ($rp in $needed) {
    $state = az provider show -n $rp --query registrationState -o tsv 2>$null
    if ($state -eq 'Registered') {
        Ok "$rp"
    } else {
        # Registering is something the deployment itself will trigger, but it can
        # take minutes and it needs Contributor -- better to know now.
        Warn "$rp is '$state'; register it before deployment and wait for Registered status"
    }
}

# ------------------------------------------------------------------- capacity

Section "Region and capacity in $Location"

# The capabilities API rather than `az postgres flexible-server list-skus`, whose
# output is not JSON and cannot be queried. This also proves the region offers
# the requested tier at all. SKU availability varies by region.
$capUrl = ("https://management.azure.com/subscriptions/$($account.id)/providers/" +
           "Microsoft.DBforPostgreSQL/locations/$Location/capabilities?api-version=2024-08-01")
$caps = az rest --method GET --url $capUrl -o json 2>$null | ConvertFrom-Json
if ($caps) {
    $tier = Get-CloudLensPostgresTier $PostgresSku
    $skus = $caps.value[0].supportedServerEditions |
        Where-Object { $_.name -eq $tier } |
        ForEach-Object { $_.supportedServerSkus.name }
    if ($skus -contains $PostgresSku) {
        Ok "PostgreSQL $PostgresSku ($tier) is available in $Location"
    } else {
        Bad "PostgreSQL $PostgresSku ($tier) is not offered in $Location"
        Info "available $tier SKUs: $($skus -join ', ')"
    }
} else {
    Warn "could not read PostgreSQL capabilities for $Location"
}

$planSkus = az appservice list-locations --sku $AppServicePlanSku --linux-workers-enabled `
    --query "[].name" -o tsv 2>$null
if ($planSkus) {
    $normalised = @($planSkus -split "`n" | ForEach-Object { $_ -replace '\s', '' }) 
    if ($normalised -contains $Location) {
        Ok "Linux App Service $AppServicePlanSku is available in $Location"
        Info "this is SKU availability, not a reservation or proof of regional subscription capacity"
    } else {
        Bad "Linux App Service $AppServicePlanSku is not offered in $Location"
    }
} else {
    Warn "could not read App Service locations"
}

# ------------------------------------------------------------------ AI model

Section "Model availability in $AiLocation"

# The chat agent is not optional -- the app refuses to start without a Foundry
# endpoint -- so a region that does not offer the model on a pay-as-you-go SKU
# is a blocker rather than a degraded deployment. centralindia is the common
# case: it lists gpt-4.1-mini, but only as GlobalProvisionedManaged, which is
# reserved capacity bought by the month.
$models = az cognitiveservices model list -l $AiLocation -o json 2>$null | ConvertFrom-Json
$offered = @()
$modelSkus = @($models | Where-Object { $_.model.name -eq $ModelName } | ForEach-Object { $_.model.skus })
if ($modelSkus.Count) {
    $offered = @($modelSkus | ForEach-Object { $_.name } | Where-Object { $_ })
    if ($offered -contains $ModelSku) {
        Ok "$ModelName is available as $ModelSku in $AiLocation"
    } else {
        Bad "$ModelName in $AiLocation is offered only as: $(($offered | Sort-Object -Unique) -join ', ')"
        Write-Host "           Pass -AiLocation with a region offering $ModelSku, or -ModelSku with one listed above."
    }
} else {
    Bad "$ModelName is not offered in $AiLocation"
}

if ($offered -contains $ModelSku) {
    $usageUrl = "https://management.azure.com/subscriptions/$($account.id)/providers/Microsoft.CognitiveServices/locations/$AiLocation/usages?api-version=2025-06-01"
    $usages = az rest --method GET --url $usageUrl -o json 2>$null | ConvertFrom-Json
    $quota = Get-CloudLensModelQuota $models $usages.value $ModelName $ModelSku $ModelCapacity
    switch ($quota.Status) {
        'ok' { Ok $quota.Message }
        'blocked' { Bad $quota.Message }
        default { Warn $quota.Message }
    }
}

if ($NetworkIsolation) {
    Section "Network address planning"
    $null = Get-CloudLensIpv4Range $VnetAddressPrefix
    $vnets = az network vnet list --subscription $account.id -o json 2>$null | ConvertFrom-Json
    if ($LASTEXITCODE -ne 0) {
        Warn "could not inventory existing VNets; address overlap is unverified"
    } else {
        $overlaps = @()
        foreach ($vnet in $vnets) {
            foreach ($prefix in $vnet.addressSpace.addressPrefixes) {
                if ($prefix -notmatch ':') {
                    if (Test-CloudLensIpv4Overlap $VnetAddressPrefix $prefix) {
                        $overlaps += "$($vnet.resourceGroup)/$($vnet.name) ($prefix)"
                    }
                }
            }
        }
        if ($overlaps.Count) {
            Warn "$VnetAddressPrefix overlaps: $($overlaps -join '; ')"
            Info "isolated VNets can overlap, but peering/routed integration cannot; choose new ranges for a fresh customer stack"
        } else {
            Ok "$VnetAddressPrefix does not overlap the visible VNets in this subscription"
            Info "other subscriptions and on-premises networks still need customer address-plan approval"
        }
    }
}

Section "Azure Policy"
$policies = az policy assignment list --scope "/subscriptions/$($account.id)" `
    --disable-scope-strict-match -o json 2>$null | ConvertFrom-Json
if ($LASTEXITCODE -ne 0) {
    Warn "could not read applicable policy assignments; policy compatibility remains unverified"
} else {
    Info "$(@($policies).Count) applicable assignment(s) returned; assignment presence is not a compliance verdict"
    foreach ($policy in $policies) { Info "$($policy.displayName) [$($policy.enforcementMode)]" }
}
Warn "Azure Policy effects and regional capacity require template validation/What-If; read-only inventory alone cannot certify deployment"

# ------------------------------------------------------------------ existing

Section "Target resource group"

$existing = az group show -n $ResourceGroup -o json 2>$null | ConvertFrom-Json
if ($existing) {
    # Counted client-side. A JMESPath `length(@)` passed through az on Windows is
    # re-parsed by cmd.exe, which eats the parentheses and prints the command line
    # back at you instead of a number.
    $resources = az resource list -g $ResourceGroup -o json 2>$null | ConvertFrom-Json
    $count = if ($resources) { @($resources).Count } else { 0 }
    Warn "$ResourceGroup already exists in $($existing.location) with $count resource(s)"
    Info "the deployment is incremental and will update it rather than replace it"
    if ($existing.location -ne $Location) {
        Bad "existing group is in $($existing.location) but you asked for $Location"
        Info "a resource group's location cannot be changed; pick another name"
    }
} else {
    Ok "$ResourceGroup does not exist yet and will be created"
}

# ---------------------------------------------------------------- cost scopes

Section "Cost data access"
Info "Human Owner/Contributor and the Entra sign-in registration do not grant the runtime managed identity access."
Info "deploy.ps1 grants Reader + Cost Management Reader to the application's active identity on selected subscriptions."
Info "-EnableExportManagement optionally adds Cost Management Contributor for export schedules and budgets."
Info "Failed grants produce a non-secret identity/role handoff; grant-cost-access.ps1 repairs access without redeploying."
Info "This does not replace the separate hosting infrastructure/bootstrap permission requirements above."

if ($CostScopeSubscriptions.Count -eq 0) {
    $CostScopeSubscriptions = @($account.id)
}

foreach ($sub in $CostScopeSubscriptions) {
    if ($sub -notmatch '^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$') {
        Bad "invalid cost subscription GUID: $sub"
        continue
    }
    $info = az account show --subscription $sub -o json 2>$null | ConvertFrom-Json
    if (-not $info) {
        Bad "cannot see subscription $sub"
        continue
    }
    if ($info.id -ine $sub -or $info.tenantId -ine $account.tenantId) {
        Bad "subscription $sub does not match the hosting managed identity's tenant"
        continue
    }

    # Which agreement this is decides which ingestion path works, and the answer
    # is not cosmetic: FOCUS exports are refused outright on pay-as-you-go.
    $quota = az rest --method GET `
        --url "https://management.azure.com/subscriptions/$sub`?api-version=2020-01-01" `
        --query "subscriptionPolicies.quotaId" -o tsv 2>$null

    Ok "$($info.name)"
    Info "offer: $quota"

    if ($quota -match '^(MSDN|MSDNDevTest|PayAsYouGo|MS-AZR-0003P|MS-AZR-0063P|VisualStudio)') {
        Warn "  pay-as-you-go / MSDN: FOCUS exports are not supported on this agreement"
        Info "  Microsoft documents this as 'Azure MOSP billing scopes and subscriptions"
        Info "don't support FOCUS datasets'. The Cost Details API can also be unsupported;"
        Info "use the Query API fallback and disclose its missing tags/benefit detail."
    }
}

$visible = @(az account list --query "[?state=='Enabled' && tenantId=='$($account.tenantId)'].{id:id,name:name}" --all -o json 2>$null |
    ConvertFrom-Json)
$scoped = @($CostScopeSubscriptions | ForEach-Object { $_.ToLower() })
$uncovered = @($visible | Where-Object { $scoped -notcontains $_.id.ToLower() })
if ($uncovered.Count) {
    Warn "$($uncovered.Count) enabled subscription(s) you can see are outside the cost scope"
    foreach ($sub in $uncovered | Select-Object -First 8) { Info "  $($sub.name) ($($sub.id))" }
    Info "Under -AllowAppIdentityFallback these are invisible to every user regardless of"
    Info "their own RBAC. Pass -CostScopeAllVisible to deploy.ps1 to include them."
}

if ($ManagementGroupId) {
    Section "Management group scope"
    $mg = az account management-group show -n $ManagementGroupId -o json 2>$null | ConvertFrom-Json
    if ($mg) {
        Ok "$($mg.displayName)"
        Info "Cost Management Reader assigned here is inherited by every subscription below,"
        Info "which is the least-effort way to cover a whole estate."
        Warn "management group scope is not supported for Microsoft Customer Agreement,"
        Info "and the Cost Details API does not support it for EA or MCA. Per-subscription"
        Info "assignment is the portable choice."
    } else {
        Bad "cannot see management group $ManagementGroupId"
    }
}

# -------------------------------------------------------------------- verdict

Section "Verdict"

if ($script:Blockers.Count -eq 0) {
    Write-Host "   No confirmed blockers in the checks completed." -ForegroundColor Green
    if ($script:Warnings.Count) {
        Write-Host "   $($script:Warnings.Count) warning(s) or unverified prerequisites remain; this is not production approval." -ForegroundColor Yellow
    }
    Write-Host ""
    Write-Host "   ./infra/deploy.ps1 -Location $Location -AiLocation $AiLocation -ResourceGroup $ResourceGroup -VnetAddressPrefix $VnetAddressPrefix -ModelCapacity $ModelCapacity -WhatIf" -ForegroundColor Cyan
    Info "When changing the VNet prefix, also pass all three subnet prefixes contained within it."
    Write-Host ""
    exit 0
}

Write-Host "   $($script:Blockers.Count) blocker(s):" -ForegroundColor Red
foreach ($b in $script:Blockers) { Write-Host "     - $b" -ForegroundColor Red }
Write-Host ""
exit 1
