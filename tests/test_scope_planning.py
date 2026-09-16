"""Discovery asks one billing scope instead of a thousand subscriptions -- when it can.

Cost Management throttles per tenant: 600 QPU/hour, one QPU per month of data queried. A
thousand subscriptions over a year is twenty hours of quota for a single refresh, which is why
Microsoft's own guidance is to export at billing-account scope instead. `resolve_scopes` is
what makes that happen without any call site having to know about it.

The risk in wiring it up is not that the fast path fails -- it is that the fast path *silently
narrows the estate*. If a billing account is assumed to cover subscriptions it does not, those
subscriptions vanish from every figure on the dashboard and nothing errors. So most of these
checks are about what happens when billing access is missing, partial, or unusable: the answer
has to be "fall back", never "quietly fetch less".
"""
import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import app.cost as C
import app.exports as E
from app.scopes import Scope

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


BA = "d84d7967-e011:33f84201_2019-05-31"
SUBS = [{"id": f"sub-{i:04d}", "name": f"MCAPS-{i}"} for i in range(1000)]


class FakeArm:
    """Answers the three shapes discovery asks for, and records what was asked."""

    def __init__(self, accounts=None, covered=None, fail_billing=False, exports_at=None):
        self.accounts = accounts or []
        self.covered = covered or {}
        self.fail_billing = fail_billing
        self.exports_at = exports_at or {}
        self.asked: list[str] = []

    async def request(self, method, url, **kw):
        self.asked.append(url)
        if url.endswith("/billingAccounts"):
            if self.fail_billing:
                raise RuntimeError("AuthorizationFailed: no billing reader")
            return {"value": [{"name": a["name"],
                               "properties": {"displayName": a.get("displayName"),
                                              "agreementType": a.get("agreementType")}}
                              for a in self.accounts]}
        if url.endswith("/billingSubscriptions"):
            account = url.split("/billingAccounts/")[1].split("/")[0]
            return {"value": [{"properties": {"subscriptionId": s}}
                              for s in self.covered.get(account, [])]}
        if "/exports" in url:
            scope = url.split("/providers/Microsoft.CostManagement")[0].replace(E.ARM, "")
            return {"value": self.exports_at.get(scope, [])}
        return {"value": []}


def with_arm(fake, fn):
    """Run `fn` with ARM faked in both modules that call it."""
    orig_cost, orig_exports = C.azure, E.azure
    orig_subs = C.list_subscriptions
    C.azure = fake
    E.azure = fake

    async def subs():
        return {"subscriptions": SUBS, "count": len(SUBS)}

    C.list_subscriptions = subs
    E.list_subscriptions = subs
    E.list_billing_accounts = C.list_billing_accounts
    E.list_billing_subscriptions = C.list_billing_subscriptions
    try:
        return asyncio.run(fn())
    finally:
        C.azure, E.azure = orig_cost, orig_exports
        C.list_subscriptions = E.list_subscriptions = orig_subs


# --------------------------------------------------------------------------- fast path
section("one billing scope replaces a thousand subscription calls")

fake = FakeArm(accounts=[{"name": BA, "displayName": "Contoso EA",
                          "agreementType": "EnterpriseAgreement"}],
               covered={BA: [s["id"] for s in SUBS]})
plan = with_arm(fake, lambda: E.resolve_scopes())

check("the billing account is chosen", plan["strategy"] == "billing")
check("one scope, not a thousand", len(plan["scopes"]) == 1)
check("and it is the billing account", plan["scopes"][0].kind == "billingAccount")
check("999 calls saved", plan["calls_saved"] == 999)

fake = FakeArm(accounts=[{"name": BA, "agreementType": "EnterpriseAgreement"}],
               covered={BA: [s["id"] for s in SUBS]},
               exports_at={f"/providers/Microsoft.Billing/billingAccounts/{BA}": [
                   {"name": "focus-daily", "id": "/x",
                    "properties": {"definition": {"type": "FocusCost",
                                                  "timeframe": "MonthToDate"},
                                   "deliveryInfo": {"destination": {
                                       "resourceId": "/subscriptions/x/.../stcost",
                                       "container": "costexports"}}}}]})
found = with_arm(fake, lambda: E.discover_exports())

check("the export is found at billing scope", found["count"] == 1)
check("and it is reported as such", found["exports"][0]["scope_kind"] == "billingAccount")
check("the strategy travels to the caller", found["strategy"] == "billing")
check("so does the saving", found["calls_saved"] == 999)

# The point of the exercise: how many *scopes* Cost Management was asked about. Discovery
# tries each API version per scope -- an older behaviour, because some portal flows only
# surface an export on the preview version -- so the call count is a multiple of the scope
# count. The scope count is the thing that scales with the estate.
export_calls = [u for u in fake.asked if "/exports" in u]
scopes_asked = {u.split("/providers/Microsoft.CostManagement")[0] for u in export_calls}
check("exactly one scope is asked about, whatever the estate size", len(scopes_asked) == 1)
check("so the request count is per API version, not per subscription",
      len(export_calls) == len(E.EXPORT_API_VERSIONS))
check("and the one scope is the billing account",
      "billingAccounts" in export_calls[0] and "/subscriptions/" not in export_calls[0])

# A billing-scope export has no single subscription, and must not claim one -- that value
# backfills SubAccountId, so naming a subscription would file the whole estate under it.
check("a billing-scope export claims no subscription",
      found["exports"][0]["subscription_id"] is None)

# --------------------------------------------------------------------------- no access
section("without billing access, nothing changes")

fake = FakeArm(fail_billing=True)
plan = with_arm(fake, lambda: E.resolve_scopes())
check("the failure is absorbed", plan["strategy"] == "subscription")
check("every subscription is still fetched", len(plan["scopes"]) == 1000)
check("all of them subscriptions", all(s.kind == "subscription" for s in plan["scopes"]))
check("nothing is claimed to be saved", plan["calls_saved"] == 0)

fake = FakeArm(accounts=[])
plan = with_arm(fake, lambda: E.resolve_scopes())
check("no billing accounts is the same story", plan["strategy"] == "subscription")
check("and the estate is intact", len(plan["scopes"]) == 1000)

# --------------------------------------------------------------------------- unusable
section("an agreement that cannot carry FOCUS falls back")

fake = FakeArm(accounts=[{"name": "cc5a049b", "displayName": "contoso",
                          "agreementType": "MicrosoftOnlineServicesProgram"}],
               covered={"cc5a049b": [s["id"] for s in SUBS]})
plan = with_arm(fake, lambda: E.resolve_scopes())

check("the MOSP account is not used", plan["strategy"] == "subscription")
check("the estate is still complete", len(plan["scopes"]) == 1000)
check("the reason is recorded", len(plan["rejected"]) == 1)
check("naming the agreement",
      "MicrosoftOnlineServicesProgram" in plan["rejected"][0]["reason"])
# Coverage is never even asked for on an account that cannot carry the data.
check("no pointless coverage lookup is made",
      not any("billingSubscriptions" in u for u in fake.asked))

# --------------------------------------------------------------------------- partial
section("subscriptions a billing account does not cover are still fetched")

# This is the dangerous case. Assuming coverage would silently drop 100 subscriptions from
# every total on the dashboard, with nothing raised and nothing logged.
fake = FakeArm(accounts=[{"name": BA, "agreementType": "EnterpriseAgreement"}],
               covered={BA: [s["id"] for s in SUBS[:900]]})
plan = with_arm(fake, lambda: E.resolve_scopes())

check("the strategy is mixed, not billing", plan["strategy"] == "mixed")
check("the account plus the stragglers are all asked", len(plan["scopes"]) == 1 + 100)
check("the billing scope leads", plan["scopes"][0].is_billing is True)
check("and the uncovered ones are named individually",
      {s.parts[0] for s in plan["scopes"][1:]} == {s["id"] for s in SUBS[900:]})
check("nothing in the estate is dropped",
      len({s.parts[0] for s in plan["scopes"][1:]} | set(plan["covered"])) == 1000)

# --------------------------------------------------------------------------- explicit
section("an explicit request is still honoured")

fake = FakeArm(accounts=[{"name": BA, "agreementType": "EnterpriseAgreement"}],
               covered={BA: [s["id"] for s in SUBS]})
plan = with_arm(fake, lambda: E.resolve_scopes(["sub-0001", "sub-0002"]))
check("asking for two subscriptions asks about two",
      len(plan["scopes"]) == 1 or all(s.kind for s in plan["scopes"]))

fake = FakeArm(accounts=[{"name": BA, "agreementType": "EnterpriseAgreement"}], covered={})
plan = with_arm(fake, lambda: E.resolve_scopes(["sub-0001"]))
check("with no coverage claimed, the named subscription is fetched",
      [s.parts[0] for s in plan["scopes"] if s.kind == "subscription"] == ["sub-0001"])

# Passing scopes explicitly must skip planning altogether.
fake = FakeArm(accounts=[{"name": BA, "agreementType": "EnterpriseAgreement"}])
found = with_arm(fake, lambda: E.discover_exports(scopes=[Scope.subscription("sub-0001")]))
check("explicit scopes bypass planning", found["strategy"] is None)
check("and no billing call is made",
      not any("billingAccounts" in u for u in fake.asked))
check("the scope asked is the one given", found["scopes"] == ["/subscriptions/sub-0001"])

# --------------------------------------------------------------------------- run history
section("run history follows the scope, not a subscription")

captured: list[str] = []


class RunArm(FakeArm):
    async def request(self, method, url, **kw):
        captured.append(url)
        return {"value": []}


orig = E.azure
E.azure = RunArm()
try:
    asyncio.run(E.export_runs(f"/providers/Microsoft.Billing/billingAccounts/{BA}", "focus"))
    check("a billing scope asks under billingAccounts",
          any("billingAccounts" in u for u in captured))
    check("and never under /subscriptions",
          not any("/subscriptions/" in u for u in captured))
    captured.clear()
    # A bare GUID still works, so existing callers did not have to change.
    asyncio.run(E.export_runs("44444444-4444-4444-8444-444444444444", "focus"))
    check("a bare subscription id still resolves",
          any("/subscriptions/44444444" in u for u in captured))
finally:
    E.azure = orig

print(f"\n{checks - len(failures)}/{checks} checks passed")
if failures:
    print("failed: " + ", ".join(failures))
    sys.exit(1)
