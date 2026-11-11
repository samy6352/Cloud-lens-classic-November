"""Caching the export listing must never show somebody a change that did not happen.

`/api/schedules` is one ARM call per subscription plus one per export to expand `runHistory`.
Every one of them was uncached, so the Reports tab paid seventeen round trips at concurrency
four -- 16.5s measured against this tenant. Reading through the shared cache removes that, but
only if a write is guaranteed to drop what it invalidates: a schedule that has just been
created and does not appear, or one just deleted that still does, reads as the write having
silently failed. That is strictly worse than the wait.

So the cache is allowed exactly one job, and these tests fence it:
  - reads may be served from it,
  - any non-GET purges that subscription's exports, for every caller,
  - the purge is scoped, so one subscription's write does not blank another's list,
  - and `fresh=True` bypasses it for changes made outside CloudLens entirely.
"""
from __future__ import annotations

import asyncio

import pytest

from app import schedules


@pytest.fixture
def arm(monkeypatch):
    """Record what `schedules` asks of Azure, and what it purges, without leaving the process.

    Patched onto the real `app.cost` module rather than swapped into `sys.modules`: the
    `from . import cost` inside `schedules` resolves through the already-imported package
    attribute, so replacing the module entry alone is invisible to it.
    """
    from app import cost as real_cost

    state = {"calls": [], "forgotten": []}

    async def fake_request(method, url, *, json_body=None, params=None, cache=True):
        state["calls"].append({"method": method, "url": url, "cache": cache})
        return {"value": []}

    monkeypatch.setattr(real_cost.azure, "request", fake_request)
    monkeypatch.setattr(real_cost, "forget", lambda fragment: state["forgotten"].append(fragment))
    return state


EXPORTS = ("https://management.azure.com/subscriptions/sub-a"
           "/providers/Microsoft.CostManagement/exports")


def test_a_read_is_only_cached_when_the_caller_asks(arm):
    asyncio.run(schedules._request("GET", EXPORTS))
    assert arm["calls"][-1]["cache"] is False, "the default must stay uncached"

    asyncio.run(schedules._request("GET", EXPORTS, cache=True))
    assert arm["calls"][-1]["cache"] is True


def test_a_read_never_purges_anything(arm):
    asyncio.run(schedules._request("GET", EXPORTS, cache=True))
    assert arm["forgotten"] == [], "a read that invalidates would defeat the cache entirely"


@pytest.mark.parametrize("method", ["PUT", "POST", "DELETE", "PATCH"])
def test_every_write_purges_that_subscription_s_exports(arm, method):
    asyncio.run(schedules._request(method, f"{EXPORTS}/cloudlens-focus-abc"))
    assert arm["forgotten"] == [EXPORTS], (
        f"a {method} left a stale list behind, which reads as the write having failed")


def test_running_an_export_purges_it_too(arm):
    """`run_now` POSTs to `.../exports/{name}/run`, and it changes the run history shown."""
    asyncio.run(schedules._request("POST", f"{EXPORTS}/cloudlens-focus-abc/run"))
    assert arm["forgotten"] == [EXPORTS]


def test_the_purge_does_not_reach_another_subscription(arm):
    other = EXPORTS.replace("sub-a", "sub-b")
    asyncio.run(schedules._request("DELETE", f"{other}/some-export"))
    assert arm["forgotten"] == [other]
    assert EXPORTS not in arm["forgotten"], "one write must not blank every estate's list"


def test_a_url_outside_the_exports_collection_is_left_alone(arm):
    """`_grant_storage` PUTs a role assignment. It must not be mistaken for an export write."""
    role = ("https://management.azure.com/subscriptions/sub-a/providers/"
            "Microsoft.Authorization/roleAssignments/guid")
    asyncio.run(schedules._request("PUT", role))
    assert arm["forgotten"] == [role], "an unrelated URL should purge only itself"


def test_the_scope_helper_covers_the_collection_and_its_children():
    for suffix in ("", "/an-export", "/an-export/run"):
        assert schedules._exports_scope(f"{EXPORTS}{suffix}") == EXPORTS


# --------------------------------------------------------------- the listing itself

@pytest.fixture
def listing_probe(monkeypatch, arm):
    """Enough of `cost` for `listing()` to run without touching Azure."""
    from app import cost as real_cost

    async def resolved(ids):
        return [{"id": i, "name": i} for i in (ids or ["sub-a"])]

    monkeypatch.setattr(real_cost, "_resolve", resolved)
    monkeypatch.setattr(real_cost, "ARM", "https://management.azure.com")
    return arm


def test_listing_reads_through_the_cache(listing_probe):
    asyncio.run(schedules.listing(["sub-a"]))
    reads = [c for c in listing_probe["calls"] if c["method"] == "GET"]
    assert reads, "the listing made no read at all"
    assert all(c["cache"] for c in reads), (
        "an uncached listing is the 16.5s Reports tab this change exists to fix")


def test_an_explicit_reload_bypasses_the_cache(listing_probe):
    asyncio.run(schedules.listing(["sub-a"], fresh=True))
    assert listing_probe["forgotten"] == [EXPORTS], (
        "Reload must drop the cached list first, or it returns the same answer")


def test_an_ordinary_load_does_not_bypass_the_cache(listing_probe):
    asyncio.run(schedules.listing(["sub-a"]))
    assert listing_probe["forgotten"] == [], "only the Reload button may force a fresh read"


def test_the_endpoint_and_the_button_agree_on_the_parameter():
    """Structural: a `fresh` the browser sends and the server ignores is worse than neither."""
    from pathlib import Path

    root = Path(__file__).resolve().parents[1]
    server = (root / "app" / "main.py").read_text(encoding="utf-8")
    client = (root / "web" / "assets" / "dashboard-reports.js").read_text(encoding="utf-8")

    assert "fresh: bool = False" in server
    assert "schedules.listing(picked or allowed, fresh=fresh)" in server
    inventory = client.split('$("cdLoadExports").onclick = async () => {', 1)[1].split(
        "const visibility =", 1)[0]
    assert '/api/schedules?scope=' in inventory and '&fresh=true' in inventory, (
        "explicit export inventory reads must bypass the server cache")
