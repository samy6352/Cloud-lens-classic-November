"""The Uptime, Tags, Resources and AI Billing tabs answer the same from their summaries.

Each tab is built twice: once routed normally, reading `hourly_usage`, `usage_signals`,
`resource_tags`, the rollup and the presence table, and once with every one of those
forced back onto the detail table. The two payloads must agree -- which table answered
must never be visible in the answer. Tags are compared through the same lookup computed
from `costs`, so the stored monthly representative is held to the detail rows too.

The fixture is built to hit the awkward cases: a 30-day window that crosses a month
boundary, tags that change inside a month and outside the window, tag knowledge missing
on some days, resource ids that differ only by case, charges with no resource id, and two
billing currencies.
"""
from __future__ import annotations

import sys as _sys
from pathlib import Path as _Path

_sys.path.insert(0, str(_Path(__file__).resolve().parents[1]))

import os  # noqa: E402
import uuid  # noqa: E402
from contextlib import contextmanager  # noqa: E402
from datetime import date, timedelta  # noqa: E402

import pytest  # noqa: E402

pytestmark = pytest.mark.skipif(
    not os.getenv("CLOUDLENS_PG_TEST"),
    reason="set CLOUDLENS_PG_TEST=<host> to run against a PostgreSQL server",
)

if os.getenv("CLOUDLENS_PG_TEST"):
    os.environ["PGHOST"] = os.environ["CLOUDLENS_PG_TEST"]
    os.environ["PGDATABASE"] = os.getenv("CLOUDLENS_PG_DB", "postgres")
    os.environ["PGUSER"] = os.environ["CLOUDLENS_PG_USER"]
    os.environ["PGSCHEMA"] = f"cl_usage_{uuid.uuid4().hex[:8]}"

from tests.test_rollup_routing import _same  # noqa: E402

SUBS = [str(uuid.UUID(int=i)) for i in (11, 12, 13)]
END = date(2026, 9, 10)


def _rows():
    from app.warehouse import COLUMNS, NUMERIC

    out = []

    def add(day, sub, **values):
        base = {
            "SubAccountId": sub, "SubAccountName": f"name-{sub[-2:]}",
            "ChargePeriodStart": day.isoformat(), "BillingCurrency": "USD",
            "BillingCostBasis": "FOCUS", "ServiceFamily": "Compute",
            "ServiceName": "Virtual Machines", "RegionName": "eastus",
            "ResourceGroup": "rg", "ChargeCategory": "Usage", "PricingModel": "OnDemand",
            "BilledCost": 1.0, "CostInUsd": 1.0,
        }
        base.update(values)
        out.append([base.get(c, 0.0 if c in NUMERIC else None) for c in COLUMNS])

    for offset in range(45):
        day = END - timedelta(days=offset)
        for i, sub in enumerate(SUBS):
            vm = f"/subscriptions/{sub}/resourceGroups/rg/providers/Microsoft.Compute/virtualMachines/vm{i}"
            # Tags change on 1 Sep: the window's latest tags must win, not the greatest.
            tags = '{"env":"prod","owner":"zed"}' if day >= date(2026, 9, 1) else '{"env":"dev","owner":"ana"}'
            add(day, sub, ResourceId=vm if offset % 2 else vm.upper(), ResourceName=f"vm{i}",
                MeterName="D2s v3", UnitOfMeasure="1 Hour", PricingQuantity=24.0,
                BilledCost=2.5 + i, Tags=tags)
            add(day, sub, ResourceId=vm, ResourceName=f"vm{i}", MeterName="D2s v3 licence",
                UnitOfMeasure="10 Hours", PricingQuantity=2.4, BilledCost=0.4, Tags=tags,
                ServiceName="Virtual Machines Licenses")
            # Per-row tag variation within one day, as a job-tagged cluster produces.
            for job in range(2):
                add(day, sub, ResourceId=f"/subscriptions/{sub}/providers/databricks/ws{i}",
                    ResourceName=f"ws{i}", ServiceName="Azure Databricks",
                    ServiceFamily="Analytics", MeterName="DBU", UnitOfMeasure="1 Hour",
                    PricingQuantity=3.0, BilledCost=1.2,
                    Tags=f'{{"JobId":"{offset}{job}","team":"data"}}')
            # AI usage, billed in a second currency on one subscription.
            add(day, sub, ResourceId=f"/subscriptions/{sub}/providers/microsoft.cognitiveservices/accounts/ai{i}",
                ResourceName=f"ai{i}", ServiceName="Azure OpenAI", ServiceFamily="AI + Machine Learning",
                ProductName="Azure OpenAI", MeterName="gpt 1K tokens", UnitOfMeasure="1K",
                PricingQuantity=10.0, BilledCost=-0.5 if offset == 3 else 3.0,
                BillingCurrency="EUR" if i == 2 else "USD",
                Tags=None if offset in (5, 40) else '{"app":"chat"}',
                BillingCostBasis="ActualCost")
            # A subscription-level charge with no resource identity at all.
            add(day, sub, ResourceId=None, ResourceName=None, ServiceName="Support",
                ServiceFamily="Other", MeterName="Support", UnitOfMeasure="1 Month",
                PricingQuantity=1.0, BilledCost=0.1, Tags=None)
            # Never tagged. A missing tag value means the source did not say; an empty
            # document means it said "none". Knowledge is missing on offset 40 for every one
            # (outside the window) and on offset 5 for bare1 alone (inside it).
            unknown = offset == 40 or (i == 1 and offset == 5)
            add(day, sub, ResourceId=f"/subscriptions/{sub}/providers/storage/bare{i}",
                ResourceName=f"bare{i}", ServiceName="Storage", ServiceFamily="Storage",
                MeterName="LRS", UnitOfMeasure="1 GB/Month", PricingQuantity=5.0,
                BilledCost=0.7, Tags=None if unknown else "{}")
    return out


@pytest.fixture()
def wh(monkeypatch):
    schema = f"cl_usage_{uuid.uuid4().hex[:8]}"
    monkeypatch.setenv("PGSCHEMA", schema)

    import app.warehouse as module
    from app.warehouse import Warehouse, insert_rows

    warehouse = Warehouse()
    monkeypatch.setattr(module, "warehouse", warehouse)
    with warehouse.connect() as con:
        insert_rows(con, _rows())
    warehouse.refresh_rollup(strict=True)
    yield warehouse

    with warehouse._store.connect() as con:
        con.execute(f"DROP SCHEMA IF EXISTS {schema} CASCADE")
    warehouse._store.close()


def _plain(monkeypatch, wh):
    """Every summary this module routes to, forced back onto the detail table."""
    from app import resource_tags

    original = wh.reader

    @contextmanager
    def reader(*args, **kwargs):
        with original(*args, **kwargs) as r:
            r.agg = r.res = r.named = "costs"
            r.hourly = "costs" if r.hourly else None
            r.signals = None
            r.resource_costs = r.resource_ids = None
            if r.tags:
                r._con.execute(
                    f"CREATE TEMP VIEW plain_tags AS {resource_tags.select_sql('costs')}")
                r.tags = "plain_tags"
            yield r

    monkeypatch.setattr(wh, "reader", reader)


def _uptime(scope):
    from app import reporting, uptime

    with reporting.select(None):
        return uptime.hourly_cost(scope, days=30)


def _tags(scope, currency=None):
    from app import reporting, tags

    with reporting.select(None):
        return tags.cost_by_tag(scope, days=30, currency=currency)


def _resources(wh, scope):
    from app import reporting, resources

    service = resources.Resources.__new__(resources.Resources)
    service.wh = wh
    with reporting.select(None):
        return service.inventory(scope, days=30)


def _ai(scope):
    from app import ai_billing, reporting

    with reporting.select(None):
        return ai_billing._costs(scope, 30, None, None)


@pytest.mark.parametrize("scope", [None, SUBS, SUBS[:1]])
def test_every_rerouted_tab_is_the_same_either_way(wh, monkeypatch, scope):
    routed = (_uptime(scope), _tags(scope), _resources(wh, scope),
              _ai(scope or [s.lower() for s in SUBS]))
    _plain(monkeypatch, wh)
    plain = (_uptime(scope), _tags(scope), _resources(wh, scope),
             _ai(scope or [s.lower() for s in SUBS]))
    for name, a, b in zip(("uptime", "tags", "resources", "ai"), routed, plain):
        _same(a, b, name)


def test_the_routed_tabs_really_read_the_summaries(wh):
    from app import jobs

    with wh.connect(read_only=True) as con:
        fresh = jobs.fresh_derived_tables(con)
    assert {"hourly_usage", "usage_signals", "resource_tags"} <= fresh
    with wh.reader(scope=SUBS, usage=("hourly", "signals", "tags")) as r:
        assert (r.hourly, r.signals, r.tags) == ("hourly_usage", "usage_signals", "resource_tags")
        stored = r.rows('SELECT count(*) AS n FROM "hourly_usage"')[0]["n"]
        assert stored > 0


def test_each_uptime_meter_carries_exactly_its_own_daily_cells(wh):
    result = _uptime(SUBS)
    assert not result["daily_dropped"]
    meters = {row["key"]: row for row in result["resources"]}
    assert set(result["daily"]) == set(meters)
    for key, series in result["daily"].items():
        assert [cell[0] for cell in series] == sorted({cell[0] for cell in series}), "day order"
        assert sum(cell[1] for cell in series) == pytest.approx(meters[key]["hours"], abs=0.1)
        assert sum(cell[2] for cell in series) == pytest.approx(meters[key]["cost"], abs=1e-3)
        assert len(series) == meters[key]["days_billed"]


@pytest.mark.parametrize("scope", [None, SUBS[:1]])
def test_tags_money_and_daily_series_match_the_detail_table_query(wh, monkeypatch, scope):
    """The summary path groups per day first and carries each resource's cells inline;
    the `costs` path is the original single-pass query. Money, identity and every daily
    cell must agree -- only the tag text itself is sourced differently."""
    from app import warehouse as module

    routed = _tags(scope)
    original = wh.reader

    @contextmanager
    def detail_only(*args, **kwargs):
        with original(*args, **kwargs) as r:
            r.tags = None
            yield r

    monkeypatch.setattr(wh, "reader", detail_only)
    monkeypatch.setattr(module, "warehouse", wh)
    plain = _tags(scope)

    def shape(result):
        return (
            result["dates"], [round(v, 6) for v in result["daily_total"]],
            result["daily_dropped"], result["truncated"],
            [(r["id"], r["name"], r["service"], r["subscription"], r["subscription_id"],
              r["group"], round(r["cost"], 6), [[i, round(c, 6)] for i, c in r["d"]])
             for r in result["resources"]])

    assert routed["dates"], "the fixture must produce a daily series"
    assert shape(routed) == shape(plain)


def _untagged(scope, currency=None):
    from app import reporting, tags

    with reporting.select(None):
        return tags.untagged_summary(scope, days=30, currency=currency)


def _tag_figures(result):
    return (result["currency"], result["total"], result["untagged"], result["truncated"])


@pytest.mark.parametrize("cap", [None, 3])
@pytest.mark.parametrize("scope", [None, SUBS, SUBS[:1]])
def test_insights_untagged_summary_is_the_tags_tab_figures(wh, monkeypatch, scope, cap):
    """Insights reads three figures from the Tags tab; the slim summary must report
    exactly those -- routed, on the detail table, and when the tab truncates."""
    from app import tags

    if cap:
        monkeypatch.setattr(tags, "MAX_RESOURCES", cap)
    tab = _tags(scope)
    assert _tag_figures(_untagged(scope)) == _tag_figures(tab)
    if cap is None:
        assert tab["untagged"]["resources"] > 0, "the fixture must carry untagged spend"

    original = wh.reader

    @contextmanager
    def detail_only(*args, **kwargs):
        with original(*args, **kwargs) as r:
            r.tags = None
            yield r

    monkeypatch.setattr(wh, "reader", detail_only)
    assert _tag_figures(_untagged(scope)) == _tag_figures(_tags(scope))


def test_insights_untagged_summary_follows_the_display_currency(wh, monkeypatch):
    from app import currency

    monkeypatch.setattr(currency, "rate_to", lambda target: 83.0)
    assert _tag_figures(_untagged(SUBS, "INR")) == _tag_figures(_tags(SUBS, "INR"))
    assert _untagged([])["total"] == 0.0


STORAGE = {"service": ["Storage"]}
COMPUTE = {"service": ["Virtual Machines"]}


def _tags_filtered(scope, filters):
    from app import reporting, tags

    with reporting.select(None):
        return tags.cost_by_tag(scope, days=30, filters=filters)


def _uptime_filtered(scope, filters):
    from app import reporting, uptime

    with reporting.select(None):
        return uptime.hourly_cost(scope, days=30, filters=filters)


@pytest.mark.parametrize("scope", [None, SUBS[:1]])
def test_filter_bar_narrows_tags_to_exactly_the_matching_charges(wh, scope):
    """The Tags tab used to ignore the filter bar. Narrowed, it must hold only the
    resources with charges in the slice, each with the money it had unfiltered (every
    charge of those resources is in the slice), and totals that add up to them."""
    whole = _tags(scope)
    narrowed = _tags_filtered(scope, STORAGE)
    by_id = {r["id"]: r for r in whole["resources"]}

    assert {r["name"] for r in narrowed["resources"]} == (
        {"bare0", "bare1", "bare2"} if scope is None else {"bare0"})
    for row in narrowed["resources"]:
        assert row["cost"] == pytest.approx(by_id[row["id"]]["cost"])
        assert row["d"] == by_id[row["id"]]["d"]
    assert narrowed["total"] == pytest.approx(sum(r["cost"] for r in narrowed["resources"]), abs=0.01)
    assert narrowed["total"] < whole["total"]
    assert _tags_filtered(scope, None) == whole, "no selection must be the unfiltered answer"


def test_filter_bar_narrows_uptime_to_exactly_the_matching_meters(wh):
    whole = _uptime(SUBS)
    narrowed = _uptime_filtered(SUBS, COMPUTE)

    def resolved(payload, row):
        # `v` indexes this response's own value pool, which shrinks with the answer.
        return {**row, "v": [payload["tag_values"][i] for i in row.get("v", [])]}

    assert narrowed["resources"], "the fixture bills VM hours"
    assert {r["service"] for r in narrowed["resources"]} == {"Virtual Machines"}
    kept = {r["key"]: resolved(whole, r) for r in whole["resources"]
            if r["service"] == "Virtual Machines"}
    assert {r["key"]: resolved(narrowed, r) for r in narrowed["resources"]} == kept
    assert set(narrowed["daily"]) == set(kept)
    assert _uptime_filtered(SUBS, None) == whole


@pytest.mark.parametrize("filters", [STORAGE, COMPUTE, {"region": ["eastus"]},
                                     {"charge": ["Usage"], "pricing": ["OnDemand"]}])
def test_filtered_tags_and_uptime_are_the_same_from_summaries_or_detail(wh, monkeypatch, filters):
    """Under a filter the reader may answer from a summary (when its grain holds the
    filtered column) or from the filtered detail rows; which one must never show."""
    routed = (_uptime_filtered(SUBS, filters), _tags_filtered(SUBS, filters))
    _plain(monkeypatch, wh)
    plain = (_uptime_filtered(SUBS, filters), _tags_filtered(SUBS, filters))
    for name, a, b in zip(("uptime", "tags"), routed, plain):
        _same(a, b, f"{name} {filters}")


def test_tags_skips_and_names_filters_its_summary_cannot_serve(wh):
    """Charge type and pricing are not in the per-resource summary's grain; applying them
    put the tab on the detail table (timed out on the real estate). They are skipped and
    named, and the answer is exactly the one the served filters alone give."""
    whole = _tags(SUBS)
    skipped = _tags_filtered(SUBS, {"charge": ["Usage"], "pricing": ["OnDemand"], "env": []})
    assert skipped["filters_skipped"] == ["charge", "pricing"]
    assert {**skipped, "filters_skipped": []} == whole

    mixed = _tags_filtered(SUBS, {**STORAGE, "charge": ["Usage"]})
    assert mixed["filters_skipped"] == ["charge"]
    assert {**mixed, "filters_skipped": []} == _tags_filtered(SUBS, STORAGE)
    assert whole["filters_skipped"] == []


def test_latest_tags_in_the_window_win_across_a_month_boundary(wh):
    result = _tags(SUBS)
    vm = next(r for r in result["resources"] if r["name"] == "vm0")
    values = {result["values"][i] for i in vm["v"]}
    assert values == {"prod", "zed"}, "September's tags, not the lexically greatest"


def test_unknown_tag_days_count_only_inside_the_window(wh):
    result = _tags(SUBS)
    status = {r["name"]: r["tag_status"] for r in result["resources"]}
    assert status["bare0"] == "untagged", "an unknown day before the window must not count"
    assert status["bare1"] == "unknown", "an unknown day inside the window must"
    assert result["tags_not_loaded"] is True


def test_the_tag_summary_cannot_see_past_its_scope(wh):
    with wh.reader(scope=SUBS[:1], usage=("tags",)) as r:
        subs = {row["s"] for row in r.rows('SELECT DISTINCT lower("SubAccountId") AS s FROM resource_tags')}
    assert subs == {SUBS[0].lower()}


def test_stored_summaries_match_their_grain_over_costs(wh):
    from app import hourly_usage, resource_tags, usage_signals

    with wh.connect(read_only=True) as con:
        for module in (hourly_usage, usage_signals, resource_tags):
            names = (module.DIMENSIONS + module.MEASURES
                     + getattr(module, "ATTRIBUTES", []))
            cols = ", ".join(f'"{c}"' for c in names)
            stored = f'SELECT {cols} FROM "{wh._rollup_schema}".{module.TABLE}'
            computed = f"SELECT {cols} FROM ({module.select_sql('costs')}) AS c"
            for a, b in ((stored, computed), (computed, stored)):
                extra = con.execute(f"SELECT count(*) FROM ({a} EXCEPT ALL {b}) AS d").fetchone()[0]
                assert extra == 0, f"{module.TABLE} disagrees with costs"


def test_a_new_summary_is_built_alone_without_rebuilding_the_rest(wh):
    from app import derived, hourly_usage, jobs, rollup

    schema = wh._rollup_schema
    with wh.connect() as con:
        before = derived.active_table(con, schema, rollup)
        con.execute("BEGIN")
        jobs.unstamp(con, [hourly_usage.TABLE])
        con.execute(f'DROP VIEW "{schema}".{hourly_usage.TABLE}')
        for side in derived.SIDES:
            con.execute(f'DROP TABLE IF EXISTS "{schema}".{hourly_usage.TABLE}{side}')
        con.execute("COMMIT")
    with wh.connect(read_only=True) as con:
        assert jobs.rollup_current(con, rollup.TABLE)
        assert not jobs.rollup_current(con, hourly_usage.TABLE)
    assert not jobs.derived_complete()
    # The call startup makes: incremental, so only the recorded dirty periods and the
    # missing table are built. An explicit no-window refresh deliberately rebuilds all.
    wh.refresh_rollup(strict=True, incremental=True)

    with wh.connect(read_only=True) as con:
        assert derived.active_table(con, schema, rollup) == before, "rollup was rebuilt"
        assert jobs.rollup_current(con, hourly_usage.TABLE)
        assert con.execute(f'SELECT count(*) FROM "{schema}".{hourly_usage.TABLE}').fetchone()[0] > 0
    assert jobs.derived_complete()


def test_a_changed_build_filter_rebuilds_that_summary(wh, monkeypatch):
    from app import jobs, usage_signals

    monkeypatch.setattr(usage_signals, "layout", lambda: "changed-for-test")
    wh.refresh_rollup(strict=True)
    with wh.connect(read_only=True) as con:
        assert jobs.rollup_current(con, usage_signals.TABLE)
        recorded = con.execute("SELECT value FROM meta WHERE key = ?",
                               ["derived_layout:usage_signals"]).fetchone()[0]
    assert recorded == "changed-for-test"


RESOURCE_USAGE = ("tags", "resource_costs", "resource_ids")


def test_resources_ranks_on_the_hashed_tables_when_they_answer(wh):
    with wh.reader(scope=SUBS, usage=RESOURCE_USAGE) as r:
        assert (r.resource_costs, r.resource_ids) == ("resource_costs", "resource_ids")
        assert r.rows('SELECT count(*) AS n FROM resource_costs')[0]["n"] > 0


@pytest.mark.parametrize("cap", [1, 4, 7])
def test_a_truncated_resource_list_keeps_the_same_money_either_way(wh, monkeypatch, cap):
    """Ranked narrowly first on the fast path, in one pass on the other: at the row cap
    both must keep resources with the same money, and report every figure the same.
    Which of several equally-costed resources survives the cut may differ -- ties are
    broken by the hash on one path and by the id on the other."""
    from app import resources

    monkeypatch.setattr(resources, "MAX_RESOURCES", cap)
    routed = _resources(wh, SUBS)
    _plain(monkeypatch, wh)
    plain = _resources(wh, SUBS)

    def money(result):
        return sorted((r["cost"], r["prior_cost"]) for r in result["resources"])

    assert routed["truncated"] and plain["truncated"]
    assert money(routed) == money(plain)
    by_id = {(r["subscription_id"], r["id"]): r for r in plain["resources"]}
    for row in routed["resources"]:
        if (row["subscription_id"], row["id"]) in by_id:
            assert row == by_id[(row["subscription_id"], row["id"])]


@pytest.mark.parametrize("kwargs", [
    {"filters": {"charge": ["Usage"]}},  # not a column of the hashed grain
    {"currency": "INR"},                 # money there is only valid unconverted
])
def test_hashed_resource_tables_step_aside_when_they_cannot_answer(wh, monkeypatch, kwargs):
    from app import currency

    monkeypatch.setattr(currency, "rate_to", lambda target: 83.0)
    with wh.reader(scope=SUBS, usage=RESOURCE_USAGE, **kwargs) as r:
        assert r.resource_costs is None


def test_hashed_resource_tables_step_aside_when_stale(wh):
    from app import jobs

    with wh.connect() as con:
        con.execute("BEGIN")
        jobs.unstamp(con, ["resource_costs"])
        con.execute("COMMIT")
    with wh.reader(scope=SUBS, usage=RESOURCE_USAGE) as r:
        assert r.resource_costs is None, "an unstamped table must never be read"
    # And the tab still answers, from its own query.
    assert _resources(wh, SUBS)["resources"]


def test_hashed_resource_tables_cannot_see_past_their_scope(wh):
    with wh.reader(scope=SUBS[:1], usage=RESOURCE_USAGE) as r:
        for table in ("resource_costs", "resource_ids"):
            subs = {row["s"] for row in r.rows(
                f'SELECT DISTINCT lower("SubAccountId") AS s FROM {table}')}
            assert subs == {SUBS[0].lower()}, table


def test_hashed_resource_tables_match_their_grain_over_costs(wh):
    from app import resource_costs, resource_ids

    with wh.connect(read_only=True) as con:
        for module in (resource_costs, resource_ids):
            # Sums are re-added from the per-resource summary, so compare money to the
            # cent-fraction rather than to the last bit of a double.
            cols = ", ".join(
                f'round(CAST("{c}" AS numeric), 6)' if c in module.MEASURES else f'"{c}"'
                for c in module.DIMENSIONS + module.MEASURES)
            stored = f'SELECT {cols} FROM "{wh._rollup_schema}".{module.TABLE}'
            computed = f"SELECT {cols} FROM ({module.select_sql('costs')}) AS c"
            for a, b in ((stored, computed), (computed, stored)):
                extra = con.execute(f"SELECT count(*) FROM ({a} EXCEPT ALL {b}) AS d").fetchone()[0]
                assert extra == 0, f"{module.TABLE} disagrees with costs"


def test_hashed_resource_tables_are_built_from_the_published_presence(wh):
    from app import derived, jobs, presence, resource_costs, resource_ids

    schema = wh._rollup_schema
    with wh.connect() as con:
        before = derived.active_table(con, schema, presence)
        con.execute("BEGIN")
        jobs.unstamp(con, [resource_costs.TABLE, resource_ids.TABLE])
        for module in (resource_costs, resource_ids):
            con.execute(f'DROP VIEW "{schema}".{module.TABLE}')
            for side in derived.SIDES:
                con.execute(f'DROP TABLE IF EXISTS "{schema}".{module.TABLE}{side}')
        con.execute("COMMIT")
    wh.refresh_rollup(strict=True, incremental=True)

    with wh.connect(read_only=True) as con:
        assert derived.active_table(con, schema, presence) == before, "presence was rebuilt"
        for module in (resource_costs, resource_ids):
            assert jobs.rollup_current(con, module.TABLE)
            assert con.execute(
                f'SELECT count(*) FROM "{schema}".{module.TABLE}').fetchone()[0] > 0
    assert jobs.derived_complete()
