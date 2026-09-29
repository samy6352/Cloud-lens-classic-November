"""AKS cost: what a cluster actually costs, rather than what the cluster resource costs.

An AKS cluster bills almost nothing under its own name. The money is in a second resource group
Azure creates and manages — `MC_<cluster-rg>_<cluster>_<region>` — holding the node VMs, their
disks, the load balancers, the public IPs and the egress. Looking at the cluster resource shows
the control plane charge and nothing else, which on the Free tier is zero. Every other tab in
this app therefore reports AKS spend as a scattering of unattributed VM and networking lines.

Two ways to map a node resource group back to a cluster, and the difference matters:

  * **Resource Graph**, which reads `properties.nodeResourceGroup` from the cluster itself. This
    is authoritative — Azure's own answer — and survives an operator who renamed things or set
    a custom node resource group, which the naming convention does not.

  * **The `MC_` convention**, parsed from the name. Only used when no live cluster claims the
    group, and always reported as inferred.

The fallback is not a nicety. Verified against this estate: the warehouse holds 228 rows and
$81 of `MC_` spend for two clusters that no longer exist. A view built purely on Resource Graph
would show nothing at all and quietly imply the AKS bill was zero — which is the same
empty-means-zero error as everywhere else, wearing a Kubernetes hat. Deleted clusters are
exactly when somebody asks what they were costing.

Parsing is ambiguous by nature: both the cluster resource group and the cluster name may contain
underscores, so `MC_a_b_c_eastus` has more than one honest reading. The parse is therefore
best-effort, marked as such, and never overrides a live cluster.
"""
from __future__ import annotations

import logging
import re
from typing import Any

log = logging.getLogger("cloudlens.aks")

CLUSTER_QUERY = """
    resources
    | where type =~ 'microsoft.containerservice/managedclusters'
    | project
        name,
        id,
        clusterResourceGroup = resourceGroup,
        subscriptionId,
        location,
        nodeResourceGroup = tostring(properties.nodeResourceGroup),
        kubernetesVersion = tostring(properties.kubernetesVersion),
        tier              = tostring(sku.tier),
        poolCount         = array_length(properties.agentPoolProfiles),
        powerState        = tostring(properties.powerState.code)
"""

# Azure's default node resource group name. The cluster resource group and the cluster name can
# both contain underscores, so this cannot be split reliably — the region at the end is the only
# anchored part. Captured loosely and reported as a guess.
MC_RE = re.compile(r"^mc_(?P<rest>.+)_(?P<region>[a-z0-9]+)$", re.IGNORECASE)

# What the money in a node resource group is actually being spent on. Grouped this way because
# the answers differ in what you would do about them: nodes are a rightsizing question, disks
# often an orphan question, networking usually an architecture one.
CATEGORIES = (
    ("nodes", ("virtual machines", "virtual machines licenses", "azure compute")),
    ("storage", ("storage", "managed disks", "azure files")),
    ("networking", ("load balancer", "virtual network", "bandwidth", "ip addresses",
                    "nat gateway", "application gateway")),
    ("monitoring", ("azure monitor", "log analytics")),
)


def categorise(service: str | None) -> str:
    low = str(service or "").strip().lower()
    if not low:
        return "other"
    for name, needles in CATEGORIES:
        if any(n in low for n in needles):
            return name
    return "other"


def parse_node_group(name: str) -> dict[str, str] | None:
    """A best-effort read of Azure's default node resource group name.

    Returns the region confidently and the rest as one string, because splitting `rest` into a
    cluster resource group and a cluster name requires knowing where one ends — and with
    underscores legal in both, the name alone does not say. Guessing a split would produce a
    confident, wrong cluster name on the estates most likely to have unusual naming.
    """
    m = MC_RE.match(str(name or "").strip())
    if not m:
        return None
    rest = m.group("rest")
    # The common case, and the only one worth naming: neither part contains an underscore.
    parts = rest.split("_")
    cluster = parts[-1] if len(parts) == 2 else None
    return {
        "region": m.group("region"),
        "cluster": cluster or rest,
        "cluster_resource_group": parts[0] if len(parts) == 2 else None,
        "certain": bool(cluster),
    }


async def _clusters(subscription_ids: list[str]) -> list[dict[str, Any]]:
    from .waste import _arg

    return await _arg(CLUSTER_QUERY, subscription_ids, top=500)


def _cost_rows(
    wh: Any, scope: list[str] | None, days: int, currency: str | None,
    node_groups: list[tuple[str, str]] | None = None,
) -> tuple[list[dict], list[dict], str | None]:
    from . import reporting

    days = reporting.period_days(days)
    with wh.reader(scope, currency=currency) as r:
        hi = r.rows(f'SELECT max("ChargePeriodStart") AS hi FROM {r.agg}')
        hi = hi[0]["hi"] if hi and hi[0]["hi"] else None
        if not hi:
            return [], [], None

        period = f'"ChargePeriodStart" > {reporting.start_sql(hi, days)}'
        # Both halves of the bill in one pass: the managed node group, and anything charged
        # against the Kubernetes service itself, which is where the Standard-tier control plane
        # (and Automatic-tier) uptime SLA lands.
        aks_like = ('lower("ResourceGroup") LIKE \'mc\\_%\' ESCAPE \'\\\' '
                    'OR lower("ServiceName") LIKE \'%kubernetes%\'')
        if node_groups:
            pairs = ", ".join(
                "('" + sub.lower().replace("'", "''") + "', '"
                + rg.lower().replace("'", "''") + "')"
                for sub, rg in sorted(set(node_groups))
            )
            aks_like += f' OR (lower("SubAccountId"), lower("ResourceGroup")) IN ({pairs})'
        # `r.named` rather than `costs`: this needs `count(DISTINCT "ResourceId")` as well as
        # the money, and the presence table is the one derived table that keeps resource
        # identity -- `ResourceId` is in its `COUNTABLE` set, so the count is exact, not an
        # estimate. `named` is precisely the right handle for a query that counts *and*
        # prices: it resolves to the presence table where that table can also price, and to
        # the detail table under a currency conversion, which presence cannot serve because
        # `CostInUsd` is outside its grain. Every column the filter selects on --
        # `ResourceGroup`, `ServiceName`, `SubAccountId` -- is in that grain, so the
        # restriction means the same thing there as it did here.
        by_group = r.rows(
            'SELECT "ResourceGroup" AS rg, "SubAccountId" AS sub, "ServiceName" AS service, '
            '"RegionName" AS region, "BillingCurrency" AS currency, '
            'sum("BilledCost") AS cost, count(DISTINCT "ResourceId") AS resources '
            f'FROM {r.named} WHERE {period} AND ({aks_like}) '
            "GROUP BY 1, 2, 3, 4, 5 ORDER BY cost DESC", None)
        # The daily line needs no resource identity, so it takes the smaller table: every
        # column it reads or filters on is in the rollup grain.
        daily = r.rows(
            'SELECT "ChargePeriodStart" AS day, "BillingCurrency" AS currency, '
            'sum("BilledCost") AS cost '
            f'FROM {r.agg} WHERE {period} AND ({aks_like}) '
            "GROUP BY 1, 2 ORDER BY 1", None)
    return by_group, daily, str(hi)


async def clusters(scope: list[str] | None = None, days: int = 30,
                   currency: str | None = None) -> dict[str, Any]:
    """AKS spend, attributed to the cluster that caused it."""
    from . import reporting
    from .cost import list_subscriptions
    from .currency import currency_code
    from .warehouse import warehouse

    days = reporting.period_days(days)

    live = []
    searched_subs: set[str] = set()
    discovery_status = "unavailable"
    discovery_note = (
        "Cluster discovery is unavailable. Billing history is still shown; "
        "unmatched groups have unknown inventory status."
    )
    try:
        subs = scope or [s["id"] for s in (await list_subscriptions())["subscriptions"]]
        if subs and all(isinstance(sub, str) and sub.strip() for sub in subs):
            live = await _clusters(subs)
            searched_subs = {sub.lower() for sub in subs}
            discovery_status = "partial" if len(live) >= 500 else "ok"
            discovery_note = (
                "Cluster discovery may be incomplete at the result limit. The detected count "
                "is a lower bound; unmatched groups have unknown inventory status."
                if discovery_status == "partial" else
                "Clusters detected by Resource Graph in the searched subscriptions. "
                "Detection does not mean a cluster is running; an unmatched group "
                "does not establish deletion."
            )
        else:
            discovery_note = (
                "No subscription scope is available for cluster discovery. Billing history "
                "is still shown; cluster presence is unknown."
            )
    except Exception as exc:  # noqa: BLE001 - discovery must not blank historic cost
        log.info("aks: cluster discovery unavailable (%s)", type(exc).__name__)
        live = []
    detected_clusters = len(live) if discovery_status != "unavailable" else None

    # Azure's own mapping wins wherever it exists.
    by_node_rg: dict[tuple[str, str], dict[str, Any]] = {}
    for c in live:
        node_rg = str(c.get("nodeResourceGroup") or "").lower()
        sub = str(c.get("subscriptionId") or "").lower()
        if node_rg and sub:
            by_node_rg[sub, node_rg] = c

    import asyncio

    from . import cost as _cost  # noqa: F401  (kept for symmetry with other modules)

    rows, daily, as_of = await asyncio.to_thread(
        _cost_rows, warehouse, scope, days, currency, list(by_node_rg))
    found: dict[str, dict[str, Any]] = {}
    billed_keys: set[str] = set()
    for row in rows:
        rg = str(row.get("rg") or "")
        low = rg.lower()
        sub = str(row.get("sub") or "").lower()
        known = by_node_rg.get((sub, low))
        parsed = parse_node_group(rg)

        if known:
            key = str(known.get("id") or f"{sub}:{known.get('name')}")
            name = known.get("name")
            certain = True
        elif parsed:
            key = f"{sub}:{low}"
            name = parsed["cluster"]
            certain = parsed["certain"]
        else:
            # Charged against the Kubernetes service but not in a node group: the control plane
            # of a cluster whose node group produced no cost in this window.
            key = f"service:{sub}:{low or 'unknown'}"
            name = rg or "Azure Kubernetes Service"
            certain = False

        billed_keys.add(key)
        code = currency_code(row.get("currency")) or "unknown"
        key = f"{key}\0{code}"
        c = found.setdefault(key, {
            "cluster": name,
            "node_resource_group": rg,
            "region": row.get("region") or (parsed or {}).get("region"),
            "currency": code,
            "subscription_id": sub or None,
            "subscriptions": set(),
            "cost": 0.0,
            "resources": 0,
            "by_category": {},
            "services": {},
            "live": bool(known),
            "inventory_status": ("detected" if known else
                                 "not_found" if discovery_status == "ok" and sub in searched_subs
                                 else "unknown"),
            "certain": certain,
            "kubernetes_version": (known or {}).get("kubernetesVersion"),
            "tier": (known or {}).get("tier"),
            "node_pools": (known or {}).get("poolCount"),
            "power_state": (known or {}).get("powerState"),
            "cluster_resource_group": ((known or {}).get("clusterResourceGroup")
                                       or (parsed or {}).get("cluster_resource_group")),
        })
        amount = float(row.get("cost") or 0)
        c["cost"] += amount
        c["resources"] += int(row.get("resources") or 0)
        if row.get("sub"):
            c["subscriptions"].add(str(row["sub"]))
        cat = categorise(row.get("service"))
        c["by_category"][cat] = c["by_category"].get(cat, 0.0) + amount
        svc = str(row.get("service") or "other")
        c["services"][svc] = c["services"].get(svc, 0.0) + amount

    # A live cluster with no cost in the window is still a cluster. Reported at zero rather than
    # omitted, because "we have five clusters and this lists three" is the kind of gap that makes
    # somebody distrust the whole page.
    for c in live:
        node_rg = str(c.get("nodeResourceGroup") or "")
        sub = str(c.get("subscriptionId") or "").lower()
        key = str(c.get("id") or f"{sub}:{c.get('name')}")
        if key not in billed_keys:
            found[key] = {
                "cluster": c.get("name"), "node_resource_group": node_rg,
                "region": c.get("location"), "currency": None,
                "subscription_id": sub or None, "subscriptions": {sub} if sub else set(),
                "cost": 0.0, "resources": 0, "by_category": {}, "services": {},
                "live": True, "inventory_status": "detected", "certain": True,
                "kubernetes_version": c.get("kubernetesVersion"), "tier": c.get("tier"),
                "node_pools": c.get("poolCount"), "power_state": c.get("powerState"),
                "cluster_resource_group": c.get("clusterResourceGroup"),
            }

    known_codes = {c["currency"] for c in found.values() if c.get("currency")}
    fallback_currency = currency_code(currency) or (next(iter(known_codes)) if len(known_codes) == 1 else None)
    unclassified = [c for c in found.values() if not c.get("currency") and c["resources"]]
    monetary_unavailable = bool(unclassified)
    by_rg_day: dict[tuple[str, str], float] = {}
    for d in daily:
        code = currency_code(d.get("currency"))
        if not code:
            monetary_unavailable = True
            continue
        key = (code, str(d["day"]))
        by_rg_day[key] = by_rg_day.get(key, 0.0) + float(d.get("cost") or 0)

    items = []
    for c in found.values():
        c["_unrounded_cost"] = c["cost"]
        c["cost"] = round(c["cost"], 2)
        c["subscriptions"] = len(c["subscriptions"])
        c["services"] = sorted(c["services"].items(), key=lambda kv: -kv[1])[:6]
        items.append(c)
    items.sort(key=lambda c: (c.get("currency") or "", -c["cost"]))

    totals: dict[str, float] = {}
    categories_by_currency: dict[str, dict[str, float]] = {}
    for c in items:
        raw_cost = c.pop("_unrounded_cost")
        code = c.get("currency") or (fallback_currency if not c["resources"] else None)
        if not code:
            continue
        totals[code] = totals.get(code, 0.0) + raw_cost
        categories = categories_by_currency.setdefault(code, {})
        for cat, amount in c["by_category"].items():
            categories[cat] = categories.get(cat, 0.0) + amount
    categories_by_currency = {
        code: {category: round(amount, 2) for category, amount in values.items()}
        for code, values in categories_by_currency.items()
    }
    single = next(iter(totals)) if len(totals) == 1 and not monetary_unavailable else None
    total = round(totals[single], 2) if single else (
        0.0 if not totals and not monetary_unavailable else None)
    trend_by_currency: dict[str, list[dict[str, Any]]] = {}
    for (code, day), amount in sorted(by_rg_day.items()):
        trend_by_currency.setdefault(code, []).append({"day": day, "cost": round(amount, 2)})
    total_count = len(items)
    items = items[:1000]

    unmatched_groups = [
        {key: c[key] for key in
         ("cluster", "subscription_id", "node_resource_group", "cost", "inventory_status")}
        for c in items if not c["live"]
    ]
    unmatched_note = (
        "These billing groups could not be matched to a detected cluster. "
        "Not found means no match in the searched inventory; unknown means discovery "
        "was incomplete, unavailable, or did not cover the subscription. "
        "Neither status establishes deletion or ongoing charges."
        if unmatched_groups else None
    )

    return {
        "empty": as_of is None and not items,
        "billing_empty": as_of is None,
        "days": days,
        "as_of": as_of,
        "currency": single or (fallback_currency if not totals and not monetary_unavailable else None),
        "monetary_status": "unavailable" if monetary_unavailable else "complete",
        "unclassified_cluster_records": len(unclassified),
        "mixed_currency": len(totals) > 1,
        "total_by_currency": {code: round(value, 2) if code != "unknown" else None
                              for code, value in totals.items()},
        "clusters": items,
        "count": len(items),
        "total_cluster_records": total_count,
        "truncated": total_count > len(items),
        "totals_scope": "all_matching_billing",
        "live_clusters": detected_clusters,
        "detected_clusters": detected_clusters,
        "discovery_status": discovery_status,
        "discovery_note": discovery_note,
        "total": total,
        "by_category": categories_by_currency.get(single, {}),
        "categories_by_currency": categories_by_currency,
        "unmatched_groups": unmatched_groups,
        "unmatched_note": unmatched_note,
        "orphaned": [c["cluster"] for c in unmatched_groups if c["cost"] > 0],
        "trend": trend_by_currency.get(single or fallback_currency, []),
        "trend_by_currency": trend_by_currency,
        "note": ("An AKS cluster bills through a managed node resource group, so most of this is "
                 "node VMs, disks and networking rather than the cluster resource itself."),
        "orphan_note": unmatched_note,
    }
