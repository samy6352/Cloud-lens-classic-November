"""FOCUS repairs changed days, with monthly rankings refreshed from those updated summaries."""
import asyncio
from datetime import date, datetime
from unittest.mock import AsyncMock

import pytest

from tests import test_focus_incremental as samples
from tests.test_derived_partitions import _assert_exact, _snapshot
from tests.test_derived_recovery import assert_current, assert_windows, record_stages

pytestmark = pytest.mark.postgres
DAYS = ("2026-07-31", "2026-08-01", "2026-08-02", "2026-08-10", "2026-09-01")
AUG2 = (date(2026, 8, 2), date(2026, 8, 3))
AUG10 = (date(2026, 8, 10), date(2026, 8, 11))


def _rows(changes=None, deleted=()):
    changes = changes or {}
    return [
        [*samples.charge(day, changes.get(day, 10) + i, effective=8 + i,
                         resource=f"/resource/{i}", tags='{"env":"prod"}', quantity=i + 1),
         20 + i, 15 + i, "Compute", f"Resource {i}"]
        for day in DAYS if day not in deleted for i in range(4)
    ]


@pytest.fixture
def focus(postgres_warehouse, monkeypatch):
    from app import exports, jobs

    monkeypatch.setenv("CLOUDLENS_DERIVED_LAYOUT", "monthly")
    monkeypatch.setattr(exports, "warehouse", postgres_warehouse)
    monkeypatch.setattr(jobs, "publish_generation", AsyncMock())
    monkeypatch.setattr(exports.azure, "request",
                        AsyncMock(side_effect=AssertionError("No Azure calls")))
    monkeypatch.setattr(samples, "HEADER", [*samples.HEADER,
        "ListCost", "ContractedCost", "ServiceName", "ResourceName"])

    def ingest(rows, run="initial"):
        blobs = samples.report(rows, run=run, start="2026-07-01", end="2026-09-30",
                               submitted="2026-10-01")
        reader = samples.Reader(blobs)
        monkeypatch.setattr(exports, "BlobReader", lambda *args: reader)
        try:
            return asyncio.run(exports.ingest_export(
                account="offline", container="focus-exports", required_metric="FocusCost",
                subscription_id="sub-a", scope=["sub-a"], run_identity=(run, "2026-10-01")))
        finally:
            assert reader.closed

    return postgres_warehouse, ingest


def _pending(wh):
    from app import jobs

    with wh.connect(read_only=True) as con:
        return jobs.dirty_windows(con, *jobs._generation_pair(con))


def _untouched_days(wh, excluded):
    from app import billing_serving, derived, resource_tags
    from app.warehouse import DERIVED_MODULES

    result = {}
    with wh.connect(read_only=True) as con:
        for module in DERIVED_MODULES:
            table = derived.active_table(con, wh._rollup_schema, module)
            dates = excluded
            period = '"ChargePeriodStart"'
            # Month-grain tables repair the whole month that contains a corrected day:
            # a month's ranking, or a resource's latest tags, depends on every day in it.
            if module in (billing_serving, resource_tags):
                dates = sorted({day.replace(day=1) for day in excluded})
                period = 'date_trunc(\'month\', "ChargePeriodStart")::date'
            result[module.TABLE] = con.execute(
                f'SELECT tableoid::oid, ctid::text, xmin::text, to_jsonb(t)::text '
                f'FROM "{wh._rollup_schema}".{table} t '
                f'WHERE {period} NOT IN ({",".join("?" for _ in dates)}) '
                'ORDER BY tableoid, ctid', list(dates)).fetchall()
    return result


def _assert_billing(wh, monkeypatch):
    from app import billing
    from tests.test_billing_scaling import _equivalent

    with monkeypatch.context() as patch:
        patch.setattr(billing, "BREAKDOWN_LIMIT", 2)
        actual = billing.build(["sub-a"], month="2026-08", today=date(2026, 10, 1), warehouse=wh)
        patch.setattr(wh, "_rollup_fresh", lambda *args, **kwargs: False)
        expected = billing.build(["sub-a"], month="2026-08", today=date(2026, 10, 1), warehouse=wh)
    _equivalent(actual, expected)
    other = [row for row in actual["breakdowns"]["resources"] if row["is_other"]]
    assert len(other) == 1 and other[0]["group_count"] == 2
    assert sum(row["cost"] for row in actual["breakdowns"]["resources"]) == pytest.approx(
        actual["kpis"]["recorded_cost"])


def test_full_publish_then_one_day_correction_stages_only_that_day(focus, monkeypatch):
    wh, ingest = focus
    calls = record_stages(monkeypatch)
    ingest(_rows())
    assert_windows(calls, [None])
    assert_current(wh)
    _assert_exact(wh)
    before = _untouched_days(wh, [AUG2[0]])
    months = _snapshot(wh, ["202608"])
    calls.clear()
    result = ingest(_rows({"2026-08-02": 99}), "correction")
    assert result["changed_days"] == 1
    assert_windows(calls, [AUG2])
    assert _untouched_days(wh, [AUG2[0]]) == before
    assert _snapshot(wh, ["202608"]) == months
    assert_current(wh)
    _assert_exact(wh)
    _assert_billing(wh, monkeypatch)
    calls.clear()
    assert ingest(_rows({"2026-08-02": 99}), "correction")["skipped_unchanged_run"]
    assert ingest(_rows({"2026-08-02": 99}), "identical")["changed_days"] == 0
    assert calls == []
    assert_current(wh)


def test_disjoint_correction_and_deletion_leave_gap_physically_untouched(focus, monkeypatch):
    wh, ingest = focus
    ingest(_rows())
    before = _untouched_days(wh, [AUG2[0], AUG10[0]])
    calls = record_stages(monkeypatch)
    result = ingest(_rows({"2026-08-02": 99}, deleted=["2026-08-10"]), "late")
    assert result["changed_days"] == 2 and result["deleted_rows"] == 8
    assert_windows(calls, [AUG2, AUG10])
    assert _untouched_days(wh, [AUG2[0], AUG10[0]]) == before
    assert_current(wh)
    _assert_exact(wh)
    _assert_billing(wh, monkeypatch)


def test_failed_publish_then_later_write_recovers_complete_ledger_after_restart(focus, monkeypatch):
    from app import derived
    from app.warehouse import Warehouse

    wh, ingest = focus
    ingest(_rows())
    before = _untouched_days(wh, [AUG2[0], AUG10[0]])
    all_before = _snapshot(wh)
    original = derived.stage

    def fail(con, schema, module, source, window):
        if module.TABLE == "resource_presence":
            raise RuntimeError("interrupted FOCUS rebuild")
        return original(con, schema, module, source, window)

    monkeypatch.setattr(derived, "stage", fail)
    with pytest.raises(RuntimeError, match="interrupted FOCUS"):
        ingest(_rows({"2026-08-02": 99}), "first-correction")
    assert _pending(wh) == [AUG2]
    assert _snapshot(wh) == all_before
    with pytest.raises(RuntimeError, match="interrupted FOCUS"):
        ingest(_rows({"2026-08-02": 99, "2026-08-10": 77}), "second-correction")
    assert _pending(wh) == [AUG2, AUG10]
    monkeypatch.setattr(derived, "stage", original)
    calls = record_stages(monkeypatch)
    restarted = Warehouse()
    try:
        asyncio.run(restarted.refresh_rollup_async())
        assert_windows(calls, [AUG2, AUG10])
        assert_current(restarted)
        _assert_exact(restarted)
        assert _untouched_days(restarted, [AUG2[0], AUG10[0]]) == before
    finally:
        restarted._store.close()
    calls.clear()
    assert ingest(_rows({"2026-08-02": 99, "2026-08-10": 77}),
                  "second-correction")["skipped_unchanged_run"]
    assert calls == []


def test_writer_failure_rolls_back_costs_receipts_generation_and_dirty_days(focus, monkeypatch):
    from app import jobs

    wh, ingest = focus
    ingest(_rows())
    with wh.connect() as con:
        before_meta = con.execute("SELECT key,value FROM meta ORDER BY key").fetchall()
        before_costs = con.execute("SELECT * FROM costs ORDER BY 1,2,3").fetchall()
    original = jobs._put

    def fail(con, key, value):
        if key.startswith("focus-day:"):
            raise RuntimeError("receipt failed after generation advance")
        original(con, key, value)

    monkeypatch.setattr(jobs, "_put", fail)
    with pytest.raises(RuntimeError, match="receipt failed"):
        ingest(_rows({"2026-08-02": 99}), "rolled-back")
    with wh.connect() as con:
        assert con.execute("SELECT key,value FROM meta ORDER BY key").fetchall() == before_meta
        assert con.execute("SELECT * FROM costs ORDER BY 1,2,3").fetchall() == before_costs
    assert_current(wh)
    _assert_exact(wh)


def test_unknown_older_coverage_cannot_be_narrowed_by_focus_correction(focus, monkeypatch):
    from app import jobs

    wh, ingest = focus
    ingest(_rows())
    with wh.connect() as con:
        con.execute("BEGIN")
        jobs.lock_generation(con)
        con.execute('UPDATE costs SET "BilledCost" = 50 WHERE "ChargePeriodStart" = ?',
                    [date(2026, 7, 31)])
        jobs.advance_generation(con)
        con.execute("COMMIT")
    assert _pending(wh) is None
    calls = record_stages(monkeypatch)
    ingest(_rows({"2026-07-31": 50, "2026-08-02": 99}), "after-unknown")
    assert_windows(calls, [None])
    assert_current(wh)
    _assert_exact(wh)


def test_day_bounds_reject_uncertain_dates_and_merge_only_adjacent_days(focus):
    from app import exports

    assert exports._rebuild_windows({"incremental": True, "changed_dates": [
        "2026-08-10", "2026-08-02", date(2026, 8, 1), "2026-08-02"]}) == [
            (date(2026, 8, 1), AUG2[1]), AUG10]
    for dates in (None, [], "2026-08-02", ["bad"], ["2026-08-02junk"],
                  [None], [datetime(2026, 8, 2)], ["9999-12-31"]):
        assert exports._rebuild_windows({"incremental": True, "changed_dates": dates}) is None
    assert exports._rebuild_windows({"changed_dates": ["2026-08-02"]}) is None


def test_generation_windows_validate_before_mutation_and_preserve_positional_callers(focus):
    from app import jobs

    wh, ingest = focus
    ingest(_rows())
    with wh.connect() as con:
        before = jobs._generation_pair(con)
        for kwargs in ({"windows": []}, {"windows": [AUG2], "window": AUG10},
                       {"windows": [(AUG2[1], AUG2[0])]}, {"windows": [("bad", "worse")]},
                       {"windows": [(datetime(2026, 8, 2), datetime(2026, 8, 3))]}):
            with pytest.raises(ValueError):
                jobs.advance_generation(con, **kwargs)
            assert jobs._generation_pair(con) == before
            assert jobs.dirty_windows(con, *before) == []
        con.execute("BEGIN")
        jobs.lock_generation(con)
        jobs.advance_generation(con, AUG2)
        jobs.advance_generation(con, windows=[AUG10, AUG2, AUG10])
        assert jobs.dirty_windows(con, *jobs._generation_pair(con)) == [AUG2, AUG10]
        con.execute("ROLLBACK")
    assert_current(wh)
