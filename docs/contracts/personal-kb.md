# 个人知识库契约

状态：**约定，尚未实现**。本文定义个人知识库（Personal KB）的存储形态、索引、工具字段与引用语义。实现与本文冲突时先改本文，不静默偏离。

知识库保存“资料及原文证据”，与 Memory 保存“用户偏好与规则”分开，见 `skills.md`。

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

这是初始布局，不是固定分类法。Agent 可以按资料增长新建或重组目录，重组必须保持已有引用可解析（见第 5 节）。索引是派生数据，放在数据目录内且不进 Git，可随时从文件重建。

## 2. 文件格式

UTF-8 Markdown，带 frontmatter：

```yaml
---
id: kb_01HZQ3M7V4W2X8         # 稳定标识，路径变化时不变
title: 张老师
kind: person                   # person | project | reference | inbox | archive
tags: [课程, GSE]
source:                        # 可选，原始证据来源
  kind: mail                   # mail | calendar | kb | user | task
  ref: msg_19a2b3
created_at: 2026-09-14T10:22:31+08:00
updated_at: 2026-09-14T10:22:31+08:00
---
```

正文用二级标题分节，标题文本即锚点。原始证据与模型总结在同一文件中必须分节标明，不混写。

## 3. 索引与增量更新

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

| 工具 | 副作用 | 说明 |
| --- | --- | --- |
| `kb_search` | 只读 | 检索资料分节 |
| `kb_read` | 只读 | 读取原文，可指定历史版本 |
| `kb_save` | 本地写 | 新建资料文件 |
| `kb_update` | 本地写 | 修改已有资料文件 |
| `kb_archive` | 本地写 | 归档一次任务的来源与逐项结果 |

### `kb_search`

输入：

| 字段 | 类型 | 含义 |
| --- | --- | --- |
| `query` | string | 检索词 |
| `kind` | string | 可选，限定资料类型 |
| `tag` | string | 可选，限定标签 |
| `max_results` | integer | 最多返回数，默认 10 |

输出 `results[]`，每项含 `id`、`path`、`title`、`heading`、`snippet`、`ref`（结构见第 5 节）。不返回整篇正文。

### `kb_read`

输入 `path` 或 `id`（二者之一）、可选 `heading`、可选 `version`（`commit`）。输出 `title`、`kind`、`body`（Markdown 原文，不总结）、`ref`。指定 `heading` 时只返回该分节原文。

### `kb_save`

输入 `title`、`kind`、`body`、可选 `tags[]`、可选 `path`、可选 `source`。省略 `path` 时按 `kind` 与 `title` 生成。输出 `id`、`path`、`version`、`ref`。

### `kb_update`

输入 `id` 或 `path`、`expected_version`（`commit`）、以及要写入的 `title`、`tags[]`、`body`、`source`。输出 `id`、`path`、新 `version`、`ref`。版本不匹配时拒绝，历史版本不改写。

### `kb_archive`

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

## 6. 错误

复用 `server/errors.py` 的词汇与字段形状：`NotFoundError`、`VersionConflictError`（附 `current_version`）。资料校验失败返回 `KbValidationError`，附 `errors[]`，每项含 `field` 与 `message`，不保存数据。
