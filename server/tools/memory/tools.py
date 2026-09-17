"""长期记忆写入工具：只供一次性记忆会话使用，不进入前台对话的工具列表。

前台主 Agent 不持有记忆工具：每个用户消息轮由独立的一次性判断会话决定写入
（judge_registry），后台定期回顾负责跨轮模式与整理（review_registry）。两者都用同一个
按行锚点编辑的工具追加、插入、修改、删除与移动；只有判断会话能向用户追问，回顾独立于对话运行，没有提问的对象。
"""

from server.memory.service import MemoryStore
from server.tools.registry import SideEffect, ToolRegistry


def edit_memory(operations: list[dict], *, memory_store: MemoryStore) -> dict:
    """按行锚点插入、替换、删除、移动整行或在分区末尾追加；一次调用可跨分区，整体生效。"""
    return memory_store.edit(operations)


def ask_memory(question: str) -> dict:
    """判断存在歧义时不做任何写入，把需要向用户确认的问题交回程序。"""
    return {"question": question}


# 只讲怎么调用：分区标准、锚点、各 action 与失败处理；写什么、不写什么见 notices.MEMORY_RULES。
EDIT_DESCRIPTION = (
    "编辑长期记忆的两份 Markdown 文档。\n"
    "\n"
    "分区：\n"
    "- user（关于你）：用户本人。身份、长期目标、学习与研究方向、兴趣、沟通与表达偏好、"
    "工作习惯、对助理的总体期望。例：“用户偏好先给结论再解释”。\n"
    "- memory（事实与约定）：用户以外、跨任务都成立的事实与约定。常用账号、日历、"
    "联系人的事实，固定的安排规则，使用各项服务时确认过的经验。"
    "例：“‘工作’日历用于内部会议”“内部会议默认 30 分钟”。\n"
    "\n"
    "材料里每个有文字的行写成“锚点| 原文”，如 `k3f9| - 用户默认使用简体中文。`。"
    "锚点只用来定位，不写进 text。\n"
    "\n"
    "operations 是一组修改，每项写明 action：\n"
    "- append：target + text，加在分区末尾。\n"
    "- insert：after（锚点）+ text，插在那一行下面；和已有内容相关时用它，不为此改写整段。\n"
    "- replace：anchor（可加 end_anchor 表示连续多行）+ text，替换这些行。\n"
    "- delete：anchor（可加 end_anchor），删除这些行。\n"
    "- move：anchor（可加 end_anchor）+ to，把这些行原样移到另一分区末尾；"
    "换分区用它，不拆成删除加追加。\n"
    "text 写完整的行（列表项带“- ”），多行用换行分隔。锚点都指调用前看到的内容；"
    "一次调用可以同时改两个分区，整体生效或整体失败，本轮的修改尽量放进一次调用。\n"
    "\n"
    "结果：成功时返回实际改动和带新锚点的最新内容，之后用新锚点。"
    "返回 invalid_memory（锚点失效或参数不对）时按返回的最新内容重新定位，再调用一次，最多一次；"
    "返回 memory_full 时用一次调用同时精简旧内容、加入新内容；其他失败不重试。"
)
OPERATIONS_SCHEMA = {
    "type": "array",
    "description": "一组修改，锚点都指调用前的内容，整体生效",
    "minItems": 1,
    "items": {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": ["append", "insert", "replace", "delete", "move"],
            },
            "target": {
                "type": "string",
                "enum": ["user", "memory"],
                "description": "append 的分区",
            },
            "after": {"type": "string", "description": "insert：插在这个锚点的行下面"},
            "anchor": {"type": "string", "description": "replace、delete、move 的起始行锚点"},
            "end_anchor": {"type": "string", "description": "可选，连续多行的结束行锚点"},
            "to": {"type": "string", "enum": ["user", "memory"], "description": "move 的目标分区"},
            "text": {
                "type": "string",
                "description": "append、insert、replace 写入的完整行，不带锚点",
            },
        },
        "required": ["action"],
    },
}

review_registry = ToolRegistry()
judge_registry = ToolRegistry()

for registry in (judge_registry, review_registry):
    registry.register(
        edit_memory,
        name="memory_edit",
        description=EDIT_DESCRIPTION,
        side_effect=SideEffect.LOCAL_WRITE,
        param_schemas={"operations": OPERATIONS_SCHEMA},
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
