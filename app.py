#!/usr/bin/env python3
"""iCloud 日历网页版：本地缓存、增量同步、事件读取与添加。"""

from __future__ import annotations

import json
import os
import re
import threading
import time as time_module
import uuid
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import caldav
from caldav.lib import error as caldav_error
from flask import Flask, Response, jsonify, request, send_from_directory

from icloud_calendar_probe import (
    DEFAULT_CALDAV_URL,
    calendar_name,
    event_end,
    get_required_env,
    iter_vevents,
    property_value,
    search_events,
    sort_key,
)

app = Flask(__name__)
app.json.ensure_ascii = False

BASE_DIR = Path(__file__).resolve().parent
EVENTS_CACHE_FILE = BASE_DIR / "events_cache.json"
SYNC_TOKEN_FILE = BASE_DIR / "sync_token.json"
CACHE_VERSION = 2
DEBUG_EVENTS = os.environ.get("CALENDAR_DEBUG_EVENTS", "0").strip() == "1"

try:
    CACHE_TTL_SECONDS = max(30, int(os.environ.get("CALENDAR_CACHE_TTL_SECONDS", "600")))
except ValueError:
    CACHE_TTL_SECONDS = 600

_sync_lock = threading.Lock()
_refresh_running = threading.Event()


class CalendarNotFoundError(Exception):
    """请求写入的目标日历不存在。"""


def get_timezone() -> timezone | ZoneInfo:
    """读取显示和写入使用的时区，默认使用 Asia/Shanghai。"""
    name = os.environ.get("ICLOUD_TIMEZONE", "Asia/Shanghai").strip() or "Asia/Shanghai"
    try:
        return ZoneInfo(name)
    except Exception:
        if name == "Asia/Shanghai":
            return timezone(timedelta(hours=8))
        return datetime.now().astimezone().tzinfo or timezone.utc


def canonical_calendar_name(value: Any) -> str:
    """合并计划与计划/作业等同一日历的不同名称。"""
    raw = str(value or "").strip()
    compact = re.sub(r"\s+", "", raw)
    if compact in {"计划", "计划/作业", "计划作业"}:
        return "计划/作业"
    return raw or "未命名日历"


def utc_now() -> datetime:
    """返回当前 UTC 时间。"""
    return datetime.now(timezone.utc)


def iso_now() -> str:
    """返回适合 JSON 保存的当前时间。"""
    return utc_now().isoformat(timespec="seconds")


def parse_iso_temporal(value: Any) -> datetime | date | None:
    """解析缓存中的 ISO 日期或日期时间。"""
    if not isinstance(value, str) or not value:
        return None
    try:
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
            return date.fromisoformat(value)
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def temporal_to_json(value: Any) -> str:
    """把 date/datetime 转换为 ISO 字符串。"""
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    return str(value)


def date_to_utc(value: date, tz: timezone | ZoneInfo) -> datetime:
    """把本地日期零点转换为 UTC。"""
    return datetime.combine(value, time.min, tzinfo=tz).astimezone(timezone.utc)


def calendar_key(calendar: Any) -> str:
    """生成日历稳定标识，优先使用 CalDAV URL。"""
    url = getattr(calendar, "url", None)
    if url is not None and hasattr(url, "canonical"):
        return str(url.canonical())
    return str(url or calendar_name(calendar))


def object_href(obj: Any) -> str:
    """返回日历对象的 URL。"""
    url = getattr(obj, "url", None)
    if url is not None and hasattr(url, "canonical"):
        return str(url.canonical())
    return str(url or "")


def empty_events_cache() -> dict[str, Any]:
    """创建空的本地事件缓存。"""
    return {"version": CACHE_VERSION, "updated_at": None, "events": [], "coverage": {}}


def empty_token_state() -> dict[str, Any]:
    """创建空的同步令牌状态。"""
    return {"version": CACHE_VERSION, "calendars": {}}


def read_json_file(path: Path, default: dict[str, Any]) -> dict[str, Any]:
    """读取 JSON 文件，损坏或不存在时返回默认结构。"""
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else default
    except (OSError, json.JSONDecodeError):
        return default


def write_json_file(path: Path, value: dict[str, Any]) -> None:
    """使用临时文件加原子替换，避免写坏缓存。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(temporary, path)


def load_cache_bundle() -> tuple[dict[str, Any], dict[str, Any]]:
    """加载事件缓存和同步令牌。"""
    cache = read_json_file(EVENTS_CACHE_FILE, empty_events_cache())
    tokens = read_json_file(SYNC_TOKEN_FILE, empty_token_state())
    if cache.get("version") != CACHE_VERSION:
        cache = empty_events_cache()
    if tokens.get("version") != CACHE_VERSION:
        tokens = empty_token_state()
    cache.setdefault("events", [])
    cache.setdefault("coverage", {})
    tokens.setdefault("calendars", {})
    return cache, tokens


def save_cache_bundle(cache: dict[str, Any], tokens: dict[str, Any]) -> None:
    """原子保存两组缓存文件。"""
    write_json_file(EVENTS_CACHE_FILE, cache)
    write_json_file(SYNC_TOKEN_FILE, tokens)


def cache_age_seconds(cache: dict[str, Any]) -> float:
    """返回缓存年龄；时间无效时视为过期。"""
    try:
        updated = datetime.fromisoformat(str(cache.get("updated_at", "")).replace("Z", "+00:00"))
        if updated.tzinfo is None:
            updated = updated.replace(tzinfo=timezone.utc)
    except ValueError:
        return float("inf")
    return max(0.0, (utc_now() - updated).total_seconds())



def request_date_range() -> tuple[date, date, datetime, datetime]:
    """解析查询范围；默认返回今天前后各 30 天。"""
    tz = get_timezone()
    today = datetime.now(tz).date()
    start_date = today - timedelta(days=30)
    end_date = today + timedelta(days=31)
    start_text = request.args.get("start", "").strip()
    end_text = request.args.get("end", "").strip()

    if bool(start_text) != bool(end_text):
        raise ValueError("start 和 end 必须同时提供")
    if start_text and end_text:
        try:
            start_date = date.fromisoformat(start_text)
            end_date = date.fromisoformat(end_text)
        except ValueError as exc:
            raise ValueError("start 和 end 必须使用 YYYY-MM-DD 格式") from exc

    if end_date <= start_date:
        raise ValueError("end 必须晚于 start")
    if (end_date - start_date).days > 366:
        raise ValueError("单次查询范围不能超过一年")

    return start_date, end_date, date_to_utc(start_date, tz), date_to_utc(end_date, tz)


def create_client() -> caldav.DAVClient:
    """创建 CalDAV 客户端；凭据始终来自环境变量。"""
    return caldav.DAVClient(
        url=os.environ.get("ICLOUD_CALDAV_URL", DEFAULT_CALDAV_URL).strip(),
        username=get_required_env("ICLOUD_APPLE_ID"),
        password=get_required_env("ICLOUD_APP_PASSWORD"),
    )


def row_from_component(calendar_url: str, calendar_display_name: str, href: str, component: Any) -> dict[str, str] | None:
    """把一个 VEVENT 组件转换为缓存事件。"""
    event_start = property_value(component, "dtstart")
    if event_start is None:
        return None

    event_end_value = event_end(component, event_start)
    summary = property_value(component, "summary")
    location = property_value(component, "location")
    uid = property_value(component, "uid")
    return {
        "calendar_url": calendar_url,
        "href": href,
        "uid": str(uid) if uid else "",
        "calendar": canonical_calendar_name(calendar_display_name),
        "title": str(summary) if summary else "（无标题）",
        "start": temporal_to_json(event_start),
        "end": temporal_to_json(event_end_value),
        "location": str(location) if location else "",
    }

def temporal_to_utc(value: datetime | date, tz: timezone | ZoneInfo) -> datetime:
    """把缓存中的日期时间转换为 UTC，用于范围重叠判断。"""
    if isinstance(value, datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=tz)
        return value.astimezone(timezone.utc)
    return datetime.combine(value, time.min, tzinfo=tz).astimezone(timezone.utc)


def event_overlaps_range(row: dict[str, Any], start_date: date, end_date: date, tz: timezone | ZoneInfo) -> bool:
    """判断缓存事件是否与查询范围重叠。"""
    event_start = parse_iso_temporal(row.get("start"))
    event_finish = parse_iso_temporal(row.get("end"))
    if event_start is None:
        return False
    if event_finish is None:
        event_finish = event_start
    range_start = date_to_utc(start_date, tz)
    range_end = date_to_utc(end_date, tz)
    return temporal_to_utc(event_finish, tz) > range_start and temporal_to_utc(event_start, tz) < range_end


def ranges_for_calendar(cache: dict[str, Any], key: str) -> list[dict[str, str]]:
    """返回某个日历已经缓存的日期范围。"""
    state = cache.get("coverage", {}).get(key, {})
    return list(state.get("ranges", []))


def calendar_range_covered(cache: dict[str, Any], key: str, start_date: date, end_date: date) -> bool:
    """判断某个日历是否完整覆盖指定范围。"""
    for item in ranges_for_calendar(cache, key):
        try:
            cached_start = date.fromisoformat(item["start"])
            cached_end = date.fromisoformat(item["end"])
        except (KeyError, ValueError):
            continue
        if cached_start <= start_date and cached_end >= end_date:
            return True
    return False


def cache_range_covered(cache: dict[str, Any], start_date: date, end_date: date) -> bool:
    """判断所有已缓存日历是否都覆盖指定范围。"""
    coverage = cache.get("coverage", {})
    if not coverage:
        return False
    return all(calendar_range_covered(cache, key, start_date, end_date) for key in coverage)


def add_coverage(cache: dict[str, Any], key: str, raw_name: str, canonical_name: str, start_date: date, end_date: date) -> None:
    """增加并合并日历缓存覆盖范围。"""
    state = cache.setdefault("coverage", {}).setdefault(
        key, {"name": raw_name, "canonical_name": canonical_name, "ranges": []}
    )
    state["name"] = raw_name
    state["canonical_name"] = canonical_name
    ranges = list(state.get("ranges", []))
    ranges.append({"start": start_date.isoformat(), "end": end_date.isoformat()})
    normalized: list[dict[str, str]] = []
    for item in sorted(ranges, key=lambda value: value["start"]):
        if not normalized or item["start"] > normalized[-1]["end"]:
            normalized.append(dict(item))
        else:
            normalized[-1]["end"] = max(normalized[-1]["end"], item["end"])
    state["ranges"] = normalized


def remove_cached_event(
    cache: dict[str, Any], key: str, href: str = "", uid: str = "", start: str = ""
) -> None:
    """按 URL、UID 和可选开始时间从缓存删除事件。"""
    def keep(row: dict[str, Any]) -> bool:
        if row.get("calendar_url") != key:
            return True
        if start:
            if row.get("start") != start:
                return True
            if href and row.get("href") == href:
                return False
            if uid and row.get("uid") == uid:
                return False
            return True
        if href and row.get("href") == href:
            return False
        if uid and row.get("uid") == uid:
            return False
        return True

    cache["events"] = [row for row in cache.get("events", []) if keep(row)]


def upsert_cached_event(cache: dict[str, Any], row: dict[str, Any]) -> None:
    """插入或替换同一重复事件的具体展开实例。"""
    remove_cached_event(
        cache,
        row["calendar_url"],
        row.get("href", ""),
        row.get("uid", ""),
        row.get("start", ""),
    )
    cache.setdefault("events", []).append(row)


def remove_calendar_cache(cache: dict[str, Any], key: str) -> None:
    """删除某个日历的全部缓存。"""
    cache["events"] = [row for row in cache.get("events", []) if row.get("calendar_url") != key]
    cache.get("coverage", {}).pop(key, None)


def remove_cached_events_in_range(
    cache: dict[str, Any], key: str, start_date: date, end_date: date, tz: timezone | ZoneInfo
) -> None:
    """重新抓取范围前先移除该范围内旧数据，避免留下删除项。"""
    cache["events"] = [
        row
        for row in cache.get("events", [])
        if row.get("calendar_url") != key or not event_overlaps_range(row, start_date, end_date, tz)
    ]


def public_event(row: dict[str, Any]) -> dict[str, str]:
    """移除缓存内部字段，生成 API 响应。"""
    return {
        "uid": str(row.get("uid", "")),
        "calendar": str(row.get("calendar", "未命名日历")),
        "title": str(row.get("title", "（无标题）")),
        "start": str(row.get("start", "")),
        "end": str(row.get("end", "")),
        "location": str(row.get("location", "")),
    }


def cached_events_for_range(cache: dict[str, Any], start_date: date, end_date: date) -> list[dict[str, str]]:
    """从本地缓存过滤并排序指定范围的事件。"""
    tz = get_timezone()
    rows = [
        row
        for row in cache.get("events", [])
        if event_overlaps_range(row, start_date, end_date, tz)
    ]
    rows.sort(
        key=lambda row: (
            sort_key(parse_iso_temporal(row.get("start")) or datetime.max.replace(tzinfo=timezone.utc)),
            row.get("calendar", ""),
            row.get("title", ""),
        )
    )
    return [public_event(row) for row in rows]


def rows_from_calendar(calendar: Any, key: str, raw_name: str, start: datetime, end: datetime) -> list[dict[str, str]]:
    """全量读取某个日历在指定时间范围内的事件。"""
    rows: list[dict[str, str]] = []
    for event in search_events(calendar, start, end):
        href = object_href(event)
        try:
            for component in iter_vevents(event):
                row = row_from_component(key, raw_name, href, component)
                if row:
                    rows.append(row)
        except Exception:
            app.logger.exception("解析日历事件失败：%s", raw_name)
    app.logger.info("CalDAV 日历 %s 返回 %d 条展开事件", raw_name, len(rows))
    if DEBUG_EVENTS:
        for row in rows:
            app.logger.info(
                "原始事件 calendar=%s uid=%s start=%s end=%s title=%s",
                row.get("calendar"),
                row.get("uid"),
                row.get("start"),
                row.get("end"),
                row.get("title"),
            )
    return rows


def rows_from_sync_object(obj: Any, key: str, raw_name: str) -> list[dict[str, str]]:
    """解析增量同步返回的单个日历对象。"""
    try:
        object_data = getattr(obj, "data", None)
    except Exception:
        object_data = None
    if not object_data:
        return []
    rows: list[dict[str, str]] = []
    href = object_href(obj)
    for component in iter_vevents(obj):
        row = row_from_component(key, raw_name, href, component)
        if row:
            rows.append(row)
    return rows


def object_has_recurrence(obj: Any) -> bool:
    """判断同步对象是否包含重复规则。"""
    try:
        for component in iter_vevents(obj):
            if property_value(component, "rrule") or property_value(component, "rdate"):
                return True
    except Exception:
        return False
    return False


def initialize_sync_token(calendar: Any) -> str:
    """通过首次 sync-collection 获取并保存同步令牌。"""
    collection = calendar.objects_by_sync_token(load_objects=False)
    return str(collection.sync_token or "")


def full_sync_calendar(
    calendar: Any,
    key: str,
    raw_name: str,
    start_date: date,
    end_date: date,
    cache: dict[str, Any],
    tokens: dict[str, Any],
    reset_cache: bool,
) -> None:
    """对单个日历执行全量范围同步。"""
    canonical_name = canonical_calendar_name(raw_name)
    tz = get_timezone()
    token = str(tokens.get("calendars", {}).get(key, {}).get("sync_token") or "")

    if reset_cache:
        remove_calendar_cache(cache, key)
        try:
            token = initialize_sync_token(calendar)
        except Exception:
            app.logger.exception("获取同步令牌失败：%s", raw_name)
            token = ""

    remove_cached_events_in_range(cache, key, start_date, end_date, tz)
    rows = rows_from_calendar(calendar, key, raw_name, date_to_utc(start_date, tz), date_to_utc(end_date, tz))
    for row in rows:
        upsert_cached_event(cache, row)

    add_coverage(cache, key, raw_name, canonical_name, start_date, end_date)
    tokens.setdefault("calendars", {})[key] = {
        "name": raw_name,
        "canonical_name": canonical_name,
        "sync_token": token,
        "synced_at": iso_now(),
    }


def incremental_sync_calendar(
    calendar: Any,
    key: str,
    raw_name: str,
    start_date: date,
    end_date: date,
    cache: dict[str, Any],
    tokens: dict[str, Any],
) -> None:
    """执行增量同步；令牌失效时回退到全量范围同步。"""
    state = tokens.get("calendars", {}).get(key, {})
    token = str(state.get("sync_token") or "")
    if not token:
        full_sync_calendar(calendar, key, raw_name, start_date, end_date, cache, tokens, reset_cache=True)
        return

    try:
        changes = calendar.objects_by_sync_token(sync_token=token, load_objects=True)
        new_token = str(changes.sync_token or "")
        if not new_token or new_token.startswith("fake-"):
            raise RuntimeError("服务器不支持真实同步令牌或令牌无效")

        tz = get_timezone()
        coverage_ranges = ranges_for_calendar(cache, key)
        needs_range_refresh = False
        for obj in changes:
            needs_range_refresh = needs_range_refresh or object_has_recurrence(obj)
            href = object_href(obj)
            try:
                old_uid = str(getattr(obj, "id", "") or "")
            except Exception:
                old_uid = ""
            remove_cached_event(cache, key, href, old_uid)
            rows = rows_from_sync_object(obj, key, raw_name)
            for row in rows:
                if any(
                    event_overlaps_range(
                        row,
                        date.fromisoformat(item["start"]),
                        date.fromisoformat(item["end"]),
                        tz,
                    )
                    for item in coverage_ranges
                ):
                    upsert_cached_event(cache, row)

        state.update(
            {
                "name": raw_name,
                "canonical_name": canonical_calendar_name(raw_name),
                "sync_token": new_token,
                "synced_at": iso_now(),
            }
        )
        tokens["calendars"][key] = state
        if needs_range_refresh:
            for item in coverage_ranges:
                full_sync_calendar(
                    calendar,
                    key,
                    raw_name,
                    date.fromisoformat(item["start"]),
                    date.fromisoformat(item["end"]),
                    cache,
                    tokens,
                    reset_cache=False,
                )
        elif not calendar_range_covered(cache, key, start_date, end_date):
            full_sync_calendar(calendar, key, raw_name, start_date, end_date, cache, tokens, reset_cache=False)
    except Exception:
        app.logger.exception("增量同步失败，回退全量同步：%s", raw_name)
        full_sync_calendar(calendar, key, raw_name, start_date, end_date, cache, tokens, reset_cache=True)


def sync_cache(start_date: date, end_date: date, force_full: bool = False) -> dict[str, Any]:
    """同步所有日历，并保存事件缓存与同步令牌。"""
    with _sync_lock:
        cache, tokens = load_cache_bundle()
        client = create_client()
        calendars = client.principal().calendars()
        active_keys = {calendar_key(calendar) for calendar in calendars}

        for calendar in calendars:
            key = calendar_key(calendar)
            raw_name = calendar_name(calendar)
            try:
                if force_full or key not in tokens.get("calendars", {}):
                    full_sync_calendar(calendar, key, raw_name, start_date, end_date, cache, tokens, reset_cache=True)
                else:
                    incremental_sync_calendar(calendar, key, raw_name, start_date, end_date, cache, tokens)
            except Exception:
                app.logger.exception("同步日历失败：%s", raw_name)
                try:
                    full_sync_calendar(calendar, key, raw_name, start_date, end_date, cache, tokens, reset_cache=True)
                except Exception:
                    app.logger.exception("全量同步回退失败：%s", raw_name)

        for stale_key in list(tokens.get("calendars", {})):
            if stale_key not in active_keys:
                tokens["calendars"].pop(stale_key, None)
                remove_calendar_cache(cache, stale_key)

        cache["updated_at"] = iso_now()
        save_cache_bundle(cache, tokens)
        return cache


def background_refresh(start_date: date, end_date: date) -> None:
    """后台执行增量同步，不阻塞当前页面请求。"""
    try:
        sync_cache(start_date, end_date, force_full=False)
    except Exception:
        app.logger.exception("后台日历同步失败")
    finally:
        _refresh_running.clear()


def schedule_background_refresh(start_date: date, end_date: date) -> None:
    """确保同一时间只运行一个后台同步。"""
    if _refresh_running.is_set():
        return
    _refresh_running.set()
    threading.Thread(
        target=background_refresh,
        args=(start_date, end_date),
        daemon=True,
        name="calendar-cache-refresh",
    ).start()


_worker_started = threading.Event()


def default_sync_range() -> tuple[date, date]:
    """返回后台任务使用的默认范围：今天前后各 30 天。"""
    tz = get_timezone()
    today = datetime.now(tz).date()
    return today - timedelta(days=30), today + timedelta(days=31)


def background_worker() -> None:
    """启动时同步一次，之后每 10 分钟增量刷新。"""
    while True:
        try:
            start_date, end_date = default_sync_range()
            sync_cache(start_date, end_date, force_full=False)
            app.logger.info("日历缓存后台同步完成")
        except Exception:
            app.logger.exception("日历缓存后台同步失败")
        time_module.sleep(600)


def start_background_worker() -> None:
    """保证后台同步线程只启动一次。"""
    if _worker_started.is_set():
        return
    _worker_started.set()
    threading.Thread(target=background_worker, daemon=True, name="calendar-periodic-sync").start()

def read_cached_or_sync(start_date: date, end_date: date) -> list[dict[str, str]]:
    """优先读取缓存；缺失或未覆盖时执行同步。"""
    cache, _ = load_cache_bundle()
    if cache_range_covered(cache, start_date, end_date):
        if cache_age_seconds(cache) > CACHE_TTL_SECONDS:
            schedule_background_refresh(start_date, end_date)
        return cached_events_for_range(cache, start_date, end_date)

    cache = sync_cache(start_date, end_date, force_full=False)
    return cached_events_for_range(cache, start_date, end_date)


def add_created_event_to_cache(
    key: str, raw_name: str, created_event: Any, event_data: dict[str, Any]
) -> None:
    """写入成功后立即更新本地缓存，避免页面等待完整同步。"""
    with _sync_lock:
        cache, tokens = load_cache_bundle()
        row = {
            "calendar_url": key,
            "href": object_href(created_event),
            "uid": str(event_data["uid"]),
            "calendar": canonical_calendar_name(raw_name),
            "title": str(event_data.get("title", event_data.get("summary", ""))),
            "start": temporal_to_json(event_data.get("start", event_data.get("dtstart"))),
            "end": temporal_to_json(event_data.get("end", event_data.get("dtend"))),
            "location": str(event_data.get("location", "")),
        }
        upsert_cached_event(cache, row)
        cache["updated_at"] = iso_now()
        save_cache_bundle(cache, tokens)


def parse_datetime_input(value: Any, tz: timezone | ZoneInfo) -> datetime:
    """解析日期时间输入；无时区输入按 ICLOUD_TIMEZONE 解释。"""
    if not isinstance(value, str) or not value.strip():
        raise ValueError("开始时间和结束时间不能为空")
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError("日期时间格式无效") from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=tz)
    return parsed


def find_writable_calendar(calendars: list[Any], requested_name: str) -> Any:
    """查找目标日历，并兼容「计划」与「计划/作业」。"""
    requested = canonical_calendar_name(requested_name)
    matches: list[tuple[str, Any]] = []
    for calendar in calendars:
        raw_name = calendar_name(calendar)
        if canonical_calendar_name(raw_name) == requested:
            matches.append((raw_name, calendar))
    if not matches:
        raise CalendarNotFoundError(f"未找到日历：{requested_name}")
    exact = [calendar for raw_name, calendar in matches if raw_name == requested_name]
    return exact[0] if exact else matches[0][1]


def temporal_event_response(event_data: dict[str, Any]) -> dict[str, str]:
    """把内部事件字段转换为 PUT 响应。"""
    return {
        "uid": str(event_data["uid"]),
        "calendar": canonical_calendar_name(event_data["calendar"]),
        "title": str(event_data["title"]),
        "start": temporal_to_json(event_data["start"]),
        "end": temporal_to_json(event_data["end"]),
        "location": str(event_data.get("location", "")),
    }

def find_event_by_uid(calendars: list[Any], uid: str) -> tuple[Any, Any]:
    """在所有日历中按 UID 查找事件。"""
    for calendar in calendars:
        try:
            event = calendar.event_by_uid(uid)
        except caldav_error.NotFoundError:
            continue
        if event:
            return calendar, event
    raise CalendarNotFoundError(f"未找到事件：{uid}")


def update_icalendar_component(component: Any, title: str, start: datetime, end: datetime, location: str) -> None:
    """更新 VEVENT 组件中的可编辑字段。"""
    component["SUMMARY"] = title
    component["DTSTART"] = start
    component["DTEND"] = end
    if location:
        component["LOCATION"] = location
    else:
        component.pop("LOCATION", None)


def update_cache_after_edit(uid: str, key: str, raw_name: str, href: str, event_data: dict[str, Any]) -> None:
    """编辑成功后同步更新本地缓存。"""
    with _sync_lock:
        cache, tokens = load_cache_bundle()
        cache["events"] = [row for row in cache.get("events", []) if row.get("uid") != uid]
        row = {
            "calendar_url": key,
            "href": href,
            "uid": uid,
            "calendar": canonical_calendar_name(raw_name),
            "title": str(event_data.get("title", event_data.get("summary", ""))),
            "start": temporal_to_json(event_data.get("start", event_data.get("dtstart"))),
            "end": temporal_to_json(event_data.get("end", event_data.get("dtend"))),
            "location": str(event_data.get("location", "")),
        }
        upsert_cached_event(cache, row)
        cache["updated_at"] = iso_now()
        save_cache_bundle(cache, tokens)

def json_response(payload: dict[str, Any], status: int = 200) -> Response:
    """返回禁止缓存的 JSON 响应。"""
    response = jsonify(payload)
    response.status_code = status
    response.headers["Cache-Control"] = "no-store"
    return response


@app.get("/")
def index() -> Response:
    """返回静态前端页面。"""
    return send_from_directory(app.root_path, "index.html")


@app.get("/api/events")
def api_events() -> Response:
    """读取事件，优先返回本地缓存。"""
    try:
        start_date, end_date, _, _ = request_date_range()
        return json_response({"events": read_cached_or_sync(start_date, end_date)})
    except ValueError as exc:
        return json_response({"error": str(exc)}, status=400)
    except RuntimeError as exc:
        cache, _ = load_cache_bundle()
        stale = cached_events_for_range(cache, start_date, end_date) if "start_date" in locals() else []
        if stale:
            return json_response({"events": stale})
        return json_response({"error": str(exc)}, status=503)
    except Exception as exc:
        app.logger.exception("读取 iCloud 日历缓存时发生错误")
        if "start_date" in locals() and "end_date" in locals():
            cache, _ = load_cache_bundle()
            stale = cached_events_for_range(cache, start_date, end_date)
            if stale:
                return json_response({"events": stale})
        return json_response(
            {"error": "无法读取 iCloud 日历", "detail": f"{type(exc).__name__}: {exc}"},
            status=502,
        )


@app.post("/api/events")
def create_event() -> Response:
    """创建事件并写入指定 iCloud 日历。"""
    payload = request.get_json(silent=True)
    if not isinstance(payload, dict):
        return json_response({"error": "请求体必须是 JSON 对象"}, status=400)

    title = str(payload.get("title", "")).strip()
    calendar_requested = str(payload.get("calendar", "")).strip()
    location = str(payload.get("location", "")).strip()
    if not title:
        return json_response({"error": "标题不能为空"}, status=400)
    if not calendar_requested:
        return json_response({"error": "必须选择所属日历"}, status=400)

    try:
        tz = get_timezone()
        start = parse_datetime_input(payload.get("start"), tz)
        end = parse_datetime_input(payload.get("end"), tz)
        if end <= start:
            raise ValueError("结束时间必须晚于开始时间")

        client = create_client()
        calendars = client.principal().calendars()
        target_calendar = find_writable_calendar(calendars, calendar_requested)
        target_key = calendar_key(target_calendar)
        target_name = calendar_name(target_calendar)
        uid = f"{uuid.uuid4()}@icodex-calendar"
        event_data: dict[str, Any] = {
            "uid": uid,
            "dtstart": start,
            "dtend": end,
            "summary": title,
        }
        if location:
            event_data["location"] = location
        created_event = target_calendar.add_event(**event_data)
        add_created_event_to_cache(target_key, target_name, created_event, event_data)
    except ValueError as exc:
        return json_response({"error": str(exc)}, status=400)
    except CalendarNotFoundError as exc:
        return json_response({"error": str(exc)}, status=404)
    except RuntimeError as exc:
        return json_response({"error": str(exc)}, status=503)
    except Exception as exc:
        app.logger.exception("写入 iCloud 日历时发生错误")
        return json_response(
            {"error": "写入 iCloud 日历失败", "detail": f"{type(exc).__name__}: {exc}"},
            status=502,
        )

    return json_response(
        {
            "event": {
                "uid": uid,
                "calendar": canonical_calendar_name(target_name),
                "title": title,
                "start": start.isoformat(),
                "end": end.isoformat(),
                "location": location,
            }
        },
        status=201,
    )


@app.put("/api/events/<path:event_uid>")
def update_event(event_uid: str) -> Response:
    """按 UID 更新事件；如需更换日历，则移动事件到目标日历。"""
    payload = request.get_json(silent=True)
    if not isinstance(payload, dict):
        return json_response({"error": "请求体必须是 JSON 对象"}, status=400)

    title = str(payload.get("title", "")).strip()
    calendar_requested = str(payload.get("calendar", "")).strip()
    location = str(payload.get("location", "")).strip()
    if not title:
        return json_response({"error": "标题不能为空"}, status=400)
    if not calendar_requested:
        return json_response({"error": "必须选择所属日历"}, status=400)

    try:
        tz = get_timezone()
        start = parse_datetime_input(payload.get("start"), tz)
        end = parse_datetime_input(payload.get("end"), tz)
        if end <= start:
            raise ValueError("结束时间必须晚于开始时间")

        client = create_client()
        calendars = client.principal().calendars()
        old_calendar, event = find_event_by_uid(calendars, event_uid)
        component = event.icalendar_component
        if component is None:
            raise ValueError("无法解析事件内容")
        if component.get("RRULE") or component.get("RECURRENCE-ID"):
            raise ValueError("暂不支持编辑循环日程")

        update_icalendar_component(component, title, start, end, location)
        target_calendar = find_writable_calendar(calendars, calendar_requested)
        old_key = calendar_key(old_calendar)
        target_key = calendar_key(target_calendar)
        target_name = calendar_name(target_calendar)

        if old_key == target_key:
            event.data = event.icalendar_instance
            event.save()
            href = object_href(event)
        else:
            ics_data = event.icalendar_instance.to_ical()
            created_event = target_calendar.add_event(ical=ics_data)
            try:
                event.delete()
            except Exception:
                try:
                    created_event.delete()
                except Exception:
                    app.logger.exception("移动事件失败且回滚失败：%s", event_uid)
                raise
            href = object_href(created_event)

        event_data = {
            "uid": event_uid,
            "calendar": target_name,
            "title": title,
            "start": start,
            "end": end,
            "location": location,
        }
        update_cache_after_edit(event_uid, target_key, target_name, href, event_data)
    except ValueError as exc:
        return json_response({"error": str(exc)}, status=400)
    except CalendarNotFoundError as exc:
        return json_response({"error": str(exc)}, status=404)
    except RuntimeError as exc:
        return json_response({"error": str(exc)}, status=503)
    except Exception as exc:
        app.logger.exception("更新 iCloud 日历时发生错误")
        return json_response(
            {"error": "更新 iCloud 日历失败", "detail": f"{type(exc).__name__}: {exc}"},
            status=502,
        )

    return json_response({"event": temporal_event_response(event_data)})

@app.post("/api/refresh")
def manual_refresh() -> Response:
    """手动触发一次增量同步并返回最新缓存。"""
    try:
        start_date, end_date, _, _ = request_date_range()
        cache = sync_cache(start_date, end_date, force_full=False)
        return json_response(
            {
                "events": cached_events_for_range(cache, start_date, end_date),
                "updated_at": cache.get("updated_at"),
                "range": {"start": start_date.isoformat(), "end": end_date.isoformat()},
            }
        )
    except ValueError as exc:
        return json_response({"error": str(exc)}, status=400)
    except RuntimeError as exc:
        return json_response({"error": str(exc)}, status=503)
    except Exception as exc:
        app.logger.exception("手动刷新日历失败")
        return json_response(
            {"error": "手动刷新失败", "detail": f"{type(exc).__name__}: {exc}"},
            status=502,
        )

@app.after_request
def add_cors_headers(response: Response) -> Response:
    """允许已配置的前端跨域读取和创建事件。"""
    allowed_origin = os.environ.get("ALLOWED_ORIGIN", "").strip()
    if allowed_origin:
        response.headers["Access-Control-Allow-Origin"] = allowed_origin
        response.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
        response.headers["Access-Control-Allow-Headers"] = "Content-Type"
        response.headers["Vary"] = "Origin"
    return response


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "5000"))
    debug = os.environ.get("FLASK_DEBUG", "0") == "1"
    app.run(host="127.0.0.1", port=port, debug=debug)
