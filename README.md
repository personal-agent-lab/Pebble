# Pebble

持续运行的个人 Agent。一个常驻 Python 服务，由单个 Agent 按用户目标组合工具（Gmail、iCloud Calendar、个人资料库）完成真实事务，
程序负责保证确认、持久化与去重。单用户单实例部署，PC 和手机浏览器都能发起任务、编辑草稿和确认操作；任务不依赖浏览器页面保持开启。

当前状态：任务、草稿版本、确认执行与 Gateway 后端已实现，SQLite schema 为 3。
支持新邮件内部入口、任务及历史查询、消息提交、SSE、草稿编辑、确认与结果查询。
Agent、邮件校验和发送通过显式参数接入；真实 SDK/Gmail 尚未接入，相关写请求返回 503，
不会默认装配测试替身。网页仍为健康检查页面，尚无聊天和确认界面。

## 文档

- `docs/v1-spec.md`：需求范围、产品行为、验收标准。内容冲突时以此为准。
- `docs/v1-design.md`：组件划分、交付阶段、验证要求、协作分工。
- `docs/v1-mail-flow-contract.md`：第一条邮件链的接口约定，未定稿。

设计文档第 2 节的组件表是目标结构，`server/sessions/`、`server/tools/gmail/` 与 `server/approval/`
已实现本地存储；HTTP 接口位于 `server/api/`，A 的调用管理位于 `server/gateway/`，调用记录位于 `server/sessions/`，
`server/agent/` 保留给 B 的 SDK 装配，当前不含实现文件。

## 前置依赖

- Python 3.13（由 `server/.python-version` 固定，uv 会自动准备）
- [uv](https://docs.astral.sh/uv/)
- Node.js 与 npm（Vite 8 要求 20.19.0+ 或 22.12.0+，已用 v24.18.0 验证）

## 配置

所有配置项见仓库根 `.env.example`，复制为 `.env` 后按需修改：

```bash
cp .env.example .env
```

`.env` 不进 Git。骨架阶段只有实例数据目录、监听地址和前端代理目标三个键；
SDK 模型与 Gmail 凭证的配置待第一阶段验证有实测结果后再补。

不创建 `.env` 也能启动，此时使用代码内默认值（数据目录为 `<仓库根>/.data`，监听 `127.0.0.1:8000`）。

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

## 验证

浏览器打开 `http://127.0.0.1:5173`，页面应显示后端返回的状态、实例数据目录、
schema 版本与 journal mode（应为 `wal`）。后端不可达时页面显示失败原因。

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
`server.tools.gmail.service.ReplyDraftStore` 和 `server.approval.service.ConfirmationService`。
三者默认使用实例数据库，也可显式传入 `path=Path(...)`。`ReplyDraftStore` 必须传入 B 的同步
`validate_reply_draft` 函数，`ConfirmationService` 必须传入 B 的同步发送函数；生产代码没有
默认放行校验器或默认成功的发送函数。输入输出字段及错误含义见
[邮件接口字段契约](docs/v1-mail-flow-contract.md)。

任务保存用户目标与 SDK 会话关联；操作管理版本与状态；邮件字段和原邮件去重留在邮件能力内。
跨任务复用同一操作后，各任务看到相同的最新草稿与状态，SDK 会话仍独立。

### 确认发送

- `confirm_reply(task_id, operation_id, version)`：检查版本、取得执行权、调用发送函数并保存结果；
  重复确认返回已有状态，不再次发送。
- `get_execution(operation_id)`：操作当前状态、确认信息及已保存结果。
- `get_agent_result(operation_id)`：契约第 7 节的回传数据；尚无结果或回传任务未关联会话时返回 `None`。
- `recover_interrupted_executions()`：把上次进程遗留的 `sending` 置为 `unknown`；`server/main.py`
  在 `init_db()` 之后、接受请求之前调用，数据库初始化本身不执行该恢复。

发送函数输入输出见契约第 6 节；异常、中断及不符契约的返回都记 `unknown`，不自动重试，
`unknown` 结果不会阻止后续核实，但重复确认不会重新发送。结果核实由 B 后续接入，
本步骤不提供重发入口，也不提供任意修改操作状态的接口。

### 第二步验收记录（2026-09-10）

- `uv run --project server pytest -c server/pyproject.toml`：20 项通过。
- `uv run --project server ruff check --config server/pyproject.toml server tests`：通过。
- 真实 SQLite：跨进程恢复、并发编辑与准备、历史版本、跨任务关联、失败回滚、
  不可编辑状态、schema 0 到 1 原子升级和重复初始化均已验证。
- `tests/storage/test_store.py::test_cross_process`：写入进程退出后，新进程打开同一数据库，
  比较完整任务、会话关联、草稿及操作列表；测试只写临时目录。
- B 校验接口：仅测试替身验证通过，包括拒绝保存及防止校验器改写收件人。
  真实邮件规则、SDK 会话恢复、网页和确认发送不属于此次通过范围。

### 第三步验收记录（2026-09-12）

- `uv run --project server pytest -c server/pyproject.toml`：43 项通过。
- `uv run --project server ruff check --config server/pyproject.toml server tests`：通过。
- 真实 SQLite 已通过：确认记录与结果落盘、新进程读取一致、并发重复确认只有一份执行记录且
  发送调用为 1 次、编辑与确认竞争只出现合法结果、提交前失败整体回滚、
  schema 1 到 2 升级与重复初始化保留任务、草稿和历史版本、执行记录唯一约束有效。
- 发送替身已通过：多轮修改后只发送最终确认版本且中文、空白、换行与收件人顺序逐字段一致；
  确认旧版本返回版本冲突且零调用；`sent`、`failed`、`unknown` 三类结果正确保存并阻止重复确认；
  调用异常与不符契约返回记待核实；发送后结果保存失败向调用方报错并阻止重发；
  执行中进程退出后重启置为待核实且不再次调用发送函数；共享操作的回传任务取首次确认任务。
- 真实 SQLite 与发送替身共同验证了 `get_agent_result` 在会话未关联时返回 `None`、关联后可读取，
  以及 `recover_interrupted_executions` 在服务启动流程中先于请求执行。
- 待联合验证：真实 Gmail 发送（B 的发送函数）与 Agent 结果回传（SDK 会话接入后读取
  `get_agent_result`），网页确认入口不在本次范围。

## Gateway 后端验收（2026-09-12）

A 的 `server/gateway/runtime.py` 管理应用生命周期内的异步任务和事件订阅，API 将事件编码为 SSE，
同目录的 `agent_contract.py` 定义 B 的调用接口；`server/sessions/runs.py` 保存调用记录。
没有独立工作线程、额外事件循环或自建 Agent 循环。同步发送使用线程池。
`tests/support/agent_double.py` 和 `tests/support/backend_fixture.py` 仅用于测试，不进入默认应用装配。

本次检查：67 项 pytest 通过，Ruff 检查通过；保留一条上游 Starlette 弃用提示。

运行完整后端链路验收：

```bash
uv run --project server pytest -c server/pyproject.toml tests/api/test_http_flow.py -v
uv run --project server pytest -c server/pyproject.toml
uv run --project server ruff check --config server/pyproject.toml server tests
```

`tests/api/test_http_flow.py` 启动独立 Uvicorn 进程并访问真实 HTTP/SSE：自动新邮件摘要、用户要求
准备草稿、多轮修改、旧版本拒绝、最终确认、参数逐字段比较、重复确认、结果回传及进程重启。
SSE 收到事件后主动断开，后端仍完成工作。数据库与发送参数日志均保存在独立临时目录。
其他测试覆盖并发、回滚、三类发送结果、异常格式、中断恢复和回传失败。

真实 SQLite、HTTP/SSE 与后端业务服务属于真实验收；摘要、草稿生成、校验及 Gmail 发送
使用替身。历史对话由 B 接口读取，当前替身仅存内存，不能据此宣称真实 SDK 历史跨进程恢复通过。
网页操作、真实邮箱投递与真实 Agent 判断仍待接入。

供 B 装配的入口为 `create_app(gateway=..., validate_reply_draft=..., send_reply=...)`。
后台邮件检测在应用事件循环中调用 `app.state.agent.accept_new_mail(source_message_id, thread_id)`；
每封新邮件一个任务，同一邮件重复检测不重复启动，同线程不同邮件创建不同任务。

可手动启动测试后端（仅绑定本机；不会真实发送邮件）：

```bash
export PEBBLE_DATA_DIR="$(mktemp -d)"
uv run --project server uvicorn tests.support.backend_fixture:app --host 127.0.0.1 --port 8001
```

访问 `http://127.0.0.1:8001/docs`，先读取 `/api/tasks` 得到自动生成的任务；提交消息、读取操作、
编辑草稿并确认后，查看执行结果及 history。发送替身实际收到的参数保存在上述临时目录的
`sent.jsonl`，重复确认不增加行数。此入口仅用于人工验收，不是生产启动方式。

## 带终端日志的七步邮件演示

从仓库根运行：

```bash
uv run --project server python -m tests.mail_acceptance
```

按业务流程自动跑一遍：收到新邮件 → Agent 摘要和建议 → 用户要求准备回信 →
生成并保存草稿 → 自动补齐修改意见和直接编辑 → 模拟点击确认发送 → 展示执行结果及 Agent 后续回复。
所有输入自动补齐，无需终端交互；B 使用固定邮件、建议、草稿及发送替身，不调用真实 Gmail。
A 使用真实 HTTP、SQLite、任务会话、版本和确认执行服务，终端代替网页展示输出。

脚本不调用 pytest，不包含断言，不输出 PASS，也不额外跑异常或重启场景。
终端按七步输出时间、输入、任务/操作 ID、完整草稿及版本、确认响应、发送参数和最终会话。
开头打印资料目录，其中保留 `pebble.db`、`sent.jsonl` 和 `backend.log`。
脚本自动启动并关闭本机测试后端；网络或执行错误会打印中断原因并返回非零退出码。

原有自动化断言仍保留在测试套件中，独立运行：

```bash
uv run --project server pytest -c server/pyproject.toml
```
