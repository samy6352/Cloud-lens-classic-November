"""Offline managed-identity RBAC repair and administrator handoff regressions."""
# cloudlens-test: requires-offline
from __future__ import annotations

import json
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
POWERSHELL = shutil.which("pwsh") or shutil.which("powershell")
HOST = "11111111-1111-4111-8111-111111111111"
OTHER = "99999999-9999-4999-8999-999999999999"
TENANT = "22222222-2222-4222-8222-222222222222"
HUMAN = "33333333-3333-4333-8333-333333333333"
PRINCIPAL = "44444444-4444-4444-8444-444444444444"
CLIENT = "55555555-5555-4555-8555-555555555555"
READER = "acdd72a7-3385-48ef-bd42-f606fba81ae7"
COST_READER = "72fafb9e-0641-4937-9268-a91bfd8191a3"
CONTRIBUTOR = "434105ed-43f6-45c7-a02f-909b2ba83430"
APP_ID = f"/subscriptions/{HOST}/resourceGroups/fixture/providers/Microsoft.Web/sites/fixture"
UAI_ID = f"/subscriptions/{HOST}/resourceGroups/fixture/providers/Microsoft.ManagedIdentity/userAssignedIdentities/cloudlens-mi"

MOCK = r"""
function global:Start-Sleep {}
function global:az {
    $global:LASTEXITCODE = 0
    $flat = @($args | ForEach-Object { $_ })
    $call = $flat -join ' '
    $calls.Add($call)
    function arg([string]$name) {
        $index = [array]::IndexOf($flat, $name)
        if ($index -ge 0) { return $flat[$index + 1] }
        return ''
    }
    $sub = arg '--subscription'
    if (-not $sub) { $sub = $fixture.host }
    $operation = $flat[0..([math]::Min(2, $flat.Count - 1))] -join ' '
    $failure = $fixture.failures["${operation}:$sub"]
    if ($failure) {
        Write-Error $failure -ErrorAction Continue
        $global:LASTEXITCODE = 9
        return
    }
    if ($call.StartsWith('account list')) {
        return (ConvertTo-Json -Depth 20 -InputObject @($fixture.accounts.Values))
    }
    if ($call.StartsWith('account show')) {
        return (ConvertTo-Json -Depth 20 -InputObject $fixture.accounts[$sub])
    }
    if ($call.StartsWith('webapp show')) {
        return (ConvertTo-Json -Depth 20 -InputObject $fixture.app)
    }
    if ($call.StartsWith('webapp config appsettings list')) {
        if (-not $call.Contains('AZURE_CLIENT_ID')) { throw 'Must not read sign-in secrets' }
        return (ConvertTo-Json -Depth 20 -InputObject @($fixture.selectors))
    }
    if ($call.StartsWith('identity show')) {
        return (ConvertTo-Json -Depth 20 -InputObject $fixture.identities[(arg '--ids')])
    }
    if ($call.StartsWith('role assignment list')) {
        return (ConvertTo-Json -Depth 20 -InputObject @($grants[$sub]))
    }
    if ($call.StartsWith('role assignment create')) {
        $writes.Add($call)
        if ($fixture.silentFailure) {
            $global:LASTEXITCODE = 17
            return
        }
        if (-not $fixture.unverified) {
            $grants[$sub] = @($grants[$sub]) + @(@{
                principalId = (arg '--assignee-object-id')
                roleDefinitionId = "/subscriptions/$sub/providers/Microsoft.Authorization/roleDefinitions/$(arg '--role')"
                scope = "/subscriptions/$sub"
            })
        }
        return '{}'
    }
    throw "Unexpected Azure call; never invoke real Azure: $call"
}
"""


def assignment(role, principal=PRINCIPAL, sub=HOST, **extra):
    return {"principalId": principal, "roleDefinitionId": f"/subscriptions/{sub}/providers/Microsoft.Authorization/roleDefinitions/{role}",
            "scope": f"/subscriptions/{sub}", **extra}


def fixture():
    return {
        "host": HOST,
        "accounts": {sub: {"id": sub, "name": name, "tenantId": TENANT, "state": "Enabled"}
                     for sub, name in ((HOST, "Hosting"), (OTHER, "Selected"))},
        "app": {"id": APP_ID, "name": "fixture",
                "identity": {"type": "SystemAssigned", "principalId": PRINCIPAL, "tenantId": TENANT}},
        "selectors": [], "identities": {}, "failures": {},
        "grants": {HOST: [], OTHER: []},
    }


@unittest.skipUnless(POWERSHELL, "PowerShell required")
class CostAccess(unittest.TestCase):
    def setUp(self):
        (ROOT / ".pytest_cache").mkdir(exist_ok=True)
        self.work = tempfile.TemporaryDirectory(dir=ROOT / ".pytest_cache", ignore_cleanup_errors=True)
        self.addCleanup(self.work.cleanup)
        self.report = Path(self.work.name) / "handoff.json"

    def run_access(self, data=None, *, options="", scopes=None):
        data = data or fixture()
        script = str(ROOT / "infra/grant-cost-access.ps1").replace("'", "''")
        literal = json.dumps(data).replace("'", "''")
        requested = ("-CostScopeSubscriptions @(" + ",".join(f"'{s}'" for s in scopes) + ")") if scopes else ""
        command = f"""
$ErrorActionPreference = 'Stop'
$fixture = ConvertFrom-Json -AsHashtable '{literal}'
$grants = $fixture.grants
$calls = [Collections.Generic.List[string]]::new()
$writes = [Collections.Generic.List[string]]::new()
{MOCK}
try {{
    & '{script}' -ResourceGroup fixture -AppName fixture -HostingSubscriptionId '{HOST}' `
        -ReportPath '{self.report}' {requested} {options} | Out-Null
    $code = 0
}} catch {{
    Write-Host ('FAILURE:' + $_.Exception.Message)
    $code = 1
}}
Write-Output ('RESULT:' + (ConvertTo-Json -Compress -Depth 20 -InputObject @{{calls=@($calls);writes=@($writes)}}))
exit $code
"""
        result = subprocess.run([POWERSHELL, "-NoProfile", "-Command", command],
                                capture_output=True, text=True, timeout=60)
        output = next((line[7:] for line in result.stdout.splitlines() if line.startswith("RESULT:")), "{}")
        report = json.loads(self.report.read_text(encoding="utf-8-sig")) if self.report.exists() else None
        return result, json.loads(output), report

    def test_grants_and_verifies_app_not_human_or_client_id(self):
        data = fixture()
        data["grants"][HOST] = [assignment(READER, HUMAN), assignment(COST_READER, HUMAN)]
        result, calls, report = self.run_access(data)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(report["status"], "complete")
        self.assertEqual(len(calls["writes"]), 2)
        for call in calls["writes"]:
            self.assertIn(f"--assignee-object-id {PRINCIPAL}", call)
            self.assertNotIn(HUMAN, call)
            self.assertNotIn(CLIENT, call)
        self.assertEqual(report["identity"]["type"], "SystemAssigned")
        self.assertIsNone(report["identity"]["clientId"])
        self.assertIn("do NOT grant", report["explanation"])
        self.assertFalse(any(call.startswith("account list") for call in calls["calls"]))
        self.assertEqual([entry["subscriptionId"] for entry in report["subscriptions"]], [HOST])

    def test_already_present_assignments_are_idempotent(self):
        data = fixture()
        data["grants"][HOST] = [assignment(READER), assignment(COST_READER)]
        result, calls, report = self.run_access(data)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertFalse(calls["writes"])
        self.assertEqual([role["status"] for role in report["subscriptions"][0]["roles"][:2]],
                         ["already-present", "already-present"])

    def test_read_role_names_cannot_be_confused_or_conditional_grants_accepted(self):
        data = fixture()
        data["grants"][HOST] = [assignment(COST_READER), assignment(READER, condition="restricted")]
        result, calls, _ = self.run_access(data)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(len(calls["writes"]), 1)
        self.assertIn(READER, calls["writes"][0])

    def test_required_failures_report_every_scope_and_commands_before_throw(self):
        data = fixture()
        for sub in (HOST, OTHER):
            data["failures"][f"role assignment create:{sub}"] = "AuthorizationFailed: operator cannot assign roles"
        result, _, report = self.run_access(data, scopes=[HOST, OTHER])
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(report["pendingSubscriptions"], [HOST, OTHER])
        for entry in report["subscriptions"]:
            for role in entry["roles"][:2]:
                self.assertEqual(role["status"], "grant-failed")
                self.assertIn("AuthorizationFailed", role["error"])
                self.assertIn(PRINCIPAL, role["grantCommand"])
                self.assertIn(role["roleDefinitionId"], role["verifyCommand"])
                self.assertLess(result.stdout.index(role["grantCommand"]), result.stdout.index("FAILURE:"))

    def test_read_errors_have_diagnostics_and_never_trigger_speculative_grants(self):
        data = fixture()
        data["failures"][f"role assignment list:{HOST}"] = "ServiceUnavailable: throttled backend"
        result, calls, report = self.run_access(data)
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(calls["writes"])
        self.assertIn("ServiceUnavailable", report["subscriptions"][0]["roles"][0]["error"])
        self.assertNotIn("no rights", result.stdout)

    def test_empty_error_output_preserves_native_failure_code(self):
        data = fixture()
        data["silentFailure"] = True
        result, _, report = self.run_access(data)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Azure CLI exit 17", report["subscriptions"][0]["roles"][0]["error"])
        self.assertNotIn("null-valued", report["subscriptions"][0]["roles"][0]["error"])

    def test_successful_write_requires_readback(self):
        data = fixture()
        data["unverified"] = True
        result, _, report = self.run_access(data)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(report["subscriptions"][0]["roles"][0]["status"], "verification-failed")

    def test_optional_sweep_is_same_tenant_and_reports_pending_without_failing(self):
        data = fixture()
        foreign = "88888888-8888-4888-8888-888888888888"
        data["accounts"][foreign] = {"id": foreign, "state": "Enabled", "tenantId": HUMAN}
        data["failures"][f"role assignment create:{OTHER}"] = "DenyAssignment: custom restriction"
        result, calls, report = self.run_access(data, options="-CostScopeAllVisible")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(report["pendingSubscriptions"], [OTHER])
        self.assertEqual(report["status"], "pending")
        self.assertFalse(any(foreign in call for call in calls["calls"]))
        self.assertIn(OTHER, result.stdout)

    def test_write_role_is_explicit_opt_in_and_never_owner_or_uaa(self):
        result, calls, report = self.run_access()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(report["subscriptions"][0]["roles"][2]["status"], "not-selected")
        self.assertFalse(any(CONTRIBUTOR in call for call in calls["writes"]))
        result, calls, _ = self.run_access(options="-EnableExportManagement")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(len(calls["writes"]), 3)
        self.assertTrue(any(CONTRIBUTOR in call for call in calls["writes"]))
        for call in calls["writes"]:
            self.assertNotIn("Owner", call)
            self.assertNotIn("User Access Administrator", call)
            self.assertNotIn("Microsoft.Storage", call)

    def test_instructions_only_and_whatif_persist_handoff_without_writes(self):
        for option in ("-InstructionsOnly", "-WhatIf"):
            with self.subTest(option=option):
                result, calls, report = self.run_access(options=option)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertFalse(calls["writes"])
                self.assertTrue(report["instructionsOnly"])
                self.assertEqual(report["status"], "pending")
                self.assertIsNotNone(report["subscriptions"][0]["roles"][0]["grantCommand"])
                self.assertFalse(list(self.report.parent.glob("*.stderr")))

    def test_selects_attached_user_identity_using_client_id_not_system_principal(self):
        data = fixture()
        data["selectors"] = [CLIENT]
        data["app"]["identity"]["type"] = "SystemAssigned, UserAssigned"
        data["app"]["identity"]["principalId"] = HUMAN
        data["app"]["identity"]["userAssignedIdentities"] = {UAI_ID: {"clientId": CLIENT, "principalId": PRINCIPAL}}
        data["identities"][UAI_ID] = {"id": UAI_ID, "name": "cloudlens-mi", "tenantId": TENANT,
                                     "clientId": CLIENT, "principalId": PRINCIPAL}
        result, calls, report = self.run_access(data)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(report["identity"]["type"], "UserAssigned")
        self.assertEqual(report["identity"]["clientId"], CLIENT)
        self.assertEqual(report["identity"]["resourceId"], UAI_ID)
        self.assertTrue(all(PRINCIPAL in call and HUMAN not in call for call in calls["writes"]))

    def test_rejects_missing_or_unattached_user_identity_selector(self):
        for selectors in ([], [CLIENT], ["not-a-guid"]):
            data = fixture()
            data["selectors"] = selectors
            data["app"]["identity"]["type"] = "UserAssigned"
            data["app"]["identity"]["userAssignedIdentities"] = {UAI_ID: {}}
            data["identities"][UAI_ID] = {"id": UAI_ID, "name": "cloudlens-mi", "tenantId": TENANT,
                                         "clientId": HUMAN, "principalId": PRINCIPAL}
            result, calls, report = self.run_access(data)
            self.assertNotEqual(result.returncode, 0)
            self.assertFalse(calls["writes"])
            self.assertEqual(report["status"], "blocked")

    def test_invalid_or_cross_tenant_scopes_block_all_writes_before_grants(self):
        for sub in ("not-a-guid", OTHER):
            data = fixture()
            data["accounts"][OTHER]["tenantId"] = HUMAN
            result, calls, report = self.run_access(data, scopes=[HOST, sub])
            self.assertNotEqual(result.returncode, 0)
            self.assertFalse(calls["writes"])
            self.assertEqual(report["status"], "blocked")
            self.assertEqual(len(report["subscriptions"]), 2)
            self.assertIsNone(report["subscriptions"][1]["roles"][0]["grantCommand"])

    def test_identity_tenant_principal_and_app_scope_are_validated(self):
        for field in ("tenant", "principal", "resource"):
            data = fixture()
            if field == "tenant":
                data["app"]["identity"]["tenantId"] = HUMAN
            elif field == "principal":
                data["app"]["identity"]["principalId"] = "invalid"
            else:
                data["app"]["id"] = APP_ID.replace(HOST, OTHER)
            result, calls, report = self.run_access(data)
            self.assertNotEqual(result.returncode, 0)
            self.assertFalse(calls["writes"])
            self.assertTrue(report["errors"])


if __name__ == "__main__":
    unittest.main()
