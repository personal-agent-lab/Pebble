# iCloud Calendar 工具与创建契约

本文定义 Calendar 工具的输入、输出与创建边界。实现与本文冲突时先改本文。

Agent 可以查询日程，也可以在用户本轮已给出标题与起止时间时直接创建事件。日程没有待确认预览，也没有时间线卡片：创建要么在本次工具调用内完成，要么不发生。目标时间已有日程时不创建、不留任何记录，只把撞车的日程返回给模型，由模型向用户说明并请其决定改时间还是照建；用户明确要求照建时，模型带覆盖参数重新调用。每次创建都落操作记录与不可变版本，同样只创建一次。邮件发送不适用本条，仍只能由 Confirmation 在用户确认最终版本后执行。确认、版本、状态与结果语义与 `mail.md` 共用一套。

## 1. 时间与日历约定

- 所有时间为带偏移的 ISO 8601，例如 `2026-09-20T14:00:00+08:00`。
- 全天日程使用日期语义：`start`、`end` 为 `YYYY-MM-DD`，`end` 是不含的结束日。
- 时区以日历服务返回的偏移为准，工具不做隐式本地时区转换。
- 首版只读取重复日程并在冲突检查中展开，不支持创建重复日程。
- 首版只连接服务端配置的主日历；`calendar_id` 省略时为 `primary`，其他值拒绝。
- 读取已有事件时返回参与人；创建日程不接受参与人，也不会发送邀请。

## 2. Agent 可见工具

| 工具 | 副作用 | 说明 |
| --- | --- | --- |
| `calendar_list_events` | 只读 | 按时间范围查询日程 |
| `calendar_get_event` | 只读 | 读取单个日程完整字段 |
| `calendar_check_conflicts` | 只读 | 检查目标时间的忙碌区间与冲突事件 |
| `calendar_create_event` | 直接外部写 | 信息齐全时直接创建单次日程；有冲突则不创建，只返回撞车日程 |

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

### `calendar_create_event`

输入 `summary`、`start`、`end` 必填，可选 `all_day`、`location`、`description`、`calendar_id`、`overwrite_conflicts`（默认 `false`）。三项必填参数从 schema 层面强制信息齐全——缺任何一项模型无法调用本工具，只能先向用户提问，不需要额外的完整性判断。

调用后先校验字段，再按创建时的同一时间窗检查冲突：

- 有冲突且未要求覆盖：不创建，不写操作、不写内容版本、不发通知，输出 `status: "conflict"` 与 `conflicts[]`（撞车的日程，字段同事件字段）。模型据此向用户说明冲突并请其决定改时间还是照建，不得声称已创建。
- 无冲突，或用户在本轮明确表示即使冲突也要创建（模型据此把 `overwrite_conflicts` 传为真）：保存内容版本取得 `operation_id` 与 `version`，写确认记录并原子取得一次执行权后创建，输出 `operation_id`、`version` 与第 5 节的创建结果（`created` 带 `event_id`，`failed`、`unknown` 带 `reason`）。结果就地返回给模型，不登记执行结果回传轮，模型在本轮内即可如实汇报。

冲突检查与创建在同一次调用内完成，两者之间没有等待用户的间隙，因此创建器不再重查冲突；覆盖创建也不能被冲突挡住。

## 3. 字段校验

校验只管格式与字段完整性：`summary` 非空；`start`、`end` 格式合法；非全天日程 `end` 晚于 `start`；全天日程 `end` 不早于 `start`；`calendar_id` 只接受主日历。校验失败返回 `DraftValidationError` 及 `errors[]`，每项含 `field` 与 `message`，不保存数据、不创建。错误词汇与 `mail.md` 一致，模型据此向用户追问缺失或非法的字段。

时间冲突与起始时间早于当前时刻都不是校验失败。冲突由 `calendar_create_event` 在创建前检查并作为结果返回，模型把它转述给用户判断，需要时由 Agent 追问。

日程不产生时间线条目，也不发 `draft_saved` 通知：创建结果由模型在本轮的文字里如实汇报，记录留在操作与执行表中。

## 4. 存储与状态词汇

日程与邮件草稿共用 `operations` 的操作标识、版本与状态语义，日程内容存放在 `calendar_events` 与 `calendar_event_versions`，操作 `type` 为 `calendar`。每次创建都新建一个操作和它的版本 1：没有待确认阶段，也就没有可编辑的后续版本，不可变版本在这里的作用是固定执行与核实所读的字段。

| `status` | 含义 |
| --- | --- |
| `creating` | 已取得执行权，正在创建 |
| `created` | 已明确创建成功 |
| `failed` | 已明确创建失败 |
| `unknown` | 无法确定是否已创建，等待核实 |

`pending` 只在保存内容与取得执行权之间瞬时存在，外部观察不到，日程操作也不会停在等待用户确认的状态。

schema 7 曾为日程预览放宽 `operations.status` 与 `task_timeline_items.kind` 的 CHECK 约束；schema 8 取消日程卡片，时间线类型收回为 `text`、`mail_draft`、`error`，删除历史日程卡条目，并把内容表改名为 `calendar_events` 与 `calendar_event_versions`。操作与执行记录保留，`creating`、`created` 状态不变。

## 5. 创建与核实

日程只有一条创建路径：用户对话轮内的 `calendar_create_event`。确认记录由工具在同一次调用内写入（`task_id`、`operation_id`、`version`），不来自网页卡片，也不携带日程内容——执行时从已保存版本读取字段。日程与邮件分别记录结果，互不合并。

创建器输入为 `operation_id`、`version`、`summary`、`start`、`end`、`all_day`、`location`、`description`、`calendar_id`。创建时使用由 `operation_id` 确定的事件 UID（与邮件的确定 Message-ID 同一思路），供后续核实。写入用条件请求保证同一 UID 只可能创建一次；发现服务端配置的账号或日历地址与保存内容时不一致，则不创建并保存明确失败结果，由用户重新发起。

工具先写确认记录再原子取得执行权，因此同一次调用内的重复触发不会重复创建，进程中断的恢复规则与邮件一致。结果就地返回给模型，不登记执行结果回传轮。

创建结果：

| `status` | 其他字段 | 含义 |
| --- | --- | --- |
| `created` | `event_id: string` | 日历服务已返回明确成功证据 |
| `failed` | `reason: string` | 已明确未创建成功 |
| `unknown` | `reason: string` | 已进入执行，但现有证据不足以判定是否创建 |

同一操作只创建一次。重复的执行请求只返回已保存状态；`unknown` 不可直接重试。

核实器输入与创建器相同，但不需要 `version`。它用 `operation_id` 重建事件 UID 并查询目标日历，核对时间、标题、地点与描述，并确认没有参与人或重复规则。查不到不能证明未创建，所以核实只能将 `unknown` 升级为 `created`。核实由 `POST /operations/{operation_id}/verification` 显式发起，读接口不做外部调用。

## 6. 时间线与查询

日程不进入 `GET /tasks/{task_id}/timeline`，`POST /tasks/{task_id}/messages` 的 `target` 也不支持日程：没有待确认内容，就没有可定向修改的对象。创建结果由模型在本轮文字里汇报；要查记录，用 `GET /tasks/{task_id}/operations` 看操作状态，用 `GET /operations/{operation_id}/execution` 看逐项结果。

## 7. 轮次可见范围

新邮件触发轮不开放任何外部写，`calendar_create_event` 不在其中。用户对话轮允许只读、本地写与直接外部写，`calendar_create_event` 只在这一轮出现。执行结果回传轮允许只读与本地写，不含直接外部写。

这条划分是注入防御的落点：触发轮与回传轮的输入都来自系统而非用户亲自开口，两者都拿不到直接外部写，因此邮件、资料或 Skill 内容中出现的指令无法驱动日程创建。

邮件发送属于永不暴露的外部写，任何一轮都不注册给模型，只由 Confirmation 在用户确认最终版本后调用。
