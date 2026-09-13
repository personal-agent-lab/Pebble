# Pebble

持续运行的个人 Agent。一个常驻 Python 服务，由单个 Agent 按用户目标组合工具（Gmail、iCloud Calendar、个人资料库）完成真实事务，
程序负责保证确认、持久化与去重。单用户单实例部署，PC 和手机浏览器都能发起任务、编辑草稿和确认操作；任务不依赖浏览器页面保持开启。

当前状态：任务、草稿版本、确认执行与 Gateway 后端已实现，SQLite schema 为 3。
支持新邮件内部入口、任务及历史查询、消息提交、SSE、草稿编辑、确认与结果查询。
Agent、邮件校验和发送通过显式参数接入；真实 SDK/Gmail 尚未接入，相关写请求返回 503，
不会默认装配测试替身。网页已实现任务列表、任务对话、草稿编辑与确认、逐项执行结果四个界面，
PC 与手机共用同一套页面组件；规则 Memory、Skill 审阅与个人资料 KB 尚无服务端接口，
导航入口置灰标注未接入。

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

## 网页

`web/` 是 TypeScript + React + Vite 单页应用，路由为 `/tasks`（任务列表）、
`/tasks/:taskId`（对话、待确认内容与逐项执行结果）、`/tasks/:taskId/confirm`（草稿编辑与确认）。
视觉沿用 `qwenwork/` 下的设计稿与设计系统 token（`src/styles/tokens.css`）。
断点 900px：以上为侧栏布局，以下折叠为底部 tab，两端功能一致。

任务列表没有列表级事件流，按 5 秒轮询刷新（页面不可见时暂停），新邮件自动触发的任务无需手动刷新；
确认后的发送在后台执行，事件流不携带执行状态，页面对执行结果按 1.5 秒轮询直到状态离开 `sending`。
界面只呈现接口能支撑的内容：设计稿中的来源引用面板、日程预览卡与搜索框对应的接口尚未提供，暂不渲染。

## 验证

浏览器打开 `http://127.0.0.1:5173`，应看到任务列表。后端不可达时页面显示「无法连接服务」
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

## 网页界面验收（2026-09-12）

`npm run typecheck` 与 `npm run build` 通过。以下用 `tests/support/backend_fixture:app` 替身后端
（真实 HTTP、SQLite、确认执行，发送为替身，不会真实发信），在浏览器 1280×860 与 375×812
两个视口手动走完，两端行为一致：

- 替身启动时自动投递的新邮件任务出现在列表；提交消息后 SSE 出回复文本，
  `draft_saved` 后出现待确认卡，侧栏与顶栏的待确认计数同步。
- 确认页编辑正文并保存为新版本，版本号由 v1 递增到 v2，版本历史标出当前版本。
- 另一路把草稿改到 v3、v4 后，页面上以旧版本确认被拒：提示「你确认或编辑依据的是 vN，
  当前内容已是 vM」，内容刷新到最新版，`sent.jsonl` 未增加行，即未产生发送调用。
- 确认当前版本后状态转为 sent，确认记录显示确认版本、时间与邮件 ID；
  发送参数与所确认版本逐字段一致；重复确认返回已有状态，`sent.jsonl` 行数不变。
- 执行结果分区按「N / M」如实汇总，逐项展示；`PEBBLE_TEST_SEND_STATUS=failed` 显示失败原因并
  说明不自动重试，`=unknown` 显示待核实且页面上不存在任何重发入口。
- 结果回传后 Agent 的后续回复出现在同一对话中。
- 确认前刷新页面，草稿与待确认卡原样恢复。
- 停掉后端刷新页面显示「无法连接服务」与重试按钮，不白屏；后端恢复后点重试即回到列表。

未覆盖：真实 SDK 历史、真实 Gmail 发送、真机浏览器与认证部署；`sending` 中间态因替身同步返回
过快未单独截取。前端尚无自动化测试，按 `docs/v1-design.md` §9 的视口端到端测试待接口稳定后补。

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
这个后端的邮件与对话是固定脚本，只够接口验证；在网页上按业务顺序手动走完整流程见
「手动跑通完整邮件流程」。

## 手动跑通完整邮件流程

在网页上按业务顺序走一遍七步：收到新邮件 → 摘要和建议 → 要求准备回信 → 生成草稿 →
多轮修改与直接编辑 → 确认发送 → 展示结果并交回 Agent。需要三个东西：替身后端、前端开发
服务器，以及投递邮件的命令。任务、草稿版本、确认执行、SSE 与 SQLite 都是真的；邮件内容、
摘要建议、草稿改写和 Gmail 发送是替身，不会真实发信。

后端（仓库根执行；数据目录另给一个，避免和 `.data` 混在一起）：

```bash
PYTHONPATH=. PEBBLE_DATA_DIR=/tmp/pebble-manual PEBBLE_TEST_SEND_DELAY=2 uv run --project server uvicorn tests.support.manual_backend:app --host 127.0.0.1 --port 8000
```

前端按「启动」一节运行 `npm run dev`；端口 8000 与 Vite 默认代理目标一致，页面在
`http://127.0.0.1:5173`。

投递邮件（另开终端，`PEBBLE_DATA_DIR` 与后端一致）：

```bash
PYTHONPATH=. PEBBLE_DATA_DIR=/tmp/pebble-manual uv run --project server python -m tests.mail_inbox list
```

```bash
PYTHONPATH=. PEBBLE_DATA_DIR=/tmp/pebble-manual uv run --project server python -m tests.mail_inbox deliver invite
```

现写一封：

```bash
PYTHONPATH=. PEBBLE_DATA_DIR=/tmp/pebble-manual uv run --project server python -m tests.mail_inbox compose --from lawyer@example.com --subject "合同条款确认" --body "你好，\n\n第 7 条的付款周期希望改成 30 天，能接受吗？"
```

`compose` 只有 `--from`、`--subject`、`--body` 必填（正文里的 `\n` 当换行），其余可选：
`--thread` 给已有线程即为同线程的另一封邮件，`--id` 复用同一个 ID 用来验证去重，
`--summary`、`--suggestion`、`--reply-body` 指定替身的摘要、建议和起草正文；不给就按正文生成。

邮件就是收件箱目录（默认 `<数据目录>/inbox`，可用 `PEBBLE_TEST_INBOX_DIR` 指定）里的 json，
所以手写一个文件放进去、或用编辑器改一改再存，效果和上面的命令一样；`deliver` 也接受 json 路径，
样例在 `tests/support/mails/`，`clear` 清空收件箱。后台每秒扫描一次，邮件 ID 决定去重：
重复投递同一个 ID 不重复建任务，重启后重新扫描也不会重复。

替身 Agent 按关键词判断意图：普通提问只回答，出现「回信」「草稿」「改」「正式」「链接」
这类词才动草稿，所以「让它准备草稿」和「确认发送」始终是两件事。草稿保存、版本、
版本冲突和发送都走服务端真实接口。可调环境变量：`PEBBLE_TEST_AGENT_DELAY` 每段文本间隔
（默认 0.4 秒），`PEBBLE_TEST_SEND_DELAY` 发送耗时（默认 0；设成 2 能在页面上看到 sending
中间态），`PEBBLE_TEST_SEND_STATUS` 取 `sent`、`failed` 或 `unknown`。

替身的记录都在数据目录：`sent.jsonl` 是发送替身实际收到的参数，`mock_agent_history.jsonl`
是会话历史（重启后仍在），`mock_agent_mails.json` 是任务与邮件的对应关系。会话标识按任务生成，
重启后新任务不会捡到旧任务的历史。

新邮件来源通过 `create_app(mail_source=...)` 装配，接口是 `server/gateway/mail_source.py` 的
`MailSource`（`start` / `stop`）。真实 Gmail 检测由 B 实现同一接口；默认装配没有邮件来源，
不伪造邮件。`tests/support/` 下的模拟邮箱与替身 Agent 只用于测试和人工验收。

### 手动验收记录（2026-09-12）

浏览器 1280×860 走完上述七步：投递 `invite` 后任务自动出现；对话里先给摘要和建议且没有
自建草稿；问一句「这封邮件说了什么」只得到回答，操作数仍为 0；说「帮我写一封回信」后出现
v1 草稿与待确认卡；「再问一下会议链接」改出 v2；确认页手动编辑保存为 v3；确认发送时出现
`sending`（`PEBBLE_TEST_SEND_DELAY=2`），随后转 `sent` 并显示邮件 ID；`sent.jsonl` 只有一行，
收件人、主题、正文与 v3 逐字段一致；执行结果分区按「1 / 1」汇总，Agent 在同一对话里给出
后续回复；已发送后再要求修改被明确拒绝。重复投递同一封邮件任务数不变；投递第二封生成
独立任务和独立草稿；重启后端后任务不重复、对话历史仍在。

另外验证了 `compose` 现写的邮件：没有摘要字段时按正文首句摘要（跳过称呼），起草的回信按
真实发件人和主题生成。

自动化覆盖同一套装配的是 `tests/api/test_manual_flow.py`：七步、两封邮件各自独立、重启后
历史不串、现写邮件无摘要字段。

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
