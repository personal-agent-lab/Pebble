import json

import pytest

from server.db import init_db, session, write
from server.errors import SkillValidationError
from server.sessions.service import SessionStore
from server.skills import evidence
from server.skills.runtime import Scope, current
from server.skills.tools import skill_find_evidence, skill_propose
from server.tools.registry import default_registry


def add_run(task_id, run_id, *, status="done", kind="message", message="完成工作"):
    with session() as conn, write(conn):
        conn.execute(
            "INSERT INTO agent_runs(run_id,task_id,kind,input,status,created_at,finished_at) "
            "VALUES (?,?,?,?,?,'now',?)",
            (
                run_id,
                task_id,
                kind,
                json.dumps({"message": message}),
                status,
                None if status in {"pending", "running"} else "now",
            ),
        )


def test_user_can_propose_from_one_completed_work(settings):
    init_db()
    task_id = SessionStore().create_task("整理资料")["task_id"]
    add_run(task_id, "source")
    add_run(task_id, "request", status="running", message="把刚才整理资料的工作总结为 skill")
    token = current.set(Scope("request", set(), True, set()))
    try:
        found = skill_find_evidence()
        assert found["task_id"] == task_id
        assert [run["run_id"] for run in found["completed_runs"]] == ["source"]
        first = skill_propose("资料整理", "整理资料的步骤", "先读取资料，再核对结果")
        second = skill_propose("资料整理", "整理资料的步骤", "先读取资料，再核对结果")
        assert first["draft_id"] == second["draft_id"]
        assert first["status"] == "draft"
        distinct = skill_propose("资料整理", "整理资料的步骤", "另一项工作的流程")
        assert distinct["draft_id"] != first["draft_id"]
    finally:
        current.reset(token)


def test_selected_past_task_and_failed_work(settings):
    init_db()
    past = SessionStore().create_task("过去的工作")["task_id"]
    failed = SessionStore().create_task("失败的工作")["task_id"]
    current_task = SessionStore().create_task("总结")["task_id"]
    add_run(past, "past_done")
    add_run(failed, "past_failed", status="error")
    add_run(current_task, "request", status="running", message="把过去的工作总结为 Skill")
    token = current.set(Scope("request", set(), True, set()))
    try:
        assert evidence.verify(past) == {"tasks": [past], "occurrences": 1, "last_seen_at": "now"}
        with pytest.raises(SkillValidationError, match="尚无已完成记录"):
            evidence.verify(failed)
        with pytest.raises(SkillValidationError, match="尚无已完成记录"):
            evidence.verify(current_task)
        with pytest.raises(SkillValidationError, match="不超过 30"):
            skill_propose("过长标题" * 8, "描述", "步骤", source_task_id=past)
    finally:
        current.reset(token)


def test_non_user_turn_cannot_propose(settings):
    init_db()
    task_id = SessionStore().create_task("工作")["task_id"]
    add_run(task_id, "source")
    add_run(task_id, "system", status="running", kind="execution_result")
    token = current.set(Scope("system", set(), True, set()))
    try:
        with pytest.raises(SkillValidationError, match="用户对话轮"):
            evidence.verify(task_id)
    finally:
        current.reset(token)


def test_past_task_selector_is_visible_to_agent():
    definition = default_registry.get_tool("skill_find_evidence")
    assert "source_task_id" in definition.parameters_schema["properties"]
    assert "task_id" not in definition.parameters_schema["properties"]
