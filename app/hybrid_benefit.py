"""Azure Hybrid Benefit for Windows Server: which VMs are not using it, and what that costs.

The warehouse has no `OSType` or licensing column -- cost data never carries a VM's licensing
state, only what it billed -- so this reads the one place that answer actually lives: each VM's
own `licenseType` ARM property. That is not a guess. `licenseType == 'Windows_Server'` is exactly
the flag `az vm update --license-type` sets, so which machines already have the benefit turned on
is exact, not inferred from meter names the way an OS often has to be elsewhere in this app.

What *is* an estimate is the saving. Azure charges the Windows compute premium as part of the
same meter as the base VM rate, not as a separate line item, so there is no billed figure to read
off for "the licence part of what this VM costs". The estimate models it as the gap between the
public Windows and Linux on-demand rates for the same size -- the same shape of estimate Azure's
own Advisor uses for this recommendation, and the reason `note` says so plainly rather than
presenting it as read off the bill.

Scoped to Windows *Server* specifically, using the same publisher/offer check `esu.py` already
established for the same distinction -- Windows client (10/11) desktops are a different licensing
programme with different eligibility, and lumping them in here would overstate a saving that
needs a different licence to claim.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any

log = logging.getLogger("cloudlens.hybrid_benefit")


async def coverage(subscription_ids: list[str] | None = None, days: int = 30) -> dict[str, Any]:
    """Windows Server VMs, split by whether Hybrid Benefit is already on, with the gap priced."""
    from .cost import list_subscriptions
    from .esu import _is_windows_server
    from .waste import _arg, _costs_for
    from .whatif import _retail_hourly

    subs = subscription_ids or [s["id"] for s in (await list_subscriptions())["subscriptions"]]
    if not subs:
        return {"vms": [], "count": 0, "covered": 0, "uncovered": 0,
                "estimated_annual_savings": 0.0, "currency": "USD",
                "note": "No subscriptions in scope."}

    rows = await _arg(
        """
        Resources
        | where type =~ 'microsoft.compute/virtualmachines'
        | where tostring(properties.storageProfile.osDisk.osType) =~ 'windows'
        | extend img = properties.storageProfile.imageReference
        | project name, id, resourceGroup, location,
                  licenseType = tostring(properties.licenseType),
                  publisher = tostring(img.publisher), offer = tostring(img.offer),
                  size = tostring(properties.hardwareProfile.vmSize)
        """,
        subs, top=500,
    )
    servers = [v for v in rows if _is_windows_server(v.get("publisher", ""), v.get("offer", ""))]
    if not servers:
        return {"vms": [], "count": 0, "covered": 0, "uncovered": 0,
                "estimated_annual_savings": 0.0, "currency": "USD",
                "note": "No Windows Server virtual machines found in this scope -- "
                        f"checked {len(rows)} Windows machine(s) in total."}

    days = max(1, min(int(days), 365))
    ids = [v["id"] for v in servers]
    costs = await asyncio.to_thread(_costs_for, ids, days)

    async def _rates(location: str, size: str) -> tuple[float | None, float | None]:
        if not location or not size:
            return None, None
        return await asyncio.gather(
            _retail_hourly(location, size, True), _retail_hourly(location, size, False),
        )

    rate_pairs = await asyncio.gather(*(_rates(v.get("location"), v.get("size")) for v in servers))

    vms: list[dict[str, Any]] = []
    covered = uncovered = 0
    total_saving = 0.0
    currency = "USD"
    for v, (windows_rate, linux_rate) in zip(servers, rate_pairs):
        has_benefit = v.get("licenseType") == "Windows_Server"
        billed = costs.get(v["id"].lower()) or {}
        if billed.get("currency"):
            currency = billed["currency"]

        saving = None
        if not has_benefit and windows_rate and linux_rate and windows_rate > linux_rate:
            saving = round((windows_rate - linux_rate) * 24 * 365, 2)

        if has_benefit:
            covered += 1
        else:
            uncovered += 1
            total_saving += saving or 0.0

        vms.append({
            "name": v.get("name"), "id": v.get("id"), "resource_group": v.get("resourceGroup"),
            "location": v.get("location"), "size": v.get("size"),
            "hybrid_benefit": has_benefit,
            "cost": round(billed["cost"], 2) if billed.get("cost") is not None else None,
            "estimated_annual_savings": saving,
        })

    # Not yet on the benefit and priced first -- the ones worth acting on lead the table, rather
    # than being buried under machines that already have it turned on or that came back unpriced.
    vms.sort(key=lambda r: (r["hybrid_benefit"], r["estimated_annual_savings"] is None,
                            -(r["estimated_annual_savings"] or 0)))

    return {
        "vms": vms,
        "count": len(vms),
        "covered": covered,
        "uncovered": uncovered,
        "estimated_annual_savings": round(total_saving, 2) if total_saving else 0.0,
        "currency": currency,
        "days": days,
        "note": (
            "Whether a VM already has Hybrid Benefit comes from its own licenseType setting, "
            "which is exact. The saving is an estimate: the gap between Azure's public Windows "
            "and Linux on-demand rates for that size, over a year -- it does not know your "
            "Software Assurance or subscription licence position, only that the benefit needs "
            "one to claim."
        ),
    }
