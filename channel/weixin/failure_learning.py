"""WeChat failure-learning helpers.

The helpers are intentionally tiny and safe: they store only category,
sanitized command snippets and failure summaries. They never store credentials
or message attachments.
"""
from __future__ import annotations

from common.log import logger
from config import conf, get_data_root


def record_weixin_failure(command: str = "", category: str = "", message: str = "", route: str = "") -> None:
    try:
        from intelligence_center.agent2_service import Agent2Service
        root = f"{get_data_root()}/intelligence_center"
        Agent2Service(root, get_data_root(), conf()).observe_weixin_failure({
            "command": command,
            "category": category,
            "message": message,
            "route": route,
        })
    except Exception as error:
        logger.warning(f"[WeixinLearning] record failure skipped: {error}")


def record_weixin_success(
    command: str = "",
    task_type: str = "",
    action: str = "",
    route: str = "",
    evidence: str = "",
) -> None:
    try:
        from intelligence_center.agent2_service import Agent2Service
        root = f"{get_data_root()}/intelligence_center"
        Agent2Service(root, get_data_root(), conf()).observe_weixin_success({
            "command": command,
            "task_type": task_type,
            "action": action,
            "route": route,
            "evidence": evidence,
        })
    except Exception as error:
        logger.warning(f"[WeixinLearning] record success skipped: {error}")


def learned_weixin_failures(limit: int = 6) -> list:
    try:
        from intelligence_center.agent2_service import Agent2Service
        root = f"{get_data_root()}/intelligence_center"
        return Agent2Service(root, get_data_root(), conf()).weixin_failure_learning(limit)
    except Exception as error:
        logger.warning(f"[WeixinLearning] read learning skipped: {error}")
        return []


def learned_weixin_successes(limit: int = 6) -> list:
    try:
        from intelligence_center.agent2_service import Agent2Service
        root = f"{get_data_root()}/intelligence_center"
        return Agent2Service(root, get_data_root(), conf()).weixin_success_learning(limit)
    except Exception as error:
        logger.warning(f"[WeixinLearning] read success learning skipped: {error}")
        return []


def learned_weixin_route_strategy(command: str = "") -> dict:
    """Return safe routing hints learned from recent WeChat failures.

    This does not grant permissions or bypass approvals. It only changes the
    order of safe attempts: deterministic tools first, then fallback to model.
    """
    rows = learned_weixin_failures(20)
    success_rows = learned_weixin_successes(20)
    counts = {}
    for row in rows:
        category = str(row.get("category") or "general")
        counts[category] = counts.get(category, 0) + int(row.get("count") or 0)
    success_counts = {}
    for row in success_rows:
        key = f"{row.get('task_type') or 'general'}:{row.get('action') or 'success'}"
        success_counts[key] = success_counts.get(key, 0) + int(row.get("count") or 0)
    command_text = str(command or "")
    search_recovery = any(term in command_text for term in ("新闻", "热点", "资讯", "政策", "财税", "搜索", "查询"))
    file_recovery = any(term in command_text for term in ("文件", "文档", "图片", "照片", "桌面", "下载"))
    email_recovery = any(term in command_text for term in ("邮件", "邮箱", "未读", "新邮件"))
    route_notes = []
    if any(value > 0 for key, value in success_counts.items() if key.startswith(("email:", "news:", "file:"))):
        route_notes.append("已有微信成功路径，优先走确定性工具")
    if counts.get("model_auth", 0) > 0 or counts.get("empty_reply", 0) >= 2:
        route_notes.append("模型/空回复曾失败，避免先走模型兜底")
    if search_recovery and (counts.get("empty_result", 0) > 0 or success_counts.get("news:expanded_7d", 0) > 0):
        route_notes.append("搜索类任务优先启用空结果兜底")
    if file_recovery and (counts.get("file_not_found", 0) > 0 or success_counts.get("file:refresh_then_search", 0) > 0):
        route_notes.append("找文件失败较多时先刷新/使用索引")
    if email_recovery and counts.get("email_config", 0) > 0:
        route_notes.append("查邮件前先检查邮箱配置")
    return {
        "counts": counts,
        "success_counts": success_counts,
        "route_notes": route_notes[:5],
        "model_unreliable": counts.get("model_auth", 0) > 0 or counts.get("empty_reply", 0) >= 2,
        "prefer_task_first": (
            counts.get("model_auth", 0) > 0
            or counts.get("worker_failure", 0) >= 2
            or any(value > 0 for key, value in success_counts.items() if key.startswith(("email:", "news:", "file:")))
        ),
        "email_precheck": email_recovery and counts.get("email_config", 0) > 0,
        "search_recovery": search_recovery and (
            counts.get("empty_result", 0) > 0
            or counts.get("network", 0) > 0
            or success_counts.get("news:expanded_7d", 0) > 0
        ),
        "prefer_expanded_news_window": search_recovery and success_counts.get("news:expanded_7d", 0) > 0,
        "file_refresh_before_miss": file_recovery and (
            counts.get("file_not_found", 0) > 0
            or success_counts.get("file:refresh_then_search", 0) > 0
        ),
        "permission_guard": counts.get("permission", 0) > 0,
        "send_web_fallback": counts.get("send_failure", 0) > 0,
    }
