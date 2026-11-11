"""Build an allowlisted customer deployment handoff without importing the app or using Azure."""
from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
INFRA = (
    "main.bicep", "bicepconfig.json", "deploy.ps1", "preflight.ps1",
    "deployment-checks.ps1", "delegated-consent.ps1", "cost-access.ps1",
    "grant-cost-access.ps1", "storage-naming.ps1", "focus-exports.ps1",
    "modules/identity.bicep", "modules/postgres-admin.bicep", "modules/stack.bicep",
    "sql/create-app-role.sql",
)

DOCUMENTS = {
    "DEPLOYMENT.md": r"""# Customer deployment
This handoff is prepared, not customer-accepted. No customer resources were provisioned
or tenant approval granted while building it. Stop before provisioning until every
customer input and approval in READINESS.md is recorded.

Use PowerShell 7, Azure CLI with Bicep available, and Python 3.11+. Install the
pinned requirements only for local application operation; App Service builds them
through Oryx. Extract the entire ZIP into a dedicated working directory.

The identity team must create a dedicated single-tenant AzureADMyOrg registration:
isFallbackPublicClient=true, publicClient.redirectUris containing exactly the intended
HTTPS /auth/callback, no web callbacks, credentials, implicit grants, roles or exposed
API. Request ARM resource 797f4846-ba00-4fd7-ba43-dac1f8f63013 delegated scope
41094075-9dad-400e-a0bd-54e686782033 (user_impersonation). Only basic OIDC/Graph
sign-in permissions are allowed in addition. Review all permissions before the tenant
administrator grants approval; never blanket-consent an unrelated registration.
Create the customer service principal, enable it, and approve a tenant-wide
AllPrincipals ARM user_impersonation OAuth grant. Requested permissions and directory
roles alone do not prove consent. User assignment/Conditional Access policies remain
the identity team's responsibility and require a real test-user sign-in.

The subscription operator needs hosting resource-creation/bootstrap-RBAC permissions
and read access to Graph applications, service principals and oauth2PermissionGrants.
They do NOT need a directory admin role or permission to edit the preapproved app.
If Graph reads are denied, stop and arrange approved verification access; never
skip verification. The installer will not edit or consent this registration.

From the root of the fully extracted, SHA256-verified customer ZIP, run ONE line:

```powershell
pwsh -NoProfile -File .\install.ps1
```

The bundled guided installer checks prerequisites without installing them, prompts
for approved tenant/subscriptions, regions, model/SKU/capacity, hosting SKUs,
client ID, admins and isolated network CIDRs. It prints all effective parameters
(including deploy defaults); PREVIEW authorizes sign-in and read-only checks.
The existing deploy script enforces exact delegated consent and preflight, then
runs What-If. Only a successful preview followed by the exact word DEPLOY permits
provisioning with the same inputs. Ctrl+C or CANCEL stops; cancellation during apply
does not undo resources already created. No silent fallback or skip is offered.
Resolve all preflight warnings with the customer before DEPLOY. Existing storage
name state must be retained for redeployment.

Obtain this ZIP through your approved delivery channel and verify its SHA256
against the separately provided checksum. No public download endpoint is asserted;
do not use remote unsigned script execution/iex. This command runs the local
bundled installer, not a hosted bootstrap. Advanced changes to non-prompted defaults
require a separately reviewed infra/deploy.ps1 invocation, with matching preview/apply.

GlobalStandard is a model SKU, not a residency promise. Approve an appropriate
model/SKU/capacity explicitly; pass -ModelName, -ModelSku and -ModelCapacity
consistently to preview and apply when changing defaults. Approve private address
ranges and pass the same VNet/subnet parameters to preview and apply.
No automatic PostgreSQL SKU substitution is configured. -PostgresSkuFallback
may contain ONLY customer-approved alternatives, with pricing/performance reviewed.
-SkipPreflight bypasses capability, quota, network/policy and RBAC checks, not
delegated consent validation. It is not approval and does not resolve blockers.
Never use it in ordinary onboarding. Unknown/warning results require owner resolution.

Managed identity remains necessary for scheduled/background ingestion, storage,
PostgreSQL and AI access. User delegated tokens are session-bound and cannot power
unattended jobs. Reader and Cost Management Reader are separately verified/granted
on each selected scope. Export provisioning requires separate approved operator
permissions; -SkipFocusExports explicitly defers it and leaves data acceptance incomplete.
-DeferCostAccess leaves scope grants pending, not successful. Do not grant the
runtime subscription Owner to compensate. Export-backed reporting is NOT the
customer workaround; EXPORT_REPORTING_ENABLED stays unset/false.
""",
    "READINESS.md": """# Readiness and acceptance
Status: LOCAL RELEASE PREPARATION ONLY; CUSTOMER ACCEPTANCE PENDING.

Required customer decisions: tenant, hosting/cost subscriptions, resource group,
names, app/database region, AI region/model/SKU/capacity and residency approval,
network prefixes/private DNS, budget, identity owner, application admins,
scope RBAC owner, export/storage owner, retention and backup/RPO/RTO owners.

Before provisioning:
- Confirm exact app/service principal, public-client callback, tenant-wide ARM grant.
- Resolve all preflight blockers and unverified warnings; confirm actual model quota.
- Approve What-If, SKUs and any explicitly listed alternatives, network and cost.
- Decide access assignments and Conditional Access; consent does not grant user RBAC.

After provisioning (record dated evidence, never tokens):
- /readyz returns ready; anonymous / challenges; cookies secure and local login off.
- Real assigned user completes delegated ARM sign-in; access_mode is delegated.
- Permitted subscription is visible; unauthorized user/scope is denied, including API.
- Validate a user with fewer scopes and expired/revoked-session behavior.
- MI scope roles, PostgreSQL, private DNS/storage and AI data-plane grants work.
- Run initial ingestion/refresh and an unattended scheduled cycle without user tokens.
- Reconcile agreed date/currency/scope totals against Azure Cost Management.
- Verify FOCUS export delivery/blob read and detailed tabs if contracted.
- Test one AI question and grounded answer, including cost/usage telemetry.
- Test backup restore, rollback and monitoring; obtain customer acceptance sign-off.

Compilation, offline tests and packaging prove none of these live acceptance items.
No customer provisioning, consent, real sign-in, ingestion or AI validation is
performed by the release builder.
""",
    "HANDOFF.md": """# Deployment handoff record
Complete in the customer's protected change record, not in source control:
release ZIP SHA256 and manifest source hashes; tenant/subscriptions; approved
regions/residency/model/SKUs; RG/names/network; app client/object/service-principal
IDs and exact callback; consent verifier/date; assigned-user policy; application
admins; runtime MI principal; grants by scope; export delivery/storage ownership;
PostgreSQL schema/bootstrap privileges; backup/retention settings; deployment ID;
storage naming-state location; acceptance evidence and outstanding owners/dates.

Preserve the generated storage-name state and cost-access report securely for
redeployments; never regenerate names for an existing stack. Reports are not
effective-access or ingestion proof. Do not include passwords, tokens, auth stores,
customer exports or database contents in handoff attachments.

Go/no-go is the customer's responsibility. Until READINESS.md live checks have
evidence and approval, describe this release as prepared, not end-to-end ready.
""",
    "RUNBOOK.md": """# Operations and recovery
Monitor /readyz, authenticated failures, ingestion freshness/job failures,
PostgreSQL storage/connections, AI latency/quota and Azure spend/alerts. Health
does not prove fresh billing, authorized visibility or useful AI responses.

If consent/Conditional Access fails, stop and involve the identity owner. Reverify
the exact registration and OAuth grant; never enable local login, change tenant
audience, disable auth or silently switch to MI user authorization. If a scope is
missing, distinguish user RBAC from runtime MI ingestion coverage. Verify the
active runtime principal, scope roles, export target and private DNS; don't grant
Owner or use another deployment's storage.

For incidents retain sanitized deployment/job/request IDs, times and errors.
Do not log tokens or export customer billing data into support ZIPs.

Before updates, retain the previous verified code ZIP, reviewed app settings,
storage-name state and PostgreSQL backup/restore point. Re-run preflight and
What-If with unchanged identity/mode/names and approved inputs. Serialize changes.
Schema changes may be additive and cannot be reversed by a code rollback.
Restore prior code/config only under a reviewed change; never drop tables,
delete resources, rotate storage identity or overwrite a database as rollback.
Use a separately restored database and isolated rehearsal for restore validation,
then verify readiness, auth, access isolation, ingestion and AI again.

Consent revocation, role changes and session caches may require session expiry
and an approved application restart; verify access is denied after revocation.
Backups, alerts, retention, support ownership and restore rehearsal must be
approved and tested before production acceptance.
""",
}


def digest(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def source_bytes(relative: str) -> bytes:
    source = ROOT / relative
    for path in (source, *source.parents):
        if path.is_symlink() or getattr(path, "is_junction", lambda: False)():
            raise ValueError(f"Linked release source: {relative}")
        if path == ROOT:
            break
    return source.read_bytes()


def verify_current(output: Path) -> None:
    """Reject internally valid but stale handoffs before they are distributed."""
    with zipfile.ZipFile(output) as archive:
        manifest = json.loads(archive.read("customer-manifest.json"))
        for item in manifest["files"]:
            name = item["path"]
            content = archive.read(name)
            if digest(content) != item["sha256"]:
                raise ValueError(f"Manifest verification failed: {name}")
            if (name.startswith(("app/", "web/", "infra/", "tools/")) or name in {
                "requirements.txt", "install.ps1",
            }) and content != source_bytes(name):
                raise ValueError(f"Release source is stale: {name}")
        packaged = {item["path"] for item in manifest["files"]}
        for source in (ROOT / "app").rglob("*.py"):
            if "__pycache__" not in source.parts and source.relative_to(ROOT).as_posix() not in packaged:
                raise ValueError(f"Release module is missing: {source.name}")


def build(destination: Path) -> Path:
    if destination.exists():
        raise ValueError("Destination must not exist; preserve previous releases.")
    shell = shutil.which("pwsh")
    if not shell:
        raise ValueError("PowerShell 7 (pwsh) is required.")
    destination.mkdir(parents=True)
    runtime = destination / "cloudlens-app.zip"
    subprocess.run(
        [shell, "-NoProfile", "-File", str(ROOT / "tools" / "package.ps1"),
         "-Destination", str(runtime.resolve()), "-Python", sys.executable],
        check=True, cwd=ROOT,
    )
    with zipfile.ZipFile(runtime) as archive:
        entries = {item.filename: archive.read(item) for item in archive.infolist()}
    for relative in INFRA:
        entries[f"infra/{relative}"] = source_bytes(f"infra/{relative}")
    for relative in ("install.ps1", "tools/package.ps1", "tools/validate_package.py"):
        entries[relative] = source_bytes(relative)
    for name, text in DOCUMENTS.items():
        entries[name] = text.encode("utf-8")
        (destination / name).write_bytes(entries[name])
    manifest = {
        "application": "CloudLens classic customer deployment",
        "formatVersion": 1,
        "acceptance": "pending customer sign-in, ingestion and AI validation",
        "accessPlan": "tenant-admin-approved delegated ARM; separate MI background access",
        "files": [
            {"path": name, "bytes": len(content), "sha256": digest(content)}
            for name, content in sorted(entries.items())
        ],
    }
    entries["customer-manifest.json"] = json.dumps(manifest, indent=2).encode("utf-8")
    output = destination / "cloudlens-customer.zip"
    with zipfile.ZipFile(output, "x", compression=zipfile.ZIP_DEFLATED) as archive:
        for name, content in sorted(entries.items()):
            info = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
            archive.writestr(info, content, compress_type=zipfile.ZIP_DEFLATED)
    with zipfile.ZipFile(output) as archive:
        if archive.testzip() is not None:
            raise ValueError("ZIP integrity failed")
        for item in manifest["files"]:
            if digest(archive.read(item["path"])) != item["sha256"]:
                raise ValueError(f"Manifest verification failed: {item['path']}")
    (destination / "customer-manifest.json").write_bytes(entries["customer-manifest.json"])
    checksums = {path.name: digest(path.read_bytes()) for path in (runtime, output)}
    (destination / "SHA256SUMS").write_text(
        "".join(f"{value}  {name}\n" for name, value in checksums.items()), encoding="utf-8"
    )
    verify_current(output)
    print(json.dumps({"path": str(output.resolve()), "sha256": checksums[output.name]}))
    return output


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    target = parser.add_mutually_exclusive_group(required=True)
    target.add_argument("--destination", type=Path)
    target.add_argument("--verify-current", type=Path)
    args = parser.parse_args()
    if args.verify_current:
        verify_current(args.verify_current.resolve())
    else:
        build(args.destination.resolve())
