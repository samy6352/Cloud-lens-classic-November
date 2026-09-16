"""Billing provenance, null coverage, upgrade safety and scoped currency projection.

Runs through the script collector in an isolated PostgreSQL schema, never the live warehouse.
"""
from __future__ import annotations

import asyncio
import csv
import io
import json
import os
import sys
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
assert os.environ.get("PGSCHEMA", "").startswith("t_cl_test_billing_ingest_")

import httpx
import pyarrow as pa
import pyarrow.parquet as pq

from app import currency as fx
from app import exports
from app.scopes import Manifest
from app.warehouse import (
    BILLING_COLUMNS,
    BILLING_MONEY,
    BILLING_UNIT_PRICES,
    COLUMNS,
    NULLABLE_NUMERIC,
    NUMERIC,
    Warehouse,
    insert_rows,
    warehouse,
)

EXPECTED = ("InvoiceCost", "ListCost", "ContractedCost", "EffectiveCost", "ListUnitPrice",
            "ContractedUnitPrice", "PricingCurrency", "ExchangeRatePricingToBilling",
            "CommitmentDiscountType", "CommitmentDiscountStatus", "ChargeClass", "BillingCostBasis")
assert BILLING_COLUMNS == EXPECTED
assert len(NULLABLE_NUMERIC) == 7
SUB = {"id": "sub-a", "name": "A"}
checked = 0


def check(condition, label):
    global checked
    assert condition, label
    checked += 1
    print(f"  OK {label}")


with patch.object(warehouse, "currencies", return_value=["USD", "INR"]):
    try:
        warehouse._check_currency('select sum("invoicecost") from costs', None)
    except ValueError:
        check(True, "invoice totals reject mixed native currencies")
    else:
        check(False, "invoice totals must not bypass the currency guard")
    warehouse._check_currency(
        'select "billingcurrency", sum("invoicecost") from costs group by "billingcurrency"', None)
    check(True, "invoice totals may be grouped by native currency")


def csv_bytes(values):
    out = io.StringIO()
    writer = csv.writer(out)
    writer.writerow(values)
    writer.writerow(values.values())
    return out.getvalue().encode()


def load(values, metric=None, details=False):
    raw = csv_bytes(values)
    if details:
        records = warehouse._parse_blob(raw, SUB, metric)
    else:
        header, rows = exports._rows_from(raw)
        records, skipped = exports._load_rows(header, rows, SUB["id"], metric)
        assert skipped == 0
    assert len(records) == 1 and len(records[0]) == len(COLUMNS)
    return dict(zip(COLUMNS, records[0]))


focus = {
    "ChargePeriodStart": "2026-08-15", "BilledCost": "0", "BillingCurrency": "INR",
    "SubAccountId": "sub-a", "ListCost": "2400", "ContractedCost": "1800",
    "EffectiveCost": "1200", "ListUnitPrice": "100", "ContractedUnitPrice": "75",
    "PricingQuantity": "24", "PricingCurrency": "USD", "ExchangeRatePricingToBilling": "80",
    "CommitmentDiscountType": "Reservation", "CommitmentDiscountStatus": "Used",
    "ChargeCategory": "Usage", "ChargeClass": "Correction",
}
for details in (False, True):
    row = load(focus, details=details)
    check(row["InvoiceCost"] == 0 and row["BilledCost"] == 0, "FOCUS invoice zero is real")
    check(row["EffectiveCost"] == 1200 and row["ListCost"] == 2400
          and row["ContractedCost"] == 1800, "FOCUS preserves independent financial totals")
    check(row["BillingCostBasis"] == "FOCUS" and row["CommitmentDiscountType"] == "Reservation"
          and row["CommitmentDiscountStatus"] == "Used" and row["ChargeClass"] == "Correction",
          "FOCUS preserves cost basis and commitment provenance")

legacy = {
    "date": "2026-08-15", "costInBillingCurrency": "80", "costInUsd": "1",
    "billingCurrency": "INR", "pricingCurrency": "USD", "ExchangeRatePricingToBilling": "80",
    "quantity": "2", "paygPrice": "2", "unitPrice": "1.5", "effectivePrice": ".5",
    "chargeType": "Usage", "pricingModel": "OnDemand",
}
for details in (False, True):
    for basis, invoice, effective in (("ActualCost", 80, 80), ("AmortizedCost", None, 80),
                                      (None, None, None)):
        row = load(legacy, basis, details)
        check(row["BilledCost"] == 80 and row["UnitPrice"] == 1.5,
              "legacy selected cost and price unchanged")
        check(row["InvoiceCost"] == invoice and row["EffectiveCost"] == effective
              and row["BillingCostBasis"] == basis, f"metric provenance survives {basis}")
        check(row["ListCost"] == 320 and row["ContractedCost"] == 240
              and row["ListUnitPrice"] == 2 and row["ContractedUnitPrice"] == 1.5,
              "legacy pricing currency converted before deriving totals")

for overrides, expected_type in (
    ({"pricingModel": "Reservation"}, "Reservation"),
    ({"pricingModel": "SavingsPlan"}, "SavingsPlan"),
    ({"pricingModel": "", "reservationId": "r-1"}, "Reservation"),
    ({"pricingModel": "", "benefitId": "/providers/microsoft.billingBenefits/savingsPlanOrders/a/savingsPlans/b"},
     "SavingsPlan"),
    ({"pricingModel": "", "benefitName": "my-reservation-looking-name"}, None),
):
    row = load({**legacy, **overrides}, "ActualCost")
    check(row["InvoiceCost"] == 80 and row["EffectiveCost"] is None
          and row["CommitmentDiscountType"] == expected_type,
          "actual committed or ambiguous usage is not inferred effective cost")

for field in ("pricingCurrency", "ExchangeRatePricingToBilling", "quantity"):
    data = {k: v for k, v in legacy.items() if k != field}
    row = load(data, "ActualCost")
    check(row["ListCost"] is None and row["ContractedCost"] is None,
          f"missing {field} leaves derived cost unavailable")
for invalid in ("", "bogus", "NaN", "Infinity", "-Infinity"):
    row = load({**focus, **dict.fromkeys(NULLABLE_NUMERIC, invalid), "BilledCost": "0"})
    check(all(row[c] is None for c in NULLABLE_NUMERIC if c != "InvoiceCost")
          and row["InvoiceCost"] == 0, f"optional invalid numeric {invalid!r} stays NULL")
for invalid_rate in ("0", "-2", "NaN"):
    row = load({**legacy, "ExchangeRatePricingToBilling": invalid_rate}, "ActualCost")
    check(row["ListCost"] is None and row["ContractedCost"] is None,
          "invalid pricing exchange never fabricates totals")
row = load({k: v for k, v in legacy.items() if k not in ("paygPrice", "unitPrice")}, "ActualCost")
check(row["UnitPrice"] == .5 and row["ListUnitPrice"] is None
      and row["ContractedUnitPrice"] is None and row["ListCost"] is None,
      "effectiveprice alias cannot masquerade as list or contracted price")
row = load({**legacy, "paygPrice": "0", "unitPrice": "0"}, "AmortizedCost")
check(row["ListCost"] == 0 and row["ContractedCost"] == 0, "supplied price zero preserved")
row = load({**legacy, "pricingCurrency": "INR", "ExchangeRatePricingToBilling": ""}, "ActualCost")
check(row["ListCost"] == 4 and row["ExchangeRatePricingToBilling"] is None,
      "equal known currencies need no invented exchange field")
row = load({**legacy, "chargeType": "Purchase"}, "ActualCost")
check(row["EffectiveCost"] is None and row["ListCost"] is None, "purchases not priced as usage")

minimal = load({"ChargePeriodStart": "2026-08-15", "BilledCost": "0"})
check(minimal["InvoiceCost"] == 0 and minimal["ListCost"] is None
      and minimal["UnitPrice"] == 0 and minimal["PricingQuantity"] == 0,
      "optional fields NULL while legacy numeric defaults remain zero")
effective_only = load({"ChargePeriodStart": "2026-08-15", "EffectiveCost": 5})
check(effective_only["BilledCost"] == 5 and effective_only["EffectiveCost"] == 5
      and effective_only["InvoiceCost"] is None,
      "FOCUS effective-only fallback never becomes invoice cost")

# A new archive preserves the stored basis and missing values. Old snapshots are not FOCUS.
for basis in ("FOCUS", "ActualCost", "AmortizedCost", None):
    values = dict.fromkeys(COLUMNS)
    values.update(ChargePeriodStart="2026-08-15", BilledCost=7, BillingCurrency="USD",
                  SubAccountId="sub-a", BillingCostBasis=basis, ListCost=10)
    restored = load(values, "FocusCost")
    check(restored["BilledCost"] == 7 and restored["InvoiceCost"] is None
          and restored["BillingCostBasis"] == basis and restored["ListCost"] == 10,
          "canonical archive never reinterprets BilledCost")
    sink = io.BytesIO()
    pq.write_table(pa.table({k: [v] for k, v in values.items()}), sink)
    header, rows = exports._read_any("snapshot.parquet", sink.getvalue())
    parsed, _ = exports._load_rows(header, rows, None, "FocusCost")
    check(dict(zip(COLUMNS, parsed[0])) == restored, "Parquet and CSV archive mapping agree")
old = {c: "" for c in COLUMNS if c not in BILLING_COLUMNS}
old.update(ChargePeriodStart="2026-08-15", BilledCost=7, UnitPrice=11)
restored = load(old, "FocusCost")
check(all(restored[c] is None for c in BILLING_COLUMNS), "old archive cannot gain invoice or prices")


async def plumbing():
    async def handle(request):
        if request.method == "POST":
            return httpx.Response(200, json={"properties": {
                "columns": [{"name": n} for n in ("Cost", "CostUSD", "UsageDate", "Currency")],
                "rows": [[0, 0, 20260815, "USD"]],
            }})
        return httpx.Response(200, content=csv_bytes(legacy))

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        for basis in ("ActualCost", "AmortizedCost"):
            records = await warehouse._fetch(client, ["https://example.invalid/part"], SUB, basis)
            data = dict(zip(COLUMNS, records[0]))
            check(data["BillingCostBasis"] == basis, "Cost Details download carries metric to parser")
            records = await warehouse._query_period(
                client, SUB, "2026-08-01", "2026-08-31", {}, basis)
            data = dict(zip(COLUMNS, records[0]))
            supplied = "InvoiceCost" if basis == "ActualCost" else "EffectiveCost"
            check(data[supplied] == 0 and data["BillingCostBasis"] == basis
                  and all(data[c] is None for c in BILLING_COLUMNS
                          if c not in (supplied, "BillingCostBasis")),
                  "quick API retains basis and zero cost without fabricating unavailable fields")


asyncio.run(plumbing())
manifest = Manifest.parse({
    "runInfo": {"runId": "r"}, "exportConfig": {"type": "AmortizedCost"},
    "blobs": [{"blobName": "part.csv"}],
})
check(exports._blobs_from_manifests([manifest])[0]["metric"] == "AmortizedCost",
      "manifest dataset accompanies each blob")


class ReportReader:
    account = "fixture"
    container = "fixture"

    def __init__(self, basis):
        self.data = {
            # `submittedTime` is not decoration: latest_per_period uses it to decide which run
            # supersedes another for the same period, and refuses to guess when it is absent.
            # Real Cost Management manifests always carry it, as does every other manifest
            # fixture in this suite; this one omitted it and so never reached the ingest.
            "manifest.json": json.dumps({
                "runInfo": {"runId": "r", "startDate": "2026-08-01", "endDate": "2026-08-31",
                            "submittedTime": "2026-09-01T01:00:00Z"},
                "exportConfig": {"type": basis},
                "blobs": [{"blobName": "part.csv", "dataRowCount": 1}],
            }).encode(),
            "part.csv": csv_bytes(legacy),
        }

    async def list(self, prefix=""):
        return [{"name": name, "modified": "2026-09-01", "size": len(raw)}
                for name, raw in self.data.items()]

    async def get(self, name):
        return self.data[name]

    async def close(self):
        pass


for basis in ("ActualCost", "AmortizedCost"):
    with patch.object(exports, "BlobReader", return_value=ReportReader(basis)):
        result = asyncio.run(exports.ingest_export(
            account="fixture", container="fixture", subscription_id="manifest",
            metric="ActualCost" if basis == "AmortizedCost" else "AmortizedCost"))
    with warehouse.reader(scope=["manifest"]) as reader:
        data = reader.rows('SELECT "InvoiceCost", "EffectiveCost", "BillingCostBasis" FROM costs')[0]
    check(result["rows"] == 1 and data["BillingCostBasis"] == basis
          and data["EffectiveCost"] == 80
          and data["InvoiceCost"] == (80 if basis == "ActualCost" else None),
          "complete export ingestion trusts manifest metric over conflicting caller hint")

# Boot twice over pre-upgrade rows, once with the historical order and once reordered.
for reverse in (False, True):
    columns = [c for c in COLUMNS if c not in BILLING_COLUMNS]
    if reverse:
        columns.reverse()
    ddl = ", ".join(f'"{c}" ' + ("DOUBLE" if c in NUMERIC else
                                "DATE" if c == "ChargePeriodStart" else "VARCHAR")
                    for c in columns)
    with warehouse.connect() as con:
        con.execute("DROP TABLE costs")
        con.execute(f"CREATE TABLE costs ({ddl})")
        con.execute('INSERT INTO costs ("ChargePeriodStart", "BilledCost", "BillingCurrency", '
                    '"SubAccountId", "Tags") VALUES (?, ?, ?, ?, ?)',
                    ["2026-08-15", 123.45, "USD", "legacy", '{"preserve":true}'])
    warehouse._init_schema()
    warehouse._init_schema()
    with warehouse.reader(scope=["legacy"]) as reader:
        stored = reader.rows("SELECT * FROM costs")[0]
    check(stored["BilledCost"] == 123.45 and stored["Tags"] == '{"preserve":true}'
          and all(stored[c] is None for c in BILLING_COLUMNS),
          "additive migration and repeat boot preserve old rows and missing financial coverage")
    with warehouse.connect() as con:
        values = load(focus)
        insert_rows(con, [[values[c] for c in COLUMNS]])
    with warehouse.reader(scope=["sub-a"]) as reader:
        check(reader.rows('SELECT "EffectiveCost" FROM costs')[0]["EffectiveCost"] == 1200,
              "named inserts survive reordered physical schema")

with warehouse.connect() as con:
    con.execute("DELETE FROM costs")
    values = [
        load(focus),
        load({**legacy, "SubscriptionId": "sub-a"}, "ActualCost"),
        load({**focus, "SubAccountId": "sub-a", "BillingCurrency": "ZZZ",
              "PricingCurrency": "", "ExchangeRatePricingToBilling": "",
              "BilledCost": 0, "EffectiveCostInUsd": 1}),
        load({**focus, "SubAccountId": "other-sub", "ListCost": 999999}),
    ]
    insert_rows(con, [[value[c] for c in COLUMNS] for value in values])

with patch.dict(os.environ, {"FX_RATES": '{"INR":80.0}'}), \
        patch.dict(fx._cache, {"rates": {}, "at": 0, "date": None, "source": None}):
    with warehouse.reader(scope=["sub-a"], currency="USD") as reader:
        rows = reader.rows('SELECT * FROM costs ORDER BY "ListCost" NULLS LAST')
    check(len(rows) == 3, "currency projection preserves privacy-aware subscription scope")
    actual, committed, unknown = rows
    check(actual["InvoiceCost"] == 1 and actual["ListCost"] == 4
          and actual["ContractedCost"] == 3 and actual["EffectiveCost"] == 1
          and actual["ListUnitPrice"] == 2 and actual["ContractedUnitPrice"] == 1.5,
          "billing totals and legacy pricing unit rates project coherently")
    check(committed["InvoiceCost"] == 0 and committed["EffectiveCost"] == 15
          and committed["ListCost"] == 30 and committed["ListUnitPrice"] == 1.25,
          "zero-invoice RI uses known exchange, never divides by invoice zero")
    check(all(unknown[c] is None for c in BILLING_MONEY | BILLING_UNIT_PRICES)
          and unknown["ExchangeRatePricingToBilling"] is None,
          "missing conversion produces NULL money, never relabelled native amount")
    check(all(r["BillingCurrency"] == "USD" and r["PricingCurrency"] == "USD" for r in rows),
          "projected currency labels match projected money")
    with warehouse.reader(scope=["sub-a"], currency="INR") as reader:
        native = reader.rows('SELECT "ListCost" FROM costs WHERE "ListCost" = 2400')
    check(len(native) == 1, "target already equals native currency: no double conversion")
    with warehouse.reader(scope=["sub-a"]) as reader:
        raw = reader.rows('SELECT "PricingCurrency", "BillingCurrency", "ListCost" FROM costs '
                          'WHERE "ListCost" = 2400 ORDER BY "BillingCurrency"')
    # Ordered, and asserted on both rows. Unordered, this read `raw[0]` out of a query with no
    # ORDER BY and demanded "USD": PostgreSQL is free to return either row first, and returned
    # the one whose pricing currency is absent. Comparing the whole pair also says more --
    # every native label survives the converted read, not merely whichever row arrived first.
    check(len(raw) == 2
          and [(r["PricingCurrency"], r["BillingCurrency"]) for r in raw]
          == [("USD", "INR"), (None, "ZZZ")],
          "reader does not mutate native storage")

# Conversion cases where invoice zero, mismatched FOCUS metric aliases, or a non-USD
# pricing currency would make a blanket CostInUsd/BilledCost conversion wrong.
with warehouse.connect() as con:
    con.execute("DELETE FROM costs")
    values = [
        load({**focus, "SubAccountId": "same", "BillingCurrency": "USD",
              "PricingCurrency": "", "ExchangeRatePricingToBilling": "",
              "BilledCost": 10, "EffectiveCostInUsd": 5}),
        load({**focus, "SubAccountId": "live", "PricingCurrency": "",
              "ExchangeRatePricingToBilling": ""}),
        load({**legacy, "SubscriptionId": "ratio", "billingCurrency": "ZZZ",
              "pricingCurrency": "", "ExchangeRatePricingToBilling": ""}, "ActualCost"),
        load({**focus, "SubAccountId": "nonusd", "BillingCurrency": "ZZZ",
              "PricingCurrency": "SEK", "ExchangeRatePricingToBilling": 2}),
    ]
    insert_rows(con, [[value[c] for c in COLUMNS] for value in values])
with patch.dict(os.environ, {"FX_RATES": '{"INR":80.0,"SEK":10.0}'}), \
        patch.dict(fx._cache, {"rates": {}, "at": 0, "date": None, "source": None}):
    with warehouse.reader(currency="USD") as reader:
        cases = {r["SubAccountId"]: r for r in reader.rows("SELECT * FROM costs")}
    check(cases["same"]["InvoiceCost"] == 10 and cases["same"]["BilledCost"] == 10,
          "FOCUS billed cost never uses mismatched EffectiveCostInUsd")
    check(cases["live"]["InvoiceCost"] == 0 and cases["live"]["EffectiveCost"] == 15,
          "zero-invoice usage can use known native display FX when provider FX absent")
    check(cases["ratio"]["InvoiceCost"] == 1 and cases["ratio"]["EffectiveCost"] == 1,
          "legacy cost/USD metric pair supplies a valid row exchange factor")
    check(cases["nonusd"]["ListCost"] == 120 and cases["nonusd"]["EffectiveCost"] == 60,
          "known non-USD pricing currency and provider rate project unknown billing currency")

check(not __import__("app.rollup", fromlist=["covers"]).covers([], ["InvoiceCost"]),
      "financial coverage queries cannot silently use lossy rollup")
check(Warehouse._map_query({"columns": [{"name": "UsageDate"}], "rows": [[20260815]]}, SUB)[0][-1]
      is None, "unknown quick metric stays unknown")
print(f"\n{checked} billing ingestion checks passed.")
