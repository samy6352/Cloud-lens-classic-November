"""Durable incremental recovery on isolated PostgreSQL, without scanning unrelated months."""
import asyncio
import json
from concurrent.futures import ThreadPoolExecutor
from datetime import date
from types import SimpleNamespace

import pytest

pytestmark = pytest.mark.postgres
JAN = (date(2026, 1, 1), date(2026, 2, 1))
FEB = (date(2026, 2, 1), date(2026, 3, 1))
MAR = (date(2026, 3, 1), date(2026, 4, 1))


@pytest.fixture
def loaded(postgres_warehouse):
    from app.warehouse import COLUMNS, NUMERIC, insert_rows

    wh = postgres_warehouse
    rows = []
    for month in range(1, 4):
        values = {
            "ChargePeriodStart": f"2026-{month:02d}-15",
            "SubAccountId": "sub-a", "SubAccountName": "A",
            "ResourceId": "resource-a", "ResourceName": "A",
            "BillingCurrency": "USD", "BillingCostBasis": "FOCUS",
            "ServiceName": "Storage", "ServiceFamily": "Storage",
            "BilledCost": 10.0, "InvoiceCost": 10.0, "EffectiveCost": 10.0,
        }
        rows.append([values.get(c, 0.0 if c in NUMERIC else None) for c in COLUMNS])
    with wh.connect() as con:
        insert_rows(con, rows)
    wh.refresh_rollup(strict=True)
    return wh


def correct(wh, window, *, tracked=True, commit=True):
    from app import jobs

    with wh.connect() as con:
        con.execute("BEGIN")
        try:
            jobs.lock_generation(con)
            con.execute(
                'UPDATE costs SET "BilledCost" = "BilledCost" + 1 '
                'WHERE "ChargePeriodStart" >= ? AND "ChargePeriodStart" < ?', list(window))
            jobs.advance_generation(con, window if tracked else None)
            con.execute("COMMIT" if commit else "ROLLBACK")
        except BaseException:
            con.execute("ROLLBACK")
            raise


def record_stages(monkeypatch):
    from app import derived

    calls = []
    original = derived.stage

    def stage(con, schema, module, source, window):
        calls.append((module.TABLE, window))
        return original(con, schema, module, source, window)

    monkeypatch.setattr(derived, "stage", stage)
    return calls


def assert_current(wh):
    from app import jobs
    from app.warehouse import DERIVED_MODULES

    with wh.connect(read_only=True) as con:
        assert all(jobs.rollup_current(con, m.TABLE) for m in DERIVED_MODULES)
        detail = con.execute('SELECT sum("BilledCost") FROM costs').fetchone()[0]
        rolled = con.execute('SELECT sum("BilledCost") FROM costs_rollup').fetchone()[0]
        assert rolled == pytest.approx(detail)
        state = json.loads(con.execute(
            "SELECT value FROM meta WHERE key=?", [jobs.ROLLUP_DIRTY_KEY]).fetchone()[0])
        assert state["windows"] == []


def assert_windows(calls, windows):
    from datetime import timedelta

    from app import billing_serving, jobs, resource_ids, resource_tags, schema_v2
    from app.warehouse import DERIVED_MODULES

    expected = []
    for module in DERIVED_MODULES:
        periods = windows
        if module in (billing_serving, resource_tags, resource_ids) and windows != [None]:
            periods = jobs.merge_windows([
                (lo.replace(day=1), schema_v2.next_month(hi - timedelta(days=1)))
                for lo, hi in windows])
        expected.extend((module.TABLE, period) for period in periods)
    assert calls == expected


def test_restart_recovers_disjoint_periods_without_touching_the_gap(loaded, monkeypatch):
    from app import derived, rollup
    from app.warehouse import Warehouse

    with loaded.connect() as con:
        active = derived.active_table(con, loaded._rollup_schema, rollup)
        before = con.execute(
            f'SELECT ctid::text, * FROM "{loaded._rollup_schema}".{active} '
            'WHERE "ChargePeriodStart" >= ? AND "ChargePeriodStart" < ?', list(FEB)).fetchall()
    correct(loaded, JAN)
    correct(loaded, MAR)
    restarted = Warehouse()
    calls = record_stages(monkeypatch)
    try:
        asyncio.run(restarted.refresh_rollup_async())
        assert_windows(calls, [JAN, MAR])
        assert_current(restarted)
        with restarted.connect() as con:
            assert derived.active_table(con, restarted._rollup_schema, rollup) == active
            after = con.execute(
                f'SELECT ctid::text, * FROM "{restarted._rollup_schema}".{active} '
                'WHERE "ChargePeriodStart" >= ? AND "ChargePeriodStart" < ?', list(FEB)).fetchall()
        assert after == before
    finally:
        restarted._store.close()


def test_failed_rebuild_keeps_earlier_period_when_a_later_import_arrives(loaded, monkeypatch):
    from app import derived, jobs

    correct(loaded, JAN)
    original = derived.stage

    def fail(con, schema, module, source, window):
        if module.TABLE == "resource_presence":
            raise RuntimeError("interrupted during derived build")
        return original(con, schema, module, source, window)

    monkeypatch.setattr(derived, "stage", fail)
    with pytest.raises(RuntimeError, match="interrupted"):
        asyncio.run(loaded.refresh_rollup_async(JAN))
    with loaded.connect() as con:
        assert not jobs.rollup_current(con)
        assert jobs.dirty_windows(con, *jobs._generation_pair(con)) == [JAN]
    correct(loaded, MAR)
    monkeypatch.setattr(derived, "stage", original)
    calls = record_stages(monkeypatch)
    asyncio.run(loaded.refresh_rollup_async(MAR))
    assert_windows(calls, [JAN, MAR])
    assert_current(loaded)


def test_rolled_back_write_does_not_dirty_or_advance_the_generation(loaded):
    from app import jobs

    before = loaded.get_meta(jobs.ROLLUP_DIRTY_KEY)
    correct(loaded, JAN, commit=False)
    assert loaded.get_meta(jobs.ROLLUP_DIRTY_KEY) == before
    assert_current(loaded)


@pytest.mark.parametrize("uncertainty", ["unknown-write", "missing-ledger", "stale-ledger",
                                          "layout-change"])
def test_uncertain_coverage_requires_a_full_rebuild(loaded, monkeypatch, uncertainty):
    from app import jobs

    correct(loaded, JAN, tracked=uncertainty != "unknown-write")
    with loaded.connect() as con:
        if uncertainty == "missing-ledger":
            con.execute("DELETE FROM meta WHERE key=?", [jobs.ROLLUP_DIRTY_KEY])
        elif uncertainty == "stale-ledger":
            jobs._write_dirty(con, 999, 0, [MAR])
        elif uncertainty == "layout-change":
            con.execute("UPDATE meta SET value='older' WHERE key=?", [jobs.ROLLUP_LAYOUT_KEY])
    correct(loaded, MAR)
    calls = record_stages(monkeypatch)
    asyncio.run(loaded.refresh_rollup_async(MAR))
    assert_windows(calls, [None])
    assert_current(loaded)


def test_tables_missing_from_the_stamp_are_rebuilt_and_the_stamped_ones_repaired(
        loaded, monkeypatch):
    """A withdrawn table has no trusted contents, so it is built in full; a table still
    on the stamp has a complete ledger, so only the periods written since are redone.
    Rebuilding every table because one was withdrawn made adding a summary to a large
    estate a full rebuild of everything."""
    from app import jobs, rollup
    from app.warehouse import DERIVED_MODULES

    correct(loaded, JAN)
    with loaded.connect() as con:
        con.execute("UPDATE meta SET value='costs_rollup' WHERE key=?", [jobs.ROLLUP_TABLES_KEY])
    correct(loaded, MAR)
    calls = record_stages(monkeypatch)
    asyncio.run(loaded.refresh_rollup_async(MAR))
    assert calls == [(rollup.TABLE, JAN), (rollup.TABLE, MAR)] + [
        (m.TABLE, None) for m in DERIVED_MODULES if m is not rollup]
    assert_current(loaded)


def test_a_clean_restart_has_no_derived_work_to_repeat(loaded, monkeypatch):
    from app.warehouse import Warehouse

    restarted = Warehouse()
    calls = record_stages(monkeypatch)
    try:
        asyncio.run(restarted.refresh_rollup_async())
        assert calls == []
        assert_current(restarted)
    finally:
        restarted._store.close()


def test_concurrent_writers_accumulate_both_periods(loaded, monkeypatch):
    with ThreadPoolExecutor(max_workers=2) as executor:
        list(executor.map(lambda window: correct(loaded, window), [JAN, MAR]))
    calls = record_stages(monkeypatch)
    asyncio.run(loaded.refresh_rollup_async())
    assert_windows(calls, [JAN, MAR])
    assert_current(loaded)


def test_cost_details_replacement_records_its_window_with_the_write(loaded):
    from app import jobs
    from app.warehouse import COLUMNS

    with loaded.connect(read_only=True) as con:
        columns = ", ".join(f'"{column}"' for column in COLUMNS)
        records = con.execute(
            f'SELECT {columns} FROM costs WHERE "ChargePeriodStart" >= ? '
            'AND "ChargePeriodStart" < ?', list(JAN)).fetchall()
    asyncio.run(loaded._replace("sub-a", "2026-01-01", "2026-01-31", records))
    with loaded.connect(read_only=True) as con:
        assert jobs.dirty_windows(con, *jobs._generation_pair(con)) == [JAN]
        assert not jobs.rollup_current(con)


def test_adjacent_periods_merge_but_gaps_remain():
    from app import jobs

    assert jobs.merge_windows([MAR, JAN, JAN]) == [JAN, MAR]
    assert jobs.merge_windows([MAR, JAN, FEB]) == [(JAN[0], MAR[1])]
    with pytest.raises(ValueError, match="half-open"):
        jobs.merge_windows([(JAN[1], JAN[0])])


def test_explicit_failed_maintenance_survives_restart(loaded, monkeypatch):
    from app import derived, exports, jobs
    from app.main import _prepare_snapshot_generation
    from app.warehouse import Warehouse

    original = derived.stage

    def fail(*args, **kwargs):
        raise RuntimeError("maintenance failed")

    monkeypatch.setattr(derived, "stage", fail)
    with pytest.raises(RuntimeError, match="maintenance"):
        loaded.refresh_rollup(strict=True, window=JAN)
    with loaded.connect(read_only=True) as con:
        assert not jobs.rollup_current(con)
        assert jobs.snapshot_generation(con).startswith("pending:")
    assert not jobs.generation_ready(jobs.read_generation())
    assert not exports._derived_is_current()
    monkeypatch.setattr(derived, "stage", original)
    restarted = Warehouse()
    calls = record_stages(monkeypatch)
    try:
        state = SimpleNamespace()
        asyncio.run(_prepare_snapshot_generation(restarted, state))
        assert state.snapshot_bootstrap_ready
        assert_windows(calls, [JAN])
        assert_current(restarted)
    finally:
        restarted._store.close()


@pytest.mark.parametrize("damage", ["missing-relation", "changed-layout"])
def test_recreated_relation_cannot_inherit_readiness(loaded, monkeypatch, damage):
    from app import derived, jobs, rollup
    from app.warehouse import DERIVED_MODULES, Warehouse

    correct(loaded, MAR)
    with loaded.connect() as con:
        active = derived.active_table(con, loaded._rollup_schema, rollup)
        if damage == "missing-relation":
            con.execute(f'DROP TABLE "{loaded._rollup_schema}".{active} CASCADE')
        else:
            con.execute(f'ALTER TABLE "{loaded._rollup_schema}".{active} '
                        'ADD COLUMN obsolete text')
    restarted = Warehouse()
    calls = record_stages(monkeypatch)
    try:
        with restarted.connect(read_only=True) as con:
            assert not jobs.rollup_current(con, rollup.TABLE)
            assert not jobs.rollup_complete(con)
        asyncio.run(restarted.refresh_rollup_async(MAR))
        # The damaged table in full; the others, whose contents survived, only the period
        # written since -- not a rebuild of every table's whole history. The surviving
        # tables are repaired first, so a rebuilt table that reads one sees it repaired.
        assert calls == [
            (m.TABLE, MAR) for m in DERIVED_MODULES if m is not rollup] + [(rollup.TABLE, None)]
        assert_current(restarted)
        with restarted.connect(read_only=True) as con:
            assert con.execute('SELECT count(DISTINCT "ChargePeriodStart") '
                               'FROM costs_rollup').fetchone()[0] == 3
    finally:
        restarted._store.close()


@pytest.mark.parametrize("damage", ["missing-ledger", "dropped-month", "detached-month"])
def test_partition_damage_triggers_full_automatic_recovery(loaded, monkeypatch, damage):
    from app import derived, derived_partitions, jobs, rollup
    from app.main import _prepare_snapshot_generation
    from app.warehouse import Warehouse

    monkeypatch.setenv("CLOUDLENS_DERIVED_LAYOUT", "monthly")
    loaded.refresh_rollup(strict=True)
    with loaded.connect() as con:
        active = derived.active_table(con, loaded._rollup_schema, rollup)
        leaf = f"{active}_202601"
        if damage == "missing-ledger":
            con.execute(f'DROP TABLE "{loaded._rollup_schema}".{derived_partitions.META}')
        elif damage == "dropped-month":
            con.execute(f'DROP TABLE "{loaded._rollup_schema}".{leaf}')
        else:
            con.execute(f'ALTER TABLE "{loaded._rollup_schema}".{active} '
                        f'DETACH PARTITION "{loaded._rollup_schema}".{leaf}')
    restarted = Warehouse()
    calls = record_stages(monkeypatch)
    try:
        with restarted.connect(read_only=True) as con:
            assert not jobs.rollup_current(con, rollup.TABLE)
            assert not jobs.rollup_complete(con)
        state = SimpleNamespace()
        asyncio.run(_prepare_snapshot_generation(restarted, state))
        assert state.snapshot_bootstrap_ready
        if damage == "missing-ledger":
            # The ledger is shared, so every partitioned table is withdrawn at once.
            assert_windows(calls, [None])
        else:
            # One damaged table is rebuilt in full; the undamaged ones are left alone.
            assert calls == [(rollup.TABLE, None)]
        assert_current(restarted)
    finally:
        restarted._store.close()
