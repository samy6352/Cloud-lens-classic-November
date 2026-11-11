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
    $deploy = Join-Path $PSScriptRoot 'infra\deploy.ps1'
    if (-not (Test-Path -LiteralPath $deploy -PathType Leaf)) { throw 'Extract the entire customer ZIP first.' }
    $guid = '^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$'
    $tenant = Read-Approved 'Approved tenant ID' $guid
    $subscription = Read-Approved 'Approved hosting subscription ID' $guid
    $parameters = @{
        Location = Read-Approved 'Approved app/database region' '^[a-z0-9]+$'
        AiLocation = Read-Approved 'Approved AI region (no residency assumption)' '^[a-z0-9]+$'
        ResourceGroup = Read-Approved 'Approved resource group' '^[\w().-]+$'
        NameSuffix = Read-Approved 'Approved deployment suffix' '^[a-z0-9]{1,20}$'
        EntraClientId = Read-Approved 'Preapproved single-tenant public-client application ID' $guid
        ModelName = Read-Approved 'Approved AI model name'
        ModelSku = Read-Approved 'Approved AI model SKU (GlobalStandard is not a residency promise)'
        ModelCapacity = [int](Read-Approved 'Approved AI capacity (quota must be verified)' '^[1-9][0-9]{0,6}$')
        PostgresSku = Read-Approved 'Approved PostgreSQL SKU'
        AppServicePlanSku = Read-Approved 'Approved App Service plan SKU'
        VnetAddressPrefix = Read-Approved 'Approved VNet CIDR'
        AppSubnetAddressPrefix = Read-Approved 'Approved app subnet CIDR'
        PostgresSubnetAddressPrefix = Read-Approved 'Approved PostgreSQL subnet CIDR'
        PrivateEndpointSubnetAddressPrefix = Read-Approved 'Approved private endpoint subnet CIDR'
        NetworkIsolation = $true
        DelegatedArm = $true
        PostgresSkuFallback = @()
    }
    $parameters.Admins = @((Read-Approved 'Approved admin sign-in addresses, comma separated').Split(',').Trim())
    if (@($parameters.Admins | Where-Object { $_ -notmatch '^[^@\s,]+@[^@\s,]+$' }).Count) {
        throw 'Every admin must be an approved sign-in address.'
    }
    $parameters.CostScopeSubscriptions = @((Read-Approved 'Approved cost subscription IDs, comma separated').Split(',').Trim())
    if (@($parameters.CostScopeSubscriptions | Where-Object { $_ -notmatch $guid }).Count) {
        throw 'Every cost scope must be an explicit approved subscription GUID.'
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
