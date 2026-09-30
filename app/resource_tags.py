"""Each resource's tags, once per month, so no tab reads `Tags` from `costs`.

Why
---
Tags, Resources and Uptime each read the `Tags` column of the detail table for every
row in the window, and all three timed out on the 16.9M-row estate. Tags is most of the
bytes: measured on one real month it averages 992 bytes of a narrow projection's ~1.6KB
per row, and it is not stable within a resource-day -- Databricks clusters carry per-job
tags (`JobId`, `ClusterName`), so one resource showed 1,980 distinct tag sets in a
month. Carrying `Tags` in any daily grain therefore produces a table nearly the size of
`costs` itself, which is why this is a separate table rather than a column on
`resource_presence`.

What it is
----------
One row per month, subscription, resource and billing currency, holding the tags that
resource carried on the latest day it carried any (the lexically greatest set on that
day, under the C collation, so the choice is deterministic). Two more columns keep the
window arithmetic exact:

* `TaggedOn` -- the day those tags come from. A reader wants the latest tagged day
  *inside its window*. For the latest month overlapping a window that is this row's
  day whenever it falls inside the window; when it falls before the window start, the
  resource carried no tags in that month's part of the window at all. So filtering
  rows on `TaggedOn` and taking the latest is exactly "the tags on the latest tagged
  day in the window" -- no partial-month approximation.
* `UnknownDays` -- a bit per day of the month on which the source could not say
  whether the resource was tagged (tags absent from a non-FOCUS export). A reader masks
  it to its own window, so "tags unavailable" is exact per window too.

Readers look tags up for the rows they are about to return (at most a few thousand),
through the index on `ResourceKey`, never by scanning the table.
"""
from __future__ import annotations

import datetime as dt
from collections.abc import Sequence

from . import schema_v2

TABLE = "resource_tags"
PERIOD = "ChargePeriodStart"
DIMENSIONS = [PERIOD, "SubAccountId", "ResourceKey", "BillingCurrency"]
# Nothing here is summed, so there are no MEASURES; the columns beside the grain are
# listed so the boot-time shape check knows the table's full set of columns.
MEASURES: list[str] = []
ATTRIBUTES = ["TaggedOn", "Tags", "UnknownDays"]

# The identity every tab already groups by when a charge has no resource id, folded to
# lower case and trimmed: Azure ids are case-insensitive and arrive in several casings,
# and one index then serves every caller. Callers match with `key_sql(<their id>)`.
KEY = "lower(trim(COALESCE(NULLIF(\"ResourceId\", ''), \"ResourceName\", '(unnamed)')))"


def key_sql(expression: str) -> str:
    return f"lower(trim({expression}))"
TAGGED = "TRIM(COALESCE(\"Tags\", '')) NOT IN ('', '{}', '[]', 'null')"
# Same test the Tags tab applied per row: a FOCUS export always carries tags, so an empty
# value there means untagged; elsewhere an empty value means the source did not say.
UNKNOWN = ("NOT (\"Tags\" IS NOT NULL AND (COALESCE(\"BillingCostBasis\" = 'FOCUS', FALSE) "
           "OR TRIM(\"Tags\") NOT IN ('', 'null')))")


def serves_filters(filters: dict[str, list[str]] | None) -> bool:
    """Tags are an attribute of the resource, not of a filtered slice of its charges.

    Callers narrow the *resource list* by the filter bar and then look tags up for
    exactly those resources, so the filters are already applied by the time this
    table is read.
    """
    return True


def build_windows(windows: Sequence[tuple[dt.date, dt.date]] | None
                  ) -> list[tuple[dt.date, dt.date]] | None:
    """A month's representative depends on every day of it, so repair whole months."""
    if windows is None:
        return None
    from .jobs import merge_windows

    return merge_windows([
        (schema_v2.month_start(lo), schema_v2.next_month(hi - dt.timedelta(days=1)))
        for lo, hi in merge_windows(windows)
    ])


def table_sql(schema: str, table: str = TABLE) -> str:
    return (f'CREATE TABLE IF NOT EXISTS "{schema}".{table} ('
            f'"{PERIOD}" date, "SubAccountId" text, "ResourceKey" text, '
            f'"BillingCurrency" text, "TaggedOn" date, "Tags" text, "UnknownDays" integer)')


def index_sql(schema: str, table: str = TABLE) -> list[str]:
    return [
        f'CREATE INDEX IF NOT EXISTS {table}_key_idx '
        f'ON "{schema}".{table} ("ResourceKey", "{PERIOD}")',
        f'CREATE INDEX IF NOT EXISTS {table}_sub_idx '
        f'ON "{schema}".{table} (lower("SubAccountId"), "{PERIOD}")',
    ]


def select_sql(source: str, window: tuple[dt.date, dt.date] | None = None) -> str:
    # One pass, and no per-group array: prefixing each tag set with its ISO day makes
    # the latest day's greatest set the maximum of a single string, and the C collation
    # keeps that comparison byte-wise whatever the database's default is.
    latest = (f"max(CASE WHEN {TAGGED} THEN to_char(\"{PERIOD}\", 'YYYY-MM-DD') "
              f"|| '|' || \"Tags\" END COLLATE \"C\")")
    unknown = (f"bit_or(CASE WHEN {UNKNOWN} "
               f"THEN 1 << (EXTRACT(DAY FROM \"{PERIOD}\")::int - 1) ELSE 0 END)")
    where = ""
    if window is not None:
        start, stop = window
        where = (f" WHERE \"{PERIOD}\" >= DATE '{start.isoformat()}'"
                 f" AND \"{PERIOD}\" < DATE '{stop.isoformat()}'")
    # Rows that are neither tagged nor uncertain carry no information: a resource with
    # no row here is simply untagged, so leaving them out costs nothing to readers.
    return (
        f"SELECT CAST(date_trunc('month', \"{PERIOD}\") AS date) AS \"{PERIOD}\", "
        f"\"SubAccountId\", {KEY} AS \"ResourceKey\", \"BillingCurrency\", "
        f"CAST(left({latest}, 10) AS date) AS \"TaggedOn\", "
        # Back to the default collation: the byte-wise one is for choosing, and would
        # otherwise leak into the stored column and conflict with the declared shape.
        f"CAST(substr({latest}, 12) AS text) COLLATE \"default\" AS \"Tags\", "
        f"CAST({unknown} AS integer) AS \"UnknownDays\" "
        f"FROM {source}{where} GROUP BY 1, 2, 3, 4 "
        f"HAVING {latest} IS NOT NULL OR {unknown} <> 0")


def _mask(start: str, hi: str) -> str:
    """The bits of `UnknownDays` that fall inside `(start, hi]` for this row's month."""
    first = f"GREATEST(t.\"{PERIOD}\", CAST(({start}) AS date) + 1)"
    last = f"LEAST(CAST(t.\"{PERIOD}\" + INTERVAL '1 month' AS date) - 1, DATE '{hi}')"
    return (f"(CASE WHEN {last} < {first} THEN 0::bigint ELSE "
            f"(((1::bigint << ({last} - {first} + 1)) - 1) "
            f"<< (EXTRACT(DAY FROM {first})::int - 1)) END)")


def lateral_sql(source: str, key: str, currency: str, start: str, hi: str,
                normalise: str = "COALESCE(NULLIF({c}, ''), 'unknown')") -> str:
    """A `LEFT JOIN LATERAL` giving `tg.tags` and `tg.unknown` for one returned row.

    `key` and `currency` are the outer row's expressions; `normalise` is how the caller
    spells its currency, so the match is made in the caller's own terms. `start` is the
    window's exclusive lower edge as SQL, `hi` its inclusive upper day.
    """
    latest = (f"max(CASE WHEN t.\"TaggedOn\" > ({start}) AND t.\"TaggedOn\" <= DATE '{hi}' "
              f"THEN to_char(t.\"TaggedOn\", 'YYYY-MM-DD') || '|' || t.\"Tags\" END "
              f"COLLATE \"C\")")
    stored_currency = normalise.format(c='t."BillingCurrency"')
    return (
        f"LEFT JOIN LATERAL (SELECT substr({latest}, 12) COLLATE \"default\" AS tags, "
        f"COALESCE(bool_or((CAST(t.\"UnknownDays\" AS bigint) & {_mask(start, hi)}) <> 0), "
        f"FALSE) AS unknown "
        f"FROM {source} t WHERE t.\"ResourceKey\" = {key_sql(key)} "
        f"AND {stored_currency} = {currency} "
        f"AND t.\"{PERIOD}\" >= CAST(date_trunc('month', CAST(({start}) AS date) + 1) AS date) "
        f"AND t.\"{PERIOD}\" <= DATE '{hi}') AS tg ON TRUE")
