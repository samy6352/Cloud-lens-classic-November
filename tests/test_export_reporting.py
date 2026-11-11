import ast
import asyncio
import hashlib
import importlib.util
import json
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from app import export_access, export_reporting
from app.auth import User

pytestmark = pytest.mark.postgres
TENANT = "11111111-1111-1111-1111-111111111111"
OID = "22222222-2222-2222-2222-222222222222"
SUB = "44444444-4444-4444-4444-444444444444"
OTHER = "55555555-5555-5555-5555-555555555555"
PRINCIPAL = f"entra:{TENANT}:{OID}"
SOURCE = {"label": "dechow", "subscription": SUB, "account": "account123",
          "container": "focus-exports", "prefix": "focus/pinned", "export_name": "export123"}
HEADER = "SubAccountId,ChargePeriodStart,BillingCurrency,ServiceName,BilledCost,EffectiveCost\n"
ROW = f"{SUB},2026-11-01T00:00:00Z,USD,Compute,0.10,0.20\n"


@pytest.fixture(scope="module", autouse=True)
def offline_modules():
    """Keep the real export parser, without constructing its live warehouse singleton."""
    import app

    before, attrs = dict(sys.modules), dict(vars(app))
    path = Path(__file__).resolve().parents[1] / "app" / "warehouse.py"
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(
                isinstance(target, ast.Name) and target.id == "warehouse" for target in node.targets):
            node.value = ast.parse("Warehouse.__new__(Warehouse)", mode="eval").body
    spec = importlib.util.spec_from_file_location("app.warehouse", path)
    module = importlib.util.module_from_spec(spec)
    sys.modules["app.warehouse"] = app.warehouse = module
    exec(compile(ast.fix_missing_locations(tree), str(path), "exec"), module.__dict__)
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
def storage(monkeypatch):
    from app import exports

    state = {"csv": (HEADER + ROW + ROW.replace("0.10,0.20", "-0.03,0.00")).encode(),
             "closed": False}

    class Reader:
        def __init__(self, account, container):
            assert (account, container) == (SOURCE["account"], SOURCE["container"])

        async def list(self, **kwargs):
            assert kwargs["prefix"] == "focus/pinned"
            return [{"name": "focus/pinned/manifest.json", "modified": "2026-11-10"}]

        async def get(self, name):
            if name.endswith("manifest.json"):
                document = {
                    "runInfo": {"runId": "run123", "startDate": "2026-11-01",
                                "endDate": "2026-11-30", "submittedTime": "2026-11-10T12:00:00Z"},
                    "exportConfig": {"type": "FocusCost", "exportName": "export123",
                                     "timeFrame": "MonthToDate"},
                    "dataRowCount": 2, "byteCount": len(state["csv"]),
                    "blobs": [{"blobName": state.get("blob", "focus/pinned/part.csv"),
                               "byteCount": len(state["csv"]), "dataRowCount": 2}]}
                document.update(state.get("manifest", {}))
                return json.dumps(document).encode()
            return state["csv"]

        async def close(self):
            state["closed"] = True

    monkeypatch.setattr(exports, "BlobReader", Reader)
    return state


@pytest.fixture
def client(monkeypatch, postgres_warehouse):
    monkeypatch.setenv("EXPORT_REPORTING_ENABLED", "true")
    monkeypatch.setenv("CLOUDLENS_READ_ONLY", "false")
    monkeypatch.setenv("EXPORT_REPORTING_SOURCES", json.dumps({"dechow": SOURCE}))
    original = export_access.DatasetAccess
    store = postgres_warehouse._store
    monkeypatch.setattr(export_access, "DatasetAccess", lambda: original(store))
    state = SimpleNamespace(user=User("admin", True, "entra", sid="session"),
                            active=True, admin=True, oid=OID, tenant=TENANT)
    def session():
        return SimpleNamespace(tenant=state.tenant, oid=state.oid, email="verified@example.invalid")
    service = SimpleNamespace(sso=SimpleNamespace(
        get=lambda sid: session() if state.active and sid == "session" else None,
        is_admin=lambda email, oid: state.admin))
    app = FastAPI()

    def current_user(request):
        if state.user is None:
            raise HTTPException(401, "Sign in")
        return state.user

    export_reporting.install(app, current_user=current_user, auth_service=lambda: service,
                             web=Path(__file__).resolve().parents[1] / "web")
    with TestClient(app, headers={"Origin": "http://testserver"}) as http:
        yield http, state, original(store)


def onboard(http):
    response = http.post("/api/export-reporting/import", json={"source": "dechow"})
    assert response.status_code == 200, response.text
    return response.json()["dataset_id"]


def assert_uninitialized(repo):
    with repo._store_instance().connect() as con:
        assert con.execute("SELECT to_regclass('export_reporting_datasets')").fetchone()[0] is None


def test_complete_import_and_explicit_read_revocation(client, storage):
    http, state, repo = client
    dataset = onboard(http)
    report_url = f"/api/export-reporting/{dataset}/billing"
    grant_url = f"/api/export-reporting/{dataset}/grant"
    assert storage["closed"]
    assert http.get("/api/export-reporting").json()["datasets"] == []
    assert http.get(report_url).status_code == 403  # Admin is not implicitly a reader.
    assert http.post(grant_url, json={"principal": PRINCIPAL, "allow": True}).status_code == 200
    catalog = http.get("/api/export-reporting").json()["datasets"]
    assert catalog[0]["label"] == "dechow"
    assert catalog[0]["reporting_available"] and not catalog[0]["live_actions"]
    report = http.get(report_url).json()
    assert report["charges"] == [{"day": "2026-11-01", "service": "Compute", "currency": "USD",
                                  "billed_cost": "0.07", "effective_cost": "0.20", "rows": 2}]
    assert report["mode"] == "Export-backed"
    assert report["source_submitted"] and report["last_import"]
    assert repo.catalog(PRINCIPAL)[0]["dataset_id"] == dataset  # Durable fresh connection.
    state.oid = OTHER
    assert http.get(report_url).status_code == 403
    assert http.get("/api/export-reporting").json()["datasets"] == []
    state.oid, state.tenant = OID, OTHER
    assert http.get(report_url).status_code == 403
    state.tenant = TENANT
    assert http.post(grant_url, json={"principal": PRINCIPAL, "allow": False}).status_code == 200
    assert http.get(report_url).status_code == 403
    assert http.get("/api/export-reporting").json()["datasets"] == []
    with repo._connection() as con:
        assert con.execute("SELECT action FROM export_reporting_audit ORDER BY sequence").fetchall() == [
            ("record-import",), ("grant",), ("revoke",)]


@pytest.mark.parametrize("role", ["reader", "local", "expired", "revoked", "anonymous"])
def test_onboarding_requires_current_entra_admin(client, storage, role):
    http, state, repo = client
    if role == "reader":
        state.user = User("reader", source="entra", sid="session")
    elif role == "local":
        state.user = User("local", True)
    elif role == "expired":
        state.active = False
    elif role == "revoked":
        state.admin = False
    else:
        state.user = None
    assert http.post("/api/export-reporting/import", json={"source": "dechow"}).status_code in {401, 403}
    assert http.get("/api/export-reporting/admin").status_code in {401, 403}
    assert_uninitialized(repo)
    assert not storage["closed"]  # Refused before borrowing the app's storage credential.


def test_disabled_and_same_origin_and_input_boundaries(client, storage, monkeypatch):
    http, _, repo = client
    assert http.post("/api/export-reporting/import", json={"source": "dechow"},
                     headers={"Origin": "https://evil.invalid"}).status_code == 403
    assert http.post("/api/export-reporting/import", json={"source": "unknown"}).status_code == 400
    assert http.post("/api/export-reporting/import", json={"source": "dechow", "scope": [SUB]}).status_code == 422
    monkeypatch.setenv("CLOUDLENS_READ_ONLY", "true")
    assert http.post("/api/export-reporting/import", json={"source": "dechow"}).status_code == 403
    monkeypatch.setenv("EXPORT_REPORTING_ENABLED", "false")
    assert http.get("/api/export-reporting").status_code == 404
    assert_uninitialized(repo)


@pytest.mark.parametrize("change", ["subscription", "missing", "amount", "date", "count", "path", "export", "bytes", "currency", "duplicate"])
def test_invalid_complete_run_publishes_nothing(client, storage, change):
    http, _, repo = client
    if change == "subscription":
        storage["csv"] = storage["csv"].replace(SUB.encode(), OTHER.encode())
    elif change == "missing":
        storage["csv"] = storage["csv"].replace(b"BilledCost", b"Missing")
    elif change == "amount":
        storage["csv"] = storage["csv"].replace(b"0.10", b"NaN")
    elif change == "date":
        storage["csv"] = storage["csv"].replace(b"2026-11-01T", b"2026-10-01T")
    elif change == "count":
        storage["manifest"] = {"dataRowCount": 3}
    elif change == "path":
        storage["blob"] = "elsewhere/part.csv"
    elif change == "bytes":
        storage["manifest"] = {"byteCount": len(storage["csv"]) + 1}
    elif change == "currency":
        storage["csv"] = storage["csv"].replace(b"USD", b"")
    elif change == "duplicate":
        storage["csv"] = storage["csv"].replace(b"ServiceName", b"BillingCurrency")
    else:
        storage["manifest"] = {"exportConfig": {"type": "FocusCost", "exportName": "wrong"}}
    response = http.post("/api/export-reporting/import", json={"source": "dechow"})
    assert response.status_code == 400, response.text
    assert_uninitialized(repo)
    assert storage["closed"]


def test_reimport_isolated_ids_and_no_implicit_grant(client, storage):
    http, _, _ = client
    first, second = onboard(http), onboard(http)
    assert first != second
    assert http.post(f"/api/export-reporting/{first}/grant",
                     json={"principal": PRINCIPAL, "allow": True}).status_code == 200
    assert http.get(f"/api/export-reporting/{second}/billing").status_code == 403
    assert http.get(f"/api/export-reporting/{SUB}/billing").status_code == 403
    assert http.get("/api/export-reporting/not-a-uuid/billing").status_code == 422


def test_import_precision_and_digest(storage):
    report, digest, count = asyncio.run(export_reporting.read_report(SOURCE))
    assert len(digest) == len(hashlib.sha256().hexdigest()) and count == 2
    assert report["charges"][0]["billed_cost"] == "0.07"


def test_ui_safe_rendering_and_capabilities():
    root = Path(__file__).resolve().parents[1] / "web"
    html = (root / "export-reporting.html").read_text()
    script = (root / "assets" / "export-reporting.js").read_text()
    assert "Export-backed" in html and "disabled>Live refresh" in html
    assert "textContent" in script and "innerHTML" not in script
    assert "localStorage" not in script and 'cache: "no-store"' in script


def test_main_wires_separate_routes_without_export_entitlements_in_live_scope():
    root = Path(__file__).resolve().parents[1]
    source = (root / "app" / "main.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    assert "install_export_reporting(app, current_user=current_user" in source
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name in {
            "permitted", "selected_scope", "_cost_data_subscriptions", "_assert_cost_data_approval"
        }:
            assert "export_access" not in ast.get_source_segment(source, node)
            assert "export_reporting" not in ast.get_source_segment(source, node)


def test_grant_strict_boolean_unknown_dataset_and_expired_report_session(client, storage):
    http, state, _ = client
    dataset = onboard(http)
    path = f"/api/export-reporting/{dataset}/grant"
    assert http.post(path, json={"principal": PRINCIPAL, "allow": "false"}).status_code == 422
    assert http.post(path, json={"principal": "email@example.invalid", "allow": True}).status_code == 400
    assert http.post(f"/api/export-reporting/{OTHER}/grant",
                     json={"principal": PRINCIPAL, "allow": True}).status_code == 400
    assert http.post(path, json={"principal": PRINCIPAL, "allow": True}).status_code == 200
    state.active = False
    assert http.get(f"/api/export-reporting/{dataset}/billing").status_code == 403


def test_admin_revoked_during_import_cannot_publish(client, storage, monkeypatch):
    http, state, repo = client
    read = export_reporting.read_report

    async def revoke(source):
        result = await read(source)
        state.admin = False
        return result

    monkeypatch.setattr(export_reporting, "read_report", revoke)
    assert http.post("/api/export-reporting/import", json={"source": "dechow"}).status_code == 403
    assert_uninitialized(repo)


def test_currencies_are_not_summed_together(client, storage):
    http, _, _ = client
    storage["csv"] = storage["csv"].replace(b"USD,Compute,-0.03", b"EUR,Compute,-0.03")
    dataset = onboard(http)
    http.post(f"/api/export-reporting/{dataset}/grant", json={"principal": PRINCIPAL, "allow": True})
    report = http.get(f"/api/export-reporting/{dataset}/billing").json()
    assert report["totals"] == [
        {"currency": "EUR", "billed_cost": "-0.03", "effective_cost": "0.00"},
        {"currency": "USD", "billed_cost": "0.10", "effective_cost": "0.20"}]


def test_real_app_middleware_no_store_auth_and_unchanged_live_scope(client, storage, monkeypatch):
    from app import main

    _, state, _ = client
    session = SimpleNamespace(tenant=TENANT, oid=OID, email="verified@example.invalid", problem="")
    service = SimpleNamespace(
        identify=lambda *args: state.user,
        sso=SimpleNamespace(get=lambda sid: session, is_admin=lambda email, oid: True,
                            subscriptions=AsyncMock(return_value=[{"id": OTHER}])))
    monkeypatch.setattr(main, "_auth", service)
    http = TestClient(main.app)
    http.headers["Origin"] = "http://testserver"
    dataset = onboard(http)
    assert http.post(f"/api/export-reporting/{dataset}/grant",
                     json={"principal": PRINCIPAL, "allow": True}).status_code == 200
    response = http.get(f"/api/export-reporting/{dataset}/billing")
    assert response.status_code == 200 and response.headers["cache-control"] == "private, no-store"
    assert "script-src 'self'" in response.headers["content-security-policy"]
    allowed = asyncio.run(main.permitted(state.user))
    assert allowed == [OTHER]  # Export grant never appears in ARM entitlement.
    with pytest.raises(HTTPException) as error:
        main.selected_scope([SUB], allowed)
    assert error.value.status_code == 403
    monkeypatch.setenv("CLOUDLENS_READ_ONLY", "true")
    assert http.post(f"/api/export-reporting/{dataset}/grant",
                     json={"principal": PRINCIPAL, "allow": False}).status_code == 403
    assert http.get(f"/api/export-reporting/{dataset}/billing").status_code == 200
    state.user = None
    assert http.get(f"/api/export-reporting/{dataset}/billing").status_code == 401
