"""Skill 后台复盘的持久化窗口、重试与来源权限。"""

import json
from concurrent.futures import ThreadPoolExecutor

import pytest

from server.db import init_db, session, write
from server.skills.models import ChangeAction, ChangeActor, public_change_reason
from server.skills.review import (
    SkillReviewScheduler,
    record_completed_turn,
    resolve_evidence_refs,
    validate_review_reason,
)
from server.skills.service import ChangeRequest, SkillService

pytestmark = pytest.mark.anyio


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture
def pair(settings):
    init_db()
    skills = SkillService(settings.data_dir, settings.db_path)
    return skills, SkillReviewScheduler(skills, path=settings.db_path, interval=2)


def add_done(path, number: int, task: str | None = None) -> str:
    task_id = task or f"task-{number}"
    run_id = f"run-{number}"
    item_id = f"item-{number}"
    with session(path) as conn, write(conn):
        conn.execute(
            "INSERT OR IGNORE INTO tasks (task_id,goal,created_at,model) VALUES (?,?,?,'auto')",
            (task_id, "测试任务", "2026-01-01T00:00:00"),
        )
        conn.execute(
            "INSERT INTO agent_runs (run_id,task_id,kind,input,status,created_at,finished_at) "
            "VALUES (?,?,'message',?,'done',?,?)",
            (run_id, task_id, json.dumps({"message": "用户纠正顺序"}), "2026-01-01", "2026-01-01"),
        )
        conn.execute(
            "INSERT INTO task_timeline_items "
            "(item_id,task_id,run_id,sequence,kind,role,text,created_at) "
            "VALUES (?,?,?,?,'text','user',?,?)",
            (item_id, task_id, run_id, number, "先检查输入，再生成结果", "2026-01-01"),
        )
        record_completed_turn(conn, run_id, task_id)
    return item_id


class FakeReview:
    def __init__(self, candidates=None, error=None):
        self.candidates = candidates or []
        self.error = error
        self.calls = []

    async def review_skills(
        self, review_id, anchor_task_id, instructions, material, model, evidence_refs
    ):
        self.calls.append((review_id, material, evidence_refs))
        if self.error:
            raise self.error
        return self.candidates


def test_review_reason_keeps_internal_refs_out_of_user_text():
    refs = {"E4": "123e4567-e89b-12d3-a456-426614174000", "E6": "item-6"}
    for reason in (
        "用户在 E4 明确纠正，做法在 E6 的新记录上直接适用。",
        "用户在E4明确纠正，做法在E6的新记录上直接适用。",
        "依据 123e4567-e89b-12d3-a456-426614174000 修改。",
        "依据 item-6 修改。",
    ):
        with pytest.raises(ValueError, match="直接展示给用户"):
            validate_review_reason(reason, refs)
    validate_review_reason(
        "用户纠正后，只列已确认事项；后续任务采用此做法并获得认可。", refs
    )
    historical = "用户在 E4 明确纠正；做法在 E6 的新记录上适用并被接受（E7）。"
    displayed = public_change_reason(historical, "review")
    assert "E4" not in displayed and "E6" not in displayed and "E7" not in displayed
    assert "明确纠正" in displayed and "适用并被接受" in displayed
    assert public_change_reason(historical, "user") == historical


async def test_cross_task_trigger_and_empty_review_advances(pair, settings):
    _, scheduler = pair
    add_done(settings.db_path, 1)
    assert scheduler.enqueue_if_due() is None
    add_done(settings.db_path, 2)
    job = scheduler.enqueue_if_due()
    assert job["from_seq"] == 0 and job["through_seq"] == 2
    assert scheduler.enqueue_if_due() is None
    scheduler.claim(job["id"])
    gateway = FakeReview()
    await scheduler.run(job["id"], gateway)
    assert "[E1]" in gateway.calls[0][1]
    assert "[E2]" in gateway.calls[0][1]
    assert gateway.calls[0][2] == {"E1": "item-1", "E2": "item-2"}
    assert resolve_evidence_refs(["E2", "E1", "E2"], gateway.calls[0][2]) == [
        "item-2", "item-1"
    ]
    with pytest.raises(ValueError, match="无效轨迹依据编号"):
        resolve_evidence_refs(["E3"], gateway.calls[0][2])
    assert scheduler.get(job["id"])["status"] == "completed"
    add_done(settings.db_path, 3)
    assert scheduler.enqueue_if_due() is None
    add_done(settings.db_path, 4)
    assert scheduler.enqueue_if_due()["from_seq"] == 2


async def test_concurrent_enqueues_keep_one_open_window(pair, settings):
    _, scheduler = pair
    add_done(settings.db_path, 1)
    add_done(settings.db_path, 2)
    with ThreadPoolExecutor(max_workers=2) as pool:
        jobs = list(pool.map(lambda _: scheduler.enqueue_if_due(), range(2)))
    assert sum(job is not None for job in jobs) == 1
    with session(settings.db_path) as conn:
        assert (
            conn.execute("SELECT COUNT(*) FROM skill_reviews WHERE status='pending'").fetchone()[0]
            == 1
        )


async def test_failure_retries_original_window_after_next_completion(pair, settings):
    _, scheduler = pair
    add_done(settings.db_path, 1)
    add_done(settings.db_path, 2)
    job = scheduler.enqueue_if_due()
    scheduler.claim(job["id"])
    with pytest.raises(RuntimeError):
        await scheduler.run(job["id"], FakeReview(error=RuntimeError("provider failed")))
    scheduler.fail(job["id"], "provider failed")
    assert scheduler.enqueue_if_due() is None
    add_done(settings.db_path, 3)
    retry = scheduler.enqueue_if_due()
    assert (retry["from_seq"], retry["through_seq"]) == (0, 2)
    scheduler.claim(retry["id"])
    await scheduler.run(retry["id"], FakeReview())
    assert scheduler.enqueue_if_due() is None


async def test_review_creates_skill_with_window_evidence(pair, settings):
    skills, scheduler = pair
    add_done(settings.db_path, 1)
    add_done(settings.db_path, 2)
    job = scheduler.enqueue_if_due()
    scheduler.claim(job["id"])
    candidate = {
        "action": "create",
        "payload": {
            "skill_id": "report-order",
            "name": "报表顺序",
            "description": "按稳定顺序整理报表",
            "body": "先检查输入，再生成结果。",
        },
        "reason": "用户纠正后按此顺序成功完成",
        "evidence_item_ids": ["item-2"],
    }
    await scheduler.run(job["id"], FakeReview([candidate]))
    assert skills.get("report-order").origin.value == "review"
    assert skills.get("report-order").managed is True
    assert skills.changes()[0]["evidence_item_ids"] == ["item-2"]
    assert skills.repository.versions("report-order")[0].change_id == skills.changes()[0]["id"]
    with session(settings.db_path) as conn, write(conn):
        conn.execute(
            "UPDATE skill_changes SET reason=? WHERE id=?",
            ("用户在 E2 纠正后成功完成", skills.changes()[0]["id"]),
        )
    assert "E2" not in skills.changes()[0]["reason"]


async def test_review_does_not_persist_candidate_with_internal_reason(pair, settings):
    skills, scheduler = pair
    add_done(settings.db_path, 1)
    add_done(settings.db_path, 2)
    job = scheduler.enqueue_if_due()
    scheduler.claim(job["id"])
    candidate = {
        "action": "create",
        "payload": {
            "skill_id": "report-order",
            "name": "报表顺序",
            "description": "按稳定顺序整理报表",
            "body": "先检查输入，再生成结果。",
        },
        "reason": "用户在 E2 纠正后成功完成",
        "evidence_item_ids": ["item-2"],
    }
    with pytest.raises(ValueError, match="直接展示给用户"):
        await scheduler.run(job["id"], FakeReview([candidate]))
    assert skills.changes() == []
    with session(settings.db_path) as conn:
        assert conn.execute(
            "SELECT COUNT(*) FROM skill_review_candidates WHERE review_id=?", (job["id"],)
        ).fetchone()[0] == 0


async def test_user_skill_gets_proposal_and_out_of_window_evidence_is_rejected(pair, settings):
    skills, scheduler = pair
    skills.record_change(
        ChangeRequest(
            action=ChangeAction.CREATE,
            payload={
                "skill_id": "owned",
                "name": "用户流程",
                "description": "用户写的流程",
                "body": "旧步骤",
            },
            actor=ChangeActor.USER,
            reason="用户创建",
        )
    )
    add_done(settings.db_path, 1)
    add_done(settings.db_path, 2)
    job = scheduler.enqueue_if_due()
    scheduler.claim(job["id"])
    candidate = {
        "action": "patch",
        "payload": {"skill_id": "owned", "body": "新步骤"},
        "expected_revision": skills.get("owned").revision,
        "reason": "用户纠正了旧步骤",
        "evidence_item_ids": ["item-1"],
    }
    await scheduler.run(job["id"], FakeReview([candidate]))
    assert skills.get("owned").body == "旧步骤"
    assert skills.changes(status="proposed")[0]["review_job_id"] == job["id"]

    add_done(settings.db_path, 3)
    add_done(settings.db_path, 4)
    next_job = scheduler.enqueue_if_due()
    scheduler.claim(next_job["id"])
    candidate["evidence_item_ids"] = ["item-1"]
    await scheduler.run(next_job["id"], FakeReview([candidate]))
    with session(settings.db_path) as conn:
        status = conn.execute(
            "SELECT status FROM skill_review_candidates WHERE review_id=?", (next_job["id"],)
        ).fetchone()[0]
    assert status == "failed"
    assert scheduler.get(next_job["id"])["result_summary"] == "复盘完成，1 条候选未应用"


async def test_applying_resume_does_not_duplicate_change(pair, settings):
    skills, scheduler = pair
    add_done(settings.db_path, 1)
    add_done(settings.db_path, 2)
    job = scheduler.enqueue_if_due()
    scheduler.claim(job["id"])
    candidate = {
        "action": "create",
        "payload": {
            "skill_id": "once",
            "name": "一次",
            "description": "只创建一次",
            "body": "方法",
        },
        "reason": "成功流程",
        "evidence_item_ids": ["item-2"],
    }
    await scheduler.run(job["id"], FakeReview([candidate]))
    with session(settings.db_path) as conn, write(conn):
        conn.execute("UPDATE skill_reviews SET status='applying' WHERE id=?", (job["id"],))
        conn.execute(
            "UPDATE skill_review_candidates SET status='pending' WHERE review_id=?", (job["id"],)
        )
    restarted = SkillReviewScheduler(skills, path=settings.db_path, interval=2)
    restarted.recover()
    await restarted.run(job["id"], FakeReview(error=RuntimeError("model must not rerun")))
    assert len(skills.repository.versions("once")) == 1


async def test_user_write_resets_count_and_stale_review_cannot_apply(pair, settings):
    skills, scheduler = pair
    first_item = add_done(settings.db_path, 1)
    add_done(settings.db_path, 2)
    job = scheduler.enqueue_if_due()
    scheduler.claim(job["id"])
    skills.record_change(
        ChangeRequest(
            action=ChangeAction.CREATE,
            payload={
                "skill_id": "new-user-skill",
                "name": "用户技能",
                "description": "手工创建",
                "body": "用户自己的步骤",
            },
            actor=ChangeActor.USER,
            reason="手工创建",
        )
    )
    candidate = {
        "action": "create",
        "payload": {
            "skill_id": "stale-review-skill",
            "name": "过期经验",
            "description": "不应落盘",
            "body": "旧步骤",
        },
        "reason": "旧窗口",
        "evidence_item_ids": [first_item],
    }
    await scheduler.run(job["id"], FakeReview([candidate]))
    assert skills.repository.load("stale-review-skill") is None
    add_done(settings.db_path, 3)
    assert scheduler.enqueue_if_due() is None
    add_done(settings.db_path, 4)
    assert scheduler.enqueue_if_due()["from_seq"] == 2


async def test_revision_conflict_keeps_newer_skill(pair, settings):
    skills, scheduler = pair
    skills.record_change(
        ChangeRequest(
            action=ChangeAction.CREATE,
            payload={
                "skill_id": "shared",
                "name": "共用流程",
                "description": "已有的流程",
                "body": "初版",
            },
            actor=ChangeActor.FOREGROUND,
            reason="明确学习",
        )
    )
    old_revision = skills.get("shared").revision
    add_done(settings.db_path, 1)
    add_done(settings.db_path, 2)
    job = scheduler.enqueue_if_due()
    scheduler.claim(job["id"])
    skills.record_change(
        ChangeRequest(
            action=ChangeAction.PATCH,
            payload={"skill_id": "shared", "body": "用户的新版本"},
            actor=ChangeActor.USER,
            reason="用户修改",
            skill_id="shared",
            base_revision=old_revision,
        )
    )
    candidate = {
        "action": "patch",
        "payload": {"skill_id": "shared", "body": "复盘旧版本"},
        "expected_revision": old_revision,
        "reason": "旧步骤需要补充",
        "evidence_item_ids": ["item-1"],
    }
    await scheduler.run(job["id"], FakeReview([candidate]))
    assert skills.get("shared").body == "用户的新版本"
    with session(settings.db_path) as conn:
        status = conn.execute(
            "SELECT status FROM skill_review_candidates WHERE review_id=?", (job["id"],)
        ).fetchone()[0]
    assert status == "conflict"
