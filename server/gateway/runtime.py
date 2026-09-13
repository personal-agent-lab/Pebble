"""后台调用管理：登记输入、按任务串行调用 Agent、发布事件与重启恢复。

HTTP 请求和 SSE 连接不持有工作生命周期：接受输入时先保存调用记录，再交给
应用后台任务执行；同一任务的输入按接受顺序处理，不与其他任务互相阻塞。"""

import asyncio
import json
import logging
from pathlib import Path
from uuid import uuid4

from server.approval.service import ConfirmationService
from server.db import session, write
from server.errors import DependencyUnavailableError
from server.gateway.agent_contract import (
    AgentEvent,
    AgentGateway,
    AgentProtocolError,
    checked_event,
)
from server.sessions import repository as operations
from server.sessions import runs as repo
from server.sessions.service import SessionStore, timestamp
from server.tools.gmail import repository as mail


class EventHub:
    def __init__(self):
        self._subscribers: dict[str, set[asyncio.Queue]] = {}

    def subscribe(self, task_id: str) -> asyncio.Queue:
        subscription = asyncio.Queue()
        self._subscribers.setdefault(task_id, set()).add(subscription)
        return subscription

    def unsubscribe(self, task_id: str, subscription: asyncio.Queue) -> None:
        targets = self._subscribers.get(task_id, set())
        targets.discard(subscription)
        if not targets:
            self._subscribers.pop(task_id, None)

    def publish(self, task_id: str, event: dict) -> None:
        for subscription in self._subscribers.get(task_id, ()):
            subscription.put_nowait(event)

    def subscriber_count(self, task_id: str) -> int:
        return len(self._subscribers.get(task_id, ()))


NEW_MAIL_GOAL = "处理新收到的邮件"


INTERRUPTED_REASON = "上次进程退出时调用尚未结束，已记录中断"


FINISHED_KINDS = ("done", "error")


class GatewayRuntime:
    def __init__(
        self,
        gateway: AgentGateway | None,
        *,
        confirmations: ConfirmationService | None = None,
        path: Path | None = None,
    ):
        self.gateway = gateway
        self.confirmations = confirmations
        self.path = path
        self.events = EventHub()
        self._sessions = SessionStore(path)
        self._active: dict[str, asyncio.Task] = {}
        self._sends: dict[str, asyncio.Task] = {}
        self._closed = False

    def require_gateway(self) -> None:
        if self.gateway is None:
            raise DependencyUnavailableError("Agent 尚未接入")

    # ---------- 输入入口 ----------

    def accept_new_mail(self, source_message_id: str, thread_id: str) -> dict:
        """新邮件入口：同一事务创建任务、邮件关联和待处理调用；重复接收返回已有任务。"""
        self.require_gateway()
        with session(self.path) as conn, write(conn):
            existing = mail.find_task_link(conn, source_message_id)
            if existing is not None:
                return operations.task(conn, existing)
            now = timestamp()
            task_id = str(uuid4())
            operations.insert_task(conn, task_id, NEW_MAIL_GOAL, now)
            mail.insert_task_link(conn, source_message_id, task_id, now)
            repo.insert(
                conn,
                str(uuid4()),
                task_id,
                repo.KIND_NEW_MAIL,
                {"source_message_id": source_message_id, "thread_id": thread_id},
                source_message_id,
                now,
            )
            task = operations.task(conn, task_id)
        self.kick()
        return task

    def submit_message(self, task_id: str, message: str) -> dict:
        """登记用户消息并返回调用记录；会话标识由 A 从任务记录读取。"""
        self.require_gateway()
        now = timestamp()
        run_id = str(uuid4())
        with session(self.path) as conn, write(conn):
            operations.task(conn, task_id)
            repo.insert(conn, run_id, task_id, repo.KIND_MESSAGE, {"message": message}, None, now)
            row = repo.run(conn, run_id)
        self.kick()
        return repo.run_response(row)

    def confirm_execution(self, operation_id: str) -> None:
        """后台执行已接受的确认；发送结果保存后由 kick 接续回传。"""
        if self.confirmations is None:
            raise RuntimeError("确认服务未接入")

        if operation_id not in self._sends:
            self._sends[operation_id] = asyncio.create_task(self._send(operation_id))

    async def _send(self, operation_id: str) -> None:
        try:
            await asyncio.to_thread(self.confirmations.execute_accepted, operation_id)
        except Exception:
            logging.getLogger(__name__).exception("后台发送失败，执行状态以数据库为准")
        finally:
            self._sends.pop(operation_id, None)
            self.kick()

    # ---------- 查询 ----------

    def get_run(self, run_id: str) -> dict:
        return repo.run_response(self._row(run_id))

    def list_runs(self, task_id: str) -> list[dict]:
        with session(self.path) as conn:
            operations.task(conn, task_id)
            return [repo.run_response(row) for row in repo.runs(conn, task_id)]

    def latest_run(self, task_id: str) -> dict | None:
        with session(self.path) as conn:
            row = repo.latest(conn, task_id)
        return repo.run_response(row) if row is not None else None

    async def read_history(self, task_id: str) -> dict:
        """读取完整历史对话；未关联会话时为空，不另行保存模型会话。"""
        self.require_gateway()
        sdk_session_id = self._sessions.get_task(task_id)["sdk_session_id"]
        if sdk_session_id is None:
            return {"task_id": task_id, "sdk_session_id": None, "messages": []}
        messages = await self.gateway.read_history(task_id=task_id, sdk_session_id=sdk_session_id)
        return {"task_id": task_id, "sdk_session_id": sdk_session_id, "messages": messages}

    def _row(self, run_id: str) -> dict:
        with session(self.path) as conn:
            return repo.run(conn, run_id)

    # ---------- 生命周期 ----------

    def resume(self) -> list[str]:
        """启动恢复：运行中的调用记为中断，不重复调用；待处理调用继续执行。"""
        with session(self.path) as conn, write(conn):
            interrupted = repo.interrupt_running(conn, timestamp(), INTERRUPTED_REASON)
        self.kick()
        return interrupted

    def kick(self) -> None:
        """在应用事件循环中，每个任务只启动最早的待处理输入。"""
        if self._closed or self.gateway is None:
            return
        seen = set()
        for row in self._pending_rows():
            task_id = row["task_id"]
            if task_id in seen or task_id in self._active:
                continue
            if not self._ready(row):
                continue  # 尚无会话的回传需等待后续用户输入建立会话。
            seen.add(task_id)
            self._active[task_id] = asyncio.create_task(self._execute(row["run_id"]))

    async def close(self) -> None:
        self._closed = True
        # 同步发送已经在线程池中开始，正常关闭时等待结果落盘。
        if self._sends:
            await asyncio.gather(*list(self._sends.values()), return_exceptions=True)
        active = list(self._active.values())
        for task in active:
            task.cancel()
        await asyncio.gather(*active, return_exceptions=True)

    # ---------- 调用执行 ----------

    def _pending_rows(self) -> list[dict]:
        with session(self.path) as conn:
            return repo.pending(conn)

    def _ready(self, row: dict) -> bool:
        """执行结果回传要等结果已保存且任务已关联会话；其余输入接受即可执行。"""
        if row["kind"] != repo.KIND_EXECUTION_RESULT:
            return True
        if self.confirmations is None or row["reference_id"] is None:
            return False
        return self.confirmations.get_agent_result(row["reference_id"]) is not None

    async def _execute(self, run_id: str) -> None:
        row = self._row(run_id)
        try:
            if not self._claim(run_id):
                return
            try:
                await self._stream(row)
            except Exception as error:
                self._fail(row, f"Agent 调用失败：{error}")
        finally:
            self._active.pop(row["task_id"], None)
            self.kick()

    def _claim(self, run_id: str) -> bool:
        with session(self.path) as conn, write(conn):
            return repo.claim(conn, run_id, timestamp())

    def _invoke(self, row: dict, payload: dict):
        task_id = row["task_id"]
        sdk_session_id = self._sessions.get_task(task_id)["sdk_session_id"]
        if row["kind"] == repo.KIND_NEW_MAIL:
            return self.gateway.stream_new_mail(
                task_id=task_id,
                sdk_session_id=sdk_session_id,
                source_message_id=payload["source_message_id"],
                thread_id=payload["thread_id"],
            )
        if row["kind"] == repo.KIND_MESSAGE:
            return self.gateway.stream_message(
                task_id=task_id, sdk_session_id=sdk_session_id, message=payload["message"]
            )
        delivery = (
            self.confirmations.get_agent_result(row["reference_id"])
            if self.confirmations is not None
            else None
        )
        if delivery is None:
            raise AgentProtocolError("回传输入不可用：结果或会话缺失")
        return self.gateway.stream_execution_result(**delivery)

    async def _stream(self, row: dict) -> None:
        terminal = None
        async for raw in self._invoke(row, json.loads(row["input"])):
            if terminal is not None:
                raise AgentProtocolError("结束事件之后仍有事件")
            event = checked_event(raw)
            if event["type"] in FINISHED_KINDS:
                terminal = event
            else:
                self._forward(row, event)
        if terminal is None:
            raise AgentProtocolError("事件流未给出 done 或 error")
        self._forward(row, terminal)

    def _forward(self, row: dict, event: AgentEvent) -> None:
        """保存会话关联或结束状态并转发已校验的事件。"""
        kind = event["type"]
        if kind == "session":
            self._bind_session(row["task_id"], event["sdk_session_id"])
        elif kind == "done":
            self._finish(row["run_id"], "done", None)
        elif kind == "error":
            self._finish(row["run_id"], "error", event["message"])
        self.events.publish(row["task_id"], {"run_id": row["run_id"], **event})

    def _bind_session(self, task_id: str, sdk_session_id: str) -> None:
        self._sessions.bind_sdk_session(task_id, sdk_session_id)
        self.kick()

    def _finish(self, run_id: str, status: str, error: str | None) -> None:
        with session(self.path) as conn, write(conn):
            repo.finish(conn, run_id, status, error, timestamp())

    def _fail(self, row: dict, message: str) -> None:
        self._finish(row["run_id"], "error", message)
        self.events.publish(
            row["task_id"], {"run_id": row["run_id"], "type": "error", "message": message}
        )
