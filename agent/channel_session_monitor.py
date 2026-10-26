# encoding:utf-8
"""Monitor chat-channel sessions and notify when a login/session expires.

This monitor is intentionally small and conservative:
- only watches channels the user has enabled/configured;
- notifies once for the same failure signature;
- resets the signature after recovery, so a new failure is reported again;
- always includes Web inbox as a fallback destination.
"""

import json
import os
import threading
import time
from datetime import datetime

from common.log import logger
from config import get_data_root


_STARTED = False
_LOCK = threading.Lock()
_FAILURE_NOTIFY_THRESHOLD = 1
_RECOVERY_CONFIRM_THRESHOLD = 2
_ALERT_COOLDOWN_SECONDS = 24 * 60 * 60
_RECONNECT_COOLDOWN_SECONDS = 3 * 60


def start_channel_session_monitor(channel_manager, interval_seconds: int = 300, initial_delay: int = 90) -> None:
    """Start a daemon monitor for Weixin / WeCom session health."""
    global _STARTED
    with _LOCK:
        if _STARTED:
            return
        _STARTED = True

    thread = threading.Thread(
        target=_monitor_loop,
        args=(channel_manager, max(60, int(interval_seconds or 300)), max(10, int(initial_delay or 90))),
        name="channel-session-monitor",
        daemon=True,
    )
    thread.start()
    logger.info("[ChannelSessionMonitor] started")


def _monitor_loop(channel_manager, interval_seconds: int, initial_delay: int) -> None:
    time.sleep(initial_delay)
    while True:
        try:
            _check_once(channel_manager)
        except Exception as error:
            logger.warning("[ChannelSessionMonitor] check failed: %s", error)
        time.sleep(interval_seconds)


def _state_path() -> str:
    root = os.path.join(get_data_root(), "intelligence_center")
    os.makedirs(root, exist_ok=True)
    return os.path.join(root, "channel_session_monitor.json")


def _read_state() -> dict:
    try:
        with open(_state_path(), "r", encoding="utf-8") as handle:
            data = json.load(handle)
            return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _write_state(state: dict) -> None:
    path = _state_path()
    tmp = f"{path}.tmp"
    with open(tmp, "w", encoding="utf-8") as handle:
        json.dump(state, handle, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


def _check_once(channel_manager) -> None:
    from channel.web.web_channel import ChannelHealthHandler
    from plugins.intelligence_center.notifications import NotificationDispatcher

    watched = ["weixin", "wecom_bot"]
    health = {name: ChannelHealthHandler._channel_health(name) for name in watched}
    state = _read_state()
    changed = False

    for name, item in health.items():
        if not _should_watch(item):
            changed = _mark_recovered_if_needed(state, name) or changed
            continue
        if item.get("ok"):
            try:
                NotificationDispatcher.process_pending_deliveries(name, limit=5)
            except Exception as error:
                logger.warning("[ChannelSessionMonitor] pending retry failed for %s: %s", name, error)
            changed = _mark_healthy(state, name) or changed
            continue
        slot = state.get(name) if isinstance(state.get(name), dict) else {}
        slot["healthy_count"] = 0
        slot["failure_count"] = int(slot.get("failure_count") or 0) + 1
        slot["last_seen_unhealthy_at"] = datetime.now().isoformat(timespec="seconds")
        slot["last_message"] = item.get("message") or slot.get("last_message") or ""
        state[name] = slot
        changed = True
        _maybe_reconnect(name, item, slot, channel_manager)
        try:
            refreshed = ChannelHealthHandler._channel_health(name) or {}
            if refreshed.get("ok"):
                try:
                    NotificationDispatcher.process_pending_deliveries(name, limit=10)
                except Exception as error:
                    logger.warning("[ChannelSessionMonitor] pending retry after reconnect failed for %s: %s", name, error)
                changed = _mark_healthy(state, name) or changed
                continue
        except Exception:
            pass
        if slot["failure_count"] < _FAILURE_NOTIFY_THRESHOLD:
            continue
        now_ts = time.time()
        last_alert_ts = float(slot.get("last_alert_ts") or 0)
        signature = _issue_signature(item)
        if _is_issue_in_cooldown(state, name, signature, now_ts):
            state[name] = slot
            continue
        _notify_issue(name, item, health)
        _remember_issue_alert(state, name, signature, now_ts)
        state[name] = {
            **slot,
            "active_signature": signature,
            "last_alert_at": datetime.now().isoformat(timespec="seconds"),
            "last_alert_ts": now_ts,
            "last_message": item.get("message") or "",
            "level": item.get("level") or "warning",
        }
        changed = True

    if changed:
        _write_state(state)


def _should_watch(item: dict) -> bool:
    if not item:
        return False
    # Avoid nagging for channels the user never enabled/configured.
    return bool(item.get("active") or item.get("configured") or item.get("receiver_hint"))


def _mark_recovered_if_needed(state: dict, name: str) -> bool:
    slot = state.get(name)
    if not isinstance(slot, dict) or not slot.get("active_signature"):
        return False
    slot["active_signature"] = ""
    slot["recovered_at"] = datetime.now().isoformat(timespec="seconds")
    state[name] = slot
    return True


def _mark_healthy(state: dict, name: str) -> bool:
    slot = state.get(name) if isinstance(state.get(name), dict) else {}
    slot["failure_count"] = 0
    slot["healthy_count"] = int(slot.get("healthy_count") or 0) + 1
    # Avoid clearing an active incident on a single transient OK sample.
    if slot.get("active_signature") and slot["healthy_count"] < _RECOVERY_CONFIRM_THRESHOLD:
        state[name] = slot
        return True
    if slot.get("active_signature"):
        slot["active_signature"] = ""
        slot["recovered_at"] = datetime.now().isoformat(timespec="seconds")
    state[name] = slot
    return True


def _maybe_reconnect(channel_name: str, item: dict, slot: dict, channel_manager) -> None:
    runtime = item.get("runtime") or {}
    state = str(runtime.get("state") or "")
    reconnectable = {
        "wecom_bot": {"reconnecting", "error", "subscribe_failed", "stale", "takeover", "unknown"},
        "weixin": {"idle", "waiting_scan", "scanned", "stale", "error", "unknown"},
    }
    if state not in reconnectable.get(channel_name, set()):
        return
    now_ts = time.time()
    last_reconnect_ts = float(slot.get("last_reconnect_ts") or 0)
    if now_ts - last_reconnect_ts < _RECONNECT_COOLDOWN_SECONDS:
        return
    try:
        ch = channel_manager.get_channel(channel_name) if channel_manager else None
        if ch and hasattr(ch, "reconnect_now"):
            ch.reconnect_now(f"auto health recovery: {item.get('message') or state}")
            slot["last_reconnect_ts"] = now_ts
            slot["last_reconnect_at"] = datetime.now().isoformat(timespec="seconds")
            logger.warning("[ChannelSessionMonitor] auto reconnect requested for %s: %s", channel_name, item.get("message") or state)
    except Exception as error:
        logger.warning("[ChannelSessionMonitor] auto reconnect failed for %s: %s", channel_name, error)


def _issue_signature(item: dict) -> str:
    runtime = item.get("runtime") or {}
    parts = [
        str(item.get("channel") or ""),
        str(item.get("level") or ""),
        str(item.get("message") or ""),
        str(item.get("token_ready") if "token_ready" in item else ""),
        str(runtime.get("state") or ""),
        str(runtime.get("last_error") or ""),
    ]
    return "|".join(parts)[:500]


def _issue_key(channel_name: str, signature: str) -> str:
    # Keep the key stable for the same user-visible incident, even if the
    # runtime's detailed last_error text jitters between checks.
    normalized = str(signature or "")
    normalized = normalized.replace("正在自动重连", "需处理")
    normalized = normalized.replace("待刷新", "需处理")
    return f"{channel_name}:{normalized}"[:500]


def _is_issue_in_cooldown(state: dict, channel_name: str, signature: str, now_ts: float) -> bool:
    key = _issue_key(channel_name, signature)
    cooldowns = state.get("issue_cooldowns") if isinstance(state.get("issue_cooldowns"), dict) else {}
    last_ts = float((cooldowns.get(key) or {}).get("last_alert_ts") or 0)
    return bool(last_ts and now_ts - last_ts < _ALERT_COOLDOWN_SECONDS)


def _remember_issue_alert(state: dict, channel_name: str, signature: str, now_ts: float) -> None:
    cooldowns = state.get("issue_cooldowns") if isinstance(state.get("issue_cooldowns"), dict) else {}
    state["issue_cooldowns"] = cooldowns
    # Drop stale cooldown entries so the state file stays small.
    for key, value in list(cooldowns.items()):
        try:
            if now_ts - float((value or {}).get("last_alert_ts") or 0) > 7 * 24 * 60 * 60:
                cooldowns.pop(key, None)
        except Exception:
            cooldowns.pop(key, None)
    cooldowns[_issue_key(channel_name, signature)] = {
        "last_alert_ts": now_ts,
        "last_alert_at": datetime.now().isoformat(timespec="seconds"),
        "channel": channel_name,
    }


def _notify_issue(channel_name: str, item: dict, all_health: dict) -> None:
    from plugins.intelligence_center.notifications import NotificationDispatcher

    label = item.get("label") or ("微信" if channel_name == "weixin" else "企业微信")
    message = item.get("message") or "通道会话异常"
    hint = item.get("receiver_hint") or ""
    report = (
        f"⚠️ {label}会话需要处理\n\n"
        f"- 状态：{message}\n"
        f"- 影响：该通道可能无法继续接收或推送定时任务通知。\n"
        f"- 建议：打开 CowAgent 设置 → 通道与连接，重新扫码/发送一条消息刷新会话。\n"
        f"{('- 详情：' + hint) if hint else ''}"
    )
    task = {
        "id": f"channel-session-{channel_name}",
        "name": f"{label}会话过期提醒",
        "notifications": _notification_targets(channel_name, all_health),
    }
    results = NotificationDispatcher().dispatch(task, report)
    logger.warning("[ChannelSessionMonitor] notified %s issue: %s results=%s", channel_name, message, results)


def _notification_targets(issue_channel: str, all_health: dict) -> list:
    targets = [{"type": "channel", "channel": "web", "receiver": "web_user"}]
    # Cross-notify through the other chat channel when it is healthy.
    if issue_channel != "wecom_bot" and (all_health.get("wecom_bot") or {}).get("ok"):
        targets.insert(0, {"type": "channel", "channel": "wecom_bot", "receiver": ""})
    if issue_channel != "weixin" and (all_health.get("weixin") or {}).get("ok"):
        targets.insert(0, {"type": "channel", "channel": "weixin", "receiver": ""})
    return targets
