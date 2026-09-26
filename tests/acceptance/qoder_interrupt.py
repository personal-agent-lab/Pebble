"""真实 Qoder 模型的终止验收（spec §4.1 的执行中终止）。

显式运行，不进入 pytest；使用临时实例目录，不碰 `.data`。真实 CN SDK 子进程经真实
GatewayRuntime 调度，工具走进程内的 MCP 端点。依次验证：

1. 一轮仍在进行时终止：请求真的送到会话（`interrupt_turn` 拿得到句柄并返回 True），
   调用在宽限期内记为中断，调度槽位随之释放。
2. 终止只停这一轮：用户消息与已流出的回答原样保留，时间线不补程序提示，也不记失败。
3. 没有产生操作记录的中断轮可以在原位置重试：复用同一条运行记录，跑完给出回答。
4. 重试走的是同一个 SDK 会话：终止没有把会话弄坏，后续轮次照常接续。

运行：`uv run --project server python -m tests.acceptance.qoder_interrupt`
"""

from __future__ import annotations

import asyncio
import json
import os
import socket
import tempfile
import time
from pathlib import Path

import uvicorn
from fastapi import FastAPI

from server.agent.client import QoderGateway
from server.agent.mcp import MCP_MOUNT_PATH, ToolServer
from server.agent.toolset import ToolDeps
from server.config import Settings, get_settings
from server.db import init_db
from server.gateway.runtime import INTERRUPT_GRACE_SECONDS, USER_INTERRUPTED_MESSAGE, GatewayRuntime
from server.memory.service import MemoryStore
from server.sessions.service import SessionStore
from server.sessions.timeline import TimelineStore
from server.tools.gmail.service import MailDraftStore
from server.tools.personal_kb.service import KbStore
from tests.support.gmail_double import MockGmailClient

# 验收固定的托管型号：不写 auto，避免 Auto 路由到当天额度用尽的型号。
MODEL = os.environ.get("PEBBLE_ACCEPTANCE_MODEL", "qmodel_38max")

# 四份资料各带一个只出现在正文里的编号：要核对就必须逐个读原文，
# 一轮里因此有多次工具调用，终止请求有足够的机会落在轮次中间。
CODES = ("NEBULA-3390", "ORBIT-77", "TIDE-5120", "PULSE-2048")
DOCUMENTS = [
    ("项目/星云/交付.md", "星云二期交付约定", f"二期交付标识是 {CODES[0]}，负责人周恺。"),
    ("项目/星云/里程碑.md", "星云二期里程碑", f"第一个里程碑在十月第二周，代号 {CODES[1]}。"),
    ("项目/潮汐/交付.md", "潮汐三期交付约定", f"三期交付标识是 {CODES[2]}，负责人林舟。"),
    ("项目/潮汐/复盘.md", "潮汐三期复盘", f"复盘结论写在 {CODES[3]} 号纪要里。"),
]
PROMPT = (
    "请按顺序做一件事：把资料库里这四份资料逐个读一遍——星云二期交付约定、"
    "星云二期里程碑、潮汐三期交付约定、潮汐三期复盘，每份都要单独调用一次工具读取原文，"
    "记下每份里的编号。四份都读完以后，用四行分别汇报每份的编号与负责人。"
)

# 终止请求发出后的等待上限：宽限期是给会话自己收尾的，超过它说明走到了直接取消。
INTERRUPT_TIMEOUT = INTERRUPT_GRACE_SECONDS + 10.0
# 等这一轮出现第一个工具条目：此刻会话已经建立，终止请求一定拿得到句柄。
STARTED_TIMEOUT = 180.0
TURN_TIMEOUT = 420.0


def available_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def check(condition: bool, message: str, evidence: object) -> None:
    if not condition:
        raise AssertionError(f"{message}：证据={evidence!r}")


class FixedCatalog:
    """固定型号目录：验收关心终止，装配期的账号目录是另一条链路，用固定值顶替。"""

    def __init__(self, model: str) -> None:
        self.model = model

    async def validate(self, model: str, *, new_task: bool = True) -> str:
        return model

    async def close(self) -> None:
        return None


class Instance:
    """临时实例：真实网关 + 真实 SDK + 真实资料库／记忆／数据库。"""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.database = root / "pebble.db"
        self.port = available_port()
        init_db(self.database)
        self.kb = KbStore(root)
        self.memory = MemoryStore(root)
        self.tasks = SessionStore(self.database)
        self.tool_server = ToolServer()
        settings = Settings(data_dir=root, tool_port=self.port)
        self.gateway = QoderGateway(
            ToolDeps(
                drafts=MailDraftStore(self.database),
                tasks=self.tasks,
                gmail=MockGmailClient(),
                kb_store=self.kb,
                memory_store=self.memory,
            ),
            self.tool_server,
            settings=settings,
        )
        self.runtime = GatewayRuntime(
            self.gateway,
            memory_store=self.memory,
            model_catalog=FixedCatalog(MODEL),
            path=self.database,
        )
        app = FastAPI()
        app.mount(MCP_MOUNT_PATH, self.tool_server)
        self.server = uvicorn.Server(
            uvicorn.Config(app, host="127.0.0.1", port=self.port, log_level="warning")
        )
        self.serving: asyncio.Task | None = None

    async def start(self) -> None:
        for path, title, body in DOCUMENTS:
            self.kb.save(title=title, body=f"## 约定\n{body}\n", path=path, summary=title)
        self.serving = asyncio.create_task(self.server.serve())
        while not self.server.started:
            if self.serving.done():
                await self.serving
            await asyncio.sleep(0.01)

    async def stop(self) -> None:
        await self.runtime.close()
        self.server.should_exit = True
        if self.serving is not None:
            await self.serving


def items_of(task_id: str) -> list[dict]:
    return TimelineStore().list_items(task_id)["items"]


def text_of(items: list[dict], role: str) -> str:
    return "\n".join(
        item["text"] for item in items if item["kind"] == "text" and item["role"] == role
    )


async def wait_for(predicate, timeout: float, describe: str):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        await asyncio.sleep(0.2)
    raise AssertionError(f"等待超时：{describe}")


async def scenario_interrupt(instance: Instance) -> dict:
    """场景 1：真实会话里的一轮在进行中被终止。"""

    runtime = instance.runtime
    # 终止请求是否真的送到会话：替身上只能看到调度层，这里要看到 SDK 句柄那一层。
    cooperative: dict[str, bool] = {}
    original = instance.gateway.interrupt_turn

    async def spy(run_id: str) -> bool:
        granted = await original(run_id)
        cooperative[run_id] = granted
        return granted

    instance.gateway.interrupt_turn = spy

    started = runtime.start_task(model=MODEL, message=PROMPT, attachments=[])
    task_id = started["task"]["task_id"]
    run_id = started["run"]["run_id"]

    # 等到模型真的在读资料：此刻 SDK 客户端已建立，句柄已登记。
    def tool_called() -> bool:
        row = runtime.get_run(run_id)
        check(
            row["status"] not in ("error",),
            f"这一轮在终止前就失败了：{row['error']}",
            row,
        )
        return any(item["kind"] == "tool" for item in items_of(task_id))

    await wait_for(tool_called, STARTED_TIMEOUT, "第一个工具调用")
    before = items_of(task_id)

    began = time.monotonic()
    settled = await asyncio.wait_for(runtime.interrupt_task(task_id), timeout=INTERRUPT_TIMEOUT)
    elapsed = time.monotonic() - began

    check(settled["status"] == "interrupted", "终止后调用状态不是中断", settled)
    check(settled["error"] == USER_INTERRUPTED_MESSAGE, "中断原因不是用户终止", settled)
    check(settled["finished_at"] is not None, "中断没有写下结束时间", settled)
    check(cooperative.get(run_id) is True, "终止请求没有送到会话（拿不到句柄）", cooperative)
    check(
        elapsed < INTERRUPT_GRACE_SECONDS,
        "会话没有在宽限期内自己收尾，退回了直接取消",
        {"elapsed": round(elapsed, 2), "grace": INTERRUPT_GRACE_SECONDS},
    )
    check(task_id not in runtime._active, "终止后调度槽位没有释放", list(runtime._active))

    after = items_of(task_id)
    kinds = [(item["kind"], item.get("role")) for item in after]
    check(
        all(kind != "notice" for kind, _ in kinds),
        "终止补了程序提示，与约定不符",
        kinds,
    )
    check(all(kind != "error" for kind, _ in kinds), "终止被记成失败轮", kinds)
    check(
        [item["item_id"] for item in after if item["kind"] != "tool"]
        == [item["item_id"] for item in before if item["kind"] != "tool"],
        "终止改动了用户消息或已经流出的回答",
        {"before": before, "after": after},
    )
    check(after[0].get("text") == PROMPT, "用户消息没有保留在时间线第一位", after[0])

    latest = runtime.latest_run(task_id)
    check(latest["retryable"] is True, "未产生操作记录的中断轮不可重试", latest)
    check(latest["activity"] is None, "终止后还留着当前步骤说明", latest)

    return {
        "task_id": task_id,
        "run_id": run_id,
        "status": settled["status"],
        "error": settled["error"],
        "cooperative": cooperative.get(run_id),
        "interrupt_seconds": round(elapsed, 2),
        "tool_calls_before_interrupt": sum(1 for item in before if item["kind"] == "tool"),
        "assistant_chars": len(text_of(after, "assistant")),
        "kinds": [kind for kind, _ in kinds],
    }


async def scenario_retry(instance: Instance, task_id: str, run_id: str) -> dict:
    """场景 2：中断轮在原位置重试，复用同一条运行记录并与会话接续。"""

    runtime = instance.runtime
    session_before = instance.tasks.get_task(task_id)["sdk_session_id"]
    users_before = [item.get("text") for item in items_of(task_id) if item.get("role") == "user"]

    runtime.retry_last_message(task_id)
    deadline = time.monotonic() + TURN_TIMEOUT
    while time.monotonic() < deadline:
        row = runtime.get_run(run_id)
        if row["status"] in ("done", "error", "interrupted"):
            break
        await asyncio.sleep(0.5)
    row = runtime.get_run(run_id)

    check(row["status"] == "done", "重试没有跑完", row)
    check(row["run_id"] == run_id, "重试新建了运行记录", row)
    check(row["error"] is None, "重试成功却留着错误说明", row)

    items = items_of(task_id)
    check(
        [item.get("text") for item in items if item.get("role") == "user"] == users_before,
        "重试改了用户消息",
        items,
    )
    answer = text_of(items, "assistant")
    check(answer.strip() != "", "重试跑完却没有回答", items)
    session_after = instance.tasks.get_task(task_id)["sdk_session_id"]
    check(
        session_after == session_before and session_after is not None,
        "重试没有接续中断前建立的会话",
        {"before": session_before, "after": session_after},
    )
    return {
        "status": row["status"],
        "answer_chars": len(answer),
        "tool_calls": sum(1 for item in items if item["kind"] == "tool"),
        # 读到的编号：终止与重试都不该让回答丢掉已经核对过的内容。
        "codes_in_answer": [code for code in CODES if code in answer],
        "session": session_after,
    }


async def verify(root: Path) -> dict:
    if get_settings().qoder_token is None:
        raise RuntimeError("未配置 QODERCN_PERSONAL_ACCESS_TOKEN")
    instance = Instance(root)
    await instance.start()
    try:
        interrupted = await scenario_interrupt(instance)
        retried = await scenario_retry(instance, interrupted["task_id"], interrupted["run_id"])
        return {"interrupt": interrupted, "retry": retried}
    finally:
        await instance.stop()


def main() -> None:
    with tempfile.TemporaryDirectory(prefix="pebble-qoder-interrupt-") as directory:
        # 会话助手与工具轨迹按环境变量找实例目录：先指到临时目录，别写进真实 `.data`。
        os.environ["PEBBLE_DATA_DIR"] = directory
        os.environ.setdefault("PEBBLE_QODER_MODEL", MODEL)
        get_settings.cache_clear()
        report = asyncio.run(verify(Path(directory)))
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
