"""Entra sign-in and per-session Azure access.

Authorization-code flow with PKCE uses MSAL on worker threads. Tokens stay in
server-side sessions; browsers receive signed opaque IDs. Subscription access is
cached briefly and refreshed from Azure, including the user's other directories.
"""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import logging
import os
import re
import secrets
import time
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import quote

log = logging.getLogger("cloudlens.entra")

AUTHORITY_HOST = os.getenv("AUTH_AUTHORITY_HOST", "https://login.microsoftonline.com")

# Delegated ARM access. Asking for this makes the app call Azure *as* the signed-in person,
# which is the strongest possible enforcement — but it is not a permission a user can consent
# to in a tenant that restricts consent to low-impact scopes, so it is opt-in. Without it the
# app signs people in with the basic OIDC scopes, which users can consent to themselves, and
# works out what each person may see by asking Azure for their role assignments.
ARM_SCOPE = "https://management.azure.com/user_impersonation"

# `organizations` accepts a work or school account from any directory. `common` nominally also
# accepts personal Microsoft accounts -- but not here, and the reason is worth stating because it
# is not obvious: Azure Resource Manager is an Entra-only resource, so asking for an ARM scope
# makes Entra restrict *both* endpoints to work accounts. A personal account typing its own
# address gets "You can't sign in here with a personal account" from either one.
#
# That matters because personal accounts routinely own exactly the subscriptions this app is
# pointed at -- Visual Studio and pay-as-you-go especially. They reach Azure as a guest member of
# a real directory, and a *tenant* authority does admit them: it federates the sign-in to
# login.live.com and issues an ordinary organisational ARM token (verified: idp=live.com,
# tid set to the organisation).
#
# So the multi-tenant endpoints are for work accounts, and anyone whose Azure access comes
# through a personal account signs in against their tenant. `authority_for` exists for exactly
# this, and AUTH_HOME_TENANT names a directory to offer them.
MULTI_TENANT = {"organizations", "common", "multi", "any"}

# Which of the two to actually send. Anything not recognised is passed through untouched, so a
# tenant GUID still means one directory.
AUDIENCE = {"multi": "organizations", "any": "common"}

# Domains whose accounts are consumer Microsoft accounts, or are not Entra identities at all.
#
# This exists to answer one question at the door: given an email address, which authority does
# this person need? A work address goes to the multi-tenant endpoint. Anything here cannot, for
# the ARM-scope reason above, and has to go to a named directory instead.
#
# The Microsoft-owned consumer domains are the ones that matter. The others are here because an
# external collaborator invited into a directory as a guest very often has a Gmail or a Yahoo
# address, and their account is a Microsoft account too — subject to the identical restriction.
CONSUMER_DOMAINS = {
    "outlook.com", "hotmail.com", "live.com", "msn.com", "passport.com", "windowslive.com",
    "hotmail.co.uk", "live.co.uk", "outlook.in", "hotmail.fr", "live.fr", "hotmail.it",
    "hotmail.de", "live.de", "hotmail.es", "live.com.au", "outlook.jp",
    "gmail.com", "googlemail.com", "yahoo.com", "yahoo.co.in", "yahoo.co.uk",
    "icloud.com", "me.com", "protonmail.com", "proton.me", "aol.com", "gmx.com", "rediffmail.com",
}

# Deliberately permissive, and deliberately bounded. This is not here to judge whether an address
# can receive mail — Entra does that, and far better. It is here because the value is reflected
# into an authority URL as `login_hint`, so it has to be one address and nothing else: no spaces,
# no newline that could split a header, no second parameter smuggled in, no unbounded length.
EMAIL_RE = re.compile(r"^[^@\s,;<>\"'\\/?&#%]{1,64}@[A-Za-z0-9.-]{1,190}\.[A-Za-z]{2,24}$")


def normalise_email(value: str) -> str:
    """Return a lowercased address, or "" if it is not one we will put in a URL."""
    email = (value or "").strip().strip("<>").lower()
    return email if EMAIL_RE.match(email) else ""


def email_domain(email: str) -> str:
    return email.rpartition("@")[2]


def is_consumer_email(email: str) -> bool:
    """Is this an address the multi-tenant ARM endpoints will refuse?

    Only ever asked of an address that already passed `normalise_email`, so the domain is real
    enough to compare. An unknown domain is assumed to be a work directory: that is both the
    common case and the safe failure, because guessing "work" and being wrong lands the person on
    Entra's own account page, whereas guessing "consumer" and being wrong sends someone else's
    employee into a directory they do not belong to.
    """
    return email_domain(email) in CONSUMER_DOMAINS


SESSION_HOURS = float(os.getenv("AUTH_SESSION_HOURS", "12"))
# Where the "a delegated sign-in has worked here" fact lives, in the warehouse's meta table.
CONSENT_KEY = "entra_delegated_consent"
SUBSCRIPTION_TTL = float(os.getenv("AUTH_RBAC_TTL_SECONDS", "300"))
# How long a snapshot may still be *served* once it is due for revalidation.
#
# Re-enumerating RBAC is an ARM round trip, and on a cold connection it measured four seconds
# against this tenant. `_refresh_locked` discards the previous answer before it asks, under a
# per-session lock, so for those four seconds every request on the page blocks -- and because
# `permitted()` is the first line of nearly every endpoint, "a tab is slow" was really "the
# five-minute RBAC TTL just lapsed". Inside this window the due snapshot is served while a
# single revalidation runs behind it; outside it, the old blocking behaviour applies and no
# stale entitlement survives. Set to 0 to revalidate synchronously as before.
SUBSCRIPTION_GRACE = float(os.getenv("AUTH_RBAC_GRACE_SECONDS", "900"))
FLOW_TTL = 600.0  # a half-finished sign-in is worth ten minutes, not forever


@dataclass
class Session:
    """One signed-in person, and the tokens that let us act as them."""

    sid: str
    name: str
    email: str
    oid: str
    tenant: str
    app: Any                      # msal.PublicClientApplication bound to this user's cache
    account: dict
    cache: Any = None             # the shared token cache, so sibling tenants reuse the refresh token
    created: float = field(default_factory=time.time)
    seen: float = field(default_factory=time.time)

    # One MSAL app per directory, built lazily. Keeping them avoids re-fetching OIDC metadata
    # on every refresh, which for someone in six tenants is six needless round trips a time.
    apps: dict = field(default_factory=dict)
    # Their Azure access: a token per tenant, and which subscription each one is good for.
    caller: Any = None

    # Their subscriptions, from ARM, with the time we asked.
    subscriptions: list[dict] | None = None
    checked: float = 0.0
    # How many subscriptions the application's own identity can read. Only meaningful in
    # app-identity mode, where it is the ceiling on what anyone can be shown: a subscription
    # CloudLens holds no Reader on cannot be inspected, so it is offered to nobody. Recorded so
    # the undercount can be stated in the UI rather than looking like an empty estate.
    app_visible: int = 0
    # Set when ARM refuses: shown to the user instead of an empty screen with no explanation.
    problem: str = ""
    # Which tenants were reachable, and which refused. A person with access in four directories
    # who sees three should be told about the fourth rather than left to wonder.
    tenants: list[dict] = field(default_factory=list)
    skipped: list[dict] = field(default_factory=list)
    refresh_lock: asyncio.Lock = field(default_factory=asyncio.Lock, repr=False)
    reauth_required: bool = False
    # The background revalidation currently running for this session, if any. One at a time:
    # a second would re-ask ARM the same question and race the first one's answer in.
    revalidating: Any = None

    @property
    def expired(self) -> bool:
        return time.time() > self.created + SESSION_HOURS * 3600

    def public(self) -> dict:
        return {"name": self.name, "email": self.email}


class EntraSSO:
    """Configured by four environment variables; inert without them.

    `AUTH_CLIENT_ID` and `AUTH_TENANT_ID` are what switch the app from local passwords to SSO.
    A client secret is optional: with one this is a confidential client, without one it is a
    public client using PKCE, which is what a tenant that caps secret lifetimes wants anyway.
    """

    def __init__(self) -> None:
        self.client_id = os.getenv("AUTH_CLIENT_ID", "").strip()
        self.tenant = os.getenv("AUTH_TENANT_ID", "").strip()
        self.secret = os.getenv("AUTH_CLIENT_SECRET", "").strip() or None
        self.redirect_uri = os.getenv(
            "AUTH_REDIRECT_URI", "http://localhost:8100/auth/callback"
        ).strip()
        # Who may run an ingest. Everyone else can read and ask, which is the common case.
        self.admins = {
            a.strip().lower()
            for a in os.getenv("AUTH_ADMINS", "").replace("\n", ",").split(",")
            if a.strip()
        }

        self.enabled = bool(self.client_id and self.tenant)
        # On by default. The alternative — asking Azure, as *this app*, who holds a role
        # assignment — is bounded by what this app's own identity can read, so a person with
        # access to thirty subscriptions sees only the ones the app also has Reader on. That
        # undercount is silent: the dashboard looks healthy and is simply missing money. For a
        # cost tool that is the worst failure available, so the honest default is the token that
        # belongs to the person asking, and Azure decides.
        #
        # The cost is a one-time administrator approval of `user_impersonation`, without which
        # nobody can sign in at all. That is why this is guided rather than merely documented:
        # `consent_needed` tells the page to offer the approval link before anyone hits the wall,
        # and a refusal at the callback offers the same link instead of a raw OAuth code.
        self.delegated = os.getenv(
            "AUTH_DELEGATED_ARM", "").strip().lower() not in ("0", "false", "no")

        # Anyone with a work account, from any directory. The alternative — a tenant GUID — is
        # still the default, and is what an app serving one organisation should keep.
        self.multi_tenant = self.tenant.lower() in MULTI_TENANT

        # A directory to offer people whose Azure access comes through a personal Microsoft
        # account. They cannot use the multi-tenant endpoints for an ARM scope (see above), but
        # a tenant authority federates them to login.live.com and works. Optional: without it
        # the app simply says why they were turned away rather than pretending it cannot happen.
        self.home_tenant = os.getenv("AUTH_HOME_TENANT", "").strip()

        # Hide the multi-tenant work-account button, leaving only the named directory.
        #
        # For a deployment whose users all sign in through one directory, the general button is
        # worse than useless: it sends everyone else into their own tenant's consent policy and
        # ends at "ask your administrator", which reads as the app being broken rather than as a
        # door that was never open to them. The route still exists for anyone holding the URL —
        # this governs what is advertised, not what is enforced. Entitlement stays with Azure.
        self.hide_work_button = os.getenv(
            "AUTH_HIDE_WORK_SIGNIN", "").lower() in ("1", "true", "yes")

        # Whether the sign-in page asks for an address before handing over to Microsoft.
        # On unless turned off — see `email_first` for why it earns its place even in a
        # deployment that has only one directory to send anyone to.
        self.ask_email = os.getenv(
            "AUTH_ASK_EMAIL", "").strip().lower() not in ("0", "false", "no")

        if self.multi_tenant and self.enabled and not self.delegated:
            # The non-delegated path asks ARM, as this app, who holds a role assignment. The
            # app's identity exists in one directory and cannot read role assignments in any
            # other, so it would return nothing for every external user while looking perfectly
            # healthy. Refuse the combination rather than ship that.
            raise RuntimeError(
                "AUTH_TENANT_ID=organizations requires AUTH_DELEGATED_ARM=true. Resolving access "
                "as the app only works inside its own tenant, so without a delegated token every "
                "external user would sign in successfully and see nothing."
            )

        self._sessions: dict[str, Session] = {}
        self._pending: dict[str, dict] = {}
        self._login_apps: dict[str, Any] = {}
        # Whether a delegated sign-in has ever succeeded here. None until first asked, then
        # cached — this is read on every render of the sign-in page and the answer only ever
        # changes once, in one direction.
        self._consent_seen: bool | None = None

        if self.enabled:
            log.info("Entra SSO enabled: tenant=%s client=%s admins=%d delegated_arm=%s "
                     "multi_tenant=%s",
                     self.tenant, self.client_id, len(self.admins), self.delegated,
                     self.multi_tenant)
            if self.delegated:
                # The one step a deployment can be complete without and still be unusable, so it
                # is said plainly, at the top, with the exact link rather than a description of
                # where to find it. Derived from the redirect URI because that is the only place
                # this app is told its own public address.
                base = self.redirect_uri.removesuffix("/auth/callback").rstrip("/")
                log.info(
                    "Each person will see their own Azure subscriptions, which needs a one-time "
                    "administrator approval of user_impersonation. Until it is granted, sign-in "
                    "is refused for everyone. An administrator of the directory grants it at "
                    "%s/auth/admin-consent — the sign-in page offers the same link.", base)
            else:
                # The opposite trade, and the one that fails quietly, so it is a warning.
                log.warning(
                    "AUTH_DELEGATED_ARM is off: access is resolved by asking Azure, as this app, "
                    "who holds a role assignment. That is bounded by what this app's own identity "
                    "can read, so anyone whose subscriptions it lacks Reader on will silently see "
                    "fewer than they are entitled to.")

    @property
    def scopes(self) -> list[str]:
        """Empty means sign-in only: MSAL still requests openid/profile/offline_access, which
        are the scopes a user is normally allowed to consent to without an administrator."""
        return [ARM_SCOPE] if self.delegated else []

    @property
    def authority(self) -> str:
        # `common` and `organizations` are real authority paths, not placeholders: Entra
        # resolves them to whichever directory the person signing in actually belongs to.
        return f"{AUTHORITY_HOST}/{self.audience}"

    @property
    def audience(self) -> str:
        tenant = self.tenant.lower()
        if tenant in MULTI_TENANT:
            return AUDIENCE.get(tenant, tenant)
        return self.tenant

    def authority_for(self, tenant: str) -> str:
        """The authority for one specific directory, used to swap a token into that tenant."""
        return f"{AUTHORITY_HOST}/{tenant}"

    # ------------------------------------------------------------- email routing
    @property
    def email_first(self) -> bool:
        """Should the page ask for an address before handing over to Microsoft?

        Wherever SSO is on. The address does two separate jobs, and a deployment inside one
        customer's directory — which is the normal case — only gets the second:

          * **It picks the authority**, where there is more than one to pick. Only a
            multi-tenant deployment with a home tenant has that choice.
          * **It becomes the `login_hint`**, so Entra goes straight to that account instead of
            showing a picker and asking for an address the person already typed here.

        The second is reason enough on its own. Gating the whole thing on the first — which is
        what this did at first — meant the single-directory deployment, the one a customer
        actually installs, fell back to a bare button and lost the smoother path for no reason.
        """
        return self.enabled and self.ask_email

    def route_for(self, email: str) -> tuple[str, str]:
        """Which authority this address should sign in against.

        Returns (tenant, reason) where an empty tenant means the multi-tenant endpoint. The
        reason is for logging and for the page to explain itself, not for a decision.
        """
        if not self.multi_tenant:
            return self.tenant, "single directory"
        if email and is_consumer_email(email) and self.home_tenant:
            # A personal Microsoft account cannot hold an ARM token from `organizations`, but the
            # same account signing in against a directory it guests in gets an ordinary
            # organisational token. This is the whole reason the address is asked for.
            return self.home_tenant, "personal Microsoft account"
        return "", "work or school account"

    def _build(self, cache: Any = None, tenant: str = "") -> Any:
        import msal

        authority = self.authority_for(tenant) if tenant else self.authority
        kwargs = {"authority": authority, "token_cache": cache}
        if self.secret:
            return msal.ConfidentialClientApplication(
                self.client_id, client_credential=self.secret, **kwargs)
        return msal.PublicClientApplication(self.client_id, **kwargs)

    def _app_for_login(self, tenant: str = "") -> Any:
        """One shared app per resolved authority, so OIDC metadata is fetched once per
        directory this deployment ever sends someone to, not once per click.

        `tenant` is the resolved authority key from `route_for` (or "" for the shared
        multi-tenant endpoint) -- not the raw email or the raw query-string tenant, both
        of which `start` has already turned into this by the time it gets here. A single-
        directory deployment (`route_for` always returning that one tenant) and a multi-
        tenant one whose visitors are almost always a work/school account routed to ""
        both end up with exactly one entry in this cache after their first sign-in; the
        rarer personal-Microsoft-account route through `home_tenant` earns a second.

        Safe to share across every visitor who resolves to the same authority: the app
        built here always gets `cache=None` (an app-local, disposable `TokenCache`), so it
        carries no per-session state -- it exists only to hand `initiate_auth_code_flow` a
        validated authority to build an authorization URL against, and that URL's own
        state/nonce/PKCE verifier come out fresh on every call regardless of which app
        object produced them. The *redemption* half of the flow (the other `_build(cache,
        tenant=...)` call sites in this file) is a different story and is deliberately left
        alone: that one carries a real, per-session token cache and must stay one object
        per session, not a shared, cached one.
        """
        if tenant not in self._login_apps:
            self._login_apps[tenant] = self._build(tenant=tenant)
        return self._login_apps[tenant]

    def is_admin(self, email: str, oid: str = "") -> bool:
        return bool(self.admins & {email.lower(), oid.lower()})

    def consent_url(self, tenant: str = "") -> str:
        """The link an administrator opens once to approve this app for their whole directory.

        This is the supported answer to 'user consent is disabled here', which is the default in
        any security-conscious tenant and is not something an application can or should route
        around. One admin clicks it once, and afterwards everyone in that directory signs in
        normally — including guests, whose access is still their own.

        `organizations` as the tenant lets the admin's own sign-in select the directory, so the
        same link works for every organisation rather than needing one per tenant.
        """
        target = tenant or (self.audience if self.multi_tenant else self.tenant)
        return (f"{AUTHORITY_HOST}/{target}/v2.0/adminconsent"
                f"?client_id={self.client_id}"
                f"&scope={quote(ARM_SCOPE + ' offline_access openid profile', safe='')}"
                f"&redirect_uri={quote(self.redirect_uri, safe='')}")

    # ----------------------------------------------------------------- consent state
    @property
    def consent_seen(self) -> bool:
        """Has a delegated sign-in ever completed on this deployment?

        Not a claim about what the directory has consented to — an app cannot read that without
        Graph permissions it has no other reason to hold. It is the one thing we can honestly
        know: somebody got an Azure token here, so the approval exists. Persisted, because the
        answer must survive the restart that follows a deployment.
        """
        if self._consent_seen is None:
            self._consent_seen = self._read_consent()
        return self._consent_seen

    @staticmethod
    def _read_consent() -> bool:
        try:
            from .warehouse import warehouse
            return warehouse.get_meta(CONSENT_KEY) == "granted"
        except Exception as exc:  # noqa: BLE001 - never let a storage problem block a sign-in
            log.debug("could not read consent state: %s", str(exc)[:200])
            return False

    def record_consent(self) -> None:
        """Remember that a delegated sign-in worked, so the setup notice stops being shown."""
        if self._consent_seen:
            return
        self._consent_seen = True
        try:
            from .warehouse import warehouse
            warehouse.set_meta(CONSENT_KEY, "granted")
        except Exception as exc:  # noqa: BLE001
            log.debug("could not record consent state: %s", str(exc)[:200])

    @property
    def consent_needed(self) -> bool:
        """Should the sign-in page offer an administrator the approval link up front?

        Delegated ARM asks for `user_impersonation`, which most directories reserve to an
        administrator. Until one approves it *nobody* can sign in — so the difference between a
        working deployment and a dead one is a single click that the person deploying is
        perfectly able to make, if only they are told about it.

        Waiting for someone to hit the wall first and then explaining is the reactive version of
        this, and it is still there for the case where the notice was missed. This is the part
        that stops the first user of a fresh install meeting a refusal at all.
        """
        return self.enabled and self.delegated and not self.consent_seen

    # ----------------------------------------------------------------- sign-in
    async def start(self, next_url: str = "/", tenant: str = "", login_hint: str = "",
                    browser_binding: str = "") -> str:
        """Begin a sign-in and return the Entra URL to send the browser to.

        MSAL generates the PKCE verifier, state and nonce; we keep the flow server-side and
        hand the browser nothing but an opaque state, so none of it is forgeable from outside.

        `tenant` overrides the authority for this one sign-in, which is how a personal-account
        holder gets in: the multi-tenant endpoints refuse them for an ARM scope, a tenant
        authority does not.

        `login_hint` is the address the visitor typed on our own page. Passing it lets Entra do
        home-realm discovery immediately instead of asking for something we were already told.
        """
        hint = normalise_email(login_hint)

        def _initiate() -> dict:
            app = self._app_for_login(tenant)
            extra: dict = {}
            if hint:
                extra["login_hint"] = hint
            else:
                # Ask Entra to name the account in the picker rather than silently reusing the
                # last one, which on a shared machine signs you in as your colleague.
                #
                # Not sent alongside a hint: `select_account` means "show the picker regardless",
                # so the two together produce the picker *and* discard the address, which is the
                # opposite of what asking for it was for. The hint pre-fills rather than
                # asserts — Entra still lets them switch account on its own page.
                extra["prompt"] = "select_account"
            return app.initiate_auth_code_flow(
                scopes=self.scopes,
                redirect_uri=self.redirect_uri,
                **extra,
            )

        flow = await asyncio.to_thread(_initiate)
        self._sweep()
        self._pending[flow["state"]] = {"flow": flow, "next": next_url, "at": time.time(),
                                        "tenant": tenant, "browser_binding": browser_binding}
        return flow["auth_uri"]


    async def complete(self, params: dict, browser_binding: str = "") -> tuple[Session | None, str, str]:
        """Finish a sign-in from the callback query string. Returns (session, next_url, error)."""
        import msal

        state = params.get("state", "")
        pending = self._pending.get(state)
        if (pending is None or not browser_binding
            or not hmac.compare_digest(pending.get("browser_binding", ""), browser_binding)
            or time.time() - pending.get("at", 0) > FLOW_TTL):
            # Also the case for a replayed callback, which is exactly what this should refuse.
            return None, "/", "That sign-in link has expired. Please try again."

        self._pending.pop(state)

        cache = msal.SerializableTokenCache()
        # Same authority the flow was started on, or MSAL refuses the redemption. For a
        # personal-account sign-in that is the tenant, not the multi-tenant endpoint.
        def _redeem() -> tuple[Any, dict, list]:
            app = self._build(cache, tenant=pending.get("tenant", ""))
            # MSAL checks state and the id_token's nonce here; a mismatch raises rather than
            # returning a token, which is the CSRF protection for the callback.
            result = app.acquire_token_by_auth_code_flow(pending["flow"], params,
                                                         scopes=self.scopes)
            return app, result, app.get_accounts()

        try:
            app, result, accounts = await asyncio.to_thread(_redeem)
        except ValueError as exc:  # MSAL's own state/nonce validation failure
            log.warning("auth code flow rejected: %s", exc)
            return None, pending["next"], "Sign-in could not be verified. Please try again."

        # Sign-in only needs to establish who they are; a delegated deployment additionally
        # needs the Azure token, so the success condition differs between the two.
        claims = result.get("id_token_claims") or {}
        ok = "access_token" in result if self.delegated else bool(claims)
        if not ok:
            detail = result.get("error_description") or result.get("error") or "unknown error"
            log.warning("token exchange failed: %s", str(detail)[:400])
            return None, pending["next"], _friendly(result)

        # An Azure token came back, so `user_impersonation` is approved in this directory. Say so
        # once and the sign-in page stops offering the administrator a link they no longer need.
        if self.delegated:
            self.record_consent()

        session = Session(
            sid=secrets.token_urlsafe(32),
            name=claims.get("name") or claims.get("preferred_username") or "signed in",
            email=(claims.get("preferred_username") or claims.get("email") or "").lower(),
            oid=claims.get("oid", ""),
            tenant=claims.get("tid", self.tenant),
            app=app,
            account=accounts[0] if accounts else {},
            cache=cache,
        )
        self._sessions[session.sid] = session
        log.info("%s signed in via Entra (oid=%s, tenant=%s)",
                 session.email or session.name, session.oid, session.tenant)
        return session, pending["next"], ""

    # ---------------------------------------------------------------- sessions
    def get(self, sid: str | None) -> Session | None:
        if not sid:
            return None
        session = self._sessions.get(sid)
        if session is None:
            return None
        if session.expired:
            self._sessions.pop(sid, None)
            return None
        session.seen = time.time()
        return session

    def drop(self, sid: str | None) -> None:
        if sid:
            self._sessions.pop(sid, None)

    def _sweep(self) -> None:
        now = time.time()
        for state, pending in list(self._pending.items()):
            if now - pending["at"] > FLOW_TTL:
                self._pending.pop(state, None)
        for sid, session in list(self._sessions.items()):
            if session.expired:
                self._sessions.pop(sid, None)

    def count(self) -> int:
        self._sweep()
        return len(self._sessions)

    # ------------------------------------------------------------------ tokens
    async def token(self, session: Session) -> str | None:
        """A current ARM access token for this person, refreshed silently if needed.

        Only meaningful in delegated mode; without it there is no user token to hold, and calls
        run as the app's own identity restricted to the subscriptions they are entitled to.
        """
        if not self.delegated:
            return None

        def _silent() -> dict | None:
            return session.app.acquire_token_silent_with_error(
                self.scopes, account=session.account or None)

        result = await asyncio.to_thread(_silent)
        if result and "access_token" in result:
            return result["access_token"]
        if result and result.get("error") in {"temporarily_unavailable", "server_error"}:
            raise RuntimeError("Azure sign-in is temporarily unavailable. Try again.")
        # The refresh token is gone or revoked; the next request will bounce them to sign in.
        log.info("no ARM token for %s; session needs re-authentication", session.email)
        return None

    async def token_for_tenant(self, session: Session, tenant: str) -> str | None:
        """An ARM token for one specific directory, or None if that tenant will not issue one.

        The refresh token in the cache is not bound to a tenant, so an app pointed at another
        directory's authority can redeem the same one there — this is what makes a single
        sign-in work across every tenant the person belongs to, without sending them back to
        Entra once per directory.

        None is an ordinary outcome, not a failure: it means this person cannot use the app in
        that tenant (no consent there, or a Conditional Access policy said no), which is Azure's
        answer and is to be reported rather than worked around.
        """
        def _acquire() -> dict | None:
            app = session.apps.get(tenant)
            if app is None:
                app = self._build(session.cache, tenant=tenant)
                session.apps[tenant] = app
            accounts = app.get_accounts()
            account = accounts[0] if accounts else (session.account or None)
            return app.acquire_token_silent([ARM_SCOPE], account=account)

        try:
            result = await asyncio.to_thread(_acquire)
        except Exception as exc:  # noqa: BLE001 - one bad tenant must not end the sign-in
            log.info("no token for %s in tenant %s: %s", session.email, tenant, str(exc)[:160])
            return None
        if result and "access_token" in result:
            return result["access_token"]
        if result:
            log.info("tenant %s refused a token for %s: %s",
                     tenant, session.email, str(result.get("error_description"))[:160])
        return None

    # -------------------------------------------------------------------- RBAC
    async def caller(self, session: Session, force: bool = False) -> Any:
        """This person's Azure access, as something the cost layer can call Azure with.

        None in non-delegated mode: there is no user token to act with, and entitlement is
        enforced by scoping to the subscriptions their role assignments name.
        """
        await self._refresh(session, force=force)
        return session.caller

    async def subscriptions(self, session: Session, force: bool = False) -> list[dict]:
        """The subscriptions *this person* can see, decided by Azure either way.

        Delegated: ask ARM with their own token — in every tenant they belong to — so the list
        is literally what Azure shows them. Otherwise: ask ARM, as this app, which subscriptions
        carry a role assignment for their object id, which is transitive so group-granted access
        counts. Different mechanism, same authority: neither is a list of names in a config file.
        """
        await self._refresh(session, force=force)
        return session.subscriptions or []

    async def _refresh(self, session: Session, force: bool = False) -> None:
        if not force and self._serve_stale(session):
            return
        async with session.refresh_lock:
            if session.reauth_required:
                return
            await self._refresh_locked(session, force=force)

    def _serve_stale(self, session: Session) -> bool:
        """Keep answering from the due snapshot while one revalidation runs behind it.

        Returns True when the caller may use `session.subscriptions` as it stands. Nothing is
        awaited on this path, so a request that arrives during a revalidation is not queued
        behind the ARM call the way `refresh_lock` would queue it.
        """
        if SUBSCRIPTION_GRACE <= 0 or session.subscriptions is None or session.reauth_required:
            return False
        age = time.time() - session.checked
        if age < SUBSCRIPTION_TTL:
            return True                       # simply fresh; the locked path agrees
        if age >= SUBSCRIPTION_TTL + SUBSCRIPTION_GRACE:
            return False                      # past the grace window: revalidate before answering

        task = session.revalidating
        if task is None or task.done():
            session.revalidating = asyncio.create_task(self._revalidate(session))
        return True

    async def _revalidate(self, session: Session) -> None:
        """The same refresh, off the request path. A failure only shortens the grace window."""
        try:
            async with session.refresh_lock:
                if session.reauth_required:
                    return
                await self._refresh_locked(session, background=True)
        except Exception as exc:  # noqa: BLE001 - a background refresh must never raise into the loop
            log.warning("background RBAC revalidation failed for %s: %s",
                        session.email, str(exc)[:200])
        finally:
            session.revalidating = None

    async def _refresh_locked(self, session: Session, force: bool = False,
                              background: bool = False) -> None:
        fresh = (session.subscriptions is not None
                 and time.time() - session.checked < SUBSCRIPTION_TTL)
        if fresh and not force:
            return

        from . import cost

        # An expired authorization snapshot must never survive a failed refresh, including
        # transport failures. Keep the login on transient failures, but grant no stale access.
        #
        # A background revalidation is the one case where the snapshot is not expired: it is
        # inside its grace window and is still the authoritative answer until a newer one
        # arrives. Clearing it here would open a hole that every concurrent request falls
        # through into the blocking path -- exactly the stall this runs in the background to
        # avoid. The failure branch below still drops it, so a refresh that fails never leaves
        # entitlements standing.
        if not background:
            session.subscriptions = None
            session.caller = None
            session.checked = 0
            session.tenants = []
            session.skipped = []
            session.app_visible = 0
        try:
            if not self.delegated:
                found = await cost.subscriptions_for_principal(session.oid)
                session.subscriptions = [{"id": s["id"], "name": s.get("name")} for s in found]
                # The ceiling this answer was drawn from. Cheap -- the same call was just made
                # inside the resolver and is served from the response cache.
                visible = (await cost.list_subscriptions())["subscriptions"]
                session.app_visible = len(visible)
                session.caller = None
                session.problem = ""
                session.checked = time.time()
                return

            home = await self.token(session)
            if home is None:
                session.problem = "Your Azure session expired. Sign in again."
                session.reauth_required = True
                self.drop(session.sid)
                return

            if self.multi_tenant:
                await self._refresh_across_tenants(session, home)
            else:
                # One directory, so one token answers for everything in it.
                subs = await cost.subscriptions_with(home)
                session.caller = cost.Caller(fallback=home, subscriptions=subs)
                session.subscriptions = [{"id": s["id"], "name": s.get("name")} for s in subs]
            session.problem = ""
            session.checked = time.time()
        except Exception as exc:  # noqa: BLE001 - an ARM failure must not 500 the whole page
            log.warning("could not list subscriptions for %s: %s", session.email, str(exc)[:200])
            session.problem = f"Azure did not return your subscriptions: {str(exc)[:160]}"
            session.subscriptions = None
            session.caller = None

    async def _refresh_across_tenants(self, session: Session, home: str) -> None:
        """Merge successful tenant reads and report refusals separately from failed reads."""
        from . import cost

        tenants = await cost.list_tenants(home)
        if not tenants:
            tenants = [{"id": session.tenant, "name": session.tenant}]

        async def one(entry: dict) -> tuple[dict, str | None, list[dict], str | None]:
            token = (home if entry["id"].lower() == session.tenant.lower()
                     else await self.token_for_tenant(session, entry["id"]))
            if token is None:
                return entry, None, [], None
            try:
                return entry, token, await cost.subscriptions_with(token), None
            except Exception as exc:  # noqa: BLE001 - one refusal must not hide the rest
                log.info("subscription lookup failed in tenant %s for %s: %s",
                         entry["id"], session.email, str(exc)[:160])
                return entry, token, [], "Azure could not list subscriptions in this directory."

        results = await asyncio.gather(*(one(t) for t in tenants))

        by_subscription: dict[str, str] = {}
        merged: list[dict] = []
        reachable: list[dict] = []
        skipped: list[dict] = []

        for entry, token, subs, problem in results:
            if token is None or problem:
                skipped.append({"id": entry["id"], "name": entry["name"],
                                "reason": problem or "no access granted to this app in that directory",
                                "unavailable": problem is not None})
                continue
            reachable.append({"id": entry["id"], "name": entry["name"], "subscriptions": len(subs)})
            for sub in subs:
                key = sub["id"].lower()
                if key in by_subscription:
                    continue
                by_subscription[key] = token
                merged.append({"id": sub["id"], "name": sub.get("name"),
                               "tenant": entry["id"], "tenant_name": entry["name"]})

        session.tenants = reachable
        session.skipped = skipped
        if not merged and any(entry["unavailable"] for entry in skipped):
            raise cost.CostError("Azure could not list your subscriptions. Try again shortly.")
        session.caller = cost.Caller(fallback=home, by_subscription=by_subscription,
                                     subscriptions=merged)
        session.subscriptions = merged
        log.info("%s: %d subscription(s) across %d tenant(s), %d skipped",
                 session.email, len(merged), len(reachable), len(skipped))


# Entra's way of saying "an administrator has to approve this app for your directory". They
# differ in who is refusing and why, but the remedy is identical, and every one of them is a
# dead end for the person signing in unless they are handed the admin-consent link.
CONSENT_CODES = (
    "AADSTS90094",   # tenant policy: users may not consent to apps
    "AADSTS65001",   # nobody has consented for this user or tenant yet
    "AADSTS900941",  # admin consent required for the requested permission
    "AADSTS900971",  # no reply address / consent flow interrupted for a first-party admin path
)


def needs_admin_consent(text: str) -> bool:
    lowered = (text or "").lower()
    return (any(code in text for code in CONSENT_CODES)
            or "admin approval" in lowered
            or ("administrator" in lowered and "consent" in lowered))


def _friendly(result: dict) -> str:
    """Turn the failures people actually hit into something they can act on."""
    error = (result.get("error") or "") + " " + (result.get("error_description") or "")
    if "AADSTS65004" in error or "consent" in error.lower():
        return ("You declined the permission request, or an administrator must grant it. The app "
                "needs to read your Azure subscriptions on your behalf.")
    if "AADSTS50020" in error or "AADSTS50034" in error:
        return ("That account has no access to this Azure tenant. Ask for guest access to the "
                "tenant the subscriptions live in, then try again.")
    if "AADSTS700016" in error or "AADSTS700009" in error:
        return "The app registration is misconfigured. Check AUTH_CLIENT_ID and AUTH_TENANT_ID."
    # A "Web" redirect URI is a confidential-client registration, and Entra will not redeem a
    # code for one without a secret however correct everything else is. Naming the setting is
    # the difference between a five-minute fix and an afternoon spent re-checking ids.
    if "AADSTS7000218" in error or "client_assertion" in error or "client_secret" in error:
        return ("This app is registered with a Web redirect URI, which Entra will only redeem "
                "with a client secret. Set AUTH_CLIENT_SECRET, or register the redirect URI as "
                "a public client instead.")
    # Multi-tenant only: the registration is single-tenant, so Entra will not admit anyone from
    # another directory. The fix is one setting on the app registration, and without naming it
    # this reads as the user's account being at fault when it is not.
    if "AADSTS50194" in error or "AADSTS700051" in error or "not configured as a multi-tenant" in error:
        return ("This app is registered for a single directory, so accounts from other "
                "organisations cannot sign in. Set the registration's supported account types to "
                "'Accounts in any organizational directory'.")
    if "AADSTS90072" in error:
        return ("Your account is from another directory and has not been added to this one. "
                "Ask for guest access to the directory that owns the subscriptions.")
    return (f"Sign-in failed: {(result.get('error_description') or 'unknown error')[:200]}")


# ------------------------------------------------------------------- cookie id
def sign(value: str, secret: bytes) -> str:
    mac = hmac.new(secret, value.encode(), hashlib.sha256).hexdigest()[:32]
    return f"{value}.{mac}"


def unsign(token: str | None, secret: bytes) -> str | None:
    if not token or "." not in token:
        return None
    value, _, mac = token.rpartition(".")
    return value if hmac.compare_digest(sign(value, secret), f"{value}.{mac}") else None
