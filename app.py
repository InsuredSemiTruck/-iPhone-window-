#!/usr/bin/env python3
"""iCloud 日历网页版：本地缓存、增量同步、事件读取与添加。"""

from __future__ import annotations

import json
import os
import re
import ssl
import threading
import time as time_module
import uuid
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from typing import Any, Callable
from zoneinfo import ZoneInfo

import caldav
import icalendar
from caldav.elements import dav
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

try:
    CALDAV_CLIENT_TTL_SECONDS = max(30, int(os.environ.get("CALENDAR_CLIENT_TTL_SECONDS", "300")))
except ValueError:
    CALDAV_CLIENT_TTL_SECONDS = 300

try:
    HTTP_TIMEOUT_SECONDS = max(5, int(os.environ.get("CALENDAR_HTTP_TIMEOUT_SECONDS", "30")))
except ValueError:
    HTTP_TIMEOUT_SECONDS = 30

NETWORK_RETRY_ATTEMPTS = 3
NETWORK_RETRY_BASE_DELAY = 0.8
RETRYABLE_STATUS_PATTERN = re.compile(r"status (?:429|5\d\d)\b")
RETRYABLE_TEXT_MARKERS = (
    "ssleoferror",
    "unexpected_eof",
    "connection aborted",
    "connection reset",
    "remote end closed",
    "max retries exceeded",
    "read timed out",
    "timed out",
    "temporarily unavailable",
)

_sync_lock = threading.Lock()
_refresh_running = threading.Event()


class CalendarNotFoundError(Exception):
    """请求写入的目标日历不存在。"""


class EventNotFoundError(Exception):
    """目标事件在本地缓存和 iCloud 上都无法定位。"""


class EventConflictError(Exception):
    """iCloud 以 412 拒绝了本次修改（内容已被其他客户端改动）。"""


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
        timeout=HTTP_TIMEOUT_SECONDS,
    )


_client_lock = threading.Lock()
_client_state: dict[str, Any] = {"client": None, "calendars": [], "created_at": 0.0}


def invalidate_client_cache() -> None:
    """丢弃缓存的连接，下次访问时重新创建并重新做服务发现。"""
    with _client_lock:
        _client_state["client"] = None
        _client_state["calendars"] = []
        _client_state["created_at"] = 0.0


def get_calendars(force_refresh: bool = False) -> list[Any]:
    """获取日历列表。

    iCloud 上最容易失败的一步是“服务发现 + PROPFIND 根路径”，
    所以这里缓存客户端与日历列表，避免每次写入都重新做一遍。
    """
    with _client_lock:
        now = time_module.monotonic()
        usable = (
            not force_refresh
            and _client_state["client"] is not None
            and now - float(_client_state["created_at"]) < CALDAV_CLIENT_TTL_SECONDS
        )
        if usable and _client_state["calendars"]:
            return list(_client_state["calendars"])
        client = _client_state["client"] if usable else create_client()
        calendars = list(client.principal().calendars())
        _client_state.update(
            {"client": client, "calendars": calendars, "created_at": time_module.monotonic()}
        )
        return list(calendars)


def is_retryable_remote_error(exc: BaseException) -> bool:
    """判断是否属于可重试的瞬时远端故障：SSL EOF、连接中断、超时、限流、5xx。"""
    if isinstance(
        exc,
        (
            caldav_error.NotFoundError,
            caldav_error.AuthorizationError,
            caldav_error.ETagMismatchError,
            caldav_error.ScheduleTagMismatchError,
            EventNotFoundError,
            EventConflictError,
            FileNotFoundError,
            PermissionError,
            ValueError,
        ),
    ):
        return False
    if is_precondition_failed(exc):
        return False
    if isinstance(
        exc, (caldav_error.RateLimitError, ConnectionError, TimeoutError, ssl.SSLError, OSError)
    ):
        return True
    message = f"{type(exc).__name__}: {exc}".lower()
    if RETRYABLE_STATUS_PATTERN.search(message) or any(
        marker in message for marker in RETRYABLE_TEXT_MARKERS
    ):
        return True
    cause = exc.__cause__ or exc.__context__
    if cause is not None and cause is not exc:
        return is_retryable_remote_error(cause)
    return False


def call_with_network_retry(
    action: Callable[[bool], Any],
    description: str,
    attempts: int = NETWORK_RETRY_ATTEMPTS,
) -> Any:
    """执行访问 iCloud 的动作；瞬时故障时丢弃连接、等待后重试。

    action 接收一个布尔参数表示“是否需要重建连接”：首次尝试为 False，重试为 True。
    """
    delay = NETWORK_RETRY_BASE_DELAY
    for attempt in range(1, attempts + 1):
        try:
            return action(attempt > 1)
        except Exception as exc:
            if attempt >= attempts or not is_retryable_remote_error(exc):
                raise
            app.logger.warning(
                "iCloud 访问失败（%s，第 %d/%d 次尝试）：%s: %s，%.1f 秒后自动重试",
                description,
                attempt,
                attempts,
                type(exc).__name__,
                exc,
                delay,
            )
            invalidate_client_cache()
            time_module.sleep(delay)
            delay *= 2
    raise RuntimeError(f"{description}失败")


def calendars_with_retry() -> list[Any]:
    """获取日历列表（带瞬时故障重试），供读取和写入共用。"""
    return call_with_network_retry(
        lambda fresh: get_calendars(force_refresh=fresh), "获取日历列表"
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
        calendars = calendars_with_retry()
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


def parse_datetime_input(value: Any, tz: timezone | ZoneInfo) -> datetime | date:
    """解析日期时间或纯日期输入；无时区时间按 ICLOUD_TIMEZONE 解释。"""
    if not isinstance(value, str) or not value.strip():
        raise ValueError("开始时间和结束时间不能为空")
    text = value.strip()
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", text):
        try:
            return date.fromisoformat(text)
        except ValueError as exc:
            raise ValueError("日期格式无效") from exc
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
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

def cached_event_row(uid: str) -> dict[str, Any] | None:
    """从本地缓存中查找事件记录，用于定位所属日历与资源 URL。"""
    if not uid:
        return None
    cache, _ = load_cache_bundle()
    for row in cache.get("events", []):
        if str(row.get("uid", "")) != uid:
            continue
        if row.get("href") and row.get("calendar_url"):
            return row
    return None


def calendar_for_key(calendars: list[Any], key: str) -> Any | None:
    """按缓存里的日历 URL 找到对应的 CalDAV 日历对象。"""
    for calendar in calendars:
        try:
            if calendar_key(calendar) == key:
                return calendar
        except Exception:
            continue
    return None


def load_event_by_href(calendar: Any, href: str) -> Any:
    """按资源 URL 直接 GET 事件，避开 iCloud 上不可靠的 event_by_uid。"""
    event = calendar.event_by_url(href)
    if event is None or not event.data:
        raise RuntimeError(f"事件资源没有返回内容：{href}")
    app.logger.info("已按资源 URL 读取事件：%s", href)
    return event


def locate_event_for_update(calendars: list[Any], uid: str) -> tuple[Any, Any]:
    """定位要修改的事件：优先用本地缓存的 calendar_url + href，最后才回退 event_by_uid。"""
    row = cached_event_row(uid)
    if row:
        calendar = calendar_for_key(calendars, str(row.get("calendar_url", "")))
        if calendar is not None:
            try:
                return calendar, load_event_by_href(calendar, str(row["href"]))
            except Exception as exc:
                if is_retryable_remote_error(exc):
                    # 网络中断必须冒泡出去交给重试层，不能当成“事件不存在”。
                    app.logger.warning(
                        "按缓存 URL 读取事件时网络中断（uid=%s url=%s）：%s: %s",
                        uid,
                        row.get("href"),
                        type(exc).__name__,
                        exc,
                    )
                    raise
                app.logger.warning(
                    "按缓存 URL 读取事件失败（uid=%s url=%s）：%s: %s",
                    uid,
                    row.get("href"),
                    type(exc).__name__,
                    exc,
                )
        else:
            app.logger.warning(
                "缓存里的日历不在 iCloud 日历列表中（uid=%s key=%s），回退 event_by_uid",
                uid,
                row.get("calendar_url"),
            )
    else:
        app.logger.info("本地缓存没有事件 %s，回退 event_by_uid", uid)

    for calendar in calendars:
        try:
            event = calendar.event_by_uid(uid)
        except caldav_error.NotFoundError:
            continue
        except Exception as exc:
            if is_retryable_remote_error(exc):
                app.logger.warning(
                    "event_by_uid 遇到网络中断（calendar=%s uid=%s）：%s: %s",
                    calendar_name(calendar),
                    uid,
                    type(exc).__name__,
                    exc,
                )
                raise
            app.logger.warning(
                "event_by_uid 查询失败（calendar=%s uid=%s）：%s: %s",
                calendar_name(calendar),
                uid,
                type(exc).__name__,
                exc,
            )
            continue
        if event:
            return calendar, event
    raise EventNotFoundError(f"未找到事件：{uid}")


def normalize_temporal(value: Any) -> Any | None:
    """把带时区的 datetime 统一转换为 UTC，避免写出依赖 VTIMEZONE 的 TZID 值。"""
    if value is None:
        return None
    if isinstance(value, datetime) and value.tzinfo is not None:
        return value.astimezone(timezone.utc)
    return value


def set_icalendar_property(
    component: Any, name: str, value: Any, parameters: dict[str, str] | None = None
) -> None:
    """通过 add() 写入属性。

    icalendar 7.x 中 `component["DTSTART"] = datetime(...)` 会把 datetime 直接按
    `str()` 写成非法值（例如 `DTSTART:2026-10-12 09:00:00+00:00`），
    导致 iCloud 拒绝整份数据（412/400/500）；必须使用 add() 走类型转换。
    需要保留 `TZID` 等参数时通过 parameters 传入，仍然是 add() 的合法路径。
    """
    component.pop(name, None)
    if value is None:
        return
    if parameters:
        component.add(name, value, parameters=dict(parameters))
        return
    component.add(name, value)


def append_icalendar_property(
    component: Any, name: str, value: Any, parameters: dict[str, str] | None = None
) -> None:
    """追加属性，但不清除已有同名属性（EXDATE 可能已经有多条）。"""
    if value is None:
        return
    if parameters:
        component.add(name, value, parameters=dict(parameters))
        return
    component.add(name, value)


def exdate_values(component: Any) -> list[Any]:
    """读取 VEVENT 上全部 EXDATE 取值，兼容单行多值与多行两种写法。"""
    values: list[Any] = []
    prop = component.get("EXDATE")
    if prop is None:
        return values
    groups = prop if isinstance(prop, list) else [prop]
    for group in groups:
        candidates = getattr(group, "dts", None)
        if not isinstance(candidates, list):
            candidates = [group]
        for candidate in candidates:
            value = getattr(candidate, "dt", None)
            if value is None:
                value = getattr(candidate, "value", None)
            if isinstance(value, (date, datetime)):
                values.append(value)
    return values


def update_icalendar_component(component: Any, title: str, start: datetime | date, end: datetime | date, location: str) -> None:
    """更新 VEVENT 组件中的可编辑字段。"""
    set_icalendar_property(component, "SUMMARY", title)
    set_icalendar_property(component, "DTSTART", normalize_temporal(start))
    set_icalendar_property(component, "DTEND", normalize_temporal(end))
    set_icalendar_property(component, "LOCATION", location or None)


def same_cached_start(row_start: Any, target: Any, tz: timezone | ZoneInfo) -> bool:
    """判断缓存行的开始时间与目标时间是否为同一时刻（且形态一致）。"""
    parsed = parse_iso_temporal(row_start)
    if parsed is None:
        return False
    if isinstance(parsed, datetime) != isinstance(target, datetime):
        return False
    if isinstance(parsed, datetime) and isinstance(target, datetime):
        return temporal_to_utc(parsed, tz) == temporal_to_utc(target, tz)
    return parsed == target


def find_cached_instance_index(
    rows: list[dict[str, Any]], uid: str, href: str, key: str, replaced_start: Any
) -> int:
    """定位缓存中要替换的那一条实例记录；无法确定时返回 -1（绝不误删其它实例）。"""
    candidates = [
        index
        for index, row in enumerate(rows)
        if str(row.get("uid", "")) == uid
        and ((href and row.get("href") == href) or (key and row.get("calendar_url") == key))
    ]
    if not candidates:
        candidates = [index for index, row in enumerate(rows) if str(row.get("uid", "")) == uid]
    if not candidates:
        return -1
    if replaced_start is not None:
        tz = get_timezone()
        for index in candidates:
            if same_cached_start(rows[index].get("start", ""), replaced_start, tz):
                return index
        return -1
    if len(candidates) == 1:
        return candidates[0]
    return -1


def update_cache_after_edit(
    uid: str,
    key: str,
    raw_name: str,
    href: str,
    event_data: dict[str, Any],
    replaced_start: Any = None,
) -> None:
    """编辑成功后同步更新本地缓存。

    只替换目标实例：同一 uid 的其它展开实例（循环日程）必须原样保留，
    因此这里按 (uid, href/日历, 原始开始时间) 精确替换，绝不能按 uid 整体删除。
    """
    with _sync_lock:
        cache, tokens = load_cache_bundle()
        rows = cache.setdefault("events", [])
        index = find_cached_instance_index(rows, uid, href, key, replaced_start)
        if index >= 0:
            rows.pop(index)
        else:
            app.logger.warning(
                "缓存中没有定位到要替换的实例（uid=%s href=%s start=%s），只追加新记录",
                uid,
                href,
                replaced_start,
            )
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


def remove_cached_instance(uid: str, key: str, href: str, replaced_start: Any) -> None:
    """删除循环日程的某一次实例后，从缓存中只移除那一条记录。"""
    with _sync_lock:
        cache, tokens = load_cache_bundle()
        rows = cache.setdefault("events", [])
        index = find_cached_instance_index(rows, uid, href, key, replaced_start)
        if index >= 0:
            rows.pop(index)
        else:
            app.logger.warning(
                "缓存中没有定位到已删除的实例（uid=%s href=%s start=%s），可能已被替换或尚未同步",
                uid,
                href,
                replaced_start,
            )
        cache["updated_at"] = iso_now()
        save_cache_bundle(cache, tokens)


def remove_cached_series(uid: str, key: str, href: str) -> None:
    """删除整个日程（含循环系列）后，从缓存中移除该 uid 的全部实例。

    这是唯一允许按 uid 整体移除的位置：iCloud 上的整个资源都已经删除。
    """
    with _sync_lock:
        cache, tokens = load_cache_bundle()
        rows = cache.get("events", [])
        kept = [row for row in rows if str(row.get("uid", "")) != uid]
        cache["events"] = kept
        cache["updated_at"] = iso_now()
        save_cache_bundle(cache, tokens)
        app.logger.info(
            "缓存已移除日程 %s 的 %d 条记录（日历 %s）", uid, len(rows) - len(kept), key or href
        )


def json_response(payload: dict[str, Any], status: int = 200) -> Response:
    """返回禁止缓存的 JSON 响应。"""
    response = jsonify(payload)
    response.status_code = status
    response.headers["Cache-Control"] = "no-store"
    return response


@app.get("/")
def index() -> Response:
    """返回静态前端页面。"""
    response = send_from_directory(app.root_path, "index.html")
    response.headers["Cache-Control"] = "no-store, max-age=0"
    response.headers["Pragma"] = "no-cache"
    return response


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
        if is_retryable_remote_error(exc):
            return json_response(
                {
                    "error": "连接 iCloud 失败，请稍后重试",
                    "detail": "网络中断或 iCloud 限流，已自动重试仍未成功。",
                },
                status=503,
            )
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

        calendars = calendars_with_retry()
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
        if is_retryable_remote_error(exc):
            app.logger.exception("写入 iCloud 日历时网络连接被中断")
            return json_response(
                {
                    "error": "连接 iCloud 失败，请稍后重试",
                    "detail": "网络中断或 iCloud 限流，已自动重试仍未成功。",
                },
                status=503,
            )
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


def is_precondition_failed(exc: Exception) -> bool:
    """判断异常是否属于 ETag/Schedule-Tag 412 冲突。"""
    if isinstance(exc, (caldav_error.ETagMismatchError, caldav_error.ScheduleTagMismatchError)):
        return True
    message = f"{type(exc).__name__}: {exc}".lower()
    return "412" in message or "precondition failed" in message


def load_latest_event(calendar: Any, uid: str, href: str = "") -> Any:
    """重新从服务器读取事件，刷新数据和最新 ETag；优先按 URL 读取。"""
    if href:
        event = load_event_by_href(calendar, href)
        app.logger.info("准备更新事件 uid=%s url=%s etag=%s", uid, object_href(event), event.etag)
        return event
    try:
        event = calendar.event_by_uid(uid)
    except Exception as exc:
        raise EventNotFoundError(
            f"无法读取事件 {uid}：{type(exc).__name__}: {exc}"
        ) from exc
    if event is None:
        raise EventNotFoundError(f"未找到事件：{uid}")
    event.load()
    app.logger.info("准备更新事件 uid=%s url=%s etag=%s", uid, object_href(event), event.etag)
    return event


def resource_master_component(icalendar_instance: Any) -> Any | None:
    """返回资源里不带 RECURRENCE-ID 的主 VEVENT（循环日程的 master）。"""
    components = list(icalendar_instance.walk("VEVENT")) if icalendar_instance is not None else []
    for component in components:
        if not component.get("RECURRENCE-ID"):
            return component
    return components[0] if components else None


def component_is_all_day(component: Any) -> bool:
    """判断 VEVENT 的 DTSTART 是否为全天（VALUE=DATE）形态。"""
    start = property_value(component, "dtstart")
    return isinstance(start, date) and not isinstance(start, datetime)


def component_has_recurrence(component: Any) -> bool:
    """判断 VEVENT 是否带循环规则（RRULE/RDATE）。"""
    return bool(component.get("RRULE") or component.get("RDATE"))


def resource_is_recurring(event: Any) -> bool:
    """判断事件资源是否属于循环日程（master 带规则，或资源里已有 RECURRENCE-ID）。"""
    icalendar_instance = event.icalendar_instance
    master = resource_master_component(icalendar_instance)
    if master is not None and component_has_recurrence(master):
        return True
    if icalendar_instance is None:
        return False
    return any(component.get("RECURRENCE-ID") for component in icalendar_instance.walk("VEVENT"))


def temporal_instant(value: Any, tz: timezone | ZoneInfo) -> tuple[str, Any] | None:
    """把日期/时间转换成可直接比较的“同一瞬间”标识。"""
    if isinstance(value, datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=tz)
        return ("datetime", value.astimezone(timezone.utc))
    if isinstance(value, date):
        return ("date", value)
    return None


def mirror_master_temporal(master: Any, value: Any, tz: timezone | ZoneInfo) -> tuple[Any, dict[str, str] | None]:
    """按 master 的 DTSTART 形态生成 override 的取值与参数。

    全天 → VALUE=DATE；master 带 TZID → 使用同一个 TZID；UTC → Z；
    浮动时间（无时区）→ 保持无时区的本地时间，避免改变“同一瞬间”的含义。
    """
    master_start = property_value(master, "dtstart")
    if not isinstance(master_start, datetime):
        if isinstance(value, datetime):
            value = value.astimezone(tz).date() if value.tzinfo is not None else value.date()
        return value, None
    if not isinstance(value, datetime):
        value = datetime.combine(value, time.min, tzinfo=tz)
    elif value.tzinfo is None:
        value = value.replace(tzinfo=tz)

    property_object = master.get("DTSTART")
    tzid = ""
    if property_object is not None:
        tzid = str(property_object.params.get("TZID") or "")
    if tzid:
        try:
            zone: Any = ZoneInfo(tzid)
        except Exception:
            zone = tz
        return value.astimezone(zone), {"TZID": tzid}
    if master_start.tzinfo is None:
        return value.astimezone(tz).replace(tzinfo=None), None
    return value.astimezone(timezone.utc), None


def shift_sequence_and_stamp(component: Any) -> None:
    """递增 SEQUENCE 并刷新 DTSTAMP，让 iCloud 接受这次改动。"""
    try:
        next_sequence = int(component.get("SEQUENCE") or 0) + 1
    except Exception:
        next_sequence = 1
    set_icalendar_property(component, "SEQUENCE", next_sequence)
    set_icalendar_property(component, "DTSTAMP", normalize_temporal(utc_now()))


def apply_occurrence_changes(
    event: Any,
    title: str,
    start: datetime | date,
    end: datetime | date,
    location: str,
    recurrence_id: datetime | date,
) -> None:
    """只把修改写入循环日程的单个实例（RECURRENCE-ID override）。

    只改目标 override 的 UID/DTSTART/DTEND/SUMMARY/LOCATION/SEQUENCE/DTSTAMP，
    master 的 DTSTART/DTEND/RRULE 一律不动，仅在 master 上递增 SEQUENCE 与刷新 DTSTAMP。
    """
    tz = get_timezone()
    icalendar_instance = event.icalendar_instance
    master = resource_master_component(icalendar_instance)
    if master is None:
        raise ValueError("无法解析事件内容")

    master_all_day = component_is_all_day(master)
    target_all_day = isinstance(start, date) and not isinstance(start, datetime)
    if master_all_day != target_all_day:
        raise ValueError("循环日程不支持在定时日程与全天日程之间切换")

    target_key = temporal_instant(recurrence_id, tz)
    override = None
    for component in list(icalendar_instance.walk("VEVENT")):
        if component is master or not component.get("RECURRENCE-ID"):
            continue
        if temporal_instant(property_value(component, "recurrence-id"), tz) == target_key:
            override = component
            break

    uid = str(property_value(master, "uid") or "")
    if override is None:
        override = icalendar.Event()
        recurrence_value, recurrence_parameters = mirror_master_temporal(master, recurrence_id, tz)
        if uid:
            set_icalendar_property(override, "UID", uid)
        set_icalendar_property(override, "RECURRENCE-ID", recurrence_value, recurrence_parameters)
        icalendar_instance.add_component(override)
        app.logger.info("新建循环日程实例 override：uid=%s recurrence-id=%s", uid, recurrence_value)
    else:
        app.logger.info("更新已有循环日程实例 override：uid=%s", uid)

    start_value, start_parameters = mirror_master_temporal(master, start, tz)
    end_value, end_parameters = mirror_master_temporal(master, end, tz)
    if uid:
        set_icalendar_property(override, "UID", uid)
    set_icalendar_property(override, "DTSTART", start_value, start_parameters)
    set_icalendar_property(override, "DTEND", end_value, end_parameters)
    set_icalendar_property(override, "SUMMARY", title)
    set_icalendar_property(override, "LOCATION", location or None)
    shift_sequence_and_stamp(override)
    shift_sequence_and_stamp(master)
    event.data = icalendar_instance


def cancel_occurrence(event: Any, recurrence_id: datetime | date) -> Any:
    """删除循环日程的某一次实例：移除对应 override，并在 master 上写入 EXDATE。

    recurrence_id 表示「前端看到的这一次实例的开始时间」：
    - 命中某个 override 的 RECURRENCE-ID（就地修改）→ 移除该 override 并取消该槽位；
    - 命中某个 override 的 DTSTART（实例被移动过）→ 移除该 override 并取消它的原始槽位；
    - 都没有命中 → 直接在 master 上取消该时间。
    只给 master 追加 EXDATE，master 的 DTSTART/DTEND/RRULE 一律不动。
    """
    tz = get_timezone()
    icalendar_instance = event.icalendar_instance
    master = resource_master_component(icalendar_instance)
    if master is None:
        raise ValueError("无法解析事件内容")

    target_key = temporal_instant(recurrence_id, tz)
    if target_key is None:
        raise ValueError("recurrenceId 必须是日期或日期时间")

    cancel_value: Any = recurrence_id
    uid = str(property_value(master, "uid") or "")
    for component in list(icalendar_instance.walk("VEVENT")):
        if component is master:
            continue
        rid = property_value(component, "recurrence-id")
        if rid is None:
            continue
        if temporal_instant(rid, tz) == target_key:
            icalendar_instance.subcomponents.remove(component)
            cancel_value = rid
            app.logger.info("删除循环日程的本次：移除就地修改的 override（uid=%s）", uid)
            continue
        if temporal_instant(property_value(component, "dtstart"), tz) == target_key:
            icalendar_instance.subcomponents.remove(component)
            cancel_value = rid
            app.logger.info("删除循环日程的本次：移除已移动的 override（uid=%s）", uid)

    existing = [temporal_instant(value, tz) for value in exdate_values(master)]
    if temporal_instant(cancel_value, tz) not in existing:
        value, parameters = mirror_master_temporal(master, cancel_value, tz)
        append_icalendar_property(master, "EXDATE", value, parameters)
        app.logger.info("删除循环日程的本次：写入 EXDATE %s（uid=%s）", value, uid)
    else:
        app.logger.info("删除循环日程的本次：该实例已被 EXDATE 排除（uid=%s）", uid)

    shift_sequence_and_stamp(master)
    event.data = icalendar_instance
    return event


def save_cancelled_event(event: Any, recurrence_id: datetime | date) -> Any:
    """取消循环日程的某一次实例并保存；遇到 412 时重新读取资源后重试一次。"""
    cancel_occurrence(event, recurrence_id)
    try:
        put_event_with_explicit_if_match(event)
        return event
    except Exception as exc:
        if not is_precondition_failed(exc):
            raise
        app.logger.warning("取消循环日程的本次遇到 412，重新读取资源后重试：%s", object_href(event))
        latest = reload_event_resource(event)
        cancel_occurrence(latest, recurrence_id)
        try:
            put_event_with_explicit_if_match(latest)
        except Exception as retry_exc:
            if is_precondition_failed(retry_exc):
                raise EventConflictError(
                    "iCloud 拒绝了本次修改（412），日程可能已在其他设备上被改动，请刷新后重试"
                ) from retry_exc
            raise
        return latest


def apply_event_changes(
    event: Any,
    title: str,
    start: Any,
    end: Any,
    location: str,
    recurrence_id: datetime | date | None = None,
) -> None:
    """把编辑字段写入最新获取的事件组件。

    循环日程（master 带 RRULE/RDATE，或资源里已有 RECURRENCE-ID override）必须带
    recurrenceId，只修改指定的那一次实例；非循环日程保持原有行为，直接改写 DTSTART/DTEND。
    """
    component = event.icalendar_component
    if component is None:
        raise ValueError("无法解析事件内容")
    if resource_is_recurring(event):
        if recurrence_id is None:
            raise ValueError("循环日程需要指定要修改的实例")
        apply_occurrence_changes(event, title, start, end, location, recurrence_id)
        return
    update_icalendar_component(component, title, start, end, location)
    shift_sequence_and_stamp(component)
    event.data = event.icalendar_instance


def read_latest_etag(event: Any) -> str:
    """通过资源 URL 的 PROPFIND 读取最新 getetag。"""
    resource_url = object_href(event)
    if not resource_url:
        return ""
    try:
        response = event.client.propfind(
            resource_url,
            props=["getetag"],
            depth=0,
        )
        results = getattr(response, "results", None) or []
        for result in results:
            etag = result.properties.get(dav.GetEtag.tag)
            if etag:
                return str(etag)
    except Exception:
        app.logger.exception("读取事件 ETag 失败，回退到对象缓存 ETag：%s", resource_url)
    return str(event.etag or "")


def put_event_with_explicit_if_match(event: Any) -> None:
    """对具体事件资源执行 PUT，并显式携带最新 If-Match。"""
    resource_url = object_href(event)
    if not resource_url:
        raise RuntimeError("事件没有可用的资源 URL，无法更新")

    etag = read_latest_etag(event)
    headers = {"Content-Type": 'text/calendar; charset="utf-8"'}
    if etag:
        headers["If-Match"] = etag
    else:
        app.logger.warning("事件未获取到 ETag，本次 PUT 不携带 If-Match：%s", resource_url)

    app.logger.info(
        "PUT 事件资源 url=%s if_match=%s",
        resource_url,
        headers.get("If-Match", "(none)"),
    )
    response = event.client.put(resource_url, event.data, headers)
    if response.status in (200, 201, 204):
        if response.headers and response.headers.get("Etag"):
            event.props[dav.GetEtag.tag] = response.headers.get("Etag")
        return
    if response.status == 412:
        raise RuntimeError("412 Precondition Failed")
    raise RuntimeError(f"PUT failed with status {response.status}")

def reload_event_resource(event: Any) -> Any:
    """按资源 URL 重新 GET 事件内容，拿到最新内容和 ETag。"""
    href = object_href(event)
    parent = getattr(event, "parent", None)
    if href and parent is not None:
        return load_event_by_href(parent, href)
    event.load()
    return event


def save_loaded_event(
    event: Any,
    title: str,
    start: Any,
    end: Any,
    location: str,
    recurrence_id: datetime | date | None = None,
) -> Any:
    """在已加载的事件上应用修改并保存；遇到 412 时重新读取资源后重试一次。"""
    apply_event_changes(event, title, start, end, location, recurrence_id)
    try:
        put_event_with_explicit_if_match(event)
        return event
    except Exception as exc:
        if not is_precondition_failed(exc):
            raise
        app.logger.warning("更新事件遇到 412，重新读取资源后重试：%s", object_href(event))
        latest = reload_event_resource(event)
        apply_event_changes(latest, title, start, end, location, recurrence_id)
        try:
            put_event_with_explicit_if_match(latest)
        except Exception as retry_exc:
            if is_precondition_failed(retry_exc):
                raise EventConflictError(
                    "iCloud 拒绝了本次修改（412），日程可能已在其他设备上被改动，请刷新后重试"
                ) from retry_exc
            raise
        return latest


def remove_event(event: Any, uid: str) -> None:
    """删除事件资源；遇到 412 时重新读取资源后重试一次。"""
    try:
        event.delete()
        return
    except Exception as exc:
        if not is_precondition_failed(exc):
            raise
        app.logger.warning("删除事件遇到 412，重新读取资源后重试：%s", uid)
    latest = reload_event_resource(event)
    try:
        latest.delete()
    except Exception as retry_exc:
        if is_precondition_failed(retry_exc):
            raise EventConflictError(
                "iCloud 拒绝了本次改动（412），日程可能已在其他设备上被改动，请刷新后重试"
            ) from retry_exc
        raise

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
        recurrence_id: datetime | date | None = None
        if str(payload.get("recurrenceId", "")).strip():
            recurrence_id = parse_datetime_input(payload.get("recurrenceId"), tz)

        def prepare(fresh: bool):
            calendars_now = get_calendars(force_refresh=fresh)
            row = cached_event_row(event_uid) or {}
            located = locate_event_for_update(calendars_now, event_uid)
            return calendars_now, row, located[0], located[1]

        calendars, cached_row, old_calendar, event = call_with_network_retry(prepare, "定位日程")
        href = object_href(event) or str(cached_row.get("href", ""))
        target_calendar = find_writable_calendar(calendars, calendar_requested)
        old_key = calendar_key(old_calendar)
        target_key = calendar_key(target_calendar)
        target_name = calendar_name(target_calendar)

        if old_key == target_key:

            def write(fresh: bool):
                target_event = event
                if fresh:
                    calendars_now = get_calendars(force_refresh=True)
                    target_event = locate_event_for_update(calendars_now, event_uid)[1]
                return save_loaded_event(target_event, title, start, end, location, recurrence_id)

            updated_event = call_with_network_retry(write, "更新日程")
            href = object_href(updated_event) or href
        else:
            if resource_is_recurring(event):
                raise ValueError("循环日程暂不支持更换日历")
            apply_event_changes(event, title, start, end, location)
            ics_data = event.icalendar_instance.to_ical()
            created_event = target_calendar.add_event(ical=ics_data)
            try:
                remove_event(event, event_uid)
            except caldav_error.NotFoundError:
                app.logger.info("源事件已不存在，视为移动完成：%s", event_uid)
            except Exception:
                try:
                    created_event.delete()
                except Exception:
                    app.logger.exception("移动事件失败且回滚失败：%s", event_uid)
                raise
            href = object_href(created_event) or href

        event_data = {
            "uid": event_uid,
            "calendar": target_name,
            "title": title,
            "start": start,
            "end": end,
            "location": location,
        }
        update_cache_after_edit(event_uid, target_key, target_name, href, event_data, recurrence_id)
    except ValueError as exc:
        return json_response({"error": str(exc)}, status=400)
    except CalendarNotFoundError as exc:
        return json_response({"error": str(exc)}, status=404)
    except EventNotFoundError as exc:
        app.logger.warning("更新事件失败：%s", exc)
        return json_response(
            {
                "error": str(exc),
                "detail": "本地缓存和 iCloud 都没有找到该日程，可能已在其他设备上删除。",
            },
            status=404,
        )
    except EventConflictError as exc:
        app.logger.warning("更新事件冲突（412）：%s", exc)
        return json_response(
            {"error": str(exc), "detail": "请先点击刷新按钮同步最新内容后再试。"},
            status=409,
        )
    except caldav_error.DAVError as exc:
        app.logger.exception("iCloud CalDAV 拒绝更新事件")
        return json_response(
            {"error": "iCloud 更新日程失败", "detail": f"{type(exc).__name__}: {exc}"},
            status=503,
        )
    except RuntimeError as exc:
        return json_response({"error": str(exc)}, status=503)
    except Exception as exc:
        if is_retryable_remote_error(exc):
            app.logger.exception("更新 iCloud 日历时网络连接被中断")
            return json_response(
                {
                    "error": "连接 iCloud 失败，请稍后重试",
                    "detail": "网络中断或 iCloud 限流，已自动重试仍未成功。",
                },
                status=503,
            )
        app.logger.exception("更新 iCloud 日历时发生错误")
        return json_response(
            {"error": "更新 iCloud 日历失败", "detail": f"{type(exc).__name__}: {exc}"},
            status=502,
        )

    return json_response({"event": temporal_event_response(event_data)})


@app.delete("/api/events/<path:event_uid>")
def delete_event(event_uid: str) -> Response:
    """删除日程：非循环日程删除整个资源；循环日程可只删这一次或删除整个系列。"""
    payload = request.get_json(silent=True)
    if payload is None:
        payload = {}
    if not isinstance(payload, dict):
        return json_response({"error": "请求体必须是 JSON 对象"}, status=400)

    scope = str(payload.get("scope") or request.args.get("scope") or "series").strip().lower()
    if scope not in {"instance", "series"}:
        return json_response({"error": "scope 只能是 instance 或 series"}, status=400)
    recurrence_raw = payload.get("recurrenceId") or request.args.get("recurrenceId", "")

    try:
        tz = get_timezone()
        recurrence_id: datetime | date | None = None
        if str(recurrence_raw or "").strip():
            recurrence_id = parse_datetime_input(recurrence_raw, tz)

        def prepare(fresh: bool):
            calendars_now = get_calendars(force_refresh=fresh)
            row = cached_event_row(event_uid) or {}
            located = locate_event_for_update(calendars_now, event_uid)
            return row, located[0], located[1]

        cached_row, old_calendar, event = call_with_network_retry(prepare, "定位日程")
        href = object_href(event) or str(cached_row.get("href", ""))
        old_key = calendar_key(old_calendar)

        if scope == "instance" and resource_is_recurring(event):
            if recurrence_id is None:
                raise ValueError("删除循环日程的某一次需要指定 recurrenceId")

            def cancel(fresh: bool):
                target = event
                if fresh:
                    calendars_now = get_calendars(force_refresh=True)
                    target = locate_event_for_update(calendars_now, event_uid)[1]
                return save_cancelled_event(target, recurrence_id)

            saved_event = call_with_network_retry(cancel, "删除循环日程的本次")
            href = object_href(saved_event) or href
            remove_cached_instance(event_uid, old_key, href, recurrence_id)
            app.logger.info("已删除循环日程的本次：uid=%s start=%s", event_uid, recurrence_id)
            return json_response(
                {
                    "deleted": True,
                    "uid": event_uid,
                    "scope": "instance",
                    "recurrenceId": temporal_to_json(recurrence_id),
                }
            )

        def remove(fresh: bool):
            target = event
            if fresh:
                calendars_now = get_calendars(force_refresh=True)
                target = locate_event_for_update(calendars_now, event_uid)[1]
            remove_event(target, event_uid)

        call_with_network_retry(remove, "删除日程")
        remove_cached_series(event_uid, old_key, href)
        app.logger.info("已删除日程：uid=%s（scope=%s）", event_uid, scope)
        return json_response({"deleted": True, "uid": event_uid, "scope": "series"})
    except ValueError as exc:
        return json_response({"error": str(exc)}, status=400)
    except EventNotFoundError as exc:
        app.logger.warning("删除日程失败：%s", exc)
        return json_response(
            {
                "error": str(exc),
                "detail": "本地缓存和 iCloud 都没有找到该日程，可能已被删除。",
            },
            status=404,
        )
    except caldav_error.NotFoundError as exc:
        app.logger.warning("删除日程时 iCloud 返回 404：%s", exc)
        return json_response({"error": "iCloud 上已不存在该日程"}, status=404)
    except EventConflictError as exc:
        app.logger.warning("删除日程冲突（412）：%s", exc)
        return json_response(
            {"error": str(exc), "detail": "请先点击刷新按钮同步最新内容后再试。"},
            status=409,
        )
    except caldav_error.DAVError as exc:
        app.logger.exception("iCloud CalDAV 拒绝删除日程")
        return json_response(
            {"error": "iCloud 删除日程失败", "detail": f"{type(exc).__name__}: {exc}"},
            status=503,
        )
    except RuntimeError as exc:
        return json_response({"error": str(exc)}, status=503)
    except Exception as exc:
        if is_retryable_remote_error(exc):
            app.logger.exception("删除 iCloud 日历时网络连接被中断")
            return json_response(
                {
                    "error": "连接 iCloud 失败，请稍后重试",
                    "detail": "网络中断或 iCloud 限流，已自动重试仍未成功。",
                },
                status=503,
            )
        app.logger.exception("删除 iCloud 日历时发生错误")
        return json_response(
            {"error": "删除 iCloud 日程失败", "detail": f"{type(exc).__name__}: {exc}"},
            status=502,
        )


@app.get("/api/status")
def api_status() -> Response:
    """返回本地缓存状态，供设置面板显示同步时间和缓存规模。"""
    try:
        cache, _ = load_cache_bundle()
        age = cache_age_seconds(cache)
        return json_response(
            {
                "updated_at": cache.get("updated_at"),
                "events": len(cache.get("events", [])),
                "calendars": len(cache.get("coverage", {})),
                "age_seconds": None if age == float("inf") else int(age),
                "cache_ttl_seconds": CACHE_TTL_SECONDS,
            }
        )
    except Exception as exc:
        app.logger.exception("读取本地缓存状态失败")
        return json_response(
            {"error": "无法读取缓存状态", "detail": f"{type(exc).__name__}: {exc}"},
            status=503,
        )


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
        if is_retryable_remote_error(exc):
            app.logger.exception("手动刷新日历时网络连接被中断")
            return json_response(
                {
                    "error": "连接 iCloud 失败，请稍后重试",
                    "detail": "网络中断或 iCloud 限流，已自动重试仍未成功。",
                },
                status=503,
            )
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
        response.headers["Access-Control-Allow-Methods"] = "GET, POST, PUT, DELETE, OPTIONS"
        response.headers["Access-Control-Allow-Headers"] = "Content-Type"
        response.headers["Vary"] = "Origin"
    return response


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "5000"))
    debug = os.environ.get("FLASK_DEBUG", "0") == "1"
    app.run(host="127.0.0.1", port=port, debug=debug)
