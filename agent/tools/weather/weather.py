"""Reliable no-key weather lookup with location and date validation."""
from __future__ import annotations

from datetime import date, timedelta
from typing import Any, Dict
import threading
import time

import requests
from common.log import logger

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
    "浦东新区": {"name": "浦东新区", "admin1": "上海市", "country": "中国", "country_code": "CN", "latitude": 31.2211, "longitude": 121.5441},
    "上海浦东新区": {"name": "浦东新区", "admin1": "上海市", "country": "中国", "country_code": "CN", "latitude": 31.2211, "longitude": 121.5441},
    "上海市浦东新区": {"name": "浦东新区", "admin1": "上海市", "country": "中国", "country_code": "CN", "latitude": 31.2211, "longitude": 121.5441},
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


def _place_queries(location: str):
    """Try the exact place first, then the district name accepted by geocoders."""
    queries = [location]
    district = location
    for separator in ("市", "省", "自治区", "自治州"):
        if separator in district:
            district = district.rsplit(separator, 1)[-1]
    if district and district not in queries:
        queries.append(district)
    for suffix in ("新区", "自治县", "区", "县", "市"):
        if district.endswith(suffix) and len(district) > len(suffix) + 1:
            short = district[:-len(suffix)]
            if short not in queries:
                queries.append(short)
            break
    return queries


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
            "hour_start": {"type": "integer", "description": "First local forecast hour, 0-23 (inclusive)"},
            "hour_end": {"type": "integer", "description": "Last local forecast hour, 0-23 (inclusive)"},
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
                candidates = []
                for query in _place_queries(location):
                    geo = _HTTP.get(
                        "https://geocoding-api.open-meteo.com/v1/search",
                        params={"name": query, "count": 10, "language": "zh", "format": "json"},
                        timeout=3.5,
                    )
                    geo.raise_for_status()
                    candidates = geo.json().get("results") or []
                    if candidates:
                        break
                _remember(_GEO_CACHE, geo_key, candidates)
            if not candidates:
                return ToolResult.fail(f"未找到地点：{location}")
            # Prefer an exact localized name and then a Chinese result, while
            # retaining global support when the user asks for another country.
            place = sorted(candidates, key=lambda x: (
                x.get("country_code") != "CN" if any('\u4e00' <= ch <= '\u9fff' for ch in location) else False,
                bool(x.get("admin1")) and str(x.get("admin1")) not in location,
                bool(x.get("admin2")) and str(x.get("admin2")) not in location,
                str(x.get("name", "")).lower() not in location.lower(),
                -(int(x.get("population") or 0)),
            ))[0]
            forecast_key = (round(float(place["latitude"]), 4), round(float(place["longitude"]), 4))
            payload = _cached(_FORECAST_CACHE, forecast_key, _FORECAST_TTL)
            wants_hourly = "hour_start" in args or "hour_end" in args
            if payload is None or (wants_hourly and not payload.get("hourly")):
                forecast_params = {
                    "latitude": place["latitude"], "longitude": place["longitude"],
                    "daily": "weather_code,temperature_2m_max,temperature_2m_min,apparent_temperature_max,apparent_temperature_min,precipitation_probability_max,precipitation_sum,wind_speed_10m_max",
                    "hourly": "temperature_2m,apparent_temperature,precipitation_probability,precipitation,weather_code,wind_speed_10m",
                    "current": "temperature_2m,apparent_temperature,is_day,weather_code,wind_speed_10m",
                    "timezone": "auto", "forecast_days": 8,
                }
                last_error = None
                for attempt, timeout in enumerate((5, 10), start=1):
                    try:
                        forecast = _HTTP.get(
                            "https://api.open-meteo.com/v1/forecast",
                            params=forecast_params, timeout=timeout,
                        )
                        forecast.raise_for_status()
                        payload = _remember(_FORECAST_CACHE, forecast_key, forecast.json())
                        break
                    except requests.RequestException as exc:
                        last_error = exc
                        logger.warning("[Weather] Forecast attempt %s failed for %s: %s", attempt, location, exc)
                else:
                    payload = _cached_stale(_FORECAST_CACHE, forecast_key)
                    if payload is None:
                        raise last_error
            result = self._select(payload, args)
            result.update({
                "location": place.get("name"), "admin1": place.get("admin1", ""),
                "country": place.get("country", ""), "timezone": payload.get("timezone", ""),
                "source": "Open-Meteo", "source_url": "https://open-meteo.com/",
            })
            return ToolResult.success(result, display=self._display(result))
        except requests.Timeout:
            logger.exception("[Weather] Forecast request timed out")
            return ToolResult.fail("天气服务请求超时，请改用网络搜索核验")
        except (requests.RequestException, ValueError, KeyError, TypeError) as exc:
            logger.exception("[Weather] Forecast request failed")
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
        result = {"forecast": rows, "requested_date": wanted}
        if "hour_start" in args or "hour_end" in args:
            if target is None:
                raise ValueError("逐小时预报需要指定今天、明天或具体日期")
            hour_start = int(args.get("hour_start", 18))
            hour_end = int(args.get("hour_end", hour_start))
            if not 0 <= hour_start <= hour_end <= 23:
                raise ValueError("预报小时范围必须在 0–23 点之间，且结束时间不早于开始时间")
            hourly = payload.get("hourly") or {}
            times = hourly.get("time") or []
            selected = [i for i, value in enumerate(times) if str(value)[:10] == target.isoformat()
                        and hour_start <= int(str(value)[11:13]) <= hour_end]
            if len(selected) != hour_end - hour_start + 1:
                raise ValueError("天气源没有返回完整的指定时段预报")
            result["hourly"] = [{
                "time": times[i],
                "temperature": hourly["temperature_2m"][i],
                "apparent_temperature": hourly["apparent_temperature"][i],
                "precipitation_probability": hourly["precipitation_probability"][i],
                "precipitation": hourly["precipitation"][i],
                "weather": _WMO.get(int(hourly["weather_code"][i]), "天气待核验"),
                "wind_speed": hourly["wind_speed_10m"][i],
            } for i in selected]
            result["hour_range"] = [hour_start, hour_end]
        return result

    @staticmethod
    def _display(result: dict) -> str:
        if result.get("hourly"):
            hours = result["hourly"]
            start, end = result["hour_range"]
            temperatures = [float(row["temperature"]) for row in hours]
            rain_probability = max(int(row["precipitation_probability"] or 0) for row in hours)
            rain_mm = sum(float(row["precipitation"] or 0) for row in hours)
            winds = [float(row["wind_speed"] or 0) for row in hours]
            weather = "、".join(dict.fromkeys(str(row["weather"]) for row in hours))
            place = f"{result.get('admin1', '')}{result.get('location', '')}" or result.get("location", "")
            advice = []
            if rain_probability >= 50 or rain_mm >= 0.2:
                advice.append("有雨风险，出门带伞")
            elif rain_probability >= 25:
                advice.append("可能有雨，出门前再看临近预报")
            else:
                advice.append("降雨可能性较低")
            if min(temperatures) <= 10:
                advice.append("气温偏低，注意保暖")
            if max(winds) >= 30:
                advice.append("风较大，注意防风")
            return "\n".join([
                f"地区：{place}",
                f"时间：{hours[0]['time'][:10]} {start:02d}:00–{end:02d}:00",
                f"气温：{min(temperatures):g}～{max(temperatures):g}°C",
                f"天气：{weather}",
                f"降雨：最高概率 {rain_probability}%，预计降水 {rain_mm:g} mm",
                f"提醒：{'；'.join(advice)}。",
                "数据来源：Open-Meteo；逐小时预报可能变化，以临近时段为准。",
            ])
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

    @staticmethod
    def format_hourly_brief(result: dict, *, location: str = "", today: bool = True,
                            start_time: str = None, end_time: str = None) -> str:
        """Produce the fixed, single-paragraph message for a scheduled weather task."""
        hours = result.get("hourly") or []
        hour_range = result.get("hour_range") or []
        if not hours or len(hour_range) != 2:
            raise ValueError("天气源没有返回完整的逐小时预报")
        start, end = map(int, hour_range)
        display_start = start_time or f"{start:02d}:00"
        display_end = end_time or f"{end:02d}:00"
        window_rows = hours
        if start_time and end_time:
            start_hour, start_minute = map(int, start_time.split(":"))
            end_hour, end_minute = map(int, end_time.split(":"))
            window_start = start_hour * 60 + start_minute
            window_end = end_hour * 60 + end_minute
            if window_start >= window_end:
                raise ValueError("天气关注结束时间须晚于开始时间")
            overlaps = []
            for row in hours:
                sample_hour = int(str(row["time"])[11:13])
                overlap = max(0, min(window_end, (sample_hour + 1) * 60) - max(window_start, sample_hour * 60))
                if overlap:
                    overlaps.append((row, overlap / 60))
            if not overlaps:
                raise ValueError("天气源没有返回指定时段的逐小时预报")
            window_rows = [row for row, _ in overlaps]
            rain_mm = sum(float(row.get("precipitation") or 0) * weight for row, weight in overlaps)
        else:
            rain_mm = sum(float(row.get("precipitation") or 0) for row in hours)
        temperatures = [float(row["temperature"]) for row in hours]
        rain_probability = max(int(row.get("precipitation_probability") or 0) for row in window_rows)
        wind_speed = max(float(row.get("wind_speed") or 0) for row in window_rows)
        weather = "转".join(dict.fromkeys(str(row.get("weather") or "天气待核验") for row in window_rows))
        place = location.strip() or f"{result.get('admin1', '')}{result.get('location', '')}"
        for municipality in ("上海", "北京", "天津", "重庆"):
            if place.startswith(municipality + "市"):
                place = municipality + place[len(municipality) + 1:]
                break
        advice = []
        if rain_probability >= 50 or rain_mm >= 0.2:
            advice.append("建议带伞")
        elif rain_probability >= 25:
            advice.append("建议出门前留意降雨预报")
        elif rain_probability == 0 and rain_mm == 0:
            advice.append("建议照常出行，可不带伞")
        else:
            advice.append("建议照常出行，出门前留意天气变化")
        if min(temperatures) <= 10:
            advice.append("注意保暖")
        elif min(temperatures) < 18:
            advice.append("气温偏凉可穿薄外套")
        if wind_speed >= 30:
            advice.append("注意防风")
        if min(temperatures) >= 18 and wind_speed < 30:
            advice.append("无需特别保暖或防风")
        title_day = "今日" if today else "明日"
        return (
            f"**{title_day}{display_start}–{display_end}｜{place}**："
            f"{min(temperatures):.1f}–{max(temperatures):.1f}℃，{weather}，"
            f"降雨概率最高{rain_probability}%（预计{rain_mm:.1f}毫米）。"
            f"{'，'.join(advice)}。"
        )
