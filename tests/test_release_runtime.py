"""Hermetic authorization, refresh concurrency and archive regression checks."""
from __future__ import annotations

import asyncio
import io
import json
import threading
import time
from contextlib import asynccontextmanager, contextmanager
from datetime import date
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock
from urllib.parse import parse_qs, urlsplit

import httpx
import pyarrow as pa
import pyarrow.parquet as pq
import pytest
from fastapi import HTTPException

from app import agent, archive, auth, cost, entra, exports, main
from app.warehouse import warehouse

A = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
B = "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"


def snapshot(a: float, b: float) -> bytes:
    table = pa.table({
        "SubAccountId": [A, B], "SubAccountName": ["one", "two"],
        "ServiceName": ["allowed-service", "private-service"], "BilledCost": [a, b],
        "BillingCurrency": ["USD", "EUR"],
        "ChargePeriodStart": [date(2026, 8, 1), date(2025, 1, 1)],
    })
    data = io.BytesIO()
    pq.write_table(table, data)
    return data.getvalue()


def request(user: auth.User) -> SimpleNamespace:
    return SimpleNamespace(state=SimpleNamespace(user=user), query_params={})


def test_env_password_sessions_survive_reload_and_new_workers(monkeypatch):
    monkeypatch.setenv("AUTH_PASSWORD", "hermetic-password-one")
    monkeypatch.setenv("AUTH_USERNAME", "operator")
    monkeypatch.delenv("AUTH_USERS", raising=False)
    path = Path(__file__).parent / "absent-runtime-users.json"
    assert not path.exists()
    users = auth.Users(path)
    sessions = auth.Sessions(users, b"hermetic-signing-key")
    user = users.check("operator", "hermetic-password-one")
    token = sessions.issue(user)
    original_hash = users._records["operator"]["hash"]
    users.load()
    assert users._records["operator"]["hash"] != original_hash
    assert sessions.verify(token) == user
    other = auth.Sessions(auth.Users(path), b"hermetic-signing-key")
    assert other.verify(token) == user
    monkeypatch.setenv("AUTH_PASSWORD", "hermetic-password-two")
    users.load()
    assert sessions.verify(token) is None
    assert users.check("operator", "hermetic-password-two") == user


def test_archive_comparison_filters_both_snapshots_and_route_scope(monkeypatch):
    monkeypatch.setattr(archive, "enabled", lambda: True)
    reads = []

    async def get(self, name):
        reads.append(name)
        return snapshot(10.0, 900.0) if "08-01" in name else snapshot(15.0, 2000.0)

    monkeypatch.setattr(archive.BlobWriter, "get", get)
    monkeypatch.setattr(main, "permitted", AsyncMock(return_value=[A, B]))
    user = auth.User("reader", source="entra", sid="session")
    result = asyncio.run(main.archive_compare(
        request(user), earlier="2026-08-01", later="2026-08-02", scope=A))
    body = json.loads(result.body)
    assert len(reads) == 2
    assert body["scope"] == [A]
    assert body["earlier"]["cost"] == 10
    assert body["later"]["cost"] == 15
    assert body["total_delta"] == 5
    assert body["earlier"]["from"] == "2026-08-01"
    assert body["earlier"]["rows"] == 1
    assert body["currency"] == "USD"
    assert [c["key"] for c in body["changes"]] == ["allowed-service"]
    assert "private-service" not in result.body.decode()


def test_archive_no_access_or_invalid_selection_never_reads(monkeypatch):
    get = AsyncMock()
    monkeypatch.setattr(archive.BlobWriter, "get", get)
    monkeypatch.setattr(main, "permitted", AsyncMock(return_value=[A]))
    user = auth.User("reader", source="entra")
    with pytest.raises(HTTPException) as refused:
        asyncio.run(main.archive_compare(
            request(user), earlier="2026-08-01", later="2026-08-02", scope=B))
    assert refused.value.status_code == 403
    monkeypatch.setattr(main, "permitted", AsyncMock(side_effect=main.NoAccess("no access")))
    with pytest.raises(main.NoAccess):
        asyncio.run(main.archive_compare(request(user)))
    get.assert_not_awaited()
    raw = io.BytesIO()
    pq.write_table(pa.table({"BilledCost": [9.0]}), raw)
    with pytest.raises(archive.ArchiveError, match="subscription IDs"):
        archive._snapshot_totals(raw.getvalue(), scope=[A])


def test_archive_listing_follows_encoded_markers_and_returns_latest(monkeypatch):
    urls = []
    marker = "https://untrusted.example/?x=1&y=2"

    async def respond(req):
        urls.append(req.url)
        second = bool(parse_qs(urlsplit(str(req.url)).query).get("marker"))
        day = "09-06" if second else "01-01"
        continuation = "" if second else marker.replace("&", "&amp;")
        return httpx.Response(200, text=(
            "<EnumerationResults><Blobs><Blob>"
            f"<Name>focus/2026/{day[:2]}/focus-2026-{day}.parquet</Name>"
            "<Properties><Content-Length>123</Content-Length></Properties>"
            f"</Blob></Blobs><NextMarker>{continuation}</NextMarker></EnumerationResults>"
        ))

    original = httpx.AsyncClient
    monkeypatch.setattr(archive.httpx, "AsyncClient",
                        lambda **kw: original(transport=httpx.MockTransport(respond), **kw))
    writer = archive.BlobWriter("testaccount", "archive")
    monkeypatch.setattr(writer, "_headers", AsyncMock(return_value={}))
    result = asyncio.run(writer.list(prefix="focus/&stuff", limit=1))
    assert result[0]["name"].endswith("2026-09-06.parquet")
    assert len(urls) == 2
    assert all(u.host == "testaccount.blob.core.windows.net" for u in urls)
    query = parse_qs(urlsplit(str(urls[1])).query)
    assert query["marker"] == [marker]
    assert query["prefix"] == ["focus/&stuff"]


def test_archive_listing_never_claims_latest_on_truncation(monkeypatch):
    async def respond(req):
        return httpx.Response(200, text=(
            "<EnumerationResults><Blobs/><NextMarker>more</NextMarker></EnumerationResults>"))

    original = httpx.AsyncClient
    monkeypatch.setattr(archive.httpx, "AsyncClient",
                        lambda **kw: original(transport=httpx.MockTransport(respond), **kw))
    monkeypatch.setattr(archive, "_LIST_MAX_PAGES", 1)
    writer = archive.BlobWriter("testaccount", "archive")
    monkeypatch.setattr(writer, "_headers", AsyncMock(return_value={}))
    with pytest.raises(archive.ArchiveError, match="scan limit"):
        asyncio.run(writer.list())


def session_for(sso):
    session = entra.Session(sid="session", name="reader", email="reader@example.invalid",
                            oid="object", tenant="test", app=None, account={})
    session.subscriptions = [{"id": A}]
    session.caller = cost.Caller(fallback="old-test-token")
    # These tests exercise an expired authorization snapshot, not background revalidation.
    session.checked = time.time() - entra.SUBSCRIPTION_TTL - max(0, entra.SUBSCRIPTION_GRACE) - 1
    sso._sessions[session.sid] = session
    return session


def test_revoked_token_clears_authorization_and_requires_reauth(monkeypatch):
    sso = entra.EntraSSO()
    sso.delegated = True
    session = session_for(sso)
    monkeypatch.setattr(sso, "token", AsyncMock(return_value=None))
    assert asyncio.run(sso.subscriptions(session)) == []
    assert session.caller is None
    assert session.subscriptions is None
    assert session.checked == 0
    assert sso.get(session.sid) is None


def test_transient_refresh_failure_denies_stale_access_but_allows_retry(monkeypatch):
    sso = entra.EntraSSO()
    sso.delegated = True
    sso.multi_tenant = False
    session = session_for(sso)
    monkeypatch.setattr(sso, "token",
                        AsyncMock(side_effect=[httpx.ConnectError("offline"), "new-test-token"]))
    monkeypatch.setattr(cost, "subscriptions_with", AsyncMock(return_value=[{"id": B}]))
    assert asyncio.run(sso.subscriptions(session)) == []
    assert session.caller is None
    assert session.checked == 0
    assert sso.get(session.sid) is session
    assert asyncio.run(sso.subscriptions(session)) == [{"id": B, "name": None}]
    assert session.checked > 0


@pytest.mark.parametrize("error,revoked", [
    ("invalid_grant", True), ("interaction_required", True),
    ("temporarily_unavailable", False), ("server_error", False),
])
def test_msal_refresh_errors_distinguish_reauth_from_transport(monkeypatch, error, revoked):
    sso = entra.EntraSSO()
    sso.delegated = True
    session = session_for(sso)
    session.app = SimpleNamespace(
        acquire_token_silent_with_error=lambda *args, **kw: {"error": error})
    assert asyncio.run(sso.subscriptions(session)) == []
    assert session.caller is None and session.checked == 0
    assert session.reauth_required is revoked
    assert (sso.get(session.sid) is None) is revoked


def test_login_client_construction_and_redemption_are_off_event_loop(monkeypatch):
    sso = entra.EntraSSO()
    sso.delegated = True
    sso._pending["state"] = {"tenant": "chosen", "next": "/dashboard", "flow": {},
                             "browser_binding": "browser", "at": __import__("time").time()}
    thread = threading.get_ident()
    seen = []

    class Client:
        def acquire_token_by_auth_code_flow(self, flow, params, scopes):
            seen.append(threading.get_ident())
            return {"access_token": "test-token", "id_token_claims": {"name": "reader"}}

        def get_accounts(self):
            seen.append(threading.get_ident())
            return [{"id": "account"}]

    def build(cache, tenant):
        assert tenant == "chosen"
        seen.append(threading.get_ident())
        return Client()

    monkeypatch.setattr(sso, "_build", build)
    monkeypatch.setattr(sso, "record_consent", lambda: None)
    session, destination, error = asyncio.run(sso.complete({"state": "state"}, "browser"))
    assert session is not None and destination == "/dashboard" and not error
    assert len(seen) == 3 and all(t != thread for t in seen)
    assert sso.get(session.sid) is session


@pytest.mark.parametrize("admin,delegated,target,allowed", [
    (False, False, A, False),
    (True, False, A.upper(), True),
    (False, True, A, True),
    (False, True, B, False),
    (True, False, A + "/../" + B, False),
])
def test_budget_authorization_uses_role_and_canonical_scope(
        monkeypatch, admin, delegated, target, allowed):
    monkeypatch.setattr(main, "auth_service",
                        lambda: SimpleNamespace(sso=SimpleNamespace(delegated=delegated)))
    user = auth.User("reader", admin=admin, source="entra")
    if allowed:
        assert main.authorize_budget(user, target, [A]) == A
    else:
        with pytest.raises((ValueError, PermissionError)):
            main.authorize_budget(user, target, [A])


def model_client(tool_args=None):
    prompts = []
    round_number = 0

    async def create(**kw):
        nonlocal round_number
        prompts.append(kw["messages"][0]["content"])
        round_number += 1
        calls = []
        if tool_args is not None and round_number == 1:
            calls = [SimpleNamespace(index=0, id="call", function=SimpleNamespace(
                name="create_budget", arguments=json.dumps(tool_args)))]

        async def chunks():
            delta = SimpleNamespace(content="" if calls else "done", tool_calls=calls)
            yield SimpleNamespace(choices=[SimpleNamespace(delta=delta)])

        return chunks()

    return SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create))), prompts


def test_agent_prompt_reads_only_scoped_summary_on_worker(monkeypatch):
    monkeypatch.setenv("PROJECT_ENDPOINT", "https://hermetic.invalid/")
    client, prompts = model_client()
    instance = agent.CostAgent()
    monkeypatch.setattr(instance, "_openai", AsyncMock(return_value=client))
    main_thread = threading.get_ident()

    def summary(scope=None):
        assert scope == [A]
        assert threading.get_ident() != main_thread
        return {"rows": 2, "from": "2026-08-01", "to": "2026-08-02",
                "mixed_currency": True, "currencies": ["USD", "EUR"],
                "total_by_currency": [{"currency": "USD", "cost": 10},
                                      {"currency": "EUR", "cost": 20}]}

    measured = []

    def checked_summary(scope=None):
        result = summary(scope)
        measured.append(scope)
        return result

    monkeypatch.setattr(warehouse, "summary", checked_summary)

    async def collect():
        return [e async for e in instance.ask("cost?", scope=[A])]

    asyncio.run(collect())
    assert measured == [[A]]
    assert "USD 10.00" in prompts[0] and "EUR 20.00" in prompts[0]


def test_agent_warehouse_status_tool_uses_enforced_scope(monkeypatch):
    seen = []

    def summary(scope=None):
        seen.append(scope)
        return {"rows": 1, "cost": 10}

    monkeypatch.setattr(warehouse, "summary", summary)
    result = asyncio.run(agent.warehouse_status(_scope=[A]))
    assert seen == [[A]]
    assert result["cost"] == 10 and result["scope"] == [A]


@pytest.mark.parametrize("authorized", [False, True])
def test_agent_budget_dispatch_cannot_trust_model_authorization(monkeypatch, authorized):
    monkeypatch.setenv("PROJECT_ENDPOINT", "https://hermetic.invalid/")
    args = {"subscription_id": A}
    if not authorized:
        args["authorize_budget"] = True
    client, _ = model_client(args)
    instance = agent.CostAgent()
    monkeypatch.setattr(instance, "_openai", AsyncMock(return_value=client))
    monkeypatch.setattr(warehouse, "summary", lambda **kw: {"rows": 0})
    write = AsyncMock(return_value={"created": True})
    monkeypatch.setitem(agent.HANDLERS, "create_budget", write)
    authorization = (lambda target: main.authorize_budget(
        auth.User("local", admin=True), target, [A])) if authorized else None

    async def collect():
        return [e async for e in instance.ask(
            "budget", scope=[A], authorize_budget=authorization)]

    events = asyncio.run(collect())
    write.assert_not_awaited()
    assert any(e.data.get("status") == "failed" for e in events)


def test_ask_route_enforces_selected_scope_before_budget_write(monkeypatch):
    monkeypatch.setenv("PROJECT_ENDPOINT", "https://hermetic.invalid/")
    client, _ = model_client({"subscription_id": B})
    instance = agent.CostAgent()
    monkeypatch.setattr(instance, "_openai", AsyncMock(return_value=client))
    monkeypatch.setattr(main, "agent", lambda: instance)
    monkeypatch.setattr(main, "permitted", AsyncMock(return_value=[A, B]))
    monkeypatch.setattr(warehouse, "subscriptions", lambda: [{"id": A, "name": "one"}])
    monkeypatch.setattr(warehouse, "summary", lambda **kw: {"rows": 0})
    write = AsyncMock()
    monkeypatch.setitem(agent.HANDLERS, "create_budget", write)

    async def exercise():
        response = await main.ask(main.Ask(question="budget", scope=[A]),
                                  request(auth.User("local", admin=True)))
        return [part async for part in response.body_iterator]

    parts = asyncio.run(exercise())
    write.assert_not_awaited()
    assert any("Chat cannot execute budget changes" in part for part in parts)


def test_delegated_budget_route_reaches_azure_as_user(monkeypatch):
    user = auth.User("reader", source="entra", sid="session")
    session = SimpleNamespace(oid="person", sid="session")
    sso = SimpleNamespace(delegated=True, get=lambda sid: session,
                          caller=AsyncMock(return_value=cost.Caller(fallback="test-token")))
    monkeypatch.setattr(main, "auth_service", lambda: SimpleNamespace(sso=sso))
    monkeypatch.setattr(main, "permitted", AsyncMock(return_value=[A]))
    seen = []

    async def create(**kw):
        seen.append((cost._caller_id.get(), kw["subscription_id"]))
        return {"name": kw["name"]}

    monkeypatch.setattr(agent.budgets, "create", create)
    body = main.BudgetRequest(subscription=A.upper(), name="budget", amount=100)
    result = asyncio.run(main.create_budget(body, request(user)))
    assert result.status_code == 201
    assert seen == [("person", A)]
    assert not cost.acting_as_caller()


def test_manual_ingest_preserves_delegated_context_and_scope(monkeypatch):
    user = auth.User("admin", admin=True, source="entra", sid="session")
    monkeypatch.setattr(main, "permitted", AsyncMock(return_value=[A, B]))
    monkeypatch.setattr(main, "_ingest_task", None)
    seen = []

    @asynccontextmanager
    async def as_user(who):
        reset = cost.act_as("test-token", who.sid)
        try:
            yield
        finally:
            cost.stop_acting(reset)

    async def run(months, metric, quick=False, scope=None):
        seen.append((cost._caller_id.get(), scope))

    monkeypatch.setattr(main, "as_user", as_user)
    monkeypatch.setattr(main, "_run_ingest", run)

    async def exercise():
        result = await main.start_ingest(request(user), quick=True, scope=A)
        await main._ingest_task
        return result

    result = asyncio.run(exercise())
    assert json.loads(result.body)["status"] == "started"
    assert seen == [("session", [A])]


def test_scoped_refresh_uses_existing_delegated_subscriptions_not_server_sas(monkeypatch):
    monkeypatch.setenv("COST_EXPORT_SAS_URL", "https://unused.invalid/")
    monkeypatch.setattr(cost, "list_subscriptions", AsyncMock(return_value={
        "subscriptions": [{"id": A}, {"id": B}], "count": 2}))
    ingest = AsyncMock()
    monkeypatch.setattr(warehouse, "quick_ingest", ingest)
    monkeypatch.setattr(main, "_archive_after_ingest", AsyncMock())
    blob_ingest = AsyncMock()
    monkeypatch.setattr(exports, "ingest_export", blob_ingest)
    asyncio.run(main._run_ingest(3, quick=True, scope=[A]))
    ingest.assert_awaited_once_with([{"id": A}], months=3, metric="ActualCost")
    blob_ingest.assert_not_awaited()


def test_concurrent_export_refresh_claims_only_one_job_after_preflight(monkeypatch):
    monkeypatch.setattr(archive, "ACCOUNT_ID", "")
    monkeypatch.setattr(archive, "ACCOUNT", "account")
    user = auth.User("admin", admin=True)
    monkeypatch.setattr(main, "_ingest_task", None)
    monkeypatch.setattr(main, "permitted", AsyncMock(return_value=None))
    monkeypatch.delenv("COST_EXPORT_SAS_URL", raising=False)
    export = {"id": "export", "name": "export", "container": "container",
              "storage_account": "account", "root_folder": "", "type": "FocusCost",
              "subscription_id": A}
    started = []

    async def exercise():
        both = asyncio.Event()
        release = asyncio.Event()
        entered = 0

        async def discover(scope=None, *, deployment_only=False):
            assert deployment_only
            nonlocal entered
            entered += 1
            if entered == 2:
                both.set()
            await both.wait()
            return {"exports": [export]}

        async def load(*args, **kwargs):
            started.append(True)
            await release.wait()
            return {"loaded": ["export"]}

        monkeypatch.setattr(exports, "discover_exports", discover)
        monkeypatch.setattr(exports, "reachable", AsyncMock(return_value=([export], [])))
        monkeypatch.setattr(main, "_load_export_pair", load)
        results = await asyncio.gather(*[
            main.ingest_from_export(main.ExportIngest(), request(user)) for _ in range(2)])
        statuses = [json.loads(r.body)["status"] for r in results]
        assert sorted(statuses) == ["already running", "started"]
        task = main._ingest_task
        assert task is not None and not task.done()
        release.set()
        await task
        assert len(started) == 1

    asyncio.run(exercise())


def test_export_discovery_uses_selected_delegated_context_and_recovers_preflight(monkeypatch):
    monkeypatch.setattr(archive, "ACCOUNT_ID", "")
    monkeypatch.setattr(archive, "ACCOUNT", "account")
    user = auth.User("admin", admin=True, source="entra", sid="session")
    monkeypatch.setattr(main, "_ingest_task", None)
    monkeypatch.setattr(main, "permitted", AsyncMock(return_value=[A, B]))
    monkeypatch.delenv("COST_EXPORT_SAS_URL", raising=False)
    session = SimpleNamespace(oid="person", sid="session")
    sso = SimpleNamespace(delegated=True, get=lambda sid: session,
                          caller=AsyncMock(return_value=cost.Caller(fallback="test-token")))
    monkeypatch.setattr(main, "auth_service", lambda: SimpleNamespace(sso=sso))
    export = {"id": "export", "name": "export", "container": "container",
              "storage_account": "account", "root_folder": "", "type": "FocusCost",
              "subscription_id": A}
    seen = []

    async def discover(scope, *, deployment_only=False):
        assert deployment_only
        seen.append(("discover", cost._caller_id.get(), scope))
        return {"exports": [export]}

    async def load(*args, **kw):
        seen.append(("load", cost._caller_id.get(), None))
        return {"loaded": ["export"]}

    monkeypatch.setattr(exports, "discover_exports", discover)
    reachable = AsyncMock(side_effect=[([], [(export, "offline")]), ([export], [])])
    monkeypatch.setattr(exports, "reachable", reachable)
    monkeypatch.setattr(main, "_load_export_pair", load)

    async def exercise():
        with pytest.raises(HTTPException):
            await main.ingest_from_export(main.ExportIngest(scope=[A]), request(user))
        assert main._ingest_task is None
        result = await main.ingest_from_export(main.ExportIngest(scope=[A]), request(user))
        await main._ingest_task
        assert json.loads(result.body)["status"] == "started"

    asyncio.run(exercise())
    assert seen == [("discover", "person", [A]), ("discover", "person", [A]),
                    ("load", "person", None)]


def test_restricted_sas_refresh_passes_authorized_scope_and_delegated_context(monkeypatch):
    monkeypatch.setattr(main, "_ingest_task", None)
    monkeypatch.setattr(main, "permitted", AsyncMock(return_value=[A]))
    user = auth.User("admin", admin=True, source="entra", sid="session")
    session = SimpleNamespace(oid="person", sid="session")
    sso = SimpleNamespace(delegated=True, get=lambda sid: session,
                          caller=AsyncMock(return_value=cost.Caller(fallback="test-token")))
    monkeypatch.setattr(main, "auth_service", lambda: SimpleNamespace(sso=sso))
    seen = []

    async def ingest(**kw):
        seen.append((cost._caller_id.get(), kw["scope"], kw["sas_url"]))

    monkeypatch.setattr(exports, "ingest_export", ingest)
    monkeypatch.setattr(main, "_archive_after_ingest", AsyncMock())

    async def exercise():
        response = await main.ingest_from_export(
            main.ExportIngest(sas_url="https://hermetic.invalid/"), request(user))
        assert json.loads(response.body)["status"] == "started"
        await main._ingest_task

    asyncio.run(exercise())
    assert seen == [("person", [A], "https://hermetic.invalid/")]


def test_scoped_export_pair_refreshes_each_selected_subscription(monkeypatch):
    candidates = [
        {"subscription_id": sub, "name": f"{sub}-{period}", "type": "FocusCost",
         "storage_account": "account", "container": "container", "root_folder": "",
         "timeframe": period}
        for sub in (A, B) for period in ("MonthToDate", "TheLastMonth")
    ]
    seen = []

    async def ingest(**kw):
        seen.append((kw["subscription_id"], kw["scope"]))
        return {"status": "ready"}

    monkeypatch.setattr(exports, "ingest_export", ingest)
    monkeypatch.setattr(main, "_archive_after_ingest", AsyncMock())
    monkeypatch.setattr(warehouse, "state", {"status": "ready"})
    result = asyncio.run(main._load_export_pair(candidates, scope=[A, B]))
    assert seen == [(A, [A]), (A, [A]), (B, [B]), (B, [B])]
    assert len(result["loaded"]) == 4
    assert result["current"] and result["closed"]


def test_shared_billing_export_is_filtered_for_each_selected_subscription(monkeypatch):
    candidate = {"subscription_id": None, "name": "billing", "type": "FocusCost",
                 "storage_account": "account", "container": "container", "root_folder": "",
                 "timeframe": "MonthToDate"}
    scopes = []

    async def ingest(**kw):
        scopes.append(kw["scope"])

    monkeypatch.setattr(exports, "ingest_export", ingest)
    monkeypatch.setattr(main, "_archive_after_ingest", AsyncMock())
    monkeypatch.setattr(warehouse, "state", {"status": "ready"})
    asyncio.run(main._load_export_pair([candidate], scope=[A, B]))
    assert scopes == [[A], [B]]


def test_comparison_never_sums_multiple_currencies():
    with pytest.raises(archive.ArchiveError, match="multiple currencies"):
        archive._snapshot_totals(snapshot(10.0, 20.0))


@pytest.mark.parametrize("metric", ["ActualCost", "AmortizedCost"])
def test_focus_refresh_refuses_other_export_types_before_start(monkeypatch, metric):
    monkeypatch.setattr(main, "_ingest_task", None)
    monkeypatch.setattr(main, "permitted", AsyncMock(return_value=[A]))
    monkeypatch.delenv("COST_EXPORT_SAS_URL", raising=False)
    candidate = {"id": "export", "type": metric, "container": "exports"}
    discover = AsyncMock(return_value={"exports": [candidate]})
    monkeypatch.setattr(exports, "discover_exports", discover)
    reachable = AsyncMock()
    monkeypatch.setattr(exports, "reachable", reachable)
    user = auth.User("admin", admin=True)
    for export_id in (None, "export"):
        with pytest.raises(HTTPException) as refused:
            asyncio.run(main.ingest_from_export(
                main.ExportIngest(scope=[A], metric="FocusCost", export_id=export_id), request(user)))
        assert refused.value.status_code in (400, 404)
        assert "Quick refresh" in refused.value.detail
        assert main._ingest_task is None
    assert discover.await_args.args == ([A],)
    reachable.assert_not_awaited()


def test_focus_refresh_only_attempts_focus_and_passes_strict_constraint(monkeypatch):
    monkeypatch.setattr(archive, "ACCOUNT_ID", "")
    monkeypatch.setattr(archive, "ACCOUNT", "account")
    monkeypatch.setattr(main, "_ingest_task", None)
    monkeypatch.setattr(main, "permitted", AsyncMock(return_value=[A, B]))
    monkeypatch.delenv("COST_EXPORT_SAS_URL", raising=False)
    focus = {"id": "focus", "name": "focus", "type": "FocusCost", "container": "exports",
             "storage_account": "account", "root_folder": "", "subscription_id": A}
    actual = {**focus, "id": "actual", "type": "ActualCost"}
    monkeypatch.setattr(exports, "discover_exports", AsyncMock(return_value={"exports": [actual, focus]}))
    reachable = AsyncMock(return_value=([focus], []))
    monkeypatch.setattr(exports, "reachable", reachable)
    load = AsyncMock(return_value={"loaded": ["focus"]})
    monkeypatch.setattr(main, "_load_export_pair", load)
    user = auth.User("admin", admin=True)

    async def run():
        response = await main.ingest_from_export(
            main.ExportIngest(metric="FocusCost", scope=[A]), request(user))
        await main._ingest_task
        assert json.loads(response.body)["type"] == "FocusCost"

    asyncio.run(run())
    reachable.assert_awaited_once_with([focus])
    load.assert_awaited_once_with([focus], sas_url=None, max_files=60, scope=[A],
                                  required_metric="FocusCost")


def test_focus_sas_refresh_requires_focus_before_archiving(monkeypatch):
    monkeypatch.setattr(main, "_ingest_task", None)
    monkeypatch.setattr(main, "permitted", AsyncMock(return_value=[A]))
    ingest = AsyncMock(side_effect=exports.ExportError("Not FOCUS; use Quick refresh."))
    monkeypatch.setattr(exports, "ingest_export", ingest)
    archive_call = AsyncMock()
    monkeypatch.setattr(main, "_archive_after_ingest", archive_call)
    monkeypatch.setattr(warehouse, "summary", lambda **kwargs: {})
    monkeypatch.setattr(warehouse, "state", {})

    async def run():
        req = request(auth.User("admin", admin=True))
        response = await main.ingest_from_export(main.ExportIngest(
            metric="FocusCost", sas_url="https://hermetic.invalid/exports", scope=[A]),
            req)
        accepted = json.loads(response.body)
        job_id = accepted.get("job_id") or accepted["started"]
        assert job_id == main._ingest_task.job_id
        await main._ingest_task
        status = await main.warehouse_status(req, job_id=job_id)
        return json.loads(status.body)["ingest"], job_id

    progress, job_id = asyncio.run(run())
    assert ingest.await_args.kwargs["required_metric"] == "FocusCost"
    assert ingest.await_args.kwargs["scope"] == [A]
    assert progress["status"] == "failed"
    assert progress["job_id"] == progress["started"] == job_id
    archive_call.assert_not_awaited()


@pytest.mark.parametrize("mode", ["deadline", "page-cap", "complete"])
def test_query_pagination_never_returns_a_partial_subscription(monkeypatch, mode):
    import app.warehouse as module

    monkeypatch.setattr(module, "QUERY_GAP", 0)
    instance = object.__new__(module.Warehouse)
    instance._query_locks, instance._last_query = {}, {}
    calls = 0
    clock = [0.0]
    monkeypatch.setattr(module.time, "monotonic", lambda: clock[0])

    async def post(*args, **kwargs):
        nonlocal calls
        calls += 1
        if mode == "deadline":
            clock[0] = 2.0
        props = {"columns": [{"name": "UsageDate"}, {"name": "Cost"}],
                 "rows": [[20260901, 10]]}
        if mode != "complete" or calls == 1:
            props["nextLink"] = "https://management.azure.com/next"
        return httpx.Response(200, json={"properties": props})

    async def run():
        client = SimpleNamespace(post=post)
        return await instance._query_period(
            client, {"id": A}, "2026-09-01", "2026-09-01", {}, "ActualCost",
            deadline=1 if mode == "deadline" else None)

    if mode == "complete":
        assert len(asyncio.run(run())) == 2
        assert calls == 2
    else:
        with pytest.raises((TimeoutError, RuntimeError), match="unchanged"):
            asyncio.run(run())
        assert calls == (1 if mode == "deadline" else 20)


def test_quick_replacement_filters_whole_protected_days_and_binds_scope(monkeypatch):
    import app.warehouse as module

    instance = object.__new__(module.Warehouse)
    instance.state, instance._lock = {}, asyncio.Lock()
    commands, inserted = [], []

    @contextmanager
    def connect():
        def execute(sql, params=None):
            commands.append((sql, params))
            return SimpleNamespace(fetchall=lambda: [] if "FROM meta" in sql else [(date(2026, 9, 1),)])
        yield SimpleNamespace(execute=execute)

    monkeypatch.setattr(instance, "connect", connect)
    monkeypatch.setattr(module, "insert_rows", lambda con, rows: inserted.extend(rows))
    rows = instance._map_query({
        "columns": [{"name": "UsageDate"}, {"name": "Cost"}],
        "rows": [[20260901, 99], [20260902, 12]],
    }, {"id": f"/subscriptions/{A.upper()}"}, "ActualCost")
    result = asyncio.run(instance._replace(f"/subscriptions/{A.upper()}",
                                          "2026-09-01", "2026-09-02", rows, preserve_detail=True))
    assert result == 1
    assert instance.state["preserved_days"] == 1
    assert inserted == [rows[1]]
    assert inserted[0][module.COLUMNS_INDEX["SubAccountId"]] == A
    delete, params = next(c for c in commands if c[0].startswith("DELETE"))
    assert '"SubAccountId" = ?' in delete and '"ChargePeriodStart" BETWEEN ? AND ?' in delete
    assert '"ChargePeriodStart" NOT IN (?)' in delete
    assert params == [A, "2026-09-01", "2026-09-02", "2026-09-01"]
    assert commands[0][0] == "BEGIN TRANSACTION" and commands[-1][0] == "COMMIT"
    commands.clear()
    with pytest.raises(ValueError, match="outside"):
        asyncio.run(instance._replace(B, "2026-09-01", "2026-09-02", rows, preserve_detail=True))
    assert not commands


@pytest.mark.parametrize("kind", ["legacy-schema", "actual-manifest", "saved-actual"])
def test_strict_focus_download_validation_precedes_any_database_write(monkeypatch, kind):
    from app.scopes import Manifest

    csv = ("Date,Cost,SubscriptionId\n2026-09-01,12," + A + "\n"
           if kind == "legacy-schema" else
           "ChargePeriodStart,BilledCost,EffectiveCost,SubAccountId,BillingCurrency,BillingCostBasis\n"
           f"2026-09-01,12,10,{A},USD,ActualCost\n")
    reader = SimpleNamespace(
        account="offline", container="exports", get=AsyncMock(return_value=csv.encode()),
        close=AsyncMock(),
    )
    manifest = Manifest.parse({
        "exportConfig": {"type": "ActualCost" if kind == "actual-manifest" else "FocusCost"},
        "runInfo": {"runId": "run", "startDate": "2026-09-01", "endDate": "2026-09-01",
                    "submittedTime": "2026-09-02T02:00:00Z"},
        "blobs": [{"blobName": "part.csv", "dataRowCount": 1}],
    })
    monkeypatch.setattr(exports.BlobReader, "from_sas_url", lambda url: reader)
    monkeypatch.setattr(exports, "read_manifests", AsyncMock(return_value=[manifest]))
    monkeypatch.setattr(warehouse, "state", {})
    writes = []
    monkeypatch.setattr(warehouse, "connect", lambda: writes.append(True))
    with pytest.raises(exports.ExportError, match="No data was changed"):
        asyncio.run(exports.ingest_export(
            sas_url="https://offline.invalid/exports", required_metric="FocusCost", scope=[A]))
    assert not writes
    assert warehouse.state["status"] == "failed"
    reader.close.assert_awaited_once()
