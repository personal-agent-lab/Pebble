"""后台记忆回顾的存储与登记规则：只新增工具、触发计数、窗口冻结与消息组装。"""

import asyncio
import sqlite3
from uuid import uuid4

import pytest

from server.agent.toolset import ToolDeps, build_tools
from server.db import init_db, session, write
from server.errors import NotFoundError
from server.memory.review import (
    REVIEW_MESSAGE_HEADER,
    MemoryReviewScheduler,
    build_review_message,
    done_message_count_since,
    last_through_rowid,
    render_transcript,
    window_text_items,
)
from server.memory.service import MemoryStore
from server.sessions import repository, timeline
from server.sessions import runs as repo
from server.sessions.service import timestamp
from server.tools.memory.tools import review_registry


def review_tools(store):
    return build_tools(
        ToolDeps(drafts=None, tasks=None, gmail=None, memory_store=store),
        registry=review_registry,
    )


@pytest.fixture
def store(settings):
    return MemoryStore(settings.data_dir)


@pytest.fixture
def scheduler(settings, store):
    init_db()
    return MemoryReviewScheduler(memory_store=store)


def add_run(conn, task_id: str, kind: str, *, status: str = "done") -> str:
    run_id = str(uuid4())
    repo.insert(conn, run_id, task_id, kind, {"message": "文本"}, None, timestamp())
    if status != "pending":
        finished = timestamp() if status in ("done", "error", "interrupted") else None
        conn.execute(
            "UPDATE agent_runs SET status = ?, started_at = ?, finished_at = ? WHERE run_id = ?",
            (status, timestamp(), finished, run_id),
        )
    return run_id


def make_task(conn) -> str:
    task_id = str(uuid4())
    repository.insert_task(conn, task_id, "任务", timestamp())
    return task_id


def seed_round(conn, task_id: str, text: str) -> str:
    run_id = add_run(conn, task_id, repo.KIND_MESSAGE)
    timeline.insert_text(conn, task_id, run_id, "user", text)
    timeline.append_assistant_text(conn, task_id, run_id, f"回复 {text}")
    return run_id


# ---------- 只新增工具 ----------


def test_review_registry_binds_only_add_tool(store):
    tools = review_tools(store)
    assert [tool.name for tool in tools] == ["memory_add"]

    result = tools[0]("user", "用户在研究记忆机制")
    assert result["changed"] is True
    assert "用户在研究记忆机制" in store.snapshot()["user"]["content"]


def test_add_tool_rejects_duplicates_without_commit(store):
    tools = review_tools(store)
    first = tools[0]("memory", "项目使用 SQLite")
    second = tools[0]("memory", "项目使用 SQLite")
    assert first["changed"] is True and second["changed"] is False
    subjects = store._git("log", "--format=%s").stdout.splitlines()
    assert subjects.count("[Memory] Add memory entry") == 1


def test_review_registry_has_no_foreground_memory_tool():
    assert review_registry.get_tool("memory") is None


# ---------- 触发计数 ----------


def max_run_rowid(conn, task_id: str) -> int:
    rows = conn.execute("SELECT rowid FROM agent_runs WHERE task_id = ?", (task_id,))
    return max(r["rowid"] for r in rows)


def test_enqueue_if_due_counts_only_done_message_runs(scheduler):
    with session() as conn:
        task_id = make_task(conn)
        for _ in range(4):
            add_run(conn, task_id, repo.KIND_MESSAGE)
        add_run(conn, task_id, repo.KIND_NEW_MAIL)
        add_run(conn, task_id, repo.KIND_EXECUTION_RESULT)
        add_run(conn, task_id, repo.KIND_MESSAGE, status="error")
        add_run(conn, task_id, repo.KIND_MESSAGE, status="pending")
    assert scheduler.enqueue_if_due(task_id) is None

    with session() as conn:
        add_run(conn, task_id, repo.KIND_MESSAGE)
    row = scheduler.enqueue_if_due(task_id)
    assert row is not None and (row["status"], row["origin"]) == ("pending", "interval")
    assert row["from_rowid"] == 0
    with session() as conn:
        assert row["through_rowid"] == max_run_rowid(conn, task_id)
        assert done_message_count_since(conn, task_id, row["through_rowid"]) == 0


def test_enqueue_if_due_skips_when_disabled(settings, store):
    init_db()
    scheduler = MemoryReviewScheduler(memory_store=store, enabled=False)
    with session() as conn:
        task_id = make_task(conn)
        for _ in range(6):
            add_run(conn, task_id, repo.KIND_MESSAGE)
    assert scheduler.enqueue_if_due(task_id) is None


def test_interval_counts_from_last_done_review_only(scheduler):
    with session() as conn:
        task_id = make_task(conn)
        for _ in range(5):
            add_run(conn, task_id, repo.KIND_MESSAGE)
    first = scheduler.enqueue_if_due(task_id)
    assert first is not None
    # 已有未完成回顾时不重复登记。
    assert scheduler.enqueue_if_due(task_id) is None

    # 完成后窗口下界推进到它的 through_rowid，计数从那里重新起算。
    with session() as conn, write(conn):
        conn.execute(
            "UPDATE memory_reviews SET status = 'done', finished_at = ? WHERE review_id = ?",
            (timestamp(), first["review_id"]),
        )
    with session() as conn:
        assert last_through_rowid(conn, task_id) == first["through_rowid"]
        for _ in range(4):
            add_run(conn, task_id, repo.KIND_MESSAGE)
    assert scheduler.enqueue_if_due(task_id) is None
    with session() as conn:
        add_run(conn, task_id, repo.KIND_MESSAGE)
    second = scheduler.enqueue_if_due(task_id)
    assert second is not None and second["from_rowid"] == first["through_rowid"]


def test_interrupted_or_errored_review_does_not_advance_window(scheduler):
    with session() as conn:
        task_id = make_task(conn)
        for _ in range(5):
            add_run(conn, task_id, repo.KIND_MESSAGE)
    first = scheduler.enqueue_if_due(task_id)
    with session() as conn, write(conn):
        conn.execute(
            "UPDATE memory_reviews SET status = 'interrupted', finished_at = ? WHERE review_id = ?",
            (timestamp(), first["review_id"]),
        )
    with session() as conn:
        assert last_through_rowid(conn, task_id) == 0
    retried = scheduler.enqueue_if_due(task_id)
    assert retried is not None and retried["from_rowid"] == 0


def test_enqueue_manual_covers_whole_task_and_dedupes(scheduler):
    with session() as conn:
        task_id = make_task(conn)
        seed_round(conn, task_id, "一")
        add_run(conn, task_id, repo.KIND_NEW_MAIL)
    row = scheduler.enqueue_manual(task_id)
    assert (row["origin"], row["from_rowid"]) == ("manual", 0)
    with session() as conn:
        assert row["through_rowid"] == max_run_rowid(conn, task_id)
    again = scheduler.enqueue_manual(task_id)
    assert again["review_id"] == row["review_id"]


def test_open_review_unique_index_rejects_second_insert(scheduler):
    with session() as conn:
        task_id = make_task(conn)
        seed_round(conn, task_id, "一")
    scheduler.enqueue_manual(task_id)
    with session() as conn, write(conn), pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO memory_reviews (review_id, task_id, status, origin, from_rowid, "
            "through_rowid, created_at) VALUES ('r2', ?, 'pending', 'manual', 0, 0, ?)",
            (task_id, timestamp()),
        )


def test_enqueue_manual_rejects_unknown_task(scheduler):
    with pytest.raises(NotFoundError):
        scheduler.enqueue_manual("missing-task")


# ---------- 窗口与消息组装 ----------


def test_window_text_items_bounds_and_kinds(scheduler):
    with session() as conn:
        task_id = make_task(conn)
        first = seed_round(conn, task_id, "第一轮")
        second = seed_round(conn, task_id, "第二轮")
        timeline.insert_notice(conn, task_id, second, "已记住：第二轮")
        error_run = add_run(conn, task_id, repo.KIND_MESSAGE)
        timeline.insert_error(conn, task_id, error_run, "出错了")
        with write(conn):
            operation_id = str(uuid4())
            repository.insert_operation(conn, operation_id, "mail", task_id, timestamp())
            timeline.ensure_mail_draft(conn, task_id, error_run, operation_id)
        seed_round(conn, task_id, "第三轮")
        from_rowid = conn.execute(
            "SELECT rowid FROM agent_runs WHERE run_id = ?", (first,)
        ).fetchone()["rowid"]
        through_rowid = conn.execute(
            "SELECT rowid FROM agent_runs WHERE run_id = ?", (second,)
        ).fetchone()["rowid"]

        items = window_text_items(conn, task_id, from_rowid, through_rowid)
        empty = window_text_items(conn, task_id, through_rowid, through_rowid)
    assert items == [
        {"kind": "text", "role": "user", "text": "第二轮"},
        {"kind": "text", "role": "assistant", "text": "回复 第二轮"},
        {"kind": "notice", "role": None, "text": "已记住：第二轮"},
    ]
    assert empty == []


def test_render_transcript_labels_notice_as_system():
    items = [
        {"role": "user", "text": "你好"},
        {"kind": "notice", "role": None, "text": "已记住：你好"},
        {"role": "assistant", "text": "你好！"},
    ]
    assert render_transcript(items) == "用户：你好\n系统：已记住：你好\n助手：你好！"


def test_render_transcript_labels_roles():
    items = [
        {"role": "user", "text": "你好"},
        {"role": "assistant", "text": "你好！"},
    ]
    assert render_transcript(items) == "用户：你好\n助手：你好！"


def test_build_review_message_renders_materials(store):
    store.apply("add", "user", "已有画像")
    message = build_review_message("用户：新信息", store.snapshot())
    assert message.startswith(REVIEW_MESSAGE_HEADER)
    assert "## 自上次回顾以来的任务对话\n用户：新信息" in message
    assert "## 当前长期记忆：关于你\n已有画像" in message
    assert "## 当前长期记忆：事实与约定\n（空）" in message


def test_build_review_message_with_empty_transcript_and_memory(store):
    message = build_review_message("", store.snapshot())
    assert "（无新增对话）" in message
    assert "（空）" in message


# ---------- 执行 ----------


class RecordingGateway:
    def __init__(self):
        self.calls = []

    async def review_memory(self, task_id, instructions, transcript):
        self.calls.append((task_id, instructions, transcript))
        return "无"


def test_run_finishes_without_model_call_when_window_is_empty(scheduler):
    with session() as conn:
        task_id = make_task(conn)
    row = scheduler.enqueue_manual(task_id)  # 无任何对话
    assert scheduler.claim(row["review_id"])["status"] == "running"
    gateway = RecordingGateway()
    asyncio.run(scheduler.run(row["review_id"], gateway))
    assert gateway.calls == []
    with session() as conn:
        status = conn.execute(
            "SELECT status, finished_at FROM memory_reviews WHERE review_id = ?",
            (row["review_id"],),
        ).fetchone()
    assert status["status"] == "done" and status["finished_at"]


def test_run_passes_window_and_memory_snapshot_to_gateway(scheduler):
    with session() as conn:
        task_id = make_task(conn)
        seed_round(conn, task_id, "我最近正在学习 Hermes 的设计")
    row = scheduler.enqueue_manual(task_id)
    assert scheduler.claim(row["review_id"])["status"] == "running"
    gateway = RecordingGateway()
    asyncio.run(scheduler.run(row["review_id"], gateway))
    (called_task, instructions, transcript), = gateway.calls
    assert called_task == task_id
    assert "memory_add" in instructions
    assert "我最近正在学习 Hermes 的设计" in transcript
    with session() as conn:
        status = conn.execute(
            "SELECT status FROM memory_reviews WHERE review_id = ?", (row["review_id"],)
        ).fetchone()
    assert status["status"] == "done"


def test_claim_and_finish_transition(scheduler):
    with session() as conn:
        task_id = make_task(conn)
        seed_round(conn, task_id, "一")
    row = scheduler.enqueue_manual(task_id)
    assert scheduler.claim(row["review_id"])["status"] == "running"
    assert scheduler.claim(row["review_id"]) is None  # 只有 pending 能取得运行权
    scheduler.fail(row["review_id"], "记忆回顾失败：模型不可用")
    with session() as conn:
        record = conn.execute(
            "SELECT status, error FROM memory_reviews WHERE review_id = ?", (row["review_id"],)
        ).fetchone()
    assert record["status"] == "error"
    assert "记忆回顾失败" in record["error"]
