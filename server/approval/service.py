"""确认执行：版本校验、取得执行权、调用发送函数并保存实际结果。

发送与核实函数由调用方注入（Gmail 提供真实实现，测试使用替身），生产代码不提供默认值。
HTTP 入口只接受确认（accept_confirmation：写事务内检查版本、保存确认、取得执行权），
后台执行（execute_accepted）读取确认版本并调用发送函数；一次确认只调用一次，
已有执行记录或已开始的重复确认只返回已保存状态。
执行结果保存与待回传调用记录在同一事务内登记，重复确认不新增回传。

结果为 unknown 时由 verify_pending 只读核实实际结果（邮件契约 §4）：核实不重发，
只能把 unknown 升级为 sent，升级后按核实专用标识另登记一次回传。
"""

import sqlite3
from pathlib import Path
from typing import Protocol
from uuid import uuid4

from server.approval import repository as repo
from server.db import session, write
from server.errors import DependencyUnavailableError, NotEditableError, VersionConflictError
from server.sessions import repository as operations
from server.sessions import runs as agent_runs
from server.sessions.service import timestamp
from server.tools.gmail import service as mail
from server.uploads import UploadStore

RECOVERED_REASON = "发送调用未完成即中断，结果待核实"

# 取得执行权后进程即退出：发送函数从未被调用，按邮件契约 §4 不属于 unknown。
NOT_STARTED_REASON = "确认后尚未开始发送即中断，未进入执行阶段"

VERIFIED_DELIVERY_SUFFIX = ":verified"


class MailSender(Protocol):
    def __call__(
        self,
        *,
        operation_id: str,
        version: int,
        kind: str,
        to: list[str],
        subject: str,
        body: str,
        attachments: list[dict],
        source_message_id: str | None = None,
        thread_id: str | None = None,
    ) -> dict: ...


class MailVerifier(Protocol):
    """只读核实已确认版本的实际结果；内容字段是比对证据，不是重新发送的参数。"""

    def __call__(
        self,
        *,
        operation_id: str,
        kind: str,
        to: list[str],
        subject: str,
        body: str,
        attachments: list[dict],
        source_message_id: str | None = None,
        thread_id: str | None = None,
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


def register_delivery(
    conn: sqlite3.Connection,
    operation_id: str,
    task_id: str | None,
    now: str,
    *,
    reference: str | None = None,
) -> None:
    """为已保存的结果登记一次回传；同一标识只登记一次，跨进程恢复重复调用不会新增。

    `reference` 只作去重键：发送结果用操作标识，核实升级后的结果用核实专用标识，
    两者各登记一次。回传时读的是调用输入里的操作标识，与去重键无关。
    """
    if task_id is None:
        return
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
        send_message: MailSender | None,
        verify_message: MailVerifier | None = None,
        path: Path | None = None,
    ):
        self.send_message = send_message
        self.verify_message = verify_message
        self.path = path
        self.uploads = UploadStore(path)

    def accept_confirmation(self, task_id: str, operation_id: str, version: int) -> dict:
        """接受确认：写事务内检查版本、保存确认并取得执行权，返回当前状态。

        发送由后台执行；重复确认只返回已有状态，不再次发送，也不改变回传任务。
        """
        self._claim(task_id, operation_id, version)
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

    def verify_pending(self, operation_id: str) -> dict:
        """只读核实待核实的发送结果，返回操作当前状态。

        只对 unknown 生效：查到确认版本确实发出才升级为 sent，查不到保持原状态和原因，
        不改写为失败，也不重发。升级后另登记一次回传，让原会话拿到最终结果。
        """
        with session(self.path) as conn:
            row = repo.view(conn, operation_id)
            if row["status"] != "unknown":
                return execution_response(row)
            version = row["confirmed_version"]
            draft = mail.draft(conn, operation_id, version)
        if self.verify_message is None:
            raise DependencyUnavailableError("邮件结果核实尚未接入")
        result = self._verify(operation_id, draft)
        if result["status"] == "sent":
            self._upgrade(operation_id, result)
        return self.get_execution(operation_id)

    def _verify(self, operation_id: str, draft: dict) -> dict:
        try:
            attachments = self.uploads.contents(draft["attachments"])
            returned = self.verify_message(
                operation_id=operation_id,
                kind=draft["kind"],
                source_message_id=draft.get("source_message_id"),
                thread_id=draft.get("thread_id"),
                to=list(draft["to"]),
                subject=draft["subject"],
                body=draft["body"],
                attachments=attachments,
            )
        except Exception as error:  # 核实失败不能证明未发送，保持待核实
            return {"status": "unknown", "reason": f"核实调用异常：{error!r}"}
        checked = checked_result(returned)
        # 核实查不到不等于未发送：只接受 sent，其余一律按仍不确定处理，保留核实方给出的原因。
        if checked["status"] == "sent":
            return checked
        return {"status": "unknown", "reason": checked["reason"]}

    def _upgrade(self, operation_id: str, result: dict) -> None:
        """写事务内确认仍是待核实再落盘；并发核实只有第一个写入，也只登记一次回传。"""
        now = timestamp()
        with session(self.path) as conn, write(conn):
            row = repo.view(conn, operation_id)
            if row["status"] != "unknown":
                return
            repo.complete(
                conn, operation_id, message_id=result["message_id"], reason=None, completed_at=now
            )
            operations.update_status(conn, operation_id, "sent", now)
            # 原结果的回传还没跑完时，它读到的已经是升级后的结果，不必再登记一次。
            if not agent_runs.delivery_unfinished(conn, operation_id):
                register_delivery(
                    conn,
                    operation_id,
                    row["execution_task_id"],
                    now,
                    reference=operation_id + VERIFIED_DELIVERY_SUFFIX,
                )

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
        """把上次进程遗留的未完成执行按是否进入执行阶段归位，返回被处理的执行。

        由服务启动流程在接受请求前调用；不放进数据库初始化，避免普通初始化影响正在执行的调用。
        已取得执行权但 started_at 仍为空的记录从未调用发送函数，按邮件契约 §4 是明确未发送，
        记 failed 而不是 unknown；已开始的记 unknown，实际结果由后续核实兑现。
        两种结果都与正常执行一样登记回传，待兑现的发送结果不会遗漏。
        """
        with session(self.path) as conn, write(conn):
            now = timestamp()
            rows = repo.interrupted(conn)
            for row in rows:
                started = row["started_at"] is not None
                repo.complete(
                    conn,
                    row["operation_id"],
                    message_id=None,
                    reason=RECOVERED_REASON if started else NOT_STARTED_REASON,
                    completed_at=now,
                )
                status = "unknown" if started else "failed"
                operations.update_status(conn, row["operation_id"], status, now)
                register_delivery(conn, row["operation_id"], row["task_id"], now)
            return [row["operation_id"] for row in rows]

    def _claim(self, task_id: str, operation_id: str, version: int) -> bool:
        """写事务内取得执行权；已有执行记录时返回 False，由调用方读取已有状态。"""
        with session(self.path) as conn, write(conn):
            operations.task(conn, task_id)
            row = repo.view(conn, operation_id)
            if row["version"] != version:
                raise VersionConflictError(row["version"])
            if row["execution_task_id"] is not None:
                return False
            if row["status"] != "pending":
                raise NotEditableError(row["status"])
            if self.send_message is None:
                raise DependencyUnavailableError("邮件发送尚未接入")
            now = timestamp()
            repo.insert(conn, operation_id, task_id, version, now)
            operations.update_status(conn, operation_id, "sending", now)
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
            return version, mail.draft(conn, operation_id, version)

    def _send(self, operation_id: str, version: int, draft: dict) -> dict:
        try:
            attachments = self.uploads.contents(draft["attachments"])
            returned = self.send_message(
                operation_id=operation_id,
                version=version,
                kind=draft["kind"],
                source_message_id=draft.get("source_message_id"),
                thread_id=draft.get("thread_id"),
                to=list(draft["to"]),
                subject=draft["subject"],
                body=draft["body"],
                attachments=attachments,
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
                reason=result.get("reason"),
                completed_at=now,
            )
            operations.update_status(conn, operation_id, result["status"], now)
            register_delivery(conn, operation_id, row["execution_task_id"], now)
