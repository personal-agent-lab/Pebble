# 个人知识库契约

状态：**Phase 1 已实现**（`kb_save`/`kb_read`/`kb_update`）。`kb_search`/`kb_archive` 与 FTS5 索引留待后续阶段。本文定义个人知识库（Personal KB）的存储形态、索引、工具字段与引用语义。实现与本文冲突时先改本文，不静默偏离。

知识库保存“资料及原文证据”，与 Memory 保存“用户偏好与规则”分开，见 `skills.md`。

## 0. 交付阶段

知识库分四个阶段交付，当前实现到 Phase 1：

| 阶段 | 内容 | 状态 |
| --- | --- | --- |
| Phase 1 | `kb/` 文件存储 + 本地 Git 版本；`kb_save`/`kb_read`/`kb_update`；稳定 `id`、来源、版本历史；与长期记忆的边界 | 已实现 |
| Phase 2 | 按内容检索：`kb_search` + SQLite FTS5 索引、增量更新与重建 | 未实现 |
| Phase 3 | 自动跟随用户在文件系统里的改动：移动/重命名/删除的索引同步与“同一份资料”识别、并发编辑检测 | 未实现 |
| Phase 4 | 资料管理界面、版本差异视图、读写记录面板；`kb_archive` 任务归档；逐条核对后迁移旧记忆 | 未实现 |

Phase 1 只能按路径或 `id` 精确读取，不能按内容检索；Agent 写入即提交，但用户手动改动暂不自动纳入索引；不批量迁移已有长期记忆里的旧资料。下文各节中标注 Phase 的部分表示该能力的交付阶段，未标注者为 Phase 1 行为或全局约定。

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

## 3. 索引与增量更新（Phase 2）

Phase 1 无索引：`kb_read` 给 `path` 直接定位，给 `id` 时线性扫描 `kb/**/*.md` 的 frontmatter 匹配。下述索引为 Phase 2 能力。

索引为本地全文索引（SQLite FTS5），每条记录对应一个文件分节：

| 字段 | 含义 |
| --- | --- |
| `id`、`path`、`title`、`kind`、`tags` | 资料标识与分类 |
| `heading` | 分节标题路径，例如 `张老师 / 沟通偏好` |
| `lines` | 该分节在文件中的起止行 |
| `content_hash` | 该分节内容哈希 |
| `commit` | 写入该版本时的 Git 提交 |

更新规则：

- 写入或修改文件后，在同一次本地写操作内更新受影响文件的索引条目；用户直接改文件后由文件变更检测补更新。
- 服务启动时比对 `content_hash` 修复漂移；索引损坏时可整体重建，重建不改变资料内容。
- 删除文件时索引条目标记 `removed`，历史引用仍可按 `commit` 定位当时内容。
- 索引更新不需要用户触发，也不因单次写入失败而留下与文件不一致的索引。

## 4. Agent 可见工具

| 工具 | 副作用 | 阶段 | 说明 |
| --- | --- | --- | --- |
| `kb_read` | 只读 | Phase 1 | 读取原文，可指定历史版本 |
| `kb_save` | 本地写 | Phase 1 | 新建资料文件 |
| `kb_update` | 本地写 | Phase 1 | 修改已有资料文件 |
| `kb_search` | 只读 | Phase 2 | 检索资料分节 |
| `kb_archive` | 本地写 | Phase 4 | 归档一次任务的来源与逐项结果 |

### `kb_search`（Phase 2）

输入：

| 字段 | 类型 | 含义 |
| --- | --- | --- |
| `query` | string | 检索词 |
| `kind` | string | 可选，限定资料类型 |
| `tag` | string | 可选，限定标签 |
| `max_results` | integer | 最多返回数，默认 10 |

输出 `results[]`，每项含 `id`、`path`、`title`、`heading`、`snippet`、`ref`（结构见第 5 节）。不返回整篇正文。

### `kb_read`

输入 `path` 或 `id`（二者之一）、可选 `version`（某次提交的 Git commit，读取历史版本）。输出 `id`、`title`、`tags`、`source`、`body`（Markdown 原文，不总结）、`ref`。Phase 1 不支持 `heading` 分节读取。

### `kb_save`

输入 `title`、`body`、可选 `path`、可选 `tags[]`、可选 `source`。省略 `path` 时落到 `kb/inbox/`，文件名由 `title` 的 slug 加 `id` 后六位生成。输出 `id`、`path`、`version`、`ref`。目标文件已存在时拒绝（提示改用 `kb_update`）。

### `kb_update`

输入 `expected_version`（该资料当前的 Git commit）、`id` 或 `path`、以及要写入的 `title`、`tags[]`、`body`、`source`（只传需要改的字段，未传的保持原样）。输出 `id`、`path`、新 `version`、`ref`。`expected_version` 与当前 commit 不匹配时拒绝并返回 `VersionConflictError`，不写入；`id` 跨修改保持不变，历史版本不改写。

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
  "commit": "9f2c1ab",
  "version": "2026-09-14T10:22:31+08:00"
}
```

- 回答中引用资料时必须给出 `ref`；前端据此展示来源并可看到原文片段。
- 资料更新或移动后，旧 `ref` 仍可解析：按 `commit` 与 `path` 读取当时内容，按 `id` 追踪同一资料的当前位置。
- 引用不得指向不存在的内容；无法给出 `ref` 的结论只能作为模型推断，并明确标注。
- Phase 1 的 `ref` 只含 `id`、`path`、`commit`（Git sha）、`version`（`updated_at` 时间戳）；`heading` 与 `lines` 待 Phase 2 分节索引就位后再补。

## 6. 错误

复用 `server/errors.py` 的词汇与字段形状：`NotFoundError`、`VersionConflictError`（附 `current_version`，Phase 1 传 Git commit sha）。资料校验失败返回 `KbValidationError`（`invalid_kb`），附 `errors[]`，每项含 `field` 与 `message`，不保存数据。资料文件或本地版本仓库不可用时返回 `KbStoreUnavailableError`（`kb_store_unavailable`）。
