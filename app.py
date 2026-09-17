#!/usr/bin/env python3
"""iCloud 日历网页版：提供未来 7 天事件的 JSON API，并托管简单前端页面。"""

from __future__ import annotations

import os
from datetime import date, datetime, timedelta, timezone
from typing import Any

import caldav
from flask import Flask, Response, jsonify, send_from_directory

# 直接复用已经验证过的 CalDAV 连接、查询和事件解析逻辑，避免重复实现。
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


def temporal_to_json(value: Any) -> str:
    """把 date/datetime 转换为前端稳定可解析的 ISO 字符串。"""
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    return str(value)


def fetch_future_events() -> list[dict[str, str]]:
    """从 iCloud CalDAV 读取未来 7 天的事件。"""
    apple_id = get_required_env("ICLOUD_APPLE_ID")
    app_password = get_required_env("ICLOUD_APP_PASSWORD")
    caldav_url = os.environ.get("ICLOUD_CALDAV_URL", DEFAULT_CALDAV_URL).strip()

    # CalDAV 查询范围固定为从当前时刻开始的未来 7 天。
    start = datetime.now(timezone.utc)
    end = start + timedelta(days=7)

    client = caldav.DAVClient(
        url=caldav_url,
        username=apple_id,
        password=app_password,
    )
    principal = client.principal()
    calendars = principal.calendars()

    rows: list[dict[str, Any]] = []

    for calendar in calendars:
        name = calendar_name(calendar)
        try:
            events = search_events(calendar, start, end)
        except Exception:
            # 个别日历可能不是事件日历，记录后继续读取其他日历。
            app.logger.exception("查询日历失败：%s", name)
            continue

        for event in events:
            try:
                for component in iter_vevents(event):
                    event_start = property_value(component, "dtstart")
                    if event_start is None:
                        continue

                    event_end_value = event_end(component, event_start)
                    summary = property_value(component, "summary")
                    location = property_value(component, "location")
                    rows.append(
                        {
                            "calendar": name,
                            "title": str(summary) if summary else "（无标题）",
                            "start": event_start,
                            "end": event_end_value,
                            "location": str(location) if location else "",
                        }
                    )
            except Exception:
                app.logger.exception("解析日历事件失败：%s", name)

    rows.sort(
        key=lambda row: (
            sort_key(row["start"]),
            row["calendar"],
            row["title"],
        )
    )

    return [
        {
            "calendar": row["calendar"],
            "title": row["title"],
            "start": temporal_to_json(row["start"]),
            "end": temporal_to_json(row["end"]),
            "location": row["location"],
        }
        for row in rows
    ]


def json_response(payload: dict[str, Any], status: int = 200) -> Response:
    """返回禁止缓存、便于前端实时展示的 JSON 响应。"""
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
    """返回未来 7 天事件，字段为日历名、标题、开始时间、结束时间和可选地点。"""
    try:
        events = fetch_future_events()
    except RuntimeError as exc:
        # 环境变量缺失属于服务端配置问题，返回明确但不泄露密码的信息。
        return json_response({"error": str(exc)}, status=503)
    except Exception as exc:
        app.logger.exception("读取 iCloud 日历时发生错误")
        return json_response(
            {
                "error": "无法读取 iCloud 日历",
                "detail": f"{type(exc).__name__}: {exc}",
            },
            status=502,
        )

    return json_response({"events": events})


@app.after_request
def add_cors_headers(response: Response) -> Response:
    """允许单独部署的前端跨域访问 API；未配置时不添加跨域头。"""
    allowed_origin = os.environ.get("ALLOWED_ORIGIN", "").strip()
    if allowed_origin:
        response.headers["Access-Control-Allow-Origin"] = allowed_origin
        response.headers["Vary"] = "Origin"
    return response


if __name__ == "__main__":
    # 本地默认监听 127.0.0.1:5000，可通过 PORT 环境变量覆盖端口。
    port = int(os.environ.get("PORT", "5000"))
    debug = os.environ.get("FLASK_DEBUG", "0") == "1"
    app.run(host="127.0.0.1", port=port, debug=debug)
