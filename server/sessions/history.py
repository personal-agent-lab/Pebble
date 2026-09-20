"""历史对话检索：跨任务搜索时间线，并读取命中位置的前后文。

历史属于“需要时再查”的内容，不进入每轮常驻上下文。索引是 `pebble.db` 里的 FTS5 派生表，
检索前增量同步：已结束轮次里还没进索引的文字、提示与邮件草稿补进来，版本或执行状态变了的
草稿重写。进行中的轮次不进索引——流式输出期间反复改写全文索引代价太高，而当前任务本来
就在模型上下文里。
"""

from __future__ import annotations

import json
import re
import sqlite3
from datetime import UTC, datetime, time
from pathlib import Path

from server.db import session, write
from server.errors import HistoryValidationError, NotFoundError
from server.tools.gmail import service as mail
from server.tools.personal_kb.index import normalize_terms, snippet

MAX_RESULTS_DEFAULT = 10
MAX_RESULTS_LIMIT = 20
WINDOW_DEFAULT = 5
WINDOW_LIMIT = 20
FTS_MIN_TERM = 3
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
BODY_LIMIT = 4000

SYNC_SQL = (
    "SELECT i.item_id, i.task_id, i.kind, i.text, i.operation_id, "
    "o.version AS op_version, o.status AS op_status, h.fts_rowid "
    "FROM task_timeline_items i "
    "JOIN agent_runs r ON r.run_id = i.run_id "
    "LEFT JOIN operations o ON o.operation_id = i.operation_id "
    "LEFT JOIN history_items h ON h.item_id = i.item_id "
    "WHERE i.kind IN ('text', 'notice', 'mail_draft') "
    "AND r.status NOT IN ('pending', 'running') "
    "AND (h.item_id IS NULL OR (i.kind = 'mail_draft' AND "
    "(h.op_version IS NOT o.version OR h.op_status IS NOT o.status))) "
    "ORDER BY i.rowid"
)


class HistoryStore:
    def __init__(self, path: Path | None = None):
        self.path = path

    def search(
        self,
        query: str,
        *,
        exclude_task_id: str | None = None,
        after: str | None = None,
        before: str | None = None,
        max_results: int | None = None,
    ) -> dict:
        """按关键词检索历史条目，新到旧；全部词都命中才算命中。日期按本机时区的自然日。"""
        terms = normalize_terms(query)
        errors = []
        if not terms:
            errors.append({"field": "query", "message": "检索词不能为空"})
        limit = MAX_RESULTS_DEFAULT if max_results is None else max_results
        if not isinstance(limit, int) or not 1 <= limit <= MAX_RESULTS_LIMIT:
            errors.append(
                {
                    "field": "max_results",
                    "message": f"max_results 必须在 1–{MAX_RESULTS_LIMIT} 之间",
                }
            )
        bounds = {}
        for field, value, edge in (("after", after, time.min), ("before", before, time.max)):
            if value is None or value == "":
                continue
            if not DATE_RE.match(value):
                errors.append({"field": field, "message": f"{field} 必须是 YYYY-MM-DD 日期"})
                continue
            try:
                local = datetime.combine(datetime.strptime(value, "%Y-%m-%d").date(), edge)
            except ValueError:
                errors.append({"field": field, "message": f"{field} 不是有效日期"})
                continue
            # 时间线的 created_at 是 UTC ISO 文本：本机时区的自然日边界换算成 UTC 再按字符串比较。
            bounds[field] = local.astimezone().astimezone(UTC).isoformat()
        if errors:
            raise HistoryValidationError(errors)

        condition, params = _condition(terms)
        clauses = [condition]
        if exclude_task_id:
            clauses.append("h.task_id != ?")
            params.append(exclude_task_id)
        if "after" in bounds:
            clauses.append("i.created_at >= ?")
            params.append(bounds["after"])
        if "before" in bounds:
            clauses.append("i.created_at <= ?")
            params.append(bounds["before"])
        params.append(limit)
        with session(self.path) as conn:
            with write(conn):
                sync(conn)
            rows = conn.execute(
                "SELECT h.item_id, h.task_id, t.goal, i.kind, i.role, i.created_at, "
                "history_fts.body AS body "
                "FROM history_fts JOIN history_items h ON h.fts_rowid = history_fts.rowid "
                "JOIN task_timeline_items i ON i.item_id = h.item_id "
                "JOIN tasks t ON t.task_id = h.task_id "
                f"WHERE {' AND '.join(clauses)} "
                "ORDER BY i.created_at DESC, i.rowid DESC LIMIT ?",
                params,
            ).fetchall()
        return {
            "query": query,
            "results": [
                {
                    "task_id": row["task_id"],
                    "task_title": row["goal"],
                    "item_id": row["item_id"],
                    "speaker": _speaker(row["kind"], row["role"]),
                    "created_at": row["created_at"],
                    "snippet": snippet(row["body"], terms),
                }
                for row in rows
            ],
        }

    def read(
        self,
        task_id: str,
        item_id: str,
        *,
        before: int | None = None,
        after: int | None = None,
    ) -> dict:
        """读取某个任务里一条记录前后的对话原文；邮件草稿附最新内容与执行状态。"""
        errors = []
        counts = {}
        for field, value in (("before", before), ("after", after)):
            count = WINDOW_DEFAULT if value is None else value
            if not isinstance(count, int) or not 0 <= count <= WINDOW_LIMIT:
                errors.append({"field": field, "message": f"{field} 必须在 0–{WINDOW_LIMIT} 之间"})
            counts[field] = count
        if errors:
            raise HistoryValidationError(errors)
        with session(self.path) as conn:
            task = conn.execute(
                "SELECT task_id, goal FROM tasks WHERE task_id = ?", (task_id,)
            ).fetchone()
            if task is None:
                raise NotFoundError(task_id)
            rows = conn.execute(
                "SELECT i.item_id, i.kind, i.role, i.text, i.operation_id, i.created_at "
                "FROM task_timeline_items i WHERE i.task_id = ? ORDER BY i.rowid",
                (task_id,),
            ).fetchall()
            position = next(
                (index for index, row in enumerate(rows) if row["item_id"] == item_id), None
            )
            if position is None:
                raise NotFoundError(f"{task_id}:{item_id}")
            start = max(0, position - counts["before"])
            window = rows[start : position + counts["after"] + 1]
            items = [_item(conn, row, target=row["item_id"] == item_id) for row in window]
        return {
            "task_id": task["task_id"],
            "task_title": task["goal"],
            "has_earlier": start > 0,
            "has_later": position + counts["after"] + 1 < len(rows),
            "items": items,
        }


def sync(conn: sqlite3.Connection) -> int:
    """把已结束轮次里新增或变化的条目写进索引；调用方持有写事务。返回写入条数。"""
    rows = conn.execute(SYNC_SQL).fetchall()
    for row in rows:
        if row["fts_rowid"] is not None:
            conn.execute("DELETE FROM history_fts WHERE rowid = ?", (row["fts_rowid"],))
            conn.execute("DELETE FROM history_items WHERE item_id = ?", (row["item_id"],))
        body = _body(conn, row)
        cursor = conn.execute("INSERT INTO history_fts (body) VALUES (?)", (body,))
        conn.execute(
            "INSERT INTO history_items (item_id, task_id, fts_rowid, op_version, op_status) "
            "VALUES (?, ?, ?, ?, ?)",
            (row["item_id"], row["task_id"], cursor.lastrowid, row["op_version"], row["op_status"]),
        )
    return len(rows)


def _body(conn: sqlite3.Connection, row: sqlite3.Row) -> str:
    if row["kind"] != "mail_draft":
        return row["text"] or ""
    try:
        draft = mail.draft(conn, row["operation_id"], None)
    except NotFoundError:
        return ""
    return "\n".join(
        (
            f"邮件草稿（{draft['status']}）",
            f"收件人：{', '.join(draft['to'])}",
            f"主题：{draft['subject']}",
            draft["body"],
        )
    )


def _item(conn: sqlite3.Connection, row: sqlite3.Row, *, target: bool) -> dict:
    item = {
        "item_id": row["item_id"],
        "speaker": _speaker(row["kind"], row["role"]),
        "created_at": row["created_at"],
    }
    if target:
        item["matched"] = True
    if row["kind"] != "mail_draft":
        item["text"] = _clip(row["text"] or "")
        return item
    try:
        draft = mail.draft(conn, row["operation_id"], None)
    except NotFoundError:
        item["text"] = "（邮件草稿已不存在）"
        return item
    execution = conn.execute(
        "SELECT result_json FROM approval_executions WHERE operation_id = ?",
        (row["operation_id"],),
    ).fetchone()
    item["mail_draft"] = {
        "to": draft["to"],
        "subject": draft["subject"],
        "body": _clip(draft["body"]),
        "status": draft["status"],
        "result": json.loads(execution["result_json"])
        if execution is not None and execution["result_json"]
        else None,
    }
    return item


def _speaker(kind: str, role: str | None) -> str:
    if kind == "text":
        return role or "assistant"
    return kind


def _clip(text: str) -> str:
    return text if len(text) <= BODY_LIMIT else f"{text[:BODY_LIMIT]}…"


def _condition(terms: list[str]) -> tuple[str, list]:
    """全部词够长走 FTS5；含 1–2 个字的词时在同一张表上做包含匹配。"""
    if all(len(term) >= FTS_MIN_TERM for term in terms):
        expression = " AND ".join(f'"{term.replace(chr(34), chr(34) * 2)}"' for term in terms)
        return "history_fts MATCH ?", [expression]
    clauses = []
    params: list = []
    for term in terms:
        escaped = term.lower().replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        clauses.append("lower(history_fts.body) LIKE ? ESCAPE '\\'")
        params.append(f"%{escaped}%")
    return " AND ".join(clauses), params
