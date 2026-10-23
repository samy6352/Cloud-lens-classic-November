"""Offline contracts for the Environment summary design; no database or HDFC reads."""
from datetime import date, timedelta
from itertools import pairwise

import pytest

from app import environment_summary as summary

pytestmark = pytest.mark.offline


def test_coverage_matches_the_filter_exclusive_lower_and_inclusive_upper_edges():
    start, end = summary.coverage_window(date(2026, 7, 14), date(2026, 10, 12))

    assert start == date(2026, 7, 15)
    assert end == date(2026, 10, 13)
    assert (end - start).days == 90


def test_backfill_windows_are_contiguous_half_open_and_bounded():
    windows = list(summary.backfill_windows(
        date(2026, 7, 15), date(2026, 10, 13), chunk_days=7))

    assert windows[0] == (date(2026, 7, 15), date(2026, 7, 22))
    assert windows[-1][1] == date(2026, 10, 13)
    assert all((hi - lo).days <= 7 for lo, hi in windows)
    assert all(left[1] == right[0] for left, right in pairwise(windows))
    assert sum((hi - lo).days for lo, hi in windows) == 90


def test_coverage_requires_every_day_including_empty_days():
    start, end = date(2026, 7, 15), date(2026, 7, 19)
    complete = {start + timedelta(days=offset) for offset in range(4)}

    assert summary.coverage_complete(start, end, complete)
    assert not summary.coverage_complete(start, end, complete - {date(2026, 7, 17)})


def test_summary_select_parses_tags_once_and_keeps_exact_fx_provenance():
    sql = summary.select_sql(
        "costs", (date(2026, 7, 15), date(2026, 7, 22)))

    assert sql.count('TRY_CAST("Tags" AS JSON)') == 1
    assert 'sum("BilledCost") AS "BilledCost"' in sql
    for column in (
        "ChargePeriodStart", "SubAccountId", "Environment", "BillingCurrency",
        "PricingCurrency", "ExchangeRatePricingToBilling", "BillingCostBasis",
        "FxLegacyRatio",
    ):
        assert f'"{column}"' in sql
    assert '"ChargePeriodStart" >= DATE \'2026-07-15\'' in sql
    assert '"ChargePeriodStart" < DATE \'2026-07-22\'' in sql


def test_summary_schema_and_coverage_are_explicit_and_separate():
    table = summary.table_sql("cloudlens")
    coverage = summary.coverage_sql("cloudlens")
    index = summary.index_sql("cloudlens")

    assert '"cloudlens"."environment_summary"' in table
    assert '"FxLegacyRatio" double precision' in table
    assert '"cloudlens"."environment_summary_coverage"' in coverage
    assert '"ChargePeriodStart" date PRIMARY KEY' in coverage
    assert "CREATE INDEX IF NOT EXISTS" in index


def test_refresh_replaces_summary_and_marks_all_days_in_one_atomic_unit():
    statements = summary.refresh_statements(
        "cloudlens", (date(2026, 7, 15), date(2026, 7, 22)))

    assert len(statements) == 4
    assert statements[0].startswith('DELETE FROM "cloudlens"."environment_summary"')
    assert statements[1].startswith('INSERT INTO "cloudlens"."environment_summary"')
    assert statements[2].startswith(
        'DELETE FROM "cloudlens"."environment_summary_coverage"')
    assert statements[3].startswith(
        'INSERT INTO "cloudlens"."environment_summary_coverage"')
    assert "generate_series(DATE '2026-07-15'" in statements[3]
    assert "DATE '2026-07-22' - INTERVAL '1 day'" in statements[3]
    assert statements[1].count('TRY_CAST("Tags" AS JSON)') == 1


def test_invalidation_handles_known_windows_and_unknown_coverage():
    changed = summary.invalidate_sql(
        "cloudlens",
        [(date(2026, 7, 15), date(2026, 7, 16)),
         (date(2026, 9, 3), date(2026, 9, 4))])

    assert changed.startswith(
        'UPDATE "cloudlens"."environment_summary_coverage" SET dirty=true WHERE')
    assert changed.count('"ChargePeriodStart" >=') == 2
    assert " OR " in changed
    assert summary.invalidate_sql("cloudlens", None) == (
        'DELETE FROM "cloudlens"."environment_summary_coverage"')


def test_coverage_query_checks_for_any_missing_day():
    sql = summary.coverage_complete_sql(
        "cloudlens", date(2026, 7, 15), date(2026, 7, 22))

    assert sql.startswith("SELECT NOT EXISTS")
    assert "generate_series(DATE '2026-07-15'" in sql
    assert "DATE '2026-07-22' - INTERVAL '1 day'" in sql
    assert 'covered."ChargePeriodStart" IS NULL' in sql
    assert "AND NOT covered.dirty" in sql


def test_coverage_and_summary_reads_share_a_repeatable_read_snapshot():
    coverage = summary.coverage_complete_sql(
        "cloudlens", date(2026, 7, 15), date(2026, 10, 13))
    ranking = summary.ranked_sql(
        "cloudlens.environment_summary", date(2026, 7, 15), date(2026, 10, 13),
        scoped=True)

    assert summary.READ_TRANSACTION_SQL == (
        "BEGIN TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
    assert "environment_summary_coverage" in coverage
    assert "environment_summary" in ranking
    assert "lower(\"SubAccountId\")" in ranking


def test_rank_query_is_date_bounded_scoped_and_keeps_currency_buckets():
    sql = summary.ranked_sql(
        "cloudlens.environment_summary", date(2026, 7, 15), date(2026, 10, 13),
        scoped=True)

    assert 'FROM "cloudlens"."environment_summary"' in sql
    assert '"ChargePeriodStart" >= DATE \'2026-07-15\'' in sql
    assert '"ChargePeriodStart" < DATE \'2026-10-13\'' in sql
    assert 'lower("SubAccountId") = ANY(ARRAY(SELECT id FROM _scope))' in sql
    assert '"BillingCurrency" AS currency' in sql
    assert 'sum("BilledCost") AS c' in sql
    assert 'dense_rank() OVER' in sql


def test_rank_query_accepts_the_existing_fx_factor_without_merging_currency_buckets():
    factor = 'CASE WHEN "BillingCostBasis" = \'FOCUS\' THEN 1.0 ELSE "FxLegacyRatio" END'
    sql = summary.ranked_sql(
        "environment_summary", date(2026, 7, 15), date(2026, 10, 13),
        billing_factor=factor, target_currency="USD")

    assert 'FROM "public"."environment_summary"' in sql
    assert f'sum("BilledCost" * ({factor})) AS c' in sql
    assert "'USD'::text AS currency" in sql
    assert 'GROUP BY "Environment", currency' in sql


def test_rank_query_rejects_a_target_currency_without_a_conversion_factor():
    with pytest.raises(ValueError):
        summary.ranked_sql(
            "environment_summary", date(2026, 7, 15), date(2026, 10, 13),
            target_currency="USD")


@pytest.mark.parametrize("chunk_days", [0, 8, True])
def test_backfill_rejects_unbounded_or_invalid_chunk_sizes(chunk_days):
    with pytest.raises(ValueError):
        list(summary.backfill_windows(
            date(2026, 7, 1), date(2026, 8, 1), chunk_days=chunk_days))


def test_direct_refresh_rejects_an_unbounded_window():
    with pytest.raises(ValueError, match="at most 7 days"):
        summary.refresh_statements("cloudlens", (date(2026, 7, 1), date(2026, 7, 9)))


def test_feature_flag_defaults_off_and_rejects_invalid_values(monkeypatch):
    monkeypatch.delenv("CLOUDLENS_ENV_SUMMARY", raising=False)
    assert not summary.enabled()
    monkeypatch.setenv("CLOUDLENS_ENV_SUMMARY", "true")
    assert summary.enabled()
    monkeypatch.setenv("CLOUDLENS_ENV_SUMMARY", "maybe")
    with pytest.raises(ValueError, match="must be true or false"):
        summary.enabled()


def test_refresh_cannot_read_a_scoped_temporary_cost_view():
    sql = summary.refresh_statements("cloudlens", (date(2026, 7, 1), date(2026, 7, 2)))[1]
    assert 'WITH costs AS (SELECT * FROM "cloudlens"."costs")' in sql


def test_operator_preview_needs_no_database_and_apply_needs_acknowledgement(monkeypatch, capsys):
    import sys

    monkeypatch.delenv("PGHOST", raising=False)
    monkeypatch.setattr(sys, "argv", ["environment_summary", "--migrate"])
    summary.main()
    assert '"apply": false' in capsys.readouterr().out
    monkeypatch.setattr(sys, "argv", ["environment_summary", "--migrate", "--apply"])
    with pytest.raises(SystemExit) as error:
        summary.main()
    assert error.value.code == 2


def test_operator_refuses_implicit_multi_chunk_work(monkeypatch):
    import sys

    monkeypatch.setattr(sys, "argv", [
        "environment_summary", "--start", "2026-07-01", "--end", "2026-08-01"])
    with pytest.raises(SystemExit) as error:
        summary.main()
    assert error.value.code == 2


@pytest.mark.parametrize("identifier", ["schema; DROP TABLE costs", "bad.name", ""])
def test_sql_generation_rejects_untrusted_relation_identifiers(identifier):
    with pytest.raises(ValueError):
        summary.table_sql(identifier)
    with pytest.raises(ValueError):
        summary.select_sql(identifier)
