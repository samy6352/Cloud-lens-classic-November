#requires -Version 7.0
<#
.SYNOPSIS
    Guided customer installation from the fully extracted, verified release ZIP.
#>
[CmdletBinding()]
param()

$ErrorActionPreference = 'Stop'

function Read-Approved([string]$Prompt, [string]$Pattern = '.+') {
    $value = (Read-Host "$Prompt (CANCEL to stop)").Trim()
    if ($value -ceq 'CANCEL') { throw 'Installation cancelled; no further actions will run.' }
    if ($value -notmatch $Pattern) { throw "Invalid or missing approved value: $Prompt" }
    return $value
}

function Read-ApprovedWithDefault([string]$Prompt, [string]$Pattern, [string]$DefaultValue) {
    $label = if ($DefaultValue) { "$Prompt [$DefaultValue]" } else { $Prompt }
    $value = (Read-Host "$label (CANCEL to stop)").Trim()
    if ($value -ceq 'CANCEL') { throw 'Installation cancelled; no further actions will run.' }
    if (-not $value) { $value = $DefaultValue }
    if ($value -notmatch $Pattern) { throw "Invalid or missing approved value: $Prompt" }
    return $value
}

function Read-ApprovedChoice([string]$Prompt, [string[]]$AllowedValues) {
    $pattern = '(?i)^(' + (($AllowedValues | ForEach-Object { [regex]::Escape($_) }) -join '|') + ')$'
    $value = Read-Approved $Prompt $pattern
    return $value.ToUpperInvariant()
}

function Invoke-Checked([string]$Command, [string[]]$Arguments) {
    $global:LASTEXITCODE = 0
    $result = & $Command @Arguments
    if ($LASTEXITCODE -ne 0) { throw "$Command failed (exit $LASTEXITCODE). Stopping; no fallback." }
    return $result
}

function Assert-Account {
    $account = (Invoke-Checked az @('account', 'show', '-o', 'json')) | ConvertFrom-Json
    if ($account.id -ne $subscription -or $account.tenantId -ne $tenant) {
        throw 'Azure CLI tenant/subscription does not match the approved inputs.'
    }
}

try {
    Write-Host 'CloudLens guided installation. Read READINESS.md first. Ctrl+C or CANCEL stops.'
    Write-Host 'No dependencies are installed automatically. No region, quota or SKU fallback is allowed.'
    foreach ($command in @('az', 'python')) {
        if (-not (Get-Command $command -ErrorAction SilentlyContinue)) {
            throw "Install prerequisite $command, then rerun. Requires Azure CLI with Bicep and Python 3.11+."
        }
    }
    Invoke-Checked python @('-c', 'import sys; sys.exit(0 if sys.version_info >= (3,11) else 1)') | Out-Null
    Invoke-Checked az @('version', '-o', 'none') | Out-Null
    Invoke-Checked az @('bicep', 'version') | Out-Null
    $account = (Invoke-Checked az @('account', 'show', '-o', 'json')) | ConvertFrom-Json
    $suggestedTenant = [string]$account.tenantId
    $suggestedSubscription = [string]$account.id
    $suggestedAdmins = @()
    try {
        $signedIn = (Invoke-Checked az @('ad', 'signed-in-user', 'show', '-o', 'json')) | ConvertFrom-Json
        if ($signedIn.mail) { $suggestedAdmins += $signedIn.mail }
        if ($signedIn.userPrincipalName) { $suggestedAdmins += $signedIn.userPrincipalName }
    } catch {}
    $suggestedAdminCsv = ($suggestedAdmins | Where-Object { $_ } | Select-Object -Unique) -join ','
    $deploy = Join-Path $PSScriptRoot 'infra\deploy.ps1'
    if (-not (Test-Path -LiteralPath $deploy -PathType Leaf)) { throw 'Extract the entire customer ZIP first.' }
    $guid = '^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$'
    $profile = Read-ApprovedChoice 'Install profile (QUICK for defaults, CUSTOM for full input)' @('QUICK', 'CUSTOM')
    $tenant = if ($profile -eq 'QUICK') { Read-ApprovedWithDefault 'Approved tenant ID' $guid $suggestedTenant } else { Read-Approved 'Approved tenant ID' $guid }
    $subscription = if ($profile -eq 'QUICK') { Read-ApprovedWithDefault 'Approved hosting subscription ID' $guid $suggestedSubscription } else { Read-Approved 'Approved hosting subscription ID' $guid }
    $accessMode = Read-ApprovedChoice `
        'Access mode (DELEGATED for preapproved ARM consent, MEGHKOSH for managed-identity role handoff only)' `
        @('DELEGATED', 'MEGHKOSH')
    $delegatedArm = ($accessMode -eq 'DELEGATED')
    $defaults = @{
        Location = 'centralindia'
        AiLocation = 'eastus2'
        ModelName = 'gpt-5-mini'
        ModelSku = 'GlobalStandard'
        ModelCapacity = '30'
        PostgresSku = 'Standard_E2ds_v5'
        AppServicePlanSku = 'B2'
        VnetAddressPrefix = '10.70.0.0/16'
        AppSubnetAddressPrefix = '10.70.1.0/24'
        PostgresSubnetAddressPrefix = '10.70.2.0/24'
        PrivateEndpointSubnetAddressPrefix = '10.70.3.0/24'
    }
    $location = if ($profile -eq 'QUICK') { Read-ApprovedWithDefault 'Approved app/database region' '^[a-z0-9]+$' $defaults.Location } else { Read-Approved 'Approved app/database region' '^[a-z0-9]+$' }
    $aiLocation = if ($profile -eq 'QUICK') { Read-ApprovedWithDefault 'Approved AI region (no residency assumption)' '^[a-z0-9]+$' $defaults.AiLocation } else { Read-Approved 'Approved AI region (no residency assumption)' '^[a-z0-9]+$' }
    $modelName = if ($profile -eq 'QUICK') { Read-ApprovedWithDefault 'Approved AI model name' '.+' $defaults.ModelName } else { Read-Approved 'Approved AI model name' }
    $modelSku = if ($profile -eq 'QUICK') { Read-ApprovedWithDefault 'Approved AI model SKU (GlobalStandard is not a residency promise)' '.+' $defaults.ModelSku } else { Read-Approved 'Approved AI model SKU (GlobalStandard is not a residency promise)' }
    $modelCapacityRaw = if ($profile -eq 'QUICK') { Read-ApprovedWithDefault 'Approved AI capacity (quota must be verified)' '^[1-9][0-9]{0,6}$' $defaults.ModelCapacity } else { Read-Approved 'Approved AI capacity (quota must be verified)' '^[1-9][0-9]{0,6}$' }
    $postgresSku = if ($profile -eq 'QUICK') { Read-ApprovedWithDefault 'Approved PostgreSQL SKU' '.+' $defaults.PostgresSku } else { Read-Approved 'Approved PostgreSQL SKU' }
    $appServicePlanSku = if ($profile -eq 'QUICK') { Read-ApprovedWithDefault 'Approved App Service plan SKU' '.+' $defaults.AppServicePlanSku } else { Read-Approved 'Approved App Service plan SKU' }
    $vnetAddressPrefix = if ($profile -eq 'QUICK') { Read-ApprovedWithDefault 'Approved VNet CIDR' '.+' $defaults.VnetAddressPrefix } else { Read-Approved 'Approved VNet CIDR' }
    $appSubnetAddressPrefix = if ($profile -eq 'QUICK') { Read-ApprovedWithDefault 'Approved app subnet CIDR' '.+' $defaults.AppSubnetAddressPrefix } else { Read-Approved 'Approved app subnet CIDR' }
    $postgresSubnetAddressPrefix = if ($profile -eq 'QUICK') { Read-ApprovedWithDefault 'Approved PostgreSQL subnet CIDR' '.+' $defaults.PostgresSubnetAddressPrefix } else { Read-Approved 'Approved PostgreSQL subnet CIDR' }
    $privateEndpointSubnetAddressPrefix = if ($profile -eq 'QUICK') { Read-ApprovedWithDefault 'Approved private endpoint subnet CIDR' '.+' $defaults.PrivateEndpointSubnetAddressPrefix } else { Read-Approved 'Approved private endpoint subnet CIDR' }
    $parameters = @{
        Location = $location
        AiLocation = $aiLocation
        ResourceGroup = Read-Approved 'Approved resource group' '^[\w().-]+$'
        NameSuffix = Read-Approved 'Approved deployment suffix (1-10 lowercase letters/numbers)' '^[a-z0-9]{1,10}$'
        EntraClientId = Read-Approved 'Preapproved single-tenant public-client application ID' $guid
        ModelName = $modelName
        ModelSku = $modelSku
        ModelCapacity = [int]$modelCapacityRaw
        PostgresSku = $postgresSku
        AppServicePlanSku = $appServicePlanSku
        VnetAddressPrefix = $vnetAddressPrefix
        AppSubnetAddressPrefix = $appSubnetAddressPrefix
        PostgresSubnetAddressPrefix = $postgresSubnetAddressPrefix
        PrivateEndpointSubnetAddressPrefix = $privateEndpointSubnetAddressPrefix
        NetworkIsolation = $true
        DelegatedArm = $delegatedArm
        PostgresSkuFallback = @()
    }
    if (-not $delegatedArm) { $parameters.AllowAppIdentityFallback = $true }
    $adminInput = if ($profile -eq 'QUICK' -and $suggestedAdminCsv) {
        Read-ApprovedWithDefault 'Approved admin sign-in addresses, comma separated' '.+' $suggestedAdminCsv
    } else {
        Read-Approved 'Approved admin sign-in addresses, comma separated'
    }
    $parameters.Admins = @($adminInput.Split(',').Trim())
    if (@($parameters.Admins | Where-Object { $_ -notmatch '^[^@\s,]+@[^@\s,]+$' }).Count) {
        throw 'Every admin must be an approved sign-in address.'
    }
    $scopeMode = Read-ApprovedChoice `
        'Cost scope mode (LIST for explicit subscription IDs, ALLVISIBLE for same-tenant sweep)' `
        @('LIST', 'ALLVISIBLE')
    if ($scopeMode -eq 'ALLVISIBLE') {
        $parameters.CostScopeAllVisible = $true
    } else {
        $scopeInput = if ($profile -eq 'QUICK') {
            Read-ApprovedWithDefault 'Approved cost subscription IDs, comma separated' '.+' $subscription
        } else {
            Read-Approved 'Approved cost subscription IDs, comma separated'
        }
        $parameters.CostScopeSubscriptions = @($scopeInput.Split(',').Trim())
        if (@($parameters.CostScopeSubscriptions | Where-Object { $_ -notmatch $guid }).Count) {
            throw 'Every cost scope must be an explicit approved subscription GUID.'
        }
    }
    Write-Host "Required preapproved callback: https://cloudlens-web-$($parameters.NameSuffix).azurewebsites.net/auth/callback"
    Write-Host 'Review ALL effective deployment parameters below, including defaults for storage, networking and ingestion:'
    $effective = @{}
    $metadata = Get-Command $deploy
    $ast = $metadata.ScriptBlock.Ast
    foreach ($parameter in $ast.ParamBlock.Parameters) {
        $name = $parameter.Name.VariablePath.UserPath
        $effective[$name] = if ($parameters.ContainsKey($name)) { $parameters[$name] }
            elseif ($null -ne $parameter.DefaultValue) { $parameter.DefaultValue.Extent.Text }
            else { '(unset)' }
    }
    @{ Tenant = $tenant; Subscription = $subscription; Parameters = $effective } |
        ConvertTo-Json -Depth 6 | Write-Host
    Write-Host 'Apply includes hosting/bootstrap RBAC, MI cost roles, FOCUS exports and code publish.'
    Write-Host 'No local login, identity fallback, preflight bypass or automatic consent is offered.'
    if ((Read-Approved 'Type PREVIEW to approve sign-in and read-only preflight/What-If') -cne 'PREVIEW') {
        throw 'Preview not approved.'
    }
    Invoke-Checked az @('login', '--tenant', $tenant, '-o', 'none') | Out-Null
    Invoke-Checked az @('account', 'set', '--subscription', $subscription) | Out-Null
    Assert-Account
    Push-Location $PSScriptRoot
    try {
        $global:LASTEXITCODE = 0
        & $deploy @parameters -WhatIf
        if (-not $? -or $LASTEXITCODE -ne 0) { throw 'Preview failed. Deployment blocked.' }
        Write-Host 'Resolve every warning with its owner; verify quota, costs, residency, exact consent and change approval.'
        if ((Read-Approved 'Only after reviewing the successful preview, type DEPLOY to provision') -cne 'DEPLOY') {
            throw 'Deployment not approved.'
        }
        Assert-Account
        $global:LASTEXITCODE = 0
        & $deploy @parameters
        if (-not $? -or $LASTEXITCODE -ne 0) { throw 'Deployment incomplete. Review errors; do not assume acceptance.' }
        Write-Host 'Deployment command completed. Customer acceptance is still pending: complete READINESS.md live checks.'
    } finally { Pop-Location }
} catch {
    Write-Error $_
    exit 1
}
