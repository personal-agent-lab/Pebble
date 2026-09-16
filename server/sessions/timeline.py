"""任务可见时间线：持久化顺序并物化草稿与执行现状。"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from uuid import uuid4

from server.approval import repository as approvals
from server.approval.service import execution_response
from server.db import session
from server.sessions import repository as tasks
from server.sessions.service import timestamp
from server.tools.gmail import service as mail


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
        "(item_id, task_id, run_id, kind, role, text, operation_id, created_at) "
        "VALUES (?, ?, ?, 'text', ?, ?, NULL, ?)",
        (item_id, task_id, run_id, role, text, timestamp()),
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


ROLE_LABELS = {"user": "用户", "assistant": "助手"}


def run_text(conn: sqlite3.Connection, run_id: str) -> str:
    """一轮的全部对话文本，按时间顺序带角色标注；无文本时为空串。"""
    rows = conn.execute(
        "SELECT role, text FROM task_timeline_items "
        "WHERE run_id = ? AND kind = 'text' ORDER BY rowid",
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
        "(item_id, task_id, run_id, kind, role, text, operation_id, created_at) "
        "VALUES (?, ?, ?, 'mail_draft', NULL, NULL, ?, ?)",
        (item_id, task_id, run_id, operation_id, timestamp()),
    )
    return item_id


def insert_error(conn: sqlite3.Connection, task_id: str, run_id: str, text: str) -> str:
    item_id = str(uuid4())
    conn.execute(
        "INSERT INTO task_timeline_items "
        "(item_id, task_id, run_id, kind, role, text, operation_id, created_at) "
        "VALUES (?, ?, ?, 'error', NULL, ?, NULL, ?)",
        (item_id, task_id, run_id, text, timestamp()),
    )
    return item_id


def insert_notice(conn: sqlite3.Connection, task_id: str, run_id: str, text: str) -> str:
    """程序生成的提示（如记忆变更结果）：不是模型输出，role 为空。"""
    item_id = str(uuid4())
    conn.execute(
        "INSERT INTO task_timeline_items "
        "(item_id, task_id, run_id, kind, role, text, operation_id, created_at) "
        "VALUES (?, ?, ?, 'notice', NULL, ?, NULL, ?)",
        (item_id, task_id, run_id, text, timestamp()),
    )
    return item_id


def insert_source(
    conn: sqlite3.Connection,
    task_id: str,
    run_id: str,
    source: dict,
) -> str:
    """记录一次资料读取的来源；同一轮同一版本与行号只记一次。"""
    ref = source["ref"]
    start_line, end_line = ref["lines"]
    existing = conn.execute(
        "SELECT source_id FROM task_run_sources "
        "WHERE run_id = ? AND commit_sha = ? AND path = ? AND start_line = ? AND end_line = ?",
        (run_id, ref["commit"], ref["path"], start_line, end_line),
    ).fetchone()
    if existing is not None:
        return existing["source_id"]
    sequence = conn.execute(
        "SELECT COALESCE(MAX(sequence), 0) + 1 AS next FROM task_run_sources WHERE run_id = ?",
        (run_id,),
    ).fetchone()["next"]
    source_id = str(uuid4())
    conn.execute(
        "INSERT INTO task_run_sources "
        "(source_id, task_id, run_id, sequence, doc_id, path, title, heading, "
        "start_line, end_line, commit_sha, excerpt, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            source_id,
            task_id,
            run_id,
            sequence,
            ref.get("id") or "",
            ref["path"],
            source.get("title"),
            ref.get("heading"),
            start_line,
            end_line,
            ref["commit"],
            source.get("excerpt") or "",
            timestamp(),
        ),
    )
    return source_id


def run_sources(conn: sqlite3.Connection, task_id: str) -> dict[str, list[dict]]:
    """按轮次分组的来源记录，按读取顺序排列。"""
    grouped: dict[str, list[dict]] = {}
    for row in conn.execute(
        "SELECT * FROM task_run_sources WHERE task_id = ? ORDER BY rowid", (task_id,)
    ):
        grouped.setdefault(row["run_id"], []).append(
            {
                "source_id": row["source_id"],
                "title": row["title"],
                "excerpt": row["excerpt"],
                "ref": {
                    "id": row["doc_id"],
                    "path": row["path"],
                    "heading": row["heading"],
                    "lines": [row["start_line"], row["end_line"]],
                    "commit": row["commit_sha"],
                },
            }
        )
    return grouped


class TimelineStore:
    def __init__(self, path: Path | None = None):
        self.path = path

    def list_items(self, task_id: str) -> dict:
        with session(self.path) as conn:
            record = tasks.task(conn, task_id)
            rows = list(
                conn.execute(
                    "SELECT * FROM task_timeline_items WHERE task_id = ? ORDER BY rowid",
                    (task_id,),
                )
            )
            sources = run_sources(conn, task_id)
            # 来源挂在该轮最后一段回答上：一轮里模型可能先答、再读资料、再补答。
            anchors = _answer_anchors(rows)
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
                    payload = {
                        **base,
                        "role": item["role"],
                        "text": item["text"],
                    }
                    citations = sources.get(item["run_id"], [])
                    if citations and anchors.get(item["run_id"]) == item["item_id"]:
                        payload["sources"] = citations
                    items.append(payload)
                elif item["kind"] in ("error", "notice"):
                    items.append({**base, "text": item["text"]})
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


def _answer_anchors(rows: list[sqlite3.Row]) -> dict[str, str]:
    """每轮最后一段 Agent 回答的条目标识；来源只挂在这里。"""
    anchors: dict[str, str] = {}
    for row in rows:
        if row["kind"] == "text" and row["role"] == "assistant":
            anchors[row["run_id"]] = row["item_id"]
    return anchors
