"""真实 Qoder 模型的个人资料库 Phase 1 闭环验收；显式运行，不进入 pytest。

对话路径用真实模型驱动：保存资料、跨会话按原文读回、记忆与资料分流。
版本历史与并发拒绝用 KbStore 直接对真实本地 Git 校验，避免模型不确定性。
全程写入临时数据目录，KB 无外部副作用，不触碰实例 .data 与任何外部服务。
"""

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
from server.db import init_db
from server.errors import VersionConflictError
from server.gateway.agent_contract import Turn
from server.memory.judge import run_judgment
from server.memory.service import MemoryStore
from server.sessions.service import SessionStore
from server.tools.gmail.service import MailDraftStore
from server.tools.personal_kb.service import KbStore
from tests.support.gmail_double import MockGmailClient

CODE = "CORAL-7421"
TITLE = "验收会议纪要"


def available_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


async def run_turn(gateway: QoderGateway, prompt: str, task_id: str = "kb-acceptance") -> dict:
    texts = []
    notices = []
    session_id = None
    async for event in gateway.stream_turn(
        Turn(kind=TurnKind.MESSAGE, task_id=task_id, sdk_session_id=None, message=prompt)
    ):
        if event["type"] == "session":
            session_id = event["sdk_session_id"]
        elif event["type"] == "text":
            texts.append(event["text"])
        elif event["type"] == "notice":
            notices.append(event["text"])
        elif event["type"] == "error":
            raise RuntimeError(event["message"])
    if session_id is None:
        raise RuntimeError("Qoder 未返回 session_id")
    return {"session_id": session_id, "text": "".join(texts).strip(), "notices": notices}


def git(data_dir: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(data_dir), *args], check=True, capture_output=True, text=True
    ).stdout.strip()


def commit_count(data_dir: Path) -> int:
    return int(git(data_dir, "rev-list", "--count", "HEAD"))


def kb_files_with(data_dir: Path, needle: str) -> list[Path]:
    return [p for p in (data_dir / "kb").rglob("*.md") if needle in p.read_text(encoding="utf-8")]


async def verify(root: Path) -> dict:
    base = Settings()
    if base.qoder_token is None:
        raise RuntimeError("未配置 QODERCN_PERSONAL_ACCESS_TOKEN")
    port = available_port()
    settings = Settings(data_dir=root, port=port)
    db_path = root / "pebble.db"
    init_db(db_path)
    memory_store = MemoryStore(root)
    kb_store = KbStore(root)
    tool_server = ToolServer()
    gateway = QoderGateway(
        ToolDeps(
            drafts=MailDraftStore(db_path),
            tasks=SessionStore(db_path),
            gmail=MockGmailClient(),
            memory_store=memory_store,
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
        # 1. 对话保存一份资料：文件落到 kb/、产生一个 Git 提交、回答给出资料位置。
        before = commit_count(root)
        saved = await run_turn(
            gateway,
            f"请把这份会议纪要存进资料库，标题《{TITLE}》，正文：本次验收代号 {CODE}，"
            f"结论为通过。存好后告诉我这份资料保存在哪个相对路径。",
        )
        hits = kb_files_with(root, CODE)
        if len(hits) != 1:
            raise AssertionError(
                f"资料未唯一落盘到 kb/：{[str(p) for p in hits]}；"
                f"模型回复={saved['text']!r}；程序提示={saved['notices']!r}"
            )
        if commit_count(root) != before + 1:
            raise AssertionError("保存资料没有产生且仅产生一个 Git 提交")
        rel = hits[0].relative_to(root).as_posix()
        if not any(rel in notice for notice in saved["notices"]):
            raise AssertionError(f"程序没有展示实际保存位置：{saved['notices']!r}")
        saved_document = kb_store.read(path=rel)
        if saved_document["source"] != {"kind": "task", "ref": "kb-acceptance"}:
            raise AssertionError(f"资料没有记录实际来源任务：{saved_document['source']!r}")

        # 2. 全新会话只凭标题通过 kb_list 找到资料，再按原文读回。
        recalled = await run_turn(
            gateway, f"资料库里《{TITLE}》那份纪要的验收代号是什么？只回复代号本身。"
        )
        if CODE not in recalled["text"]:
            raise AssertionError(f"全新会话没有读回资料原文：{recalled['text']!r}")
        if recalled["session_id"] == saved["session_id"]:
            raise AssertionError("读回验证错误地复用了保存时的 SDK 会话")

        # 3. 通过对话修改原资料，再通过历史工具读取修改前版本。
        first = kb_store.read(path=rel)
        first_commit = first["ref"]["commit"]
        changed = await run_turn(
            gateway,
            f"请修改资料库里的《{TITLE}》，把正文改为：本次验收代号 REVOKED，结论为不通过。",
        )
        updated = kb_store.read(path=rel)
        if updated["id"] != first["id"]:
            raise AssertionError("更新改变了资料的稳定 id")
        if CODE in updated["body"] or "REVOKED" not in updated["body"]:
            raise AssertionError("当前版本仍含旧正文")
        if not any(rel in notice and first_commit in notice for notice in changed["notices"]):
            raise AssertionError(f"程序没有展示实际修改结果：{changed['notices']!r}")
        historical = await run_turn(
            gateway,
            f"请查看资料库里《{TITLE}》修改前一个版本，原来的验收代号是什么？只回复代号。",
        )
        if CODE not in historical["text"]:
            raise AssertionError(f"对话没有读回修改前正文：{historical['text']!r}")
        try:
            kb_store.update(expected_version=first_commit, path=rel, body="过期的并发写入")
        except VersionConflictError:
            pass
        else:
            raise AssertionError("过期 expected_version 没有被拒绝")
        if "过期的并发写入" in kb_store.read(path=rel)["body"]:
            raise AssertionError("被拒绝的并发写入仍然落了盘")

        # 4. 记忆与资料分流：粘贴长资料并说“记住这个”，正文不进长期记忆；
        #    单独的偏好仍由每轮记忆判断写入长期记忆。判断是独立的一次性模型调用，
        #    这里直接走真实的 run_judgment 路径，不经主回答流。
        boundary_code = "GSE-9130"
        await run_judgment(
            gateway,
            memory_store,
            db_path,
            task_id="kb-boundary",
            message=(
                f"记住这个：项目 {boundary_code} 的详细资料如下——目标、范围、里程碑、"
                "风险与逐项执行结果（此处为一大段项目细节正文，用于验证不会被塞进长期记忆）。"
            ),
        )
        await run_judgment(
            gateway,
            memory_store,
            db_path,
            task_id="kb-boundary",
            message="请记住我的偏好：回答先给结论再解释。这是普通格式文字，不是密码或令牌。",
        )
        snapshot = memory_store.snapshot()
        stored = snapshot["user"]["content"] + snapshot["memory"]["content"]
        if boundary_code in stored:
            raise AssertionError(f"详细资料正文被写进了长期记忆：{snapshot!r}")
        if "结论" not in stored:
            raise AssertionError(f"稳定偏好没有写入长期记忆：{snapshot!r}")

        return {
            "kb_save": "passed",
            "git_commit": "passed",
            "saved_notice": saved["notices"],
            "fresh_session_read": recalled["text"],
            "updated_notice": changed["notices"],
            "version_history": "passed",
            "stale_version_rejected": "passed",
            "memory_kb_boundary": "passed",
            "material_path": rel,
        }
    finally:
        server.should_exit = True
        await serving


def main() -> None:
    with tempfile.TemporaryDirectory(prefix="pebble-qoder-kb-") as directory:
        report = asyncio.run(verify(Path(directory)))
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
