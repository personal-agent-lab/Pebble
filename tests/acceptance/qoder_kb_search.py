"""真实 Qoder 模型的资料库检索验收（Phase 2）；显式运行，不进入 pytest。

走真实链路：GatewayRuntime 调度真实 SDK 子进程，工具经进程内 MCP 端点调用真实的
KbStore、文件、Git 与 SQLite，工具轨迹取自程序记录而不是模型自述。只在临时数据目录里
写入，不触碰实例 .data 与任何外部服务，也不需要 Gmail 或 iCloud 凭证。

验证内容：检索后读原文再正确作答、读取的是真实资料、更新后回答用新版本、资料互相冲突时
两份都读到并说明冲突、查不到依据时不编造。回答不向用户展示来源，因此只核对回答与工具轨迹。
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

# 摘自已保存的中文资料：提问、预期答案与（可选的）冲突答案。
CODE = "NEBULA-3390"
REVISED_CODE = "NEBULA-3391"
CONFLICT_CODE = "NEBULA-3392"
MISSING_CODE = "NEBULA-0000"


def available_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


class Harness:
    """一次验收进程内的装配：真实运行时 + 真实 SDK 网关 + 真实的资料库与数据库。"""

    def __init__(self, root: Path, settings: Settings, port: int):
        self.root = root
        self.db_path = root / "pebble.db"
        self.searches: list[str] = []
        self.reads: list[str] = []
        init_db(self.db_path)
        self.kb = RecordingKbStore(root, self.searches, self.reads)
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
        # 记忆与数据库都指向临时目录：验收只写临时数据，不碰实例 .data。
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

    async def ask(self, question: str) -> dict:
        """新建一个任务（全新 SDK 会话）提问，等这一轮跑完再取回时间线与来源。"""
        self.searches.clear()
        self.reads.clear()
        task_id = self.tasks.create_task(question)["task_id"]
        self.service.submit_message(task_id, question)
        async with asyncio.timeout(600):
            while self.service._active or self.service._titles:
                await asyncio.gather(
                    *list(self.service._active.values()),
                    *list(self.service._titles),
                    return_exceptions=True,
                )
        timeline = self.service.get_timeline(task_id)
        answers = [
            item["text"]
            for item in timeline["items"]
            if item["kind"] == "text" and item["role"] == "assistant"
        ]
        return {
            "task_id": task_id,
            "answer": "".join(answers).strip(),
            "searches": list(self.searches),
            "reads": list(self.reads),
        }


class RecordingKbStore(KbStore):
    """记录模型真正发起的检索与读取，用于核对工具轨迹，而不是解析模型措辞。"""

    def __init__(self, data_dir: Path, searches: list[str], reads: list[str]):
        super().__init__(data_dir)
        self._searches = searches
        self._reads = reads

    def search(self, **kwargs) -> dict:
        self._searches.append(kwargs["query"])
        return super().search(**kwargs)

    def read(self, **kwargs) -> dict:
        result = super().read(**kwargs)
        # 记录实际读到的资料位置：模型可能按 path、id 或 ref 定位，统一落到真实路径。
        mode = "ref" if kwargs.get("ref") else "document"
        self._reads.append(f"{mode}:{result['ref']['path']}")
        return result


def seed(kb: KbStore) -> dict:
    """两份真实中文资料：提问阶段只有一份提到验收代号，答案唯一。"""
    plan = kb.save(
        title="星云项目验收纪要",
        body=(
            "## 背景\n\n星云项目二期在九月进入验收阶段。\n\n"
            f"## 验收结果\n\n本次验收代号 {CODE}，结论为通过，遗留两项低风险问题。\n\n"
            "## 后续安排\n\n复验安排在两週后，负责人为李工。"
        ),
        path="项目/星云验收.md",
        summary="星云项目二期验收结论、代号与复验安排",
    )
    meeting = kb.save(
        title="周会纪要",
        body="## 决定\n\n下季度把运维预算提高一成，负责人待定。",
        path="项目/周会.md",
    )
    return {"plan": plan, "meeting": meeting}


async def verify(root: Path, settings: Settings, port: int) -> dict:
    harness = Harness(root, settings, port)
    await harness.start()
    report: dict = {}
    try:
        documents = seed(harness.kb)

        # 1. 全新会话提问：先检索、再读原文，读的是真实资料，回答正确。
        asked = await harness.ask("资料库里星云项目这次验收的代号是什么？只回复代号本身。")
        if CODE not in asked["answer"]:
            raise AssertionError(f"回答没有给出资料里的代号：{asked['answer']!r}")
        if not asked["searches"]:
            raise AssertionError(f"模型没有调用 kb_search：{asked!r}")
        if not any(read.endswith(documents["plan"]["path"]) for read in asked["reads"]):
            raise AssertionError(f"模型没有读取含答案的资料原文：{asked!r}")
        report["search_then_read"] = "passed"
        report["trajectory"] = {"searches": asked["searches"], "reads": asked["reads"]}

        # 2. 更新资料后：新回答用新版本。
        harness.kb.update(
            doc_id=documents["plan"]["id"],
            expected_version=documents["plan"]["version"],
            body=(
                "## 验收结果\n\n本次验收代号改为 "
                f"{REVISED_CODE}，结论为通过（已回填会签页）。\n\n"
                "## 后续安排\n\n复验安排在两週后，负责人为李工。"
            ),
        )
        after = await harness.ask("资料库里星云项目最新的验收代号是什么？只回复代号本身。")
        if REVISED_CODE not in after["answer"] or CODE in after["answer"]:
            raise AssertionError(f"更新后仍使用旧版本：{after['answer']!r}")
        if not after["reads"]:
            raise AssertionError(f"更新后没有重新读取原文：{after!r}")
        report["update_uses_new_version"] = "passed"

        # 3. 互相冲突的资料：两边都要读到，并说明冲突。
        harness.kb.save(
            title="星云验收补充说明",
            body=f"## 验收结果\n\n另一份记录写的验收代号是 {CONFLICT_CODE}，与会签页一致。",
            path="项目/星云验收补充.md",
        )
        conflicted = await harness.ask(
            "资料库里星云项目的验收代号有两个不同说法吗？把两份记录各自的代号和出处都说出来。"
        )
        if REVISED_CODE not in conflicted["answer"] or CONFLICT_CODE not in conflicted["answer"]:
            raise AssertionError(f"回答没有列出来自两份资料的冲突代号：{conflicted['answer']!r}")
        read_paths = {read.split(":", 1)[1] for read in conflicted["reads"]}
        if len(read_paths) < 2:
            raise AssertionError(f"冲突回答没有读取两份资料：{conflicted['reads']!r}")
        report["conflict_reported"] = sorted(read_paths)

        # 4. 查不到依据：不编造答案。
        missing = await harness.ask(
            f"资料库里有没有提到代号 {MISSING_CODE} 的记录？如果没有就直说没有。"
        )
        if MISSING_CODE in missing["answer"] and "没有" not in missing["answer"]:
            raise AssertionError(f"对不存在的资料给出了肯定答案：{missing['answer']!r}")
        report["missing_material"] = {"answer": missing["answer"]}
        return report
    finally:
        await harness.stop()


def main() -> None:
    base = Settings()
    if base.qoder_token is None:
        raise RuntimeError("未配置 QODERCN_PERSONAL_ACCESS_TOKEN")
    with tempfile.TemporaryDirectory(prefix="pebble-qoder-kb-search-") as directory:
        root = Path(directory)
        settings = Settings(data_dir=root, tool_port=available_port())
        report = asyncio.run(verify(root, settings, settings.tool_port))
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
