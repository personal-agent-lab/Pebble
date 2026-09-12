"""确认执行：版本校验、取得执行权、调用发送函数并保存实际结果。

发送函数由调用方注入（B 提供真实实现，测试使用替身），生产代码不提供默认值。
一次确认只调用发送函数一次：写事务内以唯一执行记录取得执行权，事务外发送，
结果写入新事务；已有执行记录的重复确认只返回已保存状态。
"""

from pathlib import Path
from typing import Protocol

from server.approval import repository as repo
from server.db import session, write
from server.sessions import repository as operations
from server.sessions.errors import NotEditableError, VersionConflictError
from server.sessions.service import timestamp
from server.tools.gmail import repository as mail

RECOVERED_REASON = "发送调用未完成即中断，结果待核实"


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


def recover_interrupted_executions(path: Path | None = None) -> list[str]:
    """把上次进程遗留的执行中记录标记为待核实，返回被处理的执行。

    由服务启动流程在接受请求前调用；不放进数据库初始化，避免普通初始化影响正在执行的调用。
    """
    with session(path) as conn, write(conn):
        now = timestamp()
        operation_ids = repo.interrupted(conn)
        for operation_id in operation_ids:
            repo.complete(
                conn,
                operation_id,
                message_id=None,
                reason=RECOVERED_REASON,
                completed_at=now,
            )
            operations.update_status(conn, operation_id, "unknown", now)
        return operation_ids


class ConfirmationService:
    def __init__(self, send_reply: ReplySender, path: Path | None = None):
        self.send_reply = send_reply
        self.path = path

    def confirm_reply(self, task_id: str, operation_id: str, version: int) -> dict:
        draft = self._claim(task_id, operation_id, version)
        if draft is not None:
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

    def _claim(self, task_id: str, operation_id: str, version: int) -> dict | None:
        """写事务内取得执行权；已有执行记录时返回 None，由调用方读取已有状态。"""
        with session(self.path) as conn, write(conn):
            operations.task(conn, task_id)
            row = repo.view(conn, operation_id)
            if row["version"] != version:
                raise VersionConflictError(row["version"])
            if row["execution_task_id"] is not None:
                return None
            if row["status"] != "pending":
                raise NotEditableError(row["status"])
            now = timestamp()
            repo.insert(conn, operation_id, task_id, version, now)
            operations.update_status(conn, operation_id, "sending", now)
            return mail.draft(conn, operation_id, version)

    def _send(self, operation_id: str, version: int, draft: dict) -> dict:
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
        now = timestamp()
        with session(self.path) as conn, write(conn):
            repo.complete(
                conn,
                operation_id,
                message_id=result.get("message_id"),
                reason=result.get("reason"),
                completed_at=now,
            )
            operations.update_status(conn, operation_id, result["status"], now)
