"""Process-isolated opt-in migration, JSON semantics and financial routing checks."""
# cloudlens-test: requires-postgres
from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
from datetime import date
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

TAGS = [
    '{"env":"PROD","Env":"dev","environment":"qa","Environment":"other"}',
    '{"env":"","Env":"prod"}', '{"env":null,"Env":"Dev"}',
    '{"environment":"QA"}', '{"Environment":"Prod"}',
    '{"env":null,"Env":null,"environment":null,"Environment":null}',
    None, "", "{bad", "null", '"prod"', "42", "true", '["prod"]',
    '{"env":["PROD",1]}', '{"env":{"Stage":"PROD"}}',
    '{"env":17}', '{"env":true}', '{"env":false}',
    '{"env":" Prod "}', '{"env":"ÄÉΣİ"}', '{"env":"o\'hare"}',
    '{"ENV":"ignored"}', '{"Env":false,"environment":"prod"}',
]
LEGACY = (
    "lower(COALESCE("
    "TRY_CAST(\"Tags\" AS JSON) #>> '{env}', "
    "TRY_CAST(\"Tags\" AS JSON) #>> '{Env}', "
    "TRY_CAST(\"Tags\" AS JSON) #>> '{environment}', "
    "TRY_CAST(\"Tags\" AS JSON) #>> '{Environment}', ''))"
)


def seed(wh):
    from app.warehouse import COLUMNS, insert_rows

    rows = []
    for sub in ("sub-a", "sub-b"):
        for month in (7, 8):
            for i, tags in enumerate(TAGS):
                values = {
                    "ChargePeriodStart": date(2026, month, i + 1),
                    "SubAccountId": sub, "SubAccountName": sub,
                    "ResourceId": f"/resource/{i % 3}", "ResourceName": f"Name {i % 3}",
                    "ServiceName": "Compute", "ServiceFamily": "Compute", "ResourceGroup": "rg",
                    "RegionName": "west", "BillingCurrency": "USD", "BillingCostBasis": "FOCUS",
                    "ChargeCategory": "Usage", "PricingModel": "OnDemand", "Tags": tags,
                    "BilledCost": 10.0, "InvoiceCost": 10.0, "CostInUsd": 10.0,
                    "EffectiveCost": 8.0, "ListCost": 20.0, "ContractedCost": 15.0,
                    "PricingQuantity": 1.0,
                }
                rows.append([values.get(c) for c in COLUMNS])
    shared = rows[0].copy()
    shared[list(COLUMNS).index("Tags")] = '{"env":"dev"}'
    rows.append(shared)
    rows.append([None if c != "BilledCost" else 7.0 for c in COLUMNS])
    with wh.connect() as con:
        insert_rows(con, rows)
    wh.refresh_rollup(strict=True)


def equivalent(con, actual, expected):
    for left, right in ((actual, expected), (expected, actual)):
        assert con.execute(
            f"SELECT * FROM (({left}) EXCEPT ALL ({right})) differences"
        ).fetchall() == []


def migrated(wh):
    from app import (
        billing,
        billing_rollup,
        billing_serving,
        derived,
        environment,
        jobs,
        presence,
        rollup,
    )
    from app.warehouse import DERIVED_MODULES

    default_cte = billing._sql_cte(None)
    assert 'SELECT "ChargePeriodStart", "BillingCostBasis"' in default_cte
    assert '"Environment"' not in default_cte
    for column in ('bad"name', "foo; DROP TABLE costs", "x.y", ""):
        try:
            billing._sql_cte(None, extra_columns=(column,))
        except ValueError:
            pass
        else:
            raise AssertionError("unsafe projection identifier accepted")
    old_totals = {}
    with wh.connect() as con:
        # Boot appends nullable Environment but retains old money and invalidates it.
        assert con.execute('SELECT sum("BilledCost") FROM costs_rollup').fetchone()[0] == 977
        assert not jobs.rollup_current(con)
        assert con.execute(
            'SELECT count(*) FROM costs_rollup WHERE "Environment" IS NOT NULL'
        ).fetchone()[0] == 0
        assert jobs.rebuild_windows(con, [m.TABLE for m in DERIVED_MODULES]) is None
        for module in (rollup, presence, billing_rollup):
            columns = con.execute(f'SELECT * FROM {module.TABLE} LIMIT 0').description
            assert columns[-1][0] == "Environment"
        for module in DERIVED_MODULES:
            measures = ", ".join(["count(*)", *(f'sum("{name}")' for name in module.MEASURES)])
            old_totals[module.TABLE] = con.execute(
                f"SELECT {measures} FROM {module.TABLE}"
            ).fetchone()

    with wh.reader(["sub-a"], filters={"env": ["prod"]}) as reader:
        assert not reader.bill_stored
        equivalent(reader._con, f'SELECT sum("BilledCost") FROM {reader.agg}',
                   'SELECT sum("BilledCost") FROM costs')
        assert reader.rows(f'SELECT sum("BilledCost") AS total FROM {reader.agg}')[0]["total"] == 40
    with patch.object(billing_serving, "build_select_sql", side_effect=RuntimeError("build failure")):
        try:
            wh.refresh_rollup(strict=True)
        except RuntimeError as exc:
            assert "build failure" in str(exc)
        else:
            raise AssertionError("failure was not exercised")
    with wh.connect() as con:
        assert not jobs.rollup_current(con)
        assert con.execute('SELECT sum("BilledCost") FROM costs_rollup').fetchone()[0] == 977
        for module in DERIVED_MODULES:
            measures = ", ".join(["count(*)", *(f'sum("{name}")' for name in module.MEASURES)])
            assert con.execute(
                f"SELECT {measures} FROM {module.TABLE}"
            ).fetchone() == old_totals[module.TABLE]
    wh.refresh_rollup(strict=True)
    with wh.connect() as con:
        assert jobs.rollup_current(con)
        # All JSON edge cases are compared inside PostgreSQL, not Python lower().
        con.execute('CREATE TEMP TABLE env_cases ("Tags" text)')
        for tags in TAGS:
            con.execute("INSERT INTO env_cases VALUES (?)", [tags])
        projection = environment.project("env_cases", ["Tags"])
        assert con.execute(
            f'SELECT count(*) FROM {projection} WHERE "Environment" IS DISTINCT FROM {LEGACY}'
        ).fetchone()[0] == 0
        for module in (rollup, presence, billing_rollup):
            equivalent(con, f"SELECT * FROM {module.TABLE}", module.select_sql("costs"))
        # Compact unfiltered serving consumes the finer grain without losing or
        # double-counting a resource that spans environments.
        equivalent(con, f"SELECT * FROM {billing_serving.TABLE}",
                   billing_serving.select_sql("billing_rollup"))
        assert con.execute('SELECT sum(charges) FROM costs_rollup').fetchone()[0] == 98
        con.execute('CREATE TEMP TABLE env_raw (LIKE costs) PARTITION BY RANGE ("ChargePeriodStart")')
        for month in (7, 8):
            con.execute(
                f"CREATE TEMP TABLE env_raw_{month} PARTITION OF env_raw "
                f"FOR VALUES FROM ('2026-{month:02d}-01') TO ('2026-{month + 1:02d}-01')")
        con.execute("CREATE TEMP TABLE env_raw_null PARTITION OF env_raw DEFAULT")
        con.execute("INSERT INTO env_raw SELECT * FROM costs")
        for module in (rollup, presence, billing_rollup):
            active = derived.active_table(con, wh._rollup_schema, module)
            plan = con.execute(
                f'EXPLAIN (FORMAT JSON) SELECT * FROM {module.TABLE} '
                '''WHERE "ChargePeriodStart" >= DATE '2026-08-01'
                AND "ChargePeriodStart" < DATE '2026-09-01' '''
            ).fetchone()[0]
            serialized = json.dumps(plan)
            assert f"{active}_202608" in serialized
            assert f"{active}_202607" not in serialized
            fallback = module.select_sql("env_raw")
            plan = con.execute(
                f'EXPLAIN (VERBOSE, FORMAT JSON) SELECT * FROM ({fallback}) source '
                '''WHERE "ChargePeriodStart" >= DATE '2026-08-01'
                AND "ChargePeriodStart" < DATE '2026-09-01' '''
            ).fetchone()[0]
            serialized = json.dumps(plan)
            assert "env_raw_8" in serialized, serialized
            assert "env_raw_7" not in serialized and "env_raw_null" not in serialized

    window = billing.period("2026-08")
    for filters in (None, {"env": ["prod"]}, {"env": ["prod", "dev"]},
                    {"env": ["prod"], "service": ["Compute"]},
                    {"env": ["o'hare"]}, {"env": ["missing"]},
                    {"env": ["prod"], "charge": ["Usage"]}):
        for scope in (None, ["sub-a"], ["not-permitted"]):
            with wh.reader(scope, filters=filters) as reader:
                con = reader._con
                if filters:
                    view = con.execute(
                        "SELECT pg_get_viewdef('pg_temp.costs_rollup'::regclass)"
                    ).fetchone()[0]
                    assert "try_cast_json" not in view and '"Tags"' not in view
                    assert "Environment" in view
                    assert reader.bill_serving is None
                assert reader.bill_stored
                equivalent(con,
                           f'SELECT sum("BilledCost"), coalesce(sum(charges), 0) FROM {reader.agg}',
                           'SELECT sum("BilledCost"), count(*)::numeric FROM costs')
                equivalent(con,
                           f'SELECT count(DISTINCT "ResourceId") FROM {reader.res}',
                           'SELECT count(DISTINCT "ResourceId") FROM costs')
                equivalent(con,
                           f'SELECT "ResourceName", sum("BilledCost") FROM {reader.named} '
                           'GROUP BY "ResourceName"',
                           'SELECT "ResourceName", sum("BilledCost") FROM costs GROUP BY "ResourceName"')
                equivalent(con, billing._statement_sql(window, reader.bill, stored=True),
                           billing._statement_sql(
                               window, f"({billing_rollup.select_sql('costs')}) raw_billing",
                               stored=False))
    # A restarted process below checks the same persistent generation again.
    from app import main
    from app.warehouse import Reader

    queries = []
    rows = Reader.rows

    def captured(reader, sql, limit=200):
        queries.append(sql)
        return rows(reader, sql, limit)

    with (patch.object(main, "current_user", return_value={}),
          patch.object(main, "permitted", new=AsyncMock(return_value=["sub-a"])),
          patch.object(Reader, "rows", captured)):
        response = asyncio.run(main.filter_values(SimpleNamespace(), days=365))
    payload = json.loads(response.body)
    assert "unavailable_dimensions" not in payload, payload
    values = {item["value"] for item in payload["dimensions"]["env"]}
    assert {"prod", "dev", "qa", "17", "true", "false", " prod ", "o'hare"} <= values
    env_query = next(query for query in queries if "AS env" in query)
    assert "FROM costs_rollup" in env_query
    assert "TRY_CAST" not in env_query and '"Tags"' not in env_query


def child(phase):
    from app.warehouse import warehouse as wh

    if phase == "legacy":
        seed(wh)
    elif phase == "migrate":
        migrated(wh)
    elif phase == "restart":
        from app import jobs
        with wh.connect() as con:
            assert jobs.rollup_current(con)
            assert con.execute('SELECT sum("BilledCost") FROM costs_rollup').fetchone()[0] == 977
    wh._store.close()


def run():
    for phase, enabled in (("legacy", "false"), ("migrate", "true"), ("restart", "true")):
        env = {**os.environ, "CLOUDLENS_ENV_ROLLUP": enabled,
               "CLOUDLENS_DERIVED_LAYOUT": "monthly", "CLOUDLENS_PARTITION_COSTS": "true"}
        result = subprocess.run([sys.executable, __file__, phase], env=env,
                                capture_output=True, text=True, timeout=100)
        assert result.returncode == 0, result.stdout + result.stderr
    for flag, expected in (("false", "already migrated"), ("typo", "must be true or false")):
        result = subprocess.run([sys.executable, __file__, "restart"],
                                env={**os.environ, "CLOUDLENS_ENV_ROLLUP": flag},
                                capture_output=True, text=True, timeout=30)
        assert result.returncode != 0 and expected in result.stderr, result.stderr
    print("Environment migration, restart, gate, JSON, routing and financial equivalence passed.")


if __name__ == "__main__":
    child(sys.argv[1]) if len(sys.argv) > 1 else run()
