"""Incoming FOCUS rows, held in the database rather than in the process.

Why
---
The FOCUS importer read every row of an export into a Python list, and then built
two `Counter`s over it to work out which days had changed. That is why
`EXPORT_MAX_ROWS` is 100,000: measured on the real column set, one parsed row costs
576-810 bytes, so a month of a real customer's estate is

    100,000 rows (the cap)              0.1 GB
    5,813,121 rows (HDFC, August)       3.1-4.4 GB   before reconciliation

and the reconciler then holds the incoming multiset *and* the stored one on top of
that. The cap was not arbitrary -- it was the only thing standing between the
importer and the process running out of memory -- but it also meant the product
could not ingest a real estate at all: 5.8M rows against a 100k limit.

What this does instead
----------------------
Rows are streamed from each blob straight into an unlogged staging table as they
are parsed, so only the batch being copied is ever in memory. Everything the
importer used to ask of the list -- how many rows, which subscriptions, which days,
which rows fall outside the requested window -- is then asked of the table, where
answering it costs no process memory at all.

Unlogged because the table is rebuilt from the export on any failure: there is
nothing in it worth replaying from the write-ahead log, and skipping the WAL is
most of the cost of writing it.

Reconciliation
--------------
`changed_days` is the same comparison the `Counter` version made, expressed where
the data now is. A day has changed when the incoming and stored multisets for it
differ in *either* direction, which is `EXCEPT ALL` both ways:

    (staged EXCEPT ALL stored) gives rows arriving or arriving more often
    (stored EXCEPT ALL staged) gives rows removed or arriving less often

`EXCEPT ALL` keeps multiplicity, which is exactly what decrementing a `Counter`
did, so legitimate duplicate rows and zero-net credits still survive a comparison
rather than being collapsed. A plain `EXCEPT` would treat two identical charges as
one and miss a correction that removed a duplicate.
"""
from __future__ import annotations

import logging
from typing import Any

log = logging.getLogger("cloudlens.staging")

TABLE = "_focus_incoming"


def _columns() -> list[str]:
    from .warehouse import COLUMNS

    return list(COLUMNS)


def _quoted() -> str:
    return ", ".join(f'"{c}"' for c in _columns())


class Staged:
    """An export's rows, in the database, answering what the importer needs to know.

    Deliberately not a temp table. The importer parses on the event loop and writes
    from worker threads, and a temp table belongs to one session; an unlogged table
    under a name unique to this run can be filled and read by whichever pooled
    connection is to hand, and is dropped explicitly when the run ends.
    """

    def __init__(self, connect: Any, suffix: str) -> None:
        # A factory rather than a connection, because an import must be able to fail
        # validation without touching the database at all -- not merely without
        # writing to `costs`, but without so much as opening a session. The connection
        # is acquired on the first batch that is actually staged.
        self._connect = connect
        self._con: Any = None
        # The suffix comes from a uuid4, but it reaches DDL, so it is filtered to
        # characters that cannot end a quoted identifier rather than trusted.
        self.table = f"{TABLE}_{''.join(c for c in suffix if c.isalnum())[:32]}"
        self._rows = 0
        self._created = False

    @property
    def con(self) -> Any:
        """The connection this run stages on, opened on first use."""
        if self._con is None:
            self._con = self._connect()
        return self._con

    def connection(self) -> Any:
        """The same connection, for the caller's write transaction.

        Named rather than reached through `con` at the call site so that it is clear
        the importer is deliberately joining the staging session, not opening a
        second one that could not see the staged rows.
        """
        return self.con

    # ---------------------------------------------------------------- lifecycle
    def create(self) -> None:
        """Make the staging table, once, on first use.

        Lazy on purpose. An import validates the export -- its schema, its manifest,
        its declared row counts -- before it is entitled to write anything, and
        creating a table is a write. A run that is refused during validation must
        leave the database exactly as it found it, including having created nothing,
        so this is called from `add` rather than up front.
        """
        if self._created:
            return
        from .warehouse import COLUMNS, NUMERIC

        cols = ",\n  ".join(
            f'"{c}" ' + ("date" if c == "ChargePeriodStart"
                         else "double precision" if c in NUMERIC else "text")
            for c in COLUMNS)
        self.con.execute(f'DROP TABLE IF EXISTS "{self.table}"')
        # Unlogged where the engine has it. The table is rebuilt from the export on any
        # failure, so there is nothing in it worth replaying from the write-ahead log --
        # and skipping the WAL is most of the cost of writing it. SQLite, which the
        # offline tests run on, has no such concept and takes the plain form.
        try:
            self.con.execute(f'CREATE UNLOGGED TABLE "{self.table}" (\n  {cols}\n)')
        except Exception:  # noqa: BLE001 - a durable staging table is still a staging table
            self.con.execute(f'CREATE TABLE "{self.table}" (\n  {cols}\n)')
        self._created = True

    def drop(self) -> None:
        """Remove the staging table. Never raises: the import has already finished."""
        if not self._created:
            return
        try:
            self.con.execute(f'DROP TABLE IF EXISTS "{self.table}"')
        except Exception:  # noqa: BLE001 - space, not correctness
            log.info("staging: could not drop %s; it will be reclaimed on restart",
                     self.table, exc_info=True)

    def index(self) -> None:
        """Index the columns every later question groups or filters by.

        Built after loading rather than before: an index maintained row by row
        through a COPY costs more than one built once over a populated table.

        Named explicitly rather than left to the server. An anonymous `CREATE INDEX
        ON t (...)` is a PostgreSQL extension; naming it is both portable and the
        only way the index can be referred to afterwards.
        """
        if not self._created:
            return
        self.con.execute(
            f'CREATE INDEX "{self.table}_sub_period_idx" ON "{self.table}" '
            '(lower("SubAccountId"), "ChargePeriodStart")')
        self.con.execute(f'ANALYZE "{self.table}"')

    # ------------------------------------------------------------------- filling
    def add(self, records: list[list[Any]]) -> int:
        """Copy one parsed batch in. The caller may discard `records` afterwards."""
        from .warehouse import insert_rows

        if not records:
            return self._rows
        self.create()
        self._rows += insert_rows(self.con, records, table=f'"{self.table}"')
        return self._rows

    # ------------------------------------------------------------------ counting
    @property
    def rows(self) -> int:
        return self._rows

    def count(self) -> int:
        if not self._created:
            return 0
        return int(self.con.execute(
            f'SELECT count(*) FROM "{self.table}"').fetchone()[0] or 0)

    def span(self) -> tuple[str | None, str | None]:
        """The first and last charge date present, as ISO strings."""
        if not self._created:
            return None, None
        row = self.con.execute(
            f'SELECT min("ChargePeriodStart"), max("ChargePeriodStart") '
            f'FROM "{self.table}"').fetchone()
        return (str(row[0]) if row and row[0] else None,
                str(row[1]) if row and row[1] else None)

    def subscriptions(self) -> set[str]:
        if not self._created:
            return set()
        return {r[0] for r in self.con.execute(
            f'SELECT DISTINCT "SubAccountId" FROM "{self.table}" '
            'WHERE "SubAccountId" IS NOT NULL').fetchall() if r[0]}

    def has_rows_without_subscription(self) -> bool:
        if not self._created:
            return False
        # `trim` rather than PostgreSQL's `btrim`: both engines the warehouse runs on
        # understand the standard spelling.
        return bool(self.con.execute(
            f'SELECT EXISTS (SELECT 1 FROM "{self.table}" '
            "WHERE \"SubAccountId\" IS NULL OR trim(\"SubAccountId\") = '')").fetchone()[0])

    def day_rows(self) -> dict[tuple[str, str], int]:
        """How many rows landed on each (subscription, day).

        Small by construction -- one entry per subscription per day -- so this is
        the one result the importer still holds in memory, and it is what the
        per-day receipts are written from.
        """
        if not self._created:
            return {}
        return {(r[0], str(r[1])): int(r[2]) for r in self.con.execute(
            f'SELECT "SubAccountId", "ChargePeriodStart", count(*) '
            f'FROM "{self.table}" GROUP BY 1, 2').fetchall()}

    def days_for(self, sub: str) -> tuple[str | None, str | None]:
        if not self._created:
            return None, None
        row = self.con.execute(
            f'SELECT min("ChargePeriodStart"), max("ChargePeriodStart") '
            f'FROM "{self.table}" WHERE "SubAccountId" = ?', [sub]).fetchone()
        return (str(row[0]) if row and row[0] else None,
                str(row[1]) if row and row[1] else None)

    # ------------------------------------------------------------------ narrowing
    def restrict_to_window(self, lo: str, hi: str) -> int:
        """Drop rows outside the requested window. Returns how many were removed."""
        if not self._created:
            return 0
        before = self.count()
        self.con.execute(
            f'DELETE FROM "{self.table}" WHERE "ChargePeriodStart" < ? '
            'OR "ChargePeriodStart" > ?', [lo, hi])
        removed = before - self.count()
        self._rows -= removed
        return removed

    def restrict_to_subscriptions(self, subs: set[str]) -> int:
        """Drop rows outside the selected subscriptions. Returns how many were removed."""
        if not self._created:
            return 0
        if not subs:
            removed = self.count()
            self.con.execute(f'DELETE FROM "{self.table}"')
            self._rows -= removed
            return removed
        placeholders = ", ".join("?" for _ in subs)
        before = self.count()
        self.con.execute(
            f'DELETE FROM "{self.table}" WHERE "SubAccountId" IS NULL '
            f"OR \"SubAccountId\" NOT IN ({placeholders})", list(subs))
        removed = before - self.count()
        self._rows -= removed
        return removed

    def drop_protected(self, protected: set[tuple[str, str]]) -> None:
        """Forget rows for days a newer correction already owns."""
        if not self._created:
            return
        for sub, day in sorted(protected):
            self.con.execute(
                f'DELETE FROM "{self.table}" WHERE "SubAccountId" = ? '
                'AND "ChargePeriodStart" = ?', [sub, day])
        self._rows = self.count()

    # -------------------------------------------------------------- reconciling
    def changed_days(self, windows: dict[str, list[tuple[str, str]]],
                     protected: set[tuple[str, str]]) -> list[tuple[str, str]]:
        """The (subscription, day) pairs whose row multiset differs from what is stored.

        Both directions, because a day changes when a row arrives, when one is
        removed, and when the same row arrives a different number of times.

        Expressed as a signed count rather than `EXCEPT ALL` in both directions. Each
        side is grouped by the whole row, the staged side counted positive and the
        stored side negative, and any row whose totals do not cancel is a difference.
        That keeps multiplicity -- two identical charges are a group of two, so a
        correction dropping one of them does not cancel -- which a plain `EXCEPT`
        would lose, and unlike `EXCEPT ALL` it is understood by every engine the
        warehouse runs on, including the SQLite the offline tests use.

        Scoped to the declared windows: a day the export does not cover is not
        being restated, and comparing it would delete history this run never saw.
        """
        cols = _quoted()
        spans, params = [], []
        for sub, periods in sorted(windows.items()):
            for start, end in periods:
                spans.append('("SubAccountId" = ? AND "ChargePeriodStart" '
                             "BETWEEN ? AND ?)")
                params += [sub, start, end]
        if not spans:
            return []
        # The table is made here if nothing was staged, because an export that
        # legitimately contains no rows for a day still has to be compared: every
        # stored row on that day is then a difference, and the day is a removal. By
        # this point validation has passed and the write transaction is open, so
        # creating it is no longer a write the import was not entitled to make.
        self.create()
        scope = " OR ".join(spans)
        grouped = ", ".join(str(i + 1) for i in range(len(_columns())))
        rows = self.con.execute(
            f'SELECT DISTINCT "SubAccountId", "ChargePeriodStart" FROM ('
            f"  SELECT {cols} FROM ("
            f"    SELECT {cols}, count(*) AS n FROM \"{self.table}\" WHERE {scope} "
            f"    GROUP BY {grouped}"
            f"    UNION ALL "
            f"    SELECT {cols}, -count(*) AS n FROM costs WHERE {scope} "
            f"    GROUP BY {grouped}"
            f"  ) sides GROUP BY {grouped} HAVING sum(n) <> 0"
            f") d",
            params + params).fetchall()
        return sorted({(r[0], str(r[1])) for r in rows} - protected)

    def stored_rows_on(self, days: list[tuple[str, str]]) -> int:
        """How many stored rows the given days hold, for the importer's report."""
        total = 0
        for sub, day in days:
            total += int(self.con.execute(
                'SELECT count(*) FROM costs WHERE "SubAccountId" = ? '
                'AND "ChargePeriodStart" = ?', [sub, day]).fetchone()[0] or 0)
        return total

    def publish(self, days: list[tuple[str, str]] | None = None) -> int:
        """Copy staged rows into `costs`, for the given days or for all of them.

        `INSERT ... SELECT` rather than reading the rows back out: they are already
        in the database, and bringing 5.8M of them into Python to hand straight
        back is the cost this table exists to avoid.
        """
        from .warehouse import ensure_partitions_between

        if not self._created:
            return 0
        cols = _quoted()
        if days is None:
            lo, hi = self.span()
            if not lo or not hi:
                return 0
            ensure_partitions_between(self.con, lo, hi)
            self.con.execute(
                f'INSERT INTO costs ({cols}) SELECT {cols} FROM "{self.table}"')
            return self.count()
        if not days:
            return 0
        ensure_partitions_between(self.con, min(d for _, d in days),
                                  max(d for _, d in days))
        spans = " OR ".join('("SubAccountId" = ? AND "ChargePeriodStart" = ?)'
                            for _ in days)
        params = [v for pair in days for v in pair]
        written = int(self.con.execute(
            f'SELECT count(*) FROM "{self.table}" WHERE {spans}', params).fetchone()[0] or 0)
        self.con.execute(
            f'INSERT INTO costs ({cols}) SELECT {cols} FROM "{self.table}" '
            f"WHERE {spans}", params)
        return written
