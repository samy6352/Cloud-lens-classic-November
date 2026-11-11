from types import SimpleNamespace

import pytest

from app.auth import User
from app.export_access import DatasetAccess, entra_principal

pytestmark = pytest.mark.postgres

TENANT = "11111111-1111-1111-1111-111111111111"
OBJECT = "22222222-2222-2222-2222-222222222222"
DATASET = "33333333-3333-3333-3333-333333333333"
SUB = "44444444-4444-4444-4444-444444444444"
OTHER = f"entra:{TENANT}:55555555-5555-5555-5555-555555555555"
USER = User("admin", True, "entra", sid="session")
SESSION = SimpleNamespace(tenant=TENANT, oid=OBJECT, email="admin@example.invalid")
PRINCIPAL = entra_principal(SESSION)


@pytest.fixture
def repository(monkeypatch, postgres_warehouse):
    monkeypatch.setenv("EXPORT_REPORTING_ENABLED", "true")
    monkeypatch.setenv("CLOUDLENS_READ_ONLY", "false")
    return DatasetAccess(postgres_warehouse._store)


def assert_uninitialized(repository):
    with repository._store_instance().connect() as con:
        assert con.execute("SELECT to_regclass('export_reporting_datasets')").fetchone()[0] is None


def authentication(*, active=True, admin=True):
    return SimpleNamespace(sso=SimpleNamespace(
        get=lambda sid: SESSION if active and sid == "session" else None,
        is_admin=lambda email, oid: admin))


def record(repo, **overrides):
    args = {"dataset": DATASET, "subscription": SUB,
            "imported_at": "2026-11-10T12:00:00+00:00",
            "row_count": 10, "manifest_sha256": "a" * 64}
    repo.record_import(USER, authentication(), **(args | overrides))


def test_disabled_creates_no_auth_artifacts(repository, monkeypatch):
    monkeypatch.delenv("EXPORT_REPORTING_ENABLED")
    with pytest.raises(PermissionError, match="disabled"):
        record(repository)
    assert_uninitialized(repository)
    with pytest.raises(PermissionError):
        repository.catalog(PRINCIPAL)


def test_explicit_grants_reload_revocation_and_no_live_capability(repository):
    assert repository.catalog(PRINCIPAL) == []
    record(repository)
    assert repository.catalog(PRINCIPAL) == []  # Even admin has no implicit read grant.
    repository.set_grant(USER, authentication(), dataset=DATASET, principal=PRINCIPAL, allow=True)
    reader = DatasetAccess(repository._store_instance())
    item = reader.require_dataset(PRINCIPAL, DATASET)
    assert item == {"dataset_id": DATASET, "subscription_id": SUB,
                    "last_import": "2026-11-10T12:00:00+00:00", "rows": 10,
                    "mode": "Export-backed", "live_actions": False, "reporting_available": False}
    assert reader.catalog(OTHER) == []
    with pytest.raises(PermissionError):
        reader.require_dataset(PRINCIPAL, SUB)  # Subscription IDs are not dataset IDs.
    repository.set_grant(USER, authentication(), dataset=DATASET, principal=PRINCIPAL, allow=False)
    with pytest.raises(PermissionError):
        reader.require_dataset(PRINCIPAL, DATASET)
    with repository._connection() as con:
        assert con.execute("SELECT action, actor FROM export_reporting_audit ORDER BY sequence").fetchall() == [
            ("record-import", PRINCIPAL), ("grant", PRINCIPAL), ("revoke", PRINCIPAL)]


@pytest.mark.parametrize("user,service", [
    (User("reader", source="entra", sid="session"), authentication()),
    (User("local-admin", True), authentication()),
    (USER, authentication(active=False)),
    (USER, authentication(admin=False)),
])
def test_mutations_require_current_app_admin(repository, user, service):
    with pytest.raises(PermissionError):
        repository.record_import(user, service, dataset=DATASET, subscription=SUB,
                                 imported_at="2026-11-10T12:00:00Z", row_count=1,
                                 manifest_sha256="a" * 64)
    with pytest.raises(PermissionError):
        repository.set_grant(user, service, dataset=DATASET, principal=PRINCIPAL, allow=True)
    assert_uninitialized(repository)


@pytest.mark.parametrize("override", [
    {"row_count": 0}, {"row_count": True}, {"manifest_sha256": "invalid"},
    {"imported_at": "2026-11-10T12:00:00"}, {"subscription": "not-a-subscription"},
])
def test_invalid_import_evidence_not_recorded(repository, override):
    with pytest.raises(ValueError):
        record(repository, **override)
    assert_uninitialized(repository)


def test_immutable_imports_and_unknown_grants(repository):
    import psycopg

    record(repository)
    with pytest.raises(psycopg.errors.UniqueViolation):
        record(repository, subscription=TENANT)
    with pytest.raises(ValueError, match="Unknown"):
        repository.set_grant(USER, authentication(), dataset=SUB, principal=PRINCIPAL, allow=True)
    with pytest.raises(ValueError):
        repository.set_grant(USER, authentication(), dataset=DATASET,
                             principal="admin@example.invalid", allow=True)
    with pytest.raises(ValueError):
        repository.set_grant(USER, authentication(), dataset=DATASET,
                             principal=PRINCIPAL, allow="false")


def test_unsupported_schema_never_falls_back_to_allow(repository):
    repository.catalog(PRINCIPAL)
    with repository._store_instance().connect() as con:
        con.execute("UPDATE export_reporting_schema_version SET version=999")
    with pytest.raises(RuntimeError, match="schema version"):
        repository.require_dataset(PRINCIPAL, DATASET)


def test_read_only_blocks_mutations_but_revocation_is_not_cached(repository, monkeypatch):
    record(repository)
    repository.set_grant(USER, authentication(), dataset=DATASET, principal=PRINCIPAL, allow=True)
    monkeypatch.setenv("CLOUDLENS_READ_ONLY", "true")
    assert repository.require_dataset(PRINCIPAL, DATASET)["live_actions"] is False
    with pytest.raises(PermissionError, match="READ_ONLY"):
        repository.set_grant(USER, authentication(), dataset=DATASET, principal=PRINCIPAL, allow=False)
    with pytest.raises(PermissionError, match="READ_ONLY"):
        record(repository, dataset=TENANT)


def test_tenant_is_part_of_identity_and_session_id_is_not(repository):
    record(repository)
    repository.set_grant(USER, authentication(), dataset=DATASET, principal=PRINCIPAL, allow=True)
    other_tenant = f"entra:{SUB}:{OBJECT}"
    assert repository.catalog(other_tenant) == []
    with pytest.raises(ValueError):
        repository.catalog("session")


def test_failed_report_publication_rolls_back_dataset_and_audit(repository):
    with pytest.raises(TypeError):
        record(repository, report={"label": object()})
    with repository._connection() as con:
        for table in ("datasets", "reports", "audit", "grants"):
            assert con.execute(f"SELECT count(*) FROM export_reporting_{table}").fetchone() == (0,)
    record(repository, report={"label": "recovered"})
    assert len(repository.admin_catalog(USER, authentication())) == 1


def test_concurrent_first_use_migrates_once(repository):
    from concurrent.futures import ThreadPoolExecutor

    with ThreadPoolExecutor(max_workers=4) as executor:
        assert list(executor.map(lambda _: repository.catalog(PRINCIPAL), range(8))) == [[]] * 8
    with repository._connection() as con:
        assert con.execute("SELECT version FROM export_reporting_schema_version").fetchall() == [(1,)]


def test_independent_worker_pool_observes_committed_revocation(repository):
    from app.store import PostgresStore

    record(repository, report={"label": "isolated"})
    repository.set_grant(USER, authentication(), dataset=DATASET, principal=PRINCIPAL, allow=True)
    worker_store = PostgresStore()
    try:
        worker = DatasetAccess(worker_store)
        assert worker.report(PRINCIPAL, DATASET)["label"] == "isolated"
        repository.set_grant(USER, authentication(), dataset=DATASET, principal=PRINCIPAL, allow=False)
        with pytest.raises(PermissionError):
            worker.report(PRINCIPAL, DATASET)
        assert worker.catalog(PRINCIPAL) == []
    finally:
        worker_store.close()


def test_read_only_missing_schema_refuses_migration(repository, monkeypatch):
    monkeypatch.setenv("CLOUDLENS_READ_ONLY", "true")
    with pytest.raises(PermissionError, match="READ_ONLY"):
        repository.catalog(PRINCIPAL)
    assert_uninitialized(repository)


def test_distinct_application_schemas_do_not_share_reports_or_grants(repository, monkeypatch):
    from uuid import uuid4

    from app.store import PostgresStore

    record(repository, report={"label": "first"})
    repository.set_grant(USER, authentication(), dataset=DATASET, principal=PRINCIPAL, allow=True)
    schema = "t_export_isolation_" + uuid4().hex
    monkeypatch.setenv("PGSCHEMA", schema)
    isolated_store = PostgresStore()
    try:
        isolated = DatasetAccess(isolated_store)
        assert isolated.catalog(PRINCIPAL) == []
        with pytest.raises(PermissionError):
            isolated.report(PRINCIPAL, DATASET)
        record(isolated, report={"label": "second"})
        isolated.set_grant(USER, authentication(), dataset=DATASET, principal=PRINCIPAL, allow=True)
        assert isolated.report(PRINCIPAL, DATASET)["label"] == "second"
        assert repository.report(PRINCIPAL, DATASET)["label"] == "first"
    finally:
        with isolated_store.connect() as con:
            con.execute(f"DROP SCHEMA {schema} CASCADE")
        isolated_store.close()
