"""长期记忆写入工具：只供一次性记忆会话使用，不进入前台对话的工具列表。

前台主 Agent 不持有记忆工具：每个用户消息轮由独立的一次性判断会话决定写入
（judge_registry），后台定期回顾负责跨轮模式与整理（review_registry）。两者都用同一个
片段编辑工具追加、修改与删除；只有判断会话能向用户追问，回顾独立于对话运行，没有提问的对象。
"""

from server.memory.service import MemoryStore
from server.tools.registry import SideEffect, ToolRegistry


def edit_memory(
    target: str, old_text: str = "", new_text: str = "", *, memory_store: MemoryStore
) -> dict:
    """片段编辑：old_text 为空时追加，new_text 为空时删除，都有时替换。"""
    return memory_store.edit(target, old_text, new_text)


def ask_memory(question: str) -> dict:
    """判断存在歧义时不做任何写入，把需要向用户确认的问题交回程序。"""
    return {"question": question}


EDIT_DESCRIPTION = (
    "编辑一份长期记忆文档（Markdown）。target=user 用于用户背景、身份、长期目标、学习方向、"
    "稳定偏好与对助理的持续要求；target=memory 用于跨会话持续适用的简短约定。\n"
    "- 追加：old_text 留空，new_text 是要加入的内容（一行、一个列表项或一小段），加在文末。\n"
    "- 修改：old_text 是当前文档里恰好出现一次的原文片段，new_text 是替换后的文字；"
    "也用于合并重复内容、精简措辞、把新内容并入相关段落。\n"
    "- 删除：old_text 是要删掉的原文（连同列表符号取整行），new_text 留空。原对话仍保留。\n"
    "old_text 必须逐字取自材料里的当前记忆。涉及条件时保留条件。容量不足时返回 memory_full："
    "先修改或删除已有内容腾出空间，再重新保存。"
)

review_registry = ToolRegistry()
judge_registry = ToolRegistry()

for registry in (judge_registry, review_registry):
    registry.register(
        edit_memory,
        name="memory_edit",
        description=EDIT_DESCRIPTION,
        side_effect=SideEffect.LOCAL_WRITE,
    )

judge_registry.register(
    ask_memory,
    name="memory_ask",
    description=(
        "判断存在歧义时向用户提出一句具体的确认问题，不做任何写入。"
        "问题要指出候选内容或两种理解，让用户一句话即可回答。"
    ),
    side_effect=SideEffect.READONLY,
)
