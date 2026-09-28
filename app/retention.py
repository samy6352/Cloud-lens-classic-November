"""Read-only storage inventory and retention *preview*, never a retention executor.

An explicit keep_months=N keeps N completed calendar months PLUS the current
month (and all future partitions). Unset means all history is preserved.
Only whole, elapsed calendar-month bounds on the expected date key qualify.
Elapsed is not proof that ingestion has finished: catalogues cannot certify that.

Capacity horizons mean 6/12/24 ADDITIONAL incoming months, retaining all current
history. The largest aligned elapsed month's raw + published-derived bytes is
an observed baseline, not a guarantee. No row scans or application startup occur.
"""
from __future__ import annotations

import datetime as dt
import os
import re
from dataclasses import asdict, dataclass
from typing import Any

PUBLISHED = ("costs_rollup", "resource_presence", "billing_rollup", "billing_serving")
DATASETS = ("costs", *PUBLISHED)
SAFE_SCHEMA = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,62}", re.ASCII)
MONTH_BOUND = re.compile(
    r"FOR VALUES FROM \('(\d{4}-\d{2}-\d{2})'\) TO \('(\d{4}-\d{2}-\d{2})'\)"
)
WARNINGS = [
    "PREVIEW ONLY: no data changed. Historical deletion is DISABLED; no execution path exists.",
    "Any future removal requires explicit approval, verified archival/restoration, coherent "
    "raw/derived generations and cache invalidation, and an ingestion retention policy "
    "preventing old exports from being reimported.",
    "Elapsed calendar months are not certified complete imports; late arrivals, sparse fixtures "
    "and workload growth can make observed capacity estimates too small.",
    "Database storage is not a RAM requirement: PostgreSQL shared_buffers and the OS page cache "
    "both cache data.",
]


def validate_schema(schema: str) -> str:
    if not SAFE_SCHEMA.fullmatch(schema):
        raise ValueError("Schema must be a plain PostgreSQL identifier (at most 63 characters)")
    return schema


def validate_policy(keep_months: int | None, as_of: dt.date) -> dt.date | None:
    if type(as_of) is not dt.date:
        raise ValueError("as_of must be a calendar date")
    if keep_months is None:
        return None
    if type(keep_months) is not int or keep_months < 1:
        raise ValueError("keep_months must be a positive integer or unset")
    ordinal = (as_of.year - 1) * 12 + as_of.month - 1 - keep_months
    if ordinal < 0:
        raise ValueError("keep_months extends before year 1")
    year, month = divmod(ordinal, 12)
    return dt.date(year + 1, month + 1, 1)


def monthly_bounds(bound: str | None) -> tuple[dt.date, dt.date] | None:
    """Parse only PostgreSQL's canonical single-date monthly RANGE bounds."""
    match = MONTH_BOUND.fullmatch(bound or "")
    if not match:
        return None
    try:
        start, end = (dt.date.fromisoformat(value) for value in match.groups())
        next_start = dt.date(start.year + (start.month == 12), start.month % 12 + 1, 1)
    except ValueError:
        return None
    return (start, end) if start.day == 1 and end == next_start else None


@dataclass
class Partition:
    dataset: str
    parent: str
    schema: str
    name: str
    oid: int
    bounds: str | None
    heap_bytes: int
    index_bytes: int
    total_bytes: int
    rows: int | None
    row_source: str
    start: dt.date | None = None
    end: dt.date | None = None
    manual_review: str | None = None


def connect_read_only():
    """Reuse only side-effect-free store configuration and its credential helper.

    Never call Store.connect/_open or construct Warehouse: those paths run DDL.
    Startup options protect the first statement, including for a writable role.
    """
    import psycopg

    from .store import PostgresStore

    store = PostgresStore()
    validate_schema(store.schema or "public")
    connection = psycopg.connect(
        host=store.host, dbname=store.database, user=store.user,
        password=store._password(), sslmode=store.sslmode,
        port=os.getenv("PGPORT", "5432"), connect_timeout=5,
        application_name="cloudlens-retention-preview",
        autocommit=False,
        options="-c default_transaction_read_only=on -c statement_timeout=15000 "
        "-c lock_timeout=2000 -c idle_in_transaction_session_timeout=30000 "
        "-c search_path=pg_catalog -c DateStyle=ISO,YMD",
    )
    connection.isolation_level = psycopg.IsolationLevel.REPEATABLE_READ
    connection.read_only = True
    return connection


def _roots(cursor: Any, schema: str) -> tuple[dict, list[str]]:
    cursor.execute(
        """SELECT c.oid, c.relname, c.relkind
           FROM pg_catalog.pg_class c
           JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace
           WHERE n.nspname = %s AND c.relname = ANY(%s)""",
        (schema, list(DATASETS)),
    )
    found = {name: (oid, kind) for oid, name, kind in cursor.fetchall()}
    roots, issues = {}, []
    for dataset in DATASETS:
        if dataset not in found:
            issues.append(f"{dataset}: missing published relation; alignment cannot be certified")
            continue
        oid, kind = found[dataset]
        name = dataset
        if dataset != "costs" and kind == "v":
            cursor.execute(
                """SELECT DISTINCT t.oid, t.relname, t.relkind, ns.nspname
                   FROM pg_catalog.pg_rewrite r
                   JOIN pg_catalog.pg_depend d ON d.objid = r.oid
                     AND d.classid = 'pg_catalog.pg_rewrite'::regclass
                     AND d.refclassid = 'pg_catalog.pg_class'::regclass
                   JOIN pg_catalog.pg_class t ON t.oid = d.refobjid
                   JOIN pg_catalog.pg_namespace ns ON ns.oid = t.relnamespace
                   WHERE r.ev_class = %s AND t.oid <> %s""",
                (oid, oid),
            )
            targets = cursor.fetchall()
            if (len(targets) != 1 or targets[0][1] not in (dataset + "_a", dataset + "_b")
                    or targets[0][3] != schema):
                issues.append(f"{dataset}: unrecognised published dependencies; manual review")
                continue
            oid, name, kind, _ = targets[0]
        elif dataset != "costs":
            issues.append(f"{dataset}: not a published active-side view; manual review")
        roots[dataset] = (oid, name, kind)
    return roots, issues


def inventory(connection: Any, schema: str = "public") -> dict:
    """Read catalogues/storage functions and the small derived ledger, never cost rows.

    Call with connect_read_only() (one repeatable-read transaction). The read-only
    assertion also rejects accidental use of a normal application connection.
    """
    schema = validate_schema(schema)
    partitions: list[Partition] = []
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT current_setting('transaction_read_only'), "
            "current_setting('transaction_isolation'), pg_database_size(current_database())"
        )
        readonly, isolation, database_bytes = cursor.fetchone()
        if readonly != "on" or isolation != "repeatable read":
            raise ValueError("Inventory requires a read-only repeatable-read transaction")
        roots, issues = _roots(cursor, schema)
        cursor.execute("SELECT to_regclass(%s)", (f'"{schema}"."_derived_partition_counts"',))
        metadata = {}
        if cursor.fetchone()[0] is not None:
            cursor.execute(
                f'SELECT parent_name, leaf_name, leaf_oid, rows, dirty '
                f'FROM "{schema}"."_derived_partition_counts" WHERE parent_name = ANY(%s)',
                ([name for dataset, (_, name, _) in roots.items() if dataset != "costs"],),
            )
            metadata = {(parent, name, oid): (rows, dirty)
                        for parent, name, oid, rows, dirty in cursor.fetchall()}
        for dataset, (parent_oid, parent, kind) in roots.items():
            cursor.execute(
                """SELECT p.partstrat, p.partnatts, a.attname, a.atttypid::regtype::text
                   FROM pg_catalog.pg_partitioned_table p
                   LEFT JOIN pg_catalog.pg_attribute a
                     ON a.attrelid = p.partrelid AND a.attnum = p.partattrs[0]
                   WHERE p.partrelid = %s""",
                (parent_oid,),
            )
            key = cursor.fetchone()
            valid_key = kind == "p" and key == ("r", 1, "ChargePeriodStart", "date")
            if not valid_key:
                issues.append(f"{dataset}: not a single ChargePeriodStart date RANGE; manual review")
            cursor.execute(
                """WITH RECURSIVE tree AS (
                     SELECT c.oid, 0 AS depth FROM pg_catalog.pg_class c WHERE c.oid = %s
                     UNION ALL
                     SELECT i.inhrelid, t.depth + 1 FROM tree t
                     JOIN pg_catalog.pg_inherits i ON i.inhparent = t.oid
                   )
                   SELECT c.oid, n.nspname, c.relname, c.relkind, t.depth,
                          pg_catalog.pg_get_expr(c.relpartbound, c.oid), c.reltuples,
                          pg_catalog.pg_relation_size(c.oid),
                          pg_catalog.pg_indexes_size(c.oid),
                          pg_catalog.pg_total_relation_size(c.oid)
                   FROM tree t JOIN pg_catalog.pg_class c ON c.oid = t.oid
                   JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace
                   WHERE NOT EXISTS (
                     SELECT 1 FROM pg_catalog.pg_inherits i WHERE i.inhparent = c.oid)
                   ORDER BY c.oid""",
                (parent_oid,),
            )
            actual = set()
            for oid, ns, name, leaf_kind, depth, bound, estimate, heap, index, total in cursor.fetchall():
                actual.add((parent, name, oid))
                month = monthly_bounds(bound)
                reasons = []
                if not valid_key or depth != 1 or leaf_kind != "r" or ns != schema:
                    reasons.append("unsupported key, nested/flat/foreign or cross-schema relation")
                if month is None:
                    reasons.append("default/NULL, irregular or unrecognised bounds")
                rows = int(estimate) if estimate is not None and estimate >= 0 else None
                source = "approximate PostgreSQL reltuples (may be stale/unknown)"
                if dataset != "costs":
                    record = metadata.get((parent, name, oid))
                    if record is None or record[1] or record[0] is None or record[0] < 0:
                        rows, source = None, "unverified derived metadata"
                        reasons.append("missing, dirty or OID-mismatched derived row metadata")
                    else:
                        rows, source = int(record[0]), "exact derived metadata (OID-verified, clean)"
                partitions.append(Partition(
                    dataset, parent, ns, name, oid, bound, heap, index, total, rows, source,
                    *(month or (None, None)), "; ".join(reasons) or None,
                ))
            if dataset != "costs" and any(
                entry[0] == parent and entry not in actual for entry in metadata
            ):
                issues.append(f"{dataset}: stale/detached/OID-mismatched ledger entries; manual review")
    return {
        "partitions": partitions, "issues": issues,
        "database_total_bytes": int(database_bytes),
        "active_roots": {dataset: {"oid": oid, "name": name}
                         for dataset, (oid, name, _) in roots.items()},
    }


def build_plan(
    snapshot: dict, *, as_of: dt.date, keep_months: int | None = None,
    headroom_bytes: int | None = None,
) -> dict:
    cutoff = validate_policy(keep_months, as_of)
    if headroom_bytes is not None and (type(headroom_bytes) is not int or headroom_bytes < 0):
        raise ValueError("headroom_bytes must be a nonnegative integer or unset")
    partitions = snapshot["partitions"]
    current = as_of.replace(day=1)
    groups: dict[dt.date, list[Partition]] = {}
    for partition in partitions:
        if partition.start is not None:
            groups.setdefault(partition.start, []).append(partition)
    issues = list(snapshot["issues"])
    aligned = {}
    for month, leaves in sorted(groups.items()):
        if (len(leaves) == len(DATASETS) and {p.dataset for p in leaves} == set(DATASETS)
                and not any(p.manual_review for p in leaves)
                and all(monthly_bounds(p.bounds) == (p.start, p.end) for p in leaves)
                and not snapshot["issues"]):
            aligned[month] = leaves
        else:
            issues.append(f"{month.isoformat()}: raw/active-derived alignment unverified; "
                          "excluded from eligibility and capacity baseline")
    completed = {month: sum(p.total_bytes for p in leaves)
                 for month, leaves in aligned.items() if leaves[0].end <= current}
    baseline_month = max(completed, key=completed.get) if completed else None
    baseline = completed[baseline_month] if baseline_month else None
    scoped_bytes = sum(p.total_bytes for p in partitions)
    projections = []
    for horizon in (6, 12, 24):
        extra = baseline * horizon if baseline is not None else None
        db_estimate = snapshot["database_total_bytes"] + extra if extra is not None else None
        projections.append({
            "additional_months": horizon,
            "estimated_additional_raw_active_bytes": extra,
            "estimated_raw_active_bytes": scoped_bytes + extra if extra is not None else None,
            "estimated_database_bytes": db_estimate,
            "estimated_database_plus_headroom_bytes": (
                db_estimate + headroom_bytes
                if db_estimate is not None and headroom_bytes is not None else None
            ),
        })
    candidates = [
        {"month": month.isoformat(), "total_bytes": sum(p.total_bytes for p in leaves),
         "partitions": [{"dataset": p.dataset, "schema": p.schema, "name": p.name, "oid": p.oid}
                        for p in leaves]}
        for month, leaves in aligned.items()
        if cutoff is not None and leaves[0].end <= cutoff
    ]
    return {
        "mode": "PREVIEW ONLY - no data changed",
        "deletion_enabled": False,
        "as_of": as_of.isoformat(),
        "keep_months_preview": keep_months,
        "policy": ("disabled; all history preserved" if cutoff is None else
                   f"preview keeps {keep_months} completed calendar months plus current/future"),
        "keep_from_inclusive": cutoff.isoformat() if cutoff else None,
        "active_roots": snapshot["active_roots"],
        "inventory": [
            {**asdict(p), "start": p.start.isoformat() if p.start else None,
             "end": p.end.isoformat() if p.end else None}
            for p in partitions
        ],
        "preview_eligible_months": candidates,
        "current_raw_active_bytes": scoped_bytes,
        "current_database_bytes": snapshot["database_total_bytes"],
        "current_other_database_bytes": max(0, snapshot["database_total_bytes"] - scoped_bytes),
        "capacity": {
            "basis": "largest aligned elapsed calendar month's raw + active-derived total bytes; "
                     "6/12/24 additional months, preserving ALL current history",
            "baseline_month": baseline_month.isoformat() if baseline_month else None,
            "baseline_bytes": baseline,
            "peak_replacement_temp_backup_headroom_bytes": headroom_bytes,
            "headroom_note": "user allowance, NOT measured; unset means capacity is not fully sized. "
                             "Also budget WAL, staging, inactive sides, backups and workload growth.",
            "estimates_not_guarantees": True,
            "projections": projections,
        },
        "warnings": WARNINGS + issues + (
            ["No aligned complete calendar month observed; capacity baseline unavailable."]
            if baseline is None else []
        ),
        "storage_notes": "heap_bytes is the main relation fork; total_bytes also includes "
                         "indexes, TOAST and auxiliary forks. Other database bytes include "
                         "inactive sides, metadata and unrelated relations, not external backups/WAL. "
                         "Partition dates are catalogue bounds, not scanned data min/max. "
                         "Filesystem sizes can change during the catalogue snapshot.",
    }


def render_text(plan: dict) -> str:
    lines = [plan["mode"], f"Historical deletion: DISABLED. Policy: {plan['policy']}.",
             f"As of: {plan['as_of']}; keep from: {plan['keep_from_inclusive'] or 'all history'}",
             f"Current raw + active derived: {plan['current_raw_active_bytes']:,} bytes",
             f"Current whole database: {plan['current_database_bytes']:,} bytes",
             "Inventory (all sizes bytes; partition bounds, not observed row dates):"]
    for item in plan["inventory"]:
        lines.append(
            f"  {item['dataset']} -> {item['schema']}.{item['name']} oid={item['oid']} "
            f"bounds={item['bounds']} heap={item['heap_bytes']} index={item['index_bytes']} "
            f"total={item['total_bytes']} rows={item['rows']} [{item['row_source']}]"
            + (f" MANUAL REVIEW: {item['manual_review']}" if item["manual_review"] else "")
        )
    eligible = ", ".join(p["month"] for p in plan["preview_eligible_months"]) or "none"
    lines.append(f"Aligned preview-eligible months: {eligible} (NO execution)")
    capacity = plan["capacity"]
    lines.extend([
        f"Capacity ESTIMATES, not guarantees: {capacity['basis']}",
        f"Observed baseline: {capacity['baseline_month']} / {capacity['baseline_bytes']} bytes",
        f"Peak replacement/temp/backup allowance: "
        f"{capacity['peak_replacement_temp_backup_headroom_bytes']} bytes; {capacity['headroom_note']}",
    ])
    for estimate in capacity["projections"]:
        lines.append(
            f"  +{estimate['additional_months']} months: raw+active="
            f"{estimate['estimated_raw_active_bytes']} bytes, database="
            f"{estimate['estimated_database_bytes']} bytes, database+allowance="
            f"{estimate['estimated_database_plus_headroom_bytes']} bytes"
        )
    lines.extend([plan["storage_notes"], *plan["warnings"]])
    return "\n".join(lines)
