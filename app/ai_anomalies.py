"""Conservative AI billing/usage signals from complete observed daily evidence only."""
from __future__ import annotations

import math
from datetime import date, timedelta

from .anomalies import (
    MIN_DELTA,
    MIN_HISTORY,
    MIN_KIND,
    SCORE_CAP,
    SETTLE_DAYS,
    THRESHOLD,
    WINDOW,
    score,
)

MIN_RELATIVE_CHANGE = 0.5
MIN_TOKENS = 1000.0
MIN_UTILIZATION = 20.0
MAX_FLAGS = 20
MAX_DAILY_GROUPS = 40_000
MATERIALITY = {"cost": MIN_DELTA, "tokens": MIN_TOKENS, "utilization": MIN_UTILIZATION}


def _finite(value: object) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def evaluate(metric: str, daily: dict[str, float | None], window: dict, today: date, *,
             source: str, currency: str | None = None, unavailable: str = "",
             not_applicable: bool = False, excluded_negative_days: int = 0) -> dict:
    """Judge selected dates against strictly earlier observations in a calendar fortnight."""
    if metric not in MATERIALITY:
        raise ValueError("Unsupported AI anomaly metric.")
    result = {
        "flags": [], "status": "unavailable", "note": unavailable,
        "coverage": {"metric": metric, "currency": currency, "eligible_days": 0,
                     "observed_days": 0, "checked_days": 0, "missing_days": 0,
                     "insufficient_history_days": 0, "negative_days": excluded_negative_days,
                     "excluded_tail_days": 0},
    }
    if not_applicable:
        result.update(status="not_applicable", note="This metric does not apply to the resource or current deployment.")
        return result
    if unavailable:
        return result
    if not window.get("from") or not window.get("to"):
        result["note"] = "No selected billing period is available for anomaly checks."
        return result
    first = date.fromisoformat(window["from"])
    last = date.fromisoformat(window["to"])
    cutoff = (min(last, today) - timedelta(days=SETTLE_DAYS) if metric == "cost"
              else min(last, today - timedelta(days=1)))
    coverage = result["coverage"]
    coverage["excluded_tail_days"] = max(0, (last - max(cutoff, first - timedelta(days=1))).days)
    if cutoff < first:
        result["note"] = "No settled/full selected days are eligible for anomaly checks."
        return result
    coverage["eligible_days"] = (cutoff - first).days + 1
    known: dict[date, float] = {}
    for raw_day, value in daily.items():
        try:
            day = date.fromisoformat(raw_day)
        except (ValueError, TypeError):
            continue
        if not first - timedelta(days=WINDOW) <= day <= cutoff or not _finite(value):
            continue
        if value < 0:
            coverage["negative_days"] += 1
            continue
        known[day] = float(value)
    for day in sorted(d for d in known if first <= d <= cutoff):
        coverage["observed_days"] += 1
        past = sorted(d for d in known if day - timedelta(days=WINDOW) <= d < day)
        if len(past) < MIN_HISTORY:
            coverage["insufficient_history_days"] += 1
            continue
        # Seven total prior days remain mandatory, even for the four-observation
        # weekend comparator that prevents an established weekly cycle becoming an alert.
        same_kind = [d for d in past if (d.weekday() >= 5) == (day.weekday() >= 5)]
        comparable = same_kind if len(same_kind) >= MIN_KIND else past
        minimum = MIN_KIND if comparable is same_kind else MIN_HISTORY
        actual = known[day]
        z, baseline, spread = score(actual, [known[d] for d in comparable], min_samples=minimum)
        delta = actual - baseline
        coverage["checked_days"] += 1
        if not all(math.isfinite(v) for v in (z, baseline, spread, delta)):
            coverage["checked_days"] -= 1
            continue
        # Existing score() uses a strict >50% fallback when MAD=0; our documented
        # inclusive floor also admits exactly 50%, including genuine zero observations.
        relative = abs(delta) / baseline if baseline > 0 else None
        if spread == 0:
            z = 10.0 if delta != 0 and (relative is None or relative >= MIN_RELATIVE_CHANGE) else 0.0
        if (abs(z) < THRESHOLD or abs(delta) < MATERIALITY[metric]
                or (relative is not None and relative < MIN_RELATIVE_CHANGE)):
            continue
        unit = (currency or "unknown") if metric == "cost" else "tokens" if metric == "tokens" else "percentage points"
        note = (
            f"Observed daily {metric}; compared with {len(comparable)} complete "
            f"{'same-kind weekday/weekend' if comparable is same_kind else 'prior'} observations "
            f"within the preceding {WINDOW} calendar days ({len(past)} total prior days). "
            "Missing days were not filled with zero."
        )
        if metric == "cost":
            note += " Credit/negative-charge days and the latest two reporting calendar days are excluded."
        elif metric == "tokens":
            note += " Each daily token value requires 24 valid hourly samples with a consistent metric basis."
        else:
            note += " Daily utilization is the unweighted mean of 24 hourly averages; delta is percentage points."
        percent = delta / baseline * 100 if baseline > 0 else None
        result["flags"].append({
            "metric": metric, "direction": "up" if delta > 0 else "down", "day": day.isoformat(),
            "actual": actual, "baseline": baseline, "delta": delta,
            "percent": round(percent, 1) if percent is not None and math.isfinite(percent) else None,
            "unit": unit, **({"currency": currency or "unknown"} if metric == "cost" else {}),
            "severity": "high" if abs(z) >= 10 and abs(delta) >= MATERIALITY[metric] * 5 else "medium",
            "score": min(SCORE_CAP, abs(z)), "baseline_from": comparable[0].isoformat(),
            "baseline_to": comparable[-1].isoformat(), "baseline_samples": len(comparable),
            "history_samples": len(past), "source": source, "note": note,
        })
    coverage["missing_days"] = coverage["eligible_days"] - coverage["observed_days"]
    if coverage["checked_days"]:
        complete = (coverage["checked_days"] == coverage["eligible_days"]
                    and not coverage["negative_days"])
        result["status"] = "checked" if complete else "partial"
        result["note"] = (
            f"Checked {coverage['checked_days']}/{coverage['eligible_days']} eligible {metric} days; "
            "missing/incomplete days and insufficient baselines were not judged.")
    elif coverage["observed_days"]:
        result.update(status="insufficient_history",
                      note=f"Fewer than {MIN_HISTORY} comparable complete prior days within the preceding {WINDOW} calendar days.")
    else:
        result["note"] = f"No complete observed {metric} days are available; absence is not a measured drop."
    if coverage["negative_days"]:
        result["note"] += " Credit/negative days were excluded, never divided by a negative baseline."
    return result


def attach(row: dict, checks: list[dict]) -> None:
    flags = [flag for check in checks for flag in check["flags"]]
    flags.sort(key=lambda flag: (flag["day"], flag["severity"] == "high", flag["score"]), reverse=True)
    row["anomalies"] = flags[:MAX_FLAGS]
    row["anomaly_checks"] = [
        {"status": check["status"], "note": check["note"], **check["coverage"]} for check in checks
    ]
    active = [check for check in checks if check["status"] != "not_applicable"]
    states = {check["status"] for check in active}
    checked = sum(check["coverage"]["checked_days"] for check in active)
    if not active:
        status = "not_applicable"
    elif states == {"checked"}:
        status = "checked"
    elif checked:
        status = "partial"
    elif "insufficient_history" in states:
        status = "insufficient_history"
    else:
        status = "unavailable"
    row["anomaly_status"] = "partial" if len(flags) > MAX_FLAGS else status
    row["anomaly_note"] = (
        f"Coverage: {row['anomaly_status'].replace('_', ' ')}. "
        f"{len(flags)} significant daily changes; {checked} metric-days checked. "
        "No alert does not establish normal usage where history or telemetry is incomplete."
    )
    if len(flags) > MAX_FLAGS:
        row["anomaly_note"] += f" Showing the newest {MAX_FLAGS} flags; additional flags are omitted."
    if flags:
        flag = flags[0]
        row["recommendation"] = (
            f"Investigate the {flag['metric']} {'increase' if flag['direction'] == 'up' else 'decrease'} "
            f"on {flag['day']} ({flag['delta']:+,.0f} {flag['unit']} versus baseline); "
            "verify workload and source data before making changes.")


def metadata(rows: list[dict], window: dict, metric_history: dict | None) -> dict:
    checks = [check for row in rows for check in row["anomaly_checks"]]
    billing_history = ({
        "from": (date.fromisoformat(window["from"]) - timedelta(days=WINDOW)).isoformat(),
        "to": window["to"], "timezone": "UTC",
    } if window.get("from") else None)
    return {
        "method": "Rolling median / median absolute deviation (MAD), two-sided modified z-score",
        "window_days": WINDOW, "min_history": MIN_HISTORY, "min_same_kind_history": MIN_KIND,
        "min_relative_change": MIN_RELATIVE_CHANGE, "threshold": THRESHOLD,
        "materiality": {"cost": {"delta": MIN_DELTA, "unit": "native/display billing currency per day"},
                        "tokens": {"delta": MIN_TOKENS, "unit": "tokens per day"},
                        "utilization": {"delta": MIN_UTILIZATION, "unit": "percentage points"}},
        "selected_window": window, "billing_history_window": billing_history,
        "metric_history_window": metric_history,
        "settle_days": SETTLE_DAYS,
        "settlement_anchor": "Two latest calendar dates ending at the selected reporting end (or today if earlier)",
        "max_flags_per_row": MAX_FLAGS, "max_daily_cost_groups": MAX_DAILY_GROUPS,
        "coverage": {
            "rows": len(rows), "rows_with_flags": sum(bool(row["anomalies"]) for row in rows),
            "displayed_flags": sum(len(row["anomalies"]) for row in rows),
            "checked_metric_days": sum(check["checked_days"] for check in checks),
            "status_counts": {state: sum(row["anomaly_status"] == state for row in rows)
                              for state in ("checked", "partial", "insufficient_history", "unavailable", "not_applicable")},
        },
        "note": (
            "Rules, not LLM-generated predictions. At least seven complete observed prior days are required "
            "within a 14-calendar-day window; four same-kind weekday/weekend samples may then be compared. "
            "A flag must satisfy score, relative change and absolute materiality floors. "
            "Zero baselines use the absolute floor and have no percentage change. "
            "No absent days or missing hourly samples are replaced with zero. "
            "Billing completeness means all recorded amounts are present, not independently verified ingestion completeness. "
            "Account and deployment signals overlap; do not count them as independent incidents. "
            "Deployment/model metadata is current, not a historical version assertion."),
    }
