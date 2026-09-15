"""每轮上下文：系统提示与技能名单的组装。

基础提示在同一 SDK 会话中保持固定。本轮材料（触发载荷、执行结果、后续 Memory 等）渲染
为独立上下文，由网关在每次用户输入时注入，不作为用户消息进入应用时间线。这样恢复会话时
也能看到最新材料：Qoder 会复用会话建立时的 system_prompt，不能靠重新传入它更新材料。
"""

from __future__ import annotations

import json
from collections.abc import Iterable
from dataclasses import dataclass, field

from server.agent.prompt import BASE_PROMPT


@dataclass(frozen=True)
class Material:
    """注入当前轮的一份材料：dict 渲染为 JSON 块，str 渲染为纯文本。"""

    title: str
    content: dict | str


@dataclass(frozen=True)
class TurnContext:
    """一轮调用的模型可见配置。"""

    system_prompt: str
    additional_context: str = ""
    # 启用哪些 Skill（SDK 的 skills 选项）；空列表即不启用，接入已批准名单后在这里扩展。
    skills: list[str] = field(default_factory=list)


def render_materials(materials: Iterable[Material]) -> str:
    """把一轮材料按序渲染为给模型的附加上下文。"""
    parts = []
    for material in materials:
        body = (
            json.dumps(material.content, ensure_ascii=False, indent=2)
            if isinstance(material.content, dict)
            else material.content
        )
        parts.append(f"## {material.title}\n{body}")
    return "\n\n".join(parts)


def assemble(*, materials: Iterable[Material] = ()) -> TurnContext:
    """组装一轮上下文：基础提示固定，动态材料由调用层逐轮注入。"""
    return TurnContext(system_prompt=BASE_PROMPT, additional_context=render_materials(materials))
