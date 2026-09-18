"""真实 Qoder 模型的长期记忆验收：每轮判断的写入、分区、静默行为和新会话读取。

显式运行，不进入 pytest；使用临时实例目录，不碰 `.data`。依次验证：
偏好与学习方向写入“关于你”，外部约定写入“事实与约定”，一次性要求不写入，修改与忘记生效，
歧义信息不写入，全新 SDK 会话按记忆作答。
"""

from __future__ import annotations

import asyncio
import json
import socket
import tempfile
from pathlib import Path

import uvicorn
from fastapi import FastAPI

from server.agent.client import QoderGateway
from server.agent.mcp import MCP_MOUNT_PATH, ToolServer
from server.agent.toolset import ToolDeps, TurnKind
from server.config import Settings
from server.db import init_db
from server.gateway.agent_contract import Turn
from server.memory.judge import JUDGE_INSTRUCTIONS, build_judge_message
from server.memory.service import MemoryStore
from server.sessions.service import SessionStore
from server.tools.gmail.service import MailDraftStore
from tests.support.gmail_double import MockGmailClient


def available_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


async def judge(
    gateway: QoderGateway,
    store: MemoryStore,
    task_id: str,
    message: str,
    transcript: str = "",
) -> list[dict]:
    """跑一轮真实的记忆判断，返回实际工具记录。"""
    prompt = build_judge_message(message, transcript, store.snapshot())
    return await gateway.judge_memory(task_id, JUDGE_INSTRUCTIONS, prompt)


async def ask(gateway: QoderGateway, task_id: str, prompt: str) -> str:
    """在全新 SDK 会话里问一句，返回回答文本。"""
    texts = []
    async for event in gateway.stream_turn(
        Turn(kind=TurnKind.MESSAGE, task_id=task_id, sdk_session_id=None, message=prompt)
    ):
        if event["type"] == "text":
            texts.append(event["text"])
        elif event["type"] == "error":
            raise RuntimeError(event["message"])
    return "".join(texts).strip()


def check(condition: bool, message: str, store: MemoryStore, records: list[dict]) -> None:
    if not condition:
        snapshot = {target: section["content"] for target, section in store.snapshot().items()}
        raise AssertionError(f"{message}：记忆={snapshot!r} 工具记录={records!r}")


async def verify(root: Path) -> dict:
    if Settings().qoder_token is None:
        raise RuntimeError("未配置 QODERCN_PERSONAL_ACCESS_TOKEN")
    port = available_port()
    database = root / "pebble.db"
    init_db(database)
    store = MemoryStore(root)
    tasks = SessionStore(database)
    task_id = tasks.create_task("真实记忆验收")["task_id"]
    tool_server = ToolServer()
    gateway = QoderGateway(
        ToolDeps(
            drafts=MailDraftStore(database),
            tasks=tasks,
            gmail=MockGmailClient(),
            memory_store=store,
        ),
        tool_server,
        settings=Settings(data_dir=root, tool_port=port),
    )
    app = FastAPI()
    app.mount(MCP_MOUNT_PATH, tool_server)
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    serving = asyncio.create_task(server.serve())
    while not server.started:
        if serving.done():
            await serving
        await asyncio.sleep(0.01)

    report: dict[str, object] = {}
    try:

        def memory(target: str) -> str:
            return store.snapshot()[target]["content"]

        records = await judge(
            gateway, store, task_id, "以后回答我的问题时，请先给结论，再解释原因。"
        )
        check("结论" in memory("user"), "偏好没有写入“关于你”", store, records)
        check("结论" not in memory("memory"), "偏好被写进了“事实与约定”", store, records)
        report["preference"] = records

        # 不是“以后请……”式的要求，只是陈述长期在做的事，也应当轮保存。
        records = await judge(
            gateway,
            store,
            task_id,
            "我最近正在学习 Hermes Agent（一个开源个人助理项目）的设计，之后会经常问你相关的问题。",
        )
        check(memory("user").count("Hermes") == 1, "学习方向没有写入“关于你”", store, records)
        report["learning"] = records

        records = await judge(gateway, store, task_id, "我们团队的内部会议默认都是 30 分钟。")
        check("30" in memory("memory"), "外部约定没有写入“事实与约定”", store, records)
        check("30" not in memory("user"), "外部约定被写进了“关于你”", store, records)
        report["convention"] = records

        before = store.snapshot()
        records = await judge(gateway, store, task_id, "这次用英文回答我就行。")
        check(store.snapshot() == before, "一次性要求被写入了记忆", store, records)
        report["one_off"] = records

        records = await judge(
            gateway,
            store,
            task_id,
            "我在南京",
            "用户：请帮我起草一封近况邮件，内容等我补充所在地后再写。\n助手：好的。",
        )
        check(store.snapshot() == before, "语境不明的当前位置被写入了记忆", store, records)
        check(records == [], "语境不明的信息不应调用记忆工具", store, records)
        report["ambiguous_location"] = records

        records = await judge(gateway, store, task_id, "我改主意了，以后先解释推导过程，再给结论。")
        check("推导" in memory("user"), "修改没有写入", store, records)
        report["update"] = records

        records = await judge(gateway, store, task_id, "忘掉回答顺序这个偏好吧。")
        check("推导" not in memory("user"), "要求忘记后内容仍在", store, records)
        report["forget"] = records

        reply = await ask(gateway, task_id, "我们团队内部会议默认多长时间？只回答时长。")
        check("30" in reply, f"全新会话没有使用记忆，回答为 {reply!r}", store, [])
        report["fresh_session_reply"] = reply
        report["memory"] = {target: memory(target) for target in ("user", "memory")}
        return report
    finally:
        server.should_exit = True
        await serving


def main() -> None:
    with tempfile.TemporaryDirectory(prefix="pebble-qoder-memory-") as directory:
        report = asyncio.run(verify(Path(directory)))
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
