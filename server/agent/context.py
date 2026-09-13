"""每轮上下文：系统提示与技能名单的组装。

本轮材料（触发载荷、执行结果等）以带标题的块追加在系统提示末尾，不作为用户消息进入
对话历史：历史接口因此只包含双方真实说过的内容，模型也能分清哪些是用户说的、哪些是
系统回传的。基础提示固定在最前且逐字节不变，材料只追加、不插入。
"""

from __future__ import annotations

import json
from collections.abc import Iterable
from dataclasses import dataclass, field

from server.agent.prompt import BASE_PROMPT


@dataclass(frozen=True)
class Material:
    """追加到系统提示的一份材料：dict 渲染为 JSON 块，str 渲染为纯文本。"""

    title: str
    content: dict | str


@dataclass(frozen=True)
class TurnContext:
    """一轮调用的模型可见配置。"""

    system_prompt: str
    # 启用哪些 Skill（SDK 的 skills 选项）；空列表即不启用，接入已批准名单后在这里扩展。
    skills: list[str] = field(default_factory=list)


def assemble(*, materials: Iterable[Material] = ()) -> TurnContext:
    """组装一轮的上下文：基础提示在前，本轮材料按序追加在系统提示末尾。"""
    parts = [BASE_PROMPT]
    for material in materials:
        body = (
            json.dumps(material.content, ensure_ascii=False, indent=2)
            if isinstance(material.content, dict)
            else material.content
        )
        parts.append(f"## {material.title}\n{body}")
    return TurnContext(system_prompt="\n\n".join(parts))
