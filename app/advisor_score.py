"""Read official Advisor percentages, without deriving scores from recommendations."""
from __future__ import annotations

import asyncio
import math
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from .cost import ARM, CostError, azure

CATEGORIES = {
    "Cost": "Cost",
    "Security": "Security",
    "HighAvailability": "Reliability",
    "OperationalExcellence": "Operational Excellence",
    "Performance": "Performance",
}


def parse_scores(payload: dict[str, Any]) -> dict[str, dict[str, Any]]:
    scores = {name: {"score": None, "date": None, "status": "unavailable"}
              for name in (*CATEGORIES, "Advisor")}
    for row in payload.get("value", []):
        name = row.get("name")
        if name not in scores:
            continue  # Recommendation-type GUIDs are not pillar scores.
        latest = (row.get("properties") or {}).get("lastRefreshedScore") or {}
        value = latest.get("score")
        date = latest.get("date")
        valid = (isinstance(value, (int, float)) and not isinstance(value, bool)
                 and math.isfinite(value) and 0 <= value <= 100)
        try:
            parsed_date = datetime.fromisoformat(date.replace("Z", "+00:00"))
            valid = valid and parsed_date.tzinfo is not None
        except (ValueError, TypeError, AttributeError):
            valid = False
        scores[name] = {"score": value if valid else None,
                        "date": date if valid else None,
                        "status": "available" if valid else "invalid"}
    return scores


async def read_scores(subscription_ids: list[str]) -> dict[str, Any]:
    semaphore = asyncio.Semaphore(6)

    async def read(subscription: str) -> dict[str, Any]:
        # Scope identifiers are also used in an ARM path; reject path/query injection.
        subscription = str(UUID(subscription))
        result: dict[str, Any] = {"subscription_id": subscription,
                                  "scores": parse_scores({}), "status": "unavailable"}
        try:
            async with semaphore:
                # Deliberately uncached: no stale score is labelled as a fresh Azure read.
                payload = await azure.request(
                    "GET", f"{ARM}/subscriptions/{subscription}/providers/Microsoft.Advisor/advisorScore",
                    params={"api-version": "2023-01-01"}, cache=False, attempts=2)
            result["scores"] = parse_scores(payload)
            available = sum(result["scores"][name]["status"] == "available" for name in CATEGORIES)
            result["status"] = "available" if available == 5 else "partial" if available else "empty"
        except CostError as exc:
            status = exc.status_code
            result["status"] = ("forbidden" if status in (401, 403)
                                else "unsupported" if status in (400, 404) else "error")
            result["message"] = (
                "Advisor score read denied. Reader (Microsoft.Advisor/advisorScore/read) "
                "is required for the active Azure identity."
                if status in (401, 403) else
                "Advisor scores are not supported or not available for this subscription."
                if status in (400, 404) else "Azure Advisor score request failed; retry later.")
        except Exception:
            result["status"] = "error"
            result["message"] = "Azure Advisor score request failed; retry later."
        return result

    rows = await asyncio.gather(*(read(s) for s in dict.fromkeys(subscription_ids)))
    return {
        "categories": [{"id": name, "label": label} for name, label in CATEGORIES.items()],
        "subscriptions": rows,
        "fetched_at": datetime.now(UTC).isoformat(),
        "aggregation": "none",
        "source": "Microsoft.Advisor/advisorScore (2023-01-01)",
        "note": "Official scores per subscription, out of 100. No cross-subscription average. "
                "Dates are Azure's last refreshed score dates; billing dates, tags, service and "
                "currency filters do not change Advisor scores.",
    }
