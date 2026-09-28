# 最小 Agent 评测

从仓库根运行（需要服务端现有 Qoder 凭证，会消耗模型额度）：

```bash
uv run --project server python -m tests.evaluation --list
uv run --project server python -m tests.evaluation --model qmodel_38max --repeat 3
uv run --project server python -m tests.evaluation --model qmodel_38max --case kb_updated
```

型号使用账号实际支持的固定型号；也可以省略 `--model` 使用 `PEBBLE_QODER_MODEL`，不接受
`auto`。普通 pytest 不调用模型。`--timeout 240` 是单次完整用例的运行预算（秒），超时后
最多额外等待 20 秒清理。`--output /absolute/new-directory` 指定报告位置，已有目录不覆盖。

用例在 `cases.json`：documents 预置真实 Markdown 资料，steps 定义用户消息、可选资料更新、
预期事实、禁止出现的事实及必须读取的资料。添加同结构记录即可扩充；预期答案不会发给模型。
`{old}`、`{current}`、`{other}` 每次替换为随机事实，实际值保存进报告。

每次重复使用新的临时实例、数据库、文件和 SDK 会话；同一用例的多轮保留对话。
复用 `tests.acceptance.qoder_kb_search.Harness`：真实 GatewayRuntime、Qoder SDK、MCP、
KB/Memory、SQLite 和本地 Git。输入走运行时提交入口，不经过浏览器/HTTP；Gmail 使用替身，
日历未装配，没有真实邮件发送或日历写入。这是合成业务场景，不代表真实用户场景全集。
后台 Memory 沿用该验收装配；不将后台学习结果作为本版评分对象。

默认生成 `.eval-results/<时间与随机标识>/report.json` 和 `report.md`，逐次写入，保留回答、
断言、运行错误、完整可见时间线和可获得的用量。临时实例完成后删除，报告不含 Settings 或
凭证配置。报告可能包含对话与工具内容，应只用合成/获准的数据；默认输出目录不进 Git。
报告记录代码提交和工作区状态；不保存 SDK 私有会话文件，也不支持精确会话回放。

结果为 passed（所有断言通过）、failed（运行结束但断言失败）、error（运行/环境异常）。
不自动重试，所有错误进入总尝试数；任一失败/错误时退出码为 1。每例全通过次数的汇总仅是
这批样本的稳定性观察，不是生产可靠性保证。数据缺失时保留为空，不当成零用量。
当前事实检查是字符串包含/排除与真实读取记录，不能识别否定、讽刺等开放式语义；
报告中的原始答复用于人工复核。尚未接入模型评分器、用户模拟器、故障注入或真实外部服务。
