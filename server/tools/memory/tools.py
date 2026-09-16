"""长期记忆写入工具：只供一次性记忆会话使用，不进入前台对话的工具列表。

前台主 Agent 不再持有记忆工具：每个用户消息轮由独立的一次性判断会话决定写入
（judge_registry），后台定期回顾只负责跨轮模式的新增（review_registry）。
"""

from server.memory.service import MemoryStore
from server.tools.registry import SideEffect, ToolRegistry

review_registry = ToolRegistry()


def add_memory(target: str, content: str, *, memory_store: MemoryStore) -> dict:
    """后台记忆回顾专用的只新增工具：没有 action 参数，结构上无法表达替换或删除。"""
    return memory_store.apply("add", target, content, None)


review_registry.register(
    add_memory,
    name="memory_add",
    description=(
        "新增长期记忆条目，只在后台记忆回顾会话中使用，只能新增，不能修改或删除已有条目。"
        "target=user 用于用户背景、身份、长期目标、学习方向、工作习惯与对助理的稳定期待；"
        "target=memory 只用于跨会话持续适用的简短约定。具体项目资料、参考正文、术语资料"
        "和具体记录属于个人资料库，不得写入长期记忆。"
        "每次保存一条简短明确的条目；与现有条目重复或仅措辞不同的内容不要保存；"
        "密码、令牌等凭证不得保存。"
    ),
    side_effect=SideEffect.LOCAL_WRITE,
)


judge_registry = ToolRegistry()

judge_registry.register(
    add_memory,
    name="memory_add",
    description=(
        "新增长期记忆条目，只在每轮记忆判断会话中使用。"
        "target=user 用于用户背景、身份、长期目标、学习方向、稳定偏好与对助理的持续要求；"
        "target=memory 只用于跨会话持续适用的简短约定。具体项目资料、参考正文、术语资料"
        "和具体记录属于个人资料库，不得写入长期记忆。"
        "每次保存一条简短明确的条目；与现有条目重复或仅措辞不同的内容不要保存；"
        "密码、令牌等凭证不得保存。"
    ),
    side_effect=SideEffect.LOCAL_WRITE,
)


def replace_memory(target: str, content: str, old_text: str, *, memory_store: MemoryStore) -> dict:
    """记忆判断的替换工具：old_text 只匹配一个现有条目时才执行。"""
    return memory_store.apply("replace", target, content, old_text)


def remove_memory(target: str, old_text: str, *, memory_store: MemoryStore) -> dict:
    """记忆判断的停止使用工具：old_text 只匹配一个现有条目时才执行。"""
    return memory_store.apply("remove", target, None, old_text)


def ask_memory(question: str) -> dict:
    """判断存在歧义时不做任何写入，把需要向用户确认的问题交回程序。"""
    return {"question": question}


judge_registry.register(
    replace_memory,
    name="memory_replace",
    description=(
        "替换一条已有长期记忆，只在每轮记忆判断会话中使用。old_text 必须是取自当前记忆"
        "原文、只匹配一个条目的简短片段；content 是替换后的完整条目，涉及条件时保留条件。"
        "无法确定替换对象时不要调用本工具，改用 memory_ask。"
    ),
    side_effect=SideEffect.LOCAL_WRITE,
)

judge_registry.register(
    remove_memory,
    name="memory_remove",
    description=(
        "停止使用一条已有长期记忆，只在每轮记忆判断会话中使用。old_text 必须是取自当前"
        "记忆原文、只匹配一个条目的简短片段。停止使用保留版本历史，不是彻底清除。"
        "无法确定对象时不要调用本工具，改用 memory_ask。"
    ),
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
