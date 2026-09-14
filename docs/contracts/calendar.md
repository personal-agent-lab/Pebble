# iCloud Calendar 工具与确认创建契约

状态：**已实现**。本文定义 Calendar 工具的输入、输出与创建边界。实现与本文冲突时先改本文。

Agent 可以查询日程和保存本地预览，但不能直接创建事件；创建只能在用户确认已保存的确切版本后由 Confirmation 执行。确认、版本、状态与结果语义与 `mail.md` 共用一套。

## 1. 时间与日历约定

- 所有时间为带偏移的 ISO 8601，例如 `2026-09-20T14:00:00+08:00`。
- 全天日程使用日期语义：`start`、`end` 为 `YYYY-MM-DD`，`end` 是不含的结束日。
- 时区以日历服务返回的偏移为准，工具不做隐式本地时区转换。
- 首版只读取重复日程并在冲突检查中展开，不支持创建重复日程。
- 首版只连接服务端配置的主日历；`calendar_id` 省略时为 `primary`，其他值拒绝。
- 读取已有事件时返回参与人；创建预览不接受参与人，也不会发送邀请。

## 2. Agent 可见工具

| 工具 | 副作用 | 说明 |
| --- | --- | --- |
| `calendar_list_events` | 只读 | 按时间范围查询日程 |
| `calendar_get_event` | 只读 | 读取单个日程完整字段 |
| `calendar_check_conflicts` | 只读 | 检查目标时间的忙碌区间与冲突事件 |
| `calendar_prepare_event` | 本地写 | 保存待确认的日程预览，不创建 |
| `calendar_read_preview` | 只读 | 读取与当前任务关联的预览 |
| `calendar_update_preview` | 本地写 | 修改预览，产生新版本 |

事件字段（`calendar_list_events`、`calendar_get_event`、`calendar_check_conflicts` 共用）：

| 字段 | 类型 | 含义 |
| --- | --- | --- |
| `event_id` | string | 日历服务的事件标识 |
| `summary` | string | 标题 |
| `location` | string \| null | 地点 |
| `start`、`end` | string | 带偏移的时间；全天日程为日期 |
| `all_day` | boolean | 是否全天 |
| `recurrence` | string[] \| null | 重复规则 |
| `status` | string | `confirmed`、`tentative` 或 `cancelled` |
| `attendees` | object[] | 每项含 `email` 与 `response_status` |
| `updated_at` | string | 带偏移的最后修改时间 |

### `calendar_list_events`

输入 `time_min`、`time_max`、可选 `calendar_id`（默认主日历）、可选 `max_results`（默认 50）。输出 `events[]`，不返回描述正文。已取消的事件同样返回并标 `status`，由模型判断。

### `calendar_get_event`

输入 `event_id`。输出上述字段，并增加 `description: string`。

### `calendar_check_conflicts`

输入 `start`、`end`、可选 `calendar_id`。输出 `busy[]`（合并后的忙碌区间，每项含 `start`、`end`）与 `conflicts[]`（落在区间内的事件）。无冲突时两个数组为空，不返回自然语言结论。`cancelled` 事件不计入忙碌。

### `calendar_prepare_event`

输入 `summary`、`start`、`end`，可选 `all_day`、`location`、`description`、`calendar_id`。输出 `operation_id: string`、`version: integer`、`status: "pending"`，不含重复的自然语言说明。

复用规则：同一任务中已存在 `pending` 日程操作，且起止时间与标题规范化后相同时复用该操作并关联到本任务，不用本次候选内容覆盖已保存预览；否则新建一份。

### `calendar_read_preview`

输入 `operation_id: string`，只能读取与当前任务关联的预览。输出 `operation_id`、`version`、`status` 及全部日程字段。

### `calendar_update_preview`

输入 `operation_id`、`expected_version: integer` 及完整日程字段集。输出 `operation_id`、新 `version`、`status: "pending"`。仅 `pending` 预览可修改；版本不匹配时拒绝，历史版本不改写。

## 3. 预览校验与通知

校验只管格式与字段完整性：`summary` 非空；`start`、`end` 格式合法；非全天日程 `end` 晚于 `start`；全天日程 `end` 不早于 `start`。校验失败返回 `DraftValidationError` 及 `errors[]`，每项含 `field` 与 `message`，不保存数据。词汇与 `mail.md` 一致，此处“草稿”指待确认的日程预览。

时间冲突与起始时间早于当前时刻都不是校验失败：冲突由 `calendar_check_conflicts` 的结果和预览展示给用户判断，需要时由 Agent 追问。

预览创建、复用或修改成功后，工具端点发出：

```json
{"type":"draft_saved","item_id":"item_123","operation_id":"op_456","version":1}
```

Runtime 在发布通知前先保存时间线位置。同一 `operation_id` 后续更新继续使用原 `item_id`；时间线卡片类型为 `calendar_preview`。

## 4. 存储与状态词汇

日程预览与邮件草稿共用 `operations` 的操作标识、版本与状态语义，预览内容存放在日历自己的表中，操作 `type` 为 `calendar`。

接入时需要一次 schema 迁移：放宽 `operations.status` 与 `task_timeline_items.kind` 的 CHECK 约束，增加 `creating`、`created` 与 `calendar_preview`。

| `status` | 含义 |
| --- | --- |
| `pending` | 预览已保存，等待审阅和确认，可编辑 |
| `creating` | 已确认，正在执行 |
| `created` | 已确认创建成功 |
| `failed` | 已明确创建失败 |
| `unknown` | 无法确定是否已创建，等待核实 |

## 5. 确认、创建与核实

确认输入为 `task_id`、`operation_id`、`version`。确认请求不重复携带日程内容，系统从指定已保存版本读取字段；内容修改后旧版本确认失效。日程创建与邮件发送分别确认、分别记录结果。

创建器输入为 `operation_id`、`version`、`summary`、`start`、`end`、`all_day`、`location`、`description`、`calendar_id`。创建时使用由 `operation_id` 确定的事件 UID（与邮件的确定 Message-ID 同一思路），供后续核实。创建前重新读取目标时间范围的日程；发现冲突时不创建，保存明确失败结果并回到澄清与重新准备。

创建结果：

| `status` | 其他字段 | 含义 |
| --- | --- | --- |
| `created` | `event_id: string` | 日历服务已返回明确成功证据 |
| `failed` | `reason: string` | 已明确未创建成功 |
| `unknown` | `reason: string` | 已进入执行，但现有证据不足以判定是否创建 |

同一操作只创建一次。重复确认只返回已保存状态；`unknown` 不可直接重试。

核实器输入与创建器相同，但不需要 `version`。它用 `operation_id` 重建事件 UID 并查询目标日历，核对时间、标题、地点与描述，并确认没有参与人或重复规则。查不到不能证明未创建，所以核实只能将 `unknown` 升级为 `created`。核实由 `POST /operations/{operation_id}/verification` 显式发起，读接口不做外部调用。

## 6. 时间线与定向消息

网页展示只读取 `GET /tasks/{task_id}/timeline`，日程预览项物化最新预览版本和当前执行状态。`POST /tasks/{task_id}/messages` 的 `target` 支持 `{"kind":"calendar_preview","operation_id":"op_456"}`：本轮把目标预览的最新完整内容交给 Agent，并只允许读取、更新该预览；不能新建日程操作或修改其他预览。

## 7. 轮次可见范围

沿用统一规则：新邮件触发轮只允许只读工具，因此 `calendar_prepare_event` 与 `calendar_update_preview` 不在其中；用户对话轮与执行结果回传轮允许只读与本地写。实际创建属于外部写，任何一轮都不暴露给模型，只由 Confirmation 调用。
