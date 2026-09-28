"""Where the warehouse stores its rows.

The warehouse used to be a DuckDB file and nothing else. That is a good engine
for this workload and a bad thing to hand to a customer: it is an embedded
library on the app's own disk, which means the data lives inside the compute,
there is nothing for a customer's DBA to back up, monitor or restore, and it is
not a component Microsoft ships to them.

So the rows live in Azure Database for PostgreSQL Flexible Server, and nowhere
else. DuckDB was kept for a while as an opt-in local engine, which turned out to
cost more than it saved: it was the engine the whole test suite actually
exercised, so PostgreSQL-only faults -- COPY TO for the archive, the interval
syntax, TRY_CAST(... AS JSON) -- reached the deployed app without a single test
failing, and were all found by reading logs. One engine means the suite tests
what runs.

Everything above this module is written against one small interface, which is
the DuckDB connection API it was already written against:

    con.execute(sql, params) -> cursor with .fetchall/.fetchone/.fetchmany
    con.close(), and use as a context manager

`PostgresConnection` presents exactly that over psycopg, translating the SQL on
the way through (see sqldialect). That interface is kept rather than rewritten
because it is a few hundred call sites across two dozen query modules, and none
of them need to know how the rows are stored.

Authentication has no password in it. PostgreSQL is deployed with password
authentication disabled outright, and the app presents an Entra token from its
managed identity instead -- so a customer deployment has no database credential
to store in app settings, rotate, or leak.
"""
from __future__ import annotations

import logging
import os
import re
import threading
import time
from typing import Any

from .sqldialect import SHIM_SCHEMA, SHIMS, to_postgres

log = logging.getLogger("cloudlens.store")

# The audience for a PostgreSQL access token. Constant across every Azure cloud.
_PG_SCOPE = "https://ossrdbms-aad.database.windows.net/.default"

# Tokens last an hour. Renewing a few minutes early avoids a connection opening
# with a token that expires while it is still being established.
_TOKEN_MARGIN = 300.0

# A schema name goes into SET search_path, which takes no parameters, so it is
# checked against this rather than quoted.
_SAFE_IDENTIFIER = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")

# A PostgreSQL memory setting: a number with an optional unit. Checked at startup so a
# typo is a clear error on the first connection rather than an obscure one on every
# connection, and because the value reaches the server through `set_config`, which does
# take parameters -- this is a usability guard, not the injection defence.
_SAFE_MEMORY = re.compile(r"^\d+(kB|MB|GB|TB)?$", re.IGNORECASE)

# A PostgreSQL duration setting, guarded the same way and for the same reason. A bare
# number means milliseconds, which is why `110` and `110s` are wildly different values
# and a typo is worth catching at startup rather than at the first slow query.
_SAFE_DURATION = re.compile(r"^\d+(us|ms|s|min|h|d)?$", re.IGNORECASE)


class PoolBusy(TimeoutError):
    """Every pooled connection is checked out and none came back in time.

    A distinct type because the caller's correct response is the opposite of the one
    for any other failure. The snapshot cache answers a failed read by falling back to
    the origin route -- the right move when the cache is the broken part, and precisely
    the wrong one here, since the origin needs a connection from the same exhausted
    pool. It cannot succeed, and trying costs another borrower its place in the queue.

    A subclass of `TimeoutError` rather than a new hierarchy: this is still a timeout,
    and existing handlers that treat it as one stay correct.
    """



class _Cursor:
    """A psycopg cursor wearing the result API the call sites were written against.

    The difference that matters is `execute`: the old engine returned something
    you could immediately fetch from, psycopg returns the cursor itself only
    sometimes. Returning `self` unconditionally is what lets existing call sites
    keep writing `con.execute(...).fetchall()`.
    """

    def __init__(self, cursor: Any) -> None:
        self._cursor = cursor

    @property
    def description(self):
        return self._cursor.description

    def execute(self, sql: str, params: Any = None) -> _Cursor:
        # `bind` tells the translator whether psycopg will be scanning this
        # statement for placeholder markers. It only does so when parameters are
        # passed, and escaping `%` when it will not corrupts the statement.
        self._cursor.execute(to_postgres(sql, bind=params is not None), params or None)
        return self

    def fetchall(self):
        return self._cursor.fetchall() if self._cursor.description else []

    def fetchone(self):
        return self._cursor.fetchone() if self._cursor.description else None

    def fetchmany(self, size: int = 1):
        return self._cursor.fetchmany(size) if self._cursor.description else []

    def close(self) -> None:
        self._cursor.close()


class PostgresConnection:
    """One PostgreSQL session, presented as the connection the call sites expect.

    Every `execute` runs on a fresh server-side cursor, because the call sites
    keep the result of one execute while issuing the next -- `summary()` does
    exactly that -- and a single reused cursor would discard the first result.

    Autocommit is on. The call sites were written against an implicit
    commit, and a long-lived idle transaction on PostgreSQL holds back vacuum and
    pins the oldest snapshot, which on a burstable server is how a warehouse
    quietly stops reclaiming space. Where a real transaction is needed --
    replacing a month of data -- `transaction()` is explicit about it.
    """

    def __init__(self, conn: Any, owner: PostgresStore) -> None:
        self._conn = conn
        self._owner = owner
        self._closed = False
        self.bill_stored = False
        self.bill_serving: str | None = None
        self.presence_stored = False

    def execute(self, sql: str, params: Any = None) -> _Cursor:
        return _Cursor(self._conn.cursor()).execute(sql, params)

    def executemany(self, sql: str, rows: Any) -> None:
        with self._conn.cursor() as cur:
            cur.executemany(to_postgres(sql), rows)

    def cursor(self) -> PostgresConnection:
        """An independent second connection, matching the old `cursor()`.

        That was a full connection with its own transaction and its own temp
        tables, sharing only the underlying database. The equivalent here is another
        pooled session -- not a second handle on this one, which would return a
        still-in-use connection to the pool the moment the caller closed what it thought
        was a separate object.
        """
        return self._owner.connect()

    @property
    def raw(self) -> Any:
        """The psycopg connection, for the COPY path in the bulk loader."""
        return self._conn

    def commit(self) -> None:
        self._conn.commit()

    def rollback(self) -> None:
        self._conn.rollback()

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._owner._release(self._conn)

    def __enter__(self) -> PostgresConnection:
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()


class PostgresStore:
    """Opens connections to PostgreSQL, with Entra tokens instead of a password.

    Connections are pooled by hand rather than with psycopg_pool, for one
    reason: the password is a token that expires. A pooled connection outlives
    the token it was opened with, which is fine -- PostgreSQL only checks
    credentials at connection time -- but a connection opened *after* expiry with
    a stale cached token is refused. So the token is cached with a margin and the
    pool is small; the cost of getting this wrong is an outage an hour after
    deployment, which is exactly the kind that reaches a customer.
    """

    # Declared on the class so an instance built without `__init__` still has them.
    # The tuning tests construct a bare store through `__new__` to exercise one method
    # in isolation, and a session setting that only exists after a full construction
    # would turn those into AttributeError rather than the behaviour under test.
    work_mem: str = ""
    statement_timeout: str = ""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._idle: list[Any] = []
        self._token: tuple[str, float] | None = None
        self._credential: Any = None
        self._shims_installed = False
        self.host = os.getenv("PGHOST", "").strip()
        self.database = os.getenv("PGDATABASE", "cloudlens")
        self.user = os.getenv("PGUSER", "")
        self.sslmode = os.getenv("PGSSLMODE", "require")
        self.max_idle = int(os.getenv("PG_POOL_SIZE", "8"))
        if self.max_idle < 1:
            raise ValueError("PG_POOL_SIZE must be positive")
        self._slots = threading.BoundedSemaphore(self.max_idle)
        self._pool_timeout = float(os.getenv("PG_POOL_TIMEOUT_SECONDS", "10"))
        # An optional schema to put the warehouse in, rather than `public`.
        #
        # Two separate uses. A deployment can share one server between environments
        # without one warehouse overwriting another; and the test suite gives each
        # test a schema of its own, which is what lets the whole suite run against a
        # single server in parallel without tests seeing each other's rows.
        #
        # Validated rather than trusted: it is a bare identifier interpolated into
        # SET search_path, which takes no parameters.
        self.schema = os.getenv("PGSCHEMA", "").strip()
        if self.schema and not _SAFE_IDENTIFIER.match(self.schema):
            raise RuntimeError(
                f"PGSCHEMA={self.schema!r} is not a plain identifier. Use letters, "
                "digits and underscores."
            )
        # How much memory one sort or hash node may use before it spills to disk.
        #
        # Opt-in, and unset means the server's own value, because the right number is a
        # property of the server rather than of this application: `work_mem` is charged
        # *per node*, so a query with three sorts can take three times what is configured
        # here, and up to `PG_POOL_SIZE` connections can be doing that at once. A default
        # chosen here without knowing the server's memory would be a guess applied to every
        # deployment.
        #
        # It is worth configuring where the dashboard reads a large derived table. Measured
        # on the 2.78M-row `resource_presence`: the top-resources query sorts 507,846 groups
        # and needs about 90MB to do it, so at the common 64MB default it spills 73MB to
        # temp files -- about a second of the eight that query took, paid on every request.
        self.work_mem = os.getenv("PG_WORK_MEM", "").strip()
        if self.work_mem and not _SAFE_MEMORY.match(self.work_mem):
            raise RuntimeError(
                f"PG_WORK_MEM={self.work_mem!r} is not a PostgreSQL memory value. "
                "Use a number with an optional unit, for example 128MB."
            )
        # How long one *read* may run on the server before PostgreSQL cancels it.
        #
        # This exists because an application-level timeout cannot stop a database query.
        # `asyncio.wait_for` around a snapshot build cancels the await; the worker thread,
        # the pooled connection and the query itself carry on. Measured here: two reads
        # still running eight minutes after the requests that issued them had gone, each
        # still holding one of the eight pool slots. Slots lost that way are never
        # returned, so the pool drains until every request fails -- the 500 is a leak
        # reaching its conclusion, not a slow query.
        #
        # Server-side, so the bound holds whatever the client does. Set it below the
        # caller's own timeout: PostgreSQL should end the query while the request is
        # still waiting, so the connection comes back rather than being abandoned.
        #
        # Applied to readers only, by `Warehouse.connect(read_only=True)`. The rebuilds
        # legitimately run for several minutes -- the presence table takes about 430s --
        # and a limit meant for a dashboard tab would abort them every time. Pooled
        # sessions serve both, so `_return_connection` resets it on the way back.
        self.statement_timeout = os.getenv("PG_STATEMENT_TIMEOUT", "").strip()
        if self.statement_timeout and not _SAFE_DURATION.match(self.statement_timeout):
            raise RuntimeError(
                f"PG_STATEMENT_TIMEOUT={self.statement_timeout!r} is not a PostgreSQL "
                "duration. Use a number with an optional unit, for example 110s. A bare "
                "number is milliseconds."
            )
        if not self.host:
            raise RuntimeError(
                "PGHOST is not set. The deployment sets it from "
                "the Bicep output; locally, point it at a server you can reach."
            )

    # ----------------------------------------------------------- credentials
    def _password(self) -> str:
        """An Entra access token, used where a password would normally go.

        `PGPASSWORD` is honoured if set, so a developer with a password-enabled
        server or a local PostgreSQL in a container needs no Azure identity at
        all. The deployed server has password auth disabled, so this path is
        never taken there.
        """
        static = os.getenv("PGPASSWORD")
        if static:
            return static

        now = time.time()
        if self._token and self._token[1] - _TOKEN_MARGIN > now:
            return self._token[0]

        if self._credential is None:
            from azure.identity import DefaultAzureCredential

            # The user-assigned identity, when the deployment gave us one. Without
            # this, DefaultAzureCredential on App Service picks the system-assigned
            # identity, which has no grant on the server and fails confusingly.
            client_id = os.getenv("AZURE_CLIENT_ID") or None
            self._credential = DefaultAzureCredential(managed_identity_client_id=client_id)

        token = self._credential.get_token(_PG_SCOPE)
        self._token = (token.token, float(token.expires_on))
        return token.token

    def _open(self) -> Any:
        import psycopg

        conn = psycopg.connect(
            host=self.host,
            dbname=self.database,
            user=self.user,
            password=self._password(),
            sslmode=self.sslmode,
            autocommit=True,
            # A hung connection to a database behind a private endpoint should
            # surface as an error on one request, not as a stuck worker thread.
            connect_timeout=int(os.getenv("PG_CONNECT_TIMEOUT", "20")),
            # Overridable so that two instances against one database can be told
            # apart in `pg_stat_activity`. Not cosmetic: profiling attributed 56.5s
            # of one endpoint to a statement a *different* instance was running,
            # because every backend called itself `cloudlens` and there was nothing
            # in the view to separate them.
            application_name=os.getenv("PG_APPLICATION_NAME", "cloudlens"),
        )
        if self.schema:
            # Created here rather than assumed, so pointing at an empty database is
            # enough to get a working warehouse.
            with conn.cursor() as cur:
                cur.execute(f"CREATE SCHEMA IF NOT EXISTS {self.schema}")
        # Creation target first. An unqualified CREATE TABLE lands in the first schema on
        # this path, so it has to be the warehouse's own schema -- `PGSCHEMA` when one is
        # configured, `public` when not. Then `public`, so a configured schema still reads
        # anything shared. The shim schema goes **last**, and that ordering is a security
        # property rather than a preference.
        #
        # It used to be `[schema?] + [SHIM_SCHEMA, "public"]`, which contradicted the comment
        # that described it: with no PGSCHEMA the shim schema was first, so every warehouse
        # table the app created -- costs, costs_rollup, ingest_log, meta -- was created
        # *inside it*. That matters because `Warehouse.query` runs model-written SQL with
        # `search_path = pg_temp, cloudlens_fn, pg_catalog`: pg_temp for the scope-carrying
        # view, cloudlens_fn for the TRY_CAST shims, and nothing else. The whole design rests
        # on cloudlens_fn holding *only functions*. With the tables in there, `SELECT * FROM
        # costs_rollup` resolved -- to the unscoped table carrying every subscription's cost
        # rows, past the temp view that exists to prevent exactly that.
        #
        # pg_temp is deliberately not listed. PostgreSQL searches it first for table
        # *references* anyway, which is what makes the scope view shadow the base table --
        # but naming it here would also make it the creation target, so every table the
        # warehouse created would be temporary and would vanish when the connection went
        # back to the pool.
        path = [self.schema] if self.schema else []
        path += ["public", SHIM_SCHEMA]
        with conn.cursor() as cur:
            cur.execute(f"SET search_path TO {', '.join(path)}")
            if self.work_mem:
                # Through `set_config` rather than `SET`, which accepts no parameters.
                # Session scope, not LOCAL: these connections are pooled and autocommit,
                # so there is no enclosing transaction for LOCAL to belong to. Every
                # borrower wants the same value, and `DISCARD TEMP` on release does not
                # reset it, so it holds for the life of the connection.
                cur.execute("SELECT set_config('work_mem', %s, false)", (self.work_mem,))
        return conn

    def check_readiness(self, tables: dict[str, tuple[str, ...]]) -> bool:
        """Check core projections on a fresh read-only connection, without setup or migrations."""
        import psycopg
        from psycopg import sql

        # Normal connections use unquoted schema names, so PostgreSQL folds them to lowercase.
        schema = self.schema.lower() or "public"
        with psycopg.connect(
            host=self.host, dbname=self.database, user=self.user,
            password=self._password(), sslmode=self.sslmode, autocommit=True,
            connect_timeout=3, application_name="cloudlens-readiness",
            options="-c default_transaction_read_only=on -c statement_timeout=2000 -c lock_timeout=1000",
        ) as connection, connection.cursor() as cursor:
            for table, columns in tables.items():
                projection = sql.SQL(", ").join(sql.Identifier(column) for column in columns)
                cursor.execute(sql.SQL("SELECT {} FROM {} LIMIT 0").format(
                    projection, sql.Identifier(schema, table),
                ))
        return True

    # ------------------------------------------------------------- pooling
    def connect(self) -> PostgresConnection:
        if not self._slots.acquire(timeout=self._pool_timeout):
            raise PoolBusy("PostgreSQL connection pool is busy; retry shortly.")
        try:
            return self._checkout()
        except BaseException:
            self._slots.release()
            raise

    def _checkout(self) -> PostgresConnection:
        while True:
            with self._lock:
                if not self._idle:
                    break
                conn = self._idle.pop()
            # Probed outside the lock: it is a round trip, and holding the lock across it
            # would serialise every checkout behind one slow server.
            if self._answers(conn):
                return PostgresConnection(conn, self)
        conn = self._open()
        try:
            if not self._shims_installed:
                self._install_shims(conn)
        except Exception:
            conn.close()
            raise
        return PostgresConnection(conn, self)

    def _answers(self, conn: Any) -> bool:
        """Whether a pooled connection is still live, asked rather than assumed.

        `conn.closed` is only this process's opinion. When the *server* hangs up the handle
        still reads as open: Azure PostgreSQL closes idle connections, and a failover or an
        administrative disconnect does the same. The next borrower then dies on its first
        statement with "server closed the connection unexpectedly" -- which surfaced as a
        random 500 on whichever tab happened to borrow that connection after a quiet spell,
        with nothing wrong with the request that got it.

        One round trip on checkout, against requests that run from tens of milliseconds to
        tens of seconds. A connection that cannot answer is closed and the caller moves on to
        the next idle one, or opens a fresh connection.
        """
        if conn.closed:
            return False
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT 1")
            return True
        except Exception:  # noqa: BLE001 - an unusable connection is replaced, never raised at the caller
            try:
                conn.close()
            except Exception:  # noqa: BLE001
                pass
            return False

    def _release(self, conn: Any) -> None:
        try:
            self._return_connection(conn)
        finally:
            self._slots.release()

    def _return_connection(self, conn: Any) -> None:
        """Return a connection to the pool, or close it if the pool is full.

        Temp objects are discarded first, and that is a correctness requirement rather than
        housekeeping. The warehouse enforces subscription scope by creating a temp view named
        `costs` that shadows the base table, so a query physically cannot see rows outside the
        caller's scope. A pooled session that kept that view would apply one user's scope to the
        next user's questions -- either hiding data they are entitled to, or showing them data
        they are not.

        A connection in an aborted transaction is rolled back first: PostgreSQL refuses every
        subsequent statement on it otherwise, and the next borrower would inherit a session that
        fails everything for no visible reason.

        `statement_timeout` is reset for the same reason the temp objects are dropped. It is
        set per checkout by readers, and the next borrower may be a rebuild that runs for
        several minutes; inheriting a dashboard's limit would abort it. `RESET` rather than
        `DISCARD ALL`, which would also throw away the search path and `work_mem` that
        `_open` established for the life of the connection.
        """
        try:
            if conn.closed:
                return
            if conn.info.transaction_status != 0:  # not IDLE
                conn.rollback()
            with conn.cursor() as cur:
                cur.execute("DISCARD TEMP")
                cur.execute("RESET statement_timeout")
        except Exception:  # noqa: BLE001 - a connection we cannot reset is discarded
            try:
                conn.close()
            except Exception:  # noqa: BLE001
                pass
            return

        with self._lock:
            if len(self._idle) < self.max_idle:
                self._idle.append(conn)
                return
        try:
            conn.close()
        except Exception:  # noqa: BLE001
            pass

    def _install_shims(self, conn: Any) -> None:
        """Create the functions the SQL translation depends on.

        TRY_CAST has no PostgreSQL equivalent, so the translator emits calls to
        `try_cast_double`, `try_cast_date` and `try_cast_json`; they have to exist
        before any query that uses them runs. Idempotent, and failure is not fatal --
        an installation whose role cannot create functions still serves every query
        that does not need them.
        """
        try:
            with conn.cursor() as cur:
                cur.execute(SHIMS)
        except Exception as exc:  # noqa: BLE001
            log.warning("could not install SQL shims (%s); TRY_CAST queries will fail", exc)
        else:
            self._evacuate_shim_schema(conn)
            self._shims_installed = True
            return
        self._evacuate_shim_schema(conn)

    # Relations, in the order it is safe to move them: a view may depend on a table, so
    # tables move first and the view is left to follow its dependency.
    _RELOCATABLE = ("r", "p", "m", "v", "f")

    def _evacuate_shim_schema(self, conn: Any) -> None:
        """Move any warehouse table that was created inside the shim schema back out of it.

        Repairs deployments created before the search path was fixed. The shim schema is on
        the search path that model-written SQL runs with -- deliberately, so the TRY_CAST
        functions resolve -- and that is only safe while it holds nothing but functions. A
        `costs_rollup` in there is every subscription's cost rows, one unqualified SELECT
        away, whatever the scope view is doing.

        Correcting the path stops it recurring but does not move what is already there, and
        those tables keep shadowing the real ones for as long as they exist. So this runs
        once per connection open, is idempotent, and is a no-op on a correct database.

        Conflicting histories are parked, never discarded or merged. A failed move rolls
        back the entire evacuation and prevents this connection from being used.
        """
        from psycopg import sql

        target = self.schema or "public"
        if target == SHIM_SCHEMA:
            raise RuntimeError("The warehouse schema must not be the SQL shim schema.")
        try:
            with conn.transaction(), conn.cursor() as cur:
                cur.execute("SELECT pg_advisory_xact_lock(hashtext(%s))",
                            ("cloudlens:evacuate-shim-schema",))
                cur.execute(
                    "SELECT c.relname, c.relkind, c.oid FROM pg_class c "
                    "JOIN pg_namespace n ON n.oid = c.relnamespace "
                    "WHERE n.nspname = %s AND c.relkind = ANY(%s)",
                    (SHIM_SCHEMA, list(self._RELOCATABLE)),
                )
                found = cur.fetchall()
                if not found:
                    return
                # Tables before views, so a view that selects from one of these tables still
                # resolves its dependency after the move rather than being rewritten to point
                # back into the schema being emptied.
                order = {"r": 0, "p": 0, "f": 0, "m": 1, "v": 2}
                def exists(schema: str, name: str) -> bool:
                    cur.execute(
                        "SELECT 1 FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace "
                        "WHERE n.nspname=%s AND c.relname=%s", (schema, name))
                    return cur.fetchone() is not None

                def unique_name(name: str, oid: int) -> str:
                    base = name.encode("utf-8")[:32].decode("utf-8", errors="ignore")
                    candidate = f"{base}__stranded_{oid}"
                    suffix = 0
                    while exists(target, candidate) or exists(SHIM_SCHEMA, candidate):
                        suffix += 1
                        candidate = f"{base}__stranded_{oid}_{suffix}"
                    return candidate

                for name, kind, oid in sorted(found, key=lambda r: (order.get(r[1], 3), r[0])):
                    verb = "MATERIALIZED VIEW" if kind == "m" else "VIEW" if kind == "v" \
                        else "FOREIGN TABLE" if kind == "f" else "TABLE"
                    parked = unique_name(name, oid) if exists(target, name) else name
                    # Indexes and owned sequences move with a table but retain their names.
                    cur.execute(
                        "SELECT c.relname, c.relkind, c.oid FROM pg_class c WHERE "
                        "c.oid IN (SELECT indexrelid FROM pg_index WHERE indrelid=%s) OR "
                        "(c.relkind='S' AND c.oid IN (SELECT objid FROM pg_depend "
                        "WHERE refobjid=%s AND classid='pg_class'::regclass "
                        "AND deptype IN ('a','i')))", (oid, oid))
                    for child, child_kind, child_oid in cur.fetchall():
                        if exists(target, child):
                            cur.execute(sql.SQL("ALTER {} {}.{} RENAME TO {}").format(
                                sql.SQL("SEQUENCE" if child_kind == "S" else "INDEX"),
                                sql.Identifier(SHIM_SCHEMA), sql.Identifier(child),
                                sql.Identifier(unique_name(child, child_oid))))
                    if parked != name:
                        cur.execute(sql.SQL("ALTER {} {}.{} RENAME TO {}").format(
                            sql.SQL(verb), sql.Identifier(SHIM_SCHEMA), sql.Identifier(name),
                            sql.Identifier(parked)))
                    cur.execute(sql.SQL("ALTER {} {}.{} SET SCHEMA {}").format(
                        sql.SQL(verb), sql.Identifier(SHIM_SCHEMA), sql.Identifier(parked),
                        sql.Identifier(target)))
                    log.warning("store: preserved %s.%s as %s.%s; reconcile conflicting "
                                "histories before removing either copy.",
                                SHIM_SCHEMA, name, target, parked)
        except Exception as exc:
            raise RuntimeError(
                f"Could not safely move relations from {SHIM_SCHEMA} into {target}; "
                "the warehouse connection is refused and no history was discarded."
            ) from exc

    def close(self) -> None:
        with self._lock:
            idle, self._idle = self._idle, []
        for conn in idle:
            try:
                conn.close()
            except Exception:  # noqa: BLE001
                pass
