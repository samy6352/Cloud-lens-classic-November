"""Offline, real FOCUS reconciliation: complete snapshots in, changed cost days out."""
from __future__ import annotations

import ast
import asyncio
import csv
import importlib.util
import io
import json
import sqlite3
import sys
from contextlib import contextmanager
from datetime import date
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

pytestmark = pytest.mark.offline


@pytest.fixture(scope="module", autouse=True)
def offline_modules():
    """Load the real parser/helpers without constructing the PostgreSQL singleton."""
    import app

    global exports, COLUMNS
    before, attrs = dict(sys.modules), dict(vars(app))
    path = Path(__file__).resolve().parents[1] / "app" / "warehouse.py"
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(
                isinstance(target, ast.Name) and target.id == "warehouse" for target in node.targets):
            node.value = ast.parse("Warehouse.__new__(Warehouse)", mode="eval").body
    spec = importlib.util.spec_from_file_location("app.warehouse", path)
    module = importlib.util.module_from_spec(spec)
    sys.modules["app.warehouse"] = app.warehouse = module
    exec(compile(ast.fix_missing_locations(tree), str(path), "exec"), module.__dict__)
    COLUMNS = module.COLUMNS
    from app import exports
    yield
    for key in list(sys.modules):
        if key.startswith("app.") and key not in before:
            sys.modules.pop(key, None)
    for key, value in before.items():
        if key.startswith("app."):
            sys.modules[key] = value
    for key in list(vars(app)):
        if key not in attrs:
            delattr(app, key)
    for key, value in attrs.items():
        setattr(app, key, value)


class DateCursor:
    """Mimic the PostgreSQL DATE return type while executing the real SQL in SQLite."""

    def __init__(self, cursor):
        self.cursor = cursor

    def fetchmany(self, size):
        return [(date.fromisoformat(row[0]), *row[1:]) for row in self.cursor.fetchmany(size)]


class Connection(sqlite3.Connection):
    lock_requests = 0
    fail_log = False
    before_lock = None

    def execute(self, sql, parameters=()):
        if sql == "LOCK TABLE costs IN SHARE ROW EXCLUSIVE MODE":
            self.lock_requests += 1
            if self.before_lock:
                callback, self.before_lock = self.before_lock, None
                callback()
            return super().execute("SELECT 1")
        if self.fail_log and sql.startswith("INSERT INTO ingest_log"):
            raise RuntimeError("offline log failure")
        parameters = [value.isoformat() if isinstance(value, date) else value for value in parameters]
        cursor = super().execute(sql, parameters)
        if sql.startswith('SELECT "ChargePeriodStart","BilledCost"'):
            assert self.in_transaction and self.lock_requests
            return DateCursor(cursor)
        return cursor


class MemoryWarehouse:
    def __init__(self):
        self.db = sqlite3.connect(":memory:", check_same_thread=False, isolation_level=None,
                                  factory=Connection)
        self.db.execute("CREATE TABLE costs (" + ",".join(
            f'"{name}" {"REAL" if name in exports.NUMERIC else "TEXT"}' for name in COLUMNS) + ")")
        self.db.execute("CREATE TABLE ingest_log (a,b,c,d,e,f,g)")
        self.db.execute("CREATE TABLE meta (key TEXT PRIMARY KEY,value TEXT,updated_at TEXT)")
        self.db.create_function("now", 0, lambda: "2026-09-14")
        self.state = {}
        self.rollups = 0
        self.rollup_windows = []

    @contextmanager
    def connect(self, **kwargs):
        yield self.db

    async def refresh_rollup_async(self, window=None):
        self.rollups += 1
        self.rollup_windows.append(window)

    def summary(self):
        return {"rows": self.db.execute("SELECT COUNT(*) FROM costs").fetchone()[0]}

    def snapshot(self):
        return self.db.execute("SELECT rowid,* FROM costs ORDER BY rowid").fetchall()

    def logs(self):
        return self.db.execute("SELECT * FROM ingest_log").fetchall()

    def rows(self):
        return self.db.execute(
            'SELECT "SubAccountId","ChargePeriodStart","BilledCost","BillingCurrency" '
            'FROM costs ORDER BY 1,2,3,4').fetchall()


def insert(con, records, table="costs", **kwargs):
    """The offline stand-in for `warehouse.insert_rows`, which is COPY on PostgreSQL.

    Takes `table` because the importer stages rows into a table of its own before
    reconciling them, and a helper hard-coded to `costs` would send them straight
    into the warehouse instead.
    """
    if not records:
        return 0
    con.executemany(f"INSERT INTO {table} VALUES ("
                    + ",".join("?" for _ in COLUMNS) + ")", records)
    return len(records)


@pytest.fixture
def memory(monkeypatch):
    from app import jobs
    from app import warehouse as warehouse_mod
    monkeypatch.setattr(jobs, "publish_generation", AsyncMock())
    wh = MemoryWarehouse()
    monkeypatch.setattr(exports, "warehouse", wh)
    # The importer stages rows through `warehouse.insert_rows`, which is COPY against
    # PostgreSQL. Offline that has to be a plain executemany into SQLite -- patched on
    # the warehouse module because that is where `staging` reads it from.
    monkeypatch.setattr(warehouse_mod, "insert_rows", insert)
    monkeypatch.setattr(warehouse_mod, "ensure_partitions_between",
                        lambda *args, **kwargs: [])
    monkeypatch.setattr(exports.azure, "request",
                        AsyncMock(side_effect=AssertionError("No Azure calls in offline tests")))
    yield wh
    wh.db.close()


class Reader:
    account, container = "offline", "focus-exports"

    def __init__(self, blobs):
        self.blobs, self.fetched, self.closed = blobs, [], False

    async def list(self, prefix=""):
        return [{"name": name, "modified": "2026-09-10", "size": len(value)}
                for name, value in self.blobs.items() if name.startswith(prefix)]

    async def get(self, name):
        self.fetched.append(name)
        return self.blobs[name]

    async def close(self):
        self.closed = True


HEADER = ["ChargePeriodStart", "BilledCost", "EffectiveCost", "BillingCurrency", "SubAccountId",
          "ResourceId", "Tags", "ChargeCategory", "PricingQuantity"]


def charge(day="2026-08-01", billed=3, *, effective=None, currency="USD", sub="sub-a",
           resource="r1", tags="", category="Usage", quantity=1):
    return [day, billed, billed if effective is None else effective, currency, sub, resource,
            tags, category, quantity]


def report(rows, *, run="run", start="2026-08-01", end="2026-08-31",
           submitted="2026-09-01", timeframe="MonthToDate", partitions=None):
    files = {}
    blobs = []
    for index, values in enumerate(partitions if partitions is not None else [rows]):
        text = io.StringIO(newline="")
        writer = csv.writer(text)
        writer.writerow(HEADER)
        writer.writerows(values)
        name = f"focus/owned/{run}/part-{index}.csv"
        files[name] = text.getvalue().encode()
        blobs.append({"blobName": name, "dataRowCount": len(values)})
    manifest = {
        "runInfo": {"runId": run, "startDate": start, "endDate": end, "submittedTime": submitted},
        "exportConfig": {"exportName": "owned", "type": "FocusCost", "timeFrame": timeframe},
        "dataRowCount": sum(blob["dataRowCount"] for blob in blobs),
        "blobCount": len(blobs),
        "blobs": blobs,
    }
    files[f"focus/owned/{run}/manifest.json"] = json.dumps(manifest).encode()
    return files


def edit_manifest(blobs, edit):
    key = next(name for name in blobs if name.endswith("manifest.json"))
    manifest = json.loads(blobs[key])
    edit(manifest)
    blobs[key] = json.dumps(manifest).encode()
    return blobs


def ingest(monkeypatch, blobs, **kwargs):
    reader = Reader(blobs)
    monkeypatch.setattr(exports, "BlobReader", lambda *args: reader)
    args = {"account": "offline", "container": "focus-exports", "required_metric": "FocusCost"}
    result = asyncio.run(exports.ingest_export(**{**args, **kwargs}))
    assert reader.closed
    return result, reader


def assert_counts(result, changed, unchanged, inserted, deleted, retained):
    assert result["incremental"] is True
    assert (result["changed_days"], result["unchanged_days"], result["inserted_rows"],
            result["deleted_rows"], result["unchanged_rows"]) == (
                changed, unchanged, inserted, deleted, retained)


def test_custom_window_keeps_native_metadata_and_skips_unchanged_partitions(monkeypatch, memory):
    blobs = report([charge("2026-08-16", 10), charge("2026-08-17", 2), charge("2026-08-17", 2)],
                   run="custom")
    def sizes(manifest):
        for blob in manifest["blobs"]:
            blob["byteCount"] = len(blobs[blob["blobName"]])
        manifest["byteCount"] = sum(b["byteCount"] for b in manifest["blobs"])
    edit_manifest(blobs, sizes)
    args = {"subscription_id": "sub-a", "scope": ["sub-a"],
            "run_identity": ("custom", "2026-09-01"),
            "requested_window": ("2026-08-17", "2026-08-31")}
    result, _ = ingest(monkeypatch, blobs, **args)
    assert result["rows"] == 2 and result["rows_inspected"] == 3
    assert result["manifests"][0]["native_timeframe"] == "MonthToDate"
    assert result["requested_window"] == ("2026-08-17", "2026-08-31")
    repeated, reader = ingest(monkeypatch, blobs, **args)
    assert repeated["skipped_unchanged_run"] and repeated["data_bytes"] == 0
    assert repeated["rows_inspected"] == 0 and repeated["rows"] == 2
    assert all(name.endswith("manifest.json") for name in reader.fetched)
    before = memory.snapshot()
    edit_manifest(blobs, lambda m: m["blobs"][0].update(byteCount=1))
    with pytest.raises(exports.ExportError, match="bytes differ"):
        ingest(monkeypatch, blobs, **args)
    assert memory.snapshot() == before


def test_repeated_reordered_complete_snapshot_is_a_noop(monkeypatch, memory):
    rows = [charge(), charge(), charge("2026-08-02", -4, category="Credit"),
            charge("2026-08-03", 0)]
    first, _ = ingest(monkeypatch, report(rows))
    assert_counts(first, 3, 0, 4, 0, 0)
    before = memory.snapshot()
    repeated, reader = ingest(monkeypatch, report(list(reversed(rows)), run="restated"))
    assert_counts(repeated, 0, 3, 0, 0, 4)
    assert repeated["rows"] == 4 and memory.snapshot() == before
    assert len(memory.logs()) == 2 and memory.rollups == 2
    assert any(name.endswith(".csv") for name in reader.fetched)


def test_only_changed_day_is_rewritten_even_when_currency_totals_cancel(monkeypatch, memory):
    initial = [charge(billed=10), charge(billed=10, currency="EUR"), charge("2026-08-02", 4)]
    ingest(monkeypatch, report(initial))
    stable_row = memory.snapshot()[-1]
    latest = [charge(billed=20), charge(billed=0, currency="EUR"), initial[-1]]
    result, _ = ingest(monkeypatch, report(latest))
    assert_counts(result, 1, 1, 2, 2, 1)
    assert stable_row in memory.snapshot()
    assert memory.rows() == [("sub-a", "2026-08-01", 0, "EUR"),
                             ("sub-a", "2026-08-01", 20, "USD"),
                             ("sub-a", "2026-08-02", 4, "USD")]


def test_duplicate_multiplicity_and_non_money_changes_are_not_deduplicated(monkeypatch, memory):
    ingest(monkeypatch, report([charge(), charge()]))
    more, _ = ingest(monkeypatch, report([charge(), charge(), charge()]))
    assert_counts(more, 1, 0, 3, 2, 0)
    tags = '{"owner":"team-a","notes":"a,b"}'
    changed, _ = ingest(monkeypatch, report([charge(tags=tags), charge(tags=tags)]))
    assert_counts(changed, 1, 0, 2, 3, 0)
    assert len(memory.snapshot()) == 2
    repeated, _ = ingest(monkeypatch, report([charge(tags=tags), charge(tags=tags)]))
    assert_counts(repeated, 0, 1, 0, 0, 2)


def test_credits_and_zero_net_rows_remain_real_cost_records(monkeypatch, memory):
    ingest(monkeypatch, report([charge(billed=0)]))
    result, _ = ingest(monkeypatch, report(
        [charge(billed=9), charge(billed=-9, category="Credit"), charge(billed=0)]))
    assert_counts(result, 1, 0, 3, 1, 0)
    assert result["rows"] == 3 and sum(row[2] for row in memory.rows()) == 0


def test_restatement_removes_last_charge_on_day_not_unchanged_day(monkeypatch, memory):
    ingest(monkeypatch, report([charge(), charge("2026-08-28", 7)]))
    stable_row = memory.snapshot()[0]
    result, _ = ingest(monkeypatch, report([charge()]))
    assert_counts(result, 1, 1, 0, 1, 1)
    assert memory.snapshot() == [stable_row]


def test_complete_empty_partitions_are_valid_but_ambiguous_empty_run_never_deletes(monkeypatch, memory):
    result, _ = ingest(monkeypatch, report([], partitions=[[], [charge()], []]))
    assert_counts(result, 1, 0, 1, 0, 0)
    before, logs = memory.snapshot(), memory.logs()
    with pytest.raises(exports.ExportError, match="no usable rows"):
        ingest(monkeypatch, report([]), scope=["sub-a"])
    assert memory.snapshot() == before and memory.logs() == logs


def test_attributed_complete_zero_row_run_removes_previous_costs_but_not_other_scopes(monkeypatch, memory):
    ingest(monkeypatch, report([charge(), charge(sub="sub-b"), charge("2026-08-29", 9)]))
    result, _ = ingest(monkeypatch, report([], submitted="2026-08-10"),
                       scope=["sub-a"], subscription_id="/subscriptions/SUB-A")
    assert_counts(result, 1, 0, 0, 1, 0)
    assert result["rows"] == result["declared_rows"] == 0
    assert (result["from"], result["to"], result["subscriptions"]) == (
        "2026-08-01", "2026-08-10", 1)
    assert memory.rows() == [("sub-a", "2026-08-29", 9, "USD"),
                             ("sub-b", "2026-08-01", 3, "USD")]
    before = memory.snapshot()
    repeated, _ = ingest(monkeypatch, report([], submitted="2026-08-10"),
                         scope=["sub-a"], subscription_id="sub-a")
    assert_counts(repeated, 0, 0, 0, 0, 0)
    assert memory.snapshot() == before


def test_explicit_subscription_coverage_cannot_include_foreign_rows_or_scope(monkeypatch, memory):
    ingest(monkeypatch, report([charge()]))
    before, logs = memory.snapshot(), memory.logs()
    with pytest.raises(exports.ExportError, match="another subscription"):
        ingest(monkeypatch, report([charge(sub="sub-b")]), subscription_id="sub-a", scope=["sub-a"])
    with pytest.raises(exports.ExportError, match="outside the selected scope"):
        ingest(monkeypatch, report([]), subscription_id="sub-a", scope=["sub-b"])
    assert memory.snapshot() == before and memory.logs() == logs


def test_subscription_coverage_does_not_cross_between_run_periods(monkeypatch, memory):
    august = [charge(billed=1), charge(billed=2, sub="sub-b")]
    september = [charge("2026-09-01", 3), charge("2026-09-01", 4, sub="sub-b")]
    sep_options = {"start": "2026-09-01", "end": "2026-09-30", "submitted": "2026-09-10"}
    ingest(monkeypatch, {**report(august, run="aug"), **report(september, run="sep", **sep_options)})
    result, _ = ingest(monkeypatch, {
        **report([charge(billed=11)], run="aug"),
        **report([charge("2026-09-01", 44, sub="sub-b")], run="sep", **sep_options)})
    assert_counts(result, 2, 0, 2, 2, 0)
    assert memory.rows() == [("sub-a", "2026-08-01", 11, "USD"),
                             ("sub-a", "2026-09-01", 3, "USD"),
                             ("sub-b", "2026-08-01", 2, "USD"),
                             ("sub-b", "2026-09-01", 44, "USD")]


def test_scope_and_mtd_submission_bound_never_widen_replacement(monkeypatch, memory):
    ingest(monkeypatch, report([charge(), charge("2026-08-29", 9), charge(sub="sub-b")]))
    blobs = report([charge(billed=30, sub="/subscriptions/SUB-A"),
                    charge(billed=100, sub="sub-b")], submitted="2026-08-10")
    result, _ = ingest(monkeypatch, blobs, scope=["/subscriptions/SUB-A"])
    assert_counts(result, 1, 0, 1, 1, 0)
    assert result["excluded_rows"] == 1 and result["subscriptions"] == 1
    assert memory.rows() == [("sub-a", "2026-08-01", 30, "USD"),
                             ("sub-a", "2026-08-29", 9, "USD"),
                             ("sub-b", "2026-08-01", 3, "USD")]
    before = memory.snapshot()
    with pytest.raises(exports.ExportError, match="no rows for the selected"):
        ingest(monkeypatch, report([charge(sub="sub-b")]), scope=["sub-a"])
    assert memory.snapshot() == before


@pytest.mark.parametrize("bad", [
    charge("bad-date"), charge("2026-07-31"), charge("2026-09-01"),
    charge(billed="bad"), charge(effective="NaN"), charge(billed="inf"),
    charge(currency=""), charge(sub=""), charge(quantity="not-a-number"),
    charge()[:-1], [*charge(), "unexpected"],
])
def test_malformed_report_never_touches_existing_costs(monkeypatch, memory, bad):
    ingest(monkeypatch, report([charge()]))
    before, logs, locks = memory.snapshot(), memory.logs(), memory.db.lock_requests
    with pytest.raises(exports.ExportError):
        ingest(monkeypatch, report([], partitions=[[charge(billed=90)], [bad]]))
    assert memory.snapshot() == before and memory.logs() == logs
    assert memory.db.lock_requests == locks


@pytest.mark.parametrize("edit", [
    lambda manifest: manifest.pop("dataRowCount"),
    lambda manifest: manifest.update(dataRowCount=0),
    lambda manifest: manifest.update(dataRowCount=-1),
    lambda manifest: manifest["blobs"][0].pop("dataRowCount"),
    lambda manifest: manifest["blobs"][0].update(dataRowCount=True),
    lambda manifest: manifest.update(dataRowCount=2, blobs=[
        {**manifest["blobs"][0], "dataRowCount": 2}]),
    lambda manifest: manifest["runInfo"].update(startDate="not-a-date"),
    lambda manifest: manifest["runInfo"].update(endDate="2026-07-31"),
])
def test_incomplete_or_invalid_manifest_never_touches_existing_costs(monkeypatch, memory, edit):
    ingest(monkeypatch, report([charge()]))
    before, logs = memory.snapshot(), memory.logs()
    with pytest.raises(exports.ExportError):
        ingest(monkeypatch, edit_manifest(report([charge(billed=99)]), edit))
    assert memory.snapshot() == before and memory.logs() == logs


def test_latest_complete_run_missing_partition_does_not_fall_back_or_write(monkeypatch, memory):
    old = report([charge()], run="old", submitted="2026-08-10")
    ingest(monkeypatch, old)
    before = memory.snapshot()
    new = report([charge(billed=90)], run="new", submitted="2026-08-11")
    new.pop(next(name for name in new if name.endswith(".csv")))
    with pytest.raises(KeyError):
        ingest(monkeypatch, {**old, **new})
    assert memory.snapshot() == before


def test_overlapping_windows_and_rows_in_wrong_file_are_not_summed(monkeypatch, memory):
    ingest(monkeypatch, report([charge()]))
    before = memory.snapshot()
    overlapping = {**report([charge()], run="month"),
                   **report([charge()], run="custom", timeframe="Custom")}
    with pytest.raises(exports.ExportError, match="overlapping"):
        ingest(monkeypatch, overlapping)
    wrong_partition = {**report([charge("2026-09-01")], run="aug"),
                       **report([charge("2026-09-01")], run="sep", start="2026-09-01",
                                end="2026-09-30", submitted="2026-09-10")}
    with pytest.raises(exports.ExportError, match="outside"):
        ingest(monkeypatch, wrong_partition)
    assert memory.snapshot() == before


@pytest.mark.parametrize("limit", ["files", "manifests"])
def test_safety_caps_never_import_partial_delta(monkeypatch, memory, limit):
    """A cap that trips must leave the warehouse exactly as it was.

    The row cap that used to be tested here is gone. EXPORT_MAX_ROWS refused any
    export larger than 100,000 rows, and a real customer month is 5.8M -- so it was
    not protecting the importer from a pathological export, it was the reason the
    importer could not take a real estate. Rows are now streamed into a staging table
    in batches, so the size of an export costs bounded memory rather than a refusal.
    The file and manifest caps remain, because those bound how much work one run may
    take on rather than how large a legitimate export may be.
    """
    ingest(monkeypatch, report([charge()]))
    before, args = memory.snapshot(), {}
    blobs = report([charge(billed=90), charge("2026-08-02")])
    if limit == "files":
        args["max_files"] = 0
    else:
        monkeypatch.setenv("EXPORT_MAX_MANIFESTS", "0")
    with pytest.raises(exports.ExportError):
        ingest(monkeypatch, blobs, **args)
    assert memory.snapshot() == before


def test_an_export_larger_than_the_old_row_cap_is_imported_rather_than_refused(
        monkeypatch, memory):
    """The whole point of staging: 100,000 rows was a ceiling, not a batch.

    Parsed in batches small enough that several are needed, so the batching itself is
    exercised rather than merely configured.
    """
    monkeypatch.setattr(exports, "PARSE_BATCH", 40)
    rows = [charge(f"2026-08-{(i % 28) + 1:02d}", billed=i % 7) for i in range(250)]
    result, _ = ingest(monkeypatch, report(rows))
    assert result["rows"] == 250
    assert len(memory.snapshot()) == 250


def test_replay_reconciles_intervening_other_source_under_writer_lock(monkeypatch, memory):
    blobs = report([charge()])
    ingest(monkeypatch, blobs)

    def another_source():
        memory.db.execute('UPDATE costs SET "BilledCost"=99,"BillingCostBasis"=?', ["ActualCost"])

    memory.db.before_lock = another_source
    result, _ = ingest(monkeypatch, blobs)
    assert_counts(result, 1, 0, 1, 1, 0)
    assert memory.rows() == [("sub-a", "2026-08-01", 3, "USD")]


def test_unchanged_retry_repairs_interrupted_post_commit_rollup(monkeypatch, memory):
    rebuild = AsyncMock(side_effect=RuntimeError("offline rollup failure"))
    monkeypatch.setattr(memory, "refresh_rollup_async", rebuild)
    with pytest.raises(RuntimeError, match="rollup failure"):
        ingest(monkeypatch, report([charge()]))
    before = memory.snapshot()
    rebuild.side_effect = None
    result, _ = ingest(monkeypatch, report([charge()]))
    assert_counts(result, 0, 1, 0, 0, 1)
    assert rebuild.await_count == 2 and memory.snapshot() == before


def _mark_derived_current(memory):
    """Say the derived tables hold the generation `costs` is now at.

    What `Warehouse.refresh_rollup` stamps on a successful rebuild. The offline
    warehouse's rebuild is a no-op that never writes it, so without this every
    import looks like one retrying after an interrupted rebuild -- which is the
    case the skip must *not* apply to.
    """
    from app import jobs
    from app.warehouse import DERIVED_MODULES

    # The whole roster, as a real rebuild stamps it. A partial list reads as a table
    # that has never been built, which is the case the skip must *not* apply to.
    with jobs.coherent_rollup(memory, [module.TABLE for module in DERIVED_MODULES]):
        pass


def _dirty_windows(memory):
    from app import jobs

    return jobs.dirty_windows(memory.db, *jobs._generation_pair(memory.db))


def test_the_rebuild_records_only_the_day_a_change_landed_in(monkeypatch, memory):
    ingest(monkeypatch, report([charge("2026-08-01"), charge("2026-08-02", 4)]))
    assert memory.rollup_windows == [None]
    assert _dirty_windows(memory) is None  # No published baseline yet.
    _mark_derived_current(memory)

    memory.rollup_windows.clear()
    result, _ = ingest(monkeypatch, report(
        [charge("2026-08-01"), charge("2026-08-02", 99)], run="restated"))
    assert result["changed_days"] == 1
    assert _dirty_windows(memory) == [(date(2026, 8, 2), date(2026, 8, 3))]
    assert memory.rollup_windows == [None]  # Read the complete durable ledger.


def test_disjoint_changes_preserve_gaps_in_the_dirty_ledger(monkeypatch, memory):
    blobs = report([charge("2026-07-30"), charge("2026-08-02", 4)],
                   start="2026-07-01", end="2026-08-31")
    ingest(monkeypatch, blobs)
    _mark_derived_current(memory)
    result, _ = ingest(monkeypatch, report(
        [charge("2026-07-30", 9), charge("2026-07-31"), charge("2026-08-02", 99)],
        run="restated", start="2026-07-01", end="2026-08-31"))
    assert result["changed_days"] == 3
    assert _dirty_windows(memory) == [
        (date(2026, 7, 30), date(2026, 8, 1)), (date(2026, 8, 2), date(2026, 8, 3))]


def test_deletion_to_zero_records_the_deleted_day(monkeypatch, memory):
    ingest(monkeypatch, report([charge(), charge("2026-08-02", 4)]))
    _mark_derived_current(memory)
    result, _ = ingest(monkeypatch, report([charge()], run="deleted"))
    assert result["deleted_rows"] == 1 and result["changed_days"] == 1
    assert _dirty_windows(memory) == [(date(2026, 8, 2), date(2026, 8, 3))]


def test_an_import_that_changed_nothing_leaves_current_derived_tables_alone(monkeypatch, memory):
    """The scheduled sync's ordinary outcome: the export has not moved, so there is nothing to do.

    Both spellings of "nothing changed" are covered -- the same run re-offered, which
    is certified by its receipts and short-circuits before reading any data, and a
    fresh run whose rows reconcile to no change at all.
    """
    blobs = report([charge()])
    args = {"subscription_id": "sub-a", "scope": ["sub-a"],
            "run_identity": ("run", "2026-09-01")}
    ingest(monkeypatch, blobs, **args)
    _mark_derived_current(memory)

    memory.rollup_windows.clear()
    repeated, _ = ingest(monkeypatch, blobs, **args)
    assert repeated["skipped_unchanged_run"] and memory.rollup_windows == []

    restated, _ = ingest(monkeypatch, report([charge()], run="restated"))
    assert restated["changed_days"] == 0 and memory.rollup_windows == []


def test_an_unchanged_import_still_repairs_derived_tables_left_behind(monkeypatch, memory):
    """The skip is decided by the generation stamp, not by "did this import write anything".

    An import that changes nothing must still rebuild when a previous one failed after
    committing costs, or the staleness would never age out.
    """
    ingest(monkeypatch, report([charge()]))
    memory.rollup_windows.clear()
    restated, _ = ingest(monkeypatch, report([charge()], run="restated"))
    assert restated["changed_days"] == 0
    # No stamp was written, so the tables are behind and get a full rebuild.
    assert memory.rollup_windows == [None]


@pytest.mark.parametrize("failure", ["insert", "log"])
def test_transaction_rolls_back_all_changed_days_and_log_on_failure(monkeypatch, memory, failure):
    from app import warehouse as warehouse_mod

    ingest(monkeypatch, report([charge(), charge("2026-08-02", 4)]))
    before, logs = memory.snapshot(), memory.logs()
    if failure == "insert":
        # Fails part-way through staging a batch, which is where a dropped connection
        # or a bad row would. Nothing may reach `costs` from a run that ends this way.
        def failing_insert(con, records, table="costs", **kwargs):
            insert(con, records[:1], table)
            raise RuntimeError("offline insert failure")
        monkeypatch.setattr(warehouse_mod, "insert_rows", failing_insert)
    else:
        memory.db.fail_log = True
    with pytest.raises(RuntimeError, match="offline"):
        ingest(monkeypatch, report([charge(billed=30), charge("2026-08-02", 40)]))
    assert memory.snapshot() == before and memory.logs() == logs
    assert not memory.db.in_transaction and memory.state["status"] == "failed"
    assert memory.rollups == 1
    memory.db.fail_log = False
    monkeypatch.setattr(warehouse_mod, "insert_rows", insert)
    repeated, _ = ingest(monkeypatch, report([charge(billed=30), charge("2026-08-02", 40)]))
    assert_counts(repeated, 2, 0, 2, 2, 0)


def test_strict_focus_never_appends_but_legacy_behavior_is_preserved(monkeypatch, memory):
    blobs = {"old/part.csv": b"date,cost,billingcurrency,subscriptionid\n2026-08-01,3,USD,sub-a\n"}
    result, _ = ingest(monkeypatch, blobs, required_metric=None, metric="ActualCost")
    assert "incremental" not in result
    ingest(monkeypatch, blobs, required_metric=None, metric="ActualCost", replace=False)
    assert len(memory.snapshot()) == 2
    before = memory.snapshot()
    with pytest.raises(exports.ExportError, match="replace=True"):
        ingest(monkeypatch, report([charge()]), replace=False)
    assert memory.snapshot() == before


@pytest.mark.parametrize("tags", [None, "", " \t ", "null", " null "])
def test_genuinely_present_empty_focus_tags_preserve_column_provenance(tags):
    rows, skipped = exports._load_rows(HEADER, [charge(tags=tags)], None, "FocusCost")
    assert skipped == 0 and rows[0][list(COLUMNS).index("Tags")] == "{}"


@pytest.mark.parametrize("tags", ['{"broken"', "NULL", "None", '{"Owner":"team-a"}'])
def test_nonempty_focus_tags_are_preserved_not_relabelled_as_empty(tags):
    rows, _ = exports._load_rows(HEADER, [charge(tags=tags)], None, "FocusCost")
    assert rows[0][list(COLUMNS).index("Tags")] == tags


def test_missing_tags_column_and_truncated_tags_cell_stay_unknown():
    header = [name for name in HEADER if name != "Tags"]
    row = charge()
    row.pop(HEADER.index("Tags"))
    rows, _ = exports._load_rows(header, [row], None, "FocusCost")
    i_tags = list(COLUMNS).index("Tags")
    assert rows[0][i_tags] is None
    rows, _ = exports._load_rows(HEADER, [charge()[:HEADER.index("Tags")]], None, "FocusCost")
    assert rows[0][i_tags] is None


@pytest.mark.parametrize("tags", [None, ""])
def test_archives_and_legacy_rows_do_not_gain_tag_provenance(tags):
    values = dict.fromkeys(COLUMNS)
    values.update(ChargePeriodStart="2026-08-01", BilledCost=3, BillingCostBasis="FOCUS",
                  SubAccountId="sub-a", Tags=tags)
    rows, _ = exports._load_rows(list(values), [list(values.values())], None, "FocusCost")
    i_tags = list(COLUMNS).index("Tags")
    assert rows[0][i_tags] == tags
    rows, _ = exports._load_rows(["date", "cost", "subscriptionid", "tags"],
                                [["2026-08-01", "3", "sub-a", tags]], None, "ActualCost")
    assert rows[0][i_tags] == tags


def test_delta_updates_tag_provenance_without_changing_cost_totals(monkeypatch, memory):
    blobs = report([charge()])
    ingest(monkeypatch, blobs)
    memory.db.execute('UPDATE costs SET "Tags"=NULL')
    result, _ = ingest(monkeypatch, blobs)
    assert_counts(result, 1, 0, 1, 1, 0)
    assert memory.db.execute('SELECT "Tags","BilledCost" FROM costs').fetchone() == ("{}", 3)
