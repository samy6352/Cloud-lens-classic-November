"""Who looked at what, and when -- the access log itself, not the page that shows it.

# cloudlens-test: requires-warehouse

`record_access`/`recent_access` are exercised directly against a fake connection, the same way
test_commitments_inventory.py fakes `self.wh.reader(...)` for `Commitments.inventory()` -- no
real row is ever written or read. The marker above is about the *import*, not the checks: unlike
`commitments.py`, which only reaches into `warehouse.py` lazily inside `Commitments.__init__`,
`Warehouse` is defined in the same module whose last line constructs the real singleton and
opens a real PostgreSQL connection to run schema setup. Importing the class at all -- the only
way to call its methods unbound against a fake `self` -- pays that connection once, up front,
exactly as `test_onboarding.py` already does for the same reason.

    python test_access_log.py
"""
from __future__ import annotations

import sys as _sys
from pathlib import Path as _Path

# Tests live in tests/ and import the application from the checkout root.
_sys.path.insert(0, str(_Path(__file__).resolve().parents[1]))

import os

os.environ.setdefault("AUTH_DISABLED", "true")

from app.warehouse import Warehouse

passed = failed = 0


def check(name: str, ok: bool, detail: str = "") -> None:
    global passed, failed
    if ok:
        passed += 1
        print(f"  OK   {name}" + (f"  {detail}" if detail else ""))
    else:
        failed += 1
        print(f"  FAIL {name}  {detail}")


class FakeCursor:
    def __init__(self, rows):
        self._rows = rows

    def fetchall(self):
        return self._rows

    def fetchone(self):
        return self._rows[0] if self._rows else None


class FakeConn:
    """Records every statement it is asked to run, and answers with canned rows."""

    def __init__(self, rows=()):
        self.calls: list[tuple[str, list]] = []
        self._rows = rows

    def execute(self, sql, params=None):
        self.calls.append((sql, list(params or [])))
        return FakeCursor(self._rows)

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class FakeSelf:
    """Enough of a `Warehouse` to call the two unbound methods against, without its
    `__init__` ever opening a real PostgreSQL connection."""

    def __init__(self, rows=()):
        self.last_conn = None
        self._rows = rows

    def connect(self, read_only=False):
        self.last_conn = FakeConn(self._rows)
        return self.last_conn


print("=" * 72)
print("recording a view")

fs = FakeSelf()
Warehouse.record_access(fs, user_name="ravi", email="ravi@example.com", source="entra",
                        is_admin=True, tab="savings")
sql, params = fs.last_conn.calls[0]
check("writes to access_log", "INSERT INTO access_log" in sql, sql)
check("carries who, from where, and what they looked at",
      params == ["ravi", "ravi@example.com", "entra", True, "savings"], params)

print()
print("=" * 72)
print("a write that fails must never break the page it is logging")


class ExplodingConn:
    def execute(self, *a, **kw):
        raise RuntimeError("connection refused")

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class ExplodingSelf:
    def connect(self, read_only=False):
        return ExplodingConn()


try:
    Warehouse.record_access(ExplodingSelf(), user_name="ravi", email="", source="file",
                            is_admin=False, tab="overview")
    check("a failed write is swallowed, not raised", True)
except Exception as exc:  # noqa: BLE001
    check("a failed write is swallowed, not raised", False, f"{type(exc).__name__}: {exc}")

print()
print("=" * 72)
print("reading it back")

rows = [
    ("ravi", "ravi@example.com", "entra", True, "savings", "2026-09-05 18:42:12.345000"),
    ("colleague", "", "file", False, "overview", "2026-09-05 18:40:01.000000"),
]
fs2 = FakeSelf(rows=rows)
entries = Warehouse.recent_access(fs2, limit=50)
sql, params = fs2.last_conn.calls[0]
check("reads from access_log, most recent first", "ORDER BY at DESC" in sql, sql)
check("the limit is passed through, capped rather than trusted blindly",
      params == [50], params)
check("every column comes back under a readable name",
      entries[0] == {"user": "ravi", "email": "ravi@example.com", "source": "entra",
                     "admin": True, "tab": "savings", "at": "2026-09-05 18:42:12.345000"},
      entries[0])
check("both rows are present, in the order the query returned them",
      [e["user"] for e in entries] == ["ravi", "colleague"])

# Azure's own API caps are clamped rather than trusted (`min(days, 365)` etc. throughout this
# app); the audit log's own limit follows the same convention.
fs3 = FakeSelf(rows=[])
Warehouse.recent_access(fs3, limit=5000)
_, params = fs3.last_conn.calls[0]
check("an oversized limit is capped rather than passed straight to the query",
      params == [500], params)

print()
print("=" * 72)
print("a read that fails must never break the page it is logging")


class ExplodingReadSelf:
    def connect(self, read_only=False):
        return ExplodingConn()


try:
    out = Warehouse.recent_access(ExplodingReadSelf(), limit=10)
    check("a failed read returns an empty log, not an exception", out == [], out)
except Exception as exc:  # noqa: BLE001
    check("a failed read returns an empty log, not an exception", False,
          f"{type(exc).__name__}: {exc}")

print()
print("=" * 72)
print(f"  {passed} passed, {failed} failed")
raise SystemExit(1 if failed else 0)
