"""What-if: the cost of resizing or shutting down one virtual machine, before doing either.

The Rightsizing view already says a VM is idle; it does not say what resizing it would actually
save, because that needs a second number rightsizing never had to compute -- what another SKU
costs. This answers exactly that, for one VM at a time, on request rather than for every VM in
the estate: the retail catalogue is a public but rate-limited API, and asking it once per row of
a table nobody is about to act on today would be work spent for nothing.

Two figures are compared, not one, because a fair comparison needs both:

  * **What you are actually billed today** -- real, from the warehouse. This can already be
    below the public rate if Hybrid Benefit or a reservation applies to the current VM.
  * **What the current size costs at the plain public rate** -- so a discount already in place
    on the *current* VM does not make a resize look better than it is; the two SKUs are priced
    on the same footing.

The target SKU (or shutdown) is priced at the same plain public rate, since there is no existing
discount to carry over to a size nothing has ever run. The gap between "billed today" and
"current size at retail" is exactly the caveat this surfaces rather than hides.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any

import httpx

log = logging.getLogger("cloudlens.whatif")

PRICES_URL = "https://prices.azure.com/api/retail/prices"


class WhatIfError(ValueError):
    """Something the caller can fix, phrased for them rather than for a log."""


def _pick_on_demand(items: list[dict[str, Any]], windows: bool) -> float | None:
    """Which one of the catalogue's several rows for a SKU is the plain on-demand rate.

    Verified against a real response for Standard_D2s_v3/eastus2: the catalogue lists the same
    SKU up to four times -- on-demand, Spot and Low Priority, each again for Windows -- and only
    `productName` carries the word "Windows"; `meterName` is identical between the Windows and
    Linux rows, so filtering on it alone cannot tell the two apart.
    """
    on_demand = [
        it for it in items
        if "spot" not in str(it.get("meterName", "")).lower()
        and "low priority" not in str(it.get("meterName", "")).lower()
    ]
    for it in on_demand:
        is_windows = "windows" in str(it.get("productName", "")).lower()
        if is_windows == windows:
            price = it.get("retailPrice")
            if price:
                return float(price)
    return None


async def _retail_hourly(region: str, sku: str, windows: bool) -> float | None:
    """The plain on-demand hourly rate for one VM size in one region, or None if unpriced."""
    params = {
        "currencyCode": "USD",
        "$filter": (f"armRegionName eq '{region}' and serviceName eq 'Virtual Machines' "
                   f"and armSkuName eq '{sku}' and priceType eq 'Consumption'"),
    }
    try:
        async with httpx.AsyncClient(timeout=20) as client:
            r = await client.get(PRICES_URL, params=params)
            r.raise_for_status()
            items = r.json().get("Items", [])
    except Exception as exc:  # noqa: BLE001 - an unpriced SKU must degrade to "no estimate"
        log.warning("could not read retail price for %s in %s: %s", sku, region, str(exc)[:150])
        return None
    return _pick_on_demand(items, windows)


async def scenario(*, resource_id: str, target_sku: str | None,
                   subscription_ids: list[str] | None, days: int = 30) -> dict[str, Any]:
    """Compare one VM's current billed cost against resizing it, or shutting it down.

    `target_sku` is an ARM VM size, e.g. `Standard_D2s_v3`. Leave it unset (or pass `"shutdown"`)
    to model shutting the VM down instead, which needs no retail lookup at all -- compute at $0
    is not an estimate.
    """
    from . import currency as fx
    from .cost import list_subscriptions
    from .waste import _arg, _costs_for

    if "/subscriptions/" not in resource_id:
        raise WhatIfError("Not a recognisable Azure resource id.")
    sub_id = resource_id.split("/subscriptions/")[1].split("/")[0]

    subs = subscription_ids or [s["id"] for s in (await list_subscriptions())["subscriptions"]]
    if sub_id not in subs:
        raise WhatIfError("This resource is not in a subscription you can access.")

    escaped = resource_id.replace("'", "''")
    rows = await _arg(
        f"""
        Resources
        | where id =~ '{escaped}' and type =~ 'microsoft.compute/virtualmachines'
        | project id, name, resourceGroup, location,
                  size = tostring(properties.hardwareProfile.vmSize),
                  osType = tostring(properties.storageProfile.osDisk.osType)
        """,
        [sub_id],
    )
    if not rows:
        raise WhatIfError("This resource was not found, or is not a virtual machine.")
    vm = rows[0]
    windows = str(vm.get("osType") or "").lower() == "windows"
    current_size = vm.get("size")

    days = max(1, min(int(days), 365))
    billed = (await asyncio.to_thread(_costs_for, [resource_id], days)).get(resource_id.lower())
    currency = str((billed or {}).get("currency") or ("unknown" if billed else "USD")).upper()
    billed_cost = (billed or {}).get("cost")
    billed_daily = round(billed_cost / days, 4) if billed_cost is not None else None

    shutdown = not target_sku or target_sku.strip().lower() == "shutdown"
    current_retail_rate, target_rate = await asyncio.gather(
        _retail_hourly(vm["location"], current_size, windows) if current_size else _none(),
        _none() if shutdown else _retail_hourly(vm["location"], target_sku, windows),
    )

    if not shutdown and target_rate is None:
        raise WhatIfError(
            f"No retail price found for {target_sku!r} in {vm['location']}. Check it is a "
            "valid Azure VM size name, e.g. Standard_D2s_v3."
        )
    retail_factor = fx.rate_to(currency)
    current_retail_daily = (
        round(current_retail_rate * 24 * retail_factor, 4)
        if current_retail_rate is not None and retail_factor is not None else None
    )
    target_daily = 0.0 if shutdown else (
        round(target_rate * 24 * retail_factor, 4)
        if target_rate is not None and retail_factor is not None else None
    )
    pricing_basis = (
        f"Azure public retail prices are USD; converted to {currency} at "
        f"{retail_factor:g} {currency}/USD. Billed amounts remain in {currency}."
        if retail_factor is not None else
        f"No USD/{currency} exchange rate is available; retail comparison and resize savings "
        "are unavailable. Billed amounts remain in their native currency."
    )

    def annualise(daily: float | None) -> float | None:
        return round(daily * 365, 2) if daily is not None else None

    return {
        "resource": vm["name"],
        "resource_group": vm["resourceGroup"],
        "region": vm["location"],
        "current_size": current_size,
        "target_size": "shutdown" if shutdown else target_sku,
        "os": "Windows" if windows else "Linux",
        "currency": currency,
        "days": days,
        "pricing_basis": pricing_basis,
        "billed": {"daily": billed_daily, "annual": annualise(billed_daily)},
        "current_retail": {"daily": current_retail_daily, "annual": annualise(current_retail_daily)},
        "target": {"daily": target_daily, "annual": annualise(target_daily)},
        # The number worth acting on: today's real bill against the plain rate for the new
        # size. Not `current_retail` against `target` -- that would compare two rates neither
        # of which is what is actually being paid right now.
        "annual_saving": (
            round((annualise(billed_daily) or 0) - (annualise(target_daily) or 0), 2)
            if billed_daily is not None and target_daily is not None else None
        ),
        "note": (
            f"'Billed' is the real cost over the last {days} days. 'Target' is Azure's public "
            "retail rate for compute only -- it excludes disks, networking, and any Hybrid "
            "Benefit or reservation discount the current VM may already carry, which is what "
            "'Current, at retail' is for: the same current size, priced the same plain way as "
            "the target, so a discount already in place does not make a resize look better "
            "than it is." if not shutdown else
            f"'Billed' is the real cost over the last {days} days. Shutting a VM down stops "
            "compute billing entirely; its disks keep billing until they are deleted too."
        ),
    }


async def _none() -> None:
    return None
