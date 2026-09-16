"""共享 SQL；调用者负责事务边界。"""

import sqlite3

from server.errors import NotFoundError
from server.sessions import runs

# 触发源随任务一起读出，界面据此区分自动开始与用户亲自发起的会话。
# 不另立一列：新邮件调用只由新邮件入口登记，有没有这种调用就是答案，旧库也无需迁移。
TASK_SELECT = (
    "SELECT t.*, CASE WHEN EXISTS("
    "SELECT 1 FROM agent_runs r WHERE r.task_id = t.task_id AND r.kind = ?"
    ") THEN 'mail' ELSE 'user' END AS source FROM tasks t"
)


def task(conn: sqlite3.Connection, task_id: str) -> dict:
    row = conn.execute(
        f"{TASK_SELECT} WHERE t.task_id = ?", (runs.KIND_NEW_MAIL, task_id)
    ).fetchone()
    if row is None:
        raise NotFoundError(task_id)
    return dict(row)


def tasks(conn: sqlite3.Connection) -> list[dict]:
    return [
        dict(row)
        for row in conn.execute(
            f"{TASK_SELECT} ORDER BY t.created_at DESC, t.task_id", (runs.KIND_NEW_MAIL,)
        )
    ]


def insert_task(conn: sqlite3.Connection, task_id: str, goal: str, now: str) -> None:
    conn.execute("INSERT INTO tasks VALUES (?, ?, NULL, ?)", (task_id, goal, now))


def bind_session(conn: sqlite3.Connection, task_id: str, sdk_session_id: str) -> None:
    conn.execute("UPDATE tasks SET sdk_session_id = ? WHERE task_id = ?", (sdk_session_id, task_id))


def update_goal(conn: sqlite3.Connection, task_id: str, goal: str) -> None:
    conn.execute("UPDATE tasks SET goal = ? WHERE task_id = ?", (goal, task_id))


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


def has_active_run(conn: sqlite3.Connection, task_id: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM agent_runs WHERE task_id = ? AND status IN ('pending','running') LIMIT 1",
        (task_id,),
    ).fetchone()
    return row is not None


def delete_task(conn: sqlite3.Connection, task_id: str) -> None:
    """删除任务及其全部从属记录；外键开启，必须按引用方向先删子表。"""
    conn.execute(
        "DELETE FROM calendar_event_versions WHERE operation_id IN "
        "(SELECT operation_id FROM operations WHERE created_task_id = ?)",
        (task_id,),
    )
    conn.execute(
        "DELETE FROM calendar_events WHERE operation_id IN "
        "(SELECT operation_id FROM operations WHERE created_task_id = ?)",
        (task_id,),
    )
    conn.execute(
        "DELETE FROM mail_draft_versions WHERE operation_id IN "
        "(SELECT operation_id FROM operations WHERE created_task_id = ?)",
        (task_id,),
    )
    conn.execute(
        "DELETE FROM mail_drafts WHERE operation_id IN "
        "(SELECT operation_id FROM operations WHERE created_task_id = ?)",
        (task_id,),
    )
    conn.execute(
        "DELETE FROM history_fts WHERE rowid IN "
        "(SELECT fts_rowid FROM history_items WHERE task_id = ?)",
        (task_id,),
    )
    conn.execute("DELETE FROM history_items WHERE task_id = ?", (task_id,))
    conn.execute("DELETE FROM task_timeline_items WHERE task_id = ?", (task_id,))
    conn.execute("DELETE FROM memory_reviews WHERE task_id = ?", (task_id,))
    conn.execute(
        "DELETE FROM approval_executions WHERE operation_id IN "
        "(SELECT operation_id FROM operations WHERE created_task_id = ?)",
        (task_id,),
    )
    conn.execute("DELETE FROM agent_runs WHERE task_id = ?", (task_id,))
    conn.execute("DELETE FROM mail_task_links WHERE task_id = ?", (task_id,))
    conn.execute("DELETE FROM task_operations WHERE task_id = ?", (task_id,))
    conn.execute("DELETE FROM operations WHERE created_task_id = ?", (task_id,))
    conn.execute("DELETE FROM tasks WHERE task_id = ?", (task_id,))


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
