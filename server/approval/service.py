"""确认执行：版本校验、取得执行权、调用发送函数并保存实际结果。

发送函数由调用方注入（B 提供真实实现，测试使用替身），生产代码不提供默认值。
HTTP 入口只接受确认（accept_confirmation：写事务内检查版本、保存确认、取得执行权），
后台执行（execute_accepted）读取确认版本并调用发送函数；一次确认只调用一次，
已有执行记录或已开始的重复确认只返回已保存状态。
执行结果保存与待回传调用记录在同一事务内登记，重复确认不新增回传。
"""

import json
import sqlite3
from pathlib import Path
from typing import Protocol
from uuid import uuid4

from server.approval import repository as repo
from server.db import session, write
from server.errors import (
    DependencyUnavailableError,
    NotEditableError,
    NotFoundError,
    VersionConflictError,
)
from server.sessions import repository as operations
from server.sessions import runs as agent_runs
from server.sessions.service import timestamp
from server.tools.calendar import service as calendar
from server.tools.gmail import repository as mail

RECOVERED_REASON = "执行调用未完成即中断，结果待核实"


class ReplySender(Protocol):
    def __call__(
        self,
        *,
        operation_id: str,
        version: int,
        source_message_id: str,
        thread_id: str,
        to: list[str],
        subject: str,
        body: str,
    ) -> dict: ...


def checked_result(returned: object) -> dict:
    """收敛发送函数返回值为契约结构；不符契约时按待核实处理。"""
    if isinstance(returned, dict) and isinstance(returned.get("status"), str):
        status = returned.get("status")
        if status == "sent" and isinstance(returned.get("message_id"), str):
            return {"status": "sent", "message_id": returned["message_id"]}
        if status in {"failed", "unknown"} and isinstance(returned.get("reason"), str):
            return {"status": status, "reason": returned["reason"]}
    return {"status": "unknown", "reason": f"发送结果不符合契约：{returned!r}"}


def result_response(row: dict) -> dict | None:
    if row["completed_at"] is None:
        return None
    if row.get("result_json") is not None:
        return json.loads(row["result_json"])
    if row["message_id"] is not None:
        return {"status": "sent", "message_id": row["message_id"]}
    return {"status": row["status"], "reason": row["reason"]}


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
    conn: sqlite3.Connection, operation_id: str, task_id: str | None, now: str
) -> None:
    """为已保存的结果登记一次回传；同一操作只登记一次，跨进程恢复重复调用不会新增。"""
    if task_id is None:
        return
    agent_runs.insert(
        conn,
        str(uuid4()),
        task_id,
        agent_runs.KIND_EXECUTION_RESULT,
        {"operation_id": operation_id},
        operation_id,
        now,
    )


def recover_interrupted_executions(path: Path | None = None) -> list[str]:
    """把上次进程遗留的执行中记录标记为待核实，返回被处理的执行。

    由服务启动流程在接受请求前调用；不放进数据库初始化，避免普通初始化影响正在执行的调用。
    恢复保存的结果与正常执行一样登记回传，待兑现的发送结果不会遗漏。
    """
    with session(path) as conn, write(conn):
        now = timestamp()
        operation_ids = repo.interrupted(conn)
        for operation_id in operation_ids:
            row = repo.view(conn, operation_id)
            repo.complete(
                conn,
                operation_id,
                message_id=None,
                reason=RECOVERED_REASON,
                completed_at=now,
            )
            operations.update_status(conn, operation_id, "unknown", now)
            register_delivery(conn, operation_id, row["execution_task_id"], now)
        return operation_ids


class ConfirmationService:
    def __init__(
        self, send_reply: ReplySender | None, path: Path | None = None, *, create_event=None
    ):
        self.send_reply = send_reply
        self.create_event = create_event
        self.path = path

    def accept_confirmation(self, task_id: str, operation_id: str, version: int) -> dict:
        """接受确认：写事务内检查版本、保存确认并取得执行权，返回当前状态。

        发送由后台执行；重复确认只返回已有状态，不再次发送，也不改变回传任务。
        """
        self._claim(task_id, operation_id, version)
        return self.get_execution(operation_id)

    def confirm_reply(self, task_id: str, operation_id: str, version: int) -> dict:
        """本地调用路径：接受确认后立即执行，供服务内联使用。"""
        self.accept_confirmation(task_id, operation_id, version)
        self.execute_accepted(operation_id)
        return self.get_execution(operation_id)

    def execute_accepted(self, operation_id: str) -> dict | None:
        """执行已接受的确认：只有首次开始的调用会发送，参数取自已确认版本。

        未被接受、已开始或已结束的操作直接返回 None，不重复发送，也不重复登记回传。
        """
        claimed = self._start(operation_id)
        if claimed is None:
            return None
        version, draft = claimed
        self._complete(operation_id, self._send(operation_id, version, draft))
        return self.get_execution(operation_id)

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
        return recover_interrupted_executions(self.path)

    def _claim(self, task_id: str, operation_id: str, version: int) -> bool:
        """写事务内取得执行权；已有执行记录时返回 False，由调用方读取已有状态。"""
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
                return False
            if row["status"] != "pending":
                raise NotEditableError(row["status"])
            executor = self.create_event if row["type"] == "calendar_create" else self.send_reply
            if executor is None:
                raise DependencyUnavailableError("操作执行尚未接入")
            now = timestamp()
            repo.insert(conn, operation_id, task_id, version, now)
            operations.update_status(
                conn,
                operation_id,
                "creating" if row["type"] == "calendar_create" else "sending",
                now,
            )
            return True

    def _start(self, operation_id: str) -> tuple[int, dict] | None:
        """写事务内标记执行开始并读取已确认版本内容，失败时不发送。"""
        with session(self.path) as conn, write(conn):
            row = repo.view(conn, operation_id)
            if row["execution_task_id"] is None:
                return None
            if not repo.start(conn, operation_id, timestamp()):
                return None
            version = row["confirmed_version"]
            return version, (
                calendar.draft(conn, operation_id, version)
                if row["type"] == "calendar_create"
                else mail.draft(conn, operation_id, version)
            )

    def _send(self, operation_id: str, version: int, draft: dict) -> dict:
        if draft.get("type") == "calendar_create":
            try:
                returned = self.create_event(
                    operation_id=operation_id, version=version, draft=draft
                )
                if isinstance(returned, dict):
                    if (
                        returned.get("status") == "created"
                        and returned.get("uid") == draft["uid"]
                        and isinstance(returned.get("resource_url"), str)
                        and returned["resource_url"]
                    ):
                        return {key: returned[key] for key in ("status", "uid", "resource_url")}
                    if returned.get("status") in {"failed", "unknown"} and isinstance(
                        returned.get("reason"), str
                    ):
                        return {key: returned[key] for key in ("status", "reason")}
            except Exception:
                pass
            return {"status": "unknown", "reason": "日程创建结果待核实，不会自动再次创建"}
        try:
            returned = self.send_reply(
                operation_id=operation_id,
                version=version,
                source_message_id=draft["source_message_id"],
                thread_id=draft["thread_id"],
                to=list(draft["to"]),
                subject=draft["subject"],
                body=draft["body"],
            )
        except Exception as error:  # 超时与普通异常都只说明结果待核实
            return {"status": "unknown", "reason": f"发送调用异常：{error!r}"}
        return checked_result(returned)

    def _complete(self, operation_id: str, result: dict) -> None:
        """保存实际结果并登记回传；两者同一事务，结果落盘就不会遗漏回传。"""
        now = timestamp()
        with session(self.path) as conn, write(conn):
            row = repo.view(conn, operation_id)
            repo.complete(
                conn,
                operation_id,
                message_id=result.get("message_id"),
                reason=result.get("reason")
                or ("日程已创建" if result["status"] == "created" else None),
                completed_at=now,
            )
            if row["type"] == "calendar_create":
                conn.execute(
                    "UPDATE approval_executions SET result_json=? WHERE operation_id=?",
                    (json.dumps(result), operation_id),
                )
            operations.update_status(conn, operation_id, result["status"], now)
            register_delivery(conn, operation_id, row["execution_task_id"], now)

    def verify_calendar(self, operation_id: str) -> dict:
        """服务端只读核实入口；只允许 unknown，绝不重新执行创建。"""
        from server.tools.calendar.client import verify_event

        with session(self.path) as conn:
            row = repo.view(conn, operation_id)
            if row["type"] != "calendar_create" or row["status"] != "unknown":
                raise NotEditableError(row["status"])
            saved = calendar.draft(conn, operation_id, row["confirmed_version"])
        result = verify_event(draft=saved)
        if result["status"] == "created":
            with session(self.path) as conn, write(conn):
                row = repo.view(conn, operation_id)
                if row["status"] == "unknown":
                    now = timestamp()
                    repo.complete(
                        conn, operation_id, message_id=None, reason="日程已创建", completed_at=now
                    )
                    conn.execute(
                        "UPDATE approval_executions SET result_json=? WHERE operation_id=?",
                        (json.dumps(result), operation_id),
                    )
                    operations.update_status(conn, operation_id, "created", now)
                    # Deliver verification once without replaying creation.
                    agent_runs.insert(
                        conn,
                        str(uuid4()),
                        row["execution_task_id"],
                        agent_runs.KIND_EXECUTION_RESULT,
                        {"operation_id": operation_id},
                        operation_id + ":verified",
                        now,
                    )
        return self.get_execution(operation_id)
