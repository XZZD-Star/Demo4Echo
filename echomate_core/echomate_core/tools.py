"""Shared read-only tools for Feishu and voice chat."""

import re
import time
from datetime import datetime

import requests

from . import store as echomate_store
from .credentials import credential

LOCAL_TZ = echomate_store.LOCAL_TZ
TAVILY_URL = "https://api.tavily.com/search"
SEARCH_CACHE_TTL = 600
_search_cache = {}

WMO_TEXT = {
    0: "晴", 1: "晴间多云", 2: "多云", 3: "阴",
    45: "雾", 48: "雾凇",
    51: "小毛毛雨", 53: "毛毛雨", 55: "大毛毛雨",
    56: "冻毛毛雨", 57: "强冻毛毛雨",
    61: "小雨", 63: "中雨", 65: "大雨",
    66: "冻雨", 67: "强冻雨",
    71: "小雪", 73: "中雪", 75: "大雪", 77: "雪粒",
    80: "小阵雨", 81: "阵雨", 82: "强阵雨",
    85: "小阵雪", 86: "大阵雪",
    95: "雷阵雨", 96: "雷阵雨伴冰雹", 99: "强雷阵雨伴冰雹",
}

WEATHER_SUFFIXES = (
    "市", "区", "县", "镇", "乡", "省", "自治区", "特别行政区",
)
PROVINCES = (
    "北京", "天津", "上海", "重庆", "河北", "山西", "辽宁", "吉林",
    "黑龙江", "江苏", "浙江", "安徽", "福建", "江西", "山东", "河南",
    "湖北", "湖南", "广东", "海南", "四川", "贵州", "云南", "陕西",
    "甘肃", "青海", "台湾", "内蒙古", "广西", "西藏", "宁夏", "新疆",
    "香港", "澳门",
)


def format_due_friendly(utc_text):
    try:
        due = datetime.fromisoformat(utc_text).astimezone(LOCAL_TZ)
    except (TypeError, ValueError):
        return utc_text or ""
    delta = (due.date() - datetime.now(LOCAL_TZ).date()).days
    day = {0: "今天", 1: "明天", 2: "后天"}.get(delta)
    if day:
        return f"{day} {due:%H:%M}"
    return due.strftime("%m-%d %H:%M")


def _place_candidates(place):
    candidates = []

    def add(value):
        value = (value or "").strip()
        if value and value not in candidates:
            candidates.append(value)

    add(place)
    trimmed = (place or "").strip()
    for suffix in WEATHER_SUFFIXES:
        if trimmed.endswith(suffix) and len(trimmed) > len(suffix):
            trimmed = trimmed[:-len(suffix)]
            add(trimmed)
            break
    for province in PROVINCES:
        if trimmed.startswith(province) and len(trimmed) > len(province):
            add(trimmed[len(province):])
            break
    return candidates


def _geocode(place):
    for candidate in _place_candidates(place):
        try:
            response = requests.get(
                "https://geocoding-api.open-meteo.com/v1/search",
                params={
                    "name": candidate,
                    "count": 1,
                    "language": "zh",
                    "format": "json",
                },
                timeout=8,
            )
            data = response.json()
        except Exception:
            continue
        results = data.get("results") or []
        if results:
            top = results[0]
            return {
                "name": top.get("name", candidate),
                "admin": top.get("admin1", ""),
                "lat": top.get("latitude"),
                "lon": top.get("longitude"),
            }
    return None


def _fetch_weather(place_info, days=1):
    try:
        response = requests.get(
            "https://api.open-meteo.com/v1/forecast",
            params={
                "latitude": place_info["lat"],
                "longitude": place_info["lon"],
                "current": "temperature_2m,relative_humidity_2m,weather_code",
                "daily": (
                    "temperature_2m_max,temperature_2m_min,"
                    "weather_code,precipitation_probability_max"
                ),
                "timezone": "Asia/Shanghai",
                "forecast_days": days,
            },
            timeout=8,
        )
        return response.json()
    except Exception:
        return None


def _memory_places(owner_id):
    candidates = []
    for item in echomate_store.memory_context(owner_id):
        content = item.get("content") or ""
        if not any(
            word in content
            for word in ("居住", "住在", "家住", "所在地", "城市")
        ):
            continue
        match = re.search(
            r"(?:居住地|住在|家住|所在地|城市)(?:是|在|为|：|:)?\s*"
            r"([\u4e00-\u9fa5]{2,20})",
            content,
        )
        if match:
            candidates.append(match.group(1))
    return candidates


def get_weather(args, owner_id):
    place = (args.get("place") or "").strip()
    try:
        days = max(1, min(int(args.get("days") or 3), 7))
    except (TypeError, ValueError):
        days = 3
    try:
        offset = max(0, min(int(args.get("day_offset") or 0), 6))
    except (TypeError, ValueError):
        offset = 0

    place_info = _geocode(place) if place else None
    if not place_info and not place:
        places = _memory_places(owner_id)
        if places:
            place_info = _geocode(places[0])
            if place_info:
                place = places[0]
    if not place_info:
        return {
            "ok": False,
            "error": (
                f"找不到地点「{place}」"
                if place
                else "没有地点，也没有记忆里的居住地"
            ),
        }

    data = _fetch_weather(place_info, days=min(7, offset + days))
    if not data:
        return {"ok": False, "error": "天气服务暂时不可用"}

    current = data.get("current") or {}
    daily = data.get("daily") or {}
    label = place_info["name"]
    admin = place_info.get("admin") or ""
    if admin and admin not in label and label not in admin:
        label = f"{admin}{label}"

    dates = daily.get("time") or []
    highs = daily.get("temperature_2m_max") or []
    lows = daily.get("temperature_2m_min") or []
    codes = daily.get("weather_code") or []
    rains = daily.get("precipitation_probability_max") or []
    forecast = []
    for index in range(offset, min(offset + days, len(dates))):
        forecast.append({
            "date": dates[index],
            "high_c": round(highs[index], 1) if index < len(highs) else None,
            "low_c": round(lows[index], 1) if index < len(lows) else None,
            "condition": WMO_TEXT.get(
                codes[index] if index < len(codes) else -1, "未知"
            ),
            "rain_chance": (
                round(rains[index])
                if index < len(rains) and rains[index] is not None
                else None
            ),
        })

    return {
        "ok": True,
        "place": label,
        "now": {
            "temp_c": (
                round(current["temperature_2m"], 1)
                if current.get("temperature_2m") is not None else None
            ),
            "condition": WMO_TEXT.get(current.get("weather_code"), "未知"),
            "humidity": (
                round(current["relative_humidity_2m"])
                if current.get("relative_humidity_2m") is not None else None
            ),
        },
        "forecast": forecast,
    }


def list_todos(args, owner_id):
    rows = echomate_store.list_todos(owner_id, limit=20)
    return {
        "ok": True,
        "count": len(rows),
        "items": [
            {
                "id": row["id"],
                "title": row["title"],
                "due": format_due_friendly(row["due_at_utc"]),
            }
            for row in rows
        ],
    }


def search_memories(args, owner_id):
    keyword = (args.get("keyword") or "").strip()
    items = echomate_store.memory_context(owner_id, limit=50)
    if keyword:
        items = [
            item for item in items
            if keyword.lower() in (item.get("content") or "").lower()
        ]
    try:
        chat_items = echomate_store.search_chat_memories(
            owner_id,
            keyword or "之前",
            limit=5,
        )
    except Exception:
        chat_items = []
    combined = [
        {"id": item["id"], "content": item["content"]}
        for item in items[:8]
    ]
    combined.extend(
        {"id": item["id"], "content": item["content"]}
        for item in chat_items
    )
    return {
        "ok": True,
        "count": len(combined),
        "items": combined,
    }


def web_search(args):
    query = (args.get("query") or "").strip()
    if not query:
        return {"ok": False, "error": "缺少搜索关键词"}

    api_key = credential("tavily_api_key")
    if not api_key:
        return {"ok": False, "error": "搜索服务未配置"}

    try:
        max_results = max(1, min(int(args.get("max_results") or 5), 8))
    except (TypeError, ValueError):
        max_results = 5

    cache_key = f"{query.lower()}|{max_results}"
    cached = _search_cache.get(cache_key)
    if cached and time.time() - cached[0] < SEARCH_CACHE_TTL:
        return cached[1]

    try:
        response = requests.post(
            TAVILY_URL,
            json={
                "api_key": api_key,
                "query": query,
                "max_results": max_results,
                "include_answer": True,
                "search_depth": "basic",
            },
            timeout=20,
        )
        response.raise_for_status()
        data = response.json()
    except Exception:
        return {"ok": False, "error": "搜索服务暂时不可用"}

    results = [
        {
            "title": item.get("title") or "",
            "url": item.get("url") or "",
            "content": (item.get("content") or "")[:1200],
        }
        for item in (data.get("results") or [])[:max_results]
    ]
    if not results:
        return {"ok": False, "error": f"没有搜到「{query}」的结果"}

    result = {
        "ok": True,
        "query": query,
        "answer": (data.get("answer") or "")[:800],
        "results": results,
    }
    _search_cache[cache_key] = (time.time(), result)
    if len(_search_cache) > 50:
        oldest = min(_search_cache, key=lambda key: _search_cache[key][0])
        _search_cache.pop(oldest, None)
    return result


def execute_read_tool(name, arguments, owner_id):
    args = arguments if isinstance(arguments, dict) else {}
    if name == "get_weather":
        return get_weather(args, owner_id)
    if name == "list_todos":
        return list_todos(args, owner_id)
    if name == "search_memories":
        return search_memories(args, owner_id)
    if name == "web_search":
        return web_search(args)
    return {"ok": False, "error": f"未知读取工具: {name}"}
