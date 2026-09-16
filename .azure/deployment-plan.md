# CloudLens Azure feed-filter release - 15 September 2026

## 1. Status
Deployed

## 2. Approval and target
User approved all planned phases on 14 September and explicitly requested deployment to
https://cloudlens.site on 15 September at 10:32 IST, followed by this focused request at
13:35 IST: service-type filters for Azure Updates/Status and region filtering for Service Health.
- Existing app: cloudlens-web-rv0913, resource group cloud-lens-cx, Central India.
- Subscription: e3d73cb0-f276-4d3f-8a06-fa56eefe7ba0 (MCAPS-Hybrid-REQ-73673-2024-raviverma).
- No infrastructure, database, settings, schedule, role, region or subscription changes.
- Existing restore database and earlier validated package remain available for rollback.
- The former unrelated administrator-login plan is preserved in session files/prior-deployment-plan.txt.

## 3. Recipe
recipe.type: azcli
Source-only ZIP deployment to existing Linux Python 3.13 App Service, using its existing
Oryx build. Azure CLI validates the target; approved AAD-authenticated Kudu ZIP deployment
is used if the CLI publication path rejects the valid caller. Basic publishing remains disabled.
Do not reapply infrastructure or create an azd environment for this non-azd code update.

## 4. Release scope
- Source-local service filtering for Azure Updates and Azure Status, region filtering
  for Service Health, combined filtering and independent Clear filters.
- Source-provided product-tag aliases, region spelling normalization and explicit missing metadata.
- Selections survive source refresh, collapse and cached navigation. No additional Azure
  requests occur when changing filters. Public feeds stay global; private health stays scoped.
- No cost-data setup, financial calculations, database schema, schedules or dependencies change.
- Preserve existing Microsoft/local authentication, domain, callbacks, user identity,
  subscriptions, secrets and storage destinations.
- Optional nonsecret identity/cache/warmup metadata only; inspect current values before merging.
- Starting stopped databases once for deployment does not change nightly MCAPS governance.

## 5. Execution and rollback
- Run targeted public-feed normalization/cache/scope and frontend filter regressions.
- Exercise actual renderer/state-preservation code in an isolated browser with mocked sources.
- Build a source-only checksummed package. This narrow, non-schema filter release uses
  targeted browser/source validation rather than restarting the temporary database/staging stack.
- Publish the same validated source package to production; wait for the new container and
  compare served content, not just the deployment-accepted response.
- Source rollback artifact: validated session files/cloudlens-cost-data-final-20260915.zip
  (production source f175949, deployment record 28dc474).
- PITR restore was compared to production before modifications: exact 13,556 cost-row multiset.
- On failure, keep/redeploy the prior code without dropping additive metadata or altering roles.
- Nightly database shutdown and billing-product access remain separate operational limitations.

## 6. All validation checks pass
- [x] Core: CLI/auth, exact existing target, enabled Python runtime and role declarations.
- [x] Application: targeted parser, source cache/scope and frontend filter regressions.
- [x] Build: existing Python/JavaScript tooling and validated source-only package.
- [x] Static roles: reviewed changed IaC; no infrastructure/RBAC deployment in this release.
- [x] Azure policy: preserve managed-service governance, authentication and private database.
- [x] Docker build, ARM deployment validate and infrastructure what-if: not applicable;
  this release does not provision/update templates or deploy containers.
- [x] Record current validation proof before setting Validated.

## 7. Validation Proof
15 September 2026, focused feed-filter release:
- `az webapp show` confirmed the same Running, HTTPS-only CloudLens app in Central India;
  production PostgreSQL is Ready. Authentication, roles and configuration remain unchanged.
- `pytest tests/test_feed_filters.py tests/test_dashboard_frontend.py`: 13 passed.
- `unittest tests.test_azure_updates tests.test_service_health`: 35 passed.
- Existing Playwright fixture style with real modules/DOM preservation: 20 assertions passed,
  including no filter-triggered Azure calls, combined filters, missing data, failed refresh,
  disappearing selections, scope isolation, cached return and mobile overflow.
- Ruff, JavaScript syntax, diff whitespace and source-package validation passed.
- Package: 112 source files, 1,031,384 bytes;
  SHA256 8a95f465fedb2929b5a35ef96dbfe44263f79299da51308e3009b67da2a4e531.
- Expected asset bundle: f8002ab288c48d65ad96155430c9802607898a959b2d3c53c6455987b51e5ce4.
- No broad roles, source-data writes, database migrations, dependency changes or governance
  changes are part of this release. Earlier full-release proof is retained in Git and session files.

## 8. Deployment results
- Production deployment bcb5c943-f471-46f5-99de-7ec535aa67c5 succeeded and is active.
- /readyz returns 200; served asset bundle matches the validated package:
  f8002ab288c48d65ad96155430c9802607898a959b2d3c53c6455987b51e5ce4.
- Real Microsoft sign-in succeeded. All three requested dropdowns are present.
- Live Azure Updates filter selected three App Service notices; Service Health selected seven
  Central India events, merging compact/human spellings. Twenty-eight notices remain explicitly
  Not specified. Selecting/clearing filters made no new source requests.
- Azure Status currently returns zero public incidents; its service filter is present and uses
  metadata from retrieved incidents rather than a fabricated service catalog.
- Source commit 6de0801 pushed to v3/feat-resource-explorer. No settings, roles, database,
  schedules, exports or dependencies were changed.
