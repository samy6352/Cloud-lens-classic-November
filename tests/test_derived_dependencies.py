"""Dependent summaries consume this transaction's prepared source, never an older view."""
from datetime import date, timedelta
from types import SimpleNamespace

import pytest

pytestmark = pytest.mark.postgres
DAY = (date(2026, 1, 17), date(2026, 1, 18))


def modules():
    from app import schema_v2

    def module(table, monthly=False):
        def select_sql(source, window=None):
            period = ('date_trunc(\'month\', "ChargePeriodStart")::date'
                      if monthly else '"ChargePeriodStart"')
            amount = '"amount"' if monthly else '"BilledCost"'
            where = ("" if window is None else
                     f'WHERE "ChargePeriodStart" >= DATE \'{window[0]}\' '
                     f'AND "ChargePeriodStart" < DATE \'{window[1]}\'')
            return (f'SELECT {period} AS "ChargePeriodStart", sum({amount}) AS amount '
                    f'FROM {source} {where} GROUP BY 1')

        return SimpleNamespace(
            TABLE=table, PERIOD="ChargePeriodStart", DIMENSIONS=["ChargePeriodStart"],
            MEASURES=["amount"], select_sql=select_sql,
            table_sql=lambda schema, name: (
                f'CREATE TABLE IF NOT EXISTS "{schema}".{name} '
                '("ChargePeriodStart" date, amount double precision)'),
            index_sql=lambda schema, name: [
                f'CREATE INDEX IF NOT EXISTS {name}_period_idx ON "{schema}".{name} '
                '("ChargePeriodStart")'],
        )

    base, monthly = module("dep_base"), module("dep_month", True)
    monthly.SOURCE_TABLE = base.TABLE
    monthly.build_windows = lambda windows: [
        (schema_v2.month_start(lo), schema_v2.next_month(hi - timedelta(days=1)))
        for lo, hi in windows]
    return base, monthly


@pytest.fixture
def loaded(postgres_warehouse):
    from app import derived

    wh = postgres_warehouse
    chain = modules()
    with wh.connect() as con:
        con.execute('INSERT INTO costs ("ChargePeriodStart", "BilledCost") VALUES '
                    "('2026-01-01', 10), ('2026-01-17', 20), ('2026-02-01', 40)")
        derived.rebuild(con, wh._rollup_schema, chain, wh._pg_table)
    return wh, chain


def values(con, table):
    return con.execute(
        f'SELECT "ChargePeriodStart", amount FROM {table} ORDER BY 1').fetchall()


def test_full_build_reads_the_new_prepared_dependency_before_publication(loaded, monkeypatch):
    from app import derived

    wh, chain = loaded
    events = []
    real_stage, real_prepare, real_publish = derived.stage, derived.prepare, derived.publish

    def stage(con, schema, module, source, window):
        if module is chain[1]:
            assert ("prepare", chain[0].TABLE) in events
            assert not any(kind == "publish" for kind, _ in events)
            assert source != chain[0].TABLE
        return real_stage(con, schema, module, source, window)

    def prepare(con, schema, module, name):
        result = real_prepare(con, schema, module, name)
        events.append(("prepare", module.TABLE))
        return result

    def publish(con, schema, module, name):
        events.append(("publish", module.TABLE))
        return real_publish(con, schema, module, name)

    monkeypatch.setattr(derived, "stage", stage)
    monkeypatch.setattr(derived, "prepare", prepare)
    monkeypatch.setattr(derived, "publish", publish)
    with wh.connect() as con:
        con.execute('UPDATE costs SET "BilledCost"=50 WHERE "ChargePeriodStart"=?', [DAY[0]])
        derived.rebuild(con, wh._rollup_schema, chain, wh._pg_table)
        assert values(con, chain[1].TABLE) == [(date(2026, 1, 1), 60), (date(2026, 2, 1), 40)]
    assert events == [("prepare", "dep_base"), ("prepare", "dep_month"),
                      ("publish", "dep_base"), ("publish", "dep_month")]


def test_daily_repair_expands_only_the_dependent_month(loaded, monkeypatch):
    from app import derived

    wh, chain = loaded
    calls = []
    original = derived.stage

    def stage(con, schema, module, source, window):
        calls.append((module.TABLE, window))
        return original(con, schema, module, source, window)

    monkeypatch.setattr(derived, "stage", stage)
    with wh.connect() as con:
        table = derived.active_table(con, wh._rollup_schema, chain[1])
        query = (f'SELECT ctid::text, amount FROM "{wh._rollup_schema}".{table} '
                 'WHERE "ChargePeriodStart" = DATE \'2026-02-01\'')
        before = con.execute(query).fetchall()
        con.execute('UPDATE costs SET "BilledCost"=50 WHERE "ChargePeriodStart"=?', [DAY[0]])
        counts = derived.rebuild(con, wh._rollup_schema, chain, wh._pg_table, DAY)
        assert counts == {"dep_base": 3, "dep_month": 2}
        assert values(con, chain[1].TABLE) == [(date(2026, 1, 1), 60), (date(2026, 2, 1), 40)]
        assert con.execute(query).fetchall() == before
    assert calls == [("dep_base", DAY),
                     ("dep_month", (date(2026, 1, 1), date(2026, 2, 1)))]


def test_late_publication_failure_rolls_back_the_entire_dependency_chain(loaded, monkeypatch):
    from app import derived

    wh, chain = loaded
    original = derived.publish

    def publish(con, schema, module, name):
        if module is chain[1]:
            raise RuntimeError("dependent publication failed")
        return original(con, schema, module, name)

    with wh.connect() as con:
        before = {m.TABLE: values(con, m.TABLE) for m in chain}
        con.execute('UPDATE costs SET "BilledCost"=50 WHERE "ChargePeriodStart"=?', [DAY[0]])
        monkeypatch.setattr(derived, "publish", publish)
        with pytest.raises(RuntimeError, match="dependent publication"):
            derived.rebuild(con, wh._rollup_schema, chain, wh._pg_table)
        assert {m.TABLE: values(con, m.TABLE) for m in chain} == before


def test_missing_or_late_dependency_is_rejected_before_mutating_data(loaded):
    from psycopg.pq import TransactionStatus

    from app import derived

    wh, chain = loaded
    with wh.connect() as con:
        before = values(con, chain[1].TABLE)
        with pytest.raises(ValueError, match="built first"):
            derived.rebuild(con, wh._rollup_schema, list(reversed(chain)), wh._pg_table)
        assert con.raw.info.transaction_status == TransactionStatus.IDLE
        assert values(con, chain[1].TABLE) == before


def test_dependent_window_cannot_exclude_a_dirty_day(loaded):
    from app import derived

    wh, chain = loaded
    chain[1].build_windows = lambda windows: [
        (lo + timedelta(days=1), hi + timedelta(days=1)) for lo, hi in windows]
    with wh.connect() as con, pytest.raises(ValueError, match="do not cover"):
        derived.rebuild(con, wh._rollup_schema, chain, wh._pg_table, DAY)


def monthly_copy(monthly):
    copied = SimpleNamespace(**vars(monthly))
    copied.TABLE = "dep_copy"
    copied.SOURCE_TABLE = monthly.TABLE
    del copied.build_windows

    def select_sql(source, window=None):
        where = ("" if window is None else
                 f'WHERE "ChargePeriodStart" >= DATE \'{window[0]}\' '
                 f'AND "ChargePeriodStart" < DATE \'{window[1]}\'')
        return f'SELECT "ChargePeriodStart", amount FROM {source} {where}'

    copied.select_sql = select_sql
    return copied


def test_expanded_source_changes_propagate_to_every_downstream_module(loaded, monkeypatch):
    from app import derived

    wh, original_chain = loaded
    copied = monthly_copy(original_chain[1])
    chain = (*original_chain, copied)
    calls = []
    real_stage = derived.stage

    def stage(con, schema, module, source, window):
        calls.append((module.TABLE, window))
        return real_stage(con, schema, module, source, window)

    with wh.connect() as con:
        derived.rebuild(con, wh._rollup_schema, chain, wh._pg_table)
        con.execute('UPDATE costs SET "BilledCost"=50 WHERE "ChargePeriodStart"=?', [DAY[0]])
        monkeypatch.setattr(derived, "stage", stage)
        derived.rebuild(con, wh._rollup_schema, chain, wh._pg_table, DAY)
        assert values(con, copied.TABLE) == [(date(2026, 1, 1), 60), (date(2026, 2, 1), 40)]
    month = (date(2026, 1, 1), date(2026, 2, 1))
    assert calls == [("dep_base", DAY), ("dep_month", month), ("dep_copy", month)]


def test_downstream_expansion_is_checked_against_its_dependency_not_raw_dates(loaded):
    from app import derived

    wh, chain = loaded
    copied = monthly_copy(chain[1])
    copied.build_windows = lambda windows: [DAY]
    with wh.connect() as con, pytest.raises(ValueError, match="do not cover"):
        derived.rebuild(con, wh._rollup_schema, (*chain, copied), wh._pg_table, DAY)
