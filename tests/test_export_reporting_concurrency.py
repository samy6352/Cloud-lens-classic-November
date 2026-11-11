import asyncio
import sys
import threading
from collections import defaultdict
from decimal import Decimal
from types import SimpleNamespace

import pytest

from app import export_reporting
from tests.test_export_reporting import offline_modules as offline_modules

SUB = "44444444-4444-4444-4444-444444444444"
HEADER = ["SubAccountId", "ChargePeriodStart", "BillingCurrency",
          "ServiceName", "BilledCost", "EffectiveCost"]


def test_real_focus_partition_preserves_decimal_precision(offline_modules):
    groups = defaultdict(lambda: [Decimal(0), Decimal(0), 0])
    raw = (",".join(HEADER) + "\n"
           + f"{SUB},2026-11-01,USD,Compute,0.10,0.20\n"
           + f"{SUB},2026-11-01,USD,Compute,-0.03,0.00\n").encode()
    observed = export_reporting._aggregate_partition(
        "part.csv", raw, SUB, "2026-11-01", "2026-11-30", groups, 0, 2,
    )
    assert observed == 2
    assert groups[("2026-11-01", "Compute", "USD")] == [
        Decimal("0.07"), Decimal("0.20"), 2,
    ]


@pytest.mark.parametrize("subscription", [SUB, "55555555-5555-5555-5555-555555555555"])
def test_partition_iteration_does_not_block_requests(monkeypatch, subscription):
    entered, release = threading.Event(), threading.Event()
    state = {"closed": False}
    blob = SimpleNamespace(name="run/part.csv", bytes=1, rows=2)
    manifest = SimpleNamespace(
        dataset="FocusCost", export_name="approved", blobs=[blob],
        row_count=2, byte_count=1, run_id="run", submitted="2026-11-10",
    )

    class Reader:
        def __init__(self, account, container):
            self.manifest_metadata = {("run", "2026-11-10"): {"document": {"run": "run"}}}

        async def get(self, name):
            return b"x"

        async def close(self):
            state["closed"] = True

    def rows():
        entered.set()
        if not release.wait(5):
            raise AssertionError("event loop blocked during row iteration")
        for billed, effective in [("0.10", "0.20"), ("-0.03", "0.00")]:
            yield [subscription, "2026-11-01", "USD", "Compute", billed, effective]

    async def manifests(*args, **kwargs):
        return [manifest]

    monkeypatch.setitem(sys.modules, "app.exports", SimpleNamespace(
        BlobReader=Reader, ExportError=type("ExportError", (Exception,), {}),
        read_manifests=manifests,
        _focus_windows=lambda _: {"run/part.csv": ("2026-11-01", "2026-11-30")},
        _read_any=lambda *args: (HEADER, rows()),
        _validated_focus_rows=lambda header, iterator: iterator,
    ))

    async def run():
        task = asyncio.create_task(export_reporting.read_report({
            "account": "account", "container": "container", "prefix": "run",
            "export_name": "approved", "subscription": SUB, "label": "test",
        }))
        try:
            async with asyncio.timeout(2):
                while not entered.is_set():
                    await asyncio.sleep(0.005)
            # A separate coroutine must run while the parser is actively iterating.
            await asyncio.sleep(0)
        finally:
            release.set()
        return await task

    if subscription != SUB:
        with pytest.raises(ValueError, match="different subscription"):
            asyncio.run(run())
    else:
        report, _, count = asyncio.run(run())
        assert count == 2
        assert report["totals"] == [
            {"currency": "USD", "billed_cost": "0.07", "effective_cost": "0.20"},
        ]
    assert state["closed"]
