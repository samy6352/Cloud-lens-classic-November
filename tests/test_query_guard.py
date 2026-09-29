"""The guard around model-authored SQL.

`query()` is the one path that executes SQL a language model wrote, so it is the one place
where "the model made a mistake" and "the database was modified" could become the same
sentence. Everything else in the application runs SQL that a person wrote and reviewed.

There are two defences and they fail differently, which is why both are tested here:

  * The text checks reject what should never have been asked, and produce a message the
    model can act on. They are a filter on a string, so they can only ever be as good as
    the list -- and a list that is too eager is its own bug, because rejecting a legitimate
    question about a service called `Load Balancer` is a wrong answer with a confident
    explanation attached.

  * The READ ONLY transaction is the guarantee. It does not depend on recognising anything:
    PostgreSQL refuses the write itself. This is what replaced the old embedded engine's
    read-only connection when that engine was removed, and losing it silently is exactly
    the kind of regression this file exists to catch.
"""

from __future__ import annotations

import sys as _sys
from pathlib import Path as _Path

# Tests live in tests/ and import the application from the checkout root.
_sys.path.insert(0, str(_Path(__file__).resolve().parents[1]))

import os
import uuid

import pytest

from app.sqldialect import SHIM_SCHEMA

pytestmark = pytest.mark.skipif(
    not os.getenv("CLOUDLENS_PG_TEST"),
    reason="set CLOUDLENS_PG_TEST=<host> to run against a PostgreSQL server",
)

# Set before anything imports app.warehouse, which builds a Warehouse at module scope and
# would otherwise create its tables in `public` on the shared server.
if os.getenv("CLOUDLENS_PG_TEST"):
    os.environ["PGHOST"] = os.environ["CLOUDLENS_PG_TEST"]
    os.environ["PGDATABASE"] = os.getenv("CLOUDLENS_PG_DB", "postgres")
    os.environ["PGUSER"] = os.environ["CLOUDLENS_PG_USER"]
    os.environ["PGSCHEMA"] = f"cl_guard_{uuid.uuid4().hex[:8]}"


@pytest.fixture(scope="module")
def wh():
    """One warehouse for the module. Nothing here writes, so nothing needs isolating."""
    from app.warehouse import Warehouse

    warehouse = Warehouse()
    yield warehouse

    schema = warehouse._store.schema
    with warehouse._store.connect() as con:
        con.execute(f"DROP SCHEMA IF EXISTS {schema} CASCADE")
    warehouse._store.close()


# --------------------------------------------------------------- what must be allowed
#
# Every one of these was refused before the scanner learned to ignore string literals, and
# every one is a question a person would reasonably ask. `Load Balancer` is a real Azure
# service name; `delete-me` is a real resource group naming convention; a meter description
# can contain any word at all. A guard that blocks them is not being careful, it is wrong.

@pytest.mark.parametrize("sql", [
    "SELECT count(*) FROM costs",
    'SELECT sum("BilledCost") FROM costs'
    ' WHERE "ServiceName" = \'Load Balancer\' AND "BillingCurrency" = \'USD\'',
    "SELECT count(*) FROM costs WHERE \"ResourceGroup\" = 'delete-me'",
    "SELECT count(*) FROM costs WHERE \"MeterName\" LIKE '%update%'",
    "SELECT count(*) FROM costs WHERE \"ResourceName\" = 'drop-test'",
    "SELECT count(*) FROM costs WHERE \"ProductName\" = 'Data Copy'",
    'WITH d AS (SELECT "ChargePeriodStart" AS day FROM costs) SELECT count(*) FROM d',
])
def test_a_legitimate_question_is_answered(wh, sql):
    assert "rows" in wh.query(sql)


# ------------------------------------------------------------- what must be refused
#
# Split by which defence is expected to catch it, because "refused somehow" is not the
# property worth holding -- a query refused only because the database happened to error
# gives the model no way to correct itself.

@pytest.mark.parametrize("sql", [
    "DELETE FROM costs",
    "UPDATE costs SET \"BilledCost\" = 0",
    "DROP TABLE costs",
    "SELECT 1; DROP TABLE costs",
    # A write hidden behind a leading SELECT: a data-modifying CTE is the one way an
    # INSERT/UPDATE/DELETE can appear in something that starts with WITH.
    "WITH w AS (DELETE FROM costs RETURNING 1) SELECT count(*) FROM w",
    # Reads that a READ ONLY transaction would happily allow.
    "SELECT pg_read_file('/etc/passwd')",
    "SELECT lo_import('/etc/passwd')",
    "SELECT pg_sleep(30)",
    "SELECT pg_terminate_backend(1)",
])
def test_a_dangerous_query_is_refused_with_an_explanation(wh, sql):
    with pytest.raises(ValueError):
        wh.query(sql)


@pytest.mark.parametrize("sql", [
    "SELECT * FROM public.costs",
    'SELECT * FROM "public".costs',
    "SELECT * FROM main.costs",
    # The one that mattered: costs_rollup holds the same money for *every* subscription and
    # is not scope-filtered, so reaching it by qualified name would return other people's spend.
    "SELECT * FROM public.costs_rollup",
    'SELECT "SubAccountName" FROM public.ingest_log',
    "SELECT a.x FROM public.costs a JOIN costs b ON true",
    # A comma join puts the second relation after no keyword at all, so a check that looked
    # for a qualified name immediately after FROM or JOIN never saw it. This is the base
    # table, unscoped -- every subscription's line-item detail.
    "SELECT * FROM costs, public.costs",
    "SELECT * FROM costs c, public.costs_rollup r",
    'SELECT * FROM costs, "public"."costs_rollup"',
    # TABLE is a complete SELECT in PostgreSQL's grammar, so a CTE can use one and never
    # mention FROM.
    "WITH x AS (TABLE public.costs) SELECT * FROM x",
    'WITH x AS (TABLE public.costs_rollup) SELECT sum("BilledCost") FROM x',
    # Qualification hidden behind a quoted schema, and in a subquery.
    'SELECT * FROM (SELECT * FROM "public".costs_rollup) z',
    "SELECT (SELECT count(*) FROM public.costs) AS n",
])
def test_a_qualified_name_cannot_reach_past_the_scope_view(wh, sql):
    """The scope view is named `costs` and shadows the base table.

    A schema-qualified name resolves straight to the table underneath it -- past the temp
    view carrying the caller's scope, and past the restricted search path, because
    qualification is what bypasses a search path. So the *qualifier* is refused wherever it
    appears, rather than the check trying to work out where a relation is: the positions a
    relation can occupy are PostgreSQL's grammar, and chasing them in a regex is a race this
    code would lose.
    """
    with pytest.raises(ValueError, match="without a database or schema prefix"):
        wh.query(sql)


def test_an_alias_qualified_column_is_still_allowed(wh):
    """`c."BilledCost"` is a dot too, and is ordinary SQL.

    Refusing the qualifier by name rather than by position is what keeps these working: `c`
    and `d` are not schemas, so they are not refused.
    """
    assert "rows" in wh.query('SELECT c."BilledCost" FROM costs c LIMIT 1')
    assert "rows" in wh.query(
        'SELECT count(*) AS n FROM costs c JOIN costs d ON c."ResourceId" = d."ResourceId"')


@pytest.mark.parametrize("sql", [
    # An E'' string honours backslash escapes, so `E'x\''` is the literal `x'` -- and a
    # scanner that closed the span at the first inner quote inverted the parity of
    # everything after it, reading executable SQL as a literal and hiding it.
    r"SELECT * FROM costs WHERE a = E'x\'' AND pg_sleep(9) = ''",
    r"SELECT * FROM costs WHERE a = E'\'' AND pg_sleep(9) IS NULL AND b = ''",
    # Dollar quoting was not recognised at all.
    "SELECT * FROM costs WHERE a = $$'$$ AND pg_sleep(9) IS NULL",
])
def test_a_keyword_cannot_be_smuggled_past_the_scanner(wh, sql):
    """The scanner must agree with PostgreSQL about where a literal ends.

    When it does not, the disagreement does not merely miss a word -- it shifts which half
    of the statement the scanner can see at all.
    """
    with pytest.raises(ValueError):
        wh.query(sql)


def test_an_unterminated_literal_is_refused_rather_than_guessed_at(wh):
    """PostgreSQL would reject it anyway; the danger is what the guess hides in the meantime."""
    with pytest.raises(ValueError, match="not valid SQL"):
        wh.query("SELECT * FROM costs WHERE a = 'unclosed")


def test_a_quoted_function_name_is_still_seen(wh):
    """Quoting a lower-case function name changes nothing about how it resolves.

    So blanking quoted identifiers along with the literals hid callable functions from the
    scanner. They keep their text now and lose only their quotes.
    """
    with pytest.raises(ValueError, match="not allowed here"):
        wh.query("SELECT \"pg_sleep_for\"('10 minutes')")
    with pytest.raises(ValueError, match="not allowed here"):
        wh.query("SELECT \"set_config\"('search_path', 'public', true)")


def test_a_semicolon_inside_a_value_is_data_not_a_second_statement(wh):
    """The same false-rejection class as `Load Balancer`, one check further down."""
    assert "rows" in wh.query("SELECT count(*) FROM costs WHERE \"Tags\" LIKE '%a;b%'")


def test_try_cast_still_works_under_the_restricted_search_path(wh):
    """The translator emits calls to shims that live in the application schema.

    Hiding that schema is what stops model SQL reaching `costs_rollup`, so the shims are
    copied into pg_temp for the statements that need them rather than the schema being put
    back on the path. TRY_CAST is the construct this whole change set was extended to
    support, so it failing here would be a silent regression in the one path that uses it.
    """
    rows = wh.query('SELECT TRY_CAST(\'not a number\' AS DOUBLE) AS d')["rows"]
    assert rows == [{"d": None}]
    rows = wh.query('SELECT TRY_CAST(\'12.5\' AS DOUBLE) AS d')["rows"]
    assert rows == [{"d": pytest.approx(12.5)}]


def test_other_tables_in_the_schema_are_invisible(wh):
    """Not by name, but by hiding the schema they live in.

    A deny-list of table names is only as good as someone remembering to extend it the next
    time a table is added. Restricting the search path to pg_temp means the scope-carrying
    `costs` view and PostgreSQL's own built-ins are all that model SQL can resolve, so this
    holds for tables that do not exist yet.

    `costs_rollup` is the one that made this necessary: it carries BilledCost for every
    subscription at a coarser grain and has no scope filter of its own.
    """
    import psycopg

    for table in ("costs_rollup", "ingest_log", "meta"):
        with pytest.raises(psycopg.errors.UndefinedTable):
            wh.query(f"SELECT count(*) FROM {table}")

    # And the table it *is* allowed to see still resolves.
    assert wh.query("SELECT count(*) AS n FROM costs")["rows"] == [{"n": 0}]


@pytest.mark.parametrize("sql", [
    # The qualifier is inside a string literal, so the scanner -- which must blank literals
    # to avoid tripping over a resource group called `delete-me` -- cannot see it. These
    # functions take that string and run it, which is what makes them a way round every
    # check that reads the statement rather than the privileges.
    "SELECT table_to_xml('public.costs_rollup', true, false, '')",
    "SELECT query_to_xml('SELECT * FROM public.costs', true, false, '')",
    "SELECT query_to_xmlschema('SELECT * FROM public.costs', true, false, '')",
    "SELECT query_to_xml_and_xmlschema('SELECT 1', true, false, '')",
    "SELECT database_to_xml(true, false, '')",
    "SELECT schema_to_xml('public', true, false, '')",
])
def test_a_function_that_evaluates_sql_cannot_reach_past_the_scope_view(wh, sql):
    """The bypass that privilege separation exists for.

    `table_to_xml('public.costs_rollup', ...)` passed every text check: the statement starts
    with SELECT, contains no semicolon, names no banned keyword, and the schema qualifier is
    invisible because it lives inside a literal. It then read every subscription's rows,
    because an explicit qualifier resolves past both the restricted search path and the
    scope-carrying view.

    Either defence is enough on its own, and the test accepts either outcome deliberately:
    the word list refuses it with a sentence, and the role it runs as cannot read those
    tables whatever it calls them. Both are present on any deployment that can run a model
    query at all -- privilege separation is now required rather than preferred, so there is
    no longer a configuration where only the word list stands between a payload and the data.
    """
    import psycopg

    with pytest.raises((ValueError, psycopg.errors.InsufficientPrivilege,
                        psycopg.errors.UndefinedTable, psycopg.errors.UndefinedFunction)):
        wh.query(sql)


def test_the_scope_view_is_still_readable_under_the_restricted_role(wh):
    """The other half of the previous test: locking it down must not lock out the product.

    Privilege separation that also refuses the legitimate question is not a fix, it is an
    outage. The role can read the view, and the shim functions the SQL translation emits
    still resolve for it.
    """
    assert wh.query("SELECT count(*) AS n FROM costs")["rows"] == [{"n": 0}]
    assert wh.query("SELECT TRY_CAST('12.5' AS DOUBLE) AS d")["rows"] == [{"d": pytest.approx(12.5)}]


# ------------------------------------------------- clearing a shim schema already polluted
#
# The search path in `PostgresStore._open` is fixed, so a database created from here on has
# its tables in the right place. That does nothing for a database created by the version
# that had them in `cloudlens_fn`, which is every affected deployment -- and on those the
# rows are still sitting in the one schema model SQL is allowed to read.
#
# `SHIM_SCHEMA` is a single shared schema on the test server, so these plant their relations
# in a scratch schema and point `app.warehouse.SHIM_SCHEMA` at it. That keeps a test from
# evacuating a relation belonging to a test running beside it, and exercises the same code.

@pytest.fixture
def fake_shim(wh, monkeypatch):
    """A private stand-in for `cloudlens_fn`, so these tests can pollute it safely."""
    import app.warehouse as warehouse_module

    name = f"cl_shim_{uuid.uuid4().hex[:8]}"
    with wh._store.connect() as con:
        con.execute(f"CREATE SCHEMA {name}")
    monkeypatch.setattr(warehouse_module, "SHIM_SCHEMA", name)
    yield name
    with wh._store.connect() as con:
        con.execute(f"DROP SCHEMA IF EXISTS {name} CASCADE")


def _relations_in(con, schema):
    return [r[0] for r in con.execute(
        "SELECT c.relname FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace "
        "WHERE n.nspname = ? AND c.relkind = ANY(?) ORDER BY c.relname",
        [schema, ["r", "p", "m", "v", "f"]],
    ).fetchall()]


def test_a_table_stranded_in_the_shim_schema_is_moved_with_its_rows(wh, fake_shim):
    """The migration, and the reason it cannot be left to `CREATE TABLE IF NOT EXISTS`.

    Fixing the search path made `_init_schema` create a *new, empty* `costs` in the real
    schema while the populated one stayed in `cloudlens_fn`. Two faults at once: the app
    reports an empty warehouse, and the rows it should be serving are in the only schema a
    model-authored query can reach. Moving the relation answers both.
    """
    schema = wh._store.schema
    with wh._store.connect() as con:
        con.execute(f"CREATE TABLE {fake_shim}.stranded_costs (id int, money numeric)")
        con.execute(f"INSERT INTO {fake_shim}.stranded_costs VALUES (1, 42.00)")
        con.execute(f"CREATE VIEW {fake_shim}.stranded_view AS "
                    f"SELECT * FROM {fake_shim}.stranded_costs")

        wh._evacuate_shim_schema(con, schema)

        assert _relations_in(con, fake_shim) == [], "the shim schema still holds relations"
        moved = _relations_in(con, schema)
        assert "stranded_costs" in moved and "stranded_view" in moved
        # The rows came with it: this is a move, not a re-create.
        assert con.execute(
            f'SELECT money FROM "{schema}".stranded_costs'
        ).fetchone()[0] == 42


def test_a_name_clash_is_parked_rather_than_resolved(wh, fake_shim):
    """Two tables called `costs` and no way to know which one the deployment means.

    Dropping either is unrecoverable and picking one is a guess, so the stranded copy is
    moved aside under another name. That is enough: the schema it is moved *into* is off the
    search path model SQL runs with, so the disclosure ends either way.
    """
    schema = wh._store.schema
    with wh._store.connect() as con:
        con.execute(f"CREATE TABLE {fake_shim}.costs (marker int)")
        con.execute(f"INSERT INTO {fake_shim}.costs VALUES (7)")

        wh._evacuate_shim_schema(con, schema)

        assert _relations_in(con, fake_shim) == []
        parked = [n for n in _relations_in(con, schema) if n.startswith("costs__stranded_")]
        assert len(parked) == 1, f"expected the clash to be parked, found {parked}"
        # Neither copy was destroyed.
        assert con.execute(f'SELECT marker FROM "{schema}"."{parked[0]}"').fetchone()[0] == 7
        assert con.execute(f'SELECT count(*) FROM "{schema}".costs').fetchone()[0] == 0
        con.execute(f'DROP TABLE "{schema}"."{parked[0]}"')


def test_a_relation_that_cannot_be_moved_stops_the_application(wh, fake_shim):
    """Fail closed, because the alternative is serving from a warehouse with no boundary.

    A relation the application cannot move -- it does not own it, or the target schema is
    not there -- is a relation that stays where model SQL can read it. Starting anyway would
    put a dashboard on top of that, so `_init_schema` refuses instead.
    """
    from app.warehouse import StrandedWarehouseTables

    with wh._store.connect() as con:
        con.execute(f"CREATE TABLE {fake_shim}.costs_rollup (id int)")
        with pytest.raises(StrandedWarehouseTables) as caught:
            wh._evacuate_shim_schema(con, "no_such_schema_here")
    assert "costs_rollup" in str(caught.value)


def test_the_warehouse_refuses_to_live_in_the_shim_schema(wh, fake_shim):
    """`PGSCHEMA=cloudlens_fn` would put the tables where the model reads, permanently.

    Nothing to migrate and nowhere to migrate to, so this is caught as configuration rather
    than discovered later as an empty result from a query that should not have run.
    """
    from app.warehouse import StrandedWarehouseTables

    with wh._store.connect() as con, pytest.raises(StrandedWarehouseTables):
        wh._evacuate_shim_schema(con, fake_shim)


def test_a_clean_shim_schema_is_left_alone(wh, fake_shim):
    """The ordinary case, which must cost one catalogue query and change nothing."""
    schema = wh._store.schema
    with wh._store.connect() as con:
        before = _relations_in(con, schema)
        wh._evacuate_shim_schema(con, schema)
        assert _relations_in(con, schema) == before


def test_startup_clears_the_shim_schema_rather_than_only_offering_to(wh, fake_shim):
    """The wiring, not the routine. A migration nothing calls fixes nothing.

    `_init_schema` runs on every boot and is where the fault would otherwise be made
    permanent: it creates `costs` in the real schema, so a stranded copy has to be moved
    before that rather than shadowed by it.
    """
    schema = wh._store.schema
    with wh._store.connect() as con:
        con.execute(f"CREATE TABLE {fake_shim}.stranded_at_boot (id int)")

    wh._init_schema()

    with wh._store.connect() as con:
        assert _relations_in(con, fake_shim) == [], (
            "startup left relations in the schema model queries can read")
        assert "stranded_at_boot" in _relations_in(con, schema)
        con.execute(f'DROP TABLE "{schema}".stranded_at_boot')


def test_the_shim_schema_holds_no_relations(wh):
    """The assumption the whole restricted search path rests on, asserted directly.

    `Warehouse.query` runs model-written SQL with `search_path = pg_temp, cloudlens_fn,
    pg_catalog`: pg_temp for the scope-carrying view, cloudlens_fn for the TRY_CAST shims,
    nothing else. That is only a boundary while cloudlens_fn holds *functions*.

    It did not. `PostgresStore._open` put the shim schema first on the connection's search
    path, so every unqualified CREATE TABLE the warehouse issued landed inside it -- costs,
    costs_rollup, ingest_log and meta, on any deployment without PGSCHEMA set. An
    unqualified `SELECT * FROM costs_rollup` then resolved there, to every subscription's
    cost rows, past the temp view that exists to prevent precisely that.

    The tests above could not catch it: each asks whether one *named* table is reachable,
    and privilege separation turns that into a raised exception either way. This asks the
    structural question instead, so it fails for the next table added as well as these four.
    """
    with wh._store.connect() as con:
        found = con.execute(
            "SELECT c.relname FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace "
            "WHERE n.nspname = ? AND c.relkind = ANY(?) ORDER BY c.relname",
            [SHIM_SCHEMA, ["r", "p", "m", "v", "f"]],
        ).fetchall()
    assert [r[0] for r in found] == [], (
        f"{SHIM_SCHEMA} is on the search path model SQL runs with and must hold only "
        f"functions, but it holds relations: {[r[0] for r in found]}"
    )


def test_the_search_path_never_makes_the_shim_schema_the_creation_target(wh):
    """The cause, rather than the symptom the test above catches.

    An unqualified CREATE TABLE lands in the first schema on the search path. The shim
    schema must therefore never be first -- and must come after the warehouse's own schema
    and public, so a table reference resolves to the real table rather than to anything
    left in there.
    """
    with wh._store.connect() as con:
        path = [p.strip().strip('"') for p in
                con.execute("SHOW search_path").fetchone()[0].split(",")]
    assert path, "no search path set"
    assert path[0] != SHIM_SCHEMA, (
        f"{SHIM_SCHEMA} is the creation target: every unqualified CREATE TABLE lands in "
        f"the one schema model SQL is allowed to read. search_path = {path}")
    assert path[-1] == SHIM_SCHEMA, (
        f"{SHIM_SCHEMA} should be last so it resolves functions and nothing else, "
        f"but search_path = {path}")


def test_a_runaway_query_is_stopped_rather_than_holding_a_connection(wh, monkeypatch):
    """The pool is small; a query with no ceiling takes a connection out of it indefinitely.

    An accidental cross join costs the same as a deliberate one, so this cannot be left to
    the word list.
    """
    import psycopg

    from app import warehouse as W

    monkeypatch.setattr(W, "QUERY_TIMEOUT_MS", 250)
    monkeypatch.setattr(wh._store, "statement_timeout", "25ms")
    with pytest.raises(psycopg.errors.QueryCanceled):
        wh.query("SELECT count(*) FROM generate_series(1, 200000000)")

    # The session is handed back in a usable state, not left aborted.
    assert wh.query("SELECT 1 AS n")["rows"] == [{"n": 1}]


# ----------------------------------------------------------------- the guarantee

def test_the_statement_runs_in_a_read_only_transaction(wh):
    """Asked of the server, not of the code.

    This is the property that replaced the removed engine's read-only connection. If it ever
    stops holding, the banned-word list becomes the only thing between a model's mistake and
    a modified warehouse -- and a list is not a guarantee.
    """
    rows = wh.query("SELECT current_setting('transaction_read_only') AS ro")["rows"]
    assert rows[0]["ro"] == "on"


def test_ordinary_connections_are_still_writable(wh):
    """The ingest has to be able to write.

    Making the whole session read-only would satisfy the test above and break every refresh,
    so the two are asserted together.
    """
    with wh.connect() as con:
        assert con.execute("SELECT current_setting('transaction_read_only')").fetchone()[0] == "off"


def test_the_connection_is_usable_after_a_refused_query(wh):
    """A rejected query must not poison the pooled session.

    The statement runs inside a transaction that has to be ended whether it succeeded or
    failed. If it were left open, the next borrower would inherit a session in an aborted
    transaction and every subsequent statement on it would fail for no visible reason.
    """
    import psycopg

    with pytest.raises(psycopg.Error):
        wh.query("SELECT no_such_column_at_all FROM costs")

    assert wh.query("SELECT 1 AS n")["rows"] == [{"n": 1}]
    assert wh.row_count() == 0


# ------------------------------------------------------ privilege separation is required
#
# All of the above rests on the model's statement running as `cloudlens_query`. That used to
# be best effort: if the role could not be created, or the per-connection grants failed,
# `query()` logged a warning and ran the statement with the application's own rights instead.
#
# Which rights those are is what makes it serious. The default deployment registers the
# application's managed identity as an Entra *administrator* of the server, so the fallback
# did not degrade privilege separation to "the caller's scope" -- it removed it entirely, and
# a bypass that got past the word list could read every tenant's costs. The only signal was a
# line in a log, on a deployment that otherwise looked healthy.


def test_the_query_role_is_actually_assumable_here(wh):
    """The fixture's own warehouse must have privilege separation, or nothing below means much.

    Asserted rather than assumed, because every other test in this file would still pass if
    the role were silently unavailable -- which is exactly how this went unnoticed.
    """
    assert wh._query_role is True
    with wh.connect() as con:
        assert wh._can_assume_query_role(con) is True


def test_a_query_is_refused_rather_than_run_unconfined(wh, monkeypatch):
    """The fix. No role, no query -- not "no role, run it as the administrator"."""
    from app.warehouse import QueryRoleUnavailable

    monkeypatch.setattr(wh, "_query_role", False)

    with pytest.raises(QueryRoleUnavailable):
        wh.query("SELECT count(*) FROM costs")


def test_the_refusal_is_not_reported_as_a_bad_query(wh, monkeypatch):
    """It must not be a ValueError.

    `query_costs` returns ValueError text straight to the model as something to correct. A
    misconfigured server dressed up as a query error sends it round a retry loop it cannot
    win, and buries an operational fault in what reads like an ordinary refusal.
    """
    from app.warehouse import QueryRoleUnavailable

    assert not issubclass(QueryRoleUnavailable, ValueError)

    monkeypatch.setattr(wh, "_query_role", False)
    try:
        wh.query("SELECT count(*) FROM costs")
    except QueryRoleUnavailable as exc:
        assert "cloudlens_query" in str(exc) or "not run" in str(exc)


def test_the_role_is_still_dropped_for_the_statement_itself(wh):
    """The role is assumed for the model's statement, not merely available.

    A `SET LOCAL ROLE` that never ran would leave every check above passing for the wrong
    reason, so the statement is asked who it is.
    """
    from app.warehouse import QUERY_ROLE

    assert wh.query("SELECT current_user AS who")["rows"] == [{"who": QUERY_ROLE}]


def test_a_grant_failure_refuses_rather_than_falling_back(wh):
    """The second fail-open path: the per-connection grants.

    These run against a temp schema that is rebuilt for every borrowed connection, so this is
    a live failure mode rather than a start-up one -- and it used to end in the same silent
    fallback.
    """
    from app.warehouse import QueryRoleUnavailable

    class _NoGrants:
        """A connection that can answer questions but refuses to grant anything."""

        def execute(self, sql, *args):
            if "pg_my_temp_schema" in sql:
                return _Answer(("pg_temp_9",))
            if sql.strip().upper().startswith("GRANT"):
                raise RuntimeError("permission denied")
            return _Answer(None)

    class _Answer:
        def __init__(self, row):
            self._row = row

        def fetchone(self):
            return self._row

    with pytest.raises(QueryRoleUnavailable):
        wh._grant_scope_to_query_role(_NoGrants())


def test_a_session_without_a_scope_view_is_refused(wh):
    """No temp schema means no scope view, so there is nothing safe to run."""
    from app.warehouse import QueryRoleUnavailable

    class _NoTempSchema:
        def execute(self, sql, *args):
            class _Answer:
                def fetchone(self):
                    return None
            return _Answer()

    with pytest.raises(QueryRoleUnavailable):
        wh._grant_scope_to_query_role(_NoTempSchema())


def test_the_documented_remedy_actually_provisions_the_role():
    """The error message names `create-app-role.sql`, so that script has to do the job.

    It did not. The script demotes the application from administrator -- which is what takes
    away its ability to create roles -- without creating the query role or granting it. A
    deployment that followed the hardening guide therefore lost privilege separation, and now
    would lose model queries altogether, on the strength of an instruction that had never
    provisioned the thing it was cited for.
    """
    from app.warehouse import QUERY_ROLE

    script = (_Path(__file__).resolve().parents[1]
              / "infra" / "sql" / "create-app-role.sql").read_text(encoding="utf-8")

    assert f"CREATE ROLE {QUERY_ROLE}" in script
    assert f"GRANT {QUERY_ROLE} TO" in script


def test_the_agent_tells_the_model_not_to_retry(monkeypatch):
    """A configuration fault is not something rewriting the SQL can fix."""
    import asyncio

    import app.agent as agent
    from app.warehouse import QueryRoleUnavailable, warehouse

    def refuse(*a, **k):
        raise QueryRoleUnavailable("the role could not be assumed")

    monkeypatch.setattr(warehouse, "query", refuse)

    for call in (agent.query_costs("SELECT 1"),
                 agent.render_chart("SELECT 1", "bar", "a", "b", "t")):
        result = asyncio.run(call)
        assert result["retry"] is False
        assert "role" in result["error"]
