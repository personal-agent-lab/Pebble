"""真实 Qoder 模型的资料库 Phase 3 验收：跟随用户改动、删除、恢复与整理前确认；不进入 pytest。

走真实链路：GatewayRuntime 调度真实 SDK 子进程，工具经进程内 MCP 端点调用真实的
KbStore、文件、Git 与 SQLite。只在临时数据目录里写入，不触碰实例 .data 与任何外部服务。

验证内容：用户直接改文件后回答使用新内容、明确要求删除时删除并展示提示、对话中找回已删除
的资料、批量整理先说明方案且未经同意不移动、用户同意后再移动。
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
from server.sessions.service import SessionStore
from server.tools.gmail.service import MailDraftStore
from server.tools.personal_kb.service import KbStore
from tests.support.gmail_double import MockGmailClient

CODE = "NEBULA-3390"
EDITED_CODE = "NEBULA-4711"


def available_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


class RecordingKbStore(KbStore):
    """记录模型真正发起的删除、移动与恢复，用于核对工具轨迹，而不是解析模型措辞。"""

    def __init__(self, data_dir: Path, calls: list[str]):
        super().__init__(data_dir)
        self._calls = calls

    def delete(self, **kwargs) -> dict:
        self._calls.append("delete")
        return super().delete(**kwargs)

    def move(self, **kwargs) -> dict:
        self._calls.append("move")
        return super().move(**kwargs)

    def restore(self, **kwargs) -> dict:
        self._calls.append("restore")
        return super().restore(**kwargs)


class Harness:
    """一次验收进程内的装配：真实运行时 + 真实 SDK 网关 + 真实的资料库与数据库。"""

    def __init__(self, root: Path, settings: Settings, port: int):
        self.root = root
        self.db_path = root / "pebble.db"
        self.calls: list[str] = []
        init_db(self.db_path)
        self.kb = RecordingKbStore(root, self.calls)
        self.tasks = SessionStore(self.db_path)
        self.tool_server = ToolServer()
        self.gateway = QoderGateway(
            ToolDeps(
                drafts=MailDraftStore(self.db_path),
                tasks=self.tasks,
                gmail=MockGmailClient(),
                memory_store=MemoryStore(root),
                kb_store=self.kb,
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
        """在新任务或已有任务里发一条消息，等这一轮跑完，取回本轮新增的回答与程序提示。"""
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
        return {
            "task_id": task_id,
            "answer": "".join(
                item["text"]
                for item in items
                if item["kind"] == "text" and item["role"] == "assistant"
            ).strip(),
            "notices": [item["text"] for item in items if item["kind"] == "notice"],
            "calls": list(self.calls),
        }


async def verify(root: Path, settings: Settings, port: int) -> dict:
    harness = Harness(root, settings, port)
    await harness.start()
    report: dict = {}
    try:
        plan = harness.kb.save(
            title="星云项目验收纪要",
            body=f"## 验收结果\n\n本次验收代号 {CODE}，结论为通过。",
            path="inbox/星云验收.md",
        )
        harness.kb.save(title="周会纪要", body="## 决定\n\n下季度运维预算提高一成。")
        path = root / plan["path"]

        # 1. 用户直接在编辑器里改文件，不经过任何工具：回答使用改动后的内容。
        path.write_text(
            path.read_text(encoding="utf-8").replace(CODE, EDITED_CODE), encoding="utf-8"
        )
        edited = await harness.say("资料库里星云项目这次验收的代号是什么？只回复代号本身。")
        if EDITED_CODE not in edited["answer"] or CODE in edited["answer"]:
            raise AssertionError(f"回答没有使用用户改动后的内容：{edited['answer']!r}")
        report["manual_edit_answered"] = edited["answer"]

        # 2. 用户明确要求删除：删除并由程序展示提示。
        deleted = await harness.say("把资料库里的星云项目验收纪要删掉。")
        if deleted["calls"].count("delete") != 1 or path.exists():
            raise AssertionError(f"明确要求删除后没有删除：{deleted!r}")
        if not any(notice.startswith("已删除资料") for notice in deleted["notices"]):
            raise AssertionError(f"删除没有程序提示：{deleted['notices']!r}")
        report["explicit_delete"] = deleted["notices"]

        # 3. 新会话找回已删除的资料。
        restored = await harness.say("刚才删掉的星云项目验收纪要，帮我找回来。")
        if "restore" not in restored["calls"] or not path.exists():
            raise AssertionError(f"没有找回已删除的资料：{restored!r}")
        if EDITED_CODE not in path.read_text(encoding="utf-8"):
            raise AssertionError("找回的不是删除前的版本")
        if not any(notice.startswith("已恢复资料") for notice in restored["notices"]):
            raise AssertionError(f"恢复没有程序提示：{restored['notices']!r}")
        report["restore_after_delete"] = restored["notices"]

        # 4. 批量整理：先说明方案，未经同意不移动；同意后再移动。
        proposal = await harness.say("帮我把资料库整理一下，项目相关的资料都归到“项目”目录。")
        if proposal["calls"]:
            raise AssertionError(f"未经同意就执行了整理：{proposal!r}")
        if not path.exists():
            raise AssertionError("未经同意资料位置发生了变化")
        report["batch_proposal"] = proposal["answer"]
        agreed = await harness.say("可以，按你说的整理。", task_id=proposal["task_id"])
        if "move" not in agreed["calls"] or path.exists():
            raise AssertionError(f"同意后没有移动资料：{agreed!r}")
        moved = [item["path"] for item in harness.kb.list()["documents"]]
        if not any(item.startswith("kb/项目/") for item in moved):
            raise AssertionError(f"资料没有归到项目目录：{moved!r}")
        report["batch_after_consent"] = {"notices": agreed["notices"], "paths": moved}
        return report
    finally:
        await harness.stop()


def main() -> None:
    base = Settings()
    if base.qoder_token is None:
        raise RuntimeError("未配置 QODERCN_PERSONAL_ACCESS_TOKEN")
    with tempfile.TemporaryDirectory(prefix="pebble-qoder-kb-followup-") as directory:
        root = Path(directory)
        settings = Settings(data_dir=root, port=available_port())
        report = asyncio.run(verify(root, settings, settings.port))
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
