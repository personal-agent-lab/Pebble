"""共享 SQL；调用者负责事务边界。"""

import sqlite3

from server.errors import NotFoundError


def task(conn: sqlite3.Connection, task_id: str) -> dict:
    row = conn.execute("SELECT * FROM tasks WHERE task_id = ?", (task_id,)).fetchone()
    if row is None:
        raise NotFoundError(task_id)
    return dict(row)


def tasks(conn: sqlite3.Connection) -> list[dict]:
    return [
        dict(row) for row in conn.execute("SELECT * FROM tasks ORDER BY created_at DESC, task_id")
    ]


def insert_task(conn: sqlite3.Connection, task_id: str, goal: str, now: str) -> None:
    conn.execute("INSERT INTO tasks VALUES (?, ?, NULL, ?)", (task_id, goal, now))


def bind_session(conn: sqlite3.Connection, task_id: str, sdk_session_id: str) -> None:
    conn.execute("UPDATE tasks SET sdk_session_id = ? WHERE task_id = ?", (sdk_session_id, task_id))


def operation(conn: sqlite3.Connection, operation_id: str) -> dict:
    row = conn.execute(
        "SELECT * FROM operations WHERE operation_id = ?", (operation_id,)
    ).fetchone()
    if row is None:
        raise NotFoundError(operation_id)
    return dict(row)


def link(conn: sqlite3.Connection, task_id: str, operation_id: str) -> None:
    conn.execute(
        "INSERT INTO task_operations VALUES (?, ?) ON CONFLICT DO NOTHING", (task_id, operation_id)
    )


def insert_operation(
    conn: sqlite3.Connection, operation_id: str, kind: str, task_id: str, now: str
) -> None:
    conn.execute(
        "INSERT INTO operations VALUES (?, ?, ?, 1, 'pending', ?, ?)",
        (operation_id, kind, task_id, now, now),
    )
    link(conn, task_id, operation_id)


def advance_version(conn: sqlite3.Connection, operation_id: str, version: int, now: str) -> None:
    conn.execute(
        "UPDATE operations SET version = ?, updated_at = ? WHERE operation_id = ?",
        (version, now, operation_id),
    )


def update_status(conn: sqlite3.Connection, operation_id: str, status: str, now: str) -> None:
    conn.execute(
        "UPDATE operations SET status = ?, updated_at = ? WHERE operation_id = ?",
        (status, now, operation_id),
    )


def task_operations(conn: sqlite3.Connection, task_id: str) -> list[dict]:
    return [
        dict(row)
        for row in conn.execute(
            "SELECT o.operation_id, o.type, o.version, o.status FROM operations o "
            "JOIN task_operations t ON t.operation_id = o.operation_id "
            "WHERE t.task_id = ? ORDER BY o.created_at, o.operation_id",
            (task_id,),
        )
    ]
