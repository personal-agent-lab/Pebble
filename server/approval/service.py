"""确认最终版本、取得一次执行权、保存结果并在不确定时只读核实。"""

import json
from pathlib import Path
from typing import Protocol
from uuid import uuid4

from server.approval import repository as repo
from server.db import session, write
from server.errors import (
    DependencyUnavailableError,
    DraftValidationError,
    NotEditableError,
    NotFoundError,
    VersionConflictError,
)
from server.sessions import repository as operations
from server.sessions import runs as agent_runs
from server.sessions.service import timestamp
from server.tools.calendar.service import preview as calendar_preview
from server.tools.calendar.service import validate_preview
from server.tools.gmail import service as mail

RECOVERED_REASON = "外部执行调用未完成即中断，结果待核实"
NOT_STARTED_REASON = "确认后尚未开始外部执行即中断，未产生外部写入"
VERIFIED_DELIVERY_SUFFIX = ":verified"


class Executor(Protocol):
    def __call__(self, **fields) -> dict: ...


def checked_result(returned: object, success: str = "sent", identifier: str = "message_id") -> dict:
    if isinstance(returned, dict) and isinstance(returned.get("status"), str):
        status = returned["status"]
        if status == success and isinstance(returned.get(identifier), str) and returned[identifier]:
            return {"status": success, identifier: returned[identifier]}
        if status in {"failed", "unknown"} and isinstance(returned.get("reason"), str):
            return {"status": status, "reason": returned["reason"]}
    return {"status": "unknown", "reason": f"执行结果不符合契约：{returned!r}"}


def result_response(row: dict) -> dict | None:
    return json.loads(row["result_json"]) if row["result_json"] is not None else None


def execution_response(row: dict) -> dict:
    confirmed = row["execution_task_id"] is not None
    return {
        "operation_id": row["operation_id"],
        "version": row["version"],
        "status": row["status"],
        "confirmation": (
            {
                "task_id": row["execution_task_id"],
                "version": row["confirmed_version"],
                "confirmed_at": row["confirmed_at"],
            }
            if confirmed
            else None
        ),
        "result": result_response(row) if confirmed else None,
    }


def register_delivery(
    conn, operation_id: str, task_id: str | None, now: str, *, reference: str | None = None
) -> None:
    if task_id is not None:
        agent_runs.insert(
            conn,
            str(uuid4()),
            task_id,
            agent_runs.KIND_EXECUTION_RESULT,
            {"operation_id": operation_id},
            reference or operation_id,
            now,
        )


class ConfirmationService:
    def __init__(
        self,
        send_message: Executor | None,
        verify_message: Executor | None = None,
        path: Path | None = None,
        *,
        create_event: Executor | None = None,
        verify_event: Executor | None = None,
    ):
        self.send_message, self.verify_message = send_message, verify_message
        self.create_event, self.verify_event = create_event, verify_event
        self.path = path

    def accept_confirmation(self, task_id: str, operation_id: str, version: int) -> dict:
        with session(self.path) as conn, write(conn):
            operations.task(conn, task_id)
            row = repo.view(conn, operation_id)
            if not conn.execute(
                "SELECT 1 FROM task_operations WHERE task_id=? AND operation_id=?",
                (task_id, operation_id),
            ).fetchone():
                raise NotFoundError(operation_id)
            if row["version"] != version:
                raise VersionConflictError(row["version"])
            if row["execution_task_id"] is not None:
                return execution_response(row)
            if row["status"] != "pending":
                raise NotEditableError(row["status"])
            kind = operations.operation(conn, operation_id)["type"]
            if kind == "mail":
                if self.send_message is None:
                    raise DependencyUnavailableError("邮件发送尚未接入")
                draft = mail.draft(conn, operation_id, version)
                valid = mail.validate_mail_draft(
                    kind=draft["kind"],
                    source_message_id=draft.get("source_message_id"),
                    thread_id=draft.get("thread_id"),
                    to=list(draft["to"]),
                    subject=draft["subject"],
                    body=draft["body"],
                    require_recipients=True,
                )
                if not valid["valid"]:
                    raise DraftValidationError(valid["errors"])
                active = "sending"
            elif kind == "calendar":
                if self.create_event is None:
                    raise DependencyUnavailableError("日程创建尚未接入")
                validate_preview(calendar_preview(conn, operation_id, version))
                active = "creating"
            else:
                raise DependencyUnavailableError("该操作尚未接入确认执行")
            now = timestamp()
            repo.insert(conn, operation_id, task_id, version, now)
            operations.update_status(conn, operation_id, active, now)
        return self.get_execution(operation_id)

    def execute_accepted(self, operation_id: str) -> dict | None:
        with session(self.path) as conn, write(conn):
            row = repo.view(conn, operation_id)
            if row["execution_task_id"] is None or not repo.start(conn, operation_id, timestamp()):
                return None
            kind = operations.operation(conn, operation_id)["type"]
            version = row["confirmed_version"]
            payload = (
                mail.draft(conn, operation_id, version)
                if kind == "mail"
                else calendar_preview(conn, operation_id, version)
            )
        try:
            if kind == "calendar":
                result = checked_result(
                    self.create_event(operation_id=operation_id, version=version, fields=payload),
                    "created",
                    "event_id",
                )
            else:
                returned = self.send_message(
                    operation_id=operation_id,
                    version=version,
                    kind=payload["kind"],
                    source_message_id=payload.get("source_message_id"),
                    thread_id=payload.get("thread_id"),
                    to=list(payload["to"]),
                    subject=payload["subject"],
                    body=payload["body"],
                )
                result = checked_result(returned)
        except Exception as error:
            result = {"status": "unknown", "reason": f"外部执行调用异常：{error!r}"}
        self._complete(operation_id, result)
        return self.get_execution(operation_id)

    def verify_pending(self, operation_id: str) -> dict:
        with session(self.path) as conn:
            row = repo.view(conn, operation_id)
            if row["status"] != "unknown":
                return execution_response(row)
            kind = operations.operation(conn, operation_id)["type"]
            version = row["confirmed_version"]
            payload = (
                mail.draft(conn, operation_id, version)
                if kind == "mail"
                else calendar_preview(conn, operation_id, version)
            )
        verifier = self.verify_event if kind == "calendar" else self.verify_message
        if verifier is None:
            raise DependencyUnavailableError("结果核实尚未接入")
        success, identifier = (
            ("created", "event_id") if kind == "calendar" else ("sent", "message_id")
        )
        try:
            returned = (
                verifier(operation_id=operation_id, fields=payload)
                if kind == "calendar"
                else verifier(
                    operation_id=operation_id,
                    kind=payload["kind"],
                    source_message_id=payload.get("source_message_id"),
                    thread_id=payload.get("thread_id"),
                    to=list(payload["to"]),
                    subject=payload["subject"],
                    body=payload["body"],
                )
            )
            result = checked_result(returned, success, identifier)
        except Exception as error:
            result = {"status": "unknown", "reason": f"核实调用异常：{error!r}"}
        if result["status"] == success:
            self._upgrade(operation_id, result)
        return self.get_execution(operation_id)

    def _upgrade(self, operation_id: str, result: dict) -> None:
        now = timestamp()
        with session(self.path) as conn, write(conn):
            row = repo.view(conn, operation_id)
            if row["status"] != "unknown":
                return
            repo.complete(
                conn,
                operation_id,
                result_json=json.dumps(result, ensure_ascii=False),
                completed_at=now,
            )
            operations.update_status(conn, operation_id, result["status"], now)
            if not agent_runs.delivery_unfinished(conn, operation_id):
                register_delivery(
                    conn,
                    operation_id,
                    row["execution_task_id"],
                    now,
                    reference=operation_id + VERIFIED_DELIVERY_SUFFIX,
                )

    def _complete(self, operation_id: str, result: dict) -> None:
        now = timestamp()
        with session(self.path) as conn, write(conn):
            row = repo.view(conn, operation_id)
            repo.complete(
                conn,
                operation_id,
                result_json=json.dumps(result, ensure_ascii=False),
                completed_at=now,
            )
            operations.update_status(conn, operation_id, result["status"], now)
            register_delivery(conn, operation_id, row["execution_task_id"], now)

    def get_execution(self, operation_id: str) -> dict:
        with session(self.path) as conn:
            return execution_response(repo.view(conn, operation_id))

    def get_agent_result(self, operation_id: str) -> dict | None:
        with session(self.path) as conn:
            row = repo.view(conn, operation_id)
        if row["completed_at"] is None or row["sdk_session_id"] is None:
            return None
        return {
            "task_id": row["execution_task_id"],
            "sdk_session_id": row["sdk_session_id"],
            "operation_id": operation_id,
            "version": row["confirmed_version"],
            "result": result_response(row),
        }

    def recover_interrupted_executions(self) -> list[str]:
        with session(self.path) as conn, write(conn):
            now = timestamp()
            rows = repo.interrupted(conn)
            for row in rows:
                started = row["started_at"] is not None
                result = {
                    "status": "unknown" if started else "failed",
                    "reason": RECOVERED_REASON if started else NOT_STARTED_REASON,
                }
                repo.complete(
                    conn,
                    row["operation_id"],
                    result_json=json.dumps(result, ensure_ascii=False),
                    completed_at=now,
                )
                operations.update_status(conn, row["operation_id"], result["status"], now)
                register_delivery(conn, row["operation_id"], row["task_id"], now)
            return [row["operation_id"] for row in rows]
