"""The dashboard behind the tabs: the same warehouse, arranged rather than asked.

The chat is good at questions nobody anticipated. It is a poor way to answer the four things
everyone wants the moment they open a cost tool — what am I spending, on what, where is it
going up, and what is idle. Those belong on a page you can read without typing.

Every figure here comes from the warehouse, so a tab renders in milliseconds and the
model is not involved at all. The one exception is stale resources, which needs live inventory
from Azure because "this disk is attached to nothing" is not a fact the cost export knows.

Grouping follows Azure's own `ServiceFamily`, with one rule that matters for a cost tool:
**every currency unit lands in exactly one tab.** People add tabs up and expect the total, so
anything not claimed by a named group falls into "Other" rather than quietly disappearing.
"""
from __future__ import annotations

import logging
from typing import Any

from . import reporting

log = logging.getLogger("cloudlens.dashboard")

# Curated groups. Ordered: this is the tab order, and it runs roughly from the infrastructure
# people recognise to the things they go looking for less often.
GROUPS: list[dict[str, Any]] = [
    {"id": "compute", "label": "Compute & Web",
     "families": ["Compute", "Web", "Containers"]},
    {"id": "networking", "label": "Networking",
     "families": ["Networking", "Bandwidth"]},
    {"id": "storage", "label": "Storage",
     "families": ["Storage"]},
    {"id": "data", "label": "AI & Data",
     "families": ["AI + Machine Learning", "Analytics", "Data", "Databases",
                  "Microsoft Fabric"]},
    {"id": "integration", "label": "Integration & IoT",
     "families": ["Integration", "Internet of Things", "Azure Communication Services",
                  "Mixed Reality", "Media"]},
    {"id": "security", "label": "Security & Ops",
     "families": ["Security", "Management and Governance", "Developer Tools", "Azure Arc",
                  "Migration", "Monitor"]},
]

CLAIMED = {f for g in GROUPS for f in g["families"]}
# Named "Other services" rather than "Other": in a rail where every neighbour names a service
# family, a bare "Other" reads as a leftovers bin rather than as the spend it actually holds.
OTHER = {"id": "other", "label": "Other services", "families": []}

# How far back "month-over-month" looks. Matches the AI agent's own cost_trend default so the
# chat and the dashboard never show a different amount of history for what is, to a reader, the
# same question -- and it is a cap, not a target: a warehouse holding less history (a fresh
# install, a dev box) simply returns however many real months it has rather than padding the
# rest with bars that never happened.
MONTHLY_TREND_MONTHS = 6


def _q(value: str) -> str:
    """Single-quote a literal for SQL. Family names are ours, not user input, but a stray
    apostrophe in a future Azure service name should not become a syntax error."""
    return "'" + str(value).replace("'", "''") + "'"


def _families_clause(families: list[str], negate: bool = False) -> str:
    if not families:
        return "1=1"
    joined = ", ".join(_q(f) for f in families)
    if negate:
        return f'("ServiceFamily" IS NULL OR "ServiceFamily" NOT IN ({joined}))'
    return f'"ServiceFamily" IN ({joined})'


# Marketplace/ISV spend is not a `ServiceFamily` at all -- a third-party firewall appliance
# bills under "Networking" the same as Azure's own, a SaaS data tool under "Analytics" the same
# as Fabric. FOCUS's own signal for "who actually gets this money" is `PublisherName`, which
# Azure leaves null (or "Microsoft") for its own services and sets to the ISV's name for
# Marketplace and third-party offers. Heuristic, not a guarantee -- Azure does not promise every
# first-party row is null rather than explicitly "Microsoft" -- so it is excluded either way and
# the tab says plainly that this is what it is reading.
MARKETPLACE_CLAUSE = (
    '"PublisherName" IS NOT NULL AND "PublisherName" <> \'\' '
    "AND lower(\"PublisherName\") NOT IN ('microsoft', 'microsoft corporation')"
)


def _group_for_family(family: str) -> tuple[str, str]:
    """Which cost-area group (id, label) a `ServiceFamily` belongs to -- the same split
    "Where the money goes" and the rail itself use, so a reader who already knows what
    "AI & Data" means there sees the identical grouping here, just over time."""
    for group in GROUPS:
        if family in group["families"]:
            return group["id"], group["label"]
    return OTHER["id"], OTHER["label"]


class Dashboard:
    """Reads the warehouse. Holds no state: a tab is a set of queries, not a session."""

    def __init__(self) -> None:
        from .warehouse import warehouse

        self.wh = warehouse

    # ------------------------------------------------------------------ helpers
    def _rows(self, sql: str, scope: list[str] | None, limit: int = 200,
              currency: str | None = None,
              filters: dict[str, list[str]] | None = None) -> list[dict]:
        """Single query on its own connection. Prefer `reader` when asking several questions."""
        with self.wh.reader(scope, currency, filters) as r:
            return r.rows(sql, limit)

    def _window(self, reader: Any, days: int) -> tuple[str, str, str]:
        """The period to report on, anchored to the newest data rather than to today.

        Cost data lands a day or two late; anchoring to `today` would show a partial or empty
        final day and make every trend look like it fell off a cliff.
        """
        return reporting.window(reader, days)

    @staticmethod
    def _reduce(rows: list[dict], key: str = "cost") -> tuple[float | None, str | None, bool]:
        """Collapse per-currency rows into one number, or refuse to if the estate is mixed.

        Adding EUR to USD produces a confident, meaningless figure — the one number a cost
        dashboard must never show.
        """
        by_currency: dict[str, float] = {}
        for r in rows:
            cur = r.get("currency") or "unknown"
            by_currency[cur] = by_currency.get(cur, 0.0) + float(r.get(key) or 0.0)
        if not by_currency:
            return 0.0, None, False
        if len(by_currency) == 1:
            cur, total = next(iter(by_currency.items()))
            return round(total, 2), cur, False
        return None, None, True

    @staticmethod
    def _money_totals(rows: list[dict]) -> list[dict]:
        totals: dict[str, float] = {}
        for row in rows:
            currency = row.get("currency") or "unknown"
            totals[currency] = totals.get(currency, 0.0) + float(row.get("cost") or 0.0)
        return [{"currency": currency, "cost": round(cost, 2)}
                for currency, cost in sorted(totals.items())]

    @staticmethod
    def _ranked(reader: Any, query: str, limit: int, *, movers: bool = False) -> list[dict]:
        """Bound presentation rows only after ranking within each native currency."""
        movement = (
            ", row_number() OVER (PARTITION BY currency ORDER BY abs(cost - prior) DESC, name) "
            "AS movement_rank" if movers else "")
        return reader.rows(
            f"WITH grouped AS ({query}), ranked AS (SELECT grouped.*, "
            "row_number() OVER (PARTITION BY currency ORDER BY cost DESC, name) AS spend_rank"
            f"{movement} FROM grouped) SELECT * FROM ranked WHERE spend_rank <= {limit}"
            + (" OR movement_rank <= 8" if movers else "")
            + " ORDER BY currency, cost DESC, name", None)

    @staticmethod
    def _currency_views(build: Any, data: dict[str, Any]) -> dict[str, Any]:
        currencies = sorted({row.get("currency") or "unknown"
                             for rows in data.values() if isinstance(rows, list)
                             for row in rows})
        if len(currencies) <= 1:
            result = build(**data, currency=currencies[0] if currencies else None)
            result["by_currency"] = []
            return result
        views = [
            build(**{
                key: [row for row in value if (row.get("currency") or "unknown") == currency]
                if isinstance(value, list) else value
                for key, value in data.items()
            }, currency=currency)
            for currency in currencies
        ]
        # Keep the familiar envelope, but never offer an aggregate number that a legacy
        # consumer could accidentally format as one currency. Each child is a complete view.
        result = {**views[0], "currency": None, "mixed_currency": True, "by_currency": views,
                  "currency_policy": "native-separated",
                  "notice": "Currencies are reported separately; no combined monetary total is available."}
        result["kpis"] = dict.fromkeys(result["kpis"])
        result["trend"] = {key: [] if isinstance(value, list) else False
                           for key, value in result["trend"].items()}
        for key, value in result.items():
            if isinstance(value, list) and key != "by_currency":
                result[key] = []
        if "monthly" in result:
            result["monthly"] = None
        return result

    # -------------------------------------------------------------------- tabs
    def sections(self, scope: list[str] | None, days: int = 30,
                 currency: str | None = None,
                 filters: dict[str, list[str]] | None = None) -> dict[str, Any]:
        """The tab bar: which groups exist in this data, and what each is costing."""
        days = reporting.period_days(days)
        with self.wh.reader(scope, currency, filters) as r:
            hi, start, _ = self._window(r, days)
            if not hi:
                return {"sections": [], "days": days, "empty": True}

            rows = r.rows(
                'SELECT "ServiceFamily" AS family, "BillingCurrency" AS currency, '
                'sum("BilledCost") AS cost FROM costs '
                f'WHERE "ChargePeriodStart" > {start} GROUP BY 1, 2', None)

            # AKS, on the same connection, because the picker offers it as a segment beside the
            # service families and a segment with no figure beside seven that have one reads as
            # broken. Warehouse only: the managed node resource group and anything charged
            # against the Kubernetes service. Naming the *cluster* needs Resource Graph, which
            # is slow and can be refused -- but the total does not, so the strip stays fast.
            aks_rows = r.rows(
                'SELECT "BillingCurrency" AS currency, sum("BilledCost") AS cost FROM costs '
                f'WHERE "ChargePeriodStart" > {start} AND ('
                'lower("ResourceGroup") LIKE \'mc\\_%\' ESCAPE \'\\\' '
                'OR lower("ServiceName") LIKE \'%kubernetes%\') GROUP BY 1', None)

            # Marketplace/ISV, the same shape as AKS above for the same reason: a second cut
            # across the families rather than a family of its own, so it rides the same
            # connection rather than a second round trip once the tab is opened.
            marketplace_rows = r.rows(
                'SELECT "BillingCurrency" AS currency, sum("BilledCost") AS cost FROM costs '
                f'WHERE "ChargePeriodStart" > {start} AND {MARKETPLACE_CLAUSE} GROUP BY 1', None)

        seen = {r["family"] for r in rows if r["family"]}
        out: list[dict] = []
        for group in GROUPS:
            mine = [r for r in rows if r["family"] in group["families"]]
            total, currency, mixed = self._reduce(mine)
            out.append({"id": group["id"], "label": group["label"], "cost": total,
                        "currency": currency, "mixed": mixed,
                        "cost_by_currency": self._money_totals(mine)})

        # Whatever no group claimed, so the tabs still add up to the total.
        leftover = [r for r in rows if r["family"] not in CLAIMED]
        total, currency, mixed = self._reduce(leftover)
        if leftover:
            out.append({"id": OTHER["id"], "label": OTHER["label"], "cost": total,
                        "currency": currency, "mixed": mixed,
                        "cost_by_currency": self._money_totals(leftover)})

        grand, currency, mixed = self._reduce(rows)
        # Deliberately outside `sections`. AKS is a cut across the families rather than another
        # family -- its node VMs are already inside Compute & Web and its load balancers inside
        # Networking -- so adding it to that list would make the segments sum to more than the
        # estate. Kept as its own key so the picker can offer it and nothing can total it in.
        aks_total, aks_currency, _ = self._reduce(aks_rows)
        # Same reasoning as AKS: a Marketplace VM is already inside Compute & Web, a Marketplace
        # data tool already inside AI & Data. This is "who gets paid", not "what kind of thing
        # was bought", so it cuts across every family rather than living beside them.
        market_total, market_currency, _ = self._reduce(marketplace_rows)
        return {"sections": out, "total": grand, "currency": currency, "mixed": mixed,
                "total_by_currency": self._money_totals(rows),
                "days": days, "as_of": hi,
                "aks": {"cost": aks_total, "currency": aks_currency,
                        "cost_by_currency": self._money_totals(aks_rows),
                        "overlaps": True},
                "marketplace": {"cost": market_total, "currency": market_currency,
                                "cost_by_currency": self._money_totals(marketplace_rows),
                                "overlaps": True},
                "unclaimed": sorted(f for f in seen if f not in CLAIMED)}

    def executive(self, scope: list[str] | None, days: int = 30,
                  currency: str | None = None,
                  filters: dict[str, list[str]] | None = None) -> dict[str, Any]:
        """The opening view: what an owner of this estate needs before asking anything.

        Everything here is one burst against the warehouse on a single connection, so a
        page that answers "what am I spending, on what, who is spending it, and what changed"
        costs milliseconds rather than a round of Azure calls.

        The comparisons are all against the *immediately preceding* window of the same length,
        anchored to the newest data rather than to today — cost lands a day or two late, so
        anchoring to today makes every trend look like it fell off a cliff on the last day.
        """
        days = reporting.period_days(days)
        with reporting.comparison_history(), self.wh.reader(scope, currency, filters) as r:
            hi, start, prev = self._window(r, days)
            if not hi:
                return {"days": days, "empty": True}

            period = f'"ChargePeriodStart" > {start}'
            window = f'"ChargePeriodStart" > {prev}'
            # One pass per shape, all inside the same connection. Splitting current from
            # previous with a CASE rather than two queries halves the scans.
            split = (
                f'sum(CASE WHEN "ChargePeriodStart" > {start} THEN "BilledCost" ELSE 0 END) AS cost, '
                f'sum(CASE WHEN "ChargePeriodStart" <= {start} THEN "BilledCost" ELSE 0 END) AS prior'
            )

            totals = r.rows(
                'SELECT "BillingCurrency" AS currency, ' + split + f" FROM costs WHERE {window} GROUP BY 1",
                None)

            counts = r.rows(
                'SELECT "BillingCurrency" AS currency, count(DISTINCT "ResourceId") AS resources, '
                'count(DISTINCT "ServiceName") AS services, '
                'count(DISTINCT "SubAccountId") AS subscriptions, '
                'count(DISTINCT "RegionName") AS regions '
                f"FROM costs WHERE {period} GROUP BY 1", None)

            daily = r.rows(
                'SELECT "ChargePeriodStart" AS day, "BillingCurrency" AS currency, '
                f'sum("BilledCost") AS cost FROM costs WHERE {window} GROUP BY 1, 2 ORDER BY 1',
                None)

            families = r.rows(
                'SELECT "ServiceFamily" AS family, "BillingCurrency" AS currency, ' + split
                + f" FROM costs WHERE {window} GROUP BY 1, 2", None)

            # Independent of the 7d/30d/60d/90d picker above -- month-over-month is deliberately
            # a longer, coarser look than the rest of this tab, so it keeps its own window rather
            # than shrinking to a handful of bars whenever someone picks "7d" for everything else.
            monthly_family = r.rows(
                'SELECT strftime("ChargePeriodStart", \'%Y-%m\') AS month, '
                '"ServiceFamily" AS family, "BillingCurrency" AS currency, '
                'sum("BilledCost") AS cost FROM costs '
                f"WHERE \"ChargePeriodStart\" > date '{hi}' - INTERVAL {MONTHLY_TREND_MONTHS} MONTH "
                "GROUP BY 1, 2, 3", None)

            services = self._ranked(r,
                'SELECT "ServiceName" AS name, "BillingCurrency" AS currency, ' + split
                + f" FROM costs WHERE {window} GROUP BY 1, 2", 8, movers=True)

            subs = self._ranked(r,
                'SELECT coalesce("SubAccountName", "SubAccountId", \'—\') AS name, '
                '"BillingCurrency" AS currency, ' + split
                + f" FROM costs WHERE {window} GROUP BY 1, 2", 8)

            groups_top = self._ranked(r,
                'SELECT coalesce("ResourceGroup", \'—\') AS name, '
                '"BillingCurrency" AS currency, ' + split
                + f" FROM costs WHERE {window} GROUP BY 1, 2", 8)

            regions_top = self._ranked(r,
                'SELECT coalesce("RegionName", \'—\') AS name, '
                '"BillingCurrency" AS currency, ' + split
                + ' , count(DISTINCT "ResourceId") AS resources'
                + f" FROM costs WHERE {window} GROUP BY 1, 2", 20)

        return self._currency_views(self._executive_payload, {
            "days": days, "hi": hi, "totals": totals, "counts": counts, "daily": daily,
            "families": families, "monthly_family": monthly_family, "services": services,
            "subs": subs, "groups_top": groups_top, "regions_top": regions_top,
        })

    def _executive_payload(self, *, days: int, hi: str, totals: list[dict],
                           counts: list[dict], daily: list[dict], families: list[dict],
                           monthly_family: list[dict], services: list[dict], subs: list[dict],
                           groups_top: list[dict], regions_top: list[dict],
                           currency: str | None) -> dict[str, Any]:
        from datetime import date, timedelta

        total, _, mixed = self._reduce(totals)
        prior_total, _, _ = self._reduce(totals, "prior")
        c = counts[0] if counts else {}

        # The trend is split by date in Python: the boundary is a real date here, whereas in
        # SQL it was an expression, and the chart wants two aligned series anyway.
        edge = date.fromisoformat(str(hi)) - timedelta(days=int(days))
        cur_days: dict[str, float] = {}
        prev_days: dict[str, float] = {}
        for row in daily:
            d = date.fromisoformat(str(row["day"]))
            bucket = cur_days if d > edge else prev_days
            bucket[str(d)] = bucket.get(str(d), 0.0) + float(row["cost"] or 0.0)

        def movers(rows: list[dict], keys: tuple[str, ...] = ("name",)) -> list[dict]:
            """Describe changes within this view's single currency."""
            merged: dict[tuple, dict] = {}
            for row in rows:
                k = tuple(row[key] for key in keys)
                item = merged.setdefault(k, {**{key: row[key] for key in keys},
                                             "cost": 0.0, "previous": 0.0})
                item["cost"] += float(row.get("cost") or 0.0)
                item["previous"] += float(row.get("prior") or 0.0)
            out = []
            for item in merged.values():
                item["cost"] = round(item["cost"], 2)
                item["previous"] = round(item["previous"], 2)
                item["delta"] = round(item["cost"] - item["previous"], 2)
                # None, not 0: "appeared this period" and "did not move" are different facts.
                item["change_pct"] = (round(item["delta"] / item["previous"] * 100, 1)
                                      if item["previous"] else None)
                item["share_pct"] = round(item["cost"] / total * 100, 1) if total else None
                out.append(item)
            return sorted(out, key=lambda x: -x["cost"])

        by_service = movers(services)
        by_sub = movers(subs)
        by_group = movers(groups_top)

        # Regions carry a resource count as well as cost, so the map can size by spend and the
        # table can still answer "how much is actually deployed there".
        by_region = movers(regions_top)
        counts_by_region: dict[str, int] = {}
        for row in regions_top:
            counts_by_region[row["name"]] = (
                counts_by_region.get(row["name"], 0) + int(row.get("resources") or 0)
            )
        for reg in by_region:
            reg["resources"] = counts_by_region.get(reg["name"], 0)

        # Areas, in the same order as the rail, so the two agree.
        family_rows = [{"family": row["family"], "cost": float(row.get("cost") or 0),
                        "previous": float(row.get("prior") or 0)} for row in families]
        areas = []
        for group in GROUPS:
            mine = [f for f in family_rows if f["family"] in group["families"]]
            areas.append({
                "id": group["id"], "label": group["label"],
                "cost": round(sum(f["cost"] for f in mine), 2),
                "previous": round(sum(f["previous"] for f in mine), 2),
            })
        leftover = [f for f in family_rows if f["family"] not in CLAIMED]
        if leftover:
            areas.append({"id": OTHER["id"], "label": OTHER["label"],
                          "cost": round(sum(f["cost"] for f in leftover), 2),
                          "previous": round(sum(f["previous"] for f in leftover), 2)})
        for a in areas:
            a["delta"] = round(a["cost"] - a["previous"], 2)
            a["change_pct"] = (round(a["delta"] / a["previous"] * 100, 1)
                               if a["previous"] else None)
            a["share_pct"] = round(a["cost"] / total * 100, 1) if total else None
        areas = [a for a in areas if a["cost"]]

        # Same areas, over calendar months rather than within one window: one series per area
        # that ever shows a bar, sized to whatever months the warehouse actually holds. Only
        # months with at least one real row appear -- like the commitments tab's own month
        # trend, a short dev warehouse gets a short chart rather than a full 6 bars padded out
        # with months that never happened.
        monthly_buckets: dict[str, dict[str, float]] = {}
        monthly_currency = None
        for row in monthly_family:
            monthly_currency = currency
            gid, _ = _group_for_family(row["family"])
            bucket = monthly_buckets.setdefault(str(row["month"]), {})
            bucket[gid] = bucket.get(gid, 0.0) + float(row.get("cost") or 0.0)

        months_sorted = sorted(monthly_buckets)
        present_ids = {gid for vals in monthly_buckets.values() for gid in vals}
        # Rail order first, "Other services" last -- the same convention `areas` above follows,
        # so the two never disagree about which area comes before which.
        ordered_groups = [g for g in GROUPS if g["id"] in present_ids]
        if OTHER["id"] in present_ids:
            ordered_groups.append(OTHER)

        monthly_trend = None
        if len(months_sorted) >= 2:
            monthly_trend = {
                "months": months_sorted,
                "areas": [
                    {
                        "id": g["id"],
                        "label": g["label"],
                        "data": [round(monthly_buckets[m].get(g["id"], 0.0), 2)
                                 for m in months_sorted],
                    }
                    for g in ordered_groups
                ],
                "currency": monthly_currency,
            }

        # What actually moved, biggest absolute swing first. Ranking by percentage would put a
        # service that went from 2p to 8p above one that added a thousand pounds.
        swings = sorted((s for s in by_service if abs(s["delta"]) >= 0.01),
                        key=lambda x: -abs(x["delta"]))[:8]

        # How concentrated the bill is. A useful thing to know before optimising: if five
        # resources are most of the spend, that is where the effort goes.
        top5 = sum(float(row.get("cost") or 0)
                   for row in sorted(services, key=lambda row: -float(row.get("cost") or 0))[:5])

        # The most recent day of a cost export is usually still filling up: the exporter writes
        # it hours before the day is over. Plotted as-is it looks like spend collapsed, which on
        # the opening screen is the most misleading thing the page could say. Flag it here,
        # where the data is, rather than leaving every caller to guess.
        cur_values = [round(v, 2) for v in cur_days.values()]
        partial_last = False
        if len(cur_values) >= 5:
            # Compare the final day against the median of up to a week before it. A median
            # rather than a mean so one spike in the window does not mask a real partial day.
            recent = sorted(cur_values[max(0, len(cur_values) - 8):-1])
            median = recent[len(recent) // 2]
            partial_last = bool(median) and cur_values[-1] < median * 0.4

        return {
            "days": days,
            "as_of": str(hi),
            "currency": currency,
            "mixed_currency": mixed,
            "kpis": {
                "total": total,
                "previous": prior_total,
                "change_pct": (round((total - prior_total) / prior_total * 100, 1)
                               if prior_total else None),
                "daily_avg": round(total / days, 2) if days else None,
                "resources": int(c.get("resources") or 0),
                "services": int(c.get("services") or 0),
                "subscriptions": int(c.get("subscriptions") or 0),
                "regions": int(c.get("regions") or 0),
                "top5_share_pct": round(top5 / total * 100, 1) if total else None,
            },
            "trend": {
                "labels": list(cur_days),
                "values": cur_values,
                # Aligned by position, not by date: the point is "day 1 vs day 1", and the
                # previous window has its own dates that would clutter the axis.
                "previous": [round(v, 2) for v in prev_days.values()],
                # ...but the tooltip has to name both, or a reader sees one date above two
                # numbers and reasonably assumes both belong to it. The axis stays clean and the
                # hover carries the second date.
                "previous_labels": list(prev_days),
                "partial_last": partial_last,
            },
            "areas": areas,
            "monthly": monthly_trend,
            "movers": swings,
            "services": by_service[:8],
            "subscriptions": by_sub[:8],
            "resource_groups": by_group[:8],
            "regions": by_region[:20],
        }

    def section(self, section_id: str, scope: list[str] | None, days: int = 30,
                currency: str | None = None,
                filters: dict[str, list[str]] | None = None) -> dict[str, Any]:
        """Everything one tab needs, in four queries."""
        group = next((g for g in GROUPS if g["id"] == section_id), None)
        if group is None and section_id not in ("other", "marketplace"):
            raise KeyError(section_id)

        if section_id == "other":
            where = _families_clause(sorted(CLAIMED), negate=True)
            label = "Other"
        elif section_id == "marketplace":
            # Same six queries every family tab gets -- by service, by region, the trend, the
            # top resources -- just with `PublisherName` standing in for `ServiceFamily`. See
            # `MARKETPLACE_CLAUSE` for what that predicate actually is and is not.
            where = MARKETPLACE_CLAUSE
            label = "Marketplace & ISV"
        else:
            where = _families_clause(group["families"])
            label = group["label"]

        hi = ""
        days = reporting.period_days(days)
        with reporting.comparison_history(), self.wh.reader(scope, currency, filters) as r:
            hi, start, prev = self._window(r, days)
            if not hi:
                return {"section": section_id, "label": label, "empty": True, "days": days}

            period = f'"ChargePeriodStart" > {start}'
            previous = f'"ChargePeriodStart" > {prev} AND "ChargePeriodStart" <= {start}'

            totals = r.rows(
                'SELECT "BillingCurrency" AS currency, sum("BilledCost") AS cost, '
                'count(DISTINCT "ResourceId") AS resources, '
                'count(DISTINCT "ServiceName") AS services FROM costs '
                f"WHERE {where} AND {period} GROUP BY 1", None)

            before = r.rows(
                'SELECT "BillingCurrency" AS currency, sum("BilledCost") AS cost FROM costs '
                f"WHERE {where} AND {previous} GROUP BY 1", None)

            everything = r.rows(
                'SELECT "BillingCurrency" AS currency, sum("BilledCost") AS cost FROM costs '
                f"WHERE {period} GROUP BY 1", None)

            trend = r.rows(
                'SELECT "ChargePeriodStart" AS day, "BillingCurrency" AS currency, '
                'sum("BilledCost") AS cost FROM costs '
                f"WHERE {where} AND {period} GROUP BY 1, 2 ORDER BY 1", None)

            services = self._ranked(r,
                'SELECT "ServiceName" AS name, "BillingCurrency" AS currency, '
                'sum("BilledCost") AS cost FROM costs '
                f"WHERE {where} AND {period} GROUP BY 1, 2", 12)

            top = self._ranked(r,
                'SELECT coalesce("ResourceName", \'—\') AS name, '
                'coalesce("ResourceGroup", \'—\') AS grp, "ServiceName" AS service, '
                'coalesce("RegionName", \'—\') AS region, "BillingCurrency" AS currency, '
                'sum("BilledCost") AS cost FROM costs '
                f"WHERE {where} AND {period} AND \"ResourceId\" IS NOT NULL "
                "GROUP BY 1, 2, 3, 4, 5", 20)

            regions = self._ranked(r,
                'SELECT coalesce("RegionName", \'—\') AS name, "BillingCurrency" AS currency, '
                'sum("BilledCost") AS cost FROM costs '
                f"WHERE {where} AND {period} GROUP BY 1, 2", 8)

        return self._currency_views(self._section_payload, {
            "section_id": section_id, "label": label, "days": days, "hi": hi,
            "totals": totals, "before": before, "everything": everything,
            "trend": trend, "services": services, "top": top, "regions": regions,
        })

    def _section_payload(self, *, section_id: str, label: str, days: int, hi: str,
                         totals: list[dict], before: list[dict], everything: list[dict],
                         trend: list[dict], services: list[dict], top: list[dict],
                         regions: list[dict], currency: str | None) -> dict[str, Any]:
        total, _, mixed = self._reduce(totals)
        resources = sum(int(x.get("resources") or 0) for x in totals)
        prior, _, _ = self._reduce(before)
        grand, _, _ = self._reduce(everything)

        by_day: dict[str, float] = {}
        for row in trend:
            key = str(row["day"])
            by_day[key] = by_day.get(key, 0.0) + float(row["cost"] or 0.0)

        def flatten(rows: list[dict], keys: tuple[str, ...]) -> list[dict]:
            """Combine detail rows within this view's single currency."""
            merged: dict[tuple, dict] = {}
            for r in rows:
                k = tuple(r[key] for key in keys)
                item = merged.setdefault(k, {**{key: r[key] for key in keys}, "cost": 0.0})
                item["cost"] += float(r["cost"] or 0.0)
            out = sorted(merged.values(), key=lambda x: -x["cost"])
            for item in out:
                item["cost"] = round(item["cost"], 2)
            return out

        return {
            "section": section_id,
            "label": label,
            "days": days,
            "as_of": hi,
            "currency": currency,
            "mixed_currency": mixed,
            "kpis": {
                "total": total,
                "previous": prior,
                # None rather than 0 when there is nothing to compare with: "no change" and
                # "no prior data" look identical otherwise, and only one of them is true.
                "change_pct": (round((total - prior) / prior * 100, 1) if prior else None),
                "share_pct": (round(total / grand * 100, 1) if grand else None),
                "resources": resources,
                "services": sum(int(row.get("services") or 0) for row in totals),
            },
            "trend": {"labels": list(by_day), "values": [round(v, 2) for v in by_day.values()]},
            "services": flatten(services, ("name",))[:12],
            "regions": flatten(regions, ("name",))[:8],
            "resources_top": flatten(top, ("name", "grp", "service", "region"))[:20],
        }


dashboard: Dashboard | None = None


def get_dashboard() -> Dashboard:
    global dashboard
    if dashboard is None:
        dashboard = Dashboard()
    return dashboard
