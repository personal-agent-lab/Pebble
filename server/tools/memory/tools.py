"""长期记忆写入工具：只供一次性记忆会话使用，不进入前台对话的工具列表。

前台主 Agent 不持有记忆工具：每个用户消息轮由独立的一次性判断会话决定写入
（judge_registry），后台定期回顾负责跨轮模式与整理（review_registry）。两者都能新增、
替换与删除；只有判断会话能向用户追问，回顾独立于对话运行，没有提问的对象。
"""

from server.memory.service import MemoryStore
from server.tools.registry import SideEffect, ToolRegistry


def add_memory(target: str, content: str, *, memory_store: MemoryStore) -> dict:
    """新增一个条目；与现有条目完全相同时不写入。"""
    return memory_store.apply("add", target, content, None)


def replace_memory(target: str, content: str, old_text: str, *, memory_store: MemoryStore) -> dict:
    """替换工具：old_text 只匹配一个现有条目时才执行。"""
    return memory_store.apply("replace", target, content, old_text)


def remove_memory(target: str, old_text: str, *, memory_store: MemoryStore) -> dict:
    """删除工具：old_text 只匹配一个现有条目时才执行。"""
    return memory_store.apply("remove", target, None, old_text)


def ask_memory(question: str) -> dict:
    """判断存在歧义时不做任何写入，把需要向用户确认的问题交回程序。"""
    return {"question": question}


ADD_DESCRIPTION = (
    "新增长期记忆条目。target=user 用于用户背景、身份、长期目标、学习方向、稳定偏好"
    "与对助理的持续要求；target=memory 用于跨会话持续适用的简短约定。每条简短明确。"
    "容量不足时返回 memory_full：先用 memory_replace 合并或精简已有条目，再重新保存。"
)
REPLACE_DESCRIPTION = (
    "替换一条已有长期记忆，也用于合并重复条目、精简措辞或更新过时内容。old_text 必须是"
    "取自当前记忆原文、只匹配一个条目的简短片段；content 是替换后的完整条目，涉及条件时"
    "保留条件，合并时不丢掉任何一条仍有效的含义。"
)
REMOVE_DESCRIPTION = (
    "删除一条已有长期记忆：用户要求忘记、条目已被合并进其他条目，或内容已明确失效时使用。"
    "old_text 必须是取自当前记忆原文、只匹配一个条目的简短片段。原对话仍保留。"
)

review_registry = ToolRegistry()
judge_registry = ToolRegistry()

for registry in (judge_registry, review_registry):
    registry.register(
        add_memory,
        name="memory_add",
        description=ADD_DESCRIPTION,
        side_effect=SideEffect.LOCAL_WRITE,
    )
    registry.register(
        replace_memory,
        name="memory_replace",
        description=REPLACE_DESCRIPTION,
        side_effect=SideEffect.LOCAL_WRITE,
    )
    registry.register(
        remove_memory,
        name="memory_remove",
        description=REMOVE_DESCRIPTION,
        side_effect=SideEffect.LOCAL_WRITE,
    )

judge_registry.register(
    ask_memory,
    name="memory_ask",
    description=(
        "判断存在歧义时向用户提出一句具体的确认问题，不做任何写入。"
        "问题要指出候选条目或两种理解，让用户一句话即可回答。"
    ),
    side_effect=SideEffect.READONLY,
)
