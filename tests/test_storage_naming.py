"""Export storage identity is random once, scoped, and preserved without Azure writes."""
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
SCOPE = "/subscriptions/11111111-1111-4111-8111-111111111111/resourceGroups/fixture"
STORAGE_SCOPE = f"{SCOPE}/providers/Microsoft.Storage/storageAccounts/"

MOCK = r"""
function global:az {
    $global:LASTEXITCODE = 0
    $call = $args -join ' '
    $calls.Add($call)
    if ($fixture.failRead -and $call.StartsWith($fixture.failRead)) {
        $global:LASTEXITCODE = 7
        return
    }
    if ($call.StartsWith('group exists')) {
        return (ConvertTo-Json -InputObject ([bool]$fixture.exists))
    }
    if ($call.StartsWith('resource list')) {
        return (ConvertTo-Json -InputObject @($fixture.resources) -Depth 20)
    }
    if ($call.StartsWith('webapp config appsettings list')) {
        if ($call.Contains('--ids')) { throw 'Appsettings list requires resource group and name.' }
        $name = $args[[array]::IndexOf($args, '--name') + 1]
        $group = $args[[array]::IndexOf($args, '--resource-group') + 1]
        $subscription = $args[[array]::IndexOf($args, '--subscription') + 1]
        $id = "/subscriptions/$subscription/resourceGroups/$group/providers/Microsoft.Web/sites/$name"
        return (ConvertTo-Json -InputObject @($fixture.settings[$id]) -Depth 20)
    }
    if ($call.StartsWith('deployment group list')) {
        return (ConvertTo-Json -InputObject @($fixture.deployments) -Depth 20)
    }
    if ($call.StartsWith('network private-endpoint show')) {
        return (ConvertTo-Json -InputObject $fixture.endpoint -Depth 20)
    }
    if ($call.StartsWith('storage account check-name')) {
        $script:checks++
        if ($fixture.unavailable -or $script:checks -le $fixture.collisions) {
            return '{"nameAvailable":false,"reason":"AlreadyExists"}'
        }
        return '{"nameAvailable":true}'
    }
    throw "Unexpected Azure call (writes are forbidden): $call"
}
"""


def resource(kind, name, **extra):
    return {"type": kind, "name": name, "id": f"{SCOPE}/providers/{kind}/{name}", **extra}


def existing(name="cloudlensstrv0913", *, id_only=False):
    app = resource("Microsoft.Web/sites", "cloud-lens-cx")
    settings = [{"name": "ARCHIVE_ACCOUNT_ID", "value": STORAGE_SCOPE + name}]
    if not id_only:
        settings.append({"name": "ARCHIVE_ACCOUNT", "value": name})
    return {
        "exists": True,
        "resources": [resource("Microsoft.Storage/storageAccounts", name), app],
        "settings": {app["id"]: settings},
    }


@unittest.skipUnless(POWERSHELL, "PowerShell is required for offline storage naming checks")
class StorageNaming(unittest.TestCase):
    def setUp(self):
        work_root = ROOT / ".pytest_cache"
        work_root.mkdir(exist_ok=True)
        self.work = tempfile.TemporaryDirectory(dir=work_root, ignore_cleanup_errors=True)
        self.addCleanup(self.work.cleanup)
        self.state = Path(self.work.name) / "storage.json"

    def resolve(self, fixture=None, *, name="", state=None):
        fixture = {"resources": [], "settings": {}, "deployments": [], **(fixture or {})}
        literal = json.dumps(fixture).replace("'", "''")
        helper = str(ROOT / "infra" / "storage-naming.ps1").replace("'", "''")
        state = str(state or self.state).replace("'", "''")
        command = f"""
$ErrorActionPreference = 'Stop'
. '{helper}'
$fixture = ConvertFrom-Json -AsHashtable '{literal}'
$calls = [Collections.Generic.List[string]]::new()
$script:checks = 0
{MOCK}
$name = Get-CloudLensStorageAccountName -SubscriptionId '11111111-1111-4111-8111-111111111111' `
    -ResourceGroup fixture -NamePrefix CloudLens -NameSuffix prod -StatePath '{state}' `
    -StorageAccountName '{name}'
Write-Output ('RESULT:' + (ConvertTo-Json -Compress -Depth 20 -InputObject @{{name=$name;calls=@($calls)}}))
"""
        result = subprocess.run([POWERSHELL, "-NoProfile", "-Command", command],
                                capture_output=True, text=True, timeout=60)
        if result.returncode:
            return result, None
        output = next(line[7:] for line in result.stdout.splitlines() if line.startswith("RESULT:"))
        return result, json.loads(output)

    def success(self, *args, **kwargs):
        result, output = self.resolve(*args, **kwargs)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        return output

    def failure(self, fixture, message, **kwargs):
        result, _ = self.resolve(fixture, **kwargs)
        self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn(message, result.stdout + result.stderr)

    def test_new_names_are_random_independent_of_reused_friendly_names(self):
        first = self.success()
        second = self.success(state=Path(self.work.name) / "second.json")
        self.assertRegex(first["name"], r"^st[0-9a-f]{20}$")
        self.assertNotEqual(first["name"], second["name"])
        self.assertTrue(any("storage account check-name" in call for call in first["calls"]))

    def test_preview_then_apply_uses_persisted_name(self):
        preview = self.success()
        applied = self.success()
        self.assertEqual(preview["name"], applied["name"])
        self.assertEqual(json.loads(self.state.read_text(encoding="utf-8-sig"))["storageAccountName"],
                         preview["name"])
        self.assertFalse(list(self.state.parent.glob("*.pending")))

    def test_first_run_retries_global_name_collision(self):
        output = self.success({"collisions": 2})
        checked = [call for call in output["calls"] if call.startswith("storage account check-name")]
        self.assertEqual(len(checked), 3)
        self.assertEqual(len(set(checked)), 3)

    def test_saved_name_collision_never_rotates(self):
        self.success()
        before = self.state.read_bytes()
        self.failure({"unavailable": True}, "Refusing to rotate")
        self.assertEqual(before, self.state.read_bytes())

    def test_live_target_and_legacy_source_are_adopted_without_name_check(self):
        for name in ("cloudlensstrv0913", "cloudlens6no73w"):
            with self.subTest(name=name):
                output = self.success(existing(name), state=self.state.with_name(f"{name}.json"))
                self.assertEqual(output["name"], name)
                self.assertFalse(any("check-name" in call for call in output["calls"]))

    def test_account_id_only_and_name_only_legacy_settings(self):
        for id_only in (True, False):
            fixture = existing(id_only=id_only)
            if not id_only:
                for settings in fixture["settings"].values():
                    settings[:] = [item for item in settings if item["name"] == "ARCHIVE_ACCOUNT"]
            self.assertEqual(self.success(fixture)["name"], "cloudlensstrv0913")

    def test_existing_identity_cannot_be_overridden(self):
        self.failure(existing(), "Conflicting export storage identities", name="stnewreplacement")

    def test_foreign_subscription_and_resource_group_ids_are_rejected(self):
        for foreign_id in (
            STORAGE_SCOPE.replace("11111111", "99999999") + "cloudlensstrv0913",
            STORAGE_SCOPE.replace("/fixture/", "/another/") + "cloudlensstrv0913",
        ):
            fixture = existing(id_only=True)
            next(iter(fixture["settings"].values()))[0]["value"] = foreign_id
            self.failure(fixture, "Foreign export storage")

    def test_archive_name_and_id_must_agree(self):
        fixture = existing()
        next(iter(fixture["settings"].values()))[1]["value"] = "stanother"
        self.failure(fixture, "name/ID mismatch")

    def test_storage_child_id_is_not_an_account(self):
        fixture = existing(id_only=True)
        next(iter(fixture["settings"].values()))[0]["value"] += "/blobServices/default"
        self.failure(fixture, "Invalid export storage account name")

    def test_existing_configured_storage_must_actually_exist(self):
        fixture = existing()
        fixture["resources"] = fixture["resources"][1:]
        self.failure(fixture, "Previously configured export storage is missing")

    def test_saved_adopted_storage_cannot_be_recreated_after_group_deletion(self):
        self.success(existing())
        self.failure({}, "Previously configured export storage is missing")

    def test_conflicting_app_and_deployment_outputs_fail_closed(self):
        fixture = existing()
        fixture["deployments"] = [{"name": "cloudlens-stack", "properties": {"outputs": {
            "storageAccount": {"value": "stother"},
            "storageAccountId": {"value": STORAGE_SCOPE + "stother"},
        }}}]
        self.failure(fixture, "Conflicting export storage identities")

    def test_stack_output_recovery_without_local_state_or_settings(self):
        fixture = {"exists": True, "resources": [resource("Microsoft.Storage/storageAccounts", "stold")],
                   "deployments": [{"name": "cloudlens-stack", "properties": {"outputs": {
                       "storageAccount": {"value": "stold"},
                       "storageAccountId": {"value": STORAGE_SCOPE + "stold"},
                   }}}]}
        self.assertEqual(self.success(fixture)["name"], "stold")

    def test_failed_deployment_parameter_recovers_reserved_name(self):
        fixture = {"exists": True, "deployments": [{"name": "cloudlens-stack", "properties": {
            "parameters": {"storageAccountName": {"value": "streserved"}}}}]}
        self.assertEqual(self.success(fixture)["name"], "streserved")

    def test_private_endpoint_cannot_be_repointed(self):
        fixture = existing()
        fixture["resources"].append(resource("Microsoft.Network/privateEndpoints", "CloudLens-pe-blob-prod"))
        fixture["endpoint"] = {"privateLinkServiceConnections": [{"privateLinkServiceId": STORAGE_SCOPE + "stold"}]}
        self.failure(fixture, "Conflicting export storage identities")

    def test_unowned_or_ambiguous_accounts_require_review(self):
        for accounts in (
            [resource("Microsoft.Storage/storageAccounts", "stunrelated")],
            [resource("Microsoft.Storage/storageAccounts", name, tags={"application": "CloudLens"})
             for name in ("stone", "sttwo")],
        ):
            self.failure({"exists": True, "resources": accounts}, "Ambiguous or unowned existing storage")

    def test_reviewed_legacy_name_is_scoped_and_adopted(self):
        fixture = {"exists": True, "resources": [resource("Microsoft.Storage/storageAccounts", "stlegacy")]}
        self.assertEqual(self.success(fixture, name="stlegacy")["name"], "stlegacy")

    def test_new_override_cannot_bypass_existing_legacy_storage(self):
        fixture = {"exists": True, "resources": [resource("Microsoft.Storage/storageAccounts", "stlegacy")]}
        self.failure(fixture, "Refusing to replace it", name="stnewreplacement")

    def test_tagged_legacy_account_is_recovered(self):
        fixture = {"exists": True, "resources": [resource("Microsoft.Storage/storageAccounts", "stlegacy",
                                                         tags={"application": "CloudLens"})]}
        self.assertEqual(self.success(fixture)["name"], "stlegacy")

    def test_unrelated_nested_resource_ids_do_not_break_recovery(self):
        fixture = existing()
        fixture["resources"].append({"type": "Microsoft.CognitiveServices/accounts/projects",
                                     "name": "aif/proj",
                                     "id": f"{SCOPE}/providers/Microsoft.CognitiveServices/accounts/aif/projects/proj"})
        self.assertEqual(self.success(fixture)["name"], "cloudlensstrv0913")

    def test_failed_reads_do_not_create_naming_state(self):
        for command in ("group exists", "resource list", "webapp config appsettings list", "deployment group list"):
            self.failure({**existing(), "failRead": command}, "Azure CLI read failed")
            self.assertFalse(self.state.exists())

    def test_state_cannot_be_reused_in_another_scope_or_with_new_resource_names(self):
        self.success()
        original = json.loads(self.state.read_text(encoding="utf-8-sig"))
        for key, value, message in (("scope", "/subscriptions/foreign/resourceGroups/fixture", "scope/version mismatch"),
                                    ("namePrefix", "other", "prefix/suffix")):
            self.state.write_text(json.dumps({**original, key: value}), encoding="utf-8")
            self.failure({}, message)

    def test_invalid_explicit_names_fail_before_any_azure_operation(self):
        for name in ("UpperCase", "st-with-dashes", "a", "a" * 25):
            self.failure({}, "Invalid export storage account name", name=name)


class TemplateStorageIdentity(unittest.TestCase):
    def test_bicep_requires_explicit_stable_name_without_rotating_defaults(self):
        entry = (ROOT / "infra/main.bicep").read_text(encoding="utf-8")
        stack = (ROOT / "infra/modules/stack.bicep").read_text(encoding="utf-8")
        for source in (entry, stack):
            self.assertRegex(source, r"param storageAccountName string\s*\n")
            self.assertNotRegex(source, r"param storageAccountName string\s*=")
            self.assertNotIn("newGuid(", source)
        self.assertIn("storageAccountName: storageAccountName", entry)
        self.assertIn("name: storageAccountName", stack)
        self.assertIn("value: storage.id", stack)
        self.assertIn("privateLinkServiceId: storage.id", stack)

    def test_generated_arm_requires_the_same_parameter(self):
        template = ROOT / "infra/main.json"
        if not template.exists():
            self.skipTest("Compile infra/main.bicep first to verify the generated ARM artifact.")
        arm = json.loads(template.read_text(encoding="utf-8-sig"))
        parameter = arm["parameters"]["storageAccountName"]
        self.assertNotIn("defaultValue", parameter)
        self.assertEqual((parameter["minLength"], parameter["maxLength"]), (3, 24))
        module = next(resource for resource in arm["resources"] if resource["name"] == "cloudlens-stack")
        self.assertEqual(module["properties"]["parameters"]["storageAccountName"]["value"],
                         "[parameters('storageAccountName')]")
        nested = module["properties"]["template"]
        storage = next(resource for resource in nested["resources"]
                       if resource["type"] == "Microsoft.Storage/storageAccounts")
        self.assertEqual(storage["name"], "[parameters('storageAccountName')]")

    def test_terraform_requires_upgrade_decision_and_pins_random_or_legacy_name(self):
        source = (ROOT / "infra/terraform/main.tf").read_text(encoding="utf-8")
        variable = source.split('variable "storage_account_name" {', 1)[1].split('variable "', 1)[0]
        self.assertNotIn("default", variable)
        self.assertIn('resource "random_id" "export_storage"', source)
        self.assertIn("byte_length = 10", source)
        self.assertIn("ignore_changes = [input]", source)
        self.assertIn("local.storage_account_name == terraform_data.export_storage_identity.output", source)
        self.assertIn("storageAccountName                 = { value = terraform_data.export_storage_identity.output }", source)
        self.assertNotIn("uuid()", source)


if __name__ == "__main__":
    unittest.main()
