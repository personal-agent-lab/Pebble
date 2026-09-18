# 个人知识库契约

本文定义个人知识库的存储、索引、工具与接口字段；产品行为见 `kb-spec.md`。实现与本文冲突时先改本文。

## 0. 交付阶段

| 阶段 | 内容 |
| --- | --- |
| Phase 1 | `kb/` 文件 + 本地 Git 版本；列举、保存、读取、更新、历史 |
| Phase 2 | `kb_search` 与 FTS5 分节索引；按路径、`id` 或引用读原文 |
| Phase 3 | 跟随用户在文件系统中的改动；删除、移动、恢复；触发轮开放资料新建与修改 |
| Phase 4 | 管理界面；`kb_archive`；`summary` 与资料目录 |
| Phase 5 | 主题页与后台主题整理 |
| Phase 6 | 正文图片（第 10 节） |

进度见 `status.md`。

## 1. 存储

资料是 `<data_dir>/kb/` 下的 Markdown 文件，目录由 Agent 按内容选择（省略 `path` 时放在根目录）。固定目录只有三个：`topics/`（主题页，后台整理只写这里）、`archive/`（任务归档）、`assets/`（正文图片）。`kb/` 属于数据目录内的独立本地 Git 仓库，写入即提交、不推送。索引与资料目录是派生数据，不进 Git，可随时重建。

## 2. 文件格式

UTF-8 Markdown，带 frontmatter：

```yaml
---
id: kb_01HZQ3M7V4W2X8         # 稳定标识，路径变化时不变
title: 张老师
summary: GSE 课程任课老师       # 一句话说明，进入资料目录与检索；主题页必填
created_at: 2026-09-14T10:22:31+08:00
updated_at: 2026-09-14T10:22:31+08:00
---
```

- 没有出处字段，也没有标签或 `kind`；类别由目录体现。
- 正文用二级标题分节；原始内容与模型总结必须分节写。
- 用户新建、缺少 `id` 的文件纳入版本时补上 `id`、`title`（取第一个一级标题，否则取文件名）与时间，正文不动；复制出来与另一份现存资料 `id` 相同的，换发新 `id`；frontmatter 无法解析的照样纳入版本，但不补标识、不进索引。

## 3. 索引与用户改动

索引文件为 `<data_dir>/kb-index.sqlite3`，每条记录是一个分节：

- 分节：每个 `##` 到下一个 `##` 为一段，二级标题前的前言与没有二级标题的整篇各成一段；代码围栏里的 `##` 不算，空分节不收录。
- 字段：`id`、`path`、`title`、`summary`、`heading`（如 `张老师 / 沟通偏好`）、`lines`（含 frontmatter 的 1-based 真实行号）、`content_hash`、`commit`。
- 检索：FTS5 `trigram` 分词，中文、英文与编号都能命中；1–2 个字的词改用同表的包含匹配。排序先看命中字段（标题 > 分节标题 > 说明 > 正文），再看相关度，最后按路径与行号；在全部候选上排序后才截断。

更新：

- 应用写入提交后，在资料库锁内只替换受影响文件的条目。
- 索引记录 `kb/` 的 Git tree 标识；缺失、损坏、结构变化或 tree 不一致时整体重建。重建失败返回 `kb_index_unavailable`，不查询旧索引。
- 文件已写入但索引失败时以文件为准，结果标 `index_status: "stale"`。

用户改动：

- 每次资料库操作前，在锁内检查 `kb/` 下未提交的改动，先纳入版本再执行。不监听文件，改动在下一次操作时才纳入。
- 移动按 `id` 识别：先以原内容单独提交移动（`[Kb] User move …`），再提交其余改动（`[Kb] User edit …`），历史因此能跟随路径。
- 用户改过的文件再被 Agent 按旧版本修改时，`expected_version` 不匹配，返回 `version_conflict`，不覆盖用户内容。

## 4. Agent 可见工具

| 工具 | 副作用 | 输入 | 输出 |
| --- | --- | --- | --- |
| `kb_search` | 只读 | `query`（空格分词，全部命中）、`max_results`（默认 10，1–20） | `results[]`：`id`、`path`、`title`、`heading`、`lines`、`snippet`、`score`、`ref`；不含正文 |
| `kb_list` | 只读 | 可选 `directory`、`deleted` | 每份资料的 `id`、`path`、`title`、`summary`、`updated_at`、`version`；`deleted: true` 时列出已删除且未恢复的资料（新到旧，含删除前的 `path`、`deleted_at`、`version`） |
| `kb_read` | 只读 | `path` 或 `id`，可选 `version`；或 `ref` | 元数据、`heading`、`lines`、`commit`、`body`、规范化的 `ref` |
| `kb_history` | 只读 | `path` 或 `id`（已删除的也能定位） | `id`、`path`、`title`、`deleted`，以及新到旧的 `versions[]`（`version`、`changed_at`、`summary`、`path`、`deleted`） |
| `kb_save` | 本地写，所有轮次 | `title`、`body`、`summary`（写入 `topics/` 时必填）、可选 `path` | `id`、`path`、`title`、`version`、`index_status`、`ref` |
| `kb_update` | 本地写，所有轮次 | `expected_version`、`id` 或 `path`，以及要改的 `title`、`summary`（空串表示删除）与正文（`body` 或 `operations` 二选一） | `id`、`path`、`title`、`previous_version`、`version`、`index_status`、`ref`；按行修改另有 `applied[]` |
| `kb_archive` | 本地写 | `title`、非空 `items[]`（`kind: original \| summary`、可选 `heading`、非空 `text`） | 同 `kb_save` |
| `kb_delete` | 本地写，仅用户对话轮 | `expected_version`、`id` 或 `path` | `id`、`path`、`previous_version`、`version` |
| `kb_move` | 本地写，仅用户对话轮 | `expected_version`、`id` 或 `path`、`new_path` | `id`、`previous_path`、`path`、`previous_version`、`version` |
| `kb_restore` | 本地写，仅用户对话轮 | `id` 或 `path`、`version` | `id`、`path`、`restored_from`、`version` |

各轮次可见范围按 `v1-design.md` §3；`kb_archive` 在用户对话轮与执行结果回传轮可见。后台主题整理会话（Phase 5）只有只读工具，以及限定写入 `kb/topics/` 的 `kb_save` 与 `kb_update`。删除、移动与恢复前要取得用户同意（`kb-spec.md` 第 5.2 节），这一点写在工具说明里由模型遵守；程序只保证这三个工具只在用户对话轮可见。工具都不产生外部副作用，不经过 Confirmation。

规则：

- **`kb_read`**：`ref` 必须原样取自工具返回。按 `commit + path + lines` 读该版本的片段：commit 或路径不存在返回 `not_found`；`id` 不匹配、行号越界或区间不对应某个分节或整篇正文返回 `invalid_kb`，不返回内容。按 `path`/`id` 读整篇时 `heading` 为空，`lines` 覆盖正文区间。给模型的当前版本整篇读取把 `body` 换成 `anchored_body`（每个非空行写成 `锚点| 原文`），供 `operations` 定位；读历史版本、按 `ref` 读取以及 HTTP 接口都不带锚点。
- **文件名**：取 `title`，把 `/\:*?"<>|` 与控制字符换成 `-`，合并空白，去掉开头的 `.`，截到 60 个字符；同一文件夹重名时编号（`标题 2.md`）。`kb_save` 目标已存在时拒绝。`kb_update` 改标题时文件名随之更新、文件夹不变：先提交修改，再以原内容单独提交改名；已一致或只差大小写时不改名，改名失败只记日志。
- **`operations`**：与 `memory_edit` 共用 `server/storage/line_edit.py`（见 `memory.md` 第 2 节），差别是只有正文一个部分、没有 `move`、`append` 不需要 `target`、不查重。锚点失效或范围冲突时返回 `invalid_kb`，附 `errors[]` 与带锚点的最新 `document`；版本不匹配时先返回 `version_conflict`；改完正文为空时拒绝。提交说明分别为 `[Kb] Edit (N changes) 标题` 与 `[Kb] Update 标题`。
- **`kb_move`** 只换文件夹，改名用 `kb_update` 改标题；目标已存在时拒绝。
- **`kb_restore`**：资料仍在时恢复内容；已删除时在原路径重建，路径被占用则拒绝。恢复产生新提交，不改写历史。
- **`kb_archive`**：写入 `kb/archive/<日期> <标题>.md`，先列全部原始内容，再列总结，每项一节（`原始内容：<heading>`、`总结与执行结果：<heading>`）。何时归档由模型判断（外部操作有了实际结果之后；同一任务再有新结果时更新原归档），不记录任务标识或出处。
- **提示**：写入成功后，程序按 `notice_renderer` 在时间线显示标题与所在文件夹（不显示文件名），修改另外显示前后版本；`index_status` 为 `stale` 时改为“……但当前不可检索”。

## 5. 引用

`ref` 是工具之间定位原文的凭据，只在工具内流转，不展示、不作为出处：

```json
{"id": "kb_01HZQ3M7V4W2X8", "path": "kb/people/zhang-laoshi.md", "heading": "张老师 / 沟通偏好", "lines": [18, 26], "commit": "9f2c1ab7d3e4f5061728394a5b6c7d8e9f0a1b2c"}
```

`commit` 为完整 sha；`lines` 为闭区间，含 frontmatter 行号；资料更新或移动后，旧 `ref` 仍按 `commit` 与 `path` 读回当时的内容；合法区间只有真实分节与整篇正文。

## 6. 回答与来源

回答依据与不展示来源的规则见 `kb-spec.md` 第 4 节；程序不记录 Agent 读了哪些资料。

## 7. 资料目录与主题页

资料目录由 `server/tools/personal_kb/catalog.py` 每轮现算，不存文件：

- 先纳入用户改动，再读取全部资料的 `title`、`summary` 与 `updated_at`。
- 作为 `## 资料目录` 材料块，排在两块记忆之后、本轮材料之前；资料库为空时不生成，生成失败只记日志，不中断调用。
- 格式：首行 `资料库共 N 份资料；需要细节时用 kb_search 检索，或用 kb_read 读取原文。`，随后按目录分组（`- 目录/（N 份）`，根目录记作 `（根目录）`），资料行 `  - 标题：summary`（说明超过 60 个字符时截断）。
- 上限 1500 个 Unicode 字符：目录按最近更新排序，先保证放得下的目录都有分组行，放不下的合并为 `- 另有 K 个目录共 M 份资料未列出`；再按更新时间逐条填入资料，没列出的写 `  - 另有 N 份`。

主题页（Phase 5）：`kb/topics/` 下的普通资料，必须有 `summary`，和其他资料一样进入目录。后台主题整理是一次性模型会话，调度复用后台记忆回顾（累计若干个已完成的用户消息轮后执行，另有手动入口），输入为窗口内对话、期间新增或修改的资料清单与当前目录；只能写 `kb/topics/`，不写时间线、不通知用户；基于 `expected_version` 写入，遇到 `version_conflict` 时本次跳过。

## 8. 资料管理 HTTP 接口

界面操作等同于用户直接改文件：不经过 Agent，不需要对话中的同意，不产生时间线提示；与工具共用 `KbStore` 的校验、版本与索引规则，错误见第 9 节。

| 方法与路径 | 输入 | 输出 |
| --- | --- | --- |
| `GET /api/kb/documents` | 可选 `directory` | 同 `kb_list` |
| `GET /api/kb/document` | `path` | `id`、`path`、`title`、`summary`、`created_at`、`updated_at`、`version`、`body` |
| `GET /api/kb/search` | `q` | 同 `kb_search`，最多 20 条 |
| `POST /api/kb/documents` | `title`、`body`、可选 `summary`，以及 `path` 或 `directory` 二选一 | 201，同 `kb_save`；`directory` 表示按标题在该文件夹下生成文件名 |
| `GET /api/kb/folders` | — | `folders[]`：全部子文件夹（不带 `kb/` 前缀，含空文件夹，不含隐藏目录） |
| `POST /api/kb/folders` | `path` | 201，`path`；为空、越界、以 `.` 开头或已存在时返回 `invalid_kb` |
| `POST /api/kb/summary` | `title`、`body` | `{summary}`：轻量模型按标题与正文开头（至多 12000 字）起草，截到 60 字符，不写入；正文为空返回 `invalid_kb`，模型失败返回 503 |
| `POST /api/kb/document/update` | `path`、`expected_version` 与要改的字段 | 同 `kb_update` |
| `POST /api/kb/document/move` | `path`、`expected_version`、`new_path` | 同 `kb_move` |
| `POST /api/kb/document/delete` | `path`、`expected_version` | 同 `kb_delete` |

界面约定（具体交互见 `web/src/pages/Kb*.tsx`、`web/src/components/KbDialogs.tsx`、`mathNodes.ts` 等组件的注释与测试）：

- 当前文件夹写在地址 `?dir=` 里，搜索覆盖整个资料库。文件夹不进版本历史，最后一份资料删除后空目录随之消失；文件夹列表不显示 `assets/`。
- 只有正文实际被编辑时才提交编辑器输出的 Markdown，其余情况提交原文；编辑过的正文里，作为文字的 `$` 写回时转义为 `\$`。
- 重命名只传 `title`；移动沿用原文件名，重名时编号。
- 界面不显示文件名，只显示所在文件夹与标题。
- 写入都带读取时的 `version`，冲突时提示重新载入，不覆盖。
- 公式按 remark-math 读写并用 KaTeX 排版，行内公式按 Pandoc 规则收紧；聊天消息采用同一套规则。原文中的 HTML 按纯文本显示。
- 不提供历史版本与恢复入口，不展示 Agent 的读取记录。

## 9. 错误

通用错误见 `v1-design.md` §3，资料的 `current_version` 是 commit sha。本域另有：

| 名称 | HTTP | 含义 |
| --- | --- | --- |
| `invalid_kb` | 422 | 校验失败，附 `errors[]`；按行修改失败时另附 `document`；后台整理写到 `kb/topics/` 之外也返回它 |
| `kb_store_unavailable` | 503 | 资料文件或本地仓库不可用 |
| `kb_index_unavailable` | 503 | 索引缺失、损坏或无法重建，不返回旧结果 |

## 10. 正文图片（Phase 6）

图片是资料的附属文件，不是独立资料。

- **存储**：`kb/assets/<内容 SHA-256 前 16 位>.<扩展名>`，写入即提交（`[Kb] Add asset …`），相同内容只存一份。只接受 PNG、JPEG、WebP，单张不超过 10 MB，按内容判断类型；不接受 SVG。`assets/` 不放 Markdown，保存或移动到这里返回 `invalid_kb`；其中的文件不补 frontmatter、不进索引与目录。
- **引用**：正文写 `![说明](assets/3f2a9c0d1e7b4a56.png)`，路径相对资料库根目录，移动资料时不需要改写（代价是外部编辑器可能显示不出子文件夹里资料的图片）。`http(s)` 外链按原地址显示，不下载。
- **图片说明**：插入后由轻量模型看图生成（有文字的图转写要点，其余简述画面；单行，不超过 1000 字符），写在 alt 文本里，随正文进入版本与索引。调用时声明 `is_vl`，所配模型须支持图片输入；模型看不到图（回 `NO_IMAGE`）或调用失败时说明为空串，照常上传。直接放进 `assets/` 的图片与手写的引用不补说明。Agent 只看引用与说明，不读图片，也没有图片工具；对话附件里的图片不自动存入资料库。

| 方法与路径 | 输入 | 输出 |
| --- | --- | --- |
| `POST /api/kb/assets` | multipart 单个文件 | 201，`{path}`；立即返回，不等说明生成；类型或大小不符返回 `invalid_kb` |
| `POST /api/kb/assets/describe` | `path`（`assets/…`） | `{description}`，不写文件；看不到图或失败时为空串；路径不在 `assets/` 下返回 `invalid_kb`，文件不存在返回 404 |
| `GET /api/kb/assets/{name}` | 文件名 | 图片原文件，带 `X-Content-Type-Options: nosniff`；只读 `kb/assets/` |

- 编辑器粘贴或拖入图片：上传成功后单独成段插入，随后在后台请求说明，填进仍为空的 alt；用户已写说明或已删除图片时不覆盖。粘贴内容引用了拿不到的本地图片时，原位留占位 `〔图片未随粘贴带入：文件名〕`。具体交互见 `web/src/components/imageBlock.ts`、`imageView.ts` 与 `web/src/markdown.ts`。
- 编辑器与对话显示时把 `assets/…` 映射到 `GET /api/kb/assets/…`，保存回文件的仍是 `assets/…`；对话里点击图片在新标签页打开原图。
- 不清理不再被引用的图片（可能被别的资料或历史版本引用）；不做缩略图、压缩或以图搜图。
