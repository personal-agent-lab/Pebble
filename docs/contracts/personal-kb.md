# 个人知识库契约

状态：**Phase 2 已实现**（`kb_list`/`kb_save`/`kb_read`/`kb_update`/`kb_history`/`kb_search`，含 SQLite FTS5 分节索引与来源记录）。`kb_archive` 留待 Phase 4；文件系统改动的自动跟随留待 Phase 3。本文定义个人知识库（Personal KB）的存储形态、索引、工具字段与引用语义。实现与本文冲突时先改本文，不静默偏离。

知识库保存“资料及原文证据”，与 Memory 保存“用户偏好与规则”分开，见 `skills.md`。

## 0. 交付阶段

知识库分四个阶段交付，当前实现到 Phase 2：

| 阶段 | 内容 | 状态 |
| --- | --- | --- |
| Phase 1 | `kb/` 文件存储 + 本地 Git 版本；资料列举、保存、读取、更新和历史读取；稳定 `id`、来源、版本历史；与长期记忆的边界 | 已实现 |
| Phase 2 | 按内容检索：`kb_search` + SQLite FTS5 分节索引、增量替换与重建；按引用读取原文；程序记录回答来源并在回答下展示 | 已实现 |
| Phase 3 | 自动跟随用户在文件系统里的改动：移动/重命名/删除的索引同步与“同一份资料”识别、并发编辑检测 | 未实现 |
| Phase 4 | 资料管理界面、版本差异视图、读写记录面板；`kb_archive` 任务归档；逐条核对后迁移旧记忆 | 未实现 |

Phase 2 的索引只收录内容与其 Git 版本一致的文件：用户直接在编辑器里改过的内容在纳入版本之前不会被索引，可检索的仍是该文件已提交的那一版。检测并纳入这类改动属于 Phase 3，因此本阶段不承诺“改完立刻可检索”，也不承诺移动或删除后自动收敛。

## 1. 存储形态

资料是实例数据目录内的 Markdown 文件加目录结构，用户可以直接阅读、修改和重组。`kb/` 与 `memory/`、`skills/` 同属实例数据目录内的独立本地 Git 仓库，不是代码仓库；写入即提交，不推送远端。

```text
<data_dir>/kb/
├── inbox/            # 未分类的原始材料
├── people/           # 人物，一人一文件
├── projects/         # 事项与项目
├── reference/        # 稳定参考资料
└── archive/          # 任务归档：来源与逐项执行结果
```

上图是示例，不是固定分类法。保存时由 Agent 按内容自选相对路径、可新建文件夹；省略 `path` 时落到 `kb/inbox/`。Agent 可随资料增长重组目录，重组必须保持已有引用可解析（见第 5 节）。索引是派生数据（Phase 2），放在数据目录内且不进 Git，可随时从文件重建。

## 2. 文件格式

UTF-8 Markdown，带 frontmatter：

```yaml
---
id: kb_01HZQ3M7V4W2X8         # 稳定标识，路径变化时不变
title: 张老师
tags: [课程, GSE]              # 可选，空则省略
source:                        # 可选，原始证据来源
  kind: mail                   # mail | calendar | kb | user | task
  ref: msg_19a2b3
created_at: 2026-09-14T10:22:31+08:00
updated_at: 2026-09-14T10:22:31+08:00
---
```

核心字段为 `id`、`title`、`created_at`、`updated_at`；`tags` 与 `source` 可选。资料的类别由 Agent 选择的目录与 `tags` 体现，不强制 `kind` 枚举。正文用二级标题分节，标题文本即锚点。原始证据与模型总结在同一文件中必须分节标明，不混写。

## 3. 索引与增量更新

索引是数据目录内的派生文件 `<data_dir>/kb-index.sqlite3`，不进 Git，任何时刻都可以由 Markdown 文件与 Git 版本重建。每条记录对应一个文件分节：

分节按 Markdown 二级标题切分：每个 `##` 到下一个 `##` 为一段，二级标题之前的正文（前言）与无二级标题的整篇各自成段；代码围栏里的 `##` 不算标题；空分节不产生记录。行号是文件的 1-based 真实行号，包含 frontmatter，因此可以直接按行号切出原文。

| 字段 | 含义 |
| --- | --- |
| `id`、`path`、`title`、`source_kind`、`tags` | 资料标识与分类 |
| `heading` | 分节标题路径，例如 `张老师 / 沟通偏好`；前言与整篇只有资料标题 |
| `lines` | 该分节在文件中的起止行（含 frontmatter 的真实行号） |
| `content_hash` | 该分节内容哈希 |
| `commit` | 写入该版本时的 Git 提交 |

检索用 SQLite FTS5 的 `trigram` 分词器，中文连续文本、英文与编号都能命中；1–2 个字的词无法被 trigram 命中，改在同一张索引表上做包含匹配，不引入第二套检索实现。排序先看命中字段（标题、分节标题、标签、正文），再看 FTS5 相关度，分数相同时按路径与行号保持稳定顺序。

更新规则：

- 应用写入（`kb_save`、`kb_update`）完成 Git 提交后，在同一把资料库锁内只替换受影响文件的索引条目；索引此前不完整时改为整体重建。
- 索引记录当前 `kb/` 目录的 Git tree 标识。应用写入后若索引更新中断，下一次检索先重建；重建失败时返回 `kb_index_unavailable`，不查询可能过期的旧索引。
- 文件写入成功但索引失败时，写入仍以文件与 Git 提交为准，结果标记 `index_status: "stale"`，程序提示“资料已保存，但当前不可检索”。
- 只收录内容与其 Git 版本一致的文件；索引缺失、损坏、结构版本变化或 tree 不一致时整体重建，重建不改变资料内容。
- 用户直接改文件、移动或删除文件后自动收敛（Phase 3）尚未实现：未纳入版本的内容不进入索引，删除或移动后的索引同步留待 Phase 3。

## 4. Agent 可见工具

| 工具 | 副作用 | 阶段 | 说明 |
| --- | --- | --- | --- |
| `kb_search` | 只读 | Phase 2 | 按关键词检索资料分节，返回摘要与引用 |
| `kb_list` | 只读 | Phase 1 | 按目录列出资料及当前位置 |
| `kb_read` | 只读 | Phase 1 / Phase 2 | 读取原文，可指定历史版本或按引用读取片段 |
| `kb_save` | 本地写 | Phase 1 | 新建资料文件 |
| `kb_update` | 本地写 | Phase 1 | 修改已有资料文件 |
| `kb_history` | 只读 | Phase 1 | 列出一份资料的历史版本 |
| `kb_archive` | 本地写 | Phase 4 | 归档一次任务的来源与逐项结果 |

### `kb_search`

输入：

| 字段 | 类型 | 含义 |
| --- | --- | --- |
| `query` | string | 检索词；含空格时按多个词处理，全部命中才算命中 |
| `source_kind` | string | 可选，限定资料的来源类型（`mail`/`calendar`/`kb`/`user`/`task`） |
| `tag` | string | 可选，限定标签 |
| `max_results` | integer | 最多返回数，默认 10，范围 1–20 |

输出 `results[]`，每项含 `id`、`path`、`title`、`heading`、`lines`、`snippet`、`score` 与 `ref`（结构见第 5 节），不返回整篇正文。无命中时 `results` 为空。

### `kb_list`

输入可选的资料库内 `directory`。输出该目录及子目录中每份资料的 `id`、`path`、`title`、`tags` 和当前 `version`，不返回正文。它只列目录与元数据，按内容找资料用 `kb_search`。

### `kb_read`

输入 `path` 或 `id`（二者之一）、可选 `version`（某次提交的 Git commit，读取历史版本），或 `ref`（按引用读取）。给 `ref` 时按 `commit + path + lines` 读取该版本的原文片段，并校验资料身份、路径与行号：commit 不存在或路径在该版本下不存在返回 `not_found`；`id` 不匹配或行号越界返回 `invalid_kb`，不返回任何内容。

输出 `id`、`title`、`tags`、`source`、`heading`、`lines`、`commit`、`body`（Markdown 原文，不总结）和同一个规范化 `ref`。按 `path`/`id` 读取整篇时 `heading` 为空、`lines` 覆盖整个文件。规范化包括：commit 展开为完整 sha，`id` 取自该版本的 frontmatter，行号区间与分节完全一致时 `heading` 重新取自该分节。

### `kb_save`

输入 `title`、`body`、可选 `path`、可选 `tags[]`、可选 `source`。省略 `path` 时落到 `kb/inbox/`，文件名由 `title` 的 slug 加 `id` 后六位生成。没有显式来源时，程序以当前任务作为来源。输出 `id`、`path`、`title`、`version`、`index_status`、`ref`。目标文件已存在时拒绝（提示改用 `kb_update`）。成功结果由程序在时间线展示实际 `kb/` 路径，不依赖模型复述；`index_status` 为 `stale` 时提示改为“资料已保存，但当前不可检索”。

### `kb_update`

输入 `expected_version`（该资料当前的 Git commit）、`id` 或 `path`、以及要写入的 `title`、`tags[]`、`body`、`source`（只传需要改的字段，未传的保持原样）。输出 `id`、`path`、`title`、`previous_version`、新 `version`、`index_status`、`ref`。`expected_version` 与当前 commit 不匹配时拒绝并返回 `VersionConflictError`，不写入；`id` 跨修改保持不变，历史版本不改写。成功结果由程序在时间线展示资料位置与修改前后版本。

### `kb_history`

输入 `path` 或 `id`（二者之一）。输出该资料从新到旧的版本列表，每项含 Git commit 形式的 `version`、修改时间和变更说明；随后可把其中一个 `version` 交给 `kb_read` 读取当时原文。

### `kb_archive`（Phase 4）

输入 `task_id`、`items[]`。每项含 `kind: "evidence" | "summary"`，以及 `ref`（证据来源，指向邮件、日程或已有资料）或 `text`（模型总结文本）。输出归档文件的 `id`、`path`、`ref`。归档文件写入 `archive/`，证据与总结分节保存。

知识库工具都不产生外部副作用，因此不经过 Confirmation；但每次写入都留下可读文件与 Git 提交，用户可直接查看和修改。

## 5. 引用格式

引用（`ref`）是回答与执行决策定位原文的唯一凭据：

```json
{
  "id": "kb_01HZQ3M7V4W2X8",
  "path": "kb/people/zhang-laoshi.md",
  "heading": "张老师 / 沟通偏好",
  "lines": [18, 26],
  "commit": "9f2c1ab7d3e4f5061728394a5b6c7d8e9f0a1b2c"
}
```

- `commit` 是唯一版本标识，取完整 sha；引用不再带时间戳字段。
- `lines` 是含 frontmatter 的真实行号区间，闭区间；整篇引用覆盖整个文件。
- 回答中引用资料时必须给出 `ref`；前端据此展示来源与原文片段。
- 资料更新或移动后，旧 `ref` 仍可解析：按 `commit` 与 `path` 读取当时内容，按 `id` 追踪同一资料的当前位置。
- 引用不得指向不存在的内容；无法给出 `ref` 的结论只能作为模型推断，并明确标注。

## 6. 回答来源的记录与展示

来源由程序记录真实读取结果，不解析模型措辞：

- 工具注册声明“来源结果”提取能力，`kb_read` 成功返回后由本域把返回值转成来源记录（引用、标题、本次实际读到的原文片段）；通用网关只转发事件，不认具体工具名。
- 只有成功执行的 `kb_read` 产生来源；`kb_search` 的摘要不算引用。读取失败不产生来源。
- 来源按轮次保存在 SQLite（`task_run_sources`：任务、轮次、规范化 `ref`、标题、分节、本次读取的原文片段、读取顺序），同一轮重复读取同一 `commit + path + lines` 只记一次。
- 时间线接口在该轮最后一段 Agent 回答上返回 `sources[]`；刷新、重连与历史任务查看得到同一份记录。
- 单条来源的原文片段最长 600 字符，超出时截断并加省略号，避免一条回答被长资料撑破。
- 本阶段只在回答下展示来源卡（标题、路径、分节、行号、commit 短标识、原文片段），不提供资料浏览器、编辑入口或点击进入完整文件；这些属于 Phase 4。

## 7. 错误

复用 `server/errors.py` 的词汇与字段形状：`NotFoundError`、`VersionConflictError`（附 `current_version`，取 Git commit sha）。资料校验失败返回 `KbValidationError`（`invalid_kb`），附 `errors[]`，每项含 `field` 与 `message`，不保存数据，按引用读取失败时也不返回内容。资料文件或本地版本仓库不可用时返回 `KbStoreUnavailableError`（`kb_store_unavailable`）；索引缺失、损坏或无法重建时返回 `KbIndexUnavailableError`（`kb_index_unavailable`），此时不返回旧索引结果。
