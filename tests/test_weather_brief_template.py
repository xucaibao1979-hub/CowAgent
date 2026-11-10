import unittest

from agent.tools.weather.weather import WeatherTool


class WeatherBriefTemplateTests(unittest.TestCase):
    def test_fixed_hourly_message(self):
        result = {
            "hour_range": [18, 19],
            "hourly": [
                {"temperature": 18.7, "weather": "小雨", "precipitation_probability": 72, "precipitation": 0.1, "wind_speed": 8},
                {"temperature": 18.0, "weather": "多云", "precipitation_probability": 40, "precipitation": 0.0, "wind_speed": 7},
            ],
        }
        self.assertEqual(
            WeatherTool.format_hourly_brief(result, location="上海市浦东新区"),
            "**今日18:00–19:00｜上海浦东新区**：18.0–18.7℃，小雨转多云，降雨概率最高72%（预计0.1毫米）。建议带伞，无需特别保暖或防风。",
        )

    def test_missing_hourly_data_is_not_invented(self):
        with self.assertRaisesRegex(ValueError, "逐小时预报"):
            WeatherTool.format_hourly_brief({"hour_range": [18, 19], "hourly": []})

    def test_half_hour_window_keeps_requested_time_and_weights_rain(self):
        rows = [
            {"time": "2026-11-05T07:00", "temperature": 18, "weather": "小雨", "precipitation_probability": 72, "precipitation": 0.2, "wind_speed": 8},
            {"time": "2026-11-05T08:00", "temperature": 19, "weather": "多云", "precipitation_probability": 40, "precipitation": 0, "wind_speed": 7},
            {"time": "2026-11-05T09:00", "temperature": 20, "weather": "晴", "precipitation_probability": 0, "precipitation": 0, "wind_speed": 7},
        ]
        message = WeatherTool.format_hourly_brief(
            {"hour_range": [7, 9], "hourly": rows}, location="上海市浦东新区",
            start_time="07:30", end_time="08:30",
        )
        self.assertEqual(message, "**今日07:30–08:30｜上海浦东新区**：18.0–20.0℃，小雨转多云，降雨概率最高72%（预计0.1毫米）。建议带伞，无需特别保暖或防风。")

    def test_dry_cool_morning_has_actionable_advice(self):
        rows = [
            {"time": "2026-11-05T07:00", "temperature": 14.8, "weather": "阴", "precipitation_probability": 0, "precipitation": 0, "wind_speed": 8},
            {"time": "2026-11-05T08:00", "temperature": 15.6, "weather": "阴", "precipitation_probability": 0, "precipitation": 0, "wind_speed": 7},
            {"time": "2026-11-05T09:00", "temperature": 16.2, "weather": "阴", "precipitation_probability": 0, "precipitation": 0, "wind_speed": 7},
        ]
        message = WeatherTool.format_hourly_brief(
            {"hour_range": [7, 9], "hourly": rows}, location="上海市浦东新区",
            today=False, start_time="07:30", end_time="08:30",
        )
        self.assertEqual(message, "**明日07:30–08:30｜上海浦东新区**：14.8–16.2℃，阴，降雨概率最高0%（预计0.0毫米）。建议照常出行，可不带伞，气温偏凉可穿薄外套。")


if __name__ == "__main__":
    unittest.main()
