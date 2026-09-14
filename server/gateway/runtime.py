"""后台调用管理：登记输入、按任务串行调用 Agent、发布事件与重启恢复。

HTTP 请求和 SSE 连接不持有工作生命周期：接受输入时先保存调用记录，再交给
应用后台任务执行；同一任务的输入按接受顺序处理，不与其他任务互相阻塞。"""

import asyncio
import json
import logging
import sqlite3
from contextlib import aclosing
from pathlib import Path
from typing import Protocol
from uuid import uuid4

from server.agent.context import Material
from server.agent.toolset import TurnKind
from server.approval.service import ConfirmationService
from server.db import session, write
from server.errors import DependencyUnavailableError, NotFoundError
from server.gateway.agent_contract import (
    AgentEvent,
    AgentGateway,
    AgentProtocolError,
    Turn,
    checked_event,
)
from server.sessions import repository as operations
from server.sessions import runs as repo
from server.sessions import timeline
from server.sessions.service import SessionStore, timestamp
from server.sessions.timeline import TimelineStore
from server.tools.calendar.service import CalendarPreviewStore
from server.tools.gmail.service import MailDraftStore
from server.tools.gmail.trigger import NEW_MAIL_GOAL, new_mail_content


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


# mail_task_links 只由新邮件入口读写：同一封邮件重复通知时找回原任务，不重复建任务。
def find_task_link(conn: sqlite3.Connection, source_message_id: str) -> str | None:
    row = conn.execute(
        "SELECT task_id FROM mail_task_links WHERE source_message_id = ?", (source_message_id,)
    ).fetchone()
    return row["task_id"] if row else None


def insert_task_link(
    conn: sqlite3.Connection, source_message_id: str, task_id: str, now: str
) -> None:
    conn.execute("INSERT INTO mail_task_links VALUES (?, ?, ?)", (source_message_id, task_id, now))


INTERRUPTED_REASON = "上次进程退出时调用尚未结束，已记录中断"

EXECUTION_RESULT_MESSAGE = "系统已完成你此前请求的操作，执行结果见系统提示。请向用户简要汇报。"
EXECUTION_RESULT_MATERIAL_TITLE = "执行结果（外部操作已结束，请据此向用户汇报）"
TARGET_DRAFT_MATERIAL_TITLE = "本轮指定修改的待确认内容"


def execution_result_content(
    *, operation_id: str, version: int, result: dict
) -> tuple[str, tuple[Material, ...]]:
    """执行结果回传轮的消息与材料：措辞属于确认子系统，与具体触发域无关。"""
    return EXECUTION_RESULT_MESSAGE, (
        Material(
            EXECUTION_RESULT_MATERIAL_TITLE,
            {"operation_id": operation_id, "version": version, "result": result},
        ),
    )


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
        self._drafts = MailDraftStore(path)
        self._calendar_previews = CalendarPreviewStore(path)
        self._timeline = TimelineStore(path)
        self._active: dict[str, asyncio.Task] = {}
        self._sends: dict[str, asyncio.Task] = {}
        self._titles: set[asyncio.Task] = set()
        self._closed = False

    def require_gateway(self) -> None:
        if self.gateway is None:
            raise DependencyUnavailableError("Agent 尚未接入")

    # ---------- 输入入口 ----------

    def accept_new_mail(self, source_message_id: str, thread_id: str) -> dict:
        """新邮件入口：同一事务创建任务、邮件关联和待处理调用；重复接收返回已有任务。"""
        self.require_gateway()
        with session(self.path) as conn, write(conn):
            existing = find_task_link(conn, source_message_id)
            if existing is not None:
                return operations.task(conn, existing)
            now = timestamp()
            task_id = str(uuid4())
            operations.insert_task(conn, task_id, NEW_MAIL_GOAL, now)
            insert_task_link(conn, source_message_id, task_id, now)
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

    def submit_message(
        self,
        task_id: str,
        message: str,
        *,
        target: dict | None = None,
    ) -> dict:
        """登记用户消息并返回调用记录；会话标识从任务记录读取。"""
        self.require_gateway()
        target_operation_id = None
        if target is not None:
            if target.get("kind") not in {"mail_draft", "calendar_preview"} or not isinstance(
                target.get("operation_id"), str
            ):
                raise ValueError("消息目标不合法")
            target_operation_id = target["operation_id"]
            known = {
                item["operation_id"]: item["type"]
                for item in self._sessions.list_task_operations(task_id)
            }
            expected_type = "calendar" if target["kind"] == "calendar_preview" else "mail"
            if known.get(target_operation_id) != expected_type:
                raise NotFoundError(target_operation_id)
        now = timestamp()
        run_id = str(uuid4())
        payload = {
            "message": message,
            "target": target,
        }
        with session(self.path) as conn, write(conn):
            operations.task(conn, task_id)
            repo.insert(conn, run_id, task_id, repo.KIND_MESSAGE, payload, None, now)
            timeline.insert_text(conn, task_id, run_id, "user", message)
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

    def get_timeline(self, task_id: str) -> dict:
        return self._timeline.list_items(task_id)

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
        for task in [*self._active.values(), *self._titles]:
            task.cancel()
        await asyncio.gather(*[*self._active.values(), *self._titles], return_exceptions=True)

    # ---------- 调用执行 ----------

    def _pending_rows(self) -> list[dict]:
        with session(self.path) as conn:
            return repo.pending(conn)

    def _ready(self, row: dict) -> bool:
        """执行结果回传要等结果已保存且任务已关联会话；其余输入接受即可执行。"""
        if row["kind"] != repo.KIND_EXECUTION_RESULT:
            return True
        if self.confirmations is None:
            return False
        # 操作标识取自调用输入：reference_id 只是回传的去重键，核实结果另有一个键。
        operation_id = json.loads(row["input"])["operation_id"]
        return self.confirmations.get_agent_result(operation_id) is not None

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
        """按调用种类组装一轮输入：触发域提供消息与材料，这里只负责构造 Turn。"""
        task_id = row["task_id"]
        sdk_session_id = self._sessions.get_task(task_id)["sdk_session_id"]
        if row["kind"] == repo.KIND_NEW_MAIL:
            message, materials = new_mail_content(**payload)
        elif row["kind"] == repo.KIND_MESSAGE:
            message = payload["message"]
            material_items = []
            target = payload.get("target")
            target_operation_id = target["operation_id"] if target is not None else None
            if target_operation_id is not None:
                content = (
                    self._drafts.get_draft(target_operation_id)
                    if target["kind"] == "mail_draft"
                    else self._calendar_previews.get_preview(target_operation_id)
                )
                material_items.append(
                    Material(
                        TARGET_DRAFT_MATERIAL_TITLE,
                        content,
                    )
                )
            materials = tuple(material_items)
        else:
            delivery = (
                self.confirmations.get_agent_result(payload["operation_id"])
                if self.confirmations is not None
                else None
            )
            if delivery is None:
                raise AgentProtocolError("回传输入不可用：结果或会话缺失")
            # 回传目标以确认记录为准：共享操作回到执行任务与其会话，而非发起任务。
            task_id = delivery["task_id"]
            sdk_session_id = delivery["sdk_session_id"]
            message, materials = execution_result_content(
                operation_id=delivery["operation_id"],
                version=delivery["version"],
                result=delivery["result"],
            )
        return self.gateway.stream_turn(
            Turn(
                kind=TurnKind(row["kind"]),
                task_id=task_id,
                sdk_session_id=sdk_session_id,
                message=message,
                materials=materials,
                target_operation_id=(
                    (payload.get("target") or {}).get("operation_id")
                    if row["kind"] == repo.KIND_MESSAGE
                    else None
                ),
            )
        )

    async def _stream(self, row: dict) -> None:
        terminal = None
        # 显式关闭事件流：异常路径也要走网关自己的清理（如撤销本轮登记的工具端点）。
        async with aclosing(self._invoke(row, json.loads(row["input"]))) as stream:
            async for raw in stream:
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
        published = dict(event)
        if kind == "session":
            self._bind_session(row["task_id"], event["sdk_session_id"])
        elif kind == "text":
            with session(self.path) as conn, write(conn):
                published["item_id"] = timeline.append_assistant_text(
                    conn, row["task_id"], row["run_id"], event["text"]
                )
        elif kind == "draft_saved":
            with session(self.path) as conn, write(conn):
                operation = operations.operation(conn, event["operation_id"])
                ensure = (
                    timeline.ensure_calendar_preview
                    if operation["type"] == "calendar"
                    else timeline.ensure_mail_draft
                )
                published["item_id"] = ensure(
                    conn, row["task_id"], row["run_id"], event["operation_id"]
                )
        elif kind == "done":
            self._finish(row["run_id"], "done", None)
            self._schedule_retitle(row)
        elif kind == "error":
            with session(self.path) as conn, write(conn):
                repo.finish(conn, row["run_id"], "error", event["message"], timestamp())
                published["item_id"] = timeline.insert_error(
                    conn, row["task_id"], row["run_id"], event["message"]
                )
        self.events.publish(row["task_id"], {"run_id": row["run_id"], **published})

    def _bind_session(self, task_id: str, sdk_session_id: str) -> None:
        self._sessions.bind_sdk_session(task_id, sdk_session_id)
        self.kick()

    # ---------- 任务标题 ----------

    def _schedule_retitle(self, row: dict) -> None:
        """任务首个调用成功结束后，用模型的短标题替换创建时的初始文案。"""
        if self.gateway is None:
            return
        with session(self.path) as conn:
            runs = repo.runs(conn, row["task_id"])
            if not runs or runs[0]["run_id"] != row["run_id"]:
                return
            text = timeline.run_text(conn, row["run_id"])
        if not text.strip():
            return
        task = asyncio.create_task(self._retitle(row["task_id"], text))
        self._titles.add(task)
        task.add_done_callback(self._titles.discard)

    async def _retitle(self, task_id: str, text: str) -> None:
        try:
            title = (await self.gateway.generate_title(text)).strip()
            if not title:
                return
            with session(self.path) as conn, write(conn):
                try:
                    operations.task(conn, task_id)
                except NotFoundError:
                    return  # 任务在标题生成期间被删除
                operations.update_goal(conn, task_id, title)
        except Exception:
            logging.getLogger(__name__).exception("标题落盘失败，保留任务原目标文案")

    def _finish(self, run_id: str, status: str, error: str | None) -> None:
        with session(self.path) as conn, write(conn):
            repo.finish(conn, run_id, status, error, timestamp())

    def _fail(self, row: dict, message: str) -> None:
        with session(self.path) as conn, write(conn):
            repo.finish(conn, row["run_id"], "error", message, timestamp())
            item_id = timeline.insert_error(conn, row["task_id"], row["run_id"], message)
        self.events.publish(
            row["task_id"],
            {"run_id": row["run_id"], "item_id": item_id, "type": "error", "message": message},
        )


class MailSource(Protocol):
    """新邮件来源的装配插孔：`create_app(mail_source=...)` 传入，恢复中断调用之后启动。

    检测逻辑不在这里：Gmail 同步游标与协议处理属于 Gmail 工具（`server/tools/gmail/sync.py`），
    检测到未处理邮件后调用 `accept_new_mail`，去重与任务创建仍由本模块保证。
    生产工厂装配 GmailSource，测试显式传入替身。
    """

    # 检测中断的原因，健康检查据此报 degraded；正常运行为 None。
    error: str | None

    async def start(self, agent: GatewayRuntime) -> None:
        """在应用事件循环中开始检测；实现自行持有后台任务。"""
        ...

    async def stop(self) -> None:
        """停止检测并等待后台任务结束。"""
        ...
