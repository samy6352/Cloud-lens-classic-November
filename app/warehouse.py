"""Cost warehouse: ingest Azure cost details once, then answer questions from PostgreSQL.

Why
---
The Cost Management Query API is slow (seconds per call) and aggressively throttled, which caps
how rich a question can be — every breakdown is another round trip, and anything it doesn't
support as a "dimension" simply cannot be asked.

Instead we pull row-level cost detail for every accessible subscription via
`generateCostDetailsReport`, normalise it into a FOCUS-shaped table, and keep it in Azure
Database for PostgreSQL. After that, questions are SQL against data we hold: milliseconds, no
throttling, arbitrary grouping, joins and window functions.

The report API takes minutes to produce a file, so ingest runs in the background and the app
stays usable throughout.
"""
from __future__ import annotations

import asyncio
import csv
import gzip
import io
import json
import logging
import math
import os
import re
import time
from collections.abc import AsyncIterator, Iterator, Sequence
from contextlib import asynccontextmanager, contextmanager
from datetime import UTC, date, datetime, timedelta
from typing import Any, ClassVar

import httpx

from . import derived, presence, rollup, schema_v2, store
from .sqldialect import SHIM_SCHEMA, strip_literals

log = logging.getLogger("cloudlens.warehouse")

ARM = "https://management.azure.com"
DETAILS_API = "2023-11-01"

# How long a single report submission may spend being throttled before giving up. Six retries
# with an escalating backoff could sleep for twelve minutes before the first poll, which made a
# doomed refresh look identical to a slow one for half an hour.
SUBMIT_BUDGET = 240.0

# Minimum gap between report submissions for the same subscription. Cost Management's limit is
# a handful per minute per subscription; five seconds keeps three months' worth comfortably
# inside it while adding only ten seconds of stagger to the whole run.
SUBMIT_GAP = 5.0

# How long a single model-authored query may run. Generous for anything the dashboard asks --
# the heaviest real query on the test estate is milliseconds -- and short enough that a runaway
# join gives a pooled connection back rather than holding it until someone notices.
QUERY_TIMEOUT_MS = int(os.getenv("COST_QUERY_TIMEOUT_MS", "30000"))

# The role a model-authored query is executed as.
#
# The text checks in `query()` decide what a statement is *allowed to say*. This decides what
# it is *able to reach*, and only the second one is a boundary. A word list cannot be, because
# PostgreSQL ships functions that take SQL as a string and run it -- `query_to_xml`,
# `table_to_xml` and their siblings -- and a schema qualifier inside a string literal is
# invisible to any scanner that blanks literals before looking, which this one must do to
# avoid tripping over an ordinary value containing the word `delete`.
#
# So the statement runs as a role that owns nothing and is granted nothing beyond the temp
# view carrying the caller's scope. Reaching past the search path then fails on privileges, in
# the server, whatever route it took to get there -- a qualified name, a function that
# evaluates one, or the next thing added to PostgreSQL after this was written.
#
# The view is read through its owner's rights, which is what makes this work: the role can see
# exactly the rows the view selects and cannot see the table it selects them from.
#
# Required, not preferred. If the role cannot be assumed, `query()` refuses to run the
# statement rather than running it with the application's own rights -- which on the default
# deployment are those of an Entra administrator of the server, so the fallback that used to
# be here turned a guard bypass from "one tenant's scope" into "every tenant's costs".
QUERY_ROLE = "cloudlens_query"


class StrandedWarehouseTables(RuntimeError):
    """Raised at startup when cost tables sit in the schema model queries may read.

    Fatal rather than a warning. The condition is that `SHIM_SCHEMA` holds relations, and
    that schema is on the search path model-authored SQL runs with -- so an unqualified
    `SELECT * FROM costs_rollup` reads every subscription's money with no scope filter.
    Starting anyway would serve a dashboard on top of a warehouse that is quietly readable
    past its own boundary, which is worse than not starting.
    """


class QueryRoleUnavailable(RuntimeError):
    """Raised when a model query cannot be confined to `QUERY_ROLE`, so it is not run.

    Deliberately not a `ValueError`. Everything `query()` rejects with a ValueError is
    something the model said and could say differently, and the agent hands those straight
    back for it to correct. This is not that: the statement was fine and the server is
    misconfigured. Reporting it as a query error would send the model round a retry loop it
    cannot win, and hide an operational fault behind what looks like an ordinary refusal.
    """


# ----------------------------------------------------------------- the quick path
#
# The Cost Details API used by `ingest` is asynchronous: submit a report, wait for Azure to
# build it, then download it. Generation alone is 40s+ per period, submissions are throttled per
# subscription, and three months across three subscriptions is nine of them — measured at ten to
# twenty minutes on this tenant, with one period timing out at the twenty-minute ceiling.
#
# The Query API answers the same question synchronously. Measured on this tenant: one month of
# one subscription, daily granularity, grouped five ways, returned 1,622 rows in 7.8 seconds
# complete — no continuation. That is the whole month at resource-and-day level.
#
# The cost of the trade is columns. A query returns the dimensions it was grouped by and nothing
# else, so quantities, unit prices, benefit attribution and tags are absent — those need the
# detail report or a FOCUS export. `quick_ingest` therefore fills what it can and leaves the
# rest empty rather than inventing it, and the caller is told which columns it did not fill.
QUERY_API = "2023-11-01"

# Cost Management throttles by *client type*, and a caller that does not name itself is filed
# under a shared "DefaultQuota" bucket along with every other anonymous client touching this
# tenant — the portal, the CLI, and whatever else happens to be running.
#
# That bucket is what has been refusing us. Measured against this tenant while a quick refresh
# was failing on all three subscriptions:
#
#   x-ms-ratelimit-remaining-microsoft.costmanagement-clienttype-requests: DefaultQuota:0
#   x-ms-ratelimit-microsoft.costmanagement-clienttype-retry-after:        9  (then 35)
#   x-ms-ratelimit-remaining-microsoft.costmanagement-entity-requests:     DefaultQuota:3
#   x-ms-ratelimit-remaining-microsoft.costmanagement-tenant-requests:     DefaultQuota:19
#   x-ms-ratelimit-microsoft.costmanagement-qpu-remaining:                 QueriesPer10Sec:11
#
# Only the client-type allowance was spent; our own entity, tenant and QPU allowances all had
# room. The identical request sent with this header was served immediately.
#
# So naming ourselves is not cosmetic. It is the difference between our own allowance and
# whatever is left of everyone else's — and it is why the "wait several minutes" advice this
# code used to print was wrong: we were not out of quota, we were in the wrong queue.
#
# Defined in cost.py, which is where request headers are assembled, so the live-query path and
# this one cannot drift apart. They did: only this module sent it, and the dashboard's fallback
# to live queries met the shared bucket on every tile.
from .cost import CLIENT_TYPE  # noqa: E402  (kept beside the reasoning it belongs to)

# Every retry-after header Cost Management may send, most specific first. The client-type one
# is listed first because it is the limit that actually fires, and it was missing here — so a
# 429 that came with precise guidance ("wait 9 seconds") was read as having none, and fell
# through to a synthetic ladder that guessed minutes. The service was answering the question
# and we were not reading the reply.
RETRY_HEADERS = (
    "x-ms-ratelimit-microsoft.costmanagement-clienttype-retry-after",
    "x-ms-ratelimit-microsoft.costmanagement-entity-retry-after",
    "Retry-After",
)

# The query throttle has more than one dimension: per subscription (entity), per tenant, per
# client type, and a QPU allowance. Firing three months of one subscription at once is what
# earns an entity 429, not the total rate, so queries queue per subscription exactly as report
# submissions do — otherwise the retry budget is spent losing a race the caller started with
# itself. `CLIENT_TYPE` above handles the other dimension; this handles this one.
QUERY_GAP = 1.5

# A quick refresh is a promise about *time*, not about completeness.
#
# The point of this path is a live answer now — "whatever can be fetched", against an API that
# returns in about six seconds when it is not throttled. Measured on this tenant: 6.1s for a
# month of a subscription, unthrottled.
#
# So the budget is the design. Waiting out a 429 is the right thing for a background job and
# exactly the wrong thing here: the previous backoff could sleep 161 seconds per subscription,
# which turned the fast option into the slowest one in the menu. Two short retries, then this
# subscription is reported as throttled and the rest carry on. Partial and immediate beats
# complete and late — that is the whole reason someone picked this option.
QUERY_BACKOFF = (2.0, 5.0)
QUICK_DEADLINE = float(os.getenv("QUICK_REFRESH_SECONDS", "75"))

# Five, because five is verified to work in one call. Each dimension multiplies the row count,
# and the Query API refuses a request whose result would be too large — so this is the set that
# buys the most schema for the least risk of that refusal.
#
# ServiceFamily earns its place over MeterCategory, which was here first and was a mistake: the
# whole cost-area breakdown — Compute & Web, AI & Data and the rest — is a GROUP BY on
# ServiceFamily. Leaving it empty did not merely lose a column, it silently moved every
# quick-loaded row into "Other services": on this estate AI & Data vanished entirely and Other
# went from $12 to $1,035. A column the dashboard classifies by is not optional.
QUERY_DIMENSIONS = [
    ("ResourceId", "ResourceId"),
    ("ServiceName", "ServiceName"),
    ("ServiceFamily", "ServiceFamily"),
    ("ResourceGroupName", "ResourceGroup"),
    ("ResourceLocation", "RegionName"),
]

# What a quick load cannot know. Named here so the UI can say so plainly rather than leaving
# someone to notice an empty Tags tab and assume the refresh failed.
QUICK_OMITS = ("PricingQuantity", "UnitPrice", "UnitOfMeasure", "MeterName", "ProductName",
               "ChargeCategory", "PricingModel", "BenefitName", "BenefitId", "PublisherName",
               "CostCenter", "Tags", "ServiceSubcategory", "ListCost", "ContractedCost",
               "ListUnitPrice", "ContractedUnitPrice", "PricingCurrency",
               "ExchangeRatePricingToBilling", "CommitmentDiscountType",
               "CommitmentDiscountStatus", "ChargeClass")

# FOCUS-aligned target schema. Azure's cost detail export uses its own column names, which also
# differ between EA / MCA / pay-as-you-go accounts, so we map many possible source names onto one
# stable set. Everything downstream — the agent, the SQL tool — only ever sees these.
#
# Verified against a real MCA export on 2026-08-21 (65 source columns). Note `resourceName` is
# NOT one of them, so ResourceName is derived from ResourceId after load.
COLUMNS: dict[str, list[str]] = {
    "ChargePeriodStart": ["date", "usagedate", "chargeperiodstart", "billingperiodstartdate"],
    "BilledCost": ["costinbillingcurrency", "cost", "pretaxcost", "billedcost"],
    "CostInUsd": ["costinusd", "xbilledcostinusd", "xeffectivecostinusd"],
    "BillingCurrency": ["billingcurrency", "billingcurrencycode", "currency"],
    "SubAccountId": ["subscriptionid", "subscriptionguid", "subaccountid"],
    "SubAccountName": ["subscriptionname", "subaccountname"],
    "ResourceGroup": ["resourcegroupname", "resourcegroup", "xresourcegroupname"],
    "ResourceId": ["resourceid", "instanceid"],
    "ResourceName": ["resourcename"],
    "ServiceName": ["metercategory", "consumedservice", "servicename"],
    "ServiceFamily": ["servicefamily", "xskuservicefamily"],
    # x_SkuMeterSubcategory is the true subcategory and is preferred where present. The
    # servicecategory fallback stays last because FOCUS ServiceCategory is the BROAD
    # grouping, so it is a lossy stand-in rather than a synonym.
    "ServiceSubcategory": ["metersubcategory", "xskumetersubcategory", "servicecategory"],
    "MeterName": ["metername", "xskumetername"],
    "ProductName": ["productname", "xskudescription"],
    "RegionName": ["resourcelocation", "location", "regionname", "meterregion"],
    "PricingQuantity": ["quantity", "consumedquantity", "pricingquantity"],
    "UnitOfMeasure": ["unitofmeasure", "pricingunit"],
    "UnitPrice": ["unitprice", "effectiveprice", "xeffectiveunitprice", "xbilledunitprice"],
    "ChargeCategory": ["chargetype", "chargecategory"],
    # Amortized and FOCUS exports name these differently from ActualCost, and the whole point
    # of switching metric is to get them — a missed alias here silently produces a column of
    # nulls, which reads as "no reservations" rather than "not mapped".
    "PricingModel": ["pricingmodel", "reservationtype", "commitmentdiscounttype"],
    "BenefitName": ["benefitname", "reservationname", "commitmentdiscountname"],
    "BenefitId": ["benefitid", "reservationid", "commitmentdiscountid"],
    "PublisherName": ["publishername", "publishertype"],
    "CostCenter": ["costcenter", "xcostcenter"],
    "Tags": ["tags"],
    # Append only: archived rows and older installations retain the original column order.
    "InvoiceCost": ["invoicecost"],
    "ListCost": ["listcost"],
    "ContractedCost": ["contractedcost"],
    "EffectiveCost": ["effectivecost"],
    "ListUnitPrice": ["listunitprice", "paygprice"],
    "ContractedUnitPrice": ["contractedunitprice"],
    "PricingCurrency": ["pricingcurrency", "xpricingcurrency"],
    "ExchangeRatePricingToBilling": ["exchangeratepricingtobilling", "xbillingexchangerate"],
    "CommitmentDiscountType": ["commitmentdiscounttype"],
    "CommitmentDiscountStatus": ["commitmentdiscountstatus"],
    "ChargeClass": ["chargeclass"],
    "BillingCostBasis": ["billingcostbasis"],
}

BILLING_COLUMNS = tuple(list(COLUMNS)[-12:])
BILLING_MONEY = {"InvoiceCost", "ListCost", "ContractedCost", "EffectiveCost"}
BILLING_UNIT_PRICES = {"ListUnitPrice", "ContractedUnitPrice"}
NULLABLE_NUMERIC = BILLING_MONEY | BILLING_UNIT_PRICES | {"ExchangeRatePricingToBilling"}
NUMERIC = {"BilledCost", "CostInUsd", "PricingQuantity", "UnitPrice"} | NULLABLE_NUMERIC

# Column name to position, built once. Several call sites need it and rebuilding a dict per row
# is the kind of thing that turns a fast path back into a slow one.
COLUMNS_INDEX = {c: i for i, c in enumerate(COLUMNS)}


def _nullable_number(value: Any) -> float | None:
    """Absent, malformed and non-finite money is unknown, never a fabricated zero."""
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _cost_basis(value: Any) -> str | None:
    return {"focus": "FOCUS", "focuscost": "FOCUS", "actualcost": "ActualCost",
            "amortizedcost": "AmortizedCost"}.get(str(value or "").strip().lower())


class _BillingMapping:
    """Financial provenance shared by CSV, Parquet and Cost Details ingestion.

    Totals are billing-currency amounts. FOCUS unit prices are also billing currency;
    legacy PayGPrice/UnitPrice are pricing currency. Never use the old UnitPrice target:
    its effectiveprice alias has already discarded the information needed to price list cost.
    """

    def __init__(self, header: list[str], metric: str | None = None) -> None:
        self.at = {h.strip().lower().replace(" ", "").replace("_", ""): i
                   for i, h in enumerate(header)}
        old_columns = {c.lower() for c in COLUMNS if c not in BILLING_COLUMNS}
        # Our snapshots look like FOCUS but BilledCost may be amortized. New snapshots
        # persist the explicit basis; old complete snapshots must remain unknown.
        self.saved = "billingcostbasis" in self.at or old_columns.issubset(self.at)
        self.basis = ("FOCUS" if not self.saved and "chargeperiodstart" in self.at
                      and ("billedcost" in self.at or "effectivecost" in self.at)
                      else _cost_basis(metric))

    def apply(self, rec: list[Any], row: Sequence[Any]) -> None:
        def value(*names: str) -> Any:
            for name in names:
                i = self.at.get(name)
                if i is not None and i < len(row):
                    v = row[i]
                    if v is not None and str(v).strip():
                        return v
            return None

        def number(*names: str) -> float | None:
            return _nullable_number(value(*names))

        fields: dict[str, Any] = {}
        for target in BILLING_COLUMNS:
            v = value(*COLUMNS[target])
            fields[target] = _nullable_number(v) if target in NULLABLE_NUMERIC else v
        basis = _cost_basis(value("billingcostbasis")) if self.saved else self.basis
        fields["BillingCostBasis"] = basis
        if self.saved:
            # Missing historical fields stay missing even when the archive path says "focus".
            for target, v in fields.items():
                rec[COLUMNS_INDEX[target]] = v
            return

        commitment = fields["CommitmentDiscountType"]
        pricing_model = str(value("pricingmodel", "xpricingmodel", "reservationtype") or "")
        normalized = re.sub(r"[\s_-]", "", pricing_model).lower()
        if not commitment:
            commitment = {"reservation": "Reservation", "reservedinstance": "Reservation",
                          "savingsplan": "SavingsPlan"}.get(normalized)
            benefit_id = str(value("benefitid") or "").lower()
            if not commitment and "/savingsplans/" in benefit_id:
                commitment = "SavingsPlan"
            elif not commitment and (value("reservationid") or "/reservations/" in benefit_id):
                commitment = "Reservation"
        fields["CommitmentDiscountType"] = commitment

        if basis == "FOCUS":
            fields["InvoiceCost"] = number("billedcost")
            fields["EffectiveCost"] = number("effectivecost")
            # FOCUS totals are authoritative. No quantity-derived replacement for a missing
            # FOCUS total: adjustment and purchase quantities do not describe priced usage.
        else:
            metric_cost = number("costinbillingcurrency", "cost", "pretaxcost")
            fields["InvoiceCost"] = metric_cost if basis == "ActualCost" else None
            fields["EffectiveCost"] = metric_cost if basis == "AmortizedCost" else None
            charge = str(value("chargetype", "chargecategory") or "").lower()
            if (basis == "ActualCost" and charge == "usage" and normalized == "ondemand"
                    and not commitment and not value("benefitid", "reservationid", "benefitname",
                                                     "reservationname", "commitmentdiscountid")):
                fields["EffectiveCost"] = metric_cost
            fields["ListUnitPrice"] = number("listunitprice", "paygprice")
            fields["ContractedUnitPrice"] = number("contractedunitprice", "unitprice")
            pricing_currency = str(fields["PricingCurrency"] or "").strip().upper()
            billing_currency = str(rec[COLUMNS_INDEX["BillingCurrency"]] or "").strip().upper()
            rate = fields["ExchangeRatePricingToBilling"]
            factor = (1.0 if pricing_currency and pricing_currency == billing_currency
                      else rate if pricing_currency and billing_currency and rate and rate > 0
                      else None)
            quantity = number("quantity", "consumedquantity", "pricingquantity")
            if charge == "usage" and quantity is not None and factor is not None:
                for total, unit in (("ListCost", "ListUnitPrice"),
                                    ("ContractedCost", "ContractedUnitPrice")):
                    price = fields[unit]
                    if fields[total] is None and price is not None:
                        fields[total] = _nullable_number(price * quantity * factor)
        for target, v in fields.items():
            rec[COLUMNS_INDEX[target]] = v


_CREDENTIAL: Any = None
_TOKEN: tuple[str, float] | None = None


def _token(url: str = "") -> str:
    """An ARM token for whoever this call is being made on behalf of.

    When the request is running as a signed-in person — delegated ARM, set up by `cost.act_as`
    — their token is used. Otherwise the app's own identity.

    That order is the whole point rather than a nicety. The app's identity can only ever read
    subscriptions somebody granted it a role on, which for a multi-tenant deployment is close to
    none of them: a user signs in from their own directory, the picker correctly lists their
    subscriptions, and every warehouse-backed tab is empty because the ingest that would have
    filled it ran as an identity with no access to their estate. The dashboard is then wrong in
    the most expensive way available — confidently, and about somebody else's money.

    Falling back to the app identity keeps single-tenant deployments and background jobs
    working, where there is no signed-in person to act as.
    """
    global _CREDENTIAL, _TOKEN

    try:
        from .cost import caller_token

        delegated = caller_token(url)
    except Exception:  # noqa: BLE001 - never let this be the reason an ingest cannot start
        delegated = None
    if delegated:
        return delegated

    if _TOKEN and _TOKEN[1] > time.time() + 120:
        return _TOKEN[0]

    if _CREDENTIAL is None:
        from azure.identity import DefaultAzureCredential

        from .identity import interactive_allowed

        _CREDENTIAL = DefaultAzureCredential(
            exclude_interactive_browser_credential=not interactive_allowed())

    try:
        tok = _CREDENTIAL.get_token(f"{ARM}/.default")
    except Exception as exc:  # noqa: BLE001
        raise RuntimeError(
            f"Could not get an Azure token: {exc}. Locally, run 'az login'. When deployed, check "
            "the app has a managed identity with Reader and Cost Management Reader."
        ) from exc

    _TOKEN = (tok.token, float(tok.expires_on))
    return tok.token


class Reader:
    """A borrowed read-only connection, handing back dicts rather than tuples.

    Deliberately minimal: it does not own the connection and cannot outlive the `reader()`
    block that produced it.
    """

    def __init__(self, con: Any, aggregate: str = "costs", counted: str = "costs",
                 named: str = "costs") -> None:
        self._con = con
        self.agg = aggregate
        self.res = counted
        self.named = named

    @property
    def aggregated(self) -> bool:
        """Whether `agg` and `res` are the derived tables rather than the detail table.

        Only for tests and diagnostics. A query must read the same either way, so
        nothing that builds a payload should branch on this.
        """
        return self.agg != "costs"

    def rows(self, sql: str, limit: int | None = 200) -> list[dict[str, Any]]:
        cur = self._con.execute(sql)
        columns = [d[0] for d in cur.description]
        rows = cur.fetchall() if limit is None else cur.fetchmany(limit)
        return [dict(zip(columns, row)) for row in rows]


# Whether a *new* `costs` is created partitioned by month. Off by default, and
# deliberately not a migration: `CREATE TABLE IF NOT EXISTS ... PARTITION BY` is
# a no-op against an existing flat table, so turning this on cannot convert, move
# or rewrite data that is already there. An existing deployment keeps the flat
# table it has until someone migrates it on purpose.
#
# Everything downstream reads the catalog rather than this flag -- see
# `_partitioned_target` -- so both shapes work whatever this is set to, and a
# database created under one setting still behaves correctly under the other.
PARTITION_COSTS = os.getenv("CLOUDLENS_PARTITION_COSTS", "").strip().lower() in {
    "1", "true", "yes", "on"}


def _costs_ddl(cols: str) -> str:
    """DDL for `costs`: partitioned by month when enabled, flat otherwise.

    The column list is identical either way. Partitioning changes where rows are
    stored, not what the table looks like to the 25 modules that read it.
    """
    if not PARTITION_COSTS:
        return f"CREATE TABLE IF NOT EXISTS costs (\n  {cols}\n)"
    return (f"CREATE TABLE IF NOT EXISTS costs (\n  {cols}\n) "
            f'PARTITION BY RANGE ("ChargePeriodStart")')


class Warehouse:
    @property
    def state(self) -> dict[str, Any]:
        from .jobs import current_progress
        progress = current_progress.get()
        return progress if progress is not None else self._state

    @state.setter
    def state(self, value: dict[str, Any]) -> None:
        from .jobs import current_progress
        progress = current_progress.get()
        if progress is None:
            self._state = value
        else:
            snapshot = dict(value)
            identity = {key: progress[key] for key in ("job_id", "started") if key in progress}
            progress.clear()
            progress.update(snapshot)
            progress.update(identity)

    def __init__(self) -> None:
        self._store = store.PostgresStore()
        log.info("warehouse: PostgreSQL at %s/%s", self._store.host, self._store.database)
        # Where the base table lives, once PostgreSQL has told us. The scope view is named
        # `costs` and shadows the table, so its definition cannot say `costs` -- it would
        # refer to itself. Hard-coding `public` looked equivalent and is not: it breaks under
        # any search_path the customer or a test has set.
        self._pg_table = "public.costs"
        self._rollup_schema = "public"
        self._lock = asyncio.Lock()
        # Report submissions are throttled per subscription, so they are queued per subscription.
        self._submit_locks: dict[str, asyncio.Lock] = {}
        self._last_submit: dict[str, float] = {}
        # The same pairing for the query path, which is throttled the same way.
        self._query_locks: dict[str, asyncio.Lock] = {}
        self._last_query: dict[str, float] = {}
        self._rebuilt = 0
        # Filled on first use by `_schema_alternation`, which needs a connection.
        self._schema_names: str | None = None
        # Whether `QUERY_ROLE` exists and may be assumed. Settled by `_ensure_query_role`
        # during schema init; None until then, False on a server where the application is
        # not permitted to create roles.
        self._query_role: bool | None = None
        self._init_schema()
        # Reflect what is already stored, so a restart doesn't report an empty warehouse.
        summary = self.summary()
        if self._rebuilt:
            self.state: dict[str, Any] = {
                "status": "empty",
                "detail": (f"{self._rebuilt:,} row(s) were discarded because the stored columns "
                           "no longer matched the schema and their values were shifted. "
                           "Refresh to reload."),
            }
        else:
            self.state = (
                {"status": "ready", **summary} if summary["rows"]
                else {"status": "empty", "detail": "no data ingested yet"}
            )

    # ------------------------------------------------------------------ rollup
    def refresh_rollup(self, *, strict: bool = False,
                       window: tuple[Any, Any] | None = None) -> int:
        """Rebuild the pre-aggregated table the dashboard reads.

        Called after every write. It is derived from the rows that were just written, so
        rebuilding it anywhere else would mean finding all the write paths again, and missing
        one would leave the dashboard showing yesterday's totals with no indication that it
        was doing so.

        `window` is an optional half-open `(lo, hi)` span of charge dates that narrows the
        rebuild to the months a write actually touched. It is an optimisation with a
        precondition, not a default: pass it only where the set of affected days is known
        for certain, because days outside it keep whatever the previous rebuild produced.
        Callers that cannot state that omit it and rebuild the lot, which is always correct.

        A window is *ignored* if the last rebuild did not succeed. That is the one way a
        narrow rebuild can go permanently wrong: a failed rebuild leaves days stale, and
        every later windowed rebuild would step over them rather than correct them, so the
        staleness would never age out. After a failure the next rebuild is full, whatever
        the caller asks for, and the flag is only cleared once one has succeeded.

        Source writes are frozen while derived data is rebuilt. Failed rebuilds retain the
        previous ready generation; ingest callers raise so recovery cannot publish stale
        aggregates as current. Synchronous maintenance retains its non-raising default.
        """
        if window is not None and getattr(self, "_rollup_full_required", True):
            log.info("warehouse: rebuilding the whole rollup rather than %s..%s, "
                     "because the last rebuild did not complete", window[0], window[1])
            window = None
        try:
            from . import derived, jobs
            with jobs.coherent_rollup(self) as con:
                # Both tables in one call, so their contents describe the same
                # generation of `costs` and the generation stamp `coherent_rollup`
                # writes covers both or neither -- and so the exclusive locks their
                # swaps need are taken together at the end, rather than one being
                # held for the duration of the other's build.
                counts = derived.rebuild(jobs.RollupTransaction(con), self._rollup_schema,
                                         (rollup, presence), self._pg_table, window)
            self._rollup_full_required = False
            return counts[rollup.TABLE]
        except Exception as exc:  # noqa: BLE001
            self._rollup_full_required = True
            log.warning("warehouse: could not rebuild the rollup (%s); "
                        "the previous ready generation is retained", exc)
            if strict:
                raise
            return 0

    async def refresh_rollup_async(self, window: tuple[Any, Any] | None = None) -> None:
        """`refresh_rollup` off the event loop — it aggregates the whole detail table."""
        await asyncio.to_thread(self.refresh_rollup, strict=True, window=window)

    # ------------------------------------------------------------------ schema
    def _columns_sql(self, rate: float | None, target: str) -> str:
        """The projection for a scoped or converted view of `costs`.

        PostgreSQL has no `* REPLACE (expr AS "BilledCost")`, so the column list is
        written out — which needs COLUMNS to be the truth about the table, and it is:
        `_init_schema` adds anything missing on every boot.
        """
        if rate is None:
            return ", ".join(f'"{name}"' for name in COLUMNS)
        billing_factor, pricing_factor = self._currency_sql(rate, target)
        parts = []
        for name in COLUMNS:
            if name == "BilledCost":
                parts.append(f'"BilledCost" * ({billing_factor}) AS "BilledCost"')
            elif name in BILLING_MONEY:
                parts.append(f'"{name}" * ({billing_factor}) AS "{name}"')
            elif name in BILLING_UNIT_PRICES:
                parts.append(f'"{name}" * ({pricing_factor}) AS "{name}"')
            elif name == "BillingCurrency":
                parts.append(f"'{target}' AS \"BillingCurrency\"")
            elif name == "PricingCurrency":
                parts.append(f"'{target}' AS \"PricingCurrency\"")
            elif name == "ExchangeRatePricingToBilling":
                parts.append(
                    f'CASE WHEN ({pricing_factor}) IS NOT NULL AND ({billing_factor}) '
                    'IS NOT NULL THEN 1.0 ELSE NULL END AS "ExchangeRatePricingToBilling"')
            else:
                parts.append(f'"{name}"')
        return ", ".join(parts)

    @staticmethod
    def _currency_sql(rate: float, target: str) -> tuple[str, str]:
        """Row-aware factors shared by selected cost and the independent billing money.

        Provider exchange rates take priority over current display FX. CostInUsd can be
        paired with the selected metric on legacy reports, but not on FOCUS: its old
        aliases can select EffectiveCostInUsd alongside BilledCost. Zero-invoice RI usage
        therefore never divides by InvoiceCost (or relies on a zero/zero ratio).
        """
        from . import currency as fx

        if not re.fullmatch(r"[A-Z]{3}", target) or not math.isfinite(rate) or rate <= 0:
            raise ValueError("invalid display currency or exchange rate")

        def current_factor(column: str) -> str:
            cases = []
            codes = set(fx.OFFERED) | set(fx._cache["rates"]) | set(fx._pinned()) | {target, "USD"}
            for code in sorted(codes):
                if not re.fullmatch(r"[A-Z]{3}", code):
                    continue
                known = fx.rate_to(code)
                if known is not None and math.isfinite(known) and known > 0:
                    cases.append(f"WHEN '{code}' THEN {rate / known}")
            return f'CASE upper(trim("{column}")) {" ".join(cases)} ELSE NULL END'

        billing = 'upper(trim("BillingCurrency"))'
        pricing = 'upper(trim("PricingCurrency"))'
        exchange = '"ExchangeRatePricingToBilling"'
        billed_fallback = current_factor("BillingCurrency")
        priced_fallback = current_factor("PricingCurrency")
        billing_factor = (
            f"CASE WHEN {billing} = '{target}' THEN 1.0 "
            f"WHEN {billing} = 'USD' THEN {rate} "
            f"WHEN {exchange} > 0 AND {pricing} = '{target}' THEN 1.0 / {exchange} "
            f"WHEN {exchange} > 0 AND {pricing} = 'USD' THEN {rate} / {exchange} "
            'WHEN ("BillingCostBasis" IN (\'ActualCost\', \'AmortizedCost\') '
            'OR "BillingCostBasis" IS NULL) '
            'AND "BilledCost" <> 0 AND "CostInUsd" / NULLIF("BilledCost", 0) > 0 '
            f'THEN "CostInUsd" / NULLIF("BilledCost", 0) * {rate} '
            f"WHEN {exchange} > 0 AND ({priced_fallback}) IS NOT NULL "
            f"THEN ({priced_fallback}) / {exchange} "
            f"ELSE {billed_fallback} END"
        )
        pricing_factor = (
            'CASE WHEN "BillingCostBasis" = \'FOCUS\' '
            f"OR ({pricing} <> '' AND {pricing} = {billing}) THEN ({billing_factor}) "
            f"WHEN {pricing} = '{target}' THEN 1.0 "
            f"WHEN {pricing} = 'USD' THEN {rate} "
            f"WHEN {exchange} > 0 THEN {exchange} * ({billing_factor}) "
            f"ELSE {priced_fallback} END"
        )
        return billing_factor, pricing_factor

    # The dimensions the filter bar offers, and the column each one narrows.
    #
    # An allow-list, not a pass-through: the key arrives from a query string and is used to build
    # a view definition, so it must never be anything but one of these. The *values* are bound
    # through a temp table and may be free text; the column name cannot be.
    #
    # Quoted from the same map the derived tables work their `FILTERABLE` sets out from, so
    # "which column does this filter name" has one answer. A second copy here would let a
    # derived table claim to serve a filter this applies to a different column.
    #
    # `env` is deliberately absent — Azure has no environment field, it is a convention people
    # implement with tags, so it is handled separately against `Tags`.
    FILTER_COLUMNS: ClassVar[dict[str, str]] = {
        key: f'"{column}"' for key, column in derived.FILTER_COLUMNS.items()
    }

    def _filter_sql(self, con: Any, filters: dict[str, list[str]] | None) -> str:
        """Extra predicates for the scope view, one per dimension the caller narrowed.

        Written into a temp table for the same reason the scope ids are: a view definition
        cannot carry parameters, and these values come from a query string. Concatenating them
        would put user text into SQL that then runs for every tab on the page.

        Each dimension is independent and each is a set, so `region in (eastus, westeurope)` and
        `service in (Storage)` narrow together. A dimension with nothing selected adds no
        predicate rather than an always-true one, so the planner still sees a clean query in the
        common case of no filters at all.
        """
        if not filters:
            return ""
        rows: list[tuple[str, str]] = []
        clauses: list[str] = []
        for key, values in filters.items():
            picked = [v.strip() for v in (values or []) if v and v.strip()]
            if not picked:
                continue
            if key == "env":
                # A tag, read from the JSON document rather than a column. Four spellings
                # because `env` and `environment` are both common and neither is consistently
                # cased — a filter that matched only one would silently report half an estate.
                #
                # TRY_CAST rather than a bare `::json`: `Tags` is stored as text and a plain
                # cast raises on any row that holds something that is not JSON, which would
                # make one malformed document break every filtered query.
                rows += [("env", v.lower()) for v in picked]
                clauses.append(
                    "lower(COALESCE("
                    "TRY_CAST(\"Tags\" AS JSON) #>> '{env}', "
                    "TRY_CAST(\"Tags\" AS JSON) #>> '{Env}', "
                    "TRY_CAST(\"Tags\" AS JSON) #>> '{environment}', "
                    "TRY_CAST(\"Tags\" AS JSON) #>> '{Environment}', '')) "
                    "IN (SELECT val FROM _filter WHERE dim = 'env')"
                )
                continue
            column = self.FILTER_COLUMNS.get(key)
            if not column:
                continue          # an unknown dimension narrows nothing rather than erroring
            rows += [(key, v.lower()) for v in picked]
            clauses.append(
                f"lower(COALESCE({column}, '')) IN "
                f"(SELECT val FROM _filter WHERE dim = '{key}')"
            )
        if not clauses:
            return ""
        con.execute("CREATE TEMP TABLE _filter (dim text, val text)")
        flat: list[str] = []
        for dim, val in rows:
            flat += [dim, val]
        placeholders = ", ".join("(?, ?)" for _ in rows)
        con.execute(f"INSERT INTO _filter (dim, val) VALUES {placeholders}", flat)
        return " AND ".join(clauses)

    def connect(self, read_only: bool = False,
                scope: list[str] | None = None,
                currency: str | None = None,
                filters: dict[str, list[str]] | None = None) -> Any:
        """A pooled PostgreSQL session, optionally restricted to a set of subscriptions.

        Scope is enforced by shadowing `costs` with a temp view holding only the selected
        rows, so a query physically cannot see anything outside the chosen subscriptions —
        the model forgetting a WHERE clause cannot produce a wrong-scope answer. `pg_temp`
        precedes `public` in the search path, so an unqualified `costs` resolves to the view;
        the base table is named explicitly inside it so the definition does not refer to
        itself.

        A view rather than a materialised copy because there is an index on `SubAccountId`:
        leaving the filter as a predicate lets the planner use it, where copying the matching
        rows first would read them all anyway and then write them out again.

        `currency` shadows the table the same way, replacing `BilledCost` with its converted
        value and `BillingCurrency` with the target. Doing it here rather than in each query is
        the difference between one change and twenty: `"BilledCost"` appears in about that many
        places across the dashboard, waste, tags and commitments modules, and every one of them
        would otherwise have to learn about exchange rates. They ask for money; this decides
        which money.

        `read_only` is the caller's statement of intent and nothing more. PostgreSQL sessions
        are independent and the server owns concurrency, so a reader needs no special handle
        while a write is in flight; the parameter is kept because it documents which call sites
        are reads.

        The store drops temp objects when the session goes back to the pool. That is not
        housekeeping — a leftover `costs` view carrying one user's scope would silently answer
        the next user's questions.
        """
        con = self._store.connect()
        try:
            return self._prepare_connection(con, read_only, scope, currency, filters)
        except BaseException:
            try:
                con.close()
            except Exception:
                log.exception("warehouse: failed to close an incompletely initialized connection")
            raise

    def _prepare_connection(self, con: Any, read_only: bool,
                            scope: list[str] | None, currency: str | None,
                            filters: dict[str, list[str]] | None) -> Any:
        # Before the early exit below, because an unscoped reader is still a reader: it
        # runs the same dashboard queries against the same tables and can be abandoned the
        # same way. See `PostgresStore.statement_timeout` for why the bound has to be the
        # server's rather than the caller's.
        if read_only and self._store.statement_timeout:
            con.execute("SELECT set_config('statement_timeout', ?, false)",
                        [self._store.statement_timeout])

        target = (currency or "").upper()
        rate = None
        if target:
            from . import currency as fx

            rate = fx.rate_to(target)

        from . import reporting

        period_filter = reporting.view_predicate() if read_only else ""
        if not scope and rate is None and not filters and not period_filter:
            return con

        select = self._columns_sql(rate, target)
        where = ""
        if scope:
            # The scope values go into a temp table rather than into the view definition,
            # because a view definition cannot contain parameters at all -- PostgreSQL has
            # nothing to infer their type from and refuses with "could not determine data type
            # of parameter $1". Writing them into a table keeps them bound rather than
            # concatenated into SQL, which is the property that matters: these values decide
            # what a user is allowed to see.
            #
            # Compared in canonical form as well as verbatim, so a caller passing an ARM path,
            # a differently cased GUID, or the plain GUID all select the same rows.
            ids = [canonical_sub_id(s) or "" for s in scope] + list(scope)
            values = ", ".join("(?)" for _ in ids)
            con.execute("CREATE TEMP TABLE _scope (id text)")
            con.execute(f"INSERT INTO _scope (id) VALUES {values}", ids)
            where = 'WHERE (lower("SubAccountId") IN (SELECT id FROM _scope))'

        # The filter bar narrows the same view the scope does, so every tab, the export, the
        # report and the agent all inherit it without any of them knowing it exists. Doing it
        # per query module instead would mean teaching two dozen files about six dimensions,
        # and the first one anybody forgot would quietly report unfiltered money.
        narrowed = self._filter_sql(con, filters)
        if narrowed:
            where = f"{where} AND {narrowed}" if where else f"WHERE {narrowed}"
        if period_filter:
            where = f"{where} AND {period_filter}" if where else f"WHERE {period_filter}"

        if rate is not None:
            billing_factor, _ = self._currency_sql(rate, target)
            missing = f'({billing_factor}) IS NULL AND "BilledCost" <> 0'
            check_where = f"{where} AND ({missing})" if where else f"WHERE {missing}"
            unknown = con.execute(
                f'SELECT DISTINCT "BillingCurrency" FROM {self._pg_table} {check_where} LIMIT 1'
            ).fetchone()
            if unknown:
                raise ValueError(
                    f"Cannot convert {unknown[0] or 'unknown billing currency'} to {target}: "
                    "no authoritative exchange rate is available. Select as-billed currency "
                    "or configure FX rates; no partial total will be reported."
                )
        con.execute(f"CREATE TEMP VIEW costs AS SELECT {select} FROM {self._pg_table} {where}")
        self._shadow_rollup(con, where, rate, filters)
        self._shadow_presence(con, where, rate, target, filters)
        return con

    def _shadow_presence(self, con: Any, where: str, rate: float | None, target: str,
                         filters: dict[str, list[str]] | None) -> None:
        """Give this connection a `resource_presence` carrying the same scope as `costs`.

        The same argument as `_shadow_rollup`: the table is not scope-filtered, so a
        scoped connection reading it unshadowed would count every subscription's
        resources rather than its own.

        Unlike the rollup, the *counting* survives a converted currency. The only
        dimension conversion touches is `BillingCurrency`, which the `costs` view
        replaces with the target for every row, and which is reproduced here by
        projecting the same literal; relabelling which currency a resource was billed
        in does not change how many resources there were.

        The *money* does not survive it, and is nulled rather than projected. The
        factor that converts a row depends on `PricingCurrency` and
        `ExchangeRatePricingToBilling`, neither of which is in the grain, so a summed
        figure here cannot be converted after the fact. `_derived_sources` already
        routes per-resource money to `costs` in that case, so nothing should read
        these columns -- and if something does, it reads NULL rather than a number
        that is silently in the wrong currency.
        """
        if presence.serves_filters(filters) and self._rollup_fresh(con):
            money_ok = presence.serves_money(rate)
            columns = ", ".join(
                [f"'{target}' AS \"BillingCurrency\""
                 if name == "BillingCurrency" and rate is not None else f'"{name}"'
                 for name in presence.DIMENSIONS]
                + [f'"{m}"' if money_ok else f'CAST(NULL AS double precision) AS "{m}"'
                   for m in presence.MEASURES])
            con.execute(f'CREATE TEMP VIEW {presence.TABLE} AS SELECT {columns} '
                        f'FROM "{self._rollup_schema}".{presence.TABLE} {where}')
            return
        # A filter on `charge`, `pricing` or `env` names something outside the grain,
        # so the counts have to come from the detail table the filter can be applied to.
        con.execute(f"CREATE TEMP VIEW {presence.TABLE} AS "
                    f"{presence.select_sql('costs')}")

    def _rollup_fresh(self, con: Any) -> bool:
        """Whether the rollup was rebuilt from the generation `costs` is now at.

        `jobs` advances the source generation inside the writing transaction and
        stamps the rollup's own generation inside the rebuilding one, so the two
        agreeing is a durable statement that the rollup is not stale -- unlike the
        in-process `_rollup_full_required` flag, which says nothing after a restart.

        Guarded, and answers False on any failure. The cost of being wrong here is
        a wrong total on a page someone sends to a finance team, so the unknown
        case has to fall back to the detail table rather than assume the best.
        """
        try:
            from . import jobs

            return jobs.rollup_current(con)
        except Exception as exc:  # noqa: BLE001
            log.debug("warehouse: could not establish rollup freshness (%s)", str(exc)[:200])
            return False

    def _shadow_rollup(self, con: Any, where: str, rate: float | None,
                       filters: dict[str, list[str]] | None) -> None:
        """Give this connection a `costs_rollup` carrying the same scope as `costs`.

        The rollup table is not scope-filtered -- an unqualified `SELECT * FROM
        costs_rollup` is every subscription's money -- so a scoped connection must
        shadow it exactly as it shadows `costs`, or routing a dashboard query to it
        would quietly widen what that query can see.

        Two backings, one shape. The rollup table itself when it can answer the
        connection exactly, and the same grain computed from the already-scoped
        `costs` view when it cannot. A query reads `costs_rollup` either way and
        cannot tell which answered it; only the speed differs.

        The fast path reuses `where` verbatim, which is sound because every column
        it can mention -- `SubAccountId` from the scope, `ResourceGroup`,
        `ServiceName`, `RegionName`, `ChargeCategory` and `PricingModel` from the
        filter bar, `ChargePeriodStart` from the reporting period -- is part of the
        rollup's grain. `serves_filters` is what guarantees that, and it is derived
        from the grain rather than written out, so the two cannot drift.
        """
        if rate is None and rollup.serves_filters(filters) and self._rollup_fresh(con):
            con.execute(f'CREATE TEMP VIEW {rollup.TABLE} AS SELECT * '
                        f'FROM "{self._rollup_schema}".{rollup.TABLE} {where}')
            return
        # A converted currency needs PricingCurrency, ExchangeRatePricingToBilling and
        # BillingCostBasis, and `env` names the Tags document. None of those survive
        # into the grain, so the rollup cannot answer these exactly and the detail
        # table has to.
        con.execute(f"CREATE TEMP VIEW {rollup.TABLE} AS {rollup.derive_sql('costs')}")

    @contextmanager
    def reader(self, scope: list[str] | None = None,
               currency: str | None = None,
               filters: dict[str, list[str]] | None = None,
               budget_ms: int | None = None) -> Iterator[Reader]:
        """One read-only connection reused across a burst of internal queries.

        Opening a connection is the expensive part — and under a scope it also rebuilds the
        shadow table. A dashboard tab asks seven questions; doing that on seven connections
        made a scoped tab take 849 ms, of which the queries themselves were a small fraction.

        For app-authored SQL only. Model-authored SQL still goes through `query()`, which is
        where the single-statement, no-file-access and currency checks live.

        `Reader.agg` names the relation aggregate queries should read, `Reader.res`
        the one resource counts should read, and `Reader.named` the one per-resource
        money should read. Each is the derived table whenever that can answer this
        connection exactly, and `costs` otherwise, so a caller writes one query and
        gets the fast path when the fast path is correct.

        `budget_ms` is for a caller that would rather fail early than hold a connection.
        The pool is small, so a query that is going to exceed the server's bound anyway
        spends that whole time occupying a slot every other request is queueing for --
        which is how one slow secondary control took the whole dashboard down with it.
        It can only ever *tighten* the bound: `LEAST` against the current setting means a
        caller cannot award itself more time than the server allows, and `NULLIF` treats
        an unlimited server bound as no competitor rather than as zero.
        """
        con = self.connect(read_only=True, scope=scope, currency=currency, filters=filters)
        try:
            if budget_ms and budget_ms > 0:
                con.execute(
                    "SELECT set_config('statement_timeout', (EXTRACT(EPOCH FROM LEAST("
                    "NULLIF(current_setting('statement_timeout'), '0')::interval, "
                    "make_interval(secs => ? / 1000.0))) * 1000)::bigint::text, false)",
                    [budget_ms])
            aggregate, counted, named = self._derived_sources(con, scope, currency, filters)
            yield Reader(con, aggregate, counted, named)
        finally:
            con.close()

    def relations(self, con: Any, scope: list[str] | None = None,
                  currency: str | None = None,
                  filters: dict[str, list[str]] | None = None) -> tuple[str, str, str]:
        """The same routing decision `reader()` makes, for a caller holding a connection.

        Not every module can use `reader()`. `anomalies` opens its connection itself
        and passes it to helpers that read tuples rather than dicts, and converting
        it wholesale would be a large change to working code for no benefit. But it
        asks the same questions, and it should get the same answer: this is the one
        place that decision lives, and a second copy of it would be a second thing
        to get wrong.

        Pass the same `scope`, `currency` and `filters` the connection was opened
        with. Passing anything else names a relation carrying a different scope from
        the one the connection can see, which is the one mistake here that is silent.
        """
        return self._derived_sources(con, scope, currency, filters)

    def _derived_sources(self, con: Any, scope: list[str] | None,
                         currency: str | None,
                         filters: dict[str, list[str]] | None) -> tuple[str, str, str]:
        """Which relations `Reader.agg`, `Reader.res` and `Reader.named` should name.

        A connection that `_prepare_connection` shadowed has both views, and both are
        correct by construction -- either the derived table, chosen only where it can
        answer exactly, or the grain computed from the scoped `costs` view. So the
        shadowed case needs no further test.

        The unshadowed case is the bare connection returned by the early exit: nothing
        is scoped, converted or filtered, so the names resolve to the derived tables
        themselves. That is the one case where staleness could show, and so the one
        case that has to check.

        `named` splits off from `res` for one reason: a converted currency leaves the
        presence table able to count but not to price, because the conversion factor
        needs columns outside its grain. Counting keeps the fast path; the money for
        that connection goes to the detail table, which is the only place it is right.
        """
        from . import reporting

        shadowed = bool(scope or currency or filters or reporting.view_predicate())
        if not (shadowed or self._rollup_fresh(con)):
            log.debug("warehouse: derived tables are behind the detail table; reading costs")
            return "costs", "costs", "costs"

        money_ok = presence.serves_money(self._display_rate(currency))
        return rollup.TABLE, presence.TABLE, presence.TABLE if money_ok else "costs"

    @staticmethod
    def _display_rate(currency: str | None) -> float | None:
        """The FX rate `_prepare_connection` would have applied, or None for as-billed.

        Asked again rather than threaded through, because `reader` is handed the
        caller's currency and not the rate derived from it. Guarded: a rate that
        cannot be established is treated as a conversion, which routes money to the
        detail table -- slower, and correct.
        """
        target = (currency or "").upper()
        if not target:
            return None
        try:
            from . import currency as fx

            rate = fx.rate_to(target)
        except Exception as exc:  # noqa: BLE001
            log.debug("warehouse: could not establish a display rate (%s)", str(exc)[:200])
            return 1.0
        return 1.0 if rate is None else rate

    def check_readiness(self) -> bool:
        """Validate the schema used by dashboard reads without reading customer rows."""
        return self._store.check_readiness({
            "costs": tuple(COLUMNS),
            rollup.TABLE: (*rollup.DIMENSIONS, *rollup.MEASURES, "charges"),
            presence.TABLE: (*presence.DIMENSIONS, *presence.MEASURES),
            "ingest_log": ("subscription_id", "subscription_name", "period", "rows",
                           "ingested_at", "status", "detail"),
            "meta": ("key", "value", "updated_at"),
            "access_log": ("user_name", "email", "source", "is_admin", "tab", "at"),
        })

    def _init_schema(self) -> None:
        from . import derived

        cols = ",\n  ".join(
            f'"{c}" {"DOUBLE" if c in NUMERIC else ("DATE" if c == "ChargePeriodStart" else "VARCHAR")}'
            for c in COLUMNS
        )
        with self.connect() as con:
            # Whichever schema CREATE TABLE will actually land in, which is the one the
            # scope view has to name explicitly and the one the column check below must
            # be restricted to.
            schema = con.execute("SELECT current_schema()").fetchone()[0]
            self._pg_table = f'"{schema}".costs'
            # Before anything is created, because a table stranded in the shim schema has to
            # be moved rather than shadowed by a new empty one of the same name.
            self._evacuate_shim_schema(con, schema)
            con.execute(_costs_ddl(cols))
            # Conditioned on what the table actually is, not on the flag. With the
            # flag on against a database whose `costs` predates partitioning, the
            # CREATE above is a no-op and the table is still flat -- and
            # `PARTITION OF` against a flat table raises.
            if _partitioned_target(con, "costs") is not None:
                # Without this, a charge whose month has no partition is rejected
                # outright and the row is lost. With it the charge is stored and
                # merely queried more slowly, which is the right way round.
                con.execute("CREATE TABLE IF NOT EXISTS costs_default "
                            "PARTITION OF costs DEFAULT")
            con.execute("CREATE TABLE IF NOT EXISTS ingest_log ("
                        "subscription_id VARCHAR, subscription_name VARCHAR, period VARCHAR, "
                        "rows BIGINT, ingested_at TIMESTAMP, status VARCHAR, detail VARCHAR)")
            # Who looked at what, and when. Not cost data at all -- an operational record, so it
            # lives beside ingest_log and meta rather than costs, and nothing here is ever
            # counted, filtered or exported as spend.
            con.execute("CREATE TABLE IF NOT EXISTS access_log ("
                        "user_name VARCHAR, email VARCHAR, source VARCHAR, is_admin BOOLEAN, "
                        "tab VARCHAR, at TIMESTAMP)")
            # Small facts about the deployment rather than about the spend: whether first-run
            # setup has been settled, and when. It lives here rather than in a file because
            # this database is the thing that already survives a restart -- on App Service the
            # filesystem does not.
            con.execute("CREATE TABLE IF NOT EXISTS meta ("
                        "key VARCHAR PRIMARY KEY, value VARCHAR, updated_at TIMESTAMP)")

            # CREATE TABLE IF NOT EXISTS only builds the schema on an empty database, so a
            # warehouse that predates a new column keeps the old shape and every query naming
            # that column fails. Add anything missing rather than requiring a re-ingest —
            # existing rows get NULL, which is honest: that data genuinely was not collected.
            #
            # Restricted to the schema the table was actually created in. Without that clause
            # this counts the columns of *every* table named `costs` the role can see, and on
            # PostgreSQL that is not hypothetical -- one stray `public.costs` made the check
            # read fifty columns instead of twenty-five, conclude the stored order was wrong,
            # and take its remedy: drop the table and rebuild it. Silent, total data loss on
            # every boot, in a database the customer shares with anything else.
            order = [
                r[0] for r in con.execute(
                    "SELECT column_name FROM information_schema.columns "
                    "WHERE table_name = 'costs' AND table_schema = ? "
                    "ORDER BY ordinal_position",
                    [schema],
                ).fetchall()
            ]
            existing = set(order)
            for name in COLUMNS:
                if name in existing:
                    continue
                kind = ("DOUBLE" if name in NUMERIC
                        else "DATE" if name == "ChargePeriodStart" else "VARCHAR")
                con.execute(f'ALTER TABLE costs ADD COLUMN "{name}" {kind}')
                order.append(name)
                log.info("warehouse: added missing column %s", name)

            # Physical order is not evidence of corrupt data. Named inserts/projections work
            # with an appended or reordered schema; destroying it also destroys irreplaceable
            # historical price provenance. Any genuinely shifted legacy data needs an explicit
            # operator repair, not a DROP TABLE on every application boot.
            if order != list(COLUMNS):
                log.warning(
                    "warehouse: retaining costs with a different physical column order; "
                    "reads and inserts use explicit column names"
                )

            self._merge_subscription_spellings(con)
            self._create_indexes(con)
            self._drop_outdated_derived(con, schema)
            # The published names are views over one of two sides; `ensure` creates
            # or upgrades that arrangement. See `derived` for why the indirection
            # exists -- in short, it is what lets a rebuild swap the contents while
            # a dashboard is holding a scoped view of them.
            derived.ensure(con, schema, rollup)
            derived.ensure(con, schema, presence)
            self._rollup_schema = schema
            self._ensure_query_role(con)

    def _drop_outdated_derived(self, con: Any, schema: str) -> None:
        """Drop a derived table whose columns no longer match the grain that defines it.

        `CREATE TABLE IF NOT EXISTS` is a no-op against a table that already exists, so
        changing `rollup.DIMENSIONS` or `presence.DIMENSIONS` would otherwise leave the
        old shape in place and every query naming the new column would fail.

        Dropping is safe here in a way it would never be for `costs`: both of these are
        derived, hold nothing that is not recomputable from the detail table, and are
        rebuilt by the refresh that follows. The generation stamp is what stops an empty
        one being read in the meantime -- a table that has not been rebuilt is behind the
        detail table by definition, so `_rollup_fresh` refuses it and reads `costs`.

        Both sides go, and the view with them. The shape is wrong on whichever side is
        published, and the retired side is older still; leaving either would mean the
        next boot found a stale shape and rebuilt again. `CASCADE` because the published
        name is a view over the side being dropped, and dropping it is the point here
        rather than something to be preserved.
        """
        from . import derived

        for module in (rollup, presence):
            expected = set(module.DIMENSIONS) | set(getattr(module, "MEASURES", []))
            if module is rollup:
                expected.add("charges")
            active = derived.active_table(con, schema, module) or module.TABLE
            found = {row[0] for row in con.execute(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_schema = ? AND table_name = ?",
                [schema, active]).fetchall()}
            if found and found != expected:
                log.info("warehouse: rebuilding %s; its columns no longer match the grain "
                         "(missing %s, extra %s)", module.TABLE,
                         sorted(expected - found) or "none", sorted(found - expected) or "none")
                con.execute(f'DROP VIEW IF EXISTS "{schema}".{module.TABLE}')
                for suffix in derived.SIDES:
                    con.execute(
                        f'DROP TABLE IF EXISTS "{schema}".{module.TABLE}{suffix} CASCADE')
                con.execute(f'DROP TABLE IF EXISTS "{schema}".{module.TABLE} CASCADE')

    # The command that moves a relation between schemas depends on what it is; ALTER TABLE
    # accepts some of these but not all, and errors rather than doing the right thing.
    _RELOCATE: ClassVar[dict[str, str]] = {
        "r": "TABLE", "p": "TABLE", "f": "FOREIGN TABLE",
        "v": "VIEW", "m": "MATERIALIZED VIEW", "S": "SEQUENCE"}

    def _evacuate_shim_schema(self, con: Any, schema: str) -> None:
        """Move warehouse relations that earlier versions created inside `SHIM_SCHEMA`.

        `PostgresStore._open` used to put the shim schema first on the search path, so every
        unqualified CREATE TABLE the warehouse issued landed in it -- costs, costs_rollup,
        ingest_log and meta, on any deployment without PGSCHEMA set. Putting the path back in
        order fixes the next database to be created and does nothing at all for the ones that
        already have the tables in the wrong place, which are exactly the affected ones.

        Leaving them there is not cosmetic. Model-authored SQL runs with `search_path =
        pg_temp, cloudlens_fn, pg_catalog`, so an unqualified `SELECT * FROM costs_rollup`
        resolves to a table carrying every subscription's money with no scope filter of its
        own -- past the temp view that exists to prevent precisely that. And `_init_schema`
        would go on to create a second, empty `costs` in the real schema, so the app would
        report an empty warehouse while the rows it should be serving sat in the one schema
        the model can read.

        A clash in the target schema is moved aside rather than resolved. Choosing between two
        tables of the same name means guessing which one the deployment means, and dropping
        either is unrecoverable; `public` is off the model's search path, so parking it there
        under another name ends the disclosure without destroying anything.
        """
        if schema == SHIM_SCHEMA:
            raise StrandedWarehouseTables(
                f"The warehouse's own schema is {SHIM_SCHEMA}, which is the schema model "
                "queries are allowed to read. There is nowhere to move the cost tables to, "
                "so they would be readable without a scope filter. Set PGSCHEMA to a schema "
                f"other than {SHIM_SCHEMA}, or restore the `public` schema."
            )
        stranded = con.execute(
            "SELECT c.relname, c.relkind FROM pg_class c "
            "JOIN pg_namespace n ON n.oid = c.relnamespace "
            "WHERE n.nspname = ? AND c.relkind = ANY(?) ORDER BY c.relname",
            [SHIM_SCHEMA, list(self._RELOCATE)],
        ).fetchall()
        if not stranded:
            return

        # Only the ones with no owning table: a sequence behind an identity or serial column
        # moves with its table, and naming it separately would fail on a name that no longer
        # exists by the time we get to it.
        owned = {r[0] for r in con.execute(
            "SELECT s.relname FROM pg_class s "
            "JOIN pg_namespace n ON n.oid = s.relnamespace "
            "JOIN pg_depend d ON d.objid = s.oid AND d.classid = 'pg_class'::regclass "
            "WHERE n.nspname = ? AND s.relkind = 'S' AND d.deptype IN ('a', 'i')",
            [SHIM_SCHEMA],
        ).fetchall()}

        log.warning(
            "warehouse: %d relation(s) are in %s, where an earlier version created them: %s. "
            "Model queries can read that schema, so they are being moved into %s.",
            len(stranded), SHIM_SCHEMA, ", ".join(r[0] for r in stranded), schema,
        )
        stamp = datetime.now(UTC).strftime("%Y%m%d%H%M%S")
        for name, kind in stranded:
            if kind == "S" and name in owned:
                continue
            keyword = self._RELOCATE[kind]
            target = name
            if self._relation_exists(con, schema, name):
                target = f"{name}__stranded_{stamp}"
                log.warning(
                    "warehouse: %s.%s cannot move to %s because a relation of that name is "
                    "already there. Moving it aside as %s.%s instead -- nothing is dropped, "
                    "but one of the two holds data this deployment is no longer reading, and "
                    "which one is a question only you can answer.",
                    SHIM_SCHEMA, name, schema, schema, target,
                )
            try:
                # Renamed first, in the schema it is still in. `SET SCHEMA` moves the name
                # along with the relation, so on a clash it is the move that fails -- there
                # is no moment at which the new name is free in the target.
                if target != name:
                    con.execute(f'ALTER {keyword} "{SHIM_SCHEMA}"."{name}" RENAME TO "{target}"')
                con.execute(f'ALTER {keyword} "{SHIM_SCHEMA}"."{target}" SET SCHEMA "{schema}"')
            except Exception as exc:  # noqa: BLE001 - see the docstring
                # Fatal, deliberately. Carrying on means starting an application whose cost
                # tables are readable by model-authored SQL without a scope filter, which is
                # the fault this is here to clear.
                raise StrandedWarehouseTables(
                    f"Could not move {SHIM_SCHEMA}.{name} into {schema} ({exc}). Until it "
                    f"moves, model-authored queries can read it without a scope filter, so "
                    f"the application is refusing to start. Move it by hand with "
                    f'ALTER {keyword} "{SHIM_SCHEMA}"."{name}" SET SCHEMA "{schema}", or '
                    f"grant the application ownership of it."
                ) from exc

    def _relation_exists(self, con: Any, schema: str, name: str) -> bool:
        return bool(con.execute(
            "SELECT 1 FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace "
            "WHERE n.nspname = ? AND c.relname = ?",
            [schema, name],
        ).fetchone())

    def _ensure_query_role(self, con: Any) -> None:
        """Make sure model-authored queries can be confined to a role that owns nothing.

        Creating the role is best effort. Being able to *assume* it is not: that is what the
        check at the end settles, by asking the server rather than inferring it.

        The distinction is the bug this fixes. The previous version concluded that privilege
        separation was in place whenever `CREATE ROLE` did not raise, which is not the same
        question. A role created but never granted to the role the application connects as --
        or granted to a different one, on a server where the application's identity changed --
        recorded success, and then every model query ran with the application's own rights.
        Since the default deployment connects as an Entra administrator, that is the whole
        estate rather than the caller's scope.

        A deployment that has demoted the application, which `infra/sql/create-app-role.sql`
        does, cannot create roles at all. That is expected and fine: the same script creates
        this role and grants it, so the create below fails and the check still passes.

        `NOLOGIN` because nothing ever connects as it; it is only ever assumed with SET ROLE by
        a session that has already authenticated as the application.
        """
        try:
            con.execute(
                f"""
                DO $$
                BEGIN
                    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '{QUERY_ROLE}') THEN
                        CREATE ROLE {QUERY_ROLE} NOLOGIN;
                    END IF;
                END
                $$
                """
            )
            # Granted to whoever the application connects as, because SET ROLE is only
            # permitted to a role you are a member of. Re-granting is a no-op.
            con.execute(f"GRANT {QUERY_ROLE} TO CURRENT_USER")
        except Exception as exc:  # noqa: BLE001 - the check below is what decides
            # Not a failure in itself, and not worth a warning: the hardened deployment can do
            # neither of these and does not need to. The connection is autocommit, so this
            # leaves nothing broken behind it.
            log.debug("warehouse: could not create or grant %s (%s); checking whether it is "
                      "usable anyway", QUERY_ROLE, exc)

        self._query_role = self._can_assume_query_role(con)
        if not self._query_role:
            # Loud, because the consequence is not local: `query()` now refuses to run model
            # SQL at all rather than running it unconfined, so this is the reason the assistant
            # will stop answering questions about cost data.
            log.error(
                "warehouse: cannot assume the %s role, so model-authored queries will be "
                "refused. The text checks in query() are a filter on text and were never "
                "meant to be the only boundary. Run infra/sql/create-app-role.sql, or grant "
                "the application CREATEROLE, to restore privilege separation.",
                QUERY_ROLE,
            )

    def _can_assume_query_role(self, con: Any) -> bool:
        """Whether this connection could actually `SET ROLE` to `QUERY_ROLE`.

        `pg_has_role` errors rather than returning false when the role does not exist, so
        existence is tested first. CASE is used rather than AND because PostgreSQL does not
        promise to short-circuit a boolean operator, and may evaluate the call anyway.
        """
        try:
            row = con.execute(
                f"""
                SELECT CASE
                    WHEN EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '{QUERY_ROLE}')
                    THEN pg_has_role(current_user, '{QUERY_ROLE}', 'USAGE')
                    ELSE false
                END
                """
            ).fetchone()
        except Exception as exc:  # noqa: BLE001 - an unanswerable question is a no
            log.warning("warehouse: could not determine whether %s can be assumed (%s)",
                        QUERY_ROLE, exc)
            return False
        return bool(row and row[0])

    def identity_check(self) -> dict[str, Any]:
        """Is every subscription id in the table stored in its canonical form?

        Azure spells a subscription two ways -- FOCUS exports use the ARM path
        `/subscriptions/<guid>`, the Query API and Cost Details use the bare GUID -- and both
        used to be stored verbatim. One subscription then existed twice, and because every
        write replaces its own range with `WHERE "SubAccountId" = ?`, neither loader deleted
        the other's rows. Both copies were summed. A month Azure billed at $476.07 was reported
        as $920.98, and nothing anywhere said so.

        `canonical_sub_id` closed that at every write. This checks the *invariant* rather than
        the code path, because the failure is silent by nature: the number stays plausible, so
        the only way anyone finds out is by reconciling against an invoice.

        Deliberately a read, not a repair. `_merge_subscription_spellings` repairs at startup;
        this reports, so a non-zero count here means a write path was added that skipped
        canonicalisation, and says so while the answer is still small enough to explain.

        Not a UNIQUE constraint, which is the other obvious answer and is the wrong one. FOCUS
        has no natural unique key -- the same resource legitimately produces several rows in a
        day for different meters, charge types and pricing tiers -- so a constraint would have
        to guess, and `ON CONFLICT DO NOTHING` on a wrong guess silently discards real charges.
        That trades over-reporting for under-reporting, which is the worse of the two.
        """
        with self.connect(read_only=True) as con:
            row = con.execute(
                'SELECT count(*) FROM costs WHERE "SubAccountId" IS NOT NULL '
                'AND ("SubAccountId" LIKE \'%/%\' OR "SubAccountId" <> lower("SubAccountId"))'
            ).fetchone()
        bad = int(row[0]) if row else 0
        return {
            "non_canonical_subscription_rows": bad,
            "ok": bad == 0,
            "detail": (
                "Every subscription id is canonical." if not bad else
                f"{bad:,} row(s) store a subscription id that is not canonical. These can be "
                "counted twice. Restart the app to run the startup repair, and check for an "
                "ingest path that writes SubAccountId without canonical_sub_id()."
            ),
        }

    def _create_indexes(self, con: Any) -> None:
        """Indexes for the shapes the dashboard actually asks for.

        Almost every query in the application is bounded by a date range, and most are then
        narrowed to a set of subscriptions, which is what the first two cover. The third is for
        the resource-level views -- waste, uptime, the resource drilldown -- which group by
        resource within a period.
        """
        statements = [
            'CREATE INDEX IF NOT EXISTS costs_period_idx ON costs ("ChargePeriodStart")',
            'CREATE INDEX IF NOT EXISTS costs_sub_period_idx '
            'ON costs (lower("SubAccountId"), "ChargePeriodStart")',
            'CREATE INDEX IF NOT EXISTS costs_resource_idx '
            'ON costs ("ResourceId", "ChargePeriodStart")',
        ]
        for statement in statements:
            try:
                con.execute(statement)
            except Exception as exc:  # noqa: BLE001 - a missing index is slow, not broken
                log.warning("warehouse: could not create index (%s)", exc)

    def _merge_subscription_spellings(self, con: Any) -> None:
        """Normalize identities without guessing which historical charges are duplicates.

        Even completely identical FOCUS rows may be legitimate repeated charges. Only a
        complete authoritative report can reconcile their multiplicity. Keep every row and
        record the affected subscription/date ranges for an explicit scoped reingest.
        """
        try:
            identities = con.execute(
                'SELECT DISTINCT "SubAccountId" FROM costs WHERE "SubAccountId" IS NOT NULL'
            ).fetchall()
            replacements = [(canonical_sub_id(raw), raw) for (raw,) in identities
                            if canonical_sub_id(raw) != raw]
            if not replacements:
                return
            con.execute("BEGIN TRANSACTION")
            try:
                from . import jobs
                jobs.lock_generation(con)
                con.execute("LOCK TABLE costs IN SHARE ROW EXCLUSIVE MODE")
                pending = con.execute(
                    "SELECT value FROM meta WHERE key = ?", ["subscription_reconciliation"]
                ).fetchone()
                affected = json.loads(pending[0]) if pending else {}
                for canonical, raw in replacements:
                    con.execute('UPDATE costs SET "SubAccountId" = ? WHERE "SubAccountId" = ?',
                                [canonical, raw])
                for canonical in sorted({sid for sid, _ in replacements if sid}):
                    first, last = con.execute(
                        'SELECT min("ChargePeriodStart"), max("ChargePeriodStart") FROM costs '
                        'WHERE "SubAccountId" = ?', [canonical]
                    ).fetchone()
                    old = affected.get(canonical, {})
                    starts = [str(value) for value in (first, old.get("from")) if value]
                    ends = [str(value) for value in (last, old.get("to")) if value]
                    affected[canonical] = {
                        "from": min(starts) if starts else None,
                        "to": max(ends) if ends else None,
                    }
                    if starts and ends:
                        con.execute(
                            "DELETE FROM meta WHERE key >= ? AND key <= ?",
                            [f"focus-day:{canonical}:{min(starts)}", f"focus-day:{canonical}:{max(ends)}"],
                        )
                con.execute(
                    "INSERT INTO meta (key, value, updated_at) VALUES (?, ?, now()) "
                    "ON CONFLICT (key) DO UPDATE SET value = excluded.value, "
                    "updated_at = excluded.updated_at",
                    ["subscription_reconciliation", json.dumps(affected)],
                )
                jobs.advance_generation(con)
                con.execute("COMMIT")
            except BaseException:
                con.execute("ROLLBACK")
                raise
            log.warning(
                "warehouse: normalized %d subscription spelling(s); every charge was preserved. "
                "Authoritative scoped reingest is required to reconcile possible overlapping "
                "imports; affected ranges are recorded in meta.subscription_reconciliation.",
                len(replacements))
        except Exception as exc:  # noqa: BLE001 - a failed repair must not stop the app booting
            log.warning("warehouse: could not normalize subscription spellings: %s", exc)

    # ------------------------------------------------------------------ ingest
    async def _report(self, client: httpx.AsyncClient, sub: str, start: str, end: str,
                      headers: dict, metric: str = "AmortizedCost") -> list[str]:
        """Kick off a cost details report and return download links once it is ready.

        Amortized by default. ActualCost bills a reservation as one lump on the day it was
        bought, which makes that month look like a spike and every later month look artificially
        cheap; amortized spreads the purchase across the term it covers, so a monthly trend is
        comparable and reservation usage is attributed to the resources that actually consumed
        it. It also populates `BenefitName` and `BenefitId`, without which "what are my
        commitments saving me" cannot be answered at all.

        The trade-off is that amortized reports the *effective* rate rather than what left the
        bank account this month. For an estate with no commitments the two are identical, which
        is why switching costs nothing until there is something to amortise.

        Nothing here is quick: submission is rate-limited per subscription and generation is a
        background job on Azure's side. Both waits are reported through `self.state` so a
        refresh that is merely slow can be told apart from one that has stopped — before this,
        a run spent up to half an hour showing `0/3` with no indication of what it was doing.
        """
        url = (f"{ARM}/subscriptions/{sub}/providers/Microsoft.CostManagement/"
               f"generateCostDetailsReport?api-version={DETAILS_API}")
        payload = {"metric": metric, "timePeriod": {"start": start, "end": end}}

        # Submissions are serialised per subscription, with a short gap between them.
        #
        # The limit Cost Management enforces here is per subscription, so firing this month,
        # last month and the month before simultaneously — which is what running the periods
        # concurrently did — guaranteed that two of the three were refused. Each then backed off
        # for a minute or more before its first poll, and a three-month refresh that should take
        # about as long as its slowest report took fifteen minutes.
        #
        # Queuing them costs a few seconds of stagger and avoids the refusal altogether. Only
        # the POST is serialised; generation and download still overlap, which is where the
        # minutes actually are.
        r = None
        self._phase(1, "submitting")
        try:
            spent = 0.0
            for attempt in range(6):
                async with self._submit_slot(sub):
                    r = await client.post(url, headers=headers, json=payload)
                if r.status_code != 429:
                    break
                wait = _retry_after(r, attempt)
                if spent + wait > SUBMIT_BUDGET:
                    log.info("submission still throttled after %.0fs (%s %s); giving up",
                             spent, sub[:8], start)
                    break
                spent += wait
                log.info("submission throttled (%s %s); retrying in %.0fs", sub[:8], start, wait)
                await asyncio.sleep(wait)
        finally:
            self._phase(-1, "submitting")

        if r is None:
            raise RuntimeError("no response from the report API")
        if r.status_code == 204:
            return []
        if r.status_code == 429:
            raise RuntimeError(
                "Cost Management is rate-limiting report requests for this subscription and did "
                "not accept one within a few minutes. Wait and try again — this is a per-"
                "subscription limit, not a permission problem."
            )
        if r.status_code not in (200, 202):
            # A WebDirect subscription (Visual Studio, pay-as-you-go, MSDN) is refused outright
            # by the Cost Details API. Left raw, the 422 reads as a bug in the app or a missing
            # permission, and neither is true -- no amount of retrying or granting will help.
            if r.status_code == 422 and "WebDirect" in r.text:
                raise RuntimeError(
                    "this subscription's offer type (WebDirect: Visual Studio, pay-as-you-go or "
                    "MSDN) is not supported by the Cost Details API. The dashboard's live figures "
                    "still work; for a loaded warehouse, use a Cost Management export and pick "
                    "FOCUS report on Refresh data."
                )
            raise RuntimeError(f"report request failed {r.status_code}: {r.text[:200]}")

        location = r.headers.get("Location")
        if not location:
            body = r.json()
            return [b["blobLink"] for b in body.get("manifest", {}).get("blobs", [])]

        # These reports genuinely take minutes; poll patiently rather than giving up early.
        deadline = time.time() + 1200
        self._phase(1, "generating")
        try:
            while time.time() < deadline:
                await asyncio.sleep(10)
                p = await client.get(location, headers=headers)
                if p.status_code in (202, 429):
                    continue
                if p.status_code != 200:
                    raise RuntimeError(f"poll failed {p.status_code}: {p.text[:200]}")
                body = p.json()
                status = (body.get("status") or "").lower()
                if status in ("completed", "success"):
                    return [b["blobLink"] for b in body.get("manifest", {}).get("blobs", [])]
                if status in ("nodatafound", "no data found"):
                    return []
                if status in ("failed", "error"):
                    raise RuntimeError(_report_failure(body, metric))
        finally:
            self._phase(-1, "generating")
        raise RuntimeError("report timed out after 20 minutes")

    @asynccontextmanager
    async def _submit_slot(self, sub: str) -> AsyncIterator[None]:
        """Serialise report submissions for one subscription, spaced by SUBMIT_GAP.

        Per subscription rather than globally: the throttle is scoped that way, so an estate
        with six subscriptions can still submit six reports at once and only queues within each.
        """
        lock = self._submit_locks.setdefault(sub, asyncio.Lock())
        async with lock:
            gap = SUBMIT_GAP - (time.monotonic() - self._last_submit.get(sub, 0.0))
            if gap > 0:
                await asyncio.sleep(gap)
            try:
                yield
            finally:
                self._last_submit[sub] = time.monotonic()

    def _phase(self, delta: int, name: str) -> None:
        """Count how many jobs are sitting in each wait, for the progress bar.

        `done` only moves when a whole period lands, and a period can be five minutes of waiting
        on Azure. A bar driven by `done` alone therefore sits at 0% for most of a refresh, which
        is barely better than a spinner — it is the thing that makes someone press the button
        again. Counting the phases lets the bar advance continuously through each period.
        """
        if self.state.get("status") != "running":
            return
        self.state[name] = max(0, self.state.get(name, 0) + delta)
        self._progress()

    # How far through a period each phase is. Submission is quick, generation is nearly all of
    # the wall time, and the download is short but not instant. The weights are deliberately
    # conservative: a bar that reaches 90% and stops reads as broken, so each phase claims
    # slightly less than it has really achieved.
    _PHASE_WEIGHT: ClassVar[dict[str, float]] = {
        "submitting": 0.10, "generating": 0.45, "loading": 0.85,
    }

    def _progress(self) -> None:
        """A single 0–1 figure for the bar, from whole periods plus partial ones."""
        total = self.state.get("total") or 0
        if not total:
            return
        partial = sum(self.state.get(name, 0) * weight
                      for name, weight in self._PHASE_WEIGHT.items())
        done = self.state.get("done", 0)
        # Never let the estimate overtake reality, and never let it reach 1 before the run ends.
        self.state["progress"] = round(min((done + partial) / total, 0.99), 3)

    @staticmethod
    def _rows(text: str) -> tuple[list[str], list[list[str]]]:
        reader = csv.reader(io.StringIO(text))
        header = next(reader, [])
        return header, [r for r in reader if r]

    @staticmethod
    def _mapping(header: list[str]) -> dict[str, int]:
        """Map our target columns onto whichever source names this account happens to use."""
        lower = {h.strip().lower().replace(" ", "").replace("_", ""): i for i, h in enumerate(header)}
        out: dict[str, int] = {}
        for target, aliases in COLUMNS.items():
            for alias in aliases:
                if alias in lower:
                    out[target] = lower[alias]
                    break
        return out

    async def _fetch(self, client: httpx.AsyncClient, links: list[str],
                     sub: dict, metric: str | None = None) -> list[list[Any]]:
        """Download and parse a report's blobs into rows ready for insertion.

        Deliberately does not touch the database. The old version inserted as it downloaded,
        which forced the caller to clear the period *before* the download so the two halves
        lined up — and the download is the part that takes minutes and fails. Separating them
        lets the caller replace a period only once it actually holds the replacement.
        """
        records: list[list[Any]] = []
        for link in links:
            d = await client.get(link)
            d.raise_for_status()
            # Off the event loop. The download awaits, so it was never the problem; the work
            # that follows it is — decompressing a multi-megabyte blob, decoding it, parsing
            # the CSV and walking every row is pure Python with no await in it, and on the
            # single worker the app runs on that freezes every other request for as long as it
            # takes. The database write below was moved to a thread for exactly this reason;
            # the parse in front of it was left behind, which is why a refresh still stuttered
            # after that fix.
            records.extend(await asyncio.to_thread(self._parse_blob, d.content, sub, metric))
        return records

    def _parse_blob(self, raw: bytes, sub: dict,
                    metric: str | None = None) -> list[list[Any]]:
        """One downloaded report blob, turned into rows. Synchronous and CPU-bound by nature."""
        if raw[:2] == b"\x1f\x8b":
            raw = gzip.decompress(raw)
        header, rows = self._rows(raw.decode("utf-8-sig", errors="replace"))
        if not rows:
            return []

        idx = self._mapping(header)
        billing = _BillingMapping(header, metric)
        if "BilledCost" not in idx:
            raise RuntimeError(f"no cost column found in export; saw {header[:12]}")

        keys = list(COLUMNS)
        i_rid, i_rname = keys.index("ResourceId"), keys.index("ResourceName")
        i_sid, i_sname = keys.index("SubAccountId"), keys.index("SubAccountName")

        records: list[list[Any]] = []
        for row in rows:
            rec: list[Any] = []
            for target in keys:
                i = idx.get(target)
                val = row[i] if i is not None and i < len(row) else None
                if target in NULLABLE_NUMERIC:
                    val = _nullable_number(val)
                elif target in NUMERIC:
                    try:
                        val = float(val) if val not in (None, "") else 0.0
                    except ValueError:
                        val = 0.0
                elif target == "ChargePeriodStart":
                    val = _to_date(val)
                rec.append(val)

            billing.apply(rec, row)
            # The export has no resourceName column; the leaf of the resource id is it.
            if not rec[i_rname] and rec[i_rid]:
                rec[i_rname] = str(rec[i_rid]).rstrip("/").split("/")[-1]
            # Fill subscription identity from what we already know, if absent in the file,
            # and canonicalise whatever was there — a FOCUS file spells this as an ARM path.
            rec[i_sid] = canonical_sub_id(rec[i_sid]) or canonical_sub_id(sub["id"])
            if not rec[i_sname]:
                rec[i_sname] = sub["name"]
            records.append(rec)
        return records

    async def _replace(self, sub_id: str, start: str, end: str,
                       records: list[list[Any]], *, preserve_detail: bool = False) -> int:
        """Swap one subscription-period's rows for the ones just downloaded, atomically.

        The delete and the insert are one transaction, so a reader never sees the window empty
        and a failure half-way leaves the previous data untouched. Before this, the delete ran
        minutes earlier — at the *start* of the job — which meant a refresh that failed had
        already destroyed what it was replacing, and the dashboard read zero for the whole run.
        """
        if not records:
            return 0
        sub_id = canonical_sub_id(sub_id)
        i_sub, i_day = COLUMNS_INDEX["SubAccountId"], COLUMNS_INDEX["ChargePeriodStart"]
        if any(canonical_sub_id(r[i_sub]) != sub_id or not start <= str(r[i_day]) <= end
               for r in records):
            raise ValueError("Replacement rows fall outside the requested subscription or dates.")
        records = [list(r) for r in records]
        for record in records:
            record[i_sub] = sub_id

        def _swap() -> tuple[int, int]:
            with self.connect() as con:
                con.execute("BEGIN TRANSACTION")
                try:
                    from . import jobs
                    jobs.assert_lease(con)
                    jobs.lock_generation(con)
                    protected: set[str] = set()
                    con.execute("LOCK TABLE costs IN SHARE ROW EXCLUSIVE MODE")
                    if preserve_detail:
                        # A day is indivisible: retaining detail and adding a query summary
                        # for that same day counts the same money twice. Legacy detail may
                        # predate BillingCostBasis, so also recognise its richer columns.
                        evidence = ['"BillingCostBasis" = \'FOCUS\'']
                        evidence.extend(f'NULLIF("{c}", \'\') IS NOT NULL' for c in (
                            "Tags", "BenefitId", "BenefitName", "PricingModel", "ChargeCategory",
                            "MeterName", "ProductName", "UnitOfMeasure", "PublisherName",
                        ))
                        evidence.extend(f'COALESCE("{c}", 0) <> 0' for c in (
                            "PricingQuantity", "UnitPrice",
                        ))
                        evidence.extend(f'"{c}" IS NOT NULL' for c in (
                            "ListCost", "ContractedCost", "ListUnitPrice", "ContractedUnitPrice",
                        ))
                        evidence.append('"InvoiceCost" IS NOT NULL AND "EffectiveCost" IS NOT NULL')
                        protected = {str(r[0]) for r in con.execute(
                            'SELECT DISTINCT "ChargePeriodStart" FROM costs '
                            'WHERE "SubAccountId" = ? AND "ChargePeriodStart" BETWEEN ? AND ? '
                            f'AND ({" OR ".join(evidence)})', [sub_id, start, end],
                        ).fetchall()}
                        protected.update(
                            row[0].rsplit(":", 1)[1] for row in con.execute(
                                "SELECT key FROM meta WHERE key >= ? AND key <= ?",
                                [f"focus-day:{sub_id}:{start}", f"focus-day:{sub_id}:{end}"],
                            ).fetchall())
                    retained = [r for r in records if str(r[i_day]) not in protected]
                    exclusion = (
                        ' AND "ChargePeriodStart" NOT IN (' + ",".join("?" for _ in protected) + ")"
                        if protected else ""
                    )
                    removed = (0 if retained else con.execute(
                        'SELECT count(*) FROM costs WHERE "SubAccountId" = ? '
                        'AND "ChargePeriodStart" BETWEEN ? AND ?' + exclusion,
                        [sub_id, start, end, *sorted(protected)],
                    ).fetchone()[0])
                    con.execute(
                        'DELETE FROM costs WHERE "SubAccountId" = ? '
                        'AND "ChargePeriodStart" BETWEEN ? AND ?' + exclusion,
                        [sub_id, start, end, *sorted(protected)],
                    )
                    receipt_exclusion = (
                        " AND key NOT IN (" + ",".join("?" for _ in protected) + ")"
                        if protected else "")
                    con.execute(
                        "DELETE FROM meta WHERE key >= ? AND key <= ?" + receipt_exclusion,
                        [f"focus-day:{sub_id}:{start}", f"focus-day:{sub_id}:{end}",
                         *(f"focus-day:{sub_id}:{day}" for day in sorted(protected))],
                    )
                    # Named, not positional. A positional insert silently depends on the table
                    # having been created with exactly today's COLUMNS order, and ALTER TABLE
                    # appends -- so a column added later shifted every value after it into the
                    # neighbouring column. Naming them costs nothing and removes the coupling.
                    insert_rows(con, retained)
                    if retained or removed:
                        jobs.advance_generation(con)
                    con.execute("COMMIT")
                    return len(retained), len(protected)
                except Exception:
                    con.execute("ROLLBACK")
                    raise

        # The lock still serialises writers; the difference is that waiting for the database no longer
        # stops the event loop. A refresh that writes tens of thousands of rows was freezing the
        # whole app while it did — no progress poll answered, no tab able to load — which is why
        # a working refresh looked like a broken button.
        async with self._lock:
            written, preserved = await asyncio.to_thread(_swap)
        if preserve_detail:
            self.state["preserved_days"] = self.state.get("preserved_days", 0) + preserved
        return written

    # ------------------------------------------------------------ the quick path
    async def _query_period(self, client: httpx.AsyncClient, sub: dict,
                            start: str, end: str, headers: dict,
                            metric: str, deadline: float | None = None) -> list[list[Any]]:
        """One synchronous Query API call, mapped into the warehouse schema.

        `deadline` is a monotonic clock reading this must not run past. It is checked before
        every request and before every sleep, because both are places where a quick refresh
        can quietly stop being quick — and the caller would rather have three subscriptions
        loaded and one reported as throttled than four loaded two minutes from now.
        """
        def out_of_time() -> bool:
            return deadline is not None and time.monotonic() >= deadline

        url = (f"{ARM}/subscriptions/{sub['id']}/providers/Microsoft.CostManagement/"
               f"query?api-version={QUERY_API}")
        body = {
            "type": metric,
            "timeframe": "Custom",
            "timePeriod": {"from": f"{start}T00:00:00Z", "to": f"{end}T23:59:59Z"},
            "dataset": {
                "granularity": "Daily",
                "aggregation": {
                    "totalCost": {"name": "Cost", "function": "Sum"},
                    "totalCostUSD": {"name": "CostUSD", "function": "Sum"},
                },
                "grouping": [{"type": "Dimension", "name": src}
                             for src, _ in QUERY_DIMENSIONS],
            },
        }

        rows: list[list[Any]] = []
        page_url = url
        for _ in range(20):          # the Query API pages with an absolute nextLink
            r = None
            for attempt in range(len(QUERY_BACKOFF) + 1):
                if out_of_time():
                    raise TimeoutError("ran out of time")
                # Serialised per subscription and spaced, so three months of one estate queue
                # instead of racing each other into a refusal.
                async with self._query_slot(sub["id"]):
                    r = await client.post(page_url, headers=headers, json=body)
                if r.status_code != 429:
                    break
                if attempt >= len(QUERY_BACKOFF):
                    break
                wait = _query_wait(r, attempt)
                # Sleeping past the deadline is the same as failing, only later. Stop now and
                # let the caller keep what the other subscriptions returned.
                if deadline is not None and time.monotonic() + wait >= deadline:
                    raise TimeoutError("throttled with no time left to wait")
                await asyncio.sleep(wait)
            if r is None or r.status_code != 200:
                raise RuntimeError(_query_failure(r))
            props = (r.json() or {}).get("properties") or {}
            rows.extend(self._map_query(props, sub, metric))
            nxt = props.get("nextLink")
            if not nxt:
                return rows
            if out_of_time():
                raise TimeoutError("incomplete query pagination; existing data is unchanged")
            page_url = nxt
        raise RuntimeError("Query exceeded 20 pages; existing data is unchanged.")

    @asynccontextmanager
    async def _query_slot(self, sub: str) -> AsyncIterator[None]:
        """One query at a time per subscription, spaced by QUERY_GAP.

        Per subscription rather than globally, for the same reason `_submit_slot` is: the limit
        is scoped that way, so an estate of six subscriptions can still have six queries in
        flight and only queues within each.
        """
        lock = self._query_locks.setdefault(sub, asyncio.Lock())
        async with lock:
            gap = QUERY_GAP - (time.monotonic() - self._last_query.get(sub, 0.0))
            if gap > 0:
                await asyncio.sleep(gap)
            try:
                yield
            finally:
                self._last_query[sub] = time.monotonic()

    @staticmethod
    def _map_query(props: dict, sub: dict, metric: str | None = None) -> list[list[Any]]:
        """Turn one Query API page into warehouse rows.

        Columns come back in whatever order the service chose and are matched by name, not
        position — the same reason the CSV loader maps by header. `UsageDate` arrives as the
        integer 20260801, which is not a date to anything downstream until it is one.
        """
        at = {c.get("name"): i for i, c in enumerate(props.get("columns") or [])}
        idx = {c: i for i, c in enumerate(COLUMNS)}
        by_source = dict(QUERY_DIMENSIONS)

        out: list[list[Any]] = []
        for raw in props.get("rows") or []:
            row: list[Any] = [None if c in BILLING_COLUMNS else 0.0 if c in NUMERIC else ""
                              for c in COLUMNS]

            if "UsageDate" not in at or raw[at["UsageDate"]] is None:
                continue
            text = str(raw[at["UsageDate"]])
            row[idx["ChargePeriodStart"]] = (
                f"{text[:4]}-{text[4:6]}-{text[6:8]}" if len(text) == 8 else text[:10])

            row[idx["BilledCost"]] = float(raw[at["Cost"]] or 0) if "Cost" in at else 0.0
            row[idx["CostInUsd"]] = float(raw[at["CostUSD"]] or 0) if "CostUSD" in at else 0.0
            row[idx["BillingCurrency"]] = (
                str(raw[at["Currency"]] or "") if "Currency" in at else "")
            basis = _cost_basis(metric)
            row[idx["BillingCostBasis"]] = basis
            cost = _nullable_number(raw[at["Cost"]]) if "Cost" in at else None
            if basis == "ActualCost":
                row[idx["InvoiceCost"]] = cost
            elif basis == "AmortizedCost":
                row[idx["EffectiveCost"]] = cost
            row[idx["SubAccountId"]] = canonical_sub_id(sub["id"])
            row[idx["SubAccountName"]] = sub.get("name") or sub["id"]

            for source, target in by_source.items():
                if source in at and target in idx:
                    row[idx[target]] = str(raw[at[source]] or "")

            # Derived exactly as the CSV path derives it, so a quick load and a full one agree
            # on what a resource is called rather than one leaving the column blank.
            rid = row[idx["ResourceId"]]
            if rid:
                row[idx["ResourceName"]] = rid.rsplit("/", 1)[-1]
            out.append(row)
        return out

    async def quick_ingest(self, subscriptions: list[dict], months: int = 3,
                           concurrency: int = 3, metric: str = "ActualCost") -> dict[str, Any]:
        """Load the same months through the synchronous Query API instead of a report.

        One call per subscription, not one per subscription per month. A report has to be
        generated per period, so `ingest` has no choice; a query does not — asking for the
        whole range at once returned all 91 days in a single response here, and it is the
        difference between three requests and nine against an endpoint that throttles per
        subscription. Nine of them raced each other into 429s and eight periods failed.
        """
        def headers_for(sub_id: str) -> dict[str, str]:
            token = _token(f"/subscriptions/{sub_id}")
            return {"Authorization": f"Bearer {token}", "Content-Type": "application/json",
                    "ClientType": CLIENT_TYPE}

        # The same window `_periods` covers, as one span rather than a list of months.
        spans = _periods(months)
        start, end = spans[0][0], spans[-1][1]
        label = f"{start} → {end}"

        # One clock for the whole run, not one per subscription: the promise is about how long
        # the person waits, and three subscriptions each taking their own budget is three times
        # the wait they were promised.
        deadline = time.monotonic() + QUICK_DEADLINE

        self.state = {"status": "running", "started": _now(), "metric": metric,
                      "total": len(subscriptions), "done": 0, "rows": 0, "failed": 0,
                      "empty": 0, "progress": 0.0, "unit": "subscription", "mode": "quick",
                      "preserved_days": 0}

        gate = asyncio.Semaphore(concurrency)
        errors: list[str] = []
        timed_out: list[str] = []
        timings: list[dict[str, Any]] = []

        async with httpx.AsyncClient(timeout=180, follow_redirects=True) as client:

            async def one(sub: dict) -> None:
                async with gate:
                    t_start = time.monotonic()
                    t_fetched = t_start
                    try:
                        self._phase(1, "loading")
                        try:
                            records = await self._query_period(
                                client, sub, start, end, headers_for(sub["id"]), metric,
                                deadline=deadline)
                        finally:
                            t_fetched = time.monotonic()
                            self._phase(-1, "loading")

                        if not records:
                            # Same reasoning as the report path: an empty answer is far more
                            # often a refusal than a period that cost nothing, and deleting on
                            # that basis destroys good data to record an absence.
                            kept = self._period_rows(sub["id"], start, end)
                            note = ("no rows returned; kept the "
                                    f"{kept:,} already held" if kept else "no rows returned")
                            self._log(sub, label, 0, "empty", note)
                            self.state["empty"] = self.state.get("empty", 0) + 1
                            return

                        # Refuse to write rows the dashboard cannot classify.
                        #
                        # The cost-area breakdown is a GROUP BY on ServiceFamily. When this
                        # column was missing from the query, every row still loaded happily and
                        # every one of them landed in "Other services" — AI & Data disappeared
                        # from the estate and nothing anywhere said so. Bad data that looks
                        # like good data is worse than no refresh, so this fails loudly instead.
                        fam = COLUMNS_INDEX["ServiceFamily"]
                        classified = sum(1 for r in records if r[fam])
                        if classified < len(records) * 0.5:
                            raise RuntimeError(
                                f"only {classified:,} of {len(records):,} rows came back with a "
                                "service family, so the cost-area breakdown would be wrong. "
                                "Nothing was written. Use FOCUS report.")

                        rows = await self._replace(sub["id"], start, end, records,
                                                  preserve_detail=True)
                        # Fetch and write timed separately, because they fail differently and
                        # only one of them is Azure's fault. Guessing which half was slow cost
                        # a deploy: the fetch was inside its budget the whole time and the
                        # write was not budgeted at all.
                        t_done = time.monotonic()
                        timings.append({
                            "subscription": sub["name"],
                            "rows": rows,
                            "fetch_s": round(t_fetched - t_start, 1),
                            "write_s": round(t_done - t_fetched, 1),
                        })
                        self._log(sub, label, rows, "ok", "quick load")
                        self.state["rows"] += rows
                        self.state["timings"] = timings
                        log.info("quick-ingested %s %s: %d rows (fetch %.1fs, write %.1fs)",
                                 sub["name"], label, rows,
                                 t_fetched - t_start, t_done - t_fetched)
                    except TimeoutError as exc:
                        # Not a failure of the data — a decision not to keep waiting. Reported
                        # separately so the UI can say "ran out of time" rather than "failed",
                        # which are different things and suggest different next steps.
                        log.info("quick ingest ran out of time for %s: %s", sub["name"], exc)
                        self._log(sub, label, 0, "failed", f"timed out: {exc}")
                        self.state["failed"] += 1
                        timed_out.append(sub["name"])
                    except Exception as exc:  # noqa: BLE001 - one subscription must not stop the rest
                        log.warning("quick ingest failed for %s: %s", sub["name"], exc)
                        self._log(sub, label, 0, "failed", str(exc)[:300])
                        self.state["failed"] += 1
                        errors.append(f"{sub['name']}: {str(exc)[:160]}")
                    finally:
                        self.state["done"] += 1
                        self._progress()

            await asyncio.gather(*(one(s) for s in subscriptions))

        total = len(subscriptions)
        failed = self.state["failed"]
        empty = self.state.get("empty", 0)
        # A run that ran out of time is described as that, not as a failure. The data it did
        # load is real and already written; what is missing is missing because the person was
        # not made to wait for it, which is the deal this option offers.
        late = (f"{len(timed_out)} subscription(s) were still rate-limited after "
                f"{int(QUICK_DEADLINE)}s and were left as they were "
                f"({', '.join(timed_out[:3])}). Their existing data is unchanged — "
                "use FOCUS report for a complete load.") if timed_out else ""

        if failed and failed == total:
            status = "failed"
            detail = late or "; ".join(errors[:3])
        elif failed:
            status = "partial"
            detail = late or (f"{failed} of {total} subscriptions failed. "
                              + "; ".join(errors[:2]))
            if late and errors:
                detail = f"{late} {errors[0]}"
        elif empty == total:
            status = "empty"
            detail = (f"All {total} subscription(s) returned no rows, so nothing was replaced. "
                      "The existing data is unchanged.")
        elif empty:
            status = "partial"
            detail = (f"{empty} of {total} subscription(s) returned no rows and were left as "
                      "they were; the rest reloaded.")
        else:
            status, detail = "ready", None

        self.state = {**self.state, **self.summary(), "status": status, "finished": _now(),
                      "loaded_rows": self.state["rows"],
                      "failed": failed, "empty": empty, "total": total, "metric": metric,
                      "unit": "subscription", "mode": "quick", "omits": list(QUICK_OMITS),
                      "timed_out": timed_out, "budget_seconds": int(QUICK_DEADLINE),
                      "progress": 1.0}
        if detail:
            self.state["detail"] = detail
        # Rebuilt once, at the end, rather than per subscription: the rollup aggregates the
        # whole table either way, so doing it three times triples the only slow part that is
        # left without changing the result.
        await self.refresh_rollup_async()
        return self.state

    async def ingest(self, subscriptions: list[dict], months: int = 3,
                     concurrency: int = 4, metric: str = "AmortizedCost") -> dict[str, Any]:
        """Pull the last N months of detail for each subscription. Safe to re-run.

        Each report takes minutes to generate server-side, so the jobs are run concurrently —
        sequentially this would be the better part of an hour for a handful of subscriptions.
        """
        # Per subscription, not once for the batch. An ARM token is issued by one tenant and
        # refused by every other, so a person with access in three directories holds three
        # tokens; one header for the batch would read the first estate and 401 on the rest,
        # which is indistinguishable from having no access at all.
        def headers_for(sub_id: str) -> dict[str, str]:
            token = _token(f"/subscriptions/{sub_id}")
            return {"Authorization": f"Bearer {token}", "Content-Type": "application/json",
                    "ClientType": CLIENT_TYPE}

        periods = _periods(months)
        jobs = [(s, p) for s in subscriptions for p in periods]

        self.state = {"status": "running", "started": _now(), "metric": metric,
                      "total": len(jobs), "done": 0, "rows": 0, "failed": 0, "empty": 0,
                      "progress": 0.0}

        gate = asyncio.Semaphore(concurrency)
        # Kept so the finished state can say *why* it failed. A bare count tells someone the
        # refresh did not work without telling them anything they can act on.
        errors: list[str] = []

        async with httpx.AsyncClient(timeout=300, follow_redirects=True) as client:

            async def one(sub: dict, period: tuple[str, str, str]) -> None:
                start, end, label = period
                async with gate:
                    try:
                        # Fetch first, replace second. The window is only cleared once the
                        # replacement rows are actually in hand, and the swap is one
                        # transaction — so a report that fails, times out or is throttled
                        # leaves the previous data exactly where it was.
                        headers = headers_for(sub["id"])
                        links = await self._report(client, sub["id"], start, end, headers, metric)
                        self._phase(1, "loading")
                        try:
                            records = await self._fetch(client, links, sub, metric) if links else []
                        finally:
                            self._phase(-1, "loading")

                        if not records:
                            # An empty report is far more often a transient refusal than a
                            # month that genuinely cost nothing, and deleting on that basis
                            # destroys good data to record an absence. Leave the period alone
                            # and say so; a stale row can be corrected, a deleted one cannot.
                            kept = self._period_rows(sub["id"], start, end)
                            note = ("no rows returned; kept the "
                                    f"{kept:,} already held" if kept else "no rows returned")
                            self._log(sub, label, 0, "empty", note)
                            log.info("ingested %s %s: %s", sub["name"], label, note)
                            self.state["empty"] = self.state.get("empty", 0) + 1
                            return

                        rows = await self._replace(sub["id"], start, end, records)
                        self._log(sub, label, rows, "ok", None)
                        self.state["rows"] += rows
                        log.info("ingested %s %s: %d rows", sub["name"], label, rows)
                    except Exception as exc:  # noqa: BLE001 - one period must not stop the rest
                        log.warning("ingest failed for %s %s: %s", sub["name"], label, exc)
                        self._log(sub, label, 0, "failed", str(exc)[:300])
                        self.state["failed"] += 1
                        errors.append(f"{sub['name']} {label}: {str(exc)[:160]}")
                    finally:
                        self.state["done"] += 1
                        self._progress()

            await asyncio.gather(*(one(s, p) for s, p in jobs))

        # Report what actually happened.
        #
        # This used to hardcode "ready" and then spread summary() over it. summary() carries no
        # status of its own, so every outcome came back as success: an ingest where all three
        # subscriptions failed still told the browser it was fine, and the UI -- which only ever
        # checked status == "failed" -- silently redrew the same data. Clicking Refresh appeared
        # to do nothing at all, with no error anywhere, which is the worst way for this to break.
        failed = self.state["failed"]
        empty = self.state.get("empty", 0)
        if failed and failed == len(jobs):
            status: str = "failed"
            detail: str | None = "; ".join(errors[:3])
        elif failed:
            status = "partial"
            detail = f"{failed} of {len(jobs)} periods failed. " + "; ".join(errors[:2])
        elif empty == len(jobs):
            # Nothing errored and nothing came back. The warehouse still holds whatever it had,
            # which is the right outcome, but calling that "ready" would claim a refresh
            # happened when no new data was written.
            status = "empty"
            detail = (f"All {len(jobs)} period(s) returned no rows, so nothing was replaced. "
                      "The existing data is unchanged. This usually means the Cost Details API "
                      "has nothing for this metric and period yet.")
        elif empty:
            status = "partial"
            detail = (f"{empty} of {len(jobs)} period(s) returned no rows and were left as they "
                      "were; the rest reloaded.")
        else:
            status, detail = "ready", None

        self.state = {**self.state, **self.summary(), "status": status, "finished": _now(),
                      "failed": failed, "empty": empty, "total": len(jobs), "metric": metric,
                      "progress": 1.0}
        if detail:
            self.state["detail"] = detail
        await self.refresh_rollup_async()
        return self.state

    def _period_rows(self, sub_id: str, start: str, end: str) -> int:
        """How many rows the warehouse already holds for one subscription-period.

        Used to say what was kept when a report comes back empty, so "0 rows" is reported as a
        decision not to overwrite rather than as a period that vanished.
        """
        with self.connect(read_only=True) as con:
            row = con.execute(
                'SELECT count(*) FROM costs WHERE "SubAccountId" = ? '
                'AND "ChargePeriodStart" BETWEEN ? AND ?',
                [canonical_sub_id(sub_id), start, end],
            ).fetchone()
        return row[0] or 0

    def _log(self, sub: dict, period: str, rows: int, status: str, detail: str | None) -> None:
        with self.connect() as con:
            con.execute("INSERT INTO ingest_log VALUES (?,?,?,?,?,?,?)",
                        [sub["id"], sub["name"], period, rows,
                         datetime.now(UTC), status, detail])

    # ------------------------------------------------------------------- query
    def budget_trend(self, sub_id: str, start: str, end: str,
                     tags: list[tuple[str, list[str]]] | None = None,
                     currency: str | None = None) -> dict[str, Any]:
        """Daily spend for one budget's current period, from warehouse cost data.

        Azure reports a budget as a single number — spend so far — with no history behind it,
        so "am I about to breach, or did that happen three weeks ago?" cannot be answered from
        the budget API at all. The warehouse has the days; this shapes them to the budget's own
        window, subscription and tag filter so the line means the same thing the headline does.

        `tags` is the budget's filter as (key, values) pairs. Matching is case-insensitive on
        both: Azure stores tag keys with whatever case they were created with — this estate has
        both `Contact` and `contact` — but treats them as one when filtering a budget, so
        matching case-sensitively here would draw an empty chart under a non-zero total.
        """
        where = ['"SubAccountId" = ?', '"ChargePeriodStart" BETWEEN ? AND ?']
        params: list[Any] = [canonical_sub_id(sub_id), start, end]

        for key, values in tags or []:
            if not values:
                # A key with no values is "has this tag at all", not "has it and equals
                # nothing" — the latter matches nothing and would silently empty the chart.
                where.append(
                    'EXISTS (SELECT 1 FROM json_each(TRY_CAST("Tags" AS JSON)) t '
                    "WHERE lower(t.key) = lower(?))")
                params.append(key)
                continue
            placeholders = ", ".join("lower(?)" for _ in values)
            where.append(
                'EXISTS (SELECT 1 FROM json_each(TRY_CAST("Tags" AS JSON)) t '
                f"WHERE lower(t.key) = lower(?) "
                f"AND lower(json_extract_string(t.value, '$')) IN ({placeholders}))")
            params.append(key)
            params.extend(values)

        sql = (f'SELECT "ChargePeriodStart" AS day, SUM(COALESCE("BilledCost", 0)) AS cost '
               f"FROM costs WHERE {' AND '.join(where)} GROUP BY 1 ORDER BY 1")
        try:
            with self.connect(read_only=True, currency=currency) as con:
                rows = con.execute(sql, params).fetchall()
        except Exception as exc:  # noqa: BLE001 - a chart is not worth failing the tab for
            log.info("budget trend failed for %s: %s", sub_id[:8], str(exc)[:200])
            return {"labels": [], "values": [], "cumulative": [], "total": 0.0,
                    "error": str(exc)[:200]}

        labels = [str(r[0])[:10] for r in rows]
        values = [round(float(r[1] or 0), 2) for r in rows]
        running = 0.0
        cumulative = []
        for v in values:
            running += v
            cumulative.append(round(running, 2))

        return {
            "labels": labels,
            "values": values,
            "cumulative": cumulative,
            "total": round(running, 2),
            "from": start,
            "to": end,
        }

    def currencies(self, scope: list[str] | None = None) -> list[str]:
        """Distinct billing currencies present, honouring the subscription scope."""
        with self.reader(scope) as reader:
            rows = reader.rows(
                f'SELECT DISTINCT "BillingCurrency" FROM {reader.agg} '
                "WHERE \"BillingCurrency\" IS NOT NULL AND \"BillingCurrency\" <> ''",
                None)
        return sorted(r["BillingCurrency"] for r in rows)

    def summary(self, scope: list[str] | None = None) -> dict[str, Any]:
        """What the warehouse holds. With a scope, only what those subscriptions contribute —
        so a person who can see one subscription isn't told the row count of the whole estate.

        Read from `Reader.agg` rather than `costs`. Every question here is at or
        above the rollup's grain: the row count is the sum of the `charges` each
        group stands for, the period is a min/max over its date column, and the
        money is already summed per currency. On the real estate that is the
        difference between 25s and 0.15s, and this runs on the header of every
        page -- so the detail table was being scanned in full to answer four
        numbers that the rollup holds pre-computed.
        """
        with self.reader(scope) as reader:
            row = reader.rows(
                f'SELECT {rollup.charges_sql(reader.agg)} AS rows, '
                f'min("ChargePeriodStart") AS lo, max("ChargePeriodStart") AS hi, '
                f'count(DISTINCT "SubAccountId") AS subs FROM {reader.agg}')[0]
            # Totals are only meaningful within a single currency, so never pre-sum across them.
            by_currency = reader.rows(
                'SELECT coalesce(nullif("BillingCurrency", \'\'), \'unknown\') AS currency, '
                f'sum("BilledCost") AS cost FROM {reader.agg} GROUP BY 1 ORDER BY 2 DESC',
                None)

        # Its own read, because `Reader` passes no parameters and this key is bound.
        # A single indexed row against `meta`, so the extra connection costs nothing
        # next to the scan it replaced.
        pending = self.get_meta("subscription_reconciliation")

        totals = [{"currency": r["currency"], "cost": round(r["cost"] or 0.0, 2)}
                  for r in by_currency]
        known = [t for t in totals if t["currency"] != "unknown"]
        mixed = len(totals) > 1
        affected = json.loads(pending) if pending else {}
        allowed = {canonical_sub_id(sid) for sid in scope} if scope else None
        reconciliation = [{"subscription_id": sid, **period}
                          for sid, period in sorted(affected.items())
                          if allowed is None or sid in allowed]
        return {
            "rows": row["rows"] or 0,
            "from": str(row["lo"]) if row["lo"] else None,
            "to": str(row["hi"]) if row["hi"] else None,
            "subscriptions": row["subs"] or 0,
            # Left as None when mixed: a single cross-currency total would be a meaningless number.
            "total_cost": totals[0]["cost"] if len(totals) == 1 else None,
            "total_by_currency": totals,
            "currency": known[0]["currency"] if len(known) == 1 and not mixed else None,
            "currencies": [t["currency"] for t in known],
            "mixed_currency": mixed,
            "reconciliation_required": bool(reconciliation),
            "reconciliation": reconciliation,
            "reconciliation_note": (
                "Legacy subscription identities were normalized without deleting charges. "
                "Reingest the listed ranges from authoritative FOCUS reports to reconcile "
                "possible overlapping imports." if reconciliation else None),
        }

    def _schema_alternation(self) -> str:
        """Every schema name on this server, as a regex alternation, cached.

        Asked of the server rather than assumed. `public` is the usual answer and `PGSCHEMA`
        is the other, but a customer database has whatever schemas the customer made, and any
        of them is a qualifier that would resolve past the restricted search path.

        Cached for the life of the process: a new schema appearing mid-run is not a case worth
        a round trip per model query, and the search path already confines unqualified names.
        `pg_temp` and `pg_toast` are added by hand -- they are aliases PostgreSQL resolves
        without ever appearing in pg_namespace under those names.
        """
        if self._schema_names is None:
            names = {"public", "pg_catalog", "information_schema", "pg_temp", "pg_toast"}
            try:
                with self.connect() as con:
                    names.update(
                        r[0].lower() for r in
                        con.execute("SELECT nspname FROM pg_namespace").fetchall() if r[0])
            except Exception as exc:  # noqa: BLE001 - the built-in names are the ones that matter
                log.warning("warehouse: could not list schemas (%s); "
                            "checking model SQL against the built-in names only", exc)
            # Longest first, so `pg_temp_3` is matched before `pg_temp` and the alternation
            # cannot stop early on a prefix.
            self._schema_names = "|".join(
                re.escape(n) for n in sorted(names, key=len, reverse=True))
        return self._schema_names

    def query(self, sql: str, limit: int = 200, scope: list[str] | None = None) -> dict[str, Any]:
        """Run a read-only SELECT against the warehouse, optionally scoped to subscriptions.

        Three defences, because this is the one path that runs SQL the model wrote.

        The checks below reject anything that is not a plain SELECT, block functions that would
        read outside the warehouse, and block catalog-qualified table names that would sidestep
        the scope filter. They are a filter on text, so they are only as good as the list.

        The transaction is the guarantee about writes. PostgreSQL refuses every write inside a
        `READ ONLY` transaction itself -- no INSERT, UPDATE, DELETE or DDL, whatever the
        statement managed to look like on the way past the checks. The scope view is created
        before it opens, since a read-only transaction cannot create even a temp object.

        The role is the guarantee about reads, and the only one that holds against a payload
        nobody has thought of yet: the statement executes as `QUERY_ROLE`, which owns nothing
        and is granted nothing beyond this session's scope view. If that cannot be arranged the
        query is refused with `QueryRoleUnavailable` rather than run with the application's own
        rights, which on the default deployment are those of an Entra administrator.
        """
        cleaned = sql.strip().rstrip(";")
        lowered = cleaned.lower()
        # Scanned with literals and comments blanked out. Azure really does sell a service
        # called `Load Balancer` and a resource group really can be called `delete-me`, so
        # matching keywords inside a string rejects perfectly good questions about them.
        # An unterminated quote raises rather than guessing which half to blank.
        try:
            scan = strip_literals(lowered)
        except ValueError as exc:
            raise ValueError(f"That query is not valid SQL: {exc}.") from exc

        if not (lowered.startswith("select") or lowered.startswith("with")):
            raise ValueError("Only SELECT queries are allowed.")
        # On the blanked text, so a semicolon inside a tag value or a resource name is data
        # rather than a second statement.
        if ";" in scan:
            raise ValueError("Only a single statement is allowed.")

        # Two kinds of thing, both reachable from a statement that starts with SELECT or WITH.
        #
        # The write verbs, because a data-modifying CTE -- `WITH x AS (DELETE ... RETURNING 1)
        # SELECT ...` -- is a write behind a SELECT. The READ ONLY transaction below refuses
        # those anyway; naming them here turns a database error into a sentence that says what
        # the rule is.
        #
        # The functions, because those are the ones a READ ONLY transaction does *not* stop:
        # reading a server file, opening a large object or a dblink connection, and sleeping are
        # all reads. Matched as prefixes, because `\bpg_sleep\b` does not match `pg_sleep_for`
        # -- an underscore is a word character, so there is no boundary after the name -- and a
        # list that blocks one spelling of a function while missing its siblings is worse than
        # no list, because it reads as though the case were handled.
        #
        # Statement keywords that cannot follow a leading SELECT at all (GRANT, TRUNCATE,
        # VACUUM and friends) are deliberately absent: they add no protection and would reject
        # a column or alias that happened to share the name.
        banned = ("insert", "update", "delete", "merge", "copy", "grant", "revoke",
                  "drop", "create", "alter", "attach", "detach", "call",
                  r"set(?:_config)?", r"pg_sleep\w*", r"dblink\w*", r"lo_\w+",
                  r"pg_read_\w*file", r"pg_ls_\w+", "pg_stat_file", "pg_read_server_files",
                  r"pg_(?:terminate|cancel)_backend", "pg_reload_conf", "pg_rotate_logfile",
                  # The functions that take SQL, or a relation name, as a *string* and read it.
                  # `table_to_xml('public.costs_rollup', ...)` hides the qualifier inside a
                  # literal, which the checks below cannot see because literals are blanked
                  # before they run. Matched on the whole family rather than by name: the
                  # siblings are `query_`, `cursor_`, `schema_`, `database_`, each with a
                  # `_to_xmlschema` and `_to_xml_and_xmlschema` spelling.
                  #
                  # Second line of defence, not the defence. The role the statement runs as
                  # cannot read those tables whatever it calls them -- see `QUERY_ROLE`. This
                  # is here so the deployment that could not create that role still refuses
                  # the known payload, and so the failure is a sentence rather than a
                  # permission error.
                  r"\w*to_xml\w*")
        if any(re.search(rf"\b{word}\b", scan) for word in banned):
            raise ValueError(
                "That query uses a statement or function that is not allowed here. "
                "Only plain SELECT/WITH queries against the `costs` table are permitted."
            )

        # No schema may be named as a qualifier, anywhere in the statement.
        #
        # This deliberately does not try to work out where a relation *is*. The previous
        # version looked for a qualified name immediately after FROM or JOIN, and a review
        # found three ways past it in minutes: `FROM costs, public.costs` puts the second
        # relation after a comma, `WITH x AS (TABLE public.costs)` uses no FROM at all, and
        # more of the grammar remains. Enumerating the positions a relation can occupy means
        # keeping up with PostgreSQL's grammar in a regex, which is a losing race.
        #
        # Turning the question around removes the race. A qualified name is only dangerous
        # because it resolves past the search path, and it can only do that if the qualifier
        # is a real schema -- so the check is on the qualifier, and where it appears stops
        # mattering. An alias called `public` is refused too, which is a fair trade.
        if re.search(rf"\b(?:{self._schema_alternation()})\s*\.", scan):
            raise ValueError(
                "Refer to the table as plain `costs`, without a database or schema prefix."
            )
        # Kept as well, because it catches a qualifier that is not a schema this deployment
        # has -- `main.costs`, say -- and says so with the same sentence rather than letting
        # PostgreSQL answer with "relation does not exist".
        if re.search(r"\b(?:from|join)\s+(?:only\s+)?(?:\w+\s*|\s*)\.", scan):
            raise ValueError(
                "Refer to the table as plain `costs`, without a database or schema prefix."
            )

        self._check_currency(lowered, scope)

        started = time.perf_counter()
        with self.connect(read_only=True, scope=scope) as con:
            from .reporting import selected_month

            if not scope and not selected_month():
                # An unscoped query still needs `costs` to exist in pg_temp, because the search
                # path set below hides the schema it normally lives in. A view, so this costs
                # nothing beyond parsing it.
                con.execute(f"CREATE TEMP VIEW costs AS SELECT * FROM {self._pg_table}")
            # Hand the restricted role the one object it is meant to see. Before BEGIN,
            # because GRANT writes to the catalogue and a read-only transaction refuses it.
            # Raises rather than degrading: if the statement cannot be confined, it does not run.
            self._grant_scope_to_query_role(con)
            # The guarantee, as opposed to the checks above. Both statements have to come after
            # the view is created: a read-only transaction cannot create even a temp object, and
            # the restricted search path would hide the base table the view is defined over.
            con.execute("BEGIN READ ONLY")
            try:
                # Everything the model is allowed to reach is now either a temp object -- the
                # scope-carrying `costs` view -- a shim function, or a built-in. The schema
                # holding the warehouse tables is off the path entirely: `costs_rollup` carries
                # the same money for *every* subscription and has no scope filter of its own,
                # and hiding the schema stops it along with the next table someone adds without
                # reading this code. The shim schema holds only functions, so leaving it
                # reachable exposes no rows.
                #
                # LOCAL, so it is undone by the ROLLBACK below rather than left on a pooled
                # session for the next borrower.
                con.execute(f"SET LOCAL search_path TO pg_temp, {SHIM_SCHEMA}, pg_catalog")
                # A ceiling on how long a model's query may hold a pooled connection. The word
                # list above cannot be the defence against a slow query: an accidental cross
                # join costs the same as a deliberate one, and with PG_POOL_SIZE connections
                # the app stops answering. LOCAL, like the search path, so it lasts exactly as
                # long as this statement.
                con.execute(f"SET LOCAL statement_timeout = {QUERY_TIMEOUT_MS}")
                # Last, so everything above is done with the application's own rights and
                # only the model's statement runs restricted. LOCAL, so the ROLLBACK below
                # gives the session back to the pool as itself.
                con.execute(f"SET LOCAL ROLE {QUERY_ROLE}")
                cur = con.execute(cleaned)
                names = [d[0] for d in cur.description]
                rows = cur.fetchmany(limit)
                more = cur.fetchone() is not None
            finally:
                # Rolled back rather than committed: nothing was written, and this leaves the
                # session idle so the pool can hand it straight on.
                #
                # Swallowed, because this runs while an exception from the query may be in
                # flight. A connection too broken to roll back is also too broken to reuse, and
                # `_release` discards it -- so failing here would replace the error the caller
                # needs to see with a less useful one.
                try:
                    con.execute("ROLLBACK")
                except Exception:  # noqa: BLE001
                    log.debug("could not roll back after a model query", exc_info=True)

        return {
            "columns": names,
            "rows": [dict(zip(names, r)) for r in rows],
            "row_count": len(rows),
            "truncated": more,
            "scope": scope or "all subscriptions",
            "ms": int((time.perf_counter() - started) * 1000),
        }

    # Columns that hold money. Summing these across different currencies is arithmetic on
    # incompatible units — the result looks authoritative and means nothing.
    _MONEY = ("billedcost", "invoicecost", "effectivecost", "listcost", "contractedcost")

    def _grant_scope_to_query_role(self, con: Any) -> None:
        """Let `QUERY_ROLE` read this session's scope view, and nothing else.

        Raises `QueryRoleUnavailable` rather than returning, because there is no acceptable
        thing to do next. The caller used to fall back to running the statement with the
        application's own rights -- the behaviour from before privilege separation existed --
        which quietly turned the strongest boundary off at exactly the moment it failed, and
        left a warning in a log as the only sign. A refused query is recoverable and visible;
        an unconfined one is neither.

        The grants are per connection rather than once, because the objects are per connection:
        `DISCARD TEMP` drops the view when the session goes back to the pool, and the next
        borrower builds a new one carrying a different caller's scope. Three catalogue writes
        against a path that has just spent a second or two in a language model.
        """
        if not self._query_role:
            raise QueryRoleUnavailable(
                "The database is not set up to run model-authored queries safely: the "
                f"{QUERY_ROLE} role could not be assumed, so this query was not run. See the "
                "server log; the fix is infra/sql/create-app-role.sql."
            )
        try:
            temp_schema = con.execute(
                "SELECT nspname FROM pg_namespace WHERE oid = pg_my_temp_schema()"
            ).fetchone()
            if not temp_schema:
                # No temp schema means no scope view, so there is nothing safe to run.
                raise QueryRoleUnavailable(
                    "This connection has no scope view, so the query was not run."
                )
            temp = temp_schema[0]
            # USAGE on the temp schema, because SET ROLE changes who is asking and the temp
            # schema belongs to the session's own role. Named explicitly rather than as
            # `pg_temp`, which is an alias PostgreSQL resolves for references, not for grants.
            con.execute(f'GRANT USAGE ON SCHEMA "{temp}" TO {QUERY_ROLE}')
            # The shim functions the SQL translation emits. Functions are executable by PUBLIC
            # by default, but a schema is not usable by PUBLIC, so without this every
            # translated query fails on `try_cast_double`.
            con.execute(f"GRANT USAGE ON SCHEMA {SHIM_SCHEMA} TO {QUERY_ROLE}")
            # The one object the model may read. Read through the view owner's rights, so this
            # grants sight of the rows the view selects without granting sight of the table.
            con.execute(f'GRANT SELECT ON "{temp}".costs TO {QUERY_ROLE}')
        except QueryRoleUnavailable:
            raise
        except Exception as exc:  # noqa: BLE001
            log.warning("warehouse: could not restrict the query role (%s)", exc)
            raise QueryRoleUnavailable(
                "This query was not run: the restricted role could not be given access to "
                "the scoped data, and running it unrestricted is not an option."
            ) from exc

    def _check_currency(self, lowered: str, scope: list[str] | None) -> None:
        """Refuse to aggregate money across mixed currencies.

        Both legitimate patterns — grouping by currency, or filtering to one — mention the
        column, so requiring it is enough to let correct queries through.
        """
        if "billingcurrency" in lowered:
            return
        if not re.search(r"\b(sum|avg|total|median)\s*\(", lowered):
            return
        if not any(col in lowered for col in self._MONEY):
            return

        found = self.currencies(scope)
        if len(found) > 1:
            raise ValueError(
                f"This data spans more than one billing currency ({', '.join(found)}), so "
                "totalling a money column across all of it would be meaningless. Either add "
                '"BillingCurrency" to the GROUP BY so each currency is reported separately, '
                'or filter to one currency with WHERE "BillingCurrency" = \'...\'.'
            )

    def subscriptions(self, scope: list[str] | None = None) -> list[dict[str, Any]]:
        """Subscriptions present in the warehouse, with their total spend.

        Grouped on three columns the rollup carries, so `Reader.agg` answers it
        exactly. This backs the subscription picker, which every page opens with.
        """
        with self.reader(scope) as reader:
            rows = reader.rows(
                'SELECT "SubAccountId" AS id, "SubAccountName" AS name, '
                'coalesce(nullif("BillingCurrency", \'\'), \'unknown\') AS currency, '
                f'round(sum("BilledCost"), 2) AS cost FROM {reader.agg} '
                'WHERE "SubAccountId" IS NOT NULL GROUP BY 1,2,3 ORDER BY cost DESC',
                None)
        return [{"id": r["id"], "name": r["name"], "currency": r["currency"],
                 "cost": r["cost"]} for r in rows]

    # ------------------------------------------------------------------ meta
    # Small durable facts about the deployment. Deliberately not part of `state`, which is
    # rebuilt from a summary on every boot and is about the last ingest rather than about the
    # installation.

    def get_meta(self, key: str, default: str | None = None) -> str | None:
        """One stored fact, or `default` if it was never written."""
        try:
            with self.connect(read_only=True) as con:
                row = con.execute("SELECT value FROM meta WHERE key = ?", [key]).fetchone()
            return row[0] if row else default
        except Exception as exc:  # noqa: BLE001 - a missing fact must never break a request
            log.debug("meta read failed for %s: %s", key, str(exc)[:200])
            return default

    def set_meta(self, key: str, value: str) -> None:
        """Record one fact. Last write wins; there is no history worth keeping here."""
        try:
            with self.connect() as con:
                con.execute(
                    "INSERT INTO meta (key, value, updated_at) VALUES (?, ?, now()) "
                    "ON CONFLICT (key) DO UPDATE SET value = excluded.value, "
                    "updated_at = excluded.updated_at",
                    [key, value],
                )
        except Exception as exc:  # noqa: BLE001
            log.warning("could not record meta %s: %s", key, str(exc)[:200])

    def record_access(self, *, user_name: str, email: str, source: str,
                      is_admin: bool, tab: str) -> None:
        """One line: who looked at what. Never allowed to break the page it is logging.

        Fired from the same request every tab switch already makes (`/api/dashboard/sections`),
        so this costs one more small INSERT rather than a request nobody else needed.
        """
        try:
            with self.connect() as con:
                con.execute(
                    "INSERT INTO access_log (user_name, email, source, is_admin, tab, at) "
                    "VALUES (?, ?, ?, ?, ?, now())",
                    [user_name, email, source, bool(is_admin), tab],
                )
        except Exception as exc:  # noqa: BLE001 - an audit write must never break the page
            log.warning("could not record access for %s: %s", user_name, str(exc)[:200])

    def recent_access(self, limit: int = 200) -> list[dict[str, Any]]:
        """The audit log itself, most recent first. Admin-only at the route that serves this."""
        try:
            with self.connect(read_only=True) as con:
                rows = con.execute(
                    "SELECT user_name, email, source, is_admin, tab, at FROM access_log "
                    "ORDER BY at DESC LIMIT ?",
                    [max(1, min(int(limit), 500))],
                ).fetchall()
            return [
                {"user": r[0], "email": r[1], "source": r[2], "admin": bool(r[3]),
                 "tab": r[4], "at": str(r[5])}
                for r in rows
            ]
        except Exception as exc:  # noqa: BLE001 - an empty log must never break the page
            log.warning("could not read access log: %s", str(exc)[:200])
            return []

    def row_count(self) -> int:
        """How many cost rows exist at all. The question "is this deployment empty"."""
        try:
            with self.connect(read_only=True) as con:
                return int(con.execute("SELECT count(*) FROM costs").fetchone()[0] or 0)
        except Exception:  # noqa: BLE001
            return 0

    def has_rows(self) -> bool:
        """Whether the warehouse holds any cost row at all.

        `row_count() > 0` answers this too, and that is what the callers used to do --
        but it answers it by counting all of them. On the real estate that is a
        5.8M-row aggregate taking 1.6-1.9s, and `EXISTS` answers the same question
        from the first row it finds, in about a millisecond:

            SELECT count(*) FROM costs              1.571s / 1.661s / 1.775s
            SELECT EXISTS (SELECT 1 FROM costs)     0.001s / 0.001s / 0.000s

        The cost matters because the subscription picker asks it on every page load,
        so it was 1.9s of every first paint to establish something a single row
        settles. Callers that publish the count itself still use `row_count`; this is
        only for the ones that were throwing the number away.
        """
        try:
            with self.connect(read_only=True) as con:
                return bool(con.execute(
                    "SELECT EXISTS (SELECT 1 FROM costs)").fetchone()[0])
        except Exception:  # noqa: BLE001
            return False



def _report_failure(body: dict, metric: str) -> str:
    """What to say when Azure reports the job itself as Failed.

    The raw body is `{"status":"Failed","error":{"code":500,"message":"InternalServerError
    occurred during processing the operation Id:..."}}` — a correlation id and nothing else.
    Printed verbatim that reads like an application bug, and the natural response is to check
    permissions and retry, neither of which helps: the request was accepted, the generation ran
    on Azure's side, and it is Azure that failed. Saying which metric was asked for matters
    because this is commonly metric-specific — ActualCost failing while AmortizedCost succeeds
    on the same subscription and period is exactly what happens.
    """
    err = body.get("error") or {}
    code = err.get("code")
    message = str(err.get("message") or "")

    if str(code) in ("500", "InternalServerError") or "internalservererror" in message.lower():
        return (
            f"Azure Cost Management could not generate the {metric} report — it accepted the "
            "request and then failed internally (HTTP 500). This is a fault on Azure's side, "
            "not a permission or configuration problem here, and it is often specific to one "
            "metric: if this was API data, try Amortized, which is generated separately."
        )
    if message:
        return f"Azure reported the {metric} report as failed: {message[:200]}"
    return f"Azure reported the {metric} report as failed: {str(body)[:200]}"


# How many rows to carry in one INSERT statement when binding parameters.
#
# `executemany` re-executes a prepared statement per row, so its cost is per row rather than
# per batch: 7,000 rows of the real 25-column schema took 17.8s locally. Batching the same rows
# into multi-row VALUES statements cut that to 3.4s. Larger chunks are slower again, because
# the statement is compiled with chunk x 25 placeholders and that overtakes the saving.
INSERT_CHUNK = 200


def _partitioned_target(con: Any, table: str) -> str | None:
    """The schema holding `table`, if `table` is partitioned. None otherwise.

    Resolved through `to_regclass` rather than by splitting the name, because the
    callers pass every form: a bare `costs` that resolves through search_path, a
    quoted `"cl_x7".costs`, and `bench`. One catalog lookup handles all three and
    cannot disagree with what the following COPY will actually write to.

    Returning None for an ordinary table is the important case, not an edge case.
    A database created before partitioning still holds a flat `costs`, and
    `CREATE TABLE IF NOT EXISTS ... PARTITION BY` does not convert it. Converting
    it is a migration, which is gated separately, so both shapes have to work.
    """
    row = con.execute(
        "SELECT c.relkind, n.nspname FROM pg_class c "
        "JOIN pg_namespace n ON n.oid = c.relnamespace "
        "WHERE c.oid = to_regclass(?)",
        [table],
    ).fetchone()
    if not row or row[0] != "p":
        return None
    return str(row[1])


def _charge_dates(records: Sequence[Sequence[Any]]) -> list[date]:
    """The distinct charge dates in a batch, ignoring anything unparseable.

    Values arrive as `date` objects from the API paths and as ISO strings from the
    CSV paths, so both are accepted. A row whose date cannot be read is skipped
    rather than raised on: it still has a default partition to land in, and
    failing the whole batch over one malformed date would lose the other rows.
    """
    index = COLUMNS_INDEX["ChargePeriodStart"]
    out: set[date] = set()
    for row in records:
        if index >= len(row):
            continue
        value = row[index]
        if isinstance(value, datetime):
            out.add(value.date())
        elif isinstance(value, date):
            out.add(value)
        elif value:
            try:
                out.add(date.fromisoformat(str(value)[:10]))
            except ValueError:
                continue
    return sorted(out)


def ensure_partitions_for(con: Any, records: Sequence[Sequence[Any]],
                          table: str = "costs") -> list[str]:
    """Create the monthly partitions a batch is about to need.

    Done here, in the one function every write path already goes through, rather
    than at each caller. The rollup docstring makes the same argument and it holds
    just as well for partitions: finding all the write paths again and missing one
    would send those charges to the default partition, where they are still
    correct but no longer prunable or droppable.

    A no-op when `costs` is not partitioned, and best-effort when it is -- the
    default partition means a partition that could not be created costs a slower
    query rather than a rejected charge, so this must never fail a load.
    """
    days = _charge_dates(records)
    if not days:
        return []
    return ensure_partitions_between(con, days[0], days[-1], table)


def ensure_partitions_between(con: Any, first: Any, last: Any,
                              table: str = "costs") -> list[str]:
    """The same, for a caller that knows the span but is not holding the rows.

    The streaming importer writes through `INSERT ... SELECT` from a staging table,
    so it never has the batch in Python to hand to `ensure_partitions_for` -- but it
    does know the range, which is all this needs.
    """
    schema = _partitioned_target(con, table)
    if schema is None:
        return []
    try:
        return schema_v2.ensure_partitions(con, schema, _as_date(first), _as_date(last))
    except Exception as exc:  # noqa: BLE001
        log.warning("warehouse: could not create partitions for %s..%s (%s); "
                    "rows will land in the default partition", first, last, exc)
        return []


def _as_date(value: Any) -> date:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    return date.fromisoformat(str(value)[:10])


def insert_rows(con: Any, records: Sequence[Sequence[Any]],
                table: str = "costs", chunk: int = INSERT_CHUNK) -> int:
    """Bulk-insert rows into `table`, naming the columns explicitly.

    Named rather than positional: a positional insert depends on the table having been created
    with exactly today's COLUMNS order, and ALTER TABLE appends — so a column added later
    shifts every value after it one place to the left, silently.

    COPY first, parameter binding as a fallback. Binding is the actual bottleneck on the
    deployed instance, and it took three measurements to see it: the box is only 5.4x slower
    than a laptop on a plain Python loop and scans the whole 20,667-row table in 8ms, but it
    inserted 51 rows/s against 2,200 locally -- a 43x gap that neither CPU nor disk explains.
    What is 43x slower is the per-value binding layer itself.

    The fallback runs inside a SAVEPOINT, and that is what makes it a fallback rather than a
    comment. Every real caller invokes this inside a transaction -- `_replace` deletes the
    range and re-inserts it as one unit -- and a failed COPY aborts that transaction, so
    PostgreSQL rejects every later statement on it with "current transaction is aborted".
    Without the savepoint to roll back to, the retry could not run and the batch was lost
    anyway, while the log claimed it had fallen back. Correctness over speed only counts if
    the slow path can actually execute.
    """
    if not records:
        return 0

    # Before the savepoint, and before COPY. Creating a partition takes a lock on
    # the parent, and taking that lock inside the same savepoint the COPY will roll
    # back on failure would undo the partitions along with the batch.
    ensure_partitions_for(con, records, table)

    # A savepoint only exists inside a transaction, and this is called both ways: the ingest
    # wraps its delete and insert in one, while the fixtures and the diagnostics bench call it
    # on an autocommit connection. Outside a transaction no savepoint is needed -- a failed
    # COPY is its own transaction and leaves the session idle, so the retry can just run.
    from psycopg.pq import TransactionStatus

    nested = con.raw.info.transaction_status != TransactionStatus.IDLE
    if nested:
        con.execute("SAVEPOINT bulk_load")
    try:
        rows = _insert_via_copy(con, records, table)
    except Exception as exc:  # noqa: BLE001
        log.warning("copy bulk load failed (%s); falling back to parameter binding", exc)
        if nested:
            con.execute("ROLLBACK TO SAVEPOINT bulk_load")
        rows = _insert_via_binding(con, records, table, chunk)
    if nested:
        con.execute("RELEASE SAVEPOINT bulk_load")
    return rows


def _insert_via_copy(con: Any, records: Sequence[Sequence[Any]], table: str) -> int:
    """Stream the batch into PostgreSQL with COPY, binding nothing.

    COPY streams, so there is no temp file to write, parse or fail to clean up.

    Text format rather than binary. Binary is a little faster and requires every value to
    already be of the column's Python type; these rows come from Azure's CSV reports, where a
    date is the string '2026-01-05' and binary COPY rejects it. Text format hands the value to
    the same server-side parser the column would use anyway.

    psycopg escapes on the way out, so a resource group containing a comma, a tag holding a
    newline, and NULL against empty string are all just values. test_bulk_insert.py checks that
    against the same hostile inputs.
    """
    names = ",".join(f'"{c}"' for c in COLUMNS)
    with con.raw.cursor() as cur, cur.copy(f"COPY {table} ({names}) FROM STDIN") as copy:
        for row in records:
            copy.write_row(row)
    return len(records)


def _insert_via_binding(con: Any, records: Sequence[Sequence[Any]],
                        table: str, chunk: int) -> int:
    names = ",".join(f'"{c}"' for c in COLUMNS)
    one = "(" + ",".join("?" * len(COLUMNS)) + ")"
    for i in range(0, len(records), chunk):
        batch = records[i:i + chunk]
        con.execute(
            f"INSERT INTO {table} ({names}) VALUES " + ",".join([one] * len(batch)),
            [v for row in batch for v in row],
        )
    return len(records)


def canonical_sub_id(value: Any) -> str | None:
    """One canonical form for a subscription id, whatever shape it arrived in.

    FOCUS exports identify a subscription by its ARM path — `/subscriptions/<guid>` — while the
    Query API, the Cost Details report and Azure's own ActualCost exports use the bare GUID.
    Both are correct; they are just different spellings of the same subscription.

    Storing them verbatim meant one subscription existed twice. Every write replaces the range
    it covers with `WHERE "SubAccountId" = ?`, so a FOCUS load never deleted the report path's
    rows and the report path never deleted FOCUS's: both survived and both were counted. The
    dashboard read $920.98 for a month Azure billed at $476.07 — not a rounding error, and not
    visibly wrong from the screen either, because a doubled total still looks like money.
    """
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    # Case-folded because ARM treats GUIDs case-insensitively and different APIs have shipped
    # different casings; two spellings of one subscription is the whole bug being fixed here.
    if "/subscriptions/" in text.lower():
        text = text.rstrip("/").rsplit("/", 1)[-1]
    return text.lower()


def _retry_after(resp: httpx.Response, attempt: int) -> float:
    """Seconds to wait, preferring the service's own guidance over our backoff."""
    for header in RETRY_HEADERS:
        value = resp.headers.get(header)
        if value:
            try:
                return min(float(value) + 2, 120.0)
            except ValueError:
                pass
    # The details API commonly asks for ~60s; start there rather than hammering.
    return min(60.0 * (attempt + 1), 180.0)


def _query_failure(resp: httpx.Response | None) -> str:
    """What went wrong with a query, phrased for the person who pressed the button.

    A raw 429 body — `{"error":{"code":"429","message":"Too many requests. Please retry."}}` —
    is the least useful thing to show here, because it invites exactly the wrong response:
    pressing Refresh again, which spends more of the quota that has just run out.

    The wait is quoted from the service rather than guessed. The previous wording invented a
    recovery time of "several minutes", which was wrong in both directions: the client-type
    limit that actually fires refills in seconds, and the advice sent people away from a button
    that would have worked on the next attempt.
    """
    if resp is None:
        return "no response from the query API"
    if resp.status_code == 429:
        wait = ""
        for header in RETRY_HEADERS:
            value = resp.headers.get(header)
            if value:
                try:
                    wait = f" Azure asks for about {int(float(value))}s."
                    break
                except ValueError:
                    pass
        return ("Cost Management is rate-limiting queries for this subscription." + wait +
                " Retrying sooner than that spends what is left of the allowance."
                " FOCUS report reads blob storage instead and is not affected.")
    if resp.status_code in (401, 403):
        return (f"Azure refused the query ({resp.status_code}). This needs Cost Management "
                "Reader on the subscription.")
    return f"query failed {resp.status_code}: {resp.text[:200]}"


def _query_wait(resp: httpx.Response, attempt: int) -> float:
    """Seconds to wait after a 429 from the *query* endpoint.

    Separate from `_retry_after` because the two APIs fail on different timescales. A report
    submission is genuinely minutes of work and asks for ~60s; a query answers in single-digit
    seconds, so borrowing that number spent a minute asleep to save an eight-second call and
    made the fast path slower than the one it was meant to replace.
    """
    for header in RETRY_HEADERS:
        value = resp.headers.get(header)
        if value:
            try:
                # Still capped: the service occasionally asks for minutes on a shared limit,
                # and a fast refresh that sleeps that long has stopped being one.
                return min(float(value) + 1, 60.0)
            except ValueError:
                pass
    return QUERY_BACKOFF[min(attempt, len(QUERY_BACKOFF) - 1)]


def _to_date(value: str | None) -> str | None:
    """Azure emits MM/DD/YYYY in MCA exports and ISO elsewhere; accept both."""
    if not value:
        return None
    v = value.strip()
    for fmt in ("%m/%d/%Y", "%Y-%m-%d", "%Y%m%d", "%d/%m/%Y"):
        try:
            return datetime.strptime(v, fmt).date().isoformat()
        except ValueError:
            continue
    try:
        return datetime.fromisoformat(v.replace("Z", "+00:00")).date().isoformat()
    except ValueError:
        return None


def _periods(months: int) -> list[tuple[str, str, str]]:
    """Whole calendar months, oldest first, ending with the current month to date."""
    today = datetime.now(UTC).date()
    out: list[tuple[str, str, str]] = []
    first = today.replace(day=1)
    starts = [first]
    for _ in range(months - 1):
        starts.append((starts[-1] - timedelta(days=1)).replace(day=1))
    for s in reversed(starts):
        if s == first:
            out.append((s.isoformat(), today.isoformat(), f"{s:%Y-%m} (MTD)"))
        else:
            last = (s.replace(day=28) + timedelta(days=4)).replace(day=1) - timedelta(days=1)
            out.append((s.isoformat(), last.isoformat(), f"{s:%Y-%m}"))
    return out


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


warehouse = Warehouse()
