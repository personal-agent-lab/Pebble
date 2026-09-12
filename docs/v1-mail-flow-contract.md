# 第一条邮件链：A/B 接口字段契约

本文仅定义接口输入、输出与字段语义。任务表示用户目标，一个任务可关联多项操作；邮件字段属于回复草稿。

## 1. 通用字段

| 字段 | 类型 | 含义 |
| --- | --- | --- |
| `task_id` | string | 用户任务标识，与邮件身份无关 |
| `sdk_session_id` | string 或 null | 任务关联的 Agent 会话标识，尚未关联时为 null |
| `operation_id` | string | 一项操作的标识；不同任务可关联同一操作 |
| `version` | integer | 草稿内容版本，从 1 开始 |
| `expected_version` | integer | 本次编辑所依据的版本 |
| `status` | string | 回复操作的当前状态，取值见第 8 节 |
| `created_at` | string | 带时区的 ISO 8601 创建时间 |

## 2. 任务接口

### 创建任务

输入：

| 字段 | 类型 | 含义 |
| --- | --- | --- |
| `goal` | string | 用户目标 |

输出为任务记录：

| 字段 | 类型 | 含义 |
| --- | --- | --- |
| `task_id` | string | 任务标识 |
| `goal` | string | 用户目标 |
| `sdk_session_id` | string 或 null | 关联的 Agent 会话标识 |
| `created_at` | string | 创建时间 |

### 读取任务与任务列表

- 任务详情输入：`task_id`；输出：任务记录。
- 任务列表无输入字段；输出：任务记录数组，按创建时间倒序，同时间按 `task_id` 升序。

### 关联 Agent 会话

输入：`task_id`、`sdk_session_id`（非 null）。输出：关联后的任务记录。

相同会话标识表示同一关联；与已有会话标识不同时为会话关联冲突。

### 读取任务关联操作

输入：`task_id`。输出为操作摘要数组，无关联操作时为空数组：

| 字段 | 类型 | 含义 |
| --- | --- | --- |
| `operation_id` | string | 操作标识 |
| `type` | string | 操作类型，本邮件契约取 `mail_reply` |
| `version` | integer | 当前草稿版本 |
| `status` | string | 当前操作状态 |

同一操作出现在不同任务的结果中时，表示共享同一份草稿及当前状态，不改变各任务的会话关联。

## 3. 用户消息与 Agent 事件

### 新邮件输入

输入：`task_id: string`、`sdk_session_id: string | null`、`source_message_id: string`、
`thread_id: string`。邮件标识用于定位待分析的新邮件；输出为下述 Agent 事件。
每封新邮件对应一个任务，同一邮件的重复通知对应原任务；同线程的不同邮件对应不同任务。
用户选择准备回复属于普通消息，不代表确认发送。

### 用户消息输入

消息输入：

| 字段 | 类型 | 含义 |
| --- | --- | --- |
| `task_id` | string | 当前任务 |
| `message` | string | 用户消息原文 |
| `sdk_session_id` | string 或 null | 当前任务的会话标识；首次尚未关联时为 null |

事件输出：

| `type` | 其他字段 | 含义 |
| --- | --- | --- |
| `session` | `sdk_session_id: string` | 本轮关联的会话 |
| `text` | `text: string` | 新增回复文本片段 |
| `done` | 无 | Agent 本轮正常结束 |
| `error` | `message: string` | Agent 本轮失败及原因 |

`session` 先于本轮文本事件；会话建立失败时返回 `error`。`text` 可出现多次，按顺序拼接。
一轮以 `done` 或 `error` 之一结束；二者不表示邮件发送结果。

## 4. 回复草稿

### 保存或复用回复草稿

输入：

| 字段 | 类型 | 含义 |
| --- | --- | --- |
| `task_id` | string | 本次任务 |
| `source_message_id` | string | 被回复的 Gmail 原邮件 ID |
| `thread_id` | string | 原邮件所在的 Gmail 线程 ID |
| `to` | string[] | 完整收件人地址列表，顺序保留 |
| `subject` | string | 完整主题，包含原有空白 |
| `body` | string | 完整正文，包含原有换行与空白 |

输出：`operation_id`、`version`、`status`。

首次保存返回第 1 版、`pending` 状态。同一原邮件已有回复操作时，返回该操作的当前版本与状态，
表示本次任务关联已有操作；输入的候选内容不代表已保存内容。原邮件 ID 的复用语义仅适用于邮件回复。

### 编辑回复草稿

输入：

| 字段 | 类型 | 含义 |
| --- | --- | --- |
| `operation_id` | string | 待编辑操作 |
| `expected_version` | integer | 本次编辑依据的版本 |
| `to` | string[] | 修改后的完整收件人列表 |
| `subject` | string | 修改后的完整主题 |
| `body` | string | 修改后的完整正文 |

输出：`operation_id`、`version`，其中 `version` 为本次保存后的新版本。

仅 `pending` 表示可编辑状态。成功编辑的版本为原版本加 1；旧版本内容保持原样。
原邮件 ID、线程 ID 不属于编辑字段。用户手动修改和 Agent 重写使用相同字段契约。

### 读取回复草稿

输入：`operation_id`、可选 `version`；未指定版本表示当前版本。

输出：

| 字段 | 类型 | 含义 |
| --- | --- | --- |
| `operation_id` | string | 操作标识 |
| `version` | integer | 本次读取的内容版本 |
| `status` | string | 操作当前状态，即使读取历史版本也不是历史状态 |
| `source_message_id` | string | 原邮件 ID |
| `thread_id` | string | Gmail 线程 ID |
| `to` | string[] | 对应版本的完整收件人列表 |
| `subject` | string | 对应版本的完整主题 |
| `body` | string | 对应版本的完整正文 |

### 草稿通知

| 字段 | 类型 | 含义 |
| --- | --- | --- |
| `type` | string | 固定为 `draft_saved` |
| `operation_id` | string | 可读取的操作 |
| `version` | integer | 本次保存或复用返回的版本 |

通知适用于新建、编辑及复用，表示可以读取草稿；不代表一定新增了版本，也不代表邮件已发送。

## 5. 邮件业务校验

输入：`source_message_id`、`thread_id`、`to`、`subject`、`body`，类型及含义同第 4 节。

输出：

| 字段 | 类型 | 含义 |
| --- | --- | --- |
| `valid` | boolean | 候选内容是否满足邮件业务要求 |
| `errors` | object[] | 校验原因列表；通过时为空数组 |
| `errors[].field` | string | 未通过校验的字段名 |
| `errors[].message` | string | 原因说明 |

输出不含改写后的邮件内容；校验通过不表示已保存、已确认或已发送。
具体邮件校验规则及 Gmail 协议所需的额外字段待 B 核对。

## 6. 确认与发送

### 确认输入

| 字段 | 类型 | 含义 |
| --- | --- | --- |
| `task_id` | string | 用户本次确认所在任务，也是此次执行结果的回传任务 |
| `operation_id` | string | 用户确认的操作 |
| `version` | integer | 用户审阅并确认的已保存版本 |

确认输入不含邮件正文。确认只对应指定操作及版本，不适用于修改后的新版本。
同一操作的重复确认表示查询已有状态，不代表新的发送；也不改变已确定的结果回传任务。

### 确认响应

| 字段 | 类型 | 含义 |
| --- | --- | --- |
| `operation_id` | string | 操作标识 |
| `version` | integer | 操作当前版本；确认后不再变化 |
| `status` | string | 操作当前状态，取值见第 8 节 |
| `confirmation` | object 或 null | 尚未确认时为 null；确认后含 `task_id`、`version`、`confirmed_at` |
| `result` | object 或 null | 尚无结果时为 null，否则为下文发送结果结构 |

`confirmation.task_id` 为首次成功确认的任务；`confirmation.version` 为用户确认的已保存版本。
重复确认返回首次确认的信息，不产生新记录，也不改变结果回传任务。

### 发送输入

| 字段 | 类型 | 含义 |
| --- | --- | --- |
| `operation_id` | string | 本次执行操作 |
| `version` | integer | 已确认的内容版本 |
| `source_message_id` | string | 原邮件 ID |
| `thread_id` | string | Gmail 线程 ID |
| `to` | string[] | 已确认版本的完整收件人列表 |
| `subject` | string | 已确认版本的完整主题 |
| `body` | string | 已确认版本的完整正文 |

### 发送结果

| `status` | 必需字段 | 含义 |
| --- | --- | --- |
| `sent` | `message_id: string` | 已确认发送成功，附 Gmail 已发送邮件 ID |
| `failed` | `reason: string` | 明确未发送成功，附实际原因 |
| `unknown` | `reason: string` | 操作已经进入执行阶段，但现有证据不足以确定外部发送是否发生或成功；附不确定原因 |

`unknown` 表示发送结果不确定，不是通用错误状态。调用发送函数前因版本冲突、对象不存在等被拒绝，不属于 `unknown`。
进入执行阶段后中断、调用超时或收到无法解释的发送返回，均可能无法确定实际发送结果；未分类异常或未查到邮件本身不表示明确失败。
`unknown` 不表示可以直接重发，重复确认仅返回已有状态，不重新发送。后续核实取得明确证据后，结果才可更新为 `sent` 或 `failed`。
核实后的结果沿用此结构；核实请求及所需证据字段待 B 验证后补齐。

## 7. 执行结果交回 Agent

输入：

| 字段 | 类型 | 含义 |
| --- | --- | --- |
| `task_id` | string | 本次确认所在任务；共享操作的创建任务不决定回传目标 |
| `sdk_session_id` | string | 该任务关联的 Agent 会话 |
| `operation_id` | string | 已执行操作 |
| `version` | integer | 对应确认版本 |
| `result` | object | 已保存的实际执行结果，结构见第 6 节 |

输出：第 3 节的 Agent 事件。

后续核实结果仍对应本次确认任务。其他关联任务读取相同操作状态，不因此产生向其他会话回传的输入。
Agent 的 `done` / `error` 描述后续会话结果，不改写 `result` 中的邮件结果。
结果保存后可按 `operation_id` 读取上述输入字段作为待回传数据；尚无结果或回传任务尚未关联会话时没有数据，稍后关联后即可读取。

## 8. 回复操作状态与接口错误

### 操作状态

| `status` | 含义 |
| --- | --- |
| `pending` | 草稿已保存，等待确认，可编辑 |
| `sending` | 已确认，正在执行 |
| `sent` | 已确认发送成功 |
| `failed` | 明确发送失败 |
| `unknown` | 已进入执行阶段，无法确定外部发送是否发生或成功，结果待核实 |

这些状态属于回复操作，不表示整个用户任务的状态。

### 已实现接口错误

以下名称标识当前接口错误，不规定传输方式或 HTTP 状态码。

| 名称 | 附带字段 | 含义 |
| --- | --- | --- |
| `NotFoundError` | 无约定结构化字段 | 任务、操作或指定草稿版本不存在 |
| `VersionConflictError` | `current_version: integer` | 当前版本与编辑依据版本不同 |
| `NotEditableError` | `status: string` | 操作当前不可编辑 |
| `SessionConflictError` | 无约定结构化字段 | 任务已关联不同会话 |
| `DraftValidationError` | `errors: object[]` | 邮件校验失败，元素为第 5 节的字段与原因 |

未返回成功结果不表示草稿已保存或邮件未发送；邮件实际结果以第 6 节结果字段为准。

## 9. 后台调用与历史字段

用户消息被接受后的输出为调用记录；任务详情增加 `latest_run`，尚无调用时为 null。
调用状态与回复操作状态分别表达，不相互替代。

| 字段 | 类型 | 含义 |
| --- | --- | --- |
| `run_id` | string | 一轮 Agent 输入的调用标识 |
| `task_id` | string | 所属任务 |
| `kind` | string | `new_mail`、`message` 或 `execution_result` |
| `status` | string | `pending`、`running`、`done`、`error` 或 `interrupted` |
| `error` | string 或 null | 调用失败或中断原因 |
| `created_at` | string | 接受时间 |
| `started_at` | string 或 null | 开始时间 |
| `finished_at` | string 或 null | 结束或识别为中断的时间 |

`pending` 表示尚未开始，`running` 表示正在调用，`interrupted` 表示调用中断且无法确认完整结束。
发给网页的 Agent 事件增加 `run_id`，表示事件所属调用。重复确认不产生新的发送或结果回传。

历史读取输入：`task_id`、`sdk_session_id`（可空）；输出为按顺序排列的消息数组。
每条消息含 `role: string`（`user` 或 `assistant`）、`text: string`。
网页历史响应含 `task_id`、`sdk_session_id`、`messages`；尚无会话时 `messages` 为空数组。

依赖未接入错误为 `unavailable`，附 `message: string`，表示相关工作未被接受，
不属于邮件的 failed 或 unknown 结果。
