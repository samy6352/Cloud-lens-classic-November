"""Explicit export-only entitlements and isolated immutable billing reports."""
from __future__ import annotations

import json
import os
from contextlib import contextmanager
from datetime import UTC, datetime
from uuid import UUID

from . import auth
from .job_policy import require_writable


def enabled() -> bool:
    return os.getenv("EXPORT_REPORTING_ENABLED", "").lower() == "true"


def entra_principal(session) -> str:
    """Use immutable tenant/object identity, never email, display name or session ID."""
    return f"entra:{UUID(session.tenant)}:{UUID(session.oid)}"


def _principal(value: str) -> str:
    parts = value.split(":")
    if len(parts) != 3 or parts[0] != "entra":
        raise ValueError("An explicit Entra tenant/object principal is required.")
    return f"entra:{UUID(parts[1])}:{UUID(parts[2])}"


class DatasetAccess:
    """Metadata-only repository; no Azure credentials, warehouse writes or implicit grants.

    Uses the warehouse's PostgreSQL pool, identity and configured application schema.
    Each operation gets a fresh transaction; grants are never cached in a session.
    """

    def __init__(self, store=None) -> None:
        self._store = store

    def _store_instance(self):
        if self._store is None:
            from .warehouse import warehouse

            return warehouse._store
        return self._store

    @staticmethod
    def _schema(con):
        """Versioned, transactional migration, serialized across all app workers."""
        with con.raw.transaction():
            exists = con.execute(
                "SELECT to_regclass(format('%I.export_reporting_schema_version', current_schema()))"
            ).fetchone()[0]
            if exists is not None:
                version = con.execute(
                    "SELECT version FROM export_reporting_schema_version"
                ).fetchall()
                if version != [(1,)]:
                    raise RuntimeError("Unsupported export reporting schema version.")
                return
            require_writable()
            con.execute("SELECT pg_advisory_xact_lock(hashtext(current_schema()), "
                        "hashtext('export-reporting-migration'))")
            con.execute("""CREATE TABLE IF NOT EXISTS export_reporting_schema_version (
                singleton INTEGER PRIMARY KEY CHECK(singleton = 1),
                version INTEGER NOT NULL)""")
            version = con.execute(
                "SELECT version FROM export_reporting_schema_version"
            ).fetchall()
            if version:
                if version != [(1,)]:
                    raise RuntimeError("Unsupported export reporting schema version.")
                return
            con.execute("""CREATE TABLE export_reporting_datasets (
                id TEXT PRIMARY KEY, subscription TEXT NOT NULL,
                imported_at TEXT NOT NULL, row_count BIGINT NOT NULL CHECK(row_count > 0),
                manifest_sha256 TEXT NOT NULL)""")
            con.execute("""CREATE TABLE export_reporting_grants (
                dataset TEXT NOT NULL REFERENCES export_reporting_datasets(id),
                principal TEXT NOT NULL, granted_by TEXT NOT NULL, granted_at TEXT NOT NULL,
                PRIMARY KEY(dataset, principal))""")
            con.execute("""CREATE TABLE export_reporting_audit (
                sequence BIGSERIAL PRIMARY KEY,
                at TEXT NOT NULL, actor TEXT NOT NULL, action TEXT NOT NULL,
                dataset TEXT NOT NULL, principal TEXT NOT NULL)""")
            con.execute("""CREATE TABLE export_reporting_reports (
                dataset TEXT PRIMARY KEY REFERENCES export_reporting_datasets(id),
                payload TEXT NOT NULL)""")
            con.execute("INSERT INTO export_reporting_schema_version VALUES (1, 1)")

    @contextmanager
    def _connection(self, *, write: bool = False):
        if not enabled():
            raise PermissionError("Export reporting is disabled.")
        if write:
            require_writable()
        with self._store_instance().connect() as con:
            self._schema(con)
            with con.raw.transaction():
                if not write:
                    con.execute("SET TRANSACTION READ ONLY")
                yield con

    @staticmethod
    def _admin(user: auth.User, authentication: auth.Auth) -> str:
        if not user.admin or user.source != "entra":
            raise PermissionError("An authenticated Entra app administrator is required.")
        session = authentication.sso.get(user.sid)
        if session is None or not authentication.sso.is_admin(session.email, session.oid):
            raise PermissionError("Administrator session expired or access was revoked.")
        return entra_principal(session)

    def record_import(self, user: auth.User, authentication: auth.Auth, *, dataset: str,
                      subscription: str, imported_at: str, row_count: int,
                      manifest_sha256: str, report: dict | None = None) -> None:
        """Publish validated aggregate report and evidence in the same transaction."""
        actor = self._admin(user, authentication)
        dataset, subscription = str(UUID(dataset)), str(UUID(subscription))
        stamp = datetime.fromisoformat(imported_at)
        if stamp.tzinfo is None or stamp.utcoffset() is None:
            raise ValueError("Import time must include a timezone.")
        if type(row_count) is not int or row_count < 1:
            raise ValueError("A completed nonempty import is required.")
        if (len(manifest_sha256) != 64 or
                any(c not in "0123456789abcdef" for c in manifest_sha256)):
            raise ValueError("A lowercase SHA-256 manifest digest is required.")
        with self._connection(write=True) as con:
            con.execute("INSERT INTO export_reporting_datasets VALUES (?, ?, ?, ?, ?)",
                        (dataset, subscription, stamp.astimezone(UTC).isoformat(),
                         row_count, manifest_sha256))
            con.execute("INSERT INTO export_reporting_audit (at, actor, action, dataset, principal) VALUES (?, ?, ?, ?, ?)",
                        (_now(), actor, "record-import", dataset, ""))
            if report is not None:
                con.execute("INSERT INTO export_reporting_reports VALUES (?, ?)", (dataset, json.dumps(report, allow_nan=False)))

    def set_grant(self, user: auth.User, authentication: auth.Auth, *, dataset: str,
                  principal: str, allow: bool) -> None:
        actor = self._admin(user, authentication)
        dataset, principal = str(UUID(dataset)), _principal(principal)
        if type(allow) is not bool:
            raise ValueError("Grant decision must be boolean.")
        with self._connection(write=True) as con:
            if not con.execute("SELECT 1 FROM export_reporting_datasets WHERE id=?", (dataset,)).fetchone():
                raise ValueError("Unknown imported dataset.")
            if allow:
                con.execute("""INSERT INTO export_reporting_grants VALUES (?, ?, ?, ?)
                    ON CONFLICT(dataset, principal) DO UPDATE SET
                    granted_by=excluded.granted_by, granted_at=excluded.granted_at""",
                            (dataset, principal, actor, _now()))
            else:
                con.execute("DELETE FROM export_reporting_grants WHERE dataset=? AND principal=?",
                            (dataset, principal))
            con.execute("INSERT INTO export_reporting_audit (at, actor, action, dataset, principal) VALUES (?, ?, ?, ?, ?)",
                        (_now(), actor, "grant" if allow else "revoke", dataset, principal))

    def catalog(self, principal: str) -> list[dict]:
        principal = _principal(principal)
        with self._connection() as con:
            rows = con.execute("""SELECT d.id, d.subscription, d.imported_at, d.row_count
                FROM export_reporting_datasets d JOIN export_reporting_grants g ON g.dataset=d.id
                WHERE g.principal=? ORDER BY d.id""", (principal,)).fetchall()
            reports = {r[0]: json.loads(r[1])["label"] for r in con.execute("""
                SELECT r.dataset, r.payload FROM export_reporting_reports r
                JOIN export_reporting_grants g ON g.dataset=r.dataset
                WHERE g.principal=?""", (principal,)).fetchall()}
        return [{"dataset_id": row[0], "subscription_id": row[1],
                 "last_import": row[2], "rows": row[3], "mode": "Export-backed",
                 "live_actions": False, "reporting_available": row[0] in reports,
                 **({"label": reports[row[0]]} if row[0] in reports else {})} for row in rows]

    def require_dataset(self, principal: str, dataset: str) -> dict:
        target = str(UUID(dataset))
        for item in self.catalog(principal):
            if item["dataset_id"] == target:
                return item
        raise PermissionError("No explicit grant for this imported dataset.")

    def report(self, principal: str, dataset: str) -> dict:
        target, principal = str(UUID(dataset)), _principal(principal)
        # Authorization and payload are read in one SQL statement, never a cached scope.
        with self._connection() as con:
            row = con.execute("""
                SELECT r.payload FROM export_reporting_reports r
                JOIN export_reporting_grants g ON g.dataset=r.dataset
                WHERE r.dataset=? AND g.principal=?""", (target, principal)).fetchone()
        if row is None:
            raise PermissionError("No explicit grant for this imported report.")
        return json.loads(row[0])

    def admin_catalog(self, user: auth.User, authentication: auth.Auth) -> list[dict]:
        self._admin(user, authentication)
        with self._connection() as con:
            rows = con.execute("SELECT id, subscription, imported_at FROM export_reporting_datasets ORDER BY imported_at DESC").fetchall()
            return [{"dataset_id": r[0], "subscription_id": r[1], "last_import": r[2],
                     "grants": [g[0] for g in con.execute(
                         "SELECT principal FROM export_reporting_grants WHERE dataset=?", (r[0],)).fetchall()]}
                    for r in rows]


def _now() -> str:
    return datetime.now(UTC).isoformat()
