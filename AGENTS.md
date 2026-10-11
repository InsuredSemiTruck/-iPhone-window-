# AGENTS.md

## 当前版本
- 版本：v0.9 日程删除版
- 状态：在 v0.8 基础上加入日程删除：双击日程进入的编辑面板在「取消 / 保存」同一行、同一套按钮样式内新增「删除日程」→ 二次确认，循环日程可选「只删这一次」（移除 override + 给 master 追加 `EXDATE`）或「删除整个系列」（删除整个资源）；新增 `DELETE /api/events/<uid>`（`scope` + 可选 `recurrenceId`），缓存分别按实例或按 uid 精确移除。
- 验证：Python `py_compile` / `import app` + 路由表、假 CalDAV 集成测试（含 ICS 字节级断言，覆盖循环实例新建/更新/幂等/全天形态/EXDATE 只删此次/删除整系列/400 闸门/缓存实例替换与按 uid 移除/412 与 SSL EOF 重试）、前端 `node --check` + HTML/CSS 结构检查 + node 实跑前端请求体构造。真实 iCloud 写回验证需按 AGENTS 清单人工执行。

## 项目背景
- 通过 CalDAV 读取 iCloud 日历，并在网页中展示。
- 运行环境：Windows 本地开发。
- 后端入口：`app.py`。
- 前端入口：`index.html`，使用原生 HTML、CSS 和 JavaScript。

## 当前结构
- `icloud_calendar_probe.py`：已验证的 CalDAV 连接和事件解析逻辑。
- `app.py`：Flask 服务，提供 `/api/events`、`/api/refresh`、`/api/status` 并托管前端。
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
- `PUT /api/events/<uid>` 用于编辑日程，支持可选 `recurrenceId` 只修改循环日程的某一次实例；请求方向新增该字段，响应结构不变。
- `DELETE /api/events/<uid>` 用于删除日程：`scope` 取 `series`（默认，删除整个资源、含循环系列）或 `instance`（只删除循环日程的某一次，需带 `recurrenceId`）；非循环日程一律删除整个资源。
- `POST /api/refresh` 手动触发缓存增量刷新。
- `GET /api/status` 只读返回本地缓存状态（`updated_at`、`events`、`calendars`、`age_seconds`），供设置面板显示同步时间。
- 写回 iCalendar 必须使用 `component.add(name, value)`（配合 `component.pop`），**禁止** `component["DTSTART"] = datetime(...)`：icalendar 7.x 会把 datetime 直接 `str()` 成非法值，iCloud 会因此拒绝请求（412/400/500）。
- 访问 iCloud 的路径遇到瞬时网络故障（SSL EOF、连接重置、超时、429/5xx）必须走 `call_with_network_retry`；网络类异常不得被当作“事件不存在”吞掉，必须冒泡给重试层。

## 先问再做（Ask First）
- 引入任何新的第三方依赖。
- 修改 CalDAV 查询的时间范围。
- 修改 `/api/events` 的返回结构或字段含义。
- 添加登录、数据库或其他超出上述功能范围的功能（设置面板的同步状态与深色模式属于现有功能范围；日程删除已实现，改动交互口径前先确认）。

## 绝不越界（Never）
- 把 Apple ID、App 专用密码、Token 写进代码或提交到 Git。
- 使用 eventkit-js 等只能在 macOS 运行的库。
- 一次修改超过 3 个文件。
- 提交 `.venv`、`__pycache__`、本地缓存或任何敏感配置。

## 当前功能边界
- `/api/events` 默认查询今天前后各 30 天；前端离开已加载范围时会按视图范围重新请求。
- 日、周、月视图共用同一份事件数据；视图范围超出缓存时自动请求新范围。
- 月视图按周日至周六排列，支持跨天事件横条。
- 月视图每周 7 列严格等宽；星期表头独立一行、每列文字居中，列边界与下方日期格逐一对齐。
- 月视图日期格内部为纵向分层：日期数字（26px）→ 跨天横条区 → 普通日程 → 「其他 X 个日程...」；跨天横条固定在顶部泳道（每道 24px），横条区下方有一条浅色分隔线。
- 月视图格子行高最小 150px（`minmax(150px,1fr)`）；普通日程数量按可用高度收敛，超出部分折叠为「其他 X 个日程...」，该行始终位于格子内部最后一行、完整显示不被裁切。
- 月视图普通日程与跨天横条均为单行省略（22px 高），保证高度可预测；周视图/日视图的日程标题仍可换行。
- 非当前月份的日期数字用 `#c7c7cc`、格子背景用 `#fafafa`；今天的红色圆形标记不受影响。
- 周视图和日视图显示 00:00 至 24:00 的可滚动时间轴；全天事件单独显示。
- 周视图的表头、全天行、时间轴三行共用同一列宽变量（`--time-column-min` = max(150, 最大泳道×120)）与同一 `min-width = 64px + 列宽 × 7`，因此三行 7 列严格等宽、边界对齐、横向滚动时不会错位。
- 月视图支持长按拖动非超长日程，拖动只改变日期，保留起始时间和时间跨度。\n- 时间重叠事件在同一日期列内按 lane 左右分列，不跨列；事件标题允许换行。
- 左侧日历面板固定为 180px、字体 10px，不提供拖动或隐藏功能。\n- 日历筛选由左侧复选框控制，不影响后端数据。
- 「计划」与「计划/作业」统一显示为「计划/作业」。
- 事件支持双击打开编辑面板（编辑 + 删除）；循环日程只修改「点中的那一次」实例（RECURRENCE-ID override），删除时可选择只删这一次或删除整个系列，均需二次确认。删除按钮与「取消 / 保存」同行同款式（`.sheet-button`），确认态在同一行内切换并临时隐藏「取消 / 保存」；操作行 `flex-wrap:nowrap` 保证不另起一行。
- `PUT /api/events/<uid>` 支持可选 `recurrenceId`（日期或 ISO 日期时间，表示被修改实例的原始开始时间）：循环日程默认只改此次，未提供 `recurrenceId` 时返回 400「循环日程需要指定要修改的实例」；非循环日程带 `recurrenceId` 时行为与之前完全一致（只更新 DTSTART/DTEND）。
- 循环日程的实例级写入使用 RECURRENCE-ID override：命中已有 override（按同一瞬间匹配）则更新，否则新建 override；只改该 override 的 `UID/DTSTART/DTEND/SUMMARY/LOCATION/SEQUENCE/DTSTAMP`，取值形态镜像 master（`VALUE=DATE` / 同 `TZID` / UTC / 浮动），master 的 `DTSTART/DTEND/RRULE` 一律不重写，仅递增 `SEQUENCE` 并刷新 `DTSTAMP`。
- 循环日程暂不支持更换日历、也不支持在定时与全天之间切换（均返回 400 中文提示），避免写出非法 iCalendar。
- 编辑成功后的缓存必须按实例替换（`uid` + `href`/日历 + 原始开始时间，时间按同一瞬间比较），**禁止**按 `uid` 整体删除，否则循环日程的其它展开实例会被误删。
- `DELETE /api/events/<uid>` 的 `scope=series` 删除整个 CalDAV 资源（含循环系列的全部 override）；`scope=instance` 只在循环日程上生效：移除匹配的 override（按 `RECURRENCE-ID` 或该 override 的 `DTSTART` 命中「点中的那一次」）并在 master 上追加 `EXDATE`（形态镜像 master、已存在则跳过），master 的 `DTSTART/DTEND/RRULE` 一律不动。
- 删除后的缓存：删整条按 uid 移除该日程的全部实例（这是唯一允许按 uid 全删的位置）；只删一次按 (uid, href/日历, 该实例开始时间) 只移除那一条，其它实例必须保留。
- 删除必须二次确认：循环日程提供「只删这一次 / 删除整个系列」，非循环只提供「确认删除」；前端用「同一 uid 在已加载数据里是否出现多次」判断是否展示两个选项，判断不到时按非循环处理（文案写「删除日程」），后端以资源的 RRULE/RDATE/override 为准。
- 删除接口沿用与编辑一致的定位与错误映射：先按缓存 `calendar_url` + `href` 定位、瞬时故障走 `call_with_network_retry`、412 重读重试后仍失败返回 409、找不到返回 404、iCloud 其他错误返回 503。
- 编辑/拖动日程时先用本地缓存里的 `calendar_url` + `href` 直接定位事件（不使用 `calendar.event_by_uid`），缓存缺失时才回退 `event_by_uid` 并捕获异常；定位失败返回 404、412 冲突返回 409、iCloud 其他错误返回 503，均在日志中记录原因。
- 前端拖动日程为乐观更新：先本地移动并重绘，PUT 失败时回滚到原位置并提示「已恢复到原位置」。
- `events_cache.json` 和 `sync_token.json` 是本地缓存，必须保持在 `.gitignore` 中。
- 日历内容字体固定 10px；设置面板已移除字体滑块与压缩阈值，超长连续全天日程阈值固定为 5 天。\n- 设置面板保留齿轮图标，内容为数据同步状态（上次同步时间 + 缓存条数）、立即同步按钮和深色模式开关。\n- 深色模式开关保存在 localStorage（键 `calendar-settings-v2`）。
- CalDAV 客户端与日历列表缓存 5 分钟（`CALENDAR_CLIENT_TTL_SECONDS`，最小 30 秒），避免每次写入都重新做一次服务发现；HTTP 超时由 `CALENDAR_HTTP_TIMEOUT_SECONDS` 控制（默认 30 秒）。
- iCloud 访问遇到瞬时网络故障会丢弃连接并重试最多 3 次（0.8s、1.6s 退避）；仍然失败时返回 503 与「连接 iCloud 失败，请稍后重试」，前端提示后把拖动的事件恢复到原位置。
- 日历颜色内置映射：个人为绿色、家庭为黄色、课程为红色/粉色、课外为橙色、计划/作业为紫色，其他日历使用稳定的备用颜色。

## 本地运行
```powershell
cd "D:\13411\desktop\codex_projects\apple_window_calendar"
& ".\.venv\Scripts\python.exe" ".\app.py"
```

浏览器访问：`http://127.0.0.1:5000`。
