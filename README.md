# Pebble

持续运行的个人 Agent。一个常驻 Python 服务，由单个 Agent 按用户目标组合工具（Gmail、iCloud Calendar、个人资料库）完成真实事务，
程序负责保证确认、持久化与去重。单用户单实例部署，PC 和手机浏览器都能发起任务、编辑草稿和确认操作；任务不依赖浏览器页面保持开启。

当前状态：任务、会话关联、共享操作及邮件草稿版本持久化已实现，SQLite schema 为 1。
后端仍只开放健康检查 HTTP 接口，前端显示连通状态；草稿目前通过 Python 服务接口操作。
确认发送、SDK 和 Gmail 真实业务校验尚未接入。

## 文档

- `docs/v1-spec.md`：需求范围、产品行为、验收标准。内容冲突时以此为准。
- `docs/v1-design.md`：组件划分、交付阶段、验证要求、协作分工。
- `docs/v1-mail-flow-contract.md`：第一条邮件链的接口约定，未定稿。

设计文档第 2 节的组件表是目标结构，`server/sessions/` 和 `server/tools/gmail/` 已实现本地存储；
`server/agent/`、`server/approval/`、`server/memory/` 等目录尚未创建。

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

## 本地任务与草稿服务

初始化数据库后使用 `server.sessions.service.SessionStore` 和
`server.tools.gmail.service.ReplyDraftStore`。两者默认使用实例数据库，也可显式传入
`path=Path(...)`。`ReplyDraftStore` 必须传入 B 的同步 `validate_reply_draft` 函数；
生产代码没有默认放行校验器。输入输出字段及错误含义见
[邮件接口字段契约](docs/v1-mail-flow-contract.md)。

任务保存用户目标与 SDK 会话关联；操作管理版本与状态；邮件字段和原邮件去重留在邮件能力内。
跨任务复用同一操作后，各任务看到相同的最新草稿与状态，SDK 会话仍独立。
本阶段没有发送入口，也不提供任意改状态的方法。

### 第二步验收记录（2026-09-10）

- `uv run --project server pytest -c server/pyproject.toml`：20 项通过。
- `uv run --project server ruff check --config server/pyproject.toml server tests`：通过。
- 真实 SQLite：跨进程恢复、并发编辑与准备、历史版本、跨任务关联、失败回滚、
  不可编辑状态、schema 0 到 1 原子升级和重复初始化均已验证。
- `tests/test_store.py::test_cross_process`：写入进程退出后，新进程打开同一数据库，
  比较完整任务、会话关联、草稿及操作列表；测试只写临时目录。
- B 校验接口：仅测试替身验证通过，包括拒绝保存及防止校验器改写收件人。
  真实邮件规则、SDK 会话恢复、网页和确认发送不属于此次通过范围。
