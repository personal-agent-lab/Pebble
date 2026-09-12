"""任务与操作规则，不包含邮件业务。"""

import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from server.db import session, write
from server.errors import NotEditableError, SessionConflictError, VersionConflictError
from server.sessions import repository as repo


def timestamp() -> str:
    return datetime.now(UTC).isoformat()


def check_editable(operation: dict, expected_version: int) -> None:
    if operation["version"] != expected_version:
        raise VersionConflictError(operation["version"])
    if operation["status"] != "pending":
        raise NotEditableError(operation["status"])


def create_operation(conn: sqlite3.Connection, task_id: str, kind: str) -> dict:
    operation_id = str(uuid4())
    repo.insert_operation(conn, operation_id, kind, task_id, timestamp())
    return repo.operation(conn, operation_id)


def next_version(conn: sqlite3.Connection, operation_id: str, expected_version: int) -> int:
    check_editable(repo.operation(conn, operation_id), expected_version)
    version = expected_version + 1
    repo.advance_version(conn, operation_id, version, timestamp())
    return version


class SessionStore:
    def __init__(self, path: Path | None = None):
        self.path = path

    def create_task(self, goal: str) -> dict:
        with session(self.path) as conn, write(conn):
            task_id = str(uuid4())
            repo.insert_task(conn, task_id, goal, timestamp())
            return repo.task(conn, task_id)

    def get_task(self, task_id: str) -> dict:
        with session(self.path) as conn:
            return repo.task(conn, task_id)

    def list_tasks(self) -> list[dict]:
        with session(self.path) as conn:
            return repo.tasks(conn)

    def bind_sdk_session(self, task_id: str, sdk_session_id: str) -> dict:
        with session(self.path) as conn, write(conn):
            current = repo.task(conn, task_id)["sdk_session_id"]
            if current is not None and current != sdk_session_id:
                raise SessionConflictError(task_id)
            repo.bind_session(conn, task_id, sdk_session_id)
            return repo.task(conn, task_id)

    def list_task_operations(self, task_id: str) -> list[dict]:
        with session(self.path) as conn:
            repo.task(conn, task_id)
            return repo.task_operations(conn, task_id)
