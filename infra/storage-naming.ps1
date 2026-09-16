# Export storage is a deployment identity, not a function of friendly resource names.
function Get-CloudLensStorageAccountName {
    param(
        [Parameter(Mandatory)][string]$SubscriptionId,
        [Parameter(Mandatory)][string]$ResourceGroup,
        [Parameter(Mandatory)][string]$NamePrefix,
        [string]$NameSuffix = '',
        [string]$StorageAccountName = '',
        [Parameter(Mandatory)][string]$StatePath
    )

    function Read-AzureJson([string[]]$Arguments) {
        $raw = & az @Arguments --subscription $SubscriptionId --only-show-errors -o json
        if ($LASTEXITCODE -ne 0) {
            throw "Cannot resolve export storage: Azure CLI read failed ($($Arguments -join ' '))."
        }
        if (-not $raw) { throw 'Cannot resolve export storage: Azure CLI returned no JSON.' }
        return ($raw | ConvertFrom-Json)
    }

    $scope = "/subscriptions/$SubscriptionId/resourceGroups/$ResourceGroup"
    $storageScope = "$scope/providers/Microsoft.Storage/storageAccounts/"
    $candidates = [Collections.Generic.List[object]]::new()
    function Add-Candidate([string]$Name, [string]$Id, [string]$Source, [bool]$MustExist) {
        if ($Id) {
            if (-not $Id.StartsWith($storageScope, [StringComparison]::OrdinalIgnoreCase)) {
                throw "Foreign export storage in ${Source}: expected an account in $scope."
            }
            $idName = $Id.Substring($storageScope.Length)
            if ($Name -and $Name -cne $idName) {
                throw "Export storage name/ID mismatch in $Source."
            }
            $Name = $idName
        }
        if ($Name -cnotmatch '^[a-z0-9]{3,24}$') {
            throw "Invalid export storage account name in $Source."
        }
        $candidates.Add([pscustomobject]@{ Name = $Name; Source = $Source; MustExist = $MustExist })
    }

    # Exclusive access also makes two local first-run previews converge on one identity.
    $StatePath = [IO.Path]::GetFullPath($StatePath)
    [IO.Directory]::CreateDirectory([IO.Path]::GetDirectoryName($StatePath)) | Out-Null
    $lock = [IO.File]::Open("$StatePath.lock", 'OpenOrCreate', 'ReadWrite', 'None')
    try {
        if (Test-Path -LiteralPath $StatePath) {
            $state = Get-Content -LiteralPath $StatePath -Raw | ConvertFrom-Json
            if ($state.version -ne 1 -or $state.scope -ine $scope) {
                throw "Export storage state scope/version mismatch: $StatePath."
            }
            if ($state.namePrefix -cne $NamePrefix -or $state.nameSuffix -cne $NameSuffix) {
                throw "Resource naming differs from $StatePath. Preserve the existing deployment's prefix/suffix."
            }
            Add-Candidate $state.storageAccountName '' 'saved deployment state' ([bool]$state.observedExisting)
        }
        if ($StorageAccountName) { Add-Candidate $StorageAccountName '' '-StorageAccountName' $false }

        $exists = Read-AzureJson @('group', 'exists', '--name', $ResourceGroup)
        if ($exists -isnot [bool]) { throw 'Resource group existence could not be verified.' }
        $accounts = @()
        if ($exists) {
            $resources = @(Read-AzureJson @('resource', 'list', '--resource-group', $ResourceGroup))
            # Never accept a result outside the requested subscription/resource group.
            foreach ($resource in $resources) {
                if (-not ([string]$resource.id).StartsWith("$scope/providers/", [StringComparison]::OrdinalIgnoreCase)) {
                    throw 'Resource inventory contains an unexpected resource ID scope.'
                }
                if ($resource.type -in @('Microsoft.Storage/storageAccounts', 'Microsoft.Web/sites', 'Microsoft.Network/privateEndpoints')) {
                    $expectedId = "$scope/providers/$($resource.type)/$($resource.name)"
                    if ($resource.id -ine $expectedId) { throw 'Resource inventory contains an unexpected resource ID.' }
                }
            }
            $accounts = @($resources | Where-Object { $_.type -ieq 'Microsoft.Storage/storageAccounts' })
            foreach ($app in @($resources | Where-Object { $_.type -ieq 'Microsoft.Web/sites' })) {
                $settings = @(Read-AzureJson @('webapp', 'config', 'appsettings', 'list',
                    '--resource-group', $ResourceGroup, '--name', $app.name))
                $names = @($settings | Where-Object { $_.name -ceq 'ARCHIVE_ACCOUNT' -and $_.value })
                $ids = @($settings | Where-Object { $_.name -ceq 'ARCHIVE_ACCOUNT_ID' -and $_.value })
                if ($names.Count -gt 1 -or $ids.Count -gt 1) { throw 'Ambiguous archive app settings.' }
                if ($names.Count -or $ids.Count) {
                    Add-Candidate ($names | Select-Object -ExpandProperty value) `
                        ($ids | Select-Object -ExpandProperty value) "app $($app.name)" $true
                }
            }
            $deployments = @(Read-AzureJson @('deployment', 'group', 'list', '--resource-group', $ResourceGroup))
            foreach ($deployment in @($deployments | Where-Object { $_.name -ceq 'cloudlens-stack' })) {
                $outputs = $deployment.properties.outputs
                $parameters = $deployment.properties.parameters
                if ($outputs.storageAccount.value -or $outputs.storageAccountId.value) {
                    Add-Candidate $outputs.storageAccount.value $outputs.storageAccountId.value 'stack outputs' $true
                }
                if ($parameters.storageAccountName.value) {
                    Add-Candidate $parameters.storageAccountName.value '' 'stack parameters' $false
                }
            }
            $endpointName = if ($NameSuffix) { "$NamePrefix-pe-blob-$NameSuffix" } else { "pe-$NamePrefix-blob" }
            foreach ($endpoint in @($resources | Where-Object {
                $_.type -ieq 'Microsoft.Network/privateEndpoints' -and $_.name -ieq $endpointName
            })) {
                $details = Read-AzureJson @('network', 'private-endpoint', 'show', '--ids', $endpoint.id)
                $connections = @($details.privateLinkServiceConnections) + @($details.manualPrivateLinkServiceConnections)
                $storageConnections = @($connections | Where-Object { $_.privateLinkServiceId })
                if (-not $storageConnections.Count) { throw 'Existing blob private endpoint has no verifiable target.' }
                foreach ($connection in $storageConnections) {
                    Add-Candidate '' $connection.privateLinkServiceId 'blob private endpoint' $true
                }
            }
            if (-not $candidates.Count -and $accounts.Count) {
                # Legacy recovery requires ownership evidence, not "first storage in the RG".
                $owned = @($accounts | Where-Object { $_.tags.application -ceq 'CloudLens' })
                if ($owned.Count -ne 1) {
                    throw 'Ambiguous or unowned existing storage. Review the account and pass -StorageAccountName.'
                }
                Add-Candidate $owned[0].name $owned[0].id 'CloudLens resource tags' $true
            }
        }

        $distinct = @($candidates.Name | Select-Object -Unique)
        if ($distinct.Count -gt 1) {
            throw "Conflicting export storage identities: $(($candidates | ForEach-Object { "$($_.Source)=$($_.Name)" }) -join ', '). Refusing to rename or repoint storage."
        }
        $name = if ($distinct.Count) { $distinct[0] } else { '' }
        $ownedAccount = @($accounts | Where-Object { $_.name -ceq $name })
        if (-not $ownedAccount.Count -and @($candidates | Where-Object { $_.MustExist }).Count) {
            throw 'Previously configured export storage is missing. Refusing to recreate it or replace its data.'
        }
        if (-not $ownedAccount.Count -and $accounts.Count) {
            throw 'Existing storage is present but does not match the saved/reviewed name. Refusing to replace it.'
        }
        if (-not $ownedAccount.Count) {
            if ($name) {
                $availability = Read-AzureJson @('storage', 'account', 'check-name', '--name', $name)
                if ($availability.nameAvailable -ne $true) {
                    throw "Saved/reviewed storage name $name is unavailable. Refusing to rotate it."
                }
            } else {
                $rng = [Security.Cryptography.RandomNumberGenerator]::Create()
                try {
                    for ($attempt = 0; $attempt -lt 10; $attempt++) {
                        $bytes = New-Object byte[] 10
                        $rng.GetBytes($bytes)
                        $name = 'st' + [BitConverter]::ToString($bytes).Replace('-', '').ToLowerInvariant()
                        $availability = Read-AzureJson @('storage', 'account', 'check-name', '--name', $name)
                        if ($availability.nameAvailable -eq $true) { break }
                        if ($availability.reason -ne 'AlreadyExists') { throw 'Storage name availability could not be verified.' }
                        $name = ''
                    }
                } finally { $rng.Dispose() }
                if (-not $name) { throw 'Could not allocate an available random export storage name.' }
            }
        }

        $record = [ordered]@{
            version = 1; scope = $scope; namePrefix = $NamePrefix; nameSuffix = $NameSuffix
            storageAccountName = $name; observedExisting = ($ownedAccount.Count -gt 0)
        }
        # Write locally even for -WhatIf; preview and apply must use exactly the same name.
        $pending = "$StatePath.pending"
        try {
            $record | ConvertTo-Json | Set-Content -LiteralPath $pending -Encoding utf8
            Move-Item -LiteralPath $pending -Destination $StatePath -Force
        } finally {
            Remove-Item -LiteralPath $pending -ErrorAction SilentlyContinue
        }
        Write-Host "    export storage: $name ($(if ($ownedAccount.Count) { 'adopted/reused' } else { 'reserved locally' }))"
        Write-Host "    naming state : $StatePath (retain for preview/apply and recovery)"
        return $name
    } finally {
        $lock.Dispose()
    }
}
