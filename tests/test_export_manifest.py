"""The loader believes the run manifest, and stops guessing which files are a run.

The bug being designed out is specific. `ingest_export` used to list blobs, keep the ones with
a data extension, and take everything whose modified time shared the newest 16-character
prefix. During the first five days of a month Cost Management writes *two* runs per day -- one
for the month in progress, one restating the month just closed -- so two runs land minutes
apart, share that prefix, and get loaded as a single run. Every row in the overlap is counted
twice. That is the same double-count that once reported $920.98 for a month Azure billed at
$476.07, arriving by a different route.

A manifest removes the guess: it names its own blobs and states its own row count. These checks
run the real `ingest_export` against a fake container, so the assertions are about the shipped
code path rather than a reimplementation of it.
"""
import asyncio
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import app.exports as E
from app.scopes import Manifest

checks = 0
failures: list[str] = []


def check(label: str, ok: bool) -> None:
    global checks
    checks += 1
    print(f"  {'ok  ' if ok else 'FAIL'} {label}")
    if not ok:
        failures.append(label)


def section(name: str) -> None:
    print(f"\n{name}")


HEADER = ("ChargePeriodStart,BilledCost,BillingCurrency,SubscriptionId,"
          "ServiceName,ResourceId\n")


def csv_rows(day: str, n: int, cost: float = 1.0) -> bytes:
    body = "".join(
        f"{day},{cost},USD,sub-a,Storage,/subscriptions/sub-a/rg/r{i}\n" for i in range(n))
    return (HEADER + body).encode()


def manifest(run_id: str, submitted: str, start: str, end: str,
             files: list[tuple[str, int]]) -> bytes:
    return json.dumps({
        "manifestVersion": "2023-07-01",
        "dataRowCount": sum(n for _, n in files),
        "byteCount": 1,
        "runInfo": {"runId": run_id, "submittedTime": submitted,
                    "startDate": start, "endDate": end},
        "exportConfig": {"exportName": "focus-daily", "type": "FocusCost",
                         "timeFrame": "MonthToDate"},
        "deliveryConfig": {"partitionData": True,
                           "dataOverwriteBehavior": "CreateNewReport"},
        "blobs": [{"blobName": name, "byteCount": 1, "dataRowCount": n}
                  for name, n in files],
    }).encode()


class FakeReader:
    """Just enough of BlobReader to run the real ingest against."""

    def __init__(self, blobs: dict[str, bytes], modified: dict[str, str] | None = None):
        self.account = "fakeacct"
        self.container = "costexports"
        self._blobs = blobs
        self._modified = modified or {}
        self.fetched: list[str] = []
        self.closed = False

    async def list(self, prefix: str = ""):
        return [{"name": n,
                 "size": len(v),
                 "modified": self._modified.get(n, "2026-09-01T02:00:00Z")}
                for n, v in self._blobs.items() if n.startswith(prefix)]

    async def get(self, name: str) -> bytes:
        self.fetched.append(name)
        return self._blobs[name]

    async def close(self):
        self.closed = True


def run_ingest(reader, **kw):
    """Call the real ingest_export with the fake reader swapped in."""
    original = E.BlobReader
    E.BlobReader = lambda *a, **k: reader  # noqa: ARG005
    try:
        return asyncio.run(E.ingest_export(account="fakeacct", container="costexports", **kw))
    finally:
        E.BlobReader = original


def total_rows() -> int:
    from app.warehouse import warehouse
    return warehouse.row_count()


def clear() -> None:
    from app.warehouse import warehouse
    with warehouse.connect() as con:
        con.execute("DELETE FROM costs")


# ------------------------------------------------------ the two-runs-a-day double count
section("two runs on the same day are not loaded as one")

# Exactly the shape Cost Management writes on the 1st-5th of a month: an August run restating
# the closed month, and a September month-to-date run, submitted hours apart on the same day.
AUG = "exports/focus/20260801-20260831/run-aug"
SEP = "exports/focus/20260901-20260930/run-sep"
blobs = {
    f"{AUG}/manifest.json": manifest("run-aug", "2026-09-01T02:00:00Z",
                                     "2026-08-01T00:00:00Z", "2026-08-31T00:00:00Z",
                                     [(f"{AUG}/part_0_0001.csv", 10)]),
    f"{AUG}/part_0_0001.csv": csv_rows("2026-08-15", 10),
    f"{SEP}/manifest.json": manifest("run-sep", "2026-09-01T14:00:00Z",
                                     "2026-09-01T00:00:00Z", "2026-09-30T00:00:00Z",
                                     [(f"{SEP}/part_0_0001.csv", 4)]),
    f"{SEP}/part_0_0001.csv": csv_rows("2026-09-01", 4),
}

clear()
reader = FakeReader(blobs)
result = run_ingest(reader)

check("both runs are loaded, because they cover different periods", result["rows"] == 14)
check("and each is counted once", total_rows() == 14)
check("the manifests were read", any("manifest.json" in f for f in reader.fetched))
# The manifest path failing silently falls back to listing, which can produce the right answer
# for the wrong reason -- it did, while Manifest.parse was being called with a stale argument.
# So assert the manifest path was the one that ran, not merely that the totals came out.
check("and were actually used, not silently skipped", result["runs"] is not None)
check("both run ids are reported", sorted(result["runs"]) == ["run-aug", "run-sep"])
check("the declared total is recorded", result["declared_rows"] == 14)
check("and it matches what landed", result["row_count_mismatch"] is None)
check("the reader is closed", reader.closed is True)

# ---------------------------------------------------------------- superseded re-runs
section("a re-run of the same period supersedes rather than duplicates")

# The same August period exported twice -- which is what happens every day of a month-to-date
# export. Loading both would double every August row.
OLD = "exports/focus/20260801-20260831/run-old"
NEW = "exports/focus/20260801-20260831/run-new"
blobs = {
    f"{OLD}/manifest.json": manifest("run-old", "2026-08-24T02:00:00Z",
                                     "2026-08-01T00:00:00Z", "2026-08-31T00:00:00Z",
                                     [(f"{OLD}/part_0_0001.csv", 5)]),
    f"{OLD}/part_0_0001.csv": csv_rows("2026-08-15", 5, cost=1.0),
    f"{NEW}/manifest.json": manifest("run-new", "2026-08-25T02:00:00Z",
                                     "2026-08-01T00:00:00Z", "2026-08-31T00:00:00Z",
                                     [(f"{NEW}/part_0_0001.csv", 5)]),
    f"{NEW}/part_0_0001.csv": csv_rows("2026-08-15", 5, cost=9.0),
}

clear()
reader = FakeReader(blobs)
result = run_ingest(reader)

check("only the newer run is loaded", result["rows"] == 5)
check("not both", result["rows"] != 10)
check("and it is the newer one", result["runs"] == ["run-new"])
check("the superseded file is never even fetched",
      f"{OLD}/part_0_0001.csv" not in reader.fetched)

from app.warehouse import warehouse

with warehouse.reader() as r:
    cost = r.rows('SELECT sum("BilledCost") AS c FROM costs')[0]["c"]
check("the restated figures win", round(cost, 2) == 45.0)

# The old heuristic is what would have failed here: both runs share the same modified-time
# prefix, so a timestamp-based selection cannot tell them apart.
same_ts = dict.fromkeys(blobs, "2026-08-25T02:00:00Z")
clear()
reader = FakeReader(blobs, modified=same_ts)
result = run_ingest(reader)
check("identical timestamps do not confuse it, because it does not use them",
      result["rows"] == 5)

# ---------------------------------------------------------------- fallback
section("an export with no manifest still loads")

legacy = {
    "exports/legacy/20260801/part_0_0001.csv": csv_rows("2026-08-10", 3),
}
clear()
reader = FakeReader(legacy)
result = run_ingest(reader)
check("the listing fallback is used", result["rows"] == 3)
check("no runs are claimed", result["runs"] is None)
check("and no row count is claimed either", result["declared_rows"] is None)
check("so 'not checked' stays distinct from 'checked and correct'",
      result["row_count_mismatch"] is None)

# ---------------------------------------------------------------- bad manifest
section("an unreadable manifest cannot trigger guessed or incomplete replacement")

mixed = dict(blobs)
mixed["exports/focus/broken/manifest.json"] = b"{ this is not json"
clear()
reader = FakeReader(mixed)
try:
    run_ingest(reader)
except E.ExportError as exc:
    check("the malformed manifest is reported", "Cannot verify manifest" in str(exc))
else:
    check("the malformed manifest is refused", False)
check("no guessed data was inserted", total_rows() == 0)

# ---------------------------------------------------------------- shortfall
section("a partial load is reported, not absorbed")

# The manifest claims more rows than the file actually holds. Without the manifest this load
# looks completely successful and the dashboard is quietly short.
lying = {
    f"{NEW}/manifest.json": manifest("run-new", "2026-08-25T02:00:00Z",
                                     "2026-08-01T00:00:00Z", "2026-08-31T00:00:00Z",
                                     [(f"{NEW}/part_0_0001.csv", 500)]),
    f"{NEW}/part_0_0001.csv": csv_rows("2026-08-15", 5),
}
reader = FakeReader(lying)
before = total_rows()
try:
    run_ingest(reader)
except E.ExportError as exc:
    check("a manifest shortfall is refused before replacement",
          "500" in str(exc) and "5" in str(exc))
else:
    check("a manifest shortfall is refused before replacement", False)
check("existing history survives a manifest shortfall", total_rows() == before)

# ---------------------------------------------------------------- manifest helper
section("manifest blob selection")

m1 = Manifest.parse(manifest("r1", "2026-08-25T02:00:00Z", "2026-08-01T00:00:00Z",
                             "2026-08-31T00:00:00Z", [("a/x.csv", 1), ("a/y.csv", 2)]))
m2 = Manifest.parse(manifest("r2", "2026-09-01T02:00:00Z", "2026-09-01T00:00:00Z",
                             "2026-09-30T00:00:00Z", [("b/x.csv", 3)]))
picked = E._blobs_from_manifests([m1, m2])
check("every declared blob is included", len(picked) == 3)
check("names come through unchanged", picked[0]["name"] == "a/x.csv")
check("per-blob row counts are carried", [p["rows"] for p in picked] == [1, 2, 3])
check("the run id travels with each file",
      [p["run_id"] for p in picked] == ["r1", "r1", "r2"])

dup = Manifest.parse(manifest("r3", "2026-09-02T02:00:00Z", "2026-09-01T00:00:00Z",
                              "2026-09-30T00:00:00Z", [("b/x.csv", 3)]))
check("a file named by two runs is fetched once",
      len(E._blobs_from_manifests([m2, dup])) == 1)

print(f"\n{checks - len(failures)}/{checks} checks passed")
if failures:
    print("failed: " + ", ".join(failures))
    sys.exit(1)
