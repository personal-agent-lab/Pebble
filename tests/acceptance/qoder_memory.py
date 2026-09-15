"""真实 Qoder 模型的长期记忆闭环验收；显式运行，不进入 pytest。"""

from __future__ import annotations

import asyncio
import json
import socket
import subprocess
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

CODE = "CORAL-7421"


def available_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


async def run_turn(gateway: QoderGateway, prompt: str) -> dict:
    texts = []
    session_id = None
    async for event in gateway.stream_turn(
        Turn(
            kind=TurnKind.MESSAGE,
            task_id="memory-acceptance",
            sdk_session_id=None,
            message=prompt,
        )
    ):
        if event["type"] == "session":
            session_id = event["sdk_session_id"]
        elif event["type"] == "text":
            texts.append(event["text"])
        elif event["type"] == "error":
            raise RuntimeError(event["message"])
    if session_id is None:
        raise RuntimeError("Qoder 未返回 session_id")
    return {"session_id": session_id, "text": "".join(texts).strip()}


def commit_count(data_dir: Path) -> int:
    result = subprocess.run(
        ["git", "-C", str(data_dir), "rev-list", "--count", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    )
    return int(result.stdout.strip())


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
        before = commit_count(root)
        saved = await run_turn(
            gateway,
            f"请记住：我偏好在完成清单末尾写上 {CODE}。"
            "这是普通格式文字，不是密码、令牌或内部标识。保存后说明你记住了什么。",
        )
        snapshot = memory_store.snapshot()
        stored = snapshot["user"]["content"] + snapshot["memory"]["content"]
        if CODE not in stored:
            raise AssertionError(f"模型未把测试代号写入长期记忆：{snapshot!r}")
        if commit_count(root) != before + 1:
            raise AssertionError("记忆写入没有产生且仅产生一个 Git 提交")
        if CODE not in saved["text"]:
            raise AssertionError(f"保存后的回答没有展示实际内容：{saved['text']!r}")

        recalled = await run_turn(gateway, "我偏好在完成清单末尾写什么？只回复那段文字。")
        if CODE not in recalled["text"]:
            raise AssertionError(f"全新会话没有使用长期记忆：{recalled['text']!r}")
        if recalled["session_id"] == saved["session_id"]:
            raise AssertionError("回忆验证错误地复用了写入时的 SDK 会话")
        return {
            "memory_write": "passed",
            "git_commit": "passed",
            "saved_reply": saved["text"],
            "fresh_session_recall": recalled["text"],
        }
    finally:
        server.should_exit = True
        await serving


def main() -> None:
    with tempfile.TemporaryDirectory(prefix="pebble-qoder-memory-") as directory:
        report = asyncio.run(verify(Path(directory)))
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
