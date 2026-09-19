"""最小执行证据：仅保存工具名称、参数键和成功状态，不保存参数值。"""

import json
from uuid import uuid4

from server.db import session, write
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


def find(task_ids: list[str] | None = None) -> dict:
    scope = current.get()
    with session(scope.path if scope else None) as conn:
        rows = conn.execute(
            "SELECT e.run_id, r.task_id, e.tool_name, e.succeeded, e.created_at "
            "FROM skill_tool_evidence e JOIN agent_runs r USING(run_id) "
            "WHERE r.status = 'done' ORDER BY e.created_at LIMIT 2000"
        ).fetchall()
    runs = {}
    for row in rows:
        if task_ids is not None and row["task_id"] not in task_ids:
            continue
        entry = runs.setdefault(
            row["run_id"],
            dict(task_id=row["task_id"], tools=[], success=True, last_seen_at=row["created_at"]),
        )
        entry["tools"].append(row["tool_name"])
        entry["success"] &= bool(row["succeeded"])
        entry["last_seen_at"] = row["created_at"]
    sequences = {}
    for run in runs.values():
        if run["success"] and run["tools"]:
            sequences.setdefault(tuple(run["tools"]), {})[run["task_id"]] = run["last_seen_at"]
    groups = [
        dict(
            tools=list(seq),
            tasks=sorted(tasks),
            occurrences=len(tasks),
            last_seen_at=max(tasks.values()),
        )
        for seq, tasks in sequences.items()
    ]
    groups.sort(key=lambda group: group["occurrences"], reverse=True)
    return {"groups": groups}


def verify(evidence: dict | None) -> dict:
    from server.errors import SkillValidationError

    ids = list(dict.fromkeys((evidence or {}).get("tasks", [])))
    for group in find(ids)["groups"]:
        if group["occurrences"] >= 3:
            return {key: group[key] for key in ("tasks", "occurrences", "last_seen_at")}
    raise SkillValidationError(
        [{"field": "evidence", "message": "需要至少三个不同任务中成功完成的相同工具序列"}]
    )
