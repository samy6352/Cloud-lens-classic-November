"""Preprocessing fences must preserve every financial measure and grain column."""
from datetime import date

import pytest

pytestmark = pytest.mark.postgres
WINDOW = (date(2024, 2, 1), date(2024, 3, 1))


@pytest.fixture
def source(postgres_warehouse):
    from app import billing_rollup

    numeric = {"BilledCost", "InvoiceCost", "ListCost", "ContractedCost", "EffectiveCost"}
    columns = billing_rollup.SOURCE_COLUMNS
    declarations = [
        f'"{name}" {"double precision" if name in numeric else "date" if name == "ChargePeriodStart" else "text"}'
        for name in columns
    ]
    with postgres_warehouse.connect() as con:
        con.execute(f'CREATE TEMP TABLE preprocessing_source ({", ".join(declarations)})')
        for i in range(480):
            values = {
                "ChargePeriodStart": [date(2024, 1, 31), date(2024, 2, 1),
                                      date(2024, 2, 29), date(2024, 3, 1), None][i % 5],
                "BillingCurrency": ["USD", " eur ", "", None][i % 4],
                "BillingCostBasis": ["FOCUS", "actual-cost", "AmortizedCost",
                                     "unknown", "", None][i % 6],
                "SubAccountId": ["Sub-A", "sub-a", None][i % 3],
                "SubAccountName": "Subscription",
                "ResourceId": ["Resource-A", "", None][i % 3],
                "ResourceName": f"resource-{i % 2}",
                "ResourceGroup": ["Group-A", None][i % 2],
                "ServiceName": ["Compute", None][i % 2],
                "RegionName": ["westus", None][i % 2],
                "ChargeCategory": ["Usage", "UnusedReservation", "UnusedSavingsPlan",
                                   "Purchase", "Refund", "Credit", "Tax", None][i % 8],
                "CommitmentDiscountStatus": ["Used", "UN-used", None][i % 3],
                "CommitmentDiscountType": ["Reserved Instances", "SavingsPlan", "other",
                                           None, "", "RI", "SavingsPlanForCompute"][i % 7],
                "PricingModel": ["OnDemand", "Spot", "Reservation", "SavingsPlan",
                                 "custom", None][i % 6],
                "BenefitId": ["benefit", "", None][i % 3],
            }
            for j, name in enumerate(sorted(numeric)):
                values[name] = [None, -8.5, 0.0, 3.125, 16.25,
                                float("inf"), float("-inf"), float("nan")][(i + j) % 8]
            con.execute(f'INSERT INTO preprocessing_source VALUES '
                        f'({", ".join("?" for _ in columns)})', [values[c] for c in columns])
        con.execute("ANALYZE preprocessing_source")
        yield con, "preprocessing_source"
        con.execute("DROP TABLE preprocessing_source")


def _reference(source, window):
    from app import billing, billing_rollup

    where = ""
    if window:
        lo, hi = window
        where = (f'WHERE "ChargePeriodStart" >= DATE \'{lo}\' '
                 f'AND "ChargePeriodStart" < DATE \'{hi}\'')
    dims = ", ".join(f'{expr} AS "{name}"' for name, expr in billing_rollup.GRAIN.items())
    grain = (f"{billing._sql_cte(None, source=source, where=where)} "
             f"SELECT {dims}, {billing._sql_measures()} FROM valued "
             f"GROUP BY {', '.join(str(i + 1) for i in range(len(billing_rollup.DIMENSIONS)))}")
    columns = ", ".join(
        [f'"{name}"' for name in billing_rollup.DIMENSIONS]
        + [f'CAST("{name}" AS double precision) AS "{name}"'
           for name in billing_rollup.MEASURES])
    return f"SELECT {columns} FROM ({grain}) reference"


@pytest.mark.parametrize("window", [None, WINDOW, (date(2023, 1, 1), date(2023, 2, 1))])
def test_every_financial_measure_and_grain_matches_existing_sql(source, window):
    from app import billing_rollup

    con, table = source
    actual, expected = billing_rollup.build_select_sql(table, window), _reference(table, window)
    assert con.execute(
        f"SELECT * FROM (({actual}) EXCEPT ALL ({expected})) differences").fetchall() == []
    assert con.execute(
        f"SELECT * FROM (({expected}) EXCEPT ALL ({actual})) differences").fetchall() == []
    result = con.execute(actual)
    assert [column[0] for column in result.description] == (
        billing_rollup.DIMENSIONS + billing_rollup.MEASURES)
    rows = result.fetchall()
    if window is None:
        assert any(row[0] is None for row in rows)
        assert sum(row[len(billing_rollup.DIMENSIONS)] for row in rows) == 480


def test_preprocessing_plan_does_not_expand_regex_into_aggregate_filters(source):
    from app import billing_rollup

    con, table = source
    plan = con.execute(
        "EXPLAIN (VERBOSE, FORMAT JSON) " + billing_rollup.build_select_sql(table, WINDOW)
    ).fetchone()[0][0]["Plan"]
    outputs = []
    aggregates = []

    def visit(node):
        outputs.extend(node.get("Output", []))
        if node["Node Type"] == "Aggregate":
            aggregates.extend(node.get("Output", []))
        for child in node.get("Plans", []):
            visit(child)

    visit(plan)
    assert aggregates
    assert not any("regexp_replace" in expression for expression in aggregates)
    # Normalization stays in the narrow input projection rather than being expanded
    # into every aggregate FILTER. EXPLAIN retains every selected measure.
    assert 5 <= sum(expression.count("regexp_replace") for expression in outputs) < 50


def test_preprocessing_preserves_ctas_types_and_half_open_date_window(source):
    from app import billing_rollup

    con, table = source
    con.execute("CREATE TEMP TABLE prepared_billing AS " + billing_rollup.build_select_sql(table, WINDOW))
    try:
        types = con.execute(
            "SELECT attname, format_type(atttypid, atttypmod) FROM pg_attribute "
            "WHERE attrelid = 'prepared_billing'::regclass AND attnum > 0 "
            "AND NOT attisdropped ORDER BY attnum").fetchall()
        assert types == (
            [(name, "date" if name == billing_rollup.PERIOD else "text")
             for name in billing_rollup.DIMENSIONS]
            + [(name, "double precision") for name in billing_rollup.MEASURES])
        days = {row[0] for row in con.execute(
            'SELECT DISTINCT "ChargePeriodStart" FROM prepared_billing').fetchall()}
        assert days == {date(2024, 2, 1), date(2024, 2, 29)}
    finally:
        con.execute("DROP TABLE prepared_billing")


def test_computed_fallback_still_pushes_date_filters_to_the_source(source):
    from app import billing_rollup

    con, table = source
    sql = billing_rollup.select_sql(table)
    assert "MATERIALIZED" not in sql
    assert "OFFSET 0" not in sql
    plan = con.execute(
        f"EXPLAIN (VERBOSE, FORMAT JSON) SELECT * FROM ({sql}) fallback "
        '''WHERE "ChargePeriodStart" >= DATE '2024-02-01'
        AND "ChargePeriodStart" < DATE '2024-03-01' '''
    ).fetchone()[0][0]["Plan"]
    scans = []

    def visit(node):
        if node.get("Relation Name") == table:
            scans.append(node)
        for child in node.get("Plans", []):
            visit(child)

    visit(plan)
    assert scans
    assert all("2024-02-01" in node["Filter"] and "2024-03-01" in node["Filter"]
               for node in scans)


@pytest.mark.parametrize("layout", ["flat", "monthly"])
def test_flat_and_monthly_rebuilds_explicitly_dispatch_to_preprocessing(
        source, postgres_warehouse, monkeypatch, layout):
    from app import billing_rollup, derived

    monkeypatch.setenv("CLOUDLENS_DERIVED_LAYOUT", layout)
    con, table = source
    statements = []

    class Recorded:
        def execute(self, sql, params=None):
            statements.append(sql)
            return con.execute(sql, params)

    recorded = Recorded()
    derived.rebuild(recorded, postgres_warehouse._rollup_schema, (billing_rollup,), table)
    ctas = [sql for sql in statements if sql.startswith("CREATE TABLE ") and " AS " in sql]
    assert len(ctas) == (4 if layout == "monthly" else 1)
    assert all("billing_prepared AS MATERIALIZED" in sql for sql in ctas)
    statements.clear()
    derived.rebuild(recorded, postgres_warehouse._rollup_schema, (billing_rollup,), table,
                    window=WINDOW)
    assert any(sql.startswith("INSERT INTO ") and "billing_prepared AS MATERIALIZED" in sql
               for sql in statements)


def test_rebuild_planner_choices_are_local_and_restored(source):
    from app import billing_rollup, derived

    con, _ = source

    def settings():
        return con.execute(
            "SELECT current_setting('enable_sort'), "
            "current_setting('enable_incremental_sort')").fetchone()

    before = settings()
    con.execute("BEGIN")
    try:
        with derived.build_options(con, billing_rollup):
            assert settings() == ("off", "off")
        assert settings() == before
        con.execute("COMMIT")
    except BaseException:
        con.execute("ROLLBACK")
        raise
    assert settings() == before


def test_failed_preprocessing_does_not_mask_error_or_leak_planner_settings(source):
    from psycopg.errors import UndefinedTable

    from app import billing_rollup, derived

    con, _ = source
    before = con.execute(
        "SELECT current_setting('enable_sort'), current_setting('enable_incremental_sort')"
    ).fetchone()
    con.execute("BEGIN")
    with pytest.raises(UndefinedTable), derived.build_options(con, billing_rollup):
        con.execute("SELECT * FROM missing_billing_preprocessing_test_relation")
    con.execute("ROLLBACK")
    assert con.execute(
        "SELECT current_setting('enable_sort'), current_setting('enable_incremental_sort')"
    ).fetchone() == before
