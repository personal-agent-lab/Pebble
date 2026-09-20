"""真实 Qoder 模型的任务归档验收（资料库 Phase 4）；显式运行，不进入 pytest。

走真实链路：GatewayRuntime 调度真实 SDK 子进程，确认后由 Confirmation 调用发送替身（只记录，
不连接 Gmail），执行结果回传轮交回真实模型。只在临时数据目录里写入。

验证内容：只起草、未确认时不归档；确认发送、结果回传后 Agent 自主归档，归档写入 archive
目录，原始内容与总结分节，执行结果与实际结果一致，并由程序展示归档位置。
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
from server.approval.service import ConfirmationService
from server.config import Settings
from server.db import init_db
from server.gateway.runtime import GatewayRuntime
from server.memory.service import MemoryStore
from server.sessions.service import SessionStore
from server.tools.gmail.service import MailDraftStore
from server.tools.personal_kb.service import KbStore
from tests.support.gmail_double import MockGmailClient

RECIPIENT = "alice@example.com"


def available_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


class Harness:
    def __init__(self, root: Path, settings: Settings, port: int):
        self.root = root
        self.db_path = root / "pebble.db"
        self.sent: list[dict] = []
        init_db(self.db_path)
        self.kb = KbStore(root)
        self.tasks = SessionStore(self.db_path)
        self.drafts = MailDraftStore(self.db_path)
        self.confirmations = ConfirmationService(self._send, path=self.db_path)
        self.tool_server = ToolServer()
        self.gateway = QoderGateway(
            ToolDeps(
                drafts=self.drafts,
                tasks=self.tasks,
                gmail=MockGmailClient(),
                memory_store=MemoryStore(root),
                kb_store=self.kb,
                confirmations=self.confirmations,
            ),
            self.tool_server,
            settings=settings,
        )
        self.service = GatewayRuntime(
            self.gateway,
            confirmations=self.confirmations,
            memory_store=MemoryStore(root),
            path=self.db_path,
        )
        self.app = FastAPI()
        self.app.mount(MCP_MOUNT_PATH, self.tool_server)
        self.server = uvicorn.Server(
            uvicorn.Config(self.app, host="127.0.0.1", port=port, log_level="warning")
        )

    def _send(self, **fields) -> dict:
        """发送替身：记录确认后的内容，返回成功结果，不连接任何外部服务。"""
        self.sent.append(fields)
        return {"status": "sent", "message_id": "acceptance-message"}

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

    async def settle(self) -> None:
        async with asyncio.timeout(600):
            while self.service._active or self.service._titles or self.service._sends:
                await asyncio.gather(
                    *list(self.service._active.values()),
                    *list(self.service._titles),
                    *list(self.service._sends.values()),
                    return_exceptions=True,
                )
                await asyncio.sleep(0.2)

    def archives(self) -> list[dict]:
        return self.kb.list(directory="archive")["documents"]

    def notices(self, task_id: str) -> list[str]:
        items = self.service.get_timeline(task_id)["items"]
        return [item["text"] for item in items if item["kind"] == "notice"]


async def verify(root: Path, settings: Settings, port: int) -> dict:
    harness = Harness(root, settings, port)
    await harness.start()
    report: dict = {}
    try:
        # 1. 只起草、未确认：不归档。
        task_id = harness.tasks.create_task("约复盘会")["task_id"]
        harness.service.submit_message(
            task_id,
            f"给 {RECIPIENT} 写封邮件，告诉她周四下午三点在第二会议室开项目复盘会，"
            "主题写“复盘会通知”。",
        )
        await harness.settle()
        operations = harness.tasks.list_task_operations(task_id)
        if len(operations) != 1:
            raise AssertionError(f"没有生成唯一的邮件草稿：{operations!r}")
        if harness.archives():
            raise AssertionError(f"只起草未确认就归档了：{harness.archives()!r}")
        report["draft_not_archived"] = "passed"

        # 2. 用户确认发送：发送替身执行，结果回传轮交回模型，Agent 自主归档。
        operation = operations[0]
        harness.confirmations.accept_confirmation(
            task_id, operation["operation_id"], operation["version"]
        )
        harness.service.confirm_execution(operation["operation_id"])
        await harness.settle()
        if len(harness.sent) != 1:
            raise AssertionError(f"确认后没有恰好发送一次：{harness.sent!r}")
        archives = harness.archives()
        if len(archives) != 1:
            raise AssertionError(f"发送成功后没有归档或重复归档：{archives!r}")
        archived = harness.kb.read(path=archives[0]["path"])
        body = archived["body"]
        if "## 原始内容" not in body or "## 总结与执行结果" not in body:
            raise AssertionError(f"归档没有把原始内容与总结分节：{body!r}")
        if "周四" not in body or "已发送" not in body and "发送成功" not in body:
            raise AssertionError(f"归档没有记录邮件内容或实际发送结果：{body!r}")
        if not any(notice.startswith("已归档任务") for notice in harness.notices(task_id)):
            raise AssertionError(f"归档没有程序提示：{harness.notices(task_id)!r}")
        report["archived_after_execution"] = {"path": archives[0]["path"], "body": body}
        return report
    finally:
        await harness.stop()


def main() -> None:
    base = Settings()
    if base.qoder_token is None:
        raise RuntimeError("未配置 QODERCN_PERSONAL_ACCESS_TOKEN")
    with tempfile.TemporaryDirectory(prefix="pebble-qoder-kb-archive-") as directory:
        root = Path(directory)
        settings = Settings(data_dir=root, tool_port=available_port())
        report = asyncio.run(verify(root, settings, settings.tool_port))
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
