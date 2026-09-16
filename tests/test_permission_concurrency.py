"""Parallelised permission checks must stay a correct OR, not merely a fast one.

Three loops that awaited inside themselves were turned into `asyncio.gather`. Each was an
independent RBAC question whose answers are combined with OR, so the ordering was never
meaningful -- but a gather that silently lost a verdict, or that stopped short-circuiting a
refusal, would widen or narrow somebody's access. These tests hold the semantics still while
the concurrency changes underneath them.

The serial versions cost one Azure round trip each, in turn: `/api/schedules` measured 16.5s
against a three-subscription estate before this, and that was the Reports tab's whole wait.
"""
from __future__ import annotations

import asyncio

import pytest

from app import cost


@pytest.fixture
def role_probe(monkeypatch):
    """Record which roles were asked about, and how much of it overlapped."""
    state = {"asked": [], "peak": 0, "live": 0}

    async def fake_permits(role, known, prefixes, *, read_only):
        state["asked"].append(role)
        state["live"] += 1
        state["peak"] = max(state["peak"], state["live"])
        try:
            await asyncio.sleep(0.02)          # stand in for the ARM round trip
            return role.endswith("yes")
        finally:
            state["live"] -= 1

    monkeypatch.setattr(cost, "_role_permits", fake_permits)
    return state


def test_any_role_permits_is_an_or_over_every_role(role_probe):
    async def go():
        return await cost._any_role_permits(
            {"a/no", "b/yes", "c/no"}, frozenset(), (), read_only=True)

    assert asyncio.run(go()) is True
    assert sorted(role_probe["asked"]) == ["a/no", "b/yes", "c/no"]


def test_a_principal_with_no_qualifying_role_is_refused(role_probe):
    async def go():
        return await cost._any_role_permits(
            {"a/no", "b/no"}, frozenset(), (), read_only=True)

    assert asyncio.run(go()) is False


def test_no_roles_is_a_refusal_and_costs_nothing(role_probe):
    async def go():
        return await cost._any_role_permits(set(), frozenset(), (), read_only=False)

    assert asyncio.run(go()) is False
    assert role_probe["asked"] == [], "an empty set must not reach Azure at all"


def test_the_roles_are_actually_asked_about_concurrently(role_probe):
    """The point of the change. Serially this is 4 round trips end to end."""
    async def go():
        return await cost._any_role_permits(
            {f"r{i}/no" for i in range(4)}, frozenset(), (), read_only=True)

    assert asyncio.run(go()) is False
    assert role_probe["peak"] == 4, f"peak in flight was {role_probe['peak']}, expected 4"


def test_write_capability_still_uses_the_narrower_role_set(monkeypatch):
    """Cost Management Reader reads cost and must never be allowed to create an export."""
    seen = {}

    async def fake_permits(role, known, prefixes, *, read_only):
        seen["known"], seen["prefixes"], seen["read_only"] = known, prefixes, read_only
        return False

    async def fake_roles(oid, sub):
        return {"/x/acdd72a7-3385-48ef-bd42-f606fba81ae7"}  # Reader

    monkeypatch.setattr(cost, "_role_permits", fake_permits)
    monkeypatch.setattr(cost, "_roles_for", fake_roles)

    assert asyncio.run(cost.principal_can_write_cost("oid", "sub")) is False
    assert seen["known"] is cost.WRITE_CAPABLE_ROLES
    assert seen["prefixes"] == cost._WRITE_ACTION_PREFIXES
    assert seen["read_only"] is False, "a write check must not accept */read"


def test_an_owner_may_write(monkeypatch):
    async def fake_roles(oid, sub):
        return {"/x/8e3af657-a8ff-443c-a75c-2fe8c4bcb635"}  # Owner

    monkeypatch.setattr(cost, "_roles_for", fake_roles)
    assert asyncio.run(cost.principal_can_write_cost("oid", "sub")) is True


def test_unreadable_assignments_are_not_treated_as_permission(monkeypatch):
    """`_roles_for` returns None when it could not read, which is not "holds none"."""
    async def unreadable(oid, sub):
        return None

    monkeypatch.setattr(cost, "_roles_for", unreadable)
    assert asyncio.run(cost.principal_can_write_cost("oid", "sub")) is False


def test_a_missing_identity_or_subscription_short_circuits(monkeypatch):
    async def explode(oid, sub):
        raise AssertionError("must not reach Azure without both identifiers")

    monkeypatch.setattr(cost, "_roles_for", explode)
    assert asyncio.run(cost.principal_can_write_cost("", "sub")) is False
    assert asyncio.run(cost.principal_can_write_cost("oid", "")) is False


def test_cost_visibility_keeps_the_reader_friendly_semantics(monkeypatch):
    """Reader holds nothing more specific than `*/read`, and that does read cost."""
    seen = {}

    async def fake_permits(role, known, prefixes, *, read_only):
        seen["known"], seen["read_only"] = known, read_only
        return True

    async def fake_roles(oid, sub):
        return {"/x/whatever"}

    monkeypatch.setattr(cost, "_role_permits", fake_permits)
    monkeypatch.setattr(cost, "_roles_for", fake_roles)
    monkeypatch.setattr(cost, "list_subscriptions", lambda: _subs())

    async def _subs():
        return {"subscriptions": [{"id": "s1"}, {"id": "s2"}]}

    found = asyncio.run(cost.subscriptions_for_principal("oid"))
    assert [s["id"] for s in found] == ["s1", "s2"]
    assert seen["known"] is cost.COST_CAPABLE_ROLES
    assert seen["read_only"] is True


def test_schedules_checks_every_subscription_at_once(monkeypatch):
    """The Reports tab's wait. Each check is one ARM call; they were awaited in a loop."""
    import app.main as main

    state = {"live": 0, "peak": 0, "checked": []}

    async def fake_assert(user, subscription):
        state["checked"].append(subscription)
        state["live"] += 1
        state["peak"] = max(state["peak"], state["live"])
        try:
            await asyncio.sleep(0.02)
        finally:
            state["live"] -= 1

    monkeypatch.setattr(main, "assert_may_write_schedule", fake_assert)

    # The same gather the endpoint builds, with the endpoint's own body shape.
    subs = [{"id": f"sub-{i}"} for i in range(5)]

    async def writable_state(sub):
        sub["can_write"] = False
        await main.assert_may_write_schedule(object(), sub["id"])
        sub["can_write"] = True

    async def go():
        await asyncio.gather(*(writable_state(s) for s in subs))

    asyncio.run(go())

    assert state["peak"] == 5, f"peak in flight was {state['peak']}, expected 5"
    assert all(s["can_write"] for s in subs)


def test_the_endpoint_gathers_rather_than_looping():
    """Structural: the loop must not come back, because nothing else would notice."""
    from pathlib import Path

    source = Path(__file__).resolve().parents[1] / "app" / "main.py"
    body = source.read_text(encoding="utf-8")
    start = body.index("async def list_schedules")
    section = body[start:body.index("class ScheduleRequest")]
    assert "asyncio.gather(*(writable_state(s)" in section, (
        "the per-subscription permission checks must be issued together")
    assert "for sub in result[\"subscriptions\"]:\n                sub[\"can_write\"]" not in section
