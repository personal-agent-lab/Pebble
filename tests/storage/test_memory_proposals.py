"""后台候选转前台：整批暂存、对话隔离、拒绝、过期与重新询问。"""

from uuid import uuid4

import pytest

from server.db import init_db, session, write
from server.errors import (
    MemoryValidationError,
    NotEditableError,
    NotFoundError,
    VersionConflictError,
)
from server.memory.proposals import MemoryProposals
from server.memory.service import MemoryStore
from server.sessions import runs, timeline
from server.sessions.service import SessionStore, timestamp
from server.tools.memory.tools import edit_memory, review_memory
from tests.support import memory_anchor, seed_memory


@pytest.fixture
def env(settings):
    init_db()
    store = MemoryStore(settings.data_dir)
    tasks = SessionStore(settings.db_path)
    source = tasks.create_task("原对话")["task_id"]
    seed_memory(store, "user", "旧偏好")
    proposals = MemoryProposals(store, settings.db_path)
    operations = [
        {"action": "delete", "anchor": memory_anchor(store, "旧偏好")},
        {"action": "append", "target": "memory", "text": "新约定"},
    ]
    candidate = proposals.stage(source, operations, "用户的长期要求改变")
    return store, tasks, proposals, source, operations, candidate


def start_user_turn(task_id, text="同意"):
    run_id = str(uuid4())
    with session() as conn, write(conn):
        runs.insert(conn, run_id, task_id, "message", {"message": text}, None, timestamp())
        runs.claim(conn, run_id, timestamp())
        timeline.insert_text(conn, task_id, run_id, "user", text)
    return run_id


def finish_turn(run_id):
    with session() as conn, write(conn):
        runs.finish(conn, run_id, "done", None, timestamp())


def test_stage_entire_batch_and_dedupes_and_survives_restart(env):
    store, tasks, proposals, source, operations, candidate = env
    assert store.snapshot()["user"]["content"] == "旧偏好"
    assert store.snapshot()["memory"]["content"] == ""
    again = MemoryProposals(store, tasks.path).stage(source, operations, "同一理由")
    assert again["proposal_id"] == candidate["proposal_id"]
    assert len(tasks.list_tasks()) == 2
    material = proposals.material(candidate["task_id"])
    assert "旧偏好" in material.content and "新约定" in material.content
    with session() as conn:
        question = conn.execute(
            "SELECT * FROM task_timeline_items WHERE task_id = ?", (candidate["task_id"],)
        ).fetchone()
        assert question["kind"] == "notice" and question["role"] is None
        assert "你同意" in question["text"]
        assert (
            conn.execute("SELECT COUNT(*) FROM agent_runs WHERE kind='message'").fetchone()[0] == 0
        )


def test_apply_only_from_candidate_user_turn_and_no_double_apply(env):
    store, tasks, proposals, source, operations, candidate = env
    with pytest.raises(NotFoundError):
        proposals.resolve(source, candidate["proposal_id"], "apply")
    with pytest.raises(MemoryValidationError):
        proposals.resolve(candidate["task_id"], candidate["proposal_id"], "apply")
    start_user_turn(candidate["task_id"])
    with pytest.raises(MemoryValidationError):
        edit_memory(operations, memory_store=store, tasks=tasks, task_id=candidate["task_id"])
    result = edit_memory(
        proposal_id=candidate["proposal_id"],
        decision="apply",
        memory_store=store,
        tasks=tasks,
        task_id=candidate["task_id"],
    )
    assert result["status"] == "applied"
    assert store.snapshot()["user"]["content"] == ""
    assert store.snapshot()["memory"]["content"] == "新约定"
    with pytest.raises(NotEditableError):
        proposals.resolve(candidate["task_id"], candidate["proposal_id"], "apply")


def test_reject_keeps_original(env):
    store, _, proposals, _, _, candidate = env
    start_user_turn(candidate["task_id"], "不同意")
    result = proposals.resolve(candidate["task_id"], candidate["proposal_id"], "reject")
    assert result["status"] == "rejected"
    assert store.snapshot()["user"]["content"] == "旧偏好"


def test_expired_version_requires_refresh_then_another_user_reply(env):
    store, tasks, proposals, _, _, candidate = env
    seed_memory(store, "user", "更新过的偏好")
    run_id = start_user_turn(candidate["task_id"])
    with pytest.raises(VersionConflictError):
        proposals.resolve(candidate["task_id"], candidate["proposal_id"], "apply")
    assert store.snapshot()["user"]["content"] == "更新过的偏好"
    operations = [
        {
            "action": "replace",
            "anchor": memory_anchor(store, "更新过的偏好"),
            "text": "用户重新确认的偏好",
        }
    ]
    result = proposals.resolve(
        candidate["task_id"], candidate["proposal_id"], "refresh", operations
    )
    assert "更新过的偏好" in result["question"]
    with pytest.raises(MemoryValidationError):
        proposals.resolve(candidate["task_id"], candidate["proposal_id"], "apply")
    finish_turn(run_id)
    start_user_turn(candidate["task_id"], "同意新的修改")
    proposals.resolve(candidate["task_id"], candidate["proposal_id"], "apply")
    assert store.snapshot()["user"]["content"] == "用户重新确认的偏好"
    assert proposals.for_task(candidate["task_id"])["status"] == "applied"


def test_invalid_batch_cannot_create_candidate_or_write(env):
    store, tasks, proposals, source, _, _ = env
    before = store.snapshot()
    with pytest.raises(MemoryValidationError):
        proposals.stage(
            source,
            [
                {"action": "append", "target": "user", "text": "不应写入"},
                {"action": "delete", "anchor": "zzzz"},
            ],
            "测试",
        )
    assert store.snapshot() == before and len(tasks.list_tasks()) == 2
    with pytest.raises(MemoryValidationError):
        review_memory(
            [{"action": "delete", "anchor": memory_anchor(store, "旧偏好")}],
            memory_store=store,
            tasks=tasks,
            task_id=source,
        )


def test_frontend_can_read_anchors_and_edit_on_regular_task(env):
    store, tasks, _, source, _, _ = env
    view = edit_memory(memory_store=store, tasks=tasks, task_id=source)
    assert "| 旧偏好" in view["memory"]["user"]["content"]
    edit_memory(
        [{"action": "delete", "anchor": memory_anchor(store, "旧偏好")}],
        memory_store=store,
        tasks=tasks,
        task_id=source,
    )
    assert store.snapshot()["user"]["content"] == ""


def test_user_can_adjust_candidate_in_foreground(env):
    store, tasks, proposals, _, _, candidate = env
    start_user_turn(candidate["task_id"], "保留原分区，只把偏好改成以下内容")
    edit_memory(
        [{"action": "replace", "anchor": memory_anchor(store, "旧偏好"), "text": "调整后的偏好"}],
        proposal_id=candidate["proposal_id"],
        decision="apply",
        memory_store=store,
        tasks=tasks,
        task_id=candidate["task_id"],
    )
    assert store.snapshot()["user"]["content"] == "调整后的偏好"
    assert store.snapshot()["memory"]["content"] == ""
    assert proposals.for_task(candidate["task_id"])["status"] == "applied"
