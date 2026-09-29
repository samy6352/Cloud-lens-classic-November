"""Isolate legacy scripts and require PostgreSQL only for selected database tests."""
from __future__ import annotations

import ast
import hashlib
import importlib
import io
import os
import re
import shutil
import subprocess
import sys
import tempfile
import tokenize
from contextlib import contextmanager
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent
TESTS = ROOT / "tests"

TIMEOUT = 300

REQUIRES = re.compile(r"^#\s*cloudlens-test:\s*requires-(\w+)\s*$", re.M)

# Environment that must not leak in from the developer's shell. A stray AUTH_PASSWORD
# or PGSCHEMA is the difference between a test that checks the code and a test that
# checks the machine -- the latter reads the developer's own warehouse and passes for
# them alone. `COST_` is still scrubbed for the settings that remain (COST_CLIENT_TYPE,
# COST_QUERY_TIMEOUT_MS); the storage variables it used to cover are gone.
SCRUB_PREFIXES = ("AUTH_", "COST_", "PG", "ARCHIVE_", "PROJECT_", "MODEL_", "CLOUDLENS_DEMO_")

# These older pytest modules predate the postgres marker. Their fixtures own schemas.
POSTGRES_MODULES = {"test_postgres.py", "test_query_guard.py", "test_rollup.py"}
# This function-style module initializes the real warehouse at import time.
ISOLATED_PYTEST_MODULES = {"test_release_runtime.py"}


def pytest_configure(config: pytest.Config) -> None:
    config.addinivalue_line("markers", "script: a standalone test script run as a subprocess")
    config.addinivalue_line("markers", "warehouse: needs the seeded fixture warehouse")
    config.addinivalue_line("markers", "network: needs outbound internet access")
    config.addinivalue_line("markers", "azure: needs Azure credentials and a real subscription")
    config.addinivalue_line("markers", "postgres: needs an explicitly configured test PostgreSQL server")
    config.addinivalue_line("markers", "offline: script needs no database, network or Azure credentials")
    # Protect ordinary pytest modules too, before collection imports the application.
    isolated = pytest.MonkeyPatch()
    workspace = Path(tempfile.mkdtemp(
        prefix="session_", dir=config.cache.mkdir("workspaces")))
    isolated.setenv("CLOUDLENS_ENV_FILE", str(workspace / "absent.env"))
    isolated.setenv("AUTH_DATA_DIR", str(workspace / "auth"))
    for name in tuple(os.environ):
        if name.startswith("PG") and name not in {"PGPASSWORD", "PGPORT", "PGSSLMODE"}:
            isolated.delenv(name)
    config.add_cleanup(lambda: shutil.rmtree(workspace, ignore_errors=True))
    config.add_cleanup(isolated.undo)


def _is_script_style(path: Path) -> bool:
    """Legacy scripts, including unittest entry points, retain process isolation."""
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"))
    except (OSError, SyntaxError):
        return False
    return not any(
        (isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
         and node.name.startswith("test"))
        or (isinstance(node, ast.ClassDef) and node.name.startswith("Test")
            and not node.bases)
        for node in tree.body
    )


@pytest.hookimpl(wrapper=True)
def pytest_collect_file(file_path: Path, parent: pytest.Collector):
    """Replace all collectors for scripts; a second Python collector would import them."""
    collectors = yield
    if (file_path.suffix == ".py" and file_path.name.startswith("test_")
            and file_path.parent == TESTS
            and (file_path.name in ISOLATED_PYTEST_MODULES or _is_script_style(file_path))):
        return [ScriptFile.from_parent(parent, path=file_path)]
    return collectors


def pytest_itemcollected(item: pytest.Item) -> None:
    """Apply dependency markers before pytest evaluates -m selections."""
    source = item.path.read_text(encoding="utf-8")
    for token in tokenize.generate_tokens(io.StringIO(source).readline):
        if token.type == tokenize.COMMENT and (match := REQUIRES.fullmatch(token.string)):
            item.add_marker(match.group(1))
    if (item.path.name in POSTGRES_MODULES or "warehouse" in item.keywords
            or {"postgres_warehouse", "module_postgres_warehouse"}
            .intersection(getattr(item, "fixturenames", ()))
            or (isinstance(item, ScriptItem) and "offline" not in item.keywords)):
        item.add_marker("postgres")
    if "offline" in item.keywords and any(
        marker in item.keywords for marker in ("postgres", "warehouse", "network", "azure")
    ):
        raise pytest.UsageError(f"{item.nodeid}: offline conflicts with external dependencies")


class ScriptFile(pytest.File):
    def collect(self):
        yield ScriptItem.from_parent(self, name=self.path.stem)


class ScriptItem(pytest.Item):
    """One test script, run in its own interpreter."""

    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)
        self.add_marker("script")

    def runtest(self) -> None:
        if "azure" in self.keywords and not os.getenv("CLOUDLENS_AZURE_TEST"):
            pytest.skip("set CLOUDLENS_AZURE_TEST=1 to run tests that call Azure")

        workspace = Path(tempfile.mkdtemp(
            prefix=f"cl_{self.path.stem}_", dir=self.config.cache.mkdir("workspaces")))
        try:
            env = self._environment(workspace)
            command = [sys.executable, str(self.path)]
            if self.path.name in ISOLATED_PYTEST_MODULES:
                # Parent owns isolation and DB gating; do not recursively load this collector.
                env["PYTEST_ADDOPTS"] = ""
                command = [
                    sys.executable, "-m", "pytest", "-q", "-o", "addopts=",
                    "--confcutdir", str(TESTS), str(self.path),
                ]
            self._result = subprocess.run(
                command,
                cwd=ROOT,
                env=env,
                # DEVNULL rather than inherit: under pytest's capture there is no real
                # stdin handle to pass on, and Windows rejects the inherit outright
                # ("[WinError 6] The handle is invalid") before the test even starts.
                stdin=subprocess.DEVNULL,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=TIMEOUT,
            )
            if self._result.returncode != 0:
                raise ScriptFailed(self._result.returncode)
        except subprocess.TimeoutExpired:
            raise ScriptFailed(None) from None
        finally:
            shutil.rmtree(workspace, ignore_errors=True)

    def _environment(self, workspace: Path) -> dict[str, str]:
        env = {k: v for k, v in os.environ.items()
               if not k.startswith(SCRUB_PREFIXES)}
        env["PYTHONIOENCODING"] = "utf-8"
        # Unbuffered, so a test that is killed on timeout still shows what it printed.
        env["PYTHONUNBUFFERED"] = "1"
        env["PYTHONPATH"] = str(ROOT) + (os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else "")
        env["AUTH_DATA_DIR"] = str(workspace / "auth")
        # No .env. Scrubbing os.environ is not enough on its own: python-dotenv fills in
        # anything that is *not* already set, so a cleaned environment is exactly the
        # condition under which the developer's own .env would be read.
        env["CLOUDLENS_ENV_FILE"] = str(workspace / "absent.env")
        if "postgres" in self.keywords:
            env.update(_storage_environment(self.config, workspace))
            if "warehouse" in self.keywords:
                _seed_postgres(env)
        return env

    def repr_failure(self, excinfo, style=None) -> str:
        if not isinstance(excinfo.value, ScriptFailed):
            return super().repr_failure(excinfo, style)
        result = getattr(self, "_result", None)
        if result is None:
            return f"{self.path.name} did not finish within {TIMEOUT}s."
        parts = [f"{self.path.name} exited {result.returncode}."]
        for label, text in (("stdout", result.stdout), ("stderr", result.stderr)):
            body = (text or "").strip()
            if body:
                tail = body.splitlines()[-40:]
                parts.append(f"\n--- {label} (last {len(tail)} lines) ---")
                parts.extend(tail)
        return "\n".join(parts)

    def reportinfo(self):
        return self.path, 0, self.path.name


class ScriptFailed(Exception):
    """Raised so pytest reports a failure rather than an error."""


NO_SERVER = (
    "Selected tests require PostgreSQL; no database fallback or implicit skip is allowed.\n"
    "Set CLOUDLENS_PG_TEST to an isolated test server and CLOUDLENS_PG_USER to the role to connect\n"
    "as. Set PGPASSWORD to a password or an Entra access token, or leave it unset to use the\n"
    'ambient Azure identity. For offline tests use -m "not postgres and not network and not azure".\n'
    "Never point the suite at production. See tests/README.md."
)


def _pg_configured() -> bool:
    return bool(os.getenv("CLOUDLENS_PG_TEST", "").strip()
                and os.getenv("CLOUDLENS_PG_USER", "").strip())


def pytest_runtestloop(session: pytest.Session):
    """Fail once for missing DB configuration, after deselection; collection stays offline."""
    if session.config.option.collectonly:
        return None
    needs_postgres = any(
        "postgres" in item.keywords
        and not ("azure" in item.keywords and not os.getenv("CLOUDLENS_AZURE_TEST"))
        for item in session.items
    )
    if needs_postgres and not _pg_configured():
        raise pytest.UsageError(NO_SERVER)
    return None  # let pytest run the tests as usual


def _storage_environment(config: pytest.Config, workspace: Path) -> dict[str, str]:
    schema = _schema_name(workspace)
    env = {
        "PGHOST": os.environ["CLOUDLENS_PG_TEST"].strip(),
        "PGPORT": os.getenv("PGPORT", "5432"),
        "PGDATABASE": os.getenv("CLOUDLENS_PG_DB", "postgres"),
        "PGUSER": os.environ["CLOUDLENS_PG_USER"].strip(),
        "PGSCHEMA": schema,
    }
    if os.getenv("PGPASSWORD"):
        env["PGPASSWORD"] = os.environ["PGPASSWORD"]
    if os.getenv("PGSSLMODE"):
        env["PGSSLMODE"] = os.environ["PGSSLMODE"]

    _record_schema(config, schema)
    return env


def _schema_name(workspace: Path) -> str:
    # The workspace directory is already unique per test and carries the test's name,
    # so the schema inherits both. Lowercased and filtered because PostgreSQL folds
    # unquoted identifiers and the directory can hold characters an identifier cannot.
    raw = workspace.name.lower().replace("-", "_")
    cleaned = "".join(c for c in raw if c.isalnum() or c == "_")
    suffix = hashlib.sha256(workspace.name.encode()).hexdigest()[:12]
    return f"t_{cleaned[:44]}_{suffix}"


def _record_schema(config: pytest.Config, schema: str) -> None:
    created = getattr(config, "_cloudlens_schemas", None)
    if created is None:
        created = []
        config._cloudlens_schemas = created  # type: ignore[attr-defined]
    created.append(schema)


def _assert_test_database_isolated(env: dict[str, str]) -> None:
    """Refuse shared tables before startup migrations can move them into a test schema."""
    import psycopg

    from app.store import PostgresStore

    store = PostgresStore()
    try:
        with psycopg.connect(
            host=env["PGHOST"], port=env["PGPORT"], dbname=env["PGDATABASE"],
            user=env["PGUSER"], password=store._password(), sslmode=store.sslmode,
            connect_timeout=20, autocommit=True,
            options="-c default_transaction_read_only=on",
        ) as connection:
            shared = connection.execute(
                "SELECT n.nspname, c.relname FROM pg_class c "
                "JOIN pg_namespace n ON n.oid = c.relnamespace "
                "WHERE n.nspname IN ('public', 'cloudlens_fn') "
                "AND c.relkind IN ('r', 'p', 'm', 'v', 'f') LIMIT 10"
            ).fetchall()
    finally:
        store.close()
    if shared:
        raise pytest.UsageError(
            "Refusing a test database with shared tables/views in public or cloudlens_fn. "
            "Set CLOUDLENS_PG_DB to an empty, dedicated test database; startup migrations "
            "can otherwise move existing data into a disposable schema."
        )


@contextmanager
def _isolated_postgres_warehouse(request):
    """Own the test connection settings, singleton aliases, schema and pool lifetime."""
    if not _pg_configured():
        raise pytest.UsageError(NO_SERVER)
    workspace = Path(tempfile.mkdtemp(
        prefix=f"cl_{request.node.path.stem}_", dir=request.config.cache.mkdir("workspaces")))
    try:
        with pytest.MonkeyPatch.context() as isolated:
            env = _storage_environment(request.config, workspace)
            for name in tuple(os.environ):
                if name.startswith("PG"):
                    isolated.delenv(name)
            for name, value in env.items():
                isolated.setenv(name, value)
            isolated.setenv("CLOUDLENS_ENV_FILE", str(workspace / "absent.env"))
            isolated.setenv("AUTH_DATA_DIR", str(workspace / "auth"))
            try:
                _assert_test_database_isolated(env)
            except BaseException:
                request.config._cloudlens_schemas.remove(env["PGSCHEMA"])
                raise
            already_loaded = "app.warehouse" in sys.modules
            module = importlib.import_module("app.warehouse")
            warehouse = module.Warehouse() if already_loaded else module.warehouse
            try:
                # Some modules captured the singleton with `from .warehouse import warehouse`.
                for name, imported in tuple(sys.modules.items()):
                    if name.startswith("app.") and isinstance(
                        getattr(imported, "warehouse", None), module.Warehouse
                    ):
                        isolated.setattr(imported, "warehouse", warehouse)
                yield warehouse
            finally:
                try:
                    with warehouse._store.connect() as connection:
                        connection.execute(f"DROP SCHEMA IF EXISTS {env['PGSCHEMA']} CASCADE")
                    request.config._cloudlens_schemas.remove(env["PGSCHEMA"])
                finally:
                    warehouse._store.close()
    finally:
        shutil.rmtree(workspace, ignore_errors=True)


@pytest.fixture
def postgres_warehouse(request):
    with _isolated_postgres_warehouse(request) as warehouse:
        yield warehouse


@pytest.fixture(scope="module")
def module_postgres_warehouse(request):
    with _isolated_postgres_warehouse(request) as warehouse:
        yield warehouse


def _seed_postgres(env: dict[str, str]) -> None:
    """Fill this test's schema with the fixed data the warehouse tests assert on."""
    seeder = "from tests.fixture_warehouse import seed_current; seed_current()"
    outcome = subprocess.run([sys.executable, "-c", seeder], capture_output=True,
                             stdin=subprocess.DEVNULL, text=True, env=env, cwd=ROOT,
                             encoding="utf-8", errors="replace", timeout=TIMEOUT)
    if outcome.returncode != 0:
        raise RuntimeError(
            f"could not seed schema {env['PGSCHEMA']}:\n{outcome.stderr[-2000:]}")


def pytest_sessionfinish(session, exitstatus) -> None:
    """Drop the schemas the run created.

    Best effort by design. A schema left behind costs nothing but a name, and
    failing the run at the very end over cleanup would obscure the actual result.
    """
    schemas = getattr(session.config, "_cloudlens_schemas", None)
    if not schemas:
        return
    host = os.getenv("CLOUDLENS_PG_TEST", "").strip()
    if not host:
        return
    try:
        import psycopg

        # The same credential the tests themselves connected with. `PGPASSWORD` unset
        # means "use the ambient Azure identity" -- which tests/README.md documents and
        # `PostgresStore._password` implements -- but this cleanup only ever read the
        # environment variable, so against an Entra-authenticated server it failed every
        # time with `fe_sendauth: no password supplied`. Nothing was ever dropped, and the
        # development server had accumulated well over a hundred abandoned schemas, four
        # tables apiece, that nothing else would ever remove.
        secret = os.environ.get("PGPASSWORD")
        if not secret:
            # Asked for directly rather than through `PostgresStore`, which validates
            # PGHOST at construction: the tests set that per subprocess, so it is not in
            # this process's environment and building one here fails on configuration
            # that this connection is about to supply by hand anyway.
            from azure.identity import DefaultAzureCredential

            from app.store import _PG_SCOPE

            secret = DefaultAzureCredential().get_token(_PG_SCOPE).token

        with psycopg.connect(
            host=host,
            port=os.getenv("PGPORT", "5432"),
            dbname=os.getenv("CLOUDLENS_PG_DB", "postgres"),
            user=os.environ["CLOUDLENS_PG_USER"],
            password=secret,
            sslmode=os.getenv("PGSSLMODE", "require"),
            autocommit=True,
            connect_timeout=30,
        ) as conn:
            for schema in schemas:
                with conn.cursor() as cur:
                    cur.execute(f"DROP SCHEMA IF EXISTS {schema} CASCADE")
    except Exception as exc:  # noqa: BLE001 - cleanup must not fail the run
        print(f"\ncould not drop {len(schemas)} test schema(s): {str(exc)[:200]}")
