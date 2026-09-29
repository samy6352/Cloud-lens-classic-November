"""Hermetic scoped AI billing tests: fake reader and mocked read-only ARM responses."""
from __future__ import annotations

import asyncio
import copy
import sys
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest

from app import ai_anomalies, cost, reporting
from app import ai_billing as ai

A = "11111111-1111-1111-1111-111111111111"
B = "22222222-2222-2222-2222-222222222222"
NOW = datetime(2026, 9, 10, 10, tzinfo=UTC)
START = datetime(2026, 9, 9, tzinfo=UTC)
WINDOW = {"start": "2026-09-09T00:00:00Z", "end": "2026-09-10T00:00:00Z", "days": 1}


def rid(name="ai", sub=A, provider="Microsoft.CognitiveServices/accounts"):
    return f"/subscriptions/{sub}/resourceGroups/rg/providers/{provider}/{name}".lower()


def billing(name="ai", sub=A, **overrides):
    return {
        "subscription_id": sub, "subscription": "Test subscription", "resource_id": rid(name, sub),
        "resource_name": name, "service": "Azure AI Services", "product": "Azure OpenAI GPT5",
        "meter": "5.6 terra ShortCo Inp Std Gl 1M Tokens", "unit": "Units",
        "currency": "USD", "cost": 72.81, "billing_rows": 1, "cost_rows": 1,
        "quantity": 36_401_063, **overrides,
    }


def definitions(split=True, utilization=False):
    values = []
    for name in (ai.INPUT, ai.OUTPUT, ai.TOTAL, *([ai.UTILIZATION] if utilization else [])):
        util = name == ai.UTILIZATION
        values.append({
            "name": {"value": name}, "unit": "Percent" if util else "Count",
            "supportedAggregationTypes": ["Average", "Maximum"] if util else ["Total"],
            "dimensions": [{"value": ai.DEPLOYMENT_DIMENSION}] if split else [],
        })
    return {"value": values}


def deployment(name="chat", account="ai", sub=A, sku="GlobalStandard", model="gpt-5"):
    return {
        "id": f"{rid(account, sub)}/deployments/{name}", "name": name, "sku": {"name": sku},
        "properties": {"model": {"name": model, "version": "2026-01-01", "format": "OpenAI"}},
    }


def series(values=None, *, aggregation="Total", name=None):
    values = [0] * 24 if values is None else values
    return {
        "metadatavalues": [] if name is None else [
            {"name": {"value": ai.DEPLOYMENT_DIMENSION}, "value": name}],
        "data": [
            {"timeStamp": ai._iso(START + timedelta(hours=index)), aggregation.lower(): value}
            for index, value in enumerate(values)
        ],
    }


def metric(name=ai.INPUT, values=None, *, aggregation="Total", split=False):
    return {"name": {"value": name}, "unit": "Percent" if name == ai.UTILIZATION else "Count",
            "timeseries": [series(values, aggregation=aggregation, name="chat" if split else None)]}


def run(scope=None, **kwargs):
    return asyncio.run(ai.report([A] if scope is None else scope, days=kwargs.pop("days", 1), **kwargs))


@pytest.fixture
def backend(monkeypatch):
    state = SimpleNamespace(rows=[billing()], daily=[], hi="2026-09-09",
                            published_day="2026-09-10", queries=[], readers=[],
                            definitions=definitions(), deployments=[deployment()])

    class Reader:
        def rows(self, sql, limit=200):
            state.queries.append((sql, limit, reporting.selected_month()))
            if "AS hi" in sql:
                return [{"hi": state.hi, "published_day": state.published_day}]
            if "AS day" in sql:
                return copy.deepcopy(state.daily[:limit])
            return copy.deepcopy(state.rows[:limit])

    @contextmanager
    def reader(*, scope, currency=None, filters=None, **options):
        state.readers.append((scope, currency, filters, reporting.selected_month(), cost._caller_id.get()))
        yield Reader()

    async def request(method, url, *, params, attempts, cache):
        assert method == "GET"
        assert url.startswith(cost.ARM + "/subscriptions/" + A + "/")
        assert attempts == 1 and cache is False
        if url.endswith("/metricDefinitions"):
            assert params == {"api-version": "2018-01-01"}
            return copy.deepcopy(state.definitions)
        if "/deployments" in url:
            return {"value": copy.deepcopy(state.deployments)}
        assert url.endswith("/metrics")
        assert params["api-version"] == "2023-10-01"
        assert params["interval"] == "PT1H" and params["AutoAdjustTimegrain"] == "false"
        assert params["timespan"] == f"2026-08-26T00:00:00Z/{WINDOW['end']}"
        return {
            "interval": "PT1H",
            "value": [metric(name, aggregation=params["aggregation"], split="$filter" in params)
                      for name in params["metricnames"].split(",")],
        }

    state.request = AsyncMock(side_effect=request)
    state.original_request = request
    monkeypatch.setattr(ai, "_now", lambda: NOW)
    monkeypatch.setattr(reporting, "today", lambda: NOW.date())
    monkeypatch.setattr(ai.azure, "request", state.request)
    monkeypatch.setitem(sys.modules, "app.warehouse", SimpleNamespace(
        warehouse=SimpleNamespace(reader=reader)))
    with reporting.select(None):
        yield state


@pytest.mark.parametrize("row", [
    {"ServiceFamily": "AI and Machine Learning"}, {"ServiceFamily": "AI + Machine Learning"},
    {"ServiceName": "Azure AI Services"}, {"ServiceName": "Foundry Tools"},
    {"ServiceName": "Foundry Models"}, {"ServiceName": "CognitiveServices", "ServiceFamily": "Other"},
    {"ServiceName": "Azure OpenAI"}, {"ServiceName": "Azure AI Search"},
    {"ServiceName": "Azure Search"}, {"ServiceName": "Bing Search"},
    {"ServiceName": "Azure Machine Learning"}, {"ProductName": "Grounding with Bing"},
    {"ProductName": "Az Form Recognizer"}, {"ProductName": "Defender for AI"},
    {"ResourceId": rid()}, {"ResourceId": rid(provider="Microsoft.Search/searchServices")},
    {"ResourceId": rid(provider="Microsoft.MachineLearningServices/workspaces")},
])
def test_ai_classifier_covers_focus_quick_and_old_cognitive_labels(row):
    assert ai._is_ai(row)
    assert all(value.lower() in ai._ai_predicate().lower() or key == "ResourceId"
               or key in {"ServiceName", "ProductName"} for key, value in row.items()
               if key != "ServiceFamily" or value != "Other")


@pytest.mark.parametrize("row", [
    {"ServiceFamily": "Databases", "ServiceName": "Azure SQL Database"},
    {"ServiceFamily": "Analytics", "ServiceName": "Azure Databricks"},
    {"ServiceFamily": "Analytics", "ServiceName": "Azure Synapse Analytics"},
    {"ServiceName": "Azure Data Factory"}, {"ServiceName": "Storage"}, {},
])
def test_classifier_does_not_treat_all_data_platforms_as_ai(row):
    assert not ai._is_ai(row)


def test_billing_units_are_not_tokens_or_scaled_by_meter_label(backend):
    result = run()
    account = result["services"][0]
    assert account["meters"][0]["quantity"] == 36_401_063
    assert account["meters"][0]["cost"] == account["cost"] == 72.81
    assert account["tokens"]["value"] == 0
    assert account["tokens"]["source"] == "Azure Monitor"
    assert "not scaled" in " ".join(result["notices"])
    assert result["models"][0]["model"] == "gpt-5"
    assert account["models"] == ["Azure OpenAI GPT5"]


def test_zero_tokens_costs_and_paygo_are_real_not_missing(backend):
    backend.rows[0]["cost"] = 0
    result = run()
    assert result["status"] == "ok"
    assert result["totals"] == [{"currency": "USD", "cost": 0}]
    account = result["services"][0]
    assert (account["tokens"]["input"], account["tokens"]["output"], account["tokens"]["value"]) == (0, 0, 0)
    assert account["tokens"]["status"] == "ok"
    assert account["utilization"]["value"] is None
    assert account["utilization"]["status"] == "not_applicable"
    assert result["models"][0]["cost"] is None and result["models"][0]["cost_note"] == ai.COST_NOTE
    assert backend.request.await_args_list[0].args[1].endswith("/metricDefinitions")


def test_mixed_currencies_and_services_share_one_account_metric(backend):
    backend.rows += [
        billing(cost=4.00491, currency="EUR", service="Foundry Models", product="another label"),
        billing(cost=2.00123, service="Foundry Tools", product="third label"),
    ]
    result = run()
    assert len(result["services"]) == 1
    account = result["services"][0]
    assert account["cost"] is None and account["currency"] is None
    assert account["cost_by_currency"] == result["totals"]
    assert [entry["currency"] for entry in result["totals"]] == ["EUR", "USD"]
    assert [entry["cost"] for entry in result["totals"]] == pytest.approx([4.00491, 74.81123])
    assert len(account["services"]) == 3 and len(account["meters"]) == 3
    assert result["metric_coverage"]["queried_accounts"] == 1
    assert backend.request.await_count == 4


def test_unknown_cost_and_quick_quantity_are_not_zero(backend):
    backend.rows[0].update(cost=None, cost_rows=0, quantity=0, unit="", meter="", product="")
    result = run()
    account = result["services"][0]
    assert result["totals"] == [{"currency": "USD", "cost": None}]
    assert account["meters"][0]["quantity"] is None
    assert account["models"] == [] and result["status"] == "partial"


def test_partial_billed_rows_make_currency_total_unknown(backend):
    backend.rows.append(billing(cost=2, billing_rows=2, cost_rows=1, product="partial"))
    result = run()
    assert result["totals"] == [{"currency": "USD", "cost": None}]
    assert result["services"][0]["meters"][0]["cost"] == 72.81


def test_empty_scope_never_reads_warehouse_or_azure(backend):
    assert run([])["services"] == []
    assert backend.readers == []
    backend.request.assert_not_awaited()


def test_empty_warehouse_and_empty_selection_are_not_zero_spend_claims(backend):
    backend.hi = None
    result = run()
    assert result["status"] == "ok" and result["totals"] == []
    assert result["metric_window"] is None
    backend.request.assert_not_awaited()
    with reporting.select("2024-02"):
        result = run([])
    assert result["window"]["days"] == 29
    assert result["window"]["from"] == "2024-02-01"


def test_bounded_group_totals_use_identical_subset(backend, monkeypatch):
    monkeypatch.setattr(ai, "MAX_GROUPS", 2)
    backend.rows[:] = [billing("one", cost=1), billing("two", cost=2), billing("three", cost=3)]
    backend.deployments = []
    result = run()
    assert result["truncated"] and result["status"] == "partial"
    assert result["totals"] == [{"currency": "USD", "cost": 3}]
    assert sum(row["cost"] for row in result["services"]) == 3
    query, limit, _ = backend.queries[1]
    assert "LIMIT 3" in query and limit == 3


def test_scope_currency_filters_and_calendar_context_propagate_to_thread(backend):
    filters = {"service": ["Azure AI Services"], "meter": ["one"]}
    backend.rows = []
    token = cost._caller_id.set("isolated-test-user")
    try:
        with reporting.select("2026-08"):
            result = run([A, A], days=2, currency="EUR", filters=filters)
    finally:
        cost._caller_id.reset(token)
    assert backend.readers == [([A], "EUR", filters, "2026-08", "isolated-test-user")]
    assert result["window"]["from"] == "2026-08-01" and result["window"]["to"] == "2026-08-31"
    assert result["window"]["days"] == 31
    assert '"ChargePeriodStart" > DATE \'2026-07-31\'' in backend.queries[1][0]
    assert '"ChargePeriodStart" <= DATE \'2026-08-31\'' in backend.queries[1][0]
    assert "may be broader" in " ".join(result["notices"])


def test_rolling_window_is_exact_and_includes_zero_billing_groups(backend):
    backend.rows = []
    run(days=30)
    query = backend.queries[1][0]
    assert '"ChargePeriodStart" > DATE \'2026-09-09\' - INTERVAL 30 DAY' in query
    assert "HAVING" not in query
    assert "AI and Machine Learning".lower() in query


def test_daily_data_day_discovery_uses_scoped_filters_not_wall_clock_or_selected_month(backend):
    filters = {"service": ["Azure AI Services"]}
    with reporting.select("2026-08"):
        assert ai.snapshot_data_day([A, A], filters) == "2026-09-09"
        assert reporting.selected_month() == "2026-08"
    assert backend.readers == [([A], None, filters, None, cost._caller_id.get())]
    assert "key = 'data_generation'" in backend.queries[0][0]
    assert 'WHERE "ChargePeriodStart" < (SELECT CAST(updated_at AT TIME ZONE' in backend.queries[0][0]
    assert "AT TIME ZONE 'UTC' AS DATE)" in backend.queries[0][0]
    backend.request.assert_not_awaited()


def test_daily_data_day_empty_scope_and_empty_data_are_explicit(backend):
    assert ai.snapshot_data_day([]) == "empty"
    assert backend.readers == []
    backend.hi = None
    assert ai.snapshot_data_day([A]) == "empty"
    backend.request.assert_not_awaited()


def test_daily_active_month_ends_at_utc_day_end_and_closed_month_keeps_bounds(backend):
    backend.rows = []
    with reporting.select("2026-09"):
        result = run()
    assert result["window"]["from"] == "2026-09-01"
    assert result["window"]["to"] == "2026-09-09"
    assert result["window"]["days"] == 9
    assert result["metric_window"]["end"] == "2026-09-10T00:00:00Z"
    backend.hi = "2026-08-12"
    with reporting.select("2026-08"):
        result = run()
    assert result["window"]["to"] == "2026-08-31"
    assert result["window"]["days"] == 31


def test_daily_window_waits_for_later_publication_not_midnight(backend, monkeypatch):
    backend.rows = []
    with reporting.select("2026-09"):
        first = run()
        monkeypatch.setattr(reporting, "today", lambda: NOW.date() + timedelta(days=1))
        monkeypatch.setattr(ai, "_now", lambda: NOW + timedelta(days=1))
        overnight = run()
        assert overnight["window"] == first["window"]
        assert ai.snapshot_data_day([A]) == "2026-09-09"
        backend.published_day = "2026-09-11"
        backend.hi = "2026-09-10"
        assert ai.snapshot_data_day([A]) == "2026-09-10"
        refreshed = run()
        assert refreshed["window"]["to"] == "2026-09-10"


def test_historical_window_outside_retention_never_substitutes_recent_metrics(backend):
    with reporting.select("2024-02"):
        result = run()
    assert result["metric_window"]["start"] == "2024-02-01T00:00:00Z"
    assert result["metric_window"]["end"] == "2024-03-01T00:00:00Z"
    assert result["services"][0]["tokens"]["value"] is None
    assert "no recent substitute" in result["services"][0]["tokens"]["note"]
    backend.request.assert_not_awaited()


def test_daily_report_ends_at_completed_utc_day_not_current_hour(backend):
    backend.rows = []
    backend.hi = "2026-09-10"
    result = run()
    assert result["metric_window"]["end"] == "2026-09-10T00:00:00Z"
    assert result["window"]["to"] == "2026-09-09"
    assert 'WHERE "ChargePeriodStart" < (SELECT CAST(updated_at AT TIME ZONE' in backend.queries[0][0]


def test_metric_end_is_truncated_to_the_hour(backend, monkeypatch):
    """A ragged end makes Azure Monitor return a ragged grid, and _series drops all of it.

    Azure clips a series to MAX_POINTS without a nextLink and re-anchors the grid on the end of
    the timespan, so an end of 14:19:41 came back as 13:19, 14:19, ... -- stamps that are not
    hour boundaries and are rejected one by one. Every token read past the cap silently became
    "unavailable". The hour in progress cannot complete a PT1H sample anyway, so asking only
    for whole hours costs nothing and keeps the grid where the parser expects it.
    """
    monkeypatch.setattr(ai, "_now", lambda: NOW.replace(minute=19, second=41, microsecond=7))
    backend.rows = []
    backend.hi = "2026-09-10"
    result, _ = ai._metric_window({"from": "2026-09-09", "to": "2026-09-10", "days": 2}, ai._now())
    assert result["end"] == "2026-09-10T10:00:00Z"


def test_history_window_never_asks_for_more_points_than_azure_returns(backend):
    """Beyond MAX_POINTS Azure drops the oldest hours silently, so asking wider buys nothing."""
    for days in (1, 17, 18, 30, 90):
        window, unavailable = ai._metric_window(
            {"from": (NOW.date() - timedelta(days=days - 1)).isoformat(),
             "to": NOW.date().isoformat(), "days": days}, NOW)
        assert not unavailable
        history = ai._metric_history_window(window, NOW)
        start, end = ai._stamp(history["start"]), ai._stamp(window["end"])
        requested = (end - start).total_seconds() / 3600
        # The baseline is clamped to the cap; the selected window itself is never truncated to
        # fit, because reporting fewer samples than expected is honest and hiding them is not.
        assert requested <= max(ai.MAX_POINTS, (end - ai._stamp(window["start"])).total_seconds() / 3600)
        assert start <= ai._stamp(window["start"])
        assert history["history_clipped"] == (start > ai._stamp(window["start"]) - timedelta(
            days=ai_anomalies.WINDOW))


def test_failed_token_reads_report_the_failure_not_a_missing_metric(backend):
    """"Denied" and "this account has no token metrics" are different answers.

    Both used to collapse into the definitions-unavailable note, with nothing added to the
    account's notes, so a report full of failed reads looked exactly like a report of accounts
    that never emit tokens.
    """
    denied = {name: {"": ai._blank(WINDOW, "Metric read denied or failed; no usage inference.")}
              for name in (ai.INPUT, ai.OUTPUT)}
    combined = ai._combine_tokens(denied, "", WINDOW)
    assert combined["value"] is None and combined["status"] == "unavailable"
    assert combined["note"] == "Metric read denied or failed; no usage inference."
    assert "definitions" not in combined["note"]
    # An account that genuinely reports no token metrics still says exactly that.
    assert "definitions or matching series are unavailable" in ai._combine_tokens({}, "", WINDOW)["note"]


def test_identical_component_caveats_are_stated_once(backend):
    """Input and output share a grid, so an identical caveat must not print twice."""
    note = "Observed 744/2150 hourly samples. Incomplete coverage; missing hours are not zero."
    metrics = {
        name: {"": {**ai._blank(WINDOW, note, "partial"), "value": 10.0,
                    "samples": 744, "expected_samples": 2150, "_daily": {}}}
        for name in (ai.INPUT, ai.OUTPUT)
    }
    combined = ai._combine_tokens(metrics, "", WINDOW)
    assert combined["value"] == 20.0 and combined["status"] == "partial"
    assert combined["note"].count("Observed 744/2150 hourly samples.") == 1

    # A component that genuinely says something different is still reported in full.
    metrics[ai.OUTPUT][""]["note"] = "Observed 2150/2150 hourly samples."
    both = ai._combine_tokens(metrics, "", WINDOW)["note"]
    assert "Observed 744/2150 hourly samples." in both
    assert "Observed 2150/2150 hourly samples." in both


@pytest.mark.parametrize("scope", [None, "", A, [None], ["bad"], [A + "/x"], [A] * 501])
def test_invalid_scope_is_rejected_without_side_effects(backend, scope):
    with pytest.raises(ValueError):
        asyncio.run(ai.report(scope))
    assert backend.readers == []
    backend.request.assert_not_awaited()


@pytest.mark.parametrize("days", [0, -1, 366, True, "30"])
def test_invalid_days_are_rejected(backend, days):
    with pytest.raises(ValueError):
        run(days=days)


@pytest.mark.parametrize("value", [
    rid(sub=B), rid() + "/extra", rid() + "/", rid() + "?x=y", rid() + "#x",
    rid().replace("/rg/", "/../"), rid().replace("/rg/", "/%2f../"),
    rid().replace("/rg/", "/rg\\x/"), "https://evil.invalid" + rid(),
])
def test_bad_account_id_keeps_billing_but_never_calls_azure(backend, value):
    backend.rows[0]["resource_id"] = value
    result = run()
    assert result["totals"] == [{"currency": "USD", "cost": 72.81}]
    assert result["status"] == "partial"
    backend.request.assert_not_awaited()


def test_out_of_scope_billing_rows_are_excluded_defensively(backend):
    backend.rows.append(billing("foreign", sub=B, cost=999))
    result = run()
    assert result["totals"] == [{"currency": "USD", "cost": 72.81}]
    assert len(result["services"]) == 1 and result["status"] == "partial"


def test_non_cognitive_ai_resources_have_no_token_probe(backend):
    backend.rows[0].update(resource_id=rid(provider="Microsoft.Search/searchServices"),
                           service="Azure AI Search", product="Search")
    result = run()
    assert result["services"][0]["tokens"]["status"] == "not_applicable"
    assert "pay-as-you-go" not in result["services"][0]["recommendation"]
    backend.request.assert_not_awaited()


def test_account_tokens_sum_input_output_not_token_transaction(backend):
    async def counts(method, url, **kwargs):
        payload = await backend.original_request(method, url, **kwargs)
        if url.endswith("/metrics"):
            assert ai.TOTAL not in kwargs["params"]["metricnames"]
            for item in payload["value"]:
                value = 2 if item["name"]["value"] == ai.INPUT else 3
                for point in item["timeseries"][0]["data"]:
                    point["total"] = value
                    point["average"] = 999
        return payload
    backend.request.side_effect = counts
    result = run()
    for tokens in [result["services"][0]["tokens"], result["models"][0]["tokens"]]:
        assert (tokens["input"], tokens["output"], tokens["value"]) == (48, 72, 120)
        assert tokens["metrics"] == [ai.INPUT, ai.OUTPUT]


def test_total_fallback_is_verified_and_not_added_to_partial_components(backend):
    backend.definitions["value"] = [d for d in backend.definitions["value"]
                                    if d["name"]["value"] != ai.OUTPUT]
    async def totals(method, url, **kwargs):
        payload = await backend.original_request(method, url, **kwargs)
        if url.endswith("/metrics"):
            for item in payload["value"]:
                for point in item["timeseries"][0]["data"]:
                    point["total"] = 3 if item["name"]["value"] == ai.TOTAL else 2
        return payload
    backend.request.side_effect = totals
    tokens = run()["services"][0]["tokens"]
    assert (tokens["input"], tokens["output"], tokens["value"]) == (48, None, 72)
    assert tokens["metrics"] == [ai.TOTAL]


def test_only_input_available_does_not_become_combined_total(backend):
    backend.definitions["value"] = backend.definitions["value"][:1]
    result = run()
    tokens = result["services"][0]["tokens"]
    assert tokens["input"] == 0 and tokens["value"] is None and tokens["status"] == "partial"


def test_model_dimension_split_maps_exact_deployment_metadata_not_product(backend):
    backend.deployments.append(deployment("other", model="gpt-4.1"))
    async def split(method, url, **kwargs):
        payload = await backend.original_request(method, url, **kwargs)
        if "$filter" in kwargs["params"]:
            assert kwargs["params"]["$filter"] == "ModelDeploymentName eq '*'"
            for item in payload["value"]:
                item["timeseries"].append(series([1] * 24, name="other"))
        return payload
    backend.request.side_effect = split
    result = run()
    assert [(m["deployment"], m["model"], m["tokens"]["value"]) for m in result["models"]] == [
        ("chat", "gpt-5", 0), ("other", "gpt-4.1", 48)]
    assert result["services"][0]["tokens"]["value"] == 0
    assert all(m["cost"] is None for m in result["models"])


def test_unsupported_dimension_is_not_requested_or_guessed(backend):
    backend.definitions = definitions(split=False)
    for definition in backend.definitions["value"]:
        definition["dimensions"] = [{"value": "ModelName"}]
    result = run()
    assert result["models"][0]["tokens"]["value"] is None
    assert result["services"][0]["tokens"]["value"] == 0
    assert all("$filter" not in c.kwargs["params"] for c in backend.request.await_args_list)


@pytest.mark.parametrize("alter", [
    lambda d: d.update(unit="Bytes"),
    lambda d: d.update(supportedAggregationTypes=["Average"]),
])
def test_live_definition_units_and_aggregation_are_required(backend, alter):
    for definition in backend.definitions["value"]:
        alter(definition)
    result = run()
    assert result["services"][0]["tokens"]["value"] is None
    assert not any(c.args[1].endswith("/metrics") for c in backend.request.await_args_list)


@pytest.mark.parametrize("change", [
    lambda p: p.update(timeseries=[]),
    lambda p: p.update(unit="Bytes"),
    lambda p: p.update(errorCode="Forbidden"),
    lambda p: p["timeseries"].append(copy.deepcopy(p["timeseries"][0])),
    lambda p: p["timeseries"][0].update(metadatavalues=[
        {"name": {"value": "Unexpected"}, "value": "x"}]),
])
def test_missing_ambiguous_or_wrong_metric_payloads_do_not_create_usage(backend, change):
    async def broken(method, url, **kwargs):
        payload = await backend.original_request(method, url, **kwargs)
        if url.endswith("/metrics"):
            for item in payload["value"]:
                change(item)
        return payload
    backend.request.side_effect = broken
    result = run()
    assert result["services"][0]["tokens"]["value"] is None
    assert result["status"] == "partial"


def test_duplicate_metric_entries_and_wrong_interval_rejected(backend):
    async def broken(method, url, **kwargs):
        payload = await backend.original_request(method, url, **kwargs)
        if url.endswith("/metrics"):
            payload["value"].append(copy.deepcopy(payload["value"][0]))
        return payload
    backend.request.side_effect = broken
    assert run()["services"][0]["tokens"]["value"] is None
    async def interval(method, url, **kwargs):
        payload = await backend.original_request(method, url, **kwargs)
        if url.endswith("/metrics"):
            payload["interval"] = "P1D"
        return payload
    backend.request.side_effect = interval
    assert run()["services"][0]["tokens"]["value"] is None


def test_sparse_and_null_samples_preserve_partial_vs_zero():
    sparse = ai._series(series([2, None, 3])["data"], "Total", WINDOW)
    assert (sparse["status"], sparse["value"], sparse["samples"]) == ("partial", 5, 2)
    missing = ai._series(series([None] * 24)["data"], "Total", WINDOW)
    assert missing["value"] is None and missing["status"] == "unavailable"
    assert ai._series(series([0] * 24)["data"], "Total", WINDOW)["value"] == 0


def test_duplicate_timestamps_are_rejected_even_if_one_is_null():
    data = series([None, 1])["data"]
    data[1]["timeStamp"] = data[0]["timeStamp"]
    result = ai._series(data, "Total", WINDOW)
    assert result["value"] is None and "Duplicate" in result["note"]


@pytest.mark.parametrize("value", [float("nan"), float("inf"), True, "12", -1])
def test_invalid_metric_numbers_never_become_usage(value):
    assert ai._series(series([value])["data"], "Total", WINDOW)["value"] is None


def test_provisioned_utilization_is_actual_hourly_average_and_maximum(backend):
    backend.definitions = definitions(utilization=True)
    backend.deployments[0]["sku"]["name"] = "GlobalProvisionedManaged"
    async def utilization(method, url, **kwargs):
        payload = await backend.original_request(method, url, **kwargs)
        if url.endswith("/metrics") and kwargs["params"]["metricnames"] == ai.UTILIZATION:
            aggregation = kwargs["params"]["aggregation"]
            for item in payload["value"]:
                for point in item["timeseries"][0]["data"]:
                    point[aggregation.lower()] = 20 if aggregation == "Average" else 95
        return payload
    backend.request.side_effect = utilization
    result = run()
    for item in [result["services"][0], result["models"][0]]:
        assert item["utilization"]["value"] == 20
        assert item["utilization"]["maximum"] == 95
        assert "throttling" in item["recommendation"]
        assert "Unweighted" in item["utilization"]["note"]


def test_multiple_ptu_capacities_never_become_combined_account_utilization(backend):
    backend.definitions = definitions(utilization=True)
    backend.deployments = [
        deployment(sku="GlobalProvisionedManaged"),
        deployment("other", sku="DataZoneProvisionedManaged", model="gpt-4.1"),
    ]
    async def distinct(method, url, **kwargs):
        payload = await backend.original_request(method, url, **kwargs)
        if url.endswith("/metrics") and kwargs["params"]["metricnames"] == ai.UTILIZATION:
            assert "$filter" in kwargs["params"]
            aggregation = kwargs["params"]["aggregation"]
            payload["value"][0]["timeseries"] = [
                series([20] * 24, name="chat", aggregation=aggregation),
                series([80] * 24, name="other", aggregation=aggregation),
            ]
        return payload
    backend.request.side_effect = distinct
    result = run()
    assert result["services"][0]["utilization"]["value"] is None
    assert result["services"][0]["utilization"]["status"] == "unavailable"
    assert "not combined" in result["services"][0]["utilization"]["note"]
    assert [model["utilization"]["value"] for model in result["models"]] == [20, 80]
    assert result["services"][0]["tokens"]["value"] == 0


def test_missing_deployment_inventory_cannot_authorize_account_ptu_percentage(backend):
    backend.definitions = definitions(utilization=True)
    async def denied(method, url, **kwargs):
        if "/deployments" in url:
            raise cost.CostError("Forbidden")
        if url.endswith("/metrics"):
            assert kwargs["params"]["metricnames"] != ai.UTILIZATION
        return await backend.original_request(method, url, **kwargs)
    backend.request.side_effect = denied
    result = run()
    assert result["services"][0]["utilization"]["value"] is None
    assert result["services"][0]["tokens"]["value"] == 0


def test_historical_extra_ptu_deployment_prevents_current_account_rollup(backend):
    backend.definitions = definitions(utilization=True)
    backend.deployments[0]["sku"]["name"] = "ProvisionedManaged"
    async def historical(method, url, **kwargs):
        payload = await backend.original_request(method, url, **kwargs)
        if url.endswith("/metrics") and kwargs["params"]["metricnames"] == ai.UTILIZATION:
            assert "$filter" in kwargs["params"]
            payload["value"][0]["timeseries"].append(
                series([80] * 24, name="deleted-model", aggregation=kwargs["params"]["aggregation"]))
        return payload
    backend.request.side_effect = historical
    result = run()
    assert result["services"][0]["utilization"]["value"] is None
    assert result["models"][0]["utilization"]["value"] == 0
    assert result["models"][0]["deployment"] == "chat"


def test_unknown_sku_is_not_assumed_paygo(backend):
    backend.deployments[0]["sku"]["name"] = "FutureSku"
    result = run()
    assert result["models"][0]["utilization"]["status"] == "unavailable"
    assert result["services"][0]["utilization"]["status"] == "unavailable"


@pytest.mark.parametrize("failure", [cost.CostError("Forbidden"), httpx.ConnectError("offline"), {},
                                     {"error": {"code": "Forbidden"}}])
def test_denied_or_failed_reads_preserve_billed_costs(backend, failure):
    backend.request.side_effect = failure if isinstance(failure, Exception) else None
    backend.request.return_value = failure
    result = run()
    assert result["totals"] == [{"currency": "USD", "cost": 72.81}]
    assert result["services"][0]["tokens"]["value"] is None
    assert result["status"] == "partial"


def test_model_metadata_failures_do_not_drop_account_token_metrics(backend):
    async def denied(method, url, **kwargs):
        if "/deployments" in url:
            raise cost.CostError("Forbidden")
        return await backend.original_request(method, url, **kwargs)
    backend.request.side_effect = denied
    result = run()
    assert result["services"][0]["tokens"]["value"] == 0
    assert result["models"] == [] and result["status"] == "partial"


def test_deployment_continuation_is_bounded_to_exact_arm_account(backend):
    async def foreign(method, url, **kwargs):
        payload = await backend.original_request(method, url, **kwargs)
        if "/deployments" in url:
            payload["nextLink"] = f"https://evil.invalid{rid()}/deployments?api-version=2024-10-01"
        return payload
    backend.request.side_effect = foreign
    result = run()
    assert len(result["models"]) == 1 and result["status"] == "partial"
    assert all("evil.invalid" not in call.args[1] for call in backend.request.await_args_list)


def test_deployment_metadata_exact_scope_and_uniqueness_required(backend):
    backend.deployments[0]["id"] = deployment(sub=B)["id"]
    assert run()["models"] == []
    backend.deployments = [deployment(), deployment()]
    result = run()
    assert result["models"] == [] and result["status"] == "partial"


def test_unmatched_metric_name_is_not_invented_as_model(backend):
    async def different(method, url, **kwargs):
        payload = await backend.original_request(method, url, **kwargs)
        if "$filter" in kwargs["params"]:
            for item in payload["value"]:
                item["timeseries"][0]["metadatavalues"][0]["value"] = "CHAT"
        return payload
    backend.request.side_effect = different
    result = run()
    assert result["models"][0]["tokens"]["value"] is None
    assert result["models"][0]["deployment"] == "chat"
    assert "mapped exactly" in " ".join(result["services"][0]["notes"])


def test_capped_accounts_are_cost_prioritized_without_duplicate_calls(backend, monkeypatch):
    monkeypatch.setattr(ai, "MAX_ACCOUNTS", 2)
    backend.rows = [billing(f"a{i}", cost=i) for i in range(5)]
    backend.deployments = []
    result = run()
    called = {call.args[1].split("/providers/microsoft.insights")[0]
              for call in backend.request.await_args_list if call.args[1].endswith("/metricDefinitions")}
    assert called == {cost.ARM + rid("a3"), cost.ARM + rid("a4")}
    assert result["totals"] == [{"currency": "USD", "cost": 10}]
    assert result["metric_coverage"]["queried_accounts"] == 2
    assert result["metric_coverage"]["eligible_accounts"] == 5
    assert len(result["services"]) == 5 and result["status"] == "partial"


def test_concurrency_and_timeout_release_all_read_tasks(backend, monkeypatch):
    backend.rows = [billing(f"a{i}") for i in range(8)]
    backend.deployments = []
    active = peak = 0
    async def slow(method, url, **kwargs):
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        try:
            await asyncio.sleep(0.01)
            return await backend.original_request(method, url, **kwargs)
        finally:
            active -= 1
    backend.request.side_effect = slow
    run()
    assert peak == ai.MAX_CONCURRENT * 2 and active == 0
    monkeypatch.setattr(ai, "ACCOUNT_TIMEOUT", 0.001)
    result = run()
    assert active == 0 and result["status"] == "partial"
    assert all("time budget" in " ".join(item["notes"]) for item in result["services"])


def test_programming_errors_propagate_instead_of_becoming_success(backend):
    backend.request.side_effect = TypeError("programming error")
    with pytest.raises(TypeError, match="programming error"):
        run()


def test_denied_account_does_not_drop_successful_peer(backend):
    backend.rows.append(billing("peer", cost=3))
    backend.deployments = []
    async def denied(method, url, **kwargs):
        if rid() in url:
            raise cost.CostError("Forbidden")
        return await backend.original_request(method, url, **kwargs)
    backend.request.side_effect = denied
    result = run()
    assert result["status"] == "partial"
    assert [(row["resource_name"], row["tokens"]["value"]) for row in result["services"]] == [
        ("ai", None), ("peer", 0)]
    assert result["totals"][0]["cost"] == pytest.approx(75.81)


def test_same_account_paging_maps_only_actual_returned_deployments(backend):
    root = cost.ARM + rid() + "/deployments"
    async def paged(method, url, **kwargs):
        if url == root + "?api-version=2024-10-01&$skiptoken=next":
            return {"value": [deployment("second", model="gpt-4.1")]}
        payload = await backend.original_request(method, url, **kwargs)
        if url == root:
            payload["nextLink"] = root + "?api-version=2024-10-01&$skiptoken=next"
        return payload
    backend.request.side_effect = paged
    result = run()
    assert [row["deployment"] for row in result["models"]] == ["chat", "second"]
    assert result["services"][0]["deployment_status"] == "ok"
    assert result["models"][1]["tokens"]["value"] is None


def test_deployment_and_dimension_caps_fail_closed(backend, monkeypatch):
    monkeypatch.setattr(ai, "MAX_DEPLOYMENTS", 1)
    result = run()
    assert len(result["models"]) == 1 and result["services"][0]["deployment_status"] == "partial"
    payload = metric()
    payload["timeseries"] = [series(name=str(i)) for i in range(ai.MAX_SERIES)]
    assert ai._read_metric(payload, ai.INPUT, "Total", WINDOW, True)[""]["value"] is None


def test_unknown_native_currency_is_not_silently_usd(backend):
    backend.rows[0]["currency"] = "unknown"
    result = run()
    assert result["totals"] == [{"currency": "unknown", "cost": 72.81}]


def test_unnamed_resources_with_no_ids_do_not_merge_across_services(backend):
    backend.rows = [
        billing(resource_id="", resource_name="", service="Foundry Tools"),
        billing(resource_id="", resource_name="", service="Foundry Models"),
    ]
    result = run()
    assert len(result["services"]) == 2
    assert len({item["id"] for item in result["services"]}) == 2
    assert all(item["resource_id"] is None for item in result["services"])
    backend.request.assert_not_awaited()


def test_service_filter_never_becomes_metric_filter_or_cost_token_ratio(backend):
    result = run(filters={"service": ["Azure AI Services"]})
    assert "account-wide" in " ".join(result["services"][0]["notes"])
    assert all(call.kwargs["params"].get("$filter") in (None, "ModelDeploymentName eq '*'")
               for call in backend.request.await_args_list)
    assert "cost_per_token" not in result["services"][0]


def test_maximum_partial_coverage_is_not_hidden_by_complete_average():
    avg = ai._series(series([20] * 24, aggregation="Average")["data"], "Average", WINDOW)
    maximum = ai._series(series([99], aggregation="Maximum")["data"], "Maximum", WINDOW)
    output = ai._combine_utilization({"Average": {"": avg}, "Maximum": {"": maximum}}, "", WINDOW)
    assert (output["value"], output["maximum"], output["status"]) == (20, 99, "partial")
    assert output["maximum_samples"] == 1


def test_metric_timestamps_must_be_hourly_within_requested_window():
    for stamp in ("2026-09-09T00:30:00Z", WINDOW["end"], "bad", "2026-09-09T00:00:00"):
        data = series([12])["data"]
        data[0]["timeStamp"] = stamp
        assert ai._series(data, "Total", WINDOW)["value"] is None


def test_case_duplicate_deployment_dimensions_are_not_summed():
    payload = metric(split=True)
    payload["timeseries"].append(series(name="CHAT"))
    result = ai._read_metric(payload, ai.INPUT, "Total", WINDOW, True)
    assert result[""]["value"] is None
    assert "Duplicate" in result[""]["note"]


def test_aggregate_overflow_does_not_emit_nonfinite_json():
    assert ai._sum([1e308, 1e308]) is None
    output = ai._series(series([1e308, 1e308])["data"], "Total", WINDOW)
    assert output["value"] is None and output["status"] == "unavailable"


@pytest.mark.parametrize("payload", [
    {"properties": []}, {"properties": {"model": ["not", "a", "model"]}},
    {"sku": {"name": {"unexpected": "object"}}},
])
def test_malformed_metadata_does_not_hide_account_tokens(backend, payload):
    backend.deployments[0].update(payload)
    result = run()
    assert result["services"][0]["tokens"]["value"] == 0
    assert all(row["model"] is None or isinstance(row["model"], str) for row in result["models"])


@pytest.mark.parametrize("provider, service", [
    ("Microsoft.Search/searchServices", "Azure AI Search / Azure Cognitive Search"),
    ("Microsoft.MachineLearningServices/workspaces", "Azure Machine Learning / Cognitive Services"),
    ("Microsoft.MachineLearning/workspaces", "Cognitive ML"),
])
def test_valid_non_cognitive_provider_overrides_cognitive_service_label(backend, provider, service):
    backend.rows[0].update(resource_id=rid(provider=provider), service=service)
    result = run()
    assert result["services"][0]["tokens"]["status"] == "not_applicable"
    assert result["services"][0]["utilization"]["status"] == "not_applicable"
    assert result["status"] == "ok"
    backend.request.assert_not_awaited()


def test_definitions_and_deployments_are_independent_concurrent_reads(backend):
    definitions_started = asyncio.Event()
    deployments_started = asyncio.Event()
    async def overlap(method, url, **kwargs):
        if url.endswith("/metricDefinitions"):
            definitions_started.set()
            await asyncio.wait_for(deployments_started.wait(), 0.5)
        if "/deployments" in url:
            deployments_started.set()
            await asyncio.wait_for(definitions_started.wait(), 0.5)
        return await backend.original_request(method, url, **kwargs)
    backend.request.side_effect = overlap
    result = run()
    assert result["services"][0]["tokens"]["value"] == 0
    assert len(result["models"]) == 1 and result["status"] == "ok"


@pytest.mark.parametrize("slow_stage", ["deployments", "deployment tokens"])
def test_timeout_after_rollup_retains_account_tokens_and_cancels_optional_reads(backend, monkeypatch, slow_stage):
    monkeypatch.setattr(ai, "ACCOUNT_TIMEOUT", 0.04)
    cancelled = []
    async def optional_slow(method, url, **kwargs):
        slow = ("/deployments" in url if slow_stage == "deployments"
                else url.endswith("/metrics") and "$filter" in kwargs["params"])
        if slow:
            try:
                await asyncio.sleep(2)
            finally:
                cancelled.append(slow_stage)
        payload = await backend.original_request(method, url, **kwargs)
        if url.endswith("/metrics") and "$filter" not in kwargs["params"]:
            for item in payload["value"]:
                for point in item["timeseries"][0]["data"]:
                    point["total"] = 2 if item["name"]["value"] == ai.INPUT else 3
        return payload
    backend.request.side_effect = optional_slow
    async def observe():
        result = await ai.report([A], days=1)
        await asyncio.sleep(0)
        assert [task for task in asyncio.all_tasks() if task is not asyncio.current_task()] == []
        return result
    result = asyncio.run(observe())
    account = result["services"][0]
    assert (account["tokens"]["input"], account["tokens"]["output"], account["tokens"]["value"]) == (48, 72, 120)
    assert account["tokens"]["status"] == "ok" and result["status"] == "partial"
    assert "time budget exceeded" in " ".join(account["notes"])
    assert slow_stage in " ".join(account["notes"])
    assert cancelled == [slow_stage]
    if slow_stage == "deployment tokens":
        assert len(result["models"]) == 1
        assert result["models"][0]["tokens"]["value"] is None
        assert result["models"][0]["recommendation"]


def test_global_report_deadline_retains_completed_metadata_and_rollup(backend, monkeypatch):
    monkeypatch.setattr(ai, "REPORT_TIMEOUT", 0.05)
    monkeypatch.setattr(ai, "ACCOUNT_TIMEOUT", 1)
    active = 0
    async def split_slow(method, url, **kwargs):
        nonlocal active
        if "$filter" in kwargs["params"]:
            active += 1
            try:
                await asyncio.sleep(2)
            finally:
                active -= 1
        return await backend.original_request(method, url, **kwargs)
    backend.request.side_effect = split_slow
    async def observe():
        started = asyncio.get_running_loop().time()
        result = await ai.report([A], days=1)
        assert asyncio.get_running_loop().time() - started < 0.5
        assert active == 0
        assert [task for task in asyncio.all_tasks() if task is not asyncio.current_task()] == []
        return result
    result = asyncio.run(observe())
    assert result["services"][0]["tokens"]["value"] == 0
    assert len(result["models"]) == 1
    assert result["models"][0]["recommendation"]
    assert "report budget" in " ".join(result["notices"])
    assert result["totals"] == [{"currency": "USD", "cost": 72.81}]


def test_global_deadline_cancels_metadata_children_and_queued_accounts(backend, monkeypatch):
    monkeypatch.setattr(ai, "REPORT_TIMEOUT", 0.05)
    backend.rows = [billing(f"a{i}") for i in range(8)]
    started = []
    stopped = []
    async def blocked(method, url, **kwargs):
        started.append(url)
        try:
            await asyncio.sleep(2)
        finally:
            stopped.append(url)
        return await backend.original_request(method, url, **kwargs)
    backend.request.side_effect = blocked
    async def observe():
        result = await ai.report([A], days=1)
        assert [task for task in asyncio.all_tasks() if task is not asyncio.current_task()] == []
        return result
    result = asyncio.run(observe())
    assert len(started) == ai.MAX_CONCURRENT * 2
    assert sorted(started) == sorted(stopped)
    assert result["metric_coverage"]["queried_accounts"] == ai.MAX_CONCURRENT
    assert result["metric_coverage"]["eligible_accounts"] == 8
    assert len(result["services"]) == 8 and result["status"] == "partial"


def test_caller_cancellation_cleans_up_both_metadata_children(backend):
    started = []
    stopped = []
    ready = asyncio.Event()
    async def blocked(method, url, **kwargs):
        started.append(url)
        if len(started) == 2:
            ready.set()
        try:
            await asyncio.sleep(2)
        finally:
            stopped.append(url)
    backend.request.side_effect = blocked
    async def observe():
        task = asyncio.create_task(ai.report([A], days=1))
        await asyncio.wait_for(ready.wait(), 0.5)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert [task for task in asyncio.all_tasks() if task is not asyncio.current_task()] == []
    asyncio.run(observe())
    assert len(started) == 2 and sorted(started) == sorted(stopped)


def test_account_rounds_fit_the_report_budget():
    """The constraint the four limits exist to satisfy, stated as a relationship.

    Every account gets its own ACCOUNT_TIMEOUT, and they run MAX_CONCURRENT at a time, so the
    worst case is one full timeout per round. That has to finish inside REPORT_TIMEOUT or the
    global deadline cuts off a round that was still working — the report then reports gaps
    caused by its own budget rather than by anything Azure did.

    Written as arithmetic rather than as pinned constants so the limits can be retuned
    together. Pinning MAX_CONCURRENT == 4 here made an improvement look like a regression:
    raising it to 6 shortens the wait *and* widens this margin, and the old assertion failed
    on both.
    """
    rounds = (ai.MAX_ACCOUNTS + ai.MAX_CONCURRENT - 1) // ai.MAX_CONCURRENT
    worst_case = rounds * ai.ACCOUNT_TIMEOUT
    assert worst_case < ai.REPORT_TIMEOUT, (
        f"{ai.MAX_ACCOUNTS} accounts at {ai.MAX_CONCURRENT} concurrent is {rounds} rounds, "
        f"{worst_case}s against a {ai.REPORT_TIMEOUT}s budget")

    # A per-account budget has to allow a real probe: definitions, tokens, deployments and
    # utilisation, several of them sequential. Single digits would time out on a healthy read.
    assert ai.ACCOUNT_TIMEOUT >= 10

    # And concurrency has to stay bounded. Each probe fans out into several Azure Monitor
    # reads, so this multiplies; unbounded would trade a slow report for a throttled one.
    assert 1 < ai.MAX_CONCURRENT <= ai.MAX_ACCOUNTS


def test_completed_model_average_survives_slow_ptu_maximum(backend, monkeypatch):
    monkeypatch.setattr(ai, "ACCOUNT_TIMEOUT", 0.04)
    backend.definitions = definitions(utilization=True)
    backend.deployments[0]["sku"]["name"] = "ProvisionedManaged"
    async def maximum_slow(method, url, **kwargs):
        if (url.endswith("/metrics") and kwargs["params"]["metricnames"] == ai.UTILIZATION
                and kwargs["params"]["aggregation"] == "Maximum"):
            await asyncio.sleep(2)
        return await backend.original_request(method, url, **kwargs)
    backend.request.side_effect = maximum_slow
    result = run()
    assert result["models"][0]["utilization"]["value"] == 0
    assert result["services"][0]["tokens"]["value"] == 0
    assert result["services"][0]["utilization"]["value"] is None
    assert result["status"] == "partial"


def test_shared_four_request_gate_is_respected_without_losing_account_rollups(backend, monkeypatch):
    monkeypatch.setattr(ai, "ACCOUNT_TIMEOUT", 0.2)
    backend.rows = [billing(f"a{i}") for i in range(8)]
    backend.deployments = []
    gate = asyncio.Semaphore(4)
    active = peak = 0
    async def gated(method, url, **kwargs):
        nonlocal active, peak
        async with gate:
            active += 1
            peak = max(peak, active)
            try:
                await asyncio.sleep(0.01)
                return await backend.original_request(method, url, **kwargs)
            finally:
                active -= 1
    backend.request.side_effect = gated
    result = run()
    assert peak == 4 and active == 0
    assert result["metric_coverage"]["accounts_with_tokens"] == 8


def test_thirty_day_720_sample_shape_preserves_reported_input_and_output(backend):
    start = datetime(2026, 8, 11, tzinfo=UTC)
    async def month(method, url, **kwargs):
        if not url.endswith("/metrics"):
            return await backend.original_request(method, url, **kwargs)
        params = kwargs["params"]
        # 744 hours back from the end, not the full 14-day baseline: beyond MAX_POINTS Azure
        # clips the series itself, so the wider request returned no more than this one does.
        assert params["timespan"] == "2026-08-10T00:00:00Z/2026-09-10T00:00:00Z"
        metrics = []
        for name in params["metricnames"].split(","):
            assert name in (ai.INPUT, ai.OUTPUT)
            payload = metric(name, split="$filter" in params)
            payload["timeseries"][0]["data"] = [
                {"timeStamp": ai._iso(start + timedelta(hours=index)),
                 "total": (152180 if name == ai.INPUT else 10197) if index == 719 else 0}
                for index in range(720)
            ]
            metrics.append(payload)
        return {"interval": "PT1H", "value": metrics}
    backend.request.side_effect = month
    result = run(days=30)
    account = result["services"][0]
    assert account["tokens"]["input"] == 152180
    assert account["tokens"]["output"] == 10197
    assert account["tokens"]["value"] == 162377
    assert account["tokens"]["samples"] == account["tokens"]["expected_samples"] == 720
    assert account["tokens"]["status"] == "ok"
    assert result["models"][0]["tokens"]["value"] == 162377
    assert result["models"][0]["cost"] is None


def test_programming_error_cancels_other_inflight_metadata_read(backend):
    started = asyncio.Event()
    cancelled = []
    async def broken(method, url, **kwargs):
        if url.endswith("/metricDefinitions"):
            await started.wait()
            raise TypeError("programming error")
        started.set()
        try:
            await asyncio.sleep(2)
        finally:
            cancelled.append(url)
    backend.request.side_effect = broken
    async def observe():
        with pytest.raises(TypeError, match="programming error"):
            await ai.report([A], days=1)
        assert [task for task in asyncio.all_tasks() if task is not asyncio.current_task()] == []
    asyncio.run(observe())
    assert len(cancelled) == 1 and "/deployments" in cancelled[0]


def test_cost_read_elapsed_time_consumes_global_budget(backend, monkeypatch):
    monkeypatch.setattr(ai, "REPORT_TIMEOUT", 0.005)
    original = ai._costs
    def slow_costs(*args):
        import time
        time.sleep(0.015)
        return original(*args)
    monkeypatch.setattr(ai, "_costs", slow_costs)
    result = run()
    backend.request.assert_not_awaited()
    assert result["totals"] == [{"currency": "USD", "cost": 72.81}]
    assert result["metric_coverage"]["queried_accounts"] == 0
    assert "report budget" in " ".join(result["notices"])
