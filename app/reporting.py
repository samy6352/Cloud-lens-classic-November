"""Request-local calendar selection and the warehouse's inclusive reporting bounds."""
from __future__ import annotations

import calendar
import re
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import UTC, date, datetime, timedelta
from typing import Any

_month: ContextVar[str | None] = ContextVar("reporting_month", default=None)
_history: ContextVar[bool] = ContextVar("reporting_history", default=False)

LIVE_NOTE = (
    "Calendar selection filters warehouse charges only. Azure inventory, Advisor, health, "
    "rightsizing and opportunity scans remain live, using their stated recent observation windows."
)


def today() -> date:
    return datetime.now(UTC).date()


def validate_month(value: str | None) -> str | None:
    if value is None:
        return None
    if not re.fullmatch(r"[0-9]{4}-[0-9]{2}", value):
        raise ValueError("period_month must be YYYY-MM.")
    try:
        start = date.fromisoformat(value + "-01")
    except ValueError as exc:
        raise ValueError("period_month must be a valid YYYY-MM.") from exc
    if start.year < 2 or start > today().replace(day=1):
        raise ValueError("period_month must be year 0002 or later and cannot be in the future.")
    return value


def selected_month() -> str | None:
    return _month.get()


@contextmanager
def select(month: str | None) -> Iterator[None]:
    token = _month.set(validate_month(month))
    try:
        yield
    finally:
        _month.reset(token)


@contextmanager
def comparison_history() -> Iterator[None]:
    token = _history.set(True)
    try:
        yield
    finally:
        _history.reset(token)


def month_bounds(month: str | None = None) -> tuple[date, date] | None:
    value = validate_month(month) if month is not None else selected_month()
    if value is None:
        return None
    start = date.fromisoformat(value + "-01")
    end = start.replace(day=calendar.monthrange(start.year, start.month)[1])
    return start, min(end, today())


def period_days(days: int) -> int:
    bounds = month_bounds()
    return (bounds[1] - bounds[0]).days + 1 if bounds else max(1, min(int(days), 365))


def start_sql(hi: Any, days: int) -> str:
    """Exclusive lower edge, shared by > predicates (not >= predicates)."""
    bounds = month_bounds()
    if bounds:
        return f"DATE '{(bounds[0] - timedelta(days=1)).isoformat()}'"
    return f"DATE '{date.fromisoformat(str(hi)[:10]).isoformat()}' - INTERVAL {int(days)} DAY"


def since_date(default: str) -> str:
    bounds = month_bounds()
    return bounds[0].isoformat() if bounds else default


def window(reader: Any, days: int) -> tuple[str, str, str]:
    rows = reader.rows(
        'SELECT max("ChargePeriodStart") AS hi, min("ChargePeriodStart") AS lo FROM costs')
    hi = rows[0]["hi"] if rows and rows[0].get("hi") else None
    if hi is None:
        return "", "", ""
    bounds = month_bounds()
    if bounds:
        hi = bounds[1].isoformat()
        previous = bounds[0] - timedelta(days=period_days(days) + 1)
        return hi, start_sql(hi, days), f"DATE '{previous.isoformat()}'"
    return str(hi), start_sql(hi, days), start_sql(hi, int(days) * 2)


def view_predicate() -> str:
    bounds = month_bounds()
    if not bounds:
        return ""
    start, end = bounds
    if _history.get():
        start -= timedelta(days=(end - start).days + 1)
    return (f'"ChargePeriodStart" >= DATE \'{start.isoformat()}\' AND '
            f'"ChargePeriodStart" <= DATE \'{end.isoformat()}\'')


def metadata(warehouse: Any, scope: list[str] | None, days: int = 30,
             filters: dict | None = None) -> dict[str, Any]:
    days = max(1, min(int(days), 365))
    month = selected_month()
    # Discover availability before applying the month, but never outside the authorized scope.
    with select(None), warehouse.reader(scope, filters=filters) as reader:
        rows = reader.rows(
            'SELECT max("ChargePeriodStart") AS hi, min("ChargePeriodStart") AS lo FROM costs')
        latest = str(rows[0]["hi"])[:10] if rows and rows[0].get("hi") else None
        months = reader.rows(
            'SELECT DISTINCT substr(CAST("ChargePeriodStart" AS VARCHAR), 1, 7) AS month '
            'FROM costs WHERE "ChargePeriodStart" IS NOT NULL ORDER BY month DESC', 12000)
    bounds = month_bounds(month) if month else None
    if bounds:
        start, end = bounds
    elif latest:
        end = date.fromisoformat(latest)
        start = end - timedelta(days=days - 1)
    else:
        start = end = None
    available = [r["month"] for r in months
                 if "0002-01" <= r["month"] <= today().strftime("%Y-%m")]
    return {
        "from": start.isoformat() if start else None,
        "to": end.isoformat() if end else None,
        "month": month,
        "days": (end - start).days + 1 if start and end else days,
        "mode": "calendar_month" if month else "rolling",
        "anchor": "calendar" if month else "latest_scoped_data",
        "as_of": latest,
        "available_months": available,
        "partial": bool(month and month == today().strftime("%Y-%m")),
        "empty": month not in available if month else latest is None,
        "timezone": "UTC", "bounds": "inclusive", "live_note": LIVE_NOTE,
    }
