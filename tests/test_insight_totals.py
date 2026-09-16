"""The count and the sum in the analysis summary must describe the same findings.

`addressable` deliberately leaves out anomalies: a spike has been detected, not shown to be
recoverable. The sentence printed beside it counted a different set, which included them. On a
real estate with one $150.05 waste finding and one $97.18 anomaly that produced:

    "Across 5 findings: 2 recoverable USD 150 over 30 days"

Two findings, one finding's money. Whichever number a reader trusted, the other was wrong.
"""
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
for s in (sys.stdout, sys.stderr):
    try:
        s.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

os.environ.setdefault("PROJECT_ENDPOINT", "https://example.invalid/api/projects/test")

from app.insights import UNCOUNTED_KINDS, Finding, _counted, _summary  # noqa: E402

fails: list[str] = []


def check(label: str, condition: bool, detail: str = "") -> None:
    if condition:
        print(f"  OK   {label}")
    else:
        print(f"  FAIL {label}{': ' + detail if detail else ''}")
        fails.append(label)


def finding(kind: str, impact: float, *, severity: str = "warning",
            effort: str = "Low", title: str = "") -> Finding:
    return Finding(
        kind=kind,
        title=title or f"a {kind} finding",
        detail="",
        impact=impact,
        severity=severity,
        effort=effort,
        currency="USD",
        confidence="measured",
        basis="test fixture",
        source="test",
    )


def summarise(findings: list[Finding]) -> dict:
    addressable = sum(f.impact for f in _counted(findings))
    return _summary(findings, addressable, "USD", 30, {})


# ------------------------------------------------------------------ the reported case
print("=" * 72)
print("the estate this was found on")

real = [
    finding("anomaly", 97.18, title="Foundry Models rose sharply on 2026-08-18"),
    finding("advisor", 4008.0, title="Azure Advisor has 19 cost recommendations"),
    finding("waste", 150.05, title="App Service plans with no apps"),
    finding("governance", 0.0, title="66% of spend carries no tag"),
    finding("commitment", 0.0, title="Compute is running almost entirely on-demand"),
]
out = summarise(real)
print(f"       {out['situation']}")
check("one finding's money is attributed to one finding",
      "1 recoverable USD 150" in out["situation"], out["situation"])
check("the anomaly is not counted as recoverable",
      "2 recoverable" not in out["situation"], out["situation"])
check("the headline still reports the addressable total",
      out["headline"] == "USD 150 of addressable waste", out["headline"])

# Sequencing is a different question from accounting, and the anomaly still wins it: a $97 spike
# nobody has explained is a better first move than a $150 tidy-up.
check("the anomaly is still what to do first",
      "Foundry" in out["priority"], out["priority"][:90])


# ------------------------------------------------------------------ the invariant
print("\n" + "=" * 72)
print("the count and the sum agree, whatever the mix")

cases = {
    "waste only": [finding("waste", 150.0)],
    "two wastes": [finding("waste", 150.0), finding("waste", 50.0)],
    "anomaly only": [finding("anomaly", 97.0)],
    "advisor only": [finding("advisor", 4008.0)],
    "zero-impact only": [finding("governance", 0.0), finding("commitment", 0.0)],
    "one of everything": [finding("waste", 150.0), finding("anomaly", 97.0),
                          finding("advisor", 4008.0), finding("budget", 10.0),
                          finding("governance", 0.0)],
    "idle plus shutdown": [finding("idle", 20.0), finding("shutdown", 30.0),
                           finding("anomaly", 900.0)],
}

for name, findings in cases.items():
    counted = _counted(findings)
    total = sum(f.impact for f in counted)
    text = summarise(findings)["situation"]
    if counted:
        expected = f"{len(counted)} recoverable USD {total:,.0f}"
        check(f"{name}: says {expected!r}", expected in text, text)
    else:
        check(f"{name}: claims nothing recoverable", "recoverable" not in text, text)


# ------------------------------------------------------------------ the rule itself
print("\n" + "=" * 72)
print("what is left out of the money, and what is not")

for kind in ("budget", "advisor", "anomaly"):
    check(f"{kind} money is not claimed as recoverable",
          _counted([finding(kind, 100.0)]) == [])
for kind in ("waste", "idle", "shutdown", "orphan"):
    check(f"{kind} money is claimed", len(_counted([finding(kind, 100.0)])) == 1)

check("a zero-impact finding is never counted", _counted([finding("waste", 0.0)]) == [])
check("a negative impact is never counted", _counted([finding("waste", -5.0)]) == [])
check("the excluded kinds are stated in one place",
      set(UNCOUNTED_KINDS) == {"budget", "advisor", "anomaly"}, str(UNCOUNTED_KINDS))


print("\n" + "=" * 72)
if fails:
    print(f"  {len(fails)} failed: " + "; ".join(fails))
else:
    print("  all checks passed")
sys.exit(1 if fails else 0)
