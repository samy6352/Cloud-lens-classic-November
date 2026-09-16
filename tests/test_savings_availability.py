"""Savings price availability: synthetic inputs, no Azure, database, or live FX reads."""
import itertools

import pytest

from app import currency, savings

PROBLEM = "Review disks that are not attached to a VM and evaluate if you still need the disks"
RID = "/subscriptions/sub-a/resourceGroups/group/providers/Microsoft.Compute/disks/disk"


def advisor(**fields):
    return {"problem": PROBLEM, "resource": "disk", "resource_id": RID,
            "resourceGroup": "group", "region": "southindia", "currency": "USD", **fields}


def assemble(rows, **kwargs):
    return savings.assemble({"advisor": {"recommendations": rows}}, {}, 30, **kwargs)


@pytest.mark.parametrize("fields", [
    {}, {"annual_savings": None}, {"annual_savings": ""}, {"annual_savings": "unknown"},
    {"annual_savings": True}, {"annual_savings": float("nan")},
    {"annual_savings": float("inf")}, {"annual_savings": None, "monthly_savings": None},
])
def test_unknown_estimates_remain_in_counts_without_zero_money(fields):
    data = assemble([advisor(**fields)])
    row = data["opportunities"][0]
    assert row["window"] is None and row["annual"] is None
    assert row["sources"][0]["window"] is None and row["sources"][0]["annual"] is None
    assert "Provider saving estimate unavailable" in row["sources"][0]["claim"]
    assert "about 0" not in row["sources"][0]["claim"]
    assert data["count"] == data["unpriced_count"] == 1
    assert data["total"] is None and data["projected_total"] is None
    assert data["partial"] is True and data["unavailable"] == {}
    category = next(c for c in data["categories"] if c["id"] == "orphaned")
    assert category["count"] == category["unpriced_count"] == 1
    assert category["total"] is None


@pytest.mark.parametrize(("fields", "annual"), [
    ({"annual_savings": 0, "monthly_savings": 90}, 0),
    ({"annual_savings": "0"}, 0),
    ({"annual_savings": 365}, 365),
    ({"annual_savings": "1,460"}, 1460),
    ({"annual_savings": None, "monthly_savings": 0}, 0),
    ({"monthly_savings": 10}, 120),
    ({"savings": 20}, 240),
])
def test_known_zero_and_positive_estimates_keep_their_periods(fields, annual):
    data = assemble([advisor(**fields)])
    row = data["opportunities"][0]
    assert row["annual"] == annual and row["currency"] == "USD"
    assert row["window"] == round(annual * 30 / 365, 2)
    assert row["sources"][0]["annual"] == annual
    assert f"about {annual:,.0f} a year" in row["sources"][0]["claim"]
    assert data["total"] == data["projected_total"] == row["window"]
    assert data["unpriced_count"] == 0 and data["partial"] is False


def test_mixed_totals_retain_known_amounts_and_count_unknown_opportunities():
    data = assemble([advisor(), advisor(resource_id=RID + "-zero", annual_savings=0),
                     advisor(resource_id=RID + "-priced", annual_savings=365)])
    assert data["total"] == data["projected_total"] == 30
    assert data["count"] == 3 and data["unpriced_count"] == 1
    assert [o["window"] for o in data["opportunities"]] == [30, 0, None]
    category = next(c for c in data["categories"] if c["id"] == "orphaned")
    assert category["total"] == 30 and category["count"] == 3
    assert category["unpriced_count"] == 1 and data["partial"] is True


def test_grouping_preserves_longest_priced_lookback_and_unpriced_alternatives():
    rows = [advisor(annual_savings=365, lookback_days=30, term="P1Y"),
            advisor(annual_savings=None, lookback_days=60, term="P1Y"),
            advisor(annual_savings=730, lookback_days=7, term="P1Y"),
            advisor(annual_savings=None, lookback_days=30, term="P3Y")]
    for permutation in itertools.permutations(rows):
        data = assemble(list(permutation))
        row = data["opportunities"][0]
        assert data["count"] == 1 and row["folded"] == 2
        assert row["window"] == 30 and row["annual"] == 365
        assert [o["annual"] for o in row["options"]] == [365, None]
        assert "from 30 days" in row["sources"][0]["claim"]
        assert "unpriced" in row["sources"][0]["claim"]
        assert "longest priced lookback" in row["options"][0]["detail"]
        assert "unavailable" in row["options"][1]["detail"]


def test_grouped_unknown_estimates_do_not_invent_an_option_or_spread_amount():
    row = assemble([advisor(term="P1Y", lookback_days=30),
                    advisor(term="P1Y", lookback_days=60),
                    advisor(term="P3Y")])["opportunities"][0]
    assert row["annual"] is None and row["window"] is None
    assert len(row["options"]) == 2 and row["folded"] == 1
    assert all(option["annual"] is None and option["window"] is None for option in row["options"])


def test_unpriced_advisor_evidence_does_not_erase_corroborated_billed_amount():
    data = savings.assemble({
        "waste": {"currency": "USD", "findings": [{
            "category": "unattached_disks", "title": "Unattached disks", "count": 1,
            "cost": 12, "items": [{"id": RID, "name": "disk", "resource_group": "group", "cost": 12}],
        }]},
        "advisor": {"recommendations": [advisor()]},
    }, {}, 30)
    row = data["opportunities"][0]
    assert data["count"] == data["merged"] == 1
    assert data["total"] == data["billed_total"] == row["window"] == 12
    assert data["unpriced_count"] == 0
    assert row["annual"] is None and row["disputed"] is None
    assert row["sources"][1]["annual"] is None
    assert "unavailable" in row["sources"][1]["claim"]


def test_repeated_source_claim_retains_known_money_and_discloses_missing_estimates():
    row = savings.Opportunity("group", "orphaned", "Disks")
    row.add_source("advisor", "unknown")
    row.add_source("advisor", "priced", window=30, annual=365)
    row.add_source("advisor", "unknown")
    assert row.window == 30 and row.annual == 365
    assert row.sources[0]["matches"] == 3 and row.sources[0]["unpriced_count"] == 2
    assert "estimate unavailable for 2, not included" in row.sources[0]["claim"]


def test_monthly_estimates_are_converted_before_grouping(monkeypatch):
    monkeypatch.setattr(currency, "rate_to", lambda code: {"USD": 1, "INR": 80}.get(code))
    for fields in ({"monthly_savings": 10}, {"annual_savings": 120}):
        data = assemble([advisor(**fields)], currency="INR")
        row = data["opportunities"][0]
        assert row["annual"] == 9600 and row["window"] == round(9600 * 30 / 365, 2)
        assert row["currency"] == data["currency"] == "INR"
    unknown = assemble([advisor()], currency="INR")
    assert unknown["total"] is None and unknown["opportunities"][0]["annual"] is None


def test_unknown_commitments_do_not_turn_the_pointer_into_zero_savings():
    data = assemble([advisor(problem="Consider purchasing a reservation")])
    assert data["count"] == 0 and data["commitment_count"] == 1
    assert data["commitment_annual"] is None and data["commitment_unpriced_count"] == 1


def test_empty_result_still_has_zero_counts_and_totals():
    data = assemble([])
    assert data["count"] == data["unpriced_count"] == 0
    assert data["total"] == data["billed_total"] == data["projected_total"] == 0
