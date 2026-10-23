"""Execute sidecar SQL on an explicitly isolated PostgreSQL fixture, never HDFC."""
from datetime import date

import pytest

from app import environment_summary as summary

pytestmark = pytest.mark.postgres
WINDOW = date(2026, 7, 1), date(2026, 7, 5)


@pytest.fixture
def sidecar(postgres_warehouse):
    wh = postgres_warehouse
    from app.warehouse import COLUMNS, insert_rows

    cases = [
        ("sub-a", "USD", "USD", None, "FOCUS", 10.0, 10.0, '{"env":"PROD"}'),
        ("SUB-A", "INR", "USD", 80.0, "FOCUS", 160.0, 999.0, '{"Env":"Prod"}'),
        ("sub-b", "USD", "USD", None, "FOCUS", 50.0, 50.0, '{"env":"dev"}'),
        ("sub-a", "INR", None, None, "ActualCost", -80.0, -1.0, '{"env":"prod"}'),
        ("sub-a", "USD", None, None, "FOCUS", 4.0, 4.0, "{invalid"),
        ("sub-a", "USD", None, None, "FOCUS", 3.0, 3.0, '{"env":"","Env":"ignored"}'),
        ("sub-a", "USD", None, None, "FOCUS", 2.0, 2.0, '{"env":null,"Env":"qa"}'),
    ]
    rows = []
    for sub, billed, priced, exchange, basis, cost, usd, tags in cases:
        values = {
            "ChargePeriodStart": WINDOW[0], "SubAccountId": sub,
            "BillingCurrency": billed, "PricingCurrency": priced,
            "ExchangeRatePricingToBilling": exchange, "BillingCostBasis": basis,
            "BilledCost": cost, "CostInUsd": usd, "Tags": tags,
        }
        rows.append([values.get(column) for column in COLUMNS])
    with wh.connect() as con:
        insert_rows(con, rows)
        with con.raw.transaction():
            from app import jobs

            jobs.lock_generation(con)
            assert summary.migrate(con, wh._store.schema)
            assert not summary.migrate(con, wh._store.schema)
    return wh


def refresh(wh, window=WINDOW):
    from app import jobs

    with wh.connect() as con, con.raw.transaction():
        jobs.lock_generation(con)
        for sql in summary.refresh_statements(wh._store.schema, window):
            con.execute(sql)


def covered(con, wh):
    return con.execute(summary.coverage_complete_sql(
        wh._store.schema, *WINDOW)).fetchone()[0]


def ranking(con, wh, **kwargs):
    return con.execute(summary.ranked_sql(
        f"{wh._store.schema}.{summary.TABLE}", *WINDOW, **kwargs)).fetchall()


def test_refresh_covers_empty_days_and_reruns_without_double_counting(sidecar):
    wh = sidecar
    with wh.connect() as con:
        assert not covered(con, wh)
    refresh(wh)
    with wh.connect() as con:
        assert covered(con, wh)
        first = ranking(con, wh)
        assert ("prod", "USD", 10.0) in first
        assert ("prod", "INR", 80.0) in first
        assert ("dev", "USD", 50.0) in first
        assert ("qa", "USD", 2.0) in first
        assert all(row[0] for row in first)
        assert con.execute(
            f'SELECT count(*) FROM "{wh._store.schema}".{summary.COVERAGE_TABLE}'
        ).fetchone()[0] == 4
    refresh(wh)
    with wh.connect() as con:
        assert ranking(con, wh) == first


def test_scoped_ranking_excludes_other_subscription_and_matches_invoice_fx(sidecar, monkeypatch):
    wh = sidecar
    refresh(wh)

    monkeypatch.setenv("FX_RATES", '{"USD": 1.0, "INR": 90.0}')
    factor, _ = wh._currency_sql(1.0, "USD", legacy_ratio='"FxLegacyRatio"')
    with wh.connect() as con:
        con.execute("CREATE TEMP TABLE _scope (id text)")
        con.execute("INSERT INTO _scope VALUES (?)", ["sub-a"])
        rows = ranking(con, wh, scoped=True, billing_factor=factor, target_currency="USD")
        assert rows == [("prod", "USD", 11.0), ("qa", "USD", 2.0)]


def test_dimension_limit_is_shared_across_currencies_not_applied_to_each_bucket(sidecar):
    wh = sidecar
    schema = wh._store.schema
    with wh.connect() as con:
        con.execute(
            f'INSERT INTO "{schema}".{summary.TABLE} '
            '("ChargePeriodStart","SubAccountId","Environment","BillingCurrency","BilledCost",'
            '"FxNonzeroCharges") '
            "SELECT DATE '2026-07-01', 'sub-a', 'env-' || lpad(i::text, 2, '0'), "
            "CASE WHEN i < 30 THEN 'USD' ELSE 'EUR' END, 1.0, 1 "
            "FROM generate_series(0, 59) AS values(i)")
        rows = ranking(con, wh, limit=30)
        assert len({row[0] for row in rows}) == 30
        assert {row[0] for row in rows} == {f"env-{i:02d}" for i in range(30)}


def test_changed_and_unknown_windows_invalidate_coverage(sidecar):
    wh = sidecar
    refresh(wh)
    with wh.connect() as con:
        con.execute(summary.invalidate_sql(wh._store.schema, [
            (date(2026, 7, 3), date(2026, 7, 4))]))
        assert not covered(con, wh)
        assert con.execute(
            f'SELECT count(*) FROM "{wh._store.schema}".{summary.COVERAGE_TABLE} WHERE NOT dirty'
        ).fetchone()[0] == 3
    refresh(wh, (date(2026, 7, 3), date(2026, 7, 4)))
    with wh.connect() as con:
        assert covered(con, wh)
        con.execute(summary.invalidate_sql(wh._store.schema, None))
        assert not covered(con, wh)


def test_failed_chunk_refresh_preserves_summary_and_coverage(sidecar):
    wh = sidecar
    refresh(wh)
    with wh.connect() as con:
        before = ranking(con, wh)
    with (pytest.raises(RuntimeError, match="synthetic failed refresh"),
          wh.connect() as con, con.raw.transaction()):
        for sql in summary.refresh_statements(wh._store.schema, WINDOW):
            con.execute(sql)
        raise RuntimeError("synthetic failed refresh")
    with wh.connect() as con:
        assert covered(con, wh)
        assert ranking(con, wh) == before


def test_reader_keeps_coverage_and_summary_in_one_snapshot_during_invalidation(sidecar):
    wh = sidecar
    refresh(wh)
    with wh.connect() as reader:
        reader.execute(summary.READ_TRANSACTION_SQL)
        try:
            assert covered(reader, wh)
            before = ranking(reader, wh)
            with wh.connect() as writer, writer.raw.transaction():
                writer.execute(summary.invalidate_sql(wh._store.schema, None))
                writer.execute(f'DELETE FROM "{wh._store.schema}".{summary.TABLE}')
            assert covered(reader, wh)
            assert ranking(reader, wh) == before
        finally:
            reader.execute("ROLLBACK")
    with wh.connect() as con:
        assert not covered(con, wh)
        assert ranking(con, wh) == []


def runtime_read(wh, *, days=4, currency=""):
    with wh.reader(["sub-a"], repeatable_read=True) as reader:
        return summary.filter_rows(
            reader, wh, "2026-07-04", days, scope=True, currency=currency)


def change_costs(wh, *, window=WINDOW, failure=False):
    from app import jobs

    with wh.connect() as con, con.raw.transaction():
        jobs.lock_generation(con)
        con.execute(
            'UPDATE costs SET "BilledCost"="BilledCost"+1 '
            'WHERE "ChargePeriodStart"=DATE \'2026-07-01\'')
        jobs.advance_generation(con, window=window)
        if failure:
            raise RuntimeError("synthetic source failure")


def test_runtime_reads_only_current_complete_scoped_coverage(sidecar, monkeypatch):
    wh = sidecar
    monkeypatch.setenv("CLOUDLENS_ENV_SUMMARY", "true")
    assert runtime_read(wh) is None
    summary.refresh(wh._store, WINDOW)
    rows = runtime_read(wh)
    assert {row["v"] for row in rows} == {"prod", "qa"}
    assert all(row["v"] != "dev" for row in rows)
    assert runtime_read(wh, days=5) is None


def test_real_filter_route_uses_summary_without_tag_json_scan(sidecar, monkeypatch):
    import asyncio
    import json
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    from app import main
    from app.warehouse import Reader

    wh = sidecar
    monkeypatch.setenv("CLOUDLENS_ENV_SUMMARY", "true")
    summary.refresh(wh._store, WINDOW)
    monkeypatch.setattr(main, "current_user", lambda request: SimpleNamespace(admin=True))
    monkeypatch.setattr(main, "permitted", AsyncMock(return_value=["sub-a"]))
    queries = []
    original = Reader.rows

    def capture(reader, sql, limit=200):
        queries.append(sql)
        return original(reader, sql, limit)

    monkeypatch.setattr(Reader, "rows", capture)
    payload = json.loads(asyncio.run(main.filter_values(
        SimpleNamespace(), days=1, env_only=True)).body)
    assert "unavailable_dimensions" not in payload, payload
    assert {item["value"] for item in payload["dimensions"]["env"]} == {"prod", "qa"}
    assert any(summary.TABLE in sql for sql in queries)
    assert not any('TRY_CAST("Tags"' in sql for sql in queries)


def test_known_writes_invalidate_and_incremental_refresh_repairs_only_changed_days(
        sidecar, monkeypatch):
    wh = sidecar
    monkeypatch.setenv("CLOUDLENS_ENV_SUMMARY", "true")
    summary.refresh(wh._store, WINDOW)
    change_costs(wh, window=(WINDOW[0], date(2026, 7, 2)))
    assert runtime_read(wh) is None
    with wh.connect() as con:
        assert con.execute(
            f'SELECT count(*) FROM "{wh._store.schema}".{summary.COVERAGE_TABLE} WHERE dirty'
        ).fetchone()[0] == 1
    assert summary.refresh_pending(wh._store) == 1
    rows = runtime_read(wh)
    assert next(row for row in rows if row["v"] == "prod" and row["currency"] == "USD")["c"] == 11
    assert summary.refresh_pending(wh._store) == 0


def test_existing_warehouse_rebuild_runs_the_incremental_sidecar_hook(sidecar, monkeypatch):
    wh = sidecar
    monkeypatch.setenv("CLOUDLENS_ENV_SUMMARY", "true")
    summary.refresh(wh._store, WINDOW)
    change_costs(wh, window=(WINDOW[0], date(2026, 7, 2)))
    assert runtime_read(wh) is None
    wh.refresh_rollup(strict=True, incremental=True)
    assert runtime_read(wh) is not None
    assert summary.refresh_pending(wh._store) == 0


def test_failed_source_transaction_rolls_back_costs_generation_and_invalidation(sidecar, monkeypatch):
    wh = sidecar
    monkeypatch.setenv("CLOUDLENS_ENV_SUMMARY", "true")
    summary.refresh(wh._store, WINDOW)
    before = runtime_read(wh)
    with pytest.raises(RuntimeError, match="synthetic source failure"):
        change_costs(wh, failure=True)
    assert runtime_read(wh) == before
    assert summary.refresh_pending(wh._store) == 0


def test_disabled_writes_cannot_reenable_stale_certification(sidecar, monkeypatch):
    wh = sidecar
    monkeypatch.setenv("CLOUDLENS_ENV_SUMMARY", "true")
    summary.refresh(wh._store, WINDOW)
    monkeypatch.setenv("CLOUDLENS_ENV_SUMMARY", "false")
    change_costs(wh, window=(WINDOW[0], date(2026, 7, 2)))
    monkeypatch.setenv("CLOUDLENS_ENV_SUMMARY", "true")
    assert runtime_read(wh) is None
    summary.refresh(wh._store, (WINDOW[0], date(2026, 7, 2)))
    assert runtime_read(wh) is None  # The old other days must be backfilled again.
    with wh.connect() as con:
        assert con.execute(
            f'SELECT count(*) FROM "{wh._store.schema}".{summary.COVERAGE_TABLE}'
        ).fetchone()[0] == 1


def test_unknown_writes_require_explicit_backfill_not_automatic_history_rebuild(sidecar, monkeypatch):
    wh = sidecar
    monkeypatch.setenv("CLOUDLENS_ENV_SUMMARY", "true")
    summary.refresh(wh._store, WINDOW)
    change_costs(wh, window=None)
    assert runtime_read(wh) is None
    assert summary.refresh_pending(wh._store) == 0


def test_enabled_writer_without_migration_cannot_commit(postgres_warehouse, monkeypatch):
    wh = postgres_warehouse
    monkeypatch.setenv("CLOUDLENS_ENV_SUMMARY", "true")
    from app import jobs

    with (pytest.raises(RuntimeError, match="explicit version-1 migration"),
          wh.connect() as con, con.raw.transaction()):
        jobs.lock_generation(con)
        jobs.advance_generation(con, window=WINDOW)
    with wh.connect() as con:
        assert con.execute("SELECT count(*) FROM costs").fetchone()[0] == 0


def test_snapshot_survives_real_writer_generation_change(sidecar, monkeypatch):
    wh = sidecar
    monkeypatch.setenv("CLOUDLENS_ENV_SUMMARY", "true")
    summary.refresh(wh._store, WINDOW)
    with wh.reader(["sub-a"], repeatable_read=True) as reader:
        assert reader._con.execute("SHOW transaction_isolation").fetchone()[0] == "repeatable read"
        before = summary.filter_rows(reader, wh, "2026-07-04", 4, scope=True)
        change_costs(wh)
        assert summary.filter_rows(reader, wh, "2026-07-04", 4, scope=True) == before
    assert runtime_read(wh) is None


def test_conversion_refuses_unknown_nonzero_charges_even_when_they_cancel(sidecar, monkeypatch):
    wh = sidecar
    from app.warehouse import COLUMNS, insert_rows

    monkeypatch.setenv("FX_RATES", '{"USD": 1, "INR": 90}')
    with wh.connect() as con:
        values = dict.fromkeys(COLUMNS)
        values.update(ChargePeriodStart=WINDOW[0], SubAccountId="sub-a",
                      BillingCurrency="ZZZ", BillingCostBasis="FOCUS", Tags='{"env":"prod"}')
        rows = []
        for cost in (1.0, -1.0):
            values["BilledCost"] = cost
            rows.append([values[column] for column in COLUMNS])
        insert_rows(con, rows)
    monkeypatch.setenv("CLOUDLENS_ENV_SUMMARY", "true")
    summary.refresh(wh._store, WINDOW)
    with pytest.raises(ValueError, match="without authoritative FX"):
        runtime_read(wh, currency="USD")


def test_refresh_reads_the_physical_source_even_with_a_scoped_temp_view(sidecar):
    wh = sidecar
    with wh.connect(read_only=True, scope=["sub-a"]) as con, con.raw.transaction():
        for sql in summary.refresh_statements(wh._store.schema, WINDOW):
            con.execute(sql)
        assert ("dev", "USD", 50.0) in ranking(con, wh)


@pytest.mark.parametrize("backfilled", [False, True])
def test_converted_filter_route_matches_native_fx_without_unbounded_preflight(
        sidecar, monkeypatch, backfilled):
    import asyncio
    import json
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    from app import main
    from app.warehouse import Reader

    wh = sidecar
    monkeypatch.setenv("FX_RATES", '{"USD": 1, "INR": 90}')
    monkeypatch.setenv("CLOUDLENS_ENV_SUMMARY", "true")
    if backfilled:
        summary.refresh(wh._store, WINDOW)
    monkeypatch.setattr(main, "current_user", lambda request: SimpleNamespace(admin=True))
    monkeypatch.setattr(main, "permitted", AsyncMock(return_value=["sub-a"]))
    queries = []
    connection_currencies = []
    original = Reader.rows
    original_connect = wh.connect

    def capture_connect(*args, **kwargs):
        connection_currencies.append(kwargs.get("currency"))
        return original_connect(*args, **kwargs)

    def capture(reader, sql, limit=200):
        queries.append(sql)
        return original(reader, sql, limit)

    monkeypatch.setattr(Reader, "rows", capture)
    monkeypatch.setattr(wh, "connect", capture_connect)
    payload = json.loads(asyncio.run(main.filter_values(
        SimpleNamespace(), days=1, currency="USD", env_only=True)).body)
    assert "unavailable_dimensions" not in payload, payload
    assert {item["value"]: item["cost"] for item in payload["dimensions"]["env"]} == {
        "prod": 11.0, "qa": 2.0}
    assert connection_currencies == [None, None]
    if backfilled:
        assert not any('TRY_CAST("Tags"' in sql for sql in queries)
        assert any(summary.TABLE in sql for sql in queries)
    else:
        assert any('TRY_CAST("Tags"' in sql for sql in queries)
        assert any('AS missing FROM costs WHERE "ChargePeriodStart" >=' in sql for sql in queries)


def test_backfill_and_flag_changes_retire_filter_snapshots_without_invalidating_other_views(
        sidecar, monkeypatch):
    import asyncio
    from types import SimpleNamespace

    from starlette.requests import Request

    from app import main
    from app.auth import User

    wh = sidecar
    monkeypatch.setattr(main, "current_user", lambda request: User("operator", admin=True, source="env"))
    monkeypatch.setattr(main, "auth_service", lambda: SimpleNamespace(
        sso=SimpleNamespace(delegated=False),
        users=SimpleNamespace(secret_for=lambda name: "synthetic-local-secret")))
    from unittest.mock import AsyncMock

    monkeypatch.setattr(main, "permitted", AsyncMock(return_value=["sub-a"]))

    def context(path):
        request = Request({
            "type": "http", "method": "GET", "scheme": "http", "path": path,
            "server": ("localhost", 80), "query_string": b"", "headers": []})
        return asyncio.run(main.authorize_snapshot(request))

    monkeypatch.setenv("CLOUDLENS_ENV_SUMMARY", "false")
    disabled = context("/api/filters")
    executive = context("/api/dashboard/executive")
    monkeypatch.setenv("CLOUDLENS_ENV_SUMMARY", "true")
    unfilled = context("/api/filters")
    assert disabled.partition() != unfilled.partition()
    summary.refresh(wh._store, WINDOW)
    filled = context("/api/filters")
    assert filled.partition() != unfilled.partition()
    assert context("/api/dashboard/executive").partition() == executive.partition()


def test_ninety_day_route_preserves_raw_currency_buckets_and_conversion(sidecar, monkeypatch):
    import asyncio
    import json
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    from app import main

    wh = sidecar
    monkeypatch.setenv("FX_RATES", '{"USD": 1, "INR": 90}')
    with wh.connect() as con:
        con.execute(
            'INSERT INTO costs ("ChargePeriodStart","SubAccountId","Tags","BillingCurrency",'
            '"PricingCurrency","ExchangeRatePricingToBilling","BillingCostBasis","BilledCost") '
            "SELECT day::date, sub, json_build_object('Env', "
            "CASE WHEN sub='sub-a' THEN 'env-' || (i % 3)::text ELSE 'other-scope' END)::text, "
            "CASE WHEN i % 2=0 THEN 'USD' ELSE 'INR' END, 'USD', "
            "CASE WHEN i % 2=0 THEN 1.0 ELSE 80.0 END, 'FOCUS', "
            "CASE WHEN i % 2=0 THEN 1.0 ELSE 80.0 END "
            "FROM generate_series(DATE '2026-07-16', DATE '2026-10-13', INTERVAL '1 day') days(day) "
            "CROSS JOIN generate_series(1,200) charges(i) "
            "CROSS JOIN (VALUES ('sub-a'),('sub-b')) subscriptions(sub)")
    wh.refresh_rollup(strict=True)
    monkeypatch.setattr(main, "current_user", lambda request: SimpleNamespace(admin=True))
    monkeypatch.setattr(main, "permitted", AsyncMock(return_value=["sub-a"]))

    def result(currency):
        payload = json.loads(asyncio.run(main.filter_values(
            SimpleNamespace(), days=90, currency=currency, env_only=True)).body)
        assert "unavailable_dimensions" not in payload, payload
        return {item["value"]: item for item in payload["dimensions"]["env"]}

    monkeypatch.setenv("CLOUDLENS_ENV_SUMMARY", "false")
    native, converted = result(""), result("USD")
    monkeypatch.setenv("CLOUDLENS_ENV_SUMMARY", "true")
    for window in summary.backfill_windows(date(2026, 7, 16), date(2026, 10, 14)):
        summary.refresh(wh._store, window)
    assert result("") == native
    assert result("USD") == converted
    assert set(native) == {"env-0", "env-1", "env-2"}
    assert sum(item["cost"] for item in converted.values()) == 18000.0
