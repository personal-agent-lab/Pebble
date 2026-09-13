# Agent 新建 iCloud 日程

## 行为与边界

用户提出新建要求 → Agent 保存预览 → 用户可编辑 → 点击确认创建 → 后台写入 iCloud →
结果回到原任务。用户在对话里说“确认”不直接触发外部创建。

只支持单次定时日程：标题、开始、结束、时区、可选地点与备注。缺少开始或结束时间时
Agent 追问，不猜测时长。时区留空采用服务端默认值；预览始终展示有效时区。
不支持全天、重复事件、邀请/参会人、已有事件修改删除、日程查询、冲突检查或后台日历同步。
预览显示固定目标日历和“未检查日历冲突”，日程确认不发送邮件或邀请。

## 服务端配置

沿用现有服务启动方式，设置 `.env`：

```dotenv
PEBBLE_ICLOUD_ACCOUNT=your-apple-account@example.com
PEBBLE_ICLOUD_PASSWORD_PATH=/absolute/path/to/.data/icloud_app_password
PEBBLE_ICLOUD_CALENDAR_URL=https://pXX-caldav.icloud.com/ACCOUNT/calendars/CALENDAR/
PEBBLE_ICLOUD_TIMEZONE=Australia/Adelaide
```

将 Apple App 专用密码保存到指定文件，限制为运行服务的用户可读（例如权限 600），
文件放在已忽略的实例数据目录或仓库外。不要使用主账号密码或将密码提交到 Git。
生成 App 专用密码需启用双重认证；主账号密码变更可能撤销 App 专用密码。
参见 [Apple 官方说明](https://support.apple.com/en-ie/102654)。

目标必须是账号下可写日历的完整 CalDAV collection URL（含实际服务器分片），
不是 `icloud.com/calendar` 网页或公开订阅 URL。部署者可用 CalDAV 客户端的
`DAVClient(...).principal().calendars()` 只读发现日历，选定后填入配置；该发现能力不开放为
Agent 工具或产品日历管理页。客户端用法见 [python-caldav 文档](https://caldav.readthedocs.io/stable/tutorial.html)。

代码使用锁文件中的 caldav 3.3 和 icalendar 7.3。创建前核对账号和目标仍与预览绑定一致；
配置变更导致旧预览执行明确失败，不会将旧确认写入新目标。凭证不出现在预览、工具结果或前端中。
未配置 iCloud 时日历准备返回不可用，邮件行为保持原样。生产启动仍沿用现有 Qoder CN/Gmail 配置。

## 工具、HTTP 与存储

模型只看到三个工具：

| 工具 | 参数 | 效果 |
| --- | --- | --- |
| `calendar_prepare_event` | request_id、title、start、end、timezone、location、description | 保存本地预览 |
| `calendar_read_event_draft` | operation_id | 读取最新内容和版本 |
| `calendar_update_event_draft` | operation_id、expected_version 和完整日程字段 | 保存新版本 |

任务身份由 SDK 包装器注入。相同任务与 request_id 唯一，重试准备返回已有操作；
修改必须使用更新工具，不通过换 request_id 代替更新。不同请求不按相似标题自动合并。

- `GET /api/operations/{id}/calendar-draft?version=N`：读取当前或指定历史版本。
- `PATCH /api/operations/{id}/calendar-draft`：提交 expected_version 和完整字段。
- `POST /api/tasks/{task_id}/confirmations`：沿用 operation_id、version 请求。
- `GET /api/operations/{id}/execution`：读取确认记录及逐项结果。

日程状态为 pending → creating → created / failed / unknown。成功结果包含 uid、resource_url；
失败和待核实包含脱敏原因，执行记录保存确认及完成时间。邮件仍使用 sending / sent。

schema 4 新增 calendar_drafts、calendar_versions 和 approval_executions.result_json，
重建 operations 的状态约束；迁移在同一事务内完成并校验外键，失败回滚，保留现有邮件记录。
预览保存账号/日历绑定及稳定 UID；实际创建只读取已确认版本。账户字段仅供服务端校验，
不在预览响应中返回。预览接口不接收目标日历或账户的修改。

日期时间采用 ISO 8601。没有偏移时按指定时区解释，夏令时重叠要求明确偏移，
不存在的本地时间、偏移不匹配、结束不晚于开始、仅日期及小数秒输入均拒绝。
iCalendar 采用库转义文本，时间序列化为 UTC，并保存原时区属性；不生成 ATTENDEE 或 METHOD。

## 重复执行与结果核实

Confirmation 原子检查任务关联、预览版本及执行状态。相同操作只取得一次执行权；
重复确认返回已有状态。网页存在未保存内容或预览读取失败时禁用确认，修改后旧版本请求返回 409。

CalDAV 使用稳定 UID 派生的资源 URL 和 `If-None-Match: *`，不覆盖已有资源；
禁用重定向及限流自动重试。成功响应后读取该资源，核对 UID、标题、时间、时区、地点、备注
及无邀请/重复设置后报告 created；已有同资源也只核对，不覆盖。
超时、服务异常、内容不一致或进程中断记 unknown，不自动重试。读取失败不能证明事件不存在。

服务端核实入口是 `ConfirmationService(None).verify_calendar(operation_id)`，仅允许 unknown；
读取保存的资源和 UID，不调用 PUT。找到完整一致内容后原子更新为 created 并登记一次结果回传。
由运行中的 Gateway 调用后执行 `kick()` 调度回传；若通过维护脚本调用，回传在下次服务启动或
任务输入触发调度时处理。此入口不开放为模型工具，也不提供自动重建入口。

## 验证记录

2026-09-13：149 项 pytest 通过，包括 22 项日历用例。涵盖版本与并发确认、最终参数一致、
配置变化、认证失败、超时、重启待核实、只读核实、资源碰撞、时间歧义、HTTP 校验、
schema 3 → 4 迁移/回滚、SDK 工具及原会话结果回传；邮件回归通过。
Ruff、前端类型检查与构建通过。

浏览器以 1280×860 和 375×812 验证准备、编辑、未保存时阻止确认、刷新恢复、确认创建及
重复确认只有一条写入记录。HTTP、SQLite、网页及确认服务真实执行，模型和日历为替身；
尚未使用真实 Apple 账号创建事件，不能据此宣称真实 iCloud 已验收。

网页验收替身为 `tests.support.calendar_backend:app`，需先构建前端，指定临时
PEBBLE_DATA_DIR 和上述测试账号配置，再用 Uvicorn 启动。该后端不连接 iCloud，
创建参数记录在临时目录的 calendar_writes.jsonl，仅供测试，不装配进生产入口。

真实验收需由用户配置自己的账号，在网页审阅并确认一个测试日程，随后在 Apple 日历检查
各字段及仅创建一次；本次未执行真实外部写入。
