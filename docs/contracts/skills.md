# Memory 与 Skills 契约

状态：Skills 已实现；Memory 保留约定，尚未实现。Skills 的本地验收记录见 `../skills-acceptance.md`。

## 1. 存储与职责

Skill 正文保存在 `<data_dir>/skills/<skill_id>/SKILL.md`；待审核草稿在
`skill_drafts/<draft_id>/SKILL.md`；正式归档在 `skill_archives/<skill_id>/SKILL.md`。
已批准或驳回的草稿保留关闭状态与 Git 历史，不再出现在待审核列表中。
实例数据目录使用独立 Git 仓库，只提交指定 Skill 路径，不提交凭证、数据库或 SDK 会话，不推送。
SQLite schema 10 仅保存运行与 Skill 版本关联、工具执行证据，不保存正文。

## 2. Memory 文件格式

```yaml
---
id: mem_01HZR2K9QX4T1B
kind: correction            # preference | correction | fact
summary: 会议默认时长 30 分钟
created_at: 2026-09-14T10:22:31+08:00
updated_at: 2026-09-14T10:22:31+08:00
source:                     # 触发该规则的用户纠正
  task_id: task_01HZR2K0
  quote: 以后约会议默认半小时，别写一小时
---

规则本身，一句话可执行。

**适用边界：** 只用于内部会议；对外邀请按对方给出的时长。
```

每轮系统提示装配时加载全部生效规则，作为一个固定标题的材料块（`## 当前生效规则`）追加在基础提示之后、本轮触发材料之前。规则不得覆盖外部写确认约束：该约束由程序（每轮工具可见范围与 Confirmation）保证，不依赖规则文本或模型自律。

## 3. Skill 文件与版本

文件由 YAML frontmatter 与 Markdown 正文组成。字段：id、name、description、status、source、
schema_version、triggers、inputs（name/required/note）、tools、side_effects、requires_confirmation、
content_hash、approved_version、base_revision、evidence、created_at、updated_at。

内容版本为完整 SHA-256；覆盖名称、描述、规范化正文、触发条件、参数（包含 note）、工具与副作用声明。
排除状态、来源、时间戳和哈希自身。名称和描述影响匹配与审核，也参与版本冲突检测。
稳定 ID 不因改名变化。旧开发版本的短哈希不会被自动信任，用户在管理页审阅并保存后重新计算。

## 4. 生命周期

- 用户新建或编辑保存即批准并启用，返回当前内容版本。
- Agent 只能创建草稿；修改建议记录当前正式版本为 base_revision，原版本继续可用。
- 草稿编辑和批准要求 expected_revision，过期返回 409；修改建议同时校验正式版本未改变。
- 重复批准同一已发布版本不重复发布；被驳回的草稿不能批准。
- 已启用可停用、再启用或归档。归档只通过历史恢复产生草稿，经用户批准重新生效。
- 直接修改磁盘文件不等于批准；运行时重新计算哈希，不一致或缺少批准版本时拒绝加载。
- 停用、归档、草稿不能通过 skill_read 加载。重新启用也不能信任被改动的内容。

## 5. 文件与 Git 一致性

写入使用进程内重入锁和跨进程文件锁；版本检查与写入处于同一临界区。
恢复记录保存指定路径的旧、新内容，先落盘恢复记录，再原子替换文件，最后提交指定路径。
提交成功后清除恢复记录；失败回滚。下次访问时检查未完成恢复记录，依据 HEAD 是否已含完整新版本，
完成发布或恢复旧内容。读操作使用同一锁，不能读取未完成发布的内容。
路径按 ID 生成，拒绝路径穿越和 Skill 目录/文件符号链接；实例根先解析操作系统路径别名。
历史恢复创建新的草稿和提交，不重置历史。

## 6. 每轮受控加载

消息协议兼容旧客户端，增加：

```json
{
  "message": "整理这周安排",
  "skills": [{"id": "sk_example", "revision": "sha256:..."}],
  "excluded_skill_ids": [],
  "auto_match_skills": true
}
```

手动选择最多 10 项、正文总长最多 40000 字符，超过限额明确报错；提交和实际执行时都校验状态与版本。
选择与版本随运行输入持久化。手动正文装配到本轮材料，自动匹配先提供最多 50 条可用目录摘要，
由同一个 Agent 按需调用 skill_read；无需相关流程时可以不加载。排除项与关闭自动匹配在工具端强制执行。
Skill 与用户本轮要求冲突时以用户要求为准；Skill 之间冲突时追问。

SDK 保持 tools=[]、setting_sources=[]、skills=[]。不依赖项目配置发现，也不导出到 `.qoder/skills/`。
理由：原实现仅传名称且没有受控加载验证，不能证明正文加载；改为材料装配与受限读取。
恢复会话时重新构造目录与校验版本，但已存在于 SDK 历史上下文中的文字不能被抹除。
运行时记录实际装配/读取的 Skill ID、版本与 manual/auto 来源；网页展示这些加载记录。
加载记录证明内容交付给模型，不证明模型完成流程或外部操作成功。

## 7. 工具与自动总结

| 工具 | 副作用 | 行为 |
| --- | --- | --- |
| skill_list | readonly | 本轮过滤后的批准目录，不含草稿正文 |
| skill_read | readonly | 校验批准、状态、版本与排除后读取正文，并记录加载 |
| skill_find_evidence | readonly | 返回已完成运行中的成功工具序列及任务 ID |
| skill_propose | local_write | 核验依据后提交草稿，不能批准 |

工具证据在 MCP 调用边界记录工具名、参数键、成功/失败状态、运行与时间，不记录参数值或结果正文。
服务端只接受至少三个不同已完成任务的相同成功工具序列，不信任模型声明的次数。
失败、冲突或待核实状态不计入成功；准备草稿成功只证明准备步骤成功，不证明外部投递成功。
同名且来源相同的待审核建议复用已有草稿；模型应抽象参数，避免私密内容，优先改进同类 Skill。
任务收尾是否值得总结由现有 Agent 判断，不新增循环、定时任务或强制每轮建议。
Memory 尚未实现，因此不宣称已核验跨任务“无重复纠正”。

## 8. HTTP 与界面

`/api/skills` 支持列表、状态筛选、搜索与创建；`/{id}` 支持详情和带版本编辑；
`/{id}/disable`、`enable`、`archive` 管理状态；`versions` 查看历史正文；
`restore?revision=...` 生成恢复草稿。
`/api/skill-drafts` 列出待审核项，`/{id}` 查看和编辑，`approve`（expected_revision）批准，`reject` 驳回。
`/api/tasks/{task_id}/skill-usage` 返回任务中已加载版本及来源。

Web `/skills` 提供创建、搜索、状态筛选、编辑、审核、依据查看、当前正文与草稿对比、历史恢复。
新任务与回复输入框多选可移除，移除项加入本轮排除列表；成功发送后重置选择。
字段错误返回 422，版本冲突返回 409 并附 current_version，不存在返回 404。

## 9. 权限边界

不提供批准、启用、删除正式版本的模型工具。local_write 仍按已有轮次权限开放，
新邮件轮仅 readonly；批准 Skill 不授予外部工具权限，既有邮件确认与日历用户轮限制不变。
Markdown/正文按文本展示，不执行任意 HTML、脚本或 Shell。
认证和 HTTPS 远程访问尚未实现，本机使用；真实账号验收属于单独明确授权的工作。
