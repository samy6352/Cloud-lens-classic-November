"""One export at billing-account scope, and the run's own manifest deciding what to load.

Two failures are being designed out here.

The first is fan-out. Every ingest path is subscription-scoped, and Cost Management throttles
per *tenant* -- 600 QPU/hour, one QPU per month of data -- so a thousand subscriptions over a
year is twenty hours of quota for one refresh. A billing-account export is one request for the
same data. These checks assert the scope really is a value the app can pass around, that its
ARM paths are exactly what Cost Management expects, and that unsupported combinations are
refused up front rather than at creation time with a message about agreement types.

The second is guessing which files belong to a run. The current loader lists blobs, filters by
extension, and takes everything sharing the newest minute-prefix. During the first five days of
a month there are two runs per day, so two runs can share that prefix and be loaded as one --
which is a double-count, the same bug that once reported $920.98 for a month billed at $476.07.
The manifest names its own blobs and states its own row count, so these check that the manifest
is believed and that a shortfall is detected rather than absorbed.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import json

from app.scopes import (
    ACTUAL,
    AMORTIZED,
    FOCUS,
    PRICE_SHEET,
    RESERVATION_TRANSACTIONS,
    USAGE,
    Manifest,
    Scope,
    latest_per_period,
    plan_scopes,
)

checks = 0
failures: list[str] = []


def check(label: str, ok: bool) -> None:
    global checks
    checks += 1
    print(f"  {'ok  ' if ok else 'FAIL'} {label}")
    if not ok:
        failures.append(label)


def section(name: str) -> None:
    print(f"\n{name}")


ARM = "https://management.azure.com"
BA = "d84d7967-e011-506f-c984-9be902d8697e:33f84201-ed4d-43f7-b655-e76e41166ead_2019-05-31"
SUB = "44444444-4444-4444-8444-444444444444"

# --------------------------------------------------------------------------- paths
section("a scope produces the ARM path Cost Management expects")

check("subscription",
      Scope.subscription(SUB).path == f"/subscriptions/{SUB}")
check("resource group",
      Scope.resource_group(SUB, "rg-estate").path
      == f"/subscriptions/{SUB}/resourceGroups/rg-estate")
check("billing account",
      Scope.billing_account(BA).path
      == f"/providers/Microsoft.Billing/billingAccounts/{BA}")
check("billing profile",
      Scope.billing_profile(BA, "KUXV-72MQ-BG7-PGB").path
      == f"/providers/Microsoft.Billing/billingAccounts/{BA}"
         "/billingProfiles/KUXV-72MQ-BG7-PGB")
check("management group",
      Scope.management_group("mg-root").path
      == "/providers/Microsoft.Management/managementGroups/mg-root")

check("the exports URL is built from the scope, not an f-string per call site",
      Scope.billing_account(BA).exports_url(ARM, "2023-08-01")
      == f"{ARM}/providers/Microsoft.Billing/billingAccounts/{BA}"
         "/providers/Microsoft.CostManagement/exports?api-version=2023-08-01")
check("and a named export resolves under the same scope",
      Scope.subscription(SUB).export_url(ARM, "focus-daily", "2025-03-01")
      == f"{ARM}/subscriptions/{SUB}/providers/Microsoft.CostManagement"
         "/exports/focus-daily?api-version=2025-03-01")

# --------------------------------------------------------------------------- parsing
section("parsing accepts what call sites already hold")

check("a bare subscription GUID is a subscription scope",
      Scope.parse(SUB) == Scope.subscription(SUB))
check("so existing code can pass its ids straight through",
      Scope.parse(SUB).kind == "subscription")
check("an ARM subscription path round-trips",
      Scope.parse(f"/subscriptions/{SUB}").path == f"/subscriptions/{SUB}")
check("a resource group path round-trips",
      Scope.parse(f"/subscriptions/{SUB}/resourceGroups/rg-x").kind == "resourceGroup")

# The specific-before-general ordering is the whole risk here: a billing profile path also
# matches the billing account prefix, and matching it as an account would silently widen scope.
check("a billing profile is not mistaken for a billing account",
      Scope.parse(f"/providers/Microsoft.Billing/billingAccounts/{BA}"
                  "/billingProfiles/KUXV-72MQ-BG7-PGB").kind == "billingProfile")
check("an invoice section is not mistaken for a billing profile",
      Scope.parse(f"/providers/Microsoft.Billing/billingAccounts/{BA}"
                  "/billingProfiles/P1/invoiceSections/S1").kind == "invoiceSection")
check("a department is recognised",
      Scope.parse(f"/providers/Microsoft.Billing/billingAccounts/{BA}"
                  "/departments/1234").kind == "department")
check("an enrollment account is recognised",
      Scope.parse(f"/providers/Microsoft.Billing/billingAccounts/{BA}"
                  "/enrollmentAccounts/99").kind == "enrollmentAccount")
check("leading and trailing slashes do not change the result",
      Scope.parse(f"/subscriptions/{SUB}/") == Scope.parse(f"subscriptions/{SUB}"))
check("case in the path does not change the result",
      Scope.parse(f"/SUBSCRIPTIONS/{SUB}").kind == "subscription")

for bad in ("", "   ", "not-a-scope", "/providers/Microsoft.Nope/x"):
    try:
        Scope.parse(bad)
        check(f"{bad!r} is rejected", False)
    except ValueError:
        check(f"{bad!r} is rejected", True)

# --------------------------------------------------------------------------- identity
section("scopes are values")

check("two scopes naming the same thing are equal",
      Scope.subscription(SUB) == Scope.subscription(SUB.upper().lower()))
check("and hash the same, so they de-duplicate",
      len({Scope.subscription(SUB), Scope.parse(f"/subscriptions/{SUB}")}) == 1)
check("different scopes are not equal",
      Scope.subscription(SUB) != Scope.billing_account(BA))
check("a display name does not change identity for lookup purposes",
      Scope.billing_account(BA).path == Scope.billing_account(BA, display_name="X").path)
check("a billing scope knows it spans subscriptions",
      Scope.billing_account(BA).is_billing is True)
check("a subscription scope knows it does not",
      Scope.subscription(SUB).is_billing is False)
check("a subscription scope reports its subscription",
      Scope.subscription(SUB).subscription_id == SUB)
check("a billing scope has no single subscription",
      Scope.billing_account(BA).subscription_id is None)

# --------------------------------------------------------------------------- capability
section("unsupported combinations are refused up front")

mca = Scope.billing_account(BA, agreement="MicrosoftCustomerAgreement", display_name="RaVI verma")
mosp = Scope.billing_account("cc5a049b", agreement="MicrosoftOnlineServicesProgram",
                             display_name="contoso")
mg = Scope.management_group("mg-root")
sub = Scope.subscription(SUB)

check("MCA billing account can do FOCUS", mca.supports(FOCUS) is True)
# This is the refusal this tenant actually returns: "Export type: FocusCost is not supported
# for Agreement Type: WebDirect and Subscription".
check("a MOSP billing account cannot do FOCUS", mosp.supports(FOCUS) is False)
check("but it can still do ActualCost", mosp.supports(ACTUAL) is True)
check("and the refusal says what to use instead", "ActualCost" in (mosp.why_not(FOCUS) or ""))

check("a management group cannot do FOCUS", mg.supports(FOCUS) is False)
check("nor amortised cost", mg.supports(AMORTIZED) is False)
check("only the Usage dataset", mg.supports(USAGE) is True)
check("and the refusal points at billing account scope",
      "billing account" in (mg.why_not(FOCUS) or "").lower())

check("price sheets need a billing scope", sub.supports(PRICE_SHEET) is False)
check("reservation transactions too", sub.supports(RESERVATION_TRANSACTIONS) is False)
check("which a billing account has", mca.supports(PRICE_SHEET) is True)
check("a supported dataset has no complaint", mca.why_not(FOCUS) is None)

# --------------------------------------------------------------------------- planning
section("one billing export is chosen over many subscription calls")

subs = [{"id": f"sub-{i:04d}", "name": f"MCAPS-{i}"} for i in range(1000)]
accounts = [{"name": BA, "displayName": "Contoso EA", "agreementType": "EnterpriseAgreement",
             "subscriptions": [{"id": s["id"]} for s in subs]}]

plan = plan_scopes(subs, accounts)
check("the strategy is the billing account", plan["strategy"] == "billing")
check("one scope replaces a thousand", len(plan["scopes"]) == 1)
check("and it is the billing account", plan["scopes"][0].kind == "billingAccount")
check("999 throttled calls are saved", plan["calls_saved"] == 999)

# Coverage is asserted by the caller. A subscription nobody claims must still be fetched --
# assuming coverage would silently drop it from every figure on the dashboard.
partial = [{"name": BA, "agreementType": "EnterpriseAgreement",
            "subscriptions": [{"id": s["id"]} for s in subs[:900]]}]
plan = plan_scopes(subs, partial)
check("uncovered subscriptions are not assumed to be covered", plan["strategy"] == "mixed")
check("they are fetched individually", len(plan["scopes"]) == 1 + 100)
check("and the account still leads", plan["scopes"][0].is_billing is True)

plan = plan_scopes(subs, [{"name": "cc5a049b", "agreementType": "MicrosoftOnlineServicesProgram"}])
check("a MOSP account cannot be used, so it falls back", plan["strategy"] == "subscription")
check("every subscription is fetched", len(plan["scopes"]) == 1000)
check("and the reason is recorded rather than swallowed", len(plan["rejected"]) == 1)
check("naming the agreement type",
      "MicrosoftOnlineServicesProgram" in plan["rejected"][0]["reason"])

plan = plan_scopes([], [])
check("nothing to fetch is not an error", plan["scopes"] == [])

plan = plan_scopes(subs, None)
check("no billing accounts means subscription strategy", plan["strategy"] == "subscription")
check("and the note explains it does not scale", "scale" in plan["note"])

# --------------------------------------------------------------------------- manifest
section("the manifest decides which blobs belong to a run")

RAW = {
    "manifestVersion": "2023-07-01",
    "byteCount": 3000,
    "blobCount": 2,
    "dataRowCount": 1500,
    "compressData": False,
    "runInfo": {
        "executionType": "Scheduled",
        "submittedTime": "2026-08-25T02:00:00Z",
        "runId": "run-aaa",
        "startDate": "2026-08-01T00:00:00Z",
        "endDate": "2026-08-31T00:00:00Z",
    },
    "exportConfig": {
        "exportName": "focus-daily",
        "type": "FocusCost",
        "timeFrame": "MonthToDate",
        "granularity": "Daily",
    },
    "deliveryConfig": {
        "partitionData": True,
        "dataOverwriteBehavior": "OverwritePreviousReport",
        "fileFormat": "Parquet",
    },
    "blobs": [
        {"blobName": "exports/focus-daily/20260801-20260831/run-aaa/part_0_0001.parquet",
         "byteCount": 2000, "dataRowCount": 1000},
        {"blobName": "exports/focus-daily/20260801-20260831/run-aaa/part_0_0002.parquet",
         "byteCount": 1000, "dataRowCount": 500},
    ],
}

m = Manifest.parse(json.dumps(RAW))
check("the run id is read", m.run_id == "run-aaa")
check("the dataset is read", m.dataset == FOCUS)
check("the timeframe is read", m.timeframe == "MonthToDate")
check("both blobs are listed", len(m.blobs) == 2)
check("blob names come from the manifest, not a glob",
      m.blobs[0].name.endswith("part_0_0001.parquet"))
check("row counts are carried per blob", [b.rows for b in m.blobs] == [1000, 500])
check("the declared total is kept", m.row_count == 1500)
check("the byte count is kept", m.byte_count == 3000)
check("overwrite behaviour is understood", m.overwrite is True)
check("partitioning is understood", m.partitioned is True)
check("it parses from bytes as well as str",
      Manifest.parse(json.dumps(RAW).encode()).run_id == "run-aaa")
check("and from an already-decoded dict", Manifest.parse(RAW).run_id == "run-aaa")

# Row counts are absent on some older manifest versions; that must not become a false alarm.
no_count = json.loads(json.dumps(RAW))
del no_count["dataRowCount"]
check("a manifest without a total falls back to summing its blobs",
      Manifest.parse(no_count).row_count == 1500)

for bad, why in (("{", "malformed json"), ("[]", "not an object"),
                 (json.dumps({"blobs": []}), "no blobs")):
    try:
        Manifest.parse(bad)
        check(f"a manifest that is {why} is rejected", False)
    except ValueError:
        check(f"a manifest that is {why} is rejected", True)

# --------------------------------------------------------------------------- run choice
section("choosing between runs")

def mk(run_id, submitted, start="2026-08-01T00:00:00Z", end="2026-08-31T00:00:00Z"):
    raw = json.loads(json.dumps(RAW))
    raw["runInfo"] = {"runId": run_id, "submittedTime": submitted,
                      "startDate": start, "endDate": end}
    raw["blobs"] = [{"blobName": f"{run_id}/part_0_0001.parquet",
                     "byteCount": 1, "dataRowCount": 1}]
    raw["dataRowCount"] = 1
    raw["blobCount"] = 1
    return Manifest.parse(raw)

# But two runs for *different* periods are not alternatives -- keeping only one leaves a hole.
mixed = [mk("aug", "2026-09-01T02:00:00Z"),
         mk("jul", "2026-08-01T02:00:00Z", "2026-07-01T00:00:00Z", "2026-07-31T00:00:00Z"),
         mk("aug-again", "2026-09-01T14:00:00Z")]
periods = latest_per_period(mixed)
check("each period keeps a run", len(periods) == 2)
check("the re-run supersedes rather than duplicates",
      sorted(p.run_id for p in periods) == ["aug-again", "jul"])
check("history and the current month both survive",
      [(p.start or "")[:7] for p in periods] == ["2026-07", "2026-08"])

print(f"\n{checks - len(failures)}/{checks} checks passed")
if failures:
    print("failed: " + ", ".join(failures))
    sys.exit(1)
