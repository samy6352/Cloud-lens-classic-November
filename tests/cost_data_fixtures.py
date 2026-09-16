"""Synthetic authoritative receipts for backend-only scheduling tests; never call Azure."""
import json
from datetime import date, timedelta


def receipt_days(sub: str, start: str, end: str) -> None:
    from app.warehouse import warehouse
    lo, hi = date.fromisoformat(start), date.fromisoformat(end)
    for i in range((hi - lo).days + 1):
        warehouse.set_meta(f"focus-day:{sub}:{lo + timedelta(days=i)}",
                           json.dumps({"run_id": "offline-validated-fixture"}))


def finish_windows(job_id: str) -> None:
    from app import jobs
    record = jobs.get(job_id)
    for task in record["tasks"]:
        if task.get("from"):
            receipt_days(task["subscription"], task["from"], task["to"])
        jobs.update(job_id, task_id=task["task_id"], status="ready")
    jobs.update(job_id, status="ready")


def update_plan(sub: str, start: str, end: str, schedule: dict | None = None) -> dict:
    from app import focus_workflow
    return {"purpose": "update", "from": start, "to": end,
            "subscriptions": [{"subscription": sub, "export": schedule or {"name": "owned"},
                               "missing_capabilities": [],
                               "tasks": focus_workflow.chunks(start, end)}]}
