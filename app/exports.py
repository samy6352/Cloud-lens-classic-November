"""Ingest Azure Cost Management **exports** from blob storage.

Why this exists
---------------
The `generateCostDetailsReport` API (see warehouse.py) generates a report per subscription per
period on demand. Measured on a real tenant that is ~115 seconds per subscription-month, so a
50-subscription customer wanting 12 months is roughly 19 hours of polling. That is fine for a
laptop demo and useless for an enterprise.

A **Cost Management export** inverts the cost model: Azure writes the data on a schedule, once,
at whatever scope you choose — subscription, resource group, management group or billing
account. Reading it is then just downloading files. One export at billing-account scope covers a
whole tenant, and a daily export keeps it current with no polling at all.

This module discovers exports, finds the newest run, and loads the files into the same `costs`
table the rest of the app already queries.

Access reality
--------------
In a governed tenant the export's storage account usually has `publicNetworkAccess=Disabled`, so
the blob data plane returns 403 from an ordinary workstation even for an owner. Verified on this
tenant. Two supported paths, in order:

  * **direct** — read blobs with the caller's AAD token. Works when the account allows access
    from where you are running (or you are inside the VNet / have a private endpoint).
  * **sas** — the operator supplies a container SAS URL. This is the pragmatic path for locked
    down accounts: a short-lived read-only SAS can be issued without changing network policy.

Both are read-only. Nothing here writes to storage.
"""
from __future__ import annotations

import asyncio
import csv
import gzip
import hashlib
import io
import json
import logging
import os
import re
from collections.abc import Iterable
from contextlib import ExitStack, nullcontext
from datetime import UTC, date, datetime, timedelta
from typing import Any
from urllib.parse import urlparse
from uuid import uuid4

import httpx

from . import staging
from .cost import (
    ARM,
    CostError,
    azure,
    list_billing_accounts,
    list_billing_subscriptions,
    list_subscriptions,
)
from .scopes import FOCUS, Manifest, Scope, latest_per_period, plan_scopes
from .warehouse import (
    COLUMNS,
    NULLABLE_NUMERIC,
    NUMERIC,
    _BillingMapping,
    _nullable_number,
    _to_date,
    canonical_sub_id,
    warehouse,
)

log = logging.getLogger("cloudlens.exports")

EXPORT_API = "2023-08-01"
# Exports created through some portal flows are only visible on the preview API — verified on
# this tenant, where the GA version returns an empty list while the preview returns nine.
# Discovery therefore tries several versions and keeps whichever returns the most.
#
# 2025-03-01 leads because it is the GA version that finally sees FocusCost: on this tenant
# 2023-08-01 and 2023-03-01 each return two exports and omit both FOCUS ones entirely, which
# is a silent omission — the app reported "no FOCUS export" for exports that plainly existed.
EXPORT_API_VERSIONS = ("2025-03-01", "2023-07-01-preview", "2023-08-01", "2023-03-01")
STORAGE_API = "2023-05-01"
BLOB_VERSION = "2021-12-02"
BLOB_RESOURCE = "https://storage.azure.com"

# Cost Management writes a manifest next to each run; the data files sit beside it. FOCUS exports
# default to Parquet in newer portal flows, so both are supported — Parquet is both smaller over
# the wire and faster to load than CSV.
_DATA_SUFFIX = (".csv", ".csv.gz", ".parquet", ".parquet.gz", ".snappy.parquet")

# Rows decoded from Parquet at a time. Large enough that per-batch overhead is irrelevant, small
# enough that a single batch of a wide FOCUS file is a few MB rather than the whole export.
_PARQUET_BATCH = 65536


class ExportError(CostError):
    """Raised for problems the operator can fix (access, missing export, empty run)."""


# --------------------------------------------------------------------- discovery
def uses_deployment_storage(export: dict[str, Any]) -> bool:
    """Automatic imports stay in the configured account; explicit imports may use another."""
    from .archive import ACCOUNT, ACCOUNT_ID

    if ACCOUNT_ID:
        actual = str(export.get("storage_resource_id") or "").rstrip("/").lower()
        return actual == ACCOUNT_ID.rstrip("/").lower()
    return bool(ACCOUNT) and str(export.get("storage_account") or "").lower() == ACCOUNT.lower()


def deployment_export_hint(metric: str | None = None) -> str:
    from .archive import ACCOUNT

    if not ACCOUNT:
        return ("No export storage is configured for this deployment. Provision its dedicated "
                "storage before setting up exports. Quick refresh reads estimates through ARM.")
    return (f"No matching {metric or 'Cost Management'} export targets this deployment's "
            f"storage account {ACCOUNT}. Other deployments' storage is not used automatically, "
            "and usually cannot be: those accounts are private-endpoint only and do not resolve "
            "from this app's network. Provision this deployment's own exports by running "
            "infra/focus-exports.ps1 from the CloudLens repository — it needs an operator's "
            "rights once, and is safe to re-run. Quick refresh reads estimates through ARM "
            "meanwhile, without the hourly quantities, meters and tags a FOCUS report carries.")


async def resolve_scopes(subscription_ids: list[str] | None = None) -> dict[str, Any]:
    """Decide which scopes to ask, preferring one billing scope over N subscriptions.

    Billing discovery is best-effort and additive: without billing-reader rights, or on an
    agreement that cannot carry FOCUS, this returns exactly the per-subscription list the app
    used before. The scalable path is taken when it is available, and nothing breaks when it
    is not.
    """
    subs = ([{"id": s} for s in subscription_ids] if subscription_ids
            else (await list_subscriptions())["subscriptions"])

    accounts = await list_billing_accounts()
    if accounts:
        # Only ask about coverage for accounts that could actually carry the data -- one extra
        # request per account, and pointless for an agreement FOCUS is unavailable on.
        usable = [a for a in accounts
                  if Scope.billing_account(a["name"],
                                           agreement=a.get("agreementType")).supports(FOCUS)]
        covered = await asyncio.gather(
            *(list_billing_subscriptions(a["name"]) for a in usable))
        for account, subs_for_account in zip(usable, covered):
            account["subscriptions"] = subs_for_account

    plan = plan_scopes(subs, accounts)
    if plan["calls_saved"]:
        log.info("exports: %s scope(s) replace %d per-subscription call(s)",
                 len(plan["scopes"]), plan["calls_saved"])
    for rejection in plan["rejected"]:
        log.info("exports: %s", rejection["reason"])
    return plan


async def discover_exports(subscription_ids: list[str] | None = None,
                           scopes: list[Scope] | None = None, *,
                           deployment_only: bool = False) -> dict[str, Any]:
    """List Cost Management exports the caller can see, with their destination and schedule.

    With no arguments this now plans its own scopes, so a billing-account export is found and
    preferred wherever one exists. Passing `subscription_ids` still narrows to those
    subscriptions -- which is what the scoped routes want -- and passing `scopes` bypasses
    planning entirely.

    Deployment-owned exports are created at subscription scope. Automatic imports must
    query those scopes directly, even when billing scope planning would otherwise replace them.
    """
    plan: dict[str, Any] | None = None
    if deployment_only:
        ids = (subscription_ids if subscription_ids is not None
               else [sub["id"] for sub in (await list_subscriptions())["subscriptions"]])
        scopes = [Scope.subscription(sub) for sub in ids]
        plan = {"strategy": "deployment-owned"}
    elif scopes is None:
        plan = await resolve_scopes(subscription_ids)
        scopes = plan["scopes"]

    async def one(scope: Scope) -> list[dict]:
        payload: dict[str, Any] = {}
        for version in EXPORT_API_VERSIONS:
            try:
                candidate = await azure.request(
                    "GET", f"{ARM}{scope.path}/providers/Microsoft.CostManagement/exports",
                    params={"api-version": version},
                )
            except Exception as exc:  # noqa: BLE001 - try the next version
                log.debug("export list failed for %s @ %s: %s", scope.path, version, exc)
                continue
            if len(candidate.get("value") or []) > len(payload.get("value") or []):
                payload = candidate
        if not payload.get("value"):
            return []

        out = []
        for e in payload.get("value", []):
            p = e.get("properties", {})
            dest = (p.get("deliveryInfo") or {}).get("destination", {}) or {}
            defn = p.get("definition", {}) or {}
            account = str(dest.get("resourceId", "")).split("/")[-1]
            out.append({
                "id": e.get("id"),                              # unique; names repeat across subs
                "name": e.get("name"),
                "scope": scope.path,
                "scope_kind": scope.kind,
                # Kept so existing callers that key on this keep working. It is None for a
                # billing scope, which genuinely has no single subscription -- better than
                # naming an arbitrary one.
                "subscription_id": scope.subscription_id,
                "type": defn.get("type"),                      # FocusCost | ActualCost | AmortizedCost
                "timeframe": defn.get("timeframe"),
                "granularity": ((defn.get("dataSet") or {}).get("granularity")),
                "format": p.get("format"),
                "compressed": (p.get("compressionMode") or "").lower() == "gzip",
                "partitioned": bool(p.get("partitionData")),
                "storage_account": account,
                "storage_resource_id": dest.get("resourceId"),
                "container": dest.get("container"),
                "root_folder": dest.get("rootFolderPath"),
                "schedule": ((p.get("schedule") or {}).get("recurrence")),
                "status": ((p.get("schedule") or {}).get("status")),
                "next_run": p.get("nextRunTimeEstimate"),
            })
        return out

    results = await asyncio.gather(*(one(s) for s in scopes))
    exports = [e for r in results for e in r]
    if deployment_only:
        exports = [e for e in exports if uses_deployment_storage(e)]

    # A FOCUS export is preferred: one stable schema across billing account types. Within that,
    # a billing-scope export beats a subscription one -- it is the same data in one file set
    # instead of N, so loading it is both cheaper and impossible to half-do.
    #
    # Ahead of all of that sits an export that writes to *this deployment's own* storage
    # account. That account is created alongside the app, in the same resource group, with a
    # private endpoint in the app's own virtual network -- so it is the one destination that is
    # reachable by construction. Every other export belongs to somebody else's resource group
    # and its storage is usually private-endpoint only from somewhere the app cannot see, which
    # is how a deployment ends up ranking an unreadable export first and reporting a DNS
    # failure instead of loading cost.
    def rank(e: dict) -> tuple:
        return (
            not uses_deployment_storage(e),
            e["type"] != "FocusCost",
            e["type"] != "AmortizedCost",
            e.get("scope_kind") == "subscription",
        )

    ranked = sorted(exports, key=rank)
    return {
        "count": len(exports),
        "exports": ranked,
        "scopes": [s.path for s in scopes],
        # How the scopes were chosen, when this call chose them. Surfaced so the onboarding
        # and export tabs can say why a tenant is on the slow path -- "no billing account can
        # carry FOCUS" is actionable, "it's slow" is not.
        "strategy": (plan or {}).get("strategy"),
        "calls_saved": (plan or {}).get("calls_saved"),
        "scope_notes": [r["reason"] for r in (plan or {}).get("rejected", [])] or None,
        "note": (
            "No Cost Management exports found. Create one (FOCUS format, daily, to a storage "
            "account you control) and re-run — that is the scalable path for a large tenant."
            if not exports else None
        ),
    }


def covers_current_period(export: dict[str, Any]) -> bool:
    """Whether this export's timeframe includes today.

    Cost Management exports come in two useful flavours and they are not alternatives:
    a MonthToDate export is rewritten daily and covers the month in progress, while a
    TheLastMonth export is written once a month and covers only closed months. Loading either
    one alone leaves a hole — the current month, or all the history before it.

    This mattered in a way that was easy to miss. The refresh loaded the first export that
    worked, which happened to be the closed-month one, so a FOCUS refresh replaced June and
    July and left August exactly as it was. Every figure for the current month stayed at
    whatever had loaded it last, and after a quick refresh (which cannot fetch tags) that meant
    Cost by Tag stayed empty and looked like an export problem. It was a coverage problem.
    """
    return (export.get("timeframe") or "") in {
        "MonthToDate", "BillingMonthToDate", "TheCurrentMonth", "WeekToDate",
        "TheCurrentQuarter", "TheCurrentYear",
    }


async def export_runs(scope: str | Scope, export_name: str, limit: int = 5) -> list[dict]:
    """Recent runs of an export, newest first.

    Takes a scope rather than a subscription id, because an export at billing-account scope has
    no subscription to name -- asking for its run history under `/subscriptions/...` is a 404.
    A bare subscription GUID is still accepted, so existing callers keep working.
    """
    target = scope if isinstance(scope, Scope) else Scope.parse(scope)
    payload: dict[str, Any] = {}
    for version in EXPORT_API_VERSIONS:
        try:
            payload = await azure.request(
                "GET",
                f"{ARM}{target.path}/providers/Microsoft.CostManagement"
                f"/exports/{export_name}/runHistory",
                params={"api-version": version},
            )
            if payload.get("value"):
                break
        except Exception:  # noqa: BLE001 - try the next version
            continue
    runs = []
    for r in payload.get("value", []):
        p = r.get("properties", {})
        runs.append({
            "id": r.get("name"),
            "status": p.get("status"),
            "submitted": p.get("submittedTime"),
            "processing_start": p.get("processingStartTime"),
            "processing_end": p.get("processingEndTime"),
            "file_name": p.get("fileName"),
        })
    runs.sort(key=lambda r: r.get("processing_end") or r.get("submitted") or "", reverse=True)
    return runs[:limit]


# ----------------------------------------------------------------- blob access
class BlobReader:
    """Minimal read-only blob client supporting an AAD token or a container SAS."""

    def __init__(self, account: str, container: str, sas: str | None = None) -> None:
        self.account = account
        self.container = container
        self.sas = (sas or "").lstrip("?") or None
        self._token: str | None = None
        self._credential: Any = None

    @classmethod
    def from_sas_url(cls, url: str) -> BlobReader:
        """Accept a full container SAS URL: https://acct.blob.core.windows.net/container?sv=..."""
        parts = urlparse(url)
        account = parts.netloc.split(".")[0]
        container = parts.path.strip("/").split("/")[0]
        if not account or not container:
            raise ExportError("That does not look like a container SAS URL.")
        return cls(account, container, parts.query)

    async def _headers(self) -> dict[str, str]:
        h = {"x-ms-version": BLOB_VERSION}
        if self.sas:
            return h
        if self._token is None:
            from azure.identity.aio import DefaultAzureCredential

            from .identity import interactive_allowed

            self._credential = DefaultAzureCredential(
                exclude_interactive_browser_credential=not interactive_allowed())
            self._token = (await self._credential.get_token(f"{BLOB_RESOURCE}/.default")).token
        h["Authorization"] = f"Bearer {self._token}"
        return h

    def _url(self, path: str = "", query: str = "") -> str:
        base = f"https://{self.account}.blob.core.windows.net/{self.container}"
        if path:
            base += "/" + path.lstrip("/")
        parts = [q for q in (query, self.sas) if q]
        return base + ("?" + "&".join(parts) if parts else "")

    async def close(self) -> None:
        if self._credential is not None:
            await self._credential.close()
            self._credential = None

    async def list(self, prefix: str = "", limit: int | None = None,
                   require_complete: bool = True) -> list[dict[str, Any]]:
        """List blobs under a prefix. Returns name, size and last-modified."""
        found: list[dict[str, Any]] = []
        limit = limit if limit is not None else int(os.getenv("EXPORT_MAX_BLOBS", "10000"))
        marker = ""
        async with httpx.AsyncClient(timeout=90) as client:
            while len(found) < limit:
                from urllib.parse import urlencode
                q = urlencode({"restype": "container", "comp": "list",
                               "maxresults": min(1000, limit), "prefix": prefix,
                               **({"marker": marker} if marker else {})})
                try:
                    r = await client.get(self._url(query=q), headers=await self._headers())
                except httpx.ConnectError as exc:
                    # A private-endpoint-only account does not resolve publicly at all.
                    raise ExportError(
                        f"Could not reach storage account '{self.account}' — it does not resolve "
                        "from here, which usually means it is private-endpoint only. Run this "
                        "from inside the VNet, or supply a container SAS URL."
                    ) from exc

                if r.status_code == 403:
                    raise ExportError(
                        f"Access to storage account '{self.account}' was denied. The account "
                        "likely has public network access disabled, or you lack Storage Blob "
                        "Data Reader. Supply a container SAS URL instead, or run from inside "
                        "the network."
                    )
                if r.status_code == 404:
                    raise ExportError(f"Container '{self.container}' not found on '{self.account}'.")
                if r.status_code >= 400:
                    error = ExportError(f"Listing blobs failed ({r.status_code}): {r.text[:200]}")
                    error.response = r
                    raise error

                body = r.text
                for m in re.finditer(r"<Blob>(.*?)</Blob>", body, re.S):
                    blob = m.group(1)
                    name = _tag(blob, "Name")
                    if not name:
                        continue
                    found.append({
                        "name": name,
                        "size": int(_tag(blob, "Content-Length") or 0),
                        "modified": _tag(blob, "Last-Modified"),
                        "etag": _tag(blob, "Etag") or _tag(blob, "ETag"),
                    })
                marker = _tag(body, "NextMarker") or ""
                if not marker:
                    break
        if require_complete and (marker or len(found) > limit):
            raise ExportError(
                f"Blob listing exceeds its {limit} file safety limit. "
                "Narrow the export prefix or raise EXPORT_MAX_BLOBS; no data was changed.")
        return found[:limit]

    async def get(self, name: str) -> bytes:
        async with (
            httpx.AsyncClient(timeout=300) as client,
            client.stream("GET", self._url(name), headers=await self._headers()) as response,
        ):
            if response.status_code >= 400:
                error = ExportError(f"Downloading '{name}' failed ({response.status_code}).")
                error.response = response
                raise error
            content = bytearray()
            async for chunk in response.aiter_bytes(chunk_size=64 * 1024):
                if len(content) + len(chunk) > EXPORT_MAX_BYTES:
                    raise ExportError("Export file exceeds the memory safety limit; no data was changed.")
                content.extend(chunk)
            return bytes(content)


def _tag(xml: str, tag: str) -> str | None:
    from html import unescape

    m = re.search(rf"<{tag}>(.*?)</{tag}>", xml, re.S)
    return unescape(m.group(1)) if m else None


# --------------------------------------------------------------------- loading
def _map_header(header: list[str]) -> dict[str, int]:
    """Map our stable schema onto whatever column names this export uses.

    FOCUS exports use FOCUS names (BilledCost, ChargePeriodStart, SubAccountId...), while
    ActualCost/AmortizedCost exports use Azure's own (costInBillingCurrency, date,
    SubscriptionId...). warehouse.COLUMNS already lists the Azure aliases; the FOCUS names are
    added here so one loader handles both.
    """
    focus_aliases = {
        "ChargePeriodStart": ["chargeperiodstart"],
        "BilledCost": ["billedcost", "effectivecost"],
        "CostInUsd": ["billedcostinusd", "effectivecostinusd"],
        "BillingCurrency": ["billingcurrency"],
        "SubAccountId": ["subaccountid"],
        "SubAccountName": ["subaccountname"],
        "ResourceGroup": ["x_resourcegroupname"],
        "ResourceId": ["resourceid"],
        "ResourceName": ["resourcename"],
        "ServiceName": ["servicename"],
        "ServiceFamily": ["servicecategory"],
        "ServiceSubcategory": ["x_servicesubcategory"],
        "MeterName": ["x_metername", "skumeter"],
        "ProductName": ["x_skudescription", "skuid"],
        "RegionName": ["regionname", "regionid"],
        "PricingQuantity": ["pricingquantity", "consumedquantity"],
        "UnitOfMeasure": ["pricingunit", "consumedunit"],
        "UnitPrice": ["listunitprice", "contractedunitprice"],
        "ChargeCategory": ["chargecategory"],
        "PricingModel": ["x_pricingmodel", "commitmentdiscountcategory"],
        "BenefitName": ["commitmentdiscountname"],
        "PublisherName": ["publishername"],
        "CostCenter": ["x_costcenter"],
        "Tags": ["tags"],
    }

    lower = {h.strip().lower().replace(" ", "").replace("_", ""): i for i, h in enumerate(header)}
    out: dict[str, int] = {}
    for target, aliases in COLUMNS.items():
        for alias in list(aliases) + [a.replace("_", "") for a in focus_aliases.get(target, [])]:
            if alias in lower:
                out[target] = lower[alias]
                break
    return out


EXPORT_MAX_BYTES = 64 * 1024 * 1024
# How many parsed rows are held before being copied into the staging table. This is
# the whole of the importer's row-shaped memory now: a batch, not an export.
#
# It replaces EXPORT_MAX_ROWS, which was 100,000 and was a cap rather than a batch --
# an export larger than it was refused outright. A real customer month is 5.8M rows,
# so that limit was not protecting the importer from a pathological export, it was
# stopping it from doing its job.
PARSE_BATCH = int(os.getenv("EXPORT_PARSE_BATCH", "50000"))


def _export_payload(raw: bytes) -> bytes:
    if raw[:2] == b"\x1f\x8b":
        with gzip.GzipFile(fileobj=io.BytesIO(raw)) as compressed:
            raw = compressed.read(EXPORT_MAX_BYTES + 1)
    if len(raw) > EXPORT_MAX_BYTES:
        raise ExportError("Decoded export exceeds the memory safety limit; no data was changed.")
    return raw


def _rows_from(raw: bytes) -> tuple[list[str], Iterable[list[str]]]:
    raw = _export_payload(raw)
    text = raw.decode("utf-8-sig", errors="replace")
    reader = csv.reader(io.StringIO(text))
    header = next(reader, [])
    return header, reader


def _rows_from_parquet(raw: bytes) -> tuple[list[str], Iterable[list[str]]]:
    """Read a Parquet export via pyarrow, the reference implementation of the format.

    Values are stringified so the single CSV/Parquet code path downstream stays identical;
    the numeric and date coercion in _load_rows then applies uniformly to both.

    Read from memory rather than through a temporary file. The bytes are already in hand,
    and a FOCUS export is large enough that writing it to disk only to read it straight
    back was doubling the I/O of the fastest ingestion path for no gain.

    Batches are consumed lazily, with decoded size and row limits enforced before replacement.
    """
    import pyarrow.parquet as pq

    raw = _export_payload(raw)
    reader = pq.ParquetFile(io.BytesIO(raw))
    header = list(reader.schema_arrow.names)
    # No row-count ceiling: rows are yielded in batches and staged as they are parsed,
    # so a large partition costs a batch rather than all of it. `EXPORT_MAX_BYTES`
    # below still bounds what a single file may decode to, which is the memory this
    # function actually holds.

    def rows():
        decoded_bytes = 0
        for batch in reader.iter_batches(batch_size=min(_PARQUET_BATCH, 1024)):
            decoded_bytes += batch.nbytes
            if decoded_bytes > EXPORT_MAX_BYTES:
                raise ExportError("Decoded export exceeds the memory safety limit; no data was changed.")
            columns = [col.to_pylist() for col in batch.columns]
            yield from (["" if value is None else str(value) for value in values]
                        for values in zip(*columns, strict=True))
    return header, rows()


def _read_any(name: str, raw: bytes) -> tuple[list[str], Iterable[list[str]]]:
    return _rows_from_parquet(raw) if ".parquet" in name.lower() else _rows_from(raw)


def _load_rows(header: list[str], reader: Iterable[list[str]],
               fallback_sub: str | None,
               metric: str | None = None,
               flush: Any = None,
               batch: int = PARSE_BATCH) -> tuple[list[list[Any]], int]:
    """Parse an export's rows into warehouse column order.

    `flush`, when given, is handed each batch of `batch` parsed rows and the list is
    then dropped, so a single multi-million-row blob costs one batch of memory rather
    than all of it. Callers that omit it get every row back at once, which is what the
    tests and the small-export paths want.
    """
    idx = _map_header(header)
    billing = _BillingMapping(header, metric)
    if "BilledCost" not in idx:
        raise ExportError(f"No cost column in the export; first columns were {header[:10]}")

    keys = list(COLUMNS)
    i_rid, i_rname = keys.index("ResourceId"), keys.index("ResourceName")
    i_sid = keys.index("SubAccountId")
    i_tags, source_tags = keys.index("Tags"), idx.get("Tags")
    focus_tags = (not billing.saved and billing.basis == "FOCUS"
                  and "chargeperiodstart" in billing.at)

    records: list[list[Any]] = []
    skipped = 0
    for row in reader:
        if flush is not None and len(records) >= batch:
            flush(records)
            records = []
        if not row:
            continue
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
        # A genuine empty FOCUS column is known untagged; an absent column is unknown.
        # Saved warehouse snapshots always have Tags, so must retain their original NULL.
        if (focus_tags and source_tags is not None and len(row) == len(header)
                and (row[source_tags] is None or str(row[source_tags]).strip() in ("", "null"))):
            rec[i_tags] = "{}"
        if not rec[keys.index("ChargePeriodStart")]:
            skipped += 1
            continue
        if not rec[i_rname] and rec[i_rid]:
            rec[i_rname] = str(rec[i_rid]).rstrip("/").split("/")[-1]
        # Canonical subscription id. A FOCUS file spells this as `/subscriptions/<guid>` while
        # the Query API and Cost Details reports use the bare GUID, so storing it verbatim put
        # one subscription in the table twice — and every replace-on-write missed the other
        # copy, so both were kept and both were counted.
        rec[i_sid] = canonical_sub_id(rec[i_sid]) or canonical_sub_id(fallback_sub)
        records.append(rec)
    return records, skipped


def _validated_focus_rows(header: list[str], rows: Iterable[list[str]]) -> Iterable[list[str]]:
    """Do not let legacy coercion turn a truncated or malformed FOCUS charge into zero."""
    names = [h.strip().lower() for h in header]
    if len(set(names)) != len(names):
        raise ExportError("The FOCUS header repeats a column. No data was changed.")
    amounts = [names.index(name) for name in ("billedcost", "effectivecost")]
    currency = names.index("billingcurrency")
    numeric = {i for target, i in _map_header(header).items() if target in NUMERIC}
    for row in rows:
        if not row:
            continue
        if len(row) != len(header) or not str(row[currency]).strip():
            raise ExportError("The FOCUS run contains an incomplete row. No data was changed.")
        if any(_nullable_number(row[i]) is None for i in amounts) or any(
                str(row[i]).strip() and _nullable_number(row[i]) is None for i in numeric):
            raise ExportError("The FOCUS run contains invalid numeric data. No data was changed.")
        yield row


def _focus_windows(manifests: list[Manifest]) -> dict[str, tuple[str, str]]:
    """Associate every partition with one validated, non-overlapping complete run window."""
    files: dict[str, tuple[str, str]] = {}
    periods: list[tuple[str, str]] = []
    for manifest in manifests:
        start, end, submitted = (_to_date(value) for value in
                                 (manifest.start, manifest.end, manifest.submitted))
        if not start or not end or not submitted:
            raise ExportError("Invalid manifest period or submission date; no data was changed.")
        if manifest.timeframe in {"MonthToDate", "BillingMonthToDate"}:
            end = min(end, submitted)
        if start > end or any(start <= hi and end >= lo for lo, hi in periods):
            raise ExportError("Invalid or overlapping FOCUS manifest periods; no data was changed.")
        periods.append((start, end))
        for blob in manifest.blobs:
            name = blob.name.lstrip("/")
            if name in files:
                raise ExportError("FOCUS runs share a data partition; no data was changed.")
            files[name] = (start, end)
    return files


def _reconcile_staged(con: Any, staged: Any,
                      windows: dict[str, list[tuple[str, str]]], *,
                      protected: set[tuple[str, str]] | None = None) -> dict[str, Any]:
    """Compare complete row multisets under the writer lock; replace only changed days.

    There is no natural usage-row key. Comparing identical rows by multiplicity, rather
    than deduplicating them or comparing summed money, preserves legitimate duplicates
    and zero-net credits.

    The comparison happens in the database rather than in this process. It used to read
    every incoming row into a `Counter` and stream the stored rows past it, which is why
    the importer was capped at 100,000 rows: a real customer month is 5.8M, and holding
    it cost several gigabytes before the stored side was even looked at. `staging`
    answers the same question with `EXCEPT ALL` in both directions -- see its module
    docstring for why `ALL` is what makes the two equivalent.
    """
    protected = protected or set()
    if protected:
        # Days a newer compact correction already owns are not this run's to restate,
        # so they leave the comparison entirely rather than being filtered out of it.
        staged.drop_protected(protected)

    changed = staged.changed_days(windows, protected)
    incoming = staged.day_rows()
    deleted = staged.stored_rows_on(changed)

    for sub, day in changed:
        # A scoped SAS import may not issue receipts of its own. Never leave an
        # older run's receipt certifying a day whose row multiset is being changed.
        con.execute("DELETE FROM meta WHERE key = ?", [f"focus-day:{sub}:{day}"])
        con.execute('DELETE FROM costs WHERE "SubAccountId" = ? AND "ChargePeriodStart" = ?',
                    [sub, day])
    inserted = staged.publish(changed) if changed else 0

    changed_set = set(changed)
    unchanged = [day for day in incoming if day not in changed_set]
    return {
        "incremental": True,
        "changed_days": len(changed),
        # The days themselves, not merely how many there were. The derived tables are
        # rebuilt only for the months an import actually touched, and a count cannot
        # say which months those are. Deduplicated across subscriptions because the
        # rebuild window is a span of charge dates, not a per-subscription thing.
        "changed_dates": sorted({day for _, day in changed}),
        "unchanged_days": len(unchanged),
        "inserted_rows": inserted,
        "deleted_rows": deleted,
        "unchanged_rows": sum(incoming[day] for day in unchanged),
        "preserved_newer_days": len(protected),
    }


def _derived_is_current() -> bool:
    """Whether the derived tables already describe the generation `costs` is at.

    The generation is advanced inside the write transaction, and only when that
    write actually changed something. So this single question answers both of the
    ones the caller has: an import that changed nothing leaves the tables current
    and needs no rebuild at all, and an import whose previous rebuild failed or was
    interrupted leaves them stale and gets one even though it changed nothing
    itself. Cheap -- two rows of `meta`.
    """
    from . import jobs

    with warehouse.connect(read_only=True) as con:
        return jobs.rollup_current(con)


def _rebuild_window(changes: dict[str, Any]) -> tuple[date, date] | None:
    """The whole-month span to rebuild the derived tables over, or None for all of it.

    A full rebuild costs one scan of every retained month. That was acceptable when
    the data was loaded once; under the customer's daily FOCUS sync it is the same
    scan of a year of history repeated every day to correct one day's rows, so the
    cost of staying fresh grows with retention while the change stays constant.

    `_reconcile_focus` already knows exactly which days it replaced, so the rebuild
    can be narrowed to the months those fall in -- and because `month_window` snaps
    outwards to month boundaries, that prunes to the matching `costs` partitions.

    None means "rebuild everything", and is returned for anything this cannot state
    with certainty: a non-incremental import, which deletes and reloads whole
    windows rather than reconciling days, and an unparseable date. Returning a
    narrow window on uncertain input is the one way this can leave a quietly stale
    dashboard, so it declines the optimisation instead of guessing. `refresh_rollup`
    independently ignores any window when the previous rebuild did not complete.
    """
    from .rollup import month_window

    if not changes.get("incremental"):
        return None
    days = []
    for value in changes.get("changed_dates") or []:
        try:
            days.append(date.fromisoformat(str(value)[:10]))
        except (TypeError, ValueError):
            return None
    return month_window(days)


async def describe_export(account: str | None = None, container: str | None = None,
                          prefix: str = "", sas_url: str | None = None) -> dict[str, Any]:
    """What columns the newest export file actually provides, and which we can use.

    "Cost by Tag is empty after a FOCUS load" is not answerable from the dashboard: the tab
    cannot tell a missing column from an untagged estate, and the loader silently fills any
    column it cannot find with NULL. That silence is reasonable — a missing PublisherName should
    not fail a refresh — but it means an export that omits something important looks exactly
    like an estate that lacks it.

    So this reads one file and reports the mapping rather than the data.
    """
    reader = (BlobReader.from_sas_url(sas_url) if sas_url
              else BlobReader(account or "", container or ""))
    if not reader.account or not reader.container:
        raise ExportError("Provide either sas_url, or both account and container.")
    try:
        blobs = await reader.list(prefix=prefix.strip("/"))
        data = [b for b in blobs if b["name"].lower().endswith(_DATA_SUFFIX)]
        if not data:
            raise ExportError(f"No data files under '{reader.container}/{prefix}'.")
        newest = max(data, key=lambda b: b["modified"] or "")
        header, rows = _read_any(newest["name"], await reader.get(newest["name"]))
        idx = _map_header(header)
        # A column can be present and empty, which reads downstream exactly like a column that
        # is absent: the loader stores NULL either way. Sampling separates "the export has no
        # Tags column" from "the export has one and Azure left it blank", and those call for
        # completely different fixes.
        filled: dict[str, int] = {}
        sampled = 0
        for row in rows:
            if sampled >= 500:
                break
            if not row:
                continue
            sampled += 1
            for col, i in idx.items():
                if i < len(row) and str(row[i]).strip() not in ("", "{}", "[]"):
                    filled[col] = filled.get(col, 0) + 1
        return {
            "source": f"{reader.account}/{reader.container}/{prefix}".rstrip("/"),
            "file": newest["name"],
            "modified": newest["modified"],
            "columns_in_file": len(header),
            "mapped": sorted(idx),
            "missing": sorted(c for c in COLUMNS if c not in idx),
            "sampled_rows": sampled,
            "empty_though_present": sorted(c for c in idx if not filled.get(c)),
            # Named separately because its absence is the one silently misreported as a fact
            # about the customer's estate rather than about the export.
            "has_tags": "Tags" in idx,
            "rows_with_tags_in_sample": filled.get("Tags", 0),
        }
    finally:
        await reader.close()


async def read_manifests(reader: BlobReader, prefix: str = "",
                         limit: int | None = None,
                         required_metric: str | None = None) -> list[Manifest]:
    """Every run manifest under `prefix`, newest first.

    Cost Management writes `manifest.json` beside each run's data files, listing exactly which
    blobs belong to it and how many rows they hold. Reading it is the difference between knowing
    a run's contents and inferring them from names and timestamps -- which Microsoft warns
    against, and which merges two runs into one during the first five days of a month when two
    runs share a day.
    """
    blobs = await reader.list(prefix=prefix.strip("/"))
    found = [b for b in blobs if b["name"].rsplit("/", 1)[-1].lower() == "manifest.json"]
    found.sort(key=lambda b: b["modified"] or "", reverse=True)
    limit = limit if limit is not None else int(os.getenv("EXPORT_MAX_MANIFESTS", "2000"))
    if len(found) > limit:
        raise ExportError(
            f"Found {len(found)} manifests, exceeding EXPORT_MAX_MANIFESTS={limit}. "
            "No data was changed; narrow the prefix or raise the cap.")

    out: list[Manifest] = []
    reader.manifest_metadata = {}
    reader.manifest_bytes = 0
    for entry in found:
        name = entry["name"]
        try:
            document_bytes = await reader.get(name)
            reader.manifest_bytes += len(document_bytes)
            raw = json.loads(document_bytes)
            if (required_metric == "FocusCost" and isinstance(raw, dict)
                    and (raw.get("exportConfig") or {}).get("type") == "FocusCost"):
                # Zero must be explicit, not the parser's default for absent metadata. A
                # manifest is our proof of complete coverage, including empty partitions.
                counts = [raw.get("dataRowCount"), *[
                    blob.get("dataRowCount") for blob in raw.get("blobs") or []]]
                if any(type(count) is not int or count < 0 for count in counts):
                    raise ValueError("FOCUS manifest lacks valid complete row counts")
                if counts[0] != sum(counts[1:]):
                    raise ValueError("FOCUS manifest row counts disagree with its partitions")
            manifest = Manifest.parse(raw)
            out.append(manifest)
            reader.manifest_metadata[(manifest.run_id, manifest.submitted)] = {
                "name": name, "etag": entry.get("etag"),
                "sha256": hashlib.sha256(json.dumps(raw, sort_keys=True).encode()).hexdigest(),
                "document": raw,
            }
        except Exception as exc:
            raise ExportError(
                f"Cannot verify manifest {name}: {exc}. No data was changed; retry after "
                "Azure finishes the run.") from exc
    return out


def _blobs_from_manifests(manifests: list[Manifest]) -> list[dict[str, Any]]:
    """The data blobs the chosen runs declare, as the loader's blob dicts.

    Manifest blob names are relative to the container, the same as a listing, so they can be
    fetched directly. Duplicates are collapsed because a re-run of a period can name a file the
    previous run also named -- loading it twice is a double-count.
    """
    seen: set[str] = set()
    out: list[dict[str, Any]] = []
    for m in manifests:
        for blob in m.blobs:
            name = blob.name.lstrip("/")
            if name in seen:
                continue
            seen.add(name)
            out.append({"name": name, "size": blob.bytes, "modified": m.submitted,
                        "rows": blob.rows, "run_id": m.run_id, "metric": m.dataset})
    return out


async def ingest_export(
    account: str | None = None,
    container: str | None = None,
    prefix: str = "",
    sas_url: str | None = None,
    subscription_id: str | None = None,
    replace: bool = True,
    max_files: int = 60,
    metric: str | None = None,
    scope: list[str] | None = None,
    required_metric: str | None = None,
    export_name: str | None = None,
    run_identity: tuple[str, str] | None = None,
    requested_window: tuple[str, str] | None = None,
) -> dict[str, Any]:
    """Load the newest export run into the warehouse.

    Args:
        account: Storage account name (omit if using sas_url).
        container: Container name (omit if using sas_url).
        prefix: Folder path inside the container, e.g. 'focus/<sub-id>'.
        sas_url: A container SAS URL — the way in when the account blocks public access.
        subscription_id: Fill missing SubAccountId. For strict FOCUS this also asserts a
            single-subscription export's complete coverage; rows naming another subscription
            are refused. This attribution is required to reconcile an entirely empty run.
        replace: Replace covered data; strict FOCUS imports require True and change only days
            whose full row multiset differs from the warehouse.
        max_files: Safety cap on partitioned exports.
        metric: Authoritative export type, when known. Manifest metadata takes precedence.
        scope: Subscription ids to retain from the complete export; None means unrestricted.
        required_metric: Refuse other datasets; unlike metric, this is a selection constraint.

    Strict FocusCost results also include incremental, changed_days, unchanged_days,
    inserted_rows, deleted_rows and unchanged_rows. Days count subscription/day partitions
    present before or after the import (empty/empty days are not counted). The existing rows
    field remains the complete retained report count, not the number physically inserted.
    Empty runs need explicit zero manifest counts, valid FOCUS headers, and subscription_id:
    scope alone is an authorization filter, not evidence that an absent subscription is covered.
    Zero-valued charges are ordinary valid rows.
    """
    from .job_policy import require_writable
    require_writable()
    incremental = required_metric == "FocusCost"
    if incremental and not replace:
        raise ExportError("FOCUS imports require replace=True to prevent duplicate costs.")
    selected_subs = None if scope is None else {
        canonical_sub_id(sub) for sub in scope if canonical_sub_id(sub)
    }
    coverage_sub = canonical_sub_id(subscription_id) if incremental else None
    if selected_subs == set():
        raise ExportError("No subscriptions were selected. No data was changed.")
    if coverage_sub and selected_subs is not None and coverage_sub not in selected_subs:
        raise ExportError("The subscription export is outside the selected scope. No data was changed.")
    reader = (BlobReader.from_sas_url(sas_url) if sas_url
              else BlobReader(account or "", container or ""))
    if not reader.account or not reader.container:
        raise ExportError("Provide either sas_url, or both account and container.")

    started = datetime.now(UTC)
    # Report progress the same way the API path does, so the strip in the header has something
    # to show. An export ingest reads dozens of blobs and can take minutes; without this it was
    # the one refresh route that still looked like nothing was happening.
    warehouse.state = {"status": "running", "started": started.isoformat(timespec="seconds"),
                       "metric": metric, "total": 0, "done": 0, "rows": 0, "failed": 0,
                       "empty": 0, "progress": 0.0, "source": f"{reader.account}/{reader.container}"}
    # Named before the try so the finally can always clean them up, including when the
    # run fails before it gets as far as staging anything.
    resources = ExitStack()
    staged: Any = None
    total_rows = 0
    try:
        # Prefer the run's own manifest over inferring a run from blob names and timestamps.
        # The old heuristic took every data file sharing the newest minute-prefix, which merges
        # two runs into one during the first five days of a month -- when Cost Management writes
        # two runs per day, one for the month in progress and one restating the month just
        # closed. That is a double-count, silently.
        latest: list[dict[str, Any]] = []
        manifests: list[Manifest] = []
        manifests_used: list[Manifest] = []
        manifests = await read_manifests(reader, prefix=prefix, required_metric=required_metric)
        if manifests:
            if export_name:
                manifests = [m for m in manifests if m.export_name == export_name]
                if not manifests:
                    raise ExportError("No manifest for the selected export. No data was changed.")
            if required_metric:
                manifests = [m for m in manifests if m.dataset == required_metric]
                if not manifests:
                    raise ExportError(
                        f"No {required_metric} run was found. No data was changed. "
                        "Use Quick refresh instead."
                    )
            if len({(m.export_name, m.dataset) for m in manifests}) > 1:
                raise ExportError(
                    "Multiple export definitions share this prefix. Select a single export "
                    "and dataset; no data was changed.")
            try:
                chosen = ([m for m in manifests if (m.run_id, m.submitted) == tuple(run_identity)]
                          if run_identity else latest_per_period(manifests))
                if run_identity and len(chosen) != 1:
                    raise ValueError("The requested complete run is unavailable or ambiguous.")
            except ValueError as exc:
                raise ExportError(f"{exc} No data was changed.") from exc
            latest = _blobs_from_manifests(chosen)
            manifests_used = chosen
            log.info("export: %d manifest(s) -> %d run(s), %d file(s)",
                     len(manifests), len(chosen), len(latest))

        if not latest:
            if required_metric == "FocusCost" or export_name:
                raise ExportError(
                    "No complete manifest for this FOCUS export. No data was changed. "
                    "Wait for Azure to finish or use Quick refresh.")
            # No manifest: an older export, or a container the caller can only partly read.
            # The timestamp heuristic is still better than nothing, but it is the fallback now
            # rather than the rule.
            blobs = await reader.list(prefix=prefix.strip("/"))
            data = [b for b in blobs if b["name"].lower().endswith(_DATA_SUFFIX)]
            if not data:
                raise ExportError(
                    f"No CSV files under '{reader.container}/{prefix}'. "
                    f"Found {len(blobs)} blob(s); check the folder path."
                )

            # Exports are written per run into a dated folder. Take the newest run only, so a
            # container holding months of history does not load everything at once.
            newest = max(b["modified"] or "" for b in data)
            newest_day = newest[:16]
            latest = [b for b in data if (b["modified"] or "")[:16] >= newest_day] or data
            latest.sort(key=lambda b: b["name"])

        if max_files < 1 or len(latest) > max_files:
            raise ExportError(
                f"The export run needs {len(latest)} file(s), exceeding max_files={max_files}. "
                "No data was changed. Increase the cap to load the complete run."
            )

        focus_periods = _focus_windows(manifests_used) if incremental else {}
        if requested_window:
            if not incremental or not run_identity:
                raise ExportError("A bounded FOCUS import requires an explicit complete run identity.")
            actual = set(focus_periods.values())
            if len(actual) != 1 or not all(
                    start <= requested_window[0] <= requested_window[1] <= end
                    for start, end in actual):
                raise ExportError("The returned report does not cover the requested dates. No data changed.")
        windows: dict[str, list[tuple[str, str]]] = {}
        if coverage_sub:
            windows[coverage_sub] = ([requested_window] if requested_window else
                                     sorted(set(focus_periods.values())))
        receipts = []
        manifest_documents = {}
        if incremental and coverage_sub:
            metadata = getattr(reader, "manifest_metadata", {})
            for manifest in manifests_used:
                receipt = {"run_id": manifest.run_id, "submitted": manifest.submitted,
                           "native_timeframe": manifest.timeframe,
                           "actual_window": [manifest.start, manifest.end],
                           "requested_window": requested_window,
                           "rows": manifest.row_count, "bytes": manifest.byte_count,
                           **{k: v for k, v in metadata.get(
                               (manifest.run_id, manifest.submitted), {}).items() if k != "document"}}
                receipt["fingerprint"] = hashlib.sha256(json.dumps(
                    [reader.account, reader.container, receipt], sort_keys=True).encode()).hexdigest()
                manifest_documents[receipt["fingerprint"]] = {
                    **receipt, "account": reader.account, "container": reader.container,
                    "document": metadata.get((manifest.run_id, manifest.submitted), {}).get("document")}
                periods = [requested_window] if requested_window else sorted({
                    focus_periods[b.name.lstrip("/")] for b in manifest.blobs})
                for lo_day, hi_day in periods:
                    day = date.fromisoformat(lo_day)
                    while day <= date.fromisoformat(hi_day):
                        receipts.append((f"focus-day:{coverage_sub}:{day.isoformat()}", receipt))
                        day += timedelta(days=1)

            def already_applied():
                applied = []
                with warehouse.connect(read_only=True) as con:
                    for key, receipt in receipts:
                        row = con.execute("SELECT value FROM meta WHERE key=?", [key]).fetchone()
                        if not row or json.loads(row[0]).get("fingerprint") != receipt["fingerprint"]:
                            return []
                        applied.append(json.loads(row[0]))
                return applied

            applied = await asyncio.to_thread(already_applied)
            if applied:
                # Every day in this run is already certified by a matching receipt, so
                # nothing is being written. The derived tables therefore only need
                # rebuilding if an earlier run left them behind the current generation;
                # otherwise this whole poll costs nothing, which matters because the
                # scheduled sync takes this path every time the export has not moved.
                if not await asyncio.to_thread(_derived_is_current):
                    await warehouse.refresh_rollup_async()
                from . import jobs
                for event_id in {r.get("publication_id") for r in applied} - {None}:
                    await jobs.publish_generation([coverage_sub], windows, event_id=event_id)
                result = {"rows": sum(r.get("day_rows", 0) for r in applied), "rows_inspected": 0, "files": 0,
                          "from": min(p[0] for p in windows[coverage_sub]),
                          "to": max(p[1] for p in windows[coverage_sub]),
                          "changed_days": 0, "unchanged_days": sum(bool(r.get("day_rows")) for r in applied),
                          "incremental": True, "inserted_rows": 0, "deleted_rows": 0,
                          "unchanged_rows": sum(r.get("day_rows", 0) for r in applied), "data_bytes": 0,
                          "source_bytes": getattr(reader, "manifest_bytes", 0),
                          "manifest_bytes": getattr(reader, "manifest_bytes", 0),
                          "skipped_unchanged_run": True, "manifests": [r for _, r in receipts[:1]]}
                warehouse.state = {"status": "ready", "progress": 1, **result}
                return result
        i_date = list(COLUMNS).index("ChargePeriodStart")
        i_sub = list(COLUMNS).index("SubAccountId")
        warehouse.state["total"] = len(latest)
        warehouse.state["loading"] = 1
        # What is being read, in the units this route actually has. The API path counts periods;
        # this one counts blobs, and calling them periods made "0/2 periods loaded" appear over a
        # FOCUS run that has no periods in it.
        warehouse.state["unit"] = "file"

        # Rows go into an unlogged staging table as they are parsed, not into a list.
        # A real customer month is 5.8M rows at 576-810 bytes each, so accumulating them
        # here needed several gigabytes before reconciliation even began -- which is what
        # EXPORT_MAX_ROWS existed to prevent, and why the importer could not take a real
        # estate at all. `staging` answers everything the list used to answer.
        #
        # Given a factory rather than a connection: an export that fails validation must
        # not have opened a database session at all, so the connection is taken on the
        # first batch that is genuinely staged. The stack releases it in the `finally`.
        staged = staging.Staged(
            lambda: resources.enter_context(warehouse.connect()), uuid4().hex)
        file_counts: dict[str, int] = {}
        skipped = 0
        source_bytes = 0
        for n, b in enumerate(latest, 1):
            # Claim the file *before* downloading it, not after.
            #
            # An export is a handful of very large blobs — often two — and essentially all of the
            # wall time is inside the download. Updating only on completion meant the bar sat at
            # 0% for minutes and then jumped to 90%, which is indistinguishable from a refresh
            # that has hung. The count now names the file being fetched, and the bar advances to
            # the start of its share, so something moves the moment work begins.
            warehouse.state["done"] = n - 1
            warehouse.state["fetching"] = n
            warehouse.state["fetching_name"] = b["name"].rsplit("/", 1)[-1][:60]
            warehouse.state["fetching_bytes"] = int(b.get("size") or 0)
            warehouse.state["progress"] = round(((n - 1) / len(latest)) * 0.9, 3)

            raw_bytes = await reader.get(b["name"])
            source_bytes += len(raw_bytes)
            if requested_window and (not b.get("size") or len(raw_bytes) != b["size"]):
                raise ExportError("FOCUS partition bytes differ from its manifest. No data was changed.")
            header, rows = await asyncio.to_thread(_read_any, b["name"], raw_bytes)
            if required_metric == "FocusCost":
                # Also validate SAS containers and stale/misconfigured export definitions:
                # a requested type is not evidence that the downloaded file has that schema.
                names = {h.strip().lower() for h in header}
                if not {"chargeperiodstart", "billedcost", "effectivecost",
                        "subaccountid", "billingcurrency"}.issubset(names):
                    raise ExportError(
                        "The selected file is not a FOCUS report. No data was changed. "
                        "Use Quick refresh or select a FOCUS export."
                    )
                rows = _validated_focus_rows(header, rows)
            # Parsing is pure Python over every row of a multi-megabyte CSV, so it goes to a
            # thread for the same reason the write does: on the event loop it stops the app
            # answering anything, including the poll that draws this progress bar.
            #
            # Each batch is checked and copied in as it is parsed, rather than the whole
            # blob being held and checked at the end. Every row still passes through
            # exactly one batch, so the checks see all of them; what changes is that a
            # multi-million-row partition costs one batch of memory instead of all of it.
            basis = list(COLUMNS).index("BillingCostBasis")
            period = focus_periods.get(b["name"])
            seen = {"rows": 0}

            def take(records: list[list[Any]], *, _b=b, _period=period,
                     _basis=basis, _seen=seen) -> None:
                if required_metric == "FocusCost":
                    if any(r[_basis] != "FOCUS" for r in records):
                        raise ExportError(
                            "The selected file contains non-FOCUS cost data. No data was "
                            "changed. Use Quick refresh or select a FOCUS export.")
                    if coverage_sub and any(r[i_sub] != coverage_sub for r in records):
                        raise ExportError(
                            "The FOCUS subscription export contains another subscription. "
                            "No data was changed.")
                    if any(not _period[0] <= r[i_date] <= _period[1] for r in records):
                        raise ExportError(
                            "The FOCUS file contains a date outside its manifest period. "
                            "No data was changed.")
                    for sub in {r[i_sub] for r in records if r[i_sub]}:
                        if selected_subs is None or sub in selected_subs:
                            periods = windows.setdefault(sub, [])
                            bounded = requested_window or _period
                            if bounded not in periods:
                                periods.append(bounded)
                _seen["rows"] += len(records)
                staged.add(records)

            recs, sk = await asyncio.to_thread(
                _load_rows, header, rows, subscription_id, b.get("metric") or metric,
                take)
            await asyncio.to_thread(take, recs)
            recs = []
            observed_rows = seen["rows"] + sk
            file_counts[b["name"]] = observed_rows
            if (incremental or b.get("rows")) and b["rows"] != observed_rows:
                raise ExportError(
                    f"The export declares {b['rows']:,} row(s) in {b['name']} but "
                    f"{observed_rows:,} were read. No data was changed."
                )
            skipped += sk
            # Downloading is nearly all of the wall time here, so the bar tracks it directly and
            # stops just short of full — the database write still has to happen.
            warehouse.state["done"] = n
            warehouse.state["rows"] = staged.rows
            warehouse.state["progress"] = round(min(n / len(latest), 1.0) * 0.9, 3)
        warehouse.state.pop("fetching", None)
        await asyncio.to_thread(staged.index)
        from . import jobs
        await jobs.phase("validating")

        if skipped and required_metric == "FocusCost":
            raise ExportError(
                f"The FOCUS run contains {skipped} unusable row(s). No data was changed.")
        empty_covered = (bool(coverage_sub) and bool(manifests_used)
                         and all(m.row_count == 0 for m in manifests_used))
        if not staged.rows and not empty_covered:
            raise ExportError(
                f"Read {len(latest)} file(s) but found no usable rows — {skipped} row(s) were "
                "skipped for a missing date. The export schema or subscription coverage "
                "may be unrecognised. No data was changed."
            )

        for manifest in manifests_used:
            observed = sum(file_counts[b.name.lstrip("/")] for b in manifest.blobs)
            if (incremental or manifest.row_count) and manifest.row_count != observed:
                raise ExportError(
                    f"The export declares {manifest.row_count:,} row(s) in run "
                    f"{manifest.run_id} but {observed:,} were read. No data was changed."
                )
            if requested_window and manifest.byte_count != sum(b.bytes for b in manifest.blobs):
                raise ExportError("FOCUS manifest byte counts disagree. No data was changed.")

        if replace and await asyncio.to_thread(staged.has_rows_without_subscription):
            raise ExportError(
                "Cannot replace an export containing rows without SubAccountId. Supply an "
                "authoritative subscription_id for this export, or use a subscription-attributed "
                "export. No data was changed."
            )
        excluded = 0
        if requested_window:
            await asyncio.to_thread(staged.restrict_to_window, *requested_window)
            empty_covered = bool(coverage_sub and manifests_used)
        if selected_subs is not None:
            excluded = await asyncio.to_thread(staged.restrict_to_subscriptions, selected_subs)
            if not staged.rows and not empty_covered:
                raise ExportError(
                    "The complete export contains no rows for the selected subscriptions. "
                    "No data was changed."
                )
        first, last = await asyncio.to_thread(staged.span)
        lo = first or min(start for periods in windows.values() for start, _ in periods)
        hi = last or max(end for periods in windows.values() for _, end in periods)
        subs = await asyncio.to_thread(staged.subscriptions) | set(windows)
        day_rows = await asyncio.to_thread(staged.day_rows)
        receipts = [(key, {**receipt, "day_rows": day_rows.get(
            (coverage_sub, key.rsplit(":", 1)[1]), 0)}) for key, receipt in receipts]
        if replace and not incremental:
            for sub in subs:
                if manifests_used:
                    # A correction may remove the last charge on a day. Use the declared
                    # run period, not just surviving rows, without deleting gaps in history.
                    windows[sub] = []
                    for m in manifests_used:
                        start, end = m.start[:10], m.end[:10]
                        if m.timeframe == "MonthToDate":
                            end = min(end, m.submitted[:10])
                        if start > end:
                            raise ExportError("Invalid manifest period; no data was changed.")
                        windows[sub].append((start, end))
                else:
                    lo_day, hi_day = await asyncio.to_thread(staged.days_for, sub)
                    windows[sub] = [(lo_day, hi_day)]

        # Writing is its own phase and takes real time — tens of thousands of rows through
        # the database — so it says so rather than leaving the bar at 90% with no explanation.
        warehouse.state["loading"] = 0
        warehouse.state["writing"] = staged.rows
        warehouse.state["progress"] = 0.92
        publication_id = str(uuid4())
        total_rows = staged.rows

        def _write() -> dict[str, Any]:
            changes: dict[str, Any] = {}
            # The connection the rows were staged on, because the staging table belongs to
            # it and this is the transaction that reads from it. Not closed here: the run
            # owns it and drops the staging table on the way out.
            with nullcontext(staged.connection()) as con:
                # One transaction, for the same reason `Warehouse._replace` uses one: the
                # connection is in autocommit, so without an explicit BEGIN the DELETE commits
                # on its own and any failure during the insert -- a dropped blob connection, a
                # bad row, the COPY-to-binding fallback giving up on chunk five of nine --
                # leaves the window deleted and not replaced. The dashboard then reads a month
                # short with nothing to say it happened, which is the worst outcome available
                # to a cost tool: not an outage, a confidently wrong number.
                con.execute("BEGIN TRANSACTION")
                try:
                    from . import jobs
                    jobs.assert_lease(con)
                    jobs.lock_generation(con)
                    con.execute("LOCK TABLE costs IN SHARE ROW EXCLUSIVE MODE")
                    protected = set()
                    if incremental:
                        # A delayed historical run must not roll back a newer compact
                        # correction which covers only part of its wider window.
                        for key, receipt in receipts:
                            previous = jobs._read(con, key)
                            if previous and previous.get("submitted"):
                                old = datetime.fromisoformat(previous["submitted"].replace("Z", "+00:00"))
                                new = datetime.fromisoformat(receipt["submitted"].replace("Z", "+00:00"))
                                if old.replace(tzinfo=old.tzinfo or UTC) > new.replace(tzinfo=new.tzinfo or UTC):
                                    protected.add((coverage_sub, key.rsplit(":", 1)[1]))
                        changes = _reconcile_staged(con, staged, windows, protected=protected)
                    elif replace:
                        for sub, periods in sorted(windows.items()):
                            for start, end in periods:
                                con.execute("DELETE FROM meta WHERE key >= ? AND key <= ?",
                                            [f"focus-day:{sub}:{start}", f"focus-day:{sub}:{end}"])
                                con.execute(
                                    'DELETE FROM costs WHERE "ChargePeriodStart" BETWEEN ? AND ? '
                                    'AND "SubAccountId" = ?',
                                    [start, end, sub],
                                )
                    # `INSERT ... SELECT` from the staging table. This was a positional
                    # `INSERT INTO costs VALUES (...)` executed per row: positional couples the
                    # write to today's COLUMNS order, which ALTER TABLE quietly breaks, and
                    # per-row execution is what made a FOCUS load take minutes. The rows never
                    # come back into Python at all now.
                    if not incremental:
                        staged.publish()
                    if changes.get("changed_days") or (not incremental and total_rows):
                        jobs.advance_generation(con)
                        changes["generation"] = int(con.execute(
                            "SELECT value FROM meta WHERE key=?", [jobs.GENERATION_KEY]).fetchone()[0])
                    con.execute(
                        "INSERT INTO ingest_log VALUES (?,?,?,?,?,?,?)",
                        [subscription_id or ",".join(sorted(subs))[:200], reader.account,
                         f"{lo}..{hi}", total_rows, started, "ok",
                         f"export {reader.container}/{prefix} ({len(latest)} file(s))"],
                    )
                    for key, receipt in receipts:
                        if (coverage_sub, key.rsplit(":", 1)[1]) in protected:
                            continue
                        if changes.get("changed_days"):
                            receipt["publication_id"] = publication_id
                        jobs._put(con, key, receipt)
                    for fingerprint, document in manifest_documents.items():
                        jobs._put(con, "focus-manifest:" + fingerprint, document)
                    reconciliation = jobs._read(con, "subscription_reconciliation") or {}
                    remaining = dict(reconciliation)
                    if receipts:
                        for sub in set(reconciliation) & set(windows):
                            affected = reconciliation[sub]
                            start, end = affected.get("from"), affected.get("to")
                            if not start or not end:
                                continue
                            expected = (date.fromisoformat(end) - date.fromisoformat(start)).days + 1
                            covered = con.execute(
                                "SELECT count(*) FROM meta WHERE key >= ? AND key <= ?",
                                [f"focus-day:{sub}:{start}", f"focus-day:{sub}:{end}"],
                            ).fetchone()[0]
                            if expected > 0 and covered == expected:
                                remaining.pop(sub)
                        if remaining != reconciliation:
                            jobs._put(con, "subscription_reconciliation", remaining)
                    if changes.get("changed_days"):
                        jobs._put(con, "data-publication:" + publication_id, {
                            "id": publication_id, "scope": sorted(subs), "windows": windows,
                            "status": "committed", "job_id": jobs.current_job.get(),
                            "generation": changes["generation"]})
                    con.execute("COMMIT")
                except Exception:
                    con.execute("ROLLBACK")
                    raise
            return changes

        # On a worker thread, because psycopg is synchronous and this is the single longest step
        # in the whole refresh.
        #
        # Run inline it blocked the event loop for over five minutes on a real estate: the app
        # served its last request at 11:15:38 and its next at 11:20:51, with nothing in between.
        # For that whole window the dashboard was frozen — the progress poll could not be
        # answered, so the bar stayed where it was, and every other tab hung too. That is the
        # entire substance of "the refresh button doesn't load the data": it was loading, and
        # the app had stopped being able to say so.
        await jobs.phase("committing")
        changes = await asyncio.to_thread(_write)
        warehouse.state.pop("writing", None)
        # Also retry on an unchanged snapshot: a prior import may have committed costs but
        # failed (or been interrupted) before rebuilding the derived dashboard tables. The
        # generation stamp distinguishes the two, so an import that changed nothing and
        # found the tables current does no work here at all.
        await jobs.phase("warming-cache")
        if not await asyncio.to_thread(_derived_is_current):
            await warehouse.refresh_rollup_async(_rebuild_window(changes))
        if changes.get("changed_days"):
            from . import jobs
            await jobs.publish_generation(sorted(subs), windows, generation=changes["generation"],
                                          event_id=publication_id)
    except Exception as exc:
        warehouse.state.update(status="failed", failed=1, detail=str(exc))
        warehouse.state.pop("writing", None)
        raise
    finally:
        await reader.close()
        # The staging table belongs to this run, whether it succeeded or not. Dropped
        # rather than left for the next import to find: it is unlogged and rebuilt from
        # the export, so there is nothing in it worth keeping. The connection is then
        # released by the stack that entered it.
        if staged is not None:
            await asyncio.to_thread(staged.drop)
        await asyncio.to_thread(resources.close)

    # Full runs were validated before scope filtering and before any writes. Rows outside
    # the explicit subscription selection are intentionally excluded, not a manifest shortfall.
    shortfall = None
    declared = sum(m.row_count for m in (manifests_used or []))

    elapsed = (datetime.now(UTC) - started).total_seconds()
    result = {
        "source": f"{reader.account}/{reader.container}/{prefix}".rstrip("/"),
        "files": len(latest),
        "source_bytes": source_bytes + getattr(reader, "manifest_bytes", 0),
        "data_bytes": source_bytes,
        "manifest_bytes": getattr(reader, "manifest_bytes", 0),
        "requested_window": requested_window,
        "manifests": list({r["fingerprint"]: r for _, r in receipts}.values()),
        "rows": total_rows,
        "rows_inspected": sum(file_counts.values()),
        "skipped_rows": skipped,
        "excluded_rows": excluded,
        "from": lo,
        "to": hi,
        "subscriptions": len(subs),
        "seconds": round(elapsed, 1),
        # Present only when the run manifests were readable, so "not checked" stays
        # distinguishable from "checked and correct".
        "declared_rows": declared if incremental else declared or None,
        "row_count_mismatch": shortfall,
        "runs": [m.run_id for m in (manifests_used or []) if m.run_id] or None,
        "warehouse": warehouse.summary(),
        **changes,
    }
    # Say that it finished. Without this the state is left at whatever the *previous* run set —
    # so a successful export ingest inherited a stale "failed" from the last attempt, and the
    # archive step, which only fires on a run that actually succeeded, skipped it silently.
    warehouse.state = {
        "status": "ready",
        "finished": datetime.now(UTC).isoformat(timespec="seconds"),
        "source": result["source"],
        "files": result["files"],
        "seconds": result["seconds"],
        "progress": 1.0,
        **changes,
        **warehouse.summary(),
    }
    return result


async def ingest_all_exports(subscription_ids: list[str] | None = None,
                             prefer: str = "FocusCost") -> dict[str, Any]:
    """Discover exports and load whichever ones are reachable.

    Best-effort by design: in a governed tenant most export storage accounts will refuse a
    direct read, and that is information the operator needs rather than a hard failure.
    """
    found = await discover_exports(subscription_ids)
    exports = [e for e in found["exports"] if e["container"]]
    if prefer:
        exports.sort(key=lambda e: e["type"] != prefer)

    loaded, failed = [], []
    for e in exports:
        try:
            result = await ingest_export(
                account=e["storage_account"],
                container=e["container"],
                prefix=e["root_folder"] or "",
                # None for a billing-scope export, and deliberately so: this only backfills
                # SubAccountId when the file omits it, and a billing-scope export carries rows
                # for many subscriptions. Forcing one id onto all of them would file every
                # subscription's spend under whichever one happened to be named.
                subscription_id=e["subscription_id"],
                metric=e.get("type"),
            )
            loaded.append({"export": e["name"], "type": e["type"],
                           "scope": e.get("scope"), **result})
        except Exception as exc:  # noqa: BLE001 - report and continue to the next export
            failed.append({"export": e["name"], "account": e["storage_account"],
                           "scope": e.get("scope"), "reason": str(exc)[:220]})

    return {
        "discovered": found["count"],
        "strategy": found.get("strategy"),
        "calls_saved": found.get("calls_saved"),
        "loaded": loaded,
        "failed": failed,
        "rows_loaded": sum(item["rows"] for item in loaded),
        "note": (
            "No export could be read directly. This is normal in a governed tenant: the storage "
            "accounts have public network access disabled. Supply a container SAS URL "
            "(ingest_export(sas_url=...)) or run from inside the network."
            if not loaded and failed else None
        ),
    }


async def reachable(exports: list[dict[str, Any]], timeout: float = 8.0) -> tuple[list, list]:
    """Split exports into the ones whose storage answers and the ones that do not.

    A pre-flight, run before any ingest starts, because the alternative is a background job that
    spends half a minute discovering the same thing and then reports a failure to someone who
    has been watching a spinner. Where storage is private-endpoint only -- the normal case in a
    governed tenant -- this turns "press refresh, wait, fail" into an immediate, explained no.

    Cheap on purpose: one listing of one blob per account, in parallel, with a short timeout.
    The question is only whether the data plane answers at all.
    """
    async def probe(e: dict[str, Any]) -> tuple[dict, str | None]:
        reader = BlobReader(e.get("storage_account") or "", e.get("container") or "")
        try:
            await asyncio.wait_for(reader.list(prefix=(e.get("root_folder") or "").strip("/"),
                                               limit=1, require_complete=False), timeout=timeout)
            return e, None
        except TimeoutError:
            return e, (f"Storage account '{e.get('storage_account')}' did not answer within "
                       f"{timeout:.0f}s.")
        except Exception as exc:  # noqa: BLE001 - the reason is what the caller reports
            return e, str(exc)
        finally:
            await reader.close()

    results = await asyncio.gather(*(probe(e) for e in exports), return_exceptions=False)
    ok = [e for e, err in results if err is None]
    bad = [(e, err) for e, err in results if err is not None]
    return ok, bad
