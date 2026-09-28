"""Compact monthly serving must remain equivalent to the complete stored source."""
from __future__ import annotations

from contextlib import contextmanager, nullcontext
from datetime import date
from types import SimpleNamespace

import pytest

from app import billing, billing_serving, currency


def _row(**values):
    return {
        "ChargePeriodStart": "2026-08-01", "SubAccountId": "sub-a",
        "SubAccountName": "Subscription", "ResourceId": "/shared",
        "ResourceName": "Shared name", "ResourceGroup": "group",
        "ServiceName": "Compute", "RegionName": "west", "BillingCurrency": "USD",
        "BillingCostBasis": "FOCUS", "ChargeCategory": "Usage", "PricingModel": "OnDemand",
        "BilledCost": 10, "InvoiceCost": 10, "ListCost": 20, "ContractedCost": 15,
        "EffectiveCost": 10, "Tags": '{"env":"prod"}', **values,
    }


@pytest.fixture(scope="module")
def estate(module_postgres_warehouse):
    from app.warehouse import COLUMNS, insert_rows

    wh = module_postgres_warehouse
    data = []
    for sub in ("sub-a", "sub-b", "sub-c"):
        for code in ("USD", "EUR"):
            for i in range(215):
                cost = (i // 3) * (-1 if i % 2 else 1)
                data.append(_row(
                    SubAccountId=sub, BillingCurrency=code, ResourceId=f"/r/{i}",
                    BilledCost=cost, InvoiceCost=cost, ResourceName=None if i % 5 else "Same",
                    ServiceName="Compute" if i % 2 else "Storage",
                    CommitmentDiscountType="Reservation" if i % 2 else "SavingsPlan",
                    Tags='{"env":"dev"}' if i % 2 else '{"env":"prod"}',
                ))
            data.extend([
                _row(SubAccountId=sub, BillingCurrency=code, ResourceId="/r/214",
                     BilledCost=0, InvoiceCost=0, ResourceName="A name", ResourceGroup="a-group",
                     ChargePeriodStart="2026-08-20"),
                _row(SubAccountId=sub, BillingCurrency=code, ResourceId="/Å", BilledCost=70),
                _row(SubAccountId=sub, BillingCurrency=code, ResourceId="/É", BilledCost=-70),
                _row(SubAccountId=sub, BillingCurrency=code, ResourceId="", ResourceName="orphan"),
                _row(SubAccountId=sub, BillingCurrency=code, ResourceId=None, ResourceName=None,
                     ResourceGroup=None, ServiceName=None, BilledCost=9),
                _row(SubAccountId=sub, BillingCurrency=code, ResourceId=None, ResourceName=None,
                     ResourceGroup=None, ServiceName="", BilledCost=8),
                _row(SubAccountId=sub, BillingCurrency=code, ResourceId="/credit",
                     ChargeCategory="Credit", BilledCost=-5, InvoiceCost=-5),
                _row(SubAccountId=sub, BillingCurrency=code, ResourceId="/unused",
                     ChargeCategory="UnusedReservation", ListCost=None, ContractedCost=None),
                _row(SubAccountId=sub, BillingCurrency=code, ResourceId="/unknown",
                     BillingCostBasis=None, InvoiceCost=None, ListCost=None, EffectiveCost=None),
                _row(SubAccountId=sub, BillingCurrency=code, ResourceId="/prior",
                     ChargePeriodStart="2026-07-31", BilledCost=123456),
                _row(SubAccountId=sub, BillingCurrency=code, ResourceId="/future",
                     ChargePeriodStart="2026-09-01", BilledCost=987654),
            ])
    data.append(_row(SubAccountId=None, SubAccountName=None, BillingCurrency=None,
                     ResourceId=None, ResourceName=None, ResourceGroup=None, InvoiceCost=None))
    with wh.connect() as con:
        insert_rows(con, [[r.get(c) for c in COLUMNS] for r in data])
    wh.refresh_rollup(strict=True)
    table = '"_billing_serving_test"'
    with wh.connect() as con:
        con.execute(f"CREATE TABLE {table} AS {billing_serving.select_sql('billing_rollup')}")
    return wh, table


class Routed:
    def __init__(self, wh, serving=None):
        self.wh, self.serving, self.statements = wh, serving, []

    @contextmanager
    def reader(self, *args):
        with self.wh.reader(*args) as reader:
            # Match the warehouse contract: the serving relation is subscription-scoped.
            if self.serving and args[0]:
                reader._con.execute(
                    f'CREATE TEMP VIEW _serving_scoped AS SELECT * FROM {self.serving} '
                    'WHERE lower("SubAccountId") IN (SELECT id FROM _scope)')
                reader.bill_serving = "_serving_scoped"
            else:
                reader.bill_serving = self.serving
            original = reader.rows

            def rows(sql, limit):
                self.statements.append(sql)
                return original(sql, limit)

            reader.rows = rows
            yield reader


@pytest.fixture(autouse=True)
def fixed_fx_and_serving(monkeypatch):
    monkeypatch.setattr(billing, "_SERVING_ENABLED", True)
    monkeypatch.setenv("FX_RATES", '{"EUR": 0.8}')
    monkeypatch.setattr(currency, "_cache", {"at": 0, "rates": {}, "date": None, "source": None})


def _raw_equal(a, b):
    def key(row):
        return tuple(str(row.get(k)) for k in (
            "bucket", "day", "currency", "basis", "id", "subscription_id", "name"))

    aa, bb = sorted(a, key=key), sorted(b, key=key)
    assert len(aa) == len(bb)
    for actual, expected in zip(aa, bb):
        assert actual.keys() == expected.keys()
        for field, value in expected.items():
            if field in billing_serving.MEASURES:
                assert actual[field] == pytest.approx(value, rel=1e-12, abs=1e-8)
            else:
                assert actual[field] == value


@pytest.mark.postgres
@pytest.mark.parametrize(("scope", "month", "target", "limit"), [
    (None, "2026-08", "", 200),
    (["sub-a"], "2026-08", "USD", 200),
    (["sub-a", "sub-c"], "2026-08", "", 200),
    (["sub-b", "sub-c"], "2026-08", "USD", 3),
    (["nobody"], "2026-08", "", 200),
    (None, "2026-07", "", 200),
    (None, "2026-06", "", 200),
])
def test_serving_matches_all_measures_and_complete_payload(estate, monkeypatch, scope, month, target, limit):
    wh, table = estate
    monkeypatch.setattr(billing, "BREAKDOWN_LIMIT", limit)
    window = billing.period(month)
    candidate = Routed(wh, table)
    with candidate.reader(scope, None, None) as r:
        raw = r.rows(billing_serving.statement_sql(window, r.bill_serving, limit), 20000)
        complete = r.rows(billing._statement_sql(window, r.bill, stored=True), 20000)
    status = [r for r in raw if r["bucket"] == "__serving__"]
    assert len(status) == 1 and status[0]["total_groups"] == 1
    _raw_equal([r for r in raw if r["bucket"] != "__serving__"], complete)
    baseline = billing.build(scope, target, month, warehouse=Routed(wh))
    candidate.statements.clear()
    served = billing.build(scope, target, month, warehouse=candidate)
    assert served == baseline
    assert len(candidate.statements) == 1
    assert "__serving__" in candidate.statements[0]
    if month == "2026-08" and scope != ["nobody"]:
        resources = served["breakdowns"]["resources"]
        assert any(r["is_other"] and r["group_count"] > 0 for r in resources)
        assert not any(r["id"] in ("/prior", "/future") for r in resources)
        if served["fx"]["complete"]:
            assert sum(r["recorded_cost"] for r in resources) == pytest.approx(
                served["kpis"]["recorded_cost"], abs=0.01)
        else:
            for native in served["per_currency"]:
                code = native["currency"]
                if not code:
                    continue
                shown = [r for r in resources if r["currency"] == code]
                assert sum(r["recorded_cost"] for r in shown) == pytest.approx(
                    native["kpis"]["recorded_cost"], abs=0.01)


@pytest.mark.postgres
@pytest.mark.parametrize("filters", [
    {"service": ["Storage"]}, {"rg": ["group"]}, {"region": ["west"]},
    {"charge": ["Usage"]}, {"pricing": ["OnDemand"]}, {"env": ["prod"]},
])
def test_unsupported_filters_never_read_candidates(estate, filters):
    wh, table = estate
    wrapped = Routed(wh, table)
    actual = billing.build(["sub-a"], "", "2026-08", filters, warehouse=wrapped)
    expected = billing.build(["sub-a"], "", "2026-08", filters, warehouse=Routed(wh))
    assert actual == expected
    assert all("__serving__" not in sql for sql in wrapped.statements)


@pytest.mark.postgres
def test_partial_month_never_reads_monthly_candidates(estate, monkeypatch):
    wh, table = estate
    window = {**billing.period("2026-08"), "end": "2026-08-15", "days": 15, "partial": True}
    monkeypatch.setattr(billing, "statement_period", lambda *args: window)
    wrapped = Routed(wh, table)
    actual = billing.build(["sub-a"], warehouse=wrapped)
    assert actual == billing.build(["sub-a"], warehouse=Routed(wh))
    assert all("__serving__" not in sql for sql in wrapped.statements)


@pytest.mark.postgres
@pytest.mark.parametrize(("column", "bad", "good"), [
    ("candidate_limit", 199, 200), ("format_version", 0, billing_serving.FORMAT_VERSION),
    ("candidate_limit", None, 200),
])
def test_persisted_capacity_and_version_fail_closed(estate, column, bad, good):
    wh, table = estate
    try:
        with wh.connect() as con:
            con.execute(f'UPDATE {table} SET "{column}" = ?', [bad])
        wrapped = Routed(wh, table)
        actual = billing.build(["sub-a"], "", "2026-08", warehouse=wrapped)
        assert actual == billing.build(["sub-a"], "", "2026-08", warehouse=Routed(wh))
        assert len(wrapped.statements) == 2
        assert "__serving__" in wrapped.statements[0]
        assert "__serving__" not in wrapped.statements[1]
    finally:
        with wh.connect() as con:
            con.execute(f'UPDATE {table} SET "{column}" = ?', [good])


@pytest.mark.postgres
def test_increased_display_limit_uses_complete_source(estate, monkeypatch):
    wh, table = estate
    monkeypatch.setattr(billing, "BREAKDOWN_LIMIT", billing_serving.CANDIDATE_LIMIT + 1)
    wrapped = Routed(wh, table)
    actual = billing.build(["sub-a"], "", "2026-08", warehouse=wrapped)
    assert actual == billing.build(["sub-a"], "", "2026-08", warehouse=Routed(wh))
    assert len(wrapped.statements) == 2
    assert "__serving__" not in wrapped.statements[-1]


def test_windows_expand_merge_months_without_filling_gaps():
    assert billing_serving.build_windows(None) is None
    assert billing_serving.build_windows([]) == []
    assert billing_serving.build_windows([
        (date(2026, 8, 15), date(2026, 8, 16)),
        (date(2026, 7, 30), date(2026, 8, 1)),
        (date(2026, 11, 1), date(2026, 11, 2)),
    ]) == [(date(2026, 7, 1), date(2026, 9, 1)),
           (date(2026, 11, 1), date(2026, 12, 1))]
    with pytest.raises(ValueError):
        billing_serving.build_windows([(date(2026, 8, 2), date(2026, 8, 1))])
    sql = billing_serving.build_select_sql('"staged".billing_rollup',
                                         (date(2026, 8, 15), date(2026, 8, 16)))
    assert '"staged".billing_rollup' in sql
    assert ">= DATE '2026-08-01'" in sql and "< DATE '2026-09-01'" in sql
    assert "regexp_replace" not in sql
    assert billing_serving.serves_filters({"env": [], "rg": [""]})
    assert not billing_serving.serves_filters({"env": ["prod"]})


def test_serving_envelope_cannot_hide_a_truncated_payload():
    limits = []
    envelope = [{"bucket": "__serving__", "total_groups": 1}]
    envelope += [{"bucket": "daily"} for _ in range(20000)]

    def bounded_rows(sql, limit):
        limits.append(limit)
        return envelope[:limit]

    reader = SimpleNamespace(bill_serving="billing_serving", bill="billing_rollup",
                             bill_stored=True, rows=bounded_rows)
    wh = SimpleNamespace(reader=lambda *args: nullcontext(reader))
    with pytest.raises(ValueError, match="narrow the subscription scope"):
        billing.build(None, "", "2026-08", warehouse=wh)
    assert limits == [20001]
