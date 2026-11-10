function Get-CloudLensIpv4Range([string]$Cidr) {
    if ($Cidr -notmatch '^(\d{1,3}(?:\.\d{1,3}){3})/(\d{1,2})$') {
        throw "Invalid IPv4 CIDR: $Cidr"
    }
    $prefix = [int]$Matches[2]
    $octets = @($Matches[1].Split('.') | ForEach-Object { [int]$_ })
    if ($prefix -gt 32 -or @($octets | Where-Object { $_ -gt 255 }).Count) {
        throw "Invalid IPv4 CIDR: $Cidr"
    }
    $address = [uint64]0
    foreach ($octet in $octets) { $address = $address * 256 + $octet }
    $size = [uint64][math]::Pow(2, 32 - $prefix)
    $start = [uint64]([math]::Floor($address / $size) * $size)
    if ($start -ne $address) { throw "CIDR must start at its network address: $Cidr" }
    return @{ Start = $start; End = $start + $size - 1 }
}

function Test-CloudLensIpv4Overlap([string]$Left, [string]$Right) {
    $a = Get-CloudLensIpv4Range $Left
    $b = Get-CloudLensIpv4Range $Right
    return $a.Start -le $b.End -and $b.Start -le $a.End
}

function Get-CloudLensPostgresTier([string]$Sku) {
    if ($Sku -match '^Standard_B') { return 'Burstable' }
    if ($Sku -match '^Standard_E') { return 'MemoryOptimized' }
    return 'GeneralPurpose'
}

function Get-CloudLensConsentCapability {
    # Directory roles that can grant tenant-wide admin consent for delegated permissions.
    $roles = @('Global Administrator', 'Privileged Role Administrator',
               'Cloud Application Administrator', 'Application Administrator')
    $raw = az rest --method GET `
        --url 'https://graph.microsoft.com/v1.0/me/transitiveMemberOf/microsoft.graph.directoryRole?$select=displayName' `
        -o json 2>$null
    if ($LASTEXITCODE -ne 0 -or -not $raw) { return @{ Status = 'unknown'; Roles = $roles } }
    $held = @(($raw | ConvertFrom-Json).value | ForEach-Object { $_.displayName } | Where-Object { $_ -in $roles })
    # Eligible (not yet activated) PIM roles are not listed, so 'no' means no active role.
    if ($held.Count) { return @{ Status = 'yes'; Roles = $held } }
    return @{ Status = 'no'; Roles = $roles }
}

function Get-CloudLensModelAlternatives($Models, $Usages, [string]$Sku, [int]$Capacity, [string[]]$Preferred) {
    # Other models offered on the same SKU with enough free quota. Compatibility with the
    # application is not established by this list; it only says where capacity exists.
    foreach ($name in $Preferred) {
        $q = Get-CloudLensModelQuota $Models $Usages $name $Sku $Capacity
        if ($q.Status -eq 'ok') { $name }
    }
}

function Get-CloudLensModelQuota($Models, $Usages, [string]$ModelName, [string]$Sku, [int]$Capacity) {
    $names = @($Models | Where-Object { $_.model.name -eq $ModelName } |
        ForEach-Object { $_.model.skus } |
        Where-Object { $_.name -eq $Sku -and $_.usageName -and $_.usageName -notmatch 'finetune' } |
        ForEach-Object { $_.usageName } | Select-Object -Unique)
    $matches = @($Usages | Where-Object { $_.name.value -in $names })
    if ($matches.Count -ne 1 -or $null -eq $matches[0].limit -or $null -eq $matches[0].currentValue) {
        return @{ Status = 'unknown'; Message = 'Exact model quota could not be resolved; catalogue availability is not a quota guarantee.' }
    }

    $quota = $matches[0]
    $remaining = [double]$quota.limit - [double]$quota.currentValue
    $status = if ($quota.limit -lt 0) { 'unknown' } elseif ($remaining -lt $Capacity) { 'blocked' } else { 'ok' }
    return @{ Status = $status; Name = $quota.name.value; Remaining = $remaining
        Message = "$($quota.name.value): $($quota.currentValue) used of $($quota.limit); requesting $Capacity capacity units." }
}

function Wait-CloudLensApplication {
    param(
        [Parameter(Mandatory)][string]$BaseUrl,
        [ValidateRange(1, 3600)][int]$TimeoutSeconds = 600,
        [ValidateRange(1, 120)][int]$Attempts = 40
    )
    $deadline = [DateTime]::UtcNow.AddSeconds($TimeoutSeconds)
    $ready = $false
    for ($attempt = 1; $attempt -le $Attempts; $attempt++) {
        $remaining = [int][math]::Ceiling(($deadline - [DateTime]::UtcNow).TotalSeconds)
        if ($remaining -le 0) { break }
        try {
            $readiness = Invoke-RestMethod -Uri "$BaseUrl/readyz" -TimeoutSec ([math]::Min(20, $remaining))
            if ($readiness.status -eq 'ready') { $ready = $true; break }
        } catch {
            Write-Verbose "Readiness check $attempt failed: $($_.Exception.Message)"
        }
        $remaining = [int][math]::Floor(($deadline - [DateTime]::UtcNow).TotalSeconds)
        if ($attempt -lt $Attempts -and $remaining -gt 0) {
            Start-Sleep -Seconds ([math]::Min(15, $remaining))
        }
    }
    if (-not $ready) {
        throw "The app did not become ready within $TimeoutSeconds seconds. Check application startup and PostgreSQL connectivity/schema."
    }

    # Do not follow a login redirect and mistake its 200 response for anonymous access.
    try {
        $response = Invoke-WebRequest -Uri $BaseUrl -UseBasicParsing -MaximumRedirection 0 -TimeoutSec 20 -ErrorAction Stop
        $status = [int]$response.StatusCode
    } catch {
        if (-not $_.Exception.Response) { throw }
        $status = [int]$_.Exception.Response.StatusCode
    }
    if ($status -notin @(401, 302, 303, 307)) {
        throw "/ returned $status without an authentication challenge. Verify sign-in settings before hand-off."
    }
}
