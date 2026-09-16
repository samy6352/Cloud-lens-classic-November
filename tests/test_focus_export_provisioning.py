"""The provisioning script must derive the same export names the app does.

infra/focus-exports.ps1 creates the Cost Management exports CloudLens imports from. The app
recognises an export as its own by hashing the destination and comparing the name
(schedules.owned), so the two derivations have to agree exactly — in a different language, in a
different process, at a different time.

If they ever drift the failure is silent and expensive: the script reports success, Azure
writes the reports, and CloudLens ignores them as another deployment's data. Nothing errors.
Somebody notices weeks later that Hourly Billing is still empty.

These tests read the real script rather than a copy of it, so editing either side breaks them.
"""
from __future__ import annotations

import hashlib
import re
from pathlib import Path

import pytest

from app import schedules

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "infra" / "focus-exports.ps1"


@pytest.fixture(scope="module")
def script() -> str:
    assert SCRIPT.exists(), "the FOCUS export provisioning script is missing"
    return SCRIPT.read_text(encoding="utf-8")


def powershell_name(account_id: str, container: str, folder: str, script: str) -> str:
    """Re-derive a name the way the script does, using the script's own literals.

    The algorithm is read out of the file — the key separator, the hash, the prefix and the
    truncation — so a change to any of them shows up here rather than in production.
    """
    body = re.search(r"function Get-ExportName \{.*?\n\}", script, re.S)
    assert body, "Get-ExportName is gone from the script"
    text = body.group(0)

    assert "ToLowerInvariant().TrimEnd('/')" in text, "account id is no longer lower-cased"
    assert "SHA256" in text, "the hash changed"

    prefix = re.search(r'return "([a-z-]+)\$\(', text)
    assert prefix, "the export name prefix could not be read"

    length = re.search(r"Substring\(0,\s*(\d+)\)", text)
    assert length, "the truncation length could not be read"

    key = f"{account_id.lower().rstrip('/')}|{container}|{folder}"
    digest = hashlib.sha256(key.encode()).hexdigest()[: int(length.group(1))]
    return f"{prefix.group(1)}{digest}"


ACCOUNT = ("/subscriptions/11111111-1111-1111-1111-111111111111/resourceGroups/rg"
           "/providers/Microsoft.Storage/storageAccounts/cloudlensstore")


@pytest.mark.parametrize("subscription", [
    "22222222-2222-2222-2222-222222222222",
    "e3d73cb0-f276-4d3f-8a06-fa56eefe7ba0",
    "AAAAAAAA-BBBB-CCCC-DDDD-EEEEEEEEEEEE",   # mixed case must fold identically
])
def test_the_script_and_the_app_agree_on_the_name(script, subscription, monkeypatch):
    monkeypatch.setattr(schedules, "FOCUS_CONTAINER", "focus-exports")
    monkeypatch.setattr(schedules, "FOCUS_PREFIX", "focus")

    import app.archive as archive
    monkeypatch.setattr(archive, "ACCOUNT_ID", ACCOUNT)

    destination = schedules.destination(subscription)
    assert schedules.export_name("FocusCost", **destination) == powershell_name(
        destination["account_id"], destination["container"], destination["folder"], script)


def test_a_differing_destination_gives_a_differing_name(script, monkeypatch):
    """The hash is what isolates one deployment's exports from another's."""
    monkeypatch.setattr(schedules, "FOCUS_CONTAINER", "focus-exports")
    monkeypatch.setattr(schedules, "FOCUS_PREFIX", "focus")

    import app.archive as archive
    monkeypatch.setattr(archive, "ACCOUNT_ID", ACCOUNT)
    mine = schedules.export_name("FocusCost", **schedules.destination(
        "22222222-2222-2222-2222-222222222222"))

    monkeypatch.setattr(archive, "ACCOUNT_ID", ACCOUNT.replace("cloudlensstore", "otherstore"))
    theirs = schedules.export_name("FocusCost", **schedules.destination(
        "22222222-2222-2222-2222-222222222222"))

    assert mine != theirs, "two deployments would claim each other's exports"


def test_the_script_writes_what_the_app_expects_to_read(script):
    """Every field schedules.owned() checks has to be set by the script."""
    assert "'FocusCost'" in script, "the export type must be FocusCost"
    assert "type = 'AzureBlob'" in script
    assert "resourceId = $ArchiveAccountId" in script
    assert "container = $Container" in script
    assert "rootFolderPath = $folder" in script
    # The folder layout the app derives in schedules.destination.
    assert '"$Prefix/$($sub.ToLowerInvariant())".Trim(\'/\')' in script


def test_the_script_uses_the_api_version_the_app_uses(script):
    """A version skew shows up as 'invalid definition type', which reads like a scope problem."""
    found = re.search(r"\$EXPORT_API = '([\d-]+)'", script)
    assert found, "the script does not pin an export API version"
    assert found.group(1) == schedules.EXPORT_API, (
        f"script pins {found.group(1)}, app pins {schedules.EXPORT_API}")


def test_the_script_grants_the_role_the_app_would_have_granted(script):
    """Storage Blob Data Contributor, by id — the export identity can do nothing without it."""
    found = re.search(r"\$BLOB_CONTRIBUTOR = '([0-9a-f-]+)'", script)
    assert found, "the script does not pin the storage role"
    assert found.group(1) == schedules.BLOB_CONTRIBUTOR


def test_the_script_is_safe_to_re_run(script):
    """Re-running is how an operator fixes a partial failure, so it must not duplicate a grant."""
    assert "role', 'assignment', 'list'" in script, "the grant is not checked before it is made"
    assert "already granted" in script


def test_the_script_can_be_rehearsed(script):
    """-WhatIf has to reach the writes, or a rehearsal proves nothing."""
    assert "SupportsShouldProcess" in script
    assert "$PSCmdlet.ShouldProcess" in script


# ------------------------------------------------------- wired into deployment

# A script nobody runs fixes nothing. The fault this addresses is that a deployment could
# finish looking healthy — the dashboard loads, Quick refresh works — while the views that
# need hourly detail stayed empty, and that was discovered weeks later. So deployment
# provisions exports by default and says which state it left behind.

@pytest.fixture(scope="module")
def deploy() -> str:
    path = ROOT / "infra" / "deploy.ps1"
    assert path.exists()
    return path.read_text(encoding="utf-8")


def test_deployment_provisions_exports_by_default(deploy):
    assert "focus-exports.ps1" in deploy, "deployment never provisions the data path"
    assert "if (-not $SkipFocusExports)" in deploy, (
        "provisioning must be the default, not an opt-in nobody discovers")
    assert "[switch]$SkipFocusExports" in deploy, "there must be a way to defer it"


def test_deployment_provisions_the_scopes_it_granted_cost_access_to(deploy):
    """Exporting a subscription CloudLens cannot read would produce data nobody can see."""
    assert "$costAccess.subscriptions" in deploy
    assert "$_.validation -eq 'valid'" in deploy


def test_a_failed_provision_does_not_fail_the_deployment(deploy):
    """The stack is complete and usable without exports; only the detail views are empty."""
    start = deploy.index("Provisioning FOCUS exports")
    section = deploy[start:deploy.index('Step "Configuring sign-in"', start)]
    assert "try {" in section and "} catch {" in section
    assert "Write-Warning" in section
    assert "throw" not in section, "a missing export must not abort a working deployment"


def test_the_deployment_says_which_state_it_left_behind(deploy):
    """'It looks fine' is the wrong signal when the hourly views are empty."""
    tail = deploy[deploy.index('Step "Done"'):]
    assert "$focusProvisioned" in tail, "the summary does not report the data path"
    assert "Hourly Billing" in tail, "the summary does not say what is affected"
    assert "focus-exports.ps1" in tail, "the summary does not say how to fix it"


def test_every_path_sets_the_flag_the_summary_reads(deploy):
    """An unset variable would make a skipped provision read as a successful one."""
    section = deploy[deploy.index("if (-not $SkipFocusExports)"):deploy.index('Step "Done"')]
    assert section.count("$focusProvisioned = $true") == 1
    assert section.count("$focusProvisioned = $false") == 2, (
        "both the failure and the -SkipFocusExports paths must set it")
