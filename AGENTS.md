# AGENTS.md

## Pebble

对话优先的个人助手 Agent：常驻 Python 服务，用户随时对话，由单个 Agent 按目标组合工具（Gmail、iCloud Calendar、个人知识库、Memory/Skills），程序保证外部写授权、持久化与去重。新邮件是首版触发源，不是唯一入口。

## 文档

- `docs/v1-spec.md`：需求范围、产品行为、验收标准。内容冲突时以此为准。
- `docs/v1-design.md`：组件与代码结构（§2）、交付阶段（§7）、验证要求（§9）、当前实现与已知偏差（§10）。
- `docs/contracts/`：按域的接口字段与语义。`mail.md`、`calendar.md` 已实现；`skills.md` 已实现；`personal-kb.md` 是约定，尚未实现。
- `README.md`：启动与检查命令、远程访问现状。

涉及功能、架构或执行流程的工作先读相关约定。用户要求与现有约定冲突时先明确差异，不自行改变产品行为。

## 关键约束

- `server/agent/` 只装配 Qoder Agent SDK，不实现自有 Agent 循环。
- 外部写操作统一由 Confirmation（`server/approval/`）执行：邮件发送函数不暴露给模型，日程创建工具按轮次暴露，只在用户亲自发起的对话轮可见。
- 通用层（基础提示、上下文组装、网关与调度）不出现具体领域措辞；领域语义写在该域工具的 description 与触发域内。
- SQLite 只保存任务与 SDK 会话关联及确认、执行状态；Memory 规则、Skill 内容与知识库资料以文件保存，由实例数据目录内独立的本地 Git 仓库管理（不是代码仓库）。
- 模型与外部服务凭证仅存服务端，不进入聊天上下文、前端或 Git。
- `ruff` 必须显式给 `--config server/pyproject.toml`：`tests/` 在仓库根，否则静默退回默认规则。

## 自主执行边界

无需逐步审批：已授权范围内的实现、必要验证及相关修复；不改变需求范围和产品行为的常规实现细节。

先向用户明确并获得确认：改变需求范围或既定产品行为；破坏性操作；真实外部写入，即实际发送邮件、创建日程或使用未授权账号。

## Git

- 直接提交到当前分支，不自动推送。
- commit message：`[模块] 功能描述`，使用英文，例如 `[Gmail] Support email thread association`。
- 不加 `co-authored-by`，除非对方明确要求。
