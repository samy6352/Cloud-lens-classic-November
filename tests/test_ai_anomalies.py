"""Offline daily anomaly scoring, bounded billing history and metric-window regressions."""
from __future__ import annotations

import asyncio
import copy
import json
import sys
from contextlib import contextmanager
from datetime import UTC, date, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app import ai_anomalies as signals
from app import ai_billing as ai
from app import reporting
from tests.test_ai_billing import NOW, A, B, billing, definitions, deployment, rid

FIRST = date(2026, 8, 31)
TODAY = date(2026, 9, 10)
WINDOW = {"from": "2026-08-31", "to": "2026-09-06", "days": 7}


def days(first=FIRST - timedelta(days=14), count=21, value=100):
    return {(first + timedelta(days=i)).isoformat(): value for i in range(count)}


def detect(daily, metric="cost", window=None, **kwargs):
    return signals.evaluate(metric, daily, window or WINDOW, TODAY, source="test evidence",
                            currency="USD" if metric == "cost" else None, **kwargs)


@pytest.mark.parametrize("metric,baseline,actual", [
    ("cost", 100, 200), ("cost", 100, 0), ("tokens", 4000, 8000), ("tokens", 4000, 0),
    ("utilization", 40, 80), ("utilization", 80, 20),
])
def test_material_spikes_and_drops_have_comparable_baselines(metric, baseline, actual):
    daily = days(value=baseline)
    daily[WINDOW["from"]] = actual
    flag = detect(daily, metric)["flags"][0]
    assert flag["metric"] == metric and flag["day"] == WINDOW["from"]
    assert flag["direction"] == ("up" if actual > baseline else "down")
    assert flag["actual"] == actual and flag["baseline"] == baseline
    assert flag["delta"] == actual - baseline
    assert flag["percent"] == round((actual - baseline) / baseline * 100, 1)
    assert flag["score"] >= 3.5
    assert flag["history_samples"] >= 7
    assert flag["baseline_to"] < flag["day"]
    assert flag["baseline_from"] >= (FIRST - timedelta(days=14)).isoformat()
    assert flag["unit"] == {"cost": "USD", "tokens": "tokens", "utilization": "percentage points"}[metric]


@pytest.mark.parametrize("metric,baseline,actual", [
    ("cost", 20, 29.99), ("cost", 100, 149.99), ("cost", 10, 24.99),
    ("tokens", 100, 1099), ("tokens", 10_000, 14_999), ("utilization", 20, 39.99),
    ("utilization", 80, 110), ("utilization", 10, 20),
])
def test_every_flag_requires_relative_and_absolute_floors(metric, baseline, actual):
    daily = days(value=baseline)
    daily[WINDOW["from"]] = actual
    assert detect(daily, metric)["flags"] == []


@pytest.mark.parametrize("metric,baseline,actual", [
    ("cost", 30, 45), ("cost", 30, 15), ("tokens", 2000, 3000),
    ("utilization", 40, 60), ("utilization", 40, 20),
])
def test_exact_materiality_and_fifty_percent_boundary_is_inclusive(metric, baseline, actual):
    daily = days(value=baseline)
    daily[WINDOW["from"]] = actual
    assert detect(daily, metric)["flags"][0]["actual"] == actual


def test_score_threshold_still_applies_when_relative_and_absolute_floors_pass():
    daily = days(value=100)
    for raw in list(daily)[:14]:
        daily[raw] = 10 if date.fromisoformat(raw).day % 2 else 200
    daily[WINDOW["from"]] = 220
    assert not any(flag["day"] == WINDOW["from"] for flag in detect(daily)["flags"])


def test_genuine_zero_baseline_new_activity_has_no_infinite_percent():
    daily = days(value=0)
    daily[WINDOW["from"]] = 100
    result = detect(daily)
    flag = result["flags"][0]
    assert flag["baseline"] == 0 and flag["percent"] is None and flag["direction"] == "up"
    assert json.dumps(result, allow_nan=False)


def test_real_zero_drop_is_distinct_from_absence_or_null():
    for value in (None, "absent", 0):
        daily = days()
        if value == "absent":
            del daily[WINDOW["from"]]
        else:
            daily[WINDOW["from"]] = value
        flags = detect(daily)["flags"]
        assert any(flag["day"] == WINDOW["from"] for flag in flags) == (value == 0)


def test_missing_dates_are_never_created_as_zero_observations():
    daily = days()
    for day in list(daily)[14:]:
        del daily[day]
    result = detect(daily)
    assert result["flags"] == [] and result["status"] == "unavailable"
    assert result["coverage"]["observed_days"] == 0
    assert result["coverage"]["missing_days"] == 5


def test_new_resource_is_insufficient_history_not_normal():
    result = detect(days(first=FIRST, count=5))
    assert result["status"] == "insufficient_history" and result["flags"] == []
    assert result["coverage"]["checked_days"] == 0
    row = {}
    signals.attach(row, [result])
    assert row["anomaly_status"] == "insufficient_history"
    assert "insufficient history" in row["anomaly_note"]


def test_minimum_history_is_observed_calendar_days_not_number_of_older_rows():
    daily = days(first=FIRST - timedelta(days=35), count=15)
    daily[WINDOW["from"]] = 500
    assert detect(daily)["status"] == "insufficient_history"
    daily = days(first=FIRST - timedelta(days=6), count=6)
    daily[WINDOW["from"]] = 500
    assert detect(daily)["flags"] == []
    daily[(FIRST - timedelta(days=7)).isoformat()] = 100
    assert detect(daily)["flags"][0]["history_samples"] == 7


def test_established_weekend_pattern_does_not_produce_repeating_drops():
    daily = {day: 20 if date.fromisoformat(day).weekday() >= 5 else 100 for day in days(count=42)}
    window = {"from": "2026-08-31", "to": "2026-09-20", "days": 21}
    result = signals.evaluate("cost", daily, window, date(2026, 9, 25), source="test", currency="USD")
    assert result["flags"] == []
    assert result["coverage"]["checked_days"] == 19
    daily["2026-09-05"] = 100
    result = signals.evaluate("cost", daily, window, date(2026, 9, 25), source="test", currency="USD")
    flag = next(flag for flag in result["flags"] if flag["day"] == "2026-09-05")
    assert flag["baseline"] == 20 and flag["baseline_samples"] == 4
    assert flag["history_samples"] == 14
    assert "same-kind" in flag["note"]


def test_negative_cost_days_are_excluded_without_negative_denominators():
    daily = days()
    daily[WINDOW["from"]] = -500
    daily["2026-08-25"] = -100
    result = detect(daily)
    assert not result["flags"]
    assert result["coverage"]["negative_days"] == 2
    assert "negative baseline" in result["note"]
    assert result["status"] == "partial"


def test_all_negative_history_cannot_establish_normal_cost_baseline():
    daily = days(value=-100)
    daily[WINDOW["from"]] = 1000
    result = detect(daily)
    assert result["status"] == "insufficient_history" and result["flags"] == []


@pytest.mark.parametrize("value", [float("nan"), float("inf"), True, "1000"])
def test_invalid_daily_values_are_not_alerts(value):
    daily = days()
    daily[WINDOW["from"]] = value
    assert not detect(daily)["flags"]


def test_only_selected_dates_are_flagged_and_two_reporting_tail_dates_are_excluded():
    daily = days()
    for day in ("2026-08-30", "2026-08-31", "2026-09-04", "2026-09-05", "2026-09-06"):
        daily[day] = 1000
    result = detect(daily)
    assert {flag["day"] for flag in result["flags"]} == {"2026-08-31", "2026-09-04"}
    assert result["coverage"]["excluded_tail_days"] == 2


def test_today_is_not_judged_for_metrics_even_if_synthetic_daily_value_exists():
    window = {"from": "2026-09-10", "to": "2026-09-10", "days": 1}
    result = detect(days(first=date(2026, 8, 27), count=15), "tokens", window)
    assert result["status"] == "unavailable" and result["flags"] == []


def test_mixed_currency_checks_are_independent_and_row_flags_are_capped():
    usd, eur = days(), days(value=10)
    usd[WINDOW["from"]] = 500
    eur[WINDOW["from"]] = 100
    one = detect(usd)
    two = signals.evaluate("cost", eur, WINDOW, TODAY, source="test", currency="EUR")
    row = {}
    signals.attach(row, [one, two])
    assert {flag["unit"] for flag in row["anomalies"]} == {"USD", "EUR"}
    many = copy.deepcopy(one)
    many["flags"] = [dict(one["flags"][0], day=f"2026-09-{day:02}") for day in range(1, 26)]
    signals.attach(row, [many])
    assert len(row["anomalies"]) == 20 and row["anomaly_status"] == "partial"
    assert row["anomalies"][0]["day"] == "2026-09-25"
    assert "omitted" in row["anomaly_note"]
    assert "Investigate" in row["recommendation"]


@pytest.fixture
def source(monkeypatch):
    state = SimpleNamespace(rows=[billing()], daily=[], queries=[], readers=[], hi="2026-09-06",
                            definitions=definitions(), deployments=[deployment()], requests=[])

    class Reader:
        def rows(self, sql, limit=200):
            state.queries.append((sql, limit, reporting.selected_month()))
            if "AS hi" in sql:
                # `_completed_data` selects two columns from one row: the latest
                # charge day, and the day the source was published. Returning only
                # `hi` models a row the query cannot produce, and the caller reads
                # `published_day` without a guard because it never has to.
                # Published the day after the last charge day, which is what an
                # import that completed a day's data looks like -- it makes `hi`
                # itself the last completed day, which is the window every test
                # below is written against.
                published = date.fromisoformat(state.hi) + timedelta(days=1)
                return [{"hi": state.hi, "published_day": published.isoformat()}]
            if "AS day" in sql:
                return copy.deepcopy(state.daily[:limit])
            return copy.deepcopy(state.rows[:limit])

    @contextmanager
    def reader(*, scope, currency, filters):
        state.readers.append((scope, currency, filters, reporting.selected_month()))
        yield Reader()

    async def request(method, url, **kwargs):
        assert method == "GET" and kwargs["attempts"] == 1 and kwargs["cache"] is False
        state.requests.append((url, kwargs["params"]))
        if url.endswith("/metricDefinitions"):
            return copy.deepcopy(state.definitions)
        if "/deployments" in url:
            return {"value": copy.deepcopy(state.deployments)}
        params = kwargs["params"]
        first, end = [datetime.fromisoformat(v.replace("Z", "+00:00")) for v in params["timespan"].split("/")]
        value = []
        for name in params["metricnames"].split(","):
            data = []
            stamp = first
            while stamp < end:
                data.append({"timeStamp": ai._iso(stamp), params["aggregation"].lower(): 100})
                stamp += timedelta(hours=1)
            value.append({
                "name": {"value": name}, "unit": "Percent" if name == ai.UTILIZATION else "Count",
                "timeseries": [{"metadatavalues": [{"name": {"value": ai.DEPLOYMENT_DIMENSION}, "value": "chat"}]
                               if "$filter" in params else [], "data": data}],
            })
        return {"interval": "PT1H", "value": value}

    monkeypatch.setitem(sys.modules, "app.warehouse", SimpleNamespace(
        warehouse=SimpleNamespace(reader=reader)))
    monkeypatch.setattr(reporting, "today", lambda: NOW.date())
    monkeypatch.setattr(ai, "_now", lambda: NOW)
    state.original = request
    state.request = AsyncMock(side_effect=request)
    monkeypatch.setattr(ai.azure, "request", state.request)
    with reporting.select(None):
        yield state


def cost_day(day, amount=100, **overrides):
    return {"subscription_id": A, "resource_id": rid(), "resource_name": "", "service": "",
            "currency": "USD", "day": day, "cost": amount, "billing_rows": 1,
            "cost_rows": 1, "negative_rows": 0, **overrides}


def report(**kwargs):
    return asyncio.run(ai.report([A], days=kwargs.pop("days", 7), **kwargs))


def test_cost_baseline_extends_seven_day_selection_without_changing_costs(source):
    source.daily = [cost_day(day, value) for day, value in days().items()]
    source.daily[14]["cost"] = 500
    result = report()
    row = result["services"][0]
    assert result["window"] == {**WINDOW, "timezone": "UTC", "bounds": "inclusive"}
    assert result["totals"] == [{"currency": "USD", "cost": 72.81}]
    assert row["cost"] == 72.81 and row["meters"][0]["quantity"] == 36_401_063
    assert row["anomalies"][0]["metric"] == "cost"
    assert row["anomalies"][0]["day"] == WINDOW["from"] and row["anomalies"][0]["actual"] == 500
    query = next(q for q, _, _ in source.queries if "AS day" in q)
    assert '"ChargePeriodStart" >= DATE \'2026-08-17\'' in query
    assert '"ChargePeriodStart" <= DATE \'2026-09-06\'' in query
    assert all(not key.startswith("_") for key in row)
    assert all(not key.startswith("_") for key in row["tokens"])


def test_calendar_month_baseline_only_temporarily_bypasses_month_filter(source):
    source.hi = "2026-09-06"
    filters = {"service": ["Azure AI Services"], "rg": ["one"]}
    with reporting.select("2026-08"):
        result = report(days=7, currency="EUR", filters=filters)
        assert reporting.selected_month() == "2026-08"
    assert result["window"]["from"] == "2026-08-01"
    assert result["window"]["to"] == "2026-08-31" and result["window"]["days"] == 31
    assert source.readers[:2] == [([A], "EUR", filters, "2026-08"), ([A], "EUR", filters, None)]
    daily = next((query, month) for query, _, month in source.queries if "AS day" in query)
    assert "2026-07-18" in daily[0] and daily[1] is None
    assert result["totals"] == [{"currency": "USD", "cost": 72.81}]


def test_cost_baseline_scope_identity_and_currencies_do_not_leak(source):
    source.rows += [billing(currency="EUR", cost=4)]
    source.daily = [cost_day(day, 100) for day in days()]
    source.daily += [cost_day(day, 10, currency="EUR") for day in days()]
    source.daily += [cost_day(day, 10000, subscription_id=B, resource_id=rid(sub=B)) for day in days()]
    for row in source.daily:
        if row["day"] == WINDOW["from"] and row["subscription_id"] == A:
            row["cost"] *= 5
    result = report()
    flags = result["services"][0]["anomalies"]
    assert {(flag["currency"], flag["actual"]) for flag in flags if flag["metric"] == "cost"} == {
        ("USD", 500), ("EUR", 50)}
    assert result["totals"] == [{"currency": "EUR", "cost": 4}, {"currency": "USD", "cost": 72.81}]


def test_missing_resource_id_uses_same_billing_identity_for_history(source):
    source.rows = [billing(resource_id="", resource_name="account-less", service="Foundry Tools")]
    source.daily = [cost_day(day, value, resource_id="", resource_name="account-less", service="Foundry Tools")
                    for day, value in days().items()]
    source.daily[14]["cost"] = 500
    result = report()
    assert result["services"][0]["anomalies"][0]["metric"] == "cost"
    source.request.assert_not_awaited()


@pytest.mark.parametrize("changes", [{"cost_rows": 0}, {"cost": None}, {"negative_rows": 1, "cost": 400}])
def test_incomplete_or_credit_containing_daily_cost_is_not_a_spike(source, changes):
    source.daily = [cost_day(day, value) for day, value in days().items()]
    source.daily[14].update(cost=1000)
    source.daily[14].update(changes)
    result = report()
    assert not any(flag["metric"] == "cost" for flag in result["services"][0]["anomalies"])
    check = next(check for check in result["services"][0]["anomaly_checks"] if check["metric"] == "cost")
    assert check["status"] == "partial"
    assert check["missing_days"] == 1


def test_daily_query_cap_rejects_all_partial_cost_signals(source, monkeypatch):
    monkeypatch.setattr(signals, "MAX_DAILY_GROUPS", 10)
    source.daily = [cost_day(day, value) for day, value in days().items()]
    source.daily[14]["cost"] = 500
    result = report()
    row = result["services"][0]
    assert not any(flag["metric"] == "cost" for flag in row["anomalies"])
    check = next(check for check in row["anomaly_checks"] if check["metric"] == "cost")
    assert check["status"] == "unavailable" and "partial source" in check["note"]
    assert result["totals"] == [{"currency": "USD", "cost": 72.81}]
    assert next(limit for q, limit, _ in source.queries if "AS day" in q) == 11


def test_selected_billing_cap_disables_cost_baseline_comparison(source, monkeypatch):
    monkeypatch.setattr(ai, "MAX_GROUPS", 1)
    source.rows.append(billing(service="Foundry Tools", cost=200))
    result = report()
    assert result["truncated"] and result["totals"] == [{"currency": "USD", "cost": 72.81}]
    assert not any("AS day" in query for query, _, _ in source.queries)
    assert "cap" in result["services"][0]["anomaly_checks"][0]["note"]


def test_duplicate_daily_billing_rows_disable_cost_comparison(source):
    source.daily = [cost_day(day, value) for day, value in days().items()]
    source.daily.append(copy.deepcopy(source.daily[0]))
    result = report()
    assert "Duplicate" in result["services"][0]["anomaly_checks"][0]["note"]
    assert result["services"][0]["anomaly_checks"][0]["status"] == "unavailable"


def test_history_read_failure_preserves_selected_billing(source, monkeypatch):
    import psycopg
    module = sys.modules["app.warehouse"]
    original = module.warehouse.reader

    @contextmanager
    def failed_history(**kwargs):
        if len(source.readers) == 1:
            raise psycopg.OperationalError("history unavailable")
        with original(**kwargs) as reader:
            yield reader
    monkeypatch.setattr(module.warehouse, "reader", failed_history)
    result = report()
    assert result["totals"] == [{"currency": "USD", "cost": 72.81}]
    assert "retained" in result["services"][0]["anomaly_checks"][0]["note"]


def test_extended_single_metric_read_does_not_change_selected_aggregate(source):
    result = report()
    row = result["services"][0]
    assert row["tokens"]["input"] == row["tokens"]["output"] == 7 * 24 * 100
    assert row["tokens"]["value"] == 33600 and row["tokens"]["samples"] == 168
    assert row["tokens"]["window"]["start"] == "2026-08-31T00:00:00Z"
    assert result["models"][0]["tokens"]["value"] == 33600
    assert len(source.requests) == 4
    metric_requests = [params for url, params in source.requests if url.endswith("/metrics")]
    assert all(params["timespan"] == "2026-08-17T00:00:00Z/2026-09-07T00:00:00Z"
               for params in metric_requests)
    checks = result["models"][0]["anomaly_checks"]
    assert checks[0]["status"] == "checked" and checks[0]["checked_days"] == 7
    assert checks[1]["status"] == "not_applicable"
    assert result["models"][0]["cost"] is None
    assert all(not key.startswith("_") for row in [*result["services"], *result["models"]]
               for metric in ("tokens", "utilization") for key in row[metric])


def test_account_and_exact_model_tokens_flag_daily_spike_and_real_zero_drop(source):
    async def varied(method, url, **kwargs):
        payload = await source.original(method, url, **kwargs)
        if url.endswith("/metrics"):
            for item in payload["value"]:
                for point in item["timeseries"][0]["data"]:
                    if point["timeStamp"].startswith("2026-08-31"):
                        point["total"] = 300
                    elif point["timeStamp"].startswith("2026-09-01"):
                        point["total"] = 0
        return payload
    source.request.side_effect = varied
    result = report()
    for row in [result["services"][0], result["models"][0]]:
        flags = [flag for flag in row["anomalies"] if flag["metric"] == "tokens"]
        assert {(flag["day"], flag["direction"], flag["actual"]) for flag in flags} == {
            ("2026-08-31", "up", 14400), ("2026-09-01", "down", 0)}
        assert row["tokens"]["value"] == 38400
        assert "Investigate" in row["recommendation"]


def test_incomplete_component_day_never_becomes_a_token_drop(source):
    async def missing(method, url, **kwargs):
        payload = await source.original(method, url, **kwargs)
        if url.endswith("/metrics"):
            for item in payload["value"]:
                if item["name"]["value"] == ai.OUTPUT:
                    item["timeseries"][0]["data"] = [
                        point for point in item["timeseries"][0]["data"]
                        if not point["timeStamp"].startswith("2026-08-31")]
        return payload
    source.request.side_effect = missing
    result = report()
    assert not result["models"][0]["anomalies"]
    check = result["models"][0]["anomaly_checks"][0]
    assert check["status"] == "partial" and check["missing_days"] == 1
    assert check["checked_days"] == 6


def test_consistent_total_fallback_is_used_without_mixing_partial_components(source):
    source.definitions["value"] = [d for d in source.definitions["value"] if d["name"]["value"] != ai.OUTPUT]
    async def total(method, url, **kwargs):
        payload = await source.original(method, url, **kwargs)
        if url.endswith("/metrics"):
            for item in payload["value"]:
                for point in item["timeseries"][0]["data"]:
                    if point["timeStamp"].startswith("2026-08-31"):
                        point["total"] = 300 if item["name"]["value"] == ai.TOTAL else 10000
        return payload
    source.request.side_effect = total
    result = report()
    flag = result["models"][0]["anomalies"][0]
    assert flag["actual"] == 7200 and flag["baseline"] == 2400
    assert "TokenTransaction" in flag["source"]
    assert result["models"][0]["tokens"]["value"] == 21600


def test_incomplete_days_do_not_switch_between_component_and_total_bases():
    window = {"start": "2026-08-31T00:00:00Z", "end": "2026-09-01T00:00:00Z", "days": 1}
    def part(value, daily):
        return {"value": value, "status": "ok", "samples": 24, "expected_samples": 24, "note": "", "_daily": daily}
    combined = ai._combine_tokens({
        ai.INPUT: {"": part(100, {"2026-08-30": 100, "2026-08-31": 100})},
        ai.OUTPUT: {"": part(100, {"2026-08-30": None, "2026-08-31": 100})},
        ai.TOTAL: {"": part(9999, {"2026-08-30": 9999, "2026-08-31": 9999})},
    }, "", window)
    assert combined["_daily"] == {"2026-08-30": None, "2026-08-31": 200}
    assert combined["value"] == 200


def test_daily_utilization_requires_full_24_hour_average_samples(source):
    source.definitions = definitions(utilization=True)
    source.deployments[0]["sku"]["name"] = "ProvisionedManaged"
    async def utilization(method, url, **kwargs):
        payload = await source.original(method, url, **kwargs)
        if url.endswith("/metrics") and kwargs["params"]["metricnames"] == ai.UTILIZATION:
            agg = kwargs["params"]["aggregation"].lower()
            for point in payload["value"][0]["timeseries"][0]["data"]:
                point[agg] = 20 if point["timeStamp"].startswith("2026-08-31") else 80
                if point["timeStamp"] == "2026-09-01T03:00:00Z":
                    point[agg] = None
        return payload
    source.request.side_effect = utilization
    result = report()
    for row in [result["services"][0], result["models"][0]]:
        flags = [flag for flag in row["anomalies"] if flag["metric"] == "utilization"]
        assert len(flags) == 1
        assert flags[0]["delta"] == -60 and flags[0]["unit"] == "percentage points"
        check = next(check for check in row["anomaly_checks"] if check["metric"] == "utilization")
        assert check["missing_days"] == 1


def test_retention_clips_only_extra_baseline_not_valid_selected_metrics(source):
    source.hi = "2026-06-16"
    result = report()
    row = result["services"][0]
    assert result["window"]["from"] == "2026-06-10"
    assert row["tokens"]["value"] == 33600
    metadata = result["anomaly_detection"]["metric_history_window"]
    assert metadata["start"] == "2026-06-10T00:00:00Z" and metadata["history_clipped"]
    assert result["models"][0]["anomaly_status"] == "insufficient_history"
    assert not result["models"][0]["anomalies"]


def test_bad_historical_timestamp_does_not_change_selected_total():
    window = {"start": "2026-08-31T00:00:00Z", "end": "2026-09-01T00:00:00Z", "days": 1}
    history = {**window, "start": "2026-08-30T00:00:00Z", "days": 2}
    first = datetime(2026, 8, 30, tzinfo=UTC)
    data = [{"timeStamp": ai._iso(first + timedelta(hours=index)), "total": 100} for index in range(48)]
    data.append(copy.deepcopy(data[0]))
    result = ai._series(data, "Total", window, history)
    assert result["value"] == 2400 and result["status"] == "ok"
    assert result["_daily"]["2026-08-30"] is None
    assert result["_daily"]["2026-08-31"] == 2400


def test_duplicate_selected_timestamp_rejects_daily_and_display_aggregation():
    window = {"start": "2026-08-31T00:00:00Z", "end": "2026-09-01T00:00:00Z", "days": 1}
    first = datetime(2026, 8, 31, tzinfo=UTC)
    data = [{"timeStamp": ai._iso(first + timedelta(hours=index)), "total": 100} for index in range(24)]
    data.append(copy.deepcopy(data[0]))
    result = ai._series(data, "Total", window)
    assert result["value"] is None and result["_daily"] == {}


def test_root_metadata_explains_thresholds_and_never_returns_raw_hourly_points(source):
    result = report()
    meta = result["anomaly_detection"]
    assert (meta["window_days"], meta["min_history"], meta["threshold"], meta["min_relative_change"]) == (14, 7, 3.5, 0.5)
    assert meta["materiality"]["cost"]["delta"] == 15
    assert meta["materiality"]["tokens"]["delta"] == 1000
    assert meta["materiality"]["utilization"]["delta"] == 20
    assert meta["coverage"]["rows"] == 2
    serialized = json.dumps(result, allow_nan=False)
    assert "timeStamp" not in serialized and '"_daily"' not in serialized
    assert len(serialized) < 16000


def test_billing_flags_survive_denied_azure_metrics(source):
    source.daily = [cost_day(day, value) for day, value in days().items()]
    source.daily[14]["cost"] = 500
    source.request.side_effect = ai.CostError("Forbidden")
    result = report()
    row = result["services"][0]
    assert row["anomalies"][0]["metric"] == "cost"
    assert row["anomaly_status"] == "partial" and row["tokens"]["value"] is None
    assert result["totals"] == [{"currency": "USD", "cost": 72.81}]


def test_new_deployment_with_few_complete_days_is_insufficient_not_normal(source):
    async def recent(method, url, **kwargs):
        payload = await source.original(method, url, **kwargs)
        if url.endswith("/metrics") and "$filter" in kwargs["params"]:
            for item in payload["value"]:
                item["timeseries"][0]["data"] = [
                    point for point in item["timeseries"][0]["data"]
                    if point["timeStamp"][:10] >= "2026-09-04"]
        return payload
    source.request.side_effect = recent
    result = report()
    model = result["models"][0]
    assert model["tokens"]["value"] == 14400
    assert model["anomaly_status"] == "insufficient_history" and not model["anomalies"]
    assert "insufficient history" in model["anomaly_note"]


def test_token_flags_survive_optional_deployment_timeout(source, monkeypatch):
    monkeypatch.setattr(ai, "ACCOUNT_TIMEOUT", 0.05)
    async def slow_split(method, url, **kwargs):
        if url.endswith("/metrics") and "$filter" in kwargs["params"]:
            await asyncio.sleep(1)
        payload = await source.original(method, url, **kwargs)
        if url.endswith("/metrics"):
            for item in payload["value"]:
                for point in item["timeseries"][0]["data"]:
                    if point["timeStamp"].startswith("2026-08-31"):
                        point["total"] = 300
        return payload
    source.request.side_effect = slow_split
    result = report()
    assert any(flag["metric"] == "tokens" for flag in result["services"][0]["anomalies"])
    assert result["models"][0]["anomaly_status"] == "unavailable"
    assert "time budget" in " ".join(result["services"][0]["notes"])


def test_large_but_finite_change_never_serializes_infinite_percent():
    daily = days(value=1e-308)
    daily[WINDOW["from"]] = 1e308
    result = detect(daily)
    assert result["flags"][0]["percent"] is None
    assert json.dumps(result, allow_nan=False)


def test_detector_does_not_call_existing_zero_filling_detect(monkeypatch):
    from app import anomalies
    monkeypatch.setattr(anomalies, "detect", lambda *_args, **_kwargs: pytest.fail("Do not zero-fill gaps"))
    daily = days()
    daily[WINDOW["from"]] = 500
    del daily["2026-09-01"]
    assert detect(daily)["flags"][0]["actual"] == 500


def test_sparse_baseline_zeroes_are_never_manufactured(source):
    source.daily = [cost_day("2026-08-30", 100), cost_day("2026-08-31", 500)]
    result = report()
    check = result["services"][0]["anomaly_checks"][0]
    assert check["status"] == "insufficient_history" and check["checked_days"] == 0
    assert not result["services"][0]["anomalies"]
