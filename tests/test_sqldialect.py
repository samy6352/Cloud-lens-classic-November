"""The SQL translator, tested on what it must change and what it must not touch."""

from __future__ import annotations

import sys as _sys
from pathlib import Path as _Path

# Tests live in tests/ and import the application from the checkout root.
_sys.path.insert(0, str(_Path(__file__).resolve().parents[1]))

import pytest

from app.sqldialect import to_postgres


def test_round_on_a_float_picks_the_numeric_overload():
    # PostgreSQL has no round(double precision, int), which is the shape every
    # money column in the warehouse produces.
    out = to_postgres('SELECT round(sum("BilledCost"), 2) FROM costs')
    assert out == ('SELECT round((sum("BilledCost"))::numeric, 2)::double precision '
                   'FROM costs')


def test_a_rounded_value_comes_back_as_a_float_not_a_decimal():
    # Callers divide and compare these. Decimal / float raises, and it would
    # raise in a dashboard tab rather than here.
    assert to_postgres("SELECT round(x, 2) FROM t").endswith(
        "::double precision FROM t")


def test_single_argument_round_is_left_alone():
    assert to_postgres("SELECT round(x) FROM t") == "SELECT round(x) FROM t"


def test_nested_round_calls_are_both_translated():
    out = to_postgres("SELECT round(round(a, 3) + b, 2) FROM t")
    assert out == ("SELECT round((round((a)::numeric, 3)::double precision + b)"
                   "::numeric, 2)::double precision FROM t")


def test_strftime_becomes_to_char():
    out = to_postgres("SELECT strftime(\"ChargePeriodStart\", '%Y-%m') FROM costs")
    assert out == 'SELECT to_char("ChargePeriodStart", \'YYYY-MM\') FROM costs'


def test_strftime_quotes_literal_letters_in_the_template():
    # An unquoted letter in a to_char template is a pattern code, so a literal
    # 'T' between date and time would silently become something else.
    out = to_postgres("SELECT strftime(d, '%Y-%m-%dT%H:%M') FROM t")
    assert out == 'SELECT to_char(d, \'YYYY-MM-DD"T"HH24:MI\') FROM t'


def test_unknown_strftime_code_is_refused_rather_than_guessed():
    with pytest.raises(ValueError):
        to_postgres("SELECT strftime(d, '%j') FROM t")


def test_regexp_extract_becomes_substring():
    out = to_postgres("SELECT regexp_extract(s, '([^/]+)$') FROM t")
    assert out == "SELECT substring(s from '([^/]+)$') FROM t"


def test_regexp_matches_becomes_the_match_operator():
    out = to_postgres("SELECT 1 FROM t WHERE regexp_matches(lower(u), 'hour')")
    assert out == "SELECT 1 FROM t WHERE ((lower(u)) ~ ('hour'))"


def test_try_cast_double_becomes_the_shim():
    out = to_postgres("SELECT TRY_CAST(x AS DOUBLE) FROM t")
    assert out == "SELECT try_cast_double((x)::text) FROM t"


def test_placeholders_become_psycopg_markers():
    out = to_postgres('SELECT * FROM costs WHERE "SubAccountId" = ?')
    assert out == 'SELECT * FROM costs WHERE "SubAccountId" = %s'


def test_percent_signs_are_escaped_so_psycopg_leaves_them_alone():
    # psycopg scans the statement for markers only when parameters are passed,
    # so a LIKE pattern has to be escaped in that case or it is read as one.
    out = to_postgres("SELECT 1 FROM t WHERE name LIKE '%prod%'")
    assert out == "SELECT 1 FROM t WHERE name LIKE '%%prod%%'"


def test_percent_is_left_alone_when_there_are_no_parameters():
    # With no parameters psycopg sends the string verbatim, so escaping corrupts
    # it: `i % 365` went out as `i %% 365` and PostgreSQL rejected it with
    # "operator does not exist: integer %% integer".
    assert to_postgres("SELECT i % 365 FROM t", bind=False) == "SELECT i % 365 FROM t"
    assert to_postgres("SELECT 1 FROM t WHERE s LIKE '/subscriptions/%'", bind=False) == \
        "SELECT 1 FROM t WHERE s LIKE '/subscriptions/%'"


def test_questions_still_become_markers_without_binding():
    assert to_postgres("SELECT ? FROM t", bind=False) == "SELECT %s FROM t"


def test_a_translated_format_string_is_escaped_exactly_once():
    # to_char introduces no % of its own, but strftime's input has two, and they
    # must not survive into the statement psycopg sees.
    out = to_postgres("SELECT strftime(d, '%Y-%m') FROM t WHERE n LIKE '%a%'")
    assert "%Y" not in out
    assert out.count("%%") == 2


def test_type_names_are_translated():
    out = to_postgres('CREATE TABLE t ("a" DOUBLE, "b" VARCHAR)')
    assert out == 'CREATE TABLE t ("a" double precision, "b" text)'


def test_an_already_translated_double_is_not_translated_twice():
    # round() emits its own ::double precision cast, and rewriting the DOUBLE in
    # that produced "double precision precision".
    assert "precision precision" not in to_postgres("SELECT round(x, 2) FROM t")
    assert to_postgres("SELECT x::double precision FROM t") == \
        "SELECT x::double precision FROM t"


# ------------------------------------------------------------------ leave alone
#
# The difference between a translator and a find-and-replace. Azure resource
# names, tag values and meter descriptions are arbitrary text, and any of the
# strings below occur in real billing data.

def test_a_literal_that_looks_like_a_function_call_is_not_rewritten():
    sql = "SELECT 'round(x, 2)' AS label FROM t"
    assert to_postgres(sql) == sql


def test_a_question_mark_inside_a_literal_is_data():
    sql = "SELECT 1 FROM t WHERE \"MeterName\" = 'What?'"
    assert to_postgres(sql) == sql


def test_a_column_named_like_a_function_is_not_rewritten():
    sql = 'SELECT "strftime" FROM t'
    assert to_postgres(sql) == sql


def test_an_escaped_quote_does_not_end_the_literal():
    # 'it''s round(a, 2)' is one literal containing an apostrophe. Reading the
    # doubled quote as a terminator would expose the rest of it to rewriting.
    sql = "SELECT 'it''s round(a, 2)' FROM t"
    assert to_postgres(sql) == sql


def test_a_comment_is_not_rewritten():
    sql = "SELECT 1 -- round(a, 2) and a ? too\nFROM t"
    assert to_postgres(sql) == sql


def test_a_parenthesis_inside_a_literal_does_not_end_the_call():
    out = to_postgres("SELECT round(coalesce(x, 0) + length(')'), 2) FROM t")
    assert out == ("SELECT round((coalesce(x, 0) + length(')'))::numeric, 2)"
                   "::double precision FROM t")


def test_a_comma_inside_a_literal_does_not_split_arguments():
    out = to_postgres("SELECT round(coalesce(x, ','), 2) FROM t")
    assert out == ("SELECT round((coalesce(x, ','))::numeric, 2)"
                   "::double precision FROM t")


def test_ordinary_portable_sql_is_untouched():
    sql = ('SELECT "ServiceName", sum("BilledCost") AS cost FROM costs '
           'WHERE "ChargePeriodStart" >= DATE \'2026-01-01\' '
           'GROUP BY 1 ORDER BY cost DESC LIMIT 10')
    assert to_postgres(sql) == sql


# --------------------------------------------------------------------- intervals
#
# Every dashboard window is `date '...' - INTERVAL <n> DAY`. DuckDB takes a bare
# number and a bare keyword; PostgreSQL rejects it with "syntax error at or near
# <n>", which is what broke the cost breakdown, tags, uptime, AKS and commitments
# panels on PostgreSQL while the header totals kept working.

def test_a_day_interval_becomes_a_literal():
    out = to_postgres("SELECT * FROM t WHERE d > date '2026-09-02' - INTERVAL 30 DAY")
    assert out == "SELECT * FROM t WHERE d > date '2026-09-02' - INTERVAL '30 day'"


def test_the_plural_unit_is_singularised():
    assert to_postgres("SELECT INTERVAL 2 DAYS") == "SELECT INTERVAL '2 day'"


def test_other_units_are_translated_too():
    assert to_postgres("SELECT INTERVAL 3 MONTH") == "SELECT INTERVAL '3 month'"
    assert to_postgres("SELECT INTERVAL 1 YEAR") == "SELECT INTERVAL '1 year'"
    assert to_postgres("SELECT INTERVAL 12 HOURS") == "SELECT INTERVAL '12 hour'"


def test_lower_case_is_translated():
    assert to_postgres("SELECT interval 7 day") == "SELECT INTERVAL '7 day'"


def test_an_interval_that_is_already_a_literal_is_left_alone():
    sql = "SELECT INTERVAL '30 day'"
    assert to_postgres(sql) == sql


def test_an_interval_inside_a_string_is_data():
    sql = "SELECT 'INTERVAL 30 DAY' AS note FROM t"
    assert to_postgres(sql) == sql


def test_an_expression_interval_is_left_for_postgres_to_reject():
    # Deliberately not guessed at: the translator declines rather than inventing
    # a cast that might change the meaning.
    sql = "SELECT INTERVAL (x) DAY"
    assert to_postgres(sql) == sql
