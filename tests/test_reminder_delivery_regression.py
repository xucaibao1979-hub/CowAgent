"""Regression coverage for failed Weixin reminders and stale one-off reminders."""

from datetime import datetime, timedelta
from unittest.mock import patch

from agent.tools.scheduler.integration import _execute_send_message
from plugins.intelligence_center.personal_productivity import PersonalProductivityService
from plugins.intelligence_center.notifications import NotificationDispatcher


def _scheduled_message(receiver="someone@im.wechat"):
    return {
        "id": "reminder-test",
        "name": "喝水提醒",
        "action": {
            "type": "send_message",
            "channel_type": "weixin",
            "receiver": receiver,
            "content": "请喝水",
        },
    }


def test_web_fallback_does_not_mark_weixin_reminder_delivered():
    task = _scheduled_message()
    results = [
        {"channel": "weixin", "success": False, "message": "没有可用发送令牌"},
        {"channel": "web", "success": True, "web_fallback": True},
    ]
    with patch("agent.tools.scheduler.integration._get_running_channel", return_value=None), \
         patch("agent.tools.scheduler.integration._resolve_outbound_receiver", return_value=task["action"]["receiver"]), \
         patch("agent.tools.scheduler.integration._store_scheduler_artifact", return_value=""), \
         patch("agent.tools.scheduler.integration._remember_delivered_output"), \
         patch("plugins.intelligence_center.notifications.NotificationDispatcher.dispatch", return_value=results):
        assert _execute_send_message(task, None) is False
    assert task["_last_run_status"] == "failed"
    assert "未送达" in task["_last_result_summary"]


def test_missing_receiver_is_not_success():
    task = _scheduled_message(receiver="")
    with patch("agent.tools.scheduler.integration._resolve_outbound_receiver", return_value=""), \
         patch("agent.tools.scheduler.integration._store_scheduler_artifact", return_value=""):
        assert _execute_send_message(task, None) is False
    assert task["_last_run_status"] == "failed"


def test_weixin_fallback_does_not_reuse_weekly_alias_of_expired_receiver():
    class Channel:
        _context_tokens = {
            "someone@im.wechat#week:2026-W41": "old-token",
            "another@im.wechat": "current-token",
        }

        @staticmethod
        def context_token_status(receiver):
            return {"ready": receiver == "another@im.wechat"}

    assert NotificationDispatcher._default_weixin_receiver(
        Channel(), exclude="someone@im.wechat"
    ) == "another@im.wechat"


def test_weixin_fallback_does_not_guess_from_historical_session():
    class Channel:
        _context_tokens = {}

        @staticmethod
        def has_context_token(receiver):
            return False

    with patch("channel.channel_access_service.get_channel_access_service") as access, \
         patch("agent.memory.get_conversation_store") as get_store:
        access.return_value.list.return_value = {"entries": []}
        get_store.return_value.list_sessions.return_value = {
            "sessions": [{"session_id": "someone@im.wechat#week:2026-W41"}]
        }
        assert NotificationDispatcher._default_weixin_receiver(Channel()) == ""


def test_personal_reminder_web_fallback_is_failed_and_not_replayed(tmp_path):
    service = PersonalProductivityService(".")
    data = {"version": 1, "items": []}
    results = [
        {"channel": "weixin", "success": False},
        {"channel": "web", "success": True, "web_fallback": True},
    ]
    with patch.object(service, "_read", return_value=data), \
         patch.object(service, "_write"), \
         patch("plugins.intelligence_center.personal_productivity.NotificationDispatcher.dispatch", return_value=results):
        reminder = service.create_reminder({
            "content": "喝水",
            "due_at": (datetime.now().astimezone() + timedelta(minutes=1)).isoformat(),
            "source_channel": "weixin",
            "source_receiver": "someone@im.wechat",
        })
        run = service.run(reminder["id"], "scheduled")
        saved = service.list_reminders()[0]
    assert run["status"] == "failed"
    assert saved["status"] == "failed"
    assert saved["enabled"] is False


def test_expired_personal_reminder_is_not_sent(tmp_path):
    service = PersonalProductivityService(".")
    data = {"version": 1, "items": []}
    with patch.object(service, "_read", return_value=data), \
         patch.object(service, "_write"), \
         patch("plugins.intelligence_center.personal_productivity.NotificationDispatcher.dispatch") as dispatch:
        reminder = service.create_reminder({
            "content": "喝水",
            "due_at": (datetime.now().astimezone() - timedelta(hours=2)).isoformat(),
            "source_channel": "weixin",
            "source_receiver": "someone@im.wechat",
        })
        run = service.run(reminder["id"], "scheduled")
        saved = service.list_reminders()[0]
    dispatch.assert_not_called()
    assert run["status"] == "skipped"
    assert saved["status"] == "expired"
