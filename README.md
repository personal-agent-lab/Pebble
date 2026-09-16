# Pebble

对话优先的个人助手 Agent。你可以随时和它对话——聊天、提问、交办事项；它按你的目标自主组合工具
（Gmail、iCloud Calendar、个人知识库，后续更多）完成真实事务，并在会话之间记住你的偏好（Memory）、
复用沉淀下来的流程（Skills）。程序负责保证外部写操作的授权、持久化与去重。

单用户单实例部署，服务常驻。PC 与手机浏览器共用同一套响应式界面：发起任务、查看进度、编辑草稿、
确认操作；任务不依赖始终开启的浏览器页面。新邮件是首版的系统触发源，收到即自动开始处理，
但邮件只是它能做的事之一。

当前状态：对话链、任务时间线、Gateway 与 Qoder CN/Gmail/iCloud Calendar 生产装配已接通，SQLite schema 为 11。
Gmail 支持搜索、单封与完整往来读取、入站附件读取、回复与主动新写邮件；两者共用内嵌草稿卡、
草稿编辑、最终版本确认、去重发送和结果核实。外发邮件只支持纯文字正文。
iCloud Calendar 支持查询、详情、冲突检查与创建单次日程：对话里给出标题和起止时间就直接创建，
缺信息先追问补齐，目标时间已有日程则不创建、在对话里说明冲突，你明确要求照建才覆盖创建；
日程不渲染卡片，结果只在对话文字里汇报。不邀请参与人。
Memory 的两个长期记忆文件、Agent 写入工具、逐轮加载与本地 Git 历史已实现；管理页面与历史
对话检索尚未实现。个人知识库已实现前四个阶段：Markdown 资料保存在实例数据目录的 `kb/` 下，
由同一数据目录的本地 Git 管理版本；可以保存、读取、更新、删除、移动、按历史版本读取与恢复，
也能按关键词检索分节、按引用读回原文，Agent 查到资料后直接作答，不展示来源；你直接改动的
文件会自动纳入版本；侧栏“资料”页可以浏览、搜索和用所见即所得编辑器编辑资料；邮件发送等操作
有结果后 Agent 会自主归档任务；主题页与导入、Skills、认证与 HTTPS 远程访问尚未实现，目前只能本机和同局域网访问。
真实账号验收仍在进行，本地测试通过不代表真实邮件或日程操作成功。

## 文档

- `docs/v1-spec.md`：需求范围、产品行为、验收标准。内容冲突时以此为准。
- `docs/v1-design.md`：组件划分、交付阶段、验证要求、当前实现与已知偏差。
- `docs/memory-spec.md`：记忆功能规格：短期上下文、长期记忆、常驻上下文与按需检索、历史检索。
- `docs/kb-spec.md`：个人资料库功能规格：保存与主动保存、检索作答、删除与恢复、主题页、外部笔记导入。
- `docs/contracts/mail.md`：Gmail 工具、同步触发、确认发送与核实的字段与语义（已实现）。
- `docs/contracts/calendar.md`：iCloud Calendar 工具、直连创建与冲突处理的字段与语义（已实现）。
- `docs/contracts/skills.md`：Memory 已实现，Skills 仍为约定。
- `docs/contracts/personal-kb.md`：个人知识库的工具、存储与引用语义（Phase 1–4 已实现，Phase 5 为约定）。

设计文档第 2 节的组件表与代码结构是目标结构，第 10 节记录已实现部分与已知偏差。

## 前置依赖

- Python 3.13（由 `server/.python-version` 固定，uv 会自动准备）
- [uv](https://docs.astral.sh/uv/)
- Node.js 与 npm（Vite 8 要求 20.19.0+ 或 22.12.0+，已用 v24.18.0 验证）

## 配置

所有配置项见仓库根 `.env.example`，复制为 `.env` 后按需修改：

```bash
cp .env.example .env
```

`.env` 不进 Git。

| 变量 | 含义 |
| --- | --- |
| `PEBBLE_DATA_DIR` | 实例数据目录，默认 `<仓库根>/.data` |
| `PEBBLE_HOST`、`PEBBLE_PORT` | 监听地址与端口，默认 `127.0.0.1:8000` |
| `QODERCN_PERSONAL_ACCESS_TOKEN` | Qoder CN 访问令牌，注意没有 `PEBBLE_` 前缀 |
| `PEBBLE_QODER_MODEL` | 托管模型型号，取值由 CLI 按账号动态下发（`qodercli --list-models`） |
| `PEBBLE_TITLE_MODEL` | 只给任务标题生成用的型号，默认沿用 `PEBBLE_QODER_MODEL` |
| `PEBBLE_MODEL_PROVIDER`、`PEBBLE_MODEL_API_KEY`、`PEBBLE_MODEL_BASE_URL` | 自定义模型（BYOK）。供应商、密钥、型号必须同时给全，`BASE_URL` 可选；provider 必须匹配账号的 BYOK 目录 |
| `PEBBLE_GMAIL_CREDENTIALS_PATH` | Gmail OAuth 桌面应用 JSON，默认 `<data_dir>/credentials.json` |
| `PEBBLE_GMAIL_TOKEN_PATH` | Gmail 授权结果，默认 `<data_dir>/gmail_token.json` |
| `PEBBLE_ICLOUD_ACCOUNT` | Apple ID 账号，仅在服务端使用 |
| `PEBBLE_ICLOUD_PASSWORD_PATH` | Apple App 专用密码文件的绝对路径 |
| `PEBBLE_ICLOUD_CALENDAR_URL` | 唯一主日历的完整 iCloud CalDAV collection URL |
| `PEBBLE_BACKEND_URL` | 只给 Vite 开发代理使用，后端不读 |

Gmail 首次启动在浏览器授权读取和发送权限。Key 仅交给 SDK 的模型配置，不进入系统提示或工具结果。
Qoder CN 与国际版的 SDK、Token 和配置目录不能混用。

生产运行需要有效的 Qoder CN、Gmail 和 iCloud Calendar 凭证；仅测试使用不装配真实依赖的 `create_app()`。

## 启动

以下命令都在仓库根目录执行。后端与前端是两个常驻进程，分别开终端运行。

后端：

```bash
uv sync --project server
uv run --project server python -m server.main
```

服务监听 `http://127.0.0.1:8000`，带热重载。

前端：

```bash
cd web
npm install
npm run dev
```

页面在 `http://127.0.0.1:5173`，Vite 把 `/api` 代理到后端；`host` 已开放局域网，
手机连同一网络后可用 Vite 打印的 Network 地址直接访问。

## 远程访问

规格要求手机不与服务在同一局域网也能完整使用（HTTPS + 单用户认证，见 `docs/v1-spec.md` §4.6）。
该能力属于交付阶段 6，尚未实现：当前服务只监听本机，认证与来源校验都没有接入，
因此只能在本机和同局域网内测试，不要暴露到公网。

设计只固定安全要求，不固定传输方案；反向隧道、反向代理加域名或私有网络的选定结果与配置步骤
会在实现后写回本节。

## 网页

`web/` 是 TypeScript + React + Vite 单页应用，路由为 `/tasks`（任务列表与发起新任务）和
`/tasks/:taskId`（统一时间线、完整草稿卡与逐项执行结果）。
视觉设计系统 token 见 `src/styles/tokens.css`，分种子、原语与语义三层。
断点 900px：以上为侧栏布局，以下折叠为底部 tab，两端功能一致。
任务列表就是导航本身：PC 在侧栏，手机在任务页内，默认列最近 5 条，其余折在「展开显示」后面。

邮件草稿固定在 Agent 生成时的对话位置，卡片内展示完整正文，并支持直接编辑、定向对话修改与最终确认。
未保存的修改不能确认，确认绑定卡片当前展示的草稿版本；发送状态和结果继续显示在原卡片上。
编辑收件人时每行填写一个地址，可保留显示名。
日程不渲染卡片：创建结果由 Agent 在对话文字里汇报，操作与执行记录通过接口查询。

任务列表没有列表级事件流，按 5 秒轮询刷新（页面不可见时暂停），新邮件自动触发的任务无需手动刷新；
任务详情用 SSE，确认后的执行在后台进行，事件流不携带执行状态，页面对执行结果按 1.5 秒轮询直到
草稿卡状态离开 `sending`，SSE 重连后整体重读时间线对账。
界面只呈现接口能支撑的内容：搜索框对应的接口尚未提供，暂不渲染；回答不展示资料来源。

## 验证

浏览器打开 `http://127.0.0.1:5173`，应看到任务列表（PC 在左侧侧栏）。后端不可达时页面显示
「无法连接服务」并提供重试，不白屏。

或直接请求接口：

```bash
curl http://127.0.0.1:8000/api/health
```

重启后端后确认数据文件仍在：

```bash
ls .data/pebble.db
```

## 长期记忆

当前有效的长期记忆保存在实例数据目录中：

```text
<PEBBLE_DATA_DIR>/memory/USER.md
<PEBBLE_DATA_DIR>/memory/MEMORY.md
```

`USER.md` 保存用户背景、长期目标与偏好，上限 1375 个 Unicode 字符；`MEMORY.md` 保存项目
事实、环境信息、术语与稳定约定，上限 2200 个字符。条目以独立一行 `§` 分隔，可直接编辑。
下一轮 Agent 调用会重新读取文件；Agent 有效修改会提交到 `<PEBBLE_DATA_DIR>` 内的独立本地 Git
仓库，数据库、凭证和 SDK 会话不进入该仓库。

## 个人资料库

资料是实例数据目录里的 Markdown 文件，目录结构由 Agent 按内容组织，你可以直接阅读、修改和
重组：

```text
<PEBBLE_DATA_DIR>/kb/**/*.md       # 资料本体，frontmatter 里带稳定 id
<PEBBLE_DATA_DIR>/kb-index.sqlite3 # 检索索引，派生数据，不进 Git，可随时重建
```

`kb/` 与 `memory/` 属于同一个本地 Git 仓库，每次写入即一次提交，可用 Git 查看历史与差异。
检索索引按二级标题分节，写入后自动增量更新；索引缺失、损坏或与已提交内容不一致时会在下一次
检索前整体重建。你在编辑器或文件夹里直接编辑、新增、移动、删除资料都不需要手动提交：下一次
资料库操作前，程序会把改动纳入版本（没有 frontmatter 的新文件会补上 id 与标题，正文不变；
移动按 id 认作同一份资料）。删除的资料历史仍在，可以在对话里让 Agent 找回。
Agent 查到资料后直接作答，回答不展示资料来源。

## 检查命令

后端测试与静态检查（仓库根执行）：

```bash
uv run --project server pytest -c server/pyproject.toml
uv run --project server ruff check --config server/pyproject.toml server tests
uv run --project server ruff format --config server/pyproject.toml server tests
```

`ruff` 需要显式给 `--config`：`tests/` 在仓库根，向上找不到 `server/pyproject.toml`，
否则会退回默认规则，与 `server/` 不一致。

前端类型检查、测试与构建（`web/` 内执行）：

```bash
npm run typecheck
npm test
npm run build
```

## 本地任务、草稿与确认执行服务

初始化数据库后使用 `server.sessions.service.SessionStore`、`server.sessions.timeline.TimelineStore`、
`server.tools.gmail.service.MailDraftStore`、`server.tools.calendar.service.CalendarEventStore` 和
`server.approval.service.ConfirmationService`。它们默认使用实例数据库，也可显式传入
`path=Path(...)`。邮件草稿与日程字段分别在对应域内校验，日程每次创建保存一份不可变内容版本；
`ConfirmationService` 必须注入实际的
Gmail 发送或 iCloud 创建函数，生产代码没有默认成功的外部写入。输入输出字段及错误含义见
[Gmail 契约](docs/contracts/mail.md)和 [Calendar 契约](docs/contracts/calendar.md)。

任务保存用户目标与 SDK 会话关联；操作管理版本与状态；邮件字段和原邮件去重留在邮件能力内。
跨任务复用同一操作后，各任务看到相同的最新草稿与状态，SDK 会话仍独立。

### 确认执行

- `accept_confirmation(task_id, operation_id, version)`：检查版本、保存确认并取得执行权。
- `execute_accepted(operation_id, deliver=True)`：读取已确认版本、执行并保存结果；重复调用不再次执行。
  日程直连创建传 `deliver=False`：结果就地返回给模型，不登记回传轮，避免同一件事汇报两遍。
- `get_execution(operation_id)`：操作当前状态、确认信息及已保存结果。
- `get_agent_result(operation_id)`：回传数据；尚无结果或回传任务未关联会话时返回 `None`。
- `verify_pending(operation_id)`：只读核实 `unknown`，找到对应外部结果后更新状态并登记回传。
- `recover_interrupted_executions()`：重启时已开始的外部执行记 `unknown`，尚未开始的记 `failed`；
  在数据库初始化后、接受请求前调用，不自动重试。

外部执行函数的输入输出见对应契约；异常、中断及不符契约的返回都记 `unknown`，不自动重试，
`unknown` 可以显式核实，重复确认不会再次产生外部写入。

## 验证范围

- `tests/storage/`：真实 SQLite 的版本、并发、回滚、恢复与确认去重。
- `tests/gateway/`：后台调度、SDK 选项装配与事件映射、工具端点与工具边界、执行结果回传、邮件全链路衔接。
- `tests/api/`：健康检查，以及独立进程的 HTTP/SSE、断线后继续执行与重启。
- `tests/tools/`：邮件与日历的解析、字段校验、协议内容、结果核实、日程直连创建与冲突覆盖、工具声明。

测试替换 SDK 子进程、Gmail 投递和 iCloud CalDAV 这三个外部边界，其余模块、SQLite、HTTP 都是真的。
SDK 模型响应、Gmail 投递与 iCloud 读写仍须用明确授权的账号和内容验收；测试通过不代表真实外部操作成功。

Qoder 短期上下文的真实验收脚本不会随 pytest 运行。它验证多轮、独立进程恢复和每轮最新
材料；加 `--compact` 会发送较长的合成文本并验证压缩，因而消耗更多真实额度：

```bash
uv run --project server python -m tests.acceptance.qoder_context
uv run --project server python -m tests.acceptance.qoder_context --compact
```

长期记忆的真实验收同样不会随 pytest 运行。它使用临时实例目录和真实 Qoder 模型，验证模型
实际写入文件、产生 Git 提交，并在全新的 SDK 会话中读回记忆：

```bash
uv run --project server python -m tests.acceptance.qoder_memory
uv run --project server python -m tests.acceptance.qoder_kb
uv run --project server python -m tests.acceptance.qoder_kb_search
```

最近一次结果见 `docs/validation/qoder-context-2026-09-15.md`、
`docs/validation/qoder-memory-2026-09-15.md` 与 `docs/validation/qoder-kb-phase2-2026-09-16.md`。

触发源通过 `create_app(mail_source=...)` 装配，接口是 `server/gateway/runtime.py` 的 `MailSource`
（`start` / `stop` / `error`）。真实 Gmail 检测由 `server/tools/gmail/sync.py` 实现同一接口，
生产工厂统一装配。

## 生产装配

生产入口为 `server.main:create_production_app`（Uvicorn factory），上面的 `python -m server.main`
已使用该入口。`create_app()` 保留为显式依赖注入的应用构造函数，供测试使用。启动顺序为
初始化数据库 → 恢复中断的发送 → 恢复调用调度 → 启动邮件检测；关闭时先停检测再等发送落盘。

Gmail 首次启动记录当前 historyId，随后每 10 秒检测新增的收件箱邮件；首次启动前的旧邮件不会批量触发。
跨进程游标保存在 `.data/gmail_sync.json`。邮件成功交给 Gateway 的持久化任务入口后才推进游标，
重复通知由 Gateway 去重。游标失效明确停止检测，health 显示原因，需要核对后重新建立同步位置，
不静默跳过缺口。常规检测错误保留游标，在下一轮重新查询。

新邮件轮次只开放只读工具，结果回传轮次开放只读与本地写；只有用户亲自发起的对话轮能看到日程直连
创建工具，因此邮件或资料内容里的指令无法驱动外部写入。用户要求起草后，SDK 才可调用准备、读取及
更新草稿工具；更新使用当前已保存版本，直接编辑与 Agent 修改共用 `MailDraftStore` 的版本控制。
草稿保存事件交给网页。邮件发送函数只由 Confirmation 在用户确认最终版本后调用，执行结果回到确认
任务的原 SDK 会话。

模型经应用进程内的 MCP 端点（server 名 `pebble`）调用工具，内置工具与本机设置关闭：每轮登记一个
一次性路径供 qodercli 子进程按回环地址连接，轮次结束即撤销。每轮独立启动一次 qodercli 子进程，
会话标识由 SDK 生成并按任务保存，重启后靠它接续。会话记录落在 `.data/agent/config/` 下，
模型上下文由该记录恢复；网页时间线由 SQLite 独立持久化，刷新后仍保持文字与草稿卡的生成顺序。
基础系统提示在同一 SDK 会话内保持固定；每轮动态材料通过 SessionStart hook 注入。当前 CN
SDK headless runtime 未启用自动压缩，Gateway 达到 SDK 报告的阈值时先执行手动压缩再处理输入。

联合测试 `tests/gateway/test_integrated_mail.py` 覆盖实际模块衔接、Agent 修改与手动编辑、
旧版本拒绝、最终内容一致性、重复确认、结果会话关联与游标推进；`tests/gateway/test_agent_stream.py`
覆盖选项装配、事件映射与工具边界；`tests/api/test_http_flow.py` 覆盖内嵌时间线、原卡片更新和确认发送。
其中 SDK 模型响应和 Gmail 投递仍为测试边界替身；真实验收结果需另行记录。
