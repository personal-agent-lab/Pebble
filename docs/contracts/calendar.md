# iCloud Calendar 契约

本文定义日历工具与创建的字段和语义；授权规则见 `v1-spec.md` §3.3。日程没有待确认预览，也没有时间线卡片：创建要么在一次工具调用内完成，要么不发生。确认、版本与结果语义与 `mail.md` 相同。

## 1. 时间与日历约定

- 时间都是带偏移的 ISO 8601（如 `2026-09-20T14:00:00+08:00`）；全天日程的 `start`、`end` 为 `YYYY-MM-DD`，`end` 不含当天。时区以日历服务返回的偏移为准，工具不做隐式转换。
- 重复日程只读取，并在冲突检查中展开；不支持创建重复日程。
- 只连接服务端配置的主日历：`calendar_id` 省略时为 `primary`，其他值拒绝。
- 读取时返回参与人；创建不接受参与人，也不发邀请。

## 2. Agent 可见工具

| 工具 | 副作用 | 输入 | 输出 |
| --- | --- | --- | --- |
| `calendar_list_events` | 只读 | `time_min`、`time_max`、可选 `calendar_id`、`max_results`（默认 50） | `events[]`，不含描述；已取消的也返回，以 `status` 标明 |
| `calendar_get_event` | 只读 | `event_id` | 事件字段加 `description` |
| `calendar_check_conflicts` | 只读 | `start`、`end`、可选 `calendar_id` | `busy[]`（合并后的忙碌区间）、`conflicts[]`（落在区间内的事件）；没有冲突时都为空；已取消的不计 |
| `calendar_create_event` | 直接外部写 | 必填 `summary`、`start`、`end`；可选 `all_day`、`location`、`description`、`calendar_id`、`overwrite_conflicts`（默认 `false`） | 见下 |

事件字段：`event_id`、`summary`、`location`、`start`、`end`、`all_day`、`recurrence`、`status`（`confirmed` \| `tentative` \| `cancelled`）、`attendees[]`（`email`、`response_status`）、`updated_at`。

`calendar_create_event` 的三项必填参数在 schema 层面保证信息齐全：缺任何一项模型都无法调用，只能先问用户。调用后先校验字段，再用创建时的同一时间窗检查冲突：

- **有冲突且没要求覆盖**：不创建，不写操作、版本或通知；返回 `status: "conflict"` 与 `conflicts[]`，由模型向用户说明，不得声称已创建。
- **无冲突，或用户本轮明确要求照建**（模型传 `overwrite_conflicts: true`）：保存版本、写确认记录、原子取得执行权后创建，返回 `operation_id`、`version` 与第 4 节的结果。结果就地返回模型，不登记回传轮。

冲突检查与创建在同一次调用内完成，中间没有等待用户的间隙，所以创建器不再重复检查。

## 3. 字段校验

只校验格式与完整性：`summary` 非空；`start`、`end` 格式合法；非全天日程的 `end` 晚于 `start`，全天日程的 `end` 不早于 `start`；`calendar_id` 只能是主日历。失败返回 `invalid_draft`（附 `errors[]`），不保存、不创建；错误词汇见 `v1-design.md` §3。时间冲突与开始时间早于现在都不算校验失败。

## 4. 存储、状态与核实

内容存在 `calendar_events` 与 `calendar_event_versions`，操作 `type` 为 `calendar`。每次创建都新建一个操作及其版本 1，没有后续版本；不可变版本用来固定执行与核实时读取的字段。

| `status` | 含义 | 结果字段 |
| --- | --- | --- |
| `creating` | 已取得执行权，正在创建 | — |
| `created` | 日历服务返回了明确的成功证据 | `event_id` |
| `failed` | 明确未创建 | `reason` |
| `unknown` | 已进入执行，但证据不足以判断是否创建 | `reason` |

`pending` 只在保存内容与取得执行权之间瞬时存在，外部观察不到。

- 创建只有一条路径：用户对话轮内的 `calendar_create_event`。确认记录（`task_id`、`operation_id`、`version`）由工具在同一次调用内写入，执行时从已保存版本读取字段。
- 事件 UID 由 `operation_id` 确定，写入用条件请求保证同一 UID 只创建一次；服务端配置的账号或日历与保存内容不一致时不创建，记为明确失败。
- 核实器用 `operation_id` 重建 UID 并查询日历，核对时间、标题、地点、描述，且没有参与人和重复规则；查不到不能证明未创建，因此只会把 `unknown` 升级为 `created`。核实只由 `POST /operations/{operation_id}/verification` 显式发起。
- 同一操作只创建一次；`unknown` 不能直接重试。日程与邮件分别记录结果。

## 5. 时间线与查询

日程不进入时间线，`POST /tasks/{task_id}/messages` 的 `target` 也不支持日程。结果由模型在本轮文字里汇报；记录通过 `GET /tasks/{task_id}/operations` 与 `GET /operations/{operation_id}/execution` 查询。

## 6. 轮次可见范围

`calendar_create_event` 是直接外部写，按 `v1-design.md` §3 只在用户亲自发起的对话轮可见；新邮件触发轮与执行结果回传轮拿不到它，外部内容中的指令因此无法驱动日程创建。
