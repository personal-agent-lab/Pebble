# Skill 系统开发任务清单

本文件保留原始任务拆分；当前实现与验收状态以 `skills-acceptance.md` 为准。

基于现有代码结构、`docs/contracts/skills.md` 契约与规划文档，按依赖顺序拆分。

---

## P0：契约与技术验证

### 1. 更新 `docs/contracts/skills.md` 契约
- 补充多 Skill 组合、手动选择/排除、自动匹配、版本哈希、`expected_revision` 语义
- 明确草稿与生效版本分离、`409` 冲突、恢复会话刷新名单等行为
- 统一"用户编辑保存即批准"与"编辑后重新审核"的交叉描述

### 2. 技术验证 Qoder SDK Skill 加载
- 验证 `QoderAgentOptions.skills` 列表是否能让模型读取指定 Skill 文件内容
- 验证 `setting_sources=[]` 下 Skill 发现路径是否可控
- 验证恢复会话（`resume`）时 Skill 名单是否同步更新
- 验证草稿目录（`skill_drafts/`）是否对模型不可见
- 验证启用 Skill 是否会意外开启 Shell、文件读取等工具
- 记录验证结果，决定是否需要 `skill_read` 工具作为降级方案

---

## P1：数据模型与存储层

### 3. 定义 Skill 数据模型 `server/skills/models.py`
- `SkillStatus` 枚举：draft / approved / disabled / archived
- `SkillSource` 枚举：user / distilled
- `SkillMeta` 数据类：id、name、description、status、source、triggers、inputs、tools、side_effects、requires_confirmation、content_hash、schema_version、created_at、updated_at
- `Skill(SkillMeta)`：增加 body、evidence、base_revision
- `SkillVersion`：skill_id、revision、snapshot、approved_at、created_at
- `SkillDraft`：draft_id、skill_id(None=新建)、base_revision、skill、created_at、updated_at

### 4. 实现文件存储 `server/skills/repository.py`
- 文件布局：`<data_dir>/skills/<skill_id>/SKILL.md`、`<data_dir>/skill_drafts/<draft_id>/SKILL.md`
- YAML frontmatter + Markdown body 格式
- `load_skill(skill_id)`：读取并校验 approved_version 哈希
- `load_draft(draft_id)`：读取草稿
- `list_approved()`：只读元数据，返回 list[SkillMeta]
- `list_drafts()`：返回 list[SkillDraft]
- `save_skill(skill)`：写入文件，返回 content_hash
- `save_draft(draft)`：写入草稿目录
- `delete_draft(draft_id)`：删除草稿文件
- `move_to_archive(skill_id)`：移入 skill_archives/
- 所有写操作原子化：先写暂存文件，再 rename
- 路径安全校验：拒绝 ..、符号链接、ID 格式不合法

### 5. 实现版本历史 Git 管理（在 repository.py 中）
- 写入后立即 `git add <specific_path> && git commit`
- 不执行 `git add .`，只跟踪明确允许的路径
- `list_versions(skill_id)`：从 Git log 读取历史
- `restore_version(skill_id, revision)`：从历史生成恢复草稿
- Git 提交失败时回滚文件写入
- 启动时检查 .data/ 下的 Git 仓库是否存在，不存在则 git init

### 6. 实现 Skill 哈希与版本校验 `server/skills/validation.py`
- `compute_hash(body, triggers, inputs, tools, side_effects, requires_confirmation) -> str`
- 内容哈希覆盖影响执行的正文与元数据，排除 id、name、时间戳、content_hash 本身
- `validate_skill_format(skill) -> list[FieldError]`
- `validate_draft_format(draft) -> list[FieldError]`
- `validate_skill_id(skill_id) -> bool`：格式 sk_[a-zA-Z0-9]+

### 7. 新增 SQLite schema `server/db.py`（schema 9）
- `skill_run_links` 表：run_id、skill_id、revision、source(manual/auto)、loaded_at
- `skill_draft_evidence` 表：draft_id、task_id、created_at
- 文件存储是 Skill 正文的唯一事实来源，SQLite 只存运行关联

---

## P2：Service 层与 API

### 8. 实现 Skill 服务 `server/skills/service.py`
- `list_skills(status?, search?)`：搜索名称、描述、标签
- `get_skill(skill_id)`：含正文
- `create_skill(...)`：用户创建即批准
- `update_skill(skill_id, expected_revision, ...)`：带版本校验
- `disable_skill(skill_id)` / `enable_skill(skill_id)`
- `archive_skill(skill_id)`
- `create_draft(...)`：Agent 或用户创建草稿
- `update_draft(draft_id, expected_revision, ...)`
- `approve_draft(draft_id)`：批准并移入 skills/
- `reject_draft(draft_id)`
- `restore_version(skill_id, revision)`
- `get_versions(skill_id)`

### 9. 实现 Skill 目录服务 `server/skills/catalog.py`
- `available_catalog()`：只返回 status=approved 且哈希一致的
- `filter_for_turn(manual_ids, excluded_ids, max_skills)`：为单轮过滤
- `match_skills(goal_text, catalog)`：根据任务文本匹配候选
- `record_skill_usage(run_id, skill_id, revision, source)`：写入 skill_run_links

### 10. 实现 Skill Agent 工具 `server/skills/tools.py`
- `skill_list`（READONLY）：返回可用目录（名称+描述+状态）
- `skill_read`（READONLY）：加载指定 Skill 的完整内容
- `skill_propose`（LOCAL_WRITE）：创建或更新草稿
- `skill_find_evidence`（READONLY）：读取脱敏的执行依据
- 注册到 `server/tools/registry.py` 的 default_registry
- 不提供批准、启用、删除正式版本的模型工具

### 11. 添加错误类型 `server/errors.py`
- `SkillValidationError(errors: list[FieldError])`：字段校验失败
- 在 `error_details()` 中增加映射

### 12. 实现 REST API `server/api/routes.py`
新增路由（挂在 /api 前缀下）：
- `GET /skills`：搜索与状态筛选
- `POST /skills`：用户手动创建并启用
- `GET /skills/{id}`：详情
- `PATCH /skills/{id}`：带版本编辑保存
- `POST /skills/{id}/disable`
- `POST /skills/{id}/enable`
- `POST /skills/{id}/archive`
- `GET /skills/{id}/versions`：版本列表
- `POST /skills/{id}/restore`：从历史生成恢复草稿
- `GET /skill-drafts/{id}`：审阅草稿
- `PATCH /skill-drafts/{id}`：修改草稿
- `POST /skill-drafts/{id}/approve`：批准
- `POST /skill-drafts/{id}/reject`：驳回
- 请求体支持幂等键；编辑使用 expected_revision

### 13. 扩展消息提交接口 `server/api/routes.py`
- `MessageInput` 增加可选字段：skills（列表）、excluded_skill_ids（列表）、auto_match_skills（布尔）
- 旧客户端省略这些字段仍可正常提交
- 服务端校验手动选择的 ID、版本和状态

### 14. 装配 Skill 依赖 `server/main.py`
- 在 `create_app` 和 `create_production_app` 中构造 SkillService
- 注入到 ToolDeps（增加 skill_service 字段）
- 传入路由依赖

---

## P3：SDK 集成与运行时加载

### 15. 实现 Skill 加载器 `server/skills/loader.py`
- `load_approved_skills(data_dir) -> list[str]`：扫描 skills/ 目录，返回可用 Skill 名单
- `validate_skill_file(path) -> bool`：校验哈希一致性
- 每轮装配时调用，结果传入 `TurnContext.skills`

### 16. 集成到上下文装配 `server/agent/context.py`
- `assemble()` 增加 `skills` 参数
- `TurnContext.skills` 不再恒为空
- 将加载器返回的名单填入

### 17. 集成到 SDK 选项 `server/agent/client.py`
- `_options()` 中 `skills=ctx.skills` 已有占位，确保名单正确传入
- 恢复会话时同步当前批准名单

### 18. 集成到工具装配 `server/agent/toolset.py`
- `ToolDeps` 增加 `skill_service` 字段
- `build_tools()` 绑定 Skill 工具的依赖
- `ALLOWED_EFFECTS` 中 Skill 工具的副作用（READONLY / LOCAL_WRITE）按轮次筛选

### 19. 集成到 MCP 端点 `server/agent/mcp.py`
- 确保 skill_list、skill_read、skill_propose、skill_find_evidence 正确注册
- NEW_OPERATION_TOOLS 中不需要加 Skill 工具（它们不创建操作）

### 20. 扩展 Gateway 运行时 `server/gateway/runtime.py`
- `submit_message()` 接收并保存本轮选择的 Skill ID 和排除列表
- `_invoke()` 组装 Turn 时，加载手动选择和自动匹配的 Skill
- 调用 `catalog.record_skill_usage()` 记录实际加载
- Turn 数据结构增加 skill_ids、excluded_skill_ids、auto_match_skills 字段

### 21. 记录使用证据
- 每轮结束后，将实际加载的 skill_id、revision、source 写入 skill_run_links
- 为后续自动总结提供数据基础

---

## P4：前端 UI

### 22. 前端类型与 API `web/src/api.ts`
- 增加 Skill、SkillDraft、SkillVersion 类型定义
- 增加 API 函数：listSkills、createSkill、getSkill、updateSkill、disableSkill、enableSkill、archiveSkill、getSkillVersions、restoreSkillVersion、getSkillDraft、updateSkillDraft、approveSkillDraft、rejectSkillDraft
- sendMessage 扩展支持 skills、excluded_skill_ids、auto_match_skills

### 23. Skill 管理页面 `web/src/pages/SkillsPage.tsx`
- 三个区域：已启用、待审核、已停用/归档
- 搜索框：按名称、描述、标签过滤
- 每个 Skill 卡片：名称、描述、状态、操作按钮（编辑、停用、归档）
- 待审核区域：草稿列表，含来源依据

### 24. Skill 编辑器 `web/src/components/SkillEditor.tsx`
- 必填字段：名称、描述、提示词正文
- 可折叠高级字段：triggers、inputs、tools、side_effects
- 不要求普通用户填写 YAML，前端表单生成 frontmatter
- 保存时调用 API，处理版本冲突（409）

### 25. Skill 审核界面 `web/src/components/SkillReview.tsx`
- 展示草稿内容：为什么建议保存、适用/不适用场景、参数、步骤、验证方式
- 修改草稿内容
- 批准 / 驳回按钮
- 修改已有 Skill 时展示差异（diff）

### 26. 任务输入框 Skill 选择器 `web/src/components/SkillPicker.tsx`
- 输入框下方显示已选 Skill 标签（带 x 可移除）
- 点击"+ 添加 Skill"打开选择面板
- 选择面板：搜索、多选、取消选择、简短说明、可用状态
- "新建 Skill"入口
- 草稿和停用项不可选
- 手动移除的 Skill 在本轮自动匹配中也被排除

### 27. 侧栏 Skill 入口启用 `web/src/components/AppShell.tsx`
- PLACEHOLDERS 中的 Skill 改为 NavLink，指向 /skills
- 移除 disabled 状态

### 28. 路由注册 `web/src/App.tsx`
- 增加 `/skills` 路由指向 SkillsPage

---

## P5：自动匹配与持续改进

### 29. 实现自动匹配逻辑 `server/skills/catalog.py`
- 任务提交时，根据消息文本与 Skill 的 triggers/description 做初步匹配
- 返回候选列表供 Agent 在运行时按需加载
- 排除用户本轮明确排除的 Skill

### 30. 实现自动总结 `server/skills/evidence.py`（或集成到 service.py）
- 任务收尾阶段，Agent 判断是否存在可复用流程
- 通过 `skill_propose` 工具提交草稿
- 门槛：至少 3 个不同任务中重复成功
- 成功判据：工具序列稳定、操作最终成功、无重复纠正
- 证据字段由服务端核验，不接受模型自报
- 去重：相同内容指纹幂等；语义相似提示合并

### 31. Agent 提示词更新 `server/agent/prompt.py`
- 基础提示中增加 Skill 使用指引
- 说明何时查看目录、何时加载正文、如何组合多个 Skill
- 说明草稿提交规范

---

## P6：测试与验收

### 32. 后端单元测试 `tests/skills/`
- `test_models.py`：数据模型构造、哈希计算
- `test_repository.py`：文件读写、原子化、路径安全、Git 历史
- `test_validation.py`：格式校验、哈希一致性
- `test_service.py`：创建、编辑、审批、停用、恢复全生命周期
- `test_catalog.py`：目录筛选、自动匹配
- `test_tools.py`：Agent 工具调用
- `test_api.py`：REST 接口、409 冲突、幂等

### 33. 前端测试 `web/src/`
- `SkillPicker.test.tsx`：选择、移除、排除、搜索
- `SkillEditor.test.tsx`：表单校验、保存、冲突处理
- `SkillReview.test.tsx`：审批、驳回、差异展示
- `SkillsPage.test.tsx`：列表、搜索、状态切换

### 34. 集成测试
- 端到端场景：手动创建 → 多选 → 加载 → 运行记录 → 草稿 → 审批 → 新会话自动匹配 → 停用
- SDK 加载验证：确认 Skill 正文被模型读取
- 恢复会话：重启后 Skill 名单同步
- 并发：旧版本提交返回 409；重复批准只发布一次

### 35. 回归测试
- 邮件流程：新邮件触发、草稿编辑、确认发送
- 日历流程：查询、冲突检查、创建
- 聊天流程：纯对话轮次不调用工具
- 数据库迁移：schema 9 升级不影响已有数据

### 36. 工程检查
- `ruff check --config server/pyproject.toml server/`
- `cd web && npm run typecheck`
- `cd web && npm test`
- `cd web && npm run build`

---

## 里程碑划分

| 里程碑 | 任务 | 验收条件 |
|--------|------|----------|
| M1：SDK 验证 | 1-2 | 证明受控加载、多选与恢复会话可行 |
| M2：存储与管理 | 3-7, 8-11 | 创建、编辑、停用、审核和重启恢复通过 |
| M3：手动使用 | 12-14, 15-21, 22-28 | 用户自写 Skill 后可在任务中真实加载 |
| M4：自动匹配 | 29 | 正确匹配、不相关不加载、来源可见 |
| M5：自动总结 | 30-31 | 成功流程生成草稿，批准后跨会话复用 |
| M6：完整验收 | 32-36 | 检查、构建和端到端场景全部通过 |
