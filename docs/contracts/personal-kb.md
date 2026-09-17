# 个人知识库契约

状态：**Phase 4 已实现**（`kb_list`/`kb_save`/`kb_read`/`kb_update`/`kb_history`/`kb_search`/`kb_delete`/`kb_move`/`kb_restore`/`kb_archive`，含 SQLite FTS5 分节索引、用户改动的自动跟随、每轮常驻的资料目录（第 7 节）与资料管理 HTTP 接口（第 9 节））。Phase 5 是约定，尚未实现。产品行为以 `docs/kb-spec.md` 为准；本文定义个人知识库（Personal KB）的存储形态、索引、工具字段与引用语义。实现与本文冲突时先改本文，不静默偏离。

知识库保存“具体资料”，属于按需检索；Memory 保存“关于用户的精简背景、偏好与长期目标”，属于常驻上下文，见 `skills.md` 与 `memory-spec.md`。

## 0. 交付阶段

| 阶段 | 内容 | 状态 |
| --- | --- | --- |
| Phase 1 | `kb/` 文件存储 + 本地 Git 版本；资料列举、保存、读取、更新和历史读取；稳定 `id`、版本历史；与长期记忆的边界 | 已实现 |
| Phase 2 | 按内容检索：`kb_search` + SQLite FTS5 分节索引、增量替换与重建；按路径、`id` 或引用读取原文；回答不向用户展示来源 | 已实现 |
| Phase 3 | 自动跟随用户在文件系统里的改动（自动纳入版本、识别移动与重命名、为无标识文件补标识）；`kb_delete`/`kb_move`/`kb_restore`；触发轮开放资料新建与修改 | 已实现 |
| Phase 4 | 资料管理界面（浏览、搜索、编辑）；`kb_archive` 任务归档；资料 `summary` 与每轮常驻的资料目录 | 已实现 |
| Phase 5 | 主题页与后台主题整理；外部笔记导入（首个来源熊掌记） | 未实现 |

索引只收录内容与其 Git 版本一致的文件；Phase 3 起用户在文件系统里的改动在每次资料库操作前自动纳入版本（第 3 节），因此改完即可在下一次检索中命中。程序不监听文件变化，改动在下一次任何资料库操作时才纳入。

## 1. 存储形态

资料是实例数据目录内的 Markdown 文件加目录结构，用户可以直接阅读、修改和重组。`kb/` 与 `skills/` 同属实例数据目录内的独立本地 Git 仓库，不是代码仓库；写入即提交，不推送远端。`memory/` 不做版本管理。

```text
<data_dir>/kb/
├── inbox/            # 未分类的资料
├── people/           # 人物，一人一文件
├── projects/         # 事项与项目
├── reference/        # 稳定参考资料
├── topics/           # 主题页（Phase 5），后台整理只写这里
└── archive/          # 任务归档：关键信息与逐项执行结果
```

除 `topics/` 与 `archive/` 外，上图只是示例，不是固定分类法。保存时由 Agent 按内容自选相对路径、可新建文件夹；省略 `path` 时落到 `kb/inbox/`。索引、资料目录与导入状态都是派生或运行数据，放在数据目录内且不进 Git，可随时从文件重建（导入状态除外，见第 8 节）。

## 2. 文件格式

UTF-8 Markdown，带 frontmatter：

```yaml
---
id: kb_01HZQ3M7V4W2X8         # 稳定标识，路径变化时不变
title: 张老师
tags: [课程, GSE]              # 可选，空则省略
summary: GSE 课程任课老师       # 一句话说明，出现在资料目录里；主题页必填
created_at: 2026-09-14T10:22:31+08:00
updated_at: 2026-09-14T10:22:31+08:00
---
```

核心字段为 `id`、`title`、`created_at`、`updated_at`；`summary`（一句话说明）与 `tags` 可选，`summary` 强烈建议填写、主题页必填。资料不记录出处字段。资料的类别由目录与 `tags` 体现，不强制 `kind` 枚举。正文用二级标题分节，标题文本即锚点。原始内容与模型总结在同一文件中必须分节标明，不混写。

用户新建、没有 frontmatter 或缺少 `id` 的 Markdown 文件在纳入版本时由程序补上 `id`、`title`（取第一个一级标题，没有时取文件名）、`created_at`、`updated_at`，正文原样保留。复制出来、`id` 与另一份仍存在的资料相同的新文件换发新 `id`。frontmatter 无法解析的文件照样纳入版本，但不补标识、不进入索引。

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

- 每次资料库操作（检索、列举、读取、历史与各类写入）开始前，在资料库锁内检查 `kb/` 下未提交的改动（修改、新增、删除、移动）；有改动时先纳入版本，再照常执行，索引按 tree 标识在下一次检索或写入时对齐。
- 移动与重命名按 frontmatter 的 `id` 识别为同一份资料：旧路径消失、新路径出现且 `id` 相同时记为移动。移动以原内容单独提交一次（`[Kb] User move …`），其余改动随后作为一次 `[Kb] User edit …` 提交；这样即使移动时正文大改，历史也能沿路径跟随。
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

输入可选的资料库内 `directory` 与可选 `deleted`。默认输出该目录及子目录中每份资料的 `id`、`path`、`title`、`summary`、`tags` 和当前 `version`，不返回正文。它只列目录与元数据，按内容找资料用 `kb_search`。

`deleted: true` 时改为列出已删除且尚未恢复的资料（按删除时间从新到旧，每份资料只列最近一次删除），每项含 `id`、删除前的 `path`、`title`、`tags`、`deleted_at` 与删除前最后的 `version`；检索与普通列举看不到已删除资料，找回时先用它定位，再交给 `kb_restore`。

### `kb_read`

输入 `path` 或 `id`（二者之一）、可选 `version`（某次提交的 Git commit，读取历史版本），或 `ref`（按引用读取）。`ref` 必须原样取自 `kb_search`、`kb_read` 或 `kb_save` 的返回：`path`、`commit`、`lines` 必填（模型可见 schema 已声明）。给 `ref` 时按 `commit + path + lines` 读取该版本的原文片段，并校验资料身份、路径与行号：commit 不存在或路径在该版本下不存在返回 `not_found`；`id` 不匹配、行号越界，或行号区间与资料的某个分节/整篇正文区间不对应返回 `invalid_kb`，不返回任何内容——拼接出来的区间不算引用。

输出 `id`、`path`、`title`、`summary`、`tags`、`created_at`、`updated_at`、`heading`、`lines`、`commit`、`body`（Markdown 原文，不总结）和同一个规范化 `ref`（按 `ref` 读取时不含 `path`、`summary`、`created_at`、`updated_at`，路径见 `ref`）。按 `path`/`id` 读取整篇时 `heading` 为空、`lines` 覆盖正文区间（不含 frontmatter），与返回正文严格对应。模型按哪种方式读取由它自行判断，读取方式不影响回答。规范化包括：commit 展开为完整 sha，`id` 取自该版本的 frontmatter，行号区间与分节完全一致时 `heading` 重新取自该分节，否则（整篇区间）`heading` 为空。

### `kb_save`

输入 `title`、`body`、可选 `summary`（工具说明要求填写；写入 `topics/` 时必填）、可选 `path`、可选 `tags[]`。省略 `path` 时落到 `kb/inbox/`，文件名由 `title` 的 slug 加 `id` 后六位生成。输出 `id`、`path`、`title`、`version`、`index_status`、`ref`。目标文件已存在时拒绝（提示改用 `kb_update`）。成功结果由程序在时间线展示实际 `kb/` 路径，不依赖模型复述；`index_status` 为 `stale` 时提示改为“资料已保存，但当前不可检索”。

### `kb_update`

输入 `expected_version`（该资料当前的 Git commit）、`id` 或 `path`、以及要写入的 `title`、`tags[]`、`body`、`summary`（只传需要改的字段，未传的保持原样；`summary` 传空串表示删除说明）。输出 `id`、`path`、`title`、`previous_version`、新 `version`、`index_status`、`ref`。`expected_version` 与当前 commit 不匹配时拒绝并返回 `VersionConflictError`，不写入；`id` 跨修改保持不变，历史版本不改写。成功结果由程序在时间线展示资料位置与修改前后版本。

### `kb_history`

输入 `path` 或 `id`（二者之一），已删除的资料也能定位。输出 `id`、`path`、`title`、`deleted`（资料当前是否已删除）与从新到旧的 `versions[]`，每项含 Git commit 形式的 `version`、`changed_at`、`summary`、该版本所在的 `path` 与 `deleted`（这一项是否为删除记录）。历史跟随移动；同一路径先后放过不同资料时，按 `id` 定位只列出这份资料自己的版本。随后可把其中一个 `version` 交给 `kb_read` 读取当时原文，或交给 `kb_restore` 恢复。

### `kb_delete`（Phase 3）

输入 `expected_version`、`id` 或 `path`。从 `kb/` 删除该文件并提交，历史保留。输出 `id`、`path`、`previous_version`、`version`（删除提交）。成功结果由程序展示“已删除资料：标题。历史版本仍保留”。

### `kb_move`（Phase 3）

输入 `expected_version`、`id` 或 `path`、`new_path`。目标已存在时拒绝。输出 `id`、`previous_path`、`path`、`previous_version`、`version`。`id` 不变。

### `kb_restore`（Phase 3）

输入 `id` 或 `path`、`version`（要恢复到的历史 commit）。资料当前存在时，把内容恢复为该版本；已删除时在该版本所在路径重建，路径已被占用则拒绝并返回冲突。恢复产生新提交，不改写历史。输出 `id`、`path`、`restored_from`、`version`。

### `kb_archive`（Phase 4）

输入 `title` 与非空 `items[]`。每项含 `kind: "original" | "summary"`、可选 `heading` 与非空 `text`：`original` 为邮件、日程等原始内容的摘录，`summary` 为模型总结或逐项执行结果。归档文件写入 `kb/archive/<日期>-<标题 slug>-<随机后缀>.md`，正文先列全部原始内容再列总结，每项一节，节标题为 `原始内容：<heading>` 或 `总结与执行结果：<heading>`（无 `heading` 时省去冒号部分）。输出与 `kb_save` 相同，程序在时间线展示“已归档任务：标题。位置：…”。字段不合法返回 `invalid_kb`，不写入。

归档时机由模型判断，写在工具说明里：任务中的外部操作有了实际结果之后归档（典型是执行结果回传轮）；纯问答、闲聊、只起草未确认的任务不归档；同一任务再有新结果时用 `kb_update` 修改已有归档。执行结果以工具返回与系统回传为准。归档不记录任务标识或出处。

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

## 7. 资料目录与主题页

资料目录是每轮常驻上下文里的资料指针（`server/tools/personal_kb/catalog.py`），与长期记忆一同注入；资料正文不常驻。

- 生成：每轮组装上下文时由 `KbStore.catalog()` 现算——先纳入用户改动，再读 `kb/` 下全部 Markdown 的 `title`（缺省取文件名）、`summary` 与 `updated_at`。不存成文件、不进 Git。
- 位置：作为 `## 资料目录` 材料块，排在 `## 关于你`、`## 事实与约定` 之后、本轮触发材料之前；资料库为空时不生成材料块。生成失败只记日志，本轮不带目录，不中断调用。
- 格式：首行 `资料库共 N 份资料；需要细节时用 kb_search 检索，或用 kb_read 读取原文。`；随后按资料所在目录分组，分组行 `- 目录/（N 份）`，资料行 `  - 标题：summary`（无说明时只有标题，说明超过 60 个字符截断加省略号）；根目录下的资料归入 `（根目录）`。
- 排序与上限：上限 1500 个 Unicode 字符。目录按其中最近一次更新排序，目录内资料最近更新在前。先保证能放下的目录都有分组行，放不下的目录合并为末行 `- 另有 K 个目录共 M 份资料未列出`；再按全局更新时间逐条填入资料，目录里没列出的写 `  - 另有 N 份`。
- 主题页（Phase 5）是 `kb/topics/` 下的普通资料，frontmatter 必须有 `summary`，和其他资料一样出现在资料目录里，不单独维护主题目录。
- 后台主题整理（Phase 5）是一次性模型会话，调度复用后台记忆回顾：每个任务累计若干个已完成的用户消息轮后执行一次，另有手动入口。输入是窗口内的对话文本、窗口期间新增或修改的资料清单与当前资料目录；只能写 `kb/topics/`。整理不写任务时间线、不发 SSE、不通知用户。
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

## 9. 资料管理 HTTP 接口（Phase 4）

界面操作由用户本人发起，等同于直接改文件（认证随交付阶段 6 接入）：不经过 Agent，不需要对话中的同意，也不产生时间线提示。接口复用 `KbStore` 的同一套校验、版本与索引规则；错误体与第 10 节相同，`invalid_kb` 为 422，`not_found` 为 404，`version_conflict` 为 409（附 `current_version`），资料库或索引不可用为 503，资料库未装配时返回 `unavailable`（503）。

| 方法与路径 | 输入 | 输出 |
| --- | --- | --- |
| `GET /api/kb/documents` | 可选 `directory` | 与 `kb_list` 相同 |
| `GET /api/kb/document` | `path` | `id`、`path`、`title`、`summary`（无说明为空串）、`tags`（无标签为空列表）、`created_at`、`updated_at`、`version`、`body` |
| `GET /api/kb/search` | `q`、可选 `tag` | 与 `kb_search` 相同，最多 20 条 |
| `POST /api/kb/documents` | `title`、`body`、可选 `summary`、`path`、`tags[]` | 201，与 `kb_save` 相同 |
| `POST /api/kb/document/update` | `path`、`expected_version`，以及要写入的 `title`、`body`、`summary`、`tags[]` | 与 `kb_update` 相同 |
| `POST /api/kb/document/move` | `path`、`expected_version`、`new_path` | 与 `kb_move` 相同 |
| `POST /api/kb/document/delete` | `path`、`expected_version` | 与 `kb_delete` 相同 |

界面约定：

- 标题、一句话说明与标签是表单字段，`id` 与时间只显示不修改；正文用所见即所得的 Markdown 编辑器（CommonMark + GFM）。
- 编辑器会把原文重新排版（例如表格对齐）。只有正文实际被编辑时才提交编辑器输出的 Markdown；只改标题或标签时提交原文，打开不编辑不产生写入。
- 保存、移动与删除都带读取时的 `version`；冲突时提示资料已被修改，用户选择重新载入，不覆盖。
- 原文中的 HTML 按纯文本显示，不在页面中执行。
- 不提供历史版本、差异与恢复入口，不展示 Agent 的读取记录。

## 10. 错误

复用 `server/errors.py` 的词汇与字段形状：`NotFoundError`、`VersionConflictError`（附 `current_version`，取 Git commit sha）。资料校验失败返回 `KbValidationError`（`invalid_kb`），附 `errors[]`，每项含 `field` 与 `message`，不保存数据，按引用读取失败时也不返回内容。资料文件或本地版本仓库不可用时返回 `KbStoreUnavailableError`（`kb_store_unavailable`）；索引缺失、损坏或无法重建时返回 `KbIndexUnavailableError`（`kb_index_unavailable`），此时不返回旧索引结果。后台整理写入 `kb/topics/` 之外的路径返回 `invalid_kb`。
