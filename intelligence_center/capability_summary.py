"""User-facing summaries: configuration is not proof of execution or delivery."""
from datetime import datetime


def is_enabled(value):
    return value is True or value == 1 or str(value).lower() in {"true", "enabled", "yes"}


def automation_summary(tasks, records, pending, today=None):
    today = today or datetime.now().astimezone().date().isoformat()
    enabled = sum(is_enabled(item.get("enabled")) for item in tasks)
    failed = sum(str(item.get("last_status", "")).lower() in {
        "failed", "error", "失败", "异常", "错误"} for item in tasks)
    recent_today = sum(str(item.get("date") or item.get("started_at") or "").startswith(today)
                       for item in records)
    detail = (f"共 {len(tasks)} 个任务，{enabled} 个已启用；最近 {len(records)} 条运行记录中，"
              f"今天 {recent_today} 条；待审批 {pending} 项。运行记录不等于有效产物或通知送达。")
    return {"status": "warning" if failed or not enabled else "success",
            "label": f"{failed} 项异常" if failed else f"{enabled} 个已启用",
            "summary": detail}


def artifact_summary(items, today=None):
    today = today or datetime.now().astimezone().date().isoformat()
    count = 0
    for item in items:
        value = str(item.get("created_at") or item.get("date") or "")
        try:
            day = datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone().date().isoformat()
        except ValueError:
            day = value[:10]
        count += day == today
    return {"status": "success", "label": f"{len(items)} 条记录",
            "summary": f"全部产物共 {len(items)} 条，今天新增 {count} 条（本机时区）；与产物/交付物中心使用同一数据源。"}
