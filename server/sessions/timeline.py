"""任务可见时间线：持久化顺序并物化草稿与执行现状。"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from uuid import uuid4

from server.approval import repository as approvals
from server.approval.service import execution_response
from server.db import session
from server.sessions import repository as tasks
from server.sessions.service import timestamp
from server.tools.gmail import service as mail


def next_sequence(conn: sqlite3.Connection, task_id: str) -> int:
    """任务内下一个轨迹序号；调用方必须已持有写事务（write），保证单调不重。"""
    row = conn.execute(
        "SELECT COALESCE(MAX(sequence), -1) AS s FROM task_timeline_items WHERE task_id = ?",
        (task_id,),
    ).fetchone()
    return row["s"] + 1


def insert_text(
    conn: sqlite3.Connection,
    task_id: str,
    run_id: str,
    role: str,
    text: str,
) -> str:
    item_id = str(uuid4())
    conn.execute(
        "INSERT INTO task_timeline_items "
        "(item_id, task_id, run_id, sequence, kind, role, text, operation_id, created_at) "
        "VALUES (?, ?, ?, ?, 'text', ?, ?, NULL, ?)",
        (item_id, task_id, run_id, next_sequence(conn, task_id), role, text, timestamp()),
    )
    return item_id


def append_assistant_text(conn: sqlite3.Connection, task_id: str, run_id: str, delta: str) -> str:
    last = conn.execute(
        "SELECT item_id, kind, role FROM task_timeline_items "
        "WHERE run_id = ? ORDER BY sequence DESC LIMIT 1",
        (run_id,),
    ).fetchone()
    if last is not None and last["kind"] == "text" and last["role"] == "assistant":
        conn.execute(
            "UPDATE task_timeline_items SET text = text || ? WHERE item_id = ?",
            (delta, last["item_id"]),
        )
        return last["item_id"]
    return insert_text(conn, task_id, run_id, "assistant", delta)


ROLE_LABELS = {"user": "用户", "assistant": "助手"}


def run_text(conn: sqlite3.Connection, run_id: str) -> str:
    """一轮的全部对话文本，按时间顺序带角色标注；无文本时为空串。"""
    rows = conn.execute(
        "SELECT role, text FROM task_timeline_items "
        "WHERE run_id = ? AND kind = 'text' ORDER BY sequence",
        (run_id,),
    ).fetchall()
    return "\n".join(f"{ROLE_LABELS.get(row['role'], row['role'])}：{row['text']}" for row in rows)


def ensure_mail_draft(
    conn: sqlite3.Connection, task_id: str, run_id: str, operation_id: str
) -> str:
    existing = conn.execute(
        "SELECT item_id FROM task_timeline_items "
        "WHERE task_id = ? AND operation_id = ? AND kind = 'mail_draft'",
        (task_id, operation_id),
    ).fetchone()
    if existing is not None:
        return existing["item_id"]
    item_id = str(uuid4())
    conn.execute(
        "INSERT INTO task_timeline_items "
        "(item_id, task_id, run_id, sequence, kind, role, text, operation_id, created_at) "
        "VALUES (?, ?, ?, ?, 'mail_draft', NULL, NULL, ?, ?)",
        (item_id, task_id, run_id, next_sequence(conn, task_id), operation_id, timestamp()),
    )
    return item_id


def insert_error(conn: sqlite3.Connection, task_id: str, run_id: str, text: str) -> str:
    item_id = str(uuid4())
    conn.execute(
        "INSERT INTO task_timeline_items "
        "(item_id, task_id, run_id, sequence, kind, role, text, operation_id, created_at) "
        "VALUES (?, ?, ?, ?, 'error', NULL, ?, NULL, ?)",
        (item_id, task_id, run_id, next_sequence(conn, task_id), text, timestamp()),
    )
    return item_id


def insert_notice(conn: sqlite3.Connection, task_id: str, run_id: str, text: str) -> str:
    """程序生成的提示（如记忆变更结果）：不是模型输出，role 为空。"""
    item_id = str(uuid4())
    conn.execute(
        "INSERT INTO task_timeline_items "
        "(item_id, task_id, run_id, sequence, kind, role, text, operation_id, created_at) "
        "VALUES (?, ?, ?, ?, 'notice', NULL, ?, NULL, ?)",
        (item_id, task_id, run_id, next_sequence(conn, task_id), text, timestamp()),
    )
    return item_id


def insert_tool_item(
    conn: sqlite3.Connection,
    *,
    task_id: str,
    run_id: str,
    tool_call_id: str,
    name: str,
    arguments: dict,
    status: str,
    result: str,
    created_at: str,
) -> str:
    """一次工具调用的轨迹条目：参数含值、成败与返回内容，与其他条目共用任务内序号。

    供 mcp 边界在每次调用结束时写入；被拒调用（unknown_tool、wrong_target）与业务
    失败同样是模型的真实尝试，照记为 error。
    """
    item_id = str(uuid4())
    conn.execute(
        "INSERT INTO task_timeline_items "
        "(item_id, task_id, run_id, sequence, kind, role, text, operation_id, "
        "tool_call_id, tool_name, tool_arguments, tool_status, tool_result, created_at) "
        "VALUES (?, ?, ?, ?, 'tool', NULL, NULL, NULL, ?, ?, ?, ?, ?, ?)",
        (
            item_id,
            task_id,
            run_id,
            next_sequence(conn, task_id),
            tool_call_id,
            name,
            json.dumps(arguments, ensure_ascii=False, sort_keys=True),
            status,
            result,
            created_at,
        ),
    )
    return item_id


def start_tool_item(
    conn: sqlite3.Connection,
    *,
    task_id: str,
    run_id: str,
    tool_call_id: str,
    name: str,
    arguments: dict,
    created_at: str,
) -> str:
    """调用开始即确定序号；同一调用重复通知不生成第二条。"""
    existing = conn.execute(
        "SELECT item_id FROM task_timeline_items WHERE run_id = ? AND tool_call_id = ?",
        (run_id, tool_call_id),
    ).fetchone()
    if existing is not None:
        return existing["item_id"]
    item_id = str(uuid4())
    conn.execute(
        "INSERT INTO task_timeline_items "
        "(item_id, task_id, run_id, sequence, kind, role, text, operation_id, "
        "tool_call_id, tool_name, tool_arguments, tool_status, tool_result, created_at) "
        "VALUES (?, ?, ?, ?, 'tool', NULL, NULL, NULL, ?, ?, ?, 'running', NULL, ?)",
        (
            item_id,
            task_id,
            run_id,
            next_sequence(conn, task_id),
            tool_call_id,
            name,
            json.dumps(arguments, ensure_ascii=False, sort_keys=True),
            created_at,
        ),
    )
    return item_id


def finish_tool_item(
    conn: sqlite3.Connection,
    *,
    run_id: str,
    tool_call_id: str,
    status: str,
    result: str,
) -> bool:
    """只完成仍在运行的条目；重复的成功、失败或拒绝通知不覆盖首次结果。"""
    changed = conn.execute(
        "UPDATE task_timeline_items SET tool_status = ?, tool_result = ? "
        "WHERE run_id = ? AND tool_call_id = ? AND kind = 'tool' AND tool_status = 'running'",
        (status, result, run_id, tool_call_id),
    )
    return changed.rowcount == 1


class TimelineStore:
    def __init__(self, path: Path | None = None):
        self.path = path

    def list_items(self, task_id: str) -> dict:
        with session(self.path) as conn:
            record = tasks.task(conn, task_id)
            rows = list(
                conn.execute(
                    "SELECT * FROM task_timeline_items WHERE task_id = ? ORDER BY sequence",
                    (task_id,),
                )
            )
            items = []
            for row in rows:
                item = dict(row)
                base = {
                    "item_id": item["item_id"],
                    "kind": item["kind"],
                    "run_id": item["run_id"],
                    "created_at": item["created_at"],
                }
                if item["kind"] == "text":
                    attachment_rows = conn.execute(
                        "SELECT f.file_id,f.filename,f.mime_type,f.size,f.sha256 "
                        "FROM timeline_item_attachments a JOIN uploaded_files f "
                        "ON f.file_id=a.file_id WHERE a.item_id=? ORDER BY a.position",
                        (item["item_id"],),
                    ).fetchall()
                    payload = {
                        **base,
                        "role": item["role"],
                        "text": item["text"],
                        "attachments": [
                            {
                                **dict(attachment),
                                "url": f"/api/tasks/{task_id}/attachments/{attachment['file_id']}",
                            }
                            for attachment in attachment_rows
                        ],
                    }
                    items.append(payload)
                elif item["kind"] in ("error", "notice"):
                    items.append({**base, "text": item["text"]})
                elif item["kind"] == "tool":
                    items.append(
                        {
                            **base,
                            "tool_call_id": item["tool_call_id"],
                            "name": item["tool_name"],
                            "arguments": json.loads(item["tool_arguments"]),
                            "status": item["tool_status"],
                            "result": item["tool_result"],
                        }
                    )
                elif item["kind"] == "mail_draft":
                    operation_id = item["operation_id"]
                    items.append(
                        {
                            **base,
                            "operation_id": operation_id,
                            "draft": mail.draft(conn, operation_id, None),
                            "execution": execution_response(approvals.view(conn, operation_id)),
                        }
                    )
            return {
                "task_id": task_id,
                "sdk_session_id": record["sdk_session_id"],
                "items": items,
            }
