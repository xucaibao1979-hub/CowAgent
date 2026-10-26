"""Unified read model for Intelligence Center and generic scheduler tasks."""

import json
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import re


CATALOG_SCHEMA_VERSION = 1


def normalize_scheduler_task(task):
    action = task.get("action") or {}
    schedule = task.get("schedule") or {}
    return {
        "uid": f"scheduler:{task.get('id')}", "id": str(task.get("id") or ""), "origin": "scheduler",
        "name": str(task.get("name") or "未命名定时任务"), "kind": "scheduled_agent",
        "enabled": bool(task.get("enabled", True)), "created_at": task.get("created_at", ""),
        "updated_at": task.get("updated_at", ""), "last_run_at": task.get("last_run_at", ""),
        "last_run_status": task.get("last_run_status", ""), "last_result_summary": task.get("last_result_summary", ""),
        "last_artifact_path": task.get("last_artifact_path", ""), "last_error": task.get("last_error", ""),
        "next_run_at": task.get("next_run_at", ""), "status": "enabled" if task.get("enabled", True) else "disabled",
        "definition": {
            "trigger": deepcopy(schedule),
            "sources": [],
            "conditions": {"rules": [], "match": "all"},
            "processor": {"instruction": action.get("task_description") or action.get("content") or "", "operations": [action.get("type") or "agent_task"]},
            "approval": {"policy": "risk_based", "required_levels": [2, 3]},
            "actions": [{"type": "notify", "enabled": True, "target": {"channel": action.get("channel_type", "web"), "receiver": action.get("receiver_name") or action.get("receiver", "")}}],
        },
    }


def normalize_automation_task(task):
    item = deepcopy(task)
    item.update({"uid": f"automation:{task.get('id')}", "origin": "automation",
                 "status": "enabled" if task.get("enabled", True) else "disabled",
                 "next_run_at": task.get("next_run_at", "") or _estimate_next_run(task)})
    return item


def _latest_recovery_by_task(root):
    try:
        from intelligence_center.sqlite_state import SQLiteStateStore
        legacy = root / "automation_runs.json"
        data = SQLiteStateStore(root).read("automation_runs", {"version": 1, "runs": []}, legacy)
    except Exception:
        try:
            legacy = root / "automation_runs.json"
            data = json.loads(legacy.read_text(encoding="utf-8")) if legacy.exists() else {}
        except Exception:
            data = {}
    output = {}
    for run in sorted(data.get("runs") or [], key=lambda x: str(x.get("started_at") or ""), reverse=True):
        task_id = str(run.get("task_id") or "")
        if not task_id or task_id in output:
            continue
        actions = [str(x) for x in (run.get("recovery_actions") or []) if str(x).strip()]
        fallback = bool(run.get("web_fallback_notification"))
        if fallback and not any("Web" in x for x in actions):
            actions.append("通知失败：已自动补发到 Web")
        output[task_id] = {
            "last_run_id": run.get("id") or "",
            "last_recovery_at": run.get("finished_at") or run.get("started_at") or "",
            "last_recovery_status": run.get("status") or "",
            "last_recovery_actions": actions[:6],
            "last_recovery_summary": "；".join(actions[:3]),
        }
    return output


def _parse_datetime(value):
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=datetime.now().astimezone().tzinfo)
    except Exception:
        return None


def _estimate_next_run(task):
    """Best-effort next run time for legacy automation task files."""
    if not task.get("enabled", True):
        return ""
    now = datetime.now().astimezone()
    trigger = (task.get("definition") or {}).get("trigger") or {}
    mode = str(task.get("schedule_type") or trigger.get("type") or "daily")
    if mode == "interval":
        minutes = int(task.get("interval_minutes") or trigger.get("interval_minutes") or 0)
        if minutes <= 0:
            return ""
        last = _parse_datetime(task.get("last_run_at"))
        return ((last.astimezone(now.tzinfo) if last else now) + timedelta(minutes=minutes)).isoformat(timespec="seconds")
    match = re.fullmatch(r"每天\s*(\d{1,2}):(\d{2})", str(task.get("schedule") or trigger.get("schedule") or ""))
    if not match:
        return ""
    hour, minute = int(match.group(1)), int(match.group(2))
    candidate = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if candidate <= now:
        candidate += timedelta(days=1)
    return candidate.isoformat(timespec="seconds")


class UnifiedTaskCatalog:
    def __init__(self, automation_service, scheduler_store):
        self.automation_service = automation_service
        self.scheduler_store = scheduler_store

    def list(self, query="", origin="", enabled=None):
        automation = self.automation_service.list_tasks().get("tasks", [])
        scheduled = self.scheduler_store.list_tasks() if self.scheduler_store else []
        items = [normalize_automation_task(x) for x in automation] + [normalize_scheduler_task(x) for x in scheduled]
        recovery = _latest_recovery_by_task(self.automation_service.root)
        for item in items:
            item.update(recovery.get(str(item.get("id") or ""), {}))
        needle = str(query or "").strip().lower()
        if needle:
            items = [x for x in items if needle in (x.get("name", "") + " " + x.get("kind", "")).lower()]
        if origin:
            items = [x for x in items if x.get("origin") == origin]
        if enabled is not None:
            items = [x for x in items if x.get("enabled") is bool(enabled)]
        items.sort(key=lambda x: x.get("created_at") or x.get("updated_at") or "", reverse=True)
        return {"version": CATALOG_SCHEMA_VERSION, "updated_at": datetime.now(timezone.utc).isoformat(),
                "total": len(items), "tasks": items,
                "summary": {"automation": sum(x["origin"] == "automation" for x in items),
                            "scheduler": sum(x["origin"] == "scheduler" for x in items),
                            "enabled": sum(bool(x.get("enabled")) for x in items)}}
