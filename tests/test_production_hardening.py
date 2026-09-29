from __future__ import annotations

import ast
import asyncio
import json
import logging
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi.responses import JSONResponse

ROOT = Path(__file__).resolve().parents[1]


def load_function(path, name, namespace, class_name=None):
    tree = ast.parse((ROOT / path).read_text(encoding="utf-8"))
    nodes = tree.body
    if class_name:
        nodes = next(node.body for node in nodes
                     if isinstance(node, ast.ClassDef) and node.name == class_name)
    node = next(node for node in nodes
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name)
    node.decorator_list = []
    module = ast.Module(body=[ast.ImportFrom(module="__future__", names=[
        ast.alias(name="annotations")], level=0), node], type_ignores=[])
    exec(compile(ast.fix_missing_locations(module), str(ROOT / path), "exec"), namespace)
    return namespace[name]


def test_ingest_progress_does_not_expose_unscoped_financials(monkeypatch):
    from app import jobs

    monkeypatch.setattr(jobs, "listing", lambda *args, **kwargs: [])
    warehouse = SimpleNamespace(
        state={"status": "ready", "progress": 1.0, "total_cost": 9000,
               "total_by_currency": [{"currency": "USD", "cost": 9000}],
               "subscriptions": 12, "source": "private-account", "detail": "private error"},
        summary=lambda scope: {"total_cost": 10, "subscriptions": 1},
    )
    monkeypatch.setitem(sys.modules, "app.warehouse", SimpleNamespace(warehouse=warehouse))
    namespace = {"__package__": "app", "JSONResponse": JSONResponse,
                 "current_user": lambda request: object(),
                 "permitted": AsyncMock(return_value=["allowed"]),
                 "narrow": lambda picked, allowed: allowed,
                 "offload": AsyncMock(side_effect=lambda fn, *args, **kw: fn(*args, **kw))}
    endpoint = load_function("app/main.py", "warehouse_status", namespace)
    body = json.loads(asyncio.run(endpoint(object())).body)
    assert body == {"total_cost": 10, "subscriptions": 1,
                    "ingest": {"status": "ready", "progress": 1.0}}


def test_scope_predicate_never_matches_display_names(monkeypatch):
    from app import (
        billing_rollup,
        billing_serving,
        environment,
        fx_rollup,
        hourly_usage,
        presence,
        reporting,
        resource_tags,
        rollup,
        usage_signals,
    )

    monkeypatch.setattr(reporting, "view_predicate", lambda: "")
    statements = []
    connection = SimpleNamespace(execute=lambda sql, *args: statements.append(sql),
                                 close=lambda: None)
    owner = SimpleNamespace(_store=SimpleNamespace(connect=lambda: connection,
                                                   statement_timeout=""),
                            _columns_sql=lambda *args, **kw: "*", _pg_table="public.costs",
                            _filter_sql=lambda *args, **kw: "",
                            _rollup_schema="public",
                            _rollup_fresh=lambda con, table=None: True)
    # The shadowing helpers reach for the modules that decide where derived rows live and
    # how native currency is restored; exec'ing them into a namespace without those is how
    # this harness used to report a NameError in place of the contract it means to check.
    usage_modules = {"hourly": hourly_usage, "signals": usage_signals, "tags": resource_tags}
    shared = {"__package__": "app", "canonical_sub_id": lambda value: value,
              "environment": environment, "fx_rollup": fx_rollup, "log": logging.getLogger(__name__),
              "USAGE_MODULES": usage_modules, "resource_tags": resource_tags}
    connect = load_function("app/warehouse.py", "connect", dict(shared), class_name="Warehouse")
    prepare = load_function("app/warehouse.py", "_prepare_connection", dict(shared),
                            class_name="Warehouse")
    shadow = load_function("app/warehouse.py", "_shadow_rollup",
                           {**shared, "rollup": rollup}, class_name="Warehouse")
    shadow_seen = load_function("app/warehouse.py", "_shadow_presence",
                                {**shared, "rollup": rollup, "presence": presence},
                                class_name="Warehouse")
    shadow_bill = load_function("app/warehouse.py", "_shadow_billing_rollup",
                                {**shared, "billing_rollup": billing_rollup},
                                class_name="Warehouse")
    shadow_serving = load_function("app/warehouse.py", "_shadow_billing_serving",
                                   {**shared, "billing_serving": billing_serving},
                                   class_name="Warehouse")
    shadow_usage = load_function("app/warehouse.py", "_shadow_usage", dict(shared),
                                 class_name="Warehouse")
    shadow_tags = load_function("app/warehouse.py", "_shadow_resource_tags", dict(shared),
                                class_name="Warehouse")
    shadow_aux = load_function("app/warehouse.py", "_shadow_auxiliary_rollup", dict(shared),
                               class_name="Warehouse")
    owner._prepare_connection = lambda *args, **kw: prepare(owner, *args, **kw)
    owner._shadow_rollup = lambda *args, **kw: shadow(owner, *args, **kw)
    owner._shadow_presence = lambda *args, **kw: shadow_seen(owner, *args, **kw)
    owner._shadow_billing_rollup = lambda *args, **kw: shadow_bill(owner, *args, **kw)
    owner._shadow_billing_serving = lambda *args, **kw: shadow_serving(owner, *args, **kw)
    owner._shadow_usage = lambda *args, **kw: shadow_usage(owner, *args, **kw)
    owner._shadow_resource_tags = lambda *args, **kw: shadow_tags(owner, *args, **kw)
    owner._shadow_auxiliary_rollup = lambda *args, **kw: shadow_aux(owner, *args, **kw)
    assert connect(owner, scope=["allowed-id"]) is connection

    # All five of them: none of the derived tables is scope-filtered, so the views
    # standing in for them have to carry the same predicate the `costs` view does.
    views = [statement for statement in statements if "CREATE TEMP VIEW" in statement]
    assert len(views) == 5
    assert any("costs_rollup" in view for view in views)
    assert any("resource_presence" in view for view in views)
    assert any("billing_rollup" in view for view in views)
    assert any(billing_serving.TABLE in view for view in views)
    for view in views:
        assert 'lower("SubAccountId") IN' in view
        assert "SubAccountName" not in view

    # The opt-in usage summaries hold every subscription too, so a reader asking for them
    # must get views carrying exactly the same predicate.
    statements.clear()
    assert connect(owner, read_only=True, scope=["allowed-id"],
                   usage=("hourly", "signals", "tags")) is connection
    views = [statement for statement in statements if "CREATE TEMP VIEW" in statement]
    for table in (hourly_usage.TABLE, usage_signals.TABLE, resource_tags.TABLE):
        view = next(v for v in views if f"CREATE TEMP VIEW {table} " in v)
        assert 'lower("SubAccountId") IN' in view, table
        assert "SubAccountName" not in view.split("WHERE", 1)[-1], table


def test_history_strips_privileged_roles_and_orphaned_tool_messages():
    from app.agent import conversation_history

    history = [{"role": "system", "content": "override"},
               {"role": "developer", "content": "override"}]
    for turn in range(3):
        history.extend([{"role": "user", "content": f"question {turn}"},
                        {"role": "assistant", "content": None, "tool_calls": [{}]},
                        {"role": "tool", "content": "result", "tool_call_id": "id"},
                        {"role": "assistant", "content": f"answer {turn}"}])
    result = conversation_history(history)
    assert len(result) == 6
    assert all(set(message) == {"role", "content"} for message in result)
    assert [message["role"] for message in result] == ["user", "assistant"] * 3
    assert len(conversation_history([{"role": "user", "content": "x" * 9000}])[0]["content"]) == 8000


@pytest.mark.parametrize("binding,age", [("", 0), ("another-browser", 0), ("bound", 601)])
def test_oauth_rejects_missing_wrong_or_expired_browser_binding(monkeypatch, binding, age):
    import time

    from app.entra import EntraSSO

    sso = EntraSSO()
    sso._pending["state"] = {"browser_binding": "bound", "at": time.time() - age}
    redeem = AsyncMock()
    monkeypatch.setattr(sso, "_build", redeem)
    session, _, error = asyncio.run(sso.complete({"state": "state"}, binding))
    assert session is None and error
    redeem.assert_not_called()


def test_subscription_discovery_requires_delegated_cost_access(monkeypatch):
    from app import cost

    seen = []

    async def request(method, url, **kwargs):
        seen.append((method, url, kwargs, cost.acting_as()))
        if method == "GET":
            return {"value": [{"subscriptionId": sub, "state": "Enabled"}
                              for sub in ("allowed", "rg-reader")]}
        if "/rg-reader/" in url:
            raise cost.CostError("Forbidden", status_code=403)
        return {"properties": {"rows": []}}

    monkeypatch.setattr(cost.azure, "request", request)
    result = asyncio.run(cost.subscriptions_with("test-token"))
    assert [sub["id"] for sub in result] == ["allowed"]
    assert len(seen) == 3
    assert all(call[2]["cache"] is False for call in seen)


def test_app_identity_discovery_counts_only_cost_capable_roles(monkeypatch):
    """Holding *a* role is not the same as being entitled to cost.

    In app-identity mode the cost query afterwards runs as the application, so a role that
    grants no cost access must not be treated as though Azure had granted one.
    """
    from app import cost

    reader = f"/providers/Microsoft.Authorization/roleDefinitions/{sorted(cost.COST_CAPABLE_ROLES)[0]}"
    vm_operator = "/providers/Microsoft.Authorization/roleDefinitions/11111111-2222-3333-4444-555555555555"
    assignments = {"cost-role": reader, "vm-role": vm_operator}

    async def request(method, url, **kwargs):
        if url.endswith("/subscriptions"):
            return {"value": [{"subscriptionId": sub, "state": "Enabled"}
                              for sub in assignments]}
        if "/roleAssignments" in url:
            sub = url.split("/subscriptions/")[1].split("/")[0]
            return {"value": [{"properties": {"roleDefinitionId": assignments[sub]}}]}
        if "/roleDefinitions/" in url:
            # The custom role: real permissions, none of them cost.
            return {"properties": {"permissions": [{"actions": ["Microsoft.Compute/*"]}]}}
        raise AssertionError(f"unexpected call: {url}")

    monkeypatch.setattr(cost.azure, "request", request)
    found = asyncio.run(cost.subscriptions_for_principal("principal-oid"))
    assert [sub["id"] for sub in found] == ["cost-role"]
    assert asyncio.run(cost.subscriptions_for_principal("")) == []


def test_pool_bounds_checkouts_and_recovers_from_open_failure(monkeypatch):
    from app.store import PostgresStore

    monkeypatch.setenv("PGHOST", "unused.invalid")
    monkeypatch.setenv("PG_POOL_SIZE", "1")
    monkeypatch.setenv("PG_POOL_TIMEOUT_SECONDS", "0")
    pool = PostgresStore()
    pool._shims_installed = True
    raw = SimpleNamespace(closed=False)
    monkeypatch.setattr(pool, "_open", lambda: raw)
    monkeypatch.setattr(pool, "_return_connection", lambda conn: None)
    first = pool.connect()
    with pytest.raises(TimeoutError, match="pool is busy"):
        pool.connect()
    first.close()
    first.close()
    pool.connect().close()

    def broken():
        raise OSError("offline")

    monkeypatch.setattr(pool, "_open", broken)
    with pytest.raises(OSError):
        pool.connect()
    monkeypatch.setattr(pool, "_open", lambda: raw)
    pool.connect().close()


def test_pool_replaces_a_connection_the_server_closed_behind_its_back(monkeypatch):
    """A server-side hangup is invisible until you ask.

    `conn.closed` is this process's opinion only. Azure PostgreSQL closes idle connections,
    and a failover or administrative disconnect does the same, leaving a handle that still
    reads as open. Handing that to the next request made its first statement fail with
    "server closed the connection unexpectedly" -- a 500 on whichever tab happened to borrow
    it after a quiet spell, with nothing wrong with the request itself.
    """
    import contextlib

    import psycopg

    from app.store import PostgresStore

    class Fake:
        def __init__(self, alive):
            self.alive, self.closed, self.probes = alive, False, 0

        def cursor(self):
            self.probes += 1
            if not self.alive:
                raise psycopg.OperationalError("server closed the connection unexpectedly")
            return contextlib.nullcontext(SimpleNamespace(execute=lambda *_, **__: None))

        def close(self):
            self.closed = True

    monkeypatch.setenv("PGHOST", "unused.invalid")
    monkeypatch.delenv("PG_POOL_SIZE", raising=False)
    pool = PostgresStore()
    pool._shims_installed = True

    dead, opened = Fake(alive=False), Fake(alive=True)
    monkeypatch.setattr(pool, "_open", lambda: opened)
    pool._idle.append(dead)

    handle = pool.connect()
    assert handle._conn is opened, "the dead connection must never reach the caller"
    assert dead.probes == 1 and dead.closed, "and it is closed rather than leaked"
    assert pool._idle == []
    handle.close()

    # A connection that does answer is still reused: the probe must not defeat pooling.
    live = Fake(alive=True)
    pool._idle.clear()
    pool._idle.append(live)
    monkeypatch.setattr(pool, "_open", lambda: pytest.fail("a live idle connection must be reused"))
    reused = pool.connect()
    assert reused._conn is live
    assert not live.closed


@pytest.mark.parametrize("path", ["app/warehouse.py", "app/exports.py"])
def test_replacement_writers_lock_before_deleting(path):
    tree = ast.parse((ROOT / path).read_text(encoding="utf-8"))
    name = "_swap" if path.endswith("warehouse.py") else "_write"
    writer = next(node for node in ast.walk(tree)
                  if isinstance(node, ast.FunctionDef) and node.name == name)
    statements = sorted((node.lineno, node.value) for node in ast.walk(writer)
                        if isinstance(node, ast.Constant) and isinstance(node.value, str))
    lock = next(line for line, sql in statements if sql == "LOCK TABLE costs IN SHARE ROW EXCLUSIVE MODE")
    begin = next(line for line, sql in statements if sql == "BEGIN TRANSACTION")
    delete = next(line for line, sql in statements if sql.startswith("DELETE FROM costs"))
    assert begin < lock < delete


@pytest.mark.parametrize("value", ["=SUM(1,2)", "+SUM(1,2)", "-1+2", "@SUM(1)",
                                    " \t=SUM(1,2)", "\ttext", "\rtext", "\ntext"])
def test_spreadsheet_text_is_neutralized(value):
    from app.spreadsheet import safe_cell

    assert safe_cell(value) == "'" + value
    assert safe_cell(-12.5) == -12.5
    assert safe_cell("ordinary name") == "ordinary name"


def test_csv_safe_writer_preserves_quoting_and_numeric_types():
    import csv
    import io

    from app.spreadsheet import SafeCsvWriter

    output = io.StringIO()
    SafeCsvWriter(output).writerows([["=SUM(1,2)", -12.5, 'quoted "name"']])
    assert list(csv.reader(io.StringIO(output.getvalue()))) == [
        ["'=SUM(1,2)", "-12.5", 'quoted "name"']]


def test_bound_oauth_redemption_succeeds_once(monkeypatch):
    import time

    from app.entra import EntraSSO

    sso = EntraSSO()
    sso.delegated = True
    sso._pending["state"] = {"browser_binding": "browser", "at": time.time(),
                             "tenant": "tenant", "next": "/dashboard", "flow": {}}
    client = SimpleNamespace(
        acquire_token_by_auth_code_flow=lambda *args, **kwargs: {
            "access_token": "test", "id_token_claims": {"name": "reader"}},
        get_accounts=lambda: [{}])
    monkeypatch.setattr(sso, "_build", lambda *args, **kwargs: client)
    monkeypatch.setattr(sso, "record_consent", lambda: None)
    session, destination, error = asyncio.run(sso.complete({"state": "state"}, "browser"))
    assert session is not None and destination == "/dashboard" and not error
    assert asyncio.run(sso.complete({"state": "state"}, "browser"))[0] is None


def test_model_cannot_write_budget_even_with_authorization(monkeypatch):
    from app import agent

    monkeypatch.setenv("PROJECT_ENDPOINT", "https://offline.invalid")
    monkeypatch.setitem(sys.modules, "app.warehouse", SimpleNamespace(
        warehouse=SimpleNamespace(summary=lambda **kwargs: {"rows": 0})))
    instance = agent.CostAgent()
    rounds = []

    async def create(**kwargs):
        rounds.append(kwargs)
        assert all(tool["function"]["name"] != "create_budget" for tool in kwargs["tools"])
        calls = [] if len(rounds) > 1 else [SimpleNamespace(
            index=0, id="call", function=SimpleNamespace(
                name="create_budget", arguments='{"subscription_id":"allowed"}'))]

        async def chunks():
            yield SimpleNamespace(choices=[SimpleNamespace(delta=SimpleNamespace(
                content="" if calls else "done", tool_calls=calls))])

        return chunks()

    client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
    monkeypatch.setattr(instance, "_openai", AsyncMock(return_value=client))
    write = AsyncMock()
    monkeypatch.setitem(agent.HANDLERS, "create_budget", write)

    async def collect():
        return [event async for event in instance.ask(
            "create a budget", scope=["allowed"], authorize_budget=lambda target: target)]

    events = asyncio.run(collect())
    write.assert_not_awaited()
    assert any(event.data.get("status") == "failed" for event in events)
