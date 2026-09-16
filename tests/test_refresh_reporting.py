"""The refresh has to *report*, and the RBAC snapshot must not stall the page.

Two faults found on the live deployment sat behind these three seams.

`/api/warehouse` was the whole of "Quick refresh doesn't work". The refresh ran and the data
landed; the endpoint simply did not return any of the fields the header reports with, so the
progress strip said "Starting..." for the entire run and the outcome -- rows loaded, reason
for a failure, whether the archive was written -- was never shown at all.

The durable job identity closes the window between accepting a job and its first progress
update. The browser must not accept a previous run's finished state or lose a failed run.

`EntraSSO._serve_stale` is why tabs went slow on a five-minute rhythm: revalidating the RBAC
snapshot discarded it first, under a per-session lock, and `permitted()` is the first line of
nearly every endpoint.
"""
from __future__ import annotations

import ast
import asyncio
import json
import re
import sys
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock
from uuid import uuid4

import pytest
from fastapi.responses import JSONResponse

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tests"))


def load_function(path, name, namespace):
    """Exec one function out of a module that cannot be imported without a database."""
    tree = ast.parse((ROOT / path).read_text(encoding="utf-8"))
    node = next(n for n in tree.body
                if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == name)
    node.decorator_list = []
    module = ast.Module(body=[ast.ImportFrom(module="__future__", names=[
        ast.alias(name="annotations")], level=0), node], type_ignores=[])
    exec(compile(ast.fix_missing_locations(module), str(ROOT / path), "exec"), namespace)
    return namespace[name]


# A finished quick refresh, exactly as `Warehouse.quick_ingest` leaves it: its own progress
# fields, plus `summary()` spread over the top -- which is where the estate-wide financials
# come from and why this endpoint filters at all.
FINISHED_QUICK = {
    "status": "ready", "started": "2026-09-14T09:49:20+00:00",
    "finished": "2026-09-14T09:49:29+00:00", "progress": 1.0, "unit": "subscription",
    "mode": "quick", "metric": "ActualCost", "total": 3, "done": 3,
    "loaded_rows": 11894, "preserved_days": 42, "failed": 0, "empty": 0,
    "detail": "1 of 3 subscriptions returned no rows.",
    "archive": {"name": "2026-09-14-ActualCost.csv", "rows": 11894},
    # Never anyone's business on this endpoint.
    "rows": 11894, "total_cost": 9000, "total_by_currency": [{"currency": "USD", "cost": 9000}],
    "subscriptions": 12, "source": "private-account",
    "timings": [{"subscription": "Production", "rows": 9000}],
}


JOB_ID = "11111111-2222-4333-8444-555555555555"


def call_warehouse_status(admin: bool, state: dict, monkeypatch, *, durable=False):
    from app import jobs

    warehouse = SimpleNamespace(state=dict(state),
                                summary=lambda scope: {"total_cost": 10, "subscriptions": 1})
    monkeypatch.setitem(sys.modules, "app.warehouse", SimpleNamespace(warehouse=warehouse))
    record = {"job_id": JOB_ID, "scope": ["allowed"], "status": state["status"],
              "progress": dict(state), "finished": state.get("finished")}
    monkeypatch.setattr(jobs, "listing", Mock(return_value=[record] if durable else []))
    namespace = {"__package__": "app", "JSONResponse": JSONResponse,
                 "current_user": lambda request: SimpleNamespace(admin=admin),
                 "permitted": AsyncMock(return_value=["allowed"]),
                 "narrow": lambda picked, allowed: allowed,
                 "offload": AsyncMock(side_effect=lambda fn, *args, **kw: fn(*args, **kw))}
    endpoint = load_function("app/main.py", "warehouse_status", namespace)
    return json.loads(asyncio.run(endpoint(object())).body)["ingest"]


@pytest.mark.parametrize("durable", [False, True])
def test_operator_is_told_what_the_refresh_actually_did(monkeypatch, durable):
    """The fields the header reports with survive the filter for the only role that sees them.

    Every name below is read by `paintIngest` or `watchIngest` in dashboard.js. Dropping any
    of them is not a cosmetic loss: with `mode` gone the quick-refresh row count never prints,
    with `detail` gone a failure reports "No reason was reported", and with `done`/`total`
    gone the strip cannot say how far through it is.
    """
    ingest = call_warehouse_status(True, FINISHED_QUICK, monkeypatch, durable=durable)
    for key in ("status", "started", "finished", "progress", "unit", "mode", "metric",
                "total", "done", "loaded_rows", "preserved_days", "detail", "archive"):
        assert key in ingest, f"the refresh cannot report itself without {key!r}"
    assert ingest["loaded_rows"] == 11894
    assert ingest["mode"] == "quick"
    assert ingest["detail"] == FINISHED_QUICK["detail"]


@pytest.mark.parametrize("durable", [False, True])
def test_progress_still_never_carries_estate_wide_money(monkeypatch, durable):
    """Widening the projection must not reopen what it was narrowed for.

    `warehouse.state` ends a run with `summary()` spread over it, so it holds totals for the
    whole estate. Someone entitled to one subscription polls this endpoint throughout a
    refresh; none of that may reach them, admin or not.
    """
    for admin in (True, False):
        ingest = call_warehouse_status(admin, FINISHED_QUICK, monkeypatch, durable=durable)
        for leak in ("total_cost", "total_by_currency", "subscriptions", "rows",
                     "source", "timings"):
            assert leak not in ingest, f"{leak!r} escaped to admin={admin}"


@pytest.mark.parametrize("durable", [False, True])
def test_a_member_sees_progress_but_no_operational_detail(monkeypatch, durable):
    """Only admins can start a refresh, so only admins get its diagnostics.

    `detail` and `archive` name subscriptions and storage accounts the reader may hold no
    entitlement to, so they stay behind the same door as the button itself.
    """
    ingest = call_warehouse_status(False, FINISHED_QUICK, monkeypatch, durable=durable)
    assert ingest == {"status": "ready", "started": JOB_ID if durable else FINISHED_QUICK["started"],
                      "finished": "2026-09-14T09:49:29+00:00", "progress": 1.0,
                      "unit": "subscription",
                      **({"job_id": JOB_ID, "phase": "ready"} if durable else {})}


# ------------------------------------------------------------------ the start of a run

@pytest.fixture
def job_runtime(monkeypatch):
    """Exercise the real runner and completion callbacks with an in-memory job store."""
    from app import jobs

    records = {}
    warehouse = SimpleNamespace(state={"status": "ready", "started": "OLD", "progress": 1.0})
    monkeypatch.setitem(sys.modules, "app.warehouse", SimpleNamespace(warehouse=warehouse))
    monkeypatch.setenv("CLOUDLENS_READ_ONLY", "false")
    monkeypatch.setattr(jobs, "_workers", set())
    monkeypatch.setattr(jobs, "claim", lambda *args: True)
    monkeypatch.setattr(jobs, "release", Mock())
    monkeypatch.setattr(jobs, "get", lambda jid: deepcopy(records.get(jid)))

    def update(jid, **values):
        records[jid].update(deepcopy(values))
        return deepcopy(records[jid])

    monkeypatch.setattr(jobs, "update", update)
    claim = load_function("app/main.py", "_claim_ingest", {"__package__": "app"})

    def spawn(factory):
        jid = str(uuid4())
        records[jid] = {"job_id": jid, "status": "queued", "progress": {}, "scope": ["allowed"]}
        return jobs.spawn(records[jid], factory)

    return SimpleNamespace(warehouse=warehouse, records=records, claim=claim, spawn=spawn)


def test_a_started_refresh_says_so_before_the_job_gets_to_run(job_runtime):
    """`create_task` schedules; it does not run. The state has to be claimed anyway.

    The real job sets `status: "running"` only after enumerating subscriptions -- four seconds
    against a cold ARM connection. Anything polling in that window used to read the previous
    run's terminal state and conclude this one had already finished.
    """
    async def run():
        task = job_runtime.spawn(AsyncMock(return_value={"status": "ready"}))
        started = job_runtime.claim(task, mode="quick", metric="ActualCost")
        state = job_runtime.warehouse.state
        assert not task.done()
        assert state["status"] == "running"
        assert state["mode"] == "quick"
        assert state["started"] == state["job_id"] == task.job_id == started != "OLD"
        await task
        assert job_runtime.records[started]["status"] == "ready"

    asyncio.run(run())


def test_a_job_that_dies_without_reporting_is_not_left_running(job_runtime):
    """The safety net. A claim nobody resolves is worse than no claim at all."""
    async def run():
        task = job_runtime.spawn(AsyncMock(side_effect=RuntimeError("the pod went away")))
        started = job_runtime.claim(task)
        with pytest.raises(RuntimeError, match="the pod went away"):
            await task
        record = job_runtime.records[started]
        assert record["status"] == "failed"
        assert "the pod went away" in record["detail"]
        assert record["progress"]["job_id"] == record["progress"]["started"] == started

    asyncio.run(run())


def test_a_later_run_owns_the_state_and_an_old_callback_keeps_out(job_runtime):
    """Two refreshes in a row must not have the first one's epilogue overwrite the second."""
    async def run():
        task = job_runtime.spawn(AsyncMock(return_value=None))
        started = job_runtime.claim(task)
        newer = str(uuid4())
        current = {"status": "running", "started": newer, "job_id": newer}
        job_runtime.warehouse.state = dict(current)
        await task
        assert job_runtime.records[started]["status"] == "ready"
        assert job_runtime.warehouse.state == current

    asyncio.run(run())


# ------------------------------------------------------------------ the browser's watcher

def test_the_watcher_only_accepts_this_run_s_outcome():
    from dashboard_source import read_dashboard

    source = read_dashboard()
    watch = re.search(r"async function watchIngest\((.*?)\)\s*\{(.*?)\n\}", source, re.S)
    assert watch, "watchIngest is gone"
    assert watch.group(1).strip(), "the watcher has to know which run it is watching"
    body = watch.group(2)
    assert "(state.job_id || state.started) !== runStarted" in body, (
        "without this the first poll can accept a finished state from an earlier refresh")
    assert "?job_id=${encodeURIComponent(runStarted)}" in body
    assert "watchIngest(data.job_id || data.started)" in source, (
        "the server stamps the run; the start call has to pass it on")


# ------------------------------------------------------------------ the RBAC snapshot

@pytest.fixture
def sso(monkeypatch):
    from app import entra

    monkeypatch.setattr(entra, "SUBSCRIPTION_TTL", 300.0)
    monkeypatch.setattr(entra, "SUBSCRIPTION_GRACE", 900.0)
    return entra


def session_for(entra, age: float):
    """A signed-in person whose RBAC snapshot was taken `age` seconds ago."""
    import time

    return SimpleNamespace(
        subscriptions=[{"id": "sub-1", "name": "Production"}],
        checked=time.time() - age, revalidating=None, reauth_required=False,
        email="someone@example.com")


def test_a_fresh_snapshot_is_simply_used(sso):
    assert sso.EntraSSO._serve_stale(object(), session_for(sso, 10)) is True


def test_a_due_snapshot_answers_now_and_revalidates_behind(sso):
    """The fix for the five-minute stall.

    `permitted()` runs at the top of nearly every endpoint. When the TTL lapsed, the old path
    cleared the snapshot and re-asked ARM under a lock -- so the first request paid four
    seconds and every concurrent one queued behind it. That is the identical ~4s cluster seen
    across ten endpoints on a cold page load.
    """
    async def scenario():
        session = session_for(sso, 400)  # past the 300s TTL, inside the 900s grace
        served = sso.EntraSSO._serve_stale(SimpleNamespace(
            _revalidate=lambda s: asyncio.sleep(0)), session)
        assert served is True, "a due snapshot must not block the request"
        assert session.revalidating is not None, "and it must be revalidated behind the answer"
        await session.revalidating

    asyncio.run(scenario())


def test_one_revalidation_at_a_time(sso):
    async def scenario():
        session = session_for(sso, 400)
        owner = SimpleNamespace(_revalidate=lambda s: asyncio.sleep(0.05))
        sso.EntraSSO._serve_stale(owner, session)
        first = session.revalidating
        sso.EntraSSO._serve_stale(owner, session)
        assert session.revalidating is first, "two would race each other's answer in"
        await first

    asyncio.run(scenario())


def test_past_the_grace_window_entitlement_is_checked_before_it_is_used(sso):
    """The bound on the whole idea: stale access is tolerated briefly, never indefinitely."""
    session = session_for(sso, 300 + 900 + 1)
    assert sso.EntraSSO._serve_stale(object(), session) is False


def test_nothing_is_served_without_a_snapshot_or_after_reauth_is_demanded(sso):
    gone = session_for(sso, 400)
    gone.subscriptions = None
    assert sso.EntraSSO._serve_stale(object(), gone) is False

    locked_out = session_for(sso, 400)
    locked_out.reauth_required = True
    assert sso.EntraSSO._serve_stale(object(), locked_out) is False


def test_the_grace_window_can_be_switched_off(monkeypatch):
    """An operator who wants the old synchronous behaviour sets the window to zero."""
    from app import entra

    monkeypatch.setattr(entra, "SUBSCRIPTION_GRACE", 0.0)
    assert entra.EntraSSO._serve_stale(object(), session_for(entra, 10)) is False


def test_a_background_revalidation_does_not_blank_the_snapshot_it_is_replacing():
    """Otherwise the hole it opens is the stall it was written to avoid.

    The request path still clears first -- an expired snapshot must never survive a failed
    refresh. A background one is inside its grace window and is still the authoritative
    answer until a newer one lands, and every concurrent request reads it while it runs.
    """
    source = (ROOT / "app" / "entra.py").read_text(encoding="utf-8")
    body = re.search(r"async def _refresh_locked\(.*?\n    async def ", source, re.S)
    assert body, "_refresh_locked is gone"
    clearing = re.search(r"if not background:\n((?:\s{12}.*\n)+)", body.group(0))
    assert clearing, "the eager clear must be conditional on this not being a background run"
    assert "session.subscriptions = None" in clearing.group(1)
    assert "session.checked = 0" in clearing.group(1)
    # And a failed refresh still drops it, background or not.
    assert body.group(0).count("session.subscriptions = None") >= 2
