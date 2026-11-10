from datetime import date
import unittest
from unittest.mock import Mock, patch

import requests

from agent.tools.weather.weather import WeatherTool
from plugins.intelligence_center.notifications import NotificationDispatcher


def _payload():
    today = date.today().isoformat()
    return {
        "daily": {
            "time": [today],
            "weather_code": [61],
            "temperature_2m_max": [20],
            "temperature_2m_min": [15],
            "apparent_temperature_max": [19],
            "apparent_temperature_min": [14],
            "precipitation_probability_max": [75],
            "precipitation_sum": [0.5],
            "wind_speed_10m_max": [12],
        },
        "hourly": {
            "time": [f"{today}T18:00", f"{today}T19:00"],
            "temperature_2m": [18.5, 17.3],
            "apparent_temperature": [18.0, 17.0],
            "precipitation_probability": [70, 55],
            "precipitation": [0.2, 0.1],
            "weather_code": [61, 3],
            "wind_speed_10m": [9, 8],
        },
    }


class WeatherHourlySchedulerTests(unittest.TestCase):
    def test_weather_reminder_retries_only_until_forecast_window_ends(self):
        from datetime import datetime, timedelta, timezone

        task = {
            "name": "上海天气提醒",
            "action": {"template": {"type": "weather_hourly_brief", "end_time": "08:30"}},
        }
        strategy = NotificationDispatcher._pending_strategy(task)
        deadline = datetime.fromisoformat(strategy["expires_at"])
        now = datetime.now(timezone(timedelta(hours=8)))
        self.assertTrue(strategy["allow_retry"])
        self.assertEqual(deadline.date(), now.date())
        self.assertEqual((deadline.hour, deadline.minute), (8, 30))
        self.assertFalse(NotificationDispatcher._pending_strategy({"name": "喝水提醒"})["allow_retry"])

    def test_weather_retry_respects_forecast_target_date(self):
        from datetime import datetime, timedelta, timezone

        now = datetime.now(timezone(timedelta(hours=8)))
        target_date = (now + timedelta(days=1)).date().isoformat()
        task = {"name": "明日天气", "action": {
            "template": {"type": "weather_hourly_brief", "end_time": "08:30"},
            "_forecast_target_date": target_date,
        }}
        deadline = datetime.fromisoformat(NotificationDispatcher._pending_strategy(task)["expires_at"])
        self.assertEqual(deadline.date().isoformat(), target_date)
        self.assertEqual((deadline.hour, deadline.minute), (8, 30))

    def test_expired_weather_is_not_queued_for_later_delivery(self):
        from datetime import datetime, timedelta, timezone

        yesterday = (datetime.now(timezone(timedelta(hours=8))) - timedelta(days=1)).date().isoformat()
        task = {"id": "weather-expired", "name": "过期天气", "action": {
            "template": {"type": "weather_hourly_brief", "end_time": "08:30"},
            "_forecast_target_date": yesterday,
        }}
        with patch("plugins.intelligence_center.notifications.append_execution_event") as audit:
            self.assertEqual(NotificationDispatcher.enqueue_pending_delivery(task, "weixin", "receiver", "天气内容"), {})
        self.assertEqual(audit.call_args.args[1], "pending_policy")

    def test_evening_weather_notification_uses_hourly_forecast(self):
        result = WeatherTool._select(_payload(), {"date": "today", "hour_start": 18, "hour_end": 19})
        result.update({"location": "浦东新区", "admin1": "上海市"})
        message = WeatherTool._display(result)
        self.assertIn("地区：上海市浦东新区", message)
        self.assertIn("18:00–19:00", message)
        self.assertIn("气温：17.3～18.5°C", message)
        self.assertIn("最高概率 70%", message)
        self.assertIn("出门带伞", message)
        self.assertNotIn("新闻", message)

    def test_hourly_weather_rejects_missing_hours(self):
        payload = _payload()
        payload["hourly"]["time"].pop()
        with self.assertRaisesRegex(ValueError, "完整"):
            WeatherTool._select(payload, {"date": "today", "hour_start": 18, "hour_end": 19})

    def test_forecast_timeout_retries_before_failing_task(self):
        response = Mock()
        response.json.return_value = _payload()
        with patch("agent.tools.weather.weather._cached", return_value=None), patch(
            "agent.tools.weather.weather._remember", side_effect=lambda cache, key, value: value
        ), patch("agent.tools.weather.weather._HTTP.get", side_effect=[requests.Timeout(), response]) as get:
            result = WeatherTool().execute({"location": "上海市浦东新区", "date": "today", "hour_start": 18, "hour_end": 19})
        self.assertEqual(result.status, "success")
        self.assertEqual(get.call_count, 2)


if __name__ == "__main__":
    unittest.main()
