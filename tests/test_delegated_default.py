"""Prove managed-identity defaults and guide consent for explicit delegated ARM.

The choice is a factual claim about what a person sees. Asking Azure *as this app*
which subscriptions carry a role assignment is bounded by what the app's own identity can read,
so someone entitled to thirty subscriptions can be shown four and the dashboard will look
perfectly healthy while missing money. Holding the person's own token has no such bound.

Explicit delegation requires a one-time administrator approval; until it is granted nobody can sign in at
all. That is a fine trade only if the approval is impossible to miss, so this also checks the
three places it is offered: on the sign-in page before anyone is refused, in the startup log,
and at the callback when Entra does refuse.
"""
import importlib
import logging
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
for s in (sys.stdout, sys.stderr):
    try:
        s.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

os.environ.setdefault("PROJECT_ENDPOINT", "https://example.invalid/api/projects/test")

TENANT = "55555555-5555-4555-8555-555555555555"

fails: list[str] = []


def check(label: str, condition: bool, detail: str = "") -> None:
    if condition:
        print(f"  OK   {label}")
    else:
        print(f"  FAIL {label}{': ' + detail if detail else ''}")
        fails.append(label)


def fresh_sso(**env):
    keys = ["AUTH_CLIENT_ID", "AUTH_TENANT_ID", "AUTH_DELEGATED_ARM", "AUTH_HOME_TENANT",
            "AUTH_HIDE_WORK_SIGNIN", "AUTH_CLIENT_SECRET", "AUTH_REDIRECT_URI"]
    saved = {k: os.environ.get(k) for k in keys}
    try:
        for k in keys:
            os.environ.pop(k, None)
        os.environ.update(env)
        from app import entra
        importlib.reload(entra)
        return entra.EntraSSO(), entra
    finally:
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


ONE = {"AUTH_CLIENT_ID": "abc", "AUTH_TENANT_ID": TENANT}


# ------------------------------------------------------------------ the default
print("=" * 72)
print("managed identity is the default; delegated ARM requires explicit opt-in")
sso, entra = fresh_sso(**ONE)
check("delegated ARM is off by default", sso.delegated is False)
check("so sign-in does not ask for an Azure token",
      entra.ARM_SCOPE not in sso.scopes, str(sso.scopes))

for spelling in ("", "false", "False", "0", "no"):
    s, _ = fresh_sso(**ONE, AUTH_DELEGATED_ARM=spelling)
    check(f"AUTH_DELEGATED_ARM={spelling!r} turns it off", s.delegated is False)
for spelling in ("true", "1", "yes"):
    s, _ = fresh_sso(**ONE, AUTH_DELEGATED_ARM=spelling)
    check(f"AUTH_DELEGATED_ARM={spelling!r} leaves it on", s.delegated is True)

s, _ = fresh_sso(**ONE, AUTH_DELEGATED_ARM="false")
check("switching it off drops the ARM scope", s.scopes == [], str(s.scopes))

# The combination that would sign every external user in and show them nothing.
try:
    fresh_sso(AUTH_CLIENT_ID="abc", AUTH_TENANT_ID="organizations", AUTH_DELEGATED_ARM="false")
    check("multi-tenant without delegation is still refused", False, "no error raised")
except RuntimeError as exc:
    check("multi-tenant without delegation is still refused", True)
    check("…and says why", "delegated" in str(exc).lower(), str(exc)[:80])

# Multi-tenant deployments must opt in explicitly.
try:
    multi, _ = fresh_sso(AUTH_CLIENT_ID="abc", AUTH_TENANT_ID="organizations")
    check("multi-tenant without explicit delegation is refused", False, "no error raised")
except RuntimeError as exc:
    check("multi-tenant without explicit delegation is refused",
          "AUTH_DELEGATED_ARM=true" in str(exc), str(exc)[:80])
multi, _ = fresh_sso(AUTH_CLIENT_ID="abc", AUTH_TENANT_ID="organizations",
                     AUTH_DELEGATED_ARM="true")
check("multi-tenant with explicit delegation works", multi.delegated is True)


# ------------------------------------------------------------------ consent state
print("\n" + "=" * 72)
print("the approval is offered until it demonstrably works, then never again")
sso, entra = fresh_sso(**ONE, AUTH_DELEGATED_ARM="true")
from app.warehouse import warehouse  # noqa: E402

warehouse.set_meta(entra.CONSENT_KEY, "not-granted")
sso, entra = fresh_sso(**ONE, AUTH_DELEGATED_ARM="true")
check("a fresh install asks for approval", sso.consent_needed is True)

sso.record_consent()
check("a successful delegated sign-in settles it", sso.consent_needed is False)
check("…durably", warehouse.get_meta(entra.CONSENT_KEY) == "granted")

after_restart, _ = fresh_sso(**ONE, AUTH_DELEGATED_ARM="true")
check("and it stays settled across a restart", after_restart.consent_needed is False)

# Nothing to approve when the app never asks for the scope.
nodel, _ = fresh_sso(**ONE, AUTH_DELEGATED_ARM="false")
check("no approval is asked for when delegation is off", nodel.consent_needed is False)
off, _ = fresh_sso()
check("nor when SSO is off entirely", off.consent_needed is False)

warehouse.set_meta(entra.CONSENT_KEY, "not-granted")


# ------------------------------------------------------------------ where it is offered
print("\n" + "=" * 72)
print("the three places the approval is put in front of someone")

# 1. The startup log, with the actual link rather than a description of it.
records: list[str] = []


class Capture(logging.Handler):
    def emit(self, record):
        records.append(record.getMessage())


logger = logging.getLogger("cloudlens.entra")
handler = Capture()
logger.addHandler(handler)
logger.setLevel(logging.INFO)
try:
    fresh_sso(**ONE, AUTH_DELEGATED_ARM="true",
              AUTH_REDIRECT_URI="https://cloudlens.contoso.com/auth/callback")
    startup = " ".join(records)
    check("startup names the consent endpoint",
          "https://cloudlens.contoso.com/auth/admin-consent" in startup, startup[:120])
    check("…and warns that sign-in fails until it is granted",
          "refused" in startup.lower())

    records.clear()
    fresh_sso(**ONE, AUTH_DELEGATED_ARM="false")
    check("turning delegation off warns about the silent undercount",
          any("silently" in r for r in records), " | ".join(records)[:160])
finally:
    logger.removeHandler(handler)

# 2. The sign-in page, via /api/auth/me.
os.environ["AUTH_SECRET"] = "test-secret-not-used-anywhere-real"
os.environ["AUTH_CLIENT_ID"] = "77777777-7777-4777-8777-777777777777"
os.environ["AUTH_TENANT_ID"] = TENANT
os.environ["AUTH_REDIRECT_URI"] = "http://localhost:8100/auth/callback"
for leftover in ("AUTH_DISABLED", "AUTH_ALLOW_LOCAL", "AUTH_PASSWORD", "AUTH_USERS",
                 "AUTH_API_TOKENS", "AUTH_CLIENT_SECRET", "AUTH_DELEGATED_ARM",
                 "AUTH_HOME_TENANT", "AUTH_HIDE_WORK_SIGNIN"):
    os.environ[leftover] = ""
os.environ["AUTH_DELEGATED_ARM"] = "true"

from fastapi.testclient import TestClient  # noqa: E402

from app import main  # noqa: E402

importlib.reload(main)
client = TestClient(main.app)

me = client.get("/api/auth/me").json()
check("/api/auth/me tells the page approval is outstanding",
      me.get("consent_needed") is True, str(me))

page = (ROOT / "web" / "login.html").read_text(encoding="utf-8")
check("the page reads it", "me.consent_needed" in page)
check("…and offers our own endpoint, not a URL from the query string",
      'href="/auth/admin-consent"' in page)
check("…and stands down when a refusal is already explaining the same thing",
      'me.consent_needed && !params.get("error")' in page)

# 3. The callback, when Entra refuses anyway.
res = client.get("/auth/callback?error=access_denied&error_description=access_denied&state=x",
                 follow_redirects=False)
loc = res.headers.get("location", "")
check("a refusal lands on the sign-in page", loc.startswith("/login?"), loc[:60])
# This is the case the old condition missed: it keyed on multi_tenant, and a deployment inside
# one customer's directory is not that — so the most likely fresh install got a dead end.
check("a single-directory refusal still offers the approval", "consent=1" in loc, loc[:120])
check("…with a sentence rather than an OAuth code",
      "administrator" in loc.lower(), loc[:120])

res = client.get("/auth/admin-consent", follow_redirects=False)
target = res.headers.get("location", "")
check("the approval link goes to Entra's admin consent endpoint",
      "/v2.0/adminconsent" in target, target[:80])
check("…for this directory", f"/{TENANT}/" in target, target[:80])
check("…asking for the scope that is actually missing",
      "user_impersonation" in target, target[:160])


print("\n" + "=" * 72)
if fails:
    print(f"  {len(fails)} failed: " + "; ".join(fails))
else:
    print("  all checks passed")
sys.exit(1 if fails else 0)
