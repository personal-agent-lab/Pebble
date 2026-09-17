# Memory 与 Skills 契约

状态：**Memory 已实现，Skills 尚未实现**。SDK 侧的 `skills` 配置插孔与每轮上下文装配已存在，生效名单当前为空。本文定义文件格式、存储布局、沉淀来源、审核与加载边界。实现与本文冲突时先改本文。

Memory 保存“关于用户的精简背景、偏好、长期目标与约定”，每轮常驻上下文；个人知识库保存“具体资料”，按需检索，两者分开，见 `personal-kb.md`。Memory 只保存跨任务适用的用户信息与事实；某类任务的做法（步骤、流程、这类工作中的偏好与纠正）由 Skill 负责，记忆不保存。记忆不记录出处。

## 1. 存储布局

```text
<data_dir>/memory/USER.md   # 用户本人：背景、目标与偏好
<data_dir>/memory/MEMORY.md # 用户以外、跨任务都成立的事实与约定
<data_dir>/skills/          # 生效 Skill，SDK 发现目录
<data_dir>/skill_drafts/    # 待审核草稿，发现目录之外
```

`skills/`、`skill_drafts/` 与 `kb/` 同属实例数据目录内的一个独立本地 Git 仓库，不是代码仓库；`memory/` 不做版本管理，不进入该仓库。仓库的忽略规则只允许这些内容目录进入版本管理，SQLite、凭证、SDK 会话与日志均不跟踪。每次有效写入即提交，不推送远端；用户可以直接阅读、修改文件，并通过 Git 查看变化和回退。

## 2. Memory 文件格式

`USER.md`：

```markdown
- 用户默认使用简体中文。
- 用户偏好先给结论，再解释原因。
```

`MEMORY.md`：

```markdown
## 会议

- 内部会议默认 30 分钟；对外邀请按对方给出的时长。
```

每个文件是一份完整的 Markdown 文档，写法不限，不按条分隔。内容按规范化后计数：统一换行、去掉首尾空白、连续空行合并为一个。旧格式中独立一行的 `§` 分隔符在启动时换成空行，原条目变为段落。`USER.md` 最多 1375 个 Unicode 字符，`MEMORY.md` 最多 2200 个；使写入后内容超限且变大的写入整体拒绝，不截断旧内容。文件被直接编辑到超限时照常读取，只接受让它变小的写入。写入是原子替换，不做版本管理；每块记忆的版本号 `version` 是规范化内容的哈希，只用于管理页的冲突检查。

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
| 用户纠正 | 任务时间线中的记忆提示（“已修改：原内容 → 新内容”） | 同类纠正是否重复出现 |

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
| 每轮记忆判断 | 每个用户消息轮与主回答并行 | `memory_edit`、`memory_ask` |
| 后台记忆回顾 | 每攒够若干个已完成消息轮，找跨轮模式并整理 | `memory_edit` |

分区：`user` 写用户本人（身份、长期目标、学习方向、兴趣、沟通与表达偏好、工作习惯、对助理的总体期望），`memory` 写用户以外、跨任务都成立的事实与约定（账号、日历、联系人的事实，固定安排规则，使用服务时确认过的经验）。内容写成陈述句，不写成对助理的命令。分区标准与示例写在 `memory_edit` 的工具说明里，判断与回顾共用。

`memory_edit` 输入 `target`（`user` 或 `memory`）、`old_text`、`new_text`，两者去掉首尾空白后不能都为空：`old_text` 为空时把 `new_text` 追加到文末（与已有内容以空行分隔），`new_text` 已原样出现在文档中时不写入；两者都有时把文档中恰好出现一次的 `old_text` 替换为 `new_text`；`new_text` 为空时删除 `old_text`。`old_text` 找不到或出现多次时返回 `invalid_memory`，不写入。也可以改传 `operations`（不能与 `old_text`、`new_text` 同时使用）：一组 `{old_text, new_text}`，按顺序作用在同一分区上，每处的匹配以前几处编辑后的文档为准；整体生效或整体失败，容量只按全部编辑后的结果检查，失败信息指出第几处。成功输出 `target`、`changed`、`content`（全文）、`usage`、当前 `version`，以及按顺序列出每处编辑的 `applied[]`（`old_text`、`new_text`、`changed`）；几处编辑互相抵消、文档没有变化时每处都记为未生效。提示按 `applied[]` 逐处生成。`memory_ask` 输入一句确认问题，不做任何写入，问题由程序作为提示展示给用户。两个会话收到的当前记忆材料标题附带已用与上限字数，容量不足时用一次带 `operations` 的调用同时整理旧内容并加入新内容。每轮判断的写入结果与追问、后台回顾实际生效的替换、删除与移动，由程序按记录到的工具结果渲染成用户可见的提示（见 `memory-spec.md` §5.3），模型自述不作为事实来源；回顾的新增不提示。同一次会话里从一个分区删除、又原样追加到另一分区的内容（忽略列表符号）合并为一条“已移到……”提示。回顾提示以 `notice` 写入任务时间线，挂在回顾窗口内最后一个调用上，并经 SSE 推送。

管理页面的 HTTP 接口：

| 接口 | 输入 | 输出 |
| --- | --- | --- |
| `GET /api/memory` | — | `user`、`memory` 各含 `content`（全文）、`usage`（`chars`、`limit`）、`version` |
| `PUT /api/memory/{target}` | `content`（整份文档，可为空）、`expected_version` | `target`、`changed`、`content`、`usage`、`version` |

页面整份保存；`expected_version` 与当前内容不一致时返回 409 `version_conflict`，附 `current_version`，不写入。内容规范化后与当前一致时 `changed` 为假，不写文件。

## 8. 错误

Memory 字段校验（含页面接口的未知分区）或片段匹配失败返回 `invalid_memory` 并附 `errors[]`（HTTP 422）；容量超限返回 `memory_full`，附 `target`、`used` 与 `limit`（HTTP 422）；文件不可读写返回 `memory_store_unavailable`（HTTP 503）。失败不保留文件的半完成修改。Skills 字段校验仍使用 `SkillValidationError`，附 `errors[]`。
