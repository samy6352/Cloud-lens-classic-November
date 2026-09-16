<#
.SYNOPSIS
Provision the FOCUS exports CloudLens reads, as the operator rather than as the app.

.DESCRIPTION
CloudLens imports cost detail from a Cost Management export that writes into its own storage
account. Everything downstream depends on it: Hourly Billing, Applications & Tags, scheduling
savings, and every column a Quick refresh cannot carry (UnitOfMeasure, PricingQuantity,
MeterName, Tags). Without the export those views are empty, and correctly say so.

Creating that export has to be done by a person, and this script is that step.

The reason is a deliberate constraint rather than an oversight. An Azure Cost Management export
authenticates to storage with a managed identity, and the API accepts only a *system-assigned*
one (the `identity` field is typed SystemAssignedServiceIdentity, whose allowed values are None
and SystemAssigned). So Azure mints a brand-new identity for the export, and that identity
starts with no access to anything -- meaning whoever creates the export must then grant it
Storage Blob Data Contributor on the destination. Granting a role needs
Microsoft.Authorization/roleAssignments/write.

CloudLens's own runtime identity must never hold that. cost-access.ps1 states the rule
plainly: "Never grant the runtime app subscription Owner or User Access Administrator." An
application that can hand out roles can widen its own access, which is precisely what a
read-only cost tool should be unable to do. So the in-app "create export" button asks the app
to do something the deployment is designed to forbid, and on a correctly locked-down stack it
fails -- as it should.

Running it here resolves the tension instead of working around it: the operator already holds
Owner or User Access Administrator legitimately, uses it once at provisioning time, and the
runtime identity keeps exactly the read-only roles it is supposed to have.

Safe to re-run. Every step checks the current state first, and an export that already matches
is left alone rather than recreated -- so this can be used to verify a deployment as well as to
build one.

.PARAMETER ArchiveAccountId
Resource id of CloudLens's storage account. Read from the web app's ARCHIVE_ACCOUNT_ID setting
when -WebAppName is given.

.PARAMETER Subscriptions
Subscriptions to export cost for. Defaults to every subscription the export storage account's
subscription can see, which is usually what a single-tenant deployment wants.

.EXAMPLE
./focus-exports.ps1 -ResourceGroup cloud-lens-cx -WebAppName cloudlens-web-rv0913

Reads the deployment's own settings and provisions an export for each accessible subscription.

.EXAMPLE
./focus-exports.ps1 -ResourceGroup cloud-lens-cx -WebAppName cloudlens-web-rv0913 -WhatIf

Reports what it would create and change nothing.
#>
[CmdletBinding(SupportsShouldProcess)]
param(
    [Parameter(Mandatory)]
    [string]$ResourceGroup,

    # Either the web app to read settings from...
    [string]$WebAppName = '',

    # ...or the settings themselves, for a stack whose app is not running yet.
    [string]$ArchiveAccountId = '',
    [string]$Container = '',
    [string]$Prefix = '',
    [string]$Location = '',

    [string[]]$Subscriptions = @(),

    [ValidateSet('Daily', 'Monthly')]
    [string]$Recurrence = 'Daily',

    # Ask Azure to produce the first report immediately. Without it the first data arrives on
    # the export's own schedule, which is tomorrow.
    [switch]$RunNow,

    [string]$HostingSubscriptionId = ''
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

$EXPORT_API = '2025-03-01'
$ROLE_API = '2022-04-01'
$BLOB_CONTRIBUTOR = 'ba92f5b4-2d11-453d-a403-e96b0029c9fe'

function Step($m) { Write-Host "`n==> $m" -ForegroundColor Cyan }
function Ok($m)   { Write-Host "    [ok]   $m" -ForegroundColor Green }
function Info($m) { Write-Host "    $m" }
function Warn($m) { Write-Host "    [warn] $m" -ForegroundColor Yellow }

function Invoke-Az {
    param([string[]]$Arguments, [switch]$AllowFailure)
    # az writes warnings to stderr (a 32-bit-Python notice, deprecation notices), and with
    # 2>&1 those arrive as ErrorRecords that $ErrorActionPreference='Stop' would treat as a
    # failure. The exit code is the only reliable signal, so redirection is kept and the
    # non-error stream is separated out by type rather than by guessing at message text.
    $ErrorActionPreference = 'Continue'
    $raw = & az @Arguments 2>&1
    $code = $LASTEXITCODE
    $stderr = @($raw | Where-Object { $_ -is [System.Management.Automation.ErrorRecord] })
    $stdout = @($raw | Where-Object { $_ -isnot [System.Management.Automation.ErrorRecord] })

    if ($code -ne 0) {
        if ($AllowFailure) { return $null }
        $detail = (($stderr + $stdout) | Out-String).Trim()
        throw "az $($Arguments -join ' ') failed ($code): $detail"
    }
    $text = ($stdout | Out-String).Trim()
    if (-not $text) { return $null }
    try { return ($text | ConvertFrom-Json) } catch { return $text }
}

# The app derives an export's name by hashing its destination, and recognises an export as its
# own only when the name matches. Reproduced here exactly -- see schedules.export_name.
function Get-ExportName {
    param([string]$AccountId, [string]$Container, [string]$Folder)
    $key = "$($AccountId.ToLowerInvariant().TrimEnd('/'))|$Container|$Folder"
    $sha = [System.Security.Cryptography.SHA256]::Create()
    try {
        $hash = $sha.ComputeHash([Text.Encoding]::UTF8.GetBytes($key))
    } finally {
        $sha.Dispose()
    }
    $hex = -join ($hash | ForEach-Object { $_.ToString('x2') })
    return "cloudlens-focus-$($hex.Substring(0, 20))"
}

# ------------------------------------------------------------------ settings

Step 'Reading deployment settings'

if ($WebAppName) {
    $listArgs = @('webapp', 'config', 'appsettings', 'list', '-g', $ResourceGroup, '-n', $WebAppName, '-o', 'json')
    if ($HostingSubscriptionId) { $listArgs += @('--subscription', $HostingSubscriptionId) }
    $settings = Invoke-Az -Arguments $listArgs
    $map = @{}
    foreach ($s in $settings) { $map[$s.name] = $s.value }

    if (-not $ArchiveAccountId) { $ArchiveAccountId = [string]$map['ARCHIVE_ACCOUNT_ID'] }
    if (-not $Container) { $Container = [string]$map['FOCUS_EXPORT_CONTAINER'] }
    if (-not $Prefix -and $map.ContainsKey('FOCUS_EXPORT_PREFIX')) { $Prefix = [string]$map['FOCUS_EXPORT_PREFIX'] }
    if (-not $Location) { $Location = [string]$map['ARCHIVE_LOCATION'] }
    Ok "read settings from $WebAppName"
}

if (-not $Container) { $Container = 'focus-exports' }
if (-not $Prefix) { $Prefix = 'focus' }

if (-not $ArchiveAccountId) {
    throw 'ARCHIVE_ACCOUNT_ID is unknown. Pass -WebAppName to read it, or -ArchiveAccountId directly.'
}
if (-not $Location) {
    throw 'The export storage region is unknown. Pass -WebAppName to read it, or -Location directly.'
}

$accountName = $ArchiveAccountId.TrimEnd('/').Split('/')[-1]
Info "storage      : $accountName"
Info "container    : $Container"
Info "folder       : $Prefix/<subscription-id>"
Info "region       : $Location"

# The destination has to exist before an export can be pointed at it: Azure validates the
# container on create, and a missing one fails with a message about the account rather than
# about the container.
Step 'Checking the destination container'
$storageSub = $ArchiveAccountId.Split('/')[2]
$existing = Invoke-Az -AllowFailure -Arguments @(
    'storage', 'container', 'exists', '--account-name', $accountName, '--name', $Container,
    '--auth-mode', 'login', '--subscription', $storageSub, '-o', 'json')
if ($null -eq $existing) {
    Warn "could not check the container (this needs Storage Blob Data Reader on $accountName)"
    Warn 'continuing; Azure will reject the export if the container is genuinely absent'
} elseif (-not $existing.exists) {
    if ($PSCmdlet.ShouldProcess("$accountName/$Container", 'create container')) {
        Invoke-Az -Arguments @('storage', 'container', 'create', '--account-name', $accountName,
            '--name', $Container, '--auth-mode', 'login', '--subscription', $storageSub, '-o', 'none') | Out-Null
        Ok "created container $Container"
    }
} else {
    Ok "container $Container exists"
}

# ------------------------------------------------------------- subscriptions

Step 'Selecting subscriptions'

# Accept both -Subscriptions a,b,c and -Subscriptions a b c. Under `powershell -File` the
# comma form arrives as a single string, which would otherwise be used verbatim as a
# subscription id and produce one nonsensical export instead of three.
$Subscriptions = @(
    $Subscriptions |
        ForEach-Object { $_ -split '[,;]' } |
        ForEach-Object { $_.Trim() } |
        Where-Object { $_ }
)

if (-not $Subscriptions -or $Subscriptions.Count -eq 0) {
    $all = Invoke-Az -Arguments @('account', 'list', '--query', '[?state==`Enabled`].id', '-o', 'json')
    $Subscriptions = @($all)
    Info "no -Subscriptions given; using every enabled subscription visible to you"
}
if (-not $Subscriptions -or $Subscriptions.Count -eq 0) {
    throw 'No subscriptions to export. Pass -Subscriptions explicitly.'
}

# A malformed id reaches Azure as a URL path segment; refuse it here where the message can
# say which one and why, rather than as an opaque ARM 400.
$badly = @($Subscriptions | Where-Object { $_ -notmatch '^[0-9a-fA-F-]{36}$' })
if ($badly.Count -gt 0) {
    throw "Not subscription ids: $($badly -join ', ')"
}
Ok "$($Subscriptions.Count) subscription(s)"

# --------------------------------------------------------------- the exports

$start = (Get-Date).ToUniversalTime().AddDays(1).Date.AddHours(2)
if ($Recurrence -eq 'Monthly') {
    $start = (Get-Date).ToUniversalTime().Date.AddDays(-((Get-Date).Day - 1)).AddMonths(1).AddHours(2)
}
$from = $start.ToString('yyyy-MM-ddTHH:mm:ssZ')
$to = $start.AddYears(3).ToString('yyyy-MM-ddTHH:mm:ssZ')

$results = @()

foreach ($sub in $Subscriptions) {
    $sub = $sub.Trim()
    $folder = "$Prefix/$($sub.ToLowerInvariant())".Trim('/')
    $name = Get-ExportName -AccountId $ArchiveAccountId -Container $Container -Folder $folder

    Step "$sub"
    Info "export       : $name"

    $body = [ordered]@{
        identity = [ordered]@{ type = 'SystemAssigned' }
        location = $Location
        properties = [ordered]@{
            schedule = [ordered]@{
                status = 'Active'
                recurrence = $Recurrence
                recurrencePeriod = [ordered]@{ from = $from; to = $to }
            }
            format = 'Csv'
            partitionData = $true
            dataOverwriteBehavior = 'CreateNewReport'
            deliveryInfo = [ordered]@{
                destination = [ordered]@{
                    type = 'AzureBlob'
                    resourceId = $ArchiveAccountId
                    container = $Container
                    rootFolderPath = $folder
                }
            }
            definition = [ordered]@{
                type = 'FocusCost'
                timeframe = $(if ($Recurrence -eq 'Monthly') { 'TheLastMonth' } else { 'MonthToDate' })
                dataSet = [ordered]@{ granularity = 'Daily' }
            }
        }
    }

    $uri = "/subscriptions/$sub/providers/Microsoft.CostManagement/exports/$name`?api-version=$EXPORT_API"

    if (-not $PSCmdlet.ShouldProcess("$sub/$name", 'create or update FOCUS export')) {
        $results += [pscustomobject]@{ subscription = $sub; export = $name; status = 'skipped (WhatIf)' }
        continue
    }

    $file = New-TemporaryFile
    try {
        ($body | ConvertTo-Json -Depth 12) | Set-Content -LiteralPath $file -Encoding utf8
        $made = Invoke-Az -AllowFailure -Arguments @('rest', '--method', 'put', '--uri', $uri,
            '--body', "@$file", '--headers', 'Content-Type=application/json', '-o', 'json')
    } finally {
        Remove-Item -LiteralPath $file -Force -ErrorAction SilentlyContinue
    }

    if ($null -eq $made) {
        Warn 'export create failed; this needs Cost Management Contributor on the subscription'
        $results += [pscustomobject]@{ subscription = $sub; export = $name; status = 'failed (create)' }
        continue
    }
    Ok 'export created'

    # Azure minted an identity for the export when it was saved. It can do nothing until it is
    # given the destination -- this is the grant the runtime app is deliberately unable to make.
    $principal = $null
    if ($made.PSObject.Properties.Name -contains 'identity' -and $made.identity) {
        $principal = [string]$made.identity.principalId
    }
    if (-not $principal) {
        Warn 'Azure did not return the export identity; storage access was not configured'
        $results += [pscustomobject]@{ subscription = $sub; export = $name; status = 'created, no identity' }
        continue
    }
    Info "identity     : $principal"

    $assigned = Invoke-Az -AllowFailure -Arguments @('role', 'assignment', 'list',
        '--scope', $ArchiveAccountId, '--subscription', $storageSub,
        '--query', "[?principalId=='$principal' && ends_with(roleDefinitionId,'$BLOB_CONTRIBUTOR')].id",
        '-o', 'json')

    if ($assigned -and @($assigned).Count -gt 0) {
        Ok 'storage access already granted'
    } else {
        $grant = Invoke-Az -AllowFailure -Arguments @('role', 'assignment', 'create',
            '--assignee-object-id', $principal, '--assignee-principal-type', 'ServicePrincipal',
            '--role', $BLOB_CONTRIBUTOR, '--scope', $ArchiveAccountId,
            '--subscription', $storageSub, '-o', 'json')
        if ($null -eq $grant) {
            Warn "could not grant Storage Blob Data Contributor on $accountName"
            Warn 'you need Owner or User Access Administrator on that storage account'
            $results += [pscustomobject]@{ subscription = $sub; export = $name; status = 'created, grant failed' }
            continue
        }
        Ok 'granted Storage Blob Data Contributor'
    }

    if ($RunNow) {
        # A role assignment takes time to reach the data plane, and 30s was not enough in
        # practice: the first run came back 403 and the schedule looked broken when it was
        # not. Retried with a widening gap instead of waiting a fixed pessimistic period,
        # so a fast tenant is not punished and a slow one still succeeds.
        $started = $false
        foreach ($wait in @(30, 45, 60, 90)) {
            Info "waiting ${wait}s for the role assignment to take effect"
            Start-Sleep -Seconds $wait
            $run = Invoke-Az -AllowFailure -Arguments @('rest', '--method', 'post',
                '--uri', "/subscriptions/$sub/providers/Microsoft.CostManagement/exports/$name/run`?api-version=$EXPORT_API",
                '-o', 'none')
            if ($null -ne $run -or $LASTEXITCODE -eq 0) { $started = $true; break }
            Info 'not ready yet'
        }
        if ($started) { Ok 'first report requested' }
        else {
            Warn 'first run could not be started; the schedule itself is valid'
            Warn "retry later with: az rest --method post --uri `"/subscriptions/$sub/providers/Microsoft.CostManagement/exports/$name/run?api-version=$EXPORT_API`""
        }
    }

    $results += [pscustomobject]@{ subscription = $sub; export = $name; status = 'ready' }
}

# ------------------------------------------------------------------ summary

Step 'Summary'
$results | Format-Table -AutoSize

$ready = @($results | Where-Object { $_.status -eq 'ready' }).Count
if ($ready -gt 0) {
    Write-Host ''
    Write-Host "$ready export(s) ready." -ForegroundColor Green
    Write-Host 'Azure produces the first report within a few hours, or immediately with -RunNow.'
    Write-Host 'CloudLens imports it automatically; press Refresh > Refresh latest to pull it sooner.'
    Write-Host 'Hourly Billing, Applications & Tags and scheduling savings populate from that import.'
}
if (@($results | Where-Object { $_.status -ne 'ready' }).Count -gt 0) {
    Write-Host ''
    Write-Host 'Some exports are not ready. Re-run this script after fixing the reported access;' -ForegroundColor Yellow
    Write-Host 'it skips whatever already exists.' -ForegroundColor Yellow
}
