# AGENTS.md

## 当前版本
- 版本：v0.3 新增定时更新、添加和修改日程
- 状态：已完成两个月缓存、10 分钟后台增量同步、手动刷新、字体设置、超长全天日程压缩和事件编辑。
- 验证：Python、JavaScript 和 HTML 检查通过；缓存首次同步、缓存命中、增量更新、删除、令牌失效回退、POST 新增和 PUT 编辑均已使用模拟 CalDAV 验证。

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
- API 保持 `{ "events": [...] }` 结构；事件字段为 `calendar`、`title`、`start`、`end`、可选 `location` 和 `uid`。
- `GET /api/events` 支持可选 `start`、`end` 参数，格式为 `YYYY-MM-DD`；默认范围是今天前后各 30 天。
- `POST /api/events` 只用于新增日程，通过 CalDAV 写入 iCloud。
- `PUT /api/events/<uid>` 只用于编辑非循环日程；循环事件暂时不支持编辑。
- `POST /api/refresh` 手动触发缓存增量刷新。

## 先问再做（Ask First）
- 引入任何新的第三方依赖。
- 修改 CalDAV 查询的时间范围。
- 修改 `/api/events` 的返回结构或字段含义。
- 添加事件删除、登录、数据库或其他超出上述功能范围的功能。

## 绝不越界（Never）
- 把 Apple ID、App 专用密码、Token 写进代码或提交到 Git。
- 使用 eventkit-js 等只能在 macOS 运行的库。
- 一次修改超过 3 个文件。
- 实现事件删除功能。
- 提交 `.venv`、`__pycache__`、本地缓存或任何敏感配置。

## 当前功能边界
- `/api/events` 默认查询今天前后各 30 天；前端离开已加载范围时会按视图范围重新请求。
- 日、周、月视图共用同一份事件数据；视图范围超出缓存时自动请求新范围。
- 月视图按周日至周六排列，支持跨天事件横条。
- 周视图和日视图显示 00:00 至 24:00 的可滚动时间轴；全天事件单独显示。
- 时间重叠事件使用嵌套偏移展示，不再左右分列。
- 日历筛选由左侧复选框控制，不影响后端数据。
- 「计划」与「计划/作业」统一显示为「计划/作业」。
- 事件支持双击编辑非循环日程，不支持删除。
- `events_cache.json` 和 `sync_token.json` 是本地缓存，必须保持在 `.gitignore` 中。
- 字体大小与连续日程压缩阈值保存在浏览器 localStorage。
- 日历颜色内置映射：个人为绿色、家庭为黄色、课程为红色/粉色、课外为橙色、计划/作业为紫色，其他日历使用稳定的备用颜色。

## 本地运行
```powershell
cd "D:\13411\desktop\codex_projects\apple_window_calendar"
& ".\.venv\Scripts\python.exe" ".\app.py"
```

浏览器访问：`http://127.0.0.1:5000`。
