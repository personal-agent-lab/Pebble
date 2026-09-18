# Pebble

对话优先的个人助手 Agent。你可以随时和它对话——聊天、提问、交办事项；它按你的目标自主组合工具
（Gmail、iCloud Calendar、个人知识库，后续更多）完成真实事务，并在会话之间记住你的偏好（Memory）、
复用沉淀下来的流程（Skills）。程序负责保证外部写操作的授权、持久化与去重。

单用户单实例部署，服务常驻。PC 与手机浏览器共用同一套响应式界面：发起任务、查看进度、编辑草稿、
确认操作；任务不依赖始终开启的浏览器页面。新邮件是首版的系统触发源，收到即自动开始处理，
但邮件只是它能做的事之一。

已接通对话与任务、Gmail、iCloud Calendar、长期记忆、历史对话检索与个人资料库；主题页、Skills、
认证与 HTTPS 远程访问尚未实现，目前只能本机和同局域网访问。完整的实现状态与已知偏差见
`docs/status.md`。真实账号验收仍在进行，本地测试通过不代表真实邮件或日程操作成功。

## 文档

- `docs/v1-spec.md`：需求范围、产品行为、验收标准。内容冲突时以此为准。
- `docs/v1-design.md`：组件划分、交付阶段、验证要求与实现要点。
- `docs/status.md`：实现状态与已知偏差，唯一的状态记录。
- `docs/memory-spec.md`：记忆功能规格：短期上下文、长期记忆、常驻上下文与按需检索、历史检索。
- `docs/kb-spec.md`：个人资料库功能规格：保存与主动保存、检索作答、删除与恢复、主题页。
- `docs/contracts/mail.md`：Gmail 工具、同步触发、确认发送与核实的字段与语义。
- `docs/contracts/calendar.md`：iCloud Calendar 工具、直连创建与冲突处理的字段与语义。
- `docs/contracts/memory.md`：长期记忆与历史检索的文件格式、工具与接口。
- `docs/contracts/skills.md`：Skills 的文件格式、工具与加载边界。
- `docs/contracts/personal-kb.md`：个人知识库的工具、存储与引用语义。

设计文档第 2 节的组件表与代码结构是目标结构。

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
| `PEBBLE_QODER_MODEL` | 新任务默认选中的型号标识（如 `qmodel_38max`），取值见 `GET /api/models` 的 `id`；未配置时默认 `auto` |
| `PEBBLE_LIGHT_MODEL_PROVIDER`、`PEBBLE_LIGHT_MODEL`、`PEBBLE_LIGHT_MODEL_API_KEY`、`PEBBLE_LIGHT_MODEL_BASE_URL` | 轻量模型（自有 API Key），只用于任务标题、资料说明与资料图片说明生成；图片说明需要所配模型支持图片输入。供应商、型号、密钥必须同时给全，`BASE_URL` 可选；provider 与型号取自账号的 BYOK 目录（如 `deepseek` / `deepseek-flash-pg`）。都不配时沿用 `PEBBLE_QODER_MODEL` |
| `PEBBLE_GMAIL_CREDENTIALS_PATH` | Gmail OAuth 桌面应用 JSON，默认 `<data_dir>/credentials.json` |
| `PEBBLE_GMAIL_TOKEN_PATH` | Gmail 授权结果，默认 `<data_dir>/gmail_token.json` |
| `PEBBLE_ICLOUD_ACCOUNT` | Apple ID 账号，仅在服务端使用 |
| `PEBBLE_ICLOUD_PASSWORD_PATH` | Apple App 专用密码文件的绝对路径 |
| `PEBBLE_ICLOUD_CALENDAR_URL` | 唯一主日历的完整 iCloud CalDAV collection URL |
| `PEBBLE_BACKEND_URL` | 只给 Vite 开发代理使用，后端不读 |

Gmail 首次启动在浏览器授权读取和发送权限。Key 仅交给 SDK 的模型配置，不进入系统提示或工具结果。
Qoder CN 与国际版的 SDK、Token 和配置目录不能混用。

生产运行只要求 Qoder CN 模型配置。Gmail 与 iCloud Calendar 是可选服务：没配凭证时服务照常启动，
对话、记忆与个人资料库都能用，只是该服务的工具、新邮件检测与确认执行一并关闭（草稿工具也不交给
模型，免得起草出发不出去的邮件）；启动日志会说明哪项未接入，`/api/health` 的 `services` 也会列出
`unconfigured` 与原因。补齐凭证后重启即可启用。仅测试使用不装配真实依赖的 `create_app()`。

## 启动

以下命令都在仓库根目录执行。后端与前端是两个常驻进程，分别开终端运行。

后端：

```bash
uv sync --project server
uv run --project server python -m server.main
```

服务监听 `http://127.0.0.1:8000`，带热重载，使用生产入口 `server.main:create_production_app`
（Uvicorn factory）。

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

## 检查

浏览器打开 `http://127.0.0.1:5173`，应看到任务列表（PC 在左侧侧栏）。后端不可达时页面显示
「无法连接服务」并提供重试，不白屏。也可以直接请求接口：

```bash
curl http://127.0.0.1:8000/api/health
curl http://127.0.0.1:8000/api/models
```

返回里的 `services.gmail` 与 `services.calendar` 为 `ok` 表示已接入，`unconfigured` 表示缺凭证、相关功能已关闭。

## 数据位置

以下都在实例数据目录 `<PEBBLE_DATA_DIR>`（默认 `.data/`）下，可以直接查看：

| 路径 | 内容 |
| --- | --- |
| `pebble.db` | 任务、时间线、草稿、确认与执行状态（SQLite） |
| `memory/USER.md`、`memory/MEMORY.md` | 长期记忆，可直接编辑，下一轮生效；上限 1375 / 2200 字符 |
| `kb/` | 个人资料（Markdown），直接编辑、新增、移动、删除都会在下一次资料库操作前自动纳入版本 |
| `kb-index.sqlite3` | 资料检索索引，派生数据，可随时重建 |
| `agent/` | SDK 会话记录、模型目录缓存与每个任务的工作目录（含上传附件） |
| `gmail_sync.json` | 新邮件检测的游标 |

`kb/` 属于数据目录内的独立本地 Git 仓库，每次写入即一次提交，可用 Git 查看历史与差异；数据库、凭证、
SDK 会话与 `memory/` 不进入该仓库。

## 测试

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

自动化测试替换 SDK 子进程、Gmail 投递和 iCloud CalDAV 这三个外部边界，其余模块、SQLite、HTTP 都是真的；
测试通过不代表真实外部操作成功。

`tests/acceptance/` 下是使用真实 Qoder 模型的验收脚本，不随 pytest 运行，会消耗真实额度。每个脚本
单独运行，例如：

```bash
uv run --project server python -m tests.acceptance.qoder_memory
```

`qoder_context` 加 `--compact` 会发送较长的合成文本验证上下文压缩，消耗更多额度。
