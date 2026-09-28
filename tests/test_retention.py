"""Planner tests never touch a real warehouse; PostgreSQL is explicitly opt-in."""
from __future__ import annotations

import datetime as dt
import importlib.util
import json
import os
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from app import retention

TODAY = dt.date(2026, 9, 21)


def leaves(month="2026-08-01", size=100):
    start = dt.date.fromisoformat(month)
    end = dt.date(start.year + (start.month == 12), start.month % 12 + 1, 1)
    return [
        retention.Partition(
            dataset, dataset if dataset == "costs" else dataset + "_a", "public",
            f"arbitrary_{index}_{month}", index + 1,
            f"FOR VALUES FROM ('{start}') TO ('{end}')", size // 2, size // 4, size,
            10, "approximate" if dataset == "costs" else "exact", start, end,
        )
        for index, dataset in enumerate(retention.DATASETS)
    ]


def snapshot(partitions=None, issues=None):
    return {
        "partitions": leaves() if partitions is None else partitions,
        "issues": issues or [],
        "database_total_bytes": 100_000,
        "active_roots": {},
    }


@pytest.mark.parametrize(("as_of", "keep", "expected"), [
    (TODAY, 1, "2026-08-01"),
    (dt.date(2026, 1, 1), 1, "2025-12-01"),
    (dt.date(2024, 3, 1), 1, "2024-02-01"),
    (dt.date(2024, 2, 29), 12, "2023-02-01"),
    (TODAY, None, None),
])
def test_policy_calendar_boundaries(as_of, keep, expected):
    cutoff = retention.validate_policy(keep, as_of)
    assert (cutoff.isoformat() if cutoff else None) == expected


@pytest.mark.parametrize("keep", [0, -1, True, 1.5, "3", 999999])
def test_invalid_keep(keep):
    with pytest.raises(ValueError):
        retention.validate_policy(keep, TODAY)


@pytest.mark.parametrize("as_of", ["2026-09-21", None, dt.datetime(2026, 9, 21)])
def test_invalid_date(as_of):
    with pytest.raises(ValueError):
        retention.validate_policy(None, as_of)


@pytest.mark.parametrize("bound", [
    None, "DEFAULT", "FOR VALUES IN (NULL)",
    "FOR VALUES FROM (MINVALUE) TO ('2026-08-01')",
    "FOR VALUES FROM ('2026-08-01') TO (MAXVALUE)",
    "FOR VALUES FROM ('2026-08-02') TO ('2026-09-01')",
    "FOR VALUES FROM ('2026-08-01') TO ('2026-10-01')",
    "FOR VALUES FROM ('2026-02-30') TO ('2026-03-01')",
    "FOR VALUES FROM ('2026-08-01 00:00:00') TO ('2026-09-01 00:00:00')",
    "FOR VALUES FROM ('2026-08-01', 1) TO ('2026-09-01', 1)",
])
def test_irregular_bounds_never_recognized(bound):
    assert retention.monthly_bounds(bound) is None


@pytest.mark.parametrize(("start", "end"), [
    ("2024-02-01", "2024-03-01"), ("2026-12-01", "2027-01-01"),
])
def test_leap_year_and_year_end_bounds(start, end):
    assert retention.monthly_bounds(f"FOR VALUES FROM ('{start}') TO ('{end}')") == (
        dt.date.fromisoformat(start), dt.date.fromisoformat(end),
    )


def test_unset_policy_preserves_all_history():
    plan = retention.build_plan(snapshot(leaves("2020-01-01")), as_of=TODAY)
    assert plan["deletion_enabled"] is False
    assert plan["preview_eligible_months"] == []
    assert plan["keep_from_inclusive"] is None
    assert "all history preserved" in retention.render_text(plan)
    assert "no data changed" in json.dumps(plan)


def test_preview_keeps_completed_current_future_and_aligns_all_datasets():
    parts = [part for month in (
        "2026-07-01", "2026-08-01", "2026-09-01", "2026-10-01",
    ) for part in leaves(month)]
    plan = retention.build_plan(snapshot(parts), as_of=TODAY, keep_months=1)
    assert [x["month"] for x in plan["preview_eligible_months"]] == ["2026-07-01"]
    assert len(plan["preview_eligible_months"][0]["partitions"]) == 5
    assert plan["deletion_enabled"] is False


def test_partial_missing_and_unverified_groups_require_review():
    parts = leaves("2026-06-01")[:-1] + leaves("2026-07-01")
    parts[-1] = replace(parts[-1], manual_review="OID-mismatched metadata")
    parts += [replace(p, start=None, end=None, bounds="DEFAULT",
                      manual_review="default/NULL") for p in leaves()]
    plan = retention.build_plan(snapshot(parts), as_of=TODAY, keep_months=1)
    assert not plan["preview_eligible_months"]
    assert plan["capacity"]["baseline_bytes"] is None
    assert any("alignment unverified" in warning for warning in plan["warnings"])


def test_preview_rechecks_bounds_even_for_caller_constructed_inventory():
    parts = [replace(p, bounds="DEFAULT") for p in leaves("2026-07-01")]
    plan = retention.build_plan(snapshot(parts), as_of=TODAY, keep_months=1)
    assert not plan["preview_eligible_months"]
    assert plan["capacity"]["baseline_bytes"] is None


def test_capacity_largest_completed_not_average_or_current():
    parts = leaves("2026-07-01", 1) + leaves("2026-08-01", 1000)
    parts += leaves("2026-09-01", 1_000_000) + leaves("2026-10-01", 10_000_000)
    plan = retention.build_plan(snapshot(parts), as_of=TODAY, headroom_bytes=2000)
    capacity = plan["capacity"]
    assert capacity["baseline_month"] == "2026-08-01"
    assert capacity["baseline_bytes"] == 5000
    assert [x["additional_months"] for x in capacity["projections"]] == [6, 12, 24]
    for estimate in capacity["projections"]:
        extra = estimate["additional_months"] * 5000
        assert estimate["estimated_database_bytes"] == 100_000 + extra
        assert estimate["estimated_raw_active_bytes"] == sum(p.total_bytes for p in parts) + extra
        assert estimate["estimated_database_plus_headroom_bytes"] == 102_000 + extra


def test_unknown_headroom_and_baseline_are_not_zero_estimates():
    plan = retention.build_plan(snapshot(leaves("2026-09-01")), as_of=TODAY)
    assert plan["capacity"]["baseline_bytes"] is None
    assert plan["capacity"]["peak_replacement_temp_backup_headroom_bytes"] is None
    assert all(p["estimated_database_bytes"] is None for p in plan["capacity"]["projections"])
    plan = retention.build_plan(snapshot(), as_of=TODAY)
    assert all(p["estimated_database_plus_headroom_bytes"] is None
               for p in plan["capacity"]["projections"])


@pytest.mark.parametrize("value", [-1, 1.5, True, "100"])
def test_invalid_headroom(value):
    with pytest.raises(ValueError):
        retention.build_plan(snapshot(), as_of=TODAY, headroom_bytes=value)


class FakeCursor:
    def __init__(self, *, mismatch=False, dirty=False, readonly="on",
                 kind="v", key=("r", 1, "ChargePeriodStart", "date")):
        self.calls = []
        self.result = []
        self.mismatch, self.dirty, self.readonly = mismatch, dirty, readonly
        self.kind, self.key = kind, key

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        pass

    def execute(self, query, params=None):
        self.calls.append((query, params))
        assert query.lstrip().startswith(("SELECT", "WITH RECURSIVE"))
        assert not any(word in query.upper().split() for word in ("DROP", "DELETE", "TRUNCATE"))
        assert "count(" not in query.lower()
        if "current_setting" in query:
            self.result = [(self.readonly, "repeatable read", 100_000)]
        elif "c.relname = ANY" in query:
            self.result = [(1, "costs", "p")] + [
                (i + 2, name, self.kind) for i, name in enumerate(retention.PUBLISHED)
            ]
        elif "pg_rewrite r" in query:
            index = params[0] - 2
            self.result = [(index + 20, retention.PUBLISHED[index] + "_b", "p", "public")]
        elif "SELECT to_regclass" in query:
            self.result = [(100,)]
        elif "SELECT parent_name" in query:
            self.result = [
                (name + "_b", f"odd_leaf_{i + 20}", i + 120 + self.mismatch, 42, self.dirty)
                for i, name in enumerate(retention.PUBLISHED)
            ]
            # Inactive metadata exists but SQL must restrict it to the resolved active roots.
            if self.kind == "v":
                assert all(name.endswith("_b") for name in params[0])
        elif "p.partstrat" in query:
            self.result = [self.key]
        elif "WITH RECURSIVE" in query:
            oid = params[0]
            self.result = [
                (oid + 100, "public", f"odd_leaf_{oid}", "r", 1,
                 "FOR VALUES FROM ('2026-07-01') TO ('2026-08-01')",
                 19.8, 100, 10, 120)
            ]
        else:
            raise AssertionError(query)
        return self

    def fetchone(self):
        return self.result[0] if self.result else None

    def fetchall(self):
        return self.result


def fake_inventory(**kwargs):
    cursor = FakeCursor(**kwargs)
    return retention.inventory(SimpleNamespace(cursor=lambda: cursor)), cursor


def test_catalogues_resolve_actual_active_oids_and_arbitrary_leaf_names():
    data, cursor = fake_inventory()
    assert not data["issues"]
    assert len(data["partitions"]) == 5
    assert data["partitions"][0].rows == 19
    assert "approximate" in data["partitions"][0].row_source
    assert all(p.rows == 42 and "exact" in p.row_source for p in data["partitions"][1:])
    assert all(root["name"].endswith("_b") for name, root in data["active_roots"].items()
               if name != "costs")
    assert all("_a" not in str(params) for _, params in cursor.calls)
    assert all('FROM "public"."costs' not in query for query, _ in cursor.calls)
    assert all(params is not None for query, params in cursor.calls if "current_setting" not in query)


@pytest.mark.parametrize("kwargs", [{"mismatch": True}, {"dirty": True}])
def test_derived_rows_require_clean_matching_oid(kwargs):
    data, _ = fake_inventory(**kwargs)
    assert all(p.rows is None for p in data["partitions"][1:])
    plan = retention.build_plan(data, as_of=TODAY, keep_months=1)
    assert not plan["preview_eligible_months"]
    assert plan["capacity"]["baseline_bytes"] is None


@pytest.mark.parametrize("kwargs", [
    {"kind": "r"}, {"key": ("r", 1, "BillingPeriodStart", "date")},
    {"key": ("r", 2, "ChargePeriodStart", "date")},
])
def test_unsupported_layout_never_yields_candidates(kwargs):
    data, _ = fake_inventory(**kwargs)
    assert data["issues"]
    assert not retention.build_plan(data, as_of=TODAY, keep_months=1)["preview_eligible_months"]


def test_readwrite_connection_rejected_before_catalogues():
    cursor = FakeCursor(readonly="off")
    with pytest.raises(ValueError, match="read-only"):
        retention.inventory(SimpleNamespace(cursor=lambda: cursor))
    assert len(cursor.calls) == 1


@pytest.mark.parametrize("schema", ["public; SELECT 1", "a.b", '"public"', "", "x" * 64])
def test_invalid_schema_never_queries(schema):
    cursor = Mock()
    with pytest.raises(ValueError):
        retention.inventory(SimpleNamespace(cursor=lambda: cursor), schema)
    cursor.execute.assert_not_called()


def test_direct_connection_readonly_from_first_statement(monkeypatch):
    import psycopg

    from app import store

    credential = Mock()
    credential.host, credential.database, credential.user = "localhost", "isolated", "writer"
    credential.schema, credential.sslmode = "", "disable"
    credential._password.return_value = "not-logged"
    monkeypatch.setattr(store, "PostgresStore", lambda: credential)
    opened = Mock()
    connect = Mock(return_value=opened)
    monkeypatch.setattr(psycopg, "connect", connect)
    assert retention.connect_read_only() is opened
    config = connect.call_args.kwargs
    assert "default_transaction_read_only=on" in config["options"]
    assert "statement_timeout=15000" in config["options"]
    assert "lock_timeout=2000" in config["options"]
    assert config["connect_timeout"] == 5
    assert opened.read_only is True
    assert opened.isolation_level == psycopg.IsolationLevel.REPEATABLE_READ
    credential.connect.assert_not_called()
    credential._open.assert_not_called()


def cli_module():
    path = Path(__file__).resolve().parents[1] / "tools" / "plan_retention.py"
    spec = importlib.util.spec_from_file_location("plan_retention", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_cli_invalid_policy_never_connects_and_does_not_log_secrets(monkeypatch, capsys):
    cli = cli_module()
    connect = Mock(side_effect=RuntimeError("password=secret host=private"))
    monkeypatch.setattr(cli, "connect_read_only", connect)
    assert cli.main(["--no-env", "--keep-months", "0"]) == 1
    connect.assert_not_called()
    assert cli.main(["--no-env"]) == 1
    output = capsys.readouterr().out
    assert "secret" not in output and "private" not in output
    assert "no data changed" in output


def test_cli_env_is_root_relative_and_shell_wins(monkeypatch, capsys):
    import dotenv

    cli = cli_module()
    loader = Mock()
    monkeypatch.setattr(dotenv, "load_dotenv", loader)
    connection = Mock()
    connection.__enter__ = Mock(return_value=connection)
    connection.__exit__ = Mock(return_value=False)
    monkeypatch.setattr(cli, "connect_read_only", lambda: connection)
    monkeypatch.setattr(cli, "inventory", lambda *args: snapshot())
    assert cli.main(["--env-file", "explicit.env", "--format", "json",
                     "--as-of", "2026-09-21"]) == 0
    loader.assert_called_once_with(cli.ROOT / "explicit.env", override=False)
    assert json.loads(capsys.readouterr().out)["deletion_enabled"] is False


@pytest.mark.parametrize("args", [["--as-of", "2026-02-30"], ["--apply"], ["--delete"]])
def test_cli_rejects_invalid_date_and_execution_options(args):
    with pytest.raises(SystemExit):
        cli_module().main(args)


@pytest.mark.postgres
def test_postgres_catalogue_inventory_and_readonly_startup(monkeypatch):
    """Only the dedicated test DB; synthetic objects roll back without deleting anything."""
    import uuid

    import psycopg
    from psycopg import sql

    from app.store import PostgresStore

    database = os.getenv("CLOUDLENS_PG_DB", "")
    if database != "cloudlens_envfx_test_6282cfcc":
        pytest.skip("requires parent's explicitly isolated cloudlens_envfx_test_6282cfcc")
    monkeypatch.setenv("PGHOST", os.environ["CLOUDLENS_PG_TEST"])
    monkeypatch.setenv("PGUSER", os.environ["CLOUDLENS_PG_USER"])
    monkeypatch.setenv("PGDATABASE", database)
    schema = "retention_" + uuid.uuid4().hex
    monkeypatch.setenv("PGSCHEMA", schema)
    store = PostgresStore()
    # Keep fixture DDL uncommitted, and switch this same transaction to read-only.
    # A rollback at the end discards only this synthetic test's creations.
    with psycopg.connect(
        host=store.host, user=store.user, password=store._password(), dbname=database,
        port=os.getenv("PGPORT", "5432"), sslmode=store.sslmode,
        options="-c statement_timeout=15000 -c lock_timeout=2000",
    ) as fixture:
        fixture.isolation_level = psycopg.IsolationLevel.REPEATABLE_READ
        fixture.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))
        fixture.execute(sql.SQL(
            "CREATE TABLE {}._derived_partition_counts "
            "(parent_name text, leaf_name text, leaf_oid oid, rows bigint, dirty boolean)"
        ).format(sql.Identifier(schema)))
        for index, dataset in enumerate(retention.DATASETS):
            parent = dataset if dataset == "costs" else dataset + "_b"
            identifier = sql.Identifier(schema, parent)
            fixture.execute(sql.SQL('CREATE TABLE {} ("ChargePeriodStart" date) '
                                    'PARTITION BY RANGE ("ChargePeriodStart")').format(identifier))
            for suffix, bound in [
                ("july", "FOR VALUES FROM ('2026-07-01') TO ('2026-08-01')"),
                ("aug", "FOR VALUES FROM ('2026-08-01') TO ('2026-09-01')"),
                ("null", "DEFAULT"),
            ]:
                leaf = f"arbitrary_{index}_{suffix}"
                fixture.execute(sql.SQL("CREATE TABLE {} PARTITION OF {} " + bound).format(
                    sql.Identifier(schema, leaf), identifier,
                ))
                if dataset != "costs":
                    fixture.execute(sql.SQL(
                        "INSERT INTO {}._derived_partition_counts VALUES "
                        "(%s, %s, %s::regclass::oid, 0, false)"
                    ).format(sql.Identifier(schema)), (parent, leaf, f'"{schema}"."{leaf}"'))
            if dataset != "costs":
                fixture.execute(sql.SQL("CREATE VIEW {} AS SELECT * FROM {}").format(
                    sql.Identifier(schema, dataset), identifier,
                ))
                fixture.execute(sql.SQL('CREATE TABLE {} ("ChargePeriodStart" date)').format(
                    sql.Identifier(schema, dataset + "_a"),
                ))
        fixture.execute("SET TRANSACTION READ ONLY")
        data = retention.inventory(fixture, schema)
        plan = retention.build_plan(data, as_of=TODAY, keep_months=1)
        assert len(data["partitions"]) == 15
        assert not data["issues"]
        assert [x["month"] for x in plan["preview_eligible_months"]] == ["2026-07-01"]
        assert all(p.rows == 0 for p in data["partitions"] if p.dataset != "costs")
        assert all(p.manual_review for p in data["partitions"] if p.bounds == "DEFAULT")
        assert all(not p.parent.endswith("_a") for p in data["partitions"])
        fixture.rollback()
    # Independently verify protected startup for the writable fixture role.
    with retention.connect_read_only() as reader:
        assert reader.execute(
            "SELECT current_setting('default_transaction_read_only'), "
            "current_setting('transaction_read_only'), current_setting('statement_timeout'), "
            "current_setting('lock_timeout')"
        ).fetchone() == ("on", "on", "15s", "2s")
        assert reader.execute(
            "SELECT to_regnamespace(%s)", (schema,),
        ).fetchone() == (None,)
