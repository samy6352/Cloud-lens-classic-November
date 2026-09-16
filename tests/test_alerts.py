"""Email alerts for cost anomalies, sent by Azure rather than by this app.

The design decision worth testing is that this app never sends mail. A budget carries
`contactEmails` and Azure delivers; anomaly alerts work the same way through a scheduled action.
Sending it ourselves would mean credentials, deliverability, unsubscribe handling and a
background job that outlives every deploy, in exchange for nothing the customer can see.

The failure this exists to catch is quieter than a bounce. Azure gives these alerts an end date
and says nothing when it passes: the alert on the live subscription expired on 2026-07-21 and
had been sending nothing for six weeks while still showing as Enabled. An alert everyone
believes is running is worse than no alert at all.

    python test_alerts.py
"""


from __future__ import annotations

import sys as _sys
from pathlib import Path as _Path

# Tests live in tests/ and import the application from the checkout root.
_sys.path.insert(0, str(_Path(__file__).resolve().parents[1]))

import asyncio
import os
from datetime import UTC, datetime

os.environ.setdefault("AUTH_DISABLED", "true")
# app.main refuses to start without this. It used to be picked up from the developer's
# own .env, so this test passed on a machine that had one and failed on a fresh clone.
os.environ.setdefault("PROJECT_ENDPOINT", "https://example.invalid/api/projects/test")

from app import alerts

passed = failed = 0
NOW = datetime(2026, 9, 1, tzinfo=UTC)


def check(name: str, ok: bool, detail: str = "") -> None:
    global passed, failed
    if ok:
        passed += 1
        print(f"  OK   {name}" + (f"  {detail}" if detail else ""))
    else:
        failed += 1
        print(f"  FAIL {name}  {detail}")


def action(end="2027-07-21T05:59:10.825Z", status="Enabled", to=("a@b.com",), kind="InsightAlert"):
    return {
        "name": "dailyanomalybyresourcegroup", "kind": kind,
        "properties": {
            "displayName": "Daily anomaly by resource group", "status": status,
            "notification": {"to": list(to), "subject": "Cost anomaly detected"},
            "schedule": {"frequency": "Daily", "hourOfDay": 12, "endDate": end},
        },
    }


print("=" * 72)
print("an alert that has quietly stopped")

# The real one, read from the live subscription: enabled, and expired six weeks ago.
s = alerts.summarise(action(end="2026-07-21T05:59:10.825Z"), now=NOW)
check("an expired alert is called expired", s["expired"] is True)
check("and is not counted as sending, however Enabled it looks",
      s["sending"] is False and s["status"] == "Enabled",
      "the portal shows this one as on")
check("with something to do about it",
      "no longer sending" in (s["warning"] or ""), (s["warning"] or "")[:60])
check("the days are counted rather than the date shown raw",
      s["expires_in_days"] == -42, str(s["expires_in_days"]))

s = alerts.summarise(action(end="2027-07-21T05:59:10.825Z"), now=NOW)
check("a live alert is sending", s["sending"] is True and s["expired"] is False)
check("and warns about nothing", s["warning"] is None)

s = alerts.summarise(action(status="Disabled", end="2027-07-21T05:59:10.825Z"), now=NOW)
check("a disabled alert is not sending either", s["sending"] is False)
check("but is not blamed on expiry", s["warning"] is None, "it was turned off deliberately")

s = alerts.summarise(action(end=None), now=NOW)
check("no end date is unknown rather than expired",
      s["expired"] is False and s["expires_in_days"] is None)
s = alerts.summarise(action(end="not-a-date"), now=NOW)
check("and an unparsable one does not crash the list", s["expires_in_days"] is None)

print()
print("=" * 72)
print("recipients")

def body(emails, **kw):
    return alerts.build_body(subscription_id="sub-a", emails=emails,
                             display_name="Daily cost anomaly", **kw)


b = body(["a@b.com", "c@d.com"])
check("both are carried", b["properties"]["notification"]["to"] == ["a@b.com", "c@d.com"])
check("and the first is repeated where Azure wants it",
      b["properties"]["notificationEmail"] == "a@b.com",
      "Azure rejects the action when the two disagree")

for bad in ("", "   ", "not-an-email", "a@b"):
    raised = ""
    try:
        alerts._clean_emails([bad])
    except alerts.AlertError as exc:
        raised = str(exc)
    check(f"{bad!r} is refused", bool(raised), raised[:50])

check("duplicates collapse, case-insensitively",
      alerts._clean_emails(["A@b.com", "a@B.com"]) == ["A@b.com"])
raised = ""
try:
    alerts._clean_emails([f"a{i}@b.com" for i in range(alerts.MAX_RECIPIENTS + 1)])
except alerts.AlertError as exc:
    raised = str(exc)
check("more than Azure accepts is refused here rather than by ARM", "at most" in raised)

print()
print("=" * 72)
print("the alert Azure is asked for")

b = body(["a@b.com"], hour_of_day=8, years=1)
p = b["properties"]
check("it is an anomaly alert", b["kind"] == "InsightAlert")
check("pointed at Azure's own anomaly view", alerts.ANOMALY_VIEW in p["viewId"],
      "so an alert made here and one made in the portal are the same alert")
check("scoped to the subscription", p["scope"] == "subscriptions/sub-a")
check("daily", p["schedule"]["frequency"] == "Daily")
check("at the hour asked for", p["schedule"]["hourOfDay"] == 8)
check("enabled", p["status"] == "Enabled")

# Written rather than left to Azure's default, so the expiry is a date this app chose and can
# therefore warn about later.
end = datetime.fromisoformat(p["schedule"]["endDate"].replace("Z", "+00:00"))
check("an end date is set explicitly", 360 <= (end - datetime.now(UTC)).days <= 366,
      "the surprise expiry above is the reason")

check("an out-of-range hour is clamped, not sent to ARM",
      body(["a@b.com"], hour_of_day=99)["properties"]["schedule"]["hourOfDay"] == 23)
check("and an over-long term too",
      (datetime.fromisoformat(body(["a@b.com"], years=99)["properties"]["schedule"]["endDate"]
                              .replace("Z", "+00:00")) - datetime.now(UTC)).days < 1100)

print()
print("=" * 72)
print("names, and not creating two of the same alert")

check("the default name is the portal's own", alerts.default_name() == "dailyanomalybyresourcegroup",
      "so this updates that alert rather than shadowing it")

sent: list[tuple[str, str]] = []


async def fake_request(method, url, *, body=None, params=None):
    sent.append((method, url))
    return {"name": url.rsplit("/", 1)[-1], **(body or {})}


real = alerts._request
alerts._request = fake_request
try:
    r = asyncio.run(alerts.create(subscription_id="sub-a", emails=["a@b.com"]))
    check("saving uses PUT so a retry updates rather than duplicates",
          sent and sent[0][0] == "PUT",
          "two alerts arriving every morning is how people learn to filter them")
    check("and reports back what it saved", r["saved"] is True and r["emails"] == ["a@b.com"])
    check("without confusing the write with the alert's own state",
          r["status"] == "Enabled",
          "Azure's status is Enabled/Disabled; whether the save worked is a different question")

    for bad in ("has space", "-starts-with-dash", "x" * 64):
        raised = ""
        try:
            asyncio.run(alerts.create(subscription_id="s", emails=["a@b.com"], name=bad))
        except alerts.AlertError as exc:
            raised = str(exc)
        check(f"name {bad[:14]!r} is refused", bool(raised))

    raised = ""
    try:
        asyncio.run(alerts.create(subscription_id="", emails=["a@b.com"]))
    except alerts.AlertError as exc:
        raised = str(exc)
    check("a missing subscription is refused", "Choose the subscription" in raised)
finally:
    alerts._request = real

print()
print("=" * 72)
print("listing")


async def fake_list(method, url, *, body=None, params=None):
    return {"value": [action(), action(kind="Email"), action(end="2026-07-21T05:59:10.825Z")]}


alerts._request = fake_list
try:
    r = asyncio.run(alerts.listing("sub-a"))
    check("only anomaly alerts are listed", r["count"] == 2,
          "a scheduled report is not an anomaly alert")
    check("expired ones are named at the top level", r["expired"] == ["dailyanomalybyresourcegroup"])
    check("and the note says who actually sends the mail",
          "not by this app" in (r["note"] or ""),
          "so it keeps working whether or not this is running")
finally:
    alerts._request = real

print()
print("=" * 72)
print("this app never sends mail itself")

from pathlib import Path  # noqa: E402

src = (Path(__file__).resolve().parents[1] / "app" / "alerts.py").read_text(encoding="utf-8")
check("no SMTP, no mail client, no credentials",
      not any(t in src for t in ("smtplib", "EmailClient", "sendgrid", "SMTP_")),
      "Azure delivers; there is nothing here to rotate or leak")

print()
print("=" * 72)
print("the estate, not the first subscription in it")

# Two failures in one endpoint, both of which made the panel look broken rather than empty.
# It read `picked[0]`, so an estate of three subscriptions reported one of them and silently
# implied the other two had no alerts. And for a caller entitled to "whatever the server can
# see" -- every local account -- `permitted` returns None, `narrow` returns [], and a perfectly
# valid request was answered with 400.
import os as _os  # noqa: E402

_os.environ.setdefault("AUTH_ALLOW_LOCAL", "true")

from fastapi.testclient import TestClient  # noqa: E402

from app import main as main_mod  # noqa: E402

SUBS = [{"id": "sub-a", "name": "Alpha"}, {"id": "sub-b", "name": "Beta"},
        {"id": "sub-c", "name": "Gamma"}]


async def _subs():
    return {"subscriptions": SUBS}


async def _listing(sub):
    if sub == "sub-c":
        raise alerts.AlertError("Could not read cost alerts: 403")
    if sub == "sub-b":
        return {"alerts": [], "count": 0, "expired": [], "note": "n"}
    return {"alerts": [alerts.summarise(action(), now=NOW)], "count": 1,
            "expired": [], "note": "n"}


import app.cost as cost_mod  # noqa: E402

real_subs, real_listing = cost_mod.list_subscriptions, alerts.listing
cost_mod.list_subscriptions = _subs
alerts.listing = _listing
try:
    with TestClient(main_mod.app) as client:
        r = client.get("/api/anomalies/alerts")
        check("an unrestricted caller who picked nothing is not refused",
              r.status_code == 200, f"{r.status_code} {r.text[:120]}")
        body = r.json() if r.status_code == 200 else {}
        check("the answer covers every subscription in scope, not just the first",
              len(body.get("subscriptions") or []) == 3,
              str([s["id"] for s in body.get("subscriptions") or []]))
        check("each alert names the subscription it lives on",
              (body.get("alerts") or [{}])[0].get("subscription") == "Alpha",
              "a scheduled action is per subscription; turning one off needs to know which")
        check("a subscription that refuses the read is named rather than dropped",
              body.get("unreadable") == ["Gamma"],
              "silently omitting it makes an unreadable subscription look like a covered one")
finally:
    cost_mod.list_subscriptions = real_subs
    alerts.listing = real_listing

print()
print("=" * 72)
print(f"  {passed} passed, {failed} failed")
raise SystemExit(1 if failed else 0)
