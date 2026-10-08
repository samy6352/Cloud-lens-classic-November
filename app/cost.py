"""Azure Cost Management queries, scoped to whatever the caller can see.

Deliberately narrow: this module answers money questions and nothing else. It runs as the
**signed-in person** when one is present — their delegated ARM token is carried on a ContextVar
and used in preference to the server's own credential — and as the server (az login / managed
identity) for background work. Either way "subscriptions I have access to" is simply whatever
Azure returns, so there is no separate permission model here to get wrong.

Everything here is a read. Nothing in this file can change an Azure resource.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import math
import os
import re
import time
from contextvars import ContextVar
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from typing import Any, Literal

import httpx

log = logging.getLogger("cloudlens.cost")

ARM = "https://management.azure.com"
COST_API = "2024-08-01"
BUDGET_API = "2024-08-01"
SUBS_API = "2022-12-01"
ROLE_API = "2022-04-01"

# Built-in roles Azure accepts for Cost Management at subscription scope. Listed by GUID rather
# than resolved through the role definition below because these five answer almost every real
# assignment, and skipping the lookup keeps the common path to one call per subscription.
COST_CAPABLE_ROLES = frozenset({
    "8e3af657-a8ff-443c-a75c-2fe8c4bcb635",  # Owner
    "b24988ac-6180-42a0-ab88-20f7382dd24c",  # Contributor
    "acdd72a7-3385-48ef-bd42-f606fba81ae7",  # Reader
    "72fafb9e-0641-4937-9268-a91bfd8191a3",  # Cost Management Reader
    "434105ed-43f6-45c7-a02f-909b2ba83430",  # Cost Management Contributor
    "fa23ad8b-c56e-40d8-ac0c-ce449e1d2c64",  # Billing Reader
})

# Action patterns that imply cost access in a custom role.
_COST_ACTION_PREFIXES = ("microsoft.costmanagement/", "microsoft.consumption/", "microsoft.billing/")

# Roles that let their holder create or run a Cost Management export. Deliberately narrower than
# COST_CAPABLE_ROLES: reading cost and scheduling a job that writes into a storage account are
# different asks, and a Reader must not be able to cause the second.
WRITE_CAPABLE_ROLES = frozenset({
    "8e3af657-a8ff-443c-a75c-2fe8c4bcb635",  # Owner
    "b24988ac-6180-42a0-ab88-20f7382dd24c",  # Contributor
    "434105ed-43f6-45c7-a02f-909b2ba83430",  # Cost Management Contributor
})

_WRITE_ACTION_PREFIXES = ("microsoft.costmanagement/exports/",)

# Cost Management throttles hard — a handful of queries a minute. Keep concurrency modest and
# always honour Retry-After, or multi-subscription questions become flaky for no good reason.
_CONCURRENCY = asyncio.Semaphore(4)

# What we call ourselves to Cost Management. A caller that does not send this is filed under a
# shared "DefaultQuota" client-type bucket along with every other anonymous client touching the
# tenant — the portal, the CLI, and anything else running — and that bucket is routinely empty.
# warehouse.py carries the full measurements behind this; it is imported from here because this
# is the module that builds the request headers.
CLIENT_TYPE = os.getenv("COST_CLIENT_TYPE", "CloudLens")

# Billed cost is recomputed a few times a day at most, so a short cache costs nothing in accuracy
# and removes whole seconds from follow-up questions ("...and by resource group?"), which
# otherwise re-run the identical query per subscription.
_CACHE_TTL = float(os.getenv("CACHE_TTL_SECONDS", "300"))

# How long past that a cached answer may still be *served* while a fresh one is fetched behind it.
#
# Without this, the expensive tabs are fast only inside the five-minute window and slow again the
# moment it lapses. Measured against the live deployment, cold: Waste 6.8s, Governance 6.6s,
# Cost exports 8.0s -- and 0.11s once warm. Somebody opening Waste six minutes after their
# colleague did paid the full 6.8 seconds again, for data that had not changed.
#
# So an expired entry is stale, not gone: it is returned immediately and refreshed in the
# background, and only the very first caller of all ever waits. These are inventory and
# recommendation views that Azure itself recomputes hourly at best, so a reply that is up to a
# few minutes old is the same answer -- and the cost figures on the dashboard do not come from
# here at all, they come from the warehouse.
#
# A write still purges through `forget()`, so reading back something you just changed is never
# served from this. Set CACHE_STALE_SECONDS=0 to turn the behaviour off entirely.
_CACHE_STALE = float(os.getenv("CACHE_STALE_SECONDS", "900"))

# (expiry, payload, url). The URL is carried because the key is a hash over the caller and the
# whole request, which cannot be searched: without it there is no way to drop the entries for
# one resource after writing to it, and the only alternatives are keeping a stale answer or
# throwing away everyone's cache.
_cache: dict[str, tuple[float, dict, str]] = {}
_cache_tokens: dict[str, tuple[str, object]] = {}

# Keys with a background refresh already running, so ten concurrent readers of a stale entry
# cause one refresh rather than ten. The task set holds a reference to each task: the event loop
# keeps only a weak one, and a refresh that is garbage-collected half way through leaves the
# entry stale forever.
_refreshing: set[str] = set()
_refresh_tasks: set[asyncio.Task] = set()
_flight_tasks: set[asyncio.Task] = set()

# The same idea for the *first* fetch. `_refreshing` above only covers refreshing an entry that
# already exists, so a key nobody has fetched yet had no protection at all: every concurrent
# reader missed, and every one of them called Azure. Three identical `GET /subscriptions` go out
# on startup for exactly this reason, and `list_subscriptions()` alone has some two dozen callers
# that each ask independently.
#
# Wasting the call is the smaller half. `_CONCURRENCY` below is four slots for the whole process,
# so duplicates crowd out work that is not duplicated, and Cost Management throttles by client
# type -- the measurements in `_fetch` are what that costs when it goes wrong.
#
# Keyed by the same cache key, which already contains the caller id. Two identities therefore
# never share a task, and this inherits the cache's RBAC property rather than adding a new one.
# The URL rides along so `forget()` can find these the same way it finds cache entries.
_inflight: dict[str, tuple[str, asyncio.Task]] = {}


def _drop_inflight(key: str, done: asyncio.Task) -> None:
    entry = _inflight.get(key)
    if entry is not None and entry[1] is done:
        del _inflight[key]

# Nothing evicts this cache on a timer: an entry is dropped when it is next read and found to be
# past use, so a query that is never repeated is never dropped. Keys include the caller, the
# period and the scope, so a busy estate generates a great many one-off keys, and the app runs as
# a single long-lived process. Serving stale answers extends each entry's life from five minutes
# to twenty, which makes an unbounded dict a worse idea than it already was.
#
# So: an occasional sweep, triggered by writes rather than by a timer, costing one pass over the
# keys and only when the cache has grown past a size no honest working set reaches.
_CACHE_MAX_ENTRIES = int(os.getenv("CACHE_MAX_ENTRIES", "2000"))


def _prune() -> None:
    """Drop entries no reader could still be served, oldest first if that is not enough."""
    now = time.time()
    for key in [k for k, (exp, _, _) in _cache.items() if now >= exp + _CACHE_STALE]:
        _cache.pop(key, None)
    # Still too many: everything left is live, so give up the least recently refreshed.
    if len(_cache) > _CACHE_MAX_ENTRIES:
        by_age = sorted(_cache.items(), key=lambda kv: kv[1][0])
        for key, _ in by_age[:len(_cache) - _CACHE_MAX_ENTRIES]:
            _cache.pop(key, None)
    for key in list(_cache_tokens):
        if key not in _cache and key not in _inflight and key not in _refreshing:
            _cache_tokens.pop(key, None)


def cache_stats() -> dict[str, int]:
    now = time.time()
    return {
        "entries": sum(1 for exp, _, _ in _cache.values() if exp > now),
        "stale": sum(1 for exp, _, _ in _cache.values()
                     if exp <= now < exp + _CACHE_STALE),
        "refreshing": len(_refreshing),
    }


def clear_cache() -> None:
    _cache.clear()
    _cache_tokens.clear()
    _inflight.clear()


def forget(url_fragment: str) -> int:
    """Drop cached reads whose URL contains `url_fragment`, across every caller.

    Called after a write. A cached list from before the write is not merely stale, it actively
    contradicts what the caller just did — reading back a budget they have just changed and
    being shown the old amount reads as the write having silently failed. Dropped for everyone
    because the resource changed for everyone, not just for whoever wrote it.
    """
    gone = [k for k, (_, _, url) in _cache.items() if url_fragment in url]
    invalidated = {k for k, (url, _) in _cache_tokens.items() if url_fragment in url}
    for k in gone:
        del _cache[k]

    # Stop anyone new joining a fetch that was already running when the write landed: it will
    # answer with what Azure held before the change, which is the exact confusion above. Readers
    # already waiting on it keep their result -- they asked before the write too -- and the task
    # is left to finish rather than cancelled, since something is awaiting it.
    for k in [k for k, (url, _) in _inflight.items() if url_fragment in url]:
        invalidated.add(k)
        del _inflight[k]
    for k in invalidated:
        _cache_tokens.pop(k, None)

    return len(gone)


class CostError(RuntimeError):
    """Something the caller can act on; surfaced to the model as text."""

    def __init__(self, message: str, *, status_code: int | None = None,
                 retry_after: str | None = None):
        super().__init__(message)
        self.status_code = status_code
        self.retry_after = retry_after


class SubscriptionsNotReadable(CostError):
    """None of the selected subscriptions is one this identity can read live.

    The message used to embed the selection as a Python list of GUIDs, which the Budgets tab
    and every other live read showed verbatim (live Scale, 8 Oct). It now counts the selection
    and names what can be read, so the sentence says what to change.
    """

    def __init__(self, requested: int, readable: list[str]):
        noun = "subscription" if requested == 1 else "subscriptions"
        shown = ", ".join(readable[:10])
        more = f" and {len(readable) - 10} more" if len(readable) > 10 else ""
        tail = (f" It can read: {shown}{more}." if readable
                else " It cannot read any subscription live.")
        super().__init__(
            f"None of the {requested} selected {noun} can be read live by this identity."
            + tail)


# Whose Azure access a call runs under. Set per request when someone is signed in with Entra,
# unset for the server's own background work. A ContextVar rather than a parameter because it
# has to reach every live Azure call in this module and in waste.py without threading an
# argument through two dozen signatures — and because asyncio copies the context into tasks,
# so the fan-out inside cost_summary() inherits it automatically.
_caller: ContextVar[Caller | None] = ContextVar("caller", default=None)
_caller_id: ContextVar[str] = ContextVar("caller_id", default="server")

# A subscription id in an ARM URL, which is how a call is matched to the tenant that can
# authorise it. Every ARM path that is scoped to a subscription starts this way.
_SUB_IN_URL = re.compile(r"/subscriptions/([0-9a-fA-F-]{36})\b")


@dataclass
class Caller:
    """One signed-in person's Azure access, which may span several tenants.

    An ARM token is issued by one tenant and refused by every other, so a person with access in
    three directories genuinely holds three tokens. Keeping a single token here would work for
    whichever tenant happened to issue it and quietly 401 on the rest — indistinguishable, from
    the outside, from not having access at all.

    `by_subscription` maps each subscription to the token that can actually read it. `fallback`
    is the home-tenant token, used for tenant-wide calls like listing subscriptions.
    """

    fallback: str
    by_subscription: dict[str, str] = field(default_factory=dict)
    # The union across tenants, when it is already known. GET /subscriptions answers for the
    # tenant that issued the token and no other, so for a multi-tenant caller it is not the
    # question we mean to ask — whoever assembled the token map has the real answer.
    subscriptions: list[dict[str, Any]] | None = None

    def token_for(self, url: str) -> str:
        match = _SUB_IN_URL.search(url)
        if match:
            token = self.by_subscription.get(match.group(1).lower())
            if token:
                return token
        return self.fallback


def act_as(token: str | Caller, caller_id: str) -> tuple[Any, Any]:
    """Run subsequent Azure calls in this context as the holder of `token`.

    A bare token is still accepted: single-tenant callers, and the tests, have exactly one.
    """
    caller = token if isinstance(token, Caller) else Caller(fallback=token)
    return _caller.set(caller), _caller_id.set(caller_id)


def stop_acting(reset: tuple[Any, Any]) -> None:
    _caller.reset(reset[0])
    _caller_id.reset(reset[1])


def acting_as_caller() -> bool:
    """Whether live Azure calls here carry the signed-in person's own token.

    Without delegated ARM the call runs as the app's managed identity instead, which makes
    "you need this role" the wrong sentence: the principal Azure refused is the app, not the
    person reading the message.
    """
    return _caller.get() is not None


def acting_as() -> str:
    return _caller_id.get()


def caller_token(url: str = "") -> str | None:
    """The signed-in person's ARM token for this URL, or None if we are not acting as anyone.

    A subscription-shaped URL picks the token issued by the tenant that owns that subscription;
    anything else gets the home-tenant one. Callers that hold a single token are unaffected —
    `Caller.token_for` returns the fallback in that case.
    """
    caller = _caller.get()
    if caller is None:
        return None
    return caller.token_for(url) if url else caller.fallback


# One connection pool for every ARM call, instead of one per call.
#
# `_fetch` used to open an `httpx.AsyncClient` inside its own `async with`, so every request
# paid a fresh TCP connect and TLS handshake to management.azure.com. That is invisible on one
# call and dominant on a fan-out: Advisor asks once per subscription, and on the 71-subscription
# live estate the handshakes were almost the whole cost. Measured there, same 71 calls, same
# four concurrency slots, same outcome (14 recommendations, 68 refusals):
#
#   a client per call:   59.4s wall, 141.3s summed, mean 2.05s per refused call
#   one shared client:    4.2s wall,  13.3s summed, mean 0.18s per refused call
#
# This changes no request, no header and no concurrency limit, so it is neutral with respect to
# the throttling behaviour `_CONCURRENCY` and `_retry_after` exist to manage -- the same number
# of requests still leaves at the same rate. It only stops re-establishing the connection they
# travel over.
#
# Bound to the event loop that created it. An `AsyncClient` is not portable across loops, and
# the test suite creates a fresh loop per test, so a pool cached from a previous one would hand
# out connections belonging to a closed loop. Rebuilt whenever the running loop differs.
_pool: httpx.AsyncClient | None = None
_pool_loop: asyncio.AbstractEventLoop | None = None


def _shared_client() -> httpx.AsyncClient:
    global _pool, _pool_loop

    loop = asyncio.get_running_loop()
    factory = httpx.AsyncClient
    # Rebuilt when the pool belongs to another loop, has been closed, or is no longer an
    # instance of whatever `httpx.AsyncClient` currently names. That last case is not
    # hypothetical: several suites monkeypatch the attribute with a stub or a lambda returning a
    # MockTransport client, and a cached real pool would silently ignore the substitution and
    # send their requests to Azure. `getattr` for `is_closed` because those stubs are not
    # clients and do not have it.
    reusable = (
        _pool is not None
        and _pool_loop is loop
        and not getattr(_pool, "is_closed", False)
        and isinstance(factory, type)
        and isinstance(_pool, factory)
    )
    if not reusable:
        # Keep-alives at least as wide as `_CONCURRENCY`, so four callers in flight together
        # never queue behind one another for a connection.
        _pool = factory(
            timeout=90,
            limits=httpx.Limits(max_connections=16, max_keepalive_connections=16))
        _pool_loop = loop
    return _pool


class Azure:
    """Minimal ARM client using the caller's credential, or the server's when there isn't one."""

    def __init__(self) -> None:
        self._credential: Any | None = None
        self._token: tuple[str, float] | None = None

    async def _bearer(self, url: str = "") -> str:
        import time

        # A signed-in person's own token wins, so Azure applies their RBAC rather than ours.
        # Which token depends on the URL: the one issued by the tenant that owns the
        # subscription being read.
        caller = _caller.get()
        if caller:
            return caller.token_for(url)

        if self._token and self._token[1] > time.time() + 120:
            return self._token[0]

        if self._credential is None:
            from azure.identity.aio import DefaultAzureCredential

            from .identity import interactive_allowed

            self._credential = DefaultAzureCredential(
                exclude_interactive_browser_credential=not interactive_allowed())

        tok = await self._credential.get_token(f"{ARM}/.default")
        self._token = (tok.token, float(tok.expires_on))
        return tok.token

    async def close(self) -> None:
        global _pool, _pool_loop

        # Readers shield shared fetches, and invalidation removes their cache keys without
        # cancelling them. Keep ownership until completion, then join them before transport close.
        pending = set(_flight_tasks) | set(_refresh_tasks)
        for task in pending:
            task.cancel()
        await asyncio.gather(*pending, return_exceptions=True)
        _flight_tasks.clear()
        _refresh_tasks.clear()
        _refreshing.clear()
        clear_cache()
        self._token = None
        if self._credential is not None:
            await self._credential.close()
            self._credential = None
        # The pool outlives any one request but not the process, and closing it here keeps
        # `close()` meaning "release what this module is holding". It can only be awaited from
        # the loop that built it; called from any other, it is dropped and `_shared_client`
        # rebuilds on next use.
        pool, loop = _pool, _pool_loop
        _pool, _pool_loop = None, None
        if pool is None or pool.is_closed:
            return
        try:
            running: asyncio.AbstractEventLoop | None = asyncio.get_running_loop()
        except RuntimeError:
            running = None
        if running is loop:
            await pool.aclose()

    async def request(self, method: str, url: str, *, json_body: dict | None = None,
                      params: dict | None = None, attempts: int = 5, cache: bool = True) -> dict:
        key = ""
        if cache and _CACHE_TTL > 0:
            # The caller is part of the key. Two people can ask for the same URL and be entitled
            # to different answers, so a cache shared across identities would leak across RBAC.
            blob = json.dumps([_caller_id.get(), method, url, json_body, params],
                              sort_keys=True, default=str)
            key = hashlib.sha256(blob.encode()).hexdigest()
            hit = _cache.get(key)
            if hit:
                fresh_until, payload, _ = hit
                now = time.time()
                if fresh_until > now:
                    return payload
                if now < fresh_until + _CACHE_STALE:
                    # Answer now, refresh behind it. The task inherits this context, so the
                    # refresh runs as the same caller, with the same token and the same
                    # entitlement as the request that triggered it.
                    self._revalidate(key, method, url, json_body, params, attempts)
                    return payload
                del _cache[key]

        # Uncached calls keep going straight out: without a key there is nothing to collapse on,
        # and `cache=False` is how a caller asks for a genuinely fresh read.
        if not key:
            return await self._fetch(key, method, url, json_body, params, attempts)

        entry = _inflight.get(key)
        if entry is None:
            task = asyncio.create_task(
                self._fetch(key, method, url, json_body, params, attempts))
            _flight_tasks.add(task)
            task.add_done_callback(_flight_tasks.discard)
            _inflight[key] = (url, task)
            # Discard by identity. A replacement is only ever created once the previous task has
            # been removed, so this cannot drop a newer one -- but comparing costs nothing and
            # means the invariant does not have to hold for this line to be correct.
            task.add_done_callback(lambda done, k=key: _drop_inflight(k, done))
        else:
            task = entry[1]

        # Shielded, because awaiting a task hands *this* request's cancellation to it. A reader
        # who navigates away would otherwise abort the fetch for everyone else waiting on it.
        #
        # A failure now fails every waiter together rather than each retrying alone. That is the
        # intended trade: `_fetch` already retries internally, and the alternative is the
        # stampede this exists to prevent, arriving a moment later.
        return await asyncio.shield(task)

    def _revalidate(self, key: str, method: str, url: str, json_body: dict | None,
                    params: dict | None, attempts: int) -> None:
        """Refresh a stale entry in the background, once, without blocking the reader."""
        if key in _refreshing:
            return
        _refreshing.add(key)

        async def run() -> None:
            try:
                await self._fetch(key, method, url, json_body, params, attempts)
            except Exception:  # noqa: BLE001 - a failed refresh must not disturb the reader
                # Deliberately quiet: the reader already has an answer, and the entry stays
                # stale until it refreshes or ages out, so the next reader simply tries again.
                log.debug("background cache refresh failed for %s", url, exc_info=True)
            finally:
                _refreshing.discard(key)

        task = asyncio.create_task(run())
        _refresh_tasks.add(task)
        task.add_done_callback(_refresh_tasks.discard)

    async def _fetch(self, key: str, method: str, url: str, json_body: dict | None,
                     params: dict | None, attempts: int) -> dict:
        """The network call, and the only thing that writes the cache."""
        publication = _cache_tokens.setdefault(key, (url, object())) if key else None
        headers = {"Authorization": f"Bearer {await self._bearer(url)}",
                   "Content-Type": "application/json"}

        # Name ourselves to Cost Management, or every query from here queues in the shared
        # anonymous bucket. The ingest path in warehouse.py has done this for a while, and
        # carries the measurements that established why -- but this path never did, and this is
        # the one the dashboard falls back to whenever the warehouse is behind today's date.
        # Since the warehouse is always behind on a fresh estate, that meant 429 on every tile.
        #
        # Measured on one subscription, seconds apart:
        #   without:  ...costmanagement-clienttype-requests: DefaultQuota:0      (429)
        #   with:     ...costmanagement-clienttype-requests: DefaultQuota:1999
        if "/providers/Microsoft.CostManagement/" in url:
            headers["ClientType"] = CLIENT_TYPE

        async with _CONCURRENCY:
            client = _shared_client()
            for attempt in range(attempts):
                r = await client.request(method, url, json=json_body, params=params,
                                         headers=headers)

                if r.status_code in (429, 503):
                    if attempt == attempts - 1:
                        raise CostError(
                            "Azure is still rate-limiting cost queries after several retries. "
                            "Try a narrower question, or wait a minute.",
                            status_code=r.status_code, retry_after=r.headers.get("Retry-After"),
                        )
                    await asyncio.sleep(_retry_after(r, attempt))
                    continue

                if r.status_code in (401, 403):
                    # 401 and 403 are different problems and were reported as the same one.
                    #
                    # 403 is "this principal is not allowed to do that" — a role to grant.
                    # 401 is "Azure would not accept the token at all", which happens when
                    # the token is expired, issued for the wrong tenant, or missing a claim
                    # the operation needs. Telling someone to grant Cost Management Reader
                    # when they already hold Cost Management Contributor sends them to check
                    # a role assignment that was never the problem — which is exactly what
                    # happened with scheduled exports: every GET succeeded on the same
                    # token, the PUT came back 401, and the message blamed a missing role.
                    #
                    # Azure's own explanation is in the body and in WWW-Authenticate. It was
                    # being discarded, so the one piece of evidence that could distinguish
                    # the two cases never reached anybody.
                    detail = (r.text or "").strip()[:400]
                    hint = r.headers.get("WWW-Authenticate", "")
                    if r.status_code == 403:
                        raise CostError(
                            "Azure refused this on that scope: the identity making the call "
                            "does not have the necessary role. Cost reads need Cost "
                            "Management Reader; creating or running an export needs Cost "
                            "Management Contributor. "
                            + (f"Azure said: {detail}" if detail else ""),
                            status_code=403,
                        )
                    raise CostError(
                        f"Azure rejected the credential for {method} on this resource "
                        f"(401). This is the token being refused rather than a missing "
                        f"role, so check the tenant it was issued for and whether it has "
                        f"expired. "
                        + (f"Azure said: {detail}" if detail else "")
                        + (f" Challenge: {hint[:200]}" if hint else ""),
                        status_code=401,
                    )
                if r.status_code >= 400:
                    raise CostError(f"Azure returned {r.status_code}: {r.text[:300]}",
                                    status_code=r.status_code,
                                    retry_after=r.headers.get("Retry-After"))

                payload = r.json() if r.content else {}
                if key and _cache_tokens.get(key) is publication:
                    _cache[key] = (time.time() + _CACHE_TTL, payload, url)
                    if len(_cache) > _CACHE_MAX_ENTRIES:
                        _prune()
                return payload
        raise CostError("Azure request failed after retries.")


def _retry_after(resp: httpx.Response, attempt: int) -> float:
    """How long to wait, according to whichever Cost Management bucket is actually binding.

    Azure reports a separate retry-after per bucket -- client type, entity, tenant -- and a
    request is refused until *every* one of them has room. Taking the first header present, which
    is what this did, obeys whichever happened to be listed first: told "clienttype 14, entity
    27" it waited 14 and was refused again, then waited 14 again. Take the longest instead, since
    that is the one the request is genuinely queued behind.
    """
    waits = []
    for name, value in resp.headers.items():
        low = name.lower()
        if low == "retry-after" or (
                low.startswith("x-ms-ratelimit-") and low.endswith("-retry-after")):
            try:
                waits.append(float(value))
            except ValueError:
                pass
    if waits:
        return min(max(waits), 60.0)
    return min(2.0 * (2**attempt), 30.0)


azure = Azure()


# --------------------------------------------------------------------------- helpers
def month_window(months_back: int) -> tuple[str, str]:
    """0 = this month to date, 1 = last complete month, 2 = the month before that."""
    today = datetime.now(UTC).date()
    start = today.replace(day=1)
    if months_back <= 0:
        return start.isoformat(), today.isoformat()
    for _ in range(months_back):
        start = (start - timedelta(days=1)).replace(day=1)
    last = (start.replace(day=28) + timedelta(days=4)).replace(day=1) - timedelta(days=1)
    return start.isoformat(), last.isoformat()


def _rows(payload: dict) -> tuple[list[dict], str | None, str | None]:
    props = payload.get("properties", {})
    cols = [c.get("name") for c in props.get("columns", [])]
    rows = [dict(zip(cols, r)) for r in props.get("rows", [])]
    cost_col = next((c for c in cols if c and c.lower() in ("totalcost", "cost", "pretaxcost")), None)
    currency = next((r.get("Currency") for r in rows if r.get("Currency")), None)
    return rows, cost_col, currency


async def _query(sub: str, body: dict) -> dict:
    return await azure.request(
        "POST", f"{ARM}/subscriptions/{sub}/providers/Microsoft.CostManagement/query?api-version={COST_API}",
        json_body=body,
    )


# --------------------------------------------------------------------------- tools
async def list_subscriptions() -> dict[str, Any]:
    """Every subscription the signed-in user can see.

    For a caller whose access spans tenants this cannot be a single ARM call: /subscriptions is
    answered by the tenant that issued the token, so asking once would silently drop every
    subscription in every other directory. Where that enumeration has already been done, use it.
    """
    caller = _caller.get()
    if caller is not None and caller.subscriptions is not None:
        subs = [s for s in caller.subscriptions if s.get("state", "Enabled") == "Enabled"]
        return {"subscriptions": subs, "count": len(subs)}

    payload = await azure.request("GET", f"{ARM}/subscriptions", params={"api-version": SUBS_API})
    subs = [
        {"id": s["subscriptionId"], "name": s.get("displayName"), "state": s.get("state")}
        for s in payload.get("value", [])
        if s.get("state") == "Enabled"
    ]
    return {"subscriptions": subs, "count": len(subs)}


BILLING_API = "2024-04-01"


async def list_billing_accounts() -> list[dict[str, Any]]:
    """Billing accounts the caller can see, with the subscriptions each one covers.

    This is what makes a billing-scope export possible: a single export there replaces one
    request per subscription, and Cost Management throttles per *tenant* (600 QPU/hour, one QPU
    per month of data), so at a thousand subscriptions the per-subscription path is twenty hours
    of quota for one refresh.

    Coverage is asked for explicitly rather than inferred. A subscription no account claims is
    left for the caller to fetch on its own -- assuming coverage would drop it from every figure
    on the dashboard, which is worse than being slow.

    Never raises. Not every caller has billing-reader rights, and most do not need them: an
    empty list simply means the per-subscription path is used, exactly as before.
    """
    try:
        payload = await azure.request(
            "GET", f"{ARM}/providers/Microsoft.Billing/billingAccounts",
            params={"api-version": BILLING_API, "$expand": "soldTo,billingProfiles"},
        )
    except Exception as exc:  # noqa: BLE001 - billing access is a bonus, not a requirement
        log.info("billing accounts unavailable (%s); using per-subscription scopes", exc)
        return []

    out: list[dict[str, Any]] = []
    for account in payload.get("value", []):
        props = account.get("properties", {}) or {}
        out.append({
            "name": account.get("name"),
            "displayName": props.get("displayName"),
            "agreementType": props.get("agreementType"),
            "accountType": props.get("accountType"),
            # Filled by list_billing_subscriptions; kept separate because it is a second call
            # per account and only worth making once an account is known to be usable.
            "subscriptions": [],
        })
    return out


async def list_billing_subscriptions(account_name: str) -> list[dict[str, Any]]:
    """The subscriptions a billing account bills for.

    Used to decide which subscriptions a billing-scope export already covers, so the rest can
    still be fetched individually.
    """
    try:
        payload = await azure.request(
            "GET",
            f"{ARM}/providers/Microsoft.Billing/billingAccounts/{account_name}/billingSubscriptions",
            params={"api-version": BILLING_API},
        )
    except Exception as exc:  # noqa: BLE001
        log.info("billing subscriptions unavailable for %s (%s)", account_name, exc)
        return []

    out = []
    for s in payload.get("value", []):
        props = s.get("properties", {}) or {}
        sub_id = (props.get("subscriptionId") or s.get("name") or "").strip()
        if sub_id:
            out.append({"id": sub_id, "name": props.get("displayName")})
    return out


async def list_tenants(token: str) -> list[dict[str, Any]]:
    """Every Entra directory this token's holder can reach.

    The starting point for cross-tenant access: a home-tenant token is enough to *list* the
    other directories, though not to read anything in them — each needs its own token, which is
    what the caller does next with this list.
    """
    reset = act_as(token, acting_as())
    try:
        payload = await azure.request(
            "GET", f"{ARM}/tenants", params={"api-version": "2022-12-01"}, cache=False)
    finally:
        stop_acting(reset)
    return [
        {"id": t["tenantId"],
         "name": t.get("displayName") or t.get("defaultDomain") or t["tenantId"]}
        for t in payload.get("value", [])
        if t.get("tenantId")
    ]


async def subscriptions_with(token: str) -> list[dict[str, Any]]:
    """Subscriptions with verified subscription-wide cost access under this token."""
    reset = act_as(token, acting_as())
    try:
        payload = await azure.request(
            "GET", f"{ARM}/subscriptions", params={"api-version": SUBS_API}, cache=False)
        allowed = []
        for subscription in payload.get("value", []):
            if subscription.get("state") != "Enabled":
                continue
            try:
                await azure.request(
                    "POST", f"{ARM}/subscriptions/{subscription['subscriptionId']}"
                    "/providers/Microsoft.CostManagement/query",
                    params={"api-version": COST_API}, cache=False,
                    json_body={"type": "ActualCost", "timeframe": "MonthToDate",
                              "dataset": {"granularity": "None", "aggregation": {
                                  "totalCost": {"name": "Cost", "function": "Sum"}}}})
            except CostError as exc:
                if exc.status_code == 403:
                    continue
                # Token rejection or throttling does not revoke the user's access.
                raise
            allowed.append({"id": subscription["subscriptionId"],
                            "name": subscription.get("displayName"),
                            "state": subscription.get("state"),
                            "tenant": subscription.get("tenantId", "")})
        return allowed
    finally:
        stop_acting(reset)


async def _roles_for(object_id: str, subscription_id: str) -> set[str] | None:
    """Role definition ids assigned to this principal on this subscription.

    None means the assignments could not be read, which is not the same as holding none.
    Transitive, so access granted through a group counts exactly as Azure counts it.
    """
    url = f"{ARM}/subscriptions/{subscription_id}/providers/Microsoft.Authorization/roleAssignments"
    try:
        payload = await azure.request(
            "GET", url,
            params={"api-version": ROLE_API, "$filter": f"assignedTo('{object_id}')"},
        )
    except CostError as exc:
        # One unreadable subscription must not hide the rest, or a single missing Reader
        # role would silently empty somebody's picker.
        log.warning("could not read role assignments on %s: %s", subscription_id, str(exc)[:150])
        return None
    return {
        (assignment.get("properties", {}) or {}).get("roleDefinitionId", "")
        for assignment in payload.get("value", [])
    } - {""}


async def _role_permits(role_definition_id: str, known: frozenset[str],
                        prefixes: tuple[str, ...], *, read_only: bool) -> bool:
    """Whether one role grants the asked-for capability, resolving custom roles against Azure."""
    guid = role_definition_id.rsplit("/", 1)[-1].lower()
    if guid in known:
        return True

    # A custom role, so ask what it actually permits. Role definitions barely change, and
    # azure.request caches, so this costs one call per distinct custom role.
    try:
        payload = await azure.request(
            "GET", f"{ARM}{role_definition_id}", params={"api-version": ROLE_API})
    except CostError as exc:
        log.warning("could not read role definition %s: %s", guid, str(exc)[:150])
        return False

    for permission in payload.get("properties", {}).get("permissions", []):
        for action in permission.get("actions", []):
            action = action.strip().lower()
            if action == "*" or action.startswith(prefixes):
                return True
            # `*/read` is enough to read cost: Azure grants Cost Management reads to Reader,
            # which holds nothing more specific than that. It is nowhere near enough to write.
            if read_only and action == "*/read":
                return True
    return False


async def _any_role_permits(roles: set[str], known: frozenset[str],
                           prefixes: tuple[str, ...], *, read_only: bool) -> bool:
    """Whether any of these roles grants the capability, asking about all of them at once.

    The well-known ones answer from a frozenset without touching the network; only a custom
    role costs a call. Awaiting them in a loop meant a principal holding several custom roles
    paid for each in turn, and the answer is a plain OR -- the order was never meaningful.
    """
    if not roles:
        return False
    verdicts = await asyncio.gather(*(
        _role_permits(role, known, prefixes, read_only=read_only) for role in roles))
    return any(verdicts)


async def _grants_cost_access(role_definition_id: str) -> bool:
    """Does this role let its holder read cost, rather than merely hold some access?

    "Any role assignment" is the wrong test. A Virtual Machine Contributor or a Key Vault
    reader holds a real assignment and no cost entitlement whatsoever, and in this mode the
    cost query runs under the application's identity — so treating them as entitled would be
    the app granting access Azure never did.
    """
    return await _role_permits(role_definition_id, COST_CAPABLE_ROLES,
                               _COST_ACTION_PREFIXES, read_only=True)


async def principal_can_write_cost(object_id: str, subscription_id: str) -> bool:
    """May this person create or run a Cost Management export on this subscription?

    Worth asking because without a delegated token the write would otherwise execute as the
    application, which holds Cost Management Contributor and would carry it out just as
    happily for a Reader. Checking the person's own role keeps Azure the authority for a
    write, exactly as a delegated token would — the app simply has to ask explicitly.
    """
    if not object_id or not subscription_id:
        return False
    roles = await _roles_for(object_id, subscription_id)
    if not roles:
        return False
    return await _any_role_permits(roles, WRITE_CAPABLE_ROLES, _WRITE_ACTION_PREFIXES,
                                   read_only=False)


async def subscriptions_for_principal(object_id: str) -> list[dict[str, Any]]:
    """Which subscriptions a person may see cost for, asked of Azure itself.

    This answers "what may they see" where the app cannot hold a delegated token for them —
    a tenant that requires administrator approval for Azure Resource Manager delegation leaves
    no other honest way to find out. The `assignedTo` filter is transitive, so access granted
    through a group counts exactly as Azure counts it, and none of it is a list we maintain.

    Two bounds apply, and both are deliberate. Only cost-capable roles count, because the cost
    query afterwards runs as this application rather than as the person. And it can only see
    subscriptions this application's own identity holds Reader on, so one it cannot inspect is
    never offered to anyone.
    """
    if not object_id:
        return []

    visible = (await list_subscriptions())["subscriptions"]

    async def has_cost_access(sub: dict) -> dict | None:
        roles = await _roles_for(object_id, sub["id"])
        if not roles:
            return None
        return sub if await _any_role_permits(
            roles, COST_CAPABLE_ROLES, _COST_ACTION_PREFIXES, read_only=True) else None

    found = await asyncio.gather(*(has_cost_access(s) for s in visible))
    return [s for s in found if s]


async def _resolve(subscription_ids: list[str] | None) -> list[dict]:
    """Turn a possibly-empty selection into concrete subscriptions."""
    known = (await list_subscriptions())["subscriptions"]
    if not subscription_ids:
        return known
    wanted = {s.lower() for s in subscription_ids}
    picked = [s for s in known if s["id"].lower() in wanted or (s["name"] or "").lower() in wanted]
    if not picked:
        raise SubscriptionsNotReadable(
            len(subscription_ids), [s["name"] or s["id"] for s in known])
    return picked


async def cost_summary(
    subscription_ids: list[str] | None = None,
    months_back: int = 1,
    group_by: Literal["ServiceName", "ResourceGroupName", "ResourceId", "ResourceLocation",
                      "MeterCategory", "None"] = "ServiceName",
    metric: Literal["ActualCost", "AmortizedCost"] = "ActualCost",
    top: int = 15,
) -> dict[str, Any]:
    """Spend for a period, broken down by a dimension, aggregated across subscriptions."""
    subs = await _resolve(subscription_ids)
    start, end = month_window(months_back)

    dataset: dict[str, Any] = {
        "granularity": "None",
        "aggregation": {"totalCost": {"name": "Cost", "function": "Sum"}},
    }
    if group_by != "None":
        dataset["grouping"] = [{"type": "Dimension", "name": group_by}]

    body = {
        "type": metric,
        "timeframe": "Custom",
        "timePeriod": {"from": f"{start}T00:00:00Z", "to": f"{end}T23:59:59Z"},
        "dataset": dataset,
    }

    results = await asyncio.gather(*(_query(s["id"], body) for s in subs), return_exceptions=True)

    # Keyed by (currency, group) rather than by group alone. Azure bills each subscription in
    # its own currency, and adding them produces a number that looks authoritative and means
    # nothing -- 100 USD + 100 EUR is not 200 of anything. The warehouse already refuses this
    # (`Warehouse.summary` returns no total when the scope is mixed, and `_check_currency`
    # rejects the query outright); this is the live Cost Management path, which is what answers
    # whenever the warehouse cannot cover the month, and it used to add them anyway and label
    # the result with whichever currency the first subscription happened to use.
    combined: dict[tuple[str, str], float] = {}
    per_sub: list[dict] = []
    currencies: set[str] = set()
    errors: list[str] = []

    for sub, res in zip(subs, results):
        if isinstance(res, Exception):
            errors.append(f"{sub['name']}: {res}")
            continue
        rows, cost_col, cur = _rows(res)
        cur = (cur or "").upper() or "(unknown)"
        currencies.add(cur)
        total = 0.0
        for r in rows:
            amount = float(r.get(cost_col) or 0)
            key = str(r.get(group_by, "(ungrouped)")) if group_by != "None" else "Total"
            combined[(cur, key)] = combined.get((cur, key), 0.0) + amount
            total += amount
        per_sub.append({"subscription": sub["name"], "id": sub["id"],
                        "total": round(total, 2), "currency": cur})

    ranked = sorted(combined.items(), key=lambda kv: kv[1], reverse=True)

    # If every subscription query failed we have no data at all. Returning a total of 0 here
    # would be reported by the model as "you spent nothing", which is a confidently wrong answer
    # — the exact failure mode this app must not have. Fail loudly instead.
    if errors and not per_sub:
        raise CostError(
            "Could not retrieve cost data for any subscription, so there is no total to report. "
            + " | ".join(errors[:3])
        )

    by_currency = {cur: round(sum(v for (c, _), v in combined.items() if c == cur), 2)
                   for cur in sorted(currencies)}
    mixed = len(currencies) > 1

    return {
        "period": {"start": start, "end": end,
                   "label": "month to date" if months_back == 0 else f"{months_back} month(s) back"},
        "metric": metric,
        "currency": None if mixed else next(iter(currencies), None),
        # No single grand total when the scope spans currencies. None rather than a sum,
        # because the model will read a number as an answer whatever the caveat says.
        "grand_total": None if mixed else round(sum(combined.values()), 2),
        "totals_by_currency": by_currency,
        "mixed_currency": mixed or None,
        "mixed_currency_note": (
            "These subscriptions are billed in more than one currency, so there is no single "
            "total. Report each currency separately." if mixed else None
        ),
        "grouped_by": group_by,
        "breakdown": [{"name": k, "cost": round(v, 2), "currency": c}
                      for (c, k), v in ranked[:top]],
        "other_count": max(0, len(ranked) - top),
        "by_subscription": sorted(per_sub, key=lambda x: x["total"], reverse=True),
        "subscriptions_queried": len(subs),
        "subscriptions_returned": len(per_sub),
        # Partial data must be visible to the model so it can caveat the answer.
        "incomplete": bool(errors) or None,
        "errors": errors or None,
    }


async def cost_trend(subscription_ids: list[str] | None = None, months: int = 6) -> dict[str, Any]:
    """Monthly totals over the last N months, for spotting direction of travel."""
    from .currency import currency_code

    months = max(2, min(int(months), 12))
    subs = await _resolve(subscription_ids)

    today = datetime.now(UTC).date()
    start = today.replace(day=1)
    for _ in range(months - 1):
        start = (start - timedelta(days=1)).replace(day=1)

    body = {
        "type": "ActualCost",
        "timeframe": "Custom",
        "timePeriod": {"from": f"{start.isoformat()}T00:00:00Z", "to": f"{today.isoformat()}T23:59:59Z"},
        "dataset": {"granularity": "Monthly",
                    "aggregation": {"totalCost": {"name": "Cost", "function": "Sum"}}},
    }

    results = await asyncio.gather(*(_query(s["id"], body) for s in subs), return_exceptions=True)

    monthly: dict[tuple[str, str], float] = {}
    unclassified: list[dict[str, Any]] = []
    errors: list[str] = []
    ok = 0
    for sub, res in zip(subs, results):
        if isinstance(res, Exception):
            errors.append(f"{sub['name']}: {res}")
            continue
        ok += 1
        rows, cost_col, cur = _rows(res)
        for r in rows:
            code = currency_code(r.get("Currency", r.get("BillingCurrency", cur)))
            raw = str(r.get("BillingMonth") or r.get("UsageDate") or "")
            key = raw[:7] if "-" in raw else (raw[:6] if len(raw) >= 6 else raw)
            if len(key) == 6 and key.isdigit():
                key = f"{key[:4]}-{key[4:6]}"
            if not code:
                unclassified.append({"subscription_id": sub["id"], "month": key,
                                     "amount": r.get(cost_col), "reason": "Billing currency is unavailable."})
                continue
            monthly[(code, key)] = monthly.get((code, key), 0.0) + float(r.get(cost_col) or 0)

    if errors and not ok:
        raise CostError("Could not retrieve trend data for any subscription. " + " | ".join(errors[:3]))

    currencies = sorted({code for code, _ in monthly})
    currency = currencies[0] if len(currencies) == 1 and currencies[0] != "UNKNOWN" else None
    series = [{"month": m, "cost": round(c, 2), "currency": code}
              for (code, m), c in sorted(monthly.items())]
    change = None
    if currency and len(series) >= 2 and series[-2]["cost"]:
        change = round((series[-1]["cost"] - series[-2]["cost"]) / series[-2]["cost"] * 100, 1)

    return {
        "months": series,
        "currency": currency,
        "mixed_currency": len(currencies) > 1,
        "months_by_currency": {code: [r for r in series if r["currency"] == code]
                               for code in currencies},
        "latest_vs_previous_pct": change,
        "subscriptions_returned": ok,
        "incomplete": bool(errors or unclassified) or None,
        "unclassified": unclassified,
        "errors": errors or None,
        "note": "The final month may be partial if it is the current month.",
    }


async def cost_changes(subscription_ids: list[str] | None = None, top: int = 10) -> dict[str, Any]:
    """What moved: last complete month vs the month before, by service.

    This is usually the real question behind "why has my bill gone up".
    """
    # Two full multi-subscription passes; run them together rather than one after the other.
    this_month, prior = await asyncio.gather(
        cost_summary(subscription_ids, months_back=1, group_by="ServiceName", top=200),
        cost_summary(subscription_ids, months_back=2, group_by="ServiceName", top=200),
    )

    # A service-by-service comparison only means something inside one currency: the same
    # service in two subscriptions billed differently would collide on name here, and the
    # difference between the two months would be a subtraction across units. Say so rather
    # than producing a table of plausible nonsense.
    if this_month.get("mixed_currency") or prior.get("mixed_currency"):
        currencies = sorted(set(this_month.get("totals_by_currency", {}))
                            | set(prior.get("totals_by_currency", {})))
        return {
            "comparing": {"current": this_month["period"], "previous": prior["period"]},
            "currency": None,
            "mixed_currency": True,
            "currencies": currencies,
            "total_change": None,
            "increases": [],
            "decreases": [],
            "note": (
                "These subscriptions are billed in more than one currency "
                f"({', '.join(currencies)}), so month-on-month change cannot be totalled. "
                "Ask again for one currency's subscriptions at a time."
            ),
        }

    now = {b["name"]: b["cost"] for b in this_month["breakdown"]}
    was = {b["name"]: b["cost"] for b in prior["breakdown"]}

    deltas = []
    for name in set(now) | set(was):
        a, b = was.get(name, 0.0), now.get(name, 0.0)
        deltas.append({
            "service": name,
            "previous": round(a, 2),
            "current": round(b, 2),
            "change": round(b - a, 2),
            "change_pct": round((b - a) / a * 100, 1) if a else None,
            "status": "new" if not a else ("stopped" if not b else "changed"),
        })

    deltas.sort(key=lambda d: d["change"], reverse=True)
    return {
        "comparing": {"current": this_month["period"], "previous": prior["period"]},
        "currency": this_month["currency"],
        "total_change": round(this_month["grand_total"] - prior["grand_total"], 2),
        "increases": [d for d in deltas if d["change"] > 0][:top],
        "decreases": [d for d in deltas if d["change"] < 0][-top:][::-1],
    }


async def cost_forecast(subscription_ids: list[str] | None = None, days_ahead: int = 30) -> dict[str, Any]:
    """Projected spend over the coming days, based on recent usage."""
    subs = await _resolve(subscription_ids)
    days_ahead = max(1, min(int(days_ahead), 90))
    today = datetime.now(UTC).date()
    end = today + timedelta(days=days_ahead)

    body = {
        "type": "ActualCost",
        "timeframe": "Custom",
        "timePeriod": {"from": f"{today}T00:00:00Z", "to": f"{end}T23:59:59Z"},
        "dataset": {"granularity": "Daily",
                    "aggregation": {"totalCost": {"name": "Cost", "function": "Sum"}}},
        "includeActualCost": True,
        "includeFreshPartialCost": False,
    }

    async def one(sub: str) -> tuple[float, str]:
        payload = await azure.request(
            "POST",
            f"{ARM}/subscriptions/{sub}/providers/Microsoft.CostManagement/forecast?api-version={COST_API}",
            json_body=body,
        )
        rows, cost_col, cur = _rows(payload)
        return sum(float(r.get(cost_col) or 0) for r in rows), (cur or "").upper() or "(unknown)"

    results = await asyncio.gather(*(one(s["id"]) for s in subs), return_exceptions=True)
    failed = [s["name"] for s, r in zip(subs, results) if isinstance(r, Exception)]

    # Per currency, for the same reason `cost_summary` is: a projection that adds dollars to
    # euros is not a projection, and "you will spend 200" is the kind of answer nobody checks.
    by_currency: dict[str, float] = {}
    for r in results:
        if isinstance(r, Exception):
            continue
        amount, cur = r
        by_currency[cur] = by_currency.get(cur, 0.0) + amount
    by_currency = {k: round(v, 2) for k, v in sorted(by_currency.items())}
    mixed = len(by_currency) > 1

    return {
        "period": {"start": today.isoformat(), "end": end.isoformat(), "days": days_ahead},
        "projected_total": None if mixed else next(iter(by_currency.values()), 0.0),
        "currency": None if mixed else next(iter(by_currency), None),
        "projected_by_currency": by_currency,
        "mixed_currency": mixed or None,
        "mixed_currency_note": (
            "These subscriptions are billed in more than one currency, so there is no single "
            "projected total. Report each currency separately." if mixed else None
        ),
        "subscriptions_included": len(subs) - len(failed),
        "no_forecast_available": failed or None,
    }


async def budgets(subscription_ids: list[str] | None = None, *, refresh: bool = False) -> dict[str, Any]:
    """Budgets and how spend is tracking against them.

    Reported per budget rather than as a bare list of amounts, because the question anyone
    opens this with is "am I over?" — which needs the percentage, the headroom left, and what
    the budget actually watches, all of which the raw ARM shape leaves to the caller.
    """
    from . import demo_mode

    if demo_mode.enabled():
        return demo_mode.budgets_payload()

    subs = await _resolve(subscription_ids)

    async def one(sub: dict) -> list[dict]:
        payload = await azure.request(
            "GET", f"{ARM}/subscriptions/{sub['id']}/providers/Microsoft.Consumption/budgets",
            params={"api-version": BUDGET_API},
            cache=not refresh,
        )
        out = []
        for b in payload.get("value", []):
            p = b.get("properties", {})
            spend = p.get("currentSpend") or {}
            cur, amt = spend.get("amount"), p.get("amount")

            # Zero spend is a fact, not a missing one. Treating it as unknown — which a plain
            # truthiness check does — reported every brand-new budget as "—", the same as one
            # Azure genuinely has no figure for, so a budget that was working looked broken.
            known = cur is not None and amt
            pct = round(cur / amt * 100, 1) if known else None
            period = p.get("timePeriod") or {}
            notifications = p.get("notifications") or {}

            out.append({
                "subscription": sub["name"],
                "subscription_id": sub["id"],
                "name": b.get("name"),
                "amount": amt,
                "current_spend": round(cur, 2) if cur is not None else None,
                "remaining": round(amt - cur, 2) if known else None,
                "currency": spend.get("unit"),
                "time_grain": p.get("timeGrain"),
                "percent_used": pct,
                "status": _budget_status(pct),
                "start": (period.get("startDate") or "")[:10] or None,
                "end": (period.get("endDate") or "")[:10] or None,
                "filter": _filter_words(p.get("filter")),
                # Kept alongside the words so the trend can honour the same filter the budget
                # does. Without it a tag-scoped budget would be drawn against whole-subscription
                # spend: on this estate one such budget tracks $14 while its subscription spends
                # $466, and a chart showing the latter under a headline saying the former is the
                # kind of contradiction that makes a reader stop trusting the page.
                "filter_raw": p.get("filter") or None,
                "alerts": len(notifications),
                # De-duplicated: Azure holds separate notifications for an actual and a
                # forecast alert at the same percentage, and listing "125%, 125%" reads as a
                # mistake rather than as two rules that happen to share a threshold.
                "alerting": sorted({
                    n.get("threshold") for n in notifications.values()
                    if isinstance(n, dict) and n.get("threshold") is not None
                }),
            })
        return out

    results = await asyncio.gather(*(one(s) for s in subs), return_exceptions=True)
    found = [b for r in results if not isinstance(r, Exception) for b in r]
    unavailable = []
    for sub, result in zip(subs, results):
        if isinstance(result, Exception):
            unavailable.append({"id": sub["id"], "name": sub["name"]})
            log.warning("could not read budgets for subscription %s: %s", sub["id"], result)

    # Worst first. A list in Azure's arbitrary order buries the one budget that is blown behind
    # four that are fine, which is the opposite of what the page is for.
    found.sort(key=lambda b: (b["percent_used"] is None, -(b["percent_used"] or 0)))
    over = [b for b in found if b["status"] == "over"]
    return {
        "budgets": found,
        "count": len(found),
        "over": len(over),
        "near": len([b for b in found if b["status"] == "near"]),
        "subscriptions_requested": len(subs),
        "subscriptions_read": len(subs) - len(unavailable),
        "unavailable_subscriptions": unavailable,
        "partial": bool(unavailable),
        "error": (
            f"Could not read budgets for {len(unavailable)} of {len(subs)} selected subscriptions. "
            "Available results may be incomplete; retry or check Azure access."
            if unavailable else None
        ),
        "note": "No budgets defined on these subscriptions." if not found and not unavailable else None,
    }


def _budget_status(percent: float | None) -> str:
    """Where a budget stands, as one word.

    The thresholds are the same ones the portal alerts on by default, so the colour on screen
    agrees with the email someone will have received rather than inventing a second opinion.
    """
    if percent is None:
        return "unknown"
    if percent >= 100:
        return "over"
    if percent >= 80:
        return "near"
    return "ok"


def _filter_words(node: Any) -> str | None:
    """An ARM budget filter in words.

    Budgets made here are tag filters; budgets made in the portal are usually `dimensions`
    (resource group, service name). Both have to read the same way in the list, otherwise the
    portal's budgets show up as untitled rows that appear to track nothing.
    """
    if not isinstance(node, dict) or not node:
        return None

    if "and" in node:
        parts = [_filter_words(c) for c in node.get("and") or []]
        return " AND ".join(p for p in parts if p) or None

    for kind in ("tags", "dimensions"):
        clause = node.get(kind)
        if isinstance(clause, dict):
            name = clause.get("name") or kind
            values = [str(v) if v != "" else "(no value)" for v in clause.get("values") or []]
            shown = ", ".join(values[:4])
            if len(values) > 4:
                shown += f" and {len(values) - 4} more"
            return f"{name} = {shown}" if shown else str(name)
    return None



async def top_resources(subscription_ids: list[str] | None = None, months_back: int = 1,
                        top: int = 15) -> dict[str, Any]:
    """The individual resources costing the most."""
    result = await cost_summary(subscription_ids, months_back=months_back,
                                group_by="ResourceId", top=top)
    for row in result["breakdown"]:
        rid = row["name"]
        parts = rid.split("/")
        row["resource"] = parts[-1] if len(parts) > 1 else rid
        row["resource_group"] = parts[4] if len(parts) > 4 else None
        row["type"] = "/".join(parts[6:8]) if len(parts) > 7 else None
    return result


async def overview(scope: list[str] | None = None, *,
                   timings: dict[str, float] | None = None,
                   failed_phases: set[str] | None = None) -> dict[str, Any]:
    """A small at-a-glance summary for the header strip.

    Historical totals come from the warehouse when it has data — instant, and immune to the
    Cost Management throttling that intermittently blanked these tiles. Forecast and budgets are
    kept per-subscription so that narrowing the scope is a re-aggregation in memory rather than
    another round of slow API calls.
    """
    from .currency import currency_code
    from .warehouse import warehouse

    async def measured(name: str, operation):
        started = time.perf_counter()
        try:
            return await operation
        except Exception:
            if failed_phases is not None:
                failed_phases.add(name)
            raise
        finally:
            duration = round((time.perf_counter() - started) * 1000, 1)
            if timings is not None:
                timings[name] = duration
            if duration >= 500:
                outcome = "error" if failed_phases is not None and name in failed_phases else "ok"
                log.warning("slow overview phase=%s duration_ms=%.1f outcome=%s",
                            name, duration, outcome)

    mtd = last = None
    currency = None
    stale_note = None
    currency_note = None
    month_to_date_by_currency: list[dict[str, Any]] = []
    last_month_by_currency: list[dict[str, Any]] = []
    warehouse_mtd_answered = False
    warehouse_last_answered = False
    # Both warehouse reads on a worker thread. psycopg is synchronous and this is the path the
    # header tiles take on every dashboard load, so running them inline puts a database round
    # trip on the event loop for every visitor.
    stored = await measured("warehouse_summary", asyncio.to_thread(warehouse.summary))
    wanted = {s.lower() for s in scope} if scope else None
    if stored.get("summary_pending"):
        stale_note = "Local cost summaries are rebuilding; warehouse totals are temporarily unavailable."
        currency_note = stale_note
        warehouse_mtd_answered = True
        warehouse_last_answered = True

    today = datetime.now(UTC).date()
    this_start = today.replace(day=1)
    prev_start = (this_start - timedelta(days=1)).replace(day=1)
    prev_end = this_start - timedelta(days=1)

    # What the warehouse can honestly answer. It holds a fixed window, and asking it for a month
    # it has no rows for returns 0.0 — a real number, so every downstream check for "no answer"
    # passes and the header states $0 for a month that simply has not been loaded. Read on the
    # 2nd of September against a warehouse ending 25 August, that tile said the estate had spent
    # nothing, which is the empty-means-zero error in the most prominent number in the app.
    covered_to = None
    if stored["to"]:
        try:
            covered_to = date.fromisoformat(str(stored["to"])[:10])
        except ValueError:
            covered_to = None
    # A partial month is not the same failure. August 1–25 summed as "last month" is wrong by
    # however many days are missing, so it is named rather than silently presented as a total.
    covers_mtd = bool(covered_to and covered_to >= this_start)
    covers_last = bool(covered_to and covered_to >= prev_end)

    if stored["rows"] and covers_mtd:
        try:
            # Dates are computed here, never user input, so interpolation is safe.
            def month_totals() -> tuple[str, list[dict]]:
                with warehouse.reader(scope, budget_ms=5000) as reader:
                    # Never turn a stale/missing aggregate into a multi-million-row scan
                    # for a page-header tile. The reader can still answer from raw rows for
                    # small warehouses; large estates wait for the rollup instead.
                    if reader.agg == "costs" and stored["rows"] > 100_000:
                        return reader.agg, []
                    sql = (
                        'SELECT "BillingCurrency" AS currency, '
                        f'  sum(CASE WHEN "ChargePeriodStart" >= DATE \'{this_start}\' '
                        '       THEN "BilledCost" ELSE 0 END) AS mtd, '
                        f'  sum(CASE WHEN "ChargePeriodStart" BETWEEN DATE \'{prev_start}\' '
                        f'       AND DATE \'{prev_end}\' THEN "BilledCost" ELSE 0 END) AS prev '
                        f'FROM {reader.agg} GROUP BY 1'
                    )
                    return reader.agg, reader.rows(sql, None)

            source, rows = (await measured(
                "warehouse_month_totals",
                asyncio.to_thread(month_totals),
            ))
            warehouse_mtd_answered = True
            warehouse_last_answered = covers_last
            if source == "costs":
                currency_note = (
                    "The warehouse monthly summary is not current; totals are temporarily "
                    "unavailable rather than scanning all raw charges."
                )
            elif not rows:
                currency_note = "No complete monthly warehouse totals are available."
            if rows:
                totals: dict[str, dict[str, float]] = {}
                for row in rows:
                    code = currency_code(row.get("currency")) or "UNKNOWN"
                    totals[code] = {
                        "mtd": round(row["mtd"] or 0, 2),
                        "last": round(row["prev"] or 0, 2),
                    }
                month_to_date_by_currency = [
                    {"currency": code, "cost": values["mtd"]}
                    for code, values in sorted(totals.items())
                ]
                last_month_by_currency = [
                    {"currency": code, "cost": values["last"]}
                    for code, values in sorted(totals.items())
                ] if covers_last else []
                if len(totals) == 1:
                    code, values = next(iter(totals.items()))
                    if code != "UNKNOWN":
                        mtd = values["mtd"]
                        last = values["last"] if covers_last else None
                        currency = code
                else:
                    currency = None
                    codes = ", ".join(sorted(totals))
                    currency_note = (
                        "As billed, the selected subscriptions span multiple currencies "
                        f"({codes}); no combined total is shown."
                    )
                if "UNKNOWN" in totals:
                    currency_note = (
                        "Some warehouse charges have no billing currency; a single total "
                        "cannot be shown safely."
                    )
        except Exception as exc:  # noqa: BLE001 - fall through to the live API
            log.warning("warehouse overview failed: %s", exc)
            if stored["rows"]:
                warehouse_mtd_answered = True
                warehouse_last_answered = covers_last
                currency_note = (
                    "Local monthly totals are temporarily unavailable; no cross-currency "
                    "total is shown."
                )
    elif stored["rows"]:
        # Worth saying which, because the two have different fixes: refresh the data, or wait
        # for Azure to write the first days of the month.
        stale_note = (f"the loaded data ends {stored['to']}, so this month is not in it"
                      if covered_to else "the loaded data has no dates")
        log.info("overview: warehouse ends %s, before %s — asking Azure for the month instead",
                 stored["to"], this_start)

    tasks: dict[str, Any] = {
        "subs": measured("subscriptions", list_subscriptions()),
        "fc": measured("forecast", _forecast_by_subscription(scope)),
        "buds": measured("budgets", budgets(scope)),
    }
    if mtd is None and not warehouse_mtd_answered:
        tasks["mtd"] = measured(
            "mtd_cost_query", cost_summary(scope, months_back=0, group_by="None"))
    if last is None and not warehouse_last_answered:
        tasks["last"] = measured(
            "last_cost_query", cost_summary(scope, months_back=1, group_by="None"))

    keys = list(tasks)
    done = await asyncio.gather(*(tasks[k] for k in keys), return_exceptions=True)
    got = dict(zip(keys, done))

    def value(x: Any, *path: str, default: Any = None) -> Any:
        if isinstance(x, Exception):
            return default
        for p in path:
            x = x.get(p) if isinstance(x, dict) else None
            if x is None:
                return default
        return x

    if mtd is None:
        mtd = value(got.get("mtd"), "grand_total")
    if last is None:
        last = value(got.get("last"), "grand_total")
    if currency is None:
        currency = value(got.get("mtd"), "currency") or value(got.get("last"), "currency")

    all_subs = value(got.get("subs"), "subscriptions", default=[]) or []
    in_scope = [s for s in all_subs if not wanted
                or s["id"].lower() in wanted or (s["name"] or "").lower() in wanted]

    worst = None
    budget_status = "unavailable"
    budget_note = "Budget information is unavailable for the selected scope; retry or check Azure access."
    buds = got.get("buds")
    if isinstance(buds, dict) and buds.get("demo_mode"):
        budget_status = "demo"
        budget_note = buds.get("note") or "Budgets are disabled in demo mode."
    elif isinstance(buds, dict) and isinstance(buds.get("budgets"), list):
        rows = buds["budgets"]
        tracked = [
            b for b in rows if isinstance(b, dict) and isinstance(b.get("name"), str)
            and "subscription" in b and type(b.get("percent_used")) in (int, float)
            and math.isfinite(b["percent_used"])
        ]
        if tracked:
            worst = max(tracked, key=lambda b: b["percent_used"])
        requested, read = buds.get("subscriptions_requested"), buds.get("subscriptions_read")
        counts_known = (type(requested) is int and type(read) is int
                        and requested > 0 and 0 <= read <= requested)
        scope_names = {v.lower() for s in in_scope for v in (s["id"], s["name"]) if v}
        scope_known = bool(in_scope) and (not wanted or wanted <= scope_names)
        # An empty list only proves absence after every selected scope was actually read.
        # In particular, old/malformed payloads and a selection resolving to nothing do not.
        complete = (
            counts_known and read == requested and requested == len(in_scope) and scope_known
            and not (buds.get("partial") or buds.get("error") or buds.get("unavailable_subscriptions"))
            and type(buds.get("count")) is int and buds["count"] == len(rows)
        )
        if complete and not rows:
            budget_status = "none"
            budget_note = "No budgets defined on the selected subscriptions."
        elif complete and len(tracked) == len(rows):
            budget_status = "ok"
            budget_note = None
        elif worst or (not complete and counts_known and read > 0 and scope_known):
            budget_status = "partial"
            budget_note = (
                "Budget information is incomplete for the selected scope; "
                "some subscriptions or usage readings are unavailable."
            )
        elif complete and all(isinstance(b, dict) and "name" in b for b in rows):
            budget_status = "unknown"
            budget_note = "Budgets exist, but their usage readings are unavailable."

    fc = got.get("fc")
    forecast_note = _forecast_note(fc)

    return {
        "subscriptions": len(in_scope) or stored["subscriptions"],
        "currency": (currency or (
            "USD" if not stored.get("mixed_currency") and not currency_note else None)),
        "month_to_date": mtd,
        "last_month": last,
        "month_to_date_by_currency": month_to_date_by_currency,
        "last_month_by_currency": last_month_by_currency,
        "currency_note": currency_note,
        "forecast_30d": value(fc, "projected_total"),
        # Why the forecast tile is empty or narrower than the selection. Azure only forecasts
        # subscriptions this identity can read live, which a cost-data-only selection is not.
        "forecast_note": forecast_note,
        "budget": {"name": worst["name"], "percent_used": worst["percent_used"],
                   "status": worst.get("status"),
                   "over": value(got.get("buds"), "over", default=0),
                   "tracked": value(got.get("buds"), "count", default=0),
                   "subscription": worst["subscription"]} if worst else None,
        "budget_status": budget_status,
        "budget_note": budget_note,
        "source": "warehouse" if stored["rows"] else "live",
        # Why a tile is empty, when it is. "—" with no reason is indistinguishable from a bug.
        "stale_note": stale_note,
        "data_to": stored["to"],
        "scope": scope or None,
        "generated_at": datetime.now(UTC).isoformat(timespec="seconds"),
    }


def _forecast_note(fc: Any) -> str | None:
    """Why the header's forecast is missing or covers less than the selection, if it does."""
    if isinstance(fc, SubscriptionsNotReadable) or (
            isinstance(fc, CostError) and "match a subscription you can see" in str(fc)):
        return "no forecast: these subscriptions are not readable live"
    if isinstance(fc, Exception):
        return "Azure forecast unavailable"
    if isinstance(fc, dict):
        if fc.get("mixed_currency"):
            return "no single forecast across currencies"
        if fc.get("no_forecast_available"):
            included = fc.get("subscriptions_included") or 0
            missing = len(fc["no_forecast_available"])
            return f"from Azure, {included} of {included + missing} subscriptions"
    return None


async def _forecast_by_subscription(scope: list[str] | None) -> dict[str, Any]:
    return await cost_forecast(scope, days_ahead=30)
