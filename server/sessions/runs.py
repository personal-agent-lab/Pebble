"""后台调用记录 SQL；调用者负责事务边界。"""

import json
import sqlite3

from server.errors import NotFoundError, RetryUnavailableError

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


def delivery_unfinished(conn: sqlite3.Connection, reference_id: str) -> bool:
    """该操作已有尚未结束的结果回传；核实升级时据此避免登记重复的回传。"""
    row = conn.execute(
        "SELECT 1 FROM agent_runs WHERE reference_id = ? AND kind = ? "
        "AND status IN ('pending', 'running') LIMIT 1",
        (reference_id, KIND_EXECUTION_RESULT),
    ).fetchone()
    return row is not None


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


def retryable(conn: sqlite3.Connection, row: dict | sqlite3.Row) -> bool:
    """只有未产生操作记录的最后一轮已中断用户消息可安全整体重试。"""
    if row["kind"] != KIND_MESSAGE or row["status"] != "interrupted":
        return False
    operation = conn.execute(
        "SELECT 1 FROM operations WHERE created_task_id = ? AND "
        "((created_at >= ? AND created_at <= ?) OR (updated_at >= ? AND updated_at <= ?)) "
        "LIMIT 1",
        (
            row["task_id"],
            row["started_at"],
            row["finished_at"],
            row["started_at"],
            row["finished_at"],
        ),
    ).fetchone()
    return operation is None


def retry_latest_message(conn: sqlite3.Connection, task_id: str) -> dict:
    """把最后一轮已中断的用户消息恢复为待执行，不新增第二条用户消息。"""
    row = conn.execute(
        "SELECT * FROM agent_runs WHERE task_id = ? ORDER BY rowid DESC LIMIT 1",
        (task_id,),
    ).fetchone()
    if row is None or not retryable(conn, row):
        raise RetryUnavailableError()

    # 中断前可能已经流出半截回答；重试替换这段未完成文字。程序提示与操作卡代表已经
    # 发生的事实，必须保留。历史索引是派生数据，但要同时移除对应条目，避免留下孤儿。
    assistant_ids = [
        item["item_id"]
        for item in conn.execute(
            "SELECT item_id FROM task_timeline_items "
            "WHERE run_id = ? AND kind = 'text' AND role = 'assistant'",
            (row["run_id"],),
        )
    ]
    for item_id in assistant_ids:
        indexed = conn.execute(
            "SELECT fts_rowid FROM history_items WHERE item_id = ?", (item_id,)
        ).fetchone()
        if indexed is not None:
            conn.execute("DELETE FROM history_fts WHERE rowid = ?", (indexed["fts_rowid"],))
            conn.execute("DELETE FROM history_items WHERE item_id = ?", (item_id,))
        conn.execute("DELETE FROM task_timeline_items WHERE item_id = ?", (item_id,))

    cursor = conn.execute(
        "UPDATE agent_runs SET status = 'pending', error = NULL, started_at = NULL, "
        "finished_at = NULL WHERE run_id = ? AND status = 'interrupted'",
        (row["run_id"],),
    )
    if cursor.rowcount != 1:
        raise RetryUnavailableError()
    return run(conn, row["run_id"])
