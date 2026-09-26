# 轨迹追踪与可观测性契约

产品行为以 docs/observability-spec.md 为准。执行轨迹继续使用 task_timeline_items 与 agent_runs（contracts/skill.md §3）；观测只补这两处没有的运行事实。

## 1. 存储

在实例 SQLite 增加两张从属表，不建立通用追踪树：

| 表 | 一条记录 | 字段 |
| --- | --- | --- |
| run_observations | 一轮的运行摘要，run_id 为主键并引用 agent_runs，删除轮次时级联删除 | materials、sdk_result、context_before、context_after、updated_at |
| observation_steps | 本轮发生的一次工具尝试、压缩或静默降级 | step_id、run_id、kind（tool/compact/degraded）、code、status（running/ok/error/denied）、started_at、ended_at、item_id、tool_call_id、detail |

轮次的触发类别、模型、状态与起止时间从 agent_runs 和 tasks 读取；邮件与日程结果从 operations、approval_executions 读取，不重复保存。item_id 只引用时间线工具条目，允许为空（例如权限拒绝或轨迹写入失败）。run_id 外键级联删除；观测层不修改业务状态。

materials 只列每类实际装配材料的名称与字符数，以及明确跳过的类别和原因；不存正文或内容哈希。sdk_result 只存 SDK ResultMessage 给出的 duration_ms、duration_api_ms、num_turns、结束原因、本轮各次 AssistantMessage.usage 的请求级用量、ResultMessage.usage 的末次请求读数（result_usage），以及轮末会话累计快照 session_totals（model_usage 汇总的 token 与 total_credits），读取面用快照差值推算本轮消耗。context_before/after 只存 get_context_usage() 给出的已用比例与压缩阈值。字段缺失保留空值，不补零。

observation_steps.detail 按 kind 构造，禁止整包序列化：工具只存工具名、来源（mcp/builtin）和返回字符数；压缩只存前后占用；降级只存类别、原因码与必要计数。正文、工具参数与返回仍只在时间线保存一份。

## 2. 采集与关联

- Gateway 在一轮开始和结束时使用已有 agent_runs；Agent 在材料装配后记摘要，逐次读取 AssistantMessage.usage，在 ResultMessage 到达时记总时长与调用次数。手动或 SDK 上报的压缩记一次步骤，并记压缩前后占用；没有实际边界信号就不声称发生了压缩。
- Pebble MCP 工具沿用 agent/mcp.py 的轨迹写入，在同一调用边界开始和结束观测步骤；以现有 tool_call_id 和新建的轨迹 item_id 关联。业务返回 isError 时步骤为 error；unknown_tool、wrong_target 等拒绝保留原始错误语义。
- 成功的内置 WebSearch、WebFetch、Read 由 SDK PreToolUse 开始，PostToolUse 结束；PostToolUseFailure 记失败。回调给出的 tool_use_id 配对调用与结果，工具参数和返回只写一次时间线，观测步骤只记引用、状态与规模。仅匹配这三个实际开放的内置工具，不让回调重复记录 Pebble MCP 调用。授权回调实际拒绝的调用记 denied；模型未发出的工具调用不产生记录。
- 关键静默降级只覆盖：材料目录生成失败、手动选择的技能未装配、记忆判断失败、工具轨迹条目写入失败。各发生处记一条 degraded 步骤；写观测自身失败只进进程日志，不递归记步骤。
- 每次工具调用的步骤在开始时写 running，结束时更新。进程中断留下的 running 由读取面显示为“中断时未记录结果”，不伪装为成功或失败；轮次恢复／重试仍以 agent_runs 为准。

官方 [Python SDK 参考](https://docs.qoder.com/cli/sdk/references-python) 与 [CLI 工具回调](https://docs.qoder.com/cli/hooks) 给出了上述回调和成功结果字段；本地 CN SDK 1.0.14 的类型还包含 tool_use_id 及可选 duration_ms。实际回调顺序、内置工具覆盖和结果内容必须按规格 §4 用真实 CN 运行时验证。

## 3. 用量口径

单次模型请求的 token 与 Credits 来自对应 AssistantMessage.usage；用 message_id 或 usage.request_id 去重。本轮合计优先由相邻轮会话累计快照（session_totals）的差值推算：任务首轮的 SDK 会话由该轮新建，累计快照即本轮消耗；其后任一轮缺快照无法划界、或差值回落（会话被重置）时，该轮退回请求级求和，有请求缺字段或无法排除重复时显示“未记录”。推算合计在界面上标注“按相邻轮累计差值推算”，不冒充逐次求和；result_usage 是末次请求的读数，单独一行显示，不并入逐次明细；会话累计值只在快照里出现，不作为单轮消耗展示。SDK 报告的 duration_api_ms 直接标为“API 时长”，不与工具耗时相减。口径依据：[Qoder Cost and usage](https://docs.qoder.com/cli/sdk/cost-usage)。

## 4. 读取与界面

GET /api/tasks/{task_id}/observations 返回该任务按 agent_runs 顺序排列的轮次摘要和步骤；只读、沿用任务权限。任务不存在返回 404，观测存储不可用返回 503，均不影响原有对话与确认接口。任务页每轮一个默认折叠的「执行详情」，与时间线工具条目按 item_id 双向定位。未记录字段显示“未记录”；进行中的步骤显示“进行中”。

不提供跨任务执行页、用量汇总、告警、通知、第三方追踪后端、训练数据导出或观测数据写入接口。
