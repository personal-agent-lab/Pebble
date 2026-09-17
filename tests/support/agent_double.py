"""Agent 网关测试替身：按脚本产生事件并记录每次调用。

用于后台调度测试与手动验收；生产代码不默认装配本模块。
"""

import threading
from collections.abc import AsyncIterator, Callable
from itertools import count
from typing import Any

from server.gateway.agent_contract import AgentEvent, Turn

Handler = Callable[..., AsyncIterator[AgentEvent]]


class FakeAgentGateway:
    """替身默认按类别给出最小事件，可用 handle() 替换为测试脚本。

    脚本是异步生成器函数，接收当轮 `turn`，可在其中调用真实存储
    （例如保存草稿并以 draft_saved 通知实际版本）。
    """

    def __init__(self, *, session_prefix: str = "fake", title: str = "替身标题"):
        self.session_prefix = session_prefix
        self.title = title
        self.calls: list[dict[str, Any]] = []
        self.title_calls: list[str] = []
        self.review_calls: list[dict[str, Any]] = []
        self.judge_calls: list[dict[str, Any]] = []
        # 可替换的一次性记忆回顾实现；缺省记录调用并返回空工具记录（无改动）。
        self.review_handler = None
        # 可替换的每轮记忆判断实现；缺省记录调用并返回空工具记录（无变化）。
        self.judge_handler = None
        self._handlers: dict[str, Handler] = {}
        self._sessions = count(1)
        self._lock = threading.Lock()

    def handle(self, kind: str, handler: Handler) -> None:
        self._handlers[kind] = handler

    def new_session_id(self) -> str:
        return f"{self.session_prefix}-session-{next(self._sessions)}"

    def calls_of(self, kind: str) -> list[dict[str, Any]]:
        with self._lock:
            return [call for call in self.calls if call["kind"] == kind]

    async def generate_title(self, text: str) -> str:
        with self._lock:
            self.title_calls.append(text)
        return self.title

    async def review_memory(self, task_id: str, instructions: str, transcript: str) -> list[dict]:
        with self._lock:
            self.review_calls.append(
                {"task_id": task_id, "instructions": instructions, "transcript": transcript}
            )
        if self.review_handler is not None:
            return await self.review_handler(task_id, instructions, transcript)
        return []

    async def judge_memory(self, task_id: str, instructions: str, message: str) -> list[dict]:
        with self._lock:
            self.judge_calls.append(
                {"task_id": task_id, "instructions": instructions, "message": message}
            )
        if self.judge_handler is not None:
            return await self.judge_handler(task_id, instructions, message)
        return []

    async def stream_turn(self, turn: Turn) -> AsyncIterator[AgentEvent]:
        async for event in self._stream(turn):
            yield event

    async def _stream(self, turn: Turn) -> AsyncIterator[AgentEvent]:
        kind = str(turn.kind)
        with self._lock:
            self.calls.append(
                {
                    "kind": kind,
                    "task_id": turn.task_id,
                    "sdk_session_id": turn.sdk_session_id,
                    "message": turn.message,
                    "model": turn.model,
                    "attachments": turn.attachments,
                    "materials": turn.materials,
                }
            )
        handler = self._handlers.get(kind)
        events = handler(turn=turn) if handler is not None else self._default(turn)
        async for event in events:
            yield event

    async def _default(self, turn: Turn) -> AsyncIterator[AgentEvent]:
        session_id = turn.sdk_session_id or self.new_session_id()
        yield {"type": "session", "sdk_session_id": session_id}
        kind = str(turn.kind)
        if kind == "new_mail":
            source_message_id = turn.materials[0].content["source_message_id"]
            yield {"type": "text", "text": f"新邮件 {source_message_id} 的摘要与建议"}
        elif kind == "message":
            yield {"type": "text", "text": f"收到：{turn.message}"}
        else:
            status = turn.materials[0].content["result"]["status"]
            yield {"type": "text", "text": f"发送结果：{status}"}
        yield {"type": "done"}
