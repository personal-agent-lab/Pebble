# Skill 系统重构任务清单

目标约定是 `skill-spec.md` 与 `contracts/skill.md`（2026-09-22 重写）；旧实现按旧契约 `contracts/skills.md`（已删除）于 2026-09-19 验收。本文记录两者的差距与重构步骤；实现状态以 `status.md` 为准。

各阶段的当前实现与验收状态以 `status.md` 为准；下列步骤保留交付拆分与对应代码位置。

## 1. 主要差距

| 方面 | 旧实现 | 目标 |
| --- | --- | --- |
| 标识 | `sk_<hex>` 随机生成，id 即目录名 | 目录名即 `skill_id`，用户可读（旧 `sk_` 名恰符合新格式，可原样保留） |
| 元数据 | triggers、inputs、tools、side_effects、requires_confirmation、evidence、schema_version、approved_version | 仅 name、description、origin、managed、state、时间戳 |
| 状态 | draft/approved/disabled/archived 四态 + `skill_drafts/`、`skill_archives/` 独立目录 | active/stale/archived 三态，全部留在 `skills/<id>/`；草稿概念由 SkillChange 记录取代 |
| 附件 | 无 | `references/`、`templates/`，进 revision 哈希，`skill_view` 按需读取 |
| 来源与写权 | source（user/distilled）+ 草稿一律审批 | origin（user/explicit/review）与 managed 分离：managed 技能直写留档，用户技能后台只出 proposed |
| 工具 | skill_list、skill_read、skill_propose、skill_find_evidence | skill_list、skill_view（含附件）、skill_manage（仅用户发起轮） |
| 沉淀 | 前台 Agent 收尾时 skill_propose + ≥3 任务相同工具序列核验 | 后台 ReviewJob 复盘；程序只校验 evidence_item_ids 存在性，不校验序列 |
| 轨迹 | 仅 `skill_tool_evidence`（工具名+参数键+成败，无配对、无序号、无返回）；时间线完全不存工具调用 | 持久轨迹：sequence 排序、tool_call_id 调用/返回配对、成败状态 |
| 使用统计 | `skill_run_links` 只记加载 | view/load/change 三类事件 + 陈旧标记（90 天） |
| HTTP/界面 | `/api/skill-drafts`、待审核 tab | `/api/skill-changes`、`/managed`、`/settings`、变更审批（差异+依据跳转原对话） |

## 2. 复用与偏差

- `repository.py` 的 Git 事务、跨进程锁与原子写、路径安全校验——契约 §8 沿用同一机制。旧文件与旧表在重写时整体替换，不复用旧技能数据。
- `runtime.py` 的 Scope 与受控加载骨架——改为 `excluded_skill_ids`/`auto_match`/`manual_skill_ids`，目录格式与 revision 语义按新契约。
- Memory 的 `MemoryReviewScheduler` 整套模式（`memory_reviews` 表、done 后触发、`_kick_reviews` 空闲调度、启动中断恢复、一次性 SDK 会话、notice 发布）——skill 复盘按它镜像；差异是 skill 计数器全局（自上次技能写入起），不按任务。
- `mcp.invoke` 的边界记录钩子——从 evidence 记录改为轨迹记录（阶段 C）。
- 前端 SkillPicker、任务页加载面板与管理页框架已按新契约重写。

**已定偏差（2026-09-23，随实现更新本文）**

- **A3**：v19 迁移**删除全部三张旧表**（`skill_run_links`、`skill_draft_evidence`、`skill_tool_evidence`）并新建 `skill_changes` + `skill_loads`，不保留 `skill_run_links` 作 load 事件——旧表结构（`run_id` 外键、无 task_id 索引）与新加载记录不一致，且新旧技能 id 不同源，历史加载行无读者。旧表数据随迁移丢弃。
- **A5**：实例数据清空重建。`.data/` 无 `skills/`、`skill_drafts/`、`skill_archives/` 目录，无需迁移代码。
- **B2**：目录材料只带 `skill_id`、`name`、`description`（≤160 字符截断），不带 `revision`——契约 §6 的规定优先；`revision` 只在 `skill_list` 工具与 `skill_view` 的返回值里出现。

## 3. 步骤

### 阶段 A：数据模型与存储（对应 spec 阶段 1 基础）——已完成

- **A1** ✅ `models.py`：frontmatter 收敛为 §2 字段；SkillState 三态；新增 SkillChange/ChangeAction/ChangeActor/ChangeStatus；revision 覆盖 name/description/正文/附件有序哈希清单，不含 origin/managed/state/时间戳。
- **A2** ✅ `repository.py`：frontmatter 重渲染；附件读写（`_safe_path` 收紧到 `references/`、`templates/`）；取消 `skill_drafts/`、`skill_archives/`（归档技能留在原目录，state=archived）；目录名即 id，创建与读写路径都校验格式；手写 frontmatter 缺 origin/managed/state 时按“用户来源、需确认、启用”兜底。
- **A3** ✅ SQLite 升 v19：新建 `skill_changes`（契约 §5 全字段）与 `skill_loads`；删 `skill_run_links`、`skill_draft_evidence`、`skill_tool_evidence`（见上偏差）。
- **A4** ✅ `service.py`：create/patch/write_file/remove_file 走 SkillChange；按 actor × managed 决定直写或 proposed；`expected_revision` 过期 409 附 `current_revision`；恢复历史版本=整份 patch 变更；归档、恢复、`managed` 切换写文件并提交但不建变更、不升版本；`origin=user` 恒为 `managed=false`。
- **A5** ✅ 实例数据清空重建，不写迁移代码。

### 阶段 B：工具与加载（基础段收尾）——已完成

- **B1** ✅ `tools.py`：skill_list、skill_view（正文或附件、revision、附件清单 `relative_path`/`content_hash`）、skill_manage（四 action + `expected_revision`，仅 MESSAGE 轮可见）；`evidence.py` 与 skill_find_evidence 已删（轨迹与依据校验留到 C）。
- **B2** ✅ `runtime.py` 与 `agent/client.py` 装配：目录材料按契约 §6；选择字段改名；排除与关闭自动匹配由 `skill_list`/`skill_view` 强制执行；装配层复检手动选择额度。
- **B3** ✅ HTTP（契约 §10，缺 settings）与前端：`/api/skills` 管理接口、`/api/skill-changes` 审批、`/api/tasks/{id}/skill-usage`；管理页（列表、详情、创建编辑含 409、附件、managed、归档恢复、版本历史恢复、待审变更）、输入框 SkillPicker、任务页加载面板。

### 阶段 C：执行轨迹（spec 阶段 2）——Pebble MCP 与内置工具路径完成（2026-09-24）；真实验收边界见 `status.md`

- **C1** ✅ 持久化工具调用：在 `mcp.invoke` 边界把每次调用与返回写入轨迹存储（tool_call_id、sequence、tool 名、参数含值、成败、返回内容）；轨迹条目并入 `task_timeline_items`（v20：加 `sequence` 列按 rowid 回填 + `tool` 条目），任务页以可折叠细行渲染（含展开完整参数与返回、复制原始记录、依据定位自动展开；2026-09-24 与用户确认，参数含值已改契约 §3）。被拒调用（unknown_tool、wrong_target）与业务失败同样记为失败；一次性会话（run_id 为空）不记；写入失败只告警不中断调用。
- **C2** ✅ 轨迹视图查询：`server/sessions/trajectory.py` 按任务返回有序条目流（轮状态映射 running/completed/failed/cancelled、条目角色 user/assistant/notice/tool），支持 `since_item_id`/`through_item_id` 边界截取；`item_within_boundary` 供 D 阶段 evidence_item_ids 边界校验。

### 阶段 D：自动沉淀（spec 阶段 3）——代码接入完成，真实模型验收见 `status.md`

- **D1** ✅ `skill_review_turns` 完成序号、全局游标与 `skill_reviews`；done 同事务登记、阈值入队、空复盘推进、失败后下一轮重试与启动恢复。
- **D2** ✅ 一次性 SDK 会话读取窗口轨迹、目录及已加载 Skill；复盘专用的 skill_list/skill_view/skill_manage 只暂存候选，正常结束后按证据边界、当前版本与 `managed` 应用或提出建议；候选先落库以便中断恢复。
- **D3** ✅ 复盘提示词按 spec §7 优先补旧、不保存临时错误；真实模型验收与使用边界见 `status.md`。手动复盘及网页设置留到 E 阶段。

### 阶段 E：维护（spec 阶段 4）

- **E1** 使用统计三类事件（view/load/change）与最近加载查询；`record_skill_view` 接线。
- **E2** 陈旧标记：active 技能 90 天无 load 转 stale 并提示；不做自动归档。
- **E3** `GET/PUT /api/skills/settings`（review_interval、review_enabled、stale_days）与前端设置入口。
- **E4** 审计增强：区分“服务写入”与“用户自行 git 提交”，补上直接提交的变更留档（当前只有工作区干净度判定）。

## 4. 验证

- 每阶段跑既有测试并对齐改造：`test_skills.py`（A）、`test_skills_service.py`（A4）、`test_skills_runtime.py`（B2）、`test_skills_http.py`（B3）；轨迹测试为 `tests/gateway/test_trajectory.py`（C：记录、顺序、重启持久、重试保留、一次性会话不记），复盘测试随 D 新增。
- 阶段 A/B 完成后按 spec §12 场景 1、2 做真实模型验收，三种方式都做：`tests/acceptance/qoder_skills_basic.py`（临时实例 + 真实 Qoder 模型，断言附证据）、管理页与选择器的浏览器点验（Playwright 驱动的创建→编辑→冲突→版本恢复、Picker 勾选与排除随消息提交）、`http://127.0.0.1:8000` 上真实运行实例的对话入口走查（含手动选择、排除、关闭自动匹配与 409/422/404 边界）。
- 阶段 C 已按 spec §12 场景 7 的轨迹一半做真实模型验收：`tests/acceptance/qoder_skills_trajectory.py`（走生产 GatewayRuntime 路径：失败链重建、重启持久、轮状态映射、依据边界校验、时间线载荷），2026-09-24 通过；复盘对照的一半随 D 补。
