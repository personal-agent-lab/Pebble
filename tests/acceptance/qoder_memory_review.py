"""真实 Qoder 模型的后台记忆回顾验收：真实 MCP 环回 + 只新增工具 + 周期触发。

显式运行，不进入 pytest。同时验证一次性回顾会话在带工具时 include_partial_messages=False 可用。
"""

from __future__ import annotations

import asyncio
import json
import socket
import subprocess
import tempfile
from pathlib import Path
from uuid import uuid4

import uvicorn
from fastapi import FastAPI

from server.agent.client import QoderGateway
from server.agent.mcp import MCP_MOUNT_PATH, ToolServer
from server.agent.toolset import ToolDeps
from server.config import Settings
from server.db import init_db, session, write
from server.memory.review import MemoryReviewScheduler
from server.memory.service import MemoryStore
from server.sessions import repository, timeline
from server.sessions import runs as runs_repo
from server.sessions.service import SessionStore, timestamp
from server.tools.gmail.service import MailDraftStore
from tests.support.gmail_double import MockGmailClient

SEED = [
    (
        "我最近正在学习 Hermes Agent（一个开源个人助理项目）的设计，"
        "之后大概会经常问你相关的问题。",
        "好的，后续聊到 Hermes Agent 我会直接接上这个背景。",
    ),
    (
        "另外我更喜欢简洁的中文回复，别每次都写长篇大论。",
        "明白，之后回复尽量精简。",
    ),
    (
        "上次你开头总写“好的！”，这个习惯改掉，直接说结论。",
        "知道了，以后直接进入正题。",
    ),
    (
        "我在准备一个自己的个人助理 side project，存储打算用 SQLite。",
        "收到，SQLite 做单机个人数据很合适。",
    ),
    (
        "帮我把这周的名字列表按拼音排一下：志强、伟、静。",
        "排序结果：静、伟、志强。",
    ),
]


def available_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def git_log(data_dir: Path) -> list[str]:
    result = subprocess.run(
        ["git", "-C", str(data_dir), "log", "--format=%s"],
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.splitlines()


def seed_round(conn, task_id: str, user_text: str, assistant_text: str) -> None:
    run_id = str(uuid4())
    runs_repo.insert(
        conn, run_id, task_id, runs_repo.KIND_MESSAGE, {"message": user_text}, None, timestamp()
    )
    conn.execute(
        "UPDATE agent_runs SET status = 'done', started_at = ?, finished_at = ? WHERE run_id = ?",
        (timestamp(), timestamp(), run_id),
    )
    timeline.insert_text(conn, task_id, run_id, "user", user_text)
    timeline.append_assistant_text(conn, task_id, run_id, assistant_text)


def timeline_count(task_id: str, path: Path) -> int:
    with session(path) as conn:
        return conn.execute(
            "SELECT COUNT(*) AS n FROM task_timeline_items WHERE task_id = ?", (task_id,)
        ).fetchone()["n"]


async def run_review(
    root: Path, gateway: QoderGateway, scheduler: MemoryReviewScheduler, task_id: str
) -> dict:
    row = scheduler.enqueue_if_due(task_id)
    assert row is not None, "攒满 5 个已完成消息轮后未触发周期回顾"
    claimed = scheduler.claim(row["review_id"])
    assert claimed is not None, "回顾未能取得运行权"
    await scheduler.run(row["review_id"], gateway)
    with session(root / "pebble.db") as conn:
        return dict(
            conn.execute(
                "SELECT status, error FROM memory_reviews WHERE review_id = ?",
                (row["review_id"],),
            ).fetchone()
        )


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
        init_db(root / "pebble.db")
        scheduler = MemoryReviewScheduler(memory_store=memory_store, path=root / "pebble.db")

        calls: list[tuple] = []
        original_apply = memory_store.apply

        def traced_apply(action, target, content, old_text):
            calls.append((action, target, content))
            return original_apply(action, target, content, old_text)

        memory_store.apply = traced_apply

        # ---------- 正例：5 轮含稳定信息的对话 ----------
        review_task = str(uuid4())
        with session(root / "pebble.db") as conn, write(conn):
            repository.insert_task(conn, review_task, "记忆回顾验收", timestamp())
            for user_text, assistant_text in SEED:
                seed_round(conn, review_task, user_text, assistant_text)
        before_items = timeline_count(review_task, root / "pebble.db")
        before_log = git_log(root)

        status = await run_review(root, gateway, scheduler, review_task)

        snapshot = memory_store.snapshot()
        user_md = snapshot["user"]["content"]
        memory_md = snapshot["memory"]["content"]
        assert "Hermes" in user_md, f"回顾未把学习方向写入 USER.md：{user_md!r} 调用={calls!r}"
        added = [call for call in calls if call[0] == "add"]
        assert len(added) >= 2, f"回顾新增少于两条：{calls!r}"
        after_log = git_log(root)
        add_commits = [s for s in after_log if s not in before_log]
        assert len(add_commits) == len(added), (
            f"新增条目数与 Git 提交数不一致：提交={add_commits!r} 调用={added!r}"
        )
        assert all("[Memory] Add" in subject for subject in add_commits), add_commits
        assert timeline_count(review_task, root / "pebble.db") == before_items, (
            "回顾改动了任务时间线"
        )
        assert status["status"] == "done", status

        # ---------- 负例：纯闲聊任务零新增 ----------
        chatter_task = str(uuid4())
        with session(root / "pebble.db") as conn, write(conn):
            repository.insert_task(conn, chatter_task, "闲聊", timestamp())
            for user_text, assistant_text in (
                ("今天天气怎么样？", "我看不到实时天气，建议看天气应用。"),
                ("那我先去散步了。", "好的，慢慢走。"),
                ("回来了，有点累。", "那就早点休息。"),
                ("晚饭吃什么好？", "看冰箱里有什么吧。"),
                ("还是点外卖算了。", "也行，省事。"),
            ):
                seed_round(conn, chatter_task, user_text, assistant_text)
        chatter_before_items = timeline_count(chatter_task, root / "pebble.db")
        chatter_before_log = git_log(root)
        chatter_calls = len(calls)

        chatter_status = await run_review(root, gateway, scheduler, chatter_task)

        assert len(calls) == chatter_calls, f"闲聊任务产生了记忆写入：{calls[chatter_calls:]!r}"
        assert git_log(root) == chatter_before_log, "闲聊任务产生了 Git 提交"
        assert timeline_count(chatter_task, root / "pebble.db") == chatter_before_items
        assert chatter_status["status"] == "done", chatter_status

        return {
            "review_status": status["status"],
            "chatter_status": chatter_status["status"],
            "user_md": user_md,
            "memory_md": memory_md,
            "added": added,
            "commits": add_commits,
        }
    finally:
        server.should_exit = True
        await serving


def main() -> None:
    with tempfile.TemporaryDirectory(prefix="pebble-qoder-memory-review-") as directory:
        report = asyncio.run(verify(Path(directory)))
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
