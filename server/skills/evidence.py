"""用户指定的已完成工作，以及不含参数值的工具执行记录。"""

import json
from uuid import uuid4

from server.db import session, write
from server.errors import SkillValidationError
from server.sessions.service import timestamp
from server.skills.runtime import current


def record(tool_name: str, arguments: dict, succeeded: bool) -> None:
    scope = current.get()
    if scope is None or not scope.run_id or tool_name.startswith("skill_"):
        return
    with session(scope.path) as conn, write(conn):
        conn.execute(
            "INSERT INTO skill_tool_evidence VALUES (?, ?, ?, ?, ?, ?)",
            (
                uuid4().hex,
                scope.run_id,
                tool_name,
                json.dumps(sorted(arguments)),
                int(succeeded),
                timestamp(),
            ),
        )


def find(task_id: str | None = None) -> dict:
    """列出指定任务已完成的轮次；默认查看当前对话的任务。"""
    scope = current.get()
    with session(scope.path if scope else None) as conn:
        if task_id is None and scope and scope.run_id:
            row = conn.execute(
                "SELECT task_id FROM agent_runs WHERE run_id = ?", (scope.run_id,)
            ).fetchone()
            task_id = row["task_id"] if row else None
        if not task_id:
            return {"task_id": None, "completed_runs": []}
        task = conn.execute("SELECT goal FROM tasks WHERE task_id = ?", (task_id,)).fetchone()
        if task is None:
            return {"task_id": task_id, "completed_runs": []}
        rows = conn.execute(
            "SELECT run_id, kind, finished_at FROM agent_runs "
            "WHERE task_id = ? AND status = 'done' AND finished_at IS NOT NULL "
            "ORDER BY rowid DESC LIMIT 20",
            (task_id,),
        ).fetchall()
    return {"task_id": task_id, "goal": task["goal"], "completed_runs": [dict(row) for row in rows]}


def verify(task_id: str | None = None) -> dict:
    """只接受用户对话轮指定的、在本轮之前已完成的工作。"""
    scope = current.get()
    if scope is None or not scope.run_id:
        raise SkillValidationError(
            [{"field": "source_task_id", "message": "只能在用户对话轮总结 Skill"}]
        )
    with session(scope.path) as conn:
        current_run = conn.execute(
            "SELECT rowid, task_id, kind, status, input FROM agent_runs WHERE run_id = ?",
            (scope.run_id,),
        ).fetchone()
        if (
            current_run is None
            or current_run["kind"] != "message"
            or current_run["status"] != "running"
        ):
            raise SkillValidationError(
                [{"field": "source_task_id", "message": "只能在用户对话轮总结 Skill"}]
            )
        instruction = json.loads(current_run["input"]).get("message", "")
        if not isinstance(instruction, str) or not instruction.strip():
            raise SkillValidationError(
                [{"field": "source_task_id", "message": "需要用户在对话中指定工作"}]
            )
        selected_task_id = task_id or current_run["task_id"]
        source = conn.execute(
            "SELECT r.task_id, r.finished_at FROM agent_runs r "
            "WHERE r.task_id = ? AND r.rowid < ? AND r.status = 'done' "
            "AND r.finished_at IS NOT NULL ORDER BY r.rowid DESC LIMIT 1",
            (selected_task_id, current_run["rowid"]),
        ).fetchone()
    if source is None:
        raise SkillValidationError(
            [{"field": "source_task_id", "message": "指定的工作尚无已完成记录"}]
        )
    return {"tasks": [source["task_id"]], "occurrences": 1, "last_seen_at": source["finished_at"]}
