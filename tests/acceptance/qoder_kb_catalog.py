"""真实 Qoder 模型的资料目录验收；显式运行，不进入 pytest。

走真实链路：GatewayRuntime 调度真实 SDK 子进程，每轮上下文带上程序生成的资料目录，
工具经进程内 MCP 端点调用真实的 KbStore。只在临时数据目录里写入，不触碰任何外部服务。

验证内容：Agent 能凭资料目录说出库里有哪些相关资料；目录里没有的细节仍去读原文再回答；
Agent 保存资料时填写一句话说明，新资料随即出现在下一轮的资料目录里。
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


def available_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


class RecordingKbStore(KbStore):
    """记录模型发起的检索、读取与保存，用于核对工具轨迹。"""

    def __init__(self, data_dir: Path, calls: list[str]):
        super().__init__(data_dir)
        self._calls = calls

    def search(self, **kwargs) -> dict:
        self._calls.append("search")
        return super().search(**kwargs)

    def read(self, **kwargs) -> dict:
        self._calls.append("read")
        return super().read(**kwargs)

    def save(self, **kwargs) -> dict:
        self._calls.append("save")
        return super().save(**kwargs)


class Harness:
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

    async def ask(self, message: str) -> dict:
        """新任务提问（全新 SDK 会话），取回回答与这一轮的资料库工具轨迹。"""
        self.calls.clear()
        task_id = self.tasks.create_task(message)["task_id"]
        self.service.submit_message(task_id, message)
        async with asyncio.timeout(600):
            while self.service._active or self.service._titles:
                await asyncio.gather(
                    *list(self.service._active.values()),
                    *list(self.service._titles),
                    return_exceptions=True,
                )
        items = self.service.get_timeline(task_id)["items"]
        answer = "".join(
            item["text"]
            for item in items
            if item["kind"] == "text" and item["role"] == "assistant"
        )
        return {"answer": answer.strip(), "calls": list(self.calls)}


async def verify(root: Path, settings: Settings, port: int) -> dict:
    harness = Harness(root, settings, port)
    await harness.start()
    report: dict = {}
    try:
        KbStore.save(
            harness.kb,
            title="GSE 实验一要求",
            path="课程/gse-lab1",
            summary="第一次实验的任务要求与提交方式",
            body="## 要求\n\n实现一个 Shell。\n\n## 提交\n\n"
            "截止 10 月 8 日 23:59，提交到课程平台。",
        )
        KbStore.save(
            harness.kb,
            title="GSE 课程大纲",
            path="课程/gse-syllabus",
            summary="课程安排、评分构成与助教联系方式",
            body="## 评分\n\n实验 60%，期末 40%。",
        )
        KbStore.save(
            harness.kb,
            title="星云项目验收纪要",
            path="项目/星云验收",
            summary="二期验收结论与代号",
            body="## 结果\n\n代号 NEBULA-3390，结论为通过。",
        )

        # 1. 凭资料目录说出库里有哪些相关资料。
        listed = await harness.ask("我资料库里有哪些跟课程有关的资料？只列标题和各自讲什么。")
        for title in ("GSE 实验一要求", "GSE 课程大纲"):
            if title not in listed["answer"]:
                raise AssertionError(f"没有列出资料目录里的课程资料：{listed!r}")
        if "星云" in listed["answer"]:
            raise AssertionError(f"把无关资料也当成课程资料列出：{listed['answer']!r}")
        report["list_from_catalog"] = listed

        # 2. 目录里没有的细节：去读原文再回答。
        detail = await harness.ask("GSE 实验一什么时候截止？只回复日期和时间。")
        if "10 月 8 日" not in detail["answer"] and "10月8日" not in detail["answer"]:
            raise AssertionError(f"没有读到原文里的截止时间：{detail!r}")
        if "read" not in detail["calls"] and "search" not in detail["calls"]:
            raise AssertionError(f"没有查资料就回答了细节：{detail!r}")
        report["detail_needs_reading"] = detail

        # 3. Agent 保存资料时填写说明，新资料出现在下一轮目录里。
        saved = await harness.ask(
            "帮我把这段存进资料库：Pebble 周会定在每周三上午十点，地点第二会议室，负责人王工。"
        )
        if "save" not in saved["calls"]:
            raise AssertionError(f"没有保存资料：{saved!r}")
        documents = harness.kb.list()["documents"]
        created = [item for item in documents if "周会" in (item["title"] or "")]
        if len(created) != 1 or not (created[0]["summary"] or "").strip():
            raise AssertionError(f"保存的资料没有一句话说明：{documents!r}")
        catalog = harness.kb.catalog()
        if created[0]["title"] not in catalog:
            raise AssertionError(f"新资料没有进入资料目录：{catalog}")
        report["saved_with_summary"] = {"document": created[0], "catalog": catalog}
        return report
    finally:
        await harness.stop()


def main() -> None:
    base = Settings()
    if base.qoder_token is None:
        raise RuntimeError("未配置 QODERCN_PERSONAL_ACCESS_TOKEN")
    with tempfile.TemporaryDirectory(prefix="pebble-qoder-kb-catalog-") as directory:
        root = Path(directory)
        settings = Settings(data_dir=root, port=available_port())
        report = asyncio.run(verify(root, settings, settings.port))
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
