# Gmail 契约

本文定义 Gmail 工具、新邮件检测与确认发送的字段和语义。Agent 能读邮件、保存本地草稿，但不能发送：发送只由 Confirmation 在用户确认已保存的确切版本后执行。外发只支持纯文字；入站附件可以读取。

## 1. Agent 可见工具

| 工具 | 副作用 | 输入 | 输出 |
| --- | --- | --- | --- |
| `gmail_search` | 只读 | `query`（Gmail 搜索语法）、`max_results`（默认 10） | 邮件摘要数组（字段见下，不含正文） |
| `gmail_get_message` | 只读 | `message_id` | 邮件字段加纯文本 `body` |
| `gmail_get_thread` | 只读 | `thread_id` | `thread_id`、按时间正序的 `messages[]`（每封含 `body`） |
| `gmail_get_attachment` | 只读 | `message_id`、`attachment_id` | 文本部分为 `message_id`、`attachment_id`、`filename`、`mime_type`、`size`；原始字节作为 MCP 文件内容交付，不放进 JSON |
| `gmail_prepare_reply` | 本地写 | `source_message_id`、`to[]`、`subject`、`body` | `operation_id`、`version`、`status: "pending"`、`presented_to_user: true` |
| `gmail_prepare_email` | 本地写 | `to[]`（可以为空）、`subject`、`body` | 同上 |
| `gmail_read_draft` | 只读 | `operation_id` | 草稿全部字段：`operation_id`、`kind`（`reply` \| `new`）、`version`、`status`、`to`、`subject`、`body`；回复另有 `source_message_id`、`thread_id` |
| `gmail_update_draft` | 本地写 | `operation_id`、`expected_version`、`to[]`、`subject`、`body` | 同 `prepare`；另起新草稿时 `operation_id` 是新草稿的 |

邮件字段：`message_id`、`thread_id`、`rfc_message_id`、`from`、`to[]`、`cc[]`、`subject`、`snippet`、`received_at`（带时区的 ISO 8601）、`attachments[]`（`attachment_id`、`filename`、`mime_type`、`size`）。读取详情失败时整次失败，不返回一半的结果。

规则：

- `thread_id` 由工具按原邮件读取，不由 Agent 传入。同一原邮件已有回复操作时复用它并关联到当前任务，不用候选内容覆盖已保存的草稿（复用时候选内容也不参与校验）。用户取消过的回复不再复用：再为同一原邮件起草时新建操作，在当前位置出现新卡片，已取消的那份原样保留。
- `prepare_email` 每次调用都新建一份草稿；收件人可以稍后在卡片上补填。
- `presented_to_user: true` 表示草稿已由系统以卡片展示给用户，Agent 不必复述内容。
- 用户在对话框里发出消息（不带 `target`）时，该任务里全部 `pending` 草稿随消息登记立即改为 `cancelled`；卡片上的修改要求只定向那一份，不取消任何草稿。
- `gmail_update_draft` 对 `pending` 草稿原地另存一版；对 `cancelled` 草稿以它为底另起一份新草稿（回复沿用原邮件与往来），时间线在当前位置出现新卡片，原卡片保持已取消。同一原邮件已有未取消的回复时拒绝另起。
- 只能读取和修改与当前任务关联的草稿，其余按不存在处理。只有 `pending` 草稿可以修改；版本不匹配时拒绝。用户在卡片上的直接编辑走同一套版本规则。
- 轮次可见范围见 `v1-design.md` §3：新邮件触发轮只能读邮件，用户明确要求后才起草；发送不注册给模型。定向修改某张卡片的轮次只能读取和更新该草稿，不能新建操作。

## 2. 草稿校验与时间线

- 保存草稿只要求主题与正文非空；收件人可以为空，填写时每项都必须是有效邮箱地址；回复另外校验原邮件与往来。失败返回 `invalid_draft`，不保存。
- 确认发送时按更严的口径校验已保存版本，还要求至少一个收件人；不通过时确认被拒绝，操作保持 `pending`，可以继续编辑。
- 草稿创建、复用或修改成功后发出 `{"type":"draft_saved","item_id":"…","operation_id":"…","version":2}`；时间线位置先保存再发布，同一操作后续更新沿用原 `item_id`。事件只表示草稿可读，不表示已发送。
- 时间线（`GET /tasks/{task_id}/timeline`）按顺序返回 `text`（用户消息可带附件）、`mail_draft`、`notice` 与 `error` 四类条目；草稿项在读取时物化最新版本与执行状态。
- 卡片上的修改要求经 `POST /tasks/{task_id}/messages` 提交，带 `target: {"kind":"mail_draft","operation_id":"…"}`，该操作必须属于这个任务；本轮把这份草稿的最新完整内容交给 Agent。

## 3. 新邮件检测

- 每 10 秒查询一次 Gmail 历史增量，只接受收件箱新增邮件；首次启动只记录当前 `historyId`，不补处理此前的邮件。
- 游标存在 `<data_dir>/gmail_sync.json`（`email`、`history_id`）；账号与游标不一致时报错，不静默切换。
- 成功交给 Gateway 后才推进游标，重复通知由 Gateway 按 `source_message_id` 去重。游标失效时停止检测，并在健康检查中说明原因，不跳过缺口；其他错误保留游标，下一轮重试。
- 触发轮只带邮件与往来标识，后续由 Agent 决定；用户明确要求前不起草回复。

## 4. 确认、发送与核实

执行机制见 `v1-design.md` §4。

- 确认输入 `task_id`、`operation_id`、`version`，不带邮件内容；发送字段从该版本读取。修改后旧确认失效。
- 发送器输入 `operation_id`、`version`、`kind`、`source_message_id`、`thread_id`、`to`、`subject`、`body`。回复发送前重新读取原邮件，确认它仍属于已保存的往来，并用它的 Message-ID 设置 `In-Reply-To`；新邮件不传 `threadId`。
- Message-ID 由 `operation_id` 确定，供核实识别。
- 核实器用 `operation_id` 重建 Message-ID，核对收件人、主题、正文（回复还要核对往来与 `In-Reply-To`）。查不到不能证明没发，因此核实只会把 `unknown` 升级为 `sent`。核实只由 `POST /operations/{operation_id}/verification` 显式发起。
- 同一操作只发送一次；重复确认返回已有状态；`unknown` 没有重发入口。
- 用户可以取消 `pending` 草稿：输入同确认（`task_id`、`operation_id`、`version`），版本不匹配时拒绝；取消后状态为 `cancelled`，不产生执行记录，也不向 Agent 回传结果。重复取消返回已有状态；已确认的操作不能取消。

## 5. 状态与结果

操作 `type` 为 `mail`，`status` 取值：

| `status` | 含义 | 结果字段 |
| --- | --- | --- |
| `pending` | 草稿已保存，可编辑，等待确认 | — |
| `sending` | 已确认，正在发送 | — |
| `sent` | Gmail 返回了明确的成功证据 | `message_id` |
| `failed` | 明确未发送成功 | `reason` |
| `unknown` | 已进入执行，但证据不足以判断是否发出 | `reason` |
| `cancelled` | 用户取消，不会发送，不再能编辑或确认 | — |

错误：通用错误见 `v1-design.md` §3；本域另有 `invalid_draft`（422，附 `errors[]`）。

## 6. HTTP 接口

| 接口 | 用途 |
| --- | --- |
| `GET /api/operations/{operation_id}/draft?version=` | 读取草稿的指定版本或最新版本 |
| `PATCH /api/operations/{operation_id}/draft` | 卡片上直接编辑：`expected_version`、`to`、`subject`、`body` |
| `POST /api/tasks/{task_id}/confirmations` | 确认：`operation_id`、`version` |
| `POST /api/tasks/{task_id}/cancellations` | 取消：`operation_id`、`version` |
| `GET /api/operations/{operation_id}/execution` | 当前状态、确认信息与已保存的结果 |
| `POST /api/operations/{operation_id}/verification` | 显式核实 `unknown` |

确认、执行状态与核实三个接口与日程共用。
