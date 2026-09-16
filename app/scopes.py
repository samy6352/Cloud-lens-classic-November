"""Cost Management scope capabilities, export planning, and manifest validation.

Billing exports reduce subscription fan-out, but dataset support varies by agreement.
Only declared coverage can replace a subscription fetch. Manifests identify a run's
partitions and row counts; timestamps and blob-name conventions do not.
"""
from __future__ import annotations

import json
import logging
import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from typing import Any

log = logging.getLogger("cloudlens.scopes")

BILLING = "/providers/Microsoft.Billing/billingAccounts"
MG = "/providers/Microsoft.Management/managementGroups"

# Dataset names as the Exports API spells them.
FOCUS = "FocusCost"
ACTUAL = "ActualCost"
AMORTIZED = "AmortizedCost"
USAGE = "Usage"
PRICE_SHEET = "PriceSheet"
RESERVATION_DETAILS = "ReservationDetails"
RESERVATION_TRANSACTIONS = "ReservationTransactions"
RESERVATION_RECOMMENDATIONS = "ReservationRecommendations"

# Which datasets each scope kind can export.
#
# Sources (MS Learn, verified 2026-09-02):
#   tutorial-improved-exports -- per-dataset scope table and the Limitations section
#   rest/api/cost-management/exports/create-or-update (2026-06-01) -- scope examples
#
# Management group is the outlier: "Only the cost and usage details (Usage) dataset is
# available in CSV format without any compression", Enterprise Agreement only, usage charges
# only, and no multi-currency. It is listed as supported almost everywhere and is a trap.
_DATASETS: dict[str, frozenset[str]] = {
    "subscription": frozenset({FOCUS, ACTUAL, AMORTIZED}),
    "resourceGroup": frozenset({FOCUS, ACTUAL, AMORTIZED}),
    "billingAccount": frozenset({FOCUS, ACTUAL, AMORTIZED, PRICE_SHEET,
                                 RESERVATION_DETAILS, RESERVATION_TRANSACTIONS,
                                 RESERVATION_RECOMMENDATIONS}),
    "billingProfile": frozenset({FOCUS, ACTUAL, AMORTIZED, PRICE_SHEET,
                                 RESERVATION_DETAILS, RESERVATION_TRANSACTIONS,
                                 RESERVATION_RECOMMENDATIONS}),
    "invoiceSection": frozenset({FOCUS, ACTUAL, AMORTIZED}),
    "department": frozenset({FOCUS, ACTUAL, AMORTIZED}),
    "enrollmentAccount": frozenset({FOCUS, ACTUAL, AMORTIZED}),
    "customer": frozenset({FOCUS, ACTUAL, AMORTIZED}),
    "managementGroup": frozenset({USAGE}),
}

# Agreements that cannot do FOCUS at all. MOSP is pay-as-you-go / "web direct", and this is the
# exact refusal seen on this tenant: "Export type: FocusCost is not supported for Agreement
# Type: WebDirect and Subscription".
_NO_FOCUS_AGREEMENTS = frozenset({
    "microsoftonlineservicesprogram", "mosp", "webdirect", "payasyougo",
})


@dataclass(frozen=True)
class Scope:
    """One place Cost Management can be asked about, as a value rather than a URL fragment.

    `kind` is the Azure scope type; `parts` are its identifiers, outermost first -- so a billing
    profile is ("billing-account-id", "profile-id"). Frozen because a scope is an identity: two
    scopes naming the same thing must be equal and hashable, which is what lets callers
    de-duplicate them.
    """

    kind: str
    parts: tuple[str, ...]
    display_name: str | None = None
    agreement: str | None = None

    # ------------------------------------------------------------------ construction
    @classmethod
    def subscription(cls, sub_id: str, **kw: Any) -> Scope:
        return cls("subscription", (_clean(sub_id),), **kw)

    @classmethod
    def resource_group(cls, sub_id: str, name: str, **kw: Any) -> Scope:
        return cls("resourceGroup", (_clean(sub_id), _clean(name)), **kw)

    @classmethod
    def billing_account(cls, account_id: str, **kw: Any) -> Scope:
        return cls("billingAccount", (_clean(account_id),), **kw)

    @classmethod
    def billing_profile(cls, account_id: str, profile_id: str, **kw: Any) -> Scope:
        return cls("billingProfile", (_clean(account_id), _clean(profile_id)), **kw)

    @classmethod
    def management_group(cls, group_id: str, **kw: Any) -> Scope:
        return cls("managementGroup", (_clean(group_id),), **kw)

    @classmethod
    def parse(cls, value: str, **kw: Any) -> Scope:
        """Build a scope from an ARM path, or from a bare subscription GUID.

        Accepting a bare GUID matters: every existing call site holds subscription ids, so this
        is what lets them be passed straight through without each one learning about scopes.
        """
        raw = (value or "").strip()
        if not raw:
            raise ValueError("a scope cannot be empty")

        if _GUID.fullmatch(raw):
            return cls.subscription(raw, **kw)

        path = "/" + raw.strip("/")
        for kind, pattern in _PATTERNS:
            m = pattern.fullmatch(path)
            if m:
                return cls(kind, tuple(_clean(g) for g in m.groups()), **kw)
        raise ValueError(f"unrecognised scope: {value!r}")

    # ------------------------------------------------------------------ identity
    @property
    def path(self) -> str:
        """The ARM path Cost Management expects, with no leading or trailing slash issues."""
        p = self.parts
        if self.kind == "subscription":
            return f"/subscriptions/{p[0]}"
        if self.kind == "resourceGroup":
            return f"/subscriptions/{p[0]}/resourceGroups/{p[1]}"
        if self.kind == "billingAccount":
            return f"{BILLING}/{p[0]}"
        if self.kind == "billingProfile":
            return f"{BILLING}/{p[0]}/billingProfiles/{p[1]}"
        if self.kind == "invoiceSection":
            return f"{BILLING}/{p[0]}/billingProfiles/{p[1]}/invoiceSections/{p[2]}"
        if self.kind == "department":
            return f"{BILLING}/{p[0]}/departments/{p[1]}"
        if self.kind == "enrollmentAccount":
            return f"{BILLING}/{p[0]}/enrollmentAccounts/{p[1]}"
        if self.kind == "customer":
            return f"{BILLING}/{p[0]}/customers/{p[1]}"
        if self.kind == "managementGroup":
            return f"{MG}/{p[0]}"
        raise ValueError(f"no ARM path for scope kind {self.kind!r}")

    @property
    def label(self) -> str:
        return self.display_name or self.parts[-1]

    @property
    def is_billing(self) -> bool:
        """Whether this scope spans subscriptions, which is the whole point of using one."""
        return self.kind in {"billingAccount", "billingProfile", "department",
                             "enrollmentAccount", "invoiceSection", "customer"}

    @property
    def subscription_id(self) -> str | None:
        """The subscription this scope sits in, when it is inside exactly one."""
        return self.parts[0] if self.kind in {"subscription", "resourceGroup"} else None

    def exports_url(self, arm: str, api_version: str) -> str:
        return f"{arm}{self.path}/providers/Microsoft.CostManagement/exports?api-version={api_version}"

    def export_url(self, arm: str, name: str, api_version: str) -> str:
        return (f"{arm}{self.path}/providers/Microsoft.CostManagement/exports/{name}"
                f"?api-version={api_version}")

    # ------------------------------------------------------------------ capability
    def supports(self, dataset: str) -> bool:
        """Whether this scope can export `dataset`, given its kind and agreement type."""
        if dataset not in _DATASETS.get(self.kind, frozenset()):
            return False
        return not (dataset == FOCUS and _normalise_agreement(self.agreement) in _NO_FOCUS_AGREEMENTS)

    def why_not(self, dataset: str) -> str | None:
        """A sentence explaining a refusal, or None when it is supported.

        Returned rather than raised because the useful thing to do with an unsupported
        combination is usually to pick another one, not to stop.
        """
        if self.supports(dataset):
            return None
        if dataset == FOCUS and _normalise_agreement(self.agreement) in _NO_FOCUS_AGREEMENTS:
            return (f"{self.label}: FOCUS exports are not available on "
                    f"{self.agreement} billing accounts. Use ActualCost instead.")
        if self.kind == "managementGroup":
            return (f"{self.label}: management group exports carry only the '{USAGE}' dataset, "
                    "in uncompressed CSV, on Enterprise Agreement only — no purchases, no "
                    "amortised cost, and no multiple currencies. Export at billing account "
                    "scope instead to get combined data for every subscription.")
        if dataset in {PRICE_SHEET, RESERVATION_DETAILS, RESERVATION_TRANSACTIONS,
                       RESERVATION_RECOMMENDATIONS}:
            return (f"{self.label}: '{dataset}' exists only at billing account (EA) or "
                    "billing profile (MCA) scope, not at {kind} scope.".format(kind=self.kind))
        return f"{self.label}: '{dataset}' is not available at {self.kind} scope."

    def __str__(self) -> str:
        return self.path


_GUID = re.compile(r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
                   r"[0-9a-fA-F]{4}-[0-9a-fA-F]{12}")

# Longest first: a billing profile path also matches the billing account prefix, so the more
# specific patterns have to be tried before the more general ones.
_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("invoiceSection", re.compile(
        rf"{BILLING}/([^/]+)/billingProfiles/([^/]+)/invoiceSections/([^/]+)", re.I)),
    ("billingProfile", re.compile(rf"{BILLING}/([^/]+)/billingProfiles/([^/]+)", re.I)),
    ("department", re.compile(rf"{BILLING}/([^/]+)/departments/([^/]+)", re.I)),
    ("enrollmentAccount", re.compile(rf"{BILLING}/([^/]+)/enrollmentAccounts/([^/]+)", re.I)),
    ("customer", re.compile(rf"{BILLING}/([^/]+)/customers/([^/]+)", re.I)),
    ("billingAccount", re.compile(rf"{BILLING}/([^/]+)", re.I)),
    ("managementGroup", re.compile(rf"{MG}/([^/]+)", re.I)),
    ("resourceGroup", re.compile(r"/subscriptions/([^/]+)/resourceGroups/([^/]+)", re.I)),
    ("subscription", re.compile(r"/subscriptions/([^/]+)", re.I)),
)


def _clean(value: str) -> str:
    return str(value or "").strip().strip("/")


def _normalise_agreement(value: str | None) -> str:
    return re.sub(r"[^a-z]", "", (value or "").lower())


def plan_scopes(subscriptions: Sequence[dict[str, Any]],
                billing_accounts: Sequence[dict[str, Any]] | None = None,
                ) -> dict[str, Any]:
    """Prefer supported billing exports; fetch uncovered subscriptions individually."""
    subs = [Scope.subscription(s["id"], display_name=s.get("name"))
            for s in subscriptions if s.get("id")]

    usable: list[Scope] = []
    covered: set[str] = set()
    rejected: list[dict[str, str]] = []
    for account in billing_accounts or []:
        name = account.get("name") or account.get("id")
        if not name:
            continue
        scope = Scope.parse(
            name if str(name).startswith("/") else f"{BILLING}/{name}",
            display_name=account.get("displayName"),
            agreement=account.get("agreementType"),
        )
        reason = scope.why_not(FOCUS)
        if reason is None:
            usable.append(scope)
            covered.update(
                _clean(sub["id"]).lower()
                for sub in account.get("subscriptions") or []
                if isinstance(sub, dict) and sub.get("id")
            )
        else:
            rejected.append({"scope": scope.path, "reason": reason})

    if not usable:
        return {
            "strategy": "subscription",
            "scopes": subs,
            "covered": [],
            "rejected": rejected,
            "calls_saved": 0,
            "note": ("No billing account can carry a FOCUS export, so each subscription is "
                     "fetched on its own. That is throttled per tenant and does not scale past "
                     "a few dozen subscriptions."
                     if subs else "Nothing to fetch."),
        }

    # Rejected accounts and undeclared relationships never suppress subscription fetches.
    leftovers = [s for s in subs if s.parts[0].lower() not in covered]

    return {
        "strategy": "billing" if not leftovers else "mixed",
        "scopes": usable + leftovers,
        "covered": sorted(covered),
        "rejected": rejected,
        # What the billing scope actually buys, in the unit that is throttled.
        "calls_saved": max(0, len(subs) - len(leftovers) - len(usable)),
        "note": (f"{len(usable)} billing scope(s) cover {len(covered)} subscription(s); "
                 f"{len(leftovers)} fetched individually."),
    }


# --------------------------------------------------------------------------- manifest
@dataclass(frozen=True)
class Blob:
    name: str
    bytes: int = 0
    rows: int = 0


@dataclass(frozen=True)
class Manifest:
    """One export run, as the run itself describes it.

    Everything here comes from `manifest.json`. Nothing is inferred from blob names or
    timestamps, because both change and neither is authoritative.
    """

    run_id: str
    export_name: str
    dataset: str
    timeframe: str
    start: str | None
    end: str | None
    submitted: str | None
    blobs: tuple[Blob, ...]
    row_count: int
    byte_count: int
    compressed: bool
    partitioned: bool
    overwrite: bool
    version: str | None

    @classmethod
    def parse(cls, raw: bytes | str | dict[str, Any]) -> Manifest:
        if isinstance(raw, (bytes, str)):
            data = json.loads(raw)
        else:
            data = raw
        if not isinstance(data, dict):
            raise ValueError("manifest is not a JSON object")

        run = data.get("runInfo") or {}
        cfg = data.get("exportConfig") or {}
        delivery = data.get("deliveryConfig") or {}

        blobs = []
        for b in data.get("blobs") or []:
            name = (b or {}).get("blobName") or (b or {}).get("name")
            if not name:
                continue
            blobs.append(Blob(name=str(name),
                              bytes=int(b.get("byteCount") or 0),
                              rows=int(b.get("dataRowCount") or 0)))
        if not blobs:
            raise ValueError("manifest lists no blobs")
        if len({b.name for b in blobs}) != len(blobs):
            raise ValueError("manifest repeats a blob")
        if data.get("blobCount") is not None and int(data["blobCount"]) != len(blobs):
            raise ValueError("manifest blobCount does not match its partition list")

        return cls(
            run_id=str(run.get("runId") or ""),
            export_name=str(cfg.get("exportName") or ""),
            dataset=str(cfg.get("type") or ""),
            timeframe=str(cfg.get("timeFrame") or cfg.get("timeframe") or ""),
            start=_iso(run.get("startDate")),
            end=_iso(run.get("endDate")),
            submitted=_iso(run.get("submittedTime")),
            blobs=tuple(blobs),
            row_count=int(data.get("dataRowCount") or sum(b.rows for b in blobs)),
            byte_count=int(data.get("byteCount") or sum(b.bytes for b in blobs)),
            compressed=bool(data.get("compressData")),
            partitioned=bool(delivery.get("partitionData")),
            overwrite=(str(delivery.get("dataOverwriteBehavior") or "")
                       .lower() == "overwritepreviousreport"),
            version=(str(data.get("manifestVersion")) if data.get("manifestVersion") else None),
        )


def _iso(value: Any) -> str | None:
    text = str(value or "").strip()
    return text or None


def latest_per_period(manifests: Iterable[Manifest]) -> list[Manifest]:
    """The newest run for each period covered, so history and the current month both load.

    A MonthToDate export and a TheLastMonth export are not alternatives: loading only one
    leaves either the current month or all the history before it missing. Keyed by period so
    both survive, while a re-run of the same period supersedes rather than duplicates.
    """
    best: dict[tuple, Manifest] = {}
    for m in manifests:
        # Retained MTD runs have advancing end dates but replace the same month's data.
        # Export definitions are isolated; callers must choose one, never sum alternatives.
        period = ((m.start or "")[:7] if m.timeframe in {"MonthToDate", "TheLastMonth"}
                  else (m.start, m.end))
        if not m.start or not m.end or not m.submitted:
            raise ValueError("Manifest lacks a covered period or submission time.")
        key = (m.export_name, m.dataset, period)
        current = best.get(key)
        if current is None or (m.submitted or "", m.run_id) > (current.submitted or "",
                                                               current.run_id):
            best[key] = m
    return sorted(best.values(), key=lambda m: (m.start or "", m.run_id))
