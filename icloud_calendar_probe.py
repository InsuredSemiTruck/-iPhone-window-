#!/usr/bin/env python3
"""iCloud CalDAV 最小技术验证：列出日历并打印未来 7 天的事件。"""

from __future__ import annotations

import os
import sys
from datetime import date, datetime, timedelta, timezone
from typing import Any, Iterable


DEFAULT_CALDAV_URL = "https://caldav.icloud.com/"


def get_required_env(name: str) -> str:
    """读取必需的环境变量，并在缺失时给出明确错误。"""
    value = os.environ.get(name, "").strip()
    if not value:
        raise RuntimeError(f"缺少环境变量 {name}")
    return value


def calendar_name(calendar: Any) -> str:
    """兼容不同 caldav 版本，尽量读取日历显示名称。"""
    name = getattr(calendar, "name", None)
    if not name:
        get_display_name = getattr(calendar, "get_display_name", None)
        if callable(get_display_name):
            name = get_display_name()
    return str(name or "(未命名日历)")


def iter_vevents(event: Any) -> Iterable[Any]:
    """从 caldav Event 对象中取出一个或多个 VEVENT 组件。"""
    # 较新的 caldav 版本推荐通过 icalendar_instance 访问组件。
    icalendar_instance = getattr(event, "icalendar_instance", None)
    if icalendar_instance is not None:
        yield from icalendar_instance.walk("VEVENT")
        return

    # 兼容旧版本；正常情况下不会走到这个分支。
    vobject_instance = getattr(event, "vobject_instance", None)
    if vobject_instance is None:
        return
    vevent = getattr(vobject_instance, "vevent", None)
    if isinstance(vevent, list):
        yield from vevent
    elif vevent is not None:
        yield vevent


def property_value(component: Any, name: str) -> Any:
    """同时兼容 icalendar 与 vobject 的属性取值方式。"""
    getter = getattr(component, "get", None)
    prop = getter(name) if callable(getter) else None
    if prop is None:
        # vobject 的组件通常把内容保存在 contents 字典中。
        contents = getattr(component, "contents", {})
        candidates = contents.get(name.lower(), [])
        prop = candidates[0] if candidates else None
    if prop is None:
        return None

    # icalendar 的 DTSTART/DTEND 使用 .dt，vobject 使用 .value。
    if hasattr(prop, "dt"):
        return prop.dt
    if hasattr(prop, "value"):
        return prop.value
    return prop


def event_end(component: Any, start: Any) -> Any:
    """读取 DTEND；缺失时按 DURATION 或 iCalendar 默认规则补全。"""
    end = property_value(component, "dtend")
    if end is not None:
        return end

    duration = property_value(component, "duration")
    if isinstance(duration, timedelta):
        return start + duration

    # 全天事件没有 DTEND 时默认持续一天；普通事件默认零时长。
    if isinstance(start, date) and not isinstance(start, datetime):
        return start + timedelta(days=1)
    return start


def search_events(calendar: Any, start: datetime, end: datetime) -> list[Any]:
    """查询时间窗口内的事件，并兼容新旧 caldav API。"""
    search = getattr(calendar, "search", None)
    if callable(search):
        # expand=True 会把重复事件展开成实际发生的各个事件。
        return list(search(start=start, end=end, event=True, expand=True))

    # caldav 1.0 之前的常见接口；新版本中作为兼容后备。
    return list(calendar.date_search(start=start, end=end, expand=True))


def format_time(value: Any) -> str:
    """用易读但保留时区的形式打印日期时间或全天日期。"""
    if value is None:
        return "(未提供)"
    if isinstance(value, datetime):
        return value.isoformat(sep=" ", timespec="minutes")
    if isinstance(value, date):
        return f"{value.isoformat()} (全天)"
    return str(value)


def sort_key(value: Any) -> datetime:
    """把 date/datetime 统一成可排序的 aware datetime。"""
    if isinstance(value, datetime):
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value
    if isinstance(value, date):
        return datetime.combine(value, datetime.min.time(), tzinfo=timezone.utc)
    return datetime.max.replace(tzinfo=timezone.utc)


def main() -> int:
    # 先检查依赖，避免把安装问题误判成认证问题。
    try:
        import caldav
    except ImportError:
        print("缺少 caldav 库，请先运行：python -m pip install caldav", file=sys.stderr)
        return 2

    try:
        apple_id = get_required_env("ICLOUD_APPLE_ID")
        app_password = get_required_env("ICLOUD_APP_PASSWORD")
    except RuntimeError as exc:
        print(str(exc), file=sys.stderr)
        print("请设置 ICLOUD_APPLE_ID 和 ICLOUD_APP_PASSWORD 后重试。", file=sys.stderr)
        return 2

    # 默认使用 iCloud 全球 CalDAV 入口；如账号在特定区域，可通过环境变量覆盖。
    caldav_url = os.environ.get("ICLOUD_CALDAV_URL", DEFAULT_CALDAV_URL).strip()

    # CalDAV 时间范围使用 aware UTC，避免“有/无时区”比较报错。
    start = datetime.now(timezone.utc)
    end = start + timedelta(days=7)

    try:
        client = caldav.DAVClient(
            url=caldav_url,
            username=apple_id,
            password=app_password,
        )

        # principal() 会执行 CalDAV 服务发现和认证，是验证连接是否成功的第一步。
        principal = client.principal()
        calendars = principal.calendars()
    except Exception as exc:
        print(f"CalDAV 连接或认证失败：{type(exc).__name__}: {exc}", file=sys.stderr)
        print("优先检查 App 专用密码、Apple ID、网络和 CalDAV 地址。", file=sys.stderr)
        return 1

    print(f"发现 {len(calendars)} 个日历：")
    for index, calendar in enumerate(calendars, start=1):
        print(f"  {index}. {calendar_name(calendar)}")

    print()
    print(f"查询未来 7 天的事件（UTC）：{format_time(start)} 至 {format_time(end)}")
    print()

    rows: list[dict[str, Any]] = []
    failures: list[str] = []

    for calendar in calendars:
        name = calendar_name(calendar)
        try:
            events = search_events(calendar, start, end)
        except Exception as exc:
            # 一个日历失败不应阻止其他日历输出，例如提醒事项日历可能不支持 VEVENT 查询。
            failures.append(f"{name}: {type(exc).__name__}: {exc}")
            continue

        for event in events:
            try:
                for component in iter_vevents(event):
                    event_start = property_value(component, "dtstart")
                    if event_start is None:
                        continue
                    event_end_value = event_end(component, event_start)
                    summary = property_value(component, "summary")
                    rows.append(
                        {
                            "calendar": name,
                            "summary": str(summary) if summary else "(无标题)",
                            "start": event_start,
                            "end": event_end_value,
                        }
                    )
            except Exception as exc:
                failures.append(f"{name} 中单个事件解析失败：{type(exc).__name__}: {exc}")

    rows.sort(key=lambda row: sort_key(row["start"]))

    if not rows:
        print("未来 7 天没有查询到事件。")
    else:
        for index, row in enumerate(rows, start=1):
            print(f"{index}. 日历：{row['calendar']}")
            print(f"   标题：{row['summary']}")
            print(f"   开始：{format_time(row['start'])}")
            print(f"   结束：{format_time(row['end'])}")
            print()

    if failures:
        print("部分查询或解析失败：")
        for message in failures:
            print(f"  - {message}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())

