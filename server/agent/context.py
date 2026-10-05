"""每轮上下文：系统提示与技能名单的组装。

基础提示在同一 SDK 会话中保持固定。背景材料由 materials.py 跟踪内容摘要，首次与压缩后
完整提供，普通恢复只追加变化。本轮事件随输入追加，不作为用户消息进入应用时间线。
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
    # 可选的稳定身份，用于同名材料的会话去重；不进入模型正文。
    key: str | None = None


@dataclass(frozen=True)
class TurnContext:
    """一轮调用的模型可见配置。"""

    system_prompt: str
    additional_context: str = ""
    # 启用哪些 Skill（SDK 的 skills 选项）；空列表即不启用，接入已批准名单后在这里扩展。
    skills: list[str] = field(default_factory=list)


def render_material(material: Material) -> str:
    """单份材料的渲染文本，与 render_materials 里那一段完全一致。"""
    body = (
        json.dumps(material.content, ensure_ascii=False, indent=2)
        if isinstance(material.content, dict)
        else material.content
    )
    return f"## {material.title}\n{body}"


def render_materials(materials: Iterable[Material]) -> str:
    """把一轮材料按序渲染为给模型的附加上下文。"""
    return "\n\n".join(render_material(material) for material in materials)


def assemble(*, materials: Iterable[Material] = ()) -> TurnContext:
    """组装一轮上下文：基础提示固定，动态材料由调用层逐轮注入。"""
    return TurnContext(system_prompt=BASE_PROMPT, additional_context=render_materials(materials))
