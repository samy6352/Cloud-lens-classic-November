# Read-only prerequisite verification; never changes registrations or grants consent.
function Assert-CloudLensDelegatedConsent {
    param(
        [Parameter(Mandatory)][string]$ClientId,
        [Parameter(Mandatory)][string]$RedirectUri
    )
    function Read-ConsentGraph([string]$Url) {
        $raw = az rest --method GET --url $Url -o json 2>$null
        if ($LASTEXITCODE -ne 0 -or -not $raw) {
            throw 'Cannot verify Entra registration/consent. Ask the identity team for read access or run this verification with an approved reader; directory administrator deployment rights are not required.'
        }
        return ($raw | ConvertFrom-Json)
    }
    function Read-ConsentPages([string]$Url) {
        while ($Url) {
            $page = Read-ConsentGraph $Url
            @($page.value) | Where-Object { $_ }
            $Url = $page.'@odata.nextLink'
            if ($Url -and -not $Url.StartsWith('https://graph.microsoft.com/v1.0/')) {
                throw 'Unexpected consent pagination origin.'
            }
        }
    }
    if ($ClientId -notmatch '^[0-9a-fA-F-]{36}$') { throw 'A valid preapproved Entra client ID is required.' }
    $apps = @(Read-ConsentPages "https://graph.microsoft.com/v1.0/applications?`$filter=appId eq '$ClientId'")
    if ($apps.Count -ne 1) { throw 'The exact customer app registration could not be resolved.' }
    $app = $apps[0]
    if ($app.signInAudience -ne 'AzureADMyOrg' -or
        $app.isFallbackPublicClient -ne $true -or
        @($app.publicClient.redirectUris) -cnotcontains $RedirectUri -or
        @($app.web.redirectUris | Where-Object { $_ }).Count -or
        $app.web.implicitGrantSettings.enableAccessTokenIssuance -or
        $app.web.implicitGrantSettings.enableIdTokenIssuance -or
        @($app.passwordCredentials | Where-Object { $_ }).Count -or
        @($app.keyCredentials | Where-Object { $_ }).Count -or
        @($app.appRoles | Where-Object { $_ }).Count -or
        @($app.api.oauth2PermissionScopes | Where-Object { $_ }).Count) {
        throw 'Preapproved registration must be single-tenant public-client PKCE, with the exact callback, no credentials, web callbacks, implicit grants, roles or exposed API.'
    }
    $armAppId = '797f4846-ba00-4fd7-ba43-dac1f8f63013'
    $armScopeId = '41094075-9dad-400e-a0bd-54e686782033'
    $allowed = @{
        '00000003-0000-0000-c000-000000000000' = @(
            '37f7f235-527c-4136-accd-4a02d197296e', '14dad69e-099b-42c9-810b-d002981feec1',
            '7427e0e9-2fba-42fe-b0c0-848c9e6a8182', 'e1fe6dd8-ba31-4d61-89e7-88639da4683d')
        $armAppId = @($armScopeId)
    }
    foreach ($resource in @($app.requiredResourceAccess)) {
        foreach ($permission in @($resource.resourceAccess)) {
            if (-not $allowed.ContainsKey($resource.resourceAppId) -or
                $permission.type -ne 'Scope' -or
                $permission.id -notin $allowed[$resource.resourceAppId]) {
                throw 'Preapproved registration requests unreviewed API permissions; no blanket consent is permitted.'
            }
        }
    }
    $requested = @($app.requiredResourceAccess | Where-Object { $_.resourceAppId -eq $armAppId } |
        ForEach-Object { $_.resourceAccess } | Where-Object { $_.id -eq $armScopeId -and $_.type -eq 'Scope' })
    if (-not $requested.Count) { throw 'Registration must request ARM delegated user_impersonation before deployment.' }
    $clients = @(Read-ConsentPages "https://graph.microsoft.com/v1.0/servicePrincipals?`$filter=appId eq '$ClientId'")
    $resources = @(Read-ConsentPages "https://graph.microsoft.com/v1.0/servicePrincipals?`$filter=appId eq '$armAppId'")
    if ($clients.Count -ne 1 -or $resources.Count -ne 1 -or $clients[0].accountEnabled -ne $true) {
        throw 'Customer and ARM service principals must exist; the customer service principal must be enabled.'
    }
    $scope = @($resources[0].oauth2PermissionScopes | Where-Object {
        $_.id -eq $armScopeId -and $_.value -eq 'user_impersonation' -and $_.isEnabled -eq $true
    })
    if ($scope.Count -ne 1) { throw 'ARM user_impersonation scope could not be verified on the resource service principal.' }
    $grants = @(Read-ConsentPages "https://graph.microsoft.com/v1.0/oauth2PermissionGrants?`$filter=clientId eq '$($clients[0].id)'")
    $approved = @($grants | Where-Object {
        $_.clientId -eq $clients[0].id -and $_.resourceId -eq $resources[0].id -and
        $_.consentType -eq 'AllPrincipals' -and -not $_.principalId -and
        (($_.scope -split '\s+') -ccontains 'user_impersonation')
    })
    if (-not $approved.Count) {
        throw 'Tenant administrator approval of ARM user_impersonation for this exact customer service principal is missing. Individual consent or a directory role is not approval.'
    }
    Write-Host '    verified: exact registration, callback and tenant-wide ARM delegated grant (read-only)'
}
