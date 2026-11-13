#requires -Version 7.0
<#
.SYNOPSIS
  Customer bootstrap entrypoint for CloudLens guided installation.

.DESCRIPTION
  Downloads the latest repository snapshot into a temporary folder and launches
  the guided installer (install.ps1). This script is intended to be run via:

    irm https://raw.githubusercontent.com/samy6352/Cloud-lens-classic-November/main/tools/install-customer.ps1 | iex
#>
[CmdletBinding()]
param()

$ErrorActionPreference = 'Stop'

function Step([string]$Message) {
    Write-Host "`n==> $Message" -ForegroundColor Cyan
}

function Require-Command([string]$Name, [string]$Hint) {
    if (-not (Get-Command $Name -ErrorAction SilentlyContinue)) {
        throw "$Name is required. $Hint"
    }
}

Step 'Checking prerequisites'
Require-Command 'git' 'Install Git for Windows and retry.'
Require-Command 'pwsh' 'Install PowerShell 7 and retry.'

$tempRoot = Join-Path $env:TEMP ("cloudlens-customer-" + [guid]::NewGuid().ToString("N"))
$repoUrl = 'https://github.com/samy6352/Cloud-lens-classic-November.git'

Step 'Downloading installer source'
git clone --depth 1 $repoUrl $tempRoot | Out-Null
if ($LASTEXITCODE -ne 0) {
    throw 'Git clone failed. Check internet access and repository permissions.'
}

$installer = Join-Path $tempRoot 'install.ps1'
if (-not (Test-Path -LiteralPath $installer -PathType Leaf)) {
    throw "install.ps1 was not found at $installer"
}

Write-Host ''
Write-Host 'Launching CloudLens guided installer...' -ForegroundColor Green
Write-Host "Temporary source path: $tempRoot"

pwsh -NoProfile -ExecutionPolicy Bypass -File $installer
$code = $LASTEXITCODE
if ($code -ne 0) {
    throw "Guided installer exited with code $code."
}

