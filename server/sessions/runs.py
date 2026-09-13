"""后台调用记录 SQL；调用者负责事务边界。"""

import json
import sqlite3

from server.errors import NotFoundError

KIND_NEW_MAIL = "new_mail"
KIND_MESSAGE = "message"
KIND_EXECUTION_RESULT = "execution_result"

RUN_FIELDS = (
    "run_id",
    "task_id",
    "kind",
    "status",
    "error",
    "created_at",
    "started_at",
    "finished_at",
)

# 执行结果回传按操作标识唯一：重复确认或重复恢复不会新增回传记录。
INSERT_SQL = (
    "INSERT INTO agent_runs "
    "(run_id, task_id, kind, reference_id, input, status, created_at) "
    "VALUES (?, ?, ?, ?, ?, 'pending', ?) "
    "ON CONFLICT(reference_id) WHERE kind = 'execution_result' DO NOTHING"
)


def insert(
    conn: sqlite3.Connection,
    run_id: str,
    task_id: str,
    kind: str,
    payload: dict,
    reference_id: str | None,
    now: str,
) -> None:
    conn.execute(
        INSERT_SQL,
        (run_id, task_id, kind, reference_id, json.dumps(payload, ensure_ascii=False), now),
    )


def run_response(row: dict) -> dict:
    return {field: row[field] for field in RUN_FIELDS}


def run(conn: sqlite3.Connection, run_id: str) -> dict:
    row = conn.execute("SELECT * FROM agent_runs WHERE run_id = ?", (run_id,)).fetchone()
    if row is None:
        raise NotFoundError(run_id)
    return dict(row)


def runs(conn: sqlite3.Connection, task_id: str) -> list[dict]:
    return [
        dict(row)
        for row in conn.execute(
            "SELECT * FROM agent_runs WHERE task_id = ? ORDER BY rowid", (task_id,)
        )
    ]


def latest(conn: sqlite3.Connection, task_id: str) -> dict | None:
    row = conn.execute(
        "SELECT * FROM agent_runs WHERE task_id = ? ORDER BY rowid DESC LIMIT 1",
        (task_id,),
    ).fetchone()
    return dict(row) if row is not None else None


def pending(conn: sqlite3.Connection) -> list[dict]:
    return [
        dict(row)
        for row in conn.execute("SELECT * FROM agent_runs WHERE status = 'pending' ORDER BY rowid")
    ]


def claim(conn: sqlite3.Connection, run_id: str, now: str) -> bool:
    """标记调用开始；只有待处理记录能取得运行权。"""
    cursor = conn.execute(
        "UPDATE agent_runs SET status = 'running', started_at = ? "
        "WHERE run_id = ? AND status = 'pending'",
        (now, run_id),
    )
    return cursor.rowcount == 1


def finish(conn: sqlite3.Connection, run_id: str, status: str, error: str | None, now: str) -> None:
    conn.execute(
        "UPDATE agent_runs SET status = ?, error = ?, finished_at = ? "
        "WHERE run_id = ? AND status = 'running'",
        (status, error, now, run_id),
    )


def interrupt_running(conn: sqlite3.Connection, now: str, reason: str) -> list[str]:
    """把上次进程遗留的运行中调用记为中断，返回被处理的调用。"""
    run_ids = [
        row["run_id"]
        for row in conn.execute(
            "SELECT run_id FROM agent_runs WHERE status = 'running' ORDER BY rowid"
        )
    ]
    for run_id in run_ids:
        conn.execute(
            "UPDATE agent_runs SET status = 'interrupted', error = ?, finished_at = ? "
            "WHERE run_id = ?",
            (reason, now, run_id),
        )
    return run_ids
