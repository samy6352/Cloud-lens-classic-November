"""The streaming importer's reconciliation must agree with the one it replaced.

`_reconcile_focus` compared complete row multisets in Python, which is why the
importer was capped at 100,000 rows: a real customer month is 5.8M and would need
several gigabytes of process memory before reconciliation even began. `staging`
asks the same question in SQL, where the rows already are.

That is only safe if the two agree exactly, so this is a differential test rather
than a demonstration: random multisets are mutated in every way a restatement can
mutate them and both implementations are asked which days changed.
"""
from __future__ import annotations

import os
import random
from collections import Counter

import pytest

pytestmark = pytest.mark.postgres

pytest.importorskip("psycopg")

pytestmark = [
    pytest.mark.postgres,
    pytest.mark.skipif(
        not os.getenv("CLOUDLENS_PG_TEST"),
        reason="set CLOUDLENS_PG_TEST=<host> to run against a PostgreSQL server"),
]

if os.getenv("CLOUDLENS_PG_TEST"):
    os.environ["PGHOST"] = os.environ["CLOUDLENS_PG_TEST"]

SUBS = ["sub-a", "sub-b"]
DAYS = ["2026-08-01", "2026-08-02", "2026-08-03"]


def _row(keys, sub, day, cost, resource):
    from app.warehouse import NUMERIC

    rec = [0.0 if name in NUMERIC else None for name in keys]
    rec[keys.index("SubAccountId")] = sub
    rec[keys.index("ChargePeriodStart")] = day
    rec[keys.index("BilledCost")] = float(cost)
    rec[keys.index("ResourceId")] = resource
    rec[keys.index("BillingCurrency")] = "USD"
    return rec


def _counter_changed(keys, stored, incoming):
    """The original algorithm, lifted out of `_reconcile_focus` unchanged."""
    i_sub, i_date = keys.index("SubAccountId"), keys.index("ChargePeriodStart")
    remaining = Counter(tuple(r) for r in incoming)
    changed = set()
    for r in stored:
        value = tuple(r)
        if remaining[value]:
            remaining[value] -= 1
        else:
            changed.add((r[i_sub], r[i_date]))
    changed.update((r[i_sub], r[i_date]) for r, n in remaining.items() if n)
    return sorted(changed)


def _random_rows(keys, rng, n):
    return [_row(keys, rng.choice(SUBS), rng.choice(DAYS), rng.randint(0, 3),
                 f"r{rng.randint(1, 4)}") for _ in range(n)]


def _mutate(keys, rng, rows):
    """Every way a restatement can differ from what is already stored."""
    out = [list(r) for r in rows]
    kind = rng.choice(["none", "add", "remove", "change", "duplicate", "dedupe"])
    if kind == "add":
        out.append(_random_rows(keys, rng, 1)[0])
    elif kind == "remove" and out:
        out.pop(rng.randrange(len(out)))
    elif kind == "change" and out:
        out[rng.randrange(len(out))][keys.index("BilledCost")] = 99.0
    elif kind == "duplicate" and out:
        out.append(list(rng.choice(out)))
    elif kind == "dedupe" and out:
        target = rng.choice(out)
        out.remove(target)
    return out, kind


def test_the_sql_reconciler_agrees_with_the_counter_it_replaced(postgres_warehouse):
    from app import staging
    from app.warehouse import COLUMNS, insert_rows

    keys = list(COLUMNS)
    i_date = keys.index("ChargePeriodStart")
    windows = {sub: [("2026-08-01", "2026-08-31")] for sub in SUBS}
    rng = random.Random(20260918)

    with postgres_warehouse.connect() as con:
        for case in range(60):
            stored = _random_rows(keys, rng, rng.randint(0, 12))
            incoming, kind = _mutate(keys, rng, stored)

            con.execute("BEGIN")
            con.execute("DELETE FROM costs")
            if stored:
                insert_rows(con, stored)

            staged = staging.Staged(lambda: con, f"t{case}")
            staged.create()
            staged.add(incoming)
            answer = staged.changed_days(windows, set())
            staged.drop()
            con.execute("ROLLBACK")

            # PostgreSQL hands dates back as DATE objects; the parser stages ISO
            # strings. `_reconcile_focus` normalised the same way before comparing.
            expected = _counter_changed(
                keys,
                [[str(v) if i == i_date else v for i, v in enumerate(r)] for r in stored],
                incoming)
            assert answer == expected, f"case {case} ({kind})"


def test_a_protected_day_is_never_reported_as_changed(postgres_warehouse):
    """A newer compact correction owns its day; a wider older run must not restate it."""
    from app import staging
    from app.warehouse import COLUMNS, insert_rows

    keys = list(COLUMNS)
    with postgres_warehouse.connect() as con:
        con.execute("BEGIN")
        con.execute("DELETE FROM costs")
        insert_rows(con, [_row(keys, "sub-a", "2026-08-01", 5, "r1"),
                          _row(keys, "sub-a", "2026-08-02", 5, "r1")])
        staged = staging.Staged(lambda: con, "prot")
        staged.create()
        # Both days differ from what is stored, but the first is spoken for.
        staged.add([_row(keys, "sub-a", "2026-08-01", 99, "r1"),
                    _row(keys, "sub-a", "2026-08-02", 99, "r1")])
        windows = {"sub-a": [("2026-08-01", "2026-08-31")]}
        assert staged.changed_days(windows, {("sub-a", "2026-08-01")}) == [
            ("sub-a", "2026-08-02")]
        staged.drop()
        con.execute("ROLLBACK")


def test_a_day_outside_the_declared_window_is_left_alone(postgres_warehouse):
    """History the export does not cover is not being restated, so it must not be touched."""
    from app import staging
    from app.warehouse import COLUMNS, insert_rows

    keys = list(COLUMNS)
    with postgres_warehouse.connect() as con:
        con.execute("BEGIN")
        con.execute("DELETE FROM costs")
        insert_rows(con, [_row(keys, "sub-a", "2026-07-15", 5, "r1")])
        staged = staging.Staged(lambda: con, "win")
        staged.create()
        staged.add([_row(keys, "sub-a", "2026-08-01", 5, "r1")])
        # July is stored and absent from the import, but the window is August only.
        assert staged.changed_days(
            {"sub-a": [("2026-08-01", "2026-08-31")]}, set()) == [("sub-a", "2026-08-01")]
        staged.drop()
        con.execute("ROLLBACK")
