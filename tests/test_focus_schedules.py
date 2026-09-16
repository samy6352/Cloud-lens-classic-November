"""Offline schedule contracts and retained-report ingestion; no PostgreSQL or Azure needed."""
from __future__ import annotations

import ast
import asyncio
import copy
import importlib.util
import json
import runpy
import sqlite3
import sys
from contextlib import asynccontextmanager, contextmanager
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException

from app import archive, cost, onboarding, schedules

ACCOUNT = "/subscriptions/storage-sub/resourceGroups/test/providers/Microsoft.Storage/storageAccounts/offline"


def run(awaitable):
    return asyncio.run(awaitable)


@pytest.fixture(scope="module", autouse=True)
def offline_modules():
    """Load unchanged warehouse methods without its import-time PostgreSQL singleton."""
    import app

    global exports, main, COLUMNS
    before = dict(sys.modules)
    attrs = dict(vars(app))
    path = Path(__file__).resolve().parents[1] / "app" / "warehouse.py"
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(
                isinstance(t, ast.Name) and t.id == "warehouse" for t in node.targets):
            node.value = ast.parse("Warehouse.__new__(Warehouse)", mode="eval").body
    spec = importlib.util.spec_from_file_location("app.warehouse", path)
    module = importlib.util.module_from_spec(spec)
    sys.modules["app.warehouse"] = module
    app.warehouse = module
    exec(compile(ast.fix_missing_locations(tree), str(path), "exec"), module.__dict__)
    COLUMNS = module.COLUMNS
    from app import exports, main
    yield
    for key in list(sys.modules):
        if key.startswith("app.") and key not in before:
            sys.modules.pop(key, None)
    for key, value in before.items():
        if key.startswith("app."):
            sys.modules[key] = value
    for key in list(vars(app)):
        if key not in attrs:
            delattr(app, key)
    for key, value in attrs.items():
        setattr(app, key, value)


@pytest.fixture
def destination(monkeypatch):
    monkeypatch.setattr(archive, "ACCOUNT_ID", ACCOUNT)
    monkeypatch.setattr(archive, "ACCOUNT", "offline")
    monkeypatch.setattr(archive, "CONTAINER", "old-snapshots")
    monkeypatch.setattr(archive, "LOCATION", "eastus")
    monkeypatch.setattr(schedules, "FOCUS_CONTAINER", "focus-exports")
    monkeypatch.setattr(schedules, "FOCUS_PREFIX", "focus")


@pytest.mark.parametrize("cadence,period", [
    ("Daily", "MonthToDate"), ("Weekly", "MonthToDate"), ("Monthly", "TheLastMonth")])
def test_documented_payload_and_retention(destination, cadence, period):
    body = schedules._body("FocusCost", ACCOUNT, "focus-exports", "focus/sub-a", cadence)
    p = body["properties"]
    assert body["identity"] == {"type": "SystemAssigned"}
    assert p["definition"] == {"type": "FocusCost", "timeframe": period,
                               "dataSet": {"granularity": "Daily"}}
    assert p["dataOverwriteBehavior"] == "CreateNewReport"
    assert p["partitionData"] is True
    assert p["schedule"]["recurrence"] == cadence
    assert p["deliveryInfo"]["destination"] == {
        "type": "AzureBlob", "resourceId": ACCOUNT, "container": "focus-exports",
        "rootFolderPath": "focus/sub-a"}
    if cadence == "Monthly":
        assert p["schedule"]["recurrencePeriod"]["from"][8:10] == "01"


def test_monthly_last_day_and_leap_year_start(monkeypatch, destination):
    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return cls(2028, 2, 29, 23, 0, tzinfo=UTC)
    monkeypatch.setattr(schedules, "datetime", Clock)
    p = schedules._body("FocusCost", ACCOUNT, "focus-exports", "focus/sub-a",
                         "Monthly")["properties"]
    assert p["schedule"]["recurrencePeriod"]["from"] == "2028-03-01T02:00:00Z"


def test_stable_deployment_and_scope_names(destination):
    one = schedules.destination("sub-a")
    assert one["container"] != archive.CONTAINER
    assert schedules.export_name("FocusCost", **one) == schedules.export_name("FocusCost", **one)
    for different in ({**one, "account_id": ACCOUNT + "other"},
                      {**one, "container": "another-container"},
                      schedules.destination("sub-b")):
        assert schedules.export_name("FocusCost", **one) != schedules.export_name("FocusCost", **different)


def test_create_update_etag_and_no_duplicate_on_cadence_change(monkeypatch, destination):
    stored, calls = {}, []

    async def request(method, url, *, body=None, params=None):
        calls.append((method, url, copy.deepcopy(body)))
        if method == "GET":
            if url not in stored:
                raise RuntimeError("404 ExportNotFound")
            return copy.deepcopy(stored[url])
        if method == "PUT":
            if url in stored:
                assert body["eTag"] == stored[url]["eTag"]
            saved = {**copy.deepcopy(body), "name": url.rsplit("/", 1)[-1], "eTag": "v2",
                     "identity": {"type": "SystemAssigned", "principalId": "export-principal"}}
            stored[url] = saved
            return saved
        raise AssertionError(method)

    monkeypatch.setattr(schedules, "_request", request)
    grant = AsyncMock(return_value="granted")
    monkeypatch.setattr(schedules, "_grant_storage", grant)
    options = {"account_id": ACCOUNT, "container": "focus-exports", "run_now": False}
    first = run(schedules.create("sub-a", "FocusCost", **options))
    changed = run(schedules.create("sub-a", "FocusCost", recurrence="Monthly", etag="v2", **options))
    assert first["name"] == changed["name"] and len(stored) == 1
    assert calls[-1][2]["eTag"] == "v2"
    assert calls[-1][2]["properties"]["definition"]["timeframe"] == "TheLastMonth"
    grant.assert_awaited_with("export-principal", ACCOUNT + "/blobServices/default/containers/focus-exports")
    count = len([c for c in calls if c[0] == "PUT"])
    with pytest.raises(schedules.ScheduleError, match="changed"):
        run(schedules.create("sub-a", "FocusCost", etag="stale", **options))
    assert len([c for c in calls if c[0] == "PUT"]) == count
    stored[next(iter(stored))]["properties"]["deliveryInfo"]["destination"]["container"] = "foreign"
    with pytest.raises(schedules.ScheduleError, match="another destination"):
        run(schedules.create("sub-a", "FocusCost", **options))


@pytest.mark.parametrize("metric", ["ActualCost", "AmortizedCost", "other"])
def test_removed_metrics_never_write(monkeypatch, destination, metric):
    request = AsyncMock()
    monkeypatch.setattr(schedules, "_request", request)
    with pytest.raises(schedules.ScheduleError, match="FOCUS"):
        run(schedules.create("sub-a", metric, account_id=ACCOUNT, container="focus-exports"))
    request.assert_not_awaited()


def test_listing_legacy_and_run_history(monkeypatch, destination):
    owned = {"name": schedules.export_name("FocusCost", **schedules.destination("sub-a")),
             **schedules._body("FocusCost", ACCOUNT, "focus-exports", "focus/sub-a")}
    owned["properties"]["runHistory"] = {"value": [
        {"properties": {"status": "Completed", "executionTime": "2026-09-07T02:00:00Z"}}]}
    legacy = copy.deepcopy(owned)
    legacy["name"] = "cloudlens-amortized-daily"
    legacy["properties"]["definition"]["type"] = "AmortizedCost"
    legacy["properties"]["dataOverwriteBehavior"] = "OverwritePreviousReport"

    async def request(method, url, **kwargs):
        if url.endswith("/exports"):
            return {"value": [owned, legacy]}
        assert kwargs["params"]["$expand"] == "runHistory"
        return owned if url.endswith(owned["name"]) else legacy

    monkeypatch.setattr(cost, "_resolve", AsyncMock(return_value=[{"id": "sub-a", "name": "A"}]))
    monkeypatch.setattr(schedules, "_request", request)
    result = run(schedules.listing(["sub-a"]))
    rows = {s["name"]: s for s in result["schedules"]}
    assert rows[owned["name"]]["owned"] and not rows[owned["name"]]["legacy"]
    assert rows[legacy["name"]]["legacy"] and not rows[legacy["name"]]["owned"]
    assert rows[owned["name"]]["last_status"] == "Completed"
    assert result["metrics"] == ["FocusCost"]


def user_request(admin=True, source="local"):
    return SimpleNamespace(state=SimpleNamespace(user=SimpleNamespace(
        admin=admin, name="Offline", source=source, sid="sid-1")), query_params={})


def test_routes_enforce_scope_admin_identity_and_destination(monkeypatch, destination):
    from app import focus_sync

    monkeypatch.setattr(focus_sync, "capture", AsyncMock(return_value=[]))
    monkeypatch.setattr(focus_sync, "record", AsyncMock())
    monkeypatch.setattr(main, "permitted", AsyncMock(return_value=["sub-a"]))
    delegated = []

    @asynccontextmanager
    async def as_user(user):
        delegated.append(user)
        yield

    monkeypatch.setattr(main, "as_user", as_user)
    create = AsyncMock(return_value={"name": "owned"})
    run_now, remove = AsyncMock(return_value={}), AsyncMock(return_value={})
    monkeypatch.setattr(schedules, "create", create)
    monkeypatch.setattr(schedules, "run_now", run_now)
    monkeypatch.setattr(schedules, "remove", remove)
    req = user_request()
    response = run(main.create_schedule(main.ScheduleRequest(subscription="sub-a"), req))
    assert response.status_code == 201
    assert create.await_args.kwargs["account_id"] == ACCOUNT
    assert create.await_args.kwargs["container"] == "focus-exports"
    assert create.await_args.args == ("sub-a", "FocusCost")
    run(main.run_schedule(req, "sub-a", "legacy"))
    run(main.delete_schedule(req, "sub-a", "legacy"))
    assert len(delegated) == 3
    for op in (main.run_schedule, main.delete_schedule):
        with pytest.raises(HTTPException) as failure:
            run(op(req, "sub-b", "legacy"))
        assert failure.value.status_code == 403
        with pytest.raises(HTTPException):
            run(op(user_request(admin=False), "sub-a", "legacy"))
    with pytest.raises(HTTPException):
        run(main.create_schedule(main.ScheduleRequest(subscription="sub-b"), req))

    # App-identity mode. The write would run as CloudLens rather than as this person, so their
    # own Azure role decides it -- refusing everyone outright left such a deployment unable to
    # create the one export it can actually reach.
    monkeypatch.setattr(main, "auth_service",
                        lambda: SimpleNamespace(sso=SimpleNamespace(
                            delegated=False, get=lambda sid: SimpleNamespace(oid="oid-1"))))
    entra = user_request(source="entra").state.user

    monkeypatch.setattr(cost, "principal_can_write_cost", AsyncMock(return_value=False))
    with pytest.raises(HTTPException, match="Cost Management Contributor"):
        run(main.assert_may_write_schedule(entra, "sub-a"))

    writable = AsyncMock(return_value=True)
    monkeypatch.setattr(cost, "principal_can_write_cost", writable)
    run(main.assert_may_write_schedule(entra, "sub-a"))
    assert writable.await_args.args == ("oid-1", "sub-a")

    # A local deployment admin is unaffected either way.
    run(main.assert_may_write_schedule(user_request().state.user, "sub-a"))


def report(run_id, end, submitted, cost_value=3, name="owned", sub="sub-a", timeframe="MonthToDate"):
    path = f"focus/{sub}/{name}/{run_id}"
    csv = ("ChargePeriodStart,BilledCost,EffectiveCost,BillingCurrency,SubAccountId\n"
           f"2026-08-01,{cost_value},{cost_value},USD,{sub}\n").encode()
    manifest = {
        "runInfo": {"runId": run_id, "startDate": "2026-08-01",
                    "endDate": end, "submittedTime": submitted},
        "exportConfig": {"exportName": name, "type": "FocusCost", "timeFrame": timeframe},
        "dataRowCount": 1,
        "blobs": [{"blobName": f"{path}/part.csv", "dataRowCount": 1}],
    }
    return {f"{path}/manifest.json": json.dumps(manifest).encode(), f"{path}/part.csv": csv}


class Reader:
    account, container = "offline", "focus-exports"

    def __init__(self, blobs):
        self.blobs = blobs
        self.fetched = []

    async def list(self, prefix=""):
        return [{"name": n, "modified": "2026-09-01", "size": len(v)}
                for n, v in self.blobs.items() if n.startswith(prefix)]

    async def get(self, name):
        self.fetched.append(name)
        return self.blobs[name]

    async def close(self):
        pass


class OfflineConnection(sqlite3.Connection):
    lock_requests = 0

    def execute(self, sql, parameters=()):
        if sql == "LOCK TABLE costs IN SHARE ROW EXCLUSIVE MODE":
            self.lock_requests += 1
            return super().execute("SELECT 1")
        return super().execute(sql, parameters)


def test_oversized_export_leaves_existing_rows_untouched(monkeypatch, memory):
    ingest(monkeypatch, report("old", "2026-08-10", "2026-08-11", 10))
    before = memory.snapshot()
    monkeypatch.setattr(exports, "EXPORT_MAX_ROWS", 0)
    with pytest.raises(exports.ExportError, match="row limit"):
        ingest(monkeypatch, report("new", "2026-08-20", "2026-08-21", 20))
    assert memory.snapshot() == before


def test_gzip_expansion_limit(monkeypatch):
    import gzip

    monkeypatch.setattr(exports, "EXPORT_MAX_BYTES", 100)
    with pytest.raises(exports.ExportError, match="memory safety"):
        exports._rows_from(gzip.compress(b"a" * 101))


class MemoryWarehouse:
    def __init__(self):
        self.db = sqlite3.connect(":memory:", check_same_thread=False, isolation_level=None,
                                  factory=OfflineConnection)
        self.db.execute("CREATE TABLE costs (" + ",".join(f'"{c}"' for c in COLUMNS) + ")")
        self.db.execute("CREATE TABLE ingest_log (a,b,c,d,e,f,g)")
        self.db.execute("CREATE TABLE meta (key TEXT PRIMARY KEY,value TEXT,updated_at TEXT)")
        self.db.create_function("now", 0, lambda: datetime.now(UTC).isoformat())
        self.state = {}

    @contextmanager
    def connect(self, **kwargs):
        yield self.db

    async def refresh_rollup_async(self):
        pass

    async def quick_ingest(self, *args, **kwargs):
        raise AssertionError("Quick Azure reads must be mocked")

    def summary(self):
        return {}

    def row_count(self):
        return self.db.execute("SELECT COUNT(*) FROM costs").fetchone()[0]

    @contextmanager
    def reader(self):
        def rows(sql):
            cur = self.db.execute(sql)
            columns = [d[0] for d in cur.description]
            return [dict(zip(columns, values)) for values in cur.fetchall()]
        yield SimpleNamespace(rows=rows)

    def snapshot(self):
        return self.db.execute('SELECT "SubAccountId","ChargePeriodStart","BilledCost" FROM costs').fetchall()


@pytest.fixture
def memory(monkeypatch):
    from uuid import uuid4

    from app import jobs
    from app.warehouse import Warehouse

    monkeypatch.setattr(MemoryWarehouse, "state", Warehouse.state, raising=False)
    records = {}
    def create(kind, scope, **kwargs):
        jid = str(uuid4())
        record = {"job_id": jid, "started": jid, "scope": scope, "status": "queued",
                  "progress": {}, "kind": kind}
        records[jid] = record
        return record
    def update(jid, **kwargs):
        records[jid].update(kwargs)
        return records[jid]
    monkeypatch.setattr(jobs, "create", create)
    monkeypatch.setattr(jobs, "get", lambda jid: records[jid])
    monkeypatch.setattr(jobs, "update", update)
    monkeypatch.setattr(jobs, "claim", lambda *a: True)
    monkeypatch.setattr(jobs, "release", lambda *a: None)
    monkeypatch.setattr(jobs, "assert_lease", lambda *a: None)
    monkeypatch.setattr(jobs, "publish_generation", AsyncMock())
    wh = MemoryWarehouse()
    monkeypatch.setattr(exports, "warehouse", wh)
    import app.warehouse as module
    monkeypatch.setattr(module, "warehouse", wh)
    monkeypatch.setattr(exports, "insert_rows", lambda con, rows: con.executemany(
        "INSERT INTO costs VALUES (" + ",".join("?" for _ in COLUMNS) + ")", rows))
    yield wh
    wh.db.close()


def ingest(monkeypatch, blobs, **kwargs):
    reader = Reader(blobs)
    monkeypatch.setattr(exports, "BlobReader", lambda *a: reader)
    result = run(exports.ingest_export(account="offline", container="focus-exports",
                                       required_metric="FocusCost", **kwargs))
    return result, reader


def test_retained_advancing_mtd_and_closed_month_never_sum(monkeypatch, memory):
    old = report("old", "2026-08-10", "2026-08-11", 10)
    new = report("new", "2026-08-20", "2026-08-21", 20)
    closed = report("closed", "2026-08-31", "2026-09-01", 30, timeframe="TheLastMonth")
    result, reader = ingest(monkeypatch, {**old, **new, **closed}, export_name="owned")
    assert result["rows"] == 1 and result["runs"] == ["closed"]
    assert memory.snapshot() == [("sub-a", "2026-08-01", 30)]
    assert not any("/old/part.csv" in p or "/new/part.csv" in p for p in reader.fetched)
    ingest(monkeypatch, {**old, **new, **closed}, export_name="owned")
    assert len(memory.snapshot()) == 1


def test_multiple_subscriptions_and_corrected_removed_day(monkeypatch, memory):
    ingest(monkeypatch, report("a", "2026-08-31", "2026-09-01", sub="sub-a"), scope=["sub-a"])
    ingest(monkeypatch, report("b", "2026-08-31", "2026-09-01", sub="sub-b"), scope=["sub-b"])
    memory.db.execute('UPDATE costs SET "ChargePeriodStart" = ? WHERE "SubAccountId" = ?',
                      ["2026-08-28", "sub-a"])
    ingest(monkeypatch, report("a2", "2026-08-31", "2026-09-02", 9, sub="sub-a"), scope=["sub-a"])
    assert sorted(memory.snapshot()) == [
        ("sub-a", "2026-08-01", 9), ("sub-b", "2026-08-01", 3)]


def test_incomplete_latest_and_caps_leave_old_data(monkeypatch, memory):
    old = report("old", "2026-08-10", "2026-08-11", 10)
    ingest(monkeypatch, old)
    before = memory.snapshot()
    new = report("new", "2026-08-20", "2026-08-21", 20)
    new.pop(next(k for k in new if k.endswith(".csv")))
    with pytest.raises(KeyError):
        ingest(monkeypatch, {**old, **new})
    assert memory.snapshot() == before
    monkeypatch.setenv("EXPORT_MAX_MANIFESTS", "1")
    with pytest.raises(exports.ExportError, match="EXPORT_MAX_MANIFESTS"):
        ingest(monkeypatch, {**old, **new})
    assert memory.snapshot() == before
    with pytest.raises(exports.ExportError, match="max_files"):
        ingest(monkeypatch, old, max_files=0)
    assert memory.snapshot() == before


def test_no_guessing_missing_manifest_or_mixed_definitions(monkeypatch, memory):
    blobs = {**report("a", "2026-08-20", "2026-08-21", name="a"),
             **report("b", "2026-08-20", "2026-08-21", name="b")}
    with pytest.raises(exports.ExportError, match="Multiple export"):
        ingest(monkeypatch, blobs)
    result, _ = ingest(monkeypatch, blobs, export_name="a")
    assert result["runs"] == ["a"]
    with pytest.raises(exports.ExportError, match="manifest"):
        ingest(monkeypatch, {k: v for k, v in blobs.items() if k.endswith(".csv")})


def test_onboarding_focus_destination_and_quick_fallback(monkeypatch, destination):
    create = AsyncMock(return_value={})
    monkeypatch.setattr(schedules, "create", create)
    monkeypatch.setattr(schedules, "listing", AsyncMock(return_value={"schedules": [], "errors": []}))
    run(onboarding._create_exports([{"id": "sub-a"}]))
    assert create.await_args.args == ("sub-a", "FocusCost")
    assert create.await_args.kwargs["container"] == "focus-exports"
    import app.warehouse as wh
    fake = SimpleNamespace(quick_ingest=AsyncMock(return_value={"rows": 2}))
    monkeypatch.setattr(wh, "warehouse", fake)
    assert run(onboarding._load_from_api([{"id": "sub-a"}], 3)) == 2
    fake.quick_ingest.assert_awaited_once_with([{"id": "sub-a"}], months=3, metric="ActualCost")


def test_background_never_chooses_legacy_and_keeps_each_subscription(monkeypatch, destination):
    import app.warehouse as wh
    fake = SimpleNamespace(state={}, quick_ingest=AsyncMock(return_value={"rows": 2}))
    monkeypatch.setattr(wh, "warehouse", fake)
    found = [{"type": "ActualCost", "container": "old", "storage_resource_id": ACCOUNT},
             {"type": "FocusCost", "container": "new", "storage_resource_id": ACCOUNT}]
    monkeypatch.setattr(exports, "discover_exports", AsyncMock(return_value={"exports": found}))
    reachable = AsyncMock(return_value=([found[1]], []))
    monkeypatch.setattr(exports, "reachable", reachable)
    load = AsyncMock(return_value={"loaded": ["focus"], "problems": []})
    monkeypatch.setattr(main, "_load_export_pair", load)
    result = run(main._refresh_primary([{"id": "sub-a"}, {"id": "sub-b"}], 3))
    reachable.assert_awaited_once_with([found[1]])
    assert load.await_args.kwargs["scope"] == ["sub-a", "sub-b"]
    assert load.await_args.kwargs["required_metric"] == "FocusCost"
    assert result["rows"] == 2


def test_startup_sas_requires_focus_and_falls_back_explicitly(monkeypatch, memory):
    monkeypatch.setenv("COST_EXPORT_SAS_URL", "https://offline.invalid/focus")
    ingest = AsyncMock(return_value={"rows": 1})
    monkeypatch.setattr(exports, "ingest_export", ingest)
    monkeypatch.setattr(main, "_archive_after_ingest", AsyncMock())
    primary = AsyncMock(return_value={"rows": 1})
    monkeypatch.setattr(main, "_refresh_primary", primary)
    monkeypatch.setattr(cost, "list_subscriptions",
                        AsyncMock(return_value={"subscriptions": [{"id": "sub-a"}]}))
    run(main._run_ingest(3))
    assert ingest.await_args.kwargs["required_metric"] == "FocusCost"
    primary.assert_not_awaited()
    ingest.side_effect = exports.ExportError("FOCUS unavailable")
    run(main._run_ingest(3))
    primary.assert_awaited_once_with([{"id": "sub-a"}], 3)


def test_frontend_choices_approval_legacy_and_archive_mapping():
    root = Path(__file__).resolve().parents[1]
    ui = (root / "web/assets/dashboard-reports.js").read_text(encoding="utf-8")
    select = ui.split('<select id="sMetric">', 1)[1].split("</select>", 1)[0]
    assert select.count("<option") == 1 and 'value="FocusCost"' in select
    for cadence in schedules.RECURRENCE:
        assert f'value="{cadence}"' in ui
    assert ui.count("window.confirm(") >= 3
    assert "Edit cadence" in ui and "Legacy / external" in ui
    assert "s.can_write === true" in ui and "sub.can_write === true" in ui
    assert "Daily — recommended" in ui and '"/api/schedules/refresh"' in ui
    assert 'focus: "FOCUS", amortized: "Amortized", actual: "Actual"' in ui
    refresh = (root / "web/assets/dashboard.js").read_text(encoding="utf-8")
    menu = refresh.split("function buildRefreshMenu()", 1)[1].split("menu.innerHTML = `", 1)[1].split("`;", 1)[0]
    assert menu.count("data-act=") == 3
    assert all(f'data-act="{action}"' in menu for action in ("latest", "focus", "quick"))
    assert "reports.refreshLatest" in refresh and "cloudlens:focus-synced" in refresh


@pytest.mark.parametrize("script", ["test_scopes.py", "test_onboarding.py", "test_export_manifest.py"])
def test_existing_offline_script_regressions(memory, script):
    try:
        runpy.run_path(str(Path(__file__).with_name(script)), run_name="__main__")
    except SystemExit as exc:
        assert exc.code in (None, 0)


@pytest.mark.parametrize("name", [
    "test_manual_ingest_preserves_delegated_context_and_scope",
    "test_scoped_refresh_uses_existing_delegated_subscriptions_not_server_sas",
    "test_concurrent_export_refresh_claims_only_one_job_after_preflight",
    "test_export_discovery_uses_selected_delegated_context_and_recovers_preflight",
    "test_restricted_sas_refresh_passes_authorized_scope_and_delegated_context",
    "test_scoped_export_pair_refreshes_each_selected_subscription",
    "test_shared_billing_export_is_filtered_for_each_selected_subscription",
    "test_focus_refresh_only_attempts_focus_and_passes_strict_constraint",
    "test_focus_sas_refresh_requires_focus_before_archiving",
    "test_quick_replacement_filters_whole_protected_days_and_binds_scope",
])
def test_existing_refresh_contracts(monkeypatch, memory, name):
    cases = runpy.run_path(str(Path(__file__).with_name("test_release_runtime.py")))
    cases[name](monkeypatch)


def test_competing_update_is_not_blindly_retried(monkeypatch, destination):
    existing = schedules._body("FocusCost", ACCOUNT, "focus-exports", "focus/sub-a")
    existing["eTag"] = "v1"
    request = AsyncMock(side_effect=[existing, RuntimeError("412 PreconditionFailed")])
    monkeypatch.setattr(schedules, "_request", request)
    with pytest.raises(schedules.ScheduleError, match="412"):
        run(schedules.create("sub-a", "FocusCost", account_id=ACCOUNT,
                             container="focus-exports", run_now=False))
    assert request.await_count == 2
    assert request.await_args.kwargs["body"]["eTag"] == "v1"


def test_create_requests_run_only_after_identity_grant(monkeypatch, destination):
    calls = []
    async def request(method, url, **kwargs):
        calls.append(method)
        if method == "GET":
            raise RuntimeError("404")
        if method == "PUT":
            return {"identity": {"principalId": "writer"}}
        return {}
    async def grant(*args):
        calls.append("grant")
        return "granted"
    monkeypatch.setattr(schedules, "_request", request)
    monkeypatch.setattr(schedules, "_grant_storage", grant)
    monkeypatch.setattr(schedules.asyncio, "sleep", AsyncMock())
    made = run(schedules.create("sub-a", "FocusCost", account_id=ACCOUNT,
                               container="focus-exports", run_now=True))
    assert made["first_run_started"] and calls == ["GET", "PUT", "grant", "POST"]


def test_blob_listing_cap_is_fail_closed_but_probe_is_bounded(monkeypatch):
    client = SimpleNamespace(get=AsyncMock(return_value=SimpleNamespace(
        status_code=200, text="<Blobs><Blob><Name>part.csv</Name></Blob></Blobs>"
                              "<NextMarker>next+page</NextMarker>")))
    @asynccontextmanager
    async def http_client(**kwargs):
        yield client
    monkeypatch.setattr(exports.httpx, "AsyncClient", http_client)
    reader = exports.BlobReader("offline", "focus-exports")
    monkeypatch.setattr(reader, "_headers", AsyncMock(return_value={}))
    with pytest.raises(exports.ExportError, match="safety limit"):
        run(reader.list(limit=1))
    assert len(run(reader.list(limit=1, require_complete=False))) == 1
    assert client.get.await_count == 2


def test_infra_adds_container_without_changing_snapshot_destination():
    root = Path(__file__).resolve().parents[1]
    text = (root / "infra/modules/stack.bicep").read_text(encoding="utf-8")
    assert "name: 'focus-exports'" in text and "name: 'cloudlens'" in text
    assert "name: 'FOCUS_EXPORT_CONTAINER'" in text
    assert "name: 'ARCHIVE_CONTAINER'\n          value: exportsContainer.name" in text
    assert "allowSharedKeyAccess: false" in text
