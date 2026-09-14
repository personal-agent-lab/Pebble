# Pebble 

持续运行的个人 Agent。一个常驻 Python 服务，由单个 Agent 按用户目标组合工具（Gmail、iCloud Calendar、个人资料库）完成真实事务，
程序负责保证确认、持久化与去重。单用户单实例部署，PC 和手机浏览器都能发起任务、编辑草稿和确认操作；任务不依赖浏览器页面保持开启。

当前状态：任务、草稿版本、确认执行、Gateway、网页与 Qoder CN/Gmail
生产装配已连接，SQLite schema 为 4。Gmail 支持搜索、单封与完整往来读取、附件读取、
回复与主动新写邮件；两者共用草稿编辑、最终版本确认、去重发送和结果核实。
生产入口不使用模拟邮箱或内存草稿；真实账号七步验收仍在进行，
本地测试通过不代表真实邮件已发送。Memory、Skill、KB 和认证部署仍未完成。

## 文档

- `docs/v1-spec.md`：需求范围、产品行为、验收标准。内容冲突时以此为准。
- `docs/v1-design.md`：组件划分、交付阶段、验证要求、当前实现。
- `docs/v1-mail-flow-contract.md`：第一条邮件链的接口字段与语义。

设计文档第 2 节的组件表是目标结构，`server/sessions/`、`server/tools/gmail/` 与 `server/approval/`
已实现本地存储；HTTP 接口位于 `server/api/`，后台调用管理位于 `server/gateway/`，调用记录位于 `server/sessions/`，
`server/agent/` 装配 Qoder CN SDK，会话历史直接读取 SDK 持久化记录。

## 前置依赖

- Python 3.13（由 `server/.python-version` 固定，uv 会自动准备）
- [uv](https://docs.astral.sh/uv/)
- Node.js 与 npm（Vite 8 要求 20.19.0+ 或 22.12.0+，已用 v24.18.0 验证）

## 配置

所有配置项见仓库根 `.env.example`，复制为 `.env` 后按需修改：

```bash
cp .env.example .env
```

`.env` 不进 Git。Qoder CN 使用 `QODERCN_PERSONAL_ACCESS_TOKEN`。
Gmail 使用 `PEBBLE_GMAIL_CREDENTIALS_PATH` 指向 OAuth 桌面应用 JSON；首次启动在浏览器授权
读取和发送权限，授权结果保存到实例目录的 `gmail_token.json`。
自定义模型使用 `PEBBLE_MODEL_PROVIDER`、`PEBBLE_QODER_MODEL`、`PEBBLE_MODEL_API_KEY`，
可选 `PEBBLE_MODEL_BASE_URL`；provider 必须匹配账号的 BYOK 目录。Key 仅交给 SDK 的模型配置，
不进入系统提示或工具结果。Qoder CN 与国际版的 SDK、Token 和配置目录不能混用。

默认数据目录为 `<仓库根>/.data`，监听 `127.0.0.1:8000`。生产运行需要有效的 Qoder CN
和 Gmail 凭证；仅测试使用不装配真实依赖的 `create_app()`。

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

## 网页

`web/` 是 TypeScript + React + Vite 单页应用，路由为 `/tasks`（发起新任务）、
`/tasks/:taskId`（对话、待确认内容与逐项执行结果）、`/tasks/:taskId/confirm`（草稿编辑与确认）。
视觉设计系统 token 见 `src/styles/tokens.css`。
断点 900px：以上为侧栏布局，以下折叠为底部 tab，两端功能一致。
任务列表就是导航本身：PC 在侧栏，手机在任务页内，默认列最近 5 条，其余折在「展开显示」后面。
对话卡片进入完整草稿页审阅与确认；未保存的修改不能确认，确认绑定展示草稿的版本。
编辑收件人时每行填写一个地址，可保留显示名。

任务列表没有列表级事件流，按 5 秒轮询刷新（页面不可见时暂停），新邮件自动触发的任务无需手动刷新；
确认后的发送在后台执行，事件流不携带执行状态，页面对执行结果按 1.5 秒轮询直到状态离开 `sending`。
界面只呈现接口能支撑的内容：设计稿中的来源引用面板、日程预览卡与搜索框对应的接口尚未提供，暂不渲染。

## 验证

浏览器打开 `http://127.0.0.1:5173`，应看到任务列表（PC 在左侧侧栏）。后端不可达时页面显示「无法连接服务」
并提供重试，不白屏。

或直接请求接口：

```bash
curl http://127.0.0.1:8000/api/health
```

重启后端后确认数据文件仍在：

```bash
ls .data/pebble.db
```

## 检查命令

后端测试与静态检查（仓库根执行）：

```bash
uv run --project server pytest -c server/pyproject.toml
uv run --project server ruff check --config server/pyproject.toml server tests
uv run --project server ruff format --config server/pyproject.toml server tests
```

`ruff` 需要显式给 `--config`：`tests/` 在仓库根，向上找不到 `server/pyproject.toml`，
否则会退回默认规则，与 `server/` 不一致。

前端类型检查与构建（`web/` 内执行）：

```bash
npm run typecheck
npm run build
```

## 本地任务、草稿与确认发送服务

初始化数据库后使用 `server.sessions.service.SessionStore`、
`server.tools.gmail.service.MailDraftStore` 和 `server.approval.service.ConfirmationService`。
三者默认使用实例数据库，也可显式传入 `path=Path(...)`。草稿校验由 `server/tools/gmail/service.py`
的纯函数在存储内完成；`ConfirmationService` 必须传入 Gmail 同步发送函数，生产代码没有
默认成功的发送函数。输入输出字段及错误含义见
[邮件接口字段契约](docs/v1-mail-flow-contract.md)。

任务保存用户目标与 SDK 会话关联；操作管理版本与状态；邮件字段和原邮件去重留在邮件能力内。
跨任务复用同一操作后，各任务看到相同的最新草稿与状态，SDK 会话仍独立。

### 确认发送

- `accept_confirmation(task_id, operation_id, version)`：检查版本、保存确认并取得执行权。
- `execute_accepted(operation_id)`：后台读取已确认版本、发送并保存结果；重复调用不再次发送。
- `get_execution(operation_id)`：操作当前状态、确认信息及已保存结果。
- `get_agent_result(operation_id)`：契约第 7 节的回传数据；尚无结果或回传任务未关联会话时返回 `None`。
- `verify_pending(operation_id)`：只读核实 `unknown`，找到已发送证据后更新为 `sent` 并登记回传。
- `recover_interrupted_executions()`：重启时已开始的发送记 `unknown`，尚未开始的记 `failed`；
  在数据库初始化后、接受请求前调用，不自动重发。

发送函数输入输出见契约第 6 节；异常、中断及不符契约的返回都记 `unknown`，不自动重试，
`unknown` 可以显式核实，重复确认不会重新发送，也没有重发入口。

## 验证范围

- `tests/storage/`：真实 SQLite 的版本、并发、回滚、恢复与确认去重。
- `tests/gateway/`：后台调度、SDK 选项装配与事件映射、工具端点与工具边界、执行结果回传。
- `tests/api/test_http_flow.py`：独立进程的 HTTP/SSE、断线后继续执行与重启。
- `tests/tools/`：邮件解析、草稿校验、报文构建与发送结果核实。

测试只替换 SDK 子进程与 Gmail 投递这两个外部边界，其余模块、SQLite、HTTP 都是真的。
SDK 模型响应与 Gmail 投递仍须用明确授权的账号和内容验收；测试通过不代表真实邮件发送成功。

新邮件来源通过 `create_app(mail_source=...)` 装配，接口是 `server/gateway/runtime.py` 的
`MailSource`（`start` / `stop` / `error`）。真实 Gmail 检测由 `server/tools/gmail/sync.py` 实现同一接口，生产工厂统一装配。

## 生产装配

生产入口为 `server.main:create_production_app`（Uvicorn factory），上面的 `python -m server.main`
已使用该入口。`create_app()` 保留为显式依赖注入的应用构造函数，供测试使用。

Gmail 首次启动记录当前 historyId，随后每 10 秒检测新增的收件箱邮件；首次启动前的旧邮件
不会批量触发。跨进程游标保存在 `.data/gmail_sync.json`。邮件成功交给 Gateway 的持久化任务入口后
才推进游标，重复通知由 Gateway 去重。游标失效明确停止检测，health 显示原因，需要核对后重新建立
同步位置，不静默跳过缺口。常规检测错误保留游标，在下一轮重新查询。

新邮件轮次仅开放邮件读取工具。用户要求起草后，SDK 才可调用准备、读取及更新草稿工具；
更新使用当前已保存版本，直接编辑与 Agent 修改共用 MailDraftStore 的版本控制。草稿保存事件交给网页。
发送函数仅由 Confirmation 调用，执行结果回到确认任务的原 SDK 会话。

模型经应用进程内的 MCP 端点（server 名 `pebble`）调用工具，内置工具与本机设置关闭：每轮登记一个
一次性路径供 qodercli 子进程按回环地址连接，轮次结束即撤销。每轮独立启动一次 qodercli 子进程，
会话标识由 SDK 生成并按任务保存，重启后靠它接续。会话记录落在 `.data/agent/config/` 下，
历史接口直接读取该记录。

联合测试 `tests/gateway/test_integrated_mail.py` 覆盖实际模块衔接、Agent 修改与手动编辑、
旧版本拒绝、最终内容一致性、重复确认、结果会话关联与游标推进；`tests/gateway/test_agent_stream.py`
覆盖选项装配、事件映射、工具边界与会话历史读取。其中 SDK 模型响应和 Gmail 投递仍为测试边界替身；
真实验收结果需另行记录。
