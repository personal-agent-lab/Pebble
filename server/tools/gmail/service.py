"""邮件草稿：业务校验、版本存储与去重。

新邮件与回复共用同一份草稿和版本实现；只有创建入口不同。
本模块不调用模型、不发送邮件。
"""

from __future__ import annotations

import json
import re
import sqlite3
from email.utils import parseaddr
from pathlib import Path
from typing import Literal, TypedDict

from server.db import session, write
from server.errors import DraftValidationError, NotFoundError
from server.sessions import repository as operations
from server.sessions.service import check_editable, create_operation, next_version, timestamp

DraftKind = Literal["reply", "new"]
EMAIL_REGEX = re.compile(r"^[a-zA-Z0-9_.+-]+@[a-zA-Z0-9-]+(?:\.[a-zA-Z0-9-]+)+$")


class ValidationError(TypedDict):
    field: str
    message: str


class ValidationResult(TypedDict):
    valid: bool
    errors: list[ValidationError]


def is_valid_email_address(addr: str) -> bool:
    if not addr or not isinstance(addr, str) or "\r" in addr or "\n" in addr:
        return False
    clean = addr.strip()
    if not clean:
        return False
    _, parsed = parseaddr(clean)
    return bool(EMAIL_REGEX.match(parsed.strip() if parsed else clean))


def validate_draft(
    to: list[str], subject: str, body: str, *, require_recipients: bool = False
) -> ValidationResult:
    """校验新邮件和回复共有的内容，不保存也不改写。

    草稿只要求主题与正文非空，收件人可以留空待填；发送口径（`require_recipients`）
    额外要求至少一个收件人，缺失只在确认发送时拒绝，不阻止保存草稿。
    """
    errors: list[ValidationError] = []
    if not isinstance(to, list):
        errors.append({"field": "to", "message": "收件人 (to) 必须为邮箱地址列表"})
    else:
        if require_recipients and not to:
            errors.append({"field": "to", "message": "收件人 (to) 不能为空，填写后才能发送"})
        for address in to:
            if not is_valid_email_address(address):
                errors.append({"field": "to", "message": f"邮箱格式错误: {address}"})
    if isinstance(subject, str) and ("\r" in subject or "\n" in subject):
        errors.append({"field": "subject", "message": "邮件主题不能包含换行"})
    if not isinstance(subject, str) or not subject.strip():
        errors.append({"field": "subject", "message": "邮件主题 (subject) 不能为空"})
    if not isinstance(body, str) or not body.strip():
        errors.append({"field": "body", "message": "邮件正文 (body) 不能为空"})
    return {"valid": not errors, "errors": errors}


def validate_reply_identity(source_message_id: str, thread_id: str) -> ValidationResult:
    errors: list[ValidationError] = []
    for field, value, label in (
        ("source_message_id", source_message_id, "原邮件标识"),
        ("thread_id", thread_id, "邮件往来标识"),
    ):
        if not isinstance(value, str) or not value.strip():
            errors.append({"field": field, "message": f"{label} ({field}) 不能为空"})
        elif re.search(r"\s", value):
            errors.append(
                {"field": field, "message": f"{label} ({field}) 不合法，不能包含空白字符"}
            )
    return {"valid": not errors, "errors": errors}


def validate_mail_draft(
    *,
    kind: DraftKind,
    to: list[str],
    subject: str,
    body: str,
    source_message_id: str | None = None,
    thread_id: str | None = None,
    require_recipients: bool = False,
) -> ValidationResult:
    content = validate_draft(to, subject, body, require_recipients=require_recipients)
    if kind == "new":
        return content
    identity = validate_reply_identity(source_message_id or "", thread_id or "")
    return {
        "valid": content["valid"] and identity["valid"],
        "errors": identity["errors"] + content["errors"],
    }


def find_reply(conn: sqlite3.Connection, source_message_id: str) -> str | None:
    row = conn.execute(
        "SELECT operation_id FROM mail_drafts WHERE source_message_id = ?", (source_message_id,)
    ).fetchone()
    return row["operation_id"] if row else None


def insert_draft(
    conn: sqlite3.Connection,
    operation_id: str,
    kind: DraftKind,
    source_message_id: str | None,
    thread_id: str | None,
) -> None:
    conn.execute(
        "INSERT INTO mail_drafts VALUES (?, ?, ?, ?)",
        (operation_id, kind, source_message_id, thread_id),
    )


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
        "INSERT INTO mail_draft_versions "
        "(operation_id, version, recipients, subject, body, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (
            operation_id,
            version,
            json.dumps(to, ensure_ascii=False),
            subject,
            body,
            now,
        ),
    )


def draft(conn: sqlite3.Connection, operation_id: str, version: int | None) -> dict:
    row = conn.execute(
        "SELECT o.operation_id, d.kind, v.version, o.status, d.source_message_id, d.thread_id, "
        "v.recipients, v.subject, v.body FROM operations o "
        "JOIN mail_drafts d ON d.operation_id = o.operation_id "
        "JOIN mail_draft_versions v ON v.operation_id = o.operation_id "
        "AND v.version = COALESCE(?, o.version) WHERE o.operation_id = ?",
        (version, operation_id),
    ).fetchone()
    if row is None:
        raise NotFoundError(f"{operation_id}:{version}")
    result = dict(row)
    result["to"] = json.loads(result.pop("recipients"))
    if result["kind"] == "new":
        result.pop("source_message_id")
        result.pop("thread_id")
    return result


def summary(operation: dict) -> dict:
    # presented_to_user 如实陈述工具效果：草稿保存后由系统以审阅卡片呈现给用户。
    return {
        "operation_id": operation["operation_id"],
        "version": operation["version"],
        "status": operation["status"],
        "presented_to_user": True,
    }


class MailDraftStore:
    def __init__(self, path: Path | None = None):
        self.path = path

    @staticmethod
    def _validate(**fields) -> None:
        # 校验器只能检查内容，不能通过修改可变列表改写待保存的草稿。
        validated = {**fields, "to": list(fields["to"])}
        result = validate_mail_draft(**validated)
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
        recipients = list(to)
        with session(self.path) as conn:
            operations.task(conn, task_id)
            known = find_reply(conn, source_message_id)
        if known is not None:
            with session(self.path) as conn, write(conn):
                return self._reuse(conn, task_id, known)
        self._validate(
            kind="reply",
            source_message_id=source_message_id,
            thread_id=thread_id,
            to=recipients,
            subject=subject,
            body=body,
        )
        with session(self.path) as conn, write(conn):
            existing = find_reply(conn, source_message_id)
            if existing is not None:
                return self._reuse(conn, task_id, existing)
            operation = create_operation(conn, task_id, "mail")
            operation_id = operation["operation_id"]
            insert_draft(conn, operation_id, "reply", source_message_id, thread_id)
            insert_version(conn, operation_id, 1, recipients, subject, body, timestamp())
            return summary(operation)

    def save_email_draft(
        self,
        task_id: str,
        to: list[str],
        subject: str,
        body: str,
    ) -> dict:
        recipients = list(to)
        self._validate(kind="new", to=recipients, subject=subject, body=body)
        with session(self.path) as conn, write(conn):
            operations.task(conn, task_id)
            operation = create_operation(conn, task_id, "mail")
            operation_id = operation["operation_id"]
            insert_draft(conn, operation_id, "new", None, None)
            insert_version(conn, operation_id, 1, recipients, subject, body, timestamp())
            return summary(operation)

    def get_draft(self, operation_id: str, version: int | None = None) -> dict:
        with session(self.path) as conn:
            return draft(conn, operation_id, version)

    def update_draft(
        self,
        operation_id: str,
        expected_version: int,
        to: list[str],
        subject: str,
        body: str,
    ) -> dict:
        recipients = list(to)
        current = self.get_draft(operation_id)
        check_editable(current, expected_version)
        self._validate(
            kind=current["kind"],
            source_message_id=current.get("source_message_id"),
            thread_id=current.get("thread_id"),
            to=recipients,
            subject=subject,
            body=body,
        )
        with session(self.path) as conn, write(conn):
            version = next_version(conn, operation_id, expected_version)
            insert_version(
                conn,
                operation_id,
                version,
                recipients,
                subject,
                body,
                timestamp(),
            )
            return {
                "operation_id": operation_id,
                "version": version,
                "status": "pending",
                "presented_to_user": True,
            }
