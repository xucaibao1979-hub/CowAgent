"""Bridge WeChat instructions into Mission Control routing.

This module keeps the WeChat side small: it only handles safe management
commands that can be answered or retried deterministically. Ordinary user
requests still go through the normal Agent pipeline.
"""
from __future__ import annotations

from typing import Optional

from common.log import logger


CONTROL_KEYWORDS = (
    "总控台", "任务状态", "运行状态", "失败任务", "无内容", "自动重试", "重新执行",
    "按推荐参数", "待审批", "审批请求", "授权请求", "任务结果", "最近产物",
    "维护看板", "健康检查", "通知通道", "通道配置",
    "微信慢", "微信太慢", "微信没反应", "微信无回复", "微信没回复", "微信失败", "微信为什么", "微信诊断", "微信状态",
)


def maybe_handle_weixin_mission_command(command: str) -> Optional[str]:
    """Return a direct reply if the command is a Mission Control command.

    Returning None means the message should continue to the normal Agent.
    """
    text = str(command or "").strip()
    if not text:
        return None
    weixin_diag = "微信" in text and any(token in text for token in ("慢", "没反应", "无回复", "没回复", "失败", "为什么", "诊断", "状态"))
    if not weixin_diag and not any(token in text for token in CONTROL_KEYWORDS):
        return None
    try:
        from channel.web.web_channel import IntelligenceRunsHandler, MissionControlHandler
        payload = MissionControlHandler._build_payload()
        if "微信" in text and any(token in text for token in ("修复", "重建", "刷新")) and any(token in text for token in ("文件", "索引", "目录")):
            try:
                result = MissionControlHandler._run_weixin_repair_action("rebuild_file_index")
                reply = result.get("message") or "已开始重建微信文件索引。"
                MissionControlHandler._record_command_learning(text, {"intent": "repair_weixin_file_index", "target": "agent2"}, "success", reply, source="微信助理", channel="weixin")
                return reply
            except Exception as error:
                reply = f"文件索引修复失败：{str(error)[:120]}"
                MissionControlHandler._record_command_learning(text, {"intent": "repair_weixin_file_index", "target": "agent2"}, "failed", reply, source="微信助理", channel="weixin")
                return reply
        routed = MissionControlHandler._route_external_command(text, source="微信助理", channel="weixin", payload=payload)
        decision = routed.get("decision") or {}
        intent = decision.get("intent") or ""
        target = decision.get("target") or ""

        if intent == "retry_failed_runs":
            run_ids = [str(x) for x in (decision.get("run_ids") or []) if str(x).strip()][:5]
            if not run_ids:
                MissionControlHandler._record_command_learning(text, decision, "success", "没有可重试的失败任务", source="微信助理", channel="weixin")
                return "我查了一下，当前没有找到可重试的近期失败任务。你可以在 Web 端打开「总控台 → 最近执行」查看详情。"
            ok = 0
            failed = 0
            messages = []
            for run_id in run_ids:
                try:
                    result = IntelligenceRunsHandler.retry_run(run_id=run_id)
                    ok += 1
                    actions = result.get("recovery_retry_actions") or []
                    if actions:
                        messages.extend(str(x) for x in actions[:2])
                except Exception as error:
                    failed += 1
                    messages.append(str(error)[:80])
            outcome = "success" if ok and not failed else ("failed" if failed and not ok else "planned")
            reply = f"已按推荐参数重试失败任务：成功 {ok} 个，失败 {failed} 个。"
            if messages:
                reply += "\n" + "\n".join(f"- {item}" for item in list(dict.fromkeys(messages))[:4])
            MissionControlHandler._record_command_learning(text, decision, outcome, reply, source="微信助理", channel="weixin")
            return reply

        if intent == "diagnose_weixin":
            reply = decision.get("message") or "微信诊断暂时没有可用数据。"
            MissionControlHandler._record_command_learning(text, decision, "success", reply, source="微信助理", channel="weixin")
            return reply

        if target == "approvals":
            count = (payload.get("summary") or {}).get("pending_approvals") or 0
            reply = f"当前有 {count} 个待审批请求。请打开 Web 端「总控台 → 处理审批」快速处理。"
            MissionControlHandler._record_command_learning(text, decision, "success", reply, source="微信助理", channel="weixin")
            return reply
        if target == "artifacts":
            items = payload.get("recent_artifacts") or []
            if not items:
                reply = "当前还没有可查看的近期产物。"
            else:
                rows = []
                for item in items[:5]:
                    title = str(item.get("title") or "未命名产物")
                    created = str(item.get("created_at") or "").replace("T", " ")[:16]
                    rows.append(f"- {created}｜{title}")
                reply = "最近产物：\n" + "\n".join(rows)
            MissionControlHandler._record_command_learning(text, decision, "success", reply, source="微信助理", channel="weixin")
            return reply
        if target == "maintenance":
            maintenance = payload.get("maintenance") or {}
            latest = maintenance.get("latest") or {}
            cache = maintenance.get("cache") or {}
            summary = payload.get("summary") or {}
            reply = (
                "维护看板状态：\n"
                f"- 最近维护：{latest.get('label') or latest.get('status') or '暂无'}\n"
                f"- 最近失败：{summary.get('recent_failures') or 0} 个\n"
                f"- 缓存占用：{cache.get('total_mb', 0)} MB\n"
                "需要详细处理时，请打开 Web 端「总控台 → 维护看板」。"
            )
            MissionControlHandler._record_command_learning(text, decision, "success", reply, source="微信助理", channel="weixin")
            return reply
        if target == "channels":
            reply = "通道配置需要在 Web 端完成。请打开「设置 → 通道与连接」，可配置 Web、微信、Webhook、邮箱等发送方式。"
            MissionControlHandler._record_command_learning(text, decision, "success", reply, source="微信助理", channel="weixin")
            return reply

        # Creation and broad automation requests are better handled by the full Agent.
        if target in {"new_task", "intelligence", "automation"}:
            MissionControlHandler._record_command_learning(text, decision, "planned", decision.get("message", ""), source="微信助理", channel="weixin")
            return None
        return None
    except Exception as error:
        logger.warning(f"[WeixinMissionControl] bridge failed: {error}")
        return None
