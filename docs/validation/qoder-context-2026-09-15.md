# Qoder SDK 短期上下文验收记录（2026-09-15）

## 结论

Pebble 当前使用的 `qodercn-agent-sdk==1.0.14`（配套 Qoder CLI CN 1.1.38）能够通过
`resume` 保持多轮上下文，并在新的 Python 进程中恢复同一会话。长上下文经手动压缩后，
任务目标、关键约束和未完成事项仍能被模型正确说出。

恢复会话时重新传入 `system_prompt` 的行为不能作为动态更新机制：相同探测曾分别得到旧规则和
新规则，官方文档也未将“替换已存在会话的基础系统提示”声明为稳定契约。Pebble 因此固定基础提示，
将执行结果、触发材料以及后续 Memory 通过 `SessionStart.additionalContext` 注入每次新启动或
恢复的 CLI 进程。真实模型验收已确认恢复会话能读取该轮最新的“关于你”内容。

当前 headless runtime 的 `get_context_usage()` 返回 `autoCompact.enabled: false`，阈值为
81.7%。Pebble 在恢复会话前读取该状态；自动压缩关闭且使用率达到阈值时，先执行 `/compact`，
收到 `compact_boundary` 后再发送用户消息。压缩失败时终止本轮并如实报告，不带着未经整理的
上下文继续执行。

## 真实验收结果

| 场景 | 输入与检查 | 结果 |
| --- | --- | --- |
| 多轮目标与条件 | 首轮给出 `ORBIT-731` 与“周二前”，下一轮只要求压缩原文 | 保留两项硬性信息 |
| 进程重启后继续 | 首轮退出 SDK/Python 调用；另一个 Python 进程按 session ID 恢复 | session ID 不变，能继续修改原文 |
| 恢复时使用最新材料 | 恢复轮注入“用户希望被称为林舟” | 回答正确称呼“林舟” |
| 基础系统提示更新 | 首轮规则为 `OLD-RULE`，恢复时传入 `NEW-RULE` | 重复执行分别得到 `OLD-RULE` 与 `NEW-RULE`；不依赖此行为 |
| 长上下文压缩 | 构造长测试上下文，观察 Pre/PostCompact 与 compact boundary | 三项事件完整出现 |
| 压缩后任务保持 | 压缩后询问代号、约束、未完成事项 | 正确保留 `COMPACT-481`、蓝色标签、最终检查单 |

长上下文探测在压缩前由 SDK 报告使用率 100%。手动压缩完成后，当前 CLI 的使用率接口仍
报告 100%，因此“压缩是否完成”以 PostCompact 与 `compact_boundary` 为准，不能用该百分比
单独判断。官方发布说明中已有 `/compact` 后进度显示修复，但当前绑定版本尚未包含这些更新。

## 可重复执行

基础真实验收（调用真实 Qoder 模型）：

```bash
uv run --project server python -m tests.acceptance.qoder_context
```

包含长上下文与压缩验收（会发送约 14,000 行合成文本，消耗更多额度）：

```bash
uv run --project server python -m tests.acceptance.qoder_context --compact
```

普通 pytest 不执行该脚本，避免日常检查意外消耗真实账号额度。

## 验证边界

- 使用了真实 Qoder 模型、真实本地会话存储与独立 Python 进程。
- 测试内容完全为合成文本，没有调用 Gmail、Calendar 或其他外部工具。
- 应用的任务与 session ID 持久关联、重启恢复调度另由 Gateway/SQLite 自动测试覆盖；本次
  真实验收验证的是被恢复的 Qoder 会话本身确实保留上下文。
- 自动压缩在当前 runtime 中未启用；已验证的是 Pebble 的阈值触发逻辑与真实 `/compact`
  行为。后续升级 SDK 时应重新检查 `autoCompact.enabled` 和使用率读数。

## 参考

- [Qoder Agent SDK：How it works](https://docs.qoder.com/cli/sdk/how-it-works)
- [Qoder Agent SDK：System Prompt](https://docs.qoder.com/cli/sdk/system-prompt)
- [Qoder Agent SDK：Python Reference](https://docs.qoder.com/cli/sdk/references-python)
- [Qoder：Context compaction](https://docs.qoder.com/qoder/context-compaction)
- [Qoder CLI Release Notes](https://docs.qoder.com/release-notes/qoder-cli)
