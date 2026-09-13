# AGENTS.md

## Pebble

持续运行的个人 Agent：常驻 Python 服务，由单个 Agent 按用户目标组合工具（Gmail、iCloud Calendar、Personal KB），程序保证确认、持久化与去重。

## 文档

- `docs/v1-spec.md`：需求范围、产品行为、验收标准。内容冲突时以此为准。
- `docs/v1-design.md`：组件与代码结构（§2）、交付阶段（§7）、验证要求（§9）、协作分工（§10）。
- `docs/v1-mail-flow-contract.md`：第一条邮件链的 A/B 接口约定，未定稿。
- `README.md`：启动与检查命令。

涉及功能、架构或执行流程的工作先读相关约定。用户要求与现有约定冲突时先明确差异，不自行改变产品行为。

## 关键约束

- `server/agent/` 只装配 Qoder Agent SDK，不实现自有 Agent 循环。
- 外部写工具的发送与创建函数由 Confirmation（`server/approval/`）调用，不暴露给模型。
- SQLite 只保存任务与 SDK 会话关联及确认、执行状态；Memory 规则与 Skill 内容以文件保存并经 Git 管理。
- 模型与外部服务凭证仅存服务端，不进入聊天上下文、前端或 Git。
- `ruff` 必须显式给 `--config server/pyproject.toml`：`tests/` 在仓库根，否则静默退回默认规则。

## 自主执行边界

无需逐步审批：已授权范围内的实现、必要验证及相关修复；不改变需求范围和产品行为的常规实现细节。

先向用户明确并获得确认：改变需求范围或既定产品行为；破坏性操作；真实外部写入，即实际发送邮件、创建日程或使用未授权账号。

## Git

- 直接提交到当前分支，不自动推送。
- commit message：`[模块] 功能描述`，使用英文，例如 `[Gmail] Support email thread association`。
- 不加 `co-authored-by`，除非对方明确要求。
