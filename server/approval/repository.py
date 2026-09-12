"""确认执行记录 SQL；调用者负责事务边界。"""

import sqlite3

from server.sessions.errors import NotFoundError

VIEW_SQL = (
    "SELECT o.operation_id, o.version, o.status, e.task_id AS execution_task_id, "
    "e.version AS confirmed_version, e.confirmed_at, e.message_id, e.reason, e.completed_at, "
    "t.sdk_session_id FROM operations o "
    "LEFT JOIN approval_executions e ON e.operation_id = o.operation_id "
    "LEFT JOIN tasks t ON t.task_id = e.task_id WHERE o.operation_id = ?"
)


def view(conn: sqlite3.Connection, operation_id: str) -> dict:
    """单条查询读取操作、确认记录与回传会话，避免分别读取看到不同提交点的组合。"""
    row = conn.execute(VIEW_SQL, (operation_id,)).fetchone()
    if row is None:
        raise NotFoundError(operation_id)
    return dict(row)


def insert(
    conn: sqlite3.Connection, operation_id: str, task_id: str, version: int, confirmed_at: str
) -> None:
    conn.execute(
        "INSERT INTO approval_executions (operation_id, task_id, version, confirmed_at) "
        "VALUES (?, ?, ?, ?)",
        (operation_id, task_id, version, confirmed_at),
    )


def complete(
    conn: sqlite3.Connection,
    operation_id: str,
    *,
    message_id: str | None,
    reason: str | None,
    completed_at: str,
) -> None:
    conn.execute(
        "UPDATE approval_executions SET message_id = ?, reason = ?, completed_at = ? "
        "WHERE operation_id = ?",
        (message_id, reason, completed_at, operation_id),
    )


def interrupted(conn: sqlite3.Connection) -> list[str]:
    return [
        row["operation_id"]
        for row in conn.execute(
            "SELECT operation_id FROM approval_executions WHERE completed_at IS NULL "
            "ORDER BY confirmed_at, operation_id"
        )
    ]
