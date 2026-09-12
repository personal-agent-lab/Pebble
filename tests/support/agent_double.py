"""Agent 网关测试替身：按脚本产生事件、记录每次调用并保存内存历史。

用于后端测试与 B 在真实 SDK 接入前的联调；生产代码不默认装配本模块。
"""

import threading
from collections.abc import AsyncIterator, Callable
from itertools import count
from typing import Any

from server.gateway.agent_contract import AgentEvent

Handler = Callable[..., AsyncIterator[AgentEvent]]


class FakeAgentGateway:
    """替身默认按类别给出最小事件，可用 handle() 替换为测试脚本。

    脚本是异步生成器函数，参数与对应网关方法一致，可在其中调用真实存储
    （例如保存草稿并以 draft_saved 通知实际版本）。
    """

    def __init__(self, *, session_prefix: str = "fake"):
        self.session_prefix = session_prefix
        self.calls: list[dict[str, Any]] = []
        self.history: dict[str, list[dict]] = {}
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

    async def stream_new_mail(
        self,
        *,
        task_id: str,
        sdk_session_id: str | None,
        source_message_id: str,
        thread_id: str,
    ) -> AsyncIterator[AgentEvent]:
        async for event in self._stream(
            "new_mail",
            task_id=task_id,
            sdk_session_id=sdk_session_id,
            source_message_id=source_message_id,
            thread_id=thread_id,
        ):
            yield event

    async def stream_message(
        self, *, task_id: str, sdk_session_id: str | None, message: str
    ) -> AsyncIterator[AgentEvent]:
        async for event in self._stream(
            "message", task_id=task_id, sdk_session_id=sdk_session_id, message=message
        ):
            yield event

    async def stream_execution_result(
        self,
        *,
        task_id: str,
        sdk_session_id: str,
        operation_id: str,
        version: int,
        result: dict,
    ) -> AsyncIterator[AgentEvent]:
        async for event in self._stream(
            "execution_result",
            task_id=task_id,
            sdk_session_id=sdk_session_id,
            operation_id=operation_id,
            version=version,
            result=result,
        ):
            yield event

    async def read_history(self, *, task_id: str, sdk_session_id: str | None) -> list[dict]:
        with self._lock:
            return [dict(message) for message in self.history.get(sdk_session_id or "", [])]

    def _remember(self, session_id: str | None, messages: list[dict]) -> None:
        if session_id is None or not messages:
            return
        with self._lock:
            self.history.setdefault(session_id, []).extend(messages)

    async def _stream(self, kind: str, **kwargs: Any) -> AsyncIterator[AgentEvent]:
        with self._lock:
            self.calls.append({"kind": kind, **kwargs})
        session_id = kwargs.get("sdk_session_id")
        # 边产生边记录：消费方在 done 处停止迭代，生成器不会回到循环之后。
        pending: list[dict] = []
        if kind == "message":
            pending.append({"role": "user", "text": kwargs["message"]})
        handler = self._handlers.get(kind)
        events = handler(**kwargs) if handler is not None else self._default(kind, **kwargs)
        async for event in events:
            if event.get("type") == "session":
                session_id = event["sdk_session_id"]
                self._remember(session_id, pending)
                pending.clear()
            elif event.get("type") == "text":
                text = {"role": "assistant", "text": event["text"]}
                if session_id is None:
                    pending.append(text)
                else:
                    self._remember(session_id, [text])
            yield event

    async def _default(self, kind: str, **kwargs: Any) -> AsyncIterator[AgentEvent]:
        session_id = kwargs.get("sdk_session_id") or self.new_session_id()
        yield {"type": "session", "sdk_session_id": session_id}
        if kind == "new_mail":
            yield {"type": "text", "text": f"新邮件 {kwargs['source_message_id']} 的摘要与建议"}
        elif kind == "message":
            yield {"type": "text", "text": f"收到：{kwargs['message']}"}
        else:
            yield {"type": "text", "text": f"发送结果：{kwargs['result']['status']}"}
        yield {"type": "done"}
