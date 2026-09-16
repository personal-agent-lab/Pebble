# Memory 与 Skills 契约

状态：**Memory 已实现，Skills 尚未实现**。SDK 侧的 `skills` 配置插孔与每轮上下文装配已存在，生效名单当前为空。本文定义文件格式、存储布局、沉淀来源、审核与加载边界。实现与本文冲突时先改本文。

Memory 保存“关于用户的精简背景、偏好、长期目标与约定”，每轮常驻上下文；个人知识库保存“具体资料”，按需检索，两者分开，见 `personal-kb.md`。记忆条目不记录出处。

## 1. 存储布局

```text
<data_dir>/memory/USER.md   # 用户背景与偏好
<data_dir>/memory/MEMORY.md # 跨会话持续适用的简短约定
<data_dir>/skills/          # 生效 Skill，SDK 发现目录
<data_dir>/skill_drafts/    # 待审核草稿，发现目录之外
```

三者与 `kb/` 同属实例数据目录内的一个独立本地 Git 仓库，不是代码仓库。仓库的忽略规则只允许这些内容目录进入版本管理，SQLite、凭证、SDK 会话与日志均不跟踪。每次有效写入即提交，不推送远端；用户可以直接阅读、修改文件，并通过 Git 查看变化和回退。

## 2. Memory 文件格式

```markdown
默认使用简体中文。

§

内部会议默认 30 分钟；对外邀请按对方给出的时长。
```

每条记忆以独立一行 `§` 分隔，条目本身可包含多行。`USER.md` 最多 1375 个 Unicode 字符，`MEMORY.md` 最多 2200 个；超限写入整体拒绝，不截断旧内容。

每轮上下文重新读取文件：`USER.md` 作为 `## 关于你`，`MEMORY.md` 作为 `## 事实与约定`，资料目录（`personal-kb.md` 第 7 节）作为 `## 资料目录`，放在本轮触发材料之前。空文件不生成材料块。这三块构成常驻上下文，总量受各自容量上限约束；历史对话、资料正文、邮件与日程不自动注入，由 Agent 按需检索。规则不得覆盖外部写确认约束：该约束由程序（每轮工具可见范围与 Confirmation）保证，不依赖规则文本或模型自律。

## 3. Skill 文件格式

`skills/<name>/SKILL.md` 与 `skill_drafts/<name>/SKILL.md` 使用同一格式：

```markdown
---
id: sk_01HZR5M3V4W2X8
name: 会议邀请处理
description: 用户要求安排会议或收到活动邀请时，查询空闲、创建日程与准备回复草稿
status: approved            # draft | approved | archived
source: distilled           # user | distilled
triggers:
  - 用户要求安排会议
  - 收到活动邀请邮件
inputs:
  - {name: window, required: true, note: 目标时间范围}
tools: [calendar_list_events, calendar_check_conflicts, calendar_create_event, gmail_prepare_reply]
side_effects: [external_write]
requires_confirmation: true
evidence:                   # source 为 distilled 时必填
  tasks: [task_01HZR2K0, task_01HZR31A, task_01HZR4BC]
  occurrences: 5
  last_seen_at: 2026-09-14T09:10:00+08:00
approved_version: 9f2c1ab   # 批准绑定的内容哈希
approved_at: 2026-09-14T10:30:00+08:00
---

## 执行流程

1. …
2. …

## 边界与禁止事项

- …

## 示例

…
```

`side_effects` 声明该流程可能触达的最高副作用，`requires_confirmation: true` 表示流程含外部写操作。Skill 不能声明绕过确认；生效的 Skill 在运行时仍逐项确认外部写操作。

## 4. 两条来源

### 4.1 用户自建

用户在 Web 的 Skills 页面新建或编辑，也可以直接改文件。保存即 `status: approved`，`approved_version` 为当前内容哈希。用户创建即为批准，不走草稿审核。

### 4.2 Agent 从使用记录总结

使用记录来源：

| 记录 | 位置 | 用途 |
| --- | --- | --- |
| 任务时间线 | `task_timeline_items` | 用户目标、Agent 处理过程与卡片位置 |
| 调用轨迹 | `agent_runs` 与时间线中的操作项 | 轮次类型与工具序列 |
| 执行结果 | `approval_executions` | 流程是否稳定成功 |
| 用户纠正 | `memory/` 的 Git 历史 | 同类纠正是否重复出现 |

判据（首版）：同类目标在不少于 3 个不同任务中出现；工具序列稳定；相关操作最终状态为成功；同一纠正不再重复出现。

产出：调用 `skill_propose` 写入 `skill_drafts/`，`status: draft`、`source: distilled`、`evidence` 必填（列出依据的任务标识与出现次数）；随后在对话中提示用户审阅，给出草稿名称、触发条件、必要参数与安全性信息摘要。时机由模型在任务结束时自主判断，不引入离线批处理。

## 5. 审核与生效

- 草稿只能由用户批准：在 Web 上以已认证身份操作。不提供“批准 Skill”的模型工具，本地写工具只能产出或修改草稿。
- 批准绑定最终内容哈希：写入 `approved_version`，文件移入 `skills/`，`status: approved`。
- 批准后内容再次变化（用户编辑或 Agent 修改）时，当前哈希与 `approved_version` 不一致，加载器立即停止提供该 Skill，`status` 回到 `draft`，须重新批准。
- 未批准或版本不一致的 Skill 不得被应用或执行，新会话中同样不生效。
- 用户可撤回已生效 Skill：`status` 置 `archived` 并移出发现目录，历史留在 Git。
- Skill 生效的批准不替代运行时每次具体外部写操作的确认。

## 6. 加载与每轮装配

- 每轮装配读取 `skills/` 中 `status: approved` 且内容哈希与 `approved_version` 一致的名单，交给 SDK 的 `skills` 配置；恢复会话时同步当前批准名单。
- `skill_drafts/` 不在 SDK 发现路径内，也不出现在名单中，仅用于审阅。
- 过滤只在程序侧进行，不依赖模型自律。

## 7. Agent 可见工具

| 工具 | 副作用 | 说明 |
| --- | --- | --- |
| `skill_propose` | 本地写 | 产出或更新草稿，不能批准 |
| `skill_list` | 只读 | 生效名单与草稿名单，含状态与版本一致性 |
| `skill_read` | 只读 | 读取完整内容 |

`skill_propose` 输入 `name`、`description`、`triggers[]`、`inputs[]`、`tools[]`、`steps`、`side_effects[]`、`requires_confirmation`、`evidence`，输出 `id`、`path`、`status: "draft"`。同一 `name` 的草稿已存在时更新它，不新建第二份。

草稿写入属本地写：用户对话轮与执行结果回传轮可用；新邮件触发轮不开放 Skill 草稿，避免在用户未参与时沉淀流程。触发轮只额外开放资料库的新建与修改，见 `personal-kb.md` 第 4 节。

Memory 不向前台对话暴露工具，写入集中在两个独立的一次性会话，都不接续对话、不进入任何任务历史：

| 会话 | 时机 | 工具 |
| --- | --- | --- |
| 每轮记忆判断 | 每个用户消息轮与主回答并行 | `memory_add`、`memory_replace`、`memory_remove`、`memory_ask` |
| 后台记忆回顾 | 每攒够若干个已完成消息轮，只找跨轮模式 | 仅 `memory_add` |

`target` 取 `user` 或 `memory`。`memory_add` 输入 `target` 与非空 `content`；`memory_replace` 另要求 `old_text` 只匹配一个现有条目；`memory_remove` 只输入 `target` 与 `old_text`。完全相同的新增不重复写入，也不产生空提交。成功输出 `target`、`action`、`changed`、`entries`、`usage`、当前 `commit`，替换与删除另附原条目 `old`。`memory_ask` 输入一句确认问题，不做任何写入，问题由程序作为提示展示给用户。每轮判断的写入结果与追问由程序渲染成用户可见的提示（见 `memory-spec.md` §5.3），模型自述不作为事实来源；后台回顾不进入时间线，也不向用户发通知。

## 8. 错误

Memory 字段或匹配失败返回 `invalid_memory` 并附 `errors[]`；容量超限返回 `memory_full`，附 `target`、`used` 与 `limit`；文件或 Git 不可用返回 `memory_store_unavailable`。失败不保留文件或 Git 索引的半完成修改。Skills 字段校验仍使用 `SkillValidationError`，附 `errors[]`。
