"""Agent 接口：逐轮调用与历史读取。

Gateway 通过本接口把输入交给 Agent 会话并消费事件流；实现由 Qoder Agent SDK
装配提供，测试使用替身。接口只定义输入与事件，不规定 SDK 装配方式。
一轮的消息与材料由调用方（调度层与触发域）组装，网关不区分触发来源。
"""

from collections.abc import AsyncIterator
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol, TypedDict

from server.agent.context import Material
from server.agent.toolset import TurnKind


@dataclass(frozen=True)
class TurnAttachment:
    file_id: str
    filename: str
    mime_type: str
    size: int
    path: Path
    relative_path: str

    @property
    def is_image(self) -> bool:
        return self.mime_type.startswith("image/")


@dataclass(frozen=True)
class Turn:
    """一轮调用的输入；材料只进系统提示，不进对话历史。"""

    kind: TurnKind
    task_id: str
    sdk_session_id: str | None
    message: str
    model: str | None = None
    attachments: tuple[TurnAttachment, ...] = ()
    materials: tuple[Material, ...] = ()
    target_operation_id: str | None = None


class AgentEvent(TypedDict, total=False):
    """Agent 事件；type 取 session、text、notice、activity、draft_saved、done、error。

    activity 是一轮进行中的当前步骤说明，只实时转发、不进时间线。
    """

    type: str
    sdk_session_id: str
    text: str
    operation_id: str
    version: int
    message: str
    source: dict


class AgentProtocolError(Exception):
    """Agent 事件流不符契约：缺字段、类型错误或未给出结束事件。"""


class AgentGateway(Protocol):
    def stream_turn(self, turn: Turn) -> AsyncIterator[AgentEvent]: ...

    async def generate_title(self, text: str) -> str: ...

    async def generate_text(self, instructions: str, text: str) -> str: ...

    async def describe_image(self, instructions: str, data: bytes, mime_type: str) -> str: ...

    async def review_memory(
        self, task_id: str, instructions: str, transcript: str
    ) -> list[dict]: ...

    async def judge_memory(self, task_id: str, instructions: str, message: str) -> list[dict]: ...


def checked_event(event: object) -> AgentEvent:
    """收敛 Agent 事件到契约结构；不符契约时按协议错误处理。"""
    if not isinstance(event, dict) or not isinstance(event.get("type"), str):
        raise AgentProtocolError(f"事件缺少类型：{event!r}")
    kind = event["type"]
    if kind not in {"session", "text", "notice", "activity", "draft_saved", "done", "error"}:
        raise AgentProtocolError(f"未知事件类型：{kind}")
    if kind == "session" and not isinstance(event.get("sdk_session_id"), str):
        raise AgentProtocolError(f"session 事件缺少会话标识：{event!r}")
    if kind == "text" and not isinstance(event.get("text"), str):
        raise AgentProtocolError(f"text 事件缺少文本：{event!r}")
    if kind == "notice" and not isinstance(event.get("text"), str):
        raise AgentProtocolError(f"notice 事件缺少文本：{event!r}")
    if kind == "activity" and not isinstance(event.get("text"), str):
        raise AgentProtocolError(f"activity 事件缺少文本：{event!r}")
    if kind == "draft_saved" and not (
        isinstance(event.get("operation_id"), str) and isinstance(event.get("version"), int)
    ):
        raise AgentProtocolError(f"draft_saved 事件缺少操作或版本：{event!r}")
    if kind == "error" and not isinstance(event.get("message"), str):
        raise AgentProtocolError(f"error 事件缺少原因：{event!r}")
    return event
