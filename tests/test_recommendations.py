"""One AI narrative over data the Savings and Anomalies tabs already produced -- not a new
scan, so the one thing worth checking is that it never claims more than it was given.

Hermetic: `synthesize()` takes already-computed `savings`/`anomalies` dicts and a fake `agent`
whose `_openai()` returns a fake client, so no warehouse, no network, no real model call.

    python test_recommendations.py
"""
from __future__ import annotations

import asyncio
import json
import sys as _sys
from pathlib import Path as _Path

# Tests live in tests/ and import the application from the checkout root.
_sys.path.insert(0, str(_Path(__file__).resolve().parents[1]))

import os

os.environ.setdefault("AUTH_DISABLED", "true")

from app import recommendations as rec

passed = failed = 0


def check(name: str, ok: bool, detail: str = "") -> None:
    global passed, failed
    if ok:
        passed += 1
        print(f"  OK   {name}" + (f"  {detail}" if detail else ""))
    else:
        failed += 1
        print(f"  FAIL {name}  {detail}")


class FakeChoice:
    def __init__(self, content):
        self.message = type("M", (), {"content": content})()


class FakeResponse:
    def __init__(self, content):
        self.choices = [FakeChoice(content)]


class FakeCompletions:
    """Records the exact prompt sent, and answers with a canned reply."""

    def __init__(self, reply="A canned narrative.", raise_exc=None):
        self.reply = reply
        self.raise_exc = raise_exc
        self.last_call: dict | None = None

    async def create(self, **kwargs):
        self.last_call = kwargs
        if self.raise_exc:
            raise self.raise_exc
        return FakeResponse(self.reply)


class FakeClient:
    def __init__(self, completions: FakeCompletions):
        self.chat = type("Chat", (), {"completions": completions})()


class FakeAgent:
    def __init__(self, completions: FakeCompletions):
        self.model = "gpt-4.1-mini"
        self._client = FakeClient(completions)

    async def _openai(self):
        return self._client


SAMPLE_SAVINGS = {
    "opportunities": [
        {"title": "Right-size or shut down underutilised virtual machines",
         "category_label": "Rightsizing", "resource": "demovm1",
         "resource_group": "rg-demo", "window": 34.52, "annual": 420.0,
         "currency": "USD", "corroborated": True, "confidence": "high"},
        {"title": "Unassociated public IP addresses", "category_label": "Orphaned",
         "resource": None, "resource_group": None, "window": 4.62, "annual": 55.0,
         "currency": "USD", "corroborated": False, "confidence": "medium"},
    ],
    "billed_total": 34.52,
    "projected_total": 55.0,
    "count": 2,
}
SAMPLE_ANOMALIES = {
    "anomalies": [
        {"dimension": "ServiceName", "key": "Foundry Tools", "day": "2026-09-03",
         "delta": 175.4, "direction": "up", "severity": "high"},
    ],
}


async def main() -> int:
    print("=" * 72)
    print("what the model is actually sent")

    fc = FakeCompletions()
    agent = FakeAgent(fc)
    result = await rec.synthesize(agent, savings=SAMPLE_SAVINGS, anomalies=SAMPLE_ANOMALIES,
                                  currency="USD")

    check("a narrative comes back", result["narrative"] == "A canned narrative.")
    check("through the app's own model, not a hardcoded one", fc.last_call["model"] == "gpt-4.1-mini")
    sent = json.loads(fc.last_call["messages"][1]["content"])
    check("every opportunity sent is one that was actually in the data",
          {o["resource"] for o in sent["opportunities"] if o["resource"]} == {"demovm1"})
    check("the anomaly sent matches what analyse() actually found",
          sent["anomalies"][0]["key"] == "Foundry Tools" and sent["anomalies"][0]["delta"] == 175.4)
    check("the absolute rule against inventing figures is in the system prompt, not left implied",
          "must appear verbatim" in fc.last_call["messages"][0]["content"])
    check("the model is not handed the full nested shape -- sources/items are provenance for "
          "a table row, not something a 3-sentence summary should quote back",
          "sources" not in json.dumps(sent) and "items" not in json.dumps(sent))

    print()
    print("=" * 72)
    print("capped, not handed the whole list")

    big_savings = {
        "opportunities": [
            {"title": f"finding {i}", "category_label": "Rightsizing", "resource": f"vm-{i}",
             "resource_group": "rg", "window": float(100 - i), "annual": float((100 - i) * 12),
             "currency": "USD", "corroborated": False, "confidence": "low"}
            for i in range(rec.MAX_OPPORTUNITIES + 15)
        ],
        "billed_total": 1000.0, "projected_total": 0.0, "count": rec.MAX_OPPORTUNITIES + 15,
    }
    fc2 = FakeCompletions()
    agent2 = FakeAgent(fc2)
    await rec.synthesize(agent2, savings=big_savings, anomalies=None, currency="USD")
    sent2 = json.loads(fc2.last_call["messages"][1]["content"])
    check(f"capped at MAX_OPPORTUNITIES ({rec.MAX_OPPORTUNITIES}) even though more were given",
          len(sent2["opportunities"]) == rec.MAX_OPPORTUNITIES, len(sent2["opportunities"]))
    check("the costliest ones are kept, not an arbitrary slice",
          sent2["opportunities"][0]["resource"] == "vm-0")

    many_anomalies = {"anomalies": [
        {"dimension": "ServiceName", "key": f"svc-{i}", "day": "2026-09-03",
         "delta": float(100 - i), "direction": "up", "severity": "high"}
        for i in range(rec.MAX_ANOMALIES + 10)
    ]}
    fc3 = FakeCompletions()
    agent3 = FakeAgent(fc3)
    await rec.synthesize(agent3, savings=SAMPLE_SAVINGS, anomalies=many_anomalies, currency="USD")
    sent3 = json.loads(fc3.last_call["messages"][1]["content"])
    check(f"anomalies are capped at MAX_ANOMALIES ({rec.MAX_ANOMALIES}) too",
          len(sent3["anomalies"]) == rec.MAX_ANOMALIES, len(sent3["anomalies"]))

    print()
    print("=" * 72)
    print("nothing to summarise, and a failed call")

    empty = await rec.synthesize(agent, savings={"opportunities": []}, anomalies=None,
                                 currency="USD")
    check("an empty scope is said plainly rather than calling the model for nothing",
          empty["narrative"] is None and empty["based_on"] == 0, empty)

    fc_fail = FakeCompletions(raise_exc=RuntimeError("model unavailable"))
    agent_fail = FakeAgent(fc_fail)
    failed_result = await rec.synthesize(agent_fail, savings=SAMPLE_SAVINGS,
                                         anomalies=SAMPLE_ANOMALIES, currency="USD")
    check("a failed model call is swallowed into a result, not raised",
          failed_result["narrative"] is None and "error" in failed_result, failed_result)

    print()
    print("=" * 72)
    print(f"  {passed} passed, {failed} failed")
    return 1 if failed else 0


if __name__ == "__main__":
    _sys.exit(asyncio.run(main()))
