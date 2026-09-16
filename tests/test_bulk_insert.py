"""Bulk insert -- correctness first, then speed.

Getting Python values into the database was the whole cost of a refresh. The deployed instance
is only 5.4x slower than a laptop on a plain Python loop and scans its whole 20,667-row table in
8ms, but it inserted 51 rows/s against 2,200 locally. Neither CPU nor disk explains a 43x gap;
the per-value parameter binding layer does.

So batches are streamed with COPY, which binds nothing. That is exactly the kind of change that
trades a performance problem for a correctness one -- a resource group named with a comma, a tag
holding a newline, a backslash that COPY's text format treats as an escape, or a NULL coming
back as an empty string. These checks are the evidence that it does not: they run hostile values
through COPY, through parameter binding, and through a plain executemany, and demand that all
three store byte-identical rows.

The schema here is the REAL one, with a DATE column and four DOUBLEs. An earlier version of this
file used VARCHAR for all 25 columns, and that convenient simplification is exactly why the
first server-side benchmark blew up on data the real table would never have accepted.
"""
import ast
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import app.warehouse as W
from app.warehouse import COLUMNS, INSERT_CHUNK, NUMERIC, insert_rows
from app.warehouse import warehouse as WAREHOUSE

checks = 0
failures: list[str] = []


def check(label: str, ok: bool) -> None:
    global checks
    checks += 1
    print(f"  {'ok  ' if ok else 'FAIL'} {label}")
    if not ok:
        failures.append(label)


def section(name: str) -> None:
    print(f"\n{name}")


COLS = list(COLUMNS)
names = ",".join(f'"{c}"' for c in COLS)
TYPED = ",\n ".join(
    f'"{c}" ' + ("DOUBLE" if c in NUMERIC else
                 ("DATE" if c == "ChargePeriodStart" else "VARCHAR"))
    for c in COLS)

con = WAREHOUSE.connect()
_made = 0


def fresh(extra_first_column: bool = False) -> str:
    """An empty table of the real shape, and its name.

    A table per case rather than one reused table: several of the comparisons below read
    two of them side by side, and a DELETE between cases would make the test order-dependent.
    """
    global _made
    _made += 1
    table = f"bulk_{_made}"
    prefix = '"Extra" VARCHAR, ' if extra_first_column else ""
    con.execute(f"DROP TABLE IF EXISTS {table}")
    con.execute(f"CREATE TABLE {table} ({prefix}{TYPED})")
    return table


def rows_of(table: str):
    return con.execute(f"SELECT {names} FROM {table} ORDER BY 1, 2, 3").fetchall()


def count(table: str, where: str = "") -> int:
    return con.execute(f"SELECT count(*) FROM {table} {where}").fetchone()[0]


# The values that break a naive CSV round-trip, plus the ones that break COPY's text format.
# The backslash entries matter most here: COPY reads `\N` as NULL and `\t` as a tab unless the
# driver escapes them, so 'has\\backslash' and the literal '\\N' are the shapes that would
# silently change meaning.
nasty = ['has,comma', 'has"quote', "has'apostrophe", 'has\nnewline', 'has\ttab',
         '', 'trailing ', ' leading', 'unicode Rs e ZH', 'back\\slash',
         'NULL', 'null', '\\N', '""']


def build(n):
    out = []
    for i in range(n):
        row = []
        for j, c in enumerate(COLS):
            if c in NUMERIC:
                row.append(None if i % 13 == 0 else round((i + j) * 1.7, 6))
            elif c == "ChargePeriodStart":
                row.append(f"2026-08-{i % 28 + 1:02d}")
            elif i % 7 == 0 and j % 4 == 0:
                row.append(None)
            else:
                row.append(nasty[(i + j) % len(nasty)])
        out.append(row)
    return out


section("every path writes exactly what executemany writes")

rows = build(600)

base = fresh()
con.executemany(
    f"INSERT INTO {base} ({names}) VALUES ({','.join('?' * len(COLS))})", rows)
want = rows_of(base)

# Force each path rather than trusting the caller to pick the interesting one.
bound = fresh()
W._insert_via_binding(con, rows, bound, INSERT_CHUNK)
got_bound = rows_of(bound)

copied = fresh()
W._insert_via_copy(con, rows, copied)
got_copy = rows_of(copied)

check("parameter binding matches executemany exactly", got_bound == want)
check("the copy path matches executemany exactly", got_copy == want)
check("and the two helpers agree with each other", got_bound == got_copy)

nulls_txt = count(base, 'WHERE "MeterName" IS NULL')
nulls_num = count(base, 'WHERE "UnitPrice" IS NULL')
check("the fixture actually contains text NULLs to preserve", nulls_txt > 0)
check("and numeric NULLs", nulls_num > 0)
check("the copy path keeps text NULL as NULL",
      count(copied, 'WHERE "MeterName" IS NULL') == nulls_txt)
check("the copy path keeps numeric NULL as NULL",
      count(copied, 'WHERE "UnitPrice" IS NULL') == nulls_num)
# An empty string and a NULL are different facts. A scheme that maps '' to NULL loses one.
check("an empty string does not become NULL",
      count(copied, """WHERE "ResourceName" = ''""")
      == count(base, """WHERE "ResourceName" = ''"""))
check("a value containing a comma survives the copy path",
      count(copied, """WHERE "ResourceGroup" LIKE '%,%'""")
      == count(base, """WHERE "ResourceGroup" LIKE '%,%'"""))
check("so does one containing a newline",
      count(copied, """WHERE "ResourceGroup" LIKE '%' || chr(10) || '%'""") > 0)
check("so does one containing a tab",
      count(copied, """WHERE "ResourceGroup" LIKE '%' || chr(9) || '%'""") > 0)
check("so does one containing a double quote",
      count(copied, """WHERE "ResourceGroup" LIKE '%\"%'""") > 0)
check("dates are stored as dates, not text",
      str(con.execute(f'SELECT "ChargePeriodStart" FROM {base} LIMIT 1').fetchone()[0])
      == str(con.execute(f'SELECT "ChargePeriodStart" FROM {copied} LIMIT 1').fetchone()[0]))

section("COPY's own escape characters are data, not syntax")

# COPY text format spells NULL as \N and uses backslash for escapes. If psycopg did not escape
# on the way out, these rows would come back as NULL or with a character missing -- and nothing
# would raise, because both are valid text.
check("a literal backslash survives",
      count(copied, """WHERE "ResourceGroup" LIKE '%\\%'""")
      == count(base, """WHERE "ResourceGroup" LIKE '%\\%'"""))
check("the literal text '\\N' is preserved, not turned into NULL",
      count(copied, """WHERE "ResourceGroup" = '\\N'""")
      == count(base, """WHERE "ResourceGroup" = '\\N'"""))
check("and it is genuinely present in the fixture",
      count(base, """WHERE "ResourceGroup" = '\\N'""") > 0)
check("the literal text 'NULL' is preserved too",
      count(copied, """WHERE "ResourceGroup" = 'NULL'""")
      == count(base, """WHERE "ResourceGroup" = 'NULL'"""))

section("batching is invisible to the caller")

for n in (0, 1, INSERT_CHUNK - 1, INSERT_CHUNK, INSERT_CHUNK + 1):
    table = fresh()
    sample = build(n)
    wrote = insert_rows(con, sample, table=table)
    check(f"{n} rows in, {n} rows out (crosses a batch boundary)",
          wrote == n and count(table) == n)

section("a copy failure falls back rather than losing the batch")

# Speed is never worth a lost refresh. If anything about the COPY stream goes wrong the slow
# path still has to run, and the rows still have to land.
original = W._insert_via_copy


def boom(*a, **k):
    raise OSError("connection reset by peer")


W._insert_via_copy = boom
try:
    table = fresh()
    wrote = insert_rows(con, rows, table=table)
    check("the rows are written anyway", wrote == len(rows) and count(table) == len(rows))
    check("and they are still correct", rows_of(table) == want)

    # The case that actually matters, and the one that was broken. A COPY that fails
    # server-side aborts the surrounding transaction, and PostgreSQL then refuses every later
    # statement on it with "current transaction is aborted". Without a savepoint to roll back
    # to, the retry could not run: the batch was lost while the log claimed it had fallen back.
    #
    # So the stand-in aborts the transaction before raising, which a plain Python exception
    # does not -- a failure that never reaches the server leaves the transaction healthy and
    # the test would pass with or without the fix. The rows themselves stay valid, because the
    # property under test is that the *fallback* runs, not that bad data is rejected twice.
    def server_side_boom(*a, **k):
        try:
            con.execute("SELECT 1 / 0")
        except Exception:  # noqa: BLE001 - the point is the aborted transaction it leaves
            pass
        raise OSError("simulated server-side COPY failure")

    W._insert_via_copy = server_side_boom
    table = fresh()
    con.execute("BEGIN TRANSACTION")
    try:
        wrote = insert_rows(con, rows, table=table)
        con.execute("COMMIT")
    except Exception as exc:  # noqa: BLE001
        con.execute("ROLLBACK")
        wrote = f"raised {type(exc).__name__}"
    check("an aborted transaction still falls back rather than losing the batch",
          wrote == len(rows))
    check("and the rows survive the commit", count(table) == len(rows))
    check("with the same values", rows_of(table) == want)
finally:
    W._insert_via_copy = original

section("columns are named, not positional")

# A positional insert depends on the table matching today's COLUMNS order. ALTER TABLE appends,
# so a column added later shifts every value after it one place left -- silently, and only in
# the columns nobody checks first.
shifted = fresh(extra_first_column=True)
insert_rows(con, build(3), table=shifted)
check("an unexpected leading column does not shift the data",
      con.execute(f'SELECT "Extra" FROM {shifted}').fetchone()[0] is None)
check("and the named columns still receive their values",
      con.execute(f'SELECT "ServiceName" FROM {shifted}').fetchone()[0] is not None)

section("fast enough to matter")

big = build(7000)

bind_con = fresh()
t0 = time.perf_counter()
W._insert_via_binding(con, big, bind_con, INSERT_CHUNK)
bind = time.perf_counter() - t0

copy_con = fresh()
t0 = time.perf_counter()
W._insert_via_copy(con, big, copy_con)
fast = time.perf_counter() - t0

print(f"       binding {bind:6.2f}s   copy {fast:6.2f}s"
      f"   ({bind / fast if fast else 0:.0f}x)")
check("the copy path beats parameter binding", fast < bind)
check("and both wrote the same number of rows",
      count(bind_con) == count(copy_con) == 7000)

section("every write path uses the shared helper")


def calls_insert_rows(path):
    """Which functions in `path` call `insert_rows`, found in the parse tree.

    This used to match the literal text `insert_rows(con, records)`. That asserted the name
    of a local variable, so renaming one -- `records` became `retained` -- reported that the
    warehouse had stopped bulk-inserting when nothing about the write had changed. It would
    equally have been satisfied by the same characters inside a comment. Ask the syntax tree
    what is called instead; that is the thing the check is actually about.
    """
    tree = ast.parse(open(path, encoding="utf-8").read())
    parents = {child: node for node in ast.walk(tree) for child in ast.iter_child_nodes(node)}

    def enclosing(node):
        while node is not None:
            if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
                return node.name
            node = parents.get(node)
        return "<module>"

    return {enclosing(node) for node in ast.walk(tree)
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
            and node.func.id == "insert_rows"}


def writes_rows_with_executemany(path):
    """`executemany` on a connection or cursor, which is the slow path COPY replaced."""
    tree = ast.parse(open(path, encoding="utf-8").read())
    return any(isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
               and node.func.attr == "executemany" for node in ast.walk(tree))


wh = open("app/warehouse.py", encoding="utf-8").read()
warehouse_callers = calls_insert_rows("app/warehouse.py")
export_callers = calls_insert_rows("app/exports.py")
check("the quick and report paths use it", bool(warehouse_callers))
check("the FOCUS/export path uses it", bool(export_callers))
check("neither writes rows with executemany any more",
      not writes_rows_with_executemany("app/warehouse.py")
      and not writes_rows_with_executemany("app/exports.py"))
check("a failed COPY is logged rather than swallowed silently",
      "copy bulk load failed" in wh)

con.close()

print(f"\n{checks - len(failures)}/{checks} checks passed")
if failures:
    print("failed: " + ", ".join(failures))
    sys.exit(1)
