# Skill 系统重构任务清单

> 2026-09-25 决定：用户指定一次已完成工作即可总结为 Skill，Agent 不主动判断是否值得保存。阶段 D 的自动沉淀已取消；本清单其余重构步骤实施前须对齐这项决定与 `status.md`。

目标约定是 `skill-spec.md` 与 `contracts/skill.md`（2026-09-22 重写）；当前实现按旧契约 `contracts/skills.md`（已删除）于 2026-09-19 验收。本文记录两者的差距、可复用部分与重构步骤；实现状态以 `status.md` 为准。

## 1. 主要差距

| 方面 | 现状 | 目标 |
| --- | --- | --- |
| 标识 | `sk_<hex>` 随机生成，id 即目录名 | 目录名即 `skill_id`，用户可读（旧 `sk_` 名恰符合新格式，可原样保留） |
| 元数据 | triggers、inputs、tools、side_effects、requires_confirmation、evidence、schema_version、approved_version | 仅 name、description、origin、managed、state、时间戳 |
| 状态 | draft/approved/disabled/archived 四态 + `skill_drafts/`、`skill_archives/` 独立目录 | active/stale/archived 三态，全部留在 `skills/<id>/`；草稿概念由 SkillChange 记录取代 |
| 附件 | 无 | `references/`、`templates/`，进 revision 哈希，`skill_view` 按需读取 |
| 来源与写权 | source（user/distilled）+ 草稿一律审批 | origin（user/explicit/review）与 managed 分离：managed 技能直写留档，用户技能后台只出 proposed |
| 工具 | skill_list、skill_read、skill_propose、skill_find_evidence | skill_list、skill_view（含附件）、skill_manage（仅用户发起轮） |
| 沉淀 | 用户明确指定一次已完成工作后，前台 Agent 用 `skill_propose` 提交待审核草稿 | 后台 ReviewJob 方案已取消；未来接口须保持用户明确触发 |
| 轨迹 | 仅 `skill_tool_evidence`（工具名+参数键+成败，无配对、无序号、无返回）；时间线完全不存工具调用 | 持久轨迹：sequence 排序、tool_call_id 调用/返回配对、成败状态 |
| 使用统计 | `skill_run_links` 只记加载 | view/load/change 三类事件 + 陈旧标记（90 天） |
| HTTP/界面 | `/api/skill-drafts`、待审核 tab | `/api/skill-changes`、`/managed`、`/settings`、变更审批（差异+依据跳转原对话） |

## 2. 可复用

- `repository.py` 的 Git 事务、恢复日志、跨进程锁、原子写与路径安全校验——契约 §8 沿用同一机制，不动。
- `runtime.py` 的 Scope 与受控加载骨架——改字段名（`auto_match_skills`→`auto_match`）、目录格式与 revision 语义。
- Memory 的 `MemoryReviewScheduler` 整套模式（`memory_reviews` 表、done 后触发、`_kick_reviews` 空闲调度、启动中断恢复、一次性 SDK 会话、notice 发布）——skill 复盘按它镜像；差异是 skill 计数器全局（自上次技能写入起），不按任务。
- `mcp.invoke` 的边界记录钩子——从 evidence 记录改为轨迹记录。
- 前端 SkillPicker、任务页加载面板与管理页框架。

## 3. 步骤

### 阶段 A：数据模型与存储（对应 spec 阶段 1 基础）

- **A1** 重写 `models.py`：SkillMeta 收敛为 §2 字段；SkillStatus 改三态；删 SkillDraft/SkillInput/SkillEvidence，新增 SkillChange；revision 覆盖 name/description/正文/附件有序哈希清单。
- **A2** 改 `repository.py`：frontmatter 重渲染；附件读写（`_safe_path` 收紧到 references/、templates/）；取消 `skill_drafts/`、`skill_archives/`（归档技能留在原目录，state=archived）；目录名即 id，创建时校验格式。
- **A3** SQLite 升 v19：新增 `skill_changes`（契约 §5 字段）；`skill_run_links` 保留为 load 事件；删 `skill_draft_evidence`（本就无写入）。
- **A4** 改 `service.py`：create/update 走 SkillChange（user 修改直接 applied）；按 managed 决定直写或 proposed；approve 带 `expected_revision`，过期 409；恢复历史版本=patch 变更。
- **A5** 实例数据清空重建（已定，2026-09-22）：删除 `skill_drafts/`、`skill_archives/` 目录；`skills/` 下旧技能一并清掉；`skill_run_links` 历史行随任务保留但引用的技能不再存在，读取时按缺失跳过。不写迁移代码。

### 阶段 B：工具与加载（基础段收尾）

- **B1** 重写 `tools.py`：skill_read→skill_view（返回 revision 与附件清单，支持 `file_path`）；skill_propose→skill_manage（create/patch/write_file/remove_file + expected_revision，按 managed 分流）；删 skill_find_evidence 与 `evidence.py` 整个模块；toolset 中 skill_manage 仅 MESSAGE 轮。
- **B2** 改 `runtime.py` 与 `agent/client.py` 装配：目录材料改契约格式（50 条、含 revision）；selection 字段改名；scope 强制不变。
- **B3** HTTP 与前端：路由按契约 §10 重排（skill-drafts→skill-changes 等）；管理页 tab 改状态、加 managed 开关与变更审批（差异对比、依据条目跳转原对话）；Picker 随 selection 改名。

### 阶段 C：执行轨迹（spec 阶段 2）

- **C1** 持久化工具调用：在 `mcp.invoke` 边界把每次调用与返回写入轨迹存储（tool_call_id、sequence、tool 名、参数键、成败、返回关联）；v19 迁移直接 DROP `skill_tool_evidence`（已定，2026-09-22，旧表无读者、历史行不留档），写入钩子整体改写新轨迹存储；时间线新增工具条目类型或独立表，选型在实现时定，契约 §3 只约束可查询形状。
- **C2** 轨迹视图查询：按任务返回有序条目流，供 C 之后复盘装配与 evidence_item_ids 存在性校验。

### 阶段 D：自动沉淀（已取消，不实施）

- **D1** `skill_reviews` 表 + 全局计数器（自上次技能写入起累计完成消息轮，默认 10）；done 后钩子入队、同 `through_item_id` 去重、空闲调度、启动中断恢复，全部镜像 MemoryReviewScheduler。
- **D2** 复盘会话：一次性 SDK 会话，材料注入边界内轨迹快照 + 技能目录；工具仅 skill_list/skill_view/skill_manage；产出变更带 evidence_item_ids 与 reason。
- **D3** 复盘提示词：处理优先级与“不沉淀什么”按 spec §7 写进载荷结构（改模型行为先改载荷，见 KB 经验）。

### 阶段 E：维护（spec 阶段 4）

- **E1** 使用统计三类事件（view/load/change）与最近加载查询；`record_skill_view` 接线。
- **E2** 陈旧标记：active 技能 90 天无 load 转 stale 并提示；不做自动归档。
- **E3** `GET/PUT /api/skills/settings`（review_interval、review_enabled、stale_days）与前端设置入口。

## 4. 验证

每阶段跑既有测试并对齐改造：`test_skills.py`（A）、`test_skills_runtime.py`（B2）、`test_skills_http.py`（B3）、新增轨迹与复盘测试（C/D，参照 `test_skill_evidence.py` 的替身模式；该文件随 evidence 模块删除）。全部完成后按 spec §12 六个场景做真实模型验收；浏览器不可用，界面走组件测试 + 载荷取证。
