"""Email alerts for cost anomalies, sent by Azure rather than by this app.

The same shape as budgets, and for the same reason. A budget carries `contactEmails` and Azure
mails them; nothing here needs an SMTP server, a mailbox, a secret to rotate, or an app that
happens to be running at the moment the anomaly appears. Cost Management's `scheduledActions`
does the identical job for anomalies: `kind: InsightAlert` against Azure's own daily anomaly
view, delivered on a schedule.

The alternative was to send mail from `anomalies.py`, which detects anomalies here with a
median/MAD score over the warehouse. It is a better detector for some questions -- it sees the
subscriptions the reader may see, and it explains its reasoning -- but making it a *notifier*
means credentials, deliverability, unsubscribe handling, and a background job that must outlive
every deploy. Azure already solved that, so this configures Azure.

One thing worth knowing about these alerts, which is why `summarise` exists: Azure gives them an
end date, defaulting to a year out, and says nothing when it passes. The alert on this very
subscription expired on 2026-07-21 and had been quietly sending nothing for six weeks by the
time anyone looked. An alert everybody believes is running is worse than no alert, so an expired
one is reported as expired rather than merely listed.
"""
from __future__ import annotations

import logging
import re
from datetime import UTC, datetime, timedelta
from typing import Any

log = logging.getLogger("cloudlens.alerts")

SCHEDULED_ACTION_API = "2023-11-01"

# Azure's own daily anomaly view. Referenced rather than recreated: it is what the portal's
# anomaly detection uses, so an alert configured here and one configured in the portal are the
# same alert, and the portal can edit ours.
ANOMALY_VIEW = "ms:DailyAnomalyByResourceGroup"

NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,62}$")
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")

# Azure accepts up to twenty recipients on a scheduled action.
MAX_RECIPIENTS = 20
MAX_SUBJECT_LENGTH = 50


class AlertError(Exception):
    """Something the caller should read, not a stack trace."""


def _arm() -> str:
    from . import cost

    return cost.ARM


async def _request(method: str, url: str, *, body: dict | None = None,
                   params: dict | None = None) -> dict:
    from . import cost

    return await cost.azure.request(method, url, json_body=body, params=params, cache=False)


def default_name() -> str:
    """The name Azure's own portal uses, so ours updates that alert instead of shadowing it."""
    return "dailyanomalybyresourcegroup"


def _clean_emails(emails: list[str] | None) -> list[str]:
    out: list[str] = []
    for raw in emails or []:
        addr = str(raw).strip()
        if not addr:
            continue
        if not EMAIL_RE.match(addr):
            raise AlertError(f"{addr!r} is not an email address.")
        if addr.lower() not in {o.lower() for o in out}:
            out.append(addr)
    if not out:
        raise AlertError("Give at least one email address to alert.")
    if len(out) > MAX_RECIPIENTS:
        raise AlertError(f"Azure accepts at most {MAX_RECIPIENTS} recipients on one alert.")
    return out


def build_body(*, subscription_id: str, emails: list[str], display_name: str,
               hour_of_day: int = 8, years: int = 1,
               subject: str | None = None) -> dict[str, Any]:
    """The alert as Azure wants it.

    `endDate` is set explicitly rather than left to Azure. It defaults to a year either way, but
    writing it here means the expiry is a number this app chose and can therefore warn about,
    instead of one that arrives as a surprise.
    """
    if subject is None or subject == "":
        subject = "Cost anomaly detected"
    if not isinstance(subject, str) or not subject.strip():
        raise AlertError("Email subject must contain text.")
    if len(subject) > MAX_SUBJECT_LENGTH:
        raise AlertError(f"Email subject must be at most {MAX_SUBJECT_LENGTH} characters. Shorten it before saving.")
    now = datetime.now(UTC)
    end = now + timedelta(days=365 * max(1, min(int(years), 3)))
    scope = f"subscriptions/{subscription_id}"
    return {
        "kind": "InsightAlert",
        "properties": {
            "displayName": display_name,
            "status": "Enabled",
            "scope": scope,
            "viewId": f"/{scope}/providers/Microsoft.CostManagement/views/{ANOMALY_VIEW}",
            "notification": {
                "to": emails,
                "subject": subject,
                "language": "en",
                "regionalFormat": "en",
                "message": "",
            },
            # Azure carries the first recipient here as well as in `notification.to`, and
            # rejects the action when they disagree.
            "notificationEmail": emails[0],
            "schedule": {
                "frequency": "Daily",
                "hourOfDay": max(0, min(int(hour_of_day), 23)),
                "startDate": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
                "endDate": end.strftime("%Y-%m-%dT%H:%M:%SZ"),
                "dayOfMonth": 0,
                "daysOfWeek": None,
                "weeksOfMonth": None,
            },
        },
    }


def summarise(action: dict[str, Any], now: datetime | None = None) -> dict[str, Any]:
    """One alert, as somebody checking whether they are covered would want it.

    Expiry is computed rather than shown raw. "endDate 2026-07-21" is a date; "expired 42 days
    ago, sending nothing" is the thing that needs doing something about.
    """
    now = now or datetime.now(UTC)
    props = action.get("properties") or {}
    note = props.get("notification") or {}
    sched = props.get("schedule") or {}

    end_raw = sched.get("endDate")
    expires_in = None
    expired = False
    if end_raw:
        try:
            end = datetime.fromisoformat(str(end_raw).replace("Z", "+00:00"))
            expires_in = (end - now).days
            expired = expires_in < 0
        except ValueError:
            log.info("alerts: unparsable endDate %r", end_raw)

    enabled = str(props.get("status") or "").lower() == "enabled"
    return {
        "name": action.get("name"),
        "display_name": props.get("displayName"),
        "kind": action.get("kind"),
        "emails": list(note.get("to") or []),
        "subject": note.get("subject"),
        "frequency": sched.get("frequency"),
        "hour_of_day": sched.get("hourOfDay"),
        "end_date": end_raw,
        "expires_in_days": expires_in,
        "expired": expired,
        "status": props.get("status"),
        # Enabled and expired at once is the state that fools people: the portal shows it as on.
        "sending": bool(enabled and not expired),
        "warning": ("This alert has expired and is no longer sending. Re-save it to extend the "
                    "end date." if enabled and expired else None),
    }


async def listing(subscription_id: str) -> dict[str, Any]:
    """Every anomaly alert on this subscription, and whether it is actually sending."""
    url = (f"{_arm()}/subscriptions/{subscription_id}/providers/"
           "Microsoft.CostManagement/scheduledActions")
    try:
        payload = await _request("GET", url, params={"api-version": SCHEDULED_ACTION_API})
    except Exception as exc:  # noqa: BLE001
        raise AlertError(f"Could not read cost alerts: {str(exc)[:200]}") from exc

    alerts = [summarise(a) for a in payload.get("value", [])
              if str(a.get("kind") or "").lower() == "insightalert"]
    return {
        "alerts": alerts,
        "count": len(alerts),
        "expired": [a["name"] for a in alerts if a["expired"]],
        "note": ("Anomaly alerts are sent by Azure Cost Management, not by this app, so they "
                 "keep working whether or not it is running."),
    }


async def create(*, subscription_id: str, emails: list[str], name: str | None = None,
                 display_name: str = "Daily cost anomaly",
                 hour_of_day: int = 8, years: int = 1,
                 subject: str | None = None) -> dict[str, Any]:
    """Create or update one anomaly alert.

    A PUT, so re-submitting the same name updates rather than duplicating — which is what
    somebody correcting a recipient expects, and what a retry after a timeout must not turn into
    two alerts arriving every morning.
    """
    subscription_id = str(subscription_id or "").strip()
    if not subscription_id:
        raise AlertError("Choose the subscription to watch.")

    name = str(name or default_name()).strip()
    if not NAME_RE.match(name):
        raise AlertError(
            "An alert name starts with a letter or digit and can contain letters, digits, "
            "hyphens, underscores and dots (up to 63 characters).")

    clean = _clean_emails(emails)
    body = build_body(subscription_id=subscription_id, emails=clean,
                      display_name=display_name or "Daily cost anomaly",
                      hour_of_day=hour_of_day, years=years, subject=subject)

    url = (f"{_arm()}/subscriptions/{subscription_id}/providers/"
           f"Microsoft.CostManagement/scheduledActions/{name}")
    try:
        saved = await _request("PUT", url, body=body,
                               params={"api-version": SCHEDULED_ACTION_API})
    except Exception as exc:  # noqa: BLE001
        text = str(exc)
        if "AuthorizationFailed" in text or "403" in text:
            raise AlertError(
                "You do not have permission to create cost alerts on this subscription. "
                "Saving a scheduled action needs Cost Management Contributor."
            ) from exc
        raise AlertError(f"Azure refused the alert: {text[:250]}") from exc

    # Spread first, then our own keys. Azure's `status` is the alert's state — Enabled or
    # Disabled — and this function's is whether the save worked; letting them share a name meant
    # the response reported "Enabled" as the outcome of the write, which the test caught.
    return {**summarise(saved or body | {"name": name}),
            "saved": True, "subscription_id": subscription_id}


async def delete(*, subscription_id: str, name: str) -> dict[str, Any]:
    url = (f"{_arm()}/subscriptions/{subscription_id}/providers/"
           f"Microsoft.CostManagement/scheduledActions/{name}")
    try:
        await _request("DELETE", url, params={"api-version": SCHEDULED_ACTION_API})
    except Exception as exc:  # noqa: BLE001
        raise AlertError(f"Could not delete that alert: {str(exc)[:200]}") from exc
    return {"status": "deleted", "name": name}
