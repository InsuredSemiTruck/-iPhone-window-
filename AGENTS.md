# AGENTS.md

## 当前版本
- 版本：v0.2 初步网页版
- 状态：已完成 Flask JSON API、日/周/月三视图网页和日历筛选展示。
- 验证：Python 与 JavaScript 语法检查通过；Flask 首页返回 200；缺少环境变量时 API 返回 503。

## 项目背景
- 通过 CalDAV 读取 iCloud 日历，并在网页中展示。
- 运行环境：Windows 本地开发。
- 后端入口：`app.py`。
- 前端入口：`index.html`，使用原生 HTML、CSS 和 JavaScript。

## 当前结构
- `icloud_calendar_probe.py`：已验证的 CalDAV 连接和事件解析逻辑。
- `app.py`：Flask 服务，提供 `/api/events` 并托管前端。
- `index.html`：日、周、月三种日历视图。
- `requirements.txt`：Flask 和 caldav 依赖。
- `APPLE_DESIGN.md`：页面视觉设计参考。

## 必须遵守（Always）
- CalDAV 认证使用 App 专用密码，从环境变量读取，绝不硬编码。
- 每次改动后代码必须能直接运行，无语法错误。
- 前端继续使用原生 HTML、CSS 和 JavaScript，不引入前端框架。
- 后端继续复用 `icloud_calendar_probe.py` 的 CalDAV 查询和解析逻辑。
- API 默认保持 `{ "events": [...] }` 结构；事件字段为 `calendar`、`title`、`start`、`end`、可选 `location`。

## 先问再做（Ask First）
- 引入任何新的第三方依赖。
- 修改 CalDAV 查询的时间范围。
- 修改 `/api/events` 的返回结构或字段含义。
- 添加事件编辑、删除、登录、数据库或其他超出日历展示范围的功能。

## 绝不越界（Never）
- 把 Apple ID、App 专用密码、Token 写进代码或提交到 Git。
- 使用 eventkit-js 等只能在 macOS 运行的库。
- 一次修改超过 3 个文件。
- 提交 `.venv`、`__pycache__`、本地缓存或任何敏感配置。

## 当前功能边界
- `/api/events` 当前只查询未来 7 天，查询窗口为从当前时刻开始的 168 小时。
- 日、周、月视图共用同一份事件数据；超出后端已返回范围时，仅显示日历界面，不显示事件。
- 月视图按周日至周六排列，支持跨天事件横条。
- 周视图和日视图显示 9:00 至 21:00 的时间轴；全天事件单独显示。
- 日历筛选由左侧复选框控制，不影响后端数据。
- 日历颜色内置映射：个人为绿色、家庭为黄色、课程为红色/粉色、课外为橙色、计划/作业为紫色，其他日历使用稳定的备用颜色。

## 本地运行
```powershell
cd "D:\13411\desktop\codex_projects\apple_window_calendar"
& ".\.venv\Scripts\python.exe" ".\app.py"
```

浏览器访问：`http://127.0.0.1:5000`。
