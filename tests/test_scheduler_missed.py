from datetime import datetime
from unittest.mock import patch

from agent.tools.scheduler.scheduler_service import SchedulerService
from agent.tools.scheduler.task_store import TaskStore
from intelligence_center.inbox_service import InboxStore


def test_overdue_weather_run_is_recorded_and_visible(tmp_path):
    store = TaskStore(str(tmp_path / "scheduler" / "tasks.json"))
    store.add_task({
        "id": "morning-weather",
        "name": "早上天气提醒",
        "enabled": True,
        "schedule": {"type": "cron", "expression": "29 7 * * 1-5"},
        "action": {"type": "tool_call"},
        "next_run_at": "2026-11-06T07:29:00",
    })
    service = SchedulerService(store, lambda task: True)
    with patch("config.get_data_root", return_value=str(tmp_path)):
        assert service._is_task_due(store.get_task("morning-weather"), datetime(2026, 11, 6, 8, 37)) is False

    task = store.get_task("morning-weather")
    assert task["last_run_status"] == "missed"
    assert task["next_run_at"] == "2026-11-09T07:29:00"
    assert task["run_history"][0]["status"] == "missed"
    inbox = InboxStore(tmp_path / "intelligence_center").list()
    assert any("错过执行" in item["title"] for item in inbox["items"])
