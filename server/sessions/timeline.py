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
from server.uploads import files_for_ids, public_file


def _ids(value: str) -> list[str]:
    return list(json.loads(value))


def insert_text(
    conn: sqlite3.Connection,
    task_id: str,
    run_id: str,
    role: str,
    text: str,
    attachment_ids: list[str] | None = None,
) -> str:
    item_id = str(uuid4())
    conn.execute(
        "INSERT INTO task_timeline_items "
        "(item_id, task_id, run_id, kind, role, text, operation_id, attachment_ids, created_at) "
        "VALUES (?, ?, ?, 'text', ?, ?, NULL, ?, ?)",
        (item_id, task_id, run_id, role, text, json.dumps(attachment_ids or []), timestamp()),
    )
    return item_id


def append_assistant_text(conn: sqlite3.Connection, task_id: str, run_id: str, delta: str) -> str:
    last = conn.execute(
        "SELECT item_id, kind, role FROM task_timeline_items "
        "WHERE run_id = ? ORDER BY rowid DESC LIMIT 1",
        (run_id,),
    ).fetchone()
    if last is not None and last["kind"] == "text" and last["role"] == "assistant":
        conn.execute(
            "UPDATE task_timeline_items SET text = text || ? WHERE item_id = ?",
            (delta, last["item_id"]),
        )
        return last["item_id"]
    return insert_text(conn, task_id, run_id, "assistant", delta)


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
        "(item_id, task_id, run_id, kind, role, text, operation_id, attachment_ids, created_at) "
        "VALUES (?, ?, ?, 'mail_draft', NULL, NULL, ?, '[]', ?)",
        (item_id, task_id, run_id, operation_id, timestamp()),
    )
    return item_id


def insert_error(conn: sqlite3.Connection, task_id: str, run_id: str, text: str) -> str:
    item_id = str(uuid4())
    conn.execute(
        "INSERT INTO task_timeline_items "
        "(item_id, task_id, run_id, kind, role, text, operation_id, attachment_ids, created_at) "
        "VALUES (?, ?, ?, 'error', NULL, ?, NULL, '[]', ?)",
        (item_id, task_id, run_id, text, timestamp()),
    )
    return item_id


class TimelineStore:
    def __init__(self, path: Path | None = None):
        self.path = path

    def list_items(self, task_id: str) -> dict:
        with session(self.path) as conn:
            record = tasks.task(conn, task_id)
            items = []
            for row in conn.execute(
                "SELECT * FROM task_timeline_items WHERE task_id = ? ORDER BY rowid", (task_id,)
            ):
                item = dict(row)
                base = {
                    "item_id": item["item_id"],
                    "kind": item["kind"],
                    "run_id": item["run_id"],
                    "created_at": item["created_at"],
                }
                if item["kind"] == "text":
                    ids = _ids(item["attachment_ids"])
                    items.append(
                        {
                            **base,
                            "role": item["role"],
                            "text": item["text"],
                            "attachments": [
                                public_file(value) for value in files_for_ids(conn, ids)
                            ],
                        }
                    )
                elif item["kind"] == "error":
                    items.append({**base, "text": item["text"]})
                else:
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
