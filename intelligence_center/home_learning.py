"""Shared learning signals from the home recommendation cards.

This module keeps the recommendation-learning data small, local and reusable.
It is preference evidence only: it can influence ranking, prompts and routing
tips, but it must never grant permissions or bypass approvals.
"""
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path


TARGET_LABELS = {
    "failed_runs": "失败任务恢复",
    "maintenance": "维护检查",
    "channels": "通知通道配置",
    "agent-reach": "Agent-Reach 互联网来源",
    "profile_briefing": "按用户画像生成简报",
    "reuse_last_success": "复用最近成功任务",
    "morning_briefing": "早间简报",
    "network-search": "网络搜索 Agent",
    "templates": "自动化模板",
}


TARGET_HINTS = {
    "failed_runs": {"keywords": ["失败", "异常", "重试", "无内容"], "tools": ["scheduler", "logs"], "intent": "automation"},
    "maintenance": {"keywords": ["维护", "健康检查", "缓存", "日志"], "tools": ["scheduler", "logs"], "intent": "automation"},
    "channels": {"keywords": ["通知", "发送", "微信", "webhook", "邮箱"], "tools": ["send"], "intent": "automation"},
    "agent-reach": {"keywords": ["rss", "github", "视频", "v2ex", "社媒"], "tools": ["web_search", "web_fetch"], "intent": "search"},
    "profile_briefing": {"keywords": ["简报", "日报", "关注", "画像"], "tools": ["web_search", "scheduler"], "intent": "automation"},
    "reuse_last_success": {"keywords": ["复用", "再执行", "同类任务"], "tools": ["scheduler"], "intent": "automation"},
    "morning_briefing": {"keywords": ["早报", "早间简报", "新闻"], "tools": ["web_search", "scheduler"], "intent": "automation"},
    "network-search": {"keywords": ["搜索", "查询", "新闻", "资料"], "tools": ["web_search", "web_fetch"], "intent": "search"},
    "templates": {"keywords": ["模板", "复用", "常用任务"], "tools": ["scheduler"], "intent": "automation"},
}


KIND_TARGETS = {
    "news": ["network-search", "morning_briefing", "profile_briefing"],
    "briefing": ["morning_briefing", "profile_briefing"],
    "github": ["network-search", "agent-reach"],
    "website": ["network-search", "agent-reach"],
    "forex": ["network-search"],
    "maintenance": ["maintenance"],
    "ops": ["maintenance"],
    "email": ["channels"],
    "social": ["network-search", "agent-reach"],
    "product": ["profile_briefing"],
    "research": ["profile_briefing"],
    "code": ["templates"],
}


def read_home_learning(root) -> dict:
    path = Path(root) / "home_suggestion_preferences.json"
    if not path.exists():
        return {"pinned": [], "hidden": {}, "stats": {}, "top": [], "summary": ""}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        data = {}
    stats = data.get("stats") if isinstance(data.get("stats"), dict) else {}
    pinned = [str(x) for x in (data.get("pinned") or []) if str(x).strip()]
    hidden = data.get("hidden") if isinstance(data.get("hidden"), dict) else {}
    top = []
    for target, item in stats.items():
        score = learned_score(item) + (8 if target in pinned else 0) - (6 if target in hidden else 0)
        if score > 0:
            top.append({
                "target": target,
                "label": TARGET_LABELS.get(target, target),
                "score": score,
                "opens": int(item.get("opens") or 0),
                "pins": int(item.get("pins") or 0),
                "dismisses": int(item.get("dismisses") or 0),
                "dislikes": int(item.get("dislikes") or 0),
                "last_action": item.get("last_action") or "",
                "last_at": item.get("last_at") or "",
                "hint": TARGET_HINTS.get(target, {}),
            })
    top.sort(key=lambda x: (-x["score"], x["label"]))
    summary = "；".join(f"{item['label']} 使用偏好 {item['score']}" for item in top[:3])
    return {"pinned": pinned, "hidden": hidden, "stats": stats, "top": top[:6], "summary": summary}


def write_home_learning(root, prefs: dict) -> dict:
    root_path = Path(root)
    root_path.mkdir(parents=True, exist_ok=True)
    path = root_path / "home_suggestion_preferences.json"
    clean = {
        "version": int(prefs.get("version") or 1),
        "updated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "pinned": [str(x) for x in (prefs.get("pinned") or []) if str(x).strip()],
        "hidden": prefs.get("hidden") if isinstance(prefs.get("hidden"), dict) else {},
        "stats": prefs.get("stats") if isinstance(prefs.get("stats"), dict) else {},
    }
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(clean, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)
    return clean


def record_event(root, target: str, action: str) -> dict:
    prefs = _raw_preferences(root)
    stats = prefs.setdefault("stats", {})
    target = str(target or "").strip()
    action = str(action or "").strip()
    if not target or not action:
        return prefs
    _record_stats_event(stats, target, action)
    return write_home_learning(root, prefs)


def observe_execution_result(root, *, kind: str = "", status: str = "", task_name: str = "", notification_failed: bool = False) -> dict:
    """Feed actual task outcomes back into home recommendation learning."""
    prefs = _raw_preferences(root)
    stats = prefs.setdefault("stats", {})
    targets = _targets_for_task(kind, task_name)
    if not targets:
        return prefs
    outcome = _normalize_status(status, notification_failed)
    for target in targets:
        item = stats.setdefault(target, {})
        item[outcome] = int(item.get(outcome) or 0) + 1
        item["last_outcome"] = outcome
        item["last_task_kind"] = str(kind or "")
        item["last_task_name"] = str(task_name or "")[:120]
        item["last_at"] = datetime.now().astimezone().isoformat(timespec="seconds")
    return write_home_learning(root, prefs)


def learned_score(stats) -> int:
    return (
        int(stats.get("opens") or 0) * 3
        + int(stats.get("pins") or 0) * 5
        + int(stats.get("outcome_success") or 0) * 6
        + int(stats.get("outcome_notification_warning") or 0) * 1
        - int(stats.get("outcome_empty") or 0) * 3
        - int(stats.get("outcome_failed") or 0) * 5
        - int(stats.get("dismisses") or 0) * 2
        - int(stats.get("dislikes") or 0) * 4
    )


def learning_prompt_context(root) -> str:
    data = read_home_learning(root)
    if not data.get("top"):
        return ""
    lines = ["【首页推荐学习偏好】"]
    for item in data["top"][:4]:
        hint = item.get("hint") or {}
        keywords = "、".join(hint.get("keywords") or [])
        tools = "、".join(hint.get("tools") or [])
        lines.append(f"- {item['label']}：学习分 {item['score']}；常用关键词：{keywords or '无'}；优先工具：{tools or '按任务判断'}")
    lines.append("使用要求：这些只是偏好信号，不代表授权；涉及发送、删除、写入、安装、登录仍需遵循审批策略。")
    return "\n".join(lines)


def _raw_preferences(root) -> dict:
    path = Path(root) / "home_suggestion_preferences.json"
    if path.exists():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                data.setdefault("version", 1)
                data.setdefault("pinned", [])
                data.setdefault("hidden", {})
                data.setdefault("stats", {})
                return data
        except (OSError, ValueError):
            pass
    return {"version": 1, "updated_at": "", "pinned": [], "hidden": {}, "stats": {}}


def _record_stats_event(stats: dict, target: str, action: str) -> dict:
    now = datetime.now().astimezone().isoformat(timespec="seconds")
    item = dict(stats.get(target) or {})
    key_map = {"open": "opens", "pin": "pins", "unpin": "unpins", "dismiss": "dismisses", "dislike": "dislikes"}
    key = key_map.get(action, action)
    item[key] = int(item.get(key) or 0) + 1
    item["last_action"] = action
    item["last_at"] = now
    stats[target] = item
    return item


def _targets_for_task(kind: str, task_name: str = "") -> list[str]:
    kind = str(kind or "").strip()
    targets = list(KIND_TARGETS.get(kind) or [])
    name = str(task_name or "").lower()
    if any(token in name for token in ("搜索", "新闻", "资讯", "政策", "热点", "rss", "github")):
        targets.extend(["network-search"])
    if any(token in name for token in ("早报", "简报", "日报")):
        targets.extend(["morning_briefing", "profile_briefing"])
    if any(token in name for token in ("维护", "健康", "失败", "日志", "缓存")):
        targets.extend(["maintenance"])
    if any(token in name for token in ("通知", "微信", "webhook", "邮箱")):
        targets.extend(["channels"])
    return list(dict.fromkeys(targets))


def _normalize_status(status: str, notification_failed: bool = False) -> str:
    status = str(status or "").lower()
    if notification_failed:
        return "outcome_notification_warning"
    if status in {"success", "completed", "ok"}:
        return "outcome_success"
    if status in {"empty", "no_content"}:
        return "outcome_empty"
    return "outcome_failed"
