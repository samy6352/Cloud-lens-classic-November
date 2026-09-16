"""Waste and utilisation analysis: what you are paying for but not using.

Cost data alone answers "what did I spend". This module answers "what could I stop spending",
by combining three sources:

  * **Azure Resource Graph** — the inventory patterns that indicate waste: disks with no VM,
    public IPs with no NIC, app service plans with no sites, and so on.
  * **The cost warehouse** — the *actual billed cost* of each of those resources. This is
    the part that makes a finding actionable: "12 unattached disks" is trivia, "12 unattached
    disks that cost you $47 last month" is a decision.
  * **Azure Monitor metrics** — CPU utilisation, to spot VMs that are running but idle.

Everything here is read-only.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime, timedelta
from typing import Any

from .cost import ARM, CostError, azure, list_subscriptions

log = logging.getLogger("cloudlens.waste")

ARG_API = "2022-10-01"
METRICS_API = "2023-10-01"

# Each rule is an Azure Resource Graph query that isolates one waste pattern. `id` must be
# projected so the finding can be joined to real spend; everything else is context for the user.
# Gateway existence, provisioned Cosmos throughput and Premium storage SKUs do not establish
# idleness. These require a separate usage/connection review, not automatic savings findings.
RULES: dict[str, dict[str, str]] = {
    "unattached_disks": {
        "title": "Unattached managed disks",
        "why": "Billed at full price while attached to nothing.",
        "query": """
            Resources
            | where type =~ 'microsoft.compute/disks'
            | where tostring(properties.diskState) == 'Unattached'
            | project id, name, resourceGroup, subscriptionId, location,
                      sizeGb = toint(properties.diskSizeGB), sku = tostring(sku.name),
                      detail = strcat(tostring(sku.name), ', ', tostring(properties.diskSizeGB), ' GB')
        """,
    },
    "unassociated_public_ips": {
        "title": "Unassociated public IP addresses",
        "why": "A reserved standard IP is billed whether or not anything answers on it.",
        "query": """
            Resources
            | where type =~ 'microsoft.network/publicipaddresses'
            | where isnull(properties.ipConfiguration) and isnull(properties.natGateway)
            | project id, name, resourceGroup, subscriptionId, location,
                      detail = strcat(tostring(sku.name), ', ',
                                      tostring(properties.publicIPAllocationMethod))
        """,
    },
    "orphaned_nics": {
        "title": "Network interfaces with no VM",
        "why": "Usually left behind by a deleted VM. Free themselves, but a reliable sign of debris.",
        "query": """
            Resources
            | where type =~ 'microsoft.network/networkinterfaces'
            | where isnull(properties.virtualMachine) and isnull(properties.privateEndpoint)
            | project id, name, resourceGroup, subscriptionId, location, detail = 'no VM attached'
        """,
    },
    "stopped_vms": {
        "title": "VMs stopped but not deallocated",
        "why": "Still billed for compute. Deallocating stops the compute charge.",
        "query": """
            Resources
            | where type =~ 'microsoft.compute/virtualmachines'
            | extend state = tostring(properties.extended.instanceView.powerState.code)
            | where state == 'PowerState/stopped'
            | project id, name, resourceGroup, subscriptionId, location,
                      detail = strcat(tostring(properties.hardwareProfile.vmSize), ', stopped')
        """,
    },
    "deallocated_vms": {
        "title": "Deallocated VMs still holding disks",
        "why": "No compute charge, but their OS and data disks are still billed.",
        "query": """
            Resources
            | where type =~ 'microsoft.compute/virtualmachines'
            | extend state = tostring(properties.extended.instanceView.powerState.code)
            | where state == 'PowerState/deallocated'
            | project id, name, resourceGroup, subscriptionId, location,
                      detail = strcat(tostring(properties.hardwareProfile.vmSize), ', deallocated')
        """,
    },
    "empty_app_service_plans": {
        "title": "App Service plans with no apps",
        "why": "A plan is billed on its tier, not on whether anything is deployed to it.",
        "query": """
            Resources
            | where type =~ 'microsoft.web/serverfarms'
            | where toint(properties.numberOfSites) == 0
            | project id, name, resourceGroup, subscriptionId, location,
                      detail = strcat(tostring(sku.name), ', 0 apps')
        """,
    },
    "old_snapshots": {
        "title": "Snapshots older than 90 days",
        "why": "Snapshots accumulate silently and are billed on the space they occupy.",
        "query": """
            Resources
            | where type =~ 'microsoft.compute/snapshots'
            | extend created = todatetime(properties.timeCreated)
            | where created < ago(90d)
            | project id, name, resourceGroup, subscriptionId, location,
                      detail = strcat('created ', format_datetime(created, 'yyyy-MM-dd'))
        """,
    },
    "unused_nsgs": {
        "title": "Network security groups attached to nothing",
        "why": "Free, but clutter that makes the estate harder to reason about.",
        "query": """
            Resources
            | where type =~ 'microsoft.network/networksecuritygroups'
            | where isnull(properties.networkInterfaces) and isnull(properties.subnets)
            | project id, name, resourceGroup, subscriptionId, location, detail = 'unattached'
        """,
    },
    "empty_resource_groups": {
        "title": "Empty resource groups",
        "why": "No cost, but usually a sign of an unfinished clean-up.",
        "query": """
            ResourceContainers
            | where type =~ 'microsoft.resources/subscriptions/resourcegroups'
            | project rgId = tolower(id), name, subscriptionId, location
            | join kind=leftouter (
                Resources | project rgId = tolower(strcat('/subscriptions/', subscriptionId,
                                     '/resourcegroups/', resourceGroup))
                | summarize count() by rgId
              ) on rgId
            | where isnull(count_)
            | project id = rgId, name, resourceGroup = name, subscriptionId, location,
                      detail = 'no resources'
        """,
    },

    # ---------------------------------------------------------------- networking
    #
    # These are the expensive orphans. A NAT gateway or a load balancer left behind by a torn-down
    # environment bills a fixed hourly rate forever and appears in no report, because nothing is
    # wrong with it — it is simply pointed at nothing.
    "idle_nat_gateways": {
        "title": "NAT gateways serving no subnet",
        "why": "Billed hourly plus per-GB regardless. With no subnet attached it processes nothing"
               " and bills the hourly rate anyway — one of the costliest things to leave behind.",
        "query": """
            Resources
            | where type =~ 'microsoft.network/natgateways'
            | where array_length(todynamic(properties.subnets)) == 0
            | project id, name, resourceGroup, subscriptionId, location,
                      detail = strcat(tostring(sku.name), ', no subnets')
        """,
    },
    "empty_load_balancers": {
        "title": "Load balancers with no backend",
        "why": "A Standard load balancer bills hourly whether or not anything sits behind it. An"
               " empty backend pool means traffic has nowhere to go — the rule is still charged.",
        "query": """
            Resources
            | where type =~ 'microsoft.network/loadbalancers'
            | where tostring(sku.name) =~ 'Standard'
            | extend pools = todynamic(properties.backendAddressPools)
            | where array_length(pools) == 0
            | project id, name, resourceGroup, subscriptionId, location,
                      detail = strcat(tostring(sku.name), ', no backend pool')
        """,
    },
    "orphaned_private_endpoints": {
        "title": "Private endpoints with no connection",
        "why": "Billed per hour. One whose target was deleted keeps its NIC and its charge.",
        "query": """
            Resources
            | where type =~ 'microsoft.network/privateendpoints'
            | extend conns = todynamic(properties.privateLinkServiceConnections)
            | where array_length(conns) == 0
            | project id, name, resourceGroup, subscriptionId, location,
                      detail = 'no service connection'
        """,
    },
    "unused_app_gateways": {
        "title": "Application gateways with no backend targets",
        "why": "Billed per hour plus capacity units from the moment it exists, empty or not.",
        "query": """
            Resources
            | where type =~ 'microsoft.network/applicationgateways'
            | extend pools = todynamic(properties.backendAddressPools)
            | mv-expand pool = pools
            | extend addrs = array_length(todynamic(pool.properties.backendAddresses))
            | summarize total = sum(addrs) by id, name, resourceGroup, subscriptionId, location,
                        sku = tostring(properties.sku.name)
            | where total == 0
            | project id, name, resourceGroup, subscriptionId, location,
                      detail = strcat(sku, ', no backend targets')
        """,
    },

    # ------------------------------------------------------------------ databases
    #
    # Deliberately conservative: an idle database is not necessarily waste — a disaster-recovery
    # replica is *supposed* to be quiet. So these report the tier and let a person decide, rather
    # than asserting that something is wrong.
    "stopped_sql_databases": {
        "title": "SQL databases paused or offline",
        "why": "A paused serverless database still bills for storage. One offline for weeks is"
               " usually forgotten rather than resting.",
        "query": """
            Resources
            | where type =~ 'microsoft.sql/servers/databases'
            | where tostring(properties.status) in~ ('Paused', 'Offline', 'Disabled')
            | project id, name, resourceGroup, subscriptionId, location,
                      detail = strcat(tostring(properties.status), ', ',
                                      tostring(sku.name))
        """,
    },
    "oversized_sql_pools": {
        "title": "Elastic pools with one database or none",
        "why": "An elastic pool costs more than a single database and exists to share capacity"
               " across many. With one member or none it is strictly more expensive than not"
               " having it.",
        "query": """
            Resources
            | where type =~ 'microsoft.sql/servers/elasticpools'
            | project poolId = tolower(id), name, resourceGroup, subscriptionId, location,
                      sku = tostring(sku.name)
            | join kind=leftouter (
                Resources
                | where type =~ 'microsoft.sql/servers/databases'
                | where isnotempty(tostring(properties.elasticPoolId))
                | project poolId = tolower(tostring(properties.elasticPoolId))
                | summarize members = count() by poolId
              ) on poolId
            | extend members = coalesce(members, 0)
            | where members <= 1
            | project id = poolId, name, resourceGroup, subscriptionId, location,
                      detail = strcat(sku, ', ', tostring(members), ' database(s)')
        """,
    },
    # -------------------------------------------------------------------- storage
    "classic_disks": {
        "title": "Unmanaged (classic) disks",
        "why": "Page blobs from before managed disks. They bill for the full allocated size"
               " rather than what is used, and cannot take advantage of reserved capacity.",
        "query": """
            Resources
            | where type =~ 'microsoft.compute/virtualmachines'
            | where isnotnull(properties.storageProfile.osDisk.vhd)
            | project id, name, resourceGroup, subscriptionId, location,
                      detail = 'VM using an unmanaged OS disk'
        """,
    },
}


async def _arg(
    query: str, subscriptions: list[str], top: int = 500, *,
    max_records: int = 5000, max_pages: int = 100,
) -> list[dict[str, Any]]:
    from .cost import act_as, acting_as, caller_token, stop_acting

    if top < 1 or max_records < 1 or max_pages < 1:
        raise ValueError("Resource Graph page size and safety bounds must be positive.")
    if not subscriptions:
        return []
    groups: dict[str | None, list[str]] = {}
    for sub in dict.fromkeys(s.lower() for s in subscriptions):
        token = caller_token(f"{ARM}/subscriptions/{sub}")
        groups.setdefault(token, []).append(sub)
    rows: list[dict[str, Any]] = []
    for token, subs in groups.items():
        reset = act_as(token, acting_as()) if token is not None else None
        try:
            rows.extend(await _arg_group(
                query, subs, top, max_records=max(1, max_records - len(rows)),
                max_pages=max_pages))
        finally:
            if reset is not None:
                stop_acting(reset)
        if len(rows) > max_records:
            raise CostError(f"Resource Graph results exceed the {max_records}-record safety limit.")
    return rows


async def _arg_group(
    query: str, subscriptions: list[str], top: int = 500, *,
    max_records: int = 5000, max_pages: int = 100,
) -> list[dict[str, Any]]:
    """Fetch complete results, or fail explicitly instead of returning a truncated inventory.

    `top` is the requested page size, not an overall limit. Azure allows at most 1000 per
    page. Callers can raise the overall record/page safety bounds for larger inventories.
    """
    if top < 1 or max_records < 1 or max_pages < 1:
        raise ValueError("Resource Graph page size and safety bounds must be positive.")
    rows: list[dict[str, Any]] = []
    skip = 0
    token = None
    for _ in range(max_pages):
        page_size = min(top, 1000, max_records - skip)
        options = {"resultFormat": "objectArray", "$top": page_size}
        options.update({"$skipToken": token} if token else {"$skip": skip})
        payload = await azure.request(
            "POST",
            f"{ARM}/providers/Microsoft.ResourceGraph/resources?api-version={ARG_API}",
            json_body={
                "query": query,
                "subscriptions": subscriptions,
                "options": options,
            },
        )
        batch = payload.get("data", []) or []
        rows.extend(batch)
        skip += len(batch)
        token = payload.get("$skipToken")
        total = payload.get("totalRecords")
        truncated = str(payload.get("resultTruncated", "")).lower() == "true"
        more = bool(token) or truncated or (total is not None and skip < int(total))
        if not more and (total is not None or len(batch) < page_size):
            return rows
        if not batch:
            raise CostError("Resource Graph pagination made no progress; results are incomplete.")
        if skip >= max_records:
            raise CostError(
                f"Resource Graph results exceed the {max_records}-record safety limit; "
                "results are incomplete."
            )
    raise CostError(
        f"Resource Graph results exceed the {max_pages}-page safety limit; results are incomplete."
    )


def _costs_for(resource_ids: list[str], days: int, *,
               allow_mixed: bool = False) -> dict[str, dict[str, Any]]:
    """Actual billed cost per resource over the last N days, from the warehouse.

    Resource ids in the cost export are inconsistently cased (both `resourceGroups` and
    `resourcegroups` appear), so the join has to be case-insensitive on both sides.

    Currency-aware callers receive every billing bucket. Legacy scalar consumers fail closed
    on mixed or unknown currencies instead of selecting a numerically larger, unrelated unit.
    """
    if not resource_ids:
        return {}
    from .warehouse import warehouse

    since = (datetime.now(UTC).date() - timedelta(days=max(1, days) - 1)).isoformat()
    wanted = {rid.strip().lower() for rid in resource_ids if rid and rid.strip()}
    if not wanted:
        return {}
    identifiers = ", ".join("'" + rid.replace("'", "''") + "'" for rid in sorted(wanted))
    try:
        with warehouse.reader() as reader:
            rows = reader.rows(
                'SELECT lower(trim("ResourceId")) AS rid, '
                'coalesce(nullif(upper(trim("BillingCurrency")), \'\'), \'unknown\') AS currency, '
                'sum("BilledCost") AS cost '
                f"FROM costs WHERE \"ChargePeriodStart\" >= DATE '{since}' "
                f'AND lower(trim("ResourceId")) IN ({identifiers}) GROUP BY 1,2', None)
    except Exception as exc:  # noqa: BLE001 - waste findings are still useful without cost
        log.warning("cost join failed: %s", exc)
        return {}

    from .currency import currency_code

    grouped: dict[str, dict[str, float]] = {}
    for r in rows:
        if r["rid"] not in wanted:
            continue
        code = currency_code(r["currency"]) or "unknown"
        buckets = grouped.setdefault(r["rid"], {})
        buckets[code] = buckets.get(code, 0.0) + float(r["cost"] or 0)
    codes = {code for buckets in grouped.values() for code in buckets}
    if not allow_mixed and (len(codes) > 1 or "unknown" in codes):
        raise CostError("Billed costs have mixed or unknown currencies; a currency-aware calculation is required.")
    out: dict[str, dict[str, Any]] = {}
    for rid, buckets in grouped.items():
        buckets = {code: round(amount, 2) if code != "unknown" else None for code, amount in buckets.items()}
        single = next(iter(buckets)) if len(buckets) == 1 and "unknown" not in buckets else None
        out[rid] = {"cost": buckets[single] if single else None, "currency": single}
        if allow_mixed:
            out[rid].update(cost_by_currency=buckets,
                            pricing_status="unavailable" if "unknown" in buckets else
                            "mixed_currency" if not single else "priced")
    return out


def _cost_buckets(value: dict[str, Any]) -> dict[str, float | None]:
    from .currency import currency_code

    if value.get("cost_by_currency") is not None:
        return {currency_code(code) or "unknown": amount if currency_code(code) else None
                for code, amount in value["cost_by_currency"].items()}
    if value.get("cost") is None:
        return {}
    code = currency_code(value.get("currency"))
    return {code or "unknown": float(value["cost"]) if code else None}



async def find_waste(
    categories: list[str] | None = None,
    subscription_ids: list[str] | None = None,
    days: int = 30,
    top: int = 15,
) -> dict[str, Any]:
    """Find idle and orphaned resources, with what each has actually cost.

    Args:
        categories: Which rules to run. Omit for all. See RULES for the keys.
        subscription_ids: Scope. Omit for everything the caller can see.
        days: Window for the cost figures.
        top: Max items listed per category (the totals still cover everything found).
    """
    subs = subscription_ids or [s["id"] for s in (await list_subscriptions())["subscriptions"]]
    if not subs:
        raise CostError("No accessible subscriptions.")

    keys = [c for c in (categories or list(RULES)) if c in RULES]
    unknown = [c for c in (categories or []) if c not in RULES]
    if not keys:
        raise CostError(f"Unknown categories {unknown}. Valid: {', '.join(RULES)}")

    results = await asyncio.gather(
        *(_arg(RULES[k]["query"], subs) for k in keys), return_exceptions=True
    )

    # One warehouse pass for every id found, rather than one per category.
    all_ids: list[str] = []
    per_key: dict[str, list[dict]] = {}
    errors: dict[str, str] = {}
    for key, res in zip(keys, results):
        if isinstance(res, Exception):
            log.warning("rule %s failed: %s", key, res)
            errors[key] = str(res)
            per_key[key] = []
            continue
        per_key[key] = res
        all_ids.extend(str(r.get("id", "")) for r in res if r.get("id"))

    # The scoped warehouse read is synchronous; keep it off the event loop.
    cost_by_id = await asyncio.to_thread(_costs_for, all_ids, days, allow_mixed=True)

    findings = []
    # Keep totals per currency; adding EUR to USD would produce a confident, wrong number.
    grand_by_currency: dict[str, float | None] = {}
    for key in keys:
        items = []
        sub_by_currency: dict[str, float | None] = {}
        unpriced = 0
        for r in per_key[key]:
            hit = cost_by_id.get(str(r.get("id", "")).lower()) or {}
            c = hit.get("cost")
            cur = hit["currency"] if hit else None
            buckets = _cost_buckets(hit)
            if not buckets or any(amount is None for amount in buckets.values()):
                unpriced += 1
            for code, amount in buckets.items():
                if amount is None:
                    sub_by_currency[code] = grand_by_currency[code] = None
                    continue
                sub_by_currency[code] = sub_by_currency.get(code, 0.0) + amount
                grand_by_currency[code] = grand_by_currency.get(code, 0.0) + amount
            items.append({
                "name": r.get("name"),
                "resource_group": r.get("resourceGroup"),
                "location": r.get("location"),
                "detail": r.get("detail"),
                "cost": c,
                "currency": cur,
                "cost_by_currency": buckets,
                "pricing_status": hit.get("pricing_status") or ("priced" if cur else "unavailable"),
                "id": r.get("id"),
            })
        items.sort(key=lambda x: (x["currency"] or "", -(x["cost"] or 0)))
        single = next(iter(sub_by_currency)) if len(sub_by_currency) == 1 and not unpriced else None
        if unpriced:
            errors[f"{key}_pricing"] = f"{unpriced} resource(s) have no complete currency-qualified billing cost."
        findings.append({
            "category": key,
            "title": RULES[key]["title"],
            "why": RULES[key]["why"],
            "count": len(items),
            "cost": round(sub_by_currency[single], 2) if single else None,
            "currency": single,
            "cost_by_currency": {code: round(amount, 2) if amount is not None else None
                                 for code, amount in sub_by_currency.items()},
            "unpriced_count": unpriced,
            "items": items[:top],
            "resource_ids": [i["id"] for i in items if i.get("id")],
            "more": max(0, len(items) - top),
        })

    findings.sort(key=lambda f: (f["cost"] or 0, f["count"]), reverse=True)
    priced = sum(1 for i in cost_by_id.values() if _cost_buckets(i))
    currencies = sorted(grand_by_currency)
    mixed = len(currencies) > 1

    return {
        "period_days": days,
        "subscriptions": len(subs),
        "total_items": sum(f["count"] for f in findings),
        # Only a single-currency estate gets a single total.
        "total_cost": round(sum(grand_by_currency.values()), 2) if not mixed and not errors else None,
        "total_by_currency": {code: round(amount, 2) if code != "unknown" else None
                              for code, amount in grand_by_currency.items()},
        "currency": currencies[0] if len(currencies) == 1 and currencies[0] != "unknown" else None,
        "mixed_currency": mixed,
        "findings": findings,
        "errors": errors,
        "partial": bool(errors),
        "review_note": (
            "VPN/ExpressRoute gateways, provisioned Cosmos DB accounts and Premium storage "
            "accounts are excluded: inventory alone does not prove they are idle or empty. "
            "Review connections and usage before considering changes; no savings are assumed."
        ),
        "note": (
            "Cost is what these resources actually billed in the window, taken from the local "
            "warehouse. Items showing 0 either cost nothing (NICs, NSGs) or fall outside the "
            "loaded date range."
            if priced else
            "No costs could be matched — the warehouse may be empty or the window may not "
            "overlap the loaded data."
        ),
    }


async def vm_utilisation(
    subscription_ids: list[str] | None = None,
    days: int = 30,
    cpu_threshold: float = 5.0,
) -> dict[str, Any]:
    """Average and peak CPU per VM, to separate genuinely busy machines from idle ones.

    Args:
        subscription_ids: Scope. Omit for everything the caller can see.
        days: Look-back window (1-90).
        cpu_threshold: Average CPU below this is reported as idle.
    """
    days = max(1, min(int(days), 90))
    subs = subscription_ids or [s["id"] for s in (await list_subscriptions())["subscriptions"]]

    vms = await _arg(
        """
        Resources
        | where type =~ 'microsoft.compute/virtualmachines'
        | project id, name, resourceGroup, subscriptionId, location,
                  size = tostring(properties.hardwareProfile.vmSize),
                  state = tostring(properties.extended.instanceView.powerState.code)
        """,
        subs,
    )
    if not vms:
        return {"vms": [], "count": 0, "note": "No virtual machines found in this scope."}

    end = datetime.now(UTC).replace(microsecond=0)
    start = end - timedelta(days=days)
    running = [v for v in vms if v.get("state") == "PowerState/running"]

    # The batch endpoint is per region *and* per subscription, so group by both.
    groups: dict[tuple[str, str], list[str]] = {}
    for v in running:
        groups.setdefault((v["location"], v["subscriptionId"]), []).append(v["id"])

    async def batch(region: str, sub: str, ids: list[str]) -> dict[str, dict]:
        out: dict[str, dict] = {}
        for chunk in (ids[i:i + 50] for i in range(0, len(ids), 50)):
            try:
                payload = await azure.request(
                    "POST",
                    f"https://{region}.metrics.monitor.azure.com/subscriptions/{sub}/metrics:getBatch",
                    params={
                        "api-version": METRICS_API,
                        "metricnamespace": "Microsoft.Compute/virtualMachines",
                        "metricnames": "Percentage CPU",
                        "aggregation": "average,maximum",
                        "interval": "P1D",
                        "starttime": start.strftime("%Y-%m-%dT%H:%M:%SZ"),
                        "endtime": end.strftime("%Y-%m-%dT%H:%M:%SZ"),
                    },
                    json_body={"resourceids": chunk},
                    cache=False,
                )
            except Exception as exc:  # noqa: BLE001 - a region without metrics must not fail all
                log.warning("metrics failed for %s/%s: %s", region, sub, exc)
                continue

            for entry in payload.get("values", []):
                for metric in entry.get("value", []):
                    rid = str(metric.get("id", "")).split("/providers/Microsoft.Insights")[0]
                    series = (metric.get("timeseries") or [{}])[0].get("data", [])
                    pts = [p for p in series if p.get("average") is not None]
                    if pts:
                        out[rid.lower()] = {
                            "avg": round(sum(p["average"] for p in pts) / len(pts), 1),
                            "peak": round(max((p.get("maximum") or 0) for p in pts), 1),
                            "days": len(pts),
                        }
        return out

    metric_sets = await asyncio.gather(
        *(batch(region, sub, ids) for (region, sub), ids in groups.items()),
        return_exceptions=True,
    )
    cpu: dict[str, dict] = {}
    for m in metric_sets:
        if isinstance(m, dict):
            cpu.update(m)

    cost_by_id = await asyncio.to_thread(_costs_for, [v["id"] for v in vms], days, allow_mixed=True)

    rows = []
    for v in vms:
        m = cpu.get(v["id"].lower())
        billed = cost_by_id.get(v["id"].lower()) or {}
        state = (v.get("state") or "").replace("PowerState/", "") or "unknown"
        rows.append({
            "id": v["id"],
            "name": v["name"],
            "resource_group": v["resourceGroup"],
            "subscription_id": v.get("subscriptionId"),
            "size": v.get("size"),
            "location": v.get("location"),
            "state": state,
            "cpu_avg": m["avg"] if m else None,
            "cpu_peak": m["peak"] if m else None,
            "days_with_data": m["days"] if m else 0,
            "cost": billed.get("cost"),
            "currency": billed.get("currency"),
            "cost_by_currency": _cost_buckets(billed),
            "pricing_status": billed.get("pricing_status") or ("priced" if billed.get("currency") else "unavailable"),
        })

    rows.sort(key=lambda r: (r["cpu_avg"] is None, r["cpu_avg"] or 0))
    idle = [r for r in rows if r["cpu_avg"] is not None and r["cpu_avg"] < cpu_threshold]
    stopped = [r for r in rows if r["state"] in ("deallocated", "stopped")]

    def totals(selected):
        buckets: dict[str, float | None] = {}
        incomplete = False
        for row in selected:
            costs = _cost_buckets(row)
            incomplete |= not costs or any(amount is None for amount in costs.values())
            for code, amount in costs.items():
                if amount is None:
                    buckets[code] = None
                    continue
                buckets[code] = buckets.get(code, 0.0) + amount
        scalar = round(sum(buckets.values()), 2) if len(buckets) <= 1 and not incomplete else None
        return scalar, {code: round(amount, 2) if code != "unknown" else None for code, amount in buckets.items()}

    idle_cost, idle_buckets = totals(idle)
    stopped_cost, stopped_buckets = totals(stopped)
    unpriced = sum(not r["cost_by_currency"] or any(amount is None for amount in r["cost_by_currency"].values())
                   for r in rows)
    codes = {code for row in rows for code in row["cost_by_currency"]}
    return {
        "period_days": days,
        "cpu_threshold": cpu_threshold,
        "count": len(rows),
        "running": len(running),
        "idle_count": len(idle),
        "stopped_count": len(stopped),
        "idle_cost": idle_cost,
        "stopped_cost": stopped_cost,
        "idle_cost_by_currency": idle_buckets,
        "stopped_cost_by_currency": stopped_buckets,
        "currency": next(iter(codes)) if len(codes) == 1 and not unpriced else None,
        "mixed_currency": len(codes) > 1,
        "partial": bool(unpriced),
        "errors": [f"{unpriced} VM(s) have no complete currency-qualified billing cost."] if unpriced else [],
        "vms": rows,
        "note": (
            "CPU is unavailable for VMs that are not running; those are reported by state "
            "instead. Cost is the VM's own billed compute over the window — its disks are "
            "billed separately and appear under the disk resources."
        ),
    }


async def advisor_recommendations(subscription_ids: list[str] | None = None,
                                  category: str = "Cost") -> dict[str, Any]:
    """Azure Advisor recommendations, with Microsoft's own savings estimate where present."""
    subs = subscription_ids or [s["id"] for s in (await list_subscriptions())["subscriptions"]]

    async def one(sub: str) -> list[dict]:
        from urllib.parse import urlparse

        url = f"{ARM}/subscriptions/{sub}/providers/Microsoft.Advisor/recommendations"
        params = {"api-version": "2023-01-01", "$filter": f"Category eq '{category}'"}
        records: list[dict] = []
        seen: set[str] = set()
        for _ in range(100):
            if url in seen:
                raise CostError("Advisor pagination repeated a page.")
            seen.add(url)
            payload = await azure.request("GET", url, params=params)
            records.extend(payload.get("value") or [])
            if len(records) > 5000:
                raise CostError("Advisor exceeds the 5000-recommendation safety limit.")
            next_url = payload.get("nextLink")
            if not next_url:
                break
            parsed, original = urlparse(next_url), urlparse(url)
            if (parsed.scheme != "https" or parsed.netloc != original.netloc
                    or parsed.path.lower() != original.path.lower()):
                raise CostError("Advisor returned an invalid continuation URL.")
            url, params = next_url, None
        else:
            raise CostError("Advisor pagination exceeds the safety limit.")
        out = []
        for rec in records:
            p = rec.get("properties", {})
            ext = p.get("extendedProperties") or {}
            resource_id = str((p.get("resourceMetadata") or {}).get("resourceId") or "")
            parts = resource_id.strip("/").split("/")
            resource_group = next((parts[i + 1] for i, part in enumerate(parts[:-1])
                                   if part.lower() == "resourcegroups"), None)
            out.append({
                "problem": (p.get("shortDescription") or {}).get("problem"),
                "solution": (p.get("shortDescription") or {}).get("solution"),
                "impact": p.get("impact"),
                "resource": resource_id.rstrip("/").split("/")[-1],
                "resource_id": resource_id.lower(),
                "subscriptionId": sub,
                "resourceGroup": resource_group,
                "annual_savings": _to_float(ext.get("annualSavingsAmount")),
                "monthly_savings": _to_float(ext.get("savingsAmount")),
                "currency": str(ext.get("savingsCurrency") or "").strip().upper() or None,
                # Reservation recommendations describe their SKU under `displaySKU`/`sku`, not
                # `targetSku` — that name belongs to the rightsizing ones. Reading only the
                # latter left every reservation with a null SKU, which made six rows offering
                # one-year and three-year terms on the same plan look like six copies of the
                # same recommendation with no way to tell them apart.
                "sku": (ext.get("displaySKU") or ext.get("targetSku")
                        or ext.get("sku") or ext.get("skuName")
                        or ext.get("targetResourceType")),
                # What actually distinguishes one reservation recommendation from the next.
                "term": ext.get("term"),
                "quantity": ext.get("displayQty") or ext.get("qty"),
                "region": ext.get("region") or ext.get("armRegionName"),
                "scope": ext.get("scope"),
                "resource_type": ext.get("reservedResourceType"),
                # Not a choice the reader makes — it is how far back Advisor looked to produce
                # the estimate. Kept because the spread across lookbacks is the honest measure
                # of how firm the number is, and because without it the same recommendation
                # priced over 7, 30 and 60 days is indistinguishable from three separate ones.
                "lookback_days": _to_int(ext.get("lookbackPeriod")),
            })
        return out

    results = await asyncio.gather(*(one(s) for s in subs), return_exceptions=True)
    recs = [r for res in results if isinstance(res, list) for r in res]
    errors = [{"subscription_id": sub, "error": str(result)}
              for sub, result in zip(subs, results) if isinstance(result, Exception)]
    recs.sort(key=lambda r: r["annual_savings"] or 0, reverse=True)

    # The headline used to be the sum of every row, which counted the same reservation once per
    # lookback window Advisor priced it over — three times for most of them, so an estate with
    # two real reservation decisions reported six. Sum one row per distinct decision instead:
    # same problem, resource, SKU, term and quantity is one thing you can buy, however many ways
    # Advisor estimated it. The longest lookback wins, because it is the estimate with the most
    # evidence behind it — and notably not the largest, since the shortest window is usually the
    # most optimistic.
    best: dict[tuple, dict[str, Any]] = {}
    for r in recs:
        key = (r.get("problem"), r.get("resource_id") or r.get("resource"),
               r.get("subscriptionId"), r.get("sku"), r.get("region"), r.get("scope"),
               r.get("term"), str(r.get("quantity")), r.get("currency"))
        prior = best.get(key)
        if prior is None or (r.get("lookback_days") or 0) > (prior.get("lookback_days") or 0):
            best[key] = r

    # Then collapse the terms, because the first pass was not enough.
    #
    # Advisor prices the same reservation over one year and three years, and both survived the
    # key above — so the headline added them together and claimed a saving nobody can buy. You
    # purchase P1Y or P3Y, never both. On this estate that inflated $3,395 to $5,445, a 38%
    # overstatement, and it put the Advisor tab in direct contradiction with the Savings tab,
    # which had always collapsed them correctly. Two numbers for the same question is the one
    # thing a cost tool cannot afford.
    #
    # The larger saving wins rather than the longer lookback: these are alternatives, not
    # estimates of one thing, and the best available deal is the actionable figure.
    actionable: dict[tuple, dict[str, Any]] = {}
    for r in best.values():
        key = (r.get("problem"), r.get("resource_id") or r.get("resource"),
               r.get("subscriptionId"), r.get("sku"), r.get("region"), r.get("scope"),
               str(r.get("quantity")), r.get("currency"))
        prior = actionable.get(key)
        if prior is None or (r.get("annual_savings") or 0) > (prior.get("annual_savings") or 0):
            actionable[key] = r
    totals: dict[str, float] = {}
    for r in actionable.values():
        code = str(r.get("currency") or "unknown").upper()
        totals[code] = totals.get(code, 0.0) + (r["annual_savings"] or 0)
    single = next(iter(totals)) if len(totals) == 1 and "UNKNOWN" not in totals else None

    return {
        "category": category,
        "count": len(recs),
        "distinct_count": len(actionable),
        "estimated_annual_savings": round(totals[single], 2) if single else None,
        "annual_savings_by_currency": {code: round(value, 2) for code, value in totals.items()},
        "currency": single,
        "mixed_currency": len(totals) > 1,
        "recommendations": recs,
        "partial": bool(errors),
        "errors": errors,
        "error": "Advisor could not be read for any selected subscription."
                 if errors and len(errors) == len(subs) else None,
        "note": "Empty is normal — Advisor needs a few days of usage before it reports."
                if not recs and not errors else None,
    }


def _to_float(v: Any) -> float | None:
    try:
        return round(float(v), 2)
    except (TypeError, ValueError):
        return None


def _to_int(v: Any) -> int | None:
    try:
        return int(float(v))
    except (TypeError, ValueError):
        return None
