"""A/B Agent 接口：三类调用与历史读取。

A 通过本接口把输入交给 Agent 会话并消费事件流；实现由 B 提供（Qoder Agent SDK
装配），测试使用替身。接口只定义输入与事件，不规定 SDK 装配方式。
"""

from collections.abc import AsyncIterator
from typing import Protocol, TypedDict


class AgentEvent(TypedDict, total=False):
    """Agent 事件；type 取 session、text、draft_saved、done、error，见契约第 3 节。"""

    type: str
    sdk_session_id: str
    text: str
    operation_id: str
    version: int
    message: str


class AgentProtocolError(Exception):
    """Agent 事件流不符契约：缺字段、类型错误或未给出结束事件。"""


class AgentGateway(Protocol):
    def stream_new_mail(
        self,
        *,
        task_id: str,
        sdk_session_id: str | None,
        source_message_id: str,
        thread_id: str,
    ) -> AsyncIterator[AgentEvent]: ...

    def stream_message(
        self, *, task_id: str, sdk_session_id: str | None, message: str
    ) -> AsyncIterator[AgentEvent]: ...

    def stream_execution_result(
        self,
        *,
        task_id: str,
        sdk_session_id: str,
        operation_id: str,
        version: int,
        result: dict,
    ) -> AsyncIterator[AgentEvent]: ...

    async def read_history(self, *, task_id: str, sdk_session_id: str | None) -> list[dict]: ...


def checked_event(event: object) -> AgentEvent:
    """收敛 Agent 事件到契约结构；不符契约时按协议错误处理。"""
    if not isinstance(event, dict) or not isinstance(event.get("type"), str):
        raise AgentProtocolError(f"事件缺少类型：{event!r}")
    kind = event["type"]
    if kind not in {"session", "text", "draft_saved", "done", "error"}:
        raise AgentProtocolError(f"未知事件类型：{kind}")
    if kind == "session" and not isinstance(event.get("sdk_session_id"), str):
        raise AgentProtocolError(f"session 事件缺少会话标识：{event!r}")
    if kind == "text" and not isinstance(event.get("text"), str):
        raise AgentProtocolError(f"text 事件缺少文本：{event!r}")
    if kind == "draft_saved" and not (
        isinstance(event.get("operation_id"), str) and isinstance(event.get("version"), int)
    ):
        raise AgentProtocolError(f"draft_saved 事件缺少操作或版本：{event!r}")
    if kind == "error" and not isinstance(event.get("message"), str):
        raise AgentProtocolError(f"error 事件缺少原因：{event!r}")
    return event
