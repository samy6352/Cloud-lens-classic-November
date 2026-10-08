"""One savings list, with the duplicates collapsed.

Six tabs used to answer the same question — Orphaned resources, Rightsizing, Shutdown savings,
Rate optimization, Advisor and Commitments — and they overlapped in three separate ways:

  * Advisor repeats itself. Sixteen cost recommendations on this estate are five distinct
    problems; the rest are the same problem priced at a different SKU or term. Two groups of six
    identical rows. Shown as a list they read as sixteen things to do.

  * The scans overlap each other. `demovm1` is deallocated, so the orphan scan bills it for its
    disks; it also has no CPU, so the rightsizing scan lists it; Advisor flags it too. Three
    tabs, three rows, one virtual machine.

  * Rate optimization *is* Advisor. It reads the same Cost recommendations and splits them into
    commitments and usage, so every rate suggestion already appeared under Advisor.

The costly part is not the clutter, it is the arithmetic. Each of those tabs shows a total, and
the totals share resources — $15.82 of deallocated VM spend is inside both the orphan total and
the rightsizing total. Anyone who adds the tabs up gets a saving they cannot bank.

So the rule here is: **two claims about the same resource are one opportunity, and the money is
counted once.** Where two sources disagree about how much, we keep the larger and record both,
because a disagreement is worth seeing rather than averaging away.

What is deliberately *not* merged: the reason each scan gave. A VM that three independent checks
flag is a stronger candidate than one caught by a single heuristic, and that signal only exists
if we keep the provenance. It is the one useful thing the duplication was telling us.
"""

from __future__ import annotations

import asyncio
import logging
import math
import re
import time
from collections.abc import Callable
from typing import Any

log = logging.getLogger("cost-agent.savings")

# How a category is presented: the chip label, and the order it sorts in when impact ties.
# Ordered by how directly you can act on it — deleting an orphan is a decision you can take this
# afternoon, buying a three-year reservation is not.
CATEGORIES: dict[str, dict[str, str]] = {
    "orphaned":   {"label": "Orphaned",    "hint": "Billed, attached to nothing"},
    "idle":       {"label": "Idle",        "hint": "Running, barely used"},
    "rightsize":  {"label": "Rightsize",   "hint": "Bigger than the load needs"},
    "schedule":   {"label": "Schedule",    "hint": "Idle on a predictable rhythm"},
    "rate":       {"label": "Rate",        "hint": "Same usage, cheaper price"},
    "commitment": {"label": "Commitment",  "hint": "Reserve capacity you already use"},
}
CATEGORY_ORDER = list(CATEGORIES)

# Which scan said so. `weight` is how much the source's own cost figure is trusted when two
# sources disagree: a warehouse figure is a bill we have, an Advisor estimate is a projection.
SOURCES: dict[str, dict[str, Any]] = {
    "waste":       {"label": "Orphan scan",  "basis": "billed", "weight": 3},
    "rightsizing": {"label": "Utilisation",  "basis": "billed", "weight": 3},
    "shutdown":    {"label": "Schedule scan", "basis": "estimate", "weight": 2},
    "advisor":     {"label": "Azure Advisor", "basis": "estimate", "weight": 1},
    "rates":       {"label": "Rate analysis", "basis": "estimate", "weight": 1},
}
SCAN_SOURCES = ("waste", "rightsizing", "shutdown", "advisor")
_SOURCE_TIMEOUT_SECONDS = 120.0


def _issue_text(issue: Any, limit: int = 200) -> str:
    """A source's problem as a sentence a reader can act on.

    Sources report problems as a string, a list of strings, or a mapping from check to
    reason. `str()` of the last two put Python's own `['...']` and `{'...': '...'}` on
    the Savings banner, and a fixed slice then cut it mid-word.
    """
    def flat(value: Any) -> list[str]:
        if isinstance(value, dict):
            return [f"{str(key).replace('_', ' ')}: {'; '.join(flat(item)) or 'failed'}"
                    for key, item in value.items()]
        if isinstance(value, (list, tuple, set)):
            return [part for item in value for part in flat(item)]
        text = str(value).strip() if value is not None else ""
        return [text] if text else []

    text = "; ".join(flat(issue))
    if len(text) <= limit:
        return text
    cut = text[:limit - 1]
    space = cut.rfind(" ")
    return (cut[:space] if space > limit // 2 else cut).rstrip(" ;:,") + "…"


def _num(v: Any) -> float:
    """Money, or zero. Azure sends nulls, empty strings and occasionally strings with commas."""
    if v is None or v is True or v is False:
        return 0.0
    if isinstance(v, (int, float)):
        return float(v)
    try:
        return float(str(v).replace(",", "").strip() or 0)
    except (TypeError, ValueError):
        return 0.0


def _amount(value: Any) -> float | None:
    """An available amount, including a genuine zero; absent prices stay absent."""
    if value is None or isinstance(value, bool):
        return None
    try:
        amount = float(value.replace(",", "").strip() if isinstance(value, str) else value)
    except (TypeError, ValueError):
        return None
    return amount if math.isfinite(amount) else None


def _rounded(value: float | None) -> float | None:
    return round(value, 2) if value is not None else None


def _known_total(values: list[float | None]) -> float | None:
    known = [value for value in values if value is not None]
    return _rounded(sum(known)) if known or not values else None


def _advisor_annual(row: dict[str, Any]) -> float | None:
    annual = _amount(row.get("annual_savings"))
    if annual is not None:
        return annual
    for field in ("monthly_savings", "savings"):
        monthly = _amount(row.get(field))
        if monthly is not None:
            return monthly * 12
    return None


def _norm_id(v: Any) -> str:
    """An ARM id reduced to something two sources can agree on.

    Azure is inconsistent about the case of resource-group segments — the same disk arrives as
    `.../resourceGroups/CostAnomaly/...` from one API and `.../resourcegroups/costanomaly/...`
    from another — so a case-sensitive match silently fails to dedupe. Trailing slashes likewise.
    """
    s = str(v or "").strip().rstrip("/").lower()
    return s


def _res_key(name: Any, group: Any, sub: Any = "") -> str:
    """The fallback key, for sources that report a name but no id.

    The rightsizing scan returns `name` and `resource_group` and no ARM id at all, so a VM it
    flags can only be matched to the orphan scan's entry by that pair. Scoped by subscription
    where we know it, because resource-group names repeat across subscriptions and matching
    `tub/TUB-VM` in one against `tub/TUB-VM` in another would merge two different machines.

    Returns empty when there is nothing to key on. Joining three blanks yields "||", which is
    perfectly truthy and would have every source with no resource fields matching every other.
    """
    parts = [str(x or "").strip().lower() for x in (sub, group, name)]
    return "|".join(parts) if any(parts) else ""


def _sub_of(rid: str) -> str:
    m = re.search(r"/subscriptions/([^/]+)", rid or "", re.I)
    return (m.group(1) if m else "").lower()


class Opportunity:
    """One thing worth doing, however many scans noticed it."""

    __slots__ = (
        "action",
        "annual",
        "category",
        "count",
        "currency",
        "detail",
        "disputed",
        "folded",
        "impact_class",
        "items",
        "key",
        "kind",
        "options",
        "region",
        "resource",
        "resource_group",
        "resource_id",
        "sources",
        "subscription",
        "title",
        "why",
        "window",
    )

    def __init__(self, key: str, category: str, title: str, *,
                 why: str = "", resource: str = "", resource_id: str = "",
                 resource_group: str = "", subscription: str = "", region: str = "",
                 kind: str = "", detail: str = "", window: float | None = None,
                 annual: float | None = None, currency: str = "USD", action: str = "",
                 count: int = 1, items: list[dict[str, Any]] | None = None,
                 impact_class: str = "potential_savings") -> None:
        self.key = key
        self.category = category
        self.title = title
        self.why = why
        self.resource = resource
        self.resource_id = resource_id
        self.resource_group = resource_group
        self.subscription = subscription
        self.region = region
        self.kind = kind
        self.detail = detail
        self.window = _rounded(_amount(window))
        self.annual = _rounded(_amount(annual))
        self.currency = currency or "USD"
        self.action = action
        self.count = count
        # Which evidence class this belongs to, carried from `waste.IMPACT`. Only
        # `potential_savings` is money an estate stops paying by acting on this row;
        # `cost_at_risk` is real spend that needs a decision first and is reported
        # beside the total rather than inside it, exactly as commitments already are.
        #
        # Named `impact_class` and not `impact` on purpose: `impact` is already a money
        # key here, carrying Advisor's low/medium/high severity, and a non-severity
        # string landing in it goes to `fx.convert_amount` and takes the whole finding
        # out of the report as an unconvertible amount.
        self.impact_class = impact_class or "potential_savings"
        self.items = items or []
        self.sources: list[dict[str, Any]] = []
        self.options: list[dict[str, Any]] = []
        self.disputed: dict[str, Any] | None = None
        # How many rows the source collapsed into this one that were re-estimates rather than
        # choices. Reported so the count is auditable rather than something the reader has to
        # take on trust after the list got shorter.
        self.folded = 0

    # ------------------------------------------------------------------ merging

    def add_source(self, name: str, claim: str, window: float | None = None,
                   annual: float | None = None) -> None:
        """Record that a scan found this, and reconcile its money with what we already had.

        Across sources the money is *not* added. Two scans looking at the same deallocated VM are
        describing one bill, and summing them is how the old tab totals came to overlap. We keep
        the larger figure — the smaller usually reflects a narrower view of the same resource,
        such as the orphan scan pricing only the disks while utilisation prices the whole machine
        — and when they differ by enough to matter we say so rather than quietly picking one.

        Within one source it is added, because repeat calls there are different resources: the
        utilisation scan attaches once per VM to a row covering three of them. Recording only the
        first left a row totalling 20.99 quoting a source that appeared to have found 5.92, which
        reads as a contradiction rather than as one VM out of three.
        """
        window, annual = _amount(window), _amount(annual)
        meta = SOURCES.get(name, {"label": name, "basis": "estimate", "weight": 1})
        prior = next((s for s in self.sources if s["source"] == name), None)
        if prior is None:
            self.sources.append({
                "source": name, "label": meta["label"], "basis": meta["basis"],
                "claim": claim, "matches": 1,
                "window": _rounded(window), "annual": _rounded(annual),
                "unpriced_count": int(window is None),
            })
        else:
            prior["matches"] += 1
            prior["window"] = _known_total([prior["window"], window])
            prior["annual"] = _known_total([prior["annual"], annual])
            prior["unpriced_count"] += int(window is None)
            priced = (f"{prior['window']:,.2f} over the window"
                      if prior["window"] is not None else "Provider saving estimate unavailable")
            prior["claim"] = f"{prior['matches']} of these, {priced}"
            if prior["unpriced_count"] and prior["window"] is not None:
                prior["claim"] += f"; estimate unavailable for {prior['unpriced_count']}, not included"

        w = window if prior is None else prior["window"]
        a = annual if prior is None else prior["annual"]
        if w is not None and (self.window is None or w > self.window):
            before = self.window
            self.window = round(w, 2)
            # Worth surfacing only when the gap is both relatively and absolutely material;
            # a few cents between two roundings is not a disagreement anyone needs to see.
            if before is not None and before > 0 and (w - before) / max(w, 0.01) > 0.25 and (w - before) >= 1:
                self.disputed = {"low": before, "high": round(w, 2)}
        if a is not None and (self.annual is None or a > self.annual):
            self.annual = round(a, 2)

    def add_option(self, label: str, window: float | None, annual: float | None,
                   detail: str = "") -> None:
        """Another way of buying the same saving — a different SKU, term or quantity.

        Advisor emits one recommendation per purchasable configuration, which is why six rows can
        describe one decision. They are alternatives, not additions: you buy one. Recorded so the
        choice is still visible, but only the best one counts toward the total.
        """
        if any(o["label"] == label for o in self.options):
            return
        self.options.append({
            "label": label, "detail": detail,
            "window": _rounded(_amount(window)), "annual": _rounded(_amount(annual)),
        })

    @property
    def corroborated(self) -> bool:
        """Flagged by more than one independent scan."""
        return len(self.sources) > 1

    @property
    def confidence(self) -> str:
        """How much to trust the figure, from where it came rather than how it feels.

        A number taken from the bill is not the same kind of claim as a projection from Advisor,
        and two independent scans agreeing is stronger than either alone.
        """
        billed = any(s["basis"] == "billed" for s in self.sources)
        if billed and self.corroborated:
            return "high"
        if billed:
            return "medium"
        return "low" if len(self.sources) < 2 else "medium"

    def as_dict(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "category": self.category,
            "category_label": CATEGORIES.get(self.category, {}).get("label", self.category),
            "title": self.title,
            "why": self.why,
            "resource": self.resource,
            "resource_id": self.resource_id,
            "resource_group": self.resource_group,
            "subscription": self.subscription,
            "region": self.region,
            "kind": self.kind,
            "detail": self.detail,
            "window": self.window,
            "annual": self.annual,
            "currency": self.currency,
            "action": self.action,
            "count": self.count,
            "impact_class": self.impact_class,
            "items": self.items[:12],
            "more": max(0, len(self.items) - 12),
            "sources": self.sources,
            "options": sorted(self.options, key=lambda o: (
                o["annual"] is None, -(o["annual"] or 0)))[:6],
            "corroborated": self.corroborated,
            "confidence": self.confidence,
            "disputed": self.disputed,
            "folded": self.folded,
        }


# ------------------------------------------------------------------- collection

# Waste categories carry their own shape; this maps them onto the small set of categories the
# UI filters by, so a chip means the same thing whichever scan produced the row.
_WASTE_CATEGORY = {
    "unattached_disks": "orphaned",
    "unassociated_public_ips": "orphaned",
    "empty_app_service_plans": "orphaned",
    "deallocated_vms": "orphaned",
    "orphaned_nics": "orphaned",
    "orphaned_snapshots": "orphaned",
    "empty_load_balancers": "orphaned",
    "idle_gateways": "idle",
    "stale_restore_points": "orphaned",
}


class _Index:
    """Resource id (or name+group) to the opportunity that already covers it.

    The whole point of the merge: before a scan creates a row, it asks here whether some other
    scan already reported this resource. Every id an opportunity covers is registered, including
    each item inside a grouped one, so that a per-VM finding from the utilisation scan can attach
    itself to the orphan scan's "3 deallocated VMs" row instead of becoming a fourth row.

    Two key shapes are kept for every resource, because the scans do not agree on what they know.
    The orphan scan sends a full ARM id, from which a subscription can be read; the utilisation
    scan sends a bare name and resource group and no subscription at all. Registering only the
    subscription-qualified key meant the two never met, and the deallocated VMs were counted
    twice — which is the exact arithmetic this module exists to prevent.

    The unqualified key is riskier, since resource-group names repeat across subscriptions, so
    any unqualified key claimed by two different opportunities is marked ambiguous and stops
    matching altogether. A missed merge shows one resource twice; a wrong merge silently folds
    two real resources into one and loses money from the total. The first is recoverable by
    looking, the second is not.
    """

    def __init__(self) -> None:
        self._by_key: dict[str, Opportunity] = {}
        self._ambiguous: set[str] = set()

    def register(self, opp: Opportunity, *keys: str, weak: bool = False) -> None:
        for k in keys:
            if not k:
                continue
            prior = self._by_key.get(k)
            if prior is None:
                self._by_key[k] = opp
            elif prior is not opp and weak:
                self._ambiguous.add(k)

    def find(self, *keys: str) -> Opportunity | None:
        for k in keys:
            if k and k not in self._ambiguous and k in self._by_key:
                return self._by_key[k]
        return None


def _from_waste(data: dict[str, Any], index: _Index, cur: str) -> list[Opportunity]:
    """The orphan scan. Grouped by kind, because fifteen unassociated IPs are one decision.

    Each item's id is still registered individually, so another scan that names one of those
    resources joins this row rather than opening its own.
    """
    out: list[Opportunity] = []
    for f in data.get("findings") or []:
        items = f.get("items") or []
        if f.get("count") == 0 and not items and not f.get("resource_ids"):
            continue
        impact_class = f.get("impact_class") or "potential_savings"
        # An unattached NIC or an empty resource group bills nothing, so deleting it
        # saves nothing. Listing it here put a row with a 0.00 saving next to rows worth
        # real money and invited the reader to treat the count as the finding.
        if impact_class == "inventory":
            continue
        cat = _WASTE_CATEGORY.get(f.get("category") or "", "orphaned")
        cost = _num(f.get("cost"))
        opp = Opportunity(
            key=f"waste:{f.get('category')}",
            category=cat,
            title=f.get("title") or "Idle resources",
            why=f.get("why") or "",
            window=cost,
            currency=f.get("currency") or cur,
            count=int(f.get("count") or len(items) or 1),
            impact_class=impact_class,
            items=[{
                "name": i.get("name"), "group": i.get("resource_group"),
                "region": i.get("location"), "detail": i.get("detail"),
                "cost": _num(i["cost"]) if i.get("cost") is not None else None, "id": i.get("id"),
            } for i in items],
            action="Delete it, or attach it to something that uses it.",
        )
        opp.add_source("waste", f"{opp.count} found, billed {cost:,.2f} over the window",
                       window=cost)
        strong = [_norm_id(rid) for rid in f.get("resource_ids") or []]
        strong += [_norm_id(i.get("id")) for i in items]
        strong += [_res_key(i.get("name"), i.get("resource_group"),
                            _sub_of(str(i.get("id") or ""))) for i in items]
        index.register(opp, opp.key, *strong)
        # The same resources keyed without their subscription, for scans that do not send one.
        index.register(opp, *[_res_key(i.get("name"), i.get("resource_group"), "")
                              for i in items], weak=True)
        out.append(opp)
    return out


def _source_subscription(row: dict[str, Any]) -> str:
    return str(row.get("subscriptionId") or row.get("subscription_id")
               or _sub_of(str(row.get("id") or row.get("resource_id") or "")) or "").lower()


def _from_rightsizing(data: dict[str, Any], index: _Index, cur: str) -> list[Opportunity]:
    """The utilisation scan. Per VM, and usually the second scan to reach a given machine.

    A deallocated VM is already in the orphan scan's row; all this adds is a second opinion on
    the same bill. A *running* VM with no load is new information and gets its own row.
    """
    out: list[Opportunity] = []
    for vm in data.get("vms") or []:
        name, group = vm.get("name"), vm.get("resource_group")
        sub = _source_subscription(vm)
        cost = _num(vm.get("cost"))
        state = (vm.get("state") or "").lower()
        cpu = vm.get("cpu_avg")
        idle = cpu is not None and _num(cpu) < _num(data.get("cpu_threshold") or 5)
        stopped = "deallocat" in state

        claim = (f"{state or 'unknown'}, "
                 + (f"CPU {_num(cpu):.1f}% average" if cpu is not None else "no CPU data")
                 + f", {cost:,.2f} over the window")

        existing = index.find(_norm_id(vm.get("id")), _res_key(name, group, sub),
                              _res_key(name, group, "") if not sub else "")
        if existing is not None:
            existing.add_source("rightsizing", claim, window=cost)
            continue

        if not (idle or stopped):
            continue          # a busy VM is not an opportunity
        if cost <= 0:
            # Nothing is being spent on it, so there is nothing to recover. A row here would be
            # an instruction to go and save zero pounds, which is worse than silence.
            continue

        opp = Opportunity(
            key=f"vm:{_norm_id(vm.get('id')) or _res_key(name, group, sub)}",
            category="idle" if not stopped else "orphaned",
            title=f"{name} is {'stopped but still billed' if stopped else 'running near idle'}",
            why=("Deallocated machines cost nothing to run, but their disks are still billed."
                 if stopped else
                 "The machine is on and charged in full while doing almost no work."),
            resource=name or "", resource_id=vm.get("id") or "", resource_group=group or "",
            subscription=sub,
            region=vm.get("location") or "", kind=vm.get("size") or "",
            detail=f"{vm.get('size') or ''} · {state}".strip(" ·"),
            window=cost, currency=vm.get("currency") or cur,
            action="Resize it to the load it actually carries, or shut it down.",
        )
        opp.add_source("rightsizing", claim, window=cost)
        index.register(opp, opp.key, _norm_id(vm.get("id")),
                       _res_key(name, group, sub))
        out.append(opp)
    return out


def _from_shutdown(data: dict[str, Any], index: _Index, cur: str,
                   days: int = 30) -> list[Opportunity]:
    """Machines idle on a rhythm. Almost always a VM some other scan has already named."""
    out: list[Opportunity] = []
    for c in data.get("candidates") or []:
        name, group = c.get("name"), c.get("resource_group")
        sub = _source_subscription(c)
        period = float(c.get("estimate_period_days") or data.get("estimate_period_days") or 30)
        monthly = _num(c.get("saving") if c.get("saving") is not None else c.get("monthly_saving"))
        saving = monthly * max(1, int(days)) / period
        claim = c.get("pattern") or c.get("detail") or "idle on a predictable schedule"
        claim += f" — {monthly:,.2f} per {period:g} days, projected over {days} days"

        existing = index.find(_norm_id(c.get("id")), _res_key(name, group, sub),
                              _res_key(name, group, "") if not sub else "")
        if existing is not None:
            existing.add_source("shutdown", f"{claim} — about {saving:,.2f} recoverable",
                                window=saving)
            continue

        opp = Opportunity(
            key=f"sched:{_norm_id(c.get('id')) or _res_key(name, group, sub)}",
            category="schedule",
            title=f"{name} is idle on a predictable schedule",
            why="It is quiet at the same hours every day, so those hours need not be paid for.",
            resource=name or "", resource_group=group or "",
            resource_id=c.get("id") or "", subscription=sub,
            region=c.get("location") or "", window=round(saving, 2),
            currency=c.get("currency") or cur,
            action="Put it on a start/stop schedule covering the quiet hours.",
        )
        opp.add_source("shutdown", claim, window=saving)
        index.register(opp, opp.key, _norm_id(c.get("id")))
        out.append(opp)
    return out


def _advisor_category(problem: str) -> str:
    """Advisor states the problem in prose; this is the only signal for what kind it is."""
    p = (problem or "").lower()
    if "reserv" in p or "savings plan" in p or "reserved instance" in p:
        return "commitment"
    if "right-size" in p or "rightsize" in p or "underutilized" in p or "under-utilized" in p:
        return "rightsize"
    if "not attached" in p or "unattached" in p or "idle" in p or "unused" in p:
        return "orphaned"
    return "rate"


def _from_advisor(rows: list[dict[str, Any]], index: _Index, cur: str,
                  days: int, source: str) -> list[Opportunity]:
    """Azure Advisor, with its two different kinds of repetition told apart.

    Advisor repeats itself twice over, and the two repetitions mean opposite things:

      * **Terms are alternatives.** A one-year and a three-year reservation on the same plan are
        two ways to buy the same saving. You take one, so only the best counts — summing them
        would treble the estate's headline.

      * **Lookback windows are re-estimates.** The same term priced over 7, 30 and 60 days of
        history is one recommendation Advisor has costed three ways. Those are not choices at
        all, and showing them as rows is what made sixteen recommendations out of five.

    So rows collapse to one per *decision* — problem, resource, SKU, term, quantity — and the
    lookback spread becomes the confidence range on that decision's figure. The longest lookback
    supplies the headline number, deliberately not the largest: on this estate the seven-day
    window is the most optimistic of the three, so taking the maximum would have quietly
    published the least-evidenced estimate every time.
    """
    def decision_key(r: dict[str, Any]) -> str:
        problem = (r.get("problem") or r.get("solution") or "Cost recommendation").strip()
        target = (r.get("resource_id") or r.get("resource") or r.get("name")
                  or r.get("subscriptionId") or "").strip()
        return "||".join(str(x or "").lower() for x in (
            problem, target, _source_subscription(r), r.get("sku"), r.get("term"),
            r.get("quantity"), r.get("region"), r.get("scope"), r.get("currency")))

    # First fold the re-estimates, keeping the spread.
    decisions: dict[str, dict[str, Any]] = {}
    for r in rows:
        k = decision_key(r)
        annual = _advisor_annual(r)
        d = decisions.get(k)
        if d is None:
            decisions[k] = {"row": r, "annual": annual, "low": annual, "high": annual,
                            "estimates": 1, "unpriced": int(annual is None),
                            "lookback": _num(r.get("lookback_days"))}
            continue
        d["estimates"] += 1
        d["unpriced"] += int(annual is None)
        if annual is not None:
            d["low"] = min(d["low"], annual) if d["low"] is not None else annual
            d["high"] = max(d["high"], annual) if d["high"] is not None else annual
        # A longer but unpriced lookback cannot erase the last qualified estimate.
        if ((annual is not None and d["annual"] is None)
                or ((annual is None) == (d["annual"] is None)
                    and _num(r.get("lookback_days")) > d["lookback"])):
            d["row"], d["lookback"], d["annual"] = r, _num(r.get("lookback_days")), annual

    # Then group the remaining alternatives by the problem they solve.
    groups: dict[str, list[dict[str, Any]]] = {}
    for d in decisions.values():
        r = d["row"]
        problem = (r.get("problem") or r.get("solution") or "Cost recommendation").strip()
        target = (r.get("resource_id") or r.get("resource") or r.get("name")
                  or r.get("subscriptionId") or "").strip()
        identity = [problem, target, _source_subscription(r), r.get("currency")]
        if _advisor_category(problem) == "commitment":
            identity += [r.get("sku"), r.get("region"), r.get("scope"), r.get("quantity")]
        key = "||".join(str(v or "").strip().lower() for v in identity)
        groups.setdefault(key, []).append(d)

    out: list[Opportunity] = []
    for gkey, ds in groups.items():
        best_d = max(ds, key=lambda d: (d["annual"] is not None, d["annual"] or 0))
        first = best_d["row"]
        problem = (first.get("problem") or first.get("solution") or "Cost recommendation").strip()
        target = (first.get("resource") or first.get("name")
                  or first.get("subscriptionId") or "").strip()
        cat = _advisor_category(problem)

        annual = best_d["annual"]
        window = annual * (max(days, 1) / 365.0) if annual is not None else None
        folded = sum(d["estimates"] - 1 for d in ds)

        claim = f"{problem} — " + (
            f"about {annual:,.0f} a year" if annual is not None
            else "Provider saving estimate unavailable")
        if len(ds) > 1:
            claim += f", best of {len(ds)} purchasable options"
        if best_d["lookback"]:
            claim += f", from {int(best_d['lookback'])} days of usage"
        unpriced = sum(d["unpriced"] for d in ds)
        if unpriced and annual is not None:
            claim += f"; {unpriced} unpriced re-estimates/options excluded"

        # Advisor names the *subscription* as the resource for anything bought at subscription
        # scope, so a reservation and a savings plan in the same subscription arrive with an
        # identical resource field. Matching on it merged two unrelated recommendations into one
        # row and added their money together — the opposite of what this module is for. A
        # subscription is a place, not a thing to act on, so it is not an identity here.
        looks_like_sub = bool(re.fullmatch(r"[0-9a-f-]{36}", target, re.I))
        match_key = "" if looks_like_sub else _norm_id(first.get("resource_id") or target)

        existing = index.find(match_key, _norm_id(first.get("id")),
                              _res_key(first.get("name") or target, first.get("resourceGroup"),
                                       _source_subscription(first)))
        target_opp = existing
        if target_opp is None:
            target_opp = Opportunity(
                key=f"advisor:{gkey}",
                category=cat,
                title=problem,
                why=("Azure Advisor compares your recent usage against the price of committing "
                     "to it in advance." if cat == "commitment" else
                     "Azure Advisor flagged this from your recent usage."),
                resource="" if looks_like_sub else target,
                resource_id="" if looks_like_sub else (first.get("resource_id") or target),
                resource_group=first.get("resourceGroup") or "",
                subscription=first.get("subscriptionId") or (target if looks_like_sub else ""),
                region=first.get("region") or "",
                kind=first.get("sku") or "",
                detail=" · ".join(x for x in (first.get("sku"), first.get("region"),
                                              first.get("scope")) if x),
                window=window, annual=annual,
                currency=first.get("currency") or cur,
                action=("Choose the term that matches how long you expect to keep running this."
                        if cat == "commitment" else "Apply the change Advisor describes."),
            )
            index.register(target_opp, target_opp.key, match_key)
            out.append(target_opp)

        target_opp.add_source(source, claim, window=window, annual=annual)
        target_opp.folded += folded
        if len(ds) > 1:
            for d in ds:
                a = d["annual"]
                target_opp.add_option(
                    _option_label(d["row"]), a * (max(days, 1) / 365.0) if a is not None else None, a,
                    detail=_spread_note(d))
    return out


def _spread_note(d: dict[str, Any]) -> str:
    """How firm one option's figure is, said in terms of what produced it."""
    if d["annual"] is None:
        return "Provider saving estimate unavailable"
    if d["unpriced"]:
        return (f"{d['unpriced']} unpriced lookback windows excluded; "
                "shown from the longest priced lookback")
    if d["estimates"] < 2:
        return f"from {int(d['lookback'])} days of usage" if d["lookback"] else ""
    if abs(d["high"] - d["low"]) < 0.5:
        return f"same figure across {d['estimates']} lookback windows"
    return (f"{d['low']:,.0f}–{d['high']:,.0f} depending on the lookback window; "
            f"shown from the longest")


def _option_label(r: dict[str, Any]) -> str:
    """Name a purchasable variant by what distinguishes it from its siblings.

    Term first, because that is the decision: a three-year commitment saves more and binds you
    for longer, and everything else about the two rows is usually identical.
    """
    term = str(r.get("term") or "").strip()
    pretty = {"P1Y": "1 year", "P3Y": "3 years", "P5Y": "5 years"}.get(term.upper(), term)
    bits = [pretty, str(r.get("sku") or "").strip()]
    q = str(r.get("quantity") or "").strip()
    if q and q not in {"0", "None", "1"}:
        bits.append(f"×{q}")
    label = " · ".join(b for b in bits if b)
    return label or "as recommended"


# -------------------------------------------------------------------- assembly

def _rank(opps: list[Opportunity]) -> list[Opportunity]:
    """Biggest recoverable money first, with corroboration breaking ties.

    Ranking on the window figure rather than the annual one, because the annual figure is only
    available for Advisor rows and sorting a mixed list by it puts every projection above every
    resource that is being billed today.
    """
    return sorted(opps, key=lambda o: (
        o.window is None,
        -(o.window or 0),
        not o.corroborated,
        CATEGORY_ORDER.index(o.category) if o.category in CATEGORY_ORDER else 99,
        o.title.lower(),
    ))


async def build(subs: list[str] | None, days: int,
                currency: str | None = None, *,
                on_progress: Callable[[dict[str, Any]], None] | None = None) -> dict[str, Any]:
    """Every savings source, merged into one list.

    Sources run concurrently and a failure is named rather than hidden — an estate whose Advisor
    is throttled should still see its orphaned disks, and "we could not read Advisor" is itself
    worth showing next to a total that is therefore incomplete.
    """
    from . import shutdown as shutdown_mod
    from . import waste as waste_mod

    jobs = {
        "waste": waste_mod.find_waste(subscription_ids=subs, days=days, top=60),
        "rightsizing": waste_mod.vm_utilisation(subscription_ids=subs, days=min(days, 90)),
        "advisor": waste_mod.advisor_recommendations(subscription_ids=subs, category="Cost"),
        "shutdown": shutdown_mod.find_schedulable(subs, days=min(days, 14)),
    }
    started = time.monotonic()
    data: dict[str, Any] = {}
    failed: dict[str, str] = {}
    completed: list[str] = []

    async def read(name: str, job: Any) -> tuple[str, Any]:
        try:
            async with asyncio.timeout(_SOURCE_TIMEOUT_SECONDS):
                return name, await job
        except TimeoutError:
            return name, TimeoutError("The source timed out.")
        except asyncio.CancelledError:
            return name, RuntimeError("The source was cancelled.")
        except Exception as exc:
            return name, exc

    tasks = [asyncio.create_task(read(name, job)) for name, job in jobs.items()]
    try:
        for finished in asyncio.as_completed(tasks):
            name, res = await finished
            completed.append(name)
            if isinstance(res, Exception):
                failed[name] = _issue_text(str(res) or type(res).__name__)
            elif not isinstance(res, dict):
                failed[name] = "The source returned an invalid response."
            else:
                data[name] = res
                issue = (res.get("error") or res.get("errors") or res.get("unavailable")
                         or ("The source returned partial results." if res.get("partial") else None))
                if issue:
                    failed[name] = _issue_text(issue)
            if name in failed:
                log.warning("savings: %s unavailable: %s", name, failed[name])
            # Rebuild from raw inputs, never incrementally mutate the previous merge: a late
            # orphan scan must still take precedence over rightsizing and Advisor.
            try:
                result = assemble(data, failed, days, currency,
                                  took_ms=int((time.monotonic() - started) * 1000))
            except (TypeError, ValueError, KeyError, AttributeError):
                data.pop(name, None)
                failed[name] = "The source returned an invalid response."
                result = assemble(data, failed, days, currency,
                                  took_ms=int((time.monotonic() - started) * 1000))
            status = "running" if len(completed) < len(SCAN_SOURCES) else (
                "error" if len(failed) == len(SCAN_SOURCES) else "complete")
            result["scan"] = {
                "status": status, "completed": list(completed), "total": len(SCAN_SOURCES),
                "errors": dict(result["unavailable"]), "revision": len(completed),
            }
            if on_progress:
                on_progress(result)
        return result
    finally:
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


def assemble(data: dict[str, Any], failed: dict[str, str], days: int,
             currency: str | None = None, *, took_ms: int = 0) -> dict[str, Any]:
    """Merge any completed subset using the same deterministic precedence as a full scan."""
    source_failures = {
        name: _issue_text(payload.get("error") or payload.get("errors")
                          or "Source scan is incomplete.", 400)
        for name, payload in data.items()
        if isinstance(payload, dict) and (payload.get("error") or payload.get("errors") or payload.get("partial"))
    }
    data, cur, currency_failures, unconverted = _normalise_sources(data, currency)
    failed = {**source_failures, **failed, **currency_failures}
    index = _Index()
    opps: list[Opportunity] = []

    # Order matters. The orphan scan runs first because its figures come from the bill and its
    # groupings are the ones a reader recognises; later scans then attach to those rows rather
    # than the other way round, which keeps "3 deallocated VMs" as one line instead of three.
    opps += _from_waste(data.get("waste") or {}, index, cur)
    opps += _from_rightsizing(data.get("rightsizing") or {}, index, cur)
    opps += _from_shutdown(data.get("shutdown") or {}, index, cur, days)
    opps += _from_advisor(list((data.get("advisor") or {}).get("recommendations") or []),
                          index, cur, days, "advisor")

    ranked = _rank(opps)

    # Commitments come out of this list entirely, and this is the de-duplication that matters
    # most on screen. Buying a reservation is not a cleanup task: it does not remove a resource,
    # it re-prices one that stays. Left in, the same Advisor recommendation appeared as a card
    # here, a row in the commitments table, and a share of the "recoverable" headline — three
    # sightings of one decision, and a total nobody could bank because a third of it needed a
    # three-year signature rather than a delete button. They now live on the Commitments tab,
    # which compares them against pay-as-you-go, which is the only frame in which they mean
    # anything.
    commitments = [o for o in ranked if o.category == "commitment"]
    ranked = [o for o in ranked if o.category != "commitment"]

    # The same argument as commitments, one step earlier: a deallocated VM's retained
    # disks, a paused SQL database and an oversized elastic pool are all real, continuing
    # spend, but none of them is money the estate stops paying by acting on the row as
    # written -- each needs a sizing, migration or retention decision first. Adding them
    # to the recoverable total produces a headline that cannot survive its first review,
    # so they are reported alongside it instead. See `waste.IMPACT`.
    at_risk = [o for o in ranked if o.impact_class == "cost_at_risk"]
    ranked = [o for o in ranked if o.impact_class != "cost_at_risk"]

    # The number that matters, and the one the six separate tabs could not state: money counted
    # once. Split by basis because a bill and a projection should not be added into one figure
    # and presented as if they were the same kind of certainty.
    billed = _known_total([o.window for o in ranked
                           if any(s["basis"] == "billed" for s in o.sources)])
    projected = _known_total([o.window for o in ranked
                              if not any(s["basis"] == "billed" for s in o.sources)])
    unpriced_count = sum(o.window is None for o in ranked)

    merged = sum(1 for o in ranked if o.corroborated)
    collapsed = sum(o.folded for o in ranked)

    return {
        "opportunities": [o.as_dict() for o in ranked],
        "count": len(ranked),
        "billed_total": billed,
        "projected_total": projected,
        "total": _known_total([o.window for o in ranked]),
        "unpriced_count": unpriced_count,
        "currency": cur,
        "days": days,
        "categories": [
            {"id": cid, **meta,
             "count": sum(1 for o in ranked if o.category == cid),
             "unpriced_count": sum(o.window is None for o in ranked if o.category == cid),
             "total": _known_total([o.window for o in ranked if o.category == cid])}
            for cid, meta in CATEGORIES.items() if cid != "commitment"
        ],
        # Reported rather than dropped: the Savings tab still says how much sits behind the
        # commitment decision and where to go and make it, it just does not add it in.
        "commitments": [o.as_dict() for o in commitments],
        "commitment_count": len(commitments),
        "commitment_annual": _known_total([o.annual for o in commitments]),
        "commitment_unpriced_count": sum(o.annual is None for o in commitments),
        "commitment_note": ("Reservations and savings plans re-price what you keep rather than "
                            "remove it, so they are not in the figures above. They are compared "
                            "against pay-as-you-go on the Reservations & Savings Plans tab."),
        # Same treatment, different reason: real spend, but not recoverable by the row alone.
        "at_risk": [o.as_dict() for o in at_risk],
        "at_risk_count": len(at_risk),
        "at_risk_total": _known_total([o.window for o in at_risk]),
        "at_risk_unpriced_count": sum(o.window is None for o in at_risk),
        "at_risk_note": ("Spend that is continuing but is not recovered by deleting the resource "
                         "named — retained disks behind a deallocated VM, a paused database, an "
                         "oversized pool. Reported separately so the recoverable total stays "
                         "one an estate can actually bank."),
        "merged": merged,
        "variants_folded": collapsed,
        "sources_read": sorted(data),
        "unavailable": dict(failed),
        "partial": bool(failed) or bool(unpriced_count) or any(
            payload.get("partial") for payload in data.values()),
        "unconverted": unconverted,
        "pricing_basis": (
            f"Each source amount is converted to {cur} before merging or comparing estimates. "
            "Amounts without a known exchange rate are excluded and listed as unavailable."
        ),
        "took_ms": took_ms,
        "note": ("Each resource is counted once, however many scans found it. Where two scans "
                 "disagreed on the amount, the larger is shown and the range is noted."),
    }


def _normalise_sources(
    data: dict[str, Any], currency: str | None,
) -> tuple[dict[str, Any], str, dict[str, str], list[dict[str, Any]]]:
    """Convert individual claims before any max, sum, grouping or deduplication."""
    from . import currency as fx

    target = (currency or "").upper()
    codes: set[str] = set()

    def collect(node: Any) -> None:
        if isinstance(node, dict):
            code = str(node.get("currency") or "").strip().upper()
            if code and code != "MIXED":
                codes.add(code)
            codes.update(str(code).upper() for code in (node.get("cost_by_currency") or {}))
            for value in node.values():
                collect(value)
        elif isinstance(node, list):
            for value in node:
                collect(value)

    collect(data)
    if not target or target == "BILLED":
        target = next(iter(codes)) if len(codes) == 1 else "USD"
    money = fx.MONEY_KEYS | {"window", "annual", "total", "low", "high", "current", "monthly_savings"}

    def walk(node: Any, inherited: str) -> Any:
        if isinstance(node, list):
            return [walk(value, inherited) for value in node]
        if not isinstance(node, dict):
            return node
        original = fx.currency_code(node.get("currency") if "currency" in node else inherited) or ""
        bucket_fields = {
            key: key.removesuffix("_by_currency") for key in node
            if key in {"cost_by_currency", "current_by_currency", "scheduled_by_currency", "saving_by_currency"}
            and node[key]
        }
        out = {}
        for key, field in bucket_fields.items():
            converted = [fx.convert_amount(amount, code, target) for code, amount in node[key].items()]
            if any(amount is None for amount in converted):
                raise ValueError(f"Complete {field} currency conversion to {target} is unavailable.")
            amount = sum(converted)
            out[field] = amount
            out[key] = {target: amount}
        for key, value in node.items():
            if key in bucket_fields or key in bucket_fields.values():
                continue
            if key in {"cost_by_currency", "current_by_currency", "scheduled_by_currency", "saving_by_currency"} and not value:
                out[key] = {}
                continue
            if key.startswith("native_"):
                out[key] = value
                continue
            if key in money and value is not None:
                if key == "window" and isinstance(value, str) and ":" in value:
                    out[key] = value
                    continue
                if key == "impact" and isinstance(value, str) and value.strip().lower() in {"low", "medium", "high"}:
                    out[key] = value
                    continue
                amount = value.replace(",", "").strip() if isinstance(value, str) else value
                converted = fx.convert_amount(amount, original, target)
                if converted is None:
                    raise ValueError(f"No verified amount/currency conversion from {original or 'unknown'} to {target}.")
                out[key] = converted
            elif key == "currency":
                out[key] = target
            else:
                out[key] = walk(value, original)
        out["currency"] = target
        return out

    out: dict[str, Any] = {}
    failed: dict[str, str] = {}
    unconverted = []
    collections = {"waste": "findings", "rightsizing": "vms",
                   "shutdown": "candidates", "advisor": "recommendations"}
    for source, payload in data.items():
        key = collections.get(source)
        if key is None:
            out[source] = payload
            continue
        inherited = str(payload.get("currency") or "") if source != "advisor" else ""
        converted = []
        for row in payload.get(key) or []:
            try:
                if row.get("unpriced_count"):
                    raise ValueError("The finding includes resources without complete currency-qualified billing.")
                if source == "shutdown":
                    period = float(row.get("estimate_period_days") or payload.get("estimate_period_days") or 30)
                    if not math.isfinite(period) or period <= 0:
                        raise ValueError("The shutdown estimate period is unavailable.")
                if source == "waste" and row.get("items") and not row.get("cost_by_currency"):
                    native = str(row.get("currency") or inherited).upper()
                    item_codes = {str(item.get("currency") or native).upper()
                                  for item in row["items"]}
                    if len(item_codes) > 1 or native == "MIXED":
                        if int(row.get("count") or len(row["items"])) != len(row["items"]):
                            raise ValueError("Mixed-currency finding has incomplete item coverage.")
                        items = walk(row["items"], native)
                        # The source's aggregate is not money until its item currencies agree.
                        clean = {k: v for k, v in row.items() if k not in {"cost", "items"}}
                        normalised = walk(clean, native)
                        normalised.update(items=items, cost=sum(_num(i.get("cost")) for i in items))
                    else:
                        normalised = walk(row, native)
                else:
                    if source == "advisor":
                        row = {**row, **{field: _amount(row.get(field))
                                        for field in ("annual_savings", "monthly_savings", "savings")
                                        if field in row}}
                    normalised = walk(row, inherited)
                converted.append(normalised)
            except ValueError as exc:
                failed[f"{source}_currency"] = str(exc)
                unconverted.append({"source": source, "reason": str(exc), "finding": row})
        out[source] = {**payload, key: converted, "currency": target}
    return out, target, failed, unconverted


def _currency_of(data: dict[str, Any]) -> str:
    for key in ("waste", "advisor", "rightsizing", "shutdown"):
        c = (data.get(key) or {}).get("currency")
        if c:
            return str(c).upper()
    return ""
