# Gmail 工具与确认发送契约

外发邮件只支持纯文字正文；入站附件可读取。本文定义 Gmail 工具的输入、输出、同步触发与发送边界。

Agent 可以读邮件和保存本地草稿，但不能直接发送；发送只能在用户确认已保存的确切版本后由 Confirmation 执行。

## 1. Agent 可见工具

| 工具 | 副作用 | 说明 |
| --- | --- | --- |
| `gmail_search` | 只读 | 按条件搜索邮件摘要 |
| `gmail_get_message` | 只读 | 读取单封邮件含正文 |
| `gmail_get_thread` | 只读 | 读取完整往来 |
| `gmail_get_attachment` | 只读 | 读取入站附件原始内容 |
| `gmail_prepare_reply` | 本地写 | 保存回复草稿，不发送 |
| `gmail_prepare_email` | 本地写 | 保存主动新写草稿，不发送 |
| `gmail_read_draft` | 只读 | 读取与当前任务关联的草稿 |
| `gmail_update_draft` | 本地写 | 修改草稿，产生新版本 |

### `gmail_search`

输入：

| 字段 | 类型 | 含义 |
| --- | --- | --- |
| `query` | string | Gmail 搜索条件 |
| `max_results` | integer | 最多返回数，默认 10 |

输出为邮件摘要数组。每项包含：

| 字段 | 类型 | 含义 |
| --- | --- | --- |
| `message_id` | string | Gmail 邮件 ID |
| `thread_id` | string | Gmail 往来 ID |
| `rfc_message_id` | string | 邮件头中的 Message-ID |
| `from` | string | 发件人 |
| `to` | string[] | 收件人 |
| `cc` | string[] | 抄送人 |
| `subject` | string | 主题 |
| `snippet` | string | Gmail 摘要 |
| `received_at` | string | 带时区的 ISO 8601 时间 |
| `attachments` | object[] | 附件信息，结构见下文 |

搜索结果与单封邮件使用同一组字段名，但不返回正文。邮件详情读取失败时整次调用失败，不返回一半完整的混合结果。

### `gmail_get_message`

输入 `message_id: string`。输出为上述邮件字段，并增加 `body: string` 表示纯文本正文。

### `gmail_get_thread`

输入 `thread_id: string`。输出包含 `thread_id: string` 和 `messages: object[]`。`messages` 是按时间正序排列的完整邮件，每封均含 `body`。不另外返回拼接文本或邮件总数，它们都可由 `messages` 直接得到。

### `gmail_get_attachment`

输入 `message_id: string` 和 `attachment_id: string`。输出的文本信息包含 `message_id`、`attachment_id`、`filename`、`mime_type`、`size`，附件原始字节作为 MCP 文件内容交付，不放进 JSON 或普通文本。

邮件中的 `attachments[]` 每项包含 `attachment_id: string`、`filename: string`、`mime_type: string`、`size: integer`。附件只支持读取入站内容；首版没有上传入口，外发邮件不带附件。

### `gmail_prepare_reply`

输入：

| 字段 | 类型 | 含义 |
| --- | --- | --- |
| `source_message_id` | string | 被回复的 Gmail 邮件 ID |
| `to` | string[] | 完整收件人列表 |
| `subject` | string | 完整主题 |
| `body` | string | 完整正文 |

`thread_id` 不由 Agent 传入，工具根据原邮件读取，避免原邮件与往来不匹配。同一原邮件已有回复操作时复用它并关联到当前任务，不用本次候选内容覆盖已保存草稿。

### `gmail_prepare_email`

输入 `to: string[]`、`subject: string`、`body: string`。用于不依赖已有邮件的新邮件，每次成功调用新建一份待审阅草稿。收件人可以是空列表，正文先起草、收件人稍后在卡片上补填。

`gmail_prepare_reply` 和 `gmail_prepare_email` 的输出均为 `operation_id: string`、`version: integer`、`status: "pending"`、`presented_to_user: true`，不含重复的自然语言说明。`presented_to_user` 如实陈述工具效果：草稿已由系统以审阅卡片呈现给用户，Agent 回复无需重复其内容。工具只保存本地草稿，不发送。

### `gmail_read_draft`

输入 `operation_id: string`，只能读取与当前任务关联的草稿；不属于当前任务时按不存在处理。

- 回复草稿包含 `operation_id`、`kind: "reply"`、`version`、`status`、`source_message_id`、`thread_id`、`to`、`subject`、`body`。
- 新邮件草稿包含 `operation_id`、`kind: "new"`、`version`、`status`、`to`、`subject`、`body`，不含原邮件和往来标识。

### `gmail_update_draft`

输入 `operation_id: string`、`expected_version: integer`、`to: string[]`、`subject: string`、`body: string`。输出 `operation_id`、新 `version`、`status: "pending"`、`presented_to_user: true`，语义同上。

仅 `pending` 草稿可修改；版本不匹配时拒绝，历史版本不改写。用户在卡片上的直接编辑走同一套版本规则。

## 2. 轮次可见范围

模型每轮看到的工具由注册时声明的副作用决定：

| 轮次类型 | 可见的邮件工具 |
| --- | --- |
| 新邮件触发轮 | 只读 |
| 用户对话轮 | 只读、本地写 |
| 执行结果回传轮 | 只读、本地写 |

其他域在各轮次开放的工具见各自契约（例如新邮件触发轮还可新建与修改资料，见 `personal-kb.md` 第 4 节）。因此新邮件触发轮只能读邮件，用户明确要求起草后才有准备、读取与更新草稿的工具。实际发送属外部写，任何一轮都不暴露给模型，只由 Confirmation 调用。

定向修改轮次额外绑定目标 `operation_id`：禁止准备新操作，并拒绝读取或更新其他草稿。

## 3. 草稿校验与通知

新邮件和回复共用内容校验，保存草稿只要求主题与正文非空；收件人可以为空，填写时每项必须是有效邮箱地址。回复额外校验原邮件与往来标识。校验失败返回 `DraftValidationError` 及 `errors[]`，每项含 `field` 和 `message`，不保存数据。复用已有操作时候选内容不参与校验。

确认发送按更严的发送口径校验已保存版本，额外要求至少一个收件人；不满足时确认被拒绝（`DraftValidationError`），操作保持 `pending` 且可继续编辑，不进入发送阶段。

草稿创建、复用或修改成功后，工具端点发出：

```json
{"type":"draft_saved","item_id":"item_123","operation_id":"op_123","version":2}
```

Runtime 在发布通知前先保存时间线位置。通知表示草稿可读取，不表示邮件已发送；同一 `operation_id` 后续更新继续使用原 `item_id`。

## 4. 时间线与定向消息

网页展示只读取 `GET /tasks/{task_id}/timeline`。返回的 `items` 按持久化顺序组成联合类型：用户或 Agent `text`（用户消息可带附件）、`mail_draft`、`notice`（程序生成的提示，如记忆与资料写入提示）、`error`。连续 Agent 文本增量使用同一 `item_id`；草稿项在读取时物化最新草稿版本和当前执行状态。运行结束后网页重新读取该接口，与流式状态对账。

`POST /tasks/{task_id}/messages` 输入 `message` 和可选 `target`。邮件卡片中的修改要求使用 `target: {"kind":"mail_draft","operation_id":"op_123"}`；`operation_id` 必须属于该任务。本轮把目标草稿最新完整内容交给 Agent，并只允许读取、更新该草稿。

## 5. 新邮件同步与触发

检测程序按固定间隔（默认 10 秒）查询 Gmail 历史增量，只接受收件箱的新增邮件，并把未处理邮件交给 Gateway。

- 首次启动记录当前 `historyId`，此前邮件不批量触发。
- 游标保存在 `<data_dir>/gmail_sync.json`，含 `email` 与 `history_id`；账号与游标记录不一致时明确报错，不静默切换账号。
- 邮件成功交给 Gateway 的持久化任务入口后才推进游标；重复通知由 Gateway 按 `source_message_id` 去重。
- 游标失效时明确停止检测，健康检查显示原因，需要核对后重新建立同步位置，不静默跳过缺口。其他检测错误保留游标，下一轮重新查询。

触发轮的输入只携带邮件与往来标识，摘要、建议及后续工具选择由 Agent 决定，不固化为邮件处理流水线；用户没有明确要求前不起草回复。

## 6. 确认、发送与核实

确认输入为 `task_id`、`operation_id`、`version`。确认请求不重复携带邮件内容，系统从指定已保存版本读取发送字段，并按发送口径校验（§3）：不通过时返回 `DraftValidationError`，不取得执行权。内容修改后旧版本确认失效。

发送器输入为 `operation_id`、`version`、`kind: "reply" | "new"`、`source_message_id: string | null`、`thread_id: string | null`、`to`、`subject`、`body`。回复发送前再读取原邮件，校验它仍属于已保存的往来，并使用原邮件 Message-ID 构建 In-Reply-To。新邮件的两个关联字段为 null，不传 Gmail `threadId`。

发送时使用由 `operation_id` 确定的 Message-ID，使一次操作在 Gmail 中可被唯一识别，供后续核实。

发送结果：

| `status` | 其他字段 | 含义 |
| --- | --- | --- |
| `sent` | `message_id: string` | Gmail 已返回明确成功证据 |
| `failed` | `reason: string` | 已明确未发送成功 |
| `unknown` | `reason: string` | 已进入执行，但现有证据不足以判定是否发送 |

同一操作只发送一次。重复确认只返回已保存状态；`unknown` 不可直接重发，也没有重发入口。

核实器输入与发送器相同，但不需要 `version`。它用 `operation_id` 重建确定的 Message-ID，并核对已发送邮件的标识、收件人、主题和正文；回复还要核对往来和 In-Reply-To。查不到不能证明未发送，所以核实只能将 `unknown` 升级为 `sent`，不下明确失败的结论。核实由 `POST /operations/{operation_id}/verification` 显式发起，读接口不做外部调用。

## 7. 状态和错误

| `status` | 含义 |
| --- | --- |
| `pending` | 草稿已保存，等待审阅和确认，可编辑 |
| `sending` | 已确认，正在执行 |
| `sent` | 已确认发送成功 |
| `failed` | 已明确发送失败 |
| `unknown` | 无法确定是否已发送，等待核实 |

任务关联的邮件操作 `type` 统一为 `mail`。

| 错误 | HTTP | 附加字段 |
| --- | --- | --- |
| `NotFoundError` | 404 `not_found` | — |
| `VersionConflictError` | 409 `version_conflict` | `current_version` |
| `NotEditableError` | 409 `not_editable` | `status` |
| `DraftValidationError` | 422 `invalid_draft` | `errors[]`（`field`、`message`） |
| `DependencyUnavailableError` | 503 `unavailable` | — |

工具端点把同一套错误名与字段交回模型，与 HTTP 响应体共用词汇。调用不在当轮清单里的工具按不存在处理，不解释原因。

## 8. 相关 HTTP 接口

| 接口 | 用途 |
| --- | --- |
| `GET /api/operations/{operation_id}/draft?version=` | 读取草稿指定版本或最新版本 |
| `PATCH /api/operations/{operation_id}/draft` | 用户在卡片上直接编辑，输入 `expected_version`、`to`、`subject`、`body` |
| `POST /api/tasks/{task_id}/confirmations` | 确认，输入 `operation_id`、`version` |
| `GET /api/operations/{operation_id}/execution` | 当前状态、确认信息与已保存结果 |
| `POST /api/operations/{operation_id}/verification` | 显式核实 `unknown` |

确认、执行与核实三项接口与日程创建共用（日程没有草稿读写接口），通用语义见 `v1-design.md` 第 4 节。
