"""Deliver approval-request notifications through user-selected channels."""
import json
from pathlib import Path

from common.log import logger


def notify_approval(data_dir, item):
    state_path=Path(data_dir)/"agent2_state.json"
    try:data=json.loads(state_path.read_text(encoding="utf-8"))
    except (OSError,ValueError):return []
    targets=(data.get("settings") or {}).get("approval_notifications") or []
    if not targets:return []
    from plugins.intelligence_center.notifications import NotificationDispatcher
    action=item.get("action") or "external_action";preview=item.get("preview") or {}
    title=preview.get("subject") or preview.get("title") or action
    content=(preview.get("body") or preview.get("content") or "")
    report=(f"[CowAgent 授权请求]\n操作：{title}\n风险等级：{item.get('risk_level',2)}\n"
            f"摘要：{str(content)[:500]}\n审批编号：{item.get('id')}\n\n"
            "请打开 CowAgent → AI · 自动化 → 审批，选择批准或拒绝。未批准前系统不会执行该操作。")
    task={"id":item.get("task_id") or item.get("id"),"name":"授权请求","notifications":targets}
    try:return NotificationDispatcher().dispatch(task,report)
    except Exception as error:
        logger.warning("[Approval] notification dispatch failed: %s",error);return [{"success":False,"message":str(error)}]
