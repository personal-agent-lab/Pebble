# Skills 契约

产品行为以 `docs/skill-spec.md` 为准；本文定义技能的文件格式、数据模型、Agent 工具、复盘与变更接口及管理页 HTTP 的字段和语义。实现与本文冲突时先改本文。与记忆、资料库的分工见 `skill-spec.md` 第 10 节；技能工具的领域语义只写在工具说明与本文，不进入通用层。

## 1. 存储与职责

```text
<data_dir>/skills/<skill_id>/SKILL.md            # 正文与元数据
<data_dir>/skills/<skill_id>/references/…        # 参考资料附件
<data_dir>/skills/<skill_id>/templates/…         # 模板附件
```

- 正文、附件与管理元数据（frontmatter）以文件为准一式保存；`skills/` 由实例数据目录的本地 Git 仓库管理，只提交 `skills/` 下的路径，不推送。
- `skill_id` 即目录名，格式 `^[a-z0-9][a-z0-9_-]{0,63}$`，创建后不变；显示名在 frontmatter 中，可改。不使用独立的 id 字段。
- SQLite 只存运行与管理状态：复盘计数与边界、复盘任务（ReviewJob）、变更记录（SkillChange）、加载与使用记录；不存正文。表不进入 Git。
- 附件路径只能是 `references/`、`templates/` 下的相对路径，拒绝 `..`、绝对路径与符号链接。不提供 `scripts/`：附件是数据，不是可执行内容。

## 2. Skill 模型

`SKILL.md` 由 YAML frontmatter 与 Markdown 正文组成。frontmatter 字段：

| 字段 | 类型 | 语义 |
| --- | --- | --- |
| `name` | str | 显示名，必填，非空 |
| `description` | str | 给模型判断相关性的一句话，必填，≤160 字符 |
| `origin` | `user` / `explicit` / `review` | 谁创建：管理页手写 / 用户当轮明确要求学习 / 后台复盘沉淀 |
| `managed` | bool | 后台复盘是否可直接修改；`origin=user` 恒为 false，其余默认 true，用户可在管理页改 |
| `state` | `active` / `stale` / `archived` | 运行状态，见下 |
| `created_at`、`updated_at` | datetime | 程序维护 |

`origin` 与 `managed` 是两个问题：前者记录来源事实，后者是管理策略，不互相推导（除 user 恒 false）。

正文建议按适用场景 / 前置条件 / 操作步骤 / 验证方法 / 常见问题组织，写法不限。正文与附件内容不进 SQLite，完整哈希即版本：

- `revision` = SHA-256，覆盖 `name`、`description`、规范化正文、全部附件的有序 `(relative_path, content_hash)` 列表。不覆盖 `origin`、`managed`、`state`、时间戳与哈希自身；改管理策略不产生新版本。
- 附件另记 `SkillFile`：`skill_id`、`relative_path`、`content_hash`，派生自文件，不单独保存。

状态机：`active` ⇄ `stale`；任一状态可归档（`archived`，由用户发起）；归档恢复直接回 `active`，照常加载（`skill-spec.md` §8）。只有 `active` 技能进入目录、可被装配与读取；`stale`、`archived` 不进上下文，但保留在管理页与 Git 历史中。删除技能不做；退出使用走归档。

## 3. 执行轨迹

不新建轨迹存储。轨迹是既有任务时间线与工具记录的视图，补齐以下可查询形状即可：

| 实体 | 字段 | 来源 |
| --- | --- | --- |
| 任务（对应 Session） | `task_id`、`created_at` | 既有任务表 |
| 轮（对应 Turn） | `turn_id`、`task_id`、`status`（`running/completed/failed/cancelled`）、`started_at`、`ended_at` | 既有轮次记录 |
| 条目（对应 Message） | `item_id`、`turn_id`、`sequence`、`role`（`user/assistant/notice/tool`）、`content`、`tool_call_id`、`created_at` | 既有时间线 |
| 工具调用 | `tool_call_id`、`turn_id`、`name`、`arguments` 键、`status`（成功/失败）、`created_at` | 既有工具执行记录 |

- `sequence` 全任务单调递增，保证顺序可恢复；工具返回条目用 `tool_call_id` 与调用配对，可重建“调用 → 失败 → 调整 → 成功 → 回答”的完整链。
- `status=completed` 只表示执行结束，不表示方法经验证；判断权在复盘（`skill-spec.md` §5）。
- 轨迹留在 SQLite，不进技能 Git 仓库；外部格式导出不在范围内。

## 4. ReviewJob：一次后台复盘

| 字段 | 类型 | 语义 |
| --- | --- | --- |
| `id` | str | |
| `trigger` | `interval` / `manual` | 轮数阈值触发 / 用户在管理页手动发起 |
| `through_item_id` | str | 输入边界：只复盘到此条目 |
| `since_item_id` | str \| null | 上边界：上次复盘或上次写入的边界 |
| `status` | `pending/running/completed/failed` | |
| `result_summary` | str \| null | 复盘结论（含“无值得保存的经验”） |
| `error` | str \| null | |
| `created_at`、`finished_at` | datetime | |

- 计数器存 SQLite：自上次技能写入以来累计完成的用户消息轮数，达到阈值（默认 10，可配置、可关闭）即入队，写入后清零。阈值内多次完成不重复入队（同一 `through_item_id` 去重）。
- 输入由程序装配为材料注入：边界内已完成轮次的轨迹快照 + 当前 `active` 技能目录。复盘不接续对话、不进任务时间线、不等用户消息；工具只有 `skill_list`、`skill_view`、`skill_manage`（受第 6 节限制）。
- 失败只记 `error`，计数不清零，下次阈值再触发；重启时 `running` 记为 `failed`。
- 与记忆的后台回顾独立：各自计数、各自会话、互不触发。

## 5. SkillChange：一次变更

“提出修改”与“应用修改”分离，审批、审计、回滚都基于此表：

| 字段 | 类型 | 语义 |
| --- | --- | --- |
| `id` | str | |
| `review_job_id` | str \| null | 来源复盘；用户或前台修改为空 |
| `skill_id` | str \| null | `action=create` 时为空，目标 id 在 payload 中 |
| `action` | `create/patch/write_file/remove_file` | |
| `payload` | dict | create：`skill_id`、frontmatter、正文；patch：`old_string`/`new_string` 或整份正文；write_file：`relative_path`、`content`；remove_file：`relative_path` |
| `base_revision` | str \| null | 基于的当前版本；create 为空 |
| `reason` | str | 为什么改，给审批页看 |
| `evidence_item_ids` | list[str] | 经验依据的时间线条目，必须真实存在且属于边界内轮次，程序校验存在性、不校验语义 |
| `actor` | `user/foreground/review` | 管理页 / 用户当轮要求的前台 Agent / 后台复盘 |
| `status` | `proposed/applied/rejected/conflict` | |
| `created_at`、`applied_at` | datetime | |

写入规则按目标技能的 `managed` 决定：

- **`managed=true`**（含复盘自建技能与前台明确学习）：变更直接应用，`status=applied`，写入文件并提交 Git，记录留档审计；出错靠 Git 历史回滚（§8 恢复）。
- **`managed=false`（用户技能）**：后台复盘只能产生 `proposed` 变更，正文不动；用户在管理页批准后应用。`actor=foreground` 的修改例外——用户当轮明确要求改某个技能时直接应用，靠 `skill_manage` 只在用户发起轮可见保证。
- 应用的并发检查：`base_revision` 与磁盘当前 `revision` 不一致时不应用，记 `conflict`；批准接口要求传 `expected_revision`，过期返回 409。
- 复盘的处理优先级（补正使用中的、其次修既有、最后才新建）与“不沉淀什么”写在复盘提示词中（`skill-spec.md` §7），程序不强制排序，只留记录。

## 6. Agent 工具

| 工具 | 可见轮次 | 输入 | 输出 |
| --- | --- | --- | --- |
| `skill_list` | 所有轮 | 可选 `state` 过滤（默认 `active`） | 目录：`skill_id`、`name`、`description`、`revision` |
| `skill_view` | 所有轮 | `skill_id`、可选 `file_path`（附件相对路径） | 正文或附件内容、当前 `revision`、附件清单（`relative_path`、`content_hash`）；并记一次加载 |
| `skill_manage` | 仅用户发起的对话轮 | `action` 及对应 payload（同 §5）、`expected_revision` | 应用结果或（对 `managed=false` 技能的后台调用）`proposed` 的变更 id |

- 目录常驻装配进每轮材料：最多 50 条、每条 `name` + `description`（超 160 字符截断），超出按最近使用时间取前 50 并注明有省略。正文不进目录。
- 手动选择随消息提交（multipart `selection` 字段，JSON）：`{"skills": [{"id": …}], "excluded_skill_ids": […], "auto_match": true}`。手动项最多 10 个、装配正文合计 ≤40000 字符；发送时绑定当前 `revision` 并记入加载记录（`source=manual`）；`skill_view` 读取记 `source=auto`。排除项与关闭自动匹配由 `skill_list`/`skill_view` 强制执行。
- `skill_view` 只返回 `active` 技能；`archived`、`stale`、不存在一律 `unknown_skill`。
- 无 `delete` 工具；归档、恢复、`managed` 切换只在管理页。技能不授予任何外部工具权限，外部操作仍走既有确认与轮次限制。

## 7. 运行时内部接口

模型不接触计数器、调度与持久化细节，由运行时管理：

```python
# 轨迹（既有记录的写入与查询）
append_timeline_item(task_id, turn_id, item) -> item_id
finish_turn(turn_id, status)

# 复盘调度
should_review() -> bool                      # 计数达到阈值且未关闭
enqueue_review(through_item_id, trigger) -> ReviewJob   # 同边界去重
run_review(review_job_id)                    # 装配材料，独立会话执行

# 变更管理
record_change(change) -> SkillChange         # 按 §5 规则决定直接应用或 proposed
apply_change(change_id, expected_revision, actor="user")
reject_change(change_id)

# 加载与维护
record_skill_load(task_id, turn_id, skill_id, revision, source)
record_skill_view(skill_id, task_id)         # 使用统计，见 §9
archive_skill(skill_id) / restore_skill(skill_id)
```

## 8. 版本历史与恢复

- 每次应用的变更产生一次 Git 提交，提交信息含 `change_id` 与 `actor`；`GET /api/skills/{id}/versions` 从 Git log 派生：`revision`、`created_at`、`change_id`、`actor`、`reason`。
- 恢复历史版本 = 以目标 `revision` 的内容创建一个 `actor=user` 的 `apply` 变更（`action=patch` 整份正文替换），产生新提交，不重置历史。
- 文件写入与 Git 提交同临界区（复用 Memory/资料库的进程内锁与跨进程文件锁）；提交失败回滚文件，不留半完成状态。直接改磁盘文件不产生变更记录，运行时按 `revision` 校验发现不一致时该技能退出目录并告警，不静默加载。

## 9. 使用统计

事件分三类，不混计：`view`（`skill_view` 目录级查看或管理页查看，正文未进上下文）、`load`（正文进入本轮材料，手动装配或 `skill_view` 读取）、`change`（变更应用）。被加载不等于帮助任务成功，统计只反映活动，不用于自动评价技能好坏。陈旧判定：`active` 技能连续 90 天无 `load` 事件标记 `stale` 并提示用户（阈值可配置）；不做自动归档、不做自动合并。

## 10. HTTP 与界面

| 接口 | 输入 | 输出 |
| --- | --- | --- |
| `GET /api/skills` | 可选 `state`、`q` | 目录列表：`skill_id`、`name`、`description`、`origin`、`managed`、`state`、`revision`、`updated_at`、最近 `load` 时间 |
| `POST /api/skills` | `skill_id`、`name`、`description`、正文、可选附件 | 创建 `origin=user` 技能；冲突的 `skill_id` 返回 422 |
| `GET /api/skills/{id}` | — | 详情：frontmatter 全字段、正文、附件清单、`revision`、`usage` |
| `PUT /api/skills/{id}` | 可改字段、`expected_revision` | 保存；409 见 §11 |
| `POST /api/skills/{id}/archive`、`/restore`、`/managed` | `/managed` 传 `value` | 状态与管理策略切换 |
| `GET /api/skills/{id}/versions`、`/versions/{revision}` | — | 历史列表 / 指定版本正文 |
| `GET /api/skill-changes` | 可选 `status`、`skill_id` | 变更列表：§5 全字段 |
| `POST /api/skill-changes/{id}/approve` | `expected_revision` | 应用 proposed 变更 |
| `POST /api/skill-changes/{id}/reject` | — | 驳回 |
| `GET /api/tasks/{task_id}/skill-usage` | — | 本任务加载记录：`skill_id`、`revision`、`source`、轮次 |
| `GET/PUT /api/skills/settings` | `review_interval`、`review_enabled`、`stale_days` | 复盘与陈旧配置 |

管理页 `/skills`：目录浏览、正文与附件查看、创建、编辑、启停归档恢复、`managed` 切换、历史版本与恢复、待审变更（差异对比、依据条目跳转原对话）、使用统计。发起任务的输入框提供技能多选与排除，随消息提交 `selection`。字段错误 422；版本冲突 409 附 `current_revision`；不存在 404。

## 11. 错误

通用错误见 `v1-design.md` §3。字段校验失败（含非法 `skill_id`、附件路径越界、`payload` 与 `action` 不符、`evidence_item_ids` 含不存在条目）返回 `invalid_skill`（HTTP 422），附 `errors[]`；`expected_revision`/`base_revision` 过期返回 `skill_conflict`（HTTP 409），附 `current_revision`，不写入；技能或附件不存在、状态不可读返回 `unknown_skill`（HTTP 404）；文件或 Git 不可用返回 `skill_store_unavailable`（HTTP 503），不留半完成修改。
