"""工具装配：把注册表里的声明绑定到具体依赖，并按轮次给出模型可见范围。

注册表（`server/tools/registry.py`）只记录声明，本模块做两件事：

- 装配：工具函数上的仅关键字参数按名字绑定到 `ToolDeps` 的字段，绑定在装配期完成，
  缺依赖或名字对不上直接报错，不推迟到模型调用时；
- 范围：按本轮允许的副作用筛选。新邮件轮只分析，写工具只出现在后续轮次。

接入新服务（日历、知识库等）时只需在 `ToolDeps` 加字段，工具函数声明同名仅关键字参数，
装配与筛选逻辑不用改。
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, fields, replace
from enum import StrEnum
from functools import partial
from inspect import Parameter, signature

from server.approval.service import ConfirmationService
from server.sessions.service import SessionStore
from server.tools.calendar.service import CalendarEventStore
from server.tools.calendar.tools import CalendarReader
from server.tools.gmail.client import BaseGmailClient
from server.tools.gmail.service import MailDraftStore
from server.tools.registry import SideEffect, ToolDefinition, ToolRegistry, default_registry


class TurnKind(StrEnum):
    """一轮输入的种类，与调度记录 `agent_runs.kind` 一致。"""

    NEW_MAIL = "new_mail"
    MESSAGE = "message"
    EXECUTION_RESULT = "execution_result"


# 每类轮次允许模型看到并调用的副作用集合。EXTERNAL_WRITE 永不出现，由 Confirmation 在用户
# 确认最终版本后调用。DIRECT_EXTERNAL_WRITE 只出现在用户亲自发起的轮次：触发轮与结果回传轮
# 的输入都来自系统而非用户，拿不到直接外部写，因此外部内容中的指令无法驱动写入。
ALLOWED_EFFECTS: dict[TurnKind, frozenset[SideEffect]] = {
    TurnKind.NEW_MAIL: frozenset({SideEffect.READONLY}),
    TurnKind.MESSAGE: frozenset(
        {SideEffect.READONLY, SideEffect.LOCAL_WRITE, SideEffect.DIRECT_EXTERNAL_WRITE}
    ),
    TurnKind.EXECUTION_RESULT: frozenset({SideEffect.READONLY, SideEffect.LOCAL_WRITE}),
}


@dataclass(frozen=True)
class ToolDeps:
    """工具运行所需的装配依赖，一个字段对应一类服务。"""

    drafts: MailDraftStore
    tasks: SessionStore
    gmail: BaseGmailClient
    calendar: CalendarReader | None = None
    calendar_events: CalendarEventStore | None = None
    confirmations: ConfirmationService | None = None


def build_tools(
    deps: ToolDeps, *, registry: ToolRegistry = default_registry
) -> list[ToolDefinition]:
    """把注册表里的每个工具绑定到依赖，返回可交给网关的工具列表。

    仅关键字参数按名字从 `ToolDeps` 取值；`task_id` 除外，它是每轮由网关注入的任务身份。
    """
    available = {field.name: getattr(deps, field.name) for field in fields(deps)}
    bound: list[ToolDefinition] = []
    for definition in registry.list_tools():
        injected = {}
        unavailable = False
        for name, parameter in signature(definition.func).parameters.items():
            if parameter.kind is not Parameter.KEYWORD_ONLY or name == "task_id":
                continue
            if name not in available:
                raise RuntimeError(f"工具 {definition.name} 声明的依赖 {name} 不在 ToolDeps 中")
            if available[name] is None:
                unavailable = True
                break
            injected[name] = available[name]
        if not unavailable:
            bound.append(replace(definition, func=partial(definition.func, **injected)))
    return bound


def exposed_tools(
    tools: Iterable[ToolDefinition], *, allowed: frozenset[SideEffect]
) -> list[ToolDefinition]:
    """本轮模型可见的工具：副作用落在允许集合内的才出现，其余不进入 SDK。"""
    return [tool for tool in tools if tool.side_effect in allowed]
