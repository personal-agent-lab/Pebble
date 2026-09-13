"""本地回复草稿；不调用模型、不发送邮件。"""

from pathlib import Path
from typing import Protocol, TypedDict

from server.db import session, write
from server.errors import DependencyUnavailableError
from server.sessions import repository as operations
from server.sessions.service import check_editable, create_operation, next_version, timestamp
from server.tools.gmail import repository as repo


class FieldError(TypedDict):
    field: str
    message: str


class ValidationResult(TypedDict):
    valid: bool
    errors: list[FieldError]


class ReplyValidator(Protocol):
    def __call__(
        self, *, source_message_id: str, thread_id: str, to: list[str], subject: str, body: str
    ) -> ValidationResult: ...


class DraftValidationError(Exception):
    def __init__(self, errors: list[FieldError]):
        self.errors = errors
        super().__init__(str(errors))


def summary(operation: dict) -> dict:
    return {key: operation[key] for key in ("operation_id", "version", "status")}


class ReplyDraftStore:
    def __init__(self, validate_reply_draft: ReplyValidator | None, path: Path | None = None):
        self.validate = validate_reply_draft
        self.path = path

    def _validate(self, source: str, thread: str, to: list[str], subject: str, body: str) -> None:
        # 独立列表避免业务校验意外修改待保存的收件人。
        if self.validate is None:
            raise DependencyUnavailableError("邮件校验尚未接入")
        result = self.validate(
            source_message_id=source, thread_id=thread, to=list(to), subject=subject, body=body
        )
        if not result["valid"]:
            raise DraftValidationError(result["errors"])

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
        with session(self.path) as conn, write(conn):
            operations.task(conn, task_id)
            existing = repo.find_reply(conn, source_message_id)
            if existing is not None:
                operations.link(conn, task_id, existing)
                return summary(operations.operation(conn, existing))
        self._validate(source_message_id, thread_id, recipients, subject, body)
        with session(self.path) as conn, write(conn):
            operations.task(conn, task_id)
            existing = repo.find_reply(conn, source_message_id)
            if existing is not None:
                operations.link(conn, task_id, existing)
                return summary(operations.operation(conn, existing))
            operation = create_operation(conn, task_id, "mail_reply")
            operation_id = operation["operation_id"]
            repo.insert_draft(conn, operation_id, source_message_id, thread_id)
            repo.insert_version(conn, operation_id, 1, recipients, subject, body, timestamp())
            return summary(operation)

    def get_reply_draft(self, operation_id: str, version: int | None = None) -> dict:
        with session(self.path) as conn:
            return repo.draft(conn, operation_id, version)

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
            repo.insert_version(conn, operation_id, version, recipients, subject, body, timestamp())
            return {"operation_id": operation_id, "version": version}
