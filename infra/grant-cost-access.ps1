<#
.SYNOPSIS
    Grant or review cost access for an existing App Service's active managed identity.
.DESCRIPTION
    No infrastructure, app registration, storage or app settings are changed. Additional
    subscriptions must be selected explicitly; the default is the hosting subscription.
    -InstructionsOnly and -WhatIf perform reads and write a local administrator handoff only.
#>
[CmdletBinding(SupportsShouldProcess)]
param(
    [Parameter(Mandatory)][string]$ResourceGroup,
    [Parameter(Mandatory)][string]$AppName,
    [string]$HostingSubscriptionId = '',
    [string[]]$CostScopeSubscriptions = @(),
    [switch]$CostScopeAllVisible,
    [switch]$EnableExportManagement,
    [switch]$InstructionsOnly,
    [string]$ReportPath = ''
)
$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'cost-access.ps1')
$readOnly = $InstructionsOnly -or -not $PSCmdlet.ShouldProcess(
    "$ResourceGroup/$AppName runtime managed identity", 'Grant selected subscription cost roles'
)
Invoke-CloudLensCostAccess -HostingSubscriptionId $HostingSubscriptionId -ResourceGroup $ResourceGroup `
    -AppName $AppName -CostScopeSubscriptions $CostScopeSubscriptions -CostScopeAllVisible:$CostScopeAllVisible `
    -EnableExportManagement:$EnableExportManagement -InstructionsOnly:$readOnly -ReportPath $ReportPath
