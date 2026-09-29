"""Hermetic AKS discovery semantics; no Azure, settings, or PostgreSQL imports."""
# cloudlens-test: requires-offline
from __future__ import annotations

import asyncio
import json
import sys
from contextlib import contextmanager
from types import ModuleType, SimpleNamespace
from unittest.mock import AsyncMock

import pytest

import app
from app import aks, reporting


def cluster(name="production", sub="sub-a", node_group="MC_group_production_eastus", **fields):
    return {
        "name": name,
        "id": f"/subscriptions/{sub}/resourceGroups/group/managedClusters/{name}",
        "subscriptionId": sub,
        "nodeResourceGroup": node_group,
        "clusterResourceGroup": "group",
        "location": "eastus",
        "kubernetesVersion": "1.30",
        "tier": "Standard",
        "poolCount": 2,
        "powerState": "Running",
        **fields,
    }


def charge(rg="MC_group_production_eastus", sub="sub-a", cost=90, service="Virtual Machines"):
    return {
        "rg": rg, "sub": sub, "cost": cost, "service": service,
        "region": "eastus", "currency": "USD", "resources": 2,
    }


@pytest.fixture
def scan(monkeypatch):
    state = SimpleNamespace(
        arg=AsyncMock(return_value=[]),
        subscriptions=AsyncMock(return_value={"subscriptions": [{"id": "sub-a"}]}),
        rows=[], daily=[], hi="2026-09-01", queries=[], scopes=[],
    )

    class Reader:
        # Mirrors the real `Reader`'s relation handles, which default to `costs`.
        agg = res = named = bill = "costs"

        def rows(self, sql, limit=None):
            state.queries.append(sql)
            if 'max("ChargePeriodStart") AS hi' in sql:
                return [{"hi": state.hi}]
            if '"ChargePeriodStart" AS day' in sql:
                return [dict(row) for row in state.daily]
            return [dict(row) for row in state.rows]

    @contextmanager
    def reader(scope, currency=None):
        state.scopes.append((scope, currency))
        yield Reader()

    for name, fields in {
        "waste": {"_arg": state.arg},
        "cost": {"list_subscriptions": state.subscriptions},
        "warehouse": {"warehouse": SimpleNamespace(reader=reader)},
    }.items():
        module = ModuleType(f"app.{name}")
        module.__dict__.update(fields)
        monkeypatch.setitem(sys.modules, f"app.{name}", module)
        monkeypatch.setattr(app, name, module, raising=False)

    def run(scope=("sub-a",)):
        return asyncio.run(aks.clusters(
            None if scope is None else list(scope), days=30, currency="USD"))

    state.run = run
    with reporting.select(None):
        yield state


def test_cluster_helper_does_not_swallow_discovery_failure(scan):
    scan.arg.side_effect = RuntimeError("discovery failed")
    with pytest.raises(RuntimeError, match="discovery failed"):
        asyncio.run(aks._clusters(["sub-a"]))


def test_failed_discovery_keeps_cost_history_and_unknown_count(scan):
    scan.arg.side_effect = RuntimeError("403 secret-internal-detail")
    scan.rows = [charge()]
    scan.daily = [{"day": "2026-09-01", "rg": scan.rows[0]["rg"], "cost": 90, "currency": "USD"}]
    result = scan.run()

    assert result["discovery_status"] == "unavailable"
    assert result["detected_clusters"] is None
    assert result["live_clusters"] is None
    assert result["total"] == 90
    assert result["trend"] == [{"day": "2026-09-01", "cost": 90}]
    assert result["clusters"][0]["inventory_status"] == "unknown"
    assert result["clusters"][0]["live"] is False
    assert result["unmatched_groups"][0]["inventory_status"] == "unknown"
    assert result["orphan_note"] == result["unmatched_note"]
    assert "secret-internal-detail" not in json.dumps(result)
    assert "establishes deletion" in result["unmatched_note"]
    assert not result["billing_empty"]
    assert scan.scopes == [(["sub-a"], "USD")]


def test_successful_empty_discovery_is_not_found_not_deleted(scan):
    scan.rows = [charge()]
    result = scan.run()

    assert result["discovery_status"] == "ok"
    assert result["detected_clusters"] == result["live_clusters"] == 0
    assert result["clusters"][0]["inventory_status"] == "not_found"
    assert result["unmatched_groups"] == [{
        "cluster": "production", "subscription_id": "sub-a",
        "node_resource_group": "MC_group_production_eastus",
        "cost": 90, "inventory_status": "not_found",
    }]
    assert result["orphaned"] == ["production"]
    assert "deleted" not in result["orphan_note"].lower()
    scan.arg.assert_awaited_once_with(aks.CLUSTER_QUERY, ["sub-a"], top=500)
    scan.subscriptions.assert_not_awaited()


@pytest.mark.parametrize("hi", [None, "2026-09-01"])
def test_cost_free_detected_cluster_is_retained_even_without_billing(scan, hi):
    scan.hi = hi
    scan.arg.return_value = [cluster()]
    result = scan.run()
    item = result["clusters"][0]

    assert result["discovery_status"] == "ok"
    assert result["detected_clusters"] == result["live_clusters"] == result["count"] == 1
    assert result["billing_empty"] is (hi is None)
    assert result["empty"] is False
    assert result["as_of"] == hi
    assert result["total"] == item["cost"] == 0
    assert item["inventory_status"] == "detected"
    assert item["live"] is True
    assert item["subscription_id"] == "sub-a"
    assert item["subscriptions"] == 1
    assert item["node_pools"] == 2
    assert item["tier"] == "Standard"
    assert result["unmatched_groups"] == result["orphaned"] == []
    assert result["unmatched_note"] is None
    assert result["trend"] == []


def test_stopped_cluster_is_detected_not_reported_as_running(scan):
    scan.arg.return_value = [cluster(powerState="Stopped")]
    scan.rows = [charge()]
    result = scan.run()

    assert result["detected_clusters"] == 1
    assert result["clusters"][0]["inventory_status"] == "detected"
    assert result["clusters"][0]["live"] is True
    assert result["clusters"][0]["power_state"] == "Stopped"
    assert "does not mean a cluster is running" in result["discovery_note"]
    assert result["unmatched_groups"] == []


@pytest.mark.parametrize("count", [499, 500, 501])
def test_result_limit_is_conservatively_partial_with_unknown_unmatched(scan, count):
    scan.arg.return_value = [cluster(name=f"cluster-{i}", node_group=f"nodes-{i}")
                             for i in range(count)]
    scan.rows = [charge("nodes-0", cost=60), charge("MC_group_unmatched_eastus", cost=30)]
    result = scan.run()
    items = {item["cluster"]: item for item in result["clusters"]}

    assert result["discovery_status"] == ("partial" if count >= 500 else "ok")
    assert result["detected_clusters"] == result["live_clusters"] == count
    assert items["cluster-0"]["inventory_status"] == "detected"
    assert items["cluster-0"]["cost"] == 60
    assert items["unmatched"]["inventory_status"] == ("unknown" if count >= 500 else "not_found")
    assert result["total"] == 90
    if count >= 500:
        assert "lower bound" in result["discovery_note"]


@pytest.mark.parametrize("failure", [
    RuntimeError("subscription-secret"),
    {"subscriptions": [{}]},
    {},
    {"subscriptions": None},
    {"subscriptions": [{"id": None}]},
])
def test_subscription_resolution_failures_do_not_erase_billing(scan, failure):
    if isinstance(failure, Exception):
        scan.subscriptions.side_effect = failure
    else:
        scan.subscriptions.return_value = failure
    scan.rows = [charge()]
    result = scan.run(scope=None)

    assert result["discovery_status"] == "unavailable"
    assert result["detected_clusters"] is None
    assert result["live_clusters"] is None
    assert result["clusters"][0]["inventory_status"] == "unknown"
    assert result["total"] == 90
    assert "subscription-secret" not in json.dumps(result)
    scan.arg.assert_not_awaited()


def test_no_visible_subscriptions_means_unknown_inventory(scan):
    scan.subscriptions.return_value = {"subscriptions": []}
    scan.rows = [charge()]
    result = scan.run(scope=None)

    assert result["discovery_status"] == "unavailable"
    assert result["detected_clusters"] is None
    assert result["clusters"][0]["inventory_status"] == "unknown"
    assert result["total"] == 90
    scan.arg.assert_not_awaited()


def test_resolved_scope_success_and_unsearched_subscription_stays_unknown(scan):
    scan.rows = [charge(sub="SUB-A"), charge(sub="sub-b", cost=15)]
    result = scan.run(scope=None)
    items = {item["subscription_id"]: item for item in result["clusters"]}

    assert result["discovery_status"] == "ok"
    assert result["detected_clusters"] == 0
    assert items["sub-a"]["inventory_status"] == "not_found"
    assert items["sub-b"]["inventory_status"] == "unknown"
    assert len(result["unmatched_groups"]) == 2
    scan.subscriptions.assert_awaited_once()
    scan.arg.assert_awaited_once_with(aks.CLUSTER_QUERY, ["sub-a"], top=500)


@pytest.mark.parametrize("discovery_fails", [False, True])
def test_shared_node_group_names_stay_separate_per_subscription(scan, discovery_fails):
    scan.rows = [
        charge("MC_group_shared_eastus", sub="sub-a", cost=50),
        charge("MC_group_shared_eastus", sub="sub-b", cost=75),
    ]
    if discovery_fails:
        scan.arg.side_effect = RuntimeError("unavailable")
    result = scan.run(scope=["sub-a", "sub-b"])
    groups = {group["subscription_id"]: group for group in result["unmatched_groups"]}

    assert result["count"] == 2
    assert result["total"] == 125
    assert groups["sub-a"]["cost"] == 50
    assert groups["sub-b"]["cost"] == 75
    assert all(group["inventory_status"] == ("unknown" if discovery_fails else "not_found")
               for group in groups.values())


def test_detected_custom_group_mapping_does_not_claim_same_group_in_other_subscription(scan):
    scan.arg.return_value = [cluster(sub="SUB-A", node_group="PRODUCTION-NODES")]
    scan.rows = [
        charge("production-nodes", sub="sub-a", cost=40),
        charge("production-nodes", sub="sub-b", cost=15, service="Azure Kubernetes Service"),
    ]
    result = scan.run(scope=["sub-a", "sub-b"])
    items = {item["subscription_id"]: item for item in result["clusters"]}

    assert result["count"] == 2
    assert result["detected_clusters"] == 1
    assert items["sub-a"]["inventory_status"] == "detected"
    assert items["sub-a"]["cost"] == 40
    assert items["sub-b"]["inventory_status"] == "not_found"
    assert items["sub-b"]["cost"] == 15
    assert result["unmatched_groups"][0]["subscription_id"] == "sub-b"


def test_matched_shared_groups_use_subscription_and_cluster_identity(scan):
    scan.arg.return_value = [
        cluster(name="same", sub="sub-a", node_group="shared-nodes", id=None),
        cluster(name="same", sub="sub-b", node_group="shared-nodes", id=None),
    ]
    scan.rows = [
        charge("shared-nodes", sub="sub-a", cost=50),
        charge("shared-nodes", sub="sub-b", cost=75),
    ]
    result = scan.run(scope=["sub-a", "sub-b"])
    items = {item["subscription_id"]: item for item in result["clusters"]}

    assert result["count"] == result["detected_clusters"] == 2
    assert result["total"] == 125
    assert items["sub-a"]["cost"] == 50
    assert items["sub-b"]["cost"] == 75
    assert all(item["inventory_status"] == "detected" for item in items.values())
    assert result["unmatched_groups"] == []


def test_missing_subscription_cannot_establish_cluster_absence(scan):
    scan.rows = [charge(sub=None)]
    result = scan.run()

    assert result["discovery_status"] == "ok"
    assert result["clusters"][0]["subscription_id"] is None
    assert result["clusters"][0]["inventory_status"] == "unknown"


@pytest.mark.parametrize("discovery_fails", [False, True])
def test_empty_billing_preserves_discovery_outcome(scan, discovery_fails):
    scan.hi = None
    if discovery_fails:
        scan.arg.side_effect = RuntimeError("unavailable")
    result = scan.run()

    assert result["empty"] is True
    assert result["billing_empty"] is True
    assert result["discovery_status"] == ("unavailable" if discovery_fails else "ok")
    assert result["detected_clusters"] == (None if discovery_fails else 0)
    assert result["clusters"] == result["unmatched_groups"] == result["trend"] == []
    assert result["total"] == 0


def test_discovery_status_does_not_change_cost_categories_or_totals(scan):
    scan.arg.return_value = [cluster()]
    scan.rows = [
        charge(cost=100),
        charge(cost=12, service="Storage"),
        charge(cost=34, service="Load Balancer"),
    ]
    result = scan.run()

    assert result["count"] == 1
    assert result["total"] == result["clusters"][0]["cost"] == 146
    assert result["by_category"] == {"nodes": 100, "storage": 12, "networking": 34}
    assert result["currency"] == "USD"
