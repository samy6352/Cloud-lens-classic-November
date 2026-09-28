"""Structural retention-scale checks; small fixtures do not benchmark throughput."""
from datetime import date

import pytest

pytestmark = pytest.mark.postgres
FEB = (date(2024, 2, 1), date(2024, 3, 1))
NOV = (date(2025, 11, 1), date(2025, 12, 1))


def _insert(wh, dates):
    from app.warehouse import COLUMNS, NUMERIC, insert_rows

    rows = []
    for day in dates:
        values = {
            "ChargePeriodStart": day, "SubAccountId": "sub-a", "SubAccountName": "A",
            "ResourceId": "shared-resource", "ResourceName": "A",
            "BillingCurrency": "USD", "BillingCostBasis": "FOCUS",
            "ServiceName": "Storage", "ServiceFamily": "Storage",
            "BilledCost": 10.0, "InvoiceCost": 10.0, "EffectiveCost": 10.0,
        }
        rows.append([values.get(c, 0.0 if c in NUMERIC else None) for c in COLUMNS])
    with wh.connect() as con:
        insert_rows(con, rows)


@pytest.fixture
def loaded(postgres_warehouse, monkeypatch):
    monkeypatch.setenv("CLOUDLENS_DERIVED_LAYOUT", "monthly")
    wh = postgres_warehouse
    _insert(wh, [date(year, month, day)
                 for year in (2024, 2025) for month in range(1, 13) for day in (1, 15)]
            + [date(2024, 2, 29), None])
    wh.refresh_rollup(strict=True)
    return wh


def _rebuild(wh, *, windows=None, con=None):
    from app import derived
    from app.warehouse import DERIVED_MODULES

    if con is not None:
        return derived.rebuild(con, wh._rollup_schema, DERIVED_MODULES,
                               wh._pg_table, windows=windows)
    with wh.connect() as connection:
        return _rebuild(wh, windows=windows, con=connection)


def _assert_exact(wh, counts=None):
    from app import derived, derived_partitions
    from app.warehouse import DERIVED_MODULES

    with wh.connect() as con:
        for module in DERIVED_MODULES:
            derived.validate_storage(con, wh._rollup_schema, module)
            actual = f'"{wh._rollup_schema}".{module.TABLE}'
            dependency = getattr(module, "SOURCE_TABLE", None)
            source = f'"{wh._rollup_schema}".{dependency}' if dependency else wh._pg_table
            expected = module.select_sql(source)
            assert con.execute(
                f'SELECT * FROM ((SELECT * FROM {actual} EXCEPT ALL ({expected})) '
                f'UNION ALL (({expected}) EXCEPT ALL SELECT * FROM {actual})) differences'
            ).fetchall() == []
            table = derived.active_table(con, wh._rollup_schema, module)
            entries = derived_partitions._entries(con, wh._rollup_schema, table)
            total = con.execute(f"SELECT count(*) FROM {actual}").fetchone()[0]
            assert sum(rows for _, rows, _ in entries) == total
            assert not any(dirty for _, _, dirty in entries)
            if counts is not None:
                assert counts[module.TABLE] == total
            for leaf, rows, _ in entries:
                assert con.execute(
                    f'SELECT count(*) FROM "{wh._rollup_schema}".{leaf}'
                ).fetchone()[0] == rows


def _snapshot(wh, excluded=()):
    from app import derived
    from app.warehouse import DERIVED_MODULES

    result = {}
    with wh.connect() as con:
        for module in DERIVED_MODULES:
            table = derived.active_table(con, wh._rollup_schema, module)
            leaves = con.execute(
                "SELECT c.oid, c.relname, c.relfilenode FROM pg_inherits i "
                "JOIN pg_class c ON c.oid = i.inhrelid "
                "WHERE i.inhparent = CAST(? AS regclass)::oid ORDER BY c.relname",
                [f'"{wh._rollup_schema}".{table}']).fetchall()
            for oid, leaf, node in leaves:
                if any(leaf.endswith(suffix) for suffix in excluded):
                    continue
                rows = con.execute(
                    f'SELECT ctid::text, xmin::text, to_jsonb(t)::text '
                    f'FROM "{wh._rollup_schema}".{leaf} t ORDER BY ctid').fetchall()
                result[leaf] = (oid, node, rows)
    return result


class Recorded:
    def __init__(self, con):
        self.con = con
        self.sql = []

    def execute(self, statement, params=None):
        self.sql.append(statement)
        return self.con.execute(statement, params)

    def __getattr__(self, key):
        return getattr(self.con, key)


def test_full_matches_flat_shape_types_null_dates_and_exact_distinct(loaded):
    from app import derived, derived_partitions, presence
    from app.warehouse import DERIVED_MODULES

    _assert_exact(loaded)
    with loaded.connect() as con:
        for module in DERIVED_MODULES:
            table = derived.active_table(con, loaded._rollup_schema, module)
            assert derived_partitions.partitioned(con, loaded._rollup_schema, table)
            assert len(derived_partitions._entries(con, loaded._rollup_schema, table)) == 25
            con.execute(module.table_sql(loaded._rollup_schema, "declared"))
            def types(name):
                return con.execute(
                    "SELECT attname, atttypid, atttypmod, attnotnull FROM pg_attribute "
                    "WHERE attrelid = CAST(? AS regclass) AND attnum > 0 AND NOT attisdropped "
                    "ORDER BY attnum", [f'"{loaded._rollup_schema}".{name}']).fetchall()
            assert types(table) == types("declared")
            con.execute(f'DROP TABLE "{loaded._rollup_schema}".declared')
        assert con.execute(f'SELECT count(DISTINCT "ResourceId") FROM {presence.TABLE}'
                           ).fetchone()[0] == 1
        assert con.execute(f'SELECT count(*) FROM {presence.TABLE} '
                           'WHERE "ChargePeriodStart" IS NULL').fetchone()[0] == 1


def test_explain_actually_prunes_to_one_month(loaded):
    from app.warehouse import DERIVED_MODULES

    def relations(plan):
        return ({plan["Relation Name"]} if "Relation Name" in plan else set()).union(
            *(relations(child) for child in plan.get("Plans", [])))

    with loaded.connect() as con:
        for module in DERIVED_MODULES:
            plan = con.execute(
                f'EXPLAIN (ANALYZE, FORMAT JSON) SELECT * FROM "{loaded._rollup_schema}".'
                f'{module.TABLE} WHERE "ChargePeriodStart" >= ? AND "ChargePeriodStart" < ?',
                list(FEB)).fetchone()[0][0]["Plan"]
            leaves = relations(plan)
            assert len(leaves) == 1
            assert next(iter(leaves)).endswith("_202402")


@pytest.mark.parametrize("windows", [[FEB], [FEB, NOV]])
def test_repairs_only_touch_intersecting_months_and_bound_maintenance(loaded, windows):
    suffixes = [lo.strftime("%Y%m") for lo, _ in windows]
    before = _snapshot(loaded, suffixes)
    with loaded.connect() as con:
        for lo, hi in windows:
            con.execute('UPDATE costs SET "BilledCost" = 99 '
                        'WHERE "ChargePeriodStart" >= ? AND "ChargePeriodStart" < ?', [lo, hi])
        recorded = Recorded(con)
        counts = _rebuild(loaded, windows=windows, con=recorded)
    assert _snapshot(loaded, suffixes) == before
    maintenance = [s for s in recorded.sql
                   if s.startswith(("SELECT count(*)", "ANALYZE", "DELETE FROM"))
                   and "_derived_partition_counts" not in s]
    assert maintenance
    assert all(any("_" + suffix in s for suffix in suffixes) for s in maintenance)
    _assert_exact(loaded, counts)


def test_partial_leap_month_preserves_outside_rows_and_nulls(loaded):
    before = _snapshot(loaded, ["202402"])
    with loaded.connect() as con:
        con.execute('UPDATE costs SET "BilledCost" = 42 WHERE "ChargePeriodStart" = ?',
                    [date(2024, 2, 29)])
    counts = _rebuild(loaded, windows=[(date(2024, 2, 29), date(2024, 3, 1))])
    assert _snapshot(loaded, ["202402"]) == before
    _assert_exact(loaded, counts)
    with loaded.connect() as con:
        assert con.execute('SELECT "BilledCost" FROM costs_rollup '
                           'WHERE "ChargePeriodStart" = ?', [date(2024, 2, 1)]
                           ).fetchone()[0] == 10


def test_cross_month_window_preserves_both_partial_months(loaded):
    before = _snapshot(loaded, ["202402", "202403"])
    window = (date(2024, 2, 15), date(2024, 3, 15))
    with loaded.connect() as con:
        con.execute('UPDATE costs SET "BilledCost" = 73 WHERE "ChargePeriodStart" >= ? '
                    'AND "ChargePeriodStart" < ?', list(window))
    _assert_exact(loaded, _rebuild(loaded, windows=[window]))
    assert _snapshot(loaded, ["202402", "202403"]) == before


def test_counts_stay_exact_after_delete_to_zero_new_month_and_retry(loaded):
    with loaded.connect() as con:
        con.execute('DELETE FROM costs WHERE "ChargePeriodStart" >= ? '
                    'AND "ChargePeriodStart" < ?', list(FEB))
    _assert_exact(loaded, _rebuild(loaded, windows=[FEB]))
    _insert(loaded, [date(2026, 1, 1), date(2026, 1, 31)])
    january = (date(2026, 1, 1), date(2026, 2, 1))
    counts = _rebuild(loaded, windows=[january])
    _assert_exact(loaded, counts)
    assert _rebuild(loaded, windows=[january, FEB]) == counts
    _assert_exact(loaded, counts)


def test_later_publication_failure_rolls_back_every_module_and_counts(loaded, monkeypatch):
    from app import derived
    from app.warehouse import DERIVED_MODULES

    before = _snapshot(loaded)
    original = derived.publish

    def fail(con, schema, module, table):
        if module == DERIVED_MODULES[-1]:
            raise RuntimeError("later publication failed")
        original(con, schema, module, table)

    monkeypatch.setattr(derived, "publish", fail)
    with pytest.raises(RuntimeError, match="later publication failed"):
        _rebuild(loaded)
    assert _snapshot(loaded) == before
    _assert_exact(loaded)


def test_later_repair_failure_rolls_back_all_rows_and_metadata(loaded, monkeypatch):
    from app import derived
    from app.warehouse import DERIVED_MODULES

    before = _snapshot(loaded)
    original = derived.prepare

    def fail(con, schema, module, table):
        if module == DERIVED_MODULES[-1]:
            raise RuntimeError("later preparation failed")
        return original(con, schema, module, table)

    monkeypatch.setattr(derived, "prepare", fail)
    with pytest.raises(RuntimeError, match="later preparation failed"):
        _rebuild(loaded, windows=[FEB, NOV])
    assert _snapshot(loaded) == before
    _assert_exact(loaded)


def test_opt_in_migrates_only_on_full_build_and_preserves_dependent_view(
        postgres_warehouse, monkeypatch):
    from app import derived, derived_partitions, rollup

    wh = postgres_warehouse
    monkeypatch.setenv("CLOUDLENS_DERIVED_LAYOUT", "flat")
    _insert(wh, [date(2024, 2, 29)])
    wh.refresh_rollup(strict=True)
    with wh.connect() as con:
        table = derived.active_table(con, wh._rollup_schema, rollup)
        assert not derived_partitions.partitioned(con, wh._rollup_schema, table)
        derived.validate_storage(con, wh._rollup_schema, rollup)
        con.execute(f'CREATE TEMP VIEW retained_scope AS SELECT * '
                    f'FROM "{wh._rollup_schema}".costs_rollup')
        expected = con.execute("SELECT * FROM retained_scope").fetchall()
        monkeypatch.setenv("CLOUDLENS_DERIVED_LAYOUT", "monthly")
        assert not derived.ensure(con, wh._rollup_schema, rollup)
        assert con.execute("SELECT * FROM retained_scope").fetchall() == expected
        _rebuild(wh, windows=[FEB], con=con)
        assert not derived_partitions.partitioned(con, wh._rollup_schema, table)
        _rebuild(wh, con=con)
        assert con.execute("SELECT * FROM retained_scope").fetchall() == expected
        con.execute("DROP VIEW retained_scope")
    _assert_exact(wh)


def test_failed_migration_keeps_flat_generation_and_scoped_view(
        postgres_warehouse, monkeypatch):
    from app import derived, derived_partitions
    from app.warehouse import DERIVED_MODULES

    wh = postgres_warehouse
    monkeypatch.setenv("CLOUDLENS_DERIVED_LAYOUT", "flat")
    _insert(wh, [date(2024, 2, 29)])
    wh.refresh_rollup(strict=True)
    original = derived.publish

    def fail(con, schema, module, table):
        if module == DERIVED_MODULES[-1]:
            raise RuntimeError("migration failed")
        original(con, schema, module, table)

    with wh.connect() as con:
        con.execute(f'CREATE TEMP VIEW retained_scope AS SELECT * '
                    f'FROM "{wh._rollup_schema}".costs_rollup')
        before = con.execute("SELECT * FROM retained_scope").fetchall()
        monkeypatch.setenv("CLOUDLENS_DERIVED_LAYOUT", "monthly")
        monkeypatch.setattr(derived, "publish", fail)
        with pytest.raises(RuntimeError, match="migration failed"):
            _rebuild(wh, con=con)
        assert con.execute("SELECT * FROM retained_scope").fetchall() == before
        for module in DERIVED_MODULES:
            table = derived.active_table(con, wh._rollup_schema, module)
            assert not derived_partitions.partitioned(con, wh._rollup_schema, table)
        con.execute("DROP VIEW retained_scope")
    monkeypatch.setattr(derived, "publish", original)
    _assert_exact(wh, _rebuild(wh))


def test_full_build_uses_ctas_not_whole_history_insert(loaded):
    from app.warehouse import DERIVED_MODULES

    with loaded.connect() as con:
        recorded = Recorded(con)
        _rebuild(loaded, con=recorded)
    builds = [s for s in recorded.sql if s.startswith("CREATE TABLE ") and " AS " in s]
    assert len(builds) == 25 * len(DERIVED_MODULES)
    assert not any(s.startswith("INSERT INTO") and "_derived_partition_counts" not in s
                   for s in recorded.sql)


def test_failure_keeps_published_generation_metadata_atomic(loaded, monkeypatch):
    from app import derived, jobs

    with loaded.connect() as con:
        old = jobs._generation_pair(con)
        jobs.advance_generation(con)
        dirty = jobs._generation_pair(con)
        assert dirty != old
        before = con.execute("SELECT key, value FROM meta ORDER BY key").fetchall()
    original = derived.publish

    def fail(con, schema, module, table):
        if module.TABLE == "billing_rollup":
            raise RuntimeError("publication interrupted")
        original(con, schema, module, table)

    monkeypatch.setattr(derived, "publish", fail)
    with pytest.raises(RuntimeError, match="publication interrupted"):
        loaded.refresh_rollup(strict=True)
    with loaded.connect() as con:
        assert jobs._generation_pair(con) == dirty
        assert con.execute("SELECT key, value FROM meta ORDER BY key").fetchall() == before
    monkeypatch.setattr(derived, "publish", original)
    loaded.refresh_rollup(strict=True)
    with loaded.connect() as con:
        generation, published = jobs._generation_pair(con)
        assert generation == published
    _assert_exact(loaded)


def test_repeat_builds_retire_old_leaves_indexes_and_counts(loaded, monkeypatch):
    from app import derived, derived_partitions
    from app.warehouse import DERIVED_MODULES

    # Disabling conversion must not accidentally flatten a converted installation.
    monkeypatch.setenv("CLOUDLENS_DERIVED_LAYOUT", "flat")
    for _ in range(2):
        _assert_exact(loaded, _rebuild(loaded))
        with loaded.connect() as con:
            for module in DERIVED_MODULES:
                active = derived.active_table(con, loaded._rollup_schema, module)
                assert derived_partitions.partitioned(con, loaded._rollup_schema, active)
                stale = derived._inactive(con, loaded._rollup_schema, module)
                assert con.execute("SELECT to_regclass(?)",
                                   [f'"{loaded._rollup_schema}".{stale}']).fetchone()[0] is None
                assert con.execute(
                    f'SELECT count(*) FROM "{loaded._rollup_schema}".'
                    f'{derived_partitions.META} WHERE parent_name = ?', [stale]
                ).fetchone()[0] == 0
                assert con.execute(
                    "SELECT count(*) FROM pg_class c JOIN pg_namespace n "
                    "ON n.oid = c.relnamespace WHERE n.nspname = ? "
                    "AND starts_with(c.relname, ?)",
                    [loaded._rollup_schema, stale + "_"]).fetchone()[0] == 0


def test_empty_full_build_then_first_month(postgres_warehouse, monkeypatch):
    monkeypatch.setenv("CLOUDLENS_DERIVED_LAYOUT", "monthly")
    wh = postgres_warehouse
    counts = _rebuild(wh)
    assert set(counts.values()) == {0}
    _assert_exact(wh, counts)
    _insert(wh, [date(2024, 2, 29)])
    _assert_exact(wh, _rebuild(wh, windows=[FEB]))


@pytest.mark.parametrize("damage", [
    "missing-ledger", "untracked-leaf", "dropped-leaf", "detached-leaf", "wrong-oid",
])
def test_missing_metadata_fails_closed_then_full_rebuild_recovers(loaded, damage):
    from app import derived, derived_partitions, rollup

    with loaded.connect() as con:
        table = derived.active_table(con, loaded._rollup_schema, rollup)
        leaf = f"{table}_202402"
        if damage == "missing-ledger":
            con.execute(f'DROP TABLE "{loaded._rollup_schema}".{derived_partitions.META}')
        elif damage == "untracked-leaf":
            con.execute(f'DELETE FROM "{loaded._rollup_schema}".{derived_partitions.META} '
                        "WHERE leaf_name LIKE '%202402'")
        elif damage == "dropped-leaf":
            con.execute(f'DROP TABLE "{loaded._rollup_schema}".{leaf}')
        elif damage == "detached-leaf":
            con.execute(f'ALTER TABLE "{loaded._rollup_schema}".{table} '
                        f'DETACH PARTITION "{loaded._rollup_schema}".{leaf}')
        else:
            con.execute(f'UPDATE "{loaded._rollup_schema}".{derived_partitions.META} '
                        'SET leaf_oid = 0 WHERE leaf_name = ?', [leaf])
        recorded = Recorded(con)
        with pytest.raises(derived.RebuildRequired, match="full rebuild is required"):
            derived.validate_storage(recorded, loaded._rollup_schema, rollup)
        assert all(s.startswith(("SELECT", "WITH")) for s in recorded.sql)
        assert not any("count(*)" in s for s in recorded.sql)
        assert not any(f'FROM "{loaded._rollup_schema}".{table}' in s for s in recorded.sql)
        # Recovery can catch the application exception without aborting its transaction.
        assert con.execute("SELECT 1").fetchone()[0] == 1
    before = _snapshot(loaded)
    with pytest.raises(derived.RebuildRequired, match="full rebuild is required"):
        _rebuild(loaded, windows=[NOV])
    assert _snapshot(loaded) == before
    _assert_exact(loaded, _rebuild(loaded))
    with loaded.connect() as con:
        assert con.execute("SELECT to_regclass(?)",
                           [f'"{loaded._rollup_schema}".{leaf}']).fetchone()[0] is None
    # A detached leaf must not linger and block reuse of the retired side's names.
    _assert_exact(loaded, _rebuild(loaded))


def test_readiness_rejects_unfinished_counts_without_reading_derived_rows(loaded):
    from app import derived, derived_partitions, rollup

    with loaded.connect() as con:
        table = derived.active_table(con, loaded._rollup_schema, rollup)
        con.execute(f'UPDATE "{loaded._rollup_schema}".{derived_partitions.META} '
                    'SET dirty = true WHERE parent_name = ?', [table])
        recorded = Recorded(con)
        with pytest.raises(derived.RebuildRequired, match="unfinished partition counts"):
            derived.validate_storage(recorded, loaded._rollup_schema, rollup)
        assert all(s.startswith(("SELECT", "WITH")) for s in recorded.sql)
        assert not any("count(*)" in s for s in recorded.sql)
    _assert_exact(loaded, _rebuild(loaded))


def test_unrepresentable_source_date_fails_without_dropping_published_rows(loaded):
    from psycopg import DataError

    before = _snapshot(loaded)
    with loaded.connect() as con:
        con.execute('UPDATE costs SET "ChargePeriodStart" = \'infinity\'::date '
                    'WHERE "ChargePeriodStart" = ?', [date(2024, 2, 29)])
    with pytest.raises(DataError, match="date"):
        _rebuild(loaded)
    assert _snapshot(loaded) == before


def test_configuration_rejects_unknown_values(monkeypatch):
    from app import derived_partitions

    monkeypatch.setenv("CLOUDLENS_DERIVED_LAYOUT", "montly")
    with pytest.raises(ValueError, match=r"flat.*monthly"):
        derived_partitions.enabled()
