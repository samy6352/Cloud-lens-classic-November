#requires -Version 7.0
<#
.SYNOPSIS
    Short guided CloudLens installer with menu choices and minimal typing.
#>
[CmdletBinding()]
param()

$ErrorActionPreference = 'Stop'
$GuidPattern = '^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$'

function Invoke-Checked([string]$Command, [string[]]$Arguments) {
    $global:LASTEXITCODE = 0
    $result = & $Command @Arguments
    if ($LASTEXITCODE -ne 0) { throw "$Command failed (exit $LASTEXITCODE)." }
    return $result
}

function Invoke-AzJson([string[]]$Arguments) {
    $json = Invoke-Checked 'az' ($Arguments + @('--only-show-errors', '-o', 'json'))
    if (-not $json) { return $null }
    return ($json -join "`n") | ConvertFrom-Json
}

function Read-Input([string]$Prompt, [string]$Pattern = '.+', [string]$Default = '') {
    $suffix = if ($Default) { " [$Default]" } else { '' }
    $value = (Read-Host "$Prompt$suffix (CANCEL to stop)").Trim()
    if ($value -ceq 'CANCEL') { throw 'Installation cancelled.' }
    if (-not $value -and $Default) { $value = $Default }
    if ($value -notmatch $Pattern) { throw "Invalid value for: $Prompt" }
    return $value
}

function Read-Choice([string]$Title, [object[]]$Options, [int]$Default = 1) {
    Write-Host ''
    Write-Host $Title -ForegroundColor Cyan
    for ($i = 0; $i -lt $Options.Count; $i++) {
        $hint = if (($i + 1) -eq $Default) { '  <- recommended' } else { '' }
        Write-Host ("  {0}. {1}{2}" -f ($i + 1), $Options[$i].Label, $hint)
    }
    $answer = (Read-Host "Choose 1-$($Options.Count) [$Default]").Trim()
    if ($answer -ceq 'CANCEL') { throw 'Installation cancelled.' }
    if ($answer -and $answer -notmatch '^\d+$') {
        throw "Choose a number from 1 to $($Options.Count)."
    }
    $pick = if ($answer) { [int]$answer } else { $Default }
    if ($pick -lt 1 -or $pick -gt $Options.Count) { throw "Choose a number from 1 to $($Options.Count)." }
    return $Options[$pick - 1].Value
}

function Get-StableSuffix([string]$SubscriptionId, [string]$ResourceGroup) {
    $sha = [Security.Cryptography.SHA256]::Create()
    try {
        $bytes = [Text.Encoding]::UTF8.GetBytes(("$SubscriptionId/$ResourceGroup").ToLowerInvariant())
        return [BitConverter]::ToString($sha.ComputeHash($bytes)).Replace('-', '').Substring(0, 10).ToLowerInvariant()
    } finally { $sha.Dispose() }
}

function Assert-Account([string]$ExpectedSub, [string]$ExpectedTenant) {
    $account = Invoke-AzJson @('account', 'show')
    if ($account.id -ine $ExpectedSub -or $account.tenantId -ine $ExpectedTenant) {
        throw 'Azure CLI subscription/tenant changed during install; stopping for safety.'
    }
}

try {
    Write-Host 'CloudLens guided installation (short mode).' -ForegroundColor Cyan
    Write-Host 'You will choose from menus and only type values where required.'
    Write-Host 'Use CANCEL at any prompt to stop.'

    foreach ($command in @('az', 'python')) {
        if (-not (Get-Command $command -ErrorAction SilentlyContinue)) {
            throw "Install prerequisite $command, then rerun."
        }
    }
    Invoke-Checked python @('-c', 'import sys; sys.exit(0 if sys.version_info >= (3,11) else 1)') | Out-Null
    Invoke-Checked az @('version', '-o', 'none') | Out-Null
    Invoke-Checked az @('bicep', 'version') | Out-Null

    $deploy = Join-Path $PSScriptRoot 'infra\deploy.ps1'
    if (-not (Test-Path -LiteralPath $deploy -PathType Leaf)) { throw 'Extract the complete CloudLens package first.' }

    $current = Invoke-AzJson @('account', 'show')
    if (-not $current -or -not $current.id) {
        Write-Host 'Azure sign-in required. Opening az login...'
        Invoke-Checked az @('login') | Out-Null
        $current = Invoke-AzJson @('account', 'show')
    }

    $enabled = @(
        Invoke-AzJson @('account', 'list', '--all', '--query',
            "[?state=='Enabled' && tenantId=='$($current.tenantId)'].{name:name,id:id,isDefault:isDefault}") |
        Where-Object { $_ -and $_.id }
    )
    if (-not $enabled.Count) {
        # Some constrained shells/policies suppress account-list responses; keep going with the
        # active subscription rather than failing the guided flow.
        $enabled = @([pscustomobject]@{
                name = $current.name
                id = $current.id
                isDefault = $true
            })
    }
    $subChoices = @($enabled | Sort-Object name | ForEach-Object {
            [pscustomobject]@{ Label = "$($_.name) ($($_.id))"; Value = $_.id }
        })
    $defaultSubIndex = [array]::FindIndex($subChoices, [Predicate[object]]{
            param($x) $x.Value -ieq $current.id
        }) + 1
    if ($defaultSubIndex -lt 1) { $defaultSubIndex = 1 }
    $subscription = Read-Choice 'Choose hosting subscription:' $subChoices $defaultSubIndex

    Invoke-Checked az @('account', 'set', '--subscription', $subscription) | Out-Null
    $account = Invoke-AzJson @('account', 'show')
    $tenant = $account.tenantId
    Assert-Account $subscription $tenant

    $size = Read-Choice 'Choose estate size:' @(
        @{ Label = 'Small (lower cost, lighter estates)'; Value = 'small' }
        @{ Label = 'Medium (recommended)'; Value = 'medium' }
        @{ Label = 'Large (heavier estates)'; Value = 'large' }
    ) 2
    $sizeProfile = @{
        small = @{ PostgresSku = 'Standard_D2ds_v5'; AppServicePlanSku = 'B1'; ModelCapacity = 20 }
        medium = @{ PostgresSku = 'Standard_D2ds_v5'; AppServicePlanSku = 'B2'; ModelCapacity = 30 }
        large = @{ PostgresSku = 'Standard_E4ds_v5'; AppServicePlanSku = 'B2'; ModelCapacity = 50 }
    }

    $location = Read-Choice 'Choose app/database region:' @(
        @{ Label = 'Central India (centralindia)'; Value = 'centralindia' }
        @{ Label = 'South India (southindia)'; Value = 'southindia' }
        @{ Label = 'Southeast Asia (southeastasia)'; Value = 'southeastasia' }
        @{ Label = 'West Europe (westeurope)'; Value = 'westeurope' }
        @{ Label = 'East US (eastus)'; Value = 'eastus' }
    ) 1
    $aiLocation = Read-Choice 'Choose AI region:' @(
        @{ Label = 'East US 2 (eastus2)'; Value = 'eastus2' }
        @{ Label = 'Sweden Central (swedencentral)'; Value = 'swedencentral' }
        @{ Label = 'West Europe (westeurope)'; Value = 'westeurope' }
    ) 1

    $scopeMode = Read-Choice 'Choose monitoring scope:' @(
        @{ Label = 'All enabled subscriptions in this tenant (recommended)'; Value = 'all' }
        @{ Label = 'Hosting subscription only'; Value = 'hosting-only' }
        @{ Label = 'Custom list of subscription IDs'; Value = 'custom' }
    ) 1

    $accessMode = Read-Choice 'Choose Azure access mode:' @(
        @{ Label = 'Delegated user access (recommended, auto-picks new user subscription access)'; Value = 'delegated' }
        @{ Label = 'App identity only (managed identity, fixed to app RBAC grants)'; Value = 'app-identity' }
    ) 1

    $defaultResourceGroup = ('rg-cloudlens-{0}' -f (Get-Date -Format 'yyyyMMdd-HHmm')).ToLowerInvariant()
    $rgMode = Read-Choice 'Choose resource-group name:' @(
        @{ Label = "Use recommended name ($defaultResourceGroup)"; Value = 'recommended' }
        @{ Label = 'Type custom name'; Value = 'custom' }
    ) 1
    $resourceGroup = if ($rgMode -eq 'custom') {
        Read-Input 'Resource group name' '^[A-Za-z0-9_-]{1,80}$' $defaultResourceGroup
    } else {
        $defaultResourceGroup
    }

    $entraClientId = ''
    if ($accessMode -eq 'delegated') {
        $entraClientId = Read-Input 'Preapproved single-tenant Entra app client ID' $GuidPattern
        Write-Host 'Delegated mode selected: after deployment, new user subscription access appears automatically.' -ForegroundColor Green
    } else {
        Write-Warning 'App-identity mode selected: post-install subscription visibility is capped by app managed-identity RBAC.'
        Write-Warning 'New user access on a subscription will not appear until Reader + Cost Management Reader are granted to the app identity there.'
        $appRegistrationMode = Read-Choice 'Sign-in app registration for local-user login:' @(
            @{ Label = 'Auto-create during deployment (recommended for first-time install)'; Value = 'auto-create' }
            @{ Label = 'Use precreated app client ID'; Value = 'precreated' }
        ) 1
        if ($appRegistrationMode -eq 'precreated') {
            $entraClientId = Read-Input 'Preapproved single-tenant Entra app client ID' $GuidPattern
        }
    }
    $defaultAdmin = if ($account.user -and $account.user.name) { $account.user.name } else { '' }
    $adminsRaw = Read-Input 'Admin sign-in addresses (comma separated)' '^[^@\s,]+@[^@\s,]+(,[^@\s,]+@[^@\s,]+)*$' $defaultAdmin
    $admins = @($adminsRaw.Split(',').Trim() | Where-Object { $_ })
    if (-not $admins.Count) { throw 'At least one admin sign-in address is required.' }

    $costScopes = @()
    $useAllVisible = $false
    switch ($scopeMode) {
        'all' { $useAllVisible = $true }
        'hosting-only' { $costScopes = @($subscription) }
        'custom' {
            $raw = Read-Input 'Cost scope subscription IDs (comma separated)' '^.+$'
            $costScopes = @($raw.Split(',').Trim() | Where-Object { $_ })
            if (@($costScopes | Where-Object { $_ -notmatch $GuidPattern }).Count) {
                throw 'Each custom scope must be a subscription GUID.'
            }
        }
    }

    $nameSuffix = Get-StableSuffix $subscription $resourceGroup
    $parameters = @{
        Location = $location
        AiLocation = $aiLocation
        ResourceGroup = $resourceGroup
        NameSuffix = $nameSuffix
        ModelName = 'gpt-4.1-mini'
        ModelSku = 'GlobalStandard'
        ModelCapacity = $sizeProfile[$size].ModelCapacity
        PostgresSku = $sizeProfile[$size].PostgresSku
        AppServicePlanSku = $sizeProfile[$size].AppServicePlanSku
        VnetAddressPrefix = '10.70.0.0/16'
        AppSubnetAddressPrefix = '10.70.1.0/24'
        PostgresSubnetAddressPrefix = '10.70.2.0/24'
        PrivateEndpointSubnetAddressPrefix = '10.70.3.0/24'
        NetworkIsolation = $true
        PostgresSkuFallback = @()
        Admins = $admins
    }
    if ($accessMode -eq 'delegated') {
        $parameters.EntraClientId = $entraClientId
        $parameters.DelegatedArm = $true
    } else {
        if ($entraClientId) {
            $parameters.EntraClientId = $entraClientId
        } else {
            $parameters.EntraAppUniqueName = "cloudlens-$nameSuffix"
        }
        $parameters.AllowAppIdentityFallback = $true
    }
    if ($useAllVisible) {
        $parameters.CostScopeAllVisible = $true
    } else {
        $parameters.CostScopeSubscriptions = $costScopes
    }

    Write-Host ''
    Write-Host "Required callback URI: https://cloudlens-web-$nameSuffix.azurewebsites.net/auth/callback"
    Write-Host 'Effective deployment parameters:' -ForegroundColor Cyan
    @{
        Tenant = $tenant
        Subscription = $subscription
        ScopeMode = $scopeMode
        AccessMode = $accessMode
        Parameters = $parameters
    } | ConvertTo-Json -Depth 6 | Write-Host

    $runMode = Read-Choice 'Choose run mode:' @(
        @{ Label = 'Preview only (What-If)'; Value = 'preview' }
        @{ Label = 'Preview, then deploy'; Value = 'deploy' }
    ) 1

    Invoke-Checked az @('login', '--tenant', $tenant, '-o', 'none') | Out-Null
    Invoke-Checked az @('account', 'set', '--subscription', $subscription) | Out-Null
    Assert-Account $subscription $tenant

    Push-Location $PSScriptRoot
    try {
        Write-Host ''
        Write-Host 'Running preview...' -ForegroundColor Cyan
        $global:LASTEXITCODE = 0
        & $deploy @parameters -WhatIf
        if (-not $? -or $LASTEXITCODE -ne 0) { throw 'Preview failed. Deployment blocked.' }

        if ($runMode -eq 'deploy') {
            $go = Read-Choice 'Proceed to deployment now?' @(
                @{ Label = 'Yes, deploy now'; Value = 'yes' }
                @{ Label = 'No, stop after preview'; Value = 'no' }
            ) 2
            if ($go -eq 'yes') {
                Assert-Account $subscription $tenant
                Write-Host ''
                Write-Host 'Running deployment...' -ForegroundColor Cyan
                $global:LASTEXITCODE = 0
                & $deploy @parameters
                if (-not $? -or $LASTEXITCODE -ne 0) { throw 'Deployment incomplete. Review errors.' }
                Write-Host 'Deployment command completed. Complete READINESS.md live checks.'
            } else {
                Write-Host 'Stopped after successful preview.'
            }
        } else {
            Write-Host 'Preview completed successfully.'
        }
    } finally {
        Pop-Location
    }
} catch {
    Write-Error $_
    exit 1
}
