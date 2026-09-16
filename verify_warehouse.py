"""Verify the warehouse holds sane data, and time some real questions against it.

Run against a server you can reach, with PGHOST/PGUSER/PGPASSWORD set as the test suite
expects. Exits non-zero if a statement that must be refused was allowed, or if the
subscription-identity invariant is broken.

The SQL below is written in the application's own dialect -- `strftime`, `round(x, 2)`,
`TRY_CAST` -- which `app/sqldialect.py` translates to PostgreSQL on the way to the server.
That is deliberate and is what every query module in the app uses; it is not DuckDB residue.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from app.warehouse import warehouse

s = warehouse.summary()
print("WAREHOUSE")
print(f"  rows          : {s['rows']:,}")
print(f"  range         : {s['from']} to {s['to']}")
print(f"  subscriptions : {s['subscriptions']}")
# `total_cost` is None in two legitimate cases, and this used to assume a float and crash on
# both: an empty warehouse, and one holding subscriptions billed in different currencies --
# where `summary()` deliberately returns no total rather than adding euros to dollars.
if s["total_cost"] is None:
    print(f"  total         : (none — {'empty' if not s['rows'] else 'mixed currencies'})")
else:
    print(f"  total         : {s['total_cost']:,.2f} {s['currency']}")

QUERIES = [
    ("Monthly totals",
     'SELECT strftime("ChargePeriodStart", \'%Y-%m\') AS month, round(sum("BilledCost"),2) AS cost '
     'FROM costs GROUP BY 1 ORDER BY 1'),
    ("July by service (top 8)",
     'SELECT "ServiceName", round(sum("BilledCost"),2) AS cost FROM costs '
     'WHERE "ChargePeriodStart" BETWEEN DATE \'2026-07-01\' AND DATE \'2026-07-31\' '
     'GROUP BY 1 ORDER BY cost DESC LIMIT 8'),
    ("By subscription, July",
     'SELECT "SubAccountName", round(sum("BilledCost"),2) AS cost FROM costs '
     'WHERE "ChargePeriodStart" BETWEEN DATE \'2026-07-01\' AND DATE \'2026-07-31\' '
     'GROUP BY 1 ORDER BY cost DESC'),
    ("Top 5 resources overall",
     'SELECT "ResourceName", "ServiceName", round(sum("BilledCost"),2) AS cost FROM costs '
     'GROUP BY 1,2 ORDER BY cost DESC LIMIT 5'),
    ("Regions",
     'SELECT "RegionName", round(sum("BilledCost"),2) AS cost FROM costs '
     'WHERE "RegionName" IS NOT NULL GROUP BY 1 ORDER BY cost DESC LIMIT 5'),
    ("Something only SQL can do: daily spend, 7-day moving average",
     'SELECT "ChargePeriodStart" AS d, round(sum("BilledCost"),2) AS cost, '
     'round(avg(sum("BilledCost")) OVER (ORDER BY "ChargePeriodStart" ROWS 6 PRECEDING),2) AS avg7 '
     'FROM costs GROUP BY 1 ORDER BY 1 DESC LIMIT 5'),
    ("Untagged spend share",
     'SELECT CASE WHEN "Tags" IS NULL OR "Tags" = \'\' THEN \'untagged\' ELSE \'tagged\' END AS t, '
     'round(sum("BilledCost"),2) AS cost FROM costs GROUP BY 1 ORDER BY cost DESC'),
]

print("\nQUERIES")
total_ms = 0
for title, sql in QUERIES:
    try:
        r = warehouse.query(sql, limit=10)
        total_ms += r["ms"]
        print(f"\n  {title}  [{r['ms']}ms]")
        for row in r["rows"]:
            print("    ", {k: (round(v, 2) if isinstance(v, float) else str(v)[:42])
                           for k, v in row.items()})
    except Exception as exc:
        print(f"\n  {title} -> FAILED: {str(exc)[:200]}")

print(f"\n  {len(QUERIES)} queries in {total_ms}ms total")

print("\nSAFETY")
# The statements that must be refused, and *why* each one matters on PostgreSQL.
#
# This list used to end with `read_csv('/etc/passwd')`, which is a DuckDB function. The
# translator passes it through untouched -- it is not part of the dialect -- so PostgreSQL
# refused it with "function does not exist". That looked like a guard catching an attack and
# was nothing of the kind: the guard was never consulted, and the same line would have kept
# printing "refused" no matter what the guard did. Every entry below is reachable on the
# engine this actually runs on.
CHECKS = [
    ("DROP TABLE costs", "DDL behind a leading verb"),
    ("DELETE FROM costs", "a write"),
    ("INSERT INTO costs VALUES (1)", "a write"),
    ("SELECT pg_read_file('/etc/passwd')", "reading a server file -- a read, so READ ONLY allows it"),
    ("SELECT * FROM public.costs_rollup", "a qualified name reaching past the scope view"),
    ("SELECT table_to_xml('public.costs_rollup', true, false, '')",
     "the same reach, hidden inside a string literal where the scanner cannot see it"),
    ("SELECT query_to_xml('SELECT * FROM public.costs', true, false, '')",
     "a function that evaluates SQL handed to it as text"),
]
failures = 0
for bad, why in CHECKS:
    try:
        warehouse.query(bad)
        failures += 1
        print(f"  !! ALLOWED: {bad}")
        print(f"     {why}")
    except Exception as exc:
        print(f"  refused: {bad[:46]:<48} ({type(exc).__name__})")

print(f"\n  {len(CHECKS) - failures}/{len(CHECKS)} refused")

print("\nIDENTITY")
# The invariant behind the worst bug this warehouse has had. A subscription stored under two
# spellings is summed twice, and the total stays plausible while it happens -- so this is
# checked rather than assumed.
check = warehouse.identity_check()
print(f"  {'ok' if check['ok'] else 'FAIL'}  {check['detail']}")

raise SystemExit(1 if failures or not check["ok"] else 0)
