"""Opt-in monthly derived storage, with exact, transactional leaf row counts.

``CLOUDLENS_DERIVED_LAYOUT=monthly`` enables conversion on the next *full*
rebuild only. Boot and window repairs never migrate existing flat data. Once
partitioned, a side stays partitioned even if the setting returns to ``flat``.
Full builds use one parallel-capable CTAS per occupied month and a NULL-date
default leaf. Window repairs count/analyze only the intersecting leaves; summing
the small metadata table is not a scan of retained data. Parent statistics are
refreshed on full builds only (PostgreSQL does not auto-analyze partition parents).
"""
from __future__ import annotations

import datetime as dt
import os
from typing import Any

META = "_derived_partition_counts"


class RebuildRequired(RuntimeError):
    """Stored derived data cannot be certified without rebuilding from source."""


def enabled() -> bool:
    value = os.getenv("CLOUDLENS_DERIVED_LAYOUT", "flat").strip().lower()
    if value not in {"flat", "monthly"}:
        raise ValueError("CLOUDLENS_DERIVED_LAYOUT must be 'flat' or 'monthly'")
    return value == "monthly"


def partitioned(con: Any, schema: str, table: str) -> bool:
    row = con.execute(
        "SELECT c.relkind FROM pg_class c JOIN pg_namespace n "
        "ON n.oid = c.relnamespace WHERE n.nspname = ? AND c.relname = ?",
        [schema, table]).fetchone()
    return bool(row and row[0] == "p")


def _metadata(con: Any, schema: str) -> None:
    con.execute(f'''CREATE TABLE IF NOT EXISTS "{schema}".{META} (
        parent_name text NOT NULL,
        leaf_name text NOT NULL,
        leaf_oid oid NOT NULL,
        rows bigint NOT NULL CHECK (rows >= 0),
        dirty boolean NOT NULL,
        PRIMARY KEY (parent_name, leaf_name)
    )''')


def forget(con: Any, schema: str, table: str) -> None:
    exists = con.execute("SELECT to_regclass(?)",
                         [f'"{schema}".{META}']).fetchone()[0]
    if exists:
        con.execute(f'DELETE FROM "{schema}".{META} WHERE parent_name = ?', [table])


def drop_detached(con: Any, schema: str, table: str) -> None:
    """Retire recorded leaves detached from this side, never unrelated relations.

    Called only when the side is inactive, under the rebuild lock. Names alone
    are insufficient ownership evidence: match the recorded OID and namespace,
    and refuse to remove anything attached to another parent.
    """
    exists = con.execute("SELECT to_regclass(?)",
                         [f'"{schema}".{META}']).fetchone()[0]
    if not exists:
        return
    leaves = con.execute(
        f'''SELECT c.relname FROM "{schema}".{META} m
        JOIN pg_class c ON c.oid = m.leaf_oid AND c.relname = m.leaf_name
        JOIN pg_namespace n ON n.oid = c.relnamespace
        WHERE m.parent_name = ? AND n.nspname = ? AND c.relkind = 'r'
        AND NOT EXISTS (SELECT 1 FROM pg_inherits i WHERE i.inhrelid = c.oid)''',
        [table, schema]).fetchall()
    for (leaf,) in leaves:
        con.execute(f'DROP TABLE "{schema}".{leaf}')


def _month(day: dt.date) -> dt.date:
    return day.replace(day=1)


def _next(day: dt.date) -> dt.date:
    return dt.date(day.year + (day.month == 12), day.month % 12 + 1, 1)


def _leaf(table: str, month: dt.date | None) -> str:
    return f"{table}_{month.strftime('%Y%m') if month else 'null'}"


def _record(con: Any, schema: str, table: str, leaf: str) -> None:
    con.execute(f'''INSERT INTO "{schema}".{META}
        (parent_name, leaf_name, leaf_oid, rows, dirty)
        VALUES (?, ?, CAST(? AS regclass)::oid, 0, true)
        ON CONFLICT (parent_name, leaf_name) DO UPDATE SET dirty = true''',
                [table, leaf, f'"{schema}".{leaf}'])


def _attach(con: Any, schema: str, module: Any, table: str,
            month: dt.date | None) -> str:
    leaf = _leaf(table, month)
    period = f'"{module.PERIOD}"'
    if month is None:
        constraint = f"{period} IS NULL"
        bound = "DEFAULT"
    else:
        lo, hi = month.isoformat(), _next(month).isoformat()
        constraint = (f"{period} IS NOT NULL AND {period} >= DATE '{lo}' "
                      f"AND {period} < DATE '{hi}'")
        bound = f"FOR VALUES FROM ('{lo}') TO ('{hi}')"
    con.execute(f'ALTER TABLE "{schema}".{leaf} ADD CHECK ({constraint})')
    con.execute(f'ALTER TABLE "{schema}".{table} ATTACH PARTITION '
                f'"{schema}".{leaf} {bound}')
    _record(con, schema, table, leaf)
    return leaf


def build(con: Any, schema: str, module: Any, table: str, source: str) -> None:
    select = getattr(module, "build_select_sql", module.select_sql)
    _metadata(con, schema)
    forget(con, schema, table)
    con.execute(module.table_sql(schema, table)
                + f' PARTITION BY RANGE ("{module.PERIOD}")')
    # Discover occupied months once on full migration/build, not on each repair.
    # NULLs go into a constrained default partition, never silently disappear.
    months = con.execute(
        f'SELECT DISTINCT date_trunc(\'month\', "{module.PERIOD}")::date '
        f'FROM {source} WHERE "{module.PERIOD}" IS NOT NULL ORDER BY 1').fetchall()
    for (month,) in months:
        leaf = _leaf(table, month)
        con.execute(f'CREATE TABLE "{schema}".{leaf} AS '
                    + select(source, (month, _next(month))))
        _attach(con, schema, module, table, month)
    leaf = _leaf(table, None)
    null_source = f'(SELECT * FROM {source} WHERE "{module.PERIOD}" IS NULL) AS undated'
    con.execute(f'CREATE TABLE "{schema}".{leaf} AS '
                + select(null_source))
    _attach(con, schema, module, table, None)


def _entries(con: Any, schema: str, table: str) -> list:
    exists = con.execute("SELECT to_regclass(?)",
                         [f'"{schema}".{META}']).fetchone()[0]
    if not exists:
        raise RebuildRequired(f"{table} lacks partition counts; a full rebuild is required")
    entries = con.execute(
        f'''WITH actual AS (
            SELECT c.oid, c.relname, n.nspname FROM pg_inherits i
            JOIN pg_class c ON c.oid = i.inhrelid
            JOIN pg_namespace n ON n.oid = c.relnamespace
            WHERE i.inhparent = CAST(? AS regclass)::oid
        ), recorded AS (
            SELECT leaf_name, leaf_oid, rows, dirty FROM "{schema}".{META}
            WHERE parent_name = ?
        )
        SELECT a.relname, m.rows, m.dirty, a.oid, m.leaf_oid
        FROM actual a FULL OUTER JOIN recorded m
            ON m.leaf_name = a.relname AND m.leaf_oid = a.oid AND a.nspname = ?''',
        [f'"{schema}".{table}', table, schema]).fetchall()
    if not entries or any(oid is None or stored_oid is None or rows is None
                          for _, rows, _, oid, stored_oid in entries):
        raise RebuildRequired(
            f"{table} has mismatched partitions/counts; a full rebuild is required")
    return [(leaf, rows, dirty) for leaf, rows, dirty, _, _ in entries]


def validate(con: Any, schema: str, table: str) -> None:
    """Read-only readiness check using catalogs and metadata, never derived rows.

    Flat sides need no partition ledger. Call under the generation/rebuild locks;
    catch RebuildRequired to invalidate publication and schedule a full repair.
    Dirty entries are allowed *during* staging, but not in published ready data.
    """
    if (partitioned(con, schema, table)
            and any(dirty for _, _, dirty in _entries(con, schema, table))):
        raise RebuildRequired(
            f"{table} has unfinished partition counts; a full rebuild is required")


def repair(con: Any, schema: str, module: Any, table: str, source: str,
           window: tuple[dt.date, dt.date]) -> None:
    select = getattr(module, "build_select_sql", module.select_sql)
    lo, hi = window
    if lo >= hi:
        raise ValueError("Derived repair window must have start < end")
    _entries(con, schema, table)
    month = _month(lo)
    while month < hi:
        end = _next(month)
        leaf = _leaf(table, month)
        exists = con.execute(
            "SELECT 1 FROM pg_inherits WHERE inhparent = CAST(? AS regclass)::oid "
            "AND inhrelid = to_regclass(?)",
            [f'"{schema}".{table}', f'"{schema}".{leaf}']).fetchone()
        if not exists:
            con.execute(f'CREATE TABLE "{schema}".{leaf} '
                        f'(LIKE "{schema}".{table} INCLUDING DEFAULTS INCLUDING CONSTRAINTS)')
            _attach(con, schema, module, table, month)
        start, stop = max(lo, month), min(hi, end)
        con.execute(f'DELETE FROM "{schema}".{leaf} '
                    f'WHERE "{module.PERIOD}" >= ? AND "{module.PERIOD}" < ?',
                    [start, stop])
        con.execute(f'INSERT INTO "{schema}".{leaf} '
                    + select(source, (start, stop)))
        _record(con, schema, table, leaf)
        month = end


def prepare(con: Any, schema: str, module: Any, table: str) -> int:
    entries = _entries(con, schema, table)
    for leaf, _, dirty in entries:
        if dirty:
            count = con.execute(f'SELECT count(*) FROM "{schema}".{leaf}').fetchone()[0]
            con.execute(f'ANALYZE "{schema}".{leaf}')
            con.execute(f'UPDATE "{schema}".{META} SET rows = ?, dirty = false '
                        'WHERE parent_name = ? AND leaf_name = ?', [count, table, leaf])
    # Join against current OIDs: stale metadata cannot inflate a total.
    return sum(int(rows) for _, rows, _ in _entries(con, schema, table))
