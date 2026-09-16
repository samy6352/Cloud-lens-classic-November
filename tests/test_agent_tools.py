"""Does every tool the model can see actually dispatch to something, and does the one tool
that writes to Azure fail the way its own validation intends rather than as a raw exception?

Hermetic: `create_budget`'s error paths are all raised by `budgets.build_body`/`build_filter`
before any Azure call is made, so a bad name or a missing tag value is testable without a
warehouse, a credential, or the network.
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
for s in (sys.stdout, sys.stderr):
    try:
        s.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass


async def main() -> int:
    passed = failed = 0

    def check(label, ok, detail=""):
        nonlocal passed, failed
        print(f"  {'OK  ' if ok else 'FAIL'} {label}  {detail}")
        if ok:
            passed += 1
        else:
            failed += 1

    from app import agent as a

    print("=" * 72)
    print("every tool the model can call actually dispatches to something")

    tool_names = {t["function"]["name"] for t in a.TOOLS}
    check("TOOLS is non-empty", len(tool_names) > 0, len(tool_names))
    check("every TOOLS entry has a HANDLERS entry",
          tool_names <= a.HANDLERS.keys(), tool_names - a.HANDLERS.keys())
    check("every HANDLERS entry is a declared tool",
          a.HANDLERS.keys() <= tool_names, a.HANDLERS.keys() - tool_names)

    print("\n" + "=" * 72)
    print("create_budget is registered as the one write tool")

    check("create_budget is a declared tool", "create_budget" in tool_names)
    check("create_budget dispatches to the wrapper, not budgets.create directly",
          a.HANDLERS.get("create_budget") is a.create_budget)

    schema = next(t for t in a.TOOLS if t["function"]["name"] == "create_budget")
    required = set(schema["function"]["parameters"]["required"])
    check("required parameters match budgets.create's required keyword args",
          required == {"subscription_id", "name", "amount", "tags"}, required)

    print("\n" + "=" * 72)
    print("create_budget is not scope-injected — it takes its own subscription_id")

    check("not in SCOPED_TOOLS", "create_budget" not in a.SCOPED_TOOLS)
    check("not in SUBSCRIPTION_ARG_TOOLS", "create_budget" not in a.SUBSCRIPTION_ARG_TOOLS)

    print("\n" + "=" * 72)
    print("create_budget surfaces BudgetError as a tool result, not a raised exception")

    # No subscription_id: budgets.create raises BudgetError before any Azure call.
    result = await a.create_budget(
        subscription_id="", name="ok-name", amount=100, tags=[{"key": "env", "values": ["prod"]}],
    )
    check("a missing subscription is a tool error, not an exception",
          isinstance(result, dict) and "error" in result, result)

    # An invalid name: same path, still before any network call.
    result = await a.create_budget(
        subscription_id="00000000-0000-0000-0000-000000000000", name="bad name!", amount=100,
        tags=[{"key": "env", "values": ["prod"]}],
    )
    check("an invalid budget name is a tool error, not an exception",
          isinstance(result, dict) and "error" in result and "letter or digit" in result["error"],
          result)

    print("\n" + "=" * 72)
    print(f"  {passed} passed, {failed} failed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
