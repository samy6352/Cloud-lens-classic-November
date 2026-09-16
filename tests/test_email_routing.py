"""Prove the email decides which Microsoft authority a person is sent to, and nothing else.

An externally published CloudLens has two doors into Microsoft and they are not
interchangeable. Azure Resource Manager is an Entra-only resource, so asking for an ARM scope
makes the multi-tenant endpoints refuse a personal Microsoft account outright — even though
personal accounts routinely own the Visual Studio and pay-as-you-go subscriptions this app
exists to explain. Those accounts get in only through a named tenant's authority.

A visitor cannot possibly know which of those applies to them. Their email address does, so the
page asks for it and routes on it. This checks the routing, and checks that the address — which
is reflected into an authority URL — cannot carry anything else along with it.
"""
# cloudlens-test: requires-network
#
# Building MSAL's client fetches the authority's OpenID metadata, so this needs
# outbound internet even though the routing decisions themselves are computed locally.
import asyncio
import importlib
import os
import sys
from pathlib import Path
from urllib.parse import parse_qs, urlparse

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
for s in (sys.stdout, sys.stderr):
    try:
        s.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

os.environ.setdefault("PROJECT_ENDPOINT", "https://example.invalid/api/projects/test")

# Both are used as authorities, and MSAL resolves an authority by fetching its OpenID
# metadata -- so these have to be directories that exist, not placeholders. Microsoft's
# own tenant is the default because it is published in Microsoft's public documentation.
# CLOUDLENS_TEST_TENANT overrides the one standing in for "a single named directory".
HOME = os.environ.get("CLOUDLENS_TEST_TENANT", "72f988bf-86f1-41af-91ab-2d7cd011db47")
# The same directory serves both, because they are never used in one
# configuration: HOME is the named fallback of a multi-tenant deployment,
# ONE_TENANT is the sole directory of a single-tenant one.
ONE_TENANT = HOME

fails: list[str] = []


def check(label: str, condition: bool, detail: str = "") -> None:
    if condition:
        print(f"  OK   {label}")
    else:
        print(f"  FAIL {label}{': ' + detail if detail else ''}")
        fails.append(label)


def fresh_sso(**env):
    """An EntraSSO built against a given environment, since the flags are read in __init__."""
    keys = ["AUTH_CLIENT_ID", "AUTH_TENANT_ID", "AUTH_DELEGATED_ARM",
            "AUTH_HOME_TENANT", "AUTH_HIDE_WORK_SIGNIN", "AUTH_CLIENT_SECRET"]
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


EXTERNAL = {"AUTH_CLIENT_ID": "abc", "AUTH_TENANT_ID": "organizations",
            "AUTH_DELEGATED_ARM": "true", "AUTH_HOME_TENANT": HOME}


# ------------------------------------------------------------------ the address itself
print("=" * 72)
print("what counts as an address we will put in a URL")
_, entra = fresh_sso(**EXTERNAL)

for good in ("alice@contoso.com", "Alice@Contoso.COM", "  bob@fabrikam.co.uk  ",
             "first.last+tag@sub.domain.example", "<carol@contoso.com>"):
    check(f"accepts {good!r}", entra.normalise_email(good) != "", repr(good))

check("lowercases what it returns",
      entra.normalise_email("Alice@Contoso.COM") == "alice@contoso.com")
check("strips surrounding whitespace",
      entra.normalise_email("  bob@fabrikam.co.uk  ") == "bob@fabrikam.co.uk")
check("strips the angle brackets a mail client adds",
      entra.normalise_email("<carol@contoso.com>") == "carol@contoso.com")

# The reason the shape is checked at all. Each of these would otherwise be reflected into the
# authority URL, and the last three are attempts to add a parameter to it or to split a header.
for bad, why in [
    ("", "empty"),
    ("nobody", "no domain"),
    ("@contoso.com", "no local part"),
    ("alice@contoso", "no dot in the domain"),
    ("alice contoso@x.com", "contains a space"),
    ("alice@contoso.com&prompt=none", "smuggles a second parameter"),
    ("alice@contoso.com\nSet-Cookie: x=1", "smuggles a newline"),
    ("alice@contoso.com#fragment", "smuggles a fragment"),
    ("alice@contoso.com?x=1", "smuggles a query"),
    ("a" * 80 + "@contoso.com", "unbounded local part"),
    ("alice@" + "d" * 250 + ".com", "unbounded domain"),
]:
    check(f"refuses {why}", entra.normalise_email(bad) == "", repr(bad))


# ------------------------------------------------------------------ classification
print("\n" + "=" * 72)
print("which addresses the multi-tenant ARM endpoints will refuse")
for consumer in ("someone@outlook.com", "someone@hotmail.com", "someone@live.com",
                 "someone@gmail.com", "someone@yahoo.co.in", "someone@icloud.com"):
    check(f"{consumer} is a consumer account", entra.is_consumer_email(consumer))
for work in ("alice@contoso.com", "ravi@microsoft.com", "x@fabrikam.onmicrosoft.com"):
    check(f"{work} is a work account", not entra.is_consumer_email(work))

check("an unknown domain is assumed to be a work directory",
      not entra.is_consumer_email("someone@a-company-nobody-listed.example"))


# ------------------------------------------------------------------ the routing decision
print("\n" + "=" * 72)
print("an externally published deployment routes on the address")
sso, _ = fresh_sso(**EXTERNAL)
check("email-first is available when both destinations exist", sso.email_first is True)
check("a work address goes to the multi-tenant endpoint",
      sso.route_for("alice@contoso.com")[0] == "")
check("a personal address goes to the named directory",
      sso.route_for("someone@outlook.com")[0] == HOME)
check("no address at all falls back to the multi-tenant endpoint",
      sso.route_for("")[0] == "")

print("\n" + "=" * 72)
print("asking still earns its place with only one directory to send anyone to")
one, _ = fresh_sso(AUTH_CLIENT_ID="abc", AUTH_TENANT_ID=ONE_TENANT)
# The normal customer deployment. There is no authority to choose, but the hint still skips
# Entra's account picker, which is the whole "smooth" part.
check("a single-directory deployment still asks", one.email_first is True)
check("and sends every address to its one directory",
      one.route_for("someone@outlook.com")[0] == ONE_TENANT
      and one.route_for("alice@contoso.com")[0] == ONE_TENANT)

nohome, _ = fresh_sso(AUTH_CLIENT_ID="abc", AUTH_TENANT_ID="organizations",
                      AUTH_DELEGATED_ARM="true")
check("multi-tenant with no home tenant still asks", nohome.email_first is True)
check("but cannot invent a directory for a personal account",
      nohome.route_for("someone@outlook.com")[0] == "")

off, _ = fresh_sso()
check("an unconfigured deployment does not ask", off.email_first is False)

optout, _ = fresh_sso(**EXTERNAL, AUTH_ASK_EMAIL="false")
check("AUTH_ASK_EMAIL=false turns the question off", optout.email_first is False)
check("…without changing where an address would have been routed",
      optout.route_for("someone@outlook.com")[0] == HOME)
for spelling in ("0", "no", "False"):
    s, _ = fresh_sso(**EXTERNAL, AUTH_ASK_EMAIL=spelling)
    check(f"AUTH_ASK_EMAIL={spelling!r} turns it off", s.email_first is False)
for spelling in ("", "true", "yes", "anything else"):
    s, _ = fresh_sso(**EXTERNAL, AUTH_ASK_EMAIL=spelling)
    check(f"AUTH_ASK_EMAIL={spelling!r} leaves it on", s.email_first is True)


# ------------------------------------------------------------------ the request to Entra
print("\n" + "=" * 72)
print("the hint reaches Entra, and replaces the account picker rather than joining it")
sso, _ = fresh_sso(**EXTERNAL)

hinted = asyncio.run(sso.start("/", login_hint="alice@contoso.com"))
q = parse_qs(urlparse(hinted).query)
check("the address is sent as login_hint", q.get("login_hint") == ["alice@contoso.com"])
# Both together produce the picker *and* discard the address, which is the opposite of the point.
check("select_account is dropped when there is a hint", "prompt" not in q)

plain = asyncio.run(sso.start("/"))
qp = parse_qs(urlparse(plain).query)
check("without a hint the picker is still asked for", qp.get("prompt") == ["select_account"])
check("and no login_hint is invented", "login_hint" not in qp)

dirty = asyncio.run(sso.start("/", login_hint="alice@contoso.com&prompt=none"))
qd = parse_qs(urlparse(dirty).query)
check("a malformed hint is dropped, not passed through", "login_hint" not in qd)
check("and cannot smuggle a parameter of its own", qd.get("prompt") == ["select_account"])

tenanted = asyncio.run(sso.start("/", tenant=HOME, login_hint="someone@outlook.com"))
check("a tenant sign-in uses that directory's authority",
      urlparse(tenanted).path.startswith(f"/{HOME}/"), urlparse(tenanted).path)
check("and still carries the hint",
      parse_qs(urlparse(tenanted).query).get("login_hint") == ["someone@outlook.com"])


# ------------------------------------------------------------------ the HTTP endpoint
print("\n" + "=" * 72)
print("/auth/login turns an address into the right redirect")

os.environ["AUTH_SECRET"] = "test-secret-not-used-anywhere-real"
os.environ.update(EXTERNAL)
os.environ["AUTH_REDIRECT_URI"] = "http://localhost:8100/auth/callback"
for leftover in ("AUTH_DISABLED", "AUTH_ALLOW_LOCAL", "AUTH_PASSWORD", "AUTH_USERS",
                 "AUTH_API_TOKENS", "AUTH_CLIENT_SECRET", "AUTH_HIDE_WORK_SIGNIN"):
    os.environ[leftover] = ""

from fastapi.testclient import TestClient  # noqa: E402

from app import main  # noqa: E402

importlib.reload(main)
client = TestClient(main.app)


def redirect_for(url: str):
    res = client.get(url, follow_redirects=False)
    return res.status_code, res.headers.get("location", "")


status, loc = redirect_for("/auth/login?email=alice%40contoso.com")
q = parse_qs(urlparse(loc).query)
check("a work address redirects", status == 303, str(status))
check("…to the multi-tenant authority",
      urlparse(loc).path.startswith("/organizations/"), urlparse(loc).path)
check("…carrying the hint", q.get("login_hint") == ["alice@contoso.com"])

status, loc = redirect_for("/auth/login?email=someone%40outlook.com")
q = parse_qs(urlparse(loc).query)
check("a personal address redirects to the named directory",
      urlparse(loc).path.startswith(f"/{HOME}/"), urlparse(loc).path)
check("…carrying the hint", q.get("login_hint") == ["someone@outlook.com"])

status, loc = redirect_for("/auth/login")
q = parse_qs(urlparse(loc).query)
check("no address still works, with the picker",
      urlparse(loc).path.startswith("/organizations/") and q.get("prompt") == ["select_account"])

status, loc = redirect_for("/auth/login?email=not%20an%20address")
q = parse_qs(urlparse(loc).query)
check("a malformed address does not refuse the sign-in", status == 303, str(status))
check("…it just falls back to the picker",
      "login_hint" not in q and q.get("prompt") == ["select_account"])

# The address picks the tenant, but it must not be able to pick *any* tenant: only the two this
# deployment already names. An explicit tenant is still checked exactly as before.
status, _ = redirect_for("/auth/login?tenant=11111111-2222-3333-4444-555555555555")
check("an unnamed directory is still refused", status == 400, str(status))

status, loc = redirect_for(f"/auth/login?tenant={HOME}&email=alice%40contoso.com")
check("an explicit tenant still wins over the address",
      urlparse(loc).path.startswith(f"/{HOME}/"), urlparse(loc).path)

# What the page needs in order to know whether to ask at all.
me = client.get("/api/auth/me").json()
check("/api/auth/me advertises email-first", me.get("email_first") is True, str(me))


print("\n" + "=" * 72)
if fails:
    print(f"  {len(fails)} failed: " + "; ".join(fails))
else:
    print("  all checks passed")
sys.exit(1 if fails else 0)
