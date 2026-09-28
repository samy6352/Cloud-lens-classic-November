"""Can a scoped connection see another subscription's money through any derived table?

This is the property that matters most before a customer deployment: two customers
share a deployment, each is scoped to their own subscriptions, and every derived
table is stored estate-wide. If any one of them is readable unshadowed on a scoped
connection, one customer sees another's spend.

Reading the code says each is shadowed. This asks the database instead: open a
connection scoped to a single subscription and require every relation reachable
through it to contain that subscription and nothing else.
"""
import os
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
for line in (ROOT / ".env").read_text().splitlines():
    line = line.strip()
    if line and not line.startswith("#") and "=" in line:
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip())
os.environ["PG_STATEMENT_TIMEOUT"] = "0"

from app.warehouse import DERIVED_MODULES, warehouse  # noqa: E402

with warehouse.connect(read_only=True) as con:
    subs = [r[0] for r in con.execute(
        'SELECT "SubAccountId" FROM costs_rollup GROUP BY 1 '
        'ORDER BY sum("BilledCost") DESC NULLS LAST LIMIT 2').fetchall()]
    total = con.execute("SELECT count(DISTINCT \"SubAccountId\") FROM costs_rollup").fetchone()[0]

mine, theirs = subs[0], subs[1]
print(f"estate holds {total} subscriptions")
print(f"scoping to   {mine}")
print(f"must not see {theirs}\n")

failures = []
with warehouse.connect(read_only=True, scope=[mine]) as con:
    for module in DERIVED_MODULES:
        table = module.TABLE
        column = "SubAccountId"
        if column not in set(module.DIMENSIONS):
            print(f"  {table:<22} no {column} in grain — skipped")
            continue
        rows = con.execute(
            f'SELECT lower("{column}"), count(*) FROM {table} GROUP BY 1').fetchall()
        seen = {r[0] for r in rows if r[0]}
        leaked = seen - {mine.lower()}
        verdict = "OK" if not leaked else f"LEAK: {len(leaked)} other subscriptions"
        if leaked:
            failures.append((table, sorted(leaked)[:3]))
        print(f"  {table:<22} {len(seen)} subscription(s) visible   {verdict}")

    # The detail view itself, which everything else falls back to.
    rows = con.execute(
        'SELECT lower("SubAccountId"), count(*) FROM costs GROUP BY 1').fetchall()
    seen = {r[0] for r in rows if r[0]}
    leaked = seen - {mine.lower()}
    if leaked:
        failures.append(("costs", sorted(leaked)[:3]))
    print(f"  {'costs':<22} {len(seen)} subscription(s) visible   "
          f"{'OK' if not leaked else 'LEAK'}")

print("\nVERDICT:", "scope holds on every derived relation" if not failures
      else f"SCOPE LEAK on {[f[0] for f in failures]}")
for table, examples in failures:
    print(f"  {table}: {examples}")
