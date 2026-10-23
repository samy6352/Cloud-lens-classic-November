"""Opt-in Environment sidecar with explicit schema and bounded maintenance.

Never registered in DERIVED_MODULES or created at startup. Incomplete coverage
keeps the existing raw filter authoritative.
"""
from __future__ import annotations

import argparse
import json
import os
import re
from collections.abc import Iterable, Iterator
from datetime import date, timedelta
from typing import Any

from . import environment, fx_rollup

TABLE = "environment_summary"
COVERAGE_TABLE = "environment_summary_coverage"
BACKFILL_CHUNK_DAYS = 7
LAYOUT_KEY = "environment_summary_layout"
GENERATION_KEY = "environment_summary_generation"
CACHE_REVISION_KEY = "environment_summary_cache_revision"
LAYOUT_VERSION = "1"
MAX_REFRESH_BUDGET_MS = 15000
NONZERO = "FxNonzeroCharges"
READ_TRANSACTION_SQL = "BEGIN TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY"
_IDENTIFIER = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")

DIMENSIONS = (
    "ChargePeriodStart",
    "SubAccountId",
    "Environment",
    "BillingCurrency",
    "PricingCurrency",
    "ExchangeRatePricingToBilling",
    "BillingCostBasis",
    "FxLegacyRatio",
)

SOURCE_COLUMNS = (
    "ChargePeriodStart",
    "SubAccountId",
    "BillingCurrency",
    "PricingCurrency",
    "ExchangeRatePricingToBilling",
    "BillingCostBasis",
    "BilledCost",
    "CostInUsd",
)


def enabled() -> bool:
    value = os.getenv("CLOUDLENS_ENV_SUMMARY", "false").strip().lower()
    if value not in {"true", "false"}:
        raise ValueError("CLOUDLENS_ENV_SUMMARY must be true or false")
    return value == "true"


def _relation(schema: str, name: str) -> str:
    if not _IDENTIFIER.fullmatch(schema) or not _IDENTIFIER.fullmatch(name):
        raise ValueError("Environment summary relation identifiers must be simple SQL names.")
    return f'"{schema}"."{name}"'


def _date(value: date) -> str:
    if type(value) is not date:
        raise TypeError("Environment summary windows require date bounds.")
    return f"DATE '{value.isoformat()}'"


def coverage_window(since_exclusive: date, through_inclusive: date) -> tuple[date, date]:
    """Convert the filter's `period > since AND period <= through` to half-open dates."""
    if type(since_exclusive) is not date or type(through_inclusive) is not date:
        raise TypeError("Environment coverage requires date bounds.")
    if through_inclusive <= since_exclusive:
        raise ValueError("Environment coverage bounds must contain at least one day.")
    return since_exclusive + timedelta(days=1), through_inclusive + timedelta(days=1)


def backfill_windows(start: date, end: date, *,
                     chunk_days: int = BACKFILL_CHUNK_DAYS) -> Iterator[tuple[date, date]]:
    """Yield bounded, contiguous half-open date windows without gaps or overlap."""
    if type(start) is not date or type(end) is not date:
        raise TypeError("Environment backfill bounds must be dates.")
    if start >= end:
        raise ValueError("Environment backfill requires an ordered, non-empty window.")
    if type(chunk_days) is not int or not 1 <= chunk_days <= BACKFILL_CHUNK_DAYS:
        raise ValueError(f"Environment backfill chunks must be 1-{BACKFILL_CHUNK_DAYS} days.")

    lo = start
    while lo < end:
        hi = min(end, lo + timedelta(days=chunk_days))
        yield lo, hi
        lo = hi


def coverage_complete(start: date, end: date, covered: Iterable[date]) -> bool:
    """Whether every day in a half-open interval, including zero-charge days, is certified."""
    if type(start) is not date or type(end) is not date or start >= end:
        return False
    days = set(covered)
    return all(start + timedelta(days=offset) in days
               for offset in range((end - start).days))


def select_sql(source: str, window: tuple[date, date] | None = None) -> str:
    """Aggregate cost and row-level FX provenance at Environment-filter grain."""
    if not _IDENTIFIER.fullmatch(source):
        raise ValueError("Environment summary source must be a simple SQL relation name.")
    if window is not None:
        lo, hi = window
        if type(lo) is not date or type(hi) is not date or lo >= hi:
            raise ValueError("Environment summary windows must be ordered half-open dates.")
        if (hi - lo).days > BACKFILL_CHUNK_DAYS:
            raise ValueError(
                f"Environment refresh windows must contain at most {BACKFILL_CHUNK_DAYS} days.")
        where = (f' WHERE "ChargePeriodStart" >= {_date(lo)}'
                 f' AND "ChargePeriodStart" < {_date(hi)}')
    else:
        where = ""

    projected = environment.project(source, SOURCE_COLUMNS)
    columns = ", ".join(f'"{name}"' for name in SOURCE_COLUMNS)
    fx_ratio = fx_rollup.LEGACY_RATIO
    dimensions = ", ".join(f'"{name}"' for name in DIMENSIONS)
    return (
        f'SELECT {dimensions}, sum("BilledCost") AS "BilledCost", '
        f'count(*) FILTER (WHERE "BilledCost" <> 0) AS "{NONZERO}" '
        f'FROM (SELECT {columns}, "Environment", {fx_ratio} AS "FxLegacyRatio" '
        f'FROM {projected}{where}) AS environment_summary_source '
        f'GROUP BY {dimensions}'
    )


def table_sql(schema: str) -> str:
    """DDL text for explicit maintenance tooling; never run during app startup."""
    relation = _relation(schema, TABLE)
    columns = (
        '"ChargePeriodStart" date NOT NULL',
        '"SubAccountId" text',
        '"Environment" text NOT NULL',
        '"BillingCurrency" text',
        '"PricingCurrency" text',
        '"ExchangeRatePricingToBilling" double precision',
        '"BillingCostBasis" text',
        '"FxLegacyRatio" double precision',
        '"BilledCost" double precision',
        f'"{NONZERO}" bigint NOT NULL',
    )
    return (f'CREATE TABLE IF NOT EXISTS {relation} '
            f'({", ".join(columns)})')


def coverage_sql(schema: str) -> str:
    """DDL text for per-day completeness, including days with no source rows."""
    relation = _relation(schema, COVERAGE_TABLE)
    return (
        f'CREATE TABLE IF NOT EXISTS {relation} ('
        '"ChargePeriodStart" date PRIMARY KEY, '
        '"built_at" timestamp with time zone NOT NULL DEFAULT now(), '
        '"dirty" boolean NOT NULL DEFAULT false)'
    )


def index_sql(schema: str) -> str:
    """Index the date cut first, then the caller's permitted subscription scope."""
    relation = _relation(schema, TABLE)
    return (
        f'CREATE INDEX IF NOT EXISTS {TABLE}_period_scope_idx '
        f'ON {relation} ("ChargePeriodStart", lower("SubAccountId"))'
    )


def refresh_statements(schema: str, window: tuple[date, date]) -> tuple[str, ...]:
    """Statements to replace one bounded chunk and certify every day in it.

    Execute all statements in one transaction while holding `jobs.lock_generation`.
    The caller commits only after both summary rows and coverage have been written.
    """
    lo, hi = window
    if type(lo) is not date or type(hi) is not date or lo >= hi:
        raise ValueError("Environment summary windows must be ordered half-open dates.")
    table = _relation(schema, TABLE)
    coverage = _relation(schema, COVERAGE_TABLE)
    start, stop = _date(lo), _date(hi)
    columns = ", ".join(f'"{name}"' for name in (*DIMENSIONS, "BilledCost", NONZERO))
    source = _relation(schema, "costs")
    return (
        f'DELETE FROM {table} WHERE "ChargePeriodStart" >= {start} '
        f'AND "ChargePeriodStart" < {stop}',
        f'INSERT INTO {table} ({columns}) WITH costs AS (SELECT * FROM {source}) '
        f'{select_sql("costs", window)}',
        f'DELETE FROM {coverage} WHERE "ChargePeriodStart" >= {start} '
        f'AND "ChargePeriodStart" < {stop}',
        f'INSERT INTO {coverage} ("ChargePeriodStart") '
        f'SELECT day::date FROM generate_series({start}, {stop} - INTERVAL \'1 day\', '
        "INTERVAL '1 day') AS days(day)",
    )


def invalidate_sql(schema: str,
                   windows: Iterable[tuple[date, date]] | None) -> str:
    """Invalidate changed dates in the source-write transaction; None means unknown."""
    coverage = _relation(schema, COVERAGE_TABLE)
    if windows is None:
        return f"DELETE FROM {coverage}"
    ranges = list(windows)
    if not ranges:
        raise ValueError("Environment coverage invalidation needs at least one window.")
    predicates = []
    for lo, hi in ranges:
        if type(lo) is not date or type(hi) is not date or lo >= hi:
            raise ValueError("Environment invalidation windows must be ordered half-open dates.")
        predicates.append(
            f'("ChargePeriodStart" >= {_date(lo)} AND "ChargePeriodStart" < {_date(hi)})')
    return f'UPDATE {coverage} SET dirty=true WHERE {" OR ".join(predicates)}'


def coverage_complete_sql(schema: str, start: date, end: date) -> str:
    """Fast boolean check for complete half-open date coverage."""
    if type(start) is not date or type(end) is not date or start >= end:
        raise ValueError("Environment coverage needs ordered half-open date bounds.")
    coverage = _relation(schema, COVERAGE_TABLE)
    return (
        "SELECT NOT EXISTS (SELECT 1 FROM generate_series("
        f"{_date(start)}, {_date(end)} - INTERVAL '1 day', INTERVAL '1 day') "
        f"AS required(day) LEFT JOIN {coverage} AS covered "
        'ON covered."ChargePeriodStart" = required.day::date AND NOT covered.dirty '
        'WHERE covered."ChargePeriodStart" IS NULL) AS complete'
    )


def ranked_sql(source: str, start: date, end: date, *,
               billing_factor: str | None = None, target_currency: str | None = None,
               scoped: bool = False,
               limit: int = 50) -> str:
    """Read ranked Environment values from a fully covered summary interval.

    `billing_factor` is the trusted SQL expression returned by the warehouse's
    existing `_currency_sql(..., legacy_ratio='"FxLegacyRatio"')`. Supply the
    corresponding target currency when conversion is requested; otherwise values
    remain separated into native currency buckets.

    The caller must first run `coverage_complete_sql` in a transaction begun with
    READ_TRANSACTION_SQL, then run this statement only if coverage is complete.
    If coverage is missing, use the existing raw-cost fallback before ending that
    same transaction. Repeatable read keeps the coverage decision, summary, and
    fallback on one MVCC snapshot while a source writer invalidates coverage.
    """
    relation = _relation(*source.split(".", 1)) if "." in source else _relation(
        "public", source)
    if type(start) is not date or type(end) is not date or start >= end:
        raise ValueError("Environment ranking needs ordered half-open date bounds.")
    if type(limit) is not int or not 1 <= limit <= 200:
        raise ValueError("Environment ranking limit must be between 1 and 200.")
    if billing_factor is not None:
        if not billing_factor.strip() or not target_currency or not re.fullmatch(
                r"[A-Z]{3}", target_currency):
            raise ValueError("A trusted FX expression and ISO target currency are required.")
    elif target_currency is not None:
        raise ValueError("A target currency requires a billing conversion expression.")

    currency = (f"'{target_currency}'::text AS currency" if billing_factor
                else '"BillingCurrency" AS currency')
    currency_group = "currency" if billing_factor else '"BillingCurrency"'
    cost = (f'sum("BilledCost" * ({billing_factor}))'
            if billing_factor else 'sum("BilledCost")')
    where = (f'"ChargePeriodStart" >= {_date(start)} '
             f'AND "ChargePeriodStart" < {_date(end)} AND "Environment" <> \'\'')
    if scoped:
        where += ' AND lower("SubAccountId") = ANY(ARRAY(SELECT id FROM _scope))'
    return (
        f'WITH grouped AS (SELECT "Environment" AS env, {currency}, {cost} AS c '
        f'FROM {relation} WHERE {where} GROUP BY "Environment", '
        f'{currency_group}), '
        'ranked AS (SELECT env, currency, c, dense_rank() OVER '
        '(ORDER BY lower(env)) AS rn FROM grouped) '
        f'SELECT env, currency, c FROM ranked WHERE rn <= {limit} '
        'ORDER BY currency, c DESC'
    )


def _marker(con: Any, schema: str, key: str) -> str | None:
    row = con.execute(
        f"SELECT value FROM {_relation(schema, 'meta')} WHERE key=?", [key]).fetchone()
    return row[0] if row else None


def _put_marker(con: Any, schema: str, key: str, value: str) -> None:
    con.execute(
        f"INSERT INTO {_relation(schema, 'meta')} (key,value,updated_at) VALUES (?,?,now()) "
        "ON CONFLICT (key) DO UPDATE SET value=excluded.value,updated_at=excluded.updated_at",
        [key, value])


def _require_layout(con: Any, schema: str) -> None:
    if _marker(con, schema, LAYOUT_KEY) != LAYOUT_VERSION:
        raise RuntimeError("Environment summary requires its explicit version-1 migration.")


def migrate(con: Any, schema: str) -> bool:
    """Explicit operator DDL; caller owns one transaction and the generation lock."""
    version = _marker(con, schema, LAYOUT_KEY)
    if version is not None:
        _require_layout(con, schema)
        for name, columns in (
            (TABLE, (*DIMENSIONS, "BilledCost", NONZERO)),
            (COVERAGE_TABLE, ("ChargePeriodStart", "built_at", "dirty")),
        ):
            actual = con.execute(f"SELECT * FROM {_relation(schema, name)} LIMIT 0").description
            if tuple(column[0] for column in actual) != columns:
                raise RuntimeError("Environment summary schema does not match version 1.")
        return False
    for name in (TABLE, COVERAGE_TABLE):
        if con.execute("SELECT to_regclass(?)", [_relation(schema, name)]).fetchone()[0]:
            raise RuntimeError("Refusing to adopt unversioned Environment summary tables.")
    for sql in (table_sql(schema), coverage_sql(schema), index_sql(schema)):
        con.execute(sql)
    _put_marker(con, schema, LAYOUT_KEY, LAYOUT_VERSION)
    _put_marker(con, schema, GENERATION_KEY, "-1")
    _put_marker(con, schema, CACHE_REVISION_KEY, "0")
    return True


def invalidate(con: Any, windows: Iterable[tuple[date, date]] | None,
               source_generation: int) -> None:
    """Called in the cost writer's transaction before advancing its generation."""
    schema = con._owner.schema
    _require_layout(con, schema)
    if _marker(con, schema, GENERATION_KEY) != str(source_generation):
        windows = None
    con.execute(invalidate_sql(schema, windows))
    _put_marker(con, schema, GENERATION_KEY, str(source_generation + 1))


def refresh(store: Any, window: tuple[date, date], *,
            budget_ms: int = MAX_REFRESH_BUDGET_MS) -> None:
    """Certify at most seven days, atomically and under the source-generation lock."""
    if not enabled():
        raise RuntimeError("Environment summary maintenance requires CLOUDLENS_ENV_SUMMARY=true.")
    if type(budget_ms) is not int or not 1 <= budget_ms <= MAX_REFRESH_BUDGET_MS:
        raise ValueError(f"Environment refresh budget must be 1-{MAX_REFRESH_BUDGET_MS} ms.")
    statements = refresh_statements(store.schema, window)
    from . import jobs

    with store.connect() as con, con.raw.transaction():
        con.execute("SELECT set_config('statement_timeout', ?, true)", [str(budget_ms)])
        con.execute("SELECT set_config('lock_timeout', '5000', true)")
        jobs.lock_generation(con)
        _require_layout(con, store.schema)
        source = _marker(con, store.schema, jobs.GENERATION_KEY) or "0"
        if _marker(con, store.schema, GENERATION_KEY) != source:
            con.execute(invalidate_sql(store.schema, None))
        for sql in statements:
            con.execute(sql)
        _put_marker(con, store.schema, GENERATION_KEY, source)
        revision = int(_marker(con, store.schema, CACHE_REVISION_KEY) or "0")
        _put_marker(con, store.schema, CACHE_REVISION_KEY, str(revision + 1))


def cache_revision(store: Any) -> str:
    """A backfill changes filter availability without changing raw cost generation."""
    if not enabled():
        return "off"
    with store.connect() as con:
        values = dict(con.execute(
            f"SELECT key,value FROM {_relation(store.schema, 'meta')} WHERE key IN (?,?)",
            [LAYOUT_KEY, CACHE_REVISION_KEY]).fetchall())
    return f"on:{values.get(LAYOUT_KEY, 'missing')}:{values.get(CACHE_REVISION_KEY, '0')}"


def refresh_pending(store: Any, *, max_chunks: int = 4) -> int:
    """Repair previously certified, changed dates only; never auto-backfill history."""
    if not enabled():
        return 0
    if type(max_chunks) is not int or not 1 <= max_chunks <= 4:
        raise ValueError("Environment incremental refresh requires 1-4 chunks.")
    with store.connect() as con:
        _require_layout(con, store.schema)
        days = [row[0] for row in con.execute(
            f'SELECT "ChargePeriodStart" FROM {_relation(store.schema, COVERAGE_TABLE)} '
            'WHERE dirty ORDER BY "ChargePeriodStart" LIMIT ?',
            [max_chunks * BACKFILL_CHUNK_DAYS]).fetchall()]
    windows: list[tuple[date, date]] = []
    for day in days:
        if (windows and day == windows[-1][1]
                and (day - windows[-1][0]).days < BACKFILL_CHUNK_DAYS):
            windows[-1] = windows[-1][0], day + timedelta(days=1)
        else:
            windows.append((day, day + timedelta(days=1)))
    for window in windows[:max_chunks]:
        refresh(store, window)
    return min(len(windows), max_chunks)


def _window(hi: Any, days: int) -> tuple[date, date]:
    from . import reporting

    bounds = reporting.month_bounds()
    through = date.fromisoformat(str(hi)[:10])
    return (bounds[0], bounds[1] + timedelta(days=1)) if bounds else coverage_window(
        through - timedelta(days=days), through)


def _billing_factor(warehouse: Any, currency: str, *, stored: bool) -> str:
    from . import currency as fx

    target = currency.upper()
    rate = fx.rate_to(target)
    if rate is None:
        raise ValueError(f"No authoritative exchange rate is available for {target}.")
    factor, _ = warehouse._currency_sql(
        rate, target, legacy_ratio=f'"{fx_rollup.RATIO}"' if stored else None)
    return factor


def filter_rows(reader: Any, warehouse: Any, hi: Any, days: int, *,
                scope: bool, currency: str = "") -> list[dict[str, Any]] | None:
    """Use a certified summary only inside the caller's repeatable-read snapshot."""
    from . import jobs

    con, schema = reader._con, warehouse._rollup_schema
    _require_layout(con, schema)
    source = _marker(con, schema, jobs.GENERATION_KEY) or "0"
    if _marker(con, schema, GENERATION_KEY) != source:
        return None
    start, end = _window(hi, days)
    if not con.execute(coverage_complete_sql(schema, start, end)).fetchone()[0]:
        return None
    factor = None
    target = currency.upper() or None
    if target:
        factor = _billing_factor(warehouse, target, stored=True)
        where = (f'"ChargePeriodStart" >= {_date(start)} AND "ChargePeriodStart" < {_date(end)} '
                 f'AND "{NONZERO}" > 0 AND ({factor}) IS NULL')
        if scope:
            where += ' AND lower("SubAccountId") = ANY(ARRAY(SELECT id FROM _scope))'
        if con.execute(
                f'SELECT 1 FROM {_relation(schema, TABLE)} WHERE {where} LIMIT 1').fetchone():
            raise ValueError("Environment summary contains charges without authoritative FX.")
    rows = reader.rows(ranked_sql(
        f"{schema}.{TABLE}", start, end, scoped=scope,
        billing_factor=factor, target_currency=target), 10000)
    return [{"d": "env", "v": row["env"], "currency": row["currency"], "c": row["c"]}
            for row in rows]


def raw_currency_rows(reader: Any, warehouse: Any, hi: Any, days: int,
                      currency: str) -> list[dict[str, Any]]:
    """Converted fallback on the same scoped snapshot, without an eager full-history scan."""
    start, end = _window(hi, days)
    target = currency.upper()
    factor = _billing_factor(warehouse, target, stored=False)
    where = f'"ChargePeriodStart" >= {_date(start)} AND "ChargePeriodStart" < {_date(end)}'
    if reader.rows(
            f'SELECT 1 AS missing FROM costs WHERE {where} '
            f'AND "BilledCost" <> 0 AND ({factor}) IS NULL LIMIT 1', 1):
        raise ValueError("Environment source contains charges without authoritative FX.")
    source = environment.project("costs", [
        "ChargePeriodStart", "BilledCost", "BillingCurrency", "PricingCurrency",
        "ExchangeRatePricingToBilling", "BillingCostBasis", "CostInUsd"])
    return reader.rows(
        f'WITH recent AS MATERIALIZED (SELECT "Environment" AS env, '
        f'"BilledCost" * ({factor}) AS cost FROM {source} WHERE {where}), '
        'grouped AS (SELECT env, sum(cost) AS c FROM recent WHERE env <> \'\' GROUP BY env), '
        'ranked AS (SELECT env, c, dense_rank() OVER (ORDER BY lower(env)) AS rn FROM grouped) '
        f'SELECT \'env\' AS d, env AS v, \'{target}\'::text AS currency, c '
        'FROM ranked WHERE rn <= 50 ORDER BY c DESC', 10000)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--migrate", action="store_true", help="Explicit sidecar DDL only.")
    parser.add_argument("--start", type=date.fromisoformat)
    parser.add_argument("--end", type=date.fromisoformat, help="Exclusive date bound.")
    parser.add_argument("--max-chunks", type=int, choices=range(1, 17), default=1)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--acknowledgement", choices=["environment-summary-only"])
    args = parser.parse_args()
    if args.migrate:
        if args.start or args.end:
            parser.error("--migrate cannot be combined with backfill dates.")
        windows = []
    else:
        if args.start is None or args.end is None:
            parser.error("Backfill requires --start and exclusive --end.")
        try:
            windows = list(backfill_windows(args.start, args.end))
        except ValueError as exc:
            parser.error(str(exc))
        if len(windows) > args.max_chunks:
            parser.error("Requested interval exceeds --max-chunks; split or explicitly raise it.")
    if not args.apply:
        print(json.dumps({"apply": False, "migrate": args.migrate, "windows": [
            [lo.isoformat(), hi.isoformat()] for lo, hi in windows]}))
        return
    if args.acknowledgement is None:
        parser.error("--apply requires --acknowledgement environment-summary-only.")
    from . import jobs
    from .store import PostgresStore

    store = PostgresStore()
    try:
        if args.migrate:
            with store.connect() as con, con.raw.transaction():
                con.execute("SELECT set_config('lock_timeout', '5000', true)")
                jobs.lock_generation(con)
                migrate(con, store.schema)
        else:
            for window in windows:
                refresh(store, window)
    finally:
        store.close()
    print(json.dumps({"apply": True, "migrate": args.migrate, "chunks": len(windows)}))


if __name__ == "__main__":
    main()
