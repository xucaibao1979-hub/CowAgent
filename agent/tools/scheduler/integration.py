"""
Integration module for scheduler with AgentBridge
"""

import os
import threading
from typing import Dict, Optional
from config import conf
from common.log import logger
from common.utils import expand_path
from bridge.context import Context, ContextType
from bridge.reply import Reply, ReplyType

# Backward-compatible aliases for the configured default agent.
_scheduler_service = None
_task_store = None
_scheduler_services: Dict[str, object] = {}
_task_stores: Dict[str, object] = {}
# Module-level lock to guard idempotent initialization across threads
_init_lock = threading.RLock()


def _get_running_channel(channel_type: str):
    """Return the live channel instance managed by the app when available."""
    channel_type = str(channel_type or "").strip()
    try:
        import sys
        main_module = sys.modules.get("__main__")
        app_module = sys.modules.get("app")
        manager = getattr(main_module, "_channel_mgr", None) if main_module else None
        if manager is None and app_module is not None:
            manager = getattr(app_module, "_channel_mgr", None)
        channel = manager.get_channel(channel_type) if manager else None
        if channel is not None:
            return channel
    except Exception as error:
        logger.warning(f"[Scheduler] Failed to get running channel {channel_type}: {error}")
    try:
        from channel.channel_factory import create_channel
        return create_channel(channel_type)
    except Exception:
        return None


def _resolve_workspace(workspace_root: str = None, agent_id: str = None):
    """An explicit workspace wins, then an explicit agent_id, then the routed
    identity. An agent_id that does not resolve raises: falling back to the
    default workspace here would silently file one Agent's tasks under
    another."""
    if workspace_root is not None:
        return os.path.realpath(expand_path(str(workspace_root)))
    from common.runtime_identity import current_identity
    from common.state_dir import state_root

    identity = current_identity()
    if agent_id:
        identity = identity.derive(agent_id=agent_id)
    return str(state_root(identity))


def init_scheduler(agent_bridge, workspace_root: str = None, agent_id: str = None) -> bool:
    """
    Initialize scheduler service (idempotent).

    Safe to call multiple times and from multiple threads: only the first
    successful call creates the singleton ``SchedulerService`` + background
    scanning thread. Subsequent calls return immediately.

    Args:
        agent_bridge: AgentBridge instance

    Returns:
        True if scheduler is initialized (newly created or already running)
    """
    global _scheduler_service, _task_store
    workspace_root = _resolve_workspace(workspace_root, agent_id)
    agent_id = agent_bridge.agent_registry.get(agent_id).id

    # Fast path: already initialized and running
    service = _scheduler_services.get(workspace_root)
    if service is not None and getattr(service, "running", False):
        return True

    with _init_lock:
        # Re-check under the lock to avoid races where multiple threads
        # passed the fast-path check before any of them acquired the lock.
        service = _scheduler_services.get(workspace_root)
        if service is not None and getattr(service, "running", False):
            return True

        try:
            from agent.tools.scheduler.task_store import TaskStore
            from agent.tools.scheduler.scheduler_service import SchedulerService

            from common.state_dir import scheduler_file

            store_path = str(scheduler_file(base=workspace_root))

            # Create task store (reuse if already created)
            task_store = _task_stores.get(workspace_root)
            if task_store is None:
                task_store = TaskStore(store_path)
                _task_stores[workspace_root] = task_store
                logger.debug(f"[Scheduler] Task store initialized: {store_path}")

            # Create execute callback. Returns True on success, False to ask
            # the scheduler to retry on the next tick (e.g. channel not yet
            # ready right after process start).
            def execute_task_callback(task: dict):
                # Scheduler threads carry no identity of their own. Binding it
                # here is the counterpart to chat_channel._handle: leaf code
                # reached from a task (ToolManager, tmp_dir, memory) then lands
                # in this Agent's workspace rather than the default one's.
                from common.runtime_identity import identity_scope

                try:
                    with identity_scope(agent_id=agent_id):
                        action = task.get("action", {})
                        action_type = action.get("type")
                        channel_type = action.get("channel_type", "unknown")
                        receiver = action.get("receiver", "")
                        readiness_receiver = (
                            _delivery_session_id(action)
                            if channel_type == "web"
                            else receiver
                        )

                        # Fixed reminders should try the unified notification
                        # dispatcher directly.  Its Weixin/WeCom path can
                        # switch to the newest sendable conversation; the
                        # older readiness probe may falsely fail after QR
                        # refreshes or backend restarts.
                        skip_readiness_probe = action_type == "send_message" and channel_type in {
                            "weixin", "wecom_bot", "email", "webhook"
                        }
                        if not skip_readiness_probe and not _is_channel_ready(channel_type, readiness_receiver, agent_id):
                            if channel_type == "web":
                                action["_inbox_only"] = True
                            else:
                                task["_last_run_status"] = "retrying"
                                task["_last_result_summary"] = (
                                    f"{channel_type} 通道暂未就绪，等待自动重试；"
                                    "如果长时间未恢复，请重新扫码或给 Agent 发送一条消息刷新会话。"
                                )
                                logger.warning(
                                    f"[Scheduler] Task {task.get('id')}: channel "
                                    f"'{channel_type}' not ready for receiver={receiver} "
                                    f"(no inbound msg cached since restart?); deferring"
                                )
                                return False
                        if action.get("_inbox_only"):
                            logger.warning(
                                f"[Scheduler] Task {task.get('id')}: web session is offline; "
                                "the result will be stored in the application inbox"
                            )

                        if action_type == "agent_task":
                            return _execute_agent_task(task, agent_bridge, agent_id)
                        elif action_type == "send_message":
                            return _execute_send_message(task, agent_bridge, agent_id)
                        elif action_type == "tool_call":
                            return _execute_tool_call(task, agent_bridge, agent_id)
                        elif action_type == "skill_call":
                            return _execute_skill_call(task, agent_bridge, agent_id)
                        else:
                            logger.warning(f"[Scheduler] Unknown action type: {action_type}")
                            return True
                except Exception as e:
                    logger.error(f"[Scheduler] Error executing task {task.get('id')}: {e}")
                    return False

            # Create scheduler service
            service = SchedulerService(task_store, execute_task_callback)
            service.start()
            _scheduler_services[workspace_root] = service
            if agent_id == agent_bridge.agent_registry.default_agent_id:
                _scheduler_service = service
                _task_store = task_store

            logger.info(
                f"[Scheduler] Service initialized for agent={agent_id}, "
                f"workspace={workspace_root}"
            )
            return True

        except Exception as e:
            logger.error(f"[Scheduler] Failed to initialize scheduler: {e}")
            return False


def _is_channel_ready(
    channel_type: str, receiver: str, agent_id: str = None
) -> bool:
    """Best-effort readiness probe for outbound channels.

    Returns False when we know the send will drop (e.g. weixin not yet
    logged in, web session has no polling queue), so the scheduler can
    defer instead of consuming the task. Unknown channels return True
    to preserve previous behaviour.
    """
    if not channel_type or channel_type == "unknown":
        return True
    try:
        channel = _get_running_channel(channel_type)
        if channel is None:
            return False

        if channel_type == "weixin":
            if _weixin_receiver_token_ready(channel, receiver):
                return True
            return bool(_fallback_weixin_receiver(channel, exclude=receiver))

        if channel_type == "web":
            if hasattr(channel, "has_session_queue"):
                return channel.has_session_queue(receiver, agent_id)
            queues = getattr(channel, "session_queues", None)
            if not queues or receiver not in queues:
                return False
            return True

        return True
    except Exception as e:
        logger.warning(f"[Scheduler] Channel readiness check failed for {channel_type}: {e}")
        return True


def _weixin_receiver_token_ready(channel, receiver: str) -> bool:
    receiver = _base_weixin_receiver(receiver)
    if not receiver:
        return False
    try:
        if hasattr(channel, "has_context_token"):
            return bool(channel.has_context_token(receiver))
        tokens = getattr(channel, "_context_tokens", None)
        return bool(tokens and receiver in tokens)
    except Exception:
        return False


def _fallback_weixin_receiver(channel=None, exclude: str = "") -> str:
    try:
        from plugins.intelligence_center.notifications import NotificationDispatcher
        return NotificationDispatcher._default_weixin_receiver(channel, exclude=_base_weixin_receiver(exclude))
    except Exception:
        return ""


def _base_weixin_receiver(receiver: str) -> str:
    """Strip short-lived routing suffixes from Weixin receiver ids.

    Older scheduled reminders stored receiver ids such as
    ``xxx@im.wechat#week:2026-W38``.  The suffix is only a transient routing
    marker; keeping it in later weeks prevents the scheduler from matching the
    current send token.
    """
    return str(receiver or "").split("#week:", 1)[0].strip()


def _resolve_outbound_receiver(channel_type: str, receiver: str, action: dict, channel=None) -> str:
    """Return a currently sendable receiver for scheduler pushes.

    Weixin push tokens are tied to real inbound conversations and can expire or
    move when the user rescans/logs in.  Old scheduled reminders should follow
    the newest sendable conversation instead of failing forever.
    """
    receiver = str(receiver or "").strip()
    if channel_type != "weixin":
        if channel_type == "wecom_bot":
            fallback = _fallback_wecom_receiver()
            if fallback and receiver != fallback:
                logger.warning(
                    "[Scheduler] WeCom receiver %s is not the current default sendable chat; switched to %s",
                    receiver,
                    fallback,
                )
                if isinstance(action, dict):
                    action["receiver"] = fallback
                    action["receiver_name"] = fallback
                    action["notify_session_id"] = fallback
                return fallback
        return receiver
    base_receiver = _base_weixin_receiver(receiver)
    if base_receiver and base_receiver != receiver and isinstance(action, dict):
        logger.warning(
            "[Scheduler] Normalized stale Weixin receiver %s to %s",
            receiver,
            base_receiver,
        )
        receiver = base_receiver
        action["receiver"] = base_receiver
        action["receiver_name"] = base_receiver
        action["notify_session_id"] = base_receiver
    try:
        if _weixin_receiver_token_ready(channel, receiver):
            return receiver
        fallback = _fallback_weixin_receiver(channel, exclude=receiver)
        if fallback:
            logger.warning(
                "[Scheduler] Weixin receiver %s has no send token; switched to current sendable receiver %s",
                receiver,
                fallback,
            )
            if isinstance(action, dict):
                action["receiver"] = fallback
                action["receiver_name"] = fallback
                action["notify_session_id"] = fallback
            return fallback
    except Exception as error:
        logger.warning(f"[Scheduler] Failed to resolve fallback weixin receiver: {error}")
    return receiver


def _fallback_wecom_receiver() -> str:
    try:
        from plugins.intelligence_center.notifications import NotificationDispatcher
        return NotificationDispatcher._default_wecom_receiver()
    except Exception:
        return ""


def _delivery_session_id(action: dict) -> str:
    """Resolve the conversation that should receive a scheduled result."""
    return str(action.get("notify_session_id") or action.get("receiver") or "")


def get_task_store(workspace_root: str = None, agent_id: str = None):
    """Get the task store owned by one agent workspace."""
    workspace_root = _resolve_workspace(workspace_root, agent_id)
    return _task_stores.get(workspace_root)


def get_scheduler_service(workspace_root: str = None, agent_id: str = None):
    """Get the scheduler service owned by one agent workspace."""
    workspace_root = _resolve_workspace(workspace_root, agent_id)
    return _scheduler_services.get(workspace_root)


def reset_scheduler_services(stop: bool = True) -> None:
    """Stop and forget all scheduler services, primarily for reloads/tests."""
    global _scheduler_service, _task_store
    with _init_lock:
        if stop:
            for service in list(_scheduler_services.values()):
                try:
                    service.stop()
                except Exception:
                    pass
        _scheduler_services.clear()
        _task_stores.clear()
        _scheduler_service = None
        _task_store = None


def _remember_delivered_output(
    agent_bridge,
    task: dict,
    channel_type: str,
    content: str,
    agent_id: str = None,
) -> None:
    """Best-effort persistence of the message the scheduler sent to a user.

    Uses notify_session_id (the real chat session_id stored at task creation time)
    so that group chats correctly associate the output with the user's conversation.
    Falls back to receiver for backward compatibility with old tasks.

    Per-action-type behaviour:
        - agent_task / tool_call / skill_call: gated by ``scheduler_inject_to_session``
          (default True). These produce AI-generated content worth remembering.
        - send_message: additionally gated by ``scheduler_inject_send_message``
          (default False). Fixed reminder text rarely benefits follow-up Q&A and
          would just consume context tokens.
    """
    if not content:
        return
    action = task.get("action", {})
    action_type = action.get("type", "")

    # send_message defaults to NOT being injected; explicit opt-in via config.
    if action_type == "send_message":
        # Web tasks need a durable place where the user can read a reminder
        # that fired while the page was closed. Other chat channels already
        # retain the delivered message at their provider, so keep their old
        # opt-in behaviour to avoid unnecessary Agent context growth.
        if channel_type != "web" and not conf().get("scheduler_inject_send_message", False):
            return

    session_id = action.get("notify_session_id") or action.get("receiver")
    if not session_id:
        return
    try:
        remember = getattr(agent_bridge, "remember_scheduled_output", None)
        if remember:
            task_desc = action.get("task_description") or action.get("content", "")
            kwargs = {
                "channel_type": channel_type,
                "task_description": task_desc,
            }
            if hasattr(agent_bridge, "agent_registry"):
                kwargs["agent_id"] = agent_id
            remember(session_id, str(content), **kwargs)
    except Exception as e:
        logger.warning(
            f"[Scheduler] Failed to remember delivered output for {session_id}: {e}"
        )


def _store_scheduler_artifact(task: dict, content: str, status: str = "success", error: str = "") -> str:
    """Persist scheduler output so every execution appears in 产物/交付物."""
    try:
        from plugins.intelligence_center.output_formatting import compact_artifact_summary, format_automation_output
        if content:
            content = format_automation_output(str(content))
        summary = compact_artifact_summary(str(content or error or ""), 180)
    except Exception:
        summary = " ".join(str(content or error or "").split())[:180]
    task["_last_run_status"] = status
    task["_last_result_summary"] = summary or ("执行失败" if status == "failed" else "暂无内容")
    try:
        from datetime import datetime
        from pathlib import Path
        from config import get_data_root
        root = Path(get_data_root()) / "intelligence_center" / "reports"
        root.mkdir(parents=True, exist_ok=True)
        task_id = str(task.get("id") or "scheduled").strip()[:40]
        existing_path = _recent_scheduler_artifact(root, task_id, status=status, summary=summary)
        if existing_path:
            task["_last_artifact_path"] = existing_path
            task["_artifact_reused"] = True
            logger.warning(
                f"[Scheduler] Suppressed duplicate artifact for task={task_id}, "
                f"reuse={existing_path}"
            )
            return existing_path
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
        path = root / f"scheduler-{task_id}-{stamp}.md"
        title = str(task.get("name") or "定时任务结果").strip()
        action = task.get("action", {}) if isinstance(task.get("action"), dict) else {}
        task_body = str(action.get("task_description") or action.get("content") or "").strip()
        if task_body:
            try:
                from agent.output_compactor import compact_text
                task_body = compact_text(task_body, 30, 160)
            except Exception:
                task_body = " ".join(task_body.split())[:160]
        body = str(content or "").strip()
        if not body:
            body = "当前条件下暂无合格结果"
        if status in ("failed", "empty"):
            label = "执行失败" if status == "failed" else "暂无内容"
            parts = [
                f"# {title}",
                f"生成时间：{datetime.now().astimezone().isoformat(timespec='seconds')}",
                f"状态：{label}",
            ]
            if task_body:
                parts.extend(["", "## 任务内容", task_body])
            parts.extend(["", "## 执行结果", body])
            if error:
                parts.extend(["", "## 失败原因", str(error)])
            body = "\n".join(parts)
        first_line = body.splitlines()[0].strip() if body else ""
        if first_line.lstrip("# ").strip() != title:
            body = f"# {title}\n\n{body}"
        path.write_text(body, encoding="utf-8")
        task["_last_artifact_path"] = str(path)
        task["_artifact_reused"] = False
        try:
            from intelligence_center.artifact_service import ArtifactService
            artifact_root = Path(get_data_root()) / "intelligence_center"
            action_channel = str(action.get("channel_type") or "web").strip()
            tags = ["定时任务", "自动化", "报告"]
            if status == "failed":
                tags.insert(0, "失败")
            elif status == "empty":
                tags.insert(0, "暂无内容")
            if action_channel:
                tags.append(action_channel.upper() if action_channel == "web" else action_channel)
            ArtifactService(str(artifact_root)).register(str(path), {
                "title": title,
                "type": str(task.get("artifact_type") or "report"),
                "tags": tags,
                "task_id": task.get("id"),
                "task_name": title,
                "run_status": status,
                "trigger": "scheduled_agent",
                "kind_label": "定时任务",
                "result_summary": task.get("_last_result_summary") or summary,
                "notification_summary": f"发送到：{action_channel.upper() if action_channel == 'web' else action_channel or 'web'}",
                "started_at": datetime.now().astimezone().isoformat(timespec="seconds"),
                "finished_at": datetime.now().astimezone().isoformat(timespec="seconds"),
            })
        except Exception as register_error:
            logger.warning(f"[Scheduler] Failed to register artifact: {register_error}")
        return str(path)
    except Exception as error:
        logger.warning(f"[Scheduler] Failed to store artifact: {error}")
        return ""


def _recent_scheduler_artifact(root, task_id: str, status: str = "success", summary: str = "") -> str:
    """Return a very recent artifact for the same scheduled task if present.

    This is a last-resort guard for desktop restarts or two backends briefly
    running at the same time. The scheduler lock prevents execution overlap;
    this prevents the UI from being flooded by near-identical files if an old
    process was already mid-write.
    """
    try:
        import time
        from datetime import datetime
        from pathlib import Path

        now = time.time()
        today = datetime.now().date()
        prefix = f"scheduler-{task_id}-"
        candidates = []
        for path in Path(root).glob(f"{prefix}*.md"):
            try:
                stat = path.stat()
                if status == "empty":
                    # 空结果一天只保留一条，避免“暂无内容”把产物中心刷屏。
                    if datetime.fromtimestamp(stat.st_mtime).date() == today:
                        candidates.append(path)
                    continue
                reuse_window = 120
                if now - stat.st_mtime <= reuse_window:
                    candidates.append(path)
            except OSError:
                continue
        if not candidates:
            return ""
        candidates.sort(key=lambda p: p.stat().st_mtime, reverse=True)
        newest = candidates[0]
        if summary:
            try:
                old_text = newest.read_text(encoding="utf-8", errors="ignore")
                old_summary = " ".join(old_text.split())[:180]
                if old_summary == summary:
                    return str(newest)
            except Exception:
                pass
        if status == "empty":
            return str(newest)
        return ""
    except Exception:
        return ""


def _store_scheduled_inbox(task: dict, content: str, status: str = "success", report_path: str = "") -> None:
    """Persist web task output even when the original browser session is offline."""
    try:
        import uuid
        from datetime import datetime
        from config import get_data_root
        from intelligence_center.inbox_service import InboxStore
        action = task.get("action", {})
        title_status = {"success": "执行完成", "failed": "执行失败", "empty": "暂无内容"}.get(status, status)
        source_id = f"scheduler:{task.get('id')}:{uuid.uuid4().hex[:10]}"
        if status == "empty":
            source_id = f"scheduler:{task.get('id')}:empty:{datetime.now().strftime('%Y%m%d')}"
        InboxStore(os.path.join(get_data_root(), "intelligence_center")).upsert(
            source_id=source_id,
            kind="automation_result",
            title=f"{task.get('name') or '定时任务'} · {title_status}",
            summary=str(content or ""), status=status,
            target={"view": "automation", "tab": "records", "task_id": task.get("id"), "report_path": report_path},
            payload={"task_id": task.get("id"), "channel": action.get("channel_type", "web"),
                     "report_path": report_path},
        )
    except Exception as error:
        logger.warning(f"[Scheduler] Failed to store inbox result: {error}")


def _record_scheduler_notification_event(
    task: dict,
    channel_type: str,
    receiver: str,
    success: bool,
    message: str = "",
    content: str = "",
) -> None:
    """Keep a compact delivery audit trail for scheduled task notifications."""
    try:
        import json
        import uuid
        from datetime import datetime
        from pathlib import Path
        from config import get_data_root
        root = Path(get_data_root()) / "intelligence_center"
        root.mkdir(parents=True, exist_ok=True)
        path = root / "notification_events.json"
        try:
            data = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
        except Exception:
            data = {}
        events = data.get("events") if isinstance(data, dict) else []
        if not isinstance(events, list):
            events = []
        safe_receiver = "configured_webhook" if channel_type == "webhook" and str(receiver).startswith(("http://", "https://")) else receiver
        events.append({
            "id": uuid.uuid4().hex[:12],
            "created_at": datetime.now().astimezone().isoformat(timespec="seconds"),
            "task_id": task.get("id"),
            "task_name": task.get("name") or "定时任务",
            "target": channel_type or "unknown",
            "receiver": safe_receiver,
            "success": bool(success),
            "delivery_status": "success" if success else "failed",
            "verified": bool(success),
            "verification_message": "通道已接受发送请求" if success else str(message or "发送失败"),
            "message": str(message or ""),
            "summary": " ".join(str(content or "").split())[:220],
        })
        path.write_text(json.dumps({"events": events[-1000:]}, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception as error:
        logger.warning(f"[Scheduler] Failed to record notification event: {error}")


def _audit_delivery(task: dict, stage: str, channel: str, receiver: str, status: str, message: str, details: dict = None):
    try:
        try:
            from intelligence_center.execution_audit import append_execution_event
        except Exception:
            from plugins.intelligence_center.execution_audit import append_execution_event
        append_execution_event(task, stage, channel, receiver, status, message, details or {})
    except Exception as error:
        logger.warning(f"[Scheduler] Delivery audit skipped: {error}")


def _preflight_scheduler_delivery(task: dict, channel_type: str, receiver: str) -> dict:
    try:
        try:
            from intelligence_center.execution_audit import channel_preflight, request_channel_recovery
        except Exception:
            from plugins.intelligence_center.execution_audit import channel_preflight, request_channel_recovery
        health = channel_preflight(task, channel_type, receiver) or {}
        if not health.get("ok", True) and channel_type in {"weixin", "wecom_bot"}:
            request_channel_recovery(channel_type, health.get("message") or "scheduler preflight")
        return health
    except Exception as error:
        logger.warning(f"[Scheduler] Delivery preflight skipped: {error}")
        _audit_delivery(task, "preflight", channel_type, receiver, "warning", f"预检暂不可用，继续尝试发送：{error}")
        return {"ok": True, "message": str(error)}


def _send_scheduler_output(
    task: dict,
    agent_bridge,
    context: Context,
    content: str,
    report_path: str = "",
    status: str = "success",
    agent_id: str = None,
) -> bool:
    """Unified delivery path for scheduler output.

    Every scheduled execution should leave the same trail: inbox/web history
    when applicable, notification audit event, and channel history for chat
    channels.  Webhook is handled here instead of pretending it is a chat
    channel.
    """
    action = task.get("action", {}) if isinstance(task.get("action"), dict) else {}
    channel_type = str(action.get("channel_type") or context.get("channel_type") or "web").strip()
    receiver = str(action.get("receiver") or context.get("receiver") or "").strip()
    delivery_session_id = _delivery_session_id(action)
    preflight = _preflight_scheduler_delivery(task, channel_type, receiver)

    if action.get("silent", False):
        _audit_delivery(task, "send", channel_type, receiver, "success", "静默执行，未发送通知", {"silent": True})
        _record_scheduler_notification_event(task, channel_type, receiver, True, "静默执行，未发送通知", content)
        return True

    if status == "empty" and task.get("_artifact_reused"):
        # 同一任务同一天已经记录过空结果时，不再重复推送到微信/企微/Webhook。
        # Web 收件箱使用固定 source_id 更新同一条记录，保留可见状态但不刷屏。
        _store_scheduled_inbox(task, content, status=status, report_path=report_path)
        _record_scheduler_notification_event(
            task,
            channel_type,
            receiver,
            True,
            "同日暂无内容已合并到已有记录，未重复发送通知",
            content,
        )
        return True

    if channel_type == "web":
        _store_scheduled_inbox(task, content, status=status, report_path=report_path)
        _audit_delivery(task, "verify", channel_type, receiver or "web_user", "success", "已保存到 Web 消息中心", {"report_path": report_path})
        _record_scheduler_notification_event(task, channel_type, receiver, True, content=content)
        _remember_delivered_output(agent_bridge, task, channel_type, content, agent_id)
        if action.get("_inbox_only"):
            return True

    try:
        if channel_type == "webhook":
            from config import conf
            from plugins.intelligence_center.notifications import NotificationDispatcher
            url = str(action.get("webhook_url") or "").strip()
            if not url and receiver != "configured_webhook":
                url = receiver
            if not url:
                url = str(conf().get("webhook_url") or "").strip()
            if not url:
                raise RuntimeError("Webhook 地址未配置")
            NotificationDispatcher._webhook({"type": "webhook", "url": url, "name": "Webhook", "receiver": receiver}, task, content)
            _audit_delivery(task, "verify", channel_type, receiver or "configured_webhook", "success", "Webhook 已接受发送请求")
            _record_scheduler_notification_event(task, channel_type, receiver or "configured_webhook", True, content=content)
            return True

        if channel_type == "email":
            from plugins.intelligence_center.notifications import NotificationDispatcher
            NotificationDispatcher._email({"type": "channel", "channel": "email", "receiver": receiver}, task, content)
            _audit_delivery(task, "verify", channel_type, receiver, "success", "邮件已提交给 SMTP 服务")
            _record_scheduler_notification_event(task, channel_type, receiver, True, content=content)
            return True

        channel = _get_running_channel(channel_type)
        if not channel:
            raise RuntimeError(f"消息通道“{channel_type}”不可用")

        resolved_receiver = _resolve_outbound_receiver(channel_type, receiver, action, channel)
        if resolved_receiver and resolved_receiver != receiver:
            _audit_delivery(task, "resolve_receiver", channel_type, receiver, "success", f"已切换到当前可发送会话：{resolved_receiver}", {"resolved_receiver": resolved_receiver})
            receiver = resolved_receiver
            delivery_session_id = resolved_receiver
            context["receiver"] = resolved_receiver
            context["session_id"] = resolved_receiver

        if channel_type == "web" and hasattr(channel, "request_to_session"):
            request_id = context.get("request_id")
            if request_id:
                channel.request_to_session[request_id] = delivery_session_id

        delivered = channel.send(Reply(ReplyType.TEXT, content), context)
        if delivered is False:
            detail = getattr(channel, "_last_send_error", "") or getattr(channel, "_last_error", "")
            raise RuntimeError("通道返回发送失败" + (f"：{detail}" if detail else ""))
        _audit_delivery(
            task,
            "verify",
            channel_type,
            receiver,
            "success",
            "通道已接受发送请求；如需逐条确认，可在连接中心查看会话状态",
            {"provider_result": "accepted", "preflight_ok": bool(preflight.get("ok", True))},
        )
        _record_scheduler_notification_event(task, channel_type, receiver, True, content=content)
        _remember_delivered_output(agent_bridge, task, channel_type, content, agent_id)
        return True
    except Exception as error:
        logger.error(f"[Scheduler] Failed to deliver scheduled output: {error}")
        task["_last_run_status"] = "failed"
        task["_last_result_summary"] = f"{channel_type or 'unknown'} 发送失败：{error}"
        _audit_delivery(task, "send", channel_type, receiver, "failed", str(error), {"preflight": preflight})
        _record_scheduler_notification_event(task, channel_type, receiver, False, str(error), content)
        if channel_type in {"weixin", "wecom_bot"}:
            try:
                from plugins.intelligence_center.notifications import NotificationDispatcher
                NotificationDispatcher.enqueue_pending_delivery(task, channel_type, receiver, content, str(error))
            except Exception as queue_error:
                logger.warning(f"[Scheduler] Failed to enqueue pending delivery: {queue_error}")
        if channel_type == "web":
            _store_scheduled_inbox(task, f"消息发送失败：{error}", status="failed", report_path=report_path)
            return False
        if not _scheduler_allow_web_fallback():
            logger.warning("[Scheduler] Web fallback is disabled by recovery policy")
            return False
        try:
            fallback_content = (
                f"原通知通道“{channel_type or 'unknown'}”发送失败，已自动保底保存到 Web。\n\n"
                f"{content}"
            )
            _store_scheduled_inbox(task, fallback_content, status=status or "warning", report_path=report_path)
            _audit_delivery(task, "fallback", "web", "web_user", "success", f"原通道发送失败，已保底保存到 Web：{error}", {"source_channel": channel_type})
            _record_scheduler_notification_event(
                task,
                "web",
                "web_user",
                True,
                f"原通道发送失败，已自动补发到 Web：{error}",
                content,
            )
            logger.warning(f"[Scheduler] Task {task.get('id')}: delivery fallback saved to web inbox")
            return True
        except Exception as fallback_error:
            logger.error(f"[Scheduler] Failed to fallback scheduled output to web: {fallback_error}")
        return False


def _try_execute_news_briefing_task(
    task: dict,
    task_description: str,
    agent_bridge,
    context: Context,
    agent_id: str = None,
) -> Optional[bool]:
    """Run generic scheduled news/briefing tasks through the deterministic
    local collector when candidates are available.

    Older "普通定时任务" such as “查询新闻热点” were stored as free text and
    therefore relied on the LLM to browse/summarize.  That is slow and can
    produce repeated empty artifacts when the model/tool path is unavailable.
    This adapter reuses the same server-side candidate collector used by the
    Network Search Agent; if it finds candidates, we render the standard
    morning-brief format directly, then use the normal scheduler delivery path.
    Returning None means "not a news task or no candidates; continue legacy
    Agent execution".
    """
    text = _normalize_scheduler_news_prompt(str(task_description or ""))
    if not any(token in text for token in (
        "新闻热点", "热点新闻", "新闻资讯", "早间简报", "每日简报",
        "AI新闻", "AI 新闻", "行业动态", "隔夜重点",
    )):
        return None
    try:
        from config import get_data_root
        from plugins.intelligence_center.briefing_enrichment import (
            _BRIEF_MARKER,
            enrich_personal_briefing_prompt,
        )
        from plugins.intelligence_center.news import NewsAssistantService

        enriched = enrich_personal_briefing_prompt(
            text, os.path.join(get_data_root(), "intelligence_center")
        )
        marker = f"【{_BRIEF_MARKER}】"
        if marker not in enriched:
            return None

        items = []
        for line in enriched.split(marker, 1)[1].splitlines():
            line = line.strip()
            if not line or not line[0].isdigit() or "|" not in line:
                continue
            parts = line.split("|", 4)
            if len(parts) < 5:
                continue
            _, title, published_at, link, summary = parts
            if not title.strip() or not link.strip():
                continue
            items.append({
                "title": title.strip(),
                "published_at": published_at.strip(),
                "link": link.strip(),
                "summary": summary.strip(),
                "source": link.strip(),
            })

        if not items:
            return None

        limit = _scheduler_news_limit(text)
        report_task = {
            "name": task.get("name") or "个人早间简报",
            "keywords": [],
            "sources": [],
            "rules": {},
        }
        content = NewsAssistantService._build_report(report_task, items[:limit], [], "")
        report_path = _store_scheduler_artifact(task, content, status="success")
        if not _send_scheduler_output(task, agent_bridge, context, content, report_path, "success", agent_id):
            return False
        logger.info(
            f"[Scheduler] Task {task.get('id')}: rendered news briefing from "
            f"{len(items[:limit])} local candidates"
        )
        return True
    except Exception as exc:
        logger.warning(f"[Scheduler] Deterministic news briefing skipped: {exc}")
        return None


def _scheduler_news_limit(text: str) -> int:
    """Extract a safe card count for scheduled news output."""
    import re
    material = str(text or "")
    match = re.search(r"最多\s*(\d+)\s*条", material)
    if match:
        return max(1, min(int(match.group(1)), 10))
    match = re.search(r"(\d+)\s*[-至到~]\s*(\d+)\s*条", material)
    if match:
        return max(1, min(int(match.group(2)), 10))
    match = re.search(r"整理\s*(\d+)\s*条", material)
    if match:
        return max(1, min(int(match.group(1)), 10))
    return 5


def _normalize_scheduler_news_prompt(text: str) -> str:
    """Make legacy news-task prompts safe and consistent at runtime.

    Some early one-sentence tasks stored output examples such as
    "链接：完整URL" or "没有合格内容时只输出暂无结果".  Those examples leaked into
    final artifacts and encouraged empty results.  Normalize them here without
    rewriting the user's saved task so old tasks benefit immediately.
    """
    import re
    value = str(text or "")
    if not any(token in value for token in (
        "新闻", "热点", "早报", "简报", "AI新闻", "AI 新闻", "行业动态", "资讯"
    )):
        return value
    value = re.sub(r"链接\s*[:：]\s*完整\s*URL", "标题后使用 [链接](原文URL)，禁止单独展示完整网址", value, flags=re.I)
    value = re.sub(r"\[链接\]\s*\(\s*原文\s*URL\s*\)", "[链接](原文URL)", value, flags=re.I)
    value = re.sub(r"不超过\s*120\s*字摘要", "30-50字具体摘要", value)
    value = re.sub(r"没有合格(?:内容|结果)?时只输出[“\"]?当前条件下暂无合格结果[”\"]?[。.]?", "", value)
    guard = (
        "\n\n【运行时优化】"
        "这是新闻/简报类定时任务。优先使用本地候选、RSS 或已配置来源；"
        "最近24小时候选不足时，可自动扩大到最近7天并按时效和重要性排序；"
        "输出必须使用“个人早间简报｜YYYY-MM-DD / 栏目一｜隔夜重点 / 标题 [链接](URL) / 30-50字摘要”格式；"
        "标题必须是简短事实标题；摘要必须是一句话概括内容含义、影响或可关注点，不能复述标题，不能使用“出现新的进展/可继续跟踪/体现人工智能技术与产业应用正在持续推进”等通用套话；"
        "禁止展示完整网址，禁止输出检索过程、候选统计或排查步骤。"
    )
    return value + guard if "【运行时优化】" not in value else value


def _scheduler_allow_web_fallback() -> bool:
    try:
        from config import get_data_root
        from intelligence_center.agent2_service import Agent2Service
        root = os.path.join(get_data_root(), "intelligence_center")
        settings = Agent2Service(root, get_data_root(), conf())._read().get("settings") or {}
        policy = settings.get("recovery_policy") or {}
        return bool(policy.get("enabled", True) and policy.get("allow_web_fallback", True))
    except Exception:
        return True


def _execute_agent_task(task: dict, agent_bridge, agent_id: str = None) -> bool:
    """
    Execute an agent_task action - let Agent handle the task.
    Returns True on successful delivery, False to retry next tick.
    """
    try:
        action = task.get("action", {})
        task_description = action.get("task_description")
        receiver = action.get("receiver")
        is_group = action.get("is_group", False)
        channel_type = action.get("channel_type", "unknown")
        delivery_session_id = _delivery_session_id(action)
        
        if not task_description:
            logger.error(f"[Scheduler] Task {task['id']}: No task_description specified")
            _store_scheduler_artifact(task, "缺少任务内容，任务未执行。", status="failed", error="No task_description specified")
            return True  # malformed task, don't loop forever
        
        if not receiver:
            logger.error(f"[Scheduler] Task {task['id']}: No receiver specified")
            _store_scheduler_artifact(task, "缺少消息接收人，任务未发送。", status="failed", error="No receiver specified")
            task["_last_run_status"] = "failed"
            task["_last_result_summary"] = "缺少消息接收人，提醒未送达"
            return False
        
        # Check for unsupported channels
        if channel_type == "dingtalk":
            logger.warning(f"[Scheduler] Task {task['id']}: DingTalk channel does not support scheduled messages (Stream mode limitation). Task will execute but message cannot be sent.")
        
        logger.info(f"[Scheduler] Task {task['id']}: Executing agent task '{task_description}'")
        task_description = _normalize_scheduler_news_prompt(task_description)

        # Ensure saved/legacy personal-brief tasks receive real local
        # candidates even when they were created before the frontend collector.
        try:
            from config import get_data_root
            from plugins.intelligence_center.briefing_enrichment import enrich_personal_briefing_prompt
            task_description = enrich_personal_briefing_prompt(
                task_description, os.path.join(get_data_root(), "intelligence_center")
            )
        except Exception as exc:
            logger.warning(f"[Scheduler] Briefing enrichment skipped: {exc}")

        # Wrap the raw description with an execution directive. The stored
        # description is often phrased as a rule ("every day at 08:00 send...").
        # Without this prefix the agent may treat it as a spec to acknowledge
        # instead of a task to run right now, especially after a mid-run failure.
        execution_prompt = (
            "这是一个定时任务的立即执行请求，当前已到执行时刻。"
            "请直接完成下面描述的任务并产出最终交付内容，"
            "无需复述、确认或讨论任务规则，不要输出任务指令或调试信息。"
            "若执行失败，返回简洁明确的失败说明。\n\n"
            "统一输出规范：按内容分类设置小标题；分类之间保留一行，分类内部不要空行；"
            "每条使用“- **子标题**：具体说明 [链接](URL)”；摘要字数以任务描述为准，未指定时使用20至50字；"
            "标题和正文必须分工明确：标题只写短事实，正文用一句话说明内容含义、影响或下一步关注点；禁止把标题原文复制到正文，禁止使用泛化套话。"
            "禁止展示完整网址；重要结论统一放入“今日关注”或“重点关注”栏目。\n\n"
            f"任务描述：\n{task_description}"
        )

        # Create a unique session_id for this scheduled task to avoid polluting user's conversation
        # Format: scheduler_<receiver>_<task_id> to ensure isolation
        scheduler_session_id = f"scheduler_{receiver}_{task['id']}"
        
        # Create context for Agent
        context = Context(ContextType.TEXT, execution_prompt)
        context["receiver"] = receiver
        context["isgroup"] = is_group
        context["session_id"] = scheduler_session_id
        context["agent_id"] = agent_id
        
        # Channel-specific setup
        if channel_type == "web":
            import uuid
            request_id = f"scheduler_{task['id']}_{uuid.uuid4().hex[:8]}"
            context["request_id"] = request_id
        elif channel_type == "feishu":
            context["receive_id_type"] = "chat_id" if is_group else "open_id"
            context["msg"] = None
        elif channel_type == "dingtalk":
            # DingTalk requires msg object, set to None for scheduled tasks
            context["msg"] = None
            if not is_group:
                sender_staff_id = action.get("dingtalk_sender_staff_id")
                if sender_staff_id:
                    context["dingtalk_sender_staff_id"] = sender_staff_id
        elif channel_type == "wecom_bot":
            context["msg"] = None
        elif channel_type == "webhook":
            context["webhook_url"] = action.get("webhook_url", "")

        # Use Agent to execute the task
        # Mark this as a scheduled task execution to prevent recursive task creation
        context["is_scheduled_task"] = True

        deterministic_news_result = _try_execute_news_briefing_task(
            task, task_description, agent_bridge, context, agent_id
        )
        if deterministic_news_result is not None:
            return deterministic_news_result
        
        try:
            # Don't clear history - scheduler tasks use isolated session_id so they won't pollute user conversations
            reply = agent_bridge.agent_reply(execution_prompt, context=context, on_event=None, clear_history=False)

            if not (reply and reply.content):
                # Empty is a valid outcome, but it must still leave a durable
                # trace so users can see that the task really ran.
                empty_content = "当前条件下暂无合格结果"
                report_path = _store_scheduler_artifact(task, empty_content, status="empty")
                if not _send_scheduler_output(task, agent_bridge, context, empty_content, report_path, "empty", agent_id):
                    return False
                logger.info(
                    f"[Scheduler] Task {task['id']}: agent produced no content, stored empty result"
                )
                return True

            try:
                from plugins.intelligence_center.output_formatting import format_automation_output
                reply.content = format_automation_output(reply.content)
            except Exception as exc:
                logger.warning(f"[Scheduler] Output formatting skipped: {exc}")

            report_path = _store_scheduler_artifact(task, reply.content)

            if not _send_scheduler_output(task, agent_bridge, context, reply.content, report_path, "success", agent_id):
                return False
            logger.info(f"[Scheduler] Task {task['id']} executed successfully, result sent to {receiver}")
            return True

        except Exception as e:
            logger.error(f"[Scheduler] Failed to execute task via Agent: {e}")
            failure = f"定时任务执行失败：{e}"
            report_path = _store_scheduler_artifact(task, failure, status="failed", error=str(e))
            if channel_type == "web":
                _store_scheduled_inbox(task, failure, status="failed", report_path=report_path)
            _record_scheduler_notification_event(task, channel_type, receiver, False, str(e), failure)
            import traceback
            logger.error(f"[Scheduler] Traceback: {traceback.format_exc()}")
            return False

    except Exception as e:
        logger.error(f"[Scheduler] Error in _execute_agent_task: {e}")
        import traceback
        logger.error(f"[Scheduler] Traceback: {traceback.format_exc()}")
        return False


def _execute_send_message(task: dict, agent_bridge, agent_id: str = None) -> bool:
    """Execute a send_message action. Returns True/False for delivery."""
    try:
        action = task.get("action", {})
        content = action.get("content", "")
        receiver = action.get("receiver")
        is_group = action.get("is_group", False)
        channel_type = action.get("channel_type", "unknown")
        if channel_type == "weixin":
            try:
                channel = _get_running_channel(channel_type)
                receiver = _resolve_outbound_receiver(channel_type, receiver, action, channel)
            except Exception as error:
                logger.warning(f"[Scheduler] Failed to pre-resolve weixin receiver: {error}")
        delivery_session_id = _delivery_session_id(action)
        
        if not receiver:
            logger.error(f"[Scheduler] Task {task['id']}: No receiver specified")
            _store_scheduler_artifact(task, "缺少消息接收人，任务未发送。", status="failed", error="No receiver specified")
            task["_last_run_status"] = "failed"
            task["_last_result_summary"] = "缺少消息接收人，提醒未送达"
            return False

        report_path = _store_scheduler_artifact(task, content or "提醒内容为空", status="empty" if not content else "success")

        if channel_type in {"weixin", "wecom_bot", "email", "webhook"}:
            try:
                from plugins.intelligence_center.notifications import NotificationDispatcher

                if channel_type == "webhook":
                    target = {
                        "type": "webhook",
                        "name": "Webhook",
                        "url": action.get("webhook_url") or receiver,
                        "receiver": receiver,
                    }
                else:
                    target = {
                        "type": "channel",
                        "channel": channel_type,
                        "receiver": receiver,
                        "name": channel_type,
                    }
                notify_task = {
                    "id": task.get("id"),
                    "name": task.get("name") or "定时提醒",
                    "notifications": [target],
                }
                results = NotificationDispatcher().dispatch(notify_task, content or "提醒内容为空")
                real_success = [
                    item for item in results
                    if item.get("success")
                    and not item.get("skipped")
                    and str(item.get("channel") or item.get("target") or "") == channel_type
                ]
                if real_success:
                    resolved = str(real_success[0].get("receiver") or "").strip()
                    if resolved and resolved != receiver and channel_type in {"weixin", "wecom_bot"}:
                        action["receiver"] = resolved
                        action["receiver_name"] = resolved
                        action["notify_session_id"] = resolved
                    _remember_delivered_output(agent_bridge, task, channel_type, content, agent_id)
                    logger.info(f"[Scheduler] Task {task['id']} executed via dispatcher: {channel_type}")
                    return True
                web_fallback_success = [
                    item for item in results
                    if item.get("success")
                    and not item.get("skipped")
                    and str(item.get("channel") or item.get("target") or "") == "web"
                ]
                if web_fallback_success:
                    task["_last_run_status"] = "failed"
                    task["_last_result_summary"] = f"{channel_type} 未送达；已写入 Web 消息提醒，请刷新原通道会话"
                    _remember_delivered_output(agent_bridge, task, "web", content, agent_id)
                    logger.info(
                        "[Scheduler] Task %s primary channel %s failed; Web fallback was saved",
                        task.get("id"),
                        channel_type,
                    )
                    return False
                deferred_success = [
                    item for item in results
                    if str(item.get("channel") or item.get("target") or "") == channel_type
                    and item.get("success")
                    and item.get("skipped")
                ]
                queued_failure = [
                    item for item in results
                    if str(item.get("channel") or item.get("target") or "") == channel_type
                    and not item.get("success")
                    and "待发送队列" in str(item.get("message") or "")
                ]
                has_web_fallback = any(item.get("web_fallback") and item.get("success") for item in results)
                if (deferred_success or queued_failure) and has_web_fallback:
                    task["_last_run_status"] = "failed"
                    task["_last_result_summary"] = (
                        f"{channel_type} 未送达；已保留到 Web 消息提醒"
                        + ("，原通道恢复后可补发" if queued_failure else "")
                    )
                    _remember_delivered_output(agent_bridge, task, "web", content, agent_id)
                    logger.info(
                        "[Scheduler] Task %s deferred %s delivery; primary remained failed",
                        task.get("id"),
                        channel_type,
                    )
                    return False
                primary_failures = [
                    item for item in results
                    if str(item.get("channel") or item.get("target") or "") == channel_type
                    and not item.get("success")
                    and item.get("message")
                ]
                message = "；".join(str(item.get("message") or "") for item in primary_failures) or "通知通道未返回成功"
                if any(item.get("web_fallback") or item.get("fallback") for item in results):
                    message += "；已保底写入 Web 消息提醒，但目标微信未送达"
                task["_last_run_status"] = "failed"
                task["_last_result_summary"] = f"{channel_type} 发送失败：{message}"
                _store_scheduler_artifact(task, f"消息发送失败：{message}", status="failed", error=message)
                return False
            except Exception as error:
                logger.error(f"[Scheduler] Dispatcher delivery failed: {error}")
                task["_last_run_status"] = "failed"
                task["_last_result_summary"] = f"{channel_type} 发送失败：{error}"
                _store_scheduler_artifact(task, f"消息发送失败：{error}", status="failed", error=str(error))
                return False
        
        # Create context for sending message
        context = Context(ContextType.TEXT, content)
        context["receiver"] = receiver
        context["isgroup"] = is_group
        context["session_id"] = delivery_session_id
        context["agent_id"] = agent_id
        
        # Channel-specific context setup
        if channel_type == "web":
            # Web channel needs request_id
            import uuid
            request_id = f"scheduler_{task['id']}_{uuid.uuid4().hex[:8]}"
            context["request_id"] = request_id
            logger.debug(f"[Scheduler] Generated request_id for web channel: {request_id}")
        elif channel_type == "feishu":
            # Feishu channel: for scheduled tasks, send as new message (no msg_id to reply to)
            # Use chat_id for groups, open_id for private chats
            context["receive_id_type"] = "chat_id" if is_group else "open_id"
            # Keep isgroup as is, but set msg to None (no original message to reply to)
            # Feishu channel will detect this and send as new message instead of reply
            context["msg"] = None
            logger.debug(f"[Scheduler] Feishu: receive_id_type={context['receive_id_type']}, is_group={is_group}, receiver={receiver}")
        elif channel_type == "dingtalk":
            # DingTalk channel setup
            context["msg"] = None
            # 如果是单聊，需要传递 sender_staff_id
            if not is_group:
                sender_staff_id = action.get("dingtalk_sender_staff_id")
                if sender_staff_id:
                    context["dingtalk_sender_staff_id"] = sender_staff_id
                    logger.debug(f"[Scheduler] DingTalk single chat: sender_staff_id={sender_staff_id}")
                else:
                    logger.warning(f"[Scheduler] Task {task['id']}: DingTalk single chat message missing sender_staff_id")
        elif channel_type == "wecom_bot":
            context["msg"] = None
        elif channel_type == "qq":
            context["msg"] = None
        elif channel_type == "webhook":
            context["webhook_url"] = action.get("webhook_url", "")

        if not _send_scheduler_output(task, agent_bridge, context, content or "提醒内容为空", report_path, "empty" if not content else "success", agent_id):
            _store_scheduler_artifact(task, "消息发送失败，详情请查看通知记录。", status="failed", error="delivery failed")
            return False

        logger.info(f"[Scheduler] Task {task['id']} executed: sent message to {receiver}")
        return True

    except Exception as e:
        logger.error(f"[Scheduler] Error in _execute_send_message: {e}")
        task["_last_run_status"] = "failed"
        task["_last_result_summary"] = f"定时提醒执行失败：{e}"
        _store_scheduler_artifact(task, f"定时提醒执行失败：{e}", status="failed", error=str(e))
        import traceback
        logger.error(f"[Scheduler] Traceback: {traceback.format_exc()}")
        return False


def _execute_tool_call(task: dict, agent_bridge, agent_id: str = None) -> bool:
    """Execute a tool_call action. Returns True/False for delivery."""
    try:
        action = task.get("action", {})
        tool_name = action.get("call_name") or action.get("tool_name")
        tool_params = action.get("call_params") or action.get("tool_params", {})
        result_prefix = action.get("result_prefix", "")
        receiver = action.get("receiver")
        is_group = action.get("is_group", False)
        channel_type = action.get("channel_type", "unknown")
        delivery_session_id = _delivery_session_id(action)

        if not tool_name:
            logger.error(f"[Scheduler] Task {task['id']}: No tool_name specified")
            _store_scheduler_artifact(task, "缺少工具名称，任务未执行。", status="failed", error="No tool_name specified")
            return True
        if not receiver:
            logger.error(f"[Scheduler] Task {task['id']}: No receiver specified")
            _store_scheduler_artifact(task, "缺少消息接收人，任务未发送。", status="failed", error="No receiver specified")
            return True

        from agent.tools.tool_manager import ToolManager
        tool = ToolManager().create_tool(tool_name)
        if not tool:
            logger.error(f"[Scheduler] Task {task['id']}: Tool '{tool_name}' not found")
            _store_scheduler_artifact(task, f"工具“{tool_name}”不存在，任务未执行。", status="failed", error=f"Tool '{tool_name}' not found")
            return True

        logger.info(f"[Scheduler] Task {task['id']}: Executing tool '{tool_name}' with params {tool_params}")
        result = tool.execute(tool_params)
        content = result.result if hasattr(result, 'result') else str(result)
        if result_prefix:
            content = f"{result_prefix}\n\n{content}"

        context = Context(ContextType.TEXT, content)
        context["receiver"] = receiver
        context["isgroup"] = is_group
        context["session_id"] = delivery_session_id
        context["agent_id"] = agent_id

        request_id = None
        if channel_type == "web":
            import uuid
            request_id = f"scheduler_{task['id']}_{uuid.uuid4().hex[:8]}"
            context["request_id"] = request_id
        elif channel_type == "feishu":
            context["receive_id_type"] = "chat_id" if is_group else "open_id"
            context["msg"] = None
        elif channel_type == "wecom_bot":
            context["msg"] = None

        reply = Reply(ReplyType.TEXT, content)
        report_path = _store_scheduler_artifact(task, content or "工具执行未返回内容", status="empty" if not content else "success")

        if not _send_scheduler_output(task, agent_bridge, context, content or "工具执行未返回内容", report_path, "empty" if not content else "success", agent_id):
            _store_scheduler_artifact(task, "工具结果发送失败，详情请查看通知记录。", status="failed", error="delivery failed")
            return False

        logger.info(f"[Scheduler] Task {task['id']} executed: sent tool result to {receiver}")
        return True

    except Exception as e:
        logger.error(f"[Scheduler] Error in _execute_tool_call: {e}")
        _store_scheduler_artifact(task, f"工具任务执行失败：{e}", status="failed", error=str(e))
        return False


def _execute_skill_call(task: dict, agent_bridge, agent_id: str = None) -> bool:
    """Execute a skill_call action by asking Agent to run the skill.
    Returns True/False for delivery."""
    try:
        action = task.get("action", {})
        skill_name = action.get("call_name") or action.get("skill_name")
        skill_params = action.get("call_params") or action.get("skill_params", {})
        result_prefix = action.get("result_prefix", "")
        receiver = action.get("receiver")
        is_group = action.get("isgroup", False)
        channel_type = action.get("channel_type", "unknown")
        delivery_session_id = _delivery_session_id(action)

        if not skill_name:
            logger.error(f"[Scheduler] Task {task['id']}: No skill_name specified")
            _store_scheduler_artifact(task, "缺少技能名称，任务未执行。", status="failed", error="No skill_name specified")
            return True
        if not receiver:
            logger.error(f"[Scheduler] Task {task['id']}: No receiver specified")
            _store_scheduler_artifact(task, "缺少消息接收人，任务未发送。", status="failed", error="No receiver specified")
            return True

        logger.info(f"[Scheduler] Task {task['id']}: Executing skill '{skill_name}' with params {skill_params}")

        scheduler_session_id = f"scheduler_{receiver}_{task['id']}"
        param_str = ", ".join([f"{k}={v}" for k, v in skill_params.items()])
        query = f"Use {skill_name} skill"
        if param_str:
            query += f" with {param_str}"

        context = Context(ContextType.TEXT, query)
        context["receiver"] = receiver
        context["isgroup"] = is_group
        context["session_id"] = scheduler_session_id
        context["agent_id"] = agent_id

        if channel_type == "web":
            import uuid
            request_id = f"scheduler_{task['id']}_{uuid.uuid4().hex[:8]}"
            context["request_id"] = request_id
        elif channel_type == "feishu":
            context["receive_id_type"] = "chat_id" if is_group else "open_id"
            context["msg"] = None
        elif channel_type == "wecom_bot":
            context["msg"] = None

        try:
            reply = agent_bridge.agent_reply(query, context=context, on_event=None, clear_history=False)
        except Exception as e:
            logger.error(f"[Scheduler] Failed to execute skill via Agent: {e}")
            _store_scheduler_artifact(task, f"技能任务执行失败：{e}", status="failed", error=str(e))
            import traceback
            logger.error(f"[Scheduler] Traceback: {traceback.format_exc()}")
            return False

        if not (reply and reply.content):
            logger.error(f"[Scheduler] Task {task['id']}: No result from skill execution")
            report_path = _store_scheduler_artifact(task, "技能执行未返回内容", status="empty")
            if channel_type == "web":
                _store_scheduled_inbox(task, "技能执行未返回内容", status="empty", report_path=report_path)
            return True

        content = reply.content
        if result_prefix:
            content = f"{result_prefix}\n\n{content}"

        report_path = _store_scheduler_artifact(task, content)
        if not _send_scheduler_output(task, agent_bridge, context, content, report_path, "success", agent_id):
            _store_scheduler_artifact(task, "技能结果发送失败，详情请查看通知记录。", status="failed", error="delivery failed")
            return False

        logger.info(f"[Scheduler] Task {task['id']} executed: skill result sent to {receiver}")
        return True

    except Exception as e:
        logger.error(f"[Scheduler] Error in _execute_skill_call: {e}")
        _store_scheduler_artifact(task, f"技能任务执行失败：{e}", status="failed", error=str(e))
        import traceback
        logger.error(f"[Scheduler] Traceback: {traceback.format_exc()}")
        return False


def attach_scheduler_to_tool(tool, context: Context = None):
    """
    Attach scheduler components to a SchedulerTool instance
    
    Args:
        tool: SchedulerTool instance
        context: Current context (optional)
    """
    if context:
        agent_id = context.get("agent_id")
        task_store = get_task_store(agent_id=agent_id)
        scheduler_service = get_scheduler_service(agent_id=agent_id)
        if task_store:
            tool.task_store = task_store
        if scheduler_service:
            tool.scheduler_service = scheduler_service
        tool.current_context = context
        
        channel_type = context.get("channel_type") or conf().get("channel_type", "unknown")
        if not tool.config:
            tool.config = {}
        tool.config["channel_type"] = channel_type
