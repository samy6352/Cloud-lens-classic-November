"""Opt-in, exact-key indexes; no new financial grain or readiness generation.

The normal derived lifecycle creates these on both active and staged sides. A
reader also checks the actual published side: a flag/name alone must never enable
hundreds of parameterized probes against an unindexed computed source.
"""
from __future__ import annotations

import os
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

from . import environment

ENABLED = os.getenv("CLOUDLENS_READ_INDEXES", "false").lower() == "true"


def _definition(module: str) -> tuple[str, tuple[str, ...], tuple[str, ...]]:
    if module == "resource_presence":
        return "identity_v1", (
            '"ResourceId"', '"RegionName"', '"BillingCurrency"', '"ChargePeriodStart"',
        ), ()
    if module == "billing_rollup":
        return "lookup_v1", (
            'lower("SubAccountId")', "currency", "resource_id", '"ChargePeriodStart"',
        ), ('"SubAccountId"', "recorded_cost_total") + (
            ('"Environment"',) if environment.ENABLED else ())
    raise ValueError(f"No read index defined for {module}")


def index_sql(schema: str, table: str, module: str) -> list[str]:
    if not ENABLED:
        return []
    suffix, keys, included = _definition(module)
    include = f' INCLUDE ({", ".join(included)})' if included else ""
    return [f'CREATE INDEX IF NOT EXISTS {table}_read_{suffix} '
            f'ON "{schema}".{table} ({", ".join(keys)}){include}']


def available(con: Any, schema: str, table: str, module: str) -> bool:
    suffix, keys, included = _definition(module)
    row = con.execute(
        "SELECT i.indnkeyatts, ARRAY(SELECT pg_get_indexdef(i.indexrelid, n, true) "
        "FROM generate_series(1, i.indnatts) n) "
        "FROM pg_index i JOIN pg_class t ON t.oid=i.indrelid "
        "JOIN pg_namespace ns ON ns.oid=t.relnamespace "
        "JOIN pg_class idx ON idx.oid=i.indexrelid JOIN pg_am am ON am.oid=idx.relam "
        "WHERE ns.nspname=? AND t.relname=? AND idx.relname=? "
        "AND i.indisvalid AND i.indisready AND i.indislive AND i.indpred IS NULL "
        "AND am.amname='btree' AND NOT EXISTS ("
        "SELECT 1 FROM unnest(i.indoption) opt WHERE opt <> 0) "
        "AND NOT EXISTS (SELECT 1 FROM unnest(i.indclass) op "
        "JOIN pg_opclass c ON c.oid=op WHERE NOT c.opcdefault) "
        "AND NOT EXISTS (SELECT 1 FROM unnest(i.indcollation) col "
        "JOIN pg_collation c ON c.oid=col WHERE NOT c.collisdeterministic)",
        [schema, table, f"{table}_read_{suffix}"]).fetchone()
    return bool(row and row[0] == len(keys) and tuple(row[1]) == keys + included)


@contextmanager
def certified(con: Any, schema: str, modules: set[str],
              published: list[str]) -> Iterator[frozenset[str]]:
    if not ENABLED or not modules:
        yield frozenset()
        return
    from types import SimpleNamespace

    from . import derived

    # Pin the published views and their underlying sides until the reader exits.
    # Otherwise a rebuild could publish an unindexed side between certification
    # and the bounded-key query. Writes/ingest remain compatible with these locks.
    with con.raw.transaction():
        # Match the publisher's order across *all* summaries, not only the indexed
        # ones: later queries in this burst also read the money/FX views. Locking a
        # subset can deadlock with a publisher already holding another view.
        relations = ", ".join(f'"{schema}".{name}' for name in published)
        con.execute(f"LOCK TABLE {relations} IN ACCESS SHARE MODE")
        supported = set()
        for module in sorted(modules):
            _definition(module)
            table = derived.active_table(con, schema, SimpleNamespace(TABLE=module))
            if table and available(con, schema, table, module):
                supported.add(module)
        yield frozenset(supported)
