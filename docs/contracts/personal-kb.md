# 个人知识库契约

状态：**Phase 2 已实现**（`kb_list`/`kb_save`/`kb_read`/`kb_update`/`kb_history`/`kb_search`，含 SQLite FTS5 分节索引）。Phase 3–5 是约定，尚未实现。产品行为以 `docs/kb-spec.md` 为准；本文定义个人知识库（Personal KB）的存储形态、索引、工具字段与引用语义。实现与本文冲突时先改本文，不静默偏离。

当前代码与本文的已知差异：代码仍保留 frontmatter 的 `source` 字段、`kb_save`/`kb_update` 的 `source` 参数与 `kb_search` 的 `source_kind` 筛选；按本文移除（资料不记录出处）。

知识库保存“具体资料”，属于按需检索；Memory 保存“关于用户的精简背景、偏好与目标”，属于常驻上下文，见 `skills.md` 与 `memory-spec.md`。

## 0. 交付阶段

| 阶段 | 内容 | 状态 |
| --- | --- | --- |
| Phase 1 | `kb/` 文件存储 + 本地 Git 版本；资料列举、保存、读取、更新和历史读取；稳定 `id`、版本历史；与长期记忆的边界 | 已实现 |
| Phase 2 | 按内容检索：`kb_search` + SQLite FTS5 分节索引、增量替换与重建；按路径、`id` 或引用读取原文；回答不向用户展示来源 | 已实现 |
| Phase 3 | 自动跟随用户在文件系统里的改动（自动纳入版本、识别移动与重命名、为无标识文件补标识）；`kb_delete`/`kb_move`/`kb_restore`；触发轮开放资料新建与修改 | 未实现 |
| Phase 4 | 资料管理界面（浏览、搜索、编辑）；`kb_archive` 任务归档 | 未实现 |
| Phase 5 | 主题页、主题目录常驻上下文与后台主题整理；外部笔记导入（首个来源熊掌记） | 未实现 |

Phase 2 的索引只收录内容与其 Git 版本一致的文件：用户直接在编辑器里改过的内容在纳入版本之前不会被索引，且该文件在改动提交前不保证可检索——增量更新会暂时保留其旧条目，一旦索引整体重建，这份文件会整份缺席，直到改动纳入版本。Phase 3 之前不承诺“改完立刻可检索”，也不承诺移动或删除后自动收敛。

## 1. 存储形态

资料是实例数据目录内的 Markdown 文件加目录结构，用户可以直接阅读、修改和重组。`kb/` 与 `memory/`、`skills/` 同属实例数据目录内的独立本地 Git 仓库，不是代码仓库；写入即提交，不推送远端。

```text
<data_dir>/kb/
├── inbox/            # 未分类的资料
├── people/           # 人物，一人一文件
├── projects/         # 事项与项目
├── reference/        # 稳定参考资料
├── topics/           # 主题页（Phase 5），后台整理只写这里
└── archive/          # 任务归档：关键信息与逐项执行结果
```

除 `topics/` 与 `archive/` 外，上图只是示例，不是固定分类法。保存时由 Agent 按内容自选相对路径、可新建文件夹；省略 `path` 时落到 `kb/inbox/`。索引、主题目录与导入状态都是派生或运行数据，放在数据目录内且不进 Git，可随时从文件重建（导入状态除外，见第 8 节）。

## 2. 文件格式

UTF-8 Markdown，带 frontmatter：

```yaml
---
id: kb_01HZQ3M7V4W2X8         # 稳定标识，路径变化时不变
title: 张老师
tags: [课程, GSE]              # 可选，空则省略
summary: GSE 课程任课老师       # 仅主题页必填：主题目录里的一句话说明
created_at: 2026-09-14T10:22:31+08:00
updated_at: 2026-09-14T10:22:31+08:00
---
```

核心字段为 `id`、`title`、`created_at`、`updated_at`；`tags` 可选；`summary` 只用于主题页。资料不记录出处字段。资料的类别由目录与 `tags` 体现，不强制 `kind` 枚举。正文用二级标题分节，标题文本即锚点。原始内容与模型总结在同一文件中必须分节标明，不混写。

用户新建、没有 frontmatter 或缺少 `id` 的 Markdown 文件在纳入版本时（Phase 3）由程序补上 `id`、`title`（取一级标题或文件名）、`created_at`、`updated_at`，不改动正文。

## 3. 索引与增量更新

索引是数据目录内的派生文件 `<data_dir>/kb-index.sqlite3`，不进 Git，任何时刻都可以由 Markdown 文件与 Git 版本重建。每条记录对应一个文件分节：

分节按 Markdown 二级标题切分：每个 `##` 到下一个 `##` 为一段，二级标题之前的正文（前言）与无二级标题的整篇各自成段；代码围栏里的 `##` 不算标题；空分节不产生记录。行号是文件的 1-based 真实行号，包含 frontmatter，因此可以直接按行号切出原文。

| 字段 | 含义 |
| --- | --- |
| `id`、`path`、`title`、`tags` | 资料标识与分类 |
| `heading` | 分节标题路径，例如 `张老师 / 沟通偏好`；前言与整篇只有资料标题 |
| `lines` | 该分节在文件中的起止行（含 frontmatter 的真实行号） |
| `content_hash` | 该分节内容哈希 |
| `commit` | 写入该版本时的 Git 提交 |

检索用 SQLite FTS5 的 `trigram` 分词器，中文连续文本、英文与编号都能命中；1–2 个字的词无法被 trigram 命中，改在同一张索引表上做包含匹配，不引入第二套检索实现。排序先看命中字段（标题、分节标题、标签、正文），再看 FTS5 相关度，分数相同时按路径与行号保持稳定顺序。排序在全部候选上完成后才截断到 `max_results`，正文只在截断后为最终命中取回，保证最相关的结果不会因命中数量大而被丢弃。

更新规则：

- 应用写入完成 Git 提交后，在同一把资料库锁内只替换受影响文件的索引条目；索引此前不完整时改为整体重建。
- 索引记录当前 `kb/` 目录的 Git tree 标识。应用写入后若索引更新中断，下一次检索先重建；重建失败时返回 `kb_index_unavailable`，不查询可能过期的旧索引。
- 文件写入成功但索引失败时，写入仍以文件与 Git 提交为准，结果标记 `index_status: "stale"`，程序提示“资料已保存，但当前不可检索”。
- 索引缺失、损坏、结构版本变化或 tree 不一致时整体重建，重建不改变资料内容。

用户直接改动的跟随（Phase 3）：

- 检索、列举与写入前检查 `kb/` 下未提交的改动（修改、新增、删除、移动）；有改动时先把它们作为一次“用户编辑”提交纳入版本，再更新索引。提交在资料库锁内完成，与应用写入串行。
- 移动与重命名按 frontmatter 的 `id` 识别为同一份资料：旧路径消失、新路径出现且 `id` 相同时记为移动，不当成删除加新建。
- 同一文件既有用户未提交的改动、又被 Agent 按旧版本修改时，先纳入用户改动；Agent 的 `expected_version` 因此不再匹配，返回 `version_conflict`，不覆盖用户内容。

## 4. Agent 可见工具

| 工具 | 副作用 | 阶段 | 说明 |
| --- | --- | --- | --- |
| `kb_search` | 只读 | Phase 2 | 按关键词检索资料分节，返回摘要与引用 |
| `kb_list` | 只读 | Phase 1 | 按目录列出资料及当前位置 |
| `kb_read` | 只读 | Phase 1 / Phase 2 | 读取原文，可指定历史版本或按引用读取片段 |
| `kb_save` | 本地写 | Phase 1 | 新建资料文件 |
| `kb_update` | 本地写 | Phase 1 | 修改已有资料文件 |
| `kb_history` | 只读 | Phase 1 / Phase 3 | 列出一份资料的历史版本，Phase 3 起含已删除资料 |
| `kb_delete` | 本地写，仅用户对话轮 | Phase 3 | 删除资料文件，历史保留 |
| `kb_move` | 本地写，仅用户对话轮 | Phase 3 | 移动或重命名资料 |
| `kb_restore` | 本地写，仅用户对话轮 | Phase 3 | 从历史版本恢复资料 |
| `kb_archive` | 本地写 | Phase 4 | 归档一次任务的关键信息与逐项结果 |

轮次可见范围：

- 用户对话轮：全部工具。
- 执行结果回传轮：只读工具、`kb_save`、`kb_update`、`kb_archive`。
- 新邮件等触发轮（Phase 3 起）：只读工具、`kb_save`、`kb_update`。删除、移动、恢复与归档不开放，其他域的本地写（如 Skill 草稿）也不因此开放。开放范围由工具注册时的声明决定，通用层不认工具名。
- 后台主题整理会话（Phase 5）：只读工具，以及限定写入 `kb/topics/` 的 `kb_save`/`kb_update`；路径限制由程序校验。

删除、移动与恢复要求先在对话中取得用户同意（`kb-spec.md` 第 5.2 节）。这一要求写在工具说明里，由模型遵守；程序保证的是这三个工具只在用户亲自发起的对话轮可见。

### `kb_search`

输入：

| 字段 | 类型 | 含义 |
| --- | --- | --- |
| `query` | string | 检索词；含空格时按多个词处理，全部命中才算命中 |
| `tag` | string | 可选，限定标签 |
| `max_results` | integer | 最多返回数，默认 10，范围 1–20 |

输出 `results[]`，每项含 `id`、`path`、`title`、`heading`、`lines`、`snippet`、`score` 与 `ref`（结构见第 5 节），不返回整篇正文。无命中时 `results` 为空。

### `kb_list`

输入可选的资料库内 `directory`。输出该目录及子目录中每份资料的 `id`、`path`、`title`、`tags` 和当前 `version`，不返回正文。它只列目录与元数据，按内容找资料用 `kb_search`。

### `kb_read`

输入 `path` 或 `id`（二者之一）、可选 `version`（某次提交的 Git commit，读取历史版本），或 `ref`（按引用读取）。`ref` 必须原样取自 `kb_search`、`kb_read` 或 `kb_save` 的返回：`path`、`commit`、`lines` 必填（模型可见 schema 已声明）。给 `ref` 时按 `commit + path + lines` 读取该版本的原文片段，并校验资料身份、路径与行号：commit 不存在或路径在该版本下不存在返回 `not_found`；`id` 不匹配、行号越界，或行号区间与资料的某个分节/整篇正文区间不对应返回 `invalid_kb`，不返回任何内容——拼接出来的区间不算引用。

输出 `id`、`title`、`tags`、`heading`、`lines`、`commit`、`body`（Markdown 原文，不总结）和同一个规范化 `ref`。按 `path`/`id` 读取整篇时 `heading` 为空、`lines` 覆盖正文区间（不含 frontmatter），与返回正文严格对应。模型按哪种方式读取由它自行判断，读取方式不影响回答。规范化包括：commit 展开为完整 sha，`id` 取自该版本的 frontmatter，行号区间与分节完全一致时 `heading` 重新取自该分节，否则（整篇区间）`heading` 为空。

### `kb_save`

输入 `title`、`body`、可选 `path`、可选 `tags[]`、可选 `summary`（写入 `topics/` 时必填）。省略 `path` 时落到 `kb/inbox/`，文件名由 `title` 的 slug 加 `id` 后六位生成。输出 `id`、`path`、`title`、`version`、`index_status`、`ref`。目标文件已存在时拒绝（提示改用 `kb_update`）。成功结果由程序在时间线展示实际 `kb/` 路径，不依赖模型复述；`index_status` 为 `stale` 时提示改为“资料已保存，但当前不可检索”。

### `kb_update`

输入 `expected_version`（该资料当前的 Git commit）、`id` 或 `path`、以及要写入的 `title`、`tags[]`、`body`、`summary`（只传需要改的字段，未传的保持原样）。输出 `id`、`path`、`title`、`previous_version`、新 `version`、`index_status`、`ref`。`expected_version` 与当前 commit 不匹配时拒绝并返回 `VersionConflictError`，不写入；`id` 跨修改保持不变，历史版本不改写。成功结果由程序在时间线展示资料位置与修改前后版本。

### `kb_history`

输入 `path` 或 `id`（二者之一）。输出该资料从新到旧的版本列表，每项含 Git commit 形式的 `version`、修改时间、变更说明与该版本所在路径；已删除资料的最后一项标明删除。随后可把其中一个 `version` 交给 `kb_read` 读取当时原文，或交给 `kb_restore` 恢复。

### `kb_delete`（Phase 3）

输入 `expected_version`、`id` 或 `path`。从 `kb/` 删除该文件并提交，历史保留。输出 `id`、`path`、`previous_version`、`version`（删除提交）。成功结果由程序展示“已删除资料：标题。历史版本仍保留”。

### `kb_move`（Phase 3）

输入 `expected_version`、`id` 或 `path`、`new_path`。目标已存在时拒绝。输出 `id`、`previous_path`、`path`、`previous_version`、`version`。`id` 不变。

### `kb_restore`（Phase 3）

输入 `id` 或 `path`、`version`（要恢复到的历史 commit）。资料当前存在时，把内容恢复为该版本；已删除时在该版本所在路径重建，路径已被占用则拒绝并返回冲突。恢复产生新提交，不改写历史。输出 `id`、`path`、`restored_from`、`version`。

### `kb_archive`（Phase 4）

输入 `task_id`、`items[]`。每项含 `kind: "original" | "summary"` 与 `text`：`original` 为邮件、日程等原始内容的摘录，`summary` 为模型总结或逐项执行结果。输出归档文件的 `id`、`path`、`ref`。归档文件写入 `archive/`，原始内容与总结分节保存；执行结果以实际执行记录为准。

知识库工具都不产生外部副作用，因此不经过 Confirmation；但每次写入都留下可读文件与 Git 提交，用户可直接查看和修改。

## 5. 引用格式

引用（`ref`）是工具之间定位原文的凭据：检索、读取与保存结果都带 `ref`，`kb_read` 可按它读回该版本的原文片段。引用只在工具内部流转，不作为来源展示给用户，也不作为出处记录保存。

```json
{
  "id": "kb_01HZQ3M7V4W2X8",
  "path": "kb/people/zhang-laoshi.md",
  "heading": "张老师 / 沟通偏好",
  "lines": [18, 26],
  "commit": "9f2c1ab7d3e4f5061728394a5b6c7d8e9f0a1b2c"
}
```

- `commit` 是唯一版本标识，取完整 sha；引用不带时间戳字段。
- `lines` 是含 frontmatter 的真实行号区间，闭区间；整篇引用覆盖正文区间（不含 frontmatter）。
- 资料更新或移动后，旧 `ref` 仍可解析：按 `commit` 与 `path` 读取当时内容。
- 引用不得指向不存在的内容；合法区间只有真实分节与整篇正文区间，拼造的行号会被拒绝。

## 6. 回答与来源

- 涉及资料中的具体事实时，模型先用 `kb_search` 检索，再用 `kb_read` 读取原文确认后作答；只有检索摘要不作为回答依据。
- 程序不记录也不展示回答来源，时间线上的 Agent 回答不带来源卡；模型按路径、`id` 还是 `ref` 读取都不改变回答的呈现。
- 查不到依据时如实说明没有找到，不凭印象作答；资料互相冲突时读取相关几份，说明冲突，不自行挑一个当事实。
- 资料与记忆都不记录出处。

## 7. 主题页与主题目录（Phase 5）

- 主题页是 `kb/topics/` 下的普通资料，frontmatter 必须有 `summary`。它和其他资料一样被索引、检索、修改和版本管理。
- 主题目录由程序从 `kb/topics/` 的 frontmatter 生成，每个主题一行 `标题：summary`，不进 Git，随主题页写入重建。
- 主题目录作为常驻上下文每轮注入，容量上限 30 行、1500 个 Unicode 字符；超出时按 `updated_at` 保留最近更新的主题，并注明还有多少个主题未列出。
- 后台主题整理是一次性模型会话，调度复用后台记忆回顾：每个任务累计若干个已完成的用户消息轮后执行一次，另有手动入口。输入是窗口内的对话文本、窗口期间新增或修改的资料清单与当前主题目录；只能写 `kb/topics/`。整理不写任务时间线、不发 SSE、不通知用户。
- 整理修改主题页时基于当前版本（`expected_version`），用户在整理期间改过的主题页返回 `version_conflict`，本次跳过，下次基于用户的版本重新整理。

## 8. 外部笔记导入（Phase 5）

导入由用户发起，按“来源适配器 → 转换 → 写入”执行：

- 适配器读取来源并逐篇给出：外部笔记标识、标题、Markdown 正文、标签、创建与修改时间、是否在废纸篓、是否加密或不可读。首个适配器为熊掌记，读取方式等待定项见 `kb-spec.md` 第 9.2 节。
- 转换：保留标题、正文结构与标签；图片与附件不存入资料库。
- 导入状态文件 `<data_dir>/kb-imports.json`（不进 Git）按来源记录外部笔记标识 → 资料 `id`，以及上次导入写入的资料版本。它只用于去重与冲突判断，不写入资料 frontmatter，也不展示为出处；文件丢失时退化为按标题匹配并报告疑似重复。
- 写入：没有对应记录的笔记用 `kb_save` 语义新建；有记录且资料当前版本等于上次导入写入的版本时，用 `kb_update` 语义更新；资料在 Pebble 中被改过（当前版本不等于上次导入版本）时不写入，计为冲突。资料已被删除时计为跳过，不自动重建。
- 废纸篓中的笔记不导入；加密或不可读的笔记计为跳过。
- 一次导入的全部写入合并为一个 Git 提交，提交后统一更新索引。
- 输出 `created`、`updated`、`skipped[]`、`conflicts[]`、`failed[]`，后三者每项含标题与原因。

## 9. 错误

复用 `server/errors.py` 的词汇与字段形状：`NotFoundError`、`VersionConflictError`（附 `current_version`，取 Git commit sha）。资料校验失败返回 `KbValidationError`（`invalid_kb`），附 `errors[]`，每项含 `field` 与 `message`，不保存数据，按引用读取失败时也不返回内容。资料文件或本地版本仓库不可用时返回 `KbStoreUnavailableError`（`kb_store_unavailable`）；索引缺失、损坏或无法重建时返回 `KbIndexUnavailableError`（`kb_index_unavailable`），此时不返回旧索引结果。后台整理写入 `kb/topics/` 之外的路径返回 `invalid_kb`。
