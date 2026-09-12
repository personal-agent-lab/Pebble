"""邮件内容及邮件能力内的去重 SQL。"""

import json
import sqlite3

from server.errors import NotFoundError


def find_reply(conn: sqlite3.Connection, source_message_id: str) -> str | None:
    row = conn.execute(
        "SELECT operation_id FROM mail_reply_drafts WHERE source_message_id = ?",
        (source_message_id,),
    ).fetchone()
    return row["operation_id"] if row else None


def find_task_link(conn: sqlite3.Connection, source_message_id: str) -> str | None:
    row = conn.execute(
        "SELECT task_id FROM mail_task_links WHERE source_message_id = ?", (source_message_id,)
    ).fetchone()
    return row["task_id"] if row else None


def insert_task_link(
    conn: sqlite3.Connection, source_message_id: str, task_id: str, now: str
) -> None:
    conn.execute("INSERT INTO mail_task_links VALUES (?, ?, ?)", (source_message_id, task_id, now))


def insert_draft(conn: sqlite3.Connection, operation_id: str, source: str, thread: str) -> None:
    conn.execute("INSERT INTO mail_reply_drafts VALUES (?, ?, ?)", (operation_id, source, thread))


def insert_version(
    conn: sqlite3.Connection,
    operation_id: str,
    version: int,
    to: list[str],
    subject: str,
    body: str,
    now: str,
) -> None:
    conn.execute(
        "INSERT INTO mail_reply_versions VALUES (?, ?, ?, ?, ?, ?)",
        (operation_id, version, json.dumps(to, ensure_ascii=False), subject, body, now),
    )


def draft(conn: sqlite3.Connection, operation_id: str, version: int | None) -> dict:
    row = conn.execute(
        "SELECT o.operation_id, v.version, o.status, d.source_message_id, d.thread_id, "
        "v.recipients, v.subject, v.body FROM operations o "
        "JOIN mail_reply_drafts d ON d.operation_id = o.operation_id "
        "JOIN mail_reply_versions v ON v.operation_id = o.operation_id "
        "AND v.version = COALESCE(?, o.version) WHERE o.operation_id = ?",
        (version, operation_id),
    ).fetchone()
    if row is None:
        raise NotFoundError(f"{operation_id}:{version}")
    result = dict(row)
    result["to"] = json.loads(result.pop("recipients"))
    return result
