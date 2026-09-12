# Pebble 

持续运行的个人 Agent。需求见 `docs/v1-spec.md`，组件划分与执行约束见 `docs/v1-design.md`。

当前状态：最小骨架。后端提供健康检查，前端显示连通状态，SQLite 只建了 schema 版本表；
任务、草稿、确认记录等业务表待接口与状态含义确定后再加。
 
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

以下命令都在仓库根目录执行。

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

## 目录

```text
web/        前端：React + TypeScript + Vite
server/     后端：FastAPI、配置、SQLite
tests/      后端测试
docs/       规格与设计
```

## 下一步

第一条邮件完整链：聊天提交与 SSE、草稿编辑与确认、SDK 装配与 Gmail 工具。
数据库业务表随该链路的接口约定一起建，不提前铺空目录。
