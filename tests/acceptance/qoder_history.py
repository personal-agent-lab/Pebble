"""真实 Qoder 模型的历史对话检索验收；显式运行，不进入 pytest。

走真实链路：GatewayRuntime 调度真实 SDK 子进程，工具经进程内 MCP 端点调用真实的
HistoryStore、SQLite 与邮件草稿存储。只在临时数据目录里写入，不发送邮件、不触碰外部服务。

验证内容：新任务里能找回过去任务中的决定及其原因；只起草未确认的邮件被如实说成没发；
从没讨论过的事如实说没找到。
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
from server.agent.toolset import ToolDeps
from server.config import Settings
from server.db import init_db
from server.gateway.runtime import GatewayRuntime
from server.memory.service import MemoryStore
from server.sessions.history import HistoryStore
from server.sessions.service import SessionStore
from server.tools.gmail.service import MailDraftStore
from tests.support.gmail_double import MockGmailClient

NOT_SENT = ("没有发", "未发送", "还没发", "没发出", "尚未发送", "待确认", "没有发出", "还没有发送")
NOT_FOUND = ("没有找到", "没找到", "未找到", "没有相关", "没有讨论过", "没有提到", "查不到")


def available_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


class RecordingHistoryStore(HistoryStore):
    """记录模型发起的历史检索与读取。"""

    def __init__(self, path: Path, calls: list[str]):
        super().__init__(path)
        self._calls = calls

    def search(self, query, **kwargs) -> dict:
        self._calls.append(f"search:{query}")
        return super().search(query, **kwargs)

    def read(self, task_id, item_id, **kwargs) -> dict:
        self._calls.append("read")
        return super().read(task_id, item_id, **kwargs)


class Harness:
    def __init__(self, root: Path, settings: Settings, port: int):
        self.db_path = root / "pebble.db"
        self.calls: list[str] = []
        init_db(self.db_path)
        self.tasks = SessionStore(self.db_path)
        self.drafts = MailDraftStore(self.db_path)
        self.tool_server = ToolServer()
        self.gateway = QoderGateway(
            ToolDeps(
                drafts=self.drafts,
                tasks=self.tasks,
                gmail=MockGmailClient(),
                memory_store=MemoryStore(root),
                history=RecordingHistoryStore(self.db_path, self.calls),
            ),
            self.tool_server,
            settings=settings,
        )
        self.service = GatewayRuntime(
            self.gateway, memory_store=MemoryStore(root), path=self.db_path
        )
        self.app = FastAPI()
        self.app.mount(MCP_MOUNT_PATH, self.tool_server)
        self.server = uvicorn.Server(
            uvicorn.Config(self.app, host="127.0.0.1", port=port, log_level="warning")
        )

    async def start(self) -> None:
        self.serving = asyncio.create_task(self.server.serve())
        while not self.server.started:
            if self.serving.done():
                await self.serving
            await asyncio.sleep(0.01)

    async def stop(self) -> None:
        await self.service.close()
        self.server.should_exit = True
        await self.serving

    async def say(self, message: str, task_id: str | None = None) -> dict:
        self.calls.clear()
        if task_id is None:
            task_id = self.tasks.create_task(message)["task_id"]
        before = len(self.service.get_timeline(task_id)["items"])
        self.service.submit_message(task_id, message)
        async with asyncio.timeout(600):
            while self.service._active or self.service._titles:
                await asyncio.gather(
                    *list(self.service._active.values()),
                    *list(self.service._titles),
                    return_exceptions=True,
                )
        items = self.service.get_timeline(task_id)["items"][before:]
        answer = "".join(
            item["text"]
            for item in items
            if item["kind"] == "text" and item["role"] == "assistant"
        )
        return {"task_id": task_id, "answer": answer.strip(), "calls": list(self.calls)}


async def verify(root: Path, settings: Settings, port: int) -> dict:
    harness = Harness(root, settings, port)
    await harness.start()
    report: dict = {}
    try:
        # 准备：任务 A 讨论并定下方案；任务 C 只起草邮件、不确认发送。
        discussed = await harness.say(
            "Pebble 的历史检索有两个方案：方案 A 接 Elasticsearch，方案 B 用 SQLite FTS5。"
            "先别展开，简单回一句收到。"
        )
        await harness.say(
            "决定了：放弃方案 A，因为要额外部署和维护一个 Elasticsearch 服务，"
            "改用方案 B。回一句收到。",
            task_id=discussed["task_id"],
        )
        drafted = await harness.say(
            "给 bob@example.com 起草一封邮件，主题“预算评审”，告诉他评审改到下周二上午。先不要发。"
        )
        if len(harness.tasks.list_task_operations(drafted["task_id"])) != 1:
            raise AssertionError(f"没有生成邮件草稿：{drafted!r}")

        # 1. 新任务里找回过去的决定与原因。
        recalled = await harness.say("上次我们为什么放弃方案 A？")
        if not any(call.startswith("search:") for call in recalled["calls"]):
            raise AssertionError(f"没有检索历史：{recalled!r}")
        if "维护" not in recalled["answer"] and "部署" not in recalled["answer"]:
            raise AssertionError(f"没有答出放弃方案 A 的原因：{recalled!r}")
        report["recall_decision"] = recalled

        # 2. 只起草未确认的邮件：如实说没发。
        mail = await harness.say("上次给 bob@example.com 那封预算评审的邮件发出去了吗？")
        if not any(call.startswith("search:") for call in mail["calls"]):
            raise AssertionError(f"没有检索历史：{mail!r}")
        if not any(phrase in mail["answer"] for phrase in NOT_SENT):
            raise AssertionError(f"没有如实说明邮件未发送：{mail!r}")
        report["unsent_mail"] = mail

        # 3. 从没讨论过的事：如实说没找到。
        missing = await harness.say("我们之前讨论过火星基地选址的事吗？")
        if not any(call.startswith("search:") for call in missing["calls"]):
            raise AssertionError(f"没有检索历史：{missing!r}")
        if not any(phrase in missing["answer"] for phrase in NOT_FOUND):
            raise AssertionError(f"没有如实说明没找到：{missing!r}")
        report["not_found"] = missing
        return report
    finally:
        await harness.stop()


def main() -> None:
    base = Settings()
    if base.qoder_token is None:
        raise RuntimeError("未配置 QODERCN_PERSONAL_ACCESS_TOKEN")
    with tempfile.TemporaryDirectory(prefix="pebble-qoder-history-") as directory:
        root = Path(directory)
        settings = Settings(data_dir=root, port=available_port())
        report = asyncio.run(verify(root, settings, settings.port))
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
