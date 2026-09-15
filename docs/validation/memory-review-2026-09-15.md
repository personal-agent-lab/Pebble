# 后台记忆回顾真实验收记录（2026-09-15）

## 结论

两层记忆更新机制以真实 Qoder 模型完成闭环：

1. 前台即时保存（提示词更新后回归）：非指令式的稳定信息（“我最近正在学习 Hermes Agent 的设计”）在当轮产生 `memory(action=add, target=user)` 调用，`USER.md` 落盘且恰好一个 `[Memory] Add user entry` 提交。
2. 后台复盘（新功能）：真实 SDK 一次性会话 + 真实 MCP 环回 + 只新增工具，5 轮种子对话产生 3 条记忆新增（学习方向、回复偏好、side project 事实），每条恰好一个 Git 提交；纯闲聊任务零新增、零提交；时间线零变化。
3. 生产服务端到端：真实 HTTP + SSE，5 轮消息自动触发复盘，任务时间线 / SSE 事件 / agent_runs / latest_run 零可见副作用；手动触发 202、未知任务 404。
4. 重启恢复：复盘执行中 kill 服务进程，重启后行变为 interrupted；第 6 条消息完成后计数自愈，复盘自动重新触发并完成。

验收使用 `qodercn-agent-sdk==1.0.14` 和其捆绑的 Qoder CLI CN 1.1.38。

## 真实验收结果

### Layer 1：前台即时保存（qoder_memory_prompt.py）

| 检查 | 结果 |
| --- | --- |
| 当轮工具调用 | `memory(action="add", target="user", content="正在学习 Hermes Agent（开源个人助理项目）的设计…")` |
| USER.md 落盘 | 包含该学习方向条目 |
| Git 提交 | 恰好一个 `[Memory] Add user entry` |
| 回复内容 | 明确说明已记住的内容 |

原 `qoder_memory.py` 回归同时通过（写入、单提交、跨会话回忆 CORAL-7421）。

### Layer 2：后台复盘（qoder_memory_review.py）

| 检查 | 结果 |
| --- | --- |
| 5 轮种子对话后复盘 | `review_status=done`；3 条新增（2 条 user、1 条 memory），每条恰好一个 `[Memory] Add` 提交 |
| 只新增约束 | MCP 端点只暴露 `memory_add`，无 action 参数 |
| 闲聊负例 | 零记忆写入、零 Git 提交、`status=done` |
| 时间线 | 两个任务的 `task_timeline_items` 计数前后不变 |
| `include_partial_messages=False` 带工具 | 可用（一次假设验证通过，无需回退 True） |

新增条目内容（正例）：

- user：正在学习 Hermes Agent（开源个人助理项目），后续会频繁讨论相关内容。
- user：偏好简洁的中文回复，不要长篇大论；不要用“好的！”等填充词开头，直接给出结论。
- memory：正在开发个人助理 side project，存储方案选用 SQLite。

### 生产服务端到端（qoder_memory_review_http.py）

| 检查 | 结果 |
| --- | --- |
| 5 轮消息自动触发 | `memory_reviews` 出现 `origin=interval` 且 `status=done` 的行 |
| 复盘不可见 | 时间线计数不变；agent_runs 只有 `message` kind；SSE 无复盘事件（49 条全部来自消息轮）；`latest_run.kind == "message"` |
| 记忆写入 | 首次运行产生 4 个 `[Memory] Add` 提交，USER.md 出现 Hermes 学习方向；重复运行时内容已在库，复盘幂等零新增 |
| 手动触发 | `POST /api/tasks/{id}/memory-review` → 202，复盘真实执行并完成 |
| 未知任务 | 404 |
| 重启恢复（phase B） | 复盘 running 时 kill：重启后行为 `interrupted`（error=“上次进程退出时记忆回顾尚未结束，已记录中断”）；第 6 条消息完成后新复盘自动触发并 `done`；仍无 agent_runs/SSE 副作用 |

## 调试过程中的发现

- 验收脚本最初断言 `root/USER.md`，而 MemoryStore 实际写入 `root/memory/USER.md`。此前的“模型未保存”假象源于路径错误；修正路径后，批准计划中的原始提示词文本一次通过，无需任何提示词强化（试验过的独立“长期记忆”小节与“没有调用就不要声称已经记住”等改动已回退）。
- 首次 E2E 断言用提交主题做差集，与既有同名提交混淆导致误报；改用 `rev-list` 哈希。
- 生产 E2E 直接对仓库实例目录 `.data` 工作，因此产生的记忆条目会真实保留在实例记忆库中。

## 可重复执行

在仓库根目录运行（均显式调用真实模型并消耗账号额度，不进入普通 pytest）：

```bash
uv run --project server python -m tests.acceptance.qoder_memory        # Layer 1 回归
uv run --project server python -m tests.acceptance.qoder_memory_prompt # Layer 1 新提示词
uv run --project server python -m tests.acceptance.qoder_memory_review # Layer 2 复盘
uv run --project server python -m tests.acceptance.qoder_memory_review_http  # 生产端到端 + 重启恢复
```

`qoder_memory_review_http` 需要实例数据目录具备 Gmail 凭证（`.data/credentials-gmail.json` 与 token）；任务在结束时删除，记忆条目保留。

## 验证边界

- 使用真实 Qoder 模型、真实 qodercli 子进程、真实 MCP HTTP 环回、真实 SQLite 与本地 Git。
- Layer 1/2 使用临时 `PEBBLE_DATA_DIR`；HTTP 端到端使用仓库实例目录（Gmail 检测随生产装配启动，但不读取邮件内容；不执行任何外部写操作）。
- 未验证 Memory 管理页面、历史检索与恢复（尚未实现）；未验证记忆满容量时复盘的行为。
