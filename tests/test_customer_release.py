"""Offline customer-release prerequisites and allowlist regressions."""
# cloudlens-test: requires-offline
import hashlib
import importlib.util
import json
import shutil
import subprocess
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SHELL = shutil.which("pwsh")


@unittest.skipUnless(SHELL, "PowerShell 7 required")
class DelegatedConsent(unittest.TestCase):
    def test_exact_consent_and_registration_matrix(self):
        helper = str(ROOT / "infra" / "delegated-consent.ps1").replace("'", "''")
        script = r"""
$ErrorActionPreference = 'Stop'
. '__HELPER__'
$client='11111111-1111-4111-8111-111111111111'
$redirect='https://cloudlens-web-fixture.azurewebsites.net/auth/callback'
function global:az {
    $global:LASTEXITCODE=0
    $call=$args -join ' '
    if (-not $call.StartsWith('rest --method GET')) { throw 'Unexpected Azure mutation' }
    if ($scenario -eq 'unreadable') { $global:LASTEXITCODE=3; return }
    $arm='797f4846-ba00-4fd7-ba43-dac1f8f63013'
    $scope='41094075-9dad-400e-a0bd-54e686782033'
    if ($call.Contains('/applications?')) {
        $app=@{
            signInAudience='AzureADMyOrg';isFallbackPublicClient=$true
            publicClient=@{redirectUris=@($redirect)};web=@{redirectUris=@()}
            requiredResourceAccess=@(@{resourceAppId=$arm;resourceAccess=@(@{id=$scope;type='Scope'})})
        }
        switch($scenario) {
            'callback' { $app.publicClient.redirectUris=@('https://wrong.invalid/auth/callback') }
            'audience' { $app.signInAudience='AzureADMultipleOrgs' }
            'public' { $app.isFallbackPublicClient=$false }
            'credential' { $app.passwordCredentials=@(@{keyId='secret'}) }
            'implicit' { $app.web.implicitGrantSettings=@{enableIdTokenIssuance=$true} }
            'request' { $app.requiredResourceAccess=@() }
            'extra-permission' { $app.requiredResourceAccess[0].resourceAccess+=@(@{id='unknown';type='Scope'}) }
            'application-permission' { $app.requiredResourceAccess[0].resourceAccess[0].type='Role' }
        }
        return (@{value=@($app)} | ConvertTo-Json -Depth 12)
    }
    if ($call.Contains('/servicePrincipals?')) {
        if ($call.Contains($arm)) {
            return (@{value=@(@{id='arm-object';oauth2PermissionScopes=@(@{
                id=$scope;value='user_impersonation';isEnabled=($scenario -ne 'disabled-scope')
            })})} | ConvertTo-Json -Depth 12)
        }
        return (@{value=@(@{id='client-object';accountEnabled=($scenario -ne 'disabled-client')})} | ConvertTo-Json -Depth 12)
    }
    if ($call.Contains('/oauth2PermissionGrants?') -or $call.Contains('/next-grants')) {
        $grant=@{clientId='client-object';resourceId='arm-object';consentType='AllPrincipals';scope='user_impersonation'}
        switch($scenario) {
            'resource' { $grant.resourceId='other-object' }
            'client' { $grant.clientId='other-object' }
            'individual' { $grant.consentType='Principal';$grant.principalId='person' }
            'scope' { $grant.scope='User.Read user_impersonation_extra' }
            'missing' { return '{"value":[]}' }
        }
        if ($scenario -eq 'paged' -and -not $call.Contains('/next-grants')) {
            return '{"value":[],"@odata.nextLink":"https://graph.microsoft.com/v1.0/next-grants"}'
        }
        return (@{value=@($grant)} | ConvertTo-Json -Depth 12)
    }
    throw "Unexpected Graph read: $call"
}
foreach($scenario in @('ok','paged','callback','audience','public','credential','implicit','request',
    'resource','client','individual','scope','missing','unreadable','disabled-scope','disabled-client',
    'extra-permission','application-permission')) {
    $passed=$false
    try { Assert-CloudLensDelegatedConsent $client $redirect; $passed=$true } catch {}
    if ($passed -ne ($scenario -in @('ok','paged'))) { throw "Consent verification mismatch: $scenario" }
}
Write-Output 'All exact-consent cases passed'
"""
        result = subprocess.run(
            [SHELL, "-NoProfile", "-Command", script.replace("__HELPER__", helper)],
            text=True, capture_output=True, timeout=60,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_default_and_skip_preflight_still_block_before_provisioning(self):
        script = str(ROOT / "infra" / "deploy.ps1").replace("'", "''")
        command = r"""
$ErrorActionPreference='Stop'
function global:az {
    $global:LASTEXITCODE=0
    if (($args -join ' ').StartsWith('account show')) { return '{"id":"fixture","tenantId":"fixture"}' }
    throw 'Must stop before any provisioning or other Azure call'
}
$blocked=$false
try { & '__SCRIPT__' -Location fixture -AiLocation fixture -SkipPreflight } catch {
    if ($_.Exception.Message -notlike '*already admin-approved registration*') { throw }
    $blocked=$true
}
if (-not $blocked) { throw 'Default delegated mode was bypassed' }
"""
        result = subprocess.run(
            [SHELL, "-NoProfile", "-Command", command.replace("__SCRIPT__", script)],
            text=True, capture_output=True, timeout=60,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


class ReleaseContents(unittest.TestCase):
    def test_release_documentation_and_source_allowlist(self):
        spec = importlib.util.spec_from_file_location("customer_release", ROOT / "tools" / "customer_release.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        self.assertEqual(set(module.DOCUMENTS), {"DEPLOYMENT.md", "READINESS.md", "HANDOFF.md", "RUNBOOK.md"})
        for path in module.INFRA:
            self.assertTrue((ROOT / "infra" / path).is_file(), path)
            self.assertIn(Path(path).suffix, {".ps1", ".bicep", ".json", ".sql"})
        self.assertIn("AllPrincipals", module.DOCUMENTS["DEPLOYMENT.md"])
        self.assertIn("EXPORT_REPORTING_ENABLED", module.DOCUMENTS["DEPLOYMENT.md"])
        installer = (ROOT / "infra" / "deploy.ps1").read_text(encoding="utf-8-sig")
        executable = "\n".join(line for line in installer.splitlines()
                               if line.lstrip().startswith("az "))
        self.assertNotIn("az ad app permission admin-consent", executable)
        self.assertIn("[string[]]$PostgresSkuFallback = @()", installer)
        self.assertIn('"authClientId=$EntraClientId"', installer)
        self.assertIn("param authDelegatedArm bool = true",
                      (ROOT / "infra" / "main.bicep").read_text())
        self.assertLess(installer.index("Assert-CloudLensDelegatedConsent -ClientId"),
                        installer.index('Step "Deploying infrastructure'))

    @unittest.skipUnless(SHELL, "PowerShell 7 required")
    def test_actual_bundle_integrity_and_state_exclusion(self):
        with tempfile.TemporaryDirectory(dir=ROOT / ".pytest_cache",
                                         ignore_cleanup_errors=True) as workspace:
            output = Path(workspace) / "release"
            result = subprocess.run(
                [sys.executable, str(ROOT / "tools" / "customer_release.py"),
                 "--destination", str(output)],
                text=True, capture_output=True, timeout=120,
            )
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            with zipfile.ZipFile(output / "cloudlens-customer.zip") as archive:
                self.assertIsNone(archive.testzip())
                names = set(archive.namelist())
                manifest = json.loads(archive.read("customer-manifest.json"))
                self.assertEqual(names, {entry["path"] for entry in manifest["files"]}
                                 | {"customer-manifest.json"})
                for entry in manifest["files"]:
                    content = archive.read(entry["path"])
                    self.assertEqual(len(content), entry["bytes"])
                    self.assertEqual(hashlib.sha256(content).hexdigest(), entry["sha256"])
                for name in names:
                    parts = Path(name).parts
                    self.assertFalse(any(part.startswith(".") or part in {
                        "data", "logs", "__pycache__", "node_modules", "dist", "tests",
                    } for part in parts), name)
                    self.assertNotIn(Path(name).suffix, {".log", ".pyc", ".zip", ".csv", ".parquet"})
                self.assertIn("infra/delegated-consent.ps1", names)
                self.assertIn("install.ps1", names)
                self.assertEqual(archive.read("install.ps1"), (ROOT / "install.ps1").read_bytes())
                self.assertIn("app/export_access.py", names)  # preserved, not enabled
                self.assertIn("app/advisor_score.py", names)
                self.assertIn("app/model_compatibility.py", names)
                self.assertIn("app/telemetry.py", names)
                self.assertNotIn("docs/export-reporting-groundwork.md", names)
                access = archive.read("app/export_access.py").decode()
                self.assertIn('os.getenv("EXPORT_REPORTING_ENABLED", "")', access)
                installer = archive.read("infra/deploy.ps1").decode("utf-8-sig")
                self.assertIn("[switch]$DelegatedArm = $true", installer)
            verified = subprocess.run(
                [sys.executable, str(ROOT / "tools" / "customer_release.py"),
                 "--verify-current", str(output / "cloudlens-customer.zip")],
                text=True, capture_output=True, timeout=30,
            )
            self.assertEqual(verified.returncode, 0, verified.stdout + verified.stderr)


@unittest.skipUnless(SHELL, "PowerShell 7 required")
class GuidedInstall(unittest.TestCase):
    def test_syntax_and_offline_guided_flow(self):
        # All Azure calls and deployment execution are mocked; no Azure access occurs.
        install = str(ROOT / "install.ps1").replace("'", "''")
        command = r"""
$ErrorActionPreference='Stop'
$tokens=$null; $errors=$null
$ast=[System.Management.Automation.Language.Parser]::ParseFile('__INSTALL__',[ref]$tokens,[ref]$errors)
if ($errors.Count) { throw ($errors | Out-String) }
$source=Get-Content -LiteralPath '__INSTALL__' -Raw
$source=$source.Replace('Join-Path $PSScriptRoot ''infra\deploy.ps1''', "'__DEPLOY__'")
$source=$source.Replace('Push-Location $PSScriptRoot', "Push-Location (Split-Path '__INSTALL__')")
$source=$source.Replace('& $deploy @parameters -WhatIf', 'Invoke-FakeDeploy $parameters $true')
$source=$source.Replace('& $deploy @parameters', 'Invoke-FakeDeploy $parameters $false')
# Throw instead of exiting the test host on expected rejection.
$source=$source.Replace('Write-Error $_', 'throw $_').Replace('exit 1','throw "Stopped"')
function global:python { $global:LASTEXITCODE=0 }
function global:az {
    $global:LASTEXITCODE=0
    $call=$args -join ' '
    $script:calls.Add($call)
    if ($scenario -eq 'dependency-fail' -and $call -eq 'bicep version') { $global:LASTEXITCODE=7 }
    if ($scenario -eq 'native-fail' -and $call -like 'login *') { $global:LASTEXITCODE=9 }
    if ($call -eq 'account show -o json') {
        if ($scenario -eq 'wrong-account') { return '{"id":"wrong","tenantId":"wrong"}' }
        return '{"id":"11111111-1111-4111-8111-111111111111","tenantId":"11111111-1111-4111-8111-111111111111"}'
    }
}
function global:Read-Host {
    param($Prompt)
    if (-not $script:answers.Count) { throw "Unexpected prompt: $Prompt" }
    return $script:answers.Dequeue()
}
function Invoke-FakeDeploy($parameters,$preview) {
    if ($preview) {
        $script:previewParameters=$parameters.Clone()
        $script:previewCount++
        if ($scenario -eq 'preview-fail') { throw 'Consent/preflight blocked' }
        if ($scenario -eq 'preview-native-fail') { $global:LASTEXITCODE=12 }
    } else {
        $script:applyCount++
        foreach ($key in $parameters.Keys) {
            if (($parameters[$key] | ConvertTo-Json -Compress) -cne
                ($script:previewParameters[$key] | ConvertTo-Json -Compress)) {
                throw "Apply parameter differs from preview: $key"
            }
        }
        if (-not $parameters.DelegatedArm -or $parameters.PostgresSkuFallback.Count) { throw 'Unsafe mode' }
    }
}
foreach($scenario in @('ok','cancel','early-cancel','decline','dependency-fail','native-fail',
    'wrong-account','preview-fail','preview-native-fail')) {
    $script:calls=[System.Collections.Generic.List[string]]::new()
    $script:answers=[System.Collections.Generic.Queue[string]]::new()
    $id='11111111-1111-4111-8111-111111111111'
    foreach($answer in @($(if($scenario -eq 'early-cancel'){'CANCEL'}else{$id}),$id,
        'DELEGATED','approvedregion','approvedairegion','rg-fixture','fixture',$id,
        'approvedmodel','Standard','10','Standard_E2ds_v5','B2','10.70.0.0/16',
        '10.70.1.0/24','10.70.2.0/24','10.70.3.0/24','admin@example.invalid','LIST',$id,
        'PREVIEW',$(if($scenario -eq 'cancel'){'CANCEL'}elseif($scenario -eq 'decline'){'deploy'}else{'DEPLOY'}))) {
        $script:answers.Enqueue($answer)
    }
    $script:previewCount=0; $script:applyCount=0; $failed=$false
    try { & ([scriptblock]::Create($source)) } catch {
        $failed=$true
        if ($scenario -eq 'ok') { throw }
    }
    if ($scenario -eq 'ok') {
        if ($failed -or $applyCount -ne 1 -or $previewCount -ne 1) { throw 'Happy path failed' }
    } elseif (-not $failed -or $applyCount -ne 0) { throw "Fail-closed mismatch: $scenario" }
    if ($scenario -in @('early-cancel','dependency-fail') -and
        @($script:calls | Where-Object { $_ -like 'login *' }).Count) {
        throw 'Sign-in started before approved inputs and dependencies'
    }
}
"""
        result = subprocess.run(
            [SHELL, "-NoProfile", "-Command", command.replace("__INSTALL__", install)
             .replace("__DEPLOY__", str(ROOT / "infra" / "deploy.ps1").replace("'", "''"))],
            text=True, capture_output=True, timeout=60,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


if __name__ == "__main__":
    unittest.main()
