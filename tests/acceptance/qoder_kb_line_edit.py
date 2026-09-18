"""真实 Qoder 模型的资料按行修改验收；显式运行，不进入 pytest。

在一份较长的资料里只改一处事实：模型应当先读取带锚点的正文，再用 kb_update 的
operations 只改那一行，其余原文逐字不变。全程写入临时数据目录，不触碰实例 .data。
"""

from __future__ import annotations

import asyncio
import json
import tempfile
from pathlib import Path

import uvicorn
from fastapi import FastAPI

from server.agent.client import QoderGateway
from server.agent.mcp import MCP_MOUNT_PATH, ToolServer
from server.agent.toolset import ToolDeps
from server.config import Settings
from server.db import init_db
from server.memory.service import MemoryStore
from server.sessions.service import SessionStore
from server.tools.gmail.service import MailDraftStore
from server.tools.personal_kb.service import KbStore
from tests.acceptance.qoder_kb import available_port, git, run_turn
from tests.support.gmail_double import MockGmailClient

WEEKS = [
    f"## 第 {index} 周\n\n- 主题：模块 {index} 的设计评审\n- 负责人：成员 {index}"
    for index in range(1, 13)
]
BODY = "\n\n".join([*WEEKS, "## 汇报\n\n期末汇报时间：12 月 20 日下午，地点 A3-201。"])


async def verify(root: Path) -> dict:
    if Settings().qoder_token is None:
        raise RuntimeError("未配置 QODERCN_PERSONAL_ACCESS_TOKEN")
    port = available_port()
    settings = Settings(data_dir=root, tool_port=port)
    db_path = root / "pebble.db"
    init_db(db_path)
    kb_store = KbStore(root)
    tool_server = ToolServer()
    gateway = QoderGateway(
        ToolDeps(
            drafts=MailDraftStore(db_path),
            tasks=SessionStore(db_path),
            gmail=MockGmailClient(),
            memory_store=MemoryStore(root),
            kb_store=kb_store,
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
        plan = kb_store.save(title="学期计划", body=BODY, summary="每周设计评审安排与期末汇报")
        edited = await run_turn(
            gateway,
            "资料库里的《学期计划》：期末汇报改到 12 月 27 日下午了，地点不变，帮我改一下。",
            task_id="kb-line-edit",
        )
        after = kb_store.read(path=plan["path"])["body"]
        if after != BODY.replace("12 月 20 日", "12 月 27 日"):
            raise AssertionError(
                f"局部修改动到了其他内容或没有改对：{after!r}；模型回复={edited['text']!r}"
            )
        subject = git(root, "log", "-1", "--format=%s")
        if not subject.startswith("[Kb] Edit"):
            raise AssertionError(f"没有走按行修改（operations），而是整篇替换：{subject!r}")
        return {"line_edit": "passed", "commit": subject, "notices": edited["notices"]}
    finally:
        server.should_exit = True
        await serving


def main() -> None:
    with tempfile.TemporaryDirectory(prefix="pebble-qoder-kb-line-edit-") as directory:
        report = asyncio.run(verify(Path(directory)))
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
