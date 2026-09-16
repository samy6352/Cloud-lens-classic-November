"""AI-generated spend recommendations: one narrative over data the rule-based tabs already
produced, rather than a third independent scan.

The Savings tab already merges six sources into a ranked list, corroboration and all; the
Anomalies tab already scores every unusual day. Both are reports -- correct, but something a
reader has to read in full to answer "where do I actually start". This module asks the model
to do that ordering and grouping, and nothing else: it is handed the same structured data those
tabs render, told exactly what it may and may not do with it, and produces prose, not a scan of
its own.

The one rule that matters more than the others: every figure in the answer must already be in
the data handed to the model. This is the same "never state a figure you did not receive from a
tool" rule `agent.py`'s own SYSTEM_PROMPT holds the chat agent to -- a hallucinated saving is
worse than none, because the reader cannot tell the difference from here.
"""
from __future__ import annotations

import json
import logging
from typing import Any

log = logging.getLogger("cloudlens.recommendations")

# How many of the ranked opportunities the model actually sees. The list itself can run past a
# hundred rows on a large estate; a summary that has to weigh all of them is slower and no more
# useful than one built from the costliest handful, since anything past this is already too
# small to be "where to start" by definition -- `_from_waste` and friends sort by cost first.
MAX_OPPORTUNITIES = 40
MAX_ANOMALIES = 5

PROMPT = """You are summarising cost-optimisation findings a rule-based scan already produced, \
for someone who has not read the raw data behind it. You are not scanning anything yourself and \
you have no knowledge of this estate beyond the JSON you are given.

## The rule that matters most
Every dollar figure, resource name, category and percentage in your answer must appear \
verbatim in the JSON below. Never estimate, round to a "nicer" number, combine two figures into \
a new one, or state anything the data does not support. A shorter, fully-grounded answer is \
correct; a fluent, plausible, partly-invented one is not -- the reader cannot tell the \
difference from here, which is exactly why this rule is absolute rather than a preference.

If the data holds nothing worth prioritising (few or trivial opportunities, no notable \
anomalies), say so plainly in one sentence rather than padding an answer out.

## What to produce
A short, prioritised narrative -- not a table, the numbers are already tabulated on the tabs \
this reads from:
1. Lead with the single biggest, most confident opportunity: name it and its figure.
2. Group the rest by theme rather than listing every row ("three idle VMs in rg-x total $Y" \
beats three separate sentences about each one).
3. Call out anything corroborated by more than one scan as a safer place to start -- that signal \
only exists once findings are compared, which is the one thing a sorted list cannot show.
4. Mention at most one anomaly, only if it is large and recent, and only to flag it. This data \
cannot explain *why* a day was unusual, so do not guess a cause.
5. End with one concrete next action, not a restatement of what you just said.

Three to six sentences, plain prose, no markdown headings, no bullet list longer than the \
findings themselves already are."""


def _trim_opportunities(opportunities: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """What the model needs to reason about, not the full nested shape a table row carries.

    `sources`, `items` and `options` are provenance and detail for a person already looking at
    the row on screen -- exactly what a three-sentence summary should point at, not quote back.
    """
    return [
        {
            "title": o.get("title"),
            "category": o.get("category_label") or o.get("category"),
            "resource": o.get("resource"),
            "resource_group": o.get("resource_group"),
            "window": o.get("window"),
            "annual": o.get("annual"),
            "currency": o.get("currency"),
            "corroborated": o.get("corroborated"),
            "confidence": o.get("confidence"),
        }
        for o in opportunities[:MAX_OPPORTUNITIES]
    ]


def _trim_anomalies(found: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        {
            "dimension": a.get("dimension"),
            "key": a.get("key"),
            "day": a.get("day"),
            "delta": a.get("delta"),
            "direction": a.get("direction"),
            "severity": a.get("severity"),
        }
        for a in found[:MAX_ANOMALIES]
    ]


async def synthesize(agent: Any, *, savings: dict[str, Any],
                     anomalies: dict[str, Any] | None, currency: str) -> dict[str, Any]:
    """One narrative over already-computed savings and anomaly data.

    `agent` is the app's single `CostAgent` (see app/agent.py) -- reused for its already-proven
    Foundry client construction and auth, not for its tool-calling loop. This is a plain one-shot
    completion: there is nothing here for the model to call a tool for, since every fact it may
    use already arrived in the prompt.
    """
    opportunities = (savings or {}).get("opportunities") or []
    if not opportunities and not ((anomalies or {}).get("anomalies")):
        return {
            "narrative": None,
            "based_on": 0,
            "note": "Nothing to summarise for this period and scope.",
        }

    payload = {
        "currency": currency,
        "billed_total": (savings or {}).get("billed_total"),
        "projected_total": (savings or {}).get("projected_total"),
        "opportunity_count": (savings or {}).get("count"),
        "opportunities": _trim_opportunities(opportunities),
        "anomalies": _trim_anomalies((anomalies or {}).get("anomalies") or []),
    }

    try:
        client = await agent._openai()
        response = await client.chat.completions.create(
            model=agent.model,
            messages=[
                {"role": "system", "content": PROMPT},
                {"role": "user", "content": json.dumps(payload, default=str)},
            ],
            temperature=0.1,
            max_tokens=400,
        )
        text = (response.choices[0].message.content or "").strip()
    except Exception as exc:  # noqa: BLE001 - a synthesis failure must not break the Savings tab
        log.warning("recommendation synthesis failed: %s", str(exc)[:200])
        return {"narrative": None, "based_on": len(payload["opportunities"]),
                "error": "Could not generate a summary right now."}

    return {
        "narrative": text or None,
        "based_on": len(payload["opportunities"]),
        "anomalies_considered": len(payload["anomalies"]),
        "currency": currency,
    }
