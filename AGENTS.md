# AGENTS.md

## Pebble 是什么

对话优先的个人助手 Agent：常驻 Python 服务 + Web 前端，单用户单实例。单个 Agent 按目标组合工具（Gmail、iCloud Calendar、个人资料库、记忆、技能、历史检索）完成真实事务；外部写授权、持久化与去重由程序保证，不靠提示词。新邮件是首个系统触发源，不是唯一入口。

项目主线包含持续对话、个人信息与技能的使用、工具执行和常驻运行；不要将整体开发重点概括为邮件、日历的完整事务流程。Qoder Agent SDK 与 Pebble 的职责分工见 `docs/spec.md` §1.1。

## 文档：先读再动

| 要做的工作 | 先读 |
| --- | --- |
| 任何功能、架构、流程改动 | `docs/spec.md` —— 全局不变量、轮次模型、上下文预算、部署与跨域验收。**内容冲突时以它为准** |
| 邮件：触发、草稿、确认发送、核实 | `docs/mail.md` |
| 日历：查询、冲突、会话式创建 | `docs/calendar.md` |
| 记忆：两个文件、写路径、注入 | `docs/memory.md` |
| 资料库：保存、检索、版本、用户直改 | `docs/kb.md` |
| 技能：装配、变更管道、后台复盘、审批 | `docs/skills.md` |
| 观测：轮次观测、降级、健康检查 | `docs/observability.md` |
| 真实场景评测：目标、环境、场景库、红线 | `docs/evaluation.md` |
| 启动、检查命令、远程访问、数据目录 | `README.md` |

规则：

- 规格文档只写"应该是什么、具备什么能力、遵守什么规则"；实现状态与已知偏差**只**记录在 `docs/status.md`。实现变化时更新 `status.md`，不要在规格里写"已实现 / 未实现"。
- 用户要求与规格冲突时，先指出差异、获得确认再动手，不自行改变产品行为。

## 硬约束

**架构**

- `server/agent/` 只装配 Qoder Agent SDK，不实现自有 Agent 循环。
- 通用层（基础提示、上下文组装、网关与调度）不出现具体领域措辞；领域语义只写在该域工具的 description 与触发 / 回报文案里。

**外部写与凭证**

- 外部写的唯一闸门是 Confirmation（`server/approval/`）：`EXTERNAL_WRITE` 级函数（邮件发送与核实）永不注册给模型；`DIRECT_EXTERNAL_WRITE` 与 `LOCAL_WRITE_USER_TURN` 只在用户亲自发起的轮暴露。暴露矩阵在 `server/agent/toolset.py` 的 `ALLOWED_EFFECTS`，改工具副作用前先对照 `docs/spec.md` §4.1。
- 模型与外部服务凭证仅存服务端，不进入聊天上下文、前端或 Git。

**持久化**

- SQLite（`pebble.db`）只保存运行状态：任务与 SDK 会话关联、确认与执行、时间线、观测。
- Memory、Skill 内容与知识库资料以文件保存在实例数据目录；`kb/` 与 `skills/` 由该目录内独立的本地 Git 仓库管理（不是代码仓库），`memory/` 不做版本管理、不入该仓库。
- 派生数据（`kb-index.sqlite3`、模型目录缓存）可随时删除重建。

**行为红线**（细节见对应规格）

- 记忆与资料只保存、整理、提供信息，不执行外部操作，不记录出处。
- 常驻材料每轮受预算约束（`docs/spec.md` §6）；超限走降级并告知，不静默丢弃用户点名的内容。
- 前台 Agent 不能改记忆；磁盘直改未收编的技能不可装配；索引落后不给过期答案。

## 代码地形

```
server/
  main.py config.py db.py errors.py attachments.py   # 装配、配置、迁移、错误、附件
  agent/      # SDK 网关、轮次工具集、上下文组装、MCP 工具服务、模型目录
  gateway/    # 运行时调度、事件流、轮次契约
  approval/   # Confirmation：外部写唯一闸门
  tools/      # 各域工具（gmail/ calendar/ memory/ personal_kb/）+ registry（SideEffect）
  skills/     # 技能服务、复盘、运行时装配
  memory/     # 记忆服务、判断、回顾
  sessions/   # 时间线、观测、历史检索
  api/        # HTTP 路由、访问控制、静态托管
  storage/    # 数据仓库（本地 Git）、行锚点编辑
web/          # React + Vite 前端（页面在 src/pages，测试与组件同目录）
tests/        # pytest（support/ 提供三个外部边界的替身）；acceptance/ 真实模型脚本
docs/         # 规格文档（见"文档：先读再动"）
```

实例数据目录（默认 `.data/`）含凭证与用户数据，已被 `.gitignore` 覆盖，绝不提交、绝不写进测试快照。

## 验证

改动后端：仓库根执行，pytest 与 ruff 都必须过。`ruff` 必须显式给 `--config`，否则 `tests/` 在仓库根、向上找不到配置，会静默退回默认规则：

```bash
uv run --project server pytest -c server/pyproject.toml
uv run --project server ruff check --config server/pyproject.toml server tests
```

改动前端（`web/` 内执行）：

```bash
npm run typecheck && npm test && npm run build
```

`tests/acceptance/` 使用真实 Qoder 模型额度，不随 pytest 运行，只在明确要求时单独执行。

## 自主执行边界

无需逐步审批：已授权范围内的实现、必要验证及相关修复；不改变需求范围和产品行为的常规实现细节。

先向用户明确并获得确认：改变需求范围或既定产品行为（以规格文档为准）；破坏性操作；真实外部写入，即实际发送邮件、创建日程或使用未授权账号。

## Git

- 直接提交到当前分支，不自动推送。
- commit message：`[模块] 功能描述`，英文，例如 `[Gmail] Support email thread association`。
- 不加 `co-authored-by`，除非对方明确要求。
