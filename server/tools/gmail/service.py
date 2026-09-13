"""回复草稿：业务校验规则、内容存储与版本去重。

校验是纯函数（契约 §5）：模型首次生成与用户编辑共用同一套规则，不保存、不发送、不改写内容。
存储按契约 §4 管理版本并按原邮件去重；去重先于校验，复用已有操作时候选内容不参与校验。
本模块不调用模型、不发送邮件。
"""

from __future__ import annotations

import json
import re
import sqlite3
from email.utils import parseaddr
from pathlib import Path
from typing import TypedDict

from server.db import session, write
from server.errors import DraftValidationError, NotFoundError
from server.sessions import repository as operations
from server.sessions.service import check_editable, create_operation, next_version, timestamp

# 匹配标准 Email 格式（支持形如 user.name+tag@domain.co.uk）
EMAIL_REGEX = re.compile(r"^[a-zA-Z0-9_.+-]+@[a-zA-Z0-9-]+(?:\.[a-zA-Z0-9-]+)+$")


class ValidationError(TypedDict):
    field: str
    message: str


class ValidationResult(TypedDict):
    valid: bool
    errors: list[ValidationError]


def is_valid_email_address(addr: str) -> bool:
    """检查邮箱地址格式是否合规（支持纯地址或带姓名的 RFC 格式）。"""
    if not addr or not isinstance(addr, str):
        return False

    if "\r" in addr or "\n" in addr:
        return False
    clean_str = addr.strip()
    if not clean_str:
        return False

    # 若包含 Name <email> 结构，提取实际 email 地址
    _, parsed_email = parseaddr(clean_str)
    target = parsed_email.strip() if parsed_email else clean_str
    return bool(EMAIL_REGEX.match(target))


def validate_reply_draft(
    source_message_id: str,
    thread_id: str,
    to: list[str],
    subject: str,
    body: str,
) -> ValidationResult:
    """回复草稿业务规则校验纯函数。

    Args:
        source_message_id: 原邮件标识（供定位与去重）
        thread_id: Gmail 邮件线程 ID
        to: 收件人地址列表
        subject: 回复主题
        body: 回复正文

    Returns:
        ValidationResult 字典结构: {"valid": bool, "errors": [...]}
    """
    errors: list[ValidationError] = []

    # 1. 校验 source_message_id 和 thread_id 必须存在且合法
    if not isinstance(source_message_id, str) or not source_message_id.strip():
        errors.append(
            {
                "field": "source_message_id",
                "message": "原邮件标识 (source_message_id) 必须存在且不能为空",
            }
        )
    elif re.search(r"\s", source_message_id):
        errors.append(
            {
                "field": "source_message_id",
                "message": "原邮件标识 (source_message_id) 不合法，不能包含空白字符",
            }
        )

    if not isinstance(thread_id, str) or not thread_id.strip():
        errors.append(
            {
                "field": "thread_id",
                "message": "邮件线程标识 (thread_id) 必须存在且不能为空",
            }
        )
    elif re.search(r"\s", thread_id):
        errors.append(
            {
                "field": "thread_id",
                "message": "邮件线程标识 (thread_id) 不合法，不能包含空白字符",
            }
        )

    # 2. 校验 to 必须为非空且符合 RFC 5322 格式的有效邮箱地址列表
    if not isinstance(to, list) or not to:
        errors.append(
            {
                "field": "to",
                "message": "收件人 (to) 必须为非空且符合 RFC 5322 格式的有效邮箱地址列表",
            }
        )
    else:
        for addr in to:
            if not is_valid_email_address(addr):
                errors.append(
                    {
                        "field": "to",
                        "message": f"邮箱格式错误: {addr}",
                    }
                )

    if isinstance(subject, str) and ("\r" in subject or "\n" in subject):
        errors.append({"field": "subject", "message": "邮件主题不能包含换行"})

    # 3. 校验 subject 与 body 不能为空或全空白字符
    if not isinstance(subject, str) or not subject.strip():
        errors.append(
            {
                "field": "subject",
                "message": "邮件主题 (subject) 不能为空或全空白字符",
            }
        )

    if not isinstance(body, str) or not body.strip():
        errors.append(
            {
                "field": "body",
                "message": "邮件正文 (body) 不能为空或全空白字符",
            }
        )

    return {
        "valid": len(errors) == 0,
        "errors": errors,
    }


def find_reply(conn: sqlite3.Connection, source_message_id: str) -> str | None:
    row = conn.execute(
        "SELECT operation_id FROM mail_reply_drafts WHERE source_message_id = ?",
        (source_message_id,),
    ).fetchone()
    return row["operation_id"] if row else None


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


def summary(operation: dict) -> dict:
    return {key: operation[key] for key in ("operation_id", "version", "status")}


class ReplyDraftStore:
    def __init__(self, path: Path | None = None):
        self.path = path

    def _validate(self, source: str, thread: str, to: list[str], subject: str, body: str) -> None:
        # 独立列表避免业务校验意外修改待保存的收件人。
        result = validate_reply_draft(
            source_message_id=source, thread_id=thread, to=list(to), subject=subject, body=body
        )
        if not result["valid"]:
            raise DraftValidationError(result["errors"])

    def _reuse(self, conn: sqlite3.Connection, task_id: str, operation_id: str) -> dict:
        operations.link(conn, task_id, operation_id)
        return summary(operations.operation(conn, operation_id))

    def save_reply_draft(
        self,
        task_id: str,
        source_message_id: str,
        thread_id: str,
        to: list[str],
        subject: str,
        body: str,
    ) -> dict:
        """去重先于校验（契约 §4）：复用已有操作时候选内容不参与校验。

        只读检查与纯函数校验都不持有写锁，并发准备互不阻塞；创建前的写事务内再查一次，
        两个并发请求只有一个能创建，另一个复用。
        """
        recipients = list(to)
        with session(self.path) as conn:
            operations.task(conn, task_id)
            known = find_reply(conn, source_message_id)
        if known is not None:
            with session(self.path) as conn, write(conn):
                return self._reuse(conn, task_id, known)
        self._validate(source_message_id, thread_id, recipients, subject, body)
        with session(self.path) as conn, write(conn):
            existing = find_reply(conn, source_message_id)
            if existing is not None:
                return self._reuse(conn, task_id, existing)
            operation = create_operation(conn, task_id, "mail_reply")
            operation_id = operation["operation_id"]
            insert_draft(conn, operation_id, source_message_id, thread_id)
            insert_version(conn, operation_id, 1, recipients, subject, body, timestamp())
            return summary(operation)

    def get_reply_draft(self, operation_id: str, version: int | None = None) -> dict:
        with session(self.path) as conn:
            return draft(conn, operation_id, version)

    def update_reply_draft(
        self, operation_id: str, expected_version: int, to: list[str], subject: str, body: str
    ) -> dict:
        recipients = list(to)
        current = self.get_reply_draft(operation_id)
        check_editable(current, expected_version)
        self._validate(
            current["source_message_id"], current["thread_id"], recipients, subject, body
        )
        with session(self.path) as conn, write(conn):
            version = next_version(conn, operation_id, expected_version)
            insert_version(conn, operation_id, version, recipients, subject, body, timestamp())
            return {"operation_id": operation_id, "version": version}
