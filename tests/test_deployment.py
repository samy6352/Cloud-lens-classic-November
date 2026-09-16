"""Deployment failures must stop the hand-off; Azure CLI calls are completely mocked."""
# cloudlens-test: requires-offline
from __future__ import annotations

import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
POWERSHELL = shutil.which("pwsh") or shutil.which("powershell")
MOCK = r"""
$ErrorActionPreference = 'Stop'
$global:roleGrants = @{}
function global:az {
    $global:LASTEXITCODE = 0
    # --parameters is passed an array, which arrives as one argument and stringifies to
    # "System.Object[]" under a plain -join. Flatten it so assertions can see the values.
    $call = ($args | ForEach-Object { if ($_ -is [array]) { $_ -join ' ' } else { $_ } }) -join ' '
    if ($call.StartsWith('account list')) {
        return '[{"id":"11111111-1111-4111-8111-111111111111","name":"Fixture","tenantId":"22222222-2222-4222-8222-222222222222","state":"Enabled"},{"id":"99999999-9999-4999-8999-999999999999","name":"Ungranted","tenantId":"22222222-2222-4222-8222-222222222222","state":"Enabled"}]'
    }
    if ($call.StartsWith('account show')) {
        if ($call.Contains('99999999')) {
            return '{"id":"99999999-9999-4999-8999-999999999999","name":"Ungranted","tenantId":"22222222-2222-4222-8222-222222222222","state":"Enabled"}'
        }
        return '{"id":"11111111-1111-4111-8111-111111111111","name":"Fixture","tenantId":"22222222-2222-4222-8222-222222222222","state":"Enabled"}'
    }
    if ($call.StartsWith('ad signed-in-user show')) {
        return '{"id":"33333333-3333-4333-8333-333333333333","userPrincipalName":"admin@example.invalid"}'
    }
    if ($call.StartsWith('group exists')) {
        if ($scenario -eq 'storage-preview') { return 'true' }
        return 'false'
    }
    if ($call.StartsWith('resource list') -and $scenario -eq 'storage-preview') {
        return '[{"id":"/subscriptions/11111111-1111-4111-8111-111111111111/resourceGroups/rg-cloudlens/providers/Microsoft.Storage/storageAccounts/cloudlensstrv0913","type":"Microsoft.Storage/storageAccounts","name":"cloudlensstrv0913","tags":{"application":"CloudLens"}}]'
    }
    if ($call.StartsWith('deployment group list') -and $scenario -eq 'storage-preview') { return '[]' }
    if ($call.StartsWith('storage account check-name')) { return '{"nameAvailable":true}' }
    if ($call.StartsWith('deployment sub what-if')) {
        if ($scenario -eq 'storage-preview') {
            if (-not $call.Contains('storageAccountName=cloudlensstrv0913')) { throw 'Preview would rename storage' }
            Write-Host 'EXISTING_STORAGE_PRESERVED_IN_PREVIEW'
            return
        }
        $global:LASTEXITCODE = 7
        return
    }
    if ($call.StartsWith('deployment sub create')) {
        if ($scenario -eq 'storage-preview') { throw 'WhatIf attempted an Azure write' }
        if ($call.Contains('authDelegatedArm=false')) { Write-Host 'EXPLICIT_FALLBACK_APPLIED' }
        return '{"webAppName":{"value":"fixture-app"},"webAppUrl":{"value":"https://fixture-app.azurewebsites.net"},"managedIdentityPrincipalId":{"value":"44444444-4444-4444-8444-444444444444"}}'
    }
    if ($call.StartsWith('webapp show')) {
        return '{"id":"/subscriptions/11111111-1111-4111-8111-111111111111/resourceGroups/rg-cloudlens/providers/Microsoft.Web/sites/fixture-app","name":"fixture-app","identity":{"type":"SystemAssigned","principalId":"44444444-4444-4444-8444-444444444444","tenantId":"22222222-2222-4222-8222-222222222222"}}'
    }
    if ($call.StartsWith('webapp config appsettings list') -and $call.Contains('AZURE_CLIENT_ID')) { return '[]' }
    if ($call.StartsWith('role assignment list')) {
        $sub = if ($call.Contains('99999999')) { '99999999-9999-4999-8999-999999999999' } else { '11111111-1111-4111-8111-111111111111' }
        $roles = @('acdd72a7-3385-48ef-bd42-f606fba81ae7', '72fafb9e-0641-4937-9268-a91bfd8191a3')
        if ($scenario -eq 'role-failure' -or ($scenario -eq 'partial-grant' -and $sub.StartsWith('99999999'))) { return '[]' }
        if ($scenario -eq 'partial-grant') { $roles = @($global:roleGrants[$sub]) }
        $assignments = @($roles | Where-Object { $_ } | ForEach-Object {
            @{principalId='44444444-4444-4444-8444-444444444444';roleDefinitionId="/subscriptions/$sub/providers/Microsoft.Authorization/roleDefinitions/$_";scope="/subscriptions/$sub"}
        })
        return (ConvertTo-Json -Depth 10 -InputObject $assignments)
    }
    if ($call.StartsWith('role assignment create')) {
        # partial-grant: the swept-up subscription is one this deployer cannot assign on.
        if ($scenario -eq 'partial-grant' -and -not $call.Contains('99999999')) {
            $role = $args[[array]::IndexOf($args, '--role') + 1]
            $global:roleGrants['11111111-1111-4111-8111-111111111111'] += @($role)
            return '{}'
        }
        $global:LASTEXITCODE = 8
        return
    }
    if ($call.StartsWith('ad app show') -and $call.Contains('publicClient.redirectUris')) {
        return 'https://fixture-app.azurewebsites.net/auth/callback'
    }
    if ($call.StartsWith('ad app show') -and $call.Contains('requiredResourceAccess')) {
        return '[{"resourceAppId":"797f4846-ba00-4fd7-ba43-dac1f8f63013","resourceAccess":[{"id":"41094075-9dad-400e-a0bd-54e686782033","type":"Scope"}]}]'
    }
    if ($call.StartsWith('webapp config appsettings set')) {
        if ($call.Contains('AUTH_DELEGATED_ARM=true')) { Write-Host 'UPGRADED_TO_DELEGATED' }
        return
    }
    if ($call.StartsWith('ad app permission admin-consent')) {
        if ($scenario -eq 'consent-ok') { return }
        $global:LASTEXITCODE = 9
        return
    }
    throw "Unexpected Azure call in offline deployment test: $call"
}
"""


@unittest.skipUnless(POWERSHELL, "PowerShell is required for deployment checks")
class DeploymentFailures(unittest.TestCase):
    def run_deploy(self, scenario, options):
        script = str(ROOT / "infra" / "deploy.ps1").replace("'", "''")
        with tempfile.TemporaryDirectory(dir=ROOT / ".pytest_cache", ignore_cleanup_errors=True) as work:
            state = str(Path(work) / "storage.json").replace("'", "''")
            report = str(Path(work) / "cost-access.json").replace("'", "''")
            command = f"$scenario = '{scenario}'\n{MOCK}\n& '{script}' -Location eastus2 -StorageNameStatePath '{state}' -CostAccessReportPath '{report}' {options}"
            return subprocess.run([POWERSHELL, "-NoProfile", "-Command", command],
                                  capture_output=True, text=True, timeout=60)

    def test_failed_preview_is_not_reported_successful(self):
        result = self.run_deploy("preview-failure", "-WhatIf -SkipEntra")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Previewing the infrastructure deployment failed", result.stdout + result.stderr)
        self.assertNotIn("Preview only. Re-run", result.stdout)

    def test_preview_passes_adopted_storage_to_template_without_creating_resources(self):
        result = self.run_deploy("storage-preview", "-WhatIf -SkipEntra")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("EXISTING_STORAGE_PRESERVED_IN_PREVIEW", result.stdout)
        self.assertIn("Preview only. Re-run", result.stdout)

    def test_failed_role_grant_stops_deployment(self):
        result = self.run_deploy("role-failure", "-SkipCode -SkipEntra")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Cost Management Reader [grant-failed]", result.stdout + result.stderr)
        self.assertIn("az role assignment create --assignee-object-id 44444444", result.stdout)
        self.assertNotIn("==> Done", result.stdout)

    def test_consent_failure_requires_explicit_fallback(self):
        result = self.run_deploy("consent-failure", "-SkipCode -EntraClientId fixture-client")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Delegated ARM consent is required", result.stdout + result.stderr)
        self.assertNotIn("EXPLICIT_FALLBACK_APPLIED", result.stdout)

    def test_explicit_limited_mode_is_reported_as_infrastructure_only(self):
        result = self.run_deploy("consent-failure",
                                 "-SkipCode -EntraClientId fixture-client -AllowAppIdentityFallback")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("EXPLICIT_FALLBACK_APPLIED", result.stdout)
        self.assertIn("application code was not published", result.stdout)
        self.assertNotIn("Nothing further is required", result.stdout)

    def test_granted_consent_overrides_a_requested_fallback(self):
        result = self.run_deploy("consent-ok",
                                 "-SkipCode -EntraClientId fixture-client -AllowAppIdentityFallback")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("UPGRADED_TO_DELEGATED", result.stdout)

    def test_default_does_not_sweep_unselected_subscriptions(self):
        """Other visible subscriptions are not implicitly selected for application grants."""
        result = self.run_deploy("consent-failure",
                                 "-SkipCode -EntraClientId fixture-client -AllowAppIdentityFallback")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertNotIn("99999999-9999-4999-8999-999999999999", result.stdout)
        self.assertIn("11111111-1111-4111-8111-111111111111 : Reader [already-present]", result.stdout)

    def test_cost_scope_all_visible_grants_every_visible_subscription(self):
        result = self.run_deploy("consent-ok",
                                 "-SkipCode -EntraClientId fixture-client -CostScopeAllVisible")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("99999999-9999-4999-8999-999999999999 : Cost Management Reader", result.stdout)
        self.assertNotIn("were not granted to CloudLens", result.stdout)

    def test_a_swept_up_subscription_we_cannot_grant_on_is_skipped_not_fatal(self):
        """-CostScopeAllVisible is an offer, not a requirement, so one refusal must not
        fail a deployment that was never really about that subscription."""
        result = self.run_deploy(
            "partial-grant",
            "-SkipCode -EntraClientId fixture-client -CostScopeAllVisible -AllowAppIdentityFallback")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("Pending managed-identity coverage: 99999999-9999-4999-8999-999999999999", result.stdout + result.stderr)
        self.assertIn("Optional cost scopes remain pending", result.stdout)
        self.assertIn("11111111-1111-4111-8111-111111111111 : Cost Management Reader", result.stdout)

    def test_readiness_retries_transient_errors_and_requires_auth(self):
        helper = str(ROOT / "infra" / "deployment-checks.ps1").replace("'", "''")
        command = rf"""
$ErrorActionPreference = 'Stop'
. '{helper}'
function Start-Sleep {{ }}
$script:calls = 0
function Invoke-RestMethod {{
    param([string]$Uri)
    if ($Uri -ne 'https://fixture.invalid/readyz') {{ throw 'Must use dependency readiness, not liveness' }}
    $script:calls++
    if ($script:calls -eq 1) {{ throw 'still starting' }}
    return @{{ status = 'ready' }}
}}
function Invoke-WebRequest {{ return @{{ StatusCode = 303 }} }}
Wait-CloudLensApplication -BaseUrl 'https://fixture.invalid' -Attempts 2
if ($script:calls -ne 2) {{ throw 'Transient health failure was not retried' }}
function Invoke-WebRequest {{ return @{{ StatusCode = 200 }} }}
$failed = $false
try {{ Wait-CloudLensApplication -BaseUrl 'https://fixture.invalid' -Attempts 1 }}
catch {{ $failed = $_.Exception.Message.Contains('without an authentication challenge') }}
if (-not $failed) {{ throw 'Anonymous application access was accepted' }}
function Invoke-RestMethod {{ return @{{ status = 'unavailable' }} }}
$failed = $false
try {{ Wait-CloudLensApplication -BaseUrl 'https://fixture.invalid' -Attempts 2 }}
catch {{ $failed = $_.Exception.Message.Contains('did not become ready') }}
if (-not $failed) {{ throw 'Unavailable application was accepted' }}
function Invoke-RestMethod {{ return @{{ status = 'ok' }} }}
$failed = $false
try {{ Wait-CloudLensApplication -BaseUrl 'https://fixture.invalid' -Attempts 1 }}
catch {{ $failed = $_.Exception.Message.Contains('did not become ready') }}
if (-not $failed) {{ throw 'A liveness-only response was accepted as readiness' }}
"""
        result = subprocess.run([POWERSHELL, "-NoProfile", "-Command", command],
                                capture_output=True, text=True, timeout=60)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


LEGACY_MOCK = r"""
$ErrorActionPreference = 'Stop'
function global:Get-Command { return @{ Source = 'Invoke-FixtureAz' } }
function global:Start-Sleep {}
function global:Invoke-FixtureAz {
    $global:LASTEXITCODE = 0
    $flat = foreach ($argument in $args) { $argument }
    $call = $flat -join ' '
    if ($call.StartsWith('account show')) {
        return '{"id":"11111111-1111-4111-8111-111111111111","name":"Fixture","tenantId":"22222222-2222-4222-8222-222222222222","state":"Enabled","user":{"name":"fixture-user"}}'
    }
    if ($call.StartsWith('group show')) { return 'eastus2' }
    if ($call.StartsWith('appservice plan create')) { return }
    if ($call.StartsWith('webapp show')) {
        return '{"id":"/subscriptions/11111111-1111-4111-8111-111111111111/resourceGroups/fixture/providers/Microsoft.Web/sites/fixture","name":"fixture","identity":{"type":"SystemAssigned","principalId":"44444444-4444-4444-8444-444444444444","tenantId":"22222222-2222-4222-8222-222222222222"}}'
    }
    if ($call.StartsWith('webapp config set')) { return }
    if ($call.StartsWith('webapp identity show')) { return '{"type":"SystemAssigned","principalId":"44444444-4444-4444-8444-444444444444"}' }
    if ($call.StartsWith('webapp identity assign')) { throw 'Existing identity must not be changed' }
    if ($call.StartsWith('role assignment create')) { return '{}' }
    if ($call.StartsWith('role assignment list')) {
        return '[{"principalId":"44444444-4444-4444-8444-444444444444","roleDefinitionId":"/subscriptions/11111111-1111-4111-8111-111111111111/providers/Microsoft.Authorization/roleDefinitions/72fafb9e-0641-4937-9268-a91bfd8191a3","scope":"/subscriptions/11111111-1111-4111-8111-111111111111"}]'
    }
    if ($call.StartsWith('webapp config appsettings list')) {
        if ($call.Contains('AZURE_CLIENT_ID')) { return '[]' }
        if ($call.Contains('AUTH_DATA_DIR')) { return '/home/cloudlens' }
        if ($call.Contains('AUTH_SECRET')) {
            if ($scenario -eq 'secret-read-failure') { $global:LASTEXITCODE = 5; return }
            return 'fixture-existing-secret'
        }
    }
    if ($call.StartsWith('webapp config appsettings set')) {
        if (-not $call.Contains('AUTH_DATA_DIR=/home/cloudlens')) { throw 'Auth directory changed' }
        if ($call.Contains('AUTH_SECRET=')) { throw 'Existing auth secret was overwritten' }
        Write-Host 'AUTH_STATE_PRESERVED'
        return
    }
    if ($call.StartsWith('webapp deploy')) {
        Write-Host 'PUBLISH_ATTEMPT'
        if ($scenario -eq 'publish-failure') { $global:LASTEXITCODE = 7 }
        return
    }
    throw "Unexpected Azure call in offline legacy test: $call"
}
function global:Invoke-RestMethod { return @{ status = 'ready' } }
function global:Invoke-WebRequest { return @{ StatusCode = 303 } }
"""


@unittest.skipUnless(POWERSHELL, "PowerShell is required for deployment checks")
class ExistingInfrastructureDeployment(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=ROOT / ".pytest_cache", ignore_cleanup_errors=True)
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        (self.root / "tools").mkdir()
        (self.root / "infra").mkdir()
        shutil.copy(ROOT / "tools/deploy.ps1", self.root / "tools/deploy.ps1")
        shutil.copy(ROOT / "infra/deployment-checks.ps1", self.root / "infra/deployment-checks.ps1")
        shutil.copy(ROOT / "infra/cost-access.ps1", self.root / "infra/cost-access.ps1")
        (self.root / "tools/package.ps1").write_text("""
param($Destination, $SourceRoot)
[IO.File]::WriteAllText($Destination, 'fixture-package')
[pscustomobject]@{ Files = 1; Bytes = 15; Sha256 = 'fixture' }
""", encoding="utf-8")

    def run_deploy(self, scenario, skip_roles=True):
        script = str(self.root / "tools/deploy.ps1").replace("'", "''")
        options = "-SkipRoleAssignment" if skip_roles else ""
        command = f"""
$scenario = '{scenario}'
{LEGACY_MOCK}
& '{script}' -ResourceGroup fixture -AppName fixture -Location eastus2 -Sku F1 `
    -PgHost fixture.invalid -PgUser fixture -ProjectEndpoint https://fixture.invalid {options}
"""
        return subprocess.run([POWERSHELL, "-NoProfile", "-Command", command],
                              capture_output=True, text=True, timeout=60)

    def test_failed_publish_is_not_retried_or_mistaken_for_an_old_success(self):
        result = self.run_deploy("publish-failure")
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(result.stdout.count("PUBLISH_ATTEMPT"), 1)
        self.assertIn("Publication failed or its status is unknown", result.stdout)
        self.assertNotIn("Checking application readiness", result.stdout)
        self.assertEqual(list(self.root.glob("cloudlens-*.zip")), [])
        self.assertEqual(list(self.root.glob(".cloudlens-cli-*.stderr")), [])

    def test_existing_auth_state_and_free_tier_warning_are_preserved(self):
        result = self.run_deploy("success")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("AUTH_STATE_PRESERVED", result.stdout)
        self.assertIn("F1 has no Always On", result.stdout)
        self.assertEqual(list(self.root.glob("cloudlens-*.zip")), [])

    def test_failed_secret_lookup_cannot_regenerate_the_signing_key(self):
        result = self.run_deploy("secret-read-failure")
        self.assertNotEqual(result.returncode, 0)
        self.assertNotIn("AUTH_STATE_PRESERVED", result.stdout)
        self.assertNotIn("PUBLISH_ATTEMPT", result.stdout)

    def test_cost_reader_is_not_mistaken_for_the_reader_role(self):
        result = self.run_deploy("missing-reader", skip_roles=False)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Reader [verification-failed]", result.stdout + result.stderr)
        self.assertNotIn("PUBLISH_ATTEMPT", result.stdout)


if __name__ == "__main__":
    unittest.main()
