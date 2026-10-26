"""Reliable no-key weather lookup with location and date validation."""
from __future__ import annotations

from datetime import date, timedelta
from typing import Any, Dict
import threading
import time

import requests

from agent.tools.base_tool import BaseTool, ToolResult


_WMO = {
    0: "晴", 1: "大部晴朗", 2: "多云", 3: "阴",
    45: "有雾", 48: "雾凇", 51: "小毛毛雨", 53: "毛毛雨", 55: "较强毛毛雨",
    56: "冻毛毛雨", 57: "较强冻毛毛雨", 61: "小雨", 63: "中雨", 65: "大雨",
    66: "冻雨", 67: "较强冻雨", 71: "小雪", 73: "中雪", 75: "大雪", 77: "米雪",
    80: "小阵雨", 81: "阵雨", 82: "强阵雨", 85: "小阵雪", 86: "强阵雪",
    95: "雷暴", 96: "雷暴伴小冰雹", 99: "雷暴伴强冰雹",
}

_HTTP = requests.Session()
_CACHE_LOCK = threading.RLock()
_GEO_CACHE = {}
_FORECAST_CACHE = {}
_GEO_TTL = 24 * 60 * 60
_FORECAST_TTL = 5 * 60

_KNOWN_PLACES = {
    "北京": {"name": "北京", "admin1": "北京", "country": "中国", "country_code": "CN", "latitude": 39.9042, "longitude": 116.4074},
    "上海": {"name": "上海", "admin1": "上海", "country": "中国", "country_code": "CN", "latitude": 31.2304, "longitude": 121.4737},
    "广州": {"name": "广州", "admin1": "广东", "country": "中国", "country_code": "CN", "latitude": 23.1291, "longitude": 113.2644},
    "深圳": {"name": "深圳", "admin1": "广东", "country": "中国", "country_code": "CN", "latitude": 22.5431, "longitude": 114.0579},
    "杭州": {"name": "杭州", "admin1": "浙江", "country": "中国", "country_code": "CN", "latitude": 30.2741, "longitude": 120.1551},
    "南京": {"name": "南京", "admin1": "江苏", "country": "中国", "country_code": "CN", "latitude": 32.0603, "longitude": 118.7969},
    "苏州": {"name": "苏州", "admin1": "江苏", "country": "中国", "country_code": "CN", "latitude": 31.2989, "longitude": 120.5853},
    "成都": {"name": "成都", "admin1": "四川", "country": "中国", "country_code": "CN", "latitude": 30.5728, "longitude": 104.0668},
    "重庆": {"name": "重庆", "admin1": "重庆", "country": "中国", "country_code": "CN", "latitude": 29.5630, "longitude": 106.5516},
    "武汉": {"name": "武汉", "admin1": "湖北", "country": "中国", "country_code": "CN", "latitude": 30.5928, "longitude": 114.3055},
    "西安": {"name": "西安", "admin1": "陕西", "country": "中国", "country_code": "CN", "latitude": 34.3416, "longitude": 108.9398},
    "天津": {"name": "天津", "admin1": "天津", "country": "中国", "country_code": "CN", "latitude": 39.3434, "longitude": 117.3616},
    "郑州": {"name": "郑州", "admin1": "河南", "country": "中国", "country_code": "CN", "latitude": 34.7466, "longitude": 113.6254},
    "长沙": {"name": "长沙", "admin1": "湖南", "country": "中国", "country_code": "CN", "latitude": 28.2282, "longitude": 112.9388},
    "青岛": {"name": "青岛", "admin1": "山东", "country": "中国", "country_code": "CN", "latitude": 36.0671, "longitude": 120.3826},
    "宁波": {"name": "宁波", "admin1": "浙江", "country": "中国", "country_code": "CN", "latitude": 29.8683, "longitude": 121.5440},
}


def _cached(cache, key, ttl):
    with _CACHE_LOCK:
        item = cache.get(key)
        if item and time.monotonic() - item[0] <= ttl:
            return item[1]
    return None


def _cached_stale(cache, key):
    with _CACHE_LOCK:
        item = cache.get(key)
        return item[1] if item else None


def _remember(cache, key, value):
    with _CACHE_LOCK:
        cache[key] = (time.monotonic(), value)
    return value


class WeatherTool(BaseTool):
    name = "weather"
    description = (
        "Get validated current or forecast weather for a city using a no-key weather service. "
        "Use this dedicated tool first for weather questions instead of fetching a skill's hard-coded API URL."
    )
    params = {
        "type": "object",
        "properties": {
            "location": {"type": "string", "description": "City or district name, e.g. 上海"},
            "date": {"type": "string", "description": "today, tomorrow, YYYY-MM-DD, or 未来/forecast (default tomorrow)"},
            "days": {"type": "integer", "description": "Forecast days for 未来/forecast, 1-7"},
        },
        "required": ["location"],
    }

    def execute(self, args: Dict[str, Any]) -> ToolResult:
        location = str(args.get("location") or "").strip()
        if not location:
            return ToolResult.fail("请提供城市或地区名称")
        try:
            geo_key = location.casefold()
            known = _KNOWN_PLACES.get(location)
            candidates = [known] if known else _cached(_GEO_CACHE, geo_key, _GEO_TTL)
            if candidates is None:
                geo = _HTTP.get(
                    "https://geocoding-api.open-meteo.com/v1/search",
                    params={"name": location, "count": 5, "language": "zh", "format": "json"},
                    timeout=3.5,
                )
                geo.raise_for_status()
                candidates = _remember(_GEO_CACHE, geo_key, geo.json().get("results") or [])
            if not candidates:
                return ToolResult.fail(f"未找到地点：{location}")
            # Prefer an exact localized name and then a Chinese result, while
            # retaining global support when the user asks for another country.
            place = sorted(candidates, key=lambda x: (
                str(x.get("name", "")).lower() != location.lower(),
                x.get("country_code") != "CN",
                -(int(x.get("population") or 0)),
            ))[0]
            forecast_key = (round(float(place["latitude"]), 4), round(float(place["longitude"]), 4))
            payload = _cached(_FORECAST_CACHE, forecast_key, _FORECAST_TTL)
            if payload is None:
                try:
                    forecast = _HTTP.get(
                        "https://api.open-meteo.com/v1/forecast",
                        params={
                            "latitude": place["latitude"], "longitude": place["longitude"],
                            "daily": "weather_code,temperature_2m_max,temperature_2m_min,apparent_temperature_max,apparent_temperature_min,precipitation_probability_max,precipitation_sum,wind_speed_10m_max",
                            "current": "temperature_2m,apparent_temperature,is_day,weather_code,wind_speed_10m",
                            "timezone": "auto", "forecast_days": 8,
                        },
                        timeout=4.5,
                    )
                    forecast.raise_for_status()
                    payload = _remember(_FORECAST_CACHE, forecast_key, forecast.json())
                except requests.RequestException:
                    payload = _cached_stale(_FORECAST_CACHE, forecast_key)
                    if payload is None:
                        raise
            result = self._select(payload, args)
            result.update({
                "location": place.get("name"), "admin1": place.get("admin1", ""),
                "country": place.get("country", ""), "timezone": payload.get("timezone", ""),
                "source": "Open-Meteo", "source_url": "https://open-meteo.com/",
            })
            return ToolResult.success(result, display=self._display(result))
        except requests.Timeout:
            return ToolResult.fail("天气服务请求超时，请改用网络搜索核验")
        except (requests.RequestException, ValueError, KeyError, TypeError) as exc:
            return ToolResult.fail(f"天气服务暂时不可用：{exc}；请改用网络搜索核验")

    @staticmethod
    def _select(payload: dict, args: dict) -> dict:
        wanted = str(args.get("date") or "tomorrow").strip().lower()
        today = date.today()
        if wanted in ("today", "今天"):
            target = today
        elif wanted in ("tomorrow", "明天", ""):
            target = today + timedelta(days=1)
        elif wanted in ("未来", "forecast", "未来几天"):
            target = None
        else:
            target = date.fromisoformat(wanted)
        daily = payload.get("daily") or {}
        dates = daily.get("time") or []
        indices = list(range(min(len(dates), max(1, min(int(args.get("days") or 3), 7))))) if target is None else [dates.index(target.isoformat())]
        rows = []
        for i in indices:
            rows.append({
                "date": dates[i], "weather": _WMO.get(int(daily["weather_code"][i]), f"天气代码 {daily['weather_code'][i]}"),
                "temperature_min": daily["temperature_2m_min"][i], "temperature_max": daily["temperature_2m_max"][i],
                "apparent_min": daily["apparent_temperature_min"][i], "apparent_max": daily["apparent_temperature_max"][i],
                "precipitation_probability": daily["precipitation_probability_max"][i],
                "precipitation_sum": daily["precipitation_sum"][i], "wind_max": daily["wind_speed_10m_max"][i],
            })
        return {"forecast": rows, "requested_date": wanted}

    @staticmethod
    def _display(result: dict) -> str:
        place = " · ".join(x for x in (result.get("location"), result.get("admin1"), result.get("country")) if x)
        lines = [f"{place} 天气预报（{result.get('timezone')}）"]
        for row in result["forecast"]:
            lines.append(
                f"{row['date']}：{row['weather']}，{row['temperature_min']}～{row['temperature_max']}°C，"
                f"体感 {row['apparent_min']}～{row['apparent_max']}°C，降水概率 {row['precipitation_probability']}%，"
                f"预计降水 {row['precipitation_sum']} mm，最大风速 {row['wind_max']} km/h"
            )
        lines.append("数据来源：Open-Meteo（预报可能变化，请以临近时段为准）")
        return "\n".join(lines)
