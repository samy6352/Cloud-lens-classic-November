# Shared by both deployment paths and the post-deployment RBAC repair command.
function Test-CloudLensGuid([string]$Value) {
    return $Value -match '^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$' -and
        $Value -ne '00000000-0000-0000-0000-000000000000'
}

function Invoke-CloudLensCostAz {
    param([string[]]$Arguments, [string]$AzCommand = 'az', [string]$WorkDirectory)
    $errorFile = Join-Path $WorkDirectory ".$([guid]::NewGuid().ToString('N')).stderr"
    try {
        # AzCommand is explicitly the raw executable in tools/deploy.ps1, not its throwing Az wrapper.
        $raw = & $AzCommand @Arguments --only-show-errors -o json 2>$errorFile
        $code = $LASTEXITCODE
        $errorText = if (Test-Path $errorFile) { [string](Get-Content $errorFile -Raw) } else { '' }
        if ($code -ne 0) {
            return [pscustomobject]@{ Success = $false; Data = $null; Error = "Azure CLI exit ${code}: $(([string]$errorText).Trim())"; ExitCode = $code }
        }
        if (-not $raw) { throw 'Azure CLI returned no JSON.' }
        $data = $raw | ConvertFrom-Json -ErrorAction Stop
        return [pscustomobject]@{ Success = $true; Data = $data; Error = $null; ExitCode = 0 }
    } catch {
        # Normalize invocation/JSON failures too, so every scope gets a durable handoff.
        return [pscustomobject]@{ Success = $false; Data = $null; Error = $_.Exception.Message; ExitCode = $null }
    } finally {
        Remove-Item -LiteralPath $errorFile -Force -ErrorAction SilentlyContinue -WhatIf:$false
    }
}

function Get-CloudLensRuntimeIdentity {
    param(
        [string]$SubscriptionId, [string]$TenantId, [string]$ResourceGroup, [string]$AppName,
        [string]$AzCommand = 'az', [string]$WorkDirectory, [switch]$AllowUnassigned
    )
    if (-not (Test-CloudLensGuid $SubscriptionId) -or -not (Test-CloudLensGuid $TenantId)) {
        throw 'Hosting subscription and tenant must be nonzero GUIDs.'
    }
    if ($ResourceGroup -notmatch '^[a-zA-Z0-9_.()-]{1,90}$' -or $AppName -notmatch '^[a-zA-Z0-9-]{1,60}$') {
        throw 'Invalid resource group or App Service name.'
    }
    $appId = "/subscriptions/$SubscriptionId/resourceGroups/$ResourceGroup/providers/Microsoft.Web/sites/$AppName"
    $readArgs = @{ AzCommand = $AzCommand; WorkDirectory = $WorkDirectory }
    $app = Invoke-CloudLensCostAz @readArgs -Arguments @(
        'webapp', 'show', '--subscription', $SubscriptionId, '--resource-group', $ResourceGroup, '--name', $AppName
    )
    if (-not $app.Success) { throw "Reading App Service identity failed: $($app.Error)" }
    if ($app.Data.id -ine $appId) { throw 'App Service resource ID does not match the requested hosting scope.' }
    $settings = Invoke-CloudLensCostAz @readArgs -Arguments @(
        'webapp', 'config', 'appsettings', 'list', '--subscription', $SubscriptionId,
        '--resource-group', $ResourceGroup, '--name', $AppName, '--query', "[?name=='AZURE_CLIENT_ID'].value"
    )
    if (-not $settings.Success) { throw "Reading runtime AZURE_CLIENT_ID failed: $($settings.Error)" }
    $selectors = @($settings.Data | Where-Object { $_ })
    if ($selectors.Count -gt 1) { throw 'Ambiguous AZURE_CLIENT_ID settings.' }
    $selector = if ($selectors.Count) { [string]$selectors[0] } else { '' }
    $identity = $app.Data.identity
    $attached = @($identity.userAssignedIdentities.PSObject.Properties | Where-Object { $_.Name })
    if ($selector) {
        if (-not (Test-CloudLensGuid $selector)) { throw 'AZURE_CLIENT_ID must be the managed identity client GUID, not its principal ID or a resource ID.' }
        $selectedIdentities = @()
        foreach ($entry in $attached) {
            $id = $entry.Name
            if ($id -notmatch '^/subscriptions/([0-9a-fA-F-]{36})/resourceGroups/([a-zA-Z0-9_.()-]{1,90})/providers/Microsoft.ManagedIdentity/userAssignedIdentities/([a-zA-Z0-9_-]{1,128})$' -or
                -not (Test-CloudLensGuid $Matches[1])) {
                throw 'An attached user-assigned identity has an invalid resource ID.'
            }
            $found = Invoke-CloudLensCostAz @readArgs -Arguments @('identity', 'show', '--ids', $id)
            if (-not $found.Success) { throw "Reading attached managed identity failed: $($found.Error)" }
            $uai = $found.Data
            if ($uai.id -ine $id -or -not (Test-CloudLensGuid $uai.clientId) -or
                -not (Test-CloudLensGuid $uai.principalId) -or $uai.tenantId -ine $TenantId) {
                throw 'Attached managed identity has invalid IDs or belongs to a different tenant.'
            }
            if (($entry.Value.clientId -and $entry.Value.clientId -ine $uai.clientId) -or
                ($entry.Value.principalId -and $entry.Value.principalId -ine $uai.principalId)) {
                throw 'App Service and managed identity resource IDs disagree.'
            }
            if ($uai.clientId -ieq $selector) { $selectedIdentities += $uai }
        }
        if ($selectedIdentities.Count -ne 1) { throw 'AZURE_CLIENT_ID does not select exactly one attached user-assigned managed identity; refusing to guess.' }
        $selected = $selectedIdentities[0]
        return [pscustomobject]@{
            name = $selected.name; type = 'UserAssigned'; resourceId = $selected.id
            principalObjectId = $selected.principalId; clientId = $selected.clientId
            tenantId = $TenantId; appResourceId = $appId; selection = 'AZURE_CLIENT_ID'
        }
    }
    if ($attached.Count) { throw 'User-assigned identities are attached but AZURE_CLIENT_ID is absent. Configure the active identity explicitly; refusing to guess.' }
    if ($identity.type -match 'SystemAssigned') {
        if (-not (Test-CloudLensGuid $identity.principalId) -or $identity.tenantId -ine $TenantId) {
            throw 'System-assigned identity has an invalid principal ID or tenant.'
        }
        return [pscustomobject]@{
            name = $AppName; type = 'SystemAssigned'; resourceId = $appId
            principalObjectId = $identity.principalId; clientId = $null
            tenantId = $TenantId; appResourceId = $appId; selection = 'system-assigned (no AZURE_CLIENT_ID)'
        }
    }
    if ($AllowUnassigned -and (-not $identity.type -or $identity.type -eq 'None')) { return $null }
    throw 'The app has no verifiable active managed identity.'
}

function Test-CloudLensCostRole {
    param($Assignments, [string]$PrincipalId, [string]$RoleId, [string]$Scope)
    return @($Assignments | Where-Object {
        $_.principalId -ieq $PrincipalId -and
        ([string]$_.roleDefinitionId).Split('/')[-1] -ieq $RoleId -and
        -not $_.condition -and
        ($_.scope -ieq $Scope -or $_.scope -match '^/providers/Microsoft.Management/managementGroups/[^/]+$')
    }).Count -gt 0
}

function Invoke-CloudLensCostAccess {
    param(
        [string]$HostingSubscriptionId, [string]$ResourceGroup, [string]$AppName,
        [string[]]$CostScopeSubscriptions = @(), [switch]$CostScopeAllVisible,
        [switch]$EnableExportManagement, [switch]$InstructionsOnly,
        [string]$ReportPath = '', [string]$AzCommand = 'az',
        [ValidateRange(1, 10)][int]$VerificationAttempts = 3
    )
    if (-not $ReportPath) {
        $ReportPath = Join-Path (Split-Path $PSScriptRoot -Parent) "data\cost-access\$([guid]::NewGuid().ToString('N')).json"
    }
    $ReportPath = [IO.Path]::GetFullPath($ReportPath)
    $directory = Split-Path $ReportPath -Parent
    [IO.Directory]::CreateDirectory($directory) | Out-Null
    $cli = @{ AzCommand = $AzCommand; WorkDirectory = $directory }
    $roles = @(
        @{ name = 'Reader'; id = 'acdd72a7-3385-48ef-bd42-f606fba81ae7'; selected = $true; purpose = 'Runtime resource inventory and subscription discovery.' }
        @{ name = 'Cost Management Reader'; id = '72fafb9e-0641-4937-9268-a91bfd8191a3'; selected = $true; purpose = 'Runtime cost analysis and reading cost data.' }
        @{ name = 'Cost Management Contributor'; id = '434105ed-43f6-45c7-a02f-909b2ba83430'; selected = [bool]$EnableExportManagement; purpose = 'OPTIONAL: create/manage Cost Management export schedules and budgets; opt in with -EnableExportManagement. Does not grant storage data-plane access.' }
    )
    $report = [ordered]@{
        version = 1; generatedAtUtc = [DateTime]::UtcNow.ToString('o'); reportPath = $ReportPath
        instructionsOnly = [bool]$InstructionsOnly; status = 'pending'; identity = $null
        explanation = 'Human Owner/Contributor rights and the Entra sign-in app registration do NOT grant the runtime managed identity access. Assign the roles to principalObjectId (NOT clientId). Hosting infrastructure/bootstrap permissions are separate. Never grant the runtime app subscription Owner or User Access Administrator.'
        operatorRequirement = 'A role-assignment administrator (for example Owner, User Access Administrator, or Role Based Access Control Administrator at each selected scope, subject to conditions) must execute pending grant commands, then rerun grant-cost-access.ps1 to verify.'
        errors = @(); subscriptions = @(); pendingSubscriptions = @()
        optionalExportManagement = $roles[2].purpose
    }
    try {
        if ($HostingSubscriptionId -and -not (Test-CloudLensGuid $HostingSubscriptionId)) { throw 'Invalid hosting subscription GUID.' }
        if ($CostScopeAllVisible -and $CostScopeSubscriptions.Count) { throw 'Choose explicit -CostScopeSubscriptions OR opt-in -CostScopeAllVisible, not both.' }
        $accountArgs = @('account', 'show')
        if ($HostingSubscriptionId) { $accountArgs += @('--subscription', $HostingSubscriptionId) }
        $accountRead = Invoke-CloudLensCostAz @cli -Arguments $accountArgs
        if (-not $accountRead.Success) { throw "Reading hosting subscription failed: $($accountRead.Error)" }
        $account = $accountRead.Data
        if (-not (Test-CloudLensGuid $account.id) -or -not (Test-CloudLensGuid $account.tenantId) -or
            ($HostingSubscriptionId -and $account.id -ine $HostingSubscriptionId)) {
            throw 'Hosting subscription or tenant metadata is invalid.'
        }
        $HostingSubscriptionId = $account.id
        $requested = @($CostScopeSubscriptions)
        if ($CostScopeAllVisible) {
            $list = Invoke-CloudLensCostAz @cli -Arguments @('account', 'list', '--all')
            if (-not $list.Success) { throw "Listing opted-in same-tenant subscriptions failed: $($list.Error)" }
            $requested = @($list.Data | Where-Object { $_.tenantId -ieq $account.tenantId -and $_.state -eq 'Enabled' } | ForEach-Object { $_.id })
            if (-not $requested.Count) { throw 'No enabled subscriptions found in the managed identity tenant.' }
        }
        if (-not $requested.Count) { $requested = @($account.id) }
        foreach ($sub in @($requested | Select-Object -Unique)) {
            $entry = [ordered]@{
                subscriptionId = $sub; scope = $null; name = $null; tenantId = $null
                required = (-not $CostScopeAllVisible); validation = 'blocked'; error = $null; roles = @()
            }
            if (-not (Test-CloudLensGuid $sub)) {
                $entry.error = 'Invalid subscription GUID; no grant command generated.'
            } else {
                $entry.scope = "/subscriptions/$sub"
                $info = Invoke-CloudLensCostAz @cli -Arguments @('account', 'show', '--subscription', $sub)
                if (-not $info.Success) { $entry.error = "Reading subscription failed: $($info.Error)" }
                elseif ($info.Data.id -ine $sub -or $info.Data.tenantId -ine $account.tenantId) {
                    $entry.error = 'Subscription ID/tenant mismatch; cross-tenant grants are forbidden.'
                } elseif ($info.Data.state -ne 'Enabled') {
                    $entry.error = 'Subscription is not enabled.'
                } else {
                    $entry.name = $info.Data.name; $entry.tenantId = $info.Data.tenantId; $entry.validation = 'valid'
                }
            }
            $report.subscriptions += $entry
        }
        # Validate every requested scope before ANY RBAC write, including mixed valid/invalid lists.
        $blocked = @($report.subscriptions | Where-Object { $_.validation -ne 'valid' }).Count -gt 0
        $report.identity = Get-CloudLensRuntimeIdentity -SubscriptionId $account.id -TenantId $account.tenantId `
            -ResourceGroup $ResourceGroup -AppName $AppName @cli
        foreach ($entry in $report.subscriptions) {
            $principal = $report.identity.principalObjectId
            $listArgs = @('role', 'assignment', 'list', '--scope', $entry.scope, '--include-inherited',
                '--subscription', $entry.subscriptionId, '--fill-principal-name', 'false',
                '--query', "[?principalId=='$principal'].{principalId:principalId,roleDefinitionId:roleDefinitionId,scope:scope,condition:condition}")
            $existing = if (-not $blocked) { Invoke-CloudLensCostAz @cli -Arguments $listArgs } else { $null }
            foreach ($role in $roles) {
                $item = [ordered]@{
                    name = $role.name; roleDefinitionId = $role.id; selected = $role.selected
                    purpose = $role.purpose; status = 'not-selected'; error = $null
                    grantCommand = $null; verifyCommand = $null
                }
                if ($entry.validation -eq 'valid') {
                    $item.grantCommand = "az role assignment create --assignee-object-id $principal --assignee-principal-type ServicePrincipal --role $($role.id) --scope $($entry.scope) --subscription $($entry.subscriptionId) --only-show-errors"
                    $item.verifyCommand = "az role assignment list --scope $($entry.scope) --include-inherited --subscription $($entry.subscriptionId) --fill-principal-name false --query `"[?principalId=='$principal' && ends_with(roleDefinitionId, '$($role.id)')].{principalId:principalId,roleDefinitionId:roleDefinitionId,scope:scope,condition:condition}`" -o json"
                }
                if ($role.selected) {
                    if ($blocked) {
                        $item.status = 'blocked'; $item.error = 'No writes attempted: one or more requested scopes failed validation.'
                    } elseif (-not $existing.Success) {
                        $item.status = 'read-failed'; $item.error = $existing.Error
                    } elseif (Test-CloudLensCostRole $existing.Data $principal $role.id $entry.scope) {
                        $item.status = 'already-present'
                    } elseif ($InstructionsOnly) {
                        $item.status = 'pending-instructions'
                    } else {
                        $grant = Invoke-CloudLensCostAz @cli -Arguments @('role', 'assignment', 'create',
                            '--assignee-object-id', $principal, '--assignee-principal-type', 'ServicePrincipal',
                            '--role', $role.id, '--scope', $entry.scope, '--subscription', $entry.subscriptionId)
                        if (-not $grant.Success) {
                            $item.status = 'grant-failed'; $item.error = $grant.Error
                        } else {
                            $item.status = 'verification-failed'
                            $item.error = 'Role assignment write returned success but the grant was not visible on verification; rerun after propagation.'
                            for ($attempt = 0; $attempt -lt $VerificationAttempts; $attempt++) {
                                if ($attempt) { Start-Sleep -Seconds 2 }
                                $verify = Invoke-CloudLensCostAz @cli -Arguments $listArgs
                                if (-not $verify.Success) { $item.error = $verify.Error; break }
                                if (Test-CloudLensCostRole $verify.Data $principal $role.id $entry.scope) {
                                    $item.status = 'granted-verified'; $item.error = $null; break
                                }
                            }
                        }
                    }
                }
                $entry.roles += $item
            }
        }
    } catch {
        # Fail closed, but never discard the administrator handoff when discovery/validation fails.
        $report.errors += $_.Exception.Message
    }
    foreach ($entry in $report.subscriptions | Where-Object { -not $_.roles.Count }) {
        foreach ($role in $roles) {
            $entry.roles += [ordered]@{
                name = $role.name; roleDefinitionId = $role.id; selected = $role.selected; purpose = $role.purpose
                status = $(if ($role.selected) { 'blocked' } else { 'not-selected' })
                error = 'Identity/scope discovery failed; no writes attempted.'
                grantCommand = $null; verifyCommand = $null
            }
        }
    }
    $pending = @($report.subscriptions | Where-Object {
        $_.validation -ne 'valid' -or @($_.roles | Where-Object { $_.selected -and $_.status -notin @('already-present', 'granted-verified') }).Count
    })
    $report.pendingSubscriptions = @($pending | ForEach-Object { $_.subscriptionId })
    $report.status = if ($report.errors.Count -or @($report.subscriptions | Where-Object { $_.validation -ne 'valid' }).Count) {
        'blocked'
    } elseif ($pending.Count) { 'pending' } else { 'complete' }
    $report | ConvertTo-Json -Depth 12 | Set-Content -LiteralPath $ReportPath -Encoding utf8 -WhatIf:$false

    Write-Host $report.explanation
    if ($report.identity) {
        Write-Host "Runtime identity: $($report.identity.name) [$($report.identity.type)]"
        Write-Host "  Resource ID: $($report.identity.resourceId)"
        Write-Host "  Principal/object ID (RBAC assignee): $($report.identity.principalObjectId)"
        Write-Host "  Client ID (credential selector, NOT RBAC assignee): $($report.identity.clientId)"
    }
    Write-Host $report.optionalExportManagement
    Write-Host "Cost access report: $ReportPath [$($report.status)]"
    foreach ($errorMessage in $report.errors) { Write-Warning $errorMessage }
    foreach ($entry in $report.subscriptions) {
        if ($entry.error) { Write-Warning "$($entry.subscriptionId): $($entry.error)" }
        foreach ($role in $entry.roles | Where-Object { $_.selected }) {
            Write-Host "  $($entry.subscriptionId) : $($role.name) [$($role.status)]"
            if ($role.status -notin @('already-present', 'granted-verified')) {
                if ($role.error) { Write-Warning $role.error }
                if ($role.grantCommand) {
                    Write-Host "    $($role.grantCommand)"
                    Write-Host "    Verify: $($role.verifyCommand)"
                }
            }
        }
    }
    if ($pending.Count -or $report.errors.Count) {
        Write-Warning "Pending managed-identity coverage: $($report.pendingSubscriptions -join ', '). $($report.operatorRequirement)"
    }
    if ($report.status -eq 'blocked' -or (-not $InstructionsOnly -and @($pending | Where-Object { $_.required }).Count)) {
        throw "Required managed-identity cost access is incomplete. Review $ReportPath and the commands above; rerun infra\grant-cost-access.ps1 after the operator has role-assignment rights."
    }
    return [pscustomobject]$report
}
