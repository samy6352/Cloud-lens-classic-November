"""Current/future Billing opportunities, using the existing deduplicated savings scan.

No purchase, resize, schedule write, or permission grant is performed. Monthly estimates
use a 30-day month, except provider annual estimates which retain annual / 12.
"""
from __future__ import annotations

import asyncio
import hashlib
import re
from datetime import UTC, datetime
from typing import Any

from . import billing_commitments, rates, savings
from . import currency as fx
from .billing_commitments import normalize_scopes, number

MONTH_DAYS = 30
SCAN_TIMEOUT = 130
OVERLAP_NOTE = (
    "Only compatible actions are included. RI, savings-plan and term alternatives are not "
    "added together. Subscription-wide Advisor estimates are excluded where operational "
    "changes could affect that subscription; no discount is layered on removal or resizing. "
    "Excluded/alternative amounts remain visible per opportunity and are not additive."
)


def _money(value: float | None) -> float | None:
    return round(value, 2) if value is not None else None


def _positive(value: Any) -> float | None:
    value = number(value)
    # savings.build historically coerces absent prices to zero. A zero-price candidate
    # therefore cannot establish either an actual zero saving or an actionable estimate.
    return value if value is not None and value > 0 else None


def _identifier(value: Any) -> str:
    return str(value or "").lower().rstrip("/")


def _sub(value: Any) -> str:
    match = re.search(r"/subscriptions/([^/]+)", str(value or ""), re.I)
    return match.group(1).lower() if match else ""


def _footprint(raw: dict) -> tuple[set[str], set[str], bool]:
    resources = set()
    subscriptions = set()
    rid = _identifier(raw.get("resource_id"))
    if "/providers/" in rid and "/subscriptions/" in rid:
        resources.add(rid)
    if raw.get("subscription"):
        subscriptions.add(_sub(raw["subscription"]) or _identifier(raw["subscription"]))
    for item in raw.get("items") or []:
        child = _identifier(item.get("id"))
        if "/subscriptions/" in child:
            resources.add(child)
    subscriptions.update(filter(None, (_sub(r) for r in resources)))
    return resources, subscriptions, bool(raw.get("more"))


def _overlaps(left: dict, right: dict) -> bool:
    lr, ls, truncated_l = _footprint(left)
    rr, rs, truncated_r = _footprint(right)
    if ls and rs and not ls.intersection(rs) and not (truncated_l or truncated_r):
        return False
    if lr and rr and not truncated_l and not truncated_r:
        return any(a == b or a.startswith(b + "/") or b.startswith(a + "/")
                   for a in lr for b in rr)
    return True


def _factor(original: str | None, target: str | None) -> float | None:
    if not original or not target or original.upper() in {"UNKNOWN", "BILLED"}:
        return None
    if original.upper() == target.upper():
        return 1
    source_rate = number(fx.rate_to(original.upper()))
    target_rate = number(fx.rate_to(target.upper()))
    if source_rate is None or target_rate is None or source_rate <= 0 or target_rate <= 0:
        return None
    return number(target_rate / source_rate)


def _action(raw: dict, commitment: bool) -> str:
    if commitment:
        title = raw.get("title") or ""
        kind = rates._kind_of(title)
        advisor = any(s.get("source") in {"advisor", "rates"} for s in raw.get("sources") or [])
        purchase = bool(re.search(r"\b(buy|purchas\w*|consider|save|saving|reduce|commit\w*)\b",
                                  title, re.I))
        review = bool(re.search(r"\b(expir\w*|underutil\w*|unused|exchange|refund|cancel)\b",
                                title, re.I))
        if advisor and purchase and not review and kind == "reservation":
            return "Buy Reservation"
        if advisor and purchase and not review and kind == "savings plan":
            return "Increase Savings Plan coverage"
        return "Review commitment options"
    return {
        "orphaned": "Remove orphaned resource", "schedule": "Schedule shutdown",
        "rightsize": "Right-size resources", "idle": "Right-size resources",
    }.get(raw.get("category"), "Review Azure Advisor recommendation")


def _amounts(raw: dict, days: int, commitment: bool) -> tuple[float | None, float | None, str, int | None]:
    sources = raw.get("sources") or []
    billed = [s for s in sources if s.get("basis") == "billed"]
    if billed and not commitment:
        candidates = []
        for source in billed:
            # The shutdown scanner prices 30 days of billing even though its CPU
            # lookback is 7–14 days; rightsizing caps its billing window at 90 days.
            window = 30 if source.get("source") == "shutdown" else (
                min(days, 90) if source.get("source") == "rightsizing" else days)
            amount = _positive(source.get("window"))
            if amount is not None:
                candidates.append((amount * MONTH_DAYS / window, window))
        if not candidates:
            return None, None, "Billed-window estimate unavailable", days
        monthly, window = max(candidates)
        return monthly, monthly * 12, (
            f"Extrapolated billed-window opportunity: {window} days × 30/{window}; "
            "annual = monthly × 12. Not a guaranteed saving or a replacement-SKU quote."
        ), window
    annual = _positive(raw.get("annual"))
    if annual is not None:
        return annual / 12, annual, "Azure Advisor annual estimate; monthly = annual / 12", None
    return None, None, "Provider saving estimate unavailable", None


def _opportunity(raw: dict, days: int, target: str | None, commitment: bool) -> dict:
    monthly, annual, basis, window = _amounts(raw, days, commitment)
    original = str(raw.get("currency") or "").upper() or None
    factor = _factor(original, target)
    provider = _positive(raw.get("annual")) if not any(
        s.get("basis") == "billed" for s in raw.get("sources") or []) else None
    ident = str(raw.get("key") or hashlib.sha256(
        repr((raw.get("title"), raw.get("resource_id"), raw.get("subscription"))).encode()
    ).hexdigest()[:16])
    resources, subs, _ = _footprint(raw)
    result = {
        "id": ident, "title": raw.get("title") or "Cost opportunity",
        "action": _action(raw, commitment),
        "monthly_savings": _money(monthly * factor) if monthly is not None and factor else None,
        "annual_savings": _money(annual * factor) if annual is not None and factor else None,
        "currency": target, "resource_id": raw.get("resource_id") or None,
        "resource_name": raw.get("resource") or None, "region": raw.get("region") or None,
        "resource_group": raw.get("resource_group") or None,
        "subscription_id": next(iter(subs)) if len(subs) == 1 else None,
        "basis": basis, "confidence": raw.get("confidence") or "low",
        "reason": raw.get("why") or raw.get("action") or "",
        "related_tab": "commitments" if commitment else "savings",
        "overlap_group": None, "included_in_total": False, "exclusion_reason": None,
        "category": "commitment" if commitment else raw.get("category"),
        "sources": raw.get("sources") or [],
        "source_currency": original, "source_monthly_savings": _money(monthly),
        "source_annual_savings": _money(annual),
        "conversion_rate": factor,
        "provider_annual_savings": _money(provider * factor) if provider is not None and factor else None,
        "window_days": window, "normalization_month_days": MONTH_DAYS,
        "extrapolated": window is not None,
        "resource_ids": sorted(resources),
        "options": [],
        "provider_annual_estimates": [
            {"source": source.get("source"), "annual": _positive(source.get("annual")),
             "currency": original if provider is not None else None,
             "basis": "Source annual estimate, not additive. Merged billed rows do not retain "
                      "the provider currency; those raw estimates are not converted or totaled."}
            for source in raw.get("sources") or []
            if source.get("basis") != "billed" and _positive(source.get("annual")) is not None
        ],
    }
    for option in raw.get("options") or []:
        estimate = _positive(option.get("annual"))
        result["options"].append({
            "label": option.get("label"), "detail": option.get("detail"),
            "annual_savings": _money(estimate * factor) if estimate is not None and factor else None,
            "monthly_savings": _money(estimate * factor / 12) if estimate is not None and factor else None,
            "currency": target, "included_in_total": False,
            "basis": "Alternative provider annual estimate; not additive",
        })
    if factor is None:
        result["exclusion_reason"] = "Currency conversion unavailable; source amounts retained separately."
    elif monthly is None:
        result["exclusion_reason"] = "A priced savings estimate is unavailable; zero is not assumed."
    return result


def _lifecycle_opportunities(inventory: dict, target: str | None) -> list[dict]:
    rows = []
    for item in inventory.get("items") or []:
        active = str(item.get("state")).lower() not in {
            "expired", "cancelled", "failed", "billingfailed", "split", "merged",
        }
        for condition, action in (
            (item.get("expiring") is True and active, "Review expiring commitment"),
            (item.get("underutilized") is True and active, "Review underutilized commitment"),
        ):
            if not condition:
                continue
            rows.append({
                "id": f"lifecycle:{item['id']}:{action}",
                "title": f"{action}: {item.get('name') or item['id']}",
                "action": action, "monthly_savings": None, "annual_savings": None,
                "currency": target, "resource_id": item["id"], "resource_group": None,
                "subscription_id": None, "basis": "Provider commitment lifecycle; not monetized",
                "confidence": "high", "reason": (
                    f"Expires in {item['days_to_expiry']} calendar days."
                    if action == "Review expiring commitment" else
                    f"Provider utilisation is {item['utilization_pct']}%, below 80%."),
                "related_tab": "commitments", "overlap_group": f"benefit:{item['id']}",
                "included_in_total": False,
                "exclusion_reason": "Review signal, not a priced recoverable saving.",
                "category": "commitment_lifecycle", "sources": item.get("sources") or [],
                "portal_url": item.get("portal_url"),
            })
    return rows


async def build(scopes: list[str], currency: str | None = None, *, days: int = 30) -> dict[str, Any]:
    """Called inside main.as_user, with an explicit, already-authorized subscription set."""
    if isinstance(days, bool) or not isinstance(days, int) or not 1 <= days <= 365:
        raise ValueError("days must be between 1 and 365")
    scopes = normalize_scopes(scopes)
    checked = datetime.now(UTC).isoformat()
    warnings = []
    if not scopes:
        return {
            "status": "error", "opportunities": [],
            "potential": {"monthly": None, "annual": None, "currency": currency,
                          "basis": "No subscriptions selected", "overlap_note": OVERLAP_NOTE,
                          "partial": True, "included_count": 0, "excluded_count": 0, "excluded": []},
            "commitments": {"items": [], "unavailable": [
                {"source": "scope", "reason": "No explicit subscriptions selected."}],
                            "coverage": "none", "partial": True},
            "freshness": {"checked_at": checked, "window_days": days},
            "warnings": ["No explicit subscriptions selected; no live or warehouse reads were made."],
        }

    async def scan():
        # Retain each opportunity's native currency: the existing scan's global conversion
        # can re-label unlike native currencies using one estate-level exchange rate.
        return await asyncio.wait_for(savings.build(scopes, days, currency=None), SCAN_TIMEOUT)

    scan_result, inventory = await asyncio.gather(
        scan(), billing_commitments.build(scopes, currency, days=days), return_exceptions=True)
    scan_failed = isinstance(scan_result, Exception) or not isinstance(scan_result, dict)
    if scan_failed:
        warnings.append("Savings sources could not be read; potential savings are unavailable.")
        scan_result = {"unavailable": {"savings": "Unavailable"}, "opportunities": [], "commitments": []}
    unavailable = scan_result.get("unavailable") or {}
    warnings.extend(f"{source}: savings source unavailable; estimates may be incomplete."
                    for source in unavailable)
    if isinstance(inventory, Exception) or not isinstance(inventory, dict):
        inventory = {"items": [], "unavailable": [{"source": "commitments",
                     "reason": "Commitment inventory could not be read."}], "partial": True}
    if inventory.get("partial"):
        warnings.append("Commitment lifecycle coverage is partial. Review the Reservation & Savings Plan watch below.")
    target = (currency or "").upper()
    if not target or target == "BILLED":
        target = str(scan_result.get("currency") or "").upper() or None
    operational = [(raw, _opportunity(raw, days, target, False))
                   for raw in scan_result.get("opportunities") or []]
    native_currencies = inventory.get("native_billing_currencies")
    for raw, row in operational:
        if any(s.get("source") == "waste" for s in raw.get("sources") or []):
            native = (native_currencies[0] if isinstance(native_currencies, list)
                      and len(native_currencies) == 1 else None)
            if not native or native == "UNKNOWN" or native != row["source_currency"]:
                row.update(monthly_savings=None, annual_savings=None,
                           source_monthly_savings=None, source_annual_savings=None,
                           source_currency=None, conversion_rate=None,
                           exclusion_reason="Grouped billed amounts lack unambiguous native-currency "
                                            "provenance; the group is not totaled.")
    purchases = [(raw, _opportunity(raw, days, target, True))
                 for raw in scan_result.get("commitments") or []]
    unknown = False
    for _, row in operational:
        row["overlap_group"] = f"operational:{row['id']}"
        row["included_in_total"] = row["monthly_savings"] is not None
        if not row["included_in_total"]:
            unknown = True

    selected: list[tuple[dict, dict]] = []
    for raw, row in sorted(purchases, key=lambda x: -(x[1]["monthly_savings"] or 0)):
        if row["monthly_savings"] is None:
            unknown = True
        blockers = [o for o, _ in operational if _overlaps(raw, o)]
        alternatives = [(r, o) for r, o in selected if _overlaps(raw, r)]
        if blockers:
            row["exclusion_reason"] = (
                "Potential overlap with operational changes; re-evaluate Advisor after removal/resizing.")
            row["overlap_group"] = "operational-overlap"
        elif alternatives:
            row["exclusion_reason"] = "Alternative RI / savings-plan / term estimate, not additive."
            row["overlap_group"] = alternatives[0][1]["overlap_group"]
        elif row["action"] == "Review commitment options":
            row["exclusion_reason"] = "No Azure Advisor purchase recommendation supports this action."
        elif row["monthly_savings"] is not None:
            row.update(included_in_total=True, overlap_group=f"commitment:{row['id']}")
            selected.append((raw, row))
    rows = [o for _, o in operational + purchases]
    monetary = [r for r in rows if r["included_in_total"]]
    # An empty successful scan is zero *identified* opportunity. Failure or missing prices
    # without any priced result is unavailable, never a misleading zero.
    source_failed = scan_failed or bool(unavailable) or (scan_result.get("scan") or {}).get("status") == "error"
    monthly = sum(r["monthly_savings"] for r in monetary) if monetary or not (source_failed or unknown) else None
    annual = sum(r["annual_savings"] for r in monetary) if monthly is not None else None
    if unknown:
        warnings.append("Some opportunities have unavailable prices/currency; potential is only the priced subset.")
    rows.extend(_lifecycle_opportunities(inventory, target))
    partial = bool(source_failed or unknown or inventory.get("partial"))
    return {
        "status": "error" if (scan_failed or (scan_result.get("scan") or {}).get("status") == "error")
        and not inventory.get("items") else "partial" if partial else "ok",
        "opportunities": rows,
        "potential": {
            "monthly": _money(monthly), "annual": _money(annual), "currency": target,
            "basis": "Compatible estimated opportunities; 30-day month, annual = monthly × 12 "
                     "or direct provider annual estimate. Not realized savings.",
            "overlap_note": OVERLAP_NOTE, "partial": partial,
            "included_count": len(monetary),
            "excluded_count": sum(not r["included_in_total"] for r in rows),
            "excluded": [{"id": r["id"], "monthly": r["monthly_savings"], "annual": r["annual_savings"],
                          "reason": r["exclusion_reason"]} for r in rows if not r["included_in_total"]],
        },
        "commitments": inventory,
        "freshness": {"checked_at": checked, "window_days": days,
                      "normalization_month_days": MONTH_DAYS,
                      "warehouse_as_of": inventory.get("as_of"),
                      "metadata_checked_at": inventory.get("metadata_checked_at"),
                      "sources_read": scan_result.get("sources_read") or [],
                      "source_note": "Existing Azure recommendation/resource caches may be used; "
                                     "checked_at is response assembly time, not billing or metrics time."},
        "warnings": warnings,
    }
