"""Exercise collection and dependency gating without importing the application."""
from __future__ import annotations

import importlib.util
import os
import shutil
import subprocess
import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("collector_under_test", ROOT / "conftest.py")
collector = importlib.util.module_from_spec(spec)
spec.loader.exec_module(collector)


@pytest.fixture
def project(tmp_path_factory):
    tmp_path = tmp_path_factory.mktemp("collector")
    shutil.copy(ROOT / "conftest.py", tmp_path / "conftest.py")
    (tmp_path / "pytest.ini").write_text("[pytest]\naddopts = --strict-markers\n")
    (tmp_path / ".pytest_cache").mkdir()
    (tmp_path / "tests").mkdir()
    return tmp_path


def run_pytest(project, *args, environment=None):
    env = {key: value for key, value in os.environ.items()
           if not key.startswith(("PG", "CLOUDLENS_", "AUTH_", "PYTEST_"))}
    env["PYTEST_DISABLE_PLUGIN_AUTOLOAD"] = "1"
    env.update(environment or {})
    return subprocess.run(
        [sys.executable, "-m", "pytest", "--confcutdir", str(project),
         "--basetemp", str(project / "pytest-work"), "-q", *args],
        cwd=project, env=env, stdin=subprocess.DEVNULL,
        capture_output=True, text=True, timeout=60,
    )


def write_test(project, name, source):
    (project / "tests" / name).write_text(source, encoding="utf-8")


def test_plain_function_and_class_tests_need_no_postgres(project):
    write_test(project, "test_unit.py", """
def test_function():
    assert True

class TestPlain:
    def test_method(self):
        assert True
""")
    result = run_pytest(project, "tests/test_unit.py")
    assert result.returncode == 0, result.stdout + result.stderr
    assert "2 passed" in result.stdout


def test_collection_cannot_inherit_production_connection_settings(project):
    write_test(project, "test_unit.py", """
import os
from pathlib import Path
assert not os.getenv("PGHOST")
assert not os.getenv("PGHOSTADDR")
assert not os.getenv("PGSERVICE")
assert not os.getenv("PGPASSFILE")
assert not os.getenv("PGSCHEMA")
assert not Path(os.environ["CLOUDLENS_ENV_FILE"]).exists()
def test_unit():
    assert True
""")
    result = run_pytest(project, environment={
        "PGHOST": "production.invalid", "PGHOSTADDR": "192.0.2.1",
        "PGSERVICE": "production", "PGPASSFILE": "must-not-read",
        "PGSCHEMA": "public", "CLOUDLENS_ENV_FILE": "must-not-read.env",
    })
    assert result.returncode == 0, result.stdout + result.stderr


def test_script_is_not_imported_and_can_explicitly_run_offline(project):
    write_test(project, "test_script.py", """
# cloudlens-test: requires-offline
import os
from pathlib import Path
assert not os.getenv("PGHOST")
assert not Path(os.environ["CLOUDLENS_ENV_FILE"]).exists()
assert Path(os.environ["AUTH_DATA_DIR"]).parent.name.startswith("cl_test_script_")
raise SystemExit(0)
""")
    result = run_pytest(project, "tests/test_script.py")
    assert result.returncode == 0, result.stdout + result.stderr
    assert "1 passed" in result.stdout


@pytest.mark.parametrize("name,source", [
    ("test_script.py", "raise SystemExit('must not execute')"),
    ("test_postgres.py", "def test_db(): assert False"),
    ("test_explicit.py", "import pytest\n@pytest.mark.postgres\ndef test_db(): assert False"),
    ("test_release_runtime.py", "raise RuntimeError('must not import')\ndef test_db(): assert False"),
])
def test_selected_database_tests_fail_loudly_without_server(project, name, source):
    write_test(project, name, source)
    result = run_pytest(project, f"tests/{name}")
    assert result.returncode == 4, result.stdout + result.stderr
    assert "Selected tests require PostgreSQL" in result.stderr
    assert "skipped" not in result.stdout


def test_deselection_and_collection_do_not_require_a_server(project):
    write_test(project, "test_script.py", "raise SystemExit('must not execute')")
    write_test(project, "test_unit.py", "def test_unit(): assert True")
    result = run_pytest(project, "-m", "not postgres")
    assert result.returncode == 0, result.stdout + result.stderr
    assert "1 passed, 1 deselected" in result.stdout
    result = run_pytest(project, "--collect-only")
    assert result.returncode == 0, result.stdout + result.stderr
    assert "2 tests collected" in result.stdout


def test_import_time_database_module_collects_without_being_imported(project):
    write_test(project, "test_release_runtime.py",
               "raise RuntimeError('must not import')\ndef test_db(): assert False")
    result = run_pytest(project, "--collect-only")
    assert result.returncode == 0, result.stdout + result.stderr
    assert "1 test collected" in result.stdout


def test_isolated_pytest_child_keeps_private_environment_and_avoids_recursion(monkeypatch):
    calls = []

    def run(command, **kwargs):
        calls.append((command, kwargs))
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(collector.subprocess, "run", run)
    monkeypatch.setattr(collector.tempfile, "mkdtemp", lambda **kwargs: str(ROOT / "not-created"))
    monkeypatch.setattr(collector.shutil, "rmtree", lambda *args, **kwargs: None)
    item = SimpleNamespace(
        path=ROOT / "tests" / "test_release_runtime.py",
        keywords={"postgres": True},
        config=SimpleNamespace(cache=SimpleNamespace(mkdir=lambda name: ROOT)),
        _environment=lambda workspace: {"PGSCHEMA": "private_fixture", "PYTEST_ADDOPTS": "-n 99"},
    )
    collector.ScriptItem.runtest(item)
    command, options = calls[0]
    assert command[:4] == [sys.executable, "-m", "pytest", "-q"]
    assert command[command.index("--confcutdir") + 1] == str(ROOT / "tests")
    assert options["env"]["PGSCHEMA"] == "private_fixture"
    assert options["env"]["PYTEST_ADDOPTS"] == ""


def test_conflicting_offline_marker_is_rejected(project):
    write_test(project, "test_bad.py", """
# cloudlens-test: requires-offline
# cloudlens-test: requires-warehouse
raise SystemExit(0)
""")
    result = run_pytest(project)
    assert result.returncode != 0
    assert "offline conflicts" in result.stdout + result.stderr


def test_long_schema_names_keep_the_unique_suffix():
    first = collector._schema_name(Path("cl_test_" + "long_" * 20 + "first"))
    second = collector._schema_name(Path("cl_test_" + "long_" * 20 + "second"))
    assert first != second
    assert len(first) <= 63
    assert first.startswith("t_cl_test_")


def test_seeder_uses_only_isolated_environment_and_a_deadline(monkeypatch):
    calls = []

    def run(*args, **kwargs):
        calls.append((args, kwargs))
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(collector.subprocess, "run", run)
    env = {"PGSCHEMA": "test_schema", "CLOUDLENS_ENV_FILE": "absent.env"}
    collector._seed_postgres(env)
    assert calls[0][1]["env"] == env
    assert calls[0][1]["timeout"] == collector.TIMEOUT
    assert calls[0][1]["cwd"] == ROOT


@pytest.mark.parametrize("body_fails", [False, True])
def test_postgres_fixture_pins_target_and_cleans_schema_pool_and_aliases(monkeypatch, tmp_path, body_fails):
    statements = []
    snapshots = []

    class Connection:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def execute(self, statement):
            statements.append(statement)

    class Store:
        closed = False

        def connect(self):
            return Connection()

        def close(self):
            self.closed = True

    class Warehouse:
        def __init__(self):
            self._store = Store()
            snapshots.append({name: os.environ[name] for name in (
                "PGHOST", "PGUSER", "PGDATABASE", "PGPORT", "PGSSLMODE",
                "PGSCHEMA", "PGHOSTADDR", "PGSERVICE", "CLOUDLENS_ENV_FILE",
            ) if name in os.environ})

    original = Warehouse()
    module = ModuleType("app.warehouse")
    module.Warehouse = Warehouse
    module.warehouse = original
    alias = ModuleType("app.fixture_test_alias")
    alias.warehouse = original
    monkeypatch.setitem(sys.modules, "app.warehouse", module)
    monkeypatch.setitem(sys.modules, "app.fixture_test_alias", alias)
    monkeypatch.setattr(collector, "_assert_test_database_isolated", lambda env: None)
    monkeypatch.setenv("CLOUDLENS_PG_TEST", "fixture.invalid")
    monkeypatch.setenv("CLOUDLENS_PG_USER", "fixture_role")
    monkeypatch.setenv("CLOUDLENS_PG_DB", "fixture_database")
    monkeypatch.setenv("PGHOST", "production.invalid")
    monkeypatch.setenv("PGHOSTADDR", "192.0.2.1")
    monkeypatch.setenv("PGSERVICE", "production")
    monkeypatch.setenv("PGPORT", "6432")
    monkeypatch.setenv("PGSSLMODE", "verify-full")
    request = SimpleNamespace(
        node=SimpleNamespace(path=ROOT / "tests" / "test_billing.py"),
        config=SimpleNamespace(cache=SimpleNamespace(mkdir=lambda name: tmp_path)),
    )
    try:
        with collector._isolated_postgres_warehouse(request) as warehouse:
            env = snapshots[-1]
            assert env["PGHOST"] == "fixture.invalid"
            assert env["PGUSER"] == "fixture_role"
            assert env["PGDATABASE"] == "fixture_database"
            assert env["PGPORT"] == "6432"
            assert env["PGSSLMODE"] == "verify-full"
            assert "PGHOSTADDR" not in env
            assert "PGSERVICE" not in env
            assert env["PGSCHEMA"].startswith("t_cl_test_billing_")
            assert not Path(env["CLOUDLENS_ENV_FILE"]).exists()
            assert module.warehouse is warehouse
            assert alias.warehouse is warehouse
            if body_fails:
                raise RuntimeError("fixture-body-failed")
    except RuntimeError as error:
        assert body_fails and str(error) == "fixture-body-failed"
    assert warehouse._store.closed
    assert statements == [f"DROP SCHEMA IF EXISTS {env['PGSCHEMA']} CASCADE"]
    assert request.config._cloudlens_schemas == []
    assert module.warehouse is original
    assert alias.warehouse is original
    assert os.environ["PGHOST"] == "production.invalid"
    assert not Path(env["CLOUDLENS_ENV_FILE"]).parent.exists()


@pytest.mark.parametrize("shared", [[], [("cloudlens_fn", "costs")], [("public", "other_data")]])
def test_database_guard_is_read_only_and_refuses_shared_relations(monkeypatch, shared):
    calls = []

    class Connection:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def execute(self, statement):
            assert statement.startswith("SELECT ")
            return SimpleNamespace(fetchall=lambda: shared)

    def connect(**kwargs):
        calls.append(kwargs)
        return Connection()

    store = SimpleNamespace(
        _password=lambda: "fixture-password", sslmode="require", close=lambda: None)
    monkeypatch.setitem(sys.modules, "psycopg", SimpleNamespace(connect=connect))
    monkeypatch.setitem(sys.modules, "app.store", SimpleNamespace(PostgresStore=lambda: store))
    env = {"PGHOST": "fixture.invalid", "PGPORT": "6432",
           "PGDATABASE": "fixture_database", "PGUSER": "fixture_role"}
    if shared:
        with pytest.raises(pytest.UsageError, match="empty, dedicated test database"):
            collector._assert_test_database_isolated(env)
    else:
        collector._assert_test_database_isolated(env)
    assert calls[0]["host"] == "fixture.invalid"
    assert calls[0]["dbname"] == "fixture_database"
    assert calls[0]["port"] == "6432"
    assert calls[0]["options"] == "-c default_transaction_read_only=on"


def test_rejected_database_is_never_imported_or_registered_for_drop(monkeypatch, tmp_path):
    monkeypatch.setenv("CLOUDLENS_PG_TEST", "fixture.invalid")
    monkeypatch.setenv("CLOUDLENS_PG_USER", "fixture_role")

    def refuse(env):
        raise pytest.UsageError("dedicated database required")

    def never_import(name):
        raise AssertionError("Warehouse imported before safety check")

    monkeypatch.setattr(collector, "_assert_test_database_isolated", refuse)
    monkeypatch.setattr(collector.importlib, "import_module", never_import)
    request = SimpleNamespace(
        node=SimpleNamespace(path=ROOT / "tests" / "test_billing.py"),
        config=SimpleNamespace(cache=SimpleNamespace(mkdir=lambda name: tmp_path)),
    )
    with pytest.raises(pytest.UsageError, match="dedicated database required"), \
            collector._isolated_postgres_warehouse(request):
        pytest.fail("Rejected database reached the test body")
    assert request.config._cloudlens_schemas == []
