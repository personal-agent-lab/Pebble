"""真实模型验收新提示词：非指令式的稳定信息当轮即保存。显式运行，不进入 pytest。"""

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
from server.gateway.agent_contract import Turn
from server.memory.service import MemoryStore
from server.sessions.service import SessionStore
from server.tools.gmail.service import MailDraftStore
from tests.support.gmail_double import MockGmailClient


def available_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


async def run_turn(gateway: QoderGateway, prompt: str) -> str:
    texts = []
    async for event in gateway.stream_turn(
        Turn(
            kind=TurnKind.MESSAGE,
            task_id="memory-prompt-acceptance",
            sdk_session_id=None,
            message=prompt,
        )
    ):
        if event["type"] == "text":
            texts.append(event["text"])
        elif event["type"] == "error":
            raise RuntimeError(event["message"])
    return "".join(texts).strip()


def read_memory(root: Path, filename: str) -> str:
    path = root / "memory" / filename
    return path.read_text(encoding="utf-8") if path.exists() else ""


async def verify(root: Path) -> dict:
    base = Settings()
    if base.qoder_token is None:
        raise RuntimeError("未配置 QODERCN_PERSONAL_ACCESS_TOKEN")
    port = available_port()
    settings = Settings(data_dir=root, port=port)
    memory_store = MemoryStore(root)
    tool_server = ToolServer()
    gateway = QoderGateway(
        ToolDeps(
            drafts=MailDraftStore(root / "pebble.db"),
            tasks=SessionStore(root / "pebble.db"),
            gmail=MockGmailClient(),
            memory_store=memory_store,
        ),
        tool_server,
        settings=settings,
    )
    app = FastAPI()
    app.mount(MCP_MOUNT_PATH, tool_server)
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    serving = asyncio.create_task(server.serve())
    while not server.started:
        if serving.done():
            await serving
        await asyncio.sleep(0.01)

    try:
        calls: list[tuple] = []
        original_edit = memory_store.edit

        def traced_edit(target, old_text="", new_text=""):
            calls.append((target, old_text, new_text))
            return original_edit(target, old_text, new_text)

        memory_store.edit = traced_edit
        reply = await run_turn(
            gateway,
            "我最近正在学习 Hermes Agent（一个开源个人助理项目）的设计，"
            "之后大概会经常问你相关的问题。今天先不用查资料，简单说说你打算怎么帮我。",
        )
        user_md = read_memory(root, "USER.md")
        memory_md = read_memory(root, "MEMORY.md")
        if "Hermes" not in user_md:
            raise AssertionError(
                f"模型未当轮把学习方向写入 USER.md：USER.md={user_md!r} "
                f"MEMORY.md={memory_md!r} 工具调用={calls!r} 回复={reply!r}"
            )
        if user_md.count("Hermes") != 1:
            raise AssertionError(f"USER.md 中学习方向重复保存：{user_md!r}")
        return {
            "user_md": user_md,
            "reply": reply,
            "memory_calls": calls,
        }
    finally:
        server.should_exit = True
        await serving


def main() -> None:
    with tempfile.TemporaryDirectory(prefix="pebble-qoder-memory-prompt-") as directory:
        report = asyncio.run(verify(Path(directory)))
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
