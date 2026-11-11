"""Narrow FOCUS billing surface; never inserts into the live warehouse or ARM scopes."""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
from collections import defaultdict
from datetime import UTC, date, datetime
from decimal import Decimal
from uuid import UUID, uuid4

from fastapi import HTTPException, Request
from fastapi.responses import Response
from pydantic import BaseModel, ConfigDict

from . import export_access


def _aggregate_partition(name, raw, expected, lo, hi, groups, count, expected_rows):
    from .exports import _read_any, _validated_focus_rows

    header, rows = _read_any(name, raw)
    names = [h.strip().lower() for h in header]
    required = ("subaccountid", "chargeperiodstart", "billingcurrency",
                "servicename", "billedcost", "effectivecost")
    if not set(required).issubset(names):
        raise ValueError("Missing required FOCUS billing columns.")
    at = {name: names.index(name) for name in required}
    observed = 0
    for row in _validated_focus_rows(header, rows):
        sub = str(row[at["subaccountid"]]).strip().lower()
        if sub.startswith("/subscriptions/"):
            sub = sub[len("/subscriptions/"):]
        if str(UUID(sub)) != expected:
            raise ValueError("Export contains a different subscription; nothing published.")
        day = date.fromisoformat(str(row[at["chargeperiodstart"]])[:10]).isoformat()
        if not lo <= day <= hi:
            raise ValueError("Charge falls outside manifest coverage.")
        currency = str(row[at["billingcurrency"]]).strip()
        if not re.fullmatch(r"[A-Z]{3}", currency):
            raise ValueError("Invalid billing currency.")
        service = str(row[at["servicename"]]).strip()
        billed = Decimal(str(row[at["billedcost"]]))
        effective = Decimal(str(row[at["effectivecost"]]))
        if not billed.is_finite() or not effective.is_finite():
            raise ValueError("Nonfinite cost.")
        group = groups[(day, service, currency)]
        group[0] += billed
        group[1] += effective
        group[2] += 1
        observed += 1
        if observed + count > 2_000_000 or len(groups) > 100_000:
            raise ValueError("Report exceeds aggregate safety limits.")
    if observed != expected_rows:
        raise ValueError("Partition row count disagrees with manifest.")
    return observed


def sources() -> dict:
    """Only deployment operators choose storage destinations; HTTP callers choose a key."""
    configured = json.loads(os.getenv("EXPORT_REPORTING_SOURCES", "{}"))
    if not isinstance(configured, dict):
        raise ValueError("EXPORT_REPORTING_SOURCES must be an object.")
    for key, source in configured.items():
        if not re.fullmatch(r"[a-z0-9-]{1,64}", key):
            raise ValueError("Invalid source key.")
        UUID(source["subscription"])
        if not re.fullmatch(r"[a-z0-9]{3,24}", source["account"]):
            raise ValueError("Invalid storage account.")
        if not re.fullmatch(r"[a-z0-9][a-z0-9-]{1,61}[a-z0-9]", source["container"]):
            raise ValueError("Invalid container.")
        prefix = source["prefix"]
        if not prefix or any(c in prefix for c in ("?", "#", "\\", "..", "%")):
            raise ValueError("An exact export run prefix is required.")
        if not source["export_name"] or not source["label"]:
            raise ValueError("Source needs an export name and label.")
    return configured


async def read_report(source: dict) -> tuple[dict, str, int]:
    from .exports import (
        BlobReader,
        ExportError,
        _focus_windows,
        read_manifests,
    )

    reader = BlobReader(source["account"], source["container"])
    prefix = source["prefix"].strip("/") + "/"
    try:
        manifests = await read_manifests(reader, prefix=prefix, required_metric="FocusCost")
        # Pin one run, not a heuristic collection of overlapping exports.
        if len(manifests) != 1:
            raise ValueError("Pin a prefix containing exactly one complete FOCUS manifest.")
        manifest = manifests[0]
        if manifest.dataset != "FocusCost" or manifest.export_name != source["export_name"]:
            raise ValueError("The manifest does not match the configured FOCUS export.")
        windows = _focus_windows([manifest])
        if len(manifest.blobs) > 60 or manifest.row_count > 2_000_000:
            raise ValueError("Report exceeds the bounded importer limits.")
        if manifest.byte_count > 256 * 1024 * 1024:
            raise ValueError("Report exceeds the bounded byte limit.")
        expected = str(UUID(source["subscription"]))
        groups = defaultdict(lambda: [Decimal(0), Decimal(0), 0])
        count = size = 0
        for blob in manifest.blobs:
            if not blob.name.startswith(prefix) or any(
                c in blob.name for c in ("?", "#", "\\", "..", "%")
            ):
                raise ValueError("Partition escapes the approved run prefix.")
            raw = await reader.get(blob.name)
            size += len(raw)
            if len(raw) != blob.bytes or size > 256 * 1024 * 1024:
                raise ValueError("Partition byte count disagrees with the manifest.")
            lo, hi = windows[blob.name.lstrip("/")]
            observed = await asyncio.to_thread(
                _aggregate_partition, blob.name, raw, expected, lo, hi, groups, count, blob.rows
            )
            count += observed
        if count != manifest.row_count or count < 1 or size != manifest.byte_count:
            raise ValueError("Incomplete or empty report; nothing published.")
        metadata = reader.manifest_metadata[(manifest.run_id, manifest.submitted)]
        totals = defaultdict(lambda: [Decimal(0), Decimal(0)])
        for (_, _, currency), (billed, effective, _) in groups.items():
            totals[currency][0] += billed
            totals[currency][1] += effective
        report = {
            "label": source["label"], "subscription_id": expected, "mode": "Export-backed",
            "live_actions": False, "reporting_available": True, "source_submitted": manifest.submitted,
            "coverage_start": lo, "coverage_end": hi,
            "last_import": datetime.now(UTC).isoformat(), "rows": count,
            "detail": "Snapshot billing only; Azure billing can lag. No live subscription access.",
            "totals": [{"currency": currency, "billed_cost": str(values[0]),
                        "effective_cost": str(values[1])} for currency, values in sorted(totals.items())],
            "charges": [{"day": k[0], "service": k[1], "currency": k[2],
                         "billed_cost": str(v[0]), "effective_cost": str(v[1]), "rows": v[2]}
                        for k, v in sorted(groups.items())],
        }
        digest = hashlib.sha256(json.dumps(metadata["document"], sort_keys=True).encode()).hexdigest()
        return report, digest, count
    except ExportError as exc:
        raise ValueError(str(exc)) from exc
    finally:
        await reader.close()


class ImportRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    source: str


class GrantRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    principal: str
    allow: bool


def install(app, *, current_user, auth_service, web):
    repo = export_access.DatasetAccess()

    def identity(request):
        if not export_access.enabled():
            raise HTTPException(404, "Export reporting is not enabled.")
        user = current_user(request)
        service = auth_service()
        session = service.sso.get(user.sid) if user.source == "entra" else None
        if session is None:
            raise HTTPException(403, "A current Entra session is required.")
        return user, service, export_access.entra_principal(session)

    def admin(request):
        user, service, principal = identity(request)
        repo._admin(user, service)
        # Explicit same-origin protection for browser session mutations.
        if request.method != "GET" and request.headers.get("origin") != str(request.base_url).rstrip("/"):
            raise HTTPException(403, "Same-origin approval is required.")
        return user, service, principal

    def failure(exc):
        if isinstance(exc, PermissionError):
            raise HTTPException(403, str(exc)) from exc
        raise HTTPException(400, str(exc)) from exc

    @app.get("/export-reporting")
    async def page(request: Request):
        identity(request)
        return Response((web / "export-reporting.html").read_text(encoding="utf-8"),
                        media_type="text/html")

    @app.get("/api/export-reporting")
    async def catalog(request: Request):
        _, _, principal = identity(request)
        return {"principal": principal, "datasets": await asyncio.to_thread(repo.catalog, principal)}

    @app.get("/api/export-reporting/admin")
    async def configuration(request: Request):
        try:
            user, service, principal = admin(request)
            configured = sources()
            return {"principal": principal, "sources": [
                {"key": k, "label": s["label"], "subscription_id": s["subscription"]}
                for k, s in configured.items()],
                "datasets": await asyncio.to_thread(repo.admin_catalog, user, service)}
        except (ValueError, PermissionError) as exc:
            failure(exc)

    @app.post("/api/export-reporting/import")
    async def onboard(body: ImportRequest, request: Request):
        try:
            user, service, _ = admin(request)
            from .job_policy import require_writable
            require_writable()
            source = sources().get(body.source)
            if source is None:
                raise ValueError("Unknown approved export source.")
            report, digest, count = await read_report(source)
            dataset = str(uuid4())
            # Recheck the administrator session after potentially slow storage reads.
            await asyncio.to_thread(
                repo.record_import, user, service, dataset=dataset,
                subscription=source["subscription"], imported_at=report["last_import"],
                row_count=count, manifest_sha256=digest, report=report)
            return {"dataset_id": dataset, "rows": count, "detail": "Imported. Explicit read grant required."}
        except (ValueError, PermissionError) as exc:
            failure(exc)

    @app.post("/api/export-reporting/{dataset}/grant")
    async def grant(dataset: UUID, body: GrantRequest, request: Request):
        try:
            user, service, _ = admin(request)
            await asyncio.to_thread(repo.set_grant, user, service, dataset=str(dataset),
                                    principal=body.principal, allow=body.allow)
            return {"ok": True}
        except (ValueError, PermissionError) as exc:
            failure(exc)

    @app.get("/api/export-reporting/{dataset}/billing")
    async def billing(dataset: UUID, request: Request):
        _, _, principal = identity(request)
        try:
            return await asyncio.to_thread(repo.report, principal, str(dataset))
        except PermissionError as exc:
            failure(exc)
