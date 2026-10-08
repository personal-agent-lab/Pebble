"""全局记忆回顾：跨任务计数、冻结窗口、成功检查点和计划提交。"""

import asyncio

import pytest

from server.db import init_db, session, write
from server.memory.review import (
    MemoryReviewScheduler,
    build_review_message,
    finish_review,
    render_transcript,
    sync_completed_turns,
    window_text_items,
)
from server.memory.service import MemoryStore
from server.sessions import runs, timeline
from server.sessions.service import SessionStore, timestamp
from server.tools.memory.tools import review_memory
from tests.support import memory_anchor, seed_memory


@pytest.fixture
def environment(settings):
    init_db()
    store = MemoryStore(settings.data_dir)
    tasks = SessionStore(settings.db_path)
    scheduler = MemoryReviewScheduler(memory_store=store, path=settings.db_path)
    return store, tasks, scheduler


def seed_round(task_id, text, kind="message", status="done"):
    from uuid import uuid4

    run_id = str(uuid4())
    with session() as conn, write(conn):
        runs.insert(conn, run_id, task_id, kind, {"message": text}, None, timestamp())
        if status != "pending":
            runs.claim(conn, run_id, timestamp())
        if status in {"done", "error", "interrupted"}:
            runs.finish(conn, run_id, status, None, timestamp())
        timeline.insert_text(conn, task_id, run_id, "user", text)
        timeline.append_assistant_text(conn, task_id, run_id, f"回复 {text}")
    return run_id


class RecordingGateway:
    def __init__(self, records=None):
        self.records = records or []
        self.calls = []

    async def review_memory(self, task_id, instructions, transcript):
        self.calls.append(transcript)
        return self.records


def test_default_ten_rounds_cross_tasks_excludes_failed_and_system_rounds(environment):
    store, tasks, scheduler = environment
    first, second = (tasks.create_task(name)["task_id"] for name in ("甲", "乙"))
    for n in range(5):
        seed_round(first, f"甲{n}")
    for n in range(4):
        seed_round(second, f"乙{n}")
    seed_round(second, "失败", status="error")
    seed_round(second, "中断", status="interrupted")
    seed_round(second, "回报", kind="execution_result")
    seed_round(second, "邮件", kind="new_mail")
    assert scheduler.enqueue_if_due(second) is None
    seed_round(second, "乙最后")
    row = scheduler.enqueue_if_due(second)
    assert row["scope"] == "global"
    assert scheduler.enqueue_if_due(first) is None
    seed_round(first, "窗口外")
    scheduler.claim(row["review_id"])
    gateway = RecordingGateway()
    asyncio.run(scheduler.run(row["review_id"], gateway))
    prompt = gateway.calls[0]
    assert f"[任务 {first}]" in prompt and f"[任务 {second}]" in prompt
    assert "甲0" in prompt and "乙最后" in prompt and "回报" in prompt
    assert "失败" not in prompt and "中断" not in prompt and "窗口外" not in prompt
    assert scheduler.enqueue_if_due(first) is None


def test_checkpoint_survives_anchor_task_deletion_and_restart(environment, settings):
    store, tasks, scheduler = environment
    first = tasks.create_task("甲")["task_id"]
    for n in range(10):
        seed_round(first, f"旧{n}")
    row = scheduler.enqueue_if_due(first)
    scheduler.claim(row["review_id"])
    asyncio.run(scheduler.run(row["review_id"], RecordingGateway()))
    tasks.delete_task(first)
    scheduler = MemoryReviewScheduler(memory_store=store, path=settings.db_path)
    second = tasks.create_task("乙")["task_id"]
    for n in range(9):
        seed_round(second, f"新{n}")
    assert scheduler.enqueue_if_due(second) is None
    seed_round(second, "新最后")
    row = scheduler.enqueue_if_due(second)
    assert row["from_rowid"] == 10
    scheduler.claim(row["review_id"])
    gateway = RecordingGateway()
    asyncio.run(scheduler.run(row["review_id"], gateway))
    assert "新最后" in gateway.calls[0] and "旧0" not in gateway.calls[0]


def test_failed_review_does_not_advance_checkpoint(environment):
    _, tasks, scheduler = environment
    task_id = tasks.create_task("甲")["task_id"]
    seed_round(task_id, "一")
    row = scheduler.enqueue_manual(task_id)
    scheduler.claim(row["review_id"])
    scheduler.fail(row["review_id"], "失败")
    next_row = scheduler.enqueue_manual(task_id)
    assert next_row["from_rowid"] == 0


def test_manual_review_is_global_and_reuses_pending(environment):
    _, tasks, scheduler = environment
    first, second = (tasks.create_task(name)["task_id"] for name in ("甲", "乙"))
    seed_round(first, "一")
    seed_round(second, "二")
    row = scheduler.enqueue_manual(first)
    assert scheduler.enqueue_manual(second)["review_id"] == row["review_id"]
    scheduler.claim(row["review_id"])
    gateway = RecordingGateway()
    asyncio.run(scheduler.run(row["review_id"], gateway))
    assert "用户：一" in gateway.calls[0] and "用户：二" in gateway.calls[0]


def test_multiple_tool_plans_stage_whole_review_without_early_add(environment):
    store, tasks, scheduler = environment
    task_id = tasks.create_task("甲")["task_id"]
    seed_round(task_id, "请简洁")
    seed_memory(store, "user", "旧偏好")
    add = review_memory(
        [{"action": "append", "target": "user", "text": "新偏好"}],
        memory_store=store,
        tasks=tasks,
        task_id=task_id,
    )
    remove = review_memory(
        [{"action": "delete", "anchor": memory_anchor(store, "旧偏好")}],
        "更新过时偏好",
        memory_store=store,
        tasks=tasks,
        task_id=task_id,
    )
    assert store.snapshot()["user"]["content"] == "旧偏好"
    row = scheduler.enqueue_manual(task_id)
    scheduler.claim(row["review_id"])
    notices = asyncio.run(
        scheduler.run(
            row["review_id"],
            RecordingGateway(
                [
                    {"result": add},
                    {"result": remove},
                ]
            ),
        )
    )
    assert store.snapshot()["user"]["content"] == "旧偏好"
    assert len(tasks.list_tasks()) == 2
    assert len(notices) == 1 and "新对话" in notices[0]["text"]


def test_add_plan_applied_only_after_success_and_stale_plan_refused(environment):
    store, tasks, scheduler = environment
    task_id = tasks.create_task("甲")["task_id"]
    seed_round(task_id, "一")
    plan = review_memory(
        [{"action": "append", "target": "user", "text": "长期目标"}],
        memory_store=store,
        tasks=tasks,
        task_id=task_id,
    )
    assert store.snapshot()["user"]["content"] == ""
    row = scheduler.enqueue_manual(task_id)
    scheduler.claim(row["review_id"])
    asyncio.run(scheduler.run(row["review_id"], RecordingGateway([{"result": plan}])))
    assert store.snapshot()["user"]["content"] == "长期目标"
    seed_round(task_id, "二")
    row = scheduler.enqueue_manual(task_id)
    scheduler.claim(row["review_id"])
    from server.errors import MemoryValidationError

    with pytest.raises(MemoryValidationError):
        asyncio.run(scheduler.run(row["review_id"], RecordingGateway([{"result": plan}])))


def test_window_only_reads_done_text_and_notice(environment):
    store, tasks, _ = environment
    task_id = tasks.create_task("甲")["task_id"]
    seed_round(task_id, "一")
    seed_round(task_id, "失败", status="error")
    with session() as conn, write(conn):
        sync_completed_turns(conn)
        items = window_text_items(conn, 0, 100)
    assert [item["text"] for item in items] == ["一", "回复 一"]
    assert "各任务" in build_review_message(render_transcript(items), store.snapshot())


def test_finish_review_requires_claim_and_advances_success_only(environment):
    _, tasks, scheduler = environment
    task_id = tasks.create_task("甲")["task_id"]
    seed_round(task_id, "一")
    row = scheduler.enqueue_manual(task_id)
    assert scheduler.claim(row["review_id"])["status"] == "running"
    assert scheduler.claim(row["review_id"]) is None
    with session() as conn, write(conn):
        finish_review(conn, row["review_id"], "done", None, timestamp())
        assert conn.execute("SELECT through_rowid FROM memory_review_checkpoint").fetchone()[0] == 1
